# Plan v1: Invoice Triage as a desktop-friendly app

## Goal
A finance person with no technical background can do two things:
1. Double-click to open the app, paste the Drive link (or drop files), press **Run**, and get the
   spreadsheet.
2. Tick a box so it runs **every Monday at 07:00** without anyone touching it.

It must work on Windows, macOS and Linux, and look professionally made, not like a template.

## Constraints (agreed before building)
- The engine is the local Claude Code CLI on the user's subscription. There is no API key.
- Prerequisites stay minimal: **Python 3.10+ and Claude Code (logged in)**. Nothing installed
  with pip, and no poppler.
- The principle stays: **the AI reads, the code decides**. Flags come from tested rules.
- It works offline apart from Drive and Claude. No CDN assets.

## Evidence already gathered
| Question | Finding |
|---|---|
| Can Claude read PDFs itself (no poppler)? | Yes. Via the CLI, both the scanned and the text PDF extracted correctly in about 3 s, about $0.014 per file |
| Does the CLI take a prompt or schema from a file? | No `--system-prompt-file`. Arguments go through argv, which is fine for `claude.exe`. The npm `.cmd` shim on Windows can mangle quoting |
| Current accuracy | 30/30 fields correct, 14 flags, stable across 3 fresh runs, 24 tests |

## Architecture
```
invoice_triage/
  pipeline.py    NEW  run(source, settings, on_event) -> RunResult. The only orchestration;
                      used by both the UI and the CLI. Emits events: started, file_done, finished.
  ingest.py      CHG  no poppler. PDF -> Claude "document" block; image -> image block;
                      .eml -> body text + attachments as blocks. Warnings: multi-invoice email.
  extract.py     CHG  build blocks; keep schema, cache (hash key), atomic writes; Windows-safe
                      CLI discovery (prefer claude.exe; clear error if not found / not logged in).
  rules.py       KEEP unchanged (tests stay green).
  report.py      CHG  UTF-8 everywhere; CSV as utf-8-sig so Excel on Windows shows € correctly.
  drive.py       CHG  accept a full share URL or bare id.
  scheduler.py   NEW  enable/disable/status of a weekly job, per OS:
                      Windows schtasks · macOS launchd LaunchAgent · Linux systemd --user timer.
  settings.py    NEW  load/save settings.json (threshold, FX, Drive link, company names, model).
  server.py      NEW  stdlib ThreadingHTTPServer on 127.0.0.1, random free port, opens browser.
  web/           NEW  index.html, app.css, app.js (vanilla, no build step).
  __main__.py    CHG  `python -m invoice_triage` opens the app; `--weekly` runs headless.
Start Invoice Triage.bat / .command / start.sh   NEW  double-click launchers.
```
Removed: `run_weekly.sh` and `schedule/install_timer.sh`, both replaced by `scheduler.py`.
`notify.py` is dropped (YAGNI); weekly results show up in the app's history.

## Runs and storage
`runs/<YYYY-MM-DD_HHMM>/` holds `invoices.xlsx`, `invoices.csv`, `results.json` (what the UI
reads) and `log.txt`. The ledger and cache stay in `state/`. A weekly run uses `--only-new`
semantics. A manual run processes whatever the user selected.

## UI (single page, three states)
1. **Start**
   - Source picker: a Drive link field (prefilled from settings), or drop files / choose folder.
   - A large **Run** button.
   - A readiness check row: Claude installed and logged in, plus Drive reachable, each with a
     plain-language fix.
2. **Running**
   - Per-file progress list with status (reading, done, needs review), elapsed time and a
     progress bar.
   - Live updates over Server-Sent Events, announced through `aria-live`.
3. **Results**
   - Four figures: files, needs review, clear to pay, $ awaiting approval.
   - Filter tabs: All / Needs review / Clear. Search.
   - Table: file, vendor, invoice #, due date, amount, status pill, reason.
   - Expanding a row shows the audit detail: raw date, terms, currency/FX, source type, AI notes.
   - **Download Excel** and **Download CSV** buttons, plus **Open folder**.

Sidebar or header:
- **History** of past runs, weekly ones included.
- **Settings** drawer: approval threshold, FX table, Drive link, "Run every Monday 07:00"
  switch with its status.

Design bar:
- Type and colour: system font stack, neutral greys, one accent, a red/amber/green status
  scale, tabular numerals for money.
- Behaviour: dark mode, keyboard reachable, visible focus, a 4.5:1 contrast minimum.
- Copy: no emojis, no gradients, no marketing copy, plain wording.

## Security (the server is local but browser-reachable)
- Bind to 127.0.0.1 only and reject a `Host` header that isn't localhost (DNS rebinding).
- A per-launch random token, embedded in the page and required as a header on every API call
  (blocks cross-site POSTs).
- Uploads: base name only, an extension allowlist (.pdf .png .jpg .jpeg .webp .gif .eml), a
  25 MB per-file cap, written to `runs/<id>/inbox/`.
- Untrusted text from documents is rendered with `textContent` only, never `innerHTML`.
- Excel formula neutralisation stays in place.

## Windows specifics
- UTF-8 for every file read and write. Reconfigure stdout to UTF-8.
- No shell scripts are needed to run; the `.bat` launcher finds `py -3` or `python`.
- `claude` discovery: look on PATH (`.exe` preferred), then `%USERPROFILE%\.local\bin\claude.exe`.
  If only the `.cmd` shim is found, warn in the readiness check.
- The scheduler uses `schtasks /Create /SC WEEKLY /D MON /ST 07:00 /TN "Invoice Triage" /TR ...`
  and the full path to `pythonw.exe`, so no console window pops up.
- Honest limit: it cannot be tested on a real Windows machine from this Linux box.
  Command-building is unit-tested and the README says so.

## Tests
- Keep: rules and report (24).
- New:
  - ingest: eml → blocks, multi-attachment warning, extension handling.
  - drive: URL → id parsing.
  - scheduler: command/plist/unit text per OS (pure functions, no system calls).
  - server: start on port 0, then check token enforcement, Host check, `/api/state`, and that
    upload rejects `../`.
  - pipeline: a fake extractor (no Claude) runs end to end into a temp `runs/` folder.
- Manual check: drive the real UI in Chrome on the 30-file folder and screenshot each state.

## Out of scope
Multi-user access, writing to Google Sheets through the API, authentication beyond the local
token, and email or Slack notifications.

---

# Plan v2: changes after independent review (2026-11-08)

| # | Change | Reason |
|---|---|---|
| 1 | The cache key includes `INGEST_VERSION`. Re-validate 30/30 three times with `--no-cache` **before** the UI work | Old text-layer answers would otherwise come back from the cache and hide regressions |
| 2 | The schema gains `appears_scanned`, mapped to `pdf_text`/`pdf_scan`. Add a Redwood regression test | Without poppler, the scan could win "original" over the clean PDF |
| 3 | Progress by **polling** `/api/run` every 700 ms instead of SSE | Simpler; enough for 30 files |
| 4 | Cut: dark mode, search, history sidebar, FX/threshold editing in the UI, `aria-live` per file | Scope for a 1-hour brief; config.json still holds these |
| 5 | Keep "Choose files": each file is a raw `PUT /api/upload` body, so no multipart parsing is needed | Clerks can't type folder paths |
| 6 | `run_weekly.pyw` entry script (fixes its own `sys.path`). Windows task via `schtasks /XML` (WorkingDirectory, StartWhenAvailable); launchd with WorkingDirectory; systemd `Persistent=true` | 261-character `/TR` limit; missed runs; working directory |
| 7 | Store the absolute `claude` path when scheduling. Block the `.cmd` shim. `CREATE_NO_WINDOW`; `encoding="utf-8"`; guard `sys.stdout is None` under pythonw | Schedulers don't inherit PATH; Windows quirks |
| 8 | One cross-platform run lock (`O_CREAT\|O_EXCL`, pid, stale after 2 h), shared by the UI and the weekly run | A manual run and a weekly run could otherwise collide on the ledger |
| 9 | The weekly run writes `runs/<id>/`, copies the Excel file to `Weekly results/`, and writes `Weekly results/LAST_RUN_FAILED.txt` with the fix if it fails. The UI shows next run / last result | "Without you" needs a visible outcome |
| 10 | Wording: "Review" / "No issues found" (never "clear to pay"); one results line, no stat cards | The tool must not imply payment approval |
| 11 | Each row links to the original document (served with nosniff and a CSP; `.eml` as a download) | Clerks verify against the source |
| 12 | Launchers check the Python version (3.10+) and print a plain fix | macOS ships 3.9 |
| 13 | Cap uploads and documents at 25 MB and flag anything over it | Claude PDF limits |
| 14 | Ledger semantics: every run records what it triaged. Weekly runs skip files already triaged (same name and bytes) | Explicit and documented |
