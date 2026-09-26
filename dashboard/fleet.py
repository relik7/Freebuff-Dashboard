from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from urllib.parse import quote

from . import config as config_module
from . import corpus

BUSY_TIMEOUT_MS = 2000

WORKERS = 2

FALLBACK_ATTACHED = 10

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

def _close(connection) -> None:
    try:
        connection.close()
    except sqlite3.Error:
        pass

def attached_ceiling() -> int:
    connection = None
    try:
        connection = sqlite3.connect(":memory:")
        marker = getattr(sqlite3, "SQLITE_LIMIT_ATTACHED", None)
        if marker is None or not hasattr(connection, "getlimit"):
            return FALLBACK_ATTACHED
        found = int(connection.getlimit(marker))
    except (sqlite3.Error, ValueError, TypeError):
        return FALLBACK_ATTACHED
    finally:
        if connection is not None:
            _close(connection)
    return found if found > 0 else FALLBACK_ATTACHED

def key_group(key: str, ceiling: int) -> int:
    try:
        index = int(key[1:])
    except (ValueError, IndexError):
        index = 0
    return index // max(1, ceiling)

def read_only_uri(path: Path) -> str:
    return f"file:{quote(path.as_posix())}?mode=ro"

def read_only_authorizer(audit: list):
    def callback(action, arg1, arg2, dbname, source):
        audit.append((action, arg1, dbname, source))
        return sqlite3.SQLITE_OK if action in READ_ACTIONS else sqlite3.SQLITE_DENY

    return callback

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

class Pool:
    def __init__(self, workers: int, build) -> None:
        self.build = build
        self.jobs: Queue = Queue()
        self.threads = [
            threading.Thread(target=self._serve, name=f"fb-dashboard-db-{number}",
                             daemon=True)
            for number in range(max(1, workers))]
        for thread in self.threads:
            thread.start()

    def run(self, group: int, generation: int, work):
        future: Future = Future()
        self.jobs.put((group, generation, work, future))
        return future.result()

    def forget(self, group: int) -> None:
        for _ in self.threads:
            self.jobs.put((group, None, None, None))

    def _serve(self) -> None:
        pinned: dict[int, tuple] = {}
        while True:
            item = self.jobs.get()
            if item is None:
                break
            group, generation, work, future = item
            if work is None:
                dropped = pinned.pop(group, None)
                if dropped is not None:
                    _close(dropped[1])
                continue
            try:
                found = pinned.get(group)
                if found is None or found[0] != generation:
                    if found is not None:
                        _close(found[1])
                    found = (generation, self.build(group))
                    pinned[group] = found
                future.set_result(work(found[1]))
            except BaseException as exc:
                if isinstance(exc, sqlite3.DatabaseError):
                    dropped = pinned.pop(group, None)
                    if dropped is not None:
                        _close(dropped[1])
                future.set_exception(exc)
        for _, connection in pinned.values():
            _close(connection)

    def close(self) -> None:
        for _ in self.threads:
            self.jobs.put(None)
        for thread in self.threads:
            thread.join(timeout=5)

class Fleet:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.keys: dict = {}
        self.ceiling = attached_ceiling()
        self.plan: dict[int, list[Project]] = {}
        self.generations: dict[int, int] = {}
        self.signatures: dict[int, tuple] = {}
        self.versions: dict[str, int] = {}
        self.registry: dict[str, Project] = {}
        self.audit: list = []
        self.runtime_schema_warnings: dict[str, str] = {}
        self._refresh_lock = threading.Lock()
        self.pool = Pool(WORKERS, self._build_group)
        try:
            self.projects = self._discover()
        except BaseException:
            self.pool.close()
            raise

    def group_of(self, project: Project) -> int:
        return key_group(project.key, self.ceiling)

    def groups(self) -> list[int]:
        return sorted(self.plan)

    def _plan_of(self, projects) -> dict[int, list[Project]]:
        plan: dict[int, list[Project]] = {}
        for project in projects:
            if project.readable:
                plan.setdefault(self.group_of(project), []).append(project)
        return plan

    def _version(self, project: Project) -> int | None:
        def work(connection):
            connection.set_authorizer(None)
            try:
                return corpus.data_version(connection, project.key)
            finally:
                connection.set_authorizer(read_only_authorizer(self.audit))

        try:
            return self.read(project, work)
        except sqlite3.Error:
            return None

    def _build_group(self, group: int):
        connection = sqlite3.connect(":memory:", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma query_only=on")
        connection.execute(f"pragma busy_timeout = {BUSY_TIMEOUT_MS}")
        for project in self.plan.get(group, ()):
            if not project.readable:
                continue
            try:
                connection.execute(
                    f"attach database '{read_only_uri(project.database)}'"
                    f" as {project.key}")
            except sqlite3.Error as exc:
                project.unreadable = f"{type(exc).__name__}: {exc}"
                project.schema_warning = f"{type(exc).__name__}: {exc}"
        connection.set_authorizer(read_only_authorizer(self.audit))
        return connection

    def _inspect(self, project: Project) -> None:
        def work(connection):
            connection.set_authorizer(None)
            try:
                schema = corpus.inspect_schema(connection, project.key)
            finally:
                connection.set_authorizer(read_only_authorizer(self.audit))
            project.schema = schema
            problem = corpus.schema_problem(schema)
            if problem:
                return [], problem
            rows = connection.execute(
                corpus.thread_list(project.key, schema)).fetchall()
            return [Thread(*row) for row in rows], None

        try:
            project.threads, warning = self.read(project, work)
            project.schema_warning = warning
        except sqlite3.Error as exc:
            project.unreadable = f"{type(exc).__name__}: {exc}"
            project.schema_warning = f"{type(exc).__name__}: {exc}"

    def _discover(self) -> list[Project]:
        directories = project_directories(self.config)
        assign_keys(directories, self.keys)
        candidates: list[tuple[Path, Project]] = []
        for directory in directories:
            project = load_project(directory, self.keys[directory.name],
                                   self.config)
            if project is not None:
                candidates.append((directory, project))
        self.plan = self._plan_of([project for _, project in candidates])
        signatures = {group: attached_keys(members)
                      for group, members in self.plan.items()}
        for group, signature in signatures.items():
            if self.signatures.get(group) != signature:
                self.generations[group] = self.generations.get(group, 0) + 1
        for group in set(self.signatures) - set(signatures):
            self.pool.forget(group)
        self.signatures = signatures
        found: list[Project] = []
        for directory, project in candidates:
            if not project.readable:
                found.append(project)
                continue
            version = self._version(project)
            cached = self.registry.get(directory.name)
            if (cached is not None and cached.readable and version is not None
                    and cached.label == project.label
                    and cached.path == project.path
                    and str(cached.database) == str(project.database)
                    and self.versions.get(cached.key) == version):
                found.append(cached)
                continue
            self._inspect(project)
            if version is not None:
                self.versions[project.key] = version
            found.append(project)
        self.plan = self._plan_of(found)
        self.registry = {project.directory.name: project for project in found}
        found.sort(key=lambda project: -project.newest())
        return found

    def read(self, project: Project, work):
        group = self.group_of(project)
        return self.pool.run(group, self.generations.get(group, 0), work)

    def refresh(self) -> list[str]:
        with self._refresh_lock:
            before = snapshot(self.projects)
            found = self._discover()
            lines = changes(before, snapshot(found))
            self.projects = found
            return lines

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
        self.pool.close()
