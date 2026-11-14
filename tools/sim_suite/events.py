"""customer-sim 세션의 `events.jsonl` 파서와 outcome 도출 로직.

주의:
- `tool` 이벤트는 `session_end` 뒤에 기록될 수 있다(`end_call_by_user_request`
  관측). 따라서 항상 파일 끝까지 읽는다.
- 깨진 줄(부분 기록, 비-JSON)은 조용히 건너뛰고 개수만 센다.
- **도구 이벤트는 호출(invocation) 시점에 기록된다.** 성공을 뜻하지 않는다.
  `create_new_appointment` / `modify_appointment` / `cancel_appointment` 는
  의도만 확인되면 무조건 한 번 호출되고, 예약이 없거나 액션 모드가 메모로
  라우팅된 경우에도 이벤트가 남는다. 그래서 변경/취소 성공 여부는 에이전트
  발화(i18n 성공 문구)로, 예약 생성 성공 여부는 `commit_booking` 으로 본다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .text_sanitizer import clean_agent_message, normalized_message_key

# 예약 "생성"은 commit_booking 이 실제 확정 단계다.
# create_new_appointment 는 예약 의도가 확인되면 곧바로 호출되는 진입 도구라
# 완료 신호로 쓸 수 없다.
BOOKING_CREATE_TOOLS = ("commit_booking",)
BOOKING_MODIFY_TOOLS = ("modify_appointment",)
BOOKING_CANCEL_TOOLS = ("cancel_appointment",)
MEMO_TOOLS = ("collect_leave_message", "submit_booking_contact")
MEMO_HANDOFF_MARKER = "LeaveMessageTask"
TRANSFER_TOOL = "transfer_call"
END_CALL_TOOLS = ("end_call_by_system", "end_call_by_user_request")

# 완료 판정용 에이전트 발화(i18n booking.modify/cancel.success,
# booking.modify/cancel.no_reservations).
# 에이전트가 통화를 끝내겠다고 말한 뒤에도 콘솔 프로세스가 종료되지 않는
# 경우가 있다. 이 문구가 마지막 이벤트면 대화는 사실상 끝난 것으로 본다.
SESSION_CLOSING_MARKERS = (
    "응답지연으로 인해 통화를 종료합니다",
    "통화를 종료하겠습니다",
)
IDLE_GRACE_SEC = 20.0

MODIFY_SUCCESS_RE = re.compile(r"예약\s*변경이\s*접수")
CANCEL_SUCCESS_RE = re.compile(r"예약\s*취소가\s*접수")
CREATE_SUCCESS_RE = re.compile(r"예약\s*신청이\s*(?:접수|완료)\s*(?:되었|됐)\s*습니다")
NO_RESERVATION_RE = re.compile(r"(변경|취소)할\s*예약내역이\s*없습니다")

AGENT_ROLE = "에이전트"
CUSTOMER_ROLE = "고객"

OUTCOMES = (
    "probe_passed",
    "booking_created",
    "booking_modified",
    "booking_cancelled",
    "no_reservation",
    "memo_left",
    "transferred",
    "info_answered",
    "ended_by_agent",
    "no_outcome",
)


@dataclass
class ParsedEvents:
    """`events.jsonl` 한 세션 분량을 구조화한 결과."""

    path: Path | None = None
    tools: list[str] = field(default_factory=list)
    handoffs: list[str] = field(default_factory=list)
    agent_msgs: list[str] = field(default_factory=list)
    customer_msgs: list[str] = field(default_factory=list)
    transcript: list[tuple[str, str]] = field(default_factory=list)
    session_ended: bool = False
    bad_lines: int = 0
    total_lines: int = 0
    last_event: dict | None = None

    @property
    def customer_turns(self) -> int:
        return len(self.customer_msgs)

    @property
    def has_events(self) -> bool:
        return bool(self.transcript or self.tools or self.handoffs)

    def agent_msgs_after_first_customer(self) -> int:
        """첫 고객 발화 이후에 나온 에이전트 발화 수."""
        seen_customer = False
        count = 0
        for role, _ in self.transcript:
            if role == CUSTOMER_ROLE:
                seen_customer = True
            elif seen_customer:
                count += 1
        return count

    def transcript_text(self) -> str:
        return "\n".join(f"{role}: {content}" for role, content in self.transcript)

    def quality_transcript_text(self) -> str:
        """Return spoken dialogue only, excluding harness closures and duplicates."""
        cleaned: list[tuple[str, str]] = []
        for role, content in self.transcript:
            content = clean_agent_message(content)
            if not content:
                continue
            if role == AGENT_ROLE and content == "응답지연으로 인해 통화를 종료합니다.":
                continue
            if cleaned:
                previous_role, previous_content = cleaned[-1]
                if previous_role == role and normalized_message_key(
                    previous_content
                ) == normalized_message_key(content):
                    continue
            cleaned.append((role, content))
        return "\n".join(f"{role}: {content}" for role, content in cleaned)


def parse_events(path: Path | str | None) -> ParsedEvents:
    """`events.jsonl`을 끝까지 읽어 구조화한다. 파일이 없으면 빈 결과."""
    if path is None:
        return ParsedEvents()
    path = Path(path)
    parsed = ParsedEvents(path=path)
    if not path.exists():
        return parsed

    raw = path.read_text(encoding="utf-8", errors="replace")
    for line in raw.splitlines():
        if not line.strip():
            continue
        parsed.total_lines += 1
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parsed.bad_lines += 1
            continue
        if not isinstance(event, dict):
            parsed.bad_lines += 1
            continue

        event_type = event.get("type")
        if event_type == "tool":
            parsed.tools.append(str(event.get("name", "")))
        elif event_type == "handoff":
            parsed.handoffs.append(str(event.get("text", "")))
        elif event_type == "agent_msg":
            content = clean_agent_message(str(event.get("content", "")))
            if not content:
                parsed.last_event = event
                continue
            parsed.agent_msgs.append(content)
            parsed.transcript.append((AGENT_ROLE, content))
        elif event_type == "customer_msg":
            content = str(event.get("content", ""))
            parsed.customer_msgs.append(content)
            parsed.transcript.append((CUSTOMER_ROLE, content))
        elif event_type == "session_end":
            parsed.session_ended = True
        parsed.last_event = event

    return parsed


def _has_tool(parsed: ParsedEvents, names: tuple[str, ...]) -> bool:
    return any(tool in names for tool in parsed.tools)


def _has_memo(parsed: ParsedEvents) -> bool:
    if _has_tool(parsed, MEMO_TOOLS):
        return True
    return any(MEMO_HANDOFF_MARKER in text for text in parsed.handoffs)


def _agent_says(parsed: ParsedEvents, pattern: re.Pattern[str]) -> bool:
    return any(pattern.search(msg or "") for msg in parsed.agent_msgs)


def derive_outcome(parsed: ParsedEvents) -> str:
    """결정론적 outcome 도출.

    우선순위는 메모/전환을 먼저 본다. NO_SLOT 메모 경로에서는 예약 도구가
    먼저 호출된 뒤 메모로 빠질 수 있어, 예약 도구를 먼저 보면 오분류된다.

    변경/취소는 도구 호출만으로는 성공을 알 수 없어 성공 문구까지 확인한다.
    """
    if _has_memo(parsed):
        return "memo_left"
    if TRANSFER_TOOL in parsed.tools:
        return "transferred"
    if _has_tool(parsed, BOOKING_CANCEL_TOOLS) and _agent_says(
        parsed, CANCEL_SUCCESS_RE
    ):
        return "booking_cancelled"
    if _has_tool(parsed, BOOKING_MODIFY_TOOLS) and _agent_says(
        parsed, MODIFY_SUCCESS_RE
    ):
        return "booking_modified"
    if _has_tool(parsed, BOOKING_CREATE_TOOLS) and _agent_says(
        parsed, CREATE_SUCCESS_RE
    ):
        return "booking_created"
    if _agent_says(parsed, NO_RESERVATION_RE):
        return "no_reservation"
    if _has_tool(parsed, END_CALL_TOOLS):
        return "ended_by_agent"
    if (
        parsed.session_ended
        and parsed.customer_msgs
        and parsed.agent_msgs_after_first_customer() >= 1
    ):
        return "info_answered"
    return "no_outcome"


def session_looks_finished(parsed: ParsedEvents) -> bool:
    """마지막 이벤트가 종료 신호인지 판별한다(순수 함수)."""
    event = parsed.last_event
    if not event:
        return False
    if event.get("type") == "session_end":
        return True
    if event.get("type") == "agent_msg":
        content = str(event.get("content", ""))
        return any(marker in content for marker in SESSION_CLOSING_MARKERS)
    return False


def should_stop_early(
    parsed: ParsedEvents,
    *,
    idle_sec: float,
    grace_sec: float = IDLE_GRACE_SEC,
) -> bool:
    """대화는 끝났는데 콘솔이 안 죽은 상태인지 판별한다(순수 함수).

    종료 신호가 마지막 이벤트이고 `grace_sec` 동안 새 이벤트가 없으면 True.
    """
    return session_looks_finished(parsed) and idle_sec >= grace_sec


def find_session_dir(sim_root: Path) -> Path | None:
    """`--sim-root` 아래에서 가장 최근 세션 디렉터리를 고른다."""
    if not sim_root.exists():
        return None
    sessions = sorted(p for p in sim_root.iterdir() if p.is_dir())
    return sessions[-1] if sessions else None
