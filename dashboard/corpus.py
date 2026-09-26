from __future__ import annotations

KIND = "json_extract(p.value,'$.kind')"

TEXT = "json_extract(p.value,'$.text')"

WHITESPACE = "' ' || char(9) || char(10) || char(13)"

CONVERSATION_KINDS = ("text",)

HEAVY_KINDS = ("reasoning", "tool", "changes")
ALL_KINDS = CONVERSATION_KINDS + HEAVY_KINDS

KIND_CATEGORY = (
    f"CASE {KIND} "
    "WHEN 'text' THEN CASE m.role WHEN 'user' THEN 'user_text' "
    "ELSE 'assistant_text' END "
    "WHEN 'reasoning' THEN 'reasoning' "
    "WHEN 'tool' THEN 'tool_input' "
    "WHEN 'changes' THEN 'changes' END")

RECORD_TEXT = (
    f"CASE {KIND} "
    "WHEN 'tool' THEN coalesce(json_extract(p.value,'$.input'),'') "
    "WHEN 'changes' THEN coalesce(json_extract(p.value,'$.files'),'') "
    "ELSE coalesce(json_extract(p.value,'$.text'),'') END")

TOOL_OUTPUT_TEXT = "coalesce(json_extract(p.value,'$.output'),'')"
TOOL_OUTPUT_CATEGORY = "'tool_output'"

TABLE_ALIASES = {"threads": ("threads",), "messages": ("messages",)}
COLUMN_ALIASES = {
    "threads": {
        "id": ("id",), "title": ("title",), "status": ("status",),
        "turn_state": ("turn_state",), "created_at": ("created_at",),
        "updated_at": ("updated_at",), "model": ("model",),
        "byok_connection": ("byok_connection",),
        "sidebar_archived_at": ("sidebar_archived_at",),
        "last_turn_finished_at": ("last_turn_finished_at",),
    },
    "messages": {
        "thread_id": ("thread_id",), "seq": ("seq",), "role": ("role",),
        "ts": ("ts",), "parts_json": ("parts_json",),
        "attachments_json": ("attachments_json",), "metrics_json": ("metrics_json",),
    },
}
REQUIRED_COLUMNS = {
    "threads": ("id",),
    "messages": ("thread_id", "seq", "role", "ts", "parts_json"),
}
COLUMN_DEFAULTS = {
    ("threads", "title"): "''", ("threads", "status"): "'open'",
    ("threads", "turn_state"): "'idle'", ("threads", "created_at"): "0",
    ("threads", "updated_at"): "0", ("threads", "model"): "''",
    ("threads", "byok_connection"): "''",
    ("threads", "sidebar_archived_at"): "0",
    ("threads", "last_turn_finished_at"): "0",
    ("messages", "attachments_json"): "'[]'", ("messages", "metrics_json"): "''",
}

def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'

def data_version(con, source: str | None = None) -> int:
    prefix = f"{source}." if source else ""
    return int(con.execute(f"pragma {prefix}data_version").fetchone()[0])

def inspect_schema(con, source: str | None = None) -> dict:
    prefix = f"{source}." if source else ""
    names = [row[0] for row in con.execute(
        f"select name from {prefix}sqlite_master"
        " where type in ('table','view')")]
    catalog = {}
    for name in names:
        escaped = name.replace("'", "''")
        catalog[name] = {row[1] for row in con.execute(
            f"pragma {prefix}table_info('{escaped}')")}
    tables = {logical: next((candidate for candidate in aliases
                             if candidate in catalog), None)
              for logical, aliases in TABLE_ALIASES.items()}
    columns = {}
    for logical, table in tables.items():
        available = catalog.get(table) or set()
        columns[logical] = {field: next((candidate for candidate in aliases
                                         if candidate in available), None)
                            for field, aliases in COLUMN_ALIASES[logical].items()}
    missing = [f"{table}.{field}" for table, fields in REQUIRED_COLUMNS.items()
               for field in fields
               if tables[table] is None or columns[table][field] is None]
    return {"catalog": catalog, "tables": tables, "columns": columns,
            "missing": missing}

def schema_problem(schema: dict | None) -> str | None:
    missing = (schema or {}).get("missing") or []
    return "missing required schema: " + ", ".join(missing) if missing else None

def adapt_sql(sql: str, schema: dict | None, project_key: str) -> str:
    if not schema:
        return sql
    for logical, alias in (("threads", "t"), ("messages", "m")):
        table = schema["tables"].get(logical)
        if table:
            sql = sql.replace(f"{project_key}.{logical}",
                              f"{project_key}.{quote(table)}")
        for field in COLUMN_ALIASES[logical]:
            actual = schema["columns"][logical].get(field)
            expression = (f"{alias}.{quote(actual)}" if actual
                          else COLUMN_DEFAULTS.get((logical, field)))
            if expression:
                sql = sql.replace(f"{alias}.{field}", expression)
    return sql


def not_blank(expression: str) -> str:
    return f"length(trim(coalesce({expression},''), {WHITESPACE})) > 0"

BOARD_THREADS = (
    "SELECT t.id AS id, coalesce(t.title,'') AS title,"
    " coalesce(t.model,'') AS model,"
    " coalesce(t.byok_connection,'') AS byok_connection,"
    " coalesce(t.turn_state,'idle') AS turn_state,"
    " CASE WHEN coalesce(t.turn_state,'idle') IN ('','idle') THEN 1 ELSE 0 END"
    "  AS finished,"
    " coalesce(t.last_turn_finished_at,0) AS finished_at,"
    " coalesce(t.created_at,0) AS created_at,"
    " (SELECT count(*) FROM {alias}.messages m"
    "  WHERE m.thread_id = t.id) AS messages,"
    " coalesce((SELECT max(m.ts) FROM {alias}.messages m"
    "  WHERE m.thread_id = t.id),0) AS last_ts,"
    " coalesce((SELECT max(m.ts) FROM {alias}.messages m"
    "  WHERE m.thread_id = t.id AND m.role = 'user'),0) AS started_at"
    " FROM {alias}.threads t"
    " WHERE (coalesce(t.turn_state,'idle') NOT IN ('','idle')"
    "        OR coalesce(t.last_turn_finished_at,0) > {since_ms})"
    " AND EXISTS (SELECT 1 FROM {alias}.messages m WHERE m.thread_id = t.id)")

LAST_TEXT = (
    "SELECT m.thread_id AS thread_id, m.role AS role, m.ts AS ts,"
    " json_extract(p.value,'$.text') AS text"
    " FROM {alias}.messages m, json_each(m.parts_json) p"
    " WHERE json_extract(p.value,'$.kind') = 'text'"
    " AND {not_blank}"
    " AND m.thread_id IN ({ids})"
    " ORDER BY m.seq DESC, p.key DESC")

def board_threads(alias: str, since_ms: int, schema: dict | None = None) -> str:
    return adapt_sql(BOARD_THREADS.format(alias=alias, since_ms=int(since_ms)),
                     schema, alias)

def last_text(alias: str, count: int, schema: dict | None = None) -> str:
    sql = LAST_TEXT.format(alias=alias, not_blank=not_blank(TEXT),
                           ids=_placeholders(count))
    return adapt_sql(sql, schema, alias)

SUBJECT_CHARS = 2000

NOT_BLANK = not_blank(TEXT)

RECORD_COLUMNS = (
    "m.thread_id AS thread_id, coalesce(t.title,'') AS thread_title, "
    "m.seq AS seq, m.role AS role, {category} AS category, m.ts AS ts, "
    "{text} AS text")

SEARCH_COLUMNS = (
    "? AS project_key, ? AS project, ? AS project_path, "
    "m.thread_id AS thread_id, coalesce(t.title,'') AS thread_title, "
    "coalesce(t.status,'open') AS thread_status, "
    "m.seq AS seq, m.role AS role, m.ts AS ts, p.key AS part_index, "
    "{category} AS category, {text} AS text, "
    "coalesce(json_extract(p.value,'$.toolName'),'') AS tool_name, "
    "substr(coalesce(json_extract(p.value,'$.input'),''),1,"
    f"{SUBJECT_CHARS}) AS tool_input")

def _placeholders(count: int) -> str:
    return ",".join("?" * count)

def _record_body(alias: str, *, columns: str, kinds: tuple, text: str,
                 extra: str) -> str:
    return (
        f"SELECT {columns}\n"
        f"FROM {alias}.messages m\n"
        f"LEFT JOIN {alias}.threads t ON t.id = m.thread_id,"
        f" json_each(m.parts_json) p\n"
        f"WHERE {KIND} IN ({_placeholders(len(kinds))})\n"
        f"  AND {not_blank(text)}\n"
        f"{extra}")

def record_select(alias: str, *, kinds: tuple = ALL_KINDS,
                  schema: dict | None = None) -> tuple[str, list]:
    sql = _record_body(
        alias, kinds=tuple(kinds), text=RECORD_TEXT, extra="",
        columns=RECORD_COLUMNS.format(category=KIND_CATEGORY, text=RECORD_TEXT))
    params: list = list(kinds)
    if "tool" in kinds:
        sql = f"{sql}\nUNION ALL\n{select_tool_output(alias)[0]}"
    return adapt_sql(sql, schema, alias), params

def select_tool_output(alias: str, *, columns: str | None = None,
                       extra: str = "") -> tuple[str, list]:
    columns = columns or RECORD_COLUMNS.format(category=TOOL_OUTPUT_CATEGORY,
                                               text=TOOL_OUTPUT_TEXT)
    sql = (
        f"SELECT {columns}\n"
        f"FROM {alias}.messages m\n"
        f"LEFT JOIN {alias}.threads t ON t.id = m.thread_id,"
        f" json_each(m.parts_json) p\n"
        f"WHERE {KIND} = 'tool'\n"
        f"  AND {not_blank(TOOL_OUTPUT_TEXT)}\n"
        f"{extra}")
    return sql, []

def search_branch(project, *, kinds: tuple = CONVERSATION_KINDS,
                  extra: str = "", extra_params: list | None = None) -> tuple[str, list]:
    extra_params = list(extra_params or [])
    columns = SEARCH_COLUMNS.format(category=KIND_CATEGORY, text=RECORD_TEXT)
    sql = _record_body(project.key, columns=columns, kinds=tuple(kinds),
                       text=RECORD_TEXT, extra=extra)
    params: list = [project.key, project.label, project.path, *kinds, *extra_params]
    if "tool" in kinds:
        other = select_tool_output(
            project.key, extra=extra,
            columns=SEARCH_COLUMNS.format(category=TOOL_OUTPUT_CATEGORY,
                                          text=TOOL_OUTPUT_TEXT))[0]
        sql = f"{sql}\nUNION ALL\n{other}"
        params = params + [project.key, project.label, project.path,
                           *extra_params]
    return adapt_sql(sql, project.schema, project.key), params

THREAD_PARTS = (
    "SELECT m.seq AS seq, m.role AS role, m.ts AS ts,"
    " coalesce(m.attachments_json,'[]') AS attachments_json,"
    " coalesce(m.metrics_json,'') AS metrics_json,"
    " p.key AS part_index,"
    " {kind} AS kind,"
    " json_extract(p.value,'$.text') AS text,"
    " json_extract(p.value,'$.open') AS \"open\","
    " json_extract(p.value,'$.collapse') AS collapse,"
    " json_extract(p.value,'$.toolName') AS tool_name,"
    " json_extract(p.value,'$.input') AS tool_input,"
    " json_extract(p.value,'$.output') AS tool_output,"
    " json_extract(p.value,'$.exitCode') AS exit_code,"
    " json_extract(p.value,'$.status') AS status,"
    " json_extract(p.value,'$.files') AS files,"
    " json_extract(p.value,'$.notice') AS notice"
    " FROM {alias}.messages m, json_each(m.parts_json) p"
    " WHERE {kind} IN ({kinds}) AND m.thread_id = ?"
    " ORDER BY m.seq, p.key")

RENDERED_KINDS = ("text", "reasoning", "tool", "changes", "notice")

def thread_parts(alias: str, schema: dict | None = None) -> tuple[str, list]:
    sql = THREAD_PARTS.format(alias=alias, kind=KIND,
                              kinds=_placeholders(len(RENDERED_KINDS)))
    return adapt_sql(sql, schema, alias), list(RENDERED_KINDS)

THREAD_LIST = (
    "SELECT t.id AS id, coalesce(t.title,'') AS title,"
    " coalesce(t.status,'open') AS status,"
    " coalesce(t.turn_state,'idle') AS turn_state,"
    " coalesce(t.created_at,0) AS created_at,"
    " coalesce(t.updated_at,0) AS updated_at,"
    " (SELECT count(*) FROM {alias}.messages m WHERE m.thread_id = t.id) AS messages,"
    " coalesce(t.sidebar_archived_at,0) AS archived_at"
    " FROM {alias}.threads t"
    " WHERE EXISTS (SELECT 1 FROM {alias}.messages m WHERE m.thread_id = t.id)"
    " ORDER BY coalesce(t.updated_at,0) DESC")

def thread_list(alias: str, schema: dict | None = None) -> str:
    return adapt_sql(THREAD_LIST.format(alias=alias), schema, alias)
