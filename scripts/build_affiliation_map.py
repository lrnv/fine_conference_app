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

"""Build a conference affiliation -> canonical short-name map.

WHAT THIS MODULE DOES
The processor's conference_data.json contains affiliation text in several
different forms (long postal addresses, short institution names, presider
affiliations). This module gathers all of those raw strings, removes
duplicates, and produces one dict mapping each distinct raw string to a single
canonical short label. For example, all of these raw strings:
    "School of Physics, University of Bristol, Bristol, United Kingdom"
    "University of Bristol"
    "Univ. of Bristol"
map to the same short label "Bristol". The app then displays the short label
wherever that affiliation appears.

INPUT
Everything comes from conference_data.json's source-agnostic
"affiliation_sources" value. This module reads ONLY that JSON; the processor
does all of the upstream scraping/parsing. It is a single flat list of raw
affiliation strings that the processor has already pooled, de-duplicated, and
sorted. The strings come in several forms, differing only in where the
processor harvested them; they are treated identically here (each becomes a key
to canonicalize):
  - Long, multi-field postal-address lines, e.g.
        "4th Physical Institute, University of Göttingen, Göttingen, Germany".
  - Session-presider affiliations, usually already short, e.g.
        "KAUST" or "University of Florence".
  - Institution names the processor pre-extracted, usually already fairly
        short, e.g. "North Carolina State University". These mostly duplicate
        names already present in the full-address lines, but occasionally
        contribute an institution that never appears in one.
The processor splits any ';'-joined lists at the source, so every entry here is
a single affiliation string and this module needs no further splitting.

HOW A RAW STRING BECOMES A SHORT LABEL (see canonicalize())
Each raw string is run through these steps, in order, and the FIRST one that
produces an answer wins:
  1. RAW_OVERRIDES — an exact-match lookup table for a handful of strings the
     later steps get wrong (e.g. a typo no pattern can catch). Checked first so
     it can override everything else.
  2. ANCHORS — the main, ORDERED list of substring/regex "anchor" patterns,
     each mapping to a canonical short name. The first pattern found anywhere
     in the (normalized) string wins, so the list runs MORE-SPECIFIC patterns
     before MORE-GENERAL ones — e.g. "Johns Hopkins APL" -> "JHU APL" must come
     before plain "Johns Hopkins" -> "Johns Hopkins".
  3. LATE_ANCHORS — extra low-priority patterns tried only after every ANCHOR
     misses (kept separate so short/ambiguous tokens can't pre-empt the
     specific patterns above).
  4. fallback_shorten() — if nothing matched, an algorithm strips address/
     department clutter and shortens the leading institution segment
     (e.g. "X University" -> "X"; "University of X" -> "U X"). NOTE many common
     "University of X" names are given explicit ANCHORS so they resolve to the
     bare place name ("Bristol") instead of this fallback's "U X" form.

Misspellings are absorbed by the anchor patterns (a pattern matching the
substring "ublin" catches both "Dublin" and the typo "Dunlin"), or by a
RAW_OVERRIDES entry when an anchor can't be coaxed into matching.
"""

from __future__ import annotations

import argparse
import re
import unicodedata
from pathlib import Path

# NOTE: this module reads only the source-agnostic affiliation_sources list of
# the processor's data JSON: one flat, de-duplicated list of raw affiliation
# strings (full-address lines, presider affiliations, and institution names are
# all pooled together by the processor, which also splits any ';'-joined lists
# at the source). This module just canonicalizes each string into a short label.


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

# Punctuation-folding table applied inside normalize(): maps every cosmetic
# dash/quote variant seen in the source data to a single canonical ASCII form.
# Built once at import time and reused via str.translate (cheap, table-driven).
#   dashes/minus  -> "-"   apostrophes/quotes -> "'"
# This is what lets anchors keyed on punctuation be written ONCE in ASCII
# instead of one needle per glyph. NFKC alone does not collapse these.
_DASH_VARIANTS = (
    '\u2010'  # hyphen
    '\u2011'  # non-breaking hyphen
    '\u2012'  # figure dash
    '\u2013'  # en dash
    '\u2014'  # em dash
    '\u2015'  # horizontal bar
    '\u2212'  # minus sign
)
_APOS_VARIANTS = (
    '\u2018'  # left single quotation mark
    '\u2019'  # right single quotation mark (typographer's apostrophe)
    '\u201b'  # single high-reversed-9 quotation mark
    '\u02bc'  # modifier letter apostrophe
    '\u00b4'  # acute accent
    '\u0060'  # grave accent
)
_PUNCT_FOLD = {ord(c): '-' for c in _DASH_VARIANTS}
_PUNCT_FOLD.update({ord(c): "'" for c in _APOS_VARIANTS})


def normalize(s: str) -> str:
    """Lowercase + fold cosmetic punctuation + fold diacritics + collapse whitespace.

    Used only for matching against the anchor patterns; the raw key is what
    actually goes into the output dict.
    """
    s = unicodedata.normalize('NFKC', s)
    s = s.lower()
    # Fold punctuation that varies cosmetically across the source data to a
    # single canonical ASCII form, BEFORE any anchor/fallback sees the string.
    # NFKC does NOT unify these (an en dash, a hyphen, and a straight vs. curly
    # apostrophe all survive NFKC distinct), so without this every anchor would
    # otherwise need a duplicate needle per glyph (e.g. "friedrich-alexander"
    # AND "friedrich–alexander"; "dell'insubria" AND "dell’insubria"). Folding
    # here lets each such anchor be written once, in plain ASCII punctuation.
    #   - all Unicode dash/hyphen/minus variants -> ASCII hyphen-minus "-"
    #   - all curly/grave/acute single-quote variants -> ASCII apostrophe "'"
    # Anchors keyed on punctuation should therefore use ASCII "-" and "'".
    s = s.translate(_PUNCT_FOLD)
    # Fold diacritics to their base ASCII letters (université -> universite,
    # universität -> universitat, münchen -> munchen, méxico -> mexico), again
    # BEFORE any anchor sees the string. The source data is inconsistent about
    # accents (the same institution shows up both accented and unaccented), so
    # without this every accented anchor needs a duplicate unaccented twin. With
    # it, each anchor is written ONCE in plain ASCII and matches either spelling.
    # NFD splits a precomposed letter into base + combining mark; we drop the
    # marks and recombine. Anchors should therefore be written unaccented.
    # (Safe w.r.t. the "university" misspelling fixes below: the foreign stems
    # universita/universite/universitat lack the 'r' those typo patterns key on.)
    s = ''.join(c for c in unicodedata.normalize('NFD', s)
                if not unicodedata.combining(c))
    s = unicodedata.normalize('NFC', s)
    # Normalize common MISSPELLINGS of the English word "university" up front,
    # so every downstream anchor/fallback sees the canonical word and we don't
    # need a bespoke anchor per typo. Word-boundary anchored and limited to an
    # explicit list of unambiguous English-typo spellings, so it never touches
    # real foreign forms (universidad, universita`/università, universitat/
    # universität, universite/universite', universidade, universitet,
    # universiteit) nor the legit abbreviations (univ, the "universit" stem).
    s = re.sub(r'\buniversity?of\b', 'university of', s)   # "universityof" (missing space)
    s = re.sub(r'\b(?:univeristy|univerisity|univrsity|universty|'
               r'universiy|universityy|universitry|universitity|univerce|'
               r'uniwersity|niversity)\b',
               'university', s)
    # Fold the "Univ." abbreviation to the full word — every "university of <X>"
    # / "<X> university" anchor below then matches the abbreviated form for
    # free, instead of needing a per-string RAW_OVERRIDE for each "Univ. of X"
    # the source data uses. The trailing dot is consumed so the result is a
    # clean " university " (or "university") boundary; "univ" without a dot is
    # NOT touched (it's a legal abbreviation token in foreign forms like
    # "Univ Politec.").
    s = re.sub(r'\buniv\.', 'university', s)
    # Folks sometimes ASCII-fy the German umlaut as "ae" (Universitaet for
    # Universität); fold to the diacritic-stripped form normalize() already
    # produces from the accented spelling, so a single anchor catches both.
    s = re.sub(r'\buniversitaet\b', 'universitat', s)
    # Same idea for misspellings of "technology" that sit in an institution
    # token an anchor keys on (e.g. "...Science and Technogy" -> KIST,
    # "...Science and Techcnology" -> SUSTech). Explicit list, word-boundary
    # anchored, so it never touches the legit forms (technology, technologies,
    # technological, technische, technical, tech, technion). (Note: "technsche"
    # is a typo of German "technische", not "technology", so it's excluded —
    # it already resolves correctly via the PTB anchor.)
    s = re.sub(r'\b(?:technogy|techcnology|techenology|technologygy|technolog|'
               r'thechnology)\b',
               'technology', s)
    # "Politechnico" is a recurring typo of the Italian "Politecnico" (the
    # English "technology" stem leaking in). Folding here means every
    # "Politecnico di X" anchor matches both spellings without duplication.
    s = re.sub(r'\bpolitechnico\b', 'politecnico', s)
    # Same idea for other tokens that several anchors key on. Each list is
    # explicit (not pattern-based) and word-boundary anchored, so foreign-stem
    # near-misses don't get touched — e.g. Italian "istituto"/"istituti",
    # Spanish "instituto", Italian "ricerca", Catalan "recerca", Spanish
    # "nacional", Portuguese "universidade", Danish "universitet" — none of
    # these appear in the lists below, so they pass through unchanged.
    s = re.sub(r'\b(?:institue|institite|insitute|insttute|intitute)\b',
               'institute', s)
    s = re.sub(r'\b(?:laborator|labortory|labratory)\b', 'laboratory', s)
    s = re.sub(r'\b(?:naitional|natioal)\b', 'national', s)
    s = re.sub(r'\breasearch\b', 'research', s)
    s = re.sub(r'\bmetropokitan\b', 'metropolitan', s)
    s = re.sub(r'\bpolytechinic\b', 'polytechnic', s)
    # City/location MISSPELLINGS that change which campus anchor a string
    # resolves to. Explicit and word-boundary anchored, like the lists above, so
    # a legit token is never touched. "los angles" -> "los angeles" lets the UCLA
    # campus anchor match instead of falling back to a bare "UC". (Only typos
    # that feed an ANCHOR belong here; a misspelling with no anchor — e.g.
    # "Shenzen" for "Shenzhen" — is shortened from the raw string by the fallback
    # and can't be folded here.)
    s = re.sub(r'\blos angles\b', 'los angeles', s)
    # The Indian IITs are often written "Indian Institute of Technology, <City>"
    # / "IIT, <City>" with a comma before the campus. Drop that comma so the
    # per-campus anchors below (which key on "... technology delhi" etc.) match
    # the comma form too, instead of falling through to the bare-IIT anchor.
    s = re.sub(r'\b(indian institute of technology|iit),', r'\1', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


# A "needle" can be either:
#   - a plain substring (default)
#   - a regex string, indicated by starting with "re:" — useful when a short
#     acronym like "ARL" or "LLE" would falsely match inside longer words
#     ("Arlington", "Melbourne", "Bouville").
def _anchor_matches(needle: str, norm: str) -> bool:
    if needle.startswith('re:'):
        return re.search(needle[3:], norm, flags=re.IGNORECASE) is not None
    return needle in norm


# Separator between a state-university name and its campus/branch qualifier.
# In the source data the same campus shows up joined several different ways:
#   "University of California, Irvine"   (comma)
#   "University of California Irvine"     (plain space)
#   "University of California at Irvine"  (the word "at")
#   "University of California - Irvine"   (spaced hyphen; normalize() has
#                                          already folded all dash glyphs to "-")
# A campus anchor written with this separator matches every variant with ONE
# pattern, so a stray punctuation form can't slip past a specific campus anchor
# and fall through to the bare flagship label. Anchored with leading/trailing
# spaces so it always consumes at least one separator character (never matches
# an empty gap, which would let "californiairvine"-style runs through).
_UC_SEP = r'(?:\s*,\s*|\s+at\s+|\s*-\s*|\s+)'


# ---------------------------------------------------------------------------
# Anchor patterns: ordered list of (needle_lowercase, canonical_short).
#
# The first matching needle wins, so order list from MORE-SPECIFIC to
# LESS-SPECIFIC. Matching is plain substring on the normalized key.
#
# These were extracted by clustering the existing map's outputs. Patterns are
# deliberately broad enough to absorb typos ("dunlin"/"dublin", "techenology"/
# "technology") that don't actually destroy the anchor word ("hopkins" still
# says "hopkins" even when misspelled around it).
# ---------------------------------------------------------------------------

_ANCHORS_SRC: list = [
    # ---- Disambiguation: DIFFERENT institutions that share a city/name and
    # would otherwise collapse to the same short via the generic shortener
    # (e.g. "Miami University" in Ohio vs "University of Miami" in Florida).
    # Each is pinned to its standard distinct short. Placed first so a specific
    # "<Place> University of Technology/Science…" wins over a later, broader
    # anchor for the bare flagship (e.g. NTUST before the NTU-Taiwan anchor).
    ('american university in cairo', 'AUC'),
    ('national technical university of athens', 'NTUA'),
    ('auckland university of technology', 'AUT'),
    ('cyprus university of technology', 'CUT'),
    ('fukui university of technology', 'Fukui Tech'),
    ('university of jinan', 'UJN'),  # Shandong — distinct from Jinan Univ. (Guangzhou)
    # Miami: "University of Miami" (FL) and "Miami University" (Ohio) are
    # different schools; "Miami" alone is ambiguous, so pin each distinctly.
    # "U Miami" is kept verbatim (see _POLISH_KEEP) rather than reduced to "Miami".
    ('university of miami', 'U Miami'),   # Florida
    ('miami university', 'Miami Ohio'),   # Ohio
    (['national taiwan university of science and technology', 'national taiwan univ of science', 'ntust'], 'NTUST'),
    ('national taiwan university of sport', 'NTU Sport'),
    ('missouri university of science and technology', 'Missouri S&T'),
    ('queensland university of technology', 'QUT'),  # vs Univ. of Queensland
    ('shenzhen university of information technology', 'SZIIT'),  # vs Shenzhen Univ.
    # Vrije Universiteit Amsterdam (VU) — distinct from Univ. of Amsterdam (UvA),
    # which keeps the bare "Amsterdam". Covers the Dutch spelling and the folded
    # "Vrije University Amsterdam" form (normalize turns "Univ." -> "university").
    (['vrije universiteit amsterdam', 'vrije university amsterdam'], 'VU Amsterdam'),
    ('george washington', 'GW'),  # GWU (DC) — must beat the "washington university" -> WashU anchor
    ('tokyo university of technology', 'Tokyo Technology'),  # vs Univ. of Tokyo
    # University of Texas system: the broad "University of Texas" anchor folds
    # every campus to UT Austin, so pin the distinct ones first.
    ('university of texas m.d. anderson', 'MD Anderson'),
    ('university of texas, md anderson', 'MD Anderson'),
    ('university of texas md anderson', 'MD Anderson'),
    ('university of texas at arlington', 'UT Arlington'),
    ('university of texas at san antonio', 'UTSA'),
    ('university of texas southwestern', 'UT Southwestern'),
    ('university of texas medical branch', 'UTMB'),
    ('university of texas health science ctr. at san antonio', 'UT Health San Antonio'),
    ('university of texas health science ctr. at houston', 'UTHealth Houston'),
    ('university of texas school of dentistry', 'UTHealth Houston'),

    # ---- Well-known institutes whose full name is long but has a standard
    # acronym the generic shortener can't derive. ----
    (['sante et de la recherche medicale', r're:\binserm\b'], 'INSERM'),
    (['microelectronique et de nanotechnologie', r're:\biemn\b'], 'IEMN'),
    ('national institute of child health', 'NICHD'),
    ('national institute of biomedical imaging', 'NIBIB'),
    (['physique et de chimie industrielles', r're:\bespci\b'], 'ESPCI Paris'),
    ('american institute for manufacturing integrated photonics', 'AIM Photonics'),
    # NOTE: IQUIST and QuICS are research centers hosted INSIDE a university, so
    # they are LATE_ANCHORS (low priority) — when the parent university (UIUC /
    # Maryland) is also in the string, it wins; the center acronym is only used
    # for a standalone mention.
    ('organic chemistry and biochemistry of the cas', 'IOCB'),
    (['transformative meta-optical systems', r're:\btmos\b'], 'TMOS'),

    # ---- US national labs (specific names before generic) ------------------
    ('los alamos national lab', 'LANL'),
    ('lawrence livermore', 'LLNL'),
    ('lawrence berkeley', 'LBNL'),
    ('oak ridge national lab', 'Oak Ridge'),
    ('pacific northwest national', 'PNNL'),
    ('brookhaven', 'Brookhaven'),
    ('argonne', 'Argonne'),
    (['sandia nat. laboratories', 'sandia'], 'Sandia'),
    # CINT (Center for Integrated Nanotechnologies) is a Sandia/Los Alamos
    # user facility; map it to Sandia. Some CINT strings carry no "Sandia"
    # token (just the center name + city), so they'd otherwise fall through
    # to the fallback shortener and keep the long center name.
    ('center for integrated nanotechnologies', 'Sandia'),
    (['air force research laboratory', 'air force research lab', 'afrl munitions', 'afrl,', 'afrl ', r're:\bafrl$'], 'AFRL'),
    ('naval research lab', 'NRL'),
    ('naval surface warfare', 'Naval Surface Warfare Center'),
    ('naval air warfare', 'Naval Air Warfare Center'),
    (['mit lincoln', 'mitll', 'lincoln laboratory, massachusetts institute', 'lincoln laboratory, mit', 'massachusetts inst of tech lincoln lab'], 'MIT Lincoln Lab'),
    ('mitre', 'MITRE'),
    ([r're:\bnist\b', 'national institute of standards and technology'], 'NIST'),
    # Abbreviated short-forms from the institution strings. The anchors above
    # only match the fully spelled-out name ("Institute" / "and"); these catch
    # the "Inst" / "&" contractions.
    (['national inst of standards', 'national institute of standards'], 'NIST'),
    # JPL rule: anything mentioning "JPL" OR the spelled-out "Jet Propulsion
    # Laboratory" (NASA's JPL, regardless of whether "NASA" is also present)
    # → 'JPL'.
    ([r're:\bjpl\b', 'jet propulsion lab'], 'JPL'),
    ('nasa goddard', 'NASA Goddard'),
    ('nasa', 'NASA'),
    ('noaa', 'NOAA'),
    ('slac', 'SLAC'),
    ('jila', 'JILA'),
    ([r're:\blle,\s+rochester\b', 'laboratory for laser energetics'], 'Rochester'),
    (['darpa mto', 'darpa'], 'DARPA'),
    (['army research lab', r're:\barl\b'], 'ARL'),
    # The US Army Combat Capabilities Development Command: the spelled-out name,
    # its former "CCDC" acronym, and the current "DEVCOM" branding are all the
    # same command, so they fold to one short. (The ARL anchor above still wins
    # for the "…Development Command Army Research Laboratory" strings.) Note the
    # unrelated Chinese "CCDC Drilling …" institute carries neither "us army" nor
    # "combat capabilities" and is handled by its own anchor far below.
    (['us army ccdc', 'combat capabilities development command', 'devcom'], 'DEVCOM'),
    ('hrl', 'HRL'),
    ('draper', 'Draper'),
    (['jhu/apl', 'johns hopkins applied physics'], 'JHU APL'),

    # ---- US elite private universities ------------------------------------
    (['massachusetts institute of technology', ', mit,', r're:\bmit,'], 'MIT'),
    # Tolerate the 'Insttute' (missing-i) misspelling seen in the ground
    # truth, consistent with the typo-absorbing philosophy above.
    (['re:california inst[i]?tute of technology', 'caltech'], 'Caltech'),
    ('stanford', 'Stanford'),
    ('harvard', 'Harvard'),
    ('princeton', 'Princeton'),
    ('yale', 'Yale'),
    ('cornell', 'Cornell'),
    (['columbia university', 'columbia,'], 'Columbia'),
    (['university of pennsylvania', 'upenn'], 'UPenn'),
    ('johns hopkins', 'Johns Hopkins'),
    ('duke university', 'Duke'),
    (['vanderbilt', 'vandertbilt'], 'Vanderbilt'),
    ('northwestern polytechnical', 'NWPU'),
    (['northwestern university', 'northwestern'], 'Northwestern'),
    (['northeastern university', 'northeastern'], 'Northeastern'),
    ('carnegie mellon', 'Carnegie Mellon'),
    ('rice university', 'Rice'),
    (['baylor university', 'baylor'], 'Baylor'),
    (['washington university', 'washu'], 'WashU'),
    ('tufts', 'Tufts'),
    ('tulane', 'Tulane'),
    ('emory', 'Emory'),
    ('university of chicago', 'U Chicago'),
    ('boston university', 'BU'),
    (['new york university', 'nyu'], 'NYU'),
    # Saint Louis University (the private Jesuit university), distinct from
    # University of Missouri-St. Louis (UMSL). Anchor on the full institution
    # name (not the bare city "St. Louis") so it can't fire on address lines.
    (['saint louis university', 'st. louis university', 'st louis university'], 'SLU'),

    # ---- UC system (specific campus before the generic word) --------------
    # The campus qualifier may be joined to "california" by a comma, the word
    # "at", a spaced hyphen ("University of California - Irvine"), or just a
    # space. _UC_SEP captures all of those so each campus needs only ONE anchor
    # and a stray separator variant can't fall through to the bare "UC" form.
    (r're:university of california' + _UC_SEP + r'berkeley', 'UC Berkeley'),
    ('uc berkeley', 'UC Berkeley'),
    (r're:university of california' + _UC_SEP + r'irvine', 'UC Irvine'),
    ('uc irvine', 'UC Irvine'),
    (r're:university of california' + _UC_SEP + r'riverside', 'UC Riverside'),
    ('uc riverside', 'UC Riverside'),
    (r're:university of california' + _UC_SEP + r'san diego', 'UC San Diego'),
    ('uc san diego', 'UC San Diego'),
    # La Jolla is UC San Diego's town; some strings give only the city
    # ("University of California, La Jolla") with no "San Diego" token, so pin
    # it explicitly before the bare "UC" fallback.
    (r're:university of california' + _UC_SEP + r'la jolla', 'UC San Diego'),
    (r're:university of california' + _UC_SEP + r'santa barbara', 'UC Santa Barbara'),
    (['uc santa barbara', 'ucsb'], 'UC Santa Barbara'),
    (r're:university of california' + _UC_SEP + r'davis', 'UC Davis'),
    ('uc davis', 'UC Davis'),
    # Tolerate the 'Califonia' (missing-r) misspelling seen in the input,
    # consistent with the typo-absorbing philosophy. Separator-tolerant like
    # the other campuses; must precede the bare "university of california"
    # fallback.
    (r're:university of califo[r]?nia' + _UC_SEP + r'los angeles', 'UCLA'),
    ('ucla', 'UCLA'),
    (r're:university of california' + _UC_SEP + r'merced', 'UC Merced'),
    (r're:university of california' + _UC_SEP + r'santa cruz', 'UC Santa Cruz'),
    ('university of california', 'UC'),  # fallback bare form
    (['university of southern california', ' usc,'], 'USC'),

    # ---- Other big US state schools ---------------------------------------
    # UMBC has irregular ground-truth treatment; most variants → UMBC, but
    # specific RAW_OVERRIDES preserve the verbatim/Maryland exceptions.
    # Separator-tolerant: "Baltimore County" (and the bare "Baltimore" form,
    # which in this dataset also refers to UMBC, not the UMB medical campus) is
    # joined to "maryland" by a space OR a comma. Must precede the bare
    # "university of maryland" anchor so it doesn't degrade to "Maryland".
    (r're:university of maryland' + _UC_SEP + r'baltimore', 'UMBC'),
    ('umbc', 'UMBC'),
    ([
        'laboratory for physical sciences, college park',
        'laboratory for telecommunication science', 'lps maryland',
    ], 'LPS Maryland'),
    # IREAP and the Institute for Physical Science and Technology are
    # both at Maryland College Park.
    ([
        'institute for research in electronics',
        'institute for physical science and technology', 'university of maryland',
    ], 'Maryland'),
    # University of Michigan-Dearborn and -Flint are separate campuses, not the
    # Ann Arbor flagship. Separator-tolerant and listed before the bare
    # "university of michigan" anchor so they don't degrade to "Michigan".
    (r're:university of michigan' + _UC_SEP + r'dearborn', 'UM-Dearborn'),
    (r're:university of michigan' + _UC_SEP + r'flint', 'UM-Flint'),
    ('university of michigan', 'Michigan'),
    # UT Austin campus. Separator-tolerant (_UC_SEP covers "at"/comma/space/
    # hyphen), so "...at Austin", "..., Austin", and the plain "Texas Austin"
    # form all land here instead of the fallback's "U Texas Austin". The "of"
    # is optional so the "University Texas at Austin" form (no "of", as some
    # sources print it, e.g. via the "Univ. Texas at Austin" abbreviation) also
    # hits this anchor instead of needing a per-string RAW_OVERRIDE.
    (r're:university (?:of )?texas' + _UC_SEP + r'austin', 'UT Austin'),
    ('ut austin', 'UT Austin'),
    # UT Dallas campus — listed before the bare flagship fallback below.
    (r're:university (?:of )?texas' + _UC_SEP + r'dallas', 'UT Dallas'),
    ('ut dallas', 'UT Dallas'),
    # Bare "University of Texas" / "University Texas" with NO campus qualifier
    # resolves to the flagship (UT Austin). Only Austin and Dallas campuses
    # appear in the data, and both are caught by the campus anchors above, so
    # this generic form is safe here as a last resort for the Texas system.
    (r're:university (?:of )?texas\b', 'UT Austin'),
    (['university of central florida', 'ucf,', 'creol'], 'UCF'),
    ('university of florida', 'Florida'),
    (['university of arizona', r're:univ\.? of arizona'], 'Arizona'),
    ('wyant college', 'Wyant College of Optical Sciences'),
    (['arizona state university', 'asu,'], 'ASU'),
    ('northern arizona university', 'Northern Arizona University'),
    (r're:university of colorado' + _UC_SEP + r'boulder', 'CU Boulder'),
    ('cu boulder', 'CU Boulder'),
    # CU Denver and CU Colorado Springs are separate campuses, not the Boulder
    # flagship. Separator-tolerant, before the bare "university of colorado".
    (r're:university of colorado' + _UC_SEP + r'denver', 'CU Denver'),
    (r're:university of colorado' + _UC_SEP + r'colorado springs', 'UCCS'),
    ('university of colorado', 'Colorado'),
    ('colorado school of mines', 'Colorado School of Mines'),
    (['university of washington', 'uw seattle'], 'UW Seattle'),
    # UW-Milwaukee is a separate campus; pin it before the flagship anchors so
    # it can't be swallowed by the bare "university of wisconsin" form below.
    (r're:university of wisconsin' + _UC_SEP + r'milwaukee', 'UW-Milwaukee'),
    # Flagship Madison campus → "Wisconsin" (preferred over "UW-Madison").
    # Covers the explicit "-Madison" form and the bare "University of Wisconsin"
    # (no other campus besides Milwaukee appears in the data, and that's caught
    # above), so the campus-less form resolves to the flagship.
    (r're:university of wisconsin' + _UC_SEP + r'madison', 'Wisconsin'),
    (['university of wisconsin', 'uw-madison'], 'Wisconsin'),
    ([
        'university of illinois urbana champaign',
        'university of illinois at urbana-champaign',
        'university of illinois urbana-champaign',
        'university of illinois at urbana champaign',
    ], 'UIUC'),
    # Misspelling guard: catch any "Illinois … Urbana … Champa{ign,gne,…}"
    # spelling (the data carries a "Urbana Champagne" typo) so it still lands on
    # UIUC instead of falling through to the fallback shortener. Requires both
    # "urbana" and a "champa…" token, so it can't fire on "University of
    # Illinois Chicago" or the bare "University of Illinois".
    ([r're:university of illinois.*\burbana\b.*\bchampa', 'university of illinois,', 'univ of illinois at urbana'], 'UIUC'),
    # UIC (Chicago campus) must precede the bare-Illinois fallback below so it
    # isn't swallowed by it. Covers the "at chicago" and bare "illinois chicago"
    # spellings and the "UIC," initialism.
    (['university of illinois at chicago', 'university of illinois chicago', 'uic,'], 'UIC'),
    # Bare "University of Illinois" (no campus qualifier) -> the flagship UIUC.
    # The Urbana-Champaign and Chicago campus anchors above already win for those
    # spellings; only the campus-less form reaches here.
    ('university of illinois', 'UIUC'),
    # The Beckman Institute for Advanced Science and Technology is UIUC's; its
    # affiliation strings often omit "University of Illinois", and the name is
    # unambiguous (distinct from Caltech's "Beckman Institute" and City of Hope's
    # "Beckman Research Institute"), so map the full phrase to UIUC.
    ('beckman institute for advanced science and technology', 'UIUC'),
    ('uiuc', 'UIUC'),
    ('purdue', 'Purdue'),
    ('university of minnesota', 'Minnesota'),
    ('michigan state', 'Michigan State'),
    ('michigan technological', 'Michigan Tech'),
    ('ohio state', 'Ohio State'),
    # Penn State. The main campus is "University Park, PA 16802", so some
    # affiliations name only the campus town / ZIP (e.g. an institute line like
    # "Materials Research Institute, University Park, PA 16802") without the
    # words "Penn State". Catch those campus forms here — up at the early anchor
    # position so they win over the generic bare "university park" anchor far
    # below (which would otherwise degrade them to "University Park"). The
    # "university park" needles are PA-qualified / ZIP-qualified so they don't
    # also grab the unrelated "University Park, TX" (SMU's town).
    (['penn state', 'pennsylvania state',
      'university park, pa', 'university park pa', 'university park, pennsylvania',
      '16802'], 'Penn State'),
    ('north carolina state', 'NC State'),
    (['university of north carolina at charlotte', 'university of north carolina charlotte', 'unc charlotte', 'univ of north carolina at charlotte'], 'UNC Charlotte'),
    (['north carolina agricultural and technical state', 'north caorlina agriculture and technology'], 'NC A&T'),
    (['georgia institute of technology', 'georgia tech'], 'Georgia Tech'),
    (['virginia polytechnic', 'virginia tech'], 'Virginia Tech'),
    (['university of virginia', ', uva,'], 'UVA'),
    (['university of pittsburgh', 'pittsburgh, '], 'Pittsburgh'),
    ('pennsylvania state university', 'Penn State'),
    (['rensselaer', 'rpi,'], 'RPI'),
    (['rochester institute of technology', ', rit,'], 'RIT'),
    (['university of rochester', 'university of rochester lle', 'institute of optics, university of rochester', 'the institute of optics, university of rochester', 'the institute of optics,', 'laboratory of laser and energetics'], 'Rochester'),
    ('sydor technologies', 'Sydor'),
    ('vpiphotonics', 'VPIphotonics'),
    ('photonect', 'Photonect'),
    ('texas a&m', 'Texas A&M'),
    ('texas tech', 'Texas Tech'),
    ('university of oklahoma', 'U Oklahoma'),
    ('university of arkansas', 'Arkansas'),
    # UAB (Birmingham) and UAH (Huntsville) are separate campuses, not the
    # Tuscaloosa flagship. They're joined by "at"/"in"/comma/hyphen, so allow
    # "in" in addition to the usual _UC_SEP separators. Listed before the bare
    # "university of alabama" flagship anchor.
    (r're:university of alabama(?:' + _UC_SEP + r'|\s+in\s+)birmingham', 'UAB'),
    (r're:university of alabama(?:' + _UC_SEP + r'|\s+in\s+)huntsville', 'UAH'),
    ('auburn', 'Auburn'),
    ('clemson', 'Clemson'),
    # UT Chattanooga is a separate campus from the Knoxville flagship.
    (r're:university of tennessee' + _UC_SEP + r'chattanooga', 'UT Chattanooga'),
    ('university of tennessee', 'U Tennessee'),
    ([
        'university of louisiana at lafayette',
        'university of louisiana lafayette',
    ], 'U Louisiana Lafayette'),
    # University of Missouri-Kansas City (UMKC) and -St. Louis (UMSL) are
    # separate campuses from the Columbia flagship.
    (r're:university of missouri' + _UC_SEP + r'kansas city', 'UMKC'),
    (r're:university of missouri' + _UC_SEP + r'st\.? louis', 'UMSL'),
    ('university of missouri', 'Missouri'),
    ('university of utah', 'Utah'),
    ('university of idaho', 'Idaho'),
    ('university of hawaii', 'Hawaii'),
    ('university of miami', 'U Miami'),
    ('delaware state', 'Delaware State'),
    ('university of north texas', 'U North Texas'),
    (['university of new mexico', 'unm,'], 'UNM'),
    ('umass amherst', 'UMass Amherst'),
    ('umass lowell', 'UMass Lowell'),
    ('umass boston', 'UMass Boston'),
    ('umass dartmouth', 'UMass Dartmouth'),
    # Separator-tolerant campus anchors (comma / space / spaced-hyphen forms all
    # appear, e.g. "University of Massachusetts-Amherst"). Each must precede the
    # bare "university of massachusetts" flagship so a hyphen/comma variant
    # can't degrade to plain "UMass".
    (r're:university of massachusetts' + _UC_SEP + r'amherst', 'UMass Amherst'),
    (r're:university of massachusetts' + _UC_SEP + r'lowell', 'UMass Lowell'),
    (r're:university of massachusetts' + _UC_SEP + r'boston', 'UMass Boston'),
    (r're:university of massachusetts' + _UC_SEP + r'dartmouth', 'UMass Dartmouth'),
    (['university of massachusetts', 'umass'], 'UMass'),
    # Dartmouth College — placed AFTER the UMass Dartmouth anchors above, so the
    # bare 'dartmouth' token can't hijack "UMass Dartmouth" (a different school).
    ('dartmouth', 'Dartmouth'),
    ('stony brook', 'SUNY Stony Brook'),
    (['university at albany', 'suny albany'], 'SUNY Albany'),
    # CUNY: all "CUNY" variants → CUNY per ground truth; the bare
    # "Physics and Astronomy, College of Staten Island, Staten Island, NY"
    # (no CUNY in string) maps to 'Staten Island' via the LATE anchor.
    ([
        'cuny advanced science research center', 'cuny,', 'cuny graduate center',
        ', cuny,', 'the graduate center,', 'graduate center cuny',
        'graduate center of the city university of new york',
        'city university of new york',
    ], 'CUNY'),
    ('city college of new york', 'CCNY'),
    ('rutgers', 'Rutgers'),
    ('stevens', 'Stevens'),
    ('syracuse', 'Syracuse'),
    ([
        'university of indiana', 'iu bloomington',
        'indiana university bloomington', 'indiana university,',
        'indiana university ',
    ], 'IU Bloomington'),
    ('oregon state university', 'Oregon State'),
    ('florida international university', 'FIU'),
    ('florida polytechnic university', 'Florida Polytechnic'),
    ('florida state university', 'FSU'),
    ('mcgill', 'McGill'),
    ('mcmaster', 'McMaster'),
    ('university of toronto', 'U Toronto'),
    ('university of ottawa', 'U Ottawa'),
    ('universite laval', 'Laval'),
    (['universite de montreal', 'university of montreal'], 'U Montreal'),
    ('university of waterloo', 'Waterloo'),
    ('university of alberta', 'U Alberta'),
    (['universite de sherbrooke', 'university of sherbrooke'], 'Sherbrooke'),
    ('institut national de la recherche scientifique', 'INRS'),
    ('inrs-emt', 'INRS-EMT'),
    (['inrs ', 'inrs,'], 'INRS'),
    ('university of calgary', 'Calgary'),
    ('simon fraser', 'SFU'),
    ('queens college', 'CUNY'),
    (r"re:queen's university", 'Queen’s University'),
    ('concordia', 'Concordia'),
    ('lakehead', 'Lakehead University'),
    ([
        'polytechnique montreal', 'ecole polytechnique de montreal',
    ], 'Polytechnique Montreal'),

    # ---- US "private mid-major" + research orgs ---------------------------
    ('boeing', 'Boeing'),
    # BAE Systems, incl. the "SMS" and spelled-out "Space and Missions Systems"
    # (Boulder, CO) sub-unit forms, all fold to the parent "BAE Systems".
    ('bae systems', 'BAE Systems'),
    (['apple inc', 'apple,'], 'Apple'),
    ('google', 'Google'),
    (['meta platforms', 'meta,', 'meta inc'], 'Meta'),
    ('microsoft', 'Microsoft'),
    ('amazon', 'Amazon'),
    (['intel ', 'intel,'], 'Intel'),
    ('nvidia', 'Nvidia'),  # most are lowercase nv...; existing map also has NVDIA typo - handle specifically below
    ('nvdia', 'NVDIA'),
    ('ibm', 'IBM'),
    ([
        'hewlett packard enterprise', 'hewlett packard labs', 'hpe labs belgium',
        'hpe labs', 'hpe,',
    ], 'HPE Labs'),
    ('hewlett-packard', 'HP'),
    ('cisco', 'Cisco'),
    ('nokia bell labs', 'Nokia Bell Labs'),
    ('bell labs', 'Bell Labs'),
    ('nokia', 'Nokia'),
    ('honeywell', 'Honeywell'),
    ('northrop grumman', 'Northrop Grumman'),
    (['coherent corp', 'coherent,'], 'Coherent'),
    ('thorlabs', 'Thorlabs'),
    ('newport', 'Newport'),
    ('corning', 'Corning'),
    ('amentum,', 'Amentum'),
    (['lumentum', 'mentum,'], 'Lumentum'),
    ('lam research', 'Lam Research Corporation'),
    ('thermo fisher', 'Thermo Fisher Scientific'),
    (['global foundries', 'globalfoundries'], 'GlobalFoundries'),
    ('broadcom', 'Broadcom'),
    # "Marvell Technologies (formerly Polariton Technologies AG)" — substring
    # anchor on the parent name catches both the bare "Marvell" form and the
    # parenthetical-former-name form.
    ('marvell', 'Marvell'),
    ('western digital', 'Western Digital Corporation'),
    ('stmicro', 'STMicro'),
    ('samsung', 'Samsung'),
    ('texas instruments', 'Texas Instruments'),
    ('ciena', 'Ciena'),
    ('tektronix', 'Tektronix'),
    ('accenture', 'Accenture'),
    ('amentum', 'Amentum'),
    ('mayo clinic florida', 'Mayo Clinic Florida'),
    ('johnson & johnson', 'Johnson & Johnson'),
    ('lightmatter', 'Lightmatter'),
    ('ayar labs', 'Ayar Labs Inc.'),
    ('openlight photonics', 'OpenLight Photonics'),
    ('lionix', 'LioniX International'),  # also LiX BV; handled later
    ('ligentec', 'Ligentec'),
    ('imra', 'IMRA'),
    ('toptica', 'Toptica'),
    ('menlo systems', 'Menlo Systems GmbH'),
    ('menhir', 'Menhir Photonics'),
    ('vescent', 'Vescent'),
    ('quantinuum', 'Quantinuum'),
    ('psiquantum', 'PsiQuantum'),
    ('ionq', 'IonQ'),
    ('xanadu', 'Xanadu'),
    ('quera', 'QuEra'),
    ('coldquanta', 'ColdQuanta'),
    ('vector atomic', 'Vector Atomic'),
    ('cablelabs', 'Cablelabs'),
    ('hamamatsu', 'Hamamatsu'),
    # Chi 3 Optics (Boulder, CO) shows up as "Chi 3 Optics", "Chi-3 Optics",
    # and "Chi3 Optics LLC". The hyphen is already folded to "-" by normalize(),
    # so one regex tolerating an optional space/hyphen between "chi" and "3"
    # catches all forms; canonical label uses the spaced form.
    (r're:chi[\s-]?3 optics', 'Chi 3 Optics'),
    (['center for microsystem technology', 'imec'], 'imec'),

    # ---- UK ----------------------------------------------------------------
    ('imperial college london', 'Imperial'),
    ('university of oxford', 'Oxford'),
    (['university of cambridge', 'cambridge university'], 'Cambridge'),
    (['university college london', ', ucl,'], 'UCL'),
    # normalize() folds curly apostrophes to ASCII, so one needle covers both
    # "King's" and "King’s".
    ('king\'s college london', 'King’s College London'),
    (['heriot-watt', 'heriot watt'], 'Heriot-Watt'),
    (['university of glasgow', 'glasgow university'], 'Glasgow'),
    (['university of strathclyde', 'strathclyde'], 'Strathclyde'),
    ('university of southampton', 'Southampton'),
    # The Optoelectronics Research Centre (ORC) is at the University of
    # Southampton; standalone mentions (no "University of Southampton" token)
    # fold to the same short.
    ('optoelectronics research centre', 'Southampton'),
    ('university of bristol', 'Bristol'),
    ('university of bath', 'Bath'),
    ('university of manchester', 'U Manchester'),
    ('cardiff', 'Cardiff'),
    # Aston: all Aston University / Aston Institute of Photonic Technologies
    # variants collapse to the single short label 'Aston'.
    (['aston institute of photonic', 'aston university', 'aston,'], 'Aston'),
    ('loughborough', 'Loughborough'),
    ('plymouth', 'Plymouth'),
    (['national physical lab', 'npl,'], 'NPL UK'),
    ('stfc', 'STFC'),
    ('epsrc centre for doctoral training in applied photonics', 'EPSRC CDT Photonics'),
    ('university hospital southampton', 'University Hospital Southampton'),
    ('nihr biomedical research', 'NIHR Biomedical Research Centre'),

    # ---- Ireland -----------------------------------------------------------
    (r're:trinity college du[bn]lin', 'Trinity College Dublin'),  # absorbs "Dunlin" misspelling
    ('university college cork', 'University College Cork'),
    ('university college dublin', 'University College Dublin'),
    ('tyndall', 'Tyndall'),

    # ---- Germany -----------------------------------------------------------
    (['max planck institute for the science of light', 'max-planck institute for the science of light', 'max-planck-inst physik des lichts'], 'MPI Light'),
    ('max planck institute of microstructure', 'MPI Microstructure'),
    (['max planck institute for multidisciplinary sciences',
      'multidisziplinare naturwissenschaften'], 'MPI Multidisc Sci'),
    ([
        'max-planck-institut fur quantenoptik',
        'max planck institute of quantum optics', 'mpq,',
    ], 'MPQ'),
    ('max plank for multidisciplinary sciences', 'MPI Multidisc Sci'),
    (['max born institute', 'max-born institute', 'max-born-institut'], 'Max Born'),
    ('max planck', 'Max Planck'),
    ('fraunhofer hhi', 'Fraunhofer HHI'),
    ('fraunhofer ilt', 'Fraunhofer ILT'),
    ('fraunhofer ims', 'Fraunhofer IMS'),
    ('fraunhofer iof', 'Fraunhofer IOF'),
    ('fraunhofer', 'Fraunhofer'),
    ('forschungszentrum julich', 'FZJ'),
    ('julich-aachen', 'Jülich-Aachen Research Alliance'),
    ('peter grunberg institute', 'FZJ'),
    ('helmholtz center dresden-rossendorf', 'Helmholtz Center Dresden-Rossendorf'),
    ('hzdr', 'HZDR'),
    # GSI (Darmstadt) — must be matched BEFORE the broad 'helmholtz' catch-all
    # below, which would otherwise swallow GSI's German name ("GSI
    # Helmholtzzentrum für Schwerionenforschung") and English name ("GSI
    # Helmholtz Centre for Heavy Ion Research") into 'Helmholtz Jena'.
    (['gsi helmholtz', 'schwerionenforschung'], 'GSI'),
    ('helmholtz', 'Helmholtz Jena'),
    ('rwth aachen', 'RWTH Aachen'),
    ([
        'lmu munich', 'ludwig-maximilians', 'ludwig maximilians',
        'ludwig maximilian university', 'ludwig-maximilian-universitat',
        # LIFE Center (Laser- und Immunologie-Forschungs-Einrichtungen Zentrum)
        # is an LMU Munich facility; the dashes are folded to "-" by normalize().
        'immunologie-forschungs-einrichtungen',
    ], 'LMU Munich'),
    ([
        'technical university of munich', 'technische universitat munchen',
        # "Technische Univ. München" arrives as "technische university munchen"
        # (Univ.->university folded, ü->u diacritic-stripped by normalize()).
        'technische university munchen',
        'tu munich',
    ], 'TU Munich'),
    ([
        'technische universitat berlin', 'technical university of berlin',
        'tu berlin',
    ], 'TU Berlin'),
    ([
        'technische universitat darmstadt', 'technical university of darmstadt',
        'tu darmstadt',
    ], 'TU Darmstadt'),
    (['technische universitat dresden', 'tu dresden'], 'TU Dresden'),
    (['karlsruhe institute of technology', 'karlsruher institut fur technologie', 'kit,'], 'KIT'),
    ([
        'humboldt-universitat', 'humboldt universitat', 'humboldt university',
    ], 'Humboldt'),
    ([
        'friedrich-schiller-universitat jena', 'friedrich-schiller',
        'friedrich schiller university jena', 'friedrich schiller university,',
        'friedrich schiller',
    ], 'Jena'),
    ('iap jena', 'IAP Jena'),
    ('university of stuttgart', 'Stuttgart'),
    ('institute for microelectronics stuttgart', 'IMS CHIPS'),
    ('si stuttgart instruments', 'SI Stuttgart Instruments GmbH'),
    ('siloriX', 'SilOriX'),
    ('university of gottingen', 'Göttingen'),
    ('university of mainz', 'Mainz'),
    ('regensburg center for ultrafast', 'Regensburg'),
    ([
        'university of hannover', 'leibniz university hannover',
        'leibniz universitat hannover',
    ], 'Leibniz U Hannover'),
    ('leibniz-institut fur oberflachenmodifizierung', 'IOM'),
    # IFW Dresden — Leibniz Institute for Solid State and Materials Research
    # (German "Leibniz-Institut für Festkörper- und Werkstoffforschung"). Cover
    # the English and German names plus the standard "IFW Dresden" acronym.
    ([
        'leibniz institute for solid state and materials research',
        'festkorper- und werkstoffforschung', 'ifw dresden',
    ], 'IFW Dresden'),
    ('cluster of excellence phoenixd', 'Cluster of Excellence PhoenixD'),
    ('laser zentrum hannover', 'Laser Zentrum Hannover e.V'),
    (['universitat hamburg', 'university of hamburg'], 'Universitat Hamburg'),
    (['european xfel', 'european x-ray free electron laser', 'eu xfel'], 'European XFEL'),
    ('xfel', 'XFEL'),
    (['deutsches elektronen-synchrotron', 'desy'], 'DESY'),
    ('cycle gmbh', 'Cycle GmbH'),
    ('picoquant', 'PicoQuant GmbH'),
    ('swabian instruments', 'Swabian Instruments'),
    ('toptica photonics', 'Toptica'),
    ('trumpf', 'Trumpf'),
    ('bosch', 'Bosch'),
    ('marvel fusion', 'MARVEL Fusion GmbH'),
    ('mpi corporation', 'MPI Corporation'),
    ('mpi light', 'MPI Light'),
    (['weierstraß-institut', 'weierstrass-institut', 'weierstrass institute', 'wias berlin'], 'WIAS Berlin'),
    ('paderborn', 'Paderborn'),
    # German city Münster (accent-folded to "munster"), but NOT Ireland's
    # "Munster Technological University" (Cork) — the bare substring would grab
    # it once accents are folded, so exclude that one specific institution and
    # let it fall through to the fallback shortener, which keeps its full name.
    (['re:munster(?! technological)', 'university of munster', 'westfalische wilhelms'], 'Münster'),
    ('saot', 'SAOT Erlangen'),
    # FAU Erlangen-Nürnberg: all Friedrich-Alexander spellings (hyphen,
    # en-dash, no-hyphen, English "Erlangen-Nuremberg") collapse to 'FAU'.
    # normalize() folds the en-dash to an ASCII hyphen, so the hyphen needle
    # below covers the en-dash spelling too; the no-hyphen form still needs its
    # own needle.
    # SAOT (graduate school) and MPI Light, both in Erlangen, are distinct
    # institutions handled by their own anchors above/below and are untouched.
    (['friedrich-alexander', 'friedrich alexander', 'fau,'], 'FAU'),
    ('ihp gmbh', 'IHP'),
    ('chemnitz', 'TU Chemnitz'),
    ('brandenburgische technische', 'BTU Cottbus'),
    ('rheinland-pfalzische', 'RPTU'),
    ([
        'christian-albrechts-universitat', 'christian-albrechts',
        'kiel university', 'university of kiel',
    ], 'Kiel'),
    ('otto-von-guericke', 'Otto-von-Guericke-Universitat Magdeburg'),
    (['fbh', 'ferdinand-braun-institut'], 'FBH'),
    (['physikalisch-technische bundesanstalt', 'physikalisch-technsche'], 'PTB'),
    ('cluster of excellence', 'Cluster of Excellence PhoenixD'),
    (['dlr,', 'german aerospace center'], 'DLR'),
    ('deeplight', 'DeepLight S.A./GmbH'),

    # ---- Switzerland -------------------------------------------------------
    ('eth zurich', 'ETH Zürich'),
    # Bare "ETH" is always ETH Zürich — there's no other ETH worth
    # distinguishing. Word-boundary regex so it doesn't fire inside
    # "Bethesda", "Methodist", etc.
    (r're:\beth\b', 'ETH Zürich'),
    ('eidgenossische technische hochschule', 'ETH Zürich'),
    # All of these are EPFL in Lausanne, Switzerland — distinct from
    # France's École Polytechnique (Paris) and Polytechnique Montréal.
    ([
        'ecole polytechnique federale de lausanne',
        'swiss federal institute of technology lausanne',
        'swiss federal institute of technology, lausanne',
        'swiss federal technology institute of lausanne', 'epfl',
    ], 'EPFL'),
    (['paul scherrer', ', psi,'], 'PSI'),
    # Bern University of Applied Sciences = Berner Fachhochschule, standard
    # short name BFH. Distinct from University of Bern ('U Bern'); match the
    # full applied-sciences phrase so the two never conflate.
    (['bern university of applied sciences', 'berner fachhochschule'], 'BFH'),
    (['university of neuchatel', 'universite de neuchatel'], 'Neuchâtel'),
    ('centre suisse d', 'CSEM'),
    # IT'IS Foundation (Zurich) — Foundation for Research on Information
    # Technologies in Society.
    ('foundation for research on information technologies in society', "IT'IS"),
    (['empa,', r're:,\s*empa\b'], 'Empa'),
    ('lumiphase', 'Lumiphase AG'),
    ('lightium', 'Lightium AG'),
    ('enlightra', 'Enlightra'),

    # ---- France ------------------------------------------------------------
    ('institut d\'optique', 'Institut d\'Optique'),
    ('institut fresnel', 'Institut Fresnel'),
    # C2N (Centre de Nanoscience et Nanotechnologie) is at Paris-Saclay. Must
    # come BEFORE the generic paris-saclay anchor, since the C2N strings carry
    # "Université Paris-Saclay" in the address and would otherwise degrade to
    # plain "Paris-Saclay". The "(c2n)" token is distinctive to these strings.
    # Match both singular ("Nanotechnologie") and plural ("Nanotechnologies"),
    # the French ("Centre de … et [de] …") and English ("Center of … and …")
    # spellings, and the connecting "de"/"of" variants. All appear in the
    # source data (e.g. "Center of nanosciences and nanotechnologies, CNRS,
    # Univ. Paris Saclay" alongside the French forms).
    ([r"re:cent(?:re|er) (?:de|of) nanoscience(?:s)? (?:et|and) (?:de )?nanotechnologie(?:s)?",
      '(c2n)'], 'C2N Paris-Saclay'),
    # Accept both the hyphenated official form ("Paris-Saclay") and the space-
    # separated variant ("Paris Saclay") that appears in some affiliation strings
    # (e.g. "Université Paris Saclay" without the hyphen).
    (['universite paris-saclay', 'paris-saclay',
      'universite paris saclay', 'paris saclay'], 'Paris-Saclay'),
    # The "Laboratoire de Physique de l'ENS / de l'École Normale Supérieure"
    # (LPENS) is the ENS physics department. Its affiliation strings list many
    # co-tutelles (ENS, PSL, CNRS, Sorbonne, Université Paris Cité) in varying
    # order, so without this specific anchor the generic Paris-Cité / Sorbonne /
    # CNRS anchors below would catch it inconsistently. Map the lab itself to
    # "ENS Paris". Placed before those generic anchors so it wins.
    (r"re:laboratoire de physique de l'(ens|ecole normale superieure)",
     'ENS Paris'),
    (['universite paris cite', 'universite de paris', 'paris cite'], 'U Paris Cité'),
    ('sorbonne', 'Sorbonne'),
    # Grenoble Alpes must come BEFORE CEA-LETI so combined strings with both
    # are attributed to Grenoble Alpes per ground truth. The "Univ." dotted
    # abbreviation is folded to "university" upstream in normalize(), so a
    # single "university grenoble alpes" anchor covers both spellings; the
    # no-dot "Univ Grenoble Alpes" form (untouched by the folder) keeps its
    # own anchor.
    ([
        'universite grenoble alpes', 'univ grenoble alpes',
        'university grenoble alpes', 'university of grenoble',
    ], 'Grenoble Alpes'),
    # CEA: combined CEA-Leti + CEA strings → CEA; pure CEA-LETI alone → CEA-Leti.
    ('cea-leti, cea,', 'CEA'),
    ('cea-leti', 'CEA-Leti'),
    (['cea,', 'cea-saclay'], 'CEA'),
    ('insa lyon', 'INSA Lyon'),
    # Université Marie et Louis Pasteur (Besançon) — formed 2025 from the
    # merger of UFC and UTBM. Must run BEFORE the generic 'cnrs' anchor so
    # joint strings like "Universite Marie et Louis Pasteur and CNRS" map to
    # UMLP rather than being swallowed by CNRS.
    ('marie et louis pasteur', 'UMLP'),
    # University of Burgundy (recently renamed "Université Bourgogne Europe")
    # and its physics lab "Laboratoire Interdisciplinaire Carnot de Bourgogne"
    # (ICB UMR 6303) all fold into 'Bourgogne'. These MUST precede the generic
    # 'cnrs' (and 'dijon') anchors so a joint string like "Université Bourgogne
    # Europe, CNRS, Laboratoire ... de Bourgogne" maps to Bourgogne, not CNRS.
    (['universite de bourgogne europe', 'universite bourgogne europe', 'universite de bourgogne', 'laboratoire interdisciplinaire carnot de bourgogne'], 'Bourgogne'),
    ('cnrs', 'CNRS'),
    ('ecole normale superieure', 'ENS Paris'),
    # ULB's engineering faculty is literally named "École Polytechnique de
    # Bruxelles", so its affiliation strings contain "Ecole Polytechnique".
    # This must run BEFORE the generic Paris "ecole polytechnique," anchors
    # below, or the Brussels institution gets mislabelled "Polytechnique".
    ([
        're:ecole polytechnique.*libre de bruxelles',
        're:ecole polytechnique.*universite libre de bruxelles',
    ], 'ULB'),
    (['ecole polytechnique,', 'institut polytechnique de paris'], 'IP Paris'),
    ('universite de lyon', 'U Lyon'),
    ('ecole centrale de lyon', 'Ecole Centrale de Lyon'),
    (['universite de lille', 'university of lille'], 'Lille'),
    ('universite de toulouse', 'U Toulouse'),
    ('universite de montpellier', 'Université de Montpellier'),
    (['universite de limoges', 'university of limoges'], 'Université de Limoges'),
    ('universite cote d', 'Université Cote d\'Azur'),
    (['universite de dijon', 'dijon'], 'Dijon'),
    ('xlim', 'XLIM'),
    ('iii-v lab', 'III-V Lab'),
    ('amplitude laser', 'Amplitude Laser'),
    ('fastlite', 'Fastlite by Amplitude'),
    (['luli', 'laboratoire pour l'], 'LULI'),
    ('lpgp', 'Paris-Saclay'),  # gas-discharge lab at Saclay
    ('thales', 'Thales'),
    ('centre national de la recherche scientifique', 'CNRS'),
    ('exail', 'Exail'),
    ('exail,', 'EXAIL'),

    # ---- Italy -------------------------------------------------------------
    # ("Politechnico" misspelling is folded to "Politecnico" in normalize().)
    ('politecnico di milano', 'PoliMi'),
    ('politecnico di torino', 'PoliTo'),
    # PoliBa (Politecnico di Bari), distinct from Università di Bari ("Bari").
    ([
        'politecnico di bari', 'polytechnic university of bari',
    ], 'PoliBa'),
    (["scuola superiore sant'anna", "sant'anna"], "Scuola Superiore Sant'Anna"),
    ('sapienza', 'Sapienza'),
    ('universita cattolica del sacro cuore', 'Università Cattolica del Sacro Cuore'),
    ('universita nicolo cusano', 'Università Nicolò Cusano'),
    # Italian "Università della Calabria" and the English "University of
    # Calabria" are the same institution — fold both to one short.
    (['universita della calabria', 'university of calabria'], 'Calabria'),
    (['universita di trento', 'university of trento'], 'University of Trento'),
    # Florence. Cover the English name and the Italian "Università di Firenze"
    # plus the "Università degli Studi di Firenze" long form (the "degli studi di
    # firenze" needle is robust to the "Univ." abbreviation, which normalize()
    # folds to "university degli studi di firenze").
    (['university of florence', 'universita di firenze',
      'degli studi di firenze', 'university, florence'], 'Florence'),
    ([
        'university of pavia', 'universita degli studi di pavia',
        'universita di pavia', 'universita pavia',
    ], 'Pavia'),
    (['universita di brescia', 'university of brescia'], 'Brescia'),
    ([
        'university of padua', 'university of padova', 'universita di padova',
        'universita degli studi di padova',
    ], 'Padua'),
    (['universita di ferrara', 'university of ferrara'], 'Ferrara'),
    (['universita di cagliari', 'university of cagliari'], 'Cagliari'),
    # (Università della Campania is anchored later as 'UniCampania' — search
    # for "Luigi Vanvitelli" below.)
    ('istituto di fotonica e nanotecnologie', 'Istituto di Fotonica e Nanotecnologie'),
    ('cnit', 'CNIT'),
    ([
        'consiglio nazionale delle ricerche', 'cnr,',
        'national research council (cnr)',
    ], 'CNR Italy'),
    (['national institute of optics-national research council', 'cnr-ino'], 'CNR-INO'),
    ('sezione di perugia', 'Sezione di Perugia'),
    ('sezione di roma', 'Sezione di Roma'),
    ('osservatorio astrofisico di catania', 'Osservatorio Astrofisico di Catania'),
    ('enrico fermi research center', 'Enrico Fermi Research Center (CREF)'),
    ('university of l\'aquila', 'University of L\'Aquila'),

    # ---- Spain -------------------------------------------------------------
    (['icfo', 'institute of photonic sciences'], 'ICFO'),
    (['universitat politecnica de catalunya', 'upc,'], 'UPC'),
    ('universitat politecnica de valencia', 'Universitat Politecnica de Valencia'),
    ('universidad politecnica de madrid', 'Universidad Politecnica de Madrid'),
    ('universidad complutense de madrid', 'Complutense Madrid'),
    (['csic,', 'consejo superior de investigaciones'], 'CSIC'),
    ('instituto de ciencia de materiales de madrid', 'ICMM'),
    ('university of vigo', 'University of Vigo'),
    ('universitat rovira', 'URV'),
    # Spanish "Universidad de Almería" and English "University of Almería" are
    # the same institution — fold both to one short.
    (['universidad de almeria', 'university of almeria'], 'Almería'),
    # University of Zaragoza. The Aragón Institute of Engineering Research (I3A)
    # is a research institute hosted there; its strings carry "Universidad de
    # Zaragoza" after the institute name, but the comma-based fallback would keep
    # the leading institute segment, so anchor the university (Spanish + English)
    # and the institute name itself to the bare place name.
    (['universidad de zaragoza', 'university of zaragoza',
      'investigacion en ingenieria de aragon'], 'Zaragoza'),
    ('eurecat', 'Eurecat'),
    ('donostia international physics center', 'Donostia International Physics Center'),
    ('radiantis', 'Radiantis'),
    ('microliquid', 'Microliquid'),

    # ---- Portugal ----------------------------------------------------------
    ('instituto de telecomunicacoes', 'Instituto de Telecomunicações'),
    ('instituto de plasmas e fusao nuclear', 'Instituto de Plasmas e Fusão Nuclear'),
    ('instituto superior tecnico', 'IST Lisbon'),
    (['ciceco', 'university of aveiro', 'universidade de aveiro'], 'Aveiro'),
    (['university of porto', 'porto university', 'universidade do porto'], 'Porto'),
    (['university of lisbon', 'universidade de lisboa'], 'Lisbon'),
    (['instituto de engenharia de sistemas e computadores', 'inesc mn'], 'INESC MN'),
    ('sphere ultrafast', 'Sphere Ultrafast Photonics'),
    ('glophotonics', 'GLOphotonics'),

    # ---- Netherlands -------------------------------------------------------
    (['eindhoven university of technology', 'tu eindhoven'], 'TU Eindhoven'),
    (['delft university of technology', 'tu delft'], 'TU Delft'),
    ('university of twente', 'Twente'),
    (['university of amsterdam', 'university amsterdam'], 'Amsterdam'),
    ('institute: amsterdam medical', 'Amsterdam UMC'),
    ('photon design', 'Photon Design'),
    ('lionix bv international', 'LioniX International'),

    # ---- Belgium -----------------------------------------------------------
    ('ku leuven', 'KU Leuven'),
    ('universite libre de bruxelles', 'ULB'),
    ('vrije universiteit brussel', 'VUB'),
    ('ulb,', 'ULB'),
    # Ghent University, incl. the Dutch name ("Universiteit Gent"), the
    # "Gent University" half-translation, and the abbreviated "Univ. Gent" form
    # (which normalize() folds to "university gent"), all map to the English
    # short. (The University Hospital "UZ Gent" is a separate body and keeps its
    # own label — it carries neither "university" nor "ghent".)
    (['ghent university', 'ugent', 'universiteit gent', 'gent university',
      'university gent'], 'Ghent'),
    ('intec,', 'INTEC'),

    # ---- Nordics -----------------------------------------------------------
    ([
        'technical university of denmark', 'danmarks tekniske universitet',
        'dtu copenhagen',
    ], 'DTU'),
    (['danish national metrology institute', 'danish fundamental metrologi'], 'DFM'),
    (['dtu electro', 'dtu,', 'dtu:'], 'DTU'),
    ('nkt photonics', 'NKT Photonics'),
    ('uv medico', 'UV Medico'),
    (['niels bohr institute', 'university of copenhagen'], 'Copenhagen'),
    ('sparrow quantum', 'Sparrow Quantum ApS'),
    # KTH, incl. the Swedish name "Kungliga Tekniska Högskolan" (diacritics
    # already folded by normalize()).
    (['royal institute of technology', 'kth royal institute', 'kth,', 'kungliga tekniska'], 'KTH'),
    ('chalmers', 'Chalmers'),
    ('linkoping', 'Linköping'),
    ('rise research institutes', 'RISE Research Institutes of Sweden'),
    ('aalto', 'Aalto'),
    # VTT Technical Research Centre of Finland (the "Ltd."/"Oy" legal suffix and
    # the Centre/Center spelling both vary); the bare "VTT" acronym is the short.
    ([r're:\bvtt\b', r're:technical research cent(?:re|er) of finland'], 'VTT'),
    ('vexlum', 'Vexlum Oy'),
    ('university of turku', 'U Turku'),
    ('university of jyvaskyla', 'U Jyväskylä'),

    # ---- Austria -----------------------------------------------------------
    ([
        'tu wien', 'tu vienna', 'technische universitat wien',
        # the "Univ." abbreviation folds to "university" in normalize(), so the
        # German "Technische Univ. Wien" arrives as "technische university wien".
        'technische university wien',
        'vienna university of technology',
    ], 'TU Vienna'),
    (['tu graz', 'graz university of technology'], 'TU Graz'),
    ('johannes kepler', 'Johannes Kepler University'),
    ('iqoqi', 'IQOQI'),
    (['ist austria', 'institute of science and technology austria'], 'IST Austria'),
    ('silicon austria labs', 'Silicon Austria Labs'),

    # ---- Eastern Europe ----------------------------------------------------
    ('czech technical university', 'Czech TU Prague'),
    ('uct prague', 'UCT Prague'),
    ('charles university', 'Charles U Prague'),
    ('czech academy', 'Czech Academy'),
    ('fnspe', 'FNSPE'),
    (['eli beamlines', 'eli-beamlines'], 'ELI-Beamlines'),
    (['eli-alps', 'eli alps'], 'ELI-ALPS'),
    ('cesnet', 'CESNET'),
    ('hilase', 'HiLASE Centre'),
    ('palacky university', 'Palacky University'),
    ('alexander dubcek', 'Alexander Dubček University of Trenčín'),
    ('jozef stefan', 'Jozef Stefan Institute'),
    # University of Warsaw, incl. the Polish name "Uniwersytet Warszawski".
    (['university of warsaw', 'uniwersytet warszawski'], 'Warsaw U'),
    ('warsaw university of technology', 'Warsaw UT'),
    # Łukasiewicz Institute of Microelectronics and Photonics (IMiF, Poland).
    # 2025 strings appear as "Łukasiewicz Research Network, Institute of
    # Microelectronics and Photonics" or the bare "Institute of Microelectronics
    # and Photonics" — neither contains "lukasiewicz institute of
    # microelectronics". Cover all forms here, and BEFORE both the bare
    # 'warsaw,' city anchor (so the ", Warsaw, Poland" address-tailed variants
    # don't degrade to 'Warsaw') and the Singapore A*STAR "institute of
    # microelectronics" anchor further below (so they aren't mislabelled A*STAR).
    ([
        'lukasiewicz institute of microelectronics',
        'lukasiewicz research network',
        'institute of microelectronics and photonics',
    ], 'Lukasiewicz IMiF'),
    ('warsaw,', 'Warsaw'),
    # Wroclaw University of Science and Technology -> Wroclaw. Match the full
    # institution phrase so the separate "Gekko Photonics, Wroclaw" company
    # (which only carries the CITY token) is never swept up. Cover the
    # accented "Wrocław" spelling too.
    ([
        'wroclaw university of science and technology',
        'wrocław university of science and technology',
    ], 'Wroclaw'),
    ('lodz university of technology', 'Lodz University of Technology'),
    ([
        'uniwersytet mikolaja kopernika', 'nicolaus copernicus',
    ], 'Nicolaus Copernicus'),
    ('polish academy', 'Polish Academy'),
    # FTMC's English name; place before bare 'vilnius,' so it wins for the
    # address-bearing variant ("..., Vilnius, Lithuania") too.
    ('center for physical sciences', 'FTMC Vilnius'),
    ('vilnius,', 'Vilnius'),
    ([
        'state research institute center for physical sciences', 'ftmc,',
    ], 'FTMC Vilnius'),
    ('university of ss. cyril and methodius in trnava', 'UCM Trnava'),
    ('slovak centre of scientific', 'SCSTI Slovakia'),
    ('iict', 'IICT'),
    ('national hellenic research', 'National Hellenic Research Foundation'),
    ('aristotle', 'Aristotle'),
    ('thessaloniki', 'Thessaloniki'),
    ('university of athens', 'U Athens'),
    ('eulambia', 'Eulambia Advanced Technologies'),
    ('izmir institute of technology', 'Izmir Institute of Technology'),
    # Koç University, Istanbul (Turkish "Koç Üniversitesi"; the ç/ü are folded by
    # normalize(), so the ASCII needle covers both spellings).
    (['koc universitesi', 'koc university'], 'Koç'),
    (['metu', 'middle east technical'], 'METU'),

    # ---- Israel ------------------------------------------------------------
    ('technion', 'Technion'),
    ('weizmann', 'Weizmann'),
    (['tel aviv university', 'tel-aviv university'], 'TAU'),
    (['hebrew university', 'hebrew universit', 'hebrew univ'], 'Hebrew U'),
    (['ben-gurion', 'ben gurion'], 'Ben-Gurion'),
    (['bar-ilan', 'bar ilan'], 'Bar-Ilan'),
    ('ariel university', 'Ariel U'),
    ('soreq nrc', 'Soreq NRC'),
    ('hadassah-hebrew-university', 'Hebrew U'),
    ('civan lasers', 'Civan Lasers'),
    ('cognifiber', 'Cognifiber'),
    ('ephos', 'Ephos'),

    # ---- Russia / former Soviet --------------------------------------------
    (['a. f. ioffe', 'a.f. ioffe', 'ioffe institute', 'ioffe'], 'Ioffe'),
    ('a.v. rzhanov institute', 'Rzhanov ISP'),
    ('lebedev physical institute', 'Lebedev Physical Institute'),
    (['mipt', 'moscow institute of physics and technology'], 'MIPT'),
    (['moscow state university', 'lomonosov moscow'], 'Moscow State'),
    ('tomsk state university of control systems', 'TUSUR'),
    ('v.e. zuev institute', 'Zuev Institute'),
    ('kutateladze inst', 'Kutateladze Institute'),
    ('russian quantum', 'Russian Quantum Ctr'),
    ('russian academy of science', 'RAS'),
    ('nas ra institute of chemical physics', 'NAS RA Institute of Chemical Physics'),

    # ---- China: top universities (specific city/name BEFORE generic) ------
    # Many Chinese universities have multiple full-name spellings and abbrev.
    # HUST's Shenzhen satellite ("Shenzhen Huazhong University of Sci. and
    # Technol. Research Institute") is a distinct branch from the Wuhan
    # flagship — same parent name, different city, separate institute. Pin
    # the Shenzhen variant first so the bare-Huazhong anchor below doesn't
    # swallow it the way it would otherwise.
    ('shenzhen huazhong', 'HUST Shenzhen'),
    ([
        'huazhong university of scien', 'huazhong univ of science',
        'huazhong university of sci', 'hust,',
    ], 'Huazhong'),
    ('wuhan national lab for optoelectronic', 'Wuhan National Lab for Optoelectronics'),
    (['tsinghua university', 'tsinghua,'], 'Tsinghua'),
    ([
        'beijing national research center for information science and technology',
        'beijing national research center for information and technology',
        'bnrist',
    ], 'BNRist'),
    # "Peking University Yangtze Delta Institute of Optoelectronics" is a
    # separate campus/institute (Nantong, Jiangsu) and keeps its own label;
    # it must be matched BEFORE the generic "peking universit" anchor.
    (['peking university yangtze delta', 'peking universitity yangtze delta'], 'Peking U Yangtze Delta'),
    (['peking university', 'peking universit', 'pekin university', 'pku'], 'Peking U'),
    (['beijing institute of technology', 'bit,'], 'BIT'),
    ('beihang', 'Beihang'),
    (['beijing university of posts and telecomm', 'bupt,'], 'BUPT'),
    (['beijing normal university', 'bnu,'], 'BNU'),
    ('beijing univ of posts', 'BUPT'),
    ('fudan', 'Fudan'),
    (['shanghai jiao tong', 'shanghai jiaotong', 'sjtu,'], 'SJTU'),
    ('sjtu-pinghu institute', 'SJTU-Pinghu'),
    ('shanghaitech', 'ShanghaiTech'),
    ('shanghai university,', 'Shanghai'),
    # CAS sub-institutes are split by campus/institute rather than collapsed to
    # a single "CAS". Each institute is detected whether or not the string also
    # carries "Chinese Academy of Sciences" / "CAS", so these run BEFORE the
    # generic 'chinese academy of sciences' anchor below. Bare strings that name
    # no specific institute fall through to that generic anchor and stay 'CAS'.
    #   IOP   Institute of Physics, Beijing (+ Beijing Natl Lab for Condensed Matter)
    #   IOS   Institute of Semiconductors, Beijing
    #   IME   Institute of Microelectronics, Beijing (CAS — distinct from A*STAR IME)
    #   ICT   Institute of Computing Technology, Beijing
    #   AIR   Aerospace Information Research Inst / Natl Key Lab of Microwave Imaging
    #   SIMIT Shanghai Institute of Microsystem and Information Technology
    #   SIOM  Shanghai Institute of Optics and Fine Mechanics
    #   FJIRSM Fujian Institute of Research on the Structure of Matter, Fuzhou
    #   XIOPM Xi'an Institute of Optics and Precision Mechanics
    #   UCAS  University of Chinese Academy of Sciences (the CAS-affiliated university)
    ([
        'beijing national laboratory for condensed matter physics',
        'institute of physics, cas', 'institute of physics, chinese',
    ], 'CAS IOP Beijing'),
    # "Institute of Physics, Beijing, ..." (no "CAS"/"Chinese Academy" token,
    # only the city) is the CAS Institute of Physics in Beijing — confirmed by
    # its co-affiliation with UCAS in the data. Anchor the city-tailed form
    # specifically; IoP *departments* of named universities (EPFL, Mainz,
    # Amsterdam, Belgrade, …) carry their parent's name and resolve via that
    # parent's anchor/the fallback, so they are unaffected. The bare,
    # location-less "Institute of Physics" alt-name is pinned via RAW_OVERRIDES.
    ('institute of physics, beijing', 'CAS IOP Beijing'),
    # Institute of Semiconductors at CAS (Beijing). Require the CAS context
    # ("Chinese Academy" or ", CAS") so the same-named institute at the *Henan*
    # Academy of Sciences is NOT swept in (it stays 'Henan Academy of Sciences').
    ([
        'institute of semiconductors, chinese academy',
        'institute of semiconductors,chinese academy',
        'institute of semiconductors, cas',
    ], 'CAS IOS Beijing'),
    ([
        'institute of microelectronics, chinese academy',
        'institute of microelectronics of the chinese academy',
    ], 'CAS IME Beijing'),
    ('institute of computing technology, chinese academy', 'CAS ICT Beijing'),
    ([
        'aerospace information research institute',
        'national key laboratory of microwave imaging',
    ], 'CAS AIR Beijing'),
    ('shanghai institute of microsystem', 'SIMIT'),
    ('shanghai institute of optics and fine mechanics', 'SIOM'),
    ('fujian institute of research on the structure of matter', 'FJIRSM'),
    (r"re:xi'an institute of optics and precision mechanics", 'CAS XIOPM'),
    (['university of chinese academy', 'niversity ofchinese academy'], 'UCAS'),
    ('shanghai institute of ceramics', 'Shanghai Institute of Ceramics'),
    ('shanghai engineering research center of energy efficient', 'SERC-EECAI Shanghai'),
    ('siom', 'SIOM'),
    (['zhejiang university', 'zju-hangzhou'], 'Zhejiang'),
    ('zhejiang lab', 'Zhejiang Lab'),
    ('nanjing university of aeronautics', 'Nanjing U Aeronautics & Astronautics'),
    ('nanjing university of posts and telecommunications', 'NUPT'),
    ('southeast university', 'Southeast U'),
    ('purple mountain lab', 'Purple Mountain Lab'),
    ('nankai', 'Nankai'),
    (["xi'an jiaotong", 'xian jiaotong'], "Xi'an Jiaotong"),
    ('xidian', 'Xidian'),
    # NOTE: no bare-city "xi'an," anchor here. It was dead code before
    # normalize() folded punctuation (the source data's curly apostrophe in
    # "Xi’an," never matched an ASCII needle), and once the fold makes it live
    # it does the wrong thing — matching the CITY in an address tail and
    # clobbering the real institution (e.g. "QXP Technology Inc, Xi'an, China"
    # -> "Xi'an"). Same bare-city mistake the Rochester/Sydor LATE anchors were
    # removed for; specific Xi'an institutions (Jiaotong, Xidian, XIOPM) have
    # their own anchors above, and everything else should fall through to the
    # fallback shortener, which keeps the leading institution name.
    ('nwpu', 'NWPU'),
    (['university of electronic science and technology of china', 'university of electronic science and technology', 'university electronic sci. & tech. of china', 'univ of electronic science & tech china', 'uestc'], 'UESTC'),
    ([
        'university of science and technology of china',
        'university of science and technology of chin,', 'ustc,',
    ], 'USTC'),
    ([
        'chinese academy of sciences', 'chinese academy of science',
        'chinese academic of science',
    ], 'CAS'),
    ('chinese academy of medical sciences', 'CAMS-PUMC'),
    ('south china normal', 'South China Normal University'),
    (['south china university of technology', 'scut,'], 'SCUT'),
    ('south china academy of advanced opto', 'SCAAO'),
    (['sun yat-sen', 'sun yat sen'], 'Sun Yat-sen U'),
    ('shenzhen technology', 'Shenzhen Tech U'),
    (['southern university of science and technology', 'sustech'], 'SUSTech'),
    # HK: order matters — more-specific first.
    (['hong kong university of science and technology', 'hkust'], 'HKUST'),
    (['city university of hong kong', 'city university hong kong'], 'CityU HK'),
    ([
        'chinese university of hong kong (shenzhen)',
        'the chinese university of hong kong (shenzhen)',
        'chinese university of hong kong, shenzhen',
    ], 'CUHK Shenzhen'),
    (['the chinese university of hong kong', 'chinese university of hong kong', r're:chinese univ\w*rsity of hong kong'], 'CUHK'),
    ('cuhk shenzhen', 'CUHK Shenzhen'),
    ('cuhk,', 'CUHK'),
    (['hong kong polytechnic', 'hong kong polytechinic', 'the hong kong polytechnic', 'the hong kong polytechinic'], 'PolyU HK'),
    ('hong kong baptist', 'HK Baptist'),
    ([
        'the university of hong kong', 'university of hong kong',
        'the university of hongkong', 'hku,',
    ], 'HKU'),
    ('pui ching middle school macau', 'Pui Ching Middle School Macau'),
    # National Taiwan University (NTU). NTUST ("… of Science and Technology") is a
    # SEPARATE school, handled earlier as 'NTUST'; keep only the plain-NTU forms
    # here (incl. a common misspelling) so NTUST no longer leaks in as 'NTU Taiwan'.
    (['national taiwan university', 'natioal taiwan university'], 'NTU Taiwan'),
    (['national tsing hua', 'national tsing-hua', 'nthu'], 'NTHU'),
    ('national chiao tung', 'NTU Taiwan'),
    ([
        'national yang ming chiao tung', 'national ang ming chiao tung',
        'national yaming chiaotung',
    ], 'NYCU'),
    ('national central university', 'National Central University'),
    ('national cheng kung', 'NCKU'),
    ('national chung hsing', 'National Chung Hsing University'),
    ('feng chia', 'Feng Chia University'),
    ('hon hai research', 'Hon Hai Research Institute'),
    ('artilux', 'Artilux Inc.'),
    ('chengdu', 'Chengdu'),
    ([
        'university of petroleum (beijing)', 'china university of petroleum',
    ], 'China University of Petroleum (Beijing)'),
    ('central south university', 'Central South University'),
    ('south university of science', 'SUSTech'),
    ('north china electric', 'North China Electric Power University'),
    ([
        'national university of defense technology', 'nudt,',
        'university of defense technology',
    ], 'NUDT'),
    ([
        'national engineering research center for next generation internet access',
        'national engineering research center of next generation internet access-system',
    ], 'NERC-NGIAS Wuhan'),
    (['cqu,', 'chongqing university'], 'CQU'),
    ('guangdong laboratory of artificial intelligence', 'GDLAB AI SZ'),
    ('guangdong university of technology', 'Guangdong U Tech'),
    ('guangxi university', 'Guangxi University'),
    ('guangxi medical', 'Guangxi Medical University'),
    (['harbin institute of technology', 'hit,'], 'HIT'),
    ('jiangsu normal', 'Jiangsu Normal University'),
    (['hefei national laboratory', 'hefei natl lab'], 'Hefei Natl Lab'),
    ('ningbo ori-chip', 'Ningbo Ori-chip'),
    ('henan academy', 'Henan Academy of Sciences'),
    ('henan normal university', 'Henan Normal University'),
    ('hebei,', 'Hebei University'),
    ('hubei optical fundamental', 'HUST'),
    ('fjirsm', 'FJIRSM'),
    ('fujian science', 'Fujian S&T Innovation Lab'),
    ('wuhan textile', 'Wuhan Textile U'),
    (['optics valley lab', 'optics valley laboratory'], 'Optics Valley Lab'),
    (['zte ', 'zte,', 'zte corporation'], 'ZTE'),
    ([
        'hanjiang naitional laboratory', 'hanjiang national laboratory',
    ], 'Hanjiang National Lab'),
    ('china mobile xiong', 'China Mobile Xiong’an'),
    ('china mobile research', 'China Mobile Research Institute'),
    ('china telecom research', 'China Telecom Research Institute'),
    ('china academy of electronics', 'CAEIT'),
    ('accelink', 'Accelink'),
    (['cict,', 'cict '], 'CICT'),
    ('yofc', 'YOFC'),
    ('state key laboratory of optical fiber and cable', 'YOFC'),
    ('huawei', 'Huawei'),
    ('cetus photonics', 'Cetus Photonics'),
    ('tianfu xinglong', 'Tianfu Xinglong Lake Laboratory'),
    ('wuzhen laboratory', 'Wuzhen Laboratory'),
    ('jinyinhu laboratory', 'Jinyinhu Laboratory'),
    ('jinhua no. 1 high school', 'Jinhua No. 1 High School'),
    ('berxel photonics', 'Berxel Photonics'),
    ('luzhou laojiao', 'Luzhou Laojiao Co.Ltd.'),
    (['liobate technology', 'liobate technologies'], 'Liobate'),
    ([
        'zhangjiang lab', 'zhangjiang laboratory', 'zhang jiang laboratory',
    ], 'Zhangjiang Laboratory'),
    ('yongjiang laboratory', 'Yongjiang Laboratory'),
    ('jinyinhu', 'Jinyinhu Laboratory'),
    ('purple mountain', 'Purple Mountain Lab'),
    ('shenzhen jufei', 'Shenzhen Jufei Optoelectronics Co'),
    (['peng cheng laboratory', 'pengcheng laboratory', 'pcl shenzhen'], 'PCL Shenzhen'),
    ('aerospace system engineering', 'Aerospace System Engineering'),
    ('ccdc drilling', 'CCDC Drilling Research Institute'),
    ([
        'national key lab amnm',
        'national key laboratory of advanced micro and nano manufacture',
    ], 'National Key Lab AMNM'),
    ('bangladesh university of engineering', 'BUET'),
    ([
        'consorzio nazionale interuniversitario per le telecomunicazioni', 'cnit,',
    ], 'CNIT Italy'),
    ('icrea', 'ICREA'),
    ('joint international research laboratory of specialty fiber', 'Shanghai'),
    ('vereshchagin institute', 'Vereshchagin IHPP'),
    ('state key laboratory for artificial microstructure', 'Peking U'),
    ('state key laboratory of information photonics and optical communications', 'BUPT'),
    ('state key laboratory of photonics and communications', 'SKL Photonics & Comm'),
    ('state key laboratory of transient optics and photonics', 'CAS XIOPM'),
    ('laboratory of solid state optoelectronics', 'CAS IOP Beijing'),
    ('nantong nanlitai', 'Nantong Nanlitai Technology'),
    ('sanway optoelectronic', 'Sanway Optoelectronic Tech. Corp.'),

    # ---- Japan -------------------------------------------------------------
    (['the university of tokyo', 'university of tokyo'], 'U Tokyo'),
    ('tokyo university of science', 'Tokyo U Science'),
    ([
        'tokyo institute of technology', 'tokyo tech',
        'institute of science tokyo',
    ], 'Tokyo Tech'),
    ('tokyo metropolitan university', 'Tokyo Metropolitan University'),
    ('tokyo university of agriculture and technology', 'TUAT'),
    (['keio university', 'keio,'], 'Keio'),
    ('waseda', 'Waseda'),
    (['the university of osaka', 'university of osaka', 'osaka university'], 'Osaka'),
    ('osaka metropolitan', 'Osaka Metropolitan University'),
    ('kyoto university', 'Kyoto'),
    ('tohoku university', 'Tohoku'),
    ('nagoya university', 'Nagoya'),
    ('nagoya institute of technology', 'Nagoya Institute of Technology'),
    ('hiroshima university', 'Hiroshima'),
    ('okayama university', 'Okayama'),
    ('yokohama national university', 'Yokohama Nat'),
    (['utsunomiya university', 'utsunomiya u'], 'Utsunomiya'),
    ('university of electro-communications', 'U Electro-Comm Tokyo'),
    ('graduate institute for advanced studies', 'Graduate Institute for Advanced Studies'),
    (['okinawa institute of science', 'okinawa inst of science'], 'OIST'),
    ('gifu university', 'Gifu'),
    ('kogakuin', 'Kogakuin University'),
    ('chitose institute of science', 'Chitose'),
    ('tamagawa university', 'Tamagawa'),
    ('hanseo university', 'Hanseo University'),
    ('toyota tech', 'Toyota Technological Institute'),
    ('toyota central r&d', 'Toyota Central R&D Labs Inc'),
    ('toyota research institute of north america', 'TRINA'),
    ('kyung hee', 'Kyung Hee University'),
    # AIST (Japan's Natl. Inst. of Advanced Industrial Science and Technology).
    # Use \b word boundaries so the short "aist" token can't fire inside
    # "KAIST" (Korea) or "NAIST" (Nara), which are different institutions
    # handled by their own anchors below — \b doesn't match between letters,
    # so "kaist" / "naist" can't trigger this. The boundary anchor matches
    # the bare token too ("AIST" / "AISt" with nothing after), so the special
    # exact-match RAW_OVERRIDES for those forms are not needed.
    ([r're:\baist\b',
      'national institute of advanced industrial science and technology',
      'natl inst of adv industrial', 'natl. inst. adv. ind. sci'],
     'AIST Japan'),
    ([
        'nict ', 'nict,', 'nict network', 'advanced ict research institute',
        'national institute of information and communications technology',
        'national institute of information and communication technology',
        'national inst of information & comm tech',
    ], 'NICT'),
    ('nims', 'NIMS'),
    ('riken', 'RIKEN'),
    ('national institute of metrology', 'National Institute of Metrology'),
    ('jasri', 'JASRI'),
    ('jaxa', 'JAXA'),
    ('nichia', 'Nichia'),
    ('mitsubishi electric', 'Mitsubishi Electric'),
    ('toshiba', 'Toshiba'),
    ('sumitomo electric', 'Sumitomo Electric Industries'),
    ('furukawa fitel', 'Furukawa FITEL Optical Components'),
    ('fujikura', 'Fujikura Ltd.'),
    (['nec ', 'nec,', 'nec corp'], 'NEC'),
    ('ntt innovative devices', 'NTT Innovative Devices Corporation'),
    ('nippon telegraph & telephone', 'NTT Japan'),
    # NTT: the bare "NTT Inc., <city>" parent-company form and all other NTT
    # subdivisions collapse to 'NTT' (company suffix dropped). Named NTT spin-out
    # corporations with a distinct identity (e.g. NTT Innovative Devices) keep
    # their own label above.
    ([r're:^ntt inc\.,', 'ntt research', 'ntt,', 'ntt '], 'NTT'),
    ('kddi', 'KDDI'),
    ('samusng r&d japan', 'Samsung'),  # "Samusng" is a typo for Samsung
    ('asai nursery', 'Asai Nursery'),
    ('ambition photonics', 'Ambition Photonics Inc.'),
    ('epiphotonics corp', 'EpiPhotonics'),
    ('epiphotonics usa', 'EpiPhotonics USA'),
    ('cellid', 'Cellid'),
    ('optqc', 'OptQC Corp.'),
    ('photonic inc', 'Photonic Inc'),
    ('center for quantum information and quantum biology', 'Osaka'),
    ('extreme photonics research team', 'Extreme Photonics Research Team'),
    ('joint attosecond science laboratory', 'Joint Attosecond Science Laboratory'),
    ('john a. paulson school', 'Harvard'),
    ('kapteyn-murnane', 'Kapteyn-Murnane Laboratories Inc.'),
    ('ryukoku', 'Ryukoku Univ'),
    (['tokushima university', 'tokushima'], 'Tokushima'),
    (['naist', 'nara institute of science and technology'], 'NAIST'),
    ('functional nanosystems', 'Functional Nanosystems'),

    # ---- Korea -------------------------------------------------------------
    (['korea advanced institute of science', 'korea advanced inst of science', 'kaist,', ', kaist'], 'KAIST'),
    # Daegu Gyeongbuk Institute of Science & Technology — the "&" and spelled-out
    # "and" forms both fold to DGIST.
    ('daegu gyeongbuk', 'DGIST'),
    ('yonsei', 'Yonsei'),
    # "Korea University of Science and Technology" (UST, Daejeon) must be matched
    # BEFORE the generic "korea university" below — otherwise that substring
    # mislabels UST as Korea University (Seoul), an unrelated school. One base
    # needle covers the bare form and the "(UST)"/"(KIST)" campus variants.
    ('korea university of science and technology', 'UST'),
    ('korea university', 'Korea U'),
    (['postech', 'pohang university of science and technology'], 'POSTECH'),
    ('sungkyunkwan', 'Sungkyunkwan'),
    ('hanyang', 'Hanyang'),
    ('chungbuk', 'Chungbuk National University'),
    ('hanbat', 'Hanbat National University'),
    ('ajou', 'Ajou University'),
    (['gist', 'gwangju institute of science and technology'], 'GIST'),
    (['unist', 'ulsan national institute of science and technology'], 'UNIST'),
    # \b so it doesn't fire inside accent-folded Portuguese "elétrica" ->
    # "eletrica" (contains the substring "etri").
    ([r're:\betri\b', 'electronics and telecommunications research institute'], 'ETRI'),
    (['kist ', 'kist,'], 'KIST'),
    ('kist school', 'KIST School'),
    ('korea institute of science and technology', 'KIST'),
    ([
        'kriss', 'korea research institute of standards and science',
        'korea research institute of standard and science',
    ], 'KRISS'),
    ('korea institute of machinery and materials', 'KIMM'),

    # ---- Singapore / SE Asia ----------------------------------------------
    ('nanyang technological university', 'NTU Singapore'),
    (['national university of singapore', 'nus,'], 'NUS'),
    (['singapore university of technology and design', 'sutd,'], 'SUTD'),
    ([
        'a*star', 'agency for science, technology and research',
        'agency for science technology and research',
    ], 'A*STAR'),
    # A*STAR Singapore sub-institutes fold into 'A*STAR'. By the time we reach
    # here the conflicting same-named institutes have already been routed away:
    #   - "Institute of Microelectronics, Chinese Academy of Sciences" -> CAS IME Beijing
    #   - Łukasiewicz "Institute of Microelectronics and Photonics" (Poland) -> Lukasiewicz IMiF
    # so the remaining "Institute of Microelectronics" strings are Singapore.
    # We deliberately do NOT use a bare "institute of microelectronics" anchor
    # (too greedy — it swept up the Polish/Henan institutes); the "(ime)" and
    # trailing-comma forms are what the Singapore strings actually carry.
    ([
        'institute of microelectronics (ime)', 'institute of microelectronics,',
        'institute for infocomm research', 'i2r,',
        'institute of high performance computing', 'q.inc',
        'quantum innovation centre',
    ], 'A*STAR'),
    ('maritime', 'Maritime Port Authority'),
    ('singtel', 'Singtel'),
    ('singapore telecommunications', 'Singtel'),
    (['national space technology and information center', 'nstic'], 'NSTIC Singapore'),
    (['advanced micro foundry', 'advanced micro foundry,'], 'Advanced Micro Foundry'),
    ('silterra malaysia', 'SilTerra Malaysia'),
    ('silterra', 'SilTerra'),
    ('linkstar microtronics', 'Linkstar Microtronics Pte. Ltd'),
    # Bare/abbreviated Nanyang (Singapore) forms. CDPT, SPMS and EEE are all
    # NTU Singapore units, and "Nanyang Technological Institute" is a typo for
    # the University. These fold into 'NTU Singapore'. Placed in the Singapore
    # section, AFTER the Taiwan anchors (NTU Taiwan / NYCU / NTHU) and the
    # Athens anchor (NTUA) have already run, so they can't capture those.
    (['nanyang technological institute', r're:\bntu\b'], 'NTU Singapore'),
    ('university of the philippines', 'UP Visayas'),
    ('de la salle', 'De La Salle University'),
    ('commission on higher education', 'Commission on Higher Education'),
    ('asian institute of technology', 'AIT'),
    ('kasetsart', 'Kasetsart University'),
    ('chulalongkorn', 'Chulalongkorn'),

    # ---- India -------------------------------------------------------------
    ('iit bombay', 'IIT Bombay'),
    ('indian institute of technology - bombay', 'Indian Institute of Technology - Bombay'),
    ('indian institute of technology bombay', 'IIT Bombay'),
    (['iit delhi', 'indian institute of technology delhi'], 'IIT Delhi'),
    (['iit madras', 'indian institute of technology madras'], 'IIT Madras'),
    (['iit kanpur', 'indian institute of technology kanpur'], 'IIT Kanpur'),
    (['iit kharagpur', 'indian institute of technology kharagpur'], 'IIT Kharagpur'),
    (['iit roorkee', 'indian institute of technology roorkee'], 'IIT Roorkee'),
    (['iit guwahati', 'indian institute of technology guwahati'], 'IIT Guwahati'),
    (['iit hyderabad', 'indian institute of technology hyderabad'], 'IIT Hyderabad'),
    (['iit indore', 'indian institute of technology indore', 'indian institute of technology (iit) indore', r're:indian institu[t]?e of technology \(iit\) indore'], 'IIT Indore'),
    ([
        'iit jodhpur', 'indian institute of technology jodhpur',
    ], 'IIT Jodhpur'),
    ([
        'indian institute of technology ropar', 'iit ropar',
    ], 'Indian Institute of Technology Ropar'),
    ([
        'indian institute of technology,', 'indian institute of technology ',
    ], 'Indian Institute of Technology'),
    ('iit,', 'IIT'),
    ('indian institute of information technology', 'IIIT'),
    (['iisc bangalore', 'indian institute of science'], 'IISc Bangalore'),
    (['tifr', 'tata institute of fundamental research'], 'TIFR'),
    ('inst sw comm', 'Inst SW Comm'),
    (['csir csio', 'csir-cspio'], 'CSIR CSIO'),
    (['hyderabad,', 'uoh', 'university of hyderabad'], 'UoH'),
    ('punjab engineering college', 'Punjab Engineering College'),
    ('gail (india)', 'Gail (India) Ltd.'),

    # ---- Australia / NZ ----------------------------------------------------
    (['australian national university', 'anu,'], 'ANU'),
    ('university of sydney', 'Sydney'),
    ('university of new south wales', 'UNSW'),
    ('unsw canberra', 'UNSW Canberra'),
    (['unsw,', 'unsw '], 'UNSW'),
    (['university of melbourne', 'the university of melbourne', 'the university of mlebourne'], 'Melbourne'),
    ('monash', 'Monash'),
    (['royal melbourne institute of technology', 'rmit'], 'RMIT'),
    (['university of western australia', r're:\buwa\b'], 'UWA'),
    (['university of technology sydney', 'uts sydney'], 'UTS Sydney'),
    (['university of adelaide', 'adelaide university'], 'Adelaide University'),
    ('macquarie', 'Macquarie'),
    # COMBS Centre (ARC Centre of Excellence in Optical Microcombs for
    # Breakthrough Science): a distributed centre with no single host university
    # (members span Sydney, Monash, Swinburne, Adelaide, ANU), so every spelling
    # canonicalizes to the centre's own short label "COMBS Australia" rather than
    # any one university. Covers the British/American "Centre/Center", "in/for",
    # the "COMBS and Optical Sciences Centre" and bare "COMBS Centre of
    # Excellence" variants, and the "(COMBS)" acronym form. MUST precede the
    # bare-university anchors below (e.g. "swinburne"): a string like
    # "…Microcombs… (COMBS), Swinburne University…" should resolve to the centre,
    # not to whichever member university happens to appear in the same line.
    ([
        'optical microcombs', 'microcombs and breakthrough science',
        'combs and optical sciences centre', 'combs centre of excellence',
        'combs australia',
    ], 'COMBS Australia'),
    ('swinburne', 'Swinburne'),
    (['ozgrav', 'centre of excellence for gravitational wave'], 'OzGrav'),
    ('victoria university of wellington', 'Victoria U Wellington'),
    ('university of canterbury nz', 'U Canterbury NZ'),
    ('dodd-walls', 'Dodd-Walls Centre'),

    # ---- Canada ------------------------------------------------------------
    (['national research council canada', 'nrc canada'], 'NRC Canada'),
    ('defence research and development canada', 'DRDC'),
    ('institut courtois', 'Institut Courtois'),

    # ---- Latin America / Africa -------------------------------------------
    ('cinvestav', 'CINVESTAV'),
    # UNAM — Universidad Nacional Autónoma de México (National Autonomous
    # University of Mexico). "UNAM" is the standard short name. Cover the
    # Spanish name, the English translation, and the bare acronym.
    (['universidad nacional autonoma de mexico', 'national autonomous university of mexico', r're:\bunam\b'], 'UNAM'),
    (['universidade federal de pernambuco', 'ufpe,'], 'UFPE'),
    # Federal University of Alagoas (UFAL) — English and Portuguese spellings
    # are the same institution → 'Alagoas'. The Federal *Institute* of Alagoas
    # (IFAL) is a separate body and keeps its own label (anchor below).
    (['universidade federal de alagoas', 'federal university of alagoas'], 'Alagoas'),
    ('federal institute of alagoas', 'Federal Institute of Alagoas'),
    ('federal university of parana', 'Federal University of Paraná'),
    ([
        'universidade estadual de campinas', 'unicamp,', 'unicamp',
        'state university of campinas',
    ], 'Unicamp'),
    (['universidade de sao paulo', 'university of sao paulo'], 'São Paulo'),
    ('usp - instituto de fisica de sao carlos', 'USP - Instituto de Fisica de Sao Carlos'),
    ('centro brasileiro de pesquisas fisicas', 'Centro Brasileiro de Pesquisas Fisicas'),
    ('university of guanajuato', 'U Guanajuato'),
    ('south african astronomical observatory', 'South African Astronomical Observatory'),

    # ---- Middle East -------------------------------------------------------
    (['king abdullah university of science', 'kaust'], 'KAUST'),
    (['king fahd university of petroleum', 'kfupm'], 'KFUPM'),
    ('expec advanced research', 'EXPEC ARC'),
    ('halliburton', 'Halliburton Technology'),
    ('al-azhar', 'Al-Azhar University'),
    ('ain shams', 'Ain Shams University'),
    (['alexandria u', 'university of alexandria'], 'Alexandria'),
    ('technology innovation institute', 'Technology Innovation Institute'),
    ('university of jeddah', 'University of Jeddah'),

    # ---- Cross-cutting US specialty ---------------------------------------
    ('rit,', 'RIT'),
    ('rensselaer polytechnic institute', 'RPI'),
    ('lehigh', 'Lehigh'),
    ('drexel', 'Drexel'),
    ('villanova', 'Villanova'),
    ('bowling green state', 'Bowling Green State University'),
    ('augustana', 'Augustana'),
    ('washington & jefferson', 'Washington & Jefferson College'),
    ('williams', 'Williams'),
    ('mount holyoke', 'Mount Holyoke College'),
    ('east tennessee state', 'East Tennessee State University'),
    (['middle tennessee state', 'middle tennesse state', 'middle tenesse state'], 'Middle Tennessee State'),
    ('central connecticut', 'Central Connecticut State University'),
    ('central michigan', 'Central Michigan University'),
    ('morgan state', 'Morgan State University'),
    ('saint john\'s', 'St. John\'s'),
    ('staten island', 'Staten Island'),
    ('norfolk state', 'Norfolk State'),
    ('west virginia university', 'West Virginia University'),
    (['university of north dakota', 'north dakota,'], 'University of North Dakota'),
    ('farmingdale state college', 'Farmingdale State College'),
    ('hershey high school', 'Hershey High School'),
    ('bridgewater state university', 'Bridgewater State'),
    ('us military academy', 'US Military Academy'),
    (['byu,', 'brigham young'], 'BYU'),
    ('weber state', 'Weber State'),
    ('utah state', 'Utah State'),
    # NOTE: there is intentionally no bare ('university park', ...) anchor here.
    # "University Park" is a campus town, not an institution: it's Penn State's
    # main campus (handled by the PA-/ZIP-qualified needles on the Penn State
    # anchor above) and also SMU's town ("University Park, TX"). A bare token
    # anchor mislabeled both as "University Park"; without it, any unqualified
    # leftover falls through to the fallback shortener instead.
    ('university of guelph', 'U Guelph'),
    ([
        'clemson center for optical materials',
        'center for optical materials science and engineering',
    ], 'Clemson'),
    ('center for advanced self-powered systems', 'ASSIST'),
    (['usra research institute for advanced computer science', 'riacs,'], 'USRA RIACS'),
    ('institut interdisciplinaire d', '3IT Sherbrooke'),  # Institut Interdisciplinaire d'Innovation Technologique
    ('triangle regional research', 'TRRDC'),
    ('w&wsens', 'W&Wsens Devices Inc'),
    ('oewaves', 'OEwaves'),
    ('ipg photonics', 'IPG Photonics'),
    (['np photonics, inc', 'np photonics,'], 'NP Photonics'),
    ('phase sensitive innovations,', 'Phase Sensitive Innovations'),
    ('phase sensitive innovations, inc', 'Phase Sensitive Innovations, Inc.'),
    ('drs daylight', 'DRS Daylight Solutions'),
    ('emode photonix', 'EMode Photonix'),
    ('flexcompute', 'Flexcompute'),
    ('gdsfactory', 'GDSFactory'),
    ('ansys', 'Ansys'),
    ('comsol', 'Comsol Multiphysics'),
    ('lumerical', 'Ansys'),
    ('octave photonics', 'Octave Photonics'),
    ('omega optics', 'Omega Optics'),
    ('axiomatic-ai', 'Axiomatic-AI'),
    ('aloe semiconductor', 'Aloe Semiconductor Inc.'),
    (['adtech optics', 'adtech photonics'], 'AdTech Photonics'),
    ('xscape', 'Xscape Photonics'),
    ('nexus photonics', 'Nexus Photonics'),
    ('beacon photonics', 'Beacon Photonics'),
    ('cubiq technologies', 'CUbIQ Technologies'),
    ('xcimer energy', 'Xcimer Energy Corporation'),
    ('octosig', 'Octosig Consulting'),
    ('castor optics', 'Castor Optics Inc'),
    ('arktonics', 'Arktonics'),
    ('femtovision', 'FemtoVision'),
    ('avo photonics', 'Avo Photonics'),
    ('pinc technologies', 'PINC Technologies Inc.'),
    ('lumina', 'Lumina'),
    ('lightera labs', 'Lightera Labs'),
    ('icarus quantum', 'Icarus Quantum Inc.'),
    ('mesa quantum', 'Mesa Quantum'),
    ('photon queue', 'Photon Queue'),
    ('temporis solutio', 'Temporis Solutio LLC'),
    ('rydberg technologies', 'Rydberg Technologies Inc.'),
    ('qubitekk', 'Qubitekk'),
    ('qunnect', 'Qunnect Inc.'),
    ('quantum computing inc', 'Quantum Computing Inc'),
    (['mesa lab', 'national center for atmospheric research'], 'NCAR'),
    ('lawrence semiconductor', 'LSRL'),
    ('mpi multidisciplinary sciences', 'MPI Multidisc Sci'),
    ('relativity networks', 'Relativity Networks'),
    ('postdoctoral research associate', 'Postdoctoral Research Associate'),
    (['ii-vi,', ' ii-vi '], 'II-VI'),
    ('tau systems', 'TAU Systems Inc'),
    ('teragear', 'Teragear'),
    ('thorlabs quantum', 'Thorlabs'),
    (['eu tech', 'ieu,'], 'IEU'),
    ('qxp technology', 'QXP Technology'),
    ('qaleido', 'Qaleido Photonics'),
    ('qioptiq', 'Qioptiq Ltd.'),
    ('photonic crystal photonic frontiers', 'Photonic Inc'),
    ('hubble', 'Hubble'),
    ('alphawave', 'AlphaWave Semi'),

    # ---- Misc / very specific institutions ---------------------------------
    ('advanced fiber resources milan', 'AFR Milan'),
    ('saint petersburg', 'SPb State Univ'),  # may need adjustment
    ('iberian nanotechnology lab', 'INL'),
    ('clemson center', 'Clemson'),
    ('ki3 photonics', 'Ki3 Photonics'),
    ('chi 3 optics', 'Chi 3 Optics'),
    ('chi-3 optics', 'Chi-3 Optics'),
    ('chi3 optics', 'Chi3 Optics LLC'),
    (['opms', 'open minded solutions'], 'OpMS - Open Minded Solutions'),
    (['ks photonics', 'hs photonics'], 'HS Photonics'),
    (['flyth aerospace', 'flyht aerospace'], 'FLYHT Aerospace Solutions Ltd'),
    ('avirata', 'Avirata Defence Systems'),
    ('atlantic technological', 'Atlantic Technological University'),
    ('measurement science and technology', 'Measurement Science and Technology'),
    ('radiation oncology', 'Radiation Oncology'),
    # Generic department abbreviations like "EE," and "ECE," are too brittle —
    # they catch unrelated strings ("Fort Lee, NJ", "Singapore, Singapore"
    # post a 'NTU, EEE,' prefix). Removed; the fallback shortener can do better.
    ('cto office', 'CTO Office'),
    ('joint quantum institute', 'Maryland'),
    (['hpe labs,', 'hpe '], 'HPE Labs'),
    (r're:\blle\s+rochester\b', 'Rochester'),
    ('aeluma', 'Aeluma'),
    ('lumiphase ag', 'Lumiphase AG'),
    ('bright quantum', 'Bright Quantum Inc.'),
    ('shiva photonics', 'Shiva Photonics'),
    (['coreace', 'core4ce'], 'Core4ce'),
    ('columbus technologies', 'Columbus Technologies and Services'),
    ('photonic crystal', 'Photonic Inc'),
    ([
        'north carolina,', 'north carolina state university,',
        'north carolina, raleigh',
    ], 'NC State'),
    ('photon design,', 'Photon Design'),

    # ---- Lebanon -----------------------------------------------------------

    # ---- Other catch-all institutes ----------------------------------------
    ('hp inc', 'HP'),
    (['av incorporated', 'av inc.'], 'AV Inc.'),
    # MDPI (the open-access publisher), spelled out or as the bare acronym.
    ([r're:\bmdpi\b', 'multidisciplinary digital publishing institute'], 'MDPI'),

    # ---- Ad-hoc rarities ---------------------------------------------------
    ('uniwersytet mikolaja', 'Nicolaus Copernicus'),
    ([
        'aerospace, mechanical engineering, university of notre dame',
        'notre dame',
    ], 'Notre Dame'),
    ('binghamton', 'Binghamton'),
    ('university of bonn', 'U Bonn'),
    ('university of cologne', 'U Cologne'),
    ('lumina,', 'Lumina'),
    ('uviquity', 'Uviquity'),
    ('aeluma,', 'Aeluma'),
    ('amcl optical lab', 'Intel'),  # AMCL is an Intel lab
    ('photonic integrated cricuits group', 'UCF'),  # CREOL group → UCF
    ('seventh framework programme', 'EU FP7'),
    ('postech,', 'POSTECH'),
    ('andrew and erna viterbi', 'Technion'),
    # ---- bare-name short forms (prefer the plain place/proper name) --------
    # These institutions are routinely referred to without a "U"/"University"
    # qualifier in the field, and the bare form is unambiguous here.
    ('university of aarhus', 'Aarhus'),
    ('university of campinas', 'Unicamp'),
    ('university of kaiserslautern', 'Kaiserslautern'),
    ('university of tampere', 'Tampere'),
    # Konstanz: the data carries a misspelling ("Kostanz"). Anchor both the
    # correct and the typo'd spelling to the canonical bare name so neither
    # falls through to a "U Kostanz" fallback.
    ([
        'university of konstanz', 'university of kostanz', 'universitat konstanz',
    ], 'Konstanz'),
    # ---- special relabels --------------------------------------------------
    # "University of Los Angeles" is a mangled "University of California, Los
    # Angeles"; there is no separate UCLA-less institution by that name.
    ('university of los angeles', 'UCLA'),
    # University of Illinois Chicago: use the standard initialism.
    ('university of illinois chicago', 'UIC'),
    # Università della Campania "Luigi Vanvitelli".
    (['university of campania', 'universita della campania'], 'UniCampania'),
    # Diamond SA (fiber-optic connector maker, Losone, Switzerland). The raw
    # string is "Diamond Company"; map to its proper short name.
    ('diamond company', 'Diamond SA'),
    # ---- cross-year / variant-phrasing merges -----------------------------
    # Same institution written different ways across the 2025/2026 programs.
    # Fold each alternate phrasing onto the canonical (bare, per house style)
    # label its other spelling already resolves to.
    ('imperial college', 'Imperial'),          # bare "Imperial College" (no London)
    # "University of Sydney" already matched above; this entry keeps the
    # no-dot "Univ of Sydney" abbreviation (which normalize() leaves alone).
    ('univ of sydney', 'Sydney'),
    ('tohoku univ', 'Tohoku'),                  # "Tohoku Univ." abbreviation
    ('saitama univ', 'Saitama'),
    ('kassel universitat', 'Kassel'),
    # "Shanghai University" with no trailing comma (the comma form is anchored
    # elsewhere). Use a regex that REQUIRES the name to end there, so it can't
    # fire on "Shanghai University of ..." or "Shanghai Jiao Tong University".
    (r're:\bshanghai university\b(?!\s+of)', 'Shanghai'),
    # Case-only typos in acronyms.
    # SJTU lowercase form.
    ('sjtu', 'SJTU'),
    # Ruhr University Bochum: many hyphen/spelling variants -> one label.
    (['ruhr-universitat-bochum - puls group', 'puls group', 'ruhr-universitat bochum', 'ruhr universitat bochum', 'ruhr-university bochum', 'ruhr-university-bochum', 'ruhr university bochum'], 'RUB'),
    # ---- garbled / typo'd source strings ----------------------------------
    # These raw spellings are mangled enough that the normal anchors miss them;
    # fold each onto the correct institution. Substrings (not exact overrides)
    # so the address-tailed variants ("…, Bath, United Kingdom") match too.
    ('niversity of copenhagen', 'Copenhagen'),  # dropped leading "U"
    ('colorado university of boulder', 'CU Boulder'),  # scrambled CU Boulder

    # ---- map-audit fixes: typos, variant merges, bare names ----------------
    # Misspelled/mangled forms the normal anchors miss; alternate phrasings of
    # one institution; and single-institution bare place names. Substrings so
    # address-tailed variants match too.
    ('university of mlebourne', 'Melbourne'),
    # (Tokyo Metropokitan University typo is folded to "metropolitan" in
    # normalize(), so the main 'tokyo metropolitan university' anchor catches it.)
    ('standford university', 'Stanford'),
    ('pennslvania state university', 'Penn State'),
    (['technical university munich', 'technical university muncih'], 'TU Munich'),
    (['philipps-universitat marburg', 'phillips-university marburg'], 'Marburg'),
    (['helmut schmidt university', 'helmut-schmidt-university'], 'Helmut Schmidt U'),
    # The "universit'a" form is a mangled "università" (the à arrived as a
    # quote+a), a LETTER corruption normalize() can't fix, so it keeps its own
    # needle. The two real "università dell'insubria" spellings differ only by
    # apostrophe glyph, which normalize() folds, so one ASCII needle covers both.
    (["universit'a dell'insubria", "universita dell'insubria"], 'Insubria U'),
    (['universita di pisa', 'university of pisa'], 'U Pisa'),
    (['universitat rostock', 'university of rostock'], 'U Rostock'),
    ('universidad de guanajuato', 'U Guanajuato'),
    ('university of kansas', 'U Kansas'),
    ('shizuoka university', 'Shizuoka'),
    ('hunan university', 'Hunan'),

    # ---- Additional curated institutions -----------------------------------
    # Canonical short names for institutions that fell through the broader
    # anchors above. Each needle is specific enough not to collide with the
    # rest of the curated list. Needles are matched against the normalized
    # string (lowercased, diacritics and dash/apostrophe glyphs folded), so
    # they are written in plain ASCII.
    # University of Leeds: the program writes it several long ways (with the
    # school suffix, as the Pollard Institute, etc.). All collapse to "Leeds".
    (['university of leeds', 'pollard institute', r're:\bu leeds\b'], 'Leeds'),
    # Laboratoire Pierre Aigrain / former UPMC Paris 6 — the ENS Paris physics
    # lab; fold the historical Pierre-et-Marie-Curie / Paris 6 form to ENS Paris.
    (['pierre et marie curie', 'laboratoire pierre aigrain'], 'ENS Paris'),
    # "Institute of Quantum Electronics Zurich (ETHZ)" and similar ETH Zürich
    # spellings -> ETH Zürich (matches the existing ETH handling).
    (['quantum electronics zurich', r're:\bethz\b'], 'ETH Zürich'),
    ('university of wurzburg', 'Würzburg'),
    ('european laboratory for non-linear spectroscopy', 'LENS'),
    ('ernst ruska-centre', 'Ernst Ruska Centre'),
    # NEST = the CNR-Istituto Nanoscienze + Scuola Normale Superiore lab in Pisa,
    # written with the "(NEST)" tag in some forms and as a leading "NEST" in
    # others; both fold to NEST.
    (['scuola normale superiore (nest)', 'nest cnr-istituto nanoscienze'], 'NEST'),
    ('nrc post-doctoral research associate', 'NRL'),
    ('ihp-leibniz institut', 'IHP'),
    # Peter Grünberg Institute (all spellings: "Gruenberg"/"Grünberg"->"grunberg"
    # after diacritic folding, hyphenated or not) is part of Forschungszentrum
    # Jülich; map every form there so the various long "Peter[- ]Grünberg-
    # Institute (PGI-N)" spellings all collapse to it.
    (r're:peter[- ]gr(?:ue|u)nberg', 'FZJ'),
    ('paul drude institute', 'Paul Drude Institute'),
    # German-language form of the same institute (Paul-Drude-Institut für
    # Festkörperelektronik); normalize() has already folded the diacritics and
    # dashes, so the needle is plain ASCII.
    ('paul-drude-institut', 'Paul Drude Institute'),
    ('mohammed vi polytechnic', 'Mohammed VI'),
    # Wroclaw (some sources use the typo "Universityof").
    ('wroclaw university', 'Wroclaw'),
    (r're:\binstitut universitaire de france\b', 'IUF'),
    ('celare quantum communications', 'Celare'),
    ('austrian institute of technology', 'AIT'),
    (['technical univeristy of dresden', 'technical university of dresden'], 'TU Dresden'),
    # "Dipartimento di Scienze, Università degli Studi Roma" = Roma Tre's
    # science dept; and the explicit "Università Roma Tre" form.
    (['universita roma tre', 'universita degli studi roma'], 'Roma Tre'),
    # Note: "Institut Polytechnique de Paris" -> "IP Paris" and "Silicon Austria
    # Labs GmbH" -> "Silicon Austria Labs" are applied by editing their existing
    # curated anchors earlier in this list.
    ('de vinci higher education', 'De Vinci'),
    ('laser components germany', 'Laser Components'),
    ('vigo photonics', 'Vigo Photonics'),                # drops trailing "SA"
    ('nextnano', 'nextnano'),   # "nextnano Lab" / "nextnano GmbH" -> "nextnano"
]

# Append more late patterns AFTER the above big batch (lower priority).
# These are short tokens that should only trigger if nothing earlier did.
# They use word-boundary regex to avoid matching inside larger words.
_LATE_ANCHORS_SRC: list = [
    # Bare-city LATE anchors removed — they wrongly turned "Sydor Technologies,
    # Rochester, NY" into "Rochester" and similar. The fallback shortener
    # produces "Sydor Technologies" instead.
    #
    # The `re:\buniversity,` -> 'University' catch-all was also removed: it
    # collapsed any affiliation containing the word "university," to the
    # useless bare label "University" (e.g. "…, Beijing Information Science
    # and Technology University, Beijing, China"). The fallback shortener
    # extracts the real institution name instead.

    # Research centers hosted inside a university: low priority so the parent
    # university wins when it's present in the string (e.g. "IQUIST, University
    # of Illinois at Urbana-Champaign" -> UIUC), but a standalone mention of the
    # center still resolves to its acronym.
    (['illinois quantum information science and technology', r're:\biquist\b'], 'IQUIST'),
    ('joint center for quantum information and computer science', 'QuICS'),
]


def _expand_anchors(src: list) -> list[tuple[str, str]]:
    """Flatten the authored anchor source into the (needle, short) pairs the
    matcher consumes, preserving order exactly.

    Two authoring forms are accepted and may be freely mixed, both reading
    needle(s) first, short label last:
      - ('needle', 'Short')           a single anchor
      - (['n1', 'n2', ...], 'Short')  several needles that all map to one Short,
                                      expanded in place to
                                      ('n1','Short'), ('n2','Short'), ...
    The grouped form just removes the repetition of writing the same Short label
    on every line; because each group expands at its own position, the resulting
    ordered list is identical to writing the pairs out individually. Order is
    load-bearing (first match wins, specific-before-general), so this must never
    reorder entries.
    """
    out: list[tuple[str, str]] = []
    for entry in src:
        key, val = entry
        if isinstance(key, (list, tuple)) and not isinstance(key, str):
            # Grouped form: ([needles], Short)
            for needle in key:
                out.append((needle, val))
        else:
            # Plain form: (needle, Short)
            out.append((key, val))
    return out


ANCHORS: list[tuple[str, str]] = _expand_anchors(_ANCHORS_SRC)
LATE_ANCHORS: list[tuple[str, str]] = _expand_anchors(_LATE_ANCHORS_SRC)


# ---------------------------------------------------------------------------
# Algorithmic fallback for affiliations no anchor matched.
# ---------------------------------------------------------------------------

# Words that imply "this comma-segment is a department, not the institution".
DEPT_HINT_WORDS = re.compile(
    r'\b(department|dept|school|institute of|laboratory|lab\.?|laboratoire|'
    r'group|center|centre|faculty|college of|division|division of|'
    r'graduate school|state key|key lab|research center|research centre|'
    # Common misspellings of "department" and the Spanish/Portuguese forms,
    # so a leading "Departament of Physics, <University>" segment is stripped
    # as clutter rather than mistaken for the institution.
    r'deparment|departament|deptartment|departmento|departamento|departemento|'
    r'departamento de|departement|'
    # Misspellings of "institute of" (the correct form is already covered
    # above); catch the dropped/transposed-letter variants too.
    r'insitute of|intitute of|intsitute of|institue of|instutite of)\b',
    re.IGNORECASE,
)

# Words that imply "this comma-segment IS the institution" (so the trailing
# 'drop a city' heuristic must not discard it). Deliberately narrow: only the
# unambiguous top-level institution nouns, NOT department words like
# "institute of" (which DEPT_HINT_WORDS owns).
INSTITUTION_HINT_WORDS = re.compile(
    r'\b(university|universit[eé]|universidad|universität|università|'
    r'college|polytechnic|politecnico|institute of technology|'
    r'national lab|national laboratory)\b',
    re.IGNORECASE,
)

# Bare department-name segments that imply they're a department label,
# not the institution. Matched on the whole segment (case-insensitive).
DEPT_BARE_NAMES = {
    'physics', 'physics & astronomy', 'physics and astronomy', 'astronomy',
    'mathematics', 'maths', 'math', 'chemistry', 'biology', 'biophysics',
    'biochemistry', 'biotechnology', 'computer science', 'cs',
    'electrical engineering', 'electrical and computer engineering',
    'mechanical engineering', 'civil engineering', 'chemical engineering',
    'aerospace engineering', 'materials science', 'materials science and engineering',
    'optics', 'photonics', 'optical engineering',
    'ee', 'ece', 'eee', 'me', 'engineering', 'physics department',
    'ece department', 'ee department', 'physics dept',
    'applied physics', 'engineering physics',
    'physics, applied physics, & astronomy', 'physics, applied physics and astronomy',
}

# Country/region tokens — comma-segments matching these are addresses, not institutions.
COUNTRY_TOKENS = {
    'United States', 'USA', 'U.S.A.', 'U.S.', 'United Kingdom', 'UK',
    'Germany', 'France', 'Italy', 'Spain', 'Portugal', 'Netherlands',
    'Belgium', 'Switzerland', 'Austria', 'Sweden', 'Norway', 'Denmark',
    'Finland', 'Iceland', 'Ireland', 'Poland', 'Czech Republic', 'Czechia',
    'Slovakia', 'Hungary', 'Romania', 'Bulgaria', 'Greece', 'Turkey',
    'Russian Federation', 'Russia', 'Ukraine', 'Belarus', 'Lithuania',
    'Latvia', 'Estonia', 'Slovenia', 'Croatia', 'Serbia',
    'China', 'Japan', 'Korea (the Republic of)', 'Korea',
    'Taiwan', 'Hong Kong', 'Macau', 'Singapore', 'Malaysia', 'Thailand',
    'Vietnam', 'Indonesia', 'Philippines', 'India', 'Pakistan', 'Bangladesh',
    'Sri Lanka', 'Australia', 'New Zealand', 'Canada', 'Mexico', 'Brazil',
    'Argentina', 'Chile', 'Colombia', 'Peru', 'Venezuela', 'Cuba',
    'Israel', 'Egypt', 'Morocco', 'Tunisia', 'Algeria', 'Saudi Arabia',
    'United Arab Emirates', 'UAE', 'Qatar', 'Kuwait', 'Lebanon', 'Iran',
    'Iraq', 'Jordan', 'Pakistan', 'South Africa', 'Kenya', 'Nigeria',
    'Ethiopia', 'Ghana',
}

US_STATE_TOKENS = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI',
    'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI',
    'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC',
    'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT',
    'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC',
}

# Tokens often appearing as the city-or-region piece in Chinese addresses.
COMMON_REGION_TOKENS = {
    'BEIJING', 'HuBei', 'HUBEI', 'GUANGDONG', 'Shaanxi', 'Hubei', 'Beijing',
    'Shandong', 'Jiangsu', 'Anhui', 'Zhejiang', 'Tianjin', 'Hong Kong',
    'NSW', 'Victoria', 'WA', 'SA', 'QLD', 'ACT',
    'Bayern', 'BW', 'Baden-Württemberg', 'Hessen',
    'Select Region', 'Please select region, state or province',
    'Île-de-France', 'Lombardia',
}


# University-designator words: the English word and "Univ"/"Univ." abbreviation
# the source uses heavily, plus the foreign stems (accented and accent-free).
# Ordered longest-first so the alternation prefers the full foreign stem before
# the bare "univ". The trailing dot is matched separately ("\.?") in the regex.
_UNIV_WORD = (r'(?:université|universität|università|universidade|universidad|'
              r'universiteit|universite|universitat|universita|university|univ)')
# Connective after a leading designator ("University OF X", "Univ. DE X").
_UNIV_CONN = r'(?:of|de|del|della|di|do|da|der|des|du)'
_UNIV_TOKEN_RE = re.compile(
    r'(?i)^(?P<before>.*?)\s*\b' + _UNIV_WORD + r'\b\.?\s*'
    r'(?P<conn>(?:' + _UNIV_CONN + r')\s+)?(?P<after>.*)$')

# Generic university qualifiers: when one of these is all that precedes the
# designator, the distinctive name is what FOLLOWS ("Technische University
# Berlin" -> "Berlin"), not the qualifier itself.
_GENERIC_QUALIFIER = {
    'technische', 'technical', 'technological', 'polytechnic', 'polytechnical',
    'politecnico', 'politecnica', 'pontificia', 'pontifical', 'pontificie',
}


def _strip_university_word(segment: str) -> str:
    """Reduce a university name to its distinctive place/proper-noun part.

    Splits on the university designator and keeps the recognizable name, per
    house style:
        "University of Michigan"              -> "Michigan"
        "Michigan State University"           -> "Michigan State"
        "Konkuk Univ."                        -> "Konkuk"
        "Univ. de Montpellier"                -> "Montpellier"
        "AGH Univ. of Science and Technology" -> "AGH"
        "Eulji University School of Medicine" -> "Eulji"
    Rule: text BEFORE the designator wins when present (it's the institution's
    name); otherwise the text AFTER it (the "University of X" shape). Guards
    "University College X", a distinct institution type, from being cut down.
    """
    s = re.sub(r'^the\s+', '', segment.strip(), flags=re.IGNORECASE)
    # German closed compounds ("Universitätsklinikum/Universitätsmedizin/
    # UniversitätsSpital Bonn") — the designator is glued to a noun, so the
    # word-boundary split below can't see it. Keep the distinctive name before
    # the compound if any ("Charité Universitätsmedizin Berlin" -> "Charité"),
    # else the city/name after it ("Universitätsklinikum Bonn" -> "Bonn").
    m = re.match(r'(?i)^(?P<before>.*?)\s*\buniversit[äa]ts\S*\s*(?P<after>.*)$', s)
    if m and (m.group('before').strip() or m.group('after').strip()):
        return (m.group('before').strip(' ,-') or m.group('after').strip())
    m = _UNIV_TOKEN_RE.match(s)
    if not m:
        return s
    before = m.group('before').strip(' ,-')
    after = m.group('after').strip()
    # Italian "Università degli Studi di X" and the "Studi di X" tail -> X.
    after = re.sub(r"(?i)^(?:degli\s+studi\s+|studi\s+)(?:di|del|della|dell'?)\s+",
                   '', after).strip()
    # "University College X" / "Univ. College X" — keep as-is (not "College X").
    if not before and re.match(r'college\b', after, re.IGNORECASE):
        return s
    # "before" is normally the institution's name ("Michigan State University" ->
    # "Michigan State"), EXCEPT when it's only a generic qualifier ("Technische"/
    # "Polytechnic"/"Pontificia ... University X"), where the distinctive part is
    # what follows.
    if before.lower() in {'technische', 'technical', 'technological'} and after:
        return 'TU ' + after          # standard short for technical universities
    if before and before.lower() not in _GENERIC_QUALIFIER:
        return before
    if after:
        return after
    return before or s


def _strip_parens(s: str) -> str:
    """Remove every parenthetical group, e.g. a "(United States)" /
    "(Korea, Republic of)" country tail or an embedded "(China)". Repeated so
    nested/adjacent groups all go. Keeps the short name free of parentheses."""
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r'\s*\([^()]*\)', '', s).strip()
    return s


def _final_clean(s: str) -> str:
    """Final tidy of a short name: no parentheses, no commas, no periods —
    only internal hyphens and ampersands survive as punctuation."""
    s = _strip_parens(s)
    s = s.replace(',', ' ')
    s = s.replace('.', '')                    # "Co." -> "Co", "I.D." -> "ID"
    s = re.sub(r'\s+', ' ', s).strip()
    # Drop a leftover dangling connector at either edge ("Foo &" / "& Bar"),
    # e.g. a residue of a stripped legal tail — an internal "&" ("Texas A&M",
    # "Johnson & Johnson") is kept since strip() only trims the ends.
    s = re.sub(r'^(?:&|\+)\s+|\s+(?:&|\+)$', '', s)
    return s.strip(' -&+')


def _strip_legal_suffix(s: str) -> str:
    """Remove trailing corporate/legal-entity designators.

    Companies arrive as "Berxel Photonics Co., Ltd.", "Avo Photonics, Inc.",
    "Arktonics, LLC", "Advanced Fiber Resources Milan s.r.l." etc. The trailing
    designator (and any comma before it) is noise for a short name. Strip it
    repeatedly so "Co., Ltd." (two designators) fully collapses.
    """
    designators = (
        r'co\.,?\s*ltd\.?', r'co\.,?\s*limited', r'pvt\.?\s*ltd\.?',
        r'pte\.?,?\s*ltd\.?', r'pte\.?',
        r'ltd\.?', r'limited', r'llc', r'l\.l\.c\.', r'inc\.?',
        r'incorporated', r'corp\.?', r'corporation', r'gmbh',
        # German limited-partnership tails, incl. the "Co. KG"/"Co. KGaA"
        # compounds and the bare "KG"/"KGaA" forms (longest listed first).
        r'co\.,?\s*kgaa', r'co\.,?\s*kg', r'kgaa', r'kg',
        r'ag', r's\.r\.l\.?', r's\.a\.?', r'b\.v\.?',
        r'plc', r'pty\.?\s*ltd\.?',
        r'k\.?k\.?', r'oy', r'ab', r'a/s', r's\.p\.a\.?', r'spa',
        r'oyj', r'asa', r'nv', r'n\.v\.?', r'sas', r's\.a\.s\.?',
        r'co\.', r'company',
        # German registered association / Mexican civil association / Czech &
        # Polish Ltd / professional corp — trailing entity tags, not part of the
        # institution name.
        r'e\.?\s*v\.?', r'a\.?\s*c\.?', r'p\.?\s*c\.?',
        r's\.?\s*r\.?\s*o\.?', r'sp\.?\s*z\s*o\.?\s*o\.?', r's\.l\.?',
    )
    # Require a separator (comma, space, or start) before the designator so it
    # can't chew into a real word — e.g. "s.a." must not match the "sa" in
    # "Tulsa", and "ag" must be a standalone token, not the tail of a word.
    #
    # Between the separator and the designator, optionally consume an "&"/"and"/
    # "und"/"+" connector so a compound trailing tag like the German "GmbH & Co.
    # KG" fully collapses: one pass strips "& Co. KG" (connector + designator),
    # leaving "<Name> GmbH" for the next pass to strip — instead of stranding a
    # dangling "<Name> GmbH &". The connector is only consumed when a real
    # designator follows it, so an internal "&" in a name (e.g. "Johnson &
    # Johnson", "Texas A&M") is never touched.
    pat = re.compile(
        r'(?:^|(?<=[\s,]))[\s,]*(?:(?:&|\+|and|und)[\s,]*)?'
        r'(?:' + '|'.join(designators) + r')\s*$',
        re.IGNORECASE,
    )
    prev = None
    while prev != s:
        prev = s
        s = pat.sub('', s).strip()
    return s


_TRAILING_ACRONYM_RE = re.compile(
    r'^(?P<name>.+?)\s*\(\s*'
    r'(?P<acr>[A-Z][A-Za-z0-9]*[A-Z](?:-[A-Z0-9]+)?)'
    r'\s*\)\.?$'
)


def _trailing_acronym(raw: str) -> str | None:
    """If `raw` is a single clean 'Long Institution Name (ACRONYM)' string,
    return the ACRONYM; otherwise None.

    Fires only for the unambiguous whole-string shape, with guards so it can't
    mangle addresses or splice two organizations together:
      - the acronym is >=3 chars and mostly uppercase, and shorter than the
        name it abbreviates (rejects "(USA)"-style country tails and ordinary
        words);
      - the name part has no comma, no second '(' (a second parenthetical means
        the real unit lives elsewhere, e.g.
        "National Research Council of Italy (CNR) -The Institute…(ISOF)"), and
        no " - " clause splice joining two distinct bodies.
    Internal word hyphens ("Hamburg-Eppendorf", "Technology-Hellas") are fine.
    """
    s = (raw or '').strip()
    m = _TRAILING_ACRONYM_RE.match(s)
    if not m:
        return None
    acr = m.group('acr')
    name = m.group('name').strip()
    if len(acr) < 3:
        return None
    if sum(c.isupper() for c in acr) < 3:
        return None
    if len(acr) >= len(name):
        return None
    # A parenthetical country ("(USA)", "(UK)") is an address tail, not an
    # institution acronym — never treat it as the short label.
    if acr in COUNTRY_TOKENS or acr.upper() in {t.upper() for t in COUNTRY_TOKENS}:
        return None
    if ',' in name or '(' in name:
        return None
    # Reject a spaced-dash clause join (" - ", " – ") that splices two orgs;
    # tolerate tight intra-word hyphens like "Hamburg-Eppendorf".
    if re.search(r'\s[-\u2013\u2014]\s', name):
        return None
    return acr


def fallback_shorten(raw: str) -> str:
    """Algorithmic short name for affiliation strings no anchor matched.

    Strategy: split by commas, drop trailing country/state/zip/city pieces and
    leading department-like pieces, then take the first remaining segment as
    the institution.  Apply "University of X -> U X" if it fits.
    """
    # Self-declared acronym: when the WHOLE string is a single clean
    # "Long Institution Name (ACRONYM)" — e.g.
    # "Foundation for Research and Technology-Hellas (FORTH)",
    # "University Medical Center Hamburg-Eppendorf (UKE)" — the parenthetical
    # acronym is a far better short label than the long name the comma-based
    # logic below would otherwise return verbatim. This only runs after every
    # ANCHOR/LATE_ANCHOR/override has missed (canonicalize() calls
    # fallback_shorten last), so curated short names are never overridden.
    acr = _trailing_acronym(raw)
    if acr:
        return acr

    # Remove any parenthetical (country tail like "(United States)" /
    # "(Korea, Republic of)", or an embedded "(China)") before anything else, so
    # no short name ever carries parentheses and a "(Country, qualifier)" tail
    # can't survive the comma-split below as a stray "Republic of)" fragment.
    raw = _strip_parens(raw)
    raw = _strip_legal_suffix(raw)
    parts = [p.strip() for p in raw.split(',') if p.strip()]

    # Drop trailing region/country/state segments.
    def is_address_tail(p: str) -> bool:
        if p in COUNTRY_TOKENS:
            return True
        if p in US_STATE_TOKENS:
            return True
        if p in COMMON_REGION_TOKENS:
            return True
        # zip-code-like / postal-code-like
        if re.fullmatch(r'\d{4,6}', p):
            return True
        return False

    while parts and is_address_tail(parts[-1]):
        parts.pop()
    # Drop a trailing city segment if there's still a comma-chain (best-effort)
    # — but NOT if that trailing segment is itself clearly an institution.
    # Strings shaped like "Key Laboratory ..., Beijing ... University" leave
    # [Lab, University] after the tail strip; blindly popping the last segment
    # would discard the actual university and leave the lab behind.
    if len(parts) >= 2 and not INSTITUTION_HINT_WORDS.search(parts[-1]):
        parts.pop()

    # Drop leading department-like segments.
    while len(parts) > 1 and (
        DEPT_HINT_WORDS.search(parts[0])
        or parts[0].lower().strip().rstrip('.') in DEPT_BARE_NAMES
    ):
        parts.pop(0)

    if not parts:
        return raw.split(',')[0].strip()

    inst = parts[0]
    # Re-strip a legal/corporate designator that was sitting on the chosen
    # segment rather than at the very end of the raw string. The up-front
    # _strip_legal_suffix only catches designators at the string's end; for
    # "HyperLight Corp., Cambridge, MA, USA" the "Corp." is mid-string and
    # survives until now (it's the tail of parts[0]). Strip it here so
    # "HyperLight Corp." -> "HyperLight", "Metalenz Inc" -> "Metalenz", etc.
    inst = _strip_legal_suffix(inst)
    # Reduce a university designator to its place/proper-noun ("University of
    # Michigan" -> "Michigan", "Konkuk Univ." -> "Konkuk") and strip any
    # remaining stray punctuation.
    inst = _strip_university_word(inst)
    return _final_clean(inst)


# ---------------------------------------------------------------------------
# Raw-key overrides — last resort for cases where the algorithm gets it wrong.
# Empty by default; populate during reconciliation against the existing map.
# ---------------------------------------------------------------------------

RAW_OVERRIDES: dict[str, str] = {
    # Truncated source (no trailing comma, so the 'chin,' anchor misses).
    'University of Science and Technology of Chin': 'USTC',
    # Typo'd standalone string that the 'university of rochester' anchor misses
    # because the misspelling ("Unviersity") breaks the substring match.
    'Unviersity of Rochester': 'Rochester',
    # CNR (Italy's Consiglio Nazionale delle Ricerche) short names arrive with
    # inconsistent hyphenization: some institutes use a spaced hyphen
    # ("CNR - IFN") and others a tight hyphen ("CNR-INO", "CNR-NANO"). Normalize
    # everything to the tight-hyphen scheme, and fold the bare-council labels
    # into "CNR Italy" to match the spelled-out anchor.
    'CNR': 'CNR Italy',
    'CNR - IFN': 'CNR-IFN',
    'CNR - ITAE': 'CNR-ITAE',
    # Bare institutional acronyms that arrive with no surrounding context for
    # any ANCHOR to match. Each is pinned to the same canonical short the
    # longer spelled-out forms resolve to elsewhere, so a single institution
    # renders the same short name regardless of which source string carried it.
    'TUW': 'TU Vienna',
    'INO': 'CNR-INO',
    'C2N': 'C2N Paris-Saclay',
    # Some source forms attach the parent CNRS organisation rather than the
    # specific lab — "CNRS - Université Montpellier" is really the IES
    # (Institut d'Électronique et des Systèmes) Montpellier lab. The bare
    # "CNRS" anchor would otherwise short this to the generic council label;
    # pin it to the specific lab.
    'CNRS - Université Montpellier': 'IES Montpellier',
    # Bare, location-less "Institute of Physics" — in this dataset it is the
    # alt-name of "Institute of Physics, Beijing, ..." (the CAS Institute of
    # Physics, co-affiliated with UCAS on the same talk), not a department of
    # some named university. Exact-match override so it pins ONLY this string
    # and can never grab "Institute of Physics, <University>" forms.
    'Institute of Physics': 'CAS IOP Beijing',
    # The Quantum Science Center is a DOE center headquartered at and led by
    # Oak Ridge National Laboratory, so it canonicalizes to "Oak Ridge" like the
    # spelled-out variants do. Most QSC strings already resolve to Oak Ridge via
    # the Oak Ridge anchor; these two carry no "Oak Ridge National Laboratory"
    # text (so the anchor misses) and would otherwise surface the bare "QSC"
    # acronym or the full string. Pin them to Oak Ridge for consistency.
    'Quantum Science Center (QSC)': 'Oak Ridge',
    'Quantum Science Center (QSC), Oak Ridge, TN, United States': 'Oak Ridge',
}


# ---------------------------------------------------------------------------
# Main canonicalization
# ---------------------------------------------------------------------------

# Curated labels kept verbatim by _polish — the bare place name would be
# ambiguous, so the disambiguating "U " prefix is deliberately preserved.
_POLISH_KEEP = {'U Miami'}


def _polish(short: str) -> str:
    """Enforce house style on a final short label, whatever produced it (anchor,
    override, or fallback). General pass, not a per-string fix:
      - no parentheses ("Konkuk Univ. (Korea" / "X (Country)" never survive);
      - drop the university designator to a place name ("Ain Shams University" ->
        "Ain Shams", "University of Huddersfield" -> "Huddersfield");
      - drop a lone "U " / " U" affix in favor of the place ("U Chicago" ->
        "Chicago", "American U" -> "American"); acronym prefixes like "UC", "UT",
        "TU", "UMass" (no following space) are untouched;
      - strip stray commas/abbreviation dots (hyphens and '&' are kept).
    Idempotent: labels already in good form pass through unchanged."""
    if short.strip() in _POLISH_KEEP:
        return short.strip()
    s = _strip_parens(short)
    # Drop a leading "<Label>:" prefix ("Institute: Amsterdam Medical Center" ->
    # "Amsterdam Medical Center") and a leading lowercase article.
    s = re.sub(r'^[A-Za-z][\w.&-]*:\s+', '', s)
    s = re.sub(r'(?i)^the\s+', '', s)
    s = _strip_university_word(s)
    # Drop any remaining standalone "Univ"/"Univ." abbreviation token (a mid-name
    # one the leading/trailing rules don't reach, e.g. "Azienda Ospedaliera Univ.
    # Careggi"). Only the abbreviation — the full word "University" is left for
    # the College guard above to protect.
    s = re.sub(r'\bUniv\.?\b', '', s, flags=re.IGNORECASE)
    s = _strip_legal_suffix(s)         # "Aloe Semiconductor Inc." -> "Aloe Semiconductor"
    # A standalone "U" token is the "University" abbreviation ("U Chicago",
    # "Leibniz U Hannover", "American U") — drop it for the place name. Multi-
    # letter acronyms ("UC", "NYU", "VU") have no word boundary and are untouched.
    s = re.sub(r'\bU\b', '', s)
    # Expand the "Nat"/"Natl" abbreviation that some short forms carry
    # ("Seoul Nat" -> "Seoul National", "Hefei Natl Lab" -> "Hefei National Lab").
    s = re.sub(r'(?i)\bnatl?\b', 'National', s)
    out = _final_clean(s)
    # Never collapse to nothing: a degenerate input that is only a legal tag
    # ("A.C.") keeps its parenthesis-free, comma-free original rather than ''.
    return out or _final_clean(_strip_parens(short)) or short.strip()


def canonicalize(raw: str) -> str:
    out = _polish(_canonicalize(raw))
    if out.strip():
        return out
    # Degenerate input (only a legal tag like "A.C.") polished to nothing — keep
    # the parenthesis/comma-free original so the label is never empty.
    return _final_clean(_strip_parens(raw)) or raw.strip()


def _canonicalize(raw: str) -> str:
    if raw in RAW_OVERRIDES:
        return RAW_OVERRIDES[raw]
    norm = normalize(raw)
    for needle, short in ANCHORS:
        if _anchor_matches(needle, norm):
            return short
    for needle, short in LATE_ANCHORS:
        if _anchor_matches(needle, norm):
            return short
    result = fallback_shorten(raw)

    # Some sources write affiliations as "Institution (Country)" or
    # "Institution – Country" with NO comma (e.g. "mirSense (France)",
    # "Technical University Vienna (Austria)", "IEMN – France"). The comma-based
    # tail-stripping in fallback_shorten can't see past a glued, comma-less
    # country, so such a string canonicalizes to ITSELF — and the app then shows
    # no short-name chip (a chip is only rendered when short != long). When, and
    # ONLY when, the normal pipeline above failed to shorten the string at all
    # (result == raw, modulo a trailing period), retry on a copy with that
    # trailing country tail removed. This is strictly additive: any string the
    # existing anchors/overrides/fallback already shorten is returned before we
    # get here, so already-curated maps are byte-for-byte unchanged; we
    # only rescue strings that would otherwise have had no short form.
    if _norm_eq_raw(result, raw):
        stripped = _strip_trailing_country(raw)
        if stripped and stripped != raw:
            # Re-canonicalize the country-free string: it may now hit an anchor
            # (e.g. "University of Leeds …" -> "U Leeds") or, at minimum, the
            # bare institution itself is a shorter label than the original
            # "Institution (Country)". Either is an improvement over showing no
            # chip, so prefer the stripped canonicalization whenever it differs
            # from the original raw input.
            retry = canonicalize(stripped)
            if retry and not _norm_eq_raw(retry, raw):
                return retry
    return result


def _norm_eq_raw(short: str, raw: str) -> bool:
    """True when a canonicalization 'short' is really just the input unchanged
    (no shortening happened), ignoring a trailing period and surrounding
    whitespace — the condition under which the app would render no chip."""
    a = (short or "").strip().rstrip(".").strip()
    b = (raw or "").strip().rstrip(".").strip()
    return a == b


# Trailing country tail in the comma-less "(Country)" or "<dash> Country" forms
# that the comma-based fallback can't reach. Built from the existing
# COUNTRY_TOKENS set (defined above) so the two never drift apart. Dash variants
# cover ASCII hyphen and en/em dashes. Anchored to the END of the string.
_COUNTRY_TAIL_RE = re.compile(
    r"\s*(?:\(\s*(?:%s)\s*\)|[\-\u2013\u2014]\s*(?:%s))\s*$" % (
        "|".join(re.escape(c) for c in
                 sorted(COUNTRY_TOKENS, key=len, reverse=True)),
        "|".join(re.escape(c) for c in
                 sorted(COUNTRY_TOKENS, key=len, reverse=True)),
    ),
    re.IGNORECASE,
)


def _strip_trailing_country(raw: str) -> str:
    """Remove a trailing comma-less country tail, e.g.
    'mirSense (France)' -> 'mirSense', 'IEMN – France' -> 'IEMN'. Loops to
    handle a rare doubled tail like '… (NEST) (Italy)' (the parenthetical that
    is NOT a country, like '(NEST)', is left intact)."""
    out = (raw or "").strip()
    while True:
        new = _COUNTRY_TAIL_RE.sub("", out).strip()
        if new == out:
            return out
        out = new


# ---------------------------------------------------------------------------
# Cross-string consolidation of diacritic-only spelling variants.
#
# The same institution often appears under spellings that differ ONLY by
# accents — typically because it's written in different languages/orthographies
# (Spanish "Universidad de Málaga" vs the ASCII "Universidad de Malaga"; Catalan
# "Universitat Politècnica de València" vs Spanish "Universidad Politécnica de
# Valencia"; French "École Polytechnique" vs "Ecole Polytechnique"). Each
# spelling shortens consistently, but to a DIFFERENT label ("Málaga" vs
# "Malaga"), so one institution ends up with two chips.
#
# This pass folds those together: shorts that become byte-identical once their
# diacritics are stripped (CASE PRESERVED) are treated as one institution and
# all remapped to a single winner spelling. Case is deliberately kept in the
# fold key so this NEVER merges labels that differ by case alone — those can be
# genuinely different institutions (e.g. the German acronym "IOM" vs the
# Portuguese company "Iom"), which we must not conflate.
# ---------------------------------------------------------------------------

def _strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s)
                   if not unicodedata.combining(c))


# Every short label any curated anchor / late anchor / raw override can emit.
# A spelling in this set is an intentional, hand-chosen canonical form, so when
# an accent-only group contains one it wins over any fallback-derived spelling
# (protects deliberately-accented labels like "Almería", "Göttingen", "Münster"
# from being de-accented by a colliding fallback string).
_CURATED_SHORTS = ({s for _, s in ANCHORS}
                   | {s for _, s in LATE_ANCHORS})


def _consolidate_accent_variants(mapping: dict[str, str]) -> dict[str, str]:
    """Remap shorts that differ only by diacritics onto one winner spelling.

    Winner per group, by descending preference:
      1. a curated (anchor/override) spelling — the intended canonical form;
      2. the spelling the most raw strings already resolve to (data consensus);
      3. the accented spelling (the native, more-correct orthography);
      4. the longer spelling, then alphabetical — purely for determinism.
    No-op for any conference whose shorts have no accent-only collisions, so
    maps without such variants are byte-for-byte unchanged.
    """
    from collections import Counter, defaultdict
    counts = Counter(mapping.values())            # raw strings per short label
    curated = _CURATED_SHORTS | set(RAW_OVERRIDES.values())
    groups: dict[str, set] = defaultdict(set)
    for short in set(mapping.values()):
        groups[_strip_accents(short)].add(short)

    winner_of: dict[str, str] = {}
    for variants in groups.values():
        if len(variants) < 2:
            continue
        winner = max(variants, key=lambda s: (
            s in curated, counts[s], s != _strip_accents(s), len(s), s))
        for v in variants:
            if v != winner:
                winner_of[v] = winner
    if not winner_of:
        return mapping
    return {raw: winner_of.get(short, short) for raw, short in mapping.items()}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def build(data: dict | list, out_txt: Path | None = None) -> dict[str, str]:
    """Build the raw-affiliation -> short-name mapping.

    `data` is the dict the processor bundles into conference_data.json. This
    reads only its "affiliation_sources" value: a single flat list of raw
    affiliation strings (full-address lines, presider affiliations, and
    institution names all pooled and de-duplicated by the processor, which also
    splits any ';'-joined lists at the source). Each distinct string is
    canonicalized into its short label.

    Callers may pass either the whole JSON (a dict with an "affiliation_sources"
    list) or the bare list itself.

    Verbose by design: the canonicalization is summarized on stdout, prefixed
    `[affil]` to match the convention build_conference_app.py uses for
    affiliation-related logs.

    Side effect: writes the mapping as a tab-separated text file. By default
    it lands at ``affiliation_map.txt`` in the current directory; pass
    ``out_txt`` to override. The file is small and the caller usually wants
    it on disk for inspection.
    """
    print('[affil] building map from the processor data JSON')
    if isinstance(data, dict):
        sources = data.get('affiliation_sources') or []
    else:
        # The bare affiliation_sources list passed directly.
        sources = data or []
    affils: set[str] = {s.strip() for s in sources if s and s.strip()}

    print(f'[affil]   canonicalizing {len(affils):,} unique raw strings…')
    mapping = {k: canonicalize(k) for k in sorted(affils)}

    # How many of the raw strings landed in the curated anchors/overrides
    # vs. fell all the way through to fallback_shorten? Computed BEFORE the
    # accent-consolidation below so the stat reflects the canonicalizer itself.
    n_fallback = sum(1 for k, v in mapping.items() if v == fallback_shorten(k))

    # Fold together shorts that differ only by diacritics (the same institution
    # written in different languages/orthographies), so one institution renders
    # one chip rather than several accent-variant chips.
    before = dict(mapping)
    mapping = _consolidate_accent_variants(mapping)
    n_merged = sum(1 for k in mapping if mapping[k] != before[k])
    if n_merged:
        print(f'[affil]   consolidated {n_merged} affiliation(s) onto an '
              f'accent-variant canonical spelling')
    n_short = len(set(mapping.values()))
    n_anchored = len(mapping) - n_fallback
    print(f'[affil]   {n_anchored:,} matched a curated anchor; '
          f'{n_fallback:,} used the fallback shortener')
    print(f'[affil] built map: {len(mapping):,} raw -> {n_short:,} short names')

    if out_txt is None:
        out_txt = Path('affiliation_map.txt')
    try:
        write_text(mapping, out_txt)
        print(f'[affil] wrote {out_txt}')
    except OSError as e:
        # Don't fail the whole build_conference_app run just because we couldn't
        # drop the txt file (read-only volume, permission issue, etc.) —
        # the in-memory mapping is what the caller actually needs.
        print(f'[affil] (could not write {out_txt}: {e})')

    return mapping


def write_text(mapping: dict[str, str], out: Path) -> None:
    """Write the mapping as a tab-separated text file.

    Format:
      # header comment lines
      <raw_affiliation>\t<canonical_short_name>
      ...

    Tab is used as separator (rather than comma) because the raw affiliation
    keys themselves contain many commas. Sorted alphabetically by key.
    """
    lines = [
        '# Mapping from raw conference affiliation strings to canonical short names.',
        f'# Auto-generated. {len(mapping)} unique affiliation strings -> '
        f'{len(set(mapping.values()))} canonical short names.',
        '# Format: <raw_affiliation>\\t<canonical_short_name>',
        '',
    ]
    for k in sorted(mapping):
        # Defensive: replace any embedded tabs/newlines in the raw key with spaces.
        kk = k.replace('\t', ' ').replace('\n', ' ')
        vv = mapping[k].replace('\t', ' ').replace('\n', ' ')
        lines.append(f'{kk}\t{vv}')
    out.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main() -> None:
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default='conference_data.json',
                    help='Path to the data JSON the processor produces.')
    ap.add_argument('--out', default=None,
                    help='Where to write the affiliation map text file. '
                         'Defaults to affiliation_map.txt in the cwd.')
    args = ap.parse_args()
    with open(args.data, encoding='utf-8') as f:
        data = json.load(f)
    out = Path(args.out) if args.out else None
    build(data, out_txt=out)


if __name__ == '__main__':
    main()