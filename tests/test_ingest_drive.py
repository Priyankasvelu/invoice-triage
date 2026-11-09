import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

from invoice_triage import drive, extract, ingest

LINK = "https://drive.google.com/drive/folders/1MetHWXxUR2j4mb3vdZpqLw7OyH1YM0NT?usp=sharing"


def make_email(attachments: list[tuple[str, bytes, str]]) -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "ar@vendor.example", "ap@waverock.example", "Invoice"
    msg.set_content("Please find the invoice attached.")
    for name, data, ctype in attachments:
        maintype, subtype = ctype.split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


class Ingest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        p = self.dir / name
        p.write_bytes(data)
        return p

    def test_pdf_goes_to_claude_as_a_document(self):
        doc = ingest.load_document(self.write("a.pdf", b"%PDF-1.4 test"))
        self.assertEqual(doc["source_kind"], "pdf")
        self.assertEqual(doc["parts"][0]["kind"], "document")

    def test_email_body_and_attachment(self):
        raw = make_email([("inv.pdf", b"%PDF-1.4", "application/pdf")])
        doc = ingest.load_document(self.write("m.eml", raw))
        self.assertEqual([p["kind"] for p in doc["parts"]], ["text", "document"])
        self.assertIn("Please find the invoice", doc["parts"][0]["text"])
        self.assertEqual(doc["warnings"], [])

    def test_email_with_two_invoices_warns(self):
        raw = make_email([("a.pdf", b"%PDF", "application/pdf"), ("b.jpg", b"\xff\xd8", "image/jpeg")])
        doc = ingest.load_document(self.write("m.eml", raw))
        self.assertTrue(any("2 invoice attachments" in w for w in doc["warnings"]))

    def test_unsupported_and_oversized_files_become_errors(self):
        self.assertIn("unsupported", ingest.load_document(self.write("x.docx", b"x"))["error"])
        big = self.write("big.pdf", b"0" * (ingest.MAX_BYTES + 1))
        self.assertIn("25 MB", ingest.load_document(big)["error"])

    def test_scan_signal_maps_to_source_kind(self):
        self.assertEqual(extract.source_kind("pdf", {"appears_scanned": True}), "pdf_scan")
        self.assertEqual(extract.source_kind("pdf", {"appears_scanned": False}), "pdf_text")
        self.assertEqual(extract.source_kind("image", {"appears_scanned": True}), "image")


class DriveLinks(unittest.TestCase):
    def test_share_link_open_link_and_bare_id(self):
        fid = "1MetHWXxUR2j4mb3vdZpqLw7OyH1YM0NT"
        self.assertEqual(drive.folder_id(LINK), fid)
        self.assertEqual(drive.folder_id(f"https://drive.google.com/open?id={fid}"), fid)
        self.assertEqual(drive.folder_id(f"  {fid} "), fid)

    def test_rejects_non_links(self):
        for bad in ("", "hello", "https://example.com/folders/x"):
            self.assertIsNone(drive.folder_id(bad))


if __name__ == "__main__":
    unittest.main()
