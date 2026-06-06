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

"""process_program_spiepw2025.py — PARSE ONLY (no network).

Reads the single "Technical Program" PDF for the conference from data/ and
emits conference_data.json (the source-agnostic schema in
docs/CONFERENCE_JSON.md) for the shared builder.

SOURCE FORMAT
-------------
The PDF lays out, per sub-conference (a 5-digit conference number), a header
block followed by a sequence of sessions and talks. Every line type is
recognizable by SHAPE, never by its content. The schematic below uses entirely
INVENTED placeholders — no real program titles, names, or affiliations appear
in this source file (see scripts/AGENTS.md: program content is copyrighted and
must stay in data/, not in tracked code, even in comments):

  CONFERENCE NNNNN                         <- new sub-conference (all caps, alone)
  <Conference Title> YYYY                  <- sub-conference title (1+ lines)
  D - D Month YYYY | <Venue, Room ...>     <- sub-conference date range + room
  Conference Chair(s): <Name>, <Aff> (<Country>); ...
  Program Committee: <Name>, <Aff> (<Country>); ...
  <Weekday> D Month YYYY                   <- day separator
  SESSION N: <SESSION TITLE>               <- session title (UPPERCASE, 1+ lines)
  D Month YYYY • H:MM AM - H:MM AM | <Venue, Room>
  Session Chair(s): <Name>, <Aff> (<Country>); ...
  NNNNN-N • H:MM AM - H:MM AM              <- talk: paper number + time span
  <Talk Title> (Invited Paper)             <- talk title (1+ lines, opt. marker)
  Author(s): <Name>, <Aff> (<Country>); <Name>, <Aff> (<Country>)

Poster sessions use a "POSTERS-<DAY>" header instead of "SESSION N:". Plenary /
hot-topics sessions use special UPPERCASE headers (e.g. "<SYMPOSIUM> HOT TOPICS",
"<SYMPOSIUM> PLENARY"). Coffee/Lunch breaks appear as inline non-talk lines and
are skipped. The running page header "Conference NNNNN" (title case) and the
footer "N of M <Symposium> Generated: ..." are stripped; the footer is also how
each sub-conference's symposium is identified.

Plenary and hot-topics SESSIONS are reprinted inside many sub-conference sections
(a hot-topics talk whose number belongs to one sub-conference is reprinted under
others), so talks are de-duplicated globally by paper number and sessions by
(title, start, location); session membership is unioned across reprints.

TYPE TAXONOMY (see scripts/AGENTS.md)
-------------------------------------
Sessions -> Technical (blue) | Plenary (orange) | Poster (teal) | Event (rose).
Talks    -> Invited (indigo) | Contributed (sky) | Plenary (orange) |
            Poster (teal) | Tutorial (fuchsia).
"(Invited Paper)" -> Invited; "(Plenary/Keynote Presentation)" -> Plenary;
"(Tutorial Presentation)" -> Tutorial; poster-session talks -> Poster; else
Contributed.

No content (titles, names, affiliations) is hardcoded here; everything is read
from the PDF at runtime. The only embedded strings are FORMAT descriptors
(regex shapes, the conference display name, generic type labels).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
INPUT_PDF = DATA_DIR / "PW25-Technical-Program.pdf"
OUTPUT_JSON = SCRIPT_DIR / "conference_data.json"

# The single obvious top-level constant the curator may want to review/edit.
CONFERENCE_NAME = "Photonics West 2025"

# Optional curator credit shown at the bottom of the app's About section.
# Set to a {"name", "affiliation"?, "link"?} dict to show one; None omits it.
CURATOR = None

# ---------------------------------------------------------------- color palette
# id == color token == Types-panel filter id. RGB per scripts/AGENTS.md.
COLOR_PALETTE = {
    "blue":    {"fg": "#2563eb", "bg_light": "#e8efff", "bg_dark": "#1a233d"},
    "orange":  {"fg": "#ea580c", "bg_light": "#ffedd5", "bg_dark": "#3b1d0a"},
    "teal":    {"fg": "#0d9488", "bg_light": "#d6f3ef", "bg_dark": "#102b27"},
    "fuchsia": {"fg": "#c026d3", "bg_light": "#fae8ff", "bg_dark": "#3a0f3f"},
    "rose":    {"fg": "#e11d48", "bg_light": "#ffe1e8", "bg_dark": "#38161f"},
    "indigo":  {"fg": "#4f46e5", "bg_light": "#e6e4ff", "bg_dark": "#1d1a3d"},
    "sky":     {"fg": "#0284c7", "bg_light": "#e0f2fe", "bg_dark": "#0c2a3d"},
}
SESSION_TYPES = [
    {"id": "blue", "label": "Technical"},
    {"id": "orange", "label": "Plenary"},
    {"id": "teal", "label": "Poster"},
    {"id": "rose", "label": "Event"},
]
TALK_TYPES = [
    {"id": "indigo", "label": "Invited"},
    {"id": "sky", "label": "Contributed"},
    {"id": "orange", "label": "Plenary"},
    {"id": "teal", "label": "Poster"},
    {"id": "fuchsia", "label": "Tutorial"},
]


def _with_rgb(types: list[dict]) -> list[dict]:
    return [{**t, **COLOR_PALETTE.get(t["id"], {})} for t in types]


# ---------------------------------------------------------------- pdf bootstrap
def _bootstrap_pdfplumber() -> None:
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        print("[setup] Installing pdfplumber…")
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "--quiet", "pdfplumber>=0.10"])


# ---------------------------------------------------------------- line shapes
MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}

NEW_CONF = re.compile(r"^CONFERENCE (\d{4,5})$")
RUN_HEADER = re.compile(r"^Conference (\d{4,5})$")
FOOTER = re.compile(r"\d+ of \d+ (BiOS|LASE|OPTO|Quantum West) Generated")
PAGENUM = re.compile(r"^\d{1,3}$")

# conference-level date+room line, e.g. "25 - 26 January 2025 | Moscone South..."
CONF_DTR = re.compile(r"^\d{1,2}(?:\s*-\s*\d{1,2})? (\w+) 2025 \|\s*(.+)$")
# session-level date • time-time | room
SESSION_DTR = re.compile(
    r"^(\d{1,2}) (\w+) 2025 • (\d{1,2}:\d{2}\s*[AP]M)\s*-\s*"
    r"(\d{1,2}:\d{2}\s*[AP]M)\s*\|\s*(.+)$")
# a session header line that itself carries the date/time on the SAME line,
# e.g. "POSTERS-WEDNESDAY 29 January 2025 • 6:00 PM -8:00 PM | Moscone West..."
HEADER_WITH_DTR = re.compile(
    r"^(.*?)\s+(\d{1,2}) (\w+) 2025 • (\d{1,2}:\d{2}\s*[AP]M)\s*-\s*"
    r"(\d{1,2}:\d{2}\s*[AP]M)\s*\|\s*(.+)$")
DAY = re.compile(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) "
                 r"\d{1,2} \w+ 2025$")
TALK = re.compile(r"^(\d{4,5}-\w+)\s+•\s+(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*"
                  r"(\d{1,2}:\d{2}\s*[AP]M)\s*$")
# A break/meal announcement line, e.g. "Coffee Break 10:30 AM - 11:00 AM" or
# "Lunch/<Symposium> Exhibition Break 11:55 AM - 1:30 PM" (the symposium varies).
BREAK = re.compile(
    r"^(?:(?:Coffee|Lunch|Morning|Afternoon|Networking|Tea|Refreshment)\b.*?"
    r"\bBreak\b|Break\b)", re.IGNORECASE)
# The same announcement when it runs onto the end of an author/affiliation line
# in the PDF layout; used to trim it off so it doesn't pollute an affiliation.
BREAK_TAIL = re.compile(
    r"\s+(?:Coffee|Lunch|Morning|Afternoon|Networking|Tea|Refreshment)\b[\w/ &]*?"
    r"\bBreak\b.*$", re.IGNORECASE)
MARKETING = re.compile(r"^(See full details and updates|spie\.org/pw|"
                       r"This program is current as of|Add events to MySchedule|"
                       r"OPEN TO ALL)", re.IGNORECASE)
# "ON-DEMAND POSTERS" — a no-time online-only poster section (its header is NOT
# followed by a date/time/room line). These are excluded entirely.
ON_DEMAND = re.compile(r"on.?demand\s+posters", re.IGNORECASE)

# talk-title trailing markers -> talk type token
MARKERS = [
    (re.compile(r"\(Invited Paper\)\s*$"), "indigo"),
    (re.compile(r"\((?:Plenary|Keynote) Presentation\)\s*$"), "orange"),
    (re.compile(r"\(Tutorial Presentation\)\s*$"), "fuchsia"),
]


# ---------------------------------------------------------------- author parser
# Words that mark a comma-segment as belonging to an institution (not a person).
# NOTE: deliberately excludes the dotted legal-entity suffixes "S.L."/"S.A."/
# "B.V."/"N.V."/"S.r.l", because those collide with personal middle initials
# (e.g. "Anderson S.L. Gomes") and would misread a name as an institution. Real
# "<Company>, S.L. (Country)" affiliations are still handled by SUFFIX + the
# trailing-country PAREN, so dropping them here loses no institution detection.
INST_KW = re.compile(
    r"\b(Univ|Universit|Institut|Institute|College|Laborator|Lab\.|Ctr\.|Center|"
    r"Centre|Hospital|School|Dept|Department|Faculty|Academ|Foundation|Agency|"
    r"Nazionale|National|Office|Research|Sciences?|Technolog|Politecnico|"
    r"Klinik|Clinic|Hochschule|Zentrum|Consiglio|Consejo|GmbH|Ltd|Inc\.|Corp|"
    r"LLC|Company|Therapeutics|"
    r"Photonics|Optics|Systems|Technologies|CNRS|CEA|NASA|NIST|NTT|IBM|AIST|"
    r"Optronics|Aerospace|Semiconductor|Microsystems|"
    # Non-English institution-type words (generic vocabulary, not program
    # content): Italian "Istituto", Spanish "Universidad", French "Ecole"/
    # "École"/"Recherche"/"Photonique", etc.
    r"Istituto|Universidad|Ecole|École|Recherche|Photonique)", re.IGNORECASE)

# A STRICTER institution keyword set for deciding that a *name* segment is
# actually an institution (the interleaved-affiliation guard in parse_people).
# It contains only distinctive multi-letter institution words — NO acronyms or
# substring-prone tokens — so it never trips on a real surname (the broad
# INST_KW above matches e.g. 'Aist' via 'AIST' and 'Corpuz' via 'Corp', which is
# fine for structural splitting but would wrongly discard those authors here).
_INST_NAME_KW = re.compile(
    r"\b(?:Universit|Universidad|Institut|Istituto|Laborator|Laboratoire|"
    r"Lab\.|Ctr\.|Politecnico|Hochschule|Ecole|École|Recherche|Photonique|"
    r"Nanoscienze|Nanotecnolog)\b", re.IGNORECASE)
# A BARE legal-entity suffix segment: the suffix token alone, optionally trailed
# by its country marker and nothing else ("S.L. (Spain)", "Inc. (United States)",
# "GmbH"). Anchored to end so it does NOT match a company whose NAME merely
# starts with such a token ("AG Consulting (Japan)") — otherwise the preceding
# author name would be bound into the institution.
SUFFIX = re.compile(r"^(LLC|Inc\.?|Ltd\.?|Co\.?|Corp\.?|S\.L\.|S\.A\.|S\.r\.l\.?|"
                    r"GmbH|AG|B\.V\.|N\.V\.|LLP|Pty\.?|Oy|SpA|S\.p\.A\.|KG|K\.K\.|"
                    r"Plc\.?|PLC)\s*(?:\([^)]*\))?\s*$", re.IGNORECASE)
PAREN = re.compile(r"\([^()]*\)\s*$")  # trailing (...) closes an institution
# A country marker: "(United States)", "(Korea, Republic of)", "(China)" — a
# parenthesis whose content starts with a capital then a LOWERCASE letter. The
# lowercase requirement rejects all-caps acronym parens like "(FSOC)"/"(SLM)"
# that occur inside prose, so this distinguishes an affiliation line from a
# descriptive paragraph and an end-of-affiliation from a parenthetical aside.
HAS_COUNTRY = re.compile(r"\([A-Z][a-z][^)]*\)")


def _norm(s: str) -> str:
    """Collapse whitespace and re-join a surname hyphen-split across a line
    wrap (e.g. "Foo- Bar" -> "Foo-Bar")."""
    s = re.sub(r"(\w)-\s+(\w)", r"\1-\2", s)
    return re.sub(r"\s+", " ", s).strip()


def _is_inst_ish(seg: str) -> bool:
    """A comma-segment that clearly belongs to an institution, not a person:
    carries an institution keyword, ends in 'lab', or is a lone token (an
    acronym or one-word company name)."""
    if INST_KW.search(seg) or re.search(r"lab$", seg, re.IGNORECASE):
        return True
    return len(seg.split()) == 1


def _parse_group(group: str) -> tuple[list[str], list[str]]:
    """One ';'-separated author group -> (author names, institution strings).

    Layout is "name1, name2, ..., inst1, inst2, ..." where each institution
    ends with a "(Country)" segment and may span internal commas. We find where
    the names stop and the institutions begin, then split the institution tail
    at each "(Country)"."""
    # Protect commas INSIDE parentheses (e.g. "(Korea, Republic of)") so the
    # comma-split below doesn't tear a country marker in half, then restore them.
    protected = re.sub(r"\(([^()]*)\)",
                        lambda m: "(" + m.group(1).replace(",", "\x00") + ")", group)
    segs = [s.replace("\x00", ",").strip()
            for s in protected.split(",") if s.strip()]
    if not segs:
        return [], []
    paren_idx = [i for i, s in enumerate(segs) if PAREN.search(s)]
    if not paren_idx:
        # No country marker: cut at the first institution keyword, else names.
        for i, s in enumerate(segs):
            if INST_KW.search(s):
                return (segs[:i], [", ".join(segs[i:])])
        return segs, []
    p0 = paren_idx[0]
    start = p0
    # Pull preceding institution-ish segments (a keyword-bearing tail such as a
    # "Univ. of <Place>" head, or a lab name) into the institution.
    while start - 1 >= 0 and _is_inst_ish(segs[start - 1]):
        start -= 1
    # A bare company-suffix head ("LLC", "Inc.", "S.L.") binds the one segment
    # before it (e.g. "<Company>, LLC" / "<Company>, S.L.").
    if SUFFIX.match(segs[start]) and start - 1 >= 0:
        start -= 1
    names = segs[:start]
    insts, cur = [], []
    for s in segs[start:]:
        cur.append(s)
        if PAREN.search(s):
            insts.append(", ".join(cur))
            cur = []
    if cur:
        insts.append(", ".join(cur))
    return names, insts


# Lowercase function/prose words that never appear as a (capitalized) person
# name token; their presence marks a sentence fragment. Name particles (de, van,
# von, della, di, da, la, le, du, dos, bin, al, …) are deliberately excluded.
_PROSE_WORDS = {
    "the", "and", "are", "was", "were", "will", "with", "this", "that", "these",
    "those", "from", "have", "has", "had", "their", "your", "our", "its", "been",
    "which", "who", "can", "could", "would", "should", "may", "might", "must",
    "not", "please", "come", "view", "enjoy", "ask", "attend", "join", "invited",
    "questions", "they", "you", "we", "at", "to", "of", "for", "in", "on", "by",
    "as", "is", "be", "or", "an", "a", "about", "into", "how", "where", "while",
    "during", "each", "all", "use", "used", "using", "present", "provide",
}


def parse_people(block: str) -> list[tuple[str, list[str]]]:
    """Parse an "Author(s):"/"Chair(s):" person block into ordered
    (name, [affiliation strings]) pairs. Each author inherits every institution
    listed in its ';'-group."""
    # Co-chairs/committee members are sometimes printed one per line with no
    # ';' between them, so the lines arrive space-joined: "...(United States)
    # Jane Doe, ...". Re-insert the ';' where a country marker is immediately
    # followed by a new capitalized name, so each becomes its own group.
    # A new person after a country marker: either "(Country) Firstname Last…" or
    # an initial-led "(Country) T. Joshua Pfefer". The initial alternative is
    # needed because a name beginning with an initial (period) would otherwise
    # not match the plain-word pattern and the author would merge into the
    # affiliation. Company suffixes ("Co.", "Inc.") stay protected: they have no
    # space-or-comma right after the first word and no initial pattern.
    block = re.sub(
        r"(\([A-Z][a-z][^)]*\))\s+"
        r"(?=(?:[A-ZÀ-Þ]\.\s*)+[A-ZÀ-Þ]|[A-ZÀ-Þ][A-Za-zà-ÿ'’-]+[-\s,])",
        r"\1; ", block)
    out: list[tuple[str, list[str]]] = []
    for g in block.split(";"):
        g = _norm(g)
        if not g:
            continue
        names, insts = _parse_group(g)
        # Affiliations are sometimes interleaved among the author names rather
        # than listed all-at-end, so the names/institutions split above can
        # leave an institution sitting in `names`. Any "name" segment carrying
        # an institution keyword is not a person — move it to the affiliation
        # list so it never becomes an author (or the bubble byline).
        kept = []
        for n in names:
            (insts.append(n) if _INST_NAME_KW.search(n) else kept.append(n))
        names = kept
        # Drop affiliations that are too long to be a real institution name —
        # these are descriptive prose that leaked past the upstream guards.
        insts = [i for i in insts if len(i) <= 160]
        names = [_norm(n) for n in names if _norm(n)]
        if not names:
            # Group with only an institution and no parseable name: keep its
            # affiliation (the author name was simply unparseable here).
            if insts:
                out.append(("", insts))
            continue
        for n in names:
            # A "name" carrying a lowercase prose word (e.g. "the", "are",
            # "invited") or far too many tokens is a sentence fragment, not a
            # person — skip it. The check is case-sensitive so capitalized real
            # names ("Will", "An", "Le") are never mistaken for prose.
            if len(n.split()) > 6 or any(
                    w.strip(".,;:()") in _PROSE_WORDS for w in n.split()):
                continue
            out.append((n, insts))
    return out


# ---------------------------------------------------------------- time helpers
def _to_iso(year: int, month: int, day: int, hhmm: str) -> str:
    m = re.match(r"(\d{1,2}):(\d{2})\s*([AP])M", hhmm.strip(), re.IGNORECASE)
    h, mn, ap = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if ap == "P" and h != 12:
        h += 12
    if ap == "A" and h == 12:
        h = 0
    return f"{year:04d}-{month:02d}-{day:02d}T{h:02d}:{mn:02d}:00"



# Compact building abbreviations for the short location shown on bubbles.
_BLDG_SHORT = {
    "Moscone South": "MS", "Moscone West": "MW", "Moscone North": "MN",
    "Moscone Center": "Moscone", "InterContinental Hotel": "IC Hotel",
}


def _short_level(lvl: str) -> str:
    """Compact a floor/area descriptor: 'Level 1 Lobby'->'L1 Lobby',
    'Level 2'->'L2', '5th Floor'->'L5', 'Upper Mezz'->'Upper Mezz.', etc."""
    s = lvl.strip()
    m = re.match(r"(?i)^level\s+(\d+)(?:\s+lobby)?$", s)   # "Level 1 Lobby"->"L1"
    if m:
        return f"L{m.group(1)}"
    m = re.match(r"(?i)^(\d+)(?:st|nd|rd|th)\s+floor$", s)
    if m:
        return f"L{m.group(1)}"
    if re.match(r"(?i)^lower\s+mezz", s):
        return "L Mezz"
    if re.match(r"(?i)^upper\s+mezz", s):
        return "U Mezz"
    if re.match(r"(?i)^exhibit\s+level$", s):
        return "Exhibit"
    return s


def _short_location(loc: str) -> str:
    """Build the compact bubble location, e.g.
        'Moscone South, Room 151 (Upper Mezz)'  -> 'MS 151 (U Mezz)'
        'Moscone West, Room 2003 (Level 2)'     -> 'MW 2003 (L2)'
        'Moscone South, Room 101 (Level 1 Lobby)' -> 'MS 101 (L1)'
        'InterContinental Hotel, InterContinental Ballroom B (5th Floor)'
                                                -> 'IC Hotel, Ballroom B (L5)'
    Returns '' when there's nothing to shorten."""
    if not loc:
        return ""
    lvl = ""
    m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", loc)
    if m:
        main, lvl = m.group(1).strip(), _short_level(m.group(2).strip())
    else:
        main = loc.strip()
    segs = [s.strip() for s in main.split(",") if s.strip()]
    if not segs:
        return ""
    bshort = _BLDG_SHORT.get(segs[0], segs[0])
    space = ", ".join(segs[1:])
    space = re.sub(r"(?i)^room\s+", "", space)        # "Room 151" -> "151"
    space = re.sub(r"(?i)^intercontinental\s+", "", space)  # de-dup ballroom prefix
    if bshort in ("MS", "MW", "MN") and re.fullmatch(r"[\d/]+[A-Za-z]?", space):
        core = f"{bshort} {space}"                      # "MS 151"
    elif space:
        core = f"{bshort}, {space}"                     # "IC Hotel, Ballroom B"
    else:
        core = bshort
    return core + (f" ({lvl})" if lvl else "")


def _clean_location(loc: str) -> str:
    """Tidy a room/location string. The source PDF occasionally mis-prints one
    (e.g. "Moscone West, Room 155 (Upper Mezz))" with a doubled ')'); collapse
    repeated parentheses and balance a stray trailing ')'."""
    loc = _norm(loc)
    loc = re.sub(r"\){2,}", ")", loc)
    loc = re.sub(r"\({2,}", "(", loc)
    while loc.count(")") > loc.count("("):
        loc = re.sub(r"\s*\)\s*$", "", loc)
    return loc.strip()


def _paren_open(buf: list[str]) -> bool:
    """True when the accumulated buffer has an unclosed '(' — i.e. an
    affiliation's country marker wrapped across a line ("... (United" / "States)")
    and the closing fragment is still to come. Used so a wrapped-country tail
    line isn't mistaken for a prose paragraph and dropped."""
    j = " ".join(buf)
    return j.count("(") > j.count(")")


def _headerish(line: str) -> bool:
    """True for an UPPERCASE session-title line (or a wrapped continuation of
    one). The source renders every session title in uppercase, while talk
    titles, author lines, and affiliations are mixed case."""
    letters = [c for c in line if c.isalpha()]
    if len(letters) < 3:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) >= 0.85


# ---------------------------------------------------------------- main parse
class Parser:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}     # key -> session dict
        self.by_id: dict[str, dict] = {}        # session id -> session dict
        self.session_order: list[str] = []
        self.talks: dict[str, dict] = {}        # paper number -> talk dict
        self.aff_sources: set[str] = set()

        self.conf_num = ""
        self.conf_title = ""
        self.symposium = ""
        self.cur_session_key: str | None = None
        self.cur_session_date: tuple[int, int] | None = None  # (month, day)

        self.mode = "idle"
        self.conf_title_buf: list[str] = []
        self.header_buf: list[str] = []
        self.chair_buf: list[str] = []          # session chair, accumulating
        self.title_buf: list[str] = []          # talk title, accumulating
        self.author_buf: list[str] = []         # talk authors, accumulating
        self.meta_buf: list[str] = []           # conf chair/committee, accumulating
        self.cur_talk: dict | None = None
        self.skip_ondemand = False     # inside an excluded "ON-DEMAND POSTERS" block

    # -- finalizers -----------------------------------------------------------
    def _finalize_talk(self) -> None:
        if self.cur_talk is None:
            return
        t = self.cur_talk
        title = BREAK_TAIL.sub("", _norm(" ".join(self.title_buf)))
        color = t["_session_color_default"]  # poster->teal, plenary->orange...
        for rx, tok in MARKERS:
            if rx.search(title):
                title = rx.sub("", title).strip()
                if t["_in_poster"]:
                    color = "teal"           # a poster stays a poster
                else:
                    color = tok
                break
        else:
            if not t["_in_poster"] and t["_session_color_default"] == "orange":
                color = "orange"             # plenary-session talk
            elif not t["_in_poster"]:
                color = "sky"                # contributed
        t["title"] = title
        t["color"] = color

        # authors / institutions
        block = BREAK_TAIL.sub("", _norm(" ".join(self.author_buf)))
        people = parse_people(block) if block else []
        institutions: list[dict] = []
        inst_num: dict[str, int] = {}
        authors_out: list[dict] = []
        names_only: list[str] = []
        for name, insts in people:
            nums = []
            for raw in insts:
                self.aff_sources.add(raw)
                if raw not in inst_num:
                    inst_num[raw] = len(inst_num) + 1
                    institutions.append({"n": inst_num[raw], "name": raw})
                nums.append(inst_num[raw])
            if name:
                authors_out.append({"name": name, "insts": nums})
                names_only.append(name)
        if authors_out:
            t["authors"] = authors_out
            t["institutions"] = institutions
            t["speaker"] = names_only[0]
            t["speaker_pos"] = 0
            t["first_author"] = names_only[0]
            t["last_author"] = names_only[-1]

        for k in list(t):
            if k.startswith("_"):
                del t[k]
        # de-dup by paper number; keep first occurrence + its session
        num = t["number"]
        if num not in self.talks:
            self.talks[num] = t
            sess = self.by_id.get(t["session_id"])
            if sess is not None and num not in sess["_talk_nums"]:
                sess["_talk_nums"].add(num)
                sess["talk_ids"].append(t["id"])
        self.cur_talk = None
        self.title_buf = []
        self.author_buf = []

    def _flush_meta(self) -> None:
        """Parse the accumulated conference Chair(s)/Program Committee block as a
        whole (it wraps across many lines) and harvest its affiliations for the
        shortener. Parsing the full block — rather than each physical line —
        avoids splitting an institution mid-name into fragments like 'Inc.'."""
        if not self.meta_buf:
            return
        for _name, insts in parse_people(_norm(" ".join(self.meta_buf))):
            for raw in insts:
                self.aff_sources.add(raw)
        self.meta_buf = []

    def _finalize_chair(self) -> None:
        if not self.chair_buf or self.cur_session_key is None:
            self.chair_buf = []
            return
        block = BREAK_TAIL.sub("", _norm(" ".join(self.chair_buf)))
        people = parse_people(block)
        sess = self.sessions[self.cur_session_key]
        names, affs = [], []
        for name, insts in people:
            if not name:
                continue
            names.append(name)
            affs.append(insts[0] if insts else "")
            for raw in insts:
                self.aff_sources.add(raw)
        if names:
            sess["presider"] = "; ".join(names)
            if any(affs):
                sess["presider_aff"] = "; ".join(affs)
        self.chair_buf = []

    # -- session creation -----------------------------------------------------
    def _open_session(self, title: str, month: int, day: int,
                      start: str, end: str, location: str) -> None:
        location = _clean_location(location)
        title = _norm(title)
        is_poster = title.upper().startswith("POSTER")
        # strip a leading "SESSION N:" label from the displayed title; the final
        # display casing is applied in run() once acronyms have been learned.
        m = re.match(r"^SESSION\s+\w+:\s*(.+)$", title, re.IGNORECASE)
        disp_raw = _norm(m.group(1).strip() if m else title)
        start_ts = _to_iso(2025, month, day, start)
        end_ts = _to_iso(2025, month, day, end)
        # Within a conference, a session is keyed by its (normalized title, time,
        # room). Cross-conference reprints (plenary / hot-topics / joint / focus
        # sessions printed in EVERY participating conference's pages) are merged
        # afterwards in run() by their normalized title — keying on disp_raw here
        # (whitespace-folded) rather than the raw wrapped header makes that merge
        # robust to the different line wrapping each reprint uses.
        key = f"{self.conf_num}|{disp_raw}|{start_ts}|{location.lower()}"
        self.cur_session_date = (month, day)
        if key not in self.sessions:
            sid = f"S{len(self.session_order) + 1}"
            sess = {
                "id": sid,
                "title": disp_raw,        # display casing applied in run()
                "color": "blue",          # finalized later by _type_session
                "start_ts": start_ts,
                "end_ts": end_ts,
                "location": location,
                # Compact form for bubbles; the builder shows this in lists and
                # the full `location` in detail views. Omitted when it wouldn't
                # actually be shorter.
                **({"short_location": _short_location(location)}
                   if location and _short_location(location) != location else {}),
                "talk_ids": [],
                "tags": self._session_tags(is_poster),
                "_talk_nums": set(),
                "_is_poster": is_poster,
                "_disp_raw": disp_raw,
                "_raw_title": title.upper(),
            }
            self.sessions[key] = sess
            self.by_id[sid] = sess
            self.session_order.append(key)
        self.cur_session_key = key

    def _session_tags(self, is_poster: bool) -> list[dict]:
        tags = []
        if self.symposium:
            tags.append({"key": "Symposium", "value": self.symposium})
        if self.conf_num:
            cname = f"{self.conf_num}"
            if self.conf_title:
                cname += f": {self.conf_title}"
            tags.append({"key": "Conference", "value": cname})
        return tags

    def _session_default_talk_color(self) -> str:
        sess = self.sessions.get(self.cur_session_key or "")
        if sess is None:
            return "sky"
        if sess["_is_poster"]:
            return "teal"
        t = sess["_raw_title"]
        if "PLENARY" in t or "HOT TOPIC" in t:
            return "orange"
        return "sky"

    # -- line dispatch --------------------------------------------------------
    def feed_line(self, line: str) -> None:
        s = line.strip()
        if not s:
            return

        m = NEW_CONF.match(s)
        if m:
            self._finalize_talk()
            self._finalize_chair()
            self._flush_meta()
            self.conf_num = m.group(1)
            self.conf_title = ""
            self.cur_session_key = None
            self.skip_ondemand = False
            self.mode = "conf_title"
            self.conf_title_buf = []
            self.header_buf = []
            return

        if RUN_HEADER.match(s) or FOOTER.search(s) or MARKETING.match(s):
            return
        if PAGENUM.fullmatch(s):
            return

        if self.mode == "conf_title":
            cm = CONF_DTR.match(s)
            if cm:
                self.conf_title = _norm(" ".join(self.conf_title_buf))
                self.mode = "idle"
                return
            self.conf_title_buf.append(s)
            return

        # conference room/date line outside conf_title (ignore content)
        if CONF_DTR.match(s) and not SESSION_DTR.match(s):
            return

        mm = re.match(r"^(Conference\b[^:]*Chair\(s\):|Program Committee:)", s)
        if mm:
            self._finalize_talk()
            self.meta_buf.append(s[mm.end():].strip())
            self.mode = "conf_meta"
            return

        if DAY.match(s):
            self._finalize_talk()
            self._finalize_chair()
            self._flush_meta()
            self.skip_ondemand = False
            self.mode = "idle"
            self.header_buf = []
            return

        # session header that carries its own date/time on the same line
        hm = HEADER_WITH_DTR.match(s)
        if hm and (self.header_buf or _headerish(hm.group(1))):
            self._finalize_talk()
            self._finalize_chair()
            self._flush_meta()
            title = _norm(" ".join(self.header_buf + [hm.group(1)]))
            self._open_session(title, MONTHS.get(hm.group(3), 1), int(hm.group(2)),
                               hm.group(4), hm.group(5), _norm(hm.group(6)))
            self.header_buf = []
            self.skip_ondemand = False
            self.mode = "in_session"
            return

        sm = SESSION_DTR.match(s)
        if sm:
            self._finalize_talk()
            self._finalize_chair()
            self._flush_meta()
            title = _norm(" ".join(self.header_buf))
            if not title:
                title = "Session"
            self._open_session(title, MONTHS.get(sm.group(2), 1), int(sm.group(1)),
                               sm.group(3), sm.group(4), _norm(sm.group(5)))
            self.header_buf = []
            self.skip_ondemand = False
            self.mode = "in_session"
            return

        if s.startswith("Session Chair(s):"):
            self._finalize_talk()
            self.chair_buf = [s[len("Session Chair(s):"):].strip()]
            self.mode = "sess_chair"
            return

        tm = TALK.match(s)
        if tm and self.skip_ondemand:
            return                       # talk inside an excluded on-demand block
        if tm and self.cur_session_key is not None:
            self._finalize_talk()
            self._finalize_chair()
            num = tm.group(1)
            sess = self.sessions[self.cur_session_key]
            month, day = self.cur_session_date
            self.cur_talk = {
                "id": f"T-{num}",
                "session_id": sess["id"],
                "number": num,
                "title": "",
                "color": "sky",
                "start_ts": _to_iso(2025, month, day, tm.group(2)),
                "end_ts": _to_iso(2025, month, day, tm.group(3)),
                "_in_poster": sess["_is_poster"],
                "_session_color_default": self._session_default_talk_color(),
            }
            self.title_buf = []
            self.author_buf = []
            self.mode = "talk_title"
            return

        if s.startswith("Author(s):"):
            self.author_buf = [s[len("Author(s):"):].strip()]
            self.mode = "authors"
            return

        if BREAK.match(s):
            self._finalize_talk()
            self.header_buf = []
            if self.mode not in ("sess_chair",):
                self.mode = "in_session" if self.cur_session_key else "idle"
            return

        # An "ON-DEMAND POSTERS" header (no date/time line follows it) starts an
        # online-only poster block we exclude entirely: skip its talks until the
        # next real boundary (conference / day / session), which clears the flag.
        if _headerish(s) and ON_DEMAND.search(s):
            self._finalize_talk()
            self._finalize_chair()
            self.skip_ondemand = True
            self.header_buf = []
            self.mode = "in_session"
            return

        # ---- generic / continuation line --------------------------------
        if self.mode == "talk_title":
            self.title_buf.append(s)
            return
        if self.mode == "authors":
            if _headerish(s):
                self._finalize_talk()
                self.header_buf = [s]
                self.mode = "collect_header"
            else:
                self.author_buf.append(s)
            return
        if self.mode == "sess_chair":
            if _headerish(s):
                self._finalize_chair()
                self.header_buf = [s]
                self.mode = "collect_header"
            elif HAS_COUNTRY.search(s) or _paren_open(self.chair_buf):
                # genuine chair-list wrap (carries a country, or completes a
                # country marker that wrapped across the previous line)
                self.chair_buf.append(s)
            else:
                # a descriptive paragraph follows the chair line — end the chair
                # list here and ignore the prose so it can't pollute the presider
                self._finalize_chair()
                self.mode = "in_session"
            return
        if self.mode == "conf_meta":
            # committee / chair continuation: accumulate genuine list wraps; a
            # line with no affiliation country is prose -> stop accumulating.
            if _headerish(s):
                self._flush_meta()
                self.header_buf = [s]
                self.mode = "collect_header"
            elif HAS_COUNTRY.search(s) or _paren_open(self.meta_buf):
                self.meta_buf.append(s)
            else:
                self._flush_meta()
                self.mode = "idle"
            return
        # idle / in_session / collect_header
        if _headerish(s):
            self.header_buf.append(s)
            self.mode = "collect_header"
        else:
            # descriptive / marketing text: drop, and abandon a partial header
            if self.mode == "collect_header":
                self.header_buf = []
                self.mode = "in_session" if self.cur_session_key else "idle"

    # -- type finalization ----------------------------------------------------
    def _type_session(self, sess: dict) -> None:
        t = sess["_raw_title"]
        has_talks = bool(sess["talk_ids"])
        fmt = "Oral"
        if sess["_is_poster"]:
            sess["color"] = "teal"
            fmt = "Poster"
        elif "PLENARY" in t or "HOT TOPIC" in t:
            sess["color"] = "orange"
            fmt = "Plenary"
        elif not has_talks:
            sess["color"] = "rose"
            fmt = "Event"
        else:
            sess["color"] = "blue"
            fmt = "Oral"
        sess["tags"].insert(0, {"key": "Format", "value": fmt})


def _union_session_talks(talks: dict, keep: dict, sess: dict) -> None:
    """Move sess's talks into keep (de-duplicated by paper number) and redirect
    those talks' session_id to keep. Shared by the cross-sub-conference reprint
    merge and the co-located poster merge."""
    for tid in sess["talk_ids"]:
        num = tid[len("T-"):]
        if num not in keep["_talk_nums"]:
            keep["_talk_nums"].add(num)
            keep["talk_ids"].append(tid)
        t = talks.get(num)
        if t is not None:
            t["session_id"] = keep["id"]


def _combine_poster_tags(group: list[dict]) -> None:
    """Rebuild the surviving (first) poster session's tags by unioning each tag
    key's values across the whole co-located group, joined into a comma-separated
    list in first-seen order. Every sub-conference's poster session runs in one
    shared hall at one time, so the merged session credits all of them: the
    Conference tag becomes "<num: title>, <num: title>, ..." and Symposium likewise
    when the posters span symposia. De-duplication is by FULL value — sub-conference
    titles themselves contain commas, so a comma-split would corrupt them."""
    keep = group[0]
    values: dict[str, list[str]] = {}
    order: list[str] = []
    for sess in group:
        for t in sess["tags"]:
            k, v = t["key"], t["value"]
            if k not in values:
                values[k] = []
                order.append(k)
            if v not in values[k]:
                values[k].append(v)
    keep["tags"] = [{"key": k, "value": ", ".join(values[k])} for k in order]


def run(pdf_path: Path) -> dict:
    import pdfplumber
    p = Parser()
    with pdfplumber.open(str(pdf_path)) as pdf:
        started = False
        for page in pdf.pages:
            text = page.extract_text() or ""
            sym = None
            for ln in text.splitlines():
                fm = FOOTER.search(ln)
                if fm:
                    sym = fm.group(1)
            if sym:
                p.symposium = sym
            for raw in text.splitlines():
                if not started:
                    if NEW_CONF.match(raw.strip()):
                        started = True
                    else:
                        continue
                p.feed_line(raw)
    p._finalize_talk()
    p._finalize_chair()
    p._flush_meta()

    talks = list(p.talks.values())

    # Merge cross-sub-conference reprinted sessions. A special session (an evening
    # plenary, hot-topics, joint, or "focus" session) is printed in EACH
    # participating sub-conference's pages; because talks are de-duplicated globally
    # by paper number, each reprint holds a DIFFERENT subset of the talks (and
    # some reprints hold none). Collapse every group of sessions sharing the same
    # (normalized title, start, room) into one, unioning their talks and
    # redirecting those talks' session_id. POSTER sessions are handled by their
    # own merge below.
    merged_away: set[str] = set()
    primary: dict[tuple, dict] = {}
    for key in p.session_order:
        sess = p.sessions[key]
        if sess["_is_poster"]:
            continue
        sig = (sess["_disp_raw"], sess["start_ts"], sess["location"].lower())
        keep = primary.get(sig)
        if keep is None:
            primary[sig] = sess
            continue
        _union_session_talks(p.talks, keep, sess)
        merged_away.add(sess["id"])

    # Poster sessions get a parallel merge. Every sub-conference's poster session
    # is scheduled into the SAME room at the SAME time — physically one big
    # poster hall — yet printed under its own sub-conference number. Collapse all
    # posters sharing (start, room) into one (ignoring the per-sub-conference title),
    # unioning their talks and combining the per-sub-conference Symposium/Conference
    # tags into comma-separated lists so the survivor credits every participating
    # sub-conference (see _combine_poster_tags). Distinct one-off poster events
    # (award talks, "poster pop", review sessions) sit at a unique time+room, so
    # they form singleton groups and pass through untouched.
    poster_groups: dict[tuple, list[dict]] = {}
    poster_order: list[tuple] = []
    for key in p.session_order:
        sess = p.sessions[key]
        if not sess["_is_poster"]:
            continue
        sig = (sess["start_ts"], sess["location"].lower())
        if sig not in poster_groups:
            poster_groups[sig] = []
            poster_order.append(sig)
        poster_groups[sig].append(sess)
    for sig in poster_order:
        group = poster_groups[sig]
        keep = group[0]
        for sess in group[1:]:
            _union_session_talks(p.talks, keep, sess)
            merged_away.add(sess["id"])
        _combine_poster_tags(group)

    sessions = []
    for key in p.session_order:
        sess = p.sessions[key]
        if sess["id"] in merged_away:
            continue
        sess["talk_ids"].sort(key=lambda tid: p.talks[tid.replace("T-", "", 1)]
                              ["start_ts"] if tid.replace("T-", "", 1) in p.talks
                              else "")
        raw = sess["_disp_raw"]
        sess["title"] = raw
        p._type_session(sess)
        for k in list(sess):
            if k.startswith("_"):
                del sess[k]
        sessions.append(sess)

    # Guard: drop any affiliation string that is empty or nothing but a country
    # and/or a bare company suffix ("Inc.", "LLC") — such fragments shorten to an
    # empty label and carry no institution. (Belt-and-suspenders; full-block
    # parsing above already avoids producing them.)
    def _meaningful(s: str) -> bool:
        core = re.sub(r"\([^)]*\)", "", s).strip().strip(",").strip()
        core = SUFFIX.sub("", core).strip().strip(",").strip("-").strip()
        return len(re.sub(r"[^A-Za-z]", "", core)) >= 2

    aff_sources = sorted(s for s in p.aff_sources if _meaningful(s))
    out = {
        "conference_name": CONFERENCE_NAME,
        "session_types": _with_rgb(SESSION_TYPES),
        "talk_types": _with_rgb(TALK_TYPES),
        "sessions": sessions,
        "talks": talks,
        "affiliation_sources": aff_sources,
    }
    if CURATOR and (CURATOR.get("name") or "").strip():
        out["curator"] = CURATOR
    return out


def main() -> None:
    print("=" * 72)
    print("[config] conference program PROCESSOR")
    print(f"[config]   input PDF : {INPUT_PDF}")
    print(f"[config]   output    : {OUTPUT_JSON}")
    print("=" * 72)
    if not INPUT_PDF.exists():
        print(f"[fatal] input PDF not found: {INPUT_PDF}")
        print("[fatal] run fetch_program_spiepw2025.py first.")
        sys.exit(1)

    _bootstrap_pdfplumber()
    data = run(INPUT_PDF)

    OUTPUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    ntalk_auth = sum(1 for t in data["talks"] if t.get("authors"))
    print(f"[ok] sessions: {len(data['sessions'])}")
    print(f"[ok] talks   : {len(data['talks'])} "
          f"({ntalk_auth} with parsed authors)")
    print(f"[ok] affiliation sources: {len(data['affiliation_sources'])}")
    print(f"[ok] wrote {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
