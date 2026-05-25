#!/usr/bin/env python3
# MIT License
#
# Copyright (c) 2026 David Burghoff <burghoff@utexas.edu>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
process_program_cleo2025.py — PROCESS ONLY.

The "processor" half of the CLEO 2025 pipeline: it reads the raw files the
downloader (fetch_program_cleo2025.py) saved into data/ and turns them
into the single, clean, FINAL conference_data.json the downstream
build_conference_app.py / build_affiliation_map.py scripts consume. It does NO
live web scraping and downloads nothing.

Inputs it reads (all from a data/ subdirectory next to this script):
  1. CLEO2025_planner_expanded.html — the fully-expanded planner DOM.
  2. CLEO2025_short_courses.html    — the archived public short-courses page.
  3. the official Program + Abstracts PDF (CLEO2025_Program_Abstracts.pdf, or
     whatever single *.pdf is present in data/ — see _resolve_data_file).
  4. the official Program + Abstracts CSV (CLEO2025_Program_Abstracts.csv, or
     the lone *.csv in data/).

Output it writes (next to this script):
  conference_data.json

What it does
------------
1. Parses the saved planner HTML offline with lxml to recover the same day ->
   session -> talk structure a live browser pass would produce. Small helpers
   reproduce the browser's .innerText / .innerHTML semantics on the lxml tree,
   so the parse matches the live-rendered result (no live site, no browser). In
   particular .innerHTML preserves the <b>…</b> bold markup that CLEO 2025 uses
   to mark Invited talks (see _title_is_bolded / parse_talk_content).
2. Parses the saved short-courses HTML (an archived page whose <h2>/<h3>/<h4>
   blocks give course title / instructor / affiliation) and matches each course
   to its planner session by NORMALIZED TITLE (the page carries no SC codes).
3. Supplements poster-style sessions (whose talks the planner only exposes
   behind a popup) from the official abstract-book CSV.
4. Parses the PDF (abstracts, speaker underlines, affiliation lines) and the
   official CSV, and bundles everything into conference_data.json.

NOTE: an intermediate scraped-program CSV (CLEO2025_program.csv) is not
written or re-read here. That round-trip is unnecessary, so the scraped rows
are built in memory and handed straight to the JSON builder.
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


# -----------------------------------------------------------------------------
# Tiny verbose logger.
# -----------------------------------------------------------------------------
def log(msg: str) -> None:
    print(msg, flush=True)


# -----------------------------------------------------------------------------
# Hard-coded configuration — all inputs live under data/; the JSON output stays
# in the script directory (where the downstream builder expects it).
# -----------------------------------------------------------------------------
SCRIPT_DIR    = Path(__file__).resolve().parent
DATA_DIR      = SCRIPT_DIR / "data"

# Inputs the downloader produced.
INPUT_DOM_HTML          = DATA_DIR / "CLEO2025_planner_expanded.html"
INPUT_SHORTCOURSE_HTML  = DATA_DIR / "CLEO2025_short_courses.html"

# Name for the intermediate scraped-program CSV. We never write it (that
# intermediate CSV is skipped), but _autodetect_data_file references it
# to make sure it never mistakes that file for the official abstract CSV.
OUTPUT_CSV    = DATA_DIR / "CLEO2025_program.csv"

# The JSON the downstream scripts read stays in the SCRIPT directory.
OUTPUT_JSON   = SCRIPT_DIR / "conference_data.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)



# =============================================================================
# Small HTML helpers + session/talk content parsers
# (including _title_is_bolded: CLEO 2025 marks Invited talks via a bold title)
# =============================================================================
# -----------------------------------------------------------------------------
# Small HTML helpers
# -----------------------------------------------------------------------------
_HTML_ENTS = (
    ("&nbsp;", " "),
    ("&amp;",  "&"),
    ("&lt;",   "<"),
    ("&gt;",   ">"),
    ("&quot;", '"'),
    ("&#39;",  "'"),
    ("&apos;", "'"),
)


def html_to_text(s: str) -> str:
    """Light-weight HTML -> plain-text. Preserves line breaks from <br>."""
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in _HTML_ENTS:
        s = s.replace(a, b)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# -----------------------------------------------------------------------------
# Parsers for session header and talk content
# -----------------------------------------------------------------------------
def split_session_code(line: str) -> tuple[str, str]:
    """ 'AM1C. Sample Title'          -> ('AM1C', 'Sample Title')
        'JM2C . Sample & Title'       -> ('JM2C', 'Sample & Title')
        'SC477. Short Course: Topic…' -> ('SC477', 'Short Course: Topic…')
    """
    if "." not in line:
        return ("", line.strip())
    code, rest = line.split(".", 1)
    return (re.sub(r"\s+", "", code), rest.strip())


def split_talk_number(line: str) -> tuple[str, str]:
    """ 'AM1C.1. Title'  -> ('AM1C.1', 'Title')
        'JM2C .1. Title' -> ('JM2C.1', 'Title')
        'AM1C.2.'        -> ('AM1C.2', '')
        '4290312. Title' -> ('4290312', 'Title')   # bare numeric Final ID

    CLEO's planner uses two talk-number styles. Most coded sessions show a
    conference code like 'AM1C.1', but a large number of talks (poster /
    contributed, and anything supplemented from the abstract book) are shown
    with a bare numeric Final ID like '4290312'. The original regex required
    the number to begin with a letter, so the numeric form fell through and
    left the ID glued to the front of the title (breaking downstream matching
    in build_conference_app.py). We now try the coded form first, then fall back to
    a leading 'NNNN.' numeric ID."""
    # 1) Coded form: AM1C.1 / JM2C .1 / FM1D.1
    m = re.match(
        r"^\s*([A-Z][A-Z0-9 ]*?)\s*\.\s*(\d+)\s*\.?\s*(.*)$",
        line, flags=re.S,
    )
    if m:
        return (re.sub(r"\s+", "", m.group(1)) + "." + m.group(2),
                m.group(3).strip())
    # 2) Bare numeric Final ID: '4290312. Title' (or '4290312 Title').
    m = re.match(r"^\s*(\d{3,})\s*\.?\s+(.*)$", line, flags=re.S)
    if m:
        return (m.group(1), m.group(2).strip())
    return ("", line.strip())


def parse_session_header(text: str) -> dict:
    """Header paragraph text for a session, e.g.:

        AM1C. Sample Session Title
        Presider(s): <Name> (<Aff>)
        8:00 AM - 10:00 AM; W211
    """
    out = {"code": "", "title": "",
           "presiders_raw": "",
           "time": "", "location": ""}
    if not text:
        return out
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return out

    code, title = split_session_code(lines[0])
    out["code"]  = code
    out["title"] = title

    for ln in lines[1:]:
        if ln.lower().startswith("presider"):
            m = (re.match(r"^Presider\(s\):?\s*(.*)$", ln, flags=re.I)
                 or re.match(r"^Presider:?\s*(.*)$", ln, flags=re.I))
            if m:
                out["presiders_raw"] = m.group(1).strip()
        elif re.search(r"\d{1,2}:\d{2}\s*(AM|PM)", ln, flags=re.I):
            # "8:00 AM - 10:00 AM; W211" or just "1:30 PM - 5:30 PM; E212 AC"
            if ";" in ln:
                t, loc = ln.split(";", 1)
                out["time"]     = t.strip()
                out["location"] = loc.strip()
            else:
                out["time"] = ln.strip()
    return out


def parse_presiders(raw: str) -> list[dict]:
    """Split a presider string into [{name, affiliation}, ...].

    A session can list one OR several presiders. Multiple presiders are joined
    by a top-level comma or the word 'and' (occasionally '&'), e.g.:
        'Foo Bar (Inst A) and Baz Qux (Inst B)'
        'Foo Bar (Inst A), Baz Qux (Inst B)'
    Each presider is 'Name (Affiliation)'.

    Crucially, separators that appear INSIDE an affiliation's parentheses
    must NOT split it, e.g.:
        'Foo Bar (University of Science and Technology)'
        'Baz Qux (Natl Inst of Sci & Tech)'
        'Qui Gon (IFSW, University of Placeholder)'
    Those 'and'/'&'/',' tokens sit at paren-depth >= 1, so we only break on
    separators seen at depth 0. Each resulting part is then parsed as
    'Name (Affiliation)'."""
    if not raw:
        return []
    parts: list[str] = []
    buf = ""
    depth = 0
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch == "(":
            depth += 1; buf += ch; i += 1; continue
        if ch == ")":
            depth = max(0, depth - 1); buf += ch; i += 1; continue
        if depth == 0:
            # Top-level comma separator.
            if ch == ",":
                if buf.strip():
                    parts.append(buf.strip())
                buf = ""; i += 1; continue
            # Top-level ' & ' separator.
            if ch == "&":
                if buf.strip():
                    parts.append(buf.strip())
                buf = ""; i += 1; continue
            # Top-level ' and ' separator (needs surrounding spaces so it
            # never fires mid-word, e.g. on a name ending in '...and').
            if raw[i:i + 5].lower() == " and ":
                if buf.strip():
                    parts.append(buf.strip())
                buf = ""; i += 5; continue
        buf += ch; i += 1
    if buf.strip():
        parts.append(buf.strip())

    out = []
    for p in parts:
        m = re.match(r"^(.*?)\s*\((.*)\)\s*$", p)
        if m:
            out.append({"name": m.group(1).strip(),
                        "affiliation": m.group(2).strip()})
        else:
            out.append({"name": p.strip(), "affiliation": ""})
    return out


def _title_is_bolded(title_html: str) -> bool:
    """Decide whether a talk TITLE is emboldened in the planner DOM, which is
    how CLEO 2025 marks Invited talks.

    `title_html` is the HTML BEFORE the first <br> — i.e. "NUMBER. <b>Title</b>"
    for an invited talk, or "NUMBER. Title" for a contributed one. The bold may
    be split across multiple <b> runs, and the title may itself contain <i>…</i>
    emphasis, so rather than insisting on one <b> wrapping everything we compare
    the concatenated text inside <b> runs to the title's full text (number
    stripped). If the bold text covers essentially the whole title, it's
    Invited. A handful of bolded characters (e.g. a single emphasized word in an
    otherwise plain title) does NOT qualify."""
    if not title_html or "<b" not in title_html.lower():
        return False

    # Full visible title text, with the leading "NUMBER." removed.
    full_text = html_to_text(title_html)
    _, body = split_talk_number(full_text)
    body_norm = re.sub(r"\s+", "", body)
    if not body_norm:
        return False

    # Concatenated text inside every <b>…</b> run.
    bold_runs = re.findall(r"<b\b[^>]*>(.*?)</b>", title_html, flags=re.I | re.S)
    bold_text = html_to_text(" ".join(bold_runs))
    # The number can sit inside or outside the bold; strip a leading number
    # from the bold text too so the comparison is apples-to-apples.
    _, bold_body = split_talk_number(bold_text)
    bold_norm = re.sub(r"\s+", "", bold_body or bold_text)
    if not bold_norm:
        return False

    # Invited when the bold text covers (almost) the entire title. Using a high
    # coverage ratio tolerates minor punctuation/whitespace differences between
    # the split <b> runs and the rendered title without misfiring on a title
    # that merely contains one bold word.
    covered = len(bold_norm) >= 0.9 * len(body_norm)
    return covered


def parse_talk_content(html: str) -> dict:
    """The talk-content cell has the shape:

        <p class="pagecontents">
          NUMBER. TITLE<br>
          <i> [<u>SPEAKER</u>;] AUTHORS … </i><br>
          <table> … View Presentation link … </table>
        </p>

    Note that TITLE itself can contain <i>…</i> (italic words inside the
    title), so we split on the FIRST <br> rather than on <i>."""
    out = {"number": "", "title": "",
           "authors": [], "speaker": "",
           "status_tags": []}
    if not html:
        return out

    # Drop the View-Presentation table at the end so we don't catch its HTML.
    cleaned = re.sub(r"<table[\s\S]*?</table>", "", html, flags=re.I)
    # Split on the FIRST <br> -> title side / authors side.
    parts = re.split(r"<br\s*/?>", cleaned, maxsplit=1, flags=re.I)
    title_part = parts[0]
    rest       = parts[1] if len(parts) > 1 else ""

    # In `rest`, authors live inside a single <i>…</i>. There should be no
    # other <i>… by this point.
    m = re.search(r"<i>(.*?)</i>", rest, flags=re.I | re.S)
    authors_html = m.group(1) if m else rest

    # ---- title -------------------------------------------------------------
    # Detect the CLEO-2025 "Invited" convention BEFORE stripping tags: invited
    # talks have their TITLE emboldened (wrapped in <b>…</b>) in the planner
    # DOM, whereas contributed talks have a plain-text title. The bold may be
    # split across several <b> runs (e.g. "<b>… and</b><b> more</b>"), so we
    # compare the concatenated bold text against the full title text rather
    # than requiring a single <b> wrapping the whole thing.
    title_is_bold = _title_is_bolded(title_part)

    title_text = html_to_text(title_part)
    number, body = split_talk_number(title_text)
    out["number"] = number
    out["title"]  = body

    # Status tags
    for pattern, label in [
        (r"\[Invited Talk\]",   "Invited"),
        (r"\[Tutorial Talk\]",  "Tutorial"),
        (r"\[Pre-recorded\]",   "Pre-recorded"),
        (r"\(WITHDRAWN\)",      "Withdrawn"),
    ]:
        if re.search(pattern, out["title"], flags=re.I):
            out["status_tags"].append(label)
            out["title"] = re.sub(pattern, "", out["title"], flags=re.I).strip()
    out["title"] = re.sub(r"\s{2,}", " ", out["title"])

    # Bolded title -> Invited (CLEO 2025 planner convention). Only add when not
    # already flagged by an explicit "[Invited Talk]" marker above.
    #
    # We tag bold-derived invited talks as "Invited (bold)" rather than plain
    # "Invited" so the distinction survives the CSV->JSON pipeline: bold-only
    # invited talks shorter than 30 min are dropped downstream (see the talk
    # assembly loop), whereas explicitly "[Invited Talk]"-marked talks are
    # always kept. The "(bold)" qualifier is normalized back to "Invited" in the
    # final emitted status for any talk that survives.
    if title_is_bold and "Invited" not in out["status_tags"]:
        out["status_tags"].append("Invited (bold)")

    # ---- authors -----------------------------------------------------------
    # Speaker(s): every <u>…</u> block (usually just one).
    u_blocks  = re.findall(r"<u>(.*?)</u>", authors_html, flags=re.I | re.S)
    speakers  = [html_to_text(u) for u in u_blocks if html_to_text(u)]
    out["speaker"] = "; ".join(speakers)

    # Full author list: strip <u> markers but keep their text, then split on ;
    no_u         = re.sub(r"</?u>", "", authors_html, flags=re.I)
    authors_text = html_to_text(no_u)
    authors_list = [a.strip(" \t\r\n;,") for a in authors_text.split(";")]
    out["authors"] = [a for a in authors_list if a]

    return out


def time_cell_status(time_text: str) -> str:
    """Withdrawn talks show '… Withdrawn' in the time cell instead of a time."""
    if time_text and "withdrawn" in time_text.lower():
        return "Withdrawn"
    return ""


# =============================================================================
# Official-data-file resolution. Downloads are saved under the fixed names;
# otherwise the lone *.pdf / *.csv in data/ is used.
# =============================================================================
def _autodetect_data_file(suffix: str, label: str) -> Path:
    """Return the single file in data/ with the given suffix (e.g. '.pdf').

    The in-app download step is disabled (the site's download buttons don't
    work reliably and the served filenames don't match the fixed names
    anyway), so the Program + Abstracts files are placed into
    data/ manually under WHATEVER name the site gave them, e.g.
    'CLEO 2025_06DEC2025-1.pdf' / 'CLEO 2025_06DEC2025.csv'. Rather than force
    a rename, we just pick up the lone PDF and the lone CSV.

    The returned Path may not exist (if nothing matches); callers already
    guard on .exists() and warn. The scrape_child.log is excluded so a stray
    .log never confuses things, and known *output* files we ourselves write
    (the scraped program CSV) are excluded from the CSV search so we don't
    accidentally treat our own output as the official abstract book.
    """
    candidates = sorted(
        p for p in DATA_DIR.glob(f"*{suffix}")
        if p.name != OUTPUT_CSV.name        # never match our own scraped CSV
    )
    if len(candidates) == 1:
        log(f"[autodetect] {label}: using {candidates[0].name}")
        return candidates[0]
    if not candidates:
        log(f"[autodetect] {label}: WARNING — no '*{suffix}' file found in "
            f"{DATA_DIR}. Place the official file there. (Downloads are "
            "disabled, so nothing will be fetched automatically.)")
        # Return a non-existent placeholder path so callers' .exists() guard
        # fires and they warn/skip gracefully.
        return DATA_DIR / f"__MISSING__{suffix}"
    # More than one match: pick the first deterministically but make the
    # ambiguity loud so the extra file can be removed.
    log(f"[autodetect] {label}: WARNING — found {len(candidates)} '*{suffix}' "
        f"files in {DATA_DIR}: {[p.name for p in candidates]}. "
        f"Using '{candidates[0].name}'. Remove the extras to be sure.")
    return candidates[0]


def _resolve_data_file(fixed_name: str, suffix: str, label: str) -> Path:
    """Resolve the path to an official data file.

    Phase 0 now downloads the Program + Abstracts files itself (like the 2026
    fetch script) and saves them under the fixed DOWNLOAD_BUTTONS names. So we
    PREFER the fixed name in data/. If it isn't there yet (download hasn't run,
    failed, or the file was placed manually under the site's own
    name), fall back to autodetecting the lone file of that type in data/.

    Note: at import time the download usually hasn't happened, so this
    typically returns the fixed path (which may not exist yet). main() calls
    download_program_files() before anything reads these, and write_data_json()
    re-resolves via this same function, so by the time the files are read the
    fixed download is present.
    """
    fixed = DATA_DIR / fixed_name
    if fixed.exists():
        return fixed
    return _autodetect_data_file(suffix, label)


INPUT_OFFICIAL_CSV = _resolve_data_file(
    "CLEO2025_Program_Abstracts.csv", ".csv", "official abstract CSV")
INPUT_OFFICIAL_PDF = _resolve_data_file(
    "CLEO2025_Program_Abstracts.pdf", ".pdf", "official abstract PDF")

INPUT_OFFICIAL_CSV = _resolve_data_file(
    "CLEO2025_Program_Abstracts.csv", ".csv", "official abstract CSV")
INPUT_OFFICIAL_PDF = _resolve_data_file(
    "CLEO2025_Program_Abstracts.pdf", ".pdf", "official abstract PDF")



# =============================================================================
# PDF parsing, conference-name + affiliation extraction, name/affiliation
# helpers, type registries, and the JSON builder build_conference_data()
# =============================================================================
def _bootstrap_pdfplumber() -> None:
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        print("[setup] Installing pdfplumber…")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install",
             "--quiet", "pdfplumber>=0.10"])


# -----------------------------------------------------------------------------
# PDF parsing (used by the builder, build_conference_app.py)
# -----------------------------------------------------------------------------
INST_RE        = re.compile(r"^(\d+)\.\s")
ABS_RE         = re.compile(r"^Abstract\s*\([^)]*\):\s*(.*)$", re.DOTALL)
FID_RE         = re.compile(r"^Final\s+ID:\s+(\S+)")
SUPER_TOKEN_RE = re.compile(r"^[\d,]+$")


def cluster_rows(words: list[dict], y_tol: float = 3.0) -> list[dict]:
    """Group `extract_words` output by approximate `top` coordinate. The
    tolerance is chosen so the size-10 letters co-cluster with size-12
    punctuation on the same visual baseline but the superscript numerals
    (≥4.4 pt above the baseline) stay in their own row."""
    if not words:
        return []
    sw = sorted(words, key=lambda w: (w["top"], w["x0"]))
    rows: list[dict] = []
    for w in sw:
        if rows and abs(w["top"] - rows[-1]["top"]) <= y_tol:
            rows[-1]["words"].append(w)
            rows[-1]["top"] = min(rows[-1]["top"], w["top"])
        else:
            rows.append({"top": w["top"], "words": [w]})
    for r in rows:
        r["words"].sort(key=lambda w: w["x0"])
        r["text"]  = " ".join(w["text"] for w in r["words"])
    return rows


def merge_inline_supersub(rows: list[dict]) -> list[dict]:
    """Re-merge orphan superscript/subscript rows back into their host line.

    `cluster_rows` deliberately splits glyphs that sit ≥~4 pt off the baseline
    into their own row, so that the affiliation-marker superscripts (the lone
    '1', '2,3' digits that trail an author's name) land on a separate row from
    the names — the author-pair machinery depends on that. But the SAME vertical
    offset is used by ordinary inline sup/subscripts that belong INSIDE running
    text: the 'th' of '8th', the '2' of 'SO2', exponents like 'cm2'. Those get
    orphaned too, and then leak into whichever field's row-slice happens to
    border them (a stray 'th' on the front of a title, on the end of an
    affiliation, or relocated to the end of an abstract as '^2').

    This helper folds an orphan back into its host purely by geometry: a row
    whose entire x-span sits *inside* a neighbouring (larger) row's x-span and
    whose baseline is 2-9 pt away is an inline sup/sub of that neighbour. It is
    glued at its true x-position (no separating space, since a sup/sub abuts the
    character it modifies). Rows that are NOT horizontally contained — e.g. the
    affiliation-marker digits, which trail past the end of the name row — are
    left untouched, so this must only be applied to the title and
    institution/abstract regions, never across the author band.
    """
    if not rows:
        return rows
    merged_into: dict[int, list[dict]] = {}
    orphan: set[int] = set()
    for ri, r in enumerate(rows):
        rx0 = min(w["x0"] for w in r["words"])
        rx1 = max(w["x1"] for w in r["words"])
        best: int | None = None
        best_dy = 1e9
        for hj, host in enumerate(rows):
            if hj == ri:
                continue
            dy = abs(r["top"] - host["top"])
            if not (2 < dy < 9):
                continue
            hx0 = min(w["x0"] for w in host["words"])
            hx1 = max(w["x1"] for w in host["words"])
            # orphan must sit horizontally inside the host, and the host must be
            # the "main" line (more words than the little sup/sub fragment).
            if (rx0 >= hx0 - 1 and rx1 <= hx1 + 1
                    and len(host["words"]) > len(r["words"])
                    and dy < best_dy):
                best = hj
                best_dy = dy
        if best is not None:
            orphan.add(ri)
            merged_into.setdefault(best, []).extend(r["words"])
    out: list[dict] = []
    for ri, r in enumerate(rows):
        if ri in orphan:
            continue
        words = sorted(r["words"] + merged_into.get(ri, []), key=lambda w: w["x0"])
        parts: list[str] = []
        for k, w in enumerate(words):
            # A sup/subscript abuts the character it modifies with a near-zero
            # x-gap (the '8'|'th', 'SO'|'2' boundaries measure 0.00 pt), whereas
            # ordinary inter-word gaps in this PDF are ~0.6 pt or more. Glue only
            # across a near-zero gap; otherwise insert a normal space so tightly
            # set titles/abstracts don't collapse into one run-on word.
            if k > 0 and (w["x0"] - words[k - 1]["x1"]) <= 0.2:
                parts.append(w["text"])          # abutting sup/sub: glue directly
            else:
                parts.append((" " if k > 0 else "") + w["text"])
        out.append({"top": r["top"], "words": words, "text": "".join(parts).strip()})
    return out


def _join_text(rows: list[dict]) -> str:
    return " ".join(r["text"] for r in rows)


def _collect_underlines(page) -> list[tuple[float, float, float]]:
    """Find horizontal underline segments — pdfplumber surfaces them either
    as 'lines' or as very thin rectangles, so accept both. Returns a list of
    (x0, x1, y) where y is the underline's vertical position."""
    out: list[tuple[float, float, float]] = []
    for ln in page.lines:
        y0 = ln.get("top")
        y1 = ln.get("bottom", y0)
        if y0 is None:
            continue
        if abs((y1 or y0) - y0) > 1.0:
            continue
        out.append((min(ln["x0"], ln["x1"]),
                    max(ln["x0"], ln["x1"]),
                    y0))
    for rc in page.rects:
        if rc.get("height", 99) >= 2.0:
            continue
        out.append((rc["x0"], rc["x1"], rc["top"]))
    return out


def _author_word_ranges(base_row: dict) -> list[tuple[float, float, str]]:
    """From the words of an author baseline row, return one (x0, x1, name)
    tuple per author. An author ends at a word terminating in ';'."""
    out: list[tuple[float, float, str]] = []
    cur: list[dict] = []
    for w in base_row["words"]:
        cur.append(w)
        if w["text"].endswith(";"):
            if cur:
                out.append((min(c["x0"] for c in cur),
                            max(c["x1"] for c in cur),
                            " ".join(c["text"] for c in cur).rstrip(";").strip()))
            cur = []
    if cur:
        out.append((min(c["x0"] for c in cur),
                    max(c["x1"] for c in cur),
                    " ".join(c["text"] for c in cur).rstrip(";").strip()))
    return out


def _find_speaker_indices(underlines: list[tuple[float, float, float]],
                          author_pairs: list[tuple[dict, dict]]
                          ) -> list[int]:
    """Map underlines to GLOBAL author indices (0-based, across all baseline
    rows). An author counts as a speaker if an underline sits 8-15 pt below
    its baseline AND covers >40 % of its x-range."""
    if not underlines or not author_pairs:
        return []
    out: list[int] = []
    seen: set[int] = set()
    global_idx = 0
    for _super_row, base_row in author_pairs:
        ranges = _author_word_ranges(base_row)
        row_top = min(w["top"] for w in base_row["words"])
        for ux0, ux1, uy in underlines:
            if not (row_top + 8 < uy < row_top + 15):
                continue
            for j, (ax0, ax1, _name) in enumerate(ranges):
                overlap = max(0.0, min(ax1, ux1) - max(ax0, ux0))
                if (ax1 - ax0) > 0 and overlap > 0.4 * (ax1 - ax0):
                    idx = global_idx + j
                    if idx not in seen:
                        seen.add(idx)
                        out.append(idx)
        global_idx += len(ranges)
    return out


def _split_authors(words: list[dict]) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    for w in words:
        cur.append(w["text"])
        if w["text"].endswith(";"):
            name = " ".join(cur).rstrip(";").strip()
            if name:
                out.append(name)
            cur = []
    if cur:
        name = " ".join(cur).rstrip(";").strip()
        if name:
            out.append(name)
    return out


def _group_supers(words: list[dict]) -> list[str]:
    """Group superscript-row tokens into affiliation strings, one per
    author. A token ending in ',' continues with the next token."""
    groups: list[str] = []
    cur: str = ""
    for w in sorted(words, key=lambda w: w["x0"]):
        tok = w["text"]
        if cur and cur.endswith(","):
            cur = cur + " " + tok
        else:
            if cur:
                groups.append(cur)
            cur = tok
    if cur:
        groups.append(cur)
    return groups


# Intra-author superscript digits sit ~3 pt apart (e.g. the '2,' and '1' of a
# '2,1' affiliation); digits belonging to *different* authors are separated by
# the width of a name, ~50-70 pt. 14 pt cleanly splits the two regimes.
_SUPER_GAP_PT = 14.0


def _group_supers_x(words: list[dict]) -> list[tuple[float, float, str]]:
    """Like `_group_supers`, but returns (x0, x1, text) per group and clusters
    by x-gap as well as trailing commas. The x-gap rule is what lets us tell a
    single author's space-separated multi-affiliation (rare, e.g. '1 7' would be
    one author) apart from two adjacent single-affiliation authors — though in
    practice CLEO writes multi-affiliations comma-separated, so the gap rule is
    mainly a safety net. The (x0, x1) spans are what `_align_supers_to_names`
    uses to attach each group to the name it trails."""
    ws = sorted(words, key=lambda w: w["x0"])
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in ws:
        if cur:
            prev = cur[-1]
            if prev["text"].endswith(",") or (w["x0"] - prev["x1"]) < _SUPER_GAP_PT:
                cur.append(w)
                continue
            groups.append(cur)
        cur = [w]
    if cur:
        groups.append(cur)
    out: list[tuple[float, float, str]] = []
    for g in groups:
        txt = re.sub(r"\s*,\s*", ",", " ".join(x["text"] for x in g))
        out.append((min(x["x0"] for x in g),
                    max(x["x1"] for x in g),
                    txt))
    return out


def _align_supers_to_names(
    author_pairs: list[tuple[dict, dict]]
) -> list[tuple[str, str]]:
    """Map superscript groups to author names by page GEOMETRY rather than by
    sequential index.

    On the abstract pages the affiliation superscripts TRAIL the name they
    modify (they sit just to the right of the name's terminating ';'). When the
    author block wraps across several baseline rows, a name can finish on one
    row while its trailing superscript wraps to the start of the next row's
    superscript band — and a name can itself be split across rows. Index-based
    zipping (group k -> name k) breaks at every such wrap, shifting affiliations
    onto the wrong authors. Instead we flatten every name and every superscript
    group into reading order (row, then x) and assign each superscript to the
    most recent name whose end precedes it. A name with no trailing superscript
    before the next name simply gets '' (correct for wrapped name fragments,
    which `_merge_fragmented_pairs` later folds into their continuation)."""
    names: list[tuple[int, float, float, str]] = []   # (row, x0, x1, name)
    supers: list[tuple[int, float, str]] = []         # (row, x0, text)
    for ridx, (super_row, base_row) in enumerate(author_pairs):
        for x0, x1, nm in _author_word_ranges(base_row):
            # `_author_word_ranges` emits an empty-name segment for each lone
            # ';' token (the wrapped tail of the previous author's name); skip
            # those so superscripts attach to real names only.
            if nm:
                names.append((ridx, x0, x1, nm))
        for x0, _x1, tx in _group_supers_x(super_row["words"]):
            supers.append((ridx, x0, tx))

    # names are already in reading order (rows top-to-bottom, words left-to-right)
    assigned: list[list[str]] = [[] for _ in names]
    for srow, sx0, stext in supers:
        best: int | None = None
        for ni, (nrow, nx0, _nx1, _nm) in enumerate(names):
            if nrow < srow or (nrow == srow and nx0 <= sx0):
                best = ni      # keep the latest name that still precedes it
            else:
                break          # reading order — nothing further can precede it
        if best is not None:
            assigned[best].append(stext)
    # An author's affiliations can arrive as several groups — e.g. when a
    # trailing-comma group ('3,') and its continuation ('1') sit on different
    # baseline rows because the author block wrapped. Join, then collapse any
    # doubled/stray commas so '3,' + '1' -> '3,1' (not '3,,1').
    out: list[tuple[str, str]] = []
    for i in range(len(names)):
        aff = ",".join(assigned[i])
        aff = re.sub(r",\s*,+", ",", aff).strip(",")
        out.append((names[i][3], aff))
    return out


def _is_initials_only(name: str) -> bool:
    """Detect a name that's nothing but initials, e.g. 'B.' or 'B. J.'.
    Such 'authors' show up when pdfplumber splits a multi-initial name
    like 'B. J. Eggleton' into two adjacent words (one with a trailing
    semicolon, one without) — the row parser treats the semicolon as the
    end of an author and emits two pairs instead of one."""
    if not name:
        return False
    tokens = name.split()
    if not tokens:
        return False
    return all(re.fullmatch(r"[A-Z]\.?", t) for t in tokens)


# Surname particles that signal a name has been split mid-surname across a
# baseline-row break (e.g. 'D. Van' + 'Thourhout', 'J. De' + 'Witte'). A
# fragment ending in one of these — with no affiliation of its own — is an
# incomplete name to be glued onto its continuation, just like an initials-only
# fragment. Lower-cased compare so 'van'/'Van'/'VAN' all match.
_NAME_PARTICLES = {
    "van", "von", "de", "del", "della", "der", "den", "di", "da", "dos",
    "das", "du", "la", "le", "el", "al", "bin", "ibn", "ter", "ten", "te",
    "vander", "vande", "op",
}


def _is_incomplete_name_fragment(name: str) -> bool:
    """A name fragment that clearly continues on the next pair: either nothing
    but initials ('C.', 'B. J.') or ending in a surname particle ('D. Van')."""
    if _is_initials_only(name):
        return True
    tokens = name.split()
    return bool(tokens) and tokens[-1].lower() in _NAME_PARTICLES


def _merge_fragmented_pairs(pairs: list[tuple[str, str]]
                            ) -> list[tuple[str, str]]:
    """Walk pairs left-to-right; when an entry is an incomplete name fragment
    (initials-only, or ending in a surname particle like 'Van'/'De') and has no
    affiliation, glue it onto the next entry. Repeats until non-fragment so
    'C.' + 'J.' + 'Eggleton=1' collapse to 'C. J. Eggleton=1' and
    'D. Van' + 'Thourhout=1' collapses to 'D. Van Thourhout=1'."""
    if not pairs:
        return pairs
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(pairs):
        name, aff = pairs[i]
        while (i + 1 < len(pairs)
               and _is_incomplete_name_fragment(name)
               and not aff):
            next_name, next_aff = pairs[i + 1]
            name = f"{name} {next_name}"
            aff  = next_aff
            i += 1
        out.append((name, aff))
        i += 1
    return out


def parse_pdf_page(page) -> dict | None:
    words = page.extract_words(extra_attrs=["size", "top"])
    rows  = cluster_rows(words)
    if not rows:
        return None

    m = FID_RE.match(rows[0]["text"])
    if not m:
        return None
    final_id = m.group(1)

    abs_idx = next(
        (i for i, r in enumerate(rows) if r["text"].startswith("Abstract (35")),
        len(rows),
    )
    inst_idxs = [
        i for i in range(1, abs_idx) if INST_RE.match(rows[i]["text"])
    ]
    first_inst = inst_idxs[0] if inst_idxs else abs_idx

    # Walk upward from (first_inst - 1) collecting (super-row, baseline-row)
    # pairs. The super row sits ~4-7 pt above its baseline and contains only
    # digits / commas.
    author_pairs: list[tuple[dict, dict]] = []
    i = first_inst - 1
    while i > 0:
        if i - 1 <= 0:
            break
        super_row = rows[i - 1]
        base_row  = rows[i]
        gap = base_row["top"] - super_row["top"]
        if not (3 < gap < 8):
            break
        if not all(SUPER_TOKEN_RE.match(w["text"]) for w in super_row["words"]):
            break
        author_pairs.append((super_row, base_row))
        i -= 2
    author_pairs.reverse()

    # Fold any inline sup/subscript orphan rows (e.g. the 'th' of '8th') back
    # into the title line before joining, so they don't leak onto the front of
    # the title as a stray fragment.
    title_rows = merge_inline_supersub(rows[1 : i + 1])
    title = _join_text(title_rows).strip()

    # Affiliation superscripts TRAIL the name they belong to, so align them to
    # names by page geometry across the whole author block rather than zipping
    # group-k to name-k row by row (which shifts affiliations by one wherever a
    # name or its superscript wraps across baseline rows).
    pairs: list[tuple[str, str]] = _align_supers_to_names(author_pairs)

    # pdfplumber occasionally splits multi-initial names ('B. J. Eggleton')
    # into separate row tokens because of a stray semicolon between them.
    # Stitch those fragments back together before they cause an off-by-one
    # vs. the scraped author list.
    pairs = _merge_fragmented_pairs(pairs)

    # The institution lines and the abstract body both carry inline
    # sup/subscripts (e.g. the 'th' of '8th', the '2' of 'SO2', exponents like
    # 'cm2'). cluster_rows orphans those onto their own rows, which then border
    # — and leak into — the wrong field: a stray 'th' lands on the end of an
    # affiliation (breaking the affiliation match) or gets relocated to the end
    # of the abstract as '^2'. Merge the whole post-author region by geometry
    # first, then recompute the Abstract boundary, so each orphan attaches to
    # its true host regardless of which side of the boundary it sits on.
    post_rows = merge_inline_supersub(rows[first_inst:])
    post_abs_idx = next(
        (k for k, r in enumerate(post_rows)
         if r["text"].startswith("Abstract (35")),
        len(post_rows),
    )

    institutions: list[str] = []
    cur: list[str] = []
    for r in post_rows[:post_abs_idx]:
        t = r["text"]
        if INST_RE.match(t):
            if cur:
                institutions.append(" ".join(cur).strip())
            cur = [t]
        else:
            cur.append(t)
    if cur:
        institutions.append(" ".join(cur).strip())

    abstract = ""
    if post_abs_idx < len(post_rows):
        full = " ".join(r["text"] for r in post_rows[post_abs_idx:]).strip()
        m2 = ABS_RE.match(full)
        abstract = m2.group(1).strip() if m2 else full

    speaker_idxs = _find_speaker_indices(_collect_underlines(page),
                                         author_pairs)
    speakers = [pairs[i][0] for i in speaker_idxs if 0 <= i < len(pairs)]

    return {
        "final_id":     final_id,
        "title":        title,
        "pairs":        pairs,         # list[(name, affil_str)]
        "institutions": institutions,
        "abstract":     abstract,
        "speakers":     speakers,
    }


def parse_pdf(path: Path) -> dict[str, dict]:
    """Return {final_id: parsed_entry} for every abstract page in the PDF."""
    import pdfplumber
    result: dict[str, dict] = {}
    print(f"[pdf] Opening {path.name}…")
    with pdfplumber.open(str(path)) as pdf:
        n = len(pdf.pages)
        print(f"[pdf] {n} pages. Parsing…")
        for i, page in enumerate(pdf.pages, 1):
            try:
                entry = parse_pdf_page(page)
            except Exception as e:
                print(f"  page {i:>4}: error {e!s}")
                continue
            if entry:
                result[entry["final_id"]] = entry
    return result


# -----------------------------------------------------------------------------
# Affiliation-source extraction (consumed by build_affiliation_map.py)
# -----------------------------------------------------------------------------
PDF_AFFIL_START = re.compile(r'^\d{1,2}\.\s+(\S.*)$')


def _pdf_to_text(pdf_path: Path) -> str:
    """Extract the full PDF as plain text, page by page, with blank lines
    between pages.

    Previously this shelled out to the `pdftotext` binary (Poppler), which
    is standard on Linux but not on Windows without a separate Poppler
    install, whereas pdfplumber is already a dependency of
    build_conference_app.py and works the same way on every platform.
    """
    _bootstrap_pdfplumber()
    import pdfplumber
    parts: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ''
            parts.append(t)
    # Blank line between pages so the affiliation parser's blank-line
    # delimiter still terminates a buffer at page boundaries.
    return '\n\n'.join(parts)


def extract_pdf_affiliations(pdf_path: Path) -> set[str]:
    """Pull every full-address affiliation line out of the abstract book PDF.

    Lines look like ``1. Department of X, University Y, City, ST, Country.``
    A new affiliation starts with ``<N>. ``. Continuation lines (wrap) have
    no such prefix and we join them onto the current buffer until the buffer
    ends with a period.
    """
    text = _pdf_to_text(pdf_path)
    out: set[str] = set()
    buf: str | None = None
    for raw in text.split('\n'):
        s = raw.rstrip()
        if not s:
            if buf is not None:
                out.add(buf)
                buf = None
            continue
        m = PDF_AFFIL_START.match(s)
        if m:
            if buf is not None:
                out.add(buf)
            buf = m.group(1)
        elif buf is not None:
            if buf.endswith('.'):
                out.add(buf)
                buf = None
            else:
                buf = buf + ' ' + s
    if buf is not None:
        out.add(buf)
    # Keep only address-like lines; strip the trailing period (existing map omits it).
    keep = {a[:-1] for a in out if a.endswith('.') and a.count(',') >= 2}
    # Repair a hyphen-then-space wrap artifact like "Koganei- shi" -> "Koganei-shi".
    keep = {re.sub(r'(\w)- (\w)', r'\1-\2', a) for a in keep}
    return keep


# A conference-name line on the abstract-book cover looks like "CLEO 2025"
# (and variants: "CLEO: 2025", "CLEO 2025 — Conference on Lasers and
# Electro-Optics"). It always carries a 4-digit year and is a short title
# line, never a paragraph. The date line ("May 4 - 9, 2025") and the header
# ("Program Schedule and Abstract Book") don't match this shape.
_CONF_NAME_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9&'.:()\u2013\u2014\- ]*"
    r"\b(?:19|20)\d{2}\b"
    r"[A-Za-z0-9&'.:()\u2013\u2014\- ]*$")


def extract_conference_name(pdf_path: Path) -> str:
    """Pull the conference name (e.g. 'CLEO 2025') off the abstract book's
    cover page.

    The cover's first lines are typically:
        Program Schedule and Abstract Book
        CLEO 2025
        May 4 - 9, 2025
    so we prefer the line immediately AFTER the 'Abstract Book' / 'Program
    Schedule' header, falling back to the first short, year-bearing line near
    the top. Returns '' if nothing matches (the downstream build script then
    falls back to its own default), so this never raises on an odd cover.
    """
    import pdfplumber
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            if not pdf.pages:
                return ""
            text = pdf.pages[0].extract_text() or ""
    except Exception as e:
        log(f"[json]     WARNING: couldn't read PDF cover for conference "
            f"name: {e}")
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    def _ok(s: str) -> bool:
        return bool(s) and len(s) <= 60 and _CONF_NAME_RE.match(s) is not None

    # 1) Line right after the cover header.
    for i in range(len(lines) - 1):
        if re.search(r"abstract book|program schedule", lines[i], re.I):
            if _ok(lines[i + 1]):
                return lines[i + 1]
    # 2) First short, year-bearing line near the top of the cover.
    for ln in lines[:8]:
        if _ok(ln):
            return ln
    return ""


# Note: there are no separate extract_csv_presider_affiliations() /
# extract_csv_institutions() helpers here; build_conference_data()
# (above) assembles the affiliation_sources list of the JSON itself.


# -----------------------------------------------------------------------------
# JSON emission — one file for both downstream scripts
# -----------------------------------------------------------------------------
# =============================================================================
# CONFERENCE-DATA PROCESSING
# -----------------------------------------------------------------------------
# Everything from here to build_conference_data() turns this processor's raw
# inputs (the scraped planner CSV rows, the official Program+Abstracts CSV
# rows, and the parsed abstract-book PDF) into the clean, FINAL,
# conference-agnostic data dict the builder consumes. The processor emits
# fully-processed data so the builder (build_conference_app.py) only does
# affiliation shortening + HTML templating.
#
# Two things are deferred to the builder. (1) Affiliation SHORTENING (so
# build_affiliation_map.py stays as-is, run by the builder): author
# affiliations and institutions are emitted as RAW strings, and presider
# affiliations are left as scraped; the builder canonicalizes them and does
# the institution de-dup + presider-affiliation backfill that depend on it.
# (2) Abstract inline-LaTeX -> Unicode rendering: abstracts are emitted RAW
# (still carrying any '$...$' math) and the builder converts them.
# =============================================================================


# =============================================================================
# Name normalization + matching helpers
# =============================================================================
def _title_key(s: str) -> str:
    """Normalise a title for cross-source matching."""
    return re.sub(r"\s+", " ", (s or "")).strip(" .;:").lower()


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip(" *.").lower()


def _name_key(name: str) -> str:
    """Reduce a name to 'first-initial last-surname' for matching across the
    initialed (PDF) and full-name (scrape) forms."""
    if not name:
        return ""
    s = re.sub(r"[*]", "", name).strip().rstrip(".")
    tokens = [t for t in re.split(r"\s+", s) if t]
    if not tokens:
        return ""
    first = tokens[0].rstrip(".")
    first_init = first[0].lower() if first else ""
    surname = tokens[-1].rstrip(".").lower() if len(tokens) > 1 else first.lower()
    return f"{first_init} {surname}" if first_init else surname


def _normalize_name_case(name: str) -> str:
    """Normalize the capitalization of a personal name (handles SHOUTED
    surnames like 'Jo SMITH' and fully-lowercased 'jane doe')."""
    if not name:
        return name
    tokens = name.split()
    if not tokens:
        return name
    any_upper = any(any(ch.isupper() for ch in t) for t in tokens)

    out_tokens: list[str] = []
    for tok in tokens:
        if len(tok) <= 1:
            out_tokens.append(tok)
            continue
        lead, core, trail = "", tok, ""
        while core and not (core[0].isalpha() or core[0].isdigit()):
            lead += core[0]; core = core[1:]
        while core and not (core[-1].isalpha() or core[-1].isdigit()):
            trail = core[-1] + trail; core = core[:-1]
        if not core:
            out_tokens.append(tok)
            continue
        letters = [ch for ch in core if ch.isalpha()]
        if not letters:
            out_tokens.append(tok)
            continue
        is_upper = all(ch.isupper() for ch in letters)
        is_lower = all(ch.islower() for ch in letters)
        if not (is_upper or is_lower):
            out_tokens.append(tok)
            continue
        if is_lower and any_upper:
            out_tokens.append(tok)
            continue
        parts = re.split(r"(['\u2019\-])", core)
        cased = "".join(
            (p.capitalize() if p and p[0].isalpha() else p)
            for p in parts
        )
        out_tokens.append(lead + cased + trail)
    return " ".join(out_tokens)


def _normalize_author_list(s: str, sep: str = ";") -> str:
    if not s:
        return s
    return sep.join(_normalize_name_case(a.strip()) for a in s.split(sep))


_AUTHOR_SPLIT_RE = re.compile(r"\s*;\s*")


def split_authors(s: str) -> list[str]:
    if not s:
        return []
    return [a.rstrip("*").strip() for a in _AUTHOR_SPLIT_RE.split(s) if a.strip()]


def _n_full_tokens(s: str) -> int:
    s = re.sub(r"[*]", "", s or "")
    return sum(1 for t in re.split(r"\s+", s.strip())
               if len(t.rstrip(",.;:-")) > 2 and not t.endswith("."))


def _looks_fuller_tokens(full: str, initialed: str) -> bool:
    return _n_full_tokens(full) > _n_full_tokens(initialed)


def _looks_fuller_len(scraped: str, pdf: str) -> bool:
    sc = (scraped or "").replace("*", "").strip()
    pf = (pdf or "").replace("*", "").strip()
    return len(sc) >= len(pf) + 3


# A glued Final-ID prefix looks like "4290312. Some Title" or "AM1C.1. Title".
_ID_PREFIX_NUM = re.compile(r"^\s*(\d{3,})\.\s+(.*)$", re.S)
_ID_PREFIX_CODE = re.compile(r"^\s*([A-Z][A-Z0-9]*\.\d+)\.\s+(.*)$", re.S)


def _strip_talk_id_prefix(title: str) -> tuple[str, str]:
    """Detect a glued Final-ID prefix on a scraped talk title.
    Returns (recovered_id, clean_title)."""
    if not title:
        return "", title
    m = _ID_PREFIX_CODE.match(title)
    if m:
        return m.group(1), m.group(2).strip()
    m = _ID_PREFIX_NUM.match(title)
    if m:
        return m.group(1), m.group(2).strip()
    return "", title


# =============================================================================
# Date/time + presider parsing
# =============================================================================
def parse_dt(date_str: str, time_str: str) -> str | None:
    if not date_str or not time_str:
        return None
    for fmt in ("%d-%b-%Y %I:%M %p", "%d-%b-%Y %H:%M"):
        try:
            return datetime.strptime(f"{date_str} {time_str}", fmt).isoformat()
        except ValueError:
            continue
    return None


def parse_presider(hosts: str) -> tuple[str, str]:
    """'Presider: Jane Doe, Some University' -> ('Jane Doe', 'Some University').
    Fallback path used only when the planner scrape doesn't have presider info
    for the session."""
    if not hosts:
        return "", ""
    m = re.match(r"Presider\(?s?\)?:\s*(.+)", hosts.strip())
    if not m:
        return "", ""
    rest = m.group(1).strip().rstrip(".")
    parts = rest.rsplit(",", 1)
    if len(parts) == 2:
        return _normalize_name_case(parts[0].strip()), parts[1].strip()
    return _normalize_name_case(rest), ""


def _affiliation_is_usable(aff: str) -> bool:
    """Is a presider affiliation string actually informative?"""
    if not aff:
        return False
    for piece in aff.split(";"):
        core = re.sub(r"[\s.\-*]+", "", piece)
        if len(core) >= 2:
            return True
    return False


# =============================================================================
# Type registries: which session/talk types exist, their labels + colors.
# -----------------------------------------------------------------------------
# This replaces the CLEO-specific keyword heuristics + JS label maps that used
# to live in build_conference_app.py (pick_session_color / pick_talk_color /
# TYPE_LABELS_*). The classification logic stays keyword-based (so behaviour is
# identical), but now it runs HERE and the result — a stable color token, which
# IS the type id the app filters on — is baked into every record, alongside a
# registry that gives each color its human label and display order.
#
# The app keys everything off the `color` field (it groups + filters by color
# and looks up a human label per color). So "type id" == color token here.
# =============================================================================

# -----------------------------------------------------------------------------
# Color palette: the actual RGB values for every color token used by the
# registries below. Each entry is (fg, bg_light, bg_dark):
#   - fg       : solid edge / swatch color (used in both light & dark mode)
#   - bg_light : bubble surface in light mode
#   - bg_dark  : bubble surface in dark mode
# The processor is the single source of truth for these: it ships the RGB for
# every token it emits, so the builder can synthesize the matching CSS and no
# token ever falls back to a gray default just because the builder didn't
# already know about it (e.g. "sky"). Add a token here and reference it in a
# registry and it Just Works in the app.
COLOR_PALETTE: dict[str, dict] = {
    "blue":    {"fg": "#2563eb", "bg_light": "#e8efff", "bg_dark": "#1a233d"},
    "sky":     {"fg": "#0284c7", "bg_light": "#e0f2fe", "bg_dark": "#0c2a3d"},
    "indigo":  {"fg": "#4f46e5", "bg_light": "#e6e4ff", "bg_dark": "#1d1a3d"},
    "violet":  {"fg": "#7c3aed", "bg_light": "#efe9ff", "bg_dark": "#271f3e"},
    "amber":   {"fg": "#c2750a", "bg_light": "#fdf0d6", "bg_dark": "#36280f"},
    "fuchsia": {"fg": "#c026d3", "bg_light": "#fae8ff", "bg_dark": "#3a0f3f"},
    "orange":  {"fg": "#ea580c", "bg_light": "#ffedd5", "bg_dark": "#3b1d0a"},
    "rose":    {"fg": "#e11d48", "bg_light": "#ffe1e8", "bg_dark": "#38161f"},
    "teal":    {"fg": "#0d9488", "bg_light": "#d6f3ef", "bg_dark": "#102b27"},
    "emerald": {"fg": "#059669", "bg_light": "#def7ec", "bg_dark": "#133024"},
    "pink":    {"fg": "#db2777", "bg_light": "#ffe4f1", "bg_dark": "#371525"},
}


def _with_colors(entries: list[dict]) -> list[dict]:
    """Attach the (fg, bg_light, bg_dark) RGB triple to each registry entry by
    looking its token up in COLOR_PALETTE. Entries whose token isn't in the
    palette are passed through unchanged (the builder will gray-fallback them,
    same as before), so this is safe even for ad-hoc tokens."""
    out = []
    for e in entries:
        pal = COLOR_PALETTE.get(e["id"])
        out.append({**e, **pal} if pal else dict(e))
    return out


# Standard session/talk type taxonomy. The seven shared types; a conference only
# surfaces the ones its program actually uses (the app hides count-0 types).
SESSION_TYPE_REGISTRY: list[dict] = _with_colors([
    {"id": "blue",    "label": "Technical"},
    {"id": "orange",  "label": "Plenary"},
    {"id": "fuchsia", "label": "Tutorial"},
    {"id": "teal",    "label": "Poster"},
    {"id": "rose",    "label": "Event"},
])

TALK_TYPE_REGISTRY: list[dict] = _with_colors([
    {"id": "orange",  "label": "Plenary"},
    {"id": "indigo",  "label": "Invited"},
    {"id": "sky",     "label": "Contributed"},
    {"id": "fuchsia", "label": "Tutorial"},
    {"id": "teal",    "label": "Poster"},
    {"id": "rose",    "label": "Event"},
])


def classify_session_color(session_type: str, session_title: str = "") -> str:
    """Map a session to a color token (== the session's type id):

      - Plenary   (orange):  the session title contains "plenary"
      - Poster    (teal):    the session title contains "poster"
      - Tutorial  (fuchsia): short-course sessions (short courses fold into
                             Tutorial), matching the short-course talk color
      - Technical (blue):    the main tracks (Applications & Technology,
                             Fundamental Science, Science & Innovations),
                             Symposium-typed sessions, AND postdeadline sessions
                             (postdeadline folds into Technical)
      - Event     (rose):    everything else (ceremonies, socials, etc.)

    The Plenary/Poster buckets are detected from the TITLE and take precedence,
    since such a session may also carry a track type."""
    s = (session_type or "").strip().lower()
    title = (session_title or "").strip().lower()
    tokens = s.split()
    if "plenary" in title:                          return "orange"
    if "poster" in title:                           return "teal"
    if "short course" in title or "short course" in s: return "fuchsia"
    if "postdeadline" in title:                     return "blue"
    if "symposi" in s:                              return "blue"
    if "a&t" in s or "fs" in tokens or "s&i" in s:  return "blue"
    if "oral" in tokens:                            return "blue"
    return "rose"


def classify_talk_color(talk_title: str, session_title: str,
                        session_type: str, status: str = "") -> str:
    """Map a talk to a color token (== the talk's type id).

    `status` carries any scraped/official status tags (e.g. "Invited",
    "Tutorial"). CLEO 2025 marks Invited talks by emboldening the title in the
    planner DOM rather than with an "[Invited Talk]" text marker, so the
    processor records "Invited" in the status; we honor that here in addition to
    the legacy title-text check.

      - Plenary     (orange):  talk in a Plenary session
      - Tutorial    (fuchsia): a Tutorial-marked talk (title or status), or a
                               short course (short courses fold into Tutorial)
      - Invited     (indigo):  an Invited-marked talk (title or status), OR any
                               talk in a Symposium session (all symposium talks
                               are treated as Invited)
      - Poster      (teal):    talk in a Poster session
      - Contributed (sky):     everything else, INCLUDING postdeadline talks
                               (postdeadline folds into Contributed)
    """
    tt = (talk_title or "").lower()
    st = (session_title or "").lower()
    stype = (session_type or "").lower()
    status_l = (status or "").lower()
    if "plenary" in st or "plenary" in stype:           return "orange"
    if "tutorial" in tt or "tutorial" in status_l:      return "fuchsia"
    if "short course" in tt or "short course" in stype: return "fuchsia"
    if "invited" in tt or "invited" in status_l:        return "indigo"
    if "symposi" in stype:                              return "indigo"
    if "poster" in st or "poster" in stype:             return "teal"
    return "sky"


# =============================================================================
# Scraped-CSV ingestion
# =============================================================================
def read_scraped_csv(rows: list[dict]
                     ) -> tuple[dict[str, dict], dict[str, dict],
                                dict[tuple[str, str], dict]]:
    """Return (talks_by_id, sessions_by_code, talks_by_title).

    talks_by_id[final_id] = {"authors": [...], "speaker": str, "status": str}
    sessions_by_code[code] = {"presider_names": str,
                              "presider_affiliations": str}
    talks_by_title[(session_title_key, talk_title_key)] = same shape as
        talks_by_id (fallback for rows with no Final ID, e.g. plenaries).
    """
    talks: dict[str, dict] = {}
    sessions: dict[str, dict] = {}
    talks_by_title: dict[tuple[str, str], dict] = {}
    if not rows:
        print("[scrape] no scraped rows; skipping enrichment.")
        return talks, sessions, talks_by_title

    prefix_hits = 0
    for r in rows:
        row_type = (r.get("row_type") or "").strip()
        if row_type == "session":
            code = (r.get("session_code") or "").strip()
            if not code:
                continue
            names = (r.get("session_presider_names") or "").strip()
            affs = (r.get("session_presider_affiliations") or "").strip()
            names = _normalize_author_list(names, sep=";")
            if names or affs:
                sessions[code] = {
                    "presider_names":        names,
                    "presider_affiliations": affs,
                }
        elif row_type == "talk":
            tid = (r.get("talk_number") or "").strip()
            raw_auth = (r.get("talk_authors") or "").strip()
            speaker = (r.get("talk_speaker") or "").strip()
            status = (r.get("talk_status") or "").strip()
            speaker = _normalize_name_case(speaker)
            authors = [_normalize_name_case(a.strip())
                       for a in raw_auth.split(";") if a.strip()]
            rec = {"authors": authors, "speaker": speaker, "status": status}
            ttitle_raw = (r.get("talk_title") or "").strip()
            recovered_id, ttitle_clean = _strip_talk_id_prefix(ttitle_raw)
            if recovered_id:
                prefix_hits += 1
                if not tid:
                    tid = recovered_id
            if tid:
                talks[tid] = rec
            stitle = (r.get("session_title") or "").strip()
            if stitle and ttitle_clean:
                talks_by_title[(_title_key(stitle),
                                _title_key(ttitle_clean))] = rec
    if prefix_hits:
        print(f"[scrape] recovered Final-ID prefix from {prefix_hits} "
              "scraped talk title(s)")
    print(f"[scrape] indexed {len(talks)} talks (by id) + "
          f"{len(talks_by_title)} (by title) + {len(sessions)} sessions")
    return talks, sessions, talks_by_title


# =============================================================================
# Source-agnostic emission helpers.
#
# The processor's enrichment naturally yields, per talk, an ordered author list
# with each author's institution NUMBERS plus a numbered institution list with
# both a detailed display form and (often) a cleaner variant. These helpers
# turn that into the SOURCE-AGNOSTIC JSON shape the builder consumes — no field
# names off the page mention csv/pdf/etc. A different conference's processor just
# has to produce the same shape.
# =============================================================================
def _structured_authors(affil_map: str) -> list[dict]:
    """'Name=1,2; Name2=3; Name3' -> [{name, insts:[int,...]}, ...].
    A bare name (no '=') yields insts:[]. Empty index tokens are dropped (so a
    stray trailing comma like 'Name=3,' becomes insts:[3])."""
    authors: list[dict] = []
    for pair in (affil_map or "").split(";"):
        pair = pair.strip()
        if not pair:
            continue
        eqi = pair.find("=")
        if eqi < 0:
            authors.append({"name": pair, "insts": []})
            continue
        name = pair[:eqi].strip()
        insts = [int(tok) for tok in pair[eqi + 1:].split(",")
                 if tok.strip().isdigit()]
        authors.append({"name": name, "insts": insts})
    return authors


def _structured_institutions(insts_detailed: list[str],
                             insts_clean: str) -> list[dict]:
    """Build the unified institution list. `insts_detailed` is the ordered list
    of detailed institution bodies (display forms). `insts_clean` is an optional
    ';'-joined list of cleaner institution-level variants, positionally parallel
    to the detailed list. Each institution carries {n, name (detailed),
    alt_names (cleaner variants)}."""
    clean_bodies = [c.strip() for c in (insts_clean or "").split(";")
                    if c.strip()]
    out: list[dict] = []
    for i, body in enumerate(insts_detailed):
        body = (body or "").strip()
        # Detailed bodies may arrive with a leading "N. " number prefix (e.g.
        # from a numbered PDF list); use that EXPLICIT number as `n` (author
        # `insts` reference it) and strip it from the displayed name. Fall back
        # to 1-based position when there's no explicit prefix.
        m = re.match(r"^(\d+)\.\s*(.+)$", body)
        if m:
            n = int(m.group(1))
            body = m.group(2).strip()
        else:
            n = i + 1
        if not body:
            continue
        alt: list[str] = []
        if i < len(clean_bodies) and clean_bodies[i] and clean_bodies[i] != body:
            alt.append(clean_bodies[i])
        out.append({"n": n, "name": body, "alt_names": alt})
    return out


def _author_aliases(structured_authors: list[dict], *loose: str) -> list[str]:
    """Extra loose author-name forms (e.g. initials) kept only as search fodder,
    deduped against the structured author names and the presenter '*' dropped."""
    have = {re.sub(r"\s+", " ", a["name"]).strip().lower()
            for a in structured_authors}
    aliases: list[str] = []
    seen = set(have)
    for blob in loose:
        for nm in (blob or "").split(";"):
            nm = nm.replace("*", "").strip()
            if not nm:
                continue
            k = re.sub(r"\s+", " ", nm).strip().lower()
            if k not in seen:
                seen.add(k)
                aliases.append(nm)
    return aliases


# =============================================================================
# build_conference_data — the official-CSV walk, with PDF + scrape enrichment.
#
# This builds the conference data dict from the official-CSV walk, with two
# notable properties:
#   (1) it produces clean, FINAL records (everything resolved except the
#       affiliation SHORTENING, which the builder still does);
#   (2) authors and institutions are emitted in a SOURCE-AGNOSTIC structured
#       shape (see the emission helpers above and the talks.append below), so
#       the builder can run the affiliation map without knowing the source.
# Author upgrading, speaker upgrading, presider scraping, synthesized codes,
# and presider backfilling all happen here now.
# =============================================================================
def build_conference_data(conference_name: str,
                          scraped_rows: list[dict],
                          official_rows: list[dict],
                          pdf_entries: dict[str, dict],
                          pdf_affiliation_lines: list[str]) -> dict:
    """Produce the final, conference-agnostic data dict (JSON-ready)."""
    scraped_talks, scraped_sessions, scraped_by_title = read_scraped_csv(
        scraped_rows)

    sessions: dict[str, dict] = {}
    talks: list[dict] = []

    n_pdf_matched = 0
    n_authors_upgraded = 0
    n_speaker_upgraded = 0
    n_presider_scraped = 0
    n_presider_csv = 0
    n_synth_sessions = 0
    n_synth_talks = 0
    n_bold_invited_reclassified = 0

    synth_session_codes: dict[tuple[str, str, str], str] = {}
    _used_synth_codes: set[str] = set()
    plenary_counter = [0]
    other_counter = [0]

    # Target length for synthesized codes = median length (including digits)
    # of the real session abbreviations, so a synthesized code blends in with
    # codes like "AM1C" rather than being conspicuously shorter or longer.
    _real_code_lens = sorted(
        len(a) for r in official_rows
        if (a := (r.get("Session or Event Abbreviation") or "").strip()))
    if _real_code_lens:
        _m = len(_real_code_lens)
        _synth_target_len = (_real_code_lens[_m // 2] if _m % 2
                             else (_real_code_lens[_m // 2 - 1]
                                   + _real_code_lens[_m // 2]) // 2)
    else:
        _synth_target_len = 4   # no real codes to measure; sane default

    # Skip filler words when building an acronym so "The Future of Optics"
    # gives FO, not TFOO. Single-character words are dropped automatically.
    _SLUG_STOPWORDS = {"the", "of", "and", "a", "an", "in", "on", "for",
                       "to", "with", "at"}

    def _slugify_for_code(title: str) -> str:
        words = [w for w in re.findall(r"[A-Za-z0-9]+", title or "")
                 if len(w) > 1 and w.lower() not in _SLUG_STOPWORDS]
        if not words:
            return ""
        # Build up to _synth_target_len characters by taking letters from the
        # title in round-robin passes: first every word's 1st letter (a plain
        # acronym, e.g. "Quantum Information" -> "QI"), then 2nd letters, etc.
        # This pads short acronyms with more title letters ("QI" -> "QUIN")
        # instead of leaving them too short, while long titles still cap at
        # the target. Stops as soon as a pass adds nothing (all words spent).
        out: list[str] = []
        pos = 0
        while len(out) < _synth_target_len:
            added = False
            for w in words:
                if pos < len(w):
                    out.append(w[pos].upper())
                    added = True
                    if len(out) >= _synth_target_len:
                        break
            if not added:
                break
            pos += 1
        return "".join(out)

    def _synth_code(row: dict) -> str:
        key = ((row.get("Session or Event Title") or "").strip(),
               (row.get("Session or Event Date") or "").strip(),
               (row.get("Session or Event Start Time") or "").strip())
        if key in synth_session_codes:
            return synth_session_codes[key]
        stype = (row.get("Session or Event Type") or "").strip().lower()
        if "plenary" in stype:
            plenary_counter[0] += 1
            code = f"PLEN{plenary_counter[0]}"
        else:
            slug = _slugify_for_code(row.get("Session or Event Title", ""))
            if slug:
                # Short acronyms collide easily; ensure the final code is
                # unique among synthesized codes by suffixing a counter.
                code = slug
                if code in _used_synth_codes:
                    n = 2
                    while f"{slug}{n}" in _used_synth_codes:
                        n += 1
                    code = f"{slug}{n}"
            else:
                other_counter[0] += 1
                code = f"EVENT{other_counter[0]}"
        _used_synth_codes.add(code)
        synth_session_codes[key] = code
        return code

    for r in official_rows:
        sa = (r.get("Session or Event Abbreviation") or "").strip()
        if not sa:
            sa = _synth_code(r)
            synthesized_session = True
        else:
            synthesized_session = False

        if sa not in sessions:
            scr_sess = scraped_sessions.get(sa)
            if scr_sess and (scr_sess["presider_names"]
                             or scr_sess["presider_affiliations"]):
                presider = scr_sess["presider_names"]
                presider_aff = scr_sess["presider_affiliations"]
                n_presider_scraped += 1
            else:
                presider, presider_aff = parse_presider(
                    r.get("Session or Event Hosts", ""))
                if presider:
                    n_presider_csv += 1

            stype = (r.get("Session or Event Type") or "").strip()
            sessions[sa] = {
                "id":              sa,
                "title":           (r.get("Session or Event Title") or "").strip(),
                "type":            stype,
                # Sub-conference (e.g. "FS 2: Quantum Information ..."):
                "topic":           (r.get("Session or Event Topic") or "").strip(),
                "date":            (r.get("Session or Event Date") or "").strip(),
                "location":        (r.get("Session or Event Location") or "").strip(),
                "presider":        presider,
                # RAW presider affiliations — builder shortens these.
                "presider_aff":    presider_aff,
                "details":         (r.get("Session or Event Details") or "").strip(),
                "start_ts":        parse_dt(r.get("Session or Event Date", ""),
                                            r.get("Session or Event Start Time", "")),
                "end_ts":          parse_dt(r.get("Session or Event Date", ""),
                                            r.get("Session or Event End Time", "")),
                # type id (== color token the app filters on) + its label:
                "color":           classify_session_color(
                                       stype,
                                       (r.get("Session or Event Title") or "")),
                "talk_ids":        [],
            }
            if synthesized_session:
                n_synth_sessions += 1

        fid = (r.get("Abstract Final ID") or "").strip()
        placeholder_title = (r.get("Abstract or Placeholder Title") or "").strip()
        stat_synth_talk = False
        if not fid:
            if not placeholder_title:
                continue
            n_in_sess = len(sessions[sa]["talk_ids"])
            fid = f"{sa}.{n_in_sess + 1}"
            stat_synth_talk = True

        sessions[sa]["talk_ids"].append(fid)

        authors_csv = (r.get("Abstract Authors") or "").strip()
        insts_csv = (r.get("Institutions All") or "").strip()
        presenter = (r.get("Abstract Presenter Name") or "").rstrip("*").strip()

        # --- PDF enrichment ---
        pdf_entry = pdf_entries.get(fid)
        pdf_pairs: list[tuple[str, str]] = []
        pdf_authors_ordered = ""
        affil_map = ""
        insts_det = ""
        abstract_pdf = ""
        speakers_pdf = ""
        # Per-talk stat flags. These are tallied into the running counters only
        # once the talk SURVIVES the drop filter below (see talks.append),
        # otherwise dropped talks would inflate the numerators above len(talks).
        stat_pdf_matched = False
        stat_authors_upgraded = False
        stat_speaker_upgraded = False
        if pdf_entry:
            stat_pdf_matched = True
            pdf_pairs = [tuple(p) for p in pdf_entry["pairs"]]
            pdf_authors_ordered = "; ".join(n for n, _ in pdf_pairs)
            affil_map = "; ".join((f"{n}={a}" if a else n) for n, a in pdf_pairs)
            insts_det = " | ".join(pdf_entry["institutions"])
            abstract_pdf = pdf_entry["abstract"]
            speakers_pdf = "; ".join(pdf_entry["speakers"])

        # --- Scrape enrichment: full names ---
        scr_talk = scraped_talks.get(fid)
        if not scr_talk:
            sess_title_key = _title_key(sessions[sa]["title"])
            talk_title_key = _title_key(placeholder_title)
            scr_talk = scraped_by_title.get((sess_title_key, talk_title_key))
        full_authors: list[str] = []
        full_speaker: str = ""
        scraped_status: str = ""
        if scr_talk:
            full_authors = scr_talk["authors"]
            full_speaker = scr_talk["speaker"]
            scraped_status = scr_talk.get("status", "")

        # Bespoke: a plenary row in the CSV may glue the speaker onto the title
        # ("Talk Title - Speaker Name") with no Final ID and no presenter/author
        # fields, so nothing matched the PDF/scrape. Recover the speaker from the
        # trailing " - Name" so the name is not left dangling in the talk title.
        if ("plenary" in (sessions[sa]["type"] or "").lower()
                and stat_synth_talk and " - " in placeholder_title
                and not presenter and not authors_csv
                and not full_authors and not full_speaker):
            head, _, tail = placeholder_title.rpartition(" - ")
            head = head.strip()
            tail = tail.strip()
            words = tail.split()
            looks_like_name = (head and 2 <= len(words) <= 4
                               and all(w[:1].isupper() for w in words))
            if looks_like_name:
                placeholder_title = head
                full_authors = [tail]
                full_speaker = tail

        upgraded_authors = False
        final_affil_map = affil_map

        if pdf_pairs and full_authors:
            pdf_by_key: dict[str, tuple[str, str]] = {}
            for pdf_name, pdf_affil in pdf_pairs:
                k = _name_key(pdf_name)
                if k and k not in pdf_by_key:
                    pdf_by_key[k] = (pdf_name, pdf_affil)
            improved = False
            matched_pairs: list[tuple[str, str]] = []
            for scraped_name in full_authors:
                hit = pdf_by_key.get(_name_key(scraped_name))
                if hit:
                    pdf_name, pdf_affil = hit
                    if _looks_fuller_tokens(scraped_name, pdf_name):
                        improved = True
                    matched_pairs.append((scraped_name, pdf_affil))
                else:
                    matched_pairs.append((scraped_name, ""))
            if improved:
                final_affil_map = "; ".join(
                    (f"{n}={a}" if a else n) for n, a in matched_pairs)
                upgraded_authors = True
                stat_authors_upgraded = True
        elif full_authors and any(_n_full_tokens(a) >= 2 for a in full_authors):
            final_affil_map = "; ".join(full_authors)
            upgraded_authors = True
            stat_authors_upgraded = True

        author_list_for_first_last = (full_authors
                                      if upgraded_authors and full_authors
                                      else split_authors(authors_csv))
        first_a = author_list_for_first_last[0] if author_list_for_first_last else ""
        last_a = author_list_for_first_last[-1] if author_list_for_first_last else ""
        same_a = (first_a == last_a)

        # Speaker priority: scrape > PDF underline > CSV presenter > first author
        if full_speaker and _looks_fuller_len(full_speaker, presenter):
            speaker = full_speaker
            stat_speaker_upgraded = True
        elif full_speaker:
            speaker = full_speaker
        elif speakers_pdf:
            speaker = speakers_pdf.split(";")[0].strip()
        elif presenter:
            speaker = presenter
        else:
            speaker = first_a

        speaker_pos = -1
        if speaker and author_list_for_first_last:
            tgt = _norm_name(speaker)
            for i, a in enumerate(author_list_for_first_last):
                if _norm_name(a) == tgt:
                    speaker_pos = i
                    break

        sess_title = sessions[sa]["title"]
        sess_type = sessions[sa]["type"]

        # Institutions: prefer the PDF's numbered list; otherwise synthesize a
        # list from the CSV's Institutions All. We emit RAW institution bodies
        # here and keep ALL of them (no de-dup) — the builder collapses
        # duplicates by their canonical SHORT name when it shortens, since
        # affiliation shortening lives there. Order is preserved.
        #
        # `inst_detailed_bodies` is the ordered list of detailed display forms
        # (author `insts` reference them by number). When there's no detailed
        # (PDF) list we fall back to the flat institution list; in that case
        # there is no per-author index structure to protect, so the builder MAY
        # collapse duplicates -> institutions_may_dedup = True.
        if insts_det:
            inst_detailed_bodies = [c.strip() for c in insts_det.split("|")
                                    if c.strip()]
            institutions_may_dedup = False
        else:
            inst_detailed_bodies = [p.strip() for p in insts_csv.split(";")
                                    if p.strip()]
            institutions_may_dedup = bool(inst_detailed_bodies)

        # The flat CSV institutions act as cleaner per-institution name variants
        # (positionally parallel to the PDF's detailed bodies). When the detailed
        # list IS the CSV list (no PDF), there's no separate cleaner variant.
        clean_variants = "" if not insts_det else insts_csv

        structured_authors = _structured_authors(final_affil_map)
        institutions = _structured_institutions(inst_detailed_bodies,
                                                clean_variants)

        status_tags = (r.get("Abstract Status") or "").strip()
        withdrawn = "withdrawn" in status_tags.lower()

        # CLEO 2025 marks Invited talks by emboldening the title in the planner
        # (no "[Invited Talk]" text and often nothing in the official CSV's
        # Abstract Status), so fold the scraped status in. Merge as a
        # ';'-separated tag list, de-duped case-insensitively.
        if scraped_status:
            existing = {t.strip().lower()
                        for t in status_tags.split(";") if t.strip()}
            merged = [t for t in status_tags.split(";") if t.strip()]
            for tag in scraped_status.split(";"):
                tag = tag.strip()
                if tag and tag.lower() not in existing:
                    existing.add(tag.lower())
                    merged.append(tag)
            status_tags = "; ".join(merged)
            withdrawn = withdrawn or "withdrawn" in status_tags.lower()

        # --- Talk timestamps (needed for the duration-based filter below) ---
        talk_start_ts = parse_dt(
            sessions[sa]["date"],
            r.get("Abstract or Placeholder Start Time", ""))
        talk_end_ts = parse_dt(
            sessions[sa]["date"],
            r.get("Abstract or Placeholder End Time", ""))

        # --- Reclassify short bold-derived "Invited" talks --------------------
        # CLEO 2025 has no "[Invited Talk]" text marker; the processor instead
        # infers "Invited" from a bolded title and records it as the distinct
        # "Invited (bold)" tag. But the bold-title heuristic over-fires: short
        # (sub-30-min) slots tagged this way are really ordinary CONTRIBUTED
        # talks, not invited ones. For those we DROP the bogus "Invited (bold)"
        # label and keep the talk as a normal contributed talk. Bold-only talks
        # that run 30 min or longer are treated as genuinely Invited (the tag is
        # normalized to plain "Invited" just below). Talks carrying an explicit
        # "Invited" marker are unaffected here.
        tag_set = {t.strip().lower() for t in status_tags.split(";") if t.strip()}
        bold_invited_only = ("invited (bold)" in tag_set
                             and "invited" not in tag_set)
        if bold_invited_only:
            duration_min = None
            if talk_start_ts and talk_end_ts:
                duration_min = (datetime.fromisoformat(talk_end_ts)
                                - datetime.fromisoformat(talk_start_ts)
                                ).total_seconds() / 60.0
            # Treat as invited only when we can confirm a >=30-min slot. A short
            # slot — or one whose times we couldn't parse — is a false positive,
            # so strip the inferred-invited tag and let it through as contributed.
            if duration_min is None or duration_min < 30:
                status_tags = "; ".join(
                    t.strip() for t in status_tags.split(";")
                    if t.strip() and t.strip().lower() != "invited (bold)")
                tag_set.discard("invited (bold)")
                n_bold_invited_reclassified += 1

        # Any surviving talk normalizes "Invited (bold)" back to plain
        # "Invited" so the emitted status is clean and downstream consumers see
        # a single canonical tag.
        if "invited (bold)" in tag_set:
            status_tags = "; ".join(
                ("Invited" if t.strip().lower() == "invited (bold)"
                 else t.strip())
                for t in status_tags.split(";") if t.strip())
            # De-dup in case both "Invited" and "Invited (bold)" were present.
            seen: set[str] = set()
            deduped: list[str] = []
            for t in status_tags.split(";"):
                t = t.strip()
                if t and t.lower() not in seen:
                    seen.add(t.lower())
                    deduped.append(t)
            status_tags = "; ".join(deduped)

        # This talk survived the drop filter — tally its stat flags now so the
        # numerators stay consistent with the final len(talks) denominator.
        if stat_pdf_matched:
            n_pdf_matched += 1
        if stat_authors_upgraded:
            n_authors_upgraded += 1
        if stat_speaker_upgraded:
            n_speaker_upgraded += 1
        if stat_synth_talk:
            n_synth_talks += 1

        talks.append({
            "id":            fid,
            "session_id":    sa,
            "title":         placeholder_title,
            "number":        fid,
            "start_ts":      talk_start_ts,
            "end_ts":        talk_end_ts,
            "presenter":     presenter,
            "speaker":       speaker,
            "speaker_pos":   speaker_pos,
            # Ordered authors; each author's `insts` are the EXPLICIT institution
            # numbers (the `n` in `institutions`) they belong to. The builder
            # resolves these and shortens to produce speaker_aff/last_aff.
            "authors":       structured_authors,
            # Loose name forms (initials etc.) kept ONLY as search fodder.
            "author_aliases": _author_aliases(structured_authors,
                                              authors_csv, pdf_authors_ordered),
            # Numbered institutions: each {n, name (detailed display form),
            # alt_names (cleaner variants the builder may prefer when shortening
            # a fallback)}.
            "institutions":  institutions,
            # True when the institution list has no per-author index structure
            # to protect, so the builder MAY collapse duplicates by short name.
            "institutions_may_dedup": institutions_may_dedup,
            "abstract":      (abstract_pdf
                              or (r.get("Abstract Body") or "").strip()),
            "status":        status_tags,
            "withdrawn":     withdrawn,
            "first_author":  first_a,
            "last_author":   "" if same_a else last_a,
            "color":         classify_talk_color(
                                placeholder_title,
                                sess_title, sess_type, status_tags),
            "location":      sessions[sa]["location"],
        })

    print(f"[build] {len(sessions)} sessions, {len(talks)} talks")
    print(f"[build]   PDF matched: {n_pdf_matched}/{len(talks)} talks")
    print(f"[build]   author lists upgraded to full names: "
          f"{n_authors_upgraded}/{len(talks)}")
    print(f"[build]   speakers upgraded to full names: "
          f"{n_speaker_upgraded}/{len(talks)}")
    print(f"[build]   presider from scrape: {n_presider_scraped}; "
          f"from CSV fallback: {n_presider_csv}")
    print(f"[build]   synthesized codes: {n_synth_sessions} session(s), "
          f"{n_synth_talks} talk(s)")
    print(f"[build]   short bold-invited talks reclassified as contributed: "
          f"{n_bold_invited_reclassified}")

    # -------------------------------------------------------------------
    # Presider-affiliation backfill is intentionally NOT done here.
    #
    # When a session's presider has no usable affiliation, the backfill step
    # borrows one from a paper that presider authors elsewhere — choosing the
    # MOST COMMON affiliation across their papers. That count is only reliable
    # once affiliations are canonicalized to short names (so "Univ. of Maryland,
    # College Park, MD" and "University of Maryland" count as one vote, not
    # two). Affiliation shortening lives in the builder, so the backfill must
    # run there too. We leave each session's presider_aff exactly as the
    # scrape/CSV provided it (possibly empty or malformed); the builder fills
    # the gaps after it has the affiliation map. Everything the backfill needs
    # is already on the talk records it emits (authors, institutions,
    # author_aliases).
    # -------------------------------------------------------------------

    # -------------------------------------------------------------------
    # Affiliation SOURCES for the builder's affiliation map: one flat,
    # de-duplicated, sorted list of RAW affiliation strings the shortener
    # learns from. We pool three harvests (nothing downstream cares which is
    # which): full multi-field address lines from the PDF, presider affiliations
    # from the scrape, and institution-level forms from the CSV. The presider
    # and institution strings can be ';'-joined lists, so we split them here at
    # the source; the full-address lines are kept whole.
    # -------------------------------------------------------------------
    presider_aff_strings: set[str] = set()
    for srow in scraped_rows:
        v = srow.get("session_presider_affiliations") or ""
        for piece in v.split(";"):
            p = piece.strip()
            if p:
                presider_aff_strings.add(p)
    institution_strings: set[str] = set()
    for orow in official_rows:
        v = orow.get("Institutions All") or ""
        for piece in v.split(";"):
            p = piece.strip()
            if p:
                institution_strings.add(p)

    # Pool every affiliation source into one flat, de-duplicated, sorted list.
    # Full-address lines are kept whole; the presider/institution strings were
    # already split on ';' above.
    affiliation_pool: set[str] = set(pdf_affiliation_lines or [])
    affiliation_pool |= presider_aff_strings
    affiliation_pool |= institution_strings

    return {
        "conference_name": conference_name or "",
        "sessions": sorted(sessions.values(),
                           key=lambda s: (s["start_ts"] or "")),
        "talks":    sorted(talks, key=lambda t: (t["start_ts"] or "")),
        "session_types": SESSION_TYPE_REGISTRY,
        "talk_types":    TALK_TYPE_REGISTRY,
        "affiliation_sources": sorted(affiliation_pool),
    }




# =============================================================================
# Short-course title/affiliation normalization helpers
# =============================================================================
def _normalize_course_title(title: str) -> str:
    """Normalize a course title for matching between the website (<h2>) and the
    planner header: lowercase, strip all punctuation to spaces, collapse
    whitespace. 'LiDAR and remote sensing: An application-oriented introduction'
    and 'LiDAR and Remote Sensing: An Application-Oriented Introduction' both
    normalize to the same key."""
    t = (title or "").lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _strip_course_aff_country(aff: str) -> str:
    """Drop the trailing ', <Country>' from a website affiliation, keeping any
    internal commas. 'CREOL, The College of Optics and Photonics, USA' ->
    'CREOL, The College of Optics and Photonics'; 'MIT, USA' -> 'MIT'."""
    aff = (aff or "").strip()
    if not aff:
        return ""
    # Remove only the final comma-separated chunk (the country).
    return re.sub(r",\s*[^,]+$", "", aff).strip(" .,;")


def fetch_short_course_instructors(page) -> dict[str, dict]:
    """Open the archived short-courses page in a NEW tab in the same browser
    context and return a map keyed by NORMALIZED course title:
        { '<norm title>': {'title','instructor','aff'}, … }.

    Each course on the page is a block with an <h2> course title, an <h3>
    instructor name, and an <h4> affiliation. We pair each <h3> with the <h4>
    that follows it in the same column and the nearest preceding <h2> title.
    Returns {} on any failure so the scrape carries on without instructors."""
    log(f"  [course] fetching short-course instructors from "
        f"{SHORT_COURSES_URL}")
    ctx = page.context
    sc_page = None
    try:
        sc_page = ctx.new_page()
        sc_page.goto(SHORT_COURSES_URL, wait_until="domcontentloaded",
                     timeout=60_000)
        # Walk the heading elements in document order. A course is an <h3>
        # (instructor) immediately followed (in order) by an <h4> (affiliation),
        # with the most recent <h2> before it being the course title. This is
        # robust to the page's nested column markup.
        triples = sc_page.evaluate(
            r"""
            () => {
                const heads = Array.from(
                    document.querySelectorAll('h2, h3, h4'));
                const txt = el => (el.innerText || el.textContent || '')
                    .replace(/\s+/g, ' ').trim();
                const out = [];
                let lastH2 = '';
                for (let i = 0; i < heads.length; i++) {
                    const tag = heads[i].tagName.toLowerCase();
                    if (tag === 'h2') { lastH2 = txt(heads[i]); continue; }
                    if (tag === 'h3') {
                        // Look ahead for the next heading; if it's an h4, treat
                        // it as this instructor's affiliation.
                        let aff = '';
                        if (i + 1 < heads.length &&
                            heads[i + 1].tagName.toLowerCase() === 'h4') {
                            aff = txt(heads[i + 1]);
                        }
                        out.push({title: lastH2,
                                  instructor: txt(heads[i]),
                                  aff: aff});
                    }
                }
                return out;
            }
            """
        )
    except Exception as e:
        log(f"  [course] couldn't load {SHORT_COURSES_URL}: {e}")
        return {}
    finally:
        if sc_page is not None:
            try:
                sc_page.close()
            except Exception:
                pass

    out: dict[str, dict] = {}
    for t in triples or []:
        title = (t.get("title") or "").strip()
        instructor = (t.get("instructor") or "").strip()
        aff = _strip_course_aff_country(t.get("aff", ""))
        if not title or not instructor:
            continue
        # Heuristic: an instructor name is a short, non-sentence string. Skip
        # any <h3> that's clearly a section heading (e.g. "Short Courses").
        if len(instructor.split()) > 6 or instructor.lower() == "short courses":
            continue
        key = _normalize_course_title(title)
        if not key or key in out:
            continue
        out[key] = {"title": title, "instructor": instructor, "aff": aff}
        log(f"  [course]   {title!r} -> {instructor!r}"
            + (f" ({aff})" if aff else ""))
    log(f"  [course] parsed {len(out)} short-course instructor(s) from the "
        "website.")
    return out


def attach_short_course_instructors(page, program: list[dict]) -> None:
    """Mutate `program` in place: for every short-course session, match its
    title to the archived short-courses page and stash the instructor name and
    affiliation as `course_instructor` / `course_instructor_aff`. These become
    the session's presider (+ affiliation) in write_program_csv when the planner
    gave no presider."""
    courses = fetch_short_course_instructors(page)
    if not courses:
        log("  [course] no website instructors available; "
            "short courses will have no instructor.")
        return

    n_matched = 0
    for s in program:
        if not _is_short_course_session(s):
            continue
        hdr = parse_session_header(s.get("headerText", ""))
        key = _normalize_course_title(hdr.get("title", ""))
        rec = courses.get(key)
        if not rec:
            continue
        s["course_instructor"] = rec["instructor"]
        s["course_instructor_aff"] = rec.get("aff", "")
        n_matched += 1
    log(f"  [course] matched instructors to {n_matched} short-course "
        "session(s) in the program.")


def _is_short_course_session(s: dict) -> bool:
    """A session is a short course if its header title (or code) marks it so.
    We key off the visible header text, e.g. 'SC477. Short Course: LiDAR …'."""
    hdr = parse_session_header(s.get("headerText", ""))
    title = (hdr.get("title") or "").lower()
    code = (hdr.get("code") or "").upper()
    return ("short course" in title) or code.startswith("SC")


# =============================================================================
# Abstract-book supplementation
# =============================================================================
def supplement_from_abstract_book(program: list[dict]) -> None:
    """Mutate `program` in place: for sessions whose planner-side talk
    list is empty, fill it in from the official abstract-book CSV that
    Phase 0 already downloaded. Catches poster sessions (JTU1, JW1, etc.)
    whose contents the planner only exposes behind a 'View Session
    Details' popup, plus any other session we somehow missed.

    Talks added this way carry a fake `timeText` matching the abstract-
    book's per-talk start time, and their html is a synthetic stand-in
    that parse_talk_content can already handle (number, italics-wrapped
    authors, optional <u>presenter</u>)."""
    book_path = INPUT_OFFICIAL_CSV
    if not book_path.exists():
        log(f"  [supp] abstract-book CSV not found at {book_path} — "
            "skipping supplementation. (Did Phase 0 download skip?)")
        return

    # Index the abstract book by session code. We only care about rows
    # that have an Abstract Final ID — those are the actual talks/posters.
    by_code: dict[str, list[dict]] = {}
    try:
        with open(book_path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                code = (row.get("Session or Event Abbreviation") or "").strip()
                fid  = (row.get("Abstract Final ID") or "").strip()
                if not code or not fid:
                    continue
                by_code.setdefault(code, []).append(row)
    except Exception as e:
        log(f"  [supp] couldn't read abstract-book CSV: {e}")
        return

    sessions_filled = 0
    talks_added     = 0

    for s in program:
        if s["talks"]:
            continue                              # already have talks
        # The session header text starts with the session code, e.g.
        # "JTU1. Poster Session I". Parse it out.
        hdr = parse_session_header(s.get("headerText", ""))
        code = hdr["code"]
        rows = by_code.get(code)
        if not rows:
            continue

        for r in rows:
            talk_id   = (r.get("Abstract Final ID") or "").strip()
            title     = (r.get("Abstract or Placeholder Title") or "").strip()
            presenter = (r.get("Abstract Presenter Name") or "").strip()
            authors   = (r.get("Abstract Authors") or "").strip()
            status    = (r.get("Abstract Status") or "").strip()
            t_start   = (r.get("Abstract or Placeholder Start Time") or "").strip()
            t_end     = (r.get("Abstract or Placeholder End Time") or "").strip()

            # Build an authors string with the presenter wrapped in <u> so
            # parse_talk_content (the same parser used for planner-scraped
            # rows) recognises them as the speaker. The abstract-book
            # presenter is given in short form (e.g. "A. Godard*") whereas
            # the authors list usually has the full form. If the short
            # form happens to also be the FIRST author we mark that one;
            # otherwise we leave the authors string alone and set the
            # speaker via the final extracted dict.
            authors_html = authors
            if presenter and authors:
                first_author = authors.split(";")[0].strip().rstrip("*")
                presenter_clean = presenter.rstrip("*").strip()
                if first_author == presenter_clean:
                    head, sep, rest = authors.partition(";")
                    authors_html = f"<u>{head.strip()}</u>{sep}{rest}"

            # Synthetic talk_html that parse_talk_content can parse.
            # status-tag bracket markers are detected by that function, so
            # a Withdrawn abstract gets "(WITHDRAWN)" baked into the title.
            title_marked = title
            if status and status.lower() == "withdrawn":
                title_marked = f"(WITHDRAWN) {title}"
            talk_html = (f"{talk_id}. {title_marked}"
                         f"<br><i>{authors_html}</i>"
                         f"<br><table>VPR</table>")
            time_text = (f"{t_start} - {t_end}").strip(" -")

            s["talks"].append({
                "timeText": time_text,
                "html":     talk_html,
                "text":     "",
                "vprId":    "",   # no planner VPR id; came from the book
            })
            talks_added += 1
        sessions_filled += 1

    if talks_added:
        log(f"  [supp] supplemented {talks_added} talk(s) into "
            f"{sessions_filled} session(s) from the abstract book "
            f"(e.g. poster sessions like JTU1, JW1).")
    else:
        log("  [supp] nothing to supplement — every session already "
            "had its talks in the planner data.")



# =============================================================================
# Offline planner-HTML parsing (lxml).
#
# The downloader saved the FULLY-EXPANDED planner DOM to disk. We re-parse that
# static HTML with lxml to recover the exact same day -> session -> talk
# structure a live browser pass would produce. A live browser reads the DOM via
# the browser's .innerText / .innerHTML; the helpers below reproduce those two
# operations faithfully on the lxml tree:
#
#   * inner_text(el)  mimics element.innerText: <br> (and any block-level child)
#     becomes a newline, inline children (b/i/u/a/sup/sub/span/…) do not, and
#     runs of spaces/tabs collapse to a single space with per-line trimming.
#   * inner_html(el)  mimics element.innerHTML: it serializes the element with
#     lxml's HTML serializer and strips the outer tag, so stray '<' / '&' in
#     text nodes come back escaped as '&lt;' / '&amp;' like the browser — and,
#     crucially for CLEO 2025, the <b>…</b> bold markup that flags Invited talks
#     is preserved so parse_talk_content's _title_is_bolded still fires.
#
# One structural note: an HTML parser (like the browser, and like lxml/libxml2)
# may HOIST the per-talk "View Presentation" <table> out of the content <p>
# (a <p> can't legally contain a block <table>). So the talk's VPR id is read
# from the enclosing CONTENT CELL rather than from the content <p> itself. This
# id isn't used in the final JSON anyway; it's carried only for parity with the
# original scraped rows.
#
# No network and no browser are involved — only lxml + the saved files.
# =============================================================================
def _bootstrap_lxml() -> None:
    """Install lxml if absent (the only third-party dep this parsing step needs)."""
    try:
        import lxml  # noqa: F401
    except ImportError:
        print("[setup] Installing the 'lxml' package…")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", "lxml"])


# Inline (phrasing) elements whose boundaries do NOT introduce a line break in
# innerText. Everything else (p, div, tr, td, table, li, …) is treated as a
# block and contributes newlines around its content, matching browser layout.
_INLINE_TAGS = {
    "b", "strong", "i", "u", "a", "span", "sup", "sub", "font", "em", "small",
    "mark", "abbr", "cite", "code", "s", "strike", "big", "tt", "label",
}


def _inner_text_parts(el, parts: list[str]) -> None:
    if el.text:
        parts.append(el.text)
    for ch in el:
        tag = ch.tag.lower() if isinstance(ch.tag, str) else ""
        if tag == "br":
            parts.append("\n")
        elif tag in ("script", "style"):
            pass
        else:
            block = tag not in _INLINE_TAGS
            if block:
                parts.append("\n")
            _inner_text_parts(ch, parts)
            if block:
                parts.append("\n")
        if ch.tail:
            parts.append(ch.tail)


def inner_text(el) -> str:
    """Reproduce element.innerText for the lxml element `el`: <br>/block edges
    become newlines, inline edges don't, intra-line whitespace collapses, and
    empty lines are dropped. (Callers .strip() the result as the JS did.)"""
    parts: list[str] = []
    _inner_text_parts(el, parts)
    lines = [re.sub(r"[ \t\r]+", " ", ln).strip()
             for ln in "".join(parts).split("\n")]
    return "\n".join(ln for ln in lines if ln != "")


_OUTER_TAG_RE = re.compile(r"^<[^>]+>(.*)</[^>]+>\s*$", re.S)


def inner_html(el) -> str:
    """Reproduce element.innerHTML for the lxml element `el`. Serializing the
    WHOLE element (then peeling the outer tag) is what makes stray '<' and '&'
    in text nodes come back as '&lt;'/'&amp;', and preserves <b> bold runs."""
    from lxml import html as _lh
    full = _lh.tostring(el, encoding="unicode", method="html")
    m = _OUTER_TAG_RE.match(full)
    return m.group(1) if m else full


def _load_html_root(html_path: Path):
    """Parse a saved HTML file into an lxml element tree. We read the bytes as
    UTF-8 explicitly (the saved files are UTF-8 but carry no charset meta, so
    letting lxml sniff would mis-decode accented names like 'Béla')."""
    from lxml import html as _lh
    text = html_path.read_text(encoding="utf-8")
    return _lh.document_fromstring(text)


def _has_class(node, cls: str) -> bool:
    return cls in (node.get("class") or "").split()


def collect_program_from_html(html_path: Path) -> list[dict]:
    """Re-parse the saved, fully-expanded planner DOM into the same flat list
    of session dicts the live scrape produced:
        [ { dayName, sessionId, headerHTML, headerText, vsdId,
            talks: [ { timeText, html, text, vprId } ] } ]

    Pure lxml — no browser, no network."""
    if not html_path.exists():
        log(f"  [parse] ERROR: planner HTML not found at {html_path}.")
        return []

    log(f"  [parse] loading saved planner DOM: {html_path}")
    root = _load_html_root(html_path)

    day_divs = [d for d in root.xpath('//div[starts-with(@id,"LEVEL:")]')
                if _has_class(d, "text")]
    log(f"  [parse] {len(day_divs)} day container(s) found")

    out: list[dict] = []
    for i, day_div in enumerate(day_divs, 1):
        hdr_el = day_div.xpath('.//p[contains(concat(" ",'
                               'normalize-space(@class)," ")," pageheader ")]')
        day_name = inner_text(hdr_el[0]).strip() if hdr_el else ""

        sessions: list[dict] = []
        sess_divs = [s for s in
                     day_div.xpath('.//div[starts-with(@id,"SESSION:")]')
                     if _has_class(s, "text")]
        for s_div in sess_divs:
            session_id = s_div.get("id").replace("SESSION:", "")

            header_html, header_text = "", ""
            for p in s_div.xpath('.//p[contains(concat(" ",'
                                 'normalize-space(@class)," "),'
                                 '" pagecontents ")]'):
                if p.xpath('ancestor::td[contains(concat(" ",'
                           'normalize-space(@class)," "),'
                           '" ip_expanded_session ")]'):
                    continue
                if not p.text_content().strip():
                    continue
                header_html = inner_html(p)
                header_text = inner_text(p).strip()
                break

            vsd = s_div.xpath('.//*[starts-with(@id,"VSD:")]')
            vsd_id = vsd[0].get("id") if vsd else ""

            talks: list[dict] = []
            exp_tds = [t for t in s_div.xpath('.//td[contains(concat(" ",'
                       'normalize-space(@class)," "),'
                       '" ip_expanded_session ")]')]
            for exp_td in exp_tds:
                for tr in exp_td.xpath('.//tr'):
                    cells = [c for c in tr.xpath('./td')
                             if _has_class(c, "ip_border_top")]
                    if len(cells) < 3:
                        continue
                    time_text = inner_text(cells[1]).strip()
                    content_ps = [p for p in cells[2].xpath('.//p')
                                  if _has_class(p, "pagecontents")]
                    if not content_ps:
                        continue
                    content_p = content_ps[0]
                    # VPR id: search the whole content CELL, not just the <p>,
                    # because the parser hoists the "View Presentation" <table>
                    # (which holds the VPR <div>) out of the <p>.
                    vpr = cells[2].xpath('.//*[starts-with(@id,"VPR:")]')
                    vpr_id = vpr[0].get("id") if vpr else ""
                    talks.append({
                        "timeText": time_text,
                        "html":     inner_html(content_p),
                        "text":     inner_text(content_p),
                        "vprId":    vpr_id,
                    })
            sessions.append({"sessionId": session_id,
                             "headerHTML": header_html,
                             "headerText": header_text,
                             "vsdId": vsd_id,
                             "talks": talks})

        n_ses = len(sessions)
        n_tk  = sum(len(s["talks"]) for s in sessions)
        log(f"  [parse day {i}/{len(day_divs)}] '{day_name}': "
            f"{n_ses} session(s), {n_tk} talk(s)")
        for s in sessions:
            s["dayName"] = day_name
            out.append(s)
    return out


# =============================================================================
# Short-course instructors — parsed from the saved short-courses HTML (lxml).
#
# CLEO 2025's archived short-courses page has, per course, an <h2> title, an
# <h3> instructor, and an <h4> affiliation. The page carries no SC codes, so we
# match each course to its planner session by NORMALIZED TITLE (see
# _normalize_course_title). This mirrors a live fetch, which would walk
# h2/h3/h4 in document order.
# =============================================================================
def _heading_text(h) -> str:
    """Visible text of a short-courses heading (<h2>/<h3>/<h4>), block-aware.

    Most headings hold their text directly, but some course TITLES on the
    archived page are marked up as block children INSIDE the heading, e.g.
    `<h2><p>Foundations of</p> <p>Nonlinear Optics</p></h2>`. An HTML parser
    (like a browser, and like lxml) hoists those block <p> elements OUT of the
    heading — a heading can't legally contain a <p> — leaving the heading empty
    and the title text stranded in the heading's enclosing title container.
    A plain text_content() on the now-empty heading would yield "" (dropping
    the course); even when it isn't hoisted, concatenating the <p> runs without
    a separator gives "Foundations ofNonlinear Optics" (no space), which then
    fails the title match.

    So: take the heading's own block-aware inner_text first; if that's empty
    (its blocks were hoisted), fall back to the enclosing container's text, but
    only the portion BEFORE any nested heading, so we never pull the following
    instructor/affiliation into the title. inner_text() turns the inter-<p>
    boundary into a newline, which _normalize_course_title collapses to a space,
    restoring "Foundations of Nonlinear Optics"."""
    own = inner_text(h)
    if own:
        return re.sub(r"\s+", " ", own).strip()
    parent = h.getparent()
    if parent is None:
        return ""
    # Collect text from the parent up to (excluding) the first nested heading,
    # so a container that also wraps the h3/h4 doesn't bleed into the title.
    parts: list[str] = [parent.text or ""]
    for child in parent:
        ctag = child.tag.lower() if isinstance(child.tag, str) else ""
        if ctag in ("h1", "h2", "h3", "h4", "h5", "h6") and child is not h:
            break
        parts.append(inner_text(child))
        if child.tail:
            parts.append(child.tail)
    text = "\n".join(p for p in parts if p)
    return re.sub(r"\s+", " ", text).strip()


def fetch_short_course_instructors_from_html(html_path: Path) -> dict[str, dict]:
    """Parse the saved short-courses HTML and return a map keyed by NORMALIZED
    course title: { '<norm title>': {'title','instructor','aff'}, … }.

    Pure lxml — no browser, no network. Returns {} on any failure (missing
    file, layout change, etc.) so the caller can carry on without instructors."""
    if not html_path.exists():
        log(f"  [course] short-courses HTML not found at {html_path}; "
            "short courses will have no instructor.")
        return {}

    log(f"  [course] parsing saved short-courses HTML: {html_path}")
    try:
        root = _load_html_root(html_path)
    except Exception as e:
        log(f"  [course] couldn't read {html_path}: {e}")
        return {}

    # Walk h2/h3/h4 in document order. A course is an <h3> (instructor) whose
    # immediately following heading is an <h4> (affiliation), with the most
    # recent <h2> before it as the course title — same rule as the live fetch.
    heads = root.xpath('//h2 | //h3 | //h4')
    triples: list[dict] = []
    last_h2 = ""
    for i, h in enumerate(heads):
        tag = (h.tag.lower() if isinstance(h.tag, str) else "")
        txt = _heading_text(h)
        if tag == "h2":
            last_h2 = txt
            continue
        if tag == "h3":
            aff = ""
            if i + 1 < len(heads):
                nxt = heads[i + 1]
                if (nxt.tag.lower() if isinstance(nxt.tag, str) else "") == "h4":
                    aff = _heading_text(nxt)
            triples.append({"title": last_h2, "instructor": txt, "aff": aff})

    out: dict[str, dict] = {}
    for t in triples:
        title = (t.get("title") or "").strip()
        instructor = (t.get("instructor") or "").strip()
        aff = _strip_course_aff_country(t.get("aff", ""))
        if not title or not instructor:
            continue
        # Heuristic: an instructor name is a short, non-sentence string. Skip
        # any <h3> that's clearly a section heading (e.g. "Short Courses").
        if len(instructor.split()) > 6 or instructor.lower() == "short courses":
            continue
        key = _normalize_course_title(title)
        if not key or key in out:
            continue
        out[key] = {"title": title, "instructor": instructor, "aff": aff}
        log(f"  [course]   {title!r} -> {instructor!r}"
            + (f" ({aff})" if aff else ""))
    log(f"  [course] parsed {len(out)} short-course instructor(s) from the "
        "saved page.")
    return out


def attach_short_course_instructors(program: list[dict],
                                    html_path: Path) -> None:
    """Mutate `program` in place: for every short-course session, match its
    title to the saved short-courses page and stash the instructor name and
    affiliation as `course_instructor` / `course_instructor_aff`. These become
    the session's presider (+ affiliation) when the planner gave no presider."""
    courses = fetch_short_course_instructors_from_html(html_path)
    if not courses:
        log("  [course] no website instructors available; "
            "short courses will have no instructor.")
        return

    n_matched = 0
    for s in program:
        if not _is_short_course_session(s):
            continue
        hdr = parse_session_header(s.get("headerText", ""))
        key = _normalize_course_title(hdr.get("title", ""))
        rec = courses.get(key)
        if not rec:
            continue
        s["course_instructor"] = rec["instructor"]
        s["course_instructor_aff"] = rec.get("aff", "")
        n_matched += 1
    log(f"  [course] matched instructors to {n_matched} short-course "
        "session(s) in the program.")


# =============================================================================
# Program -> scraped rows, IN MEMORY (the intermediate CSV is skipped).
# Identical column set + per-row construction to the old write_program_csv.
# =============================================================================
def program_to_scraped_rows(program: list[dict]) -> list[dict]:
    empty_talk_fields = {
        "talk_number":     "",
        "talk_title":      "",
        "talk_time":       "",
        "talk_status":     "",
        "talk_speaker":    "",
        "talk_authors":    "",
        "talk_n_authors":  "",
        "vpr_id":          "",
    }

    rows: list[dict] = []
    for s in program:
        day        = s["dayName"]
        session_id = s["sessionId"]
        hdr        = parse_session_header(s["headerText"])
        presiders  = parse_presiders(hdr["presiders_raw"])

        presider_names = "; ".join(p["name"] for p in presiders)
        presider_affs  = "; ".join(p["affiliation"] for p in presiders)

        instructor = (s.get("course_instructor") or "").strip()
        instructor_aff = (s.get("course_instructor_aff") or "").strip()
        if instructor and not presider_names:
            presider_names = instructor
            if instructor_aff and not presider_affs:
                presider_affs = instructor_aff

        common = {
            "day":                            day,
            "session_id":                     session_id,
            "session_code":                   hdr["code"],
            "session_title":                  hdr["title"],
            "session_time":                   hdr["time"],
            "session_location":               hdr["location"],
            "session_presiders_raw":          hdr["presiders_raw"],
            "session_presider_names":         presider_names,
            "session_presider_affiliations":  presider_affs,
        }

        rows.append({**common, **empty_talk_fields, "row_type": "session"})

        for t in s["talks"]:
            tk = parse_talk_content(t["html"])
            status_tags = list(tk["status_tags"])
            cell_status = time_cell_status(t["timeText"])
            if cell_status and cell_status not in status_tags:
                status_tags.append(cell_status)
            talk_time = "" if cell_status else t["timeText"]

            rows.append({**common,
                         "row_type":        "talk",
                         "talk_number":     tk["number"],
                         "talk_title":      tk["title"],
                         "talk_time":       talk_time,
                         "talk_status":     "; ".join(status_tags),
                         "talk_speaker":    tk["speaker"],
                         "talk_authors":    "; ".join(tk["authors"]),
                         "talk_n_authors":  str(len(tk["authors"])),
                         "vpr_id":          t["vprId"]})

    log(f"  [rows] built {len(rows)} scraped row(s) in memory "
        "(intermediate CSV skipped)")
    return rows


# =============================================================================
# Main — wire the pieces together and write conference_data.json.
# =============================================================================
def _collapse_session_tags(sessions):
    """Collapse each session's legacy ``type``/``topic`` into an ordered list of
    labelled ``tags`` ({"key", "value"} pairs), shown in the app as
    "Key: Value · Key: Value". Redundant topics are dropped: empty,
    identical to the session id, or merely restating the format."""
    for s in sessions:
        fmt = (s.pop("type", None) or "").strip()
        topic = (s.pop("topic", None) or "").strip()
        tags = []
        if fmt:
            tags.append({"key": "Session Type", "value": fmt})
        tl, fl = topic.casefold(), fmt.casefold()
        redundant = (
            not topic
            or tl == str(s.get("id", "")).casefold()
            or (bool(fl) and (tl == fl or tl.startswith(fl)))
        )
        if not redundant:
            head = topic.split(":", 1)[0].strip()
            if ":" in topic and head and " " not in head:
                k, v = topic.split(":", 1)
                tags.append({"key": k.strip(), "value": v.strip()})
            else:
                tags.append({"key": "Session Topic", "value": topic})
        if tags:
            s["tags"] = tags
    return sessions


def main() -> None:
    log("=" * 72)
    log("[config] CLEO 2025 PROCESSOR starting up.")
    log(f"[config]   script dir          : {SCRIPT_DIR}")
    log(f"[config]   data dir            : {DATA_DIR}")
    log(f"[config]   planner HTML        : {INPUT_DOM_HTML}")
    log(f"[config]   short-courses HTML  : {INPUT_SHORTCOURSE_HTML}")
    log(f"[config]   bundled JSON out    : {OUTPUT_JSON}")
    log("=" * 72)

    _bootstrap_lxml()

    # ---------------- 1. parse the saved planner DOM ------------------
    log("[1/5] Parsing the saved planner DOM into session/talk data…")
    program = collect_program_from_html(INPUT_DOM_HTML)
    n_sessions = len(program)
    n_talks    = sum(len(s["talks"]) for s in program)
    log(f"  Sessions/events parsed: {n_sessions}")
    log(f"  Talks parsed:           {n_talks}")

    # ---------------- 2. short-course instructors ---------------------
    log("[2/5] Attaching short-course instructors from the saved page…")
    attach_short_course_instructors(program, INPUT_SHORTCOURSE_HTML)

    # ---------------- 3. supplement poster sessions etc. --------------
    log("[3/5] Supplementing empty sessions from the abstract-book CSV…")
    supplement_from_abstract_book(program)

    # ---------------- 4. build the scraped rows in memory -------------
    log("[4/5] Building scraped program rows in memory…")
    scraped_rows = program_to_scraped_rows(program)

    # ---------------- 5. bundle everything into the JSON --------------
    log("[5/5] Bundling everything into conference_data.json…")
    write_data_json(scraped_rows)

    print(flush=True)
    print("=" * 72, flush=True)
    print("DONE (process only).", flush=True)
    print(f"  data dir     : {DATA_DIR}", flush=True)
    print(f"  bundled JSON : {OUTPUT_JSON}", flush=True)
    print("=" * 72, flush=True)


def write_data_json(scraped_rows: list[dict]) -> None:
    """Read the PDF and the official CSV, combine them with the in-memory
    scraped rows, hand them to build_conference_data() to produce the clean,
    FINAL conference_data.json, and write it."""
    log(f"[json] Building {OUTPUT_JSON.name} from PDF + CSV + scraped rows…")

    # Re-resolve the official files now (a download may have placed them, or the
    # user dropped them into data/ under the site's own names).
    official_csv = _resolve_data_file(
        "CLEO2025_Program_Abstracts.csv", ".csv", "official abstract CSV")
    official_pdf = _resolve_data_file(
        "CLEO2025_Program_Abstracts.pdf", ".pdf", "official abstract PDF")

    # --- official abstracts CSV rows -------------------------------------
    official_rows: list[dict] = []
    log(f"[json]   reading official CSV: {official_csv}")
    if official_csv.exists():
        with open(official_csv, encoding="utf-8-sig", newline="") as f:
            official_rows = list(csv.DictReader(f))
        log(f"[json]     official CSV: {len(official_rows)} row(s)")
    else:
        log(f"[json]     WARNING: {official_csv.name} not found; "
            "official rows will be empty.")

    log(f"[json]   scraped rows (in memory): {len(scraped_rows)} row(s)")

    # --- PDF: per-abstract entries + full-address affiliation lines ------
    pdf_entries: dict[str, dict] = {}
    pdf_affil_lines: list[str] = []
    conference_name: str = ""
    log(f"[json]   reading PDF: {official_pdf}")
    if official_pdf.exists():
        _bootstrap_pdfplumber()
        conference_name = extract_conference_name(official_pdf)
        if conference_name:
            log(f"[json]     conference name from cover page: "
                f"{conference_name!r}")
        else:
            log("[json]     WARNING: couldn't read conference name from "
                "cover page; downstream will fall back to a default.")
        log("[json]     parsing PDF abstract pages (this can take a while)…")
        pdf_entries = parse_pdf(official_pdf)
        log(f"[json]     PDF abstracts parsed: {len(pdf_entries)}")
        log("[json]     extracting full-address affiliation lines from PDF…")
        pdf_affil_lines = sorted(extract_pdf_affiliations(official_pdf))
        log(f"[json]     PDF affiliation lines: {len(pdf_affil_lines)}")
    else:
        log(f"[json]     WARNING: {official_pdf.name} not found; "
            "pdf_entries, conference name, and PDF affiliation lines "
            "will be empty.")

    data = build_conference_data(
        conference_name=conference_name,
        scraped_rows=scraped_rows,
        official_rows=official_rows,
        pdf_entries=pdf_entries,
        pdf_affiliation_lines=pdf_affil_lines,
    )

    _collapse_session_tags(data["sessions"])
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    size_kb = OUTPUT_JSON.stat().st_size / 1024
    log(f"[json] Wrote {OUTPUT_JSON} ({size_kb:,.1f} KB) — "
        f"{len(data.get('sessions', []))} sessions, "
        f"{len(data.get('talks', []))} talks (clean, final)")


if __name__ == "__main__":
    main()