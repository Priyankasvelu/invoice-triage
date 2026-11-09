"""Build docs/Invoice Triage overview.pdf from the latest real run.

    python3 docs/overview/build.py

Numbers in the document come from runs/<latest>/results.json, never typed by hand.
Needs Google Chrome (headless print to PDF) and an internet connection for the fonts.
"""
from __future__ import annotations

import html
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from invoice_triage import pipeline  # noqa: E402
from invoice_triage.paths import Paths  # noqa: E402

OUT_PDF = ROOT / "docs" / "Invoice Triage overview.pdf"
OUT_HTML = HERE / "overview.html"
TESTS = 54


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def money(amount, currency="USD") -> str:
    if amount is None:
        return ""
    sym = {"USD": "$", "EUR": "€"}.get(currency, currency + " ")
    return f"{sym}{amount:,.2f}"


def short_date(iso: str) -> str:
    if not iso:
        return ""
    d = date.fromisoformat(iso[:10])
    return d.strftime("%b %-d, %Y")


def appendix_rows(rows: list[dict]) -> str:
    out = []
    for r in sorted(rows, key=lambda r: r["file"].lower()):
        review = r["flag"] == "Y"
        status = '<span class="hold">Review</span>' if review else '<span class="ok">No issues</span>'
        why = "".join(f'<div class="why">{esc(f)}</div>' for f in r.get("flags") or [])
        out.append(
            f'<tr><td class="m">{status}</td><td><div class="file">{esc(r["file"])}</div>{why}</td>'
            f'<td>{esc(r["vendor"])}</td><td class="nw">{esc(r["invoice_number"])}</td>'
            f'<td class="nw">{short_date(r["due_date"])}</td><td class="n">{money(r["amount"], r["currency"])}</td></tr>')
    return "\n".join(out)


def facts(summary: dict) -> dict:
    rows = summary["rows"]
    held = [r for r in rows if r["flag"] == "Y"]
    kinds = Counter(r["source_kind"] for r in rows)

    def count(prefix: str) -> int:
        return sum(1 for r in held if any(f.startswith(prefix) for f in r.get("flags") or []))

    return {
        "files": summary["files"], "held": len(held), "clear": summary["files"] - len(held),
        "usd_held": money(summary["usd_review"]), "usd_total": money(summary["usd_total"]),
        "over": count("Over $"), "dupes": count("DUPLICATE"), "math": count("Math error"),
        "fx": count("Billed in"), "pdf_text": kinds["pdf_text"], "pdf_scan": kinds["pdf_scan"],
        "image": kinds["image"], "email": kinds["email"],
        "printed_due": sum(1 for r in rows if r["due_date_basis"] == "printed"),
        "run_date": short_date(summary["finished"]), "tests": TESTS,
        "appendix": appendix_rows(rows),
    }


def render(f: dict) -> str:
    text = (HERE / "overview.template.html").read_text(encoding="utf-8")
    for k, v in f.items():
        text = text.replace("{{" + k + "}}", str(v))
    if "{{" in text:
        raise SystemExit("Unfilled placeholder: " + text[text.index("{{"):text.index("{{") + 40])
    return text


def print_pdf(html_path: Path, pdf_path: Path) -> None:
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome")
    if not chrome:
        raise SystemExit("Google Chrome is needed to print the PDF.")
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run([chrome, "--headless=new", f"--user-data-dir={profile}", "--no-pdf-header-footer",
                        "--virtual-time-budget=15000", "--run-all-compositor-stages-before-draw",
                        f"--print-to-pdf={pdf_path}", html_path.as_uri()],
                       check=True, capture_output=True, timeout=120)


def main() -> int:
    summary = pipeline.latest_run(Paths())
    if not summary:
        raise SystemExit("No run found. Run a check first.")
    OUT_HTML.write_text(render(facts(summary)), encoding="utf-8")
    print_pdf(OUT_HTML, OUT_PDF)
    print(f"Wrote {OUT_PDF}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
