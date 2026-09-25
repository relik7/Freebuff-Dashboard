from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stamps
import suites
import testlog

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

SLACK = 1.5

MAX_SUITES = 3

DEFAULT_THREADS = 4

CHILD_ENV = {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
             "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}

RUN_LOCK = Path(tempfile.gettempdir()) / "fb-dashboard-tests" / "run.lock"

WAIT_S = 80.0
POLL_S = 0.25

LOCK_AT = 4096
HEAD_BYTES = 256
BINARY = getattr(os, "O_BINARY", 0)

@dataclass
class Outcome:
    name: str
    seconds: float
    verdict: str
    budget: float
    out: Path
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict == "pass"

def lock_path() -> Path:
    override = os.environ.get("FB_DASHBOARD_RUN_LOCK")
    if override:
        path = Path(override)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    RUN_LOCK.parent.mkdir(parents=True, exist_ok=True)
    return RUN_LOCK

def lock_holder(path: Path) -> str:
    try:
        head = path.read_bytes()[:HEAD_BYTES].decode("utf-8", "replace")
    except OSError:
        return ""
    return head.split("\x00", 1)[0].strip()

def _try_lock(fd: int) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, LOCK_AT, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True

def claim_run(wait_s: float = WAIT_S) -> int | None:
    path = lock_path()
    fd = os.open(path, os.O_RDWR | os.O_CREAT | BINARY, 0o644)
    if _try_lock(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        note = (f"pid {os.getpid()} started {datetime.now():%Y-%m-%d %H:%M:%S} "
                f"({ROOT.name})")
        os.write(fd, note.encode("utf-8")[:HEAD_BYTES - 1] + b"\x00")
        return fd
    holder = lock_holder(path) or "another run"
    print(f"run_tests: waiting up to {wait_s:.0f} s for {holder} to finish "
          f"(one run at a time on this machine)")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(POLL_S)
        if _try_lock(fd):
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, (f"pid {os.getpid()} started "
                          f"{datetime.now():%Y-%m-%d %H:%M:%S} ({ROOT.name})"
                          ).encode("utf-8")[:HEAD_BYTES - 1] + b"\x00")
            print("run_tests: the lock is free; running")
            return fd
    os.close(fd)
    print(f"run_tests: the run lock was seized by {holder}; nothing was run. "
          f"Re-run when it finishes, or look for a stuck gate in its log.")
    return None

def release_run(fd: int | None) -> None:
    if fd is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, LOCK_AT, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass

def one(suite: suites.Suite, *, quiet: bool = False,
        slack: float = SLACK) -> Outcome:
    out = testlog.child_log(suite.name)
    log = testlog.log_dir() / f"{suite.name}.log"
    limit = suite.budget_s * slack if suite.budget_s else None
    started = time.monotonic()
    with open(out, "w", encoding="utf-8") as handle:
        child = subprocess.Popen(
            [PYTHON, str(suite.path)], cwd=str(ROOT), stdout=handle,
            stderr=subprocess.STDOUT, env={**os.environ, **CHILD_ENV},
            creationflags=(subprocess.CREATE_NO_WINDOW
                           if sys.platform == "win32" else 0),
        )
        note = ""
        try:
            child.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            note = f"ended at its budget ({limit:.1f} s)" if limit else "ended on timeout"
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
    seconds = time.monotonic() - started
    passed = child.returncode == 0 and not note
    verdict = testlog.verdict(passed)
    stamps.record(suite.name, seconds, verdict=verdict)
    if not quiet:
        print(f"  [{verdict:4}] {suite.name:26} {seconds:6.1f} s "
              f"(budget {suite.budget_s:.1f})  {out}")
    return Outcome(suite.name, seconds, verdict, suite.budget_s, out, note)

def run(plan: list[suites.Suite], *, threads: int, quiet: bool = False,
        slack: float = SLACK) -> list[Outcome]:
    queue = sorted(plan, key=lambda s: -stamps.forecast(s))
    outcomes: list[Outcome] = []
    guard = threading.Lock()

    def worker() -> None:
        while True:
            with guard:
                if not queue:
                    return
                suite = queue.pop(0)
            outcome = one(suite, quiet=quiet, slack=slack)
            with guard:
                outcomes.append(outcome)

    workers = [threading.Thread(target=worker, name="gate", daemon=True)
               for _ in range(max(1, min(threads, len(queue))))]
    for worker_thread in workers:
        worker_thread.start()
    for worker_thread in workers:
        worker_thread.join()
    return outcomes

def markdown_row(outcomes: list[Outcome], *, threads: int) -> str:
    lines = ["| gate | elapsed | budget | verdict |", "|---|---:|---:|---|"]
    for outcome in sorted(outcomes, key=lambda o: -o.seconds):
        lines.append(f"| `{outcome.name}` | {outcome.seconds:.1f} s | "
                     f"{outcome.budget:.1f} s | {outcome.verdict} |")
    total = sum(o.seconds for o in outcomes)
    wall = max((o.seconds for o in outcomes), default=0.0)
    lines.append(f"| **{len(outcomes)} gate(s), {threads} thread(s)** | {total:.1f} s "
                 f"| wall {wall:.1f} s | "
                 f"{'pass' if all(o.ok for o in outcomes) else 'fail'} |")
    return "\n".join(lines)

def describe_plan(plan: list[suites.Suite]) -> str:
    lines = [f"{'gate':28} {'budget':>8} {'forecast':>9}  last pass"]
    for suite in sorted(plan, key=lambda s: -stamps.forecast(s)):
        remembered = stamps.recent(suite.name)
        last = f"{remembered[-1]:.1f} s" if remembered else "(no run yet)"
        lines.append(f"{suite.name:28} {suite.budget_s:8.1f} "
                     f"{stamps.forecast(suite):9.1f}  {last}")
    return "\n".join(lines)

def _force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="run_tests.py",
        description="Run the gates (no arguments prints the plan and runs nothing).")
    parser.add_argument("--full", action="store_true",
                        help="run every gate")
    parser.add_argument("--suites", nargs="+", metavar="NAME",
                        help=f"run fewer than {MAX_SUITES + 1} named gates, alone")
    parser.add_argument("--threads", type=int, default=DEFAULT_THREADS,
                        help=f"pool size for --full (default {DEFAULT_THREADS})")
    parser.add_argument("--list", action="store_true",
                        help="print the gate names, one per line, and exit")
    parser.add_argument("--markdown", action="store_true",
                        help="print this run's costs as a Markdown table")
    parser.add_argument("--slack", type=float, default=SLACK, metavar="X",
                        help=f"kill a gate at its declared budget times X "
                             f"(default {SLACK}). Raise it where the filesystem "
                             f"is slower than the machine the declarations were "
                             f"measured on -- WSL over /mnt -- and pair it with "
                             f"FB_DASHBOARD_TEST_LOG_DIR so the slower machine's "
                             f"costs are not recorded beside the faster one's")
    args = parser.parse_args(argv)

    plan = suites.discover()
    if not plan:
        print("run_tests: no gates found in tests/ -- a test_*.py file is a gate")
        return 1

    if args.list and not (args.full or args.suites):
        for suite in plan:
            print(suite.name)
        return 0

    if args.suites:
        if len(args.suites) > MAX_SUITES:
            print(f"run_tests: --suites takes at most {MAX_SUITES} gates "
                  f"({len(args.suites)} given); use --full for everything")
            return 2
        try:
            chosen = [suites.by_name(name) for name in args.suites]
        except suites.UnknownSuite as exc:
            print(f"run_tests: no gate named {exc} in tests/")
            return 2
        threads = 1
    elif args.full:
        chosen = plan
        threads = max(1, args.threads)
    else:
        print(f"=== the plan ({len(plan)} gate(s)) ===")
        print(describe_plan(plan))
        print()
        print("nothing was run: pass --full for every gate, or "
              "--suites NAME [NAME ...] for one or two")
        return 0

    lock = claim_run()
    if lock is None:
        return 1
    started = time.monotonic()
    try:
        print(f"=== {len(chosen)} gate(s) on {threads} thread(s) ===")
        if args.slack != SLACK:
            print(f"run_tests: every budget scaled by {args.slack:g} for this run")
        outcomes = run(chosen, threads=threads, slack=args.slack)
    finally:
        release_run(lock)

    seconds = time.monotonic() - started
    failed = [o for o in outcomes if not o.ok]
    print()
    print(f"run_tests: {len(outcomes) - len(failed)} passed, {len(failed)} failed "
          f"in {seconds:.1f} s")
    for outcome in sorted(outcomes, key=lambda o: o.name):
        print(f"run_tests: {outcome.name} -- {outcome.verdict} "
              f"({outcome.seconds:.1f} s), out {outcome.out}, "
              f"log {testlog.log_dir() / (outcome.name + '.log')}")
        if outcome.note:
            print(f"           {outcome.note}")
    for outcome in failed:
        print()
        print(f"--- {outcome.name}: last 40 lines of {outcome.out} ---")
        for row in testlog.tail(outcome.out, 40).splitlines():
            print(f"  {row}")
    if args.markdown:
        print()
        print(markdown_row(outcomes, threads=threads))
    if failed:
        print()
        print(f"run_tests: FAIL -- {', '.join(o.name for o in failed)}")
        return 1
    print("run_tests: all gates passed")
    return 0

if __name__ == "__main__":
    _force_utf8_output()
    sys.exit(main())
