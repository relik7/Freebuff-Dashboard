from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fixture
import testlog
from dashboard import corpus
from dashboard.fleet import Fleet

DATA = testlog.work_dir("extract")

CATEGORIES = {"user_text", "assistant_text", "reasoning", "tool_input",
              "tool_output", "changes"}

failures: list[str] = []

def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {label}")
    else:
        failures.append(label)
        print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))

def records(fleet: Fleet, kinds: tuple) -> list:
    found = []
    for project in fleet.readable():
        sql, params = corpus.record_select(project.key, kinds=kinds)
        found.extend(fleet.connection().execute(sql, params).fetchall())
    return found

def main() -> int:
    print("=== the extraction: kinds, categories and the two-record tool part ===")
    facts = fixture.build(DATA)
    expected = facts["expected"]
    fleet = Fleet(fixture.config_for(DATA))
    try:
        conversation = records(fleet, corpus.CONVERSATION_KINDS)
        everything = records(fleet, corpus.ALL_KINDS)
        print(f"      {len(conversation)} conversation record(s),"
              f" {len(everything)} with every kind")

        check("conversation extracts exactly the non-blank text parts",
              len(conversation) == expected["nonblank_text_parts"],
              f"{len(conversation)} != {expected['nonblank_text_parts']}")
        check("conversation carries only the two conversation categories",
              {row["category"] for row in conversation}
              == {"user_text", "assistant_text"},
              str({row["category"] for row in conversation}))

        blanks = [row for row in everything
                  if (row["text"] or "").strip() == ""]
        check("no record whose text is only whitespace ever comes back",
              not blanks, f"{len(blanks)} blank record(s)")
        check("no record has empty text",
              all((row["text"] or "") != "" for row in everything))

        check("the categories are exactly the recognised vocabulary",
              {row["category"] for row in everything} == CATEGORIES,
              str({row["category"] for row in everything}))
        spans = " ".join((row["text"] or "") for row in everything)
        check("a sponsored card's text and URL never reach a record",
              "never to be rendered" not in spans
              and "never to be indexed" not in spans
              and "ads.example" not in spans,
              "an ad's text or URL appeared")
        check("a notice never reaches a record either",
              "freebuff-session-ended" not in spans, "the notice appeared")

        calls = [row for row in everything if row["category"] == "tool_input"]
        results = [row for row in everything if row["category"] == "tool_output"]
        check("a tool part yields a record for its call and one for its result",
              len(calls) == expected["tool_parts"]
              and len(results) == expected["tool_parts"],
              f"{len(calls)} call(s), {len(results)} result(s) for"
              f" {expected['tool_parts']} tool part(s)")
        check("the call record carries the command, not the output",
              all("ls -la" in (row["text"] or "") for row in calls), str(calls))
        check("the result record carries the output, not the command",
              all("retry backoff file.txt" in (row["text"] or "") for row in results),
              str(results))
        check("the two records of a tool part share its message and its turn",
              calls and results and calls[0]["seq"] == results[0]["seq"]
              and calls[0]["thread_id"] == results[0]["thread_id"],
              f"{calls} vs {results}")
        check("a conversation record of a mixed message is still only prose",
              all((row["text"] or "") != ""
                  for row in conversation if row["seq"] == calls[0]["seq"]),
              "a tool text leaked into a conversation record")

        prompts = [row for row in conversation if row["category"] == "user_text"]
        check("user prose is one part per prompt",
              len(prompts) == expected["user_prose_parts"]
              == expected["user_messages"],
              f"{len(prompts)} part(s), {expected['user_prose_parts']} prose part(s),"
              f" {expected['user_messages']} user message(s)")

        sample = everything[0]
        check("a record carries the thread, the turn and the time",
              sample["thread_id"] and sample["seq"] and sample["ts"]
              and sample["role"] in ("user", "assistant"),
              str(dict(sample)))

        print()
        if failures:
            print(f"FAIL: {len(failures)} extraction check(s) failed")
            return 1
        print("ALL EXTRACT TESTS PASS")
        return 0
    finally:
        fleet.close()

STAMP_S = 0.2
BUDGET_S = 0.3

if __name__ == "__main__":
    testlog.start("test_extract")
    try:
        code = main()
    finally:
        fixture.close()
    testlog.finish(code == 0)
    sys.exit(code)
