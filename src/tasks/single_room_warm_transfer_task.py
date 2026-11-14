"""
SingleRoomWarmTransferTask - Single Room Warm Transfer using Track Subscription Control

기존 LiveKit WarmTransferTask와 달리 단일 Room 내에서 Track Subscription 제어를 통해
Warm Transfer를 구현합니다.

핵심 차이점:
- Room 구조: 1개 (콜러와 상담원이 같은 방)
- 콜러 격리: Track Subscription 제어
- 최종 연결: Subscription 활성화 (moveParticipant 없음)
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import TYPE_CHECKING

from livekit import api, rtc
from livekit.agents import AgentTask, function_tool, get_job_context
from livekit.agents.voice.background_audio import (
    AudioConfig,
    BackgroundAudioPlayer,
    BuiltinAudioClip,
)
from livekit.rtc._proto.track_pb2 import ParticipantTrackPermission
from loguru import logger

from data import TransferRecord
from services import fake_transfer_gateway
from services.warm_transfer_briefing import (
    extract_warm_transfer_conversation_history,
    generate_warm_transfer_briefing_text,
)
from utils.agent_event import agent_event_extra
from utils.i18n import t
from utils.korean_speech_guard import KoreanSpeechGuardMixin

if TYPE_CHECKING:
    from livekit.agents import llm
    from livekit.agents.voice import SpeechHandle


# No-briefing auto-connect: max wait for in-flight "block agent → caller" calls
AUTO_CONNECT_BLOCK_DRAIN_TIMEOUT_SECONDS = 2.0
# No-briefing auto-connect: per-call timeout for strict unmute/subscribe API calls
AUTO_CONNECT_API_TIMEOUT_SECONDS = 5.0
# No-briefing auto-connect failure: max remove_participant attempts for the agent
AUTO_CONNECT_AGENT_REMOVAL_ATTEMPTS = 2


class WarmTransferStatus(Enum):
    """Warm transfer 완료 상태"""

    SUCCESS = "success"  # 연결 성공
    DECLINED = "declined"  # 상담원 거부
    VOICEMAIL = "voicemail"  # 음성사서함 감지
    TIMEOUT = "timeout"  # 타임아웃
    CALLER_DISCONNECTED = "caller_disconnected"  # 콜러 이탈
    ERROR = "error"  # 에러


@dataclass
class WarmTransferResult:
    """Warm transfer 결과"""

    status: WarmTransferStatus
    agent_identity: str | None = None
    error_message: str | None = None
    briefing_text: str | None = None
    # True when the agent leg could not be removed: the caller must not be
    # re-dialed while the stale agent may still be in the room.
    terminate_call: bool = False


BASE_INSTRUCTIONS = """
# 역할

당신은 AI 어시스턴트로서, 인간 상담원에게 통화를 연결하기 위한 대기 상태입니다.

# 사용 가능한 도구

- `voicemail_detected`: 음성사서함이 감지되면 사용
"""


class SingleRoomWarmTransferTask(KoreanSpeechGuardMixin, AgentTask[WarmTransferResult]):
    """
    단일 Room에서 Track Subscription 제어를 통한 Warm Transfer Task.

    흐름:
    1. 콜러에게 대기음악 재생 시작
    2. 상담원에게 SIP 전화 발신 (같은 Room)
    3. 상담원 연결 대기
    4. AI가 상담원에게 콜러 상황 브리핑
    5. 상담원 결정: DTMF 1번(수락) 또는 2번(콜백/거절)
    6. 연결 시: 콜러 ↔ 상담원 오디오 활성화, 대기음악 중지, AI 퇴장
    """

    def __init__(
        self,
        *,
        target_phone_number: str,
        sip_trunk_id: str,
        caller_identity: str,
        chat_ctx: llm.ChatContext | None = None,
        extra_instructions: str = "",
        customer_name: str | None = None,
        is_new_customer: bool = False,
        agent_connection_timeout: float = 45.0,
        dtmf_timeout: float = 30.0,
        transfer_record: TransferRecord | None = None,
        transfer_start_time: float | None = None,
        external_hold_music_player: BackgroundAudioPlayer | None = None,
        restore_caller_ai_audio_on_failure: bool = True,
        language_code: str | None = None,
        realtime_update_callback: (
            Callable[[str, Mapping[str, str]], Awaitable[None]] | None
        ) = None,
        queued_briefing_future: asyncio.Future[str | None] | None = None,
        briefing_enabled: bool = True,
    ) -> None:
        """
        Args:
            target_phone_number: 상담원 전화번호
            sip_trunk_id: SIP 트렁크 ID
            caller_identity: 콜러의 participant identity
            chat_ctx: 이전 대화 컨텍스트
            extra_instructions: 추가 지시사항
            customer_name: 고객 이름 (기존 고객인 경우)
            is_new_customer: 신규 고객 여부
            agent_connection_timeout: 상담원 연결 타임아웃 (초)
            dtmf_timeout: DTMF 입력 대기 타임아웃 (초)
            realtime_update_callback: 브리핑/상태를 외부 live-state 저장소에 반영하는 callback
            queued_briefing_future: 큐 등록 직후 시작한 공유 브리핑 생성 작업
            briefing_enabled: False이면 브리핑/DTMF 없이 상담원 응답 즉시 연결
        """
        self._briefing_enabled = briefing_enabled
        self._target_phone = target_phone_number
        self._sip_trunk_id = sip_trunk_id
        self._caller_identity = caller_identity
        self._extra_instructions = extra_instructions
        self._customer_name = customer_name
        self._is_new_customer = is_new_customer
        self._language_code = language_code
        self._realtime_update_callback = realtime_update_callback
        self._queued_briefing_future = queued_briefing_future
        self._agent_connection_timeout = agent_connection_timeout

        # 내부 상태
        self._agent_identity: str | None = None
        self._background_audio: BackgroundAudioPlayer | None = (
            external_hold_music_player
        )
        self._owns_background_audio: bool = external_hold_music_player is None
        self._hold_music_handle = None
        self._agent_connected_event = asyncio.Event()
        self._room: rtc.Room | None = None
        self._job_ctx = None
        self._participant_connected_handler = None
        self._agent_track_block_handler = None  # 상담원 트랙 자동 차단 핸들러
        self._restore_caller_ai_audio_on_failure = restore_caller_ai_audio_on_failure

        # 상담원 응답 여부 (create_sip_participant 성공 반환 후 True)
        self._agent_answered: bool = False

        # DTMF 상태
        self._dtmf_completed: bool = False
        self._caller_disconnected: bool = False
        self._briefing_complete: bool = False
        self._briefing_speech_handle: SpeechHandle | None = None
        self._dtmf_timeout_task: asyncio.Task | None = None
        self._dtmf_timeout_duration: float = dtmf_timeout
        self._invalid_dtmf_count: int = 0
        self._max_invalid_dtmf: int = 3
        self._background_tasks: set[asyncio.Task] = set()

        # 상담원 → 콜러 트랙 차단 작업 (no-briefing 연결 전 drain 대상)
        self._agent_block_tasks: set[asyncio.Task] = set()

        # No-briefing auto-connect 소유권 상태
        self._auto_connect_claimed: bool = False
        self._agent_left_during_auto_connect: bool = False
        self._agent_removal_requested: bool = False
        self._auto_connect_unmuted_caller_sids: list[str] = []

        # 브리핑 텍스트
        self._briefing_text: str | None = None
        self._transfer_record = transfer_record
        self._transfer_start_time = transfer_start_time

        # 브리핑 텍스트 생성용 대화 내역 저장 (no-briefing 모드에서는 불필요)
        self._conversation_history = (
            self._extract_conversation_history(chat_ctx) if briefing_enabled else ""
        )

        super().__init__(instructions=BASE_INSTRUCTIONS, chat_ctx=None)

    @staticmethod
    def _extract_conversation_history(chat_ctx: llm.ChatContext | None) -> str:
        """chat_ctx에서 대화 내역 문자열 추출"""
        return extract_warm_transfer_conversation_history(chat_ctx)

    def _sync_transfer_record(self, status: WarmTransferStatus) -> None:
        """공유 transfer record를 즉시 갱신해 shutdown race를 줄인다."""
        if not self._transfer_record:
            return

        self._transfer_record.transfer_status = status.value
        self._transfer_record.briefing_text = self._briefing_text

        if self._transfer_start_time is not None:
            self._transfer_record.total_wait_seconds = round(
                time.monotonic() - self._transfer_start_time, 1
            )

    def _complete_once(self, result: WarmTransferResult) -> bool:
        """중복 완료 race를 무시하고 pending 상태에서만 complete 한다."""
        fut = getattr(self, "_AgentTask__fut", None)
        if fut is not None and fut.done():
            logger.bind(
                event="WARM_TRANSFER_DUPLICATE_COMPLETION_IGNORED",
                status=result.status.value,
                agent_identity=result.agent_identity,
            ).warning("Ignoring duplicate warm-transfer completion")
            return False

        self.complete(result)
        return True

    async def _maybe_restore_caller_ai_audio(self) -> None:
        if not self._restore_caller_ai_audio_on_failure:
            return

        await self._restore_caller_ai_audio()

    async def _notify_realtime_update(
        self, event: str, fields: Mapping[str, str]
    ) -> None:
        """Best-effort dashboard update without coupling this task to Redis."""
        if self._realtime_update_callback is None:
            return

        try:
            await self._realtime_update_callback(event, fields)
        except Exception as exc:
            logger.opt(exception=exc).warning(
                "Failed to publish warm-transfer realtime update"
            )

    async def _prepare_briefing_text(self) -> str:
        """브리핑 텍스트를 준비하고 가능하면 transfer_record 캐시를 재사용한다."""
        queued_briefing_future = getattr(self, "_queued_briefing_future", None)
        if queued_briefing_future is not None:
            try:
                queued_briefing_text = await asyncio.shield(queued_briefing_future)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.opt(exception=exc).warning(
                    "Queued warm-transfer briefing generation failed; regenerating"
                )
            else:
                if queued_briefing_text:
                    self._briefing_text = queued_briefing_text
                    if self._transfer_record:
                        self._transfer_record.briefing_text = queued_briefing_text
                    return queued_briefing_text

        if self._transfer_record and self._transfer_record.briefing_text:
            self._briefing_text = self._transfer_record.briefing_text
            logger.bind(
                event="BRIEFING_TEXT_REUSED",
                briefing_text=self._briefing_text,
            ).debug("Reusing cached briefing text")
            await self._notify_realtime_update(
                "briefing_ready",
                {"briefing_text": self._briefing_text},
            )
            return self._briefing_text

        briefing_text = await generate_warm_transfer_briefing_text(
            conversation_history=self._conversation_history,
            customer_name=self._customer_name,
            is_new_customer=self._is_new_customer,
            language_code=self._language_code,
            extra_instructions=self._extra_instructions,
        )
        self._briefing_text = briefing_text

        if self._transfer_record:
            self._transfer_record.briefing_text = briefing_text

        await self._notify_realtime_update(
            "briefing_ready",
            {"briefing_text": briefing_text},
        )

        return briefing_text

    async def on_enter(self) -> None:
        """Task 시작 시 호출"""
        self._job_ctx = get_job_context()
        self._room = self._job_ctx.room

        # ! 오디오 입력 비활성화
        self.session.input.set_audio_enabled(False)

        # 콜러 이탈 감지 핸들러 등록 (가장 먼저 등록)
        self._room.on("participant_disconnected", self._on_participant_disconnected)

        logger.bind(
            event="WARM_TRANSFER_START",
            target_phone=self._target_phone,
            caller_identity=self._caller_identity,
            room_name=self._room.name,
            briefing_enabled=self._briefing_enabled,
        ).info("Starting single-room warm transfer")

        if not self._briefing_enabled:
            await self._on_enter_no_briefing()
            return

        briefing_task: asyncio.Task[str] | None = None
        try:
            # Step 0: 브리핑 텍스트 사전 생성 (LangChain, 상담원 연결 대기와 병렬 실행)
            briefing_task = asyncio.create_task(self._prepare_briefing_text())

            # Step 1: 콜러 격리, 오디오 mute, 대기음악 시작
            await self._isolate_caller()
            await self._mute_caller_audio()
            await self._start_hold_music()

            # Step 2: 상담원에게 전화 걸기
            await self._dial_agent()

            # Step 3: 상담원 연결 대기
            connected = await self._wait_for_agent_connection()
            if self._caller_disconnected or self._dtmf_completed:
                briefing_task.cancel()
                return
            if not connected:
                briefing_task.cancel()
                await self._handle_connection_failure()
                return

            await self._notify_realtime_update(
                "agent_answered",
                {
                    "status": "briefing",
                    "briefing_started_at": str(time.time()),
                },
            )

            # Step 3.5: 상담원 트랙 자동 차단 리스너 등록 + 기존 트랙 즉시 차단
            # (LiveKit auto-subscribe로 콜러가 상담원 음성을 잠깐 듣는 것 방지)
            self._start_blocking_agent_tracks_from_caller()
            await self._block_caller_from_agent_audio()

            # Step 4: 상담원 오디오 트랙 발행 대기 (브리핑이 처음부터 들리도록)
            audio_ready = await self._wait_for_agent_audio_ready()
            logger.bind(
                event="AGENT_AUDIO_READY",
                audio_ready=audio_ready,
                agent_identity=self._agent_identity,
            ).debug("Agent audio readiness checked")

            # Step 5: DTMF 리스너 등록 (상담원 연결 후 바로 등록)
            self._room.on("sip_dtmf_received", self._on_agent_dtmf_received)

            # Step 6: 브리핑 오디오 설정
            await self._setup_briefing_audio()

            # Step 7: 사전 생성된 브리핑 텍스트로 TTS 재생
            briefing_text = await briefing_task
            if self._caller_disconnected or self._dtmf_completed:
                return
            logger.bind(
                event="BRIEFING_TEXT_READY",
                briefing_text=briefing_text,
            ).debug("Playing pre-generated briefing text")

            speech_handle = self.session.say(
                briefing_text,
                # allow_interruptions=False,
                add_to_chat_ctx=False,
            )
            self._watch_briefing_playout(speech_handle)
            if self._caller_disconnected or self._dtmf_completed:
                return

            # SpeechHandle playout 완료 callback에서 브리핑 완료 상태로 전환하고
            # 그 시점부터 DTMF 입력 타임아웃을 시작한다. AgentTask.on_enter에서
            # wait_for_playout()을 await하면 tool 실행과 circular wait가 생길 수 있다.
            logger.debug("Briefing playout started; DTMF timeout is not armed yet")

        except Exception as e:
            # 실패한 전환의 브리핑 생성이 뒤늦게 realtime 상태를 갱신하지 않게 한다.
            if briefing_task is not None:
                briefing_task.cancel()
            # 콜러/상담원 이탈로 인한 에러는 이미 핸들러에서 처리됨
            if self._caller_disconnected or self._dtmf_completed:
                return
            logger.opt(exception=e).error("Error during warm transfer setup")
            await self._cleanup()
            await self._maybe_restore_caller_ai_audio()
            self._complete_once(
                WarmTransferResult(
                    status=WarmTransferStatus.ERROR,
                    error_message=str(e),
                    briefing_text=self._briefing_text,
                )
            )

    # ──────────────────────────────────────────────────────────────
    # No-briefing 모드: 상담원 응답 즉시 콜러 연결
    # ──────────────────────────────────────────────────────────────

    def _is_result_set(self) -> bool:
        fut = getattr(self, "_AgentTask__fut", None)
        return fut is not None and fut.done()

    async def _on_enter_no_briefing(self) -> None:
        """브리핑/DTMF 없이 상담원이 받으면 바로 콜러와 연결한다."""
        try:
            # Step 1: 콜러 격리, 오디오 mute, 대기음악 시작 (브리핑 생성 없음)
            await self._isolate_caller()
            await self._mute_caller_audio()
            await self._start_hold_music()

            # Step 2: 발신 전에 상담원 트랙 자동 차단 리스너 등록
            # (ringing / early media 중 발행되는 트랙도 콜러에게 들리지 않도록)
            self._agent_identity = f"human-agent-{self._target_phone}"
            self._start_blocking_agent_tracks_from_caller()

            # Step 3: 상담원 발신 및 연결 대기
            await self._dial_agent()
            connected = await self._wait_for_agent_connection()
            if self._caller_disconnected or self._dtmf_completed:
                return
            if not connected:
                await self._handle_connection_failure()
                return

            # Step 4: 완료 소유권 획득 (await 없이 동기적으로 두 플래그 설정)
            self._auto_connect_claimed = True
            self._dtmf_completed = True

            await self._execute_auto_connect()

        except Exception as e:
            if self._is_result_set():
                return
            if self._auto_connect_claimed:
                logger.opt(exception=e).error(
                    "Error during no-briefing warm transfer auto-connect"
                )
                await self._fail_auto_connect("setup_exception")
                return
            # 콜러/상담원 이탈로 인한 에러는 이미 핸들러에서 처리됨
            if self._caller_disconnected or self._dtmf_completed:
                return
            logger.opt(exception=e).error("Error during warm transfer setup")
            await self._cleanup()
            await self._maybe_restore_caller_ai_audio()
            self._complete_once(
                WarmTransferResult(
                    status=WarmTransferStatus.ERROR,
                    error_message=str(e),
                    briefing_text=self._briefing_text,
                )
            )

    def _caller_left(self) -> bool:
        if self._caller_disconnected:
            return True
        return self._caller_identity not in self._room.remote_participants

    def _agent_left(self) -> bool:
        if self._agent_left_during_auto_connect:
            return True
        if self._agent_removal_requested:
            return False
        return self._agent_identity not in self._room.remote_participants

    async def _finish_if_participant_left(self) -> bool:
        """Auto-connect 단계 경계에서 이탈 확인. 콜러 이탈 > 상담원 이탈 우선."""
        if self._caller_left():
            self._caller_disconnected = True
            await self._handle_caller_disconnected()
            return True
        if self._agent_left():
            self._agent_left_during_auto_connect = True
            await self._handle_agent_hangup()
            return True
        return False

    def _collect_audio_track_sids(self, identity: str | None) -> list[str]:
        participant = self._room.remote_participants.get(identity) if identity else None
        if not participant:
            return []
        return [
            pub.sid
            for pub in participant.track_publications.values()
            if pub.kind == rtc.TrackKind.KIND_AUDIO and pub.sid
        ]

    def _track_agent_block_task(self, coro) -> asyncio.Task:
        """상담원 트랙 차단 작업을 생성하고 drain 대상 집합에 등록한다."""
        task = self._create_background_task(coro)
        self._agent_block_tasks.add(task)
        task.add_done_callback(self._agent_block_tasks.discard)
        return task

    async def _drain_agent_block_tasks(self) -> bool:
        """진행 중인 차단 작업 완료 대기. 타임아웃 시 취소 후 False."""
        pending = {task for task in self._agent_block_tasks if not task.done()}
        if not pending:
            return True

        _, still_pending = await asyncio.wait(
            pending, timeout=AUTO_CONNECT_BLOCK_DRAIN_TIMEOUT_SECONDS
        )
        if still_pending:
            for task in still_pending:
                task.cancel()
            return False
        return True

    async def _unmute_caller_audio_strict(self, caller_audio_sids: list[str]) -> None:
        """콜러 오디오 unmute (실패/타임아웃 시 예외 전파)."""
        for track_sid in caller_audio_sids:
            await asyncio.wait_for(
                self._job_ctx.api.room.mute_published_track(
                    api.MuteRoomTrackRequest(
                        room=self._room.name,
                        identity=self._caller_identity,
                        track_sid=track_sid,
                        muted=False,
                    )
                ),
                timeout=AUTO_CONNECT_API_TIMEOUT_SECONDS,
            )
            self._auto_connect_unmuted_caller_sids.append(track_sid)

    async def _enable_caller_agent_audio_strict(
        self, caller_audio_sids: list[str], agent_audio_sids: list[str]
    ) -> None:
        """콜러 ↔ 상담원 구독 활성화 (실패/타임아웃 시 예외 전파)."""
        # 콜러가 상담원 음성을 듣도록
        await asyncio.wait_for(
            self._job_ctx.api.room.update_subscriptions(
                api.UpdateSubscriptionsRequest(
                    room=self._room.name,
                    identity=self._caller_identity,
                    track_sids=agent_audio_sids,
                    subscribe=True,
                )
            ),
            timeout=AUTO_CONNECT_API_TIMEOUT_SECONDS,
        )
        # 상담원이 콜러 음성을 듣도록
        await asyncio.wait_for(
            self._job_ctx.api.room.update_subscriptions(
                api.UpdateSubscriptionsRequest(
                    room=self._room.name,
                    identity=self._agent_identity,
                    track_sids=caller_audio_sids,
                    subscribe=True,
                )
            ),
            timeout=AUTO_CONNECT_API_TIMEOUT_SECONDS,
        )

    async def _execute_auto_connect(self) -> None:
        """소유권 획득 후 유일한 완료 경로: 브리핑 없이 콜러 ↔ 상담원 연결."""
        try:
            # Step 0: 이미 발행된 상담원 트랙 차단 (drain 대상으로 추적)
            self._track_agent_block_task(self._block_caller_from_agent_audio())
            if await self._finish_if_participant_left():
                return

            # Step 1: 상담원 오디오 트랙 발행 대기
            audio_ready = await self._wait_for_agent_audio_ready()
            if await self._finish_if_participant_left():
                return
            if not audio_ready:
                await self._fail_auto_connect("agent_audio_not_ready")
                return

            # Step 2: 차단 리스너 해제 후 진행 중인 차단 작업 drain
            self._stop_blocking_agent_tracks_from_caller()
            drained = await self._drain_agent_block_tasks()
            if await self._finish_if_participant_left():
                return
            if not drained:
                await self._fail_auto_connect("block_drain_timeout")
                return

            # Step 3: 현재 오디오 트랙 재수집
            caller_audio_sids = self._collect_audio_track_sids(self._caller_identity)
            agent_audio_sids = self._collect_audio_track_sids(self._agent_identity)
            if not caller_audio_sids or not agent_audio_sids:
                await self._fail_auto_connect("audio_track_missing")
                return

            # Step 4: strict unmute
            try:
                await self._unmute_caller_audio_strict(caller_audio_sids)
            except Exception as e:
                logger.opt(exception=e).warning("Strict caller unmute failed")
                await self._fail_auto_connect("unmute_failed")
                return
            if await self._finish_if_participant_left():
                return

            # Step 5: strict subscribe
            try:
                await self._enable_caller_agent_audio_strict(
                    caller_audio_sids, agent_audio_sids
                )
            except Exception as e:
                logger.opt(exception=e).warning("Strict caller-agent subscribe failed")
                await self._fail_auto_connect("subscription_failed")
                return
            if await self._finish_if_participant_left():
                return
        except Exception as e:
            logger.opt(exception=e).error("Unexpected error during auto-connect")
            await self._fail_auto_connect("auto_connect_exception")
            return

        # Step 6: AI 트랙 권한 해제 + AI 구독 해제 (실패해도 연결은 유지)
        try:
            self._release_ai_from_bridged_audio()
        except Exception as e:
            logger.opt(exception=e).warning(
                "Failed to release AI audio after auto-connect bridge"
            )

        # Step 7: 대기음악 정리 (외부 player는 건드리지 않음)
        try:
            await self._cleanup()
        except Exception as e:
            logger.opt(exception=e).warning("Cleanup failed after auto-connect")

        # Step 8: 최종 이탈 확인 후 SUCCESS
        try:
            if await self._finish_if_participant_left():
                return
        except Exception as e:
            logger.opt(exception=e).warning("Final presence check failed")
            if self._is_result_set():
                return

        logger.bind(
            event="WARM_TRANSFER_AUTO_CONNECT",
            agent_identity=self._agent_identity,
            caller_identity=self._caller_identity,
        ).info("Caller auto-connected to agent without briefing")
        self._complete_once(
            WarmTransferResult(
                status=WarmTransferStatus.SUCCESS,
                agent_identity=self._agent_identity,
                briefing_text=self._briefing_text,
            )
        )

    async def _fail_auto_connect(self, reason: str) -> None:
        """Auto-connect 실패 처리 (ERROR → 외부 루프 재시도)."""
        if not self._auto_connect_claimed or self._is_result_set():
            return

        logger.bind(
            event="WARM_TRANSFER_AUTO_CONNECT_FAILED",
            reason=reason,
            agent_identity=self._agent_identity,
        ).warning("No-briefing auto-connect failed")

        if self._caller_left():
            self._caller_disconnected = True
            await self._handle_caller_disconnected()
            return
        if self._agent_left():
            self._agent_left_during_auto_connect = True
            await self._handle_agent_hangup()
            return

        # 의도적 제거: 이후 상담원 이탈 이벤트는 자발적 끊음으로 기록하지 않음
        self._agent_removal_requested = True
        agent_removed = await self._remove_agent_after_auto_connect_failure()
        if not agent_removed:
            logger.bind(
                event="WARM_TRANSFER_AUTO_CONNECT_AGENT_CLEANUP_FAILED",
                reason=reason,
                agent_identity=self._agent_identity,
            ).error("Agent leg could not be removed; call will be terminated")
            await self._unsubscribe_caller_from_agent_audio()

        if self._auto_connect_unmuted_caller_sids:
            try:
                await self._mute_caller_audio()
            except Exception as e:
                logger.opt(exception=e).warning(
                    "Failed to re-mute caller after auto-connect failure"
                )
            self._auto_connect_unmuted_caller_sids = []

        try:
            await self._cleanup()
        except Exception as e:
            logger.opt(exception=e).warning("Cleanup failed after auto-connect failure")
        await self._maybe_restore_caller_ai_audio()

        # 최종 재확인: 위 await 동안 콜러가 이탈했으면 CALLER_DISCONNECTED.
        # 상담원 제거가 확인되지 않았으면 terminate_call 을 유지해 방을 닫게 한다.
        if self._caller_left():
            self._caller_disconnected = True
            self._sync_transfer_record(WarmTransferStatus.CALLER_DISCONNECTED)
            self._complete_once(
                WarmTransferResult(
                    status=WarmTransferStatus.CALLER_DISCONNECTED,
                    agent_identity=self._agent_identity,
                    error_message="콜러가 통화를 종료했습니다.",
                    briefing_text=self._briefing_text,
                    terminate_call=not agent_removed,
                )
            )
            return

        if not agent_removed:
            self._complete_once(
                WarmTransferResult(
                    status=WarmTransferStatus.ERROR,
                    agent_identity=self._agent_identity,
                    error_message="agent_cleanup_failed",
                    briefing_text=self._briefing_text,
                    terminate_call=True,
                )
            )
            return

        self._complete_once(
            WarmTransferResult(
                status=WarmTransferStatus.ERROR,
                agent_identity=self._agent_identity,
                error_message=reason,
                briefing_text=self._briefing_text,
            )
        )

    async def _remove_agent_after_auto_connect_failure(self) -> bool:
        """상담원 제거 (최대 N회). 제거가 확인되면 True."""
        if not self._agent_identity:
            return True

        for attempt in range(1, AUTO_CONNECT_AGENT_REMOVAL_ATTEMPTS + 1):
            try:
                await asyncio.wait_for(
                    self._job_ctx.api.room.remove_participant(
                        api.RoomParticipantIdentity(
                            room=self._room.name,
                            identity=self._agent_identity,
                        )
                    ),
                    timeout=AUTO_CONNECT_API_TIMEOUT_SECONDS,
                )
                return True
            except Exception as e:
                logger.opt(exception=e).bind(
                    event="WARM_TRANSFER_AUTO_CONNECT_AGENT_REMOVE_FAILED",
                    attempt=attempt,
                    agent_identity=self._agent_identity,
                ).warning("Failed to remove agent after auto-connect failure")
                if self._agent_identity not in self._room.remote_participants:
                    return True
        return False

    async def _unsubscribe_caller_from_agent_audio(self) -> None:
        """Best-effort: 남아 있는 상담원 오디오를 콜러가 듣지 않도록 구독 해제."""
        agent_audio_sids = self._collect_audio_track_sids(self._agent_identity)
        if not agent_audio_sids:
            return
        try:
            await asyncio.wait_for(
                self._job_ctx.api.room.update_subscriptions(
                    api.UpdateSubscriptionsRequest(
                        room=self._room.name,
                        identity=self._caller_identity,
                        track_sids=agent_audio_sids,
                        subscribe=False,
                    )
                ),
                timeout=AUTO_CONNECT_API_TIMEOUT_SECONDS,
            )
        except Exception as e:
            logger.opt(exception=e).warning(
                "Failed to unsubscribe caller from stale agent audio"
            )

    async def on_exit(self) -> None:
        """Task 종료 시 호출"""
        # 이벤트 핸들러 제거
        if self._room and self._participant_connected_handler:
            self._room.off("participant_connected", self._participant_connected_handler)

        # 콜러 이탈 리스너 제거
        if self._room:
            self._room.off(
                "participant_disconnected", self._on_participant_disconnected
            )

        # DTMF 리스너 제거
        if self._room:
            self._room.off("sip_dtmf_received", self._on_agent_dtmf_received)

        # 상담원 트랙 자동 차단 리스너 제거
        self._stop_blocking_agent_tracks_from_caller()

        # DTMF 타임아웃 취소
        self._cancel_dtmf_timeout()

        # 브리핑 playout callback 제거
        self._stop_watching_briefing_playout()

        await self._cleanup()

    async def _isolate_caller(self) -> None:
        """콜러를 완전히 격리

        - 콜러: AI 트랙 구독 불가 (브리핑 내용 안 들림)
        - AI: set_audio_enabled(False)로 콜러 음성 처리 중지 (트랙 구독은 유지)
        - 상담원: AI 트랙 구독 가능 (브리핑 내용 들림)

        Note: 콜러 트랙 구독을 직접 해제(pub.set_subscribed(False))하면
        room_io 오디오 파이프라인이 끊어져 복원 시 재연결이 안 되므로,
        on_enter()의 set_audio_enabled(False)로 세션 레벨에서 처리를 중지합니다.
        """
        # AI의 트랙을 아무도 구독하지 못하도록 설정 (브리핑 시작 전)
        self._room.local_participant.set_track_subscription_permissions(
            allow_all_participants=False,
            participant_permissions=[],  # 아무도 구독 불가
        )

        logger.debug(
            f"Isolated caller {self._caller_identity}: AI audio input disabled"
        )

    async def _mute_caller_audio(self) -> None:
        """콜러의 오디오 발행을 mute (브리핑 중 상담원에게 안 들리도록)"""
        caller = self._room.remote_participants.get(self._caller_identity)
        if not caller:
            logger.warning("Caller not found, cannot mute caller audio")
            return

        for pub in caller.track_publications.values():
            if pub.kind == rtc.TrackKind.KIND_AUDIO and pub.sid:
                try:
                    await self._job_ctx.api.room.mute_published_track(
                        api.MuteRoomTrackRequest(
                            room=self._room.name,
                            identity=self._caller_identity,
                            track_sid=pub.sid,
                            muted=True,
                        )
                    )
                    logger.debug(f"Muted caller audio track: {pub.sid}")
                except Exception as e:
                    logger.opt(exception=e).warning(
                        f"Failed to mute caller audio track: {pub.sid}"
                    )

    async def _unmute_caller_audio(self) -> None:
        """콜러의 오디오 발행을 unmute (연결 시)"""
        caller = self._room.remote_participants.get(self._caller_identity)
        if not caller:
            logger.warning("Caller not found, cannot unmute caller audio")
            return

        for pub in caller.track_publications.values():
            if pub.kind == rtc.TrackKind.KIND_AUDIO and pub.sid:
                try:
                    await self._job_ctx.api.room.mute_published_track(
                        api.MuteRoomTrackRequest(
                            room=self._room.name,
                            identity=self._caller_identity,
                            track_sid=pub.sid,
                            muted=False,
                        )
                    )
                    logger.debug(f"Unmuted caller audio track: {pub.sid}")
                except Exception as e:
                    logger.opt(exception=e).warning(
                        f"Failed to unmute caller audio track: {pub.sid}"
                    )

    async def _start_hold_music(self) -> None:
        """콜러에게 대기음악 재생 (외부 player 재사용 가능)."""
        if not self._owns_background_audio:
            # 외부 owner가 이미 시작/재생 중. 콜러 구독 권한만 다시 보장.
            if self._background_audio and self._background_audio.publication:
                hold_music_sid = self._background_audio.publication.sid
                self._room.local_participant.set_track_subscription_permissions(
                    allow_all_participants=False,
                    participant_permissions=[
                        ParticipantTrackPermission(
                            participant_identity=self._caller_identity,
                            allow_all=False,
                            allowed_track_sids=[hold_music_sid],
                        ),
                    ],
                )
                logger.debug(
                    f"Reusing external hold music player (SID: {hold_music_sid})"
                )
            return

        self._background_audio = BackgroundAudioPlayer()
        await self._background_audio.start(room=self._room)
        self._hold_music_handle = self._background_audio.play(
            AudioConfig(BuiltinAudioClip.HOLD_MUSIC, volume=0.8),
            loop=True,
        )

        if self._background_audio.publication:
            hold_music_sid = self._background_audio.publication.sid
            self._room.local_participant.set_track_subscription_permissions(
                allow_all_participants=False,
                participant_permissions=[
                    ParticipantTrackPermission(
                        participant_identity=self._caller_identity,
                        allow_all=False,
                        allowed_track_sids=[hold_music_sid],
                    ),
                ],
            )
            logger.debug(
                f"Hold music started, caller {self._caller_identity} can hear hold music (SID: {hold_music_sid})"
            )
        else:
            logger.warning("Hold music publication not available")

    async def _dial_agent(self) -> None:
        """상담원에게 SIP 전화 발신 (같은 Room에 참여)"""
        self._agent_identity = f"human-agent-{self._target_phone}"

        # 이벤트 핸들러 등록
        def on_participant_connected(participant: rtc.RemoteParticipant):
            if participant.identity == self._agent_identity:
                self._agent_connected_event.set()
                logger.debug(f"Agent connected: {participant.identity}")

        self._participant_connected_handler = on_participant_connected
        self._room.on("participant_connected", on_participant_connected)

        # SIP 참여자 생성 — 시뮬레이션 게이트웨이를 쓰며 실제 전화는 걸지 않는다.
        await fake_transfer_gateway.create_sip_participant(
            self._job_ctx,
            api.CreateSIPParticipantRequest(
                room_name=self._room.name,
                sip_trunk_id=self._sip_trunk_id,
                sip_call_to=self._target_phone,
                participant_identity=self._agent_identity,
                participant_name="Human Agent",
                participant_metadata=json.dumps(
                    {"role": "human-agent", "transfer": True}
                ),
                wait_until_answered=True,
                play_dialtone=True,
                ringing_timeout=timedelta(seconds=self._agent_connection_timeout),
            )
        )
        # wait_until_answered=True이므로 여기 도달 = 상담원이 실제로 전화를 받음
        self._agent_answered = True
        logger.debug(f"SIP participant answered for {self._target_phone}")

    async def _wait_for_agent_connection(self) -> bool:
        """상담원 연결 대기"""
        started_at = time.monotonic()
        try:
            await asyncio.wait_for(
                self._agent_connected_event.wait(),
                timeout=self._agent_connection_timeout + 5,  # SIP 타임아웃 + 버퍼
            )
            waited_seconds = round(time.monotonic() - started_at, 1)
            logger.bind(
                event="WARM_TRANSFER_AGENT_CONNECTED",
                agent_identity=self._agent_identity,
                waited_seconds=waited_seconds,
            ).info("Agent connected to room")
            return True
        except asyncio.TimeoutError:
            waited_seconds = round(time.monotonic() - started_at, 1)
            logger.bind(
                event="WARM_TRANSFER_AGENT_CONNECTION_TIMEOUT",
                agent_identity=self._agent_identity,
                waited_seconds=waited_seconds,
            ).warning("Agent connection timeout")
            return False

    async def _wait_for_agent_audio_ready(self, timeout: float = 10.0) -> bool:
        """상담원의 오디오 트랙이 실제로 발행될 때까지 대기

        participant_connected 이벤트는 SDP negotiation 완료 시점에 발생하지만,
        상담원의 실제 오디오 트랙 발행은 그 이후일 수 있음.
        브리핑이 처음부터 들리도록 오디오 트랙 발행을 대기함.
        """
        audio_ready_event = asyncio.Event()

        def on_track_published(
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ):
            if (
                participant.identity == self._agent_identity
                and publication.kind == rtc.TrackKind.KIND_AUDIO
            ):
                logger.bind(
                    event="AGENT_AUDIO_TRACK_PUBLISHED",
                    track_sid=publication.sid,
                    agent_identity=self._agent_identity,
                ).debug("Agent audio track published")
                audio_ready_event.set()

        # 이미 오디오 트랙이 있는지 확인
        agent = self._room.remote_participants.get(self._agent_identity)
        if agent:
            for pub in agent.track_publications.values():
                if pub.kind == rtc.TrackKind.KIND_AUDIO:
                    logger.debug("Agent audio track already exists")
                    return True

        # 이벤트 핸들러 등록
        self._room.on("track_published", on_track_published)

        try:
            await asyncio.wait_for(audio_ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                f"Timeout ({timeout}s) waiting for agent audio track, proceeding anyway"
            )
            return False
        finally:
            self._room.off("track_published", on_track_published)

    async def _handle_connection_failure(self) -> None:
        """상담원 연결 실패 처리"""
        logger.bind(
            event="WARM_TRANSFER_TIMEOUT",
            target_phone=self._target_phone,
        ).warning("Agent did not answer")

        await self._cleanup()
        await self._maybe_restore_caller_ai_audio()
        self._complete_once(
            WarmTransferResult(
                status=WarmTransferStatus.TIMEOUT,
                error_message="상담원이 응답하지 않습니다.",
                briefing_text=self._briefing_text,
            )
        )

    async def _setup_briefing_audio(self) -> None:
        """AI ↔ 상담원 브리핑을 위한 오디오 설정

        - 상담원만 AI 음성 트랙 구독 가능 (대기음악 트랙 제외)
        - AI는 상담원 트랙 구독
        - 콜러는 여전히 AI 트랙 구독 불가 + 대기음악 계속 재생
        - 콜러는 상담원 음성도 듣지 못함 (server-side subscription 제어)

        Note: 대기음악은 connect_to_caller() 호출 시 중지됨
        """
        # AI의 음성 트랙 SID 수집 (대기음악 트랙 제외)
        background_audio_sid = None
        if self._background_audio and self._background_audio.publication:
            background_audio_sid = self._background_audio.publication.sid

        allowed_track_sids = []
        for pub in self._room.local_participant.track_publications.values():
            # 대기음악 트랙은 제외
            if background_audio_sid and pub.sid == background_audio_sid:
                continue
            allowed_track_sids.append(pub.sid)

        logger.debug(
            f"Allowed track SIDs for agent: {allowed_track_sids}, "
            f"background audio SID (excluded): {background_audio_sid}"
        )

        # 상담원: AI 음성만 (대기음악 제외)
        # 콜러: 대기음악만 (AI 음성 제외)
        participant_permissions = [
            ParticipantTrackPermission(
                participant_identity=self._agent_identity,
                allow_all=False,
                allowed_track_sids=allowed_track_sids,  # AI 음성만, 대기음악 제외
            ),
        ]

        # 콜러에게는 대기음악만 계속 들리도록 설정
        if background_audio_sid:
            participant_permissions.append(
                ParticipantTrackPermission(
                    participant_identity=self._caller_identity,
                    allow_all=False,
                    allowed_track_sids=[background_audio_sid],  # 대기음악만
                ),
            )

        self._room.local_participant.set_track_subscription_permissions(
            allow_all_participants=False,
            participant_permissions=participant_permissions,
        )

        # AI가 상담원 트랙 구독
        agent = self._room.remote_participants.get(self._agent_identity)
        if agent:
            for pub in agent.track_publications.values():
                if pub.kind == rtc.TrackKind.KIND_AUDIO:
                    pub.set_subscribed(True)

        # 콜러가 상담원의 음성을 듣지 못하도록 서버 측에서 구독 해제
        # (set_track_subscription_permissions는 AI 트랙만 제어하므로,
        #  상담원 → 콜러 오디오는 별도로 서버 API로 제어해야 함)
        await self._block_caller_from_agent_audio()

        logger.debug(
            f"Briefing audio setup: agent {self._agent_identity} can hear AI voice only, "
            f"caller {self._caller_identity} cannot hear agent"
        )

    def _start_blocking_agent_tracks_from_caller(self) -> None:
        """상담원 트랙이 발행될 때마다 콜러의 구독을 자동 차단하는 리스너 등록

        LiveKit auto-subscribe로 인해 상담원이 트랙을 발행하면
        콜러가 자동으로 구독하게 되므로, 트랙 발행 이벤트마다 즉시 차단합니다.
        """

        def on_agent_track_published(
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ):
            if (
                participant.identity == self._agent_identity
                and publication.kind == rtc.TrackKind.KIND_AUDIO
            ):
                logger.debug(
                    f"Auto-blocking new agent audio track from caller: {publication.sid}"
                )
                self._track_agent_block_task(
                    self._block_track_from_caller(publication.sid)
                )

        self._agent_track_block_handler = on_agent_track_published
        self._room.on("track_published", on_agent_track_published)

    def _stop_blocking_agent_tracks_from_caller(self) -> None:
        """상담원 트랙 자동 차단 리스너 해제"""
        if self._agent_track_block_handler:
            self._room.off("track_published", self._agent_track_block_handler)
            self._agent_track_block_handler = None

    async def _block_track_from_caller(self, track_sid: str) -> None:
        """특정 트랙을 콜러가 듣지 못하도록 서버 측에서 구독 해제"""
        try:
            await self._job_ctx.api.room.update_subscriptions(
                api.UpdateSubscriptionsRequest(
                    room=self._room.name,
                    identity=self._caller_identity,
                    track_sids=[track_sid],
                    subscribe=False,
                )
            )
            logger.debug(
                f"Blocked caller {self._caller_identity} from track: {track_sid}"
            )
        except Exception as e:
            logger.opt(exception=e).warning(
                f"Failed to block caller from track: {track_sid}"
            )

    async def _block_caller_from_agent_audio(self) -> None:
        """콜러가 상담원 음성을 듣지 못하도록 서버 측에서 구독 해제"""
        agent = self._room.remote_participants.get(self._agent_identity)
        if not agent:
            logger.warning("Agent not found, cannot block caller from agent audio")
            return

        # 상담원의 오디오 트랙 SID 수집
        agent_audio_sids = []
        for pub in agent.track_publications.values():
            if pub.kind == rtc.TrackKind.KIND_AUDIO:
                agent_audio_sids.append(pub.sid)

        if not agent_audio_sids:
            logger.debug(
                "No agent audio tracks found yet, will be blocked when published"
            )
            return

        # 서버 API로 콜러의 상담원 트랙 구독 해제
        try:
            await self._job_ctx.api.room.update_subscriptions(
                api.UpdateSubscriptionsRequest(
                    room=self._room.name,
                    identity=self._caller_identity,
                    track_sids=agent_audio_sids,
                    subscribe=False,
                )
            )
            logger.debug(
                f"Blocked caller {self._caller_identity} from hearing agent tracks: {agent_audio_sids}"
            )
        except Exception as e:
            logger.opt(exception=e).warning("Failed to block caller from agent audio")

    async def _connect_caller_to_agent(self) -> None:
        """콜러와 상담원 간 직접 오디오 연결

        순서가 중요 (찌지직 방지):
        1. 먼저 콜러↔상담원 오디오 구독 활성화 (대기음악은 아직 유지)
        2. 상담원 트랙 자동 차단 해제
        3. 그 후 AI 트랙 구독 권한 해제 (대기음악 포함)
        이렇게 하면 콜러는 "대기음악 + 상담원 음성" → "상담원 음성만"으로
        자연스럽게 전환되어 오디오 파형 절단이 발생하지 않음
        """
        # Step 1: 콜러↔상담원 오디오 구독 먼저 활성화 (대기음악은 아직 재생 중)
        await self._enable_caller_agent_audio()

        # Step 2: 상담원 트랙 자동 차단 중지 (이제 콜러가 상담원 음성을 들어야 함)
        self._stop_blocking_agent_tracks_from_caller()

        # Step 3-4: AI 트랙 권한 해제 + AI의 콜러/상담원 구독 해제
        self._release_ai_from_bridged_audio()

        logger.debug("Caller and agent audio connected, AI unsubscribed")

    def _release_ai_from_bridged_audio(self) -> None:
        """콜러 ↔ 상담원 연결 후 AI를 오디오 경로에서 제외한다."""
        # AI 트랙 구독 권한 해제 (대기음악 포함 - 이미 상담원 음성이 연결됨)
        self._room.local_participant.set_track_subscription_permissions(
            allow_all_participants=False,
            participant_permissions=[],
        )

        # AI가 콜러/상담원 트랙 구독 해제 (AI는 더 이상 듣지 않음)
        caller = self._room.remote_participants.get(self._caller_identity)
        agent = self._room.remote_participants.get(self._agent_identity)

        if caller:
            for pub in caller.track_publications.values():
                if pub.kind == rtc.TrackKind.KIND_AUDIO:
                    pub.set_subscribed(False)

        if agent:
            for pub in agent.track_publications.values():
                if pub.kind == rtc.TrackKind.KIND_AUDIO:
                    pub.set_subscribed(False)

    async def _enable_caller_agent_audio(self) -> None:
        """콜러 ↔ 상담원 간 오디오 연결 활성화 (서버 측)"""
        caller = self._room.remote_participants.get(self._caller_identity)
        agent = self._room.remote_participants.get(self._agent_identity)

        # 콜러의 오디오 트랙 SID 수집
        caller_audio_sids = []
        if caller:
            for pub in caller.track_publications.values():
                if pub.kind == rtc.TrackKind.KIND_AUDIO:
                    caller_audio_sids.append(pub.sid)

        # 상담원의 오디오 트랙 SID 수집
        agent_audio_sids = []
        if agent:
            for pub in agent.track_publications.values():
                if pub.kind == rtc.TrackKind.KIND_AUDIO:
                    agent_audio_sids.append(pub.sid)

        try:
            # 콜러가 상담원 음성을 들을 수 있도록 구독 활성화
            if agent_audio_sids:
                await self._job_ctx.api.room.update_subscriptions(
                    api.UpdateSubscriptionsRequest(
                        room=self._room.name,
                        identity=self._caller_identity,
                        track_sids=agent_audio_sids,
                        subscribe=True,
                    )
                )
                logger.debug(
                    f"Enabled caller {self._caller_identity} to hear agent tracks: {agent_audio_sids}"
                )

            # 상담원이 콜러 음성을 들을 수 있도록 구독 활성화
            if caller_audio_sids:
                await self._job_ctx.api.room.update_subscriptions(
                    api.UpdateSubscriptionsRequest(
                        room=self._room.name,
                        identity=self._agent_identity,
                        track_sids=caller_audio_sids,
                        subscribe=True,
                    )
                )
                logger.debug(
                    f"Enabled agent {self._agent_identity} to hear caller tracks: {caller_audio_sids}"
                )

        except Exception as e:
            logger.opt(exception=e).warning(
                "Failed to enable caller-agent audio connection"
            )

    async def _cleanup(self) -> None:
        """리소스 정리 (외부 player는 건드리지 않음)."""
        if self._hold_music_handle:
            self._hold_music_handle.stop()
            self._hold_music_handle = None

        if self._background_audio and self._owns_background_audio:
            await self._background_audio.aclose()
            self._background_audio = None

    async def _restore_caller_ai_audio(self) -> None:
        """콜러-AI 간 오디오 연결 복원 (transfer 실패 시)

        - 콜러가 AI 음성을 다시 들을 수 있도록 track subscription 복원
        - 콜러 오디오 unmute (AI가 콜러 음성을 다시 들을 수 있도록)
        - 세션 오디오 입력 다시 활성화

        Note: 콜러 트랙 구독은 _isolate_caller()에서 해제하지 않으므로
        (room_io 파이프라인 보존), 여기서 재구독할 필요 없음.
        """
        try:
            # 1. 콜러가 AI 음성을 들을 수 있도록 track subscription permissions 복원
            self._room.local_participant.set_track_subscription_permissions(
                allow_all_participants=True,
            )
            logger.debug("Restored track subscription permissions for caller")

            # 2. 콜러 오디오 unmute
            await self._unmute_caller_audio()

            # 3. 세션 오디오 입력 다시 활성화 (on_enter에서 비활성화했던 것 복원)
            self.session.input.set_audio_enabled(True)
            logger.debug("Re-enabled session audio input")

        except Exception as e:
            logger.opt(exception=e).warning(
                "Failed to restore caller-AI audio connection"
            )

    # ──────────────────────────────────────────────────────────────
    # 참여자 이탈 감지 (콜러 / 상담원)
    # ──────────────────────────────────────────────────────────────

    def _on_participant_disconnected(self, participant: rtc.RemoteParticipant) -> None:
        """참여자 이탈 감지 - 콜러 또는 상담원이 끊으면 즉시 정리"""
        # No-briefing auto-connect 소유 중: 기록만 하고 완료는 auto-connect가 담당
        if getattr(self, "_auto_connect_claimed", False) and not self._is_result_set():
            if participant.identity == self._caller_identity:
                self._caller_disconnected = True
                self._sync_transfer_record(WarmTransferStatus.CALLER_DISCONNECTED)
                logger.bind(
                    event="WARM_TRANSFER_CALLER_DISCONNECTED",
                    caller_identity=self._caller_identity,
                    agent_identity=self._agent_identity,
                ).info("Caller disconnected during warm transfer auto-connect")
            elif (
                participant.identity == self._agent_identity
                and not self._agent_removal_requested
            ):
                self._agent_left_during_auto_connect = True
                logger.bind(
                    event="WARM_TRANSFER_AGENT_HANGUP",
                    caller_identity=self._caller_identity,
                    agent_identity=self._agent_identity,
                ).info("Agent hung up during warm transfer auto-connect")
            return

        if self._caller_disconnected or self._dtmf_completed:
            return

        # 콜러 이탈 → 상담원 전화 끊기
        if participant.identity == self._caller_identity:
            self._caller_disconnected = True
            self._dtmf_completed = True
            self._cancel_dtmf_timeout()
            self._sync_transfer_record(WarmTransferStatus.CALLER_DISCONNECTED)

            logger.bind(
                event="WARM_TRANSFER_CALLER_DISCONNECTED",
                caller_identity=self._caller_identity,
                agent_identity=self._agent_identity,
            ).info("Caller disconnected during warm transfer")

            self._run_completion_task(self._handle_caller_disconnected())

        # 상담원 이탈
        elif participant.identity == self._agent_identity:
            self._dtmf_completed = True
            self._cancel_dtmf_timeout()

            # 상담원이 실제로 전화를 받았는지 확인
            # (participant_connected는 ringing 시작 시 발생하므로 신뢰할 수 없음,
            #  create_sip_participant(wait_until_answered=True) 성공 반환 여부로 판단)
            agent_answered = self._agent_answered

            if agent_answered:
                # 전화를 받은 후 끊음 → 거절(DECLINED) 처리
                logger.bind(
                    event="WARM_TRANSFER_AGENT_HANGUP",
                    caller_identity=self._caller_identity,
                    agent_identity=self._agent_identity,
                ).info("Agent hung up during warm transfer (treating as declined)")
                self._run_completion_task(self._handle_agent_hangup())
            else:
                # 전화를 받지 않음 → 타임아웃(TIMEOUT) 처리
                logger.bind(
                    event="WARM_TRANSFER_AGENT_NO_ANSWER",
                    caller_identity=self._caller_identity,
                    agent_identity=self._agent_identity,
                ).info(
                    "Agent did not answer (SIP participant removed after ringing timeout)"
                )
                self._run_completion_task(self._handle_agent_no_answer())

    async def _handle_caller_disconnected(self) -> None:
        """콜러 이탈 시 상담원 연결 해제 및 리소스 정리"""
        self._sync_transfer_record(WarmTransferStatus.CALLER_DISCONNECTED)

        # 상담원이 연결되어 있으면 Room에서 제거 (전화 끊기)
        if self._agent_identity:
            try:
                await self._job_ctx.api.room.remove_participant(
                    api.RoomParticipantIdentity(
                        room=self._room.name,
                        identity=self._agent_identity,
                    )
                )
                logger.debug(
                    f"Disconnected agent {self._agent_identity} due to caller disconnect"
                )
            except Exception as e:
                logger.opt(exception=e).warning(
                    "Failed to disconnect agent after caller left"
                )

        await self._cleanup()

        self._complete_once(
            WarmTransferResult(
                status=WarmTransferStatus.CALLER_DISCONNECTED,
                agent_identity=self._agent_identity,
                error_message="콜러가 통화를 종료했습니다.",
                briefing_text=self._briefing_text,
            )
        )

    async def _handle_agent_hangup(self) -> None:
        """상담원이 DTMF 입력 없이 전화를 끊은 경우 → 거절 처리"""
        logger.bind(
            event="WARM_TRANSFER_DECLINED",
            agent_identity=self._agent_identity,
            reason="상담원이 전화를 끊음",
        ).info("Agent hung up without DTMF response")

        # 진행 중인 TTS 즉시 중단 (콜러에게 브리핑이 들리지 않도록)
        self.session.interrupt()

        # 상담원은 이미 Room에서 나갔으므로 remove_participant 불필요
        await self._cleanup()
        await self._maybe_restore_caller_ai_audio()

        self._complete_once(
            WarmTransferResult(
                status=WarmTransferStatus.DECLINED,
                agent_identity=self._agent_identity,
                error_message="상담원이 전화를 끊었습니다.",
                briefing_text=self._briefing_text,
            )
        )

    async def _handle_agent_no_answer(self) -> None:
        """상담원이 전화를 받지 않은 경우 → 타임아웃 처리"""
        logger.bind(
            event="WARM_TRANSFER_TIMEOUT",
            agent_identity=self._agent_identity,
            reason="상담원 미응답",
        ).info("Agent did not answer the call")

        await self._cleanup()
        await self._maybe_restore_caller_ai_audio()

        self._complete_once(
            WarmTransferResult(
                status=WarmTransferStatus.TIMEOUT,
                agent_identity=self._agent_identity,
                error_message="상담원이 전화를 받지 않았습니다.",
                briefing_text=self._briefing_text,
            )
        )

    # ──────────────────────────────────────────────────────────────
    # DTMF 기반 상담원 결정 처리
    # ──────────────────────────────────────────────────────────────

    def _create_background_task(self, coro) -> asyncio.Task:
        """asyncio.Task 생성 및 참조 보관 (GC 방지)"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _run_completion_task(self, coro) -> asyncio.Task:
        """완료 경로를 백그라운드로 실행하고, 실패해도 Task가 반드시 끝나게 한다."""
        task = self._create_background_task(coro)
        task.add_done_callback(self._complete_on_failure)
        return task

    def _complete_on_failure(self, task: asyncio.Task) -> None:
        """완료 경로가 예외로 끝나면 ERROR로 완료한다.

        상위 agent는 이 Task를 timeout 없이 await하므로, 완료 경로 예외를 그대로
        두면 콜러가 대기음악 상태로 무기한 남는다. 상담원 leg가 아직 room에 있으면
        재발신하지 않도록 terminate_call을 요청한다.
        """
        if task.cancelled() or self._is_result_set():
            return
        exc = task.exception()
        if exc is None:
            return

        room = self._room
        agent_still_present = bool(
            self._agent_identity
            and room is not None
            and self._agent_identity in room.remote_participants
        )
        logger.bind(
            event="WARM_TRANSFER_COMPLETION_FAILED",
            agent_identity=self._agent_identity,
            terminate_call=agent_still_present,
        ).opt(exception=exc).error("Warm-transfer completion path failed")
        self._complete_once(
            WarmTransferResult(
                status=WarmTransferStatus.ERROR,
                agent_identity=self._agent_identity,
                error_message=str(exc),
                briefing_text=self._briefing_text,
                terminate_call=agent_still_present,
            )
        )

    def _on_agent_dtmf_received(self, ev: rtc.SipDTMF) -> None:
        """상담원의 DTMF 입력 처리 (1=수락, 2=거절/콜백)"""
        if self._dtmf_completed or self._caller_disconnected:
            return

        # 상담원의 DTMF만 처리 (콜러 DTMF 무시)
        participant_identity = getattr(
            getattr(ev, "participant", None), "identity", None
        )
        if participant_identity != self._agent_identity:
            logger.debug(
                f"Ignoring DTMF from {participant_identity}, expected {self._agent_identity}"
            )
            return

        digit = ev.digit
        is_early_dtmf = not self._briefing_complete

        if is_early_dtmf:
            logger.debug(f"Early DTMF received during briefing: {digit}")

        logger.bind(
            event="WARM_TRANSFER_DTMF",
            digit=digit,
            agent_identity=self._agent_identity,
        ).debug("Agent DTMF input received")

        self.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "WARM_TRANSFER_DTMF",
                details={"digit": digit},
            ),
        )

        if digit == "1":
            self._dtmf_completed = True
            self._cancel_dtmf_timeout()
            self.session.interrupt()
            self._run_completion_task(self._execute_connect())
        elif digit == "2":
            self._dtmf_completed = True
            self._cancel_dtmf_timeout()
            self.session.interrupt()
            self._run_completion_task(self._execute_decline("콜백 요청"))
        else:
            self._invalid_dtmf_count += 1
            logger.debug(
                f"Invalid DTMF digit: {digit}, attempt {self._invalid_dtmf_count}/{self._max_invalid_dtmf}"
            )
            if self._invalid_dtmf_count >= self._max_invalid_dtmf:
                self._dtmf_completed = True
                self._cancel_dtmf_timeout()
                self._run_completion_task(
                    self._execute_decline("유효하지 않은 입력 초과")
                )
            else:
                self._create_background_task(self._reprompt_dtmf())

    # ──────────────────────────────────────────────────────────────
    # DTMF 타임아웃 및 재안내
    # ──────────────────────────────────────────────────────────────

    def _watch_briefing_playout(self, speech_handle: SpeechHandle) -> None:
        """브리핑 실제 재생 완료 후 DTMF 응답 타이머를 시작한다."""
        self._stop_watching_briefing_playout()
        self._briefing_speech_handle = speech_handle
        speech_handle.add_done_callback(self._on_briefing_playout_done)

    def _stop_watching_briefing_playout(self) -> None:
        """등록된 브리핑 playout callback을 안전하게 제거한다."""
        speech_handle = self._briefing_speech_handle
        if speech_handle is None:
            return

        speech_handle.remove_done_callback(self._on_briefing_playout_done)
        self._briefing_speech_handle = None

    def _on_briefing_playout_done(self, speech_handle: SpeechHandle) -> None:
        """브리핑 playout 완료를 DTMF 응답 대기 시작점으로 사용한다."""
        if speech_handle is not self._briefing_speech_handle:
            return

        self._briefing_speech_handle = None
        if self._caller_disconnected or self._dtmf_completed:
            return

        self._briefing_complete = True
        logger.bind(
            event="WARM_TRANSFER_BRIEFING_PLAYOUT_COMPLETE",
            agent_identity=self._agent_identity,
            dtmf_timeout_seconds=self._dtmf_timeout_duration,
        ).debug("Briefing playout complete; starting DTMF response timeout")
        self._start_dtmf_timeout()

    def _start_dtmf_timeout(self) -> None:
        """현재 응답 대기 상태에 대해 DTMF 타임아웃을 시작한다."""
        if self._caller_disconnected or self._dtmf_completed:
            return

        self._cancel_dtmf_timeout()
        self._dtmf_timeout_task = asyncio.create_task(self._dtmf_timeout_countdown())
        self._dtmf_timeout_task.add_done_callback(self._complete_on_failure)

    async def _dtmf_timeout_countdown(self) -> None:
        """DTMF 입력 대기 타임아웃 카운트다운"""
        try:
            await asyncio.sleep(self._dtmf_timeout_duration)
            if not self._dtmf_completed:
                logger.bind(
                    event="WARM_TRANSFER_DTMF_TIMEOUT",
                    agent_identity=self._agent_identity,
                ).warning("No DTMF response from agent")
                self._dtmf_completed = True
                await self._execute_timeout()
        except asyncio.CancelledError:
            pass

    def _cancel_dtmf_timeout(self) -> None:
        """DTMF 타임아웃 취소"""
        timeout_task = self._dtmf_timeout_task
        self._dtmf_timeout_task = None
        if timeout_task and not timeout_task.done():
            timeout_task.cancel()

    async def _reprompt_dtmf(self) -> None:
        """유효하지 않은 DTMF 입력 시 재안내"""
        self.session.interrupt()
        self.session.say(
            t("warm_transfer.dtmf.invalid", self._language_code),
            allow_interruptions=True,
            add_to_chat_ctx=False,
        )
        # 타임아웃 리셋
        self._start_dtmf_timeout()

    # ──────────────────────────────────────────────────────────────
    # 연결/거절/타임아웃 실행 로직
    # ──────────────────────────────────────────────────────────────

    async def _execute_connect(self) -> None:
        """콜러와 상담원 연결 실행 (DTMF 1번)

        순서가 중요:
        1. DTMF 톤 소거 대기 (인밴드 DTMF 오디오가 버퍼에서 사라질 때까지)
        2. 먼저 콜러↔상담원 오디오 연결을 활성화
        3. 그 다음 대기음악 정리 (콜러가 "대기음악 → 상담원 음성"으로 자연스럽게 전환)
        대기음악을 먼저 끊으면 오디오 파형 절단으로 찌지직 소리가 발생함
        """
        logger.bind(
            event="WARM_TRANSFER_CONNECT",
            agent_identity=self._agent_identity,
        ).info("Agent accepted transfer via DTMF")

        # DTMF 1번 키 인밴드 톤(~200ms)이 버퍼에서 소거되도록 짧게 대기
        # 이 대기 없이 즉시 구독하면 콜러가 키패드 소리를 듣게 됨
        await asyncio.sleep(0.8)

        # 콜러 오디오 unmute (상담원이 콜러 음성을 들을 수 있도록)
        await self._unmute_caller_audio()

        # 콜러 ↔ 상담원 연결 (대기음악이 아직 재생 중이므로 자연스러운 전환)
        await self._connect_caller_to_agent()

        # 연결 완료 후 대기음악 정리 (콜러는 이미 상담원 음성을 듣고 있음)
        await self._cleanup()

        # Task 완료
        self._complete_once(
            WarmTransferResult(
                status=WarmTransferStatus.SUCCESS,
                agent_identity=self._agent_identity,
                briefing_text=self._briefing_text,
            )
        )

    async def _execute_decline(self, reason: str | None = None) -> None:
        """상담원 연결 거절 실행 (DTMF 2번 또는 유효하지 않은 입력 초과)"""
        logger.bind(
            event="WARM_TRANSFER_DECLINED",
            agent_identity=self._agent_identity,
            reason=reason,
        ).info("Agent declined transfer via DTMF")

        # 상담원 전화 끊기 (Room에서 제거)
        if self._agent_identity:
            try:
                await self._job_ctx.api.room.remove_participant(
                    api.RoomParticipantIdentity(
                        room=self._room.name,
                        identity=self._agent_identity,
                    )
                )
                logger.debug(f"Disconnected agent {self._agent_identity} from room")
            except Exception as e:
                logger.opt(exception=e).warning("Failed to disconnect agent")

        # 대기음악 정리 후 콜러-AI 오디오 복원 (cleanup → restore 순서 필수)
        await self._cleanup()
        await self._maybe_restore_caller_ai_audio()

        self._complete_once(
            WarmTransferResult(
                status=WarmTransferStatus.DECLINED,
                agent_identity=self._agent_identity,
                error_message=reason,
                briefing_text=self._briefing_text,
            )
        )

    async def _execute_timeout(self) -> None:
        """DTMF 타임아웃 처리"""
        logger.bind(
            event="WARM_TRANSFER_DTMF_TIMEOUT",
            agent_identity=self._agent_identity,
        ).info("DTMF timeout - no response from agent")

        # 상담원 전화 끊기 (Room에서 제거)
        if self._agent_identity:
            try:
                await self._job_ctx.api.room.remove_participant(
                    api.RoomParticipantIdentity(
                        room=self._room.name,
                        identity=self._agent_identity,
                    )
                )
            except Exception as e:
                logger.opt(exception=e).warning("Failed to disconnect agent on timeout")

        # 대기음악 정리 후 콜러-AI 오디오 복원 (cleanup → restore 순서 필수)
        await self._cleanup()
        await self._maybe_restore_caller_ai_audio()

        self._complete_once(
            WarmTransferResult(
                status=WarmTransferStatus.TIMEOUT,
                error_message="상담원이 DTMF 입력 응답을 하지 않았습니다.",
                briefing_text=self._briefing_text,
            )
        )

    # ──────────────────────────────────────────────────────────────
    # 음성사서함 감지 (AI 도구 - 유지)
    # ──────────────────────────────────────────────────────────────

    @function_tool
    async def voicemail_detected(self) -> str:
        """
        음성사서함이 감지되었을 때 사용합니다.

        상담원 대신 음성사서함 안내가 들리면 이 도구를 사용하세요.
        """
        self._dtmf_completed = True
        self._cancel_dtmf_timeout()

        logger.bind(
            event="WARM_TRANSFER_VOICEMAIL",
            agent_identity=self._agent_identity,
        ).info("Voicemail detected")

        await self._cleanup()
        await self._maybe_restore_caller_ai_audio()

        self._complete_once(
            WarmTransferResult(
                status=WarmTransferStatus.VOICEMAIL,
                error_message="음성사서함이 감지되었습니다.",
                briefing_text=self._briefing_text,
            )
        )

        return "음성사서함이 감지되어 연결이 취소되었습니다."
