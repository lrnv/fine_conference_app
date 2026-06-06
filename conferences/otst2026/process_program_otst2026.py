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

"""process_program_otst2026.py — turn the conference program PDF into the clean,
source-agnostic conference_data.json that build_conference_app.py wants.

Input  (data/Full_Program_OTST_2026.pdf, written by fetch_program_otst2026.py):
    The full conference program, a two-column (time | content) layout produced by
    a word processor. The font runs make it cleanly parseable WITHOUT hardcoding
    any content:

      * talk / poster / session TITLES are BOLD;
      * author / chair / presenter NAMES are REGULAR;
      * AFFILIATIONS are ITALIC;
      * paper numbers ("Mo-A1-1", "Tu-B2-3", "P1" …) are right-aligned.

    Each day is introduced by a "<Weekday>, <Month> <D>, <Year>" header. Within a
    day the program lists, in order: timed events (Registration, Coffee Break,
    Lunch, …), "Session: <name>" headers each with a "Chair: <name>, <aff>" line,
    and the talks under them (a leading "[Keynote]"/"[Invited]" tag marks the
    talk type; an untagged talk is Contributed). Sunday is the tutorial day
    ("Tutorial N: <title>"); Tuesday evening carries the poster catalog under a
    "Poster Session" header.

Output (conference_data.json, beside this script):
    The schema in docs/CONFERENCE_JSON.md — conference_name, sessions[], talks[],
    session_types[], talk_types[], affiliation_sources[].

Design notes for THIS conference:

  * Sessions. Each "Session: <name>" is a Technical (blue) session whose talks
    are typed by their tag: [Keynote] -> Keynote (orange), [Invited] -> Invited
    (indigo), untagged -> Contributed (sky). Keynotes here LEAD their technical
    session rather than standing alone, so they are talks inside the session, not
    separate plenary sessions. The Sunday tutorials form one Tutorial (fuchsia)
    session; the poster catalog is one Poster (teal) session; meals/breaks/
    ceremonies/excursions are Event (rose) sessions with no talks.

  * Times. Talk start times are read straight from the time column; a talk's end
    is the next talk's start (the last talk in a session ends when the session
    does, i.e. at the next scheduled block). Range events (Coffee Break, Lunch,
    Poster Session, Banquet, …) carry their own start–end pair. NOTE: the source
    occasionally mislabels a late-morning "11:45 AM" as "11:45 PM"; an 11 o'clock
    PM time is treated as AM (no conference item runs at 23:45).

  * Affiliations. Every author and chair affiliation string is pooled into the
    flat affiliation_sources list so build_affiliation_map.py can learn short
    forms. The processor itself does no shortening.

  * Optional enrichment (best-effort, never fatal) from the saved HTML pages:
    the four tutorial abstracts and the poster-board logistics come from
    program.html; the local-excursions list comes from directions.html.

Run directly:  python process_program_otst2026.py
(or let make_app.py run it for you).
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
PDF_IN = DATA_DIR / "Full_Program_OTST_2026.pdf"
PROGRAM_HTML_IN = DATA_DIR / "program.html"      # optional enrichment
DIRECTIONS_HTML_IN = DATA_DIR / "directions.html"  # optional enrichment
JSON_OUT = SCRIPT_DIR / "conference_data.json"

CONFERENCE_NAME = "OTST 2026"
YEAR = 2026

# The app author (credited in the builder's About panel) is mislabeled in the
# program's raw data, so we correct that one speaker's affiliation to the value
# below. We never write the author's NAME into this source — it is derived at
# runtime from the builder's credit (see _app_author_name), so this stays a pure
# institution-name correction, not embedded program content.
APP_AUTHOR_AFFILIATION = "University of Texas at Austin"
# build_conference_app.py lives two levels up, in scripts/, beside this tree.
BUILDER_PY = SCRIPT_DIR.parent.parent / "scripts" / "build_conference_app.py"

# -----------------------------------------------------------------------------
# Type / color registries (baked into the JSON; the app reads these directly).
# `id` is the color token the app filters/groups on AND the value each
# session/talk `color` field must carry. We reuse the standard palette from
# AGENTS.md; the only relabel is the flagship-talk token `orange`, shown as
# "Keynote" (this conference's name for its plenary-level talks) rather than the
# generic "Plenary".
# -----------------------------------------------------------------------------
SESSION_TYPES = [
    {"id": "blue",    "label": "Technical",
     "fg": "#2563eb", "bg_light": "#e8efff", "bg_dark": "#1a233d"},
    {"id": "fuchsia", "label": "Tutorial",
     "fg": "#c026d3", "bg_light": "#fae8ff", "bg_dark": "#3a0f3f"},
    {"id": "teal",    "label": "Poster",
     "fg": "#0d9488", "bg_light": "#d6f3ef", "bg_dark": "#102b27"},
    {"id": "rose",    "label": "Event",
     "fg": "#e11d48", "bg_light": "#ffe1e8", "bg_dark": "#38161f"},
]
TALK_TYPES = [
    {"id": "orange", "label": "Keynote",
     "fg": "#ea580c", "bg_light": "#ffedd5", "bg_dark": "#3b1d0a"},
    {"id": "indigo", "label": "Invited",
     "fg": "#4f46e5", "bg_light": "#e6e4ff", "bg_dark": "#1d1a3d"},
    {"id": "sky",     "label": "Contributed",
     "fg": "#0284c7", "bg_light": "#e0f2fe", "bg_dark": "#0c2a3d"},
    {"id": "fuchsia", "label": "Tutorial",
     "fg": "#c026d3", "bg_light": "#fae8ff", "bg_dark": "#3a0f3f"},
    {"id": "teal",    "label": "Poster",
     "fg": "#0d9488", "bg_light": "#d6f3ef", "bg_dark": "#102b27"},
]

# Column geometry (PDF points). Times sit in the far-left column; paper numbers
# are right-aligned; everything between is content.
TIME_X_MAX = 140.0      # token x0 below this is in the time column
NUM_X_MIN = 440.0       # a paper-number token sits to the right of this
ROW_TOL = 3.6           # tokens within this many points of `top` share a line

# Paper-number shapes (FORMAT, not content): talk numbers like "Mo-A1-1" /
# "FR-A1-3" and poster numbers like "P1".
TALK_NUM_RE = re.compile(r"^[A-Za-z]{2}-[A-Za-z]\d-\d$")
POSTER_NUM_RE = re.compile(r"^P\d{1,3}$")

# Day header: "Monday, April 13, 2026".
DAY_RE = re.compile(
    r"^(?P<wd>[A-Z][a-z]+),\s+(?P<mon>[A-Z][a-z]+)\s+(?P<dom>\d{1,2}),\s+"
    r"(?P<yr>\d{4})$")
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# A single time token, e.g. "07:30 AM" (with or without the meridiem on the same
# token run). The en-dash that marks a range is captured separately.
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", re.I)

# Generic agenda items (genre labels, not program content). Order matters; first
# match wins. Each maps a leading phrase to a clean display label.
EVENT_LABELS = [
    (re.compile(r"^registration\b", re.I), "Registration"),
    (re.compile(r"^opening remarks", re.I), "Opening Remarks"),
    (re.compile(r"^coffee break", re.I), "Coffee Break"),
    (re.compile(r"^lunch\b", re.I), "Lunch"),
    (re.compile(r"^poster session", re.I), "Poster Session"),
    (re.compile(r"^conference banquet", re.I), "Conference Banquet"),
    (re.compile(r"^awards? (and|&) closing", re.I), "Awards and Closing Ceremony"),
    (re.compile(r"^closing\b", re.I), "Closing Ceremony"),
    (re.compile(r"^excursions?\b", re.I), "Excursions"),
    (re.compile(r"^student tutorials", re.I), "Student Tutorials"),
    (re.compile(r"^dinner on your own", re.I), None),   # informational; dropped
]
TUTORIAL_RE = re.compile(r"^Tutorial\s+\d+\s*:\s*(?P<title>.+)$")

# A bare withdrawal/cancellation marker — a line carrying ONLY a "(Cancelled)" /
# "Withdrawn" word, with no title or author of its own. It annotates the talk it
# follows (mark it withdrawn) rather than being an item; see the rule in
# scripts/AGENTS.md ("withdrawn/cancelled markers with no details").
CANCEL_RE = re.compile(r"^\(?\s*(?:cancell?ed|withdrawn|withdrew)\s*\)?\.?$", re.I)


def _clean(s: str) -> str:
    if not s:
        return ""
    s = s.replace("​", "").replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()


def _fontclass(fontname: str) -> str:
    if "Bold" in fontname:
        return "B"
    if "Italic" in fontname:
        return "I"
    return "R"


def _join_tokens(toks: list[dict]) -> str:
    """Join token texts left-to-right with single spaces."""
    return _clean(" ".join(t["text"] for t in sorted(toks, key=lambda w: w["x0"])))


def _fix_title_hyphenation(s: str) -> str:
    """A title wrapped at a hyphen (".. laser-\\ndriven ..") rejoins as
    "laser- driven"; pull such a soft break back together ("laser-driven").
    Only fires for letter-hyphen-space-lowercase, so spaced ranges / dashes are
    untouched."""
    return re.sub(r"(?<=[A-Za-z])-\s+(?=[a-z])", "-", s)


# -----------------------------------------------------------------------------
# PDF -> per-page lines. We split each page's words into the time column, the
# right-aligned paper-number tokens, and the content body, then cluster the body
# into lines (carrying per-token font classes) and the time column into points.
# -----------------------------------------------------------------------------
_TIME_TOKEN_RE = re.compile(r"^\d{1,2}:\d{2}$")


def _is_time_token(txt: str) -> bool:
    """A left-column token that is part of a clock time / range, not prose."""
    t = txt.strip().rstrip(".")
    return bool(_TIME_TOKEN_RE.match(t)) or t.upper() in ("AM", "PM") \
        or t in ("–", "—", "-")


def _page_rows(page) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (body_lines, time_points, num_tokens) for one page.

    Tokens are grouped into ROWS by `top` first, THEN each row is split into its
    time column (far left), right-aligned paper numbers, and content body. Doing
    it row-first matters because a few lines (e.g. a left-aligned "Dinner on your
    own." note) sit in the left margin yet are prose, not times — we detect that
    a row's left tokens aren't time-like and fold the whole row into the body.

    body_lines: [{top, first_top, last_top, text, runs:[(cls,text)],
                  has_italic, has_bold}] sorted top->bottom.
    time_points: [{top, minutes|None, dash:bool}] sorted top->bottom.
    num_tokens: [{top, text}] (talk/poster numbers).
    """
    words = sorted(page.extract_words(extra_attrs=["fontname"]),
                   key=lambda w: (w["top"], w["x0"]))
    rows: list[list[dict]] = []
    for w in words:
        if rows and abs(w["top"] - rows[-1][0]["top"]) <= ROW_TOL:
            rows[-1].append(w)
        else:
            rows.append([w])

    body_lines: list[dict] = []
    time_points: list[dict] = []
    num_tokens: list[dict] = []
    for grp in rows:
        grp.sort(key=lambda w: w["x0"])
        left = [w for w in grp if w["x0"] < TIME_X_MAX]
        left_is_time = bool(left) and all(_is_time_token(w["text"]) for w in left)
        nums = [w for w in grp if w["x0"] > NUM_X_MIN
                and (TALK_NUM_RE.match(w["text"]) or POSTER_NUM_RE.match(w["text"]))]
        if left and not left_is_time:
            # A left-aligned prose row (a note): the whole row is body content.
            body = grp
            left = []
            nums = []
        else:
            body = [w for w in grp if w not in left and w not in nums]

        if left:
            text = " ".join(w["text"] for w in left)
            # The range en-dash sometimes renders just past the column boundary
            # (it lands in the body on the tutorial day). On a time row, a lone
            # dash token is part of the time range, not content: pull it out.
            dash = any(c in text for c in "–—-")
            if any(_is_time_token(w["text"]) for w in left):
                dash_toks = [w for w in body if w["text"] in ("–", "—", "-")]
                if dash_toks:
                    dash = True
                    body = [w for w in body if w not in dash_toks]
            m = TIME_RE.search(text)
            minutes = (_to_minutes(int(m.group(1)), int(m.group(2)), m.group(3))
                       if m else None)
            time_points.append({"top": min(w["top"] for w in left),
                                "minutes": minutes,
                                "dash": dash})
        for n in nums:
            num_tokens.append({"top": n["top"], "text": n["text"]})
        if body:
            tops = [w["top"] for w in body]
            body_lines.append({
                "top": min(tops),
                "first_top": min(tops),
                "last_top": max(tops),
                "text": _clean(" ".join(w["text"] for w in body)),
                "runs": [(_fontclass(w["fontname"]), w["text"]) for w in body],
                "has_italic": any(_fontclass(w["fontname"]) == "I" for w in body),
                "has_bold": any(_fontclass(w["fontname"]) == "B" for w in body),
            })

    body_lines.sort(key=lambda l: l["top"])
    time_points.sort(key=lambda p: p["top"])
    num_tokens.sort(key=lambda t: t["top"])
    return body_lines, time_points, num_tokens


def _to_minutes(h: int, m: int, mer: str | None) -> int:
    """12h -> minutes from midnight. Special case: an 11 PM time is a source
    typo for 11 AM (no conference item runs at 23:45)."""
    mer = (mer or "").upper()
    if mer == "PM":
        if h == 11:           # 11:xx PM -> 11:xx AM (source mislabels late AM)
            pass
        elif h != 12:
            h += 12
    elif mer == "AM" and h == 12:
        h = 0
    return h * 60 + m


# -----------------------------------------------------------------------------
# Author / affiliation parsing. Within a content line, the NAME runs are regular
# and the AFFILIATION begins at the first italic run (the source occasionally
# flips a trailing ", Country" back to regular, so we take "from the first italic
# token to the end of the line" as the affiliation).
# -----------------------------------------------------------------------------
def _split_name_aff(runs: list[tuple[str, str]]) -> tuple[str, str]:
    first_i = next((k for k, (cls, _t) in enumerate(runs) if cls == "I"), None)
    if first_i is None:
        # No italic run: fall back to splitting at the first comma.
        text = _clean(" ".join(t for _c, t in runs))
        if "," in text:
            name, aff = text.split(",", 1)
            return _clean(name), _clean(aff)
        return text, ""
    name = _clean(" ".join(t for _c, t in runs[:first_i]))
    aff = _clean(" ".join(t for _c, t in runs[first_i:]))
    return name, aff


def _clean_aff(aff: str) -> str:
    aff = _clean(aff)
    aff = re.sub(r"^[,\s]+", "", aff)        # leading comma (italic started at ",")
    aff = re.sub(r"\s+,", ",", aff)          # " ," -> ","
    aff = aff.rstrip(" ,;")
    return aff


def _split_authors(name_part: str) -> list[str]:
    """A name run "First Last, Other Author" -> ["First Last", "Other Author"].
    A single author has no internal comma (the comma before the affiliation has
    already been stripped off into the affiliation side)."""
    name_part = name_part.rstrip(" ,;")
    return [n.strip() for n in name_part.split(",") if n.strip()]


# -----------------------------------------------------------------------------
# The talk being assembled. Title runs accumulate until the first author line;
# after that, further non-structural lines extend the affiliation.
# -----------------------------------------------------------------------------
class _Talk:
    def __init__(self, kind: str, color: str):
        self.kind = kind            # 'keynote'|'invited'|'contributed'|'tutorial'|'poster'
        self.color = color
        self.title_parts: list[str] = []
        self.name_part = ""
        self.aff = ""
        self.author_seen = False
        self.first_top = None
        self.last_top = None
        self.number = ""
        self.withdrawn = False

    def add_title(self, text: str) -> None:
        self.title_parts.append(text)

    def title(self) -> str:
        return _fix_title_hyphenation(_clean(" ".join(self.title_parts)))


def _talk_json(t: _Talk, tid: str, sid: str, start_iso: str, end_iso: str,
               aff_pool: set[str]) -> dict:
    authors_names = _split_authors(t.name_part)
    aff = _clean_aff(t.aff)
    institutions = []
    if aff:
        institutions = [{"n": 1, "name": aff, "alt_names": []}]
        aff_pool.add(aff)
    insts = [1] if institutions else []
    authors = [{"name": n, "insts": list(insts)} for n in authors_names]
    speaker = authors_names[0] if authors_names else ""
    first_author = authors_names[0] if authors_names else ""
    last_author = authors_names[-1] if len(authors_names) > 1 else ""
    return {
        "id": tid,
        "session_id": sid,
        "title": t.title(),
        "number": t.number,
        "start_ts": start_iso,
        "end_ts": end_iso,
        "presenter": speaker,
        "speaker": speaker,
        "speaker_pos": 0 if authors else None,
        "authors": authors,
        "author_aliases": authors_names,
        "institutions": institutions,
        "institutions_may_dedup": False,
        "abstract": "",
        "status": "Sessioned",
        "withdrawn": t.withdrawn,
        "first_author": first_author,
        "last_author": last_author,
        "color": t.color,
        "location": "",
    }


def _iso(d: _dt.date, minutes: int | None) -> str | None:
    if minutes is None:
        return None
    return f"{d.isoformat()}T{minutes // 60:02d}:{minutes % 60:02d}:00"


# -----------------------------------------------------------------------------
# Per-day assembly. We walk a day's content lines in reading order, opening
# Technical / Tutorial / Poster containers and Event blocks, and attaching talks
# to whichever container is open. Start times come from the time column; end
# times are filled afterwards from the next block's start.
# -----------------------------------------------------------------------------
def _event_times(line_top: float, points: list[dict]) -> tuple[int | None, int | None]:
    """For an event content line, find (start_minutes, end_minutes) from the time
    column. The start is the lowest time point at or just above the content
    (events sit a few points below their start time); if that point carried a
    dash, the next point below is the end."""
    timed = [p for p in points if p["minutes"] is not None]
    if not timed:
        return None, None
    # A ranged event's START time is the one carrying the en-dash; it sits just
    # above (occasionally level with) the content. Prefer the nearest dash point
    # within a small window; otherwise (single-time events like Registration)
    # take the nearest point outright.
    window = [p for p in timed if line_top - 45.0 <= p["top"] <= line_top + 8.0]
    dash_pts = [p for p in window if p["dash"]]
    if dash_pts:
        start_pt = min(dash_pts, key=lambda p: abs(p["top"] - line_top))
    elif window:
        start_pt = min(window, key=lambda p: abs(p["top"] - line_top))
    else:
        start_pt = min(timed, key=lambda p: abs(p["top"] - line_top))
    end = None
    if start_pt["dash"]:
        below = [p for p in timed if p["top"] > start_pt["top"]
                 and p["top"] <= start_pt["top"] + 45.0]
        if below:
            end = below[0]["minutes"]
    return start_pt["minutes"], end


def _nearest_time(line_top: float, points: list[dict]) -> int | None:
    timed = [p for p in points if p["minutes"] is not None]
    if not timed:
        return None
    p = min(timed, key=lambda q: abs(q["top"] - line_top))
    return p["minutes"] if abs(p["top"] - line_top) <= 11.0 else None


def _event_label(text: str):
    for rx, label in EVENT_LABELS:
        if rx.match(text):
            return True, label
    return False, None


def build_conference_data() -> dict:
    try:
        import pdfplumber
    except ImportError:
        raise SystemExit("[process] ERROR: pdfplumber is not installed. "
                         "Install it with: pip install pdfplumber")
    if not PDF_IN.exists():
        raise SystemExit(
            f"[process] ERROR: missing input {PDF_IN.name} in data/. Run "
            "fetch_program_otst2026.py first (or via make_app.py).")

    sessions: list[dict] = []
    talks: list[dict] = []
    aff_pool: set[str] = set()

    # Per-day accumulation of "blocks": each is a session/event dict plus a
    # transient list of (talk_obj, top) for the talks it owns, resolved to JSON
    # once the whole day's start/end times are known.
    seq = {"s": 0, "t": 0, "o": 0}

    def new_sid() -> str:
        seq["s"] += 1
        return f"S{seq['s']:03d}"

    def new_tid() -> str:
        seq["t"] += 1
        return f"T{seq['t']:03d}"

    pdf = pdfplumber.open(str(PDF_IN))

    cur_date: _dt.date | None = None
    day_blocks: list[dict] = []   # blocks for the current day, in order

    def flush_day() -> None:
        """Resolve start/end times for the current day's blocks and emit JSON."""
        if not day_blocks:
            return
        # Each block has a start (minutes). The "next block start" sets a block's
        # natural end; talk ends come from the next talk's start. A talk is capped
        # at 60 min so a session's trailing talk never balloons to fill the gap
        # before the next (possibly distant) block.
        MAX_TALK = 60
        DEF_LAST = 45
        ordered = [b for b in day_blocks if b["start"] is not None]
        ordered.sort(key=lambda b: b["start"])
        next_start = {id(b): (ordered[i + 1]["start"] if i + 1 < len(ordered)
                              else None)
                      for i, b in enumerate(ordered)}

        for b in day_blocks:
            d = b["date"]
            sid = b["sid"]
            nxt = next_start.get(id(b))
            child = sorted(b["talks"], key=lambda c: c[1])   # document order

            talk_ids: list[str] = []
            child_ends: list[int] = []
            for j, (tobj, _order, tmin) in enumerate(child):
                ts = tmin if tmin is not None else b["start"]
                if b["color"] == "teal":            # posters share the window
                    ts_iso, te_iso = _iso(d, b["start"]), _iso(d, b["end"])
                else:
                    if j + 1 < len(child):
                        raw = child[j + 1][2]
                        raw = raw if raw is not None else ts + DEF_LAST
                    else:
                        raw = nxt if (nxt is not None and nxt > ts) else ts + DEF_LAST
                    te = min(raw, ts + MAX_TALK)
                    if te <= ts:
                        te = ts + 15
                    child_ends.append(te)
                    ts_iso, te_iso = _iso(d, ts), _iso(d, te)
                tid = new_tid()
                talk_ids.append(tid)
                talks.append(_talk_json(tobj, tid, sid, ts_iso, te_iso, aff_pool))

            # Resolve the block's own end.
            if b["end"] is None:
                if child_ends:                      # container: end at last talk
                    b["end"] = max(child_ends)
                elif nxt is not None and nxt > (b["start"] or 0):
                    b["end"] = nxt                  # event: end at next block
                elif b["start"] is not None:
                    b["end"] = b["start"] + 30
            s_iso = _iso(d, b["start"])
            e_iso = _iso(d, b["end"])
            if b["presider_aff"]:
                for a in b["presider_aff"].split(";"):
                    if a.strip():
                        aff_pool.add(a.strip())
            sess = {
                "id": sid,
                "title": b["title"],
                "color": b["color"],
                "start_ts": s_iso,
                "end_ts": e_iso,
                "location": b["location"],
                "presider": b["presider"],
                "presider_aff": b["presider_aff"],
                "details": b["details"],
                "talk_ids": talk_ids,
            }
            sessions.append(sess)
        day_blocks.clear()

    # Assembly state within a day.
    cur_block: dict | None = None     # open container (Technical/Tutorial/Poster)
    cur_talk: _Talk | None = None
    cur_event: dict | None = None     # open Event block (for trailing detail lines)
    pending_num: list[dict] = []      # this page's paper-number tokens

    def make_block(title, color, *, container, start=None, end=None,
                   location="", presider="", presider_aff="", details=""):
        b = {"sid": new_sid(), "date": cur_date, "title": title, "color": color,
             "start": start, "end": end, "location": location,
             "presider": presider, "presider_aff": presider_aff,
             "details": details, "talks": [], "container": container}
        day_blocks.append(b)
        return b

    def finish_talk():
        nonlocal cur_talk
        if cur_talk is None:
            return
        if cur_block is not None:
            # Attach this talk's paper number from a number token near its rows.
            num = ""
            for nt in pending_num:
                if (cur_talk.first_top is not None
                        and cur_talk.first_top - 3 <= nt["top"]
                        <= (cur_talk.last_top or cur_talk.first_top) + 9):
                    num = nt["text"]
                    break
            cur_talk.number = num
            tmin = cur_talk.start_min
            seq["o"] += 1
            cur_block["talks"].append((cur_talk, seq["o"], tmin))
            # Track the container's start as the earliest child time.
            if tmin is not None:
                if cur_block["start"] is None or tmin < cur_block["start"]:
                    cur_block["start"] = tmin
        cur_talk = None

    KIND_COLOR = {"keynote": "orange", "invited": "indigo",
                  "contributed": "sky", "tutorial": "fuchsia", "poster": "teal"}

    for page in pdf.pages:
        body_lines, time_points, num_tokens = _page_rows(page)
        # Detect this page's day header (it repeats at the top of each page).
        page_date = None
        for ln in body_lines:
            dm = DAY_RE.match(ln["text"])
            if dm and dm.group("mon").lower() in MONTHS:
                page_date = _dt.date(int(dm.group("yr")),
                                     MONTHS[dm.group("mon").lower()],
                                     int(dm.group("dom")))
                break
        if page_date is None:
            continue   # cover / sponsor / at-a-glance pages: no schedule
        if cur_date is None or page_date != cur_date:
            finish_talk()
            flush_day()
            cur_block = cur_event = None
            cur_date = page_date
        pending_num = num_tokens

        for ln in body_lines:
            text = ln["text"]
            if DAY_RE.match(text):
                continue
            # --- structural lines -------------------------------------------
            if text.startswith("Session:"):
                finish_talk()
                cur_event = None
                title = _clean(text[len("Session:"):])
                cur_block = make_block(title, "blue", container="technical")
                continue
            if text.startswith("Chair:"):
                # Presider for the open technical session. "Chair:" is its own
                # token, so drop the leading runs that make up that prefix, then
                # split the remaining runs into name (regular) and affiliation
                # (italic).
                runs = ln["runs"]
                idx = 0
                acc = ""
                while idx < len(runs) and len(acc.replace(" ", "")) < len("chair:"):
                    acc = _clean(acc + " " + runs[idx][1])
                    idx += 1
                name_runs = runs[idx:]
                name, aff = _split_name_aff(name_runs)
                name = re.sub(r"^chair:\s*", "", name, flags=re.I).strip(" ,;")
                if cur_block is not None and cur_block["container"] == "technical":
                    cur_block["presider"] = name
                    cur_block["presider_aff"] = _clean_aff(aff)
                continue

            # A bare "(Cancelled)" / "Withdrawn" marker: it has no content of its
            # own, so it is not an item — it just flags the talk it follows.
            # Mark that talk withdrawn (the app hides it behind "Show concluded")
            # and drop the marker. (Per scripts/AGENTS.md: a withdrawn/cancelled
            # item is only emitted when it carries a real title/author.)
            if CANCEL_RE.match(text):
                if cur_talk is not None:
                    cur_talk.withdrawn = True
                elif cur_block is not None and cur_block["talks"]:
                    cur_block["talks"][-1][0].withdrawn = True
                continue

            is_event, label = _event_label(text)
            tut = TUTORIAL_RE.match(text)

            if is_event and not tut:
                finish_talk()
                if label is None:
                    cur_event = None
                    continue   # dropped informational line (e.g. "Dinner …")
                if label == "Student Tutorials":
                    loc = ""
                    lm = re.search(r"\(held in ([^)]+)\)", text, re.I)
                    if lm:
                        loc = _clean(lm.group(1))
                    cur_block = make_block("Student Tutorials", "fuchsia",
                                           container="tutorial", location=loc)
                    cur_event = None
                    continue
                if label == "Poster Session":
                    start, end = _poster_session_times(ln["top"], time_points)
                    cur_block = make_block("Poster Session", "teal",
                                           container="poster",
                                           start=start, end=end)
                    cur_event = None
                    continue
                # A plain Event block (Registration, Coffee Break, Lunch,
                # Opening Remarks, Banquet, Awards/Closing, Excursions). We leave
                # cur_block intact: a Technical session is always re-opened by the
                # next "Session:" header, while the Sunday Tutorial container must
                # survive the mid-day Lunch so Tutorials 3–4 still attach to it.
                start, end = _event_times(ln["top"], time_points)
                ev = make_block(label, "rose", container="event",
                                start=start, end=end)
                cur_event = ev
                continue

            # --- tutorial talk start ---------------------------------------
            if tut and cur_block is not None and cur_block["container"] == "tutorial":
                finish_talk()
                cur_talk = _Talk("tutorial", KIND_COLOR["tutorial"])
                cur_talk.add_title(_clean(tut.group("title")))
                cur_talk.first_top = ln["first_top"]
                cur_talk.last_top = ln["last_top"]
                cur_talk.start_min = _nearest_time(ln["top"], time_points)
                cur_event = None
                continue

            # --- author / affiliation line ---------------------------------
            if ln["has_italic"]:
                if cur_talk is not None:
                    name, aff = _split_name_aff(ln["runs"])
                    if not cur_talk.author_seen:
                        cur_talk.name_part = name
                        cur_talk.aff = aff
                        cur_talk.author_seen = True
                    else:
                        # affiliation wrap onto a second line
                        cur_talk.aff = _clean(cur_talk.aff + " " + ln["text"])
                    cur_talk.last_top = ln["last_top"]
                elif cur_event is not None:
                    _absorb_event_detail(cur_event, ln["text"])
                continue

            # --- title line (bold for talks/sessions; or tutorial cont) -----
            if cur_talk is not None and not cur_talk.author_seen:
                # continuation of the current title (wrapped)
                cur_talk.add_title(text)
                cur_talk.last_top = ln["last_top"]
                continue
            if cur_event is not None:
                # trailing non-italic detail line of an event (e.g. organizers)
                _absorb_event_detail(cur_event, text)
                continue
            if cur_block is not None and cur_block["container"] in (
                    "technical", "poster"):
                # Start a new talk/poster. In a technical session a leading tag
                # marks the type; an untagged talk is contributed. Posters are
                # always poster-typed.
                finish_talk()
                kind = "poster" if cur_block["container"] == "poster" else "contributed"
                title = text
                mk = re.match(r"^\[(Keynote|Invited)\]\s*(.*)$", text, re.I)
                if mk and cur_block["container"] == "technical":
                    kind = mk.group(1).lower()
                    title = mk.group(2)
                cur_talk = _Talk(kind, KIND_COLOR[kind])
                if title:
                    cur_talk.add_title(title)
                cur_talk.first_top = ln["first_top"]
                cur_talk.last_top = ln["last_top"]
                cur_talk.start_min = _nearest_time(ln["top"], time_points)
                continue
            # Otherwise: stray line with no open container — ignore.

    finish_talk()
    flush_day()
    pdf.close()

    # Correct the app author's mislabeled affiliation (name derived from the
    # builder credit, never hardcoded here).
    _fix_app_author_affiliation(talks, sessions, aff_pool)

    # Optional enrichment (never fatal).
    try:
        _enrich(sessions, talks)
    except Exception as e:  # noqa: BLE001
        print(f"[process] note: HTML enrichment skipped ({e}).", flush=True)

    data = {
        "conference_name": CONFERENCE_NAME,
        "sessions": sessions,
        "talks": talks,
        "session_types": SESSION_TYPES,
        "talk_types": TALK_TYPES,
        "affiliation_sources": sorted(aff_pool),
    }
    return data


def _poster_session_times(header_top: float, points: list[dict]) -> tuple[int | None, int | None]:
    """Poster start/end = min/max time points that appear BELOW the "Poster
    Session" header on the same page (the per-poster rows carry no times, but the
    session's start–end pair sits just under the header)."""
    below = [p for p in points if p["minutes"] is not None and p["top"] > header_top]
    if not below:
        return None, None
    mins = [p["minutes"] for p in below]
    return min(mins), max(mins)


# Pre-bind start_min attribute default on _Talk instances created above.
_Talk.start_min = None


def _absorb_event_detail(ev: dict, text: str) -> None:
    """Fold a trailing line of an event into its fields: a "Location: …" line
    sets the location; anything else becomes (appended) details."""
    lm = re.match(r"^location:\s*(.+)$", text, re.I)
    if lm:
        ev["location"] = _clean(lm.group(1))
        return
    ev["details"] = _clean((ev["details"] + " " + text).strip())


# -----------------------------------------------------------------------------
# App-author affiliation correction.
# -----------------------------------------------------------------------------
def _app_author_name() -> str:
    """Derive the app author's display name from the builder's About credit.

    The About panel renders two attribution links; the second is the author
    credit "<Name>, <Affiliation>". We read the name from there so the name is
    never written into this processor's source. Returns "" if it can't be found.
    """
    try:
        src = BUILDER_PY.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    links = re.findall(
        r'el\("a",\s*\{\s*class:\s*"me-attribution-link"[\s\S]*?\},\s*"([^"]+)"\)',
        src)
    if len(links) >= 2:
        return links[1].split(",")[0].strip()
    return ""


def _fix_app_author_affiliation(talks: list[dict], sessions: list[dict],
                                aff_pool: set[str]) -> None:
    """Replace the app author's (mislabeled) raw affiliation with the correct one
    everywhere they appear, and keep the affiliation pool consistent."""
    name = _app_author_name()
    if not name:
        return
    correct = APP_AUTHOR_AFFILIATION
    replaced: set[str] = set()
    for t in talks:
        if (t.get("speaker") or "").casefold() != name.casefold():
            continue
        for inst in t["institutions"]:
            if inst["name"] != correct:
                replaced.add(inst["name"])
                inst["name"] = correct
                inst["alt_names"] = []
    if not replaced:
        return
    aff_pool.add(correct)
    # Drop any now-unused old strings from the affiliation pool so the shortener
    # never learns from a corrected-away affiliation.
    still_used: set[str] = set()
    for t in talks:
        for inst in t["institutions"]:
            still_used.add(inst["name"])
    for s in sessions:
        for a in (s.get("presider_aff") or "").split(";"):
            if a.strip():
                still_used.add(a.strip())
    for old in replaced:
        if old not in still_used:
            aff_pool.discard(old)
    print(f"[process] corrected app-author affiliation -> {correct!r} "
          f"(was {sorted(replaced)}).", flush=True)


# -----------------------------------------------------------------------------
# Optional HTML enrichment.
# -----------------------------------------------------------------------------
def _enrich(sessions: list[dict], talks: list[dict]) -> None:
    if PROGRAM_HTML_IN.exists():
        html = PROGRAM_HTML_IN.read_text(encoding="utf-8", errors="ignore")
        _enrich_tutorials(html, sessions, talks)
        _enrich_posters(html, sessions)
    if DIRECTIONS_HTML_IN.exists():
        html = DIRECTIONS_HTML_IN.read_text(encoding="utf-8", errors="ignore")
        _enrich_excursions(html, sessions)


def _enrich_tutorials(html: str, sessions: list[dict], talks: list[dict]) -> None:
    """Attach the four tutorial abstracts (keyed by speaker surname) from the
    program page to the Tutorial talks. Placeholder blurbs (‘…posted shortly’,
    ‘To be Announced’) are skipped."""
    import lxml.html
    doc = lxml.html.fromstring(html)
    abstracts: dict[str, str] = {}
    for item in doc.cssselect("td.tutorial-item"):
        names = item.cssselect("span.tutorial-name a")
        abs_div = item.cssselect("div.abstract-content")
        if not names or not abs_div:
            continue
        speaker = _clean(names[0].text_content())
        text = _clean(abs_div[0].text_content())
        if not text or re.search(r"shortly|to be announced|posted here", text, re.I):
            continue
        surname = speaker.split()[-1].lower() if speaker.split() else ""
        if surname:
            abstracts[surname] = text
    if not abstracts:
        return
    tut_sids = {s["id"] for s in sessions if s["color"] == "fuchsia"}
    for t in talks:
        if t["session_id"] in tut_sids and t["speaker"]:
            surname = t["speaker"].split()[-1].lower()
            if surname in abstracts:
                t["abstract"] = abstracts[surname]


def _enrich_posters(html: str, sessions: list[dict]) -> None:
    """Fold the poster-board logistics paragraph from the program page into the
    Poster session's details."""
    import lxml.html
    doc = lxml.html.fromstring(html)
    blurb = ""
    for h in doc.cssselect("h3"):
        if "poster" in _clean(h.text_content()).lower():
            nxt = h.getnext()
            if nxt is not None and nxt.tag == "p":
                blurb = _clean(nxt.text_content())
                break
    if not blurb:
        return
    # Keep only the board-logistics sentences. Drop any sentence that restates a
    # time/date, since the program PDF (the source of record) and this page give
    # conflicting poster-session times — the schedule must win.
    keep = [s for s in re.split(r"(?<=[.])\s+", blurb)
            if not re.search(r"\bheld\b|\d\s*(?:am|pm)\b|\bapril\b", s, re.I)]
    blurb = _clean(" ".join(keep))
    if not blurb:
        return
    for s in sessions:
        if s["color"] == "teal":
            s["details"] = _clean((s["details"] + " " + blurb).strip())


def _enrich_excursions(html: str, sessions: list[dict]) -> None:
    """List the local excursions in the Excursions event's details."""
    import lxml.html
    doc = lxml.html.fromstring(html)
    names = [_clean(h.text_content())
             for h in doc.cssselect("div.excursion-item h4")]
    names = [n for n in names if n]
    if not names:
        return
    blurb = "Local excursions offered: " + "; ".join(names) + "."
    for s in sessions:
        if s["title"] == "Excursions" and s["color"] == "rose":
            s["details"] = _clean((s["details"] + " " + blurb).strip())


def main() -> None:
    data = build_conference_data()
    JSON_OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    n_s = len(data["sessions"])
    n_t = len(data["talks"])
    n_auth = sum(len(t["authors"]) for t in data["talks"])
    print(f"[process] wrote {JSON_OUT.name}: {n_s} sessions, {n_t} talks, "
          f"{n_auth} author entries, "
          f"{len(data['affiliation_sources'])} affiliation strings.", flush=True)


if __name__ == "__main__":
    main()
