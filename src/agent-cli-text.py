import os

from utils.entrypoint_bootstrap import (
    build_agents_data,
    configure_process_environment,
    kst_now_naive_iso,
)

configure_process_environment(
    dotenv_override=os.getenv("SIM_SUITE_PRESERVE_ENV") != "1"
)

from livekit.agents import (
    AgentServer,
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    cli,
    room_io,
)
from livekit.agents.worker import ServerType

from agents.single_agent import SingleAgent
from data import CallSessionData
from utils.agent_llm import build_agent_llm, resolve_agent_llm_model
from utils.call_end_event import publish_call_end_event
from utils.common import (
    load_dial_info_file,
    resolve_dial_info_path,
    write_transcript,
    write_transcript_events,
)
from utils.logging_utils import setup_json_logging
from utils.session_event_handlers import register_session_event_handlers
from utils.session_settings import (
    PRODUCTION_MAX_TOOL_STEPS,
    load_production_vad,
)

# Configure JSON logging with required fields
logger = setup_json_logging(level=os.getenv("LOG_LEVEL", "INFO"))


agent_name = os.getenv("AGENT_NAME", "service-center-demo-local")
livekit_url = os.getenv("LIVEKIT_URL", "ws://localhost:7880")

print("🚀 Starting inbound agent with:")
print(f"  - Agent Name: {agent_name}")
print(f"  - LiveKit URL: {livekit_url}")


server = AgentServer(
    load_threshold=0.65,
    # initialize_process_timeout=60.0,  # 10초 → 60초로 증가 (prewarm 시간 확보)
    # shutdown_process_timeout=30.0,  # 10초 → 30초로 증가 (shutdown callback 실행 시간 확보)
)


def prewarm(proc: JobProcess):
    print("*********** Prewarming agent server... *************")
    proc.userdata["vad"] = load_production_vad()


server.setup_fnc = prewarm


async def save_local_transcripts(
    ctx: JobContext,
    session: AgentSession[CallSessionData],
    *,
    reason: str,
) -> None:
    if getattr(session, "_local_transcripts_saved", False):
        return
    session._local_transcripts_saved = True

    logger.bind(
        event="LOCAL_TRANSCRIPT_SAVE",
        room_name=ctx.room.name,
        reason=reason,
    ).info("Saving local transcripts")

    await write_transcript(ctx.room.name, session.history)
    await write_transcript_events(session._recorded_events)
    # Write the call-end event to the local event log.
    call_config = session.userdata.call_config_data
    call_config.call_end_time = call_config.call_end_time or kst_now_naive_iso()
    await publish_call_end_event(session.userdata, session)


async def on_session_end(ctx: JobContext) -> None:
    await save_local_transcripts(
        ctx,
        ctx.primary_session,
        reason="session_end",
    )


async def agent_session_start(ctx: JobContext, dial_info: dict):
    call_session_data = CallSessionData.from_dial_info(ctx, dial_info)
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

        await save_local_transcripts(
            ctx,
            session,
            reason="job_shutdown",
        )

    ctx.add_shutdown_callback(shutdown_callback)

    llm_model = resolve_agent_llm_model(call_session_data.call_config_data.llm_model)
    logger.bind(agent_llm_model=llm_model).info("Configured agent LLM")

    session = AgentSession[CallSessionData](
        vad=ctx.proc.userdata["vad"],
        userdata=call_session_data,
        # Text console input has an explicit boundary when Enter is pressed, so
        # neither the cloud turn detector nor the local audio model is needed.
        turn_handling=TurnHandlingOptions(turn_detection="manual"),
        # AgentTask 기반 예약 태스크는 stage·commit을 락으로 직렬화하므로
        # 병렬 툴 호출을 끈다.
        # dial_info llm_model / AGENT_LLM_MODEL(--agent-model)을 존중한다 —
        # 고정 LLM을 꽂아두면 두 설정이 조용히 무시된다(groq/compound-mini 는
        # tool calling 미지원이라 SingleAgent 툴 호출에서 400으로 죽었다).
        # llm=groq.LLM(model="groq/compound-mini"),
        llm=build_agent_llm(
            llm_model,
            parallel_tool_calls=False,
        ),
        max_tool_steps=PRODUCTION_MAX_TOOL_STEPS,
    )

    register_session_event_handlers(
        session=session, call_session_data=call_session_data
    )

    logger.debug("********* PARTICIPANT_JOINED *********")
    call_session_data.call_config_data.call_start_time = kst_now_naive_iso()

    # Same AgentsData wiring as agent-server.py: flow nodes hand DTMF/agent-node
    # routing state to SingleAgent through it.
    call_session_data.agents_data = build_agents_data(
        single_agent=await SingleAgent.create(call_session_data=call_session_data),
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
        ),
    )

    # 추가: metrics 집계를 위한 agent_name 설정
    await ctx.room.local_participant.set_attributes({"agent_name": agent_name})


@server.rtc_session(
    type=ServerType.PUBLISHER,
    agent_name=agent_name,
    on_session_end=on_session_end,
)
async def entrypoint(ctx: JobContext):
    path = resolve_dial_info_path("dial_info.voice.ko.yaml")
    dial_info = load_dial_info_file(path=path)

    logger.info(f"Loaded dial_info from {path}")

    room = ctx.room

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    logger.info(f"Connected to room: {room.name}")

    dial_info.setdefault("recipient_data", {})["sip_identity"] = "demo-caller-0001"  # synthetic identity
    await agent_session_start(ctx, dial_info)


if __name__ == "__main__":
    cli.run_app(server)
