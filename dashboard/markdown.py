from __future__ import annotations

import html
import re

FENCE = re.compile(r"^\s*(?:```+|~~~+)\s*([\w+#.-]*)\s*$")
HEADING = re.compile(r"^(\s*)(#{1,6})\s+(.*)$")
BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
NUMBER = re.compile(r"^(\s*)(\d{1,9})[.)]\s+(.*)$")
QUOTE = re.compile(r"^\s*>\s?(.*)$")
RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")

CODE_SPAN = re.compile(r"`([^`]+)`")
IMAGE = re.compile(r"!\[([^\]]*)\]\(([^\s)]*)\)")
STRONG = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)

STRONG_UNDERSCORE = re.compile(r"(?<![\w])__(?=\S)(.+?)(?<=\S)__(?![\w])", re.S)
STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.S)
EM_STAR = re.compile(r"\*(?=\S)([^*]+?)(?<=\S)\*", re.S)
EM_UNDERSCORE = re.compile(r"(?<![\w])_(?=\S)([^_]+?)(?<=\S)_(?![\w])", re.S)
LINK = re.compile(r"\[([^\]]*)\]\(([^\s)]+)\)")

TAG = re.compile(r"<[^>]+>")

SAFE_SCHEMES = ("http://", "https://", "mailto:")

INDENT_SPACES = 2

DELIMITER_CELL = re.compile(r"^:?-{1,}:?$")

def escape(text: str | None) -> str:
    return html.escape((text or "").replace("\x00", ""), quote=True)

def safe_url(url: str) -> str | None:
    return url if url.startswith(SAFE_SCHEMES) else None

def inline(text: str | None) -> str:
    source = escape(text)
    stash: list[str] = []

    def keep(tagged: str) -> str:
        stash.append(tagged)
        return f"\x00{len(stash) - 1}\x00"

    def code_span(match: re.Match) -> str:
        return keep(f"<code>{match.group(1)}</code>")

    def link(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        if safe_url(url) is None:
            return keep(match.group(0))
        return keep(f'<a href="{url}" rel="noreferrer noopener">{label}</a>')

    source = CODE_SPAN.sub(code_span, source)
    source = IMAGE.sub(lambda match: keep(match.group(0)), source)
    source = LINK.sub(link, source)
    source = STRONG.sub(r"<strong>\1</strong>", source)
    source = STRONG_UNDERSCORE.sub(r"<strong>\1</strong>", source)
    source = STRIKE.sub(r"<del>\1</del>", source)
    source = EM_STAR.sub(r"<em>\1</em>", source)
    source = EM_UNDERSCORE.sub(r"<em>\1</em>", source)
    for index, tagged in enumerate(stash):
        source = source.replace(f"\x00{index}\x00", tagged)
    return source

def plain(text: str, limit: int = 0) -> str:
    first = (text or "").strip().splitlines()
    source = first[0] if first else ""
    if FENCE.match(source):
        return ""
    source = HEADING.sub(lambda match: match.group(3), source)
    source = BULLET.sub(lambda match: match.group(2), source)
    source = NUMBER.sub(lambda match: match.group(3), source)
    source = QUOTE.sub(lambda match: match.group(1), source)
    source = html.unescape(TAG.sub("", inline(source))).strip()
    return source[:limit] if limit else source

def split_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in body.split("|")]

def delimiter_row(line: str, columns: int) -> list[str] | None:
    cells = split_row(line)
    if len(cells) != columns or not all(DELIMITER_CELL.match(cell) for cell in cells):
        return None
    aligned: list[str] = []
    for cell in cells:
        centre = cell.startswith(":") and cell.endswith(":")
        right = cell.endswith(":") and not centre
        aligned.append(" center" if centre else " right" if right else "")
    return aligned

def table(header: list[str], aligns: list[str], rows: list[list[str]]) -> str:
    def cells(tag: str, values: list[str]) -> str:
        padded = values + [""] * (len(aligns) - len(values))
        return "".join(
            f'<{tag} class="ta{align}">{inline(value)}</{tag}>'
            for value, align in zip(padded, aligns))

    head = cells("th", header)
    body = "".join(f"<tr>{cells('td', row)}</tr>" for row in rows)
    return (f'<div class="md-table"><table><thead><tr>{head}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>")

def render(text: str | None) -> str:
    lines = (text or "").replace("\x00", "").splitlines()
    out: list[str] = []
    index = 0
    paragraph: list[str] = []
    quote: list[str] = []
    lists: list[str] = []
    open_items: list[int] = []
    fence = None
    language = ""
    code: list[str] = []

    def close_lists(keep: int = 0) -> None:
        while len(lists) > keep:
            if open_items and open_items[-1] == len(lists):
                out.append("</li>")
                open_items.pop()
            out.append(f"</{lists.pop()}>")

    def flush_paragraph() -> None:
        if paragraph:
            joined = " ".join(paragraph).strip()
            if joined:
                out.append(f"<p>{inline(joined)}</p>")
            paragraph.clear()

    def flush_quote() -> None:
        if quote:
            body = " ".join(quote).strip()
            if body:
                out.append(f"<blockquote>{inline(body)}</blockquote>")
            quote.clear()

    def close_code() -> None:
        nonlocal fence, language, code
        if fence is not None:
            label = f'<span class="lang">{escape(language)}</span>' if language else ""
            body = html.escape("\n".join(code))
            out.append(f'<pre class="code">{label}<code>{body}</code></pre>')
        fence, language, code = None, "", []

    def list_item(tag: str, depth: int, body: str) -> None:
        close_lists(depth)
        while len(lists) < depth:
            out.append(f"<{tag}>")
            lists.append(tag)
        if open_items and open_items[-1] >= depth:
            out.append("</li>")
            open_items.pop()
        out.append(f"<li>{inline(body)}")
        open_items.append(depth)

    def flush_blocks() -> None:
        flush_paragraph()
        flush_quote()
        close_lists()

    while index < len(lines):
        line = lines[index]
        index += 1
        if fence is not None:
            if FENCE.match(line):
                close_code()
            else:
                code.append(line)
            continue

        if "|" in line and index < len(lines):
            header = split_row(line)
            aligns = delimiter_row(lines[index], len(header))
            if header and aligns is not None:
                flush_blocks()
                index += 1
                rows: list[list[str]] = []
                while (index < len(lines) and "|" in lines[index]
                       and not FENCE.match(lines[index])):
                    rows.append(split_row(lines[index]))
                    index += 1
                out.append(table(header, aligns, rows))
                continue

        match = FENCE.match(line)
        if match:
            flush_blocks()
            fence, language = "open", match.group(1)
            continue
        if RULE.match(line):
            flush_blocks()
            out.append("<hr>")
            continue
        match = HEADING.match(line)
        if match:
            flush_blocks()
            level = len(match.group(2))
            out.append(f"<h{level}>{inline(match.group(3).strip())}</h{level}>")
            continue
        match = BULLET.match(line)
        if match:
            flush_paragraph()
            flush_quote()
            list_item("ul", len(match.group(1)) // INDENT_SPACES + 1, match.group(2))
            continue
        match = NUMBER.match(line)
        if match:
            flush_paragraph()
            flush_quote()
            list_item("ol", len(match.group(1)) // INDENT_SPACES + 1, match.group(3))
            continue
        match = QUOTE.match(line)
        if match:
            flush_paragraph()
            close_lists()
            quote.append(match.group(1))
            continue
        if not line.strip():
            flush_blocks()
            continue
        flush_quote()
        close_lists()
        paragraph.append(line.strip())

    flush_blocks()
    close_code()
    return "".join(out)
