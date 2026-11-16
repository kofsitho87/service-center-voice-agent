"""액션 모드 라우팅 이벤트 계측 — 상태와 사유를 명시한다.

leave_memo / transfer 로 설정된 센터는 예약 툴 본문을 건너뛰고 후속 태스크로
넘어간다. 이때 남기는 `BOOKING_CREATE` 이벤트에 status 가 없으면 분석기가
예약 시도를 열어만 두고 닫지 못해 '본인확인 이탈' 로 오분류한다.
런타임은 EXITED + reason_code 로 라우팅을 명시하고, mode 는 레거시 분류기의
booking_create_incomplete 계산을 위해 유지한다.
"""

from types import SimpleNamespace

from decorators.agent_decorator import action_mode_handler
from tasks.transfer_reason_task import TransferPurpose


class _FakeAgent:
    def __init__(self, order: list[str]):
        self.calls: list[tuple] = []
        self._order = order

    async def _handle_leave_message(
        self, call_session_data, *, is_after_transfer_failure, action_key
    ):
        self._order.append("handler")
        self.calls.append(("leave_message", action_key, is_after_transfer_failure))
        return "memo"

    async def _handle_transfer_call(
        self, call_session_data, *, action_key, transfer_purpose
    ):
        self._order.append("handler")
        self.calls.append(("transfer", action_key, transfer_purpose))
        return "transfer"


def _ctx(mode: str, action_key: str = "booking_create"):
    # 이벤트 기록과 handler 호출의 순서를 한 로그에 남긴다. 이벤트가 handler
    # 뒤에 기록되면 후속 태스크의 이벤트보다 늦게 나와 분석기가 시도 경계를
    # 잘못 자른다.
    order: list[str] = []
    agent = _FakeAgent(order)
    history: list[dict] = []

    def add_message(*, role, content, extra):
        order.append("event")
        history.append({"role": role, "content": content, "extra": extra})

    booking_agent = SimpleNamespace(**{action_key: mode})
    userdata = SimpleNamespace(
        room_name="room-test",
        call_config_data=SimpleNamespace(
            agents_config=SimpleNamespace(booking_agent=booking_agent)
        ),
    )
    session = SimpleNamespace(
        current_agent=agent,
        history=SimpleNamespace(add_message=add_message),
        input=SimpleNamespace(set_audio_enabled=lambda enabled: None),
    )
    ctx = SimpleNamespace(userdata=userdata, session=session)
    ctx.order = order
    return ctx, agent, history


async def _run(mode: str, action_key: str = "booking_create"):
    ctx, agent, history = _ctx(mode, action_key)
    body_calls = []

    @action_mode_handler(action_key=action_key, agent_name="booking_agent")
    async def tool(ctx):
        body_calls.append(True)
        return "auto"

    result = await tool(ctx)
    return result, agent, history, body_calls, ctx.order


async def test_leave_memo_route_emits_exited_with_reason_and_mode():
    result, agent, history, body_calls, order = await _run("leave_memo")

    assert result == "memo"
    assert body_calls == []
    assert agent.calls == [("leave_message", "booking_create", False)]
    assert order == ["event", "handler"]
    assert history == [
        {
            "role": "developer",
            "content": "",
            "extra": {
                "agent_event": {
                    "name": "BOOKING_CREATE",
                    "status": "EXITED",
                    "details": {
                        "mode": "LEAVE_MEMO",
                        "reason_code": "routed_leave_memo",
                    },
                }
            },
        }
    ]


async def test_transfer_route_emits_exited_with_reason_and_mode():
    result, agent, history, body_calls, order = await _run("transfer", "booking_cancel")

    assert result == "transfer"
    assert body_calls == []
    assert agent.calls == [
        ("transfer", "booking_cancel", TransferPurpose(code="booking_cancel"))
    ]
    assert order == ["event", "handler"]
    assert history[0]["extra"]["agent_event"] == {
        "name": "BOOKING_CANCEL",
        "status": "EXITED",
        "details": {"mode": "TRANSFER", "reason_code": "routed_transfer"},
    }


async def test_auto_mode_runs_tool_body_without_routing_event():
    result, agent, history, body_calls, order = await _run("auto")

    assert result == "auto"
    assert body_calls == [True]
    assert agent.calls == []
    assert history == []
    assert order == []
