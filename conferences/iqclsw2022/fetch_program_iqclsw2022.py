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

"""fetch_program_iqclsw2022.py — DOWNLOAD ONLY.

The "downloader" half of the pipeline. The conference program is
published on the conference website as three static artifacts:

    Program_IQCLSW2022.pdf   the full program (cover + day-by-day schedule +
                             numbered poster catalog), as a multi-page PDF.
    PosterSession.html       the public poster-session page (a single HTML table
                             with N°, Title, Authors columns).
    TutorialSpeakers.html    the public "Keynote, Invited and Tutorial speakers"
                             page (a single HTML table with Name, Affiliation,
                             Type, Title columns including country flag emoji).

The PDF is the authoritative source for the SCHEDULE (every day, every time
slot, every chair, every talk title and speaker initial+surname). The two HTML
pages enrich what the PDF prints:

  - TutorialSpeakers.html lets us recover FULL speaker names (the PDF only
    prints "A. Smith" / "B. Jones" style initials+surnames) and an
    AFFILIATION for every keynote/invited/tutorial talk (the PDF prints no
    affiliations at all).
  - PosterSession.html provides cleaner title/author separation for the
    posters, but it numbers them slightly differently from the PDF (the HTML
    has gaps at 7 and 23 and goes up to 34); the PDF's contiguous 1-32 list
    remains the canonical source.

The PDF URL on the conference site carries the upload date in its filename
(.../2022/08/Program_IQCLSW2022_LIN_chair20220822.pdf), which would change if
the organisers ever re-publish — but unlike a rolling annual programme, the
program is now archival, so the URL is stable. We hard-code it; if it
ever 404s the data_requirements manifest carries the manual-download fallback.

Contacts the network only; launches no browser. The processor
(process_program_iqclsw2022.py) runs entirely offline against what we save here.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"

# Each artifact is downloaded from a fixed URL into data/ under the filename
# the processor and the requirements manifest both expect.
ARTIFACTS = [
    {
        "name": "Program_IQCLSW2022.pdf",
        "url":  "https://iqclsw.phys.ethz.ch/wp-content/uploads/2022/08/"
                "Program_IQCLSW2022_LIN_chair20220822.pdf",
        "desc": "full program PDF",
    },
    {
        "name": "PosterSession.html",
        "url":  "https://iqclsw.phys.ethz.ch/poster-session/",
        "desc": "poster-session page",
    },
    {
        "name": "TutorialSpeakers.html",
        "url":  "https://iqclsw.phys.ethz.ch/tutorial-speakers/",
        "desc": "keynote/invited/tutorial speakers page",
    },
]

# Polite UA — some CMS-hosted sites 403 the default urllib UA string.
UA = "Mozilla/5.0 (iqclsw2022-fetch; fine-conference-app)"


def _fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def main() -> None:
    print("=" * 72)
    print("[config] conference program DOWNLOADER starting up.")
    print(f"[config]   script dir : {SCRIPT_DIR}")
    print(f"[config]   data dir   : {DATA_DIR}")
    print("=" * 72)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    saved_any = False
    failed: list[str] = []
    for art in ARTIFACTS:
        target = DATA_DIR / art["name"]
        print(f"[info] downloading {art['desc']} from {art['url']}")
        try:
            body = _fetch_bytes(art["url"])
        except urllib.error.URLError as e:
            print(f"[warn]   download failed: {e}")
            failed.append(art["name"])
            continue
        target.write_bytes(body)
        size_kb = target.stat().st_size / 1024
        print(f"[ok]   saved {target.name} ({size_kb:,.1f} KB).")
        saved_any = True

    print()
    print("=" * 72)
    if failed:
        print(f"DONE WITH WARNINGS — {len(failed)} file(s) not retrieved:")
        for n in failed:
            print(f"  - {n}")
        print("See data_requirements_iqclsw2022.txt for the manual fallback.")
    else:
        print("DONE. Next: run process_program_iqclsw2022.py")
    print(f"  data dir : {DATA_DIR}")
    print("=" * 72)
    if not saved_any:
        sys.exit(1)


if __name__ == "__main__":
    main()
