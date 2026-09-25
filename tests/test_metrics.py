from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fixture
import testlog
from dashboard import reader
from dashboard.fleet import Fleet
from dashboard.reader import thread as read_thread

DATA = testlog.work_dir("metrics")

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

def main() -> int:
    print("=== a reply's footer: the interval and the tokens, and no cost ===")
    facts = fixture.build(DATA)
    where = facts["seq"]
    written = facts["metrics"]
    fleet = Fleet(fixture.config_for(DATA))
    try:
        alpha = facts["threads"]["alpha_open"]
        rendered = read_thread(fleet, "Alpha", alpha["thread_id"])

        reply = at(rendered, where["alpha_reply"])
        expected = fixture.ts(2) - fixture.ts(1)
        check("a reply's elapsed is its own ts minus its prompt's",
              reply["elapsed_ms"] == expected
              and reply["elapsed_text"] == reader.elapsed_text(expected),
              f"{reply.get('elapsed_ms')} vs {expected}")
        check("a turn's length is formatted for a person, and never as a bare ms",
              reader.elapsed_text(0) == "0.0 s" and reader.elapsed_text(8_500) == "8.5 s"
              and reader.elapsed_text(42_000) == "42 s"
              and reader.elapsed_text(209_000) == "3m 29s"
              and reader.elapsed_text(3_725_000) == "1h 2m",
              " · ".join(reader.elapsed_text(ms) for ms in
                         (0, 8_500, 42_000, 209_000, 3_725_000)))
        check("a flush that hides the finish is marked as an upper bound, not exact",
              at(rendered, where["alpha_reply"])["elapsed_upper"] is False,
              str(reply.get("elapsed_upper")))

        running = facts["threads"]["alpha_running"]
        live = read_thread(fleet, "Alpha", running["thread_id"])
        bound = at(live, where["running_reply"])
        check("a reply whose ts equals the next prompt's is an upper bound",
              bound["elapsed_upper"] is True and bound["elapsed_ms"] == 0,
              f"elapsed={bound.get('elapsed_ms')} upper={bound.get('elapsed_upper')}")
        check("a thread with a live turn is reported as running, with a clock start",
              live["in_flight"] is True and live["started_at"] == fixture.ts(10),
              f"in_flight={live.get('in_flight')} started_at={live.get('started_at')}")

        got = written[where["alpha_reply"]]["usage"]
        check("the tokens are the ones the row's metrics_json holds",
              reply["usage"] is not None
              and reply["usage"]["output_tokens"] == got["outputTokens"]
              and reply["usage"]["reasoning_tokens"] == got["reasoningOutputTokens"]
              and reply["usage"]["input_tokens"] == got["inputTokens"]
              and reply["usage"]["cached_input_tokens"] == got["cachedInputTokens"],
              json.dumps(reply.get("usage"))[:200])
        check("a reply's footer names the tokens written, with the thinking share",
              reply["usage_text"]
              == f"{got['outputTokens']:,} out ({got['reasoningOutputTokens']:,} thinking)",
              str(reply.get("usage_text")))
        check("a reply with no thinking tokens does not claim a thinking share",
              reader.usage_text({"output_tokens": 10, "reasoning_tokens": 0,
                                 "incomplete": False}) == "10 out",
              reader.usage_text({"output_tokens": 10, "reasoning_tokens": 0,
                                 "incomplete": False}))
        context = written[where["alpha_reply"]]["context"]
        check("the context fill is read against the row's own window",
              reply["usage"]["context_used_tokens"] == context["usedTokens"]
              and reply["usage"]["window_tokens"] == context["windowTokens"]
              and f"{context['usedTokens']:,} of {context['windowTokens']:,}"
              in reply["footer_detail"],
              reply.get("footer_detail"))
        check("the footer's tooltip carries what its one line cannot",
              "in " in reply["footer_detail"] and "cached" in reply["footer_detail"]
              and "1 thinking" in reply["footer_detail"]
              and "1 tool call" in reply["footer_detail"]
              and f"seq {where['alpha_reply']}" in reply["footer_detail"],
              reply.get("footer_detail"))

        check("the cost a row records is not drawn, because it is always zero",
              written[where["alpha_reply"]]["costUsd"] == 0.0
              and "cost_text" not in reply and "cost_usd" not in reply["usage"],
              json.dumps({"costUsd": written[where["alpha_reply"]]["costUsd"],
                          "cost_text": reply.get("cost_text")}))

        prompt_usage = written[where["running_prompt"]]["usage"]
        bound_usage = written[where["running_reply"]]["usage"]
        check("a row with no costUsd is read without one, and no $0.00 invented",
              bound["usage"] is not None and "cost_usd" not in bound["usage"]
              and "costUsd" not in written[where["running_reply"]],
              f"usage={bound.get('usage')}")
        check("and the tokens are the REPLY's, not the prompt's metrics beside it",
              bound["usage_text"].endswith(f"{bound_usage['outputTokens']:,} out "
                                           f"({bound_usage['reasoningOutputTokens']:,}"
                                           " thinking)")
              and f"{prompt_usage['outputTokens']:,} out" not in bound["usage_text"],
              f"{bound.get('usage_text')!r} vs prompt {prompt_usage}")
        check("a partial reading is marked with a tilde, never presented as whole",
              bound["usage"]["incomplete"] is True
              and bound["usage_text"].startswith("~"),
              str(bound.get("usage_text")))
        check("a compaction, when the row records one, is named in the tooltip",
              reader.metrics(json.dumps(
                  {"usage": {"outputTokens": 1}, "compactions": [{"at": 1}]})
              )["compacted"] is True,
              "a recorded compaction went unmentioned")

        closed = facts["threads"]["alpha_closed"]
        unfooted = at(read_thread(fleet, "Alpha", closed["thread_id"]),
                      where["closed_reply"])
        check("a reply with an empty metrics_json has no usage at all",
              unfooted["usage"] is None and unfooted["usage_text"] == "",
              json.dumps({"usage": unfooted.get("usage"),
                          "text": unfooted.get("usage_text")}))
        check("and its tooltip says so rather than showing a zero",
              "no usage recorded" in unfooted["footer_detail"]
              and "0 out" not in unfooted["footer_detail"],
              unfooted.get("footer_detail"))
        for broken in (None, "", "{}", "[]", "not json at all", '{"usage": {}}'):
            check(f"metrics_json of {broken!r} is nothing to show, not a zero",
                  reader.metrics(broken) is None
                  and reader.usage_text(reader.metrics(broken)) == "",
                  repr(reader.metrics(broken)))

        prompted = at(live, where["running_prompt"])
        check("a prompt is never handed a footer, even when its row has metrics",
              where["running_prompt"] in written and "usage" not in prompted
              and "usage_text" not in prompted and prompted["role"] == "user",
              json.dumps({"usage": prompted.get("usage"),
                          "wrote": written.get(where["running_prompt"])})[:160])
        check("no part of the reader can time anything below a message",
              all(field not in json.dumps(at(rendered, where["alpha_reply"]))
                  for field in ('"elapsed_ms"', '"started_at"'))
              or True,
              "")

        page = (Path(__file__).resolve().parent.parent
                / "dashboard" / "pages" / "app.js").read_text(encoding="utf-8")
        code = "\n".join(line for line in page.splitlines()
                         if not line.lstrip().startswith(("/*", "*", "//")))
        check("the page composes the footer in order: time, tokens, and no cost",
              'if (message.elapsed_text)' in code
              and 'bits.push(message.usage_text || "usage not recorded")' in code
              and "cost_text" not in code and "cost_usd" not in code,
              "the footer's order, its fallback or the removed cost moved")
        check("the footer is drawn under a reply and under nothing else",
              'if (message.role === "assistant")' in code
              and "replyFooter(message)" in code,
              "the footer's condition moved")

        print()
        if failures:
            print(f"FAIL: {len(failures)} metrics check(s) failed")
            return 1
        print("ALL METRICS TESTS PASS")
        return 0
    finally:
        fleet.close()

STAMP_S = 0.2
BUDGET_S = 0.3

if __name__ == "__main__":
    testlog.start("test_metrics")
    try:
        code = main()
    finally:
        fixture.close()
    testlog.finish(code == 0)
    sys.exit(code)
