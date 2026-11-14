"""고아가 된 에이전트 콘솔 프로세스 수거.

`tools/customer-sim.py`는 에이전트를 `start_new_session=True`로 띄운다. 즉
에이전트는 자기만의 프로세스 그룹을 가진다. 러너가 타임아웃·조기정리 경로에서
customer-sim 그룹에 SIGINT → SIGKILL 을 보내면, 멈춰 있던 에이전트는 그 신호를
받지 않고 살아남고(PPID 1로 재부모화), SIGKILL 당한 customer-sim 은 더 이상
자식을 정리할 수 없다. 실제로 전체 스위트 실행 뒤 40분 넘은
`agent-cli-text.py console` 프로세스 25개가 남았다.

해결: 자식 환경변수에 `SIM_SUITE_RUN_TAG`를 심고(customer-sim 은 자신의 env 를
에이전트에게 그대로 물려준다), 런이 끝난 뒤 **그 태그를 가진** 프로세스만
골라 SIGKILL 한다. 태그가 없는 프로세스는 절대 건드리지 않는다 — 다른 스위트
실행이나 사람이 띄운 시뮬레이터일 수 있다.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Callable

RUN_TAG_ENV = "SIM_SUITE_RUN_TAG"
AGENT_COMMAND_MARKER = "agent-cli-text.py console"

# 정상 종료 중일 수 있으므로 죽이기 전에 이만큼 기다린다.
ORPHAN_GRACE_SEC = 10.0

EntryReader = Callable[[int], "str | None"]


def make_run_tag(out_dir_name: str, scenario_id: str, attempt: int) -> str:
    """이 시도(attempt)만 가리키는 고유 태그."""
    return f"{out_dir_name}/{scenario_id}/{attempt}"


def suite_tag_prefix(out_dir_name: str) -> str:
    """스위트 실행 전체를 가리키는 태그 접두사."""
    return f"{out_dir_name}/"


def matches_tag(entry: str, tag: str, *, prefix: bool = False) -> bool:
    """프로세스 항목(커맨드 + 환경변수)이 우리 태그를 가진 에이전트인지 판별한다.

    `entry` 는 `ps -E -o pid= -o command=` 한 줄이거나
    `/proc/<pid>/cmdline` + `environ` 을 이어 붙인 문자열이다.
    `prefix=False` 면 태그가 정확히 일치해야 한다(`.../1` 이 `.../10` 에
    걸리지 않도록 경계를 본다).
    """
    if not entry or AGENT_COMMAND_MARKER not in entry:
        return False
    needle = f"{RUN_TAG_ENV}={tag}"
    if prefix:
        return needle in entry
    return re.search(re.escape(needle) + r"(\s|$)", entry) is not None


def _read_entry_proc(pid: int) -> str | None:
    """Linux: /proc/<pid>/cmdline + environ."""
    base = Path(f"/proc/{pid}")
    try:
        cmdline = base.joinpath("cmdline").read_bytes()
        environ = base.joinpath("environ").read_bytes()
    except OSError:
        return None
    decoded = (cmdline + b" " + environ).replace(b"\x00", b" ")
    return decoded.decode("utf-8", errors="replace")


def _read_entry_ps(pid: int) -> str | None:
    """macOS: ps -E 는 커맨드 뒤에 환경변수를 붙여 준다."""
    try:
        completed = subprocess.run(
            ["ps", "-E", "-o", "pid=", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = completed.stdout.strip()
    return output or None


def read_process_entry(pid: int) -> str | None:
    """현재 OS에 맞게 프로세스의 커맨드 + 환경변수를 읽는다. 실패하면 None."""
    if Path(f"/proc/{pid}/environ").exists():
        entry = _read_entry_proc(pid)
        if entry:
            return entry
    return _read_entry_ps(pid)


def list_candidate_pids() -> list[int]:
    """에이전트 콘솔로 보이는 프로세스 pid 목록. 실패하면 빈 목록."""
    try:
        completed = subprocess.run(
            ["pgrep", "-f", AGENT_COMMAND_MARKER],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    own_pid = os.getpid()
    for line in completed.stdout.split():
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid and pid != own_pid:
            pids.append(pid)
    return pids


def find_orphans(
    tag: str,
    *,
    prefix: bool = False,
    pids: Iterable[int] | None = None,
    reader: EntryReader = read_process_entry,
) -> list[int]:
    """태그가 일치하는, 아직 살아 있는 에이전트 프로세스 pid 목록."""
    candidates = list(pids) if pids is not None else list_candidate_pids()
    matched: list[int] = []
    for pid in candidates:
        entry = reader(pid)
        if entry and matches_tag(entry, tag, prefix=prefix):
            matched.append(pid)
    return matched


def kill_pids(pids: Iterable[int]) -> list[int]:
    """SIGKILL 을 보내고 실제로 신호를 보낸 pid 를 돌려준다."""
    killed: list[int] = []
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
    return killed


def reap_orphans(
    tag: str,
    *,
    prefix: bool = False,
    grace_sec: float = ORPHAN_GRACE_SEC,
    sleep: Callable[[float], None] = time.sleep,
    finder: Callable[..., list[int]] = find_orphans,
    killer: Callable[[Iterable[int]], list[int]] = kill_pids,
) -> list[int]:
    """태그가 붙은 잔존 에이전트를 정리한다. 죽인 pid 목록을 돌려준다.

    남은 프로세스가 아예 없으면 기다리지 않고 바로 끝낸다. 하나라도 있으면
    정상 종료 중일 수 있으므로 `grace_sec` 만큼 기다렸다가 그래도 살아 있는
    것만 SIGKILL 한다.
    """
    if not finder(tag, prefix=prefix):
        return []
    if grace_sec > 0:
        sleep(grace_sec)
    remaining = finder(tag, prefix=prefix)
    if not remaining:
        return []
    return killer(remaining)
