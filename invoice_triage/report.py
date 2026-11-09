"""Write the results: invoices.csv, invoices.xlsx (Invoices + Audit tabs), summary.md.
The XLSX writer is standard-library only: no openpyxl or pip needed."""
from __future__ import annotations

import csv
import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

MAIN_COLUMNS = ["file", "vendor", "invoice_number", "due_date", "amount", "flag", "reason"]
AUDIT_COLUMNS = ["file", "source_kind", "vendor", "invoice_number", "invoice_date", "invoice_date_raw",
                 "payment_terms", "due_date", "due_date_basis", "currency", "amount", "amount_usd",
                 "confidence", "flag", "reason", "notes", "sha256"]
NUMERIC = {"amount", "amount_usd"}
WIDTHS = {"file": 30, "vendor": 30, "invoice_number": 18, "due_date": 12, "amount": 12, "flag": 6,
          "reason": 90, "notes": 70, "invoice_date_raw": 18, "payment_terms": 16, "due_date_basis": 34,
          "sha256": 20}


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _safe_text(value) -> str:
    """Neutralise spreadsheet formulas coming from untrusted invoice text."""
    s = str(value)
    return "'" + s if s.startswith(_FORMULA_START) else s


def write_csv(rows: list[dict], path: Path) -> None:
    # utf-8-sig: Excel on Windows then shows € and — correctly.
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=MAIN_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            safe = {c: _safe_text(r.get(c) or "") for c in MAIN_COLUMNS if c != "amount"}
            w.writerow({**safe, "amount": "" if r["amount"] is None else f"{r['amount']:.2f}"})


# ---------- minimal XLSX ----------

def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        s = chr(65 + rem) + s
    return s


def _cell(ref: str, value, style: int) -> str:
    if value is None or value == "":
        return f'<c r="{ref}" s="{style}"/>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    text = escape(_XML_ILLEGAL.sub("", str(value)))
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet(rows: list[dict], columns: list[str]) -> str:
    # styles: 1 header, 2 flagged text, 3 number, 4 flagged number, 0 default
    out = ['<row r="1">' + "".join(_cell(f"{_col(i)}1", c, 1) for i, c in enumerate(columns)) + "</row>"]
    for n, r in enumerate(rows, start=2):
        flagged = r.get("flag") == "Y"
        cells = []
        for i, c in enumerate(columns):
            style = (4 if flagged else 3) if c in NUMERIC else (2 if flagged else 0)
            cells.append(_cell(f"{_col(i)}{n}", r.get(c), style))
        out.append(f'<row r="{n}">' + "".join(cells) + "</row>")
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{WIDTHS.get(c, 14)}" customWidth="1"/>'
                   for i, c in enumerate(columns))
    last = f"{_col(len(columns) - 1)}{len(rows) + 1}"
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
            'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            f'<cols>{cols}</cols><sheetData>{"".join(out)}</sheetData>'
            f'<autoFilter ref="A1:{last}"/></worksheet>')


_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00"/></numFmts>
<fonts count="2"><font><sz val="10"/><name val="Arial"/></font><font><b/><sz val="10"/><name val="Arial"/></font></fonts>
<fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFE8EAED"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFFCE8E6"/></patternFill></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf/></cellStyleXfs>
<cellXfs count="5"><xf/>
<xf fontId="1" fillId="2" applyFont="1" applyFill="1"/>
<xf fillId="3" applyFill="1"/>
<xf numFmtId="164" applyNumberFormat="1"/>
<xf numFmtId="164" fillId="3" applyNumberFormat="1" applyFill="1"/></cellXfs>
</styleSheet>"""


def write_xlsx(rows: list[dict], path: Path) -> None:
    sheets = [("Invoices", MAIN_COLUMNS), ("Audit", AUDIT_COLUMNS)]
    ns = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(len(sheets))) + "</Types>")
        z.writestr("_rels/.rels", f'<?xml version="1.0" encoding="UTF-8"?><Relationships {ns}>'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, (n, _) in enumerate(sheets))
                   + "</sheets>"
                   + "<definedNames>" + "".join(
                       f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">\'{n}\'!$A$1:${_col(len(c) - 1)}${len(rows) + 1}</definedName>'
                       for i, (n, c) in enumerate(sheets)) + "</definedNames></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", f'<?xml version="1.0" encoding="UTF-8"?><Relationships {ns}>'
                   + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
                             for i in range(len(sheets)))
                   + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr("xl/styles.xml", _STYLES)
        for i, (_, cols) in enumerate(sheets):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", _sheet(rows, cols))


# ---------- summary ----------

def write_summary(summary: dict, path: Path) -> None:
    rows = summary["rows"]
    flagged = [r for r in rows if r["flag"] == "Y"]
    lines = [f"# Invoice check, {summary['run_id'][:10]}", "",
             f"{summary['files']} files checked. {summary['review']} need review before payment "
             f"(about ${summary['usd_review']:,.2f} of ${summary['usd_total']:,.2f}). "
             f"{summary['files'] - summary['review']} had no issues found.", ""]
    if flagged:
        lines += ["| File | Vendor | Amount | Why it needs review |", "|---|---|---|---|"]
        for r in flagged:
            amt = "" if r["amount"] is None else f"{r['currency']} {r['amount']:,.2f}"
            lines.append(f"| {r['file']} | {r['vendor']} | {amt} | {r['reason'].replace('|', '/')} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
