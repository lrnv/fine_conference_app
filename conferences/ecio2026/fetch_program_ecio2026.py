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

"""fetch_program_ecio2026.py — DOWNLOAD ONLY.

The "downloader" half of the conference pipeline. The full program is
published as two PDFs linked from the public programme page,

    https://www.ecio-conference.org/programme-26/

There is no planner / Excel / abstract-book export. The two PDFs are:

    ECIO26_DetailedSchedule.pdf   the wide A3 grid: every session, every time
                                  slot, every talk title + speaker.
    ECIO26_Concise.pdf            one-page program overview (session blocks
                                  only, no per-talk detail).

We also save the Agenda-of-Sessions PDF,

    2026_ecio_agenda_of_sessions.pdf   a clean one-table-per-day overview that,
                                  unlike the detailed grid, prints the LOCATION
                                  of every non-talk event (registration desk,
                                  coffee/lunch foyers, the reception/gala
                                  venues, the student-event rooms) and the daily
                                  Registration/Coffee/Lunch rows.

This file is not linked from the CMS-hosted programme page; it is served from
the publisher's media CDN at a fixed path, so we fetch it directly (and treat it
as optional — its CDN path can rotate).

We also save a third artifact, the invited-speakers HTML page,

    ECIO26_InvitedSpeakers.html   the public list of invited speakers laid out
                                  as (Name, Affiliation, Talk Title) triples.

The detailed schedule PDF prints only the speaker's name in each cell, with no
affiliation; the invited-speakers page is the one public conference source that
attaches an affiliation to those speaker names, so we cache it here for the
processor to cross-reference when filling talk institutions.

The PDF filenames on the website carry the re-issue date in their suffix
(e.g. ECIO26_DetailedProgramSchedule_21_5.pdf), which changes whenever the
organisers publish a refresh, so we do NOT hard-code those URLs. Instead we
scrape the programme page itself and pick the most recent matching PDF link
by the date encoded in its filename. This way the fetcher keeps working when
the organisers publish an updated version of either PDF. The invited-speakers page,
in contrast, lives at a fixed URL and is fetched directly.

Finally, we render and save the three per-day schedule pages

    ECIO26_OpticaMonday.html / …Tuesday.html / …Wednesday.html

from the official event site. These mirror the full program and, unlike the
PDFs, carry the COMPLETE author list (with affiliations) and the abstract for
every talk. That schedule is a JavaScript single-page app behind bot protection,
so this part drives a real headed Chromium via Playwright (switching to
"Detailed View" and expanding every "Continue Reading" link before saving the
rendered HTML) rather than a plain HTTP fetch. See `_fetch_event_schedule`.

We also save the fully-expanded conference-planner DOM,

    ECIO26_planner_expanded.html   the planner.jsp page DOM captured AFTER every
                                   day, session, and "See More…" control has
                                   been expanded.

The planner is the only public source that lists each technical session's
PRESIDER(s) — neither the detailed-schedule PDF nor the schedule pages render
them. As in the sibling fetchers, we drive a headless Chromium via Playwright,
click every '+' (day and session) and every "See More…" link, then save the
expanded DOM for the processor to parse offline. We deliberately do NOT download
the planner's Program+Abstracts PDF/CSV — the PDFs above are the authoritative
program; the planner is used only for presiders.

Contacts the network via urllib for the PDFs/HTML pages and via Chromium for the
schedule pages (headed) and the planner (headless). The processor
(process_program_ecio2026.py) runs entirely offline against what we save here.
"""

from __future__ import annotations

import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"

PROGRAMME_URL = "https://www.ecio-conference.org/programme-26/"

# Each artifact saved into data/ is one of:
#   - "pattern" artifact: discovered on PROGRAMME_URL by regex, then downloaded.
#     The pattern matches the rolling re-issue filenames the organisers publish.
#   - "url" artifact: lives at a fixed URL on the conference site and is fetched
#     directly. Used for HTML pages that don't carry rolling date suffixes.
# All entries share the same "name" (saved filename) and "desc" (log label).
ARTIFACTS = [
    {
        "name": "ECIO26_DetailedSchedule.pdf",
        # Detailed schedule: ECIO26_DetailedProgramSchedule_<dd>_<m>.pdf
        "pattern": re.compile(
            r"https?://[^\"'>\s]+/ECIO26_DetailedProgramSchedule[^\"'>\s]*\.pdf",
            re.IGNORECASE,
        ),
        "desc": "detailed program schedule",
    },
    {
        "name": "ECIO26_Concise.pdf",
        # Concise overview: ECIO_FinalProgram_Concise_<dd>_<mm>.pdf
        "pattern": re.compile(
            r"https?://[^\"'>\s]+/ECIO_FinalProgram_Concise[^\"'>\s]*\.pdf",
            re.IGNORECASE,
        ),
        "desc": "concise program overview",
    },
    {
        "name": "2026_ecio_agenda_of_sessions.pdf",
        # Agenda of Sessions: a clean one-table-per-day overview that, unlike the
        # detailed schedule, prints the LOCATION of every non-talk event
        # (registration desk, coffee/lunch foyers, the reception/gala venues,
        # the student-event rooms) and the daily Registration/Coffee/Lunch rows.
        # It is NOT linked from the CMS-hosted programme page; it is served from
        # the publisher's media CDN at a fixed path. `required: no` — the processor
        # falls back to detailed-schedule locations when it's absent.
        "url": (
            "https://opticaorg-dev-cac7d2csctagc8bm.z01.azurefd.net/$web/"
            "optica/media/files/events/ecio/2026/"
            "2026_ecio_agenda_of_sessions.pdf"
        ),
        "desc": "agenda of sessions (locations + logistics)",
        "optional": True,
    },
    {
        "name": "ECIO26_InvitedSpeakers.html",
        # Fixed URL — the invited-speakers page is the only public conference source
        # that ties each invited speaker's name to an affiliation.
        "url": "https://www.ecio-conference.org/invited-speakers-2/",
        "desc": "invited speakers page",
    },
    # The six pages below are *enrichment* sources. The detailed-schedule PDF
    # already supplies enough information to build a usable program (session
    # times, rooms, talk titles + presenters); each of these adds detail the
    # PDF doesn't render — plenary abstracts and bios, workshop chairs, student-
    # event panellists, cleaner industry-talk metadata (company, talk title,
    # speaker as separate fields), and short descriptions for social and lab-
    # tour events. The processor cross-references them by speaker name or
    # session id and falls back gracefully when any one is missing.
    {
        "name": "ECIO26_PlenarySpeakers.html",
        "url": "https://www.ecio-conference.org/plenary-speakers/",
        "desc": "plenary speakers page",
    },
    {
        "name": "ECIO26_Workshops.html",
        "url": "https://www.ecio-conference.org/workshops/",
        "desc": "workshops page",
    },
    {
        "name": "ECIO26_StudentEvent.html",
        "url": "https://www.ecio-conference.org/sunday-student-event/",
        "desc": "Sunday student-event page",
    },
    {
        "name": "ECIO26_IndustryTalks.html",
        "url": "https://www.ecio-conference.org/industry-talks/",
        "desc": "industry talks page",
    },
    {
        "name": "ECIO26_SocialEvents.html",
        "url": "https://www.ecio-conference.org/social-events/",
        "desc": "social events page",
    },
    {
        "name": "ECIO26_LabTours.html",
        "url": "https://www.ecio-conference.org/lab-and-company-visit/",
        "desc": "lab + company-visit page",
    },
]

# Filename-date pattern: ..._<d>_<m>.pdf or ..._<dd>_<mm>.pdf at the very end of
# the basename (used to pick the most recent re-issue when multiple candidates
# show up). Year is assumed 2026.
DATE_RE = re.compile(r"_(\d{1,2})_(\d{1,2})\.pdf$", re.IGNORECASE)

# Polite UA — some CMS installs 403 the default urllib UA.
UA = "Mozilla/5.0 (ecio2026-fetch; fine-conference-app)"

# ---------------------------------------------------------------------------
# Per-day schedule pages (browser-rendered).
#
# The full program is also published on the official event site as a per-day
# schedule. Unlike the PDFs, each talk cell there carries the COMPLETE author
# list (with affiliations) and the abstract — the richest content source for
# the conference. The schedule is a JavaScript single-page app (the day is
# selected by the URL hash) sitting behind bot protection, so plain urllib can't
# read it; we drive a real Chromium via Playwright instead.
#
# For each day we: load the page, switch the view toggle to "Detailed View",
# click every "Continue Reading" link so each abstract's full text is expanded
# in the DOM, then save the rendered HTML. The processor parses these offline.
#
# The bot wall passes automatically for a headed (non-headless) browser with a
# realistic UA; if a CAPTCHA ever appears we leave the window open and wait so
# the user can solve it. A persistent browser profile (kept outside the repo)
# carries any clearance cookie across runs.
EVENT_SCHEDULE_URL = (
    "https://www.optica.org/events/topical_meetings/"
    "european_conference_on_integrated_optics_(ecio)/schedule/#/"
)
# Day -> saved filename. The day names are the schedule's own hash routes, not
# program content.
EVENT_SCHEDULE_DAYS = {
    "Monday": "ECIO26_OpticaMonday.html",
    "Tuesday": "ECIO26_OpticaTuesday.html",
    "Wednesday": "ECIO26_OpticaWednesday.html",
}
# Real-browser UA for the headed Chromium (the bot wall blocks the headless
# default UA). Run headed so the user can solve a CAPTCHA if one ever appears.
EVENT_SCHEDULE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
EVENT_SCHEDULE_PROFILE_DIR = Path.home() / ".cache" / "ecio2026_optica_profile"
# How long to wait (seconds) for a human to clear a bot-wall / CAPTCHA before
# giving up on a day. Generous because solving it is a manual step.
EVENT_SCHEDULE_WALL_WAIT_S = 180


# ---------------------------------------------------------------------------
# Conference planner (browser-rendered).
#
# The program is ALSO published on a conference planner site. That planner is
# the only public source that lists each technical session's PRESIDER(s) —
# neither the detailed-schedule PDF nor the schedule pages render presiders. The
# planner is a legacy JavaScript app whose day rows, session rows, and "See
# More…" links must each be clicked to reveal their content, so (as in the
# sibling fetchers) we drive a headless Chromium and expand everything before
# saving the fully-expanded DOM. The processor parses the saved HTML offline for
# the per-session presider map. We deliberately do NOT download the planner's
# "Program + Abstracts" PDF/CSV — the PDFs above are the authoritative program
# source; the planner is used only for presiders.
PLANNER_URL = "https://ecio2026.abstractcentral.com/planner.jsp"
PLANNER_OUTPUT_HTML = DATA_DIR / "ECIO26_planner_expanded.html"
# Persistent Chromium profile for the planner, kept outside the repo.
PLANNER_PROFILE_DIR = Path.home() / ".cache" / "ecio2026_planner_profile"


def _planner_pending_ids(page) -> dict:
    """ids of every still-expandable planner element. Working with stable ids
    (not positional locators) avoids the 'every other one' re-indexing bug when
    a click flips plus.gif -> minus.gif. 'hourglasses' are sessions mid-load
    (icon swapped to hourglass.png while the app fetches the talk list)."""
    return page.evaluate(r"""
        () => {
            const grab = sel => Array.from(document.querySelectorAll(sel)).map(el => el.id);
            return {
                day_pluses:     grab('img[id^="DAY:"][src*="plus"]'),
                session_pluses: grab('img[id^="SES:"][src*="plus"]'),
                see_more:       grab('[id^="LEVELSHOWNORECORDS:"]'),
                hourglasses:    grab('img[id^="SESSION:"][src*="hourglass"]'),
            };
        }""")


def _planner_count_hourglasses(page) -> int:
    return page.evaluate(
        "() => document.querySelectorAll("
        "'img[id^=\"SESSION:\"][src*=\"hourglass\"]').length")


def _planner_wait_hourglasses(page, *, max_wait_ms=15000, poll_ms=400) -> int:
    """Block until no session shows an hourglass, OR the count stops decreasing
    for ~2s (stalled), OR max_wait_ms elapses. Returns the final count."""
    waited = 0
    last = _planner_count_hourglasses(page)
    if last == 0:
        return 0
    stall = 0
    while waited < max_wait_ms:
        page.wait_for_timeout(poll_ms)
        waited += poll_ms
        now = _planner_count_hourglasses(page)
        if now == 0:
            return 0
        if now < last:
            last = now
            stall = 0
        else:
            stall += 1
            if stall * poll_ms >= 2000:
                return now
    return last


def _planner_click_batch(page, ids, *, wait_after_ms=0) -> tuple[int, int]:
    """Fire mousedown+mouseup+click on each id in one round-trip (the legacy
    JavaScript widgets sometimes bind to mousedown/up rather than click)."""
    if not ids:
        return 0, 0
    res = page.evaluate(r"""
        (ids) => {
            const opts = {bubbles:true,cancelable:true,view:window,button:0,buttons:0,composed:true};
            const fire = (el) => {
                el.dispatchEvent(new MouseEvent('mousedown', opts));
                el.dispatchEvent(new MouseEvent('mouseup',   opts));
                el.dispatchEvent(new MouseEvent('click',     opts));
            };
            let clicked=0, missing=0;
            for (const id of ids) {
                const el = document.getElementById(id);
                if (!el) { missing++; continue; }
                try { fire(el); clicked++; } catch(e) { missing++; }
            }
            return {clicked, missing};
        }""", ids)
    if wait_after_ms:
        page.wait_for_timeout(wait_after_ms)
    return res["clicked"], res["missing"]


def _planner_expand_everything(page, max_outer_rounds=8) -> None:
    """Click every '+' (DAY and SES) and every 'See More…' using batched JS
    clicks, refreshing the id snapshot before each phase so we never depend on
    positional locators against a mutating DOM. Sessions are expanded in small
    batches with a hourglass-settle wait between them, then a one-at-a-time
    mop-up handles any stragglers. Ported from a sibling fetcher."""
    for outer in range(max_outer_rounds):
        p = _planner_pending_ids(page)
        nd, ns, nm, nh = (len(p["day_pluses"]), len(p["session_pluses"]),
                          len(p["see_more"]), len(p["hourglasses"]))
        print(f"[planner]   [expand {outer+1}] DAY+={nd} SES+={ns} "
              f"SeeMore={nm} Hourglass={nh}")
        if nd + ns + nm + nh == 0:
            page.wait_for_timeout(500)
            return
        if nh and not (nd or ns or nm):
            _planner_wait_hourglasses(page)
            continue
        if p["day_pluses"]:
            _planner_click_batch(page, p["day_pluses"], wait_after_ms=500)
        for _ in range(50):
            sm_ids = page.evaluate(
                "() => Array.from(document.querySelectorAll("
                "'[id^=\"LEVELSHOWNORECORDS:\"]')).map(el => el.id)")
            if not sm_ids:
                break
            _planner_click_batch(page, sm_ids, wait_after_ms=1200)
        ses_ids = page.evaluate(
            "() => Array.from(document.querySelectorAll("
            "'img[id^=\"SES:\"][src*=\"plus\"]')).map(el => el.id)")
        if ses_ids:
            BATCH = 10
            for i in range(0, len(ses_ids), BATCH):
                _planner_click_batch(page, ses_ids[i:i + BATCH],
                                     wait_after_ms=200)
                _planner_wait_hourglasses(page, max_wait_ms=20000)
            page.wait_for_timeout(500)
    # One-at-a-time mop-up of any leftover pluses / stuck hourglasses.
    for _ in range(6):
        p = _planner_pending_ids(page)
        ns, nh = len(p["session_pluses"]), len(p["hourglasses"])
        if p["day_pluses"]:
            _planner_click_batch(page, p["day_pluses"], wait_after_ms=500)
        if p["see_more"]:
            _planner_click_batch(page, p["see_more"], wait_after_ms=1200)
        if not (ns or nh):
            break
        for pid in p["session_pluses"]:
            _planner_click_batch(page, [pid], wait_after_ms=200)
            _planner_wait_hourglasses(page, max_wait_ms=20000)
        still = page.evaluate(
            "() => Array.from(document.querySelectorAll("
            "'img[id^=\"SESSION:\"][src*=\"hourglass\"]')).map(el => el.id)")
        for hid in still:
            waited = 0
            while waited < 25000:
                present = page.evaluate(
                    "(id) => { const el = document.getElementById(id);"
                    " return !!(el && /hourglass/.test(el.src)); }", hid)
                if not present:
                    break
                page.wait_for_timeout(500)
                waited += 500
    p = _planner_pending_ids(page)
    leftover = sum(len(p[k]) for k in p)
    if leftover:
        print(f"[planner]   WARNING: {leftover} element(s) still pending "
              "after expansion; some sessions may be missing in the DOM.")


def _fetch_planner_dom() -> bool:
    """Render the conference planner, expand every day/session/See-More control,
    and save the fully-expanded DOM to PLANNER_OUTPUT_HTML. Returns True on a
    successful save. Headless; never raises — logs and returns False so a flaky
    planner can't abort the rest of the download (the PDFs remain authoritative
    and the presider map is optional enrichment)."""
    _bootstrap_playwright()
    import time
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    PLANNER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[info] rendering conference planner (headless) from {PLANNER_URL} "
          "to harvest per-session presiders…")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PLANNER_PROFILE_DIR), headless=True, accept_downloads=False,
            viewport={"width": 1500, "height": 950},
            args=["--disable-blink-features=AutomationControlled"])
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(PLANNER_URL, wait_until="domcontentloaded", timeout=60000)
            # planner.jsp is a JavaScript shell filled in asynchronously; wait
            # for the side-menu link, then for the DAY rows to render.
            try:
                page.wait_for_selector("#BROWSE_THE_PROGRAM", timeout=45000)
            except PWTimeout:
                print("[warn]   planner side-menu link didn't appear in 45s.")
            if page.locator("[id^='DAY:']").count() == 0:
                try:
                    page.locator("#BROWSE_THE_PROGRAM").first.click(timeout=5000)
                except Exception:
                    pass
            deadline = time.time() + 180
            while time.time() < deadline:
                if page.locator("[id^='DAY:']").count() > 0:
                    break
                page.wait_for_timeout(750)
            _planner_expand_everything(page)
            html_text = page.evaluate("() => document.documentElement.outerHTML")
            # Inject a <base href> so a later manual open resolves the site's
            # relative assets (cosmetic; the markup itself is complete).
            try:
                base_url = page.url
                if base_url and "<base" not in html_text[:2000].lower():
                    html_text = re.sub(
                        r"(<head[^>]*>)", r'\1<base href="' + base_url + '">',
                        html_text, count=1, flags=re.I)
            except Exception:
                pass
            with open(PLANNER_OUTPUT_HTML, "w", encoding="utf-8") as f:
                f.write("<!DOCTYPE html>\n")
                f.write(html_text)
            size_kb = PLANNER_OUTPUT_HTML.stat().st_size / 1024
            print(f"[ok]   saved {PLANNER_OUTPUT_HTML.name} ({size_kb:,.1f} KB).")
            return True
        except Exception as e:  # noqa: BLE001 — never abort the whole download
            print(f"[warn]   could not save the planner DOM: {e}. "
                  "Sessions will have no presiders.")
            return False
        finally:
            ctx.close()


def _bootstrap_playwright() -> None:
    """Ensure the 'playwright' package and its Chromium browser are installed.
    Mirrors the sibling fetchers' bootstrap so a fresh checkout self-provisions."""
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        print("[setup] Installing the 'playwright' Python package…")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet",
             "playwright>=1.40"])
    # Installing the browser is idempotent and a no-op when already present.
    subprocess.check_call(
        [sys.executable, "-m", "playwright", "install", "chromium"])


def _fetch_event_schedule() -> tuple[int, list[str]]:
    """Render and save the per-day event-site schedule pages.

    Returns (saved_count, failed_filenames). Never raises for a single day's
    failure — it logs, records the filename, and moves on, so a flaky day or an
    unsolved CAPTCHA doesn't abort the whole download. The PDFs remain the
    pipeline's required inputs; these pages are optional enrichment."""
    _bootstrap_playwright()
    import time
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    EVENT_SCHEDULE_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    saved = 0
    failed: list[str] = []
    print("[info] rendering event-site schedule pages via headed Chromium "
          "(a browser window will open)…")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(EVENT_SCHEDULE_PROFILE_DIR),
            headless=False,
            user_agent=EVENT_SCHEDULE_UA,
            viewport={"width": 1400, "height": 1000},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            for day, fname in EVENT_SCHEDULE_DAYS.items():
                target = DATA_DIR / fname
                try:
                    page.goto(EVENT_SCHEDULE_URL + day,
                              wait_until="domcontentloaded", timeout=60000)
                    # Bot wall: a headed browser normally passes, but if the
                    # bot-wall interstitial shows, wait for the user to clear it.
                    waited = 0
                    while ("perfdrive" in page.url
                           or "captcha" in page.title().lower()):
                        if waited == 0:
                            print(f"[warn]   {day}: bot-wall/CAPTCHA detected — "
                                  f"please solve it in the open browser window "
                                  f"(waiting up to {EVENT_SCHEDULE_WALL_WAIT_S}s)…")
                        time.sleep(3)
                        waited += 3
                        if waited >= EVENT_SCHEDULE_WALL_WAIT_S:
                            raise PWTimeout("bot wall not cleared in time")
                    # Wait for the schedule to render its talk rows.
                    page.wait_for_selector("li.presentation", timeout=45000)
                    time.sleep(2)
                    # Switch to Detailed View so every cell shows its full meta.
                    try:
                        page.get_by_text("Detailed View",
                                         exact=True).click(timeout=8000)
                        time.sleep(2)
                    except PWTimeout:
                        print(f"[warn]   {day}: 'Detailed View' toggle not "
                              f"found; saving the default view.")
                    # Expand every truncated abstract ("Continue Reading").
                    expanded = page.evaluate(
                        "() => { let c = 0;"
                        " document.querySelectorAll("
                        "'a.presentation__description-expand').forEach(a => {"
                        " if (/Continue Reading/i.test(a.innerText)) {"
                        " a.click(); c++; } }); return c; }")
                    time.sleep(2)
                    html_text = page.content()
                    target.write_bytes(html_text.encode("utf-8"))
                    npres = page.evaluate(
                        "() => document.querySelectorAll("
                        "'li.presentation').length")
                    size_kb = target.stat().st_size / 1024
                    print(f"[ok]   saved {fname} ({size_kb:,.1f} KB; "
                          f"{npres} talks, {expanded} abstracts expanded).")
                    saved += 1
                except Exception as e:  # noqa: BLE001 — log & continue per day
                    print(f"[warn]   {day}: could not save {fname}: {e}")
                    failed.append(fname)
        finally:
            ctx.close()
    return saved, failed


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def _fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def _date_key(url: str) -> tuple[int, int, str]:
    """Sort key for picking the freshest re-issue of a PDF: extract (month, day)
    from the trailing _<d>_<m>.pdf suffix; fall back to the URL itself so the
    sort is total even when no date can be parsed."""
    m = DATE_RE.search(url)
    if not m:
        return (0, 0, url)
    day, month = int(m.group(1)), int(m.group(2))
    return (month, day, url)


def _pick_latest(html: str, pat: re.Pattern[str]) -> str | None:
    candidates = sorted(set(pat.findall(html)), key=_date_key, reverse=True)
    return candidates[0] if candidates else None


def main() -> None:
    print("=" * 72)
    print("[config] conference DOWNLOADER starting up.")
    print(f"[config]   script dir   : {SCRIPT_DIR}")
    print(f"[config]   data dir     : {DATA_DIR}")
    print(f"[config]   programme URL: {PROGRAMME_URL}")
    print(f"[config]   run date     : {date.today().isoformat()}")
    print("=" * 72)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Fetch the programme HTML once, lazily — only if at least one artifact is
    # link-discovery (pattern) based.
    needs_programme_html = any("pattern" in a for a in ARTIFACTS)
    html = ""
    if needs_programme_html:
        print(f"[info] fetching programme page to discover PDF links …")
        try:
            html = _fetch_text(PROGRAMME_URL)
        except urllib.error.URLError as e:
            print(f"[fatal] could not fetch {PROGRAMME_URL}: {e}")
            sys.exit(1)
        print(f"[info]   fetched {len(html):,} chars of HTML.")

    saved_any = False
    failed: list[str] = []
    for art in ARTIFACTS:
        target = DATA_DIR / art["name"]
        if "url" in art:
            # Fixed-URL artifact: download directly.
            url = art["url"]
        else:
            # Pattern artifact: find the freshest matching link on programme
            # page and download that.
            url = _pick_latest(html, art["pattern"])
            if not url:
                print(f"[warn] no link matching {art['desc']} found on the "
                      f"programme page; cannot fetch {art['name']}.")
                failed.append(art["name"])
                continue
        print(f"[info] downloading {art['desc']} from {url}")
        try:
            body = _fetch_bytes(url)
        except urllib.error.URLError as e:
            # Optional artifacts (e.g. the agenda PDF, served from a CDN path
            # that can rotate) only warn softly and don't count as a retrieval
            # failure — the processor falls back gracefully without them.
            if art.get("optional"):
                print(f"[note]   optional {art['name']} unavailable ({e}); "
                      f"skipping — the processor will fall back.")
            else:
                print(f"[warn]   download failed: {e}")
                failed.append(art["name"])
            continue
        target.write_bytes(body)
        size_kb = target.stat().st_size / 1024
        print(f"[ok]   saved {target.name} ({size_kb:,.1f} KB).")
        saved_any = True

    # Render + save the per-day event-site schedule pages (browser-driven).
    # These are optional enrichment, so a failure here is a warning, not fatal.
    print("-" * 72)
    try:
        sched_saved, sched_failed = _fetch_event_schedule()
        if sched_saved:
            saved_any = True
        failed.extend(sched_failed)
    except Exception as e:  # noqa: BLE001 — never let enrichment abort the run
        print(f"[warn] event-site schedule fetch failed entirely: {e}")
        failed.extend(EVENT_SCHEDULE_DAYS.values())

    # Render + save the conference planner DOM (browser-driven). Optional
    # enrichment used only for the per-session presider map; a failure here is
    # a warning, not fatal.
    print("-" * 72)
    try:
        if _fetch_planner_dom():
            saved_any = True
        else:
            failed.append(PLANNER_OUTPUT_HTML.name)
    except Exception as e:  # noqa: BLE001 — never let enrichment abort the run
        print(f"[warn] planner DOM fetch failed entirely: {e}")
        failed.append(PLANNER_OUTPUT_HTML.name)

    print()
    print("=" * 72)
    if failed:
        print(f"DONE WITH WARNINGS — {len(failed)} file(s) not retrieved:")
        for n in failed:
            print(f"  - {n}")
        print("Re-check the programme page or see data_requirements_ecio2026.txt "
              "for the manual fallback.")
    else:
        print("DONE (downloaded program PDFs). Next: run process_program_ecio2026.py")
    print(f"  data dir : {DATA_DIR}")
    print("=" * 72)
    if not saved_any:
        sys.exit(1)


if __name__ == "__main__":
    main()
