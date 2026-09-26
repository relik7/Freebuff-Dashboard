from __future__ import annotations

import json

from . import corpus, markdown

MAX_PART_CHARS = 200_000

AROUND_TURNS = 5

LABEL_CHARS = 120

TOOL_LABELS = {
    "str_replace": "Edit",
    "apply_patch": "Edit",
    "write_file": "Write",
    "write_doc": "Write doc",
    "read_files": "Read",
    "read_docs": "Read Docs",
    "read_subtree": "List deeply",
    "read_url": "Read URL",
    "code_search": "Search",
    "web_search": "Web Search",
    "glob": "Glob",
    "list_directory": "List",
    "run_terminal_command": "Run command",
    "write_todos": "Update todos",
    "skill": "Load Skill",
    "think_deeply": "Think",
    "task_completed": "Task complete",
    "spawn_agents": "Spawn agents",
    "suggest_followups": "Suggest follow-ups",
}

TOOL_ICONS = {
    "str_replace": "edit",
    "apply_patch": "edit",
    "write_file": "edit",
    "write_doc": "edit",
    "read_files": "read",
    "read_docs": "read",
    "read_thread_context": "read",
    "read_subtree": "list",
    "list_directory": "list",
    "read_url": "globe",
    "register_preview": "globe",
    "preview_open": "globe",
    "preview_snapshot": "globe",
    "preview_screenshot": "globe",
    "code_search": "search",
    "web_search": "search",
    "glob": "search",
    "run_terminal_command": "run",
    "write_todos": "todos",
    "skill": "bolt",
    "think_deeply": "think",
    "task_completed": "done",
}

TOOL_ICON_PREFIXES = (("preview", "globe"), ("read", "read"), ("search", "search"),
                      ("list", "list"), ("todo", "todos"))

SUBJECT_KEYS = ("path", "paths", "htmlPath", "filePath", "command", "pattern",
                "query", "url", "to")

class ReaderError(Exception):
    def __init__(self, message: str, status: int = 404) -> None:
        super().__init__(message)
        self.status = status

def clip(text: str | None) -> dict:
    body = text or ""
    if len(body) > MAX_PART_CHARS:
        return {"text": body[:MAX_PART_CHARS], "truncated": True}
    return {"text": body, "truncated": False}

def label(text: str) -> str:
    return markdown.plain(text, LABEL_CHARS)

def elapsed_text(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds)) / 1000
    if seconds < 10:
        return f"{seconds:.1f} s"
    total = round(seconds)
    if total < 60:
        return f"{total} s"
    minutes, rest = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {rest}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"

def metrics(raw) -> dict | None:
    try:
        loaded = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(loaded, dict):
        return None
    usage = loaded.get("usage")
    if not isinstance(usage, dict) or not usage:
        return None
    context = loaded.get("context")
    if not isinstance(context, dict):
        context = {}
    found = {
        "output_tokens": usage.get("outputTokens") or 0,
        "reasoning_tokens": usage.get("reasoningOutputTokens") or 0,
        "input_tokens": usage.get("inputTokens") or 0,
        "cached_input_tokens": usage.get("cachedInputTokens") or 0,
        "context_used_tokens": context.get("usedTokens") or 0,
        "window_tokens": context.get("windowTokens") or 0,
        "incomplete": bool(loaded.get("usageIncomplete")),
        "compacted": bool(loaded.get("compactions")),
    }
    return found

def usage_text(usage: dict | None) -> str:
    if not usage:
        return ""
    written = f"{usage['output_tokens']:,} out"
    if usage["reasoning_tokens"]:
        written += f" ({usage['reasoning_tokens']:,} thinking)"
    return ("~" if usage["incomplete"] else "") + written

def footer_detail(message: dict) -> str:
    usage = message.get("usage")
    bits: list[str] = []
    if usage:
        bits.append(f"in {usage['input_tokens']:,} "
                    f"({usage['cached_input_tokens']:,} cached)")
        if usage["window_tokens"]:
            bits.append(f"context {usage['context_used_tokens']:,} "
                        f"of {usage['window_tokens']:,}")
        if usage["compacted"]:
            bits.append("context compacted")
        if usage["incomplete"]:
            bits.append("usage reported incomplete")
    else:
        bits.append("no usage recorded")
    thoughts = sum(1 for part in message["blocks"] if part["kind"] == "reasoning")
    calls = sum(1 for part in message["blocks"] if part["kind"] == "tool")
    if thoughts:
        bits.append(f"{thoughts} thinking")
    if calls:
        bits.append(f"{calls} tool call" + ("" if calls == 1 else "s"))
    bits.append(f"seq {message['seq']}")
    return " · ".join(bits)

def tool_icon(name: str) -> str:
    found = TOOL_ICONS.get(name or "")
    if found:
        return found
    lowered = (name or "").lower()
    for prefix, icon in TOOL_ICON_PREFIXES:
        if lowered.startswith(prefix):
            return icon
    return "tool"

def tool_label(name: str) -> str:
    known = TOOL_LABELS.get(name or "")
    if known:
        return known
    words = [word for word in (name or "").split("_") if word]
    if not words:
        return name or "tool"
    return " ".join([words[0].capitalize(), *(word.lower() for word in words[1:])])

def tool_subject(raw_input) -> str:
    try:
        loaded = json.loads(raw_input or "{}")
    except (ValueError, TypeError):
        return ""
    if not isinstance(loaded, dict):
        return ""
    for key in SUBJECT_KEYS:
        value = loaded.get(key)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if isinstance(value, str) and value.strip():
            return value.strip()[:LABEL_CHARS]
    return ""

def attachments(raw: str | None) -> list[dict]:
    try:
        loaded = json.loads(raw or "[]")
    except ValueError:
        return []
    if not isinstance(loaded, list):
        return []
    found: list[dict] = []
    for entry in loaded:
        if not isinstance(entry, dict):
            continue
        found.append({"name": entry.get("name") or entry.get("filename") or "?",
                      "kind": entry.get("kind") or entry.get("type") or "file",
                      "path": entry.get("path") or entry.get("filePath") or ""})
    return found

def changed_files(raw: str | None) -> list[dict]:
    try:
        loaded = json.loads(raw or "[]")
    except ValueError:
        return []
    if not isinstance(loaded, list):
        return []
    found: list[dict] = []
    for entry in loaded:
        if not isinstance(entry, dict):
            continue
        found.append({
            "path": entry.get("path") or "",
            "status": entry.get("status") or "modified",
            "adds": entry.get("adds") or 0,
            "dels": entry.get("dels") or 0,
            "patch": clip(entry.get("patch") or ""),
        })
    return found

def part_index(row):
    try:
        return row["part_index"]
    except (KeyError, IndexError, TypeError):
        return None

def block(row) -> dict | None:
    kind = row["kind"]
    part = part_index(row)
    if kind == "text":
        text = row["text"]
        if text is None or not text.strip():
            return None
        clipped = clip(text)
        return {"kind": "text", "part": part,
                "html": markdown.render(clipped["text"]), **clipped}
    if kind == "reasoning":
        text = row["text"] or ""
        clipped = clip(text)
        return {"kind": "reasoning", "part": part,
                "icon": "think",
                "label": label(text),
                "html": markdown.render(clipped["text"]), **clipped}
    if kind == "tool":
        name = row["tool_name"] or "tool"
        raw_input = row["tool_input"]
        return {
            "kind": "tool",
            "part": part,
            "name": name,
            "icon": tool_icon(name),
            "label": tool_label(name),
            "detail": tool_subject(raw_input),
            "input": clip(str(raw_input if raw_input is not None else "")),
            "output": clip(str(row["tool_output"] or "")),
            "exit_code": row["exit_code"],
            "status": row["status"] or "",
        }
    if kind == "changes":
        files = changed_files(row["files"])
        return {"kind": "changes", "part": part,
                "icon": "edit",
                "files": files,
                "adds": sum(one["adds"] for one in files),
                "dels": sum(one["dels"] for one in files)}
    if kind == "notice":
        return {"kind": "notice", "part": part,
                "text": str(row["notice"] or row["text"] or "")}
    return None

def _message(seq: int, role: str, ts: int, raw_attachments: str | None,
             raw_metrics: str | None = None) -> dict:
    found = {"seq": seq, "role": role, "ts": ts, "blocks": [],
             "attachments": attachments(raw_attachments)}
    if role == "assistant":
        found["usage"] = metrics(raw_metrics)
    return found

def messages(rows) -> list[dict]:
    found: dict[int, dict] = {}
    order: list[int] = []
    for row in rows:
        seq = row["seq"]
        if seq not in found:
            found[seq] = _message(seq, row["role"], row["ts"],
                                  row["attachments_json"], row["metrics_json"])
            order.append(seq)
        part = block(row)
        if part is not None:
            found[seq]["blocks"].append(part)
    return [found[seq] for seq in order]

def turns(messages_found: list[dict]) -> list[dict]:
    found: list[dict] = []
    index = 0
    while index < len(messages_found):
        current = messages_found[index]
        following = messages_found[index + 1] if index + 1 < len(messages_found) else None
        if (current["role"] == "user" and following is not None
                and following["role"] == "assistant"
                and following["seq"] == current["seq"] + 1):
            after = (messages_found[index + 2]
                     if index + 2 < len(messages_found) else None)
            _time_reply(following, current, after)
            found.append({"seq": current["seq"], "ts": current["ts"],
                          "messages": [current, following]})
            index += 2
        else:
            found.append({"seq": current["seq"], "ts": current["ts"],
                          "messages": [current]})
            index += 1
    return found

def _time_reply(reply: dict, prompt: dict, after: dict | None) -> None:
    elapsed = reply["ts"] - prompt["ts"]
    reply["elapsed_ms"] = elapsed
    reply["elapsed_text"] = elapsed_text(elapsed)
    reply["elapsed_upper"] = bool(after is not None and after["ts"] == reply["ts"])
    reply["usage_text"] = usage_text(reply.get("usage"))
    reply["footer_detail"] = footer_detail(reply)

def window(found: list[dict], around: int) -> list[dict]:
    centre = next((index for index, turn in enumerate(found)
                   if any(message["seq"] == around for message in turn["messages"])),
                  None)
    if centre is None:
        return found
    return found[max(0, centre - AROUND_TURNS):centre + AROUND_TURNS + 1]

def thread(fleet, name: str | None, thread_id: str | None, *,
           around: int | None = None) -> dict:
    project = fleet.find(name)
    if project is None:
        raise ReaderError(f"no project {name!r}", status=404)
    if not project.readable:
        raise ReaderError(f"{project.label} cannot be read: {project.unreadable}",
                          status=503)
    if project.schema_limited:
        raise ReaderError(f"{project.label} cannot be read:"
                          f" {project.schema_warning}", status=503)
    entry = next((one for one in project.threads if one.id == thread_id), None)
    if entry is None:
        raise ReaderError(f"no thread {thread_id!r} in {project.label}", status=404)

    sql, kind_params = corpus.thread_parts(project.key, project.schema)
    params = [*kind_params, thread_id]
    rows = fleet.read(
        project, lambda connection: connection.execute(sql, params).fetchall())
    found = messages(rows)
    grouped = turns(found)
    if around is not None:
        grouped = window(grouped, int(around))
    return {
        "project": project.label,
        "project_path": project.path,
        "project_key": project.key,
        "thread_id": entry.id,
        "title": entry.title,
        "status": entry.status,
        "turn_state": entry.turn_state,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "message_count": entry.messages,
        "kinds": list(corpus.RENDERED_KINDS),
        "in_flight": entry.turn_state not in ("", "idle", None),
        "started_at": grouped[-1]["ts"] if grouped else entry.created_at,
        "turns": grouped,
    }
