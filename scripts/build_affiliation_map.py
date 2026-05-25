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
"affiliation_sources" block. This module reads ONLY that JSON; the processor
does all of the upstream scraping/parsing. The block holds three lists of raw
strings, which differ only in where the processor harvested them — they are
treated identically once read (pooled together, de-duplicated, and each
becomes a key to canonicalize):
  - affiliation_sources["affiliation_full_lines"]
        Long, multi-field postal-address lines, e.g.
        "4th Physical Institute, University of Göttingen, Göttingen, Germany".
        Used whole (not split).
  - affiliation_sources["presider_affiliation_strings"]
        Affiliations of session presiders, usually already short, e.g.
        "KAUST" or "University of Florence". A single string may pack several
        affiliations separated by ";", so these are split on ";" before use.
  - affiliation_sources["institution_strings"]
        Institution names the processor pre-extracted, usually already fairly
        short, e.g. "North Carolina State University". Like the presider
        strings, one entry may be a ";"-separated list, so these are also
        split on ";". They mostly duplicate names already present in the
        full-address lines, but are included anyway because they occasionally
        contribute an institution that never appears in a full-address line.

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

# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
# NOTE: this module reads only the source-agnostic affiliation_sources block of
# the processor's data JSON. The processor does all upstream data gathering and
# bundles the full-address affiliation lines into "affiliation_full_lines", so
# this module only consumes that JSON.


def extract_presider_affiliations(strings: list[str]) -> set[str]:
    """Pull every presider affiliation out of the presider-affiliation strings.

    The values are short forms like ``KAUST``, ``University of Florence``,
    ``DTU Copenhagen``, ``Trinity College Dunlin`` (note the typo), each
    possibly a semicolon-separated list of several affiliations.

    `strings` is the list of presider-affiliation strings bundled in the data
    JSON under affiliation_sources["presider_affiliation_strings"].
    """
    out: set[str] = set()
    for v in strings:
        for piece in (v or '').split(';'):
            p = piece.strip()
            if p:
                out.add(p)
    return out


def extract_institutions(strings: list[str]) -> set[str]:
    """Pull every institution value out of the institution strings.

    These are semicolon-separated short forms like
    ``Hewlett Packard Enterprise; North Carolina State University``.
    They are usually duplicates of the full-address short forms but
    occasionally add something the address lines don't (e.g. when no
    full-address line is generated).

    `strings` is the list of institution strings bundled in the data JSON
    under affiliation_sources["institution_strings"].
    """
    out: set[str] = set()
    for v in strings:
        for piece in (v or '').split(';'):
            p = piece.strip()
            if p:
                out.add(p)
    return out


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize(s: str) -> str:
    """Lowercase + collapse whitespace.

    Used only for matching against the anchor patterns; the raw key is what
    actually goes into the output dict.
    """
    s = unicodedata.normalize('NFKC', s)
    s = s.lower()
    # Normalize common MISSPELLINGS of the English word "university" up front,
    # so every downstream anchor/fallback sees the canonical word and we don't
    # need a bespoke anchor per typo. Word-boundary anchored and limited to an
    # explicit list of unambiguous English-typo spellings, so it never touches
    # real foreign forms (universidad, universita`/università, universitat/
    # universität, universite/universite', universidade, universitet,
    # universiteit) nor the legit abbreviations (univ, the "universit" stem).
    s = re.sub(r'\buniversity?of\b', 'university of', s)   # "universityof" (missing space)
    s = re.sub(r'\b(?:univeristy|univerisity|univrsity|universty|'
               r'universiy|universityy|universitry|universitity|univerce)\b',
               'university', s)
    # Same idea for misspellings of "technology" that sit in an institution
    # token an anchor keys on (e.g. "...Science and Technogy" -> KIST,
    # "...Science and Techcnology" -> SUSTech). Explicit list, word-boundary
    # anchored, so it never touches the legit forms (technology, technologies,
    # technological, technische, technical, tech, technion). (Note: "technsche"
    # is a typo of German "technische", not "technology", so it's excluded —
    # it already resolves correctly via the PTB anchor.)
    s = re.sub(r'\b(?:technogy|techcnology|techenology|technologygy|technolog)\b',
               'technology', s)
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

ANCHORS: list[tuple[str, str]] = [
    # ---- US national labs (specific names before generic) ------------------
    ('los alamos national lab', 'LANL'),
    ('lawrence livermore', 'LLNL'),
    ('lawrence berkeley', 'LBNL'),
    ('oak ridge national lab', 'ORNL'),
    ('pacific northwest national', 'PNNL'),
    ('brookhaven', 'Brookhaven'),
    ('argonne', 'Argonne'),
    ('sandia nat. laboratories', 'Sandia'),
    ('sandia', 'Sandia'),
    # CINT (Center for Integrated Nanotechnologies) is a Sandia/Los Alamos
    # user facility; map it to Sandia. Some CINT strings carry no "Sandia"
    # token (just the center name + city), so they'd otherwise fall through
    # to the fallback shortener and keep the long center name.
    ('center for integrated nanotechnologies', 'Sandia'),
    ('air force research laboratory', 'AFRL'),
    ('air force research lab,', 'Air Force Research Lab'),  # truncated form → verbatim per existing
    ('afrl munitions', 'AFRL Munitions Directorate'),
    ('afrl,', 'AFRL'),
    ('naval research lab', 'NRL'),
    ('naval surface warfare', 'Naval Surface Warfare Center'),
    ('naval air warfare', 'Naval Air Warfare Center'),
    ('mit lincoln', 'MIT Lincoln Lab'),
    ('mitll', 'MIT Lincoln Lab'),
    ('lincoln laboratory, massachusetts institute', 'MIT Lincoln Lab'),
    ('lincoln laboratory, mit', 'MIT Lincoln Lab'),
    ('massachusetts inst of tech lincoln lab', 'MIT Lincoln Lab'),  # abbreviated short-form variant
    ('mitre', 'MITRE'),
    (r're:\bnist\b', 'NIST'),  # \b so it doesn't fire inside "miNISTry"
    ('national institute of standards and technology', 'NIST'),
    # Abbreviated short-forms from the institution strings. The anchors above
    # only match the fully spelled-out name ("Institute" / "and"); these catch
    # the "Inst" / "&" contractions.
    ('national inst of standards', 'NIST'),
    ('national institute of standards', 'NIST'),  # "...& Technology" variant
    ('jet propulsion lab', 'NASA JPL'),
    ('nasa jpl', 'NASA JPL'),
    ('jpl,', 'JPL'),
    ('jpl', 'JPL'),
    ('nasa goddard', 'NASA Goddard'),
    ('nasa', 'NASA'),
    ('noaa', 'NOAA'),
    ('slac', 'SLAC'),
    ('jila', 'JILA'),
    (r're:\blle,\s+rochester\b', 'Rochester'),
    ('laboratory for laser energetics', 'Rochester'),
    ('darpa mto', 'DARPA'),
    ('darpa', 'DARPA'),
    ('army research lab', 'ARL'),
    (r're:\barl\b', 'ARL'),
    ('us army ccdc', 'US Army CCDC'),
    ('devcom', 'DEVCOM'),
    ('hrl', 'HRL'),
    ('draper', 'Draper'),
    ('jhu/apl', 'JHU APL'),
    ('johns hopkins applied physics', 'JHU APL'),

    # ---- US elite private universities ------------------------------------
    ('massachusetts institute of technology', 'MIT'),
    (', mit,', 'MIT'),
    (r're:\bmit,', 'MIT'),  # \b so it doesn't fire inside "RMIT," (Melbourne)
    # Tolerate the 'Insttute' (missing-i) misspelling seen in the ground
    # truth, consistent with the typo-absorbing philosophy above.
    (r're:california inst[i]?tute of technology', 'Caltech'),
    ('caltech', 'Caltech'),
    ('stanford', 'Stanford'),
    ('harvard', 'Harvard'),
    ('princeton', 'Princeton'),
    ('yale', 'Yale'),
    ('cornell', 'Cornell'),
    ('columbia university', 'Columbia'),
    ('columbia,', 'Columbia'),
    ('university of pennsylvania', 'UPenn'),
    ('upenn', 'UPenn'),
    ('brown university', 'Brown'),
    ('dartmouth', 'Dartmouth'),
    ('johns hopkins', 'Johns Hopkins'),
    ('duke university', 'Duke'),
    ('vanderbilt', 'Vanderbilt'),
    ('vandertbilt', 'Vanderbilt'),
    ('northwestern polytechnical', 'NWPU'),
    ('northwestern university', 'Northwestern'),
    ('northwestern', 'Northwestern'),
    ('northeastern university', 'Northeastern'),
    ('northeastern', 'Northeastern'),
    ('carnegie mellon', 'Carnegie Mellon'),
    ('rice university', 'Rice'),
    ('baylor university', 'Baylor'),
    ('baylor', 'Baylor'),
    ('washington university', 'WashU'),
    ('washu', 'WashU'),
    ('tufts', 'Tufts'),
    ('tulane', 'Tulane'),
    ('emory', 'Emory'),
    ('university of chicago', 'U Chicago'),
    ('boston university', 'BU'),
    ('new york university', 'NYU'),
    ('nyu', 'NYU'),
    ('george washington', 'George Washington'),
    ('american university', 'American U'),

    # ---- UC system (specific campus before the generic word) --------------
    ('university of california, berkeley', 'UC Berkeley'),
    ('university of california berkeley', 'UC Berkeley'),
    ('uc berkeley', 'UC Berkeley'),
    ('university of california, irvine', 'UC Irvine'),
    ('university of california at irvine', 'UC Irvine'),
    ('university of california irvine', 'UC Irvine'),
    ('uc irvine', 'UC Irvine'),
    ('university of california, riverside', 'UC Riverside'),
    ('university of california riverside', 'UC Riverside'),
    ('uc riverside', 'UC Riverside'),
    ('university of california, san diego', 'UC San Diego'),
    ('university of california san diego', 'UC San Diego'),
    ('uc san diego', 'UC San Diego'),
    ('university of california, santa barbara', 'UC Santa Barbara'),
    ('university of california at santa barbara', 'UC Santa Barbara'),
    ('university of california santa barbara', 'UC Santa Barbara'),
    ('uc santa barbara', 'UC Santa Barbara'),
    ('ucsb', 'UC Santa Barbara'),
    ('university of california, davis', 'UC Davis'),
    ('university of california at davis', 'UC Davis'),
    ('university of california davis', 'UC Davis'),
    ('uc davis', 'UC Davis'),
    ('university of california, los angeles', 'UCLA'),
    ('university of california los angeles', 'UCLA'),
    # Tolerate the 'Califonia' (missing-r) misspelling seen in the input,
    # consistent with the typo-absorbing philosophy. Regex covers the comma
    # and no-comma forms; must precede the bare "university of california"
    # fallback (which never matches anyway since it's spelled correctly there).
    ('re:university of califo[r]?nia,?\\s*los angeles', 'UCLA'),
    ('ucla', 'UCLA'),
    ('university of california, merced', 'UC Merced'),
    ('university of california, santa cruz', 'UC Santa Cruz'),
    ('university of california', 'UC'),  # fallback bare form
    ('university of southern california', 'USC'),
    (' usc,', 'USC'),

    # ---- Other big US state schools ---------------------------------------
    # UMBC has irregular ground-truth treatment; most variants → UMBC, but
    # specific RAW_OVERRIDES preserve the verbatim/Maryland exceptions.
    ('university of maryland baltimore county', 'UMBC'),
    ('umbc', 'UMBC'),
    ('laboratory for physical sciences, college park', 'LPS Maryland'),
    ('laboratory for telecommunication science', 'LPS Maryland'),
    ('lps maryland', 'LPS Maryland'),
    # IREAP and the Institute for Physical Science and Technology are
    # both at Maryland College Park.
    ('institute for research in electronics', 'Maryland'),
    ('institute for physical science and technology', 'Maryland'),
    ('university of maryland', 'Maryland'),
    ('university of michigan', 'Michigan'),
    ('university of texas at austin', 'UT Austin'),
    ('ut austin', 'UT Austin'),
    ('the university of texas at austin', 'UT Austin'),
    # Comma-form variants: "University of Texas, Austin" (same campus, just
    # written with a comma instead of "at"). Regex tolerates the comma +
    # whitespace. Must precede any generic "University of Texas" fallback so
    # these don't degrade to a campus-less "U Texas".
    ('re:university of texas,\\s*austin', 'UT Austin'),
    ('university of texas at dallas', 'UT Dallas'),
    ('re:university of texas,\\s*dallas', 'UT Dallas'),
    ('ut dallas', 'UT Dallas'),
    ('university of central florida', 'UCF'),
    ('ucf,', 'UCF'),
    ('creol', 'UCF'),
    ('university of florida', 'Florida'),
    ('university of arizona', 'Arizona'),
    ('re:univ\\.? of arizona', 'Arizona'),  # "Univ of Arizona" / "Univ. of Arizona"
    ('wyant college', 'Wyant College of Optical Sciences'),
    ('arizona state university', 'ASU'),
    ('asu,', 'ASU'),
    ('northern arizona university', 'Northern Arizona University'),
    ('university of colorado boulder', 'CU Boulder'),
    ('cu boulder', 'CU Boulder'),
    ('university of colorado, boulder', 'CU Boulder'),
    ('university of colorado', 'Colorado'),
    ('colorado school of mines', 'Colorado School of Mines'),
    ('university of washington', 'UW Seattle'),
    ('uw seattle', 'UW Seattle'),
    ('university of wisconsin', 'UW-Madison'),
    ('uw-madison', 'UW-Madison'),
    ('university of illinois urbana champaign', 'UIUC'),
    ('university of illinois at urbana-champaign', 'UIUC'),
    ('university of illinois urbana-champaign', 'UIUC'),
    ('university of illinois at urbana champaign', 'UIUC'),
    # Misspelling guard: catch any "Illinois … Urbana … Champa{ign,gne,…}"
    # spelling (the data carries a "Urbana Champagne" typo) so it still lands on
    # UIUC instead of falling through to the fallback shortener. Requires both
    # "urbana" and a "champa…" token, so it can't fire on "University of
    # Illinois Chicago" or the bare "University of Illinois".
    (r're:university of illinois.*\burbana\b.*\bchampa', 'UIUC'),
    ('university of illinois,', 'UIUC'),
    ('univ of illinois at urbana', 'UIUC'),  # abbreviated short-form variant
    ('university of illinois at chicago', 'UIC'),
    ('uic,', 'UIC'),
    ('uiuc', 'UIUC'),
    ('purdue', 'Purdue'),
    ('university of minnesota', 'Minnesota'),
    ('michigan state', 'Michigan State'),
    ('michigan technological', 'Michigan Tech'),
    ('ohio state', 'Ohio State'),
    ('penn state', 'Penn State'),
    ('pennsylvania state', 'Penn State'),
    ('north carolina state', 'NC State'),
    ('university of north carolina at charlotte', 'UNC Charlotte'),
    ('university of north carolina charlotte', 'UNC Charlotte'),
    ('unc charlotte', 'UNC Charlotte'),
    ('univ of north carolina at charlotte', 'UNC Charlotte'),  # abbreviated short-form variant
    ('north carolina agricultural and technical state', 'NC A&T'),
    ('north caorlina agriculture and technology', 'NC A&T'),  # typo
    ('georgia institute of technology', 'Georgia Tech'),
    ('georgia tech', 'Georgia Tech'),
    ('virginia polytechnic', 'Virginia Tech'),
    ('virginia tech', 'Virginia Tech'),
    ('university of virginia', 'UVA'),
    (', uva,', 'UVA'),
    ('university of pittsburgh', 'Pittsburgh'),
    ('pittsburgh, ', 'Pittsburgh'),  # weak; only late
    ('pennsylvania state university', 'Penn State'),
    ('rensselaer', 'RPI'),
    ('rpi,', 'RPI'),
    ('rochester institute of technology', 'RIT'),
    (', rit,', 'RIT'),
    ('university of rochester', 'Rochester'),
    ('university of rochester lle', 'Rochester'),
    ('institute of optics, university of rochester', 'Rochester'),
    ('the institute of optics, university of rochester', 'Rochester'),
    ('the institute of optics,', 'Rochester'),
    ('laboratory of laser and energetics', 'Rochester'),  # misspelled variant
    ('sydor technologies', 'Sydor'),
    ('vpiphotonics', 'VPIphotonics'),
    ('photonect', 'Photonect'),
    ('texas a&m', 'Texas A&M'),
    ('texas tech', 'Texas Tech'),
    ('university of houston', 'Houston'),
    ('university of oklahoma', 'U Oklahoma'),
    ('university of arkansas', 'Arkansas'),
    ('university of alabama', 'U Alabama'),
    ('auburn', 'Auburn'),
    ('clemson', 'Clemson'),
    ('university of tennessee', 'U Tennessee'),
    ('university of louisiana at lafayette', 'U Louisiana Lafayette'),
    ('university of louisiana lafayette', 'U Louisiana Lafayette'),
    ('university of missouri', 'Missouri'),
    ('university of iowa', 'Iowa'),
    ('university of utah', 'Utah'),
    ('university of idaho', 'Idaho'),
    ('university of hawaii', 'Hawaii'),
    ('university of miami', 'U Miami'),
    ('university of connecticut', 'Connecticut'),
    ('university of delaware', 'Delaware'),
    ('delaware state', 'Delaware State'),
    ('university of north texas', 'U North Texas'),
    ('university of new mexico', 'UNM'),
    ('unm,', 'UNM'),
    ('umass amherst', 'UMass Amherst'),
    ('umass lowell', 'UMass Lowell'),
    ('university of massachusetts amherst', 'UMass Amherst'),
    ('university of massachusetts lowell', 'UMass Lowell'),
    ('university of massachusetts', 'UMass'),
    ('umass', 'UMass'),
    ('stony brook', 'SUNY Stony Brook'),
    ('university at albany', 'SUNY Albany'),
    ('suny albany', 'SUNY Albany'),
    # CUNY: all "CUNY" variants → CUNY per ground truth; the bare
    # "Physics and Astronomy, College of Staten Island, Staten Island, NY"
    # (no CUNY in string) maps to 'Staten Island' via the LATE anchor.
    ('cuny advanced science research center', 'CUNY'),
    ('cuny,', 'CUNY'),
    ('cuny graduate center', 'CUNY'),
    (', cuny,', 'CUNY'),
    ('the graduate center,', 'CUNY'),
    ('graduate center cuny', 'CUNY'),
    ('graduate center of the city university of new york', 'CUNY'),
    ('city university of new york', 'CUNY'),
    ('city college of new york', 'CCNY'),
    ('rutgers', 'Rutgers'),
    ('stevens', 'Stevens'),
    ('syracuse', 'Syracuse'),
    ('university of indiana', 'IU Bloomington'),
    ('iu bloomington', 'IU Bloomington'),
    ('indiana university bloomington', 'IU Bloomington'),
    ('indiana university,', 'IU Bloomington'),
    ('indiana university ', 'IU Bloomington'),
    ('oregon state university', 'Oregon State'),
    ('washington state university', 'Washington State'),
    ('florida international university', 'FIU'),
    ('florida polytechnic university', 'Florida Polytechnic'),
    ('florida state university', 'FSU'),
    ('mcgill', 'McGill'),
    ('mcmaster', 'McMaster'),
    ('university of toronto', 'U Toronto'),
    ('university of ottawa', 'U Ottawa'),
    ('université laval', 'Laval'),
    ('universite laval', 'Laval'),
    ('université de montréal', 'U Montreal'),
    ('university of montreal', 'U Montreal'),
    ('university of waterloo', 'Waterloo'),
    ('university of alberta', 'U Alberta'),
    ('université de sherbrooke', 'Sherbrooke'),
    ('university of sherbrooke', 'Sherbrooke'),
    ('institut national de la recherche scientifique', 'INRS'),
    ('inrs-emt', 'INRS-EMT'),
    ('inrs ', 'INRS'),
    ('inrs,', 'INRS'),
    ('university of calgary', 'Calgary'),
    ('simon fraser', 'SFU'),
    ('university of queensland', 'Queensland'),
    ('queens college', 'CUNY'),
    (r"re:queen[’']s university", 'Queen’s University'),
    ('concordia', 'Concordia'),
    ('lakehead', 'Lakehead University'),
    ('polytechnique montreal', 'Polytechnique Montreal'),
    ('polytechnique montréal', 'Polytechnique Montreal'),
    ('école polytechnique de montréal', 'Polytechnique Montreal'),
    ('ecole polytechnique de montreal', 'Polytechnique Montreal'),

    # ---- US "private mid-major" + research orgs ---------------------------
    ('boeing', 'Boeing'),
    ('apple inc', 'Apple'),
    ('apple,', 'Apple'),
    ('google', 'Google'),
    ('meta platforms', 'Meta'),
    ('meta,', 'Meta'),
    ('meta inc', 'Meta'),
    ('microsoft', 'Microsoft'),
    ('amazon', 'Amazon'),
    ('intel ', 'Intel'),
    ('intel,', 'Intel'),
    ('nvidia', 'Nvidia'),  # most are lowercase nv...; existing map also has NVDIA typo - handle specifically below
    ('nvdia', 'NVDIA'),
    ('ibm', 'IBM'),
    ('hewlett packard enterprise', 'HPE'),
    ('hpe labs belgium', 'HPE Labs Belgium'),
    ('hpe labs', 'HPE Labs'),
    ('hpe,', 'HPE'),
    ('hewlett-packard', 'HP'),
    ('cisco', 'Cisco'),
    ('nokia bell labs', 'Nokia Bell Labs'),
    ('bell labs', 'Bell Labs'),
    ('nokia', 'Nokia'),
    ('honeywell', 'Honeywell'),
    ('northrop grumman', 'Northrop Grumman'),
    ('coherent corp', 'Coherent'),
    ('coherent,', 'Coherent'),
    ('thorlabs', 'Thorlabs'),
    ('newport', 'Newport'),
    ('corning', 'Corning'),
    ('amentum,', 'Amentum'),
    ('lumentum', 'Lumentum'),
    ('mentum,', 'Lumentum'),
    ('lam research', 'Lam Research Corporation'),
    ('thermo fisher', 'Thermo Fisher Scientific'),
    ('global foundries', 'GlobalFoundries'),
    ('globalfoundries', 'GlobalFoundries'),
    ('broadcom', 'Broadcom'),
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
    ('center for microsystem technology', 'imec'),
    ('imec', 'imec'),

    # ---- UK ----------------------------------------------------------------
    ('imperial college london', 'Imperial'),
    ('university of oxford', 'Oxford'),
    ('university of cambridge', 'Cambridge'),
    ('cambridge university', 'Cambridge'),
    ('university college london', 'UCL'),
    (', ucl,', 'UCL'),
    ('king\'s college london', 'King’s College London'),
    ('king’s college london', 'King’s College London'),
    ('heriot-watt', 'Heriot-Watt'),
    ('heriot watt', 'Heriot-Watt'),
    ('university of glasgow', 'Glasgow'),
    ('glasgow university', 'Glasgow'),
    ('university of strathclyde', 'Strathclyde'),
    ('strathclyde', 'Strathclyde'),
    ('university of edinburgh', 'U Edinburgh'),
    ('university of southampton', 'Southampton'),
    ('university of bristol', 'Bristol'),
    ('university of bath', 'Bath'),
    ('university of birmingham', 'Birmingham'),
    ('university of manchester', 'U Manchester'),
    ('university of sheffield', 'Sheffield'),
    ('university of exeter', 'Exeter'),
    ('university of york', 'U York'),
    ('university of surrey', 'Surrey'),
    ('university of huddersfield', 'University of Huddersfield'),
    ('cardiff', 'Cardiff'),
    # Aston: ground truth varies. "Aston Institute of Photonics Technologies, Aston University" → Aston;
    # standalone "Aston Institute of Photonics Technologies, Birmingham" → Aston U;
    # the odd "Aston university, B, United Kingdom" → Aston (RAW_OVERRIDE).
    ('aston institute of photonic technologies, aston university', 'Aston'),
    ('aston institute of photonics technologies, aston university', 'Aston'),
    ('aston university', 'Aston U'),
    ('aston institute of photonic', 'Aston U'),  # other Aston Institute variants
    ('aston,', 'Aston'),
    ('loughborough', 'Loughborough'),
    ('plymouth', 'Plymouth'),
    ('national physical lab', 'NPL UK'),
    ('npl,', 'NPL UK'),
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
    ('max planck institute for the science of light', 'MPI Light'),
    ('max-planck institute for the science of light', 'MPI Light'),
    ('max-planck-inst physik des lichts', 'MPI Light'),  # German name for Science of Light
    ('max planck institute of microstructure', 'MPI Microstructure'),
    ('max planck institute for multidisciplinary sciences', 'MPI Multidisc Sci'),
    ('max-planck-institut für quantenoptik', 'MPQ'),
    ('max planck institute of quantum optics', 'MPQ'),
    ('mpq,', 'MPQ'),
    ('max plank for multidisciplinary sciences', 'MPI Multidisc Sci'),
    ('max born institute', 'Max Born'),
    ('max-born-institut', 'Max Born'),
    ('max planck', 'Max Planck'),
    ('fraunhofer hhi', 'Fraunhofer HHI'),
    ('fraunhofer ilt', 'Fraunhofer ILT'),
    ('fraunhofer ims', 'Fraunhofer IMS'),
    ('fraunhofer iof', 'Fraunhofer IOF'),
    ('fraunhofer', 'Fraunhofer'),
    ('forschungszentrum jülich', 'Forschungszentrum Jülich'),
    ('forschungszentrum julich', 'Forschungszentrum Jülich'),
    ('jülich-aachen', 'Jülich-Aachen Research Alliance'),
    ('peter grünberg institute', 'Peter Grünberg Institute (PGI-6)'),
    ('helmholtz center dresden-rossendorf', 'Helmholtz Center Dresden-Rossendorf'),
    ('hzdr', 'HZDR'),
    ('helmholtz', 'Helmholtz Jena'),
    ('rwth aachen', 'RWTH Aachen'),
    ('lmu munich', 'LMU Munich'),
    ('ludwig-maximilians', 'LMU Munich'),
    ('ludwig maximilians', 'LMU Munich'),
    ('ludwig maximilian university', 'LMU Munich'),
    ('ludwig-maximilian-universität', 'LMU Munich'),
    ('technical university of munich', 'TU Munich'),
    ('technische universität münchen', 'TU Munich'),
    ('tu munich', 'TU Munich'),
    ('technische universität berlin', 'TU Berlin'),
    ('technical university of berlin', 'TU Berlin'),
    ('tu berlin', 'TU Berlin'),
    ('technische universität darmstadt', 'TU Darmstadt'),
    ('technical university of darmstadt', 'TU Darmstadt'),
    ('tu darmstadt', 'TU Darmstadt'),
    ('technische universität dresden', 'TU Dresden'),
    ('tu dresden', 'TU Dresden'),
    ('technische universität dortmund', 'TU Dortmund'),
    ('karlsruhe institute of technology', 'KIT'),
    ('kit,', 'KIT'),
    ('humboldt-universität', 'Humboldt'),
    ('humboldt universität', 'Humboldt'),
    ('humboldt university', 'Humboldt'),
    ('friedrich-schiller-universität jena', 'Jena'),
    ('friedrich-schiller', 'Jena'),
    ('friedrich schiller university jena', 'Jena'),
    ('friedrich schiller university,', 'Jena'),
    ('friedrich schiller', 'Jena'),
    ('iap jena', 'IAP Jena'),
    ('university of jena', 'Jena'),
    ('university of stuttgart', 'Stuttgart'),
    ('institute for microelectronics stuttgart', 'Institute for Microelectronics Stuttgart'),
    ('si stuttgart instruments', 'SI Stuttgart Instruments GmbH'),
    ('siloriX', 'SilOriX'),
    ('university of göttingen', 'Göttingen'),
    ('university of mainz', 'Mainz'),
    ('university of regensburg', 'Regensburg'),
    ('regensburg center for ultrafast', 'Regensburg Center for Ultrafast Nanoscopy'),
    ('university of kassel', 'Kassel'),
    ('university of hannover', 'Leibniz U Hannover'),
    ('leibniz university hannover', 'Leibniz U Hannover'),
    ('leibniz universität hannover', 'Leibniz U Hannover'),
    ('leibniz-institut für oberflächenmodifizierung', 'Leibniz-Institut für Oberflächenmodifizierung e.V'),
    ('cluster of excellence phoenixd', 'Cluster of Excellence PhoenixD'),
    ('laser zentrum hannover', 'Laser Zentrum Hannover e.V'),
    ('university of duisburg-essen', 'Duisburg-Essen'),
    ('universität hamburg', 'Universitat Hamburg'),
    ('university of hamburg', 'Universitat Hamburg'),
    ('european xfel', 'European XFEL'),
    ('european x-ray free electron laser', 'European XFEL'),
    ('xfel', 'XFEL'),
    ('desy', 'DESY'),
    ('cycle gmbh', 'Cycle GmbH'),
    ('picoquant', 'PicoQuant GmbH'),
    ('swabian instruments', 'Swabian Instruments'),
    ('swabian instruments', 'Swabian Instruments'),
    ('toptica photonics', 'Toptica'),
    ('trumpf', 'Trumpf'),
    ('bosch', 'Bosch'),
    ('marvel fusion', 'MARVEL Fusion GmbH'),
    ('mpi corporation', 'MPI Corporation'),
    ('mpi light', 'MPI Light'),
    ('weierstraß-institut', 'WIAS Berlin'),
    ('weierstrass institute', 'WIAS Berlin'),
    ('wias berlin', 'WIAS Berlin'),
    ('paderborn', 'Paderborn'),
    ('münster', 'Münster'),
    ('university of münster', 'Münster'),
    ('westfälische wilhelms', 'Münster'),
    ('saot', 'SAOT Erlangen'),
    # FAU Erlangen has several short names in the existing map; keep them
    ('friedrich-alexander', 'Erlangen-Nürnberg'),
    ('friedrich alexander', 'Friedrich Alexander University'),  # rarer "no-hyphen"
    ('friedrich–alexander', 'Erlangen-Nürnberg'),  # en-dash variant
    ('fau,', 'FAU Erlangen'),
    ('ihp gmbh', 'IHP'),
    ('chemnitz', 'Chemnitz University of Technology'),
    ('brandenburgische technische', 'BTU Cottbus'),
    ('rheinland-pfälzische', 'Rheinland-Pfälzische Technische Universität'),
    ('christian-albrechts-universität', 'Kiel'),
    ('christian-albrechts', 'Kiel'),
    ('kiel university', 'Kiel'),
    ('university of kiel', 'Kiel'),
    ('otto-von-guericke', 'Otto-von-Guericke-Universitat Magdeburg'),
    ('fbh', 'FBH'),
    ('ferdinand-braun-institut', 'FBH'),
    ('physikalisch-technische bundesanstalt', 'PTB'),
    ('physikalisch-technsche', 'PTB'),
    ('cluster of excellence', 'Cluster of Excellence PhoenixD'),
    ('dlr,', 'DLR'),
    ('german aerospace center', 'DLR'),
    ('gsi helmholtz centre', 'GSI Helmholtz Centre for Heavy Ion Research'),
    ('deeplight', 'DeepLight S.A./GmbH'),

    # ---- Switzerland -------------------------------------------------------
    ('eth zurich', 'ETH Zurich'),
    ('eth zürich', 'ETH Zurich'),
    (', eth,', 'ETH'),
    ('eidgenössische technische hochschule', 'ETH Zurich'),
    # All of these are EPFL in Lausanne, Switzerland — distinct from
    # France's École Polytechnique (Paris) and Polytechnique Montréal.
    ('école polytechnique fédérale de lausanne', 'EPFL'),
    ('ecole polytechnique federale de lausanne', 'EPFL'),
    ('swiss federal institute of technology lausanne', 'EPFL'),
    ('swiss federal institute of technology, lausanne', 'EPFL'),
    ('swiss federal technology institute of lausanne', 'EPFL'),
    ('epfl', 'EPFL'),
    ('paul scherrer', 'PSI'),
    (', psi,', 'PSI'),
    ('university of basel', 'Basel'),
    ('university of geneva', 'Geneva'),
    # Bern University of Applied Sciences = Berner Fachhochschule, standard
    # short name BFH. Distinct from University of Bern ('U Bern'); match the
    # full applied-sciences phrase so the two never conflate.
    ('bern university of applied sciences', 'BFH'),
    ('berner fachhochschule', 'BFH'),
    ('university of neuchâtel', 'Neuchâtel'),
    ('université de neuchâtel', 'Neuchâtel'),
    ('universite de neuchatel', 'Neuchâtel'),
    ('centre suisse d', 'CSEM'),
    ('empa,', 'Empa'),
    (r"re:,\s*empa\b", 'Empa'),
    ('lumiphase', 'Lumiphase AG'),
    ('lightium', 'Lightium AG'),
    ('enlightra', 'Enlightra'),

    # ---- France ------------------------------------------------------------
    ('institut d\'optique', 'Institut d\'Optique'),
    ('institut fresnel', 'Institut Fresnel'),
    # C2N (Centre de Nanoscience et Nanotechnologie) is at Paris-Saclay. Must
    # come BEFORE the generic paris-saclay anchor, since the C2N strings carry
    # "Université Paris-Saclay" in the address and would otherwise degrade to
    # "U Paris-Saclay". The "(c2n)" token is distinctive to these strings.
    ('(c2n)', 'C2N Paris-Saclay'),
    ('centre de nanoscience et nanotechnologie', 'C2N Paris-Saclay'),
    ('université paris-saclay', 'U Paris-Saclay'),
    ('paris-saclay', 'U Paris-Saclay'),
    ('université paris cité', 'U Paris Cité'),
    ('université de paris', 'U Paris Cité'),
    ('paris cite', 'U Paris Cité'),
    ('sorbonne', 'Sorbonne'),
    # Grenoble Alpes must come BEFORE CEA-LETI so combined strings with both
    # are attributed to Grenoble Alpes per ground truth.
    ('université grenoble alpes', 'Grenoble Alpes'),
    ('universite grenoble alpes', 'Grenoble Alpes'),
    ('univ. grenoble alpes', 'Grenoble Alpes'),
    ('univ grenoble alpes', 'Grenoble Alpes'),
    ('university grenoble alpes', 'Grenoble Alpes'),
    ('university of grenoble', 'Grenoble Alpes'),
    # CEA: combined CEA-Leti + CEA strings → CEA; pure CEA-LETI alone → CEA-Leti.
    ('cea-leti, cea,', 'CEA'),
    ('cea-leti', 'CEA-Leti'),
    ('cea,', 'CEA'),
    ('cea-saclay', 'CEA'),
    ('insa lyon', 'INSA Lyon'),
    # Université Marie et Louis Pasteur (Besançon) — formed 2025 from the
    # merger of UFC and UTBM. Must run BEFORE the generic 'cnrs' anchor so
    # joint strings like "Universite Marie et Louis Pasteur and CNRS" map to
    # UMLP rather than being swallowed by CNRS.
    ('marie et louis pasteur', 'UMLP'),
    ('cnrs', 'CNRS'),
    ('école normale supérieure', 'ENS Paris'),
    # ULB's engineering faculty is literally named "École Polytechnique de
    # Bruxelles", so its affiliation strings contain "Ecole Polytechnique".
    # This must run BEFORE the generic Paris "ecole polytechnique," anchors
    # below, or the Brussels institution gets mislabelled "Polytechnique".
    ('re:ecole polytechnique.*libre de bruxelles', 'ULB'),
    ('re:ecole polytechnique.*université libre de bruxelles', 'ULB'),
    ('école polytechnique,', 'Institut Polytechnique de Paris'),  # Paris campus
    ('ecole polytechnique,', 'Institut Polytechnique de Paris'),
    ('institut polytechnique de paris', 'Institut Polytechnique de Paris'),
    ('université de bordeaux', 'Bordeaux'),
    ('université de bourgogne europe', 'Université Bourgogne Europe'),
    ('université de bourgogne', 'Université de Bourgogne'),
    ('universite de bourgogne', 'Université de Bourgogne'),
    ('université de caen', 'Universite de Caen'),
    ('universite de caen', 'Universite de Caen'),
    ('université de lyon', 'U Lyon'),
    ('ecole centrale de lyon', 'Ecole Centrale de Lyon'),
    ('insa lyon', 'INSA Lyon'),
    ('université de lille', 'Lille'),
    ('university of lille', 'Lille'),
    ('université de toulouse', 'U Toulouse'),
    ('université de montpellier', 'Université de Montpellier'),
    ('universite de montpellier', 'Université de Montpellier'),
    ('université de limoges', 'Université de Limoges'),
    ('university of limoges', 'Université de Limoges'),
    ('université côte d', 'Université Cote d\'Azur'),
    ('universite cote d', 'Université Cote d\'Azur'),
    ('université de dijon', 'Dijon'),
    ('dijon', 'Dijon'),
    ('xlim', 'XLIM'),
    ('iii-v lab', 'III-V Lab'),
    ('amplitude laser', 'Amplitude Laser'),
    ('fastlite', 'Fastlite by Amplitude'),
    ('luli', 'LULI'),
    ('laboratoire pour l', 'LULI'),  # "Laboratoire pour l'Utilisation des Lasers Intenses"
    ('lpgp', 'U Paris-Saclay'),  # gas-discharge lab at Saclay
    ('thales', 'Thales'),
    ('centre de nanoscience et de nanotechnologies', 'C2N Paris-Saclay'),
    ('centre national de la recherche scientifique', 'CNRS'),
    ('exail', 'Exail'),
    ('exail,', 'EXAIL'),

    # ---- Italy -------------------------------------------------------------
    ('politecnico di milano', 'Politecnico di Milano'),
    ('politecnico di torino', 'Politecnico di Torino'),
    ('politecnico di bari', 'Polytechnic University of Bari'),
    ('polytechnic university of bari', 'Polytechnic University of Bari'),
    ('scuola superiore sant\'anna', 'Scuola Superiore Sant\'Anna'),
    ('sant\'anna', 'Scuola Superiore Sant\'Anna'),
    ('sapienza', 'Sapienza'),
    ('università cattolica del sacro cuore', 'Università Cattolica del Sacro Cuore'),
    ('università nicolò cusano', 'Università Nicolò Cusano'),
    ('università della calabria', 'Università della Calabria'),
    ('university of calabria', 'University of calabria'),
    ('università di trento', 'University of Trento'),
    ('university of trento', 'University of Trento'),
    ('university of florence', 'Florence'),
    ('università di firenze', 'Florence'),
    ('university, florence', 'Florence'),  # truncated form
    ('university of pavia', 'Pavia'),
    ('università degli studi di pavia', 'Pavia'),
    ('università di pavia', 'Pavia'),
    ('università pavia', 'Pavia'),
    ('università di brescia', 'Brescia'),
    ('university of brescia', 'Brescia'),
    ('university of padua', 'Padua'),
    ('university of padova', 'Padua'),
    ('università di padova', 'Padua'),
    ('università degli studi di padova', 'Padua'),
    ('università di ferrara', 'Ferrara'),
    ('university of ferrara', 'Ferrara'),
    ('università di cagliari', 'Cagliari'),
    ('university of cagliari', 'Cagliari'),
    ('università della campania', 'U Campania'),
    ('istituto di fotonica e nanotecnologie', 'Istituto di Fotonica e Nanotecnologie'),
    ('cnit', 'CNIT'),
    ('consiglio nazionale delle ricerche', 'CNR Italy'),
    ('cnr,', 'CNR Italy'),
    ('national research council (cnr)', 'CNR Italy'),
    ('national institute of optics-national research council', 'CNR-INO'),
    ('cnr-ino', 'CNR-INO'),
    ('sezione di perugia', 'Sezione di Perugia'),
    ('sezione di roma', 'Sezione di Roma'),
    ('osservatorio astrofisico di catania', 'Osservatorio Astrofisico di Catania'),
    ('enrico fermi research center', 'Enrico Fermi Research Center (CREF)'),
    ('university of modena and reggio emilia', 'University of Modena and Reggio Emilia'),
    ('university of l\'aquila', 'University of L\'Aquila'),

    # ---- Spain -------------------------------------------------------------
    ('icfo', 'ICFO'),
    ('institute of photonic sciences', 'ICFO'),
    ('universitat politècnica de catalunya', 'UPC'),
    ('upc,', 'UPC'),
    ('universitat politecnica de catalunya', 'UPC'),
    ('universitat politècnica de valència', 'Universitat Politecnica de Valencia'),
    ('universitat politecnica de valencia', 'Universitat Politecnica de Valencia'),
    ('universidad politecnica de madrid', 'Universidad Politecnica de Madrid'),
    ('universidad politécnica de madrid', 'Universidad Politecnica de Madrid'),
    ('universidad complutense de madrid', 'Complutense Madrid'),
    ('universitat jaume i', 'Universitat Jaume I'),
    ('csic,', 'CSIC'),
    ('consejo superior de investigaciones', 'CSIC'),
    ('instituto de ciencia de materiales de madrid', 'Instituto de Ciencia de Materiales de Madrid'),
    ('university of vigo', 'University of Vigo'),
    ('universitat rovira', 'URV'),
    ('universidad de almería', 'University of Almería'),
    ('eurecat', 'Eurecat'),
    ('donostia international physics center', 'Donostia International Physics Center'),
    ('radiantis', 'Radiantis'),
    ('microliquid', 'Microliquid'),

    # ---- Portugal ----------------------------------------------------------
    ('instituto de telecomunicações', 'Instituto de Telecomunicações'),
    ('instituto de plasmas e fusão nuclear', 'Instituto de Plasmas e Fusão Nuclear'),
    ('instituto superior técnico', 'IST Lisbon'),
    ('instituto superior tecnico', 'IST Lisbon'),
    ('ciceco', 'Aveiro'),
    ('university of aveiro', 'Aveiro'),
    ('universidade de aveiro', 'Aveiro'),
    ('university of porto', 'Porto'),
    ('porto university', 'Porto'),
    ('universidade do porto', 'Porto'),
    ('university of lisbon', 'Lisbon'),
    ('universidade de lisboa', 'Lisbon'),
    ('instituto de engenharia de sistemas e computadores', 'INESC MN'),
    ('inesc mn', 'INESC MN'),
    ('sphere ultrafast', 'Sphere Ultrafast Photonics'),
    ('glophotonics', 'GLOphotonics'),

    # ---- Netherlands -------------------------------------------------------
    ('eindhoven university of technology', 'TU Eindhoven'),
    ('tu eindhoven', 'TU Eindhoven'),
    ('delft university of technology', 'TU Delft'),
    ('tu delft', 'TU Delft'),
    ('university of twente', 'Twente'),
    ('university of amsterdam', 'Amsterdam'),
    ('university amsterdam', 'Amsterdam'),
    ('institute: amsterdam medical', 'Institute: Amsterdam Medical Center'),
    ('the hague university', 'The Hague University'),
    ('photon design', 'Photon Design'),
    ('vpiphotonics gmbh', 'VPIphotonics GmbH'),
    ('vpiphotonics inc', 'VPIphotonics Inc'),
    ('lionix bv international', 'Lionix BV International'),

    # ---- Belgium -----------------------------------------------------------
    ('ku leuven', 'KU Leuven'),
    ('université libre de bruxelles', 'ULB'),
    ('universite libre de bruxelles', 'ULB'),
    ('vrije universiteit brussel', 'VUB'),
    ('ulb,', 'ULB'),
    ('ghent university', 'Ghent'),
    ('ugent', 'Ghent'),
    ('intec,', 'INTEC'),
    ('hpe labs belgium', 'HPE Labs Belgium'),

    # ---- Nordics -----------------------------------------------------------
    ('technical university of denmark', 'DTU'),
    ('danmarks tekniske universitet', 'Danmarks Tekniske Universitet'),
    ('danish national metrology institute', 'Danish National Metrology Institute (DFM)'),
    ('danish fundamental metrologi', 'Danish Fundamental Metrologi'),
    ('dtu electro', 'DTU'),
    ('dtu,', 'DTU'),
    ('nkt photonics', 'NKT Photonics'),
    ('uv medico', 'UV Medico'),
    ('niels bohr institute', 'Copenhagen'),
    ('university of copenhagen', 'Copenhagen'),
    ('aarhus university', 'Aarhus'),
    ('sparrow quantum', 'Sparrow Quantum ApS'),
    ('royal institute of technology', 'KTH'),
    ('kth royal institute', 'KTH'),
    ('kth,', 'KTH'),
    ('chalmers', 'Chalmers'),
    ('university of gothenburg', 'Gothenburg'),
    ('linköping', 'Linköping'),
    ('linkoping', 'Linköping'),
    ('rise research institutes', 'RISE Research Institutes of Sweden'),
    ('aalto', 'Aalto'),
    ('university of helsinki', 'U Helsinki'),
    ('tampere university', 'Tampere'),
    ('vexlum', 'Vexlum Oy'),
    ('university of oulu', 'U Oulu'),
    ('university of turku', 'U Turku'),
    ('university of jyväskylä', 'U Jyväskylä'),
    ('university west', 'University West'),

    # ---- Austria -----------------------------------------------------------
    ('tu wien', 'TU Vienna'),
    ('tu vienna', 'TU Vienna'),
    ('technische universität wien', 'TU Vienna'),
    ('vienna university of technology', 'TU Vienna'),
    ('tu graz', 'TU Graz'),
    ('graz university of technology', 'TU Graz'),
    ('university of vienna', 'Vienna'),
    ('johannes kepler', 'Johannes Kepler University'),
    ('iqoqi', 'IQOQI'),
    ('ist austria', 'IST Austria'),
    ('institute of science and technology austria', 'IST Austria'),
    ('silicon austria labs', 'Silicon Austria Labs GmbH'),
    ('university of graz', 'Graz'),

    # ---- Eastern Europe ----------------------------------------------------
    ('czech technical university', 'Czech TU Prague'),
    ('uct prague', 'UCT Prague'),
    ('charles university', 'Charles U Prague'),
    ('czech academy', 'Czech Academy'),
    ('fnspe', 'FNSPE'),
    ('eli beamlines', 'ELI-Beamlines'),
    ('eli-beamlines', 'ELI-Beamlines'),
    ('eli-alps', 'ELI-ALPS'),
    ('eli alps', 'ELI-ALPS'),
    ('cesnet', 'CESNET'),
    ('hilase', 'HiLASE Centre'),
    ('palacky university', 'Palacky University'),
    ('palacký university', 'Palacky University'),
    ('alexander dubček', 'Alexander Dubček University of Trenčín'),
    ('jozef stefan', 'Jozef Stefan Institute'),
    ('university of ljubljana', 'University of Ljubljana'),
    ('university of warsaw', 'Warsaw U'),
    ('warsaw university of technology', 'Warsaw UT'),
    ('warsaw,', 'Warsaw'),
    ('lukasiewicz institute of microelectronics', 'Lukasiewicz IMiF'),
    # Wroclaw University of Science and Technology -> Wroclaw. Match the full
    # institution phrase so the separate "Gekko Photonics, Wroclaw" company
    # (which only carries the CITY token) is never swept up. Cover the
    # accented "Wrocław" spelling too.
    ('wroclaw university of science and technology', 'Wroclaw'),
    ('wrocław university of science and technology', 'Wroclaw'),
    ('lodz university of technology', 'Lodz University of Technology'),
    ('uniwersytet mikolaja kopernika', 'Uniwersytet Mikolaja Kopernika W Toruniu'),
    ('nicolaus copernicus', 'Uniwersytet Mikolaja Kopernika W Toruniu'),
    ('polish academy', 'Polish Academy'),
    ('vilnius university', 'Vilnius University'),
    # FTMC's English name; place before bare 'vilnius,' so it wins for the
    # address-bearing variant ("..., Vilnius, Lithuania") too.
    ('center for physical sciences', 'FTMC Vilnius'),
    ('vilnius,', 'Vilnius'),
    ('state research institute center for physical sciences', 'FTMC Vilnius'),
    ('ftmc,', 'FTMC Vilnius'),
    ('university of ss. cyril and methodius in trnava', 'UCM Trnava'),
    ('university of ss. cyril and metodius', 'University of Ss. Cyril and Metodius'),
    ('slovak centre of scientific', 'SCSTI Slovakia'),
    ('iict', 'IICT'),
    ('national hellenic research', 'National Hellenic Research Foundation'),
    ('aristotle', 'Aristotle'),
    ('thessaloniki', 'Thessaloniki'),
    ('university of athens', 'U Athens'),
    ('university of crete', 'Crete'),
    ('university of ioannina', 'Ioannina'),
    ('university of west attica', 'University of West Attica'),
    ('eulambia', 'Eulambia Advanced Technologies'),
    ('izmir institute of technology', 'Izmir Institute of Technology'),
    ('metu', 'METU'),
    ('middle east technical', 'METU'),

    # ---- Israel ------------------------------------------------------------
    ('technion', 'Technion'),
    ('weizmann', 'Weizmann'),
    ('tel aviv university', 'TAU'),
    ('tel-aviv university', 'TAU'),
    ('hebrew university', 'Hebrew U'),
    ('hebrew universit', 'Hebrew U'),
    ('ben-gurion', 'Ben-Gurion'),
    ('ben gurion', 'Ben-Gurion'),
    ('bar-ilan', 'Bar-Ilan'),
    ('bar ilan', 'Bar-Ilan'),
    ('ariel university', 'Ariel U'),
    ('soreq nrc', 'Soreq NRC'),
    ('hadassah-hebrew-university', 'Hadassah-Hebrew-University-Medical-Center'),
    ('civan lasers', 'Civan Lasers'),
    ('cognifiber', 'Cognifiber'),
    ('ephos', 'Ephos'),

    # ---- Russia / former Soviet --------------------------------------------
    ('a. f. ioffe', 'A. F. Ioffe Institute'),
    ('a.f. ioffe', 'A. F. Ioffe Institute'),
    ('ioffe institute', 'Ioffe Institute'),
    ('a.v. rzhanov institute', 'Rzhanov ISP'),
    ('lebedev physical institute', 'Lebedev Physical Institute'),
    ('mipt', 'MIPT'),
    ('moscow institute of physics and technology', 'MIPT'),
    ('moscow state university', 'Moscow State'),
    ('lomonosov moscow', 'Moscow State'),
    ('novosibirsk state university', 'Novosibirsk State University'),
    ('tomsk state university of control systems', 'TUSUR'),
    ('tomsk state university', 'Tomsk State University'),
    ('v.e. zuev institute', 'V.E. Zuev Institute of Atmospheric Optics'),
    ('kutateladze inst', 'Kutateladze Inst Thermophys SB RAS'),
    ('orel state university', 'Orel State University'),
    ('university of nizhny novgorod', 'University of Nizhny Novgorod'),
    ('russian quantum', 'Russian Quantum Ctr'),
    ('russian academy of science', 'RAS'),
    ('nas ra institute of chemical physics', 'NAS RA Institute of Chemical Physics'),

    # ---- China: top universities (specific city/name BEFORE generic) ------
    # Many Chinese universities have multiple full-name spellings and abbrev.
    ('huazhong university of scien', 'Huazhong'),
    ('huazhong univ of science', 'Huazhong'),
    ('huazhong univ. of science', 'Huazhong'),
    ('huazhong univ. of sci', 'Huazhong'),
    ('hust,', 'Huazhong'),
    ('wuhan national lab for optoelectronic', 'Wuhan National Lab for Optoelectronics'),
    ('tsinghua university', 'Tsinghua'),
    ('tsinghua,', 'Tsinghua'),
    ('beijing national research center for information science and technology', 'BNRist'),
    ('beijing national research center for information and technology', 'BNRist'),
    ('bnrist', 'BNRist'),
    # "Peking University Yangtze Delta Institute of Optoelectronics" → Peking U.
    # The "Universitity" typo variant → its verbatim name (RAW_OVERRIDE handles it).
    ('peking university yangtze delta', 'Peking U Yangtze Delta'),
    ('peking university', 'Peking U'),
    ('peking universit', 'Peking U'),
    ('pekin university', 'Pekin University (PKU)'),  # rare misspelling
    ('beijing institute of technology', 'BIT'),
    ('bit,', 'BIT'),
    ('beihang', 'Beihang'),
    ('beijing university of posts and telecomm', 'BUPT'),
    ('bupt,', 'BUPT'),
    ('beijing normal university', 'BNU'),
    ('bnu,', 'BNU'),
    ('beijing university of posts and telecomm', 'BUPT'),
    ('beijing univ of posts', 'BUPT'),  # abbreviated short-form variant
    ('fudan', 'Fudan'),
    ('shanghai jiao tong', 'SJTU'),
    ('shanghai jiaotong', 'SJTU'),
    ('sjtu,', 'SJTU'),
    ('sjtu-pinghu institute', 'SJTU-Pinghu'),
    ('shanghaitech', 'ShanghaiTech'),
    ('shanghai university,', 'Shanghai'),
    # CAS sub-institutes: when string also mentions "Chinese Academy" → CAS;
    # otherwise the institute's own verbatim name (handled by the lone anchors
    # further down). These combined patterns must precede the lone anchors.
    (r"re:chinese academy.*shanghai institute of microsystem", 'CAS'),
    (r"re:shanghai institute of microsystem.*chinese academy", 'CAS'),
    (r"re:chinese academy.*shanghai institute of optics", 'CAS'),
    (r"re:shanghai institute of optics.*chinese academy", 'CAS'),
    (r"re:xi'an institute of optics.*chinese academy", 'CAS'),
    (r"re:chinese academy.*xi'an institute of optics", 'CAS'),
    ('shanghai institute of microsystem', 'SIMIT'),
    ('shanghai institute of optics and fine mechanics', 'Shanghai Institute of Optics and Fine Mechanics'),
    ('shanghai institute of ceramics', 'Shanghai Institute of Ceramics'),
    ('shanghai engineering research center of energy efficient', 'SERC-EECAI Shanghai'),
    ('siom', 'SIOM'),
    ('zhejiang university', 'Zhejiang'),
    ('zju-hangzhou', 'Zhejiang'),
    ('zhejiang lab', 'Zhejiang Lab'),
    ('nanjing university of aeronautics', 'Nanjing U Aeronautics & Astronautics'),
    ('nanjing university of posts and telecommunications', 'NUPT'),
    ('nanjing university', 'Nanjing'),
    ('southeast university', 'Southeast U'),
    ('purple mountain lab', 'Purple Mountain Lab'),
    ('nankai', 'Nankai'),
    ('xi\'an jiaotong', 'Xi\'an Jiaotong'),
    ('xian jiaotong', 'Xi\'an Jiaotong'),
    ('xidian', 'Xidian'),
    ('xi\'an,', 'Xi\'an'),
    ('northwestern polytechnical', 'NWPU'),
    ('nwpu', 'NWPU'),
    ('university of electronic science and technology of china', 'UESTC'),
    ('univ. electronic sci. & tech. of china', 'UESTC'),
    ('univ of electronic science & tech china', 'UESTC'),
    ('uestc', 'UESTC'),
    ('university of science and technology of china', 'USTC'),
    ('university of science and technology of chin,', 'USTC'),
    ('ustc,', 'USTC'),
    ('chinese academy of sciences', 'CAS'),
    ('chinese academy of science', 'CAS'),
    ('chinese academic of science', 'CAS'),
    ('chinese academy of medical sciences', 'CAMS-PUMC'),
    ('institute of physics, chinese', 'CAS'),
    # "Institute of Physics" / "Institute of Semiconductors" are departments,
    # not standalone institutions — they're basically always a unit inside
    # some larger org. Route the CAS-abbreviated forms (", CAS") to CAS, and
    # let everything else fall through to the fallback shortener so it picks
    # up the actual parent institution (a university, academy, etc.) rather
    # than freezing the department name as the canonical short.
    ('institute of physics, cas', 'CAS'),
    ('institute of semiconductors, cas', 'CAS'),
    ('university of chinese academy', 'CAS'),
    ('south china normal', 'South China Normal University'),
    ('south china university of technology', 'SCUT'),
    ('scut,', 'SCUT'),
    ('south china academy of advanced opto', 'SCAAO'),
    ('sun yat-sen', 'Sun Yat-sen U'),
    ('sun yat sen', 'Sun Yat-sen U'),
    ('great bay university', 'Great Bay University'),
    ('shenzhen university,', 'Shenzhen U'),
    ('shenzhen technology', 'Shenzhen Tech U'),
    ('southern university of science and technology', 'SUSTech'),
    ('sustech', 'SUSTech'),
    ('jinan university', 'Jinan'),
    # HK: order matters — more-specific first.
    ('hong kong university of science and technology', 'HKUST'),
    ('hkust', 'HKUST'),
    ('city university of hong kong', 'CityU HK'),
    ('city university hong kong', 'CityU HK'),
    ('chinese university of hong kong (shenzhen)', 'CUHK Shenzhen'),
    ('the chinese university of hong kong (shenzhen)', 'CUHK Shenzhen'),
    ('chinese university of hong kong, shenzhen', 'CUHK Shenzhen'),
    ('the chinese university of hong kong', 'CUHK'),
    ('chinese university of hong kong', 'CUHK'),
    (r're:chinese univ\w*rsity of hong kong', 'CUHK'),  # absorbs "Univrsity" typo
    ('cuhk shenzhen', 'CUHK Shenzhen'),
    ('cuhk,', 'CUHK'),
    ('hong kong polytechnic', 'PolyU HK'),
    ('hong kong polytechinic', 'PolyU HK'),  # typo
    ('the hong kong polytechnic', 'PolyU HK'),
    ('the hong kong polytechinic', 'PolyU HK'),
    ('hong kong baptist', 'HK Baptist'),
    ('the university of hong kong', 'HKU'),
    ('university of hong kong', 'HKU'),
    ('the university of hongkong', 'HKU'),
    ('hku,', 'HKU'),
    ('university of macau', 'Macau'),
    ('pui ching middle school macau', 'Pui Ching Middle School Macau'),
    # Per existing ground-truth, NTUST is classified as NTU Taiwan too.
    ('national taiwan university of science and technology', 'NTU Taiwan'),
    ('national taiwan univ of science', 'NTU Taiwan'),  # abbreviated short-form variant
    ('ntust', 'NTU Taiwan'),
    ('national taiwan university', 'NTU Taiwan'),
    ('natioal taiwan university', 'NTU Taiwan'),  # misspelling
    ('national tsing hua', 'NTHU'),
    ('national tsing-hua', 'National Tsing-Hua University'),
    ('national chiao tung', 'NTU Taiwan'),
    ('national yang ming chiao tung', 'NYCU'),
    ('national ang ming chiao tung', 'NYCU'),
    ('national yaming chiaotung', 'NYCU'),
    ('national central university', 'National Central University'),
    ('national cheng kung', 'NCKU'),
    ('national chung cheng university', 'National Chung Cheng University'),
    ('national chung hsing', 'National Chung Hsing University'),
    ('national taiwan university of science and technology', 'NTUST'),
    ('ntust', 'NTUST'),
    ('feng chia', 'Feng Chia University'),
    ('hon hai research', 'Hon Hai Research Institute'),
    ('artilux', 'Artilux Inc.'),
    ('chengdu', 'Chengdu'),
    ('university of petroleum (beijing)', 'China University of Petroleum (Beijing)'),
    ('china university of petroleum', 'China University of Petroleum (Beijing)'),
    ('china university of geosciences', 'China University of Geosciences'),
    ('central south university', 'Central South University'),
    ('south university of science', 'SUSTech'),
    ('north china electric', 'North China Electric Power University'),
    ('national university of defense technology', 'NUDT'),
    ('nudt,', 'NUDT'),
    ('university of defense technology', 'NUDT'),
    ('national engineering research center for next generation internet access', 'NERC-NGIAS Wuhan'),
    ('national engineering research center of next generation internet access-system', 'NERC-NGIAS Wuhan'),
    ('cqu,', 'CQU'),
    ('chongqing university', 'CQU'),
    ('guangdong laboratory of artificial intelligence', 'GDLAB AI SZ'),
    ('guangdong university of technology', 'Guangdong U Tech'),
    ('guangxi university', 'Guangxi University'),
    ('guangxi medical', 'Guangxi Medical University'),
    ('harbin institute of technology', 'HIT'),
    ('hit,', 'HIT'),
    ('harbin engineering university', 'Harbin Engineering University'),
    ('jiangsu normal', 'Jiangsu Normal University'),
    ('jilin university', 'Jilin'),
    ('xiamen university', 'Xiamen'),
    ('hefei national laboratory', 'Hefei Natl Lab'),
    ('hefei natl lab', 'Hefei Natl Lab'),
    ('tianjin university', 'Tianjin'),
    ('tongji university', 'Tongji'),
    ('ningbo university of technology', 'Ningbo University of Technology'),
    ('ningbo university', 'Ningbo University'),
    ('ningbo ori-chip', 'Ningbo Ori-chip'),
    ('shanxi university', 'Shanxi'),
    ('hebei university', 'Hebei University'),
    ('henan academy', 'Henan Academy of Sciences'),
    ('henan normal university', 'Henan Normal University'),
    ('hebei,', 'Hebei University'),
    ('fuzhou university', 'Fuzhou'),
    ('hubei optical fundamental', 'Hubei Optical Fundamental Research Center'),
    ('fjirsm', 'FJIRSM'),
    ('fujian science', 'Fujian S&T Innovation Lab'),
    ('wuhan university', 'Wuhan U'),
    ('wuhan textile', 'Wuhan Textile U'),
    ('optics valley lab', 'Optics Valley Lab'),
    ('optics valley laboratory', 'Optics Valley Lab'),
    ('zte ', 'ZTE'),
    ('zte,', 'ZTE'),
    ('zte corporation', 'ZTE'),
    ('hanjiang naitional laboratory', 'Hanjiang National Lab'),
    ('hanjiang national laboratory', 'Hanjiang National Lab'),
    ('china mobile xiong', 'China Mobile Xiong’an'),
    ('china mobile research', 'China Mobile Research Institute'),
    ('china telecom research', 'China Telecom Research Institute'),
    ('china academy of electronics', 'CAEIT'),
    ('accelink', 'Accelink'),
    ('cict,', 'CICT'),
    ('cict ', 'CICT'),
    ('yofc', 'YOFC'),
    ('state key laboratory of optical fiber and cable', 'State Key Lab of Optical Fiber and Cable'),
    ('huawei', 'Huawei'),
    ('cetus photonics', 'Cetus Photonics'),
    ('tianfu xinglong', 'Tianfu Xinglong Lake Laboratory'),
    ('wuzhen laboratory', 'Wuzhen Laboratory'),
    ('jinyinhu laboratory', 'Jinyinhu Laboratory'),
    ('jinhua no. 1 high school', 'Jinhua No. 1 High School'),
    ('berxel photonics', 'Berxel Photonics'),
    ('luzhou laojiao', 'Luzhou Laojiao Co.Ltd.'),
    ('liobate technology', 'Liobate'),
    ('liobate technologies', 'Liobate'),
    ('zhangjiang lab', 'Zhangjiang Laboratory'),
    ('zhangjiang laboratory', 'Zhangjiang Laboratory'),
    ('zhang jiang laboratory', 'Zhangjiang Laboratory'),
    ('yongjiang laboratory', 'Yongjiang Laboratory'),
    ('jinyinhu', 'Jinyinhu Laboratory'),
    ('purple mountain', 'Purple Mountain Lab'),
    ('shenzhen jufei', 'Shenzhen Jufei Optoelectronics Co'),
    ('peng cheng laboratory', 'PCL Shenzhen'),
    ('pengcheng laboratory', 'PCL Shenzhen'),
    ('pcl shenzhen', 'PCL Shenzhen'),
    ('aerospace system engineering', 'Aerospace System Engineering'),
    ('ccdc drilling', 'CCDC Drilling Research Institute'),
    ('national key lab amnm', 'National Key Lab AMNM'),
    ('national key laboratory of advanced micro and nano manufacture', 'National Key Lab AMNM'),
    ('bangladesh university of engineering', 'BUET'),
    ('beijing national laboratory for condensed matter physics', 'CAS IOP Beijing'),
    ('consorzio nazionale interuniversitario per le telecomunicazioni', 'CNIT Italy'),
    ('cnit,', 'CNIT Italy'),
    ('icrea', 'ICREA'),
    ('joint international research laboratory of specialty fiber', 'Shanghai'),
    ('vereshchagin institute', 'Vereshchagin IHPP'),
    ('laboratoire interdisciplinaire carnot de bourgogne', 'ICB UMR 6303'),
    ('state key laboratory for artificial microstructure', 'Peking U'),
    ('state key laboratory of information photonics and optical communications', 'BUPT'),
    ('state key laboratory of photonics and communications', 'SKL Photonics & Comm'),
    ('state key laboratory of transient optics and photonics', 'CAS XIOPM'),
    ('laboratory of solid state optoelectronics', 'CAS IOP Beijing'),
    ('nantong nanlitai', 'Nantong Nanlitai Technology'),
    ('sanway optoelectronic', 'Sanway Optoelectronic Tech. Corp.'),
    ('yofc', 'YOFC'),

    # ---- Japan -------------------------------------------------------------
    ('the university of tokyo', 'U Tokyo'),
    ('university of tokyo', 'U Tokyo'),
    ('tokyo university of science', 'Tokyo U Science'),
    ('tokyo institute of technology', 'Tokyo Tech'),
    ('tokyo tech', 'Tokyo Tech'),
    ('institute of science tokyo', 'Tokyo Tech'),
    ('tokyo metropolitan university', 'Tokyo Metropolitan University'),
    ('tokyo university of agriculture and technology', 'TUAT'),
    ('keio university', 'Keio'),
    ('keio,', 'Keio'),
    ('waseda', 'Waseda'),
    ('the university of osaka', 'Osaka'),
    ('university of osaka', 'Osaka'),
    ('osaka university', 'Osaka'),
    ('osaka metropolitan', 'Osaka Metropolitan University'),
    ('kyoto university', 'Kyoto'),
    ('kyushu university', 'Kyushu'),
    ('tohoku university', 'Tohoku'),
    ('hokkaido university', 'Hokkaido'),
    ('nagoya university', 'Nagoya'),
    ('nagoya institute of technology', 'Nagoya Institute of Technology'),
    ('hiroshima university', 'Hiroshima'),
    ('okayama university', 'Okayama'),
    ('yokohama national university', 'Yokohama Nat'),
    ('saitama university', 'Saitama'),
    ('utsunomiya university', 'Utsunomiya'),
    ('utsunomiya u', 'Utsunomiya'),
    ('university of electro-communications', 'U Electro-Comm Tokyo'),
    ('graduate institute for advanced studies', 'Graduate Institute for Advanced Studies'),
    ('okinawa institute of science', 'OIST'),
    ('okinawa inst of science', 'OIST'),  # abbreviated short-form variant
    ('university of yamanashi', 'University of Yamanashi'),
    ('university of nagasaki', 'University of Nagasaki'),
    ('university of hyogo', 'University of Hyogo'),
    ('university of fukui', 'University of Fukui'),
    ('mie university', 'Mie'),
    ('gifu university', 'Gifu'),
    ('gunma university', 'Gunma'),
    ('shimane university', 'Shimane'),
    ('kogakuin', 'Kogakuin University'),
    ('toho university', 'Toho'),
    ('chitose institute of science', 'Chitose Institute of Science and Technology'),
    ('toyohashi university of technology', 'Toyohashi University of Technology'),
    ('bunkyo university', 'Bunkyo University'),
    ('tamagawa university', 'Tamagawa'),
    ('hanseo university', 'Hanseo University'),
    ('toyota tech', 'Toyota Tech Inst'),
    ('toyota central r&d', 'Toyota Central R&D Labs Inc'),
    ('toyota research institute of north america', 'Toyota Research Institute of North America'),
    ('nihon university', 'Nihon University'),
    ('kagawa university', 'Kagawa'),
    ('kyung hee', 'Kyung Hee University'),
    ('kochi university of technology', 'Kochi University of Technology'),
    # AIST (Japan's Natl. Inst. of Advanced Industrial Science and Technology).
    # Use \b word boundaries so the short "aist" token can't fire inside
    # "KAIST" (Korea) or "NAIST" (Nara), which are different institutions
    # handled by their own anchors below.
    (r're:\baist\b\s*,', 'AIST Japan'),
    ('national institute of advanced industrial science and technology', 'AIST Japan'),
    # Abbreviated short-forms for AIST.
    ('natl inst of adv industrial', 'AIST Japan'),
    ('natl. inst. adv. ind. sci', 'AIST Japan'),
    (r're:\baist\b\s', 'AIST Japan'),
    ('nict ', 'NICT'),
    ('nict,', 'NICT'),
    ('nict network', 'NICT Network System Research Institute'),
    ('advanced ict research institute', 'NICT'),
    ('national institute of information and communications technology', 'NICT'),
    ('national institute of information and communication technology', 'NICT'),
    ('national inst of information & comm tech', 'NICT'),
    ('nims', 'NIMS'),
    ('riken', 'RIKEN'),
    ('national institute of metrology', 'National Institute of Metrology'),
    ('jasri', 'JASRI'),
    ('jaxa', 'JAXA'),
    ('hamamatsu', 'Hamamatsu'),
    ('nichia', 'Nichia'),
    ('mitsubishi electric', 'Mitsubishi Electric'),
    ('toshiba', 'Toshiba'),
    ('sumitomo electric', 'Sumitomo Electric Industries'),
    ('furukawa fitel', 'Furukawa FITEL Optical Components'),
    ('fujikura', 'Fujikura Ltd.'),
    ('nec ', 'NEC'),
    ('nec,', 'NEC'),
    ('nec corp', 'NEC'),
    ('ntt innovative devices', 'NTT Innovative Devices Corporation'),
    ('nippon telegraph & telephone', 'NTT Japan'),
    # NTT: the bare "NTT Inc., <city>" parent-company form and all other NTT
    # subdivisions collapse to 'NTT' (company suffix dropped). Named NTT spin-out
    # corporations with a distinct identity (e.g. NTT Innovative Devices) keep
    # their own label above.
    (r're:^ntt inc\.,', 'NTT'),
    ('ntt research', 'NTT'),
    ('ntt,', 'NTT'),
    ('ntt ', 'NTT'),
    ('kddi', 'KDDI'),
    ('samusng r&d japan', 'Samsung'),  # "Samusng" is a typo for Samsung
    ('asai nursery', 'Asai Nursery'),
    ('ambition photonics', 'Ambition Photonics Inc.'),
    ('epiphotonics corp', 'EpiPhotonics'),
    ('epiphotonics usa', 'EpiPhotonics USA'),
    ('cellid', 'Cellid'),
    ('optqc', 'OptQC Corp.'),
    ('photonic inc', 'Photonic Inc'),
    ('center for quantum information and quantum biology', 'Center for Quantum Information and Quantum Biology'),
    ('extreme photonics research team', 'Extreme Photonics Research Team'),
    ('joint attosecond science laboratory', 'Joint Attosecond Science Laboratory'),
    ('john a. paulson school', 'John A. Paulson School of Engineering and Applied Sciences'),
    ('kapteyn-murnane', 'Kapteyn-Murnane Laboratories Inc.'),
    ('ryukoku', 'Ryukoku Univ'),
    ('tokushima university', 'Tokushima'),
    ('tokushima', 'Tokushima'),
    ('naist', 'NAIST'),
    ('nara institute of science and technology', 'NAIST'),
    ('functional nanosystems', 'Functional Nanosystems'),

    # ---- Korea -------------------------------------------------------------
    ('korea advanced institute of science', 'KAIST'),
    ('kaist,', 'KAIST'),
    (', kaist', 'KAIST'),
    ('seoul national university', 'Seoul Nat U'),
    ('yonsei', 'Yonsei'),
    ('korea university', 'Korea U'),
    ('postech', 'POSTECH'),
    ('pohang university of science and technology', 'POSTECH'),
    ('sungkyunkwan', 'Sungkyunkwan'),
    ('hanyang', 'Hanyang'),
    ('chungbuk', 'Chungbuk National University'),
    ('hanbat', 'Hanbat National University'),
    ('ajou', 'Ajou University'),
    ('gist', 'GIST'),
    ('gwangju institute of science and technology', 'GIST'),
    ('unist', 'UNIST'),
    ('ulsan national institute of science and technology', 'UNIST'),
    ('etri', 'ETRI'),
    ('electronics and telecommunications research institute', 'ETRI'),
    ('kist ', 'KIST'),
    ('kist,', 'KIST'),
    ('kist school', 'KIST School'),
    ('korea institute of science and technology', 'KIST'),
    ('kriss', 'KRISS'),
    ('korea research institute of standards and science', 'KRISS'),
    ('korea research institute of standard and science', 'KRISS'),
    ('korea institute of machinery and materials', 'KIMM'),
    ('korea university of science and technology (ust)', 'Korea University of Science and Technology (UST)'),
    ('korea university of science and technology (kist)', 'Korea University of Science and Technology (KIST)'),
    ('korea university of science and technology', 'Korea University of Science and Technology'),
    ('sejong university', 'Sejong University'),

    # ---- Singapore / SE Asia ----------------------------------------------
    ('nanyang technological university', 'NTU Singapore'),
    ('national university of singapore', 'NUS'),
    ('nus,', 'NUS'),
    ('singapore university of technology and design', 'SUTD'),
    ('sutd,', 'SUTD'),
    ('a*star', 'A*STAR'),
    ('agency for science, technology and research', 'A*STAR'),
    ('institute of microelectronics (ime)', 'Institute of Microelectronics (IME)'),
    ('institute of microelectronics,', 'Institute of Microelectronics'),
    ('institute for infocomm research', 'I2R Singapore'),
    ('i2r,', 'I2R Singapore'),
    ('maritime', 'Maritime Port Auth SG'),
    ('singtel', 'Singtel'),
    ('singapore telecommunications', 'Singapore Telecommunications Limited (Singtel)'),
    ('national space technology and information center', 'NSTIC Singapore'),
    ('nstic', 'NSTIC Singapore'),
    ('advanced micro foundry', 'Advanced Micro Foundry'),
    ('advanced micro foundry,', 'Advanced Micro Foundry'),
    ('silterra malaysia', 'SilTerra Malaysia'),
    ('silterra', 'SilTerra'),
    ('linkstar microtronics', 'Linkstar Microtronics Pte. Ltd'),
    ('nanyang technological institute', 'Nanyang Technological Institute'),
    ('university of the philippines', 'University of the Philippines - Visayas'),
    ('de la salle', 'De La Salle University'),
    ('commission on higher education', 'Commission on Higher Education'),
    ('asian institute of technology', 'AIT'),
    ('kasetsart', 'Kasetsart University'),
    ('chulalongkorn', 'Chulalongkorn'),

    # ---- India -------------------------------------------------------------
    ('iit bombay', 'IIT Bombay'),
    ('indian institute of technology - bombay', 'Indian Institute of Technology - Bombay'),
    ('indian institute of technology bombay', 'IIT Bombay'),
    ('iit delhi', 'IIT Delhi'),
    ('indian institute of technology delhi', 'IIT Delhi'),
    ('iit madras', 'IIT Madras'),
    ('indian institute of technology madras', 'IIT Madras'),
    ('iit kanpur', 'IIT Kanpur'),
    ('indian institute of technology kanpur', 'IIT Kanpur'),
    ('iit kharagpur', 'IIT Kharagpur'),
    ('indian institute of technology kharagpur', 'IIT Kharagpur'),
    ('iit roorkee', 'IIT Roorkee'),
    ('indian institute of technology roorkee', 'IIT Roorkee'),
    ('iit guwahati', 'IIT Guwahati'),
    ('indian institute of technology guwahati', 'IIT Guwahati'),
    ('iit hyderabad', 'IIT Hyderabad'),
    ('indian institute of technology hyderabad', 'IIT Hyderabad'),
    ('iit indore', 'IIT Indore'),
    ('indian institute of technology indore', 'IIT Indore'),
    ('indian institute of technology (iit) indore', 'IIT Indore'),
    (r're:indian institu[t]?e of technology \(iit\) indore', 'IIT Indore'),  # absorbs "institue" typo
    ('iit jodhpur', 'Indian Inst Tech Jodhpur'),
    ('indian institute of technology jodhpur', 'Indian Inst Tech Jodhpur'),
    ('indian institute of technology ropar', 'Indian Institute of Technology Ropar'),
    ('iit ropar', 'Indian Institute of Technology Ropar'),
    ('indian institute of technology,', 'Indian Institute of Technology'),
    ('indian institute of technology ', 'Indian Institute of Technology'),
    ('iit,', 'IIT'),
    ('indian institute of information technology', 'Indian Institute of Information Technology'),
    ('iisc bangalore', 'IISc Bangalore'),
    ('indian institute of science', 'IISc Bangalore'),
    ('tifr', 'TIFR'),
    ('tata institute of fundamental research', 'TIFR'),
    ('inst sw comm', 'Inst SW Comm'),
    ('csir csio', 'CSIR CSIO'),
    ('csir-cspio', 'CSIR CSIO'),
    ('hyderabad,', 'UoH'),
    ('uoh', 'UoH'),
    ('university of hyderabad', 'UoH'),
    ('punjab engineering college', 'Punjab Engineering College'),
    ('christ university', 'Christ University'),
    ('gail (india)', 'Gail (India) Ltd.'),

    # ---- Australia / NZ ----------------------------------------------------
    ('australian national university', 'ANU'),
    ('anu,', 'ANU'),
    ('university of sydney', 'Sydney'),
    ('university of new south wales', 'UNSW'),
    ('unsw canberra', 'UNSW Canberra'),
    ('unsw,', 'UNSW'),
    ('unsw ', 'UNSW'),
    ('university of melbourne', 'Melbourne'),
    ('the university of melbourne', 'Melbourne'),
    ('the university of mlebourne', 'Melbourne'),  # "Mlebourne" misspelling
    ('monash', 'Monash'),
    ('royal melbourne institute of technology', 'Royal Melbourne Institute of Technology'),
    ('rmit', 'RMIT'),
    ('university of queensland', 'U Queensland'),
    ('university of western australia', 'UWA'),
    (r"re:\buwa\b", 'UWA'),
    ('university of technology sydney', 'UTS Sydney'),
    ('uts sydney', 'UTS Sydney'),
    ('university of adelaide', 'Adelaide University'),
    ('adelaide university', 'Adelaide University'),
    ('macquarie', 'Macquarie'),
    ('swinburne', 'Swinburne'),
    # COMBS Centre: ground truth is inconsistent. Order matters:
    # - "Australian Research Council (ARC) Centre of Excellence in Optical Microcombs ..." → verbatim
    # - "ARC Centre of Excellence in Optical Microcombs ..." → COMBS Australia
    # - bare "COMBS Centre of Excellence" string → COMBS Centre of Excellence
    ('australian research council (arc) centre of excellence in optical microcombs', 'COMBS Australia'),
    ('arc centre of excellence in optical microcombs', 'COMBS Australia'),
    ('optical microcombs for breakthrough science', 'COMBS Australia'),
    ('combs centre of excellence', 'COMBS Centre of Excellence'),
    ('combs australia', 'COMBS Australia'),
    ('ozgrav', 'OzGrav'),
    ('centre of excellence for gravitational wave', 'OzGrav'),
    ('victoria university of wellington', 'Victoria U Wellington'),
    ('university of auckland', 'Auckland'),
    ('university of canterbury nz', 'U Canterbury NZ'),
    ('dodd-walls', 'Dodd-Walls Centre'),

    # ---- Canada ------------------------------------------------------------
    ('national research council canada', 'NRC Canada'),
    ('nrc canada', 'NRC Canada'),
    ('defence research and development canada', 'DRDC'),
    ('institut courtois', 'Institut Courtois'),

    # ---- Latin America / Africa -------------------------------------------
    ('cinvestav', 'CINVESTAV'),
    # UNAM — Universidad Nacional Autónoma de México (National Autonomous
    # University of Mexico). "UNAM" is the standard short name. Cover the
    # Spanish name, the English translation, and the bare acronym.
    ('universidad nacional autónoma de méxico', 'UNAM'),
    ('universidad nacional autonoma de mexico', 'UNAM'),
    ('national autonomous university of mexico', 'UNAM'),
    (r're:\bunam\b', 'UNAM'),
    ('universidade federal de pernambuco', 'UFPE'),
    ('ufpe,', 'UFPE'),
    ('universidade federal de alagoas', 'Universidade Federal de Alagoas'),
    ('federal institute of alagoas', 'Federal Institute of Alagoas'),
    ('federal university of alagoas', 'Federal University of Alagoas'),
    ('federal university of bahia', 'Federal University of Bahia'),
    ('federal university of lavras', 'Federal University of Lavras'),
    ('federal university of ouro preto', 'Federal University of Ouro Preto'),
    ('federal university of paraná', 'Federal University of Paraná'),
    ('federal university of parana', 'Federal University of Paraná'),
    ('fluminense federal university', 'Fluminense Federal University'),
    ('universidade estadual de campinas', 'Unicamp'),
    ('unicamp,', 'Unicamp'),
    ('unicamp', 'Unicamp'),
    ('state university of campinas', 'Unicamp'),
    ('universidade de são paulo', 'São Paulo'),
    ('university of são paulo', 'São Paulo'),
    ('usp - instituto de fisica de sao carlos', 'USP - Instituto de Fisica de Sao Carlos'),
    ('centro brasileiro de pesquisas fisicas', 'Centro Brasileiro de Pesquisas Fisicas'),
    ('university of guanajuato', 'U Guanajuato'),
    ('south african astronomical observatory', 'South African Astronomical Observatory'),
    ('university of witwatersrand', 'University of Witwatersrand'),

    # ---- Middle East -------------------------------------------------------
    ('king abdullah university of science', 'KAUST'),
    ('kaust', 'KAUST'),
    ('king fahd university of petroleum', 'KFUPM'),
    ('kfupm', 'KFUPM'),
    ('expec advanced research', 'EXPEC Advanced Research Center (EXPEC ARC)'),
    ('halliburton', 'Halliburton Technology'),
    ('al-azhar', 'Al-Azhar University'),
    ('ain shams', 'Ain Shams University'),
    ('alexandria u', 'Alexandria'),
    ('university of alexandria', 'Alexandria'),
    ('minia university', 'Minia University'),
    ('abu dhabi university', 'Abu Dhabi University'),
    ('technology innovation institute', 'Technology Innovation Institute'),
    ('university of jeddah', 'University of Jeddah'),

    # ---- Cross-cutting US specialty ---------------------------------------
    ('rochester institute of technology', 'RIT'),
    ('rit,', 'RIT'),
    ('rensselaer polytechnic institute', 'RPI'),
    ('lehigh', 'Lehigh'),
    ('drexel', 'Drexel'),
    ('villanova', 'Villanova'),
    ('temple university', 'Temple'),
    ('saint louis university', 'Saint Louis University'),
    ('bowling green state', 'Bowling Green State University'),
    ('augustana', 'Augustana'),
    ('washington & jefferson', 'Washington & Jefferson College'),
    ('williams', 'Williams'),
    ('mount holyoke', 'Mount Holyoke College'),
    ('east tennessee state', 'East Tennessee State University'),
    ('middle tennessee state', 'Middle Tennessee State'),
    ('middle tennesse state', 'Middle Tennessee State'),  # typo
    ('middle tenesse state', 'Middle Tennessee State'),  # another typo
    ('central connecticut', 'Central Connecticut State University'),
    ('central michigan', 'Central Michigan University'),
    ('morgan state', 'Morgan State University'),
    ('saint john\'s', 'St. John\'s'),
    ('staten island', 'Staten Island'),
    ('howard university', 'Howard'),
    ('virginia state university', 'Virginia State University'),
    ('norfolk state', 'Norfolk State'),
    ('west virginia university', 'West Virginia University'),
    ('university of north dakota', 'University of North Dakota'),
    ('north dakota,', 'University of North Dakota'),
    ('farmingdale state college', 'Farmingdale State College'),
    ('hershey high school', 'Hershey High School'),
    ('bridgewater state university', 'Bridgewater State'),
    ('us military academy', 'US Military Academy'),
    ('byu,', 'BYU'),
    ('brigham young', 'BYU'),
    ('weber state', 'Weber State'),
    ('utah state', 'Utah State'),
    ('university park', 'University Park'),
    ('university of guelph', 'U Guelph'),
    ('clemson center for optical materials', 'COMSET Clemson'),
    ('center for optical materials science and engineering', 'COMSET Clemson'),
    ('center for advanced self-powered systems', 'ASSIST'),
    ('usra research institute for advanced computer science', 'USRA RIACS'),
    ('riacs,', 'USRA RIACS'),
    ('institut interdisciplinaire d', '3IT Sherbrooke'),  # Institut Interdisciplinaire d'Innovation Technologique
    ('triangle regional research', 'TRRDC'),
    ('w&wsens', 'W&Wsens Devices Inc'),
    ('oewaves', 'OEwaves'),
    ('ipg photonics', 'IPG Photonics'),
    ('np photonics, inc', 'NP Photonics'),
    ('np photonics,', 'NP Photonics'),
    ('phase sensitive innovations,', 'Phase Sensitive Innovations'),
    ('phase sensitive innovations, inc', 'Phase Sensitive Innovations, Inc.'),
    ('photonect', 'Photonect Interconnect Solutions Inc'),
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
    ('mesa lab', 'NCAR'),
    ('national center for atmospheric research', 'NCAR'),
    ('lawrence semiconductor', 'LSRL'),
    ('mpi corporation', 'MPI Corporation'),
    ('mpi multidisciplinary sciences', 'MPI Multidisc Sci'),
    ('relativity networks', 'Relativity Networks'),
    ('postdoctoral research associate', 'Postdoctoral Research Associate'),
    ('ii-vi,', 'II-VI'),
    (' ii-vi ', 'II-VI'),
    ('coherent corp', 'Coherent'),
    ('tau systems', 'TAU Systems Inc'),
    ('teragear', 'Teragear'),
    ('thorlabs quantum', 'Thorlabs'),
    ('eu tech', 'IEU'),
    ('ieu,', 'IEU'),
    ('qxp technology', 'QXP Technology'),
    ('qaleido', 'Qaleido Photonics'),
    ('qioptiq', 'Qioptiq Ltd.'),
    ('photonic crystal photonic frontiers', 'Photonic Inc'),
    ('hubble', 'Hubble'),
    ('cellid', 'Cellid'),
    ('alphawave', 'AlphaWave Semi'),
    ('vector atomic', 'Vector Atomic'),

    # ---- Misc / very specific institutions ---------------------------------
    ('jasri', 'JASRI'),
    ('advanced fiber resources milan', 'AFR Milan'),
    ('saint petersburg', 'SPb State Univ'),  # may need adjustment
    ('iberian nanotechnology lab', 'INL'),
    ('eu xfel', 'European XFEL'),
    ('clemson center', 'Clemson Center for Optical Materials Science and Engineering Technologies'),
    ('ki3 photonics', 'Ki3 Photonics'),
    ('chi 3 optics', 'Chi 3 Optics'),
    ('chi-3 optics', 'Chi-3 Optics'),
    ('chi3 optics', 'Chi3 Optics LLC'),
    ('opms', 'OpMS - Open Minded Solutions'),
    ('open minded solutions', 'OpMS - Open Minded Solutions'),
    ('ks photonics', 'HS Photonics'),  # close enough; specific name
    ('hs photonics', 'HS Photonics'),
    ('hubble', 'Hubble'),
    ('flyth aerospace', 'FLYHT Aerospace Solutions Ltd'),
    ('flyht aerospace', 'FLYHT Aerospace Solutions Ltd'),
    ('avirata', 'Avirata Defence Systems'),
    ('atlantic technological', 'Atlantic Technological University'),
    ('measurement science and technology', 'Measurement Science and Technology'),
    ('radiation oncology', 'Radiation Oncology'),
    # Generic department abbreviations like "EE," and "ECE," are too brittle —
    # they catch unrelated strings ("Fort Lee, NJ", "Singapore, Singapore"
    # post a 'NTU, EEE,' prefix). Removed; the fallback shortener can do better.
    ('cto office', 'CTO Office'),
    ('joint quantum institute', 'Maryland'),
    ('lps maryland', 'LPS Maryland'),
    ('hpe labs,', 'HPE Labs'),
    ('the institute of optics, university of rochester', 'Rochester'),
    ('institute of optics, university of rochester', 'Rochester'),
    (r're:\blle\s+rochester\b', 'Rochester'),
    ('aeluma', 'Aeluma'),
    ('lumiphase ag', 'Lumiphase AG'),
    ('bright quantum', 'Bright Quantum Inc.'),
    ('shiva photonics', 'Shiva Photonics'),
    ('coreace', 'Core4ce'),
    ('core4ce', 'Core4ce'),
    ('columbus technologies', 'Columbus Technologies and Services'),
    ('xcimer energy', 'Xcimer Energy Corporation'),
    ('photonic crystal', 'Photonic Inc'),
    ('north carolina,', 'NC State'),
    ('north carolina state university,', 'NC State'),
    ('north carolina, raleigh', 'NC State'),
    ('photon design,', 'Photon Design'),
    ('vpiphotonics', 'VPIphotonics GmbH'),

    # ---- Lebanon -----------------------------------------------------------

    # ---- Other catch-all institutes ----------------------------------------
    ('hpe ', 'HPE'),
    ('hp inc', 'HP'),
    ('av incorporated', 'AV'),
    ('av inc.', 'AV Inc.'),

    # ---- Ad-hoc rarities ---------------------------------------------------
    ('uniwersytet mikolaja', 'Uniwersytet Mikolaja Kopernika W Toruniu'),
    ('university of trento', 'University of Trento'),
    ('university of macau', 'Macau'),
    ('university of jeddah', 'University of Jeddah'),
    ('aerospace, mechanical engineering, university of notre dame', 'Notre Dame'),
    ('notre dame', 'Notre Dame'),
    ('binghamton', 'Binghamton'),
    ('university of bonn', 'U Bonn'),
    ('university of cologne', 'U Cologne'),
    ('university of cyprus', 'U Cyprus'),
    ('university of l\'aquila', 'University of L\'Aquila'),
    ('lumina,', 'Lumina'),
    ('uviquity', 'Uviquity'),
    ('aeluma,', 'Aeluma'),
    ('amcl optical lab', 'Intel'),  # AMCL is an Intel lab
    ('photonic integrated cricuits group', 'UCF'),  # CREOL group → UCF
    ('seventh framework programme', 'EU FP7'),
    ('postech,', 'POSTECH'),
    ('andrew and erna viterbi', 'Technion'),
    ('national chiao tung', 'NTU Taiwan'),
    # ---- bare-name short forms (prefer the plain place/proper name) --------
    # These institutions are routinely referred to without a "U"/"University"
    # qualifier in the field, and the bare form is unambiguous here.
    ('university of aarhus', 'Aarhus'),
    ('university of belgrade', 'Belgrade'),
    ('university of campinas', 'Unicamp'),
    ('university of kaiserslautern', 'Kaiserslautern'),
    ('university of zagreb', 'Zagreb'),
    ('university of almería', 'Almería'),
    ('university of almeria', 'Almería'),
    ('university of tampere', 'Tampere'),
    # Konstanz: the data carries a misspelling ("Kostanz"). Anchor both the
    # correct and the typo'd spelling to the canonical bare name so neither
    # falls through to a "U Kostanz" fallback.
    ('university of konstanz', 'Konstanz'),
    ('university of kostanz', 'Konstanz'),
    ('universität konstanz', 'Konstanz'),
    ('universitat konstanz', 'Konstanz'),
    # ---- special relabels --------------------------------------------------
    # "University of Los Angeles" is a mangled "University of California, Los
    # Angeles"; there is no separate UCLA-less institution by that name.
    ('university of los angeles', 'UCLA'),
    # University of Illinois Chicago: use the standard initialism.
    ('university of illinois chicago', 'UIC'),
    ('university of illinois at chicago', 'UIC'),
    # Università della Campania "Luigi Vanvitelli".
    ('university of campania', 'UniCampania'),
    ('università della campania', 'UniCampania'),
    ('universita della campania', 'UniCampania'),
    # Diamond SA (fiber-optic connector maker, Losone, Switzerland). The raw
    # string is "Diamond Company"; map to its proper short name.
    ('diamond company', 'Diamond SA'),
    # ---- cross-year / variant-phrasing merges -----------------------------
    # Same institution written different ways across the 2025/2026 programs.
    # Fold each alternate phrasing onto the canonical (bare, per house style)
    # label its other spelling already resolves to.
    ('imperial college', 'Imperial'),          # bare "Imperial College" (no London)
    ('oxford university', 'Oxford'),
    ('laval university', 'Laval'),
    ('university konstanz', 'Konstanz'),        # "University Konstanz" (no "of")
    ('universität stuttgart', 'Stuttgart'),
    ('universitat stuttgart', 'Stuttgart'),
    ('univ. of sydney', 'Sydney'),
    ('univ of sydney', 'Sydney'),
    ('tohoku univ', 'Tohoku'),                  # "Tohoku Univ." abbreviation
    ('saitama univ', 'Saitama'),
    ('kassel universität', 'Kassel'),
    ('kassel universitat', 'Kassel'),
    ('university duisburg-essen', 'Duisburg-Essen'),  # variant without "of"
    ('gothenburg university', 'Gothenburg'),
    # "Shanghai University" with no trailing comma (the comma form is anchored
    # elsewhere). Use a regex that REQUIRES the name to end there, so it can't
    # fire on "Shanghai University of ..." or "Shanghai Jiao Tong University".
    (r're:\bshanghai university\b(?!\s+of)', 'Shanghai'),
    # Case-only typos in acronyms.
    # SJTU lowercase form.
    ('sjtu', 'SJTU'),
    # Ruhr University Bochum: many hyphen/spelling variants -> one label.
    ('ruhr-universität-bochum - puls group', 'RUB'),  # PULS research group at RUB
    ('puls group', 'RUB'),
    ('ruhr-universität bochum', 'RUB'),
    ('ruhr universität bochum', 'RUB'),
    ('ruhr-universitat bochum', 'RUB'),
    ('ruhr universitat bochum', 'RUB'),
    ('ruhr-university bochum', 'RUB'),
    ('ruhr-university-bochum', 'RUB'),
    ('ruhr university bochum', 'RUB'),
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
    ('tokyo metropokitan university', 'Tokyo Metropolitan University'),  # typo
    ('standford university', 'Stanford'),
    ('pennslvania state university', 'Penn State'),
    ('technical university munich', 'TU Munich'),
    ('technical university muncih', 'TU Munich'),
    ('technische universitat munchen', 'TU Munich'),
    ('technical university dortmund', 'TU Dortmund'),
    ('technische universitat dortmund', 'TU Dortmund'),  # no-umlaut variant
    ('technical university berlin', 'TU Berlin'),
    ('technische universiteit eindhoven', 'TU Eindhoven'),
    ('philipps-universität marburg', 'Marburg'),
    ('philipps-universitat marburg', 'Marburg'),
    ('phillips-university marburg', 'Marburg'),  # "Phillips" misspelling
    ('helmut schmidt university', 'Helmut Schmidt U'),
    ('helmut-schmidt-university', 'Helmut Schmidt U'),
    ('universita di trento', 'University of Trento'),
    ('università di trento', 'University of Trento'),
    ('insubria university', 'Insubria U'),
    ('universit‘a dell’insubria', 'Insubria U'),
    ('università dell’insubria', 'Insubria U'),
    ("università dell'insubria", 'Insubria U'),
    ('università di pisa', 'U Pisa'),
    ('universita di pisa', 'U Pisa'),
    ('university of pisa', 'U Pisa'),
    ('universität rostock', 'U Rostock'),
    ('universitat rostock', 'U Rostock'),
    ('university of rostock', 'U Rostock'),
    ('universidad de guanajuato', 'U Guanajuato'),
    ('university of kansas', 'U Kansas'),
    ('shizuoka university', 'Shizuoka'),
    ('saarland university', 'Saarland'),
    ('heidelberg university', 'Heidelberg'),
    ('shandong university', 'Shandong'),
    ('hunan university', 'Hunan'),
    ('stockholm university', 'Stockholm'),
    ('lund university', 'Lund'),
]

# Append more late patterns AFTER the above big batch (lower priority).
# These are short tokens that should only trigger if nothing earlier did.
# They use word-boundary regex to avoid matching inside larger words.
LATE_ANCHORS: list[tuple[str, str]] = [
    # Bare-city LATE anchors removed — they wrongly turned "Sydor Technologies,
    # Rochester, NY" into "Rochester" and similar. The fallback shortener
    # produces "Sydor Technologies" instead.
    #
    # The `re:\buniversity,` -> 'University' catch-all was also removed: it
    # collapsed any affiliation containing the word "university," to the
    # useless bare label "University" (e.g. "…, Beijing Information Science
    # and Technology University, Beijing, China"). The fallback shortener
    # extracts the real institution name instead.
]


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


def _strip_university_word(segment: str) -> str:
    """Strip trailing/leading 'university' to derive a place-only short name."""
    s = segment.strip()
    # "University of X" -> "X"
    m = re.match(r'^(?:the\s+)?university of\s+(.+)$', s, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # "X University" -> "X"
    m = re.match(r'^(.+?)\s+university$', s, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return s


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
        r'co\.,?\s*kg', r'ag', r's\.r\.l\.?', r's\.a\.?', r'b\.v\.?',
        r'plc', r'pty\.?\s*ltd\.?',
        r'k\.?k\.?', r'oy', r'ab', r'a/s', r's\.p\.a\.?', r'spa',
        r'oyj', r'asa', r'nv', r'n\.v\.?', r'sas', r's\.a\.s\.?',
        r'co\.', r'company',
    )
    # Require a separator (comma, space, or start) before the designator so it
    # can't chew into a real word — e.g. "s.a." must not match the "sa" in
    # "Tulsa", and "ag" must be a standalone token, not the tail of a word.
    pat = re.compile(
        r'(?:^|(?<=[\s,]))[\s,]*(?:' + '|'.join(designators) + r')\s*$',
        re.IGNORECASE,
    )
    prev = None
    while prev != s:
        prev = s
        s = pat.sub('', s).strip()
    return s


def fallback_shorten(raw: str) -> str:
    """Algorithmic short name for affiliation strings no anchor matched.

    Strategy: split by commas, drop trailing country/state/zip/city pieces and
    leading department-like pieces, then take the first remaining segment as
    the institution.  Apply "University of X -> U X" if it fits.
    """
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
    # Convert "University of X" -> "U X" if it's a simple form (one or two words).
    m = re.match(r'^(?:the\s+)?university of\s+(.+)$', inst, re.IGNORECASE)
    if m:
        place = m.group(1).strip()
        # Cap to ~2 words to keep it short ("Bristol", "Western Australia").
        words = place.split()
        if len(words) <= 3:
            return 'U ' + ' '.join(words)
    return inst


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
    # Bare AIST label with stray casing ("AISt") and the plain all-caps form,
    # which carry no comma/space for the boundary-anchored AIST patterns to
    # catch. Map to the same canonical "AIST Japan" the spelled-out name uses.
    'AISt': 'AIST Japan',
    'AIST': 'AIST Japan',
}


# ---------------------------------------------------------------------------
# Main canonicalization
# ---------------------------------------------------------------------------

def canonicalize(raw: str) -> str:
    if raw in RAW_OVERRIDES:
        return RAW_OVERRIDES[raw]
    norm = normalize(raw)
    for needle, short in ANCHORS:
        if _anchor_matches(needle, norm):
            return short
    for needle, short in LATE_ANCHORS:
        if _anchor_matches(needle, norm):
            return short
    return fallback_shorten(raw)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def build(data: dict, out_txt: Path | None = None) -> dict[str, str]:
    """Build the raw-affiliation -> short-name mapping.

    `data` is the dict the processor bundles into conference_data.json. This
    reads only its "affiliation_sources" block (see the module docstring for
    what the three string pools are):
        affiliation_sources["affiliation_full_lines"]
        affiliation_sources["presider_affiliation_strings"]
        affiliation_sources["institution_strings"]
    All three are pooled into one de-duplicated set of raw strings, and each
    distinct raw string is canonicalized into its short label.

    For backward compatibility, if no "affiliation_sources" block is present
    the three pools are looked up at the top level of `data` under the same
    neutral names, so callers may pass either the whole JSON or just the block.

    Verbose by design: each input is reported as it's consumed, then the
    canonicalization is summarized. Output is prefixed `[affil]` to match
    the convention build_conference_app.py uses for affiliation-related logs.

    Side effect: writes the mapping as a tab-separated text file. By default
    it lands at ``affiliation_map.txt`` in the current directory; pass
    ``out_txt`` to override. The file is small and the caller usually wants
    it on disk for inspection.
    """
    print('[affil] building map from the processor data JSON')
    src = data.get('affiliation_sources')
    if not isinstance(src, dict):
        # Accept either the whole JSON (with an affiliation_sources block) or
        # the block itself passed directly.
        src = data
    affils: set[str] = set()

    print('[affil]   reading full-address lines from affiliation_full_lines…')
    full_line_affils = set(src.get('affiliation_full_lines') or [])
    print(f'[affil]     {len(full_line_affils):,} unique full-address lines')
    affils |= full_line_affils

    print('[affil]   reading presider affiliations from '
          'presider_affiliation_strings…')
    presider_affils = extract_presider_affiliations(
        src.get('presider_affiliation_strings') or [])
    print(f'[affil]     {len(presider_affils):,} unique presider strings')
    affils |= presider_affils

    print('[affil]   reading short-form institutions from institution_strings…')
    inst_affils = extract_institutions(
        src.get('institution_strings') or [])
    print(f'[affil]     {len(inst_affils):,} unique short-form institutions')
    affils |= inst_affils

    print(f'[affil]   canonicalizing {len(affils):,} unique raw strings…')
    mapping = {k: canonicalize(k) for k in sorted(affils)}
    n_short = len(set(mapping.values()))

    # How many of the raw strings landed in the curated anchors/overrides
    # vs. fell all the way through to fallback_shorten? Useful for spotting
    # when a large new batch of inputs is bypassing the curated patterns.
    n_fallback = sum(1 for k, v in mapping.items() if v == fallback_shorten(k))
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