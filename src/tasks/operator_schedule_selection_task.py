from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from livekit.agents import function_tool
from livekit.agents.llm.chat_context import ChatContext
from loguru import logger

from data import RunContext_T
from external_apis.booking_api import (
    get_operator_available_times as api_get_operator_available_times,
)
from tasks.schedule_selection_base import (
    DEFAULT_LOOKUP_DAYS,
    PERIOD_HOUR_RANGES,
    BaseScheduleSelectionTask,
    ScheduleSelectionResult,
    _format_spoken_datetime_korean,
    _format_spoken_time_korean,
    _parse_time_to_hhmm,
    _schedule_lookup_range,
    _spoken_date_with_day,
    _tool_response,
    _try_parse_yyyy_mm_dd,
)
from utils.agent_event import agent_event_extra, record_api_result
from utils.common import load_prompt
from utils.i18n import t

_WEEKDAY_NAMES = {
    name: weekday
    for weekday, names in enumerate(
        (
            ("mon", "monday", "월", "월요일"),
            ("tue", "tuesday", "화", "화요일"),
            ("wed", "wednesday", "수", "수요일"),
            ("thu", "thursday", "목", "목요일"),
            ("fri", "friday", "금", "금요일"),
            ("sat", "saturday", "토", "토요일"),
            ("sun", "sunday", "일", "일요일"),
        )
    )
    for name in names
}


def _operator_times_to_schedules(
    available_times: list[str], operator_id: int
) -> list[dict[str, Any]]:
    """Convert ISO datetime strings (already KST) to schedule dicts.

    Each ISO string like "2026-04-09T09:00:00.000Z" is treated as KST (no tz
    conversion) and mapped to {"time": "2026-04-09 09:00:00", "advisor_id": id}.
    Unparseable strings are skipped.
    """
    schedules: list[dict[str, Any]] = []
    for iso_str in available_times:
        try:
            naive_str = iso_str.replace("Z", "").split("+")[0]
            dt = datetime.fromisoformat(naive_str)
            schedules.append(
                {"time": dt.strftime("%Y-%m-%d %H:%M:%S"), "advisor_id": operator_id}
            )
        except Exception:
            logger.bind(
                task="OPERATOR_SCHEDULE_SELECTION_TASK", iso_str=iso_str
            ).warning("Failed to parse ISO time string")
            continue
    return schedules


class OperatorScheduleSelectionTask(BaseScheduleSelectionTask):
    """예약변경 시간 선택 (상담사 기준 가용 시간 조회).

    ScheduleSelectionTask와 동일한 UX이지만 operator_id 기반으로
    get_operator_available_times API를 호출한다.
    """

    _TASK_TAG = "OPERATOR_SCHEDULE_SELECTION_TASK"

    def __init__(
        self,
        *,
        operator_id: int,
        reservation_data: dict[str, Any],
        lookup_days: int = DEFAULT_LOOKUP_DAYS,
        service_name: str | None = None,
        action: Literal["modify", "create"] = "modify",
        language_code: str = "ko-KR",
        chat_ctx: ChatContext | None = None,
        search_start_date: str | None = None,
        search_end_date: str | None = None,
    ) -> None:
        self._operator_id = operator_id
        self._modify_flow = action == "modify"
        self._loaded_start_date: str | None = None
        self._loaded_end_date: str | None = None

        action_kr = "변경" if action == "modify" else "신청"
        action_en = "change" if action == "modify" else "booking"
        if search_start_date is None or search_end_date is None:
            search_start_date, search_end_date = _schedule_lookup_range(
                lookup_days, language_code
            )

        service_hint = (
            t(
                "operator_schedule.service_hint",
                language_code,
                service=service_name,
            )
            if service_name and service_name.strip()
            else ""
        )
        instructions = load_prompt(
            "operator_schedule_selection_task_prompt.yaml",
            language_code=language_code,
            action_kr=action_kr,
            action_en=action_en,
            service_hint=service_hint,
            booking_start_date=search_start_date,
            booking_end_date=search_end_date,
            lookup_days=lookup_days,
        )
        if self._modify_flow:
            instructions += (
                "\nFor appointment modification, the opening question is already spoken "
                "by on_enter. Do not claim availability until search_preferred_schedules "
                "returns successfully. On the caller's first answer, call "
                "search_preferred_schedules with every stated date, weekday, and time "
                "condition. All fields are optional; for a single date use the same "
                "start_date and end_date. Resolve relative dates from Current time. "
                "Weekdays are names: mon, tue, wed, thu, fri, sat, sun. Use period morning, "
                "afternoon, or evening and 24-hour HH:MM bounds. If the caller says "
                "anytime, call it without conditions. Read only user_message from the "
                "tool response. If it returns lookup_required, call this tool first. "
                "If it finds no match, ask for a different preference and call this "
                "tool again. Do not ask the caller to repeat an exact date already given."
                " If the caller later changes their date or time preference, call "
                "search_preferred_schedules again so a range outside the cached API "
                "response is queried. Use other schedule tools only to choose or "
                "inspect options already returned by the latest lookup."
                " If search_preferred_schedules returns response_type "
                "pending_confirmation, the caller's exact time is already staged: "
                "read only its user_message and, on an affirmative answer, call "
                "confirm_pick. Never ask your own confirmation question for a time "
                "that is not staged; to finalize any other offered time, call pick first."
            )
        super().__init__(
            instructions=instructions,
            reservation_data=reservation_data,
            action_kr=action_kr,
            lookup_days=lookup_days,
            language_code=language_code,
            chat_ctx=chat_ctx,
            allow_interruptions=True,
            search_start_date=search_start_date,
            search_end_date=search_end_date,
        )
        if self._modify_flow:
            self._availability_loaded = False

    async def on_enter(self) -> None:
        if not self._modify_flow:
            await super().on_enter()
            return
        self.session.history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(self._TASK_TAG, status="ON_ENTER"),
        )
        message = (
            "어떤 날짜로 변경 원하세요? 편하신 요일이나 시간대 있으시면 같이 말씀해주세요"
            if self._language_code == "ko-KR"
            else "What date would you like to change to? Please also tell me any "
            "preferred weekday or time of day."
        )
        self.session.say(message, allow_interruptions=False)

    @function_tool
    async def search_preferred_schedules(
        self,
        ctx: RunContext_T,
        start_date: str = "",
        end_date: str = "",
        weekdays: list[str] | None = None,
        period: str = "",
        after_time: str = "",
        before_time: str = "",
    ) -> dict[str, Any] | None:
        """Look up availability after the caller states preferred date/day/time.

        Dates use YYYY-MM-DD; for one date pass it as start_date and end_date.
        Weekdays are names: mon, tue, wed, thu, fri, sat, sun (case-insensitive).
        Full English names and Korean forms such as 월/월요일 are also accepted.
        For a single date, the date takes precedence over weekdays.
        Period is morning, afternoon, or evening. Times use 24-hour HH:MM.
        Omit conditions not stated.
        """
        start = _try_parse_yyyy_mm_dd(start_date) if start_date else None
        end = _try_parse_yyyy_mm_dd(end_date) if end_date else start
        if (start_date and not start) or (end_date and not end):
            return self._preference_error("date")
        correction = None
        if start and end:
            start, end, correction = self._apply_caller_date_hint(start, end)
        correction_fields = {"date_corrected": correction} if correction else {}
        if (start and not self._is_date_inside_cached_range(start)) or (
            end and not self._is_date_inside_cached_range(end)
        ):
            return {**self._preference_error("out_of_range"), **correction_fields}
        if start and end and end < start:
            return {**self._preference_error("date_range"), **correction_fields}
        weekday_numbers = set()
        for day in weekdays or []:
            weekday = _WEEKDAY_NAMES.get(day.lower()) if isinstance(day, str) else None
            if weekday is None:
                return {**self._preference_error("weekday"), **correction_fields}
            weekday_numbers.add(weekday)
        exact_date = bool(start and start == end)
        if (
            exact_date
            and weekday_numbers
            and datetime.strptime(start, "%Y-%m-%d").weekday() not in weekday_numbers
        ):
            logger.bind(
                task=self._TASK_TAG,
                stage="weekday_mismatch",
                date=start,
                weekdays=weekdays,
            ).warning("Ignoring weekday preference that contradicts the exact date")
        if period and period not in PERIOD_HOUR_RANGES:
            return {**self._preference_error("period"), **correction_fields}

        after_hhmm = _parse_time_to_hhmm(after_time) if after_time else None
        before_hhmm = _parse_time_to_hhmm(before_time) if before_time else None
        if (after_time and not after_hhmm) or (before_time and not before_hhmm):
            return {**self._preference_error("time"), **correction_fields}
        if after_hhmm and before_hhmm and after_hhmm > before_hhmm:
            return {**self._preference_error("time_range"), **correction_fields}

        self._reservation_data.pop("reservation_date", None)
        self._pending_pick = None
        self._stage = "show_dates"
        self._last_offered_slots = []

        query_start = start or self._search_start_date
        query_end = end or self._search_end_date
        if (
            not self._availability_loaded
            or self._loaded_start_date is None
            or self._loaded_end_date is None
            or query_start < self._loaded_start_date
            or query_end > self._loaded_end_date
        ):
            response = await self._request_operator_times(query_start, query_end)
            if not response.success or response.data is None:
                self.complete(
                    ScheduleSelectionResult(
                        lookup_failed=True,
                        lookup_failure_message=response.message,
                    )
                )
                return None
            self._schedules = _operator_times_to_schedules(
                list(response.data.availableTimes), self._operator_id
            )
            self._availability_loaded = True
            self._loaded_start_date = query_start
            self._loaded_end_date = query_end

        slots = self._slots_matching_time_condition(
            period=period or None,
            after_hhmm=after_hhmm,
            before_hhmm=before_hhmm,
            start_date=start,
            end_date=end,
        )
        if weekday_numbers and not exact_date:
            slots = [slot for slot in slots if slot.weekday() in weekday_numbers]
        self._last_offered_slots = [
            slot.strftime("%Y-%m-%d %H:%M:%S") for slot in slots
        ]
        lang = self._language_code
        if not slots:
            if lang == "ko-KR":
                if self._schedules:
                    message = (
                        "말씀하신 시간에 맞는 예약 가능 일정이 없습니다. "
                        "혹시 다른 편하신 날짜나 시간대가 있으실까요?"
                    )
                elif exact_date:
                    # message = (
                    #     f"{_spoken_date_with_day(start, lang)}에는 변경 가능한 시간이 "
                    #     "없습니다. 다른 날짜를 말씀해 주세요."
                    # )
                    message = "아쉽게도 말씀하신 시간은 이미 예약이 차 있네요. 혹시 다른 편하신 날짜나 시간대가 있으실까요?"
                else:
                    message = (
                        "말씀하신 시간에는 변경 가능한 일정이 없습니다. "
                        "다른 날짜나 시간대를 말씀해 주세요."
                    )
            else:
                message = (
                    "No appointments match that preference. "
                    "What other date or time works?"
                )
            return _tool_response(
                status="unavailable",
                response_type="preferred_schedule_unavailable",
                user_message=message,
                next_action="ask_preference",
                **correction_fields,
            )

        if exact_date and after_hhmm and before_hhmm and after_hhmm == before_hhmm:
            raw, advisor_id = self._find_slot_for(start, after_hhmm)
            if (
                raw
                and advisor_id is not None
                and raw in [slot.strftime("%Y-%m-%d %H:%M:%S") for slot in slots]
            ):
                response = self._stage_pending_pick(start, after_hhmm, raw, advisor_id)
                response.update(correction_fields)
                return response

        if exact_date:
            self._reservation_data["reservation_date"] = start
            self._stage = "select_time"
            times = [_format_spoken_time_korean(slot, lang) for slot in slots[:3]]
            spoken_date = _spoken_date_with_day(start, lang)
            message = (
                f"{spoken_date}에는 {', '.join(times)} 가능합니다. 어느 시간이 편하세요?"
                if lang == "ko-KR"
                else f"Available times on {spoken_date}: {', '.join(times)}. "
                "Which time works for you?"
            )
        else:
            offered = [
                _format_spoken_datetime_korean(slot.strftime("%Y-%m-%d %H:%M:%S"), lang)
                for slot in slots[:3]
            ]
            message = (
                f"{', '.join(offered)} 가능합니다. 어느 날짜와 시간이 편하세요?"
                if lang == "ko-KR"
                else f"Matching options: {', '.join(offered)}. "
                "Which date and time works for you?"
            )
        return _tool_response(
            status="ok",
            response_type="preferred_schedules",
            user_message=message,
            next_action="ask_time" if exact_date else "ask_date_and_time",
            available_slots=[slot.strftime("%Y-%m-%d %H:%M:%S") for slot in slots],
            **correction_fields,
        )

    def _preference_error(self, reason: str) -> dict[str, Any]:
        messages = {
            "date": (
                "원하시는 날짜를 다시 말씀해 주세요.",
                "Please clarify the preferred date.",
            ),
            "out_of_range": (
                "예약 가능한 조회 기간 밖의 날짜입니다. 다른 날짜를 말씀해 주세요.",
                "That date is outside the booking period. Please choose another date.",
            ),
            "date_range": (
                "원하시는 날짜 범위를 다시 말씀해 주세요.",
                "Please clarify the date range.",
            ),
            "weekday": (
                "편하신 요일을 다시 말씀해 주세요.",
                "Please clarify the weekday.",
            ),
            "period": (
                "편하신 시간대를 다시 말씀해 주세요.",
                "Please clarify the time of day.",
            ),
            "time": ("편하신 시간을 다시 말씀해 주세요.", "Please clarify the time."),
            "time_range": (
                "편하신 시간 범위를 다시 말씀해 주세요.",
                "Please clarify the time range.",
            ),
        }
        return _tool_response(
            status="invalid_preference",
            response_type="invalid_preference",
            user_message=messages[reason][0 if self._language_code == "ko-KR" else 1],
            next_action="ask_preference",
        )

    async def _request_operator_times(self, start_date: str, end_date: str) -> Any:
        response = await api_get_operator_available_times(
            operator_id=self._operator_id,
            start_date=start_date,
            end_date=end_date,
            client_id=self.session.userdata.business_data.client_id,
        )
        record_api_result(
            self.session.history,
            "GET_OPERATOR_AVAILABLE_TIMES",
            response,
        )
        return response

    async def _fetch_schedules(self, start_date: str, end_date: str) -> list[Any]:
        response = await self._request_operator_times(start_date, end_date)
        raw_times = (
            response.data.availableTimes if response.success and response.data else []
        )
        return _operator_times_to_schedules(raw_times, self._operator_id)
