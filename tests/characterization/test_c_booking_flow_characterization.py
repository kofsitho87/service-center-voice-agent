"""Characterization of the booking tool bodies' observable timeline.

Each scenario drives a real tool body (decorators unwrapped) against a fake
session and records, in order, every developer event, spoken line, audio
toggle, task construction, router exit, transfer and shutdown. The recorded
timelines are pinned in SNAPSHOTS so refactors cannot reorder or drop them.
"""

from __future__ import annotations

import dataclasses
import importlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from livekit.agents import ToolError, llm

from data import (
    BusinessData,
    CallConfigData,
    CallSessionData,
    LastReservation,
    RecipientData,
)
from tasks.booking.get_my_reservations_task import GetMyReservationsResult
from tasks.booking.select_reservation_task import SelectReservationResult
from tasks.dtmf_verification_code_task import GetDtmfResult
from tasks.schedule_selection_base import ScheduleSelectionResult
from tasks.verify_customer_identity_task import VerifyCustomerResult
from tools import booking_tools, dynamic_booking_tools
from tools.reservation_submit import SubmitOutcome

_TOOL_MODULE_NAMES = (
    "tools.booking_tools",
    "tools.dynamic_booking_tools",
    "tools.booking_common",
)


def _tool_modules() -> list[Any]:
    modules = []
    for name in _TOOL_MODULE_NAMES:
        try:
            modules.append(importlib.import_module(name))
        except ModuleNotFoundError:
            continue
    return modules


def _patch_everywhere(monkeypatch, name: str, value: Any) -> None:
    """Patch a collaborator in whichever tool module(s) reference it."""
    patched = False
    for module in _tool_modules():
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
            patched = True
    assert patched, name


def _unwrap(tool) -> Any:
    body = tool._func
    while hasattr(body, "__wrapped__"):
        body = body.__wrapped__
    return body


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


class Harness:
    def __init__(self, monkeypatch, userdata: CallSessionData) -> None:
        self.monkeypatch = monkeypatch
        self.userdata = userdata
        self.log: list[tuple] = []
        log = self.log

        def add_message(**kwargs):
            log.append(("event", _plain(kwargs.get("extra"))))

        def say(text, **kwargs):
            log.append(("say", text, kwargs.get("allow_interruptions")))

            async def wait_for_playout():
                log.append(("playout",))

            return SimpleNamespace(wait_for_playout=wait_for_playout)

        async def handle_booking_exit(ctx, **kwargs):
            log.append(("route", _plain(kwargs)))

        async def handle_transfer_call(call_session_data, **kwargs):
            assert call_session_data is userdata
            log.append(("transfer", _plain(kwargs)))

        async def ctx_wait_for_playout():
            log.append(("ctx_playout",))

        self.session = SimpleNamespace(
            history=SimpleNamespace(add_message=add_message),
            say=say,
            input=SimpleNamespace(
                set_audio_enabled=lambda enabled: log.append(("audio", enabled))
            ),
            current_agent=SimpleNamespace(
                chat_ctx=llm.ChatContext(),
                handle_booking_exit=handle_booking_exit,
                _handle_transfer_call=handle_transfer_call,
                _center_context="",
            ),
            shutdown=lambda **kwargs: log.append(("shutdown", kwargs)),
            generate_reply=lambda **kwargs: log.append(("generate_reply",)),
        )
        self.ctx = SimpleNamespace(
            session=self.session,
            userdata=userdata,
            disallow_interruptions=Mock(),
            wait_for_playout=ctx_wait_for_playout,
            speech_handle=SimpleNamespace(interrupted=False),
        )

        # Create/modify/cancel submit ``reservation_info`` to the fake
        # backend before the success line. Stub it as ACCEPTED and record the
        # call so the timeline stays pinned around the hook.
        async def submit_current_reservation(ctx, action):
            log.append(("submit", action))
            return SubmitOutcome(status="ACCEPTED")

        _patch_everywhere(
            monkeypatch, "submit_current_reservation", submit_current_reservation
        )

    def agent_task(self, name: str, result: Any) -> None:
        """Replace an awaited AgentTask class with one returning `result`."""
        log = self.log

        async def fake(*args, **kwargs):
            log.append(("task", name, _task_kwargs(kwargs)))
            return result

        _patch_everywhere(self.monkeypatch, name, fake)

    def agent_task_sequence(self, name: str, results: list[Any]) -> None:
        log = self.log
        remaining = list(results)

        async def fake(*args, **kwargs):
            log.append(("task", name, _task_kwargs(kwargs)))
            return remaining.pop(0)

        _patch_everywhere(self.monkeypatch, name, fake)

    def run_task(self, name: str, result: Any) -> None:
        """Replace a `.run()` style task class with one returning `result`."""
        log = self.log

        def factory(*args, **kwargs):
            log.append(("task", name, _task_kwargs(kwargs)))

            async def run():
                if callable(result):
                    return result()
                return result

            return SimpleNamespace(run=run)

        _patch_everywhere(self.monkeypatch, name, factory)

    def api(self, name: str, response: Any) -> None:
        log = self.log

        async def fake(*args, **kwargs):
            log.append(("api", name))
            return response

        _patch_everywhere(self.monkeypatch, name, fake)


_SKIPPED_TASK_KWARGS = {"chat_ctx", "history", "reservation_data"}


def _task_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    plain = {}
    for key, value in kwargs.items():
        if key == "chat_ctx":
            plain["chat_ctx"] = None if value is None else "ChatContext"
        elif key in _SKIPPED_TASK_KWARGS or key in {"call_session_data", "reference"}:
            continue
        else:
            plain[key] = _plain(value)
    return plain


def _userdata(
    *,
    verification_code: str | None = "1990-01-01",
    client_id: int = 1,
    select_operator: bool = False,
    last_reservation: LastReservation | None = None,
) -> CallSessionData:
    data = CallSessionData(
        ctx=None,
        room_name="c-characterization",
        business_data=BusinessData(site_id="site-1", client_id=client_id),
        recipient_data=RecipientData(
            user_name="홍길동",
            phone="01000000101",
            verification_code=verification_code,
            customer_no="C-1",
            customer_visit="REVISIT",
            last_reservation=last_reservation,
        ),
        call_config_data=CallConfigData(language="ko-KR"),
    )
    data.workflow_config.booking.booking_create.select_operator = select_operator
    return data


_SERVICES = [
    {"service_id": 11, "service_name": "휴대폰 점검"},
    {"service_id": 12, "service_name": "노트북 상담"},
]
_SLOT = ScheduleSelectionResult(
    appointment_datetime="2030-01-07 15:00:00",
    advisor_id=77,
)
_RESERVATION = {
    "reservation_id": "R-1",
    "appointment_date": "2030-01-09 10:00:00",
    "service_name": "휴대폰 점검",
    "service_id": 11,
    "operator_id": 77,
    "customer_no": "C-1",
}


def _exit(cls, code, route_target=None, reason="사유"):
    return cls(
        exited=True,
        exit_reason=reason,
        exit_reason_code=code,
        route_target=route_target,
    )


# ── scenario setups ──────────────────────────────────────────────────────


def _new_customer(h: Harness, *, rejections: int = 1) -> None:
    h.run_task("VerifyCustomerIdentityTask", VerifyCustomerResult(status="no_customer"))
    results = [GetDtmfResult(user_input="")]
    results += [
        GetDtmfResult(user_input="900101", normalized_date="1990-01-01")
        for _ in range(rejections)
    ]
    results.append(
        GetDtmfResult(user_input="900101", normalized_date="1990-01-01", confirmed=True)
    )
    h.agent_task_sequence("GetVerificationCodeDtmfTask", results)


def _matched(h: Harness) -> None:
    def matched():
        h.userdata.recipient_data.verification_code = "19850505"
        h.userdata.recipient_data.phone = "01000000104"
        h.userdata.recipient_data.user_name = "김철수"
        h.userdata.recipient_data.customer_no = "C-9"
        return VerifyCustomerResult(status="matched")

    h.run_task("VerifyCustomerIdentityTask", matched)


def _reference_fails(h: Harness) -> None:
    async def fail(*args, **kwargs):
        h.log.append(("api", "load_booking_reference"))
        raise RuntimeError("reference down")

    h.monkeypatch.setattr(dynamic_booking_tools, "load_booking_reference", fail)


async def _call(h: Harness, tool) -> Any:
    try:
        result = await _unwrap(tool)(h.ctx)
    except ToolError as error:
        h.log.append(("tool_error", str(error)))
        return "ToolError"
    h.log.append(("return", result))
    return result


def _dynamic(h):
    return _call(h, dynamic_booking_tools.create_new_appointment)


# Scenario name -> (userdata kwargs, setup(h), runner)
SCENARIOS: dict[str, tuple[dict[str, Any], Any, Any]] = {}


def scenario(name, runner, **userdata_kwargs):
    def register(setup):
        SCENARIOS[name] = (userdata_kwargs, setup, runner)
        return setup

    return register


for _flow, _runner, _stop in (("dynamic", _dynamic, _reference_fails),):

    def _make(runner=_runner, stop=_stop, flow=_flow):
        @scenario(f"{flow}_new_customer_after_retry", runner, verification_code=None)
        def _(h, stop=stop):
            _new_customer(h, rejections=1)
            stop(h)

        @scenario(f"{flow}_new_customer_rejected", runner, verification_code=None)
        def _(h):
            h.run_task(
                "VerifyCustomerIdentityTask", VerifyCustomerResult(status="no_customer")
            )
            h.agent_task_sequence(
                "GetVerificationCodeDtmfTask",
                [
                    GetDtmfResult(user_input="900101", normalized_date="1990-01-01")
                    for _ in range(3)
                ],
            )

        @scenario(f"{flow}_identity_escalated", runner, verification_code=None)
        def _(h):
            h.run_task(
                "VerifyCustomerIdentityTask",
                VerifyCustomerResult(status="escalated", escalation_reason="no_match"),
            )

        @scenario(f"{flow}_identity_matched", runner, verification_code=None)
        def _(h, stop=stop):
            _matched(h)
            stop(h)

        @scenario(f"{flow}_identity_known", runner)
        def _(h, stop=stop):
            stop(h)

    _make()


# ── modify / cancel ──────────────────────────────────────────────────────


def _reservations(h, reservations=(_RESERVATION,)):
    h.run_task(
        "GetMyReservationsTask",
        GetMyReservationsResult(status="completed", reservations=list(reservations)),
    )


def _modify(h):
    return _call(h, booking_tools.modify_appointment)


def _cancel(h):
    return _call(h, booking_tools.cancel_appointment)


for _flow, _runner in (("modify", _modify), ("cancel", _cancel)):

    @scenario(f"{_flow}_reservation_exit", _runner)
    def _(h):
        _reservations(h)
        h.agent_task(
            "SelectReservationTask",
            _exit(SelectReservationResult, "unrelated", "booking_inquiry"),
        )

    @scenario(f"{_flow}_success", _runner)
    def _(h):
        _reservations(h)
        h.agent_task(
            "SelectReservationTask",
            SelectReservationResult(selected_reservation=dict(_RESERVATION)),
        )
        h.agent_task("OperatorScheduleSelectionTask", _SLOT)

    @scenario(f"{_flow}_operator_missing", _runner)
    def _(h):
        _reservations(h)
        reservation = dict(_RESERVATION, operator_id=None)
        h.agent_task(
            "SelectReservationTask",
            SelectReservationResult(selected_reservation=reservation),
        )


@scenario("modify_schedule_exit", _modify)
def _(h):
    _reservations(h)
    h.agent_task(
        "SelectReservationTask",
        SelectReservationResult(selected_reservation=dict(_RESERVATION)),
    )
    h.agent_task(
        "OperatorScheduleSelectionTask",
        _exit(ScheduleSelectionResult, "staff_connection"),
    )


for _label, _message in (
    ("operator_not_found", "해당 상담사가 존재하지 않습니다."),
    ("other", "timeout"),
):

    @scenario(f"modify_schedule_lookup_failed_{_label}", _modify)
    def _(h, message=_message):
        _reservations(h)
        h.agent_task(
            "SelectReservationTask",
            SelectReservationResult(selected_reservation=dict(_RESERVATION)),
        )
        h.agent_task(
            "OperatorScheduleSelectionTask",
            ScheduleSelectionResult(lookup_failed=True, lookup_failure_message=message),
        )


async def run_scenario(monkeypatch, name: str) -> list[tuple]:
    userdata_kwargs, setup, runner = SCENARIOS[name]
    h = Harness(monkeypatch, _userdata(**userdata_kwargs))
    setup(h)
    await runner(h)
    progress = h.userdata.booking_create_progress
    h.log.append(
        (
            "final",
            {
                "progress": _plain(progress),
                "reservation_info": _plain(h.userdata.reservation_info),
                "reservation_data": _plain(h.userdata.reservation_data),
                "recipient_verification_code": h.userdata.recipient_data.verification_code,
            },
        )
    )
    return h.log


SNAPSHOTS: dict[str, list[tuple]] = {
    "cancel_operator_missing": [
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_CANCEL",
                    "details": {},
                    "status": "STARTED",
                }
            },
        ),
        ("task", "GetMyReservationsTask", {"action": "cancel"}),
        (
            "event",
            {
                "agent_event": {
                    "name": "IDENTITY_VERIFY",
                    "details": {"action": "cancel", "source": "verified"},
                    "status": "SUCCESS",
                }
            },
        ),
        (
            "task",
            "SelectReservationTask",
            {
                "reservations": [
                    {
                        "reservation_id": "R-1",
                        "appointment_date": "2030-01-09 10:00:00",
                        "service_name": "휴대폰 점검",
                        "service_id": 11,
                        "operator_id": 77,
                        "customer_no": "C-1",
                    }
                ],
                "action": "취소",
                "language_code": "ko-KR",
            },
        ),
        ("ctx_playout",),
        ("audio", False),
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_CANCEL",
                    "details": {"reason": "reservation_operator_missing"},
                    "status": "FAILED",
                }
            },
        ),
        (
            "say",
            "해당 예약의 담당 상담사 정보를 확인할 수 없어 이 전화로는 취소가 어렵습니다. 상담원이 확인할 수 있도록 메모를 남겨드리겠습니다.",
            False,
        ),
        ("playout",),
        (
            "transfer",
            {
                "action_key": "booking_cancel",
                "transfer_purpose": {
                    "code": "reservation_operator_missing",
                    "detail": "1월 9일 오전 10시 예약 취소 요청, 상담사 정보 없음",
                },
            },
        ),
        ("return", None),
        (
            "final",
            {
                "progress": {
                    "service_id": None,
                    "service_name": None,
                    "operator_id": None,
                    "operator_name": None,
                    "restart_hint": None,
                },
                "reservation_info": {},
                "reservation_data": {},
                "recipient_verification_code": "1990-01-01",
            },
        ),
    ],
    "cancel_reservation_exit": [
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_CANCEL",
                    "details": {},
                    "status": "STARTED",
                }
            },
        ),
        ("task", "GetMyReservationsTask", {"action": "cancel"}),
        (
            "event",
            {
                "agent_event": {
                    "name": "IDENTITY_VERIFY",
                    "details": {"action": "cancel", "source": "verified"},
                    "status": "SUCCESS",
                }
            },
        ),
        (
            "task",
            "SelectReservationTask",
            {
                "reservations": [
                    {
                        "reservation_id": "R-1",
                        "appointment_date": "2030-01-09 10:00:00",
                        "service_name": "휴대폰 점검",
                        "service_id": 11,
                        "operator_id": 77,
                        "customer_no": "C-1",
                    }
                ],
                "action": "취소",
                "language_code": "ko-KR",
            },
        ),
        ("ctx_playout",),
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_CANCEL",
                    "details": {
                        "reason": "사유",
                        "reason_code": "unrelated",
                        "route_target": "booking_inquiry",
                    },
                    "status": "EXITED",
                }
            },
        ),
        (
            "route",
            {
                "source_action": "booking_cancel",
                "reason_code": "unrelated",
                "detail": "사유",
                "route_target": "booking_inquiry",
                "handoff_accepted": None,
                "handoff_channel": None,
                "stopped_before_selection": False,
            },
        ),
        ("return", None),
        (
            "final",
            {
                "progress": {
                    "service_id": None,
                    "service_name": None,
                    "operator_id": None,
                    "operator_name": None,
                    "restart_hint": None,
                },
                "reservation_info": {},
                "reservation_data": {},
                "recipient_verification_code": "1990-01-01",
            },
        ),
    ],
    "cancel_success": [
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_CANCEL",
                    "details": {},
                    "status": "STARTED",
                }
            },
        ),
        ("task", "GetMyReservationsTask", {"action": "cancel"}),
        (
            "event",
            {
                "agent_event": {
                    "name": "IDENTITY_VERIFY",
                    "details": {"action": "cancel", "source": "verified"},
                    "status": "SUCCESS",
                }
            },
        ),
        (
            "task",
            "SelectReservationTask",
            {
                "reservations": [
                    {
                        "reservation_id": "R-1",
                        "appointment_date": "2030-01-09 10:00:00",
                        "service_name": "휴대폰 점검",
                        "service_id": 11,
                        "operator_id": 77,
                        "customer_no": "C-1",
                    }
                ],
                "action": "취소",
                "language_code": "ko-KR",
            },
        ),
        ("ctx_playout",),
        ("audio", False),
        ("submit", "booking_cancel"),
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_CANCEL",
                    "details": {
                        "reservation_record_id": "R-1",
                        "previous_reservation_date_time": "2030-01-09 10:00:00",
                    },
                    "status": "SUCCESS",
                }
            },
        ),
        (
            "say",
            "예약 취소가 접수되었습니다. 센터에서 확인한 뒤 최종 안내를 드릴게요.\n통화를 종료하겠습니다. 좋은 하루 되세요.",
            False,
        ),
        ("ctx_playout",),
        ("shutdown", {"drain": True}),
        ("return", None),
        (
            "final",
            {
                "progress": {
                    "service_id": None,
                    "service_name": None,
                    "operator_id": None,
                    "operator_name": None,
                    "restart_hint": None,
                },
                "reservation_info": {
                    "type": "CANCEL_RESERVATION",
                    "verification_code": "1990-01-01",
                    "reservation_record_id": "R-1",
                    "previous_reservation_date_time": "2030-01-09 10:00:00",
                    "operator_id": 77,
                    "service_id": 11,
                },
                "reservation_data": {},
                "recipient_verification_code": "1990-01-01",
            },
        ),
    ],
    "dynamic_identity_escalated": [
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_CREATE",
                    "details": {},
                    "status": "STARTED",
                }
            },
        ),
        (
            "task",
            "VerifyCustomerIdentityTask",
            {"action": "create", "verify_single_customer_identity": False},
        ),
        (
            "event",
            {
                "agent_event": {
                    "name": "IDENTITY_VERIFY",
                    "details": {"action": "create", "reason": "no_match"},
                    "status": "FAILED",
                }
            },
        ),
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_CREATE",
                    "details": {"reason": "no_match"},
                    "status": "ESCALATED",
                }
            },
        ),
        (
            "say",
            "일치하는 고객 정보가 없어 상담 직원과 확인이 필요합니다. 잠시만 기다려주세요.",
            False,
        ),
        ("playout",),
        (
            "transfer",
            {
                "action_key": "booking_create",
                "transfer_purpose": {
                    "code": "identity_verification_failed",
                    "detail": None,
                },
            },
        ),
        ("return", None),
        (
            "final",
            {
                "progress": {
                    "service_id": None,
                    "service_name": None,
                    "operator_id": None,
                    "operator_name": None,
                    "restart_hint": None,
                },
                "reservation_info": {},
                "reservation_data": {},
                "recipient_verification_code": None,
            },
        ),
    ],
    "dynamic_identity_known": [
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_CREATE",
                    "details": {},
                    "status": "STARTED",
                }
            },
        ),
        (
            "event",
            {
                "agent_event": {
                    "name": "IDENTITY_VERIFY",
                    "details": {"action": "create", "source": "known"},
                    "status": "SUCCESS",
                }
            },
        ),
        ("api", "load_booking_reference"),
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_CREATE",
                    "details": {"reason": "reference_load_failed"},
                    "status": "ERROR",
                }
            },
        ),
        ("tool_error", "지금은 예약 접수가 어렵습니다. 잠시 후 다시 시도해 주세요."),
        (
            "final",
            {
                "progress": {
                    "service_id": None,
                    "service_name": None,
                    "operator_id": None,
                    "operator_name": None,
                    "restart_hint": None,
                },
                "reservation_info": {},
                "reservation_data": {},
                "recipient_verification_code": "1990-01-01",
            },
        ),
    ],
    "dynamic_identity_matched": [
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_CREATE",
                    "details": {},
                    "status": "STARTED",
                }
            },
        ),
        (
            "task",
            "VerifyCustomerIdentityTask",
            {"action": "create", "verify_single_customer_identity": False},
        ),
        (
            "event",
            {
                "agent_event": {
                    "name": "IDENTITY_VERIFY",
                    "details": {"action": "create", "source": "verified"},
                    "status": "SUCCESS",
                }
            },
        ),
        ("api", "load_booking_reference"),
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_CREATE",
                    "details": {"reason": "reference_load_failed"},
                    "status": "ERROR",
                }
            },
        ),
        ("tool_error", "지금은 예약 접수가 어렵습니다. 잠시 후 다시 시도해 주세요."),
        (
            "final",
            {
                "progress": {
                    "service_id": None,
                    "service_name": None,
                    "operator_id": None,
                    "operator_name": None,
                    "restart_hint": None,
                },
                "reservation_info": {},
                "reservation_data": {},
                "recipient_verification_code": "19850505",
            },
        ),
    ],
    "dynamic_new_customer_after_retry": [
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_CREATE",
                    "details": {},
                    "status": "STARTED",
                }
            },
        ),
        (
            "task",
            "VerifyCustomerIdentityTask",
            {"action": "create", "verify_single_customer_identity": False},
        ),
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_CREATE",
                    "details": {"collecting_verification_code": True},
                    "status": "NEW_CUSTOMER",
                }
            },
        ),
        (
            "task",
            "GetVerificationCodeDtmfTask",
            {
                "num_digits": 6,
            },
        ),
        (
            "task",
            "GetVerificationCodeDtmfTask",
            {
                "num_digits": 6,
            },
        ),
        (
            "task",
            "GetVerificationCodeDtmfTask",
            {
                "num_digits": 6,
            },
        ),
        (
            "event",
            {
                "agent_event": {
                    "name": "IDENTITY_VERIFY",
                    "details": {"action": "create", "source": "new_customer"},
                    "status": "SUCCESS",
                }
            },
        ),
        ("api", "load_booking_reference"),
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_CREATE",
                    "details": {"reason": "reference_load_failed"},
                    "status": "ERROR",
                }
            },
        ),
        ("tool_error", "지금은 예약 접수가 어렵습니다. 잠시 후 다시 시도해 주세요."),
        (
            "final",
            {
                "progress": {
                    "service_id": None,
                    "service_name": None,
                    "operator_id": None,
                    "operator_name": None,
                    "restart_hint": None,
                },
                "reservation_info": {},
                "reservation_data": {},
                "recipient_verification_code": "1990-01-01",
            },
        ),
    ],
    "dynamic_new_customer_rejected": [
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_CREATE",
                    "details": {},
                    "status": "STARTED",
                }
            },
        ),
        (
            "task",
            "VerifyCustomerIdentityTask",
            {"action": "create", "verify_single_customer_identity": False},
        ),
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_CREATE",
                    "details": {"collecting_verification_code": True},
                    "status": "NEW_CUSTOMER",
                }
            },
        ),
        ("task", "GetVerificationCodeDtmfTask", {"num_digits": 6}),
        ("task", "GetVerificationCodeDtmfTask", {"num_digits": 6}),
        ("task", "GetVerificationCodeDtmfTask", {"num_digits": 6}),
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_CREATE",
                    "details": {"reason": "verification_code_confirmation_rejected"},
                    "status": "ERROR",
                }
            },
        ),
        ("tool_error", "데모 확인번호 확인이 거부되었습니다."),
        (
            "final",
            {
                "progress": {
                    "service_id": None,
                    "service_name": None,
                    "operator_id": None,
                    "operator_name": None,
                    "restart_hint": None,
                },
                "reservation_info": {},
                "reservation_data": {},
                "recipient_verification_code": None,
            },
        ),
    ],
    "modify_operator_missing": [
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_MODIFY",
                    "details": {},
                    "status": "STARTED",
                }
            },
        ),
        ("task", "GetMyReservationsTask", {"action": "modify"}),
        (
            "event",
            {
                "agent_event": {
                    "name": "IDENTITY_VERIFY",
                    "details": {"action": "modify", "source": "verified"},
                    "status": "SUCCESS",
                }
            },
        ),
        (
            "task",
            "SelectReservationTask",
            {
                "reservations": [
                    {
                        "reservation_id": "R-1",
                        "appointment_date": "2030-01-09 10:00:00",
                        "service_name": "휴대폰 점검",
                        "service_id": 11,
                        "operator_id": 77,
                        "customer_no": "C-1",
                    }
                ],
                "action": "변경",
                "language_code": "ko-KR",
            },
        ),
        ("audio", False),
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_MODIFY",
                    "details": {"reason": "reservation_operator_missing"},
                    "status": "FAILED",
                }
            },
        ),
        (
            "say",
            "해당 예약의 담당 상담사 정보를 확인할 수 없어 이 전화로는 변경이 어렵습니다. 상담원이 확인할 수 있도록 메모를 남겨드리겠습니다.",
            False,
        ),
        ("playout",),
        (
            "transfer",
            {
                "action_key": "booking_modify",
                "transfer_purpose": {
                    "code": "reservation_operator_missing",
                    "detail": "1월 9일 오전 10시 예약 변경 요청, 상담사 정보 없음",
                },
            },
        ),
        ("return", None),
        (
            "final",
            {
                "progress": {
                    "service_id": None,
                    "service_name": None,
                    "operator_id": None,
                    "operator_name": None,
                    "restart_hint": None,
                },
                "reservation_info": {},
                "reservation_data": {},
                "recipient_verification_code": "1990-01-01",
            },
        ),
    ],
    "modify_reservation_exit": [
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_MODIFY",
                    "details": {},
                    "status": "STARTED",
                }
            },
        ),
        ("task", "GetMyReservationsTask", {"action": "modify"}),
        (
            "event",
            {
                "agent_event": {
                    "name": "IDENTITY_VERIFY",
                    "details": {"action": "modify", "source": "verified"},
                    "status": "SUCCESS",
                }
            },
        ),
        (
            "task",
            "SelectReservationTask",
            {
                "reservations": [
                    {
                        "reservation_id": "R-1",
                        "appointment_date": "2030-01-09 10:00:00",
                        "service_name": "휴대폰 점검",
                        "service_id": 11,
                        "operator_id": 77,
                        "customer_no": "C-1",
                    }
                ],
                "action": "변경",
                "language_code": "ko-KR",
            },
        ),
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_MODIFY",
                    "details": {
                        "reason": "사유",
                        "reason_code": "unrelated",
                        "route_target": "booking_inquiry",
                    },
                    "status": "EXITED",
                }
            },
        ),
        (
            "route",
            {
                "source_action": "booking_modify",
                "reason_code": "unrelated",
                "detail": "사유",
                "route_target": "booking_inquiry",
                "handoff_accepted": None,
                "handoff_channel": None,
                "stopped_before_selection": False,
            },
        ),
        ("return", None),
        (
            "final",
            {
                "progress": {
                    "service_id": None,
                    "service_name": None,
                    "operator_id": None,
                    "operator_name": None,
                    "restart_hint": None,
                },
                "reservation_info": {},
                "reservation_data": {},
                "recipient_verification_code": "1990-01-01",
            },
        ),
    ],
    "modify_schedule_exit": [
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_MODIFY",
                    "details": {},
                    "status": "STARTED",
                }
            },
        ),
        ("task", "GetMyReservationsTask", {"action": "modify"}),
        (
            "event",
            {
                "agent_event": {
                    "name": "IDENTITY_VERIFY",
                    "details": {"action": "modify", "source": "verified"},
                    "status": "SUCCESS",
                }
            },
        ),
        (
            "task",
            "SelectReservationTask",
            {
                "reservations": [
                    {
                        "reservation_id": "R-1",
                        "appointment_date": "2030-01-09 10:00:00",
                        "service_name": "휴대폰 점검",
                        "service_id": 11,
                        "operator_id": 77,
                        "customer_no": "C-1",
                    }
                ],
                "action": "변경",
                "language_code": "ko-KR",
            },
        ),
        (
            "task",
            "OperatorScheduleSelectionTask",
            {
                "operator_id": 77,
                "lookup_days": 50,
                "service_name": "휴대폰 점검",
                "language_code": "ko-KR",
                "chat_ctx": "ChatContext",
            },
        ),
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_MODIFY",
                    "details": {"reason": "사유", "reason_code": "staff_connection"},
                    "status": "EXITED",
                }
            },
        ),
        (
            "route",
            {
                "source_action": "booking_modify",
                "reason_code": "staff_connection",
                "detail": "사유",
                "route_target": None,
                "handoff_accepted": None,
                "handoff_channel": None,
                "stopped_before_selection": False,
            },
        ),
        ("return", None),
        (
            "final",
            {
                "progress": {
                    "service_id": None,
                    "service_name": None,
                    "operator_id": None,
                    "operator_name": None,
                    "restart_hint": None,
                },
                "reservation_info": {},
                "reservation_data": {},
                "recipient_verification_code": "1990-01-01",
            },
        ),
    ],
    "modify_schedule_lookup_failed_operator_not_found": [
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_MODIFY",
                    "details": {},
                    "status": "STARTED",
                }
            },
        ),
        ("task", "GetMyReservationsTask", {"action": "modify"}),
        (
            "event",
            {
                "agent_event": {
                    "name": "IDENTITY_VERIFY",
                    "details": {"action": "modify", "source": "verified"},
                    "status": "SUCCESS",
                }
            },
        ),
        (
            "task",
            "SelectReservationTask",
            {
                "reservations": [
                    {
                        "reservation_id": "R-1",
                        "appointment_date": "2030-01-09 10:00:00",
                        "service_name": "휴대폰 점검",
                        "service_id": 11,
                        "operator_id": 77,
                        "customer_no": "C-1",
                    }
                ],
                "action": "변경",
                "language_code": "ko-KR",
            },
        ),
        (
            "task",
            "OperatorScheduleSelectionTask",
            {
                "operator_id": 77,
                "lookup_days": 50,
                "service_name": "휴대폰 점검",
                "language_code": "ko-KR",
                "chat_ctx": "ChatContext",
            },
        ),
        ("audio", False),
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_MODIFY",
                    "details": {"reason": "reservation_operator_missing"},
                    "status": "FAILED",
                }
            },
        ),
        (
            "say",
            "해당 예약의 담당 상담사 정보를 확인할 수 없어 이 전화로는 변경이 어렵습니다. 상담원이 확인할 수 있도록 메모를 남겨드리겠습니다.",
            False,
        ),
        ("playout",),
        (
            "transfer",
            {
                "action_key": "booking_modify",
                "transfer_purpose": {
                    "code": "reservation_operator_missing",
                    "detail": "1월 9일 오전 10시 예약 변경 요청, 상담사 정보 없음",
                },
            },
        ),
        ("return", None),
        (
            "final",
            {
                "progress": {
                    "service_id": None,
                    "service_name": None,
                    "operator_id": None,
                    "operator_name": None,
                    "restart_hint": None,
                },
                "reservation_info": {},
                "reservation_data": {},
                "recipient_verification_code": "1990-01-01",
            },
        ),
    ],
    "modify_schedule_lookup_failed_other": [
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_MODIFY",
                    "details": {},
                    "status": "STARTED",
                }
            },
        ),
        ("task", "GetMyReservationsTask", {"action": "modify"}),
        (
            "event",
            {
                "agent_event": {
                    "name": "IDENTITY_VERIFY",
                    "details": {"action": "modify", "source": "verified"},
                    "status": "SUCCESS",
                }
            },
        ),
        (
            "task",
            "SelectReservationTask",
            {
                "reservations": [
                    {
                        "reservation_id": "R-1",
                        "appointment_date": "2030-01-09 10:00:00",
                        "service_name": "휴대폰 점검",
                        "service_id": 11,
                        "operator_id": 77,
                        "customer_no": "C-1",
                    }
                ],
                "action": "변경",
                "language_code": "ko-KR",
            },
        ),
        (
            "task",
            "OperatorScheduleSelectionTask",
            {
                "operator_id": 77,
                "lookup_days": 50,
                "service_name": "휴대폰 점검",
                "language_code": "ko-KR",
                "chat_ctx": "ChatContext",
            },
        ),
        ("audio", False),
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_MODIFY",
                    "details": {"reason": "operator_schedule_lookup_failed"},
                    "status": "FAILED",
                }
            },
        ),
        (
            "say",
            "예약 변경 가능한 일정을 확인하는 중 오류가 발생해 이 전화로는 변경이 어렵습니다. 상담원이 확인할 수 있도록 메모를 남겨드리겠습니다.",
            False,
        ),
        ("playout",),
        (
            "transfer",
            {
                "action_key": "booking_modify",
                "transfer_purpose": {
                    "code": "operator_schedule_lookup_failed",
                    "detail": "1월 9일 오전 10시 예약 변경 요청, 상담사 일정 조회 실패",
                },
            },
        ),
        ("return", None),
        (
            "final",
            {
                "progress": {
                    "service_id": None,
                    "service_name": None,
                    "operator_id": None,
                    "operator_name": None,
                    "restart_hint": None,
                },
                "reservation_info": {},
                "reservation_data": {},
                "recipient_verification_code": "1990-01-01",
            },
        ),
    ],
    "modify_success": [
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_MODIFY",
                    "details": {},
                    "status": "STARTED",
                }
            },
        ),
        ("task", "GetMyReservationsTask", {"action": "modify"}),
        (
            "event",
            {
                "agent_event": {
                    "name": "IDENTITY_VERIFY",
                    "details": {"action": "modify", "source": "verified"},
                    "status": "SUCCESS",
                }
            },
        ),
        (
            "task",
            "SelectReservationTask",
            {
                "reservations": [
                    {
                        "reservation_id": "R-1",
                        "appointment_date": "2030-01-09 10:00:00",
                        "service_name": "휴대폰 점검",
                        "service_id": 11,
                        "operator_id": 77,
                        "customer_no": "C-1",
                    }
                ],
                "action": "변경",
                "language_code": "ko-KR",
            },
        ),
        (
            "task",
            "OperatorScheduleSelectionTask",
            {
                "operator_id": 77,
                "lookup_days": 50,
                "service_name": "휴대폰 점검",
                "language_code": "ko-KR",
                "chat_ctx": "ChatContext",
            },
        ),
        ("audio", False),
        ("submit", "booking_modify"),
        (
            "event",
            {
                "agent_event": {
                    "name": "BOOKING_MODIFY",
                    "details": {
                        "reservation_id": "R-1",
                        "previous_reservation_date_time": "2030-01-09 10:00:00",
                        "reservation_request_date_time": "2030-01-07 15:00:00",
                    },
                    "status": "SUCCESS",
                }
            },
        ),
        (
            "say",
            "예약 변경이 접수되었습니다. 아직 확정된 예약은 아니며, 센터에서 확인한 뒤 확정 안내를 드릴게요.\n"
            "통화를 종료하겠습니다. 좋은 하루 되세요.",
            False,
        ),
        ("ctx_playout",),
        ("shutdown", {"drain": True}),
        ("return", None),
        (
            "final",
            {
                "progress": {
                    "service_id": None,
                    "service_name": None,
                    "operator_id": None,
                    "operator_name": None,
                    "restart_hint": None,
                },
                "reservation_info": {
                    "type": "MODIFY_RESERVATION",
                    "service_id": 11,
                    "verification_code": "1990-01-01",
                    "operator_id": 77,
                    "reservation_request_date_time": "2030-01-07 15:00:00",
                    "reservation_record_id": "R-1",
                    "previous_reservation_date_time": "2030-01-09 10:00:00",
                },
                "reservation_data": {},
                "recipient_verification_code": "1990-01-01",
            },
        ),
    ],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_booking_flow_timeline_is_unchanged(monkeypatch, name):
    assert await run_scenario(monkeypatch, name) == SNAPSHOTS[name]
