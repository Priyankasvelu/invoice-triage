"""One run, start to finish. The app, the command line and the Monday job all call run().

    source  ──► sync/collect ──► Claude reads each file ──► rules decide flags ──► runs/<id>/
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import drive, extract, ingest, report, rules
from .paths import Paths

LOCK_STALE_SECONDS = 2 * 60 * 60


def _pid_alive(pid: int) -> bool:
    """Is the process that took the lock still running?"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259                                          # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunBusy(RuntimeError):
    pass


@dataclass
class Progress:
    """Live state of a run; read by the app every few hundred milliseconds."""
    run_id: str
    trigger: str
    stage: str = "starting"           # starting · syncing · reading · checking · done · failed
    total: int = 0
    files: list[dict] = field(default_factory=list)
    message: str = ""
    error: str = ""
    started: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        done = sum(1 for f in self.files if f["status"] != "reading")
        return {"run_id": self.run_id, "trigger": self.trigger, "stage": self.stage,
                "total": self.total, "done": done, "files": list(self.files),
                "message": self.message, "error": self.error,
                "elapsed": round(time.time() - self.started, 1)}


# ---------- lock: one run at a time, across the app and the scheduler ----------

def _acquire_lock(lock: Path, run_id: str) -> None:
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"run_id": run_id, "pid": os.getpid(), "at": time.time()}, f)
            return
        except FileExistsError:
            try:
                held = json.loads(lock.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                held = {"at": 0}
            crashed = not _pid_alive(int(held.get("pid", 0)))
            if crashed or time.time() - held.get("at", 0) > LOCK_STALE_SECONDS:
                lock.unlink(missing_ok=True)       # left behind by a closed window or crash
                continue
            raise RunBusy("Another check is already running. Wait for it to finish.")
    raise RunBusy("Could not start the run.")


# ---------- ledger: everything ever triaged, for cross-week duplicate checks ----------

def _load_ledger(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))["invoices"]
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError, KeyError):
        backup = path.with_suffix(f".corrupt-{int(time.time())}.json")
        shutil.copyfile(path, backup)              # keep the evidence, start clean
        return []


def _save_ledger(path: Path, ledger: list[dict], records: list[dict], run_id: str) -> None:
    seen = {(h["file"], h["sha256"]) for h in ledger}
    added = []
    for rec in records:
        if rec.get("error") or (rec["file"], rec["sha256"]) in seen:
            continue
        exact, loose = rules.dedupe_keys(rec)
        d = rec["data"]
        added.append({"key": exact, "loose_key": loose, "file": rec["file"], "sha256": rec["sha256"],
                      "vendor": d.get("vendor_name"), "invoice_number": d.get("invoice_number"),
                      "amount": d.get("total_amount"), "currency": d.get("currency"),
                      "run_id": run_id, "run_date": run_id[:10]})
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"invoices": ledger + added}, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------- the run ----------

def _app_relative(path: Path, paths: Paths) -> str:
    """Store paths relative to the app folder: no username on disk, and the folder can move."""
    try:
        return Path(path).resolve().relative_to(paths.root.resolve()).as_posix()
    except ValueError:
        return str(path)

def new_run_id() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H%M%S")


def run(paths: Paths, cfg: dict, *, drive_folder: str | None = None, folder: Path | None = None,
        only_new: bool = False, trigger: str = "manual", use_cache: bool = True,
        progress: Progress | None = None,
        on_update: Callable[[Progress], None] = lambda p: None) -> dict:
    """Run one check. Exactly one of drive_folder / folder must be given.
    Returns the run summary (also written to runs/<id>/results.json)."""
    progress = progress or Progress(run_id=new_run_id(), trigger=trigger)
    run_dir = paths.runs / progress.run_id
    _acquire_lock(paths.lock, progress.run_id)
    try:
        return _run_locked(paths, cfg, run_dir, drive_folder, folder, only_new, use_cache,
                           progress, on_update)
    except Exception as exc:
        progress.stage, progress.error = "failed", str(exc)
        on_update(progress)
        raise
    finally:
        paths.lock.unlink(missing_ok=True)


def _run_locked(paths, cfg, run_dir, drive_folder, folder, only_new, use_cache, progress, on_update):
    if drive_folder:
        progress.stage, progress.message = "syncing", "Fetching new files from Google Drive"
        on_update(progress)
        inbox = paths.drive_inbox(drive_folder)
        total, new = drive.sync_folder(drive_folder, inbox)
        progress.message = (f"{total} files in the Drive folder"
                            + (f" ({new} downloaded just now)" if new else ""))
    else:
        inbox = folder
    docs = [ingest.load_document(p) for p in ingest.list_inbox(inbox)]

    ledger = _load_ledger(paths.ledger)
    if only_new:      # same name and same bytes = already triaged; a renamed resend is NOT skipped
        seen = {(h["file"], h["sha256"]) for h in ledger}
        docs = [d for d in docs if (d["file"], d["sha256"]) not in seen]

    progress.stage, progress.total = "reading", len(docs)
    progress.files = [{"file": d["file"], "status": "reading", "seconds": None} for d in docs]
    on_update(progress)

    records = []
    index = {d["file"]: i for i, d in enumerate(docs)}
    with ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
        futures = {pool.submit(extract.extract, d, cfg, paths.cache, use_cache): d for d in docs}
        for fut in as_completed(futures):
            d = futures[fut]
            try:
                rec = fut.result()
            except Exception as exc:              # one bad file never sinks the run
                rec = {"file": d["file"], "sha256": d["sha256"], "source_kind": d["source_kind"],
                       "error": f"unexpected error: {exc!r}"[:200]}
            records.append(rec)
            progress.files[index[d["file"]]] = {
                "file": d["file"], "status": "error" if rec.get("error") else "read",
                "seconds": None if rec.get("cached") else round(rec.get("duration_ms", 0) / 1000, 1)}
            on_update(progress)
    records.sort(key=lambda r: r["file"].lower())
    if records and all(r.get("error") for r in records):
        # Nothing could be read: almost always sign-in or connection, not the invoices.
        raise RuntimeError(f"Claude could not read any of the files. First error: {records[0]['error']}")

    progress.stage, progress.message = "checking", "Checking amounts, duplicates and due dates"
    on_update(progress)
    batch = {(r["file"], r["sha256"]) for r in records}
    history = [h for h in ledger if (h["file"], h["sha256"]) not in batch]
    rows = rules.evaluate(records, cfg, history)
    # Flagged first, then by file name: the clerk's work list comes first.
    rows.sort(key=lambda r: (r["flag"] != "Y", r["file"].lower()))
    for row in rows:
        row["source"] = _app_relative(Path(inbox) / row["file"], paths)

    if not rows:                    # nothing new: keep the last real results on screen
        progress.stage, progress.message = "done", "No new invoices"
        on_update(progress)
        return {"run_id": progress.run_id, "trigger": progress.trigger, "files": 0, "review": 0,
                "rows": []}
    run_dir.mkdir(parents=True, exist_ok=True)
    report.write_xlsx(rows, run_dir / "invoices.xlsx")
    report.write_csv(rows, run_dir / "invoices.csv")
    summary = {
        "run_id": progress.run_id, "trigger": progress.trigger,
        "finished": datetime.now().isoformat(timespec="seconds"),
        "source": {"drive_folder": drive_folder} if drive_folder else {"folder": _app_relative(Path(inbox), paths)},
        "files": len(rows), "review": sum(1 for r in rows if r["flag"] == "Y"),
        "usd_review": round(sum(r["amount_usd"] or 0 for r in rows if r["flag"] == "Y"), 2),
        "usd_total": round(sum(r["amount_usd"] or 0 for r in rows), 2),
        "seconds": round(time.time() - progress.started, 1), "model": cfg["model"],
        "ai_cost_usd": round(sum(r.get("cost_usd", 0) for r in records if not r.get("cached")), 3),
        "rows": rows,
    }
    (run_dir / "results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    report.write_summary(summary, run_dir / "summary.md")
    _save_ledger(paths.ledger, ledger, records, progress.run_id)

    progress.stage = "done"
    progress.message = f"{summary['files']} files checked, {summary['review']} to review"
    on_update(progress)
    return summary


# ---------- reading past runs ----------

def latest_run(paths: Paths) -> dict | None:
    if not paths.runs.is_dir():
        return None
    for d in sorted(paths.runs.iterdir(), reverse=True):
        f = d / "results.json"
        if f.is_file():
            try:
                summary = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if summary.get("files"):          # an empty run never hides the last real one
                return summary
    return None
