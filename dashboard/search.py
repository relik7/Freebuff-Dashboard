from __future__ import annotations

import datetime
import html
import re
import sqlite3
import time
from dataclasses import dataclass

from . import config as config_module
from . import corpus
from . import markdown
from . import reader

NO_CATEGORIES = ("Select at least one of User messages / Agent responses / "
                 "Thinking / Tool runs / File diffs.")

CATEGORY_KINDS = {
    "user": corpus.CONVERSATION_KINDS,
    "assistant": corpus.CONVERSATION_KINDS,
    "reasoning": ("reasoning",),
    "tools": ("tool",),
    "changes": ("changes",),
}

BOTH_ROLES = ("user", "assistant")
CATEGORY_ROLES = {
    "user": ("user",),
    "assistant": ("assistant",),
    "reasoning": BOTH_ROLES,
    "tools": BOTH_ROLES,
    "changes": BOTH_ROLES,
}

CATEGORIES = config_module.CATEGORIES
DEFAULT_CATEGORIES = config_module.DEFAULT_CATEGORIES

KIND_OF_CATEGORY = {
    "user_text": "text",
    "assistant_text": "text",
    "reasoning": "reasoning",
    "tool_input": "tool",
    "tool_output": "tool",
    "changes": "changes",
}

BUSY_RETRY_S = 0.2

SNIPPET_BEFORE = 70

LIKE_ESCAPE = "\\"

class SearchError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status

@dataclass(frozen=True)
class Filters:
    q: str = ""
    mode: str = "words"
    scope: str = "all"
    project: str | None = None
    thread: str | None = None
    categories: tuple = DEFAULT_CATEGORIES
    since: int | None = None
    until: int | None = None
    hide_closed: bool = False
    limit: int = 50
    offset: int = 0
    snippet_chars: int = 240

    @property
    def needles(self) -> list[str]:
        return needles(self.q, self.mode)

    @property
    def roles(self) -> tuple:
        found: list[str] = []
        for name in self.categories:
            for role in CATEGORY_ROLES.get(name, ()):
                if role not in found:
                    found.append(role)
        return tuple(found)

    @property
    def kinds(self) -> tuple:
        found: list[str] = []
        for name in self.categories:
            for kind in CATEGORY_KINDS.get(name, ()):
                if kind not in found:
                    found.append(kind)
        return tuple(found)

def _asked(params, name: str) -> bool:
    return name in params and params[name] is not None

def _raw(params, name: str, default: str = "") -> str:
    found = params.get(name)
    if found is None:
        return default
    value = found[0] if isinstance(found, (list, tuple)) else found
    return default if value is None else str(value)

def _value(params, name: str, default=None):
    if not _asked(params, name):
        return default
    value = _raw(params, name)
    return default if value.strip() == "" else value

def _bounded(value, low: int, high: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))

def parse(params, config: dict) -> Filters:
    search = config.get("search") or {}
    mode = _value(params, "mode", search.get("mode", "words"))
    if mode not in config_module.MODES:
        raise SearchError(f"unknown mode {mode!r}; expected one of "
                          f"{', '.join(config_module.MODES)}")
    scope = _value(params, "scope", search.get("scope", "all"))
    if scope not in config_module.SCOPES:
        raise SearchError(f"unknown scope {scope!r}; expected one of "
                          f"{', '.join(config_module.SCOPES)}")

    requested_categories = (_raw(params, "category").split(",")
                            if _asked(params, "category")
                            else (search.get("categories")
                                  or DEFAULT_CATEGORIES))
    picked = {str(name).strip().lower() for name in requested_categories}
    categories = tuple(name for name in CATEGORIES if name in picked)
    if not categories:
        raise SearchError(NO_CATEGORIES)

    limit = _bounded(_value(params, "limit"), config_module.MIN_LIMIT,
                     config_module.MAX_LIMIT,
                     int(search.get("limit", 50) or 50))
    offset = _bounded(_value(params, "offset"), 0, 10 ** 9, 0)
    return Filters(
        q=_value(params, "q", "") or "",
        mode=mode,
        scope=scope,
        project=_value(params, "project"),
        thread=_value(params, "thread"),
        categories=categories,
        since=day_start(_value(params, "since"), search.get("since")),
        until=day_end(_value(params, "until"), search.get("until")),
        hide_closed=_truthy(_value(params, "hide_closed"),
                            bool(search.get("hide_closed", False))),
        limit=limit,
        offset=offset,
        snippet_chars=int(search.get("snippet_chars", 240) or 240),
    )

def _truthy(value, fallback: bool) -> bool:
    if value is None:
        return fallback
    return str(value).strip().lower() not in ("", "0", "false", "no", "off")

DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

def _day(value, *, end: bool):
    text = str(value or "").strip()
    if not DAY.match(text):
        return None
    try:
        day = datetime.date.fromisoformat(text)
    except ValueError:
        return None
    start = datetime.datetime.combine(day, datetime.time.min)
    if not end:
        return int(start.timestamp() * 1000)
    following = start + datetime.timedelta(days=1)
    return int(following.timestamp() * 1000) - 1

def day_start(value, fallback=None):
    return _day(value, end=False) if value else _day(fallback, end=False)

def day_end(value, fallback=None):
    return _day(value, end=True) if value else _day(fallback, end=True)

def needles(q: str, mode: str) -> list[str]:
    text = (q or "").strip()
    if not text:
        return []
    if mode == "exact":
        return [text]
    if text.startswith('"') and not text.endswith('"'):
        text += '"'
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        inner = text[1:-1].strip()
        return [inner] if inner else []
    return text.split()

def escape_like(text: str) -> str:
    return (text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
                .replace("%", LIKE_ESCAPE + "%")
                .replace("_", LIKE_ESCAPE + "_"))

def pattern(needle: str) -> str:
    return f"%{escape_like(needle.lower())}%"

def match_clause(found: list[str]) -> tuple[str, list]:
    if not found:
        return "", []
    pieces = [f"  AND lower(coalesce({corpus.RECORD_TEXT},''))"
              f" LIKE ? ESCAPE '{LIKE_ESCAPE}'" for _ in found]
    return "\n".join(pieces), [pattern(needle) for needle in found]

def role_clause(roles: tuple) -> tuple[str, list]:
    if set(roles) >= set(config_module.ROLES):
        return "", []
    return ("  AND m.role IN (" + ",".join("?" * len(roles)) + ")",
            list(roles))

def thread_clause(scope: str, thread: str | None) -> tuple[str, list]:
    if scope == "thread":
        return "  AND m.thread_id = ?", [thread]
    return "", []

def date_clause(since: int | None, until: int | None) -> tuple[str, list]:
    pieces: list[str] = []
    params: list = []
    if since is not None:
        pieces.append("  AND m.ts >= ?")
        params.append(since)
    if until is not None:
        pieces.append("  AND m.ts <= ?")
        params.append(until)
    return "\n".join(pieces), params

def closed_clause(hide_closed: bool) -> tuple[str, list]:
    if not hide_closed:
        return "", []
    return "  AND coalesce(t.status,'open') <> 'closed'", []

def scoped(fleet, filters: Filters) -> list:
    if filters.scope == "all":
        return fleet.readable()
    if filters.scope == "project":
        project = fleet.find(filters.project)
        if project is None:
            raise SearchError(f"no project {filters.project!r}", status=404)
        return [project] if project.readable else []
    if filters.scope == "thread":
        found = fleet.by_thread(filters.thread)
        if found is None:
            raise SearchError(f"no thread {filters.thread!r}", status=404)
        return [found[0]]
    return []

def execute(con: sqlite3.Connection, sql: str, params: list) -> list:
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "locked" not in message and "busy" not in message:
            raise
    time.sleep(BUSY_RETRY_S)
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        raise SearchError(f"a source database is busy; retry in a moment ({exc})",
                          status=503) from exc

def snippet_text(body: str | None) -> str:
    return markdown.plain(_flatten(body or ""))

def highlight(text: str, found: list[str], chars: int, *,
              before: int = SNIPPET_BEFORE) -> str:
    body = snippet_text(text)
    lowered = body.lower()
    wanted: list[str] = []
    for needle in found or []:
        target = str(needle).lower()
        if target and target not in wanted:
            wanted.append(target)
    hits: list[tuple[int, int]] = []
    for target in wanted:
        at = lowered.find(target)
        while at >= 0:
            hits.append((at, at + len(target)))
            at = lowered.find(target, at + len(target))
    if not hits:
        head = _flatten(body[:chars])
        return html.escape(head) + ("…" if len(body) > chars else "")
    hits.sort()
    start = max(0, hits[0][0] - before)
    end = min(len(body), start + chars)
    marked: list[list[int]] = []
    for left, right in hits:
        if left >= end or right <= start:
            continue
        left, right = max(left, start), min(right, end)
        if marked and left <= marked[-1][1]:
            marked[-1][1] = max(marked[-1][1], right)
        else:
            marked.append([left, right])
    pieces = ["…" if start > 0 else ""]
    cursor = start
    for left, right in marked:
        pieces.append(html.escape(_flatten(body[cursor:left])))
        pieces.append("<mark>" + html.escape(_flatten(body[left:right])) + "</mark>")
        cursor = right
    pieces.append(html.escape(_flatten(body[cursor:end])))
    pieces.append("…" if end < len(body) else "")
    return "".join(pieces)

def _flatten(text: str) -> str:
    return text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")

def shape(row, filters: Filters) -> dict:
    kind = KIND_OF_CATEGORY.get(row["category"], "text")
    result = {
        "project": row["project"],
        "project_path": row["project_path"],
        "project_key": row["project_key"],
        "thread_id": row["thread_id"],
        "thread_title": row["thread_title"],
        "thread_status": row["thread_status"],
        "seq": row["seq"],
        "role": row["role"],
        "ts": row["ts"],
        "category": row["category"],
        "part_index": row["part_index"],
        "kind": kind,
        "snippet": highlight(row["text"], filters.needles, filters.snippet_chars),
    }
    if kind == "reasoning":
        result["icon"] = "think"
        result["label"] = reader.label(row["text"] or "")
    elif kind == "tool":
        name = row["tool_name"] or "tool"
        result["name"] = name
        result["icon"] = reader.tool_icon(name)
        result["label"] = reader.tool_label(name)
        result["detail"] = reader.tool_subject(row["tool_input"])
        result["half"] = ("result" if row["category"] == "tool_output" else "call")
    elif kind == "changes":
        files = reader.changed_files(row["text"])
        result["files"] = len(files)
        result["icon"] = "edit"
        result["label"] = "Diffs"
        if len(files) == 1:
            one = files[0]
            result["detail"] = (f"{one['path']} · +{one['adds']}/-{one['dels']} "
                                f"· {one['status']}")
        else:
            result["detail"] = f"{len(files)} file(s)"
    return result

def search(fleet, filters: Filters) -> dict:
    if not filters.categories:
        raise SearchError(NO_CATEGORIES)
    started = time.perf_counter()
    found = filters.needles
    projects = scoped(fleet, filters)
    match_sql, match_params = match_clause(found)
    role_sql, role_params = role_clause(filters.roles)
    thread_sql, thread_params = thread_clause(filters.scope, filters.thread)
    date_sql, date_params = date_clause(filters.since, filters.until)
    closed_sql, closed_params = closed_clause(filters.hide_closed)

    extra = "\n".join(piece for piece in
                      (role_sql, thread_sql, date_sql, closed_sql, match_sql)
                      if piece)
    extra_params = [*role_params, *thread_params, *date_params, *closed_params,
                    *match_params]

    branches: list[tuple] = []
    for project in projects:
        if corpus.schema_problem(project.schema):
            continue
        sql, branch_params = corpus.search_branch(
            project, kinds=filters.kinds, extra=extra,
            extra_params=extra_params)
        branches.append((project, sql, branch_params))

    rows: list = []
    total = 0
    for project, sql, branch_params in branches:
        def gather(connection, sql=sql, params=branch_params,
                   limit=filters.offset + filters.limit):
            count = execute(connection, f"SELECT COUNT(*) AS n FROM ({sql})",
                            params)[0]["n"]
            page = execute(
                connection,
                f"SELECT * FROM ({sql}) ORDER BY ts DESC, seq DESC LIMIT ?",
                [*params, limit])
            return count, page

        try:
            count, page = fleet.read(project, gather)
        except sqlite3.DatabaseError as exc:
            fleet.mark_schema_warning(project, exc)
            continue
        fleet.clear_schema_warning(project)
        total += count
        rows.extend(page)
    rows.sort(key=lambda row: (row["ts"], row["seq"]), reverse=True)
    rows = rows[filters.offset:filters.offset + filters.limit]
    return answer(filters, total=total,
                  elapsed=(time.perf_counter() - started) * 1000,
                  results=[shape(row, filters) for row in rows],
                  schema_limited=fleet.has_schema_warning())

def answer(filters: Filters, *, total: int, elapsed: float, results: list,
           schema_limited: bool = False) -> dict:
    return {
        "query": filters.q,
        "browsing": not filters.needles,
        "mode": filters.mode,
        "scope": filters.scope,
        "roles": list(filters.roles),
        "categories": list(filters.categories),
        "since": filters.since,
        "until": filters.until,
        "hide_closed": filters.hide_closed,
        "total": total,
        "elapsed_ms": elapsed,
        "results": results,
        "schema_limited": schema_limited,
    }
