# Invoice Triage

Turns a folder of vendor invoices into one spreadsheet row per file:
`file, vendor, invoice_number, due_date, amount, flag, reason`. Invoices can be PDFs, scans,
phone photos or emails with attachments. `flag = Y` means a person should look before it is
paid.

**On the 30-file test folder:**
- Every file is read correctly. Three fresh runs were each compared field by field against
  a hand-checked answer key, with zero mismatches.
- 14 files are held for review and 16 have no issues found.
- A run takes about 30 seconds and uses about $0.30 of Claude usage. A re-run of unchanged
  files is instant and free.

## Using it (no technical knowledge needed)

1. Double-click **Start Invoice Triage** (`.bat` on Windows, `.command` on Mac). The app
   opens in your browser.
2. Paste the Google Drive folder link and press **Run check**. Or choose invoice files from
   your computer.
3. Read the list. Flagged invoices come first, each with the reason it is held. Select a row
   to see what Claude read. Select a file name to open the original document.
4. Press **Download Excel** for the spreadsheet.
5. To have it run on its own, turn on **Check the Drive folder every Monday at 07:00**.

**Needs:**
- Python 3.10 or newer, from python.org. The launcher explains if it's missing.
- Claude Code, installed and signed in. On Windows, use the native installer, not npm.

There's nothing else to install: no pip packages and no PDF tools.

## How it works

```
Drive folder or chosen files
   │
   ├─ 1  Sync      download only files not seen before                     drive.py
   ├─ 2  Read      Claude reads each PDF, scan, photo or email (+ attachments)
   │               into a strict schema, 6 at a time                         extract.py
   ├─ 3  Decide    plain-code rules set the flag and the reason             rules.py
   └─ 4  Report    Excel (Invoices + Audit tabs), CSV, summary, ledger      report.py
```

**The AI reads; the code decides.** Claude handles what code can't: messy layouts, scans,
photos, the meaning of an email ("resending as requested"). Whether an invoice is held comes
from fixed, unit-tested rules. That makes the result repeatable and auditable.

Claude runs with no tools, no internet access of its own, and an empty working folder. It
is told that document text is data, never instructions. It can add a concern, but it cannot
remove a rule's flag.

## What gets held for review

| Rule | In the test folder |
|---|---|
| Over $5,000, after converting foreign currency | 11. Includes Fathom: €4,700 ≈ $5,405 |
| Duplicate: same vendor + invoice #, or same vendor + amount + date. Checked within the batch and against every earlier run. Look-alike characters (`1O41` = `1041`) are treated as equal | The Keystone email resend; the Redwood scan of an invoice already received as a PDF |
| Arithmetic: line items don't add up to the total | Summit: lines total $1,284.50, invoice says $1,384.50 |
| Not billed in USD | Fathom (EUR, international wire) |
| Billed to someone other than Waverock | none |
| No due date or terms; due date before invoice date | none (12 printed, 18 worked out from terms) |
| Claude's judgement: changed bank details, "this replaces…", charges for another company | Atlas placement fees for roles at Grasp and PlanIt Geo |
| Unreadable, too large, or an email with several invoices | none |

Of a duplicate pair, the clean PDF is the original and is not held. The copy is held.

## Every Monday without anyone

The switch registers a job with the computer's own scheduler:
- Windows: Task Scheduler
- Mac: launchd
- Linux: a systemd user timer

At 07:00 every Monday the job:
- checks the Drive folder;
- reads only files it hasn't seen (same name and contents);
- compares each one with everything checked before, so a resend next month is still caught;
- saves the spreadsheet to **Weekly results/**.

If something goes wrong, `Weekly results/LAST_RUN_FAILED.txt` says what happened and how to
fix it, and the app shows the same message. If the computer was off at 07:00, the check runs
the next time it is on.

**Production version.** The same code runs on a server or in a GitHub Actions schedule when
`ANTHROPIC_API_KEY` is set. Claude is then called in API mode, with no laptop or sign-in
involved. For a private folder, swap the public-link Drive sync for a Google service
account, and write results straight into the team's shared Sheet.

## Command line

```bash
python3 -m invoice_triage                                  # open the app
python3 -m invoice_triage run --drive <link> [--no-cache]  # check from the terminal
python3 -m invoice_triage run --folder <path>
python3 -m invoice_triage weekly                           # what the Monday job runs
python3 -m unittest discover -s tests -t .                 # 56 tests
```

Settings live in `config.json`: threshold, FX rates, company names, model, parallel reads.

## Project layout

| File | Job |
|---|---|
| `invoice_triage/pipeline.py` | One run, start to finish; run lock; ledger |
| `invoice_triage/ingest.py` | Files → parts for Claude; email splitting; size and type limits |
| `invoice_triage/claude_cli.py` | Finding and calling Claude Code safely on each OS |
| `invoice_triage/extract.py` | Schema, instructions, cache |
| `invoice_triage/rules.py` | Every flag decision |
| `invoice_triage/report.py` | Excel (standard library only), CSV, summary |
| `invoice_triage/scheduler.py`, `weekly.py`, `run_weekly.pyw` | The Monday run |
| `invoice_triage/server.py`, `web/` | The local app |
| `docs/PLAN.md` | The plan, and what changed after independent review |

## Security of the local app

- It listens only on this computer (127.0.0.1) and rejects requests addressed to any other
  host name.
- Every API call needs a secret generated each time the app starts.
- The page has a strict Content-Security-Policy and shows document text as text, never as
  HTML.
- Uploads keep only the file name, are limited to PDF, image and email types, and are capped
  at 25 MB.
- Only files belonging to a run can be opened from it.
- Spreadsheet cells that start like a formula are neutralised.

## Known limits

- The Windows and Mac paths are written for those systems but were tested here only on
  Linux. The scheduler files they generate are unit-tested, and the Linux timer is verified
  with `systemd-analyze`.
- The FX rate is a fixed number in `config.json`.
- Numeric dates are read as US `MM/DD/YYYY`. The date as printed is kept in the Audit tab.
- One row per file. An email carrying several invoices is held for manual splitting.
- The public-link Drive sync skips a file whose name it already has. A corrected re-upload
  under the same name needs a new name, or the service-account version.
