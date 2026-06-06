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
process_program_test2026.py — PROCESS ONLY.

The "processor" half of the TEST 2026 pipeline. TEST 2026 is a SYNTHETIC
example conference whose single source of truth is one PDF:

    data/TEST2026_Program_Abstracts.pdf

This script reads ONLY that PDF (no HTML, no CSV, no network, no browser) and
turns it into the same clean, FINAL conference_data.json that the shared
downstream scripts (build_conference_app.py / build_affiliation_map.py)
consume — exactly the JSON shape produced by a real conference's processor.

The PDF has two regions, both authored to mirror a typical conference book:

  1. Program-schedule pages (right after the cover). One block per session: a
     session-header line — "<start>-<end>, <location>, <CODE>. <Title>, <Type>
     [, <Topic>][, Presider: <Name>[, <Aff>]]" — followed by one line per talk
     — "<start>-<end> <CODE>.<n>. [<status tag>] <Title> <authors>". These give
     the day -> session -> talk skeleton: ordering, per-talk times, status
     (Invited/Tutorial/Withdrawn) and presiders.

  2. Abstract pages (one per non-withdrawn talk), in a typical conference's
     abstract-page geometry: a bold "Final ID:" line, a bold title, an italic author band with
     superscript affiliation markers and the speaker underlined, a numbered
     affiliation list, then a bold "Abstract (35 Word Limit):" body. These give
     the authoritative title, full author list, per-author affiliations, the
     numbered institution list, the abstract text and the underlined speaker.

The two regions are joined on the Final ID (the session-relative talk code,
e.g. "TM1A.2"), which appears in both.

Output (next to this script):
    conference_data.json
"""

from __future__ import annotations

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
# Hard-coded configuration — the single PDF input lives under data/; the JSON
# output stays in the script directory (where the downstream builder expects it).
# -----------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
INPUT_PDF = DATA_DIR / "TEST2026_Program_Abstracts.pdf"
OUTPUT_JSON = SCRIPT_DIR / "conference_data.json"

# Optional curator credit shown at the bottom of the app's About section as
# "<conference> curated by <name, affiliation>" (the name/affiliation links to
# `link` when one is given). Leave name empty (or set CURATOR = None) to omit
# the line entirely — the builder simply skips it when there is no curator.
CURATOR = {
    "name":        "Ada Lovelace",
    "affiliation": "Analytical Engine Society",
    "link":        "https://example.org/curator",
}

DATA_DIR.mkdir(parents=True, exist_ok=True)


def _bootstrap_pdfplumber() -> None:
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        print("[setup] Installing pdfplumber…")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install",
             "--quiet", "pdfplumber>=0.10"])


# =============================================================================
# PART A — Abstract-page parsing (geometry based).
#
# This is the SAME geometry-driven approach a real conference's processor uses: cluster
# words into baseline rows by their `top`, treat a digits-only row sitting a few
# points above a name row as that name's affiliation-marker superscripts, map
# underlines onto the names they sit beneath to find the speaker, and read the
# numbered institution list + abstract body that follow.
# =============================================================================
INST_RE = re.compile(r"^(\d+)\.\s")
ABS_RE = re.compile(r"^Abstract\s*\([^)]*\):\s*(.*)$", re.DOTALL)
FID_RE = re.compile(r"^Final\s+ID:\s+(\S+)")
SUPER_TOKEN_RE = re.compile(r"^[\d,]+$")


def cluster_rows(words: list[dict], y_tol: float = 3.0) -> list[dict]:
    """Group extract_words output by approximate `top`. The tolerance keeps the
    size-10 letters together with size-12 punctuation on the same baseline while
    leaving the superscript numerals (a few points higher) in their own row."""
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
        r["text"] = " ".join(w["text"] for w in r["words"])
    return rows


def merge_inline_supersub(rows: list[dict]) -> list[dict]:
    """Fold orphan inline superscript/subscript rows (e.g. the exponent in a
    '<sup>…</sup>' run that pdfplumber lifted onto its own row) back into the
    host line they sit inside, purely by geometry. Only safe on the title and
    post-author (institution/abstract) regions, never across the author band
    where the affiliation-marker digits legitimately live on their own rows."""
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
        words = sorted(r["words"] + merged_into.get(ri, []),
                       key=lambda w: w["x0"])
        parts: list[str] = []
        for k, w in enumerate(words):
            if k > 0 and (w["x0"] - words[k - 1]["x1"]) <= 0.2:
                parts.append(w["text"])
            else:
                parts.append((" " if k > 0 else "") + w["text"])
        out.append({"top": r["top"], "words": words,
                    "text": "".join(parts).strip()})
    return out


def _join_text(rows: list[dict]) -> str:
    return " ".join(r["text"] for r in rows)


def _collect_underlines(page) -> list[tuple[float, float, float]]:
    """Horizontal underline segments — pdfplumber surfaces them as 'lines' or as
    very thin rectangles, so accept both. Returns (x0, x1, y)."""
    out: list[tuple[float, float, float]] = []
    for ln in page.lines:
        y0 = ln.get("top")
        y1 = ln.get("bottom", y0)
        if y0 is None:
            continue
        if abs((y1 or y0) - y0) > 1.0:
            continue
        out.append((min(ln["x0"], ln["x1"]), max(ln["x0"], ln["x1"]), y0))
    for rc in page.rects:
        if rc.get("height", 99) >= 2.0:
            continue
        out.append((rc["x0"], rc["x1"], rc["top"]))
    return out


def _author_word_ranges(base_row: dict) -> list[tuple[float, float, str]]:
    """One (x0, x1, name) per author on a baseline row. An author ends at a word
    terminating in ';'."""
    out: list[tuple[float, float, str]] = []
    cur: list[dict] = []
    for w in base_row["words"]:
        cur.append(w)
        if w["text"].endswith(";"):
            out.append((min(c["x0"] for c in cur),
                        max(c["x1"] for c in cur),
                        " ".join(c["text"] for c in cur).rstrip(";").strip()))
            cur = []
    if cur:
        out.append((min(c["x0"] for c in cur),
                    max(c["x1"] for c in cur),
                    " ".join(c["text"] for c in cur).rstrip(";").strip()))
    return out


def _find_speaker_indices(underlines, author_pairs) -> list[int]:
    """Map underlines to GLOBAL author indices. An author is a speaker if an
    underline sits 8-15 pt below its baseline and covers >40% of its x-range."""
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


_SUPER_GAP_PT = 14.0


def _group_supers_x(words: list[dict]) -> list[tuple[float, float, str]]:
    """Cluster superscript-row tokens into (x0, x1, text) affiliation groups, by
    trailing comma and x-gap, so a single author's '1,2' stays one group while
    two adjacent authors' lone markers stay separate."""
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
        out.append((min(x["x0"] for x in g), max(x["x1"] for x in g), txt))
    return out


def _align_supers_to_names(author_pairs) -> list[tuple[str, str]]:
    """Attach each superscript group to the most recent name (in reading order)
    whose end precedes it — robust to author blocks that wrap across rows."""
    names: list[tuple[int, float, float, str]] = []
    supers: list[tuple[int, float, str]] = []
    for ridx, (super_row, base_row) in enumerate(author_pairs):
        for x0, x1, nm in _author_word_ranges(base_row):
            if nm:
                names.append((ridx, x0, x1, nm))
        for x0, _x1, tx in _group_supers_x(super_row["words"]):
            supers.append((ridx, x0, tx))

    assigned: list[list[str]] = [[] for _ in names]
    for srow, sx0, stext in supers:
        best: int | None = None
        for ni, (nrow, nx0, _nx1, _nm) in enumerate(names):
            if nrow < srow or (nrow == srow and nx0 <= sx0):
                best = ni
            else:
                break
        if best is not None:
            assigned[best].append(stext)
    out: list[tuple[str, str]] = []
    for i in range(len(names)):
        aff = ",".join(assigned[i])
        aff = re.sub(r",\s*,+", ",", aff).strip(",")
        out.append((names[i][3], aff))
    return out


def parse_abstract_page(page) -> dict | None:
    """Parse one abstract page into
    {final_id, title, pairs:[(name, affmarkers)], institutions:[str], abstract,
     speakers:[str]} — or None if the page isn't an abstract page."""
    words = page.extract_words(extra_attrs=["size", "top"])
    rows = cluster_rows(words)
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
    inst_idxs = [i for i in range(1, abs_idx) if INST_RE.match(rows[i]["text"])]
    first_inst = inst_idxs[0] if inst_idxs else abs_idx

    # Walk upward from (first_inst - 1) collecting (super-row, baseline-row)
    # pairs: a digits-only row 3-8 pt above its baseline row.
    author_pairs: list[tuple[dict, dict]] = []
    i = first_inst - 1
    while i > 0:
        if i - 1 <= 0:
            break
        super_row = rows[i - 1]
        base_row = rows[i]
        gap = base_row["top"] - super_row["top"]
        if not (3 < gap < 8):
            break
        if not all(SUPER_TOKEN_RE.match(w["text"]) for w in super_row["words"]):
            break
        author_pairs.append((super_row, base_row))
        i -= 2
    author_pairs.reverse()

    title_rows = merge_inline_supersub(rows[1:i + 1])
    title = _join_text(title_rows).strip()

    pairs = _align_supers_to_names(author_pairs)

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

    speaker_idxs = _find_speaker_indices(_collect_underlines(page), author_pairs)
    speakers = [pairs[i][0] for i in speaker_idxs if 0 <= i < len(pairs)]

    return {
        "final_id": final_id,
        "title": title,
        "pairs": pairs,
        "institutions": institutions,
        "abstract": abstract,
        "speakers": speakers,
    }


# =============================================================================
# PART B — Program-schedule parsing (text based).
#
# The schedule region is plain running text. A session-header line begins with a
# time range; a talk line begins with a time range and then "<CODE>.<n>.". We
# stitch wrapped continuation lines back onto whichever record they belong to.
# =============================================================================
_TIME = r"\d{1,2}:\d{2}\s*(?:AM|PM)"
# Header: "8:00 AM-9:30 AM, Grand Ballroom, PL1. <rest>"
_HDR_RE = re.compile(
    rf"^({_TIME})\s*-\s*({_TIME}),\s*(.*?),\s*([A-Za-z0-9]+)\.\s+(.*)$")
# Talk:   "8:00 AM-8:30 AM PL1.1. <rest>"
_TALK_RE = re.compile(
    rf"^({_TIME})\s*-\s*({_TIME})\s+([A-Za-z0-9]+\.\d+)\.\s*(.*)$")


def _is_line_start(line: str) -> bool:
    return bool(_HDR_RE.match(line) or _TALK_RE.match(line))


def parse_schedule(pages_text: list[str]) -> list[dict]:
    """Return an ordered list of session dicts:
        {code, title, stype, topic, date, start, end, location,
         presider, presider_aff, talks:[{code, start, end, status, raw_rest}]}

    The schedule pages carry no explicit per-session date, so we thread the date
    forward from the only date anchor present — the cover's date line is the
    conference span, and each session block is tagged with a day inferred from
    the running order of start times (a new day begins when the clock resets to
    an earlier time than the previous session's start). For TEST 2026 that maps
    cleanly to the two-day program.
    """
    # 1) Flatten all schedule pages into logical lines, re-joining wrapped
    #    continuation lines onto their starting line.
    raw_lines: list[str] = []
    for txt in pages_text:
        for ln in txt.split("\n"):
            s = ln.strip()
            if not s:
                continue
            raw_lines.append(s)

    logical: list[str] = []
    for s in raw_lines:
        if s == "Program Schedule":
            continue
        if _is_line_start(s) or not logical:
            logical.append(s)
        else:
            logical[-1] = logical[-1] + " " + s

    # 2) Walk logical lines, building sessions and their talks.
    sessions: list[dict] = []
    cur: dict | None = None
    for line in logical:
        mh = _HDR_RE.match(line)
        if mh:
            start, end, location, code, rest = mh.groups()
            title, stype, topic, presider, presider_aff = _split_header_rest(rest)
            cur = {
                "code": code, "title": title, "stype": stype, "topic": topic,
                "start": _norm_time(start), "end": _norm_time(end),
                "location": location.strip(),
                "presider": presider, "presider_aff": presider_aff,
                "date": None, "talks": [],
            }
            sessions.append(cur)
            continue
        mt = _TALK_RE.match(line)
        if mt and cur is not None:
            tstart, tend, tcode, rest = mt.groups()
            status = ""
            if re.match(r"^Abstract\s+Withdrawn\b", rest, re.I):
                status = "Withdrawn"
            elif re.match(r"^\[Invited Talk\]", rest, re.I):
                status = "Invited"
            elif re.match(r"^\[Tutorial Talk\]", rest, re.I):
                status = "Tutorial"
            cur["talks"].append({
                "code": tcode, "start": _norm_time(tstart),
                "end": _norm_time(tend), "status": status, "raw_rest": rest,
            })
            continue
        # Any other line is ignored (cover/section noise).

    _assign_dates(sessions)
    return sessions


def _split_header_rest(rest: str) -> tuple[str, str, str, str, str]:
    """From the part after '<CODE>. ' split out (title, type, topic, presider,
    presider_aff). Presider is the trailing 'Presider: …' clause; the remaining
    comma fields are title, type, and an optional topic."""
    presider = ""
    presider_aff = ""
    m = re.search(r",\s*Presider:\s*(.*)$", rest, re.I)
    if m:
        presider_blob = m.group(1).strip()
        rest = rest[:m.start()].strip()
        # presider blob is "Name[, Affiliation]"
        parts = presider_blob.split(",", 1)
        presider = parts[0].strip()
        presider_aff = parts[1].strip() if len(parts) > 1 else ""

    fields = [f.strip() for f in rest.split(",")]
    title = fields[0] if fields else ""
    stype = fields[1] if len(fields) > 1 else ""
    topic = fields[2] if len(fields) > 2 else ""
    return title, stype, topic, presider, presider_aff


def _assign_dates(sessions: list[dict]) -> None:
    """Tag each session with a calendar date. The program runs two days; a new
    day starts whenever a session's start-of-day clock goes backwards relative
    to the previous session. The actual dates come from CONFERENCE_DATES."""
    day_idx = 0
    prev_minutes = -1
    for s in sessions:
        mins = _to_minutes(s["start"])
        if prev_minutes >= 0 and mins + 1 < prev_minutes:
            day_idx += 1
        s["date"] = CONFERENCE_DATES[min(day_idx, len(CONFERENCE_DATES) - 1)]
        prev_minutes = mins


# TEST 2026 runs on these two days (Tue/Wed). Kept here as the single date
# anchor the schedule region needs.
CONFERENCE_DATES = ["26-May-2026", "27-May-2026"]


def _norm_time(t: str) -> str:
    return re.sub(r"\s+", " ", t.strip())


def _to_minutes(t: str) -> int:
    m = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)", t.strip(), re.I)
    if not m:
        return 0
    h = int(m.group(1)) % 12
    if m.group(3).upper() == "PM":
        h += 12
    return h * 60 + int(m.group(2))


# =============================================================================
# PART C — conference name from the cover page (same anchor logic as a real conference).
# =============================================================================
_COVER_HEADER = "program schedule and abstract book"
_COVER_DATE_RE = re.compile(
    r"^[A-Z][a-z]+\.?\s+\d{1,2}\s*[-–]\s*\d{1,2},?\s+\d{4}$|"
    r"^[A-Z][a-z]+\.?\s+\d{1,2}\s*[-–]\s*[A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}$",
    re.I)


def extract_conference_name(pdf_path: Path) -> str:
    import pdfplumber
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            if not pdf.pages:
                return ""
            text = pdf.pages[0].extract_text() or ""
    except Exception as e:
        log(f"  [name] WARNING: couldn't read cover page: {e}")
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if ln.lower().startswith(_COVER_HEADER):
            for cand in lines[i + 1:]:
                if _COVER_DATE_RE.match(cand):
                    continue
                return cand
            break
    return ""


# =============================================================================
# PART D — affiliation-source extraction (consumed by build_affiliation_map.py).
# Identical approach to a real conference: pull every full-address line
# "N. <body>." out of the PDF text.
# =============================================================================
PDF_AFFIL_START = re.compile(r"^\d{1,2}\.\s+(\S.*)$")


def _pdf_to_text(pdf_path: Path) -> str:
    import pdfplumber
    parts: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n\n".join(parts)


def extract_pdf_affiliations(pdf_path: Path) -> set[str]:
    text = _pdf_to_text(pdf_path)
    out: set[str] = set()
    buf: str | None = None
    for raw in text.split("\n"):
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
            if buf.endswith("."):
                out.add(buf)
                buf = None
            else:
                buf = buf + " " + s
    if buf is not None:
        out.add(buf)
    keep = {a[:-1] for a in out if a.endswith(".") and a.count(",") >= 2}
    keep = {re.sub(r"(\w)- (\w)", r"\1-\2", a) for a in keep}
    return keep


# =============================================================================
# PART E — shared helpers + type/color registries (mirroring a real conference's
# processor so the JSON is byte-shape identical for the downstream builder).
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


def _normalize_name_case(name: str) -> str:
    """Normalize capitalisation of a personal name (handles SHOUTED surnames
    like 'Avery ISAKSSON')."""
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
            (p.capitalize() if p and p[0].isalpha() else p) for p in parts)
        out_tokens.append(lead + cased + trail)
    return " ".join(out_tokens)


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip(" *.").lower()


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
    out = []
    for e in entries:
        pal = COLOR_PALETTE.get(e["id"])
        out.append({**e, **pal} if pal else dict(e))
    return out


# Standard session/talk type taxonomy. The seven shared types; a conference only
# surfaces the ones its program actually uses (the app hides count-0 types).
SESSION_TYPE_REGISTRY = _with_colors([
    {"id": "blue",   "label": "Technical"},
    {"id": "orange", "label": "Plenary"},
    {"id": "fuchsia","label": "Tutorial"},
    {"id": "teal",   "label": "Poster"},
    {"id": "rose",  "label": "Event"},
])

TALK_TYPE_REGISTRY = _with_colors([
    {"id": "orange",  "label": "Plenary"},
    {"id": "indigo",  "label": "Invited"},
    {"id": "sky",     "label": "Contributed"},
    {"id": "fuchsia", "label": "Tutorial"},
    {"id": "teal",    "label": "Poster"},
    {"id": "rose",   "label": "Event"},
])


def classify_session_color(session_type: str, session_title: str = "") -> str:
    s = (session_type or "").strip().lower()
    title = (session_title or "").strip().lower()
    tokens = s.split()
    if "plenary" in title or "plenary" in s:        return "orange"
    if "poster" in title or "poster" in s:          return "teal"
    if "postdeadline" in title or "postdeadline" in s: return "blue"
    if "symposi" in s:                              return "blue"
    if "a&t" in s or "fs" in tokens or "s&i" in s:  return "blue"
    return "rose"


def classify_talk_color(talk_title: str, session_title: str,
                        session_type: str) -> str:
    tt = (talk_title or "").lower()
    st = (session_title or "").lower()
    stype = (session_type or "").lower()
    if "plenary" in st or "plenary" in stype:           return "orange"
    if "tutorial talk" in tt:                           return "fuchsia"
    if "short course" in tt or "short course" in stype: return "fuchsia"
    if "invited talk" in tt:                            return "indigo"
    if "symposi" in stype:                              return "indigo"
    if "poster" in st or "poster" in stype:             return "teal"
    return "sky"


# =============================================================================
# Source-agnostic emission helpers (identical shapes to a real conference's processor).
# =============================================================================
def _structured_authors(affil_map: str) -> list[dict]:
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
    clean_bodies = [c.strip() for c in (insts_clean or "").split(";")
                    if c.strip()]
    out: list[dict] = []
    for i, body in enumerate(insts_detailed):
        body = (body or "").strip()
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


def _shorten_inst_body(body: str) -> str:
    """A cheap 'cleaner variant' for an institution body: the first comma-field
    that looks like an institution (contains 'Univ'/'Institute'/'Laborator'/…),
    else the longest comma-field. Only used to populate alt_names so the
    downstream affiliation map has a hint, mirroring how a real conference passes
    the CSV's cleaner institution strings."""
    fields = [f.strip() for f in body.split(",") if f.strip()]
    if not fields:
        return ""
    ke = ("univ", "institut", "laborator", "national", "academy", "college",
          "technolog", "corporation", "gmbh", "inc", "ltd", "center", "centre",
          "company", "research")
    for f in fields:
        low = f.lower()
        if any(k in low for k in ke):
            return f
    return max(fields, key=len)


# =============================================================================
# PART F — assemble the final conference_data.json.
# =============================================================================
def build_conference_data(conference_name: str,
                          schedule_sessions: list[dict],
                          abstracts: dict[str, dict],
                          pdf_affiliation_lines: list[str]) -> dict:
    sessions: dict[str, dict] = {}
    talks: list[dict] = []

    n_with_abstract = 0
    presider_aff_strings: set[str] = set()
    institution_strings: set[str] = set()

    for s in schedule_sessions:
        code = s["code"]
        presider = _normalize_name_case(s["presider"]) if s["presider"] else ""
        presider_aff = s["presider_aff"]
        if presider_aff:
            presider_aff_strings.add(presider_aff)

        sessions[code] = {
            "id":           code,
            "title":        s["title"],
            "date":         s["date"],
            "location":     s["location"],
            "presider":     presider,
            "presider_aff": presider_aff,
            "details":      "",
            "start_ts":     parse_dt(s["date"], s["start"]),
            "end_ts":       parse_dt(s["date"], s["end"]),
            "color":        classify_session_color(s["stype"], s["title"]),
            "talk_ids":     [],
        }
        _tags = _session_tags(s["stype"], s["topic"], code)
        if _tags:
            sessions[code]["tags"] = _tags

        for t in s["talks"]:
            fid = t["code"]
            status = t["status"]
            withdrawn = (status == "Withdrawn")
            sessions[code]["talk_ids"].append(fid)

            entry = abstracts.get(fid)
            if entry:
                n_with_abstract += 1
                pairs = [tuple(p) for p in entry["pairs"]]
                authors = [_normalize_name_case(n) for n, _ in pairs]
                affil_map = "; ".join(
                    (f"{_normalize_name_case(n)}={a}" if a
                     else _normalize_name_case(n)) for n, a in pairs)
                inst_bodies = list(entry["institutions"])  # "N. body."
                abstract_text = entry["abstract"]
                title = entry["title"]
                speakers_pdf = [_normalize_name_case(x) for x in entry["speakers"]]
            else:
                # No abstract page (e.g. a withdrawn talk). Use the schedule's
                # title (stripped of its status tag) and leave authors empty.
                pairs = []
                authors = []
                affil_map = ""
                inst_bodies = []
                abstract_text = ""
                title = _schedule_title_only(t["raw_rest"], status)
                speakers_pdf = []

            for b in inst_bodies:
                body = re.sub(r"^\d+\.\s*", "", b).rstrip(".").strip()
                if body:
                    institution_strings.add(body)

            # Build a status string the same way a real conference's tags read.
            status_tags: list[str] = []
            if status == "Invited":
                status_tags.append("Invited")
            elif status == "Tutorial":
                status_tags.append("Tutorial")
            if withdrawn:
                status_tags.append("Withdrawn")

            # Speaker priority: PDF underline > first author.
            if speakers_pdf:
                speaker = speakers_pdf[0]
            elif authors:
                speaker = authors[0]
            else:
                speaker = ""

            speaker_pos = -1
            if speaker and authors:
                tgt = _norm_name(speaker)
                for i, a in enumerate(authors):
                    if _norm_name(a) == tgt:
                        speaker_pos = i
                        break

            first_a = authors[0] if authors else ""
            last_a = authors[-1] if authors else ""
            same_a = (first_a == last_a)

            # institutions: detailed PDF bodies + a cleaner variant in alt_names.
            clean_variants = "; ".join(
                _shorten_inst_body(re.sub(r"^\d+\.\s*", "", b).rstrip("."))
                for b in inst_bodies)
            structured_authors = _structured_authors(affil_map)
            institutions = _structured_institutions(inst_bodies, clean_variants)

            talks.append({
                "id":            fid,
                "session_id":    code,
                "title":         title,
                "number":        fid,
                "start_ts":      parse_dt(s["date"], t["start"]),
                "end_ts":        parse_dt(s["date"], t["end"]),
                "presenter":     speaker,
                "speaker":       speaker,
                "speaker_pos":   speaker_pos,
                "authors":       structured_authors,
                "author_aliases": _author_aliases(structured_authors),
                "institutions":  institutions,
                "institutions_may_dedup": False,
                "abstract":      abstract_text,
                "status":        "; ".join(status_tags),
                "withdrawn":     withdrawn,
                "first_author":  first_a,
                "last_author":   "" if same_a else last_a,
                # classify_talk_color keys off the literal "[Invited Talk]" /
                # "[Tutorial Talk]" bracket tag. The abstract pages carry only
                # the clean title (the tag lives on the schedule line), so we
                # re-prepend the tag for classification while STORING the clean
                # title above (status carries the tag, exactly as a real conference does).
                "color":         classify_talk_color(
                                     _tag_for(status) + title,
                                     sessions[code]["title"],
                                     s["stype"]),
                "location":      sessions[code]["location"],
            })

    log(f"[build] {len(sessions)} sessions, {len(talks)} talks")
    log(f"[build]   talks with an abstract page: {n_with_abstract}/{len(talks)}")

    # Pool every affiliation source into one flat, de-duplicated, sorted list for
    # the builder's affiliation map. Full-address lines are kept whole; the
    # presider affiliations and institution bodies may be ';'-joined lists, so
    # split them here at the source.
    affiliation_pool: set[str] = set(pdf_affiliation_lines or [])
    for _v in list(presider_aff_strings) + list(institution_strings):
        for _piece in _v.split(";"):
            _p = _piece.strip()
            if _p:
                affiliation_pool.add(_p)

    data = {
        "conference_name": conference_name or "",
        "sessions": sorted(sessions.values(), key=lambda s: (s["start_ts"] or "")),
        "talks":    sorted(talks, key=lambda t: (t["start_ts"] or "")),
        "session_types": SESSION_TYPE_REGISTRY,
        "talk_types":    TALK_TYPE_REGISTRY,
        "affiliation_sources": sorted(affiliation_pool),
    }

    # Optional curator credit. Only emit the block when a non-empty name is
    # configured; otherwise leave the JSON as-is and the app shows just the
    # app-author attribution.
    if CURATOR and (CURATOR.get("name") or "").strip():
        data["curator"] = {
            "name":        (CURATOR.get("name") or "").strip(),
            "affiliation": (CURATOR.get("affiliation") or "").strip(),
            "link":        (CURATOR.get("link") or "").strip(),
        }
        log(f"[build]   curator: {data['curator']['name']!r}")

    return data


def _tag_for(status: str) -> str:
    """Reconstruct the bracket tag classify_talk_color keys off of."""
    if status == "Invited":
        return "[Invited Talk] "
    if status == "Tutorial":
        return "[Tutorial Talk] "
    return ""


def _schedule_title_only(raw_rest: str, status: str) -> str:
    """For a talk with no abstract page, recover a title from the schedule line.
    Withdrawn talks read 'Abstract Withdrawn' (we surface an empty title and let
    the withdrawn flag carry the meaning, matching a real conference)."""
    if status == "Withdrawn":
        return ""
    rest = raw_rest
    rest = re.sub(r"^\[(?:Invited|Tutorial) Talk\]\s*", "", rest, flags=re.I)
    return rest.strip()


# =============================================================================
# Main — wire the pieces together and write conference_data.json.
# =============================================================================
def _session_tags(fmt: str, topic: str, sid: str) -> list[dict]:
    """Build a session's ordered ``tags`` list ({"key", "value"} pairs) directly
    from its format/topic, shown in the app as "Key: Value · Key: Value".
    Redundant topics are dropped: empty, identical to the session id, or merely
    restating the format. Returns [] when there is nothing to show."""
    fmt = (fmt or "").strip()
    topic = (topic or "").strip()
    tags: list[dict] = []
    if fmt:
        tags.append({"key": "Session Type", "value": fmt})
    tl, fl = topic.casefold(), fmt.casefold()
    redundant = (
        not topic
        or tl == str(sid).casefold()
        or (bool(fl) and (tl == fl or tl.startswith(fl)))
    )
    if not redundant:
        head = topic.split(":", 1)[0].strip()
        if ":" in topic and head and " " not in head:
            k, v = topic.split(":", 1)
            tags.append({"key": k.strip(), "value": v.strip()})
        else:
            tags.append({"key": "Session Topic", "value": topic})
    return tags


def main() -> None:
    log("=" * 72)
    log("[config] TEST 2026 PROCESSOR starting up.")
    log(f"[config]   script dir : {SCRIPT_DIR}")
    log(f"[config]   data dir   : {DATA_DIR}")
    log(f"[config]   input PDF  : {INPUT_PDF}")
    log(f"[config]   JSON out   : {OUTPUT_JSON}")
    log("=" * 72)

    if not INPUT_PDF.exists():
        raise SystemExit(
            f"[fatal] Input PDF not found: {INPUT_PDF}\n"
            f"        Run fetch_program_test2026.py first — it stages the "
            f"committed source PDF into data/ for the processor to read.")

    _bootstrap_pdfplumber()
    import pdfplumber

    log("[1/4] Reading conference name from the cover page…")
    conference_name = extract_conference_name(INPUT_PDF)
    log(f"  conference name: {conference_name!r}")

    log("[2/4] Parsing schedule pages + abstract pages from the PDF…")
    schedule_pages_text: list[str] = []
    abstracts: dict[str, dict] = {}
    with pdfplumber.open(str(INPUT_PDF)) as pdf:
        n = len(pdf.pages)
        log(f"  PDF has {n} pages.")
        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            if text.startswith("Final ID:"):
                try:
                    entry = parse_abstract_page(page)
                except Exception as e:
                    log(f"  page {i}: abstract parse error {e!s}")
                    entry = None
                if entry:
                    abstracts[entry["final_id"]] = entry
            elif i > 1:  # skip the cover; everything else pre-abstracts is schedule
                schedule_pages_text.append(text)
    log(f"  abstract pages parsed: {len(abstracts)}")

    sessions = parse_schedule(schedule_pages_text)
    n_talks = sum(len(s["talks"]) for s in sessions)
    log(f"  schedule sessions parsed: {len(sessions)} ({n_talks} talks)")

    log("[3/4] Extracting full-address affiliation lines from the PDF…")
    pdf_affil_lines = sorted(extract_pdf_affiliations(INPUT_PDF))
    log(f"  affiliation lines: {len(pdf_affil_lines)}")

    log("[4/4] Bundling everything into conference_data.json…")
    data = build_conference_data(
        conference_name=conference_name,
        schedule_sessions=sessions,
        abstracts=abstracts,
        pdf_affiliation_lines=pdf_affil_lines,
    )

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    size_kb = OUTPUT_JSON.stat().st_size / 1024
    log(f"[json] Wrote {OUTPUT_JSON} ({size_kb:,.1f} KB) — "
        f"{len(data['sessions'])} sessions, {len(data['talks'])} talks")

    print(flush=True)
    print("=" * 72, flush=True)
    print("DONE (process only).", flush=True)
    print(f"  data dir     : {DATA_DIR}", flush=True)
    print(f"  bundled JSON : {OUTPUT_JSON}", flush=True)
    print("=" * 72, flush=True)


if __name__ == "__main__":
    main()
