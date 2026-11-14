from datetime import datetime
from typing import Any, Generic, List, TypeVar

from pydantic import BaseModel, Field
from pydantic.generics import GenericModel

# Operator API Response Types


class OperatorServiceItem(BaseModel):
    """상담사가 담당 가능한 서비스 참조 (실측: {"serviceId": 3588, "name": "..."})"""

    serviceId: int
    name: str | None = None


class OperatorItem(BaseModel):
    """개별 상담사 정보"""

    operatorId: int
    serviceId: int | None = None
    # 상담사가 담당 가능한 서비스 리스트. 필드가 없거나 빈 배열인 상담사는
    # 예약 가능 상담사 목록에서 제외한다. serviceId 단수 필드는 폴백하지 않는다.
    services: List[OperatorServiceItem] | None = None
    displayName: str
    description: str | None = None


# Service API Response Types


class ServiceChildItem(BaseModel):
    """개별 서비스 정보"""

    serviceId: int
    name: str
    operatorCount: int
    keywords: List[str] = Field(default_factory=list)


class ServiceItem(BaseModel):
    """개별 서비스 정보"""

    serviceId: int
    name: str
    operatorCount: int
    keywords: List[str] = Field(default_factory=list)

    services: List[ServiceChildItem] | None


class ServiceContents(BaseModel):
    """서비스 내용"""

    services: List[ServiceItem]


class AvailableScheduleItem(BaseModel):
    """예약 가능 일정 정보"""

    time: str
    advisor_id: int


class AvailableSchedulesContents(BaseModel):
    """예약 가능 일정 내용"""

    schedules: List[AvailableScheduleItem]


class SiteAvailableScheduleItem(BaseModel):
    """사이트 기준 예약 가능 일정 정보"""

    time: str
    operator_ids: List[int] = Field(alias="operatorIds")


class SiteAvailableSchedulesContents(BaseModel):
    """사이트 기준 예약 가능 일정 내용"""

    schedules: List[SiteAvailableScheduleItem]


# Customer API Response Types


class ConfirmedAppointmentItem(BaseModel):
    """RECORD 예약 확정 예약 정보"""

    reservationDateTime: str
    reservationEndDateTime: str | None = None
    operatorId: int | None = None
    operatorName: str | None = None
    serviceId: int | None = None
    serviceName: str | None = None
    customerNumber: str | None = None
    customerName: str | None = None
    state: str
    memo: str | None = None
    appointmentDetail: str | None = None
    reservationRecordId: str | None = None
    todayModifiable: bool
    todayCancellable: bool


class CustomerItem(BaseModel):
    """개별 고객 정보"""

    customerNo: str
    customerNm: str
    verification_code: str
    telNum: str
    type: str | None = None
    responseCode: str | None = None
    reservationRecordMetaData: Any | None = None
    responseYn: str | None = None
    customerType: str | None = None
    new: bool
    fail: bool


class CustomerContents(BaseModel):
    """고객 조회 내용"""

    customers: List[CustomerItem]
    responseCode: str
    customerResponseType: str
    single: bool = False
    new: bool = False
    multi: bool = False


# Generic 타입 변수 정의
T = TypeVar("T")


class APIResponse(GenericModel, Generic[T]):
    """Generic API 응답 - contents 타입을 외부에서 주입 가능"""

    success: bool
    message: str | None = Field(alias="resultMsg")
    data: T | None = Field(alias="contents")

    class Config:
        validate_by_name = True  # 원래 필드명과 alias 모두 허용


ServiceAPIResponse = APIResponse[ServiceContents]

OperatorAPIResponse = APIResponse[List[OperatorItem]]

AvailableSchedulesAPIResponse = APIResponse[AvailableSchedulesContents]


class SiteAvailableSchedulesAPIResponse(APIResponse[SiteAvailableSchedulesContents]):
    """사이트 일정 응답과 HTTP 요청 경과 시간."""

    elapsed_ms: int | None = Field(default=None, exclude=True)
    request_params: dict[str, Any] | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )


CustomerAPIResponse = APIResponse[CustomerContents]

ConfirmedAppointmentsAPIResponse = APIResponse[List[ConfirmedAppointmentItem]]


class OperatorAvailableTimesContents(BaseModel):
    """상담사 예약 가능 시간 내용"""

    operatorId: int
    availableTimes: List[str]


OperatorAvailableTimesAPIResponse = APIResponse[OperatorAvailableTimesContents]


# App API (call context / supplement prompt) Response Types


class SupplementPromptResponse(BaseModel):
    client_id: int
    site_id: str
    prompt_text: str


class CallContext(BaseModel):
    id: str
    client_id: int
    site_id: str
    center_name: str
    context: str
    content_updated_at: datetime
    created_at: datetime
    updated_at: datetime


class CallContextResponse(BaseModel):
    data: CallContext


# External API (SMS) Response Types


class MessageSendResponse(BaseModel):
    """``POST /sms/send`` response (``MessageSendResponse``)."""

    message_id: str | None = Field(default=None, alias="messageId")
    message_type: str | None = Field(default=None, alias="messageType")  # SMS/LMS/MMS
    send_status: str | None = Field(default=None, alias="sendStatus")  # "SUCCESS"
    aligo_message_id: int | None = Field(default=None, alias="aligoMessageId")
    # ALIGO result code: 1 = success
    result_code: int | None = Field(default=None, alias="resultCode")
    result_message: str | None = Field(default=None, alias="resultMessage")
    send_date_time: str | None = Field(default=None, alias="sendDateTime")
    image_count: int | None = Field(default=None, alias="imageCount")


# Reservation submission: written during the call, before success is
# announced.


class ReservationSubmitContents(BaseModel):
    status: str  # ACCEPTED | CONFLICT | NOT_FOUND | INVALID | ERROR
    reservationRecordId: str | None = None
    reservationDateTime: str | None = None
    replayed: bool = False


ReservationSubmitAPIResponse = APIResponse[ReservationSubmitContents]
