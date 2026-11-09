"""Find and call the local Claude Code CLI, on Windows, macOS and Linux.

The call is deliberately locked down: no tools, no MCP servers, no user settings,
an empty working directory, and the document passed on stdin. The model can only
read what we hand it and answer in the JSON schema we give it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
# Stop a console window from flashing up for every call when run from pythonw / the scheduler.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ClaudeError(RuntimeError):
    pass


def find_claude(preferred: str | None = None) -> str | None:
    """Absolute path to a usable claude executable, or None."""
    candidates = [preferred] if preferred else []
    candidates.append(shutil.which("claude"))
    home = Path.home()
    candidates += [str(home / ".local" / "bin" / ("claude.exe" if IS_WINDOWS else "claude")),
                   str(home / ".claude" / "local" / "claude")]
    for c in candidates:
        if c and Path(c).is_file() and not _is_batch_shim(c):
            return str(Path(c).absolute())
    return None


def _is_batch_shim(path: str) -> bool:
    # The npm .cmd wrapper routes arguments through cmd.exe, which mangles the JSON
    # schema and empty arguments. Only the native claude.exe is safe to call.
    return Path(path).suffix.lower() in {".cmd", ".bat", ".ps1"}


def readiness(preferred: str | None = None) -> dict:
    """Plain-language status for the app's readiness check."""
    exe = find_claude(preferred)
    if not exe:
        shim = shutil.which("claude")
        if shim and _is_batch_shim(shim):
            fix = ("Claude Code is installed through npm. Install the native version instead: "
                   "open PowerShell and run  irm https://claude.ai/install.ps1 | iex")
        else:
            fix = "Install Claude Code from claude.ai/download, open it once and sign in."
        return {"ok": False, "path": None, "message": "Claude Code not found", "fix": fix}
    if os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": True, "path": exe, "message": "Claude ready (API key)", "fix": ""}
    try:
        out = subprocess.run([exe, "auth", "status"], capture_output=True, encoding="utf-8",
                             errors="replace", timeout=20, creationflags=_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "path": exe, "message": "Claude Code did not start", "fix": str(exc)}
    try:
        signed_in = bool(json.loads(out).get("loggedIn"))
    except (json.JSONDecodeError, AttributeError):
        signed_in = False
    if not signed_in:
        return {"ok": False, "path": exe, "message": "Claude Code is not signed in",
                "fix": "Open Claude Code once and sign in with your Claude account, then reload this page."}
    return {"ok": True, "path": exe, "message": "Claude Code ready", "fix": ""}


def run_structured(exe: str, system_prompt: str, schema: dict, content: list[dict],
                   model: str, timeout_s: int) -> dict:
    """Send one user message (content blocks) and return the schema-validated object."""
    cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--model", model, "--tools", "", "--strict-mcp-config", "--setting-sources", "",
           "--no-session-persistence", "--system-prompt", system_prompt,
           "--json-schema", json.dumps(schema, separators=(",", ":"))]
    if os.environ.get("ANTHROPIC_API_KEY"):
        cmd.insert(2, "--bare")   # API-key mode: same call works on a server with no login
    message = json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"
    with tempfile.TemporaryDirectory() as empty_cwd:
        proc = subprocess.run(cmd, input=message, capture_output=True, encoding="utf-8",
                              errors="replace", timeout=timeout_s, cwd=empty_cwd,
                              creationflags=_NO_WINDOW)
    result = None
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            result = event
    if not result or result.get("is_error") or not result.get("structured_output"):
        detail = (result or {}).get("result") or proc.stderr.strip()[-300:] or "no answer"
        if "login" in detail.lower() or "auth" in detail.lower():
            detail = "Claude Code is not signed in. Open Claude Code once and sign in."
        raise ClaudeError(detail)
    return {"data": result["structured_output"], "cost_usd": result.get("total_cost_usd", 0.0),
            "duration_ms": result.get("duration_ms", 0)}
