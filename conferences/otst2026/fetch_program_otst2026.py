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

"""fetch_program_otst2026.py — download the conference program sources.

The conference publishes a small static HTML site. Everything the
app needs comes from three files on it:

  - assets/Full_Program_OTST_2026.pdf  (REQUIRED) — the full day-by-day program;
    the processor's source of record.
  - program.html        (optional) — tutorial abstracts + poster-board logistics.
  - directions.html     (optional) — the local-excursions list.

The site is served over plain HTTP and presents an INVALID HTTPS certificate
(the hostname does not match), so we deliberately fetch the http:// URLs with a
plain urllib client (a normal browser User-Agent is enough — the server does not
filter non-browser clients). No JavaScript renders the content, so there is no
need for a headless browser here.

Run directly:  python fetch_program_otst2026.py
(or let make_app.py run it for you).
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

BASE_URL = "http://www.otst2026.org/"

# data/ lives next to this script (the conference subdirectory layout that
# make_app.py expects). The downloader writes here; the processor reads here.
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"

# (url path, output filename, required) — the program PDF is the source of
# record; the two HTML pages are optional enrichment.
SOURCES = [
    ("assets/Full_Program_OTST_2026.pdf", "Full_Program_OTST_2026.pdf", True),
    ("program.html", "program.html", False),
    ("directions.html", "directions.html", False),
]

# A normal desktop UA. The server is happy with anything, but a realistic one is
# the safest default.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _download(url: str, out: Path, *, required: bool) -> bool:
    """Fetch `url` into `out`. Returns True on success. A failed REQUIRED file
    aborts the run; a failed optional file warns and continues."""
    print(f"[fetch] downloading {url}", flush=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
    except Exception as e:  # network error, 404, etc.
        msg = f"could not download {url}: {e}"
        if required:
            print(f"[fetch] ERROR: {msg}", flush=True)
            raise SystemExit(1)
        print(f"[fetch] WARNING: {msg} (optional — continuing.)", flush=True)
        return False

    if not data:
        msg = f"{url} returned an empty response."
        if required:
            print(f"[fetch] ERROR: {msg}", flush=True)
            raise SystemExit(1)
        print(f"[fetch] WARNING: {msg} (optional — continuing.)", flush=True)
        return False

    out.write_bytes(data)
    print(f"[fetch] wrote {out.name} ({len(data):,} bytes)", flush=True)
    return True


def fetch() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for path, name, required in SOURCES:
        _download(BASE_URL + path, DATA_DIR / name, required=required)
    print("[fetch] done.", flush=True)


if __name__ == "__main__":
    fetch()
