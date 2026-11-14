"""SMS/LMS sending through the external booking API (``POST /sms/send``).

The endpoint takes ``multipart/form-data`` with a JSON ``request`` part
(``MessageSendRequest``) and optional ``images`` parts. Hosted images are sent
as ``imageUrls`` inside the JSON ``request`` part. The response body is a
``MessageSendResponse``; ``sendStatus == "SUCCESS"`` marks a sent message.

Sending a message is a side effect of the call flow, so ``send_sms`` never
raises: every failure comes back as ``SmsSendResult(success=False)``.
"""

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from fake_backend.config import FAKE_RESERVATION_BASEURL
from utils.http_client import create_async_http_client

from .api_logging import log_api_exception, log_api_response
from .types.response_types import MessageSendResponse

# The SMS endpoint lives on the same external API server as reservations.
# The fake backend records the message and never delivers it.
RESERVATION_API_BASEURL = os.getenv("RESERVATION_API_BASEURL", FAKE_RESERVATION_BASEURL)
# The OpenAPI spec declares an ``Authorization`` security requirement without a
# scheme. Send the header only when a value is configured.
SMS_API_AUTHORIZATION = os.getenv("SMS_API_AUTHORIZATION", "")

_EVENT = "SEND_SMS"
_SUCCESS_STATUS = "SUCCESS"


@dataclass(frozen=True)
class SmsSendResult:
    success: bool
    message: str | None = None
    message_id: str | None = None
    message_type: str | None = None
    send_status: str | None = None
    result_code: int | None = None


def normalize_phone_number(phone: str | None) -> str:
    """Keep digits only; the API expects a hyphen-free number."""
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def _is_sent(response: MessageSendResponse) -> bool:
    if response.send_status is not None:
        return response.send_status.strip().upper() == _SUCCESS_STATUS
    # ALIGO-style result code: 1 is success, anything else is a failure.
    return response.result_code == 1


def _log_summary(data: Any, success: bool) -> dict[str, Any]:
    """Safe, success-normalized payload for the common API logger.

    ``resultCode`` uses ALIGO semantics (1 = success), which the shared logger
    would read as an HTTP-like code and mark as failed, so pass an explicit
    ``success`` flag and leave out the message ID and phone number.
    """
    summary: dict[str, Any] = {"success": success}
    if isinstance(data, dict):
        for key in ("messageType", "sendStatus", "resultCode", "resultMessage"):
            if data.get(key) is not None:
                summary[key] = data[key]
    return summary


async def send_sms(
    *,
    site_id: str,
    receiver_phone_number: str,
    content: str,
    subject: str | None = None,
    image_urls: Sequence[str] | None = None,
    timeout: float = 5.0,
) -> SmsSendResult:
    """Send one SMS/LMS/MMS message. Never raises."""
    receiver = normalize_phone_number(receiver_phone_number)
    urls = [url.strip() for url in image_urls or () if url and url.strip()]
    request_params = {
        "site_id": site_id,
        "has_subject": bool(subject),
        "image_count": len(urls),
    }

    if not site_id or not receiver or not content.strip():
        return SmsSendResult(
            success=False, message="site_id, receiver and content are required"
        )
    if not RESERVATION_API_BASEURL:
        return SmsSendResult(success=False, message="RESERVATION_API_BASEURL unset")

    payload: dict[str, Any] = {
        "siteId": site_id,
        "receiverPhoneNumber": receiver,
        "content": content,
        # Source contract field; the demo backend never delivers messages.
        "testMode": False,
    }
    if subject:
        payload["subject"] = subject
    if urls:
        payload["imageUrls"] = urls

    headers = {"accept": "*/*"}
    if SMS_API_AUTHORIZATION:
        headers["Authorization"] = SMS_API_AUTHORIZATION

    try:
        async with await create_async_http_client(timeout=timeout) as client:
            response = await client.post(
                f"{RESERVATION_API_BASEURL.rstrip('/')}/sms/send",
                headers=headers,
                # Spring @RequestPart needs the JSON part's own content type.
                files={
                    "request": (
                        None,
                        json.dumps(payload, ensure_ascii=False),
                        "application/json",
                    )
                },
            )
            response.raise_for_status()
            data = response.json()
            result = MessageSendResponse.model_validate(data)
            success = _is_sent(result)
            log_api_response(
                _EVENT,
                response,
                _log_summary(data, success),
                request_params=request_params,
            )
            return SmsSendResult(
                success=success,
                message=result.result_message,
                message_id=result.message_id,
                message_type=result.message_type,
                send_status=result.send_status,
                result_code=result.result_code,
            )
    except httpx.HTTPStatusError as e:
        data = _safe_response_json(e.response)
        log_api_response(
            _EVENT, e.response, _log_summary(data, False), request_params=request_params
        )
        message = data.get("message") if isinstance(data, dict) else None
        return SmsSendResult(
            success=False,
            message=message or f"HTTP {e.response.status_code}",
        )
    except Exception as e:
        log_api_exception(_EVENT, e, request_params=request_params)
        return SmsSendResult(success=False, message=type(e).__name__)


def _safe_response_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return None
