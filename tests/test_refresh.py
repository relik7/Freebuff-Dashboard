from __future__ import annotations

import json
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fixture
import harness
import testlog
from dashboard import app
from dashboard.fleet import Fleet

ROOT = Path(__file__).resolve().parent.parent
DATA = testlog.work_dir("refresh")
CHILD = "refresh-server"

STREAM_WAIT_S = 15.0

WATCH_S = 0.5

DISCONNECT_BEATS = 4

DISCONNECT_WAIT_S = WATCH_S * DISCONNECT_BEATS

NEW_THREAD = "aaaa1111-1111-4111-8111-111111111111"
PUSHED_THREAD = "bbbb2222-2222-4222-8222-222222222222"
NEW_PROJECT = "Aaa-00000000-0000-5000-8000-000000000001"
NEW_PROJECT_THREAD = "cccc3333-3333-4333-8333-333333333333"
NEW_WORKSPACE = "C:/work/Aaa"

failures: list[str] = []

def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {label}")
    else:
        failures.append(label)
        print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))

def write_to(database: Path, statements: list[tuple]) -> None:
    con = sqlite3.connect(database)
    try:
        con.execute("pragma busy_timeout = 2000")
        for sql, params in statements:
            con.execute(sql, params)
        con.commit()
    finally:
        con.close()

THREAD_INSERT = ("insert into threads (id, project_id, project_path, title,"
                 " status, turn_state, model, created_at, updated_at,"
                 " sidebar_archived_at) values (?,?,?,?,?,?,?,?,?,NULL)")

MESSAGE_INSERT = ("insert into messages (thread_id, role, parts_json,"
                  " attachments_json, metrics_json, ts) values (?,?,?,?,?,?)")

def add_thread(database: Path, ident: str, title: str, status: str = "open",
               turn_state: str = "idle") -> None:
    write_to(database, [(THREAD_INSERT,
                         (ident, "pid", "C:/work/Alpha", title, status,
                          turn_state, "claude", 1, 1))])

def add_message(database: Path, ident: str, body: str) -> None:
    write_to(database, [(MESSAGE_INSERT,
                         (ident, "user", json.dumps([{"kind": "text",
                                                      "text": body}]),
                          "[]", "{}", 1))])

def touch_thread(database: Path, ident: str, title: str, status: str,
                 turn_state: str) -> None:
    write_to(database, [("update threads set title=?, status=?, turn_state=?,"
                         " updated_at=updated_at+1 where id=?",
                         (title, status, turn_state, ident))])

def add_project(root: Path, name: str, workspace: str, ident: str) -> Path:
    directory = root / "projects" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "project.json").write_text(
        json.dumps({"version": 1, "projectId": name, "projectPath": workspace,
                    "database": "desktop-v2.db"}),
        encoding="utf-8", newline="\n")
    database = directory / "desktop-v2.db"
    con = sqlite3.connect(database)
    try:
        con.executescript(fixture.SCHEMA)
        con.execute("pragma journal_mode=wal")
        con.commit()
    finally:
        con.close()
    add_thread(database, ident, "Aaa Thread")
    add_message(database, ident, "the first thing said in Aaa")
    return database

def get_json(port: int, path: str, params: dict | None = None,
             method: str = "GET") -> tuple:
    url = f"http://127.0.0.1:{port}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"_body": body[:200]}

def open_stream(port: int) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.sendall(f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                 f"Accept: text/event-stream\r\n\r\n".encode("ascii"))
    return sock

def read_until(sock: socket.socket, needle: bytes, timeout: float) -> bytes:
    sock.settimeout(0.5)
    deadline = time.monotonic() + timeout
    seen = b""
    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        seen += chunk
        if needle in seen:
            break
    return seen

def main() -> int:
    print("=== the refresh: what the sources did since the last look ===")
    fixture.build(DATA)
    fleet = Fleet(fixture.config_for(DATA))
    child = None
    try:
        alpha = fleet.by_label("Alpha")
        check("the fixture has the project this gate mutates",
              alpha is not None and alpha.readable, str(alpha))
        if alpha is None:
            return 1
        first_keys = {project.directory.name: project.key
                      for project in fleet.projects}
        print(f"      {len(first_keys)} project(s), keys "
              f"{sorted(first_keys.values())}")

        add_thread(alpha.database, NEW_THREAD, "New thread")
        lines = fleet.refresh()
        check("a thread with no messages yet is not a thread this tool shows",
              fleet.by_thread(NEW_THREAD) is None
              and not any(NEW_THREAD in line for line in lines), str(lines))

        add_message(alpha.database, NEW_THREAD, "Increase Message Area")
        lines = fleet.refresh()
        found = fleet.by_thread(NEW_THREAD)
        check("the moment its first message lands, a thread created after the "
              "startup look is listed",
              found is not None and found[1].messages == 1, str(lines))
        check("...and the change says which thread moved, so the log and the "
              "stream can name it",
              any(line.startswith(f"+ thread Alpha/{NEW_THREAD}")
                  for line in lines), str(lines))

        touch_thread(alpha.database, NEW_THREAD, "Increase Message Area Size",
                     "closed", "running")
        lines = fleet.refresh()
        check("a thread that changed in place is reported in place, naming every "
              "field that moved",
              len(lines) == 1
              and lines[0].startswith(f"~ thread Alpha/{NEW_THREAD}")
              and all(field in lines[0] for field in ("title", "status",
                                                      "turn_state")),
              str(lines))

        check("the shared connection reads the fixture before anything is added",
              fleet.read(alpha, lambda connection: connection.execute(
                  f"select count(*) from {alpha.key}.messages")
                  .fetchone()[0]) > 0)

        add_project(DATA, NEW_PROJECT, NEW_WORKSPACE, NEW_PROJECT_THREAD)
        lines = fleet.refresh()
        last_keys = {project.directory.name: project.key
                     for project in fleet.projects}
        check("a project directory that appears after the startup look is "
              "discovered",
              NEW_PROJECT in last_keys, ", ".join(sorted(last_keys)))
        check("the new project sorts first, and still renumbers nobody: a key is "
              "assigned once",
              NEW_PROJECT in last_keys
              and all(name in last_keys and last_keys[name] == key
                      for name, key in first_keys.items())
              and last_keys[NEW_PROJECT] not in first_keys.values(),
              f"{first_keys} -> {last_keys}")
        added = fleet.by_label("Aaa")
        check("the key names a project's own database, not a neighbour's",
              added is not None and added.readable
              and fleet.read(added, lambda connection: connection.execute(
                  f"select count(*) from {added.key}.messages")
                  .fetchone()[0]) == 1,
              f"{added and added.key}")

        port = harness.free_port()
        served = fixture.config_for(
            DATA, server={"port": port, "open_browser": False},
            data={"watch_seconds": WATCH_S})
        config_path = fixture.write_config(DATA, served)
        log = testlog.child_log(CHILD)
        child = harness.start_child(CHILD,
                                    ["fb-dashboard.py", "--config", str(config_path)],
                                    root=ROOT)
        ready = harness.wait_for_log(log, f"serving on http://127.0.0.1:{port}", 40)
        check("the server started for the stream session", ready, f"see {log}")
        if not ready:
            return 1

        sock = open_stream(port)
        try:
            head = read_until(sock, b"\r\n\r\n", 10)
            check("the change stream answers as an event stream",
                  b"200" in head.split(b"\r\n")[0]
                  and b"text/event-stream" in head,
                  head[:160].decode("utf-8", "replace"))
            add_thread(alpha.database, PUSHED_THREAD, "Arrived while watching")
            add_message(alpha.database, PUSHED_THREAD, "pushed to the page")
            pushed = read_until(sock, b"data:", STREAM_WAIT_S)
            check("a change the server finds is pushed to the open page without "
                  "the page asking for it",
                  b"data:" in pushed,
                  pushed[-200:].decode("utf-8", "replace"))
            body = (pushed.split(b"data: ", 1)[-1]
                    + read_until(sock, b"\n\n", STREAM_WAIT_S)).split(b"\n\n", 1)[0]
            try:
                carried = json.loads(body.decode("utf-8")).get("board") or {}
            except ValueError:
                carried = {}
            titles = [thread["title"] for thread in carried.get("threads", [])]
            check("the pushed payload carries the board beside the change, so "
                  "the board is pushed rather than polled",
                  carried.get("running") == len(titles) >= 1
                  and "Running Thread" in titles
                  and "Arrived while watching" not in titles
                  and carried.get("close_ms") == 30000,
                  body[:200].decode("utf-8", "replace"))
        finally:
            sock.close()

        disconnect = open_stream(port)
        read_until(disconnect, b"\r\n\r\n", 10)
        disconnect.close()
        ended = harness.wait_for_log(log, "stream ended", DISCONNECT_WAIT_S)
        child_output = log.read_text(encoding="utf-8", errors="replace")
        check("the server ends a stream whose reader has gone rather than "
              "holding the connection",
              ended, f"no 'stream ended' in {log} within "
                     f"{DISCONNECT_WAIT_S:g} s")
        check("a closed event stream is an expected disconnect, not a traceback",
              ended and "Traceback" not in child_output
              and "Exception occurred during processing" not in child_output,
              child_output[-240:])

        logged = []
        error_server = app.Server(("127.0.0.1", harness.free_port()), None,
                                  router=None, log=logged.append)
        try:
            try:
                raise RuntimeError("expected request failure")
            except RuntimeError:
                error_server.handle_error(None, ("test", 1))
            check("unexpected request errors go through the application logger",
                  any("expected request failure" in message for message in logged),
                  str(logged))
        finally:
            error_server.server_close()

        status, tree = get_json(port, "/api/projects")
        listed = [row["id"] for project in tree.get("projects", [])
                  for row in project["open"] + project["closed"]]
        check("...and the same change is in the project list, with no restart",
              status == 200 and PUSHED_THREAD in listed,
              f"{status}, {len(listed)} thread(s)")

        status, refreshed = get_json(port, "/api/refresh", method="POST")
        check("/api/refresh re-reads the sources and answers with what it found",
              status == 200 and refreshed.get("refreshed") is True
              and isinstance(refreshed.get("changes"), list),
              f"{status} {str(refreshed)[:160]}")

        print()
        if failures:
            print(f"FAIL: {len(failures)} refresh check(s) failed")
            return 1
        print("ALL REFRESH TESTS PASS")
        return 0
    finally:
        if child is not None:
            harness.stop_child(child, label="the server")
        harness.reap_survivors(everything=True)
        fleet.close()
        fixture.close()

STAMP_S = 2.6
BUDGET_S = 3.9

if __name__ == "__main__":
    testlog.start("test_refresh")
    code = main()
    testlog.finish(code == 0)
    sys.exit(code)
