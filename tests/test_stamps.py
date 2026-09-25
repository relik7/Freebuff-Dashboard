from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stamps
import suites
import testlog

failures: list[str] = []

def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {label}")
    else:
        failures.append(label)
        print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))

def fake_suite(name: str, *, stamp_s: float = 0.0, budget_s: float = 60.0):
    return suites.Suite(name=name, path=Path("(none)"), budget_s=budget_s,
                        stamp_s=stamp_s)

def main() -> int:
    print("=== the runtime memory ===")
    tmp = tempfile.mkdtemp(prefix="fb-dashboard-stamps-")
    os.environ["FB_DASHBOARD_TEST_LOG_DIR"] = tmp

    log = Path(tmp) / testlog.SUITE / "test_fake.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("--- started 2026-09-22 00:00:00\n"
                   "--- elapsed: 3.0 s (fail)\n"
                   "--- log: x\n"
                   "--- elapsed: 12.5 s (pass)\n", encoding="utf-8")
    check("parse_elapsed reads a stamp",
          stamps.parse_elapsed("--- elapsed: 12.5 s (pass)\n") == 12.5)
    check("...and takes the newest one in a log",
          stamps.log_stamp("test_fake") == 12.5, f"{stamps.log_stamp('test_fake')}")
    check("a log with no stamp is None, not zero",
          stamps.log_stamp("test_absent") is None)
    check("a skip line is not a cost",
          stamps.parse_elapsed("--- elapsed: 0.1 s (skip)\n") is None)
    check("a fail line is not a cost either (a broken gate's time is not its "
          "cost)", stamps.parse_elapsed("--- elapsed: 1.0 s (fail)\n") is None)

    (Path(tmp) / testlog.SUITE / "test_failed.log").write_text(
        "--- started 2026-09-22 00:00:00\n--- elapsed: 1.0 s (fail)\n",
        encoding="utf-8")
    check("...so a gate whose every log line failed has no log stamp",
          stamps.log_stamp("test_failed") is None)

    check("the memory has no history before anything is recorded",
          stamps.recent("test_fake") == [])
    stamps.record("test_fake", 10.0, verdict="pass")
    stamps.record("test_fake", 14.0, verdict="pass")
    check("a recorded run is remembered",
          stamps.recent("test_fake") == [10.0, 14.0], f"{stamps.recent('test_fake')}")
    stamps.record("test_fake", 0.1, verdict="skip")
    check("a skipped gate is not recorded as a cost",
          stamps.recent("test_fake") == [10.0, 14.0], f"{stamps.recent('test_fake')}")

    stamps.record("test_fake", 0.4, verdict="fail")
    check("a failed run is not a cost either (a fast failure must never drag "
          "the next forecast down)",
          stamps.recent("test_fake") == [10.0, 14.0], f"{stamps.recent('test_fake')}")
    check("...but its row is kept, so the file still dates the failure",
          "test_fake\t0.4\t" in stamps.memory_path().read_text(encoding="utf-8"))
    for seconds in (11.0, 12.0, 13.0):
        stamps.record("test_fake", seconds, verdict="pass")
    check(f"only the last {stamps.HISTORY} runs are kept",
          stamps.recent("test_fake") == [11.0, 12.0, 13.0],
          f"{stamps.recent('test_fake')}")

    remembered = fake_suite("test_fake", stamp_s=99.0, budget_s=99.0)
    check("memory beats the declaration and the log (median of the last three)",
          stamps.forecast(remembered) == 12.0, f"{stamps.forecast(remembered)}")

    stamps.record("test_median", 1.0, verdict="pass")
    stamps.record("test_median", 2.0, verdict="pass")
    stamps.record("test_median", 99.0, verdict="pass")
    median = stamps.forecast(fake_suite("test_median", budget_s=99.0))
    check("the forecast is the median, not the mean", median == 2.0, f"{median}")

    logged_only = fake_suite("test_fake")
    stamps.memory_path().unlink()
    check("with no memory the gate's own stamp is next",
          stamps.forecast(logged_only) == 12.5, f"{stamps.forecast(logged_only)}")
    declared = fake_suite("test_nothing", stamp_s=7.5, budget_s=99.0)
    check("with no memory and no log the declaration is next",
          stamps.forecast(declared) == 7.5, f"{stamps.forecast(declared)}")
    bare = fake_suite("test_nothing", stamp_s=0.0, budget_s=42.0)
    check("with nothing at all the budget is the last resort",
          stamps.forecast(bare) == 42.0, f"{stamps.forecast(bare)}")

    os.environ.pop("FB_DASHBOARD_TEST_LOG_DIR", None)
    print()
    if failures:
        print(f"FAIL: {len(failures)} stamp check(s) failed")
        return 1
    print("ALL STAMP TESTS PASS")
    return 0

STAMP_S = 0.1
BUDGET_S = 0.2

if __name__ == "__main__":
    testlog.start("test_stamps")
    code = main()
    testlog.finish(code == 0)
    sys.exit(code)
