"""Where everything lives. One place, so the app, the CLI and the scheduler agree."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Paths:
    root: Path = APP_DIR

    @property
    def config(self) -> Path: return self.root / "config.json"
    @property
    def state(self) -> Path: return self.root / "state"
    @property
    def settings(self) -> Path: return self.state / "settings.json"
    @property
    def ledger(self) -> Path: return self.state / "ledger.json"
    @property
    def cache(self) -> Path: return self.state / "cache"
    @property
    def lock(self) -> Path: return self.state / "run.lock"
    @property
    def uploads(self) -> Path: return self.state / "uploads"
    @property
    def runs(self) -> Path: return self.root / "runs"
    @property
    def weekly(self) -> Path: return self.root / "Weekly results"

    def drive_inbox(self, folder_id: str) -> Path:
        return self.state / "drive" / re.sub(r"[^A-Za-z0-9_-]", "", folder_id)


DEFAULT_SETTINGS = {"drive_link": "", "claude_path": ""}


def load_config(paths: Paths) -> dict:
    return json.loads(paths.config.read_text(encoding="utf-8"))


def _home_relative(path: str) -> str:
    """'/home/ana/.local/bin/claude' -> '~/.local/bin/claude', so no username is stored."""
    home = str(Path.home())
    return "~" + path[len(home):] if path and path.startswith(home) else path


def load_settings(paths: Paths) -> dict:
    try:
        stored = {**DEFAULT_SETTINGS, **json.loads(paths.settings.read_text(encoding="utf-8"))}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)
    return {**stored, "claude_path": str(Path(stored["claude_path"]).expanduser()) if stored["claude_path"] else ""}


def save_settings(paths: Paths, settings: dict) -> None:
    paths.state.mkdir(parents=True, exist_ok=True)
    tmp = paths.settings.with_suffix(".tmp")
    stored = {**settings, "claude_path": _home_relative(settings.get("claude_path", ""))}
    tmp.write_text(json.dumps(stored, indent=2), encoding="utf-8")
    tmp.replace(paths.settings)
