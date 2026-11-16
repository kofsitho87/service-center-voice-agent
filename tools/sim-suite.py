#!/usr/bin/env python3
"""고객 시뮬레이션 회귀 스위트 러너.

`simulations/suite.yaml`이 가리키는 시나리오들을 텍스트 모드 customer-sim으로
돌리고, 결정론적 체크 + LLM 저지로 PASS/FAIL 리포트를 만든다.

사용:
    uv run tools/sim-suite.py --list
    uv run tools/sim-suite.py                       # 전체 실행
    uv run tools/sim-suite.py --intent booking_create --difficulty easy
    uv run tools/sim-suite.py --id create_easy_01_phone_inspection
    uv run tools/sim-suite.py --phase 1 --no-judge
    uv run tools/sim-suite.py --compare logs/simulations/suite/<id>/summary.json
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sim_suite.report import (
    build_report_md,
    compare_summaries,
    load_summary,
    validate_comparison_metadata,
    write_report_md,
    write_summary_json,
)
from sim_suite.runner import (
    VERDICT_ERROR,
    VERDICT_FAIL,
    VERDICT_JUDGE_FAIL,
    RunResult,
    run_suite,
    sweep_suite_orphans,
)
from sim_suite.schema import (
    DIFFICULTIES,
    INTENTS,
    Scenario,
    ScenarioError,
    Suite,
    load_scenarios,
    load_suite,
)

FAKE_RESERVATION_BASEURL = "http://booking.fake.invalid"  # mirrors src/fake_backend/config.py
PRD_HOST_MARKER = "production.invalid"  # placeholder production host marker
DEFAULT_SUITE = "simulations/suite.yaml"
DEFAULT_OUT_ROOT = "logs/simulations/suite"


def resolve_api_base_url() -> str:
    """예약 API base URL을 해석한다. 미설정이면 프로세스 내 가짜 백엔드다."""
    return os.getenv("RESERVATION_API_BASEURL", "") or FAKE_RESERVATION_BASEURL


def fixture_identity(suite: Suite) -> list[tuple[str, str, str, str]]:
    """(fixture key, path name, site_id, client_id) 목록."""
    import yaml

    rows = []
    for key, path in sorted(suite.fixtures.items()):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        business = data.get("business_data") or {}
        rows.append(
            (
                key,
                path.name,
                str(business.get("site_id", "?")),
                str(business.get("client_id", "?")),
            )
        )
    return rows


def select_scenarios(scenarios: list[Scenario], args) -> list[Scenario]:
    selected = scenarios
    if args.id:
        wanted = set(args.id)
        selected = [s for s in selected if s.id in wanted]
    if args.intent:
        wanted = set(args.intent)
        selected = [s for s in selected if s.intent in wanted]
    if args.difficulty:
        wanted = set(args.difficulty)
        selected = [s for s in selected if s.difficulty in wanted]
    if args.phase:
        wanted = set(args.phase)
        selected = [s for s in selected if s.phase in wanted]
    if args.tags:
        wanted = {tag.strip() for tag in args.tags.split(",") if tag.strip()}
        selected = [s for s in selected if wanted & set(s.tags)]
    return selected


def print_selection(scenarios: list[Scenario], suite: Suite) -> None:
    print(f"선택된 시나리오: {len(scenarios)}")
    for phase in sorted({s.phase for s in scenarios}):
        label = suite.phases.get(phase, "")
        batch = [s for s in scenarios if s.phase == phase]
        print(f"\n[phase {phase}] {label} — {len(batch)}건")
        for scenario in batch:
            tags = ",".join(scenario.tags) or "-"
            print(
                f"  {scenario.id:<40} {scenario.intent:<16} "
                f"{scenario.difficulty:<8} {scenario.dial_info:<20} tags={tags}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="고객 시뮬레이션 회귀 스위트 러너 (텍스트 모드 전용)"
    )
    parser.add_argument("--suite", default=DEFAULT_SUITE, help="suite.yaml 경로")
    parser.add_argument(
        "--intent", action="append", choices=list(INTENTS), help="의도 필터(반복 가능)"
    )
    parser.add_argument(
        "--difficulty",
        action="append",
        choices=list(DIFFICULTIES),
        help="난이도 필터(반복 가능)",
    )
    parser.add_argument("--tags", help="태그 필터(쉼표 구분, 하나라도 맞으면 선택)")
    parser.add_argument("--id", action="append", help="시나리오 id 필터(반복 가능)")
    parser.add_argument(
        "--phase", action="append", type=int, help="phase 필터(반복 가능)"
    )
    parser.add_argument("--list", action="store_true", help="선택 결과만 출력하고 종료")
    parser.add_argument("--concurrency", type=int, help="phase 내 동시 실행 수")
    parser.add_argument("--agent-model", help="에이전트 LLM (dial_info에 주입)")
    parser.add_argument("--customer-model", help="고객 연기 LLM")
    parser.add_argument("--judge-model", help="저지 LLM")
    parser.add_argument("--no-judge", action="store_true", help="LLM 저지 비활성화")
    parser.add_argument(
        "--strict", action="store_true", help="JUDGE_FAIL도 실패로 취급(exit 1)"
    )
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="BLOCKED가 하나라도 있으면 실패(exit 1)",
    )
    parser.add_argument("--compare", help="이전 summary.json 경로(회귀 비교)")
    parser.add_argument("--out-root", default=DEFAULT_OUT_ROOT, help="결과 저장 루트")
    parser.add_argument(
        "--allow-prd", action="store_true", help="운영 예약 API 사용 허용"
    )
    parser.add_argument(
        "--run-timeout", type=int, help="시나리오별 타임아웃(초) 강제 지정"
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=1,
        help="스톨로 보이는 런의 재시도 횟수 (기본 1, 0이면 비활성화)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    load_dotenv(PROJECT_ROOT / ".env", override=False)

    suite_path = Path(args.suite)
    if not suite_path.is_absolute():
        suite_path = PROJECT_ROOT / suite_path
    try:
        suite = load_suite(suite_path)
        scenarios = load_scenarios(suite)
    except ScenarioError as exc:
        print(f"{exc.path}: {exc.message}", file=sys.stderr)
        return 1

    selected = select_scenarios(scenarios, args)
    if not selected:
        print(
            f"경고: 필터에 맞는 시나리오가 없습니다 "
            f"(전체 {len(scenarios)}건). 아무것도 실행하지 않습니다.",
            file=sys.stderr,
        )
        return 2

    if args.list:
        print_selection(selected, suite)
        return 0

    agent_model = args.agent_model or suite.defaults.agent_model
    judge_model = args.judge_model or suite.defaults.judge_model
    customer_model = args.customer_model or suite.defaults.customer_model
    concurrency = args.concurrency or suite.defaults.concurrency
    use_judge = not args.no_judge

    env_name = os.getenv("ENV", "(unset)")
    api_base_url = resolve_api_base_url()
    uses_external_api = any(scenario.api_fixture is None for scenario in selected)
    displayed_api_base_url = (
        api_base_url if uses_external_api else "(scenario-local fixtures)"
    )
    # Demo: the fake backend is always safe; any other host still needs ENV=dev
    # (and is blocked by the agent's fake transport regardless).
    unsafe_external_env = (
        uses_external_api
        and api_base_url != FAKE_RESERVATION_BASEURL
        and env_name.lower() != "dev"
    )
    if (
        uses_external_api
        and (PRD_HOST_MARKER in api_base_url or unsafe_external_env)
        and not args.allow_prd
    ):
        print(
            f"거부: 외부 예약 API 실행은 ENV=dev와 명시된 dev URL이 필요합니다 "
            f"(ENV={env_name}, API={api_base_url}). 정말 필요하면 --allow-prd를 "
            f"명시하세요.",
            file=sys.stderr,
        )
        return 1

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_root = Path(args.out_root)
    if not out_root.is_absolute():
        out_root = PROJECT_ROOT / out_root
    out_dir = out_root / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print(f"sim-suite 실행 {run_id}")
    print(f"  ENV              : {env_name}")
    print(f"  예약 API         : {displayed_api_base_url}")
    print(f"  agent model      : {agent_model}")
    print(f"  customer model    : {customer_model or '(customer-sim 기본값)'}")
    print(f"  judge model      : {judge_model if use_judge else '(비활성화)'}")
    print(f"  concurrency      : {concurrency}")
    print(f"  retries          : {args.retries}")
    print("  dial_info fixtures:")
    for key, name, site_id, client_id in fixture_identity(suite):
        print(f"    - {key:<20} {name:<28} site_id={site_id} client_id={client_id}")
    print("  시나리오:")
    for phase in sorted({s.phase for s in selected}):
        label = suite.phases.get(phase, "")
        count = sum(1 for s in selected if s.phase == phase)
        print(f"    - phase {phase} ({label}): {count}건")
    print(f"  결과 디렉터리    : {out_dir}")
    print("=" * 72, flush=True)

    def on_result(result: RunResult, done: int, total: int) -> None:
        print(
            f"[{done}/{total}] {result.scenario_id} → {result.verdict} "
            f"({result.outcome}, {result.elapsed_sec}s)"
            + (" TIMEOUT" if result.timed_out else "")
            + (" (early stop)" if result.stopped_early else "")
            + (f" [flaky, {result.attempts} attempts]" if result.flaky else "")
            + (
                f" [{result.attempts} attempts]"
                if result.attempts > 1 and not result.flaky
                else ""
            ),
            flush=True,
        )

    results = run_suite(
        selected,
        suite,
        out_dir,
        agent_model=agent_model,
        customer_model=customer_model,
        judge_model=judge_model,
        use_judge=use_judge,
        concurrency=concurrency,
        run_timeout=args.run_timeout,
        retries=max(0, args.retries),
        on_result=on_result,
    )

    # customer-sim 이 강제 종료된 런은 자기 에이전트 콘솔을 남길 수 있다.
    # 이 스위트 실행 태그가 붙은 잔존 프로세스를 마지막으로 한 번 더 쓸어낸다.
    swept = sweep_suite_orphans(out_dir)
    if swept:
        print(f"\n고아 에이전트 프로세스 {len(swept)}개를 정리했습니다: {swept}")

    metadata = {
        "agent_model": agent_model,
        "judge_model": judge_model if use_judge else None,
        "scenario_ids": sorted(scenario.id for scenario in selected),
    }
    diff = None
    if args.compare:
        try:
            previous = load_summary(args.compare)
            validate_comparison_metadata(previous, metadata)
            diff = compare_summaries(previous, results)
        except (OSError, ValueError) as exc:
            print(f"거부: --compare 기준선을 사용할 수 없습니다 ({exc})", file=sys.stderr)
            return 1

    summary_path = write_summary_json(
        results,
        out_dir / "summary.json",
        metadata=metadata,
    )
    report_path = write_report_md(
        build_report_md(
            results,
            suite,
            run_id=run_id,
            agent_model=agent_model,
            judge_model=judge_model if use_judge else None,
            api_base_url=displayed_api_base_url,
            env_name=env_name,
            diff=diff,
            compare_path=args.compare,
        ),
        out_dir / "report.md",
    )

    counts: dict[str, int] = {}
    for result in results:
        counts[result.verdict] = counts.get(result.verdict, 0) + 1
    passed = counts.get("PASS", 0)
    blocked = counts.get("BLOCKED", 0)
    print("\n=== 요약 ===")
    print(f"  PASS {passed}/{len(results)}")
    for verdict, count in sorted(counts.items()):
        print(f"  {verdict}: {count}")
    for layer in ("business_status", "quality_status", "harness_status"):
        layer_counts: dict[str, int] = {}
        for result in results:
            value = getattr(result, layer)
            layer_counts[value] = layer_counts.get(value, 0) + 1
        print(
            f"  {layer}: "
            + ", ".join(
                f"{key}={value}" for key, value in sorted(layer_counts.items())
            )
        )
    flaky = sum(1 for result in results if result.flaky)
    if flaky:
        print(
            f"  flaky: {flaky}건 (스톨 후 재시도에서 통과 — 실행 간 추이를 세어볼 것)"
        )
    if blocked:
        blocked_effect = (
            "--require-complete로 종료 코드 1"
            if args.require_complete
            else "기본 종료 코드에는 영향 없음"
        )
        print(
            f"  (BLOCKED {blocked}건은 dev 전제 조건 미충족입니다 — "
            f"에이전트 실패가 아니며 {blocked_effect}.)"
        )
    if diff is not None:
        print(f"  newly failing: {len(diff.newly_failing)}")
        print(f"  newly passing: {len(diff.newly_passing)}")
    print(f"\n저장: {summary_path}")
    print(f"      {report_path}")

    hard_fail = any(
        result.verdict in (VERDICT_FAIL, VERDICT_ERROR) for result in results
    )
    judge_fail = any(result.verdict == VERDICT_JUDGE_FAIL for result in results)
    incomplete = any(result.verdict == "BLOCKED" for result in results)
    if hard_fail or (args.strict and judge_fail) or (args.require_complete and incomplete):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
