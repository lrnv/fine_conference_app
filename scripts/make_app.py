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

"""make_app.py — build one conference's app, from the ROOT directory.

This script lives in the ROOT directory (next to the shared
build_conference_app.py / build_affiliation_map.py) and operates on a
conference SUBDIRECTORY (or several) named on the command line:

    python make_app.py cleo2026
    python make_app.py cleo2025
    python make_app.py cleo2026 cleo2025    # build several, in order
    python make_app.py all                  # build every conference found

When more than one subdirectory is given, the full build runs once per
subdirectory in the order listed. The special argument 'all' (case-insensitive,
and used on its own) expands to every valid conference subdirectory under the
root, in sorted order — a subdirectory counts as a conference when it contains
both a fetch*.py downloader and a process*.py processor. A failure in one
conference is reported but does not stop the others; the run ends with a summary
and a non-zero exit code if any conference failed.

Each conference subdirectory is expected to contain:
  - exactly one downloader script whose name starts with "fetch"  (e.g.
    fetch_program_cleo2026.py),
  - exactly one processor script whose name starts with "process" (e.g.
    process_program_cleo2026.py),
  - a data_requirements_<sub>.txt manifest (e.g. data_requirements_cleo2026.txt)
    listing the input files those scripts need
    (see that file's own header for its format), and
  - a data/ subdirectory where the downloader saves, and the processor reads,
    those input files.

What a run does, for the chosen subdirectory <sub>:

  1. Decide whether to download (see FORCE_DOWNLOAD below). If downloading, run
     <sub>/fetch_*  (its own run_in_subprocess() re-spawns a clean process for
     Playwright). If skipping, reuse whatever is already in <sub>/data/.
  2. Verify (against data_requirements_<sub>.txt) that every required input file is
     now present in <sub>/data/. If any are missing, stop with an error.
  3. Decide whether to process (see FORCE_PROCESS below). Processing is skipped
     when the contents of <sub>/data/ are unchanged since the last run (verified
     against a hash stored in <sub>/data/) and a <sub>/conference_data.json
     already exists; otherwise run <sub>/process_* to produce
     <sub>/conference_data.json from the files in <sub>/data/ (pure local
     processing; no browser). Either way, the current data/ hash is (re)stored
     in <sub>/data/ for next time.
  4. Copy that conference_data.json up to the ROOT directory, where the shared
     build_conference_app.py expects its input (the builder resolves
     conference_data.json and build_affiliation_map.py relative to its OWN
     location, so the JSON must live beside it).
  5. Run build_conference_app.py in ROOT (it writes conference_app.html there).
  6. Move that conference_app.html into <sub>/, renamed to <sub>_app.html
     (e.g. cleo2026_app.html), move the affiliation_map.txt the builder wrote
     in ROOT into <sub>/data/ as well, and clean up the staged JSON copy in ROOT.

Assumed layout:

    root/
    |-- make_app.py                       <- this script
    |-- build_affiliation_map.py
    |-- build_conference_app.py
    |-- cleo2025/
    |   |-- fetch_program_cleo2025.py
    |   |-- process_program_cleo2025.py
    |   |-- data_requirements_cleo2025.txt
    |   `-- data/
    `-- cleo2026/
        |-- fetch_program_cleo2026.py
        |-- process_program_cleo2026.py
        |-- data_requirements_cleo2026.txt
        `-- data/

FORCE_DOWNLOAD:
  - False (default): consult <sub>/data_requirements_<sub>.txt. If every required
    file is already present in <sub>/data/, SKIP the (slow) browser download and
    go straight to processing. If anything required is missing, run the
    downloader to fetch it.
  - True: ALWAYS run the downloader, regardless of what's already on disk.
  In BOTH cases, after the download step the required files are re-checked and
  the run aborts with an error if any are still missing.

  Override at the command line without editing this file:
      python make_app.py cleo2026 --no-force-download   -> FORCE_DOWNLOAD = False
      python make_app.py cleo2026 --force-download       -> FORCE_DOWNLOAD = True

FORCE_PROCESS:
  - False (default): before processing, hash the contents of <sub>/data/ and
    compare against the hash stored there from the previous run. If they match
    AND <sub>/conference_data.json already exists, SKIP the processor and reuse
    that JSON. Otherwise (hash differs, no stored hash, or no JSON yet) run the
    processor. The data/ hash is (re)written to <sub>/data/ in every case.
  - True: ALWAYS run the processor, regardless of the stored hash.
  The hash ignores the stored-hash file itself and the affiliation map the
  builder later writes into data/, so neither perturbs the cache.

  Override at the command line without editing this file:
      python make_app.py cleo2026 --no-force-process    -> FORCE_PROCESS = False
      python make_app.py cleo2026 --force-process        -> FORCE_PROCESS = True
"""

from __future__ import annotations

# Always re-run the downloader? See the module docstring. False = download only
# when data_requirements_<sub>.txt reports something missing; True = always download.
FORCE_DOWNLOAD = False

# Always re-run the processor? When False (default), processing is skipped if the
# data/ directory is unchanged since the last run (verified via a stored hash)
# AND a conference_data.json already exists in the conference subdirectory. When
# True, the processor always runs regardless of the cached hash. See the module
# docstring (FORCE_PROCESS) and _hash_data_dir() / step 3 for details.
FORCE_PROCESS = False

import os
import shutil
import subprocess
import sys
import importlib.util
from pathlib import Path

# ROOT is where this script now lives (next to the shared builder).
ROOT = Path(__file__).resolve().parent

BUILDER = ROOT / "build_conference_app.py"
DATA_JSON_NAME = "conference_data.json"
BUILT_HTML_NAME = "conference_app.html"
# The builder runs build_affiliation_map.py, which writes this map file next to
# itself in ROOT. We move it into the conference subdirectory's data/ directory
# after the build, since it's a per-conference data artifact.
AFFILIATION_MAP_NAME = "affiliation_map.txt"
# The requirements manifest is named per-subdirectory, e.g. cleo2026 ->
# data_requirements_cleo2026.txt . _requirements_name() builds that name.
DATA_DIRNAME = "data"
# Cache marker: a hash of the data/ directory's contents, stored INSIDE data/.
# Before processing we compare a freshly-computed hash of data/ against this; if
# they match (and conference_data.json already exists) we skip the processor.
# The hash deliberately ignores this file itself and the affiliation map (which
# the builder later drops into data/), so neither perturbs the cache.
DATA_HASH_NAME = ".data_hash"


def _requirements_name(subdir_name: str) -> str:
    """Per-conference requirements filename, e.g. 'data_requirements_cleo2026.txt'."""
    return f"data_requirements_{subdir_name}.txt"


def _die(msg: str, code: int = 1) -> "None":
    print(f"[make] ERROR: {msg}")
    raise SystemExit(code)


# -----------------------------------------------------------------------------
# Command-line parsing: one positional subdirectory + optional flags.
# -----------------------------------------------------------------------------
def _parse_args() -> tuple[list[str], bool, bool]:
    """Return (subdir_names, force_download, force_process). Exits with usage on error.

    One or more conference subdirectories may be named; they are built in the
    order given (see main(), which loops over them). The special argument 'all'
    (case-insensitive) selects every valid conference subdirectory under ROOT
    and must be the only positional given."""
    argv = sys.argv[1:]
    positionals = [a for a in argv if not a.startswith("-")]
    flags = [a for a in argv if a.startswith("-")]

    force = FORCE_DOWNLOAD
    force_proc = FORCE_PROCESS
    for f in flags:
        if f in ("--force-download", "--force"):
            force = True
        elif f in ("--no-force-download", "--no-force"):
            force = False
        elif f in ("--force-process",):
            force_proc = True
        elif f in ("--no-force-process",):
            force_proc = False
        elif f in ("-h", "--help"):
            print(__doc__)
            raise SystemExit(0)
        else:
            _die(f"unknown option {f!r}. "
                 "Use --force-download / --no-force-download / "
                 "--force-process / --no-force-process.")

    if len(positionals) < 1:
        _die("expected at least one conference subdirectory argument, e.g.\n"
             "    python make_app.py cleo2026\n"
             "    python make_app.py cleo2026 cleo2025\n"
             "    python make_app.py all\n"
             f"(got {positionals!r}).")

    # 'all' (case-insensitive) is a special selector for every valid conference
    # subdirectory under ROOT. It must be used on its own, not mixed with names.
    if any(p.lower() == "all" for p in positionals):
        if len(positionals) > 1:
            _die("'all' selects every conference and cannot be combined with "
                 f"other subdirectory names (got {positionals!r}).")
        names = _discover_conferences()
        if not names:
            _die(f"'all' was requested but no valid conference subdirectories "
                 f"(each needing a fetch*.py and a process*.py) were found "
                 f"under {ROOT}.")
        print(f"[make] 'all' matched {len(names)} conference(s): "
              f"{', '.join(names)}", flush=True)
        return names, force, force_proc

    return positionals, force, force_proc


# -----------------------------------------------------------------------------
# Locating the per-conference scripts.
# -----------------------------------------------------------------------------
def _find_one(subdir: Path, prefix: str, what: str) -> Path:
    """Locate the single <prefix>*.py in `subdir`."""
    matches = sorted(p for p in subdir.glob(f"{prefix}*.py") if p.is_file())
    if not matches:
        _die(f"no {prefix}*.py ({what}) found in {subdir}")
    if len(matches) > 1:
        print(f"[make] WARNING: multiple {what} scripts found "
              f"({[m.name for m in matches]}); using {matches[0].name}.")
    return matches[0]


def _is_valid_conference(subdir: Path) -> bool:
    """A subdirectory is a buildable conference if it contains at least one
    fetch*.py (downloader) AND at least one process*.py (processor). This is the
    same pair _build_one() requires via _find_one(); we check it cheaply here so
    'all' can skip directories that aren't conferences (data/, build artifacts,
    .git, venvs, etc.) without aborting the run."""
    if not subdir.is_dir():
        return False
    has_fetch = any(p.is_file() for p in subdir.glob("fetch*.py"))
    has_process = any(p.is_file() for p in subdir.glob("process*.py"))
    return has_fetch and has_process


def _discover_conferences() -> list[str]:
    """Return the names of all valid conference subdirectories under ROOT, sorted.

    A subdirectory qualifies when _is_valid_conference() is true (it has both a
    fetch*.py and a process*.py). Hidden directories (names starting with '.')
    are skipped. This backs the 'all' command-line argument."""
    names = sorted(
        p.name for p in ROOT.iterdir()
        if p.is_dir() and not p.name.startswith(".")
        and _is_valid_conference(p)
    )
    return names


# -----------------------------------------------------------------------------
# data_requirements_<sub>.txt parsing + required-file checking.
# -----------------------------------------------------------------------------
def _parse_requirements(req_path: Path) -> tuple[list[dict], str]:
    """Parse a data_requirements_<sub>.txt into a list of file-requirement dicts and
    the manual_steps_written date string.

    Each requirement dict has: pattern, required (bool), produced_by, description,
    manual. The format is the simple line-oriented one documented in the file's
    own header: "[file: <pattern>]" blocks with "key: value" lines, where the
    "manual"/"description" values may wrap onto indented continuation lines.
    Lines beginning with '#' are comments; blank lines are ignored except that
    they terminate a wrapping value."""
    reqs: list[dict] = []
    date_written = ""
    cur: dict | None = None
    cur_key: str | None = None     # which key a continuation line extends

    def _flush():
        nonlocal cur
        if cur is not None:
            reqs.append(cur)
            cur = None

    for raw in req_path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            cur_key = None
            continue
        if stripped.startswith("#"):
            continue

        header = None
        if stripped.startswith("[file:") and stripped.endswith("]"):
            header = stripped[len("[file:"):-1].strip()
        if header is not None:
            _flush()
            cur = {"pattern": header, "required": True,
                   "produced_by": "", "description": "", "manual": ""}
            cur_key = None
            continue

        # Continuation line: indented (starts with whitespace in the raw line)
        # and we're currently accumulating a value.
        is_indented = raw[:1] in (" ", "\t")
        if is_indented and cur is not None and cur_key is not None:
            cur[cur_key] = (cur[cur_key] + " " + stripped).strip()
            continue

        # Otherwise expect "key: value".
        if ":" not in stripped:
            cur_key = None
            continue
        key, _, value = stripped.partition(":")
        key = key.strip().lower()
        value = value.strip()

        if cur is None:
            # Top-level metadata (outside any [file:] block).
            if key == "manual_steps_written":
                date_written = value
            continue

        if key == "required":
            cur["required"] = value.strip().lower() not in ("no", "false", "0")
            cur_key = None
        elif key in ("produced_by", "description", "manual"):
            cur[key] = value
            cur_key = key
        else:
            cur_key = None

    _flush()
    return reqs, date_written


def _missing_required(data_dir: Path, reqs: list[dict]) -> list[dict]:
    """Return the requirement dicts whose pattern matches NO file in data_dir.
    Only requirements with required=True are considered."""
    missing: list[dict] = []
    for r in reqs:
        if not r.get("required", True):
            continue
        if not any(data_dir.glob(r["pattern"])):
            missing.append(r)
    return missing


def _report_missing(missing: list[dict], data_dir: Path,
                    date_written: str, req_name: str) -> None:
    """Pretty-print the missing files and their manual-download instructions."""
    print(f"[make] {len(missing)} required input file(s) missing from "
          f"{data_dir}:", flush=True)
    for r in missing:
        print(f"  - {r['pattern']}"
              + (f"  — {r['description']}" if r.get("description") else ""),
              flush=True)
        if r.get("manual"):
            print(f"      manual download: {r['manual']}", flush=True)
    if date_written:
        print(f"  (manual steps in {req_name} were written "
              f"{date_written}; re-verify if the site has changed since.)",
              flush=True)


# -----------------------------------------------------------------------------
# Hashing the data/ directory, to decide whether processing can be skipped.
# -----------------------------------------------------------------------------
def _hash_data_dir(data_dir: Path) -> str:
    """Return a hex digest summarizing the contents of `data_dir`.

    Every regular file under data_dir (recursively) contributes its relative
    path and its bytes to the digest, so adding, removing, renaming, or editing
    any input file changes the result. Two files are deliberately excluded so
    the cache isn't perturbed by our own bookkeeping:
      - the stored hash file itself (DATA_HASH_NAME), and
      - the affiliation map (AFFILIATION_MAP_NAME), which the builder writes
        into data/ AFTER processing — it's an output, not a processor input.
    Files are processed in sorted relative-path order for a stable digest."""
    import hashlib

    excluded = {DATA_HASH_NAME, AFFILIATION_MAP_NAME}
    h = hashlib.sha256()
    files = sorted(
        (p for p in data_dir.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(data_dir).as_posix(),
    )
    for p in files:
        rel = p.relative_to(data_dir).as_posix()
        # Exclude by relative path so e.g. data/affiliation_map.txt is ignored
        # but a same-named file deeper in a subfolder would still count.
        if rel in excluded:
            continue
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()


def _read_stored_hash(data_dir: Path) -> str | None:
    """Return the previously-stored data/ hash, or None if absent/unreadable."""
    hp = data_dir / DATA_HASH_NAME
    if not hp.is_file():
        return None
    try:
        return hp.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _write_stored_hash(data_dir: Path, digest: str) -> None:
    """Persist `digest` into data/ as DATA_HASH_NAME (best-effort)."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / DATA_HASH_NAME).write_text(digest + "\n", encoding="utf-8")
    except OSError as e:
        print(f"[make]   note: could not write data hash "
              f"({DATA_HASH_NAME}): {e}", flush=True)


# -----------------------------------------------------------------------------
# Running a child script (downloader / processor) with live-streamed output.
# -----------------------------------------------------------------------------
def _run_script(path: Path, cwd: Path) -> None:
    """Run a .py file as a fresh child Python process from `cwd`, streaming its
    output here live. Used for the downloader and the processor so their verbose
    logs show up and their module-level state never collides with this process
    or the builder's."""
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, str(path)],
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
    rc = proc.wait()
    if rc != 0:
        _die(f"{path.name} exited with code {rc}.")


def _import_module(path: Path, mod_name: str):
    """Import a .py file as a uniquely-named module (used for the builder)."""
    parent = str(path.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        _die(f"couldn't load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def _build_one(subdir_name: str, force_download: bool, force_process: bool) -> "None":
    """Build a single conference's app for the subdirectory `subdir_name`.

    This is the per-conference body; main() calls it once per subdirectory named
    on the command line. Step labels below remain "Step N/5" for each individual
    conference's build."""
    # Let the (possibly CLI-overridden) value drive the step-3 cache check below,
    # which reads the module-level FORCE_PROCESS.
    global FORCE_PROCESS
    FORCE_PROCESS = force_process

    subdir = (ROOT / subdir_name).resolve()
    if not subdir.is_dir():
        _die(f"conference subdirectory not found: {subdir}")
    # Keep the resolved subdir inside ROOT (so 'cleo2026' works but '../x' or an
    # absolute path can't wander off).
    if ROOT not in subdir.parents and subdir != ROOT:
        _die(f"{subdir_name!r} does not resolve to a subdirectory of {ROOT}.")

    data_dir = subdir / DATA_DIRNAME
    req_name = _requirements_name(subdir.name)
    req_path = subdir / req_name
    final_html_name = f"{subdir.name}_app.html"

    downloader = _find_one(subdir, "fetch", "downloader")
    processor = _find_one(subdir, "process", "processor")

    processor_json = subdir / DATA_JSON_NAME
    root_json = ROOT / DATA_JSON_NAME
    root_html = ROOT / BUILT_HTML_NAME
    final_html = subdir / final_html_name
    root_affmap = ROOT / AFFILIATION_MAP_NAME
    final_affmap = data_dir / AFFILIATION_MAP_NAME

    # Requirements manifest is what tells us which files must exist in data/.
    reqs: list[dict] = []
    date_written = ""
    if req_path.exists():
        reqs, date_written = _parse_requirements(req_path)
    else:
        print(f"[make] WARNING: no {req_name} in {subdir}; cannot "
              "verify required inputs. Proceeding without that safety check.",
              flush=True)

    print(f"[make] === Building '{subdir.name}' "
          f"(FORCE_DOWNLOAD={force_download}, "
          f"FORCE_PROCESS={force_process}) ===", flush=True)

    # ----------------------------------------------------------- 1. download?
    # Decide whether to download. With FORCE_DOWNLOAD on we always do. With it
    # off we download only if the requirements manifest reports something
    # missing; if everything's present we skip the slow browser step.
    if force_download:
        do_download = True
        print("[make] === Step 1/5: FORCE_DOWNLOAD is on — downloading ===",
              flush=True)
    else:
        data_dir.mkdir(parents=True, exist_ok=True)
        missing = _missing_required(data_dir, reqs) if reqs else []
        if reqs and not missing:
            do_download = False
            print("[make] === Step 1/5: all required inputs present — "
                  "SKIPPING download ===", flush=True)
        else:
            do_download = True
            if reqs:
                print("[make] === Step 1/5: required inputs missing — "
                      "downloading ===", flush=True)
                _report_missing(missing, data_dir, date_written, req_name)
            else:
                print("[make] === Step 1/5: no requirements manifest — "
                      "downloading to be safe ===", flush=True)

    if do_download:
        print(f"[make] running downloader {downloader.name} …", flush=True)
        _run_script(downloader, cwd=subdir)

    # ------------------------------------------- 2. verify required files
    # Regardless of whether we downloaded or skipped, the required inputs must
    # now be present before we try to process. This catches a download that
    # silently failed to produce a file, as well as a skip premised on a stale
    # or incomplete data/ directory.
    print("[make] === Step 2/5: verifying required input files ===", flush=True)
    if reqs:
        missing = _missing_required(data_dir, reqs)
        if missing:
            _report_missing(missing, data_dir, date_written, req_name)
            why = ("The downloader ran but did not produce them"
                   if do_download else "No download was attempted")
            _die(f"required input file(s) still missing from {data_dir} after "
                 f"the download step. {why}; fetch them (see the manual steps "
                 "above) or run with --force-download.")
        print(f"[make] all required input files present in "
              f"{data_dir.name}/.", flush=True)
    else:
        print(f"[make] (no {req_name}; skipping verification.)",
              flush=True)

    # ------------------------------------------------------------- 3. process
    # Decide whether we can skip the processor. We can skip when: FORCE_PROCESS
    # is off, a conference_data.json already exists in the subdirectory, a hash
    # was stored on a previous run, and a fresh hash of data/ matches it (i.e.
    # the inputs are byte-for-byte unchanged). Otherwise we run the processor.
    # In ALL cases we (re)store the data/ hash afterwards so the next run can
    # use it.
    print(f"[make] === Step 3/5: processing with {processor.name} ===",
          flush=True)

    current_hash = _hash_data_dir(data_dir)
    stored_hash = _read_stored_hash(data_dir)

    if FORCE_PROCESS:
        do_process = True
        print("[make] FORCE_PROCESS is on — processing.", flush=True)
    elif not processor_json.exists():
        do_process = True
        print(f"[make] no existing {DATA_JSON_NAME} in {subdir.name}/ — "
              "processing.", flush=True)
    elif stored_hash is None:
        do_process = True
        print(f"[make] no stored data hash ({DATA_HASH_NAME}) — processing.",
              flush=True)
    elif stored_hash != current_hash:
        do_process = True
        print("[make] data/ has changed since the last run — processing.",
              flush=True)
    else:
        do_process = False
        print(f"[make] data/ unchanged and {DATA_JSON_NAME} present — "
              "SKIPPING processing, reusing existing JSON.", flush=True)

    if do_process:
        _run_script(processor, cwd=subdir)
        if not processor_json.exists():
            _die(f"processor finished but {processor_json} was not produced.")
        # The processor may have rewritten files in data/; recompute so the
        # stored hash reflects the post-processing state of the inputs.
        current_hash = _hash_data_dir(data_dir)

    # Store the hash either way: after a real run it captures the current inputs;
    # after a skip it (re-)affirms the cache (e.g. if the hash file was missing
    # for some other reason it gets written, though a missing hash forces a run
    # above — this mainly normalizes the stored value).
    _write_stored_hash(data_dir, current_hash)

    # --------------------------------------------- 4. stage JSON + build
    print(f"[make] === Step 4/5: staging {DATA_JSON_NAME} into root and "
          f"building with {BUILDER.name} ===", flush=True)
    backup_json = None
    if root_json.exists():
        backup_json = ROOT / (DATA_JSON_NAME + ".make_bak")
        print(f"[make] a {DATA_JSON_NAME} already exists in root; "
              f"backing it up to {backup_json.name}", flush=True)
        shutil.move(str(root_json), str(backup_json))
    shutil.copy2(str(processor_json), str(root_json))

    try:
        # The builder reads conference_data.json at IMPORT time, so the JSON
        # must already be staged in root before we import it — which it is now.
        saved_argv = sys.argv
        sys.argv = [str(BUILDER)]
        try:
            bld = _import_module(BUILDER, "make_builder")
            bld.main()
        finally:
            sys.argv = saved_argv
        if not root_html.exists():
            _die(f"builder finished but {root_html} was not produced.")

        # --------------------------------- 5. move results into subdirectory
        print(f"[make] === Step 5/5: moving result to "
              f"{final_html.name} in {subdir.name}/ ===", flush=True)
        if final_html.exists():
            final_html.unlink()
        shutil.move(str(root_html), str(final_html))

        # Also move the affiliation map the builder wrote in ROOT into the
        # conference subdirectory's data/ directory (it's a per-conference data
        # artifact). Clobber any existing copy there.
        if root_affmap.exists():
            print(f"[make]   moving {AFFILIATION_MAP_NAME} into "
                  f"{subdir.name}/{DATA_DIRNAME}/", flush=True)
            data_dir.mkdir(parents=True, exist_ok=True)
            if final_affmap.exists():
                final_affmap.unlink()
            shutil.move(str(root_affmap), str(final_affmap))
        else:
            print(f"[make]   note: builder produced no {AFFILIATION_MAP_NAME} "
                  "in root; nothing to move.", flush=True)
    finally:
        # Always clean up the staged JSON copy and restore any backup.
        if root_json.exists():
            root_json.unlink()
        if backup_json and backup_json.exists():
            shutil.move(str(backup_json), str(root_json))

    print(f"[make] DONE -> {final_html}", flush=True)


def main() -> "None":
    subdir_names, force_download, force_process = _parse_args()

    if not BUILDER.exists():
        _die(f"builder not found: {BUILDER} "
             "(expected build_conference_app.py in the root directory)")

    if len(subdir_names) == 1:
        _build_one(subdir_names[0], force_download, force_process)
        return

    # Multiple subdirectories: build each in turn. One conference's failure
    # (via _die -> SystemExit) would abort the whole run, so catch SystemExit
    # per conference, keep going, and report a summary at the end.
    print(f"[make] ===== building {len(subdir_names)} conferences: "
          f"{', '.join(subdir_names)} =====", flush=True)
    failures: list[tuple[str, str]] = []
    for i, name in enumerate(subdir_names, 1):
        print(f"[make] ===== conference {i}/{len(subdir_names)}: "
              f"{name} =====", flush=True)
        try:
            _build_one(name, force_download, force_process)
        except SystemExit as e:
            # _die() raises SystemExit after printing its own ERROR line.
            failures.append((name, str(e.code)))
            print(f"[make] ===== {name} FAILED — continuing with the rest "
                  "=====", flush=True)
        print(flush=True)

    built = len(subdir_names) - len(failures)
    print(f"[make] ===== summary: {built}/{len(subdir_names)} built "
          "successfully =====", flush=True)
    if failures:
        for name, _code in failures:
            print(f"[make]   FAILED: {name}", flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()