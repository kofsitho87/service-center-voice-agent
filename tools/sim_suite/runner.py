"""시나리오 실행기.

각 시나리오마다
1. 픽스처 dial_info를 복사해 `call_config_data.llm_model`을 주입하고,
2. `tools/customer-sim.py`를 자체 프로세스 그룹으로 띄운 뒤,
3. `events.jsonl`을 파싱해 결정론적 체크와 LLM 저지를 돌린다.

프로세스 그룹/타임아웃 처리는 `tools/customer-sim-batch.py`의 `run_one`
패턴을 그대로 따른다(타임아웃 시 해당 그룹에만 SIGINT → 10초 후 SIGKILL).
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from . import PROJECT_ROOT
from .api_fixture import BookingApiFixtureServer
from .checks import (
    CheckResult,
    check_api_requests,
    check_outcome,
    failed_checks,
    run_checks,
)
from .events import (
    ParsedEvents,
    derive_outcome,
    find_session_dir,
    parse_events,
    should_stop_early,
)
from .judge import DEFAULT_JUDGE_MODEL, JudgeResult, judge_session
from .orphans import RUN_TAG_ENV, make_run_tag, reap_orphans, suite_tag_prefix
from .schema import Scenario, Suite

UV = os.environ.get("UV_BIN", "uv")

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_JUDGE_FAIL = "JUDGE_FAIL"
VERDICT_ERROR = "ERROR"
VERDICT_BLOCKED = "BLOCKED"

# BLOCKED 판정이 가능한 의도. dev 예약 API 는 create 요청을 "예약 신청"(pending)
# 으로만 남기므로 CONFIRMED 예약이 없고, 변경/취소 시나리오는 대화가 시작되기도
# 전에 "변경할 예약내역이 없습니다"로 끝난다. 에이전트 결함이 아니라 전제 조건
# 미충족이므로 실패로 세지 않는다.
PRECONDITION_INTENTS = ("booking_modify", "booking_cancel")

# 실행 중 세션 진행 상황을 확인하는 주기.
POLL_INTERVAL_SEC = 5.0

# 재시도로 판정이 뒤집혔을 때 flaky 로 볼 (이전, 이후) 판정 집합.
FLAKY_BEFORE = (VERDICT_ERROR, VERDICT_FAIL)
FLAKY_AFTER = (VERDICT_PASS, VERDICT_JUDGE_FAIL, VERDICT_BLOCKED)

BUSINESS_PASS = "PASS"
BUSINESS_FAIL = "FAIL"
BUSINESS_BLOCKED = "BLOCKED"
BUSINESS_NOT_EVALUATED = "NOT_EVALUATED"
QUALITY_PASS = "PASS"
QUALITY_FAIL = "FAIL"
QUALITY_ERROR = "ERROR"
QUALITY_NOT_RUN = "NOT_RUN"
HARNESS_CLEAN = "CLEAN"
HARNESS_CLEANUP = "CLEANUP_REQUIRED"
HARNESS_STALL_AFTER_OUTCOME = "STALL_AFTER_OUTCOME"
HARNESS_ERROR = "ERROR"


@dataclass
class RunResult:
    scenario_id: str
    title: str
    intent: str
    difficulty: str
    phase: int
    tags: list[str]
    verdict: str
    outcome: str
    elapsed_sec: float
    timed_out: bool
    exit_code: int | None
    business_status: str = BUSINESS_NOT_EVALUATED
    quality_status: str = QUALITY_NOT_RUN
    harness_status: str = HARNESS_CLEAN
    # 대화가 끝났는데 콘솔이 안 죽어서 러너가 먼저 정리한 경우. 타임아웃과
    # 달리 실패 신호가 아니다.
    stopped_early: bool = False
    # 이 시나리오를 실제로 몇 번 돌렸는지(재시도 포함).
    attempts: int = 1
    # 한 번 스톨했다가 재시도에서 통과한 경우. 그 자체가 관찰 대상이다.
    flaky: bool = False
    # 이 런이 남긴 고아 에이전트 콘솔을 정리한 pid 목록.
    orphans_killed: list[int] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    judge: JudgeResult | None = None
    run_dir: Path | None = None
    session_dir: Path | None = None
    customer_turns: int = 0
    tools: list[str] = field(default_factory=list)
    handoffs: list[str] = field(default_factory=list)
    api_requests: list[str] = field(default_factory=list)
    # 마지막 에이전트 발화 몇 줄. 실패 리포트에서 멈춘 지점을 바로 보여준다.
    last_agent_msgs: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.scenario_id,
            "title": self.title,
            "intent": self.intent,
            "difficulty": self.difficulty,
            "phase": self.phase,
            "tags": self.tags,
            "verdict": self.verdict,
            "outcome": self.outcome,
            "elapsed_sec": self.elapsed_sec,
            "timed_out": self.timed_out,
            "stopped_early": self.stopped_early,
            "attempts": self.attempts,
            "flaky": self.flaky,
            "orphans_killed": self.orphans_killed,
            "exit_code": self.exit_code,
            "business_status": self.business_status,
            "quality_status": self.quality_status,
            "harness_status": self.harness_status,
            "checks": [check.to_dict() for check in self.checks],
            "judge": self.judge.to_dict() if self.judge else None,
            "run_dir": str(self.run_dir) if self.run_dir else None,
            "session_dir": str(self.session_dir) if self.session_dir else None,
            "customer_turns": self.customer_turns,
            "tools": self.tools,
            "handoffs": self.handoffs,
            "api_requests": self.api_requests,
            "last_agent_msgs": self.last_agent_msgs,
            "error": self.error,
        }


def materialise_dial_info(
    scenario: Scenario, suite: Suite, run_dir: Path, agent_model: str
) -> Path:
    """픽스처를 복사하고 `call_config_data.llm_model`을 주입해 저장한다.

    dial_info의 `llm_model` 값은 `AGENT_LLM_MODEL` 환경변수/`--agent-model`
    보다 우선하므로, 스위트가 쓰는 모델을 파일에 직접 써 넣어야 한다.
    """
    fixture_path = suite.fixtures[scenario.dial_info]
    data = yaml.safe_load(fixture_path.read_text(encoding="utf-8")) or {}
    data = copy.deepcopy(data)
    call_config = data.setdefault("call_config_data", {})
    call_config["llm_model"] = agent_model

    target = run_dir / "dial_info.yaml"
    target.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return target


def materialise_persona(scenario: Scenario, run_dir: Path) -> Path:
    target = run_dir / "persona.yaml"
    target.write_text(
        yaml.safe_dump(scenario.persona, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return target


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """SIGINT → 10초 대기 → SIGKILL. 이 런의 프로세스 그룹에만 보낸다."""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    # SIGINT → customer-sim 의 KeyboardInterrupt 경로가 _shutdown 을 돌려
    # 자식 에이전트까지 정리한다.
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(pgid, signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(pgid, signal.SIGKILL)


def _events_path(sim_root: Path) -> Path | None:
    session_dir = find_session_dir(sim_root)
    if session_dir is None:
        return None
    events_path = session_dir / "events.jsonl"
    return events_path if events_path.exists() else None


def _events_signature(events_path: Path | None) -> tuple[float, int] | None:
    """events.jsonl 이 변했는지 싸게 판별하기 위한 (mtime, size)."""
    if events_path is None:
        return None
    try:
        stat = events_path.stat()
    except OSError:
        return None
    return (stat.st_mtime, stat.st_size)


def _spawn_customer_sim(
    *,
    persona_path: Path,
    dial_info_path: Path,
    sim_root: Path,
    stop_on_transfer: bool,
    customer_model: str | None,
    run_timeout: int,
    log_path: Path,
    run_tag: str,
    reservation_api_base_url: str | None = None,
) -> tuple[bool, bool, int | None, float]:
    """customer-sim 을 돌리고 (timed_out, stopped_early, exit_code, elapsed) 반환."""
    env = dict(os.environ)
    # DIAL_INFO_FILE 이 남아 있으면 DIAL_INFO_JSON 보다 우선한다
    # (src/utils/common.py::resolve_dial_info_path). 반드시 제거한다.
    env.pop("DIAL_INFO_FILE", None)
    env["DIAL_INFO_JSON"] = str(dial_info_path)
    # customer-sim 은 자신의 env 를 에이전트 콘솔에 그대로 물려준다. 이 태그가
    # 있어야 런이 끝난 뒤 남은 에이전트를 안전하게 찾아 정리할 수 있다.
    env[RUN_TAG_ENV] = run_tag
    env["SIM_SUITE_PRESERVE_ENV"] = "1"
    if reservation_api_base_url:
        env["SIM_RESERVATION_API_BASEURL"] = reservation_api_base_url

    cmd = [
        UV,
        "run",
        "tools/customer-sim.py",
        "--persona-file",
        str(persona_path),
        "--dial-info-json",
        str(dial_info_path),
        "--sim-root",
        str(sim_root),
        # 항상 원본 에이전트 출력을 남긴다. 에이전트가 한 마디 하고 침묵한 채
        # 타임아웃까지 가는 런은 agent_raw.log 없이는 진단할 수 없다.
        "--raw-log",
    ]
    if stop_on_transfer:
        cmd.append("--stop-on-transfer")
    if customer_model:
        cmd += ["--model", customer_model]

    started = time.time()
    timed_out = False
    stopped_early = False
    with log_path.open("w", encoding="utf-8") as log:
        # start_new_session=True → 이 런만의 프로세스 그룹. 종료 신호는
        # 이 그룹에만 보내 다른 동시 런을 건드리지 않는다.
        proc = subprocess.Popen(
            cmd,
            cwd=PROJECT_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = started + run_timeout
        last_signature: tuple[float, int] | None = None
        last_change = started
        while True:
            try:
                proc.wait(timeout=POLL_INTERVAL_SEC)
                break
            except subprocess.TimeoutExpired:
                pass

            now = time.time()
            if now >= deadline:
                timed_out = True
                _terminate_process_group(proc)
                break

            events_path = _events_path(sim_root)
            signature = _events_signature(events_path)
            if signature != last_signature:
                last_signature = signature
                last_change = now
                continue

            # 대화는 끝났는데 콘솔이 살아 있는 경우. 남은 timeout_sec 를
            # 통째로 태우지 않고 정상 종료 경로로 정리한다.
            if events_path and should_stop_early(
                parse_events(events_path), idle_sec=now - last_change
            ):
                stopped_early = True
                _terminate_process_group(proc)
                break
    elapsed = round(time.time() - started, 1)
    return timed_out, stopped_early, proc.returncode, elapsed


def is_blocked(scenario: Scenario, outcome: str) -> bool:
    """dev 데이터 전제 조건이 없어 시나리오를 시작조차 못 한 경우인지 판별한다."""
    return (
        outcome == "no_reservation"
        and scenario.intent in PRECONDITION_INTENTS
        and scenario.allow_blocked
        and "no_reservation" not in scenario.expect.outcomes
    )


def compute_layer_statuses(
    checks: list[CheckResult],
    judge: JudgeResult | None,
    *,
    timed_out: bool,
    parsed: ParsedEvents,
    scenario: Scenario,
    outcome: str,
    stopped_early: bool = False,
) -> tuple[str, str, str]:
    if is_blocked(scenario, outcome):
        business_status = BUSINESS_BLOCKED
    else:
        business_failures = [
            check
            for check in failed_checks(checks)
            if check.name != "session_ended_cleanly"
        ]
        business_status = BUSINESS_FAIL if business_failures else BUSINESS_PASS

    if judge is None:
        quality_status = QUALITY_NOT_RUN
    elif judge.verdict == VERDICT_ERROR:
        quality_status = QUALITY_ERROR
    elif judge.verdict == "FAIL":
        quality_status = QUALITY_FAIL
    else:
        quality_status = QUALITY_PASS

    expected_outcome = outcome in scenario.expect.outcomes and outcome != "no_outcome"
    if not parsed.has_events:
        harness_status = HARNESS_ERROR
    elif timed_out:
        harness_status = (
            HARNESS_STALL_AFTER_OUTCOME if expected_outcome else HARNESS_ERROR
        )
    elif stopped_early:
        harness_status = HARNESS_CLEANUP
    elif not parsed.session_ended:
        harness_status = HARNESS_ERROR
    else:
        harness_status = HARNESS_CLEAN

    return business_status, quality_status, harness_status


def compute_verdict(
    checks: list[CheckResult],
    judge: JudgeResult | None,
    *,
    timed_out: bool,
    parsed: ParsedEvents,
    scenario: Scenario,
    outcome: str,
    stopped_early: bool = False,
) -> str:
    business, quality, harness = compute_layer_statuses(
        checks,
        judge,
        timed_out=timed_out,
        parsed=parsed,
        scenario=scenario,
        outcome=outcome,
        stopped_early=stopped_early,
    )
    if business == BUSINESS_BLOCKED:
        return VERDICT_BLOCKED
    if harness == HARNESS_ERROR:
        return VERDICT_ERROR
    if business == BUSINESS_FAIL:
        return VERDICT_FAIL
    if quality == QUALITY_ERROR:
        return VERDICT_ERROR
    if quality == QUALITY_FAIL:
        return VERDICT_JUDGE_FAIL
    return VERDICT_PASS


def _write_result_json(result: RunResult, run_dir: Path) -> None:
    """판정 직후 런 디렉터리에 개별 결과를 남긴다.

    스위트가 중간에 끊겨도 이미 끝난 시나리오의 판정은 디스크에 남는다.
    `summary.json` 은 끝까지 간 실행의 집계본이다.
    """
    with contextlib.suppress(OSError, TypeError, ValueError):
        (run_dir / "result.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _run_runtime_probe(
    scenario: Scenario,
    run_dir: Path,
    *,
    attempt: int,
) -> RunResult:
    result = RunResult(
        scenario_id=scenario.id,
        title=scenario.title,
        intent=scenario.intent,
        difficulty=scenario.difficulty,
        phase=scenario.phase,
        tags=list(scenario.tags),
        verdict=VERDICT_ERROR,
        outcome="no_outcome",
        elapsed_sec=0.0,
        timed_out=False,
        exit_code=None,
        run_dir=run_dir,
        attempts=attempt,
    )
    if scenario.api_fixture is None or scenario.runtime_probe is None:
        result.error = "runtime probe requires api_fixture and runtime_probe"
        return result

    server = BookingApiFixtureServer(
        scenario.api_fixture,
        run_dir / "api_requests.jsonl",
    )
    started = time.time()
    try:
        server.start()
        env = {
            **os.environ,
            "RESERVATION_API_BASEURL": server.base_url,
            "SIM_SUITE_PRESERVE_ENV": "1",
        }
        completed = subprocess.run(
            [UV, "run", "python", "tools/sim-suite-probe.py", scenario.runtime_probe],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=scenario.run.timeout_sec,
            check=False,
        )
        result.exit_code = completed.returncode
        (run_dir / "probe_stdout.log").write_text(
            completed.stdout, encoding="utf-8"
        )
        (run_dir / "probe_stderr.log").write_text(
            completed.stderr, encoding="utf-8"
        )
        probe_passed = completed.returncode == 0
        result.outcome = "probe_passed" if probe_passed else "no_outcome"
        result.error = "" if probe_passed else completed.stderr.strip()
    except subprocess.TimeoutExpired:
        result.timed_out = True
        result.error = "runtime probe timed out"
    except Exception as exc:
        result.error = f"runtime probe failed: {exc}"
    finally:
        result.api_requests = server.requests
        server.stop()

    result.elapsed_sec = round(time.time() - started, 1)
    result.checks = [
        CheckResult(
            "runtime_probe",
            result.outcome == "probe_passed",
            result.error or "probe completed",
        ),
        check_outcome(scenario.expect, result.outcome),
        *check_api_requests(scenario.expect, result.api_requests),
    ]
    result.verdict = (
        VERDICT_ERROR
        if result.timed_out
        else VERDICT_FAIL
        if failed_checks(result.checks)
        else VERDICT_PASS
    )
    result.business_status = (
        BUSINESS_PASS if result.verdict == VERDICT_PASS else BUSINESS_FAIL
    )
    result.quality_status = QUALITY_NOT_RUN
    result.harness_status = HARNESS_ERROR if result.timed_out else HARNESS_CLEAN
    _write_result_json(result, run_dir)
    return result


def run_scenario(
    scenario: Scenario,
    suite: Suite,
    run_dir: Path,
    *,
    agent_model: str,
    customer_model: str | None = None,
    judge_model: str = DEFAULT_JUDGE_MODEL,
    use_judge: bool = True,
    run_timeout: int | None = None,
    attempt: int = 1,
) -> RunResult:
    """시나리오 하나를 끝까지 실행하고 판정한다."""
    run_dir.mkdir(parents=True, exist_ok=True)
    if scenario.runtime_probe is not None:
        return _run_runtime_probe(scenario, run_dir, attempt=attempt)
    sim_root = run_dir / "session"
    sim_root.mkdir(parents=True, exist_ok=True)

    result = RunResult(
        scenario_id=scenario.id,
        title=scenario.title,
        intent=scenario.intent,
        difficulty=scenario.difficulty,
        phase=scenario.phase,
        tags=list(scenario.tags),
        verdict=VERDICT_ERROR,
        outcome="no_outcome",
        elapsed_sec=0.0,
        timed_out=False,
        exit_code=None,
        run_dir=run_dir,
    )

    try:
        dial_info_path = materialise_dial_info(scenario, suite, run_dir, agent_model)
        persona_path = materialise_persona(scenario, run_dir)
    except Exception as exc:
        result.error = f"setup failed: {exc}"
        return result

    timeout = run_timeout or scenario.run.timeout_sec
    run_tag = make_run_tag(run_dir.parent.name, scenario.id, attempt)
    fixture_server: BookingApiFixtureServer | None = None
    try:
        if scenario.api_fixture is not None:
            fixture_server = BookingApiFixtureServer(
                scenario.api_fixture,
                run_dir / "api_requests.jsonl",
            )
            fixture_server.start()
        timed_out, stopped_early, exit_code, elapsed = _spawn_customer_sim(
            persona_path=persona_path,
            dial_info_path=dial_info_path,
            sim_root=sim_root,
            stop_on_transfer=scenario.run.stop_on_transfer,
            customer_model=customer_model,
            run_timeout=timeout,
            log_path=run_dir / "stdout.log",
            run_tag=run_tag,
            reservation_api_base_url=(
                fixture_server.base_url if fixture_server is not None else None
            ),
        )
    except Exception as exc:
        result.error = f"customer-sim spawn failed: {exc}"
        return result
    finally:
        if fixture_server is not None:
            result.api_requests = fixture_server.requests
            fixture_server.stop()
        # customer-sim 이 SIGKILL 로 죽으면 자기 자식(에이전트 콘솔)을 정리할 수
        # 없다. 태그가 붙은 잔존 프로세스만 직접 수거한다.
        result.orphans_killed = reap_orphans(run_tag)

    result.timed_out = timed_out
    result.stopped_early = stopped_early
    result.exit_code = exit_code
    result.elapsed_sec = elapsed

    session_dir = find_session_dir(sim_root)
    result.session_dir = session_dir
    parsed = parse_events(session_dir / "events.jsonl" if session_dir else None)
    result.tools = list(parsed.tools)
    result.handoffs = list(parsed.handoffs)
    result.customer_turns = parsed.customer_turns
    result.last_agent_msgs = parsed.agent_msgs[-3:]

    outcome = derive_outcome(parsed)
    result.outcome = outcome
    result.checks = run_checks(
        scenario.expect,
        parsed,
        outcome,
        timed_out=timed_out,
        api_requests=result.api_requests,
    )

    if not parsed.has_events:
        result.error = result.error or "no events recorded"

    blocked = is_blocked(scenario, outcome)
    if blocked:
        result.error = (
            "dev precondition missing: no CONFIRMED reservation for this customer"
        )

    judge_result: JudgeResult | None = None
    expected_outcome_reached = outcome in scenario.expect.outcomes
    if (
        use_judge
        and parsed.has_events
        and not blocked
        and (not timed_out or expected_outcome_reached)
    ):
        judge_result = judge_session(scenario, parsed, model_name=judge_model)
    result.judge = judge_result

    (
        result.business_status,
        result.quality_status,
        result.harness_status,
    ) = compute_layer_statuses(
        result.checks,
        judge_result,
        timed_out=timed_out,
        parsed=parsed,
        scenario=scenario,
        outcome=outcome,
        stopped_early=stopped_early,
    )

    result.verdict = compute_verdict(
        result.checks,
        judge_result,
        timed_out=timed_out,
        parsed=parsed,
        scenario=scenario,
        outcome=outcome,
        stopped_early=stopped_early,
    )
    _write_result_json(result, run_dir)
    return result


def should_retry(result: RunResult) -> bool:
    """이 결과가 간헐적 스톨로 보여 재시도할 가치가 있는지 판별한다(순수 함수).

    텍스트 모드에서 에이전트가 안내만 하고 도구를 호출하지 않는 스톨은
    브랜치·모델과 무관하게 간헐적으로 재현된다(같은 시나리오가 몇 분 뒤
    깨끗하게 통과). 그래서 아래 두 신호는 실패로 확정하기 전에 한 번 더 돌린다.
    """
    if result.verdict == VERDICT_ERROR:
        return True
    return result.outcome == "no_outcome"


def is_flaky(first_verdict: str, last_verdict: str) -> bool:
    """재시도가 판정을 뒤집었는지 판별한다(순수 함수)."""
    return first_verdict in FLAKY_BEFORE and last_verdict in FLAKY_AFTER


def run_scenario_with_retries(
    scenario: Scenario,
    suite: Suite,
    out_dir: Path,
    *,
    retries: int,
    **kwargs,
) -> RunResult:
    """스톨로 보이면 새 런 디렉터리에서 다시 돌린다. 마지막 시도를 보고한다.

    모든 시도의 아티팩트는 `<id>`, `<id>__retry1`, ... 로 남는다.
    """
    result = run_scenario(scenario, suite, out_dir / scenario.id, attempt=1, **kwargs)
    first_verdict = result.verdict
    attempt = 1

    while attempt <= retries and should_retry(result):
        retry_dir = out_dir / f"{scenario.id}__retry{attempt}"
        result = run_scenario(scenario, suite, retry_dir, attempt=attempt + 1, **kwargs)
        attempt += 1

    result.attempts = attempt
    result.flaky = attempt > 1 and is_flaky(first_verdict, result.verdict)
    if attempt > 1:
        _write_result_json(result, result.run_dir or out_dir / scenario.id)
    return result


def sweep_suite_orphans(out_dir: Path) -> list[int]:
    """스위트 실행 전체 태그 접두사로 마지막 한 번 더 수거한다."""
    return reap_orphans(suite_tag_prefix(out_dir.name), prefix=True)


def run_suite(
    scenarios: list[Scenario],
    suite: Suite,
    out_dir: Path,
    *,
    agent_model: str,
    customer_model: str | None,
    judge_model: str,
    use_judge: bool,
    concurrency: int,
    run_timeout: int | None = None,
    retries: int = 1,
    on_result: Callable[[RunResult, int, int], None] | None = None,
) -> list[RunResult]:
    """phase 순서대로, phase 안에서는 동시에 실행한다."""
    total = len(scenarios)
    done = 0
    results: list[RunResult] = []

    phases = sorted({scenario.phase for scenario in scenarios})
    for phase in phases:
        batch = [scenario for scenario in scenarios if scenario.phase == phase]
        workers = max(1, min(concurrency, len(batch)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    run_scenario_with_retries,
                    scenario,
                    suite,
                    out_dir,
                    retries=retries,
                    agent_model=agent_model,
                    customer_model=customer_model,
                    judge_model=judge_model,
                    use_judge=use_judge,
                    run_timeout=run_timeout,
                ): scenario
                for scenario in batch
            }
            for future in as_completed(futures):
                scenario = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = RunResult(
                        scenario_id=scenario.id,
                        title=scenario.title,
                        intent=scenario.intent,
                        difficulty=scenario.difficulty,
                        phase=scenario.phase,
                        tags=list(scenario.tags),
                        verdict=VERDICT_ERROR,
                        outcome="no_outcome",
                        elapsed_sec=0.0,
                        timed_out=False,
                        exit_code=None,
                        run_dir=out_dir / scenario.id,
                        error=f"runner crashed: {exc}",
                    )
                results.append(result)
                done += 1
                if on_result:
                    on_result(result, done, total)

    return results
