from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from pathlib import Path

BASE_TS = 1_790_000_000_000
MINUTE = 60_000

BIG_TEXT_CHARS = 1_048_576

PROJECTS = (
    {"label": "Alpha", "path": "C:/work/Alpha"},
    {"label": "Beta", "path": "C:/work/Beta"},
    {"label": "Gamma", "path": "C:/work/Gamma"},
)

BROKEN = {"label": "Delta", "path": "C:/work/Delta"}

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
  byok_connection TEXT,
  created_at INTEGER,
  updated_at INTEGER,
  sidebar_archived_at INTEGER,
  last_turn_finished_at INTEGER
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

WRITERS: list[sqlite3.Connection] = []

def ts(step: int) -> int:
    return BASE_TS + step * MINUTE

def text(body: str) -> dict:
    return {"kind": "text", "text": body}

def blank() -> list[dict]:
    return [text("\n\n"), text("   "), text("")]

def tool(name: str, command: str, output: str, exit_code: int = 0) -> dict:
    return {"kind": "tool", "toolName": name, "input": command, "output": output,
            "exitCode": exit_code, "status": "ok" if exit_code == 0 else "failed"}

def reasoning(body: str) -> dict:
    return {"kind": "reasoning", "text": body, "open": False, "collapse": True}

def usage(written: int, *, thinking: int = 0, sent: int = 0, cached: int = 0,
          used: int = 0, window: int = 1_048_576, cost: float | None = None,
          incomplete: bool = False, compactions: int = 0) -> dict:
    found: dict = {
        "context": {"usedTokens": used, "compactionThresholdTokens": 400_000,
                    "windowTokens": window},
        "compactions": [{}] * compactions,
        "usage": {"inputTokens": sent, "cachedInputTokens": cached,
                  "outputTokens": written, "reasoningOutputTokens": thinking,
                  "totalTokens": sent + written},
    }
    if cost is not None:
        found["costUsd"] = cost
    if incomplete:
        found["usageIncomplete"] = True
    return found

def ad() -> dict:
    return {"kind": "ad", "ad": {
        "title": "Sponsored title never to be rendered",
        "adText": "sponsored body never to be indexed",
        "url": "https://ads.example/sponsored",
        "impUrl": "https://ads.example/impression"}}

def notice(body: str) -> dict:
    return {"kind": "notice", "notice": body, "text": body}

def changes(path: str) -> dict:
    return {"kind": "changes", "files": [
        {"path": path, "status": "modified", "adds": 12, "dels": 3,
         "patch": "@@ -1,3 +1,3 @@\n-old line\n+new line\n"}]}

def changes_many(records) -> dict:
    return {"kind": "changes", "files": [
        {"path": path, "status": status, "adds": adds, "dels": dels,
         "patch": "@@ -1,3 +1,3 @@\n-old line\n+new line\n"}
        for path, status, adds, dels in records]}

def _open_project(root: Path, label: str, workspace: str, *,
                  broken: bool = False) -> tuple[Path, sqlite3.Connection | None]:
    directory = root / f"{label}-{uuid.uuid5(uuid.NAMESPACE_URL, label)}"
    directory.mkdir(parents=True, exist_ok=True)
    meta = directory / "project.json"
    if broken:
        meta.write_text("{ this is not JSON at all", encoding="utf-8", newline="\n")
        return directory, None
    meta.write_text(json.dumps({
        "version": 1,
        "projectId": str(uuid.uuid5(uuid.NAMESPACE_URL, label)),
        "projectPath": workspace,
        "database": "desktop-v2.db"}), encoding="utf-8", newline="\n")
    con = sqlite3.connect(directory / "desktop-v2.db")
    con.executescript(SCHEMA)
    con.execute("pragma journal_mode=wal")
    con.execute("pragma wal_autocheckpoint=0")
    con.execute("insert into projects (id, path, created_at) values (?,?,?)",
                (str(uuid.uuid5(uuid.NAMESPACE_URL, label)), workspace, ts(0)))
    WRITERS.append(con)
    return directory, con

def _thread(con: sqlite3.Connection, thread_id: str, project_id: str,
            workspace: str, title: str, status: str, turn_state: str,
            created: int, updated: int, archived: int = 0) -> None:
    con.execute(
        "insert into threads (id, project_id, project_path, title, status,"
        " turn_state, model, created_at, updated_at, sidebar_archived_at)"
        " values (?,?,?,?,?,?,?,?,?,?)",
        (thread_id, project_id, workspace, title, status, turn_state, "claude",
         created, updated, archived or None))

def _message(con: sqlite3.Connection, thread_id: str, seq: int, role: str,
             parts: list[dict], when: int, *, attachments: list | None = None,
             metrics: dict | None = None) -> None:
    con.execute(
        "insert into messages (seq, thread_id, request_id, input_id, role,"
        " parts_json, attachments_json, metrics_json, ts) values (?,?,?,?,?,?,?,?,?)",
        (seq, thread_id, None, f"in-{seq}" if role == "user" else None, role,
         json.dumps(parts, ensure_ascii=False),
         json.dumps(attachments or [], ensure_ascii=False),
         json.dumps(metrics or {}, ensure_ascii=False), when))

def finish_turn(root: Path, label: str, thread_id: str, when: int,
                reply: str) -> None:
    directory = (Path(root) / "projects"
                 / f"{label}-{uuid.uuid5(uuid.NAMESPACE_URL, label)}")
    con = sqlite3.connect(directory / "desktop-v2.db")
    try:
        con.execute(
            "insert into messages (thread_id, request_id, input_id, role,"
            " parts_json, attachments_json, metrics_json, ts)"
            " values (?,?,?,?,?,?,?,?)",
            (thread_id, None, None, "assistant",
             json.dumps([text(reply)], ensure_ascii=False), "[]", "{}", when))
        con.execute("update threads set turn_state='idle', last_turn_finished_at=?"
                    " where id=?", (when, thread_id))
        con.commit()
    finally:
        con.close()

def prose_parts(parts: list[dict]) -> int:
    return sum(1 for part in parts
               if part.get("kind") == "text"
               and (part.get("text") or "").strip())

def _count(parts: list[dict], role: str, counts: dict) -> None:
    for part in parts:
        kind = part.get("kind")
        if kind == "text":
            body = part.get("text") or ""
            counts["text_parts"] += 1
            if body == "":
                counts["empty_parts"] += 1
            elif body.strip() == "":
                counts["whitespace_only_parts"] += 1
                if set(body) <= {"\n"}:
                    counts["newline_only_parts"] += 1
            else:
                counts["nonblank_text_parts"] += 1
                if role == "user":
                    counts["user_prose_parts"] += 1
        elif kind == "tool":
            counts["tool_parts"] += 1
        elif kind == "reasoning":
            counts["reasoning_parts"] += 1
        elif kind == "changes":
            counts["changes_parts"] += 1
        elif kind == "ad":
            counts["ad_parts"] += 1
        elif kind == "notice":
            counts["notice_parts"] += 1

def config_for(root: Path, **overrides) -> dict:
    from dashboard import config as config_module

    found = config_module.load()
    found["data"]["freebuff_config_root"] = str(root)
    found["data"]["watch_seconds"] = 0
    found["index"]["enabled"] = False
    found["server"]["open_browser"] = False
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(found.get(key), dict):
            found[key].update(value)
        else:
            found[key] = value
    return found

def write_config(root: Path, config: dict, name: str = "config.json") -> Path:
    path = Path(root) / name
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8",
                    newline="\n")
    return path

def close() -> None:
    while WRITERS:
        try:
            WRITERS.pop().close()
        except sqlite3.Error:
            pass

def build(root: Path) -> dict:
    root = Path(root)
    close()
    projects_root = root / "projects"
    if projects_root.exists():
        shutil.rmtree(projects_root)
    projects_root.mkdir(parents=True, exist_ok=True)

    counts = {"text_parts": 0, "nonblank_text_parts": 0, "empty_parts": 0,
              "whitespace_only_parts": 0, "newline_only_parts": 0,
              "user_prose_parts": 0, "tool_parts": 0, "reasoning_parts": 0,
              "changes_parts": 0, "ad_parts": 0, "notice_parts": 0,
              "user_messages": 0}

    metrics_written: dict[int, dict] = {}

    per_thread: dict[str, int] = {}
    facts = {"root": str(root), "projects": [], "threads": {}, "seq": {},
             "counts": counts, "metrics": metrics_written, "thread_prose": per_thread}

    alpha_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "Alpha"))
    beta_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "Beta"))
    alpha_dir, alpha = _open_project(projects_root, "Alpha", PROJECTS[0]["path"])
    beta_dir, beta = _open_project(projects_root, "Beta", PROJECTS[1]["path"])
    gamma_dir, gamma = _open_project(projects_root, "Gamma", PROJECTS[2]["path"])
    delta_dir, _ = _open_project(projects_root, BROKEN["label"], BROKEN["path"],
                                 broken=True)
    for label, directory in (("Alpha", alpha_dir), ("Beta", beta_dir),
                             ("Gamma", gamma_dir), ("Delta", delta_dir)):
        facts["projects"].append({
            "label": label, "directory": str(directory),
            "path": next(project["path"] for project in (*PROJECTS, BROKEN)
                         if project["label"] == label)})

    open_thread = "11111111-1111-4111-8111-111111111111"
    running_thread = "22222222-2222-4222-8222-222222222222"
    closed_thread = "33333333-3333-4333-8333-333333333333"
    beta_thread = "44444444-4444-4444-8444-444444444444"
    archived_thread = "55555555-5555-4555-8555-555555555555"

    _thread(alpha, open_thread, alpha_id, PROJECTS[0]["path"], "Open Thread",
            "open", "idle", ts(0), ts(9))
    _thread(alpha, running_thread, alpha_id, PROJECTS[0]["path"],
            "Running Thread", "open", "working", ts(10), ts(11))
    _thread(alpha, closed_thread, alpha_id, PROJECTS[0]["path"], "Closed Thread",
            "closed", "idle", ts(12), ts(13))
    _thread(alpha, archived_thread, alpha_id, PROJECTS[0]["path"],
            "Archived Thread", "open", "idle", ts(14), ts(15), archived=ts(16))
    _thread(beta, beta_thread, beta_id, PROJECTS[1]["path"], "Other Project",
            "open", "idle", ts(20), ts(24))

    batches = [
        (1, "user", [text("Please retry the backoff logic")], ts(1), None, None),
        (2, "assistant", [
            text("First paragraph of the reply."),
            text("\n\n"),
            text("Second paragraph with fake_media inside."),
            tool("bash", "ls -la /tmp", "retry backoff file.txt", 0),
            reasoning("private thinking about fake_media\nsecond thought"),
            changes("C:/work/Alpha/src/main.py"),
        ], ts(2), None,
         usage(3_063, thinking=1_070, sent=114_440, cached=101_575, used=25_393,
               cost=0.0)),
        (3, "user", [text("A note about retry only")], ts(3), None,
         usage(11, sent=2_000, cost=0.0)),
        (4, "user", [text("quote ' and \" and 100% coverage and snake_case_name "
                          "and a--b and \U0001F680 and CRLF\r\nsecond line")],
         ts(4), [{"name": "diagram.png", "kind": "image",
                  "path": "C:/tmp/diagram.png"}], None),
        (5, "assistant", blank(), ts(5), None, None),
        (6, "assistant", [text("<script>alert(1)</script> nothing from the "
                               "transcript is markup")], ts(6), None, None),
        (7, "assistant", [text("A paragraph before the card."), ad()],
         ts(7), None, None),
        (8, "assistant", [notice("freebuff-session-ended"),
                          text("Everything after the notice.")], ts(8), None, None),
    ]
    for seq, role, parts, when, attachments, metrics in batches:
        _message(alpha, open_thread, seq, role, parts, when,
                 attachments=attachments, metrics=metrics)
        _count(parts, role, counts)
        per_thread[open_thread] = per_thread.get(open_thread, 0) + prose_parts(parts)
        if metrics is not None:
            metrics_written[seq] = metrics
        if role == "user":
            counts["user_messages"] += 1

    shared = [
        (11, "user", [text("shared timestamp prompt")], ts(10),
         usage(11, sent=2_000, cost=0.0)),
        (12, "assistant", [text("shared timestamp reply")], ts(10),
         usage(1_200, thinking=200, sent=8_000, cached=7_000, used=17_530,
               incomplete=True)),
        (13, "user", [text("shared timestamp follow-up")], ts(10), None),
    ]
    for seq, role, parts, when, metrics in shared:
        _message(alpha, running_thread, seq, role, parts, when, metrics=metrics)
        _count(parts, role, counts)
        per_thread[running_thread] = (per_thread.get(running_thread, 0)
                                      + prose_parts(parts))
        if metrics is not None:
            metrics_written[seq] = metrics
        if role == "user":
            counts["user_messages"] += 1

    for seq, role, parts, when in (
            (21, "user", [text("closed thread prompt")], ts(12)),
            (22, "assistant", [text("closed thread reply"),
                               changes_many((
                                   ("C:/work/Alpha/src/ledger.py",
                                    "modified", 9, 4),
                                   ("C:/work/Alpha/docs/quay.md",
                                    "added", 26, 0))),
                               text("A line after the two-file card.")], ts(12))):
        _message(alpha, closed_thread, seq, role, parts, when)
        _count(parts, role, counts)
        per_thread[closed_thread] = (per_thread.get(closed_thread, 0)
                                     + prose_parts(parts))
        if role == "user":
            counts["user_messages"] += 1

    beta_batches = [
        (31, "user", [text("make no changes to that DB")], ts(21)),
        (32, "assistant", [text(".db-wal and --suites and %callreports% literal "
                               "and snakeXcaseXname")], ts(22)),
        (33, "user", [text("callreportsXXX has no wildcards around it")], ts(23)),
        (34, "assistant", [text("x" * BIG_TEXT_CHARS)], ts(24)),
    ]
    for seq, role, parts, when in beta_batches:
        _message(beta, beta_thread, seq, role, parts, when)
        _count(parts, role, counts)
        per_thread[beta_thread] = per_thread.get(beta_thread, 0) + prose_parts(parts)
        if role == "user":
            counts["user_messages"] += 1

    for seq, role, parts, when in (
            (41, "user", [text("archived thread prompt")], ts(15)),
            (42, "assistant", [text("archived thread reply")], ts(15))):
        _message(alpha, archived_thread, seq, role, parts, when)
        _count(parts, role, counts)
        per_thread[archived_thread] = (per_thread.get(archived_thread, 0)
                                       + prose_parts(parts))
        if role == "user":
            counts["user_messages"] += 1

    for connection in (alpha, beta, gamma):
        if connection is not None:
            connection.commit()

    facts["threads"] = {
        "alpha_open": {"project": "Alpha", "thread_id": open_thread,
                       "title": "Open Thread", "status": "open"},
        "alpha_running": {"project": "Alpha", "thread_id": running_thread,
                          "title": "Running Thread", "status": "open"},
        "alpha_closed": {"project": "Alpha", "thread_id": closed_thread,
                         "title": "Closed Thread", "status": "closed"},
        "alpha_archived": {"project": "Alpha", "thread_id": archived_thread,
                           "title": "Archived Thread", "status": "open"},
        "beta_open": {"project": "Beta", "thread_id": beta_thread,
                      "title": "Other Project", "status": "open"},
    }
    facts["diffs"] = {
        "one_file": {"path": "C:/work/Alpha/src/main.py", "adds": 12,
                     "dels": 3, "files": 1},
        "two_files": {"files": 2, "adds": 35, "dels": 4,
                      "added": "C:/work/Alpha/docs/quay.md"},
    }
    facts["seq"] = {"alpha_prompt": 1, "alpha_reply": 2, "alpha_retry_only": 3,
                    "alpha_marks": 4, "alpha_blank": 5, "alpha_markup": 6,
                    "alpha_ad": 7, "alpha_notice": 8,
                    "running_prompt": 11, "running_reply": 12,
                    "running_after": 13,
                    "closed_prompt": 21, "closed_reply": 22,
                    "archived_prompt": 41, "archived_reply": 42,
                    "beta_phrase": 31, "beta_literal": 32,
                    "beta_decoy": 33, "beta_big": 34}
    facts["expected"] = counts
    return facts
