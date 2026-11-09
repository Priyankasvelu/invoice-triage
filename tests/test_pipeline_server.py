"""End-to-end without Claude: a stand-in extractor feeds the real pipeline, rules,
report and server. Proves the wiring; accuracy is proven by the live runs."""
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from invoice_triage import pipeline, server
from invoice_triage.paths import Paths

ROOT = Path(__file__).resolve().parent.parent

FAKE = {
    "Redwood_RW-1041.pdf": {"appears_scanned": False, "invoice_number": "RW-1041", "total_amount": 4850.0},
    "Redwood_invoice_scan.pdf": {"appears_scanned": True, "invoice_number": "RW-1O41", "total_amount": 4850.0},
    "Atlas_ARP-3302.pdf": {"appears_scanned": False, "invoice_number": "ARP-3302", "total_amount": 18500.0,
                           "vendor_name": "Atlas Recruiting Partners"},
}


def fake_extract(doc, cfg, cache_dir, use_cache=True):
    data = {"document_type": "invoice", "vendor_name": "Redwood IT Consulting", "invoice_date": "2026-09-12",
            "invoice_date_raw": "09/12/2026", "due_date": None, "payment_terms": "Net 30", "currency": "USD",
            "line_items": [], "bill_to_name": "Waverock Holdings LLC", "remit_bank_details": None,
            "email_context": None, "extraction_confidence": "high", "unreadable_fields": [],
            "review_concerns": [], **FAKE[doc["file"]]}
    kind = "pdf_scan" if data["appears_scanned"] else "pdf_text"
    return {"file": doc["file"], "sha256": doc["sha256"], "source_kind": kind, "ingest_warnings": [],
            "data": data, "cost_usd": 0.0, "duration_ms": 5}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copy(ROOT / "config.json", self.root / "config.json")
        self.paths = Paths(self.root)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        for name in FAKE:
            (self.inbox / name).write_bytes(f"%PDF-1.4 {name}".encode())
        self.cfg = {**json.loads((self.root / "config.json").read_text()), "claude_path": "unused"}
        patcher = mock.patch("invoice_triage.extract.extract", side_effect=fake_extract)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()


class Pipeline(Base):
    def test_run_writes_outputs_and_ranks_scan_as_the_copy(self):
        summary = pipeline.run(self.paths, self.cfg, folder=self.inbox)
        by = {r["file"]: r for r in summary["rows"]}
        self.assertEqual(by["Redwood_RW-1041.pdf"]["flag"], "N")
        self.assertIn("DUPLICATE of Redwood_RW-1041.pdf", by["Redwood_invoice_scan.pdf"]["reason"])
        self.assertEqual(by["Atlas_ARP-3302.pdf"]["flag"], "Y")
        self.assertEqual(summary["rows"][0]["flag"], "Y")            # work list first
        run_dir = self.paths.runs / summary["run_id"]
        for name in ("invoices.xlsx", "invoices.csv", "results.json", "summary.md"):
            self.assertTrue((run_dir / name).is_file(), name)
        self.assertFalse(self.paths.lock.exists())

    def test_weekly_mode_skips_files_already_checked(self):
        pipeline.run(self.paths, self.cfg, folder=self.inbox)
        again = pipeline.run(self.paths, self.cfg, folder=self.inbox, only_new=True,
                             progress=pipeline.Progress(run_id="2099-01-01_000000", trigger="weekly"))
        self.assertEqual(again["files"], 0)

    def test_a_second_run_is_refused_while_one_holds_the_lock(self):
        self.paths.state.mkdir(parents=True, exist_ok=True)
        pipeline._acquire_lock(self.paths.lock, "x")
        with self.assertRaises(pipeline.RunBusy):
            pipeline.run(self.paths, self.cfg, folder=self.inbox)


class Server(Base):
    def setUp(self):
        super().setUp()
        self.httpd, self.app = server.make_server(self.paths, port=0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.app.port}"
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def call(self, path, method="GET", body=None, token=True, host=None):
        req = urllib.request.Request(self.base + path, data=body, method=method)
        if token:
            req.add_header("X-Token", self.app.token)
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def test_page_embeds_token_and_sets_csp(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(self.app.token, r.read().decode())
            self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])

    def test_api_requires_token(self):
        self.assertEqual(self.call("/api/state", token=False)[0], 403)

    def test_foreign_host_header_is_rejected(self):
        self.assertEqual(self.call("/api/state", host="evil.example")[0], 403)

    def test_upload_rejects_wrong_type_and_strips_paths(self):
        uid = "0123456789abcdef"
        self.assertEqual(self.call(f"/api/upload/{uid}/evil.exe", "PUT", b"x")[0], 400)
        status, _ = self.call(f"/api/upload/{uid}/..%2F..%2Finv.pdf", "PUT", b"%PDF")
        self.assertEqual(status, 200)
        self.assertTrue((self.paths.uploads / uid / "inv.pdf").is_file())
        self.assertFalse((self.root / "inv.pdf").exists())

    def test_original_is_served_only_for_files_in_the_run(self):
        summary = pipeline.run(self.paths, self.cfg, folder=self.inbox)
        rid = summary["run_id"]
        ok = self.call(f"/api/runs/{rid}/original?file=Atlas_ARP-3302.pdf")
        self.assertEqual(ok[0], 200)
        self.assertEqual(self.call(f"/api/runs/{rid}/original?file=../config.json")[0], 404)
        self.assertEqual(self.call(f"/api/runs/{rid}/excel")[0], 200)

    def test_state_hides_local_paths(self):
        pipeline.run(self.paths, self.cfg, folder=self.inbox)
        with mock.patch("invoice_triage.server.readiness",
                        return_value={"ok": True, "path": "x", "message": "", "fix": ""}), \
             mock.patch("invoice_triage.scheduler.is_enabled", return_value=False):
            status, body = self.call("/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertNotIn("source", state["latest"]["rows"][0])
        self.assertEqual(state["latest"]["review"], 2)



class Weekly(Base):
    def test_failure_on_a_fresh_install_leaves_a_plain_note(self):
        from invoice_triage import weekly
        self.assertEqual(weekly.main(self.paths), 2)
        note = (self.paths.weekly / weekly.FAILED_NOTE).read_text(encoding="utf-8")
        self.assertIn("No Google Drive folder is set", note)
        self.assertFalse(weekly.last_status(self.paths)["ok"])

    def test_success_removes_an_old_failure_note(self):
        from invoice_triage import weekly
        from invoice_triage.paths import save_settings
        weekly.main(self.paths)                                  # leaves a failure note
        save_settings(self.paths, {"drive_link": "1MetHWXxUR2j4mb3vdZpqLw7OyH1YM0NT", "claude_path": ""})
        empty = {"run_id": "2099-01-01_000000", "files": 0, "review": 0}
        with mock.patch("invoice_triage.weekly.find_claude", return_value="/bin/true"), \
             mock.patch("invoice_triage.pipeline.run", return_value=empty):
            self.assertEqual(weekly.main(self.paths), 0)
        self.assertFalse((self.paths.weekly / weekly.FAILED_NOTE).exists())
        self.assertEqual(weekly.last_status(self.paths)["message"], "No new invoices this week")



class ReviewRegressions(Base):
    def test_lock_left_by_a_dead_process_is_cleared(self):
        self.paths.state.mkdir(parents=True, exist_ok=True)
        self.paths.lock.write_text(json.dumps({"run_id": "x", "pid": 2_000_000_000, "at": 9e12}))
        summary = pipeline.run(self.paths, self.cfg, folder=self.inbox)   # does not raise RunBusy
        self.assertEqual(summary["files"], 3)

    def test_run_where_nothing_could_be_read_fails_loudly(self):
        def broken(doc, *a, **k):
            return {"file": doc["file"], "sha256": doc["sha256"], "source_kind": "pdf",
                    "error": "AI extraction failed: Claude Code is not signed in."}
        with mock.patch("invoice_triage.extract.extract", side_effect=broken):
            with self.assertRaises(RuntimeError) as ctx:
                pipeline.run(self.paths, self.cfg, folder=self.inbox)
        self.assertIn("not signed in", str(ctx.exception))
        self.assertFalse(self.paths.lock.exists())

    def test_empty_weekly_run_keeps_the_last_results_on_screen(self):
        first = pipeline.run(self.paths, self.cfg, folder=self.inbox)
        pipeline.run(self.paths, self.cfg, folder=self.inbox, only_new=True,
                     progress=pipeline.Progress(run_id="2099-01-01_000000", trigger="weekly"))
        self.assertEqual(pipeline.latest_run(self.paths)["run_id"], first["run_id"])

    def test_windows_illegal_and_colliding_drive_names(self):
        from invoice_triage import drive
        used: set = set()
        a = drive.safe_name('Inv: ACME?.pdf', used); used.add(a.lower())
        b = drive.safe_name('Inv| ACME*.pdf', used)
        self.assertEqual(a, "Inv_ ACME_.pdf")
        self.assertEqual(b, "Inv_ ACME_ (2).pdf")
        self.assertEqual(drive.safe_name("../../x.pdf", set()), "x.pdf")

    def test_content_disposition_survives_non_latin_names(self):
        header = server._disposition("inline", "Rechnung €12 Łódź 請求書.pdf")
        header.encode("latin-1")                                   # must not raise
        self.assertIn("filename*=UTF-8''", header)



class NoPersonalPaths(Base):
    def test_run_results_store_paths_relative_to_the_app_folder(self):
        summary = pipeline.run(self.paths, self.cfg, folder=self.inbox)
        stored = (self.paths.runs / summary["run_id"] / "results.json").read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), stored)
        self.assertEqual(summary["rows"][0]["source"].split("/")[0], "inbox")

    def test_settings_never_store_the_home_folder(self):
        from invoice_triage.paths import load_settings, save_settings
        exe = str(Path.home() / ".local" / "bin" / "claude")
        save_settings(self.paths, {"drive_link": "", "claude_path": exe})
        self.assertNotIn(str(Path.home()), self.paths.settings.read_text(encoding="utf-8"))
        self.assertEqual(load_settings(self.paths)["claude_path"], exe)


if __name__ == "__main__":
    unittest.main()
