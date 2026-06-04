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

"""process_program_ecio2026.py — PROCESS ONLY.

The "processor" half of the ECIO 2026 pipeline. Reads ONLY what fetch put into
data/ (no network), and emits a clean conference_data.json next to itself.

Inputs (under data/):
    ECIO26_DetailedSchedule.pdf   the wide A3 grid of every session/talk
    ECIO26_Concise.pdf            one-page program-overview (currently used only
                                  as a cross-check; the skeleton below is the
                                  authoritative session list)

ECIO publishes no abstract book and no per-talk page, so this processor cannot
recover full author lists, affiliations, or abstracts. Each talk carries only
its title and a single presenting-author name (what the schedule grid prints).

Strategy
--------
The schedule PDF is one wide page laid out as a vertical sequence of day blocks.
Each day block is a TIME x ROOM grid: the leftmost column holds the time-slot
labels (e.g. "0830-0845") and the next three columns hold the parallel-room
cells, one per session-track (HG F1 / HG E1.1 / HG E1.2). A cell is one talk:
title text on the left, speaker name right-aligned at the cell's right edge,
separated by a visible gap. We parse this geometry directly.

The day-level structure (sessions, time blocks, rooms, types) is *discovered*
at runtime from the PDF — see _discover_skeleton. There is no hand-curated
list in this file; every session, every room, every day-key-to-ISO-date map
is derived from the PDF text. The processor's job after discovery is to
populate each track session with the talks the PDF actually prints under it.

For non-track items (Plenary, Workshop panels, Industry Talks, Poster sessions,
ceremonies, social events) we recognise their characteristic cell text
patterns (e.g. "Plenary Session N", "Industry Talk Session N: …",
"Poster Blitz 1.1", "Welcome Reception") and emit them as sessions in their
own right. The talks under Workshops and Industry Talk sessions also come
straight from the PDF — those cells don't use the wide title-vs-speaker
x-gap of the tech grid; they pack "Title. Speaker, Affiliation" into a single
run of words. We parse that run with _harvest_block_cells (see below).

Plenary lectures are the one place where the PDF prints only the lecturer's
name on a meta-row, with no extractable talk title. Discovery emits a single
"Plenary Lecture" placeholder talk per plenary session; when the cached
plenary-speakers HTML is present, that placeholder is upgraded in-place
with the real title, abstract, and bio at emission time.

Session titles come from the PDF wherever the PDF renders one: the topic
words above each tech-track column (e.g. "<Track Title>"), the
long "WORKSHOP N: …" headers, and the "Industry Talk Session N: …" headers
all sit at a known Y in a known column and we read them off. For sessions
the PDF has no explicit header for (ceremonies, lunches, social events)
discovery synthesises a fixed generic label from the row's classifier
("Opening Ceremony", "Welcome Reception", "Coffee + Poster Session N", …).

Output:
    conference_data.json   schema documented in docs/CONFERENCE_JSON.md
"""

from __future__ import annotations

import difflib
import html
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path


def log(msg: str) -> None:
    print(msg, flush=True)


SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
INPUT_PDF = DATA_DIR / "ECIO26_DetailedSchedule.pdf"
# Agenda-of-Sessions PDF: a clean, bordered one-table-per-day overview that
# carries two things the wide detailed-schedule grid renders only patchily —
# the LOCATION of every non-talk event (Registration desk, coffee/lunch foyers,
# the Welcome Reception / Gala venues, the Student-event rooms, …) and the room
# column each session block sits under. It's `required: no`; when present the
# processor uses it to (a) fill the `location` of any session the detailed PDF
# left without one and (b) add the daily logistics rows (Registration, Coffee
# Break, Lunch) the grid omits. See `_parse_agenda_pdf` / `_load_agenda`.
INPUT_AGENDA_PDF = DATA_DIR / "2026_ecio_agenda_of_sessions.pdf"
INPUT_INVITED_HTML = DATA_DIR / "ECIO26_InvitedSpeakers.html"
# Optional web-enrichment HTML pages (all under data/, all `required: no` in
# data_requirements_ecio2026.txt). Each adds detail the detailed-schedule PDF
# doesn't render; the processor uses what's there and falls back when any is
# missing. See `_load_web_enrichment` for how each is wired in.
INPUT_PLENARY_HTML  = DATA_DIR / "ECIO26_PlenarySpeakers.html"
INPUT_WORKSHOPS_HTML = DATA_DIR / "ECIO26_Workshops.html"
INPUT_STUDENT_HTML  = DATA_DIR / "ECIO26_StudentEvent.html"
INPUT_INDUSTRY_HTML = DATA_DIR / "ECIO26_IndustryTalks.html"
INPUT_SOCIAL_HTML   = DATA_DIR / "ECIO26_SocialEvents.html"
INPUT_LABS_HTML     = DATA_DIR / "ECIO26_LabTours.html"
# Optica schedule day pages. The public ECIO program is also published on
# Optica's event site as a per-day schedule whose "Detailed View" cells carry
# the full author list (with affiliations) and the abstract for every talk —
# neither of which the detailed-schedule PDF renders. These three files (one
# per conference day) are the richest content source we have; the processor
# cross-references them by session code + talk title to fill author lists and
# abstracts on the PDF-derived oral talks, and to populate the poster sessions
# the PDF leaves empty (the wide grid lists no individual posters). All three
# are `required: no` — each is optional and the processor falls back to the
# PDF-only harvest when any is absent. See `_load_optica_enrichment`.
INPUT_OPTICA_HTML = [
    DATA_DIR / "ECIO26_OpticaMonday.html",
    DATA_DIR / "ECIO26_OpticaTuesday.html",
    DATA_DIR / "ECIO26_OpticaWednesday.html",
]
# the conference planner planner DOM (outerHTML) captured by the fetcher after every day,
# session, and "See More…" control has been expanded. This is the only ECIO
# source that publishes the per-session PRESIDER(s); the detailed-schedule PDF
# and the Optica pages don't render them. The processor keys this by the same
# session code the PDF uses (M1B, T2A, W3B, …) and attaches presider +
# affiliation to the matching session. `required: no` — when the file is
# absent the sessions simply carry no presider. See `_parse_planner_presiders`.
INPUT_PLANNER_HTML = DATA_DIR / "ECIO26_planner_expanded.html"
OUTPUT_JSON = SCRIPT_DIR / "conference_data.json"


def _bootstrap_pdfplumber() -> None:
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        log("[setup] Installing pdfplumber…")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install",
             "--quiet", "pdfplumber>=0.10"])


def _bootstrap_bs4() -> None:
    try:
        import bs4  # noqa: F401
    except ImportError:
        log("[setup] Installing beautifulsoup4…")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install",
             "--quiet", "beautifulsoup4>=4.10"])


# =============================================================================
# Conference name
# =============================================================================
CONFERENCE_NAME = "ECIO 2026"

# Curator credit shown at the bottom of the About section in the built app.
# Schema (per CONFERENCE_JSON.md): {name, affiliation?, link?}. Leave `name`
# empty (or set CURATOR = None) to omit the curator line entirely.
CURATOR = {
    "name": "Dmitry Kazakov",
    "affiliation": "AyLight AG",
    "link": "https://aylight.io/",
}

# Day-key / ISO-date map, the parallel-session room labels per column, and the
# plenary room label all derive from the PDF at runtime (see the discovery
# helpers below). Nothing about the program is hardcoded here.

# Standard session/talk type taxonomy. The shared types; a conference only
# surfaces the ones its program actually uses (the app hides count-0 types).
SESSION_TYPES = [
    {"id": "blue",   "label": "Technical",
     "fg": "#2563eb", "bg_light": "#e8efff", "bg_dark": "#1a233d"},
    {"id": "orange", "label": "Plenary",
     "fg": "#ea580c", "bg_light": "#ffedd5", "bg_dark": "#3b1d0a"},
    {"id": "teal",   "label": "Poster",
     "fg": "#0d9488", "bg_light": "#d6f3ef", "bg_dark": "#102b27"},
    {"id": "rose",   "label": "Event",
     "fg": "#e11d48", "bg_light": "#ffe1e8", "bg_dark": "#38161f"},
]
TALK_TYPES = [
    {"id": "orange", "label": "Plenary",
     "fg": "#ea580c", "bg_light": "#ffedd5", "bg_dark": "#3b1d0a"},
    {"id": "indigo", "label": "Invited",
     "fg": "#4f46e5", "bg_light": "#e6e4ff", "bg_dark": "#1d1a3d"},
    {"id": "sky",    "label": "Contributed",
     "fg": "#0284c7", "bg_light": "#e0f2fe", "bg_dark": "#0c2a3d"},
    {"id": "teal",   "label": "Poster",
     "fg": "#0d9488", "bg_light": "#d6f3ef", "bg_dark": "#102b27"},
    {"id": "rose",   "label": "Event",
     "fg": "#e11d48", "bg_light": "#ffe1e8", "bg_dark": "#38161f"},
]


# =============================================================================
# Skeleton discovery
#
# The session skeleton (days, rooms, ordered session list) is discovered
# from the PDF at runtime by _discover_skeleton (see below). The discoverer
# returns a list of dicts in the legacy SKELETON shape — see the field
# reference in _build_session_dict's docstring for what each key means.
# The main loop iterates over those dicts identically to how it iterated
# the literal list before; no content from the program lives in this file.
# =============================================================================

# =============================================================================
# PDF parsing: row-bucket the words, locate day Y bands, harvest column cells.
# =============================================================================

# Column X-ranges in the detailed PDF. The grid has session-room cells centred
# at x≈216 / 565 / 911 (from the "Session Rooms ->" header). Speakers are
# right-aligned to ~378 / 742 / 1065. The boundaries below sit comfortably in
# the inter-column gaps so a word's midpoint deterministically picks one column,
# including the long invited speakers (e.g. a long three-part name) whose last
# token straddles the visual seam.
COL_X_RANGES = {
    1: (55.0, 415.0),
    2: (415.0, 770.0),
    3: (770.0, 1100.0),
}
TIME_X_RANGE = (15.0, 55.0)  # left-edge time-slot column

# A row is "the same line" if its top differs by at most this. The schedule
# sometimes baselines a speaker chip 2-3pt below its title (especially for
# italic names rendered in a tighter font), so the tolerance has to clear that
# small offset without merging adjacent time-slot rows (gap >= 5pt).
ROW_TOL = 3.5
# A speaker is split from a title when the words inside a row have an x-gap
# of at least this many points between them. Cells are narrow enough that
# 13pt is a clean separator — normal word-to-word gaps inside titles are
# 2-6pt, and hyphenated compounds carry NO internal space (pdfplumber emits
# "Single-Photon" as one word). The smallest title→speaker gap we measured
# in the ECIO 2026 PDF was ~13.9pt (a representative title-then-speaker row),
# so the threshold sits just under that.
SPEAKER_GAP_PT = 13.0
# The session-track topic header above each block is rendered in a slightly
# larger font (4.56pt) than talk text (4.08pt). Used to filter topic words out
# when harvesting talk content.
TOPIC_FONT_MIN = 4.4

# Patterns that mark a "row" as a non-talk break (coffee, lunch, plenary
# announcements, etc.) when they appear inside what would otherwise be a track
# session's Y band. The schedule PDF lays these out as full-width rows that
# bleed slightly into the column we're harvesting — drop them outright.
NON_TALK_PREFIXES = (
    "Coffee", "Lunch", "Welcome", "Closing", "Opening", "Plenary",
    "Industry Talks", "Industry Talk", "Workshop", "Poster Blitz",
    "Panel Discussion", "Gala", "Networking", "Bench to Business",
    "Student Workshop", "Zurich City", "Lab Tours", "Registration",
    "Exhibition", "Session Rooms",
)

TIME_RE = re.compile(r"^\d{4}-\d{4}$")
DAY_RE = re.compile(
    r"^(SUNDAY|MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY)$"
)
TRACK_LABEL_RE = re.compile(r"^[MTW][1-3][A-C]$")


def _hhmm_to_minutes(hhmm: str) -> int:
    """Convert 'HH:MM' or 'HHMM' to minutes-since-midnight."""
    s = hhmm.replace(":", "")
    return int(s[:2]) * 60 + int(s[2:])


def _extract_words(pdf_path: Path) -> list[dict]:
    import pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        words = page.extract_words(
            keep_blank_chars=False,
            use_text_flow=False,
            extra_attrs=["size", "fontname"],
        )
    # pdfplumber returns floats as strings sometimes; normalise.
    out: list[dict] = []
    for w in words:
        out.append({
            "text": w["text"],
            "x0": float(w["x0"]),
            "x1": float(w["x1"]),
            "top": float(w["top"]),
            "size": float(w.get("size", 0.0) or 0.0),
        })
    return out


def _cluster_rows(words: list[dict], tol: float = ROW_TOL) -> list[dict]:
    """Cluster words by `top` into baseline rows. Chaining is transitive on the
    sorted stream: each new word merges into the current row when its top is
    within `tol` of the *most recently added* word's top. This lets a title at
    y=268.7 chain together with a tracked italic name whose letters sit on
    y=266.3 (above) and y=271.3 (below) — the kind of split baseline a few of
    the longer invited-speaker chips use in the schedule grid."""
    if not words:
        return []
    sw = sorted(words, key=lambda w: (w["top"], w["x0"]))
    rows: list[dict] = []
    for w in sw:
        if rows and (w["top"] - rows[-1]["last_top"]) <= tol:
            rows[-1]["words"].append(w)
            rows[-1]["last_top"] = w["top"]
        else:
            rows.append({"last_top": w["top"], "words": [w]})
    for r in rows:
        r["words"].sort(key=lambda w: w["x0"])
        tops = sorted(w["top"] for w in r["words"])
        # `top` = the median word baseline. Using the median (not the min)
        # keeps a long row anchored to its bulk text even when a handful of
        # words sit on a slightly different baseline (e.g. a tracked italic
        # name whose letters render 5pt below the title's baseline). That bulk
        # baseline is what _talk_time_window matches against slot anchors.
        r["top"] = tops[len(tops) // 2]
    return rows


def _day_y_bands(rows: list[dict], page_h: float) -> dict[str, tuple[float, float]]:
    """Return {day_key: (y_top, y_bottom)} for each weekday header found.

    Day headers in the detailed PDF appear as a two-word run "<WEEKDAY>, JUNE",
    rendered in a noticeably larger font (~4.92pt) than talk text. We locate
    each such header's Y and treat the day's vertical band as everything from
    that Y down to the next day's Y (or the page bottom for the last day).
    """
    found: list[tuple[float, str]] = []
    for r in rows:
        # A row is a day header if it contains one of the WEEKDAY tokens at
        # the larger font size (4.6+).
        for w in r["words"]:
            t = w["text"].rstrip(",").upper()
            if DAY_RE.match(t) and w["size"] >= 4.4:
                key = {
                    "SUNDAY": "sun",
                    "MONDAY": "mon",
                    "TUESDAY": "tue",
                    "WEDNESDAY": "wed",
                }.get(t)
                if key:
                    found.append((r["top"], key))
                    break
    found.sort()
    bands: dict[str, tuple[float, float]] = {}
    for i, (y, key) in enumerate(found):
        y_end = found[i + 1][0] if i + 1 < len(found) else page_h
        bands[key] = (y, y_end)
    return bands


def _row_in_band(row: dict, band: tuple[float, float]) -> bool:
    return band[0] <= row["top"] <= band[1]


# =============================================================================
# Runtime discovery of days, rooms, and the session skeleton
#
# Everything below replaces the hand-curated DAYS dict, ROOM_COL constants,
# and SKELETON list that used to live near the top of this file. The three
# helpers _discover_day_isos, _discover_room_cols, and _discover_skeleton
# extract that structure from the PDF on each run so no conference content
# is embedded in this source file. All of them work
# off the already-clustered `rows` list and the raw `words` list.
# =============================================================================

_DAY_KEY_BY_WEEKDAY = {
    "SUNDAY": "sun", "MONDAY": "mon", "TUESDAY": "tue", "WEDNESDAY": "wed",
    "THURSDAY": "thu", "FRIDAY": "fri", "SATURDAY": "sat",
}
_MONTH_BY_NAME = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "MAY": 5,
    "JUNE": 6, "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10,
    "NOVEMBER": 11, "DECEMBER": 12,
}
_CONF_YEAR_RE = re.compile(r"(20\d{2})")


def _conference_year(name: str) -> int:
    """Pull the four-digit conference year out of CONFERENCE_NAME, falling
    back to the current calendar year if no year token is present."""
    m = _CONF_YEAR_RE.search(name)
    if m:
        return int(m.group(1))
    import datetime as _dt
    return _dt.date.today().year


def _discover_day_isos(
    rows: list[dict], conf_year: int
) -> dict[str, str]:
    """Return {day_key: 'YYYY-MM-DD'} for every day-header row found in the
    PDF. A day header is a single PDF row containing a WEEKDAY token, a
    MONTH-name token, and a day-number token, all at the larger header
    font (size >= 4.4). The year is taken from CONFERENCE_NAME."""
    out: dict[str, str] = {}
    for r in rows:
        weekday = ""
        month = 0
        day_num = 0
        for w in r["words"]:
            if w["size"] < 4.4:
                continue
            t = w["text"].strip().rstrip(",").upper()
            if not weekday and t in _DAY_KEY_BY_WEEKDAY:
                weekday = _DAY_KEY_BY_WEEKDAY[t]
            elif not month and t in _MONTH_BY_NAME:
                month = _MONTH_BY_NAME[t]
            elif not day_num and t.isdigit() and 1 <= int(t) <= 31:
                day_num = int(t)
        if weekday and month and day_num:
            out[weekday] = f"{conf_year:04d}-{month:02d}-{day_num:02d}"
    return out


# Pattern for "HG <code>" tokens in the Session Rooms header row.
_ROOM_PREFIX_RE = re.compile(r"^[A-Z]{1,3}$")
_ROOM_CODE_RE = re.compile(r"^[A-Z]?\d+(?:\.\d+)?$")


def _discover_room_cols(rows: list[dict]) -> dict[int, str]:
    """Return {1: 'HG F1', 2: 'HG E1.1', 3: 'HG E1.2'} (or however many
    columns the PDF has) by reading the dedicated 'Session Rooms ->' header
    row. The row carries a prefix-then-code pair for each column; we pair
    consecutive (prefix, code) tokens whose centres fall in the same column
    x-range as the talk grid (COL_X_RANGES)."""
    header = None
    for r in rows:
        joined = " ".join(w["text"] for w in r["words"])
        if "Session Rooms" in joined:
            header = r
            break
    if header is None:
        return {}
    out: dict[int, str] = {}
    ws = sorted(header["words"], key=lambda w: w["x0"])
    i = 0
    while i < len(ws):
        w = ws[i]
        if (_ROOM_PREFIX_RE.match(w["text"])
                and i + 1 < len(ws)
                and _ROOM_CODE_RE.match(ws[i + 1]["text"])):
            mid = (w["x0"] + ws[i + 1]["x1"]) / 2
            col = None
            for c, (lo, hi) in COL_X_RANGES.items():
                if lo <= mid < hi:
                    col = c
                    break
            if col is not None and col not in out:
                out[col] = f"{w['text']} {ws[i + 1]['text']}"
            i += 2
            continue
        i += 1
    return out


# Pattern for the plenary row's cell text: "Plenary Session N, Prof. Dr. X,
# <speaker affiliation>, <room>". The speaker's affiliation sits between the
# name and the room, so the ROOM is the LAST comma-separated segment — anchoring
# (?P<room>...) to the end (and letting the optional (?P<affiliation>...) soak up
# the middle, commas and all) keeps an affiliation like "EPFL" out of the room.
# Room names ("HG F30 (Plenary Auditorium)") carry parentheses but no comma.
_PLENARY_ROW_RE = re.compile(
    r"^Plenary\s+Session\s+(?P<n>\d+)\s*,\s*"
    r"(?P<honorific>(?:Prof\.?\s+Dr\.?|Dr\.?|Prof\.?)\s+)?"
    r"(?P<speaker>[^,]+?)\s*,\s*"
    r"(?:(?P<affiliation>.*),\s*)?"
    r"(?P<room>[^,]+?)\s*$"
)


def _discover_plenary_room(rows: list[dict]) -> str:
    """Return the plenary auditorium label read from a Plenary row's room
    field (or "" if no plenary row is found)."""
    for r in rows:
        text = " ".join(w["text"] for w in r["words"])
        if "Plenary Session" not in text:
            continue
        # The cell may have leading time-slot tokens or other columns; the
        # plenary text starts at "Plenary Session". Slice from there.
        m = _PLENARY_ROW_RE.search(text[text.find("Plenary Session"):])
        if m:
            return m.group("room").strip()
    return ""


# Track-code pattern (M1A, T2B, W3C, …): single letter for the day, single
# digit for the slot block within the day, single letter for the column.
_TRACK_CODE_RE = re.compile(r"^([MTW])([1-9])([A-C])$")
# Per-conference mapping of track-code day-letter to discovered day-key.
# The mapping is induced from observed track codes: the (M, T, W) letters
# are conventional but conferences could in principle use different days,
# so we infer the assignment from co-occurrence with day Y-bands.
_TRACK_DAY_LETTERS = ("M", "T", "W")


def _column_of_x(x: float) -> int | None:
    mid = x
    for c, (lo, hi) in COL_X_RANGES.items():
        if lo <= mid < hi:
            return c
    return None


def _row_left_time_slots(words: list[dict], y: float, tol: float = ROW_TOL
                         ) -> list[tuple[int, int]]:
    """Return all (start_min, end_min) time-slot pairs sitting at the left
    edge of the page on the row centred at y. The PDF often packs 2 (or 3)
    consecutive HHMM-HHMM tokens onto one row when a tech-track session has
    no talk content in some slots."""
    out: list[tuple[int, int]] = []
    for w in words:
        if w["x0"] >= TIME_X_RANGE[1]:
            continue
        if abs(w["top"] - y) > tol + 0.5:
            continue
        if TIME_RE.match(w["text"]):
            out.append(_slot_minutes(w["text"]))
    out.sort()
    return out


def _all_slot_rows(
    words: list[dict], band: tuple[float, float]
) -> list[tuple[float, int, int]]:
    """[(top_y, start_min, end_min), …] for every HHMM-HHMM time-slot label
    in the band's left column, sorted by Y ascending. Duplicate slots in
    multi-slot rows are kept as separate entries — each carries the same Y."""
    out: list[tuple[float, int, int]] = []
    for w in words:
        if not (band[0] <= w["top"] <= band[1]):
            continue
        if w["x0"] >= TIME_X_RANGE[1]:
            continue
        if not TIME_RE.match(w["text"]):
            continue
        s, e = _slot_minutes(w["text"])
        out.append((w["top"], s, e))
    out.sort()
    return out


def _row_column_text(row: dict, col: int) -> str:
    """Return the (sorted, joined) text of the row's words whose centres
    fall inside column `col`'s x-range. Empty string for an empty column."""
    col_lo, col_hi = COL_X_RANGES[col]
    cell = [w for w in row["words"]
            if col_lo <= (w["x0"] + w["x1"]) / 2 < col_hi]
    cell.sort(key=lambda w: w["x0"])
    return _join_words(cell).strip()


def _row_left_first_token(row: dict) -> str:
    """First word text on the row whose centre sits in the left-edge time
    column (or '' if none). Used to classify rows by their time-slot label."""
    candidates = [w for w in row["words"]
                  if w["x0"] < TIME_X_RANGE[1] and TIME_RE.match(w["text"])]
    candidates.sort(key=lambda w: w["x0"])
    return candidates[0]["text"] if candidates else ""


# Row-level break/event prefixes used during discovery. Each constant is a
# tuple of substrings that, if present at the start of the right-band text
# of a time-slot row, classify the row as that kind of event.
_BREAK_PREFIXES = ("Coffee,", "Coffee ,", "Lunch,", "Lunch:,",
                   "Lunch ,", "Coffee+Poster")
_REGISTRATION_PREFIX = "Registration"


def _is_break_row(text: str) -> bool:
    """True for coffee/lunch break rows (which we skip entirely)."""
    return any(text.startswith(p) for p in _BREAK_PREFIXES
               if not p.startswith("Coffee+Poster"))


def _is_registration_row(text: str) -> bool:
    return text.startswith(_REGISTRATION_PREFIX)


def _is_topic_header_row(row: dict, band: tuple[float, float]) -> bool:
    """True when this non-time-slot row carries topic-header text for a
    tech-track block. We require:
      (a) no time-slot token in the left column (so it's not a talk row),
      (b) no left-side track-code token (track codes appear on talk rows),
      (c) at least one cell whose first token is NOT a known break-prefix,
      (d) cell text not matching the special-event patterns we recognise
          (Plenary, WORKSHOP, Industry Talk Session, Poster Blitz,
          Coffee+Poster, ceremonies, social events, lab tours).
    Topic headers can be at size 4.6 (Mon/Tue) or 4.1 (Wed) — we don't key
    on font size; we key on row-level structure.
    """
    if _row_left_first_token(row):
        return False
    # Reject day-banner rows (e.g. "MONDAY, JUNE 15").
    for w in row["words"]:
        if w["size"] >= 4.7:
            return False
    # Reject rows whose only content is a bare track code.
    non_track = [w for w in row["words"]
                 if not _TRACK_CODE_RE.match(w["text"])]
    if not non_track:
        return False
    # Inspect each column individually.
    found_any = False
    for c in (1, 2, 3):
        text = _row_column_text(row, c)
        if not text:
            continue
        if _classify_special_row(text) is not None:
            return False
        # Plain words with no time tag and no break prefix → topic-like.
        if (text and not _is_break_row(text)
                and not _is_registration_row(text)):
            found_any = True
    return found_any


# Special-event classifier: maps a column's cell text to an (event_kind,
# parsed-fields) tuple, or returns None when the cell is talk content.
# event_kind ∈ {"plenary", "industry", "workshop", "poster_blitz",
# "poster_session", "welcome", "gala", "city_tour", "opening", "closing",
# "lab_tours", "student_workshop", "bench", "pizza"}.
_INDUSTRY_HEADER_RE = re.compile(
    r"^Industry\s+Talk\s+Session\s+(?P<n>\d+)\s*:\s*(?P<label>.*)$",
    re.IGNORECASE)
_WORKSHOP_HEADER_RE = re.compile(
    r"^WORKSHOP\s+(?P<n>\d+)\s*:\s*(?P<label>.*)$")
_POSTER_BLITZ_RE = re.compile(
    r"^Poster\s+Blitz\s+(?P<n>\d+)\.(?P<sub>\d+)\b\s*(?P<label>.*)$",
    re.IGNORECASE)
_POSTER_SESSION_RE = re.compile(
    r"^Coffee\+Poster\s+Session\s+(?P<n>\d+)\s*,?\s*(?P<room>.*)$",
    re.IGNORECASE)
_OPENING_RE = re.compile(r"^Opening\s+Ceremony\s*,?\s*(?P<room>.*)$",
                         re.IGNORECASE)
_CLOSING_RE = re.compile(r"^Closing\s+Ceremony\s*,?\s*(?P<room>.*)$",
                         re.IGNORECASE)
_WELCOME_RE = re.compile(r"^Welcome\s+Reception\b\s*(?P<room>.*)$",
                         re.IGNORECASE)
_GALA_RE = re.compile(r"^Gala\s+Dinner\s*@?\s*(?P<room>.*)$",
                      re.IGNORECASE)
_CITY_TOUR_RE = re.compile(r"^Zurich\s+City\s+Tour\b\s*(?P<room>.*)$",
                           re.IGNORECASE)
_LAB_TOURS_RE = re.compile(r"^Lab\s+Tours\b.*$", re.IGNORECASE)
_STUDENT_WORKSHOP_RE = re.compile(
    r"^Student\s+Workshop\s*\(?.*?\)?\s*,?\s*(?P<room>.*)$", re.IGNORECASE)
_BENCH_RE = re.compile(
    r"^Bench\s+to\s+Business\b\s*[^,]*,?\s*(?P<room>.*)$", re.IGNORECASE)
_PIZZA_RE = re.compile(
    r"^(?:Networking\s+)?Pizza\s+Dinner\b\s*(?P<room>.*)$", re.IGNORECASE)
_NETWORKING_PIZZA_RE = re.compile(
    r"^Networking\s+Pizza\s+Dinner\b\s*(?P<room>.*)$", re.IGNORECASE)


def _classify_special_row(text: str) -> tuple[str, dict] | None:
    """Recognise a special-event cell text. Returns (kind, fields) where
    `fields` may carry `n`, `sub`, `label`, `room`, `speaker` (depending on
    kind). The first matching pattern wins. Returns None for plain talk
    cells, break rows, and registration rows."""
    s = text.strip()
    if not s:
        return None
    for pat, kind, ckey in (
        (_PLENARY_ROW_RE, "plenary", None),
        (_INDUSTRY_HEADER_RE, "industry", None),
        (_WORKSHOP_HEADER_RE, "workshop", None),
        (_POSTER_BLITZ_RE, "poster_blitz", None),
        (_POSTER_SESSION_RE, "poster_session", None),
        (_OPENING_RE, "opening", None),
        (_CLOSING_RE, "closing", None),
        (_NETWORKING_PIZZA_RE, "pizza", None),
        (_PIZZA_RE, "pizza", None),
        (_BENCH_RE, "bench", None),
        (_STUDENT_WORKSHOP_RE, "student_workshop", None),
        (_WELCOME_RE, "welcome", None),
        (_GALA_RE, "gala", None),
        (_CITY_TOUR_RE, "city_tour", None),
        (_LAB_TOURS_RE, "lab_tours", None),
    ):
        m = pat.match(s)
        if m:
            return kind, m.groupdict()
    return None


def _row_classified_events(
    row: dict, day_band: tuple[float, float]
) -> list[tuple[int, str, dict, str]]:
    """For a time-slot row, return one (column, kind, fields, text) tuple
    per column whose cell text classifies as a special event. Rows with
    only talk / break / registration content return an empty list."""
    out: list[tuple[int, str, dict, str]] = []
    for c in (1, 2, 3):
        text = _row_column_text(row, c)
        if not text:
            continue
        cls = _classify_special_row(text)
        if cls is None:
            continue
        out.append((c, cls[0], cls[1], text))
    return out


def _topic_header_columns(
    row: dict, room_by_col: dict[int, str]
) -> dict[int, str]:
    """For a topic-header row, return {column: title_text} for every column
    that has topic text. Skips columns with only a track-code label."""
    out: dict[int, str] = {}
    for c in room_by_col:
        text = _row_column_text(row, c)
        if not text:
            continue
        # The topic header text sometimes leads with a stray track label or
        # punctuation; trim known noise patterns.
        text = re.sub(r"[:\s]+$", "", text)
        if not text or _TRACK_CODE_RE.match(text):
            continue
        out[c] = text
    return out


def _row_is_session_boundary(row: dict) -> bool:
    """True for rows that mark the END of a tech-track session block: coffee
    or lunch breaks, Coffee+Poster sessions, or any special-event header
    (Poster Blitz, Industry, Workshop, ceremonies, social events). The
    slot anchored to this row is excluded from the tech-track block above."""
    for c in (1, 2, 3):
        text = _row_column_text(row, c)
        if not text:
            continue
        if _is_break_row(text):
            return True
        if text.startswith("Coffee+Poster"):
            return True
        if _classify_special_row(text) is not None:
            return True
    return False


def _next_break_or_dayend_y(
    slot_rows: list[tuple[float, int, int]], rows_in_band: list[dict],
    after_y: float
) -> float:
    """Return the Y of the first 'session-ending' row at or after `after_y`,
    or +inf when there is none in the same day band."""
    for r in rows_in_band:
        if r["top"] <= after_y:
            continue
        if _row_is_session_boundary(r):
            return r["top"]
    return float("inf")


def _tech_block_slot_window(
    slot_rows: list[tuple[float, int, int]], rows_in_band: list[dict],
    start_y: float, max_y: float,
) -> tuple[int, int] | None:
    """Return (start_min, end_min) for the contiguous run of talk-content
    time-slot rows starting just below `start_y` (topic-header Y) and
    extending until we hit a row whose Y is at or past `max_y` (next topic
    header) or a row classified as a session boundary (break or special
    event). Slots co-located with the boundary row are EXCLUDED.

    Returns None when the run is empty (no usable slot rows)."""
    sorted_slots = sorted(slot_rows, key=lambda t: t[0])
    keep: list[tuple[int, int]] = []
    for top, s, e in sorted_slots:
        if top <= start_y:
            continue
        if top >= max_y:
            break
        row = _row_at_y(rows_in_band, top)
        if row is not None and _row_is_session_boundary(row):
            break
        keep.append((s, e))
    if not keep:
        return None
    return min(k[0] for k in keep), max(k[1] for k in keep)


def _topic_header_rows_in_band(
    rows: list[dict], band: tuple[float, float]
) -> list[dict]:
    """Topic-header rows inside a day's band, in Y order. Excludes the
    day-banner row itself (size >= 4.7)."""
    out = [r for r in rows
           if band[0] < r["top"] <= band[1]
           and _is_topic_header_row(r, band)]
    out.sort(key=lambda r: r["top"])
    return out


def _slot_minutes_to_hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _slot_rows_between(
    slot_rows: list[tuple[float, int, int]], y_lo: float, y_hi: float
) -> list[tuple[float, int, int]]:
    """Slots whose Y falls strictly between y_lo (exclusive) and y_hi
    (exclusive)."""
    return [s for s in slot_rows if y_lo < s[0] < y_hi]


def _discover_tech_sessions(
    rows: list[dict], words: list[dict], day_key: str,
    band: tuple[float, float], room_by_col: dict[int, str],
    day_letter: str,
) -> list[dict]:
    """Emit one SKELETON entry per (topic-header column) on the given day.
    For each topic header row, the session's time range is the first/last
    time-slot row down to the next break / next topic header. Each column's
    track code (e.g. M1A, M2B) is derived from the day letter + the topic
    header's ordinal position in the day + the column letter."""
    out: list[dict] = []
    headers = _topic_header_rows_in_band(rows, band)
    if not headers:
        return out
    rows_in_band = [r for r in rows if band[0] <= r["top"] <= band[1]]
    rows_in_band.sort(key=lambda r: r["top"])
    slot_rows = _all_slot_rows(words, band)

    for ord_idx, h in enumerate(headers, 1):
        # Block bounds: from this topic header down to the next topic
        # header (or band end), then truncated by the first session-
        # boundary row inside that range.
        next_y = (headers[ord_idx]["top"]
                  if ord_idx < len(headers) else band[1])
        window = _tech_block_slot_window(
            slot_rows, rows_in_band, h["top"], next_y)
        if window is None:
            continue
        start_min, end_min = window
        cols = _topic_header_columns(h, room_by_col)
        col_letters = {1: "A", 2: "B", 3: "C"}
        for col in sorted(cols):
            letter = col_letters.get(col)
            if not letter:
                continue
            track = f"{day_letter}{ord_idx}{letter}"
            out.append({
                "id": track, "day": day_key,
                "start": _slot_minutes_to_hhmm(start_min),
                "end": _slot_minutes_to_hhmm(end_min),
                "type": "Technical Session", "color": "blue",
                "track": track, "column": col,
            })
    return out


def _slot_for_row(
    row: dict, slot_rows: list[tuple[float, int, int]]
) -> tuple[int, int] | None:
    """Return the (start_min, end_min) of the slot anchor co-located with
    this row's Y (within ROW_TOL)."""
    for top, s, e in slot_rows:
        if abs(top - row["top"]) <= ROW_TOL + 0.5:
            return s, e
    return None


def _multi_slot_span(
    row: dict, slot_rows: list[tuple[float, int, int]], words: list[dict],
    rows_in_band: list[dict]
) -> tuple[int, int] | None:
    """For full-row events that span multiple consecutive time-slot rows
    (the plenary case), return (start_min, end_min) of the spanned range.

    Start: the earliest start_min among all slot labels on the same row
    (PDF rows can pack multiple slot labels — e.g. a plenary row that
    occupies a 15-min slot plus a 30-min slot side-by-side).

    Then: extend the start backward and the end forward through every
    consecutive row that is empty in all three columns (so a plenary
    spanning the 1805-1820 slot grows to cover 1805-1850 when the
    neighbouring time-slot rows are empty)."""
    # All slot labels on the current row, sorted by start_min.
    row_slot_labels = _row_left_time_slots(words, row["top"])
    if not row_slot_labels:
        anchor = _slot_for_row(row, slot_rows)
        if not anchor:
            return None
        s, e = anchor
    else:
        s = min(lbl[0] for lbl in row_slot_labels)
        e = max(lbl[1] for lbl in row_slot_labels)
    sorted_slots = sorted(slot_rows, key=lambda t: t[0])
    # Index of the first slot whose Y matches this row.
    idx_first = idx_last = None
    for i, t in enumerate(sorted_slots):
        if abs(t[0] - row["top"]) <= ROW_TOL + 0.5:
            if idx_first is None:
                idx_first = i
            idx_last = i
    if idx_first is None:
        return s, e
    cur = idx_first
    while cur - 1 >= 0:
        prev_y = sorted_slots[cur - 1][0]
        r = _row_at_y(rows_in_band, prev_y)
        if r is None or not _row_is_empty_cells(r):
            break
        cur -= 1
        s = min(s, sorted_slots[cur][1])
    cur = idx_last
    while cur + 1 < len(sorted_slots):
        nxt_y = sorted_slots[cur + 1][0]
        r = _row_at_y(rows_in_band, nxt_y)
        if r is None or not _row_is_empty_cells(r):
            break
        cur += 1
        e = max(e, sorted_slots[cur][2])
    return s, e


def _row_at_y(rows_in_band: list[dict], y: float) -> dict | None:
    for r in rows_in_band:
        if abs(r["top"] - y) <= ROW_TOL + 0.5:
            return r
    return None


def _row_is_empty_cells(row: dict) -> bool:
    """True for time-slot rows whose three cells have no text (only the
    HHMM-HHMM label in the left column)."""
    for c in (1, 2, 3):
        if _row_column_text(row, c):
            return False
    return True


def _discover_skeleton(
    rows: list[dict], words: list[dict], page_h: float,
    day_isos: dict[str, str], bands: dict[str, tuple[float, float]],
    room_by_col: dict[int, str], plenary_room: str,
) -> list[dict]:
    """Build the session SKELETON from the PDF structure. Emits sessions in
    day-then-event-order matching the legacy hand-curated list; tech-track
    sessions emit first within their day, then chronological non-tech
    events."""
    # Letter of each day-key, in the order discovered.
    day_keys_in_order = list(day_isos)
    track_day_letters: dict[str, str] = {}
    # Map day-keys to track-code day letters by position; the conference
    # convention is M=first weekday, T=second, W=third (after the Sunday
    # student day, which has no tracks). We pick the letter from the day's
    # weekday name where possible, falling back to position.
    fallback_letters = list(_TRACK_DAY_LETTERS)
    for k in day_keys_in_order:
        letter = {"mon": "M", "tue": "T", "wed": "W", "thu": "R",
                  "fri": "F"}.get(k)
        if letter and letter in fallback_letters:
            track_day_letters[k] = letter
            fallback_letters.remove(letter)
    for k in day_keys_in_order:
        if k not in track_day_letters and fallback_letters:
            track_day_letters[k] = fallback_letters.pop(0)

    sessions: list[dict] = []

    for day_key in day_keys_in_order:
        band = bands.get(day_key)
        if not band:
            continue

        # ---- Sunday container -----------------------------------------------
        # Sunday has no tech tracks and no topic headers; every content row
        # in the band is an event row. We emit one container session
        # ("Sunday Student Event") wrapping the day's three components as
        # talks. The container's room is the most-frequently-mentioned room
        # in the day's event rows.
        sun_events = _discover_sun_events(rows, band)
        if sun_events:
            talks: list[dict] = []
            rooms_seen: dict[str, int] = {}
            for ev in sun_events:
                rm = ev["room"]
                if rm:
                    rooms_seen[rm] = rooms_seen.get(rm, 0) + 1
                talks.append({
                    "title": ev["title"],
                    "speaker": "", "speaker_aff": "", "color": "rose",
                    "start": _slot_minutes_to_hhmm(ev["start_min"]),
                    "end": _slot_minutes_to_hhmm(ev["end_min"]),
                })
            container_start = min(ev["start_min"] for ev in sun_events)
            container_end = max(ev["end_min"] for ev in sun_events)
            container_room = ""
            if rooms_seen:
                container_room = max(rooms_seen, key=lambda r: rooms_seen[r])
            sessions.append({
                "id": "STUD", "day": day_key,
                "start": _slot_minutes_to_hhmm(container_start),
                "end": _slot_minutes_to_hhmm(container_end),
                "title": "Sunday Student Event", "type": "Student Event",
                "color": "rose", "room": container_room,
                "talks": talks,
            })
            continue

        # ---- Weekday: merge tech and special sessions, then chronologically
        # sort. Within a tied start time, tech tracks sort by column letter
        # (A, B, C), and special events sort by id for stability. Opening
        # ceremony (08:00) lands before M1A (08:30) etc.
        day_letter = track_day_letters.get(day_key, "?")
        tech_sessions = _discover_tech_sessions(
            rows, words, day_key, band, room_by_col, day_letter)
        special_sessions = _discover_special_sessions(
            rows, words, day_key, band, room_by_col, plenary_room)
        day_sessions = tech_sessions + special_sessions
        day_sessions.sort(key=lambda s: (
            _hhmm_to_minutes(s["start"]),
            # Tech sessions before specials at the same start time, then
            # by id for stable ordering.
            0 if s.get("track") else 1,
            s["id"],
        ))
        sessions.extend(day_sessions)

    return sessions


def _split_title_room(text: str) -> tuple[str, str]:
    """Split an event-cell text into (title, room) at the first comma that
    is NOT inside parentheses. Handles cells like
        'Student Workshop (to be announced soon), HG F30 (Plenary Auditorium)'
    where the title carries its own parenthetical aside."""
    depth = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            return text[:i].strip(), text[i + 1:].strip()
    return text.strip(), ""


def _discover_sun_events(
    rows: list[dict], band: tuple[float, float]
) -> list[dict]:
    """For Sunday, walk time-slot rows in the band and emit one event per
    classifiable row. Skips Registration rows. Each event carries
    {title, room, start_min, end_min}."""
    out: list[dict] = []
    rows_in_band = sorted(
        [r for r in rows if band[0] <= r["top"] <= band[1]],
        key=lambda r: r["top"])
    for r in rows_in_band:
        # On Sunday the column-1 cell is empty; the event text sits in the
        # right band, which we treat as columns 2 + 3 here.
        text = _row_column_text(r, 2)
        if not text:
            text = _row_column_text(r, 3)
        if not text:
            cell = [w for w in r["words"]
                    if 55.0 <= (w["x0"] + w["x1"]) / 2 < 1100.0
                    and not TIME_RE.match(w["text"])]
            cell.sort(key=lambda w: w["x0"])
            text = _join_words(cell).strip()
        if not text:
            continue
        if _is_registration_row(text):
            continue
        slots = _row_left_time_slots(r["words"], r["top"])
        if not slots:
            continue
        start_min = slots[0][0]
        end_min = slots[-1][1]
        cls = _classify_special_row(text)
        if cls is None:
            continue
        kind, _fields = cls
        if kind == "student_workshop":
            title = "Student Workshop"
        elif kind == "bench":
            title = "Bench to Business Symposium"
        elif kind == "pizza":
            title = "Networking Pizza Dinner"
        else:
            continue
        # Re-extract the room by splitting the FULL cell text at the first
        # top-level comma (not inside parens). The regexes used by the
        # classifier match too greedily for cells with parenthetical
        # asides in the title.
        _, room = _split_title_room(text)
        out.append({"title": title, "room": room,
                    "start_min": start_min, "end_min": end_min})
    return out


def _industry_or_workshop_window(
    header_row: dict, slot_rows: list[tuple[float, int, int]],
    rows_in_band: list[dict], col: int,
) -> tuple[int, int] | None:
    """For an industry/workshop header row (which carries no time-slot label
    in the left column itself), return the (start_min, end_min) of the
    talk slots beneath it. Walks all slot rows strictly below the header Y
    up to the first session-boundary row, then truncates the upper bound
    to the END of the last slot whose target-column cell has non-empty
    content. This trims off the empty "gap" slot the PDF leaves between
    the session's last talk and the next event."""
    keep: list[tuple[int, int]] = []
    last_content_end: int | None = None
    for top, s, e in sorted(slot_rows, key=lambda t: t[0]):
        if top <= header_row["top"] + 0.5:
            continue
        row = _row_at_y(rows_in_band, top)
        if row is not None and _row_is_session_boundary(row):
            break
        keep.append((s, e))
        if row is not None and _row_column_text(row, col).strip():
            last_content_end = e
    if not keep:
        return None
    start_min = min(k[0] for k in keep)
    end_min = last_content_end if last_content_end is not None else max(
        k[1] for k in keep)
    return start_min, end_min


def _discover_special_sessions(
    rows: list[dict], words: list[dict], day_key: str,
    band: tuple[float, float], room_by_col: dict[int, str],
    plenary_room: str,
) -> list[dict]:
    """Emit one session per classifiable non-tech row in the day band.
    Ordering inside the day is chronological by start_min. Industry and
    Workshop header rows can sit on rows WITHOUT a left-column time-slot
    label — in that case we derive their time window from the slot rows
    beneath them, up to the next session-boundary row."""
    out: list[dict] = []
    rows_in_band = sorted(
        [r for r in rows if band[0] <= r["top"] <= band[1]],
        key=lambda r: r["top"])
    slot_rows = _all_slot_rows(words, band)

    # Per-row poster-blitz/coffee-poster de-dupe (one row can carry several
    # columns of the same coffee-poster cell text bleeding across columns).
    seen_pos_keys: set[str] = set()

    for r in rows_in_band:
        # Skip break / registration rows up-front.
        if _is_break_row(_row_column_text(r, 2)) or \
                _is_registration_row(_row_column_text(r, 2)):
            continue
        # Treat the row as a candidate. We classify each populated column
        # independently because parallel events (industry-talk sessions,
        # poster blitzes) come across as side-by-side cells.
        cls_per_col = _row_classified_events(r, band)
        if not cls_per_col:
            continue

        slot = _slot_for_row(r, slot_rows)
        s_min, e_min = slot if slot else (0, 0)

        for col, kind, fields, text in cls_per_col:
            if kind == "plenary":
                # Plenary spans multiple empty slots; widen the window from
                # the slot Y both ways while neighbouring rows are empty.
                span = _multi_slot_span(r, slot_rows, words, rows_in_band)
                sm, em = span if span else (s_min, e_min)
                n_val = int(fields.get("n") or 1)
                speaker = (fields.get("speaker") or "").strip()
                room = (fields.get("room") or plenary_room).strip()
                out.append({
                    "id": f"PLEN{n_val}",
                    "day": day_key,
                    "start": _slot_minutes_to_hhmm(sm),
                    "end": _slot_minutes_to_hhmm(em),
                    "title": f"Plenary Session {n_val}",
                    "type": "Plenary", "color": "orange", "room": room,
                    "talks": [{
                        "title": "Plenary Lecture",
                        "speaker": speaker, "speaker_aff": "",
                        "color": "orange",
                    }],
                })
            elif kind == "industry":
                # Header sits ABOVE its time slots — derive the window from
                # the slots beneath, up to the next session boundary.
                window = _industry_or_workshop_window(
                    r, slot_rows, rows_in_band, col)
                if window is None:
                    continue
                sm, em = window
                n_val = int(fields.get("n") or 1)
                out.append({
                    "id": f"IND{n_val}",
                    "day": day_key,
                    "start": _slot_minutes_to_hhmm(sm),
                    "end": _slot_minutes_to_hhmm(em),
                    "type": "Industry Talks", "color": "blue",
                    "room": room_by_col.get(col, ""),
                    "pdf_title": {"source": "row_text",
                                  "column": col, "y": r["top"]},
                    "harvest": {"column": col, "talk_color": "indigo",
                                "slot_mode": "per_slot", "slot_minutes": 10},
                })
            elif kind == "workshop":
                window = _industry_or_workshop_window(
                    r, slot_rows, rows_in_band, col)
                if window is None:
                    continue
                sm, em = window
                n_val = int(fields.get("n") or 1)
                out.append({
                    "id": f"WS{n_val}",
                    "day": day_key,
                    "start": _slot_minutes_to_hhmm(sm),
                    "end": _slot_minutes_to_hhmm(em),
                    "type": "Workshop", "color": "blue",
                    "room": room_by_col.get(col, ""),
                    "pdf_title": {"source": "row_text",
                                  "column": col, "y": r["top"]},
                    "harvest": {"column": col, "talk_color": "indigo",
                                "slot_mode": "session"},
                })
            elif kind == "poster_blitz":
                n = fields.get("n") or "?"
                sub = fields.get("sub") or "?"
                # Convert sub-digit to a column letter (1→A, 2→B, 3→C) so
                # the id reads as a CLEO-style code (PB1A, PB1B, …) rather
                # than a slug. The PDF prints both blitzes side-by-side on
                # a single row: they run in PARALLEL in different rooms,
                # so both share the same time span — the full extent of
                # every slot label on the row.
                try:
                    sub_letter = chr(ord("A") + int(sub) - 1)
                except (ValueError, TypeError):
                    sub_letter = str(sub)
                blitz_id = f"PB{n}{sub_letter}"
                if blitz_id in seen_pos_keys:
                    continue
                seen_pos_keys.add(blitz_id)
                row_slots = _row_left_time_slots(words, r["top"])
                if row_slots:
                    sm = min(lbl[0] for lbl in row_slots)
                    em = max(lbl[1] for lbl in row_slots)
                else:
                    sm, em = s_min, e_min
                title_text, _ = _split_title_room(text)
                out.append({
                    "id": blitz_id, "day": day_key,
                    "start": _slot_minutes_to_hhmm(sm),
                    "end": _slot_minutes_to_hhmm(em),
                    "title": title_text or text.strip(),
                    "type": "Poster Blitz", "color": "teal",
                    "room": room_by_col.get(col, ""),
                })
            elif kind == "poster_session":
                n = fields.get("n") or "?"
                pos_id_key = f"POS{n}"
                if pos_id_key in seen_pos_keys:
                    continue
                seen_pos_keys.add(pos_id_key)
                title_text, room = _split_title_room(text)
                # The PDF prints "Coffee+Poster" (no space); the rendered
                # session list reads better with the conventional spacing.
                title_text = title_text.replace("Coffee+Poster",
                                                "Coffee + Poster")
                out.append({
                    "id": pos_id_key, "day": day_key,
                    "start": _slot_minutes_to_hhmm(s_min),
                    "end": _slot_minutes_to_hhmm(e_min),
                    "title": title_text,
                    "type": "Poster Session", "color": "teal",
                    "room": room,
                })
            elif kind == "opening":
                title_text, room = _split_title_room(text)
                if room == "Plenary Auditorium" and plenary_room:
                    room = plenary_room
                out.append({
                    "id": "OPEN", "day": day_key,
                    "start": _slot_minutes_to_hhmm(s_min),
                    "end": _slot_minutes_to_hhmm(e_min),
                    "title": title_text, "type": "Ceremony",
                    "color": "rose", "room": room,
                })
            elif kind == "closing":
                title_text, room = _split_title_room(text)
                out.append({
                    "id": "CLSE", "day": day_key,
                    "start": _slot_minutes_to_hhmm(s_min),
                    "end": _slot_minutes_to_hhmm(e_min),
                    "title": title_text, "type": "Ceremony",
                    "color": "rose", "room": room,
                })
            elif kind == "welcome":
                # The reception is anchored to one slot in the PDF
                # (e.g. 1850-2030); the slot already covers its duration.
                title_text, room = _split_title_room(text)
                out.append({
                    "id": "WLCM", "day": day_key,
                    "start": _slot_minutes_to_hhmm(s_min),
                    "end": _slot_minutes_to_hhmm(e_min),
                    "title": title_text, "type": "Social Event",
                    "color": "rose", "room": room,
                })
            elif kind == "gala":
                # Gala cell prints "Title @ Venue" — split on the @.
                if "@" in text:
                    title_text, room = (s.strip() for s in text.split("@", 1))
                else:
                    title_text, room = _split_title_room(text)
                out.append({
                    "id": "GALA", "day": day_key,
                    "start": _slot_minutes_to_hhmm(s_min),
                    "end": _slot_minutes_to_hhmm(e_min),
                    "title": title_text, "type": "Social Event",
                    "color": "rose", "room": room,
                })
            elif kind == "city_tour":
                title_text, room = _split_title_room(text)
                out.append({
                    "id": "TOUR", "day": day_key,
                    "start": _slot_minutes_to_hhmm(s_min),
                    "end": _slot_minutes_to_hhmm(e_min),
                    "title": title_text, "type": "Social Event",
                    "color": "rose", "room": room,
                })
            elif kind == "lab_tours":
                # The PDF labels only the start slot. End time comes from
                # the lab-tours enrichment page when present; the discoverer
                # leaves a 75-minute placeholder otherwise.
                title_text, room = _split_title_room(text)
                out.append({
                    "id": "LABS", "day": day_key,
                    "start": _slot_minutes_to_hhmm(s_min),
                    "end": _slot_minutes_to_hhmm(s_min + 75),
                    "title": title_text, "type": "Other",
                    "color": "rose", "room": room,
                })

    # Chronological order for non-tech events inside the day.
    out.sort(key=lambda s: (_hhmm_to_minutes(s["start"]), s["id"]))
    return out


def _slot_minutes(slot: str) -> tuple[int, int]:
    """Convert 'HHMM-HHMM' to (start_min, end_min)."""
    a, b = slot.split("-")
    return _hhmm_to_minutes(a), _hhmm_to_minutes(b)


def _session_time_slots(
    words: list[dict],
    band: tuple[float, float],
    start_min: int,
    end_min: int,
) -> list[tuple[float, int, int]]:
    """Return [(top_y, start_min, end_min), …] for every "HHMM-HHMM" time-slot
    label in the left-edge column whose start falls inside [start_min, end_min).
    Sorted by Y ascending (top-of-page first).

    Scans the raw word stream rather than pre-clustered rows on purpose: row
    clustering chains transitively across columns at this density (talk lines
    in different columns sit at very similar Y), which would smear the time
    label onto neighbouring rows and mis-place the slot anchor."""
    out: list[tuple[float, int, int]] = []
    for w in words:
        if not (band[0] <= w["top"] <= band[1]):
            continue
        if w["x0"] >= TIME_X_RANGE[1]:
            continue
        if not TIME_RE.match(w["text"]):
            continue
        s, e = _slot_minutes(w["text"])
        if start_min <= s < end_min:
            out.append((w["top"], s, e))
    out.sort(key=lambda t: t[0])
    return out


def _harvest_session_y_range(
    slots: list[tuple[float, int, int]],
    band: tuple[float, float],
) -> tuple[float, float]:
    """Tight Y range for a session given its time-slot rows. A modest tail-pad
    below the last time-slot row catches invited talks that span two slots and
    sit just under the last labelled slot. Too generous and we'd absorb the
    next block's session header."""
    if not slots:
        return (band[0], band[0])
    tops = [s[0] for s in slots]
    return (min(tops) - 1.0, max(tops) + 5.0)


def _talk_time_window(
    y: float,
    slots: list[tuple[float, int, int]],
    sess_start_min: int,
    sess_end_min: int,
    is_invited: bool = False,
) -> tuple[int, int]:
    """Map a talk's row-Y to the time window it occupies.

    Strategy: a 15-minute talk's text row sits within ~2pt of one time-slot
    label's Y; a 30-minute invited talk's row sits roughly midway between two
    consecutive labels (each ~5-7pt away). So we pick the NEAREST slot by
    absolute Y distance, and extend to span the neighbouring slot only when
    the two are about equally far from the talk (i.e. it's genuinely between
    them, not just close to one).
    """
    if not slots:
        return sess_start_min, sess_end_min

    closest_idx = min(range(len(slots)),
                      key=lambda i: abs(y - slots[i][0]))
    a_top, a_start, a_end = slots[closest_idx]
    dist_a = abs(y - a_top)

    # Invited talks are 30-minute slots on the ECIO grid: extend the anchor to
    # the next slot's end (or pull in the previous slot's start, if the row is
    # actually above the closest slot). The "Invited:" tag in the title is the
    # authoritative signal — geometry alone can't tell a 15- from a 30-minute
    # row when an invited row sits flush with one of the two slots it covers.
    if is_invited:
        if closest_idx + 1 < len(slots) and y >= a_top - 1.0:
            return a_start, slots[closest_idx + 1][2]
        if closest_idx - 1 >= 0:
            return slots[closest_idx - 1][1], a_end
        return a_start, a_end

    # Non-invited (15-min): "equidistant neighbour" check catches the rare row
    # that lands midway between two slot labels.
    for nb_idx in (closest_idx - 1, closest_idx + 1):
        if not (0 <= nb_idx < len(slots)):
            continue
        nb_top, nb_start, nb_end = slots[nb_idx]
        dist_b = abs(y - nb_top)
        if abs(dist_a - dist_b) < 2.0 and dist_a > 3.0:
            lo = min(closest_idx, nb_idx)
            hi = max(closest_idx, nb_idx)
            return slots[lo][1], slots[hi][2]

    return a_start, a_end


def _split_title_speaker(
    line_words: list[dict],
    col_x: tuple[float, float],
) -> tuple[str, str]:
    """For one line of words inside a cell, split into (title, speaker) at the
    largest x-gap of at least SPEAKER_GAP_PT. The split is accepted only when
    the right-hand chunk starts in the last 40% of the column width — that's
    the right-aligned speaker region in the schedule grid. Otherwise the gap
    is between two title chunks and we keep the whole line as title."""
    if not line_words:
        return "", ""
    ws = sorted(line_words, key=lambda w: w["x0"])
    # Largest gap in the row.
    best_split: int | None = None
    best_gap = SPEAKER_GAP_PT
    for i in range(1, len(ws)):
        gap = ws[i]["x0"] - ws[i - 1]["x1"]
        if gap >= best_gap:
            best_gap = gap
            best_split = i
    if best_split is None:
        return _join_words_baseline_aware(ws), ""
    right = ws[best_split:]
    col_lo, col_hi = col_x
    right_zone_start = col_lo + 0.6 * (col_hi - col_lo)
    if right[0]["x0"] < right_zone_start:
        return _join_words_baseline_aware(ws), ""
    return (_join_words_baseline_aware(ws[:best_split]),
            _join_words_baseline_aware(right))


def _join_words(ws: list[dict]) -> str:
    """Reassemble a list of (sorted-by-x) word dicts into a string with single
    spaces. Letters that pdfplumber split into 1-2 character fragments (it does
    this for some condensed font runs) get glued back when their boxes touch."""
    if not ws:
        return ""
    parts: list[str] = []
    prev = None
    for w in ws:
        if prev is not None and (w["x0"] - prev["x1"]) <= 0.5:
            parts.append(w["text"])
        else:
            parts.append((" " if parts else "") + w["text"])
        prev = w
    return "".join(parts).strip()


def _join_words_baseline_aware(ws: list[dict]) -> str:
    """Like _join_words, but when the words occupy more than one distinct
    baseline (some italic speaker chips render across two y values per glyph),
    group by baseline first, sort each group by x, and concatenate groups in
    top-to-bottom order. This prevents interleaved characters like
    "S-e-y-e-d-m-o-h-…" on one baseline crossing with "S-e-y-e-d-i-n-n-…" on
    the next from being woven together by a flat x-sort."""
    if not ws:
        return ""
    # Group by top with a small tolerance — these are GLYPH baselines, not row
    # bands. 2pt is tight enough to keep two stacked italic-name rows (5pt
    # apart) in their own groups, but loose enough to fold a 1.7pt-offset
    # chemical subscript ("SiN-LiNbO3", "CuInP2S6") onto the base word so it
    # joins with no space rather than getting orphaned downstream.
    sw = sorted(ws, key=lambda w: w["top"])
    groups: list[list[dict]] = []
    for w in sw:
        if groups and abs(w["top"] - groups[-1][-1]["top"]) <= 2.0:
            groups[-1].append(w)
        else:
            groups.append([w])
    parts: list[str] = []
    for g in groups:
        g.sort(key=lambda w: w["x0"])
        text = _join_words(g)
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def _extract_cell_lines(
    rows: list[dict],
    col_x: tuple[float, float],
    y_range: tuple[float, float],
) -> list[tuple[str, str, float]]:
    """Pull (title, speaker, y) lines out of a single column inside a session's
    Y range. Filters out the larger-font track-topic header words and the bare
    3-letter track labels (M1A, T2B, …) that get rendered next to cells.

    The result is sorted by Y (top to bottom)."""
    cell_words: list[dict] = []
    for r in rows:
        if not (y_range[0] <= r["top"] <= y_range[1]):
            continue
        for w in r["words"]:
            mid = (w["x0"] + w["x1"]) / 2
            if not (col_x[0] <= mid < col_x[1]):
                continue
            if w["size"] >= TOPIC_FONT_MIN:
                continue  # topic headers
            if TRACK_LABEL_RE.match(w["text"]):
                continue  # bare track labels
            cell_words.append(w)
    if not cell_words:
        return []
    # Re-cluster these into lines (cells often print one talk per line; long
    # titles wrap to a second line at the same x0).
    lines = _cluster_rows(cell_words, tol=ROW_TOL)
    out: list[tuple[str, str, float]] = []
    for ln in lines:
        title, speaker = _split_title_speaker(ln["words"], col_x)
        if not title and not speaker:
            continue
        # Drop rows that are obviously non-talk break content (Coffee, Lunch,
        # Plenary, Workshop, …) — these are full-width rows in the PDF that
        # bleed slightly into the column we're harvesting.
        if any(title.startswith(p) for p in NON_TALK_PREFIXES):
            continue
        out.append((title, speaker, ln["top"]))
    # Merge consecutive lines where the second line had no speaker AND
    # comes within 6pt vertically of the previous one — these are wrapped
    # titles.
    merged: list[tuple[str, str, float]] = []
    for title, speaker, top in out:
        if (merged and not speaker
                and abs(top - merged[-1][2]) < 6.0
                and not merged[-1][1]):  # previous also had no speaker
            prev_t, _, prev_y = merged[-1]
            merged[-1] = (prev_t + " " + title, "", prev_y)
        else:
            merged.append((title, speaker, top))
    return merged




# =============================================================================
# Title / speaker post-processing.
# =============================================================================

_INVITED_PREFIXES = ("Invited:", "Invited :", "Invited -")


def _clean_title(raw: str) -> tuple[str, bool]:
    """Strip an 'Invited:' marker and trailing punctuation/colons. Returns
    (clean_title, is_invited)."""
    t = raw.strip()
    is_invited = False
    for pfx in _INVITED_PREFIXES:
        if t.startswith(pfx):
            t = t[len(pfx):].strip()
            is_invited = True
            break
    # Drop a stray trailing colon that the PDF sometimes carries after a
    # right-aligned speaker box.
    t = re.sub(r"[:\s]+$", "", t)
    return t, is_invited


def _clean_speaker(raw: str) -> str:
    s = raw.strip().rstrip(":,;").strip()
    # Collapse internal multi-space runs.
    s = re.sub(r"\s+", " ", s)
    return s


def _talk_id(session_id: str, n: int) -> str:
    return f"{session_id}-T{n:02d}"


def _build_minute_slots(start: str, end: str) -> list[tuple[str, str]]:
    """Return list of (start_iso_time, end_iso_time) 15-minute slots inside
    [start, end). Used to assign a default time to each talk when the PDF row
    didn't provide a finer one (we don't currently propagate per-row times
    through _extract_cell_lines, so all talks inherit the session times)."""
    # Currently unused — kept for future per-talk timing if we wire it in.
    return [(start, end)]


# =============================================================================
# PDF title + non-grid cell harvesting.
#
# Used for sessions whose talks (and titles) come from PDF rows that don't fit
# the wide title-vs-speaker tech grid: the industry-talk blocks and the two
# workshop blocks. Their cells render "Title. Speaker, Affiliation" as one run
# of words with normal letter spacing (no big x-gap), and their session titles
# sit on a dedicated row inside the column rather than as a size-4.56 topic
# header above it.
# =============================================================================

# Title and Speaker are separated by ". " (period + space). The PDF sometimes
# pads or omits the space; allow zero-or-more spaces on either side. Followed
# by a capital letter so we don't split a mid-sentence abbreviation.
_PERIOD_SPLIT_RE = re.compile(r"\s*\.\s+(?=[A-ZÀ-Ý])")
# Speaker, Affiliation separator: a comma with optional whitespace either side.
# The PDF occasionally renders as "Heidi Potts ,Zurich Instruments" (space
# before, none after), so we tolerate both directions.
_COMMA_SPLIT_RE = re.compile(r"\s*,\s*")

# Rows of this content inside a workshop band are panel/meta rows, not talks.
_WORKSHOP_NON_TALK_RE = re.compile(
    r"^(Panel Discussion|Q&A|Lunch|Coffee|Plenary|Poster|WORKSHOP\b)",
    re.IGNORECASE,
)


def _read_pdf_title(
    rows: list[dict],
    pdf_title_spec: dict,
) -> str:
    """Return the session title text read from a specific PDF row.

    Used by workshops and industry sessions, whose header text sits on a
    dedicated row inside the column (not as a larger-font topic banner above
    the column). The spec carries the column index and the target Y; we find
    the row clustered nearest that Y and pull its in-column words.
    """
    col = pdf_title_spec["column"]
    target_y = float(pdf_title_spec["y"])
    col_lo, col_hi = COL_X_RANGES[col]
    # Find the row whose centre is closest to target_y (tolerance: a single
    # ROW_TOL window). Rows further than ROW_TOL away don't actually contain
    # our header.
    candidates = [r for r in rows if abs(r["top"] - target_y) <= ROW_TOL + 0.5]
    if not candidates:
        return ""
    row = min(candidates, key=lambda r: abs(r["top"] - target_y))
    header_words = [
        w for w in row["words"]
        if col_lo <= (w["x0"] + w["x1"]) / 2 < col_hi
    ]
    if not header_words:
        return ""
    header_words.sort(key=lambda w: w["x0"])
    text = _join_words(header_words).strip()
    # Trim a trailing punctuation/colon the renderer sometimes leaves on.
    text = re.sub(r"[:\s]+$", "", text)
    return text


def _topic_header_title(
    rows: list[dict],
    band: tuple[float, float],
    column: int,
) -> str:
    """Return the topic-header text rendered above a tech-track session's
    column, e.g. a short topic phrase. The PDF uses a larger 4.56pt
    font for topic headers on Mon/Tue but mysteriously falls back to the
    4.08pt talk-text font on Wed — so we cannot key purely on size.

    Strategy: identify the row immediately above the session's first slot
    that, in this column, looks like a SHORT, NON-TALK row (no large
    word-gap, no time tag, no track label, not a day banner). The
    "Registration, Foyer in front…" banner that sometimes sits just above
    the topic row is filtered out by a non-talk-prefix check.
    """
    col_lo, col_hi = COL_X_RANGES[column]
    # All candidate rows in this column above the session's first slot row
    # but no more than ~25pt above (so we don't reach into a previous block).
    candidates: list[tuple[float, str, float]] = []  # (top, text, size)
    for r in rows:
        if r["top"] > band[0] + 0.5:  # below the band's start — talks, not headers
            continue
        if r["top"] < band[0] - 25:
            continue
        cell = [
            w for w in r["words"]
            if col_lo <= (w["x0"] + w["x1"]) / 2 < col_hi
        ]
        if not cell:
            continue
        cell.sort(key=lambda w: w["x0"])
        # Day banner rows: large font, often contain "JUNE" or weekday.
        sizes = [float(w.get("size", 0)) for w in cell]
        if max(sizes, default=0) >= 4.7:
            continue
        text = _join_words(cell).strip()
        if not text:
            continue
        # Filter generic non-topic banners.
        if text.startswith(("Registration", "Coffee", "Lunch", "Welcome",
                            "Closing", "Opening", "Plenary", "Industry",
                            "Workshop", "Poster", "Panel", "Gala",
                            "Networking", "Bench", "Student", "Zurich",
                            "Lab", "Exhibition", "Session Rooms",
                            "WORKSHOP")):
            continue
        if TIME_RE.match(text.split()[0] if text.split() else ""):
            continue
        if TRACK_LABEL_RE.match(text.split()[0] if text.split() else ""):
            continue
        # If any size-4.56 word, prefer this row strongly.
        candidates.append((r["top"], text, max(sizes)))
    if not candidates:
        return ""
    # Prefer a size-4.56 row when present (Mon/Tue case). Otherwise take
    # the row closest to band[0] from above.
    larger = [c for c in candidates if c[2] >= TOPIC_FONT_MIN]
    if larger:
        chosen = max(larger, key=lambda c: c[0])  # closest from above
    else:
        chosen = max(candidates, key=lambda c: c[0])
    return chosen[1]


def _split_industry_cell(text: str) -> tuple[str, str, str]:
    """Parse one industry/workshop cell into (title, speaker, affiliation).

    The PDF packs the three fields as "Title. Speaker, Affiliation" in one
    continuous run. We split from the right:
      1. The affiliation is everything after the LAST comma.
      2. In the prefix that remains, the title is split from the speaker
         by ". " (period + space + capital letter). Where no such period
         exists, an unambiguous trailing "X Y" name pattern (1–4 words,
         each starting upper-case) is taken as the speaker; otherwise the
         whole prefix is the title and speaker is empty.

    Degenerate inputs:
      - empty / whitespace-only             -> ("", "", "")
      - one company token, no comma         -> ("", "", text)  (sponsor slot)
      - "Bert Offrein" (one name, no comma) -> ("", "Bert Offrein", "")
    """
    def _strip_trailing_punct(s: str) -> str:
        # Some title cells embed an inner comma before the speaker (e.g.
        # "ltoi300: ... PICs, Andrei Kiselev, Luxtelligence SA"). The last
        # comma correctly splits off the affiliation, but the title is then
        # left with a stray ", " or ",". Trim any trailing comma / semicolon
        # / colon / whitespace so titles don't render with that artifact.
        return re.sub(r"[\s,;:]+$", "", s).strip()

    t = text.strip()
    if not t:
        return "", "", ""

    # Strip an opening "." (the PDF sometimes leads with one when a sponsor
    # slot has no title, e.g. ". Frederic Loizeau, Lightium AG").
    t = re.sub(r"^\.\s*", "", t)

    # No commas at all: either a bare affiliation (single sponsor) or a bare
    # speaker (workshop chair). A bare affiliation tends to be a known-company
    # short string like "LIGENTEC SA"; a bare speaker is a 1-3-word personal
    # name. Use word-count + presence of digits/all-caps as a weak signal.
    if "," not in t:
        if _looks_like_person(t):
            return "", _strip_trailing_punct(t), ""
        return "", "", t

    # One or more commas: the LAST comma chunk is the affiliation.
    last_comma = t.rfind(",")
    affiliation = t[last_comma + 1:].strip()
    prefix = t[:last_comma].strip()

    # In the prefix, split title/speaker on ". <Capital>". Look at the LAST
    # such split (titles can legitimately contain period+capital, though rare;
    # the speaker always comes last). If no such split, fall back to "look at
    # the trailing word group: if it looks like a person name (<=4 short
    # capital-led words), take it as the speaker; otherwise treat the whole
    # prefix as title".
    matches = list(_PERIOD_SPLIT_RE.finditer(prefix))
    if matches:
        last = matches[-1]
        title = prefix[:last.start()].strip()
        speaker = prefix[last.end():].strip()
        return _strip_trailing_punct(title), _strip_trailing_punct(speaker), affiliation

    # No period delimiter — look for an implicit speaker tail (a short
    # capital-led name run). Walk back from the end and absorb tokens until
    # we hit one that doesn't look like a name token.
    tokens = prefix.split()
    if not tokens:
        return "", "", affiliation
    # Collect a trailing run of "name-shaped" tokens, max 4.
    tail_start = len(tokens)
    for i in range(len(tokens) - 1, max(-1, len(tokens) - 5), -1):
        if _looks_like_name_token(tokens[i]):
            tail_start = i
        else:
            break
    if tail_start < len(tokens) and tail_start > 0:
        title = " ".join(tokens[:tail_start]).strip()
        speaker = " ".join(tokens[tail_start:]).strip()
        # Sanity: if "title" is suspiciously short (1 word), probably it's
        # actually all a name and there's no title.
        if len(title.split()) <= 1 and _looks_like_person(prefix):
            return "", _strip_trailing_punct(prefix), affiliation
        return _strip_trailing_punct(title), _strip_trailing_punct(speaker), affiliation
    # The whole prefix is name-shaped -> bare-speaker entry.
    if _looks_like_person(prefix):
        return "", _strip_trailing_punct(prefix), affiliation
    # Otherwise treat the whole prefix as title and speaker as empty.
    return _strip_trailing_punct(prefix), "", affiliation


_NAME_TOKEN_RE = re.compile(r"^[A-ZÀ-Ý][A-Za-zÀ-ÿ'’\-]*[A-Za-zÀ-ÿ\-]?\.?$")


def _looks_like_name_token(tok: str) -> bool:
    """A token that could plausibly be part of a personal name."""
    return bool(_NAME_TOKEN_RE.match(tok))


def _looks_like_person(text: str) -> bool:
    """Heuristic: 1-4 words, each starting upper-case, total ≤32 chars, no
    digits, no all-caps abbreviation token at the end. Matches "Bert Offrein",
    "Ana Filipa Carvalho", "Hernán Furci" but not "LIGENTEC SA" or
    "Industry Talk Session 1: Devices"."""
    s = text.strip()
    if not s or any(ch.isdigit() for ch in s):
        return False
    toks = s.split()
    if not (1 <= len(toks) <= 4):
        return False
    if len(s) > 36:
        return False
    for tok in toks:
        if not _looks_like_name_token(tok):
            return False
        # All-caps token of length 3+ is more company-like than name-like
        # (e.g. "IHP", "UCLA"). We allow short initials like "A." but reject
        # bare all-caps words.
        if len(tok) >= 3 and tok.isupper():
            return False
    return True


def _harvest_block_cells(
    rows: list[dict],
    band: tuple[float, float],
    column: int,
) -> list[tuple[str, str, str, float]]:
    """Walk every row whose top sits in [band[0], band[1]] and pick out the
    column's cell content. Return [(title, speaker, affiliation, top_y), …]
    sorted by Y.

    Rows whose max in-column word-gap is wider than SPEAKER_GAP_PT are SKIPPED
    — those are tech-grid rows (title left + right-aligned speaker chip) that
    bleed into the band (the schedule has one such overflow row at 1330-1345
    in the workshop column). Rows matching a workshop-meta pattern (panel
    discussion, lunch, …) are also dropped.
    """
    col_lo, col_hi = COL_X_RANGES[column]
    out: list[tuple[str, str, str, float]] = []
    for r in rows:
        if not (band[0] <= r["top"] <= band[1]):
            continue
        cell = [
            w for w in r["words"]
            if col_lo <= (w["x0"] + w["x1"]) / 2 < col_hi
            and float(w.get("size", 0)) < TOPIC_FONT_MIN
        ]
        if not cell:
            continue
        cell.sort(key=lambda w: w["x0"])
        # Tech-grid rejection: a tech-grid talk has a giant gap between its
        # title block and the right-aligned speaker chip.
        max_gap = 0.0
        for i in range(1, len(cell)):
            max_gap = max(max_gap, cell[i]["x0"] - cell[i - 1]["x1"])
        if max_gap >= SPEAKER_GAP_PT:
            continue
        text = _join_words(cell)
        if not text:
            continue
        if _WORKSHOP_NON_TALK_RE.match(text):
            continue
        if TRACK_LABEL_RE.match(text.split()[0] if text.split() else ""):
            continue
        title, speaker, aff = _split_industry_cell(text)
        out.append((title, speaker, aff, r["top"]))
    out.sort(key=lambda t: t[3])
    return out


def _harvest_per_slot_talks(
    cells: list[tuple[str, str, str, float]],
    sess_start_min: int,
    sess_end_min: int,
    slot_minutes: int,
) -> list[dict]:
    """For a per-slot industry block: assign each harvested cell to a
    fixed-length time slot, in Y order. The PDF prints six 10-min slots for
    the ECIO industry blocks; this function maps the first cell to
    [start, start+slot_minutes), the second to the next slot, and so on.

    Returns a list of dicts {title, speaker, aff, start_min, end_min}.
    """
    out: list[dict] = []
    cur = sess_start_min
    for (title, speaker, aff, _y) in cells:
        nxt = min(cur + slot_minutes, sess_end_min)
        out.append({
            "title": title, "speaker": speaker, "aff": aff,
            "start_min": cur, "end_min": nxt,
        })
        cur = nxt
        if cur >= sess_end_min:
            break
    return out


def _harvest_session_talks(
    cells: list[tuple[str, str, str, float]],
) -> list[dict]:
    """For a session-wide workshop block: emit one talk per non-empty cell,
    with no per-talk time window (they inherit the session start/end)."""
    return [
        {"title": title, "speaker": speaker, "aff": aff,
         "start_min": None, "end_min": None}
        for (title, speaker, aff, _y) in cells
    ]


# =============================================================================
# Invited-speakers HTML cross-reference
#
# The detailed-schedule PDF prints only the speaker name in each talk cell.
# The conference's public Invited Speakers page is the one source that ties
# each invited speaker to an affiliation, laid out as
#
#   <p><strong>Name</strong></p>
#   <p><em>Affiliation</em></p>
#   <p><strong>"Talk Title"</strong></p>
#
# triples grouped under SC1..SC7 <h2> section headers. We parse these triples
# from the cached HTML and join them to PDF-harvested talks at emission time.
#
# Joining is layered so we never need a hand-curated alias list. For each
# PDF talk we look for the website record whose (in order):
#   1. normalized name matches the PDF speaker exactly, OR
#   2. normalized name is "near" the PDF speaker by Levenshtein <= 2 (this
#      catches PDF spelling drift like Smyth vs Smith), OR
#   3. normalized last-name plus first-initial of the first given name
#      matches (this survives swaps of full given names), OR
#   4. normalized talk title equals the PDF talk title (this attaches the
#      website affiliation to whoever the PDF says is presenting, e.g. when
#      the page lists one PI but the conference talk is given by a group
#      member).
# All passes operate on the same normalized form (NFKD-stripped, lower-
# cased, punctuation-collapsed). No name or title literal lives in this file.
# =============================================================================

# Curly + straight quote chars that wrap talk titles on the WP page.
_INV_QUOTE_CHARS = "“”„‟″‶\"'"
_INV_TITLE_QUOTE_RE = re.compile(f"[{_INV_QUOTE_CHARS}]")
_INV_STRIP_QUOTES_RE = re.compile(
    f"^[{_INV_QUOTE_CHARS}]+|[{_INV_QUOTE_CHARS}]+$")
_INV_SECTION_HEADER_RE = re.compile(r"^SC\d+\b")


# =============================================================================
# the conference planner planner — per-session PRESIDER extraction
#
# The ECIO program is also published on a the conference planner (the conference planner) planner
# at https://ecio2026.abstractcentral.com/planner.jsp . Its expanded DOM is the
# only public ECIO source that lists each technical session's presider(s). Each
# session header paragraph looks like:
#
#   <p class="pagecontents"><strong><b>M1B. Light Emitters</b></strong><br>
#     Presider(s): Raphael Butte (École Polytechnique Fédérale de Lausanne)<br>
#     8:30 AM - 10:15 AM; Room HG E1.1</p>
#
# The leading code (M1B, T2A, W3B, …) is the SAME code the detailed-schedule PDF
# uses for that session, so we key the presider map by it and attach the
# presider to the matching session at emission time.
# =============================================================================
_PLANNER_HEADER_RE = re.compile(
    r'<p class="pagecontents"><strong><b>(?P<codetitle>.*?)</b></strong>'
    r'(?P<rest>.*?)</p>', re.S)
_PLANNER_PRESIDER_RE = re.compile(
    r"Presider\(s\):\s*(?P<raw>.*?)<br", re.S | re.I)


def _planner_strip_tags(s: str) -> str:
    """HTML fragment -> plain text: drop tags, unescape entities, collapse
    whitespace. Used for the planner's session-code and presider strings."""
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_planner_presiders(raw: str) -> list[dict]:
    """Split a presider string 'Name (Aff) and Name2 (Aff2)' into
    [{name, affiliation}, …]. Separators (',', '&', ' and ') only split at
    paren-depth 0 so an affiliation like '(Korea Adv Inst of Sci & Tech)' or
    '(Univ of Science and Technology)' is never torn apart. Mirrors the CLEO
    2026 presider parser."""
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
            if ch == ",":
                if buf.strip():
                    parts.append(buf.strip())
                buf = ""; i += 1; continue
            if ch == "&":
                if buf.strip():
                    parts.append(buf.strip())
                buf = ""; i += 1; continue
            if raw[i:i + 5].lower() == " and ":
                if buf.strip():
                    parts.append(buf.strip())
                buf = ""; i += 5; continue
        buf += ch; i += 1
    if buf.strip():
        parts.append(buf.strip())

    out: list[dict] = []
    for p in parts:
        m = re.match(r"^(.*?)\s*\((.*)\)\s*$", p)
        if m:
            out.append({"name": m.group(1).strip(),
                        "affiliation": m.group(2).strip()})
        else:
            out.append({"name": p.strip(), "affiliation": ""})
    return out


def _load_planner_presiders(path: Path) -> dict[str, dict]:
    """Parse the expanded planner DOM into {session_code: {presider,
    presider_aff}} where both values are '; '-joined and positionally aligned.
    Missing file (or no presider on a session) yields no entry — non-fatal, the
    session just carries no presider."""
    if not path.exists():
        log(f"[planner] {path.name} absent — no presiders will be attached.")
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    out: dict[str, dict] = {}
    for m in _PLANNER_HEADER_RE.finditer(text):
        codetitle = _planner_strip_tags(m.group("codetitle"))
        if "." not in codetitle:
            continue  # non-coded event header (Opening Ceremony, Coffee, …)
        code = re.sub(r"\s+", "", codetitle.split(".", 1)[0])
        pm = _PLANNER_PRESIDER_RE.search(m.group("rest"))
        if not pm:
            continue
        raw = _planner_strip_tags(pm.group("raw"))
        if not raw:
            continue
        presiders = _parse_planner_presiders(raw)
        if not presiders:
            continue
        out[code] = {
            "presider": "; ".join(p["name"] for p in presiders),
            "presider_aff": "; ".join(p["affiliation"] for p in presiders),
        }
    log(f"[planner] parsed presiders for {len(out)} session(s) "
        f"from {path.name}.")
    return out


def _inv_strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _inv_is_title(text: str) -> bool:
    return bool(_INV_TITLE_QUOTE_RE.search(text))


def _inv_strip_title_quotes(s: str) -> str:
    return _INV_STRIP_QUOTES_RE.sub("", s).strip()


def _norm_name(n: str) -> str:
    """Normalize a name for matching: strip accents, lowercase, collapse
    whitespace, drop punctuation. The canonical name (with accents) stays in
    its original form everywhere else."""
    s = unicodedata.normalize("NFKD", n)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _parse_invited_html(html_text: str) -> list[dict]:
    """Return [{"name": str, "affiliation": str, "title": str}, ...] for every
    Name/Affiliation/Title triple on the invited-speakers page. Tolerant of
    nested spans, named/numeric entities, curly vs straight quotes, and the
    SC1..SC7 section headers (which we strip before scanning so they can't
    leak in as ghost "name" tokens)."""
    # Drop heading-level wrappers so SC1.. section headers don't appear as
    # bold runs in our token stream.
    body = re.sub(r"<h[1-6]\b[^>]*>.*?</h[1-6]>", "", html_text,
                  flags=re.IGNORECASE | re.DOTALL)

    # Pull every <strong>...</strong> and <em>...</em> run, in document order.
    pat = re.compile(r"<(strong|em)\b[^>]*>(.*?)</\1>",
                     re.IGNORECASE | re.DOTALL)
    tokens: list[tuple[str, str]] = []  # kind ∈ {"name","title","aff"}
    for m in pat.finditer(body):
        tag = m.group(1).lower()
        text = _inv_strip_tags(m.group(2))
        if not text:
            continue
        if _INV_SECTION_HEADER_RE.match(text):
            continue
        if tag == "em":
            tokens.append(("aff", text))
        else:  # strong
            tokens.append(("title" if _inv_is_title(text) else "name", text))

    # Walk tokens and group into (name, aff, title) records. A new "name"
    # token starts a new record; intervening stray tokens are tolerated.
    records: list[dict] = []
    i = 0
    while i < len(tokens):
        if tokens[i][0] != "name":
            i += 1
            continue
        name = tokens[i][1]
        j = i + 1
        aff = ""
        title = ""
        while j < len(tokens) and tokens[j][0] != "name":
            if tokens[j][0] == "aff" and not aff:
                aff = tokens[j][1]
            elif tokens[j][0] == "title" and not title:
                title = _inv_strip_title_quotes(tokens[j][1])
            j += 1
        if aff or title:
            records.append({"name": name, "affiliation": aff, "title": title})
        i = j
    return records


def _norm_title(t: str) -> str:
    """Normalize a talk title for matching: drop the optional Invited: prefix,
    strip accents, lowercase, collapse non-alphanumeric runs to a single space.
    Aggressive enough that PDF spelling/punctuation drift ("Mid-IR" vs
    "Mid IR", trailing colon, smart quotes) doesn't break title joins."""
    s = t.strip()
    for pfx in _INVITED_PREFIXES:
        if s.startswith(pfx):
            s = s[len(pfx):].strip()
            break
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _name_key(n: str) -> str:
    """Last-name + first-initial fingerprint used by the layered name match.
    'Ann Smith' and 'Ann Smyth' produce the same key; so do
    'Bob Carlo Diaz' and 'Bob Diaz'. Returns '' if the input has no
    usable tokens."""
    toks = _norm_name(n).split()
    if not toks:
        return ""
    if len(toks) == 1:
        return toks[0]
    return f"{toks[-1]}|{toks[0][:1]}"


def _lev_le(a: str, b: str, threshold: int) -> bool:
    """True iff Levenshtein(a, b) <= threshold. Early-exits when the running
    cost passes `threshold`; we never need exact distances above it."""
    if abs(len(a) - len(b)) > threshold:
        return False
    if a == b:
        return True
    # Standard DP with banded early-exit on the row minimum.
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        row_min = cur[0]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min > threshold:
            return False
        prev = cur
    return prev[-1] <= threshold


class InvitedIndex:
    """In-memory index over the invited-speakers HTML records, with a single
    `lookup(speaker, title)` entry point that performs the four-pass match
    described in the section header above. The index is built once at startup
    and returns the affiliation string (or "" when no record joins)."""

    def __init__(self, records: list[dict]) -> None:
        self.records = [r for r in records if r.get("affiliation")]
        self.by_norm_name: dict[str, str] = {}
        self.by_name_key: dict[str, str] = {}
        self.by_norm_title: dict[str, str] = {}
        for r in self.records:
            nm = r["name"]
            aff = r["affiliation"]
            self.by_norm_name.setdefault(_norm_name(nm), aff)
            key = _name_key(nm)
            if key:
                # First record wins; later collisions are rare on a small
                # invited-speakers list and would only fire under last-name+
                # initial collisions, which the exact-match pass handles.
                self.by_name_key.setdefault(key, aff)
            if r.get("title"):
                self.by_norm_title.setdefault(_norm_title(r["title"]), aff)

    def lookup(self, speaker: str, talk_title: str = "") -> str:
        if not speaker and not talk_title:
            return ""
        n = _norm_name(speaker) if speaker else ""
        # Pass 1: exact normalized-name match.
        if n and n in self.by_norm_name:
            return self.by_norm_name[n]
        # Pass 2: fuzzy name (Levenshtein <= 2) on the whole normalized string.
        if n:
            for cand_norm, aff in self.by_norm_name.items():
                if _lev_le(n, cand_norm, 2):
                    return aff
        # Pass 3: last-name + first-initial fingerprint.
        if speaker:
            key = _name_key(speaker)
            if key and key in self.by_name_key:
                return self.by_name_key[key]
        # Pass 4: title-based join — attaches the website affiliation to
        # whoever the PDF says is presenting when the talk title agrees.
        if talk_title:
            t = _norm_title(talk_title)
            if t in self.by_norm_title:
                return self.by_norm_title[t]
        return ""


def _load_invited_index(path: Path) -> InvitedIndex:
    """Parse the cached invited-speakers HTML and return an InvitedIndex
    (empty when the file is missing — the pipeline still produces valid JSON,
    just without invited-speaker affiliations)."""
    if not path.exists():
        log(f"[warn] invited-speakers HTML not found at {path}; "
            f"talks will be emitted without invited-speaker affiliations.")
        return InvitedIndex([])
    records = _parse_invited_html(path.read_text(encoding="utf-8"))
    log(f"[info] parsed {len(records)} entries from {path.name}.")
    return InvitedIndex(records)


# =============================================================================
# Web enrichment: optional HTML pages from the ECIO website that fill in detail
# the detailed-schedule PDF doesn't render. Each parser is tolerant of small
# CMS-block markup drift (extra spans, attribute reordering, &-entities)
# and returns a small typed struct. _load_web_enrichment() ties them together
# into a single `enrichment` dict that main() consults by session_id and
# speaker name. Every individual file is optional: when missing we just log a
# note and leave the corresponding enrichment empty.
# =============================================================================

# Shared HTML helpers ---------------------------------------------------------

# Block-level tags we replace with whitespace when flattening text. The
# explicit inclusion of <br> is what keeps phrasing like `for AI<br>datacenters`
# from collapsing into the single token `AIdatacenters` after tag-strip — the
# WP block editor sometimes wraps inside a single <strong> across a <br>, so
# adjacent text nodes that visibly appear on two lines arrive in our parser
# with no whitespace between them.
_HTML_BLOCK_TAGS = ("p", "div", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6")
_HTML_BLOCK_TAG_RE = re.compile(
    r"</?(?:" + "|".join(_HTML_BLOCK_TAGS) + r")\b[^>]*>",
    re.IGNORECASE,
)


def _html_collapse_whitespace(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _html_strip_to_text(fragment: str) -> str:
    """Return plain text for an HTML fragment: drop tags (block-level tags
    become a space first, so adjacent text nodes that visibly appeared on
    separate lines keep their word boundary), decode entities, normalise
    whitespace."""
    s = _HTML_BLOCK_TAG_RE.sub(" ", fragment)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return _html_collapse_whitespace(s)


def _html_strip_quote_chars(s: str) -> str:
    """Strip curly/straight quote chars from the ends of a title string."""
    return _INV_STRIP_QUOTES_RE.sub("", s).strip()


def _html_extract_main(html_text: str) -> str:
    """Focus parsing on the page body: when a <main>…</main> wrapper is
    present we return its inner content; otherwise we drop the obvious
    non-content chrome (scripts, nav, headers, footers, asides)."""
    m = re.search(r"<main\b[^>]*>(.*?)</main>",
                  html_text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1)
    s = html_text
    for tag in ("script", "style", "nav", "header", "footer", "aside"):
        s = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", "",
                   s, flags=re.IGNORECASE | re.DOTALL)
    return s


# Page parser: plenary speakers -----------------------------------------------

def _parse_plenary_html(html_text: str) -> list[dict]:
    """Return [{name, affiliation, title, abstract, bio}, …] for each plenary
    speaker on the page. The structure is one <h2> per speaker followed by a
    sequence of <p> blocks; we classify each <p> by content (a <p> opening
    with a curly/straight quote char is the talk title; the first short
    non-quoted <p> is the affiliation; the rest is prose). The first prose
    paragraph that opens with the speaker's first name marks the start of
    the bio; everything before is the abstract."""
    body = _html_extract_main(html_text)
    body = re.sub(r"<h1\b[^>]*>.*?</h1>", "",
                  body, flags=re.IGNORECASE | re.DOTALL)
    chunks = re.split(r"(<h2\b[^>]*>.*?</h2>)",
                      body, flags=re.IGNORECASE | re.DOTALL)
    out: list[dict] = []
    for i in range(1, len(chunks), 2):
        head = chunks[i]
        rest = chunks[i + 1] if i + 1 < len(chunks) else ""
        name = _html_strip_to_text(head)
        # Drop a leading "Prof. Dr." / "Dr." / "Prof." honorific so the name
        # matches what the PDF schedule prints in its speaker cells.
        name = re.sub(r"^(?:Prof\.?\s+Dr\.?|Dr\.?|Prof\.?)\s+", "", name)
        paras = re.findall(r"<p\b[^>]*>(.*?)</p>",
                           rest, re.IGNORECASE | re.DOTALL)
        aff = ""
        title = ""
        prose: list[str] = []
        for raw in paras:
            txt = _html_strip_to_text(raw)
            if not txt:
                continue
            if not aff and not _INV_TITLE_QUOTE_RE.search(txt) and len(txt) < 200:
                aff = txt
                continue
            if not title and txt.lstrip()[:1] in _INV_QUOTE_CHARS:
                title = _html_strip_quote_chars(txt)
                continue
            prose.append(txt)
        abstract = ""
        bio = ""
        if prose:
            first_name = name.split()[0] if name else ""
            bio_idx = None
            for j, p in enumerate(prose):
                if first_name and p.startswith(first_name):
                    bio_idx = j
                    break
            if bio_idx is None:
                abstract = "\n\n".join(prose)
            else:
                abstract = "\n\n".join(prose[:bio_idx])
                bio = "\n\n".join(prose[bio_idx:])
        out.append({
            "name": name, "affiliation": aff, "title": title,
            "abstract": abstract, "bio": bio,
        })
    # Defensive: drop records whose "name" doesn't look like a person name
    # (contains a colon, is implausibly long, or matches the SC\d+ section
    # header pattern used elsewhere on the ECIO site).
    out = [r for r in out
           if r["name"] and ":" not in r["name"] and len(r["name"]) <= 60
           and not _INV_SECTION_HEADER_RE.match(r["name"])]
    return out


# Page parser: workshops ------------------------------------------------------

_WORKSHOP_HEAD_RE = re.compile(r"\bworkshop\s*(\d+)?\b", re.IGNORECASE)
_WORKSHOP_PLACEHOLDER_PHRASES = (
    "workshop panelist", "coming soon", "coming soon..", "coming soon ..",
    "coming soon ...",
)


def _parse_workshops_html(html_text: str) -> list[dict]:
    """Return [{position, title, chair, panelists: [{name, aff, talk_title}]}, …]
    for each workshop on the page. The page has 2 workshops, each opening
    with a "WORKSHOP N" paragraph; the H2 immediately after carries the
    workshop topic. Inside each block, panelists are laid out as:
        <p><strong>Name</strong></p>
        <p><em>Affiliation</em></p>
        <p><strong>"Talk Title"</strong></p>     (optional)
    Each row is one <p>; we walk paragraphs in document order and classify
    them by content."""
    body = _html_extract_main(html_text)
    chunks: list[tuple[str, str, str]] = []
    for m in re.finditer(r"<(h2|p)\b[^>]*>(.*?)</\1>",
                         body, re.IGNORECASE | re.DOTALL):
        kind = m.group(1).lower()
        raw = m.group(2)
        plain = _html_strip_to_text(raw)
        if plain:
            chunks.append((kind, raw, plain))

    workshops: list[dict] = []
    cur_workshop: dict | None = None
    cur_panelist: dict | None = None

    def _flush_panelist() -> None:
        nonlocal cur_panelist
        if cur_workshop is not None and cur_panelist is not None and (
            cur_panelist["name"] or cur_panelist["aff"]
        ):
            cur_workshop["panelists"].append(cur_panelist)
        cur_panelist = None

    def _flush_workshop() -> None:
        nonlocal cur_workshop
        _flush_panelist()
        if cur_workshop is not None:
            workshops.append(cur_workshop)
            cur_workshop = None

    for kind, raw, plain in chunks:
        plain_lower = plain.lower()

        # Workshop-header paragraph: short, contains "WORKSHOP N", no chair /
        # panelist keyword.
        m_h = _WORKSHOP_HEAD_RE.search(plain)
        if (kind == "p" and m_h
                and "chair" not in plain_lower
                and "panelist" not in plain_lower
                and len(plain) < 40):
            _flush_workshop()
            pos_str = m_h.group(1)
            position = int(pos_str) if pos_str else len(workshops) + 1
            cur_workshop = {
                "position": position, "title": "", "chair": "",
                "panelists": [],
            }
            continue

        if cur_workshop is None:
            continue

        # Workshop topic from the H2 following the header.
        if kind == "h2" and not cur_workshop["title"]:
            cur_workshop["title"] = plain
            continue

        # Chair line: name follows the colon in the same paragraph.
        if "workshop chair" in plain_lower:
            after = plain.split(":", 1)[1].strip() if ":" in plain else ""
            cur_workshop["chair"] = after
            _flush_panelist()
            continue

        # Placeholder: closes the current panelist without modifying fields.
        if plain_lower in _WORKSHOP_PLACEHOLDER_PHRASES:
            _flush_panelist()
            continue

        strong_texts = [_html_strip_to_text(m.group(1)) for m in re.finditer(
            r"<strong\b[^>]*>(.*?)</strong>", raw,
            re.IGNORECASE | re.DOTALL)]
        em_texts = [_html_strip_to_text(m.group(1)) for m in re.finditer(
            r"<em\b[^>]*>(.*?)</em>", raw,
            re.IGNORECASE | re.DOTALL)]
        strong_texts = [t for t in strong_texts if t]
        em_texts = [t for t in em_texts if t]

        # Affiliation paragraph: italic-only.
        if em_texts and not strong_texts:
            if cur_panelist is not None:
                cur_panelist["aff"] = em_texts[0]
            continue

        # Talk-title paragraph: the plain text begins or ends with a quote.
        stripped = plain.strip()
        if (stripped[:1] in _INV_QUOTE_CHARS
                or stripped[-1:] in _INV_QUOTE_CHARS):
            if cur_panelist is not None:
                cur_panelist["talk_title"] = _html_strip_quote_chars(stripped)
            continue

        # Panelist name: opens a new panelist record.
        if strong_texts:
            _flush_panelist()
            if _WORKSHOP_HEAD_RE.fullmatch(strong_texts[0].strip()):
                continue
            cur_panelist = {
                "name": " ".join(strong_texts).strip(),
                "aff": "", "talk_title": "",
            }
            continue

    _flush_workshop()
    return workshops


# Page parser: Sunday student event -------------------------------------------

def _parse_student_event_html(html_text: str) -> dict:
    """Return {workshop?, bench?, pizza?} dicts for the three sub-events on
    the page. Each carries any of: title, location, start, end. The bench
    record also carries `panelists: [{name, aff}, …]` parsed from the
    <em>Name, Affiliation</em> paragraphs between the bench header and the
    pizza header."""
    body = _html_extract_main(html_text)
    paras = re.findall(r"<p\b[^>]*>(.*?)</p>",
                       body, re.IGNORECASE | re.DOTALL)
    out: dict = {"workshop": {}, "bench": {}, "pizza": {}}
    section: str | None = None
    time_re = re.compile(
        r"(\d{1,2}:\d{2})\s*[-\u2013\u2014]\s*(\d{1,2}:\d{2})")

    def _heading_title(text: str) -> str:
        """Pull the sub-event title out of a heading paragraph of the form
        "<start>-<end> <Title>, <location>": drop the leading time range and
        take the text up to the first comma (the location follows it)."""
        s = text
        mt = time_re.search(s)
        if mt:
            s = s[mt.end():]
        s = s.strip(" \t\u2013\u2014-")
        return s.split(",", 1)[0].strip()

    for raw in paras:
        plain = _html_strip_to_text(raw)
        if not plain:
            continue
        lower = plain.lower()
        # The three sub-events are each introduced by a heading paragraph that
        # contains the sub-event name; we take the title from that heading text
        # rather than hard-coding it, so a re-worded program stays in sync.
        if "workshop on scientific communication" in lower:
            section = "workshop"
            out[section]["title"] = _heading_title(plain)
        elif "bench to business" in lower:
            section = "bench"
            out[section]["title"] = _heading_title(plain)
        elif "pizza dinner" in lower or "networking and pizza" in lower:
            section = "pizza"
            out[section]["title"] = _heading_title(plain)
        else:
            # Continuation: bench panelists are
            # <em>Name, <role…>, <a>Company</a></em>. The name is the text
            # before the first comma; the affiliation is the linked company
            # name (an <a>) — the comma-separated bits between them are job
            # titles (e.g. "Co-Founder, CEO") which we drop. Fall back to the
            # last comma-segment when a panelist line carries no link.
            if section == "bench" and "," in plain and len(plain) < 200:
                name = plain.split(",", 1)[0].strip()
                m_a = re.search(r"<a\b[^>]*>(.*?)</a>",
                                raw, re.IGNORECASE | re.DOTALL)
                if m_a:
                    aff = _html_strip_to_text(m_a.group(1)).strip()
                else:
                    aff = plain.rsplit(",", 1)[-1].strip()
                out[section].setdefault("panelists", []).append(
                    {"name": name, "aff": aff})
            continue
        m_t = time_re.search(plain)
        if m_t:
            out[section]["start"] = m_t.group(1)
            out[section]["end"] = m_t.group(2)
        m_loc = re.search(r"<em\b[^>]*>(.*?)</em>",
                          raw, re.IGNORECASE | re.DOTALL)
        if m_loc:
            loc = _html_strip_to_text(m_loc.group(1))
            if loc and loc.lower() not in ("th",):
                out[section]["location"] = loc
    return out


# Page parser: industry talks -------------------------------------------------

_INDUSTRY_SECTION_RE = re.compile(
    r"<h3\b[^>]*>\s*Session\s*(\d+)\s*:\s*([^<]*)</h3>",
    re.IGNORECASE,
)


def _parse_industry_html(html_text: str) -> list[dict]:
    """Return [{position, label, talks: [{company, name, title}]}, …]. The
    page is laid out as three <h3>Session N: <label></h3> blocks; inside each
    block, talks are introduced by <h4>company</h4> followed by <p>s with the
    talk title (in curly quotes inside a <strong>) and a "Speaker: <name>"
    line."""
    body = _html_extract_main(html_text)
    headers = list(_INDUSTRY_SECTION_RE.finditer(body))
    if not headers:
        return []
    sessions: list[dict] = []
    for i, m in enumerate(headers):
        position = int(m.group(1))
        label = m.group(2).strip()
        block_start = m.end()
        block_end = (headers[i + 1].start()
                     if i + 1 < len(headers) else len(body))
        block = body[block_start:block_end]

        company_split = re.split(
            r"<h4\b[^>]*>(.*?)</h4>", block, flags=re.IGNORECASE | re.DOTALL)
        talks: list[dict] = []
        for j in range(1, len(company_split), 2):
            company = _html_strip_to_text(company_split[j])
            sub = company_split[j + 1] if j + 1 < len(company_split) else ""
            title = ""
            for sm in re.finditer(r"<strong\b[^>]*>(.*?)</strong>",
                                  sub, re.IGNORECASE | re.DOTALL):
                txt = _html_strip_to_text(sm.group(1))
                if not txt:
                    continue
                if _inv_is_title(txt) or txt.lower().startswith("coming soon"):
                    title = _inv_strip_title_quotes(txt)
                    break
            plain = _html_strip_to_text(sub)
            name = ""
            sp = re.search(r"Speaker\s*:\s*([^\n,;.]+)", plain)
            if sp:
                cand = sp.group(1).strip()
                if cand and not cand.lower().startswith("coming soon"):
                    name = cand
            talks.append({"company": company, "name": name, "title": title})
        sessions.append({
            "position": position, "label": label, "talks": talks,
        })
    return sessions


# Page parser: social events --------------------------------------------------

def _parse_social_html(html_text: str) -> list[dict]:
    """Return [{heading, description}, …] for each <h3>-introduced social
    event on the page."""
    body = _html_extract_main(html_text)
    body = re.sub(r"<h1\b[^>]*>.*?</h1>", "",
                  body, flags=re.IGNORECASE | re.DOTALL)
    chunks = re.split(r"(<h3\b[^>]*>.*?</h3>)",
                      body, flags=re.IGNORECASE | re.DOTALL)
    out: list[dict] = []
    for i in range(1, len(chunks), 2):
        heading = _html_strip_to_text(chunks[i])
        sub = chunks[i + 1] if i + 1 < len(chunks) else ""
        paras = re.findall(r"<p\b[^>]*>(.*?)</p>",
                           sub, re.IGNORECASE | re.DOTALL)
        text_paras = []
        for p in paras:
            t = _html_strip_to_text(p)
            if not t:
                continue
            # Skip pure photo-credit paragraphs ("(© …)").
            if t.startswith("(") and t.endswith(")") and "©" in t:
                continue
            text_paras.append(t)
        description = "\n\n".join(text_paras)
        if heading:
            out.append({"heading": heading, "description": description})
    return out


# Page parser: lab tours ------------------------------------------------------

def _parse_lab_tours_html(html_text: str) -> list[dict]:
    """Return [{heading, description}, …] — one record per <h2>-introduced
    visit on the page (ETH Lab Tour / Menhir / Lightium / …). The <p> body
    that follows is the prose description; "Find out more:" footers are
    dropped."""
    body = _html_extract_main(html_text)
    body = re.sub(r"<h1\b[^>]*>.*?</h1>", "",
                  body, flags=re.IGNORECASE | re.DOTALL)
    chunks = re.split(r"(<h2\b[^>]*>.*?</h2>)",
                      body, flags=re.IGNORECASE | re.DOTALL)
    out: list[dict] = []
    for i in range(1, len(chunks), 2):
        heading = _html_strip_to_text(chunks[i])
        sub = chunks[i + 1] if i + 1 < len(chunks) else ""
        paras = re.findall(r"<p\b[^>]*>(.*?)</p>",
                           sub, re.IGNORECASE | re.DOTALL)
        text_paras = []
        for p in paras:
            t = _html_strip_to_text(p)
            if not t:
                continue
            if t.lower().startswith("find out more"):
                continue
            text_paras.append(t)
        description = "\n\n".join(text_paras)
        if heading:
            out.append({"heading": heading, "description": description})
    # Defensive: drop SC\d+-shaped headings (used on the invited-speakers
    # page) and implausibly long headings.
    out = [r for r in out
           if not _INV_SECTION_HEADER_RE.match(r["heading"])
           and len(r["heading"]) <= 100]
    return out


# Web-enrichment loader -------------------------------------------------------

# Mapping of (workshop position) → SKELETON session_id. The codes here must
# match the ones _discover_special_sessions mints (CLEO-style: WS<N>).
_WORKSHOP_POS_TO_SID = {1: "WS1", 2: "WS2"}

# Mapping of (industry-session position) → SKELETON session_id (IND<N>).
_INDUSTRY_POS_TO_SID = {1: "IND1", 2: "IND2", 3: "IND3"}

# Mapping of social-event heading prefix → SKELETON session_id. We match on a
# lowercased prefix because the heading lines also carry a date and time we
# don't want to re-parse.
_SOCIAL_HEADING_TO_SID = {
    "welcome reception": "WLCM",
    "zurich city tour":  "TOUR",
    "gala dinner":       "GALA",
}


# =============================================================================
# Agenda-of-Sessions PDF (one bordered table per day)
#
# Each day is a 3-room grid: column 0 is a "HH:MM—HH:MM" time range, then up to
# three room columns whose header row reads "Room HG F1 | Room HG E1.1 | …".
# A body row is one of:
#   * a SESSION row — its room cells each hold "<CODE> • <Title>" (e.g.
#     "M1A • Electro-Optic Modulators"); the column it sits under names its room.
#   * an EVENT row — a single cell spanning all rooms, holding "<Name>, <Loc>"
#     (e.g. "Welcome Reception, ETH Uhrenhalle" or "Coffee Break, Foyers in
#     front of Session Rooms"). The name may itself carry a leading code+bullet
#     ("T4A • Plenary Session II, HG F30 …").
# We read the grid with pdfplumber's table extractor (the borders make this far
# more robust than word-clustering) and return, per day: a {code -> room} map
# and a list of structured events. Only the table SHAPE is encoded here — every
# string of program content is read from the file at runtime.
# =============================================================================
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
# Time range in a left-hand agenda cell: "HH:MM—HH:MM" with an em-dash or hyphen
# and optional surrounding spaces (the PDF is inconsistent: "13:00 —16:30",
# "15:30—16:30").
_AGENDA_TIME_RE = re.compile(
    r"^(\d{1,2}):(\d{2})\s*[—–-]\s*(\d{1,2}):(\d{2})$")
# Day header in the first cell of a table: "Monday, 15 June".
_AGENDA_DAY_RE = re.compile(
    r"^\s*[A-Za-z]+,\s*(\d{1,2})\s+([A-Za-z]+)\s*$")
# A room-column header cell: "Room HG F1". The label after "Room " is the room
# name we attach to every session/event sitting in that column.
_AGENDA_ROOM_RE = re.compile(r"^\s*Room\s+(.+?)\s*$", re.IGNORECASE)
# A leading "<CODE> • " on an event/session cell. The bullet is U+2022; ECIO
# also occasionally renders it as "*". The code is the program's own session
# label (M1A, P11, T4A, …) — format, not content.
_AGENDA_CODE_RE = re.compile(r"^\s*([A-Za-z]{1,3}\d{1,3}[A-Za-z]?)\s*[•*]\s*(.*)$")
# Placeholder "locations" that carry no real venue — treated as no location.
_AGENDA_NO_LOC = {"", "location to be announced", "tba", "to be announced"}

# Generic event-kind classifier. Each kind is a universal event genre (NOT
# conference-specific content), keyed by a substring test on the lower-cased
# event name. `new_row` kinds are daily logistics the detailed grid omits and
# we synthesize as standalone Event sessions; the rest only *fill* the location
# of an existing session of the same kind on the same day. `type_label` is the
# Type tag shown for a synthesized row.
_AGENDA_KINDS = [
    # (kind, substrings, new_row, type_label)
    ("registration", ("registration",),          True,  "Registration"),
    ("coffee",       ("coffee",),                 True,  "Coffee Break"),
    ("lunch",        ("lunch",),                  True,  "Lunch"),
    ("opening",      ("opening",),                False, None),
    ("closing",      ("closing",),                False, None),
    ("plenary",      ("plenary",),                False, None),
    ("poster",       ("poster",),                 False, None),
    ("welcome",      ("welcome",),                False, None),
    ("gala",         ("gala",),                   False, None),
    ("lab",          ("lab tour", "company vis"), False, None),
    ("tour",         ("tour", "city"),            False, None),
    ("student",      ("student",),                False, None),
    ("bench",        ("bench to business",),      False, None),
    ("pizza",        ("pizza", "networking"),     False, None),
]


def _agenda_kind(name: str) -> tuple[str, bool, str | None] | None:
    """Classify an event name into a generic kind. Returns (kind, new_row,
    type_label) or None when nothing matches. The poster+coffee combo rows
    ("Poster Session I and Coffee Break") classify as `poster`, not `coffee`,
    because `poster` is tested before `coffee` only after we special-case it:
    we check `poster` membership first here so a combined row never spawns a
    spurious standalone coffee break."""
    low = name.lower()
    if "poster" in low:
        return ("poster", False, None)
    for kind, subs, new_row, label in _AGENDA_KINDS:
        if any(s in low for s in subs):
            return (kind, new_row, label)
    return None


def _agenda_split_name_loc(cell: str) -> tuple[str, str, str]:
    """Split an agenda event/session cell into (code, name, location).

    The cell shape is "[<CODE> • ]<Name>[, <Location>]". The location, when
    present, follows the FIRST comma (event names in this grid carry no internal
    commas). Stray punctuation the PDF emits — a doubled comma
    ("Symposium,, HG F30") or a space before the comma ("Coffee Break ,Foyers")
    — is normalized away. A placeholder location ("Location to be Announced")
    becomes empty."""
    text = re.sub(r"\s+", " ", (cell or "").replace("\n", " ")).strip()
    code = ""
    m = _AGENDA_CODE_RE.match(text)
    if m:
        code, text = m.group(1), m.group(2).strip()
    name, loc = text, ""
    if "," in text:
        head, tail = text.split(",", 1)
        name = head.strip()
        loc = tail.strip(" ,").strip()
    if loc.lower() in _AGENDA_NO_LOC:
        loc = ""
    return code, name, loc


def _parse_agenda_pdf(path: Path) -> dict:
    """Parse the agenda-of-sessions PDF into {rooms_by_code, events}.

    `rooms_by_code` maps each session code (M1A, P11, …) to its room label.
    `events` is a list of {day_iso, start, end, name, location, kind, new_row,
    type_label, code} for every single-cell (all-room-spanning) event row.
    Returns empty structures on any failure — the agenda is optional enrichment.
    """
    import pdfplumber
    rooms_by_code: dict[str, str] = {}
    events: list[dict] = []
    year = _conference_year(CONFERENCE_NAME)
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                day_iso = ""
                room_by_col: dict[int, str] = {}
                for row in table:
                    cells = [(c or "").replace("\n", " ").strip() for c in row]
                    if not cells:
                        continue
                    first = cells[0]
                    # Day header row: "Monday, 15 June" in the first cell.
                    dm = _AGENDA_DAY_RE.match(first)
                    if dm and not any(cells[1:]):
                        day, mon = int(dm.group(1)), dm.group(2).lower()
                        if mon in _MONTHS:
                            day_iso = f"{year:04d}-{_MONTHS[mon]:02d}-{day:02d}"
                        continue
                    # Room-header row: first cell empty, the rest "Room <X>".
                    if not first:
                        rm = {i: _AGENDA_ROOM_RE.match(c).group(1).strip()
                              for i, c in enumerate(cells)
                              if _AGENDA_ROOM_RE.match(c)}
                        if rm:
                            room_by_col = rm
                            continue
                    tm = _AGENDA_TIME_RE.match(first)
                    if not tm:
                        continue
                    start = f"{int(tm.group(1)):02d}:{tm.group(2)}"
                    end = f"{int(tm.group(3)):02d}:{tm.group(4)}"
                    filled = [(i, c) for i, c in enumerate(cells[1:], 1) if c]
                    if len(filled) == 1:
                        # Spanning EVENT row (single cell across the rooms).
                        code, name, loc = _agenda_split_name_loc(filled[0][1])
                        if not name:
                            continue
                        kind = _agenda_kind(name)
                        if code and loc:
                            rooms_by_code[code] = loc
                        events.append({
                            "day_iso": day_iso, "start": start, "end": end,
                            "name": name, "location": loc, "code": code,
                            "kind": kind[0] if kind else "",
                            "new_row": bool(kind and kind[1]),
                            "type_label": (kind[2] if kind else None),
                        })
                    else:
                        # SESSION row: each room cell is "<CODE> • <Title>".
                        for col, cell in filled:
                            code, _name, _loc = _agenda_split_name_loc(cell)
                            room = room_by_col.get(col, "")
                            if code and room:
                                rooms_by_code.setdefault(code, room)
    return {"rooms_by_code": rooms_by_code, "events": events}


def _load_agenda() -> dict:
    """Read the agenda PDF if present; otherwise return empty structures so the
    caller falls back to PDF/HTML-derived locations only."""
    empty = {"rooms_by_code": {}, "events": []}
    if not INPUT_AGENDA_PDF.exists():
        log(f"[warn] agenda PDF not found at {INPUT_AGENDA_PDF.name}; session "
            f"locations come from the detailed schedule only, and the daily "
            f"Registration/Coffee/Lunch rows are not added.")
        return empty
    try:
        agenda = _parse_agenda_pdf(INPUT_AGENDA_PDF)
    except Exception as e:  # noqa: BLE001 — optional source, never fatal
        log(f"[warn] could not parse the agenda PDF ({e}); continuing without "
            f"its locations and logistics rows.")
        return empty
    log(f"[info] agenda PDF         : {len(agenda['rooms_by_code'])} session "
        f"room(s), {len(agenda['events'])} event row(s) parsed.")
    return agenda


# =============================================================================
# Optica schedule pages (per-day "Detailed View" HTML)
#
# These three pages mirror the full program and, unlike the detailed-schedule
# PDF, expose for every talk: the complete author list with each author's
# affiliation, and the abstract. The DOM is regular and class-driven:
#
#     li.session                          one session block
#       span.session__code                session code  (e.g. "M1A")
#       span.session__track               sub-committee  (e.g. "| SC1")
#       h5.session__title                 session title
#       li.presentation                   one talk
#         p.mb-0                          talk code      (e.g. "M1A.1")
#         h6                              talk title
#         .media-body                     "Presenter: <name>" (often blank)
#         p.presentation__description     abstract, then a trailing
#                                         "<strong>Authors</strong>: <blob>"
#
# where the author blob lists authors as "Name, Affiliation[, address…]"
# joined by " / ". We parse those into structured records and key them by
# session code (for the oral tech tracks, whose JSON session id equals the
# Optica code) and by normalized session title (for the poster sessions, whose
# JSON id and Optica code differ). Selectors describe FORMAT, not content.
# =============================================================================
_OPTICA_CODE_RE = re.compile(r"^([A-Za-z0-9]+)\.(\d+)$")


def _clean_optica_aff(aff: str) -> str:
    """Trim an Optica affiliation string. The site appends the full postal
    address after the institution (shape: "<Org>, <Dept>, <house-number>
    <Street>, <City>"); we drop comma-segments that begin with a house number
    so the affiliation map keys on the institution rather than the street.
    Institution/department segments (no leading digit) are kept and re-joined;
    the builder's keyword shortener picks the canonical one."""
    kept = []
    for seg in aff.split(","):
        seg = seg.strip()
        if not seg or re.match(r"^\d", seg):
            continue
        kept.append(seg)
    return ", ".join(kept).strip(" ,")


def _parse_optica_authors(blob: str) -> list[tuple[str, str]]:
    """Split the "<strong>Authors</strong>:" blob into (name, affiliation)
    pairs. Authors are joined by " / "; within each, the text before the first
    comma is the name and the remainder is the (address-trimmed) affiliation."""
    out: list[tuple[str, str]] = []
    for chunk in blob.split("/"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "," in chunk:
            name, aff = chunk.split(",", 1)
            out.append((name.strip(), _clean_optica_aff(aff)))
        else:
            out.append((chunk, ""))
    return [(n, a) for n, a in out if n]


# Superscript / subscript Unicode maps, used to flatten <sup>/<sub> inside an
# abstract (units and chemical formulae: "0.54 W<sup>-1</sup>m<sup>-1</sup>" ->
# "0.54 W⁻¹m⁻¹", "Si<sub>3</sub>N<sub>4</sub>" -> "Si₃N₄"). Anything outside the
# mappable set falls back to a "^"/"_" prefix so no character is dropped.
_SUP_SRC = "0123456789+-=()n"
_SUB_SRC = "0123456789+-=()"
_SUP_MAP = str.maketrans(_SUP_SRC, "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ")
_SUB_MAP = str.maketrans(_SUB_SRC, "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")


def _optica_inline_text(fragment_html: str) -> str:
    """Flatten an Optica description HTML fragment to plain text WITHOUT
    splitting inline markup onto separate lines. <sup>/<sub> become Unicode
    super/subscripts (so "W<sup>-1</sup>" reads "W⁻¹" rather than the
    newline-broken "W\\n-1" a naive get_text('\\n') produces); <br> becomes a
    space; all other tags contribute their text inline."""
    from bs4 import BeautifulSoup

    def _script(txt: str, src: str, table: dict, prefix: str) -> str:
        # Map the tag's CORE text (ignoring surrounding whitespace — the page
        # sometimes tucks a stray space inside, e.g. "<sub>4 </sub>") to Unicode
        # super/subscripts, preserving that whitespace. Fall back to a prefixed
        # literal when any core character isn't mappable.
        core = txt.strip()
        if core and all(c in src for c in core):
            lead = txt[:len(txt) - len(txt.lstrip())]
            trail = txt[len(txt.rstrip()):]
            return f"{lead}{core.translate(table)}{trail}"
        return f"{prefix}{txt}"

    node = BeautifulSoup(fragment_html, "html.parser")
    for sup in node.find_all("sup"):
        sup.replace_with(_script(sup.get_text(), _SUP_SRC, _SUP_MAP, "^"))
    for sub in node.find_all("sub"):
        sub.replace_with(_script(sub.get_text(), _SUB_SRC, _SUB_MAP, "_"))
    for br in node.find_all("br"):
        br.replace_with(" ")
    return re.sub(r"\s+", " ", node.get_text("")).strip()


def _optica_split_desc(desc) -> tuple[str, list]:
    """Split a presentation__description node into (abstract, authors). The
    description is the abstract followed by a trailing
    "<strong>Authors</strong>: <name, aff / …>" block; we split on that marker
    in the HTML (not the flattened text) so the abstract's own inline markup is
    preserved by _optica_inline_text."""
    inner = desc.decode_contents()
    parts = re.split(r"<strong>\s*Authors\s*</strong>\s*:?",
                     inner, maxsplit=1, flags=re.IGNORECASE)
    abstract = _optica_inline_text(parts[0])
    authors: list = []
    if len(parts) > 1:
        from bs4 import BeautifulSoup
        atext = re.sub(
            r"\s+", " ",
            BeautifulSoup(parts[1], "html.parser").get_text(" ")).strip()
        authors = _parse_optica_authors(atext)
    return abstract, authors


def _parse_optica_day_html(html_text: str) -> list[dict]:
    """Parse one Optica schedule day page into a list of session dicts:

        {code, title, track, presentations: [
            {code, num, title, presenter, abstract,
             authors: [(name, aff), …]}, …]}

    Driven entirely by the page's CSS classes (see the section header)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_text, "html.parser")
    sessions: list[dict] = []
    for li in soup.select("li.session"):
        code_el = li.select_one(".session__code")
        title_el = li.select_one(".session__title")
        track_el = li.select_one(".session__track")
        track = ""
        if track_el:
            track = track_el.get_text(" ", strip=True).lstrip("| ").strip()
        presentations: list[dict] = []
        for p in li.select("li.presentation"):
            col = p.select_one(".col-md-8") or p
            code_p = col.select_one("p.mb-0")
            pcode = code_p.get_text(strip=True) if code_p else ""
            h6 = col.select_one("h6")
            ptitle = h6.get_text(" ", strip=True) if h6 else ""
            presenter = ""
            mb = col.select_one(".media-body")
            if mb:
                spans = mb.find_all("span")
                if len(spans) >= 2:
                    presenter = spans[1].get_text(" ", strip=True)
            abstract, authors = "", []
            desc = col.select_one("p.presentation__description")
            if desc:
                abstract, authors = _optica_split_desc(desc)
            mnum = _OPTICA_CODE_RE.match(pcode)
            presentations.append({
                "code": pcode,
                "num": int(mnum.group(2)) if mnum else None,
                "title": ptitle,
                "presenter": presenter,
                "abstract": abstract,
                "authors": authors,
            })
        sessions.append({
            "code": code_el.get_text(strip=True) if code_el else "",
            "title": title_el.get_text(" ", strip=True) if title_el else "",
            "track": track,
            "presentations": presentations,
        })
    return sessions


def _load_optica_enrichment() -> dict:
    """Parse the per-day Optica pages into lookup indices:
        by_code  : session code (e.g. 'M1A') -> session dict
        by_title : normalized session title  -> session dict
    plus n_pres (total presentations parsed) for logging. Missing files are
    skipped silently here; the caller logs the overall presence."""
    _bootstrap_bs4()
    by_code: dict[str, dict] = {}
    by_title: dict[str, dict] = {}
    n_pres = 0
    for path in INPUT_OPTICA_HTML:
        if not path.exists():
            continue
        for s in _parse_optica_day_html(path.read_text(encoding="utf-8")):
            n_pres += len(s["presentations"])
            if s["code"]:
                by_code.setdefault(s["code"], s)
            if s["title"]:
                by_title.setdefault(_norm_title(s["title"]), s)
    return {"by_code": by_code, "by_title": by_title, "n_pres": n_pres}


def _match_optica_talks(pdf_talks: list[dict],
                        opt_pres: list[dict]) -> dict[int, dict]:
    """Match PDF-harvested talks to Optica presentations within one session.
    Greedy by descending title similarity (so a confident title join wins over
    position), then a positional fallback pairs any leftovers when the counts
    line up — this rescues talks whose PDF title is OCR-mangled or truncated
    but whose order is unambiguous. Returns {pdf_index: optica_record}."""
    pairs: list[tuple[float, int, int]] = []
    for i, pt in enumerate(pdf_talks):
        nt = _norm_title(pt.get("title", ""))
        for j, op in enumerate(opt_pres):
            ratio = difflib.SequenceMatcher(
                None, nt, _norm_title(op["title"])).ratio()
            pairs.append((ratio, i, j))
    pairs.sort(key=lambda x: x[0], reverse=True)
    used_i: set[int] = set()
    used_j: set[int] = set()
    out: dict[int, dict] = {}
    for ratio, i, j in pairs:
        if ratio < 0.5:
            break
        if i in used_i or j in used_j:
            continue
        out[i] = opt_pres[j]
        used_i.add(i)
        used_j.add(j)
    left_i = [i for i in range(len(pdf_talks)) if i not in used_i]
    left_j = [j for j in range(len(opt_pres)) if j not in used_j]
    if left_i and len(left_i) == len(left_j):
        for i, j in zip(left_i, left_j):  # both ascending
            out[i] = opt_pres[j]
    return out


def _author_index_of(speaker: str, authors: list[dict]) -> int | None:
    """Index of the presenting speaker within an author list, matched by the
    last-name+initial fingerprint (tolerant of accents and middle names).
    Returns None when no author matches."""
    key = _name_key(speaker)
    if not key:
        return None
    for idx, a in enumerate(authors):
        if _name_key(a.get("name", "")) == key:
            return idx
    return None


def _load_web_enrichment() -> dict:
    """Read all optional enrichment HTML files into one structured dict that
    main() consults during emission. Any missing file leaves its branch empty
    (with a warning) — emission falls back to whatever the PDF gives us."""
    enrich: dict = {
        "plenary": {}, "workshops": {}, "student": {},
        "industry": {}, "social": {}, "lab_tours": [],
        "optica": {"by_code": {}, "by_title": {}, "n_pres": 0},
        "agenda": {"rooms_by_code": {}, "events": []},
    }

    enrich["agenda"] = _load_agenda()

    if INPUT_PLENARY_HTML.exists():
        recs = _parse_plenary_html(INPUT_PLENARY_HTML.read_text(encoding="utf-8"))
        log(f"[info] plenary HTML       : {len(recs)} speaker(s) parsed.")
        for r in recs:
            if r["name"]:
                enrich["plenary"][_norm_name(r["name"])] = r
    else:
        log(f"[warn] plenary HTML not found at {INPUT_PLENARY_HTML.name}; "
            f"plenary talks will use PDF placeholder titles only.")

    if INPUT_WORKSHOPS_HTML.exists():
        recs = _parse_workshops_html(
            INPUT_WORKSHOPS_HTML.read_text(encoding="utf-8"))
        log(f"[info] workshops HTML     : {len(recs)} workshop(s) parsed.")
        for r in recs:
            sid = _WORKSHOP_POS_TO_SID.get(r["position"])
            if sid:
                enrich["workshops"][sid] = r
    else:
        log(f"[warn] workshops HTML not found; workshop sessions will use "
            f"PDF-harvested titles + panellists only.")

    if INPUT_STUDENT_HTML.exists():
        rec = _parse_student_event_html(
            INPUT_STUDENT_HTML.read_text(encoding="utf-8"))
        present = [k for k, v in rec.items() if v]
        log(f"[info] student-event HTML : sub-events parsed: {present}")
        enrich["student"] = rec
    else:
        log(f"[warn] student-event HTML not found; Sunday student-event "
            f"talks will use SKELETON defaults only (no Bench-to-Business "
            f"panellists, no website-derived time overrides).")

    if INPUT_INDUSTRY_HTML.exists():
        recs = _parse_industry_html(
            INPUT_INDUSTRY_HTML.read_text(encoding="utf-8"))
        log(f"[info] industry HTML      : {len(recs)} session(s), "
            f"{sum(len(r['talks']) for r in recs)} talk(s) parsed.")
        for r in recs:
            sid = _INDUSTRY_POS_TO_SID.get(r["position"])
            if sid:
                enrich["industry"][sid] = r
    else:
        log(f"[warn] industry-talks HTML not found; industry sessions will "
            f"use the noisier PDF-harvested talk cells.")

    if INPUT_SOCIAL_HTML.exists():
        recs = _parse_social_html(
            INPUT_SOCIAL_HTML.read_text(encoding="utf-8"))
        log(f"[info] social-events HTML : {len(recs)} event(s) parsed.")
        for r in recs:
            lower = r["heading"].lower()
            for prefix, sid in _SOCIAL_HEADING_TO_SID.items():
                if lower.startswith(prefix):
                    enrich["social"][sid] = r["description"]
                    break
    else:
        log(f"[warn] social-events HTML not found; social-event sessions "
            f"will be emitted without descriptions.")

    if INPUT_LABS_HTML.exists():
        recs = _parse_lab_tours_html(
            INPUT_LABS_HTML.read_text(encoding="utf-8"))
        log(f"[info] lab-tours HTML     : {len(recs)} visit option(s) parsed.")
        enrich["lab_tours"] = recs
    else:
        log(f"[warn] lab-tours HTML not found; the lab-tours session will "
            f"be emitted as a single SKELETON entry with no talk options.")

    present = [p.name for p in INPUT_OPTICA_HTML if p.exists()]
    if present:
        enrich["optica"] = _load_optica_enrichment()
        log(f"[info] Optica schedule    : {len(present)} day page(s), "
            f"{enrich['optica']['n_pres']} presentation(s) parsed "
            f"({len(enrich['optica']['by_code'])} session code(s)).")
    else:
        log(f"[warn] Optica schedule pages not found; oral talks keep their "
            f"PDF-only authors (no abstracts) and poster sessions stay empty.")

    return enrich


# =============================================================================
# Driver
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
            tags.append({"key": "Type", "value": fmt})
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
                tags.append({"key": "Track", "value": topic})
        if tags:
            s["tags"] = tags
    return sessions


def main() -> None:
    _bootstrap_pdfplumber()
    log("=" * 72)
    log(f"[config] ECIO 2026 PROCESSOR")
    log(f"[config]   input PDF       : {INPUT_PDF}")
    log(f"[config]   invited HTML    : {INPUT_INVITED_HTML}")
    log(f"[config]   enrichment HTML : {DATA_DIR} (6 optional files)")
    log(f"[config]   output          : {OUTPUT_JSON}")
    log("=" * 72)

    if not INPUT_PDF.exists():
        log(f"[fatal] required input not found: {INPUT_PDF}")
        sys.exit(1)

    # Load the InvitedIndex over the cached invited-speakers page. Missing
    # file is non-fatal: the pipeline still produces valid JSON, just without
    # invited-speaker affiliations.
    invited_idx = _load_invited_index(INPUT_INVITED_HTML)

    # Load the optional web-enrichment HTML pages. Each is independently
    # optional; missing ones leave their branch of `enrich` empty and the
    # session/talk pipeline falls back to whatever the PDF harvested.
    log("-" * 72)
    log("[info] loading optional web-enrichment pages …")
    enrich = _load_web_enrichment()

    # Per-session presiders parsed from the the conference planner planner DOM. Keyed by
    # session code (M1B, T2A, …) — the same code the PDF skeleton emits as a
    # session id — so attaching them below is a direct dict lookup. Optional.
    planner_presiders = _load_planner_presiders(INPUT_PLANNER_HTML)

    import pdfplumber
    log(f"[info] reading {INPUT_PDF.name} …")
    with pdfplumber.open(INPUT_PDF) as pdf:
        page = pdf.pages[0]
        page_h = float(page.height)
    words = _extract_words(INPUT_PDF)
    log(f"[info]   page height {page_h:.1f}; {len(words):,} words extracted.")

    rows = _cluster_rows(words)
    bands = _day_y_bands(rows, page_h)
    log(f"[info]   day bands:")
    for k, (a, b) in bands.items():
        log(f"          {k}: y=[{a:.1f}, {b:.1f}]")

    # Runtime-discovered day -> ISO date map, column -> room map, and
    # plenary room label. None of this is hardcoded in source.
    day_isos = _discover_day_isos(rows, _conference_year(CONFERENCE_NAME))
    room_by_col = _discover_room_cols(rows)
    plenary_room = _discover_plenary_room(rows)
    log(f"[info]   day ISO dates : {day_isos}")
    log(f"[info]   column rooms  : {room_by_col}")
    log(f"[info]   plenary room  : {plenary_room!r}")

    # Build the session SKELETON from the PDF.
    skeleton = _discover_skeleton(
        rows, words, page_h, day_isos, bands, room_by_col, plenary_room)
    log(f"[info]   discovered {len(skeleton)} sessions from the PDF.")

    # ---- Build sessions + talks --------------------------------------------
    sessions_out: list[dict] = []
    talks_out: list[dict] = []
    affiliations_pool: set[str] = set()
    # Telemetry: how many talks had their affiliation filled from the cached
    # invited-speakers page (vs already-present from the PDF harvest).
    invited_filled_count = 0
    invited_filled_speakers: list[str] = []

    for sess in skeleton:
        day_key = sess["day"]
        day_iso = day_isos[day_key]
        start_iso = f"{day_iso}T{sess['start']}:00"
        end_iso = f"{day_iso}T{sess['end']}:00"
        room = sess.get("room") or room_by_col.get(sess.get("column", 0), "")
        day_band = bands.get(day_key)

        # ---- Resolve the session's display title --------------------------
        # Precedence: explicit `title` -> `pdf_title` directive -> topic
        # header above this session's column (default for tech tracks) ->
        # the track code as a last-resort label.
        title = sess.get("title", "").strip()
        if not title:
            spec = sess.get("pdf_title")
            if spec and spec.get("source") == "row_text":
                title = _read_pdf_title(rows, spec)
            elif (spec and spec.get("source") == "topic_header"
                  and day_band):
                s_min = _hhmm_to_minutes(sess["start"])
                e_min = _hhmm_to_minutes(sess["end"])
                slots = _session_time_slots(words, day_band, s_min, e_min)
                y_range = _harvest_session_y_range(slots, day_band)
                title = _topic_header_title(rows, y_range, spec["column"])
            elif "column" in sess and day_band:
                # Default for tech-track sessions: topic header above the
                # column at this session's Y.
                s_min = _hhmm_to_minutes(sess["start"])
                e_min = _hhmm_to_minutes(sess["end"])
                slots = _session_time_slots(words, day_band, s_min, e_min)
                y_range = _harvest_session_y_range(slots, day_band)
                title = _topic_header_title(rows, y_range, sess["column"])
        if not title:
            title = sess.get("track", "") or "(untitled session)"
            log(f"[warn] no title resolved for {sess['id']}; "
                f"falling back to {title!r}")

        topic_parts = []
        if sess.get("track"):
            topic_parts.append(sess["track"])
        topic = " · ".join(topic_parts) if topic_parts else ""

        s_obj: dict = {
            "id": sess["id"],
            "title": title,
            "color": sess["color"],
            "type": sess["type"],
            "start_ts": start_iso,
            "end_ts": end_iso,
            "talk_ids": [],
        }
        if room:
            s_obj["location"] = room
        if topic:
            s_obj["topic"] = topic
        # Attach the planner-sourced presider(s), if any, keyed by session id
        # (== the planner's session code). The builder shortens presider_aff
        # and backfills any missing affiliation from papers the presider
        # authored, so we emit the RAW affiliation string here.
        pres = planner_presiders.get(sess["id"])
        if pres and pres.get("presider"):
            s_obj["presider"] = pres["presider"]
            if pres.get("presider_aff"):
                s_obj["presider_aff"] = pres["presider_aff"]
                # Pool the presider affiliation(s) into affiliation_sources too,
                # so the builder's affiliation map canonicalizes them (e.g.
                # "Technische Universität Berlin" -> "TU Berlin"). The field is a
                # '; '-joined list of co-presider affiliations, so split it into
                # individual strings the same way the talk-author pool stores
                # them; without this the presider strings never reach the map and
                # the builder leaves them in their long raw form.
                for _paff in pres["presider_aff"].split(";"):
                    _paff = _paff.strip()
                    if _paff:
                        affiliations_pool.add(_paff)
        sessions_out.append(s_obj)

        # ---- Collect this session's talks
        # Each entry is a dict with these keys (any may be empty/None):
        #   title         : the talk title
        #   speaker       : presenting-author name (becomes the first author)
        #   aff           : presenting-author affiliation
        #   is_invited    : True for "Invited:" tech-track talks
        #   color         : color override (e.g. "indigo" for industry/workshop)
        #                   or None to derive from is_invited downstream
        #   start_min/end_min: per-talk timing in minutes-since-midnight, or
        #                   None to inherit the session's start/end
        #   abstract      : optional abstract/bio prose
        #   extra_authors : optional [{name, aff}, …] appended to the author
        #                   list. Used for multi-author talks such as the
        #                   Bench-to-Business symposium panellist roster.
        talks_for_session: list[dict] = []

        if "talks" in sess:
            # Hand-listed talks (plenary speakers + Sunday components).
            for t in sess["talks"]:
                ts = t.get("start")
                te = t.get("end")
                talks_for_session.append({
                    "title": t.get("title", "").strip(),
                    "speaker": t.get("speaker", "").strip(),
                    "aff": t.get("speaker_aff", "").strip(),
                    "is_invited": False,
                    "color": t.get("color"),
                    "start_min": _hhmm_to_minutes(ts) if ts else None,
                    "end_min": _hhmm_to_minutes(te) if te else None,
                    "abstract": "",
                    "extra_authors": [],
                })
        elif "harvest" in sess:
            # Non-grid harvest (industry talks + workshops). Walks the entire
            # band as a block, parsing "Title. Speaker, Affiliation" cells.
            if not day_band:
                log(f"[warn] no day band for {day_key}; skipping {sess['id']}")
                continue
            s_min = _hhmm_to_minutes(sess["start"])
            e_min = _hhmm_to_minutes(sess["end"])
            slots = _session_time_slots(words, day_band, s_min, e_min)
            # For "session" mode (workshops), there may be no slot rows in the
            # session's band (workshops just use the session-wide time). Fall
            # back to a Y range derived from the session's own time bounds.
            if slots:
                y_range = _harvest_session_y_range(slots, day_band)
            else:
                y_range = day_band
            harvest = sess["harvest"]
            cells = _harvest_block_cells(rows, y_range, harvest["column"])
            color_override = harvest.get("talk_color", "indigo")
            if harvest.get("slot_mode") == "per_slot":
                slot_minutes = int(harvest.get("slot_minutes", 10))
                parsed = _harvest_per_slot_talks(
                    cells, s_min, e_min, slot_minutes)
            else:
                parsed = _harvest_session_talks(cells)
            for p in parsed:
                if not (p["title"] or p["speaker"] or p["aff"]):
                    continue
                talks_for_session.append({
                    "title": p["title"],
                    "speaker": p["speaker"],
                    "aff": p["aff"],
                    "is_invited": False,
                    "color": color_override,
                    "start_min": p["start_min"],
                    "end_min": p["end_min"],
                    "abstract": "",
                    "extra_authors": [],
                })
        elif "column" in sess:
            # Tech-grid harvest (title left, right-aligned speaker chip).
            if not day_band:
                log(f"[warn] no day band for {day_key}; skipping {sess['id']}")
                continue
            s_min = _hhmm_to_minutes(sess["start"])
            e_min = _hhmm_to_minutes(sess["end"])
            slots = _session_time_slots(words, day_band, s_min, e_min)
            y_range = _harvest_session_y_range(slots, day_band)
            col_x = COL_X_RANGES[sess["column"]]
            lines = _extract_cell_lines(rows, col_x, y_range)
            for title_raw, speaker_raw, y in lines:
                t_title, is_invited = _clean_title(title_raw)
                speaker = _clean_speaker(speaker_raw)
                if not t_title and not speaker:
                    continue
                t_start, t_end = _talk_time_window(
                    y, slots, s_min, e_min, is_invited=is_invited)
                talks_for_session.append({
                    "title": t_title, "speaker": speaker, "aff": "",
                    "is_invited": is_invited, "color": None,
                    "start_min": t_start, "end_min": t_end,
                    "abstract": "", "extra_authors": [],
                })

        # ---- Apply web enrichment overrides (when the corresponding HTML
        # page was present and parsed). Each branch below either *augments*
        # PDF-derived talks (e.g. attaching an abstract to a plenary lecture)
        # or *replaces* them entirely (e.g. swapping in the website's clean
        # industry-talk list for the noisy PDF cell harvest). The session
        # object itself can also gain a `description` (social events) or
        # `chair` note in its topic (workshops) here.
        sid = sess["id"]

        # Plenary: augment the hand-listed lecture with the website's title,
        # affiliation, abstract, and bio.
        if sess.get("type") == "Plenary" and enrich["plenary"]:
            for tk in talks_for_session:
                rec = enrich["plenary"].get(_norm_name(tk["speaker"]))
                if not rec:
                    continue
                if rec.get("title"):
                    tk["title"] = rec["title"]
                if rec.get("affiliation") and not tk["aff"]:
                    tk["aff"] = rec["affiliation"]
                # Concatenate abstract + bio into one prose field (the talk
                # schema only renders a single abstract block).
                pieces = []
                if rec.get("abstract"):
                    pieces.append(rec["abstract"])
                if rec.get("bio"):
                    pieces.append(f"About the speaker:\n\n{rec['bio']}")
                if pieces:
                    tk["abstract"] = "\n\n".join(pieces)

        # Workshops: swap PDF-harvested panellists for the website's clean
        # (Name, Affiliation, Talk Title) triples; surface the website's
        # topic title; surface the workshop chair in the topic line.
        if sid in enrich["workshops"]:
            w = enrich["workshops"][sid]
            if w.get("title"):
                s_obj["title"] = w["title"]
            if w.get("chair"):
                chair_note = f"Chair: {w['chair']}"
                s_obj["topic"] = (
                    f"{s_obj['topic']} · {chair_note}"
                    if s_obj.get("topic") else chair_note
                )
            if w.get("panelists"):
                talks_for_session = [{
                    "title": p["talk_title"],
                    "speaker": p["name"],
                    "aff": p["aff"],
                    "is_invited": False,
                    "color": "indigo",
                    "start_min": None, "end_min": None,
                    "abstract": "", "extra_authors": [],
                } for p in w["panelists"]]

        # Sunday Student Event: the session wraps three sub-events (the
        # scientific-communication workshop, the Bench-to-Business symposium,
        # and the networking pizza dinner). The student-event page is the
        # authoritative source for all three — it carries their titles, precise
        # times, locations, and the Bench-to-Business panellist roster — so we
        # build the talk list from the enrichment, falling back to whatever the
        # PDF skeleton harvested for any sub-event the page doesn't describe.
        # (The detailed-schedule PDF crams all of Sunday into a few-pixel band
        # whose cells bleed across columns, so its sub-event rows are
        # unreliable; we don't depend on them when the page is present.)
        if sid == "STUD" and enrich["student"]:
            student = enrich["student"]
            # Index any PDF-skeleton talks by sub-event kind so a section the
            # page omits still falls back to the skeleton entry.
            by_kind: dict[str, dict] = {}
            for tk in talks_for_session:
                low = tk["title"].lower()
                if "workshop" in low or "scientific communication" in low:
                    by_kind.setdefault("workshop", tk)
                elif "bench" in low:
                    by_kind.setdefault("bench", tk)
                elif "pizza" in low or "networking" in low:
                    by_kind.setdefault("pizza", tk)

            rebuilt: list[dict] = []
            for kind in ("workshop", "bench", "pizza"):
                rec = student.get(kind) or {}
                tk = by_kind.get(kind)
                if not rec and tk is None:
                    continue  # neither the page nor the PDF has this sub-event
                if tk is None:
                    # Synthesize the missing sub-event from the page. Student-
                    # event items are non-technical, so they take the session's
                    # Event colour (rose), matching the skeleton-built ones.
                    tk = {
                        "title": "", "speaker": "", "aff": "",
                        "is_invited": False, "color": "rose",
                        "start_min": None, "end_min": None,
                        "abstract": "", "extra_authors": [],
                    }
                if rec.get("title"):
                    tk["title"] = rec["title"]
                if rec.get("start") and rec.get("end"):
                    tk["start_min"] = _hhmm_to_minutes(rec["start"])
                    tk["end_min"] = _hhmm_to_minutes(rec["end"])
                # Per-talk location override: the student-event page sometimes
                # specifies a different room for an individual sub-event
                # (e.g. "ETH HG, Audi Max" for the workshop, vs the session-
                # level room "HG F30, Plenary Auditorium"). We carry this on
                # the talk dict as `location` and the emit loop below picks
                # it up to override the session-default location.
                if rec.get("location"):
                    tk["location"] = rec["location"]
                # Bench-to-Business: the panellist list becomes co-authors
                # on this single talk rather than separate talks.
                if kind == "bench":
                    tk["extra_authors"] = [
                        {"name": p["name"], "aff": p["aff"]}
                        for p in rec.get("panelists", [])
                        if p.get("name")
                    ]
                rebuilt.append(tk)
            if rebuilt:
                talks_for_session = rebuilt
            # Now that the talks carry their authoritative web-derived
            # times, propagate the bounds back to the session container so
            # it doesn't appear to end at the PDF's coarse 19:30 when the
            # final pizza dinner actually runs to 20:00.
            ts_pairs = [(tk["start_min"], tk["end_min"])
                        for tk in talks_for_session
                        if tk.get("start_min") is not None
                        and tk.get("end_min") is not None]
            if ts_pairs:
                new_start = min(p[0] for p in ts_pairs)
                new_end = max(p[1] for p in ts_pairs)
                s_obj["start_ts"] = (
                    f"{day_iso}T{new_start // 60:02d}:"
                    f"{new_start % 60:02d}:00")
                s_obj["end_ts"] = (
                    f"{day_iso}T{new_end // 60:02d}:"
                    f"{new_end % 60:02d}:00")

        # Industry talks: the PDF cells for these are notoriously hard to
        # parse, so when the industry-talks page is available we *replace*
        # the PDF harvest with its clean (Company, Talk Title, Speaker)
        # triples.
        if sid in enrich["industry"]:
            ind = enrich["industry"][sid]
            if ind.get("label"):
                s_obj["title"] = f"Industry Talks · {ind['label']}"
            new_talks: list[dict] = []
            s_min = _hhmm_to_minutes(sess["start"])
            e_min = _hhmm_to_minutes(sess["end"])
            n = len(ind.get("talks", []))
            slot = (e_min - s_min) // n if n else 0
            for i, t in enumerate(ind["talks"]):
                ts = s_min + i * slot
                te = ts + slot if slot else e_min
                new_talks.append({
                    "title": t["title"] or "Industry Talk",
                    "speaker": t["name"],
                    "aff": t["company"],
                    "is_invited": False,
                    "color": "indigo",
                    "start_min": ts if slot else None,
                    "end_min":   te if slot else None,
                    "abstract": "", "extra_authors": [],
                })
            if new_talks:
                talks_for_session = new_talks

        # Social events: attach the website's blurb to the session object as
        # `details` (the schema's free-text session-description field, which the
        # builder renders as a "Details" section and indexes for search). No
        # talks are synthesized — social events have no presenters in any
        # meaningful sense; the description belongs on the session itself.
        if sid in enrich["social"]:
            s_obj["details"] = enrich["social"][sid]

        # Lab tours: synthesize one talk per visit option, with the visit
        # name as title and the description as abstract.
        if sid == "LABS" and enrich["lab_tours"]:
            talks_for_session = [{
                "title": v["heading"],
                "speaker": "", "aff": "",
                "is_invited": False, "color": "rose",
                "start_min": None, "end_min": None,
                "abstract": v["description"],
                "extra_authors": [],
            } for v in enrich["lab_tours"]]

        # Optica schedule overlay. The Optica mirror carries the full author
        # list (with affiliations) and the abstract for every talk — neither of
        # which the detailed-schedule PDF renders. Two uses, both keyed off the
        # session, so the dedicated-page enrichments above (plenary, industry,
        # workshop, student, social, labs — none of whose ids equal an Optica
        # session code or a "Poster Blitz" title) are left untouched:
        #   * Oral tech sessions (JSON session id == Optica session code, all
        #     of type Technical): enrich each PDF-harvested talk with its full
        #     Optica author list + abstract + canonical title, matched by title
        #     within the session.
        #   * Poster-blitz sessions (the PDF lists no individual posters, so
        #     these come through empty): populate them from the Optica poster
        #     list for the matching session title.
        optica = enrich.get("optica") or {}
        osess = optica.get("by_code", {}).get(sid)
        if osess and talks_for_session:
            matches = _match_optica_talks(
                talks_for_session, osess["presentations"])
            for i, tk in enumerate(talks_for_session):
                op = matches.get(i)
                if not op:
                    continue
                if op["title"]:
                    tk["title"] = op["title"]
                if op["abstract"]:
                    tk["abstract"] = op["abstract"]
                if op["authors"]:
                    tk["authors_full"] = op["authors"]
                # Adopt the canonical Optica talk code (e.g. "M1A.1") as this
                # talk's id/number, replacing the synthetic positional one.
                if op["code"]:
                    tk["code"] = op["code"]
        elif sess.get("type") == "Poster Blitz" and not talks_for_session:
            posters = optica.get("by_title", {}).get(
                _norm_title(s_obj["title"]))
            if posters:
                for op in posters["presentations"]:
                    talks_for_session.append({
                        "title": op["title"],
                        "speaker": "", "aff": "",
                        "is_invited": False, "color": "teal",
                        "start_min": None, "end_min": None,
                        "abstract": op["abstract"],
                        "extra_authors": [],
                        "authors_full": op["authors"],
                        "code": op["code"],
                    })

        # ---- Emit talks for this session
        for i, tk in enumerate(talks_for_session, 1):
            t_title = tk["title"]
            speaker = tk["speaker"]
            aff = tk["aff"]
            is_invited = tk["is_invited"]
            color_override = tk["color"]
            t_start_min = tk["start_min"]
            t_end_min = tk["end_min"]
            t_abstract = tk.get("abstract", "")
            extra_authors_in = tk.get("extra_authors", []) or []
            # Prefer the canonical Optica talk code (e.g. "M1A.1") as the id —
            # it's the number ECIO actually prints and the app surfaces it in
            # the talk's title bar. Fall back to the synthetic positional id for
            # talks with no Optica match (plenaries, ceremonies, socials, …).
            talk_code = tk.get("code")
            tid = talk_code or _talk_id(sess["id"], i)
            if color_override:
                color = color_override
            else:
                color = "indigo" if is_invited else "sky"

            authors: list[dict] = []
            institutions: list[dict] = []
            authors_full = tk.get("authors_full")
            # Build the author + institution lists. When the Optica overlay
            # supplied a full author roster (`authors_full`), it is the
            # authoritative list and is used verbatim, in paper order; `speaker`
            # is kept only as a presenter marker whose position into this list
            # is resolved below. Otherwise the presenting speaker (if any)
            # becomes the first author and any `extra_authors` follow.
            # Affiliations are deduplicated into a single institutions list and
            # each author's `insts` field carries 1-based indices into it.
            author_inputs: list[tuple[str, str]] = []  # (name, aff)
            if authors_full:
                for nm, af in authors_full:
                    nm = (nm or "").strip()
                    if nm:
                        author_inputs.append((nm, (af or "").strip()))
            else:
                if speaker:
                    # Fall back to the invited-speakers cross-reference when the
                    # PDF cell didn't carry an affiliation. PDF-harvested talks
                    # from the tech-grid never do (the grid prints only speaker
                    # name + title), so this fill is what gives invited speakers
                    # their institution in the final JSON.
                    if not aff and invited_idx.records:
                        looked_up = invited_idx.lookup(speaker, t_title)
                        if looked_up:
                            aff = looked_up
                            invited_filled_count += 1
                            invited_filled_speakers.append(speaker)
                    author_inputs.append((speaker, aff))
                for ea in extra_authors_in:
                    nm = (ea.get("name") or "").strip()
                    af = (ea.get("aff") or "").strip()
                    if nm:
                        author_inputs.append((nm, af))

            if author_inputs:
                inst_map: dict[str, int] = {}  # affiliation -> 1-based id
                for nm, af in author_inputs:
                    a: dict = {"name": nm}
                    if af:
                        if af not in inst_map:
                            inst_map[af] = len(inst_map) + 1
                            institutions.append({"n": inst_map[af], "name": af})
                            affiliations_pool.add(af)
                        a["insts"] = [inst_map[af]]
                    else:
                        a["insts"] = []
                    authors.append(a)
            elif aff:
                # Bare-affiliation sponsor slot (e.g. "LIGENTEC SA"): record
                # the institution but emit no author.
                institutions = [{"n": 1, "name": aff}]
                affiliations_pool.add(aff)

            # Per-talk timing: PDF-harvested talks get the slot window;
            # session-mode entries inherit the session times.
            if t_start_min is not None and t_end_min is not None:
                t_start_iso = (f"{day_iso}T"
                               f"{t_start_min // 60:02d}:"
                               f"{t_start_min %  60:02d}:00")
                t_end_iso = (f"{day_iso}T"
                             f"{t_end_min // 60:02d}:"
                             f"{t_end_min %  60:02d}:00")
            else:
                t_start_iso = start_iso
                t_end_iso = end_iso

            # Pick a sensible placeholder when the PDF cell has no title text
            # (e.g. ". Frederic Loizeau, Lightium AG" or a bare-affiliation
            # sponsor slot like "LIGENTEC SA"). The placeholder uses the
            # session type, not invented title text.
            sess_type = sess.get("type", "")
            if sess_type == "Industry Talks":
                placeholder = "Industry Talk"
            elif sess_type == "Workshop":
                placeholder = "Workshop Panelist"
            else:
                placeholder = "(untitled)"

            talk_obj: dict = {
                "id": tid,
                "session_id": sess["id"],
                "title": t_title or placeholder,
                "color": color,
                "start_ts": t_start_iso,
                "end_ts": t_end_iso,
            }
            # Canonical paper number (schema's optional `number`), set whenever
            # we adopted an Optica code so the JSON records it explicitly.
            if talk_code:
                talk_obj["number"] = talk_code
            # Inherit the session's room as the talk's location. The schedule
            # PDF prints rooms only at the session-block level (one column per
            # room), so every talk in a session shares its parent's location.
            # An enrichment branch above (e.g. the student-event integration)
            # can override this for an individual talk by setting tk["location"];
            # in that case we use the per-talk value.
            t_location = tk.get("location") or room
            if t_location:
                talk_obj["location"] = t_location
            # Author-display fields.
            # - `speaker` / `speaker_pos` mark the presenting author (only set
            #   when there is one; multi-author talks with no presenter, like
            #   the Sunday Bench-to-Business panel, omit both).
            # - `first_author` / `last_author` are taken from the authors list
            #   and used by the legacy byline and the search indexer.
            if speaker:
                talk_obj["speaker"] = speaker
                # With a PDF-only author list the speaker is author 0. With an
                # Optica roster the presenter can sit anywhere in paper order,
                # so locate it by name (falling back to 0 if it isn't found —
                # e.g. an OCR-mangled PDF speaker name).
                pos = 0
                if authors_full:
                    found = _author_index_of(speaker, authors)
                    if found is not None:
                        pos = found
                talk_obj["speaker_pos"] = pos
            if authors:
                talk_obj["first_author"] = authors[0]["name"]
                talk_obj["last_author"] = authors[-1]["name"]
            elif speaker:
                # Defensive: a `speaker` without a populated authors list
                # shouldn't happen given the construction above, but keep the
                # legacy fields populated either way.
                talk_obj["first_author"] = speaker
                talk_obj["last_author"] = speaker
            if authors:
                talk_obj["authors"] = authors
            if institutions:
                talk_obj["institutions"] = institutions
            if t_abstract:
                talk_obj["abstract"] = t_abstract
            talks_out.append(talk_obj)
            s_obj["talk_ids"].append(tid)


    # ---- Agenda-of-Sessions overlay ----------------------------------------
    # The agenda PDF is the cleanest source for two things the detailed grid
    # renders only patchily: the LOCATION of non-talk events and the daily
    # logistics rows (Registration / Coffee Break / Lunch). It's applied last,
    # over the fully-built session list, and only ever ADDS information:
    #   * it fills the `location` of any session that still lacks one (matched
    #     by session code, then by generic event-kind within the same day) — it
    #     never overrides a location the detailed schedule already supplied
    #     (that PDF is the newer, authoritative source for rooms); and
    #   * it appends the standalone Registration/Coffee/Lunch Event rows the
    #     grid omits.
    agenda = enrich.get("agenda") or {"rooms_by_code": {}, "events": []}
    if agenda["rooms_by_code"] or agenda["events"]:
        rooms_by_code = agenda["rooms_by_code"]
        # Location-by-kind index: (day_iso, kind) -> location, for the
        # enrichment (non-new-row) events that actually carry a venue.
        loc_by_kind: dict[tuple[str, str], str] = {}
        for ev in agenda["events"]:
            if ev["kind"] and not ev["new_row"] and ev["location"]:
                loc_by_kind.setdefault((ev["day_iso"], ev["kind"]),
                                       ev["location"])

        filled_loc = 0
        for s in sessions_out:
            if s.get("location"):
                continue
            loc = rooms_by_code.get(s["id"])
            if not loc:
                day_iso = s["start_ts"][:10]
                k = _agenda_kind(s.get("title", ""))
                if k:
                    loc = loc_by_kind.get((day_iso, k[0]))
            if loc:
                s["location"] = loc
                filled_loc += 1

        # Append the daily logistics rows (Registration / Coffee Break / Lunch).
        # These have no talks; they exist purely to tell an attendee where and
        # when registration is open and where the coffee/lunch breaks are.
        existing_keys = {(s["start_ts"][:10], s["start_ts"][11:16],
                          (s.get("title") or "").lower()) for s in sessions_out}
        added_rows = 0
        for ev in agenda["events"]:
            if not ev["new_row"] or not ev["day_iso"]:
                continue
            key = (ev["day_iso"], ev["start"], ev["name"].lower())
            if key in existing_keys:
                continue
            existing_keys.add(key)
            row = {
                "id": f"{ev['kind'].upper()}-{ev['day_iso']}-"
                      f"{ev['start'].replace(':', '')}",
                "title": ev["name"],
                "color": "rose",
                "type": ev["type_label"] or "Event",
                "start_ts": f"{ev['day_iso']}T{ev['start']}:00",
                "end_ts": f"{ev['day_iso']}T{ev['end']}:00",
                "talk_ids": [],
            }
            if ev["location"]:
                row["location"] = ev["location"]
            sessions_out.append(row)
            added_rows += 1
        log(f"[info] agenda overlay     : filled {filled_loc} session "
            f"location(s); added {added_rows} logistics row(s).")

    # ---- Assemble final JSON ------------------------------------------------
    data = {
        "conference_name": CONFERENCE_NAME,
        "sessions": sessions_out,
        "talks": talks_out,
        "session_types": SESSION_TYPES,
        "talk_types": TALK_TYPES,
    }
    # Optional curator credit (shown in the About section of the built app).
    # Per the schema, the block is rendered only when `name` is non-empty.
    if CURATOR and CURATOR.get("name"):
        cur = {"name": CURATOR["name"]}
        if CURATOR.get("affiliation"):
            cur["affiliation"] = CURATOR["affiliation"]
        if CURATOR.get("link"):
            cur["link"] = CURATOR["link"]
        data["curator"] = cur
    if affiliations_pool:
        # One flat, de-duplicated, sorted list of raw affiliation strings for
        # the builder's affiliation map.
        data["affiliation_sources"] = sorted(affiliations_pool)

    _collapse_session_tags(data["sessions"])
    OUTPUT_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    log(f"[ok] wrote {OUTPUT_JSON.name}: "
        f"{len(sessions_out)} sessions, {len(talks_out)} talks.")
    if invited_filled_count:
        log(f"[ok]   filled affiliations on {invited_filled_count} talk(s) "
            f"from cached invited-speakers page:")
        for sp in invited_filled_speakers:
            log(f"          - {sp}")
    log("=" * 72)
    log("DONE.")
    log("=" * 72)


if __name__ == "__main__":
    main()
