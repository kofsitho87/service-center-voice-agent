"""Golden coverage for English instructions and configurable response languages.

All runtime instruction branches use English templates (quoted Korean speech
examples stay Korean). Unprefixed fixtures use Korean as the call response
language; en_ fixtures use English as the response language. Both render the
same English instructions. These are rendering checks, not evidence of LLM
behavior.

Refresh only after intentional prompt changes:
    PYTHONPATH=src uv run python tests/characterization/test_dynamic_booking_prompt_rendering.py
"""

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

import tasks.dynamic_booking_task as dbt
from data import CallConfigData
from tasks.dynamic_booking_task import KST, DynamicBookingTask

GOLDEN_DIR = Path(__file__).resolve().parent / "data" / "dynamic_booking_prompts"

# 달력 블록은 now_kst() 에 의존하므로 고정한다. 날짜 자체는 임의값이다.
FIXED_NOW = dt.datetime(2026, 9, 1, 10, 30, tzinfo=KST)

SERVICES = [
    {"id": 10, "name": "휴대폰 점검", "keywords": ["화면 깨짐", "배터리 방전"]},
    {"id": 20, "name": "노트북 상담", "keywords": []},
]
ADVISORS = [
    {"id": 100, "name": "김상담사", "service_ids": [10]},
    {"id": 200, "name": "이상담사", "service_ids": [10, 20]},
]
SINGLE_SERVICE = [{"id": 10, "name": "휴대폰 점검", "keywords": []}]
SINGLE_ADVISOR = [ADVISORS[0]]
FOUR_ADVISORS = [
    {"id": 100 + index, "name": f"검증상담사{index}", "service_ids": [10]}
    for index in range(4)
]

STAGED_WITH_OPERATOR = SimpleNamespace(
    service_id=10,
    service_name="휴대폰 점검",
    operator_id=200,
    operator_name="이상담사",
    datetime="2026-09-03 14:00",
)
STAGED_WITHOUT_OPERATOR = SimpleNamespace(
    service_id=20,
    service_name="노트북 상담",
    operator_id=None,
    operator_name=None,
    datetime="2026-09-05 10:30",
)


def _stub_task(
    *,
    select_operator: bool = True,
    services: list | None = None,
    advisors: list | None = None,
    staged: SimpleNamespace | None = None,
    prior_dialogue: bool = False,
    user_request: bool = False,
    no_progress_turns: int = 0,
    default_language: str = "한국어",
    center_context: str = "",
    search_conditions: dict | None = None,
    rejected_candidates: list | None = None,
) -> DynamicBookingTask:
    """__init__ 을 우회한 렌더 전용 스텁 — 기존 entry-branch 테스트와 같은 패턴."""
    task = object.__new__(DynamicBookingTask)
    task._services = SERVICES if services is None else services
    task._advisors = ADVISORS if advisors is None else advisors
    task._select_operator = select_operator
    task._schedule_lookup_days = 50
    task._staged = staged
    task._entry_has_prior_dialogue = prior_dialogue
    task._entry_has_user_request = user_request
    task._no_progress_turns = no_progress_turns
    task._prompt_lang = "en"
    task._default_language = default_language
    task._center_context = center_context
    task._call_session_data = SimpleNamespace(
        business_data=SimpleNamespace(
            client_name="프롬프트검증센터",
            client_phone="02-2345-6789",
            client_address="서울특별시 검증로 123",
        )
    )
    task._last_search_conditions = search_conditions
    task._rejected_candidates = rejected_candidates
    return task


def _render_language(default_language: str) -> dict[str, str]:
    """Render every branch for one configured response language."""

    def stub(**kwargs) -> DynamicBookingTask:
        return _stub_task(default_language=default_language, **kwargs)

    with mock.patch.object(dbt, "now_kst", return_value=FIXED_NOW):
        return {
            "instructions_select_operator": stub(
                select_operator=True
            )._build_instructions(),
            "instructions_auto_assign": stub(
                select_operator=False
            )._build_instructions(),
            "instructions_single_service_select_operator": stub(
                services=SINGLE_SERVICE
            )._build_instructions(),
            "instructions_single_service_auto_assign": stub(
                services=SINGLE_SERVICE, select_operator=False
            )._build_instructions(),
            "instructions_resume_pending": stub(
                select_operator=True, staged=STAGED_WITH_OPERATOR
            )._build_instructions(),
            "greeting_first_contact": stub()._build_greeting_instructions(),
            "greeting_continuation_multi": stub(
                prior_dialogue=True, user_request=True
            )._build_greeting_instructions(),
            "greeting_single_service_auto_first_contact": stub(
                services=SINGLE_SERVICE, select_operator=False
            )._build_greeting_instructions(),
            "greeting_single_service_auto_continuation": stub(
                services=SINGLE_SERVICE,
                select_operator=False,
                prior_dialogue=True,
            )._build_greeting_instructions(),
            "greeting_single_service_auto_with_request": stub(
                services=SINGLE_SERVICE,
                select_operator=False,
                prior_dialogue=True,
                user_request=True,
            )._build_greeting_instructions(),
            "greeting_single_service_select_with_request": stub(
                services=SINGLE_SERVICE,
                prior_dialogue=True,
                user_request=True,
            )._build_greeting_instructions(),
            "greeting_single_service_select_no_request": stub(
                services=SINGLE_SERVICE, prior_dialogue=True
            )._build_greeting_instructions(),
            "greeting_single_service_one_advisor_first_contact": stub(
                services=SINGLE_SERVICE, advisors=SINGLE_ADVISOR
            )._build_greeting_instructions(),
            "greeting_single_service_one_advisor_with_request": stub(
                services=SINGLE_SERVICE,
                advisors=SINGLE_ADVISOR,
                prior_dialogue=True,
                user_request=True,
            )._build_greeting_instructions(),
            "greeting_single_service_four_advisors": stub(
                services=SINGLE_SERVICE, advisors=FOUR_ADVISORS
            )._build_greeting_instructions(),
            "greeting_continuation_no_request_multi": stub(
                prior_dialogue=True
            )._build_greeting_instructions(),
            "greeting_resume_pending": stub(
                staged=STAGED_WITHOUT_OPERATOR
            )._build_greeting_instructions(),
            "greeting_resume_search": stub(
                search_conditions={
                    "service_id": 10,
                    "start_date": "2026-09-03",
                    "end_date": "2026-09-03",
                    "time_to": "11:59",
                },
                rejected_candidates=[{"datetime": "2026-09-03 10:00", "scope": "slot"}],
            )._build_greeting_instructions(),
            "no_progress_soft": stub(no_progress_turns=20)._no_progress_instruction(),
            "no_progress_near_hard": stub(
                no_progress_turns=28
            )._no_progress_instruction(),
        }


def render_all() -> dict[str, str]:
    """Fixture names distinguish call response languages, not template languages."""
    rendered = dict(_render_language("한국어"))
    rendered.update(
        (f"en_{name}", text) for name, text in _render_language("English").items()
    )
    return rendered


class PromptRenderGoldenTest(TestCase):
    def test_rendering_matches_goldens(self):
        rendered = render_all()
        for name, text in rendered.items():
            with self.subTest(name=name):
                golden_path = GOLDEN_DIR / f"{name}.txt"
                self.assertTrue(
                    golden_path.exists(),
                    f"golden 없음: {golden_path} — 모듈 docstring의 갱신 명령 참고",
                )
                self.assertEqual(golden_path.read_text(encoding="utf-8"), text)

    def test_no_stale_goldens(self):
        expected = {f"{name}.txt" for name in render_all()}
        actual = {p.name for p in GOLDEN_DIR.glob("*.txt")}
        self.assertEqual(expected, actual)


class SingleServicePromptTest(TestCase):
    def _greeting(self, **kwargs) -> str:
        task = _stub_task(services=SINGLE_SERVICE, **kwargs)
        with mock.patch.object(dbt, "now_kst", return_value=FIXED_NOW):
            return task._build_greeting_instructions()

    def test_auto_assign_searches_exact_code_computed_calendar_range(self):
        task = _stub_task(services=SINGLE_SERVICE, select_operator=False)
        with mock.patch.object(dbt, "now_kst", return_value=FIXED_NOW):
            greeting = task._build_greeting_instructions()
            instructions = task._build_instructions()
            self.assertIsNone(task._validate_range("2026-09-01", "2026-10-21"))
        self.assertIn('mode="earliest_for_booking"', greeting)
        self.assertIn("service_id=10", greeting)
        self.assertIn('start_date="2026-09-01"', greeting)
        self.assertIn('end_date="2026-10-21"', greeting)
        self.assertIn("2026-09-01부터 2026-10-21까지", instructions)
        self.assertIn("in this same first response", greeting)
        self.assertIn("stage_booking", greeting)
        self.assertIn('service name "휴대폰 점검" only once', greeting)
        self.assertIn("confirmation.operator_name", greeting)
        self.assertNotIn("어떤 서비스", greeting)

    def test_select_with_one_eligible_advisor_searches_without_advisor_question(self):
        advisors = [
            ADVISORS[0],
            {"id": 999, "name": "다른서비스상담사", "service_ids": [20]},
        ]
        greeting = self._greeting(advisors=advisors)
        self.assertIn('search_schedules(mode="earliest_for_booking"', greeting)
        self.assertNotIn("원하시는 상담사", greeting)
        self.assertNotIn("다른서비스상담사", greeting)
        self.assertIn("confirmation.operator_name", greeting)

    def test_select_names_two_or_three_eligible_advisors_and_waits(self):
        for count in (2, 3):
            advisors = [
                *FOUR_ADVISORS[:count],
                {"id": 999, "name": "다른서비스상담사", "service_ids": [20]},
            ]
            with self.subTest(count=count):
                greeting = self._greeting(advisors=advisors)
                for advisor in FOUR_ADVISORS[:count]:
                    self.assertIn(advisor["name"], greeting)
                self.assertNotIn("다른서비스상담사", greeting)
                self.assertIn("원하시는 상담사 있으세요?", greeting)
                self.assertIn("Call no tools this turn", greeting)
                self.assertNotIn("search_schedules(", greeting)

    def test_select_with_four_advisors_asks_without_listing_names(self):
        greeting = self._greeting(advisors=FOUR_ADVISORS)
        self.assertIn('Ask: "원하시는 상담사 있으세요?"', greeting)
        for advisor in FOUR_ADVISORS:
            self.assertNotIn(advisor["name"], greeting)
        self.assertNotIn("search_schedules(", greeting)

    def test_greeting_matches_prior_dialogue(self):
        first = self._greeting(select_operator=False)
        continuation = self._greeting(select_operator=False, prior_dialogue=True)
        self.assertIn('greet briefly with "안녕하세요."', first)
        self.assertIn("Never invent a center name or your own name", first)
        self.assertIn(
            'skip greetings and restart phrases like "예약 도와드리겠습니다"',
            continuation,
        )
        self.assertNotIn('"안녕하세요."', continuation)

    def test_no_request_does_not_treat_calendar_today_as_caller_condition(self):
        greeting = self._greeting(select_operator=False, prior_dialogue=True)
        self.assertIn(
            "The caller has not yet stated a service, date, time, or advisor", greeting
        )
        self.assertIn(
            "Do not treat the calendar's today as a date the caller requested", greeting
        )
        self.assertIn('search_schedules(mode="earliest_for_booking"', greeting)

    def test_existing_request_preserves_conditions_before_default_advisor_step(self):
        greeting = self._greeting(prior_dialogue=True, user_request=True)
        self.assertIn(
            "Keep the date, time, and advisor the caller already stated", greeting
        )
        self.assertIn("fix the service to the auto-assigned one", greeting)
        self.assertIn(
            "if there is a date/time, without asking, call search_schedules with "
            "those conditions and service_id=10",
            greeting,
        )
        self.assertIn("Pass a requested advisor as operator_id", greeting)
        self.assertIn('report_booking_obstacle(subject="advisor"', greeting)
        self.assertIn('report_booking_obstacle(subject="service"', greeting)
        self.assertIn('mode="earliest"', greeting)
        self.assertIn('start_date="2026-09-01", end_date="2026-10-21"', greeting)
        self.assertIn("no date/time", greeting)
        self.assertLess(
            greeting.index("if there is a date/time, without asking"),
            greeting.index("원하시는 상담사 있으세요?"),
        )

    def test_booking_only_request_retains_auto_search_step(self):
        greeting = self._greeting(
            select_operator=False, prior_dialogue=True, user_request=True
        )
        self.assertIn("If there is only booking intent with no date/time", greeting)
        self.assertIn('search_schedules(mode="earliest_for_booking"', greeting)
        self.assertNotIn("원하시는 상담사", greeting)

    def test_single_service_system_block_is_present_in_both_advisor_modes(self):
        for select_operator in (False, True):
            task = _stub_task(
                services=SINGLE_SERVICE, select_operator=select_operator
            )
            with self.subTest(select_operator=select_operator):
                prompt = task._build_instructions()
                self.assertIn("[Service auto-assigned]", prompt)
                self.assertIn('"휴대폰 점검"(id: 10)', prompt)
                self.assertIn("service_id=10", prompt)
        self.assertNotIn("[Service auto-assigned]", _stub_task()._build_instructions())

    def test_single_service_renders_configured_response_language(self):
        for language in ("한국어", "English"):
            for user_request in (False, True):
                with self.subTest(language=language, user_request=user_request):
                    greeting = self._greeting(
                        default_language=language, user_request=user_request
                    )
                    self.assertIn(language, greeting)
                    self.assertNotIn("{default_language}", greeting)


# English instructions take ~1.8x the characters of the former Korean ones but
# ~11% fewer tokens; cap = English max (17,711) + ~5% (Korean max was 9,969).
PROMPT_BUDGET_CHARS = 18_600


class PromptLanguageTest(TestCase):
    def test_center_knowledge_is_literal_data_and_booking_continues(self):
        context = '주차 요금은 30분에 1,000원입니다. {"floor": 2}'
        prompt = _stub_task(center_context=context)._build_instructions()
        self.assertEqual(prompt.count(context), 1)
        self.assertIn("<service_center_knowledge>", prompt)
        self.assertIn("Center name: 프롬프트검증센터", prompt)
        self.assertIn("Phone: 02-2345-6789", prompt)
        self.assertIn("Address: 서울특별시 검증로 123", prompt)
        self.assertIn(
            "If a candidate was being confirmed before an info question, "
            "earlier consent is void",
            prompt,
        )
        self.assertIn(
            'Never call commit_booking from an info answer or a later bare "네"',
            prompt,
        )
        self.assertNotIn("route_target=info_request", prompt)

    def test_missing_center_knowledge_does_not_invite_guessing(self):
        prompt = _stub_task()._build_instructions()
        self.assertIn(
            "[General center info — not evidence of availability]\nnone", prompt
        )
        self.assertIn("if no info, say it's hard to confirm", prompt)

    def test_rendered_instructions_stay_within_compact_budget(self):
        # The injected center context is variable length; this protects the
        # fixed instruction template rendered with an empty context, including
        # the fixed-constraint/no-progress handoff rule.
        for language in ("한국어", "English"):
            for name, text in _render_language(language).items():
                if not name.startswith("instructions_"):
                    continue
                with self.subTest(language=language, name=name):
                    # Raised from 9,000 for the phrase = date calendar;
                    # re-based for the English templates (see PROMPT_BUDGET_CHARS).
                    self.assertLessEqual(len(text), PROMPT_BUDGET_CHARS)

    def test_natural_phone_style_is_part_of_every_instruction_variant(self):
        for name, text in _render_language("한국어").items():
            if not name.startswith("instructions_"):
                continue
            with self.subTest(name=name):
                self.assertIn("Continue from what you just heard", text)
                self.assertIn("verb-centered colloquial style", text)
                self.assertIn("If only part is unclear", text)
                self.assertIn("ask for one decision at a time", text)

    def test_tool_descriptions_are_concise_and_keep_critical_contracts(self):
        descriptions = {
            name: getattr(DynamicBookingTask, name).info.description
            for name in (
                "search_schedules",
                "stage_booking",
                "commit_booking",
                "exit_task",
            )
        }

        # Raised from 2,300 for the spoken-date / date_corrected contract.
        self.assertLessEqual(len(descriptions["search_schedules"]), 2_500)
        self.assertLessEqual(len(descriptions["stage_booking"]), 700)
        self.assertLessEqual(len(descriptions["commit_booking"]), 500)
        self.assertLessEqual(len(descriptions["exit_task"]), 1_100)
        self.assertIn("status=error", descriptions["search_schedules"])
        self.assertIn("date-only ``YYYY-MM-DD``", descriptions["search_schedules"])
        self.assertIn(
            "Omit unused args or use JSON null", descriptions["search_schedules"]
        )
        self.assertIn(
            "Search results alone never authorize a booking-consent question",
            descriptions["search_schedules"],
        )
        self.assertIn("confirmation", descriptions["stage_booking"])
        self.assertIn("not consent", descriptions["commit_booking"])
        self.assertIn("route_target", descriptions["exit_task"])
        self.assertNotIn("info_request", descriptions["exit_task"])
        self.assertIn(
            "Answer service center information here", descriptions["exit_task"]
        )
        self.assertIn("Never use transfer", descriptions["exit_task"])
        self.assertIn("do not merely", descriptions["exit_task"].lower())

    def test_required_tool_turn_forbids_promise_only_reply(self):
        instructions = dbt._prompt_sections()["en"]["instructions"]
        self.assertIn('Never end with only saying "확인해 드릴게요"', instructions)
        self.assertIn(
            "you must call the actual tool in the same response", instructions
        )
        self.assertIn(
            'Never ask "예약 신청해 드릴까요?" based on search_schedules results alone',
            instructions,
        )
        self.assertIn(
            'call exit_task(reason_code="cancel") in the same response',
            instructions,
        )
        self.assertIn(
            'route_target="transfer"; use reason_code="staff_connection"',
            instructions,
        )
        self.assertIn(
            "Never combine different options, such as handoff and search, "
            "in one question",
            instructions,
        )

    def test_off_list_appointment_or_advisor_is_reported_to_the_handoff_controller(self):
        sections = dbt._prompt_sections()["en"]
        instructions = sections["instructions"]
        # The controller speaks the offer; the model only reports the obstacle and
        # classifies the reply. No refusal-first or exit_task consent protocol.
        self.assertIn('report_booking_obstacle(subject="service"', instructions)
        self.assertIn("answer_handoff_offer", instructions)
        self.assertIn("데이터 복구로 해야 돼", instructions)
        self.assertNotIn("service_not_offered", instructions)
        self.assertNotIn("handoff_accepted", instructions)
        self.assertNotIn("handoff_offer_due", instructions)
        for key in ("operator_rule_6_select", "operator_rule_6_auto"):
            with self.subTest(key=key):
                self.assertIn('subject="advisor"', sections[key])
                self.assertNotIn("service_not_offered", sections[key])

    def test_auto_advisor_rules_use_confirmation_field_as_speech_boundary(self):
        sections = dbt._prompt_sections()
        for key in ("operator_mode_block_auto", "operator_rule_6_auto"):
            with self.subTest(key=key):
                rule = sections["en"][key]
                self.assertIn("confirmation.operator_name", rule)
                self.assertIn("staged", rule)
                self.assertNotIn("confirmation question includes the name", rule)

    def test_english_is_the_only_template_language(self):
        sections = dbt._prompt_sections()
        self.assertEqual(set(sections), {"en"})

    def test_english_instructions_preserve_call_response_language(self):
        for language, expected in [
            ("ko-KR", ("en", "한국어")),
            ("en-US", ("en", "English")),
        ]:
            with self.subTest(language=language):
                call_config = CallConfigData(language=language)
                session_data = SimpleNamespace(call_config_data=call_config)
                self.assertEqual(dbt._prompt_language(session_data), expected)

    def test_missing_call_config_defaults_to_korean_speech(self):
        session_data = SimpleNamespace(call_config_data=None)
        self.assertEqual(dbt._prompt_language(session_data), ("en", "한국어"))

    def test_english_prompts_inject_default_language(self):
        for language in ("한국어", "English"):
            with self.subTest(language=language):
                task = _stub_task(default_language=language)
                instructions = task._build_instructions()
                self.assertIn(language, instructions)
                self.assertNotIn("{default_language}", instructions)
                greeting = task._build_greeting_instructions()
                self.assertIn(language, greeting)
                self.assertNotIn("{default_language}", greeting)


if __name__ == "__main__":
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in render_all().items():
        (GOLDEN_DIR / f"{name}.txt").write_text(text, encoding="utf-8")
        print(f"wrote {name}.txt ({len(text)} chars)")
