"""Pull new files from a Google Drive folder shared as 'anyone with the link'.
No credentials needed. For a private folder you'd swap this for the Drive API
with a service account (files.list + files.get?alt=media), same interface."""
from __future__ import annotations

import html
import os
import re
import urllib.request
from pathlib import Path

_ENTRY = re.compile(r'href="https://drive\.google\.com/file/d/([A-Za-z0-9_-]+)/view[^"]*"[^>]*>.*?'
                    r'flip-entry-title">([^<]+)<', re.S)
_UA = {"User-Agent": "invoice-triage/1.0"}


_FOLDER_URL = re.compile(r"/folders/([A-Za-z0-9_-]{10,})|[?&]id=([A-Za-z0-9_-]{10,})")
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{10,}$")


def folder_id(link: str) -> str | None:
    """Accept what people actually paste: a share link, an 'open?id=' link, or the bare id."""
    link = (link or "").strip()
    if _BARE_ID.match(link):
        return link
    m = _FOLDER_URL.search(link)
    return (m.group(1) or m.group(2)) if m else None


def _get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=60) as r:
        return r.read()


def list_folder(folder_id: str) -> list[tuple[str, str]]:
    page = _get(f"https://drive.google.com/embeddedfolderview?id={folder_id}").decode("utf-8", "replace")
    return [(fid, html.unescape(name)) for fid, name in _ENTRY.findall(page)]


_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str, used: set[str]) -> str:
    """A file name that is legal on Windows, macOS and Linux, unique within the folder."""
    base = _ILLEGAL.sub("_", Path(name.replace("\\", "/")).name).strip(" .") or "file"
    stem, suffix = os.path.splitext(base)
    candidate, n = base, 2
    while candidate.lower() in used:
        candidate, n = f"{stem} ({n}){suffix}", n + 1
    return candidate


def sync_folder(folder_id: str, inbox: Path) -> tuple[int, int]:
    """Download files not yet in the inbox. Returns (total_in_folder, newly_downloaded)."""
    inbox.mkdir(parents=True, exist_ok=True)
    entries = list_folder(folder_id)
    new = 0
    used: set[str] = set()
    for fid, name in entries:
        safe = safe_name(name, used)
        used.add(safe.lower())
        if not safe or safe.startswith(".") or (inbox / safe).exists():
            continue
        data = _get(f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t")
        if data[:15].lower().startswith(b"<!doctype html"):
            raise RuntimeError(f"Drive returned a web page instead of {safe}; is the folder public?")
        tmp = inbox / f".{safe}.part"
        tmp.write_bytes(data)
        os.replace(tmp, inbox / safe)               # a half-finished download never looks complete
        new += 1
    return len(entries), new
