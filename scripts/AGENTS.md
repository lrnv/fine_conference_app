# AGENTS.md — Adding a new conference

This guide is for AI coding agents helping a user curate a new conference for
the Fine Conference App. Agentic coding is fine here: fetchers, processors,
and requirements manifests are mechanical glue around a conference-specific
source, and an agent can often do the bulk of the work after a short
conversation with the user.

Before you do anything else, internalize the rule below.

## The rule that matters most: no hardcoded conference content

**Conference content must never be hardcoded into any tracked source file.**
That includes the fetcher (`fetch_program_<slug>.py`), the processor
(`process_program_<slug>.py`), the requirements manifest, and any helper you
add. Specifically forbidden in source code:

- Paper titles and abstracts.
- Author names, speaker names, presider names, affiliations.
- Session titles, talk numbers, specific time slots, room names.
- Anything else from the program that you would not invent yourself.

All such content must be **extracted at runtime** from files in `data/`. The
processor reads those files when it runs; the fetcher (or the user) puts them
there. **Nothing of substance from the program goes into the Python source.**

This includes **comments, docstrings, and examples**. Do not paste a real paper
title, abstract, author name, or affiliation into a comment to illustrate a
parser — even a fragment, even "just as an example." Program text is copyrighted
wherever it lives, and a docstring in tracked source is just as public as a
string literal. When you need to show the shape of an input line in a comment,
use **invented placeholders** (`SESSION N: <SESSION TITLE>`, `Author(s): <Name>,
<Aff> (<Country>)`) — never a line copied from the actual program.

This is not a stylistic preference. Conference programs — titles, abstracts,
author lists — are typically copyrighted by the conference and its publisher.
Embedding any of that in tracked source code turns the repository into an
unlicensed redistribution of copyrighted material. Keep program content on the
user's machine, in `data/`, where it stays.

What IS fine in source code:
- The conference's short slug (e.g. `cleo2026`).
- Parsing format: CSS selectors, regex shapes, PDF layout heuristics, column
  names, header strings the source file itself uses — these describe FORMAT,
  not content.
- Generic type labels in a registry ("Invited", "Contributed", "Poster",
  "Plenary"). These are universal genre labels, not copyrighted content.
- The conference's display name — but PREFER deriving it at runtime from a
  source file (page title, PDF header, etc.) over hard-coding it. If a clean
  runtime source isn't available, a single obvious top-level constant near the
  top of the processor (e.g. `CONFERENCE_NAME = "..."`) is an acceptable
  fallback the user can review; keep it to that one place. It ends up as
  `conference_name` in the JSON output. (See "Keep platform and conference
  identity out of scripts" below.)

If you're tempted to write a fixture, mock, or "default" that contains program
content, stop. That belongs in `data/`, in a file the user supplies or the
fetcher downloads.

## Keep platform and conference identity out of scripts

Two related habits keep the tracked code clean of names it doesn't need.

**Never name a third-party platform or vendor — in comments OR in code.** The
sites a fetcher scrapes run on branded products (abstract-submission systems,
event-site hosts, bot-protection services, the JavaScript framework a planner
happens to use, …). Those trademarks have no business in this repo. Describe
them by ROLE, generically:

- the abstract/submission system or planner → "the conference planner"
- the publisher's schedule mirror → "the event-site schedule"
- a bot wall / CAPTCHA vendor → "the bot wall" / "bot protection"
- the planner's UI framework → "a legacy JavaScript app"

This applies to comments, docstrings, log/print strings, and identifiers
(variable, function, and constant names). The ONE exception is a **functional
URL** the fetcher must request — you obviously can't download without it, so the
host in a URL string stays. Everything else describing that host should be
generic.

**Avoid the conference's name in scripts; let the directory carry it.** The
subdirectory slug (e.g. `conferences/<slug>/`) is the one place the conference
is named, and that's enough — it scopes every file inside. In the scripts
themselves, prefer "the conference"/"the program" over the acronym+year, and
fetch the display name rather than hard-coding it (see the display-name bullet
above). A docstring may still reference the conference where genuinely needed
for clarity, but reach for the generic phrasing first.

Both rules are about the SOURCE you commit, not the data it produces: the
`conference_name`, vendor-hosted URLs, and program content all still flow
through at runtime — they just shouldn't be baked into tracked comments or
identifiers.

## Workflow for a new conference

1. **Pick a slug.** Lowercase conventional acronym plus year, e.g. `cleo2026`,
   `iqclsw2026`, `ecio2026`. The slug names the directory and is reused in
   every file name inside it.

2. **Ask the user where the program data lives.** Don't guess. Two paths:

   - **URL(s) the fetcher can download from.** Ask for the exact URL(s) of the
      program — the schedule HTML, abstract book PDF, CSV export, etc.
      Implement `fetch_program_<slug>.py` to download those into `data/`. If
      the source needs a login, see the login section below.

      **Explore the rest of the site, don't stop at the one URL the user
      gave.** A conference website usually spreads program content across
      several linked pages, and the user will rarely enumerate them all. When
      you're given a site, follow its own
      navigation and look for any page that carries programmatic information —
      a Program / Schedule / Agenda page, a Speakers / Invited Speakers /
      Plenaries page, an Events / Social Events overview, Short Courses /
      Tutorials / Workshops, Posters, an abstract book or proceedings link,
      etc. Fetch each such page into `data/` (one file per source) and fold
      whatever is relevant into `conference_data.json`: speaker pages often
      supply the presider/invited-speaker names and affiliations the schedule
      omits, events pages add non-talk sessions (receptions, ceremonies, lab
      tours), and so on. Prefer pulling this from the site over leaving the
      program incomplete. The same rules still apply — no program content in
      tracked source (it lives in `data/`), respect terms and any login
      wall, and skip pages that are purely marketing with no program substance.
      When in doubt about whether a page is worth including, ask the user.

   - **Manual files.** If automated download is not viable (no public URL,
      complex auth, terms of service that forbid scraping, etc.), the user
      drops files into `data/` themselves. Write a minimal
      `fetch_program_<slug>.py` that prints a clear "please supply
      `<filenames>` in `data/`" message and exits, and make
      `data_requirements_<slug>.txt` mark each input as required with a
      `manual:` field describing where the user obtains it.

   When unsure which path applies to a given file, ask the user explicitly.

3. **Write `data_requirements_<slug>.txt`.** One `[file: <pattern>]` block per
   required input, with `required:`, `description:`, `produced_by:` (the
   fetcher script name, if any), and `manual:` (instructions for obtaining the
   file by hand) keys. `manual:` is what the user sees when a file is missing
   — make it specific (URL, page, click path). See any existing conference's
   manifest, and `_parse_requirements()` in `scripts/make_app.py` for the
   canonical parser.

4. **Write `process_program_<slug>.py`.** Read the files in `data/`, parse
   them at runtime, and emit `conference_data.json` matching the schema in
   `docs/CONFERENCE_JSON.md`. Constraints:

   - Reads from `SCRIPT_DIR / "data"` (relative to the processor's own path).
   - Writes to `SCRIPT_DIR / "conference_data.json"`.
   - Does no network access — the fetcher is the only place that touches the
     network.
   - Hardcodes no titles, abstracts, names, or other program content.
   - Assigns every session and talk a type from the standard taxonomy in
     [Standard session and talk types](#standard-session-and-talk-types) below.
     Do not invent new type names or colors — map the conference's real
     program onto the seven canonical types.

5. **Verify with `make_app.py`.** From the repo root:

   ```bash
   python scripts/make_app.py <slug>
   ```

   This runs the full pipeline (fetch -> verify -> process -> build) and
   writes `conferences/<slug>/<slug>_app.html`. Open it in a browser and
   iterate on the processor until the program renders correctly.

6. **Add the conference to the affiliation-map regression suite.** Once the
   processor is stable, register the new conference's affiliation strings with
   the regression harness at `scripts/tests/` so any future tweak to anchors
   or the fallback shortener can't silently change its canonical short names:

   ```bash
   python scripts/tests/make_fixture.py conferences/<slug>/conference_data_<slug>.json
   pytest -k <slug> --update-golden
   ```

   The first command writes
   `scripts/tests/fixtures/<slug>.affiliation_sources.json` (the trimmed input
   the test reads — just the `affiliation_sources` list); the second writes
   `scripts/tests/golden/<slug>.expected.txt` (the frozen `{raw -> short}`
   mapping). Eyeball the golden once to make sure the short names look right,
   then commit both files. See `scripts/tests/README.md` for the full workflow,
   including how `--update-golden` produces `.expected.new` proposal files when
   an existing conference's map would change so a human can review the diff
   before promoting it.

## Standard session and talk types

These seven types are the **recommended** taxonomy, and conferences should reuse
them wherever they fit — the whole point is that a "Poster" or "Plenary" means
the same thing (and looks the same) in every conference, so prefer mapping the
real program onto these rather than inventing per-conference labels or colors.

That said, the taxonomy is a recommendation, not a hard constraint. If a
conference has content that genuinely does not fit any of the seven, you may
carve out an additional type (with its own color token + RGB triple). Do so
sparingly and only when the standard types would misrepresent the program;
reusing an existing type is almost always the better call.

### The model

- **A talk is technical content.** Anything with technical substance is a talk,
  even a lone plenary or keynote — emit it as a talk inside a singleton session
  if it has no natural parent. Very rarely a non-technical item is a talk-row
  (e.g. a coffee break listed inside an oral session); type those `Event`.
- **A session is a container.** Either (a) a grouping of talks, or (b) a
  non-technical event (ceremony, social, gala, meal, break, tour, exhibition).
- **Talks match their parent session's type where one exists.** Poster talks sit
  in a Poster session, tutorial talks in a Tutorial session, etc. The standard
  oral grouping (`Technical`) is the exception: it holds `Invited` /
  `Contributed` talks, which have no session-level equivalent.
- **No ambiguous names.** Sharing a name across levels is fine (`Poster` session
  / `Poster` talk); two *similar-but-different* names is the worst case and is
  forbidden. That is why the oral grouping is `Technical`, not `Oral` (SPIE uses
  "Oral" as a talk genre, which would collide).
- **Adjournment is an end marker, not content.** A program line like "Adjourn",
  "Adjournment", "Workshop Adjourns", or "Session ends" is *not* a session or a
  talk — it only records *when* the enclosing session (or the whole conference)
  ends. Do not emit it as either; instead use its timestamp to set the
  enclosing session's `end_ts` (or to backfill the preceding item's `end_ts`),
  then drop it. The same goes for any pure "the room closes now" marker that
  carries no speaker, title, or content of its own.
- **A bare withdrawn/cancelled marker is not an item.** A line that carries only
  a status word — "Withdrawn", "Cancelled"/"Canceled", "(Cancelled)", "No show",
  or similar — with no title, speaker, or abstract of its own must NOT be emitted
  as a talk or a session. It only annotates the item it follows: drop the marker
  and, if the preceding talk/session is what it refers to, set that item's
  `withdrawn` flag. Emit a withdrawn/cancelled item ONLY when it actually carries
  content (a real title and/or author) — then keep it as that talk/session with
  `withdrawn: true`, which the app hides by default behind "Show concluded". In
  other words: content present → keep it, marked withdrawn; content absent → it's
  just a marker, so drop it (don't manufacture an empty "Cancelled" item).
- **Long-form event descriptions go in `details`; bare logistics do not.** When
  the program offers a genuine *description* of an event — a workshop/short-
  course abstract, an award's purpose, a social event's write-up — put that
  prose in the session's `details`. But do NOT put bare location/time logistics
  there (a one-liner of the shape "&lt;event&gt; will be held &lt;day&gt;
  &lt;start&gt;–&lt;end&gt; in &lt;venue&gt;"): that belongs in the usual fields
  — extract the venue into `location` and the times into `start_ts`/`end_ts`,
  and leave `details` for the substantive text (empty if, once the logistics
  sentence is removed, nothing of substance remains).

### The seven types

`id` is the color token; it is also the value each session/talk's `color` field
must carry. Ship the RGB triple for each token in the processor's
`COLOR_PALETTE` (see the CLEO processors) so the builder can synthesize the CSS.

| Type | `id` | fg | bg_light | bg_dark | Used by |
|------|------|------|----------|---------|---------|
| **Technical** | `blue` | `#2563eb` | `#e8efff` | `#1a233d` | sessions only — the default oral grouping |
| **Plenary** | `orange` | `#ea580c` | `#ffedd5` | `#3b1d0a` | sessions + talks (flagship lectures) |
| **Poster** | `teal` | `#0d9488` | `#d6f3ef` | `#102b27` | sessions + talks |
| **Tutorial** | `fuchsia` | `#c026d3` | `#fae8ff` | `#3a0f3f` | sessions + talks (didactic) |
| **Event** | `rose` | `#e11d48` | `#ffe1e8` | `#38161f` | sessions (+ rare talk-rows) — non-technical |
| **Invited** | `indigo` | `#4f46e5` | `#e6e4ff` | `#1d1a3d` | talks only |
| **Contributed** | `sky` | `#0284c7` | `#e0f2fe` | `#0c2a3d` | talks only |

So `SESSION_TYPES` = {Technical, Plenary, Poster, Tutorial, Event} (≤5) and
`TALK_TYPES` = {Invited, Contributed, Plenary, Poster, Tutorial, Event}.
`blue`/`indigo`/`sky` are a deliberate "blue family" — Technical sessions and the
Invited/Contributed talks inside them read as one coherent oral block.

### Mapping real program kinds onto the seven

Conference programs use many local labels. Fold them as follows; when in doubt,
ask "does this carry technical content?" (→ talk) and "is this just a container
or a non-technical event?" (→ session type).

| Real-world kind | Type |
|-----------------|------|
| Oral track, symposium, technical session, workshop *with named child talks* | `Technical` (session); talks inside are `Invited` / `Contributed` |
| Plenary lecture, keynote | `Plenary` (singleton session + the talk) |
| Invited oral talk, **industry talk** (solicited) | `Invited` |
| Contributed oral talk, **postdeadline** (late-breaking) | `Contributed` |
| Poster session, poster blitz, poster talk | `Poster` |
| Tutorial, **short course**, school lecture | `Tutorial` |
| Panel / discussion *with no named talks* | `Event` |
| Opening/closing ceremony, remarks, welcome reception, gala, banquet, networking, meal, coffee break, lab/city tour, registration, exhibition | `Event` |

### Folding decisions (do not reintroduce these as separate types)

- **Postdeadline → Contributed.** Late-breaking talks are Contributed; their
  sessions are Technical. No dedicated postdeadline color.
- **Industry talks → Invited.** They are solicited, so they read as Invited;
  there is no separate Industry type.
- **Short Course → Tutorial.** Short courses and tutorials are one didactic type.
- **Keynote → Plenary.** A keynote is a flagship singleton talk.
- **School / didactic lecture series → Tutorial**; the **research-talk grouping →
  Technical**. (Watch conferences like IQCLSW that label these "School" /
  "Workshop" — those local names must not leak into the types.)
- **Workshop / Panel → split by content:** if it has named child talks, it is a
  `Technical` session; if it is pure discussion with no talks, it is an `Event`.

## Login-required sources

If the program lives behind a login, the fetcher can use Playwright with the
Chromium browser launched **headed** (i.e. `headless=False`) so the user can
sign in interactively in the visible browser window. After the user logs in,
the fetcher continues and downloads what it needs. Persist the storage state
between runs (Playwright's `storage_state` JSON) so the user does not have to
log in every build. The CLEO fetchers under `conferences/cleo2025/` and
`conferences/cleo2026/` demonstrate this pattern.

If automating login is too involved or fragile, fall back to the manual path:
instruct the user in `data_requirements_<slug>.txt` to log in themselves and
drop the downloaded files in `data/`.

## References

Read these before writing code:

- **`conferences/test2026/`** — Synthetic, PDF-only, minimal. The cleanest
  reference for what a small, clean conference looks like end to end. Start
  here.
- **`docs/CONFERENCE_JSON.md`** — The exact `conference_data.json` schema:
  every field, every constraint, what is required and what is optional. The
  processor's output MUST match this.
- **`scripts/make_app.py`** — The orchestration contract. The module docstring
  lays out the directory layout, the file-naming conventions, the step-by-step
  flow, and the cache rules. Skim it before you write a fetcher or processor
  so you understand what `make_app.py` will expect to find.
- **`scripts/build_conference_app.py`** — The shared builder. You usually
  don't edit it; its top docstring documents what fields it reads from the
  JSON and how it derives the short forms the app renders.

Other examples, in rough order of complexity:

- **`conferences/iqclsw2026/`** — Small, HTML-based, no presiders, no per-talk
  abstracts.
- **`conferences/ecio2026/`** — An alternative pattern combining HTML and PDF
  sources.
- **`conferences/cleo2025/`, `conferences/cleo2026/`** — Larger, multi-source
  (PDF + official CSV + scraped HTML), with presider scraping. Use this
  complexity only if the conference genuinely needs it.

## Checklist before declaring "done"

- [ ] `python scripts/make_app.py <slug>` runs end-to-end without errors.
- [ ] `<slug>_app.html` opens in a browser and renders the program correctly,
      including session/talk lists, search, and detail pages.
- [ ] No paper titles, abstracts, author names, or session-specific content
      appears anywhere in `conferences/<slug>/*.py` or the requirements
      manifest. Grep your own work if you are not sure.
- [ ] `data_requirements_<slug>.txt` lists every input file with a clear
      `manual:` instruction so a user with no familiarity with the conference
      can obtain the files themselves.
- [ ] If using Playwright with login, the storage state persists between runs
      so re-runs do not require re-login.
- [ ] The slug-named JSON (`conference_data_<slug>.json`) and built app
      (`<slug>_app.html`) end up in `conferences/<slug>/`, not committed
      anywhere else.
- [ ] `scripts/tests/fixtures/<slug>.affiliation_sources.json` and
      `scripts/tests/golden/<slug>.expected.txt` exist (see Workflow step 6),
      and `pytest scripts/tests/test_affiliation_map.py` is green.
