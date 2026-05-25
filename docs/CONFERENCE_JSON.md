# `conference_data.json` Input Format

`build_conference_app.py` turns a single data file, **`conference_data.json`**,
into a self-contained phone-friendly HTML app (`conference_app.html`). This
document describes everything that file needs to contain.

The builder does almost no conference-specific work. It expects your processor
to have already done all the hard parts (author/speaker resolution, presider
scraping, type classification, abstract rendering) and to bundle the result
into this one JSON file. The builder then only does two things: it shortens
affiliations, and it splices the data into the HTML template.

## Where the file goes

Put `conference_data.json` in the **same directory** as `build_conference_app.py`,
then run:

```
python build_conference_app.py
```

It writes `conference_app.html` next to itself. An optional
`build_affiliation_map.py` in the same directory is used to shorten
affiliations if present; without it the builder falls back to a keyword
heuristic.

## The schema is source-agnostic

Nothing in the schema names where a value came from. A completely different
conference with a completely different processor can emit the same shape and
get a working app. The keys below are the contract.

## Top-level shape

```json
{
  "conference_name": "CLEO 2026",
  "curator": { ... },
  "sessions": [ ... ],
  "talks": [ ... ],
  "session_types": [ ... ],
  "talk_types": [ ... ],
  "affiliation_sources": [ ... ]
}
```

| Key | Required | Purpose |
|-----|----------|---------|
| `conference_name` | Recommended | Page `<title>`, the "My Notes" export header, and the Sessions/Talks page headings (rendered as `"<name> Sessions"` / `"<name> Talks"`). Falls back to `"Conference"` if missing or empty. |
| `curator` | Optional | Curator credit shown at the bottom of the About section. Omit (or leave the name empty) to show only the app-author line. |
| `sessions` | **Yes** | The list of sessions. |
| `talks` | **Yes** | The list of talks. |
| `session_types` | Optional | Type registry and colors for the Sessions tab. Built-in defaults used if absent. |
| `talk_types` | Optional | Type registry and colors for the Talks tab. Built-in defaults used if absent. |
| `affiliation_sources` | Optional | One flat, de-duplicated list of raw affiliation strings the affiliation shortener learns from. |

## Timestamps

Every `start_ts` / `end_ts` is a string parsed directly by JavaScript's
`new Date(...)`, so use **ISO 8601** (e.g. `"2026-05-10T09:00:00"` or with an
offset `"2026-05-10T09:00:00-05:00"`). The day filter keys off the first 10
characters (`YYYY-MM-DD`), so that prefix must be a real calendar date.

Items with no timestamps still load (they are treated as undated and pass
through filters), but they will not sort or group by time, will not appear
under "Now", and cannot be hidden as "past".

## Color tokens

Each session and talk carries a `color` token (e.g. `"blue"`, `"violet"`,
`"orange"`). That token does triple duty: it sets the accent color, it is the
id the Types panel filters on, and it maps to a human label via the type
registries. Any token works; unknown tokens render gray unless the registry
supplies RGB for them.

---

## `sessions[]`

A session is the structurally larger unit (it owns talks). Fields:

| Field | Required | Notes |
|-------|----------|-------|
| `id` | **Yes** | Unique string. Talks reference it via `session_id`; sessions list their children via `talk_ids`. |
| `title` | **Yes** | Display title. A single trailing period is stripped automatically (an ellipsis `...` is kept). |
| `color` | **Yes** | Type/color token (see above). Drives accent and Types-panel filtering. |
| `tags` | Optional | Ordered list of labelled facts about the session — `{ "key", "value" }` pairs (see **Tags** below). Rendered in the detail header (as `Key: Value · …`) and searchable. |
| `start_ts` | Recommended | ISO start. Needed for time grouping, "Now", past-hiding, day filter. |
| `end_ts` | Recommended | ISO end. Needed to compute past/in-progress. |
| `location` | Optional | Room/venue. The **full** form, shown in the detail header. |
| `short_location` | Optional | Compact room/venue for the bubble chip in lists/search/schedule (e.g. `"MS 151 (U Mezz)"` for `"Moscone South, Room 151 (Upper Mezz)"`). When absent, bubbles fall back to `location`. Talks inherit their session's `short_location` unless they set their own. |
| `presider` | Optional | Presider name(s). Multiple separated by `;` or ` and `. |
| `presider_aff` | Optional | **RAW** presider affiliation string(s), `;`-separated and positionally aligned to `presider` names. The builder shortens these and may backfill missing ones from papers the presider authored. |
| `details` | Optional | Free-text shown in the detail header. |
| `talk_ids` | Recommended | Ordered list of child talk `id`s. Drives the talk list inside a session detail. |

The builder **adds** `presider_aff_short` and `presider_affs_short` (you do not
supply these). Sessions have no `withdrawn` flag.

### Tags

`tags` is an ordered list of labelled facts about a session — whatever extra
information you happen to have. Each entry is a `{ "key": ..., "value": ... }`
pair: `key` names what the fact is, `value` is the fact itself.

```json
"tags": [
  { "key": "Session Type", "value": "FS Oral" },
  { "key": "Session Topic", "value": "Fundamental Science 1" },
  { "key": "SPIE subconference", "value": "Quantum West" }
]
```

Common keys are **Format** (oral / poster / plenary / short course …) and
**Track** (the program track or theme), but anything goes — SPIE
subconference, symposium name, session chair, etc. Use whatever a given
conference actually provides; there is no fixed vocabulary.

The app renders the tags **in array order**, each as `Key: Value`, joined with
` · `. The example above shows as:

> Format: FS Oral · Track: Fundamental Science 1 · SPIE subconference: Quantum West

Put the most important tag first. Omit `tags` (or leave it empty) for sessions
with nothing extra to show.

### Minimal session

```json
{
  "id": "S-12",
  "title": "Quantum Cascade Lasers and Frequency Combs I",
  "color": "violet",
  "tags": [
    { "key": "Format", "value": "Contributed Session" },
    { "key": "Track", "value": "Example Track" }
  ],
  "start_ts": "2026-05-10T09:00:00",
  "end_ts": "2026-05-10T11:00:00",
  "location": "Room 201",
  "presider": "Alex Rivera",
  "presider_aff": "Institute for Quantum Electronics, Example University, Springfield, Country",
  "talk_ids": ["T-101", "T-102", "T-103"]
}
```

---

## `talks[]`

A talk belongs to a session (the presence of `session_id` is literally how the
app distinguishes a talk from a session). Fields:

| Field | Required | Notes |
|-------|----------|-------|
| `id` | **Yes** | Unique string. |
| `session_id` | **Yes** | The parent session's `id`. Its presence marks this item as a talk. |
| `title` | **Yes** | Display title; trailing period stripped (ellipsis kept). |
| `color` | **Yes** | Type/color token. |
| `start_ts` / `end_ts` | Recommended | ISO times (same role as sessions; `end_ts` also drives "past"). |
| `number` | Optional | Talk/paper number. |
| `location` | Optional | Room, if different from the session (full form). |
| `short_location` | Optional | Compact room for the bubble chip (see `short_location` under sessions). Usually omitted on talks so they inherit the session's. |
| `speaker` | Recommended | Presenting author's name; bolded in bylines and the author list. |
| `speaker_pos` | Optional | Integer index of the speaker in the author list (`0` = first). Helps the byline bold the right name when the name match is ambiguous. |
| `presenter` | Optional | Alternative presenter label if your data distinguishes it from `speaker`. |
| `first_author` / `last_author` | Recommended | Used to build the compact byline (`First ... Last`) on bubbles. |
| `authors` | Recommended | Ordered author list (see below). |
| `author_aliases` | Optional | Loose name forms (e.g. initials) kept **for search only**; never displayed. Used as a fallback author line when `authors` is absent. |
| `institutions` | Recommended | Numbered institution list (see below). |
| `institutions_may_dedup` | Optional | `true` lets the builder collapse duplicate institutions by short name and renumber. Only set this when authors carry no `insts` references to protect. |
| `abstract` | Optional | Abstract text. Literal `<sup> <sub> <i> <b> <em> <strong>` tags are rendered; everything else is escaped. |
| `status` | Optional | Shown as "Status: ..." unless it is `"sessioned"`. |
| `withdrawn` | Optional | `true` hides the talk by default (revealed by "Show concluded"). |

The builder **adds** `inst_shorts`, `speaker_aff`, and `last_aff` (you do not
supply these).

### `authors[]`

Ordered list of author objects:

```json
"authors": [
  { "name": "Jordan Lee", "insts": [1] },
  { "name": "Sam Taylor",  "insts": [2] }
]
```

- `name`: display name.
- `insts`: the **explicit institution numbers** (the `n` values in
  `institutions`) this author belongs to. An empty list means "unknown / no
  structured affiliation". These are rendered as superscripts and must match
  the `n` values, **not** list positions.

### `institutions[]`

Numbered institution list:

```json
"institutions": [
  { "n": 1, "name": "Department of Electrical and Computer Engineering, Example University, Springfield, ST, Country", "alt_names": ["Example University"] },
  { "n": 2, "name": "Sample Research Lab, Metropolis, ST, Country", "alt_names": ["Sample Lab"] }
]
```

- `n`: the explicit number authors reference via `insts`. Numbering need not be
  `1..N` or contiguous; the app renders whatever `n` you give.
- `name`: the RAW long form (often a full department-prefixed address).
- `alt_names`: optional cleaner variants; the shortener tries these
  cleanest-first before the detailed `name`.

> **Why the numbers matter:** author `insts` point at institution `n` values.
> If you set `institutions_may_dedup: true`, the builder may renumber, so only
> enable it when no author depends on the original numbering.

### Minimal talk

```json
{
  "id": "T-101",
  "session_id": "S-12",
  "title": "Example talk title",
  "color": "indigo",
  "number": "SM1A.1",
  "start_ts": "2026-05-10T09:00:00",
  "end_ts": "2026-05-10T09:15:00",
  "speaker": "Jordan Lee",
  "speaker_pos": 0,
  "first_author": "Jordan Lee",
  "last_author": "Casey Morgan",
  "authors": [
    { "name": "Jordan Lee", "insts": [1] },
    { "name": "Casey Morgan", "insts": [1] }
  ],
  "institutions": [
    { "n": 1, "name": "Department of Electrical and Computer Engineering, Example University, Springfield, ST, Country" }
  ],
  "abstract": "Example abstract text, demonstrating inline <i>italic</i> and subscript f<sub>rep</sub> markup ...",
  "status": "sessioned",
  "withdrawn": false
}
```

---

## `session_types[]` and `talk_types[]`

Each is a list of `{ id, label }` entries where **`id` is the color token**
the app filters and groups on, and `label` is what shows in the Types panel.
The order of the list is the order shown in that panel.

```json
"session_types": [
  { "id": "blue",   "label": "Applications & Technology" },
  { "id": "violet", "label": "Fundamental Science" },
  { "id": "orange", "label": "Other Sessions" }
],
"talk_types": [
  { "id": "indigo", "label": "Invited" },
  { "id": "rose",   "label": "Postdeadline" },
  { "id": "pink",   "label": "Contributed" }
]
```

If a token means different things in the two tabs (e.g. `orange` is "Other
Sessions" vs "Plenary & Tutorial"), the Search/union view shows both labels
joined with `/`.

### Custom colors (optional)

Any type entry may also carry RGB so a brand-new token gets real colors instead
of the gray fallback:

```json
{ "id": "sky", "label": "Comb Workshops",
  "fg": "#0284c7", "bg_light": "#e0f2fe", "bg_dark": "#0c2733" }
```

- `fg`: accent / left-border color.
- `bg_light`: bubble background in light mode.
- `bg_dark`: bubble background in dark mode.

Entries without RGB keep whatever the static CSS defines (or gray). If you omit
`session_types` / `talk_types` entirely, the builder uses its built-in defaults
(`blue/violet/emerald/amber/orange` for sessions; `orange/indigo/rose/teal/
slate/pink` for talks).

---

## `curator` (optional)

An optional credit shown at the bottom of the app's About section. When
present (and carrying a non-empty `name`), the app renders a line just below
the app name:

```
The Fine Conference App v0.1

<conference_name> curated by <name>, <affiliation>
App by David Burghoff, UT Austin
```

The `<name>, <affiliation>` text links to `link` when one is supplied, and is
shown as plain (muted) text when it isn't.

```json
"curator": {
  "name": "Alex Rivera",
  "affiliation": "Example University",
  "link": "https://example.org/curator"
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `name` | **Yes** (within the block) | Curator display name. If this is empty or the whole `curator` block is absent, no curator line is shown — the About section stays as the app name plus the app-author line. |
| `affiliation` | Optional | Appended after the name as `"<name>, <affiliation>"`. Omitted (with its comma) when empty. |
| `link` | Optional | If present, the `"<name>, <affiliation>"` text becomes a link to this URL; otherwise it is plain text. |

If there is no curator, the About section is left exactly as-is.

---

## `affiliation_sources` (optional)

One flat, de-duplicated list of raw affiliation strings the affiliation
shortener learns from. The builder hands this list straight to
`build_affiliation_map.py`; you do not pre-shorten anything. Pool every raw
form you have — full multi-field address lines, presider affiliations, and bare
institution names all go in the same list. Split any `;`-joined lists yourself
so each entry is a single affiliation string.

```json
"affiliation_sources": [
  "Department of Electrical and Computer Engineering, Example University, Springfield, ST, Country",
  "Institute for Quantum Electronics, Example University, Springfield, Country",
  "Example University",
  "..."
]
```

The list is optional; supply whatever raw forms you have. Without it (or without
`build_affiliation_map.py`), the builder still works using a keyword heuristic
to shorten affiliations.

---

## What you supply vs. what the builder adds

You provide everything above. The builder computes and injects these, so **do
not** put them in your JSON or they will be overwritten:

- Talks: `inst_shorts`, `speaker_aff`, `last_aff` (and a renumbered
  `institutions` if `institutions_may_dedup` is set).
- Sessions: `presider_aff_short`, `presider_affs_short` (plus backfilled
  `presider_aff` where it was missing and could be inferred).

## Quick checklist

- [ ] `conference_data.json` sits next to `build_conference_app.py`.
- [ ] Every session has a unique `id`; every talk has a unique `id` and a
      `session_id` pointing at a real session.
- [ ] `talk_ids` on each session lists its children in order.
- [ ] Timestamps are ISO 8601 with a valid `YYYY-MM-DD` prefix.
- [ ] Every item has a `color` token, and your type registries label the tokens
      you actually use.
- [ ] Author `insts` numbers match `institutions[].n` values (unless
      `institutions_may_dedup` is `true`).
