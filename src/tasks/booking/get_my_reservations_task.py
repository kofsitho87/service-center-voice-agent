from dataclasses import dataclass, field
from typing import Literal, cast

from livekit.agents import ToolError
from loguru import logger
from typing_extensions import Any

from data import CallSessionData
from external_apis.booking_api import get_confirmed_appointments
from external_apis.types.request_types import GetConfirmedAppointmentsRequest
from tasks.dtmf_verification_code_task import GetVerificationCodeDtmfTask
from tasks.verify_customer_identity_task import (
    EscalationReason,
    VerifyCustomerIdentityTask,
)
from utils.agent_event import record_api_result
from utils.common import normalize_verification_code
from utils.i18n import t

MAX_NO_CUSTOMER_CONFIRMATION_ATTEMPTS = 3

GetMyReservationsStatus = Literal[
    "completed",
    "identity_incomplete",
    "escalated",
    "error",
]

# How the caller's identity was settled (IDENTITY_VERIFY `source`):
# known = verification_code already on file, verified = matched by the customer lookup,
# new_customer = no customer record found, verification_code collected over DTMF.
IdentitySource = Literal["known", "verified", "new_customer"]


@dataclass
class GetMyReservationsResult:
    """Outcome of the reservation lookup flow.

    `completed` means the reservation API returned a successful response. An
    empty reservation list is a valid completed lookup only in that state.
    """

    status: GetMyReservationsStatus
    reservations: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None
    # Set once identity is settled, including later lookup errors.
    identity_source: IdentitySource | None = None

    @property
    def escalation_reason(self) -> EscalationReason | None:
        """Compatibility view; `status` and `reason` remain the source of truth."""
        if self.status != "escalated":
            return None
        return cast(EscalationReason | None, self.reason)


class GetMyReservationsTask:
    def __init__(
        self,
        userdata: CallSessionData,
        action: str = "inquiry",
        *,
        history: Any = None,
    ):
        self._userdata = userdata
        self._action = action
        self._history = history

    async def run(self) -> GetMyReservationsResult:
        """Execute the entire flow to get reservations."""
        userdata = self._userdata
        site_id = userdata.business_data.site_id
        phone = userdata.recipient_data.phone
        verification_code = userdata.recipient_data.verification_code
        user_name = userdata.recipient_data.user_name
        customer_no = userdata.recipient_data.customer_no

        logger.bind(
            event="GET_MY_RESERVATIONS",
            stage="start",
            site_id=site_id,
            phone=phone,
            verification_code=verification_code,
            user_name=user_name,
            customer_no=customer_no,
            room_name=userdata.room_name,
        ).debug("Starting get my reservations task")

        # Step 1: 고객 식별 (verification_code가 없는 경우)
        identity_source: IdentitySource = "known"
        if not verification_code:
            verify_task = VerifyCustomerIdentityTask(
                userdata,
                action=self._action,
                history=self._history,
            )
            verify_result = await verify_task.run()

            if verify_result.status == "no_customer":
                try:
                    verification_code = await self._collect_verification_code_for_no_customer()
                except ToolError as error:
                    if self._is_caller_disconnected():
                        logger.bind(
                            event="GET_MY_RESERVATIONS",
                            stage="collect_verification_code_caller_disconnected",
                            room_name=userdata.room_name,
                        ).info("Caller disconnected while collecting verification_code")
                        return GetMyReservationsResult(
                            status="identity_incomplete",
                            reason="verification_code_not_provided",
                        )
                    logger.bind(
                        event="GET_MY_RESERVATIONS",
                        stage="collect_verification_code_tool_error",
                        room_name=userdata.room_name,
                    ).opt(exception=error).error(
                        "VerificationCode collection tool failed while caller was connected"
                    )
                    return GetMyReservationsResult(
                        status="error",
                        reason="verification_code_collection_failed",
                    )
                except Exception as error:
                    logger.bind(
                        event="GET_MY_RESERVATIONS",
                        stage="collect_verification_code_error",
                        room_name=userdata.room_name,
                    ).opt(exception=error).error("VerificationCode collection failed")
                    return GetMyReservationsResult(
                        status="error",
                        reason="verification_code_collection_failed",
                    )

                if not verification_code:
                    return GetMyReservationsResult(
                        status="identity_incomplete",
                        reason="verification_code_not_provided",
                    )
                userdata.recipient_data.verification_code = verification_code

            if verify_result.status == "escalated":
                if (
                    self._is_caller_disconnected()
                    and verify_result.escalation_reason == "verification_code_not_provided"
                ):
                    return GetMyReservationsResult(
                        status="identity_incomplete",
                        reason="verification_code_not_provided",
                    )
                return GetMyReservationsResult(
                    status="escalated",
                    reason=verify_result.escalation_reason,
                )

            identity_source = (
                "new_customer" if verify_result.status == "no_customer" else "verified"
            )
            # matched 결과 또는 fallback 수집 결과를 userdata에서 다시 읽는다.
            verification_code = userdata.recipient_data.verification_code
            user_name = userdata.recipient_data.user_name
            customer_no = userdata.recipient_data.customer_no or None

        # Step 2: Normalize phone and verification_code
        try:
            # phone = normalize_phone(phone)
            verification_code = normalize_verification_code(verification_code)

            # Update userdata with normalized verification_code
            userdata.recipient_data.verification_code = verification_code
        except ValueError as e:
            logger.bind(
                event="GET_MY_RESERVATIONS",
                stage="normalize_error",
                phone=phone,
                verification_code=verification_code,
                room_name=userdata.room_name,
            ).opt(exception=e).error("Error occurred while normalizing phone or verification_code")
            return GetMyReservationsResult(
                status="error",
                identity_source=identity_source,
                reason="normalization_failed",
            )

        # Step 3: Fetch reservations from API
        try:
            request = GetConfirmedAppointmentsRequest(
                site_id=site_id,
                # phone=normalize_phone(phone),
                phone=phone,
                verification_code=verification_code,
                customer_no=customer_no,
            )
        except Exception as error:
            logger.bind(
                event="GET_MY_RESERVATIONS",
                stage="request_build_error",
                room_name=userdata.room_name,
            ).opt(exception=error).error("Failed to build reservation lookup request")
            return GetMyReservationsResult(
                status="error",
                identity_source=identity_source,
                reason="reservation_request_invalid",
            )

        logger.bind(
            event="GET_MY_RESERVATIONS",
            stage="request",
            site_id=site_id,
            phone=phone,
            verification_code=verification_code,
            user_name=user_name,
            customer_no=customer_no,
        ).debug("Fetching reservations from API")

        try:
            response = await get_confirmed_appointments(
                request, client_id=userdata.business_data.client_id
            )
            record_api_result(
                self._history,
                "GET_CONFIRMED_APPOINTMENTS",
                response,
            )

            # logger.bind(
            #     event="GET_MY_RESERVATIONS",
            #     stage="get_reservations",
            #     response=response,
            #     room_name=userdata.room_name,
            # ).info("Get reservations response")

            if not response.success:
                logger.bind(
                    event="GET_MY_RESERVATIONS",
                    stage="api_failed",
                    room_name=userdata.room_name,
                    reason=response.message,
                ).error("Error occurred while getting reservations")
                return GetMyReservationsResult(
                    status="error",
                    identity_source=identity_source,
                    reason="reservation_api_failed",
                )

            reservation_list = response.data
            if not reservation_list:
                logger.bind(
                    event="GET_MY_RESERVATIONS",
                    stage="no_reservations",
                    room_name=userdata.room_name,
                ).info("No reservations found")
                return GetMyReservationsResult(
                    status="completed",
                    identity_source=identity_source,
                )

            # Step 4: Transform response data
            contents = [
                {
                    "customer_no": item.customerNumber,
                    "reservation_id": item.reservationRecordId,
                    "appointment_date": item.reservationDateTime,
                    "service_name": item.serviceName,
                    "service_id": item.serviceId,
                    "content": item.appointmentDetail,
                    "reservation_state": item.state,
                    "operator_id": item.operatorId,
                }
                for item in reservation_list
            ]

            logger.bind(
                event="GET_MY_RESERVATIONS",
                stage="success",
                site_id=site_id,
                phone=phone,
                verification_code=verification_code,
                user_name=user_name,
                customer_no=customer_no,
                count=len(contents),
                contents=contents,
                room_name=userdata.room_name,
            ).debug("Fetching reservations successfully")

            return GetMyReservationsResult(
                status="completed",
                identity_source=identity_source,
                reservations=contents,
            )

        except Exception as e:
            logger.bind(
                event="GET_MY_RESERVATIONS",
                room_name=userdata.room_name,
            ).opt(exception=e).error("Error occurred while getting reservations")
            return GetMyReservationsResult(
                status="error",
                identity_source=identity_source,
                reason="reservation_api_failed",
            )

    def _is_caller_disconnected(self) -> bool:
        return bool(getattr(self._userdata, "caller_disconnected", False))

    async def _collect_verification_code_for_no_customer(self) -> str | None:
        userdata = self._userdata
        ccd = getattr(userdata, "call_config_data", None)
        purpose = t(
            "get_my_reservations.dtmf_verification_code_purpose",
            getattr(ccd, "language", None),
        )

        for _ in range(MAX_NO_CUSTOMER_CONFIRMATION_ATTEMPTS):
            if self._is_caller_disconnected():
                logger.bind(
                    event="GET_MY_RESERVATIONS",
                    stage="collect_verification_code_skipped_caller_disconnected",
                    room_name=userdata.room_name,
                ).info("Caller disconnected; skipping verification_code collection")
                return None

            verification_code_result = await GetVerificationCodeDtmfTask(
                num_digits=6,
                purpose=purpose,
            )
            if self._is_caller_disconnected():
                return None
            if not verification_code_result.verification_code:
                continue

            if verification_code_result.confirmed:
                return verification_code_result.verification_code

        return None
