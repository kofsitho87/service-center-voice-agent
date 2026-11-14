from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from livekit.agents import AgentTask, function_tool
from livekit.agents.llm.chat_context import ChatContext
from loguru import logger

from tasks.booking.exit_reasons import (
    BookingExitRouteTarget,
    BookingExitRouteTargetInput,
    ExitReasonCode,
    exit_reason_text,
    normalize_exit_reason_code,
    route_target_for_exit,
)
from utils.agent_event import agent_event_extra
from utils.common import load_prompt
from utils.i18n import language_display_name, t
from utils.korean_speech_guard import KoreanSpeechGuardMixin
from utils.spoken_datetime import spoken_date_or_datetime, spoken_time


@dataclass
class SelectReservationResult:
    """Selected reservation from the list."""

    selected_reservation: dict[str, Any] | None = None
    exited: bool = False
    exit_reason: str | None = None
    exit_reason_code: str | None = None
    route_target: BookingExitRouteTarget | None = None


def _spoken_time(hour: int, minute: int, language: str | None = None) -> str:
    """시각을 전화 안내용 구어체로 변환 (언어 분기).

    ko-KR: '오전 9시', '오후 2시 30분' / 그 외: '9 AM', '2:30 PM'
    """
    return spoken_time(datetime(2000, 1, 1, hour, minute), language)


# Convert a reservation datetime ('YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD') to the
# spoken form without the year; unparseable text is returned unchanged.
_format_spoken_date = spoken_date_or_datetime


class SelectReservationTask(KoreanSpeechGuardMixin, AgentTask[SelectReservationResult]):
    """
    예약 내역 리스트에서 변경/취소할 예약을 선택하는 Task.

    - 예약이 1건이면 해당 예약을 안내하고 확인 후 선택
    - 여러 건이면 목록을 읽어주고 사용자가 선택
    """

    # Set once the task has completed; every tool becomes a no-op afterwards.
    _finalized: bool = False

    def __init__(
        self,
        *,
        reservations: list[dict[str, Any]],
        action: str = "변경",  # "변경" or "취소"
        language_code: str = "ko-KR",
        chat_ctx: ChatContext | None = None,
    ) -> None:
        if not reservations:
            raise ValueError("reservations list cannot be empty")

        self._reservations = reservations
        self._action = action
        self._language_code = language_code
        # 발화용(통화 언어) 액션 단어 ("변경"→통화 언어로, "취소"→통화 언어로).
        # generate_reply/tool 반환 등 콜러에게 들리는 멘트(t() 호출)에 쓰인다.
        self._action_word = t(
            "select_reservation.action.cancel"
            if action == "취소"
            else "select_reservation.action.modify",
            language_code,
        )
        # 시스템 프롬프트(영어)용 액션 단어. 프롬프트가 영어로 작성되어 있으므로
        # 항상 영어 동사를 넣는다.
        self._action_en = "cancel" if action == "취소" else "change"
        # 사용자가 번호를 발화하면 즉시 확정하지 않고 임시 보관 후 재확인한다.
        # (STT 숫자 오인식으로 인한 오선택/오취소 방지)
        # 예약이 1건일 때는 고를 번호가 없어 오인식될 대상 자체가 없으므로
        # 이 재확인 게이트를 쓰지 않는다. (select() 참고)
        self._pending_index: int | None = None
        # complete() 는 한 번만 호출돼야 한다. LLM 이 한 턴에 select() 와 confirm()
        # 을 같이 호출하는 경우를 대비한 가드.
        self._finalized = False

        reservation_list = self._format_reservation_list()

        super().__init__(
            instructions=load_prompt(
                "select_reservation_prompt.yaml",
                language_code=language_code,
                default_language=language_display_name(language_code),
                action=self._action_en,
                reservation_list=reservation_list,
            ),
            chat_ctx=chat_ctx,
        )

    def _format_reservation_list(self) -> str:
        """Format reservations as numbered list with appointment_date, service_name, content."""
        lines = []
        for idx, res in enumerate(self._reservations, start=1):
            date_raw = res.get("appointment_date", "")

            spoken_date = (
                _format_spoken_date(date_raw, self._language_code)
                if date_raw
                else t("common.date_undecided", self._language_code)
            )

            lines.append(f"{idx}. {spoken_date}")

        return "\n".join(lines)

    def _spoken_reservation(self, res: dict[str, Any]) -> str:
        """Build a short spoken description of a reservation for confirmation."""
        date_raw = res.get("appointment_date", "")
        service = res.get("service_name", "")
        spoken_date = (
            _format_spoken_date(date_raw, self._language_code)
            if date_raw
            else t("common.date_undecided", self._language_code)
        )
        parts = [spoken_date]
        if service:
            parts.append(service)
        return ", ".join(parts)

    def _announce_list_reply(self) -> None:
        """Read out the reservation list and ask which one to act on."""
        self.session.generate_reply(
            tool_choice="none",
            instructions=t(
                "select_reservation.announce_list",
                self._language_code,
                action=self._action_word,
                reservation_list=self._format_reservation_list(),
            ),
            allow_interruptions=False,
        )

    async def on_enter(self) -> None:
        """Generate initial reply asking the user to choose a reservation."""
        self.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "SELECT_RESERVATION_TASK",
                status="ON_ENTER",
                details={
                    "action": self._action,
                    "count": len(self._reservations),
                },
            ),
        )

        if len(self._reservations) == 1:
            # Single reservation: read it out and ask for confirmation
            res = self._reservations[0]
            date_raw = res.get("appointment_date", "")
            service = res.get("service_name", "")
            content = res.get("content", "")
            spoken_date = (
                _format_spoken_date(date_raw, self._language_code)
                if date_raw
                else t("common.date_undecided", self._language_code)
            )

            self.session.generate_reply(
                tool_choice="none",
                instructions=t(
                    "select_reservation.single_confirm",
                    self._language_code,
                    spoken_date=spoken_date,
                    service=service,
                    content=content,
                    action=self._action_word,
                ),
                allow_interruptions=False,
            )
        else:
            # Multiple reservations: read the list and ask which one
            self._announce_list_reply()

        logger.bind(
            task="SELECT_RESERVATION_TASK",
            stage="enter",
            action=self._action,
            count=len(self._reservations),
        ).debug("SelectReservationTask on_enter")

    def _finalize(self, index: int) -> None:
        """선택된 예약을 확정하고 Task를 완료한다.

        예약 1건(즉시 확정)과 여러 건(재확인 후 확정)이 공유하는 종료 경로.
        RESERVATION_SELECTED 단계 이벤트는 여기서만 발생시킨다.
        """
        selected = self._reservations[index - 1]

        # 선택된 예약 row의 customer_no를 고객 정보에 반영한다.
        selected_customer_no = selected.get("customer_no")
        self.session.userdata.recipient_data.customer_no = selected_customer_no

        logger.bind(
            task="SELECT_RESERVATION_TASK",
            stage="confirm",
            index=index,
            reservation_id=selected.get("reservation_id"),
            customer_no=selected_customer_no,
        ).debug("Reservation selection confirmed")

        # 예약변경·예약취소가 공유하는 RESERVATION_SELECTED 단계.
        # ON_ENTER(제시)와 나눠야 '목록은 들었는데 못 고르고 이탈' 이 보인다.
        self.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "SELECT_RESERVATION_TASK",
                status="SELECTED",
                details={
                    "action": self._action,
                    "reservation_id": selected.get("reservation_id"),
                },
            ),
        )

        self._finalized = True
        self.complete(SelectReservationResult(selected_reservation=selected))

    @function_tool
    async def select(self, index: int) -> str | None:
        """
        사용자가 지정한 예약을 선택합니다.

        예약이 여러 건이면 즉시 확정하지 않고, 확인 질문을 문자열로 돌려줍니다.
        이 도구는 발화하지 않으므로 반환된 문장을 그대로 소리내어 말해야 합니다.
        (STT 숫자 오인식으로 인한 오선택/오취소 방지)
        예약이 1건이면 on_enter 안내에서 이미 확인을 받았으므로 곧바로 확정합니다.

        Args:
            index: 예약 번호 (1-based)

        Returns:
            str | None: 그대로 발화해야 하는 재확인 질문 또는 오류 안내.
                즉시 확정된 경우에만 None.
        """
        if self._finalized:
            return None

        logger.bind(
            task="SELECT_RESERVATION_TASK",
            stage="select",
            index=index,
        ).debug("select called (pending confirmation)")

        if index < 1 or index > len(self._reservations):
            logger.bind(
                task="SELECT_RESERVATION_TASK",
                stage="select",
                index=index,
                error="index out of range",
            ).warning("Invalid reservation index")

            return t(
                "select_reservation.not_found",
                self._language_code,
                action=self._action_word,
            )

        # 예약이 1건이면 on_enter 가 이미 '이 예약을 {action}할까요?' 로 확인을 받았다.
        # 재확인 게이트는 발화된 번호/날짜의 STT 오인식을 막기 위한 것이라, 후보가
        # 하나뿐이면 오인식될 대상이 없어 같은 질문을 두 번 하는 것 외에 얻는 게 없다.
        if len(self._reservations) == 1:
            logger.bind(
                task="SELECT_RESERVATION_TASK",
                stage="select",
                index=index,
                single_reservation=True,
            ).debug("Single reservation: finalizing without re-confirmation")
            self._pending_index = index
            self._finalize(index)
            return None

        # 즉시 확정하지 않고 임시 보관 후 재확인 (STT 숫자 오인식 방지)
        self._pending_index = index
        pending = self._reservations[index - 1]
        spoken = self._spoken_reservation(pending)

        logger.bind(
            task="SELECT_RESERVATION_TASK",
            stage="select",
            index=index,
            reservation_id=pending.get("reservation_id"),
        ).debug("Reservation pending confirmation")

        return t(
            "select_reservation.confirm_pending",
            self._language_code,
            spoken=spoken,
            action=self._action_word,
        )

    @function_tool
    async def confirm(self) -> str | None:
        """
        재확인 단계에서 사용자가 긍정('네', '맞아요' 등)했을 때 호출합니다.
        임시 선택된 예약을 최종 확정합니다.

        예약이 1건이면 select() 가 이미 확정하므로 호출할 필요가 없습니다.

        Returns:
            str | None: 확인 대상이 없을 때만 안내 문구를 돌려주며, 이 문자열은
                그대로 발화해야 합니다. 정상 확정 시에는 None.
        """
        if self._finalized:
            return None

        if self._pending_index is None:
            # 확인 대상이 없으면 다시 번호를 받도록 안내
            logger.bind(
                task="SELECT_RESERVATION_TASK",
                stage="confirm",
                error="no pending selection",
            ).warning("confirm called without pending selection")
            return t(
                "select_reservation.no_pending",
                self._language_code,
                action=self._action_word,
            )

        self._finalize(self._pending_index)
        return None

    @function_tool
    async def reselect(self) -> str | None:
        """
        재확인 단계에서 사용자가 부정('아니요', '틀려요')하거나 다른 예약을
        선택하려 할 때 호출합니다. 임시 선택을 취소하고 예약 목록을 다시 안내합니다.
        """
        if self._finalized:
            return None

        logger.bind(
            task="SELECT_RESERVATION_TASK",
            stage="reselect",
            prev_index=self._pending_index,
        ).debug("User requested to reselect reservation")

        self._pending_index = None

        self.session.generate_reply(
            tool_choice="none",
            instructions=t(
                "select_reservation.reselect_announce",
                self._language_code,
                action=self._action_word,
                reservation_list=self._format_reservation_list(),
            ),
        )
        return None

    @function_tool
    async def exit_task(
        self,
        reason_code: ExitReasonCode = "other",
        detail: str = "",
        route_target: BookingExitRouteTargetInput = "other",
    ) -> None:
        """
        사용자가 예약 선택을 중단하려 할 때 호출합니다.

        Args:
            reason_code: 구조화된 종료 사유. 반드시 다음 중 하나만 사용하세요:
                - "staff_connection": 상담원(사람) 연결 요청
                - "cancel": 예약 변경/취소 진행 중단
                - "unrelated": 예약 선택과 무관한 요청(예: 서비스센터 정보 문의)
                - "other": 위에 해당하지 않는 그 외
            detail: (선택) 사용자가 실제로 말한 요청 내용. 절대 지어내지 말고,
                사용자 발화에 근거가 있을 때만 그대로 요약해 채우세요. 없으면 빈 문자열.
            route_target: unrelated/other일 때 다음 요청을 booking_inquiry,
                booking_modify, booking_cancel, info_request, end_call, other 중
                하나로 분류합니다. 그 외 reason_code에서는 생략합니다.
        """
        if self._finalized:
            return None

        raw_reason_code = reason_code
        normalized_target = route_target_for_exit(raw_reason_code, route_target)
        reason_code = normalize_exit_reason_code(raw_reason_code)
        reason = exit_reason_text(reason_code, detail, self._language_code)

        logger.bind(
            task="SELECT_RESERVATION_TASK",
            stage="exit",
            reason_code=reason_code,
            reason=reason,
        ).debug("User requested to exit reservation selection")

        self._finalized = True
        self.complete(
            SelectReservationResult(
                exited=True,
                exit_reason=reason,
                exit_reason_code=reason_code,
                route_target=normalized_target,
            )
        )

        return None
