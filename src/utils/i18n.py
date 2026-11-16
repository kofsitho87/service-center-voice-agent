"""언어별 i18n 헬퍼 및 고정멘트 중앙 레지스트리.

이 모듈은 두 가지 역할을 한다.

1. 언어 메타 데이터: ``language_display_name`` / ``WEEKDAY_NAMES_BY_LANGUAGE``
   (프롬프트의 ``{default_language}`` 치환, 프롬프트 날짜 변수 등에 사용)
2. 고정멘트 레지스트리: ``MESSAGES`` + ``t(key, lang)``
   코드에서 ``session.say()`` 등으로 직접 발화하거나 tool 결과로 LLM에 전달되는
   짧은 한국어/영어 문자열을 key로 조회한다. (긴 LLM 프롬프트는 src/prompts/*.yaml)

사용 예:
    from utils.i18n import t

    lang = call_session_data.call_config_data.language
    self.session.say(t("system.error", lang))

키 네임스페이스 컨벤션: "<영역>.<용도>"
    system.* / greeting.* / dtmf.* / transfer.* / end_call.*
    (이후 booking.*, schedule.*, leave_message.*, info.*, warm_transfer.* 등으로 확장)
"""

from typing import Optional

# 언어 메타 헬퍼의 기본값(영어 표기/ISO 코드 fallback에 사용)
DEFAULT_LANG = "en-US"

# 고정멘트 레지스트리 fallback 언어. 모든 메시지는 최소한 이 언어를 반드시 정의한다.
# (현재 서비스가 한국어 우선이므로 누락 언어는 한국어로 안전하게 폴백)
DEFAULT_MESSAGE_LANGUAGE = "ko-KR"

# Spoken display name per language tag, used to fill `{default_language}` style
# prompt directives (mirrors CallConfigData.get_language_prompt_string).
LANGUAGE_DISPLAY_NAMES = {
    "ko-KR": "한국어",
    "en-US": "English",
}


def language_display_name(lang: Optional[str]) -> str:
    """Return the spoken language name for ``lang`` (falls back to English)."""
    return LANGUAGE_DISPLAY_NAMES.get(lang or DEFAULT_LANG, "English")


# Weekday names per language tag, Monday first (index == date.weekday()).
# Used for the prompt date variables in ``utils.common.load_prompt``.
WEEKDAY_NAMES_BY_LANGUAGE: dict[str, tuple[str, ...]] = {
    "ko-KR": ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"),
    "en-US": (
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ),
}


# key -> language(BCP 47) -> message
MESSAGES: dict[str, dict[str, str]] = {
    # --- single_agent.py ---
    "system.config_error": {
        "ko-KR": "죄송합니다. 시스템 설정 오류가 발생했습니다. 잠시 후 다시 전화 부탁드립니다.",
        "en-US": "We're sorry. A system configuration error occurred. Please try calling again shortly.",
    },
    "system.error": {
        "ko-KR": "죄송합니다. 시스템 오류가 발생했습니다. 잠시 후 다시 전화 부탁드립니다.",
        "en-US": "We're sorry. A system error occurred. Please try calling again shortly.",
    },
    "greeting.recording_notice": {
        "ko-KR": "본 통화는 AI 응대로 진행되며, 통화 내용은 녹음됩니다.",
        "en-US": "This call is handled by an AI assistant, and the conversation will be recorded.",
    },
    "dtmf.timeout_exit": {
        "ko-KR": "올바른 입력이 확인되지 않아 통화를 종료합니다.",
        "en-US": "No valid input was received, so the call will now end.",
    },
    # 키패드 옵션 end_call: true 일 때 통화 종료 직전 멘트
    "dtmf.end_call_goodbye": {
        "ko-KR": "통화를 종료하겠습니다.",
        "en-US": "We'll end the call now.",
    },
    # --- base_agent.py ---
    "transfer.failed": {
        "ko-KR": "죄송합니다. 상담원 연결에 실패했습니다.",
        "en-US": "We're sorry. We were unable to connect you with an agent.",
    },
    "transfer.reason.question": {
        "ko-KR": "어떤 내용으로 상담을 원하시나요?",
        "en-US": "What would you like to speak with an agent about?",
    },
    "transfer.purpose.booking_create": {
        "ko-KR": "예약 신청 상담",
        "en-US": "Help with a new appointment",
    },
    "transfer.purpose.booking_modify": {
        "ko-KR": "예약 변경 상담",
        "en-US": "Help changing an appointment",
    },
    "transfer.purpose.booking_cancel": {
        "ko-KR": "예약 취소 상담",
        "en-US": "Help cancelling an appointment",
    },
    "transfer.purpose.no_slot_memo": {
        "ko-KR": "요청하신 조건에 맞는 예약 가능 시간 확인",
        "en-US": "Finding an appointment that matches the requested conditions",
    },
    "transfer.purpose.service_not_offered": {
        "ko-KR": "이 전화에서 접수할 수 없는 서비스 항목 확인",
        "en-US": "Reviewing a service that cannot be booked on this call",
    },
    "transfer.purpose.identity_verification_failed": {
        "ko-KR": "예약 업무를 위한 본인 확인 지원",
        "en-US": "Help verifying identity for an appointment request",
    },
    "transfer.purpose.urgent_handoff": {
        "ko-KR": "긴급 상담원 연결",
        "en-US": "Urgent help from an agent",
    },
    "transfer.purpose.reservation_operator_missing": {
        "ko-KR": "예약 처리에 필요한 담당 상담사 정보 확인",
        "en-US": "Reviewing missing advisor information for an appointment",
    },
    "transfer.purpose.operator_schedule_lookup_failed": {
        "ko-KR": "예약 변경을 위한 담당 상담사 일정 확인",
        "en-US": "Checking advisor availability for an appointment change",
    },
    # LeaveMessageTask openers keyed by purpose origin and memo entry reason.
    "leave_message.progress_notice.caller_stated": {
        "ko-KR": "말씀해 주신 내용은 상담원에게 전달해 드리겠습니다. 추가로 전달할 내용이 있으신가요?",
        "en-US": "I'll pass along what you told me to an agent. Is there anything else you'd like to add?",
    },
    "leave_message.progress_notice.caller_exit.caller_stated": {
        "ko-KR": "네, 말씀해 주신 내용은 상담원에게 전달해 드리겠습니다. 추가로 전달할 내용이 있으신가요?",
        "en-US": "Sure. I'll pass along what you told me to an agent. Is there anything else you'd like to add?",
    },
    "leave_message.progress_notice.caller_exit.default": {
        "ko-KR": "네, 상담원에게 전달할 내용을 말씀해 주세요.",
        "en-US": "Sure. Please tell me what you'd like me to pass on to an agent.",
    },
    # --- tasks/single_room_warm_transfer_task.py (상담원에게 들리는 브리핑/DTMF 안내) ---
    "warm_transfer.briefing.intro": {
        "ko-KR": "AI 인바운드 전화입니다.",
        "en-US": "This is an inbound AI call.",
    },
    "warm_transfer.briefing.revisit_customer": {
        "ko-KR": "기존 고객 {name} 고객님께 걸려온 전화입니다.",
        "en-US": "This is a call regarding returning customer {name}.",
    },
    "warm_transfer.briefing.unknown_customer": {
        "ko-KR": "고객님께 걸려온 전화입니다.",
        "en-US": "This is a call from a customer.",
    },
    "warm_transfer.briefing.lead_in": {
        "ko-KR": "고객과 AI와의 통화 내용을 브리핑하겠습니다.",
        "en-US": "Here is a briefing of the conversation between the customer and the AI.",
    },
    "warm_transfer.briefing.dtmf_guide": {
        "ko-KR": "상담이 가능하시면 1번, 고객의 상담 요청 내용을 확인한 뒤 콜백하시려면 2번을 눌러주세요.",
        "en-US": "Press 1 if you can take the call now, or press 2 to review the customer's request and call them back.",
    },
    "warm_transfer.dtmf.invalid": {
        "ko-KR": "유효하지 않은 입력입니다. 상담이 가능하시면 1번, 콜백하시려면 2번을 눌러주세요.",
        "en-US": "That input wasn't valid. Press 1 if you can take the call now, or press 2 to call back.",
    },
    "end_call.goodbye": {
        "ko-KR": "통화를 종료하겠습니다. 좋은 하루 보내세요.",
        "en-US": "We'll end the call now. Have a great day.",
    },
    # --- tasks/greeting_dtmf_task.py ---
    "greeting_dtmf.replay.invalid": {
        "ko-KR": "입력하신 번호가 올바르지 않습니다. 다시 안내드리겠습니다.",
        "en-US": "That input wasn't valid. Let me go over the options again.",
    },
    "greeting_dtmf.replay.timeout": {
        "ko-KR": "입력 시간이 초과되었습니다. 다시 안내드리겠습니다.",
        "en-US": "I didn't get your input in time. Let me go over the options again.",
    },
    "greeting_dtmf.option_item": {
        "ko-KR": "{digit}번: {label}",
        "en-US": "{digit}: {label}",
    },
    "greeting_dtmf.base_instructions": {
        "ko-KR": (
            "당신은 전화 메뉴 시스템의 일부입니다. "
            "사용자에게 안내 메시지를 전달하고 DTMF 키 입력 또는 음성으로 선택을 받습니다.\n"
            "사용자가 유효한 선택을 하면 `select_option` 도구를 호출하세요.\n"
            "유효한 선택지: {options_str}\n"
        ),
        "en-US": (
            "You are part of a phone menu system. "
            "You deliver a guidance message to the caller and receive their choice "
            "via DTMF keypad or by voice.\n"
            "When the caller makes a valid choice, call the `select_option` tool.\n"
            "Valid options: {options_str}\n"
        ),
    },
    # --- tasks/operator_schedule_selection_task.py ---
    "operator_schedule.service_hint": {
        "ko-KR": "(서비스: {service})",
        "en-US": "(Service: {service})",
    },
    # --- tasks/schedule_selection_base.py ---
    "schedule.action.modify": {"ko-KR": "변경", "en-US": "change"},
    "schedule.action.create": {"ko-KR": "신청", "en-US": "book"},
    "schedule.period.morning": {"ko-KR": "오전", "en-US": "morning"},
    "schedule.period.afternoon": {"ko-KR": "오후", "en-US": "afternoon"},
    "schedule.period.evening": {"ko-KR": "저녁", "en-US": "evening"},
    "schedule.label.after": {"ko-KR": "{time} 이후", "en-US": "after {time}"},
    "schedule.label.before": {"ko-KR": "{time} 이전", "en-US": "before {time}"},
    "schedule.label.between": {
        "ko-KR": "{after}에서 {before} 사이",
        "en-US": "between {after} and {before}",
    },
    "schedule.label.fallback": {"ko-KR": "해당 시간대", "en-US": "that time"},
    "schedule.window_range": {"ko-KR": "{a}부터 {b}까지", "en-US": "from {a} to {b}"},
    "schedule.date_range": {"ko-KR": "{a}부터 {b}까지", "en-US": "{a} to {b}"},
    "schedule.earliest_date": {
        "ko-KR": "가장 빠른 날짜는 {rel}{spoken_date} {day_name}입니다. 이 날짜로 가능한 시간을 확인해드릴까요?",
        "en-US": "The earliest available date is {rel}{spoken_date} ({day_name}). Shall I check the available times for that date?",
    },
    "schedule.no_more_dates": {
        "ko-KR": "{action} 가능한 추가 날짜가 없습니다.",
        "en-US": "There are no additional dates available to {action}.",
    },
    "schedule.available_dates_header": {
        "ko-KR": "{action} 가능한 날짜입니다.\n{lines}",
        "en-US": "Here are the dates available to {action}.\n{lines}",
    },
    "schedule.date_line": {
        "ko-KR": "- {spoken_date} ({day_name})",
        "en-US": "- {spoken_date} ({day_name})",
    },
    "schedule.range_no_dates": {
        "ko-KR": "{range_topic} {action} 가능한 날짜가 없습니다. 다른 날짜를 말씀해 주세요.",
        "en-US": "There are no dates available to {action} on {range_topic}. Please tell me another date.",
    },
    "schedule.range_many": {
        "ko-KR": "{range_phrase} 가장 가까운 날짜로 {names}이 가능합니다. 이후에도 {last_spoken}까지 가능한 날짜가 더 있고요. 이 중에 괜찮으신 날이 있으세요? 선호하시는 요일이나 주가 있으면 말씀해 주세요.",
        "en-US": "For {range_phrase}, the nearest available dates are {names}. There are also more dates available up to {last_spoken}. Would any of these work for you? If you have a preferred weekday or week, please let me know.",
    },
    "schedule.range_few": {
        "ko-KR": "{range_phrase} {action} 가능한 날짜는 {names}입니다. 어느 날짜로 해드릴까요?",
        "en-US": "For {range_phrase}, the dates available to {action} are {names}. Which date would you like?",
    },
    "schedule.pm_only": {
        "ko-KR": "이 날은 오후 시간만 가능합니다. ",
        "en-US": "Only afternoon times are available on this day. ",
    },
    "schedule.am_only": {
        "ko-KR": "이 날은 오전 시간만 가능합니다. ",
        "en-US": "Only morning times are available on this day. ",
    },
    "schedule.no_times_for_date": {
        "ko-KR": "{spoken_date}에는 {action} 가능한 시간이 없습니다.",
        "en-US": "There are no times available to {action} on {spoken_date}.",
    },
    "schedule.times_windows": {
        "ko-KR": "{spoken_date}에는 {windows} {action} 가능합니다. {period_note}원하시는 시간을 말씀해 주세요.",
        "en-US": "On {spoken_date}, you can {action} {windows}. {period_note}Please tell me the time you'd like.",
    },
    "schedule.times_list": {
        "ko-KR": "{spoken_date} {action} 가능한 시간입니다. {period_note}\n- 시간: [{times}]\n원하시는 시간을 말씀해 주세요.",
        "en-US": "Here are the times available to {action} on {spoken_date}. {period_note}\n- Times: [{times}]\nPlease tell me the time you'd like.",
    },
    "schedule.on_enter_instruction": {
        "ko-KR": "아래 문장만 사용자에게 자연스럽게 안내하세요. 다른 날짜나 시간을 추가로 언급하지 마세요.\n\n{earliest_text}",
        "en-US": "Guide the user naturally using only the sentence below. Do not mention any additional dates or times.\n\n{earliest_text}",
    },
    "schedule.on_enter_no_dates": {
        "ko-KR": "{start}부터 {end}까지 {action} 가능한 날짜가 없습니다. 다른 요청이 있으면 상담원 연결이나 다른 서비스 확인을 도와드리겠다고 안내하세요.",
        "en-US": "There are no dates available to {action} from {start} to {end}. If they have another request, let them know you can help connect them to an agent or check another service.",
    },
    "schedule.invalid_date": {
        "ko-KR": "해당 날짜를 확인할 수 없습니다. 원하시는 날짜를 다시 말씀해 주세요.",
        "en-US": "I couldn't recognize that date. Please tell me the date you'd like again.",
    },
    "schedule.out_of_range": {
        "ko-KR": "{date}은 예약이 어렵습니다. 지금은 {start}부터 {end}까지만 예약을 받고 있어요. 그 안에서 원하시는 날짜를 말씀해 주세요.",
        "en-US": "{date} is not available for booking. We're currently only taking bookings from {start} to {end}. Please tell me a date within that range.",
    },
    "schedule.unavailable_date": {
        "ko-KR": "{date}에는 {action} 가능한 시간이 없습니다. 다른 날짜를 말씀해 주세요.",
        "en-US": "There are no times available to {action} on {date}. Please tell me another date.",
    },
    "schedule.weekday_answer": {
        "ko-KR": "{spoken_date_with_day}입니다.",
        "en-US": "That's {spoken_date_with_day}.",
    },
    "schedule.invalid_end_date": {
        "ko-KR": "조회 종료 날짜를 확인할 수 없습니다. 원하시는 기간을 다시 말씀해 주세요.",
        "en-US": "I couldn't recognize the end date. Please tell me the period you'd like again.",
    },
    "schedule.invalid_range": {
        "ko-KR": "조회 시작일이 종료일보다 늦습니다. 원하시는 기간을 다시 말씀해 주세요.",
        "en-US": "The start date is later than the end date. Please tell me the period you'd like again.",
    },
    "schedule.range_out_of_range": {
        "ko-KR": "말씀하신 기간에는 예약이 어렵습니다. 지금은 {start}부터 {end}까지만 예약을 받고 있어요. 그 안에서 원하시는 날짜를 말씀해 주세요.",
        "en-US": "Bookings aren't available for the period you mentioned. We're currently only taking bookings from {start} to {end}. Please tell me a date within that range.",
    },
    "schedule.invalid_time_condition": {
        "ko-KR": "원하시는 시간대를 확인하지 못했습니다. 오전, 오후 또는 구체적인 시간을 말씀해 주세요.",
        "en-US": "I couldn't determine the time you'd like. Please say morning, afternoon, or a specific time.",
    },
    "schedule.nearest_suggestion_early": {
        "ko-KR": "가장 이른 시간은 {spoken_date} {spoken_time}입니다. 이 시간으로 안내해 드릴까요?",
        "en-US": "The earliest time is {spoken_time} on {spoken_date}. Would you like this time?",
    },
    "schedule.nearest_suggestion_late": {
        "ko-KR": "가장 늦은 시간은 {spoken_date} {spoken_time}입니다. 이 시간으로 안내해 드릴까요?",
        "en-US": "The latest time is {spoken_time} on {spoken_date}. Would you like this time?",
    },
    "schedule.try_other_time": {
        "ko-KR": "다른 시간대를 말씀해 주세요.",
        "en-US": "Please tell me a different time.",
    },
    "schedule.no_slots_for_condition": {
        "ko-KR": "죄송하지만 조회 가능한 기간 안에는 {label}에 {action} 가능한 시간이 없습니다. {suggestion}",
        "en-US": "Unfortunately, within the available period there are no times to {action} {label}. {suggestion}",
    },
    "schedule.search_earliest": {
        "ko-KR": "가장 빠른 시간은 {rel}{spoken_date} {day_name} {earliest_time}입니다. ",
        "en-US": "The earliest time is {rel}{spoken_date} ({day_name}) at {earliest_time}. ",
    },
    "schedule.search_that_day": {
        "ko-KR": "그날은 {windows} 가능하고요, ",
        "en-US": "That day, you can book {windows}, ",
    },
    "schedule.search_summary": {
        "ko-KR": "{last_spoken}까지 대부분의 날짜에 {label}에 {action} 가능합니다. {spoken_date}로 안내해 드릴까요, 아니면 다른 날짜를 원하세요?",
        "en-US": "Most dates up to {last_spoken} have {label} openings. Would you like to go with {spoken_date}, or a different date?",
    },
    "schedule.times_extra": {"ko-KR": " 외 {extra}개", "en-US": " and {extra} more"},
    "schedule.search_entry": {
        "ko-KR": "{spoken_date} {day_name} {times_text}",
        "en-US": "{spoken_date} ({day_name}) {times_text}",
    },
    "schedule.search_dates_times": {
        "ko-KR": "{label}에 {action} 가능한 날짜와 시간입니다. {entries}. 원하시는 날짜와 시간을 말씀해 주세요.",
        "en-US": "Here are the dates and times available to {action} {label}. {entries}. Please tell me the date and time you'd like.",
    },
    "schedule.missing_date": {
        "ko-KR": "어느 날짜의 시간을 확인해 드릴까요?",
        "en-US": "Which date's times would you like me to check?",
    },
    "schedule.invalid_time_check": {
        "ko-KR": "확인을 원하시는 시간을 다시 한 번 말씀해 주시겠어요?",
        "en-US": "Could you please tell me again the time you'd like me to check?",
    },
    "schedule.time_available": {
        "ko-KR": "네, {spoken_req}도 {action} 가능합니다.",
        "en-US": "Yes, {spoken_req} is also available to {action}.",
    },
    "schedule.time_unavailable": {
        "ko-KR": "{spoken_req}는 현재 {action}이 어렵습니다.",
        "en-US": "{spoken_req} is currently not available to {action}.",
    },
    "schedule.unclear_reask": {
        "ko-KR": "죄송하지만 잘 못 들었어요. 원하시는 시간을 조금만 더 크고 또박또박 다시 말씀해 주시겠어요?",
        "en-US": "Sorry, I didn't catch that. Could you say the time you'd like again, a little louder and more clearly?",
    },
    "schedule.pick_invalid_stage": {
        "ko-KR": "먼저 {action} 가능한 날짜를 선택해 주세요.",
        "en-US": "Please first select a date available to {action}.",
    },
    "schedule.pick_missing_date": {
        "ko-KR": "날짜를 다시 한 번 말씀해 주시겠어요?",
        "en-US": "Could you tell me the date again?",
    },
    "schedule.pick_invalid_time": {
        "ko-KR": "시간을 다시 한 번 말씀해 주실까요? (예: 오전 9시 30분)",
        "en-US": "Could you tell me the time again? (e.g., 9:30 AM)",
    },
    "schedule.pick_nearest": {
        "ko-KR": "{spoken_req}은 {action}이 어렵습니다. 가까운 시간으로 {suggestions}이 가능합니다. 원하시는 시간을 말씀해 주세요.",
        "en-US": "{spoken_req} is not available to {action}. Nearby, {suggestions} are available. Please tell me the time you'd like.",
    },
    "schedule.pick_windows": {
        "ko-KR": "{spoken_req}은 {action}이 어렵습니다. 가능한 시간대는 {windows}입니다. 원하시는 시간을 말씀해 주세요.",
        "en-US": "{spoken_req} is not available to {action}. The available times are {windows}. Please tell me the time you'd like.",
    },
    "schedule.pick_no_times": {
        "ko-KR": "해당 날짜에는 {action} 가능한 시간이 없습니다. 다른 날짜를 선택해 주세요.",
        "en-US": "There are no times available to {action} on that date. Please select another date.",
    },
    "schedule.pick_pending": {
        "ko-KR": "{spoken_datetime}{josa} 예약이 선택되었습니다. 이대로 {action}하시겠습니까?",
        "en-US": "Your appointment is set for {spoken_datetime}. Would you like to {action} it as is?",
    },
    "schedule.confirm_no_pick": {
        "ko-KR": "확정하실 시간을 한 번만 더 말씀해 주시겠어요?",
        "en-US": "Could you tell me once more which time you'd like to confirm?",
    },
    # --- tasks/booking/select_reservation_task.py ---
    "common.date_undecided": {"ko-KR": "날짜 미정", "en-US": "date TBD"},
    "select_reservation.action.modify": {"ko-KR": "변경", "en-US": "change"},
    "select_reservation.action.cancel": {"ko-KR": "취소", "en-US": "cancel"},
    "select_reservation.announce_list": {
        "ko-KR": "다음 예약 목록을 날짜로 안내하고, 어떤 날짜의 예약을 {action}하실지 여쭤보세요. 번호는 절대 말하지 말고 날짜로만 안내하세요. 인사말 없이 바로 안내하세요.\n\n{reservation_list}",
        "en-US": "Guide the following reservation list by date and ask which date's reservation they want to {action}. Never say numbers; guide by date only. Start directly without greetings.\n\n{reservation_list}",
    },
    "select_reservation.single_confirm": {
        "ko-KR": "예약 내역이 1건입니다: {spoken_date}, {service}, {content}. 이 예약을 {action}할지 사용자에게 확인하세요. 이 질문이 유일한 확인 절차이므로, 예약을 먼저 알려주고 날짜와 시각을 포함해 한 문장으로 명확히 물어보세요. 인사말 없이 바로 안내하세요.",
        "en-US": "There is one reservation: {spoken_date}, {service}, {content}. Ask the user to confirm whether to {action} this reservation. This is the only confirmation step, so state the reservation first and ask one clear question that includes the date and time. Start directly without greetings.",
    },
    "select_reservation.not_found": {
        "ko-KR": "죄송합니다. 말씀하신 예약을 찾지 못했어요. {action}하실 예약 날짜를 다시 말씀해 주세요.",
        "en-US": "Sorry, I couldn't find the reservation you mentioned. Please tell me again the date of the reservation you'd like to {action}.",
    },
    "select_reservation.confirm_pending": {
        "ko-KR": "{spoken} 예약을 {action}하시는 게 맞을까요? 맞으시면 '네', 아니면 다시 말씀해 주세요.",
        "en-US": "Would you like to {action} the {spoken} reservation? If so, say 'yes'; otherwise, please tell me again.",
    },
    "select_reservation.no_pending": {
        "ko-KR": "어떤 날짜의 예약을 {action}하실지 먼저 말씀해 주세요.",
        "en-US": "Please first tell me which date's reservation you'd like to {action}.",
    },
    "select_reservation.reselect_announce": {
        "ko-KR": "'다시 확인하실 수 있도록 예약 내역을 안내해드리겠습니다.'라고 안내한 뒤, 다음 예약 목록을 날짜로 읽어주고 어떤 날짜의 예약을 {action}하실지 다시 여쭤보세요. 번호는 절대 말하지 말고 날짜로만 안내하세요. 인사말 없이 바로 안내하세요.\n\n{reservation_list}",
        "en-US": 'First say "Let me go over your reservations again so you can confirm.", then read the following reservation list by date and ask again which date\'s reservation they want to {action}. Never say numbers; guide by date only. Start directly without greetings.\n\n{reservation_list}',
    },
    # --- tasks/booking/select_operator_task.py ---
    "select_operator.service_hint": {
        "ko-KR": "(선택된 서비스: {service})",
        "en-US": "(Selected service: {service})",
    },
    "select_operator.service_fallback": {
        "ko-KR": "선택하신 서비스",
        "en-US": "the selected service",
    },
    "select_operator.operator_list_placeholder": {
        "ko-KR": "- on_enter에서 실제 상담사 목록을 받은 뒤 갱신됩니다.",
        "en-US": "- Will be updated after the actual advisor list is received in on_enter.",
    },
    "select_operator.no_operators": {
        "ko-KR": "현재 선택 가능한 상담사 정보가 없어 예약을 계속 진행하기 어렵고, 상담원 안내가 필요하다고 자연스럽게 안내하세요.",
        "en-US": "Naturally explain that there is currently no selectable advisor information, so it's difficult to continue booking, and that an agent's assistance is needed.",
    },
    "select_operator.proposed_operator_devtag": {
        "ko-KR": "[PROPOSED_OPERATOR: {name}] — 단일 상담사가 미리 제안되었습니다. 사용자가 동의하면 propose_operator() 없이 confirm_selection()을 바로 호출하세요.",
        "en-US": "[PROPOSED_OPERATOR: {name}] — A single advisor has been pre-proposed. If the user agrees, call confirm_selection() directly without propose_operator().",
    },
    "select_operator.single_operator_prompt": {
        "ko-KR": (
            "상담사 선택 Task에 진입한 첫 발화입니다. 인사말이나 자기소개 없이 바로 안내하세요.\n"
            "JSON, 중괄호, 도구 이름, 도구 인자를 텍스트로 절대 출력하지 마세요. "
            "텍스트는 TTS로 그대로 읽히므로 안내 문장만 말하세요.\n"
            "상담사 ID나 번호는 절대 말하지 말고, 상담사 이름만 사용하세요.\n"
            "confirm_selection()은 이 발화에서 절대 호출하지 마세요. 사용자의 동의를 받은 이후에만 호출합니다.\n\n"
            "다음 형식으로 말하세요:\n"
            '"선택하신 서비스는 {name} 상담사로 예약 가능합니다. {name} 상담사로 진행해 드릴까요?"'
        ),
        "en-US": (
            "This is the first utterance after entering the advisor-selection task. Speak directly, without greetings or self-introduction.\n"
            "Never output JSON, braces, tool names, or tool arguments as text. "
            "Text is read aloud by TTS, so say only the guidance sentence.\n"
            "Never say advisor IDs or numbers; use only the advisor's name.\n"
            "Never call confirm_selection() in this utterance. Only call it after the user agrees.\n\n"
            "Say it in this format:\n"
            '"The service you selected has {name} available for booking. Would you like to go with {name}?"'
        ),
    },
    "select_operator.fast_match_prompt": {
        "ko-KR": (
            "상담사 선택 Task에 진입한 첫 발화입니다. 인사말이나 자기소개 없이 바로 안내하세요.\n"
            "JSON, 중괄호, 도구 이름, 도구 인자를 텍스트로 절대 출력하지 마세요. "
            "텍스트는 TTS로 그대로 읽히므로 안내 문장만 말하세요.\n"
            "상담사 ID나 번호는 절대 말하지 말고, 아래 상담사 목록의 이름만 사용하세요.\n\n"
            "먼저 최근 대화 내용(chat_ctx)을 확인하세요.\n"
            "매칭 대상은 오직 사용자(user) 발화에서 상담사 선택/변경을 요청하며 "
            "언급한 이름뿐입니다. 어시스턴트 발화나 인사말 등에 등장한 단어"
            "(예: 'AI 안내', 'AI 전화')로는 절대 매칭하지 마세요.\n"
            "사용자가 요청한 상담사가 아래 목록과 의미상 일치하는 항목이 정확히 하나이면:\n"
            "  - 반드시 먼저 propose_operator(operator_name=목록의 정확한 이름)를 호출하세요.\n"
            '  - 그 다음 "[상담사 이름] 상담사로 예약하시겠어요?" 형태로 확인 질문만 하세요.\n'
            "  - 이 경우 상담사 목록 전체를 읽지 마세요.\n"
            "  - confirm_selection()은 이 발화에서 절대 호출하지 마세요. 사용자의 동의를 받은 이후에만 호출합니다.\n"
            "그렇지 않으면 (매칭 없음, 여러 개 매칭, 또는 확신이 없음):\n"
            "  - 도구를 호출하지 말고 다음 형식으로 안내하세요:\n"
            '    "선택하신 서비스에서 예약 가능한 상담사는 [상담사 이름], [상담사 이름], [상담사 이름]입니다. 원하시는 상담사 이름을 말씀해 주세요."\n\n'
            "선택된 서비스: {service}\n상담사 목록: {operator_names}"
        ),
        "en-US": (
            "This is the first utterance after entering the advisor-selection task. Speak directly, without greetings or self-introduction.\n"
            "Never output JSON, braces, tool names, or tool arguments as text. "
            "Text is read aloud by TTS, so say only the guidance sentence.\n"
            "Never say advisor IDs or numbers; use only the names from the advisor list below.\n\n"
            "First, check the recent conversation (chat_ctx).\n"
            "Only match against a name the USER mentioned while asking to select/change an advisor. "
            "Never match on words that appeared in assistant utterances or greetings (e.g. 'AI guidance', 'AI call').\n"
            "If the advisor the user requested matches exactly one item in the list below semantically:\n"
            "  - First, you MUST call propose_operator(operator_name=the exact name from the list).\n"
            '  - Then ask only a confirmation question in the form "Would you like to book with [name]?"\n'
            "  - In this case, do not read out the entire advisor list.\n"
            "  - Never call confirm_selection() in this utterance. Only call it after the user agrees.\n"
            "Otherwise (no match, multiple matches, or not confident):\n"
            "  - Do not call any tool; guide the user in this format:\n"
            '    "The advisors available in the selected service are [name], [name], [name]. Please tell me the name of the advisor you\'d like."\n\n'
            "Selected service: {service}\nAdvisor list: {operator_names}"
        ),
    },
    "select_operator.list_prompt": {
        "ko-KR": (
            "상담사 선택 Task에 진입한 첫 발화입니다. 인사말이나 자기소개 없이 바로 안내하세요.\n"
            "JSON, 중괄호, 도구 이름, 도구 인자를 텍스트로 절대 출력하지 마세요. "
            "텍스트는 TTS로 그대로 읽히므로 안내 문장만 말하세요.\n"
            "상담사 ID나 번호는 절대 말하지 말고, 아래 상담사 목록의 이름만 사용하세요.\n\n"
            "다음 형식으로 안내하세요:\n"
            '"선택하신 서비스에서 예약 가능한 상담사는 [상담사 이름], [상담사 이름], [상담사 이름]입니다. 원하시는 상담사 이름을 말씀해 주세요."\n\n'
            "위 형식의 [상담사 이름]에는 상담사 목록의 이름을 그대로 넣어 자연스럽게 안내하세요.\n"
            "선택된 서비스: {service}\n상담사 목록: {operator_names}"
        ),
        "en-US": (
            "This is the first utterance after entering the advisor-selection task. Speak directly, without greetings or self-introduction.\n"
            "Never output JSON, braces, tool names, or tool arguments as text. "
            "Text is read aloud by TTS, so say only the guidance sentence.\n"
            "Never say advisor IDs or numbers; use only the names from the advisor list below.\n\n"
            "Guide the user in this format:\n"
            '"The advisors available in the selected service are [name], [name], [name]. Please tell me the name of the advisor you\'d like."\n\n'
            "Fill the [name] placeholders above with the actual names from the advisor list and guide naturally.\n"
            "Selected service: {service}\nAdvisor list: {operator_names}"
        ),
    },
    "select_operator.invalid_operator": {
        "ko-KR": "'{name}'은(는) 목록에 없는 상담사입니다. 다음 상담사 중에서 선택해 주세요: {operator_names}",
        "en-US": "'{name}' is not in the list of advisors. Please choose from: {operator_names}",
    },
    "select_operator.proposed": {
        "ko-KR": "PROPOSED: {name} — 사용자에게 '{name} 상담사로 예약하시겠어요?' 라고 확인하세요. confirm_selection()은 사용자가 동의한 이후에만 호출하세요.",
        "en-US": 'PROPOSED: {name} — Ask the user "Would you like to book with {name}?". Only call confirm_selection() after the user agrees.',
    },
    "select_operator.no_proposal": {
        "ko-KR": "제안된 상담사가 없습니다. 사용자가 동의한 상담사 이름으로 propose_operator(operator_name=...)를 지금 즉시 호출한 다음, confirm_selection()을 다시 호출하세요. 사용자에게 같은 확인 질문을 반복하지 마세요.",
        "en-US": "There is no proposed advisor. Call propose_operator(operator_name=...) right now with the advisor the user agreed to, then call confirm_selection() again. Do not repeat the same confirmation question to the user.",
    },
    "select_operator.confirmed_say": {
        "ko-KR": "{name} 상담사로 선택되었습니다. 다음으로 예약가능한 날짜와 시간을 확인하겠습니다. 잠시만 기다려주세요.",
        "en-US": "You've selected {name}. Next, I'll check the available dates and times. Please hold for a moment.",
    },
    # --- tasks/booking/select_service_task.py ---
    "select_service.single_confirm": {
        "ko-KR": "{name}(으)로 예약을 진행할지 사용자에게 확인하세요. ",
        "en-US": "Ask the user to confirm whether to proceed with booking {name}. ",
    },
    "select_service.fast_match_prompt": {
        "ko-KR": (
            "서비스 선택 Task의 첫 발화입니다. 인사말 없이 바로 안내하세요.\n\n"
            "먼저 최근 대화 내용(chat_ctx)을 확인하세요.\n"
            "사용자가 이미 원하는 서비스를 말했고, 아래 목록과 의미상 일치하는 항목이 정확히 하나이면:\n"
            "  - 반드시 먼저 propose_service(service_name=목록의 전체 이름)를 호출하세요. (텍스트로 쓰지 말고 실제 도구 호출로)\n"
            "  - 그 다음 해당 서비스의 목록 전체 이름을 사용해 예약 진행 여부를 확인하세요.\n"
            '  - 예: "[서비스 전체 이름](으)로 예약을 진행해 드릴까요?"\n'
            "  - 이 경우 서비스 목록 전체를 읽지 마세요.\n"
            "  - confirm_selection()은 이 발화에서 절대 호출하지 마세요. 사용자의 동의를 받은 이후에만 호출합니다.\n"
            "그렇지 않으면 (매칭 없음 또는 여러 개 매칭):\n"
            "  - 도구를 호출하지 말고, 아래 서비스 목록을 안내하고 원하는 서비스를 말씀해 달라고 하세요.\n"
            '  - 예: "현재 [서비스1], [서비스2] 예약이 가능합니다. 원하는 서비스를 말씀해 주세요."\n\n'
            "서비스 목록:\n{service_list}"
        ),
        "en-US": (
            "This is the first utterance of the service-selection task. Do not greet; guide the user directly.\n\n"
            "First, check the recent conversation (chat_ctx).\n"
            "If the user already stated the service they want and exactly one item in the list below matches it semantically:\n"
            "  - First, you MUST call propose_service(service_name=the full name from the list). (As an actual tool call, not as text.)\n"
            "  - Then confirm whether to proceed, using that service's full name from the list.\n"
            '  - e.g., "Would you like to proceed with booking for the [full service name] service?"\n'
            "  - In this case, do not read out the entire service list.\n"
            "  - Never call confirm_selection() in this utterance. Only call it after the user agrees.\n"
            "Otherwise (no match or multiple matches):\n"
            "  - Do not call any tool; present the service list below and ask which service they'd like.\n"
            '  - e.g., "We currently have [Service 1], [Service 2] available. Please tell me which one you\'d like."\n\n'
            "Service list:\n{service_list}"
        ),
    },
    "select_service.list_prompt": {
        "ko-KR": (
            "서비스 선택 Task의 첫 발화입니다. 인사말 없이 바로 안내하세요.\n"
            "JSON, 중괄호, 도구 이름, 도구 인자를 텍스트로 절대 출력하지 마세요. "
            "텍스트는 TTS로 그대로 읽히므로 안내 문장만 말하세요.\n"
            "서비스 ID나 번호는 절대 말하지 말고, 아래 서비스 목록의 이름만 사용하세요.\n\n"
            "다음 형식으로 안내하세요:\n"
            '"현재 [서비스1], [서비스2] 예약이 가능합니다. 원하시는 서비스를 말씀해 주세요."\n\n'
            "위 형식의 [서비스]에는 서비스 목록의 이름을 그대로 넣어 자연스럽게 안내하세요.\n"
            "서비스 목록:\n{service_list}"
        ),
        "en-US": (
            "This is the first utterance of the service-selection task. Do not greet; guide the user directly.\n"
            "Never output JSON, braces, tool names, or tool arguments as text. "
            "Text is read aloud by TTS, so say only the guidance sentence.\n"
            "Never say service IDs or numbers; use only the names from the service list below.\n\n"
            "Guide the user in this format:\n"
            '"We currently have [Service 1], [Service 2] available. Please tell me which one you\'d like."\n\n'
            "Fill the [Service] placeholders above with the actual names from the service list and guide naturally.\n"
            "Service list:\n{service_list}"
        ),
    },
    "select_service.invalid_service": {
        "ko-KR": "'{name}'은(는) 목록에 없는 서비스입니다. 다음 서비스 중에서 선택해 주세요: {dept_names}",
        "en-US": "'{name}' is not in the list of services. Please choose from: {dept_names}",
    },
    "select_service.proposed": {
        "ko-KR": "PROPOSED: {name} — 사용자에게 '{name}(으)로 예약하시겠어요?' 라고 확인하세요. confirm_selection()은 사용자가 동의한 이후에만 호출하세요.",
        "en-US": 'PROPOSED: {name} — Ask the user "Would you like to book {name}?". Only call confirm_selection() after the user agrees.',
    },
    "select_service.no_proposal": {
        "ko-KR": "제안된 서비스가 없습니다. 사용자가 동의한 서비스 이름으로 propose_service(service_name=...)를 지금 즉시 호출한 다음, confirm_selection()을 다시 호출하세요. 사용자에게 같은 확인 질문을 반복하지 마세요.",
        "en-US": "There is no proposed service. Call propose_service(service_name=...) right now with the service the user agreed to, then call confirm_selection() again. Do not repeat the same confirmation question to the user.",
    },
    "select_service.next_operator": {
        "ko-KR": "예약 가능한 상담사를 확인하겠습니다",
        "en-US": "I'll check the available advisors",
    },
    "select_service.next_schedule": {
        "ko-KR": "예약가능한 날짜와 시간을 확인하겠습니다",
        "en-US": "I'll check the available dates and times",
    },
    "select_service.confirmed_say": {
        "ko-KR": "{name}(으)로 예약을 진행하겠습니다. 다음으로 {next_step}. 잠시만 기다려주세요.",
        "en-US": "I'll proceed with booking {name}. Next, {next_step}. Please hold for a moment.",
    },
    "select_service.exit_blocked_reprompt": {
        "ko-KR": "죄송합니다, 잘 못 들었어요. 현재 예약 가능한 서비스는 {dept_names}입니다. 원하시는 서비스를 다시 한 번 말씀해 주시겠어요?",
        "en-US": "Sorry, I didn't catch that. The currently available services are {dept_names}. Could you tell me the service you'd like again?",
    },
    "select_service.free_utterance_prompt": {
        "ko-KR": (
            "서비스 선택 Task의 첫 발화입니다. 인사말 없이 바로 안내하세요.\n"
            "서비스 목록을 낭독하지 말고, 사용자가 원하는 서비스나 기기 문제를 자유롭게 "
            "말하도록 유도하세요.\n"
            "다음처럼 말하세요: '원하는 서비스나 기기 문제를 말씀해주세요.'\n"
            "이 발화에서는 어떤 도구도 호출하지 마세요."
        ),
        "en-US": (
            "This is the first utterance of the service-selection task. "
            "Do not greet; guide the user directly.\n"
            "Do not read out the service list. Invite the user to freely "
            "describe the service they need or the issue with their device.\n"
            "Say something like: 'Please tell me the service you need or the issue "
            "with your device, and I'll help you book.'\n"
            "Do not call any tool in this utterance."
        ),
    },
    # --- tasks/booking/exit_reasons.py ---
    "exit_reason.change_service": {
        "ko-KR": "서비스 변경 요청",
        "en-US": "request to change service",
    },
    "exit_reason.change_advisor": {
        "ko-KR": "담당 상담사 변경 요청",
        "en-US": "request to change advisor",
    },
    "exit_reason.no_slot_memo": {
        "ko-KR": "예약 가능한 일정이 없어 상담원 인계(연결 또는 메모)에 동의",
        "en-US": "agreed to a staff handoff (transfer or memo) because no booking slot matched",
    },
    "exit_reason.staff_connection": {
        "ko-KR": "상담원 연결 요청",
        "en-US": "request to connect to an agent",
    },
    "exit_reason.service_not_offered": {
        "ko-KR": "AI 예약 범위에 없는 서비스 요청",
        "en-US": "service outside the AI booking scope",
    },
    "exit_reason.cancel": {
        "ko-KR": "예약 중단 요청",
        "en-US": "request to stop the booking",
    },
    "exit_reason.unrelated": {
        "ko-KR": "예약과 무관한 요청",
        "en-US": "request unrelated to booking",
    },
    "exit_reason.other": {
        "ko-KR": "기타 사유로 예약 선택 중단",
        "en-US": "stopped the selection for another reason",
    },
    # Reservation submission outcomes at the completion point.
    "booking.submit.slot_taken": {
        "ko-KR": "죄송합니다. 확인해 보니 방금 그 시간에 다른 예약이 먼저 들어와 마감되었어요. 다른 시간을 다시 찾아볼게요.",
        "en-US": "Sorry, that time was just taken by another booking. Let me look for another time.",
    },
    "booking.submit.not_found": {
        "ko-KR": "죄송합니다. 해당 예약을 찾을 수 없어 처리하지 못했어요. 다른 도움이 필요하신가요?",
        "en-US": "Sorry, I couldn't find that appointment, so nothing was changed. Is there anything else I can help with?",
    },
    "booking.submit.failed": {
        "ko-KR": "죄송합니다. 예약을 저장하는 중에 문제가 생겨 처리가 완료되지 않았어요.",
        "en-US": "Sorry, something went wrong while saving, so the request was not completed.",
    },
    "booking.exit.cancelled_ack": {
        "ko-KR": "진행 중인 예약 업무는 여기서 멈출게요. 다른 도움이 필요하신가요?",
        "en-US": "I'll stop the current appointment task here. Is there anything else I can help with?",
    },
    # Stop with nothing chosen yet: invite the next step so the caller is not left
    # in silence.
    "booking.exit.cancelled_ack_ask_more": {
        "ko-KR": "네, 예약은 진행하지 않을게요. 다른 도움이 필요하신가요?",
        "en-US": "All right, I won't make a booking. Is there anything else I can help with?",
    },
    "exit_reason.with_detail": {
        "ko-KR": "{base} ({detail})",
        "en-US": "{base} ({detail})",
    },
    # --- tools/booking_tools.py ---
    "booking.error.verification_code_rejected": {
        "ko-KR": "데모 확인번호 확인이 거부되었습니다.",
        "en-US": "Demo verification code check was rejected.",
    },
    "booking.create.start": {
        "ko-KR": "예약 접수를 진행하겠습니다. 잠시만 기다려주세요.",
        "en-US": "I'll go ahead and submit your appointment. Please hold for a moment.",
    },
    "booking.create.same_operator_confirm": {
        "ko-KR": "이전에 상담받으신 이력이 있습니다. 이번에도 {operator} 상담사로 예약할까요?",
        "en-US": "You have a prior visit on record. Would you like to book with {operator} again?",
    },
    "booking.create.success": {
        # "ko-KR": "{datetime}, {service} 예약 신청이 접수 되었습니다. 다만 이 예약은 센터에서 확정된 건이 아니며, 센터에서 확인 후 최종 확정 안내를 드리겠습니다.\n통화를 종료하겠습니다. 좋은 하루 되세요.",
        "ko-KR": "{datetime}, {service} 예약 신청이 완료되었습니다. 센터에서 확인한 뒤 확정 안내를 드릴게요. 방문하실 때 기기와 충전기를 함께 가져와 주세요. 좋은 하루 되세요!",
        "en-US": "Your {service} appointment request for {datetime} is complete. Our staff will review it and let you know once it's confirmed. Please bring your device and charger when you visit.\nI'll end the call now. Have a great day.",
    },
    # 동일 상담사 예약 + 서비스ID 미확정 시: 서비스명 대신 상담사명으로 안내한다.
    "booking.create.success_operator": {
        "ko-KR": "{datetime}, {operator} 상담사로 점검·상담 예약 신청이 완료되었습니다. 센터에서 확인한 뒤 확정 안내를 드릴게요. 방문하실 때 기기와 충전기를 함께 가져와 주세요. 좋은 하루 되세요!",
        "en-US": "Your inspection or consultation appointment request with {operator} on {datetime} is complete. Our staff will review it and let you know once it's confirmed. Please bring your device and charger when you visit.\nI'll end the call now. Have a great day.",
    },
    "booking.modify.no_reservations_handoff.transfer": {
        "ko-KR": "확인해 보니 등록된 예약 내역이 없네요. 정확한 확인을 위해 상담원에게 연결해 드릴게요.",
        "en-US": "I checked and couldn't find any registered appointments. I'll connect you with an agent to confirm the details.",
    },
    "booking.modify.no_reservations_handoff.memo": {
        "ko-KR": "확인해 보니 등록된 예약 내역이 없네요. 상담원에게 전달해 드릴 테니, 어떤 내용인지 말씀해 주시겠어요? 상담원이 확인한 후 다시 연락드릴게요.",
        "en-US": "I checked and couldn't find any registered appointments. I'll pass a message to an agent, so could you tell me what you'd like them to know? An agent will review it and call you back.",
    },
    "booking.cancel.no_reservations_handoff.transfer": {
        "ko-KR": "확인해 보니 등록된 예약 내역이 없네요. 정확한 확인을 위해 상담원에게 연결해 드릴게요.",
        "en-US": "I checked and couldn't find any registered appointments. I'll connect you with an agent to confirm the details.",
    },
    "booking.cancel.no_reservations_handoff.memo": {
        "ko-KR": "확인해 보니 등록된 예약 내역이 없네요. 상담원에게 전달해 드릴 테니, 어떤 내용인지 말씀해 주시겠어요? 상담원이 확인한 후 다시 연락드릴게요.",
        "en-US": "I checked and couldn't find any registered appointments. I'll pass a message to an agent, so could you tell me what you'd like them to know? An agent will review it and call you back.",
    },
    "booking.modify.no_operator_handoff.memo": {
        "ko-KR": "해당 예약의 담당 상담사 정보를 확인할 수 없어 이 전화로는 변경이 어렵습니다. 상담원이 확인할 수 있도록 메모를 남겨드리겠습니다.",
        "en-US": "I can't verify the advisor information for this appointment, so I can't change it on this call. I'll take a message for an agent to review.",
    },
    "booking.modify.no_operator_handoff.transfer": {
        "ko-KR": "해당 예약의 담당 상담사 정보를 확인할 수 없어 이 전화로는 변경이 어렵습니다. 상담원에게 연결해 드리겠습니다.",
        "en-US": "I can't verify the advisor information for this appointment, so I can't change it on this call. I'll connect you with an agent.",
    },
    "booking.modify.schedule_lookup_failed_handoff.memo": {
        "ko-KR": "예약 변경 가능한 일정을 확인하는 중 오류가 발생해 이 전화로는 변경이 어렵습니다. 상담원이 확인할 수 있도록 메모를 남겨드리겠습니다.",
        "en-US": "I couldn't check the available times, so I can't change the appointment on this call. I'll take a message for an agent to review.",
    },
    "booking.modify.schedule_lookup_failed_handoff.transfer": {
        "ko-KR": "예약 변경 가능한 일정을 확인하는 중 오류가 발생해 이 전화로는 변경이 어렵습니다. 상담원에게 연결해 드리겠습니다.",
        "en-US": "I couldn't check the available times, so I can't change the appointment on this call. I'll connect you with an agent.",
    },
    "booking.modify.success": {
        "ko-KR": "예약 변경이 접수되었습니다. 아직 확정된 예약은 아니며, 센터에서 확인한 뒤 확정 안내를 드릴게요.\n통화를 종료하겠습니다. 좋은 하루 되세요.",
        "en-US": "Your appointment change has been received. It isn't confirmed yet; our staff will review it and let you know once it's confirmed.\nI'll end the call now. Have a great day.",
    },
    "booking.cancel.no_operator_handoff.memo": {
        "ko-KR": "해당 예약의 담당 상담사 정보를 확인할 수 없어 이 전화로는 취소가 어렵습니다. 상담원이 확인할 수 있도록 메모를 남겨드리겠습니다.",
        "en-US": "I can't verify the advisor information for this appointment, so I can't cancel it on this call. I'll take a message for an agent to review.",
    },
    "booking.cancel.no_operator_handoff.transfer": {
        "ko-KR": "해당 예약의 담당 상담사 정보를 확인할 수 없어 이 전화로는 취소가 어렵습니다. 상담원에게 연결해 드리겠습니다.",
        "en-US": "I can't verify the advisor information for this appointment, so I can't cancel it on this call. I'll connect you with an agent.",
    },
    "booking.cancel.success": {
        "ko-KR": "예약 취소가 접수되었습니다. 센터에서 확인한 뒤 최종 안내를 드릴게요.\n통화를 종료하겠습니다. 좋은 하루 되세요.",
        "en-US": "Your appointment cancellation has been received. Our staff will review it and follow up with you.\nI'll end the call now. Have a great day.",
    },
    # --- tasks/dtmf_verification_code_task.py (+ build_verification_code_confirmation_announcement) ---
    "dtmf_verification_code.announcement.instruction": {
        "ko-KR": "데모 확인번호 6자리를 입력해 주세요.",
        "en-US": "Please enter your 6-digit demo verification code.",
    },
    "dtmf_verification_code.reprompt.incomplete": {
        "ko-KR": "6자리를 모두 입력해 주세요.",
        "en-US": "Please enter all 6 digits.",
    },
    "dtmf_verification_code.reprompt.invalid": {
        "ko-KR": "확인할 수 없는 번호예요. 다시 입력해 주세요.",
        "en-US": "I couldn't recognize that code. Please enter it again.",
    },
    "verification_code.confirm.announcement": {
        "ko-KR": "입력하신 번호가 {digits}{josa} 맞는지 네, 아니오로 말씀해 주세요.",
        "en-US": "You entered {digits}. Please say yes if that's correct, or no if it's not.",
    },
    "verification_code.confirm_instructions": {
        "ko-KR": (
            "당신은 서비스센터에 전화한 고객이 키패드로 입력한 데모 확인번호 6자리를 확인하는 안내 AI입니다.\n\n"
            "## 발화 규칙\n"
            "- 절대로 인사하지 마세요. 자기소개도 금지합니다.\n"
            "- 자연스럽고 간결한 구어체로 말하세요.\n"
            "- 입력 번호는 처음 안내 문구처럼 숫자를 두 자리씩 그대로 읽고, 연·월·일 날짜 표현으로 바꾸지 마세요.\n\n"
            "## 확인 흐름\n"
            '- 사용자가 명백히 맞다고 하면 `confirm_information` 도구를 호출하세요. (예: "네", "예", "맞습니다", "맞아요")\n'
            '- 사용자 답변이 애매한 경우(예: "글쎄요", "잘 모르겠는데") 한 번 더 확인해 주세요.\n'
            "- 사용자가 틀리다고 하거나 수정을 요청하면 `reject_information` 도구를 호출하세요.\n"
            "- 확인과 무관한 질문에는 간단히 응답한 후 다시 확인을 요청하세요.\n\n"
            "## 처음에 안내할 문구\n"
            "다음내용을 그대로 말하세요:\n"
            "{announcement}"
        ),
        "en-US": (
            "You are a guidance AI confirming the 6-digit demo verification code a customer entered on the keypad when calling the service center.\n\n"
            "## Speaking rules\n"
            "- Never greet the caller or introduce yourself.\n"
            "- Speak in a natural, concise, conversational tone.\n"
            "- Read the entered digits in pairs exactly as in the opening line; do not convert them into a year, month, or day expression.\n\n"
            "## Confirmation flow\n"
            '- If the user clearly confirms it is correct, call the `confirm_information` tool. (e.g., "yes", "that\'s right", "correct")\n'
            '- If the user\'s answer is ambiguous (e.g., "hmm", "I\'m not sure"), confirm once more.\n'
            "- If the user says it is wrong or asks to correct it, call the `reject_information` tool.\n"
            "- For questions unrelated to the confirmation, answer briefly and then ask for confirmation again.\n\n"
            "## Opening line to say\n"
            "Say the following exactly:\n"
            "{announcement}"
        ),
    },
    # --- tasks/dynamic_booking_task.py ---
    "dynamic_booking.service_request": {
        "ko-KR": "원하시는 서비스나 기기 문제를 말씀해 주세요.",
        "en-US": "Please tell me the service you need or the issue with your device.",
    },
    # --- tasks/booking/get_my_reservations_task.py ---
    "get_my_reservations.dtmf_verification_code_purpose": {
        "ko-KR": "연락처로 등록된 고객 정보를 찾지 못했습니다.",
        "en-US": "We couldn't find a customer registered under your phone number.",
    },
    # --- tasks/leave_message_task.py ---
    "leave_message.request_label.booking_create": {
        "ko-KR": "예약 신청 요청",
        "en-US": "appointment request",
    },
    "leave_message.request_label.booking_modify": {
        "ko-KR": "예약 변경 요청",
        "en-US": "appointment change request",
    },
    "leave_message.request_label.booking_cancel": {
        "ko-KR": "예약 취소 요청",
        "en-US": "appointment cancellation request",
    },
    "leave_message.request_label.booking_inquiry": {
        "ko-KR": "예약 조회 요청",
        "en-US": "appointment inquiry",
    },
    "leave_message.progress_notice.default": {
        "ko-KR": "지금은 상담원 연결이 어려운 상황이에요. 상담원에게 전달하고 싶은 요청이나 메시지를 말씀해 주세요.",
        "en-US": "Please tell me the request or message you'd like to pass on to an agent.",
    },
    "leave_message.progress_notice.booking_create": {
        # "ko-KR": "예약 신청은 상담원 확인을 통해 진행됩니다. 예약 신청 요청을 상담원에게 전달해 드리겠습니다. 추가로 상담원에게 전달하고 싶은 내용이 있을까요?",
        "ko-KR": "상담원에게 전달할 메세지를 말씀해주세요. 예약 문의을 남기시면 나중에 상담원이 확인 후 연락 드릴거에요.",
        "en-US": "Booking requests are handled after an agent reviews them. I'll pass your booking request on to an agent. Is there anything else you'd like to add for the agent?",
    },
    "leave_message.progress_notice.booking_modify": {
        # "ko-KR": "예약 변경은 상담원 확인을 통해 진행됩니다. 예약 변경 요청을 상담원에게 전달해 드리겠습니다. 추가로 상담원에게 전달하고 싶은 내용이 있을까요?",
        "ko-KR": "상담원에게 전달할 메세지를 말씀해주세요. 예약 문의을 남기시면 나중에 상담원이 확인 후 연락 드릴거에요.",
        "en-US": "Appointment changes are handled after an agent reviews them. I'll pass your change request on to an agent. Is there anything else you'd like to add for the agent?",
    },
    "leave_message.progress_notice.booking_cancel": {
        "ko-KR": "예약 취소 접수를 도와드리겠습니다. 취소 사유를 말씀해 주세요. 개인 사정, 일정 미정, 다른 센터 이용, 그 외처럼 말씀해 주세요.",
        "en-US": "I'll help you submit a cancellation. Please tell me the reason for cancelling — for example, personal reasons, an undecided schedule, using another service center, or something else.",
    },
    "leave_message.progress_notice.booking_inquiry": {
        "ko-KR": "예약 조회는 상담원 확인을 통해 진행됩니다. 예약 조회 요청을 상담원에게 전달해 드리겠습니다. 추가로 상담원에게 전달하고 싶은 내용이 있을까요?",
        "en-US": "Appointment inquiries are handled after an agent reviews them. I'll pass your inquiry on to an agent. Is there anything else you'd like to add for the agent?",
    },
    "leave_message.invalid_datetime_format": {
        "ko-KR": "희망 일시는 MM-DD HH:MM 형식으로 정리한 뒤 다시 확인해 주세요.",
        "en-US": "Please provide the date and time in MM-DD HH:MM format.",
    },
    "leave_message.collected_default": {
        # "ko-KR": "상담원에게 전달하겠습니다. 검토 후 연락드릴 예정입니다. 통화를 종료하겠습니다. 좋은 하루 보내세요.",
        "ko-KR": "말씀하신 내용은 센터 상황에 따라 변경될 수 있습니다. 상담원이 메모 확인 후 다시 전화 드릴게요. 통화를 종료하겠습니다. 좋은 하루 보내세요.",
        "en-US": "I'll pass this on to an agent. We'll review it and get back to you. I'll end the call now. Have a great day.",
    },
    "leave_message.collected_message_only": {
        "ko-KR": "네 알겠습니다. 남기신 말씀은 상담원에게 전달할게요. 통화를 종료하겠습니다. 좋은 하루 보내세요.",
        "en-US": "Okay, understood. I'll pass your message on to our staff. I'll end the call now. Have a great day.",
    },
    "leave_message.collected_cancel": {
        "ko-KR": "해당 내용은 상담원에게 전달되며, 확인 후 예약 취소가 진행됩니다. 통화를 종료하겠습니다. 좋은 하루 보내세요.",
        "en-US": "This will be passed on to an agent, and your cancellation will proceed after review. I'll end the call now. Have a great day.",
    },
    # 상담원 연결 실패 안내 접두 문구 (날짜 수집 오프너에 조건부로 prepend)
    "leave_message.transfer_failure_prefix": {
        "ko-KR": "죄송하지만 지금은 상담원 연결이 어렵습니다.",
        "en-US": "I'm sorry, but I can't connect you to an agent right now.",
    },
    # 날짜 수집 모드(collect_desired_datetime=True) 전용 첫 발화 오프너.
    # 기존 progress_notice.* 는 message_only 모드에서 계속 사용된다.
    "leave_message.opening_datetime.booking_create": {
        # "ko-KR": "예약 신청 요청을 상담원에게 전달해 드리겠습니다. 원하시는 예약 날짜와 시간을 말씀해 주세요.",
        "ko-KR": "지금 메세지를 남겨주시면 전달하겠습니다. 원하는 예약일정을 말씀하시는 경우, 임시 예약으로만 저장되오니 정확한 예약일정은 상담원과 통화 후 확인해주세요. 어떤 날짜로 신청 원하세요? 편하신 요일이나 시간대 있으시면 같이 말씀해주세요.",
        "en-US": "I'll pass your appointment request on to an agent. Please tell me the date and time you'd like.",
    },
    # {subject} 는 코드가 조립한다: "정진수 상담사 휴대폰 점검" / "휴대폰 점검" /
    # "요청하신 서비스". "조건으로" 는 조사 충돌 없이 어떤 time_condition 에도 붙는다.
    "leave_message.opening_datetime.booking_context": {
        "ko-KR": "{subject} 예약을 {time_condition} 조건으로 요청하시는 것으로 상담원에게 남길까요? 다른 날짜나 시간을 원하시면 말씀해 주세요.",
        "en-US": "Should I leave a request with our staff for {subject} at {time_condition}, or would you prefer a different date or time?",
    },
    "leave_message.opening_datetime.booking_modify": {
        # "ko-KR": "예약 변경 요청을 상담원에게 전달해 드리겠습니다. 원하시는 예약 날짜와 시간을 말씀해 주세요.",
        "ko-KR": "지금은 메세지를 남겨주시면 전달하겠습니다. 원하는 예약일정을 말씀하시는 경우, 임시 예약으로만 저장되오니 정확한 예약일정은 상담원과 통화 후 확인해주세요. 어떤 날짜로 변경 원하세요? 편하신 요일이나 시간대 있으시면 같이 말씀해주세요.",
        "en-US": "I'll pass your appointment change request on to an agent. Please tell me the date and time you'd like.",
    },
    # set_desired_datetime 의 반환값: 확정된 날짜/시간을 되읽고 맞는지 확인(STEP 2 유도).
    # After confirmation, submit without an optional additional-message question.
    "leave_message.datetime_set_confirm": {
        "ko-KR": "{datetime}으로 말씀하신게 맞으신가요?",
        "en-US": "That would be {datetime}. Is that the date and time you'd like?",
    },
    # submit_booking_contact 가 날짜/시간 미확정 상태로 호출됐을 때의 가드 반환값.
    "leave_message.datetime_not_set": {
        "ko-KR": "먼저 원하시는 예약 날짜와 시간을 말씀해 주세요.",
        "en-US": "Please tell me the date and time you'd like first.",
    },
    # Internal tool instruction; never speak this to the caller.
    "leave_message.condition_confirm": {
        "ko-KR": (
            "Condition confirmed. Do not read this result aloud. "
            "Once any required call-specific details have been collected, "
            "call submit_booking_contact now without asking for an optional message."
        ),
        "en-US": (
            "Condition confirmed. Do not read this result aloud. "
            "Once any required call-specific details have been collected, "
            "call submit_booking_contact now without asking for an optional message."
        ),
    },
    "leave_message.condition_not_set": {
        "ko-KR": "상담원에게 전달할 희망 조건을 말씀해 주세요.",
        "en-US": "Please tell me the scheduling condition you'd like me to relay.",
    },
    "leave_message.condition_set_confirm": {
        # "ko-KR": "{condition} 조건으로 요청을 남길까요?",
        "ko-KR": "{condition}으로 말씀하신게 맞으신가요?",
        "en-US": "Should I leave the request with the condition: {condition}?",
    },
    # --- tasks/verify_customer_identity_task.py + tools/booking_tools.py escalation ---
    "verify_customer.escalation.ambiguous_match": {
        "ko-KR": "동일한 정보로 등록된 분이 여러 명 확인되어 상담원 연결이 필요합니다. 잠시만 기다려주세요.",
        "en-US": "We found more than one person registered with the same information, so I need to connect you with an agent. Please hold for a moment.",
    },
    "verify_customer.escalation.no_match": {
        "ko-KR": "일치하는 고객 정보가 없어 상담 직원과 확인이 필요합니다. 잠시만 기다려주세요.",
        "en-US": "I couldn't find a customer matching that verification code, so I need to connect you with an agent. Please hold for a moment.",
    },
    # 확인번호를 끝내 받지 못한 경우. 대조가 일어나지 않았으므로 "일치하는 고객이
    # 없다"(no_match)고 말하면 사실과 다르다.
    "verify_customer.escalation.verification_code_not_provided": {
        "ko-KR": "데모 확인번호를 확인하지 못해 상담원 연결이 필요합니다. 잠시만 기다려주세요.",
        "en-US": "I wasn't able to confirm your 6-digit demo verification code, so I need to connect you with an agent. Please hold for a moment.",
    },
    # verify_failed 는 알 수 없는/누락 사유의 기본 에스컬레이션 멘트로도 사용된다
    # (booking_tools / dynamic_booking_tools 의 escalation 폴백).
    "verify_customer.escalation.verify_failed": {
        "ko-KR": "입력하신 정보로 본인 확인이 어려워 상담원 연결이 필요합니다. 잠시만 기다려주세요.",
        "en-US": "I'm unable to verify your identity with the information provided, so I need to connect you with an agent. Please hold for a moment.",
    },
    "verify_customer.purpose.create": {
        "ko-KR": "예약 신청을 위해 데모 확인번호로 본인 확인이 필요합니다.",
        "en-US": "To book an appointment, I need to verify your identity with your 6-digit demo verification code.",
    },
    "verify_customer.purpose.inquiry": {
        "ko-KR": "예약 조회를 위해 데모 확인번호로 본인 확인이 필요합니다.",
        "en-US": "To look up your appointment, I need to verify your identity with your 6-digit demo verification code.",
    },
    "verify_customer.purpose.modify": {
        "ko-KR": "예약 변경을 위해 데모 확인번호로 본인 확인이 필요합니다.",
        "en-US": "To change your appointment, I need to verify your identity with your 6-digit demo verification code.",
    },
    "verify_customer.purpose.cancel": {
        "ko-KR": "예약 취소를 위해 데모 확인번호로 본인 확인이 필요합니다.",
        "en-US": "To cancel your appointment, I need to verify your identity with your 6-digit demo verification code.",
    },
    "verify_customer.purpose.retry": {
        "ko-KR": "고객님의 연락처로 등록된 고객 정보가 여러 건 확인되어 데모 확인번호로 정확한 확인이 필요합니다.",
        "en-US": "We found multiple customer records under your phone number, so I need to confirm precisely.",
    },
    # --- tasks/caller_wait_reconfirmation_task.py ---
    "caller_wait.reconfirm.prompt": {
        "ko-KR": (
            "계속 상담원 연결을 대기하시겠습니까? "
            "계속 대기하시려면 1번, 종료하시려면 2번을 눌러주세요."
        ),
        "en-US": (
            "Would you like to keep waiting to be connected to an agent? "
            "Press 1 to keep waiting, or press 2 to end the call."
        ),
    },
}


def t(key: str, language: Optional[str] = None, **fmt: object) -> str:
    """key/언어에 해당하는 고정멘트를 반환한다.

    Fallback 순서: 요청 언어 -> DEFAULT_MESSAGE_LANGUAGE -> key 자체(누락 감지용).
    fmt가 주어지면 str.format(**fmt)로 변수를 치환한다.

    Args:
        key: 메시지 키 (예: "system.error").
        language: BCP 47 언어 태그 (예: "ko-KR", "en-US"). None이면 기본 언어.
        **fmt: 템플릿 변수 치환용 키워드 인자.

    Returns:
        해당 언어의 메시지 문자열.
    """
    lang = language or DEFAULT_MESSAGE_LANGUAGE
    by_lang = MESSAGES.get(key, {})
    text = by_lang.get(lang) or by_lang.get(DEFAULT_MESSAGE_LANGUAGE) or key
    return text.format(**fmt) if fmt else text
