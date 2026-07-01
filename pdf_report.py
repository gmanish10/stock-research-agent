"""
Render a markdown research report to a dated PDF.

Pipeline: markdown -> HTML (with tables) -> PDF via xhtml2pdf (pure-Python, no system
binaries). Kept separate from the engine so main.research() stays text-only (CLI-friendly)
and the bot decides whether to deliver a PDF or fall back to chunked text.
"""

from __future__ import annotations

import os
import re
import datetime

import markdown
from xhtml2pdf import pisa

_CSS = """
@page { size: A4; margin: 1.6cm; }
body { font-family: Helvetica, sans-serif; font-size: 9pt; line-height: 1.35; color: #111; }
h1 { font-size: 15pt; margin: 0 0 6px; }
h2 { font-size: 12pt; margin: 14px 0 4px; border-bottom: 1px solid #ccc; padding-bottom: 2px; }
h3 { font-size: 10pt; margin: 10px 0 3px; }
/* table-layout:fixed gives every column an equal share so a key/value table with an
   empty header row can't collapse its label column to zero width (which squished the
   Ratings section vertically). word-break keeps long cell text inside its column. */
table { border-collapse: collapse; width: 100%; margin: 6px 0; table-layout: fixed; }
th, td { border: 1px solid #999; padding: 3px 5px; font-size: 8pt; text-align: left;
         word-break: break-word; overflow-wrap: break-word; vertical-align: top; }
th { background: #eee; }
code { font-family: Courier, monospace; font-size: 8pt; }
hr { border: none; border-top: 1px solid #ccc; }
"""


def _safe_label(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", label or "")[:12] or "report"


# Currency glyphs the built-in PDF fonts lack (they render as ■ tofu) -> readable ISO codes.
_GLYPH_FALLBACKS = {"₩": "KRW ", "₹": "Rs ", "₽": "RUB ", "₺": "TRY "}


def _transliterate(text: str) -> str:
    for ch, rep in _GLYPH_FALLBACKS.items():
        text = text.replace(ch, rep)
    return text


def _add_colgroups(html: str) -> str:
    """xhtml2pdf auto-sizes columns and sometimes starves the last one (the 3-col 'Signal'
    column collapsed to ~1 word, wrapping vertically). Injecting a <colgroup> with explicit
    equal widths forces an even split — xhtml2pdf honours <col> widths even though it ignores
    CSS table-layout:fixed. Only tables with 3+ columns need it (2-col ones render fine)."""
    def repl(m):
        table = m.group(0)
        first = re.search(r"<tr>(.*?)</tr>", table, re.S)
        if not first:
            return table
        n = len(re.findall(r"<t[hd]\b", first.group(1)))
        if n < 3:
            return table
        w = round(100 / n, 3)
        cols = "".join(f'<col style="width:{w}%" />' for _ in range(n))
        return re.sub(r"(<table[^>]*>)", lambda mm: mm.group(1) + f"<colgroup>{cols}</colgroup>",
                      table, count=1)
    return re.sub(r"<table\b.*?</table>", repl, html, flags=re.S)


def _cells(line):
    s = line.strip().strip("|")
    return [c.strip() for c in s.split("|")]


def _is_row(line):
    s = line.strip()
    return len(s) > 1 and s.startswith("|") and s.endswith("|")


def _is_sep(line):
    s = line.strip()
    return bool(re.fullmatch(r"\|[\s:|-]+\|", s)) and "-" in s


def _flatten_keyvalue_tables(md: str) -> str:
    """The model formats SHORT/MID/LONG ratings as 2-column key/value tables with an EMPTY header
    row (| | |). xhtml2pdf sizes columns from header content, so an empty header collapses the
    label column to ~zero width and the text wraps one word per line. Convert ONLY those
    empty-header tables to bullet lines (which render perfectly); real headed tables are untouched."""
    lines = md.split("\n")
    out, i = [], 0
    while i < len(lines):
        if _is_row(lines[i]) and i + 1 < len(lines) and _is_sep(lines[i + 1]):
            header = _cells(lines[i])
            j = i + 2
            body = []
            while j < len(lines) and _is_row(lines[j]):
                body.append(_cells(lines[j]))
                j += 1
            if body and all(h == "" for h in header):      # empty-header key/value table -> flatten
                for row in body:
                    key = row[0].strip("* ").strip()       # the model may already bold the key
                    vals = " · ".join(c for c in row[1:] if c)
                    out.append(f"- **{key}** — {vals}" if key and vals else f"- {key or vals}")
                out.append("")
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def build_pdf(report_md: str, label: str, out_dir: str | None = None,
              when: str | None = None) -> str:
    """Write `report_md` to <out_dir>/<YYYY-MM-DD>_<LABEL>.pdf and return the path.
    Raises on failure so the caller can fall back to text."""
    out_dir = out_dir or os.environ.get("REPORTS_DIR") or \
        os.path.join(os.path.dirname(__file__), "reports")
    os.makedirs(out_dir, exist_ok=True)
    when = when or datetime.date.today().isoformat()
    path = os.path.join(out_dir, f"{when}_{_safe_label(label)}.pdf")

    md = _transliterate(_flatten_keyvalue_tables(report_md))
    body = _add_colgroups(markdown.markdown(md, extensions=["tables", "fenced_code"]))
    html = (f"<html><head><meta charset='utf-8'><style>{_CSS}</style></head>"
            f"<body>{body}</body></html>")
    with open(path, "wb") as fh:
        result = pisa.CreatePDF(html, dest=fh, encoding="utf-8")
    if result.err:
        raise RuntimeError("xhtml2pdf reported errors while rendering the report")
    return path
