"""Turn any file in the inbox into a Document: a list of parts Claude can read.

PDFs and images go to Claude as-is (Claude reads PDFs natively, scanned or not),
so the only requirement is Python itself; no PDF tooling to install. Emails are
split here into headers + body text and their attachments.
"""
from __future__ import annotations

import base64
import email
import email.policy
import hashlib
from pathlib import Path

# Bump when the way documents are presented to the model changes; it is part of
# the cache key, so old answers are never reused for a different input.
INGEST_VERSION = "2"

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}
SUPPORTED = set(IMAGE_TYPES) | {".pdf", ".eml"}
MAX_BYTES = 25 * 1024 * 1024


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(text: str, label: str) -> dict:
    return {"kind": "text", "label": label, "text": text}


def _binary(kind: str, data: bytes, media_type: str, label: str) -> dict:
    return {"kind": kind, "label": label, "media_type": media_type,
            "data_b64": base64.b64encode(data).decode("ascii")}


def _attachment_part(name: str, ctype: str, payload: bytes) -> dict | None:
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf" or ctype == "application/pdf":
        return _binary("document", payload, "application/pdf", f"attachment {name}")
    if suffix in IMAGE_TYPES:
        return _binary("image", payload, IMAGE_TYPES[suffix], f"attachment {name}")
    return None


def _email(raw: bytes) -> tuple[list[dict], list[str]]:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    header = (f"From: {msg['from']}\nTo: {msg['to']}\nDate: {msg['date']}\n"
              f"Subject: {msg['subject']}\n")
    bodies: list[str] = []
    attachments: list[dict] = []
    skipped: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        name = part.get_filename()
        if not name:
            if part.get_content_type() == "text/plain":
                bodies.append(part.get_content())
            continue
        att = _attachment_part(name, part.get_content_type(), part.get_payload(decode=True) or b"")
        if att:
            attachments.append(att)
        else:
            skipped.append(name)

    warnings = []
    if len(attachments) > 1:
        warnings.append(f"Email has {len(attachments)} invoice attachments but one row was "
                        "produced; split and check each")
    if skipped:
        warnings.append("Email attachment(s) not readable: " + ", ".join(skipped))
    body = _text(header + "\n" + "\n".join(bodies), "email headers and body")
    return [body] + attachments, warnings


def load_document(path: Path) -> dict:
    """Return {file, sha256, source_kind, parts, warnings} or the same with an 'error'."""
    data = path.read_bytes()
    doc = {"file": path.name, "sha256": sha256(data), "warnings": []}
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        return {**doc, "source_kind": "unsupported", "parts": [],
                "error": f"unsupported file type {suffix or '(none)'}"}
    if len(data) > MAX_BYTES:
        return {**doc, "source_kind": "too_large", "parts": [],
                "error": f"file is {len(data) // 1_048_576} MB; the limit is 25 MB"}
    if suffix == ".pdf":
        return {**doc, "source_kind": "pdf",
                "parts": [_binary("document", data, "application/pdf", path.name)]}
    if suffix in IMAGE_TYPES:
        return {**doc, "source_kind": "image",
                "parts": [_binary("image", data, IMAGE_TYPES[suffix], path.name)]}
    try:
        parts, warnings = _email(data)
    except (ValueError, LookupError) as exc:
        return {**doc, "source_kind": "email", "parts": [], "error": f"could not parse email: {exc}"}
    return {**doc, "source_kind": "email", "parts": parts, "warnings": warnings}


def list_inbox(inbox: Path) -> list[Path]:
    if not inbox.is_dir():
        return []
    return sorted((p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")),
                  key=lambda p: p.name.lower())
