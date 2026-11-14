"""Scenario-local HTTP fixtures for deterministic booking API simulations."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import yaml


class BookingApiFixtureServer:
    """Serve exact GET responses from YAML and record every request."""

    def __init__(self, fixture_path: Path, request_log_path: Path):
        self.fixture_path = fixture_path
        self.request_log_path = request_log_path
        self._lock = threading.Lock()
        self._requests: list[str] = []
        self._route_counts: dict[str, int] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

        data = yaml.safe_load(fixture_path.read_text(encoding="utf-8")) or {}
        routes = data.get("routes")
        if not isinstance(routes, dict) or not routes:
            raise ValueError(f"{fixture_path}: routes must be a non-empty mapping")
        # Resolve the single supported date token once so all responses in a run
        # share the same KST calendar day, including a run crossing midnight.
        tomorrow = (datetime.now(ZoneInfo("Asia/Seoul")) + timedelta(days=1)).date()
        self.routes = json.loads(
            json.dumps(routes, ensure_ascii=False).replace("${tomorrow}", str(tomorrow))
        )

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("fixture server is not running")
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    @property
    def requests(self) -> list[str]:
        with self._lock:
            return list(self._requests)

    def start(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                path = parsed.path
                request_line = f"GET {path}"
                with owner._lock:
                    owner._requests.append(request_line)
                    owner.request_log_path.parent.mkdir(parents=True, exist_ok=True)
                    with owner.request_log_path.open("a", encoding="utf-8") as log:
                        log.write(
                            json.dumps(
                                {
                                    "request": request_line,
                                    "query": parse_qs(parsed.query),
                                }
                            )
                            + "\n"
                        )

                route = owner.routes.get(path)
                if not isinstance(route, dict):
                    self._send_json(404, {"error": f"unconfigured route: {path}"})
                    return
                response = route
                sequence = route.get("sequence")
                if sequence is not None:
                    if not isinstance(sequence, list) or not sequence:
                        self._send_json(
                            500, {"error": f"invalid response sequence: {path}"}
                        )
                        return
                    with owner._lock:
                        index = owner._route_counts.get(path, 0)
                        owner._route_counts[path] = index + 1
                    response = sequence[min(index, len(sequence) - 1)]
                    if not isinstance(response, dict):
                        self._send_json(
                            500, {"error": f"invalid sequence item: {path}"}
                        )
                        return
                status = int(response.get("status", 200))
                self._send_json(status, response.get("json"))

            def _send_json(self, status: int, payload: object) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
