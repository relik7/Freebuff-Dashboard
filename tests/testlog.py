from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path

SUITE = "tests"

ROOT = Path(__file__).resolve().parent.parent

def log_dir() -> Path:
    root = os.environ.get("FB_DASHBOARD_TEST_LOG_DIR")
    base = Path(root) if root else ROOT / "logs"
    path = base / SUITE
    path.mkdir(parents=True, exist_ok=True)
    return path

def work_dir(name: str) -> Path:
    path = log_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path

class Tee:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.handle = open(self.path, "w", encoding="utf-8", newline="\n")

    def write(self, text: str) -> int:
        self.handle.write(text)
        self.handle.flush()
        try:
            sys.__stdout__.write(text)
            sys.__stdout__.flush()
        except Exception:
            pass
        return len(text)

    def flush(self) -> None:
        self.handle.flush()

    def close(self) -> None:
        try:
            self.handle.close()
        except Exception:
            pass

_active: Tee | None = None
_active_path: Path | None = None

_started_at: float | None = None

def start(name: str) -> Path:
    global _active, _active_path, _started_at
    path = log_dir() / f"{name}.log"
    _active = Tee(path)
    _active_path = path
    _started_at = time.monotonic()
    sys.stdout = _active
    sys.stderr = _active
    print(f"--- log: {path}")
    print(f"--- started {datetime.now():%Y-%m-%d %H:%M:%S}")
    return path

def child_log(name: str) -> Path:
    return log_dir() / f"{name}.out"

def elapsed() -> float:
    return (time.monotonic() - _started_at) if _started_at is not None else 0.0

def tail(path: str | Path, lines: int = 40) -> str:
    path = Path(path)
    if not path.exists():
        return f"(no log at {path})"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(cannot read {path}: {exc})"
    rows = text.splitlines()
    if len(rows) <= lines:
        return text.rstrip()
    return "\n".join([f"... {len(rows) - lines} earlier line(s) ..."]
                     + rows[-lines:])

def verdict(passed: bool, skipped: bool = False) -> str:
    return "skip" if skipped else "pass" if passed else "fail"

def finish(passed: bool, extras=(), lines: int = 40, skipped: bool = False) -> None:
    path = _active_path or Path("(no log was started)")
    seconds = (f"{time.monotonic() - _started_at:.1f}" if _started_at is not None
               else "?")
    print()

    print(f"--- elapsed: {seconds} s ({verdict(passed, skipped)})")
    print(f"--- log: {path}")
    for extra in extras:
        print(f"--- log: {extra}")
    if not passed:
        for extra in extras:
            print()
            print(f"--- tail of {extra} ---")
            for row in tail(extra, lines).splitlines():
                print(f"  {row}")
    if _active is not None:
        _active.close()
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
