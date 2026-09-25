from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import harness
import run_tests
import suites
import testlog

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

EXPECTED_MAX_SUITES = 3

EXPECTED_SLACK = 1.5

EXPECTED_THREADS = 4

EXPECTED_TOOL_PORT = 8770

failures: list[str] = []

def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {label}")
    else:
        failures.append(label)
        print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))

def source(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")

def is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (isinstance(test, ast.Compare) and len(test.comparators) == 1
            and isinstance(test.left, ast.Name) and test.left.id == "__name__"
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__")

def call_name(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")

def work_at_import(tree: ast.Module) -> list[str]:
    nested: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Lambda)):
            nested.update(id(child) for child in ast.walk(node))
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if is_main_guard(node):
            guarded.update(id(child) for child in ast.walk(node))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and id(node) not in nested | guarded:
            found.append(call_name(node))
    return found

def logged_name(tree: ast.Module) -> str | None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "start" \
                and node.args and isinstance(node.args[0], ast.Constant):
            return str(node.args[0].value)
    return None

def main() -> int:
    print("=== the declaration vocabulary ===")
    known = suites.discover()
    print(f"      {len(known)} gate(s) declare a budget")

    stems = {path.stem for path in suites.gate_paths()}
    check("every tests/test_*.py is discovered as a gate",
          stems == {suite.name for suite in known},
          ", ".join(sorted(stems - {suite.name for suite in known})))
    check("no module named test_* is written off as a helper (a gate cannot be "
          "skipped by being added to HELPERS)",
          not stems & set(suites.HELPERS), ", ".join(sorted(stems & set(suites.HELPERS))))
    check("every helper the project ships is in HELPERS (a gate must not need "
          "a second list)",
          all((TESTS / f"{name}.py").is_file() for name in suites.HELPERS),
          ", ".join(name for name in sorted(suites.HELPERS)
                    if not (TESTS / f"{name}.py").is_file()))

    missing = [s.name for s in known if s.stamp_s <= 0]
    check("every gate declares a STAMP_S, so a cold machine has a real cost to "
          "order by", not missing, ", ".join(missing))
    underived = [f"{s.name}: budget {s.budget_s:g} s for a {s.stamp_s:g} s gate"
                 for s in known if s.budget_s < s.stamp_s * 1.5 - 0.05]
    check("every gate's wall is at least 1.5x its own measured cost",
          not underived, "; ".join(underived[:6]))
    check("every planned gate exists on disk",
          all(suite.path.is_file() for suite in known))

    unguarded = []
    unlogged = []
    misnamed = []
    for suite in known:
        tree = ast.parse(source(suite.path))
        eager = set(work_at_import(tree)) & {"start", "finish", "main"}
        if eager:
            unguarded.append(f"{suite.name} ({', '.join(sorted(eager))})")
        name = logged_name(tree)
        if name is None:
            unlogged.append(suite.name)
        elif name != suite.name:
            misnamed.append(f"{suite.name} logs as {name}")
    check("no gate does work when it is imported (the runner reads every gate's "
          "declarations to print a plan)",
          not unguarded, ", ".join(sorted(set(unguarded))))
    check("every gate names itself in its log line", not unlogged,
          ", ".join(unlogged))
    check("...and names itself, not a neighbour (the log file is the gate's "
          "name)", not misnamed, "; ".join(misnamed))

    check(f"a gate is ended at BUDGET_S x {EXPECTED_SLACK} (2.25x its own "
          f"measurements)", run_tests.SLACK == EXPECTED_SLACK,
          f"SLACK = {run_tests.SLACK}")
    check(f"--suites takes at most {EXPECTED_MAX_SUITES} gates",
          run_tests.MAX_SUITES == EXPECTED_MAX_SUITES,
          f"MAX_SUITES = {run_tests.MAX_SUITES}")
    check(f"the --full pool is {EXPECTED_THREADS} threads",
          run_tests.DEFAULT_THREADS == EXPECTED_THREADS,
          f"DEFAULT_THREADS = {run_tests.DEFAULT_THREADS}")
    check("the run lock is machine-wide (in the system temp directory, not in a "
          "checkout)",
          run_tests.RUN_LOCK.is_absolute() and ROOT not in run_tests.RUN_LOCK.parents,
          str(run_tests.RUN_LOCK))
    check("the run lock is not inside any project tree (two checkouts share one "
          "machine)",
          "Freebuff" not in str(run_tests.RUN_LOCK), str(run_tests.RUN_LOCK))

    try:
        suites.by_name("test_nonsense")
        check("an unknown gate name is an error", False, "by_name accepted it")
    except suites.UnknownSuite as exc:
        check("an unknown gate name is an error naming the name",
              "test_nonsense" in str(exc), str(exc))
    check("a known name resolves to its own file",
          suites.by_name("test_declarations").path.name == "test_declarations.py")

    check(f"the harness knows the tool's own port ({EXPECTED_TOOL_PORT}), so a "
          "gate can avoid it without repeating the number",
          harness.TOOL_PORT == EXPECTED_TOOL_PORT, f"{harness.TOOL_PORT}")

    mine = Path(__file__).stem
    carries = [suite.name for suite in known
               if suite.name != mine and str(EXPECTED_TOOL_PORT) in source(suite.path)]
    check(f"no other gate carries the tool's own port ({EXPECTED_TOOL_PORT}) in "
          "its text: a gate asks the OS for one",
          not carries, ", ".join(carries))

    print()
    if failures:
        print(f"FAIL: {len(failures)} declaration check(s) failed")
        return 1
    print("ALL DECLARATION TESTS PASS")
    return 0

STAMP_S = 0.4
BUDGET_S = 0.6

if __name__ == "__main__":
    testlog.start("test_declarations")
    code = main()
    testlog.finish(code == 0)
    sys.exit(code)
