from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fixture
import testlog
from dashboard import corpus, reader
from dashboard.fleet import Fleet
from dashboard.reader import ReaderError, thread as read_thread

DATA = testlog.work_dir("reader")

failures: list[str] = []

def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {label}")
    else:
        failures.append(label)
        print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))

def at(thread: dict, seq: int) -> dict:
    for turn in thread["turns"]:
        for message in turn["messages"]:
            if message["seq"] == seq:
                return message
    raise AssertionError(f"no message at seq {seq}")

def kinds(message: dict) -> list[str]:
    return [part["kind"] for part in message["blocks"]]

def of(message: dict, kind: str) -> list[dict]:
    return [part for part in message["blocks"] if part["kind"] == kind]

def main() -> int:
    print("=== the reader: turns, paragraphs, blocks, and no ads at all ===")
    facts = fixture.build(DATA)
    where = facts["seq"]
    fleet = Fleet(fixture.config_for(DATA))
    try:
        alpha = facts["threads"]["alpha_open"]
        rendered = read_thread(fleet, "Alpha", alpha["thread_id"])

        first = next(turn for turn in rendered["turns"]
                     if turn["seq"] == where["alpha_prompt"])
        check("a prompt and the message at seq + 1 are one turn",
              [message["seq"] for message in first["messages"]]
              == [where["alpha_prompt"], where["alpha_reply"]]
              and [message["role"] for message in first["messages"]]
              == ["user", "assistant"],
              str([message["seq"] for message in first["messages"]]))

        reply = at(rendered, where["alpha_reply"])

        check("a message's blocks keep the order the app wrote its parts in",
              kinds(reply) == ["text", "text", "tool", "reasoning", "changes"],
              str(kinds(reply)))
        check("prose parts that are not blank become text blocks, in order",
              [part["text"] for part in of(reply, "text")]
              == ["First paragraph of the reply.",
                  "Second paragraph with fake_media inside."],
              str([part["text"] for part in of(reply, "text")]))

        blanks = at(rendered, where["alpha_blank"])
        check("a message of filler parts has no blocks at all (and no paragraphs)",
              blanks["blocks"] == [], str(blanks["blocks"]))

        thoughts = of(reply, "reasoning")
        check("reasoning is a collapsed block labelled by its first line",
              thoughts and thoughts[0]["label"] == "private thinking about fake_media"
              and "second thought" in thoughts[0]["text"],
              str(thoughts)[:160])

        labels = [block["label"] for block in thoughts]
        check("every collapsed block's label is one line, within the cap",
              all("\n" not in text and len(text) <= reader.LABEL_CHARS
                  for text in labels),
              str(labels)[:160])
        long_first = "x" * (reader.LABEL_CHARS * 3)
        check("a first line over the cap is trimmed, never sent whole or wrapped",
              reader.label(f"{long_first}\nsecond line")
              == "x" * reader.LABEL_CHARS,
              reader.label(long_first)[:80])
        calls = of(reply, "tool")
        check("a tool run carries its name, its input, its output and its exit code",
              calls and calls[0]["name"] == "bash"
              and "ls -la" in calls[0]["input"]["text"]
              and "retry backoff file.txt" in calls[0]["output"]["text"]
              and calls[0]["exit_code"] == 0,
              json.dumps(calls)[:200])

        entries = [part for part in reply["blocks"]
                   if part["kind"] in ("reasoning", "tool")]
        check("every thinking part and tool call is its own entry, never a merged row",
              len(entries) == len(thoughts) + len(calls) == 2
              and all("blocks" not in part for part in entries),
              f"{len(thoughts)} thought(s) + {len(calls)} call(s) -> {len(entries)} entry(ies)")
        check("a tool entry is labelled by the reader, and keeps its wire name",
              calls[0].get("label") == "Bash" and calls[0].get("name") == "bash",
              json.dumps({key: calls[0].get(key) for key in ("label", "name", "detail")}))

        for wire, label in (("str_replace", "Edit"), ("read_files", "Read"),
                            ("run_terminal_command", "Run command"),
                            ("code_search", "Search"),
                            ("suggest_prompts", "Suggest prompts"),
                            ("preview_evaluate", "Preview evaluate"),
                            ("read_thread_context", "Read thread context")):
            check(f"`{wire}` is named `{label}` rather than by its wire name",
                  reader.tool_label(wire) == label, reader.tool_label(wire))
        check("a tool nobody has heard of is still readable, never `tool · xyz`",
              reader.tool_label("frobnicate_widgets") == "Frobnicate widgets"
              and reader.tool_label("bash") == "Bash"
              and reader.tool_label("") == "tool",
              reader.tool_label("frobnicate_widgets"))

        check("a tool's subject is read from its input (path, command, first path)",
              reader.tool_subject('{"path": "dashboard/reader.py", "replacements": []}')
              == "dashboard/reader.py"
              and reader.tool_subject('{"command": "grep -n retry"}') == "grep -n retry"
              and reader.tool_subject('{"paths": ["tests/reader.py", "tests/api.py"]}')
              == "tests/reader.py"
              and reader.tool_subject('{"query": "retry backoff"}') == "retry backoff",
              reader.tool_subject('{"path": "dashboard/reader.py"}'))
        check("an input with no subject yields no detail, rather than a guess",
              reader.tool_subject("ls -la /tmp") == ""
              and reader.tool_subject(None) == ""
              and reader.tool_subject('{"replacements": []}') == ""
              and calls[0].get("detail") == "",
              repr(reader.tool_subject("ls -la /tmp")))
        diffs = of(reply, "changes")
        check("a diff lists the path it touched with its counts and patch",
              diffs and diffs[0]["files"]
              and diffs[0]["files"][0]["path"].endswith("main.py")
              and diffs[0]["files"][0]["adds"] == 12
              and diffs[0]["files"][0]["dels"] == 3
              and "@@" in diffs[0]["files"][0]["patch"]["text"],
              json.dumps(diffs)[:200])

        singular = facts["diffs"]["one_file"]
        check("...and the one card a turn's changes are drawn as carries their"
              " totals, summed for the header",
              len(diffs) == 1 and len(diffs[0]["files"]) == singular["files"]
              and diffs[0]["adds"] == singular["adds"]
              and diffs[0]["dels"] == singular["dels"],
              json.dumps({key: diffs[0].get(key)
                          for key in ("adds", "dels", "files")})[:200])

        closed = read_thread(fleet, "Alpha",
                             facts["threads"]["alpha_closed"]["thread_id"])
        plural = of(at(closed, where["closed_reply"]), "changes")
        two = facts["diffs"]["two_files"]
        check("a changes part with several files is still ONE part, its totals"
              " their sums",
              len(plural) == 1 and len(plural[0]["files"]) == two["files"]
              and plural[0]["adds"] == two["adds"]
              and plural[0]["dels"] == two["dels"],
              json.dumps({key: plural[0].get(key)
                          for key in ("adds", "dels", "files")})[:200])
        check("a file's own status travels with it, so a card can say `added`",
              [one["status"] for one in plural[0]["files"]]
              == ["modified", "added"]
              and plural[0]["files"][1]["path"] == two["added"],
              json.dumps([one["status"] for one in plural[0]["files"]]))

        serialised = json.dumps(rendered)
        check("a sponsored card appears nowhere in the rendered thread",
              "Sponsored" not in serialised and "ads.example" not in serialised
              and "sponsored" not in serialised.lower(),
              "an ad's title or URL reached the page")
        check("`ad` is not a kind the reader can render at all",
              "ad" not in corpus.RENDERED_KINDS, str(corpus.RENDERED_KINDS))

        notice = at(rendered, where["alpha_notice"])
        check("a notice is a quiet line, because it explains a gap",
              [part["text"] for part in of(notice, "notice")]
              == ["freebuff-session-ended"],
              str(of(notice, "notice")))

        marked = at(rendered, where["alpha_marks"])
        check("an attachment is a chip carrying its name, kind and path",
              marked["attachments"]
              and marked["attachments"][0]["name"] == "diagram.png"
              and marked["attachments"][0]["kind"] == "image"
              and marked["attachments"][0]["path"].endswith("diagram.png"),
              str(marked["attachments"]))
        check("an apostrophe, a percent, a double hyphen and an emoji survive"
              " the round trip exactly",
              "quote ' and \" and 100% coverage and snake_case_name and a--b"
              in of(marked, "text")[0]["text"]
              and "\U0001F680" in of(marked, "text")[0]["text"]
              and "\r\n" in of(marked, "text")[0]["text"],
              repr(of(marked, "text")[0]["text"])[:160])

        hostile = at(rendered, where["alpha_markup"])
        check("markup in the transcript comes back as data, not as markup",
              of(hostile, "text")[0]["text"]
              == "<script>alert(1)</script> nothing from the transcript is markup",
              repr(of(hostile, "text")[0]["text"])[:160])

        running = facts["threads"]["alpha_running"]
        live = read_thread(fleet, "Alpha", running["thread_id"])
        shared = {message["ts"] for message in live["turns"][0]["messages"]}
        check("two messages sharing a timestamp pair by seq, not by time",
              len(live["turns"][0]["messages"]) == 2 and len(shared) == 1,
              f"{len(live['turns'][0]['messages'])} message(s), {shared}")
        check("a running thread is reported as running (the sidebar spinner)",
              live["turn_state"] == "running" or live["turn_state"] != "idle",
              live["turn_state"])

        beta = facts["threads"]["beta_open"]
        big = read_thread(fleet, "Beta", beta["thread_id"])
        huge = at(big, where["beta_big"])
        words = of(huge, "text")
        check("a 1 MB single-line part comes back clipped and flagged",
              words and words[0]["truncated"] is True
              and len(words[0]["text"]) == reader.MAX_PART_CHARS,
              f"{len(words[0]['text']) if words else 0} char(s),"
              f" truncated={words[0]['truncated'] if words else None}")
        check("the header names the project, the title, the status and the count",
              big["project"] == "Beta" and big["title"] == beta["title"]
              and big["status"] == "open" and big["thread_id"] == beta["thread_id"]
              and big["message_count"] == 4 and big["updated_at"] > big["created_at"],
              json.dumps({key: big[key] for key in
                          ("project", "title", "status", "message_count")}))

        centred = read_thread(fleet, "Alpha", alpha["thread_id"],
                              around=where["alpha_reply"])
        check("`around` returns a window that contains its centre",
              any(message["seq"] == where["alpha_reply"]
                  for turn in centred["turns"] for message in turn["messages"]),
              str([turn["seq"] for turn in centred["turns"]]))

        try:
            read_thread(fleet, "Alpha", "no-such-thread")
            refused = False
        except ReaderError as exc:
            refused = exc.status == 404
        check("an unknown thread is a 404 rather than an empty thread", refused)

        script = (Path(__file__).resolve().parent.parent
                  / "dashboard" / "pages" / "app.js").read_text(encoding="utf-8")
        code_lines = [line for line in script.splitlines()
                      if not line.lstrip().startswith(("/*", "*", "//"))]
        code = "\n".join(code_lines)
        check("the page draws one entry per part and folds no run of them",
              "partEntry(" in code
              and "foldedRun" not in code and "foldable" not in code
              and 'part.kind === "reasoning" || part.kind === "tool"' in code,
              "a run-merging helper is back in the page")

        print()
        if failures:
            print(f"FAIL: {len(failures)} reader check(s) failed")
            return 1
        print("ALL READER TESTS PASS")
        return 0
    finally:
        fleet.close()

STAMP_S = 0.2
BUDGET_S = 0.3

if __name__ == "__main__":
    testlog.start("test_reader")
    try:
        code = main()
    finally:
        fixture.close()
    testlog.finish(code == 0)
    sys.exit(code)
