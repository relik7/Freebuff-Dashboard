from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fixture
import testlog
from dashboard import corpus
from dashboard.fleet import Fleet

DATA = testlog.work_dir("predicate")

NAIVE = "length(trim(coalesce(json_extract(p.value,'$.text'),''))) > 0"

failures: list[str] = []

def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {label}")
    else:
        failures.append(label)
        print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))

def count_with(fleet: Fleet, predicate: str) -> int:
    total = 0
    for project in fleet.readable():
        sql = (f"select count(*) as n from {project.key}.messages m,"
               f" json_each(m.parts_json) p"
               f" where json_extract(p.value,'$.kind') = 'text' and {predicate}")
        total += fleet.read(
            project, lambda connection, sql=sql:
            connection.execute(sql).fetchone()["n"])
    return total

def rows_with(fleet: Fleet, predicate: str) -> list:
    found = []
    for project in fleet.readable():
        sql = (f"select json_extract(p.value,'$.text') as text"
               f" from {project.key}.messages m, json_each(m.parts_json) p"
               f" where json_extract(p.value,'$.kind') = 'text' and {predicate}")
        found.extend(fleet.read(
            project, lambda connection, sql=sql:
            connection.execute(sql).fetchall()))
    return found

def main() -> int:
    print("=== the whitespace predicate: the honest form, and the naive one ===")
    facts = fixture.build(DATA)
    expected = facts["expected"]
    config = fixture.config_for(DATA)
    fleet = Fleet(config)
    try:
        print(f"      fixture: {len(fleet.projects)} project(s),"
              f" {expected['text_parts']} text part(s),"
              f" {expected['whitespace_only_parts']} whitespace-only")

        check("the fixture really contains filler parts (a predicate test with"
              " nothing to exclude proves nothing)",
              expected["newline_only_parts"] >= 1
              and expected["whitespace_only_parts"] >= 2
              and expected["empty_parts"] >= 1,
              str(expected))

        honest = count_with(fleet, corpus.NOT_BLANK)
        naive = count_with(fleet, NAIVE)
        print(f"      honest predicate: {honest} row(s); naive predicate: {naive}")

        check("the honest predicate selects exactly the non-blank parts",
              honest == expected["nonblank_text_parts"],
              f"{honest} != {expected['nonblank_text_parts']}")
        check("the naive `length(trim(text)) > 0` selects strictly more rows"
              " (the regression is real, not hypothetical)",
              naive > honest, f"{naive} vs {honest}")

        check("the difference is exactly the fixture's newline-only parts",
              naive - honest == expected["newline_only_parts"],
              f"{naive - honest} != {expected['newline_only_parts']}")

        naive_only = rows_with(fleet, f"{NAIVE} and not ({corpus.NOT_BLANK})")
        bodies = sorted({row["text"] for row in naive_only})
        check("every row the naive predicate adds is whitespace and nothing else",
              all(body.strip() == "" for body in bodies),
              f"{bodies!r}")
        check("a newline-only part is one of them (the specific defect)",
              any(body.strip() == "" and "\n" in body for body in bodies),
              f"{bodies!r}")

        for character in ("char(9)", "char(10)", "char(13)"):
            check(f"the predicate names {character} in its trim set",
                  character in corpus.NOT_BLANK, corpus.NOT_BLANK)

        print()
        if failures:
            print(f"FAIL: {len(failures)} predicate check(s) failed")
            return 1
        print("ALL PREDICATE TESTS PASS")
        return 0
    finally:
        fleet.close()

STAMP_S = 0.2
BUDGET_S = 0.3

if __name__ == "__main__":
    testlog.start("test_predicate")
    try:
        code = main()
    finally:
        fixture.close()
    testlog.finish(code == 0)
    sys.exit(code)
