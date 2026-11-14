"""`summary.json` + `report.md` 작성과 이전 요약 대비 회귀 비교."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runner import VERDICT_PASS, RunResult
from .schema import DIFFICULTIES, INTENTS, Suite

VERDICT_ORDER = ("PASS", "BLOCKED", "JUDGE_FAIL", "FAIL", "ERROR")


@dataclass
class RegressionDiff:
    newly_failing: list[str]
    newly_passing: list[str]
    unchanged_pass: int
    unchanged_fail: int
    changed_nonpass: list[str]
    missing: list[str]
    added: list[str]


@dataclass(frozen=True)
class SummarySnapshot:
    metadata: dict[str, Any]
    verdicts: dict[str, str]


def write_summary_json(
    results: list[RunResult], path: Path, *, metadata: dict[str, Any]
) -> Path:
    payload = {
        "metadata": metadata,
        "results": [result.to_dict() for result in results],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_summary(path: Path | str) -> SummarySnapshot:
    """Read verdicts and comparison metadata from a prior summary."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    metadata: dict[str, Any] = {}
    if isinstance(data, dict):
        metadata = data.get("metadata") or {}
        data = data.get("results", [])
    verdicts = {
        str(row.get("id")): str(row.get("verdict", "ERROR"))
        for row in data
        if isinstance(row, dict) and row.get("id")
    }
    return SummarySnapshot(metadata=metadata, verdicts=verdicts)


def validate_comparison_metadata(
    previous: SummarySnapshot, current: dict[str, Any]
) -> None:
    required = ("agent_model", "scenario_ids")
    missing = [key for key in required if key not in previous.metadata]
    if missing:
        raise ValueError(
            "comparison baseline lacks metadata: " + ", ".join(missing)
        )
    for key in required:
        if previous.metadata.get(key) != current.get(key):
            raise ValueError(
                f"comparison metadata mismatch for {key}: "
                f"{previous.metadata.get(key)!r} != {current.get(key)!r}"
            )


def compare_summaries(
    previous: SummarySnapshot, results: list[RunResult]
) -> RegressionDiff:
    current = {result.scenario_id: result.verdict for result in results}
    newly_failing: list[str] = []
    newly_passing: list[str] = []
    unchanged_pass = 0
    unchanged_fail = 0
    changed_nonpass: list[str] = []

    for scenario_id, verdict in sorted(current.items()):
        before = previous.verdicts.get(scenario_id)
        if before is None:
            continue
        was_pass = before == VERDICT_PASS
        is_pass = verdict == VERDICT_PASS
        if was_pass and not is_pass:
            newly_failing.append(f"{scenario_id} ({before} → {verdict})")
        elif not was_pass and is_pass:
            newly_passing.append(f"{scenario_id} ({before} → {verdict})")
        elif is_pass:
            unchanged_pass += 1
        else:
            unchanged_fail += 1
            if before != verdict:
                changed_nonpass.append(f"{scenario_id} ({before} → {verdict})")

    missing = sorted(set(previous.verdicts) - set(current))
    added = sorted(set(current) - set(previous.verdicts))
    return RegressionDiff(
        newly_failing=newly_failing,
        newly_passing=newly_passing,
        unchanged_pass=unchanged_pass,
        unchanged_fail=unchanged_fail,
        changed_nonpass=changed_nonpass,
        missing=missing,
        added=added,
    )


def _matrix_rows(results: list[RunResult]) -> list[str]:
    intents = [intent for intent in INTENTS if any(r.intent == intent for r in results)]
    difficulties = [
        level for level in DIFFICULTIES if any(r.difficulty == level for r in results)
    ]
    header = "| intent | " + " | ".join(difficulties) + " | total |"
    divider = "|---" * (len(difficulties) + 2) + "|"
    rows = [header, divider]
    for intent in intents:
        cells = []
        for level in difficulties:
            bucket = [
                r for r in results if r.intent == intent and r.difficulty == level
            ]
            if not bucket:
                cells.append("-")
                continue
            passed = sum(1 for r in bucket if r.verdict == VERDICT_PASS)
            cells.append(f"{passed}/{len(bucket)}")
        bucket = [r for r in results if r.intent == intent]
        passed = sum(1 for r in bucket if r.verdict == VERDICT_PASS)
        rows.append(
            f"| {intent} | " + " | ".join(cells) + f" | {passed}/{len(bucket)} |"
        )
    return rows


def _fixture_lines(suite: Suite) -> list[str]:
    import yaml

    lines = []
    for key, path in sorted(suite.fixtures.items()):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        business = data.get("business_data") or {}
        call_config = data.get("call_config_data") or {}
        lines.append(
            f"- `{key}` → `{path.name}` "
            f"(site_id={business.get('site_id')}, "
            f"client_id={business.get('client_id')}, "
            f"transfer_mode={call_config.get('transfer_mode')})"
        )
    return lines


def build_report_md(
    results: list[RunResult],
    suite: Suite,
    *,
    run_id: str,
    agent_model: str,
    judge_model: str | None,
    api_base_url: str,
    env_name: str,
    diff: RegressionDiff | None = None,
    compare_path: str | None = None,
) -> str:
    total = len(results)
    counts = dict.fromkeys(VERDICT_ORDER, 0)
    for result in results:
        counts[result.verdict] = counts.get(result.verdict, 0) + 1
    passed = counts.get(VERDICT_PASS, 0)
    business_counts: dict[str, int] = {}
    quality_counts: dict[str, int] = {}
    harness_counts: dict[str, int] = {}
    for result in results:
        business_counts[result.business_status] = (
            business_counts.get(result.business_status, 0) + 1
        )
        quality_counts[result.quality_status] = (
            quality_counts.get(result.quality_status, 0) + 1
        )
        harness_counts[result.harness_status] = (
            harness_counts.get(result.harness_status, 0) + 1
        )

    flaky_results = [r for r in results if r.flaky]
    retried = [r for r in results if r.attempts > 1]
    orphans_total = sum(len(r.orphans_killed) for r in results)

    lines: list[str] = [
        f"# 고객 시뮬레이션 회귀 스위트 리포트 ({run_id})",
        "",
        f"- 결과: **{passed}/{total} PASS**",
        *(
            [
                f"- 정리한 고아 에이전트 프로세스: **{orphans_total}개** "
                f"(런이 끝난 뒤에도 살아 있던 `agent-cli-text.py console`)"
            ]
            if orphans_total
            else []
        ),
        f"- flaky: **{len(flaky_results)}건** "
        f"(재시도한 시나리오 {len(retried)}건) — 한 번 스톨했다가 재시도에서 "
        f"통과한 건수. 0 이 아니면 그 자체가 관찰 대상이다.",
        "- 판정 분포: "
        + ", ".join(f"{verdict} {counts.get(verdict, 0)}" for verdict in VERDICT_ORDER),
        "- business: "
        + ", ".join(f"{key} {value}" for key, value in sorted(business_counts.items())),
        "- quality: "
        + ", ".join(f"{key} {value}" for key, value in sorted(quality_counts.items())),
        "- harness: "
        + ", ".join(f"{key} {value}" for key, value in sorted(harness_counts.items())),
        f"- agent model: `{agent_model}`",
        f"- judge model: `{judge_model or '(disabled)'}`",
        f"- ENV: `{env_name}` / reservation API: `{api_base_url}`",
        "- dial_info fixtures:",
    ]
    lines += _fixture_lines(suite)
    lines += ["", "## 매트릭스 (intent x difficulty, pass/total)", ""]
    lines += _matrix_rows(results)

    lines += [
        "",
        "## 시나리오별 결과",
        "",
        "| id | intent | diff | phase | verdict | business | quality | harness "
        "| outcome | flaky | turns | sec | run dir |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for result in sorted(results, key=lambda r: (r.phase, r.intent, r.scenario_id)):
        run_dir = result.run_dir.name if result.run_dir else "-"
        if result.flaky:
            flaky_cell = f"⚠︎ {result.attempts}회"
        elif result.attempts > 1:
            flaky_cell = f"{result.attempts}회"
        else:
            flaky_cell = ""
        lines.append(
            f"| {result.scenario_id} | {result.intent} | {result.difficulty} "
            f"| {result.phase} | **{result.verdict}** | {result.business_status} "
            f"| {result.quality_status} | {result.harness_status} | {result.outcome} "
            f"| {flaky_cell} | {result.customer_turns} | {result.elapsed_sec} "
            f"| `{run_dir}` |"
        )

    failures = [r for r in results if r.verdict != VERDICT_PASS]
    lines += ["", "## Failures", ""]
    lines += [
        "> `BLOCKED` 은 에이전트 실패가 아니라 **dev 전제 조건 미충족**이다 "
        "(해당 고객에게 CONFIRMED 예약이 없어 변경/취소 대화가 시작되지 않음). "
        "통과로 세지 않으며 `--require-complete`에서는 종료 코드 1이다.",
        "",
    ]
    if not failures:
        lines.append("전부 PASS.")
    for result in failures:
        lines.append(f"### {result.scenario_id} — {result.verdict}")
        lines.append("")
        lines.append(f"- 제목: {result.title}")
        lines.append(
            f"- layers: business=`{result.business_status}`, "
            f"quality=`{result.quality_status}`, harness=`{result.harness_status}`"
        )
        lines.append(f"- outcome: `{result.outcome}` / tools: {result.tools or '-'}")
        if result.handoffs:
            lines.append(f"- handoffs: {result.handoffs}")
        if result.timed_out:
            lines.append("- **타임아웃**으로 종료됨")
        if result.attempts > 1:
            lines.append(
                f"- 시도 {result.attempts}회"
                + (" (flaky: 재시도에서 판정이 바뀜)" if result.flaky else "")
            )
        if result.orphans_killed:
            lines.append(
                f"- 고아 에이전트 프로세스 {len(result.orphans_killed)}개 정리함: "
                f"{result.orphans_killed}"
            )
        if result.api_requests:
            lines.append("- API fixture requests: " + ", ".join(result.api_requests))
        if result.stopped_early:
            lines.append(
                "- 대화 종료 후 콘솔이 살아 있어 러너가 조기 정리함"
                " (타임아웃 아님, 실패 신호 아님)"
            )
        if result.error:
            lines.append(f"- error: {result.error}")
        bad = [check for check in result.checks if not check.passed]
        if bad:
            lines.append("- 실패한 체크:")
            for check in bad:
                lines.append(f"  - `{check.name}`: {check.detail}")
        if result.judge and result.judge.verdict != "PASS":
            lines.append(f"- judge: **{result.judge.verdict}**")
            for issue in result.judge.issues:
                lines.append(f"  - issue: {issue}")
            for reason in result.judge.reasons:
                lines.append(f"  - reason: {reason}")
            if result.judge.verdict == "ERROR" and result.judge.raw:
                lines.append(f"  - raw: `{result.judge.raw[:300]}`")
        if result.verdict in ("FAIL", "ERROR") and result.last_agent_msgs:
            lines.append("- 마지막 에이전트 발화:")
            for message in result.last_agent_msgs:
                snippet = " ".join((message or "").split())[:200]
                lines.append(f"  - {snippet}")
        if result.session_dir:
            lines.append(f"- 세션: `{result.session_dir}` (원본 로그: `agent_raw.log`)")
        lines.append("")

    if diff is not None:
        lines += ["## 회귀 비교", "", f"- 기준: `{compare_path}`"]
        lines.append(f"- newly failing: {len(diff.newly_failing)}")
        for item in diff.newly_failing:
            lines.append(f"  - {item}")
        lines.append(f"- newly passing: {len(diff.newly_passing)}")
        for item in diff.newly_passing:
            lines.append(f"  - {item}")
        lines.append(f"- unchanged pass: {diff.unchanged_pass}")
        lines.append(f"- unchanged fail: {diff.unchanged_fail}")
        if diff.changed_nonpass:
            lines.append("- 비-PASS 판정 변화:")
            for item in diff.changed_nonpass:
                lines.append(f"  - {item}")
        if diff.added:
            lines.append(f"- 새로 추가된 시나리오: {', '.join(diff.added)}")
        if diff.missing:
            lines.append(f"- 이번 실행에 없던 시나리오: {', '.join(diff.missing)}")
        lines.append("")

    harness_warnings = [
        result for result in results if result.harness_status != "CLEAN"
    ]
    if harness_warnings:
        lines += ["## Harness warnings", ""]
        for result in harness_warnings:
            lines.append(
                f"- `{result.scenario_id}`: {result.harness_status} "
                f"(business={result.business_status}, outcome={result.outcome})"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


def write_report_md(content: str, path: Path) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def summarise_counts(results: list[RunResult]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.verdict] = counts.get(result.verdict, 0) + 1
    return counts
