"""Bootstrap helpers shared by the inbound agent entrypoints.

Entrypoints call ``configure_process_environment`` before importing modules
that read environment variables at import time (for example
``external_apis.context_api`` reads ``FAQ_API_BASEURL``). This module must
therefore stay import-light: runtime dependencies are imported inside the
functions that need them.
"""

import os
import warnings
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

if TYPE_CHECKING:
    from livekit.agents import Agent

    from data import AgentsData

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


def build_agents_data(*, single_agent: "Agent") -> "AgentsData":
    """Create the session's shared AgentsData around the flow-driving agent.

    The caller creates the agent so that each entrypoint keeps importing
    ``agents.single_agent`` at module load, as before.
    """
    from data import AgentsData

    agents_data = AgentsData()
    agents_data.agents["single_agent"] = single_agent
    return agents_data
