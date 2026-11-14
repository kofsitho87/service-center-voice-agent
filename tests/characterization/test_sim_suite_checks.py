"""sim-suite 결정론적 계층 단위 테스트.

네트워크·서브프로세스·LLM 없이 events 파서, outcome 도출, 각 체크,
스키마 검증, 저지 JSON 파서만 검증한다.
`tools/` 는 pyproject 의 pytest pythonpath 에 등록되어 있다.
"""

from __future__ import annotations

import json
import textwrap
import urllib.request
from pathlib import Path

import pytest
import yaml
from sim_suite.checks import run_checks
from sim_suite.events import ParsedEvents, derive_outcome, parse_events
from sim_suite.judge import parse_judge_output
from sim_suite.schema import (
    Expect,
    ScenarioError,
    load_scenario,
    load_suite,
)

AGENT = "에이전트"
CUSTOMER = "고객"


# ------------------------------------------------------------------ helpers


# Tests that load the repository's real suite (simulations/dial_info/ and
# simulations/scenarios/) are skipped when those directories are absent.
_REPO_ROOT = Path(__file__).resolve().parents[2]
requires_regenerated_simulations = pytest.mark.skipif(
    not (_REPO_ROOT / "simulations" / "dial_info").is_dir(),
    reason="simulations/dial_info and scenarios not present",
)


def write_events(tmp_path: Path, events: list[dict | str]) -> Path:
    lines = []
    for event in events:
        if isinstance(event, str):
            lines.append(event)
        else:
            lines.append(json.dumps(event, ensure_ascii=False))
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def agent(content: str) -> dict:
    return {"type": "agent_msg", "content": content}


def customer(content: str) -> dict:
    return {"type": "customer_msg", "source": "auto", "content": content}


def tool(name: str) -> dict:
    return {"type": "tool", "name": name}


def handoff(text: str) -> dict:
    return {"type": "handoff", "text": f"Handoff: {text}"}


END = {"type": "session_end"}


def parsed_from(events: list[dict | str], tmp_path: Path) -> ParsedEvents:
    return parse_events(write_events(tmp_path, events))


def make_expect(**kwargs) -> Expect:
    kwargs.setdefault("outcomes", ["info_answered"])
    kwargs.setdefault("judge", "- 루브릭")
    return Expect(**kwargs)


# ------------------------------------------------------------------- events


def test_parse_events_reads_tool_after_session_end(tmp_path):
    parsed = parsed_from(
        [
            agent("안녕하세요"),
            customer("영업시간 알려주세요"),
            agent("7시부터입니다"),
            END,
            tool("end_call_by_user_request"),
        ],
        tmp_path,
    )
    assert parsed.session_ended is True
    assert parsed.tools == ["end_call_by_user_request"]
    assert parsed.customer_turns == 1
    assert parsed.agent_msgs_after_first_customer() == 1


def test_parse_events_tolerates_bad_lines(tmp_path):
    parsed = parsed_from(
        [
            agent("안녕하세요"),
            "this is not json",
            '{"type": "agent_msg", "content": "잘림',
            "[1, 2, 3]",
            customer("네"),
            END,
        ],
        tmp_path,
    )
    assert parsed.bad_lines == 3
    assert parsed.agent_msgs == ["안녕하세요"]
    assert parsed.customer_msgs == ["네"]


def test_parse_events_missing_file_is_empty():
    parsed = parse_events(Path("/nonexistent/events.jsonl"))
    assert parsed.has_events is False
    assert parsed.session_ended is False
    assert derive_outcome(parsed) == "no_outcome"


def test_transcript_keeps_order(tmp_path):
    parsed = parsed_from(
        [agent("A"), customer("B"), agent("C")],
        tmp_path,
    )
    assert parsed.transcript == [(AGENT, "A"), (CUSTOMER, "B"), (AGENT, "C")]
    assert "에이전트: A" in parsed.transcript_text()


def test_parser_removes_console_log_suffix_and_status_marker(tmp_path):
    parsed = parsed_from(
        [
            agent(
                "예약 신청이 접수되었습니다. 16:09:13.570 DEBUG "
                "livekit.agents conversation_item_added"
            ),
            agent("통화를 종료하겠습니다. ✓"),
            END,
        ],
        tmp_path,
    )
    assert parsed.agent_msgs == [
        "예약 신청이 접수되었습니다.",
        "통화를 종료하겠습니다.",
    ]


def test_quality_transcript_excludes_harness_closing_and_duplicates(tmp_path):
    parsed = parsed_from(
        [
            customer("없어요"),
            agent("통화를 종료하겠습니다."),
            agent("통화를 종료하겠습니다. ✓"),
            agent("응답지연으로 인해 통화를 종료합니다."),
            END,
        ],
        tmp_path,
    )
    quality = parsed.quality_transcript_text()
    assert quality.count("통화를 종료하겠습니다") == 1
    assert "응답지연" not in quality


# ------------------------------------------------------------------ outcome


def test_outcome_booking_created_requires_commit_booking(tmp_path):
    """create_new_appointment 은 진입 도구라 완료 신호가 아니다."""
    parsed = parsed_from(
        [
            agent("예약 도와드릴게요"),
            customer("네"),
            tool("create_new_appointment"),
            END,
        ],
        tmp_path,
    )
    assert derive_outcome(parsed) == "no_outcome"


def test_outcome_booking_created_requires_commit_and_success_message(tmp_path):
    parsed = parsed_from(
        [
            customer("예약이요"),
            tool("create_new_appointment"),
            tool("commit_booking"),
            agent("예약 신청이 접수되었습니다."),
            END,
        ],
        tmp_path,
    )
    assert derive_outcome(parsed) == "booking_created"

    missing_announcement = parsed_from(
        [customer("예약이요"), tool("commit_booking"), END],
        tmp_path,
    )
    assert derive_outcome(missing_announcement) != "booking_created"


def test_booking_success_detection_tolerates_missing_display_spaces(tmp_path):
    parsed = parsed_from(
        [
            customer("예약할게요"),
            tool("commit_booking"),
            agent("예약 신청이접수 되었습니다."),
            END,
        ],
        tmp_path,
    )
    assert derive_outcome(parsed) == "booking_created"


def test_outcome_booking_modified(tmp_path):
    parsed = parsed_from(
        [
            customer("변경이요"),
            tool("modify_appointment"),
            agent("예약 변경이 접수되었습니다."),
            END,
        ],
        tmp_path,
    )
    assert derive_outcome(parsed) == "booking_modified"


def test_modify_tool_without_success_text_is_not_modified(tmp_path):
    """도구 이벤트는 호출 시점 기록이다. 성공 문구가 없으면 완료가 아니다."""
    parsed = parsed_from(
        [customer("변경이요"), tool("modify_appointment"), agent("확인 중입니다."), END],
        tmp_path,
    )
    assert derive_outcome(parsed) != "booking_modified"


def test_outcome_booking_cancelled(tmp_path):
    parsed = parsed_from(
        [
            customer("취소요"),
            tool("cancel_appointment"),
            agent("예약 취소가 접수되었습니다."),
            END,
        ],
        tmp_path,
    )
    assert derive_outcome(parsed) == "booking_cancelled"


def test_cancel_tool_without_success_text_is_not_cancelled(tmp_path):
    parsed = parsed_from(
        [customer("취소요"), tool("cancel_appointment"), agent("확인 중입니다."), END],
        tmp_path,
    )
    assert derive_outcome(parsed) != "booking_cancelled"


def test_outcome_no_reservation_for_modify(tmp_path):
    parsed = parsed_from(
        [
            customer("예약 변경하고 싶어요"),
            tool("modify_appointment"),
            agent("변경할 예약내역이 없습니다."),
            END,
        ],
        tmp_path,
    )
    assert derive_outcome(parsed) == "no_reservation"


def test_outcome_no_reservation_for_cancel(tmp_path):
    parsed = parsed_from(
        [
            customer("예약 취소해 주세요"),
            tool("cancel_appointment"),
            agent("취소할 예약내역이 없습니다."),
            END,
        ],
        tmp_path,
    )
    assert derive_outcome(parsed) == "no_reservation"


def test_no_reservation_loses_to_memo(tmp_path):
    parsed = parsed_from(
        [
            customer("취소요"),
            agent("취소할 예약내역이 없습니다."),
            handoff("LeaveMessageTask"),
            END,
        ],
        tmp_path,
    )
    assert derive_outcome(parsed) == "memo_left"


def test_outcome_memo_left_wins_over_booking_tool(tmp_path):
    """NO_SLOT 메모 경로: 예약 도구가 먼저 호출돼도 memo_left 로 분류한다."""
    parsed = parsed_from(
        [
            customer("예약이요"),
            tool("search_schedules"),
            tool("commit_booking"),
            handoff("LeaveMessageTask"),
            tool("submit_booking_contact"),
            END,
        ],
        tmp_path,
    )
    assert derive_outcome(parsed) == "memo_left"


def test_outcome_memo_left_from_tool_only(tmp_path):
    parsed = parsed_from(
        [customer("메모"), tool("collect_leave_message"), END], tmp_path
    )
    assert derive_outcome(parsed) == "memo_left"


def test_outcome_transferred(tmp_path):
    parsed = parsed_from(
        [customer("상담원 연결해 주세요"), tool("transfer_call"), END], tmp_path
    )
    assert derive_outcome(parsed) == "transferred"


def test_outcome_transferred_loses_to_memo(tmp_path):
    parsed = parsed_from(
        [
            customer("상담원이요"),
            tool("transfer_call"),
            handoff("LeaveMessageTask(is_after_transfer_failure=True)"),
            END,
        ],
        tmp_path,
    )
    assert derive_outcome(parsed) == "memo_left"


def test_outcome_ended_by_agent(tmp_path):
    parsed = parsed_from(
        [
            agent("안녕하세요"),
            customer("끊을게요"),
            END,
            tool("end_call_by_user_request"),
        ],
        tmp_path,
    )
    assert derive_outcome(parsed) == "ended_by_agent"


def test_outcome_info_answered(tmp_path):
    parsed = parsed_from(
        [agent("안녕하세요"), customer("주차요"), agent("지하 3층입니다"), END],
        tmp_path,
    )
    assert derive_outcome(parsed) == "info_answered"


def test_outcome_no_outcome_when_not_ended(tmp_path):
    parsed = parsed_from(
        [agent("안녕하세요"), customer("주차요"), agent("지하 3층입니다")], tmp_path
    )
    assert derive_outcome(parsed) == "no_outcome"


def test_outcome_no_outcome_without_customer(tmp_path):
    parsed = parsed_from([agent("안녕하세요"), END], tmp_path)
    assert derive_outcome(parsed) == "no_outcome"


# ------------------------------------------------------------------- checks


def by_name(results, prefix):
    return [item for item in results if item.name.startswith(prefix)]


def test_session_ended_cleanly_pass_and_fail(tmp_path):
    parsed = parsed_from([agent("A"), customer("B"), agent("C"), END], tmp_path)
    ok = run_checks(make_expect(), parsed, "info_answered", timed_out=False)
    assert by_name(ok, "session_ended_cleanly")[0].passed is True

    timed = run_checks(make_expect(), parsed, "info_answered", timed_out=True)
    check = by_name(timed, "session_ended_cleanly")[0]
    assert check.passed is False
    assert "timed out" in check.detail

    unfinished = parsed_from([agent("A"), customer("B")], tmp_path)
    results = run_checks(make_expect(), unfinished, "no_outcome", timed_out=False)
    assert by_name(results, "session_ended_cleanly")[0].passed is False


def test_outcome_check(tmp_path):
    parsed = parsed_from([agent("A"), customer("B"), agent("C"), END], tmp_path)
    ok = run_checks(make_expect(outcomes=["info_answered"]), parsed, "info_answered")
    assert by_name(ok, "outcome")[0].passed is True

    bad = run_checks(make_expect(outcomes=["booking_created"]), parsed, "info_answered")
    assert by_name(bad, "outcome")[0].passed is False


def test_tools_any_and_none(tmp_path):
    parsed = parsed_from(
        [customer("예약"), tool("create_new_appointment"), END], tmp_path
    )
    expect = make_expect(
        outcomes=["booking_created"],
        tools_any=["create_new_appointment", "commit_booking"],
        tools_none=["transfer_call"],
    )
    results = run_checks(expect, parsed, "booking_created")
    assert by_name(results, "tools_any")[0].passed is True
    assert by_name(results, "tools_none")[0].passed is True

    expect_fail = make_expect(
        outcomes=["booking_created"],
        tools_any=["modify_appointment"],
        tools_none=["create_new_appointment"],
    )
    results = run_checks(expect_fail, parsed, "booking_created")
    assert by_name(results, "tools_any")[0].passed is False
    assert by_name(results, "tools_none")[0].passed is False


def test_tools_checks_are_skipped_when_not_declared(tmp_path):
    parsed = parsed_from([customer("A"), END], tmp_path)
    results = run_checks(make_expect(outcomes=["no_outcome"]), parsed, "no_outcome")
    assert by_name(results, "tools_any") == []
    assert by_name(results, "tools_none") == []


def test_handoffs_any_and_none(tmp_path):
    parsed = parsed_from(
        [customer("메모"), handoff("LeaveMessageTask started"), END], tmp_path
    )
    ok = make_expect(
        outcomes=["memo_left"],
        handoffs_any=["LeaveMessageTask"],
        handoffs_none=["ScheduleSelectionTask"],
    )
    results = run_checks(ok, parsed, "memo_left")
    assert by_name(results, "handoffs_any")[0].passed is True
    assert by_name(results, "handoffs_none")[0].passed is True

    bad = make_expect(
        outcomes=["memo_left"],
        handoffs_any=["ScheduleSelectionTask"],
        handoffs_none=["LeaveMessageTask"],
    )
    results = run_checks(bad, parsed, "memo_left")
    assert by_name(results, "handoffs_any")[0].passed is False
    assert by_name(results, "handoffs_none")[0].passed is False


def test_customer_turns_bounds(tmp_path):
    parsed = parsed_from([customer("1"), customer("2"), customer("3"), END], tmp_path)
    ok = run_checks(
        make_expect(outcomes=["no_outcome"], customer_turns_min=2, customer_turns_max=5),
        parsed,
        "no_outcome",
    )
    assert by_name(ok, "customer_turns")[0].passed is True

    too_few = run_checks(
        make_expect(outcomes=["no_outcome"], customer_turns_min=4),
        parsed,
        "no_outcome",
    )
    assert by_name(too_few, "customer_turns")[0].passed is False

    too_many = run_checks(
        make_expect(outcomes=["no_outcome"], customer_turns_max=2),
        parsed,
        "no_outcome",
    )
    assert by_name(too_many, "customer_turns")[0].passed is False


def test_agent_regex_max(tmp_path):
    parsed = parsed_from(
        [agent("잘 못 들었습니다"), agent("잘 못 들었습니다"), customer("네"), END],
        tmp_path,
    )
    from sim_suite.schema import RegexMax

    ok = run_checks(
        make_expect(
            outcomes=["no_outcome"], agent_regex_max=[RegexMax("잘 못 들었", 2)]
        ),
        parsed,
        "no_outcome",
    )
    assert by_name(ok, "agent_regex_max")[0].passed is True

    bad = run_checks(
        make_expect(
            outcomes=["no_outcome"], agent_regex_max=[RegexMax("잘 못 들었", 1)]
        ),
        parsed,
        "no_outcome",
    )
    check = by_name(bad, "agent_regex_max")[0]
    assert check.passed is False
    assert "matched 2" in check.detail


def test_agent_regex_any(tmp_path):
    parsed = parsed_from(
        [agent("상담원 연결이나 메모를 남겨드릴까요?"), customer("아니요"), END],
        tmp_path,
    )
    ok = run_checks(
        make_expect(
            outcomes=["info_answered"],
            agent_regex_any=["상담원.{0,10}연결", "메모.{0,10}전달"],
        ),
        parsed,
        "info_answered",
    )
    assert by_name(ok, "agent_regex_any")[0].passed is True

    bad = run_checks(
        make_expect(
            outcomes=["info_answered"],
            agent_regex_any=["예약 확정", "취소 접수"],
        ),
        parsed,
        "info_answered",
    )
    assert by_name(bad, "agent_regex_any")[0].passed is False


def test_api_request_checks(tmp_path):
    parsed = parsed_from([customer("예약 변경"), END], tmp_path)
    expect = make_expect(
        outcomes=["no_outcome"],
        api_requests_any=[r"GET /record/.+/appointment/confirm"],
        api_requests_none=[r"schedules/available"],
    )
    results = run_checks(
        expect,
        parsed,
        "no_outcome",
        api_requests=["GET /record/sim-fixture/appointment/confirm"],
    )
    assert by_name(results, "api_requests_any")[0].passed is True
    assert by_name(results, "api_requests_none")[0].passed is True


def test_agent_regex_none(tmp_path):
    parsed = parsed_from(
        [agent("2026-09-17에 예약됩니다"), customer("네"), END], tmp_path
    )
    bad = run_checks(
        make_expect(outcomes=["no_outcome"], agent_regex_none=[r"\d{4}-\d{2}-\d{2}"]),
        parsed,
        "no_outcome",
    )
    assert by_name(bad, "agent_regex_none")[0].passed is False

    clean = parsed_from([agent("9월 17일에 예약됩니다"), customer("네"), END], tmp_path)
    ok = run_checks(
        make_expect(outcomes=["no_outcome"], agent_regex_none=[r"\d{4}-\d{2}-\d{2}"]),
        clean,
        "no_outcome",
    )
    assert by_name(ok, "agent_regex_none")[0].passed is True


# ------------------------------------------------------------------- schema


SUITE_YAML = """
defaults:
  agent_model: "openai:gpt-5.6-luna"
  judge_model: "openai:gpt-5.4-mini"
  concurrency: 2
  timeout_sec: 300
  away_timeout: 60
  stop_on_transfer: true
  agent_regex_none: ["\\\\*\\\\*"]
fixtures:
  voice_leave_memo: simulations/dial_info/voice_leave_memo.yaml
scenario_dirs: [simulations/scenarios]
phases:
  1: "create"
"""

SCENARIO_BASE = {
    "id": "demo_case",
    "title": "데모",
    "intent": "info_inquiry",
    "difficulty": "easy",
    "tags": ["smoke"],
    "dial_info": "voice_leave_memo",
    "phase": 1,
    "persona": {
        "name": "가상 고객",
        "verification_code": "1987-09-04",
        "age_gender": "38세 남성",
        "personality": "정중함",
        "goal": "영업시간 문의",
        "scenario": "1. 영업시간을 묻는다.",
    },
    "expect": {"outcome": "info_answered", "judge": "- 루브릭"},
}


@pytest.fixture
def fake_repo(tmp_path):
    (tmp_path / "simulations" / "dial_info").mkdir(parents=True)
    (tmp_path / "simulations" / "scenarios").mkdir(parents=True)
    (tmp_path / "simulations" / "dial_info" / "voice_leave_memo.yaml").write_text(
        textwrap.dedent(
            """
            call_config_data:
              language: ko-KR
            business_data:
              site_id: demo-device-care
              client_id: 9001
            """
        ),
        encoding="utf-8",
    )
    suite_path = tmp_path / "simulations" / "suite.yaml"
    suite_path.write_text(SUITE_YAML, encoding="utf-8")
    return tmp_path


def write_scenario(repo: Path, data: dict, name: str | None = None) -> Path:
    name = name or f"{data['id']}.yaml"
    path = repo / "simulations" / "scenarios" / name
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def load(repo: Path, data: dict, name: str | None = None):
    suite = load_suite(repo / "simulations" / "suite.yaml")
    return load_scenario(write_scenario(repo, data, name), suite)


def test_valid_scenario_loads_and_merges_suite_regex(fake_repo):
    scenario = load(fake_repo, dict(SCENARIO_BASE))
    assert scenario.id == "demo_case"
    assert scenario.expect.outcomes == ["info_answered"]
    assert scenario.expect.agent_regex_none == [r"\*\*"]
    assert scenario.run.timeout_sec == 300


def test_scenario_resolves_api_fixture(fake_repo):
    fixture = fake_repo / "simulations" / "api_fixtures" / "customer.yaml"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("routes: {/health: {json: {ok: true}}}\n", encoding="utf-8")
    data = dict(SCENARIO_BASE)
    data["api_fixture"] = "simulations/api_fixtures/customer.yaml"
    scenario = load(fake_repo, data)
    assert scenario.api_fixture == fixture.resolve()


def test_scripted_replies_must_be_non_empty_strings(fake_repo):
    data = dict(SCENARIO_BASE)
    data["persona"] = {**SCENARIO_BASE["persona"], "scripted_replies": []}
    with pytest.raises(ScenarioError, match="scripted_replies"):
        load(fake_repo, data)


def test_suite_regex_merge_dedupes(fake_repo):
    data = dict(SCENARIO_BASE)
    data["expect"] = {
        "outcome": "info_answered",
        "judge": "- 루브릭",
        "agent_regex_none": [r"\*\*", r"\d{2}:\d{2}"],
    }
    scenario = load(fake_repo, data)
    assert scenario.expect.agent_regex_none == [r"\*\*", r"\d{2}:\d{2}"]


def test_missing_outcome_is_error(fake_repo):
    data = dict(SCENARIO_BASE)
    data["expect"] = {"judge": "- 루브릭"}
    with pytest.raises(ScenarioError) as exc:
        load(fake_repo, data)
    assert "exactly one of outcome / outcome_any" in exc.value.message


def test_both_outcome_and_outcome_any_is_error(fake_repo):
    data = dict(SCENARIO_BASE)
    data["expect"] = {
        "outcome": "info_answered",
        "outcome_any": ["info_answered"],
        "judge": "- 루브릭",
    }
    with pytest.raises(ScenarioError) as exc:
        load(fake_repo, data)
    assert "exactly one of outcome / outcome_any" in exc.value.message


def test_unknown_outcome_value_is_error(fake_repo):
    data = dict(SCENARIO_BASE)
    data["expect"] = {"outcome": "booking_done", "judge": "- 루브릭"}
    with pytest.raises(ScenarioError) as exc:
        load(fake_repo, data)
    assert "unknown outcome" in exc.value.message


def test_missing_judge_is_error(fake_repo):
    data = dict(SCENARIO_BASE)
    data["expect"] = {"outcome": "info_answered"}
    with pytest.raises(ScenarioError) as exc:
        load(fake_repo, data)
    assert "judge" in exc.value.message


def test_unknown_fixture_is_error(fake_repo):
    data = dict(SCENARIO_BASE)
    data["dial_info"] = "keypad_menu"
    with pytest.raises(ScenarioError) as exc:
        load(fake_repo, data)
    assert "unknown dial_info fixture" in exc.value.message


def test_id_filename_mismatch_is_error(fake_repo):
    data = dict(SCENARIO_BASE)
    with pytest.raises(ScenarioError) as exc:
        load(fake_repo, data, name="other_name.yaml")
    assert "file name stem" in exc.value.message


def test_underscore_prefixed_id_is_error(fake_repo):
    data = dict(SCENARIO_BASE)
    data["id"] = "_smoke_case"
    with pytest.raises(ScenarioError) as exc:
        load(fake_repo, data, name="_smoke_case.yaml")
    assert "id must match" in exc.value.message


def test_bad_regex_is_error(fake_repo):
    data = dict(SCENARIO_BASE)
    data["expect"] = {
        "outcome": "info_answered",
        "judge": "- 루브릭",
        "agent_regex_none": ["("],
    }
    with pytest.raises(ScenarioError) as exc:
        load(fake_repo, data)
    assert "invalid regex" in exc.value.message


def test_bad_intent_is_error(fake_repo):
    data = dict(SCENARIO_BASE)
    data["intent"] = "booking"
    with pytest.raises(ScenarioError) as exc:
        load(fake_repo, data)
    assert "unknown intent" in exc.value.message


def test_bad_difficulty_is_error(fake_repo):
    data = dict(SCENARIO_BASE)
    data["difficulty"] = "trivial"
    with pytest.raises(ScenarioError) as exc:
        load(fake_repo, data)
    assert "unknown difficulty" in exc.value.message


def test_missing_required_key_is_error(fake_repo):
    data = dict(SCENARIO_BASE)
    data.pop("title")
    with pytest.raises(ScenarioError) as exc:
        load(fake_repo, data)
    assert "missing required keys" in exc.value.message


def test_duplicate_ids_are_rejected(fake_repo):
    from sim_suite.schema import load_scenarios

    write_scenario(fake_repo, dict(SCENARIO_BASE))
    other = dict(SCENARIO_BASE)
    sub = fake_repo / "simulations" / "scenarios" / "nested"
    sub.mkdir()
    (sub / "demo_case.yaml").write_text(
        yaml.safe_dump(other, allow_unicode=True), encoding="utf-8"
    )
    suite = load_suite(fake_repo / "simulations" / "suite.yaml")
    with pytest.raises(ScenarioError) as exc:
        load_scenarios(suite)
    assert "duplicate id" in exc.value.message


@requires_regenerated_simulations
def test_real_suite_and_scenarios_load():
    """저장소에 실제로 들어 있는 suite.yaml + 시나리오가 모두 유효해야 한다."""
    from sim_suite.schema import load_scenarios

    repo_root = Path(__file__).resolve().parents[2]
    suite = load_suite(repo_root / "simulations" / "suite.yaml")
    scenarios = load_scenarios(suite)
    assert isinstance(scenarios, list)


# -------------------------------------------------------------------- judge


def test_parse_judge_output_plain_json():
    result = parse_judge_output(
        '{"verdict": "PASS", "reasons": ["근거"], "issues": []}'
    )
    assert result.verdict == "PASS"
    assert result.reasons == ["근거"]
    assert result.issues == []


def test_parse_judge_output_fenced_json():
    raw = '```json\n{"verdict": "FAIL", "reasons": ["a"], "issues": ["b"]}\n```'
    result = parse_judge_output(raw)
    assert result.verdict == "FAIL"
    assert result.issues == ["b"]


def test_parse_judge_output_with_surrounding_prose():
    raw = '판정합니다.\n{"verdict": "PASS", "reasons": [], "issues": []}\n끝.'
    assert parse_judge_output(raw).verdict == "PASS"


def test_parse_judge_output_garbage_is_error():
    result = parse_judge_output("도저히 JSON 이 아님", model="openai:gpt-5.4-mini")
    assert result.verdict == "ERROR"
    assert result.raw == "도저히 JSON 이 아님"
    assert result.model == "openai:gpt-5.4-mini"


def test_parse_judge_output_unknown_verdict_is_error():
    result = parse_judge_output('{"verdict": "MAYBE"}')
    assert result.verdict == "ERROR"


def test_parse_judge_output_string_reasons_are_wrapped():
    result = parse_judge_output('{"verdict": "PASS", "reasons": "한 줄"}')
    assert result.reasons == ["한 줄"]


# ------------------------------------------------------------------ verdicts


def make_scenario(
    fake_repo: Path,
    *,
    intent: str,
    outcomes: list[str],
    allow_blocked: bool = False,
):
    data = dict(SCENARIO_BASE)
    data["id"] = "verdict_case"
    data["intent"] = intent
    data["allow_blocked"] = allow_blocked
    data["expect"] = {"outcome_any": outcomes, "judge": "- 루브릭"}
    return load(fake_repo, data)


def test_blocked_when_modify_hits_no_reservation(fake_repo, tmp_path):
    from sim_suite.runner import compute_verdict, is_blocked

    scenario = make_scenario(
        fake_repo,
        intent="booking_modify",
        outcomes=["booking_modified"],
        allow_blocked=True,
    )
    parsed = parsed_from(
        [
            customer("예약 변경할게요"),
            tool("modify_appointment"),
            agent("변경할 예약내역이 없습니다."),
            END,
        ],
        tmp_path,
    )
    outcome = derive_outcome(parsed)
    assert outcome == "no_reservation"
    assert is_blocked(scenario, outcome) is True

    checks = run_checks(scenario.expect, parsed, outcome)
    verdict = compute_verdict(
        checks,
        None,
        timed_out=False,
        parsed=parsed,
        scenario=scenario,
        outcome=outcome,
    )
    assert verdict == "BLOCKED"


def test_no_reservation_is_failure_without_explicit_allow_blocked(fake_repo):
    from sim_suite.runner import is_blocked

    scenario = make_scenario(
        fake_repo, intent="booking_modify", outcomes=["booking_modified"]
    )
    assert is_blocked(scenario, "no_reservation") is False


def test_scenario_expecting_no_reservation_is_judged_normally(fake_repo, tmp_path):
    from sim_suite.runner import compute_verdict, is_blocked

    scenario = make_scenario(
        fake_repo, intent="booking_cancel", outcomes=["no_reservation"]
    )
    parsed = parsed_from(
        [
            customer("예약 취소해 주세요"),
            tool("cancel_appointment"),
            agent("취소할 예약내역이 없습니다."),
            END,
        ],
        tmp_path,
    )
    outcome = derive_outcome(parsed)
    assert is_blocked(scenario, outcome) is False

    checks = run_checks(scenario.expect, parsed, outcome)
    assert (
        compute_verdict(
            checks,
            None,
            timed_out=False,
            parsed=parsed,
            scenario=scenario,
            outcome=outcome,
        )
        == "PASS"
    )


def test_no_reservation_on_a_non_booking_intent_is_not_blocked(fake_repo, tmp_path):
    from sim_suite.runner import compute_verdict, is_blocked

    scenario = make_scenario(
        fake_repo, intent="info_inquiry", outcomes=["info_answered"]
    )
    parsed = parsed_from(
        [customer("예약 있나요?"), agent("변경할 예약내역이 없습니다."), END],
        tmp_path,
    )
    outcome = derive_outcome(parsed)
    assert outcome == "no_reservation"
    assert is_blocked(scenario, outcome) is False

    checks = run_checks(scenario.expect, parsed, outcome)
    assert (
        compute_verdict(
            checks,
            None,
            timed_out=False,
            parsed=parsed,
            scenario=scenario,
            outcome=outcome,
        )
        == "FAIL"
    )


def test_timeout_outranks_blocked(fake_repo, tmp_path):
    from sim_suite.runner import compute_verdict

    scenario = make_scenario(
        fake_repo, intent="booking_modify", outcomes=["booking_modified"]
    )
    parsed = parsed_from(
        [customer("변경이요"), agent("변경할 예약내역이 없습니다.")], tmp_path
    )
    outcome = derive_outcome(parsed)
    checks = run_checks(scenario.expect, parsed, outcome, timed_out=True)
    assert (
        compute_verdict(
            checks,
            None,
            timed_out=True,
            parsed=parsed,
            scenario=scenario,
            outcome=outcome,
        )
        == "ERROR"
    )


def test_timeout_after_expected_business_outcome_is_harness_warning(
    fake_repo, tmp_path
):
    from sim_suite.runner import (
        HARNESS_STALL_AFTER_OUTCOME,
        compute_layer_statuses,
        compute_verdict,
    )

    scenario = make_scenario(
        fake_repo, intent="booking_create", outcomes=["booking_created"]
    )
    parsed = parsed_from(
        [
            customer("예약할게요"),
            tool("commit_booking"),
            agent("예약 신청이 접수되었습니다."),
            agent("응답지연으로 인해 통화를 종료합니다."),
        ],
        tmp_path,
    )
    outcome = derive_outcome(parsed)
    checks = run_checks(scenario.expect, parsed, outcome, timed_out=True)
    business, quality, harness = compute_layer_statuses(
        checks,
        None,
        timed_out=True,
        parsed=parsed,
        scenario=scenario,
        outcome=outcome,
    )
    assert (business, quality, harness) == (
        "PASS",
        "NOT_RUN",
        HARNESS_STALL_AFTER_OUTCOME,
    )
    assert (
        compute_verdict(
            checks,
            None,
            timed_out=True,
            parsed=parsed,
            scenario=scenario,
            outcome=outcome,
        )
        == "PASS"
    )


def test_judge_prompt_uses_sanitized_quality_transcript(fake_repo, tmp_path):
    from sim_suite.judge import JUDGE_SYSTEM_PROMPT, build_judge_prompt

    scenario = make_scenario(
        fake_repo, intent="info_inquiry", outcomes=["ended_by_agent"]
    )
    parsed = parsed_from(
        [
            customer("없어요"),
            agent("통화를 종료하겠습니다."),
            agent("통화를 종료하겠습니다. ✓"),
            agent("응답지연으로 인해 통화를 종료합니다."),
            tool("end_call_by_system"),
            END,
        ],
        tmp_path,
    )
    prompt = build_judge_prompt(scenario, parsed)
    assert prompt.count("에이전트: 통화를 종료하겠습니다.") == 1
    assert "응답지연" not in prompt
    assert "루브릭에 없는 새로운 금지 조건" in JUDGE_SYSTEM_PROMPT


# --------------------------------------------------------------- idle detect


def test_session_looks_finished_on_session_end(tmp_path):
    from sim_suite.events import session_looks_finished

    parsed = parsed_from([agent("안녕히 계세요"), END], tmp_path)
    assert session_looks_finished(parsed) is True


def test_session_looks_finished_on_response_delay_close(tmp_path):
    from sim_suite.events import session_looks_finished

    parsed = parsed_from(
        [customer("..."), agent("응답지연으로 인해 통화를 종료합니다.")], tmp_path
    )
    assert session_looks_finished(parsed) is True


def test_session_looks_finished_on_agent_closing_line(tmp_path):
    from sim_suite.events import session_looks_finished

    parsed = parsed_from(
        [customer("끊을게요"), agent("통화를 종료하겠습니다. 좋은 하루 보내세요.")],
        tmp_path,
    )
    assert session_looks_finished(parsed) is True


def test_session_not_finished_mid_dialogue(tmp_path):
    from sim_suite.events import session_looks_finished

    parsed = parsed_from(
        [agent("어느 날짜가 좋으실까요?"), customer("다음 주요")], tmp_path
    )
    assert session_looks_finished(parsed) is False


def test_session_not_finished_when_tool_follows_close(tmp_path):
    """종료 문구 뒤에 다른 이벤트가 붙었으면 마지막 이벤트가 아니다."""
    from sim_suite.events import session_looks_finished

    parsed = parsed_from(
        [agent("통화를 종료하겠습니다."), tool("search_schedules")], tmp_path
    )
    assert session_looks_finished(parsed) is False


def test_should_stop_early_requires_grace_period(tmp_path):
    from sim_suite.events import should_stop_early

    parsed = parsed_from(
        [customer("..."), agent("응답지연으로 인해 통화를 종료합니다.")], tmp_path
    )
    assert should_stop_early(parsed, idle_sec=5.0) is False
    assert should_stop_early(parsed, idle_sec=19.9) is False
    assert should_stop_early(parsed, idle_sec=20.0) is True
    assert should_stop_early(parsed, idle_sec=120.0) is True


def test_should_stop_early_custom_grace(tmp_path):
    from sim_suite.events import should_stop_early

    parsed = parsed_from([agent("네"), END], tmp_path)
    assert should_stop_early(parsed, idle_sec=3.0, grace_sec=2.0) is True


def test_should_stop_early_false_while_dialogue_open(tmp_path):
    from sim_suite.events import should_stop_early

    parsed = parsed_from([agent("어느 날짜가 좋으실까요?")], tmp_path)
    assert should_stop_early(parsed, idle_sec=600.0) is False


def test_should_stop_early_false_without_events():
    from sim_suite.events import parse_events, should_stop_early

    parsed = parse_events(None)
    assert should_stop_early(parsed, idle_sec=600.0) is False


# ---------------------------------------------------------------- retries


def make_result(**kwargs):
    from sim_suite.runner import RunResult

    base = {
        "scenario_id": "case",
        "title": "제목",
        "intent": "booking_create",
        "difficulty": "easy",
        "phase": 1,
        "tags": [],
        "verdict": "PASS",
        "outcome": "booking_created",
        "elapsed_sec": 12.0,
        "timed_out": False,
        "exit_code": 0,
    }
    base.update(kwargs)
    return RunResult(**base)


def test_should_retry_on_error_verdict():
    from sim_suite.runner import should_retry

    assert should_retry(make_result(verdict="ERROR")) is True


def test_should_retry_on_no_outcome():
    from sim_suite.runner import should_retry

    assert should_retry(make_result(verdict="FAIL", outcome="no_outcome")) is True


def test_should_retry_on_away_timeout_closing():
    from sim_suite.runner import should_retry

    result = make_result(
        verdict="FAIL",
        outcome="ended_by_agent",
        stopped_early=True,
        last_agent_msgs=["네 확인했습니다", "응답지연으로 인해 통화를 종료합니다."],
    )
    assert should_retry(result) is True


def test_should_not_retry_a_clean_pass():
    from sim_suite.runner import should_retry

    assert should_retry(make_result()) is False


def test_should_not_retry_a_real_failure():
    from sim_suite.runner import should_retry

    result = make_result(
        verdict="FAIL",
        outcome="memo_left",
        last_agent_msgs=["메모를 남겨드렸습니다", "감사합니다"],
    )
    assert should_retry(result) is False


def test_should_not_retry_when_closing_is_not_the_last_line():
    """마지막 발화가 아니면 스톨의 최종 증상이 아니다."""
    from sim_suite.runner import should_retry

    result = make_result(
        verdict="JUDGE_FAIL",
        outcome="booking_created",
        last_agent_msgs=[
            "응답지연으로 인해 통화를 종료합니다.",
            "예약이 완료되었습니다",
        ],
    )
    assert should_retry(result) is False


def test_should_not_retry_blocked():
    from sim_suite.runner import should_retry

    assert (
        should_retry(make_result(verdict="BLOCKED", outcome="no_reservation")) is False
    )


def test_is_flaky_transitions():
    from sim_suite.runner import is_flaky

    assert is_flaky("ERROR", "PASS") is True
    assert is_flaky("FAIL", "PASS") is True
    assert is_flaky("ERROR", "JUDGE_FAIL") is True
    assert is_flaky("ERROR", "BLOCKED") is True
    assert is_flaky("ERROR", "ERROR") is False
    assert is_flaky("ERROR", "FAIL") is False
    assert is_flaky("PASS", "PASS") is False
    assert is_flaky("JUDGE_FAIL", "PASS") is False


# ----------------------------------------------------------------- orphans

# macOS `ps -E -o pid= -o command= -p <pid>` 한 줄을 흉내 낸 것.
PS_LINE = (
    "84213 /opt/app/.venv/bin/python src/agent-cli-text.py console --text "
    "PATH=/usr/bin SHELL=/bin/zsh DIAL_INFO_JSON=/tmp/a.yaml "
    "SIM_SUITE_RUN_TAG={tag} USER_AWAY_TIMEOUT=90"
)


def ps_line(tag: str) -> str:
    return PS_LINE.format(tag=tag)


def test_make_run_tag_and_prefix():
    from sim_suite.orphans import make_run_tag, suite_tag_prefix

    assert make_run_tag("20260915-143656", "info_easy_01_hours", 2) == (
        "20260915-143656/info_easy_01_hours/2"
    )
    assert suite_tag_prefix("20260915-143656") == "20260915-143656/"


def test_matches_tag_exact():
    from sim_suite.orphans import matches_tag

    tag = "20260915-143656/info_easy_01_hours/1"
    assert matches_tag(ps_line(tag), tag) is True


def test_matches_tag_at_end_of_line():
    from sim_suite.orphans import matches_tag

    tag = "run/case/1"
    entry = f"90 python src/agent-cli-text.py console --text SIM_SUITE_RUN_TAG={tag}"
    assert matches_tag(entry, tag) is True


def test_matches_tag_rejects_longer_attempt_number():
    """`.../1` 이 `.../10` 에 걸리면 안 된다."""
    from sim_suite.orphans import matches_tag

    assert matches_tag(ps_line("run/case/10"), "run/case/1") is False


def test_matches_tag_rejects_other_scenario():
    from sim_suite.orphans import matches_tag

    assert matches_tag(ps_line("run/other_case/1"), "run/case/1") is False


def test_matches_tag_rejects_untagged_process():
    """태그 없는 프로세스는 절대 대상이 아니다(사람이 띄운 시뮬레이터 등)."""
    from sim_suite.orphans import matches_tag

    entry = "777 /usr/bin/python src/agent-cli-text.py console --text PATH=/usr/bin"
    assert matches_tag(entry, "run/case/1") is False
    assert matches_tag(entry, "run/", prefix=True) is False


def test_matches_tag_rejects_non_agent_process():
    from sim_suite.orphans import matches_tag

    entry = "778 python tools/customer-sim.py SIM_SUITE_RUN_TAG=run/case/1"
    assert matches_tag(entry, "run/case/1") is False


def test_matches_tag_prefix_matches_whole_run():
    from sim_suite.orphans import matches_tag

    assert matches_tag(ps_line("run/case/1"), "run/", prefix=True) is True
    assert matches_tag(ps_line("run/case/10"), "run/", prefix=True) is True
    assert matches_tag(ps_line("otherrun/case/1"), "run/", prefix=True) is False


def test_matches_tag_handles_empty_entry():
    from sim_suite.orphans import matches_tag

    assert matches_tag("", "run/case/1") is False


def test_find_orphans_filters_by_tag():
    from sim_suite.orphans import find_orphans

    entries = {
        11: ps_line("run/case/1"),
        12: ps_line("run/other/1"),
        13: "13 python src/agent-cli-text.py console --text PATH=/usr/bin",
        14: None,
    }
    found = find_orphans(
        "run/case/1", pids=entries.keys(), reader=lambda pid: entries[pid]
    )
    assert found == [11]


def test_find_orphans_prefix_sweep():
    from sim_suite.orphans import find_orphans

    entries = {
        11: ps_line("run/case/1"),
        12: ps_line("run/other/2"),
        13: ps_line("otherrun/case/1"),
    }
    found = find_orphans(
        "run/", prefix=True, pids=entries.keys(), reader=lambda pid: entries[pid]
    )
    assert found == [11, 12]


def test_reap_orphans_returns_early_without_sleeping():
    from sim_suite.orphans import reap_orphans

    slept: list[float] = []
    killed = reap_orphans(
        "run/case/1",
        sleep=slept.append,
        finder=lambda tag, prefix=False: [],
        killer=lambda pids: list(pids),
    )
    assert killed == []
    assert slept == []


def test_reap_orphans_waits_then_kills_survivors():
    from sim_suite.orphans import reap_orphans

    slept: list[float] = []
    calls = {"n": 0}

    def finder(tag, prefix=False):
        calls["n"] += 1
        return [42]

    killed = reap_orphans(
        "run/case/1",
        grace_sec=10.0,
        sleep=slept.append,
        finder=finder,
        killer=lambda pids: list(pids),
    )
    assert slept == [10.0]
    assert calls["n"] == 2
    assert killed == [42]


def test_reap_orphans_spares_a_process_that_exits_during_grace():
    from sim_suite.orphans import reap_orphans

    seen = {"n": 0}

    def finder(tag, prefix=False):
        seen["n"] += 1
        return [42] if seen["n"] == 1 else []

    killed = reap_orphans(
        "run/case/1",
        grace_sec=10.0,
        sleep=lambda _: None,
        finder=finder,
        killer=lambda pids: list(pids),
    )
    assert killed == []


def test_booking_api_fixture_server_serves_and_records(tmp_path):
    from sim_suite.api_fixture import BookingApiFixtureServer

    fixture = tmp_path / "fixture.yaml"
    fixture.write_text(
        "routes:\n  /customer:\n    json:\n      customers: [fixture]\n",
        encoding="utf-8",
    )
    server = BookingApiFixtureServer(fixture, tmp_path / "requests.jsonl")
    server.start()
    try:
        with urllib.request.urlopen(f"{server.base_url}/customer") as response:
            assert json.loads(response.read()) == {"customers": ["fixture"]}
    finally:
        server.stop()

    assert server.requests == ["GET /customer"]
    assert '"GET /customer"' in (tmp_path / "requests.jsonl").read_text()


def test_booking_api_fixture_server_serves_sequence_and_repeats_last(tmp_path):
    from sim_suite.api_fixture import BookingApiFixtureServer

    fixture = tmp_path / "fixture.yaml"
    fixture.write_text(
        "routes:\n"
        "  /appointments:\n"
        "    sequence:\n"
        "      - json: [first, second]\n"
        "      - json: [second]\n"
        "      - json: []\n",
        encoding="utf-8",
    )
    server = BookingApiFixtureServer(fixture, tmp_path / "requests.jsonl")
    server.start()
    try:
        responses = []
        for _ in range(4):
            with urllib.request.urlopen(f"{server.base_url}/appointments") as response:
                responses.append(json.loads(response.read()))
    finally:
        server.stop()

    assert responses == [["first", "second"], ["second"], [], []]
    assert server.requests == ["GET /appointments"] * 4


def test_summary_metadata_validation_and_nonpass_change(tmp_path):
    from sim_suite.report import (
        compare_summaries,
        load_summary,
        validate_comparison_metadata,
        write_summary_json,
    )

    metadata = {
        "agent_model": "openai:gpt-5.6-luna",
        "judge_model": "openai:gpt-5.4-mini",
        "scenario_ids": ["case"],
    }
    path = write_summary_json(
        [make_result(verdict="BLOCKED")],
        tmp_path / "summary.json",
        metadata=metadata,
    )
    previous = load_summary(path)
    validate_comparison_metadata(previous, metadata)
    diff = compare_summaries(previous, [make_result(verdict="FAIL")])
    assert diff.changed_nonpass == ["case (BLOCKED → FAIL)"]

    with pytest.raises(ValueError, match="agent_model"):
        validate_comparison_metadata(
            previous,
            {**metadata, "agent_model": "gemma:llm"},
        )


# Identity matching is covered by the verify-identity tests and scenario S18.


def test_booking_api_fixture_resolves_tomorrow_in_kst_and_records_query(
    tmp_path, monkeypatch
):
    from datetime import datetime, timezone

    from sim_suite import api_fixture

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            # The UTC date is still October 13, but KST is already October 14.
            return datetime(2026, 10, 13, 16, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(api_fixture, "datetime", FixedDatetime)
    fixture = tmp_path / "fixture.yaml"
    fixture.write_text(
        'routes:\n  /schedules:\n    json:\n      times: ["${tomorrow} 09:00:00"]\n',
        encoding="utf-8",
    )
    request_log = tmp_path / "requests.jsonl"
    server = api_fixture.BookingApiFixtureServer(fixture, request_log)
    server.start()
    try:
        with urllib.request.urlopen(
            f"{server.base_url}/schedules?startDate=2026-10-15&type=DATE_TIME"
        ) as response:
            assert json.loads(response.read()) == {"times": ["2026-10-15 09:00:00"]}
    finally:
        server.stop()

    assert server.requests == ["GET /schedules"]
    assert json.loads(request_log.read_text()) == {
        "request": "GET /schedules",
        "query": {"startDate": ["2026-10-15"], "type": ["DATE_TIME"]},
    }


@pytest.mark.parametrize(
    "announcement",
    [
        "예약 신청이 완료되었습니다.",
        "예약신청이완료 되었습니다.",
        "예약 신청이 접수됐습니다.",
    ],
)
def test_booking_success_accepts_completed_application_wording(tmp_path, announcement):
    completed = parsed_from(
        [customer("네, 진행해주세요"), tool("commit_booking"), agent(announcement), END],
        tmp_path,
    )
    assert derive_outcome(completed) == "booking_created"
    no_commit = parsed_from([customer("예약이요"), agent(announcement), END], tmp_path)
    assert derive_outcome(no_commit) != "booking_created"


@pytest.mark.parametrize(
    "announcement",
    [
        "예약 신청이 완료되면 안내드리겠습니다.",
        "예약 신청이 접수되지 않았습니다.",
        "예약 신청이 완료되지 않았습니다.",
        "예약 신청이 접수 가능한지 확인하겠습니다.",
        "예약 신청이 접수될 예정입니다.",
    ],
)
def test_booking_success_rejects_uncompleted_wording(tmp_path, announcement):
    parsed = parsed_from(
        [customer("네"), tool("commit_booking"), agent(announcement), END], tmp_path
    )
    assert derive_outcome(parsed) != "booking_created"


def test_quality_transcript_strips_terminal_redraw_tail_before_deduplicating(tmp_path):
    speech = "내일 오전 9시 휴대폰 점검 예약 신청이 완료되었습니다."
    parsed = parsed_from(
        [
            customer("네, 진행해주세요"),
            agent(speech),
            agent(
                speech
                + " "
                + "─" * 100
                + " \u276f █ Type to talk to your agent "
                + "─" * 100
            ),
            END,
        ],
        tmp_path,
    )
    quality = parsed.quality_transcript_text()
    assert quality.count(speech) == 1
    assert "Type to talk" not in quality
    assert "─" not in quality
    assert parsed.agent_msgs == [speech, speech]
