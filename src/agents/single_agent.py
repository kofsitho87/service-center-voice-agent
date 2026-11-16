import asyncio
import os
import re
from collections.abc import AsyncIterable
from typing import Any, Optional
from uuid import uuid4

from langfuse import propagate_attributes
from livekit.agents import Agent, ModelSettings, llm
from loguru import logger

from agents.base_agent import BaseAgent
from data import (
    DTMF_DIRECT_INPUT_TARGET,
    DTMF_TOOL_ACTION_KEY_TO_FUNCTION,
    CallSessionData,
    DtmfSmsConfig,
    FlowNode,
    RunContext_T,
    parse_dtmf_option,
    parse_dtmf_side_actions,
)
from external_apis.context_api import get_call_context
from external_apis.sms_api import SmsSendResult, normalize_phone_number, send_sms
from tasks.booking.exit_reasons import (
    VALID_EXIT_REASON_CODES,
    BookingExitRouteTarget,
    normalize_booking_exit_route_target,
    normalize_exit_reason_code,
)
from tasks.dynamic_booking_task import DynamicBookingTask
from tasks.greeting_dtmf_task import GreetingDtmfTask
from tasks.transfer_reason_task import TransferPurpose
from tools.booking_tools import (
    cancel_appointment,
    get_my_appointment_list,
    modify_appointment,
)

# V3 다이나믹 플로우: 서비스+상담사+일시를 DynamicBookingTask 하나로 진행.
from tools.dynamic_booking_tools import create_new_appointment
from utils.agent_event import agent_event_extra, record_api_result
from utils.booking_resume import (
    booking_resume_tool_chunk,
    build_booking_route_context,
    consume_booking_route,
    latest_user_message_id,
    select_post_booking_route,
)
from utils.common import load_prompt
from utils.faq_match import match_faq_answer as match_context_answer
from utils.i18n import t
from utils.korean_speech_guard import guard_korean_speech

_DIRECT_BOOKING_TOOLS = frozenset(
    {
        "create_new_appointment",
        "get_my_appointment_list",
        "modify_appointment",
        "cancel_appointment",
    }
)

# 문자는 휴대전화로만 보낸다(유선·발신번호 표시 제한 번호는 건너뜀).
_MOBILE_NUMBER = re.compile(r"01[016789]\d{7,8}")
# end_call 옵션에서 통화 종료 전에 문자 발송 결과를 기다리는 최대 시간(초).
_DTMF_SMS_END_CALL_WAIT_SECONDS = 8.0


class _SafeFormatDict(dict):
    """Leave unknown ``{placeholders}`` in SMS templates untouched."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class SingleAgent(BaseAgent):
    """
    단일 에이전트로 콜 플로우를 제어하는 에이전트.
    """

    def __init__(
        self,
        *,
        call_session_data: CallSessionData,
        center_context: str = "",
    ) -> None:
        self._call_session_data = call_session_data
        self._current_node_id: Optional[str] = None

        self._booking_resume_guard_message_id: str | None = None

        center_context = center_context.strip()
        self._center_context = center_context
        instructions = self._build_instructions(call_session_data, center_context)

        # INFO_LOOKUP 이벤트 판정에 쓸 센터 안내 문단.
        # 프롬프트에 주입한 시점의 내용을 보관해야 통화 도중 원본이 수정되어도
        # 판정 기준이 흔들리지 않는다.
        self._center_context_chunks = self._split_center_context(center_context)

        # 정보 안내는 전용 조회 tool 없이 시스템 프롬프트의 센터 안내 context로 답한다.
        tools: list[Any] = [
            get_my_appointment_list,
            create_new_appointment,
            modify_appointment,
            cancel_appointment,
        ]

        super().__init__(instructions=instructions, tools=tools)

    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[llm.ChatChunk | str]:
        """Dispatch selected booking tools before any model-based routing."""
        # Session close still runs speech queued before it, so a generation can
        # start after the caller left. Produce nothing: no tool call, no reply.
        if getattr(self._call_session_data, "caller_disconnected", False):
            logger.bind(
                event="LLM_NODE_SKIPPED",
                reason="CALLER_DISCONNECTED",
                room_name=getattr(self._call_session_data, "room_name", None),
            ).info("Caller disconnected; skipping generation")
            return

        choice = model_settings.tool_choice
        tool_name = (
            choice.get("function", {}).get("name")
            if isinstance(choice, dict) and choice.get("type") == "function"
            else None
        )
        if tool_name in _DIRECT_BOOKING_TOOLS:
            if not any(getattr(tool, "id", None) == tool_name for tool in tools):
                raise ValueError(f"Selected booking tool unavailable: {tool_name}")
            # Keep LiveKit's RunContext, decorators and AgentTask lifecycle, but
            # do not ask the model to choose an action already decided by code.
            call_id = f"booking_direct_{uuid4().hex}"
            yield llm.ChatChunk(
                id=call_id,
                delta=llm.ChoiceDelta(
                    role="assistant",
                    tool_calls=[
                        llm.FunctionToolCall(
                            name=tool_name, arguments="{}", call_id=call_id
                        )
                    ],
                ),
            )
            return

        resume = getattr(self._call_session_data, "dynamic_booking_resume", None)
        user_message_id = latest_user_message_id(chat_ctx)
        should_route = (
            resume is not None
            and resume.paused
            and user_message_id is not None
            and user_message_id != resume.last_handled_user_message_id
        )
        if should_route:
            # Generation can be cancelled/restarted by interruptions. Only the
            # newest in-flight decision may publish a tool call or a reply.
            if (
                getattr(self, "_booking_resume_guard_message_id", None)
                == user_message_id
            ):
                return
            self._booking_resume_guard_message_id = user_message_id
            try:
                async with asyncio.timeout(12):
                    route = await self._classify_booking_resume(chat_ctx, resume)
                if (
                    self._booking_resume_guard_message_id != user_message_id
                    or self._call_session_data.dynamic_booking_resume is not resume
                    or not resume.paused
                ):
                    return
                resume.last_handled_user_message_id = user_message_id
                self.session.history.add_message(
                    role="developer",
                    content="",
                    extra=agent_event_extra(
                        "BOOKING_RESUME_ROUTE",
                        status="DECIDED",
                        details={
                            "route": route,
                            "user_message_id": user_message_id,
                        },
                    ),
                )
                if route == "resume_booking":
                    if not any(
                        getattr(tool, "id", None) == "create_new_appointment"
                        for tool in tools
                    ):
                        raise ValueError("Booking tool unavailable in this generation")
                    # No free-form text reaches TTS on this branch. The real
                    # booking tool still applies action-mode and identity rules.
                    yield booking_resume_tool_chunk(user_message_id)
                    return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self._booking_resume_guard_message_id != user_message_id:
                    return
                resume.last_handled_user_message_id = user_message_id
                logger.bind(
                    event="BOOKING_RESUME_ROUTE_FAILED",
                    error_type=type(error).__name__,
                    room_name=self._call_session_data.room_name,
                ).warning("Could not safely route the post-booking request")
                # A routing failure must not fall through into an answer about
                # schedules. A later user turn retries instead of disabling it.
                yield (
                    "예약 가능한 일정을 다시 찾으시는 걸까요, 다른 문의이신가요?"
                    if (self._language or "ko").startswith("ko")
                    else "Would you like to find another appointment, or do you "
                    "have a different question?"
                )
                return
            finally:
                if self._booking_resume_guard_message_id == user_message_id:
                    self._booking_resume_guard_message_id = None

        async for chunk in guard_korean_speech(
            self, Agent.default.llm_node(self, chat_ctx, tools, model_settings)
        ):
            yield chunk

    async def _classify_booking_resume(self, chat_ctx, resume) -> str:
        # Agent.default uses this activity's configured LLM and connection
        # options. No second provider/client or model override is introduced.
        stream = Agent.default.llm_node(
            self,
            build_booking_route_context(chat_ctx, resume),
            [select_post_booking_route],
            ModelSettings(
                tool_choice={
                    "type": "function",
                    "function": {"name": "select_post_booking_route"},
                }
            ),
        )
        return await consume_booking_route(stream)

    @classmethod
    async def create(cls, *, call_session_data: CallSessionData) -> "SingleAgent":
        """압축된 센터 안내 context를 조회해 주입한 SingleAgent를 생성한다.

        context가 있으면 Center Knowledge 프롬프트를, 없거나 조회에 실패하면
        센터 안내 정보가 없는 안전한 기본 프롬프트를 사용한다.
        """
        center_context = await cls._fetch_center_context(call_session_data)
        return cls(
            call_session_data=call_session_data,
            center_context=center_context,
        )

    @staticmethod
    async def _fetch_center_context(
        call_session_data: CallSessionData,
    ) -> str:
        """시스템 프롬프트에 주입할 압축된 센터 안내 context를 조회한다.

        client_id가 없거나 조회에 실패하면 빈 문자열을 반환한다. 센터 안내 조회
        실패가 통화 자체를 막지 않도록 정보 없는 기본 프롬프트로 폴백한다.
        """
        bd = call_session_data.business_data
        client_id = bd.client_id if bd else None
        room_name = call_session_data.room_name

        if client_id is None:
            logger.bind(
                event="CALL_CONTEXT_FETCH_SKIPPED",
                room_name=room_name,
            ).warning("client_id missing; using no-knowledge prompt")
            return ""

        try:
            response = await get_call_context(client_id)
            center_context = response.data.context.strip()
        except Exception as e:
            call_session_data.pending_api_results.append(
                {
                    "api": "GET_CALL_CONTEXT",
                    "success": False,
                    "message": "센터 안내 context 조회에 실패했습니다.",
                }
            )
            logger.bind(
                event="CALL_CONTEXT_FETCH_FAILED",
                client_id=client_id,
                room_name=room_name,
            ).opt(exception=e).warning(
                "Failed to fetch center context; using no-knowledge prompt"
            )
            return ""

        call_session_data.pending_api_results.append(
            {
                "api": "GET_CALL_CONTEXT",
                "success": True,
                "result_count": int(bool(center_context)),
            }
        )

        if not center_context:
            logger.bind(
                event="CALL_CONTEXT_EMPTY",
                client_id=client_id,
                room_name=room_name,
            ).warning("Center context is empty; using no-knowledge prompt")
            return ""

        logger.bind(
            event="CALL_CONTEXT_FETCHED",
            client_id=client_id,
            context_length=len(center_context),
            content_updated_at=response.data.content_updated_at.isoformat(),
            room_name=room_name,
        ).info("Center context fetched for system prompt")
        return center_context

    @property
    def _language(self) -> Optional[str]:
        """현재 통화의 언어 코드(BCP 47). 고정멘트 조회용."""
        ccd = self._call_session_data.call_config_data
        return ccd.language if ccd else None

    def _request_exact_tool_call(
        self,
        tool_name: str,
        *,
        user_input: str | None = None,
        allow_interruptions: bool = False,
    ) -> None:
        """Schedule a tool turn; llm_node bypasses the provider for booking tools."""
        if getattr(self._call_session_data, "caller_disconnected", False):
            logger.bind(
                event="EXACT_TOOL_CALL_SKIPPED",
                reason="CALLER_DISCONNECTED",
                tool_name=tool_name,
                room_name=getattr(self._call_session_data, "room_name", None),
            ).info("Caller disconnected; not scheduling tool turn")
            return

        reply_kwargs: dict[str, Any] = {
            "tool_choice": {
                "type": "function",
                "function": {"name": tool_name},
            },
            "tools": [tool_name],
            "instructions": (
                "Call the selected tool immediately. "
                "Do not emit any assistant text before the tool call. "
                "Do not produce commentary, a preamble, greeting, "
                "acknowledgement, or progress update. "
                "The tool owns all user-facing speech."
            ),
            "allow_interruptions": allow_interruptions,
        }
        if user_input:
            reply_kwargs["user_input"] = user_input
        self.session.generate_reply(**reply_kwargs)

    async def handle_booking_exit(
        self,
        ctx: RunContext_T,
        *,
        source_action: str,
        reason_code: str | None,
        detail: str = "",
        route_target: BookingExitRouteTarget | None = None,
        handoff_accepted: bool | None = None,
        handoff_channel: str | None = None,
        stopped_before_selection: bool = False,
    ) -> None:
        """Execute a booking exit without exposing routing text to the LLM."""
        raw_reason_is_valid = reason_code in VALID_EXIT_REASON_CODES
        normalized_reason = normalize_exit_reason_code(reason_code)
        normalized_target = (
            normalize_booking_exit_route_target(route_target)
            if raw_reason_is_valid
            else "other"
        )
        logger.bind(
            event="BOOKING_EXIT_ROUTED",
            source_action=source_action,
            reason_code=normalized_reason,
            route_target=(
                normalized_target
                if normalized_reason in {"unrelated", "other"}
                else None
            ),
            handoff_accepted=handoff_accepted,
            handoff_channel=handoff_channel,
            has_detail=bool(detail.strip()),
            room_name=ctx.userdata.room_name,
        ).info("Routing booking exit in Python")

        booking_context = getattr(ctx.userdata, "booking_memo_context", None)
        handoff_action = (
            source_action
            if booking_context is not None
            and booking_context.exit_reason_code == normalized_reason
            else None
        )
        transfer_purpose = (
            TransferPurpose(code=normalized_reason)
            if handoff_action == "booking_create"
            and normalized_reason in {"no_slot_memo", "service_not_offered"}
            else None
        )
        if (
            transfer_purpose is None
            and handoff_action == "booking_create"
            and normalized_reason == "staff_connection"
            and booking_context.searched_ranges
        ):
            transfer_purpose = TransferPurpose(code="no_slot_memo")

        if normalized_reason in {"no_slot_memo", "staff_connection"}:
            await self._handle_transfer_call(
                call_session_data=ctx.userdata,
                action_key=handoff_action,
                transfer_purpose=transfer_purpose,
            )
            return

        if normalized_reason == "service_not_offered":
            if handoff_accepted is True:
                await self._handle_transfer_call(
                    call_session_data=ctx.userdata,
                    action_key=handoff_action,
                    transfer_purpose=transfer_purpose,
                )
            else:
                ctx.userdata.booking_memo_context = None
                self.session.input.set_audio_enabled(True)
            return

        if normalized_reason in {"change_service", "change_advisor"}:
            self.session.input.set_audio_enabled(True)
            self._request_exact_tool_call("create_new_appointment")
            return

        if normalized_reason == "cancel":
            self.session.input.set_audio_enabled(True)
            self.session.say(
                t(
                    "booking.exit.cancelled_ack_ask_more"
                    if stopped_before_selection
                    else "booking.exit.cancelled_ack",
                    self._language,
                ),
                allow_interruptions=True,
            )
            return

        self.session.input.set_audio_enabled(True)
        target_tools = {
            "booking_inquiry": "get_my_appointment_list",
            "booking_modify": "modify_appointment",
            "booking_cancel": "cancel_appointment",
        }
        if tool_name := target_tools.get(normalized_target):
            self._request_exact_tool_call(tool_name)
            return

        if normalized_target == "end_call":
            await self._handle_end_call_by_user_request(ctx)
            return

        instructions = (
            "Answer only the caller's latest actual request using the normal "
            "service-center-knowledge grounding rules. Do not restart the booking "
            "flow, do not infer intent from tool result text, and do not end "
            "the call automatically."
            if normalized_target == "info_request"
            else (
                "Respond briefly to the caller's latest actual request. Do not "
                "restart booking, transfer, or end the call automatically."
            )
        )
        self.session.generate_reply(
            instructions=instructions,
            allow_interruptions=True,
            tool_choice="none",
        )

    @staticmethod
    def _build_instructions(
        call_session_data: CallSessionData,
        center_context: str = "",
    ) -> str:
        """단일 에이전트용 시스템 인스트럭션 생성.

        `center_context`가 있으면 context_instructions 변형을, 비어 있으면
        센터 안내 정보가 없는 기본 instructions를 사용한다.
        """
        bd = call_session_data.business_data
        common_kwargs: dict[str, str] = {
            "language_code": call_session_data.call_config_data.language,
            "default_language": (
                call_session_data.call_config_data.get_language_prompt_string()
            ),
            "client_name": (bd.client_name if bd else "") or "",
            "client_phone": (bd.client_phone if bd else "") or "",
            "client_address": (bd.client_address if bd else "") or "",
        }

        if center_context:
            return load_prompt(
                "single_agent_prompt.yaml",
                variant="context",
                center_context=center_context,
                **common_kwargs,
            )
        return load_prompt("single_agent_prompt.yaml", **common_kwargs)

    @staticmethod
    def _split_center_context(center_context: str) -> list[str]:
        """센터 안내 context를 사후 응답 판정에 사용할 문단으로 분리한다."""
        return [
            " ".join(paragraph.split())
            for paragraph in re.split(r"\n\s*\n", center_context)
            if paragraph.strip()
        ]

    def _register_context_match_handler(self) -> None:
        """Center Knowledge 기반 응대 감지 핸들러를 등록한다.

        압축 context를 시스템 프롬프트에 직접 주입하면 INFO_LOOKUP 이벤트를
        남길 tool 호출이 없다. 대신 완성된 발화를 주제별 context 문단과 사후
        비교한다. 발화 이후 동기적으로 실행되므로 응답 레이턴시에 영향이 없다.
        """
        if not self._center_context_chunks:
            return

        @self.session.on("conversation_item_added")
        def _on_conversation_item_added(event: Any) -> None:
            try:
                # The booking task receives this same context and can answer
                # center questions directly. Other tasks must not generate
                # INFO_LOOKUP events from incidental overlapping words.
                active_agent = self.session.current_agent
                if active_agent is not self and not (
                    isinstance(active_agent, DynamicBookingTask)
                    and active_agent._center_context == self._center_context
                ):
                    return
                self._record_context_answer_if_matched(event)
            except Exception as e:
                # 분류용 부가 기능이므로 어떤 경우에도 통화를 방해하지 않는다.
                logger.opt(exception=e).warning("Context match handler failed")

    def _record_context_answer_if_matched(self, event: Any) -> None:
        """에이전트 발화가 센터 안내 context와 일치하면 이벤트를 남긴다."""
        item = getattr(event, "item", None)
        if getattr(item, "type", None) != "message":
            return
        if getattr(item, "role", None) != "assistant":
            return

        text = getattr(item, "text_content", None)
        if not text:
            return

        result = match_context_answer(text, self._center_context_chunks)
        if result is None:
            return

        if not result.matched:
            # A booking task may answer a center question and then resume the
            # booking in the same turn. The continuation dilutes the score of
            # the complete answer, so also inspect substantive sentences.
            for sentence in re.split(r"(?<=[.!?。])\s+|\n+", text):
                sentence_result = match_context_answer(
                    sentence,
                    self._center_context_chunks,
                    min_length=16,
                )
                if sentence_result and sentence_result.score > result.score:
                    result = sentence_result

        # 임계값 미달도 남긴다. 미탐/오탐 추적과 임계값 조정의 유일한 근거다.
        logger.bind(
            event="CALL_CONTEXT_ANSWER_MATCH",
            matched=result.matched,
            context_chunk_index=result.index,
            score=round(result.score, 3),
            room_name=self._call_session_data.room_name,
        ).debug("Center context answer match evaluated")

        if not result.matched:
            return

        with propagate_attributes(tags=["info_lookup"]):
            self.session.history.add_message(
                role="developer",
                content="",
                extra=agent_event_extra(
                    "INFO_LOOKUP",
                    status="SUCCESS",
                    details={"source": "CALL_CONTEXT_MATCH"},
                ),
            )

    async def on_enter(self) -> None:
        # ! 시작시 오디오 입력 비활성화 (인터럽션 방지)
        # self.session.input.set_audio_enabled(False)

        for result in self._call_session_data.pending_api_results:
            record_api_result(self.session.history, **result)
        self._call_session_data.pending_api_results.clear()

        self._register_context_match_handler()

        logger.bind(
            event="SINGLE_AGENT_ON_ENTER",
            room_name=self._call_session_data.room_name,
            phone=self._call_session_data.recipient_data.phone,
        ).debug("*********** SingleAgent on_enter *************")

        # Early disconnect guard: caller가 session.start 전에 이미 끊은 경우
        # 인사말/안내 발화 없이 즉시 종료한다.
        if self._call_session_data.caller_disconnected:
            logger.bind(
                event="EARLY_DISCONNECT_GUARD",
                stage="supervisor_on_enter",
                room_name=self._call_session_data.room_name,
            ).info("Caller already disconnected before flow started; skipping flow")
            self._record_call_termination("USER", "CALLER_DISCONNECTED")
            self.session.shutdown(drain=False)
            return

        flow_config = self._call_session_data.flow_config
        if not flow_config:
            logger.error("No flow_config found")
            await self._say_and_wait(
                t("system.config_error", self._language),
                failure_log=logger,
                failure_message="Error waiting for no-flow-config error message",
            )
            self._record_call_termination("SYSTEM", "NO_FLOW_CONFIG")
            self.session.shutdown()
            return

        # 진입 노드부터 시작
        entry_node = flow_config.get_entry_node()
        if not entry_node:
            logger.error(f"Entry node not found: {flow_config.entry_node_id}")
            return

        await self._process_node(entry_node.id)

    async def _process_node(self, node_id: str) -> None:
        """노드 처리 메인 루프"""
        flow_config = self._call_session_data.flow_config
        if not flow_config:
            return

        node = flow_config.get_node(node_id)

        if not node:
            await self._handle_missing_node(node_id)
            return

        self._current_node_id = node_id

        next_node_id: Optional[str] = None

        # 노드 타입별 처리
        if node.type == "condition":
            next_node_id = await self._handle_condition_node(node)
        elif node.type == "greeting":
            next_node_id = await self._handle_greeting_node(node)
        elif node.type == "agent":
            await self._handle_agent_node(node)
            return  # 에이전트 전환 후 종료
        elif node.type == "exit":
            await self._handle_exit_node(node)
            return  # 통화 종료
        elif node.type == "action":
            next_node_id = await self._handle_action_node(node)
        else:
            logger.warning(f"Unknown node type: {node.type}")
            next_node_id = node.next

        # 다음 노드로 이동
        if next_node_id:
            await self._process_node(next_node_id)

    async def _handle_condition_node(self, node: FlowNode) -> Optional[str]:
        """조건 노드 처리 - 필드 값에 따라 브랜치 결정"""
        field = node.field
        branches = node.branches or {}

        if not field:
            logger.warning(f"Condition node {node.id} has no field")
            return node.next

        # 필드 값 가져오기
        value = self._get_field_value(field)
        value_str = str(value).lower() if value is not None else "false"

        logger.debug(f"Condition: {field} = {value_str}")

        # 브랜치에서 다음 노드 결정
        next_node_id = branches.get(value_str)
        if not next_node_id:
            # "any" 또는 default 처리
            next_node_id = branches.get("any") or node.next
            logger.debug(f"Using fallback branch: {next_node_id}")

        logger.debug(f"Condition result: next_node = {next_node_id}")
        return next_node_id

    @staticmethod
    def _should_play_recording_notice() -> bool:
        return os.getenv("ENV", "dev").strip().lower() != "local"

    async def _handle_greeting_node(self, node: FlowNode) -> Optional[str]:
        """인사말 노드 처리 - TTS 재생 및 DTMF 입력 처리"""

        # 인사말 메시지
        greeting_message = node.message

        input_method = node.input_method
        dtmf_options = node.dtmf_options

        # Case 1: DTMF 입력 필요
        if input_method == "dtmf" and dtmf_options:
            # GreetingDtmfTask용 단순 형식 옵션 맵 생성
            simple_dtmf_options = self._build_simple_dtmf_options(dtmf_options)

            # 현재 노드 자신을 가리키는 키(예: "*")는 노드 재실행이 아니라
            # task 내부에서 replay_message로 다시듣기 처리한다.
            replay_keys = {
                key
                for key, value in dtmf_options.items()
                if parse_dtmf_option(value)[0] == node.id
            }

            if greeting_message and self._should_play_recording_notice():
                greeting_message += t("greeting.recording_notice", self._language)

            logger.bind(
                message=greeting_message,
                input_method=input_method,
                dtmf_options=simple_dtmf_options,
            ).debug("Starting DTMF greeting task")

            # Run the greeting DTMF task
            with propagate_attributes(tags=["greeting_dtmf"]):
                result = await GreetingDtmfTask(
                    message=greeting_message or "",
                    replay_message=node.replay_message,
                    dtmf_options=simple_dtmf_options,
                    replay_keys=replay_keys,
                    timeout=5.0,
                    replay_on_invalid=True,
                    max_invalid_attempts=2,
                )

            logger.bind(
                message=greeting_message,
                selected_key=result.selected_key,
                interrupted=result.interrupted,
            ).debug("Greeting DTMF task result")

            if result.selected_key and result.selected_key in dtmf_options:
                # 선택된 옵션에서 라우팅 및 선택 후 안내 설정 추출
                (
                    next_node_id,
                    user_request,
                    dtmf_tool,
                    on_select_message,
                ) = parse_dtmf_option(dtmf_options[result.selected_key])

                self.session.history.add_message(
                    role="developer",
                    content="",
                    extra=agent_event_extra(
                        "GREETING_DTMF_TASK",
                        status="SELECTED",
                        details={
                            "selected_key": result.selected_key,
                            "user_request": user_request,
                            # Canonical action key for privacy-safe downstream
                            # evaluation.  Raw keypad digits are flow-specific,
                            # while this value has stable business semantics.
                            "action_key": (
                                None
                                if next_node_id == DTMF_DIRECT_INPUT_TARGET
                                else dtmf_tool
                            ),
                        },
                    ),
                )

                # 문자 발송은 안내 멘트 재생과 동시에 시작해 지연을 숨긴다.
                sms_config, end_call = parse_dtmf_side_actions(
                    dtmf_options[result.selected_key]
                )
                sms_task = self._start_dtmf_sms(
                    node.id, result.selected_key, sms_config
                )

                if next_node_id == DTMF_DIRECT_INPUT_TARGET:
                    # 직접입력 옵션은 선택 안내만 재생하고 실제 사용자 발화를
                    # 기다린다. 설정에 user_request/tool 이 잘못 포함되어도
                    # 합성 user 메시지나 지연된 tool 호출로 이어지지 않게 비운다.
                    agents_data = self._call_session_data.agents_data
                    if agents_data:
                        agents_data.user_request = None
                        agents_data.dtmf_tool = None

                    if user_request or dtmf_tool:
                        logger.bind(
                            event="DTMF_DIRECT_INPUT",
                            selected_key=result.selected_key,
                            user_request=user_request,
                            dtmf_tool=dtmf_tool,
                        ).warning(
                            "Ignoring user_request/tool configured for direct input"
                        )

                    await self._play_on_select_message(on_select_message)
                    if end_call:
                        await self._end_call_after_dtmf(
                            result.selected_key, sms_task, sms_config
                        )
                        return None
                    self._announce_dtmf_sms_later(sms_task, sms_config)
                    self.session.input.set_audio_enabled(True)
                    return None

                # user_request / dtmf_tool 을 agents_data에 저장
                agents_data = self._call_session_data.agents_data
                if agents_data:
                    if user_request:
                        agents_data.user_request = user_request
                        logger.debug(f"Saved user_request: {user_request}")
                    if dtmf_tool:
                        agents_data.dtmf_tool = dtmf_tool
                        logger.debug(f"Saved dtmf_tool: {dtmf_tool}")

                logger.debug(
                    f"User selected option {result.selected_key} -> {next_node_id}"
                )

                await self._play_on_select_message(on_select_message)

                if end_call:
                    await self._end_call_after_dtmf(
                        result.selected_key, sms_task, sms_config
                    )
                    return None
                self._announce_dtmf_sms_later(sms_task, sms_config)

                if next_node_id == "booking_agent":
                    return await self._handle_agent_node(
                        FlowNode(id="booking_agent", type="agent", name="예약 에이전트")
                    )
                elif next_node_id == "info_agent":
                    return await self._handle_agent_node(
                        FlowNode(id="info_agent", type="agent", name="정보 에이전트")
                    )
                return next_node_id
            else:
                self.session.history.add_message(
                    role="developer",
                    content="",
                    extra=agent_event_extra(
                        "GREETING_DTMF_TASK",
                        status=(
                            "INTERRUPTED"
                            if result.interrupted
                            else "TIMED_OUT"
                            if result.timed_out
                            else "NO_SELECTION"
                        ),
                        details={"selected_key": result.selected_key},
                    ),
                )

                if result.interrupted:
                    caller_disconnected = self._call_session_data.caller_disconnected
                    self._record_call_termination(
                        "USER" if caller_disconnected else "SYSTEM",
                        (
                            "CALLER_DISCONNECTED"
                            if caller_disconnected
                            else "GREETING_PLAYOUT_INTERRUPTED"
                        ),
                    )
                    self.session.shutdown(drain=not caller_disconnected)
                    return None

                if result.timed_out:
                    # caller가 이미 끊은 경우 timeout 안내 발화를 스킵한다.
                    # (인사말/타임아웃 대기 중 hangup 시 의미없는 안내가 transcript와
                    # Kafka 분석 payload에 남는 문제를 방지)
                    if self._call_session_data.caller_disconnected:
                        logger.bind(
                            event="DTMF_TIMEOUT_SKIP",
                            reason="CALLER_DISCONNECTED",
                            room_name=self._call_session_data.room_name,
                        ).debug(
                            "Caller disconnected during greeting; skipping timeout exit message"
                        )
                        self._record_call_termination("USER", "CALLER_DISCONNECTED")
                        self.session.shutdown(drain=False)
                        return None

                    # max_invalid_attempts 초과 또는 타임아웃 → 안내 후 통화 종료
                    logger.info("DTMF timed out, ending call")
                    await self._say_and_wait(
                        t("dtmf.timeout_exit", self._language),
                        failure_log=logger,
                        failure_message="Error waiting for DTMF timeout exit message",
                    )
                    self._record_call_termination("SYSTEM", "MAX_INVALID_DTMF")
                    self.session.shutdown()
                    return None
                # Timeout or invalid - use default next node
                logger.debug(f"No valid selection, using default next: {node.next}")
                return node.next

        # Case 2: 일반 인사말 (DTMF 없음)
        if greeting_message:
            logger.debug(f"Playing greeting: {greeting_message[:50]}...")

            # 인사말 시작 태그 기록
            self.session.history.add_message(
                role="developer",
                content="",
                extra=agent_event_extra("GREETING", status="STARTED"),
            )

            # Do not let caller audio start a VAD turn while the
            # uninterruptible greeting is playing. Otherwise, a noise/VAD
            # onset just before the greeting can be merged with the first
            # post-greeting transcript and distort its playback segment.
            self.session.input.set_audio_enabled(False)
            try:
                speech_handle = self.session.say(
                    greeting_message,
                    allow_interruptions=False,
                )
                await speech_handle.wait_for_playout()
            finally:
                self.session.input.set_audio_enabled(True)

            if self._call_session_data.caller_disconnected:
                logger.bind(
                    event="GREETING_CALLER_DISCONNECTED",
                    room_name=self._call_session_data.room_name,
                ).info("Caller disconnected during greeting; ending call")
                self._record_call_termination("USER", "CALLER_DISCONNECTED")
                self.session.shutdown(drain=False)
                return None

            if speech_handle.interrupted:
                logger.debug(
                    "Greeting TTS interrupted (e.g. hangup); skipping next node"
                )
                return None

            if self._should_play_recording_notice():
                # 연결된 caller에게만 녹음 안내를 재생한다. disconnect 확인과 say 사이의
                # race도 잡을 수 있도록 handle을 보관하고 발화 요청 직후 한 번 더 확인한다.
                recording_notice_message = t(
                    "greeting.recording_notice", self._language
                )
                recording_notice_handle = self.session.say(
                    recording_notice_message,
                    allow_interruptions=True,
                )

                if self._call_session_data.caller_disconnected:
                    logger.bind(
                        event="GREETING_RECORDING_NOTICE_CALLER_DISCONNECTED",
                        room_name=self._call_session_data.room_name,
                    ).info("Caller disconnected before recording notice playout")
                    recording_notice_handle.interrupt(force=True)
                    self._record_call_termination("USER", "CALLER_DISCONNECTED")
                    self.session.shutdown(drain=False)
                    return None

            self.session.history.add_message(
                role="developer",
                content="",
                extra=agent_event_extra("GREETING", status="COMPLETED"),
            )

            if node.next:
                return node.next

            if input_method == "voice":
                self.session.input.set_audio_enabled(True)
                return None

            return None

        self.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra("GREETING", status="COMPLETED"),
        )
        return node.next

    async def _play_on_select_message(self, message: Optional[str]) -> None:
        """DTMF 선택 후 다음 노드 실행 전에 설정된 안내 멘트를 재생한다."""
        if not message or not message.strip():
            return

        if self._call_session_data.caller_disconnected:
            logger.bind(
                event="DTMF_ON_SELECT_MESSAGE",
                reason="CALLER_DISCONNECTED",
                room_name=self._call_session_data.room_name,
            ).debug("Caller disconnected; skipping on-select message")
            return

        logger.bind(
            event="DTMF_ON_SELECT_MESSAGE",
            message=message,
        ).debug("Playing on-select message")

        await self._say_and_wait(
            message,
            failure_log=logger.bind(event="DTMF_ON_SELECT_MESSAGE", message=message),
            failure_message="Error waiting for on-select message playout",
        )

    def _start_dtmf_sms(
        self,
        node_id: str,
        selected_key: str,
        config: DtmfSmsConfig | None,
    ) -> asyncio.Task[SmsSendResult] | None:
        """Start the configured SMS in the background, once per node/key."""
        if config is None:
            return None

        sent = getattr(self, "_sent_dtmf_sms", None)
        if sent is None:
            sent = self._sent_dtmf_sms = set()
        dedupe_key = (node_id, selected_key)
        if dedupe_key in sent:
            logger.bind(
                event="DTMF_SMS",
                status="DUPLICATE",
                selected_key=selected_key,
                room_name=self._call_session_data.room_name,
            ).info("SMS already sent for this keypad option; skipping")
            return None
        sent.add(dedupe_key)
        return asyncio.create_task(self._send_dtmf_sms(selected_key, config))

    async def _send_dtmf_sms(
        self, selected_key: str, config: DtmfSmsConfig
    ) -> SmsSendResult:
        call_data = self._call_session_data
        recipient = call_data.recipient_data
        receiver = normalize_phone_number(
            getattr(recipient, "caller_phone", None)
            or getattr(recipient, "phone", None)
        )
        site_id = getattr(call_data.business_data, "site_id", None)

        skip_reason = None
        if not site_id:
            skip_reason = "SITE_ID_MISSING"
        elif not _MOBILE_NUMBER.fullmatch(receiver):
            skip_reason = "NON_MOBILE_RECEIVER"
        if skip_reason:
            self._record_dtmf_sms_event(selected_key, "SKIPPED", reason=skip_reason)
            return SmsSendResult(success=False, message=skip_reason)

        result = await send_sms(
            site_id=site_id,
            receiver_phone_number=receiver,
            subject=self._render_sms_text(config.subject),
            content=self._render_sms_text(config.content) or "",
            image_urls=config.image_urls,
        )
        self._record_dtmf_sms_event(
            selected_key,
            "SUCCESS" if result.success else "FAILED",
            message_type=result.message_type,
            reason=None if result.success else result.message,
        )
        return result

    def _render_sms_text(self, text: str | None) -> str | None:
        """Fill ``{client_name}`` style placeholders from business_data."""
        if not text:
            return text
        bd = self._call_session_data.business_data
        values = _SafeFormatDict(
            client_name=getattr(bd, "client_name", None) or "",
            client_phone=getattr(bd, "client_phone", None) or "",
            client_address=getattr(bd, "client_address", None) or "",
        )
        try:
            return text.format_map(values)
        except Exception:
            # Literal braces or positional fields: send the template as written.
            return text

    def _record_dtmf_sms_event(
        self,
        selected_key: str,
        status: str,
        *,
        message_type: str | None = None,
        reason: str | None = None,
    ) -> None:
        details = {"trigger": "DTMF", "selected_key": selected_key}
        if message_type:
            details["message_type"] = message_type
        if reason:
            details["reason"] = reason
        logger.bind(
            event="DTMF_SMS",
            status=status,
            room_name=self._call_session_data.room_name,
            **details,
        ).info("Keypad SMS processed")
        try:
            self.session.history.add_message(
                role="developer",
                content="",
                extra=agent_event_extra("SMS_SEND", status=status, details=details),
            )
        except Exception as e:
            # The session may already be closed when a background send finishes.
            logger.opt(exception=e).debug("Could not record SMS_SEND event")

    def _announce_dtmf_sms_later(
        self,
        sms_task: asyncio.Task[SmsSendResult] | None,
        config: DtmfSmsConfig | None,
    ) -> None:
        """Speak the configured result message once the background send ends."""
        if sms_task is None or config is None:
            return
        if not (config.on_success_message or config.on_failure_message):
            return

        async def _announce() -> None:
            try:
                result = await sms_task
                message = (
                    config.on_success_message
                    if result.success
                    else config.on_failure_message
                )
                if message and not self._call_session_data.caller_disconnected:
                    self.session.say(message, allow_interruptions=True)
            except Exception as e:
                logger.opt(exception=e).warning("Failed to announce SMS result")

        tasks = getattr(self, "_dtmf_sms_tasks", None)
        if tasks is None:
            tasks = self._dtmf_sms_tasks = set()
        task = asyncio.create_task(_announce())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _end_call_after_dtmf(
        self,
        selected_key: str,
        sms_task: asyncio.Task[SmsSendResult] | None,
        config: DtmfSmsConfig | None,
    ) -> None:
        """end_call option: wait for the SMS, speak its result, then hang up."""
        self.session.input.set_audio_enabled(False)
        agents_data = self._call_session_data.agents_data
        if agents_data:
            agents_data.user_request = None
            agents_data.dtmf_tool = None

        result: SmsSendResult | None = None
        if sms_task is not None:
            try:
                result = await asyncio.wait_for(
                    sms_task, timeout=_DTMF_SMS_END_CALL_WAIT_SECONDS
                )
            except Exception as e:
                logger.bind(
                    event="DTMF_END_CALL",
                    room_name=self._call_session_data.room_name,
                ).opt(exception=e).warning("SMS did not finish before ending call")

        if self._call_session_data.caller_disconnected:
            self._record_call_termination("USER", "CALLER_DISCONNECTED")
            self.session.shutdown(drain=False)
            return

        if config is not None:
            message = (
                config.on_success_message
                if result is not None and result.success
                else config.on_failure_message
            )
            if message:
                await self._say_and_wait(
                    message,
                    failure_log=logger.bind(event="DTMF_END_CALL"),
                    failure_message="Error waiting for SMS result message",
                )

        await self._say_and_wait(
            t("dtmf.end_call_goodbye", self._language),
            failure_log=logger.bind(event="DTMF_END_CALL"),
            failure_message="Error waiting for end-call goodbye message",
        )

        logger.bind(
            event="DTMF_END_CALL",
            selected_key=selected_key,
            room_name=self._call_session_data.room_name,
        ).info("Ending call after keypad option")
        self._record_call_termination("SYSTEM", "DTMF_END_CALL")
        self.session.shutdown()

    @staticmethod
    def _build_simple_dtmf_options(dtmf_options: dict[str, Any]) -> dict[str, str]:
        """확장 형식의 dtmf_options를 GreetingDtmfTask용 단순 형식으로 변환"""
        simple_options: dict[str, str] = {}
        for key, value in dtmf_options.items():
            _, user_request, _, _ = parse_dtmf_option(value)
            simple_options[key] = user_request
        return simple_options

    def _record_info_lookup_start(
        self, node: FlowNode, dtmf_tool: Optional[str]
    ) -> None:
        """정보 안내로 라우팅된 시점을 시도 시작으로 기록한다.

        정보 안내에는 전용 tool 이 없다(data.py: info_look_up → None). 주입된
        Center Knowledge로 답하므로 '시작' 을 관측할 지점이 이 라우팅뿐이다.
        이 이벤트가 없으면 답변에 성공한 통화만 보여 미응답률을 셀 수 없다.
        """
        if dtmf_tool != "info_look_up" and node.id != "info_agent":
            return

        self.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "INFO_LOOKUP",
                status="STARTED",
                details={"source": "DTMF" if dtmf_tool else "FLOW"},
            ),
        )

    async def _handle_agent_node(self, node: FlowNode) -> None:
        """에이전트 노드 처리 - 해당 에이전트로 전환"""
        agents_data = self._call_session_data.agents_data

        if not agents_data:
            logger.error("agents_data is None")
            return

        user_request = agents_data.user_request
        dtmf_tool = agents_data.dtmf_tool
        tool_name = (
            DTMF_TOOL_ACTION_KEY_TO_FUNCTION.get(dtmf_tool) if dtmf_tool else None
        )

        logger.bind(
            event="AGENT_NODE",
            user_request=user_request,
            dtmf_tool=dtmf_tool,
            tool_name=tool_name,
        ).debug("*********** Agent node *************")

        # 등록된 action_key 인데 tool 이 None 인 경우(예: info_look_up)는 정상이며,
        # 아래에서 user_request 만 LLM 에 전달한다. 미등록 key 만 경고한다.
        if dtmf_tool and dtmf_tool not in DTMF_TOOL_ACTION_KEY_TO_FUNCTION:
            logger.warning(f"Unknown dtmf_tool action_key: {dtmf_tool}")

        info_lookup_tags = (
            ["info_lookup"]
            if dtmf_tool == "info_look_up" or node.id == "info_agent"
            else []
        )
        with propagate_attributes(tags=info_lookup_tags):
            self._record_info_lookup_start(node, dtmf_tool)

            if tool_name:
                self._request_exact_tool_call(
                    tool_name,
                    user_input=(
                        user_request
                        if user_request and node.id != "booking_agent"
                        else None
                    ),
                    allow_interruptions=node.id != "booking_agent",
                )
                logger.bind(
                    event="DTMF_TOOL_INVOCATION",
                    dtmf_tool=dtmf_tool,
                    function_name=tool_name,
                    user_request=user_request,
                ).debug("Invoking tool from DTMF selection")
            elif user_request:
                self.session.generate_reply(
                    user_input=user_request,
                    allow_interruptions=False,
                )

        agents_data.user_request = None
        agents_data.dtmf_tool = None

        self.session.input.set_audio_enabled(True)

        return None

    async def _handle_exit_node(self, node: FlowNode) -> None:
        """종료 노드 처리 - 종료 메시지 후 통화 종료"""
        message = node.message
        termination = node.termination or {}
        termination_actor = termination.get("actor") or "USER"
        termination_reason = termination.get("reason") or "EXPLICIT_REQUEST"

        if message:
            logger.debug(f"Playing exit message: {message[:50]}...")
            await self._say_and_wait(
                message,
                failure_log=logger.bind(
                    event="EXIT_NODE",
                    message=message,
                    room_name=self._call_session_data.room_name,
                    phone=self._call_session_data.recipient_data.phone,
                ),
                failure_message="Error waiting for playout",
            )

        logger.bind(
            event="EXIT_NODE",
            message=message,
            room_name=self._call_session_data.room_name,
            phone=self._call_session_data.recipient_data.phone,
        ).info("Ending call from exit node")

        self._record_call_termination(termination_actor, termination_reason)
        for event in node.events or []:
            event_extra = self._build_exit_event_extra(event)
            if event_extra:
                self.session.history.add_message(
                    role="developer",
                    content="",
                    extra=event_extra,
                )

        self.session.shutdown()

    @staticmethod
    def _build_exit_event_extra(event: dict[str, Any]) -> dict[str, Any] | None:
        event_type = event.get("type")
        if not event_type:
            return None

        status = next(
            (
                value
                for key, value in event.items()
                if str(key).lower() == "status" and value is not None
            ),
            None,
        )
        details = {
            str(key).lower(): value
            for key, value in event.items()
            if str(key).lower() not in {"type", "status"} and value is not None
        }
        return agent_event_extra(
            str(event_type),
            status=str(status) if status is not None else None,
            details=details,
        )

    async def _handle_missing_node(self, node_id: str) -> None:
        """플로우에서 노드를 찾지 못한 경우 안내 후 통화 종료"""
        error_message = t("system.error", self._language)
        recipient_phone = (
            self._call_session_data.recipient_data.phone
            if self._call_session_data.recipient_data
            else None
        )

        logger.bind(
            event="MISSING_FLOW_NODE",
            node_id=node_id,
            room_name=self._call_session_data.room_name,
            phone=recipient_phone,
        ).warning("Node not found; ending call")

        self.session.input.set_audio_enabled(False)

        await self._say_and_wait(
            error_message,
            failure_log=logger.bind(
                event="MISSING_FLOW_NODE",
                node_id=node_id,
                room_name=self._call_session_data.room_name,
                phone=recipient_phone,
            ),
            failure_message="Error waiting for missing-node error message",
        )

        self._record_call_termination("SYSTEM", "MISSING_FLOW_NODE")
        self.session.shutdown()

    async def _handle_action_node(self, node: FlowNode) -> Optional[str]:
        """액션 노드 처리 - 상담원 연결 등 액션 수행"""
        action_type = node.action_type
        action_config = node.action_config or {}

        logger.debug(f"Executing action: {action_type}")

        if action_type == "transfer":
            # 상담원 연결
            transfer_to = action_config.get("phone_number") or None
            logger.bind(
                event="ACTION_NODE_TRANSFER",
                transfer_to=transfer_to,
            ).debug("Would transfer to human agent")

            await self._handle_transfer_call(
                call_session_data=self._call_session_data,
                transfer_to=transfer_to,
            )

        elif action_type == "transfer_direct":
            # 상담원 직접 연결
            logger.bind(
                room_name=self._call_session_data.room_name,
                phone=self._call_session_data.recipient_data.phone,
                action_type=action_type,
                action_config=action_config,
            ).debug("Would transfer to human agent directly")

            transfer_to = action_config.get("phone_number", "")
            if not transfer_to:
                logger.warning("No phone number found in action_config")
                return node.next

            transfer_mode = action_config.get("transfer_mode", "warm_transfer")

            logger.bind(
                room_name=self._call_session_data.room_name,
                phone=self._call_session_data.recipient_data.phone,
                transfer_mode=transfer_mode,
                transfer_to=transfer_to,
            ).debug("Transfer mode")

            await self._dispatch_transfer(
                call_session_data=self._call_session_data,
                transfer_to=transfer_to,
                transfer_mode=transfer_mode,
                direct=True,
            )

            return None

        elif action_type == "leave_message":
            # 인사말 없이 곧바로 상담 메모 남기기로 진입한다.
            logger.bind(
                event="ACTION_NODE_LEAVE_MESSAGE",
                room_name=self._call_session_data.room_name,
            ).debug("Entering leave message directly from flow")

            # 선택: 메모 오프너 앞에 재생할 안내 멘트(예: 센터명 안내).
            if node.message and node.message.strip():
                await self._say_and_wait(
                    node.message,
                    failure_log=logger.bind(event="ACTION_NODE_LEAVE_MESSAGE"),
                    failure_message="Error waiting for leave-message intro playout",
                )

            # NOTE: 이 경로는 녹음 고지(greeting.recording_notice)를 재생하지 않는다.
            if self._call_session_data.caller_disconnected:
                self._record_call_termination("USER", "CALLER_DISCONNECTED")
                self.session.shutdown(drain=False)
                return None

            # _handle_leave_message 가 메모 수집 후 통화를 종료한다.
            await self._handle_leave_message(
                call_session_data=self._call_session_data,
                is_after_transfer_failure=False,
                entry_reason="memo_only",
            )
            return None

        elif action_type == "log":
            # 로그 기록
            log_message = action_config.get("message", "")
            logger.debug(f"Action log: {log_message}")

        else:
            logger.warning(f"Unknown action type: {action_type}")

        return node.next

    def _get_field_value(self, field: str) -> Any:
        """필드 값 조회 - business_data, recipient_data 등에서 값 조회"""
        call_data = self._call_session_data

        for section in ("business_data", "recipient_data", "call_config_data"):
            prefix = f"{section}."
            if field.startswith(prefix):
                sub_field = field.replace(prefix, "")
                section_data = getattr(call_data, section)
                if section_data:
                    return getattr(section_data, sub_field, None)
                return None

        logger.warning(f"Unknown field: {field}")
        return None
