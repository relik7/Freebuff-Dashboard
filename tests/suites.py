from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

HELPERS = frozenset({"testlog", "suites", "stamps", "harness", "run_tests",
                    "fixture"})

@dataclass(frozen=True)
class Suite:
    name: str
    path: Path
    budget_s: float
    stamp_s: float

    def __str__(self) -> str:
        return self.name

class UnknownSuite(Exception):
    pass

def gate_paths() -> list[Path]:
    return sorted(TESTS.glob("test_*.py"))

def load(path: Path):
    spec = importlib.util.spec_from_file_location(f"_gate_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise UnknownSuite(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def discover() -> list[Suite]:
    found: list[Suite] = []
    for path in gate_paths():
        name = path.stem
        if name in HELPERS:
            continue
        module = load(path)
        found.append(Suite(
            name=name,
            path=path,
            budget_s=float(getattr(module, "BUDGET_S", 0.0) or 0.0),
            stamp_s=float(getattr(module, "STAMP_S", 0.0) or 0.0),
        ))
    return found

def by_name(name: str) -> Suite:
    for suite in discover():
        if suite.name == name:
            return suite
    raise UnknownSuite(name)
