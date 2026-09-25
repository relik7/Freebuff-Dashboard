from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fixture
import testlog
from dashboard import config as config_module
from dashboard import reader
from dashboard import search
from dashboard.fleet import Fleet

DATA = testlog.work_dir("filters")

BOTH_ROLES_Q = "DB"

failures: list[str] = []

def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {label}")
    else:
        failures.append(label)
        print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))

def run(fleet: Fleet, q: str, **kwargs):
    return search.search(fleet, search.Filters(q=q, mode="exact", **kwargs))

def seqs(answer: dict) -> list[int]:
    return [row["seq"] for row in answer["results"]]

def refused(action) -> tuple[bool, str, int]:
    try:
        action()
    except search.SearchError as exc:
        return True, str(exc), exc.status
    return False, "", 0

def main() -> int:
    print("=== filters: roles, scopes, and the refusal with a sentence ===")
    facts = fixture.build(DATA)
    where = facts["seq"]
    alpha = facts["threads"]["alpha_open"]
    beta = facts["threads"]["beta_open"]
    fleet = Fleet(fixture.config_for(DATA))
    everyone = ("user", "assistant")
    try:
        both = run(fleet, BOTH_ROLES_Q, scope="all",
                   categories=("user", "assistant"))
        mine = run(fleet, BOTH_ROLES_Q, scope="all", categories=("user",))
        yours = run(fleet, BOTH_ROLES_Q, scope="all", categories=("assistant",))
        check("the two prose categories together return every matching row",
              both["total"] == 2 and len(both["results"]) == 2, str(seqs(both)))
        check("`User messages` returns only user rows",
              set(seqs(mine)) == {where["beta_phrase"]}
              and all(row["role"] == "user" for row in mine["results"]),
              str([(row["seq"], row["role"]) for row in mine["results"]]))
        check("`Agent responses` returns only assistant rows",
              set(seqs(yours)) == {where["beta_literal"]}
              and all(row["role"] == "assistant" for row in yours["results"]),
              str([(row["seq"], row["role"]) for row in yours["results"]]))
        check("the two prose categories are disjoint and add up to both on",
              set(seqs(mine)) & set(seqs(yours)) == set()
              and set(seqs(mine)) | set(seqs(yours)) == set(seqs(both)),
              f"{seqs(mine)} + {seqs(yours)} vs {seqs(both)}")

        derived = {name: search.Filters(q="x", categories=(name,)).roles
                   for name in search.CATEGORIES}
        check("a heavy category is either side's, not one of them",
              derived["reasoning"] == ("user", "assistant")
              and derived["tools"] == ("user", "assistant")
              and derived["changes"] == ("user", "assistant")
              and derived["user"] == ("user",)
              and derived["assistant"] == ("assistant",),
              str(derived))

        ok, message, status = refused(lambda: run(
            fleet, BOTH_ROLES_Q, scope="all", categories=()))
        check("no category ticked is refused rather than answered with nothing",
              ok and status == 400, f"refused={ok} status={status}")
        check("...with the sentence the page shows verbatim",
              message == search.NO_CATEGORIES, message)

        tools = run(fleet, "ls -la", scope="all",
                    categories=("tools",))
        check("`Tool runs` reaches the parts a tool part is made of",
              tools["total"] > 0
              and {row["category"] for row in tools["results"]} <= {"tool_input",
                                                                    "tool_output"}
              and {row["kind"] for row in tools["results"]} == {"tool"},
              f"{tools['total']} row(s): "
              f"{sorted({row['category'] for row in tools['results']})}")
        everything = run(fleet, "ls -la", scope="all",
                         categories=tuple(search.CATEGORIES))

        diffs = run(fleet, "old line", scope="all", categories=("changes",))
        check("`File diffs` reaches a patch, named for the change and not for the"
              " call that made it",
              diffs["total"] > 0
              and {row["kind"] for row in diffs["results"]} == {"changes"}
              and {row["label"] for row in diffs["results"]} == {"Diffs"},
              str([(row["kind"], row["label"]) for row in diffs["results"]]))

        check("a tool hit carries the label the thread's page would give it",
              all(row["label"] == reader.tool_label(row["name"])
                  for row in tools["results"])
              and {row["label"] for row in tools["results"]} == {"Bash"},
              str([(row["label"], row["name"]) for row in tools["results"]]))
        check("naming every category is the same as naming the parts it covers",
              set(seqs(everything))
              >= {row["seq"] for row in tools["results"]},
              f"{seqs(everything)} vs {seqs(tools)}")
        ok, message, status = refused(lambda: run(
            fleet, "ls -la", scope="all", categories=()))
        check("no category ticked is refused rather than answered with nothing",
              ok and status == 400, f"refused={ok} status={status}")
        check("...with the sentence the page shows verbatim",
              message == search.NO_CATEGORIES, message)

        both_projects_q = "and"
        for label in ("Alpha", "Beta"):
            scoped = run(fleet, both_projects_q, scope="project", project=label,
                         )
            check(f"scope `this project` on {label} returns only {label} rows",
                  scoped["total"] > 0
                  and {row["project"] for row in scoped["results"]} == {label},
                  str({row["project"] for row in scoped["results"]}))
        by_key = run(fleet, both_projects_q, scope="project",
                     project=fleet.by_label("Alpha").key)
        left = run(fleet, both_projects_q, scope="project", project="Alpha",
                   )
        check("a project named by its key and by its label answer the same",
              seqs(by_key) == seqs(left), f"{seqs(by_key)} vs {seqs(left)}")

        narrow = run(fleet, "retry", scope="thread", thread=alpha["thread_id"],
                     )
        check("scope `this thread` returns only that thread's rows",
              narrow["total"] == 2
              and {row["thread_id"] for row in narrow["results"]}
              == {alpha["thread_id"]}, str(seqs(narrow)))
        elsewhere = run(fleet, "retry", scope="thread", thread=beta["thread_id"],
                        )
        check("...and a thread that does not carry the query returns nothing",
              elsewhere["total"] == 0, str(seqs(elsewhere)))

        ok, _, status = refused(lambda: run(
            fleet, "the", scope="project", project="NoSuchProject"))
        check("an unknown project is a 404, not an empty list", ok and status == 404,
              f"refused={ok} status={status}")
        ok, _, status = refused(lambda: run(
            fleet, "the", scope="thread", thread="no-such-thread"))
        check("an unknown thread is a 404, not an empty list", ok and status == 404,
              f"refused={ok} status={status}")

        first = run(fleet, "retry", scope="all", limit=1, offset=0)
        second = run(fleet, "retry", scope="all", limit=1, offset=1)
        check("a limited page keeps the unpaged total",
              first["total"] == 2 == second["total"], f"{first['total']}")
        check("offset moves the window instead of repeating it",
              seqs(first) and seqs(second) and not set(seqs(first)) & set(seqs(second)),
              f"{seqs(first)} vs {seqs(second)}")

        configured = fixture.config_for(DATA)
        configured["search"]["categories"] = ["user"]
        parsed = search.parse({"q": [BOTH_ROLES_Q]}, configured)
        check("a configured default category set is what an unparameterised search"
              " uses",
              parsed.categories == ("user",) and parsed.roles == ("user",)
              and parsed.mode == "words",
              f"categories={parsed.categories} roles={parsed.roles} "
              f"mode={parsed.mode}")
        filtered = search.search(fleet, parsed)
        check("...and it really filters",
              filtered["total"] == 1
              and all(row["role"] == "user" for row in filtered["results"]),
              str([(row["seq"], row["role"]) for row in filtered["results"]]))
        ok, _, status = refused(lambda: search.parse({"mode": ["nonsense"]},
                                                     config_module.load()))
        check("an unknown mode is refused, not guessed", ok and status == 400,
              f"refused={ok} status={status}")
        ok, _, status = refused(lambda: search.parse({"scope": ["galaxy"]},
                                                     config_module.load()))
        check("an unknown scope is refused, not guessed", ok and status == 400,
              f"refused={ok} status={status}")

        print()
        if failures:
            print(f"FAIL: {len(failures)} filter check(s) failed")
            return 1
        print("ALL FILTER TESTS PASS")
        return 0
    finally:
        fleet.close()

STAMP_S = 0.2
BUDGET_S = 0.3

if __name__ == "__main__":
    testlog.start("test_filters")
    try:
        code = main()
    finally:
        fixture.close()
    testlog.finish(code == 0)
    sys.exit(code)
