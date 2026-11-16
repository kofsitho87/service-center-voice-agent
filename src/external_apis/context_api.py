import os
from typing import Any

import httpx

from fake_backend.config import FAKE_CONTEXT_BASEURL
from utils.http_client import create_async_http_client

from .api_logging import log_api_exception, log_api_response
from .types.response_types import CallContextResponse

# Served from local fixtures by the in-process fake backend.
FAQ_API_BASEURL = os.getenv("FAQ_API_BASEURL", FAKE_CONTEXT_BASEURL)


async def get_call_context(client_id: int) -> CallContextResponse:
    """client_id로 통화 컨텍스트를 단일 조회합니다."""
    headers = {
        "accept": "application/json",
    }
    params = {
        "client_id": client_id,
    }

    try:
        async with await create_async_http_client(timeout=3.0) as client:
            response = await client.get(
                f"{FAQ_API_BASEURL}/call-context",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
            data = response.json()
            result = CallContextResponse.model_validate(data)
            log_api_response("GET_CALL_CONTEXT", response, data)
            return result
    except httpx.HTTPStatusError as e:
        log_api_response(
            "GET_CALL_CONTEXT",
            e.response,
            _safe_response_json(e.response),
        )
        raise
    except Exception as e:
        log_api_exception("GET_CALL_CONTEXT", e)
        raise


def _safe_response_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return None
