"""Bootstrap helpers shared by the inbound agent entrypoints.

Entrypoints call ``configure_process_environment`` before importing modules
that read environment variables at import time (for example
``external_apis.fastapi_app_api`` reads ``FAQ_API_BASEURL``). This module must
therefore stay import-light: runtime dependencies are imported inside the
functions that need them.
"""

import os
import warnings
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from loguru import logger

if TYPE_CHECKING:
    from livekit.agents import Agent

    from data import AgentsData, CallSessionData

_KST = ZoneInfo("Asia/Seoul")


def configure_process_environment(*, dotenv_override: bool) -> None:
    """Silence noisy third-party warnings and load the process environment."""
    # Suppress pydantic warning BEFORE any imports that might trigger it
    warnings.filterwarnings(
        "ignore",
        module="pydantic._migration",
    )

    # Suppress other warnings
    warnings.filterwarnings(
        "ignore", message="Api key is used with an insecure connection"
    )
    warnings.filterwarnings(
        "ignore",
        message=".*Qdrant client version.*is incompatible with server version.*",
    )
    warnings.filterwarnings(
        "ignore", message=".*PyTorch.*TensorFlow.*Flax.*have been found.*"
    )

    # Set environment variable to suppress transformers warning before importing
    os.environ["TRANSFORMERS_NO_ADVISORY_WARNINGS"] = "1"

    load_dotenv(override=dotenv_override)

    # GOOGLE_APPLICATION_CREDENTIALS has no default; credentials must be
    # supplied explicitly.


def kst_now_naive_iso() -> str:
    """Current Asia/Seoul wall-clock time as a naive ISO string (call times)."""
    return datetime.now(_KST).replace(tzinfo=None).isoformat()


async def load_supplement_prompt(call_session_data: "CallSessionData") -> None:
    """고객사 보충 프롬프트를 조회해 통화 세션 설정에 주입한다."""
    from external_apis.fastapi_app_api import get_supplement_prompt

    business_data = call_session_data.business_data
    client_id = business_data.client_id if business_data else None
    room_name = call_session_data.room_name

    if client_id is None:
        logger.bind(
            event="SUPPLEMENT_PROMPT_FETCH_SKIPPED",
            room_name=room_name,
        ).debug("client_id missing; keeping configured supplement prompt")
        return

    try:
        supplement_prompt = await get_supplement_prompt(client_id)
    except Exception as e:
        logger.bind(
            event="SUPPLEMENT_PROMPT_FETCH_FAILED",
            client_id=client_id,
            room_name=room_name,
        ).opt(exception=e).debug(
            "Failed to fetch supplement prompt; keeping configured prompt"
        )
        return

    call_session_data.leave_message_flow_config.additional_instructions = (
        supplement_prompt.prompt_text
    )
    logger.bind(
        event="SUPPLEMENT_PROMPT_FETCHED",
        client_id=client_id,
        site_id=supplement_prompt.site_id,
        room_name=room_name,
    ).debug("Supplement prompt fetched for leave-message flow")


def build_agents_data(*, single_agent: "Agent") -> "AgentsData":
    """Create the session's shared AgentsData around the flow-driving agent.

    The caller creates the agent so that each entrypoint keeps importing
    ``agents.single_agent`` at module load, as before.
    """
    from data import AgentsData

    agents_data = AgentsData()
    agents_data.agents["single_agent"] = single_agent
    return agents_data
