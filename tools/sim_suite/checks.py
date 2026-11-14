"""시나리오 `expect` 블록에 대한 결정론적 체크(레이어 1).

LLM을 쓰지 않고 `events.jsonl`만으로 판정 가능한 항목만 다룬다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .events import ParsedEvents
from .schema import Expect


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


def _fmt(items: list[str], limit: int = 12) -> str:
    if not items:
        return "[]"
    shown = items[:limit]
    suffix = f" (+{len(items) - limit})" if len(items) > limit else ""
    return "[" + ", ".join(shown) + "]" + suffix


def check_session_ended_cleanly(
    parsed: ParsedEvents, *, timed_out: bool
) -> CheckResult:
    """항상 켜져 있는 체크: session_end를 보았고 타임아웃이 아니어야 한다."""
    passed = parsed.session_ended and not timed_out
    if passed:
        detail = "session_end seen"
    elif timed_out:
        detail = "run timed out"
    else:
        detail = "no session_end event in events.jsonl"
    return CheckResult("session_ended_cleanly", passed, detail)


def check_outcome(expect: Expect, outcome: str) -> CheckResult:
    passed = outcome in expect.outcomes
    return CheckResult(
        "outcome",
        passed,
        f"got {outcome!r}, expected one of {_fmt(expect.outcomes)}",
    )


def check_tools_any(expect: Expect, parsed: ParsedEvents) -> CheckResult | None:
    if not expect.tools_any:
        return None
    hit = [name for name in expect.tools_any if name in parsed.tools]
    return CheckResult(
        "tools_any",
        bool(hit),
        f"expected any of {_fmt(expect.tools_any)}; seen {_fmt(parsed.tools)}",
    )


def check_tools_none(expect: Expect, parsed: ParsedEvents) -> CheckResult | None:
    if not expect.tools_none:
        return None
    hit = [name for name in expect.tools_none if name in parsed.tools]
    return CheckResult(
        "tools_none",
        not hit,
        f"forbidden {_fmt(expect.tools_none)}; hit {_fmt(hit)}",
    )


def check_handoffs_any(expect: Expect, parsed: ParsedEvents) -> CheckResult | None:
    if not expect.handoffs_any:
        return None
    hit = [
        marker
        for marker in expect.handoffs_any
        if any(marker in text for text in parsed.handoffs)
    ]
    return CheckResult(
        "handoffs_any",
        bool(hit),
        f"expected any of {_fmt(expect.handoffs_any)}; seen {_fmt(parsed.handoffs)}",
    )


def check_handoffs_none(expect: Expect, parsed: ParsedEvents) -> CheckResult | None:
    if not expect.handoffs_none:
        return None
    hit = [
        marker
        for marker in expect.handoffs_none
        if any(marker in text for text in parsed.handoffs)
    ]
    return CheckResult(
        "handoffs_none",
        not hit,
        f"forbidden {_fmt(expect.handoffs_none)}; hit {_fmt(hit)}",
    )


def check_customer_turns(expect: Expect, parsed: ParsedEvents) -> CheckResult | None:
    if expect.customer_turns_min is None and expect.customer_turns_max is None:
        return None
    turns = parsed.customer_turns
    passed = True
    if expect.customer_turns_min is not None and turns < expect.customer_turns_min:
        passed = False
    if expect.customer_turns_max is not None and turns > expect.customer_turns_max:
        passed = False
    return CheckResult(
        "customer_turns",
        passed,
        f"got {turns}, allowed min={expect.customer_turns_min} "
        f"max={expect.customer_turns_max}",
    )


def check_agent_regex_max(expect: Expect, parsed: ParsedEvents) -> list[CheckResult]:
    results: list[CheckResult] = []
    for rule in expect.agent_regex_max:
        compiled = re.compile(rule.pattern)
        count = sum(len(compiled.findall(msg or "")) for msg in parsed.agent_msgs)
        results.append(
            CheckResult(
                f"agent_regex_max[{rule.pattern}]",
                count <= rule.max,
                f"matched {count} time(s), max {rule.max}",
            )
        )
    return results


def check_agent_regex_any(expect: Expect, parsed: ParsedEvents) -> CheckResult | None:
    if not expect.agent_regex_any:
        return None
    hits = [
        pattern
        for pattern in expect.agent_regex_any
        if any(re.search(pattern, msg or "") for msg in parsed.agent_msgs)
    ]
    return CheckResult(
        "agent_regex_any",
        bool(hits),
        f"expected any of {_fmt(expect.agent_regex_any)}; matched {_fmt(hits)}",
    )


def check_agent_regex_none(expect: Expect, parsed: ParsedEvents) -> list[CheckResult]:
    results: list[CheckResult] = []
    for pattern in expect.agent_regex_none:
        compiled = re.compile(pattern)
        hits = [msg for msg in parsed.agent_msgs if compiled.search(msg or "")]
        sample = hits[0][:80] if hits else ""
        results.append(
            CheckResult(
                f"agent_regex_none[{pattern}]",
                not hits,
                f"matched {len(hits)} message(s)"
                + (f"; first: {sample!r}" if sample else ""),
            )
        )
    return results


def check_api_requests(expect: Expect, requests: list[str]) -> list[CheckResult]:
    results: list[CheckResult] = []
    for pattern in expect.api_requests_any:
        hits = [request for request in requests if re.search(pattern, request)]
        results.append(
            CheckResult(
                f"api_requests_any[{pattern}]",
                bool(hits),
                f"matched {len(hits)} request(s); seen {_fmt(requests)}",
            )
        )
    for pattern in expect.api_requests_none:
        hits = [request for request in requests if re.search(pattern, request)]
        results.append(
            CheckResult(
                f"api_requests_none[{pattern}]",
                not hits,
                f"matched {len(hits)} request(s); seen {_fmt(requests)}",
            )
        )
    return results


def run_checks(
    expect: Expect,
    parsed: ParsedEvents,
    outcome: str,
    *,
    timed_out: bool = False,
    api_requests: list[str] | None = None,
) -> list[CheckResult]:
    """모든 결정론적 체크를 돌려 결과 목록을 반환한다."""
    results: list[CheckResult] = [
        check_session_ended_cleanly(parsed, timed_out=timed_out),
        check_outcome(expect, outcome),
    ]
    optional = [
        check_tools_any(expect, parsed),
        check_tools_none(expect, parsed),
        check_handoffs_any(expect, parsed),
        check_handoffs_none(expect, parsed),
        check_customer_turns(expect, parsed),
        check_agent_regex_any(expect, parsed),
    ]
    results.extend(item for item in optional if item is not None)
    results.extend(check_agent_regex_max(expect, parsed))
    results.extend(check_agent_regex_none(expect, parsed))
    results.extend(check_api_requests(expect, api_requests or []))
    return results


def all_passed(results: list[CheckResult]) -> bool:
    return all(item.passed for item in results)


def failed_checks(results: list[CheckResult]) -> list[CheckResult]:
    return [item for item in results if not item.passed]
