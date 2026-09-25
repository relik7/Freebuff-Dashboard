from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import testlog

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

POLL_S = 0.1

TOOL_PORT = 8770

_OURS = threading.local()

_EVERYONE: list[subprocess.Popen] = []

def free_port(*, avoid: tuple[int, ...] = (TOOL_PORT,)) -> int:
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port not in avoid:
            return port
    raise RuntimeError("no free port found")

def start_child(name: str, args: list[str], *, root: Path = ROOT,
                env: dict | None = None) -> subprocess.Popen:
    out = open(testlog.child_log(name), "w", encoding="utf-8")
    child = subprocess.Popen(
        [PYTHON, *args], cwd=str(root), stdout=out, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})},
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    children = getattr(_OURS, "children", None)
    if children is None:
        children = _OURS.children = []
    children.append(child)
    _EVERYONE.append(child)
    return child

def reap_survivors(timeout: float = 5.0, *, everything: bool = False) -> list[str]:
    mine = _EVERYONE if everything else getattr(_OURS, "children", [])
    ended = []
    for child in mine:
        if child.poll() is not None:
            continue
        ended.append(_describe(child))
        child.terminate()
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            child.kill()
    if not everything:
        _OURS.children = []
    if ended:
        print(f"    reaped {len(ended)} child process(es) still running: "
              f"{', '.join(ended)}")
    return ended

def _describe(child: subprocess.Popen) -> str:
    parts = [part for part in (child.args or []) if isinstance(part, str)]
    script = next((p for p in reversed(parts) if p.endswith(".py")), "?")
    return f"{Path(script).name}(pid {child.pid})"

def wait_for(process: subprocess.Popen, label: str, timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        print(f"    {label} did not finish within {timeout:.0f} s; terminating")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        return False

def wait_for_log(log: Path, needle: str, timeout: float,
                 *, poll: float = POLL_S) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if needle in log.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)

def _load(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

def wait_for_json(path: Path, timeout: float, *,
                  poll: float = POLL_S) -> dict | None:
    deadline = time.monotonic() + timeout
    while True:
        loaded = _load(path)
        if isinstance(loaded, dict):
            return loaded
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)

def wait_for_all(artifacts: dict[str, Path], timeout: float, *,
                 poll: float = POLL_S) -> tuple[bool, list[str]]:
    deadline = time.monotonic() + timeout
    missing = list(artifacts)
    while missing:
        missing = [name for name in missing
                   if wait_for_json(artifacts[name], 0) is None]
        if not missing or time.monotonic() >= deadline:
            break
        time.sleep(poll)
    return not missing, missing

def stop_child(process: subprocess.Popen, *, timeout: float = 10.0,
               label: str = "the child") -> bool:
    if process.poll() is None:
        process.terminate()
    if wait_for(process, label, timeout):
        return process.returncode == 0
    print(f"    {label} ignored the stop signal; it was ended, not stopped")
    return False

@contextmanager
def scenario(name: str):
    started = time.monotonic()
    try:
        yield
    finally:
        print(f"--- scenario {name}: {time.monotonic() - started:.1f} s")
