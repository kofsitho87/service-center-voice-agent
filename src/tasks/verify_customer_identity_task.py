from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger

from data import CallSessionData
from external_apis.booking_api import get_customer_by_phone
from external_apis.types.request_types import GetCustomerRequest
from external_apis.types.response_types import CustomerItem
from tasks.dtmf_verification_code_task import GetVerificationCodeDtmfTask
from utils.agent_event import record_api_result
from utils.common import normalize_verification_code
from utils.i18n import t

MAX_VERIFY_VERIFICATION_CODE_CONFIRMATION_ATTEMPTS = 3

# `verification_code_not_provided` 는 대조 자체가 일어나지 않은 실패다. `no_match` 와
# 묶으면 발신자 무입력이 RECORD 데이터 문제로 계상된다(분석 v2 owner 축).
EscalationReason = Literal[
    "ambiguous_match", "no_match", "verify_failed", "verification_code_not_provided"
]

# 본인 확인 안내(purpose) 멘트가 정의된 action 키 (registry: verify_customer.purpose.<action>).
# 에스컬레이션 멘트는 registry verify_customer.escalation.* 에서 호출 측(booking_tools)이 해석한다.
_PURPOSE_ACTIONS = frozenset({"create", "inquiry", "modify", "cancel"})


@dataclass
class VerifyCustomerResult:
    status: Literal["matched", "no_customer", "escalated"]
    customer: CustomerItem | None = None
    escalation_reason: EscalationReason | None = None


class VerifyCustomerIdentityTask:
    def __init__(
        self,
        userdata: CallSessionData,
        action: str = "create",
        *,
        verify_single_customer_identity: bool = False,
        history: Any = None,
    ):
        self._userdata = userdata
        self._action = action
        self._verify_single_customer_identity = verify_single_customer_identity
        self._history = history

    def _language(self) -> str | None:
        ccd = getattr(self._userdata, "call_config_data", None)
        return getattr(ccd, "language", None)

    def _purpose_message(self) -> str:
        action = self._action if self._action in _PURPOSE_ACTIONS else "create"
        return t(f"verify_customer.purpose.{action}", self._language())

    async def run(self) -> VerifyCustomerResult:
        userdata = self._userdata
        phone = userdata.recipient_data.phone

        logger.bind(
            event="VERIFY_CUSTOMER_IDENTITY",
            stage="start",
            room_name=userdata.room_name,
            phone=phone,
        ).debug("Starting verify customer identity task")

        # Step 1: Normalize phone and call API
        lookup_result = await self._lookup_customers_by_phone(phone)
        if lookup_result is None:
            return VerifyCustomerResult(status="no_customer")

        normalized_phone, customers = lookup_result

        if not customers:
            logger.bind(
                event="VERIFY_CUSTOMER_IDENTITY",
                stage="no_customer",
                room_name=userdata.room_name,
                phone=normalized_phone,
            ).info("No customers found for phone number")
            return VerifyCustomerResult(status="no_customer")

        logger.bind(
            event="VERIFY_CUSTOMER_IDENTITY",
            stage="customers_found",
            room_name=userdata.room_name,
            phone=normalized_phone,
            count=len(customers),
            customers=customers,
        ).debug("Customers found")

        # Step 2: Single customer — no verification needed unless configured.
        if len(customers) == 1:
            customer = customers[0]
            if self._verify_single_customer_identity:
                logger.bind(
                    event="VERIFY_CUSTOMER_IDENTITY",
                    stage="single_customer_verify_verification_code",
                    room_name=userdata.room_name,
                    phone=normalized_phone,
                    customer_no=customer.customerNo,
                ).debug(
                    "Single customer found, starting configured verification_code verification"
                )

                purpose_msg = self._purpose_message()
                input_verification_code = await self._collect_verification_code(purpose_msg)
                if (
                    input_verification_code is None
                    or self._match_verification_code(
                        input_verification_code,
                        [customer],
                    )
                    is None
                ):
                    logger.bind(
                        event="VERIFY_CUSTOMER_IDENTITY",
                        stage="single_customer_verify_failed",
                        room_name=userdata.room_name,
                        phone=normalized_phone,
                        customer_no=customer.customerNo,
                        input_verification_code=input_verification_code,
                    ).warning("Single customer verification_code verification failed")
                    return VerifyCustomerResult(
                        status="escalated",
                        escalation_reason=(
                            "verification_code_not_provided"
                            if input_verification_code is None
                            else "verify_failed"
                        ),
                    )

            logger.bind(
                event="VERIFY_CUSTOMER_IDENTITY",
                stage="single_customer",
                room_name=userdata.room_name,
                phone=normalized_phone,
                customer_no=customer.customerNo,
            ).info("Single customer found, skipping verification")
            self._set_customer_to_userdata(customer)
            return VerifyCustomerResult(status="matched", customer=customer)

        # Step 3: Multiple customers — verify by verification_code.
        # A confirmed mismatch escalates immediately. Only collection failures get
        # a second chance because no comparison occurred in that case.
        logger.bind(
            event="VERIFY_CUSTOMER_IDENTITY",
            stage="multi_customer",
            room_name=userdata.room_name,
            phone=normalized_phone,
            count=len(customers),
        ).info("Multiple customers found, starting verification_code verification")

        purpose_msg = self._purpose_message()

        attempt_purposes = [
            purpose_msg,
            t("verify_customer.purpose.retry", self._language()),
        ]

        for purpose in attempt_purposes:
            input_verification_code = await self._collect_verification_code(purpose)

            if input_verification_code is None:
                logger.bind(
                    event="VERIFY_CUSTOMER_IDENTITY",
                    stage="collect_verification_code_error",
                    room_name=userdata.room_name,
                    phone=normalized_phone,
                ).warning("Failed to collect verification_code, moving to next attempt")
                continue

            verification_code_matches = self._find_verification_code_matches(input_verification_code, customers)

            if len(verification_code_matches) > 1:
                matched_customer = verification_code_matches[0]
                logger.bind(
                    event="VERIFY_CUSTOMER_IDENTITY",
                    stage="ambiguous_verification_code_first_selected",
                    room_name=userdata.room_name,
                    phone=normalized_phone,
                    input_verification_code=input_verification_code,
                    match_count=len(verification_code_matches),
                    customer_nos=[p.customerNo for p in verification_code_matches],
                    selected_customer_no=matched_customer.customerNo,
                ).warning(
                    "Multiple customers matched the same verification_code; "
                    "selecting the first match without customer number"
                )
                self._set_customer_to_userdata(
                    matched_customer,
                    include_customer_no=False,
                )
                return VerifyCustomerResult(
                    status="matched",
                    customer=matched_customer,
                )

            if len(verification_code_matches) == 1:
                matched_customer = verification_code_matches[0]
                logger.bind(
                    event="VERIFY_CUSTOMER_IDENTITY",
                    stage="matched",
                    room_name=userdata.room_name,
                    phone=normalized_phone,
                    customer_no=matched_customer.customerNo,
                ).info("Customer matched by verification_code")
                self._set_customer_to_userdata(matched_customer)
                return VerifyCustomerResult(status="matched", customer=matched_customer)

            logger.bind(
                event="VERIFY_CUSTOMER_IDENTITY",
                stage="no_match",
                room_name=userdata.room_name,
                phone=normalized_phone,
                input_verification_code=input_verification_code,
            ).warning("VerificationCode did not match any customer, escalating immediately")
            return VerifyCustomerResult(
                status="escalated",
                escalation_reason="no_match",
            )

        # All collection attempts were exhausted without a confirmed verification_code.
        logger.bind(
            event="VERIFY_CUSTOMER_IDENTITY",
            stage="escalated",
            room_name=userdata.room_name,
            phone=normalized_phone,
            verification_code_collected=False,
        ).warning("VerificationCode was not provided after all attempts, escalating")
        return VerifyCustomerResult(
            status="escalated",
            escalation_reason="verification_code_not_provided",
        )

    async def _lookup_customers_by_phone(
        self, phone: str
    ) -> tuple[str, list[CustomerItem]] | None:
        userdata = self._userdata
        client_id = userdata.business_data.client_id

        # Source note: phone normalization was disabled for one customer integration.
        normalized_phone = phone

        request = GetCustomerRequest(
            site_id=userdata.business_data.site_id,
            phone=normalized_phone,
        )

        try:
            response = await get_customer_by_phone(request, client_id=client_id)
            record_api_result(self._history, "GET_CUSTOMER_BY_PHONE", response)
            logger.bind(
                event="VERIFY_CUSTOMER_IDENTITY",
                stage="api_success",
                room_name=userdata.room_name,
                phone=normalized_phone,
                count=len(response.data.customers) if response.data else 0,
                result=response.data,
            ).debug("API call successful")
        except Exception as e:
            logger.bind(
                event="VERIFY_CUSTOMER_IDENTITY",
                stage="api_failed",
                room_name=userdata.room_name,
                phone=normalized_phone,
            ).opt(exception=e).error("API call failed")
            return None

        if not response.success or not response.data:
            logger.bind(
                event="VERIFY_CUSTOMER_IDENTITY",
                stage="api_failed",
                room_name=userdata.room_name,
                phone=normalized_phone,
            ).error("API returned failure or no data")
            return None

        return normalized_phone, response.data.customers

    def _is_caller_disconnected(self) -> bool:
        """caller(SIP participant)가 이미 끊었는지 확인한다.

        끊긴 뒤에는 session이 닫히는 중이라 `GetVerificationCodeDtmfTask`의 activity가
        시작되지 못하고 `ToolError`가 발생하며, 이 activity swap이 session 종료와
        겹치면 `aclose`가 멈춰 shutdown callback이 실행되지 않는다.
        """
        return bool(getattr(self._userdata, "caller_disconnected", False))

    async def _collect_verification_code(self, purpose: str) -> str | None:
        userdata = self._userdata

        # caller가 이미 끊었으면 verification_code DTMF task를 띄우지 않는다.
        # 닫히는 중인 session에 새 activity를 띄우면 종료가 멈춰 분석 payload 전송
        # (shutdown callback)이 누락된다.
        if self._is_caller_disconnected():
            logger.bind(
                event="VERIFY_CUSTOMER_IDENTITY",
                stage="collect_verification_code_skipped_caller_disconnected",
                room_name=userdata.room_name,
                phone=userdata.recipient_data.phone,
            ).debug("Caller already disconnected; skipping verification_code collection")
            return None

        for _ in range(MAX_VERIFY_VERIFICATION_CODE_CONFIRMATION_ATTEMPTS):
            # 재시도 루프 도중 caller가 끊은 경우에도 새 activity를 띄우지 않는다.
            if self._is_caller_disconnected():
                logger.bind(
                    event="VERIFY_CUSTOMER_IDENTITY",
                    stage="collect_verification_code_aborted_caller_disconnected",
                    room_name=userdata.room_name,
                    phone=userdata.recipient_data.phone,
                ).info("Caller disconnected during verification_code collection; aborting")
                return None

            try:
                verification_code_result = await GetVerificationCodeDtmfTask(
                    num_digits=6,
                    purpose=purpose,
                )

                logger.bind(
                    event="VERIFY_CUSTOMER_IDENTITY",
                    stage="verification_code_collected",
                    room_name=userdata.room_name,
                    phone=userdata.recipient_data.phone,
                    verification_code=verification_code_result.verification_code,
                    confirmed=verification_code_result.confirmed,
                ).debug("VerificationCode collected")

                if not verification_code_result.verification_code:
                    return None

                if verification_code_result.confirmed:
                    return verification_code_result.verification_code

            except Exception as e:
                logger.bind(
                    event="VERIFY_CUSTOMER_IDENTITY",
                    stage="collect_verification_code_error",
                    room_name=userdata.room_name,
                    phone=userdata.recipient_data.phone,
                ).opt(exception=e).error("Error while collecting verification_code")
                return None

        return None

    def _find_verification_code_matches(
        self, input_verification_code: str, customers: list[CustomerItem]
    ) -> list[CustomerItem]:
        matches: list[CustomerItem] = []
        for customer in customers:
            try:
                normalized = normalize_verification_code(customer.verification_code)
                if normalized == input_verification_code:
                    matches.append(customer)
            except ValueError:
                continue
        return matches

    def _match_verification_code(
        self, input_verification_code: str, customers: list[CustomerItem]
    ) -> CustomerItem | None:
        matches = self._find_verification_code_matches(input_verification_code, customers)
        if len(matches) == 1:
            return matches[0]
        return None

    def _set_customer_to_userdata(
        self,
        customer: CustomerItem,
        *,
        include_customer_no: bool = True,
    ) -> None:
        # TODO: 고객정보를 수신자 전화번호로 교체
        self._userdata.recipient_data.phone = customer.telNum

        self._userdata.recipient_data.verification_code = customer.verification_code
        self._userdata.recipient_data.customer_no = (
            customer.customerNo if include_customer_no else None
        )
        self._userdata.recipient_data.user_name = customer.customerNm
