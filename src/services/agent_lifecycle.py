"""Preserve fatal exit status after livekit-agents 1.8.0 CLI cleanup."""

from livekit.agents import AgentServer, cli


class ExitCodeAgentServer(AgentServer):
    """Record errors that the SDK CLI would otherwise log and swallow."""

    failed = False

    async def run(self, *, devmode: bool = False, unregistered: bool = False) -> None:
        self.failed = False
        try:
            await super().run(devmode=devmode, unregistered=unregistered)
        except Exception:
            self.failed = True
            raise


def run_agent_app(server: ExitCodeAgentServer) -> None:
    """Allow SDK cleanup, then distinguish exhausted connections from other errors."""
    try:
        try:
            cli.run_app(server)
        except SystemExit as exc:
            # Typer can report success after the SDK swallowed a run() failure.
            if server.failed and exc.code in (None, 0):
                raise SystemExit(1) from None
            raise
        if server.failed:
            raise SystemExit(1)
    finally:
        # SDK 1.8.0 sets this only after exhausting its connection retries.
        # Keep this signal even if the CLI's subsequent cleanup also raises.
        if server._connection_failed:
            raise SystemExit(3) from None
