"""The unattended Monday run. Started by the OS scheduler via run_weekly.pyw.

Success: the spreadsheet is copied to "Weekly results/" and any old failure note is removed.
Failure: "Weekly results/LAST_RUN_FAILED.txt" explains what went wrong and how to fix it,
and the app shows the same message the next time it is opened.
"""
from __future__ import annotations

import json
import shutil
import traceback
from datetime import datetime

from . import drive, pipeline
from .claude_cli import find_claude
from .paths import Paths, load_config, load_settings

FAILED_NOTE = "LAST_RUN_FAILED.txt"
STATUS_FILE = "last_weekly_run.json"


def _record(paths: Paths, status: dict) -> None:
    paths.weekly.mkdir(parents=True, exist_ok=True)
    paths.state.mkdir(parents=True, exist_ok=True)    # first run on a fresh install
    (paths.state / STATUS_FILE).write_text(json.dumps(status, indent=2), encoding="utf-8")
    note = paths.weekly / FAILED_NOTE
    if status["ok"]:
        note.unlink(missing_ok=True)
    else:
        note.write_text(f"The weekly invoice check on {status['at']} did not finish.\n\n"
                        f"What happened: {status['message']}\n\nHow to fix it: {status['fix']}\n\n"
                        "Then open Invoice Triage and press Run check.\n", encoding="utf-8")


def last_status(paths: Paths) -> dict | None:
    try:
        return json.loads((paths.state / STATUS_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def main(paths: Paths | None = None) -> int:
    paths = paths or Paths()
    at = datetime.now().isoformat(timespec="minutes")
    settings = load_settings(paths)
    claude = find_claude(settings.get("claude_path") or None)
    folder = drive.folder_id(settings.get("drive_link", ""))
    if not folder:
        _record(paths, {"ok": False, "at": at, "message": "No Google Drive folder is set.",
                        "fix": "Open Invoice Triage, paste the Drive folder link and turn the weekly run on again."})
        return 2
    if not claude:
        _record(paths, {"ok": False, "at": at, "message": "Claude Code could not be found.",
                        "fix": "Install or reinstall Claude Code and sign in."})
        return 2
    try:
        cfg = {**load_config(paths), "claude_path": claude}
        summary = pipeline.run(paths, cfg, drive_folder=folder, only_new=True, trigger="weekly")
    except pipeline.RunBusy:
        _record(paths, {"ok": False, "at": at, "message": "A manual check was running at the same time.",
                        "fix": "Nothing to fix; press Run check in the app to catch up."})
        return 1
    except Exception as exc:
        _record(paths, {"ok": False, "at": at, "message": f"{type(exc).__name__}: {exc}",
                        "fix": "Check the internet connection and that Claude Code is signed in.",
                        "trace": traceback.format_exc()[-2000:]})
        return 1
    if summary["files"]:
        dest = paths.weekly / f"Invoices {summary['run_id'][:10]}.xlsx"
        paths.weekly.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(paths.runs / summary["run_id"] / "invoices.xlsx", dest)
    _record(paths, {"ok": True, "at": at, "run_id": summary["run_id"], "files": summary["files"],
                    "review": summary["review"],
                    "message": (f"{summary['files']} new files checked, {summary['review']} to review"
                                if summary["files"] else "No new invoices this week")})
    return 0
