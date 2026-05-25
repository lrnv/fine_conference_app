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
Conference app builder.

Takes the single CLEAN, FINAL data artifact a processor produces
(conference_data.json) and turns it into a self-contained phone-friendly HTML
app.

The processors do ALL conference-specific processing — author/speaker
upgrading, presider scraping + backfilling, synthesized codes, session/talk
type classification, abstract LaTeX rendering, etc. — and bundle the result
into conference_data.json. This builder does only two things:

  1. Affiliation SHORTENING. The JSON carries author affiliations and presider
     affiliations as RAW strings; this builder runs build_affiliation_map.py
     (kept as-is) over the JSON's affiliation sources and applies the resulting
     map to derive the short forms the app renders (inst_shorts, speaker_aff,
     last_aff, presider_aff_short). The map module reads the same neutral
     "affiliation_sources" list directly, so the builder just hands it through.
     This is the only piece of conference processing that intentionally stays
     here.
  2. HTML templating. Splice the (now affiliation-enriched) data into the HTML
     template and write conference_app.html.

Input  (same directory as this script): conference_data.json
Output (same directory):                conference_app.html

JSON shape (produced by the processors — see build_conference_data there). The
schema is SOURCE-AGNOSTIC: nothing names where a value came from, so a
completely different conference with a completely different processor can
emit the same shape.
  {
    "conference_name": "Conference Name Year",
    "curator": {name, affiliation, link},  # OPTIONAL credit, shown in About
    "sessions": [ {id, title, tags, date, location, short_location,
                   presider, presider_aff (RAW), details, start_ts, end_ts,
                   color, talk_ids} ],
    "talks":    [ {id, session_id, title, number, start_ts, end_ts, presenter,
                   speaker, speaker_pos,
                   authors:        [ {name, insts:[int,...]} ],  # ordered
                   author_aliases: [ "loose name form", ... ],   # search only
                   institutions:   [ {n:int, name} ],            # RAW, numbered
                   institutions_may_dedup: bool,
                   abstract, status, withdrawn, first_author, last_author,
                   color, location, short_location} ],
    "session_types": [ {id, label} ],   # id == color token the app filters on
    "talk_types":    [ {id, label} ],
    "affiliation_sources": [ ... ],     # ONE flat, de-duplicated list of RAW
                                        # affiliation strings (full address
                                        # lines, presider affiliations, and
                                        # institution names all pooled) that the
                                        # shortener learns short labels from.
  }

About `talks[].authors` / `institutions`:
  * `authors` is the ordered author list. Each author's `insts` holds the
    EXPLICIT institution numbers (the `n` values in `institutions`) they
    belong to; an empty list means "unknown / no structured affiliation".
  * `institutions` carries each institution's RAW long name plus its explicit
    number `n`; author `insts` reference those numbers (not list positions),
    so numbering need not be 1..N.
  * `author_aliases` are extra loose name forms (e.g. initials) kept ONLY so
    search can still match them; they are never displayed.
  * `institutions_may_dedup` tells the builder it MAY collapse duplicate
    institutions by canonical short name (renumbering as it goes). It's set
    when the institution list has no per-author index structure to protect
    (otherwise collapsing would break the author->institution references).

Run: just `python build_conference_app.py`.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import zlib
from pathlib import Path


SCRIPT_DIR      = Path(__file__).resolve().parent
INPUT_DATA_JSON = SCRIPT_DIR / "conference_data.json"
OUTPUT_HTML     = SCRIPT_DIR / "conference_app.html"


# -----------------------------------------------------------------------------
# Wide/narrow (two-pane vs one-pane) breakpoint — THE SINGLE SOURCE OF TRUTH.
#
# Two-pane requires both enough horizontal room AND a landscape shape, so a
# wide-but-portrait device (e.g. a large tablet held upright, whose CSS width
# can still exceed the px floor) correctly stays one-pane. Note these are CSS
# pixels, independent of physical display density.
#
# CSS media queries cannot read custom properties, and this file is generated,
# so Python is the single point of definition: the constants below are
# templated into BOTH the stylesheet media queries and the JS isWide() matcher
# at build time. Change the breakpoint here and nowhere else; every consumer
# (one JS function + three media queries) is regenerated from these.
#   WIDE_MIN_PX  : px floor for the two-pane layout.
#   WIDE_QUERY   : full media condition for "wide" (two-pane).
#   NARROW_QUERY : its exact complement, for "narrow"-only rules.
# -----------------------------------------------------------------------------
WIDE_MIN_PX  = 900
WIDE_QUERY   = f"(min-width: {WIDE_MIN_PX}px) and (orientation: landscape)"
# Complement of WIDE_QUERY: below the px floor OR portrait. A media query has no
# clean "not (A and B)" form across the px boundary, so we spell out the
# De Morgan expansion explicitly as a comma (OR) list. The two clauses may
# overlap (a small portrait phone matches both); that's harmless.
NARROW_QUERY = (f"(max-width: {WIDE_MIN_PX - 0.02}px), "
                f"(orientation: portrait)")


# -----------------------------------------------------------------------------
# Minification of the EMITTED conference_app.html.
#
# When True, the builder strips comments (JS //, JS/CSS block, HTML <!-- -->)
# from the template before writing the output. The Python source — including
# the heavily-commented HTML_TEMPLATE string — is never touched; only the
# generated artifact is leaned out. Set False to emit the readable, debuggable
# HTML (every comment intact) for development.
#
# The strip runs on the TEMPLATE ONLY, before the conference JSON is spliced
# in, so arbitrary data (which may contain //, /*, <!--, or string/regex-like
# text) is never scanned by the comment remover. The JS pass is tokenizer-aware
# (it tracks string/template/regex literals) so comment markers living inside
# string or regex literals in the app code are preserved, not mistaken for
# comments. See minify_html() / _strip_js_comments().
# -----------------------------------------------------------------------------
MINIFY = True


# -----------------------------------------------------------------------------
# Compression of the embedded DATA blob.
#
# For large conferences the `const DATA = {...}` JSON literal dominates the
# file. When True, the builder emits DATA as a base64'd raw-DEFLATE payload
# plus its uncompressed byte length, and the app inflates it once at startup
# via a vendored synchronous tiny-inflate (raw DEFLATE) decompressor. JSON
# compresses extremely well (typically to 5-25% of original), so this is the
# dominant size win on big programs.
#
# Synchronous by design: the decode runs inside the `const DATA = ...`
# initializer, so DATA is ready before any other top-level code — the rest of
# startup (loadState, render, the boundary scheduler) is unchanged. The decode
# fires exactly ONCE per page load; afterward DATA is a plain in-memory object.
#
# We use tiny-inflate rather than the browser-native DecompressionStream
# because that API is async, which would force the whole startup path to become
# async for no real benefit on a one-time, few-millisecond decode. tiny-inflate
# is pure JS, synchronous, dependency-free once vendored, and works on any
# browser with no network (the app is fully offline-capable).
#
# Set False to emit the plain readable `const DATA = {...}` literal for
# debugging. See __decodeData / the vendored block in the template.
# -----------------------------------------------------------------------------
COMPRESS_DATA = True


# -----------------------------------------------------------------------------
# Vendored tiny-inflate decoder, spliced into the template only when
# COMPRESS_DATA is on. Kept as a constant (not a sidecar file) so the builder
# stays a single self-contained script with no missing-asset failure mode.
# -----------------------------------------------------------------------------
DECODER_BLOCK = r"""/*! tiny-inflate (raw DEFLATE decompressor) | MIT License | Copyright (c) 2015-present Devon Govett | https://github.com/foliojs/tiny-inflate
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */
/* Wrapped in an IIFE so it exposes exactly one function and touches no globals.
   Used to inflate the compressed DATA blob at startup (see COMPRESS_DATA in the
   builder). Synchronous by design: keeps `const DATA` synchronous so the rest
   of startup (loadState, render, scheduler) runs unchanged. This descriptive
   comment is ordinary and gets stripped by minify; the /*! banner above is a
   preserved license comment and is kept. */
const __tinf_uncompress = (function () {
var TINF_OK = 0;
var TINF_DATA_ERROR = -3;

function Tree() {
  this.table = new Uint16Array(16);   /* table of code length counts */
  this.trans = new Uint16Array(288);  /* code -> symbol translation table */
}

function Data(source, dest) {
  this.source = source;
  this.sourceIndex = 0;
  this.tag = 0;
  this.bitcount = 0;
  
  this.dest = dest;
  this.destLen = 0;
  
  this.ltree = new Tree();  /* dynamic length/symbol tree */
  this.dtree = new Tree();  /* dynamic distance tree */
}

/* --------------------------------------------------- *
 * -- uninitialized global data (static structures) -- *
 * --------------------------------------------------- */

var sltree = new Tree();
var sdtree = new Tree();

/* extra bits and base tables for length codes */
var length_bits = new Uint8Array(30);
var length_base = new Uint16Array(30);

/* extra bits and base tables for distance codes */
var dist_bits = new Uint8Array(30);
var dist_base = new Uint16Array(30);

/* special ordering of code length codes */
var clcidx = new Uint8Array([
  16, 17, 18, 0, 8, 7, 9, 6,
  10, 5, 11, 4, 12, 3, 13, 2,
  14, 1, 15
]);

/* used by tinf_decode_trees, avoids allocations every call */
var code_tree = new Tree();
var lengths = new Uint8Array(288 + 32);

/* ----------------------- *
 * -- utility functions -- *
 * ----------------------- */

/* build extra bits and base tables */
function tinf_build_bits_base(bits, base, delta, first) {
  var i, sum;

  /* build bits table */
  for (i = 0; i < delta; ++i) bits[i] = 0;
  for (i = 0; i < 30 - delta; ++i) bits[i + delta] = i / delta | 0;

  /* build base table */
  for (sum = first, i = 0; i < 30; ++i) {
    base[i] = sum;
    sum += 1 << bits[i];
  }
}

/* build the fixed huffman trees */
function tinf_build_fixed_trees(lt, dt) {
  var i;

  /* build fixed length tree */
  for (i = 0; i < 7; ++i) lt.table[i] = 0;

  lt.table[7] = 24;
  lt.table[8] = 152;
  lt.table[9] = 112;

  for (i = 0; i < 24; ++i) lt.trans[i] = 256 + i;
  for (i = 0; i < 144; ++i) lt.trans[24 + i] = i;
  for (i = 0; i < 8; ++i) lt.trans[24 + 144 + i] = 280 + i;
  for (i = 0; i < 112; ++i) lt.trans[24 + 144 + 8 + i] = 144 + i;

  /* build fixed distance tree */
  for (i = 0; i < 5; ++i) dt.table[i] = 0;

  dt.table[5] = 32;

  for (i = 0; i < 32; ++i) dt.trans[i] = i;
}

/* given an array of code lengths, build a tree */
var offs = new Uint16Array(16);

function tinf_build_tree(t, lengths, off, num) {
  var i, sum;

  /* clear code length count table */
  for (i = 0; i < 16; ++i) t.table[i] = 0;

  /* scan symbol lengths, and sum code length counts */
  for (i = 0; i < num; ++i) t.table[lengths[off + i]]++;

  t.table[0] = 0;

  /* compute offset table for distribution sort */
  for (sum = 0, i = 0; i < 16; ++i) {
    offs[i] = sum;
    sum += t.table[i];
  }

  /* create code->symbol translation table (symbols sorted by code) */
  for (i = 0; i < num; ++i) {
    if (lengths[off + i]) t.trans[offs[lengths[off + i]]++] = i;
  }
}

/* ---------------------- *
 * -- decode functions -- *
 * ---------------------- */

/* get one bit from source stream */
function tinf_getbit(d) {
  /* check if tag is empty */
  if (!d.bitcount--) {
    /* load next tag */
    d.tag = d.source[d.sourceIndex++];
    d.bitcount = 7;
  }

  /* shift bit out of tag */
  var bit = d.tag & 1;
  d.tag >>>= 1;

  return bit;
}

/* read a num bit value from a stream and add base */
function tinf_read_bits(d, num, base) {
  if (!num)
    return base;

  while (d.bitcount < 24) {
    d.tag |= d.source[d.sourceIndex++] << d.bitcount;
    d.bitcount += 8;
  }

  var val = d.tag & (0xffff >>> (16 - num));
  d.tag >>>= num;
  d.bitcount -= num;
  return val + base;
}

/* given a data stream and a tree, decode a symbol */
function tinf_decode_symbol(d, t) {
  while (d.bitcount < 24) {
    d.tag |= d.source[d.sourceIndex++] << d.bitcount;
    d.bitcount += 8;
  }
  
  var sum = 0, cur = 0, len = 0;
  var tag = d.tag;

  /* get more bits while code value is above sum */
  do {
    cur = 2 * cur + (tag & 1);
    tag >>>= 1;
    ++len;

    sum += t.table[len];
    cur -= t.table[len];
  } while (cur >= 0);
  
  d.tag = tag;
  d.bitcount -= len;

  return t.trans[sum + cur];
}

/* given a data stream, decode dynamic trees from it */
function tinf_decode_trees(d, lt, dt) {
  var hlit, hdist, hclen;
  var i, num, length;

  /* get 5 bits HLIT (257-286) */
  hlit = tinf_read_bits(d, 5, 257);

  /* get 5 bits HDIST (1-32) */
  hdist = tinf_read_bits(d, 5, 1);

  /* get 4 bits HCLEN (4-19) */
  hclen = tinf_read_bits(d, 4, 4);

  for (i = 0; i < 19; ++i) lengths[i] = 0;

  /* read code lengths for code length alphabet */
  for (i = 0; i < hclen; ++i) {
    /* get 3 bits code length (0-7) */
    var clen = tinf_read_bits(d, 3, 0);
    lengths[clcidx[i]] = clen;
  }

  /* build code length tree */
  tinf_build_tree(code_tree, lengths, 0, 19);

  /* decode code lengths for the dynamic trees */
  for (num = 0; num < hlit + hdist;) {
    var sym = tinf_decode_symbol(d, code_tree);

    switch (sym) {
      case 16:
        /* copy previous code length 3-6 times (read 2 bits) */
        var prev = lengths[num - 1];
        for (length = tinf_read_bits(d, 2, 3); length; --length) {
          lengths[num++] = prev;
        }
        break;
      case 17:
        /* repeat code length 0 for 3-10 times (read 3 bits) */
        for (length = tinf_read_bits(d, 3, 3); length; --length) {
          lengths[num++] = 0;
        }
        break;
      case 18:
        /* repeat code length 0 for 11-138 times (read 7 bits) */
        for (length = tinf_read_bits(d, 7, 11); length; --length) {
          lengths[num++] = 0;
        }
        break;
      default:
        /* values 0-15 represent the actual code lengths */
        lengths[num++] = sym;
        break;
    }
  }

  /* build dynamic trees */
  tinf_build_tree(lt, lengths, 0, hlit);
  tinf_build_tree(dt, lengths, hlit, hdist);
}

/* ----------------------------- *
 * -- block inflate functions -- *
 * ----------------------------- */

/* given a stream and two trees, inflate a block of data */
function tinf_inflate_block_data(d, lt, dt) {
  while (1) {
    var sym = tinf_decode_symbol(d, lt);

    /* check for end of block */
    if (sym === 256) {
      return TINF_OK;
    }

    if (sym < 256) {
      d.dest[d.destLen++] = sym;
    } else {
      var length, dist, offs;
      var i;

      sym -= 257;

      /* possibly get more bits from length code */
      length = tinf_read_bits(d, length_bits[sym], length_base[sym]);

      dist = tinf_decode_symbol(d, dt);

      /* possibly get more bits from distance code */
      offs = d.destLen - tinf_read_bits(d, dist_bits[dist], dist_base[dist]);

      /* copy match */
      for (i = offs; i < offs + length; ++i) {
        d.dest[d.destLen++] = d.dest[i];
      }
    }
  }
}

/* inflate an uncompressed block of data */
function tinf_inflate_uncompressed_block(d) {
  var length, invlength;
  var i;
  
  /* unread from bitbuffer */
  while (d.bitcount > 8) {
    d.sourceIndex--;
    d.bitcount -= 8;
  }

  /* get length */
  length = d.source[d.sourceIndex + 1];
  length = 256 * length + d.source[d.sourceIndex];

  /* get one's complement of length */
  invlength = d.source[d.sourceIndex + 3];
  invlength = 256 * invlength + d.source[d.sourceIndex + 2];

  /* check length */
  if (length !== (~invlength & 0x0000ffff))
    return TINF_DATA_ERROR;

  d.sourceIndex += 4;

  /* copy block */
  for (i = length; i; --i)
    d.dest[d.destLen++] = d.source[d.sourceIndex++];

  /* make sure we start next block on a byte boundary */
  d.bitcount = 0;

  return TINF_OK;
}

/* inflate stream from source to dest */
function tinf_uncompress(source, dest) {
  var d = new Data(source, dest);
  var bfinal, btype, res;

  do {
    /* read final block flag */
    bfinal = tinf_getbit(d);

    /* read block type (2 bits) */
    btype = tinf_read_bits(d, 2, 0);

    /* decompress block */
    switch (btype) {
      case 0:
        /* decompress uncompressed block */
        res = tinf_inflate_uncompressed_block(d);
        break;
      case 1:
        /* decompress block with fixed huffman trees */
        res = tinf_inflate_block_data(d, sltree, sdtree);
        break;
      case 2:
        /* decompress block with dynamic huffman trees */
        tinf_decode_trees(d, d.ltree, d.dtree);
        res = tinf_inflate_block_data(d, d.ltree, d.dtree);
        break;
      default:
        res = TINF_DATA_ERROR;
    }

    if (res !== TINF_OK)
      throw new Error('Data error');

  } while (!bfinal);

  if (d.destLen < d.dest.length) {
    if (typeof d.dest.slice === 'function')
      return d.dest.slice(0, d.destLen);
    else
      return d.dest.subarray(0, d.destLen);
  }
  
  return d.dest;
}

/* -------------------- *
 * -- initialization -- *
 * -------------------- */

/* build fixed huffman trees */
tinf_build_fixed_trees(sltree, sdtree);

/* build extra bits and base tables */
tinf_build_bits_base(length_bits, length_base, 4, 3);
tinf_build_bits_base(dist_bits, dist_base, 2, 1);

/* fix a special case */
length_bits[28] = 0;
length_base[28] = 258;

return tinf_uncompress;

})();

/* Decode the embedded base64 raw-DEFLATE payload back into its JSON object.
   __origLen is the uncompressed byte length (emitted by the builder) so we can
   size the destination buffer that tiny-inflate fills. */
function __decodeData(b64, origLen) {
  const bin = atob(b64);
  const src = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) src[i] = bin.charCodeAt(i);
  const dest = new Uint8Array(origLen);
  __tinf_uncompress(src, dest);
  return JSON.parse(new TextDecoder("utf-8").decode(dest));
}
"""


# -----------------------------------------------------------------------------
# Load the clean data JSON.
# -----------------------------------------------------------------------------
def _load_data_json() -> dict:
    if not INPUT_DATA_JSON.exists():
        print(f"[error] Missing input file: {INPUT_DATA_JSON}")
        print("Run a processor first; it produces "
              f"{INPUT_DATA_JSON.name} next to itself.")
        sys.exit(1)
    with open(INPUT_DATA_JSON, encoding="utf-8") as f:
        return json.load(f)


_DATA: dict = _load_data_json()


# -----------------------------------------------------------------------------
# Affiliation map: built on the fly by build_affiliation_map.py (kept as-is).
#
# That module's build(data) reads the JSON's source-agnostic
# "affiliation_sources" list directly — one flat, de-duplicated list of RAW
# affiliation strings this builder already holds — so we just hand the data
# through unchanged. No translation layer is needed.
# -----------------------------------------------------------------------------
def _load_affiliation_map() -> dict[str, str]:
    p = SCRIPT_DIR / "build_affiliation_map.py"
    if not p.exists():
        print(f"[affil] no {p.name} next to script — using heuristic only.")
        return {}
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("build_affiliation_map", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)               # type: ignore[union-attr]
        build_fn = getattr(mod, "build", None)
        if not callable(build_fn):
            print(f"[affil] {p.name} has no build() function; ignoring.")
            return {}
        m = build_fn(_DATA)
        if not isinstance(m, dict):
            print(f"[affil] {p.name}::build() returned {type(m).__name__}, "
                  "not a dict; ignoring.")
            return {}
        print(f"[affil] built {len(m):,} affiliation mappings via "
              f"{p.name} ({len(set(m.values())):,} unique short names).")
        return m
    except Exception as e:
        print(f"[affil] couldn't build map via {p.name}: {e}; "
              "using heuristic only.")
        return {}


AFFILIATION_TO_SHORT: dict[str, str] = _load_affiliation_map()


# -----------------------------------------------------------------------------
# Affiliation shortening — the one piece of content processing kept in the
# builder. Identical to the original short_affiliation()/helpers.
# -----------------------------------------------------------------------------
_KW = ("University", "Univ", "Institute", "Laboratory", "Lab",
       "School", "College", "Polytechnic", "Centre", "Center",
       "Academy", "National", "Foundation", "Corporation",
       "Hewlett", "Microsoft", "IBM", "Google", "Apple", "Intel",
       "Nokia", "Cisco", "Bell Labs", "Tech", "Hospital", "Naval",
       "AFRL", "NIST", "SLAC", "Argonne", "Lawrence", "Oak Ridge")


def short_affiliation(full: str) -> str:
    """Reduce a long affiliation string to a short canonical name. Tries the
    curated map first, then a keyword heuristic."""
    if not full:
        return ""
    norm = re.sub(r"\s+", " ", full).strip().rstrip(" .,;")
    canon = AFFILIATION_TO_SHORT.get(norm)
    if canon:
        return canon
    parts = [p.strip(" .") for p in full.split(",") if p.strip(" .")]
    if not parts:
        return ""
    for p in parts:
        for k in _KW:
            if k.lower() in p.lower():
                return p
    return parts[0]


def short_presider_affiliations(affs: str) -> str:
    """Shorten + de-dup a (possibly semicolon-separated) presider affiliation
    string."""
    if not affs:
        return ""
    seen: set[str] = set()
    out: list[str] = []
    for piece in affs.split(";"):
        p = piece.strip()
        if not p:
            continue
        s = short_affiliation(p)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return "; ".join(out)


def presider_short_aff_list(presider_field: str, affs: str) -> list[str]:
    """Return a per-presider list of canonical SHORT affiliations, aligned to
    the presider names (NOT de-duped), so the app can render
    'Name1 · Short1, Name2 · Short2'.

    `presider_field` is the '; '/' and '-joined names; `affs` is the parallel
    '; '-joined RAW affiliation string. We shorten each raw affiliation
    independently. The two lists are zipped positionally; if they differ in
    length (e.g. only one shared affiliation was recorded for several names),
    the shorter list governs and any names without a matching affiliation get
    an empty string. Malformed affiliations — empty, or a stray footnote-style
    marker like '(a)' that reduces to a single character — are dropped to ''
    so they don't render as bogus '· a' entries. Returns [] when there are no
    names."""
    names = [n.strip() for n in re.split(r";| and ", presider_field or "")
             if n.strip()]
    if not names:
        return []
    raw_affs = [a.strip() for a in (affs or "").split(";")]
    out: list[str] = []
    for i, _nm in enumerate(names):
        raw = raw_affs[i] if i < len(raw_affs) else ""
        # Skip raw affiliations that aren't actually informative (e.g. '(a)').
        if not raw or not _affiliation_is_usable(raw):
            out.append("")
            continue
        short = short_affiliation(raw)
        # The shortener can still yield a one-char remnant; guard again.
        out.append(short if _affiliation_is_usable(short) else "")
    return out


def _short_inst(inst: dict) -> str:
    """Shorten an institution to its canonical short name, consulting ALL of
    its long-form name variants. Variants come in two flavours: the display
    `name` (often a detailed, department-prefixed address) and any `alt_names`
    (cleaner institution-level forms a source pre-extracted). We try variants
    CLEANEST-FIRST — alt_names before the detailed display name — because the
    detailed form's leading clause is frequently a department ("School of …")
    that the heuristic shortener would otherwise surface instead of the parent
    institution. A curated-map hit on any variant always wins."""
    if not inst:
        return ""
    name = (inst.get("name") or "").strip()
    alts = [(a or "").strip() for a in (inst.get("alt_names") or [])
            if (a or "").strip()]
    # Cleanest-first order, de-duped.
    variants: list[str] = []
    for v in alts + ([name] if name else []):
        if v and v not in variants:
            variants.append(v)
    if not variants:
        return ""
    # 1. Curated map hit on any variant wins (exact, source-independent).
    for v in variants:
        norm = re.sub(r"\s+", " ", v).strip().rstrip(" .,;")
        mapped = AFFILIATION_TO_SHORT.get(norm)
        if mapped:
            return mapped
    # 2. Heuristic: shorten the cleanest variant.
    return short_affiliation(variants[0])


def _inst_by_number(institutions: list, n):
    """Return the institution dict whose explicit number == n, else None."""
    if not institutions or n is None:
        return None
    for inst in institutions:
        if inst.get("n") == n:
            return inst
    return None


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip(" *.").lower()


def _name_key(name: str) -> str:
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


def _author_first_inst(author: dict, institutions: list):
    """The institution dict of the FIRST institution an author belongs to (by
    the author's first inst number), or None if none / doesn't resolve."""
    insts = author.get("insts") or []
    if not insts:
        return None
    return _inst_by_number(institutions, insts[0])


def _author_short_affs(author: dict, institutions: list) -> list:
    """All of an author's affiliations as SHORT canonical names, in the
    author's `insts` order, de-duplicated (an author can list two departments
    of the same institution, which shorten to the same name). Empty/unresolved
    entries are dropped."""
    out: list = []
    for n in (author.get("insts") or []):
        inst = _inst_by_number(institutions, n)
        if not inst:
            continue
        s = short_affiliation((inst.get("name") or "").strip())
        if s and s not in out:
            out.append(s)
    return out


def _person_short_aff_by_key(person: str, authors: list,
                             institutions: list, key_fn) -> str:
    """Resolve a person's short affiliation: find the matching author, take
    their first institution and shorten its DETAILED display name (so it
    matches the institution-row short label); fall back to the talk's first
    institution — preferring its cleaner variant — when no structured
    membership resolves."""
    if person and authors:
        target = key_fn(person)
        for a in authors:
            if key_fn(a.get("name", "")) == target:
                inst = _author_first_inst(a, institutions)
                if inst:
                    s = short_affiliation((inst.get("name") or "").strip())
                    if s:
                        return s
    if institutions:
        s = _short_inst(institutions[0])
        if s:
            return s
    return ""


def speaker_short_aff(speaker: str, authors: list, institutions: list) -> str:
    """Short affiliation to display for the speaker.

    Default is the speaker's FIRST affiliation (short canonical name). But when
    the speaker lists MULTIPLE affiliations and any of them coincides (by short
    name) with one of the LAST author's affiliations, prefer the speaker's first
    such shared affiliation instead. Rationale: a speaker who did the work at a
    PI's institution and has since moved often lists the new institution first;
    the shared affiliation is the one relevant to this talk's group.

    Falls back to the original first-affiliation behaviour (and ultimately the
    talk's first institution) whenever there's no multi-affiliation overlap.
    """
    if speaker and authors:
        target = _norm_name(speaker)
        spk = next((a for a in authors
                    if _norm_name(a.get("name", "")) == target), None)
        if spk is not None:
            spk_affs = _author_short_affs(spk, institutions)
            if len(spk_affs) > 1 and authors:
                last_affs = set(_author_short_affs(authors[-1], institutions))
                shared = next((s for s in spk_affs if s in last_affs), None)
                if shared:
                    return shared
            if spk_affs:
                return spk_affs[0]
    # No structured speaker membership resolved. WHEN the talk actually carries
    # structured author→institution links (some author has a non-empty `insts`)
    # AND the speaker is not themselves the last author, a speaker with no link
    # is meaningfully unaffiliated, so fall back to the LAST author's primary
    # affiliation — the senior/PI author anchors the group, a better guess than
    # the talk's first institution. But when NO author has structured links
    # (e.g. datasets that never populate `insts`), that emptiness is just
    # missing data, not a signal; and when the speaker IS the last author there
    # is no separate PI to defer to. In both those cases keep the original
    # behaviour (the talk's first institution), which for a first-author speaker
    # is right.
    has_structured = any(a.get("insts") for a in (authors or []))
    speaker_is_last = bool(
        speaker and authors
        and _norm_name(authors[-1].get("name", "")) == _norm_name(speaker))
    if has_structured and not speaker_is_last:
        fallback = last_author_short_aff(authors, institutions)
        if fallback:
            return fallback
    return _person_short_aff_by_key(speaker, authors, institutions, _norm_name)


def last_author_short_aff(authors: list, institutions: list) -> str:
    if authors:
        inst = _author_first_inst(authors[-1], institutions)
        if inst:
            s = short_affiliation((inst.get("name") or "").strip())
            if s:
                return s
    if institutions:
        s = _short_inst(institutions[-1])
        if s:
            return s
    return ""


# -----------------------------------------------------------------------------
# Presider-affiliation backfill helpers. The backfill counts SHORT
# affiliations, which requires the affiliation map, so it runs here in the
# builder rather than in the processor.
# -----------------------------------------------------------------------------
def _affiliation_is_usable(aff: str) -> bool:
    """Is a presider affiliation string actually informative? Unusable: empty,
    or a stray footnote-style marker (e.g. 'a', '(b)', '1') that carries fewer
    than two letters once punctuation, parentheses, digits and whitespace are
    stripped. A real affiliation always has at least a couple of letters."""
    if not aff:
        return False
    for piece in aff.split(";"):
        # Count only letters, so '(a)', '(b)', '1.', '-' etc. all reduce to a
        # length below the threshold while genuine names ('MIT', 'UVA') pass.
        letters = re.sub(r"[^A-Za-z]+", "", piece)
        if len(letters) >= 2:
            return True
    return False


def author_short_aff(author: str, authors: list, institutions: list) -> str:
    """Short affiliation for an arbitrary author (initials-robust surname
    match): structured membership first, then a lone single-institution
    fallback."""
    if not author:
        return ""
    if authors:
        target = _name_key(author)
        for a in authors:
            if _name_key(a.get("name", "")) == target:
                inst = _author_first_inst(a, institutions)
                if inst:
                    s = short_affiliation((inst.get("name") or "").strip())
                    if s:
                        return s
    if institutions and len(institutions) == 1:
        s = _short_inst(institutions[0])
        if s:
            return s
    return ""


# -----------------------------------------------------------------------------
# Enrich the clean data with affiliation short forms the app renders, then
# backfill missing presider affiliations from papers the presider authors.
# -----------------------------------------------------------------------------
def _strip_trailing_periods(title: str) -> str:
    """Trim trailing whitespace from a title, and remove a single trailing
    period if one remains. An ellipsis ("...") at the end is left intact,
    since that's intentional rather than a stray sentence-final period."""
    if not title:
        return title
    stripped = title.rstrip()
    if stripped.endswith("...") or not stripped.endswith("."):
        return stripped
    return stripped[:-1]


# =============================================================================
# Person-name normalization
# -----------------------------------------------------------------------------
# Upstream data occasionally carries authors/speakers/presiders in shapes that
# are clearly data-entry artifacts rather than the person's chosen spelling.
# Two repair patterns handle every real case we've seen across multiple
# source feeds:
#
#   1. ALL-CAPS letter-runs that should be Title-Cased. Triggered only when
#      the run has at least three letters AND contains a vowel (A/E/I/O/U/Y).
#      The vowel requirement keeps run-together initials like 'JDG', 'AG',
#      'AJ' as initials while still catching every real surname error
#      ('DIDIER', 'WANG', 'YANG', 'LYU', 'KIM', 'LIU' — all have vowels).
#      Hyphenated names are repaired per-piece, so 'LAURENT-PUIG' becomes
#      'Laurent-Puig'.
#
#   2. Lowercase first/last tokens that should start with a capital. So
#      'fatih atar' -> 'Fatih Atar', 'A. matsko' -> 'A. Matsko'. Lowercase
#      mid-name particles ('von', 'de', 'da', 'di', 'te', 'van', and the
#      French elisions "d'", "l'") are valid in the middle and stay.
#      Mid-name single-letter initials in '<letter>.' form are forced
#      upper, so 'k. c. joshi' becomes 'K. C. Joshi' rather than
#      'K. c. Joshi'.
#
# When normalizing an ALL-CAPS-source name, we also lowercase any known
# particle that got dragged into caps by the same artifact ('POINSINET DE
# SIVRY-HOULE' -> 'Poinsinet de Sivry-Houle', not '... DE ...').
#
# Inputs that don't look like person names are skipped: strings with more
# than six tokens (longest real authors we see are 6 tokens, e.g.
# 'A K M Sarwar Hossain Faysal') or any digits (real names don't carry
# digits; titles and footnote-marked entries do). This guards against
# upstream bugs that occasionally drop a title into the author_aliases
# search list.
# =============================================================================

# Mid-name particles that should remain lowercase even when they appear in
# the middle of an otherwise-CAPS name. Apostrophe particles ("d'", "l'") are
# handled separately because they include punctuation.
_NAME_PARTICLES = frozenset({
    "de", "da", "di", "du", "del", "della", "dei", "degli", "delle",
    "do", "dos", "das",
    "le", "la", "lo", "las", "los",
    "van", "von", "der", "den", "ter", "ten", "te", "zu",
    "af", "av", "al", "el", "bin", "ibn",
})

_VOWELS = frozenset("AEIOUYaeiouy")
_INITIAL_RE = re.compile(r"^[A-Za-z]\.$")
_PARTICLE_PREFIX_RE = re.compile(r"^[DdLl]'")


def _has_vowel(letters: str) -> bool:
    return any(c in _VOWELS for c in letters)


def _fix_allcaps_piece(piece: str) -> str:
    """Title-case a single hyphen-free piece if it's ALL CAPS AND at least
    three letters AND contains a vowel. The length+vowel test together
    keep legitimate initials runs (M, AB, JDG) from being lowercased, while
    every real surname error gets fixed."""
    letters = re.sub(r"[^A-Za-z]", "", piece)
    if len(letters) >= 3 and letters.isupper() and _has_vowel(letters):
        out, seen = [], False
        for ch in piece:
            if ch.isalpha():
                out.append(ch.upper() if not seen else ch.lower())
                seen = True
            else:
                out.append(ch)
        return "".join(out)
    return piece


def _fix_allcaps_token(token: str) -> str:
    """Apply the all-caps fix per hyphen-separated piece, so 'LAURENT-PUIG'
    becomes 'Laurent-Puig' and 'M.-S.' (initials) is left untouched."""
    return "-".join(_fix_allcaps_piece(p) for p in token.split("-"))


def _capitalize_first_alpha(token: str) -> str:
    """Capitalize the first alphabetic character in a token (in place). A
    leading non-letter (e.g. the apostrophe in "d'herbais") is preserved;
    only the first actual letter is touched, so the rest of the token's
    casing is kept intact."""
    for i, ch in enumerate(token):
        if ch.isalpha():
            if ch.islower():
                return token[:i] + ch.upper() + token[i + 1:]
            return token
    return token


def _looks_like_non_name(name: str) -> bool:
    """Defensive check for inputs that aren't human names — typically titles
    that have leaked into the author_aliases search list. Real names never
    have more than six tokens in our feeds, and never carry digits."""
    if any(c.isdigit() for c in name):
        return True
    if len(name.split()) > 6:
        return True
    return False


def normalize_person_name(name: str) -> str:
    """Normalize a single human-name string per the rules described above.
    Idempotent. Returns the input unchanged when it carries no fixable
    artifacts (or doesn't look like a name)."""
    if not name:
        return name
    if _looks_like_non_name(name):
        return name
    tokens = name.split()
    if not tokens:
        return name

    # Snapshot the original casing of each token before any modification.
    # We need this to decide later whether a middle 'DE', 'VAN', etc. came
    # from an all-caps artifact (and should be lowered to 'de'/'van') or
    # came in already mixed-case as 'Van Thourhout', 'Da Ros', etc. — names
    # whose owners chose the capitalized form, which we must not change.
    def _is_allcaps_token(tok):
        letters = re.sub(r"[^A-Za-z]", "", tok)
        return len(letters) >= 2 and letters.isupper()

    caps_orig = [_is_allcaps_token(t) for t in tokens]

    # Rule 1: ALL-CAPS repair, every token.
    tokens = [_fix_allcaps_token(t) for t in tokens]

    # Rule 2a: first token must start with an uppercase letter, with one
    # exception: a French elision particle ("d'Aligny", "l'Estrange") that
    # is conventionally written with a lowercase leading letter regardless
    # of position.
    if not _PARTICLE_PREFIX_RE.match(tokens[0]):
        tokens[0] = _capitalize_first_alpha(tokens[0])

    # Rule 2b: last token, same logic. A last token like "d'Aligny" stays.
    if len(tokens) > 1 and not _PARTICLE_PREFIX_RE.match(tokens[-1]):
        tokens[-1] = _capitalize_first_alpha(tokens[-1])

    # Rule 3: middle tokens.
    #   * '<letter>.' is treated as an initial and forced upper, so a string
    #     like 'k. c. joshi' becomes 'K. C. Joshi' and 'Leticia d. Magalhaes'
    #     becomes 'Leticia D. Magalhaes'.
    #   * A known surname particle ('de', 'van', 'la', 'di' ...) is lowered
    #     ONLY when the original token was all-caps. That way 'POINSINET DE
    #     SIVRY-HOULE' -> 'Poinsinet de Sivry-Houle' but 'Dries Van
    #     Thourhout', 'Francesco Da Ros', 'Chris G. Van De Walle' (names
    #     whose owners use the capitalized form) are left untouched.
    for i in range(1, len(tokens) - 1):
        tok = tokens[i]
        if _INITIAL_RE.match(tok):
            tokens[i] = tok.upper()
        elif caps_orig[i] and tok.lower() in _NAME_PARTICLES:
            tokens[i] = tok.lower()

    return " ".join(tokens)


def normalize_names_in_data(data: dict) -> None:
    """Apply normalize_person_name across every name-bearing field in the
    data, in place. Touched fields:

        sessions[].presider                       (may carry '; ' / ' and '
                                                   separators between
                                                   co-presiders)
        talks[].speaker, .presenter,
               .first_author, .last_author
        talks[].authors[].name
        talks[].author_aliases[]                  (search-only loose forms)

    Other fields (titles, abstracts, affiliations, raw institution strings)
    are left untouched. Affiliations have their own all-caps tokens that are
    typically legitimate ('MIT', 'NIST', 'KAIST'), which is exactly why this
    normalization is scoped to person names."""
    n_changed = 0

    def fix(s):
        nonlocal n_changed
        if not s:
            return s
        out = normalize_person_name(s)
        if out != s:
            n_changed += 1
        return out

    def fix_multi(s):
        """Co-presider strings split on '; ' or ' and '; normalize each
        piece independently so 'JANE DOE and john smith' becomes
        'Jane Doe and John Smith' without losing the separator. Falls back
        to whole-string normalization when no separator is present."""
        if not s:
            return s
        parts = re.split(r"(\s*;\s*|\s+and\s+)", s)
        if len(parts) == 1:
            return fix(s)
        out_parts = []
        for i, p in enumerate(parts):
            # Even indices are names; odd indices are the matched separators.
            out_parts.append(fix(p) if i % 2 == 0 else p)
        return "".join(out_parts)

    for s in data.get("sessions", []) or []:
        if s.get("presider"):
            s["presider"] = fix_multi(s["presider"])

    for t in data.get("talks", []) or []:
        for f in ("speaker", "presenter", "first_author", "last_author"):
            if t.get(f):
                t[f] = fix(t[f])
        for a in t.get("authors", []) or []:
            if a.get("name"):
                a["name"] = fix(a["name"])
        aliases = t.get("author_aliases")
        if isinstance(aliases, list):
            t["author_aliases"] = [fix(x) for x in aliases]

    print(f"[names] normalized {n_changed} person-name field(s)")


# =============================================================================
# Inline-LaTeX -> Unicode for abstract bodies
# -----------------------------------------------------------------------------
# A few abstracts carry simple inline
# LaTeX, e.g. "$10^{10}$-fold", "Si$_3$N$_4$", "$\alpha_c = 0.138$". We render
# these as Unicode where a clean glyph exists and fall back to <sup>/<sub>
# tags otherwise; the app's abstract renderer un-escapes those tags.
# =============================================================================
_LATEX_SYMBOLS: dict[str, str] = {
    # lowercase Greek
    "alpha": "\u03b1", "beta": "\u03b2", "gamma": "\u03b3", "delta": "\u03b4",
    "epsilon": "\u03b5", "varepsilon": "\u03b5", "zeta": "\u03b6", "eta": "\u03b7",
    "theta": "\u03b8", "vartheta": "\u03d1", "iota": "\u03b9", "kappa": "\u03ba",
    "lambda": "\u03bb", "mu": "\u03bc", "nu": "\u03bd", "xi": "\u03be",
    "pi": "\u03c0", "varpi": "\u03d6", "rho": "\u03c1", "varrho": "\u03f1",
    "sigma": "\u03c3", "varsigma": "\u03c2", "tau": "\u03c4", "upsilon": "\u03c5",
    "phi": "\u03c6", "varphi": "\u03d5", "chi": "\u03c7", "psi": "\u03c8",
    "omega": "\u03c9",
    # uppercase Greek
    "Gamma": "\u0393", "Delta": "\u0394", "Theta": "\u0398", "Lambda": "\u039b",
    "Xi": "\u039e", "Pi": "\u03a0", "Sigma": "\u03a3", "Upsilon": "\u03a5",
    "Phi": "\u03a6", "Psi": "\u03a8", "Omega": "\u03a9",
    # operators / relations
    "times": "\u00d7", "cdot": "\u00b7", "div": "\u00f7", "pm": "\u00b1",
    "mp": "\u2213", "approx": "\u2248", "sim": "\u223c", "simeq": "\u2243",
    "propto": "\u221d", "neq": "\u2260", "ne": "\u2260", "leq": "\u2264",
    "le": "\u2264", "geq": "\u2265", "ge": "\u2265", "ll": "\u226a",
    "gg": "\u226b", "equiv": "\u2261", "infty": "\u221e", "partial": "\u2202",
    "nabla": "\u2207", "deg": "\u00b0", "degree": "\u00b0", "ast": "\u2217",
    "star": "\u2605", "circ": "\u2218", "bullet": "\u2022", "to": "\u2192",
    "rightarrow": "\u2192", "leftarrow": "\u2190", "Rightarrow": "\u21d2",
    "leftrightarrow": "\u2194", "langle": "\u27e8", "rangle": "\u27e9",
    "hbar": "\u210f", "ell": "\u2113", "angle": "\u2220", "perp": "\u22a5",
    "parallel": "\u2225", "sum": "\u2211", "prod": "\u220f", "int": "\u222b",
    "sqrt": "\u221a", "forall": "\u2200", "exists": "\u2203", "in": "\u2208",
    "notin": "\u2209", "subset": "\u2282", "supset": "\u2283", "cup": "\u222a",
    "cap": "\u2229", "emptyset": "\u2205", "dagger": "\u2020",
    "ddagger": "\u2021", "prime": "\u2032",
}

_LATEX_SPACING: dict[str, str] = {
    ",": "\u2009",
    ";": "\u2005", ":": "\u2005", " ": " ", "!": "", "quad": "\u2003",
    "qquad": "\u2003\u2003",
}

# LaTeX backslash-escaped literals: "\%" -> "%", "\&" -> "&", etc. These are
# characters that are special in LaTeX source and so get backslash-escaped to
# appear verbatim; we restore the plain character. Braces are intentionally
# NOT included here — inside math spans they're structural and stripped by
# _convert_math_span, so restoring them would be undone anyway.
_LATEX_ESCAPED_LITERALS = "%&#_$"

_SUP_GLYPHS: dict[str, str] = {
    "0": "\u2070", "1": "\u00b9", "2": "\u00b2", "3": "\u00b3", "4": "\u2074",
    "5": "\u2075", "6": "\u2076", "7": "\u2077", "8": "\u2078", "9": "\u2079",
    "+": "\u207a", "-": "\u207b", "=": "\u207c", "(": "\u207d", ")": "\u207e",
    "n": "\u207f", "i": "\u2071",
}
_SUB_GLYPHS: dict[str, str] = {
    "0": "\u2080", "1": "\u2081", "2": "\u2082", "3": "\u2083", "4": "\u2084",
    "5": "\u2085", "6": "\u2086", "7": "\u2087", "8": "\u2088", "9": "\u2089",
    "+": "\u208a", "-": "\u208b", "=": "\u208c", "(": "\u208d", ")": "\u208e",
}


def _script_run(body: str, kind: str) -> str:
    table = _SUP_GLYPHS if kind == "sup" else _SUB_GLYPHS
    if body and all(ch in table for ch in body):
        return "".join(table[ch] for ch in body)
    return f"<{kind}>{body}</{kind}>"


def _unescape_latex_literals(s: str) -> str:
    """Restore LaTeX backslash-escaped literals ("\\%" -> "%", "\\&" -> "&",
    ...). Must run BEFORE script/command conversion so an escaped underscore
    "\\_" isn't mistaken for a subscript operator."""
    return re.sub(r"\\([" + re.escape(_LATEX_ESCAPED_LITERALS) + r"])",
                  r"\1", s)


def _convert_latex_commands(s: str) -> str:
    s = re.sub(r"\\(?:mathrm|mathbf|mathit|text|mathsf|operatorname)\{([^{}]*)\}",
               r"\1", s)
    s = re.sub(r"\\(quad|qquad|[,;:! ])",
               lambda m: _LATEX_SPACING.get(m.group(1), ""), s)
    names = sorted(_LATEX_SYMBOLS, key=len, reverse=True)
    pat = re.compile(r"\\(" + "|".join(map(re.escape, names)) + r")(?![a-zA-Z])")
    return pat.sub(lambda m: _LATEX_SYMBOLS[m.group(1)], s)


def _convert_latex_scripts(s: str) -> str:
    s = re.sub(r"\^\{([^{}]*)\}", lambda m: _script_run(m.group(1), "sup"), s)
    s = re.sub(r"_\{([^{}]*)\}", lambda m: _script_run(m.group(1), "sub"), s)
    s = re.sub(r"\^(\\[a-zA-Z]+|[^\s{}])",
               lambda m: _script_run(m.group(1), "sup"), s)
    s = re.sub(r"_(\\[a-zA-Z]+|[^\s{}])",
               lambda m: _script_run(m.group(1), "sub"), s)
    return s


def _convert_math_span(expr: str) -> str:
    expr = _unescape_latex_literals(expr)
    expr = _convert_latex_scripts(expr)
    expr = _convert_latex_commands(expr)
    expr = expr.replace("~", "\u00a0")
    return expr.replace("{", "").replace("}", "")


def latex_to_unicode(text: str) -> str:
    """Convert simple inline LaTeX in ``text`` to Unicode / <sup> / <sub>."""
    if not text or ("$" not in text and "\\" not in text):
        return text
    text = re.sub(r"\$([^$]*)\$", lambda m: _convert_math_span(m.group(1)), text)
    if "\\" in text:
        text = _unescape_latex_literals(text)
        text = _convert_latex_scripts(text)
        text = _convert_latex_commands(text)
    return text


# ---------------------------------------------------------------------------
# Institution-number normalization.
#
# Author `insts` reference institutions by their EXPLICIT number `n`, not by
# list position, and the source sometimes numbers institutions in an order that
# reads out of sequence down the author list (e.g. the first author points at
# institution 3). The renderer handles that fine, but the raw numbering is
# tidier — and friendlier to anything that reads the emitted JSON directly — if
# numbers appear in ascending first-appearance order: the first author's first
# affiliation is 1, the next newly seen one is 2, and so on.
#
# This renumber is a pure relabeling of pointers. It remaps BOTH `institutions`
# and every author's `insts` together, per talk, then verifies the set of
# institution *names* each author resolves to is unchanged. A talk that fails
# verification (e.g. a dangling reference or duplicate number) is left exactly
# as-is so the build never breaks on one malformed record.
def _author_inst_namesets(authors: list, institutions: list):
    """(per-author frozenset of institution names, number->name dict).

    Raises ValueError on a duplicate institution number or an author reference
    to a number with no matching institution — the two cases we refuse to guess
    through, so the caller can skip the talk untouched.
    """
    num_to_name: dict = {}
    for inst in institutions:
        n = inst.get("n")
        if n in num_to_name:
            raise ValueError(f"duplicate institution number {n!r}")
        num_to_name[n] = (inst.get("name") or "").strip()
    per_author = []
    for a in authors:
        names = set()
        for n in (a.get("insts") or []):
            if n not in num_to_name:
                raise ValueError(
                    f"author {a.get('name')!r} references institution "
                    f"number {n!r} with no matching institution")
            names.add(num_to_name[n])
        per_author.append(frozenset(names))
    return per_author, num_to_name


def _renumber_talk_insts(talk: dict) -> bool:
    """Renumber one talk's institutions into author first-appearance order,
    remapping author `insts` in lockstep. Returns True if numbering changed.

    Behavior-preserving and verified: commits only after confirming every
    author still resolves to the same set of institution names. Raises
    ValueError (leaving the talk untouched) if the talk can't be safely
    renumbered; the caller decides how to handle that.
    """
    authors = talk.get("authors") or []
    institutions = talk.get("institutions") or []
    if not institutions:
        return False

    before_sets, before_num_to_name = _author_inst_namesets(
        authors, institutions)
    old_numbers = [inst.get("n") for inst in institutions]

    # New order: institution numbers in the order first referenced reading down
    # the author list; any never-referenced institution is appended afterward in
    # its original list order so nothing is dropped.
    first_seen: list = []
    seen: set = set()
    for a in authors:
        for n in (a.get("insts") or []):
            if n not in seen:
                seen.add(n)
                first_seen.append(n)
    for inst in institutions:
        n = inst.get("n")
        if n not in seen:
            seen.add(n)
            first_seen.append(n)

    remap = {old: i + 1 for i, old in enumerate(first_seen)}
    if all(remap[old] == old for old in old_numbers):
        return False  # already in target order

    inst_by_old = {inst.get("n"): inst for inst in institutions}
    new_insts = []
    for old in first_seen:
        inst = dict(inst_by_old[old])
        inst["n"] = remap[old]
        new_insts.append(inst)
    new_authors = []
    for a in authors:
        a2 = dict(a)
        a2["insts"] = [remap[n] for n in (a.get("insts") or [])]
        new_authors.append(a2)

    # Verify behavior preservation before committing.
    after_sets, after_num_to_name = _author_inst_namesets(
        new_authors, new_insts)
    if after_sets != before_sets:
        raise ValueError("author->institution name sets changed during remap")
    if sorted(before_num_to_name.values()) != sorted(after_num_to_name.values()):
        raise ValueError("institution name multiset changed during remap")

    talk["institutions"] = new_insts
    talk["authors"] = new_authors
    return True


def enrich_affiliations(data: dict) -> dict:
    sessions = data.get("sessions", [])
    talks = data.get("talks", [])

    # 0a. Normalize person names: fix ALL-CAPS tokens (longer than two
    #     letters) and lowercase first/last name tokens. Done before
    #     everything else so downstream affiliation matching, search
    #     aliases, and byline rendering all see the cleaned forms.
    normalize_names_in_data(data)

    # 0. Normalize titles: trim trailing whitespace and remove a single
    #    trailing period (leaving an ellipsis intact), so the rendered HTML
    #    never shows a sentence-final '.' on session/talk titles. Done once
    #    here, before serialization, so every place that renders a title
    #    (bubbles, detail headers, citations, search results) gets the
    #    cleaned form.
    for s in sessions:
        if s.get("title"):
            s["title"] = _strip_trailing_periods(s["title"])
    for t in talks:
        if t.get("title"):
            t["title"] = _strip_trailing_periods(t["title"])

    # Render any inline LaTeX in abstract bodies to Unicode / <sup>/<sub>.
    # The processors emit abstracts RAW; this is where that conversion now
    # lives so a different conference's processor needn't reimplement it.
    for t in talks:
        if t.get("abstract"):
            t["abstract"] = latex_to_unicode(t["abstract"])

    # 0b. Normalize institution numbering into author first-appearance order
    #     (first author's first affiliation -> 1, etc.). Runs before the step-1
    #     dedup so the two compose: renumber puts numbers in author order, then
    #     dedup (when institutions_may_dedup is set) collapses + renumbers 1..N
    #     over that already-ordered list. Each talk is remapped atomically and
    #     verified name-preserving; a talk that can't be safely renumbered is
    #     left untouched (its original numbering already renders correctly) and
    #     reported, so one malformed record never breaks the build.
    _renum_changed = 0
    _renum_skipped = 0
    for t in talks:
        try:
            if _renumber_talk_insts(t):
                _renum_changed += 1
        except ValueError as e:
            _renum_skipped += 1
            print(f"[insts] skipped {t.get('id') or t.get('number') or '<?>'}"
                  f": {e}")
    print(f"[insts] renumbered {_renum_changed} talk(s) into author order"
          + (f"; left {_renum_skipped} untouched (unsafe to renumber)"
             if _renum_skipped else ""))

    # 1. Talks: build inst_shorts (collapsing dup institutions by short name
    #    when allowed), speaker_aff, last_aff. `institutions` is the structured
    #    list [{n, name}]; `inst_shorts` runs parallel to it (the canonical
    #    short name per institution).
    for t in talks:
        authors = t.get("authors") or []
        institutions = t.get("institutions") or []

        # When the talk's institution list has no per-author index structure to
        # protect (institutions_may_dedup), collapse duplicates by canonical
        # short name and renumber 1..N. Otherwise leave numbering intact
        # (author `insts` reference exact numbers, so collapsing would break
        # those references).
        if t.get("institutions_may_dedup") and institutions:
            seen_short: set[str] = set()
            new_insts: list[dict] = []
            inst_shorts: list[str] = []
            n = 0
            for inst in institutions:
                body = (inst.get("name") or "").strip()
                short = short_affiliation(body)
                dedup_key = short or body
                if dedup_key in seen_short:
                    continue
                seen_short.add(dedup_key)
                n += 1
                new_insts.append({"n": n, "name": body,
                                  "alt_names": inst.get("alt_names") or []})
                inst_shorts.append(short)
            t["institutions"] = new_insts
            institutions = new_insts
            t["inst_shorts"] = inst_shorts
        else:
            t["inst_shorts"] = [
                short_affiliation((inst.get("name") or "").strip())
                for inst in institutions
            ]

        t["speaker_aff"] = speaker_short_aff(
            t.get("speaker", ""), authors, institutions)
        t["last_aff"] = last_author_short_aff(authors, institutions)

    # 2. Sessions: shorten the presider affiliations the scrape provided.
    #    `presider_aff_short` is the de-duped '; '-joined form (used for the
    #    single-presider bullet display and for searching). `presider_affs_short`
    #    is the per-presider list (aligned to names, NOT de-duped) used to render
    #    'Name1 (Short1), Name2 (Short2)' when there are multiple presiders.
    for s in sessions:
        s["presider_aff_short"] = short_presider_affiliations(
            s.get("presider_aff", ""))
        s["presider_affs_short"] = presider_short_aff_list(
            s.get("presider", ""), s.get("presider_aff", ""))

    # 3. Backfill presider affiliations from papers the presider authors.
    #    Build an author surname-key -> {short_aff: count} index across all
    #    talks, then for any session whose presider has no usable affiliation,
    #    assign the presider's most-common short affiliation (skipping ties).
    #    This backfill runs here in the builder, where the affiliation map is
    #    available.
    from collections import defaultdict
    author_aff_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int))
    for t in talks:
        authors = t.get("authors") or []
        institutions = t.get("institutions") or []
        if authors:
            names = [a.get("name", "").strip() for a in authors
                     if a.get("name", "").strip()]
        else:
            # No structured authors; fall back to the loose alias forms.
            names = [a.strip() for a in (t.get("author_aliases") or [])
                     if a.strip()]
        for nm in names:
            key = _name_key(nm)
            if not key:
                continue
            aff = author_short_aff(nm, authors, institutions)
            if aff:
                author_aff_counts[key][aff] += 1

    def _best_author_aff(name: str) -> str:
        counts = author_aff_counts.get(_name_key(name))
        if not counts:
            return ""
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if len(ranked) >= 2 and ranked[0][1] == ranked[1][1]:
            return ""  # tie -> ambiguous, don't guess
        return ranked[0][0]

    # 3a. Backfill missing TALK speaker affiliations from the same author index.
    #     A speaker can have no resolvable affiliation on one paper (their own
    #     institution wasn't listed) yet appear with a clear affiliation on other
    #     papers in the program — e.g. an invited speaker whose "Title coming
    #     soon" placeholder carries no institutions. When a talk's speaker_aff is
    #     empty, fill it with that speaker's most-common short affiliation across
    #     the conference (skipping ambiguous ties), exactly as for presiders.
    #     We set speaker_aff (the byline chip) AND, when the talk has no
    #     institution backing that speaker, inject a matching institution + link
    #     it to the speaker, so the affiliation also appears in the detail page's
    #     "Institutions" list (not just the byline).
    n_talk_aff_backfilled = 0
    for t in talks:
        if t.get("speaker_aff"):
            continue
        speaker = t.get("speaker", "")
        if not speaker:
            continue
        aff = _best_author_aff(speaker)
        if not aff:
            continue
        t["speaker_aff"] = aff
        n_talk_aff_backfilled += 1

        # Mirror it into the structured institution list so the detail page's
        # Institutions section shows it too. Only add when the speaker has no
        # institution of their own already (an empty/blank speaker membership);
        # find or create an institution whose short form equals the backfilled
        # affiliation, then point the speaker's author entry at it.
        authors = t.get("authors") or []
        institutions = t.get("institutions") or []
        inst_shorts = t.get("inst_shorts") or []
        spk_entry = next(
            (a for a in authors
             if _name_key(a.get("name", "")) == _name_key(speaker)), None)
        spk_has_inst = bool(spk_entry and spk_entry.get("insts"))
        if spk_has_inst:
            continue
        # Reuse an existing institution that already shortens to `aff`, else add.
        target_n = None
        for inst, short in zip(institutions, inst_shorts):
            if (short or "").strip() == aff:
                target_n = inst.get("n")
                break
        if target_n is None:
            existing_ns = [i.get("n") for i in institutions
                           if isinstance(i.get("n"), int)]
            target_n = (max(existing_ns) + 1) if existing_ns else 1
            institutions.append({"n": target_n, "name": aff, "alt_names": []})
            inst_shorts.append(aff)
            t["institutions"] = institutions
            t["inst_shorts"] = inst_shorts
        if spk_entry is not None:
            spk_entry["insts"] = [target_n]
    print(f"[affil]   talk speaker affiliations backfilled from other papers: "
          f"{n_talk_aff_backfilled}")

    n_backfilled = 0
    n_unresolved = 0
    for s in sessions:
        presider_field = s.get("presider", "")
        if not presider_field:
            continue
        names = [n.strip() for n in re.split(r";| and ", presider_field)
                 if n.strip()]
        if not names:
            continue
        # Backfill PER PRESIDER, not per session. A co-presided session can carry
        # one usable affiliation and one junk token (e.g. presider_aff
        # "a; Tufts University"): a whole-session usability gate would see the
        # usable half and skip it, leaving the junk presider permanently
        # mis-/un-attributed (and the junk token polluting the de-duped short
        # form). So we keep each presider's existing usable affiliation and only
        # backfill the slots that are missing or unusable, from that presider's
        # own papers. `presider_affs_short` was already computed (aligned to
        # `names`) from the raw scrape, so it is our per-slot starting point.
        cur = list(s.get("presider_affs_short") or [])
        per_name: list[str] = []       # aligned to names (NOT de-duped)
        changed = False
        for i, nm in enumerate(names):
            existing = cur[i] if i < len(cur) else ""
            if existing and _affiliation_is_usable(existing):
                per_name.append(existing)
                continue
            aff = _best_author_aff(nm)  # already a SHORT name (or "")
            if aff:
                per_name.append(aff)
                changed = True
            else:
                per_name.append("")
        if not changed:
            if not any(per_name):
                n_unresolved += 1
            continue
        found: list[str] = []          # de-duped, for the legacy joined fields
        seen: set[str] = set()
        for aff in per_name:
            if aff and aff not in seen:
                seen.add(aff)
                found.append(aff)
        joined = "; ".join(found)
        s["presider_aff"] = joined
        s["presider_aff_short"] = joined
        s["presider_affs_short"] = per_name
        n_backfilled += 1

    print(f"[affil]   presider affiliations backfilled from papers: "
          f"{n_backfilled} (still missing/invalid: {n_unresolved})")

    # ------------------------------------------------------------------ sort
    # The list views (renderTimeGrouped et al.) group items into time buckets
    # by walking the array and assuming chronological order — equal-time items
    # must already be adjacent, and a session/talk must not appear out of place
    # (e.g. an evening poster session emitted last in the array but starting
    # Tuesday). We do NOT rely on the processor to pre-sort; the builder sorts
    # here so any processor's output renders in the right order. Sort is stable
    # and by start_ts ascending; rows with a missing/empty start_ts sort last
    # while keeping their original relative order (a blank key would otherwise
    # sort before real timestamps).
    def _ts_key(x: dict) -> tuple[int, str]:
        ts = (x.get("start_ts") or "").strip()
        return (1, "") if not ts else (0, ts)

    data["sessions"] = sorted(sessions, key=_ts_key)
    data["talks"] = sorted(talks, key=_ts_key)

    return data


# =============================================================================
# HTML template + main
# =============================================================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<title>__CONFERENCE_NAME__</title>
<style>
:root {
  --fs: 1;            /* text-size multiplier; every font-size is calc(px * var(--fs)) */
  /* Space scale: tracks --fs when shrinking text, but never exceeds 1 when
     enlarging — so small text tightens the vertical bubble spacing, while
     large text keeps the default (compact) spacing since screen space is at
     a premium. Applied to bubble vertical padding + inter-bubble gaps. */
  --sp: min(var(--fs), 1);
  /* One shared corner radius for all rounded box/card/control surfaces, so
     they're consistent. --radius-base is the fixed value; --radius scales it
     with --sp (shrinks with small text, capped at default for large) for
     elements whose geometry scales. Fixed-size controls (the A−/A+ zoom
     buttons) use --radius-base directly so their corners match without
     scaling. Pills (999px), circles (50%), and tiny inline chips keep their
     own radii. */
  --radius-base: 10px;
  --radius: calc(var(--radius-base) * var(--sp));
  --bg:        #f6f6f4;
  --surface:   #ffffff;
  --surface-2: #f0efeb;
  --text:      #131313;
  --muted:     #6f6e6a;
  --line:      rgba(0,0,0,.08);
  --accent:    #d6541c;
  --accent-soft: rgba(214,84,28,.12);
  --accent-faint: rgba(214,84,28,.45);
  --tile-overlay: rgba(246,246,244,.65);
  /* Chrome-bar heights scale with the text-size multiplier so their labels
     never clip as the text grows (and the bars tighten up as it shrinks).
     All layout math references these via calc(), so scaling the definitions
     here flows through to content padding and bottom offsets automatically.
     Base values: tab 58, top 48, controls 40, indicator 28. */
  --tab-h:     calc(58px * var(--fs));
  --top-h:     calc(48px * var(--fs));
  --bot-h:     calc(40px * var(--fs));
  --ind-h:     calc(28px * var(--fs));
  --safe-top:    env(safe-area-inset-top, 0px);
  --safe-bottom: env(safe-area-inset-bottom, 0px);

  /* category colors: --fg = solid edge, --bg = bubble surface */
  --c-blue-fg:    #2563eb;  --c-blue-bg:    #e8efff;
  --c-violet-fg:  #7c3aed;  --c-violet-bg:  #efe9ff;
  --c-emerald-fg: #059669;  --c-emerald-bg: #def7ec;
  --c-amber-fg:   #c2750a;  --c-amber-bg:   #fdf0d6;
  --c-slate-fg:   #475569;  --c-slate-bg:   #e7eaee;
  --c-rose-fg:    #e11d48;  --c-rose-bg:    #ffe1e8;
  --c-teal-fg:    #0d9488;  --c-teal-bg:    #d6f3ef;
  --c-indigo-fg:  #4f46e5;  --c-indigo-bg:  #e6e4ff;
  --c-pink-fg:    #db2777;  --c-pink-bg:    #ffe4f1;
  --c-lime-fg:    #65a30d;  --c-lime-bg:    #ecfccb;
  --c-neutral-fg: #525252;  --c-neutral-bg: #ececea;
  --c-gold-fg:    #a16207;  --c-gold-bg:    #fef9c3;
  --c-orange-fg:  #ea580c;  --c-orange-bg:  #ffedd5;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:        #111111;
    --surface:   #1c1c1d;
    --surface-2: #232326;
    --text:      #f0eee9;
    --muted:     #a0a09c;
    --line:      rgba(255,255,255,.08);
    --accent:    #ff8c5c;
    --accent-soft: rgba(255,140,92,.18);
    --accent-faint: rgba(255,140,92,.55);
    --tile-overlay: rgba(17,17,17,.55);

    --c-blue-bg:    #1a233d;
    --c-violet-bg:  #271f3e;
    --c-emerald-bg: #133024;
    --c-amber-bg:   #36280f;
    --c-slate-bg:   #232a33;
    --c-rose-bg:    #38161f;
    --c-teal-bg:    #102b27;
    --c-indigo-bg:  #1d1a3d;
    --c-pink-bg:    #371525;
    --c-lime-bg:    #1f2810;
    --c-neutral-bg: #2a2a2a;
    --c-gold-bg:    #3a3010;
    --c-orange-bg:  #3b1d0a;
  }
}

* { box-sizing: border-box; }

/* Slim, subtle, theme-aware scrollbars. Firefox uses the standard
   scrollbar-* properties; Chromium/WebKit use the ::-webkit-scrollbar
   pseudo-elements, whose default is chunky and high-contrast. Styling both
   keeps the two browsers looking the same. Colors come from the theme vars
   so this tracks light/dark automatically. */
* {
  scrollbar-width: thin;
  scrollbar-color: var(--muted) transparent;
}
::-webkit-scrollbar {
  width: 10px;
  height: 10px;
}
::-webkit-scrollbar-track {
  background: transparent;
}
::-webkit-scrollbar-thumb {
  background: var(--muted);
  border-radius: 8px;
  /* Transparent border + background-clip leaves breathing room around the
     thumb so it reads as a thin pill rather than a full-width bar. */
  border: 2px solid transparent;
  background-clip: padding-box;
}
::-webkit-scrollbar-thumb:hover {
  background: var(--text);
  background-clip: padding-box;
  border: 2px solid transparent;
}
::-webkit-scrollbar-corner {
  background: transparent;
}

html, body {
  margin: 0; padding: 0;
  background: var(--bg);
  color: var(--text);
  font: calc(15px * var(--fs))/1.35 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
  -webkit-tap-highlight-color: transparent;
  overscroll-behavior: contain;
  /* Disable mobile browsers' automatic text-inflation heuristic. Without this,
     Firefox mobile rescales long flowing text blocks (notably the Talk-detail
     abstract + stacked author/institution copy) for "readability" while
     leaving short bubble titles alone — so Talk detail rendered at a different
     size than the rest of the app on Firefox mobile only (desktop has no text
     inflation, so it looked fine there). 100% pins text to the authored sizes
     across all engines. */
  -webkit-text-size-adjust: 100%;
  -moz-text-size-adjust: 100%;
  text-size-adjust: 100%;
}
/* Lock the document root itself. Putting overflow:hidden only on <body> wasn't
   enough: <html> remained the scrollable document, so a drag could pan the
   whole (correctly-833-tall) app up and down within a slightly taller visual
   viewport — the "drag the tab bar up, then back down" behavior, with a band
   of empty space appearing below the tab bar. Fixing the root height and
   hiding its overflow removes that document-level scroll entirely. */
html {
  height: 100%;
  overflow: hidden;
}
body {
  /* Pin the app shell to the viewport so the document has ZERO scrollable area
     and cannot be panned by a drag (only #content scrolls, internally).
     Pinning top AND bottom (not just top + a height) makes the body's height
     equal the viewport at INTEGER precision. This matters: --app-h is driven
     from visualViewport.height, which reports a FRACTIONAL value on some
     devices (e.g. 832.9166px) while the layout viewport is an integer 833 — so
     using it as the height left the body ~0.1–1px short, showing a thin strip
     of page background below the tab bar (the residual gap that "sometimes"
     closed as sub-pixel rounding shifted). top:0;bottom:0 sidesteps the
     fraction entirely. --app-h is retained only as a fallback height for
     engines where a fixed top+bottom doesn't resolve a height (rare). */
  position: fixed;
  top: 0; left: 0; right: 0; bottom: 0;
  /* top+bottom give an integer-precise height equal to the layout viewport
     (no fractional gap). --app-h, driven from min(visual, layout) viewport, is
     applied only as a max-height CAP: if the layout viewport ever exceeds the
     visible area (e.g. a Firefox toolbar state where the two diverge), this
     clamps the shell to the visible height so the bottom chrome can't fall
     below the fold. A cap can only shrink, never pad, so it cannot itself
     create a gap even though --app-h is fractional. */
  max-height: var(--app-h, 100dvh);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
button {
  background: none; border: none; color: inherit;
  font: inherit; padding: 0; cursor: pointer;
}

/* ── Top bar ───────────────────────────────────────────────────────── */
#topbar {
  flex: 0 0 auto;
  /* No built-in touch gestures on the chrome bars. They're just buttons; a
     touch-drag starting on a non-scrollable bar would otherwise fall through
     to the browser as a page pan and toggle Firefox-Android's address bar
     (the "drag from the tab/search bar makes it move" symptom). touch-action:
     none disables drag/pan interpretation here; taps/clicks are unaffected. */
  touch-action: none;
  height: calc(var(--top-h) + var(--safe-top));
  padding-top: var(--safe-top);
  background: rgb(246,246,244);
  border-bottom: 1px solid var(--line);
  /* Three columns with EQUAL side tracks so the centered title sits at the
     true center of the bar regardless of what's in the side slots (the back
     button on the left, sync/copy controls on the right). A plain flexbox
     mis-centers the title whenever the two sides differ in width — e.g. on
     the Sessions list where the left back-button is hidden but the right
     slot still reserves space. */
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  z-index: 30;
}
@media (prefers-color-scheme: dark) {
  #topbar { background: rgb(17,17,17); }
}
#back-btn {
  height: var(--top-h); padding: 0 14px;
  font-size: calc(17px * var(--fs)); color: var(--accent);
  display: flex; align-items: center; gap: 2px;
  justify-self: start;
}
#back-btn[hidden] { display: none; }
#page-title {
  margin: 0; padding: 0 8px;
  font-size: calc(16px * var(--fs)); font-weight: 600; letter-spacing: .01em;
  text-align: center;
  grid-column: 2;
}
/* Only the Me tab's top-level title ("My Schedule") is left-justified; the
   other tabs (Sessions / Talks / Search) keep their centered titles. The
   back-button-hidden check keeps drilled-in detail views (pushed from Me)
   centered next to the back arrow. */
body[data-active-tab="me"] #back-btn[hidden] ~ #page-title {
  grid-column: 1 / 3;
  text-align: left;
  padding-left: 14px;
}
#topbar-extra {
  display: flex; align-items: center;
  padding-right: 8px;
  justify-self: end;
  gap: 4px;
}
/* Compact "Last sync" text in the Me top bar (narrow / one-pane mode).
   Mirrors the wide pane header's sync text. Truncates rather than
   wrapping so it never pushes the Copy/Paste buttons off-screen. */
.topbar-sync {
  font-size: calc(11px * var(--fs)); color: var(--muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  max-width: 40vw; min-width: 0;
}
.icon-btn {
  height: 36px; min-width: 36px; padding: 0 8px;
  border-radius: var(--radius);
  font-size: calc(18px * var(--fs));
  color: var(--accent);
  display: inline-flex; align-items: center; justify-content: center;
}
.icon-btn:active { background: var(--accent-soft); }

/* ── Content ───────────────────────────────────────────────────────── */
#content {
  position: relative;
  /* The flexing middle of the app-shell column: it takes all space between the
     top bar and the bottom chrome and is the ONLY scrolling element (narrow
     layout). flex-basis:0 + flex-grow:1 lets it fill the leftover height;
     min-height:0 is required so a flex item is allowed to shrink below its
     content size and scroll internally instead of overflowing the column. */
  flex: 1 1 0;
  min-height: 0;
  /* overflow-y:scroll (not auto) so #content is ALWAYS a scroll container even
     when its content is shorter than the viewport — e.g. a short talk-detail
     page. With `auto`, a short page makes #content non-scrollable, so a swipe
     falls through to the document and Firefox treats it as a page gesture that
     toggles its toolbar (the "list pages don't move, detail pages do" symptom:
     long lists scroll #content and absorb the gesture; short details don't).
     overscroll-behavior:contain keeps the scroll chain from propagating past
     #content to the page in either direction. Together these keep every swipe
     inside #content, so the browser chrome isn't engaged by content scrolling. */
  overflow-y: scroll;
  overflow-x: hidden;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
  /* Left/right gutter is whitespace — shrink it with small text (--sp),
     capped at default for large text. Top/bottom stay fixed. */
  padding: 8px calc(12px * var(--sp)) 24px;
}

/* SVG overlay for the connector trees — the Me-tab session→talk tree
   (#me-connectors), the Session-detail elbows (#session-connectors), and
   the Sessions-list inline-expansion elbows (#session-list-connectors).
   Sits BEHIND the bubbles and headers (z-index 0). Bubbles have solid
   backgrounds and paint above; time/date headers are normal-flow so they
   also paint above. The .th-text chip below dims+blurs the line where it
   passes behind a time indicator.

   CRITICAL: this MUST be position:absolute. The SVG is inserted as the
   first child of #content; without absolute positioning it would sit in
   normal flow and push the header + talks down below it, stranding the
   lines in the empty space at the top. */
#me-connectors,
#session-connectors,
#session-list-connectors {
  position: absolute;
  top: 0; left: 0;
  /* Do NOT use inset:0 / right:0 / bottom:0 here. The SVG carries explicit
     width/height attributes equal to the container's full scrollHeight,
     and its viewBox maps 0..scrollHeight 1:1 onto that. If we also pinned
     right/bottom to 0, the used height would collapse to the VISIBLE
     padding-box height while the viewBox still spanned the full content —
     scaling/shifting every drawn coordinate (this is what pushed the blur
     fade-chips off their time labels). Anchoring only top-left keeps the
     SVG at native size so content-space coords land exactly. */
  pointer-events: none;
  z-index: 0;
  overflow: visible;
}

.date-header {
  margin: 14px 2px 4px;
  font-size: calc(11px * var(--fs)); font-weight: 700;
  letter-spacing: .14em; text-transform: uppercase;
  color: var(--muted);
}
.date-header:first-child { margin-top: 4px; }
/* The connector overlay SVG is inserted as the container's first child so
   it paints behind the content. That demotes the first .date-header from
   :first-child, which would otherwise revert its margin-top from 4px to
   14px — shifting ALL schedule content down 10px AFTER the connector
   geometry was measured, leaving every spine and fade-chip ~10px above its
   target. Keep the small top margin when the SVG precedes the header. */
#me-connectors + .date-header,
#session-connectors + .date-header,
#session-list-connectors + .date-header { margin-top: 4px; }

.time-header {
  margin: 4px 2px 2px;
  font-size: calc(11px * var(--fs)); font-weight: 600;
  color: var(--muted);
  display: flex; align-items: baseline; gap: 8px;
}
.time-header::after {
  content: ""; flex: 1; height: 1px; background: var(--line);
}
/* The connector spine that passes behind a time-header is faded by a
   blurred page-colored rect painted inside the SVG overlay (see
   addFadeChip / drawMeConnectors). The SVG is position:absolute z-index:0,
   so it paints ABOVE static content; to keep the LABEL TEXT crisp and on
   top of that blurred rect, the text span must be positioned with a
   higher z-index. Only on tabs/views that actually draw connectors. */
.time-header .th-text {
  border-radius: 4px;
  padding: 0;
}
body[data-active-tab="me"] .time-header .th-text,
body[data-active-view="session-detail"] .time-header .th-text,
#me-content .time-header .th-text {
  position: relative;
  z-index: 1;
}
/* Date headers sit directly above the first time-header of each day, so
   the time-header's blurred fade chip (which overscans upward) bleeds
   onto the date text and dims it. Lift the date header above the SVG
   overlay too — same treatment as the time-header chip — so its text
   stays crisp. Scoped to the same connector-drawing contexts. */
body[data-active-tab="me"] .date-header,
body[data-active-view="session-detail"] .date-header,
#me-content .date-header {
  position: relative;
  z-index: 1;
}

/* ── Bubble (the calendar entry) ───────────────────────────────────── */
.bubble {
  position: relative;
  background: var(--c-neutral-bg);
  border-left: calc(4px * var(--sp)) solid var(--c-neutral-fg);
  border-radius: var(--radius);
  /* All inner spacing and the inter-bubble gap scale with --sp (shrink with
     small text, capped at default for large text). The RIGHT inset reserves
     room for the +/− circle (which also shrink-scales). The LEFT inset and
     the colored border BOTH scale by --sp so they shrink together — and
     because session (6+11) and talk (3+14) left-edges each total 17px, they
     stay aligned at every scale. */
  margin: calc(5px * var(--sp)) 0;
  padding: calc(7px * var(--sp)) calc(50px * var(--sp)) calc(7px * var(--sp)) calc(11px * var(--sp));
  min-height: calc(44px * var(--sp));
  display: flex; flex-direction: column; justify-content: center;
  cursor: pointer;
  -webkit-user-select: none; user-select: none;
  transition: transform .08s ease, background .15s ease;
}
.bubble:active { transform: scale(.985); }

/* While the user is dragging the Me-pane resizer, the left content area and
   the Me pane both change width on every animation frame. Without help, every
   bubble in both panes re-measures on every frame — hundreds of bubbles, lots
   of work, frame drops. `content-visibility: auto` lets the browser skip
   layout and paint for bubbles that are scrolled off-screen, which is most of
   them in a long list. `contain-intrinsic-size` gives the skipped bubbles a
   placeholder height (estimated ~52px, close to typical) so the scrollbar
   doesn't jump as bubbles enter/leave the viewport. Restricted to the drag
   via `body.me-resizing` so normal interactions (where things like the
   connector SVG read offsetTop of every bubble) are unaffected. */
body.me-resizing .bubble {
  content-visibility: auto;
  contain-intrinsic-size: auto 52px;
}

.bubble.clr-blue    { background: var(--c-blue-bg);    border-left-color: var(--c-blue-fg); }
.bubble.clr-violet  { background: var(--c-violet-bg);  border-left-color: var(--c-violet-fg); }
.bubble.clr-emerald { background: var(--c-emerald-bg); border-left-color: var(--c-emerald-fg); }
.bubble.clr-amber   { background: var(--c-amber-bg);   border-left-color: var(--c-amber-fg); }
.bubble.clr-slate   { background: var(--c-slate-bg);   border-left-color: var(--c-slate-fg); }
.bubble.clr-rose    { background: var(--c-rose-bg);    border-left-color: var(--c-rose-fg); }
.bubble.clr-teal    { background: var(--c-teal-bg);    border-left-color: var(--c-teal-fg); }
.bubble.clr-indigo  { background: var(--c-indigo-bg);  border-left-color: var(--c-indigo-fg); }
.bubble.clr-pink    { background: var(--c-pink-bg);    border-left-color: var(--c-pink-fg); }
.bubble.clr-lime    { background: var(--c-lime-bg);    border-left-color: var(--c-lime-fg); }
.bubble.clr-gold    { background: var(--c-gold-bg);    border-left-color: var(--c-gold-fg); }
.bubble.clr-orange  { background: var(--c-orange-bg);  border-left-color: var(--c-orange-fg); }

.bubble-loc {
  /* The time/location prefix at the start of the subtitle line. Same font,
     size, and color as the author byline it precedes — no pill, no monospace.
     A plain " · " (identical to the separators WITHIN the byline) divides it
     from the author/presider that follows when both are present. */
  color: inherit;
}
/* The slight dim sits on the text content only, not the whole .bubble.
   Putting it on the bubble would make the bubble's background translucent
   too, so the connector spine running up behind a bubble would be faintly
   visible through it. Dimming just the title + subtitle keeps the look while
   the bubble's background stays fully opaque and cleanly hides the line. */
.bubble-title {
  font-size: calc(14px * var(--fs)); line-height: 1.25; font-weight: 600;
  overflow: hidden; text-overflow: ellipsis;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  opacity: .92;
}
.bubble-sub {
  font-size: calc(12px * var(--fs)); color: var(--muted);
  margin-top: 2px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  opacity: .92;
}
/* The speaker is marked by an underline only — it inherits the gray byline
   color like every other author (no color override). */
.speaker {
  text-decoration: underline;
  text-decoration-thickness: 1.5px;
  text-underline-offset: 2px;
}
/* In affiliation / co-author search, the author(s) that caused this talk to
   be returned are brightened to the full text color (white in dark mode) so
   it's clear why the result matched. No bold — color carries it. */
.author-hit {
  color: var(--text);
}

/* ── Bubble kind styling — uniform across every view ──────────────────
   Talks and sessions look the SAME wherever they appear; the only
   view-specific treatment is the indentation/connectors on the Me schedule
   and session-detail (handled separately below). Everything else — bar
   thickness, text alignment, title size, the slight talk de-emphasis — is
   global so a talk bubble is a talk bubble and a session bubble is a session
   bubble, regardless of which list it's in.

   Sessions are the structurally bigger unit: a thicker 6px accent bar, a
   touch more vertical presence, and a slightly larger title. */
.bubble[data-kind="session"] {
  border-left-width: calc(6px * var(--sp));
  padding-top: calc(9px * var(--sp)); padding-bottom: calc(9px * var(--sp));
}
.bubble[data-kind="session"] .bubble-title {
  font-size: calc(14.5px * var(--fs));
}

/* Talks are lighter: a thinner 3px accent bar. The extra 3px of left padding
   compensates for the thinner bar so a talk's TEXT edge lines up with session
   text (border + padding = 6+11 = 3+14) — they share a left edge in mixed
   lists. (The slight opacity dim is on the base .bubble, shared by both.) */
.bubble[data-kind="talk"] {
  border-left-width: calc(3px * var(--sp));
  padding-left: calc(14px * var(--sp));
}

/* The ONE genuinely view-specific difference: indent talks so they nest
   visually under their parent session. This is a CONTAINMENT cue, so it only
   applies where a parent session actually sits directly above its child
   talks — the Me schedule and session-detail. Flat lists (Sessions, Talks,
   search results) show talks and sessions as independent rows, no nesting. */
body[data-active-view="schedule"]       .bubble[data-kind="talk"],
body[data-active-view="session-detail"] .bubble[data-kind="talk"],
#me-content                              .bubble[data-kind="talk"] {
  /* Nesting indent is whitespace, so it shrinks with small text (via --sp)
     but never grows past its default — matching the vertical-margin rule. */
  margin-left: calc(22px * var(--sp));
}
.bubble.added { box-shadow: inset 0 0 0 1.5px var(--accent); }

/* "Partial" — session is not in schedule, but at least one of its talks is.
   A thinner, lower-opacity outline so it sits between unscheduled and fully
   added. */
.bubble.partial { box-shadow: inset 0 0 0 1px var(--accent-faint); }

.schedule-btn {
  /* Shrinks with small text (via --sp), capped at default for large text —
     matching the bubble margins. The glyph is drawn as two bars (::before
     horizontal, ::after vertical) that are absolutely centered, so the +/-
     is always dead-center regardless of font metrics. --sb-d is the circle
     diameter; bars are sized from it. */
  --sb-d: calc(36px * var(--sp));
  position: absolute; top: 50%; right: calc(6px * var(--sp));
  transform: translateY(-50%);
  width: var(--sb-d); height: var(--sb-d);
  border-radius: 50%;
  background: rgba(255,255,255,.55);
  color: var(--text);
  display: block;          /* bars are positioned against this box */
}
/* Horizontal bar — present in BOTH states (it's the "−", and the crossbar of
   the "+"). Vertical bar (::after) is added only when NOT scheduled, turning
   the minus into a plus. Both centered via the 50%/50% + translate trick. */
.schedule-btn::before,
.schedule-btn::after {
  content: "";
  position: absolute; top: 50%; left: 50%;
  background: currentColor;
  border-radius: 1px;
}
.schedule-btn::before {              /* horizontal bar */
  width: calc(var(--sb-d) * 0.42); height: calc(var(--sb-d) * 0.066);
  transform: translate(-50%, -50%);
}
.schedule-btn::after {               /* vertical bar (plus only) */
  width: calc(var(--sb-d) * 0.066); height: calc(var(--sb-d) * 0.42);
  transform: translate(-50%, -50%);
}
/* Scheduled => minus: hide the vertical bar. */
.bubble.added .schedule-btn::after { display: none; }
@media (prefers-color-scheme: dark) {
  .schedule-btn { background: rgba(255,255,255,.12); }
}
.bubble.added .schedule-btn {
  background: var(--accent); color: white;
}
.schedule-btn:active { transform: translateY(-50%) scale(.92); }

/* ── Empty state ───────────────────────────────────────────────────── */
.empty {
  padding: 60px 24px;
  text-align: center;
  color: var(--muted);
  font-size: calc(14px * var(--fs));
}

/* Sync-status line at the top of the Me tab. Subtle — informational,
   not a CTA. */
.sync-banner {
  margin: 0 0 12px;
  padding: 8px 12px;
  background: var(--surface-2);
  border-radius: var(--radius);
  color: var(--muted);
  font-size: calc(12px * var(--fs));
  text-align: center;
}

/* Settings section below Notes on the Me page. */
.me-settings {
  margin: 24px 0 24px;
}
/* About is the last block on the page — a little extra room beneath it. */
.me-about { margin-bottom: 32px; }
/* Text-size control row: magnifying-glass zoom-out / "130%" / zoom-in. The step buttons mirror
   the pill styling of .copy-notes-btn; a button dims when its rail is
   reached (disabled). The cluster is centered. */
.fs-control {
  display: flex; align-items: center; justify-content: center;
  gap: 10px;
}
.fs-control .fs-btn {
  flex: 0 0 auto;
  display: inline-flex; align-items: center; justify-content: center;
  /* Height matches the .copy-notes-btn at default zoom (its 8px padding +
     1px borders + ~17px text line ≈ 36px) so the two buttons read as the
     same control family. Fixed (doesn't track --fs) — the zoom control
     shouldn't resize itself as you zoom. */
  height: 36px;
  padding: 0 18px;
  border-radius: var(--radius-base);
  background: var(--surface-2);
  color: var(--text);
  border: 1px solid var(--line);
  -webkit-tap-highlight-color: var(--accent-soft);
}
.fs-control .fs-btn svg { display: block; width: 20px; height: 20px; }
.fs-control .fs-btn:active:not(:disabled) {
  background: var(--accent-soft); border-color: var(--accent); color: var(--accent);
}
.fs-control .fs-btn:disabled { opacity: .4; cursor: default; }
.fs-control .fs-pct {
  flex: 0 0 auto;
  min-width: 52px; text-align: center;
  /* Fixed at the base size (no var(--fs)) so the readout itself doesn't
     resize as you step — it stays a stable reference while everything
     around it scales. */
  font-size: 15px; font-variant-numeric: tabular-nums;
  color: var(--muted);
}

/* Notes section header laid out as a row: the "NOTES" label on the left, a
   compact copy-everything control pinned right. The .section-title margins
   are preserved (the row IS the title element), so the box still sits the
   same distance below. */
.notes-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
/* The label inside the row must not inherit the row's flex stretching; it
   keeps the title's letter-spacing/size from .section-title. */
.notes-head > span:first-child { flex: 0 1 auto; }
.notes-copy-all {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex: 0 0 auto;
  padding: 4px 9px;
  border-radius: 999px;
  background: var(--surface-2);
  border: 1px solid var(--line);
  color: var(--muted);
  cursor: pointer;
  /* Reset the uppercase/letter-spacing the .section-title would impose so the
     little label reads normally, not as a spaced-out caps run. */
  font-size: calc(11px * var(--fs));
  font-weight: 600;
  letter-spacing: normal;
  text-transform: none;
  -webkit-tap-highlight-color: var(--accent-soft);
  transition: color .12s, border-color .12s, background .12s;
}
.notes-copy-all:hover { color: var(--text); }
.notes-copy-all:active {
  background: var(--accent-soft);
  border-color: var(--accent);
  color: var(--accent);
}
.notes-copy-ico {
  display: inline-flex;
  font-size: calc(13px * var(--fs));   /* sizes the 1em SVG */
  line-height: 0;
}
.notes-copy-label { white-space: nowrap; }

/* Attribution block, inside the About section on the Me page. Center-aligned
   content (the section label above stays left). */
.me-attribution {
  text-align: center;
  color: var(--muted);
  font-size: calc(14px * var(--fs));
  margin: 0;
}
/* Curator credit, shown below the app attribution and set a little apart from
   it. Matches the muted, centered attribution styling above it. */
.me-curator {
  text-align: center;
  color: var(--muted);
  font-size: calc(14px * var(--fs));
  margin: 10px 0 0;
}
/* Split-rights notice, set slightly apart from the name above it. */
.me-rights {
  margin-top: 10px;
}
/* Subtle link — inherits the muted attribution color rather than the loud
   accent, with just a faint underline to signal it's tappable. */
.me-attribution-link {
  color: inherit;
  text-decoration: underline;
  text-decoration-color: var(--line);
  text-underline-offset: 2px;
}
.me-attribution-link:active { color: var(--accent); }

/* ── Search bar ────────────────────────────────────────────────────── */
.search-controls {
  position: sticky; top: 0;
  background: var(--bg);
  padding: 4px 0 10px;
  z-index: 5;
}
.search-controls input[type=search] {
  width: 100%;
  height: 40px;
  padding: 0 12px;
  font-size: calc(15px * var(--fs));
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  color: var(--text);
  outline: none;
}
.search-controls input[type=search]:focus {
  border-color: var(--accent);
}
/* One-tap suggestion bubbles under the search box — exact affiliation /
   co-author jumps that mirror the clickable short-affiliation pills in the
   detail views. Sits between the (sticky) input and the results list. */
.search-suggest {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0 2px 4px;
}
.search-suggest:empty { display: none; }
.search-suggest .suggest-bubble {
  font: inherit;
  font-size: calc(12px * var(--fs));
  font-weight: 600;
  color: var(--accent);
  background: var(--accent-soft);
  border: 1px solid transparent;
  padding: 4px 11px;
  border-radius: 999px;
  cursor: pointer;
  -webkit-tap-highlight-color: var(--accent-soft);
  white-space: nowrap;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
}
.search-suggest .suggest-bubble:hover,
.search-suggest .suggest-bubble:active {
  border-color: var(--accent);
}

/* ── Session / Talk detail views ───────────────────────────────────── */
.detail-head {
  position: relative;
  padding: 14px calc(56px * var(--sp)) 16px 16px;
  border-radius: var(--radius);
  background: var(--c-neutral-bg);
  border-left: 4px solid var(--c-neutral-fg);
  margin-bottom: 16px;
}
.detail-head.clr-blue    { background: var(--c-blue-bg);    border-left-color: var(--c-blue-fg); }
.detail-head.clr-violet  { background: var(--c-violet-bg);  border-left-color: var(--c-violet-fg); }
.detail-head.clr-emerald { background: var(--c-emerald-bg); border-left-color: var(--c-emerald-fg); }
.detail-head.clr-amber   { background: var(--c-amber-bg);   border-left-color: var(--c-amber-fg); }
.detail-head.clr-slate   { background: var(--c-slate-bg);   border-left-color: var(--c-slate-fg); }
.detail-head.clr-rose    { background: var(--c-rose-bg);    border-left-color: var(--c-rose-fg); }
.detail-head.clr-teal    { background: var(--c-teal-bg);    border-left-color: var(--c-teal-fg); }
.detail-head.clr-indigo  { background: var(--c-indigo-bg);  border-left-color: var(--c-indigo-fg); }
.detail-head.clr-pink    { background: var(--c-pink-bg);    border-left-color: var(--c-pink-fg); }
.detail-head.clr-lime    { background: var(--c-lime-bg);    border-left-color: var(--c-lime-fg); }
.detail-head.clr-gold    { background: var(--c-gold-bg);    border-left-color: var(--c-gold-fg); }
.detail-head.clr-orange  { background: var(--c-orange-bg);  border-left-color: var(--c-orange-fg); }

/* The session/talk id is the page title in the top bar for detail views
   (see pageTitleFor), so the detail head doesn't render it again — no chip
   class is needed here. */
.dh-title {
  margin: 0 0 6px;
  font-size: calc(18px * var(--fs)); line-height: 1.3; font-weight: 700;
}
.dh-meta {
  font-size: calc(12.5px * var(--fs)); color: var(--muted);
  margin-top: 2px;
}
.dh-meta strong { color: var(--text); font-weight: 600; }
.dh-add {
  /* Detail-head add/remove circle. Same construction as .schedule-btn: a
     bar-drawn +/- (perfectly centered, font-metric-independent) on a circle
     that shrink-scales via --sp. --dh-d is the diameter; bars derive from it. */
  --dh-d: calc(38px * var(--sp));
  position: absolute; top: calc(12px * var(--sp)); right: calc(12px * var(--sp));
  width: var(--dh-d); height: var(--dh-d);
  border-radius: 50%;
  background: rgba(255,255,255,.55);
  color: var(--text);
  display: block;
}
.dh-add::before,
.dh-add::after {
  content: "";
  position: absolute; top: 50%; left: 50%;
  background: currentColor;
  border-radius: 1px;
}
.dh-add::before {                    /* horizontal bar (− and +'s crossbar) */
  width: calc(var(--dh-d) * 0.42); height: calc(var(--dh-d) * 0.066);
  transform: translate(-50%, -50%);
}
.dh-add::after {                     /* vertical bar (plus only) */
  width: calc(var(--dh-d) * 0.066); height: calc(var(--dh-d) * 0.42);
  transform: translate(-50%, -50%);
}
.dh-add.added::after { display: none; }   /* scheduled => minus */
@media (prefers-color-scheme: dark) {
  .dh-add { background: rgba(255,255,255,.14); }
}
.dh-add.added { background: var(--accent); color: white; }
.dh-add:active { transform: scale(.92); }

.section-title {
  font-size: calc(11px * var(--fs)); font-weight: 700;
  letter-spacing: .14em; text-transform: uppercase;
  color: var(--muted);
  margin: 18px 2px 6px;
}
/* In a Session detail the header is immediately followed by the talk
   list (no "Talks" heading, and — since each talk now carries its time
   inline — no between-bubble time-headers either). The default 16px
   detail-head bottom margin leaves an awkwardly large gap above the
   first talk, so tighten it to bring the talks up under the header. */
body[data-active-view="session-detail"] .detail-head {
  margin-bottom: 6px;
}

/* ── Sessions list: inline expansion ──────────────────────────────────
   A session bubble in the Sessions list toggles open in place. The bubble
   itself fills with the session's full detail (date, tags, presider,
   details), and its talk bubbles render directly beneath it. No separate
   indented header card. */

/* The expansion container holds only the talk bubbles for one open session.
   The left padding is the single source of the nesting indent (and the
   gutter the connector spine drops through) — talks must NOT also carry a
   margin-left or they'd be double-indented. */
.session-expansion {
  margin: 0 0 calc(5px * var(--sp)) 0;
  padding-left: calc(22px * var(--sp));
  position: relative;
}

.author-line {
  font-size: calc(14px * var(--fs)); line-height: 1.55;
  margin: 0 2px;
}
.author-line .aff {
  font-size: calc(9.5px * var(--fs)); font-weight: 600;
  color: var(--muted);
  margin-left: 1px;
  letter-spacing: .01em;
}

/* Tappable people-search affordance on author and presider names. Kept
   visually light (a dotted underline) so the author line still reads as
   prose; the speaker keeps its solid bold underline on top. */
.author-name.clickable,
.presider-name.clickable,
.presider-aff.clickable {
  cursor: pointer;
  text-decoration: underline dotted var(--muted);
  text-underline-offset: 2px;
  -webkit-tap-highlight-color: var(--accent-soft);
}
.author-name.clickable:hover,
.author-name.clickable:active,
.presider-name.clickable:hover,
.presider-name.clickable:active,
.presider-aff.clickable:hover,
.presider-aff.clickable:active {
  color: var(--accent);
  text-decoration-color: var(--accent);
}
/* The speaker among the authors is bold-underlined; keep that styling
   dominant but still show it's tappable. */
.author-name.speaker {
  text-decoration: underline solid var(--text);
  text-decoration-thickness: 1.5px;
}
.author-name.speaker:hover,
.author-name.speaker:active {
  text-decoration-color: var(--accent);
}

/* Banner atop a temporary click-search results view, naming what was
   tapped to get here. */
.click-search-banner {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  margin: 4px 2px 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--line);
}
.click-search-banner .csb-kind {
  font-size: calc(11px * var(--fs));
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .04em;
  color: var(--accent);
  background: var(--accent-soft);
  padding: 2px 8px;
  border-radius: 999px;
}
.click-search-banner .csb-query {
  font-size: calc(15px * var(--fs));
  font-weight: 600;
  color: var(--text);
}

/* A real ordered list: the browser draws the "1." markers and handles the
   hanging indent / baseline alignment natively (list-style-position: outside),
   so numbers sit on the text baseline and wrapped lines align under the text —
   no manual gutter. The marker inherits the text's font and color. padding-left
   reserves room for the marker (incl. two-digit indices). */
.inst-list {
  margin: 0;
  padding-left: 22px;
  list-style-position: outside;
}
.inst-list li {
  font-size: calc(13px * var(--fs));
  line-height: 1.45;
  color: var(--text);
  padding: 3px 0;
}
.inst-list li::marker {
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.inst-list li.clickable {
  cursor: pointer;
  border-radius: 6px;
  -webkit-tap-highlight-color: var(--accent-soft);
}
.inst-list li.clickable:hover,
.inst-list li.clickable:active {
  background: var(--accent-soft);
}
.inst-list .inst-short {
  display: inline-block;
  font-size: calc(11px * var(--fs));
  font-weight: 600;
  letter-spacing: .02em;
  color: var(--accent);
  background: var(--accent-soft);
  padding: 2px 8px;
  border-radius: 999px;
  white-space: nowrap;
  margin-left: 8px;
  vertical-align: 2px;
}

.abstract-body {
  font-size: calc(14px * var(--fs)); line-height: 1.5;
  margin: 0;
  white-space: pre-wrap;
}

/* ── Notes box (in session/talk detail views) ───────────────────── */
.notes-section { margin-top: 18px; }
/* Make the page-level "general conference notes" heading read exactly like the
   day headers above it. The plain .section-title looked dim on the Me page
   only because the connector SVG overlay (position:absolute, z-index:0) paints
   a blurred page-colored layer OVER static content, dimming anything beneath
   it — which is why .date-header is explicitly lifted to z-index:1 there. The
   colour/size/weight already match .date-header (both use --muted, 11px/700,
   uppercase); the missing piece was the z-index lift, so add just that. */
.section-title.notes-title--bright {
  position: relative;
  z-index: 1;
}
.notes-textarea {
  display: block;
  width: 100%;
  min-height: 50px;
  padding: 10px 12px;
  font: inherit;
  font-size: calc(14px * var(--fs));
  line-height: 1.45;
  color: var(--text);
  background: var(--surface-2);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  resize: none;            /* sized via JS to fit content */
  outline: none;
  -webkit-user-select: text; user-select: text;
  box-sizing: border-box;
  transition: border-color .15s ease, background .15s ease;
}
.notes-textarea::placeholder { color: var(--muted); opacity: .75; }
.notes-textarea:focus {
  border-color: var(--accent);
  background: var(--surface);
}
/* Taller variant for the page-level "general conference notes" box. The
   min-height (~4 lines of text + padding) holds even after JS autosizing, so
   the box starts at four lines. */
.notes-textarea--tall { min-height: 104px; }

/* ── Sticky scroll indicator (current date + time) ─────────────────── */
#scroll-indicator {
  /* A flex child between the top bar and #content: when shown it takes its own
     row, so #content naturally sits below it (no body padding hack needed as
     in the old fixed-position layout). flex:0 0 auto holds its height. */
  flex: 0 0 auto;
  height: var(--ind-h);
  display: none;
  align-items: center; justify-content: flex-start;
  padding: 0 16px;
  background: var(--surface-2);
  border-bottom: 1px solid var(--line);
  /* Match .time-header so it reads as the same kind of label. */
  font-size: calc(13px * var(--fs)); font-weight: 600;
  color: var(--text);
  z-index: 28;
  -webkit-user-select: none; user-select: none;
}
#scroll-indicator .sep {
  color: var(--muted);
  margin: 0 8px;
}
/* Right-aligned usage hint on the Sessions list indicator. Pushed to the far
   right with margin-left:auto; faint and smaller so it reads as a quiet hint
   next to the date/time label. Hidden on very narrow viewports where it would
   crowd the date/time. */
#scroll-indicator .ind-hint {
  margin-left: auto;
  font-size: calc(11px * var(--fs)); font-weight: 600;
  color: var(--muted);
  opacity: .8;
  white-space: nowrap;
}
@media (max-width: 360px) {
  #scroll-indicator .ind-hint { display: none; }
}
body.has-indicator #scroll-indicator { display: flex; }

/* ── Types filter panel ────────────────────────────────────────────── */
.types-toggle {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 10px;
  margin-left: 4px;
  border-radius: var(--radius);
  color: var(--muted);
  font-size: calc(13px * var(--fs));
}
.types-toggle:active { background: var(--accent-soft); }

/* Date-range button in the bottom bar — same shape as .types-toggle so
   the three controls form a consistent row. Goes orange when a non-empty
   range is active. */
.dr-toggle {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 10px;
  margin-left: 4px;
  border-radius: var(--radius);
  color: var(--muted);
  font-size: calc(13px * var(--fs));
  background: transparent;
}
.dr-toggle:active     { background: var(--accent-soft); }
.dr-toggle.active     { color: var(--accent); }
.dr-toggle.active #dr-arrow { color: var(--accent); }

/* Date-range sheet. Reuses .sheet-overlay / .sheet-card / .sheet-btns
   from the sync sheet so the visual is consistent — just adds the
   toggleable day grid. Each tile is a button; .selected fills it with
   the accent color. */
.dr-day-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(78px, 1fr));
  gap: 8px;
  margin: 12px 0 4px;
}
.dr-day {
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  gap: 2px;
  min-height: 56px;
  padding: 8px 6px;
  border-radius: var(--radius);
  background: var(--surface-2);
  color: var(--text);
  border: 1px solid var(--line);
  -webkit-tap-highlight-color: transparent;
  transition: background .12s ease, color .12s ease, border-color .12s ease;
}
.dr-day:active { background: var(--accent-soft); }
.dr-day.selected {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}
.dr-day-dow {
  font-size: calc(11px * var(--fs));
  font-weight: 600;
  letter-spacing: .04em;
  text-transform: uppercase;
  opacity: .85;
}
.dr-day-num {
  font-size: calc(14px * var(--fs));
  font-weight: 500;
}
#types-panel {
  position: fixed;
  left: 0; right: 0;
  bottom: calc(var(--tab-h) + var(--safe-bottom) + var(--bot-h));
  background: var(--surface);
  border-top: 1px solid var(--line);
  box-shadow: 0 -8px 24px rgba(0,0,0,.08);
  max-height: 55vh;
  overflow-y: auto;
  z-index: 19;
  /* Closed = display:none, so the panel is not laid out or painted AT ALL and
     therefore can never peek above the bottom bars during a scroll repaint
     (the earlier translateY/visibility approach still occupied layout and a
     short panel's slide-down didn't always clear the visible area). The
     trade-off is that display can't be transitioned, so there's no slide-OUT
     animation — closing is instant. Opening still animates: .open is displayed
     and slides up from translateY(110%) to 0 via the keyframe below. */
  display: none;
  padding: 8px 12px 12px;
}
#types-panel.open {
  display: block;
  animation: types-slide-up .22s ease;
}
@keyframes types-slide-up {
  from { transform: translateY(110%); }
  to   { transform: translateY(0); }
}
.types-row {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 4px;
  border-bottom: 1px solid var(--line);
  font-size: calc(14px * var(--fs));
  cursor: pointer;
}
.types-row:last-child { border-bottom: none; }
.types-row .swatch {
  width: 14px; height: 14px;
  border-radius: 4px;
  flex: 0 0 14px;
  border: 1px solid rgba(0,0,0,.1);
}
.types-row .label { flex: 1; }
.types-row .count { color: var(--muted); font-size: calc(12px * var(--fs)); }
.types-row input[type=checkbox] {
  width: 18px; height: 18px;
  accent-color: var(--accent);
  flex: 0 0 18px;
}
.types-row.off { opacity: .55; }

/* ── Bottom controls + tab bar ─────────────────────────────────────── */
#bottom-controls {
  flex: 0 0 auto;
  touch-action: none;   /* see #topbar — keep bar drags from panning the page */
  height: var(--bot-h);
  background: rgb(246,246,244);
  border-top: 1px solid var(--line);
  display: flex; align-items: center; justify-content: center;
  font-size: calc(13px * var(--fs)); color: var(--muted);
  z-index: 20;
}
@media (prefers-color-scheme: dark) {
  #bottom-controls { background: rgb(17,17,17); }
}
#bottom-controls[hidden] { display: none !important; }
#bottom-controls label {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 6px 12px;
  cursor: pointer;
}
/* "Show me" sits to the left of the Concluded checkbox as a standalone
   prefix (a nod to the Show-Me State). A touch of right margin separates it
   from the box without doubling up on the label's own 8px gap. */
#show-me-label { margin-right: 2px; white-space: nowrap; }
/* When the bar overflows the screen (large text), fitBottomControls adds
   .compact, which drops the "Show me:" prefix to reclaim width. */
#bottom-controls.compact #show-me-label { display: none; }
#bottom-controls input[type=checkbox] {
  width: 16px; height: 16px;
  accent-color: var(--accent);
}

#tabbar {
  flex: 0 0 auto;
  touch-action: none;   /* see #topbar — drags on the tab bar must not pan/toggle chrome */
  height: calc(var(--tab-h) + var(--safe-bottom));
  padding-bottom: var(--safe-bottom);
  background: var(--surface);
  border-top: 1px solid var(--line);
  display: grid; grid-template-columns: repeat(4, 1fr);
  z-index: 25;
}
.tab-btn {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 2px;
  font-size: calc(11px * var(--fs));
  color: var(--muted);
  height: 100%;
}
.tab-btn .glyph {
  font-size: calc(19px * var(--fs)); line-height: 1;
}
.tab-btn.active { color: var(--accent); }
.tab-btn:active { background: var(--accent-soft); }

/* ── Toast ─────────────────────────────────────────────────────────── */
.toast {
  position: fixed; left: 50%; bottom: calc(var(--tab-h) + var(--safe-bottom) + var(--bot-h) + 16px);
  transform: translate(-50%, 10px);
  background: var(--text);
  color: var(--bg);
  padding: 10px 16px;
  border-radius: var(--radius);
  font-size: calc(13px * var(--fs));
  opacity: 0;
  transition: opacity .25s ease, transform .25s ease;
  z-index: 100;
  max-width: calc(100vw - 32px);
  text-align: center;
}
.toast.show {
  opacity: .96;
  transform: translate(-50%, 0);
}

/* ── Sync sheet (copy/paste fallback) ──────────────────────────────── */
.sheet-overlay {
  position: fixed; inset: 0;
  background: rgba(0,0,0,.45);
  z-index: 110;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  padding: 16px;
  padding-bottom: calc(16px + var(--safe-bottom));
  animation: sheet-fade .15s ease;
}
@media (min-width: 520px) {
  .sheet-overlay { align-items: center; }
}
@keyframes sheet-fade { from { opacity: 0 } to { opacity: 1 } }
@keyframes sheet-rise {
  from { transform: translateY(20px); opacity: .6; }
  to   { transform: translateY(0);    opacity: 1; }
}
.sheet-card {
  background: var(--surface);
  border-radius: var(--radius);
  padding: 16px;
  width: 100%;
  max-width: 420px;
  box-shadow: 0 12px 36px rgba(0,0,0,.32);
  animation: sheet-rise .18s ease;
}
.sheet-title {
  font-size: calc(16px * var(--fs)); font-weight: 700; margin: 0 0 4px;
}
.sheet-hint {
  font-size: calc(13px * var(--fs)); color: var(--muted); margin: 0 0 10px;
}
.sheet-textarea {
  width: 100%; min-height: 90px;
  padding: 10px 12px;
  font: calc(12px * var(--fs))/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  background: var(--surface-2);
  color: var(--text);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  resize: none;
  -webkit-user-select: text; user-select: text;
  word-break: break-all;
}
.sheet-textarea:focus { outline: 2px solid var(--accent); outline-offset: -2px; }
.sheet-btns {
  display: flex; gap: 8px; justify-content: flex-end;
  margin-top: 12px;
}
.sheet-btn {
  height: 38px; padding: 0 16px;
  border-radius: var(--radius);
  font: 600 14px inherit;
  -webkit-tap-highlight-color: transparent;
}
.sheet-btn-cancel { color: var(--muted); }
.sheet-btn-cancel:active { background: var(--surface-2); }
.sheet-btn-primary { background: var(--accent); color: #fff; }
.sheet-btn-primary:active { filter: brightness(.92); }

/* ── Wide-screen two-pane layout ───────────────────────────────────────
   On wide screens (desktops/laptops) we show TWO panes side by side:

     - LEFT pane  : the normal single-pane app, but limited to the
                    Sessions / Talks / Search tabs in its bottom tab bar.
                    Keeps the top bar, Back, Types panel, scroll
                    indicator, and its (now 3-button) tab bar — all
                    clipped to the left column.
     - RIGHT pane : a permanently-affixed "Me" pane (#me-pane), fixed to
                    the right edge and always visible. It has its OWN
                    bottom tab bar (#me-tabbar) containing a single,
                    permanently-active "Me" button — visually continuous
                    with the left tab bar — plus its own scrollable
                    content (#me-content) rendered by renderMePane().

   Two things span the FULL width across BOTH panes:
     - the "Show past / Days / Types" controls bar (#bottom-controls),
       which now applies to whatever the LEFT pane is showing and sits
       above both tab bars; and
     - the bottom tab-bar band itself (left #tabbar + right #me-tabbar
       together read as one continuous bar).

   The pane width (--me-w) defaults to one third of the viewport and is
   user-adjustable via a drag handle on the pane's left edge; the chosen
   width is persisted (and synced) — see applyMeWidth()/state.meWidth.

   #me-pane is hidden entirely on narrow screens (the phone layout is
   unchanged — Me stays a normal bottom tab there). */
:root {
  /* Default: one third of the viewport. JS overrides this inline on
     <html> from saved state, and clamps it to a sensible range. */
  --me-w: 33.3333vw;
  /* Width of the draggable divider's hit area. */
  --me-grip: 10px;
}

@media __WIDE_QUERY__ {
  /* The base (narrow) layout makes the chrome flex children of an app-shell
     column. The wide layout instead uses explicit fixed positioning (it needs
     to clip the left column at the Me-pane edge and let the controls bar span
     both panes), so re-establish position:fixed and the viewport anchors here
     for each piece that the narrow layout had turned into a flex child. */
  #topbar {
    position: fixed; top: 0; left: 0;
  }
  #scroll-indicator {
    position: fixed; top: calc(var(--top-h) + var(--safe-top)); left: 0;
  }
  #tabbar {
    position: fixed; bottom: 0; left: 0;
  }

  /* Left-column fixed chrome stops at the pane's left edge.
     NOTE: #bottom-controls is intentionally NOT clipped — it spans the
     full width across both panes (see below). */
  #topbar,
  #scroll-indicator,
  #types-panel { right: var(--me-w); }

  /* The left tab bar fills the area to the left of the Me pane. The Me
     pane's own tab bar (#me-tabbar) fills the pane width, so the two
     together look like one continuous bottom bar. */
  #tabbar {
    right: var(--me-w);
    grid-template-columns: repeat(3, 1fr);
  }
  /* Hide the Me button in the LEFT tab bar — Me lives in the right
     pane's tab bar now. (switchTab also refuses to select it on wide
     screens, guarding restored state / deep links.) */
  #tabbar .tab-btn[data-tab="me"] { display: none; }

  /* The shared controls bar spans the FULL width across both panes and
     sits just above the tab-bar band. Raised above the Me pane so it
     paints across it. */
  #bottom-controls {
    position: fixed;
    bottom: calc(var(--tab-h) + var(--safe-bottom));
    left: 0; right: 0;
    z-index: 26;
  }

  /* The left content column stops short of the Me pane. */
  body { padding-right: var(--me-w); }

  /* On wide screens the LEFT pane owns its own scrollbar so it renders at
     the RIGHT EDGE OF THE LEFT PANE (just left of the Me pane) rather than
     at the far edge of the viewport (to the right of the Me pane, which is
     where the window scrollbar would otherwise sit). To do that we turn
     #content into a fixed scroll container occupying the left region —
     between the top chrome (top bar + optional scroll indicator) and the
     bottom controls/tab-bar band — and scroll IT instead of the window.

     snapshotScroll()/render() redirect scroll save+restore to this element
     when isWide() (window.scrollY is meaningless once the body no longer
     scrolls). The scroll indicator and connector SVGs already use
     viewport-relative rects / scrollHeight, so they keep working unchanged.

     The body's own vertical padding (top/bottom) is zeroed here since the
     fixed #content is positioned explicitly; padding-right (the Me-pane
     gutter) is preserved by the rule above. */
  body {
    /* The wide layout uses its own fixed-position scheme (below), not the
       narrow app-shell flex column, so revert body to a normal block box.
       position:static undoes the narrow layout's position:fixed pin. */
    position: static;
    display: block;
    height: auto;
    inset: auto;
    padding-top: 0;
    padding-bottom: 0;
    overflow: hidden;            /* window itself no longer scrolls */
  }
  #content {
    position: fixed;
    top: calc(var(--top-h) + var(--safe-top));
    left: 0;
    right: var(--me-w);          /* stop at the Me pane's left edge */
    bottom: calc(var(--bot-h) + var(--tab-h) + var(--safe-bottom));
    overflow-y: auto;
    overflow-x: hidden;
    /* min-height was for window-flow; in a fixed scroller it would force a
       phantom scroll, so drop it back to auto here. */
    min-height: 0;
  }
  /* When the scroll indicator is showing it occupies a strip just below the
     top bar; push the scroll container down so content doesn't hide under
     it (mirrors the narrow-screen body.has-indicator padding-top bump). */
  body.has-indicator #content {
    top: calc(var(--top-h) + var(--safe-top) + var(--ind-h));
  }

  /* The permanently-affixed Me pane. It stops at the top of the shared
     controls bar so that bar shows through full-width beneath it; the
     pane's own tab bar lives in the band below the controls bar. */
  #me-pane {
    position: fixed;
    top: 0; right: 0; bottom: 0;
    width: var(--me-w);
    display: flex;
    flex-direction: column;
    background: var(--bg);
    border-left: 1px solid var(--line);
    z-index: 24;
  }

  /* Top header for the Me pane: a "My Schedule" title plus the Copy/Paste
     sync buttons. A fixed-height flow child at the top of the pane (the
     resizer is absolutely positioned and the tab bar is pinned to the
     bottom, so this is the first thing in the flex flow). Mirrors the
     left #topbar height/treatment so the two tops line up. */
  #me-pane-header {
    flex: 0 0 auto;
    height: calc(var(--top-h) + var(--safe-top));
    padding-top: var(--safe-top);
    padding-left: 14px; padding-right: 8px;
    display: flex; align-items: center; gap: 8px;
    background: rgb(246,246,244);
    border-bottom: 1px solid var(--line);
  }
  #me-pane-header .me-pane-title {
    flex: 0 0 auto;
    font-size: calc(16px * var(--fs)); font-weight: 600; letter-spacing: .01em;
  }
  /* "Last sync" text — pushed to the right, sitting just left of the
     Copy/Paste buttons. Truncates instead of wrapping if the pane is
     dragged narrow. */
  #me-pane-header .me-pane-sync {
    flex: 1 1 auto;
    text-align: right;
    font-size: calc(11px * var(--fs)); color: var(--muted);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    min-width: 0;
  }
  #me-pane-header .me-pane-extra {
    flex: 0 0 auto;
    display: flex; align-items: center; gap: 4px;
  }

  /* The Me pane's own sticky scroll indicator — "what time am I looking
     at", mirroring the left column's #scroll-indicator. It's a flow child
     between the header and the scrolling content, so it stays pinned
     while #me-content scrolls beneath it. Hidden until there are headers
     to track (body.has-me-indicator, set by updateScrollIndicator). */
  #me-scroll-indicator {
    flex: 0 0 auto;
    height: var(--ind-h);
    display: none;
    align-items: center; justify-content: flex-start;
    padding: 0 16px;
    background: var(--surface-2);
    border-bottom: 1px solid var(--line);
    font-size: calc(13px * var(--fs)); font-weight: 600;
    color: var(--text);
    -webkit-user-select: none; user-select: none;
  }
  #me-scroll-indicator .sep { color: var(--muted); margin: 0 8px; }
  body.has-me-indicator #me-scroll-indicator { display: flex; }

  /* Draggable divider on the pane's left edge. Sits just outside the
     pane content; pointer events resize --me-w live (see initMeResize). */
  #me-resizer {
    position: absolute;
    top: 0; bottom: 0; left: calc(var(--me-grip) / -2);
    width: var(--me-grip);
    cursor: col-resize;
    z-index: 27;
    touch-action: none;
  }
  #me-resizer::before {
    content: "";
    position: absolute; top: 0; bottom: 0; left: 50%;
    width: 1px; transform: translateX(-.5px);
    background: var(--line);
  }
  #me-resizer:hover::before,
  body.me-resizing #me-resizer::before {
    background: var(--accent-faint);
    width: 2px;
  }
  body.me-resizing { cursor: col-resize; -webkit-user-select: none; user-select: none; }

  /* The Me pane scrolls independently of the left column. position
     relative so the connector SVG (#me-connectors, position:absolute,
     inset:0) anchors to it exactly as it does inside #content. The header
     above provides the top chrome (incl. safe-area), so only a small
     top pad here; the bottom padding clears the shared controls bar AND
     the tab-bar band so the last item isn't hidden behind them. */
  #me-content {
    position: relative;
    flex: 1 1 auto;
    overflow-y: auto;
    padding: 8px 12px calc(var(--bot-h) + var(--tab-h) + var(--safe-bottom) + 16px);
  }

  /* The Me pane's own bottom tab bar — one permanently-active Me button,
     styled and sized identically to #tabbar so the two read as a single
     continuous bar. */
  #me-tabbar {
    position: absolute;
    left: 0; right: 0; bottom: 0;
    height: calc(var(--tab-h) + var(--safe-bottom));
    padding-bottom: var(--safe-bottom);
    background: var(--surface);
    border-top: 1px solid var(--line);
    display: grid; grid-template-columns: 1fr;
    z-index: 25;
  }
}
@media (prefers-color-scheme: dark) and __WIDE_QUERY__ {
  #me-pane-header { background: rgb(17,17,17); }
}
/* On narrow screens neither the Me pane nor its resizer/tab bar
   participate in layout — Me is a normal bottom tab there. */
@media __NARROW_QUERY__ {
  #me-pane { display: none !important; }
}
</style>
</head>
<body>

<header id="topbar">
  <button id="back-btn" hidden>‹&nbsp;Back</button>
  <h1 id="page-title">Sessions</h1>
  <div id="topbar-extra"></div>
</header>
<div id="scroll-indicator" aria-hidden="true"></div>

<main id="content"></main>

<footer id="bottom-controls">
  <label><span id="show-me-label">Show me:</span><input type="checkbox" id="show-past"> Concluded</label>
  <button id="date-range-toggle" class="dr-toggle" type="button">
    <span id="dr-label">Days</span><span id="dr-arrow">▾</span>
  </button>
  <button id="types-toggle" class="types-toggle" type="button">
    <span>Types</span><span id="types-toggle-arrow">▾</span>
  </button>
</footer>

<div id="types-panel"></div>

<nav id="tabbar">
  <button class="tab-btn" data-tab="sessions"><span class="glyph">▦</span><span>Sessions</span></button>
  <button class="tab-btn" data-tab="talks"><span class="glyph">▤</span><span>Talks</span></button>
  <button class="tab-btn" data-tab="search"><span class="glyph">⌕</span><span>Search</span></button>
  <button class="tab-btn" data-tab="me"><span class="glyph">★</span><span>Me</span></button>
</nav>

<!-- Permanently-affixed right-hand pane, only visible on wide screens
     (see the wide-layout media query / WIDE_QUERY). On narrow screens it is
     display:none and Me remains a normal bottom tab. -->
<aside id="me-pane" aria-label="My Schedule">
  <div id="me-resizer" role="separator" aria-orientation="vertical"
       aria-label="Resize My Schedule pane" title="Drag to resize"></div>
  <div id="me-pane-header">
    <span class="me-pane-title">My Schedule</span>
    <span id="me-pane-sync" class="me-pane-sync"></span>
    <span class="me-pane-extra" id="me-pane-extra"></span>
  </div>
  <div id="me-scroll-indicator" aria-hidden="true"></div>
  <div id="me-content"></div>
  <nav id="me-tabbar">
    <button class="tab-btn active" data-tab="me"><span class="glyph">★</span><span>Me</span></button>
  </nav>
</aside>

<script>
__DECODER_BLOCK__const DATA = __DATA_INIT__;

/* =============================================================== */
/* state                                                            */
/* =============================================================== */

const STORAGE_KEY = "conference.state.v1";

// Text-size multiplier bounds and slider step. The Text size slider in the
// Me page's Settings section sets state.fontScale within these bounds;
// applyFontScale turns the value into the --fs CSS variable. Kept here (above
// loadState) because the load-time validator clamps to this range at startup.
const FS_MIN = 0.5;
const FS_MAX = 2.0;
const FS_STEP = 0.1;

// Reserved key in state.notes for the page-level "general conference notes"
// (not tied to any session or talk). It uses characters that can't appear in a
// real session/talk id, so it never collides with item notes and is ignored by
// the session-id-stripping migration.
const CONFERENCE_NOTES_KEY = "__conference__";

/* Latest end time across every session and talk (ms epoch), or null if the
   data carries no end timestamps. Computed once at load — the program is
   fixed, so this never changes during a session. */
const CONFERENCE_END_MS = (() => {
  let max = null;
  for (const x of [...DATA.sessions, ...DATA.talks]) {
    if (!x.end_ts) continue;
    const t = new Date(x.end_ts).getTime();
    if (!isNaN(t) && (max == null || t > max)) max = t;
  }
  return max;
})();

/* True once the last event of the conference has ended. Used to pick a
   sensible default for the "Concluded" filter: after the conference is over,
   a brand-new session (no saved state) has nothing upcoming to show, so we
   default Concluded ON instead of greeting the user with an empty list. A
   returning user's explicitly saved choice always wins — this only seeds the
   default for fresh state. */
function conferenceIsOver() {
  return CONFERENCE_END_MS != null && Date.now() > CONFERENCE_END_MS;
}

function defaultState() {
  return {
    schedule: [],
    // Per-id schedule audit log: { id: { op: "add"|"del", ts: <ms> } }.
    // Source of truth for sync merges — `schedule` above is just the
    // derived "currently scheduled" subset for fast lookups. On every
    // toggle we update BOTH (toggle writes a new {op, ts} entry); on
    // every import we merge per id (latest ts wins) and rebuild
    // `schedule` from the merged log. Lets us propagate deletions
    // across devices, which the old union-only paste couldn't do.
    scheduleLog: {},
    // The "Show concluded" toggle is a single global control shared across
    // every tab. When off it hides both past and withdrawn items (neither can
    // still be attended); when on, everything shows. The state key stays
    // `showPast` for backward-compatible persistence/sync. Likewise the Days
    // filter (selectedDates) and Types filter (hiddenTypes) below are global —
    // set them once and they stick as you move between tabs.
    // Default: OFF while the conference is upcoming/running; ON once the whole
    // conference is over (otherwise a fresh visit would show an empty list).
    showPast: conferenceIsOver(),
    activeTab: "sessions",
    tabStacks: {
      sessions: [{ view: "list", scrollY: 0 }],
      talks:    [{ view: "list", scrollY: 0 }],
      search:   [{ view: "list", scrollY: 0 }],
      me:       [{ view: "list", scrollY: 0 }],
    },
    searchQuery: "",
    hiddenTypes: [],          // color tokens to hide globally
    typesPanelOpen: false,
    lastSyncAt: null,         // epoch ms of last successful Paste (import)
    selectedDates: [],        // 'YYYY-MM-DD' filter; [] = show all days
    notes: {},                // {itemId: "free text"} — keyed by session OR talk id
    // Sessions list: ids of sessions currently EXPANDED inline (their talks +
    // full detail shown directly under the bubble, in place of navigating to a
    // separate Session detail view). Local-only UI state — deliberately NOT
    // part of the sync code (see buildSyncPayload), since it's a transient view
    // preference, not schedule data. Multiple may be open at once.
    expandedSessions: [],
    // Width of the wide-screen "Me" pane, in CSS pixels. null = use the
    // default (one third of the viewport). Set by dragging the divider;
    // persisted locally and included in the sync code so the layout
    // preference travels with the schedule. Ignored entirely on narrow
    // screens (the pane is a normal bottom tab there).
    meWidth: null,
    // Global text-size multiplier for readability. 1 = default; set by the
    // Text size slider in the Me page's Settings section. Applied by writing
    // --fs on the root, which every font-size reads via calc(px * var(--fs)),
    // so it scales TEXT ONLY (see applyFontScale). Local-only — deliberately
    // NOT in the sync payload, since comfortable text size is per-device (a
    // phone and a desktop monitor want different sizes), not schedule data.
    fontScale: 1,
  };
}

function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return defaultState();
    const s = { ...defaultState(), ...JSON.parse(raw) };
    // Migration: `showPast` used to be a per-tab object
    // ({sessions, talks, search, me}); it's now a single global boolean.
    // Collapse any legacy object form to true if ANY tab had it on.
    if (s.showPast && typeof s.showPast === "object") {
      s.showPast = Object.values(s.showPast).some(Boolean);
    } else {
      s.showPast = !!s.showPast;
    }
    // Migration: session-level notes were removed — only per-talk notes
    // are kept. Strip any keys that correspond to session ids so they
    // can't surface in older sync codes either.
    if (s.notes && typeof s.notes === "object") {
      const sessionIds = new Set(DATA.sessions.map(x => x.id));
      for (const key of Object.keys(s.notes)) {
        if (sessionIds.has(key)) delete s.notes[key];
      }
    }
    // Migration: legacy `dateRange: {start, end}` filter was replaced
    // by `selectedDates: [...]`. We can't expand a range to a set here
    // because ALL_DATES isn't built yet — but a single-day range (the
    // common case from the old day-chip presets) translates cleanly,
    // so preserve those. Anything else resets to "all days".
    if (!Array.isArray(s.selectedDates)) {
      const dr = s.dateRange;
      if (dr && typeof dr === "object"
          && dr.start && dr.end && dr.start === dr.end) {
        s.selectedDates = [dr.start];
      } else {
        s.selectedDates = [];
      }
    }
    delete s.dateRange;
    // Migration: synthesize `scheduleLog` for users whose state predates
    // it. Every currently-scheduled id gets a baseline "add" entry; any
    // subsequent add or delete (on any device) will outrank this on
    // sync because it'll carry a real timestamp. We use lastSyncAt as
    // the baseline if known so that if the OTHER device added a NEW
    // item AFTER the last sync, that addition still wins.
    if (!s.scheduleLog || typeof s.scheduleLog !== "object") {
      const base = (typeof s.lastSyncAt === "number") ? s.lastSyncAt : 0;
      s.scheduleLog = {};
      for (const id of (Array.isArray(s.schedule) ? s.schedule : [])) {
        s.scheduleLog[id] = { op: "add", ts: base };
      }
    }
    if (!Array.isArray(s.expandedSessions)) s.expandedSessions = [];
    // Validate the text-size multiplier: must be a finite number within the
    // supported range; anything else falls back to 1 (default size).
    if (typeof s.fontScale !== "number" || !isFinite(s.fontScale)) {
      s.fontScale = 1;
    } else {
      s.fontScale = Math.max(FS_MIN, Math.min(FS_MAX, s.fontScale));
    }
    return s;
  } catch (_) { return defaultState(); }
}
function saveState() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); }
  catch (_) {}
}

let state = loadState();

/* indexes */
const sessionMap = Object.fromEntries(DATA.sessions.map(s => [s.id, s]));
const talkMap    = Object.fromEntries(DATA.talks.map(t => [t.id, t]));

// An item's location, falling back to its parent session's location when the
// item is a talk whose own location is UNSPECIFIED. The two empty cases are
// deliberately distinct:
//   * location missing / null  -> unspecified: infer from the session.
//   * location === ""          -> an explicit blank the processor chose to
//                                 assign; honored as "no location", NOT
//                                 inferred from the session.
// Any non-empty session/talk location is returned as-is.
function effectiveLocation(item) {
  if (item.location != null) return item.location;   // "" stays "" (explicit)
  if (item.session_id) {
    const s = sessionMap[item.session_id];
    if (s && s.location) return s.location;
  }
  return "";
}

// The COMPACT location for space-constrained bubbles (lists, schedule, search).
// Prefers the optional `short_location` the processor may supply; otherwise
// falls back to the full effectiveLocation, so conferences that don't provide
// short_location are unaffected. Detail views keep using effectiveLocation (the
// full string).
function effectiveShortLocation(item) {
  if (item.short_location) return item.short_location;       // own compact form
  if (item.location != null) return item.location;          // own full (or explicit "")
  if (item.session_id) {                                    // talk inherits from session
    const s = sessionMap[item.session_id];
    if (s) return s.short_location || s.location || "";
  }
  return "";
}

// Heal state saved before the empty-session guard existed: drop any persisted
// expansion of a session that isn't actually expandable (no resolvable talks,
// or no longer present in the data). Such ids render nothing yet would surface
// a Collapse All control for nothing. Done here — not in loadState — because
// isExpandableSession needs the maps above, which don't exist when loadState
// runs. Save only if something was actually pruned.
if (Array.isArray(state.expandedSessions) && state.expandedSessions.length) {
  const kept = state.expandedSessions.filter(isExpandableSession);
  if (kept.length !== state.expandedSessions.length) {
    state.expandedSessions = kept;
    saveState();
  }
}

const scheduledIds = () => new Set(state.schedule);

/* type color -> human label. */
/* Type-color -> human label, broken out per tab because 'orange' means
   different things in Sessions ("Other": Plenary/Poster/Short Course/
   Postdeadline/etc.) vs Talks ("Plenary & Tutorial"). The base map is
   used for the union (Search) view as a fallback. */
/* Type registries now come FROM THE DATA. The processor bakes a list of
   {id, label} per tab into DATA.session_types / DATA.talk_types, where `id`
   is the color token the app filters and groups on. We derive the label maps
   and the canonical orderings from those lists, falling back to the built-in
   default values if a data file predates the registries. */
const _DEFAULT_SESSION_TYPES = [
  { id: "blue",    label: "Applications & Technology" },
  { id: "violet",  label: "Fundamental Science" },
  { id: "emerald", label: "Science & Innovations" },
  { id: "amber",   label: "Symposia" },
  { id: "orange",  label: "Other Sessions" },
];
const _DEFAULT_TALK_TYPES = [
  { id: "orange", label: "Plenary & Tutorial" },
  { id: "indigo", label: "Invited" },
  { id: "rose",   label: "Postdeadline" },
  { id: "teal",   label: "Poster" },
  { id: "slate",  label: "Short Course" },
  { id: "pink",   label: "Contributed" },
];
const _SESSION_TYPES = (Array.isArray(DATA.session_types) && DATA.session_types.length)
  ? DATA.session_types : _DEFAULT_SESSION_TYPES;
const _TALK_TYPES = (Array.isArray(DATA.talk_types) && DATA.talk_types.length)
  ? DATA.talk_types : _DEFAULT_TALK_TYPES;

/* ── Dynamic category colors ───────────────────────────────────────────
   The processor ships the actual RGB for each color token inside the
   type registries: every {id, label} entry may also carry
   {fg, bg_light, bg_dark}. We synthesize the matching CSS at runtime and
   append it to <head>, AFTER the static :root block above, so:
     • new tokens the static CSS never defined (e.g. "sky") get real
       colors instead of the gray fallback, and
     • any token the processor re-colors overrides the baked-in default.
   Entries without RGB are skipped and keep whatever the static CSS gave
   them (or the gray fallback), so older data files still render fine. */
(function injectTypeColors() {
  const entries = [...(_SESSION_TYPES || []), ...(_TALK_TYPES || [])];
  const seen = new Set();
  const rootLines = [];
  const darkLines = [];
  const classLines = [];
  for (const t of entries) {
    if (!t || !t.id || seen.has(t.id)) continue;
    if (!t.fg && !t.bg_light && !t.bg_dark) continue;   // no RGB → leave as-is
    seen.add(t.id);
    const id = t.id;
    if (t.fg)       rootLines.push(`--c-${id}-fg: ${t.fg};`);
    if (t.bg_light) rootLines.push(`--c-${id}-bg: ${t.bg_light};`);
    if (t.bg_dark)  darkLines.push(`--c-${id}-bg: ${t.bg_dark};`);
    classLines.push(
      `.bubble.clr-${id}{background:var(--c-${id}-bg);border-left-color:var(--c-${id}-fg);}`);
    classLines.push(
      `.detail-head.clr-${id}{background:var(--c-${id}-bg);border-left-color:var(--c-${id}-fg);}`);
  }
  if (!rootLines.length && !darkLines.length && !classLines.length) return;
  const css =
    `:root{${rootLines.join("")}}` +
    (darkLines.length
      ? `@media (prefers-color-scheme: dark){:root{${darkLines.join("")}}}`
      : "") +
    classLines.join("");
  const styleEl = document.createElement("style");
  styleEl.id = "dynamic-type-colors";
  styleEl.textContent = css;
  document.head.appendChild(styleEl);
})();

const TYPE_LABELS_SESSION = Object.fromEntries(_SESSION_TYPES.map(t => [t.id, t.label]));
const TYPE_LABELS_TALK    = Object.fromEntries(_TALK_TYPES.map(t => [t.id, t.label]));
const TYPE_LABELS = {
  ...TYPE_LABELS_SESSION,
  ...TYPE_LABELS_TALK,
};
/* Union-view label for any id that means different things in Sessions vs
   Talks: show both, separated by " / ". */
for (const id in TYPE_LABELS_TALK) {
  if (TYPE_LABELS_SESSION[id] && TYPE_LABELS_SESSION[id] !== TYPE_LABELS_TALK[id]) {
    TYPE_LABELS[id] = TYPE_LABELS_SESSION[id] + " / " + TYPE_LABELS_TALK[id];
  }
}
function labelForType(color, tab) {
  if (tab === "sessions") return TYPE_LABELS_SESSION[color] || color;
  if (tab === "talks")    return TYPE_LABELS_TALK[color]    || color;
  return TYPE_LABELS[color] || color;
}

/* Canonical orderings of types as they appear in the Types panel. */
const SESSION_TYPE_ORDER = _SESSION_TYPES.map(t => t.id);
const TALK_TYPE_ORDER    = _TALK_TYPES.map(t => t.id);

/* Counts of each color, computed once. */
const SESSION_TYPE_COUNTS = (() => {
  const c = {}; for (const x of DATA.sessions) { const k = x.color || "neutral"; c[k] = (c[k]||0)+1; } return c;
})();
const TALK_TYPE_COUNTS = (() => {
  const c = {}; for (const x of DATA.talks) { const k = x.color || "neutral"; c[k] = (c[k]||0)+1; } return c;
})();
function typesForTab(tab) {
  if (tab === "sessions") {
    return SESSION_TYPE_ORDER
      .filter(c => SESSION_TYPE_COUNTS[c] > 0)
      .map(c => ({ color: c, count: SESSION_TYPE_COUNTS[c] }));
  }
  if (tab === "talks") {
    return TALK_TYPE_ORDER
      .filter(c => TALK_TYPE_COUNTS[c] > 0)
      .map(c => ({ color: c, count: TALK_TYPE_COUNTS[c] }));
  }
  // Search / Me: union, with sessions order first.
  const merged = {};
  for (const k in SESSION_TYPE_COUNTS) merged[k] = (merged[k]||0) + SESSION_TYPE_COUNTS[k];
  for (const k in TALK_TYPE_COUNTS)    merged[k] = (merged[k]||0) + TALK_TYPE_COUNTS[k];
  const order = [...SESSION_TYPE_ORDER, ...TALK_TYPE_ORDER.filter(c => !SESSION_TYPE_ORDER.includes(c))];
  return order.filter(c => merged[c] > 0).map(c => ({ color: c, count: merged[c] }));
}

function isTypeHidden(color) {
  return state.hiddenTypes.includes(color || "neutral");
}

/* =============================================================== */
/* time helpers                                                     */
/* =============================================================== */

/* The app's notion of "now". Normally this is the real wall clock, but a
   search of the form  NOW YYYY-MM-DD HH:MM  pins it to a fixed instant so
   time-based behavior (the "Now" group, past/upcoming filtering, the
   "today" header) can be tested even though the conference is over. The
   override lives only in memory (a module-local variable), so it lasts
   until the page is reloaded and is never persisted. See applyNowOverride
   / handleNowSearch. */
let _nowOverride = null;   // number (ms) when pinned, else null

function nowMs() {
  return _nowOverride != null ? _nowOverride : Date.now();
}

function tsToDate(ts) { return ts ? new Date(ts) : null; }

function isPast(item) {
  const d = tsToDate(item.end_ts);
  return d && d.getTime() < nowMs();
}

/* Withdrawn — the talk was pulled from the program. Like past items, these
   can no longer be attended, so the global "Show concluded" toggle hides them
   by default and reveals them (alongside past items) when on. Sessions have no
   withdrawn flag, so this is effectively talks-only. */
function isWithdrawn(item) {
  return !!item.withdrawn;
}

/* Display title for a talk/session. Withdrawn talks get a "(WITHDRAWN)"
   prefix unless the word "withdrawn" already appears in the title (so we
   don't double up). Empty-titled withdrawn talks (the program often blanks
   out a pulled talk's title) just show "(WITHDRAWN)" on its own. */
function displayTitle(item) {
  const raw = (item && item.title) || "";
  if (isWithdrawn(item) && !/withdrawn/i.test(raw)) {
    return raw ? `(WITHDRAWN) ${raw}` : "(WITHDRAWN)";
  }
  return raw;
}

/* "In progress" — start time has passed AND end time hasn't. Items
   matching this go into a dedicated "Now" pseudo-group at the top of
   any time-grouped list, even when Show past is OFF (since they're not
   strictly past yet — they're happening right now). */
function isInProgress(item) {
  const s = tsToDate(item.start_ts);
  const e = tsToDate(item.end_ts);
  if (!s || !e) return false;
  const now = nowMs();
  return s.getTime() <= now && now < e.getTime();
}

const _DAY = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"];
const _MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

function dateLabel(d) {
  return `${_DAY[d.getDay()]} · ${_MON[d.getMonth()]} ${d.getDate()}`;
}
function timeLabel(d) {
  let h = d.getHours();
  const m = d.getMinutes();
  const ap = h >= 12 ? "PM" : "AM";
  h = h % 12 || 12;
  return `${h}:${m.toString().padStart(2,"0")} ${ap}`;
}
function timeRange(item) {
  const s = tsToDate(item.start_ts), e = tsToDate(item.end_ts);
  if (s && e) return `${timeLabel(s)} – ${timeLabel(e)}`;
  if (s) return timeLabel(s);
  return "";
}
function cmpTs(a, b) {
  a = a || ""; b = b || "";
  return a < b ? -1 : a > b ? 1 : 0;
}

/* Testing hook: a search query of the form  NOW YYYY-MM-DD HH:MM  pins the
   app's notion of "now" (nowMs) to that instant until the page is reloaded,
   so the conference's time-based behavior — the "Now" group, past/upcoming
   filtering, the "today" header — can be exercised even though the real
   conference is over. The timestamp is interpreted in LOCAL time (same zone
   the start/end timestamps render in), matches "now" semantics elsewhere.

   parseNowOverride returns the ms value for a matching query, or null if the
   query isn't a NOW directive. A malformed date after the NOW keyword still
   counts as "a NOW directive" (returns NaN) so the caller can report it
   rather than fall through to an ordinary search. */
function parseNowOverride(raw) {
  const m = /^NOW\s+(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})$/.exec(
    (raw || "").trim());
  if (!m) return null;
  const [, y, mo, da, hh, mm] = m;
  // Local-time construction (month is 0-based). Invalid components (e.g.
  // 13 for month, 25 for hour) yield an out-of-range / NaN Date, which we
  // surface as NaN so the caller can flag it.
  const d = new Date(+y, +mo - 1, +da, +hh, +mm, 0, 0);
  // Reject values that rolled over (e.g. month 13 -> next year) so a
  // nonsense date doesn't silently "work".
  if (d.getFullYear() !== +y || d.getMonth() !== +mo - 1 ||
      d.getDate() !== +da || d.getHours() !== +hh ||
      d.getMinutes() !== +mm) {
    return NaN;
  }
  return d.getTime();
}

/* =============================================================== */
/* schedule toggle                                                  */
/* =============================================================== */

function toggleScheduled(id) {
  const s = new Set(state.schedule);
  let op;
  if (s.has(id)) { s.delete(id); op = "del"; }
  else           { s.add(id);    op = "add"; }
  state.schedule = [...s];
  // Record the action in the audit log so a later sync can propagate
  // it (especially deletions) instead of just unioning ids.
  state.scheduleLog = state.scheduleLog || {};
  state.scheduleLog[id] = { op, ts: Date.now() };
  saveState();

  // Targeted DOM update instead of a full render(). With thousands of talks
  // scheduled, render() is dominated by tearing down and rebuilding bubbles
  // that don't conceptually change on a single toggle.
  //
  // Left pane (#content): the bubble for this talk/session is already
  //   correct except for its .added (and possibly .partial) class and the
  //   schedule-btn aria-label. Flip in place. Session-list connectors don't
  //   depend on the schedule set, so no redraw needed.
  //
  // Right pane (#me-content): the Me pane shows ONLY scheduled items, so a
  //   bubble must be inserted or removed somewhere. Bigger but localized
  //   work, and renderMePane now benefits from the IntersectionObserver-
  //   driven fit (only visible bylines refit).
  const isAdded = op === "add";

  // Flip .added on every left-pane bubble for this item id (a session and a
  // talk are never the same id, so this is exactly the right set of nodes).
  document.querySelectorAll(
    `#content .bubble[data-bubble-id="${CSS.escape(id)}"]`
  ).forEach(b => {
    b.classList.toggle("added", isAdded);
    if (isAdded) b.classList.remove("partial");   // can't be both
    const btn = b.querySelector(".schedule-btn");
    if (btn) btn.setAttribute("aria-label",
      isAdded ? "Remove from schedule" : "Add to schedule");
  });

  // If a detail view (talk or session) for this id is currently showing, its
  // header has a .dh-add button whose visual state must also flip. The
  // detail head is identified by data-detail-id (annotated in
  // renderTalkDetail / buildSessionHead).
  const detailHead = document.querySelector(
    `#content .detail-head[data-detail-id="${CSS.escape(id)}"]`
  );
  if (detailHead) {
    const dh = detailHead.querySelector(".dh-add");
    if (dh) {
      dh.classList.toggle("added", isAdded);
      dh.setAttribute("aria-label",
        isAdded ? "Remove from schedule" : "Add to schedule");
    }
  }

  // Session bubbles show a .partial indicator when ANY of their talks are
  // scheduled but the session itself is not. A talk toggle can flip the
  // parent session's partial state; a session toggle can clear or restore
  // its own partial state. Invalidate the cache and recompute for affected
  // sessions.
  invalidatePartial();
  const t = talkMap[id];
  const affectedSessionIds = [];
  if (t && t.session_id) affectedSessionIds.push(t.session_id);
  if (sessionMap[id]) affectedSessionIds.push(id);
  for (const sid of affectedSessionIds) {
    document.querySelectorAll(
      `#content .bubble[data-bubble-id="${CSS.escape(sid)}"]`
    ).forEach(sb => {
      const sessionAdded = state.schedule.includes(sid);
      const partial = !sessionAdded && partialSessionIds().has(sid);
      sb.classList.toggle("partial", partial);
    });
  }

  // Re-render the Me pane (right pane on wide layouts).
  if (isWide()) {
    renderMePane();
  } else if (state.activeTab === "me") {
    // Narrow + the active tab IS Me: the left list shows the schedule and
    // must reflect insertion/removal. Fall back to a full render here.
    render();
    return;
  }

  refreshTypesPanel();
}

/* Sessions list: expand/collapse a session inline (show its full detail +
   talks directly under the bubble instead of navigating to a separate
   Session detail view). Multiple sessions may be open at once; the open set
   persists locally (state.expandedSessions). */
function isSessionExpanded(id) {
  return (state.expandedSessions || []).includes(id);
}

/* A session is only inline-expandable if it actually has talks to show.
   Mirrors buildSessionExpansion's emptiness test EXACTLY (resolvable talks,
   not just a non-empty talk_ids — an id might not resolve via talkMap), so the
   two never disagree about what "empty" means. An empty session's bubble
   already shows its full detail, so there's nothing to expand into. */
function isExpandableSession(id) {
  const s = sessionMap[id];
  if (!s) return false;
  return (s.talk_ids || []).some(tid => talkMap[tid]);
}

function toggleSessionExpanded(id) {
  const opening = !isSessionExpanded(id);
  // Opening an EMPTY session would record it in expandedSessions while
  // rendering nothing (buildSessionExpansion returns null for it) — a silent
  // "open" that left no visible tree yet surfaced the Collapse All control for
  // a no-op. So refuse to OPEN an empty session. Collapsing is never blocked: a
  // previously-persisted empty id (from before this guard) must still be
  // clearable.
  if (opening && !isExpandableSession(id)) return;

  // Capture the CURRENT scroll position before we re-render. render()
  // restores state.tabStacks[...].scrollY after rebuilding the DOM; without
  // this snapshot it would restore whatever the debounced scroll handler last
  // persisted (up to 350 ms stale), which on a narrow/mobile layout — where
  // the whole window scrolls a very tall list — yanks the view wildly to an
  // old position. Snapshotting here pins the restore target to where the user
  // actually is, so expanding/collapsing keeps the tapped session in place.
  snapshotScroll();
  const set = new Set(state.expandedSessions || []);
  if (set.has(id)) set.delete(id); else set.add(id);
  state.expandedSessions = [...set];
  saveState();
  // Drop the inline-expansion connector overlay SYNCHRONOUSLY, before the
  // re-render reflows the bubbles. render() rebuilds the DOM immediately but
  // only redraws connectors in a post-layout rAF; without this, a collapsing
  // session's bubble shrinks first while the old (longer) spine is still
  // painted, so its tail flashes in the empty space for a frame. Removing it
  // up front means the line is simply gone until it's redrawn at the correct
  // length.
  const svg = document.querySelector("#session-list-connectors");
  if (svg) svg.remove();
  render();
}

/* =============================================================== */
/* DOM helpers                                                      */
/* =============================================================== */

const $  = sel => document.querySelector(sel);
const $$ = sel => document.querySelectorAll(sel);
function esc(s) {
  return (s == null ? "" : String(s))
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}
function el(tag, props = {}, children = []) {
  const e = document.createElement(tag);
  for (const k in props) {
    if (k === "class") e.className = props[k];
    else if (k === "html") e.innerHTML = props[k];
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), props[k]);
    else if (k === "data") for (const d in props.data) e.dataset[d] = props.data[d];
    else e.setAttribute(k, props[k]);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return e;
}

/* =============================================================== */
/* bubble factory                                                   */
/* =============================================================== */

/* Memoised "partially-added" session set: sessions that are NOT in the
   schedule themselves but at least one of their talks is. Recomputed on
   every render() (cleared by invalidatePartial). */
let _partialSessionIds = null;
function invalidatePartial() { _partialSessionIds = null; }
function partialSessionIds() {
  if (_partialSessionIds) return _partialSessionIds;
  const scheduled = new Set(state.schedule);
  const out = new Set();
  for (const s of DATA.sessions) {
    if (scheduled.has(s.id)) continue;
    const tids = s.talk_ids || [];
    for (const tid of tids) {
      if (scheduled.has(tid)) { out.add(s.id); break; }
    }
  }
  _partialSessionIds = out;
  return out;
}

function makeBubble(item, opts = {}) {
  const isTalk = !!item.session_id;
  const added  = state.schedule.includes(item.id);
  // Sessions only: signal "interest" when one or more of the session's
  // talks is in the schedule but the session itself isn't.
  const partial = !isTalk && !added && partialSessionIds().has(item.id);

  // In the Sessions list, tapping a session expands it inline (full detail +
  // talks under the bubble) rather than navigating to a separate Session
  // detail view. `expandable` is set only by the Sessions list; talks are
  // never expandable.
  const expandable = !!opts.expandable && !isTalk;
  const expanded   = expandable && isSessionExpanded(item.id);

  const cls = [
    "bubble",
    `clr-${item.color || "neutral"}`,
    added    ? "added"    : "",
    partial  ? "partial"  : "",
    expanded ? "expanded" : "",
  ].filter(Boolean).join(" ");

  const wrap = el("article", {
    class: cls,
    "data-kind": isTalk ? "talk" : "session",
    "data-bubble-id":  item.id,
    "data-session-id": isTalk ? (item.session_id || "") : "",
    onclick: (e) => {
      // A long-press (handled below) navigates to the standalone Session
      // detail and sets this flag so the press doesn't ALSO toggle the
      // inline expansion when the finger/mouse lifts.
      if (wrap._lpFired) { wrap._lpFired = false; return; }
      // A near-miss around the +/- button toggles the schedule instead of
      // opening the detail (see inAddZone), so it's hard to overshoot the
      // small circle and land on the bubble. The +/- circle itself has its
      // own handler (stopPropagation), so this only catches the surround.
      if (inAddZone(e.clientX, e.clientY)) { toggleScheduled(item.id); return; }
      if (expandable) { toggleSessionExpanded(item.id); return; }
      navigate(isTalk ? `talk:${item.id}` : `session:${item.id}`);
    },
  });

  // The clickable surround for the +/- button: its box grown outward by the
  // gap between the button's right edge and the bubble's right edge. A click
  // (or the start of a press) anywhere in here counts as a +/- tap, not a
  // bubble tap — measured live so it tracks the --sp text-size scaling.
  function inAddZone(x, y) {
    const btn = wrap.querySelector(".schedule-btn");
    if (!btn) return false;
    const br = btn.getBoundingClientRect();
    if (!br.width) return false;
    const wr = wrap.getBoundingClientRect();
    const pad = Math.max(0, wr.right - br.right);
    return x >= br.left - pad && x <= br.right + pad
        && y >= br.top  - pad && y <= br.bottom + pad;
  }

  // Press-and-hold opens a standalone detail view. For an expandable session
  // bubble a quick tap expands it inline while a hold opens Session detail;
  // for a talk both tap AND hold open Talk detail (the hold path mainly
  // matters on touch, where it lets us suppress the OS callout/selection and
  // navigate cleanly, matching how sessions behave). We use pointer events so
  // it works for touch and mouse; a small move threshold means a scroll/drag
  // doesn't count as a press. The "press to expand, hold for detail"
  // affordance is shown once in the scroll indicator (see
  // updateScrollIndicatorIn), not per-bubble.
  if (expandable || isTalk) {
    const detailView = isTalk ? `talk:${item.id}` : `session:${item.id}`;
    let lpTimer = null, sx = 0, sy = 0;
    const LP_MS = 500, MOVE_TOL = 10;

    const cancel = () => {
      if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; }
    };
    wrap.addEventListener("pointerdown", (e) => {
      // Ignore the +/- schedule button (it has its own handler) and the
      // forgiving zone around it — a press there should toggle the schedule on
      // release (via onclick), not start a long-press into the detail view.
      if (e.target.closest(".schedule-btn")) return;
      if (inAddZone(e.clientX, e.clientY)) return;
      sx = e.clientX; sy = e.clientY;
      wrap._lpFired = false;
      cancel();
      lpTimer = setTimeout(() => {
        lpTimer = null;
        wrap._lpFired = true;        // suppress the click that follows
        navigate(detailView);
      }, LP_MS);
    });
    wrap.addEventListener("pointermove", (e) => {
      if (lpTimer && (Math.abs(e.clientX - sx) > MOVE_TOL ||
                      Math.abs(e.clientY - sy) > MOVE_TOL)) cancel();
    });
    wrap.addEventListener("pointerup", cancel);
    wrap.addEventListener("pointercancel", cancel);
    wrap.addEventListener("pointerleave", cancel);
    // Long-press on touch often raises the OS context menu; suppress it so
    // the press reads as our gesture instead.
    wrap.addEventListener("contextmenu", (e) => {
      if (wrap._lpFired) e.preventDefault();
    });
  }

  // Title line: just the title. The chip (location, or in session-detail the
  // talk's start time) is rendered as a standalone element pinned to the RIGHT
  // edge of the bubble, just left of the +/- button (see .bubble-loc), so the
  // title and subtitle both read cleanly from the left.
  //
  // The chip is the talk's location, or in session-detail (opts.inlineTime)
  // the talk's start time (room implied, which also lets the between-bubble
  // time-headers be dropped). The session/talk number is no longer shown on
  // bubbles — see below.
  let chipText;
  if (opts.inlineTime) {
    const sd = tsToDate(item.start_ts);
    chipText = sd ? timeLabel(sd) : "";
  } else {
    chipText = effectiveShortLocation(item);
  }
  // The session/talk number (item.id) is intentionally NOT shown on bubbles:
  // it only adds clutter in the list/schedule/search/expansion views. The id
  // is shown as the page title in the top bar when a detail view is open
  // (see pageTitleFor), which is where it's actually useful. The id remains
  // on the element as the data-bubble-id attribute for connector lookups and
  // click handling.
  const titleHTML = esc(displayTitle(item));
  wrap.appendChild(el("div", {
    class: "bubble-title",
    html: titleHTML,
  }));

  // Subtitle line: the time/location at the START, in the same font as the
  // author byline (no chip pill), separated from the author by a " · " dot —
  // the same separator used between names and affiliations inside the byline —
  // when both are present. subtitleFor returns "&nbsp;" as a height-reserving
  // placeholder when there's no real byline (e.g. a session with no presider),
  // so that case is treated as "no byline" and gets no trailing dot. An
  // expanded session bubble looks EXACTLY like its collapsed self — title +
  // presider subtitle. Expanding only reveals the talk bubbles beneath it; the
  // bubble's own appearance is unchanged. (The full session detail — date,
  // type, etc. — lives in the standalone Session detail view, reached by
  // press-and-hold.)
  const subBody = subtitleFor(item);
  const hasByline = !!subBody && subBody.trim() !== "" && subBody !== "&nbsp;";
  const locPrefix = chipText
    ? `<span class="bubble-loc">${esc(chipText)}</span>` +
      (hasByline ? " · " : "")   // same plain separator the byline uses internally
    : "";
  const subHTML = locPrefix + (subBody || "");
  if (subHTML) {
    const subEl = el("div", { class: "bubble-sub", html: subHTML });
    wrap.appendChild(subEl);

    // On a narrow (mobile) viewport a long author name can push the short
    // affiliation off the end of this single clipped line. Once the bubble is
    // laid out, progressively abbreviate author first names to initials so the
    // affiliation survives. Deferred to the next frame so scrollWidth/
    // clientWidth are measurable (the bubble must be in the live DOM). The
    // time/location prefix is stashed on the element so a later re-fit (e.g.
    // after a font-size change) rebuilds the full line before re-measuring.
    //
    // We don't fit eagerly here — we hand the bubble to an IntersectionObserver
    // that fits on demand when the bubble enters the viewport (see
    // observeBylineForFit). At conference-scale data the difference is huge:
    // only the ~15 visible talks fit on creation instead of all ~1900.
    // Both talks (author byline) and sessions (presider byline) are fit lazily.
    // We stash the chip prefix and the active search context on the element so
    // the deferred fit can rebuild the line — with abbreviation AND, in search
    // views, the <span class="author-hit"> highlights — even though the global
    // _bylineSearchCtx is cleared by the time the observer fires.
    subEl._chip = locPrefix;
    subEl._searchCtx = _bylineSearchCtx;
    requestAnimationFrame(() => observeBylineForFit(subEl));
  }

  // The +/- mark is drawn with CSS pseudo-element bars (see .schedule-btn),
  // not a text glyph — text "+"/"−" don't sit on the same optical center in
  // most fonts, so they look off-center in the circle. Bars center exactly
  // and scale cleanly. The button carries no text; the bubble's `.added`
  // class drives whether it shows a plus or a minus.
  wrap.appendChild(el("button", {
    class: "schedule-btn",
    "aria-label": added ? "Remove from schedule" : "Add to schedule",
    onclick: (e) => { e.stopPropagation(); toggleScheduled(item.id); },
  }));

  return wrap;
}

// Returns HTML (may contain <b> tags around the speaker).
function subtitleFor(item) {
  if (item.session_id) {
    return talkByline(item);
  }
  return presiderByline(item, _bylineSearchCtx, false);
}

/* The session subtitle: presider(s) + short affiliation. Search-aware
   (highlights via `ctx`) and optionally abbreviating each presider's given
   names to initials (when `abbrev`) — the Sessions/Search analogue of the talk
   byline's fit, run lazily by fitByline so it stays cheap at conference scale.
     * multiple presiders -> "Name1 · ShortAff1, Name2 · ShortAff2"
     * single presider     -> "Name · ShortAff"
   Hits are detected PER PRESIDER on the FULL name/affiliation, so abbreviating
   the displayed name never changes which presider highlights. */
function presiderByline(item, ctx, abbrev) {
  const affilSearch = ctx && ctx.mode === "affil" ? _norm(ctx.query) : null;
  const qName       = ctx ? ctx.qName : null;

  const presiderRaw = item.presider || "";
  // De-duped short string (used for the single-presider bullet) and the
  // per-presider short list (used for the multi-presider form).
  const affDeduped = item.presider_aff_short || item.presider_aff || "";
  const affList = Array.isArray(item.presider_affs_short)
    ? item.presider_affs_short : [];

  if (!presiderRaw) {
    // No presider: keep the same non-breaking space so bubbles keep height.
    return "&nbsp;";
  }

  const names = presiderRaw.split(/;| and /i)
    .map(n => n.trim()).filter(Boolean);

  const nameHTML = (nm, aff) => {
    const affHit = affilSearch != null && aff &&
      _norm(aff) === affilSearch;
    const hit = affHit ||
      (qName && personNamesMatch(qName, parsePersonName(nm)));
    const d = esc(abbrev ? abbrevGivenNames(nm) : nm);
    return hit ? `<span class="author-hit">${d}</span>` : d;
  };

  let s;
  if (names.length > 1) {
    s = names.map((nm, idx) => {
      const a = (affList[idx] || "").trim();
      return a ? `${nameHTML(nm, a)} · ${esc(a)}` : nameHTML(nm, a);
    }).join(", ");
  } else {
    const a = (affDeduped || (affList[0] || "")).trim();
    s = nameHTML(names[0] || presiderRaw, a);
    if (a) s += ` · ${esc(a)}`;
  }
  return s || "&nbsp;";
}

/* Build the ordered author list for a talk with each author's short
   affiliation(s) resolved. Source of truth is the structured `authors` list
   ([{name, insts:[n,...]}]); each author's `insts` are EXPLICIT institution
   numbers. `inst_shorts` holds the canonical short name per institution,
   parallel to `institutions` ([{n, name}]). We map each institution's explicit
   number to its short name so an author's inst number resolves correctly even
   if numbering isn't 1..N. Returns [] when there are no structured authors. */
function talkAuthorsWithAffs(item) {
  const authors = Array.isArray(item.authors) ? item.authors : [];
  if (!authors.length) return [];
  // explicit institution number -> short name
  const idxToShort = {};
  const shorts = Array.isArray(item.inst_shorts) ? item.inst_shorts : [];
  (Array.isArray(item.institutions) ? item.institutions : [])
    .forEach((inst, i) => {
      const num = (inst && inst.n != null) ? String(inst.n) : String(i + 1);
      if (shorts[i]) idxToShort[num] = shorts[i];
    });
  return authors.map(a => {
    const name = (a.name || "").trim();
    const idxs = Array.isArray(a.insts) ? a.insts.map(String) : [];
    // De-duplicate shorts while preserving order (an author can list two
    // departments at the same institution → same short name twice).
    const affs = [];
    idxs.forEach(ix => {
      const sh = idxToShort[ix];
      if (sh && !affs.includes(sh)) affs.push(sh);
    });
    return { name, affs };
  });
}

function talkByline(item) {
  return searchTalkByline(item, _bylineSearchCtx, 0);
}

/* The author byline, search-aware (highlights via `ctx`) and optionally
   abbreviating each SHOWN author's given names to initials (when `abbrev`), so
   a long byline still fits its single clipped line in SEARCH results too — the
   lazy fitter (fitByline) passes the stashed search ctx so highlights survive.
   With no name/affiliation hit it delegates to legacyTalkByline (which has its
   own finer 0..3 abbreviation levels for the non-search list views). */
function searchTalkByline(item, ctx, level = 0) {
  const affilSearch = ctx && ctx.mode === "affil" ? _norm(ctx.query) : null;
  const qName       = ctx ? ctx.qName : null;   // co-author / name probe
  if (!affilSearch && !qName) return legacyTalkByline(item, level);

  const authors = talkAuthorsWithAffs(item);
  if (!authors.length) return legacyTalkByline(item, level);

  // An author is a "hit" (the reason this talk matched) if their affiliation
  // matches an affiliation search OR their name matches a co-author search.
  const isHit = (a) =>
       (affilSearch && a.affs.some(s => _norm(s) === affilSearch))
    || (qName && personNamesMatch(qName, parsePersonName(a.name)));

  const speakerNorm = _norm(item.speaker || "");
  const isSpk = (a) => speakerNorm && _norm(a.name) === speakerNorm;
  const lastI = authors.length - 1;
  const spkI = authors.findIndex(isSpk);

  // Base shown set = the normal trio: first, last, and the speaker if it's a
  // middle author.
  const shownIdx = new Set();
  shownIdx.add(0);
  if (lastI > 0) shownIdx.add(lastI);
  if (spkI > 0 && spkI < lastI) shownIdx.add(spkI);

  // Reveal hidden middle authors that explain the match, so the highlight has
  // somewhere to land. For an AFFILIATION search, if no shown author carries
  // it, reveal the LAST middle author that does (several may share it). For a
  // CO-AUTHOR search, reveal every matching author (there's normally one).
  if (affilSearch) {
    const shownHasAff = [...shownIdx].some(i => isHit(authors[i]));
    if (!shownHasAff) {
      let lastMid = -1;
      for (let i = 1; i < lastI; i++) if (isHit(authors[i])) lastMid = i;
      if (lastMid >= 0) shownIdx.add(lastMid);
    }
  }
  if (qName) {
    authors.forEach((a, i) => { if (isHit(a)) shownIdx.add(i); });
  }

  // A matched author is brightened (author-hit). The speaker keeps its
  // underline. An author can be both (underlined + brightened).
  const order = [...shownIdx].sort((x, y) => x - y);
  // Progressive abbreviation by DISPLAY position, mirroring legacyTalkByline so
  // the last shown author is shortened last: level 1 = first shown only; level
  // 2 = all but the last shown; level 3 = all shown.
  const shorten = (k) =>
    level >= 3 || (level === 2 && k < order.length - 1) ||
    (level === 1 && k === 0);
  const fmtName = (a, k) => {
    const cls = [];
    if (isSpk(a)) cls.push("speaker");     // underline
    if (isHit(a)) cls.push("author-hit");  // white
    // Hits are detected on the FULL name above; only the displayed text is
    // shortened, so abbreviation never changes which authors highlight.
    const d = esc(shorten(k) ? abbrevGivenNames(a.name) : a.name);
    return cls.length ? `<span class="${cls.join(" ")}">${d}</span>` : d;
  };

  let html = "";
  let prev = -1;
  order.forEach((i, k) => {
    if (k === 0) { if (i > 0) html += "…"; }
    else if (i > prev + 1) html += "…";   // hidden author(s) between
    else html += ", ";                    // adjacent shown authors
    html += fmtName(authors[i], k);
    prev = i;
  });
  if (prev < lastI) html += "…";

  // Keep the normal trailing affiliation so the line still reads like the
  // usual byline. Skip the leading ", " when no names rendered at all
  // (e.g. an aff-only record), so the line doesn't begin with a stray comma.
  const tail = item.speaker_aff || item.last_aff || "";
  if (tail) html += html ? ` · ${esc(tail)}` : esc(tail);
  return html;
}

/* Abbreviate a DISPLAY name's first given name to an initial, preserving the
   rest of the name (middle names + surname) verbatim. "David Burghoff" ->
   "D. Burghoff"; "Jean-Pierre Dupont" -> "J.-Pierre Dupont"; "Burghoff, David"
   -> "Burghoff, D." (the inverted comma form is kept inverted). Returns the
   name unchanged when there's nothing safe to shorten (single token, leading
   token already a bare initial, or an unparseable form). This is display-only
   and independent of parsePersonName (which folds/strips for search). */
// Particles that travel WITH the surname rather than being given names.
// Lowercased; matched case-insensitively. Compound forms ("van der", "de la",
// "van den") are detected by walking consecutive particle tokens, so this
// flat set is enough. Single-letter "y"/"i"/"e" are connectors used in
// Iberian-style compound surnames (e.g. "García y Robles"). "bin"/"ibn"/"ben"
// cover Arabic/Hebrew patronymic forms.
const NOBILIARY_PARTICLES = new Set([
  "von","van","de","der","den","del","della","di","da","dos","das","du",
  "le","la","les","el","al","bin","ibn","ben","zu","auf","am","im","vom",
  "ten","ter","te","op","ait","aït","abu","abd","saint","st","st.",
  "y","i","e",
]);

// Initial one whitespace-free given-name token, preserving any hyphenated
// segments (e.g. "Jean-Pierre" -> "J.-P.", "Marie-Louise" -> "M.-L."). Returns
// null when the token can't be initialed (already an initial like "J.",
// contains no letters, or is a nobiliary particle that should stay lowercase
// even in the given-name region — e.g. "del" in Spanish "María del Carmen").
function _initialToken(token) {
  const t = (token || "").trim();
  if (!t) return null;
  // Already an initial like "J", "J.", "J.-P.", "J.P." — leave it.
  if (/^[A-Za-zÀ-ÿ]\.?(?:[-.][A-Za-zÀ-ÿ]\.?)*$/.test(t)) return null;
  // Particle word stuck inside the given-name region (Spanish "María del
  // Carmen", French "Marie de la Tour") — leave lowercase, don't initial.
  if (NOBILIARY_PARTICLES.has(t.toLowerCase())) return null;
  // Initial each hyphen-separated segment.
  const segs = t.split("-");
  const out = [];
  for (const seg of segs) {
    const m = seg.match(/^([A-Za-zÀ-ÿ])[A-Za-zÀ-ÿ'’]*$/);
    if (!m) return null;
    out.push(m[1] + ".");
  }
  return out.join("-");
}

// Find where the surname cluster starts in a tokenized name (array of tokens).
// The surname cluster = rightmost token + any immediately-preceding nobiliary
// particles. Returns the index of the first token belonging to the surname.
// For a single-token name returns 0 (the whole thing is the surname). A name
// that is entirely particles + surname (e.g. "van Beethoven") also returns 0,
// leaving nothing to abbreviate.
function _surnameStart(tokens) {
  if (tokens.length <= 1) return 0;
  let i = tokens.length - 1; // surname proper
  while (i - 1 >= 0 && NOBILIARY_PARTICLES.has(tokens[i - 1].toLowerCase())) {
    i--;
  }
  return i;
}

/* Initial every given-name token in a full name, preserving the surname
   cluster intact (including any leading nobiliary particles like "van"/
   "de"/"von"). Hyphenated given names get every segment initialed
   ("Jean-Pierre" -> "J.-P."). Also handles "Last, First Middle" comma form.
   Examples:
     "Maria Rossi"                -> "M. Rossi"
     "John Quincy Adams"          -> "J. Q. Adams"
     "Ludwig van Beethoven"       -> "L. van Beethoven"
     "Jean-Pierre Dupont"         -> "J.-P. Dupont"
     "Sam De Vries"               -> "S. De Vries"
     "Lucia María Núñez"          -> "L. M. Núñez"
*/
function abbrevGivenNames(name) {
  const raw = (name || "").trim();
  if (!raw) return raw;

  const ci = raw.indexOf(",");
  if (ci >= 0) {
    // "Last, First Middle" — initial each whitespace-separated token after
    // the comma.
    const afterComma = raw.slice(ci + 1);
    const ws = (afterComma.match(/^\s*/) || [""])[0];
    const given = afterComma.slice(ws.length);
    const toks = given.split(/\s+/).filter(Boolean);
    if (!toks.length) return raw;
    const out = [];
    for (const t of toks) {
      const ab = _initialToken(t);
      out.push(ab == null ? t : ab);
    }
    return raw.slice(0, ci + 1) + ws + out.join(" ");
  }

  const tokens = raw.split(/\s+/).filter(Boolean);
  if (tokens.length <= 1) return raw; // mononym — nothing to abbreviate

  const surStart = _surnameStart(tokens);
  if (surStart <= 0) return raw; // entirely particles + surname

  const givenToks = tokens.slice(0, surStart);
  const tail = tokens.slice(surStart);
  const out = [];
  let changed = false;
  for (const t of givenToks) {
    const ab = _initialToken(t);
    if (ab == null) { out.push(t); }
    else { out.push(ab); changed = true; }
  }
  if (!changed) return raw;
  return out.concat(tail).join(" ");
}

/* The normal byline for a talk: first…[speaker if middle]…last with a single
   trailing affiliation (the speaker's, else the last author's).

   `abbrevLevel` progressively shortens each author's GIVEN NAMES — first
   names AND middle names, including hyphenated segments — to make the line
   fit on mobile (see fitByline), so a long affiliation isn't the first thing
   clipped by the .bubble-sub ellipsis. Nobiliary particles like "van"/"de"/
   "von" stay with the surname and are never initialed.
     0 — full names (default)
     1 — first author's given names -> initials ("John Q. Adams" -> "J. Q. Adams")
     2 — ALSO a middle speaker's given names -> initials (when one is shown)
     3 — ALSO last author's given names -> initials
   Abbreviation is in display order (first, then a middle speaker, then last),
   independent of which author the speaker is — a first author who is also the
   speaker is still the first name shortened. The speaker's underline styling is
   preserved either way; only the visible name text is shortened. */
function legacyTalkByline(item, abbrevLevel = 0) {
  const first   = item.first_author || "";
  const last    = item.last_author  || "";
  const speaker = item.speaker      || "";
  const pos     = (item.speaker_pos == null ? -1 : item.speaker_pos);

  // A talk with one author (or where first and last resolve to the same
  // person) must show that name ONCE, not "Name…Name". Treat `last` as a
  // distinct trailing author only when it's actually a different person from
  // `first`; otherwise drop it. This makes the byline robust regardless of
  // whether the processor blanked last_author for single-author talks.
  const sameFirstLast = !last || _norm(last) === _norm(first);
  const effLast = sameFirstLast ? "" : last;

  const isFirstSpeaker = pos === 0 || _norm(speaker) === _norm(first);
  const isLastSpeaker  = effLast && (_norm(speaker) === _norm(effLast));
  const speakerIsMiddle = speaker && !isFirstSpeaker && !isLastSpeaker && effLast;

  // Apply given-name abbreviation per the level, in display order:
  //   level 1 — first author's given names
  //   level 2 — middle speaker's given names (only when speakerIsMiddle)
  //   level 3 — last author's given names
  // The speaker's underline is preserved regardless; only the visible name
  // text is shortened (so even when the first author IS the speaker, it's
  // still the first name shortened first, per the intended order). When there
  // is no middle speaker, level 2 is a no-op and the last author shortens at
  // level 3 — the fit loop simply advances through the empty level.
  const firstDisp  = (abbrevLevel >= 1) ? abbrevGivenNames(first) : first;
  const middleDisp = (abbrevLevel >= 2 && speakerIsMiddle)
    ? abbrevGivenNames(speaker) : speaker;
  const lastDisp   = (abbrevLevel >= 3 && effLast)
    ? abbrevGivenNames(effLast) : effLast;

  const fmt = (name, isSpeaker) => isSpeaker
    ? `<span class="speaker">${esc(name)}</span>`
    : esc(name);

  const parts = [];
  if (first) parts.push(fmt(firstDisp, isFirstSpeaker));
  if (speakerIsMiddle) parts.push(fmt(middleDisp, true));
  if (effLast) parts.push(fmt(lastDisp, isLastSpeaker));
  let html = parts.join("…");

  const aff = item.speaker_aff || item.last_aff || "";
  if (aff) html += html ? ` · ${esc(aff)}` : esc(aff);
  return html;
}

/* After a talk bubble's subtitle is in the live DOM, shrink author first names
   to initials if the byline overflows its single clipped line — so the short
   affiliation at the end survives instead of being the first thing the
   .bubble-sub ellipsis eats. Tries level 1 (first author -> initial), then
   level 2 (a middle speaker, when one is shown), then level 3 (last author),
   re-measuring after each and stopping as soon as it fits. `chipPrefix` is the
   leading location/time chip HTML, re-emitted on each re-render so it isn't
   dropped.

   Idempotent: it first restores the FULL byline (level 0) before measuring, so
   calling it again after a width/zoom change re-evaluates from scratch — a
   byline abbreviated when text was large goes back to full names if the text
   later shrinks enough to fit. No-op when the line already fits, when the
   bubble is a session (handled by its own presider byline), or when a search
   byline is active (those deliberately show names to explain the match). */
function fitByline(subEl, item, chipPrefix = "") {
  if (!subEl || !item) return;
  // The search context the bubble was rendered under (stashed at makeBubble
  // time), so the rebuild keeps any highlights even though the global ctx is
  // gone by now.
  const ctx = subEl._searchCtx || null;
  const isTalk = !!item.session_id;
  const searchActive = !!(ctx && (ctx.qName || ctx.mode === "affil"));
  // How to (re)build the line at abbreviation level `lvl`:
  //   talk, no search -> legacyTalkByline's 0..3 progressive levels
  //   talk, search    -> search byline, level>=1 abbreviates the shown authors
  //   session         -> presider byline, level>=1 abbreviates the presiders
  const build = (lvl) => {
    if (isTalk) {
      return searchActive ? searchTalkByline(item, ctx, lvl)   // progressive 0..3
                          : legacyTalkByline(item, lvl);
    }
    return presiderByline(item, ctx, lvl >= 1);
  };
  const maxLvl = isTalk ? 3 : 1;   // talks shorten progressively; sessions 0..1
  const overflowing = () => subEl.scrollWidth > subEl.clientWidth + 1;
  // Start from full names so repeat calls don't compound an earlier abbrev.
  subEl.innerHTML = chipPrefix + build(0);
  if (!overflowing()) return;
  for (let lvl = 1; lvl <= maxLvl; lvl++) {
    subEl.innerHTML = chipPrefix + build(lvl);
    if (!overflowing()) return;
  }
  // Still overflowing at the deepest level: leave it — CSS ellipsis handles
  // the rest.
}

/* Lazy byline fitting via IntersectionObserver.
   At conference-scale data (~1900 talks), running fitByline once per bubble
   on every font/pane change is an O(N²) layout thrash — each forced
   scrollWidth read after a synchronous innerHTML write flushes layout for
   the whole document. The fix: only fit talks the user can actually see.

   Mechanics:
     - One observer per scroll-container (#content, #me-content). Each new
       talk subtitle registers with the observer for its scroll container
       when it's added to the DOM.
     - The observer's currently-intersecting set lives in _bylineVisible.
     - A generation counter (_bylineFitGen) is bumped whenever something
       that would change a fit (font scale, pane width, window resize)
       happens. Visible subs are refit immediately; off-screen ones get
       refit when they scroll into view (the observer callback checks
       _fitGen and refits if stale).
   At the original ECIO scale (~160 talks) the old eager refit was already
   cheap; this only matters at conference scales like CLEO (~1900 talks). */
let _bylineFitGen = 0;
const _bylineObservers = new WeakMap();   // root element -> IntersectionObserver
const _bylineVisible   = new WeakMap();   // root element -> Set<subEl>

function _bylineScrollRoot(subEl) {
  let n = subEl.parentElement;
  while (n) {
    if (n.id === "content" || n.id === "me-content") return n;
    n = n.parentElement;
  }
  return null;
}

function _fitIfStale(subEl) {
  if (subEl._fitGen === _bylineFitGen) return;
  const id = subEl.parentElement
          && subEl.parentElement.getAttribute("data-bubble-id");
  const item = id ? (talkMap[id] || sessionMap[id]) : null;
  if (item) fitByline(subEl, item, subEl._chip || "");
  subEl._fitGen = _bylineFitGen;
}

function _getBylineObserver(root) {
  let obs = _bylineObservers.get(root);
  if (obs) return obs;
  let visSet = _bylineVisible.get(root);
  if (!visSet) { visSet = new Set(); _bylineVisible.set(root, visSet); }
  obs = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const subEl = e.target;
      if (e.isIntersecting) { visSet.add(subEl); _fitIfStale(subEl); }
      else                  { visSet.delete(subEl); }
    }
  }, { root, rootMargin: "200px 0px" });   // pre-fit a bit beyond the viewport
  _bylineObservers.set(root, obs);
  return obs;
}

/* Is the bubble currently within (or near) its scroll container's viewport?
   Mirrors the observer's rootMargin so the synchronous and lazy paths agree on
   what "visible" means. #content / #me-content are always scroll containers
   (overflow set), so the root's bounding rect IS the visible viewport — a long
   list scrolled inside it reports correctly here, not as "all visible". */
function _bylineNearViewport(subEl, root, margin) {
  const r = subEl.getBoundingClientRect();
  if (r.width === 0 && r.height === 0) return false;   // not laid out / hidden
  const rr = root.getBoundingClientRect();
  return r.bottom >= rr.top - margin && r.top <= rr.bottom + margin;
}

function observeBylineForFit(subEl) {
  const root = _bylineScrollRoot(subEl);
  if (!root) return;
  const obs = _getBylineObserver(root);   // also ensures _bylineVisible has a set
  // Fit on-screen bylines SYNCHRONOUSLY here (we run inside makeBubble's rAF,
  // before the browser paints), instead of waiting for the IntersectionObserver
  // — its first callback is delivered in a later task, AFTER one paint of the
  // full-length byline, which is the flash seen when a session expands and its
  // talk bubbles appear. Off-screen bylines are still left to the lazy observer.
  // _fitIfStale stamps _fitGen, so the observer's initial async callback for the
  // same element no-ops rather than fitting twice.
  if (_bylineNearViewport(subEl, root, 200)) {
    _bylineVisible.get(root).add(subEl);
    _fitIfStale(subEl);
  }
  obs.observe(subEl);
}

/* Tear down observers + visible sets before a container is cleared. Without
   this, observers keep strong references to their targets indefinitely
   (a subtle DOM leak across re-renders) and a re-observed root would still
   hold ghosts of the previous render. Optional `specificRoot` arg lets
   renderMePane drop just the Me-pane observer without disturbing the left. */
function disconnectBylineObservers(specificRoot) {
  const roots = specificRoot
    ? [specificRoot]
    : ["content", "me-content"].map(id => document.getElementById(id));
  for (const root of roots) {
    if (!root) continue;
    const obs = _bylineObservers.get(root);
    if (obs) { obs.disconnect(); _bylineObservers.delete(root); }
    _bylineVisible.delete(root);
  }
}

/* Re-run byline fitting on every VISIBLE talk bubble. Called after a text-
   size change or a pane resize: --fs reflows text via CSS but doesn't
   re-render bubbles, so an abbreviation chosen at the old zoom may no
   longer be right. Off-screen bubbles deferred until they scroll into
   view; the observer above handles those lazily. */
function refitAllBylines() {
  if (_bylineSearchCtx) return;
  _bylineFitGen++;
  requestAnimationFrame(() => {
    for (const id of ["content", "me-content"]) {
      const root = document.getElementById(id);
      if (!root) continue;
      const set = _bylineVisible.get(root);
      if (!set || set.size === 0) continue;
      for (const subEl of set) _fitIfStale(subEl);
    }
  });
}

function _norm(s) {
  return (s || "").replace(/\s+/g, " ").trim().toLowerCase();
}

/* Search context for the byline. While a search-results list is rendering,
   this holds what produced the results so each talk bubble's subtitle can
   reveal WHY it matched: a co-author hidden behind "…", or the author whose
   affiliation matched an affiliation search. Null in all non-search views. */
let _bylineSearchCtx = null;

/* =============================================================== */
/* list rendering                                                   */
/* =============================================================== */

function renderTimeGrouped(container, items, opts = {}) {
  const showConcluded = !!state.showPast;
  // When the toggle is off we hide BOTH past and withdrawn items (neither can
  // still be attended — the forward-looking default); when on, everything
  // shows. `alwaysAll` (session detail / session expansion) bypasses this
  // hiding entirely: a session is the unit of visibility there, so once a
  // session is shown it lists its full agenda — past and withdrawn talks
  // included — regardless of the Concluded toggle.
  let filtered = (opts.alwaysAll || showConcluded)
    ? items
    : items.filter(it => !isPast(it) && !isWithdrawn(it));
  if (!opts.ignoreTypes) {
    filtered = filtered.filter(it => !isTypeHidden(it.color));
  }
  // Day filter — global, applies on every list view but bypassed for
  // detail-view talks-of-a-session (which set alwaysAll).
  if (!opts.alwaysAll) {
    const sel = state.selectedDates || [];
    if (sel.length) {
      const selSet = new Set(sel);
      filtered = filtered.filter(it => {
        if (!it.start_ts) return true;     // undated rows pass through
        return selSet.has(it.start_ts.slice(0, 10));
      });
    }
  }

  if (filtered.length === 0) {
    container.appendChild(el("p", { class: "empty" },
      showConcluded ? "Nothing scheduled here." : "Nothing upcoming. Try showing Concluded."));
    return;
  }

  // Pull "in progress" items out so they render under a single sticky
  // "Now" group at the top of the list. We only do this for the normal
  // upcoming view — not for skipDateHeaders mode (detail-view session
  // listings) and not when Show concluded is on (in which case past items
  // are inline, in chronological order — pulling the live ones forward
  // would be jarring).
  let nowItems = [];
  if (!opts.skipDateHeaders && !showConcluded) {
    const live  = filtered.filter(it => isInProgress(it));
    const rest  = filtered.filter(it => !isInProgress(it));
    nowItems = live;
    filtered = rest;
  }

  // Within a single time bucket (or the conceptual "Now" bucket), reorder
  // so that:
  //   1) sessions move to the END of the bucket — keeps a session from
  //      visually appearing to belong to whatever talk sits above it when
  //      they happen to share a start time;
  //   2) any child talks present in the same bucket get pulled out of
  //      their chronological slot and placed immediately under their
  //      parent session, so the parent/child association reads clearly;
  //   3) among the sessions at the back of the bucket, sessions WITHOUT
  //      a visible connector (no scheduled child talk anywhere in the
  //      current view) come before sessions WITH one. The reasoning is
  //      that connector-bearing sessions visually expand downward into
  //      a spine + child block; putting them last keeps that block from
  //      pushing unrelated bubbles around.
  // Standalone talks (whose parent session isn't in the same bucket, or
  // which have no parent) keep their original relative order at the
  // front. The elbow connector code measures the rendered DOM, so it
  // picks up these positions automatically — no changes needed there.
  //
  // Connector detection: a session will get an elbow drawn for it iff
  // it's scheduled AND at least one of its scheduled child talks is
  // also present in the rendered view (any bucket, not just this one).
  // We precompute the set of "connector-bearing" session ids from the
  // full view input — see `connectorIds` below.
  const scheduledSet = new Set(state.schedule);
  const inViewIds    = new Set();
  for (const it of nowItems) inViewIds.add(it.id);
  for (const it of filtered) inViewIds.add(it.id);
  const connectorIds = new Set();
  // Walk every scheduled talk in the view and mark its parent session
  // as connector-bearing iff the parent itself is in the view + scheduled.
  const viewItems = nowItems.concat(filtered);
  for (const it of viewItems) {
    if (!it.session_id) continue;                   // sessions skipped
    if (!scheduledSet.has(it.id)) continue;         // unscheduled talks don't get added class
    const sid = it.session_id;
    if (!inViewIds.has(sid)) continue;              // parent not visible
    if (!scheduledSet.has(sid)) continue;           // parent not scheduled (no .added)
    connectorIds.add(sid);
  }

  // Final tiebreak among same-time items: order by location. `numeric` makes
  // room numbers sort naturally (… Room 50 before Room 101) rather than
  // lexically; the sort is stable so equal locations keep their prior order.
  // Talks inherit their session's location via effectiveLocation.
  const _locOf = (it) => effectiveLocation(it) || "";
  const _byLoc = (a, b) =>
    _locOf(a).localeCompare(_locOf(b), undefined, { numeric: true });

  const reorderBucket = (bucket) => {
    if (bucket.length < 2) return bucket;
    const sessionIdsHere = new Set();
    for (const it of bucket) {
      if (!it.session_id) sessionIdsHere.add(it.id);
    }
    // No sessions in this bucket (e.g. the Talks tab): just order by location.
    if (sessionIdsHere.size === 0) return [...bucket].sort(_byLoc);
    const childrenOf = {};   // sessionId -> [talks in bucket]
    for (const it of bucket) {
      if (it.session_id && sessionIdsHere.has(it.session_id)) {
        (childrenOf[it.session_id] = childrenOf[it.session_id] || []).push(it);
      }
    }
    const front = [];
    const sessionsNoConn = [];   // back, first half
    const sessionsConn   = [];   // back, second half
    for (const it of bucket) {
      if (!it.session_id) continue;                              // session — defer
      if (sessionIdsHere.has(it.session_id)) continue;           // child — defer
      front.push(it);                                            // standalone talk
    }
    for (const it of bucket) {
      if (it.session_id) continue;                               // not a session
      (connectorIds.has(it.id) ? sessionsConn : sessionsNoConn).push(it);
    }
    // Order each group by location (children follow their parent below, so they
    // move with it). Standalone talks at the front are ordered by location too.
    front.sort(_byLoc);
    sessionsNoConn.sort(_byLoc);
    sessionsConn.sort(_byLoc);
    const back = [];
    for (const s of sessionsNoConn) {
      back.push(s);
      const kids = childrenOf[s.id];
      if (kids) for (const k of kids) back.push(k);
    }
    for (const s of sessionsConn) {
      back.push(s);
      const kids = childrenOf[s.id];
      if (kids) for (const k of kids) back.push(k);
    }
    return front.concat(back);
  };

  // Apply the reorder to both "Now" (one logical bucket regardless of
  // individual start times) and the main list (bucketed by timeLabel).
  if (nowItems.length > 1) {
    nowItems = reorderBucket(nowItems);
  }
  if (filtered.length > 1) {
    // Walk `filtered` collecting runs of consecutive items sharing the
    // same time label, reorder each run, then concatenate. The input is
    // already sorted by start_ts, so equal-time items are already
    // adjacent — we just need to find the run boundaries.
    const out = [];
    let i = 0;
    while (i < filtered.length) {
      const di = tsToDate(filtered[i].start_ts);
      const tk = di ? timeLabel(di) : null;
      let j = i + 1;
      while (j < filtered.length) {
        const dj = tsToDate(filtered[j].start_ts);
        const tkj = dj ? timeLabel(dj) : null;
        if (tkj !== tk) break;
        j++;
      }
      const run = filtered.slice(i, j);
      out.push(...(run.length > 1 ? reorderBucket(run) : run));
      i = j;
    }
    filtered = out;
  }

  const frag = document.createDocumentFragment();
  let curDate = null, curTime = null;

  if (nowItems.length > 0) {
    // "Now" is a time-bucket WITHIN today — emit today's date header
    // first, then a "Now" time-header. This keeps the visual hierarchy
    // (date > time > items) intact instead of floating "Now" outside
    // any day.
    const todayLabel = dateLabel(new Date(nowMs()));
    frag.appendChild(el("h2", { class: "date-header" }, todayLabel));
    curDate = todayLabel;
    const th = el("h3", { class: "time-header" });
    th.appendChild(el("span", { class: "th-text" }, "Now"));
    frag.appendChild(th);
    curTime = "Now";
    for (const it of nowItems) {
      frag.appendChild(makeBubble(it, {
        inlineTime: !!opts.inlineTime,
        expandable: !!opts.expandable,
      }));
      // Inline expansion for an open session in the Now bucket — same as the
      // main loop below. Without this, tapping a "Now" session bubble fell
      // through to the non-expandable path and just opened Session detail.
      if (opts.expandable && !it.session_id && isSessionExpanded(it.id)) {
        const exp = buildSessionExpansion(it);
        if (exp) frag.appendChild(exp);
      }
    }
  }

  for (const it of filtered) {
    const d = tsToDate(it.start_ts);
    if (!d) continue;
    const dk = opts.skipDateHeaders ? null : dateLabel(d);
    const tk = timeLabel(d);
    if (dk && dk !== curDate) {
      frag.appendChild(el("h2", { class: "date-header" }, dk));
      curDate = dk; curTime = null;
    }
    // When inlineTime is on (session-detail), the start time lives inside
    // each bubble's location chip, so we suppress the between-bubble
    // time-headers entirely and let the bubbles abut.
    if (!opts.inlineTime && tk !== curTime) {
      const th = el("h3", { class: "time-header" });
      th.appendChild(el("span", { class: "th-text" }, tk));
      frag.appendChild(th);
      curTime = tk;
    }
    frag.appendChild(makeBubble(it, {
      inlineTime: !!opts.inlineTime,
      expandable: !!opts.expandable,
    }));
    // Sessions list inline expansion: when a session is open, render its
    // talk bubbles in a wrapper right beneath it. The session's own detail
    // shows inside the bubble itself; a talkless session adds nothing here.
    if (opts.expandable && !it.session_id && isSessionExpanded(it.id)) {
      const exp = buildSessionExpansion(it);
      if (exp) frag.appendChild(exp);
    }
  }
  container.appendChild(frag);
}

function renderSessionsList(c) {
  renderTimeGrouped(c, DATA.sessions, { expandable: true });
}
function renderTalksList(c) {
  renderTimeGrouped(c, DATA.talks);
}

/* =============================================================== */
/* detail views                                                     */
/* =============================================================== */

/* Build the Session detail header card (the "detail-head" section with
   title, date/time/location meta, tags, presider line, details, and
   the add/remove button). Extracted so both the standalone Session detail
   view and the inline expansion in the Sessions list render an identical
   header. */
/* Append the session's meta rows (date/time/location, tags, presider,
   details) to `container`. Shared by the standalone detail head and the
   in-bubble inline expansion so they stay identical. The session id is
   intentionally not rendered here — it's already the page title in the top
   bar for session-detail views (see pageTitleFor). */
function appendSessionMetaLines(s, container) {
  // Presider FIRST, so it sits on the line immediately after the title (in
  // both the standalone detail head and the in-bubble inline expansion). The
  // name(s) launch an initials-robust people search; the affiliation launches
  // a plain text search (always the short form when we have one). Each piece
  // is independently tappable.
  //   * multiple presiders -> "Name1 · ShortAff1, Name2 · ShortAff2"
  //   * single presider     -> "Name · ShortAff"  (the original bullet style)
  if (s.presider) {
    const meta = el("div", { class: "dh-meta presider-meta" });
    meta.appendChild(el("strong", {}, "Presider:"));
    meta.appendChild(document.createTextNode(" "));
    const names = s.presider.split(/;| and /i)
      .map(p => p.trim()).filter(Boolean);
    // Per-presider short affiliations, aligned to names (NOT de-duped).
    const affList = Array.isArray(s.presider_affs_short)
      ? s.presider_affs_short : [];

    const appendName = (nm) => {
      meta.appendChild(el("span", {
        class: "presider-name clickable",
        title: `Find sessions & talks by ${nm}`,
        onclick: (e) => { e.stopPropagation(); searchFor(nm, "name"); },
      }, nm));
    };
    const appendAff = (affText) => {
      meta.appendChild(el("span", {
        class: "presider-aff clickable",
        title: `Search for “${affText}”`,
        onclick: (e) => { e.stopPropagation(); searchFor(affText, "affil"); },
      }, affText));
    };

    if (names.length > 1) {
      names.forEach((nm, idx) => {
        appendName(nm);
        const affText = (affList[idx] || "").trim();
        if (affText) {
          meta.appendChild(document.createTextNode(" · "));
          appendAff(affText);
        }
        if (idx < names.length - 1) {
          meta.appendChild(document.createTextNode(", "));
        }
      });
    } else {
      appendName(names[0] || s.presider);
      const affDisplay = (s.presider_aff_short
        || (affList[0] || "")
        || s.presider_aff || "").trim();
      if (affDisplay) {
        meta.appendChild(document.createTextNode(" · "));
        appendAff(affDisplay);
      }
    }
    container.appendChild(meta);
  }

  const sd = tsToDate(s.start_ts);
  if (sd) container.appendChild(el("div", { class: "dh-meta" },
    `${dateLabel(sd)} · ${timeRange(s)}${s.location ? " · " + s.location : ""}`));

  // Tags line. Each tag is an ordered { key, value } pair (e.g.
  // { key: "Format", value: "FS Oral" }), rendered as "Key: Value" and
  // joined with " · ". The session id is NOT shown here — it's already
  // the page title in the top bar for session-detail views (see pageTitleFor).
  const tagText = Array.isArray(s.tags)
    ? s.tags
        .filter(t => t && t.value)
        .map(t => (t.key ? `${t.key}: ${t.value}` : `${t.value}`))
        .join(" · ")
    : "";
  if (tagText) {
    container.appendChild(el("div", { class: "dh-meta" }, tagText));
  }

  if (s.details)
    container.appendChild(el("div", { class: "dh-meta" }, s.details));
}

function buildSessionHead(s) {
  const added = state.schedule.includes(s.id);
  // data-detail-id lets toggleScheduled find this head's .dh-add button to
  // flip its .added class in lockstep with the schedule state, so a tap on
  // the dh-add button updates without a full re-render.
  const head = el("section", {
    class: `detail-head clr-${s.color}`,
    "data-detail-id": s.id,
  });
  // The session id is NOT shown in the head — it's already the page title
  // in the top bar for session-detail views (see pageTitleFor), so putting
  // it here again was pure duplication.
  head.appendChild(el("h2", { class: "dh-title" }, s.title || "(untitled)"));
  appendSessionMetaLines(s, head);
  head.appendChild(el("button", {
    class: `dh-add${added ? " added" : ""}`,
    "aria-label": added ? "Remove from schedule" : "Add to schedule",
    onclick: (e) => { e.stopPropagation(); toggleScheduled(s.id); },
  }));
  return head;
}

function renderSessionDetail(c, sid) {
  const s = sessionMap[sid];
  if (!s) { c.appendChild(el("p", { class: "empty" }, "Session not found.")); return; }

  c.appendChild(buildSessionHead(s));

  if (!s.talk_ids || s.talk_ids.length === 0) {
    c.appendChild(el("p", { class: "empty" }, "No talks listed for this session."));
    return;
  }

  // No "Talks" heading and no between-bubble time-headers — the header is
  // followed directly by the talk bubbles, each carrying its start time
  // inline in the chip (the room is implied by the session, so it's omitted).
  const talks = s.talk_ids.map(id => talkMap[id]).filter(Boolean)
                 .sort((a,b) => cmpTs(a.start_ts, b.start_ts));
  renderTimeGrouped(c, talks, { skipDateHeaders: true, alwaysAll: true, ignoreTypes: true, inlineTime: true });
}

/* Sessions list inline expansion: the talk bubbles rendered directly
   beneath an expanded session bubble. The session's extra detail (date,
   tags, presider, details) is shown INSIDE the session bubble itself
   (see makeBubble), so this block holds only the talks — no separate header
   card. Wrapped in a .session-expansion container scoped to one session so
   the indent CSS and the connector drawer can target just this group.

   Returns null when the session has no talks: the bubble already shows the
   full detail, so there's nothing to append — no empty box, no "No talks"
   placeholder, no wasted vertical space. */
function buildSessionExpansion(s) {
  const talks = (s.talk_ids || []).map(id => talkMap[id]).filter(Boolean)
                 .sort((a,b) => cmpTs(a.start_ts, b.start_ts));
  if (talks.length === 0) return null;
  const box = el("div", {
    class: `session-expansion clr-${s.color}`,
    "data-expansion-for": s.id,
  });
  for (const t of talks) {
    box.appendChild(makeBubble(t, { inlineTime: true }));
  }
  return box;
}

function renderTalkDetail(c, tid) {
  const t = talkMap[tid];
  if (!t) { c.appendChild(el("p", { class: "empty" }, "Talk not found.")); return; }
  const s = sessionMap[t.session_id];
  const added = state.schedule.includes(t.id);

  // data-detail-id lets toggleScheduled find this head's .dh-add button to
  // flip its .added class in lockstep with the schedule state, so a tap on
  // the dh-add button updates without a full re-render.
  const head = el("section", {
    class: `detail-head clr-${t.color}`,
    "data-detail-id": t.id,
  });
  // The talk id is NOT shown here — it's already the page title in the top
  // bar for talk-detail views (see pageTitleFor), so repeating it on a meta
  // row inside the detail head was pure duplication.
  head.appendChild(el("h2", { class: "dh-title" }, displayTitle(t) || "(untitled)"));
  const sd = tsToDate(t.start_ts);
  if (sd) {
    const loc = effectiveLocation(t);
    head.appendChild(el("div", { class: "dh-meta" },
      `${dateLabel(sd)} · ${timeRange(t)}${loc ? " · " + loc : ""}`));
  }
  if (t.status && t.status.toLowerCase() !== "sessioned")
    head.appendChild(el("div", { class: "dh-meta" }, `Status: ${t.status}`));
  head.appendChild(el("button", {
    class: `dh-add${added ? " added" : ""}`,
    "aria-label": added ? "Remove from schedule" : "Add to schedule",
    onclick: () => toggleScheduled(t.id),
  }));
  c.appendChild(head);

  // Authors (one line, with superscript affiliation numbers; speaker
  // bold). Each name is tappable: it launches an initials-robust search
  // for other talks that author co-authored (see searchFor(..,"name")).
  c.appendChild(el("div", { class: "section-title" }, "Authors"));
  const detailAuthors = Array.isArray(t.authors) ? t.authors : [];
  if (detailAuthors.length) {
    const speakerNorm = _norm(t.speaker || "");
    const line = el("p", { class: "author-line" });
    detailAuthors.forEach((a, idx) => {
      const name = (a.name || "").trim();
      // Superscript shows the author's EXPLICIT institution numbers, joined
      // exactly as they're stored (e.g. "1,2").
      const aff  = (Array.isArray(a.insts) ? a.insts : []).join(",");
      const isSpeaker = speakerNorm && _norm(name) === speakerNorm;
      const nameEl = el("span", {
        class: "author-name clickable" + (isSpeaker ? " speaker" : ""),
        title: `Find other talks by ${name}`,
        onclick: () => searchFor(name, "name"),
      }, name);
      line.appendChild(nameEl);
      if (aff) {
        line.appendChild(el("sup", { class: "aff" }, aff));
      }
      if (idx < detailAuthors.length - 1) {
        line.appendChild(document.createTextNode(", "));
      }
    });
    c.appendChild(line);
  } else if (Array.isArray(t.author_aliases) && t.author_aliases.length) {
    // No structured author list — fall back to the loose alias name forms so
    // each ("A. Descos") is still individually clickable for a co-author search.
    const line = el("p", { class: "author-line" });
    const names = t.author_aliases.map(p => p.trim()).filter(Boolean);
    names.forEach((nm, idx) => {
      const clean = nm.replace(/\*+$/, "").trim();
      line.appendChild(el("span", {
        class: "author-name clickable",
        title: `Find other talks by ${clean}`,
        onclick: () => searchFor(clean, "name"),
      }, clean));
      if (idx < names.length - 1) {
        line.appendChild(document.createTextNode(", "));
      }
    });
    c.appendChild(line);
  }

  // Institutions. Each row is clickable: tapping it launches a temporary
  // search for the institution's short form (see searchFor). Rows are
  // numbered by the EXPLICIT number `n` carried on each institution — not by
  // simple 1..N auto-numbering — so the number shown here always matches the
  // superscript that the author line points at, even if numbering is sparse.
  const detailInsts = Array.isArray(t.institutions) ? t.institutions : [];
  if (detailInsts.length) {
    c.appendChild(el("div", { class: "section-title" }, "Institutions"));
    const list = el("ol", { class: "inst-list" });
    const shorts = Array.isArray(t.inst_shorts) ? t.inst_shorts : [];
    detailInsts.forEach((inst, i) => {
      const num       = (inst && inst.n != null) ? String(inst.n) : String(i + 1);
      const longForm  = (inst.name || "").trim();
      const shortForm = (shorts[i] || "").trim();
      // A real <ol> handles numbering + alignment natively; `value` forces the
      // EXPLICIT number `n` (rather than auto 1..N), so the marker still
      // matches the superscript the author line points at even if numbering
      // is sparse or reordered.
      const li = el("li", {
        class: "inst-item" + (shortForm ? " clickable" : ""),
        value: num,
        title: shortForm ? `Search for “${shortForm}”` : "",
      });
      li.appendChild(el("span", { class: "inst-long" }, longForm));
      // Always show the short-form chip when one exists — even when it's
      // identical to the (full) affiliation — so every institution row carries
      // its clickable chip consistently.
      if (shortForm) {
        li.appendChild(el("span", { class: "inst-short" }, shortForm));
      }
      if (shortForm) {
        li.addEventListener("click", () => searchFor(shortForm, "affil"));
      }
      list.appendChild(li);
    });
    c.appendChild(list);
  }

  // Abstract (source has literal <sup>/<sub>/<i>/<b> tags — render those,
  // escape everything else for safety).
  if (t.abstract) {
    c.appendChild(el("div", { class: "section-title" }, "Abstract"));
    const safe = esc(t.abstract).replace(
      /&lt;(\/?(?:sup|sub|i|b|em|strong))&gt;/gi, "<$1>");
    c.appendChild(el("p", { class: "abstract-body", html: safe }));
  }

  // Session link
  if (s) {
    c.appendChild(el("div", { class: "section-title" }, "Session"));
    c.appendChild(makeBubble(s));
  }

  appendNotesBox(c, t.id);
}

/* =============================================================== */
/* search                                                           */
/* =============================================================== */

/* Diacritic-insensitive folding, shared by the typed Search tab and the
   click-to-search trips. Folds accented Latin to ASCII so "Gunter"
   matches "Günter", "Andre" matches "André", etc. NFD handles most of
   it; the map covers single-char extensions that don't decompose. */
const _FOLD_MAP = {
  "ß":"ss","ẞ":"SS","ø":"o","Ø":"O","æ":"ae","Æ":"AE",
  "œ":"oe","Œ":"OE","đ":"d","Đ":"D","ð":"d","Ð":"D",
  "ł":"l","Ł":"L","þ":"th","Þ":"Th",
};
function searchFold(s) {
  return (s || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[ßẞøØæÆœŒđĐðÐłŁþÞ]/g, ch => _FOLD_MAP[ch] || ch)
    .toLowerCase();
}

/* ---- initials-robust author-name matching --------------------------
   A query like "D. Burghoff" (or "D.P. Burghoff", "David Burghoff",
   "Burghoff, D.") should match an author stored in ANY of those forms.
   We model a personal name as: a SURNAME (the last alphabetic token)
   plus a set of GIVEN-name tokens (everything before it). Two names
   match when:
     - their surnames are equal (after folding), AND
     - every given token present in BOTH names is consistent — i.e. one
       is a prefix of the other (so "D" ⊆ "David", "D" ⊆ "D", and a bare
       surname-only query "Burghoff" matches anyone named Burghoff).
   Initials with periods ("D.", "D.P.") split into ["d","p"]; full given
   names stay whole. Comparison is positional only as far as both sides
   supply tokens, which keeps "D. Burghoff" matching "David P. Burghoff"
   while not over-matching unrelated people. */
function _splitGivenTokens(str) {
  // "D.P." -> ["d","p"]; "David" -> ["david"]; "J.-P." -> ["j","p"]
  const out = [];
  for (const chunk of searchFold(str).split(/[\s.\-]+/)) {
    const c = chunk.replace(/[^a-z]/g, "");
    if (c) out.push(c);
  }
  return out;
}
function parsePersonName(raw) {
  // Strip markers like trailing '*', affiliation '=1,2', and any
  // "Surname, Given" inversion -> "Given Surname".
  let s = (raw || "").replace(/\*/g, "").trim();
  const eq = s.indexOf("=");
  if (eq >= 0) s = s.slice(0, eq).trim();
  if (!s) return null;
  if (s.includes(",")) {
    const bits = s.split(",");
    s = (bits.slice(1).join(" ") + " " + bits[0]).trim();
  }
  const toks = s.split(/\s+/).filter(Boolean);
  if (!toks.length) return null;
  // Surname = last token that still has letters after folding.
  let surnameIdx = -1;
  for (let i = toks.length - 1; i >= 0; i--) {
    if (searchFold(toks[i]).replace(/[^a-z]/g, "")) { surnameIdx = i; break; }
  }
  if (surnameIdx < 0) return null;
  const surname = searchFold(toks[surnameIdx]).replace(/[^a-z]/g, "");
  const given = [];
  for (let i = 0; i < surnameIdx; i++) {
    for (const g of _splitGivenTokens(toks[i])) given.push(g);
  }
  return { surname, given };
}
function _givenConsistent(a, b) {
  // Positional comparison up to the shorter token list. A bare-surname
  // side (no given tokens) is treated as a wildcard and always passes.
  //
  // Per position, two tokens are consistent when:
  //   - they are identical, OR
  //   - at least ONE side is a single-letter initial that the other side
  //     begins with (so "l" ⊆ "linran", and "d" ⊆ "david").
  // Crucially, when BOTH sides are full multi-letter given names they must
  // be equal — "linran" and "linsheng" share the prefix "lin" but are
  // different names, so they must NOT match. (Same for Qing vs Qili.)
  if (!a.length || !b.length) return true;
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    const x = a[i], y = b[i];
    if (x === y) continue;
    const xInit = x.length === 1;
    const yInit = y.length === 1;
    // Allow an initial to match the other side's first letter, but only
    // when that side either is itself an initial or starts with it.
    if (xInit && y.startsWith(x)) continue;
    if (yInit && x.startsWith(y)) continue;
    return false;
  }
  return true;
}
function personNamesMatch(qName, candName) {
  if (!qName || !candName) return false;
  if (qName.surname !== candName.surname) return false;
  return _givenConsistent(qName.given, candName.given);
}
/* Pull every individual author/presider name a record exposes for
   name-matching. Sources are TIERED so we don't let an initials-only form
   re-introduce ambiguity that the full form already resolved: if a talk
   carries a structured `authors` list (full author names), we match ONLY
   those and ignore the loose alias forms (`author_aliases`).
   Otherwise — e.g. a poster with no detailed entry — we fall back to whatever
   loose forms exist. (Concretely: a "Qili Hu" talk also stores the
   initials "Q. Hu", which would wrongly match a search for "Qing Hu";
   keying off the full author names prevents that.) */
function recordPersonNames(rec) {
  const names = [];
  const push = (s) => { if (s) names.push(s); };
  if (rec.session_id) {
    const authors = Array.isArray(rec.authors) ? rec.authors : [];
    if (authors.length) {
      // Best source: full names. Use these exclusively — do NOT also add the
      // loose alias forms.
      authors.forEach(a => push((a.name || "").trim()));
    } else {
      // No structured full-name list; fall back to the loose alias forms and
      // any single-name fields we have.
      (Array.isArray(rec.author_aliases) ? rec.author_aliases : [])
        .map(p => (p || "").trim()).filter(Boolean).forEach(push);
      push(rec.speaker); push(rec.first_author); push(rec.last_author);
    }
  } else {
    // session: presider field may list multiple, separated by ; or " and "
    (rec.presider || "").split(/;| and /i)
      .map(p => p.trim()).filter(Boolean).forEach(push);
  }
  return names;
}
function recordMatchesPersonName(rec, qName) {
  for (const nm of recordPersonNames(rec)) {
    if (personNamesMatch(qName, parsePersonName(nm))) return true;
  }
  return false;
}

/* ---- Search suggestions (affiliation / co-author quick bubbles) ----
   As the user types, we offer up to a few one-tap bubbles below the box
   that jump straight to an exact affiliation or author search — the same
   destinations as the clickable short-affiliation names and author names
   in the detail views. The pools below are the universe of those exact
   targets, deduped and each paired with a folded form for matching.

   Built lazily on first use and memoised: the program is fixed, so the
   set of affiliations and authors never changes during a session. */
let _suggestPools = null;
function suggestPools() {
  if (_suggestPools) return _suggestPools;
  const affMap  = new Map();   // folded -> display (first form wins)
  const nameMap = new Map();
  const addAff = (s) => {
    const disp = (s || "").trim();
    if (!disp) return;
    const f = searchFold(disp);
    if (f && !affMap.has(f)) affMap.set(f, disp);
  };
  const addName = (s) => {
    const disp = (s || "").trim();
    if (!disp) return;
    const f = searchFold(disp);
    if (f && !nameMap.has(f)) nameMap.set(f, disp);
  };
  for (const t of DATA.talks) {
    (Array.isArray(t.inst_shorts) ? t.inst_shorts : []).forEach(addAff);
    addAff(t.speaker_aff);
    addAff(t.last_aff);
    (Array.isArray(t.authors) ? t.authors : []).forEach(a => addName(a && a.name));
  }
  for (const s of DATA.sessions) {
    (Array.isArray(s.presider_affs_short) ? s.presider_affs_short : []).forEach(addAff);
  }
  const toArrAff = (m) => [...m.entries()]
    .map(([fold, disp]) => ({ fold, disp }))
    .sort((a, b) => a.disp.localeCompare(b.disp));
  // Names also carry a parsed {surname, given} so the initials-robust matcher
  // (shared with co-author search) can run without re-parsing each keystroke.
  const toArrName = (m) => [...m.entries()]
    .map(([fold, disp]) => ({ fold, disp, pname: parsePersonName(disp) }))
    .sort((a, b) => a.disp.localeCompare(b.disp));
  _suggestPools = { affs: toArrAff(affMap), names: toArrName(nameMap) };
  return _suggestPools;
}

/* Surname match for suggestions: equal, OR the typed surname is a prefix of
   the candidate's AND is at least 3 letters. The 3-letter floor is what keeps
   "Capas" → "Capasso" working while stopping a short fragment like "Hu" from
   prefix-matching "Huang"/"Hughes" (a bare 2-letter "Hu" still matches a real
   surname "Hu" exactly via the equality branch). */
function _surnameSuggestMatch(qSur, candSur) {
  if (!qSur || !candSur) return false;
  if (qSur === candSur) return true;
  return qSur.length >= 3 && candSur.startsWith(qSur);
}

/* Does a pooled author name satisfy a typed name query? Surname per the rule
   above; given names via the SAME initials-tolerant consistency check the
   co-author search uses (so "Scott Diddams" hits "Scott A. A. Diddams",
   "Q. Hu" hits "Qing Hu", but "Qing Hu" never hits "Qili Hu"). */
function _nameSuggestMatch(qName, cand) {
  if (!qName || !cand || !cand.pname) return false;
  if (!_surnameSuggestMatch(qName.surname, cand.pname.surname)) return false;
  return _givenConsistent(qName.given, cand.pname.given);
}

/* "Still-typing the last name" match: a query like "David B" or "Scott Did"
   carries a real given token plus a PARTIAL surname. When the given names are
   consistent, accept a surname that merely STARTS WITH the typed fragment —
   no 3-letter floor here, because the leading given name already anchors the
   match (so "David B" → "David Burghoff" without "B" alone matching everyone).
   Requires at least one given token; a bare partial surname still goes through
   the stricter _surnameSuggestMatch (3+ letters) instead.

   The candidate MUST itself carry given tokens. Without this, a query whose
   "given" name is really an affiliation fragment ("BAE S" parses to given
   "bae" + surname "s") would match a structureless author like "Sukeert ."
   (surname "sukeert", no givens): the empty candidate given list is a
   wildcard, so the bogus "bae" given is never contradicted. Demanding the
   candidate have a given name forces "bae" to actually be checked, which it
   then fails. Legitimate authors ("David Burghoff") always have a given. */
function _namePrefixSuggestMatch(qName, cand) {
  if (!qName || !cand || !cand.pname) return false;
  if (!qName.given.length || !qName.surname) return false;
  if (!cand.pname.given.length) return false;
  const cs = cand.pname.surname;
  if (!cs || !cs.startsWith(qName.surname)) return false;
  return _givenConsistent(qName.given, cand.pname.given);
}

/* How many sessions+talks a tapped pill would actually return — the SAME
   counts the landing results page computes (affil via _affilHitPredicates,
   name via recordMatchesPersonName). Pills always carry the full canonical
   display string, so the count is stable per (mode, disp) and worth caching;
   it's what we rank the three pills by. */
const _suggestCountCache = new Map();
function _suggestionResultCount(disp, mode) {
  const key = mode + "\u0000" + disp;
  if (_suggestCountCache.has(key)) return _suggestCountCache.get(key);
  let n = 0;
  if (mode === "affil") {
    const { talkHit, sessHit } = _affilHitPredicates(disp);
    for (const s of DATA.sessions) if (sessHit(s)) n++;
    for (const t of DATA.talks) if (talkHit(t)) n++;
  } else {
    const qName = parsePersonName(disp);
    if (qName) {
      for (const s of DATA.sessions) if (recordMatchesPersonName(s, qName)) n++;
      for (const t of DATA.talks) if (recordMatchesPersonName(t, qName)) n++;
    }
  }
  _suggestCountCache.set(key, n);
  return n;
}

/* Find up to 3 exact-target suggestion pills for the search box.

   Nothing fires until 3 characters are typed.

   AFFILIATIONS match as a literal prefix of the typed text: "UM " → "UM
   Dearborn"/"UMKC", "Michigan" → "Michigan"/"Michigan State".

   AUTHOR NAMES match through the initials-robust person-name logic shared
   with co-author search: a partial surname ("Capas" → "Capasso"), a name
   missing middle initials ("Scott Diddams" → "Scott A. A. Diddams"), or
   given-name initials ("Q. Hu" → "Qing Hu") all hit, while wrong initials
   never do ("Qing Hu" ↛ "Qili Hu"). When the query is a SINGLE word, it is
   additionally tried as a FIRST name — "David" → "David Burghoff", "Fed" →
   "Federico Capasso" — using the same exact/3+ char-prefix rule as surnames.

   Candidates are gathered from both passes, then the final list is the THREE
   with the most underlying results, in descending count order (ties keep the
   earlier/alphabetical encounter). */
function suggestionsFor(raw, limit = 3) {
  const q = searchFold((raw || "").trim());
  if (q.length < 3) return [];          // no pills until 3+ chars
  const pools = suggestPools();
  const cand = [];
  const seen = new Set();               // dedupe by mode+folded display
  const add = (disp, mode) => {
    const k = mode + "\u0000" + searchFold(disp);
    if (seen.has(k)) return;
    seen.add(k);
    cand.push({ disp, mode });
  };

  // Affiliation prefix pass.
  for (const it of pools.affs) {
    if (it.fold.startsWith(q)) add(it.disp, "affil");
  }

  // Author-name pass (initials-robust). Parse the raw query as a person name.
  const qName = parsePersonName(raw);
  const singleWord = (raw || "").trim().split(/\s+/).filter(Boolean).length === 1;
  // Run the name pass when there's either a usable surname (2+ letters) OR a
  // given name anchoring a partial surname ("David B" — surname "b" alone is
  // too short, but the leading "David" makes it safe).
  const hasGiven = !!(qName && qName.given.length);
  if (qName && qName.surname && (qName.surname.length >= 2 || hasGiven)) {
    for (const it of pools.names) {
      // Surname (exact / 3+ prefix) + initials match.
      if (_nameSuggestMatch(qName, it)) { add(it.disp, "name"); continue; }
      // Given name + still-typing partial surname ("David B" → David Burghoff).
      if (_namePrefixSuggestMatch(qName, it)) { add(it.disp, "name"); continue; }
      // Single-word queries also try the token as a FIRST name.
      if (singleWord && it.pname && it.pname.given.length &&
          _surnameSuggestMatch(qName.surname, it.pname.given[0])) {
        add(it.disp, "name");
      }
    }
  }

  // Rank by how many results each pill yields, most first; keep top `limit`.
  cand.sort((a, b) =>
    _suggestionResultCount(b.disp, b.mode) - _suggestionResultCount(a.disp, a.mode));
  return cand.slice(0, limit);
}

function renderSuggestions(raw) {
  const wrap = $("#search-suggest");
  if (!wrap) return;
  wrap.innerHTML = "";
  const sugg = suggestionsFor(raw);
  if (!sugg.length) return;
  for (const s of sugg) {
    wrap.appendChild(el("button", {
      type: "button",
      class: "suggest-bubble",
      title: s.mode === "affil"
        ? `Search affiliation “${s.disp}”`
        : `Find sessions & talks by ${s.disp}`,
      onclick: () => searchFor(s.disp, s.mode),
    }, s.disp));
  }
}

function renderSearch(c) {
  const ctrl = el("section", { class: "search-controls" });
  const input = el("input", {
    type: "search",
    id: "search-input",
    placeholder: "Title, authors, affiliation, abstract…",
    autocomplete: "off",
    autocorrect: "off",
    autocapitalize: "off",
    spellcheck: "false",
  });
  input.value = state.searchQuery || "";
  // Debounce the (relatively heavy) suggestion + results recompute so a fast
  // typist triggers it once after they pause rather than on every keystroke.
  // The query itself is stored and saved IMMEDIATELY so nothing is lost if the
  // user navigates away before the timer fires; only the rendering waits.
  let _searchDebounce = null;
  const SEARCH_DEBOUNCE_MS = 300;
  input.addEventListener("input", () => {
    state.searchQuery = input.value;
    saveState();
    clearTimeout(_searchDebounce);
    _searchDebounce = setTimeout(() => {
      renderSuggestions(state.searchQuery);
      rebuildSearchResults();
    }, SEARCH_DEBOUNCE_MS);
  });
  ctrl.appendChild(input);

  c.appendChild(ctrl);
  // One-tap exact-target bubbles (affiliation / co-author) live just below
  // the input, outside the results container so rebuildSearchResults never
  // clears them.
  c.appendChild(el("div", { id: "search-suggest", class: "search-suggest" }));
  c.appendChild(el("div", { id: "search-results" }));
  renderSuggestions(input.value);
  rebuildSearchResults();
}

/* Pin the app clock to `ms` and refresh everything whose appearance depends
   on "now": the right-hand Me pane (wide screens) and the left list (Now
   group, past/upcoming filtering, today header). Intentionally NOT
   persisted — the override is in-memory only and disappears on reload.

   Note we must NOT call the full render() while the left pane is on Search:
   render() would rebuild the Search view, re-run rebuildSearchResults, see
   the same NOW directive still in the box, and recurse. The Search view
   shows its own confirmation, so it needs no re-render; we refresh the Me
   pane directly, and only full-render when the left pane is on some OTHER
   (time-dependent) view. */
function applyNowOverride(ms) {
  _nowOverride = ms;
  renderMePane();      // wide-screen right pane; no-op on narrow screens
  drawMeConnectors();
  if (currentTopView() !== "list" || state.activeTab !== "search") {
    // Safe: not the Search list, so re-rendering won't re-enter this path.
    render();
  }
}

function rebuildSearchResults() {
  const wrap = $("#search-results");
  if (!wrap) return;
  wrap.innerHTML = "";
  const raw = (state.searchQuery || "").trim();

  // Testing directive: typing "DEBUG TFCA" in search toggles a live overlay of
  // the viewport-height numbers (visualViewport vs innerHeight vs body/--app-h),
  // for diagnosing mobile dynamic-toolbar / viewport gaps. The TFCA suffix
  // keeps it from triggering on a real attendee searching "debug" (plausible
  // at a CS conference). Mirrors the NOW hook: handled before search, leaves no
  // persistent state beyond the overlay which a second invocation (or reload)
  // removes.
  if (raw.toUpperCase() === "DEBUG TFCA") {
    const on = toggleDebugOverlay();
    wrap.appendChild(el("p", { class: "empty" },
      on ? "Debug overlay ON. It shows live viewport heights at the top-left. "
         + "Type DEBUG TFCA again (or reload) to turn it off."
         : "Debug overlay OFF."));
    return;
  }

  // Testing directive: "NOW YYYY-MM-DD HH:MM" pins the app's clock instead
  // of running a search. Handled before the min-length check so the user
  // gets immediate feedback as they finish typing it.
  const nowParsed = parseNowOverride(raw);
  if (nowParsed !== null) {
    if (Number.isNaN(nowParsed)) {
      wrap.appendChild(el("p", { class: "empty" },
        "Couldn’t read that date. Use NOW YYYY-MM-DD HH:MM "
        + "(24-hour), e.g. NOW 2026-05-12 14:30."));
      return;
    }
    applyNowOverride(nowParsed);
    const when = `${dateLabel(new Date(nowParsed))}, `
               + `${timeLabel(new Date(nowParsed))}`;
    const box = el("div", { class: "now-override-note" });
    box.appendChild(el("p", { class: "empty" },
      `Clock pinned to ${when}. Time-based views now behave as if that’s `
      + `the current time. Reload the page to return to the real clock.`));
    wrap.appendChild(box);
    return;
  }

  if (raw.length < 2) {
    wrap.appendChild(el("p", { class: "empty" },
      "Type at least 2 characters to search."));
    return;
  }
  // Diacritic-insensitive search (shared folding + hit predicates).
  // Searching every stored form of a name/affiliation lets a query like
  // "antoine" match a full name even on talks with no structured author entry,
  // while "descos" still matches the initials-form fields.
  const q = searchFold(raw);
  const { talkHit, sessHit } =
    _textHitPredicates(q);

  // Plain substring matching misses a name when the stored form carries a
  // middle name/initial the query omits: searching "Weng Chow" can't find
  // the substring inside "Weng W. Chow". So when the query LOOKS like a
  // person name (a surname plus at least one given token), additionally
  // OR-in the initials-tolerant name matcher, which treats the omitted
  // middle as a wildcard. This only ever ADDS hits to the substring pass.
  const qName = parsePersonName(raw);
  const nameShaped = !!(qName && qName.surname && qName.given.length);
  const talkPred = nameShaped
    ? (t => talkHit(t) || recordMatchesPersonName(t, qName))
    : talkHit;
  const sessPred = nameShaped
    ? (s => sessHit(s) || recordMatchesPersonName(s, qName))
    : sessHit;

  const hits = [
    ...DATA.sessions.filter(sessPred),
    ...DATA.talks.filter(talkPred),
  ].sort((a, b) => cmpTs(a.start_ts, b.start_ts));

  if (hits.length === 0) {
    wrap.appendChild(el("p", { class: "empty" }, `No matches for “${raw}”.`));
    return;
  }
  // Typed search has no single "mode": the query might be a name, an
  // affiliation, or title text. Expose it as a generic probe so the byline
  // can reveal a hidden author or affiliation when one explains the match.
  _bylineSearchCtx = { mode: "text", query: raw, qName: parsePersonName(raw) };
  try {
    renderTimeGrouped(wrap, hits);
  } finally {
    _bylineSearchCtx = null;
  }
}

/* Text-search hit predicates, factored out so the temporary click-search
   ("text" mode) matches exactly what the typed Search tab finds. */
function _textHitPredicates(q) {
  const inText = (s) => searchFold(s).includes(q);
  const inArr  = (arr) => Array.isArray(arr) && arr.some(s => inText(s));
  // Author full names + their loose alias forms; institution long names.
  const inAuthors = (t) => (Array.isArray(t.authors) ? t.authors : [])
    .some(a => inText(a.name));
  const inInsts = (t) => (Array.isArray(t.institutions) ? t.institutions : [])
    .some(i => inText(i.name));
  const talkHit = t =>
       inText(t.title) || inAuthors(t) || inArr(t.author_aliases)
    || inInsts(t)
    || inText(t.id)
    || inText(t.speaker) || inText(t.first_author) || inText(t.last_author)
    || inText(t.speaker_aff) || inText(t.last_aff)
    || inArr(t.inst_shorts)
    || inText(t.abstract);   // abstracts are always searched
  const sessHit = s =>
       inText(s.title) || inText(s.id)
    || (Array.isArray(s.tags) && s.tags.some(t => t && (inText(t.value) || inText(t.key))))
    || inText(s.presider) || inText(s.presider_aff)
    || inText(s.presider_aff_short);
  return { talkHit, sessHit };
}

/* Affiliation matching. An "affil" search is NOT a text search: the query
   is always a canonical short name (the same value rendered on the rows —
   "Caltech", "EPFL", "Polytechnique Montreal"), produced by tapping an
   affiliation. So instead of phrase-matching against text, we ask the
   precise question: does this talk/session carry that exact canonical short
   name? A talk carries it when the name equals one of its canonical
   short-name fields (inst_shorts entries, speaker_aff, or last_aff); a
   session carries it when it equals the presider's canonical short name.

   This is exact set-membership, not substring/phrase matching: "EPFL"
   returns every talk tagged EPFL and nothing else, and a multi-word group
   like "Polytechnique Montreal" matches as a single unit. The whole-word /
   text-bleed gymnastics the old phrase matcher needed (so "MIT" wouldn't
   hit "emitting") are unnecessary here because the canonical short-name
   fields are already the curated identities — there is no free text to
   bleed into. Comparison is diacritic-folded so accent variants unify. */
function _affilHitPredicates(rawQuery) {
  const target = searchFold(rawQuery).trim();
  if (!target) return { talkHit: () => false, sessHit: () => false };

  // Does any entry of a canonical short-name field equal the target? The
  // short-name fields may pack several affiliations separated by
  // ; / | (e.g. multiple presiders); split and compare each entry exactly.
  const nameEquals = (s) => {
    if (!s) return false;
    return searchFold(s).split(/[;/|]+/).some(e => e.trim() === target);
  };
  const nameEqualsArr = (arr) =>
    Array.isArray(arr) && arr.some(nameEquals);

  const talkHit = t =>
       nameEqualsArr(t.inst_shorts)
    || nameEquals(t.speaker_aff)
    || nameEquals(t.last_aff);
  const sessHit = s =>
       nameEquals(s.presider_aff_short)
    || nameEquals(s.presider_aff);
  return { talkHit, sessHit };
}

/* Renders the TEMPORARY click-search results pushed onto the current
   tab's stack (view string "searchresults:<mode>:<query>"). This is the
   landing page when a user taps an author name, an institution, or a
   presider in a detail view. It is intentionally NOT the real Search tab:
   the typed query there is never disturbed, and Back simply pops this
   view to return to the talk/session the user came from. */
function renderClickSearchResults(c, payload) {
  // payload = "<mode>:<query>"
  const ci = payload.indexOf(":");
  const mode  = ci < 0 ? "text" : payload.slice(0, ci);
  const query = ci < 0 ? payload : payload.slice(ci + 1);
  const raw = (query || "").trim();

  // Banner explaining what produced these results, with a Back affordance
  // mirrored in the top bar.
  const KIND_LABEL = {
    name:  "Co-author Search",
    affil: "Affiliation Search",
    text:  "Search",
  };
  const banner = el("div", { class: "click-search-banner" });
  banner.appendChild(el("span", { class: "csb-kind" },
    KIND_LABEL[mode] || "Search"));
  banner.appendChild(el("span", { class: "csb-query" }, raw));
  c.appendChild(banner);

  const wrap = el("div", { id: "search-results" });
  c.appendChild(wrap);

  if (!raw) {
    wrap.appendChild(el("p", { class: "empty" }, "Nothing to search for."));
    return;
  }

  let talkHit, sessHit;
  if (mode === "name") {
    const qName = parsePersonName(raw);
    if (!qName) {
      wrap.appendChild(el("p", { class: "empty" },
        `Couldn't parse a name from “${raw}”.`));
      return;
    }
    talkHit = t => recordMatchesPersonName(t, qName);
    sessHit = s => recordMatchesPersonName(s, qName);
  } else if (mode === "affil") {
    const preds = _affilHitPredicates(raw);
    talkHit = preds.talkHit;
    sessHit = preds.sessHit;
  } else {
    const preds = _textHitPredicates(searchFold(raw));
    talkHit = preds.talkHit;
    sessHit = preds.sessHit;
  }

  const hits = [
    ...DATA.sessions.filter(sessHit),
    ...DATA.talks.filter(talkHit),
  ].sort((a, b) => cmpTs(a.start_ts, b.start_ts));

  if (hits.length === 0) {
    wrap.appendChild(el("p", { class: "empty" }, `No matches for “${raw}”.`));
    return;
  }
  _bylineSearchCtx = {
    mode,
    query: raw,
    qName: mode === "name" ? parsePersonName(raw) : null,
  };
  try {
    renderTimeGrouped(wrap, hits);
  } finally {
    _bylineSearchCtx = null;
  }
}

function renderMe(c) {
  // "Last sync" is surfaced in the chrome, not inline: on WIDE screens in
  // the right pane's header (#me-pane-sync), and on NARROW screens in the
  // top bar next to Copy/Paste (renderTopbarExtras). So the one-pane and
  // two-pane Me views look the same and neither carries an inline banner.

  const ids = scheduledIds();
  const items = [
    ...DATA.sessions.filter(s => ids.has(s.id)),
    ...DATA.talks   .filter(t => ids.has(t.id)),
  ].sort((a, b) => cmpTs(a.start_ts, b.start_ts));

  if (items.length === 0) {
    c.appendChild(el("p", { class: "empty" },
      "Nothing in your schedule. Tap + on any session or talk to add it."));
    // Still show the general conference-notes section, copy button, and
    // attribution below, so notes can be taken even with an empty schedule.
  } else {
    renderTimeGrouped(c, items, { ignoreTypes: true });
  }

  // General conference notes — not tied to any session/talk. Stored under the
  // reserved CONFERENCE_NOTES_KEY in state.notes. The "Copy all" control in
  // this section's header exports these PLUS every scheduled session/talk's
  // notes (see doCopyNotes); the post-copy toast confirms the scope.
  appendNotesBox(c, CONFERENCE_NOTES_KEY, {
    title: "Notes",
    placeholder: "General conference notes…",
    tall: true,
    copyAll: true,
  });

  // Settings section (below Notes): a text-size stepper.
  appendSettingsSection(c);

  // About section at the bottom: the app name (links to the GitHub repo, styled
  // subtly so it reads as tappable without shouting "link"), the app author,
  // an OPTIONAL curator credit, and finally the split-rights note — the app
  // is MIT-licensed; the program data belongs to the conference and its
  // publishers, not to this project.
  const about = el("div", { class: "me-settings me-about" });

  about.appendChild(el("div", { class: "me-attribution" }, [
    el("a", {
      class: "me-attribution-link",
      href: "https://github.com/burghoff/fine_conference_app",
      target: "_blank",
      rel: "noopener noreferrer",
    }, "The Fine Conference App v0.1"),
    el("br"),
    el("a", {
      class: "me-attribution-link",
      href: "https://burghoff.org",
      target: "_blank",
      rel: "noopener noreferrer",
    }, "David Burghoff, UT Austin"),
  ]));

  // Optional curator credit, shown between the app author above and the
  // split-rights note below, set slightly apart from both. When DATA.curator
  // carries at least a name, render two lines "<conference> data curated by"
  // / "<name, affiliation>"; the "<name, affiliation>" text links out when a
  // curator.link is supplied, and is plain (still styled muted) when it isn't.
  // With no curator (or no curator name) nothing is added and the attribution
  // and rights below sit directly under each other.
  const curator = DATA.curator || null;
  const curatorName = curator && (curator.name || "").trim();
  if (curatorName) {
    const confName = (DATA.conference_name || "Conference").trim();
    const curatorAff = (curator.affiliation || "").trim();
    // "name, affiliation" when an affiliation exists, otherwise just the name.
    const curatorText = curatorAff ? (curatorName + ", " + curatorAff)
                                   : curatorName;
    const curatorLink = (curator.link || "").trim();
    const curatorCredit = curatorLink
      ? el("a", {
          class: "me-attribution-link",
          href: curatorLink,
          target: "_blank",
          rel: "noopener noreferrer",
        }, curatorText)
      : document.createTextNode(curatorText);
    about.appendChild(el("div", { class: "me-curator" }, [
      confName + " data curated by",
      el("br"),
      curatorCredit,
    ]));
  }

  about.appendChild(el("div", { class: "me-attribution me-rights" }, [
    "App: MIT License",
    el("br"),
    "Data: Copyrighted by conference and its publishers",
  ]));

  c.appendChild(about);
}

/* Plain-text export of the user's notes. Walks every scheduled talk
   (including talks inside scheduled sessions) in chronological order
   and emits a single-line reference — "<number>, <title>, <authors>" —
   followed by the user's notes underneath. The result is plain text
   with no Markdown, so it pastes cleanly into anything.

   Session-level notes are not part of this export — only talk notes
   are. */
function buildNotesText(stats) {
  const ids = scheduledIds();
  const notes = state.notes || {};

  // Gather every talk we should consider: explicitly-scheduled talks
  // PLUS every talk inside a scheduled session. Dedup by id, sort
  // chronologically.
  const talkSet = new Map();   // id -> talk
  for (const t of DATA.talks) {
    if (ids.has(t.id)) talkSet.set(t.id, t);
  }
  for (const s of DATA.sessions) {
    if (!ids.has(s.id)) continue;
    for (const tid of (s.talk_ids || [])) {
      const t = talkMap[tid];
      if (t && !talkSet.has(t.id)) talkSet.set(t.id, t);
    }
  }
  const talks = [...talkSet.values()]
    .sort((a, b) => cmpTs(a.start_ts, b.start_ts));

  const out = [];
  out.push((DATA.conference_name || "Conference") + " — My Notes");
  out.push("=".repeat(60));
  out.push("");

  let anyEmitted = false;
  let talkCount = 0;            // talks whose notes were actually emitted
  let hasGeneral = false;

  // General conference notes (not tied to any session/talk) come first.
  const general = (notes[CONFERENCE_NOTES_KEY] || "").trim();
  if (general) {
    out.push("General notes");
    for (const ln of general.split("\n")) out.push(`  ${ln}`);
    out.push("");                                // blank separator
    anyEmitted = true;
    hasGeneral = true;
  }

  for (const t of talks) {
    const note = (notes[t.id] || "").trim();
    if (!note) continue;                       // skip talks without notes
    out.push(formatTalkReference(t));
    for (const ln of note.split("\n")) out.push(`  ${ln}`);
    out.push("");                              // blank separator
    anyEmitted = true;
    talkCount++;
  }
  if (!anyEmitted) out.push("(No notes yet.)");
  if (stats) { stats.talkCount = talkCount; stats.hasGeneral = hasGeneral; }
  return out.join("\n");
}

/* Build the one-line citation for a talk: "<id>, <title>, <authors>".
   Authors are joined with semicolons; the speaker is marked with a
   trailing asterisk when known. Prefers the full names from the structured
   `authors` list; falls back to the loose `author_aliases` form. */
function formatTalkReference(t) {
  const parts = [t.id || "", displayTitle(t) || "(untitled)"];

  let authorsStr = "";
  const names = (Array.isArray(t.authors) ? t.authors : [])
    .map(a => (a.name || "").trim()).filter(Boolean);
  if (names.length) {
    const speakerKey = (t.speaker || "").replace(/\s+/g, " ").trim().toLowerCase();
    authorsStr = names.map(name => {
      const isSpeaker = speakerKey
        && name.replace(/\s+/g, " ").trim().toLowerCase() === speakerKey;
      return isSpeaker ? `${name}*` : name;
    }).join("; ");
  } else if (Array.isArray(t.author_aliases) && t.author_aliases.length) {
    authorsStr = t.author_aliases.join("; ");
  }
  if (authorsStr) parts.push(authorsStr);

  return parts.join(", ");
}

async function doCopyNotes() {
  const stats = {};
  const text = buildNotesText(stats);
  // Confirmation conveys the SCOPE (this is why the control needn't spell it
  // out): general conference notes + however many talk notes were included.
  const bits = [];
  if (stats.hasGeneral) bits.push("conference notes");
  if (stats.talkCount) bits.push(`${stats.talkCount} talk${stats.talkCount === 1 ? "" : "s"}`);
  const scope = bits.length ? bits.join(" + ") : "no notes yet";
  try {
    await navigator.clipboard.writeText(text);
    flashToast(`Copied: ${scope}.`);
  } catch (_) {
    showSyncSheet({
      title: "Copy notes",
      hint:  "Long-press the box below to select all and copy.",
      value: text,
      readOnly: true,
      primaryLabel: "Done",
      onPrimary: () => {},
    });
  }
}

/* ─────────────────────────────────────────────────────────────────
   Me-tab connector tree: when a scheduled session ALSO has scheduled
   talks, draw an L-shaped stroke from the bottom-left of the session
   bubble, down past any intervening items, and right into each child
   talk. Painted as an SVG overlay inside #content (z-index 0) so it
   sits BEHIND bubbles and headers. Lines use the session's category
   color so multiple overlapping groups stay readable; if a group's
   Y-range collides with another already-active group, its vertical
   stroke is shifted a few pixels left so they don't visually merge.

   No-op when the active tab is not "me", when the schedule is empty,
   or when no session+talk pair is both in the schedule.
   ───────────────────────────────────────────────────────────────── */
const SVG_NS = "http://www.w3.org/2000/svg";

/* Stroke width / lateral spacing constants — kept in one place so the
   scoot math below stays in lockstep with the spine spacing. */
const SPINE_W       = 1.5;   // line stroke width, px
/* The spine sits 2 px INSIDE the session bubble's colored left strip
   (which is 4 px wide). The bubble's opaque background paints on top of
   the SVG, so the portion of the spine that's vertically inside the
   bubble disappears — the spine now starts at the bubble's vertical
   MIDDLE and runs down, so it appears to emerge from within the bubble
   (which keeps its rounded corners). It likewise butts against each
   child's strip in the middle when the elbow turns right. */
const SPINE_INSET   = 2;

/* ─────────────────────────────────────────────────────────────────
   Blurred "fade chip" helpers — the connector spine passes BEHIND
   labels (the "TALKS" heading, the time-headers). Rather than relying
   on CSS backdrop-filter on the HTML labels (which renders as a flat
   block — the chip color matches the page so there's nothing to make
   it read as blurred), we paint the chips INSIDE the SVG overlay the
   way a hand-drawn SVG would: a soft-edged, semi-transparent rect the
   color of the page background, with a Gaussian blur applied to the
   RECT ITSELF so its edges feather out. Drawn AFTER the spine path but
   still inside the SVG (which sits behind the HTML), the stack becomes:
   spine (bottom) → blurred page-colored rect (fades the line) → HTML
   label text (top, painted by normal flow). The line dissolves into a
   soft smudge under each label instead of cutting through it.

   ensureBlurDefs(svg) installs a reusable feGaussianBlur filter with a
   generous region so the blur isn't clipped at the rect's edges.
   addFadeChip(svg, rect, bg) paints one chip over a label's box. */
const FADE_BLUR_STD = 3.2;   // gaussian stdDeviation, px
const FADE_OPACITY  = 0.78;  // rect opacity — line shows through softly

function ensureBlurDefs(svg) {
  if (svg.querySelector("#conn-blur")) return;
  const defs = document.createElementNS(SVG_NS, "defs");
  const filter = document.createElementNS(SVG_NS, "filter");
  filter.setAttribute("id", "conn-blur");
  // Expand the filter region well beyond the rect so the feathered
  // edges aren't cropped (same idea as the example SVG's x/y/width/
  // height overscan on its blur filter).
  filter.setAttribute("x", "-0.5");
  filter.setAttribute("y", "-0.5");
  filter.setAttribute("width", "2");
  filter.setAttribute("height", "2");
  const blur = document.createElementNS(SVG_NS, "feGaussianBlur");
  blur.setAttribute("stdDeviation", String(FADE_BLUR_STD));
  filter.appendChild(blur);
  defs.appendChild(filter);
  svg.insertBefore(defs, svg.firstChild);
}

/* Paint a single blurred fade chip over `box` ({left,top,width,height}
   in container coords). `bg` is the page background color. A little
   inset padding makes the chip a touch larger than the text so the
   blurred edge fully covers the line under the whole label. */
function addFadeChip(svg, box, bg) {
  const padX = 4, padY = 2;
  const rect = document.createElementNS(SVG_NS, "rect");
  rect.setAttribute("x", String(box.left - padX));
  rect.setAttribute("y", String(box.top - padY));
  rect.setAttribute("width",  String(box.width + padX * 2));
  rect.setAttribute("height", String(box.height + padY * 2));
  rect.setAttribute("rx", "4");
  rect.setAttribute("fill", bg);
  rect.setAttribute("opacity", String(FADE_OPACITY));
  rect.setAttribute("filter", "url(#conn-blur)");
  svg.appendChild(rect);
}

/* Collect container-relative boxes for the labels a connector spine
   passes behind: every .time-header's text chip plus (in session
   detail) the "TALKS" .section-title. Uses the text element's box so
   the chip hugs the label, not the full-width row. */
function fadeChipBoxes(content, rectIn) {
  const boxes = [];
  const push = (el) => {
    if (!el) return;
    const r = rectIn(el);
    boxes.push({ left: r.left, top: r.top,
                 width: r.right - r.left, height: r.bottom - r.top });
  };
  content.querySelectorAll(".time-header .th-text").forEach(push);
  content.querySelectorAll(".section-title").forEach(push);
  return boxes;
}

/* Draw the session→talk connector tree for a Me schedule.

   `container` is the element the schedule was rendered into — normally
   #content (the phone/narrow layout, where Me is a bottom tab) but on
   wide screens it's #me-content (the permanently-affixed right pane).
   Defaulting to #content keeps every existing caller working unchanged.

   We used to gate this on `state.activeTab === "me"`. That's wrong now:
   on wide screens the schedule lives in the right pane regardless of
   which tab the LEFT pane is on. Instead we gate on whether the given
   container is actually showing a Me schedule — which is true when it's
   #me-content (always Me) or when it's #content and the active tab's top
   view is the Me list. */
function drawMeConnectors(container) {
  const content = container || $("#content");
  if (!content) return;
  // Remove any prior overlay first — render() wipes innerHTML so this is
  // usually unnecessary, but it's safe to call repeatedly and protects
  // against resize-handler races.
  const stale = content.querySelector("#me-connectors");
  if (stale) stale.remove();
  // Also drop any stale scoot styles from a previous draw — every draw
  // computes them fresh.
  content.querySelectorAll(".bubble[data-scoot]").forEach(b => {
    b.style.marginLeft = "";
    b.removeAttribute("data-scoot");
  });

  // Is this container actually showing a Me schedule right now?
  const showsMeSchedule = (content.id === "me-content")
    || (content.id === "content" && state.activeTab === "me"
        && currentTopView() === "list");
  if (!showsMeSchedule) return;

  const scheduled = new Set(state.schedule);
  if (scheduled.size === 0) return;

  // Find scheduled session bubbles and group them with their scheduled
  // child talks (matched via the data-session-id attribute that
  // makeBubble emits).
  const sessionBubbles = [...content.querySelectorAll(
    '.bubble.added[data-kind="session"]')];
  if (sessionBubbles.length === 0) return;

  const allBubbles  = [...content.querySelectorAll(".bubble")];
  const talkBubbles = allBubbles.filter(
    b => b.dataset.kind === "talk" && b.classList.contains("added"));

  const groups = [];
  for (const sb of sessionBubbles) {
    const sid = sb.dataset.bubbleId;
    const children = talkBubbles.filter(tb => tb.dataset.sessionId === sid);
    if (children.length === 0) continue;
    groups.push({ sb, sid, children });
  }
  if (groups.length === 0) return;

  // Container-relative coords in the SVG overlay's space, measured via the
  // offsetParent chain rather than getBoundingClientRect()-minus-scroll.
  //
  // Why: the overlay is position:absolute; top:0/left:0 inside `content`,
  // so it lives in the container's CONTENT coordinate space (origin at the
  // top of the scrollable content, independent of where the container sits
  // on screen and independent of scroll). getBoundingClientRect()-based
  // math has to subtract the container's on-screen top (cr.top) and add
  // scrollTop — but cr.top depends on the chrome above the pane (header +
  // the scroll-indicator bar, which appears partway through the render
  // pipeline). If the connectors are drawn in a frame where that chrome's
  // height differs from the settled value, every coordinate is shifted by
  // a constant (the "blur chip sits ~10px above its label" bug, which was
  // the 28px indicator bar landing between measurement and paint).
  // offsetTop/Left are layout positions relative to the offset parent and
  // don't depend on the chrome or scroll at all, so they're stable
  // whenever we run.
  const offsetIn = (el) => {
    let x = 0, y = 0, node = el;
    while (node && node !== content) {
      x += node.offsetLeft;
      y += node.offsetTop;
      node = node.offsetParent;
      if (node && node !== content && !content.contains(node)) break;
    }
    return { x, y };
  };
  const rectIn = (el) => {
    const o = offsetIn(el);
    return {
      top:    o.y,
      bottom: o.y + el.offsetHeight,
      left:   o.x,
      right:  o.x + el.offsetWidth,
    };
  };

  // Step 1 — measure each group's geometry. The spine drops from the
  // session bubble's BOTTOM-LEFT down to the LAST child's vertical
  // MIDPOINT. From the spine, each child gets a horizontal elbow that
  // turns right at the child's midY and terminates a hair INSIDE the
  // child's colored left strip, so the line visibly touches the middle
  // of the strip.
  for (const g of groups) {
    const sr = rectIn(g.sb);
    g.xAnchor = sr.left;     // outer edge of session's colored strip
    g.yStart  = sr.bottom;   // top of the spine
    g.kids    = g.children.map(tb => {
      const r = rectIn(tb);
      return {
        el:     tb,
        left:   r.left,      // outer edge of CHILD's colored strip
        bottom: r.bottom,
        top:    r.top,
        right:  r.right,
        midY:   (r.top + r.bottom) / 2,
      };
    });
    // Spine ends at the last child's vertical midpoint — the elbow into
    // that child is the natural terminus.
    g.yEnd = g.kids[g.kids.length - 1].midY;
  }

  // Step 2 — sequential layout. Every group's spine sits at slot 0,
  // i.e. exactly inside its own session's colored strip (xAnchor +
  // SPINE_INSET). Where two groups' spines would otherwise occupy the
  // same X column, we SCOOT the later group's session bubble right far
  // enough to clear the earlier group's spine + a clearance margin.
  // This produces a layout where every spine cleanly emerges from its
  // own session's bottom-left corner — no "second slot" sitting in
  // empty space next to a session.
  //
  // Process groups in start-Y order so an earlier group's spine X is
  // committed before we lay out the next. The "active" list tracks
  // spines still vertically in play (their yEnd hasn't passed the
  // current group's yStart).
  groups.sort((a, b) => a.yStart - b.yStart);
  const active = []; // [{xSpine, yEnd}]
  const PUSH_PAST = 6;
  for (const g of groups) {
    // Expire any spines that ended above this group's start.
    for (let i = active.length - 1; i >= 0; i--) {
      if (active[i].yEnd < g.yStart) active.splice(i, 1);
    }
    // Compute this group's desired spine X (slot 0 in its current
    // bubble position). If any active spine intersects this session's
    // bubble (i.e., would emerge through its strip or to the left of
    // its strip in a way that looks broken), scoot the session right
    // far enough to clear that spine.
    let sr = rectIn(g.sb);
    let need = 0;
    for (const a of active) {
      // The active spine pierces THIS bubble's vertical band if its X
      // is at or to the right of (bubble.left - SPINE_W) and not yet
      // past (bubble.left + PUSH_PAST). When it does, push so the
      // bubble's new left lands a clearance margin past the spine.
      if (a.xSpine > sr.left - SPINE_W && a.xSpine < sr.left + PUSH_PAST) {
        const want = a.xSpine + PUSH_PAST - sr.left;
        if (want > need) need = want;
      }
    }
    if (need > 0) {
      const cur = parseFloat(getComputedStyle(g.sb).marginLeft) || 0;
      g.sb.style.marginLeft = `${Math.round(cur + need)}px`;
      g.sb.dataset.scoot = "1";
      sr = rectIn(g.sb); // re-read post-scoot position
    }
    g.xAnchor = sr.left;
    // Start the spine at the session bubble's VERTICAL MIDDLE rather
    // than its bottom edge. The bubble's opaque fill paints on top of
    // the SVG, so the segment inside the bubble is hidden — the visible
    // line appears to emerge from the bubble and run "all the way up"
    // into it. Because the line now lives behind the bubble's solid
    // paint, the rounded bottom-left corner no longer reveals a gap, so
    // the bubble keeps its normal rounding (no corner-squaring needed).
    g.yStart  = (sr.top + sr.bottom) / 2;
    g.xSpine  = sr.left + SPINE_INSET;
    g.color = getComputedStyle(g.sb).borderLeftColor || "currentColor";
    // We keep the group "active" until its yEnd, computed below.
    active.push(g);
  }

  // Step 3 — re-measure children AFTER sessions are scooted, and
  // compute each group's yEnd. Also scoot ANY non-member bubble (talk
  // or session) whose left edge would sit under a spine. Children of a
  // group don't get scooted by their own group's spine, but they CAN be
  // scooted by an UNRELATED group's spine that overlaps in Y. Likewise
  // for orphan talks.
  for (const g of groups) {
    g._memberIds = new Set([g.sid, ...g.children.map(c => c.dataset.bubbleId)]);
    g.kids = g.children.map(tb => {
      const r = rectIn(tb);
      return {
        el: tb, left: r.left, bottom: r.bottom, top: r.top,
        right: r.right, midY: (r.top + r.bottom) / 2,
      };
    });
    g.yEnd = g.kids[g.kids.length - 1].midY;
    // Update the active record's yEnd now that we know it.
    const rec = active.find(a => a === g);
    if (rec) rec.yEnd = g.yEnd;
  }

  // Scoot any bubble (non-session, or sessions we missed because they
  // weren't groups themselves) whose left edge is punctured by a spine
  // it doesn't belong to. Need to iterate because scooting one bubble
  // changes what's where; but two passes is plenty for realistic
  // schedules.
  for (let pass = 0; pass < 2; pass++) {
    let didScoot = false;
    for (const b of allBubbles) {
      const bid = b.dataset.bubbleId;
      const r = rectIn(b);
      let scoot = 0;
      for (const g of groups) {
        if (g._memberIds.has(bid)) continue;
        if (r.bottom <= g.yStart || r.top >= g.yEnd) continue;
        if (g.xSpine > r.left - SPINE_W && g.xSpine < r.left + PUSH_PAST) {
          const need = g.xSpine + PUSH_PAST - r.left;
          if (need > scoot) scoot = need;
        }
      }
      if (scoot > 0) {
        const cur = parseFloat(getComputedStyle(b).marginLeft) || 0;
        b.style.marginLeft = `${Math.round(cur + scoot)}px`;
        b.dataset.scoot = "1";
        didScoot = true;
      }
    }
    if (!didScoot) break;
  }

  // After children may have scooted, re-read kid positions one more
  // time so the elbow endpoints are accurate.
  for (const g of groups) {
    g.kids = g.children.map(tb => {
      const r = rectIn(tb);
      return {
        el: tb, left: r.left, bottom: r.bottom, top: r.top,
        right: r.right, midY: (r.top + r.bottom) / 2,
      };
    });
    g.yEnd = g.kids[g.kids.length - 1].midY;
  }

  // Step 5 — build the SVG. Size it to the container's full content box:
  // clientWidth (excludes scrollbar) for width, scrollHeight for the full
  // scrollable height. These are layout values independent of scroll, in
  // the same space as offsetIn() above.
  const svgW = content.clientWidth;
  const svgH = content.scrollHeight;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.id = "me-connectors";
  // Force out-of-flow positioning inline, not just via the stylesheet.
  // This SVG is inserted as content.firstChild; if it is in normal flow
  // for even a single layout pass (e.g. before the #me-connectors CSS
  // rule is matched, or due to the default inline display of <svg>), it
  // opens a line box at the top of the container and pushes all following
  // content down by ~10px. We measured the label/spine positions BEFORE
  // inserting the SVG, so that shift would leave every spine and fade-chip
  // ~10px above its target. Setting position/display inline guarantees the
  // element is out of flow the instant it exists.
  svg.style.position = "absolute";
  svg.style.top = "0";
  svg.style.left = "0";
  svg.style.display = "block";
  svg.setAttribute("width",  svgW);
  svg.setAttribute("height", svgH);
  svg.setAttribute("viewBox", `0 0 ${svgW} ${svgH}`);
  // Pin the rendered box to the viewBox size so coordinates map 1:1 and
  // can't be rescaled by any inherited CSS width/height.
  svg.style.width  = svgW + "px";
  svg.style.height = svgH + "px";

  for (const g of groups) {
    const path = document.createElementNS(SVG_NS, "path");
    const parts = [];
    const xS = g.xSpine;

    // Vertical spine: session-bottom → last-child midpoint.
    parts.push(`M ${xS} ${g.yStart} L ${xS} ${g.yEnd}`);

    // For each child, a horizontal elbow at the child's midline,
    // terminating 2 px into the colored strip (the bubble paints on
    // top, so the last 2 px are visually hidden — the line appears to
    // butt cleanly against the strip with no gap).
    for (const k of g.kids) {
      const xC = k.left + 2;
      parts.push(`M ${xS} ${k.midY} L ${xC} ${k.midY}`);
    }

    path.setAttribute("d", parts.join(" "));
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", g.color);
    path.setAttribute("stroke-width", String(SPINE_W));
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.appendChild(path);
  }

  // Fade chips: paint a blurred, page-colored rect over each label the
  // spine passes behind (time-headers here) so the line dissolves under
  // them. Drawn after the paths; HTML labels still paint above the SVG.
  const bg = getComputedStyle(content).getPropertyValue("background-color");
  const pageBg = (bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent")
    ? bg
    : getComputedStyle(document.body).backgroundColor;
  ensureBlurDefs(svg);
  for (const box of fadeChipBoxes(content, rectIn)) {
    addFadeChip(svg, box, pageBg);
  }

  // Insert at the very start of #content so it paints behind everything
  // else inside this stacking context. (#content is position:relative.)
  content.insertBefore(svg, content.firstChild);
}

/* ─────────────────────────────────────────────────────────────────
   Session-detail connector tree: the same L-shaped "elbow" treatment
   the Me schedule uses, applied to a Session detail view. Here the
   parent is the detail-head (the session header card) rather than a
   session bubble, and EVERY talk listed under it is a child — there's
   exactly one group and no sibling sessions, so none of the Me view's
   scoot/overlap machinery is needed. We draw a single vertical spine
   from the head's bottom-left down to the last talk's midpoint, with a
   horizontal elbow turning right into each talk's colored strip.

   The talks are already indented 22px under the head (see the
   body[data-active-view="session-detail"] rule), which leaves a clean
   gutter for the spine + elbows. Painted as an SVG overlay inside
   #content (z-index 0) so it sits BEHIND the head and bubbles, exactly
   like the Me-tab overlay. No-op unless we're actually on a session
   detail view with at least one talk rendered.
   ───────────────────────────────────────────────────────────────── */
function drawSessionDetailConnectors() {
  const content = $("#content");
  if (!content) return;
  // Clear any prior overlay (render() wipes innerHTML, but resize races
  // and repeated calls are possible).
  const stale = content.querySelector("#session-connectors");
  if (stale) stale.remove();

  if (document.body.dataset.activeView !== "session-detail") return;

  const head = content.querySelector(".detail-head");
  if (!head) return;
  const talkBubbles = [...content.querySelectorAll(
    '.bubble[data-kind="talk"]')];
  if (talkBubbles.length === 0) return;

  // Container-relative coords in the SVG overlay's space (same approach
  // as drawMeConnectors). On narrow screens #content scrolls with the
  // window (scrollTop/Left ~0); on wide screens #content is itself the
  // left-pane scroll container, so scrollTop/Left can be non-zero — we
  // include them so the math is correct in both layouts.
  const cr = content.getBoundingClientRect();
  const sx = content.scrollLeft || 0;
  const sy = content.scrollTop  || 0;
  const rectIn = (el) => {
    const r = el.getBoundingClientRect();
    return {
      top:    r.top    - cr.top  + sy,
      bottom: r.bottom - cr.top  + sy,
      left:   r.left   - cr.left + sx,
      right:  r.right  - cr.left + sx,
    };
  };

  const hr = rectIn(head);
  const kids = talkBubbles.map(tb => {
    const r = rectIn(tb);
    return { left: r.left, midY: (r.top + r.bottom) / 2 };
  });

  // Spine X: 2 px inside the head's colored left strip, mirroring the
  // SPINE_INSET the Me view uses so the line butts cleanly against the
  // strip the head paints on top of it.
  const xS     = hr.left + SPINE_INSET;
  // Start the spine at the header card's VERTICAL MIDDLE rather than its
  // bottom edge. The card's opaque fill paints over the SVG, so the part
  // of the line inside the card is hidden — it appears to run up into the
  // header. Since the line sits behind the card's paint, the rounded
  // bottom-left corner no longer reveals a gap, so we keep it rounded.
  const yStart = (hr.top + hr.bottom) / 2;
  const yEnd   = kids[kids.length - 1].midY;
  const color  = getComputedStyle(head).borderLeftColor || "currentColor";

  const svgW = content.clientWidth;
  const svgH = content.scrollHeight;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.id = "session-connectors";
  svg.setAttribute("width",  svgW);
  svg.setAttribute("height", svgH);
  svg.setAttribute("viewBox", `0 0 ${svgW} ${svgH}`);
  svg.style.width  = svgW + "px";
  svg.style.height = svgH + "px";

  const path = document.createElementNS(SVG_NS, "path");
  const parts = [`M ${xS} ${yStart} L ${xS} ${yEnd}`];
  for (const k of kids) {
    const xC = k.left + 2;   // 2 px into the talk's colored strip
    parts.push(`M ${xS} ${k.midY} L ${xC} ${k.midY}`);
  }
  path.setAttribute("d", parts.join(" "));
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", color);
  path.setAttribute("stroke-width", String(SPINE_W));
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round");
  svg.appendChild(path);

  // Fade chips over the labels the spine passes behind: the time-headers
  // and the "TALKS" section title. Blurred, page-colored rects inside
  // the SVG so the line dissolves softly under each label.
  const bg = getComputedStyle(content).getPropertyValue("background-color");
  const pageBg = (bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent")
    ? bg
    : getComputedStyle(document.body).backgroundColor;
  ensureBlurDefs(svg);
  for (const box of fadeChipBoxes(content, rectIn)) {
    addFadeChip(svg, box, pageBg);
  }

  content.insertBefore(svg, content.firstChild);
}

/* ─────────────────────────────────────────────────────────────────
   Sessions-list inline-expansion connectors: one elbow tree per OPEN
   session. Structurally identical to the session-detail case (one head
   + a contiguous block of child talks, no sibling interleaving), but
   there can be several independent groups down the list — one per
   expanded session — so we iterate the .session-expansion wrappers and
   draw a separate spine for each. No-op unless we're on the Sessions
   list with at least one expanded session that has talks.
   ───────────────────────────────────────────────────────────────── */
function drawSessionListConnectors() {
  const content = $("#content");
  if (!content) return;
  const stale = content.querySelector("#session-list-connectors");
  if (stale) stale.remove();

  // Only on the flat Sessions list (the only place expansions render).
  if (state.activeTab !== "sessions" || currentTopView() !== "list") return;

  const groups = [...content.querySelectorAll(".session-expansion")];
  if (groups.length === 0) return;

  const cr = content.getBoundingClientRect();
  const sx = content.scrollLeft || 0;
  const sy = content.scrollTop  || 0;
  const rectIn = (el) => {
    const r = el.getBoundingClientRect();
    return {
      top:    r.top    - cr.top  + sy,
      bottom: r.bottom - cr.top  + sy,
      left:   r.left   - cr.left + sx,
      right:  r.right  - cr.left + sx,
    };
  };

  const svgW = content.clientWidth;
  const svgH = content.scrollHeight;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.id = "session-list-connectors";
  svg.setAttribute("width",  svgW);
  svg.setAttribute("height", svgH);
  svg.setAttribute("viewBox", `0 0 ${svgW} ${svgH}`);
  svg.style.width  = svgW + "px";
  svg.style.height = svgH + "px";

  // Map session id -> its bubble once, so we don't build dynamic selectors
  // (session ids can contain characters that need CSS escaping).
  const sessionBubbleById = {};
  for (const b of content.querySelectorAll('.bubble[data-kind="session"]')) {
    sessionBubbleById[b.getAttribute("data-bubble-id")] = b;
  }

  for (const box of groups) {
    // The spine's parent is the session BUBBLE that this expansion belongs
    // to (the bubble now holds the detail; the expansion holds only talks).
    const sid = box.getAttribute("data-expansion-for");
    const sBubble = sessionBubbleById[sid];
    if (!sBubble) continue;
    const talkBubbles = [...box.querySelectorAll('.bubble[data-kind="talk"]')];
    if (talkBubbles.length === 0) continue;

    const hr = rectIn(sBubble);
    const kids = talkBubbles.map(tb => {
      const r = rectIn(tb);
      return { left: r.left, midY: (r.top + r.bottom) / 2 };
    });
    const xS     = hr.left + SPINE_INSET;
    // Start the spine high inside the session bubble (near its top third) and
    // also run a horizontal stub rightward to the bubble's horizontal midpoint.
    // The bubble's fill is opaque and paints over the SVG, so this whole
    // upper portion is hidden — but anchoring the line DEEP inside the bubble
    // means that when the bubble briefly scales down on :active press (and its
    // bottom edge lifts a hair), there's no spine tail exposed in the gap: the
    // visible part of the line still starts below the (shrunk) bubble with the
    // anchor safely buried. Without this, the spine began at the bubble's
    // vertical middle and its lower stretch peeked out during the press.
    const yTop   = hr.top + (hr.bottom - hr.top) * 0.33;
    const xMid   = (hr.left + hr.right) / 2;
    const yEnd   = kids[kids.length - 1].midY;
    const color  = getComputedStyle(sBubble).borderLeftColor || "currentColor";

    const path = document.createElementNS(SVG_NS, "path");
    // Horizontal stub under the bubble (spine -> bubble midpoint), then the
    // vertical spine from that anchor down to the last talk, then the elbows.
    const parts = [
      `M ${xS} ${yTop} L ${xMid} ${yTop}`,
      `M ${xS} ${yTop} L ${xS} ${yEnd}`,
    ];
    for (const k of kids) {
      const xC = k.left + 2;   // 2 px into the talk's colored strip
      parts.push(`M ${xS} ${k.midY} L ${xC} ${k.midY}`);
    }
    path.setAttribute("d", parts.join(" "));
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", color);
    path.setAttribute("stroke-width", String(SPINE_W));
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.appendChild(path);
  }

  // No fade chips needed: there are no time-headers or section titles
  // inside an expansion for the spine to cross.
  content.insertBefore(svg, content.firstChild);
}

/* Window resize → bubble layouts may shift, so the SVG paths need to
   rebuild. Debounced. Covers the Me schedule's connector tree, the
   session-detail elbows, and the Sessions-list inline-expansion elbows. */
let _meResizeTimer = null;
window.addEventListener("resize", () => {
  if (_meResizeTimer) clearTimeout(_meResizeTimer);
  _meResizeTimer = setTimeout(() => {
    _meResizeTimer = null;
    // Narrow layout: Me lives in #content; redraw there when it's active.
    if (state.activeTab === "me") drawMeConnectors();
    // Wide layout: the schedule lives in the right pane regardless of the
    // left tab, so redraw its connectors against #me-content.
    if (isWide()) { const p = $("#me-content"); if (p) drawMeConnectors(p); }
    if (document.body.dataset.activeView === "session-detail")
      drawSessionDetailConnectors();
    if (state.activeTab === "sessions" && currentTopView() === "list")
      drawSessionListConnectors();
    // Bubble width changed (e.g. orientation flip): re-evaluate author-name
    // abbreviation so bylines neither over-truncate nor needlessly stay
    // abbreviated at the new width.
    refitAllBylines();
  }, 120);
});

/* Human-readable "Last sync" formatter:
   - never synced            → "never"
   - <60 s ago               → "just now"
   - <60 m ago               → "5 minutes ago"
   - same calendar day       → "today, 2:34 PM"
   - same calendar yesterday → "yesterday, 2:34 PM"
   - older                   → "Tue · May 19, 2:34 PM" */
/* Compact sync label for the Me chrome. Returns the COMPLETE text (including
   the "Synced" verb) or "" when there's been no sync yet — callers render it
   verbatim with no prefix, so the header stays empty until the first sync.
   Units are abbreviated (m/h) for tight chrome; once past a day we show a
   short date instead of an ever-growing "Nd ago". */
function formatLastSync(ts) {
  if (!ts) return "";
  const d   = new Date(ts);
  const now = new Date();
  const diff = (now - d) / 1000;
  if (diff < 60)   return "Synced just now";
  if (diff < 3600) return `Synced ${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `Synced ${Math.floor(diff / 3600)}h ago`;
  const yest = new Date(now); yest.setDate(yest.getDate() - 1);
  if (d.toDateString() === yest.toDateString()) {
    return `Synced yesterday, ${timeLabel(d)}`;
  }
  return `Synced ${dateLabel(d)}`;
}

/* =============================================================== */
/* topbar extras (Copy/Paste on Me)                                 */
/* =============================================================== */

function renderTopbarExtras(tab, top) {
  const slot = $("#topbar-extra");
  slot.innerHTML = "";
  if (tab === "me" && top.view === "list") {
    // "Last sync" sits just left of the Copy/Paste buttons, mirroring the
    // wide Me pane's header so the one-pane and two-pane Me views read the
    // same. Compact + truncating so it never crowds the buttons on a
    // small phone.
    slot.appendChild(el("span", {
      class: "topbar-sync",
      html: esc(formatLastSync(state.lastSyncAt)),
    }));
    slot.appendChild(el("button", {
      class: "icon-btn",
      title: "Paste sync code",
      "aria-label": "Paste sync code",
      onclick: doPaste,
    }, "⇲"));
    slot.appendChild(el("button", {
      class: "icon-btn",
      title: "Copy sync code",
      "aria-label": "Copy sync code",
      onclick: doCopy,
    }, "⧉"));
  } else if (tab === "sessions" && top.view === "list") {
    // Sessions ROOT list corner: a single toggle between Expand All and
    // Collapse All for the inline session expansions. (There's no longer a
    // Home/reset control — tapping the Sessions tab itself returns to this
    // list; see switchTab.) When anything is expanded we offer Collapse All;
    // otherwise, if any session can be expanded, we offer Expand All. Both use
    // the same stroked double-chevron idiom (down = open below, up = close up)
    // so the corner's rendering style is stable as it toggles.
    if ((state.expandedSessions || []).length) {
      slot.appendChild(el("button", {
        class: "icon-btn",
        title: "Collapse all sessions",
        "aria-label": "Collapse all sessions",
        html: '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" '
            + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            + 'stroke-linejoin="round" aria-hidden="true">'
            + '<path d="M5 9.5 12 4l7 5.5"></path>'
            + '<path d="M5 15.5 12 10l7 5.5"></path></svg>',
        onclick: collapseAllSessions,
      }));
    } else if (expandableSessionIds().length) {
      slot.appendChild(el("button", {
        class: "icon-btn",
        title: "Expand all sessions",
        "aria-label": "Expand all sessions",
        html: '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" '
            + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            + 'stroke-linejoin="round" aria-hidden="true">'
            + '<path d="M5 8.5 12 14l7-5.5"></path>'
            + '<path d="M5 14.5 12 20l7-5.5"></path></svg>',
        onclick: expandAllSessions,
      }));
    }
  }
}

/* =============================================================== */
/* copy / paste                                                     */
/* =============================================================== */

function exportState() {
  const payload = {
    v: 1,
    schedule: state.schedule,
    // Per-id audit log of add/delete actions with timestamps. Lets the
    // receiving device merge by "latest action wins" so deletions
    // propagate across devices instead of being silently re-added by
    // a stale Copy from the other side. Old clients ignore this
    // field and fall back to the union-of-schedule behavior; new
    // clients reading an old code synthesize zero-timestamp adds.
    scheduleLog: state.scheduleLog || {},
    showPast: state.showPast,
    activeTab: state.activeTab,
    tabStacks: state.tabStacks,
    searchQuery: state.searchQuery,
    hiddenTypes: state.hiddenTypes,
    selectedDates: state.selectedDates,
    notes: state.notes,
    // Wide-screen Me-pane width preference (CSS px; null = default 1/3).
    meWidth: state.meWidth,
  };
  const json = JSON.stringify(payload);
  // UTF-8-safe base64
  const b64 = btoa(unescape(encodeURIComponent(json)));
  return "CONF1:" + b64;
}

function importState(code) {
  code = (code || "").trim();
  // Tolerate codes pasted with wrapping quotes — straight or smart,
  // single, double, or backtick. People copying out of chat apps,
  // emails, or rich text editors often end up with the code already
  // wrapped (or the editor auto-wraps it on selection). Strip ONE
  // matched pair if present, then re-trim.
  const QUOTE_PAIRS = [
    ['"',  '"'],
    ["'",  "'"],
    ["`",  "`"],
    ["\u201C", "\u201D"],   // curly double  “ ”
    ["\u2018", "\u2019"],   // curly single  ‘ ’
    ["\u00AB", "\u00BB"],   // guillemets    « »
  ];
  for (const [open, close] of QUOTE_PAIRS) {
    if (code.length >= 2 && code.startsWith(open) && code.endsWith(close)) {
      code = code.slice(open.length, code.length - close.length).trim();
      break;
    }
  }
  if (!code.startsWith("CONF1:")) throw new Error("Not a valid sync code.");
  const json = decodeURIComponent(escape(atob(code.slice(6))));
  const data = JSON.parse(json);
  if (data.v !== 1) throw new Error("Unsupported version.");

  // SCHEDULE: per-id last-write-wins merge using the audit logs from
  // both sides. For each id we keep whichever {op, ts} has the larger
  // timestamp; ties favor incoming (a tie really only happens with the
  // zero-timestamp synthesized entries below, where it doesn't matter
  // because both sides agree the item is "added"). The current
  // `schedule` array is then rebuilt from the merged log — entries
  // with op "add" are in, "del" are out.
  //
  // For sync codes from before the log existed (`data.scheduleLog`
  // missing) we synthesize a log from `data.schedule`. These are
  // marked LEGACY: when a legacy "add" meets a local entry we treat
  // the legacy add as authoritative, even if the local entry has a
  // newer timestamp. The reasoning: a pre-log code asserts "these
  // items are in my schedule" without timestamp information, and the
  // user is pasting it specifically to bring that content forward —
  // they almost certainly want the items to appear. Without this
  // special case, a local "del" entry created post-upgrade would
  // swallow legacy adds, so pre-existing schedules would never
  // propagate across devices that had any toggling done locally
  // after upgrade.
  const incomingSchedule = Array.isArray(data.schedule) ? data.schedule : [];
  const incomingHasLog = !!(data.scheduleLog && typeof data.scheduleLog === "object");
  const incomingLog = incomingHasLog
    ? data.scheduleLog
    : Object.fromEntries(incomingSchedule.map(id => [id, { op: "add", ts: 0, legacy: true }]));

  const localLog = state.scheduleLog || {};
  const mergedLog = {};
  const idsSeen = new Set([...Object.keys(localLog), ...Object.keys(incomingLog)]);
  let added = 0, removed = 0;
  const wasScheduled = new Set(state.schedule);
  for (const id of idsSeen) {
    const a = localLog[id];
    const b = incomingLog[id];
    let winner;
    if (a && b) {
      // Legacy adds beat any local entry (see comment above). Otherwise
      // it's a normal last-write-wins comparison.
      if (b.legacy && b.op === "add")      winner = b;
      else if (a.legacy && a.op === "add") winner = a;     // can't really happen now
      else                                  winner = (b.ts >= a.ts) ? b : a;
    } else {
      winner = a || b;
    }
    // Defensive: if the entry is malformed, drop the id from the log
    // entirely so it doesn't poison future merges.
    if (!winner || (winner.op !== "add" && winner.op !== "del")) continue;
    // Strip the `legacy` marker before persisting — it was a transient
    // hint for THIS merge only. The merged entry should look like any
    // other entry going forward.
    mergedLog[id] = { op: winner.op, ts: Number(winner.ts) || 0 };
    const nowScheduled = (winner.op === "add");
    if (nowScheduled && !wasScheduled.has(id))  added++;
    if (!nowScheduled && wasScheduled.has(id))  removed++;
  }
  const merged = Object.keys(mergedLog)
    .filter(id => mergedLog[id].op === "add");

  // selectedDates: incoming is either the new field (an array of ISO
  // strings) OR — from an older sync code — the legacy `dateRange`
  // object {start, end}. Translate the legacy form by expanding the
  // contiguous range against the known conference days.
  let selDates;
  if (Array.isArray(data.selectedDates)) {
    selDates = data.selectedDates.slice();
  } else if (data.dateRange && typeof data.dateRange === "object") {
    const lo = data.dateRange.start || null;
    const hi = data.dateRange.end   || null;
    if (!lo && !hi) {
      selDates = [];
    } else {
      const loCmp = lo || "0000-00-00";
      const hiCmp = hi || "9999-99-99";
      selDates = ALL_DATES.filter(d => d >= loCmp && d <= hiCmp);
    }
  } else {
    selDates = [];
  }

  // NOTES: merge with what's already there. If the same key exists on
  // both sides, prefer the longer (or, on tie, the incoming) text —
  // hopefully the right call most of the time. Empty incoming notes
  // never overwrite a non-empty local one. Net effect: paste can ADD
  // notes from another device, but won't quietly wipe yours.
  const mergedNotes = Object.assign({}, state.notes || {});
  const incomingNotes = (data.notes && typeof data.notes === "object")
    ? data.notes : {};
  const _sessionIds = new Set(DATA.sessions.map(x => x.id));
  for (const key in incomingNotes) {
    if (_sessionIds.has(key)) continue;   // session notes are no longer kept
    const incoming = (incomingNotes[key] || "").toString();
    const existing = (mergedNotes[key] || "").toString();
    if (!incoming) continue;
    if (!existing || incoming.length > existing.length) {
      mergedNotes[key] = incoming;
    }
  }
  // Also strip any session-keyed notes that managed to creep in from
  // an earlier app version's local state.
  for (const key of Object.keys(mergedNotes)) {
    if (_sessionIds.has(key)) delete mergedNotes[key];
  }

  // Which tab the LEFT column lands on, and the nav stacks, after an import.
  //
  // On a NARROW (one-pane) device the sync code is always copied from the Me
  // tab — that's the only place the Copy button lives there — so the receiving
  // phone should likewise stay on Me. Adopting the sender's activeTab is wrong:
  // a code generated in two-pane mode carries whatever tab the sender's LEFT
  // pane happened to be on (Sessions/Talks/Search), which would yank the phone
  // away from Me on paste. So on narrow screens we pin to "me". On WIDE screens
  // Me can't be the left tab at all (it's the permanent right pane), so we take
  // the incoming tab and let render() coerce a stray "me" to Sessions.
  const incomingStacks = data.tabStacks || defaultState().tabStacks;
  const landTab = isWide() ? (data.activeTab || "sessions") : "me";
  let landStacks = incomingStacks;
  if (!isWide()) {
    // Land the phone at the Me ROOT, not wherever the sender's Me stack was
    // left (which on a two-pane sender reflects an unrelated session, or a
    // stale scroll position the receiver shouldn't inherit). A fresh root
    // entry means render() restores no anchor and we open at the top of Me.
    landStacks = Object.assign({}, incomingStacks, {
      me: [{ view: "list", scrollY: 0 }],
    });
  }

  Object.assign(state, {
    schedule:       merged,
    scheduleLog:    mergedLog,
    showPast:       (typeof data.showPast === "object" && data.showPast)
                      ? Object.values(data.showPast).some(Boolean)
                      : !!data.showPast,
    activeTab:      landTab,
    tabStacks:      landStacks,
    searchQuery:    data.searchQuery || "",
    hiddenTypes:    Array.isArray(data.hiddenTypes) ? data.hiddenTypes : [],
    selectedDates:  selDates,
    notes:          mergedNotes,
    // Adopt the incoming pane width if it's a sane number; otherwise keep
    // whatever this device already had. applyMeWidth() (called below)
    // re-clamps it to the current viewport.
    meWidth:        (typeof data.meWidth === "number" && isFinite(data.meWidth))
                      ? data.meWidth
                      : (state.meWidth ?? null),
    lastSyncAt:     Date.now(),
  });
  saveState();
  applyMeWidth();
  render();
  return { added, removed };
}

async function doCopy() {
  const code = exportState();
  try {
    await navigator.clipboard.writeText(code);
    // Note: we deliberately do NOT touch state.lastSyncAt here. "Last
    // sync" reflects when a code was last *pasted* (imported) into this
    // device, not when one was last copied out of it.
    flashToast(`Copied (${code.length} chars).`);
  } catch (_) {
    showCopySheet(code);
  }
}

async function doPaste() {
  try {
    const code = await navigator.clipboard.readText();
    if (code && code.trim()) { doImport(code.trim()); return; }
  } catch (_) {}
  showPasteSheet();
}

function doImport(code) {
  try {
    const { added, removed } = importState(code);
    const pluralize = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
    if (added === 0 && removed === 0) {
      flashToast("Nothing new — already in sync.");
    } else if (removed === 0) {
      flashToast(`Added ${pluralize(added, "item")}.`);
    } else if (added === 0) {
      flashToast(`Removed ${pluralize(removed, "item")}.`);
    } else {
      flashToast(`Added ${added}, removed ${removed}.`);
    }
  }
  catch (e) { window.alert("Couldn’t import: " + e.message); }
}

/* Generic sync sheet — replaces window.prompt() so iOS users get a
   styled in-app card instead of the browser dialog. */
function showSyncSheet({ title, hint, value, readOnly, primaryLabel, onPrimary }) {
  // Remove any existing sheet first.
  document.querySelectorAll(".sheet-overlay").forEach(o => o.remove());

  const overlay = el("div", { class: "sheet-overlay" });
  const card    = el("div", { class: "sheet-card" });
  card.appendChild(el("h2", { class: "sheet-title" }, title));
  if (hint) card.appendChild(el("p", { class: "sheet-hint" }, hint));

  const ta = el("textarea", {
    class: "sheet-textarea",
    rows: 4,
    spellcheck: "false",
    autocapitalize: "off",
    autocorrect: "off",
  });
  if (value)    ta.value = value;
  if (readOnly) ta.readOnly = true;
  card.appendChild(ta);

  const btns = el("div", { class: "sheet-btns" });
  const cancel  = el("button", { class: "sheet-btn sheet-btn-cancel" }, "Cancel");
  const primary = el("button", { class: "sheet-btn sheet-btn-primary" }, primaryLabel);
  btns.appendChild(cancel);
  btns.appendChild(primary);
  card.appendChild(btns);
  overlay.appendChild(card);
  document.body.appendChild(overlay);

  const close = () => overlay.remove();
  cancel.addEventListener("click", close);
  overlay.addEventListener("click", e => { if (e.target === overlay) close(); });
  // Stop card taps from bubbling to the overlay close handler.
  card.addEventListener("click", e => e.stopPropagation());

  // Focus + select after the DOM has settled. The focus must happen
  // inside the same gesture for iOS to pop the keyboard / paste menu.
  requestAnimationFrame(() => {
    ta.focus();
    if (readOnly) ta.select();
  });

  // For paste mode: auto-import as soon as the user pastes.
  if (!readOnly) {
    ta.addEventListener("paste", () => {
      // Wait for the textarea to actually contain the pasted value.
      setTimeout(() => {
        const v = ta.value.trim();
        if (v) { close(); onPrimary(v); }
      }, 30);
    });
  }

  primary.addEventListener("click", () => {
    if (readOnly) { close(); return; }
    const v = ta.value.trim();
    if (!v) { close(); return; }
    close();
    onPrimary(v);
  });
}

function showCopySheet(code) {
  showSyncSheet({
    title: "Copy sync code",
    hint:  "Long-press the text and choose Copy, then paste it on your other device.",
    value: code,
    readOnly: true,
    primaryLabel: "Done",
  });
}

function showPasteSheet() {
  showSyncSheet({
    title: "Paste sync code",
    hint:  "Long-press the box and choose Paste — it’ll import automatically.",
    readOnly: false,
    primaryLabel: "Import",
    onPrimary: doImport,
  });
}

/* =============================================================== */
/* Notes (per session / per talk)                                   */
/* =============================================================== */

/* Auto-size a textarea to its content's height — no internal scroll
   bar, the page scrolls instead. Capped at 60 vh as a safety net for
   very long entries. */
function _autosizeNotes(ta) {
  ta.style.height = "auto";
  const px = Math.min(ta.scrollHeight, window.innerHeight * 0.6);
  ta.style.height = `${px}px`;
}

let _notesSaveTimer = null;
function _scheduleNotesSave() {
  if (_notesSaveTimer) clearTimeout(_notesSaveTimer);
  _notesSaveTimer = setTimeout(() => { saveState(); _notesSaveTimer = null; }, 400);
}

function appendNotesBox(container, itemId, opts) {
  opts = opts || {};
  const title = opts.title || "Notes";
  const placeholder = opts.placeholder || "Add a note…";
  if (!state.notes) state.notes = {};
  const initial = (state.notes[itemId] || "");

  const section = el("section", { class: "notes-section" });
  // The page-level "general conference notes" box (tall) gets a brighter
  // heading via its own class, so it doesn't depend on view-scoping CSS.
  const titleClass = opts.tall ? "section-title notes-title--bright"
                               : "section-title";
  // When asked, the heading becomes a row: the label on the left and a compact
  // copy-everything control (icon + "Copy all") on the right. The control sits
  // OVER the whole Notes section rather than glued to the box, signalling it
  // acts on more than the visible text; the post-copy toast names the scope.
  if (opts.copyAll) {
    const head = el("div", { class: titleClass + " notes-head" });
    head.appendChild(el("span", {}, title));
    const copyBtn = el("button", {
      class: "notes-copy-all",
      type: "button",
      title: "Copy all notes — these plus every talk's notes",
      "aria-label": "Copy all notes, including every talk's notes",
      onclick: () => doCopyNotes(),
    }, [
      // Two-overlapping-sheets copy glyph, drawn as inline SVG so it inherits
      // currentColor and scales with the button's font size.
      el("span", { class: "notes-copy-ico", html:
        '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" '
        + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        + 'stroke-linejoin="round" aria-hidden="true">'
        + '<rect x="9" y="9" width="11" height="11" rx="2"></rect>'
        + '<path d="M5 15V5a2 2 0 0 1 2-2h10"></path></svg>' }),
      el("span", { class: "notes-copy-label" }, "Copy all"),
    ]);
    head.appendChild(copyBtn);
    section.appendChild(head);
  } else {
    section.appendChild(el("div", { class: titleClass }, title));
  }

  const ta = el("textarea", {
    class: opts.tall ? "notes-textarea notes-textarea--tall" : "notes-textarea",
    placeholder: placeholder,
    rows: opts.tall ? "4" : "2",
    spellcheck: "true",
    autocapitalize: "sentences",
  });
  // Set initial value via property (not attribute) so newlines survive.
  ta.value = initial;

  ta.addEventListener("input", () => {
    const v = ta.value;
    if (v.trim() === "") {
      delete state.notes[itemId];
    } else {
      state.notes[itemId] = v;
    }
    _scheduleNotesSave();
    _autosizeNotes(ta);
  });
  // Save on blur immediately so closing the view doesn't drop a pending
  // debounce.
  ta.addEventListener("blur", () => {
    if (_notesSaveTimer) {
      clearTimeout(_notesSaveTimer);
      _notesSaveTimer = null;
      saveState();
    }
  });

  section.appendChild(ta);
  container.appendChild(section);

  // Size after insertion (scrollHeight isn't meaningful until in DOM).
  requestAnimationFrame(() => _autosizeNotes(ta));
}

/* Unique calendar dates the conference spans (derived from session
   start times). Used as the bounds + preset chips on the date-range
   sheet. */
const ALL_DATES = (() => {
  const set = new Set();
  for (const s of DATA.sessions) {
    if (s.start_ts) set.add(s.start_ts.slice(0, 10));
  }
  for (const t of DATA.talks) {
    if (t.start_ts) set.add(t.start_ts.slice(0, 10));
  }
  return [...set].sort();
})();

function _fmtDayShort(iso) {
  // 'YYYY-MM-DD' -> 'Tue · May 19'
  const d = new Date(iso + "T00:00:00");
  return `${_DAY[d.getDay()].slice(0,3)} · ${_MON[d.getMonth()]} ${d.getDate()}`;
}
function _fmtMonDay(iso) {
  const d = new Date(iso + "T00:00:00");
  return `${_MON[d.getMonth()]} ${d.getDate()}`;
}

function dateRangeIsActive() {
  return (state.selectedDates || []).length > 0;
}

function updateDateRangeLabel() {
  const btn = $("#date-range-toggle");
  const lab = $("#dr-label");
  if (!btn || !lab) return;
  const sel = (state.selectedDates || []).slice().sort();
  if (sel.length === 0) {
    lab.textContent = "Days";
    btn.classList.remove("active");
    return;
  }
  btn.classList.add("active");
  if (sel.length === 1) {
    lab.textContent = _fmtDayShort(sel[0]);
  } else if (sel.length === ALL_DATES.length) {
    // Edge case — user selected every day. Treat as "all" visually
    // even though state-wise it's distinct from the empty array.
    lab.textContent = "All days";
  } else {
    lab.textContent = `${sel.length} days`;
  }
}

/* The bottom controls bar ("Show me: Concluded", Days, Types) is a single
   non-wrapping centered row. As the text-size multiplier grows it can get
   wider than the screen; when that happens we drop the "Show me:" prefix —
   the lowest-value text there — to claw back width. We always restore it
   first, then measure: the label is the narrowest element, so hiding it can
   never re-trigger overflow, hence no flicker loop. Deferred via rAF so the
   measurement runs against settled layout. */
function fitBottomControls() {
  const bar = $("#bottom-controls");
  const lab = $("#show-me-label");
  if (!bar || !lab) return;
  requestAnimationFrame(() => {
    if (bar.hidden) return;
    bar.classList.remove("compact");          // measure with label shown
    if (bar.scrollWidth > bar.clientWidth + 1) {
      bar.classList.add("compact");           // overflowing -> drop "Show me:"
    }
  });
}

function showDateRangeSheet() {
  document.querySelectorAll(".sheet-overlay").forEach(o => o.remove());

  const overlay = el("div", { class: "sheet-overlay" });
  const card    = el("div", { class: "sheet-card" });
  card.appendChild(el("h2", { class: "sheet-title" }, "Days"));
  card.appendChild(el("p", { class: "sheet-hint" },
    "Tap days to show only those. Tap again to remove. "
    + "Leave none selected to see every day."));

  // Working copy — changes apply on Done, so Cancel can bail cleanly.
  let working = new Set(state.selectedDates || []);

  const grid = el("div", { class: "dr-day-grid" });

  const renderGrid = () => {
    grid.innerHTML = "";
    // "All days" tile — selected look when working set is empty.
    const allBtn = el("button", {
      class: "dr-day" + (working.size === 0 ? " selected" : ""),
      type: "button",
      onclick: () => { working.clear(); renderGrid(); },
    });
    allBtn.appendChild(el("div", { class: "dr-day-dow" }, "All"));
    allBtn.appendChild(el("div", { class: "dr-day-num" }, "days"));
    grid.appendChild(allBtn);

    for (const iso of ALL_DATES) {
      const d = new Date(iso + "T00:00:00");
      const dow = _DAY[d.getDay()].slice(0, 3);
      const monDay = `${_MON[d.getMonth()]} ${d.getDate()}`;
      const isSel = working.has(iso);
      const tile = el("button", {
        class: "dr-day" + (isSel ? " selected" : ""),
        type: "button",
        onclick: () => {
          if (working.has(iso)) working.delete(iso);
          else                  working.add(iso);
          renderGrid();
        },
      });
      tile.appendChild(el("div", { class: "dr-day-dow" }, dow));
      tile.appendChild(el("div", { class: "dr-day-num" }, monDay));
      grid.appendChild(tile);
    }
  };
  renderGrid();
  card.appendChild(grid);

  const btns   = el("div", { class: "sheet-btns" });
  const cancel = el("button", { class: "sheet-btn sheet-btn-cancel" }, "Cancel");
  const done   = el("button", { class: "sheet-btn sheet-btn-primary" }, "Done");
  btns.appendChild(cancel);
  btns.appendChild(done);
  card.appendChild(btns);
  overlay.appendChild(card);
  document.body.appendChild(overlay);

  const close = () => overlay.remove();
  cancel.addEventListener("click", close);
  overlay.addEventListener("click", e => { if (e.target === overlay) close(); });
  card.addEventListener("click", e => e.stopPropagation());
  done.addEventListener("click", () => {
    // Persist in chronological order — keeps the label deterministic and
    // any future logic that iterates the array sane.
    state.selectedDates = [...working].sort();
    saveState();
    close();
    // Changing which days show alters the list above the fold; keep place.
    rerenderPreservingAnchor();
  });
}

function flashToast(msg) {
  document.querySelectorAll(".toast").forEach(t => t.remove());
  const t = el("div", { class: "toast" }, msg);
  document.body.appendChild(t);
  requestAnimationFrame(() => t.classList.add("show"));
  setTimeout(() => {
    t.classList.remove("show");
    setTimeout(() => t.remove(), 350);
  }, 2200);
}

/* =============================================================== */
/* scroll indicator                                                 */
/* =============================================================== */

/* Update one scroll-position indicator bar. Generalized so it serves
   both the left column's #scroll-indicator (scoped to #content) and the
   wide Me pane's #me-scroll-indicator (scoped to #me-content). The bar
   shows the date/time header that the content has most recently scrolled
   up past — i.e. "what time am I looking at".

   `ind`     — the indicator element.
   `scope`   — the element to search for headers within.
   `bodyCls` — body class toggled when there are any headers (controls
               whether the bar is shown + reserves layout space). */
function updateScrollIndicatorIn(ind, scope, bodyCls) {
  if (!ind || !scope) return;
  const dates = scope.querySelectorAll(".date-header");
  const times = scope.querySelectorAll(".time-header");
  const hasAny = dates.length + times.length > 0;
  if (bodyCls) document.body.classList.toggle(bodyCls, hasAny);
  if (!hasAny) { ind.innerHTML = ""; return; }

  // Indicator's own bottom edge is the "cursor": find the last header that
  // has scrolled to or past it. If none yet, show the first header (peek).
  const cursor = ind.getBoundingClientRect().bottom + 2;
  let curDate = "", curTime = "";
  dates.forEach(h => { if (h.getBoundingClientRect().top <= cursor) curDate = h.textContent; });
  times.forEach(h => { if (h.getBoundingClientRect().top <= cursor) curTime = h.textContent; });
  if (!curDate && dates.length) curDate = dates[0].textContent;
  if (!curTime && times.length) curTime = times[0].textContent;

  // Build the label, then guard against wrapping at high zoom: the bar is a
  // fixed-height single-line strip, so if "Tue · May 19 · 1:00 PM" wraps we
  // progressively shorten — first drop the weekday from the date ("May 19"),
  // and if it STILL wraps, drop the date entirely and keep just the time.
  const buildHtml = (dateText) => {
    const p = [];
    if (dateText) p.push(`<span class="date">${esc(dateText)}</span>`);
    if (curTime)  p.push(`<span class="time">${esc(curTime)}</span>`);
    let h = p.join('<span class="sep">·</span>');
    if (ind.id === "scroll-indicator"
        && state.activeTab === "sessions" && currentTopView() === "list") {
      h += `<span class="ind-hint">Hold for detail</span>`;
    }
    return h;
  };
  const wraps = () => ind.scrollHeight > ind.clientHeight + 1;

  ind.innerHTML = buildHtml(curDate);
  if (curDate && wraps()) {
    // Drop the weekday prefix: "Tue · May 19" -> "May 19".
    const shorter = curDate.includes("·")
      ? curDate.slice(curDate.lastIndexOf("·") + 1).trim()
      : curDate;
    ind.innerHTML = buildHtml(shorter);
    if (wraps()) ind.innerHTML = buildHtml("");   // last resort: time only
  }
}

/* Set `el`'s text to `full`, but fall back to `short` if `full` can't fit on
   a single line at the current width/zoom. Measured by temporarily forcing
   nowrap and comparing scrollWidth to clientWidth — so "would this wrap?" is
   answered without line-height math. Used so "My Schedule" collapses to "Me"
   at high zoom (or narrow widths) instead of breaking onto two lines. */
function fitTitle(el, full, short) {
  if (!el) return;
  el.textContent = full;
  const prevWS = el.style.whiteSpace;
  el.style.whiteSpace = "nowrap";
  const overflows = el.scrollWidth > el.clientWidth + 1;
  el.style.whiteSpace = prevWS;
  if (overflows) el.textContent = short;
}

function updateScrollIndicator() {
  // Left column: scoped to #content so it never reads the Me pane's
  // headers (which would otherwise cross-contaminate now that the pane
  // also emits date/time headers).
  updateScrollIndicatorIn($("#scroll-indicator"), $("#content"), "has-indicator");
  // Wide Me pane: its own indicator, scoped to #me-content. No-op on
  // narrow screens (the pane and its indicator are display:none).
  if (isWide()) {
    updateScrollIndicatorIn($("#me-scroll-indicator"), $("#me-content"), "has-me-indicator");
  }
}

/* =============================================================== */
/* types panel                                                      */
/* =============================================================== */

function refreshTypesPanel() {
  const panel = $("#types-panel");
  const arrow = $("#types-toggle-arrow");
  if (!panel) return;
  // Rebuild rows for the current tab.
  panel.innerHTML = "";
  const entries = typesForTab(state.activeTab);
  for (const { color: c, count } of entries) {
    const off = state.hiddenTypes.includes(c);
    const row = el("label", { class: `types-row${off ? " off" : ""}` });
    row.appendChild(el("span", { class: `swatch`,
      style: `background: var(--c-${c}-fg); border-color: var(--c-${c}-fg);` }));
    row.appendChild(el("span", { class: "label" },
      labelForType(c, state.activeTab)));
    row.appendChild(el("span", { class: "count" }, String(count)));
    const cb = el("input", { type: "checkbox" });
    cb.checked = !off;
    cb.addEventListener("change", () => {
      const set = new Set(state.hiddenTypes);
      if (cb.checked) set.delete(c); else set.add(c);
      state.hiddenTypes = [...set];
      saveState();
      // Hiding/showing a type reshuffles the list; keep the user's place.
      rerenderPreservingAnchor();
    });
    row.appendChild(cb);
    panel.appendChild(row);
  }
  panel.classList.toggle("open", !!state.typesPanelOpen);
  arrow.textContent = state.typesPanelOpen ? "▴" : "▾";
}

function toggleTypesPanel(force) {
  state.typesPanelOpen = (typeof force === "boolean")
    ? force : !state.typesPanelOpen;
  saveState();
  refreshTypesPanel();
}



/* True when we're on a wide screen showing the two-pane layout. The match
   condition is templated from WIDE_QUERY in the Python builder, the same
   string used by the stylesheet's media queries, so JS and CSS can never
   disagree about what "wide" means. */
function isWide() {
  return window.matchMedia("__WIDE_QUERY__").matches;
}

/* The top-of-stack view string for the currently active (left) tab.
   Used by drawMeConnectors to tell whether #content is showing Me. */
function currentTopView() {
  const stack = state.tabStacks[state.activeTab];
  const top = stack && stack[stack.length - 1];
  return top ? top.view : "";
}

/* Render the permanently-affixed right-hand Me pane (wide screens only).
   Reuses renderMe() — the very same renderer the Me tab uses — into the
   pane's own scroll container, then draws the connector tree scoped to
   that container. The pane has its own Copy/Paste buttons in its header
   (the left top bar's extras only appear when the LEFT pane is on Me,
   which never happens on wide screens). No-op on narrow screens, where
   the pane is display:none and Me is a normal bottom tab. */
/* Render the permanently-affixed right-hand Me pane (wide screens only).
   Reuses renderMe() — the very same renderer the Me tab uses — into the
   pane's own scroll container, then draws the connector tree scoped to
   that container. A small Copy/Paste sync toolbar is prepended to the
   pane content (the pane has no separate header; its only chrome is the
   bottom Me tab button). No-op on narrow screens, where the pane is
   display:none and Me is a normal bottom tab. */
/* Apply the persisted text-size multiplier. state.fontScale is a plain
   number (1 = default); we clamp it to [FS_MIN, FS_MAX] and write it to the
   --fs CSS variable on the root. Every font-size in the stylesheet is
   expressed as calc(<px> * var(--fs)), so this scales TEXT ONLY — box
   geometry (padding, heights, gaps) and therefore the connector-tree and
   scroll math are untouched. At --fs:1 the rendering is identical to the
   unscaled design. Returns the clamped value actually applied. */
function applyFontScale() {
  let f = state.fontScale;
  if (typeof f !== "number" || !isFinite(f)) f = 1;
  f = Math.max(FS_MIN, Math.min(FS_MAX, f));
  document.documentElement.style.setProperty("--fs", f);
  return f;
}

/* Find the scrollable ancestor that actually moves `node`. On wide screens
   the Me pane (#me-content) and left column (#content) are their own
   overflow scrollers; on narrow screens neither scrolls internally and the
   window scrolls instead. Rather than guess by id (which is layout-
   dependent), we walk up and pick the first ancestor that is genuinely
   scrollable right now — overflow-y auto/scroll AND content taller than the
   box — falling back to the window. Returns read/scrollBy helpers so the
   caller doesn't care which it is. */
function scrollParentOf(node) {
  let n = node ? node.parentElement : null;
  while (n && n !== document.body && n !== document.documentElement) {
    const oy = getComputedStyle(n).overflowY;
    if ((oy === "auto" || oy === "scroll") && n.scrollHeight > n.clientHeight + 1) {
      return { top: () => n.scrollTop, by: (dy) => { n.scrollTop += dy; } };
    }
    n = n.parentElement;
  }
  // Narrow layout / page flow: the window scrolls.
  return {
    top: () => window.scrollY || window.pageYOffset || 0,
    by: (dy) => window.scrollBy(0, dy),
  };
}

/* Redraw every connector overlay after a text-size change. --fs is pure CSS,
   so the text reflows on its own; only the absolutely-positioned connector
   SVGs (which are measured against live geometry) need repainting. We can't
   draw synchronously here: the calc(px * var(--fs)) reflow — and any web-font
   reflow it triggers — isn't settled in the same tick, so a synchronous draw
   measures stale positions (the bug where connectors land in odd spots, or a
   stale tall overlay leaves phantom empty space, until the 60s periodic
   render() corrects it). Instead we draw on the SETTLED layout via the same
   double-rAF the render path uses, then a short deferred catch for late
   reflow.

   We call ALL THREE left-context drawers (each is a no-op when its view
   isn't showing) plus the wide-screen right pane — matching the render path
   exactly. The earlier version only handled Me + session-detail, so the
   Sessions-list inline-expansion elbows in the OTHER pane weren't redrawn on
   a zoom change (the "other pane sometimes draws weird in two-pane mode"
   bug). Calling all of them unconditionally fixes that. */
function redrawAllConnectors() {
  drawMeConnectors();              // Me schedule in #content (no-op unless active)
  drawSessionDetailConnectors();   // session-detail elbows (self-guards)
  drawSessionListConnectors();     // Sessions-list inline expansion (self-guards)
  if (isWide()) { const p = $("#me-content"); if (p) drawMeConnectors(p); }  // right pane
}
function redrawConnectorsForFontScale() {
  // Settled-layout draw (two frames, matching render()).
  requestAnimationFrame(() => {
    requestAnimationFrame(redrawAllConnectors);
  });
  // Catch any late reflow (web-font swap, scrollbar appearing) a beat later.
  // drawMeConnectors clears its prior overlay first, so a redundant pass just
  // repaints identical pixels — no flicker.
  setTimeout(redrawAllConnectors, 220);
}

/* Nudge the text size by `dir` (+1 / -1) FS_STEP increments, clamped to
   range, then keep the clicked button visually anchored: growing the text
   pushes everything above the control taller, which would otherwise slide
   the button out from under the pointer. We record the button's viewport
   position before the change and scroll its container by the delta after, so
   it stays put. `btn` is the element that was clicked (one of possibly two
   copies of the control — left pane and right pane on wide screens). Rounded
   to one decimal so repeated steps don't accumulate float drift. */
function stepFontScale(dir, btn) {
  const cur = (typeof state.fontScale === "number" && isFinite(state.fontScale))
                ? state.fontScale : 1;
  const next = Math.max(FS_MIN, Math.min(FS_MAX,
                 Math.round((cur + dir * FS_STEP) * 10) / 10));
  if (next === state.fontScale) return;   // already at the rail

  // Anchor: where is the clicked button right now (viewport-relative)?
  // Button-anchoring keeps the tapped +/- control steady under the finger —
  // it lives in the Me area (the Me tab on narrow, the right pane on wide),
  // so this governs whichever scroller the button itself sits in.
  const anchor = btn || null;
  const scroller = anchor ? scrollParentOf(anchor) : null;
  const beforeTop = anchor ? anchor.getBoundingClientRect().top : 0;

  // Separately, when the LEFT column is showing a non-Me list (only possible
  // in two-pane mode — Sessions/Talks/Search on the left while the Me pane
  // holds the font control on the right), pin that list on its nearest
  // session/talk bubble across the reflow. The button anchor above can't do
  // that: it tracks an element in the OTHER pane. (On the Me tab itself the
  // visible list IS the Me schedule the button lives in, so button anchoring
  // already covers it and we deliberately skip the bubble anchor.)
  const leftAnchor = (state.activeTab !== "me") ? captureListAnchor() : null;

  state.fontScale = next;
  applyFontScale();
  saveState();
  updateFontScaleControl();
  redrawConnectorsForFontScale();
  fitBottomControls();   // text width changed; the "Show me:" label may need to drop/return
  // Re-evaluate the adaptive titles and the date/time bar at the new zoom:
  // "My Schedule" may need to collapse to "Me", and the indicator date may
  // need shortening if the bar now wraps.
  if (state.activeTab === "me" && currentTopView() === "list") {
    fitTitle($("#page-title"), "My Schedule", "Me");
  }
  fitTitle($(".me-pane-title"), "My Schedule", "Me");
  updateScrollIndicator();
  // Author-name abbreviation in talk bylines is width/zoom dependent; the
  // --fs reflow above can change whether a byline fits, so re-evaluate them.
  refitAllBylines();

  // Reading getBoundingClientRect forces a synchronous layout, so the new
  // position reflects the reflowed text in this same tick — no rAF needed.
  if (anchor && scroller) {
    const afterTop = anchor.getBoundingClientRect().top;
    const delta = afterTop - beforeTop;
    if (delta) scroller.by(delta);
  }
  // Re-pin the left list (non-Me tabs) on the same bubble after the reflow.
  if (leftAnchor) restoreListAnchor(leftAnchor);
}

/* Reflect the current scale on EVERY copy of the control (the left pane and
   the wide-screen right pane can each hold one): update the percentage
   readout and disable whichever step button sits at its rail. No-op if no
   control is in the DOM (it's rebuilt on each Me render). */
function updateFontScaleControl() {
  const f = (typeof state.fontScale === "number" && isFinite(state.fontScale))
              ? state.fontScale : 1;
  for (const wrap of document.querySelectorAll(".fs-control")) {
    const pct = wrap.querySelector(".fs-pct");
    if (pct) pct.textContent = Math.round(f * 100) + "%";
    const dec = wrap.querySelector(".fs-dec");
    const inc = wrap.querySelector(".fs-inc");
    if (dec) dec.disabled = f <= FS_MIN + 1e-9;
    if (inc) inc.disabled = f >= FS_MAX - 1e-9;
  }
}

/* Build the Settings section shown below Notes on the Me page: a heading and
   a text-size stepper — zoom-out / "130%" / zoom-in magnifier buttons. Lives inside the Me
   content, so it renders identically on wide and narrow layouts. Each step
   button passes itself to stepFontScale so the click can be scroll-anchored
   to the control the user actually touched. */
function appendSettingsSection(c) {
  const sec = el("div", { class: "me-settings" });

  // Magnifying-glass zoom icons (lens + handle, with − / + inside the lens).
  // currentColor so they inherit the button's text color, including the
  // active/disabled states.
  const zoomIcon = (sign) => {
    const bar = sign === "in"
      ? `<line x1="9" y1="6.5" x2="9" y2="11.5"/><line x1="6.5" y1="9" x2="11.5" y2="9"/>`  // plus
      : `<line x1="6.5" y1="9" x2="11.5" y2="9"/>`;                                          // minus
    return `<svg viewBox="0 0 24 24" width="22" height="22" fill="none"
       stroke="currentColor" stroke-width="2" stroke-linecap="round"
       aria-hidden="true">
      <circle cx="9" cy="9" r="6.5"/>
      <line x1="14" y1="14" x2="20.5" y2="20.5"/>
      ${bar}
    </svg>`;
  };

  const row = el("div", { class: "fs-control" });
  row.appendChild(el("button", {
    class: "fs-btn fs-dec", type: "button",
    "aria-label": "Decrease text size",
    html: zoomIcon("out"),
    onclick: (e) => stepFontScale(-1, e.currentTarget),
  }));
  row.appendChild(el("span", { class: "fs-pct", "aria-live": "polite" }, "100%"));
  row.appendChild(el("button", {
    class: "fs-btn fs-inc", type: "button",
    "aria-label": "Increase text size",
    html: zoomIcon("in"),
    onclick: (e) => stepFontScale(1, e.currentTarget),
  }));
  sec.appendChild(row);
  c.appendChild(sec);
  updateFontScaleControl();
}

/* Apply the persisted Me-pane width to the layout. state.meWidth is a
   pixel value (or null = "use the default, one third of the viewport").
   We clamp to a sensible range so the pane can't be dragged to a useless
   sliver or swallow the whole screen, then write --me-w on <html>. The
   clamp is viewport-relative, so this is also called on breakpoint
   crossings and after import to re-fit a width saved on a wider/narrower
   screen. No-op effect on narrow screens (the var is unused there). */
function ME_MIN() { return 280; }                       // px floor
function ME_MAX() { return Math.round(window.innerWidth * 0.6); } // px ceiling
function applyMeWidth() {
  const root = document.documentElement;
  let w = state.meWidth;
  if (typeof w !== "number" || !isFinite(w)) {
    // Default: one third of the viewport. Use a concrete px value so the
    // resizer math (which works in px) starts from the rendered width.
    w = Math.round(window.innerWidth / 3);
  }
  w = Math.max(ME_MIN(), Math.min(ME_MAX(), w));
  root.style.setProperty("--me-w", w + "px");
  return w;
}

/* Wire the drag-to-resize divider on the Me pane's left edge. Dragging
   left widens the pane (it's anchored to the right), dragging right
   narrows it. The width updates live via --me-w; on release we persist
   it to state (and thus into the sync code) and redraw the connector
   tree against the pane's new width. Pointer events cover mouse, touch,
   and pen. No-op until the divider exists; harmless on narrow screens
   (the divider is display:none with the pane). */
function initMeResize() {
  const grip = $("#me-resizer");
  if (!grip) return;
  let dragging = false;
  let pendingW = null;
  let rafScheduled = false;

  // Flushes the latest pending width to the CSS variable on the next frame.
  // pointermove can fire faster than the display refresh; without coalescing,
  // every event causes its own style write and the browser does redundant
  // layout work. Coalescing collapses all writes between paints into one,
  // which combined with content-visibility:auto on bubbles during the drag
  // (see body.me-resizing .bubble in the stylesheet) keeps the drag smooth
  // even with hundreds of bubbles scheduled.
  const flush = () => {
    rafScheduled = false;
    if (pendingW == null) return;
    document.documentElement.style.setProperty("--me-w", pendingW + "px");
    state.meWidth = pendingW;   // provisional; persisted on release
    pendingW = null;
  };

  const onMove = (e) => {
    if (!dragging) return;
    // Pane is right-anchored: its width is (viewport right edge − pointerX).
    pendingW = Math.max(ME_MIN(), Math.min(ME_MAX(),
      Math.round(window.innerWidth - e.clientX)));
    if (!rafScheduled) {
      rafScheduled = true;
      requestAnimationFrame(flush);
    }
    if (e.cancelable) e.preventDefault();
  };
  const onUp = () => {
    if (!dragging) return;
    dragging = false;
    // Flush any pending width immediately so the final position is committed
    // before we remove the .me-resizing optimization class (which otherwise
    // would re-layout off-screen bubbles at a width that's about to change).
    if (pendingW != null) flush();
    document.body.classList.remove("me-resizing");
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    window.removeEventListener("pointercancel", onUp);
    // Persist the final width (into local storage AND the sync payload)
    // and redraw the connector tree for the new geometry.
    applyMeWidth();
    saveState();
    const pane = $("#me-content");
    if (pane) drawMeConnectors(pane);
    // Both panes' bubble widths shift when the divider moves (the Me pane
    // grows while #content shrinks, or vice versa), so re-evaluate author-name
    // abbreviation across all talk bubbles.
    refitAllBylines();
  };

  grip.addEventListener("pointerdown", (e) => {
    if (!isWide()) return;        // divider is hidden on narrow anyway
    dragging = true;
    document.body.classList.add("me-resizing");
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    if (e.cancelable) e.preventDefault();
  });

  // Double-click the divider to reset to the default (1/3 viewport).
  grip.addEventListener("dblclick", () => {
    state.meWidth = null;
    applyMeWidth();
    saveState();
    const pane = $("#me-content");
    if (pane) drawMeConnectors(pane);
    refitAllBylines();   // pane width jumped back to default; re-evaluate
  });
}

/* The Me pane scrolls independently of the window (it's an overflow-y
   container), so its scroll-position indicator must update on the pane's
   OWN scroll events, not the window's. Wire that here. No-op if the pane
   is absent; harmless on narrow screens (the pane never scrolls there
   because it's display:none, and the indicator is hidden). */
function initMePaneScroll() {
  const pane = $("#me-content");
  if (!pane) return;
  pane.addEventListener("scroll", () => {
    updateScrollIndicatorIn($("#me-scroll-indicator"), pane, "has-me-indicator");
  }, { passive: true });
}

/* Render the permanently-affixed right-hand Me pane (wide screens only).
   Reuses renderMe() — the very same renderer the Me tab uses — into the
   pane's own scroll container, then draws the connector tree scoped to
   that container. The pane has a top header ("My Schedule" + Copy/Paste
   sync buttons) and a bottom Me tab button as its chrome. No-op on narrow
   screens, where the pane is display:none and Me is a normal bottom tab. */
function renderMePane() {
  const pane = $("#me-content");
  if (!pane) return;
  if (!isWide()) { disconnectBylineObservers(pane); pane.innerHTML = ""; return; }

  // Copy/Paste sync buttons live in the pane header. Rebuilt each render
  // so they stay wired after DOM swaps. Same handlers the Me tab's
  // top-bar extras use.
  const sync = $("#me-pane-sync");
  if (sync) sync.textContent = formatLastSync(state.lastSyncAt);
  // Collapse the pane title to "Me" if "My Schedule" can't fit on one line
  // (high zoom, or the pane dragged narrow) rather than wrapping.
  fitTitle($(".me-pane-title"), "My Schedule", "Me");
  const extra = $("#me-pane-extra");
  if (extra) {
    extra.innerHTML = "";
    extra.appendChild(el("button", {
      class: "icon-btn", title: "Paste sync code",
      "aria-label": "Paste sync code", onclick: doPaste,
    }, "⇲"));
    extra.appendChild(el("button", {
      class: "icon-btn", title: "Copy sync code",
      "aria-label": "Copy sync code", onclick: doCopy,
    }, "⧉"));
  }

  // Preserve the pane's scroll position across this rebuild. Replacing
  // innerHTML resets scrollTop to 0, so without this the pane would jump
  // to the top every time render() runs — including the once-a-minute
  // "Now" refresh fired from the left pane's list view. That jump also
  // made the connector tree appear to "move" relative to what the user
  // was looking at. We capture the nearest session/talk bubble (robust to
  // the schedule changing height — past items dropping off, filters, text
  // resize) and re-pin it after the rebuild; the absolute scrollTop is kept
  // as a fallback for when the pane has no bubble in view (empty schedule).
  const prevScroll = pane.scrollTop;
  const prevAnchor = captureListAnchor(pane);
  const restorePane = () => {
    if (!(prevAnchor && restoreListAnchor(prevAnchor, pane))) {
      pane.scrollTop = prevScroll;
    }
  };

  disconnectBylineObservers(pane);
  pane.innerHTML = "";
  renderMe(pane);
  // Restore the scroll position before measuring anything that depends on
  // it (indicator + connectors are scroll-position-aware).
  restorePane();
  // The pane's scroll indicator depends on the freshly-rendered headers.
  updateScrollIndicatorIn($("#me-scroll-indicator"), pane, "has-me-indicator");
  // Connector tree, scoped to the pane's own container. Drawn after the
  // pane has had a chance to lay out (two RAFs, matching render()).
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      // Re-assert scroll in case layout shifted it during the rebuild
      // (e.g. content got shorter), then draw against the settled layout.
      restorePane();
      drawMeConnectors(pane);
      updateScrollIndicatorIn($("#me-scroll-indicator"), pane, "has-me-indicator");
    });
  });

  // The connector geometry is measured against the live layout. The main
  // cause of misaligned spines/fade-chips — the first .date-header losing
  // its :first-child margin (and shifting content 10px) when the overlay
  // SVG was inserted ahead of it — is fixed in CSS (see the
  // "#me-connectors + .date-header" rule). As cheap insurance against any
  // OTHER late reflow (web-font swap, a scrollbar appearing), redraw once
  // more shortly after, but only if the content height actually changed.
  scheduleMeConnectorSettle(pane, prevScroll);
}

/* One deferred redraw of the Me-pane connector overlay, fired only if the
   pane's content height changed after the initial draw (i.e. something
   reflowed late). drawMeConnectors removes any prior overlay first, so if
   nothing moved this is a no-op that paints identical pixels — no flicker.
   Preserves scroll position across the redraw. */
function scheduleMeConnectorSettle(pane, prevScroll) {
  const h0 = pane.scrollHeight;
  setTimeout(() => {
    const p = $("#me-content");
    if (!p || !isWide()) return;
    if (p.scrollHeight === h0) return;   // layout was already stable
    const sc = p.scrollTop;
    drawMeConnectors(p);
    p.scrollTop = (typeof prevScroll === "number") ? prevScroll : sc;
  }, 200);
}

function pageTitleFor(tab, top) {
  if (top.view.startsWith("talk:"))    return talkMap[top.view.slice(5)]?.id || "Talk";
  if (top.view.startsWith("session:")) return sessionMap[top.view.slice(8)]?.id || "Session";
  if (top.view.startsWith("searchresults:")) {
    // "searchresults:<mode>:<query>"
    const rest = top.view.slice("searchresults:".length);
    const ci = rest.indexOf(":");
    const query = ci < 0 ? rest : rest.slice(ci + 1);
    return query || "Search";
  }
  const conf = (DATA.conference_name || "").trim();
  const sessionsTitle = conf ? conf + " Sessions" : "Sessions";
  const talksTitle    = conf ? conf + " Talks"    : "Talks";
  return ({ sessions: sessionsTitle, talks: talksTitle, search: "Search", me: "My Schedule" })[tab];
}

function render() {
  // Schedule may have changed since the last render — drop the memoised
  // "partial sessions" Set so it gets recomputed lazily on first lookup.
  invalidatePartial();

  // On wide screens Me is shown permanently in the right pane, so the
  // LEFT pane must never sit on the Me tab. If state restored to Me (or
  // a deep link selected it) while wide, fall back to Sessions for the
  // left column. (switchTab guards the interactive path; this guards
  // restored/initial state.)
  if (isWide() && state.activeTab === "me") {
    state.activeTab = "sessions";
  }

  const tab = state.activeTab;
  const stack = state.tabStacks[tab];
  const top = stack[stack.length - 1];

  // Set the top-bar title text. (The Me tab's "My Schedule" → "Me" collapse
  // happens AFTER the active-tab/back-button attributes below are applied,
  // since the title's available width depends on them — see fitTitle call.)
  $("#page-title").textContent = pageTitleFor(tab, top);
  // Back is meaningful whenever there's something to pop on this tab's
  // stack — which now includes temporary click-search results pushed on
  // top of a detail view.
  $("#back-btn").hidden = stack.length <= 1;

  // Only toggle active state on the LEFT tab bar's buttons. The right
  // pane's Me button (#me-tabbar .tab-btn) is permanently active and must
  // not be cleared when the left pane is on Sessions/Talks/Search.
  $$("#tabbar .tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  document.body.dataset.activeTab = tab;
  // Coarse view kind, independent of which tab we're on. Used by CSS to
  // decide where talk bubbles should nest-indent under their sessions:
  // the Me schedule, any session detail, and any search-results view all
  // show that parent/child relationship; the flat top-level Sessions and
  // Talks lists do not.
  const viewKind =
      top.view.startsWith("session:")       ? "session-detail"
    : top.view.startsWith("talk:")          ? "talk-detail"
    : top.view.startsWith("searchresults:") ? "search-results"
    : (tab === "search")                    ? "search-results"
    : (tab === "me")                        ? "schedule"
    :                                         "list";
  document.body.dataset.activeView = viewKind;

  // Now that data-active-tab / data-active-view are set (they drive the
  // title's grid placement and thus its available width), collapse the Me
  // tab's "My Schedule" to "Me" if it can't fit on one line at this zoom.
  if (tab === "me" && top.view === "list") {
    fitTitle($("#page-title"), "My Schedule", "Me");
  }

  // Show-past control + Days/Types: visible on top-level list views and
  // on the temporary click-search results view (which is itself a list).
  //
  // In two-pane (wide) mode the Me pane is permanently shown on the right,
  // and Me is a Show-past-affected list. So the bar must stay up regardless
  // of what the LEFT pane is showing — clicking a Me bubble pushes a
  // Session/Talk detail onto the left stack, but Me is still open on the
  // right and the toggle still governs it. (On narrow screens there's no
  // such pane, so the original list-only rule still applies.)
  const showPastVisible = isWide()
                          || top.view === "list"
                          || top.view.startsWith("searchresults:");
  $("#bottom-controls").hidden = !showPastVisible;
  $("#show-past").checked = !!state.showPast;
  updateDateRangeLabel();
  fitBottomControls();
  if (!showPastVisible && state.typesPanelOpen) {
    state.typesPanelOpen = false;
  }

  renderTopbarExtras(tab, top);

  const c = $("#content");
  disconnectBylineObservers(c);
  c.innerHTML = "";

  if (top.view === "list") {
    if      (tab === "sessions") renderSessionsList(c);
    else if (tab === "talks")    renderTalksList(c);
    else if (tab === "search")   renderSearch(c);
    else if (tab === "me")       renderMe(c);
  } else if (top.view.startsWith("session:")) {
    renderSessionDetail(c, top.view.slice(8));
  } else if (top.view.startsWith("talk:")) {
    renderTalkDetail(c, top.view.slice(5));
  } else if (top.view.startsWith("searchresults:")) {
    renderClickSearchResults(c, top.view.slice("searchresults:".length));
  }

  // Types-panel + indicator are layout-dependent, so refresh after DOM swap.
  refreshTypesPanel();
  updateScrollIndicator();

  // The permanently-affixed right-hand Me pane (wide screens only). It's
  // rebuilt on every render so that adding/removing items from the left
  // pane is reflected immediately on the right. No-op on narrow screens.
  renderMePane();

  // Restore scroll position after the DOM has actually flushed. Prefer the
  // robust bubble anchor (survives filters/hiding/resize); fall back to the
  // absolute scrollY when there's no anchor (bubble-less view) or its target
  // is gone from the freshly-rendered list.
  const target = top.scrollY || 0;
  const anchor = top.scrollAnchor || null;
  const place = () => {
    if (!(anchor && restoreListAnchor(anchor))) setLeftScrollTop(target);
  };
  // Restore the scroll position SYNCHRONOUSLY now, before the per-bubble byline
  // fits run. Those fits are queued in makeBubble's requestAnimationFrame (set up
  // while building the DOM just above) and fire next frame; each fits its byline
  // only if it's then on-screen. But innerHTML reset scrollTop to 0, so without
  // restoring first the fit pass would shorten the TOP of the list while the user
  // is actually scrolled elsewhere — leaving the genuinely visible bylines to the
  // async observer, which re-fits them a paint later (the unshorten/reshorten
  // flash on returning to a scrolled list). Positioning first makes the visible
  // region correct at fit time. The rAF passes below re-apply it once layout has
  // fully settled (connectors, expansions) in case the anchor shifted.
  place();
  requestAnimationFrame(() => {
    place();
    requestAnimationFrame(() => {
      place();
      updateScrollIndicator();
      // Draw the Me-tab session→talk connector tree once the layout
      // has settled. This is a no-op on every other tab/view.
      drawMeConnectors();
      // Same elbow treatment for a Session detail view (no-op elsewhere).
      drawSessionDetailConnectors();
      // Inline-expansion elbows on the Sessions list (no-op elsewhere).
      drawSessionListConnectors();
    });
  });
}

/* =============================================================== */
/* navigation                                                       */
/* =============================================================== */

function snapshotScroll() {
  const stack = state.tabStacks[state.activeTab];
  if (!stack.length) return;
  const entry = stack[stack.length - 1];
  // Keep the absolute scrollY as a fallback (used for views with no bubbles,
  // e.g. a talk-detail page, and for older sync codes that carry only it).
  entry.scrollY = leftScrollTop();
  // The PRIMARY position record: the nearest session/talk bubble + its offset.
  // This is what we restore from when possible, because it survives the list
  // changing height (past talks hidden, filters toggled, text resized) — an
  // absolute scrollY would point at the wrong content after any of those.
  // Null on views with no bubbles; restore then falls back to scrollY.
  entry.scrollAnchor = captureListAnchor();
}

/* #content is the scroll container in BOTH layouts now — the narrow app-shell
   flex column makes #content the only scroller, and the wide layout already
   scrolled #content (its scrollbar sits at the left pane's right edge). So
   both helpers simply read/write #content.scrollTop; the old window-scroll
   branch for narrow is gone along with window scrolling. */
function leftScrollTop() {
  const c = $("#content");
  return c ? c.scrollTop : 0;
}
function setLeftScrollTop(y) {
  const c = $("#content");
  if (c) c.scrollTop = y;
}

/* Re-render the current view in place while keeping the user looking at the
   same session/talk. Use this for changes that alter the list's CONTENT or
   HEIGHT without changing which view we're on — toggling Show past, turning
   type filters on/off, changing the day filter. Snapshotting the live scroll
   into the current stack entry first means render()'s own restore path picks
   up the fresh bubble anchor and re-pins it after the list rebuilds, instead
   of a stale absolute scrollY yanking the user somewhere unrelated (or to the
   top) once items above the fold appear/disappear. */
function rerenderPreservingAnchor() {
  snapshotScroll();
  render();
}

function navigate(view) {
  snapshotScroll();
  // Persist the CURRENT view's scroll into the CURRENT history entry before we
  // push the next one — otherwise pressing Back later restores this entry's
  // stale (push-time) snapshot and loses the user's scroll place.
  _replaceNav();
  state.tabStacks[state.activeTab].push({ view, scrollY: 0 });
  _pushNav();   // record a new browser-history entry for this drill-down
  saveState();
  render();
}

function back() {
  const stack = state.tabStacks[state.activeTab];
  if (stack.length > 1) {
    stack.pop();
    saveState();
    render();
  }
}

/* ---- Device / browser Back & Forward button integration ----------------
   The app navigates via per-tab view stacks AND a tab bar, not URLs. To make
   the hardware/browser Back & Forward buttons traverse the FULL navigation
   history — drill-downs and tab switches alike, in the order they happened — we
   give every navigation step ONE browser-history entry whose state holds a
   COMPLETE snapshot of the view: the active tab plus every tab's view stack. So
   Back/Forward (any distance, across tabs) just restore that snapshot — no
   per-tab bookkeeping, no direction math, no forward stack.

   The pushes happen in navigate()/switchTab() inside the user's click/tap, so
   the entry carries the user activation Chrome requires (gesture-less pushState
   entries get flagged skippable by Chrome's "history manipulation" intervention,
   which is what made the old re-arm approach flaky on Chrome). We NEVER push
   from popstate. _navIdx tracks our position so the in-app Back button can
   consume an entry via history.back() without ever stepping off the app. */
let _navIdx = 0;   // our position in browser history (0 = the app's base entry)
const _cloneNav = (o) => JSON.parse(JSON.stringify(o));

// A full, restorable view-state. Caller should snapshotScroll() first so the
// active stack's top entry carries the current scroll.
function _navSnapshot() {
  return { tab: state.activeTab, stacks: _cloneNav(state.tabStacks) };
}

function _applyNavSnapshot(snap) {
  if (!snap) return;                 // null => stepping onto the pre-app entry
  state.activeTab = snap.tab;
  state.tabStacks = _cloneNav(snap.stacks);
  saveState();
  render();
}

// Push the current state as a NEW history entry (a forward navigation step).
function _pushNav() {
  _navIdx++;
  try { history.pushState({ fcaIdx: _navIdx, fcaSnap: _navSnapshot() }, ""); }
  catch (_) {}
}

// Overwrite the CURRENT entry's snapshot with live state — for view changes
// that aren't a forward step (Home/reset pops to root), so a later Back/Forward
// never restores a now-stale view from this position.
function _replaceNav() {
  try { history.replaceState({ fcaIdx: _navIdx, fcaSnap: _navSnapshot() }, ""); }
  catch (_) {}
}

function canGoBack() {
  const stack = state.tabStacks[state.activeTab];
  return !!(stack && stack.length > 1);
}

window.addEventListener("popstate", (e) => {
  const st = e.state;
  _navIdx = (st && typeof st.fcaIdx === "number") ? st.fcaIdx : 0;
  _applyNavSnapshot(st && st.fcaSnap);
});

// At startup, stamp the restored-from-storage state onto the base history entry
// (so Back works after a reload) and rebuild the active tab's drill chain so
// Back unwinds it one level at a time. Other tabs ride along in each snapshot
// unchanged. The cross-tab history from before a reload can't be reconstructed
// — only the active tab's current depth — which is the natural limit of a fresh
// load. Runs once.
function seedBackHistory() {
  const tab = state.activeTab;
  const depth = (state.tabStacks[tab] || []).length;
  const snapAt = (d) => {
    const stacks = _cloneNav(state.tabStacks);
    stacks[tab] = stacks[tab].slice(0, d);
    return { tab, stacks };
  };
  _navIdx = 0;
  try { history.replaceState({ fcaIdx: 0, fcaSnap: snapAt(1) }, ""); } catch (_) {}
  for (let d = 2; d <= depth; d++) {
    _navIdx++;
    try { history.pushState({ fcaIdx: _navIdx, fcaSnap: snapAt(d) }, ""); } catch (_) {}
  }
}

/* The element that scrolls the LEFT column right now. On wide screens that's
   #content (its own overflow scroller); on narrow screens the window scrolls
   and there's no element-level scroller, so we return null and callers treat
   that as "the window". Centralised so anchor capture/restore agree on what
   they're measuring against. */
function leftScroller() {
  return isWide() ? $("#content") : null;
}

/* Capture the list item currently anchoring the viewport: the topmost
   .bubble still (partially) in view within `scroller`, recorded as its id +
   parent session id + pixel offset from the scroller's top edge. This is the
   ROBUST scroll position — it survives the list changing height above it
   (talks hidden because they're in the past, type/day filters toggled, text
   resized, etc.), because we re-find the SAME item afterwards rather than
   trusting an absolute pixel scrollY that now points somewhere else.

   `scroller` defaults to the active left scroller; pass an explicit element
   (e.g. the Me pane) to anchor a different container. Null when no bubble is
   in view (an empty list, or a detail view with no bubbles) — callers then
   fall back to absolute scrollY. */
function captureListAnchor(scroller) {
  if (scroller === undefined) scroller = leftScroller();
  const topEdge = scroller ? scroller.getBoundingClientRect().top : 0;
  const bubbles = (scroller || document).querySelectorAll(".bubble[data-bubble-id]");
  let best = null;
  for (let i = 0; i < bubbles.length; i++) {
    const b = bubbles[i];
    const r = b.getBoundingClientRect();
    // First bubble whose bottom is still below the top edge — i.e. the
    // topmost one at least partially in view.
    if (r.bottom > topEdge + 1) {
      // Remember a few of the FOLLOWING items' ids too. If the anchor item
      // and its parent session are both gone on restore (e.g. they were in
      // the past and Show past got turned off), restore() walks this chain
      // to the next surviving bubble so we still land near where the user
      // was rather than snapping to the top.
      const fallbacks = [];
      for (let j = i + 1; j < bubbles.length && fallbacks.length < 8; j++) {
        const id = bubbles[j].getAttribute("data-bubble-id");
        if (id) fallbacks.push(id);
      }
      best = {
        id:        b.getAttribute("data-bubble-id"),
        sessionId: b.getAttribute("data-session-id") || "",
        offset:    r.top - topEdge,   // px from scroller top to the bubble top
        fallbacks,                    // ordered ids that followed it in the list
      };
      break;
    }
  }
  return best;
}

/* Re-scroll the freshly-rendered list so `anchor` (from captureListAnchor)
   sits at the same offset from the top it had before. Falls back to the
   anchor's parent SESSION bubble when the original item was a talk inside a
   now-collapsed/hidden session (so the talk bubble no longer exists). When
   neither the item nor its parent session is present any more — e.g. the
   nearest item dropped out because it's in the past and Show past was turned
   off — we step FORWARD through the captured-from list to the next surviving
   bubble so we land as close as possible to where the user was, rather than
   giving up. Returns true if it managed to anchor on something, false if it
   couldn't (caller then falls back to absolute scrollY).

   `scroller` defaults to the active left scroller; pass an explicit element
   to restore within a different container (the Me pane scrolls its own
   #me-content rather than the window/#content). */
function restoreListAnchor(anchor, scroller) {
  if (!anchor) return false;
  if (scroller === undefined) scroller = leftScroller();
  const root = scroller || document;
  const find = (id) =>
    id ? root.querySelector(`.bubble[data-bubble-id="${cssEsc(id)}"]`)
       : null;
  let target = find(anchor.id);
  if (!target && anchor.sessionId) target = find(anchor.sessionId);
  let usedFallback = false;
  if (!target && Array.isArray(anchor.fallbacks)) {
    // The exact item and its session are both gone (hidden/filtered). Walk
    // forward to the next item that's still present and pin THAT to the top
    // edge — the closest surviving position below where the user was.
    for (const fid of anchor.fallbacks) {
      target = find(fid);
      if (target) { usedFallback = true; break; }
    }
  }
  if (!target) return false;
  const topEdge = scroller ? scroller.getBoundingClientRect().top : 0;
  const cur = target.getBoundingClientRect().top - topEdge;
  // For a real anchor hit, restore its exact offset; for a forward-fallback
  // item, sit it at the top edge (offset 0) since we don't know its original
  // position — it wasn't the anchor.
  const wantOffset = usedFallback ? 0 : anchor.offset;
  const delta = cur - wantOffset;
  if (scroller) {
    scroller.scrollTop += delta;
  } else {
    setLeftScrollTop(leftScrollTop() + delta);
  }
  return true;
}

/* CSS.escape shim for attribute-selector safety (ids here are simple, but
   defensive against odd characters). */
function cssEsc(s) {
  if (window.CSS && CSS.escape) return CSS.escape(s);
  return String(s).replace(/["\\\]]/g, "\\$&");
}

/* Collapse All (Sessions root only): close every inline-expanded session at
   once — the inverse of Expand All, sharing the same corner slot. Preserves the
   user's place: the list shrinks above the fold as expansions close, so we
   re-anchor on the item that was at the top of the viewport rather than letting
   the view jump. Expansion state isn't part of the Back/Forward snapshot (it's a
   view preference, like a filter), so this isn't a history step. */
function collapseAllSessions() {
  if (!(state.expandedSessions || []).length) return;

  // Anchor on the current top bubble before the list reflows shorter.
  const anchor = captureListAnchor();

  state.expandedSessions = [];
  // Sync the root entry's saved position to where we are so render's own
  // restore agrees with the re-anchor below (no flash to a stale spot first).
  const stack = state.tabStacks[state.activeTab];
  stack[0].scrollY = leftScrollTop();
  stack[0].scrollAnchor = anchor || null;

  saveState();
  _replaceNav();   // keep the current entry's scroll fresh after the reflow
  render();

  // Re-anchor after render's own double-rAF scroll restore, so our re-anchor
  // wins (same ordering Expand All relies on).
  if (anchor) {
    requestAnimationFrame(() => requestAnimationFrame(() => {
      restoreListAnchor(anchor);
      updateScrollIndicator();
    }));
  }
}

/* The set of session ids that CAN be inline-expanded right now, in DATA order.
   Mirrors isExpandableSession (resolvable talks, not just non-empty talk_ids),
   so it never disagrees with what the per-session toggle considers openable. */
function expandableSessionIds() {
  return DATA.sessions
    .map(s => s.id)
    .filter(isExpandableSession);
}

/* Expand All (Sessions root only): open every inline-expandable session at
   once. The inverse of Collapse All, and it shares the same corner slot — shown
   only on the Sessions root list with at least one expandable session and none
   currently expanded (see renderTopbarExtras). Preserves the user's place: the
   list grows tall below the anchor as sessions open, so we re-anchor on the
   item that was at the top of the viewport rather than letting the view jump. */
function expandAllSessions() {
  const ids = expandableSessionIds();
  if (!ids.length) return;

  // Anchor on the current top bubble before the list reflows taller.
  const anchor = captureListAnchor();

  snapshotScroll();
  state.expandedSessions = ids;
  saveState();
  _replaceNav();   // keep the current entry's scroll fresh after the reflow
  render();

  // Re-anchor after render's own double-rAF scroll restore, so our re-anchor
  // wins (same ordering Collapse All relies on).
  if (anchor) {
    requestAnimationFrame(() => requestAnimationFrame(() => {
      restoreListAnchor(anchor);
      updateScrollIndicator();
    }));
  }
}

/* Tapping a tab takes you to that section's MAIN list, in the place you last
   left it — collapsing any drill-down that tab was holding. This replaces the
   old per-tab "remembered sub-view" (a Talk detail left open under Sessions,
   etc.), which forced the user to track four independent states; the robust
   Back button can always return to a detail you came from. It does NOT collapse
   inline-expanded sessions (that's the Collapse All control's job) — only the
   navigation stack. A no-op when you're already on that tab's root. */
function switchTab(tab) {
  // On wide screens Me is permanently shown in the right pane, so the
  // left tab bar can't switch to it (the button is hidden anyway — this
  // guards programmatic / restored paths).
  if (tab === "me" && isWide()) return;
  const stack = state.tabStacks[tab];
  const drilledIn = !!(stack && stack.length > 1);
  if (tab === state.activeTab && !drilledIn) return;   // already at this root
  snapshotScroll();
  _replaceNav();                 // save where we are now, so Back returns here
  state.activeTab = tab;
  if (drilledIn) stack.length = 1;   // drop to the tab's root list
  _pushNav();                    // a Back/Forward-able navigation step
  saveState();
  render();
}

/* Click-to-search: used by clickable institutions, author names, and
   presider name/affiliation in the detail views.

   Rather than hijacking the real Search tab (which used to clobber the
   user's typed query and frequently lost its Back button), this pushes a
   TEMPORARY, self-contained search-results view onto the CURRENT tab's own
   navigation stack — encoded as "searchresults:<query>". Consequences:

     - The real Search tab and the user's typed query are never touched.
     - Back works by ordinary stack-pop semantics, so it always returns to
       the exact Talk/Session detail the user came from.
     - Chaining click-searches (tap an institution in the results, etc.)
       just stacks more views, and Back unwinds them one at a time.

   `mode` ("name" | "text") controls match semantics for the temporary
   results: "name" does initials-robust author-name matching; "text" (the
   default) is the same substring search the real Search tab uses. */
function searchFor(query, mode) {
  query = (query || "").trim();
  if (!query) return;
  const m = (mode === "name" || mode === "affil") ? mode : "text";
  const view = "searchresults:" + m + ":" + query;
  navigate(view);
}

/* Jump to the Search tab and focus its input, selecting any existing query so
   the user can immediately overtype. Used by the Search tab button and Ctrl+F /
   Cmd+F — both gated to wide (two-pane) layouts by their callers, since on a
   single-pane (mobile) layout auto-focusing yanks the keyboard open the moment
   Search is tapped. We reset the search tab's nav stack to its root (the list
   view that renders the search box) in case it was on a pushed sub-view (e.g. a
   clicked author/affil result).

   render() builds #search-input synchronously, so we focus SYNCHRONOUSLY here
   (still inside the triggering gesture); the rAF pass re-asserts it only if the
   first focus didn't take (element not yet focusable mid layout), so it never
   steals a selection the user has already started editing. */
function focusSearch() {
  snapshotScroll();
  const stack = state.tabStacks["search"];
  // Did this actually change the view? (arriving from another tab, or
  // collapsing a drilled-in Search sub-view). If so it's a Back/Forward-able
  // step; if we're already on the Search root, just re-focus, don't push.
  const changed = state.activeTab !== "search" || (stack && stack.length > 1);
  if (changed) _replaceNav();    // save where we came from (Back scroll place)
  state.activeTab = "search";
  if (stack && stack.length > 1) stack.length = 1;
  if (changed) _pushNav();
  saveState();
  render();
  const focusIt = () => {
    const input = $("#search-input");
    if (input && document.activeElement !== input) {
      input.focus();
      input.select();
    }
  };
  focusIt();
  requestAnimationFrame(focusIt);
}

/* =============================================================== */
/* events                                                           */
/* =============================================================== */

/* Keep the satisfying center-shrink on the +/- circles (.schedule-btn and
   .dh-add) without losing taps. Those buttons scale down about their center
   on :active, which slides their edges inward; a press that landed near an
   edge can end up just OUTSIDE the shrunken button, so pointerup happens off
   it and the browser fires no click — the tap is "missed".

   The fix: the instant a press STARTS on one of these buttons, capture the
   pointer to it. Pointer capture routes every subsequent pointer event — and
   crucially the synthesized click — to that button regardless of where the
   pointer actually is, so a press that began on the circle always counts even
   if the shrink moves the circle out from under a stationary finger. The
   capture is released automatically on pointerup/cancel, so nothing leaks.
   Delegated here (one listener) so it covers every button instance without
   touching the three creation sites. */
document.addEventListener("pointerdown", (e) => {
  const btn = e.target.closest(".schedule-btn, .dh-add");
  if (!btn) return;
  try { btn.setPointerCapture(e.pointerId); } catch (_) {}
}, true);

/* Ctrl+F (Windows/Linux) / Cmd+F (macOS): override the browser's native page
   find and use it to jump into our own Search. Desktop only — gated on
   isWide() because narrow/mobile layouts have no hardware keyboard and the
   chord doesn't apply. Not fired when Alt is held (avoids odd combos). */
document.addEventListener("keydown", (e) => {
  const isFind = (e.ctrlKey || e.metaKey) && !e.altKey
    && (e.key === "f" || e.key === "F");
  if (!isFind) return;
  if (typeof isWide === "function" && !isWide()) return;
  e.preventDefault();
  focusSearch();
});

// Route the in-app Back button through history so it and the device/browser
// Back/Forward stay in sync (history.back() -> popstate -> restore the previous
// snapshot, and a Forward can redo it). Only call history.back() when we own an
// entry behind us; otherwise (e.g. restored state with no seeded entry) pop
// directly so the button can never accidentally leave the app.
$("#back-btn").addEventListener("click", () => {
  if (_navIdx > 0) history.back();
  else if (canGoBack()) back();
});
// Only the LEFT tab bar's buttons switch tabs. The right pane's Me button
// is permanently active and inert (Me is always shown there on wide).
// On wide (two-pane) layouts the Search button routes through focusSearch so a
// click drops focus into the box and selects any existing query, ready to
// overtype (also works when Search is already active). In single-pane mode we
// deliberately DON'T do this: auto-focusing there forces the on-screen keyboard
// open the moment you tap the tab, which is jarring — so it's a plain tab
// switch and the user taps the box themselves when they want to type.
$$("#tabbar .tab-btn").forEach(b => b.addEventListener("click", () => {
  const t = b.dataset.tab;
  if (t === "search" && isWide()) focusSearch();
  else switchTab(t);
}));
$("#show-past").addEventListener("change", e => {
  state.showPast = e.target.checked;
  saveState();
  // Toggling past items changes the list height above the fold, so re-pin on
  // the nearest session/talk instead of letting the old scroll position drift.
  rerenderPreservingAnchor();
});
$("#date-range-toggle").addEventListener("click", showDateRangeSheet);

// Periodically save scroll position so reloads land in the same place.
// On narrow screens the window scrolls; on wide screens the left pane
// scrolls inside #content (so its scrollbar sits at the pane's right
// edge). Wire the same handler to both so the indicator updates and the
// position is saved regardless of which element is actually scrolling.
let _scrollSaveTimer = null;
function onLeftScroll() {
  updateScrollIndicator();
  clearTimeout(_scrollSaveTimer);
  _scrollSaveTimer = setTimeout(() => {
    snapshotScroll();
    // Keep the current history entry's snapshot scroll fresh too, so a later
    // Back/Forward that lands back here restores the user's actual position.
    _replaceNav();
    saveState();
  }, 350);
}
window.addEventListener("scroll", onLeftScroll, { passive: true });
$("#content").addEventListener("scroll", onLeftScroll, { passive: true });

// Types panel toggle
$("#types-toggle").addEventListener("click", () => toggleTypesPanel());
// Tap outside the types panel to close it
document.addEventListener("click", (e) => {
  if (!state.typesPanelOpen) return;
  if (e.target.closest("#types-panel") || e.target.closest("#types-toggle")) return;
  toggleTypesPanel(false);
}, true);

// Swallow swipe-back from edge if it would close us mid-stack (we still
// rely on the in-app back button; phones can use the visible Back).
// We do NOT push history entries — that gets confusing with per-tab stacks.

// Re-render when the viewport crosses the wide/narrow breakpoint so the
// Me pane appears/disappears and the left column re-flows. Also redraw
/* Pin the app-shell column to the ACTUALLY-visible viewport height.
   visualViewport.height excludes the browser's dynamic toolbar and updates as
   it shows/hides, so this fixes the Firefox-Android case where 100dvh resolves
   to a fixed (toolbar-hidden) height and leaves a gap below the tab bar until a
   scroll forces a recompute. Falls back to innerHeight where visualViewport is
   unavailable. We write a CSS var (--app-h) rather than body.style.height so
   the wide layout — which sets body height:auto — is unaffected; only the
   narrow app-shell consumes --app-h. rAF-coalesced because visualViewport can
   fire a burst of resize/scroll events during a toolbar animation. */
let _appHRaf = null;
function syncAppHeight() {
  if (_appHRaf) return;
  _appHRaf = requestAnimationFrame(() => {
    _appHRaf = null;
    const vv = window.visualViewport;
    // During a pinch-zoom the visual viewport legitimately shrinks (that IS
    // the zoom), and it also pans (offsetTop changes). Re-pinning --app-h to
    // that shrunken height mid-gesture fights the zoom and makes the layout
    // jump. So while zoomed (scale !== 1), leave --app-h alone and let the
    // user zoom/pan normally; we re-sync once they return to scale 1.
    if (vv && Math.abs(vv.scale - 1) > 0.01) return;
    // Use the SMALLER of the visual-viewport height and the layout-viewport
    // height (documentElement.clientHeight). These diverge during Firefox's
    // toolbar dance: e.g. visualViewport.height=898 while clientHeight=833.
    // The body is laid out in the LAYOUT viewport, so pinning it to the larger
    // visual height (898) makes the shell 65px taller than its 833 layout box —
    // it overflows below the visible fold into a band of empty space you can
    // pinch-pan to, with the bottom bar pushed down into it. Taking the min is
    // the largest height that fits within BOTH viewports, so the shell never
    // overflows regardless of which way the two diverge.
    const layoutH = document.documentElement.clientHeight || 0;
    const visualH = vv ? vv.height : window.innerHeight;
    let h = layoutH > 0 ? Math.min(visualH, layoutH) : visualH;
    // Round the cap UP. --app-h is used as body's max-height; visualViewport
    // reports fractional values (e.g. 832.9166px) and the body's true height
    // (from top:0;bottom:0) is the integer layout viewport (833). A fractional
    // cap of 832.92 would shave the 833 body to 832.92 and re-open the very
    // sub-pixel gap below the tab bar we're fixing. Ceiling ensures the cap is
    // >= the integer height in the steady state (so it never shrinks the body
    // there) while still clamping to ~visible height when the viewports truly
    // diverge.
    h = Math.ceil(h);
    if (h > 0) document.documentElement.style.setProperty("--app-h", h + "px");
  });
}
syncAppHeight();
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", syncAppHeight);
  window.visualViewport.addEventListener("scroll", syncAppHeight);
}
window.addEventListener("resize", syncAppHeight);
window.addEventListener("orientationchange", syncAppHeight);
// Also re-pin on wake/visibility regain — a toolbar state change while hidden
// (e.g. returning from another tab/app) won't have fired a resize.
window.addEventListener("pageshow", syncAppHeight);

/* ----------------------------------------------------------------------------
   DEBUG overlay (toggled by typing DEBUG in Search). A diagnostic readout of
   the competing viewport-height numbers, pinned over everything, updated on
   every viewport event. In the "stuck high / gap below tab bar" state, the
   screenshot of these numbers shows exactly which height is diverging and
   whether visualViewport is reporting a stale value. Purely a dev aid — leaves
   no persistent state; a second DEBUG or a reload removes it.
---------------------------------------------------------------------------- */
let _dbgEl = null;
let _dbgTick = null;
function updateDebugOverlay() {
  if (!_dbgEl) return;
  const vv = window.visualViewport;
  const body = document.body.getBoundingClientRect();
  const tab = document.getElementById("tabbar");
  const tabRect = tab ? tab.getBoundingClientRect() : null;
  const appH = getComputedStyle(document.documentElement)
                 .getPropertyValue("--app-h").trim() || "(unset)";
  const layoutH = document.documentElement.clientHeight;
  // The meaningful number is how far the bottom of the app shell (≈ body
  // bottom / tabbar bottom) sits BELOW the layout-viewport fold. tabbar.bottom
  // is in layout-viewport coords, so compare to layoutH (clientHeight), NOT
  // visualViewport.height — those are different coordinate spaces and mixing
  // them gave a misleading 0 before. Positive => shell overflows below the
  // fold (the zoomable black band under the bar).
  const overflow = tabRect ? Math.round(tabRect.bottom - layoutH) : "n/a";
  // Read the resolved safe-area insets (and the derived --tab-h) so we can see
  // whether a large env(safe-area-inset-bottom) is padding the tab bar into
  // dead space.
  const cs = getComputedStyle(document.documentElement);
  const safeB = cs.getPropertyValue("--safe-bottom").trim() || "?";
  const safeT = cs.getPropertyValue("--safe-top").trim() || "?";
  const tabH = cs.getPropertyValue("--tab-h").trim() || "?";
  // Per-element vertical bands: top→bottom of each app-shell child. A gap shows
  // up as a jump between one element's bottom and the next's top.
  const band = (id) => {
    const e = document.getElementById(id);
    if (!e) return `${id}: (none)`;
    const r = e.getBoundingClientRect();
    const disp = getComputedStyle(e).display;
    if (disp === "none" || r.height === 0) return `${id}: hidden`;
    return `${id}: ${Math.round(r.top)}→${Math.round(r.bottom)} (h${Math.round(r.height)})`;
  };
  _dbgEl.textContent =
    `visualViewport.h: ${vv ? Math.round(vv.height) : "n/a"}\n` +
    `documentElement.clientHeight: ${layoutH}\n` +
    `body: ${Math.round(body.top)}→${Math.round(body.bottom)} (h${Math.round(body.height)})\n` +
    `--app-h: ${appH}\n` +
    `${band("topbar")}\n` +
    `${band("scroll-indicator")}\n` +
    `${band("content")}\n` +
    `${band("bottom-controls")}\n` +
    `${band("tabbar")}\n` +
    `--tab-h: ${tabH} | safe-top: ${safeT} | safe-bot: ${safeB}\n` +
    `OVERFLOW below fold: ${overflow}\n` +
    `wide: ${isWide()}`;
}
function toggleDebugOverlay() {
  if (_dbgEl) {
    _dbgEl.remove();
    _dbgEl = null;
    if (_dbgTick) { clearInterval(_dbgTick); _dbgTick = null; }
    if (window.visualViewport) {
      window.visualViewport.removeEventListener("resize", updateDebugOverlay);
      window.visualViewport.removeEventListener("scroll", updateDebugOverlay);
    }
    window.removeEventListener("resize", updateDebugOverlay);
    return false;
  }
  _dbgEl = document.createElement("pre");
  // Inline styles so it needs no CSS rule; fixed + max z-index so it floats
  // above all chrome. Pointer-events:none so it never intercepts taps.
  _dbgEl.style.cssText =
    "position:fixed;top:0;left:0;z-index:99999;margin:0;padding:6px 8px;" +
    "font:11px/1.35 ui-monospace,Menlo,Consolas,monospace;white-space:pre;" +
    "background:rgba(0,0,0,.82);color:#0f0;pointer-events:none;" +
    "border-bottom-right-radius:8px;max-width:80vw;";
  document.body.appendChild(_dbgEl);
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", updateDebugOverlay);
    window.visualViewport.addEventListener("scroll", updateDebugOverlay);
  }
  window.addEventListener("resize", updateDebugOverlay);
  // Also refresh on a steady tick, since some toolbar transitions settle
  // without firing a final event — this keeps the readout current to ~250ms.
  _dbgTick = setInterval(updateDebugOverlay, 250);
  updateDebugOverlay();
  return true;
}

// the Me-pane connector tree on any width change while wide, since the
// SVG geometry depends on the pane's width. A debounce keeps drags cheap.
let _wasWide = isWide();
let _resizeTimer = null;
window.addEventListener("resize", () => {
  const nowWide = isWide();
  if (nowWide !== _wasWide) {
    _wasWide = nowWide;
    applyMeWidth();                // re-clamp/apply pane width for new vw
    render();                      // full re-render across the breakpoint
  } else {
    // Width changed without crossing the breakpoint: re-evaluate the bits
    // whose fit depends on width — the "Show me:" label, the "My Schedule"/
    // "Me" titles, and the date/time indicator's date length.
    fitBottomControls();
    if (state.activeTab === "me" && currentTopView() === "list") {
      fitTitle($("#page-title"), "My Schedule", "Me");
    }
    fitTitle($(".me-pane-title"), "My Schedule", "Me");
    updateScrollIndicator();
  }
  // Width-dependent connector redraws are handled by the other (debounced)
  // resize listener above; nothing more to do here.
}, { passive: true });

applyMeWidth();
applyFontScale();   // restore the saved text-size multiplier before first paint
initMeResize();
initMePaneScroll();
render();
seedBackHistory();  // mirror any restored drill-in depth into browser history

/* Keep the visible list's time-derived state ("Now" marker, and items
   aging out as they end when Show past is OFF) accurate — WITHOUT a blind
   once-a-minute redraw.

   The program is fixed and fully known up front, so the list's time-derived
   state only changes at discrete instants: when an item STARTS (upcoming→now)
   or ENDS (now→past, and falls off the list when Show past is OFF). Both kinds
   of instant also move the "Now" marker. So instead of polling every 60 s
   (which redrew even when nothing had changed — and every redraw tears down
   and rebuilds the Me-pane connector SVG, so the connectors visibly flicker),
   we wake only AT those boundaries. Each wake-up coincides with a real state
   change, so a redraw there is warranted rather than gratuitous.

   The redraw body below is byte-for-byte the old interval's: same typing/focus
   guard, same "top-level list views only" gate, same full render(). Only the
   SCHEDULING changed (boundary timeouts instead of a fixed interval); the
   render mechanism is untouched. */

/* All distinct session/talk boundary instants (start and end), ms epoch,
   ascending. Computed once — the program never changes during a session.
   Mirrors the CONFERENCE_END_MS idiom above (same parse + isNaN guard). */
const TIME_BOUNDARIES_MS = (() => {
  const set = new Set();
  for (const x of [...DATA.sessions, ...DATA.talks]) {
    for (const v of [x.start_ts, x.end_ts]) {
      if (!v) continue;
      const t = new Date(v).getTime();
      if (!isNaN(t)) set.add(t);
    }
  }
  return [...set].sort((a, b) => a - b);
})();

// setTimeout delays are clamped by browsers to a signed 32-bit ms value
// (~24.8 days); a delay past that fires almost immediately. For a multi-day
// program the later boundaries exceed it, so we cap each scheduled delay and
// simply re-arm when the intermediate wake-up fires. Well under the 2^31-1 ms
// limit, with headroom.
const MAX_TIMEOUT_MS = 12 * 60 * 60 * 1000;   // 12 h

let _tickTimer = null;

function refreshTimeState() {
  // Don't re-render while the user is typing. render() rebuilds the list
  // DOM wholesale; if focus is in an input that lives inside the list view
  // (e.g. the Search box), the focused node gets destroyed and replaced,
  // dropping the caret mid-keystroke. Skip when an editable element is
  // focused — the next boundary (or the visibility re-arm) catches up.
  const ae = document.activeElement;
  if (ae && (ae.tagName === "INPUT" || ae.tagName === "TEXTAREA" ||
             ae.isContentEditable)) return;

  const tab = state.activeTab;
  const stack = state.tabStacks[tab];
  const top = stack[stack.length - 1];
  if (top && top.view === "list") render();
}

/* Arm a single timeout for the next future boundary (capped at
   MAX_TIMEOUT_MS). On fire: redraw if a boundary has actually passed, then
   re-arm for the one after. Idempotent — safe to call repeatedly (e.g. from
   the visibility/focus re-arm); it always clears any pending timer first.

   NOTE: this schedules off the wall clock read at arm time. A system-clock
   jump while the page stays foregrounded (e.g. manual clock change, NTP step)
   isn't actively watched — we'd rather not reintroduce a poll for that rare
   case. The visibility/pageshow/focus re-arm below recomputes "now" and
   redraws on the common trigger for clock changes (travel: unlock the device
   after a timezone change), which covers it in practice. */
function scheduleNextTick() {
  clearTimeout(_tickTimer);
  _tickTimer = null;

  const now = Date.now();
  const next = TIME_BOUNDARIES_MS.find(t => t > now);
  if (next == null) return;   // every boundary is past — nothing left to do

  const delay = Math.min(next - now, MAX_TIMEOUT_MS);
  _tickTimer = setTimeout(() => {
    // If this fire is a real boundary (not just a MAX_TIMEOUT_MS re-arm
    // wake-up), the time state changed — redraw. The find() above guarantees
    // `next` was the soonest boundary; if we've reached it, refresh.
    if (Date.now() >= next) refreshTimeState();
    scheduleNextTick();        // arm for the following boundary
  }, delay);
}

/* Coming back from background/sleep: a single queued timeout can't fire for
   each boundary the device slept through, so on wake we redraw (catching up
   whatever crossed while hidden) and re-arm from the current clock. Covers
   throttled/suspended timers and clock changes that land while we were away. */
function rearmOnWake() {
  if (document.visibilityState === "hidden") return;
  refreshTimeState();
  scheduleNextTick();
}
document.addEventListener("visibilitychange", rearmOnWake);
window.addEventListener("pageshow", rearmOnWake);
window.addEventListener("focus", rearmOnWake);

scheduleNextTick();
</script>

</body>
</html>
"""


def _strip_js_comments(s: str) -> str:
    """Remove // and /* */ comments from JS source while preserving comment
    markers that appear inside string literals, template literals, and regex
    literals. Char-by-char state machine: the only reliable way to tell a
    regex literal from division is to track whether the previous significant
    token expects a value, so we keep `prev` (last non-space char emitted).

    Conservative by construction: anything that isn't unambiguously a comment
    is emitted verbatim, so worst case we keep a comment, never delete code.
    """
    out = []
    i, n = 0, len(s)
    prev = ""   # last significant (non-space) char, for regex-vs-divide
    while i < n:
        c = s[i]
        nxt = s[i + 1] if i + 1 < n else ""
        # string / template literals — copy through untouched
        if c in "\"'`":
            q = c
            out.append(c)
            i += 1
            while i < n:
                out.append(s[i])
                if s[i] == "\\":            # escape: copy next char too
                    if i + 1 < n:
                        out.append(s[i + 1])
                    i += 2
                    continue
                if s[i] == q:
                    i += 1
                    break
                i += 1
            prev = q
            continue
        # line comment — drop to end of line (keep the newline)
        if c == "/" and nxt == "/":
            j = s.find("\n", i)
            if j == -1:
                j = n
            i = j
            continue
        # block comment. A /*! ... */ comment is a PRESERVED license/legal
        # comment (the convention real minifiers honor) — copy it through
        # verbatim. Any other /* ... */ is dropped through its closing */.
        if c == "/" and nxt == "*":
            j = s.find("*/", i + 2)
            end = n if j == -1 else j + 2
            if i + 2 < n and s[i + 2] == "!":      # /*! preserved banner
                out.append(s[i:end])
                prev = "/"
            i = end
            continue
        # regex literal — a '/' where a value is expected. Copy through to its
        # closing '/', honoring char classes ([...] may contain an unescaped /)
        if c == "/" and prev in "(,=:[!&|?{};":
            out.append(c)
            i += 1
            in_class = False
            while i < n:
                out.append(s[i])
                if s[i] == "\\":
                    if i + 1 < n:
                        out.append(s[i + 1])
                    i += 2
                    continue
                if s[i] == "[":
                    in_class = True
                elif s[i] == "]":
                    in_class = False
                elif s[i] == "/" and not in_class:
                    i += 1
                    break
                i += 1
            prev = "/"
            continue
        out.append(c)
        if not c.isspace():
            prev = c
        i += 1
    return "".join(out)


def _strip_block_comments(s: str) -> str:
    """Remove /* */ comments from CSS, preserving /*! ... */ license banners
    (same convention as the JS pass). CSS has no string/regex ambiguity that
    matters for this template, so a non-greedy removal that skips bang-comments
    is safe."""
    return re.sub(r"/\*(?!!).*?\*/", "", s, flags=re.S)


def minify_html(template: str) -> str:
    """Strip comments from the TEMPLATE (pre-data-splice). Operates on the
    three regions by their delimiters: the <script> body (JS), the <style>
    body (CSS), and HTML <!-- --> comments in the markup. Collapses the blank
    lines that stripping leaves behind, but does NOT collapse whitespace inside
    the JS/CSS beyond that — keeping line structure makes the rare production
    debug far less painful for a near-identical size win once gzipped."""

    # JS: operate only inside <script>...</script>.
    def _js(m: "re.Match[str]") -> str:
        return m.group(1) + _strip_js_comments(m.group(2)) + m.group(3)
    template = re.sub(r"(<script[^>]*>)(.*?)(</script>)", _js, template,
                      flags=re.S)

    # CSS: operate only inside <style>...</style>.
    def _css(m: "re.Match[str]") -> str:
        return m.group(1) + _strip_block_comments(m.group(2)) + m.group(3)
    template = re.sub(r"(<style[^>]*>)(.*?)(</style>)", _css, template,
                      flags=re.S)

    # HTML comments in the markup. Guard against accidentally matching the JS
    # hack-comment idiom: there are none here, and <script>/<style> bodies were
    # already processed above, so a plain removal across the whole doc is fine
    # for the remaining markup-level <!-- --> only. To avoid touching anything
    # inside the (already comment-stripped) script/style, we only remove HTML
    # comments that aren't within those — but since JS/CSS comments are gone,
    # any surviving <!-- --> is genuine markup. Safe to remove globally.
    template = re.sub(r"<!--.*?-->", "", template, flags=re.S)

    # Collapse runs of blank lines left by stripping.
    template = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", template)
    return template


def main() -> None:
    conference_name = (_DATA.get("conference_name") or "").strip() or "Conference"
    print(f"[title] conference name: {conference_name!r}")
    print(f"[load] {len(_DATA.get('sessions', []))} sessions, "
          f"{len(_DATA.get('talks', []))} talks from data JSON.")

    data = enrich_affiliations(_DATA)

    # Drop fields the frontend never reads before serializing into the HTML.
    # These are carried through the input JSON (and used by the Python side
    # during enrichment) but the app's JS does not access them, so shipping
    # them just inflates the payload. Done with a shallow copy so _DATA itself
    # is untouched.
    #   - "affiliation_sources": only consumed Python-side by
    #     build_affiliation_map.py to learn short labels; already used by the
    #     time we get here, and the JS never references DATA.affiliation_sources.
    #   - talks[].presenter: normalized by normalize_names_in_data for
    #     cleanliness but the app renders "speaker" (and authors), never
    #     "presenter". Verified: no JS path reads t.presenter.
    #   - talks[].number: not referenced from JS anywhere (the visible "talk
    #     number" in the top bar comes from item.id via pageTitleFor).
    #   - talks[].institutions_may_dedup: a build-time hint consumed once by
    #     enrich_affiliations to decide whether to collapse duplicate
    #     institutions. It has done its job by this point.
    data = dict(data)
    data.pop("affiliation_sources", None)
    _DROP_TALK_FIELDS = ("presenter", "number", "institutions_may_dedup")
    if data.get("talks"):
        data["talks"] = [
            {k: v for k, v in t.items() if k not in _DROP_TALK_FIELDS}
            for t in data["talks"]
        ]

    json_blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    # Build the DATA initializer + the optional decoder block.
    #   COMPRESS_DATA on : DATA is a base64 raw-DEFLATE payload, inflated once
    #                      at startup by the vendored synchronous tiny-inflate.
    #   COMPRESS_DATA off: DATA is the plain JSON literal (readable/debuggable).
    # The decoder block is spliced in BEFORE minify (so its own comments get
    # stripped with everything else); the DATA payload is spliced in AFTER
    # minify (so the stripper never scans conference data). When compressing,
    # the payload is pure base64 [A-Za-z0-9+/=] — no "</" can occur — so the
    # </script> escaping the literal path needs is unnecessary there.
    if COMPRESS_DATA:
        raw = json_blob.encode("utf-8")
        co = zlib.compressobj(9, zlib.DEFLATED, -15)   # -15 => raw DEFLATE
        comp = co.compress(raw) + co.flush()
        b64 = base64.b64encode(comp).decode("ascii")
        data_init = f'__decodeData("{b64}", {len(raw)})'
        decoder_block = DECODER_BLOCK + "\n\n"
        print(f"[compress] DATA {len(raw):,} -> {len(b64):,} b64 bytes "
              f"({100 * len(b64) / len(raw):.1f}% of original).")
    else:
        # Plain literal. Escape "</" so an embedded "</script>" in the data
        # can't terminate the script element early.
        data_init = json_blob.replace("</", r"<\/")
        decoder_block = ""

    template = HTML_TEMPLATE.replace("__DECODER_BLOCK__", decoder_block)

    # Minify the TEMPLATE (comments only), before the DATA payload is spliced
    # in, so the comment stripper never sees the conference data and only the
    # tiny placeholder tokens remain to fill. No-op when MINIFY is False.
    if MINIFY:
        before = len(template)
        template = minify_html(template)
        print(f"[minify] template {before:,} -> {len(template):,} bytes "
              f"({100 * (before - len(template)) / before:.1f}% smaller).")

    # Resolve the wide/narrow breakpoint placeholders, then splice DATA last.
    html = (template
            .replace("__WIDE_QUERY__", WIDE_QUERY)
            .replace("__NARROW_QUERY__", NARROW_QUERY))
    html = html.replace("__DATA_INIT__", data_init)
    safe_name = (conference_name.replace("&", "&amp;")
                                 .replace("<", "&lt;")
                                 .replace(">", "&gt;"))
    html = html.replace("__CONFERENCE_NAME__", safe_name)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    size_kb = OUTPUT_HTML.stat().st_size / 1024
    print(f"[write] {OUTPUT_HTML} ({size_kb:,.1f} KB)")


if __name__ == "__main__":
    main()