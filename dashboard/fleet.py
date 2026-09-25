from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from . import config as config_module
from . import corpus

BUSY_TIMEOUT_MS = 2000

READ_ACTIONS = frozenset({
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_TRANSACTION,
})

@dataclass
class Thread:
    id: str
    title: str
    status: str
    turn_state: str
    created_at: int
    updated_at: int
    messages: int
    archived_at: int = 0

    @property
    def running(self) -> bool:
        return self.turn_state not in ("", "idle")

    @property
    def archived(self) -> bool:
        return bool(self.archived_at)

@dataclass
class Project:
    key: str
    label: str
    path: str
    directory: Path
    database: Path
    unreadable: str | None = None
    schema_warning: str | None = None
    schema: dict | None = None
    threads: list[Thread] = field(default_factory=list)

    @property
    def readable(self) -> bool:
        return self.unreadable is None

    @property
    def schema_limited(self) -> bool:
        return bool(self.schema_warning)

    def newest(self) -> int:
        return max((thread.updated_at for thread in self.threads), default=0)

def read_only_uri(path: Path) -> str:
    return f"file:{quote(path.as_posix())}?mode=ro"

def read_only_authorizer(audit: list):
    def callback(action, arg1, arg2, dbname, source):
        audit.append((action, arg1, dbname, source))
        return sqlite3.SQLITE_OK if action in READ_ACTIONS else sqlite3.SQLITE_DENY

    return callback

def list_threads(database: Path) -> tuple[list[Thread], dict | None, str | None]:
    con = sqlite3.connect(read_only_uri(database), uri=True)
    try:
        con.execute("pragma query_only=on")
        con.execute(f"pragma busy_timeout = {BUSY_TIMEOUT_MS}")
        schema = corpus.inspect_schema(con)
        problem = corpus.schema_problem(schema)
        if problem:
            return [], schema, problem
        rows = con.execute(corpus.thread_list("main", schema)).fetchall()
    finally:
        con.close()
    return [Thread(*row) for row in rows], schema, None


def project_directories(config: dict) -> list[Path]:
    root = config_module.root_of(config) / "projects"
    if not root.is_dir():
        return []
    return sorted((child for child in root.iterdir() if child.is_dir()),
                  key=lambda path: path.name)

def load_project(directory: Path, key: str, config: dict) -> Project | None:
    meta_path = directory / "project.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Project(key=key, label=directory.name, path=directory.name,
                       directory=directory, database=Path(),
                       unreadable=f"project.json unreadable: {exc}")
    if not isinstance(meta, dict):
        return Project(key=key, label=directory.name, path=directory.name,
                       directory=directory, database=Path(),
                       unreadable="project.json is not an object")

    workspace = str(meta.get("projectPath") or directory.name)
    label = Path(workspace).name or workspace
    excluded = config.get("data", {}).get("exclude") or []
    if label in excluded or workspace in excluded:
        return None

    database = directory / str(meta.get("database") or "desktop-v2.db")
    project = Project(key=key, label=label, path=workspace, directory=directory,
                      database=database)
    if not database.is_file():
        project.unreadable = f"no database at {database.name}"
        return project
    try:
        project.threads, project.schema, project.schema_warning = list_threads(database)
    except sqlite3.Error as exc:
        project.unreadable = f"{type(exc).__name__}: {exc}"
        project.schema_warning = f"{type(exc).__name__}: {exc}"
    return project

def assign_keys(directories: list[Path], keys: dict) -> None:
    taken = set(keys.values())
    next_index = 0
    for directory in directories:
        if directory.name in keys:
            continue
        while f"p{next_index}" in taken:
            next_index += 1
        keys[directory.name] = f"p{next_index}"
        taken.add(f"p{next_index}")

def discover(config: dict, keys: dict | None = None) -> list[Project]:
    directories = project_directories(config)
    keys = {} if keys is None else keys
    assign_keys(directories, keys)
    found: list[Project] = []
    for directory in directories:
        project = load_project(directory, keys[directory.name], config)
        if project is not None:
            found.append(project)
    found.sort(key=lambda project: -project.newest())
    return found

WATCH_FIELDS = ("title", "status", "turn_state", "messages", "updated_at",
                "archived_at")

WATCH_STATE_FIELDS = ("label", "path", "unreadable", "schema_warning")

def snapshot(projects: list[Project]) -> dict:
    found: dict = {}
    for project in projects:
        threads = {}
        for thread in project.threads:
            threads[thread.id] = {field: getattr(thread, field)
                                  for field in WATCH_FIELDS}
        found[project.directory.name] = {
            "label": project.label,
            "path": project.path,
            "unreadable": project.unreadable,
            "schema_warning": project.schema_warning,
            "threads": threads,
        }
    return found

def _thread_lines(project: str, old: dict, new: dict) -> list[str]:
    lines: list[str] = []
    for ident in sorted(set(new) - set(old)):
        row = new[ident]
        lines.append(f"+ thread {project}/{ident} {row['title']!r}"
                     f" {row['status']}/{row['turn_state']}"
                     f" {row['messages']} message(s)")
    for ident in sorted(set(old) - set(new)):
        row = old[ident]
        lines.append(f"- thread {project}/{ident} {row['title']!r} gone")
    for ident in sorted(set(old) & set(new)):
        before, after = old[ident], new[ident]
        moved = [f"{field} {before[field]!r} -> {after[field]!r}"
                 for field in WATCH_FIELDS if before[field] != after[field]]
        if moved:
            lines.append(f"~ thread {project}/{ident} {after['title']!r}: "
                         + "; ".join(moved))
    return lines

def _project_lines(name: str, old: dict | None, new: dict | None) -> list[str]:
    if old is None and new is not None:
        return [f"+ project {new['label']!r} ({new['path']})"
                f" {len(new['threads'])} thread(s)"]
    if new is None and old is not None:
        return [f"- project {old['label']!r} ({old['path']}) gone"]
    lines: list[str] = []
    for field in WATCH_STATE_FIELDS:
        if old[field] != new[field]:
            lines.append(f"~ project {name} {field}"
                         f" {old[field]!r} -> {new[field]!r}")
    lines.extend(_thread_lines(new["label"], old["threads"], new["threads"]))
    return lines

def changes(before: dict, after: dict) -> list[str]:
    lines: list[str] = []
    for name in sorted(set(after) - set(before)):
        lines.extend(_project_lines(name, None, after[name]))
    for name in sorted(set(before) - set(after)):
        lines.extend(_project_lines(name, before[name], None))
    for name in sorted(set(before) & set(after)):
        lines.extend(_project_lines(name, before[name], after[name]))
    return lines

def attached_keys(projects: list[Project]) -> tuple:
    return tuple((project.key, str(project.database))
                 for project in projects if project.readable)

def connect(config: dict, projects: list[Project],
            audit: list | None = None) -> tuple[sqlite3.Connection, list]:
    con = sqlite3.connect(":memory:", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("pragma query_only=on")
    con.execute(f"pragma busy_timeout = {BUSY_TIMEOUT_MS}")
    for project in projects:
        if not project.readable:
            continue
        con.execute(f"attach database '{read_only_uri(project.database)}'"
                    f" as {project.key}")
    if audit is None:
        audit = []

    con.set_authorizer(read_only_authorizer(audit))
    return con, audit

class Fleet:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.keys: dict = {}
        self.projects = discover(config, self.keys)
        self.generation = 0
        self.audit: list = []
        self.runtime_schema_warnings: dict[str, str] = {}
        self._connections: dict[int, tuple[int, sqlite3.Connection]] = {}
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    def refresh(self) -> list[str]:
        with self._refresh_lock:
            before = snapshot(self.projects)
            attached = attached_keys(self.projects)
            found = discover(self.config, self.keys)
            lines = changes(before, snapshot(found))
            self.projects = found
            if attached_keys(found) != attached:
                self.generation += 1
            return lines

    def connection(self) -> sqlite3.Connection:
        ident = threading.get_ident()
        found = self._connections.get(ident)
        if found is not None and found[0] == self.generation:
            return found[1]
        if found is not None:
            try:
                found[1].close()
            except sqlite3.Error:
                pass
        made = connect(self.config, self.projects, self.audit)[0]
        with self._lock:
            self._connections[ident] = (self.generation, made)
        return made

    def readable(self) -> list[Project]:
        return [project for project in self.projects if project.readable]

    def mark_schema_warning(self, project: Project, error) -> None:
        self.runtime_schema_warnings[project.key] = (
            f"{project.label}: {type(error).__name__}: {error}")

    def clear_schema_warning(self, project: Project) -> None:
        self.runtime_schema_warnings.pop(project.key, None)

    def schema_warning_details(self) -> list[str]:
        found = [f"{project.label}: {project.schema_warning}"
                 for project in self.projects if project.schema_warning]
        found.extend(self.runtime_schema_warnings.values())
        return found

    def has_schema_warning(self) -> bool:
        return bool(self.schema_warning_details())

    def by_key(self, key: str | None) -> Project | None:
        if not key:
            return None
        return next((project for project in self.projects
                     if project.key == key), None)

    def by_label(self, label: str | None) -> Project | None:
        if not label:
            return None
        wanted = label.casefold()
        return next((project for project in self.projects
                     if project.label.casefold() == wanted
                     or project.path.casefold() == wanted), None)

    def find(self, name: str | None) -> Project | None:
        return self.by_key(name) or self.by_label(name)

    def by_thread(self, thread_id: str | None) -> tuple[Project, Thread] | None:
        if not thread_id:
            return None
        for project in self.projects:
            for thread in project.threads:
                if thread.id == thread_id:
                    return project, thread
        return None

    def close(self) -> None:
        with self._lock:
            connections, self._connections = [entry[1] for entry
                                              in self._connections.values()], {}
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass
