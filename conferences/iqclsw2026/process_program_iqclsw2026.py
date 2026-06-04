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

"""process_program_iqclsw2026.py — turn the IQCLSW 2026 program text into the
clean, source-agnostic conference_data.json that build_conference_app.py wants.

Input  (data/detailed_program.txt, written by fetch_program_iqclsw2026.py):
    The visible text of the single detailed-program page. It is organized as:

        ◊ Monday 29 June
        14:30-15:45 — <Talk Title>
        <Authors line, optionally with superscript affiliation markers>
        <Affiliation line(s): "1. Inst (Country) ; 2. Inst (Country)" or a
                              single unnumbered institution>
        ...
        LIST OF POSTERS
        • <Poster Title>
        <Authors>
        <Affiliations>
        ...

Output (conference_data.json, beside this script):
    The schema documented in build_conference_app.py:
      conference_name, sessions[], talks[], session_types[], talk_types[],
      affiliation_sources[] (one flat, de-duplicated list of raw affiliation
      strings).

Design notes for THIS conference:

  * IQCLSW is a small single-track school/workshop: there are no parallel
    rooms, no presiders, no paper numbers, and no per-talk abstracts on the
    page. Every value the builder reads but the source doesn't carry is emitted
    as a well-typed empty (""/[]/false) so the app renders gracefully.

  * Day -> calendar date. The page gives weekday + day-of-month + month name
    (e.g. "Monday 29 June"); the year is 2026. We turn each timed entry's
    "HH:MM-HH:MM" plus its day into ISO start_ts/end_ts. The Friday gala block
    "15:30-00:00" is treated as ending at midnight of the SAME calendar day
    (00:00 < 15:30), which is fine for ordering — it sorts last that day.

  * We model each DAY as one session (a single-track day), and every academic
    talk that day becomes a talk under it. That gives the app its natural
    "tap a day, see its talks" structure without inventing a session hierarchy
    the source doesn't have. Non-academic blocks (meals, coffee breaks, the
    poster *sessions*, opening/closing, receptions, dinners) are NOT emitted as
    talks; they're house-keeping, and the app is about the science. The whole
    LIST OF POSTERS becomes its own "Posters" session, one talk per poster.

  * Color/type. Talks are classified into the standard type registry below:
      - school-phase lectures                        -> "fuchsia" (Tutorial)
      - workshop-phase invited lectures              -> "indigo"  (Invited)
      - short contributed talks                      -> "sky"     (Contributed)
      - posters                                      -> "teal"    (Poster)
    The heuristic: 30+ minute slots are invited/tutorial lectures, shorter
    slots are contributed. School sessions are Tutorial (fuchsia), Workshop
    sessions are Technical (blue); house-keeping dividers are Event (rose).

  * Affiliation sources. Every institution name we parse is pooled into the
    flat affiliation_sources list so build_affiliation_map.py can learn short
    forms. (This program has no presiders and no full-address lines.)

Run directly:  python process_program_iqclsw2026.py
(or let make_app.py run it for you).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
# The fetch script saves only the raw page HTML (the source of record); the
# processor extracts the text it needs from these with lxml (see _html_to_text).
HTML_IN = DATA_DIR / "detailed_program.html"
OVERVIEW_HTML_IN = DATA_DIR / "program_overview.html"   # session names (optional)
# Back-compat: if a pre-extracted .txt is present (older pipeline), use it.
TEXT_IN = DATA_DIR / "detailed_program.txt"
OVERVIEW_IN = DATA_DIR / "program_overview.txt"
JSON_OUT = SCRIPT_DIR / "conference_data.json"

PROGRAM_MARKER = "DETAILED PROGRAM"
POSTER_MARKER = "LIST OF POSTERS"
OVERVIEW_MARKER = "PROGRAM AT A GLANCE"

CONFERENCE_NAME = "IQCLSW 2026"
YEAR = 2026


# -----------------------------------------------------------------------------
# HTML → text extraction. The fetch script saves only the raw page HTML; we
# render it to the line-structured plain text the parsers below expect, using
# lxml. The key is to emit a newline at every BLOCK-level boundary (and <br>),
# while keeping inline runs (span/a/em/sup…) joined — this reproduces the
# logical line breaks the program/overview parsers rely on (each timed block,
# author line and affiliation line on its own line) without the inline
# fragmentation a naive get_text() would cause.
# -----------------------------------------------------------------------------
_BLOCK_TAGS = {
    "div", "p", "section", "article", "tr", "table", "thead", "tbody",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "header", "footer",
    "figure", "figcaption", "blockquote", "br",
}

# Sentinel injected by _html_to_text immediately before an underlined text run.
# The source program marks the PRESENTING author by underlining their name
# (`<span style="text-decoration: underline">`), so this sentinel is how that
# signal survives the HTML->text flattening: the author parser uses it to set
# speaker_pos, then strips it. A C0 control char so it can never collide with
# real program text and is trivially removed everywhere a name/title is cleaned.
_SPEAKER_MARK = "\x02"


def _is_underline(el) -> bool:
    """True if an element carries an inline underline style — the source's way
    of marking the presenting author within an author list."""
    style = (el.get("style") or "").lower()
    return "underline" in style and "text-decoration" in style


def _html_to_text(html: str, marker: str, end_marker: str | None = None) -> str:
    """Extract the page's content region as line-structured text.

    `marker` anchors the region of interest (e.g. "DETAILED PROGRAM"); the
    returned text starts at the LAST line containing it (the page chrome repeats
    the title in nav menus, so the last occurrence is the real content heading).
    `end_marker`, when given, makes the region selector walk up to the smallest
    ancestor that contains BOTH markers — needed for the detailed program, whose
    schedule and "LIST OF POSTERS" catalog live in sibling containers under one
    section.
    """
    import lxml.html

    doc = lxml.html.fromstring(html)
    for bad in doc.xpath("//script|//style|//noscript|//nav|//header|//footer"):
        bad.getparent().remove(bad)

    # Pick the region: start at the heading whose own text IS the marker, then
    # walk up to the smallest ancestor that contains end_marker (if given) or is
    # comfortably larger than just the heading.
    heads = [el for el in doc.iter()
             if isinstance(el.tag, str)
             and (el.text or "").strip().lower() == marker.lower()]
    if heads:
        node = heads[-1]
        while node.getparent() is not None:
            tc = node.text_content().lower()
            if end_marker:
                if end_marker.lower() in tc:
                    break
            elif len(node.text_content().strip()) >= 400:
                break
            node = node.getparent()
    else:
        # Fallback: smallest element whose text_content contains the marker.
        cands = [el for el in doc.iter()
                 if isinstance(el.tag, str)
                 and marker.lower() in (el.text_content() or "").lower()]
        node = min(cands, key=lambda e: len(e.text_content())) if cands else doc

    parts: list[str] = []

    def _walk(el, u_depth: int = 0) -> None:
        el_underlined = _is_underline(el)
        if el.tag == "br":
            parts.append("\n")
        # Emit the speaker sentinel just before an underlined run's text, but
        # only at the OUTERMOST underline (u_depth == 0) so a nested span can't
        # double-mark the same name. The marker precedes the name; the element's
        # own tail (the rest of the author list after </span>) stays unmarked.
        if el_underlined and u_depth == 0:
            parts.append(_SPEAKER_MARK)
        if el.text and el.text.strip():
            parts.append(re.sub(r"[ \t\r\n]+", " ", el.text))
        child_depth = u_depth + (1 if el_underlined else 0)
        for ch in el:
            if not isinstance(ch.tag, str):   # comment / processing instruction
                if ch.tail and ch.tail.strip():
                    parts.append(re.sub(r"[ \t\r\n]+", " ", ch.tail))
                continue
            blk = ch.tag in _BLOCK_TAGS
            if blk:
                parts.append("\n")
            _walk(ch, child_depth)
            if blk:
                parts.append("\n")
            if ch.tail and ch.tail.strip():
                parts.append(re.sub(r"[ \t\r\n]+", " ", ch.tail))

    _walk(node)
    text = "".join(parts)
    # Ensure a space after the "HH:MM-HH:MM —" time separator (inline spans
    # sometimes butt the dash against the title), and drop NBSPs.
    text = re.sub(r"(\d{2}:\d{2}-\d{2}:\d{2})\s*—\s*", r"\1 — ", text)
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in text.split("\n")]
    lines = [l for l in lines if l]
    # Trim page chrome before the real content heading (last marker occurrence).
    idxs = [k for k, l in enumerate(lines) if marker.lower() in l.lower()]
    if idxs:
        lines = lines[idxs[-1]:]
    return "\n".join(lines)


def _load_program_text() -> str:
    """The detailed-program text: from the saved HTML (preferred), else a
    pre-extracted .txt for back-compat with the older pipeline."""
    if HTML_IN.exists():
        return _html_to_text(HTML_IN.read_text(encoding="utf-8"),
                             PROGRAM_MARKER, POSTER_MARKER)
    if TEXT_IN.exists():
        return TEXT_IN.read_text(encoding="utf-8")
    raise SystemExit(
        f"[process] ERROR: missing input — expected {HTML_IN.name} (or "
        f"{TEXT_IN.name}) in data/. Run fetch_program_iqclsw2026.py first "
        "(or via make_app.py).")


def _load_overview_text() -> str:
    """The overview text (optional): from saved HTML, else a .txt, else ''."""
    if OVERVIEW_HTML_IN.exists():
        return _html_to_text(OVERVIEW_HTML_IN.read_text(encoding="utf-8"),
                             OVERVIEW_MARKER)
    if OVERVIEW_IN.exists():
        return OVERVIEW_IN.read_text(encoding="utf-8")
    return ""


# -----------------------------------------------------------------------------
# Type / color registries (baked into the JSON; the app reads these directly).
# `id` is the color token the app filters and groups on, AND the token each
# session/talk's `color` field must use. The conference's color caption is a
# three-way split — invited-for-school / invited-for-workshop / contributed &
# posters — which we model as the talk types below. Sessions are colored by
# their dominant talk type, so the session registry mirrors the same tokens
# (the app's Sessions tab then groups/filters by talk character, which is the
# only meaningful axis on a single-track program).
# -----------------------------------------------------------------------------
# Standard session/talk type taxonomy. The shared types; a conference only
# surfaces the ones its program actually uses (the app hides count-0 types).
SESSION_TYPES = [
    {"id": "blue",    "label": "Technical",
     "fg": "#2563eb", "bg_light": "#e8efff", "bg_dark": "#1a233d"},
    {"id": "fuchsia", "label": "Tutorial",
     "fg": "#c026d3", "bg_light": "#fae8ff", "bg_dark": "#3a0f3f"},
    {"id": "teal",   "label": "Poster",
     "fg": "#0d9488", "bg_light": "#d6f3ef", "bg_dark": "#102b27"},
    {"id": "rose",   "label": "Event",
     "fg": "#e11d48", "bg_light": "#ffe1e8", "bg_dark": "#38161f"},
]
TALK_TYPES = [
    {"id": "indigo", "label": "Invited",
     "fg": "#4f46e5", "bg_light": "#e6e4ff", "bg_dark": "#1d1a3d"},
    {"id": "sky",    "label": "Contributed",
     "fg": "#0284c7", "bg_light": "#e0f2fe", "bg_dark": "#0c2a3d"},
    {"id": "fuchsia", "label": "Tutorial",
     "fg": "#c026d3", "bg_light": "#fae8ff", "bg_dark": "#3a0f3f"},
    {"id": "teal",   "label": "Poster",
     "fg": "#0d9488", "bg_light": "#d6f3ef", "bg_dark": "#102b27"},
    {"id": "rose",   "label": "Event",
     "fg": "#e11d48", "bg_light": "#ffe1e8", "bg_dark": "#38161f"},
]

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# Superscript digit -> ASCII digit, for affiliation markers on author names.
SUP = {"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5",
       "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9"}
SUP_CLASS = "".join(re.escape(c) for c in SUP)

# Agenda blocks that DIVIDE sessions. The program's session structure (per the
# Program-at-a-Glance overview) splits the day into a MORNING and an AFTERNOON
# session at the mid-day meal/free-time, with evening events (dinner, reception,
# excursion, gala, poster session) standing apart. So only meals/free-time and
# evening events end a session — NOT coffee breaks (see below).
NON_TALK_PATTERNS = [
    r"^lunch\b", r"^dinner\b",
    r"^welcome reception", r"^reception\b", r"^poster session\b",
    r"^excursion\b", r"^gala\b", r"^free time\b", r"^poster prize\b",
    r"^registration\b", r"^lunch & ", r"^lunch and ",
]
_NON_TALK_RE = [re.compile(p, re.I) for p in NON_TALK_PATTERNS]

# Coffee breaks do NOT divide a session — the overview groups all talks in a
# morning (or afternoon) into ONE session regardless of the coffee break in the
# middle. So a coffee break is folded into the session as a (no-author) General
# talk at its time slot, exactly like Opening / Closing Remarks.
_INSESSION_BREAK_RE = re.compile(r"^(coffee break|break)\b", re.I)

# A poster-session divider is special: it both ends a session AND records a real
# scheduled poster slot whose time we attach to the poster catalog.
_POSTER_SLOT_RE = re.compile(r"^poster session\b", re.I)


def _block_kind(first_line: str) -> str:
    """Classify a block by its first (title) line:
        'talk'         — a scientific talk OR an in-session agenda item
                         (coffee break, Opening, Closing) folded into the session
        'poster_slot'  — a scheduled "Poster session" divider
        'divider'      — a meal/free-time/evening item that ENDS the session
    """
    s = _strip_emphasis(first_line)
    if _POSTER_SLOT_RE.search(s):
        return "poster_slot"
    if _INSESSION_BREAK_RE.search(s):
        return "talk"           # coffee break -> in-session General talk
    if any(rx.search(s) for rx in _NON_TALK_RE):
        return "divider"
    return "talk"


# Tidy display labels for the common divider blocks. The raw program text is
# sometimes verbose ("Lunch & free time", "Welcome Reception + Dinner"); we
# title the session by the meal/break itself. Order matters — first match wins.
_DIVIDER_LABELS = [
    (re.compile(r"coffee break", re.I), "Coffee Break"),
    (re.compile(r"\bbreak\b", re.I), "Break"),
    (re.compile(r"\blunch\b", re.I), "Lunch"),
    (re.compile(r"\bdinner\b", re.I), "Dinner"),
    (re.compile(r"reception", re.I), "Reception"),
    (re.compile(r"poster prize", re.I), "Poster Prize"),
    (re.compile(r"poster session", re.I), "Poster Session"),
    (re.compile(r"\bopening\b", re.I), "Opening"),
    (re.compile(r"closing", re.I), "Closing Remarks"),
    (re.compile(r"excursion|gala", re.I), "Excursion & Gala Dinner"),
    (re.compile(r"free time", re.I), "Free Time"),
    (re.compile(r"registration", re.I), "Registration"),
]


def _divider_label(first_line: str) -> str:
    """Return a clean session title for a meal/break/admin block."""
    s = _strip_emphasis(first_line)
    for rx, label in _DIVIDER_LABELS:
        if rx.search(s):
            return label
    # Fallback: use the raw label, trimmed of trailing decoration.
    return _clean(s).rstrip(" .–—-") or "Break"


# Day header line: "◊ Monday 29 June"
DAY_RE = re.compile(r"^\s*[◊◆♦•*]*\s*"
                    r"(?P<wd>[A-Za-z]+)\s+(?P<dom>\d{1,2})\s+(?P<mon>[A-Za-z]+)\s*$")
# Timed block start: "14:30-15:45 — rest" (any dash variant, optional trailing).
TIME_RE = re.compile(
    r"^(?P<s>\d{1,2}:\d{2})\s*[-–—]\s*(?P<e>\d{1,2}:\d{2})\s*[-–—]?\s*(?P<rest>.*)$")
# Poster bullet: "• Title"
POSTER_RE = re.compile(r"^\s*[•▪◦‣]\s*(?P<title>.+?)\s*$")


def _clean(s: str) -> str:
    """Collapse whitespace (incl. zero-width and NBSP) and trim. Also drops the
    speaker sentinel so it can never survive into an emitted name/affiliation."""
    if not s:
        return ""
    s = s.replace("\u200b", "").replace("\u200e", "").replace("\u00a0", " ")
    s = s.replace(_SPEAKER_MARK, "")
    return re.sub(r"\s+", " ", s).strip()


def _strip_emphasis(s: str) -> str:
    """Strip stray markdown/emphasis artifacts and surrounding quotes that
    occasionally survive the text extraction (e.g. '*Title coming soon*')."""
    s = s.replace(_SPEAKER_MARK, "").strip()
    s = re.sub(r"^[\*_]+|[\*_]+$", "", s).strip()
    return s


def _iso(dom: int, month: int, hhmm: str) -> str:
    h, m = hhmm.split(":")
    return f"{YEAR:04d}-{month:02d}-{dom:02d}T{int(h):02d}:{int(m):02d}:00"


def _looks_like_affiliation(line: str) -> bool:
    """True if a line is an affiliation block rather than an author list.

    Affiliation lines either start with a numbered marker ("1. ...") or carry
    a country-in-parentheses tail / strong institution keywords, and tend not
    to be a short comma list of Person Names.
    """
    s = line.strip()
    if re.match(r"^\d{1,2}[.\)]\s", s):
        return True
    if re.search(r"\([A-Za-zÀ-ÿ .'’\-]+\)\s*$", s) and any(
            kw in s for kw in (
                "University", "Universit", "Institute", "Institut",
                "Laboratory", "Laboratoire", "Lab", "CNR", "CNRS", "CEA",
                "ETH", "Technische", "School", "Centre", "Center", "GmbH",
                "Technology", "Photonics", "Physics", "Department",
                "Dipartimento", "National", "Academy", "Inc", "Labs")):
        return True
    return False


def _split_numbered_insts(line: str) -> list[tuple[int, str]]:
    """Split "1. A (X) ; 2. B (Y), 3. C (Z)" into [(1,'A (X)'), ...].

    A real institution marker is a number-dot (or number-paren) that sits at a
    DELIMITER boundary — the start of the string, or right after the ';' / ','
    that separates two numbered institutions. Requiring that boundary stops a
    number that merely lives inside an institution name from being mistaken for
    a marker — e.g. the "9)" in "Peter Gruenberg Institute 9 (PGI 9) (Germany)"
    must NOT split institution 3 into a bogus "#9". The separator before each
    marker may be ';', ',', or nothing; markers themselves are "N." or "N)".
    """
    anchors: list[tuple[int, int]] = []
    for m in re.finditer(r"(?:^|[;,])\s*(\d{1,2})[.\)]\s", line):
        # m.start(1) is where the number itself begins (after the separator/ws).
        anchors.append((m.start(1), int(m.group(1))))
    # Also accept a LEADING bare-number marker with no period, e.g.
    # "1 Laboratoire de physique …" (a source typo). Only at the very start, so
    # we never mistake a number embedded in an address (like "Building 2") for
    # an institution marker.
    lead = re.match(r"^(\d{1,2})\s+(?=[A-Za-zÀ-ÿ])", line)
    if lead and not any(pos == 0 for pos, _ in anchors):
        anchors = [(0, int(lead.group(1)))] + anchors
        anchors.sort()
    if not anchors:
        return []
    out: list[tuple[int, str]] = []
    for i, (pos, num) in enumerate(anchors):
        end = anchors[i + 1][0] if i + 1 < len(anchors) else len(line)
        seg = line[pos:end]
        # Strip the marker: "N." / "N)" / a leading bare "N ".
        seg = re.sub(r"^\d{1,2}([.\)]\s*|\s+)", "", seg).strip()
        seg = seg.rstrip(" ;,:")
        if seg:
            out.append((num, _clean(seg)))
    return out


def _parse_institutions(aff_lines: list[str]) -> list[dict]:
    """Parse one or more affiliation lines into [{n, name, alt_names}].

    Numbered form wins if present (joined across lines first). Otherwise the
    whole thing is a single unnumbered institution -> n=1.
    """
    joined = " ; ".join(l.strip() for l in aff_lines if l.strip())
    joined = _clean(joined)
    if not joined:
        return []
    # Drop a DANGLING trailing numbered marker — a "N." / "N)" with no
    # institution body after it (e.g. the source line
    # "1. … Politecnico di Milano (Italy) ; 2."). Left in place it gets
    # swallowed into the previous institution's name (defeating the shortener)
    # or yields an empty institution. Repeat to clear several (". ; 2. ; 3.").
    while True:
        trimmed = re.sub(r"[;,]?\s*\d{1,2}[.\)]\s*$", "", joined).strip()
        if trimmed == joined:
            break
        joined = trimmed
    if not joined:
        return []
    numbered = _split_numbered_insts(joined)
    insts: list[dict] = []
    if numbered:
        for n, name in numbered:
            insts.append({"n": n, "name": name, "alt_names": []})
    else:
        insts.append({"n": 1, "name": joined, "alt_names": []})
    return insts


def _parse_author_token(tok: str) -> tuple[str, list[int]]:
    """Parse one author token -> (name, [inst numbers]).

    Handles a leading 'and ', and a trailing run of superscript digits
    (optionally space-separated, e.g. 'Adam Bieganski¹ ³' -> insts [1,3]).
    """
    tok = _clean(tok)
    tok = re.sub(r"^and\s+", "", tok, flags=re.I)
    insts: list[int] = []
    m = re.search(rf"([{SUP_CLASS}\s]+)$", tok)
    if m:
        run = m.group(1)
        digits = [SUP[c] for c in run if c in SUP]
        if digits:
            insts = [int(d) for d in digits]
            tok = tok[:m.start()].strip()
    else:
        # Fallback: the source sometimes glues a PLAIN ASCII marker to the end
        # of a surname where a superscript was intended (e.g.
        # 'R. E. Dunin-Borkowski5'). Only treat a trailing 1-2 digit run as a
        # marker when it directly follows a letter (so we don't mangle a name
        # that legitimately ends in a number, which is vanishingly rare here).
        m2 = re.search(r"(?<=[A-Za-zÀ-ÿ])(\d{1,2})$", tok)
        if m2:
            insts = [int(m2.group(1))]
            tok = tok[:m2.start()].strip()
    # Strip a trailing lone comma/period left after marker removal.
    tok = tok.strip().rstrip(",")
    return tok, insts


def _parse_authors(line: str, institutions: list[dict]
                   ) -> tuple[list[dict], list[str], int | None]:
    """Parse an author line into (authors, aliases, speaker_idx).

    Each author is {name, insts}. When the talk has exactly one institution and
    NO author carried an explicit marker, every author is attributed to inst 1.
    Any author reference to an institution number that wasn't actually parsed
    is dropped, so the emitted data is always internally consistent (the builder
    rejects dangling references). Aliases collect the loose forms for search.

    `speaker_idx` is the index (into the returned authors list) of the author
    whose name carried the _SPEAKER_MARK sentinel — i.e. the presenting author,
    underlined in the source. It is None when no author was marked (the source
    occasionally omits the underline), letting the caller fall back to author 0.
    """
    valid_nums = {i["n"] for i in institutions}
    n_insts = len(institutions)
    authors: list[dict] = []
    speaker_idx: int | None = None
    for tok in line.split(","):
        tok = tok.strip()
        if not tok:
            continue
        is_speaker = _SPEAKER_MARK in tok
        tok = tok.replace(_SPEAKER_MARK, "")
        name, insts = _parse_author_token(tok)
        if name:
            if is_speaker and speaker_idx is None:
                speaker_idx = len(authors)
            authors.append({"name": name, "insts": insts})

    any_marker = any(a["insts"] for a in authors)
    if not any_marker and n_insts == 1:
        for a in authors:
            a["insts"] = [1]

    # Drop references to institutions that don't exist (source numbering gaps /
    # truncated affiliation lines), keeping order and de-duping.
    for a in authors:
        seen: set[int] = set()
        kept: list[int] = []
        for n in a["insts"]:
            if n in valid_nums and n not in seen:
                seen.add(n)
                kept.append(n)
        a["insts"] = kept

    aliases = [a["name"] for a in authors]
    return authors, aliases, speaker_idx


def _looks_like_authors(line: str) -> bool:
    """Heuristic: a line is an author list if it has comma-separated tokens that
    look like person names (capitalized words / initials), and is not itself an
    affiliation line."""
    s = line.strip()
    if not s or _looks_like_affiliation(s):
        return False
    # Author lines are usually "First Last, F. Last, ..." — short tokens, lots
    # of capitals/initials, few institution keywords.
    if re.search(r"\b(University|Institut|Laborat|CNRS|CNR|ETH|GmbH|"
                 r"Department|Dipartimento|School of)\b", s):
        return False
    # At least one capitalized name-ish token.
    return bool(re.search(r"[A-ZÀ-Þ][a-zà-ÿ]+|\b[A-Z]\.", s))


def _split_glued_name_aff(line: str) -> tuple[str, str]:
    """Some single-author blocks glue the speaker and affiliation on ONE line,
    separated by a run of >=2 spaces, e.g.
        'Benedikt Schwarz  Technische Universität Wien (Austria)'
    Return (author_part, aff_part) if such a split is detected, else (line, '').
    """
    m = re.search(r"^(.*?\S)\s{2,}(\S.*)$", line)
    if not m:
        return line, ""
    left, right = m.group(1).strip(), m.group(2).strip()
    if _looks_like_affiliation(right) and not _looks_like_affiliation(left):
        return left, right
    return line, ""


# Titles that are "General" program items regardless of slot length: the
# opening/closing housekeeping. (Coffee breaks and meals are also General — see
# _BREAK_TITLE_RE below — and a memorial lecture is treated as a
# School talk, not General, per the program's framing.)
_GENERAL_TITLE_RE = re.compile(
    r"^\s*(opening|closing\b)", re.I)

# In-session coffee breaks are folded into the session as agenda talks (not
# session splits). They are classified as Event (rose), same as the other
# housekeeping items.
_BREAK_TITLE_RE = re.compile(r"^\s*(coffee break|break\b)", re.I)

# Titles that are School talks regardless of their (short) slot length: named
# lectures (e.g. an opening named lecture) and memorial lectures that belong to
# the School program rather than being General housekeeping items. The generic
# "in memoriam" memorial marker is built in; any program-specific title patterns
# are read at runtime from an optional data file so no real talk title lives in
# tracked source (one regex per line, '#' comments and blanks ignored):
#
#     DATA_DIR / "school_title_patterns.txt"
def _load_school_title_re() -> "re.Pattern":
    pats = [r"in memoriam\b"]
    path = DATA_DIR / "school_title_patterns.txt"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                pats.append(line)
    return re.compile("|".join(pats), re.I)


_SCHOOL_TITLE_RE = _load_school_title_re()


# Some overview slots list two names for one session — a primary topic plus a
# secondary "(… session)" label that should be appended in parentheses rather
# than split off into its own session. Which names count as secondary is
# program-specific (and may include a person's name), so the matching substrings
# are read at runtime from an optional data file (case-insensitive substring
# match, one per line, '#' comments and blanks ignored; absent file -> nothing
# is treated as secondary):
#
#     DATA_DIR / "secondary_session_names.txt"
def _load_secondary_session_markers() -> list[str]:
    path = DATA_DIR / "secondary_session_names.txt"
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line.lower())
    return out


_SECONDARY_SESSION_MARKERS = _load_secondary_session_markers()


def _is_secondary_session_name(name: str) -> bool:
    low = (name or "").lower()
    return any(m in low for m in _SECONDARY_SESSION_MARKERS)


def _classify_talk(title: str, start: str, end: str,
                   phase: str) -> tuple[str, str]:
    """Return (color_id, type_label) for a talk under the standard taxonomy,
    using both the slot length AND which program PHASE the talk is in
    ('school' for Mon–Wed, 'workshop' for Thu–Fri):

        Event        (rose)    — Opening / Closing / memorial, by title.
        Tutorial     (fuchsia) — substantive lectures during the SCHOOL phase
                                (the >= 30-min school lectures), plus the named
                                opening School talk.
        Invited      (indigo)  — the >= 30-min invited talks during the WORKSHOP
                                phase.
        Contributed  (sky)     — the short (< 30-min) talks in either phase.

    (Posters are classified separately, in the poster pass.) The School vs.
    Invited distinction follows the program's color caption — "invited talk for
    the school" vs "invited talk for the Workshop" — which maps onto the
    school/workshop phase of the program.
    """
    clean = _strip_emphasis(title or "")
    if _SCHOOL_TITLE_RE.search(clean):
        return "fuchsia", "Tutorial"
    if _BREAK_TITLE_RE.search(clean):
        return "rose", "Event"
    if _GENERAL_TITLE_RE.search(clean):
        return "rose", "Event"
    try:
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
        dur = (eh * 60 + em) - (sh * 60 + sm)
        if dur < 0:
            dur += 24 * 60
    except Exception:
        dur = 30
    if dur >= 30:
        # Substantive (invited-level) lecture: its label depends on the phase.
        return ("fuchsia", "Tutorial") if phase == "school" else ("indigo", "Invited")
    return "sky", "Contributed"


# -----------------------------------------------------------------------------
# Parsing the schedule (timed blocks) and the poster list.
# -----------------------------------------------------------------------------
def _segment(text: str) -> tuple[list[dict], list[list[str]]]:
    """Split the program text into (timed_blocks, poster_blocks).

    timed_blocks: each {day_dom, day_month, start, end, lines:[...]} where lines
                  are the content lines after the time (title, authors, affs).
    poster_blocks: each a list of lines [title, authors, affs...].
    """
    lines = text.split("\n")

    # Where does the poster list begin? Everything after it is posters.
    poster_start = next(
        (i for i, l in enumerate(lines)
         if l.strip().upper().startswith("LIST OF POSTERS")),
        len(lines))

    timed: list[dict] = []
    cur_dom = cur_month = None
    cur: dict | None = None
    for i in range(poster_start):
        s = lines[i].strip()
        if not s:
            continue
        dm = DAY_RE.match(s)
        if dm and dm.group("mon").lower() in MONTHS:
            cur_dom = int(dm.group("dom"))
            cur_month = MONTHS[dm.group("mon").lower()]
            continue
        tm = TIME_RE.match(s)
        if tm and cur_dom is not None:
            if cur:
                timed.append(cur)
            rest = _clean(tm.group("rest"))
            cur = {"day_dom": cur_dom, "day_month": cur_month,
                   "start": tm.group("s"), "end": tm.group("e"),
                   "lines": [rest] if rest else []}
        elif cur is not None:
            cur["lines"].append(s)
    if cur:
        timed.append(cur)

    # Posters: bullet-delimited blocks.
    posters: list[list[str]] = []
    pcur: list[str] | None = None
    for i in range(poster_start, len(lines)):
        s = lines[i].strip()
        if not s:
            continue
        if s.upper().startswith("LIST OF POSTERS"):
            continue
        if s.lower().startswith("poster size"):
            continue
        pm = POSTER_RE.match(s)
        if pm:
            if pcur:
                posters.append(pcur)
            pcur = [_clean(pm.group("title"))]
        elif pcur is not None:
            pcur.append(s)
    if pcur:
        posters.append(pcur)

    return timed, posters


def _block_to_talk(block_lines: list[str]) -> dict | None:
    """Turn a block's content lines into a parsed talk dict, or None if the
    block is a non-academic agenda item (lunch, break, poster session, …).

    Returns {title, authors, author_aliases, institutions, speaker, presenter,
             speaker_pos, first_author, last_author}.
    """
    if not block_lines:
        return None
    title_line = _strip_emphasis(block_lines[0])
    if not title_line:
        return None
    if any(rx.search(title_line) for rx in _NON_TALK_RE):
        return None

    rest = list(block_lines[1:])

    # Normalize the "General" housekeeping titles for clean display.
    if re.match(r"^opening\b", title_line, re.I):
        title_line = "Opening remarks"
    elif re.match(r"^closing\b", title_line, re.I):
        title_line = "Closing remarks"
    elif re.match(r"^(coffee break|break)\b", title_line, re.I):
        title_line = "Coffee Break"

    # A single-line block (title only) with no people = agenda item; skip unless
    # it clearly names a speaker glued on. Try the glued split on the title too.
    title, glued_aff = _split_glued_name_aff(title_line)
    # The title rarely contains the affiliation; only accept a glued split when
    # what's left looks like a real (longish) title. Otherwise keep whole line.
    if glued_aff and len(title.split()) < 3:
        title, glued_aff = title_line, ""

    author_line = ""
    aff_lines: list[str] = []
    if glued_aff:
        # Unusual; treat the rest as affiliations.
        aff_lines = rest
        author_line = ""  # speaker unknown from a glued-title case
    elif rest:
        # First non-empty rest line is normally the author list; the glued
        # name+affiliation single-author case is handled here too.
        first = rest[0]
        a_part, aff_part = _split_glued_name_aff(first)
        if aff_part:
            author_line = a_part
            aff_lines = ([aff_part] + rest[1:]) if len(rest) > 1 else [aff_part]
        else:
            if _looks_like_affiliation(first) and not _looks_like_authors(first):
                # No authors given, only an affiliation (rare).
                aff_lines = rest
            else:
                author_line = first
                aff_lines = rest[1:]

    institutions = _parse_institutions(aff_lines)
    authors, aliases, speaker_idx = _parse_authors(author_line, institutions)

    # The presenting author is the one underlined in the source (carried here as
    # speaker_idx). It is NOT always the first author — e.g. a senior author can
    # be listed first while a student/postdoc presents — so honor the underline
    # and fall back to author 0 only when the source left no marker.
    spk = speaker_idx if (speaker_idx is not None and authors) else 0
    speaker = authors[spk]["name"] if authors else ""
    # Byline convention the builder expects (see legacyTalkByline in
    # build_conference_app.py): it renders "first … last". So `last_author` must
    # be EMPTY when there is only one author — otherwise the same name is shown
    # twice ("Strasser…Strasser"). Only set last_author for 2+ authors.
    # first/last_author follow AUTHOR order (not the speaker), so the byline
    # still reads "first … last" with the speaker underlined wherever it sits.
    first_author = authors[0]["name"] if authors else ""
    last_author = authors[-1]["name"] if len(authors) > 1 else ""
    return {
        "title": title,
        "authors": authors,
        "author_aliases": aliases,
        "institutions": institutions,
        "speaker": speaker,
        "presenter": speaker,
        "speaker_pos": spk if authors else None,
        "first_author": first_author,
        "last_author": last_author,
    }


def parse_overview(text: str) -> dict[tuple[int, int, str], dict]:
    """Parse the 'Program at a Glance' overview grid into a lookup:

        {(month, dom, 'AM'|'PM'): {'names': [str, ...], 'phase': 'school'|'workshop'}}

    The page is a day-by-time grid. When flattened to visible text (the
    innerText the fetch script saves) it reads, in order: the "PROGRAM AT A
    GLANCE" heading; the day headers (each day name and its DD/MM on SEPARATE
    lines); then the MORNING, MID-DAY, AFTERNOON and EVENING rows. Within
    MORNING and AFTERNOON each session is introduced by a "School" or "Workshop"
    tag line, followed by its name on one or more lines (long names wrap), until
    the next tag or the next section. Cells appear in day-column order; empty
    cells (e.g. Friday afternoon, or Monday morning's tagless "Arrival") simply
    don't carry a School/Workshop session.

    Returns {} if the text doesn't look like the overview, so the caller falls
    back to generic "School N"/"Workshop N" titles.
    """
    if not text or "PROGRAM AT A GLANCE" not in text.upper():
        return {}
    lines = [l.strip() for l in text.split("\n")]

    # ---- day columns: name and DD/MM may be on one line OR two lines ----
    DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday")
    one_line = re.compile(
        r"^(%s)\s+(\d{1,2})/(\d{1,2})\b" % "|".join(DAY_NAMES), re.I)
    date_only = re.compile(r"^(\d{1,2})/(\d{1,2})$")
    days: list[tuple[int, int]] = []   # (month, dom) in column order
    i = 0
    while i < len(lines):
        l = lines[i]
        m = one_line.match(l)
        if m:
            days.append((int(m.group(3)), int(m.group(2))))
        elif l in DAY_NAMES and i + 1 < len(lines):
            dm = date_only.match(lines[i + 1])
            if dm:
                days.append((int(dm.group(2)), int(dm.group(1))))
                i += 1
        i += 1
    if not days:
        return {}

    SECTIONS = {"MORNING", "MID-DAY", "AFTERNOON", "EVENING"}
    TAGS = {"school", "workshop"}

    def _section_slice(name: str) -> list[str]:
        """Lines strictly between section `name` and the next section header."""
        try:
            a = next(k for k, l in enumerate(lines) if l.upper() == name)
        except StopIteration:
            return []
        b = len(lines)
        for k in range(a + 1, len(lines)):
            if lines[k].upper() in SECTIONS:
                b = k
                break
        return lines[a + 1:b]

    def _cells(name: str) -> list[dict]:
        """Tag-anchored session cells in a MORNING/AFTERNOON section. Each
        "School"/"Workshop" tag opens a cell; subsequent non-tag lines are its
        (possibly wrapped) name, joined into one string, until the next tag."""
        cells: list[dict] = []
        cur: dict | None = None
        for l in _section_slice(name):
            if not l:
                continue
            if l.lower() in TAGS:
                if cur:
                    cells.append(cur)
                cur = {"phase": l.lower(), "parts": []}
            elif cur is not None:
                cur["parts"].append(l)
            # lines before the first tag (e.g. "Arrival") are ignored
        if cur:
            cells.append(cur)
        # Collapse each cell's wrapped name parts. A blank-ish part naming a
        # secondary "(… session)" label is a SECOND name in the same cell;
        # detect that the page lists it as its own emphasized heading by keeping
        # parts whole only when they read as continuation fragments. We treat
        # every part as a separate name candidate, then re-join fragments that
        # are clearly wrapped (start lowercase / are connective) into the prior.
        for c in cells:
            names: list[str] = []
            for p in c["parts"]:
                p = _clean(p)
                if not p:
                    continue
                # A fragment that begins with a lowercase word or a connective
                # ("for", "and", "of"…) is a wrapped continuation of the prior
                # name; otherwise it's a new name in the same cell.
                first = p.split()[0].lower() if p.split() else ""
                if names and (first in ("for", "and", "of", "the", "&")
                              or p[:1].islower()):
                    names[-1] = f"{names[-1]} {p}"
                else:
                    names.append(p)
            c["names"] = names
            del c["parts"]
        return cells

    out: dict[tuple[int, int, str], dict] = {}
    # MORNING cells map to the days that HAVE a tagged morning session, in
    # column order. Monday's morning is the tagless "Arrival" (no cell), so the
    # first tagged cell belongs to the first day after Monday, etc. We align
    # cells to days by skipping days whose morning is untagged: simplest correct
    # rule for this program — Monday has no morning session, every other day
    # does — so morning cells map to days[1:].
    morning = _cells("MORNING")
    morning_days = days[1:] if len(morning) == len(days) - 1 else days
    for (month, dom), cell in zip(morning_days, morning):
        out[(month, dom, "AM")] = {"names": cell["names"],
                                    "phase": cell["phase"]}

    # AFTERNOON cells map to the days that have an afternoon session, in column
    # order. Friday ends at lunch (no afternoon), so cells map to days[:-1] when
    # there's exactly one fewer cell than days.
    aft = _cells("AFTERNOON")
    aft_days = days[:-1] if len(aft) == len(days) - 1 else days
    for (month, dom), cell in zip(aft_days, aft):
        out[(month, dom, "PM")] = {"names": cell["names"],
                                    "phase": cell["phase"]}

    return out


def build_conference_data() -> dict:
    text = _load_program_text()
    timed, posters = _segment(text)
    print(f"[process] parsed {len(timed)} timed blocks, "
          f"{len(posters)} poster entries.", flush=True)

    # Optional: the overview grid gives per-session NAMES and the School/Workshop
    # PHASE for each (day, morning/afternoon). If it's absent or unparseable we
    # fall back to generic "School N"/"Workshop N" titles and a date-based phase.
    overview: dict[tuple[int, int, str], dict] = {}
    overview_text = _load_overview_text()
    if overview_text:
        try:
            overview = parse_overview(overview_text)
            print(f"[process] overview: {len(overview)} named session slots.",
                  flush=True)
        except Exception as e:
            print(f"[process] overview parse failed ({e}); using generic "
                  "session titles.", flush=True)
    else:
        print("[process] no overview page; using generic session titles.",
              flush=True)

    sessions: list[dict] = []
    talks: list[dict] = []
    _talk_by_id: dict[str, dict] = {}   # tid -> talk dict, for late session_id fill

    # Affiliation source pools the builder/affiliation-map learn from.
    aff_full_lines: set[str] = set()
    inst_strings: set[str] = set()

    def _record_affs(institutions: list[dict]) -> None:
        for inst in institutions:
            nm = (inst.get("name") or "").strip()
            if nm:
                aff_full_lines.add(nm)
                inst_strings.add(nm)
        if institutions:
            inst_strings.add(
                "; ".join((i.get("name") or "").strip() for i in institutions))

    # ---- group timed blocks by day -> one session per day ----
    # Preserve first-seen day order.
    day_order: list[tuple[int, int]] = []
    day_blocks: dict[tuple[int, int], list[dict]] = {}
    for b in timed:
        key = (b["day_month"], b["day_dom"])
        if key not in day_blocks:
            day_blocks[key] = []
            day_order.append(key)
        day_blocks[key].append(b)

    weekday_name = {
        0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday",
        4: "Friday", 5: "Saturday", 6: "Sunday",
    }
    import datetime as _dt
    from collections import Counter

    # Real scheduled poster-session slots, captured as we walk the program, so
    # the poster catalog can be anchored to actual times instead of a guess.
    poster_slots: list[tuple[str, str]] = []   # (start_ts, end_ts)

    # PHASE + session NAMES come from the overview grid. The overview tags each
    # morning/afternoon as School or Workshop, but the actual School→Workshop
    # transition on Wednesday happens MID-MORNING (at the 11:30 session, after
    # the coffee break) — earlier than the lunch split. So we keep an explicit
    # cutoff timestamp for the phase, and a session run is broken whenever the
    # phase changes between consecutive talks (not only at meal dividers). This
    # yields, on Wednesday: a School morning run (09:00–11:00) and a Workshop
    # pre-lunch run (11:30–12:30), then the Workshop post-lunch run (13:30–).
    WORKSHOP_START_TS = _iso(1, 7, "11:30")   # 2026-07-01T11:30:00

    def _half_of(start_ts: str | None) -> str:
        return "AM" if (start_ts or "")[11:13] < "12" else "PM"

    def _phase_of(start_ts: str | None) -> str:
        return "workshop" if (start_ts or "") >= WORKSHOP_START_TS else "school"

    # Session NAME lookup. Normally a session's name is its overview half
    # (AM->morning name, PM->afternoon name). The one wrinkle is Wednesday's
    # pre-lunch Workshop run: it sits in the AM half by the clock but belongs to
    # the Workshop phase, so it should take the AFTERNOON ("…combs") name, not
    # the morning ("Unipolar devices…") one. We detect that mismatch — a slot
    # whose clock-half is AM but whose phase is Workshop (or PM but School) — and
    # read the OTHER half's names for it.
    def _slot_names(month: int, dom: int, half: str) -> list[str]:
        info = overview.get((month, dom, half))
        return list(info["names"]) if info else []

    def _names_for(start_ts: str | None) -> list[str]:
        ts = start_ts or ""
        if not ts:
            return []
        month, dom = int(ts[5:7]), int(ts[8:10])
        half = _half_of(ts)
        # Detect exactly that window — an AM block whose START TIME-OF-DAY is at
        # or after the 11:30 cutoff (i.e. a late-morning Workshop run, not a
        # normal ~09:00 morning start) — and read the PM slot's names. A Workshop
        # morning that begins at the normal 09:00 start (e.g. Thursday
        # "Applications") starts before 11:30 and keeps its own AM name.
        if half == "AM" and _phase_of(ts) == "workshop" \
                and ts[11:16] >= WORKSHOP_START_TS[11:16]:
            half = "PM"
        return _slot_names(month, dom, half)

    talk_seq = 0
    sess_seq = 0
    school_no = 0       # continuous count of School technical sessions
    workshop_no = 0     # continuous count of Workshop technical sessions
    for (month, dom) in day_order:
        blocks = day_blocks[(month, dom)]
        d = _dt.date(YEAR, month, dom)
        wd = weekday_name[d.weekday()]
        date_label = d.strftime(f"{dom:02d}-%b-{YEAR}")  # e.g. 29-Jun-2026

        # Walk the day's blocks in order. Contiguous runs of talks accumulate
        # into one TECHNICAL session, flushed when a divider (meal/break/admin/
        # poster-slot) is hit. Technical sessions are named by phase and
        # numbered continuously ("School 1" … "Workshop 1" …). Each divider
        # becomes its OWN session titled by what it is ("Lunch", …) with no talks.
        cur_talk_ids: list[str] = []
        cur_colors: list[str] = []
        cur_start_ts: str | None = None
        cur_end_ts: str | None = None

        def _flush_tech() -> None:
            """Emit the accumulated technical-talk run as one named session."""
            nonlocal cur_talk_ids, cur_colors, cur_start_ts, cur_end_ts
            nonlocal sess_seq, school_no, workshop_no
            if not cur_talk_ids:
                cur_start_ts = cur_end_ts = None
                return
            sess_seq += 1
            sid = f"S{sess_seq:03d}"
            for tid in cur_talk_ids:
                _talk_by_id[tid]["session_id"] = sid
            phase = _phase_of(cur_start_ts)
            if phase == "school":
                school_no += 1
                phase_label = f"School {school_no}"
                phase_word = "School"
                color = "fuchsia"
            else:
                workshop_no += 1
                phase_label = f"Workshop {workshop_no}"
                phase_word = "Workshop"
                color = "blue"

            # Pick the session's display NAME from the overview, keyed by phase
            # (so a pre-lunch Workshop run gets the afternoon name, not the
            # morning name). A slot may list several names for one session — a
            # primary topic plus a secondary "(… session)" label. The secondary
            # label is appended in parentheses to the primary name, so it reads
            # "<primary topic> (<secondary> session)" — one ordinary School
            # session, not a separate one.
            names = _names_for(cur_start_ts)
            name = ""
            if names:
                secondary = next((n for n in names
                                  if _is_secondary_session_name(n)), "")
                primary = next((n for n in names
                                if not _is_secondary_session_name(n)), "")
                if primary and secondary:
                    name = f"{primary} ({secondary})"
                else:
                    name = primary or secondary or names[0]

            # Title reads "School: <name>" / "Workshop: <name>". The continuous
            # "School N"/"Workshop N" counter rides along in `topic`. With no
            # overview name, fall back to just the phase label.
            if name:
                title = f"{phase_word}: {name}"
                topic = phase_label
            else:
                title = phase_label
                topic = ""
            sessions.append({
                "id": sid,
                "title": title,
                "type": phase_word,
                "topic": topic,
                "date": date_label,
                "location": "",
                "presider": "",
                "presider_aff": "",
                "details": "",
                "start_ts": cur_start_ts,
                "end_ts": cur_end_ts,
                "color": color,
                "talk_ids": list(cur_talk_ids),
            })
            cur_talk_ids = []
            cur_colors = []
            cur_start_ts = cur_end_ts = None

        def _emit_divider(label: str, start_ts: str, end_ts: str) -> None:
            """Emit a meal/break/admin block as its own (talk-less) session,
            titled by the block label only (not numbered)."""
            nonlocal sess_seq
            sess_seq += 1
            sessions.append({
                "id": f"S{sess_seq:03d}",
                "title": label,
                "type": "General",
                "topic": "",
                "date": date_label,
                "location": "",
                "presider": "",
                "presider_aff": "",
                "details": "",
                "start_ts": start_ts,
                "end_ts": end_ts,
                "color": "rose",
                "talk_ids": [],
            })

        for b in blocks:
            start_ts = _iso(dom, month, b["start"])
            end_ts = _iso(dom, month, b["end"])
            first_line = b["lines"][0] if b["lines"] else ""
            kind = _block_kind(first_line)

            if kind in ("poster_slot", "divider"):
                # End any open technical run, THEN emit this divider as its own
                # session so the day reads in order: …#1, Coffee Break, #2, …
                _flush_tech()
                if kind == "poster_slot":
                    # Record the real slot time for the poster CATALOG session
                    # (built below); don't also emit an empty "Poster Session"
                    # divider here, which would duplicate the catalog.
                    poster_slots.append((start_ts, end_ts))
                    continue
                _emit_divider(_divider_label(first_line), start_ts, end_ts)
                continue

            parsed = _block_to_talk(b["lines"])
            if parsed is None:
                # Classifier said talk but parser disagreed; close the run so we
                # never fold a garbage block into a technical session.
                _flush_tech()
                continue

            # Break the run when the School↔Workshop phase changes between
            # consecutive talks, so the mid-morning Wednesday transition (at
            # 11:30) starts a fresh session even though no meal divides them.
            if cur_talk_ids and _phase_of(start_ts) != _phase_of(cur_start_ts):
                _flush_tech()

            talk_seq += 1
            color, _label = _classify_talk(
                parsed["title"], b["start"], b["end"], _phase_of(start_ts))
            tid = f"T{talk_seq:03d}"
            cur_talk_ids.append(tid)
            cur_colors.append(color)
            if cur_start_ts is None or start_ts < cur_start_ts:
                cur_start_ts = start_ts
            if cur_end_ts is None or end_ts > cur_end_ts:
                cur_end_ts = end_ts
            _record_affs(parsed["institutions"])
            t = {
                "id": tid,
                "session_id": "",   # filled in by _flush_tech
                "title": parsed["title"],
                "number": "",
                "start_ts": start_ts,
                "end_ts": end_ts,
                "presenter": parsed["presenter"],
                "speaker": parsed["speaker"],
                "speaker_pos": parsed["speaker_pos"],
                "authors": parsed["authors"],
                "author_aliases": parsed["author_aliases"],
                "institutions": parsed["institutions"],
                "institutions_may_dedup": False,
                "abstract": "",
                "status": "Sessioned",
                "withdrawn": False,
                "first_author": parsed["first_author"],
                "last_author": parsed["last_author"],
                "color": color,
                "location": "",
            }
            talks.append(t)
            _talk_by_id[tid] = t

        _flush_tech()   # end-of-day: flush any trailing technical run

    # Suffix-number any session NAME shared by more than one technical session
    # (e.g. Wednesday's split Workshop run -> "Quantum cascade lasers dynamics
    # and combs 1" / "… 2"). Sessions keep first-seen order; titles that are
    # unique are left untouched.
    from collections import Counter as _Counter
    _tech = [s for s in sessions if s["type"] in ("School", "Workshop")]
    _title_counts = _Counter(s["title"] for s in _tech)
    _seen: dict[str, int] = {}
    for s in _tech:
        if _title_counts[s["title"]] > 1:
            _seen[s["title"]] = _seen.get(s["title"], 0) + 1
            s["title"] = f"{s['title']} {_seen[s['title']]}"

    # ---- posters: split the catalog into TWO sessions, one per scheduled
    #      poster slot. The program lists all posters together without saying
    #      which evening each is shown, so we split the list in half: the first
    #      half goes to the first slot, the second half to the second. (If only
    #      one slot was found, everything goes there.) ----
    if posters:
        def _slot_label(ts: tuple[str, str]) -> str:
            s, e = ts
            dd = _dt.date.fromisoformat(s[:10])
            return f"{weekday_name[dd.weekday()]} {s[11:16]}–{e[11:16]}"

        # Establish the slot times (start, end), in chronological order.
        if poster_slots:
            slots = sorted(poster_slots)
        else:
            p_dom, p_month = ((day_order[-1][1], day_order[-1][0])
                              if day_order else (3, 7))
            slots = [(_iso(p_dom, p_month, "17:00"),
                      _iso(p_dom, p_month, "19:00"))]

        # Parse all posters once (skipping any that don't yield a title).
        parsed_posters = []
        for idx, pblock in enumerate(posters, 1):
            parsed = _block_to_talk(pblock)
            if parsed and parsed["title"]:
                parsed_posters.append((idx, parsed))

        # Partition into one group per slot. With two slots we split in half
        # (first half -> slot 1, remainder -> slot 2); generalizes to N slots.
        n_slots = len(slots)
        per = -(-len(parsed_posters) // n_slots)   # ceil division
        groups = [parsed_posters[i:i + per]
                  for i in range(0, len(parsed_posters), per)] or [[]]
        # Guard: never produce more groups than slots (rounding safety).
        while len(groups) > n_slots:
            groups[-2].extend(groups[-1])
            groups.pop()

        for gi, group in enumerate(groups):
            if not group:
                continue
            base_start, base_end = slots[gi] if gi < n_slots else slots[-1]
            sess_id = f"POSTERS{gi + 1}"
            tids: list[str] = []
            for idx, parsed in group:
                tid = f"P{idx:03d}"
                tids.append(tid)
                _record_affs(parsed["institutions"])
                talks.append({
                    "id": tid,
                    "session_id": sess_id,
                    "title": parsed["title"],
                    "number": f"P{idx}",
                    "start_ts": base_start,
                    "end_ts": base_end,
                    "presenter": parsed["presenter"],
                    "speaker": parsed["speaker"],
                    "speaker_pos": parsed["speaker_pos"],
                    "authors": parsed["authors"],
                    "author_aliases": parsed["author_aliases"],
                    "institutions": parsed["institutions"],
                    "institutions_may_dedup": False,
                    "abstract": "",
                    "status": "Sessioned",
                    "withdrawn": False,
                    "first_author": parsed["first_author"],
                    "last_author": parsed["last_author"],
                    "color": "teal",
                    "location": "",
                })
            label = (_slot_label(slots[gi]) if gi < n_slots else "")
            title = (f"Poster Session {gi + 1}" if len(groups) > 1
                     else "Poster Session")
            sessions.append({
                "id": sess_id,
                "title": title,
                "type": "Posters",
                "topic": label,
                "date": _dt.date.fromisoformat(base_start[:10]).strftime(
                    f"%d-%b-{YEAR}"),
                "location": "",
                "presider": "",
                "presider_aff": "",
                "details": (f"{label}. " if label else "")
                           + "Poster size is A0 vertical. "
                           "Clips for hanging are provided on site.",
                "start_ts": base_start,
                "end_ts": base_end,
                "color": "teal",
                "talk_ids": tids,
            })

    # Pool every affiliation source into one flat, de-duplicated, sorted list for
    # the builder's affiliation map (this program has no presiders). Full-address
    # lines are kept whole; the institution strings may be ';'-joined lists, so
    # split them here at the source.
    affiliation_pool: set[str] = set(aff_full_lines)
    for _v in inst_strings:
        for _piece in _v.split(";"):
            _p = _piece.strip()
            if _p:
                affiliation_pool.add(_p)

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
    # IQCLSW carries no session tags: the legacy type ("School"/"Workshop"/
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
    print(f"[process] wrote {JSON_OUT.name}: {n_s} sessions, {n_t} talks, "
          f"{n_auth} author entries, "
          f"{len(data['affiliation_sources'])} "
          f"affiliation strings.", flush=True)


if __name__ == "__main__":
    main()
