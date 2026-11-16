"""`simulations/suite.yaml` + 시나리오 YAML 로더와 검증기.

검증 실패는 모두 `ScenarioError(path, message)`로 올리고, CLI는 이를
`path: message` 한 줄로 출력한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .events import OUTCOMES

INTENTS = (
    "booking_create",
    "booking_modify",
    "booking_cancel",
    "info_inquiry",
    "handoff",
    "mixed",
)
DIFFICULTIES = ("easy", "medium", "hard", "extreme")
RUNTIME_PROBES = ("duplicate_verification_code_first_match",)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")

REQUIRED_SCENARIO_KEYS = (
    "id",
    "title",
    "intent",
    "difficulty",
    "dial_info",
    "phase",
    "persona",
    "expect",
)
REQUIRED_PERSONA_KEYS = ("name", "goal", "scenario")
ALLOWED_EXPECT_KEYS = (
    "outcome",
    "outcome_any",
    "tools_any",
    "tools_none",
    "handoffs_any",
    "handoffs_none",
    "customer_turns",
    "agent_regex_any",
    "agent_regex_max",
    "agent_regex_none",
    "api_requests_any",
    "api_requests_none",
    "judge",
)


class ScenarioError(Exception):
    """시나리오/스위트 파일 검증 오류. CLI는 `path: message`로 찍는다."""

    def __init__(self, path: Path | str, message: str):
        self.path = Path(path)
        self.message = message
        super().__init__(f"{path}: {message}")


@dataclass(frozen=True)
class RegexMax:
    pattern: str
    max: int


@dataclass
class Expect:
    outcomes: list[str] = field(default_factory=list)
    tools_any: list[str] = field(default_factory=list)
    tools_none: list[str] = field(default_factory=list)
    handoffs_any: list[str] = field(default_factory=list)
    handoffs_none: list[str] = field(default_factory=list)
    customer_turns_min: int | None = None
    customer_turns_max: int | None = None
    agent_regex_any: list[str] = field(default_factory=list)
    agent_regex_max: list[RegexMax] = field(default_factory=list)
    agent_regex_none: list[str] = field(default_factory=list)
    api_requests_any: list[str] = field(default_factory=list)
    api_requests_none: list[str] = field(default_factory=list)
    judge: str = ""


@dataclass
class RunConfig:
    timeout_sec: int
    stop_on_transfer: bool


@dataclass
class Scenario:
    id: str
    title: str
    intent: str
    difficulty: str
    tags: list[str]
    dial_info: str
    phase: int
    api_fixture: Path | None
    runtime_probe: str | None
    allow_blocked: bool
    persona: dict[str, Any]
    run: RunConfig
    expect: Expect
    path: Path


@dataclass
class SuiteDefaults:
    agent_model: str = "openai:gpt-5.6-luna"
    judge_model: str = "openai:gpt-5.4-mini"
    customer_model: str | None = None
    concurrency: int = 3
    timeout_sec: int = 360
    # 기본값 False: True 면 customer-sim 이 LeaveMessageTask 핸드오프 시점에
    # 세션을 죽여 메모 대화 자체를 관측할 수 없다. suite.yaml 이 최종 결정권을
    # 가지며 시나리오의 run.stop_on_transfer 가 그 위에 덮어쓴다.
    stop_on_transfer: bool = False
    # 모든 시나리오의 expect.agent_regex_none 에 합쳐지는 전역 금지 패턴.
    agent_regex_none: list[str] = field(default_factory=list)


@dataclass
class Suite:
    path: Path
    root: Path
    defaults: SuiteDefaults
    fixtures: dict[str, Path]
    scenario_dirs: list[Path]
    phases: dict[int, str]


# ------------------------------------------------------------------ helpers


def _require_mapping(path: Path, value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScenarioError(path, f"{what} must be a mapping")
    return value


def _str_list(path: Path, value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ScenarioError(path, f"{key} must be a list of strings")
    return list(value)


def _compile_or_raise(path: Path, pattern: str, key: str) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ScenarioError(path, f"{key}: invalid regex {pattern!r} ({exc})")


# ------------------------------------------------------------------- suite


def load_suite(suite_path: Path | str) -> Suite:
    """`suite.yaml`을 읽어 기본값/픽스처/시나리오 디렉터리를 해석한다."""
    suite_path = Path(suite_path).resolve()
    if not suite_path.exists():
        raise ScenarioError(suite_path, "suite file not found")
    data = yaml.safe_load(suite_path.read_text(encoding="utf-8")) or {}
    data = _require_mapping(suite_path, data, "suite")
    root = suite_path.parent.parent

    raw_defaults = _require_mapping(suite_path, data.get("defaults") or {}, "defaults")
    known = set(SuiteDefaults.__dataclass_fields__)
    unknown = set(raw_defaults) - known
    if unknown:
        raise ScenarioError(
            suite_path, f"unknown defaults keys: {', '.join(sorted(unknown))}"
        )
    defaults = SuiteDefaults(**raw_defaults)
    defaults.agent_regex_none = _str_list(
        suite_path, defaults.agent_regex_none, "defaults.agent_regex_none"
    )
    for pattern in defaults.agent_regex_none:
        _compile_or_raise(suite_path, pattern, "defaults.agent_regex_none")

    raw_fixtures = _require_mapping(suite_path, data.get("fixtures") or {}, "fixtures")
    if not raw_fixtures:
        raise ScenarioError(suite_path, "fixtures must define at least one dial_info")
    fixtures: dict[str, Path] = {}
    for key, rel in raw_fixtures.items():
        fixture_path = (root / str(rel)).resolve()
        if not fixture_path.exists():
            raise ScenarioError(suite_path, f"fixture {key!r} not found: {rel}")
        fixtures[str(key)] = fixture_path

    scenario_dirs = [
        (root / str(rel)).resolve()
        for rel in (data.get("scenario_dirs") or ["simulations/scenarios"])
    ]

    raw_phases = _require_mapping(suite_path, data.get("phases") or {}, "phases")
    phases: dict[int, str] = {}
    for key, label in raw_phases.items():
        try:
            phases[int(key)] = str(label)
        except (TypeError, ValueError):
            raise ScenarioError(suite_path, f"phase key must be an int: {key!r}")

    return Suite(
        path=suite_path,
        root=root,
        defaults=defaults,
        fixtures=fixtures,
        scenario_dirs=scenario_dirs,
        phases=phases,
    )


# ---------------------------------------------------------------- scenario


def parse_expect(path: Path, raw: Any) -> Expect:
    raw = _require_mapping(path, raw or {}, "expect")
    unknown = set(raw) - set(ALLOWED_EXPECT_KEYS)
    if unknown:
        raise ScenarioError(path, f"unknown expect keys: {', '.join(sorted(unknown))}")

    has_outcome = "outcome" in raw and raw["outcome"] is not None
    has_outcome_any = "outcome_any" in raw and raw["outcome_any"] is not None
    if has_outcome and has_outcome_any:
        raise ScenarioError(
            path, "expect must define exactly one of outcome / outcome_any"
        )
    if not has_outcome and not has_outcome_any:
        raise ScenarioError(
            path, "expect must define exactly one of outcome / outcome_any"
        )

    if has_outcome:
        if not isinstance(raw["outcome"], str):
            raise ScenarioError(path, "expect.outcome must be a string")
        outcomes = [raw["outcome"]]
    else:
        outcomes = _str_list(path, raw["outcome_any"], "expect.outcome_any")
        if not outcomes:
            raise ScenarioError(path, "expect.outcome_any must not be empty")
    for outcome in outcomes:
        if outcome not in OUTCOMES:
            raise ScenarioError(
                path,
                f"unknown outcome {outcome!r} (allowed: {', '.join(OUTCOMES)})",
            )

    judge = raw.get("judge")
    if not isinstance(judge, str) or not judge.strip():
        raise ScenarioError(path, "expect.judge (rubric text) is required")

    customer_turns = raw.get("customer_turns") or {}
    customer_turns = _require_mapping(path, customer_turns, "expect.customer_turns")
    unknown_turns = set(customer_turns) - {"min", "max"}
    if unknown_turns:
        raise ScenarioError(
            path,
            f"expect.customer_turns allows only min/max, got "
            f"{', '.join(sorted(unknown_turns))}",
        )
    turns_min = customer_turns.get("min")
    turns_max = customer_turns.get("max")
    for label, value in (("min", turns_min), ("max", turns_max)):
        if value is not None and not isinstance(value, int):
            raise ScenarioError(path, f"expect.customer_turns.{label} must be an int")

    regex_max: list[RegexMax] = []
    for item in raw.get("agent_regex_max") or []:
        item = _require_mapping(path, item, "expect.agent_regex_max item")
        pattern = item.get("pattern")
        max_count = item.get("max")
        if not isinstance(pattern, str) or not isinstance(max_count, int):
            raise ScenarioError(
                path, "expect.agent_regex_max items need {pattern: str, max: int}"
            )
        _compile_or_raise(path, pattern, "expect.agent_regex_max")
        regex_max.append(RegexMax(pattern=pattern, max=max_count))

    regex_any = _str_list(path, raw.get("agent_regex_any"), "expect.agent_regex_any")
    regex_none = _str_list(path, raw.get("agent_regex_none"), "expect.agent_regex_none")
    for pattern in regex_any:
        _compile_or_raise(path, pattern, "expect.agent_regex_any")
    for pattern in regex_none:
        _compile_or_raise(path, pattern, "expect.agent_regex_none")
    api_requests_any = _str_list(
        path, raw.get("api_requests_any"), "expect.api_requests_any"
    )
    api_requests_none = _str_list(
        path, raw.get("api_requests_none"), "expect.api_requests_none"
    )
    for pattern in [*api_requests_any, *api_requests_none]:
        _compile_or_raise(path, pattern, "expect.api_requests")

    return Expect(
        outcomes=outcomes,
        tools_any=_str_list(path, raw.get("tools_any"), "expect.tools_any"),
        tools_none=_str_list(path, raw.get("tools_none"), "expect.tools_none"),
        handoffs_any=_str_list(path, raw.get("handoffs_any"), "expect.handoffs_any"),
        handoffs_none=_str_list(path, raw.get("handoffs_none"), "expect.handoffs_none"),
        customer_turns_min=turns_min,
        customer_turns_max=turns_max,
        agent_regex_any=regex_any,
        agent_regex_max=regex_max,
        agent_regex_none=regex_none,
        api_requests_any=api_requests_any,
        api_requests_none=api_requests_none,
        judge=judge.strip(),
    )


def load_scenario(path: Path | str, suite: Suite) -> Scenario:
    """시나리오 YAML 한 개를 읽고 검증한다."""
    path = Path(path).resolve()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ScenarioError(path, f"invalid YAML ({exc})")
    data = _require_mapping(path, data or {}, "scenario")

    missing = [key for key in REQUIRED_SCENARIO_KEYS if data.get(key) is None]
    if missing:
        raise ScenarioError(path, f"missing required keys: {', '.join(missing)}")

    scenario_id = data["id"]
    if not isinstance(scenario_id, str) or not ID_RE.match(scenario_id):
        raise ScenarioError(path, "id must match [a-z0-9_]+ and not start with '_'")
    if scenario_id != path.stem:
        raise ScenarioError(
            path, f"id {scenario_id!r} must equal the file name stem {path.stem!r}"
        )

    intent = data["intent"]
    if intent not in INTENTS:
        raise ScenarioError(
            path, f"unknown intent {intent!r} (allowed: {', '.join(INTENTS)})"
        )
    difficulty = data["difficulty"]
    if difficulty not in DIFFICULTIES:
        raise ScenarioError(
            path,
            f"unknown difficulty {difficulty!r} (allowed: {', '.join(DIFFICULTIES)})",
        )

    dial_info = data["dial_info"]
    if dial_info not in suite.fixtures:
        raise ScenarioError(
            path,
            f"unknown dial_info fixture {dial_info!r} "
            f"(suite fixtures: {', '.join(sorted(suite.fixtures))})",
        )

    phase = data["phase"]
    if not isinstance(phase, int):
        raise ScenarioError(path, "phase must be an int")

    persona = _require_mapping(path, data["persona"], "persona")
    missing_persona = [
        key for key in REQUIRED_PERSONA_KEYS if not str(persona.get(key) or "").strip()
    ]
    if missing_persona:
        raise ScenarioError(path, f"persona missing keys: {', '.join(missing_persona)}")
    scripted_replies = persona.get("scripted_replies")
    if scripted_replies is not None and (
        not isinstance(scripted_replies, list)
        or not scripted_replies
        or not all(isinstance(reply, str) and reply.strip() for reply in scripted_replies)
    ):
        raise ScenarioError(
            path, "persona.scripted_replies must be a non-empty list of strings"
        )

    raw_run = _require_mapping(path, data.get("run") or {}, "run")
    unknown_run = set(raw_run) - {"timeout_sec", "stop_on_transfer"}
    if unknown_run:
        raise ScenarioError(path, f"unknown run keys: {', '.join(sorted(unknown_run))}")
    run = RunConfig(
        timeout_sec=int(raw_run.get("timeout_sec", suite.defaults.timeout_sec)),
        stop_on_transfer=bool(
            raw_run.get("stop_on_transfer", suite.defaults.stop_on_transfer)
        ),
    )

    expect = parse_expect(path, data.get("expect"))
    # 스위트 전역 금지 패턴을 시나리오 패턴 앞에 합친다(중복 제거, 순서 유지).
    merged: list[str] = []
    for pattern in [*suite.defaults.agent_regex_none, *expect.agent_regex_none]:
        if pattern not in merged:
            merged.append(pattern)
    expect.agent_regex_none = merged

    raw_api_fixture = data.get("api_fixture")
    api_fixture = None
    if raw_api_fixture is not None:
        api_fixture = (suite.root / str(raw_api_fixture)).resolve()
        if not api_fixture.exists():
            raise ScenarioError(path, f"api_fixture not found: {raw_api_fixture}")

    allow_blocked = data.get("allow_blocked", False)
    if not isinstance(allow_blocked, bool):
        raise ScenarioError(path, "allow_blocked must be a bool")
    runtime_probe = data.get("runtime_probe")
    if runtime_probe is not None and runtime_probe not in RUNTIME_PROBES:
        raise ScenarioError(
            path,
            f"unknown runtime_probe {runtime_probe!r} "
            f"(allowed: {', '.join(RUNTIME_PROBES)})",
        )
    if runtime_probe is not None and api_fixture is None:
        raise ScenarioError(path, "runtime_probe requires api_fixture")

    return Scenario(
        id=scenario_id,
        title=str(data["title"]),
        intent=intent,
        difficulty=difficulty,
        tags=_str_list(path, data.get("tags"), "tags"),
        dial_info=dial_info,
        phase=phase,
        api_fixture=api_fixture,
        runtime_probe=runtime_probe,
        allow_blocked=allow_blocked,
        persona={str(k): v for k, v in persona.items()},
        run=run,
        expect=expect,
        path=path,
    )


def load_scenarios(suite: Suite) -> list[Scenario]:
    """스위트가 가리키는 모든 디렉터리에서 시나리오를 재귀적으로 읽는다."""
    scenarios: list[Scenario] = []
    seen: dict[str, Path] = {}
    for directory in suite.scenario_dirs:
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*.yaml")):
            if path.name.startswith("."):
                continue
            scenario = load_scenario(path, suite)
            if scenario.id in seen:
                raise ScenarioError(
                    path, f"duplicate id {scenario.id!r} (also in {seen[scenario.id]})"
                )
            seen[scenario.id] = path
            scenarios.append(scenario)
    scenarios.sort(key=lambda s: (s.phase, s.intent, s.id))
    return scenarios
