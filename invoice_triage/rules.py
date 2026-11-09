"""Deterministic payment-safety checks.

The AI only *reads* documents. Every decision that sets flag=Y lives here, in
plain code, so it is auditable, repeatable and cannot be talked out of a flag
by text printed on an invoice (prompt injection).
"""
from __future__ import annotations

import re
from datetime import date, timedelta

# Lower number = more trustworthy copy. Used to pick the "original" in a duplicate group.
SOURCE_PRIORITY = {"pdf_text": 0, "email": 1, "pdf_scan": 2, "image": 3}

_VENDOR_NOISE = re.compile(r"\b(llc|llp|inc|co|corp|corporation|ltd|gmbh|company|the)\b")
_CURRENCY_SYMBOL = {"USD": "$", "EUR": "€", "GBP": "£"}


# ---------- normalisation helpers ----------

def normalize_invoice_number(raw: str | None) -> str:
    """Make OCR look-alikes compare equal: RW-1O41 == RW-1041, 'ab 12' == 'AB12'."""
    if not raw:
        return ""
    s = raw.upper().replace("O", "0").replace("I", "1").replace("L", "1")
    return re.sub(r"[^A-Z0-9]", "", s)


def normalize_vendor(raw: str | None) -> str:
    if not raw:
        return ""
    s = re.sub(r"[^a-z0-9 ]", " ", raw.lower())
    s = _VENDOR_NOISE.sub(" ", s)
    return " ".join(s.split())


def parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def money(amount: float | None, currency: str = "USD") -> str:
    if amount is None:
        return "?"
    sym = _CURRENCY_SYMBOL.get(currency)
    return f"{sym}{amount:,.2f}" if sym else f"{currency} {amount:,.2f}"


# ---------- due date ----------

def terms_to_days(terms: str | None) -> int | None:
    if not terms:
        return None
    t = terms.lower()
    m = re.search(r"net\s*(\d{1,3})", t)
    if m:
        return int(m.group(1))
    if "receipt" in t or "immediate" in t:
        return 0
    return None


def resolve_due_date(invoice_date: str | None, printed_due: str | None,
                     terms: str | None) -> tuple[date | None, str]:
    """Printed due date wins; otherwise derive from terms. Returns (date, basis)."""
    due = parse_iso(printed_due)
    if due:
        return due, "printed"
    inv = parse_iso(invoice_date)
    days = terms_to_days(terms)
    if inv and days is not None:
        return inv + timedelta(days=days), f"invoice date + {days} days ({terms})"
    return None, "unknown"


# ---------- per-document checks ----------

def check_document(rec: dict, cfg: dict) -> tuple[list[str], list[str], dict]:
    """Return (flag_reasons, notes, derived_fields) for one extracted document."""
    d = rec.get("data") or {}
    flags: list[str] = []
    notes: list[str] = []

    if rec.get("error"):
        return [f"Could not be read automatically ({rec['error']}); enter manually"], [], {}
    flags.extend(rec.get("ingest_warnings") or [])

    doc_type = d.get("document_type") or "invoice"
    if doc_type != "invoice":
        flags.append(f"Document looks like a {doc_type.replace('_', ' ')}, not a payable invoice")

    missing = [k for k in ("vendor_name", "invoice_number", "total_amount") if d.get(k) in (None, "")]
    if missing:
        flags.append("Missing " + ", ".join(m.replace("_", " ") for m in missing) + "; check the source")

    currency = (d.get("currency") or "USD").upper()
    total = d.get("total_amount") if isinstance(d.get("total_amount"), (int, float)) else None
    fx = cfg["fx_to_usd"].get(currency)
    amount_usd = round(total * fx, 2) if (total is not None and fx) else None

    # 1. approval threshold
    limit = cfg["approval_threshold_usd"]
    if amount_usd is not None and amount_usd > limit:
        if currency == "USD":
            flags.append(f"Over ${limit:,.0f} approval threshold ({money(total)})")
        else:
            flags.append(f"Over ${limit:,.0f} approval threshold ({money(total, currency)} "
                         f"≈ {money(amount_usd)} at {fx} {currency}/USD)")

    # 2. currency
    if currency != "USD":
        if fx is None:
            flags.append(f"Billed in {currency}; no FX rate configured, so USD value unknown")
        else:
            flags.append(f"Billed in {currency}, not USD: confirm FX and international wire details")

    # 3. arithmetic
    flags.extend(check_math(d.get("line_items") or [], total, currency))

    # 4. bill-to
    bill_to = normalize_vendor(d.get("bill_to_name"))
    ours = [normalize_vendor(n) for n in cfg["company_names"]]
    if not bill_to:
        notes.append("No bill-to name on the document")
    elif not any(bill_to.startswith(o) or o.startswith(bill_to) for o in ours):
        flags.append(f"Billed to '{d.get('bill_to_name')}', not Waverock")

    # 5. due date
    due, basis = resolve_due_date(d.get("invoice_date"), d.get("due_date"), d.get("payment_terms"))
    inv_date = parse_iso(d.get("invoice_date"))
    if due is None:
        flags.append("No due date or payment terms found; confirm when payment is due")
    elif inv_date and due < inv_date:
        flags.append(f"Due date {due} is before invoice date {inv_date}")

    # 6. read quality
    if d.get("extraction_confidence") == "low" or d.get("unreadable_fields"):
        unreadable = ", ".join(d.get("unreadable_fields") or []) or "some fields"
        flags.append(f"Low-confidence read ({unreadable}); verify against the original")

    # 7. AI judgement (content concerns the code can't see, e.g. 'bank details changed')
    for c in d.get("review_concerns") or []:
        text = (c.get("concern") or "").strip()
        if not text:
            continue
        (flags if c.get("severity") == "must_check" else notes).append(text)

    if d.get("email_context"):
        notes.append("Email: " + d["email_context"])

    derived = {
        "currency": currency,
        "amount_usd": amount_usd,
        "due_date": due.isoformat() if due else "",
        "due_date_basis": basis,
    }
    return flags, notes, derived


def check_math(lines: list[dict], total: float | None, currency: str = "USD") -> list[str]:
    out: list[str] = []
    amounts = []
    for li in lines:
        qty, unit, amt = li.get("quantity"), li.get("unit_price"), li.get("amount")
        if not isinstance(amt, (int, float)):
            continue
        amounts.append(amt)
        if isinstance(qty, (int, float)) and isinstance(unit, (int, float)) and abs(qty * unit - amt) > 0.011:
            out.append(f"Line '{(li.get('description') or '')[:40]}': {qty} × {unit:,.2f} = "
                       f"{qty * unit:,.2f}, but invoice shows {amt:,.2f}")
    if amounts and total is not None:
        s = round(sum(amounts), 2)
        if abs(s - total) > 0.011:
            diff = round(total - s, 2)
            direction = "over" if diff > 0 else "under"
            out.append(f"Math error: line items sum to {money(s, currency)} but total due is "
                       f"{money(total, currency)} ({money(abs(diff), currency)} {direction}-billed)")
    return out


# ---------- cross-document checks ----------

def dedupe_keys(rec: dict) -> tuple[str, str]:
    d = rec.get("data") or {}
    vendor = normalize_vendor(d.get("vendor_name"))
    exact = f"{vendor}|{normalize_invoice_number(d.get('invoice_number'))}"
    loose = f"{vendor}|{d.get('total_amount')}|{d.get('invoice_date')}"
    return exact, loose


def find_duplicates(records: list[dict], history: list[dict]) -> tuple[dict, dict]:
    """Return (flag_reasons, notes), each mapping file -> list[str]. Within the
    batch the best-quality copy is treated as the original and every other copy
    is flagged. Anything matching an invoice from an earlier run is flagged too."""
    reasons: dict[str, list[str]] = {}
    notes: dict[str, list[str]] = {}
    usable = [r for r in records if not r.get("error") and (r.get("data") or {}).get("invoice_number")]

    def rank(r: dict) -> tuple:
        return (SOURCE_PRIORITY.get(r.get("source_kind"), 9), r["file"])

    paired: set[tuple[str, str]] = set()
    for key_idx, label in ((0, "same vendor and invoice #"), (1, "same vendor, amount and date")):
        groups: dict[str, list[dict]] = {}
        for r in usable:
            groups.setdefault(dedupe_keys(r)[key_idx], []).append(r)
        for group in groups.values():
            if len(group) < 2:
                continue
            original, *copies = sorted(group, key=rank)
            for c in copies:
                if (c["file"], original["file"]) in paired:
                    continue
                paired.add((c["file"], original["file"]))
                reasons.setdefault(c["file"], []).append(
                    f"DUPLICATE of {original['file']} ({label}); pay only once")
                notes.setdefault(original["file"], []).append(
                    f"Second copy received as {c['file']} (flagged there)")

    hist_exact = {h["key"]: h for h in history}
    hist_loose = {h["loose_key"]: h for h in history if h.get("loose_key")}
    for r in usable:
        exact, loose = dedupe_keys(r)
        h = hist_exact.get(exact) or hist_loose.get(loose)
        if h:
            reasons.setdefault(r["file"], []).append(
                f"DUPLICATE of {h['file']} already processed on {h['run_date']}; check it wasn't paid")
    return reasons, notes


def evaluate(records: list[dict], cfg: dict, history: list[dict] | None = None) -> list[dict]:
    """Return new row dicts (inputs are not mutated)."""
    dupes, dupe_notes = find_duplicates(records, history or [])
    rows = []
    for rec in records:
        flags, notes, derived = check_document(rec, cfg)
        flags = dupes.get(rec["file"], []) + flags
        notes = dupe_notes.get(rec["file"], []) + notes
        d = rec.get("data") or {}
        rows.append({
            "file": rec["file"],
            "vendor": d.get("vendor_name") or "",
            "invoice_number": d.get("invoice_number") or "",
            "due_date": derived.get("due_date", ""),
            "amount": d.get("total_amount"),
            "flag": "Y" if flags else "N",
            "flags": flags,              # structured, for the app; never re-parse the text
            "context": notes,
            # Flagged rows also carry context (e.g. 'installment 2 of 4') so the approver sees it.
            "reason": "; ".join(flags) + (" | Context: " + "; ".join(notes) if flags and notes else ""),
            "notes": "; ".join(notes),
            "source_kind": rec.get("source_kind", ""),
            "invoice_date": d.get("invoice_date") or "",
            "invoice_date_raw": d.get("invoice_date_raw") or "",
            "payment_terms": d.get("payment_terms") or "",
            "due_date_basis": derived.get("due_date_basis", ""),
            "currency": derived.get("currency", ""),
            "amount_usd": derived.get("amount_usd"),
            "confidence": d.get("extraction_confidence") or "",
            "sha256": rec.get("sha256", ""),
        })
    return rows
