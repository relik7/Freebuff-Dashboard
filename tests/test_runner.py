from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_tests
import stamps
import suites
import testlog

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent

failures: list[str] = []

def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {label}")
    else:
        failures.append(label)
        print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))

def capture(action) -> tuple[int, str]:
    buffer = StringIO()
    with redirect_stdout(buffer):
        code = action()
    return code, buffer.getvalue()

def fake_gate(directory: Path, name: str, body: str, *, budget_s: float = 30.0,
              stamp_s: float = 0.0) -> suites.Suite:
    path = directory / f"{name}.py"
    path.write_text("import sys, time\n" + body, encoding="utf-8", newline="\n")
    return suites.Suite(name=name, path=path, budget_s=budget_s, stamp_s=stamp_s)

def live_body(folder: Path, name: str, seconds: float = 0.4) -> str:
    trace = folder / f"lives-{name}.txt"
    return (f"p, seconds = {str(trace)!r}, {seconds!r}\n"
            "with open(p, 'a') as fh:\n"
            "    fh.write('start ' + repr(time.time()) + chr(10))\n"
            "time.sleep(seconds)\n"
            "with open(p, 'a') as fh:\n"
            "    fh.write('end ' + repr(time.time()) + chr(10))\n")

def lives(folder: Path) -> dict[str, list[float]]:
    found: dict[str, list[float]] = {}
    for path in sorted(folder.glob("lives-*.txt")):
        name = path.stem.split("-", 1)[1]
        found[name] = [float(line.split()[1])
                       for line in path.read_text(encoding="utf-8").splitlines()
                       if line.split()]
    return found

def overlapping(folder: Path) -> bool:
    spans = lives(folder)
    if len(spans) != 2 or any(len(times) != 2 for times in spans.values()):
        return True
    ordered = sorted((times[0], times[1]) for times in spans.values())
    return any(ordered[index][1] > ordered[index + 1][0]
               for index in range(len(ordered) - 1))

def hold_lock(seconds: float) -> tuple[subprocess.Popen, int]:
    body = ("import os, sys, time\n"
            f"sys.path.insert(0, {str(TESTS)!r})\n"
            "import run_tests\n"
            "assert run_tests.claim_run(wait_s=0.0) is not None, 'could not claim'\n"
            "print('held', os.getpid(), flush=True)\n"
            "time.sleep(float(sys.argv[1]))\n")
    holder = subprocess.Popen([sys.executable, "-c", body, str(seconds)],
                              cwd=str(ROOT), stdout=subprocess.PIPE, text=True,
                              env=dict(os.environ))
    word, pid = holder.stdout.readline().split()
    assert word == "held", "the holder never claimed"
    return holder, int(pid)

def main() -> int:
    print("=== the runner: the CLI, alone, the budget, the memory, the lock ===")
    tmp = Path(tempfile.mkdtemp(prefix="fb-dashboard-runner-"))
    os.environ["FB_DASHBOARD_TEST_LOG_DIR"] = str(tmp)
    os.environ["FB_DASHBOARD_RUN_LOCK"] = str(tmp / "run.lock")

    code, printed = capture(lambda: run_tests.main([]))
    check("a bare run prints the plan and exits 0", code == 0, str(code))
    check("the plan names the gates", "test_runner" in printed, printed[:200])
    check("the plan teaches --full and --suites, so neither is a secret",
          "--full" in printed and "--suites" in printed, printed[-300:])
    check("...and says it ran nothing",
          "nothing was run" in printed, printed[-200:])
    check("a bare run wrote no log and no memory (nothing ran)",
          not list(tmp.rglob("*.out")) and not stamps.memory_path().exists(),
          f"{[str(p) for p in tmp.rglob('*.out')]}")

    code, listed = capture(lambda: run_tests.main(["--list"]))
    check("--list names the gates, one per line, and exits 0",
          code == 0 and all(suite.name in listed.split() for suite in suites.discover()))
    code, message = capture(lambda: run_tests.main(["--suites"] + [
        "test_tree", "test_stamps", "test_runner", "test_api"]))
    check(f"--suites refuses more than {run_tests.MAX_SUITES} names (exit 2, "
          "nothing run)", code == 2 and "most" in message, message.strip()[:120])
    code, message = capture(lambda: run_tests.main(["--suites", "test_nonsense"]))
    check("--suites refuses a name no gate has",
          code == 2 and "test_nonsense" in message, message.strip()[:120])

    records = tmp / "lives"
    records.mkdir()
    one_gate = fake_gate(tmp, "test_fake_a", live_body(records, "a"))
    other_gate = fake_gate(tmp, "test_fake_b", live_body(records, "b"))
    run_tests.run([one_gate, other_gate], threads=1, quiet=True)
    check("--suites runs its gates alone (one thread, so their recorded lives "
          "never intersect)", not overlapping(records), str(lives(records)))
    shutil.rmtree(records)
    records.mkdir()
    run_tests.run([one_gate, other_gate], threads=2, quiet=True)
    check("--full's pool really overlaps them (otherwise the check above proves "
          "nothing)", overlapping(records), str(lives(records)))

    good = fake_gate(tmp, "test_fake_pass", "print('the child spoke')\n")
    outcome = run_tests.one(good, quiet=True)
    check("a gate that exits 0 is a pass", outcome.ok, outcome.verdict)
    check("its output is in its own <name>.out",
          "the child spoke" in outcome.out.read_text(encoding="utf-8"))
    check("the run is written to the memory the next run reads",
          stamps.recent("test_fake_pass") == [round(outcome.seconds, 1)]
          or stamps.recent("test_fake_pass") != [],
          f"{stamps.recent('test_fake_pass')}")
    bad = fake_gate(tmp, "test_fake_fail", "sys.exit(3)\n")
    check("a gate that exits non-zero is a failure",
          run_tests.one(bad, quiet=True).verdict == "fail")

    marker = tmp / "the-slow-gate-finished.txt"
    slow = fake_gate(tmp, "test_fake_slow",
                     f"time.sleep(3.0)\nopen({str(marker)!r}, 'w').write('done')\n",
                     budget_s=0.2)
    started = time.monotonic()
    ended = run_tests.one(slow, quiet=True)
    elapsed = time.monotonic() - started
    limit = slow.budget_s * run_tests.SLACK
    check(f"a gate past BUDGET_S x SLACK is ended at about {limit:.1f} s, not "
          f"waited for", elapsed < limit + 2.0, f"{elapsed:.1f} s")
    check("...and it is a failure with the reason recorded",
          ended.verdict == "fail" and "budget" in ended.note, ended.note)
    time.sleep(1.0)
    check("...and it really is gone: the work it would have done after the "
          "sleep never happened", not marker.exists())

    holder, pid = hold_lock(20.0)
    try:
        check("the holder's own note names it, and the note survives a read "
              "while the lock is held (a fixed-size read does not -- see "
              "run_tests.lock_holder)",
              str(pid) in run_tests.lock_holder(run_tests.lock_path()),
              repr(run_tests.lock_holder(run_tests.lock_path())))
        started = time.monotonic()
        waited, message = capture(lambda: run_tests.claim_run(wait_s=1.0))
        check("a second claimant waits, then reports the lock seized",
              waited is None and 0.9 <= time.monotonic() - started < 4.0,
              f"after {time.monotonic() - started:.1f} s")
        check("...and the wait names the live run it is waiting for, by pid",
              str(pid) in message and "waiting" in message,
              message.strip()[:200])
    finally:
        holder.terminate()
        holder.wait(timeout=10)
    free = run_tests.claim_run(wait_s=1.0)
    check("the OS frees the lock when the holder dies, so a killed run leaves "
          "no stale lock", free is not None)
    run_tests.release_run(free)
    again = run_tests.claim_run(wait_s=0.0)
    check("...and a released lock can be taken again at once", again is not None)
    run_tests.release_run(again)
    holder.stdout.close()

    os.environ.pop("FB_DASHBOARD_TEST_LOG_DIR", None)
    os.environ.pop("FB_DASHBOARD_RUN_LOCK", None)
    print()
    if failures:
        print(f"FAIL: {len(failures)} runner check(s) failed")
        return 1
    print("ALL RUNNER TESTS PASS")
    return 0

STAMP_S = 4.0
BUDGET_S = 6.0

if __name__ == "__main__":
    testlog.start("test_runner")
    code = main()
    testlog.finish(code == 0)
    sys.exit(code)
