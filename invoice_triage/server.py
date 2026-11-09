"""The local app: a small web server on this computer only, opened in the default browser.

Security model: it listens on 127.0.0.1 only; it rejects requests whose Host header is
not this machine (blocks DNS-rebinding); every API call must carry a token generated at
launch (blocks other websites from driving it); uploads are name-sanitised, type-checked
and size-capped; the page has a strict Content-Security-Policy and renders document
text as text, never as HTML.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import drive, ingest, pipeline, scheduler, weekly
from .claude_cli import readiness
from .paths import APP_DIR, Paths, load_config, load_settings, save_settings

WEB_DIR = Path(__file__).resolve().parent / "web"
PREFERRED_PORT = 8765
MAX_UPLOAD_FILES = 200
_UPLOAD_ID = re.compile(r"^[a-f0-9]{16}$")
_RUN_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


class App:
    """Shared state for the request handlers."""

    def __init__(self, paths: Paths, port: int):
        self.paths = paths
        self.port = port
        self.token = secrets.token_urlsafe(24)
        self.progress: pipeline.Progress | None = None
        self.lock = threading.Lock()
        self._checks: tuple[float, dict, bool] | None = None   # (time, claude, schedule enabled)

    def _slow_checks(self, fresh: bool = False) -> tuple[dict, bool]:
        """Claude sign-in and scheduler status spawn processes; poll them at most every 30 s."""
        now = time.time()
        if fresh or not self._checks or now - self._checks[0] > 30:
            settings = load_settings(self.paths)
            self._checks = (now, readiness(settings.get("claude_path") or None),
                            scheduler.is_enabled(self.paths.root))
        return self._checks[1], self._checks[2]

    # ---- state ----
    def state(self, fresh: bool = False) -> dict:
        settings = load_settings(self.paths)
        claude, enabled = self._slow_checks(fresh)
        with self.lock:
            running = self.progress.as_dict() if self.progress else None
        return {
            "claude": claude,
            "drive_link": settings.get("drive_link", ""),
            "schedule": {"enabled": enabled,
                         "next_run": scheduler.next_run().isoformat() if enabled else None,
                         "last": weekly.last_status(self.paths)},
            "run": running,
            "latest": _public_summary(pipeline.latest_run(self.paths)),
            "threshold": load_config(self.paths)["approval_threshold_usd"],
        }

    # ---- actions ----
    def start_run(self, body: dict) -> dict:
        settings = load_settings(self.paths)
        claude = readiness(settings.get("claude_path") or None)
        if not claude["ok"]:
            raise ValueError(f"{claude['message']}. {claude['fix']}")
        cfg = {**load_config(self.paths), "claude_path": claude["path"]}
        kwargs: dict = {}
        if body.get("source") == "drive":
            folder = drive.folder_id(body.get("drive_link", ""))
            if not folder:
                raise ValueError("That doesn't look like a Google Drive folder link. Copy the link "
                                 "from Drive's Share button and paste it here.")
            save_settings(self.paths, {**settings, "drive_link": body["drive_link"].strip(),
                                       "claude_path": claude["path"]})
            kwargs["drive_folder"] = folder
        elif body.get("source") == "upload" and _UPLOAD_ID.match(body.get("upload_id", "")):
            folder_path = self.paths.uploads / body["upload_id"]
            if not any(folder_path.glob("*")):
                raise ValueError("No files were received. Choose the invoice files again.")
            kwargs["folder"] = folder_path
        else:
            raise ValueError("Choose a Drive folder or files to check.")

        with self.lock:
            if self.progress and self.progress.stage not in ("done", "failed"):
                raise ValueError("A check is already running.")
            progress = pipeline.Progress(run_id=pipeline.new_run_id(), trigger="manual")
            self.progress = progress

        def work() -> None:
            try:
                pipeline.run(self.paths, cfg, progress=progress, **kwargs)
            except Exception as exc:                 # surfaced through progress.error
                progress.stage, progress.error = "failed", _plain_error(exc)

        threading.Thread(target=work, daemon=True).start()
        return {"run_id": progress.run_id}

    def set_schedule(self, enabled: bool) -> dict:
        settings = load_settings(self.paths)
        if enabled:
            if not drive.folder_id(settings.get("drive_link", "")):
                raise ValueError("Run one check with a Google Drive link first; the weekly run "
                                 "checks that same folder.")
            claude = readiness(settings.get("claude_path") or None)
            if not claude["ok"]:
                raise ValueError(f"{claude['message']}. {claude['fix']}")
            save_settings(self.paths, {**settings, "claude_path": claude["path"]})
            scheduler.enable(self.paths.root)
        else:
            scheduler.disable()
        self._checks = None
        return {"enabled": scheduler.is_enabled(self.paths.root)}

    def save_upload(self, upload_id: str, name: str, data: bytes) -> None:
        safe = Path(unquote(name)).name
        if not safe or safe.startswith(".") or Path(safe).suffix.lower() not in ingest.SUPPORTED:
            raise ValueError(f"{safe or 'This file'} is not a PDF, image or email file.")
        folder = self.paths.uploads / upload_id
        folder.mkdir(parents=True, exist_ok=True)
        if len(list(folder.iterdir())) >= MAX_UPLOAD_FILES:
            raise ValueError(f"At most {MAX_UPLOAD_FILES} files per check.")
        (folder / safe).write_bytes(data)


def _public_summary(summary: dict | None) -> dict | None:
    """Results for the page; local file paths stay on the server."""
    if not summary:
        return None
    rows = [{k: v for k, v in r.items() if k not in ("source", "sha256")} for r in summary["rows"]]
    return {**summary, "rows": rows}


def _plain_error(exc: Exception) -> str:
    if isinstance(exc, pipeline.RunBusy):
        return str(exc)
    text = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" in text:
        return ("This Python can't verify secure connections yet. On a Mac, open the Python folder "
                "in Applications and double-click 'Install Certificates.command', then try again.")
    if "urlopen" in text or "Name or service" in text or "getaddrinfo" in text:
        return "Couldn't reach Google Drive. Check the internet connection and that the folder is shared."
    return text or type(exc).__name__


class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "InvoiceTriage"

    def log_message(self, *args) -> None:        # keep the console quiet for non-technical users
        pass

    # ---- guards ----
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _token_ok(self, query: dict) -> bool:
        supplied = self.headers.get("X-Token") or (query.get("t") or [""])[0]
        return secrets.compare_digest(supplied, self.app.token)

    # ---- responses ----
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"error": message}, status)

    def _body(self, limit: int) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError(f"File is larger than {limit // 1_048_576} MB.")
        return self.rfile.read(length)

    # ---- routing ----
    def _dispatch(self, method: str) -> None:
        if not self._host_ok():
            return self._error("Forbidden", HTTPStatus.FORBIDDEN)
        url = urlparse(self.path)
        query = parse_qs(url.query)
        path = url.path
        try:
            if method == "GET" and path == "/":
                html = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("{{TOKEN}}", self.app.token)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if method == "GET" and path in ("/app.css", "/app.js"):
                ctype = "text/css" if path.endswith(".css") else "text/javascript"
                return self._send(200, (WEB_DIR / path[1:]).read_bytes(), f"{ctype}; charset=utf-8")
            if not path.startswith("/api/"):
                return self._error("Not found", HTTPStatus.NOT_FOUND)
            if not self._token_ok(query):
                return self._error("Forbidden", HTTPStatus.FORBIDDEN)
            return self._api(method, path, query)
        except ValueError as exc:
            return self._error(str(exc))
        except Exception as exc:                     # never leak a traceback to the page
            return self._error(f"Something went wrong: {type(exc).__name__}: {exc}", 500)

    def _api(self, method: str, path: str, query: dict) -> None:
        a = self.app
        if method == "GET" and path == "/api/state":
            return self._json(a.state(fresh="fresh" in query))
        if method == "POST" and path == "/api/run":
            return self._json(a.start_run(json.loads(self._body(64_000) or b"{}")))
        if method == "POST" and path == "/api/schedule":
            return self._json(a.set_schedule(bool(json.loads(self._body(1000) or b"{}").get("enabled"))))
        m = re.fullmatch(r"/api/upload/([a-f0-9]{16})/(.+)", path)
        if method == "PUT" and m:
            a.save_upload(m.group(1), m.group(2), self._body(ingest.MAX_BYTES))
            return self._json({"ok": True})
        m = re.fullmatch(r"/api/runs/([0-9_-]+)/(excel|csv|folder|original)", path)
        if m and _RUN_ID.match(m.group(1)):
            return self._run_file(m.group(1), m.group(2), method, query)
        return self._error("Not found", HTTPStatus.NOT_FOUND)

    def _run_file(self, run_id: str, what: str, method: str, query: dict) -> None:
        run_dir = self.app.paths.runs / run_id
        if not run_dir.is_dir():
            return self._error("That run no longer exists.", HTTPStatus.NOT_FOUND)
        if what == "folder" and method == "POST":
            _reveal(run_dir)
            return self._json({"ok": True})
        if what in ("excel", "csv"):
            name = "invoices.xlsx" if what == "excel" else "invoices.csv"
            ctype = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                     if what == "excel" else "text/csv; charset=utf-8")
            stamp = run_id[:10]
            return self._send(200, (run_dir / name).read_bytes(), ctype,
                              {"Content-Disposition": _disposition("attachment", f"Invoice check {stamp}{Path(name).suffix}")})
        if what == "original":
            # Only files that belong to this run's results can be served: no path from the client.
            wanted = (query.get("file") or [""])[0]
            summary = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
            row = next((r for r in summary["rows"] if r["file"] == wanted), None)
            src = Path(row["source"]) if row else None
            if src is not None and not src.is_absolute():
                src = self.app.paths.root / src
            if src is None or not src.is_file():
                return self._error("The original file is no longer available.", HTTPStatus.NOT_FOUND)
            ctype = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
            inline = ctype in ("application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp")
            disposition = "inline" if inline else "attachment"
            return self._send(200, src.read_bytes(), ctype if inline else "application/octet-stream",
                              {"Content-Disposition": _disposition(disposition, src.name)})
        return self._error("Not found", HTTPStatus.NOT_FOUND)

    def do_GET(self) -> None: self._dispatch("GET")
    def do_POST(self) -> None: self._dispatch("POST")
    def do_PUT(self) -> None: self._dispatch("PUT")


def _disposition(kind: str, name: str) -> str:
    """Header-safe for any file name: ASCII fallback plus the exact UTF-8 name (RFC 6266)."""
    ascii_name = re.sub(r'[^A-Za-z0-9._ -]', "_", name) or "file"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def _reveal(folder: Path) -> None:
    """Open a folder in Explorer / Finder / the file manager."""
    if sys.platform == "win32":
        os.startfile(folder)                            # noqa: S606 (local, our own path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])


def _free_port() -> int:
    for port in (PREFERRED_PORT, 0):
        with socket.socket() as s:
            if sys.platform != "win32":
                # Same rule the real server uses; otherwise a just-closed app's port looks taken.
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("No free port")


def _already_running() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PREFERRED_PORT}/", timeout=2) as r:
            return r.headers.get("Server", "").startswith("InvoiceTriage")
    except OSError:
        return False


def make_server(paths: Paths | None = None, port: int | None = None) -> tuple[ThreadingHTTPServer, App]:
    paths = paths or Paths()
    port = _free_port() if port is None else port
    app = App(paths, port)
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    app.port = server.server_address[1]
    return server, app


def serve(open_browser: bool = True) -> int:
    if _already_running():
        url = f"http://127.0.0.1:{PREFERRED_PORT}/"
        print(f"Invoice Triage is already open: {url}")
        if open_browser:
            webbrowser.open(url)
        return 0
    server, app = make_server()
    url = f"http://127.0.0.1:{app.port}/"
    print(f"Invoice Triage is running at {url}\nKeep this window open while you use it. "
          "Close it (or press Ctrl+C) to quit.", flush=True)
    if open_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


__all__ = ["serve", "make_server", "APP_DIR"]
