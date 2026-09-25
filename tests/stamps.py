from __future__ import annotations

import re
import statistics
from datetime import datetime
from pathlib import Path

import testlog

MEMORY = "stamps.tsv"

HISTORY = 3

PASSED = "pass"

ELAPSED = re.compile(r"^--- elapsed:\s+([0-9.]+)\s+s\s+\(pass\)", re.M)

def memory_path() -> Path:
    return testlog.log_dir() / MEMORY

def parse_elapsed(text: str) -> float | None:
    found = ELAPSED.findall(text)
    return float(found[-1]) if found else None

def log_stamp(name: str) -> float | None:
    path = testlog.log_dir() / f"{name}.log"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_elapsed(text)

def record(name: str, seconds: float, *, verdict: str) -> None:
    if verdict not in ("pass", "fail"):
        return
    try:
        with memory_path().open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{name}\t{seconds:.1f}\t"
                         f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{verdict}\n")
    except OSError:
        pass

def recent(name: str, count: int = HISTORY) -> list[float]:
    try:
        rows = memory_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    found: list[float] = []
    for row in rows:
        cells = row.split("\t")
        if len(cells) < 4 or cells[0] != name or cells[3] != PASSED:
            continue
        try:
            found.append(float(cells[1]))
        except ValueError:
            continue
    return found[-count:]

def forecast(suite) -> float:
    remembered = recent(suite.name)
    if remembered:
        return statistics.median(remembered)
    stamped = log_stamp(suite.name)
    if stamped is not None:
        return stamped
    if suite.stamp_s > 0:
        return suite.stamp_s
    return suite.budget_s
