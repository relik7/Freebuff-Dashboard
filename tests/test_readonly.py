from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fixture
import harness
import testlog
from dashboard import search as search_module
from dashboard.build_id import source_files
from dashboard.fleet import READ_ACTIONS, Fleet, read_only_uri

ROOT = Path(__file__).resolve().parent.parent
DATA = testlog.work_dir("readonly")
CHILD = "readonly-server"

failures: list[str] = []

def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {label}")
    else:
        failures.append(label)
        print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))

def fingerprint(path: Path) -> tuple:
    stat = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return stat.st_size, stat.st_mtime_ns, digest

def call(port: int, path: str, params: dict | None = None,
         method: str = "GET") -> int:
    url = f"http://127.0.0.1:{port}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code

def session(port: int, facts: dict) -> list[str]:
    done: list[str] = []
    codes = {
        "projects": call(port, "/api/projects"),
        "search words": call(port, "/api/search", {
            "q": "retry backoff", "mode": "words", "scope": "all",
            "role": "user,assistant"}),
        "search exact": call(port, "/api/search", {
            "q": "%callreports%", "mode": "exact", "scope": "all",
            "role": "user,assistant"}),
        "search one role": call(port, "/api/search", {
            "q": "DB", "mode": "exact", "scope": "all", "role": "user"}),
        "search empty q": call(port, "/api/search", {"q": "", "mode": "words"}),
        "status": call(port, "/api/status"),
        "running": call(port, "/api/running"),
        "refresh": call(port, "/api/refresh", method="POST"),
        "page": call(port, "/"),
        "css": call(port, "/app.css"),
        "js": call(port, "/app.js"),
        "activity board": call(port, "/activity"),
        "board css": call(port, "/activity.css"),
        "board js": call(port, "/activity.js"),
    }
    for name, code in codes.items():
        done.append(f"{name} -> {code}")
    for name, thread in facts["threads"].items():
        code = call(port, "/api/thread",
                    {"project": thread["project"], "thread": thread["thread_id"]})
        done.append(f"thread {name} -> {code}")
    return done

def code_strings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings]

def main() -> int:
    print("=== read-only: a full session, hashed before and after ===")
    facts = fixture.build(DATA)
    alpha = facts["threads"]["alpha_open"]

    decoy = Path(facts["root"]) / "state.json"
    decoy_bytes = json.dumps({"authSessions": {"token": "decoy-never-served"}})
    decoy.write_text(decoy_bytes, encoding="utf-8", newline="\n")

    every = sorted((Path(facts["root"]) / "projects").glob("*/desktop-v2.db*"))

    sources = [path for path in every if not path.name.endswith("-shm")]
    shared = [path for path in every if path.name.endswith("-shm")]
    check("the fixture has source databases with a live WAL to protect",
          len(sources) >= 4 and any(path.name.endswith("-wal") for path in sources),
          str([path.name for path in every]))
    before = {path: fingerprint(path) for path in sources}
    before_shared = {path: path.stat().st_size for path in shared}

    port = harness.free_port()
    config = fixture.config_for(DATA, server={"port": port, "open_browser": False})
    config_path = fixture.write_config(DATA, config)
    log = testlog.child_log(CHILD)
    child = harness.start_child(CHILD, ["fb-dashboard.py", "--config",
                                        str(config_path)], root=ROOT)
    requests: list[str] = []
    try:
        if not harness.wait_for_log(log, f"serving on http://127.0.0.1:{port}", 40):
            check("the server started for the session", False, f"see {log}")
            return 1
        requests = session(port, facts)
        print(f"      session: {len(requests)} request(s),"
              f" {len(sources)} source file(s)")
        check("the session actually reached every endpoint",
              all(line.rstrip().endswith("200") for line in requests),
              "; ".join(line for line in requests
                        if not line.rstrip().endswith("200")))
    finally:
        harness.stop_child(child, label="the server")
        harness.reap_survivors(everything=True)

    after = {path: fingerprint(path) for path in sources}
    changed = [path for path in sources if before[path] != after[path]]
    check("no source database or WAL changed size, mtime or bytes across the"
          " session",
          not changed,
          f"{[p.name for p in changed]}: {[(before[p], after[p]) for p in changed]}")
    grew = [path.name for path in shared
            if path.stat().st_size != before_shared[path]]
    check("what a reader may update (`-shm`) did not even change size, so no"
          " checkpoint ran",
          not grew, ", ".join(grew))
    check("the decoy state.json was never rewritten",
          decoy.read_text(encoding="utf-8") == decoy_bytes,
          decoy.read_text(encoding="utf-8")[:80])

    fleet = Fleet(config)
    try:
        answer = search_module.search(fleet, search_module.Filters(
            q="retry backoff", mode="words", scope="all",
           ))
        check("the in-process search answers through the read-only connection",
              answer["total"] >= 1, str(answer)[:160])
        first = fleet.projects[0]
        fleet.read(first, lambda connection: connection.execute(
            f"select count(*) from {first.key}.messages").fetchall())
        actions = {action for action, _, _, _ in fleet.audit}
        check("every action the authorizer saw was a read",
              actions <= READ_ACTIONS,
              f"unexpected: {sorted(actions - READ_ACTIONS)}")
        denied = []
        for statement in (
                f"insert into {fleet.projects[0].key}.messages"
                f" (seq, thread_id, role, parts_json, ts) values (99,'t','user','[]',1)",
                f"update {fleet.projects[0].key}.threads set title = 'nope'",
                f"delete from {fleet.projects[0].key}.messages",
                f"drop table {fleet.projects[0].key}.messages",
                f"pragma {fleet.projects[0].key}.journal_mode = delete"):
            try:
                fleet.read(first, lambda connection, statement=statement:
                           connection.execute(statement))
                denied.append(f"ALLOWED: {statement}")
            except Exception:
                pass
        check("a write, an update, a delete, a drop and a pragma are all denied",
              not denied, "; ".join(denied))
    finally:
        fleet.close()

    final = {path: fingerprint(path) for path in sources}
    check("the in-process queries changed nothing either",
          final == after,
          str([path.name for path in sources if final[path] != after[path]]))
    print(f"      {len(sources)} source file(s) hashed before and after;"
          f" {len(shared)} shm file(s) size-checked")

    named = []
    for path in source_files(ROOT):
        if path.suffix != ".py":
            continue
        if any("state.json" in value for value in code_strings(path)):
            named.append(str(path.relative_to(ROOT)))
    check("no shipped module carries `state.json` in code (only in prose)",
          not named, ", ".join(named))
    check("the session opened every fixture thread, not just the first",
          all(any(line.startswith(f"thread {name} ->") and line.endswith("200")
                  for line in requests) for name in facts["threads"]),
          str(requests[-4:]))
    check("the session covered both search modes and both role filters",
          len(requests) >= 14, str(requests))

    risky = "hostile #%'" + ("" if os.name == "nt" else "?")
    folder = DATA / risky
    folder.mkdir(parents=True, exist_ok=True)
    hostile = folder / "a #%'.db"
    made = sqlite3.connect(hostile)
    made.execute("create table t(x)")
    made.execute("insert into t values (1)")
    made.commit()
    made.close()
    uri = read_only_uri(hostile)
    guarded = sqlite3.connect(uri, uri=True)
    rows = guarded.execute("select count(*) from t").fetchone()[0]
    path_part = uri[len("file:"):-len("?mode=ro")]
    check("a path carrying a space, a hash, a percent and a quote reaches "
          "its own database through the read-only URI: the URI decodes back "
          "to that exact path and keeps its own query",
          rows == 1 and uri.startswith("file:") and uri.endswith("?mode=ro")
          and urllib.parse.unquote(path_part) == hostile.as_posix()
          and " " not in path_part and "#" not in path_part,
          uri)
    refused = ""
    try:
        guarded.execute("insert into t values (2)")
    except sqlite3.Error as exc:
        refused = str(exc)
    check("the same URI still carries mode=ro, so a write is refused",
          "readonly" in refused or "read-only" in refused,
          refused or "the write went through")
    guarded.close()
    shutil.rmtree(folder, ignore_errors=True)

    print()
    if failures:
        print(f"FAIL: {len(failures)} read-only check(s) failed")
        return 1
    print("ALL READ-ONLY TESTS PASS")
    return 0

STAMP_S = 0.7
BUDGET_S = 1.1

if __name__ == "__main__":
    testlog.start("test_readonly")
    try:
        code = main()
    finally:
        fixture.close()
    testlog.finish(code == 0, extras=[testlog.child_log(CHILD)])
    sys.exit(code)
