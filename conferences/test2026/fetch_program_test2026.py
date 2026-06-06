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
fetch_program_test2026.py — DOWNLOAD ONLY.

TEST 2026 is a SYNTHETIC example conference. Unlike the real conferences in this
repository, it has no live website to scrape: its one and only
source file is a fixed, synthetic abstract book that ships WITH the repository.

That source PDF lives next to this script as

    TEST2026_Program_Abstracts.pdf

(i.e. in the conference directory itself, NOT in data/). The data/ subdirectory
is git-ignored and starts out empty on a fresh checkout, so the processor — which
reads from data/ — has nothing to read until this downloader runs.

So this "downloader" does the one thing it needs to: it COPIES the committed
source PDF into data/ as

    data/TEST2026_Program_Abstracts.pdf

That is all. It contacts no network and launches no browser. It mirrors the role
the real downloaders play (populate data/ with the raw inputs the processor
reads) without any of the scraping machinery.

make_app.py runs this whenever a required input is missing from data/ (see
data_requirements_test2026.txt); on a fresh checkout that is exactly once, to
stage the PDF. Run the companion process_program_test2026.py afterwards to turn
the staged PDF into conference_data.json.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"

PDF_NAME = "TEST2026_Program_Abstracts.pdf"
SOURCE_PDF = SCRIPT_DIR / PDF_NAME       # committed, next to this script
TARGET_PDF = DATA_DIR / PDF_NAME         # where the processor reads it


def main() -> None:
    print("=" * 72)
    print("[config] TEST 2026 DOWNLOADER starting up.")
    print(f"[config]   script dir : {SCRIPT_DIR}")
    print(f"[config]   data dir   : {DATA_DIR}")
    print(f"[config]   source PDF : {SOURCE_PDF}")
    print(f"[config]   target PDF : {TARGET_PDF}")
    print("=" * 72)
    print("[info] TEST 2026 is a synthetic example; there is nothing to "
          "download from the web.")
    print("[info] Staging the committed source PDF into data/ …")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not SOURCE_PDF.exists():
        print(f"[fatal] Committed source PDF not found next to this script: "
              f"{SOURCE_PDF}")
        print("[fatal] It is supposed to ship with the repository. Without it "
              "there is nothing to stage.")
        sys.exit(1)

    shutil.copyfile(SOURCE_PDF, TARGET_PDF)
    size_kb = TARGET_PDF.stat().st_size / 1024
    print(f"[ok] copied {PDF_NAME} into data/ ({size_kb:,.1f} KB).")
    print()
    print("=" * 72)
    print("DONE (staged the source PDF). Next: run process_program_test2026.py")
    print(f"  data dir   : {DATA_DIR}")
    print(f"  staged PDF : {TARGET_PDF}")
    print("=" * 72)


if __name__ == "__main__":
    main()
