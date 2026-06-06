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

"""fetch_program_spiepw2025.py — DOWNLOAD ONLY.

The "downloader" half of the pipeline. The entire technical program is published
by the conference as a single PDF, the "Technical Program" book (session-by-session
schedule with every talk's number, time, title, and author list — no abstracts).
We download that one PDF into data/ as

    PW25-Technical-Program.pdf

where process_program_spiepw2025.py reads it entirely offline.

The live conference URL for the program may eventually be retired, so we point
at a stable Internet Archive (Wayback Machine) snapshot of it and use the raw
("id_") modifier so we get the PDF bytes rather than the Wayback viewer page.
If the snapshot ever disappears, see data_requirements_spiepw2025.txt for the
manual fallback (download the program PDF by hand into data/).

Contacts the network only; launches no browser. The processor runs entirely
offline against what we save here.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"

PDF_NAME = "PW25-Technical-Program.pdf"

# Stable Wayback snapshot of the conference's Technical Program PDF. The "id_"
# modifier after the timestamp asks the Wayback Machine for the ORIGINAL bytes
# (the PDF) rather than its rewritten viewer page.
PDF_URL = (
    "https://web.archive.org/web/20260529103038id_/"
    "https://www.spie.org/documents/ConferencesExhibitions/Programs/2025/"
    "PW25-Technical-Program.pdf"
)

UA = "Mozilla/5.0 (spiepw2025-fetch; fine-conference-app)"


def _fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read()


def main() -> None:
    print("=" * 72)
    print("[config] conference program DOWNLOADER starting up.")
    print(f"[config]   script dir : {SCRIPT_DIR}")
    print(f"[config]   data dir   : {DATA_DIR}")
    print(f"[config]   pdf url    : {PDF_URL}")
    print("=" * 72)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / PDF_NAME

    print(f"[info] downloading technical program PDF …")
    try:
        body = _fetch_bytes(PDF_URL)
    except urllib.error.URLError as e:
        print(f"[fatal] could not download {PDF_URL}: {e}")
        print("[fatal] See data_requirements_spiepw2025.txt for the manual "
              "fallback (download the program PDF by hand into data/).")
        sys.exit(1)

    if body[:4] != b"%PDF":
        print(f"[fatal] downloaded {len(body):,} bytes but it is not a PDF "
              f"(starts with {body[:16]!r}).")
        print("[fatal] The Wayback snapshot may have changed; see "
              "data_requirements_spiepw2025.txt for the manual fallback.")
        sys.exit(1)

    target.write_bytes(body)
    size_mb = target.stat().st_size / (1024 * 1024)
    print(f"[ok] saved {PDF_NAME} into data/ ({size_mb:,.1f} MB).")
    print()
    print("=" * 72)
    print("DONE. Next: run process_program_spiepw2025.py")
    print(f"  data dir   : {DATA_DIR}")
    print(f"  staged PDF : {target}")
    print("=" * 72)


if __name__ == "__main__":
    main()
