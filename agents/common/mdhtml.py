"""A small markdown-to-HTML renderer for briefs: headings, paragraphs, bullet and numbered
lists, tables, bold, italic, inline code, links and bare URLs. Everything is escaped
first, so raw HTML in a brief (written from web pages) shows as text, and only http(s)
links become anchors.
"""

from __future__ import annotations

import html
import re

_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BARE_URL = re.compile(r"(?<![\"'=>])\bhttps?://[^\s<>()\]]+[^\s<>()\].,;:!?'\"]")
_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])")


def inline(text: str) -> str:
    out = html.escape(text, quote=True)
    codes: list[str] = []

    def keep_code(m: re.Match) -> str:
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    out = _CODE.sub(keep_code, out)
    links: list[str] = []

    def keep_link(label: str, url: str) -> str:
        raw = html.unescape(url)
        if raw.startswith(("http://", "https://")) or (not re.match(r"^[a-z][a-z0-9+.-]*:", raw, re.I)):
            links.append(f'<a href="{html.escape(raw, quote=True)}">{label}</a>')
        else:  # javascript:, data: and the like stay text
            links.append(label)
        return f"\x01{len(links) - 1}\x01"

    out = _LINK.sub(lambda m: keep_link(m.group(1), m.group(2)), out)
    out = _BARE_URL.sub(lambda m: keep_link(m.group(0), m.group(0)), out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC.sub(r"<em>\1</em>", out)
    out = re.sub("\x01(\\d+)\x01", lambda m: links[int(m.group(1))], out)
    return re.sub("\x00(\\d+)\x00", lambda m: codes[int(m.group(1))], out)


def to_html(md: str, heading_offset: int = 0) -> str:
    """heading_offset shifts heading levels down, e.g. 1 renders `#` as <h2>."""
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    i = 0

    def flush() -> None:
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush()
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush()
            level = min(6, len(m.group(1)) + heading_offset)
            out.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
            continue
        if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{3,}", lines[i + 1]):
            flush()
            head = _cells(stripped)
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i].strip()))
                i += 1
            out.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows)
                       + "</tbody></table>")
            continue
        lm = re.match(r"^([-*]|\d+[.)])\s+(.*)$", stripped)
        if lm:
            flush()
            ordered = lm.group(1)[0].isdigit()
            items = []
            while i < len(lines):
                cur = re.match(r"^\s*([-*]|\d+[.)])\s+(.*)$", lines[i])
                if cur and cur.group(1)[0].isdigit() == ordered:
                    items.append(cur.group(2))
                elif lines[i].startswith(("  ", "\t")) and lines[i].strip() and items:
                    items[-1] += " " + lines[i].strip()  # continuation or nested line
                else:
                    break
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{inline(it)}</li>" for it in items) + f"</{tag}>")
            continue
        para.append(stripped)
        i += 1
    flush()
    return "\n".join(out)


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]
