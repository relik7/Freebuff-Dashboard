from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import testlog

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

STATIC_DIRS = ("config",)

GENERATED_DIRS = ("__pycache__",)

failures: list[str] = []

def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {label}")
    else:
        failures.append(label)
        print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))

def test_names() -> set[str]:
    return {path.stem[len("test_"):] for path in TESTS.glob("test_*.py")}

def ignored(path: str) -> bool:
    proc = subprocess.run(["git", "check-ignore", "-q", f"{path}/"], cwd=ROOT,
                          capture_output=True)
    if proc.returncode not in (0, 1):
        print(f"      (git check-ignore failed with {proc.returncode}; "
              f"treating {path} as not ignored)")
        return False
    return proc.returncode == 0

def tracked() -> set[str] | None:
    proc = subprocess.run(["git", "ls-files", "-z", "--", "tests"], cwd=ROOT,
                          capture_output=True)
    if proc.returncode != 0:
        return None
    return {name for name in proc.stdout.decode("utf-8", "replace").split("\0")
            if name}

def main() -> int:
    print("=== the tests tree: the gates, the static files they need, and "
          "nothing a run writes ===")

    if not TESTS.is_dir():
        print("no tests/ directory")
        return 1

    names = test_names()
    print(f"      {len(names)} test module(s): {', '.join(sorted(names))}")

    stray: list[str] = []
    for path in sorted(TESTS.rglob("*")):
        rel = path.relative_to(TESTS)
        if path.is_dir() or rel.parts[0] in GENERATED_DIRS:
            continue
        if len(rel.parts) == 1 and rel.suffix == ".py":
            continue
        if rel.parts[0] in STATIC_DIRS and rel.suffix.lower() == ".json":
            continue
        stray.append(str(rel))
    check("tests/ holds the gates and the static files they need -- <name>.py at "
          "the top level and config/*.json beside them -- and nothing a run "
          "writes", not stray, "; ".join(stray[:8]))

    kept = sorted(path.name for path in TESTS.iterdir()
                  if path.is_dir() and path.name not in GENERATED_DIRS
                  and path.name not in STATIC_DIRS)
    check("tests/ keeps no directory of its own: a gate writes into "
          "logs/tests/<gate>/, so running the suite never changes tests/",
          not kept, "; ".join(kept[:8]))

    modules = sorted(str(path.relative_to(TESTS)) for path in TESTS.rglob("*.py")
                     if path.relative_to(TESTS).parts[0] in STATIC_DIRS)
    check("the static tier holds no Python module", not modules,
          ", ".join(modules[:4]))

    config = TESTS / "config"
    check("tests/config/ exists (the static tier)", config.is_dir())
    if config.is_dir():
        entries = sorted(path.name for path in config.iterdir())
        wrong = [name for name in entries if Path(name).suffix.lower() != ".json"]
        check("tests/config/ holds only JSON", not wrong, "; ".join(wrong[:8]))
        broken: list[str] = []
        for path in sorted(config.glob("*.json")):
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                broken.append(f"{path.name}: {exc}")
        check("every config parses as JSON", not broken, "; ".join(broken[:4]))
        check("every config is named for what it configures "
              "(config_<something>.json)",
              all(name.startswith("config_") for name in entries),
              "; ".join(n for n in entries if not n.startswith("config_")))

    scratch = testlog.log_dir()
    check("the scratch root is logs/tests/, outside the tests tree",
          scratch.name == "tests" and TESTS not in scratch.parents, f"{scratch}")

    known = tracked()
    own = known if known and "tests/test_tree.py" in known else None
    if own is None:
        print("      (this tests/ is not the tracked tree of that checkout -- a "
              "handoff copy, say -- so the git checks are shape only)")
    else:
        check("the checkout's git ignores that root", ignored("logs"),
              "logs/ is not ignored here")
        present = {path.relative_to(ROOT).as_posix() for path in TESTS.rglob("*")
                   if path.is_file()
                   and "__pycache__" not in path.relative_to(TESTS).parts}
        added = sorted(present - own)
        check("every file under tests/ is tracked, so running the suite adds "
              "nothing to it", not added, "; ".join(added[:8]))

    print()
    if failures:
        print(f"FAIL: {len(failures)} tree check(s) failed")
        return 1
    print("ALL TREE TESTS PASS")
    return 0

STAMP_S = 0.1
BUDGET_S = 0.2

if __name__ == "__main__":
    testlog.start("test_tree")
    code = main()
    testlog.finish(code == 0)
    sys.exit(code)
