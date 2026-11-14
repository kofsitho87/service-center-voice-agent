from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from loguru import logger

_RESULT_CODE_KEYS = ("resultCode", "responseCode", "code")
_RESULT_MESSAGE_KEYS = ("resultMsg", "message", "detail")
_COLLECTION_KEYS = (
    "items",
    "contents",
    "data",
    "chunks",
    "candidates",
    "schedules",
    "customers",
)


def _first_present(data: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _payload_success(data: Any) -> bool | None:
    if not isinstance(data, Mapping):
        return None

    success = data.get("success")
    if isinstance(success, bool):
        return success

    response_yn = data.get("responseYn")
    if isinstance(response_yn, str):
        return response_yn.upper() == "Y"

    result_code = _first_present(data, _RESULT_CODE_KEYS)
    if isinstance(result_code, int):
        return 200 <= result_code < 300
    if isinstance(result_code, str) and result_code.strip():
        normalized_code = result_code.strip()
        if len(normalized_code) >= 3 and normalized_code[:3].isdigit():
            return normalized_code.startswith("2")

    return None


def _collection_count(data: Any) -> int | None:
    if isinstance(data, list):
        return len(data)
    if not isinstance(data, Mapping):
        return None

    for key in _COLLECTION_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, Mapping):
            nested_count = _collection_count(value)
            if nested_count is not None:
                return nested_count
    return None


def _request_fields(response: httpx.Response) -> dict[str, Any]:
    request = getattr(response, "request", None)
    if request is None:
        return {}

    url = getattr(request, "url", None)
    return {
        "http_method": getattr(request, "method", None),
        "api_host": getattr(url, "host", None),
        "api_path": getattr(url, "path", None),
    }


def response_elapsed_ms(response: httpx.Response) -> int | None:
    """Return HTTPX's request-to-response-close duration in milliseconds."""
    try:
        return round(response.elapsed.total_seconds() * 1000)
    except (AttributeError, RuntimeError):
        return None


def _response_summary(data: Any) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        return {
            "payload_type": type(data).__name__,
            "result_count": _collection_count(data),
        }

    result_message = _first_present(data, _RESULT_MESSAGE_KEYS)
    if result_message is not None:
        result_message = str(result_message)[:500]

    summary = {
        "payload_keys": sorted(str(key) for key in data),
        "result_code": _first_present(data, _RESULT_CODE_KEYS),
        "result_message": result_message,
        "result_count": _collection_count(data),
    }
    if "selected" in data:
        summary["selected"] = data["selected"] is not None
    if "cache_hit" in data:
        summary["cache_hit"] = data["cache_hit"]
    return summary


def log_api_response(
    event: str,
    response: httpx.Response,
    data: Any = None,
    *,
    request_params: Mapping[str, Any] | None = None,
) -> None:
    """Log an HTTP API result with only explicitly supplied safe parameters."""
    http_success = 200 <= response.status_code < 400
    payload_success = _payload_success(data)
    api_success = http_success and payload_success is not False
    stage = "success" if api_success else "failed"

    log_fields = {
        "event": event,
        "stage": stage,
        "http_status_code": response.status_code,
        "http_success": http_success,
        "api_success": api_success,
        "payload_success": payload_success,
        "elapsed_ms": response_elapsed_ms(response),
        "response_summary": _response_summary(data),
        **_request_fields(response),
    }
    if request_params is not None:
        log_fields["params"] = dict(request_params)

    bound = logger.bind(
        **log_fields,
    )
    if api_success:
        bound.debug("External API response received")
    else:
        bound.debug("External API returned a failure response")


def log_api_exception(
    event: str,
    exception: Exception,
    *,
    request_params: Mapping[str, Any] | None = None,
) -> None:
    """Log transport, parsing, and validation failures in the common API format."""
    log_fields = {
        "event": event,
        "stage": "failed",
        "http_status_code": None,
        "http_success": False,
        "api_success": False,
        "exception_type": type(exception).__name__,
    }
    if request_params is not None:
        log_fields["params"] = dict(request_params)
    logger.bind(
        **log_fields,
    ).opt(exception=exception).error("External API request failed")
