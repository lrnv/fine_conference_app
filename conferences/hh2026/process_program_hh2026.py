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

"""process_program_hh2026.py — PROCESS ONLY.

The "processor" half of the conference pipeline. It reads the single
source of record — the Final Program PDF in data/ — and emits the clean,
source-agnostic conference_data.json that build_conference_app.py consumes.
No network, no browser; it parses the PDF entirely offline.

Why a geometry / font driven parser
------------------------------------
The program is a single-track schedule whose structure is encoded
almost entirely in FONT and COLUMN position, not in punctuation. Every page is
one narrow column laid out like this (described as FORMAT only — no real
program text appears in this source per the repo's no-hardcoded-content rule):

  * Day header  — Helvetica-Bold ~16pt, centred:        "<Weekday>, <D> <Month>"
  * Session banner — Helvetica-Bold ~10-11pt, centred, may wrap onto a 2nd line.
        Kinds (matched by leading FORMAT words, not by content):
          "Plenary Speaker <n>", "Rising Star Speaker <n>",
          "Invited Speaker <n>", "Session <n> - <topic>",
          "Workshop <n>: <topic>" (Sunday short courses),
          "... Industry Session ...", and poster-section pointers.
  * "Session Chair(s): <Name>, <Aff> [and <Name>, <Aff>]" — Helvetica regular,
        may wrap; attaches a presider to the most recent session.
  * A timed block — begins at the left margin (x0~36) with "HH:MM" (optionally a
        trailing "-" whose end time sits on the following bare "HH:MM" line),
        then in the content column (x0~85):
          - TITLE   : Helvetica-Bold, ALL-CAPS, one or more lines.
          - AUTHORS : Helvetica regular, one or more lines; affiliation markers
                      are bare digits glued to surnames ("Surname1,2").
          - AFFILS  : Helvetica-OBLIQUE; either a single unnumbered institution
                      or a "<n>Institution, COUNTRY" numbered list.
        A timed block whose first content line is NOT all-caps (e.g. a meal,
        break, award announcement, reception) is a non-technical EVENT.
  * Poster pages — a "Poster Presentations - Session <n>" header (with its own
        date + time range), category sub-headers (Helvetica-Bold ~11pt), then
        one block per poster starting with an alphanumeric code (e.g.
        "<LETTERS>-<NN>") in place of a time; title/authors/affils as for talks.

The parser therefore reconstructs each line from pdfplumber's CHARACTER stream
(the display font uses wide letter-spacing, so word-level extraction is
unreliable), tags every line by font (bold / oblique / size) and left position,
and runs a small state machine over the lines in reading order. The seven
standard session/talk types are assigned from the banner kind, never invented.

Output (next to this script):
    conference_data.json
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
INPUT_PDF = DATA_DIR / "HiltonHead2026_Program.pdf"
OUTPUT_JSON = SCRIPT_DIR / "conference_data.json"

# Display name shown as the app title and on the Sessions/Talks headings. This
# is the single obvious top-level constant the user can review/edit; everything
# else of substance is extracted at runtime from the PDF in data/.
CONFERENCE_NAME = "Hilton Head 2026"
YEAR = 2026

# Optional curator credit (see CONFERENCE_JSON.md). Leave name empty / set to
# None to show only the app-author attribution.
CURATOR = None


def log(msg: str) -> None:
    print(msg, flush=True)


def _bootstrap_pdfplumber() -> None:
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        print("[setup] Installing pdfplumber…", flush=True)
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet",
             "pdfplumber>=0.10"])


# =============================================================================
# Type / color registries (baked into the JSON; the app reads these directly).
# The seven standard shared types; a conference surfaces only the ones it uses.
# =============================================================================
COLOR_PALETTE = {
    "blue":    {"fg": "#2563eb", "bg_light": "#e8efff", "bg_dark": "#1a233d"},
    "orange":  {"fg": "#ea580c", "bg_light": "#ffedd5", "bg_dark": "#3b1d0a"},
    "fuchsia": {"fg": "#c026d3", "bg_light": "#fae8ff", "bg_dark": "#3a0f3f"},
    "teal":    {"fg": "#0d9488", "bg_light": "#d6f3ef", "bg_dark": "#102b27"},
    "rose":    {"fg": "#e11d48", "bg_light": "#ffe1e8", "bg_dark": "#38161f"},
    "indigo":  {"fg": "#4f46e5", "bg_light": "#e6e4ff", "bg_dark": "#1d1a3d"},
    "sky":     {"fg": "#0284c7", "bg_light": "#e0f2fe", "bg_dark": "#0c2a3d"},
}


def _with_colors(entries: list[dict]) -> list[dict]:
    out = []
    for e in entries:
        pal = COLOR_PALETTE.get(e["id"])
        out.append({**e, **pal} if pal else dict(e))
    return out


SESSION_TYPE_REGISTRY = _with_colors([
    {"id": "blue",    "label": "Technical"},
    {"id": "orange",  "label": "Plenary"},
    {"id": "fuchsia", "label": "Tutorial"},
    {"id": "teal",    "label": "Poster"},
    {"id": "rose",    "label": "Event"},
])
TALK_TYPE_REGISTRY = _with_colors([
    {"id": "orange",  "label": "Plenary"},
    {"id": "indigo",  "label": "Invited"},
    {"id": "sky",     "label": "Contributed"},
    {"id": "fuchsia", "label": "Tutorial"},
    {"id": "teal",    "label": "Poster"},
    {"id": "rose",    "label": "Event"},
])

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday")


# =============================================================================
# Line model: reconstruct lines from the character stream, tagged by font.
# =============================================================================
LEFT_MARGIN_X = 55.0     # left token column (time / poster code) starts ~36.
CONTENT_X = 75.0         # content column (title / authors / affils) starts ~85.


class Line:
    """One visual line, reconstructed from pdfplumber chars.

    Exposes the full text, the leftmost x, and the font of the CONTENT portion
    (the part in the right-hand column), so a "HH:MM <Bold Title>" row reports
    the title's font even though the time prefix shares the row."""

    __slots__ = ("page", "top", "chars", "text", "x0", "size",
                 "bold", "oblique", "content_chars", "content_text",
                 "content_bold", "content_oblique", "content_size")

    def __init__(self, page: int, chars: list[dict]):
        self.page = page
        chars = sorted(chars, key=lambda c: c["x0"])
        self.chars = chars
        self.top = min(c["top"] for c in chars)
        self.x0 = min(c["x0"] for c in chars)
        self.text = _join_chars(chars)
        self.size, self.bold, self.oblique = _font_of(chars)
        # Content portion: chars sitting in the right column. For a left-margin
        # row this drops the time / poster-code prefix so the font reflects the
        # title/author/affil; for an already-indented line it's the whole line.
        cc = [c for c in chars if c["x0"] >= CONTENT_X]
        if not cc:
            cc = chars
        self.content_chars = cc
        self.content_text = _join_chars(cc)
        self.content_size, self.content_bold, self.content_oblique = _font_of(cc)


def _join_chars(chars: list[dict]) -> str:
    """Reconstruct text from chars in x order. Space characters are present in
    the stream, so a plain join restores the words; we just normalise runs of
    whitespace and strip."""
    s = "".join(c["text"] for c in chars)
    return re.sub(r"[ \t ]+", " ", s).strip()


def _font_of(chars: list[dict]) -> tuple[float, bool, bool]:
    """(median-ish size, is_bold, is_oblique) for a run of chars, judged by the
    dominant font among the alphabetic glyphs."""
    alpha = [c for c in chars if c["text"].strip()]
    if not alpha:
        return 0.0, False, False
    sizes = sorted(c.get("size", 0.0) for c in alpha)
    size = sizes[len(sizes) // 2]
    nbold = sum(1 for c in alpha if "Bold" in c.get("fontname", ""))
    nobl = sum(1 for c in alpha
               if ("Oblique" in c.get("fontname", "")
                   or "Italic" in c.get("fontname", "")))
    n = len(alpha)
    return size, nbold * 2 >= n, nobl * 2 >= n


def _extract_lines(pdf) -> list[Line]:
    """All visual lines across all pages, in reading order, with footers and
    blank/page-number lines dropped."""
    lines: list[Line] = []
    for pno, page in enumerate(pdf.pages, 1):
        chars = page.chars
        if not chars:
            continue
        # Cluster chars into rows by their `top` baseline.
        rows: list[list[dict]] = []
        tops: list[float] = []
        for c in sorted(chars, key=lambda c: (round(c["top"], 1), c["x0"])):
            placed = False
            for i, t in enumerate(tops):
                if abs(c["top"] - t) <= 2.5:
                    rows[i].append(c)
                    tops[i] = min(t, c["top"])
                    placed = True
                    break
            if not placed:
                rows.append([c])
                tops.append(c["top"])
        for r in rows:
            ln = Line(pno, r)
            if not ln.text:
                continue
            if ln.top > 558:                      # page-footer zone
                continue
            if re.fullmatch(r"\d{1,3}", ln.text):  # bare page number
                continue
            lines.append(ln)
    return lines


# =============================================================================
# Line classification helpers.
# =============================================================================
_DAY_RE = re.compile(
    r"^(?P<wd>%s),?\s+(?P<dom>\d{1,2})\s+(?P<mon>[A-Za-z]+)$"
    % "|".join(w.capitalize() for w in WEEKDAYS), re.I)
_POSTER_HDR_RE = re.compile(r"^Poster Presentations\b", re.I)
_TIME_ROW_RE = re.compile(r"^(\d{1,2}:\d{2})\s*(-)?\s*(.*)$")
_BARE_TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*-?\s*$")
_POSTER_CODE_RE = re.compile(r"^([A-Z]{1,4}-\d+)\b[\.\s]*(.*)$")
_CHAIR_RE = re.compile(r"^Session Chairs?\b\s*:?\s*(.*)$", re.I)
# Poster-session date/time header, e.g. "<Weekday>, <D> <Month>  HH:MM – HH:MM".
_POSTER_DATETIME_RE = re.compile(
    r"^(%s),?\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{1,2}:\d{2})\s*[–—\-]\s*(\d{1,2}:\d{2})"
    % "|".join(w.capitalize() for w in WEEKDAYS), re.I)


def _is_header_class(ln: Line) -> bool:
    """A structural header line: bold and >= 10pt (day header, session banner,
    or poster category). Judged on the CONTENT column only — the left-margin
    time digits render a hair larger (~10pt) than the 9.1pt body and would
    otherwise lift a "HH:MM <event>" row to header size. Banners (no time
    column) are 10.6-16pt; talk titles/authors/affils are <= 9.1pt; the
    page-number footer is regular weight."""
    return ln.content_bold and ln.content_size >= 10.0


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace(" ", " ")).strip()


def _clean_paragraphs(s: str) -> str:
    """Like _clean, but preserves paragraph breaks: collapse whitespace WITHIN
    each paragraph (runs separated by one or more blank lines) and rejoin the
    non-empty paragraphs with a single blank line."""
    paras = [_clean(p) for p in re.split(r"\n{2,}", s or "")]
    return "\n\n".join(p for p in paras if p)


def _all_caps(s: str) -> bool:
    """True if the alphabetic content is essentially all upper-case — the signal
    that a timed block is a technical talk title rather than a plain event."""
    letters = [c for c in s if c.isalpha()]
    if len(letters) < 3:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) >= 0.8



# =============================================================================
# Author / institution parsing.
# =============================================================================
def _split_numbered_insts(s: str) -> list[tuple[int, str]]:
    """Split a numbered affiliation string into [(n, body), ...].

    The program format glues the marker to the institution with no period:
    "<n>Institution, COUNTRY, <n>Institution, COUNTRY, and <n>Institution,
    COUNTRY". A marker is a 1-2 digit number that sits at the string start, or
    right after a ',' (optionally followed by 'and'), or after a bare 'and',
    AND is immediately followed by a capital letter (institution names start
    upper-case). The body of each institution keeps its own internal commas and
    trailing country."""
    anchors: list[tuple[int, int]] = []
    for m in re.finditer(r"(?:^|,\s*(?:and\s+)?|\s+and\s+)(\d{1,2})(?=[A-Z])", s):
        anchors.append((m.start(1), int(m.group(1))))
    if not anchors:
        return []
    out: list[tuple[int, str]] = []
    for i, (pos, num) in enumerate(anchors):
        end = anchors[i + 1][0] if i + 1 < len(anchors) else len(s)
        body = s[pos:end]
        body = re.sub(r"^\d{1,2}", "", body, count=1)          # strip the marker
        body = body.strip().strip(",").strip()
        body = re.sub(r"[\s,]+and$", "", body).strip().strip(",").strip()
        if body:
            out.append((num, _clean(body)))
    return out


def _parse_institutions(aff_lines: list[str]) -> list[dict]:
    """Parse oblique affiliation line(s) into [{n, name, alt_names}].

    Numbered form wins if markers are present; otherwise the whole thing is one
    unnumbered institution (n=1). Multi-line unnumbered affiliations (a single
    institution wrapped across rows) are joined with a space."""
    joined = _clean(" ".join(l.strip() for l in aff_lines if l.strip()))
    if not joined:
        return []
    numbered = _split_numbered_insts(joined)
    if numbered:
        return [{"n": n, "name": name, "alt_names": []} for n, name in numbered]
    return [{"n": 1, "name": joined, "alt_names": []}]


def _parse_author_token(tok: str) -> tuple[str, list[int]]:
    """One author token -> (name, [inst numbers]). Strips a leading 'and ' and a
    trailing run of affiliation-marker digits glued to the surname
    ('Surname1,2' -> insts [1,2])."""
    tok = _clean(tok)
    tok = re.sub(r"^and\s+", "", tok, flags=re.I)
    insts: list[int] = []
    m = re.search(r"(?<=[A-Za-z\.\)À-ſ])([\d,]+)$", tok)
    if m and any(ch.isdigit() for ch in m.group(1)):
        insts = [int(d) for d in re.findall(r"\d+", m.group(1))]
        tok = tok[:m.start()].strip()
    return tok.strip().rstrip(",").strip(), insts


def _parse_authors(author_text: str,
                   institutions: list[dict]) -> tuple[list[dict], list[str]]:
    """Parse an author line into (authors, aliases). Authors are comma-separated
    (the internal comma of a '1,2' marker is not a separator because it is not
    followed by whitespace). When there is exactly one institution and no author
    carried a marker, every author is attributed to inst 1. References to a
    non-existent institution number are dropped so the JSON stays consistent."""
    valid = {i["n"] for i in institutions}
    authors: list[dict] = []
    # Authors are separated by commas, OR by a bare "and"/"&" (a two-author
    # byline "X and Y", or the Oxford-comma "…, and Z"). The comma split
    # requires a FOLLOWING space so an affiliation marker's internal comma
    # ("Surname1,2") is never split; " and "/" & " need surrounding whitespace
    # so a name like "Anand" is left intact.
    for tok in re.split(r",(?=\s)|\s+and\s+|\s+&\s+", author_text):
        tok = tok.strip()
        if not tok:
            continue
        name, insts = _parse_author_token(tok)
        if name:
            authors.append({"name": name, "insts": insts})

    if not any(a["insts"] for a in authors) and len(institutions) == 1:
        for a in authors:
            a["insts"] = [1]
    for a in authors:
        seen: set[int] = set()
        a["insts"] = [n for n in a["insts"]
                      if n in valid and not (n in seen or seen.add(n))]
    return authors, [a["name"] for a in authors]


def _split_body_fonts(body_lines: list[Line]) -> tuple[str, str]:
    """Split a block's byline region into (author_text, affiliation_text) by
    CHARACTER font: regular weight is author text, oblique/italic is
    affiliation. Most talks keep the two on separate lines, but the industry
    session puts them on one line ('<Speaker, regular>, <Company, oblique>'),
    so a line-level oblique test mis-files them — char-level is robust."""
    author_parts: list[str] = []
    aff_parts: list[str] = []
    for l in body_lines:
        obl: list[dict] = []
        reg: list[dict] = []
        for c in l.content_chars:
            fn = c.get("fontname", "")
            (obl if ("Oblique" in fn or "Italic" in fn) else reg).append(c)
        obl_txt = _join_chars(obl)
        reg_txt = _join_chars(reg)
        # Oblique is used for THREE things: affiliation text, the tiny
        # superscript affiliation MARKERS glued to author surnames, and the
        # italic connective "and" / spaces in an author list. Only treat the
        # oblique run as a real affiliation when it carries alphabetic content
        # beyond that connective; otherwise the line is an author line and is
        # kept whole (markers in place, spacing preserved) so the comma-split
        # works. When a line mixes real regular names AND real oblique text it
        # is an industry byline ("<Speaker>, <Company>") and is split.
        # Alphabetic content of each font run, with the connective "and" removed
        # (it is italic in author lists and often glued to a marker, e.g.
        # "45and"). What remains in the oblique run, if anything, is real
        # affiliation text.
        obl_real = re.sub(r"[^a-z]", "", obl_txt.lower()).replace("and", "")
        reg_real = re.sub(r"[^A-Za-z]", "", reg_txt)
        if not obl_real:
            author_parts.append(_join_chars(l.content_chars))
        elif not reg_real:
            aff_parts.append(obl_txt)
        else:
            author_parts.append(reg_txt)
            aff_parts.append(obl_txt)
    # Join lines with a space so the last author on one line is not glued to the
    # first on the next (a marker-suffixed surname must not abut the next
    # author's given name).
    return _clean(" ".join(author_parts)), _clean(" ".join(aff_parts))


def _block_has_body(lines: list[Line]) -> bool:
    """True once a block has moved past its (bold) title into body lines — i.e.
    it contains an author line (regular weight) or an affiliation (oblique).
    Used to decide a following ALL-CAPS title begins a new talk, not a title
    continuation."""
    return any(l.content_oblique or not l.content_bold for l in lines)


def _talk_payload_from_lines(content_lines: list[Line],
                             fallback_title: str) -> dict | None:
    """Turn a timed/poster block's content lines (already time/code-stripped)
    into a parsed talk dict, or None if it carries no usable content.

    Returns {title, authors, author_aliases, institutions, speaker, presenter,
             speaker_pos, first_author, last_author, is_event, details}."""
    if not content_lines:
        return None

    first = content_lines[0]

    # --- EVENT: a bold, mixed-case lead line (meal, break, ceremony, award
    # announcement, …). Talk titles are bold ALL-CAPS; a bold line that is not
    # all-caps is therefore an event label, not a title.
    #
    # A ceremony's body is often a list of officials as "<Role>:" (bold) lines
    # each followed by a "Name, Affiliation" line — e.g. the opening Welcome and
    # the closing Award Ceremony. Those people PRESIDE over the event, so they
    # become its presider(s) rather than free-text details. Any line that isn't
    # part of such a role/name pair stays as the details blurb. ---
    if (first.content_bold and not first.content_oblique
            and not _all_caps(first.content_text)):
        body = content_lines[1:]
        pres_names: list[str] = []
        pres_affs: list[str] = []
        det: list[str] = []
        i = 0
        while i < len(body):
            txt = body[i].content_text.strip()
            if (body[i].content_bold and txt.endswith(":")
                    and i + 1 < len(body) and not body[i + 1].content_bold):
                name_line = body[i + 1].content_text.strip()
                if "," in name_line:
                    # "Name, Affiliation" on one line.
                    nm, _, aff = name_line.partition(",")
                    consumed = 2
                else:
                    # Name alone; the affiliation (if any) is the next regular,
                    # non-role line — recognised by its comma ("Univ…, USA").
                    nm, aff, consumed = name_line, "", 2
                    if i + 2 < len(body) and not body[i + 2].content_bold:
                        cand = body[i + 2].content_text.strip()
                        if not cand.endswith(":") and "," in cand:
                            aff, consumed = cand, 3
                if nm.strip():
                    pres_names.append(_clean(nm))
                    pres_affs.append(_clean(aff))
                i += consumed
                continue
            det.append(txt)
            i += 1
        return {"is_event": True, "title": first.content_text.strip(),
                "details": _clean(" ".join(det)),
                "presider": "; ".join(pres_names),
                "presider_aff": ";".join(pres_affs)}

    # --- TALK: the leading run of bold ALL-CAPS lines is the title. Anything
    # after it is the byline — note a single featured speaker's NAME is also
    # rendered bold (but mixed-case), so it falls through to the author lines
    # rather than being mistaken for more title. Oblique lines are affiliations.
    i = 0
    title_parts: list[str] = []
    while (i < len(content_lines) and content_lines[i].content_bold
           and not content_lines[i].content_oblique
           and _all_caps(content_lines[i].content_text)):
        title_parts.append(content_lines[i].content_text)
        i += 1
    author_text, aff_text = _split_body_fonts(content_lines[i:])

    title = _clean(" ".join(title_parts))

    # No ALL-CAPS title and no affiliations: a plain agenda line that simply
    # wasn't bold-flagged like the other events (e.g. an affinity-group
    # breakfast). It is an event, not a title-less talk.
    if not title and not aff_text.strip():
        details = _clean(" ".join(l.content_text for l in content_lines[1:]))
        return {"is_event": True, "title": first.content_text.strip(),
                "details": details}

    institutions = _parse_institutions([aff_text]) if aff_text.strip() else []
    authors, aliases = _parse_authors(author_text, institutions)

    if not title:
        # Title-less talk (e.g. a Sunday workshop's organizer block); borrow the
        # parent session's topic.
        title = fallback_title
        if not title:
            return None

    for inst in institutions:
        inst["alt_names"] = []
    speaker = authors[0]["name"] if authors else ""
    first_author = authors[0]["name"] if authors else ""
    last_author = authors[-1]["name"] if len(authors) > 1 else ""
    return {
        "is_event": False,
        "title": title,
        "authors": authors,
        "author_aliases": aliases,
        "institutions": institutions,
        "speaker": speaker,
        "presenter": speaker,
        "speaker_pos": 0 if authors else None,
        "first_author": first_author,
        "last_author": last_author,
    }


# =============================================================================
# Banner classification -> session + talk colors.
# =============================================================================
def _banner_kind(title: str) -> dict:
    """Map a banner's leading FORMAT words onto the standard taxonomy. Returns
    {format, session_color, talk_color, short}. `short` strips a leading
    "Workshop N:" / "Session N -" so a title-less child talk can borrow it."""
    t = _clean(title)
    low = t.lower()
    if low.startswith("plenary speaker"):
        return {"format": "Plenary", "session_color": "orange",
                "talk_color": "orange", "short": t}
    if low.startswith("rising star"):
        return {"format": "Rising Star Speaker", "session_color": "blue",
                "talk_color": "indigo", "short": t}
    if low.startswith("invited speaker"):
        return {"format": "Invited Speaker", "session_color": "blue",
                "talk_color": "indigo", "short": t}
    if "industry session" in low:
        return {"format": "Industry Session", "session_color": "blue",
                "talk_color": "indigo", "short": t}
    if re.match(r"^workshop\s+\d", low):
        short = re.sub(r"^workshop\s+\d+\s*[:\-–]?\s*", "", t, flags=re.I)
        return {"format": "Sunday Workshop", "session_color": "fuchsia",
                "talk_color": "fuchsia", "short": short or t}
    if re.match(r"^session\s+\d", low):
        short = re.sub(r"^session\s+\d+\s*[\-–:]?\s*", "", t, flags=re.I)
        return {"format": "Technical Session", "session_color": "blue",
                "talk_color": "sky", "short": short or t}
    return {"format": "Session", "session_color": "blue",
            "talk_color": "sky", "short": t}


def _is_poster_pointer(title: str) -> bool:
    """A day-page banner that merely points at a poster section ("Poster Session
    N", "Poster Session N and Reception"). The real catalog comes from the
    poster pages, so these pointers (and their one folded subtitle line) are
    skipped."""
    return bool(re.match(r"^poster session\b", _clean(title), re.I))


# =============================================================================
# The state machine: walk the lines and build sessions + talks.
# =============================================================================
def _iso(dom: int, month: int, hhmm: str) -> str:
    h, m = hhmm.split(":")
    return f"{YEAR:04d}-{month:02d}-{dom:02d}T{int(h):02d}:{int(m):02d}:00"


# A "<Weekday> - HH:MM - HH:MM - <Room>" schedule line in the Special Events
# section (format only). The trailing field is the room/location.
_SE_WHEN_RE = re.compile(
    r"^(?:%s)\b.*?\d{1,2}:\d{2}.*?[-–—]\s*(?P<room>[^-–—]+)$"
    % "|".join(w.capitalize() for w in WEEKDAYS), re.I)


# Venue keywords that mark a trailing "(...)" in an event title as a ROOM (and
# not a parenthetical qualifier like "(on your own)"). Format only.
_ROOM_KW = re.compile(
    r"(?i)\b(room|ballroom|hall|floor|pavilion|lobby|lawn|suite|cent(?:er|re)|"
    r"patio|terrace|deck|pool|beach|lounge|garden|foyer|theat(?:er|re)|"
    r"auditorium|plaza|veranda|courtyard|atrium)\b")


def _split_event_location(title: str) -> tuple[str, str]:
    """Pull a trailing room/venue parenthetical out of an event title into a
    location: "<Event Name> (<Some> Room)" -> ("<Event Name>", "<Some> Room").
    A non-venue parenthetical (e.g. "(on your own)") is left in the title."""
    m = re.search(r"^(.*?)\s*\(([^()]+)\)\s*$", title)
    if m and _ROOM_KW.search(m.group(2)):
        return _clean(m.group(1)), _clean(m.group(2))
    return title, ""


# A venue named inside a blurb ("… in the <Some> Room.", "… in the <Name> Jr.
# Ballroom.") — captured up to a room-type word so an embedded abbreviation
# period (e.g. "Jr.") doesn't truncate it.
_VENUE_RE = re.compile(
    r"\b(?:in|at)\s+the\s+"
    r"((?:[A-Z][\w.&'’\-]*\s+)*?"     # optional leading proper words (may end ".")
    r"(?:Room|Ballroom|Hall|Pavilion|Lobby|Lawn|Suite|Center|Centre|Patio|"
    r"Terrace|Deck|Pool|Beach|Lounge|Garden|Foyer|Theater|Theatre|Auditorium|"
    r"Plaza|Veranda|Courtyard|Atrium))\b")


def _refine_blurb(text: str) -> tuple[str, str]:
    """Split an event blurb into (venue, substantive_details). The venue (if the
    blurb names one) belongs in `location`; any sentence carrying clock times
    ("18:00 - 21:00") is bare logistics and is dropped from the details, leaving
    only the descriptive prose. Paragraph breaks are preserved, and a blurb that
    is ALL logistics (a one-line "held … in …") collapses to an empty details
    string."""
    venue = ""
    m = _VENUE_RE.search(text)
    if m:
        venue = _clean(m.group(1))
    paras: list[str] = []
    for p in text.split("\n\n"):
        kept = [s for s in re.split(r"(?<=[.!?])\s+", p)
                if not re.search(r"\d{1,2}:\d{2}", s)]
        kt = _clean(" ".join(kept))
        if kt:
            paras.append(kt)
    return venue, "\n\n".join(paras)


def _se_key(text: str) -> str | None:
    """Normalised join key for a Special-Events header or a session title, so the
    two can be matched: 'workshop <n>', 'industry', or 'rump' (None = no match)."""
    low = text.lower()
    m = re.search(r"workshop\s+(\d+)", low)
    if m:
        return f"workshop {m.group(1)}"
    if "industry session" in low:
        return "industry"
    if "rump session" in low:
        return "rump"
    return None


# Words ignored when matching a session title to a front-matter description
# header, so a day-program event matches its front-matter write-up even when
# one side adds a weekday prefix or an "Announcement"/"Presentation" suffix.
_DESC_STOP = {
    "the", "a", "an", "of", "and", "in", "on", "for", "to", "with", "at", "by",
    "or", "amp",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "announcement", "announcements", "presentation", "presentations",
    "event", "events", "session", "sessions",
}


def _sig_words(text: str) -> set[str]:
    """Significant (matchable) words of a title/header: lowercase alphanumerics,
    minus stopwords, day names, and 'announcement'/'session' noise."""
    out: set[str] = set()
    for t in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if t in _DESC_STOP:
            continue
        if t.isdigit() or len(t) >= 2:
            out.add(t)
    return out


def _parse_frontmatter_descriptions(
        lines: list["Line"]) -> list[tuple[set[str], str]]:
    """Harvest every front-matter (pre-program) blurb: each event/award header —
    a ~10-12pt bold ALL-CAPS line (Awards, Social Events, …; the big ~16pt
    section banners are skipped) — followed by its FULL body text up to the next
    header. Returns [(significant-word set of the header, full body text)] for
    matching against session titles. Only the front matter (before the first day
    header) is scanned."""
    end = next((i for i, ln in enumerate(lines) if _DAY_RE.match(ln.text)),
               len(lines))
    region = lines[:end]
    out: list[tuple[set[str], str]] = []
    for idx, ln in enumerate(region):
        t = ln.text.strip()
        if not (_is_header_class(ln) and _all_caps(t)
                and ln.content_size < 13):     # 16pt = section banner, skip
            continue
        # Collect the body, preserving PARAGRAPH breaks. Blank lines are dropped
        # during extraction, so a paragraph break shows up as a larger-than-
        # normal vertical gap (or a page change); mark those with a "\n\n" the
        # app renders as a real break (its details box is white-space:pre-wrap).
        parts: list[str] = []
        prev = None
        for ln2 in region[idx + 1:]:
            if _is_header_class(ln2):           # next event/section header
                break
            if prev is not None:
                gap = (ln2.page != prev.page) or (ln2.top - prev.top > 16)
                parts.append("\n\n" if gap else " ")
            parts.append(ln2.text)
            prev = ln2
        text = _clean_paragraphs("".join(parts))
        if text:
            out.append((_sig_words(t), text))
    return out


def _match_frontmatter(title: str,
                       entries: list[tuple[set[str], str]]) -> str | None:
    """Find the front-matter blurb whose header matches a session title. An
    exact significant-word match wins; otherwise one set being a subset of the
    other (with >= 2 words on the smaller side, so a lone generic word can't
    latch onto a longer header that merely contains it)."""
    sw = _sig_words(title)
    if not sw:
        return None
    sub = None
    sub_score = 0
    for hw, text in entries:
        if not hw:
            continue
        if sw == hw:
            return text
        inter = len(sw & hw)
        if inter > sub_score and (
                (hw <= sw and len(hw) >= 2) or (sw <= hw and len(sw) >= 2)):
            sub, sub_score = text, inter
    return sub
    return out


def _parse_special_events(lines: list["Line"]) -> dict[str, dict]:
    """Harvest the descriptive Special-Events blocks (which precede the day-by-day
    program) into {key: {location, description}}. Each block is a bold >=10pt
    header, an optional bold subtitle, a 'Day - time - room' line, then the
    description paragraph(s). Best-effort: anything unparseable is skipped."""
    out: dict[str, dict] = {}
    started = False
    cur: dict | None = None

    def _flush():
        nonlocal cur
        if cur and cur["key"]:
            desc = _clean(" ".join(cur["desc"]))
            prev = out.get(cur["key"])
            if not prev or len(desc) > len(prev.get("description", "")):
                out[cur["key"]] = {"location": cur["location"],
                                   "description": desc}
        cur = None

    for ln in lines:
        if not started:
            if ln.text.strip().upper().startswith("SPECIAL EVENTS"):
                started = True
            continue
        if _DAY_RE.match(ln.text):     # the day-by-day program has begun
            break
        if _is_header_class(ln):       # a Special-Events header (may wrap)
            if cur is not None and not cur["location"] and not cur["desc"]:
                # still in the (multi-line) header — keep merging.
                cur["header"] += " " + ln.text
                cur["key"] = _se_key(cur["header"])
            else:
                _flush()
                cur = {"header": ln.text, "key": _se_key(ln.text),
                       "location": "", "desc": []}
            continue
        if cur is None:
            continue
        wm = _SE_WHEN_RE.match(ln.text)
        if wm:
            if not cur["location"]:
                cur["location"] = _clean(wm.group("room"))
            continue
        cur["desc"].append(ln.text)    # full line — these sit at the margin
    _flush()
    return out


def _parse_chair_blob(blob: str) -> tuple[str, str]:
    """'<Name>, <Aff> [and <Name>, <Aff>]' -> ('Name; Name', 'Aff;Aff')."""
    names: list[str] = []
    affs: list[str] = []
    for chunk in re.split(r"\s+and\s+", blob):
        chunk = _clean(chunk)
        if not chunk:
            continue
        nm, _, aff = chunk.partition(",")
        names.append(_clean(nm))
        affs.append(_clean(aff))
    return "; ".join(n for n in names if n), ";".join(affs)


def build_conference_data() -> dict:
    import pdfplumber

    with pdfplumber.open(str(INPUT_PDF)) as pdf:
        log(f"  PDF has {len(pdf.pages)} pages; extracting lines…")
        lines = _extract_lines(pdf)
    log(f"  reconstructed {len(lines)} content lines.")

    sessions: list[dict] = []
    talks: list[dict] = []
    aff_pool: set[str] = set()

    sess_seq = 0
    talk_seq = 0

    def _record_affs(institutions: list[dict]) -> None:
        for inst in institutions:
            nm = _clean(inst.get("name") or "")
            if nm:
                aff_pool.add(nm)

    def _new_session(title, color, fmt, *, start_ts=None, end_ts=None,
                     details="", topic="") -> dict:
        nonlocal sess_seq
        sess_seq += 1
        s = {
            "id": f"S{sess_seq:03d}", "title": title, "color": color,
            "format": fmt, "topic": topic, "details": details,
            "location": "", "presider": "", "presider_aff": "",
            "start_ts": start_ts, "end_ts": end_ts, "talk_ids": [],
        }
        sessions.append(s)
        return s

    # Parser state.
    cur_dom = cur_month = None          # current calendar day (from day header)
    mode = "pre"                        # 'pre' | 'day' | 'poster' | 'done'
    banner_session: dict | None = None  # session that TALKS attach to
    last_session: dict | None = None    # session a chair attaches to
    open_for_close: dict | None = None  # session an "Adjourn" would close/end
    poster_session: dict | None = None  # current poster catalog session
    poster_pending_meta = False         # poster header just seen; want date line
    skip_next_event = False             # consume a poster-pointer's folded line
    collecting_chair = ""               # accumulating a (possibly wrapped) chair

    # Block accumulation.
    block_lines: list[Line] = []
    block_start = None                  # "HH:MM"
    block_end = None                    # explicit "HH:MM" from a bare-time row
    block_is_poster = False
    block_code = ""

    def _flush_chair() -> None:
        nonlocal collecting_chair
        blob = _clean(collecting_chair)
        collecting_chair = ""
        if not blob or last_session is None:
            return
        names, affs = _parse_chair_blob(blob)
        if names:
            last_session["presider"] = (
                "; ".join(p for p in [last_session["presider"], names] if p))
            last_session["presider_aff"] = ";".join(
                p for p in [last_session["presider_aff"], affs] if p)
            for a in affs.split(";"):
                if _clean(a):
                    aff_pool.add(_clean(a))

    def _flush_block() -> None:
        nonlocal block_lines, block_start, block_end, block_is_poster
        nonlocal block_code, banner_session, last_session, talk_seq
        nonlocal skip_next_event, open_for_close
        if not block_lines and not block_code:
            block_lines, block_start, block_end = [], None, None
            block_is_poster, block_code = False, ""
            return

        if block_is_poster and poster_session is not None:
            fallback = ""
        elif banner_session is not None:
            fallback = _banner_kind(banner_session["format"] and
                                    banner_session["title"]).get("short", "")
            fallback = banner_session.get("topic") or banner_session["title"]
        else:
            fallback = ""

        payload = _talk_payload_from_lines(block_lines, fallback)
        # Reset block accumulators up-front; we've captured what we need.
        bs, be = block_start, block_end
        is_poster, code = block_is_poster, block_code
        block_lines, block_start, block_end = [], None, None
        block_is_poster, block_code = False, ""

        if payload is None:
            return

        # ---- POSTER ----
        if is_poster:
            if poster_session is None:
                return
            talk_seq += 1
            tid = f"T{talk_seq:03d}"
            _record_affs(payload["institutions"])
            talks.append({
                "id": tid, "session_id": poster_session["id"],
                "title": payload["title"], "number": code,
                "start_ts": poster_session["start_ts"],
                "end_ts": poster_session["end_ts"],
                "speaker": payload["speaker"], "presenter": payload["presenter"],
                "speaker_pos": payload["speaker_pos"],
                "authors": payload["authors"],
                "author_aliases": payload["author_aliases"],
                "institutions": payload["institutions"],
                "institutions_may_dedup": False,
                "abstract": "", "status": "", "withdrawn": False,
                "first_author": payload["first_author"],
                "last_author": payload["last_author"],
                "color": "teal", "location": "",
            })
            poster_session["talk_ids"].append(tid)
            return

        # ---- timed EVENT ----
        if payload["is_event"]:
            if skip_next_event:
                skip_next_event = False     # a poster pointer's folded subtitle
                return
            start_ts = _iso(cur_dom, cur_month, bs) if (bs and cur_dom) else None
            end_ts = _iso(cur_dom, cur_month, be) if (be and cur_dom) else None

            # Adjournment ("Adjourn", "Workshop Adjourns") is NOT content — it is
            # only the marker for when a session / the conference ENDS. We never
            # emit it as a session or a talk; instead we stamp the currently-open
            # session's closing time with it (so the session's span — and its
            # last item — end exactly there) and close it. See AGENTS.md
            # ("Adjournment is an end marker, not content").
            if re.search(r"(?i)\badjourn", payload["title"]):
                if open_for_close is not None and start_ts:
                    open_for_close["_closing_ts"] = start_ts
                banner_session = None
                return

            # A Sunday Workshop runs a half-day and OWNS its internal agenda
            # items (a mid-workshop Lunch, a panel discussion). Fold those into
            # the workshop as Event-typed talk-rows rather than scattering them
            # as standalone sessions.
            if banner_session is not None \
                    and banner_session["format"] == "Sunday Workshop":
                talk_seq += 1
                tid = f"T{talk_seq:03d}"
                talks.append({
                    "id": tid, "session_id": banner_session["id"],
                    "title": payload["title"], "number": "",
                    "start_ts": start_ts, "end_ts": end_ts,
                    "speaker": "", "presenter": "", "speaker_pos": None,
                    "authors": [], "author_aliases": [], "institutions": [],
                    "institutions_may_dedup": False,
                    "abstract": payload.get("details", ""),
                    "status": "", "withdrawn": False,
                    "first_author": "", "last_author": "",
                    "color": "rose", "location": "",
                })
                banner_session["talk_ids"].append(tid)
                return

            ev_title, ev_loc = _split_event_location(payload["title"])
            ev = _new_session(ev_title, "rose", "Event",
                              start_ts=start_ts, end_ts=end_ts,
                              details=payload.get("details", ""))
            if ev_loc:
                ev["location"] = ev_loc
            # A ceremony's role-people (each listed under a "<Role>:" label)
            # preside over the event — record them as presiders, not details.
            if payload.get("presider"):
                ev["presider"] = payload["presider"]
                ev["presider_aff"] = payload.get("presider_aff", "")
                for a in (payload.get("presider_aff") or "").split(";"):
                    if _clean(a):
                        aff_pool.add(_clean(a))
            last_session = ev
            open_for_close = ev          # a later "Adjourn" ends this event
            return

        # ---- timed TALK ----
        if banner_session is None:
            banner_session = _new_session(
                f"{_weekday(cur_month, cur_dom)} Program", "blue", "Session")
            last_session = banner_session
            open_for_close = banner_session
        skip_next_event = False
        talk_seq += 1
        tid = f"T{talk_seq:03d}"
        start_ts = _iso(cur_dom, cur_month, bs) if (bs and cur_dom) else None
        end_ts = _iso(cur_dom, cur_month, be) if (be and cur_dom) else None
        kind = _banner_kind(banner_session["title"])
        _record_affs(payload["institutions"])
        t = {
            "id": tid, "session_id": banner_session["id"],
            "title": payload["title"], "number": "",
            "start_ts": start_ts, "end_ts": end_ts,
            "speaker": payload["speaker"], "presenter": payload["presenter"],
            "speaker_pos": payload["speaker_pos"],
            "authors": payload["authors"],
            "author_aliases": payload["author_aliases"],
            "institutions": payload["institutions"],
            "institutions_may_dedup": False,
            "abstract": "", "status": "", "withdrawn": False,
            "first_author": payload["first_author"],
            "last_author": payload["last_author"],
            "color": kind["talk_color"], "location": "",
        }
        talks.append(t)
        banner_session["talk_ids"].append(tid)

    def _weekday(month, dom) -> str:
        import datetime as _dt
        if not month or not dom:
            return ""
        return _dt.date(YEAR, month, dom).strftime("%A")

    # ---- banner accumulation (banners may wrap onto a 2nd line) ----
    pending_banner: list[str] = []

    def _flush_banner() -> None:
        nonlocal pending_banner, banner_session, last_session, poster_session
        nonlocal skip_next_event, open_for_close
        if not pending_banner:
            return
        title = _clean(" ".join(pending_banner))
        pending_banner = []
        if mode == "poster":
            # Poster pages carry category sub-headers (a grouping that changes
            # several times within one session); we don't surface them as a
            # single misleading session-level tag.
            return
        if _is_poster_pointer(title):
            # Day-page pointer at a poster section; skip it and its folded line.
            banner_session = None
            skip_next_event = True
            return
        kind = _banner_kind(title)
        banner_session = _new_session(title, kind["session_color"],
                                      kind["format"], topic=kind["short"])
        last_session = banner_session
        open_for_close = banner_session
        skip_next_event = False

    # =====================================================================
    # Main pass.
    # =====================================================================
    for ln in lines:
        if mode == "done":
            break
        text = ln.text

        # ---- day header ----
        m = _DAY_RE.match(text)
        if m and m.group("mon").lower() in MONTHS and not _POSTER_HDR_RE.match(text):
            _flush_block(); _flush_chair(); _flush_banner()
            cur_dom = int(m.group("dom"))
            cur_month = MONTHS[m.group("mon").lower()]
            mode = "day"
            banner_session = None
            poster_session = None
            skip_next_event = False
            continue

        if mode == "pre":
            continue                       # skip front matter before day 1

        # ---- poster section header ----
        if _POSTER_HDR_RE.match(text):
            _flush_block(); _flush_chair(); _flush_banner()
            mode = "poster"
            banner_session = None
            poster_session = _new_session(_clean(text), "teal", "Poster Session")
            last_session = poster_session
            poster_pending_meta = True
            skip_next_event = False
            continue

        # ---- end of technical program ----
        if re.match(r"^Conference Announcements\b", text, re.I):
            _flush_block(); _flush_chair(); _flush_banner()
            mode = "done"
            continue

        # ---- structural header (banner / poster category) ----
        # A banner has NO leading time/poster-code token. Event rows render
        # their text a hair larger (~10pt) than talk titles (9.1pt), so they'd
        # otherwise read as header-class; the time-token guard keeps them out.
        is_time_row = ln.x0 < LEFT_MARGIN_X and bool(_TIME_ROW_RE.match(text))
        is_poster_code = (mode == "poster" and ln.x0 < LEFT_MARGIN_X
                          and bool(_POSTER_CODE_RE.match(text)))
        if _is_header_class(ln) and not is_time_row and not is_poster_code:
            _flush_block(); _flush_chair()
            pending_banner.append(ln.text)
            continue
        elif pending_banner:
            _flush_banner()

        # ---- poster session date/time + subtitle (right after poster header) --
        if mode == "poster" and poster_pending_meta:
            dm = _POSTER_DATETIME_RE.match(text)
            if dm and poster_session is not None:
                dom = int(dm.group(2)); mon = MONTHS.get(dm.group(3).lower())
                if mon:
                    poster_session["start_ts"] = _iso(dom, mon, dm.group(4))
                    poster_session["end_ts"] = _iso(dom, mon, dm.group(5))
                    cur_dom, cur_month = dom, mon
                poster_pending_meta = False
                continue
            # A non-date line here is the poster section subtitle (e.g. the
            # poster category description); fold it into the session details.
            if not _POSTER_CODE_RE.match(text):
                if poster_session is not None and not poster_session["details"]:
                    poster_session["details"] = _clean(text)
                continue

        # ---- session chair (and its wrapped continuation) ----
        cm = _CHAIR_RE.match(text)
        if cm:
            _flush_block()
            collecting_chair = cm.group(1)
            continue
        if collecting_chair:
            # Continuation lines of a chair blob are plain content in the right
            # column; a time row / poster code / header ends the chair.
            if not _TIME_ROW_RE.match(text) and not (
                    mode == "poster" and _POSTER_CODE_RE.match(text)):
                collecting_chair += " " + ln.content_text
                continue
            _flush_chair()

        # ---- poster code row ----
        if mode == "poster":
            pc = _POSTER_CODE_RE.match(text)
            if pc and ln.x0 < LEFT_MARGIN_X:
                _flush_block()
                block_is_poster = True
                block_code = pc.group(1)
                rest = pc.group(2).strip()
                if rest:
                    block_lines = [_synth_content_line(ln)]
                continue

        # ---- time row (talk/event start, or bare end-time) ----
        if ln.x0 < LEFT_MARGIN_X and _TIME_ROW_RE.match(text):
            if _BARE_TIME_RE.match(text):
                # End time of the current block (e.g. "12:20 -\n14:00").
                bt = _TIME_ROW_RE.match(text).group(1)
                block_end = bt
                continue
            tm = _TIME_ROW_RE.match(text)
            _flush_block()
            block_start = tm.group(1)
            rest = tm.group(3).strip()
            if rest:
                block_lines = [_synth_content_line(ln)]
            continue

        # ---- ordinary content line: part of the current block ----
        if block_start is not None or block_code:
            # A fresh bold ALL-CAPS title arriving after the current block has
            # already collected body (author/affil) lines is a NEW talk that
            # shares the slot — e.g. a Sunday workshop listing several talks
            # under one time with no per-talk time row. Split it off.
            if (block_start is not None and not block_is_poster
                    and ln.content_bold and not ln.content_oblique
                    and _all_caps(ln.content_text)
                    and _block_has_body(block_lines)):
                carry_start = block_start
                _flush_block()
                block_start = carry_start
                block_lines = [ln]
            else:
                block_lines.append(ln)
        # else: stray line outside any block (ignored).

    _flush_block(); _flush_chair(); _flush_banner()

    by_id = {t["id"]: t for t in talks}

    # ---- session START times first (earliest child); talk-less event sessions
    #      already carry their own start. ----
    for s in sessions:
        starts = [by_id[i]["start_ts"] for i in s["talk_ids"]
                  if i in by_id and by_id[i].get("start_ts")]
        if starts:
            s["start_ts"] = min(starts)

    # The sorted set of top-level session starts, for "what begins next that
    # day" lookups (the single-track days; the parallel Sunday workshops instead
    # carry an explicit adjournment time, so they never rely on this).
    _starts = sorted({s["start_ts"] for s in sessions if s.get("start_ts")})

    def _next_start_after(ts: str | None) -> str | None:
        if not ts:
            return None
        return next((x for x in _starts if x[:10] == ts[:10] and x > ts), None)

    # ---- per-session talk end-times, in each session's own document order.
    #      Doing this PER session (not over a global time-sorted list) is what
    #      makes the parallel Sunday workshops come out right: their start times
    #      interleave, so a global "next item" backfill would cross between
    #      concurrent workshops. A talk runs until the next talk in the same
    #      session that starts later; the LAST talk runs to the session's
    #      adjournment time (_closing_ts) if recorded, else to whatever begins
    #      next that day, else a 15-minute default. ----
    for s in sessions:
        kids = [by_id[i] for i in s["talk_ids"] if i in by_id]
        for i, k in enumerate(kids):
            if k.get("end_ts"):
                continue
            later = next((k2["start_ts"] for k2 in kids[i + 1:]
                          if k2.get("start_ts") and (not k.get("start_ts")
                                                     or k2["start_ts"] > k["start_ts"])),
                         None)
            if later:
                k["end_ts"] = later
            elif s.get("_closing_ts"):
                k["end_ts"] = s["_closing_ts"]
            elif k.get("start_ts"):
                k["end_ts"] = _next_start_after(k["start_ts"]) \
                    or _bump(k["start_ts"], 15)

    # ---- session spans: end = adjournment time if recorded, else the latest
    #      child end; talk-less sessions with no end run to the next item that
    #      day (or a half-hour default). ----
    for s in sessions:
        if s["format"] == "Poster Session":
            continue
        ends = [by_id[i]["end_ts"] for i in s["talk_ids"]
                if i in by_id and by_id[i].get("end_ts")]
        if s.get("_closing_ts"):
            s["end_ts"] = s["_closing_ts"]
        elif ends:
            s["end_ts"] = max(ends)
        elif not s.get("end_ts") and s.get("start_ts"):
            s["end_ts"] = _next_start_after(s["start_ts"]) \
                or _bump(s["start_ts"], 30)

    # ---- enrich special-event sessions with their descriptive blurbs + room
    #      from the Special Events section that precedes the schedule. ----
    special = _parse_special_events(lines)
    if special:
        n_enriched = 0
        for s in sessions:
            info = special.get(_se_key(s["title"]) or "")
            if not info:
                continue
            if info.get("description") and not s["details"]:
                s["details"] = info["description"]
            if info.get("location") and not s["location"]:
                s["location"] = info["location"]
            n_enriched += 1
        log(f"  special-events: {len(special)} blurbs, enriched "
            f"{n_enriched} session(s).")

    # ---- enrich any session still lacking details with the matching
    #      front-matter blurb: award write-ups (award announcements) and
    #      social-event descriptions, etc. ----
    frontmatter = _parse_frontmatter_descriptions(lines)
    if frontmatter:
        n_fm = 0
        for s in sessions:
            if s["details"]:
                continue
            desc = _match_frontmatter(s["title"], frontmatter)
            if not desc:
                continue
            # Route bare location/time logistics to the proper fields and keep
            # only the substantive prose as details.
            venue, prose = _refine_blurb(desc)
            if venue and not s["location"]:
                s["location"] = venue
            if prose:
                s["details"] = prose
                n_fm += 1
        log(f"  front-matter: {len(frontmatter)} blurbs, enriched "
            f"{n_fm} session(s).")

    # Drop sessions that ended up empty and undated (e.g. a skipped pointer).
    sessions = [s for s in sessions
                if s["talk_ids"] or s["start_ts"] or s["format"] == "Event"]

    data = {
        "conference_name": CONFERENCE_NAME,
        "sessions": sorted(sessions, key=lambda s: (s["start_ts"] or "")),
        "talks": sorted(talks, key=lambda t: (t["start_ts"] or "")),
        "session_types": SESSION_TYPE_REGISTRY,
        "talk_types": TALK_TYPE_REGISTRY,
        "affiliation_sources": sorted(aff_pool),
    }
    if CURATOR and (CURATOR.get("name") or "").strip():
        data["curator"] = {
            "name": CURATOR["name"].strip(),
            "affiliation": (CURATOR.get("affiliation") or "").strip(),
            "link": (CURATOR.get("link") or "").strip(),
        }
    return data


def _synth_content_line(ln: Line) -> Line:
    """A time/poster-code row carries its title in the content column; clone the
    line keeping only the content-column chars so it parses like a title line."""
    cc = [c for c in ln.chars if c["x0"] >= CONTENT_X]
    return Line(ln.page, cc or ln.chars)


def _bump(iso_ts: str, minutes: int) -> str:
    import datetime as _dt
    dt = _dt.datetime.fromisoformat(iso_ts) + _dt.timedelta(minutes=minutes)
    return dt.isoformat()


# =============================================================================
# Session tags (Format / Track) for the detail header.
# =============================================================================
def _collapse_session_tags(sessions: list[dict]) -> None:
    for s in sessions:
        s.pop("_closing_ts", None)          # internal adjournment marker
        fmt = (s.pop("format", None) or "").strip()
        topic = (s.pop("topic", None) or "").strip()
        tags = []
        if fmt:
            tags.append({"key": "Format", "value": fmt})
        tl = topic.casefold()
        title_l = str(s.get("title", "")).casefold()
        redundant = (not topic or tl in title_l or title_l.endswith(tl)
                     or tl == fmt.casefold())
        if not redundant:
            tags.append({"key": "Track", "value": topic})
        if tags:
            s["tags"] = tags


def main() -> None:
    log("=" * 72)
    log("[config] CONFERENCE PROCESSOR starting up.")
    log(f"[config]   data dir  : {DATA_DIR}")
    log(f"[config]   input PDF : {INPUT_PDF}")
    log(f"[config]   JSON out  : {OUTPUT_JSON}")
    log("=" * 72)

    if not INPUT_PDF.exists():
        raise SystemExit(
            f"[fatal] Input PDF not found: {INPUT_PDF}\n"
            f"        Run fetch_program_hh2026.py first (or via make_app.py).")

    _bootstrap_pdfplumber()

    log("[1/2] Parsing the program PDF…")
    data = build_conference_data()

    log("[2/2] Writing conference_data.json…")
    _collapse_session_tags(data["sessions"])
    OUTPUT_JSON.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    n_s = len(data["sessions"])
    n_t = len(data["talks"])
    n_posters = sum(1 for t in data["talks"] if t["color"] == "teal")
    n_auth = sum(len(t["authors"]) for t in data["talks"])
    log(f"[done] wrote {OUTPUT_JSON.name}: {n_s} sessions, {n_t} talks "
        f"({n_posters} posters), {n_auth} author entries, "
        f"{len(data['affiliation_sources'])} affiliation strings.")


if __name__ == "__main__":
    main()
