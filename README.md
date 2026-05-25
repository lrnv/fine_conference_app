# The Fine Conference App

The Fine Conference App is a lightweight planner built to replace the clunky apps large conferences foist on attendees. It's a single webpage, with nothing to install, no account, no notifications, and no splash screen. Your schedule lives in your browser, syncs across your devices with a code, and feels at home on both phone and desktop.

<table>
  <tr>
    <td align="center"><img src="pngs/desktop.png" height="280" alt="Desktop layout"></td>
    <td align="center"><img src="pngs/mobile.png" height="280" alt="Mobile layout"></td>
  </tr>
  <tr>
    <td align="center">Desktop view</td>
    <td align="center">Mobile view</td>
  </tr>
</table>

## Why

Conference organizers often ship a large app that requires an install, an account, and a network connection just to browse a program you could read on a single modern page. The Fine Conference App takes the same program data and turns it into one self-contained HTML file. Open it in any browser and you have the whole conference. If you are organizing a conference, you can just host the page yourself and give your attendees a clean user experience (at no cost)!

## Features

- **One file, no install.** The build step produces a single `conference_app.html` with everything inlined. There are no external scripts, fonts, or network calls, so it loads instantly and works fully offline.
- **Build your own schedule.** Add any session or talk to a personal *My Schedule* view, and attach notes to individual talks.
- **Browse and search.** Separate tabs list every session and every talk, with full-text search across titles, authors, affiliations, and abstracts.
- **Filter the program.** Narrow what you see by day, by session/talk type, and hide concluded items so only what's still ahead remains.
- **Local storage that syncs.** Your schedule, notes, and preferences are saved in the browser's local storage so they persist between visits on that device. To move them to another computer or phone, copy a sync code from one device and paste it into another to merge the two schedules intelligently.
- **Mobile and desktop layouts.** On phones, the four tabs (Sessions, Talks, Search, Me) sit in a bottom bar. On wide screens, Me becomes a permanent, resizable side pane next to the program.

## Usage

If your conference already has a subdirectory, building its app is a single command from the root directory — just name the subdirectory:

```
python make_app.py <conference_name>
```

That's all that's needed for any conference that's already set up. The command will download any needed program files and put them in `data/` (if needed), run the processor to produce `conference_data.json`, and run the builder to produce the HTML app.

Once built, `<conference_name>_app.html` is the whole app. Open it directly in a browser, host it anywhere as a static file, or (if you're an organizer) send it to attendees. There's nothing else to deploy.

If your conference does **not** yet have a subdirectory, you'll need to set one up first — see [Adding a new conference](#adding-a-new-conference) below.

## How it works

The project is a small pipeline. Each conference lives in its own subdirectory and produces one self-contained HTML app. The two shared scripts in the root — `build_conference_app.py` and `build_affiliation_map.py` — are conference-agnostic and never need to change; everything specific to a given conference lives in its subdirectory.

For a conference that already has a subdirectory, `make_app.py` runs the whole pipeline for you (see [Usage](#usage) above). If the subdirectory doesn't exist yet, two pieces have to be created for that conference before it can be built:

1. **A way to download the program.** Each conference needs a downloader that fetches its raw source material (the program documents and schedule) and saves it into that conference's `data/` directory. This is different for every conference and can be done manually if needed.
2. **A way to generate the conference JSON.** Each conference also needs a processor that turns those raw files into a single, clean `conference_data.json` (the data file representing all of the conference data). All conference-specific work (recovering full author and speaker names, classifying session and talk types, rendering abstract math, attaching presiders, and so on) happens here.

Once those two exist and have produced a `conference_data.json`, the shared builder takes over: `build_conference_app.py` splices the JSON into the HTML template and writes the finished `conference_app.html`.

## Adding a new conference

If your conference doesn't have a subdirectory yet, create one with two scripts: a downloader and a processor.

**The downloader** (`fetch_program_<conf>.py`) is responsible for getting the conference's raw source material onto disk and saving it into the subdirectory's `data/` directory. This is the only part of the pipeline that touches the network. A downloader can use whatever approach fits your conference's source, or you can download program files manually. All that matters is that it ends with the required input files saved in `data/`.

**The processor** (`process_program_<conf>.py`) reads those raw files entirely offline and produces a single `conference_data.json` matching the schema documented at the top of `build_conference_app.py`. That schema is source-agnostic, so completely different conferences with completely different processors can emit the same shape and use the same builder.

Because the builder and the app itself are not conference-dependent, no changes to the shared scripts are needed once those two exist. The same `python make_app.py <conf>` command will then build your conference just like any other.

## Requirements

- Python 3 for the build pipeline.
- A modern web browser to open the built app. No runtime, server, or account is required.

## License

MIT. See the license header in each source file.

## A note on conference data

This repository contains only code. The program material a downloader fetches is copyrighted by the conference and its publisher. Do not commit those files (or a built `conference_app.html`, which embeds them) to a public repository or otherwise redistribute them. If you plan to share a built app with attendees, make sure you have the right to distribute the underlying program data.
