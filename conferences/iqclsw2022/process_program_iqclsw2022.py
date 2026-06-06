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

"""process_program_iqclsw2022.py — turn the conference PDF program + two HTML
companion pages into the clean, source-agnostic conference_data.json the
shared build_conference_app.py expects.

Inputs (under data/):
    Program_IQCLSW2022.pdf   the full program PDF (cover + day-by-day
                             schedule + numbered 1..32 poster catalog at the
                             end). REQUIRED — this is the authoritative source
                             for every session, time slot, chair, talk title,
                             and poster.
    TutorialSpeakers.html    OPTIONAL public "Keynote, Invited and Tutorial
                             speakers" table (Name, Affiliation, Type, Title).
                             When present, used to upgrade every keynote /
                             invited / tutorial talk in the PDF with the
                             SPEAKER'S FULL NAME and an AFFILIATION (the PDF
                             prints only "Initial. Surname" and no affiliation).
    PosterSession.html       OPTIONAL public poster-session table (N°, Title,
                             Authors). Not currently used — the PDF's
                             contiguous 1..32 numbering is the canonical
                             poster list, and the HTML's gappy 1..34 numbering
                             does not line up cleanly. Kept as a fetched
                             artifact for future cross-referencing.

Output (conference_data.json, beside this script):
    The schema documented in docs/CONFERENCE_JSON.md (conference_name,
    sessions[], talks[], session_types[], talk_types[], affiliation_sources[]).

Design notes for THIS conference:

  * Single-track. There are no parallel rooms; the PDF lays the program out as
    one linear time line per day. So no per-talk room location is emitted (the
    venue lives on the session via `details`).

  * Phase. The week splits into a SCHOOL phase (Tue 23 + Wed 24 at ETH Zürich,
    Siemens Auditorium HIT) and a WORKSHOP phase (Thu 25 - Sun 28 at Monte
    Verità). The phase boundary is the explicit "Workshop (Monte Verità,
    Auditorium)" venue header that appears on Thursday before the first
    workshop tutorial; we track venue/phase changes as we walk the program.

  * Sessions. Each day is broken into sessions at:
      - meal / break / admin dividers (coffee break, lunch, dinner, welcome
        reception, free evening, bus departure, transit, lab visit, etc.),
      - explicit "Session I: <topic>" / "Session II: …" headers in the PDF
        (these belong to the Workshop phase), and
      - chair changes ("Chair: <Name>" rebinds the upcoming run of talks).
    Each contiguous run of scientific talks becomes one session. Non-scientific
    blocks (meals, breaks, ceremonies, transit) are emitted as their own
    `General`-coloured sessions so they show up on the timeline.

  * Talks. Inside each timed block:
      - "Tutorial: <Initial>. <Surname>"  -> Tutorial talk (`violet`)
      - "Keynote: <Initial>. <Surname>"   -> Keynote talk  (`orange`)
      - "Invited: <Initial>. <Surname>"   -> Invited talk  (`indigo`)
      - "<Initial>. <Surname>"            -> Contributed talk (`sky`)
      - Anything that looks like an admin item (Opening, Welcome Speech, Bus
        departure, Lunch, ...) becomes a General (`rose`) divider session.
    The title is the remaining line(s) under the time, joined across line
    wraps until the next time/chair/session/day boundary.

  * Speaker enrichment. The PDF prints only initial+surname; we cross-
    reference each invited/tutorial/keynote talk against TutorialSpeakers.html
    (matching by surname, disambiguating by initial when needed) to recover
    the speaker's FULL NAME and AFFILIATION. Contributed-talk speakers stay
    as the PDF prints them (the table doesn't list contributed authors).

  * Posters. The "Posters (Poster sessions Thursday/Friday 17:30):" section at
    the end of the PDF lists 32 posters numbered 1..32; each entry is a dot-
    separated "<Authors>. <Title>" run wrapped across two-or-three lines. We
    split that catalog in half across the two real scheduled poster sessions
    (Thursday 17:30 and Friday 17:30), exactly as the sibling processors do.

  * Affiliation sources. Every affiliation we recover from TutorialSpeakers.html
    is pooled into the flat affiliation_sources list so the builder's
    affiliation-map step can learn short forms.

Run directly:  python process_program_iqclsw2022.py
(or let make_app.py run it for you).
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
PDF_IN = DATA_DIR / "Program_IQCLSW2022.pdf"
SPEAKERS_HTML_IN = DATA_DIR / "TutorialSpeakers.html"
POSTERS_HTML_IN = DATA_DIR / "PosterSession.html"
JSON_OUT = SCRIPT_DIR / "conference_data.json"

CONFERENCE_NAME = "IQCLSW 2022"
YEAR = 2022
MONTH = 8   # August
# The program PDF dates each day only by day-of-month + weekday; we map those
# onto August 2022 below. The week is Tuesday 23 - Sunday 28 August 2022.

# Cap the talk title concatenation: a title that wraps past ~3 lines without
# being terminated by a new time / chair line almost always means the parser
# missed a boundary, so we'd rather truncate than swallow the rest of the day.
MAX_TITLE_LINES = 6


def log(msg: str) -> None:
    print(msg, flush=True)


# =============================================================================
# Type / color registries (baked into the JSON; the app reads these directly).
# `id` is the color token the app filters and groups on, AND the token each
# session/talk's `color` field must use. The conference program slices into
# five talk genres (Tutorial / Keynote / Invited / Contributed / Poster) plus
# generic housekeeping items; sessions are coloured by their dominant talk
# genre (or by the divider's role) using the same tokens.
# =============================================================================
# Standard session/talk type taxonomy. The seven shared types; a conference only
# surfaces the ones its program actually uses (the app hides count-0 types).
SESSION_TYPES = [
    {"id": "blue",    "label": "Technical",
     "fg": "#2563eb", "bg_light": "#e8efff", "bg_dark": "#1a233d"},
    {"id": "orange",  "label": "Plenary",
     "fg": "#ea580c", "bg_light": "#ffedd5", "bg_dark": "#3b1d0a"},
    {"id": "fuchsia", "label": "Tutorial",
     "fg": "#c026d3", "bg_light": "#fae8ff", "bg_dark": "#3a0f3f"},
    {"id": "teal",    "label": "Poster",
     "fg": "#0d9488", "bg_light": "#d6f3ef", "bg_dark": "#102b27"},
    {"id": "rose",    "label": "Event",
     "fg": "#e11d48", "bg_light": "#ffe1e8", "bg_dark": "#38161f"},
]
TALK_TYPES = [
    {"id": "orange",  "label": "Plenary",
     "fg": "#ea580c", "bg_light": "#ffedd5", "bg_dark": "#3b1d0a"},
    {"id": "indigo",  "label": "Invited",
     "fg": "#4f46e5", "bg_light": "#e6e4ff", "bg_dark": "#1d1a3d"},
    {"id": "sky",     "label": "Contributed",
     "fg": "#0284c7", "bg_light": "#e0f2fe", "bg_dark": "#0c2a3d"},
    {"id": "fuchsia", "label": "Tutorial",
     "fg": "#c026d3", "bg_light": "#fae8ff", "bg_dark": "#3a0f3f"},
    {"id": "teal",    "label": "Poster",
     "fg": "#0d9488", "bg_light": "#d6f3ef", "bg_dark": "#102b27"},
    {"id": "rose",    "label": "Event",
     "fg": "#e11d48", "bg_light": "#ffe1e8", "bg_dark": "#38161f"},
]


# -----------------------------------------------------------------------------
# Bootstrap: pdfplumber is the PDF text extractor we standardise on across the
# repo (sibling conference processors already require it). Install on demand so a
# fresh check-out builds with nothing more than `pip install pdfplumber lxml`.
# -----------------------------------------------------------------------------
def _bootstrap_pdfplumber() -> None:
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        log("[setup] installing pdfplumber …")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet",
             "pdfplumber>=0.10"])


# -----------------------------------------------------------------------------
# PDF -> text lines. pdfplumber's extract_text gives us page-by-page output
# already linewise; we concatenate, drop the cover page, and tidy whitespace.
# -----------------------------------------------------------------------------
def _load_pdf_lines() -> list[str]:
    _bootstrap_pdfplumber()
    import pdfplumber

    if not PDF_IN.exists():
        raise SystemExit(
            f"[process] ERROR: missing {PDF_IN.name} in data/. Run "
            "fetch_program_iqclsw2022.py first (or via make_app.py).")

    lines: list[str] = []
    with pdfplumber.open(PDF_IN) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for raw in text.split("\n"):
                s = raw.replace("\xa0", " ")
                s = re.sub(r"[ \t]+", " ", s).strip()
                if s:
                    lines.append(s)
    # Drop the cover-page banner (the spelled-out conference title,
    # "ETH Zürich-Monte Verità, …", "Sponsored by:") — it
    # never carries program content.
    cover_re = re.compile(
        r"^(international quantum cascade|eth z.?rich-monte|sponsored by)",
        re.I)
    lines = [l for l in lines if not cover_re.match(l)]
    return lines


# -----------------------------------------------------------------------------
# Line classifiers. The PDF lays each day out as a flat stream of:
#   - day header     "Tuesday 23"  (sometimes "Saturday 27 (Monte Verità, …)")
#   - venue header   "School (ETH Zürich, …)" / "Workshop (Monte Verità, …)"
#   - chair line     "Chair: <Name>"
#   - session topic  "Session I: <Topic>"
#   - time + title   "14:30 Tutorial: A. Smith" / "15.45 B. Jones" /
#                    "17:15-19:30 Welcome Reception at Bellavista"
#   - title cont.    free-flowing wrapped text
#   - admin line     "Free evening", "Transit to Monte Verità"
#   - "Posters …"    starts the numbered poster catalog (terminal section)
# We recognise each via a tight regex; everything else is treated as a title
# continuation line for the most-recently-opened talk.
# -----------------------------------------------------------------------------
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday")

# Day header at the start of a day. Examples:
#   "Tuesday 23"
#   "Saturday 27 (Monte Verità, Auditorium)"
DAY_RE = re.compile(
    rf"^(?P<wd>{'|'.join(WEEKDAYS)})\s+(?P<dom>\d{{1,2}})"
    r"(?:\s*\((?P<venue>[^)]+)\))?\s*$")

# Phase / venue header. Examples:
#   "School (ETH Zürich, Siemens Auditorium HIT ground floor)"
#   "Workshop (Monte Verità, Auditorium)"
VENUE_RE = re.compile(r"^(?P<phase>School|Workshop)\s*\((?P<venue>[^)]+)\)\s*$")

# "Chair: <Name>" — possibly with a trailing "(TBC)" annotation, which we
# strip; "to be confirmed" is a planning artifact, not part of the name.
CHAIR_RE = re.compile(r"^Chair:\s*(?P<name>.+?)\s*$", re.I)
_TBC_TAIL_RE = re.compile(r"\s*\(\s*TBC\s*\)\s*$", re.I)

# "Session I: <topic>" / "Session II: <topic>" / "Session V: <topic>
# (continued)" — the explicit topic header that introduces
# the next run of talks during the Workshop phase.
SESSION_HDR_RE = re.compile(
    r"^Session\s+(?P<num>[IVXLC]+):\s*(?P<topic>.+?)\s*$", re.I)

# Time + first-of-block line. Time can be "HH:MM", "H:MM", "HH.MM", or
# "HH:MM-HH:MM" / "HH:MM – HH:MM" (en/em dash with optional spaces). Whatever
# follows on the same line is the "head" of the block (talk speaker line,
# divider label, …).
TIME_RE = re.compile(
    r"^(?P<s>\d{1,2}[:.]\d{2})"
    r"(?:\s*[-–—]\s*(?P<e>\d{1,2}[:.]\d{2}))?"
    r"\s+(?P<head>.+?)\s*$")

# Speaker-line prefixes ("Tutorial: " / "Keynote: " / "Invited: ").
TALK_KIND_RE = re.compile(
    r"^(?P<kind>Tutorial|Keynote|Invited)\s*:\s*(?P<rest>.+?)\s*$", re.I)

# A "<Initial>. <Surname>" / "<Init>.<Init>. <Surname>" / "<I-J>. <Surname>"
# initials-style speaker token. The trailing "(Remote)" annotation is
# allowed. Examples it must match:
#   "A. Smith"
#   "B.C. Jones"
#   "D-E. Brown"
#   "F.G. Lee (Remote)"
#   "H. Park"
# And must NOT match generic admin titles like "Coffee break" or "Lab visit".
INITIAL_NAME_RE = re.compile(
    r"^(?:[A-ZÀ-Ý]\.?(?:[\-.][A-ZÀ-Ý]\.?)*)\s+"     # initials block
    r"[A-ZÀ-Ý][\wÀ-ÿ\-']+"                          # surname (incl. hyphens)
    r"(?:\s*\([^)]+\))?\s*$")                       # optional "(Remote)"

# Words/phrases that mark a non-scientific block (divider). Order matters
# only loosely; first match wins for the display label.
_DIVIDER_LABELS = [
    (re.compile(r"^opening\b", re.I),                    "Opening"),
    (re.compile(r"^closing\b|^final remarks", re.I),     "Closing Remarks"),
    (re.compile(r"^coffee\s*break\b", re.I),             "Coffee Break"),
    (re.compile(r"^\bbreak\b", re.I),                    "Break"),
    (re.compile(r"^lunch\s*break\b", re.I),              "Lunch"),
    (re.compile(r"^lunch\b", re.I),                      "Lunch"),
    (re.compile(r"^welcome\s*reception\b", re.I),        "Welcome Reception"),
    (re.compile(r"^reception\b", re.I),                  "Reception"),
    (re.compile(r"^dinner\b|conference dinner", re.I),   "Dinner"),
    (re.compile(r"^ap[éeè]ro", re.I),                    "Apéro & Dinner"),
    (re.compile(r"^poster session", re.I),               "Poster Session"),
    (re.compile(r"^lab visit\b", re.I),                  "Lab Visit"),
    (re.compile(r"^bus departure", re.I),                "Bus Departure"),
    (re.compile(r"^transit\b", re.I),                    "Transit"),
    (re.compile(r"^welcome speech", re.I),               "Welcome Speech"),
    (re.compile(r"^free evening", re.I),                 "Free Evening"),
    (re.compile(r"^excursion\b", re.I),                  "Excursion"),
    (re.compile(r"^awards ceremony", re.I),              "Awards Ceremony"),
]
_DIVIDER_HEADS = [rx for rx, _ in _DIVIDER_LABELS]

# Standalone untimed admin lines that may sit between time-anchored blocks.
# We emit one "Transit to Monte Verità" divider (actionable info: a bus on
# Wednesday evening), but DROP "Free evening" — the PDF prints it after every
# evening reception/dinner and it carries no schedulable information.
_BARE_ADMIN_RE = re.compile(r"^(?:transit\b)", re.I)
_BARE_DROP_RE = re.compile(r"^(?:free evening)\s*$", re.I)

# Poster section start in the PDF. Everything after this marker is the
# numbered poster catalog (lines like "1. <Authors>. <Title>" wrapped across
# 2-3 lines).
POSTER_MARKER_RE = re.compile(r"^Posters\b", re.I)

# A new poster entry begins with "<N>. " at the start of a line.
POSTER_ENTRY_RE = re.compile(r"^(?P<num>\d{1,2})\.\s+(?P<rest>.+?)\s*$")


def _divider_label(head: str) -> str | None:
    """Return the canonical short label if `head` matches a known divider, else
    None. `head` is the text AFTER the time on a time line (or a bare admin
    line)."""
    for rx, label in _DIVIDER_LABELS:
        if rx.match(head):
            return label
    return None


def _looks_like_initials_name(s: str) -> bool:
    return bool(INITIAL_NAME_RE.match(s))


def _hhmm(token: str) -> str:
    """Normalise a "HH:MM" / "H:MM" / "HH.MM" time token to "HH:MM"."""
    t = token.replace(".", ":")
    h, m = t.split(":")
    return f"{int(h):02d}:{m.zfill(2)}"


def _iso(dom: int, hhmm: str) -> str:
    """ISO timestamp for `<August 2022, dom>T<HH:MM>:00`."""
    return f"{YEAR:04d}-{MONTH:02d}-{dom:02d}T{_hhmm(hhmm)}:00"


# -----------------------------------------------------------------------------
# Parsing the speaker line ("Tutorial: A. Smith" / "B. Jones").
# -----------------------------------------------------------------------------
def _parse_speaker_head(head: str) -> tuple[str, str, str]:
    """Classify the "head" of a timed block as a talk header.

    Returns (kind, speaker, leftover) where:
      kind     -- "tutorial" / "keynote" / "invited" / "contributed" / ""
      speaker  -- "A. Smith", "B. Jones", "F.G. Lee", or ""
      leftover -- any trailing words (e.g. " (Remote)") preserved verbatim;
                  callers usually ignore this.

    "" / "" is returned when the head does NOT look like a talk speaker (so
    the caller should treat the block as a divider / general item).
    """
    m = TALK_KIND_RE.match(head)
    if m:
        kind = m.group("kind").lower()
        # The PDF occasionally tacks a trailing comma onto the speaker name
        # ("Invited: B. Jones,"). Strip it so the surname matcher and display
        # both see a clean token.
        rest = m.group("rest").strip().rstrip(",")
        if _looks_like_initials_name(rest) or re.match(
                r"^[A-ZÀ-Ý][\wÀ-ÿ\-]+(?:\s+[A-ZÀ-Ý][\wÀ-ÿ\-]+)*"
                r"(?:\s*\([^)]+\))?\s*$", rest):
            return kind, rest, ""
        # Tutorial:/Invited:/Keynote: with an unusual speaker layout — keep
        # the whole rest as the speaker text so we don't lose it.
        return kind, rest, ""
    if _looks_like_initials_name(head):
        return "contributed", head.rstrip(","), ""
    return "", "", head


# -----------------------------------------------------------------------------
# TutorialSpeakers.html parser. Returns a list of dicts with
# {name, last, initial, affiliation, kind, title} per row. The HTML page is
# essentially a single 4-column table.
# -----------------------------------------------------------------------------
# Country-flag emoji (regional-indicator pairs) and surrounding parens — we
# strip both when extracting the plain affiliation text, since the country is
# already encoded as words inside the affiliation in most cases.
_FLAG_RE = re.compile(
    r"[\U0001F1E6-\U0001F1FF]{2}", flags=re.UNICODE)


# Per-speaker affiliation overrides for the rare cases where the public
# speakers page lists only the parent organisation acronym, hiding the
# specific research center on whose behalf the speaker attended. To keep
# participant names out of tracked source, these are read at runtime from an
# optional data file (absent file -> no overrides):
#
#     DATA_DIR / "affiliation_overrides.tsv"
#
# one record per line, tab-separated, '#' comments and blank lines ignored:
#
#     <surname>\t<first-initial>\t<affiliation>
#
# Keyed by (surname, first-initial) to disambiguate within the speaker table.
def _load_affiliation_overrides() -> dict[tuple[str, str], str]:
    path = DATA_DIR / "affiliation_overrides.tsv"
    if not path.exists():
        return {}
    out: dict[tuple[str, str], str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) >= 3:
            out[(cols[0].strip(), cols[1].strip().upper())] = cols[2].strip()
    return out


_AFFILIATION_OVERRIDES: dict[tuple[str, str], str] = _load_affiliation_overrides()


def _load_speaker_table() -> list[dict]:
    if not SPEAKERS_HTML_IN.exists():
        log("[process] no TutorialSpeakers.html; "
            "keynote/invited/tutorial talks will keep PDF speaker text only.")
        return []
    try:
        import lxml.html
    except ImportError:
        log("[setup] installing lxml …")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", "lxml"])
        import lxml.html

    doc = lxml.html.parse(str(SPEAKERS_HTML_IN)).getroot()
    rows = doc.xpath("//table//tr")
    out: list[dict] = []
    for r in rows:
        cells = r.xpath("./th|./td")
        if len(cells) < 4:
            continue
        name = re.sub(r"\s+", " ", cells[0].text_content()).strip()
        if not name or name.lower() == "name":   # header row
            continue
        aff_raw = cells[1].text_content().strip()
        aff = _FLAG_RE.sub("", aff_raw)
        aff = re.sub(r"\s+", " ", aff).strip()
        kind = re.sub(r"\s+", " ", cells[2].text_content()).strip().lower()
        title = re.sub(r"\s+", " ", cells[3].text_content()).strip()
        # Last name = last whitespace-separated token. The HTML occasionally
        # uses ASCII initials in the first cell ("F.G. Lee") matching the
        # PDF, but more often spells the full first name.
        toks = name.split()
        last = toks[-1] if toks else ""
        # First-name initial used for disambiguation when the same surname
        # appears multiple times in the program.
        initial = toks[0][0] if toks and toks[0] else ""
        out.append({
            "name": name, "last": last, "initial": initial,
            "affiliation": aff, "kind": kind, "title": title,
        })
    log(f"[process] speaker table: {len(out)} rows.")
    return out


def _match_speaker(pdf_speaker: str, kind: str,
                   table: list[dict]) -> dict | None:
    """Match a "A. Smith" / "D-E. Brown" / "C. Brown" PDF speaker
    text against a TutorialSpeakers.html row, returning the row or None.

    The match strategy: take the surname (the LAST whitespace-separated token
    of the PDF speaker, stripping any "(Remote)"-style trailing), find every
    row whose `last` matches case-insensitively, and disambiguate by the
    PDF's leading initial when more than one row matches. `kind` (the talk
    type) is used only as a final tie-breaker.
    """
    if not pdf_speaker or not table:
        return None
    s = re.sub(r"\s*\([^)]*\)\s*$", "", pdf_speaker).strip()
    toks = s.split()
    if not toks:
        return None
    last_pdf = toks[-1]
    # PDF leading initial: first letter of the first token, ignoring leading
    # punctuation. "J-B." -> "J", "R.T." -> "R", "M." -> "M".
    init_pdf = ""
    head = toks[0]
    for ch in head:
        if ch.isalpha():
            init_pdf = ch.upper()
            break

    cands = [r for r in table if r["last"].lower() == last_pdf.lower()]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    by_init = [r for r in cands
               if r["initial"].upper() == init_pdf]
    if len(by_init) == 1:
        return by_init[0]
    by_kind = [r for r in (by_init or cands) if r["kind"] == kind]
    if len(by_kind) == 1:
        return by_kind[0]
    # Fall back to the first candidate; we'd rather attach the wrong
    # affiliation than lose it entirely.
    return (by_init or cands)[0]


# -----------------------------------------------------------------------------
# Poster catalog. The PDF's "Posters (Poster sessions Thursday/Friday 17:30):"
# section runs to end-of-document; each entry begins with "<N>. " and contains
# "Authors. Title" packed across 2-3 wrapped lines. We rejoin those wraps,
# then split on the LAST period that separates the author list from the
# title (a heuristic that empirically lands on the right split on every
# entry in this catalog).
# -----------------------------------------------------------------------------
def _harvest_posters(lines: list[str]) -> list[dict]:
    """Return [{n, authors_raw, title}, ...] in PDF order."""
    try:
        start = next(i for i, l in enumerate(lines)
                     if POSTER_MARKER_RE.match(l))
    except StopIteration:
        return []
    entries: list[tuple[int, list[str]]] = []
    cur_n: int | None = None
    cur_lines: list[str] = []
    for raw in lines[start + 1:]:
        m = POSTER_ENTRY_RE.match(raw)
        if m:
            if cur_n is not None:
                entries.append((cur_n, cur_lines))
            cur_n = int(m.group("num"))
            cur_lines = [m.group("rest")]
        else:
            cur_lines.append(raw)
    if cur_n is not None:
        entries.append((cur_n, cur_lines))

    posters: list[dict] = []
    for n, parts in entries:
        # PDF line wraps inside hyphenated words come out as "n-\ntype"; after
        # we space-join the parts that becomes "n- type". Stitch any
        # "<letter>- <letter>" pair back into "<letter>-<letter>" so the
        # title reads as "n-type", "off-resonant", etc.
        blob = " ".join(parts)
        blob = re.sub(r"([A-Za-zÀ-ÿ])-\s+(?=[A-Za-zÀ-ÿ])", r"\1-", blob)
        blob = re.sub(r"\s+", " ", blob).strip()
        # Title splits from the author list at the LAST sentence-ending
        # period that sits before a capitalised word. The poster lines are
        # "<Authors>. <Title>" — authors include "and ", commas, periods
        # inside initials ("J. C. Cao"); the split should land on the
        # period that ENDS the author list. We look for ". " followed by a
        # capital, preferring matches that come after an "and " in the
        # left-side run.
        cut = None
        for m in re.finditer(r"\.\s+(?=[A-ZÀ-Ý])", blob):
            left = blob[: m.start()]
            # The author run almost always ends with "and <Surname>" or a
            # comma-separated surname list; a "Surname." token usually has
            # been preceded by spaces, not "X." initials. Prefer the LAST
            # such point that's still within the first 60% of the blob.
            if m.start() <= len(blob) * 0.85:
                # Skip a period that's clearly inside an initial chain like
                # "J. C. Cao" — those are followed by a single-letter or
                # "X." token whose left side ends in a single capital.
                tail = left.rstrip()
                if tail.endswith(tuple(f"{c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")) \
                        and len(tail) >= 2 and tail[-2] in (" ", "."):
                    continue
                cut = m
        if cut is None:
            # Fall back: split at the very first ". " — better that than no
            # split (the entry would be all-authors with no title).
            cut = next(re.finditer(r"\.\s+", blob), None)
        if cut is None:
            authors_raw, title = blob, ""
        else:
            authors_raw = blob[: cut.start()].strip().rstrip(".")
            title = blob[cut.end():].strip().rstrip(".")
        posters.append({"n": n, "authors_raw": authors_raw, "title": title})
    return posters


def _parse_poster_authors(raw: str) -> list[dict]:
    """Split "A, B, C and D" / "A, B, C, D" into [{name, insts:[1]}, ...].

    Initial-only tokens ("J. C." in "Hua Li, J. C. Cao") are joined to the
    NEXT author rather than treated as a name on their own. The poster
    section has no affiliations; every author is attributed to the single
    institution number 1 (poster sessions ship one synthetic "unknown
    institution" so the builder is happy)."""
    s = raw
    s = re.sub(r"\s+and\s+", ", ", s, flags=re.I)
    parts = [p.strip() for p in s.split(",")]
    # Join initial-only chunks to the following name token: "J." + "C." +
    # "Cao" -> "J. C. Cao". An initial-only token is "X." or "X. Y.".
    joined: list[str] = []
    buf = ""
    for p in parts:
        if not p:
            continue
        looks_initial_only = bool(re.fullmatch(
            r"(?:[A-ZÀ-Ý]\.?(?:\s*[A-ZÀ-Ý]\.?)*)", p))
        if looks_initial_only and not buf:
            buf = p
            continue
        if buf:
            joined.append(f"{buf} {p}")
            buf = ""
        else:
            joined.append(p)
    if buf:
        joined.append(buf)
    return [{"name": n, "insts": [1]} for n in joined if n]


# -----------------------------------------------------------------------------
# Main: walk the PDF lines, emit sessions/talks/dividers.
# -----------------------------------------------------------------------------
def _kind_to_color(kind: str) -> tuple[str, str]:
    """(color, label) for a talk classified as `kind`. Colors follow the
    standard taxonomy: tutorials → Tutorial (fuchsia),
    keynotes → Plenary (orange), general/non-technical rows → Event (rose).
    The label is the talk's free-text descriptor; the color is the standard
    category the app filters and groups on."""
    return {
        "tutorial":    ("fuchsia", "Tutorial"),
        "keynote":     ("orange",  "Keynote"),
        "invited":     ("indigo",  "Invited"),
        "contributed": ("sky",     "Contributed"),
        "general":     ("rose",   "General"),
    }.get(kind, ("rose", "General"))


# The PDF's School venue line is "ETH Zürich, Siemens Auditorium HIT ground
# floor" — too long to fit a session row's location chip readably. Reduce it
# to the short room name that everyone on site actually uses ("Siemens
# Auditorium"). The Monte Verità Workshop venue ("Monte Verità, Auditorium")
# is short enough to pass through unchanged.
def _short_venue(venue: str) -> str:
    if not venue:
        return venue
    v = re.sub(r"\s+", " ", venue).strip()
    if re.search(r"\bSiemens\s+Auditorium\b", v, re.I):
        return "Siemens Auditorium"
    return v


def _venue_phase(venue: str) -> str:
    """Phase guess from a venue string. School venues sit at ETH Zürich (HIT);
    workshop venues sit at Monte Verità. Used as a fallback when an explicit
    School/Workshop header hasn't been seen yet."""
    v = (venue or "").lower()
    if "monte verit" in v:
        return "workshop"
    if "eth" in v or "siemens" in v or "zürich" in v or "zurich" in v:
        return "school"
    return ""


def build_conference_data() -> dict:
    lines = _load_pdf_lines()
    log(f"[process] PDF: {len(lines)} non-empty lines.")
    speaker_table = _load_speaker_table()
    poster_entries = _harvest_posters(lines)
    log(f"[process] posters: {len(poster_entries)} entries in catalog.")

    sessions: list[dict] = []
    talks: list[dict] = []
    aff_pool: set[str] = set()

    # ---- walking state ----
    dom: int | None = None
    weekday: str | None = None
    venue: str = ""
    phase: str = "school"
    chair: str = ""
    pending_topic: str = ""   # set by a "Session N: …" header; consumed by
                              # the next technical session that opens.

    # Buffer for the current technical session being assembled.
    cur_session_talks: list[dict] = []
    cur_session_start: str | None = None
    cur_session_end: str | None = None
    cur_session_chair: str = ""
    cur_session_topic: str = ""
    cur_session_phase: str = phase
    cur_session_venue: str = venue
    cur_block: dict | None = None      # the talk currently accumulating title

    sess_seq = 0
    talk_seq = 0

    def _new_sess_id() -> str:
        nonlocal sess_seq
        sess_seq += 1
        return f"S{sess_seq:03d}"

    def _new_talk_id() -> str:
        nonlocal talk_seq
        talk_seq += 1
        return f"T{talk_seq:03d}"

    def _close_block() -> None:
        """Finalise the talk currently accumulating its title (if any) and
        commit it to the open technical session."""
        nonlocal cur_block
        if cur_block is None:
            return
        # Trim and tidy the title. PDF line wraps inside hyphenated words
        # arrive as "broad-\nband" → "broad- band"; restitch.
        title = " ".join(cur_block["title_parts"])
        title = re.sub(r"([A-Za-zÀ-ÿ])-\s+(?=[A-Za-zÀ-ÿ])", r"\1-", title)
        title = re.sub(r"\s+", " ", title).strip().rstrip(" .")
        cur_block["talk"]["title"] = title or cur_block["fallback_title"]
        cur_block = None

    def _flush_session() -> None:
        """Emit any accumulated technical session as a session record. Resets
        the per-session buffer."""
        nonlocal cur_session_talks, cur_session_start, cur_session_end
        nonlocal cur_session_chair, cur_session_topic, cur_session_phase
        nonlocal cur_session_venue
        _close_block()
        if not cur_session_talks:
            cur_session_topic = ""   # discard a topic that found no talks
            cur_session_start = cur_session_end = None
            cur_session_chair = ""
            return
        phase_word = ("School" if cur_session_phase == "school"
                      else "Workshop")
        color = "fuchsia" if cur_session_phase == "school" else "blue"
        topic = cur_session_topic
        # Title: prefer the explicit "Session N: <topic>" header when present
        # (e.g. "Session II: <topic>"); otherwise fall back
        # to the phase label alone. Leave `topic` empty in the fallback case
        # so the app's secondary chip doesn't redundantly echo the title.
        title = topic or phase_word
        topic_field = "" if not topic else phase_word
        sid = _new_sess_id()
        for t in cur_session_talks:
            t["session_id"] = sid
        sessions.append({
            "id": sid,
            "title": title,
            "type": phase_word,
            "topic": topic_field,
            "date": _dt.date(YEAR, MONTH, int(cur_session_start[8:10])).strftime(
                f"%d-%b-{YEAR}"),
            "location": cur_session_venue,
            "presider": cur_session_chair,
            "presider_aff": "",
            "details": "",
            "start_ts": cur_session_start,
            "end_ts": cur_session_end,
            "color": color,
            "talk_ids": [t["id"] for t in cur_session_talks],
        })
        cur_session_talks = []
        cur_session_start = cur_session_end = None
        cur_session_chair = ""
        cur_session_topic = ""

    def _emit_divider(label: str, start_ts: str, end_ts: str | None,
                      date_dom: int, venue_text: str) -> None:
        """Emit a non-scientific block (meal, break, ceremony, transit, …) as
        its own General-coloured session with no talks."""
        sid = _new_sess_id()
        sessions.append({
            "id": sid,
            "title": label,
            "type": "General",
            "topic": "",
            "date": _dt.date(YEAR, MONTH, date_dom).strftime(f"%d-%b-{YEAR}"),
            "location": venue_text,
            "presider": "",
            "presider_aff": "",
            "details": "",
            "start_ts": start_ts,
            "end_ts": end_ts or start_ts,
            "color": "rose",
            "talk_ids": [],
        })

    def _open_talk(start_ts: str, end_ts: str | None, head: str) -> None:
        """Begin a new talk: parse `head` into kind+speaker, allocate the talk
        dict, and stash it as the current block whose title will accumulate
        from following continuation lines."""
        nonlocal cur_block, cur_session_start, cur_session_end
        nonlocal cur_session_chair, cur_session_phase, cur_session_venue
        nonlocal cur_session_topic, pending_topic

        kind, speaker, _leftover = _parse_speaker_head(head)
        if not kind:
            # Shouldn't be reached — caller pre-classified this block as a
            # talk; if we end up here treat the whole head as the title and
            # the speaker as unknown.
            kind, speaker = "general", ""

        color, label = _kind_to_color(kind)

        # Speaker enrichment via TutorialSpeakers.html. For
        # tutorial/keynote/invited talks we can usually recover a full name
        # and an affiliation; for contributed talks the table has nothing.
        full_name = speaker
        affiliation = ""
        remote = ""
        m_rem = re.search(r"\(([^)]+)\)\s*$", speaker)
        if m_rem and m_rem.group(1).strip().lower() in ("remote",):
            remote = m_rem.group(1).strip()
            speaker_clean = speaker[: m_rem.start()].strip()
        else:
            speaker_clean = speaker
        row = (_match_speaker(speaker_clean, kind, speaker_table)
               if kind in ("tutorial", "keynote", "invited") else None)
        if row:
            full_name = row["name"]
            affiliation = row["affiliation"]
            # Bespoke per-speaker override: replace the table's parent-org
            # acronym with the specific research center the speaker attended
            # under at conference time (see _AFFILIATION_OVERRIDES above).
            last_tok = row["last"]
            init_tok = (row["initial"] or "").upper()
            override = _AFFILIATION_OVERRIDES.get((last_tok, init_tok))
            if override:
                affiliation = override
            if affiliation:
                aff_pool.add(affiliation)

        tid = _new_talk_id()
        institutions = ([{"n": 1, "name": affiliation, "alt_names": []}]
                        if affiliation else [])
        authors = ([{"name": full_name, "insts": [1] if institutions else []}]
                   if full_name else [])
        # The "fallback title" is used when no continuation line shows up
        # (the talk has no title on the page) — e.g. for the rare standalone
        # speaker-only block. It encodes the type+name so the row isn't blank.
        fallback = f"{label}: {full_name}" if full_name else label

        talk = {
            "id": tid,
            "session_id": "",   # filled by _flush_session
            "title": "",        # filled by _close_block
            "number": "",
            "start_ts": start_ts,
            "end_ts": end_ts or start_ts,
            "presenter": full_name,
            "speaker": full_name,
            "speaker_pos": 0 if authors else None,
            "authors": authors,
            "author_aliases": [full_name] if full_name else [],
            "institutions": institutions,
            "institutions_may_dedup": False,
            "abstract": "",
            "status": "Sessioned",
            "withdrawn": False,
            "first_author": full_name,
            "last_author": "",   # single-author talks: keep empty (sibling convention)
            "color": color,
            "location": "",
        }
        if remote:
            talk["status"] = remote
        cur_session_talks.append(talk)
        talks.append(talk)
        if cur_session_start is None or start_ts < cur_session_start:
            cur_session_start = start_ts
        end_ref = end_ts or start_ts
        if cur_session_end is None or end_ref > cur_session_end:
            cur_session_end = end_ref
        if cur_session_chair == "" and chair:
            cur_session_chair = chair
        cur_session_phase = phase
        cur_session_venue = venue
        # Tutorial talks in this conference are stand-alone between-sessions
        # warm-ups (every School-phase tutorial gets its own slot, and the
        # Workshop phase opens each day plus often slips a tutorial in between
        # two "Session N: …" runs — see Sunday 10:45 "Tutorial on cavity
        # solitons"). They should never inherit the topic that the previous
        # technical session was carrying. Keynote / Invited / Contributed
        # talks ARE part of the surrounding Session topic and DO inherit.
        if (cur_session_topic == "" and pending_topic
                and kind != "tutorial"):
            cur_session_topic = pending_topic
            # `pending_topic` is NOT cleared here: a "Session N: …" header
            # naturally continues across the coffee/lunch break that follows
            # (the PDF doesn't re-print the header for a "(continued)" run
            # unless the topic changes). It is cleared only on day boundary,
            # phase change, or when a new "Session N:" header overwrites it.

        cur_block = {
            "talk": talk,
            "title_parts": [],
            "fallback_title": fallback,
        }

    # Index of the first poster-catalog line, so we stop walking the schedule
    # there and process posters separately.
    try:
        poster_start_idx = next(i for i, l in enumerate(lines)
                                if POSTER_MARKER_RE.match(l))
    except StopIteration:
        poster_start_idx = len(lines)

    for raw in lines[:poster_start_idx]:
        # Day header? (also resets chair and the carried-over Session topic
        # for the new day, so yesterday's "Session III: …" topic does not leak
        # into today's warm-up tutorial that runs before the day's first
        # explicit "Session N:" header).
        m = DAY_RE.match(raw)
        if m:
            _flush_session()
            dom = int(m.group("dom"))
            weekday = m.group("wd")
            v = m.group("venue") or ""
            if v.strip():
                venue = _short_venue(v.strip())
                ph = _venue_phase(venue)
                if ph:
                    phase = ph
            chair = ""
            pending_topic = ""
            continue
        # Venue / phase header?
        m = VENUE_RE.match(raw)
        if m:
            _flush_session()
            phase = m.group("phase").lower()
            venue = _short_venue(m.group("venue").strip())
            # Reset the per-session topic when phase changes, since "Session N:"
            # headers belong to the Workshop phase and shouldn't carry over.
            pending_topic = ""
            continue
        # Chair?
        m = CHAIR_RE.match(raw)
        if m:
            _flush_session()
            chair = _TBC_TAIL_RE.sub("", m.group("name").strip()).strip()
            continue
        # Session topic header?
        m = SESSION_HDR_RE.match(raw)
        if m:
            _flush_session()
            num, topic = m.group("num"), m.group("topic").strip()
            pending_topic = f"Session {num}: {topic}"
            continue
        # Standalone admin line (no time): keep the actionable ones (transit,
        # …) as end-of-day dividers; silently drop the rest (Free Evening).
        if not TIME_RE.match(raw):
            if _BARE_DROP_RE.match(raw):
                continue
            if _BARE_ADMIN_RE.match(raw):
                _flush_session()
                if dom is not None:
                    _emit_divider(_divider_label(raw) or raw,
                                  _iso(dom, "23:59"), None, dom, venue)
                continue
        # Time-line?
        m = TIME_RE.match(raw)
        if m and dom is not None:
            start_hhmm = _hhmm(m.group("s"))
            end_hhmm = _hhmm(m.group("e")) if m.group("e") else None
            head = m.group("head").strip()
            start_ts = _iso(dom, start_hhmm)
            end_ts = _iso(dom, end_hhmm) if end_hhmm else None
            # Divider (meal, break, ceremony, transit, …)?
            div = _divider_label(head)
            if div is not None:
                _flush_session()
                # The PDF's "17:30-19:00 Poster session I/II" lines are the
                # real schedule anchors for the poster catalog. We don't emit
                # them as standalone dividers — the synthetic POSTERS1 /
                # POSTERS2 sessions built from the catalog at the end of the
                # parse already sit at exactly those times, so emitting both
                # would double-book the slot on the timeline.
                if div != "Poster Session":
                    _emit_divider(div, start_ts, end_ts, dom, venue)
                continue
            # Talk header?
            kind, _spk, _ = _parse_speaker_head(head)
            if kind:
                # Same chair / same phase / same venue → keep accumulating in
                # the current session. Different chair has already triggered a
                # flush above; same-chair runs naturally stay together.
                _close_block()
                _open_talk(start_ts, end_ts, head)
                continue
            # Otherwise it's an untagged item like "14:30 Opening and remarks
            # on logistics, organization, etc." — treat as a General divider
            # carrying the head as its label (truncated if very long).
            _flush_session()
            label = re.sub(r"\s+", " ", head).strip()
            if len(label) > 80:
                label = label[:77].rstrip() + "…"
            _emit_divider(label, start_ts, end_ts, dom, venue)
            continue
        # Continuation line for the open talk's title.
        if cur_block is not None:
            if len(cur_block["title_parts"]) < MAX_TITLE_LINES:
                cur_block["title_parts"].append(raw)
            continue
        # Anything else: ignore (a stray header / footer line we don't
        # recognise; the run is well-formed so this is rare).

    _flush_session()

    # Suffix-number any EXPLICIT "Session N: …" title shared by more than one
    # technical session — they only split when the same Session run continues
    # across a coffee break, and two adjacent sessions with identical names
    # are visually confusing on the timeline. We deliberately do NOT touch
    # the generic "School" / "Workshop" fallback titles (warm-up tutorial
    # sessions); numbering those would just produce noise like
    # "School (1)" … "School (6)".
    from collections import Counter as _Counter
    _tech = [s for s in sessions
             if s["type"] in ("School", "Workshop")
             and s["title"].lower().startswith("session ")]
    _title_counts = _Counter(s["title"] for s in _tech)
    _seen: dict[str, int] = {}
    for s in _tech:
        if _title_counts[s["title"]] > 1:
            _seen[s["title"]] = _seen.get(s["title"], 0) + 1
            s["title"] = f"{s['title']} ({_seen[s['title']]})"

    # ---- posters: split the 32-entry catalog across the TWO scheduled poster
    # sessions. Per the PDF's "Poster sessions Thursday/Friday 17:30:" line,
    # both sit at 17:30-19:00 on their respective days, at Monte Verità.
    poster_slot_specs = [
        # (dom, start, end)
        (25, "17:30", "19:00"),   # Thursday 25 — Poster session I
        (26, "17:30", "19:00"),   # Friday 26   — Poster session II
    ]
    if poster_entries:
        n_slots = len(poster_slot_specs)
        per = -(-len(poster_entries) // n_slots)   # ceil
        groups = [poster_entries[i:i + per]
                  for i in range(0, len(poster_entries), per)] or [[]]
        while len(groups) > n_slots:
            groups[-2].extend(groups[-1])
            groups.pop()
        for gi, group in enumerate(groups):
            if not group:
                continue
            d_dom, s_hhmm, e_hhmm = poster_slot_specs[gi]
            sess_id = f"POSTERS{gi + 1}"
            tids: list[str] = []
            for entry in group:
                authors = _parse_poster_authors(entry["authors_raw"])
                speaker_name = authors[0]["name"] if authors else ""
                last_name = authors[-1]["name"] if len(authors) > 1 else ""
                pid = f"P{entry['n']:03d}"
                tids.append(pid)
                talks.append({
                    "id": pid,
                    "session_id": sess_id,
                    "title": entry["title"],
                    "number": f"P{entry['n']}",
                    "start_ts": _iso(d_dom, s_hhmm),
                    "end_ts": _iso(d_dom, e_hhmm),
                    "presenter": speaker_name,
                    "speaker": speaker_name,
                    "speaker_pos": 0 if authors else None,
                    "authors": authors,
                    "author_aliases": [a["name"] for a in authors],
                    # Poster section has no affiliations; emit a single
                    # synthetic "unknown" institution so author insts=[1]
                    # references resolve cleanly in the builder.
                    "institutions": [{"n": 1, "name": "", "alt_names": []}]
                                    if authors else [],
                    "institutions_may_dedup": False,
                    "abstract": "",
                    "status": "Sessioned",
                    "withdrawn": False,
                    "first_author": speaker_name,
                    "last_author": last_name,
                    "color": "teal",
                    "location": "",
                })
            sessions.append({
                "id": sess_id,
                "title": f"Poster Session {gi + 1}",
                "type": "Posters",
                "topic": "",
                "date": _dt.date(YEAR, MONTH, d_dom).strftime(
                    f"%d-%b-{YEAR}"),
                "location": "Monte Verità",
                "presider": "",
                "presider_aff": "",
                "details": "Posters recommended A0 portrait; pins provided on "
                           "site.",
                "start_ts": _iso(d_dom, s_hhmm),
                "end_ts": _iso(d_dom, e_hhmm),
                "color": "teal",
                "talk_ids": tids,
            })

    # ---- finalise: sort sessions in chronological order so the app's day
    # tabs read naturally even though we emitted dividers inline with talks.
    sessions.sort(key=lambda s: (s.get("start_ts") or "",
                                 0 if s["color"] != "rose" else 1))

    # Pool every affiliation source into one flat, de-duplicated, sorted list
    # for the builder's affiliation map.
    affiliation_pool: set[str] = set(aff_pool)
    # Split ";"-joined lists at the source (none in this dataset, but kept for
    # parity with the sibling conference processors).
    for v in list(affiliation_pool):
        for piece in v.split(";"):
            p = piece.strip()
            if p:
                affiliation_pool.add(p)

    data = {
        "conference_name": CONFERENCE_NAME,
        "sessions": sessions,
        "talks": talks,
        "session_types": SESSION_TYPES,
        "talk_types": TALK_TYPES,
        "affiliation_sources": sorted(affiliation_pool),
    }
    return data


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
            tags.append({"key": "Format", "value": fmt})
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
    data = build_conference_data()
    # This conference carries no session tags: the legacy type ("School"/"Workshop"/
    # "General"/"Posters") restates the title, and the topic was a bare
    # ordinal. Strip both so sessions emit no tags line.
    for _s in data["sessions"]:
        _s.pop("type", None)
        _s.pop("topic", None)
        _s.pop("tags", None)
    JSON_OUT.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    n_t = len(data["talks"])
    n_s = len(data["sessions"])
    n_auth = sum(len(t["authors"]) for t in data["talks"])
    log(f"[process] wrote {JSON_OUT.name}: {n_s} sessions, {n_t} talks, "
        f"{n_auth} author entries, "
        f"{len(data['affiliation_sources'])} affiliation strings.")


if __name__ == "__main__":
    main()
