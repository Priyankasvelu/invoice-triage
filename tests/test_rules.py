import unittest
from datetime import date

from invoice_triage import rules

CFG = {
    "company_names": ["Waverock Holdings LLC"],
    "approval_threshold_usd": 5000,
    "fx_to_usd": {"USD": 1.0, "EUR": 1.15},
}


def rec(file, source="pdf_text", **data):
    base = {"document_type": "invoice", "vendor_name": "Acme LLC", "invoice_number": "A-1",
            "invoice_date": "2026-09-01", "due_date": "2026-10-01", "payment_terms": "Net 30",
            "currency": "USD", "total_amount": 100.0, "line_items": [],
            "bill_to_name": "Waverock Holdings LLC", "extraction_confidence": "high",
            "unreadable_fields": [], "review_concerns": []}
    base.update(data)
    return {"file": file, "source_kind": source, "sha256": file, "data": base}


class Normalisation(unittest.TestCase):
    def test_ocr_lookalikes_match(self):
        self.assertEqual(rules.normalize_invoice_number("RW-1O41"),
                         rules.normalize_invoice_number("RW-1041"))

    def test_vendor_suffixes_ignored(self):
        self.assertEqual(rules.normalize_vendor("Summit Office Supply Co."),
                         rules.normalize_vendor("SUMMIT OFFICE SUPPLY"))


class DueDate(unittest.TestCase):
    def test_printed_wins(self):
        self.assertEqual(rules.resolve_due_date("2026-09-01", "2026-09-20", "Net 30")[0],
                         date(2026, 9, 20))

    def test_net_terms(self):
        self.assertEqual(rules.resolve_due_date("2026-09-22", None, "Net 15")[0], date(2026, 10, 7))

    def test_due_on_receipt(self):
        self.assertEqual(rules.resolve_due_date("2026-09-17", None, "Due upon receipt")[0],
                         date(2026, 9, 17))

    def test_unknown(self):
        self.assertIsNone(rules.resolve_due_date("2026-09-17", None, None)[0])


class Checks(unittest.TestCase):
    def flags(self, r):
        return rules.check_document(r, CFG)[0]

    def test_clean_invoice_not_flagged(self):
        self.assertEqual(self.flags(rec("a.pdf")), [])

    def test_threshold_is_strictly_over(self):
        self.assertEqual(self.flags(rec("a.pdf", total_amount=5000.0)), [])
        self.assertTrue(any("threshold" in f for f in self.flags(rec("a.pdf", total_amount=5000.01))))

    def test_eur_converted_before_threshold(self):
        f = self.flags(rec("a.pdf", currency="EUR", total_amount=4700.0))
        self.assertTrue(any("threshold" in x and "5,405.00" in x for x in f))
        self.assertTrue(any("Billed in EUR" in x for x in f))

    def test_math_error(self):
        lines = [{"description": "Paper", "quantity": 10, "unit_price": 48.5, "amount": 485.0},
                 {"description": "Toner", "quantity": 4, "unit_price": 112.0, "amount": 448.0},
                 {"description": "Chair", "quantity": 1, "unit_price": 351.5, "amount": 351.5}]
        f = self.flags(rec("s.pdf", total_amount=1384.5, line_items=lines))
        self.assertTrue(any("$1,284.50" in x and "$100.00 over" in x for x in f))

    def test_wrong_bill_to(self):
        self.assertTrue(any("Billed to" in x for x in self.flags(rec("a.pdf", bill_to_name="Other Corp"))))

    def test_ai_must_check_flags_but_note_does_not(self):
        r = rec("a.pdf", review_concerns=[{"severity": "note", "concern": "installment 2 of 4"}])
        self.assertEqual(self.flags(r), [])
        r = rec("a.pdf", review_concerns=[{"severity": "must_check", "concern": "bank changed"}])
        self.assertEqual(self.flags(r), ["bank changed"])

    def test_read_error_flags(self):
        self.assertTrue(self.flags({"file": "x", "error": "timeout"}))


class Duplicates(unittest.TestCase):
    def test_scan_is_duplicate_of_pdf_not_vice_versa(self):
        rows = rules.evaluate([rec("Redwood_RW-1041.pdf", invoice_number="RW-1041"),
                               rec("Redwood_scan.pdf", source="pdf_scan", invoice_number="RW-1O41")], CFG)
        by = {r["file"]: r for r in rows}
        self.assertEqual(by["Redwood_RW-1041.pdf"]["flag"], "N")
        self.assertIn("Second copy", by["Redwood_RW-1041.pdf"]["notes"])
        self.assertEqual(by["Redwood_scan.pdf"]["flag"], "Y")
        self.assertIn("DUPLICATE of Redwood_RW-1041.pdf", by["Redwood_scan.pdf"]["reason"])

    def test_same_number_different_vendor_is_not_duplicate(self):
        rows = rules.evaluate([rec("a.pdf", vendor_name="Oakridge", invoice_number="OEC-0917"),
                               rec("b.pdf", vendor_name="Keystone", invoice_number="0917")], CFG)
        self.assertEqual([r["flag"] for r in rows], ["N", "N"])

    def test_history_duplicate(self):
        r = rec("new.pdf")
        key = rules.dedupe_keys(r)[0]
        hist = [{"key": key, "file": "old.pdf", "run_date": "2026-11-02", "sha256": "other"}]
        rows = rules.evaluate([r], CFG, hist)
        self.assertIn("already processed on 2026-11-02", rows[0]["reason"])

    def test_identical_bytes_resent_under_new_name_is_duplicate(self):
        r = rec("resend.pdf")
        hist = [{"key": rules.dedupe_keys(r)[0], "file": "orig.pdf", "run_date": "2026-11-02",
                 "sha256": r["sha256"]}]
        self.assertEqual(rules.evaluate([r], CFG, hist)[0]["flag"], "Y")

    def test_history_loose_match_catches_renumbered_resend(self):
        old = rec("old.pdf", invoice_number="A-1")
        new = rec("new.pdf", invoice_number="A-1-R")
        exact, loose = rules.dedupe_keys(old)
        hist = [{"key": exact, "loose_key": loose, "file": "old.pdf", "run_date": "2026-11-02",
                 "sha256": "x"}]
        self.assertEqual(rules.evaluate([new], CFG, hist)[0]["flag"], "Y")

    def test_bill_to_with_address_is_ours(self):
        r = rec("a.pdf", bill_to_name="Waverock Holdings LLC, 1200 Harbor Point Blvd")
        self.assertEqual(rules.check_document(r, CFG)[0], [])

    def test_bad_line_items_do_not_crash(self):
        lines = [{"description": None, "quantity": 2, "unit_price": None, "amount": "abc"},
                 {"description": None, "quantity": 2, "unit_price": 5.0, "amount": 9.0}]
        f = rules.check_document(rec("a.pdf", total_amount=9.0, line_items=lines), CFG)[0]
        self.assertTrue(any("2 × 5.00" in x for x in f))

    def test_ingest_warning_flags(self):
        r = {**rec("a.pdf"), "ingest_warnings": ["only 3 of 5 pages were read"]}
        self.assertEqual(rules.evaluate([r], CFG)[0]["flag"], "Y")

    def test_inputs_not_mutated(self):
        r = rec("a.pdf")
        before = repr(r)
        rules.evaluate([r], CFG)
        self.assertEqual(before, repr(r))


if __name__ == "__main__":
    unittest.main()
