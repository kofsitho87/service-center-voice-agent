import asyncio
import os

from utils.common import (
    load_dial_info_file,
    resolve_dial_info_path,
    write_transcript,
    write_transcript_events,
)
from utils.entrypoint_bootstrap import (
    build_agents_data,
    configure_process_environment,
    kst_now_naive_iso,
    load_supplement_prompt,
)

configure_process_environment(dotenv_override=False)

from livekit.agents import (
    AgentServer,
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    cli,
    room_io,
)
from livekit.agents.types import NOT_GIVEN
from livekit.agents.worker import ServerType

from agents.single_agent import SingleAgent
from data import CallSessionData
from utils.agent_llm import build_agent_llm_async, resolve_agent_llm_model
from utils.agent_stt import (
    bind_agent_stt_session,
    build_agent_stt,
    resolve_agent_stt_model,
)
from utils.agent_tts import build_agent_tts_async
from utils.call_end_event import publish_call_end_event
from utils.logging_utils import setup_json_logging
from utils.session_event_handlers import register_session_event_handlers
from utils.session_settings import (
    PRODUCTION_MAX_TOOL_STEPS,
    PRODUCTION_NON_KO_GOOGLE_VOICE,
    PRODUCTION_STT_CUSTOM_VOCABULARY,
    PRODUCTION_USER_AWAY_TIMEOUT,
    load_production_vad,
    production_tts_text_transforms,
    production_turn_handling,
    resolve_production_interruption_settings,
)

# Configure JSON logging with required fields
logger = setup_json_logging(level=os.getenv("LOG_LEVEL", "INFO"))


def load_dev_dial_info(path: str | None = None) -> dict:
    """DEV 모드용 dial_info. 기본: src/dial_info/ 디렉터리의 파일."""
    path = path or resolve_dial_info_path("dial_info.voice.ko.yaml")
    return load_dial_info_file(path)


agent_name = os.getenv("AGENT_NAME", "service-center-demo-local")
livekit_url = os.getenv("LIVEKIT_URL", "ws://localhost:7880")

print("🚀 Starting inbound agent with:")
print(f"  - Agent Name: {agent_name}")
print("  - LiveKit URL: configured")


server = AgentServer(
    load_threshold=0.65,
    # initialize_process_timeout=60.0,  # 10초 → 60초로 증가 (prewarm 시간 확보)
    # shutdown_process_timeout=30.0,  # 10초 → 30초로 증가 (shutdown callback 실행 시간 확보)
)


def prewarm(proc: JobProcess):
    print("*********** Prewarming agent server... *************")
    proc.userdata["vad"] = load_production_vad()


server.setup_fnc = prewarm


async def agent_session_start(ctx: JobContext, dial_info: dict):
    print("*********** Agent session start... *************")

    background_tasks: set[asyncio.Task] = set()

    def track_background_task(coro):
        task = asyncio.create_task(coro)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    call_session_data = CallSessionData.from_dial_info(ctx, dial_info)
    track_background_task(load_supplement_prompt(call_session_data))
    call_session_data.agents_data = build_agents_data(
        single_agent=await SingleAgent.create(call_session_data=call_session_data),
    )
    session: AgentSession[CallSessionData] | None = None

    ####################### shutdown callback function #######################
    async def shutdown_callback():
        logger.bind(
            event="SHUTDOWN_CALLBACK",
            room_name=ctx.room.name,
            phone=call_session_data.recipient_data.phone,
        ).info("********* Shutdown callback *********")

        call_session_data.call_config_data.call_end_time = kst_now_naive_iso()

        logger.bind(
            event="MESSAGE_RESULT",
            room_name=ctx.room.name,
            phone=call_session_data.recipient_data.phone,
            call_session_data=call_session_data.to_safe_dict(),
        ).info("********* SEND_MESSAGE_RESULT *********")

        if session is None:
            logger.bind(
                event="SHUTDOWN_SESSION_UNAVAILABLE",
                room_name=ctx.room.name,
            ).warning("Skipping transcript export: AgentSession was not created")
            return

        track_background_task(write_transcript(ctx.room.name, session.history))
        track_background_task(write_transcript_events(session._recorded_events))
        # Publish the call-end event for the local post-call consumer.
        track_background_task(publish_call_end_event(call_session_data, session))

    ctx.add_shutdown_callback(shutdown_callback)

    stt_model = resolve_agent_stt_model(call_session_data.call_config_data.stt_model)
    logger.bind(agent_stt_model=stt_model).info("Configured agent STT")
    agent_stt = build_agent_stt(
        stt_model,
        language=call_session_data.call_config_data.language,
        vad=ctx.proc.userdata["vad"],
        custom_vocabulary=list(PRODUCTION_STT_CUSTOM_VOCABULARY),
    )

    llm_model = resolve_agent_llm_model(call_session_data.call_config_data.llm_model)
    logger.bind(agent_llm_model=llm_model).info("Configured agent LLM")
    # AgentTask 기반 예약 설계(-claude)는 stage·commit을 락으로 직렬화하므로
    # 병렬 툴 호출을 끈다.
    llm = await build_agent_llm_async(
        llm_model,
        parallel_tool_calls=False,
    )

    interrupt_settings = resolve_production_interruption_settings()
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
        stt=agent_stt,
        tts=await build_agent_tts_async(
            call_session_data.call_config_data.tts_model,
            language=call_session_data.call_config_data.language,
            voice=call_session_data.call_config_data.voice,
            speaking_rate=call_session_data.call_config_data.tts_speed,
            non_ko_google_voice=PRODUCTION_NON_KO_GOOGLE_VOICE,
        ),
        tts_text_transforms=production_tts_text_transforms(),
        llm=llm,
        user_away_timeout=PRODUCTION_USER_AWAY_TIMEOUT,
        max_tool_steps=PRODUCTION_MAX_TOOL_STEPS,
    )

    bind_agent_stt_session(agent_stt, session)

    register_session_event_handlers(
        session=session, call_session_data=call_session_data
    )

    logger.debug("********* PARTICIPANT_JOINED *********")
    call_session_data.call_config_data.call_start_time = kst_now_naive_iso()

    await session.start(
        room=ctx.room,
        agent=call_session_data.agents_data.agents["single_agent"],
        room_options=room_io.RoomOptions(
            participant_identity=(
                os.getenv("TEST_CALLER_IDENTITY", "").strip() or NOT_GIVEN
            ),
            text_output=room_io.TextOutputOptions(
                sync_transcription=True,
            ),
            close_on_disconnect=True,
            delete_room_on_close=True,
        ),
    )

    # 추가: metrics 집계를 위한 agent_name 설정
    await ctx.room.local_participant.set_attributes({"agent_name": agent_name})


@server.rtc_session(
    type=ServerType.PUBLISHER,
    agent_name=agent_name,
)
async def entrypoint(ctx: JobContext):
    print("*********** Entrypoint... *************")

    dial_info_path = resolve_dial_info_path("dial_info.voice.ko.yaml")
    logger.bind(dial_info_path=dial_info_path).info(
        "Loading sample dial_info from file"
    )
    dial_info = load_dev_dial_info(dial_info_path)

    room = ctx.room

    caller_identity = os.getenv("TEST_CALLER_IDENTITY", "").strip()
    if not caller_identity:
        raise ValueError("Local Room tests require TEST_CALLER_IDENTITY")
    dial_info.setdefault("recipient_data", {})["sip_identity"] = caller_identity
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    logger.info(f"Connected to room: {room.name}")
    await agent_session_start(ctx, dial_info)


if __name__ == "__main__":
    cli.run_app(server)
