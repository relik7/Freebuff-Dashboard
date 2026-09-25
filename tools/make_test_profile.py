from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
import uuid
from pathlib import Path

DEFAULT_ROOT = "testprofile"
PROFILE = Path(".config") / "freebuff-desktop"

MINUTE = 60_000
GAP_MS = 2 * MINUTE
WINDOW_MS = 57 * MINUTE

SCHEMA = """
CREATE TABLE projects (id TEXT PRIMARY KEY, path TEXT, created_at INTEGER);
CREATE TABLE threads (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  project_path TEXT,
  title TEXT,
  status TEXT,
  turn_state TEXT,
  model TEXT,
  created_at INTEGER,
  updated_at INTEGER,
  sidebar_archived_at INTEGER
);
CREATE TABLE messages (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  request_id TEXT,
  input_id TEXT,
  role TEXT NOT NULL,
  parts_json TEXT NOT NULL DEFAULT '[]',
  attachments_json TEXT NOT NULL DEFAULT '[]',
  metrics_json TEXT NOT NULL DEFAULT '{}',
  ts INTEGER NOT NULL
);
CREATE INDEX idx_messages_thread  ON messages(thread_id, seq);
CREATE INDEX idx_messages_input   ON messages(thread_id, input_id) WHERE input_id IS NOT NULL;
CREATE INDEX idx_messages_request ON messages(thread_id, request_id) WHERE request_id IS NOT NULL;
CREATE TABLE queue_items (id INTEGER PRIMARY KEY, thread_id TEXT, prompt TEXT,
                          state TEXT, source TEXT, position INTEGER);
"""

PROJECTS = (
    ("Lighthouse", "C:/work/HarbourWatch"),
    ("Comedian", "C:/work/Comedian"),
    ("Mechanic", "C:/work/Mechanic"),
)

def text(body: str) -> dict:
    return {"kind": "text", "text": body}

def thought(body: str) -> dict:
    return {"kind": "reasoning", "text": body, "open": False, "collapse": True}

def tool(name: str, call, output: str, exit_code: int = 0) -> dict:
    return {"kind": "tool", "toolName": name, "input": call, "output": output,
            "exitCode": exit_code, "status": "ok" if exit_code == 0 else "failed"}

def change(path: str, adds: int, dels: int, patch: str, status: str = "modified") -> dict:
    return {"kind": "changes", "files": [
        {"path": path, "status": status, "adds": adds, "dels": dels, "patch": patch}]}

def changes(*records) -> dict:
    return {"kind": "changes", "files": [
        {"path": record[0], "adds": record[1], "dels": record[2],
         "patch": record[3],
         "status": record[4] if len(record) > 4 else "modified"}
        for record in records]}

def notice(body: str) -> dict:
    return {"kind": "notice", "notice": body, "text": body}

def metrics(written: int, sent: int, *, thinking: int = 0, cached: int = 0,
            cost: float | None = None, incomplete: bool = False) -> dict:
    found = {
        "context": {"usedTokens": 21_700, "compactionThresholdTokens": 400_000,
                    "windowTokens": 1_048_576},
        "compactions": [],
        "usage": {"inputTokens": sent, "cachedInputTokens": cached,
                  "outputTokens": written, "reasoningOutputTokens": thinking,
                  "totalTokens": sent + written},
    }
    if cost is not None:
        found["costUsd"] = cost
    if incomplete:
        found["usageIncomplete"] = True
    return found

def when(step: int) -> int:
    return step * MINUTE

def settle(con: sqlite3.Connection, newest: int, highest: int, scale: float) -> None:
    args = (newest, highest, scale)
    con.execute("update messages set ts = cast(? - (? - ts) * ? as integer)", args)
    con.execute("update threads set created_at ="
                " cast(? - (? - created_at) * ? as integer),"
                " updated_at = cast(? - (? - updated_at) * ? as integer),"
                " sidebar_archived_at ="
                " cast(? - (? - sidebar_archived_at) * ? as integer)", args * 3)
    con.execute("update projects set created_at ="
                " cast(? - (? - created_at) * ? as integer)", args)

def anchor(writers: list) -> None:
    stamps = [con.execute("select min(ts), max(ts) from messages").fetchone()
              for _label, con in writers]
    highest = max(row[1] for row in stamps)
    lowest = min(row[0] for row in stamps)
    newest = int(time.time() * 1000) - GAP_MS
    scale = WINDOW_MS / max(highest - lowest, 1)
    for _label, con in writers:
        settle(con, newest, highest, scale)

def project_directory(projects_root: Path, label: str) -> Path:
    return projects_root / f"{label}-{uuid.uuid5(uuid.NAMESPACE_URL, label)}"

def project_id(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, label))

def open_project(projects_root: Path, label: str, path: str) -> sqlite3.Connection:
    directory = project_directory(projects_root, label)
    directory.mkdir(parents=True, exist_ok=True)
    identity = project_id(label)
    (directory / "project.json").write_text(json.dumps({
        "version": 1, "projectId": identity, "projectPath": path,
        "database": "desktop-v2.db"}, indent=2) + "\n", encoding="utf-8", newline="\n")
    con = sqlite3.connect(directory / "desktop-v2.db")
    con.executescript(SCHEMA)
    con.execute("insert into projects (id, path, created_at) values (?,?,?)",
                (identity, path, when(0)))
    return con

def thread_id(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))

def add_thread(con: sqlite3.Connection, slabel: str, label: str, path: str,
               title: str, status: str, turn_state: str, created: int,
               updated: int, archived: int | None = None) -> str:
    found = thread_id(slabel)
    con.execute(
        "insert into threads (id, project_id, project_path, title, status,"
        " turn_state, model, created_at, updated_at, sidebar_archived_at)"
        " values (?,?,?,?,?,?,?,?,?,?)",
        (found, project_id(label), path, title, status, turn_state, "anvil",
         created, updated, archived))
    con.execute("insert into queue_items (thread_id, prompt, state, source, position)"
                " values (?,?,'done','user',0)", (found, title))
    return found

def add_message(con: sqlite3.Connection, tid: str, seq: int, role: str, parts: list,
                at: int, *, attachments: list | None = None,
                metrics_json: dict | None = None) -> None:
    con.execute(
        "insert into messages (seq, thread_id, request_id, input_id, role,"
        " parts_json, attachments_json, metrics_json, ts) values (?,?,?,?,?,?,?,?,?)",
        (seq, tid, None, f"in-{tid[:8]}-{seq}" if role == "user" else None,
         role, json.dumps(parts, ensure_ascii=False),
         json.dumps(attachments or [], ensure_ascii=False),
         json.dumps(metrics_json or {}, ensure_ascii=False), at))

def lighthouse(con: sqlite3.Connection, path: str) -> None:
    label, workspace = "Lighthouse", path

    squall = add_thread(con, "lh-squall", label, workspace,
                        "Squall alarm trips on every gust", "open", "idle",
                        when(0), when(5))
    add_message(con, squall, 1, "user", [text(
        "Every squall inside twenty kilometres sets the alarm off, even the ones "
        "that pass us over the water. The keeper was up three times last night "
        "for a blow that never arrived. She has started calling the alarm "
        "decoration. It is not, but I cannot defend it as it stands.")], when(0))
    add_message(con, squall, 2, "assistant", [
        thought("A single gust sample above the threshold raises the alarm, so any "
                "squall line within range triggers it even when the sustained wind "
                "never comes up. Persistence is the missing idea: wind that matters "
                "stays up for a minute, a squall edge does not."),
        text("The alarm is reading one sample, not weather. Two changes: the "
             "threshold has to hold for a persistence window, and only count "
             "sectors that face us."),
        tool("read_files", json.dumps({"path": "src/alarm.py"}, indent=2),
             "def evaluate(sample):\n"
             "    return sample.gust >= GUST_THRESHOLD\n"),
        tool("str_replace", json.dumps({
            "path": "src/alarm.py",
            "old_string": "def evaluate(sample):\n"
                          "    return sample.gust >= GUST_THRESHOLD",
            "new_string": "def evaluate(window):\n"
                          "    return all(s.gust >= GUST_THRESHOLD for s in window)\n"
                          "    and not all(s.sector.heading for s in window)"},
            indent=2), "applied 1 replacement"),
        change("src/alarm.py", 4, 2,
               "@@ -12,3 +12,5 @@\n-def evaluate(sample):\n"
               "-    return sample.gust >= GUST_THRESHOLD\n"
               "+def evaluate(window):\n"
               "+    return all(s.gust >= GUST_THRESHOLD for s in window)\n"
               "+    and not all(s.sector.heading for s in window)\n"),
        tool("run_terminal_command",
             json.dumps({"command": "python -m station.cli alarm --replay "
                                    "data/squalls-night.csv"}, indent=2),
             "replayed 6 hours: 2 alarms (was 11)\n",
             1),
    ], when(1), metrics_json=metrics(486, 41_200, thinking=310, cached=33_000,
                                     cost=0.024))
    add_message(con, squall, 3, "user", [text(
        "Better. Now it fires twice for the same squall, about four minutes "
        "apart. One squall, two bells.")], when(3))
    add_message(con, squall, 4, "assistant", [
        thought("The alarm clears on the falling edge and re-arms while the same "
                "squall is still overhead. It needs to stay latched until there has "
                "been real calm, not just one quiet sample."),
        text("It re-arms in the lull behind the front edge. Latch it and require a "
             "calm window before it can ring again."),
        tool("str_replace", json.dumps({
            "path": "src/alarm.py",
            "old_string": "    armed = True\n",
            "new_string": "    armed = armed or all(s.gust < CALM for s in window)\n"},
            indent=2), "applied 1 replacement"),
        change("src/alarm.py", 3, 1,
               "@@ -31,3 +31,3 @@\n-    armed = True\n"
               "+    armed = armed or all(s.gust < CALM for s in window)\n"),
        tool("run_terminal_command",
             json.dumps({"command": "python -m station.cli alarm --replay "
                                    "data/squalls-night.csv"}, indent=2),
             "replayed 6 hours: 1 alarm\n"),
    ], when(4), metrics_json=metrics(322, 46_800, thinking=204, cached=41_000,
                                     cost=0.017))
    add_message(con, squall, 5, "user", [text(
        "One bell for one squall. Write it into the station notes so the next "
        "keeper knows why the alarm went quiet.")], when(4))
    add_message(con, squall, 6, "assistant", [
        tool("write_file", json.dumps({
            "path": "docs/station-notes.md",
            "content": "# Station notes\n\n## Alarm\n\nRings once per squall. "
                       "A gust must hold for the persistence window and the "
                       "sector must face the station.\n"}, indent=2),
             "wrote 180 characters"),
        change("docs/station-notes.md", 5, 0,
               "@@ -1,1 +1,6 @@\n # Station notes\n+\n+## Alarm\n+\n"
               "+Rings once per squall.\n"),
    ], when(5), metrics_json=metrics(118, 52_400, cached=45_000))

    ship = add_thread(con, "lh-ships", label, workspace,
                      "Importing the harbourmaster's ship log", "closed", "idle",
                      when(10), when(17))
    add_message(con, ship, 7, "user", [text(
        "The harbourmaster mails a CSV of arrivals every morning and I have to "
        "get it into the watch list. Half the rows come through with the ship "
        "name spelled some other way, so my importer drops them and I type them "
        "in by hand.")], when(10))
    add_message(con, ship, 8, "assistant", [
        thought("The same hull arrives as two names because the office and the "
                "pilots write them differently. Normalising case, punctuation and "
                "the usual prefixes gets most of it, and the rest is close enough "
                "to match on a set of name tokens."),
        text("Two passes. Fold the name to a comparable form, then match on the "
             "token set rather than the string."),
        tool("list_directory", json.dumps({"path": "data"}, indent=2),
             "data/arrivals.csv\ndata/fleet.json\ndata/squalls-night.csv\n"),
        tool("read_files", json.dumps({"path": "src/importer.py"}, indent=2),
             "def key(name):\n    return name.strip().lower()\n"),
        text("I will add a suffix table for the office's habit of writing 'MV', "
             "'SS' and roman numerals, and keep the unmatched rows in a report "
             "instead of throwing them away."),
    ], when(11), metrics_json=metrics(410, 34_500, thinking=196, cached=28_000,
                                      cost=0.019))
    add_message(con, ship, 9, "user", [text(
        "Do it, and show me the diff before I wire it into the morning job.")],
        when(13))
    add_message(con, ship, 10, "assistant", [
        thought("One function to normalise, a token matcher, and the unmatched "
                "rows written out so the keeper can check them by eye."),
        tool("str_replace", json.dumps({
            "path": "src/importer.py",
            "old_string": "def key(name):\n    return name.strip().lower()",
            "new_string": "SUFFIXES = (\"mv\", \"ss\", \"hms\", \"ii\", \"iii\")\n\n"
                          "def key(name):\n"
                          "    words = [w for w in normalise(name) if w not in SUFFIXES]\n"
                          "    return frozenset(words)"},
            indent=2), "applied 1 replacement"),
        change("src/importer.py", 6, 1,
               "@@ -3,2 +3,7 @@\n-def key(name):\n"
               "-    return name.strip().lower()\n"
               "+SUFFIXES = (\"mv\", \"ss\", \"hms\", \"ii\", \"iii\")\n"
               "+def key(name):\n"
               "+    words = [w for w in normalise(name) if w not in SUFFIXES]\n"
               "+    return frozenset(words)\n"),
        tool("run_terminal_command",
             json.dumps({"command": "python -m station.cli import "
                                    "--dry-run data/arrivals.csv"}, indent=2),
             "rows 96: matched 91, report 5\n"),
    ], when(14), metrics_json=metrics(372, 39_100, thinking=158, cached=34_000,
                                      cost=0.021))
    add_message(con, ship, 11, "user", [text(
        "The pilot boat Sea Otter II still lands in the report, and there is only "
        "one of her.")], when(16))
    add_message(con, ship, 12, "assistant", [
        thought("The numeral is stripped from one side of the match and not the "
                "other, so 'Sea Otter II' folds to one word and 'Sea Otter' to the "
                "same one - which should match. The office writes her as 'Sea Otter "
                "2' on some days, and a digit is a different word."),
        tool("str_replace", json.dumps({
            "path": "src/importer.py",
            "old_string": "    return frozenset(words)",
            "new_string": "    return frozenset(NUMERALS.get(w, w) for w in words)"},
            indent=2), "applied 1 replacement"),
        change("src/importer.py", 4, 1,
               "@@ -8,1 +8,4 @@\n-    return frozenset(words)\n"
               "+    return frozenset(NUMERALS.get(w, w) for w in words)\n"),
        tool("run_terminal_command",
             json.dumps({"command": "python -m station.cli import "
                                    "--dry-run data/arrivals.csv"}, indent=2),
             "rows 96: matched 95, report 1\n"),
    ], when(17), metrics_json=metrics(268, 44_600, thinking=131, cached=38_000,
                                      cost=0.016))
    add_message(con, ship, 13, "user", [text(
        "One row left and it is genuinely a new boat. Put a short note in the "
        "README for whoever runs this after me.")], when(17))
    add_message(con, ship, 14, "assistant", [
        tool("write_file", json.dumps({
            "path": "README.md",
            "content": "# HarbourWatch\n\n## Arrivals import\n\n"
                       "`station.cli import --dry-run` writes unmatched names to "
                       "`report/`.\nAdd a hull to `data/fleet.json` rather than "
                       "editing the CSVs.\n"}, indent=2),
             "wrote 210 characters"),
        change("README.md", 7, 1,
               "@@ -1,1 +1,8 @@\n # HarbourWatch\n+\n+## Arrivals import\n+\n"
               "+Unmatched names land in `report/`.\n"),
    ], when(18), metrics_json=metrics(134, 49_700, cached=43_000))

    lamp = add_thread(con, "lh-lamp", label, workspace,
                      "Lamp rotation drifts after a power cut", "closed", "idle",
                      when(22), when(32), archived=when(33))
    add_message(con, lamp, 15, "user", [text(
        "We lost power for six minutes last night and the lamp came back up about "
        "a quarter turn out of phase. A coaster off the point radioed that the "
        "flash pattern was wrong, which is the last thing I want to hear on a "
        "black night.")], when(22))
    add_message(con, lamp, 16, "assistant", [
        thought("The drive counts hall edges from wherever it happened to stop, so "
                "a cold start has no absolute position. That is survivable for the "
                "motor and not survivable for a published flash character."),
        text("The controller has no home position: it counts edges from zero after "
             "a start, so any power cut shifts the phase. It needs a marker once "
             "per revolution and a re-home on the way up."),
        tool("read_files", json.dumps({"path": "src/lamp.py"}, indent=2),
             "def start(self):\n    self.edges = 0\n    self.run()\n"),
        tool("code_search", json.dumps({"pattern": "home|phase|marker",
                                        "path": "src"}, indent=2),
             "src/lamp.py:14:    self.edges = 0\nsrc/lamp.py:52:    pass\n"),
    ], when(23), metrics_json=metrics(354, 36_800, thinking=222, cached=29_000,
                                      cost=0.018))
    add_message(con, lamp, 17, "user", [text(
        "There is a marker sensor on the turntable, it has never been wired in. "
        "Use it.")], when(25))
    add_message(con, lamp, 18, "assistant", [
        thought("Find the marker, set zero there, then start counting. The whole "
                "rotation aborts if the marker does not arrive within one and a "
                "half revolutions, because a missed marker means the sensor or the "
                "run should stop, not continue blind."),
        tool("str_replace", json.dumps({
            "path": "src/lamp.py",
            "old_string": "def start(self):\n    self.edges = 0\n    self.run()",
            "new_string": "def start(self):\n    self.edges = 0\n"
                          "    self.home(timeout=1.5 * REVOLUTION)\n    self.run()"},
            indent=2), "applied 1 replacement"),
        change("src/lamp.py", 5, 2,
               "@@ -10,3 +10,5 @@\n-def start(self):\n-    self.edges = 0\n"
               "+def start(self):\n+    self.edges = 0\n"
               "+    self.home(timeout=1.5 * REVOLUTION)\n"),
        tool("run_terminal_command",
             json.dumps({"command": "python -m station.cli lamp --selftest"},
                        indent=2),
             "marker found at edge 61\nturntable phase: 0.00 deg\n"),
    ], when(26), metrics_json=metrics(402, 42_300, thinking=241, cached=35_000,
                                      cost=0.022))
    add_message(con, lamp, 19, "user", [text(
        "Does that run on every start or only after a cold boot? A warm restart "
        "does not need a full turn if the phase is still good.")], when(28))
    add_message(con, lamp, 20, "assistant", [
        thought("It always homes now, which costs one revolution on every start. "
                "A warm restart keeps the phase, so the marker hunt can be skipped "
                "when the controller knows where it is."),
        text("It homes on every start, which is one wasted revolution. Skip the "
             "hunt when the encoder still agrees with the last known phase."),
        tool("str_replace", json.dumps({
            "path": "src/lamp.py",
            "old_string": "    self.home(timeout=1.5 * REVOLUTION)",
            "new_string": "    if not self.phase_is_trusted():\n"
                          "        self.home(timeout=1.5 * REVOLUTION)"},
            indent=2), "applied 1 replacement"),
        change("src/lamp.py", 3, 1,
               "@@ -12,1 +12,2 @@\n-    self.home(timeout=1.5 * REVOLUTION)\n"
               "+    if not self.phase_is_trusted():\n"
               "+        self.home(timeout=1.5 * REVOLUTION)\n"),
        tool("run_terminal_command",
             json.dumps({"command": "python -m station.cli lamp --selftest "
                                    "--warm"}, indent=2),
             "phase trusted, hunting skipped\nturntable phase: 0.00 deg\n"),
    ], when(29), metrics_json=metrics(318, 47_900, thinking=176, cached=40_000,
                                      cost=0.018))
    add_message(con, lamp, 21, "user", [text(
        "Then log the phase error on every start. I want to see the drift in the "
        "log before a coaster sees it from the water.")], when(31))
    add_message(con, lamp, 22, "assistant", [
        tool("str_replace", json.dumps({
            "path": "src/lamp.py",
            "old_string": "    self.run()\n",
            "new_string": "    self.log.info(\"phase %+.2f deg\", self.phase_error())\n"
                          "    self.run()\n"},
            indent=2), "applied 1 replacement"),
        change("src/lamp.py", 2, 0,
               "@@ -14,1 +14,2 @@\n+    self.log.info(\"phase %+.2f deg\", "
               "self.phase_error())\n     self.run()\n"),
        tool("run_terminal_command",
             json.dumps({"command": "python -m station.cli lamp --selftest "
                                    "--warm"}, indent=2),
             "phase +0.37 deg\nphase trusted, hunting skipped\n"),
    ], when(32), metrics_json=metrics(196, 51_900, thinking=88, cached=45_000))
    add_message(con, lamp, 23, "user", [text(
        "The keeper wants the flash pattern itself checked automatically, so a "
        "drift is caught before anyone radios us. Can the station do that?")],
        when(33))
    add_message(con, lamp, 24, "assistant", [
        thought("The character is published, so it can be measured: count the "
                "interval between the group of flashes and compare it with the "
                "chart. Two intervals of drift is worth a bell, more is worth a "
                "call."),
        text("Yes - measure the interval between flash groups and compare it with "
             "the published character, then ring when the difference passes two "
             "intervals."),
        tool("str_replace", json.dumps({
            "path": "src/lamp.py",
            "old_string": "    self.log.info(\"phase %+.2f deg\", self.phase_error())\n",
            "new_string": "    self.log.info(\"phase %+.2f deg\", self.phase_error())\n"
                          "    self.check_character()\n"},
            indent=2), "applied 1 replacement"),
        change("src/lamp.py", 8, 0,
               "@@ -14,2 +14,10 @@\n def check_character(self):\n"
               "+    measured = self.interval_between_groups()\n"
               "+    if abs(measured - PUBLISHED) > 2 * INTERVAL:\n"),
        tool("run_terminal_command",
             json.dumps({"command": "python -m station.cli lamp --selftest "
                                    "--character"}, indent=2),
             "measured 8.02 s, published 8.00 s -> within tolerance\n"),
        notice("The station tool has no simulated drift, so the alarm path was "
               "replayed from the log instead."),
    ], when(34), metrics_json=metrics(452, 56_400, thinking=264, cached=48_000,
                                      cost=0.026))

def comedian(con: sqlite3.Connection, path: str) -> None:
    label, workspace = "Comedian", path

    tuesday = add_thread(con, "com-tuesday", label, workspace,
                         "Jokes for the Tuesday set", "open", "idle",
                         when(40), when(44))
    add_message(con, tuesday, 25, "user", [text(
        "I am on at the Tuesday open mic and the room is half commuters. Give me "
        "three short jokes about driving in the rain. They have to survive being "
        "told badly, because I will tell them badly.")], when(40))
    add_message(con, tuesday, 26, "assistant", [
        thought("Commuter rooms reward recognition over wordplay, so the jokes "
                "should be about the thing they all did that evening. Short, one "
                "line of setup, and no punchline that needs a second thought."),
        tool("web_search", json.dumps({"query": "one liners about traffic in the "
                                                "rain for an open mic night"},
                                      indent=2),
             "1. rain driving one-liners (comedy forum thread, 214 replies)\n"
             "2. 40 short commute jokes (blog)\n"
             "3. open mic set lists: weather runs (archive)\n"),
        text("Three, in the order I would tell them. Start with the umbrella one "
             "to buy a laugh, keep the wipers one in the middle, and close with "
             "the bus stop joke because it is the shortest."),
        tool("write_file", json.dumps({
            "path": "bits/tuesday.md",
            "content": "# Tuesday, commuter room\n\n"
                       "1. Wipers: still the only thing on this car that works "
                       "faster the harder it rains.\n"
                       "2. Umbrella: bought one that survives the wind. It is "
                       "called a bin.\n"
                       "3. Bus stop: waited twenty minutes to get wet slower.\n"},
            indent=2), "wrote 244 characters"),
        change("bits/tuesday.md", 6, 0,
               "@@ -1,1 +1,7 @@\n+# Tuesday, commuter room\n+\n"
               "+1. Wipers: the only thing on this car that works faster...\n"),
    ], when(41), metrics_json=metrics(438, 31_400, thinking=252, cached=24_000,
                                      cost=0.021))
    add_message(con, tuesday, 27, "user", [text(
        "The bus stop one died. Nobody laughed and one man looked at me like I had "
        "cost him twenty minutes. Try again with a cheaper punchline.")], when(43))
    add_message(con, tuesday, 28, "assistant", [
        thought("It died because the punchline explains the premise instead of "
                "surprising it. The fix is to make the second half about somebody "
                "else, so the laugh lands on the situation rather than the "
                "performer's hopes."),
        text("Two rewrites. Both keep the setup you already told, so you can swap "
             "the ending without learning a new joke."),
        tool("str_replace", json.dumps({
            "path": "bits/tuesday.md",
            "old_string": "3. Bus stop: waited twenty minutes to get wet slower.",
            "new_string": "3. Bus stop: twenty minutes, got wet anyway, and the "
                          "bloke next to me was dry - he works from home.\n"
                          "4. Cheaper: my car has a rain mode. It is called "
                          "'parked'."},
            indent=2), "applied 1 replacement"),
        change("bits/tuesday.md", 4, 1,
               "@@ -6,1 +6,4 @@\n-3. Bus stop: waited twenty minutes to get "
               "wet slower.\n+3. Bus stop: ... he works from home.\n"
               "+4. Cheaper: my car has a rain mode. It is called 'parked'.\n"),
    ], when(44), metrics_json=metrics(286, 36_800, thinking=148, cached=29_000,
                                      cost=0.015))

    shop = add_thread(con, "com-shop", label, workspace,
                      "A joke for the shop's opening night", "closed", "idle",
                      when(50), when(56), archived=when(57))
    add_message(con, shop, 29, "user", [text(
        "My brother is opening a tyre shop on Friday and wants one joke for the "
        "speech before the ribbon. Nothing about his mother, he is sentimental "
        "about her and it would ruin the evening.")], when(50))
    add_message(con, shop, 30, "assistant", [
        thought("A ribbing that flatters the trade works better than a joke about "
                "the man. Everyone in the room has been overcharged for tyres, so "
                "the room is already on the punchline's side."),
        tool("web_search", json.dumps({"query": "short joke for opening a tyre "
                                                "shop speech"}, indent=2),
             "1. shop opening speech jokes (forum)\n"
             "2. tyre shop one liners (blog)\n"
             "3. ribbon cutting gags (archive)\n"),
        text("One to try: my brother has been fixing the same tyre for eleven "
             "years. It was never his. He just liked standing next to it. If that "
             "is too soft for the room, tell me and I will cut the last line."),
    ], when(51), metrics_json=metrics(324, 27_900, thinking=170, cached=21_000,
                                      cost=0.016))
    add_message(con, shop, 31, "user", [text(
        "Make it about the alignment machine, he loves that thing and it is the "
        "only equipment he talks about.")], when(53))
    add_message(con, shop, 32, "assistant", [
        thought("The machine pulls the car straight, so the joke should be about "
                "him needing the same treatment. Keep it under five lines so it "
                "can be told from a card."),
        text("Try this: he bought an alignment machine before he had a shop, a "
             "customer or a licence. He said you cannot trust a car that pulls "
             "left. His wife would like everyone to know the machine works, "
             "because it is the only thing in this family that has ever gone "
             "straight."),
        tool("str_replace", json.dumps({
            "path": "bits/shop-night.md",
            "old_string": "# Shop night\n",
            "new_string": "# Shop night\n\nThe alignment machine: bought before "
                          "the shop, the customer or the licence. Works.\n"},
            indent=2), "applied 1 replacement"),
        change("bits/shop-night.md", 4, 0,
               "@@ -1,1 +1,5 @@\n # Shop night\n+\n+The alignment machine: bought "
               "before the shop, the customer...\n"),
    ], when(54), metrics_json=metrics(356, 33_600, thinking=188, cached=26_000,
                                      cost=0.018))
    add_message(con, shop, 33, "user", [text(
        "He will use that one. Make it tighter - five lines maximum when I read "
        "it out loud, I do not want to stand there holding a card.")], when(55))
    add_message(con, shop, 34, "assistant", [
        thought("Cut the wife's sentence and the licence detail. The machine going "
                "straight while the family does not is the whole joke and it fits "
                "in four lines."),
        tool("write_file", json.dumps({
            "path": "bits/shop-night-final.md",
            "content": "He bought the alignment machine before the shop.\n"
                       "No customer, no licence, no premises.\n"
                       "But it goes straight, he said.\n"
                       "Only thing in this family that ever has.\n"},
            indent=2), "wrote 172 characters"),
        change("bits/shop-night-final.md", 4, 0,
               "@@ -1,1 +1,5 @@\n+He bought the alignment machine before the shop.\n"
               "+But it goes straight, he said.\n"),
    ], when(56), metrics_json=metrics(192, 38_200, thinking=96, cached=31_000,
                                      cost=0.013))

def mechanic(con: sqlite3.Connection, path: str) -> None:
    label, workspace = "Mechanic", path

    rattle = add_thread(con, "mech-rattle", label, workspace,
                        "Rattle at 40 km/h, 2011 Ranger", "open", "idle",
                        when(60), when(70))
    add_message(con, rattle, 35, "user", [text(
        "Customer's Ranger has a rattle that starts about 40 and is gone by 60. "
        "Ball joints and tie rod ends are new, I did them myself. Where do I look "
        "next before I start throwing parts at it?")], when(60))
    add_message(con, rattle, 36, "assistant", [
        thought("A noise that lives in one speed band and disappears is almost "
                "always something rotating: balance, a tyre with a bad belt, or a "
                "heat shield with one fixing left and its own resonance. A road "
                "test that loads one side at a time tells them apart cheaply."),
        text("Before parts: a road test that loads the steering left and then "
             "right at the speed it rattles, and a light brake application. Left "
             "load that silences it points at the right side hub or CV; steering "
             "that changes nothing leaves balance and the shield."),
        tool("read_files", json.dumps({"path": "data/job-cards/1042.md"}, indent=2),
             "# 1042 2011 Ranger\n- ball joints 4x new\n- tie rod ends new\n"
             "- rattle 40-60 km/h, customer report\n"),
        tool("web_search", json.dumps({"query": "rattle only at 40 km/h 2011 Ranger "
                                                "gone above 60"}, indent=2),
             "1. speed specific rattle diagnosis (workshop forum, 88 replies)\n"
             "2. wheel bearing vs balance symptoms (service bulletin summary)\n"
             "3. heat shield resonance at one road speed (article)\n"),
    ], when(61), metrics_json=metrics(452, 32_600, thinking=288, cached=25_000,
                                      cost=0.022))
    add_message(con, rattle, 37, "user", [text(
        "Road test done. It stops when I load the steering left, comes back on "
        "right load. Brakes change nothing. That tells you something specific, "
        "does it not?")], when(63))
    add_message(con, rattle, 38, "assistant", [
        thought("Loading left puts weight on the left front and unloads the right, "
                "so the noise lives on the right side and it is load sensitive - "
                "which is a bearing or a CV, not balance. Brakes changing nothing "
                "keeps it off the rotor."),
        text("Right front hub or its CV joint. Load sensitivity with no brake "
             "influence takes the rotor out of it; if the bearing is gritty by "
             "hand, that is your noise."),
        tool("read_files", json.dumps({"path": "service-manual/front-suspension.md"},
                                      indent=2),
             "## Hub and bearing\nCheck end float, rotate by hand, listen.\n"),
        tool("run_terminal_command",
             json.dumps({"command": "python -m bay4 specs --vehicle ranger-2011 "
                                    "--part wheel-bearing"}, indent=2),
             "hub nut: 290 Nm\nbearing type: sealed cartridge, press fit\n"
             "pair with: rear bearing inspection at 150,000 km\n"),
    ], when(64), metrics_json=metrics(396, 38_100, thinking=214, cached=31_000,
                                      cost=0.020))
    add_message(con, rattle, 39, "user", [text(
        "Pulled the right front hub. Bearing feels gritty and there is grease "
        "past the seal. What torque, and in what order do the bolts go back?")],
        when(66))
    add_message(con, rattle, 40, "assistant", [
        thought("The spec tool has the nut figure already; the calliper bracket and "
                "wheel sequence come from the manual. Write both onto the job card "
                "so nobody has to ask twice."),
        tool("run_terminal_command",
             json.dumps({"command": "python -m bay4 sequence --vehicle ranger-2011 "
                                    "--job hub-front"}, indent=2),
             "1. hub nut 290 Nm, stake\n2. calliper bracket 85 Nm\n"
             "3. wheel nuts 135 Nm, star pattern\n"),
        text("Hub nut 290 Nm and stake it, bracket 85, wheels 135 in a star. Grease "
             "past the seal means the seal goes in with the bearing, do not reuse "
             "it."),
        tool("write_file", json.dumps({
            "path": "data/job-cards/1042.md",
            "content": "# 1042 2011 Ranger\n- ball joints 4x new\n- tie rod ends "
                       "new\n- right front hub bearing, gritty, seal weeping\n"
                       "- torque: nut 290 Nm stake, bracket 85 Nm, wheels 135 Nm\n"},
            indent=2), "wrote 214 characters"),
        change("data/job-cards/1042.md", 3, 0,
               "@@ -3,3 +3,6 @@\n - tie rod ends new\n"
               "+- right front hub bearing, gritty, seal weeping\n"),
    ], when(67), metrics_json=metrics(418, 43_700, thinking=192, cached=36_000,
                                      cost=0.021))
    add_message(con, rattle, 41, "user", [text(
        "Back together and the noise is gone. I need to invoice it, and I want the "
        "job card to read the way it happened.")], when(69))
    add_message(con, rattle, 42, "assistant", [
        tool("str_replace", json.dumps({
            "path": "data/job-cards/1042.md",
            "old_string": "- torque: nut 290 Nm stake, bracket 85 Nm, wheels 135 Nm",
            "new_string": "- torque: nut 290 Nm stake, bracket 85 Nm, wheels 135 Nm\n"
                          "- road test: 40-60 km/h rattle gone, load both ways\n"
                          "- invoice: 1 hub bearing, seal, 1.6 h labour\n"},
            indent=2), "applied 1 replacement"),
        change("data/job-cards/1042.md", 3, 0,
               "@@ -6,1 +6,4 @@\n+- road test: rattle gone, load both ways\n"
               "+- invoice: 1 hub bearing, seal, 1.6 h labour\n"),
        text("Card now reads as the diagnosis, the fix and the road test. Invoice "
             "one bearing, one seal and 1.6 hours; the ball joints and rods are "
             "already on it from last week."),
    ], when(70), metrics_json=metrics(302, 47_500, thinking=142, cached=39_000,
                                      cost=0.017))
    add_message(con, rattle, 43, "user", [text(
        "Customer asked whether the left one should be done at the same time. What "
        "do I tell him? I do not want to sell him a bearing he does not need.")],
        when(71))
    add_message(con, rattle, 44, "assistant", [
        thought("Mirror wear is common but not a rule, and this one failed from "
                "load rather than age. The honest answer is to check the left by "
                "hand now, note it, and let the next service decide."),
        text("Tell him the truth: the left one is fine today. They do not fail as a "
             "pair, and this one died from a pothole, not from age. Check the left "
             "by hand, note it on the card, and let him choose at the next service."),
        tool("str_replace", json.dumps({
            "path": "data/job-cards/1042.md",
            "old_string": "- invoice: 1 hub bearing, seal, 1.6 h labour",
            "new_string": "- invoice: 1 hub bearing, seal, 1.6 h labour\n"
                          "- left front hub checked by hand: no play, advise at "
                          "next service\n"},
            indent=2), "applied 1 replacement"),
        change("data/job-cards/1042.md", 2, 0,
               "@@ -8,1 +8,3 @@\n+- left front hub checked by hand: no play\n"),
        tool("run_terminal_command",
             json.dumps({"command": "python -m bay4 history --vehicle ranger-2011 "
                                    "--part wheel-bearing"}, indent=2),
             "2024-03 left rear bearing replaced at 141,200 km\n"
             "2026-06 right front bearing replaced at 168,900 km\n"),
        text("For what it is worth, the history shows the left rear was done two "
             "years ago, so the left front is the only original one left."),
    ], when(72), metrics_json=metrics(438, 52_800, thinking=206, cached=44_000,
                                      cost=0.023))

    misfire = add_thread(con, "mech-misfire", label, workspace,
                         "P0301 back after a coil pack swap", "closed", "idle",
                         when(78), when(85))
    add_message(con, misfire, 45, "user", [text(
        "Put a new coil pack on cylinder 1 this morning and now the misfire is "
        "worse. P0301 came straight back. The coil and the plug are both new, so "
        "do not tell me to change them again.")], when(78))
    add_message(con, misfire, 46, "assistant", [
        thought("A misfire that follows a repair is nearly always the repair: a "
                "connector disturbed, a coil on the wrong cylinder, or a pigtail "
                "with a broken lock. The pair of codes will say which."),
        text("I will not. Check two things first: whether the coils went back on "
             "the right cylinders, and whether the connector you disturbed has a "
             "clean pin and a lock that clicks."),
        tool("read_files", json.dumps({"path": "data/job-cards/1055.md"}, indent=2),
             "# 1055\n- coil pack cylinder 1 new\n- spark plug new\n"
             "- misfire continues\n"),
        tool("web_search", json.dumps({"query": "P0301 after coil pack replacement "
                                                "still misfiring cylinder 1"},
                                      indent=2),
             "1. misfire after coil replacement: swapped coils (forum, 61 replies)\n"
             "2. P0301 with P0351 ignition coil primary circuit (article)\n"
             "3. coil connector corrosion causes repeat misfire (video notes)\n"),
    ], when(79), metrics_json=metrics(412, 29_800, thinking=234, cached=22_000,
                                      cost=0.020))
    add_message(con, misfire, 47, "user", [text(
        "Cleared and re-read. I get P0301 and P0351 together. What does that pair "
        "mean, in plain words?")], when(81))
    add_message(con, misfire, 48, "assistant", [
        thought("P0351 is the coil A primary circuit, which is the wiring and the "
                "driver rather than the spark. Together with P0301 it points at the "
                "connector, not the coil: the module commands a dwell and sees no "
                "current it can trust."),
        text("P0301 is the misfire, P0351 is the coil's own primary circuit. Pair "
             "them and the fault is the wire and the connector, not the coil. The "
             "module is not getting a current rise it believes."),
        tool("code_search", json.dumps({"pattern": "P03|ignition coil",
                                        "path": "data/codes"}, indent=2),
             "data/codes/faults.md:41: P0351 ignition coil A primary/secondary\n"
             "data/codes/faults.md:52: P0301 cylinder 1 misfire detected\n"),
        tool("run_terminal_command",
             json.dumps({"command": "python -m bay4 codes --pair P0301 P0351"},
                        indent=2),
             "likely: coil A circuit - connector, pigtail or driver\n"
             "unlikely: coil, plug, injector\n"),
    ], when(82), metrics_json=metrics(446, 35_400, thinking=252, cached=29_000,
                                      cost=0.022))
    add_message(con, misfire, 49, "user", [text(
        "Pulled the connector. The pin is green with corrosion and the lock is "
        "cracked. Clean it or replace the pigtail?")], when(84))
    add_message(con, misfire, 50, "assistant", [
        thought("A cleaned green pin with a cracked lock will come back, and the "
                "next person to see it will be the customer. Replace the pigtail "
                "and check pin drag with the meter before it goes together."),
        text("Replace it. Cleaning buys a month and the lock that holds it has "
             "already let go. Before you tape it up, measure the coil resistance "
             "so you know the new pigtail is not chasing an old fault."),
        tool("str_replace", json.dumps({
            "path": "data/job-cards/1055.md",
            "old_string": "- misfire continues",
            "new_string": "- misfire continues\n- P0301 + P0351, coil A primary "
                          "circuit\n- connector pin corroded, lock cracked, "
                          "pigtail replaced"},
            indent=2), "applied 1 replacement"),
        change("data/job-cards/1055.md", 4, 1,
               "@@ -4,1 +4,4 @@\n - misfire continues\n"
               "+- P0301 + P0351, coil A primary circuit\n"
               "+- pigtail replaced\n"),
        tool("run_terminal_command",
             json.dumps({"command": "python -m bay4 check --coil a --resistance"},
                        indent=2),
             "primary 0.62 ohm (spec 0.5-0.9)\nsecondary 6.4 kohm (spec 6-9)\n"),
    ], when(85), metrics_json=metrics(398, 40_900, thinking=214, cached=34_000,
                                      cost=0.020))
    add_message(con, misfire, 51, "user", [text(
        "Pigtail on, code cleared and it has stayed off through two drive cycles. "
        "Write it up and close the card, I am done with this truck.")], when(86))
    add_message(con, misfire, 52, "assistant", [
        tool("write_file", json.dumps({
            "path": "data/job-cards/1055.md",
            "content": "# 1055\n- coil pack cylinder 1 new\n- spark plug new\n"
                       "- misfire continues\n- P0301 + P0351, coil A primary "
                       "circuit\n- connector pin corroded, pigtail replaced\n"
                       "- resistance: primary 0.62 ohm, secondary 6.4 kohm\n"
                       "- cleared, two drive cycles clean, closed\n"},
            indent=2), "wrote 268 characters"),
        change("data/job-cards/1055.md", 3, 0,
               "@@ -8,1 +8,4 @@\n+- resistance: primary 0.62 ohm\n"
               "+- cleared, two drive cycles clean, closed\n"),
    ], when(87), metrics_json=metrics(226, 45_600, thinking=104, cached=38_000,
                                      cost=0.014))

    abs_light = add_thread(con, "mech-abs", label, workspace,
                           "ABS light after a wheel bearing", "closed", "idle",
                           when(92), when(98))
    add_message(con, abs_light, 53, "user", [text(
        "ABS and traction lights came on the minute I backed the car out after "
        "doing the right rear bearing. The sensor is clean, I looked at it twice.")],
        when(92))
    add_message(con, abs_light, 54, "assistant", [
        thought("A light right after a bearing job is the tone ring or the air gap. "
                "Aftermarket hubs sometimes ship with the wrong tooth count, which "
                "is invisible to the eye and obvious to the module."),
        text("Two things that look fine and are not: the air gap and the tooth "
             "count on the new ring. Count the teeth on the old hub if you still "
             "have it."),
        tool("read_files", json.dumps({"path": "data/job-cards/1061.md"}, indent=2),
             "# 1061\n- right rear hub and bearing new\n- ABS light after repair\n"),
        tool("web_search", json.dumps({"query": "ABS light immediately after rear "
                                                "wheel bearing replacement tone "
                                                "ring teeth"}, indent=2),
             "1. aftermarket hub wrong tone ring count (forum, 44 replies)\n"
             "2. air gap spec and ABS light after hub replacement (article)\n"
             "3. wheel speed mismatch sets traction light too (service notes)\n"),
    ], when(93), metrics_json=metrics(396, 31_200, thinking=218, cached=24_000,
                                      cost=0.020))
    add_message(con, abs_light, 55, "user", [text(
        "Old hub is in the scrap bin. Forty eight teeth. The new one has forty "
        "four. That is it, is it not?")], when(95))
    add_message(con, abs_light, 56, "assistant", [
        thought("Yes - the module derives speed from teeth per second, so four "
                "fewer teeth reads about nine percent slow and the traction light "
                "follows the ABS light. No amount of cleaning fixes a wrong ring."),
        text("That is it exactly. Forty four against forty eight reads about nine "
             "percent slow, which is enough for the module to distrust the wheel "
             "and light both lamps. Get the right hub, do not touch the sensor."),
        tool("run_terminal_command",
             json.dumps({"command": "python -m bay4 codes --read"}, indent=2),
             "C0035 right rear wheel speed sensor circuit\n"
             "implication: speed signal implausible, not missing\n", 1),
        notice("The code reader was not connected, so these codes were typed from "
               "the scan tool screen."),
    ], when(96), metrics_json=metrics(342, 36_600, thinking=176, cached=29_000,
                                      cost=0.018))
    add_message(con, abs_light, 57, "user", [text(
        "Supplier is exchanging it. Put it on the card so nobody here sells that "
        "part number again, and make a note I can hand to the counter.")], when(97))
    add_message(con, abs_light, 58, "assistant", [
        tool("str_replace", json.dumps({
            "path": "data/job-cards/1061.md",
            "old_string": "- ABS light after repair",
            "new_string": "- ABS light after repair\n- new hub ring 44 teeth, old "
                          "48, wrong part\n- supplier exchanging, do not refit "
                          "this part number"},
            indent=2), "applied 1 replacement"),
        change("data/job-cards/1061.md", 4, 1,
               "@@ -3,1 +3,4 @@\n - ABS light after repair\n"
               "+- new hub ring 44 teeth, old 48, wrong part\n"),
        tool("write_file", json.dumps({
            "path": "data/parts-watchlist.md",
            "content": "# Parts watchlist\n\n- Rear hub, right: ships with a 44 "
                       "tooth ring where the vehicle needs 48. Returns as C0035.\n"},
            indent=2), "wrote 152 characters"),
        change("data/parts-watchlist.md", 3, 0,
               "@@ -1,1 +1,4 @@\n+# Parts watchlist\n+\n+- Rear hub, right: 44 tooth "
               "ring, needs 48.\n"),
    ], when(98), metrics_json=metrics(268, 41_700, thinking=122, cached=34_000,
                                      cost=0.015))

    coolant = add_thread(con, "mech-coolant", label, workspace,
                         "Coolant for an aluminium radiator", "closed", "idle",
                         when(104), when(107), archived=when(108))
    add_message(con, coolant, 59, "user", [text(
        "2013 sedan, aluminium radiator and aluminium heads. The shop down the "
        "road says the green stuff is fine and they have used it for thirty years. "
        "What am I putting in this one?")], when(104))
    add_message(con, coolant, 60, "assistant", [
        thought("The old green coolant carries silicates that drop out and plate the "
                "aluminium, which shows up years later as a heater core. The "
                "manufacturer's specification for this car is a silicate free OAT, "
                "and the thirty years argument is about iron engines."),
        tool("web_search", json.dumps({"query": "silicate free coolant aluminium "
                                                "radiator old green coolant damage "
                                                "specification"}, indent=2),
             "1. OAT vs traditional green coolant, silicates and aluminium (article)\n"
             "2. Asian vehicle coolant specification list (reference table)\n"
             "3. heater core failures traced to wrong coolant (forum, 73 replies)\n"),
        tool("read_files", json.dumps({"path": "data/fluid-specs.md"}, indent=2),
             "## Coolant\n- aluminium systems: silicate free OAT only\n"
             "- never mix OAT with traditional green\n"),
        text("Silicate free OAT, and it is the manufacturer's specification, not a "
             "preference. The green stuff is not poison, it is the wrong chemistry "
             "for aluminium, and it fails in the heater core rather than the "
             "radiator, so nobody blames it until it is expensive."),
    ], when(105), metrics_json=metrics(486, 28_400, thinking=294, cached=21_000,
                                       cost=0.023))
    add_message(con, coolant, 61, "user", [text(
        "Customer is asking whether the other shop has already damaged it - it has "
        "had green in it for two years. What do I tell him?")], when(107))
    add_message(con, coolant, 62, "assistant", [
        thought("Two years of the wrong coolant is a flush and a watch, not a "
                "teardown. Tell him what to look for rather than what to fear: "
                "heater output, a sweet smell, level drop with no puddle."),
        text("Tell him it is a flush, not a repair, and give him three things to "
             "watch: heater output on a cold morning, a sweet smell after a run, "
             "and the level dropping with nothing under the car. Any of those and "
             "we pressure test it."),
        tool("str_replace", json.dumps({
            "path": "data/fluid-specs.md",
            "old_string": "- never mix OAT with traditional green",
            "new_string": "- never mix OAT with traditional green\n"
                          "- wrong coolant fitted: flush, refill, then watch heater "
                          "output, smell and level for one service"},
            indent=2), "applied 1 replacement"),
        changes(
            ("data/fluid-specs.md", 2, 0,
             "@@ -3,1 +3,2 @@\n+- wrong coolant fitted: flush, refill, watch.\n"),
            ("data/job-cards/1063.md", 24, 0,
             "@@ -0,0 +1,4 @@\n+# 1063 — coolant, wrong chemistry\n"
             "+Customer reports heater output down on cold mornings.\n"
             "+Green coolant in an aluminium system for two years.\n"
             "+Flush and refill with silicate free OAT, then watch.\n",
             "added"),
            ("service-manual/cooling.md", 5, 1,
             "@@ -12,2 +12,3 @@\n - traditional green: iron engines only\n"
             "-aluminium systems: silicate free OAT\n+-wrong coolant fitted: flush, then watch\n"),
        ),
    ], when(108), metrics_json=metrics(324, 33_400, thinking=164, cached=27_000,
                                       cost=0.017))

    board = add_thread(con, "mech-board", label, workspace,
                       "End of day, what is left in the bay", "open", "working",
                       when(114), when(115))
    add_message(con, board, 63, "user", [text(
        "Before I lock up: list what is still open on the board and flag anything "
        "that cannot wait until morning. Two customers are picking up at eight.")],
        when(114))
    add_message(con, board, 64, "assistant", [
        thought("The board tool is the source of truth, and anything that blocks an "
                "eight o'clock pickup has to be named before anything else."),
        tool("run_terminal_command",
             json.dumps({"command": "python -m bay4 board --open"}, indent=2),
             "1042 Ranger: road test done, awaiting customer 08:00\n"
             "1055 truck: closed\n1061 sedan: awaiting part, supplier AM\n"
             "1069 van: diagnostics not started, customer 08:00\n"),
        text("Two at eight: the Ranger, which is finished and only needs handing "
             "over, and the van, which has not been looked at. The sedan is waiting "
             "on the hub and the supplier is calling in the morning. The van is "
             "the one that can bite you."),
    ], when(115), metrics_json=metrics(268, 37_800, thinking=138, cached=30_000,
                                       cost=0.015))
    add_message(con, board, 65, "user", [text(
        "Right. Put the Ranger's rear brakes on the board as waiting on parts and "
        "I will close it up.")], when(115))

def build(root: Path, port: int) -> dict:
    profile = root / PROFILE
    projects_root = profile / "projects"
    if projects_root.exists():
        shutil.rmtree(projects_root)
    projects_root.mkdir(parents=True, exist_ok=True)

    writers = []
    try:
        for label, path in PROJECTS:
            writers.append((label, open_project(projects_root, label, path)))
        by_label = dict(writers)
        lighthouse(by_label["Lighthouse"], PROJECTS[0][1])
        comedian(by_label["Comedian"], PROJECTS[1][1])
        mechanic(by_label["Mechanic"], PROJECTS[2][1])
        anchor(writers)
    finally:
        for _label, con in writers:
            con.commit()
            con.execute("pragma wal_checkpoint(truncate)")
            con.execute("pragma journal_mode=delete")
            con.close()

    config_path = root / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({
        "server": {"host": "127.0.0.1", "port": port, "open_browser": True},
        "data": {"freebuff_config_root": str(profile.resolve())},
        "index": {"enabled": False},
    }, indent=2) + "\n", encoding="utf-8", newline="\n")
    return {"root": root, "profile": profile, "config": config_path}

def main(argv: list[str] | None = None) -> int:
    found = argparse.ArgumentParser(description="Build a mock Freebuff Desktop profile")
    found.add_argument("--root", default=DEFAULT_ROOT,
                       help=f"where the profile goes (default {DEFAULT_ROOT})")
    found.add_argument("--port", type=int, default=8771,
                       help="the port the written config.json uses (default 8771)")
    args = found.parse_args(argv)
    built = build(Path(args.root), args.port)
    print(f"profile : {built['profile'].resolve()}")
    for label, path in PROJECTS:
        print(f"  project {label:11s} {path}")
    print(f"config  : {built['config'].resolve()}")
    launcher = "run.cmd" if sys.platform == "win32" else "./run.sh"
    print(f"run     : {launcher} --config {built['config']}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
