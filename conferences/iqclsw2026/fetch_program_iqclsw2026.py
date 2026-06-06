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

"""fetch_program_iqclsw2026.py — download the detailed program.

The program lives on ONE static CMS-hosted page:

    https://iqclsw2026.com/index.php/detailed-program/

The whole program (every day, every talk, the full poster list) is rendered
into that single page, so unlike a paginated event-site schedule there is
nothing to click through — we just need the page as a browser would see it.

The site rejects plain HTTP clients (it answers a bare `requests.get` with
HTTP 403), so we drive a real headless Chromium via Playwright, wait for the
content to settle, and save TWO artifacts into data/:

  - detailed_program.html : the page's full rendered HTML (kept for archival /
    re-parsing if the text extraction ever needs to change), and
  - detailed_program.txt  : the visible text of the program region only
    (`innerText` of the main content container). This is the file the processor
    actually parses — it is far more stable than the CMS-hosted page markup,
    whose wrapper class names churn between theme updates.

Both files are listed in data_requirements_iqclsw2026.txt; the processor only
requires the .txt, but the .html is saved alongside so the raw source is never
lost.

This script re-spawns itself in a clean child process (run_in_subprocess) the
first time it is run, because Playwright's sync API refuses to start inside an
existing asyncio event loop — make_app.py imports/exec's a lot of modules, and
running the browser in a pristine interpreter avoids any such collision.

Run directly:  python fetch_program_iqclsw2026.py
(or let make_app.py run it for you).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROGRAM_URL = "https://iqclsw2026.com/index.php/detailed-program/"
OVERVIEW_URL = "https://iqclsw2026.com/index.php/program-overview/"

# data/ lives next to this script (the conference subdirectory layout that
# make_app.py expects). The downloader writes here; the processor reads here.
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"

# We save ONLY the raw HTML of each page — it is the source of record. The
# processor extracts the text it needs from these HTML files (via lxml), so
# there is no derived .txt to keep in sync.
HTML_OUT = DATA_DIR / "detailed_program.html"
# The "Program at a Glance" overview page carries the per-session NAMES (the
# short topic labels) that the detailed program lacks.
OVERVIEW_HTML_OUT = DATA_DIR / "program_overview.html"

# A normal desktop UA; the default headless Playwright UA is sometimes enough,
# but a realistic one is safest against the same filter that 403s requests.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Marker text that must appear in each saved page, used as a sanity check that
# the right page rendered (and as the anchor the processor's extractor keys on).
MARKER_TEXT = "DETAILED PROGRAM"
# The overview page's marker (its heading is "PROGRAM AT A GLANCE").
OVERVIEW_MARKER_TEXT = "PROGRAM AT A GLANCE"


def run_in_subprocess() -> bool:
    """Re-exec this script once in a clean child process.

    Playwright's sync API cannot run inside an already-running asyncio loop.
    Re-spawning a pristine interpreter sidesteps any loop a parent (e.g.
    make_app.py's import machinery) may have left around. Returns True in the
    PARENT (which has just waited on the child and should now return), False in
    the freshly-spawned CHILD (which should do the real work).
    """
    if os.environ.get("FETCH_CHILD") == "1":
        return False  # we ARE the child — proceed with the download.
    import subprocess

    env = dict(os.environ, FETCH_CHILD="1", PYTHONUNBUFFERED="1")
    print("[fetch] re-spawning a clean child process for Playwright …",
          flush=True)
    rc = subprocess.call([sys.executable, str(Path(__file__).resolve())],
                         env=env)
    if rc != 0:
        # Mirror the child's failure as our own exit code so make_app.py sees it.
        raise SystemExit(rc)
    return True


def _download_page(context, url: str, marker: str,
                   html_out: Path, *, required: bool) -> None:
    """Navigate to `url` and save its full rendered HTML. The HTML is the
    source of record; the processor extracts the text it needs from it. We
    sanity-check that the rendered HTML actually contains `marker` (so a blocked
    or changed page fails loudly for required pages, or warns for optional
    ones)."""
    page = context.new_page()
    print(f"[fetch] navigating to {url}", flush=True)
    page.goto(url, wait_until="networkidle", timeout=90_000)
    try:
        page.wait_for_selector(f"text={marker}", timeout=20_000)
    except Exception:
        print(f"[fetch] note: marker {marker!r} not found via selector wait; "
              "continuing to save the page anyway.", flush=True)
    html = page.content()
    page.close()

    html_out.write_text(html, encoding="utf-8")
    print(f"[fetch] wrote {html_out.name} ({len(html):,} bytes)", flush=True)

    if marker.lower() not in html.lower():
        msg = (f"the saved HTML does not contain {marker!r}. The page may have "
               "changed, failed to load, or been blocked. Inspect "
               f"{html_out.name} in data/.")
        if required:
            print(f"[fetch] ERROR: {msg}", flush=True)
            raise SystemExit(1)
        print(f"[fetch] WARNING: {msg} (optional page — continuing.)",
              flush=True)


def fetch() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[fetch] ERROR: Playwright is not installed. Install it with:",
              flush=True)
        print("            pip install playwright", flush=True)
        print("            python -m playwright install chromium", flush=True)
        raise SystemExit(2)

    print(f"[fetch] launching headless Chromium …", flush=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
            viewport={"width": 1366, "height": 2200},
        )
        # The detailed program is REQUIRED (the processor can't build without
        # it). The overview is OPTIONAL — it only supplies session names, so a
        # failure there degrades gracefully (sessions keep their generic
        # "School N"/"Workshop N" titles).
        _download_page(context, PROGRAM_URL, MARKER_TEXT,
                       HTML_OUT, required=True)
        _download_page(context, OVERVIEW_URL, OVERVIEW_MARKER_TEXT,
                       OVERVIEW_HTML_OUT, required=False)
        browser.close()

    print("[fetch] done.", flush=True)


def main() -> None:
    # Parent path: spawn the child, wait, return. Child path: do the work.
    if run_in_subprocess():
        return
    fetch()


if __name__ == "__main__":
    main()
