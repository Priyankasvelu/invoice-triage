import csv
import tempfile
import unittest
import zipfile
import xml.dom.minidom
from pathlib import Path

from invoice_triage import report

ROW = {"file": "x.pdf", "vendor": "=HYPERLINK(\"http://evil\")", "invoice_number": "@SUM(A1)",
       "due_date": "2026-10-01", "amount": 12.5, "flag": "Y", "reason": "bad\x07char",
       "notes": "", "currency": "USD", "amount_usd": 12.5}


class Report(unittest.TestCase):
    def test_csv_neutralises_formulas(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "o.csv"
            report.write_csv([ROW], p)
            with p.open(encoding="utf-8-sig") as f:
                row = next(csv.DictReader(f))
            self.assertTrue(row["vendor"].startswith("'="))
            self.assertTrue(row["invoice_number"].startswith("'@"))
            self.assertEqual(row["amount"], "12.50")

    def test_xlsx_is_well_formed_with_control_chars(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "o.xlsx"
            report.write_xlsx([ROW], p)
            with zipfile.ZipFile(p) as z:
                for name in z.namelist():
                    xml.dom.minidom.parseString(z.read(name))


if __name__ == "__main__":
    unittest.main()
