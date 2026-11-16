#!/usr/bin/env python3
"""고객 시뮬레이터 CLI.

인바운드 에이전트 텍스트 콘솔을 subprocess(PTY)로 띄우고,
내장 LLM이 페르소나에 맞는 고객 발화를 자동 생성한다.
사용자는 이 화면에서 실시간 관전하며 i/d/p/q 키로 개입할 수 있다.

사용법:
    uv run tools/customer-sim.py                     # 페르소나 대화형 입력
    uv run tools/customer-sim.py --persona-file p.yaml
    uv run tools/customer-sim.py --no-auto           # LLM 자동 응답 끄고 i키로만 입력
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import os
import pty
import queue
import re
import select
import shlex
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import tty
from collections import deque
from datetime import datetime
from pathlib import Path

import yaml
from customer_actor import (
    CustomerActor,
    ensure_model_api_key,
    pick_agent_message_for_reply,
    resolve_customer_model,
)
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from sim_suite.text_sanitizer import (
    CONSOLE_LOG_SUFFIX_RE,
    clean_agent_message,
    normalized_message_key,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(
    PROJECT_ROOT / ".env",
    override=os.getenv("SIM_SUITE_PRESERVE_ENV") != "1",
)

DEFAULT_AGENT_CMD = (
    f"{shlex.quote(sys.executable)} src/agent-cli-text.py console --text"
)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
DEFAULT_REPLY_DEBOUNCE_SEC = 3.0
GRACEFUL_SHUTDOWN_TIMEOUT_SEC = 15.0  # demo: session close + local exports take ~4 s

console = Console(highlight=False)


# ---------------------------------------------------------------- persona

PERSONA_QUESTIONS = [
    ("name", "고객 이름", "가상 고객"),
    ("verification_code", "데모 확인번호 6자리", "000101"),
    ("age_gender", "나이/성별", "38세 남성"),
    (
        "personality",
        "성격·말투 (예: 급하고 퉁명스러움, 존댓말 서툼)",
        "보통의 정중한 말투",
    ),
    (
        "goal",
        "전화 목적 (예: 휴대폰 점검 예약, 예약 변경, 비용 문의)",
        "휴대폰 액정 점검 예약",
    ),
    ("scenario", "특이 시나리오 (예: 중간에 마음 바꿈, 확인번호 입력 거부)", "없음"),
]


def ask_persona_interactively() -> dict:
    console.print(
        Panel("고객 페르소나를 입력하세요. (Enter = 기본값)", style="bold cyan")
    )
    persona = {}
    for key, label, default in PERSONA_QUESTIONS:
        answer = console.input(f"[cyan]{label}[/cyan] [dim]({default})[/dim]: ").strip()
        persona[key] = answer or default
    return persona


# ---------------------------------------------------------------- session


class Session:
    def __init__(self, root: Path, persona: dict):
        self.dir = root / datetime.now().strftime("%Y%m%d-%H%M%S")
        self.events_path = self.dir / "events.jsonl"
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "persona.yaml").write_text(
            yaml.safe_dump(persona, allow_unicode=True), encoding="utf-8"
        )
        self.events_path.touch()
        self.persona = persona
        self.paused = False
        self.actor_directions: list[str] = []

    def emit(self, type_: str, **fields):
        event = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "type": type_,
            **fields,
        }
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def transcript_lines(self) -> list[tuple[str, str]]:
        lines: list[tuple[str, str]] = []
        for raw in self.events_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event["type"] == "agent_msg":
                lines.append(("에이전트", event["content"]))
            elif event["type"] == "customer_msg":
                label = {
                    "auto": "고객(자동)",
                    "user": "고객(직접)",
                }.get(event.get("source", ""), "고객")
                lines.append((label, event["content"]))
        return lines


# ---------------------------------------------------------------- 출력 파싱


def _normalize_agent_content(content: str) -> str:
    # PTY 줄바꿈 지점에서 공백이 소실되거나(\n→붙음) 추가될 수 있어
    # 중복 비교는 공백을 전부 제거한 형태로 한다.
    return normalized_message_key(content)


class AgentOutputParser:
    """에이전트 콘솔 출력에서 대화 이벤트를 추출하는 라인 단위 상태 머신."""

    _AGENT_BLOCK_STOP_PREFIXES = ("●", "➜", "┌")
    _AGENT_BLOCK_STOP_MARKERS = ("Conversation Item Added",)
    _SESSION_END_MARKERS = (
        "shutting down job task",
        "session closed",
        "Agent Session closed",
    )
    _CONVERSATION_ITEM_FALLBACK = re.compile(
        r'"role"\s*:\s*"(?P<role>assistant|user)"\s*,\s*'
        r'"content"\s*:\s*"(?P<content>.*?)"\s*,\s*"interrupted"',
        re.DOTALL,
    )

    def __init__(self, on_event):
        self.on_event = on_event
        self._buf = ""
        self._collecting = False
        self._block_lines = []
        self._agent_collecting = False
        self._agent_lines: list[str] = []
        self._agent_turn = 0
        # JSON 파싱에서 emit한 최근 내용들 — 뒤따르는 ● Agent 표시와 중복 제거.
        # 에이전트가 발화를 연속으로 내면 JSON 여러 건이 표시보다 먼저 올 수 있어
        # 슬롯 하나가 아니라 최근 N건을 기억한다.
        self._recent_json_echoes: deque[str] = deque(maxlen=8)
        # │ 없이 줄바꿈된 JSON은 join("")로만 파싱됨 — ● Agent 표시를 우선한다
        self._compact_json_item: dict | None = None

    def feed(self, data: str):
        # 콘솔 상태바는 \n 없이 \r로만 다시 그리므로 \r도 라인 구분자로 취급해야
        # 로그 라인이 상태바 출력에 묻히지 않는다.
        self._buf += data
        parts = re.split(r"[\r\n]", self._buf)
        self._buf = parts.pop()
        for line in parts:
            self._handle_line(ANSI_RE.sub("", line))

    def flush(self):
        """PTY EOF 등으로 입력이 끊길 때 수집 중인 블록을 마무리한다."""
        if self._buf:
            self._handle_line(ANSI_RE.sub("", self._buf))
            self._buf = ""
        if self._collecting and self._block_lines:
            self._finish_block()
        if self._agent_collecting:
            self._finish_agent_block()
        elif self._compact_json_item:
            self._emit_compact_json_item()

    def _emit_agent_msg(self, content: str, *, source: str):
        # PTY 줄바꿈으로 생긴 개행·연속 공백을 정리해 발화를 한 줄로 기록한다.
        content = clean_agent_message(content)
        if not content:
            return
        normalized = _normalize_agent_content(content)
        if source == "display" and normalized in self._recent_json_echoes:
            self._recent_json_echoes.remove(normalized)
            return
        self._agent_turn += 1
        if source == "json":
            self._recent_json_echoes.append(normalized)
        self.on_event("agent_msg", content=content, turn=self._agent_turn)

    def _finish_agent_block(self):
        self._agent_collecting = False
        while self._agent_lines and self._agent_lines[-1].strip() == "✓":
            self._agent_lines.pop()
        if not self._agent_lines:
            return
        content = "\n".join(self._agent_lines).strip()
        self._agent_lines = []
        self._compact_json_item = None
        self._emit_agent_msg(content, source="display")

    def _should_stop_agent_block(self, stripped: str) -> bool:
        if "llm_ttft:" in stripped:
            return True
        if CONSOLE_LOG_SUFFIX_RE.search(f" {stripped}"):
            return True
        if any(
            stripped.startswith(prefix) for prefix in self._AGENT_BLOCK_STOP_PREFIXES
        ):
            return True
        return any(marker in stripped for marker in self._AGENT_BLOCK_STOP_MARKERS)

    def _handle_line(self, line: str):
        stripped = line.strip()
        if not stripped:
            if self._agent_collecting:
                self._agent_lines.append("")
            return

        if self._collecting:
            if stripped.startswith("└"):
                self._finish_block()
            elif stripped.startswith("│"):
                self._block_lines.append(stripped.lstrip("│"))
            elif self._block_lines:
                # PTY 폭을 넘겨 줄바꿈된 조각 — 직전 라인의 연속으로 이어붙인다
                self._block_lines[-1] += stripped
            # 블록 시작 직후 끼어든 상태바 등 잡음 라인은 무시하고 수집을 유지한다
            return

        if self._agent_collecting:
            if any(marker in stripped for marker in self._SESSION_END_MARKERS):
                self._finish_agent_block()
                self.on_event("session_end")
                return
            if self._should_stop_agent_block(stripped):
                self._finish_agent_block()
            else:
                self._agent_lines.append(stripped)
                return

        if "Conversation Item Added" in stripped:
            self._collecting = True
            self._block_lines = []
        elif stripped.startswith("● Agent"):
            self._finish_agent_block()
            self._agent_collecting = True
            self._agent_lines = []
        elif stripped.startswith("● Handoff:"):
            self.on_event("handoff", text=stripped.removeprefix("●").strip())
        elif stripped.startswith("➜"):
            self.on_event("tool", name=stripped.removeprefix("➜").strip())
        elif any(marker in stripped for marker in self._SESSION_END_MARKERS):
            self.flush()
            self.on_event("session_end")

    def _parse_json_block(self, block_lines: list[str]) -> tuple[dict | None, bool]:
        """JSON 블록 파싱. 두 번째 값은 join('') 폴백(compact) 사용 여부."""
        if not block_lines:
            return None, False
        for compact in (False, True):
            joiner = "" if compact else "\n"
            block = joiner.join(block_lines)
            start, end = block.find("{"), block.rfind("}")
            if start == -1 or end == -1:
                continue
            try:
                return json.loads(block[start : end + 1]), compact
            except json.JSONDecodeError:
                continue
        # content 필드가 PTY 줄바꿈으로 깨진 JSON — 공백 정규화 후 regex 폴백
        flat = " ".join(line.strip() for line in block_lines)
        if match := self._CONVERSATION_ITEM_FALLBACK.search(flat):
            return {
                "role": match.group("role"),
                "content": match.group("content"),
            }, False
        return None, False

    def _emit_compact_json_item(self):
        item = self._compact_json_item
        self._compact_json_item = None
        if not item:
            return
        content = item.get("content")
        if isinstance(content, list):
            content = " ".join(str(c) for c in content)
        if item.get("role") == "assistant" and content:
            self._emit_agent_msg(str(content).strip(), source="json")

    def _finish_block(self):
        self._collecting = False
        block_lines = self._block_lines
        self._block_lines = []
        item, compact = self._parse_json_block(block_lines)
        if not item:
            return
        if compact:
            self._compact_json_item = item
            return
        content = item.get("content")
        if isinstance(content, list):
            content = " ".join(str(c) for c in content)
        if item.get("role") == "assistant" and content:
            self._emit_agent_msg(str(content).strip(), source="json")


# ---------------------------------------------------------------- 렌더링


def render(event: dict):
    type_ = event["type"]
    ts = event.get("ts", "")[-8:]
    if type_ == "agent_msg":
        console.print(
            f"[dim]{ts}[/dim] [bold cyan]🤖 에이전트[/bold cyan]  {event['content']}"
        )
    elif type_ == "customer_msg":
        who = {
            "auto": "🧑 고객(자동)",
            "user": "🧑 고객(직접)",
        }.get(event.get("source"), "🧑 고객")
        style = "bold green" if event.get("source") == "auto" else "bold yellow"
        console.print(f"[dim]{ts}[/dim] [{style}]{who}[/{style}]  {event['content']}")
    elif type_ == "handoff":
        console.print(f"[dim]{ts}   ↪ {event['text']}[/dim]")
    elif type_ == "tool":
        console.print(f"[dim]{ts}   ⚙ {event['name']}[/dim]")
    elif type_ == "user_direction":
        console.print(
            f"[dim]{ts}[/dim] [magenta]📣 연기 지시: {event['content']}[/magenta]"
        )
    elif type_ == "control":
        label = (
            "⏸ 자동 응답 일시정지" if event["action"] == "pause" else "▶ 자동 응답 재개"
        )
        console.print(f"[bold red]{label}[/bold red]")
    elif type_ == "session_end":
        console.print("[bold red]── 에이전트 세션 종료 ──[/bold red]")


# ---------------------------------------------------------------- transcript


def write_transcript(session: Session):
    lines = [
        "# 고객 시뮬레이션 대화록",
        "",
        f"- 일시: {session.dir.name}",
        f"- 페르소나: {session.persona.get('name')} / {session.persona.get('goal')}",
        "",
        "## 대화",
        "",
    ]
    for raw in session.events_path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:
            continue
        t = e["type"]
        if t == "agent_msg":
            lines.append(f"**에이전트**: {e['content']}")
        elif t == "customer_msg":
            who = {
                "auto": "고객(자동)",
                "user": "고객(직접)",
            }.get(e.get("source"), "고객")
            lines.append(f"**{who}**: {e['content']}")
        elif t == "handoff":
            lines.append(f"> ↪ {e['text']}")
        elif t == "tool":
            lines.append(f"> ⚙ {e['name']}")
        elif t == "user_direction":
            lines.append(f"> 📣 지시: {e['content']}")
        lines.append("")
    path = session.dir / "transcript.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------- 메인 루프


class ScriptedCustomerActor:
    """Return deterministic customer replies for API-bound regression scenarios."""

    def __init__(self, replies: list[str]):
        self._replies = deque(str(reply) for reply in replies)

    def generate_reply(self, _agent_content: str) -> str | None:
        return self._replies.popleft() if self._replies else None


class Simulator:
    def __init__(
        self,
        session: Session,
        agent_cmd: str,
        auto: bool = True,
        customer_model: str | None = None,
        raw_log: bool = False,
        reply_debounce_sec: float = DEFAULT_REPLY_DEBOUNCE_SEC,
        dial_info_json: str | None = None,
        agent_model: str | None = None,
        stop_on_transfer: bool = False,
    ):
        self.session = session
        self.agent_cmd = agent_cmd
        self.auto = auto
        scripted_replies = session.persona.get("scripted_replies")
        self.customer_model = customer_model or resolve_customer_model()
        self.reply_debounce_sec = reply_debounce_sec
        self.dial_info_json = dial_info_json
        self.agent_model = agent_model or os.getenv("AGENT_LLM_MODEL")
        self.stop_on_transfer = stop_on_transfer
        self.actor = (
            ScriptedCustomerActor(scripted_replies)
            if auto and isinstance(scripted_replies, list)
            else CustomerActor(session, self.customer_model)
            if auto
            else None
        )
        self.proc = None
        self.master_fd = None
        self.parser = AgentOutputParser(self._on_agent_event)
        self.ended = False
        self._agent_session_end_seen = False
        self._agent_shutdown_requested = False
        self._shutdown_deadline: float | None = None
        self._stdin_attrs = None
        self._reply_queue: queue.Queue[str | Exception] = queue.Queue()
        self._debounce_lock = threading.Lock()
        self._debounce_batch: list[str] = []
        self._debounce_timer: threading.Timer | None = None
        # 고객 LLM 응답 생성 중 여부 — 생성 중 새 발화가 와도 동시에 두 번째
        # 생성을 시작하지 않고, 생성 완료 후 남은 배치로 재예약한다.
        self._generating = False
        # 파싱 실패 원인 추적용 원본 에이전트 출력 (상태바 갱신 포함이라 용량이 커서 기본 off)
        self._raw_log = (
            (session.dir / "agent_raw.log").open("a", encoding="utf-8")
            if raw_log
            else None
        )

    def start_agent(self):
        self.master_fd, slave_fd = pty.openpty()
        # 긴 로그 라인(예: 서비스 목록 안내)이 줄바꿈으로 쪼개지면 파서가 JSON을
        # 복원하지 못하므로 PTY 폭을 충분히 넓게 잡는다.
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 1000, 0, 0))
        env = dict(os.environ)
        if fixture_api := os.getenv("SIM_RESERVATION_API_BASEURL"):
            # sim-suite의 scenario-local fixture는 .env의 예약 API보다 우선한다.
            env["RESERVATION_API_BASEURL"] = fixture_api
        if self.dial_info_json:
            # .env의 DIAL_INFO_FILE이 DIAL_INFO_JSON보다 우선하므로 명시적
            # scenario fixture 실행에서는 반드시 제거한다.
            env.pop("DIAL_INFO_FILE", None)
            env["DIAL_INFO_JSON"] = self.dial_info_json
        if self.agent_model:
            env["AGENT_LLM_MODEL"] = self.agent_model
        self.proc = subprocess.Popen(
            shlex.split(self.agent_cmd),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=PROJECT_ROOT,
            start_new_session=True,
            env=env,
        )
        os.close(slave_fd)

    def send_to_agent(self, text: str) -> bool:
        if self.ended or self.master_fd is None:
            return False
        try:
            os.write(self.master_fd, (text.strip() + "\n").encode())
            return True
        except OSError:
            # 에이전트 PTY가 이미 닫힘 (세션 종료 직후 잔여 응답 등)
            return False

    def _send_customer(self, text: str, source: str):
        if not self.send_to_agent(text):
            console.print(f"[dim]세션 종료로 전송 생략: {text}[/dim]")
            return
        render(self.session.emit("customer_msg", source=source, content=text))

    def _on_agent_event(self, type_: str, **fields):
        if type_ == "session_end":
            if self._agent_session_end_seen:
                return
            self._agent_session_end_seen = True
        event = self.session.emit(type_, **fields)
        render(event)
        # 상담원 연결(transfer_call) 또는 LeaveMessageTask 진입은 통화의 실질
        # 종료 지점이다. 이후 에이전트가 메모/종료 루프를 길게 돌 수 있으므로,
        # 검증 목적이라면 여기서 바로 끊는다.
        if self.stop_on_transfer and (
            (type_ == "tool" and fields.get("name") == "transfer_call")
            or (type_ == "handoff" and "LeaveMessageTask" in fields.get("text", ""))
        ):
            console.print(
                "[bold yellow]── transfer_call 감지 → 자동 종료 "
                "(--stop-on-transfer) ──[/bold yellow]"
            )
            self._request_agent_shutdown()
            return
        if type_ == "session_end":
            self._shutdown_deadline = time.monotonic() + GRACEFUL_SHUTDOWN_TIMEOUT_SEC
        elif (
            type_ == "agent_msg"
            and self.auto
            and self.actor
            and not self.session.paused
        ):
            self._schedule_auto_reply(fields["content"])

    def _schedule_auto_reply(self, agent_content: str):
        # 에이전트가 연속으로 여러 줄(대기 안내 + 확인 질문 등)을 내면
        # reply_debounce_sec 동안 모았다가, 그중 답이 필요한 발화에만 반응한다.
        with self._debounce_lock:
            self._debounce_batch.append(agent_content)
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(
                self.reply_debounce_sec,
                self._flush_debounced_reply,
            )
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _flush_debounced_reply(self):
        with self._debounce_lock:
            self._debounce_timer = None
            if self._generating:
                # 진행 중인 생성이 끝나면 work()가 남은 배치로 재예약한다.
                # 여기서 두 번째 생성을 시작하면 첫 응답을 모르는 채 중복 답변이 나간다.
                return
            batch = self._debounce_batch
            self._debounce_batch = []
            target = pick_agent_message_for_reply(batch)
            if not target or not self.actor or self.session.paused:
                return
            self._generating = True

        def work():
            try:
                reply = self.actor.generate_reply(target)
                if reply:
                    self._reply_queue.put(reply)
            except Exception as exc:
                self._reply_queue.put(exc)
            finally:
                with self._debounce_lock:
                    self._generating = False
                    if self._debounce_batch and self._debounce_timer is None:
                        self._debounce_timer = threading.Timer(
                            self.reply_debounce_sec,
                            self._flush_debounced_reply,
                        )
                        self._debounce_timer.daemon = True
                        self._debounce_timer.start()

        threading.Thread(target=work, daemon=True).start()

    def _cancel_debounce_timer(self):
        with self._debounce_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
                self._debounce_timer = None
            self._debounce_batch = []

    def _flush_auto_replies(self):
        while True:
            try:
                item = self._reply_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, Exception):
                console.print(f"[bold red]고객 LLM 오류:[/bold red] {item}")
                continue
            if self.ended:
                continue
            if self.session.paused:
                console.print(f"[dim]일시정지로 자동 응답 폐기: {item}[/dim]")
                continue
            self._send_customer(item, source="auto")

    def _line_input(self, prompt: str) -> str:
        self._restore_stdin()
        try:
            return console.input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        finally:
            self._raw_stdin()

    def _handle_key(self, key: str):
        if key == "q":
            self._request_agent_shutdown()
        elif key == "i":
            text = self._line_input("[yellow]직접 고객 발화 입력[/yellow] > ")
            if text:
                self._send_customer(text, source="user")
        elif key == "d":
            text = self._line_input("[magenta]고객 연기 지시 추가[/magenta] > ")
            if text:
                self.session.actor_directions.append(text)
                render(self.session.emit("user_direction", content=text))
        elif key == "p":
            self.session.paused = not self.session.paused
            if self.session.paused:
                # 이미 걸려 있는 디바운스 배치도 함께 취소해야 즉시 멈춘다
                self._cancel_debounce_timer()
            render(
                self.session.emit(
                    "control",
                    action="pause" if self.session.paused else "resume",
                )
            )

    def _raw_stdin(self):
        if sys.stdin.isatty():
            self._stdin_attrs = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())

    def _restore_stdin(self):
        if self._stdin_attrs is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._stdin_attrs)

    def _request_agent_shutdown(self):
        if self._agent_shutdown_requested:
            return
        self._agent_shutdown_requested = True
        self._shutdown_deadline = time.monotonic() + GRACEFUL_SHUTDOWN_TIMEOUT_SEC
        if self.proc and self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.proc.pid, signal.SIGINT)
        else:
            self.ended = True

    def run(self):
        auto_label = (
            "자동 고객: [bold]scripted_replies[/bold]"
            if isinstance(self.actor, ScriptedCustomerActor)
            else f"자동 고객 LLM: [bold]{self.customer_model}[/bold]"
            if self.auto
            else "[dim]자동 고객 LLM: off (--no-auto)[/dim]"
        )
        agent_model_label = self.agent_model or "agent-cli-text.py default"
        console.print(
            Panel(
                f"세션: [bold]{self.session.dir}[/bold]\n"
                f"고객: {self.session.persona.get('name')} — {self.session.persona.get('goal')}\n"
                f"에이전트 LLM: [bold]{agent_model_label}[/bold]\n"
                f"{auto_label}\n\n"
                "[yellow]i[/yellow]=직접 발화  [magenta]d[/magenta]=연기 지시  "
                "[red]p[/red]=자동응답 일시정지  [red]q[/red]=종료",
                title="고객 시뮬레이터",
                style="green",
            )
        )
        self.start_agent()
        self._raw_stdin()
        try:
            while not self.ended:
                fds = [self.master_fd]
                if sys.stdin.isatty():
                    fds.append(sys.stdin.fileno())
                try:
                    readable, _, _ = select.select(fds, [], [], 0.2)
                except InterruptedError:
                    continue
                for fd in readable:
                    if fd == self.master_fd:
                        try:
                            data = os.read(self.master_fd, 65536)
                        except OSError as e:
                            if e.errno == errno.EIO:
                                data = b""
                            else:
                                raise
                        if not data:
                            self.parser.flush()
                            self._on_agent_event("session_end")
                            self.ended = True
                            break
                        text = data.decode("utf-8", errors="replace")
                        if self._raw_log:
                            self._raw_log.write(text)
                            self._raw_log.flush()
                        self.parser.feed(text)
                    else:
                        self._handle_key(os.read(fd, 1).decode(errors="ignore"))
                self._flush_auto_replies()
                if (
                    self._shutdown_deadline is not None
                    and time.monotonic() >= self._shutdown_deadline
                ):
                    self.ended = True
        except KeyboardInterrupt:
            self.session.emit("session_end")
        finally:
            self._restore_stdin()
            self._shutdown()

    def _shutdown(self):
        self._cancel_debounce_timer()
        if self.proc and self.proc.poll() is None:
            self._request_agent_shutdown()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                try:
                    self.proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, ProcessLookupError):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        if self._raw_log:
            with contextlib.suppress(Exception):
                self._raw_log.close()
        path = write_transcript(self.session)
        console.print(f"\n[bold]대화록 저장:[/bold] {path}")


def main():
    parser = argparse.ArgumentParser(description="고객 시뮬레이터 CLI")
    parser.add_argument(
        "--agent-cmd", default=DEFAULT_AGENT_CMD, help="에이전트 실행 커맨드"
    )
    parser.add_argument(
        "--persona-file", help="페르소나 YAML 파일 (생략 시 대화형 입력)"
    )
    parser.add_argument(
        "--sim-root",
        default=str(PROJECT_ROOT / "logs" / "simulations"),
        help="세션 저장 루트 디렉터리",
    )
    parser.add_argument(
        "--dial-info-json",
        help="agent-cli-text.py에 전달할 dial_info JSON 경로 (DIAL_INFO_JSON로 전달)",
    )
    parser.add_argument(
        "--agent-model",
        default=None,
        help=(
            "에이전트 LLM provider:model "
            "(AGENT_LLM_MODEL로 agent-cli-text.py에 전달)"
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help="고객 연기 LLM (기본: CUSTOMER_SIM_MODEL → OPENAI → ANTHROPIC 순)",
    )
    parser.add_argument(
        "--no-auto",
        action="store_true",
        help="LLM 자동 고객 응답 비활성화 (i키로만 입력)",
    )
    parser.add_argument(
        "--raw-log",
        action="store_true",
        help="에이전트 원본 출력을 세션 디렉터리 agent_raw.log에 저장 (디버깅용, 용량 큼)",
    )
    parser.add_argument(
        "--reply-debounce",
        type=float,
        default=DEFAULT_REPLY_DEBOUNCE_SEC,
        help="에이전트 연속 발화를 모아 고객 LLM에 넘기기 전 대기 초 (기본 3)",
    )
    parser.add_argument(
        "--stop-on-transfer",
        action="store_true",
        help="에이전트가 transfer_call(상담원 연결)/LeaveMessageTask에 도달하면 즉시 종료",
    )
    args = parser.parse_args()

    if args.persona_file:
        persona = yaml.safe_load(Path(args.persona_file).read_text(encoding="utf-8"))
    else:
        persona = ask_persona_interactively()

    scripted_replies = persona.get("scripted_replies")
    customer_model = resolve_customer_model(args.model)
    if not args.no_auto and not isinstance(scripted_replies, list):
        ensure_model_api_key(customer_model)

    session = Session(Path(args.sim_root), persona)
    Simulator(
        session,
        args.agent_cmd,
        auto=not args.no_auto,
        customer_model=customer_model,
        raw_log=args.raw_log,
        reply_debounce_sec=args.reply_debounce,
        dial_info_json=args.dial_info_json,
        agent_model=args.agent_model,
        stop_on_transfer=args.stop_on_transfer,
    ).run()


if __name__ == "__main__":
    main()
