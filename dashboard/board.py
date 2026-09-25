from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import config as config_module
from . import corpus
from . import markdown

LAST_TEXT_CHARS = 240

BYOK_CACHE = {"stamp": None, "models": {}}

def _connection_id(value) -> str:
    try:
        loaded = json.loads(value or "")
    except (TypeError, ValueError):
        return ""
    if not isinstance(loaded, dict):
        return ""
    ident = loaded.get("connectionId")
    return ident.strip() if isinstance(ident, str) else ""

def byok_models(path: Path | None = None) -> dict:
    target = Path(path) if path is not None else config_module.byok_path()
    try:
        info = target.stat()
    except OSError:
        return {}
    stamp = (str(target), info.st_mtime_ns, info.st_size)
    if BYOK_CACHE["stamp"] == stamp:
        return BYOK_CACHE["models"]
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    records = raw.get("connections") if isinstance(raw, dict) else raw
    found: dict = {}
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        ident = record.get("id")
        model = record.get("model")
        if isinstance(ident, str) and ident.strip() \
                and isinstance(model, str) and model.strip():
            found[ident.strip()] = model.strip()
    BYOK_CACHE["stamp"] = stamp
    BYOK_CACHE["models"] = found
    return found

def _last_texts(connection, project, ids: list[str]) -> dict:
    if not ids:
        return {}
    sql = corpus.last_text(project.key, len(ids), project.schema)
    found: dict = {}
    try:
        listed = list(connection.execute(sql, ids))
    except sqlite3.Error:
        return found
    for row in listed:
        ident = row["thread_id"]
        if ident in found:
            continue
        found[ident] = {"role": row["role"] or "", "ts": int(row["ts"] or 0),
                        "text": markdown.plain(row["text"] or "", LAST_TEXT_CHARS)}
    return found

def _row(project, row, said: dict | None) -> dict:
    started = int(row["started_at"] or 0) or int(row["created_at"] or 0)
    said = said or {}
    return {
        "id": row["id"],
        "project": project.label,
        "project_key": project.key,
        "title": row["title"],
        "model": row["model"] or "",
        "state": row["turn_state"],
        "finished": bool(row["finished"]),
        "finished_at": int(row["finished_at"] or 0),
        "messages": int(row["messages"] or 0),
        "started_at": started,
        "last_ts": int(row["last_ts"] or 0) or started,
        "last_role": said.get("role") or "",
        "last_text": said.get("text") or "",
    }

def running(fleet, seconds: float = 0.0,
            close_seconds: float = config_module.DEFAULT_CLOSE_SECONDS) -> dict:
    now = int(time.time() * 1000)
    since = now - int(round(float(close_seconds) * 1000))
    rows: list[dict] = []
    unnamed: list[tuple] = []
    connection = fleet.connection()
    for project in fleet.projects:
        if not project.readable or project.schema_limited:
            continue
        try:
            listed = connection.execute(
                corpus.board_threads(project.key, since,
                                     project.schema)).fetchall()
        except sqlite3.Error:
            continue
        said = _last_texts(connection, project, [row["id"] for row in listed])
        for row in listed:
            entry = _row(project, row, said.get(row["id"]))
            if not entry["model"]:
                unnamed.append((entry, row["byok_connection"] or ""))
            rows.append(entry)
    if any(byok for _, byok in unnamed):
        models = byok_models()
        for entry, byok in unnamed:
            entry["model"] = models.get(_connection_id(byok), "")
    rows.sort(key=lambda entry: (entry["finished"], -entry["started_at"],
                                 -entry["finished_at"]))
    flying = [entry for entry in rows if not entry["finished"]]
    return {
        "now": now,
        "beat": float(seconds or 0.0),
        "close_ms": int(round(float(close_seconds) * 1000)),
        "running": len(flying),
        "messages": sum(entry["messages"] for entry in flying),
        "longest_ms": max((max(0, now - entry["started_at"])
                           for entry in flying), default=0),
        "threads": rows,
    }
