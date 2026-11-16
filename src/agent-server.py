import asyncio
import json
import os
import time
from dataclasses import replace

from livekit import rtc

from agents.single_agent import SingleAgent
from external_apis.kafka_producer import send_kafka_message_async
from services.agent_lifecycle import ExitCodeAgentServer, run_agent_app
from utils.common import (
    initialize_recording,
    stop_egress,
    wait_for_recording_anchor,
)
from utils.entrypoint_bootstrap import (
    build_agents_data,
    configure_process_environment,
    kst_now_naive_iso,
)

configure_process_environment(dotenv_override=False)

from langfuse import Langfuse
from livekit.agents import (
    AgentServer,
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    room_io,
)
from livekit.agents.telemetry import set_tracer_provider
from livekit.agents.worker import ServerType
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.util.types import AttributeValue

from data import CallSessionData
from utils.agent_interruption import (
    InterruptionTracker,
    bind_interruption_tracker,
)
from utils.agent_llm import build_production_agent_llm_async
from utils.agent_nc import (
    NCSessionCounters,
    bind_nc_session,
    build_nc_processor,
    finalize_nc,
    resolve_canary_pct_override,
    resolve_nc_assignment,
    warmup_nc,
)
from utils.agent_stt import (
    bind_agent_stt_session,
    build_agent_stt,
    resolve_agent_stt_model,
)
from utils.agent_tts import build_agent_tts_async
from utils.logging_utils import setup_json_logging
from utils.session_event_handlers import register_session_event_handlers
from utils.session_settings import (
    PRODUCTION_MAX_TOOL_STEPS,
    PRODUCTION_NON_KO_GOOGLE_VOICE,
    PRODUCTION_STT_CUSTOM_VOCABULARY,
    load_production_vad,
    production_tts_text_transforms,
    production_turn_handling,
    resolve_production_interruption_settings,
)

# STT 타임스탬프가 미래 시각으로 어긋나 턴 커밋이 수 초 지연되는 현상 방어
# (VAD가 "네" 같은 초단발 발화를 놓칠 때 STT 타임스탬프 폴백 경로에서 발생)
# apply_stt_future_timestamp_clamp_patch()

ENV = os.getenv("ENV", "dev").lower()

# Configure JSON logging with required fields
logger = setup_json_logging(level=os.getenv("LOG_LEVEL", "INFO"))


def setup_langfuse(
    metadata: dict[str, AttributeValue] | None = None,
) -> TracerProvider | None:
    """Configure LiveKit OpenTelemetry spans when Langfuse credentials exist."""
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    base_url = os.getenv("LANGFUSE_BASE_URL")
    if not public_key or not secret_key or not base_url:
        logger.debug(
            "Langfuse tracing is not configured; LANGFUSE_PUBLIC_KEY, "
            "LANGFUSE_SECRET_KEY, and LANGFUSE_BASE_URL are required"
        )
        return None

    try:
        trace_provider = TracerProvider()
        set_tracer_provider(trace_provider, metadata=metadata)
        Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=base_url,
            tracer_provider=trace_provider,
            environment=ENV,
            should_export_span=lambda span: True,
        )
    except Exception as e:
        logger.bind(event="LANGFUSE_SETUP_ERROR").opt(exception=e).warning(
            "Failed to configure Langfuse tracing; continuing without tracing"
        )
        return None

    logger.bind(event="LANGFUSE_TRACING_ENABLED").info("Langfuse tracing enabled")
    return trace_provider


agent_name = os.getenv("AGENT_NAME", "inbound-demo-test-local-publisher")
livekit_url = os.getenv("LIVEKIT_URL", "ws://localhost:7880")

print("🚀 Starting inbound agent with:")
print(f"  - Agent Name: {agent_name}")
print(f"  - LiveKit URL: {livekit_url}")


server = ExitCodeAgentServer(
    load_threshold=1.0,
    # initialize_process_timeout=60.0,  # 10초 → 60초로 증가 (prewarm 시간 확보)
    # shutdown 콜백이 Kafka 발행 + NC 요약/덤프 업로드(최대 5초)를 수행하므로
    # 기본 10초로는 예산이 부족하다.
    shutdown_process_timeout=30.0,
)


def _normalize_phone(phone: object) -> str:
    """전화번호 표기 차이(+82/0 접두, 하이픈/공백 등)를 무시하고 숫자만 남긴다.

    한국 번호는 국가코드(+82)와 국내(0) 접두 표기가 섞여 들어올 수 있으므로,
    끝에서부터 비교 가능한 최소 형태(선행 0/82 제거한 숫자열)로 정규화한다.
    """
    if not phone:
        return ""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if digits.startswith("82"):
        digits = digits[2:]
    digits = digits.lstrip("0")
    return digits


def _resolve_langfuse_user_id(call_session_data: CallSessionData) -> str | None:
    """Use the caller's phone number as the Langfuse user identifier.

    Prefer the number that originated the call over customer data collected during
    the conversation. Preserve the original phone-number representation so the
    Langfuse user ID matches the source data exactly.
    """
    recipient_data = call_session_data.recipient_data
    if recipient_data is None:
        return None

    caller_phone = recipient_data.caller_phone or recipient_data.phone
    if not caller_phone:
        return None

    return str(caller_phone)


def prewarm(proc: JobProcess):
    try:
        proc.userdata["vad"] = load_production_vad()
        logger.debug("Prewarm complete: VAD model loaded")
    except Exception as e:
        logger.opt(exception=e).error("Prewarm failed: VAD model load error")
        raise  # 프리웜 실패 시 프로세스를 사용 불가로 표시


def compute_load(agent_server: AgentServer) -> float:
    """동시 통화 5개와 setup 중 job 1개를 허용하도록 부하 계산.

    LiveKit의 active_jobs는 이미 연결된 통화뿐 아니라 participant join을
    기다리는 setup 중 job도 포함한다. 따라서 "연결된 통화 5개"를 허용하려면
    내부 active_jobs 기준으로는 6개까지 수용하고, 7개째부터 막아야 한다.

    - 5 active_jobs (예: 연결 4개 + setup 1개) -> 허용
    - 6 active_jobs (예: 연결 5개 + setup 1개) -> 허용
    - 7 active_jobs -> 차단
    """
    max_jobs = 5
    current_load = len(agent_server.active_jobs) / max_jobs
    # print(f"Current load: {current_load}")
    return min(current_load, 1.0)


server.setup_fnc = prewarm
server.load_fnc = compute_load


async def agent_session_start(ctx: JobContext, dial_info: dict):
    call_session_data = CallSessionData.from_dial_info(ctx, dial_info)
    # Recording setup runs in the background independently of agent/session startup.
    recording_setup_task = asyncio.create_task(
        initialize_recording(ctx, call_session_data, egress_storage_type="s3")
    )
    call_session_data.lkapi_ref["recording_setup_task"] = recording_setup_task

    langfuse_metadata: dict[str, AttributeValue] = {
        "session.id": ctx.room.name,
        "deployment.environment": ENV,
    }
    if langfuse_user_id := _resolve_langfuse_user_id(call_session_data):
        langfuse_metadata["user.id"] = langfuse_user_id

    business_data = call_session_data.business_data
    if business_data is not None:
        business_metadata = {
            "site_id": business_data.site_id,
            "client_id": business_data.client_id,
            "client_name": business_data.client_name,
        }
        langfuse_metadata.update(
            {
                f"langfuse.trace.metadata.{key}": value
                for key, value in business_metadata.items()
                if value is not None
            }
        )

    trace_provider = setup_langfuse(metadata=langfuse_metadata)
    if trace_provider is not None:

        async def flush_langfuse_trace():
            try:
                trace_provider.force_flush()
            except Exception as e:
                logger.bind(event="LANGFUSE_FLUSH_ERROR").opt(exception=e).warning(
                    "Failed to flush Langfuse traces during shutdown"
                )

        ctx.add_shutdown_callback(flush_langfuse_trace)


    logger.bind(call_session_data=call_session_data.to_dict()).debug(
        "CALL_SESSION_DATA"
    )

    call_session_data.agents_data = build_agents_data(
        # 압축된 센터 안내 context를 조회해 시스템 프롬프트에 주입한다.
        # (조회 실패/빈 값이면 no-knowledge 프롬프트로 폴백)
        single_agent=await SingleAgent.create(call_session_data=call_session_data),
    )

    session: AgentSession[CallSessionData] | None = None
    # NC(ai-coustics trial) per-session state; filled after the shutdown callback
    # is registered, read by it at shutdown.
    nc_state: dict[str, object] = {}

    async def finalize_nc_safe() -> None:
        assignment = nc_state.pop("assignment", None)
        if assignment is None:
            return
        settings = nc_state.get("interruption_settings")
        tracker = nc_state.get("interruption_tracker")
        await finalize_nc(
            assignment=assignment,
            tee=nc_state.get("tee"),
            counters=nc_state.get("counters") or NCSessionCounters(),
            room_name=ctx.room.name,
            env=ENV,
            started_at=nc_state.get("started_at") or time.monotonic(),
            extra={
                "interruption_min_duration": (
                    settings.min_duration if settings else None
                ),
                "interruption_min_words": settings.min_words if settings else None,
                "interruptions_logged": tracker.count if tracker else 0,
            },
        )

    ####################### shutdown callback function #######################
    async def shutdown_callback():
        logger.bind(
            event="SHUTDOWN_CALLBACK",
            room_name=ctx.room.name,
            phone=call_session_data.recipient_data.phone,
        ).info("********* Shutdown callback *********")

        call_session_data.call_config_data.call_end_time = kst_now_naive_iso()

        # 짧은 통화에서도 백그라운드 ACTIVE 관찰 결과가 분석 payload에 반영되도록
        # 녹음을 중지하기 전에 제한된 시간만 합류한다.
        await wait_for_recording_anchor(call_session_data)

        # 1. Stop recording
        if call_session_data.lkapi_ref.get("instance") is not None:
            logger.bind(
                event="STOP_RECORDING",
                room_name=ctx.room.name,
                phone=call_session_data.recipient_data.phone,
            ).info("********* STOP_RECORDING *********")
            call_session_data.lkapi_ref["stop_egress_task"] = asyncio.create_task(
                stop_egress(ctx, call_session_data.lkapi_ref)
            )

        if session is None:
            logger.bind(
                event="SHUTDOWN_SESSION_UNAVAILABLE",
                room_name=ctx.room.name,
            ).warning("Skipping conversation export: AgentSession was not created")
            await finalize_nc_safe()
            return

        # TODO: Change to kafka message `service-center.call-analysis`
        logger.bind(
            event="SEND_KAFKA_MESSAGE",
            topic="service-center.call-analysis",
            room_name=ctx.room.name,
            phone=call_session_data.recipient_data.phone,
        ).info("********* SEND_KAFKA_MESSAGE: service-center.call-analysis *********")

        # 2. Send kafka message
        kafka_result = await send_kafka_message_async(
            message={
                "call_session_data": call_session_data.to_safe_dict(),
                "conversation": {
                    "items": [
                        item.model_dump()
                        for item in session.history.items
                        if item.type == "message"
                    ],
                },
                "agent_events": [
                    event.model_dump()
                    for event in session._recorded_events
                    if event.type
                    in ("conversation_item_added", "function_tools_executed")
                ],
            },
            topic="service-center.call-analysis",
            key=None,
        )
        logger.bind(
            event="SEND_KAFKA_MESSAGE_RESULT",
            result=kafka_result,
            topic="service-center.call-analysis",
            room_name=ctx.room.name,
            phone=call_session_data.recipient_data.phone,
        ).info("********* SEND_KAFKA_MESSAGE_RESULT *********")

        # 3. NC trial summary + optional caller-audio dump (fail-open, after Kafka)
        await finalize_nc_safe()

    ctx.add_shutdown_callback(shutdown_callback)

    # --- NC (ai-coustics trial): per-session assignment + warmup -------------
    # Decided once per session (sha1 bucket of the room name, Redis override as
    # kill switch). Any failure degrades to "no NC"; the call never depends on it.
    nc_override = await resolve_canary_pct_override(env=ENV)
    nc_assignment = resolve_nc_assignment(
        ctx.room.name, canary_pct_override=nc_override
    )
    nc_tee = build_nc_processor(nc_assignment)
    nc_input = None
    if nc_tee is not None:
        if await warmup_nc(nc_tee):
            nc_input = nc_tee
        else:
            # Keep the tee for its init_failed stats, but report the session as
            # effectively unprocessed.
            nc_assignment = replace(nc_assignment, engine="none")
    nc_counters = NCSessionCounters()
    nc_state.update(
        assignment=nc_assignment,
        tee=nc_tee,
        counters=nc_counters,
        started_at=time.monotonic(),
    )
    logger.bind(
        event="NC_ASSIGNED",
        room_name=ctx.room.name,
        nc_engine=nc_assignment.engine,
        nc_model=nc_assignment.model,
        nc_source=nc_assignment.source,
        nc_canary_pct=nc_assignment.canary_pct,
        nc_bucket=nc_assignment.bucket,
        nc_dump=nc_assignment.dump,
    ).info("NC assignment resolved")

    stt_model = resolve_agent_stt_model(call_session_data.call_config_data.stt_model)
    logger.bind(agent_stt_model=stt_model).debug("Configured agent STT")
    agent_stt = build_agent_stt(
        stt_model,
        language=call_session_data.call_config_data.language,
        vad=ctx.proc.userdata["vad"],
        custom_vocabulary=list(PRODUCTION_STT_CUSTOM_VOCABULARY),
    )

    interrupt_settings = resolve_production_interruption_settings()
    nc_state["interruption_settings"] = interrupt_settings
    logger.bind(
        event="INTERRUPT_SETTINGS",
        min_duration=interrupt_settings.min_duration,
        min_words=interrupt_settings.min_words,
        source=interrupt_settings.source,
    ).info("Interruption settings resolved")

    session = AgentSession[CallSessionData](
        vad=ctx.proc.userdata["vad"],
        userdata=call_session_data,
        turn_handling=production_turn_handling(interrupt_settings),
        llm=await build_production_agent_llm_async(
            call_session_data.call_config_data.llm_model
        ),
        stt=agent_stt,
        tts=await build_agent_tts_async(
            call_session_data.call_config_data.tts_model,
            language=call_session_data.call_config_data.language,
            voice=call_session_data.call_config_data.voice,
            speaking_rate=call_session_data.call_config_data.tts_speed,
            non_ko_google_voice=PRODUCTION_NON_KO_GOOGLE_VOICE,
        ),
        tts_text_transforms=production_tts_text_transforms(),
        max_tool_steps=PRODUCTION_MAX_TOOL_STEPS,
    )

    bind_agent_stt_session(agent_stt, session)

    register_session_event_handlers(
        session=session, call_session_data=call_session_data
    )
    bind_nc_session(session, nc_counters)
    interruption_tracker = InterruptionTracker(
        started_at=nc_state["started_at"],
        dump_origin=(lambda: nc_tee.first_frame_at) if nc_tee is not None else None,
        log_text=ENV in ("local", "dev", "qa"),
        context={
            "room_name": ctx.room.name,
            "nc_engine": nc_assignment.engine,
            "nc_model": nc_assignment.model,
            "min_duration": interrupt_settings.min_duration,
            "min_words": interrupt_settings.min_words,
        },
    )
    bind_interruption_tracker(session, interruption_tracker)
    nc_state["interruption_tracker"] = interruption_tracker

    logger.debug("********* PARTICIPANT_JOINED *********")
    call_session_data.call_config_data.call_start_time = kst_now_naive_iso()

    # NC on: subscribe caller audio through the ai-coustics FrameProcessor.
    # NC off: omit audio_input so the SDK defaults stay exactly as before.
    # auto_gain_control is pinned to True: the SDK default is "on" without a
    # processor but silently flips to "off" when one is given (room_io.py), which
    # would confound the A/B comparison.
    room_options_kwargs: dict[str, object] = {}
    if nc_input is not None:
        room_options_kwargs["audio_input"] = room_io.AudioInputOptions(
            noise_cancellation=nc_input,
            auto_gain_control=True,
        )

    await session.start(
        room=ctx.room,
        agent=call_session_data.agents_data.agents["single_agent"],
        room_options=room_io.RoomOptions(
            text_output=room_io.TextOutputOptions(
                sync_transcription=True,
            ),
            close_on_disconnect=True,
            delete_room_on_close=True,
            **room_options_kwargs,
        ),
    )

    # 추가: metrics 집계를 위한 agent_name 설정
    await ctx.room.local_participant.set_attributes(
        {"agent_name": os.environ.get("AGENT_NAME", "inbound-publisher")}
    )


@server.rtc_session(
    type=ServerType.ROOM,
    agent_name=agent_name,
)
async def entrypoint(ctx: JobContext):
    try:
        dial_info = json.loads(ctx.job.metadata)

    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON metadata: {e}")
        ctx.shutdown()
        return

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # Wait for participant to join
    participant = await ctx.wait_for_participant(
        kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP
    )
    logger.bind(
        event="PARTICIPANT_JOINED",
        participant_identity=participant.identity,
        participant_attributes=participant.attributes,
        room_name=ctx.room.name,
        num_participants=ctx.room.num_participants,
        num_publishers=ctx.room.num_publishers,
    ).debug("Participant joined")

    # Warm transfer self-call 가드:
    # 전화를 건 번호(sip.phoneNumber)가 인바운드 센터 번호(business_data.client_phone)와
    # 같으면, 이는 실제 고객이 아니라 warm transfer가 상담원(=자기 센터 번호)에게
    # 건 전화가 인바운드로 되돌아온 케이스다. 세션을 시작하지 않고 즉시 종료한다.
    caller_phone = participant.attributes.get("sip.phoneNumber")
    client_phone = (dial_info.get("business_data") or {}).get("client_phone")
    if (
        caller_phone
        and client_phone
        and _normalize_phone(caller_phone) == _normalize_phone(client_phone)
    ):
        logger.bind(
            event="WARM_TRANSFER_SELF_CALL_SKIP",
            caller_phone=caller_phone,
            client_phone=client_phone,
            participant_identity=participant.identity,
            room_name=ctx.room.name,
        ).warning(
            "Caller phone equals inbound client phone (warm transfer self-call); "
            "skipping session start and shutting down"
        )
        ctx.shutdown(reason="Warm transfer self-call detected")
        return

    if ctx.room.num_participants == 0:
        logger.bind(event="DIAL_INFO", dial_info=dial_info).debug(
            "********* DIAL_INFO *********"
        )

        # metadata에 recipient_data가 없거나 null인 경우 방어 (KeyError/TypeError 방지)
        if not dial_info.get("recipient_data"):
            dial_info["recipient_data"] = {}
        dial_info["recipient_data"]["sip_identity"] = participant.identity
        await agent_session_start(ctx, dial_info)
    else:
        logger.bind(
            event="HUMAN_AGENT_CONNECTED",
            participant_identity=participant.identity,
            room_name=ctx.room.name,
        ).success("Human agent connecting...")


if __name__ == "__main__":
    run_agent_app(server)
