"""진입 인사말 분기 — 조회 지시가 붙는 경계 검증.

DTMF 선택과 확인번호 확인만 거쳐 진입하면(고객 발화는 확인 응답 "네" 하나)
조회할 조건이 없다. 이때 진입 턴에 search_schedules가 당일 조건으로 호출되면
빈 결과를 "오늘 예약 가능한 시간이 없습니다"로 안내하게 된다. 반대로 고객이
"오늘 예약해주세요"라고 이미 말한 진입에서는 그 조회가 정상 동작이다.
두 경우를 가르는 것은 코드가 세는 사실이므로 여기서 고정한다.
"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import pytest

from tasks.dynamic_booking_task import (
    DynamicBookingTask,
    _has_condition_bearing_user_utterance,
    _has_prior_dialogue,
)


def _message(role: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(type="message", role=role, text_content=text)


def _chat_ctx(*items: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(items=list(items))


# DTMF 메뉴로만 진입한 시점의 chat_ctx (developer 이벤트는 content="" 라 copy 단계에서 빠진다)
DTMF_ONLY_CTX = _chat_ctx(
    _message("assistant", "Example Device Care 입니다. 예약 신청은 1번을 눌러주세요."),
    _message("assistant", "예약 신청을 위해 본인 확인이 필요합니다."),
    _message(
        "assistant",
        "입력하신 번호가 일공 공오 일일이 맞는지 네, 아니오로 말씀해 주세요.",
    ),
    _message("user", "네."),
)

REQUEST_CTX = _chat_ctx(
    _message("assistant", "Example Device Care 입니다."),
    _message("user", "오늘 예약해주세요."),
    _message(
        "assistant",
        "입력하신 번호가 일공 공오 일일이 맞는지 네, 아니오로 말씀해 주세요.",
    ),
    _message("user", "네."),
)


def _task(
    chat_ctx: SimpleNamespace, *, staged=None, single_service: bool = False
) -> DynamicBookingTask:
    task = object.__new__(DynamicBookingTask)
    task._staged = staged
    task._entry_has_prior_dialogue = _has_prior_dialogue(chat_ctx)
    task._entry_has_user_request = _has_condition_bearing_user_utterance(chat_ctx)
    task._services = [
        {"id": 1, "name": "휴대폰 점검"},
        {"id": 2, "name": "노트북 상담"},
    ]
    if single_service:
        task._services = task._services[:1]
    return task


class ConditionBearingUtteranceTest(TestCase):
    def test_acknowledgement_only_is_not_a_request(self):
        for text in ("네", "네.", "예!", "아니요", "맞아요", "응"):
            with self.subTest(text=text):
                ctx = _chat_ctx(_message("user", text))
                self.assertFalse(_has_condition_bearing_user_utterance(ctx))

    def test_booking_condition_is_a_request(self):
        ctx = _chat_ctx(_message("user", "오늘 예약해주세요"))
        self.assertTrue(_has_condition_bearing_user_utterance(ctx))

    def test_booking_intent_without_date_is_a_request(self):
        ctx = _chat_ctx(_message("user", "예약하려고요."))
        self.assertTrue(_has_condition_bearing_user_utterance(ctx))

    def test_assistant_speech_alone_is_not_a_request(self):
        ctx = _chat_ctx(_message("assistant", "오늘 예약 도와드릴까요?"))
        self.assertFalse(_has_condition_bearing_user_utterance(ctx))
        # 인사말 생략 판정에는 여전히 걸린다 — 두 사실은 분리되어 있다.
        self.assertTrue(_has_prior_dialogue(ctx))


class EntryGreetingBranchTest(TestCase):
    def test_single_service_wins_for_every_new_entry_context(self):
        contexts = (
            _chat_ctx(),
            DTMF_ONLY_CTX,
            REQUEST_CTX,
            _chat_ctx(_message("user", "예약하려고요.")),
        )
        for ctx in contexts:
            with self.subTest(ctx=ctx):
                task = _task(ctx, single_service=True)
                self.assertEqual(task._entry_greeting_branch(), "single_service")

    def test_single_service_does_not_override_staged_candidate(self):
        task = _task(
            REQUEST_CTX,
            staged=SimpleNamespace(service_id=1),
            single_service=True,
        )
        task._last_search_conditions = {"service_id": 1}
        self.assertEqual(task._entry_greeting_branch(), "resume_pending")

    def test_single_service_does_not_override_resume_search(self):
        task = _task(_chat_ctx(), single_service=True)
        task._last_search_conditions = {"service_id": 1, "time_to": "11:59"}
        self.assertEqual(task._entry_greeting_branch(), "resume_search")

    def test_single_service_ignores_removed_resume_service(self):
        task = _task(REQUEST_CTX, single_service=True)
        task._last_search_conditions = {"service_id": 999}
        self.assertEqual(task._entry_greeting_branch(), "single_service")

    def test_known_resume_service_uses_resume_search_even_after_history_loss(self):
        task = _task(_chat_ctx())
        task._last_search_conditions = {"service_id": 2, "time_to": "11:59"}
        self.assertEqual(task._entry_greeting_branch(), "resume_search")

    def test_removed_resume_service_is_not_treated_as_known(self):
        task = _task(REQUEST_CTX)
        task._last_search_conditions = {"service_id": 999}
        self.assertEqual(task._entry_greeting_branch(), "continuation")

    def test_dtmf_only_entry_uses_no_request_branch(self):
        task = _task(DTMF_ONLY_CTX)
        self.assertEqual(task._entry_greeting_branch(), "continuation_no_request")

    def test_stated_request_keeps_continuation_branch(self):
        task = _task(REQUEST_CTX)
        self.assertEqual(task._entry_greeting_branch(), "continuation")

    def test_empty_context_is_first_contact(self):
        task = _task(_chat_ctx())
        self.assertEqual(task._entry_greeting_branch(), "first_contact")

    def test_staged_candidate_wins(self):
        staged = SimpleNamespace(
            service_id=1,
            service_name="휴대폰 점검",
            operator_id=None,
            operator_name=None,
            datetime="2026-08-14 10:00",
        )
        task = _task(DTMF_ONLY_CTX, staged=staged)
        self.assertEqual(task._entry_greeting_branch(), "resume_pending")


class EntryGreetingInstructionsTest(TestCase):
    def test_resume_search_injects_grounded_conditions_without_restoring_consent(self):
        task = _task(_chat_ctx())
        task._last_search_conditions = {
            "service_id": 2,
            "start_date": "2026-09-29",
            "end_date": "2026-09-29",
            "time_to": "11:59",
        }
        task._rejected_candidates = [{"datetime": "2026-09-29 10:00", "scope": "slot"}]
        instructions = task._build_greeting_instructions()
        self.assertIn('"service": {"id": 2, "name": "노트북 상담"}', instructions)
        self.assertIn('"time_to": "11:59"', instructions)
        self.assertIn("call search_schedules in the same response", instructions)
        self.assertIn(
            "already processed; there is no need to call reject_candidate again",
            instructions,
        )
        self.assertIn("Past candidates and consent are not restored", instructions)

    def test_no_request_branch_forbids_tool_calls_and_invented_dates(self):
        instructions = _task(DTMF_ONLY_CTX)._build_greeting_instructions()
        self.assertIn("call no tools", instructions)
        self.assertIn("do not say whether anything is available", instructions)
        self.assertIn(
            "Do not treat the calendar's today as a date the caller requested",
            instructions,
        )
        self.assertIn("원하시는 서비스나 기기 문제를 말씀해 주세요.", instructions)

    def test_no_request_branch_skips_the_fresh_greeting(self):
        instructions = _task(DTMF_ONLY_CTX)._build_greeting_instructions()
        self.assertIn(
            'skip greetings and restart phrases like "예약 도와드리겠습니다"',
            instructions,
        )

    def test_continuation_branch_still_asks_for_the_entry_search(self):
        instructions = _task(REQUEST_CTX)._build_greeting_instructions()
        self.assertIn("search_schedules", instructions)

    def test_continuation_branch_uses_policy_instead_of_a_sample_date(self):
        instructions = _task(REQUEST_CTX)._build_greeting_instructions()
        self.assertNotIn('(e.g. "오늘")', instructions)
        self.assertIn(
            "find and validate the earliest candidate over the full lookup range",
            instructions,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_ctx", "service_count", "fixed_question"),
    [
        (DTMF_ONLY_CTX, 2, True),
        (DTMF_ONLY_CTX, 1, False),
        (_chat_ctx(), 2, False),
    ],
)
async def test_on_enter_uses_fixed_question_only_after_identity_with_multiple_services(
    chat_ctx, service_count, fixed_question
):
    task = _task(chat_ctx)
    task._services = task._services[:service_count]
    task._call_session_data = SimpleNamespace(
        call_config_data=SimpleNamespace(language="ko-KR")
    )
    task._record_task_entry = Mock()
    task._build_greeting_instructions = Mock(return_value="greeting instructions")
    session = SimpleNamespace(say=Mock(), generate_reply=Mock())

    with patch.object(DynamicBookingTask, "session", session):
        await task.on_enter()

    task._record_task_entry.assert_called_once_with()
    if fixed_question:
        session.say.assert_called_once_with(
            "원하시는 서비스나 기기 문제를 말씀해 주세요.", add_to_chat_ctx=True
        )
        session.generate_reply.assert_not_called()
        task._build_greeting_instructions.assert_not_called()
    else:
        session.say.assert_not_called()
        session.generate_reply.assert_called_once_with(
            instructions="greeting instructions", allow_interruptions=True
        )
