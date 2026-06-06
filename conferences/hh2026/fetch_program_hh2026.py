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

"""fetch_program_hh2026.py — DOWNLOAD ONLY.

The "downloader" half of the conference pipeline. The entire conference
program is published as a single public PDF — the Final Program — linked
from the conference site:

    https://www.hh2026.org/files/HiltonHead2026_Program.pdf

That one PDF is the authoritative, self-contained source for everything the app
needs: the Special Events descriptions, the Sunday-through-Thursday day-by-day
schedule, and the three Poster Presentation sessions. There is no conference
planner, CSV export, or per-talk abstract book to reconcile, so this fetcher
simply downloads that PDF into data/ and the processor
(process_program_hh2026.py) runs entirely offline against it.

The URL carries no rolling date suffix, so it is fetched directly. Contacts the
network only via urllib.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"

PROGRAM_URL = "https://www.hh2026.org/files/HiltonHead2026_Program.pdf"
PROGRAM_NAME = "HiltonHead2026_Program.pdf"

# Polite UA — some servers 403 the default urllib UA.
UA = "Mozilla/5.0 (hh2026-fetch; fine-conference-app)"


def _fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


def main() -> None:
    print("=" * 72)
    print("[config] CONFERENCE DOWNLOADER starting up.")
    print(f"[config]   script dir : {SCRIPT_DIR}")
    print(f"[config]   data dir   : {DATA_DIR}")
    print(f"[config]   program URL: {PROGRAM_URL}")
    print(f"[config]   run date   : {date.today().isoformat()}")
    print("=" * 72)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / PROGRAM_NAME

    print(f"[info] downloading final program PDF from {PROGRAM_URL}")
    try:
        body = _fetch_bytes(PROGRAM_URL)
    except urllib.error.URLError as e:
        print(f"[fatal] could not download {PROGRAM_URL}: {e}")
        print("        See data_requirements_hh2026.txt for the manual "
              "download fallback.")
        sys.exit(1)

    # Sanity check: a real PDF starts with "%PDF" and is more than a stub.
    if not body[:4] == b"%PDF" or len(body) < 100_000:
        print(f"[fatal] downloaded {len(body):,} bytes but it does not look "
              "like the program PDF (expected a multi-MB %PDF file). The link "
              "may have moved; see data_requirements_hh2026.txt.")
        sys.exit(1)

    target.write_bytes(body)
    size_mb = target.stat().st_size / (1024 * 1024)
    print(f"[ok]   saved {target.name} ({size_mb:,.1f} MB).")

    print()
    print("=" * 72)
    print("DONE (downloaded program PDF). Next: run process_program_hh2026.py")
    print(f"  data dir : {DATA_DIR}")
    print("=" * 72)


if __name__ == "__main__":
    main()
