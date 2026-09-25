from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__
from . import board
from . import config as config_module
from . import corpus
from . import search as search_module
from .build_id import describe
from .fleet import Fleet
from .reader import ReaderError, thread as read_thread
from .watch import Watcher

PAGES = Path(__file__).resolve().parent / "pages"

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_NAME = "fb-dashboard-server.log"

MATCH_LIMIT = 200

JSON_TYPE = "application/json; charset=utf-8"
TEXT_TYPE = "text/plain; charset=utf-8"

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/activity": ("activity.html", "text/html; charset=utf-8"),
    "/activity.css": ("activity.css", "text/css; charset=utf-8"),
    "/activity.js": ("activity.js", "text/javascript; charset=utf-8"),
}

def _one(query: dict, name: str, default=None):
    found = query.get(name)
    if not found:
        return default
    return found[0] if isinstance(found, (list, tuple)) else found

@lru_cache(maxsize=1)
def capabilities() -> dict:
    found = {"sqlite_version": sqlite3.sqlite_version, "json": False,
             "fts5": False, "trigram": False}
    con = sqlite3.connect(":memory:")
    try:
        try:
            found["json"] = con.execute(
                "select json_extract('{\"a\":1}','$.a')").fetchone()[0] == 1
        except sqlite3.Error:
            found["json"] = False
        options = [row[0] for row in con.execute("pragma compile_options")]
        found["fts5"] = any("ENABLE_FTS5" in option for option in options)
        if found["fts5"]:
            try:
                con.execute("create virtual table probe using fts5"
                            "(x, tokenize='trigram', detail='none')")
                con.execute("drop table probe")
                found["trigram"] = True
            except sqlite3.Error:
                found["trigram"] = False
    finally:
        con.close()
    return found

class Logger:
    def __init__(self, directory: Path = LOG_DIR) -> None:
        self.lock = threading.Lock()
        self.path = directory / LOG_NAME
        self.handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = self.path.open("a", encoding="utf-8", newline="\n")
        except OSError:
            self.handle = None

    def __call__(self, message: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
        with self.lock:
            try:
                print(line, flush=True)
            except (UnicodeEncodeError, ValueError):
                print(line.encode("utf-8", "replace").decode("ascii", "replace"),
                      flush=True)
            if self.handle is not None:
                self.handle.write(line + "\n")
                self.handle.flush()

    def close(self) -> None:
        if self.handle is not None:
            try:
                self.handle.close()
            except OSError:
                pass
            self.handle = None

class Router:
    def __init__(self, fleet: Fleet, config: dict, log=None, watcher=None,
                 config_path: str | None = None) -> None:
        self.fleet = fleet
        self.config = config
        self.log = log or (lambda message: None)
        self.watcher = watcher
        self.config_path = config_path

    def refresh_fleet(self) -> list[str]:
        if self.watcher is not None:
            return self.watcher.refresh()
        return self.fleet.refresh()

    @staticmethod
    def json(status: int, payload) -> tuple:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return status, JSON_TYPE, body

    @staticmethod
    def text(status: int, message: str) -> tuple:
        return status, TEXT_TYPE, message.encode("utf-8")

    def static(self, path: str) -> tuple:
        name, content_type = STATIC[path]
        try:
            body = (PAGES / name).read_bytes()
        except OSError as exc:
            self.log(f"cannot read page {name}: {exc}")
            return self.text(500, f"cannot read page {name}")
        return 200, content_type, body

    def handle(self, method: str, path: str, query: dict) -> tuple:
        if path in STATIC:
            if method not in ("GET", "HEAD"):
                return self.text(405, "method not allowed")
            return self.static(path)
        if not path.startswith("/api/"):
            return self.json(404, {"error": f"no route {path}"})
        try:
            return self.api(method, path, query)
        except (search_module.SearchError, ReaderError) as exc:
            return self.json(exc.status, {"error": str(exc), "status": exc.status})
        except sqlite3.DatabaseError as exc:
            self.log(f"sqlite error on {path}: {exc}")
            return self.json(503, {"error": f"a source database could not be read:"
                                            f" {exc}"})

    def api(self, method: str, path: str, query: dict) -> tuple:
        if method not in ("GET", "POST"):
            return self.text(405, "method not allowed")
        if path == "/api/status":
            self.refresh_fleet()
            return self.json(200, self.status())
        if path == "/api/projects":
            self.refresh_fleet()
            return self.json(200, self.projects())
        if path == "/api/search":
            filters = search_module.parse(query, self.config)
            return self.json(200, search_module.search(self.fleet, filters))
        if path == "/api/thread":
            return self.thread(query)
        if path == "/api/running":
            self.refresh_fleet()
            return self.json(200, self.running())
        if path == "/api/refresh":
            return self.refresh()
        if path == "/api/export":
            return self.json(501, {"error": "Markdown export is not built yet"})

        return self.json(404, {"error": f"no route {path}"})

    @staticmethod
    def thread_row(entry) -> dict:
        return {"id": entry.id, "title": entry.title, "status": entry.status,
                "turn_state": entry.turn_state, "updated_at": entry.updated_at,
                "created_at": entry.created_at, "messages": entry.messages,
                "archived": entry.archived, "running": entry.running}

    def projects(self) -> dict:
        found = []
        for project in self.fleet.projects:
            rows = [self.thread_row(entry) for entry in project.threads]
            archived_rows = [row for row in rows if row["archived"]]
            live_rows = [row for row in rows if not row["archived"]]
            open_rows = [row for row in live_rows if row["status"] != "closed"]
            closed_rows = [row for row in live_rows if row["status"] == "closed"]
            found.append({
                "key": project.key,
                "label": project.label,
                "path": project.path,
                "unreadable": project.unreadable,
                "schema_warning": project.schema_warning,
                "counts": {"threads": len(rows),
                           "messages": sum(row["messages"] for row in rows),
                           "open": len(open_rows), "closed": len(closed_rows),
                           "archived": len(archived_rows)},
                "open": open_rows,
                "closed": closed_rows,
                "archived": archived_rows,
            })
        return {"projects": found}

    def thread(self, query: dict) -> tuple:
        thread_id = _one(query, "thread")
        if not thread_id:
            raise ReaderError("a thread id is required (?thread=...)", status=400)
        around = _one(query, "around")
        answer = read_thread(
            self.fleet, _one(query, "project"), thread_id,
            around=int(around) if around and str(around).isdigit() else None)
        found = self.thread_matches(query, thread_id, answer["project_key"])
        if found is not None:
            answer["found"] = found
        return self.json(200, answer)

    def thread_matches(self, query: dict, thread_id: str,
                       project_key: str) -> dict | None:
        if not (_one(query, "q") or "").strip():
            return None
        try:
            filters = search_module.parse(
                {**query, "scope": "thread", "thread": thread_id,
                 "project": project_key, "limit": MATCH_LIMIT},
                self.config)
            answer = search_module.search(self.fleet, filters)
        except search_module.SearchError:
            return None
        stops, seen = [], set()
        for row in answer["results"]:
            identity = (row["seq"], row["part_index"])
            if identity not in seen:
                seen.add(identity)
                stops.append({"seq": row["seq"], "part": row["part_index"]})
        stops.sort(key=lambda stop: (stop["seq"], stop["part"]))
        return {"total": answer["total"], "stops": stops,
                "needles": list(filters.needles)}

    def running(self) -> dict:
        return board.running(self.fleet,
                             self.watcher.seconds if self.watcher else 0.0,
                             config_module.close_seconds(self.config))

    def refresh(self) -> tuple:
        lines = self.refresh_fleet()
        return self.json(200, {
            "refreshed": True,
            "changes": lines,
            "projects": len(self.fleet.projects),
            "reason": "the source databases were re-read; nothing else is "
                      "cached, so a reload of /api/projects shows this already",
        })

    def status(self) -> dict:
        projects = []
        for project in self.fleet.projects:
            row = {"key": project.key, "label": project.label,
                   "path": project.path, "unreadable": project.unreadable,
                   "threads": len(project.threads),
                   "messages": sum(entry.messages for entry in project.threads),
                   "max_seq": None}
            if project.readable and not project.schema_limited:
                table = project.schema["tables"]["messages"]
                seq = project.schema["columns"]["messages"]["seq"]
                try:
                    row["max_seq"] = self.fleet.connection().execute(
                        f"select coalesce(max({corpus.quote(seq)}),0)"
                        f" from {project.key}.{corpus.quote(table)}").fetchone()[0]
                except sqlite3.Error:
                    row["max_seq"] = None
            projects.append(row)
        return {
            "version": __version__,
            "build": describe(),
            "data_root": str(config_module.root_of(self.config)),
            "config": {"path": self.config_path},
            "schema": {"limited": self.fleet.has_schema_warning(),
                       "details": self.fleet.schema_warning_details()},
            "sqlite": capabilities(),
            "watch": {"seconds": self.watcher.seconds if self.watcher else 0.0,
                      "stream": bool(self.watcher and self.watcher.seconds > 0)},
            "activity": {"close_seconds": config_module.close_seconds(self.config)},
            "index": {"enabled": bool(self.config["index"]["enabled"]),
                      "present": False, "size": None, "age_s": None,
                      "note": "the sources are read directly and both search "
                              "modes run live, so word search ranks by recency "
                              "rather than bm25"},
            "search": {"default_mode": self.config["search"]["mode"],
                       "default_scope": self.config["search"]["scope"],
                       "categories": self.config["search"]["categories"],
                       "limit": self.config["search"]["limit"]},
            "projects": projects,
        }

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"fb-dashboard/{__version__}"
    sys_version = ""

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except ConnectionError:
            self.close_connection = True

    def do_GET(self) -> None:
        self.respond("GET")

    def do_HEAD(self) -> None:
        self.respond("HEAD")

    def do_POST(self) -> None:
        self.respond("POST")

    def plain(self, status: int, message, *, json_body: bool = True) -> None:
        body = (json.dumps(message, ensure_ascii=False) if json_body
                else str(message)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", JSON_TYPE if json_body else TEXT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.server.log(f"{self.command} {self.path} -> {status}")

    def write_chunk(self, text: str) -> None:
        body = text.encode("utf-8")
        self.wfile.write(f"{len(body):X}\r\n".encode("ascii") + body + b"\r\n")
        self.wfile.flush()

    def stream_events(self, method: str) -> None:
        watcher = self.server.router.watcher
        if method not in ("GET", "HEAD"):
            self.plain(405, "method not allowed", json_body=False)
            return
        if watcher is None or watcher.seconds <= 0:
            self.plain(503, {"error": "the change stream is off "
                                      "(data.watch_seconds is 0)"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        started = time.perf_counter()
        events = 0
        seen = watcher.version
        self.server.log(f"GET {self.path} -> 200 (streaming)")
        try:
            while True:
                version, lines = watcher.watched(seen, watcher.seconds)
                if version == seen:
                    self.write_chunk(": ping\n\n")
                    continue
                seen = version
                events += 1
                payload = json.dumps(self.push(lines), ensure_ascii=False)
                self.write_chunk("data: " + payload + "\n\n")
        except ConnectionError:
            pass
        finally:
            self.server.log(f"GET {self.path} -> stream ended ({events} event(s),"
                            f" {time.perf_counter() - started:.1f} s)")

    def push(self, lines: list[str]) -> dict:
        payload = {"changes": lines}
        try:
            payload["board"] = self.server.router.running()
        except sqlite3.DatabaseError as exc:
            self.server.log(f"the board could not be read for a push: {exc}")
        return payload

    def respond(self, method: str) -> None:
        parsed = urlsplit(self.path)
        if unquote(parsed.path) == "/api/events":
            self.stream_events(method)
            return
        started = time.perf_counter()
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            status, content_type, body = self.server.router.handle(
                method, unquote(parsed.path), query)
        except Exception:
            self.server.log(traceback.format_exc())
            status, content_type, body = Router.json(
                500, {"error": "internal error; see logs/fb-dashboard-server.log"})
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(body)
        except ConnectionError:
            pass
        self.server.log(f"{method} {self.path} -> {status} "
                        f"({(time.perf_counter() - started) * 1000:.1f} ms)")

    def log_message(self, fmt: str, *args) -> None:
        pass

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, address, handler, *, router: Router, log) -> None:
        super().__init__(address, handler)
        self.router = router
        self.log = log

    def handle_error(self, request, client_address) -> None:
        self.log(f"request error from {client_address}:\n"
                 f"{traceback.format_exc().rstrip()}")

def display_available() -> bool:
    if not sys.platform.startswith("linux"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

def _force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

def serve(config: dict, config_path: str | None = None) -> int:
    _force_utf8_output()
    host = config["server"]["host"]
    port = int(config["server"]["port"])
    log = Logger()
    fleet = Fleet(config)
    watcher = Watcher(config, fleet, log)
    router = Router(fleet, config, log, watcher, config_path=config_path)
    try:
        server = Server((host, port), Handler, router=router, log=log)
    except OSError as exc:
        log(f"cannot bind {host}:{port}: {exc}")
        fleet.close()
        log.close()
        return 2
    bound_host, bound_port = server.server_address[:2]
    start_path = str(config["server"].get("open_path") or "/")
    unreadable = sum(1 for project in fleet.projects if project.unreadable)
    log(f"freebuff-dashboard {describe()}")
    log(f"config: {config_path or 'built-in defaults (no config.json was found)'}")
    log(f"data root: {config_module.root_of(config)}")
    log(f"{len(fleet.projects)} project(s), {unreadable} unreadable")
    log(f"log: {log.path}")

    log(f"serving on http://{bound_host}:{bound_port}")
    if start_path != "/":
        log(f"what it opens on: http://{bound_host}:{bound_port}{start_path}")
    watcher.start()
    if config["server"].get("open_browser"):
        if display_available():
            threading.Thread(target=webbrowser.open,
                             args=(f"http://{bound_host}:{bound_port}{start_path}",),
                             daemon=True).start()
        else:
            log("open_browser is on, but there is no display here: browse to "
                f"http://{bound_host}:{bound_port}{start_path} yourself")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopped (Ctrl-C)")
    finally:
        watcher.close()
        server.server_close()
        fleet.close()
        log.close()
    return 0
