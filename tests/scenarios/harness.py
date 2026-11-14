"""Offline scenario harness: real session data, real booking task, fake backend.

Unlike the characterization tests (which fake the schedule API), these
scenarios run ``DynamicBookingTask`` against the real
``booking_api`` client and the in-process fake backend. Only the LiveKit session
object and the task clock are substituted.
"""

from __future__ import annotations

import datetime as dt
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import yaml
from livekit.agents import llm

from data import CallSessionData
from fake_backend import store
from tasks.booking.dynamic_reference import load_booking_reference
from tasks.dynamic_booking_task import DynamicBookingTask

KST = ZoneInfo("Asia/Seoul")
FIXED_NOW = dt.datetime(2026, 11, 16, 9, 30, tzinfo=KST)  # matches tests/conftest.py
DIAL_INFO_DIR = Path(__file__).resolve().parents[2] / "src" / "dial_info"


class History:
    """Records developer events the way ``ChatContext.add_message`` receives them."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def add_message(self, *, role: str, content: Any = "", extra=None, **_: Any):
        self.messages.append({"role": role, "content": content, "extra": extra or {}})

    def events(self, name: str | None = None) -> list[dict[str, Any]]:
        found = [
            m["extra"]["agent_event"]
            for m in self.messages
            if "agent_event" in m["extra"]
        ]
        return [e for e in found if name is None or e["name"] == name]


class FakeSession:
    def __init__(self, agent: Any = None) -> None:
        self.history = History()
        self.said: list[str] = []
        self.audio_enabled: list[bool] = []
        self.current_agent = agent or SimpleNamespace(
            exact_tool_calls=[],
            _request_exact_tool_call=None,
        )
        if self.current_agent._request_exact_tool_call is None:
            self.current_agent._request_exact_tool_call = (
                lambda name, **_: self.current_agent.exact_tool_calls.append(name)
            )
        self.input = SimpleNamespace(set_audio_enabled=self.audio_enabled.append)
        self.shutdown = Mock()

    def say(self, text: str, **_: Any):
        self.said.append(text)
        return SimpleNamespace(wait_for_playout=AsyncMock())


def session_data(
    dial_info: str = "dial_info.voice.ko.yaml",
    *,
    room: str = "scenario-room",
    **recipient: Any,
) -> CallSessionData:
    raw = yaml.safe_load((DIAL_INFO_DIR / dial_info).read_text(encoding="utf-8"))
    raw.setdefault("recipient_data", {}).update(recipient)
    ctx = SimpleNamespace(room=SimpleNamespace(name=room))
    return CallSessionData.from_dial_info(ctx, raw)


def chat(*turns: tuple[str, str]) -> llm.ChatContext:
    ctx = llm.ChatContext()
    for index, (role, text) in enumerate(turns):
        ctx.items.append(llm.ChatMessage(id=f"m{index}", role=role, content=[text]))
    return ctx


async def make_task(
    csd: CallSessionData, *turns: tuple[str, str]
) -> DynamicBookingTask:
    reference = await load_booking_reference(csd)
    csd.dynamic_booking_reference = reference
    task = DynamicBookingTask(
        call_session_data=csd,
        reference=reference,
        chat_ctx=chat(*(turns or (("user", "휴대폰 점검 예약하고 싶어요"),))),
    )
    task.complete = Mock()
    return task


@contextmanager
def booking_env(session: FakeSession):
    with ExitStack() as stack:
        stack.enter_context(patch.object(DynamicBookingTask, "session", session))
        stack.enter_context(
            patch("tasks.dynamic_booking_task.now_kst", return_value=FIXED_NOW)
        )
        yield


def user_says(task: DynamicBookingTask, text: str) -> None:
    items = task._chat_ctx.items
    items.append(llm.ChatMessage(id=f"u{len(items)}", role="user", content=[text]))


async def search(task: DynamicBookingTask, **kwargs: Any) -> dict[str, Any]:
    return await DynamicBookingTask.search_schedules._func(task, **kwargs)


async def stage(task: DynamicBookingTask, **kwargs: Any) -> dict[str, Any]:
    ctx = SimpleNamespace(disallow_interruptions=Mock())
    return await DynamicBookingTask.stage_booking._func(task, ctx, **kwargs)


async def commit(task: DynamicBookingTask, confirmed_datetime: str):
    return await DynamicBookingTask.commit_booking._func(task, confirmed_datetime)


async def reject(task: DynamicBookingTask, scope: str = "slot") -> dict[str, Any]:
    return await DynamicBookingTask.reject_candidate._func(task, scope=scope)


def completed_result(task: DynamicBookingTask):
    """The ``DynamicBookingResult`` passed to ``task.complete`` (or None)."""
    if not task.complete.called:
        return None
    return task.complete.call_args.args[0]


def confirmed_rows(**where: Any) -> list[dict[str, Any]]:
    clauses = ["state = 'CONFIRMED'"] + [f"{key} = ?" for key in where]
    with store.connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM appointments WHERE {' AND '.join(clauses)}",
            tuple(where.values()),
        ).fetchall()
    return [dict(row) for row in rows]


def agent_rows() -> list[dict[str, Any]]:
    with store.connection() as conn:
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM appointments WHERE source = 'agent'")
        ]


def tool_ctx(csd: CallSessionData, session: FakeSession) -> SimpleNamespace:
    return SimpleNamespace(
        userdata=csd,
        session=session,
        disallow_interruptions=Mock(),
        wait_for_playout=AsyncMock(),
    )


def current_agent() -> SimpleNamespace:
    """Parent-agent surface the booking tools touch (SingleAgent stand-in)."""
    agent = SimpleNamespace(
        _center_context="",
        chat_ctx=chat(("user", "휴대폰 점검 예약하고 싶어요")),
        exact_tool_calls=[],
        booking_exits=[],
    )
    agent._request_exact_tool_call = lambda name, **_: agent.exact_tool_calls.append(
        name
    )

    async def handle_booking_exit(ctx, **kwargs):
        agent.booking_exits.append(kwargs)

    agent.handle_booking_exit = handle_booking_exit
    return agent


def unwrap_tool(tool: Any):
    body = tool._func
    while hasattr(body, "__wrapped__"):
        body = body.__wrapped__
    return body


async def run_create_tool(
    csd: CallSessionData, session: FakeSession, result: Any
) -> None:
    """Run the real parent ``create_new_appointment`` with a finished task result.

    ``DynamicBookingTask`` is an AgentTask awaited inside a live session; here
    the already-run task's result is returned in its place, so identity,
    reference, merge, the reservation write hook, and the announcement all run for real.
    """
    from tools import dynamic_booking_tools

    async def finished(**_: Any):
        return result

    with patch.object(
        dynamic_booking_tools,
        "DynamicBookingTask",
        side_effect=lambda **kw: finished(**kw),
    ):
        await unwrap_tool(dynamic_booking_tools.create_new_appointment)(
            tool_ctx(csd, session)
        )
