"""Minimal real SDK entrypoint; no center services or model plugins."""

from services.agent_lifecycle import ExitCodeAgentServer, run_agent_app

server = ExitCodeAgentServer(
    num_idle_processes=0,
    max_retry=0,
    port=0,
)


@server.rtc_session(agent_name="lifecycle-test")
async def entrypoint(ctx):
    raise AssertionError("Lifecycle tests must never dispatch a job")


if __name__ == "__main__":
    run_agent_app(server)
