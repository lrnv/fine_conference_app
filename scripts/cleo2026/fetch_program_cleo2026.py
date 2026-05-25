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
fetch_program_cleo2026.py — DOWNLOAD ONLY.

The "downloader" half of the CLEO 2026 pipeline: it does nothing but DOWNLOAD
the raw CLEO 2026 source material and save it to disk. It performs NO parsing
and produces NO conference_data.json. Run the companion
process_program_cleo2026.py afterwards to turn what this saves into the final
conference_data.json.

What it downloads / saves (all into a data/ subdirectory next to this script):

  1. CLEO2026_Program_Abstracts.pdf    — the official "Program + Abstracts (PDF)"
  2. CLEO2026_Program_Abstracts.csv    — the official "Program + Abstracts (Excel)"
                                          button (it actually serves a CSV).
  3. CLEO2026_planner_expanded.html    — the planner.jsp page DOM (outerHTML)
                                          captured AFTER every DAY row, session
                                          row, and "See More…" link has been
                                          expanded. This is the reproducibility
                                          anchor the processor re-parses offline.
  4. CLEO2026_short_courses.html       — the public CLEO short-courses page
                                          (raw HTML), which lists each short
                                          course's instructor and affiliation.

How it works
------------
1. Bootstraps Playwright + Chromium on first run.
2. Spawns a clean Python subprocess so Playwright's sync API doesn't fight
   Spyder / IPython's asyncio loop.
3. Opens a Chromium window at the CLEO 2026 planner with a persistent profile
   in .chrome_profile/ next to this script. Headless by default; flip the
   module-level HEADLESS flag to False to watch it run in a visible window.
4. Auto-waits for the planner's program to render (no manual ENTER needed).
5. Clicks the two "Program + Abstracts" download buttons (PDF + Excel) and
   saves both files.
6. Clicks every '+' (DAY rows AND session rows) and every "See More…" link, in
   a stability-detecting loop, so nothing is missed, then saves the fully
   expanded planner DOM to disk.
7. Saves the public short-courses page's raw HTML.

Paths: every file this script DOWNLOADS goes in a data/ subdirectory next to
the script. Runtime artifacts (the Chromium profile and the child log) also
stay next to the script.

The heavy work runs in a re-spawned child process; its full verbose output is
both saved to fetch_child.log AND streamed live to the launching terminal.
No abstracts / detail popups are opened.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


# -----------------------------------------------------------------------------
# Tiny verbose logger — wall-clock timestamps on every line so 'silent'
# periods (e.g. while a hundreds-of-clicks batch is in flight) are obvious.
# -----------------------------------------------------------------------------
_T0 = time.monotonic()


def log(msg: str) -> None:
    print(f"[{time.monotonic() - _T0:7.1f}s] {msg}", flush=True)


# -----------------------------------------------------------------------------
# Hard-coded configuration
# -----------------------------------------------------------------------------
SCRIPT_DIR    = Path(__file__).resolve().parent
# Everything this script DOWNLOADS lives in a dedicated data/ subdirectory so it
# doesn't clutter the script directory. (The chrome profile and child log below
# are runtime artifacts, not data, so they stay next to the script.)
DATA_DIR      = SCRIPT_DIR / "data"
# Full page DOM (outerHTML) captured AFTER everything is expanded, saved for
# reproducibility so the program can be re-parsed offline by the processor
# without re-running the live browser.
OUTPUT_DOM_HTML       = DATA_DIR / "CLEO2026_planner_expanded.html"
# Raw HTML of the public short-courses page, saved so the processor can parse
# the SC<code> -> instructor/affiliation map offline.
OUTPUT_SHORTCOURSE_HTML = DATA_DIR / "CLEO2026_short_courses.html"
USER_DATA_DIR = SCRIPT_DIR / ".chrome_profile"   # persists Chromium session

# Run Chromium headless (no visible window) by default. Set to False to watch
# the download happen in a real browser window — useful for debugging or to
# watch the planner expand live.
HEADLESS = True

BROWSE_URL = "https://cleo2026.abstractcentral.com/planner.jsp"

# Public CLEO website page listing the short courses with their instructors
# and affiliations. The planner's in-app "View Event Details" popup only gives
# the instructor name (no affiliation) and is flaky to scrape, so we save this
# richer page's HTML for the processor to read.
SHORT_COURSES_URL = "https://cleoconference.org/2026-short-courses/"

# (button-label, filename-to-save-as). The "Excel" button on this site
# actually serves a CSV file, so the local name uses a .csv extension to
# match the real content. Only Program+Abstracts in both formats —
# Program Only is skipped because we want the full version.
DOWNLOAD_BUTTONS = [
    ("Program + Abstracts (PDF)",   "CLEO2026_Program_Abstracts.pdf"),
    ("Program + Abstracts (Excel)", "CLEO2026_Program_Abstracts.csv"),
]

INPUT_OFFICIAL_PDF = DATA_DIR / "CLEO2026_Program_Abstracts.pdf"
INPUT_OFFICIAL_CSV = DATA_DIR / "CLEO2026_Program_Abstracts.csv"

CHILD_FLAG = "--run-scrape"     # sentinel for the re-spawned subprocess
CHILD_LOG  = DATA_DIR / "fetch_child.log"

DATA_DIR.mkdir(parents=True, exist_ok=True)


# -----------------------------------------------------------------------------
# Bootstrap: install Playwright + Chromium if not present
# -----------------------------------------------------------------------------
def _bootstrap_playwright() -> None:
    try:
        import playwright                                   # noqa: F401
        from playwright.sync_api import sync_playwright     # noqa: F401
    except ImportError:
        print("[setup] Installing the 'playwright' Python package…")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", "playwright>=1.40"]
        )
    print("[setup] Ensuring Chromium is installed for Playwright…")
    subprocess.check_call(
        [sys.executable, "-m", "playwright", "install", "chromium"]
    )


# Only the parent runs bootstrap; the child trusts pip + the Playwright CLI
# already did their work, avoiding a silent failure inside the child console.
if CHILD_FLAG not in sys.argv:
    _bootstrap_playwright()
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # noqa: E402


# =============================================================================
# Page automation — expansion
# =============================================================================
def _pending_ids(page) -> dict:
    """One JS round-trip that returns the ids of every still-expandable
    element. Working with ids (which are unique and stable) means we never
    have to use Playwright's live nth() locator — clicking nth(0) would
    flip the first plus.gif to minus.gif and drop it out of the match set,
    which would then re-index every subsequent .nth(i) and cause the
    'every other one' bug. Using ids sidesteps that completely.

    'hourglasses' are sessions mid-load: when a SES:plus toggle is clicked,
    GWT replaces the icon with images/hourglass.png (on a SESSION:NNN <img>)
    while it fetches the talk list, then swaps in minus.gif when done. These
    are neither plus nor minus, so without tracking them the loop would think
    nothing is pending and stop early — leaving those sessions unexpanded.
    Treat a non-zero hourglass count as 'still working, keep waiting'."""
    return page.evaluate(
        r"""
        () => {
            const grab = sel => Array.from(document.querySelectorAll(sel))
                                     .map(el => el.id);
            return {
                day_pluses:     grab('img[id^="DAY:"][src*="plus"]'),
                session_pluses: grab('img[id^="SES:"][src*="plus"]'),
                see_more:       grab('[id^="LEVELSHOWNORECORDS:"]'),
                hourglasses:    grab('img[id^="SESSION:"][src*="hourglass"]'),
            };
        }
        """
    )


def _count_hourglasses(page) -> int:
    """Quick count of in-flight (hourglass) sessions."""
    return page.evaluate(
        "() => document.querySelectorAll("
        "'img[id^=\"SESSION:\"][src*=\"hourglass\"]').length"
    )


def _wait_for_hourglasses_to_settle(page, *, max_wait_ms: int = 15_000,
                                     poll_ms: int = 400,
                                     label: str = "") -> int:
    """Block until no session is showing an hourglass, OR the count has
    stopped decreasing for a few polls (stalled), OR max_wait_ms elapses.

    Returns the final hourglass count (0 means everything that was loading
    finished). We watch for 'stopped decreasing' as well as 'reached zero'
    so a server that never fully clears a particular hourglass can't hang
    the whole run — after the stall window we give up waiting and let the
    caller's one-at-a-time retry deal with the stragglers."""
    waited = 0
    last = _count_hourglasses(page)
    if last == 0:
        return 0
    tag = f"[{label}] " if label else ""
    log(f"  {tag}waiting for {last} hourglass(es) to finish loading…")
    stall_polls = 0
    while waited < max_wait_ms:
        page.wait_for_timeout(poll_ms)
        waited += poll_ms
        now = _count_hourglasses(page)
        if now == 0:
            log(f"  {tag}all hourglasses cleared after {waited} ms.")
            return 0
        if now < last:
            last = now
            stall_polls = 0          # progress — reset the stall counter
        else:
            stall_polls += 1
            # ~2s of no progress => treat as stalled and stop waiting here.
            if stall_polls * poll_ms >= 2_000:
                log(f"  {tag}{now} hourglass(es) stalled (no progress for "
                    f"~2s); moving on to per-session retry.")
                return now
    log(f"  {tag}timed out after {max_wait_ms} ms with {last} "
        "hourglass(es) still loading; moving on to per-session retry.")
    return last


def _js_click_batch(page, ids: list[str], *, wait_after_ms: int = 0
                    ) -> tuple[int, int]:
    """Dispatch a full mousedown + mouseup + click sequence on each id in
    a single round-trip. We use all three because legacy GWT widgets
    sometimes attach behaviour to mousedown/mouseup directly rather than
    to click; firing only HTMLElement.click() can silently miss those.
    Returns (clicked, missing) so callers can log progress."""
    if not ids:
        return 0, 0
    res = page.evaluate(
        r"""
        (ids) => {
            const opts = {
                bubbles: true, cancelable: true, view: window,
                button: 0, buttons: 0, composed: true,
            };
            const fire = (el) => {
                el.dispatchEvent(new MouseEvent('mousedown', opts));
                el.dispatchEvent(new MouseEvent('mouseup',   opts));
                el.dispatchEvent(new MouseEvent('click',     opts));
            };
            let clicked = 0, missing = 0;
            for (const id of ids) {
                const el = document.getElementById(id);
                if (!el) { missing++; continue; }
                try { fire(el); clicked++; }
                catch (e) { missing++; }
            }
            return {clicked, missing};
        }
        """, ids
    )
    if wait_after_ms:
        page.wait_for_timeout(wait_after_ms)
    return res["clicked"], res["missing"]


def expand_everything(page, max_outer_rounds: int = 8) -> None:
    """Click every '+' (DAY and SES) and every 'See More…' using batched
    JS clicks. Each phase grabs a fresh snapshot of ids before clicking,
    so we never depend on positional locators against a mutating DOM.

    Per-batch progress logging is intentional: with 200+ session pluses,
    we want a heartbeat every few seconds so silent stretches stand out.
    """
    for outer in range(max_outer_rounds):
        pending = _pending_ids(page)
        nd, ns, nm, nh = (len(pending["day_pluses"]),
                          len(pending["session_pluses"]),
                          len(pending["see_more"]),
                          len(pending["hourglasses"]))
        log(f"[expand outer {outer+1}]  DAY+={nd}  SES+={ns}  SeeMore={nm}  "
            f"Hourglass={nh}  (total {nd + ns + nm + nh})")
        if nd + ns + nm + nh == 0:
            page.wait_for_timeout(500)
            return
        # If the only thing outstanding is hourglasses (sessions already
        # clicked and now loading), don't re-click anything — just wait for
        # them to settle, then loop again to re-evaluate.
        if nh and not (nd or ns or nm):
            _wait_for_hourglasses_to_settle(page, label="outer-wait")
            continue

        # ---- 1. DAY pluses ---------------------------------------------
        if pending["day_pluses"]:
            log(f"  [day+] clicking {nd} day icon(s)…")
            c, m = _js_click_batch(page, pending["day_pluses"],
                                   wait_after_ms=500)
            log(f"    -> clicked {c}/{nd} (missing {m})")

        # ---- 2. See More links: each click can reveal new sessions and
        # occasionally new See More links, so refresh the id list between
        # passes. Wait 1.2s after each batch so lazily-loaded rows arrive.
        for sm_pass in range(50):
            sm_ids = page.evaluate(
                "() => Array.from(document.querySelectorAll("
                "'[id^=\"LEVELSHOWNORECORDS:\"]')).map(el => el.id)"
            )
            if not sm_ids:
                break
            log(f"  [see-more pass {sm_pass+1}] clicking "
                f"{len(sm_ids)} link(s)…")
            c, m = _js_click_batch(page, sm_ids, wait_after_ms=1200)
            log(f"    -> clicked {c}/{len(sm_ids)} (missing {m})")

        # ---- 3. SES pluses in SMALL batches, waiting for each batch's
        # hourglasses to settle before firing the next one. Clicking a
        # SES:plus kicks off an async load that shows images/hourglass.png
        # until the talk list arrives and the icon becomes minus.gif. Firing
        # 40 at once and racing ahead at a fixed 350 ms wait left most loads
        # still hourglassing when we moved on, and they were never revisited
        # (they're neither plus nor minus, so _pending_ids couldn't see them)
        # — that's the integrity bug. Smaller batches + a real settle-wait
        # fix the bulk of them; the per-session pass after the loop mops up
        # the rest.
        ses_ids = page.evaluate(
            "() => Array.from(document.querySelectorAll("
            "'img[id^=\"SES:\"][src*=\"plus\"]')).map(el => el.id)"
        )
        if ses_ids:
            BATCH = 10
            n = len(ses_ids)
            n_batches = (n + BATCH - 1) // BATCH
            log(f"  [ses+] expanding {n} session(s) in "
                f"{n_batches} batch(es) of up to {BATCH} "
                f"(waiting for hourglasses between batches)…")
            total_clicked = 0
            for i in range(0, n, BATCH):
                batch = ses_ids[i:i + BATCH]
                c, m = _js_click_batch(page, batch, wait_after_ms=200)
                total_clicked += c
                # Let this batch's loads finish before starting the next,
                # so we never have hundreds of concurrent hourglasses.
                _wait_for_hourglasses_to_settle(
                    page, max_wait_ms=20_000,
                    label=f"ses batch {i // BATCH + 1}/{n_batches}")
                log(f"    -> batch {i // BATCH + 1}/{n_batches}: "
                    f"clicked {c}/{len(batch)} "
                    f"(running total {total_clicked}/{n})")
            page.wait_for_timeout(500)

    # Final mop-up: anything still not expanded gets handled ONE AT A TIME
    # with a real wait for that session to finish loading. Two kinds of
    # straggler can remain:
    #   * SES:plus toggles that never got clicked (rare), and
    #   * SESSION:NNN hourglasses — clicked but stuck mid-load because we
    #     fired their click in a batch and moved on before the server
    #     responded. Re-clicking a hourglass does nothing useful, so for
    #     those we just WAIT (poll until the icon flips to minus.gif). For
    #     leftover pluses we click, then wait.
    for sweep in range(6):
        pending = _pending_ids(page)
        nd, ns, nm, nh = (len(pending["day_pluses"]),
                          len(pending["session_pluses"]),
                          len(pending["see_more"]),
                          len(pending["hourglasses"]))
        if nd or nm:
            # A day/see-more reappeared (lazy load); let the outer machinery
            # handle those by clicking them here then re-sweeping.
            if pending["day_pluses"]:
                _js_click_batch(page, pending["day_pluses"], wait_after_ms=500)
            if pending["see_more"]:
                _js_click_batch(page, pending["see_more"], wait_after_ms=1200)
            continue
        if not (ns or nh):
            break   # nothing left

        log(f"[expand sweep {sweep+1}] one-at-a-time: "
            f"{ns} unclicked plus(es), {nh} stuck hourglass(es)…")

        # 1) Click any leftover pluses individually, waiting for each to load.
        for pid in pending["session_pluses"]:
            c, _ = _js_click_batch(page, [pid], wait_after_ms=200)
            settled = _wait_for_hourglasses_to_settle(
                page, max_wait_ms=20_000, label=f"plus {pid}")
            log(f"  [retry plus] {pid}: clicked {c}, "
                f"hourglasses now {settled}")

        # 2) For stuck hourglasses, wait (don't re-click). Poll each until
        # it's no longer an hourglass or we hit the per-session timeout.
        still = page.evaluate(
            "() => Array.from(document.querySelectorAll("
            "'img[id^=\"SESSION:\"][src*=\"hourglass\"]')).map(el => el.id)"
        )
        for hid in still:
            waited = 0
            while waited < 25_000:
                present = page.evaluate(
                    "(id) => { const el = document.getElementById(id);"
                    " return !!(el && /hourglass/.test(el.src)); }", hid)
                if not present:
                    break
                page.wait_for_timeout(500)
                waited += 500
            state = "resolved" if waited < 25_000 else "STILL stuck"
            log(f"  [retry hourglass] {hid}: {state} after {waited} ms")

    # Final diagnostic snapshot of what (if anything) is still pending.
    pending = _pending_ids(page)
    nd, ns, nm, nh = (len(pending["day_pluses"]),
                      len(pending["session_pluses"]),
                      len(pending["see_more"]),
                      len(pending["hourglasses"]))

    if nd + ns + nm + nh:
        log(f"[expand] WARNING: still pending after sweeps — DAY+={nd}, "
            f"SES+={ns}, SeeMore={nm}, Hourglass={nh}. Affected sessions "
            "will appear in the saved DOM without their talk rows; check the "
            "diagnostic below for the list.")
        if pending["hourglasses"]:
            log(f"[expand]   stuck hourglasses: {pending['hourglasses']}")
    else:
        log("[expand] All days, sessions, and See More links expanded "
            "(no hourglasses remaining).")


def diagnose_sessions(page) -> None:
    """Print a per-day breakdown of session expansion: how many sessions
    are expanded, how many are still collapsed, how many talks are
    attached to each. Also enumerate any still-collapsed sessions to make
    clear which titles will be missing talk rows."""
    stats = page.evaluate(
        r"""
        () => {
            const days = [];
            const dayDivs = document.querySelectorAll('div.text[id^="LEVEL:"]');
            for (const dayDiv of dayDivs) {
                const header = dayDiv.querySelector('p.pageheader');
                const dayName = header ? header.innerText.trim() : '';
                const sessions = [];
                const sDivs = dayDiv.querySelectorAll(
                    'div.text[id^="SESSION:"]');
                for (const sDiv of sDivs) {
                    const plusIcon  = sDiv.querySelector(
                        'img[id^="SES:"][src*="plus"]');
                    const minusIcon = sDiv.querySelector(
                        'img[id^="SES:"][src*="minus"]');
                    const nTalks = sDiv.querySelectorAll(
                        'td.ip_expanded_session').length;
                    const headerP = sDiv.querySelector('p.pagecontents');
                    const title = headerP
                        ? headerP.innerText.trim().split('\n')[0]
                        : '';
                    sessions.push({
                        id: sDiv.id.replace('SESSION:', ''),
                        title: title,
                        hasExpandIcon: !!(plusIcon || minusIcon),
                        isExpanded: !!minusIcon,
                        nTalks: nTalks,
                    });
                }
                days.push({name: dayName, sessions});
            }
            return days;
        }
        """
    )
    log("[diag] Per-day session/talk counts:")
    for d in stats:
        sess     = d["sessions"]
        total    = len(sess)
        expanded = sum(1 for s in sess if s["isExpanded"])
        collapsed = sum(1 for s in sess
                        if s["hasExpandIcon"] and not s["isExpanded"])
        no_icon  = sum(1 for s in sess if not s["hasExpandIcon"])
        n_talks  = sum(s["nTalks"] for s in sess)
        log(f"[diag]   {d['name'] or '(unnamed day)'}: "
            f"{total} sessions  ({expanded} expanded, "
            f"{collapsed} still collapsed, {no_icon} no-expand-icon), "
            f"{n_talks} talks")
        if collapsed:
            log(f"[diag]     STILL COLLAPSED on this day:")
            for s in sess:
                if s["hasExpandIcon"] and not s["isExpanded"]:
                    log(f"[diag]       SESSION:{s['id']:>6}  "
                        f"{s['title'][:90]}")
        # Sessions that aren't short courses but have 0 talks are
        # suspicious — flag them for a manual look at the page.
        for s in sess:
            if s["isExpanded"] and s["nTalks"] == 0:
                log(f"[diag]     EXPANDED but 0 talks: "
                    f"SESSION:{s['id']:>6}  {s['title'][:90]}")


def save_expanded_dom(page) -> None:
    """Dump the full page DOM (documentElement.outerHTML) to disk AFTER the
    expand pass, so the program structure can be re-parsed offline by the
    processor without re-running the live browser. This is the
    reproducibility anchor: given this HTML file, the processor's parsing
    logic (and the JSON it builds) can be re-derived deterministically.

    We grab the live, post-expansion outerHTML rather than page.content()
    because the latter returns the original server response, not the DOM as
    mutated by the GWT widget tree's expand clicks. The DOCTYPE is prepended
    manually since outerHTML omits it.

    A <base href> tag is injected right after <head> so that opening the
    saved file later in a browser still resolves its relative CSS/JS/image
    URLs against the live site instead of against the local file path
    (purely cosmetic — the markup itself is complete regardless). Without it
    the page renders unstyled when opened locally."""
    log(f"  [dom] capturing fully-expanded DOM -> {OUTPUT_DOM_HTML}")
    try:
        html = page.evaluate("() => document.documentElement.outerHTML")

        # Best-effort <base href> injection so a later manual open still finds
        # the site's assets. Insert right after <head> if present; harmless if
        # not. Cosmetic only — never let this block the save.
        try:
            base_url = page.url
            if base_url and "<base" not in html[:2000].lower():
                html = re.sub(
                    r"(<head[^>]*>)",
                    r'\1<base href="' + base_url + '">',
                    html, count=1, flags=re.I,
                )
        except Exception:
            pass

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_DOM_HTML, "w", encoding="utf-8") as f:
            f.write("<!DOCTYPE html>\n")
            f.write(html)
        size_kb = OUTPUT_DOM_HTML.stat().st_size / 1024
        log(f"  [dom] saved expanded DOM ({size_kb:,.1f} KB)")
    except Exception as e:
        log(f"  [dom] WARNING: couldn't save expanded DOM: {e}")


# =============================================================================
# Short-courses page — save its raw HTML for the processor to parse offline
# =============================================================================
def save_short_courses_html(page) -> None:
    """Open the public short-courses page in a NEW tab in the same browser
    context and save its full DOM (documentElement.outerHTML) to disk. The
    processor parses every 'SC<code> …' link out of this file to recover each
    short course's instructor name + affiliation (which the planner's own
    popup never provides).

    Uses a fresh page so the planner tab is left untouched. On any failure
    (network, layout change, etc.) it logs a warning and returns without
    writing a file, so the rest of the download still succeeds; the processor
    will simply have no short-course instructors in that case."""
    log(f"  [course] fetching short-courses page from {SHORT_COURSES_URL}")
    ctx = page.context
    sc_page = None
    try:
        sc_page = ctx.new_page()
        sc_page.goto(SHORT_COURSES_URL, wait_until="domcontentloaded",
                     timeout=60_000)
        # Give the page a moment to settle any late-loading content, then grab
        # the live DOM (same approach as the planner snapshot).
        sc_page.wait_for_timeout(1_500)
        html = sc_page.evaluate("() => document.documentElement.outerHTML")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT_SHORTCOURSE_HTML, "w", encoding="utf-8") as f:
            f.write("<!DOCTYPE html>\n")
            f.write(html)
        size_kb = OUTPUT_SHORTCOURSE_HTML.stat().st_size / 1024
        log(f"  [course] saved short-courses HTML "
            f"({size_kb:,.1f} KB) -> {OUTPUT_SHORTCOURSE_HTML}")
    except Exception as e:
        log(f"  [course] WARNING: couldn't save {SHORT_COURSES_URL}: {e}. "
            "Short-course instructors will be unavailable to the processor.")
    finally:
        if sc_page is not None:
            try:
                sc_page.close()
            except Exception:
                pass


# =============================================================================
# Downloads — the official Program + Abstracts files
# =============================================================================
def download_program_files(page) -> None:
    log(f"  [download] target directory: {DATA_DIR}")
    for label, filename in DOWNLOAD_BUTTONS:
        dest = DATA_DIR / filename
        log(f"  [download] processing '{label}' -> {dest}")
        if dest.exists():
            size_kb = dest.stat().st_size / 1024
            log(f"  [skip] {filename} already exists ({size_kb:,.1f} KB) — "
                "delete to re-download.")
            continue

        # Two GWT buttons exist for each label (Program Only also says PDF /
        # Excel); we need the one whose visible text matches exactly.
        candidates = page.locator(f"button:has-text(\"{label}\")")
        n = candidates.count()
        log(f"  [download]   found {n} candidate button(s) for '{label}'")
        if n == 0:
            log(f"  [warn] couldn't find a button with text '{label}'.")
            continue

        clicked = False
        for i in range(n):
            btn = candidates.nth(i)
            try:
                visible_text = btn.inner_text(timeout=2_000).strip()
            except Exception:
                visible_text = ""
            log(f"  [download]   candidate {i + 1}/{n}: visible text "
                f"{visible_text!r}")
            if visible_text != label:
                log(f"  [download]   candidate {i + 1} text doesn't match "
                    "exactly — skipping it.")
                continue
            try:
                btn.scroll_into_view_if_needed(timeout=5_000)
                log(f"  [download] clicking '{label}' and waiting for the "
                    "download to start (up to 180s)…")
                with page.expect_download(timeout=180_000) as dl_info:
                    btn.click(timeout=10_000)
                dl = dl_info.value
                log(f"  [download] download started (server name: "
                    f"{dl.suggested_filename}); saving…")
                dl.save_as(str(dest))
                size_kb = dest.stat().st_size / 1024 if dest.exists() else 0.0
                log(f"  [ok] saved {filename} ({size_kb:,.1f} KB) "
                    f"(server name: {dl.suggested_filename})")
                clicked = True
                break
            except PWTimeout:
                log(f"  [warn] timed out waiting for download of {label}.")
                break
            except Exception as e:
                log(f"  [warn] couldn't download {label}: {e}")
                break
        if not clicked:
            log(f"  [warn] no button matched exactly '{label}'.")


def wait_for_planner_ready(page, timeout_s: int = 180) -> bool:
    """Block until the planner page is ready: the search-box instruction
    sentence is on the page AND the day rows are showing plus icons. Replaces
    the old 'press ENTER when ready' interactive prompt so the script can flow
    start-to-finish unattended.

    Returns True if it became ready in time, False on timeout. Prints a
    state line every time the (text-present, day-count, ses-count) tuple
    changes, so the page's progress is visible while waiting.
    """
    target = ("Enter a name, institution, Final ID, "
              "and/or words from the session or presentation title.")
    log(f"[load] Waiting up to {timeout_s}s for planner to render. "
        f"Need: '{target[:40]}…' AND DAY: plus icons.")
    deadline   = time.monotonic() + timeout_s
    last_state = None
    while time.monotonic() < deadline:
        try:
            state = page.evaluate(
                r"""
                (needle) => ({
                    has_text:  document.body.innerText.indexOf(needle) >= 0,
                    n_days:    document.querySelectorAll('[id^="DAY:"]').length,
                    n_ses:     document.querySelectorAll('[id^="SES:"]').length,
                    n_pluses:  document.querySelectorAll(
                        'img[id^="DAY:"][src*="plus"]').length,
                })
                """, target)
        except Exception:
            state = {"has_text": False, "n_days": 0,
                     "n_ses": 0, "n_pluses": 0}
        key = (state["has_text"], state["n_days"], state["n_ses"])
        if key != last_state:
            log(f"[load]   text={state['has_text']!s:>5}  "
                f"DAY rows={state['n_days']:>3}  "
                f"SES rows={state['n_ses']:>3}  "
                f"day plus icons={state['n_pluses']:>3}")
            last_state = key
        if state["has_text"] and state["n_days"] > 0:
            log("[load] Planner is ready — starting work.")
            return True
        page.wait_for_timeout(750)
    log("[load] WARNING: timed out waiting for planner; continuing anyway.")
    return False


# =============================================================================
# Main
# =============================================================================
def _close_browser(ctx) -> None:
    """Close the Playwright browser context if it's still open. Idempotent and
    exception-safe: called explicitly once the download no longer needs the
    live page, and again in main()'s finally as a safety net, so it must
    tolerate being called twice or on an already-closed context."""
    if ctx is None:
        return
    if getattr(ctx, "_closed_by_scraper", False):
        return
    try:
        ctx.close()
        log("[browser] Chromium closed.")
    except Exception as e:
        log(f"[browser] (close ignored: {e})")
    finally:
        try:
            ctx._closed_by_scraper = True
        except Exception:
            pass


def _clear_profile() -> None:
    """Delete the persistent Chromium profile dir. Called after the fetch is
    done (browser closed) so a session/cache doesn't persist between runs.
    Exception-safe: a transient lock on the profile won't crash the run."""
    if USER_DATA_DIR.exists():
        log(f"[cleanup] Removing chrome profile at {USER_DATA_DIR} …")
        shutil.rmtree(USER_DATA_DIR, ignore_errors=True)


def main() -> None:
    log("=" * 72)
    log("[config] CLEO 2026 DOWNLOADER starting up.")
    log(f"[config]   script dir          : {SCRIPT_DIR}")
    log(f"[config]   data dir            : {DATA_DIR}")
    log(f"[config]   browse URL          : {BROWSE_URL}")
    log(f"[config]   short-courses URL   : {SHORT_COURSES_URL}")
    log(f"[config]   chrome profile      : {USER_DATA_DIR}")
    log(f"[config]   downloaded PDF      : {INPUT_OFFICIAL_PDF}")
    log(f"[config]   downloaded CSV      : {INPUT_OFFICIAL_CSV}")
    log(f"[config]   expanded DOM out    : {OUTPUT_DOM_HTML}")
    log(f"[config]   short-courses out   : {OUTPUT_SHORTCOURSE_HTML}")
    log(f"[config]   child log           : {CHILD_LOG}")
    log("=" * 72)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    USER_DATA_DIR.mkdir(exist_ok=True)
    log("[setup] data dir and chrome profile dir ready.")

    log(f"[browser] Launching Chromium "
        f"({'headless' if HEADLESS else 'non-headless'}, persistent profile)…")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=HEADLESS,
            accept_downloads=True,
            viewport={"width": 1500, "height": 950},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        log("[browser] Chromium launched; got a page handle.")

        try:
            log(f"[load] Navigating to {BROWSE_URL} …")
            page.goto(BROWSE_URL, wait_until="domcontentloaded", timeout=60_000)
            log("[load] Initial DOM content loaded.")
        except Exception as e:
            log(f"[warn] Could not load {BROWSE_URL} ({e}); "
                "navigate manually if needed.")

        # planner.jsp returns a GWT app shell whose <div id="menu"> and
        # <div id="main"> get filled in asynchronously by a legacy JavaScript app.
        log("[load] Waiting for the GWT app to render the side menu…")
        try:
            page.wait_for_selector("#BROWSE_THE_PROGRAM", timeout=45_000)
        except PWTimeout:
            log("[load] Side-menu link didn't appear in 45 s — continuing.")

        if page.locator("[id^='DAY:']").count() == 0:
            try:
                page.locator("#BROWSE_THE_PROGRAM").first.click(timeout=5_000)
            except Exception:
                pass

        # New auto-wait — we no longer block on ENTER. We poll for the
        # search-box instructions and DAY rows; once both are present, the
        # GWT widget tree is settled enough to start clicking.
        wait_for_planner_ready(page)

        try:
            # ---------------- Step 1: downloads ------------------------
            # Done FIRST so we always have the official files even if the
            # expansion below fails.
            log("[1/4] Downloading Program + Abstracts files into data/…")
            download_program_files(page)

            # ---------------- Step 2: expand everything ----------------
            log("[2/4] Expanding all '+' icons and 'See More…' links…")
            expand_everything(page)

            # Quick per-day sanity report on what's actually visible
            # before we snapshot the DOM — helps spot sessions that stayed
            # shut even after the retry pass.
            diagnose_sessions(page)

            # ---------------- Step 3: snapshot the expanded DOM --------
            # Save the fully-expanded page HTML to data/ for the processor to
            # re-parse offline, so the program can be processed later without
            # re-running the live browser.
            log("[3/4] Saving the fully-expanded planner DOM…")
            save_expanded_dom(page)

            # ---------------- Step 4: short-courses page HTML ----------
            # Short courses have no inline talks and no presider; their
            # instructor (and affiliation) lives on the public CLEO short-
            # courses website. Save that page's raw HTML so the processor can
            # parse the SC<code> -> instructor map offline.
            log("[4/4] Saving the public short-courses page HTML…")
            save_short_courses_html(page)

            # The browser has now downloaded everything that needs a live page
            # (the two files, the expanded planner DOM, and the short-courses
            # HTML). Close Chrome now rather than leaving it open and idle.
            log("[browser] Download complete — closing Chromium.")
            _close_browser(ctx)

        except Exception as exc:
            # An error shouldn't slam Chrome shut before the traceback can
            # be seen — print it but fall through to the cleanup in finally.
            print(f"\n!!! Error during download: {exc!r}", flush=True)
            import traceback
            traceback.print_exc()

        finally:
            # GUARANTEED to run. Close the browser if it's still open (e.g. an
            # error before the explicit close above), then report where
            # everything landed. Use plain print() rather than log() here so
            # this works even if the timestamped logger has been disrupted.
            _close_browser(ctx)
            print(flush=True)
            print("=" * 72, flush=True)
            print("DONE (download only). Next: run process_program_cleo2026.py",
                  flush=True)
            print(f"  data dir          : {DATA_DIR}", flush=True)
            print(f"  downloaded PDF    : {INPUT_OFFICIAL_PDF}", flush=True)
            print(f"  downloaded CSV    : {INPUT_OFFICIAL_CSV}", flush=True)
            print(f"  expanded DOM      : {OUTPUT_DOM_HTML}", flush=True)
            print(f"  short-courses HTML: {OUTPUT_SHORTCOURSE_HTML}", flush=True)
            print("=" * 72, flush=True)

    # The persistent-context `with` block has now fully exited, so Playwright's
    # driver has released the profile dir; safe to delete it after the fetch.
    _clear_profile()


# =============================================================================
# Subprocess wrapper (so Spyder/IPython users don't get an asyncio collision)
# =============================================================================
def run_in_subprocess() -> None:
    """Re-invoke this script as a fresh Python process so Playwright's sync
    API can run without fighting an existing event loop.

    The child still tees its own output to CHILD_LOG, but we ALSO capture the
    child's stdout/stderr here and echo every line to this (the parent's)
    terminal as it arrives. That way the full, verbose log shows up live in
    whatever terminal launched the script — not just in the log file or a
    separate console window."""
    script = os.path.abspath(__file__)
    cwd    = os.getcwd()
    print("[parent] Spawning a clean Python subprocess so Playwright can run "
          "outside Spyder/IPython's asyncio loop…", flush=True)
    print(f"[parent] Child output is mirrored to: {CHILD_LOG}", flush=True)
    print("[parent] Streaming child output below (also saved to the log):",
          flush=True)
    print("[parent] " + "-" * 64, flush=True)
    try:
        CHILD_LOG.unlink()
    except FileNotFoundError:
        pass

    # Merge the child's stderr into its stdout so ordering is preserved, and
    # read line-by-line so the parent terminal updates in real time rather
    # than buffering until the child exits. PYTHONUNBUFFERED keeps the child's
    # own print() flushing promptly even when its stdout is a pipe.
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, script, CHILD_FLAG],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,                       # line-buffered
    )
    captured_tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        # Keep a rolling tail in memory in case we want to summarize on error.
        captured_tail.append(line.rstrip("\n"))
        if len(captured_tail) > 80:
            captured_tail.pop(0)
    returncode = proc.wait()

    print("[parent] " + "-" * 64, flush=True)
    if returncode != 0:
        print(f"[parent] Subprocess exited with code {returncode}.", flush=True)
        # The live stream above already showed everything; fall back to the
        # log tail only if for some reason we captured nothing.
        if not captured_tail and CHILD_LOG.exists():
            try:
                tail = "\n".join(
                    CHILD_LOG.read_text(encoding="utf-8",
                                        errors="replace").splitlines()[-80:]
                )
                print("[parent] === last 80 lines of child log ===")
                print(tail)
                print("[parent] === end child log ===")
            except Exception as e:
                print(f"[parent] (couldn't read child log: {e})")
        elif not CHILD_LOG.exists():
            print(f"[parent] No child log was produced at {CHILD_LOG}; the "
                  "child probably crashed before opening it.")
    else:
        print(f"[parent] Subprocess finished. Outputs:", flush=True)
        print(f"[parent]   data dir          : {DATA_DIR}", flush=True)
        print(f"[parent]   downloaded PDF    : {INPUT_OFFICIAL_PDF}", flush=True)
        print(f"[parent]   downloaded CSV    : {INPUT_OFFICIAL_CSV}", flush=True)
        print(f"[parent]   expanded DOM      : {OUTPUT_DOM_HTML}", flush=True)
        print(f"[parent]   short-courses HTML: {OUTPUT_SHORTCOURSE_HTML}",
              flush=True)


class _Tee:
    """Minimal stdout/stderr tee that mirrors writes to a file."""
    def __init__(self, *streams):
        self.streams = streams
    def write(self, s):
        for st in self.streams:
            try:
                st.write(s); st.flush()
            except Exception:
                pass
        return len(s)
    def flush(self):
        for st in self.streams:
            try: st.flush()
            except Exception: pass
    def isatty(self):
        try: return self.streams[0].isatty()
        except Exception: return False


def _run_child() -> None:
    """Entry point for the re-spawned subprocess. Mirrors stdout/stderr to a
    log file and keeps the console window open after main() returns or
    crashes, so the outcome stays readable."""
    log_fp = open(CHILD_LOG, "w", encoding="utf-8", errors="replace")
    sys.stdout = _Tee(sys.__stdout__, log_fp)   # type: ignore[assignment]
    sys.stderr = _Tee(sys.__stderr__, log_fp)   # type: ignore[assignment]

    exit_code = 0
    try:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except Exception:
            print("[child] Playwright import failed — running bootstrap once…")
            _bootstrap_playwright()
        main()
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else 1
        if exit_code:
            import traceback; traceback.print_exc()
    except BaseException:
        import traceback; traceback.print_exc()
        exit_code = 1
    finally:
        # Backstop: ensure the chrome profile is gone even if main() bailed
        # before its own cleanup (e.g. the browser failed to launch).
        try:
            _clear_profile()
        except Exception:
            pass
        print(f"\n[child] Exit code: {exit_code}")
        print(f"[child] Log written to: {CHILD_LOG}")
        try: log_fp.close()
        except Exception: pass
        sys.exit(exit_code)


if __name__ == "__main__":
    if CHILD_FLAG in sys.argv:
        _run_child()
    else:
        run_in_subprocess()
