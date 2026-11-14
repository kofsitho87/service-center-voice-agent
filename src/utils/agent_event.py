from typing import Any

from loguru import logger


def agent_event_extra(
    name: str,
    *,
    status: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build structured metadata for a developer-role agent event."""
    agent_event = {"name": name, "details": details or {}}
    if status is not None:
        agent_event["status"] = status
    return {"agent_event": agent_event}


def identity_verify_extra(
    *,
    action: str,
    status: str,
    source: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Build the IDENTITY_VERIFY event for post-call funnel analysis.

    Customer identification gates every booking intent, but a successful
    verification used to leave no trace at all — only failures surfaced, via the
    caller's ESCALATED event. That made the "identity resolved" funnel step
    impossible to count, so its drop-off could not be measured.

    Emitted by the booking tools (which hold the session) rather than by
    VerifyCustomerIdentityTask, which has no session reference.

    Args:
        action: booking intent this verification serves (create/inquiry/...).
        status: SUCCESS (identity settled) / NO_CUSTOMER / FAILED.
        source: how it was settled — "verified" (task matched) or
            "known" (verification_code already on file, verification skipped).
        reason: escalation reason code when status is FAILED.
    """
    details: dict[str, Any] = {"action": action}
    if source is not None:
        details["source"] = source
    if reason is not None:
        details["reason"] = reason
    return agent_event_extra("IDENTITY_VERIFY", status=status, details=details)


def _api_result_count(response: Any) -> int | None:
    """Extract a safe result count without serializing the response payload."""
    data = getattr(response, "data", None)
    if isinstance(data, list):
        return len(data)

    for source in (data, response):
        if source is None:
            continue
        for field in (
            "items",
            "services",
            "schedules",
            "availableTimes",
            "customers",
            "candidates",
        ):
            value = getattr(source, field, None)
            if isinstance(value, list):
                return len(value)
    return None


def record_api_result(
    history: Any,
    api: str,
    response: Any = None,
    *,
    success: bool | None = None,
    result_count: int | None = None,
    message: str | None = None,
    elapsed_ms: int | None = None,
    params: dict[str, Any] | None = None,
) -> None:
    """Append a privacy-safe API result as a developer-role agent event.

    Callers may include only request parameters that are safe to persist in the
    conversation history.
    """
    if history is None:
        return

    if success is None:
        success = bool(getattr(response, "success", True))
    if result_count is None:
        result_count = _api_result_count(response)
    if message is None:
        message = getattr(response, "message", None) or getattr(
            response,
            "resultMsg",
            None,
        )
    if params is None:
        response_params = getattr(response, "request_params", None)
        if isinstance(response_params, dict):
            params = response_params

    details: dict[str, Any] = {"api": api}
    if result_count is not None:
        details["result_count"] = result_count
    if elapsed_ms is not None:
        details["elapsed_ms"] = elapsed_ms
    if params is not None:
        details["params"] = dict(params)
    if not success and message:
        details["message"] = str(message)[:500]

    try:
        history.add_message(
            role="developer",
            content="",
            extra=agent_event_extra(
                "API_RESULT",
                status="SUCCESS" if success else "FAILED",
                details=details,
            ),
        )
    except Exception as error:
        logger.bind(event="API_RESULT", api=api).opt(exception=error).warning(
            "Failed to record API result in conversation history"
        )


def transfer_queued_extra(
    *,
    transfer_type: str,
    queue_length: int | None = None,
) -> dict[str, Any]:
    """Build the warm-transfer QUEUED event for post-call funnel analysis.

    Entering the queue used to leave no trace, so a caller who waited and hung
    up looked identical to one who never got that far. The two failures have
    opposite fixes — staffing versus wait-time messaging.

    The event name mirrors the variant used by STARTED / SUCCESS so the analyzer
    folds all three into one transfer attempt. Cold transfers dial straight out
    and never queue, so only the WARM variants reach here.

    Args:
        transfer_type: "WARM" or "DIRECT_WARM".
        queue_length: callers waiting at enqueue time. None when the lookup
            failed — omitted rather than zeroed, since 0 means "no wait".
    """
    details: dict[str, Any] = {}
    if queue_length is not None:
        details["queue_length"] = queue_length
    return agent_event_extra(
        f"{transfer_type}_TRANSFER_CALL",
        status="QUEUED",
        details=details,
    )
