from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Literal, Optional, Union

from livekit.agents import JobContext
from livekit.agents.voice import Agent, RunContext
from loguru import logger

TransferMode = Literal[
    "warm_transfer",
    "warm_transfer_no_briefing",
    "cold_transfer",
    "leave_memo",
]
TRANSFER_MODES: tuple[str, ...] = (
    "warm_transfer",
    "warm_transfer_no_briefing",
    "cold_transfer",
    "leave_memo",
)
WARM_TRANSFER_MODES: tuple[str, ...] = (
    "warm_transfer",
    "warm_transfer_no_briefing",
)


def normalize_transfer_mode(value: Any, *, default: str = "warm_transfer") -> str:
    """Return a known transfer mode, falling back to ``default``.

    Unknown strings already ran as a briefed warm transfer, so the fallback keeps
    that behavior while logging it so a misconfigured value is visible.
    """
    if value is None or value == "":
        return default
    if value in TRANSFER_MODES:
        return value
    logger.bind(
        event="TRANSFER_MODE_UNKNOWN",
        transfer_mode=value,
        fallback=default,
    ).warning("Unknown transfer_mode; falling back to default")
    return default


def _coerce_client_id_value(value: Any) -> Optional[int]:
    """client_id는 외부 입력에서 str로 올 수 있으므로 Optional[int]로 정규화한다."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            return None
        return int(stripped)
    if isinstance(value, float):
        return int(value)
    return int(value)


def _coerce_schedule_lookup_days(value: Any) -> int:
    """예약 일정 조회 일수를 1 이상의 정수로 정규화한다."""
    if value is None:
        return 50
    if isinstance(value, bool):
        raise ValueError("schedule_lookup_days must be a positive integer")
    if isinstance(value, int):
        lookup_days = value
    elif isinstance(value, str):
        try:
            lookup_days = int(value.strip())
        except ValueError as exc:
            raise ValueError("schedule_lookup_days must be a positive integer") from exc
    else:
        raise ValueError("schedule_lookup_days must be a positive integer")
    if lookup_days < 1:
        raise ValueError("schedule_lookup_days must be a positive integer")
    return lookup_days


def _normalize_transfer_record_dict(value: dict[str, Any]) -> dict[str, Any]:
    normalized = value.copy()
    transfer_status = normalized.get("transfer_status")
    if isinstance(transfer_status, str):
        normalized["transfer_status"] = transfer_status.lower()
    return normalized


def _normalize_tts_model(call_config: dict[str, Any]) -> Optional[str]:
    """Normalize current and legacy TTS config to ``provider:model`` format."""
    raw_model = call_config.get("tts_model")
    if not isinstance(raw_model, str) or not raw_model.strip():
        return None

    model = raw_model.strip()
    if ":" in model:
        return model

    provider = str(call_config.get("tts_provider") or "google").strip().lower()
    return f"{provider}:{model}"


@dataclass
class BusinessData:
    site_id: Optional[str] = None
    client_id: Optional[int] = None
    client_name: Optional[str] = None
    client_phone: Optional[str] = None
    client_address: Optional[str] = None

    # Is work time
    is_work_time: bool = True

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "client_id":
            value = _coerce_client_id_value(value)
        object.__setattr__(self, name, value)


@dataclass
class LastReservation:
    """기존 고객의 직전 예약 내역 (있으면 이전 상담사 자동 제안에 사용).

    서비스 정보(service_id/service_name)는 선택이다. 직전 예약이 상담사
    정보만 담고 서비스를 특정하지 못하는 경우, 서비스 없이 상담사 기준으로
    예약을 진행한다.
    """

    operator_id: int
    operator_name: str
    service_id: Optional[int] = None
    service_name: Optional[str] = None


@dataclass
class RecipientData:
    user_name: Optional[str] = None
    phone: Optional[str] = None
    verification_code: Optional[str] = None
    customer_no: Optional[str] = None

    # Sip participant identity
    sip_identity: Optional[str] = None

    # 전화번호를 건 사람의 전화번호
    caller_phone: Optional[str] = None

    # 기존 고객의 직전 예약 내역 (있으면 이전 상담사 자동 제안)
    last_reservation: Optional[LastReservation] = None

    def __post_init__(self) -> None:
        # dial_info에서는 dict로 들어오므로 dataclass로 코어싱한다.
        if isinstance(self.last_reservation, dict):
            self.last_reservation = LastReservation(**self.last_reservation)

    def to_safe_dict(self) -> dict[str, Any]:
        # 중첩 dataclass(last_reservation)가 직렬화에서 누락되지 않도록 한다.
        return asdict(self)


@dataclass
class BookingAgentActionConfig:
    """에이전트별 액션 모드 설정"""

    # 예약 신청: "auto" | "transfer" | "leave_memo"
    booking_create: Literal["auto", "transfer", "leave_memo"] = "auto"
    # 예약 변경: "auto" | "transfer" | "leave_memo"
    booking_modify: Literal["auto", "transfer", "leave_memo"] = "auto"
    # 예약 취소: "auto" | "transfer" | "leave_memo"
    booking_cancel: Literal["auto", "transfer", "leave_memo"] = "auto"
    # 예약 조회: "auto" | "transfer" | "leave_memo"
    booking_inquiry: Literal["auto", "transfer", "leave_memo"] = "auto"


@dataclass
class AgentsConfig:
    """전체 에이전트 설정"""

    booking_agent: BookingAgentActionConfig = field(
        default_factory=BookingAgentActionConfig
    )


@dataclass
class ServiceSelectionFlowConfig:
    display_mode: Literal["parent_with_child", "child_only"] = "parent_with_child"
    free_utterance: bool = False  # 서비스 항목 자유 발화


@dataclass
class BookingCreateFlowConfig:
    select_operator: bool = False
    verify_single_customer_identity: bool = False
    service_selection: ServiceSelectionFlowConfig = field(
        default_factory=ServiceSelectionFlowConfig
    )


@dataclass
class BookingFlowConfig:
    # 오늘부터 예약 가능 일정을 조회할 마지막 날짜의 일수 오프셋
    schedule_lookup_days: int = 50
    booking_create: BookingCreateFlowConfig = field(
        default_factory=BookingCreateFlowConfig
    )


@dataclass
class LeaveMessageFlowConfig:
    booking_contact_collection: Literal[
        "desired_datetime_and_message",
        "message_only",
    ] = "message_only"
    # 메모 수집 프롬프트의 ADDITIONAL INSTRUCTIONS 섹션에 그대로 주입되는 자유 지시문.
    # 추가로 받을 정보(이름/확인번호/기기 모델 등)를 여기에 문장으로 적는다. 빈 문자열이면 미주입.
    # 날짜 수집 모드가 아니면 Task 첫 턴(오프너)에서도 이 지시를 참조해 함께 질문한다.
    additional_instructions: str = ""

    def should_collect_desired_datetime(self, action_key: Optional[str]) -> bool:
        return (
            self.booking_contact_collection == "desired_datetime_and_message"
            and action_key in {"booking_create", "booking_modify"}
        )


@dataclass
class WorkflowConfig:
    booking: BookingFlowConfig = field(default_factory=BookingFlowConfig)
    leave_message: LeaveMessageFlowConfig = field(
        default_factory=LeaveMessageFlowConfig
    )


@dataclass
class CallConfigData:
    # 메타정보
    language: Literal[
        "ko-KR",  # Korean
        "en-US",  # English (United States)
    ] = "ko-KR"
    # Agent LLM in ``provider:model`` format (for example, openai:gpt-5.4-mini).
    llm_model: Optional[str] = None
    # Agent STT in ``provider:model`` format (for example, google:latest_long).
    stt_model: Optional[str] = None
    # Agent TTS in ``provider:model`` format (for example, google:chirp_3).
    tts_model: Optional[str] = None
    voice: str = "ko-KR-Chirp3-HD-Kore"
    tts_speed: float = 1.0

    transfer_to: Optional[str] = None
    sip_outbound_trunk_id: Optional[str] = None

    call_start_time: Optional[str] = None
    call_end_time: Optional[str] = None

    # 에이전트별 액션 모드 설정
    agents_config: AgentsConfig = field(default_factory=AgentsConfig)

    transfer_mode: TransferMode = "warm_transfer"
    transfer_message: Optional[str] = None

    def get_language_prompt_string(self) -> str:
        language_mapping = {
            "ko-KR": "한국어",
            "en-US": "English",
        }
        return language_mapping.get(self.language, "한국어")


@dataclass
class TransferRecord:
    """Transfer 최종 결과 기록 (warm/cold 공통)"""

    type: Optional[str] = None  # "warm_transfer" | "cold_transfer"
    action_type: Optional[str] = None  # "transfer" | "transfer_direct"
    transfer_requested_at: Optional[str] = (
        None  # 연결 요청 시각 (YYYY-MM-DD HH:mm:ss, Asia/Seoul)
    )
    transfer_status: Optional[str] = None  # 연결 결과 상태
    transfer_target_phone: Optional[str] = None  # 상담원 번호
    total_wait_seconds: Optional[float] = None  # 총 대기시간 (초, warm only)
    briefing_text: Optional[str] = None  # 상담원 브리핑 텍스트 (warm only)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "transfer_status" and isinstance(value, str):
            value = value.lower()
        object.__setattr__(self, name, value)


@dataclass
class DtmfSmsConfig:
    """DTMF 선택 시 caller 에게 보낼 문자 설정."""

    content: str
    subject: Optional[str] = None
    # 발송 결과에 따라 재생할 안내 멘트(없으면 재생하지 않음)
    on_success_message: Optional[str] = None
    on_failure_message: Optional[str] = None
    # 첨부 이미지 URL 목록(선택). API request 의 imageUrls 로 전달한다.
    image_urls: Optional[list[str]] = None

    @classmethod
    def from_value(cls, value: Any) -> Optional["DtmfSmsConfig"]:
        if isinstance(value, DtmfSmsConfig):
            return value
        if not isinstance(value, dict):
            return None
        content = value.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        return cls(
            content=content,
            subject=value.get("subject") or None,
            on_success_message=value.get("on_success_message") or None,
            on_failure_message=value.get("on_failure_message") or None,
            image_urls=_parse_image_urls(value.get("image_urls")),
        )


def _parse_image_urls(value: Any) -> Optional[list[str]]:
    """문자열 하나 또는 문자열 목록을 받아 빈 값을 걸러낸다. 없으면 None."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return None
    urls = [url.strip() for url in value if isinstance(url, str) and url.strip()]
    return urls or None


@dataclass
class DtmfOption:
    """DTMF 옵션 확장 형식."""

    next: str  # 다음 노드 ID 또는 예약 라우팅 값(direct_input 등)
    user_request: Optional[str] = None  # 에이전트에 전달할 사용자 의도
    tool: Optional[str] = None  # dial_info action_key → 강제 tool 호출
    # 키 선택 직후 next 노드 실행 전에 재생할 안내 멘트
    on_select_message: Optional[str] = None
    # 키 선택 시 caller 에게 보낼 문자(next 와 무관하게 발송)
    sms: Optional[DtmfSmsConfig | dict[str, Any]] = None
    # True 면 안내 멘트·문자 발송 후 next 로 가지 않고 통화를 종료한다.
    end_call: bool = False


DTMF_DIRECT_INPUT_TARGET = "direct_input"


# dial_info dtmf_options.tool (action_key) → LiveKit function_tool 이름.
# 값이 None 이면 강제 호출할 tool 이 없다는 뜻이며, dtmf_options.user_request 를 그대로
# LLM 에 전달해 시스템 프롬프트를 근거로 답하게 한다.
# (등록되지 않은 action_key 만 경고 대상이므로, 여기 없는 key 를 추가할 때 주의)
DTMF_TOOL_ACTION_KEY_TO_FUNCTION: dict[str, Optional[str]] = {
    "booking_create": "create_new_appointment",
    "booking_inquiry": "get_my_appointment_list",
    "booking_cancel": "cancel_appointment",
    "booking_modify": "modify_appointment",
    # 정보 안내: 전용 조회 tool(search_center_knowledge) 을 제거하고
    # 시스템 프롬프트에 주입된 FAQ 로 답한다.
    "info_look_up": None,
}


def parse_dtmf_option(
    option: Any,
) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    """DTMF 옵션 파싱.

    Returns:
        next_node_id, user_request, tool, on_select_message
    """
    if isinstance(option, str):
        return option, None, None, None

    if isinstance(option, DtmfOption):
        return (
            option.next,
            option.user_request,
            option.tool,
            option.on_select_message,
        )

    if isinstance(option, dict):
        return (
            option.get("next", ""),
            option.get("user_request"),
            option.get("tool"),
            option.get("on_select_message"),
        )

    return "", None, None, None


_TRUE_FLAG_STRINGS = frozenset({"true", "on", "yes", "1"})


def _is_on(value: Any) -> bool:
    """dial_info 의 on/off 플래그. YAML on/off 와 백엔드 문자열을 모두 받는다."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_FLAG_STRINGS
    return False


def parse_dtmf_side_actions(option: Any) -> tuple[Optional[DtmfSmsConfig], bool]:
    """DTMF 옵션의 부가 동작 파싱.

    Returns:
        sms(문자 설정, 없거나 잘못되면 None), end_call(통화 종료 여부)
    """
    if isinstance(option, DtmfOption):
        return DtmfSmsConfig.from_value(option.sms), _is_on(option.end_call)
    if isinstance(option, dict):
        return (
            DtmfSmsConfig.from_value(option.get("sms")),
            _is_on(option.get("end_call")),
        )
    return None, False


# DTMF 옵션 타입: 단순 형식(str) 또는 확장 형식(DtmfOption/dict)
DtmfOptionValue = Union[str, DtmfOption, dict[str, Any]]


@dataclass
class FlowNode:
    """플로우 노드 정의"""

    id: str
    type: Literal["condition", "greeting", "agent", "exit", "action"]
    name: str

    # 조건 노드용 (type: condition)
    field: Optional[str] = None
    branches: Optional[dict[str, str]] = None

    # 인사말 노드용 (type: greeting)
    input_method: Optional[Literal["dtmf", "voice"]] = None
    message: Optional[str] = None
    # 다시듣기/오답/타임아웃 시 재생할 인사말. 미설정 시 message로 fallback.
    replay_message: Optional[str] = None
    dtmf_options: Optional[dict[str, DtmfOptionValue]] = None
    # 단순 형식: {"1": "node_id"}
    # 확장 형식:
    # {"1": {"next": "node_id", "user_request": "예약 신청",
    #        "on_select_message": "예약을 도와드리겠습니다."}}

    # 에이전트 노드용 (type: agent)
    # agent_type: Optional[str] = None
    # capabilities: Optional[list[str]] = None

    # 액션 노드용 (type: action)
    action_type: Optional[str] = None
    action_config: Optional[dict[str, Any]] = None

    # 종료 노드용 (type: exit)
    termination: Optional[dict[str, Any]] = None
    events: Optional[list[dict[str, Any]]] = None

    # 공통
    label: Optional[str] = None
    next: Optional[str] = None


@dataclass
class FlowConfig:
    """콜 플로우 설정"""

    entry_node_id: str
    nodes: list[FlowNode] = field(default_factory=list)

    def get_node(self, node_id: str) -> Optional[FlowNode]:
        """노드 ID로 노드 찾기"""
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def get_entry_node(self) -> Optional[FlowNode]:
        """진입 노드 반환"""
        return self.get_node(self.entry_node_id)


@dataclass
class AgentsData:
    """Stores data and agents to be shared across the session"""

    agents: dict[str, Agent] = field(default_factory=dict)

    user_request: Optional[str] = None
    dtmf_tool: Optional[str] = None  # dial_info dtmf_options.tool (action_key)


@dataclass
class BookingCreateProgress:
    """create_new_appointment 플로우의 단계별 완료 상태.

    예약 신청 도중 상담사/서비스 변경 요청으로 플로우가 재시작될 때
    이미 완료된 단계를 건너뛰기 위해 사용한다. 저장된 값의 재사용은
    restart_hint가 설정된 재진입에서만 허용된다.
    """

    service_id: Optional[int] = None
    service_name: Optional[str] = None
    operator_id: Optional[int] = None
    operator_name: Optional[str] = None
    # 재시작 사유: "change_advisor" | "change_service"
    restart_hint: Optional[str] = None

    def clear(self) -> None:
        self.service_id = None
        self.service_name = None
        self.operator_id = None
        self.operator_name = None
        self.restart_hint = None


@dataclass(frozen=True)
class DynamicBookingPending:
    """Same-call resumable candidate for the dynamic booking task.

    This is intentionally independent of tasks.dynamic_booking_task to
    avoid coupling session data to an AgentTask implementation.
    """

    service_id: int
    service_name: str
    operator_id: int
    operator_name: str
    datetime: str


@dataclass
class DynamicBookingResumeContext:
    """Same-call search preferences, never a reservation or customer consent."""

    search_conditions: dict[str, Any] = field(default_factory=dict)
    rejected_candidates: list[dict[str, Any]] = field(default_factory=list)
    excluded_dates: list[str] = field(default_factory=list)
    paused: bool = False
    last_exit_reason: str | None = None
    last_handled_user_message_id: str | None = None
    # Handoff state for this booking attempt. It lives here, not on the task, so
    # re-entering DynamicBookingTask cannot forget a refusal or repeat a delivered
    # proactive offer.
    handoff_declined_evidence: dict[str, str] | None = None
    handoff_offer_delivered: bool = False
    # Verified other-advisor alternative already presented for an off-list advisor
    # in this attempt (presented once; a refusal or insistence then gets help).
    advisor_alternative_presented: bool = False
    # Alternatives the caller declined in this booking attempt (rejected
    # candidates, re-searches after a no-match). Drives the no-slot help offer.
    declined_alternatives: int = 0


@dataclass
class BookingMemoContext:
    """Grounded booking facts carried into a staff-request memo."""

    service_name: str | None = None
    operator_name: str | None = None
    time_condition: str | None = None
    searched_ranges: list[str] = field(default_factory=list)
    detail: str = ""
    exit_reason_code: str = ""


# CallSessionData fields left out of the Kafka payload (to_safe_dict).
_SAFE_DICT_EXCLUSIONS = frozenset(
    {
        "ctx",
        "agents_data",
        "reservation_data",
        "lkapi_ref",
        "flow_config",
        "is_warm_transferring",
        "workflow_config",
        "dynamic_booking_reference",
        "dynamic_booking_pending",
        "dynamic_booking_resume",
        "booking_memo_context",
        "booking_create_progress",
        "caller_disconnected",
    }
)

# CallSessionData fields left out of the CALL_SESSION_DATA debug log (to_dict).
_DEBUG_DICT_EXCLUSIONS = frozenset(
    {
        "ctx",
        "lkapi_ref",
        "is_warm_transferring",
        "dynamic_booking_pending",
        "dynamic_booking_resume",
        "booking_memo_context",
        "pending_api_results",
    }
)


@dataclass
class CallSessionData:
    ctx: JobContext
    agents_data: Optional[AgentsData] = None

    reservation_data: dict[str, Any] = field(default_factory=dict)
    reservation_info: dict[str, Any] = field(default_factory=dict)

    lkapi_ref: dict[str, Any] = field(default_factory=dict)

    # 방 이름
    room_name: Optional[str] = None

    # 업체정보
    business_data: Optional[BusinessData] = None

    # 사용자정보
    recipient_data: Optional[RecipientData] = None

    # 통화정보
    call_config_data: Optional[CallConfigData] = None

    # 업무 플로우 세부 설정
    workflow_config: WorkflowConfig = field(default_factory=WorkflowConfig)

    # 예약 신청(create) 플로우 단계별 진행 상태 (변경 요청 재시작 시 재사용)
    booking_create_progress: BookingCreateProgress = field(
        default_factory=BookingCreateProgress
    )

    # 다이나믹 예약(V3 클로드 트랙) 서비스/상담사 참조 데이터 캐시.
    # tasks.dynamic_booking_task.BookingReference 인스턴스를 담는다.
    # 통화 중 create_new_appointment가 재호출돼도 API를 다시 조회하지 않는다.
    dynamic_booking_reference: Optional[Any] = None

    # 다이나믹 예약 태스크가 unrelated 요청으로 잠시 이탈했을 때 같은 통화에서
    # 재검증 후 이어갈 후보. 통화 중에만 쓰는 ephemeral 상태다.
    dynamic_booking_pending: DynamicBookingPending | None = None

    dynamic_booking_resume: DynamicBookingResumeContext | None = None

    booking_memo_context: BookingMemoContext | None = None

    # AgentSession 생성 전에 실행되는 API 결과를 on_enter에서 developer 이벤트로
    # 옮기기 위한 임시 저장소. 현재 FAQ 프롬프트 사전 조회가 이 경로를 사용한다.
    pending_api_results: list[dict[str, Any]] = field(default_factory=list)

    # 녹음 파일 경로 (S3)
    recording_filepath: Optional[str] = None

    # 녹음이 실제로 시작된 시각(epoch seconds, 이 프로세스의 time.time() 기준).
    # egress가 EGRESS_ACTIVE로 전이한 것을 관측한 순간이며, 녹음 파일의 t=0에
    # 대응한다. 대화 타임스탬프(started_speaking_at)와 같은 클럭이라 두 값을
    # 그대로 빼면 파일 내 위치가 나온다. ACTIVE를 못 본 통화에서는 None이고,
    # 그 경우 분석 계층이 기존 추정(첫 발화 시각)으로 폴백한다.
    recording_started_at: Optional[float] = None

    # 플로우 설정
    flow_config: Optional[FlowConfig] = None

    # Warm transfer 진행 중 여부 (통화 상태 기록용)
    is_warm_transferring: bool = False

    # Caller(SIP participant)가 이미 끊었는지 여부
    # participant_disconnected 이벤트에서 set; greeting/timeout 분기가 caller 부재를
    # 감지하여 의미없는 안내 메시지 발화와 분석 payload 오염을 방지하기 위해 사용한다.
    caller_disconnected: bool = False

    # Transfer 결과 기록 (warm/cold 공통)
    transfer_record: Optional[TransferRecord] = None

    def update_new_reservation_info(self, **updates: Any) -> None:
        """Merge confirmed booking selections into the call-end payload.

        Replace the dict instead of mutating it in place so the shutdown callback
        always observes either the previous or the fully updated snapshot.
        """
        self.reservation_info = {
            **self.reservation_info,
            "type": "NEW_RESERVATION",
            **updates,
        }

    def replace_new_reservation_info(self, **confirmed_values: Any) -> None:
        """Replace partial booking data after a selection restart.

        An empty replacement represents a restart from service selection.
        """
        self.reservation_info = (
            {
                "type": "NEW_RESERVATION",
                **confirmed_values,
            }
            if confirmed_values
            else {}
        )

    @classmethod
    def from_dial_info(
        cls, ctx: JobContext, dial_info: dict[str, Any]
    ) -> "CallSessionData":
        """
        Create CallSessionData instance from dial_info JSON dictionary.

        Args:
            ctx: JobContext instance (required)
            dial_info: Dictionary containing dial information with structure:
                - Top-level fields: site_id, client_id, client_name, client_phone,
                  client_address, is_work_time (for BusinessData)
                - call_config_data: Dictionary for CallConfigData
                - Optional: recipient_data, room_name, etc.

        Returns:
            CallSessionData instance populated from dial_info
        """

        # Extract business data from top-level fields
        business_data = dial_info.get("business_data", {})
        business_data = BusinessData(**business_data)

        # Extract recipient data from top-level fields
        recipient_data = dial_info.get("recipient_data", {})
        recipient_data = RecipientData(**recipient_data)

        # Extract call config data
        call_config_dict = dial_info.get("call_config_data", {})

        # Extract agents_config
        agents_config_dict = dial_info.get("agents_config", {})
        booking_agent_config_dict = agents_config_dict.get("booking_agent", {})
        booking_agent_config = BookingAgentActionConfig(
            booking_create=booking_agent_config_dict.get("booking_create", "auto"),
            booking_modify=booking_agent_config_dict.get("booking_modify", "auto"),
            booking_cancel=booking_agent_config_dict.get("booking_cancel", "auto"),
            booking_inquiry=booking_agent_config_dict.get("booking_inquiry", "auto"),
        )
        agents_config = AgentsConfig(booking_agent=booking_agent_config)

        call_config_data = CallConfigData(
            language=call_config_dict.get("language", "ko-KR"),
            llm_model=(
                call_config_dict.get("llm_model")
                or call_config_dict.get("llm_model_name")
            ),
            stt_model=call_config_dict.get("stt_model"),
            tts_model=_normalize_tts_model(call_config_dict),
            voice=call_config_dict.get("voice", "ko-KR-Chirp3-HD-Kore"),
            tts_speed=call_config_dict.get("tts_speed", 1.0),
            transfer_to=call_config_dict.get("transfer_to"),
            sip_outbound_trunk_id=call_config_dict.get("sip_outbound_trunk_id"),
            call_start_time=call_config_dict.get("call_start_time"),
            call_end_time=call_config_dict.get("call_end_time"),
            transfer_mode=normalize_transfer_mode(
                call_config_dict.get("transfer_mode")
            ),
            transfer_message=call_config_dict.get("transfer_message"),
            agents_config=agents_config,
        )

        workflow_config_dict = dial_info.get("workflow_config", {}) or {}
        workflow_booking_config_dict = workflow_config_dict.get("booking", {}) or {}
        booking_create_flow_config_dict = (
            workflow_booking_config_dict.get("booking_create", {})
            or workflow_booking_config_dict.get("create", {})
            or {}
        )
        service_selection_config_dict = (
            booking_create_flow_config_dict.get("service_selection", {}) or {}
        )
        workflow_config = WorkflowConfig(
            booking=BookingFlowConfig(
                schedule_lookup_days=_coerce_schedule_lookup_days(
                    workflow_booking_config_dict.get("schedule_lookup_days")
                ),
                booking_create=BookingCreateFlowConfig(
                    select_operator=booking_create_flow_config_dict.get(
                        "select_operator", False
                    ),
                    verify_single_customer_identity=booking_create_flow_config_dict.get(
                        "verify_single_customer_identity", False
                    ),
                    service_selection=ServiceSelectionFlowConfig(
                        display_mode=service_selection_config_dict.get(
                            "display_mode", "parent_with_child"
                        ),
                        free_utterance=service_selection_config_dict.get(
                            "free_utterance", False
                        ),
                    ),
                ),
            ),
            leave_message=LeaveMessageFlowConfig(
                booking_contact_collection=(
                    (workflow_config_dict.get("leave_message", {}) or {}).get(
                        "booking_contact_collection",
                        "message_only",
                    )
                ),
                additional_instructions=(
                    (workflow_config_dict.get("leave_message", {}) or {}).get(
                        "additional_instructions"
                    )
                    or ""
                ),
            ),
        )

        # Extract flow config if present
        flow_config: Optional[FlowConfig] = None
        flow_config_dict = dial_info.get("flow_config")
        if flow_config_dict:
            nodes: list[FlowNode] = []
            for node_dict in flow_config_dict.get("nodes", []):
                node = FlowNode(
                    id=node_dict.get("id", ""),
                    type=node_dict.get("type", "condition"),
                    name=node_dict.get("name", ""),
                    field=node_dict.get("field"),
                    branches=node_dict.get("branches"),
                    input_method=node_dict.get("input_method"),
                    message=node_dict.get("message"),
                    replay_message=node_dict.get("replay_message"),
                    dtmf_options=node_dict.get("dtmf_options"),
                    action_type=node_dict.get("action_type"),
                    action_config=node_dict.get("action_config"),
                    termination=node_dict.get("termination"),
                    events=node_dict.get("events"),
                    label=node_dict.get("label"),
                    next=node_dict.get("next"),
                )
                nodes.append(node)
            flow_config = FlowConfig(
                entry_node_id=flow_config_dict.get("entry_node_id", ""),
                nodes=nodes,
            )

        return cls(
            ctx=ctx,
            room_name=ctx.room.name,
            business_data=business_data,
            recipient_data=recipient_data,
            call_config_data=call_config_data,
            workflow_config=workflow_config,
            flow_config=flow_config,
            recording_filepath=None,
        )

    @property
    def leave_message_flow_config(self) -> LeaveMessageFlowConfig:
        return self.workflow_config.leave_message

    def to_safe_dict(self, exclude_fields: Optional[set[str]] = None) -> dict[str, Any]:
        """
        Safely convert this CallSessionData instance to dict, handling unpicklable objects gracefully.

        Args:
            exclude_fields: Additional field names to exclude from conversion

        Returns:
            Dict representation of the CallSessionData, excluding problematic fields
        """
        return self._serialize((exclude_fields or set()) | _SAFE_DICT_EXCLUSIONS)

    def to_dict(self) -> dict[str, Any]:
        """
        Convert this CallSessionData instance to dict.
        """
        return self._serialize(_DEBUG_DICT_EXCLUSIONS)

    def _serialize(self, exclude_fields: set[str]) -> dict[str, Any]:
        result = {}

        for field_name in self.__dataclass_fields__:
            if field_name in exclude_fields:
                continue

            try:
                value = getattr(self, field_name)

                # Handle None values
                if value is None:
                    result[field_name] = None
                    continue

                # Handle basic types directly
                if isinstance(value, (str, int, float, bool)):
                    result[field_name] = value
                    continue

                # Handle collections
                if isinstance(value, (list, tuple)):
                    result[field_name] = [
                        item.to_safe_dict()
                        if hasattr(item, "to_safe_dict") and callable(item.to_safe_dict)
                        else item
                        for item in value
                    ]
                    continue

                if isinstance(value, dict):
                    if field_name == "reservation_info" and not value:
                        result[field_name] = None
                        continue
                    if field_name == "transfer_record":
                        result[field_name] = _normalize_transfer_record_dict(value)
                        continue
                    result[field_name] = {
                        k: (
                            v.to_safe_dict()
                            if hasattr(v, "to_safe_dict") and callable(v.to_safe_dict)
                            else v
                        )
                        for k, v in value.items()
                    }
                    continue

                # Handle nested dataclasses - check if they have to_safe_dict method first
                if is_dataclass(value):
                    if field_name == "workflow_config":
                        result[field_name] = asdict(value)
                        continue

                    if hasattr(value, "to_safe_dict") and callable(value.to_safe_dict):
                        result[field_name] = value.to_safe_dict()
                    else:
                        # Fallback to basic dict conversion for dataclasses without to_safe_dict
                        safe_dict = {}
                        for nested_field in value.__dataclass_fields__:
                            try:
                                nested_value = getattr(value, nested_field)
                                # Handle basic types
                                if isinstance(
                                    nested_value, (str, int, float, bool, type(None))
                                ):
                                    safe_dict[nested_field] = nested_value
                                # Handle lists and tuples
                                elif isinstance(nested_value, (list, tuple)):
                                    safe_dict[nested_field] = list(nested_value)
                                # Handle dicts
                                elif isinstance(nested_value, dict):
                                    safe_dict[nested_field] = nested_value
                            except Exception:
                                continue
                        if field_name == "transfer_record":
                            safe_dict = _normalize_transfer_record_dict(safe_dict)
                        result[field_name] = safe_dict if safe_dict else str(value)
                    continue

                # For complex objects that might fail, try __dict__ or fallback to str representation
                try:
                    if hasattr(value, "__dict__"):
                        # Try to get a safe dict representation
                        safe_dict = {
                            k: v
                            for k, v in value.__dict__.items()
                            if isinstance(v, (str, int, float, bool, type(None)))
                        }
                        result[field_name] = safe_dict if safe_dict else str(value)
                    else:
                        result[field_name] = str(value)
                except Exception:
                    result[field_name] = f"<{type(value).__name__} object>"

            except Exception:
                continue

        return result


RunContext_T = RunContext[CallSessionData]
