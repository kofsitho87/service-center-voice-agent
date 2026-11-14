"""Verify fatal status classification through the real SDK CLI."""

import asyncio
import os
import signal
import socket
import sys
from pathlib import Path

import pytest
from aiohttp import web
from livekit.protocol import agent

from services.agent_lifecycle import ExitCodeAgentServer, run_agent_app


@pytest.fixture
async def fake_livekit():
    registrations = asyncio.Queue()
    sockets = []
    registration_gates = []

    async def connect(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sockets.append(ws)
        message = agent.WorkerMessage()
        message.ParseFromString(await ws.receive_bytes())
        assert message.HasField("register")
        allow_registration = asyncio.Event()
        registration_gates.append(allow_registration)
        await registrations.put((ws, allow_registration))
        await allow_registration.wait()
        reply = agent.ServerMessage()
        reply.register.worker_id = "test-worker"
        await ws.send_bytes(reply.SerializeToString())
        async for _ in ws:
            pass
        return ws

    app = web.Application()
    app.router.add_get("/agent", connect)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    try:
        yield f"ws://127.0.0.1:{runner.addresses[0][1]}", registrations
    finally:
        for gate in registration_gates:
            gate.set()
        for ws in sockets:
            await ws.close()
        await runner.cleanup()


def make_server(**kwargs):
    server = ExitCodeAgentServer(
        api_key="test-key",
        api_secret="test-secret-that-is-at-least-32-characters",
        num_idle_processes=0,
        port=0,
        **kwargs,
    )

    @server.rtc_session(agent_name="lifecycle-test")
    async def entrypoint(ctx):
        raise AssertionError("No jobs should be dispatched")

    return server


@pytest.mark.parametrize("code,expected", [(0, 1), (None, 1), (2, 2)])
def test_cli_preserves_existing_error_codes(monkeypatch, code, expected):
    server = make_server()

    def fake_cli(_):
        server.failed = True
        raise SystemExit(code)

    monkeypatch.setattr("services.agent_lifecycle.cli.run_app", fake_cli)
    with pytest.raises(SystemExit) as exc:
        run_agent_app(server)
    assert exc.value.code == expected


def cli_environment(url):
    # Do not forward real provider credentials or project .env values.
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        "LIVEKIT_API_KEY": "test-key",
        "LIVEKIT_API_SECRET": "test-secret-that-is-at-least-32-characters",
    }
    if url is not None:
        environment["LIVEKIT_URL"] = url
    return environment


async def launch_cli(url):
    return await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).parent / "fixtures/lifecycle_cli.py"),
        "start",
        env=cli_environment(url),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


async def test_real_cli_connection_failure_exits_three():
    # Reserve a local non-listening TCP port: no external network dependency.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        process = await launch_cli(f"ws://127.0.0.1:{sock.getsockname()[1]}")
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 30)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    assert process.returncode == 3, output.decode()
    assert b"failed to connect to livekit" in output
    assert b"draining worker" in output


async def test_real_cli_sigterm_exits_zero(fake_livekit):
    url, registrations = fake_livekit
    process = await launch_cli(url)
    try:
        _, register = await asyncio.wait_for(registrations.get(), 20)
        register.set()
        # SDK drain must also work if the signal races with registration.
        process.send_signal(signal.SIGTERM)
        output, _ = await asyncio.wait_for(process.communicate(), 20)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, output.decode()
    assert b"draining worker" in output


async def test_real_cli_missing_url_exits_one():
    process = await launch_cli(None)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 30)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 1, output.decode()
    assert b"LIVEKIT_URL" in output


@pytest.mark.parametrize(
    "error", [None, SystemExit(0), SystemExit(2), RuntimeError("cleanup failed")]
)
def test_connection_failure_has_priority(monkeypatch, error):
    server = make_server()

    def fake_cli(_):
        server._connection_failed = True
        server.failed = True
        if error is not None:
            raise error

    monkeypatch.setattr("services.agent_lifecycle.cli.run_app", fake_cli)
    with pytest.raises(SystemExit) as exc:
        run_agent_app(server)
    assert exc.value.code == 3


def test_swallowed_general_failure_exits_one(monkeypatch):
    server = make_server()

    def fake_cli(_):
        server.failed = True

    monkeypatch.setattr("services.agent_lifecycle.cli.run_app", fake_cli)
    with pytest.raises(SystemExit) as exc:
        run_agent_app(server)
    assert exc.value.code == 1
