"""Invoice Triage.

  python -m invoice_triage                          open the app in the browser
  python -m invoice_triage run --drive LINK         check a Drive folder from the command line
  python -m invoice_triage run --folder PATH        check a local folder
  python -m invoice_triage weekly                   the unattended Monday run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import drive, pipeline
from .claude_cli import find_claude
from .paths import Paths, load_config, load_settings


def _utf8_console() -> None:
    # Windows consoles default to a legacy code page; pythonw has no console at all.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _cli_run(args: argparse.Namespace) -> int:
    paths = Paths()
    claude = find_claude(load_settings(paths).get("claude_path") or None)
    if not claude:
        print("Claude Code was not found. Install it from claude.ai/download and sign in.")
        return 2
    cfg = {**load_config(paths), "claude_path": claude}
    if args.model:
        cfg["model"] = args.model
    folder_id = drive.folder_id(args.drive) if args.drive else None
    if args.drive and not folder_id:
        print("That doesn't look like a Google Drive folder link.")
        return 2

    def show(p: pipeline.Progress) -> None:
        if p.stage == "reading":
            done = sum(1 for f in p.files if f["status"] != "reading")
            print(f"\r  Reading {done}/{p.total} files ...", end="", flush=True)
        elif p.message:
            print(f"\n  {p.message}" if p.stage in ("checking", "done") else f"  {p.message}")

    summary = pipeline.run(paths, cfg, drive_folder=folder_id, folder=Path(args.folder) if args.folder else None,
                           only_new=args.only_new, use_cache=not args.no_cache, on_update=show)
    out = paths.runs / summary["run_id"]
    print(f"\n  {summary['files']} files, {summary['review']} need review, "
          f"{summary['files'] - summary['review']} no issues found ({summary['seconds']:.0f}s)")
    print(f"  {out / 'invoices.xlsx'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    ap = argparse.ArgumentParser(prog="invoice_triage", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command")
    r = sub.add_parser("run", help="check invoices from the command line")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--drive", help="Google Drive folder link or id")
    src.add_argument("--folder", help="local folder of invoices")
    r.add_argument("--only-new", action="store_true", help="skip files already checked before")
    r.add_argument("--no-cache", action="store_true", help="have Claude read every file again")
    r.add_argument("--model", help="override the model in config.json")
    sub.add_parser("weekly", help="the unattended Monday run")
    args = ap.parse_args(argv)

    if args.command == "run":
        return _cli_run(args)
    if args.command == "weekly":
        from .weekly import main as weekly_main
        return weekly_main()
    from .server import serve
    return serve()


if __name__ == "__main__":
    sys.exit(main())
