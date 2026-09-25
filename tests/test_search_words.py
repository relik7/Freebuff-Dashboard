from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fixture
import testlog
from dashboard import search
from dashboard.fleet import Fleet

DATA = testlog.work_dir("search_words")

failures: list[str] = []

def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {label}")
    else:
        failures.append(label)
        print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))

def run(fleet: Fleet, q: str):
    return search.search(fleet, search.Filters(q=q, mode="words", scope="all",
                                               ))

def seqs(answer: dict) -> list[int]:
    return [row["seq"] for row in answer["results"]]

def main() -> int:
    print("=== words / phrase, live: every term, adjacency, and recency ===")
    facts = fixture.build(DATA)
    where = facts["seq"]
    fleet = Fleet(fixture.config_for(DATA))
    try:
        both = run(fleet, "retry backoff")
        check("an unquoted query requires every term",
              seqs(both) == [where["alpha_prompt"]], str(seqs(both)))
        check("...and a row with only one of the terms does not match",
              where["alpha_retry_only"] not in seqs(both), str(seqs(both)))
        one = run(fleet, "retry")
        check("a one-term query matches both rows that carry it",
              sorted(seqs(one)) == [where["alpha_prompt"],
                                    where["alpha_retry_only"]],
              str(seqs(one)))
        check("results rank by recency, newest first",
              seqs(one) == [where["alpha_retry_only"], where["alpha_prompt"]],
              str(seqs(one)))

        quoted = run(fleet, '"make no changes to that DB"')
        check("a quoted phrase matches the sentence that carries it",
              seqs(quoted) == [where["beta_phrase"]], str(seqs(quoted)))
        check("...and the snippet marks the whole phrase",
              quoted["results"]
              and "<mark>make no changes to that DB</mark>"
              in quoted["results"][0]["snippet"],
              str([row["snippet"] for row in quoted["results"]])[:200])
        reordered = run(fleet, '"that DB make no changes to"')
        check("a phrase does not match the same words reordered"
              " (adjacency, not a bag of words)",
              seqs(reordered) == [], str(seqs(reordered)))
        loose = run(fleet, "no changes to that DB")
        check("the unquoted spelling of the same words does match"
              " (that is what words mode means)",
              seqs(loose) == [where["beta_phrase"]], str(seqs(loose)))

        unclosed = run(fleet, '"make no changes to that DB')
        check("an unterminated phrase quote is closed, not searched for as a"
              " literal character",
              seqs(unclosed) == [where["beta_phrase"]], str(seqs(unclosed)))

        paired = run(fleet, "make changes")
        phrase_row = next((row for row in paired["results"]
                           if row["seq"] == where["beta_phrase"]), None)
        check("a snippet marks every term of a multi-term query, not only the first",
              phrase_row is not None
              and "<mark>make</mark>" in phrase_row["snippet"]
              and "<mark>changes</mark>" in phrase_row["snippet"],
              str(phrase_row["snippet"])[:200] if phrase_row else str(seqs(paired)))

        tool_only = run(fleet, "file.txt")
        check("a query that only a tool part carries finds nothing in"
              " conversation mode",
              seqs(tool_only) == [], str(seqs(tool_only)))

        across = run(fleet, "the")["results"]
        times = [row["ts"] for row in across]
        check("a query spanning projects comes back newest first",
              len(times) > 1 and times == sorted(times, reverse=True),
              str(times))

        print()
        if failures:
            print(f"FAIL: {len(failures)} word-search check(s) failed")
            return 1
        print("ALL WORD SEARCH TESTS PASS")
        return 0
    finally:
        fleet.close()

STAMP_S = 0.2
BUDGET_S = 0.3

if __name__ == "__main__":
    testlog.start("test_search_words")
    try:
        code = main()
    finally:
        fixture.close()
    testlog.finish(code == 0)
    sys.exit(code)
