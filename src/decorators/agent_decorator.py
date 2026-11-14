from functools import wraps
from typing import Literal

from loguru import logger

from agents.base_agent import BOOKING_MUTATION_ACTION_KEYS, BaseAgent
from data import (
    BookingAgentActionConfig,
    CallSessionData,
    InfoAgentActionConfig,
    RunContext_T,
)
from tasks.transfer_reason_task import TransferPurpose
from utils.agent_event import agent_event_extra
from utils.langfuse_tags import langfuse_tags

# 액션 모드 타입
ActionMode = Literal["auto", "transfer", "leave_memo"]

# 라우팅 이벤트 계측. auto 가 아닌 모드는 툴 본문 대신 후속 태스크로 넘어가므로
# 업무 시도를 '접수 후 인계' 로 명시적으로 닫는다. status 없이 mode 만 남기면
# 분석기가 시도를 열어만 두고 닫지 못해 본인확인 이탈로 오분류한다(통화 107937).
# mode 는 레거시 분류기(booking_create_incomplete)가 읽으므로 유지한다.
ACTION_ROUTE_STATUS = "EXITED"
ACTION_ROUTE_DETAILS: dict[str, dict[str, str]] = {
    "transfer": {"mode": "TRANSFER", "reason_code": "routed_transfer"},
    "leave_memo": {"mode": "LEAVE_MEMO", "reason_code": "routed_leave_memo"},
}


def get_action_mode(
    call_session_data: CallSessionData,
    agent_name: Literal["booking_agent", "info_agent"],
    action_key: str,
) -> ActionMode:
    """
    에이전트 설정에서 특정 액션의 모드를 가져옵니다.

    Args:
        call_session_data: 현재 세션 데이터
        agent_name: 에이전트 이름 (예: "booking_agent")
        action_key: 액션 키 (예: "booking_create", "booking_modify", "booking_cancel", "booking_inquiry", "introduction", "visit_guide", "advisor_guide", "service_guide", "certificate", "events")

    Returns:
        ActionMode: "auto" | "transfer" | "leave_memo"
    """
    agents_config = call_session_data.call_config_data.agents_config

    if agent_name == "booking_agent":
        agent_config: BookingAgentActionConfig = agents_config.booking_agent
        mode = getattr(agent_config, action_key, "auto")
        return mode
    elif agent_name == "info_agent":
        agent_config: InfoAgentActionConfig = agents_config.info_agent
        mode = getattr(agent_config, action_key, "auto")
        return mode

    # 기본값: auto
    return "auto"


def action_mode_handler(
    action_key: str, agent_name: Literal["booking_agent", "info_agent"]
):
    """
    Tool 함수에 액션 모드 분기 로직을 적용하는 데코레이터.

    설정에 따라 다음 동작을 수행합니다:
    - "auto": 원래 Tool 함수 실행
    - "transfer": 상담원 연결 (_handle_transfer_call)
    - "leave_memo": 상담 요청 남기기 (_handle_leave_message)

    Args:
        action_key: 액션 키 (예: "booking_create", "booking_modify", "booking_cancel", "booking_inquiry", "introduction", "visit_guide", "advisor_guide", "service_guide", "certificate", "events")
        agent_name: 에이전트 이름 (기본값: "booking_agent")

    Usage:
        @function_tool()
        @action_mode_handler("booking_inquiry")
        async def get_my_appointment_list(ctx: RunContext_T):
            ...
    """

    def decorator(func):
        @langfuse_tags(action_key)
        @wraps(func)
        async def wrapper(ctx: RunContext_T, *args, **kwargs):
            call_session_data = ctx.userdata
            mode = get_action_mode(call_session_data, agent_name, action_key)
            # ! ctx.session.current_agent를 통해 BaseAgent 인스턴스에 접근
            current_agent: BaseAgent = ctx.session.current_agent

            logger.bind(
                event="ACTION_MODE_HANDLER",
                action_key=action_key,
                agent_name=agent_name,
                mode=mode,
                room_name=call_session_data.room_name,
            ).debug(f"Action mode check: {action_key} -> {mode}")

            ctx.session.input.set_audio_enabled(True)

            route_details = ACTION_ROUTE_DETAILS.get(mode)
            if route_details is not None:
                ctx.session.history.add_message(
                    role="developer",
                    content="",
                    extra=agent_event_extra(
                        action_key.upper(),
                        status=ACTION_ROUTE_STATUS,
                        details=dict(route_details),
                    ),
                )

            if mode == "transfer":
                return await current_agent._handle_transfer_call(
                    call_session_data,
                    action_key=action_key,
                    transfer_purpose=(
                        TransferPurpose(code=action_key)
                        if action_key in BOOKING_MUTATION_ACTION_KEYS
                        else None
                    ),
                )

            elif mode == "leave_memo":
                return await current_agent._handle_leave_message(
                    call_session_data,
                    is_after_transfer_failure=False,
                    action_key=action_key,
                )

            # "auto" → 원래 함수 실행
            return await func(ctx, *args, **kwargs)

        return wrapper

    return decorator
