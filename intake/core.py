"""Token-link intake: a client submits a contact list for a campaign without database access.

Lifecycle of an invitation:  created -> (opened) -> submitted | revoked | expired.
- Only a SHA-256 hash of the token is stored; the raw token exists only in the link.
- A token can be submitted exactly once: the second submission gets 409.
- Consuming the token and storing the contacts happen in one transaction.
- Every state change is written to an audit log.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

__all__ = [
    "IntakeError", "connect", "create_invitation", "lookup", "submit", "revoke",
    "parse_contacts", "normalize_phone", "REQUIRED_CONFIRMATIONS",
]

REQUIRED_CONFIRMATIONS = ("data_ownership", "lawful_basis", "campaign_owner")
MAX_ROWS = 5000
MAX_CSV_BYTES = 2_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS invitations (
    id           INTEGER PRIMARY KEY,
    token_sha256 TEXT NOT NULL UNIQUE,
    campaign_id  TEXT NOT NULL,
    campaign_name TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    opened_at    TEXT,
    submitted_at TEXT,
    revoked_at   TEXT
);
CREATE TABLE IF NOT EXISTS contacts (
    campaign_id  TEXT NOT NULL,
    phone_e164   TEXT NOT NULL,
    company      TEXT NOT NULL,
    contact_name TEXT,
    source_row   INTEGER NOT NULL,
    PRIMARY KEY (campaign_id, phone_e164)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    invitation_id INTEGER,
    event         TEXT NOT NULL,
    detail        TEXT
);
"""


class IntakeError(Exception):
    def __init__(self, status: int, message: str, details: list | None = None):
        super().__init__(message)
        self.status, self.message, self.details = status, message, details or []


@dataclass
class ParsedContacts:
    contacts: list[dict]
    rejected: list[dict]
    duplicates: int


def _now(clock=None) -> datetime:
    return clock() if clock else datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def connect(path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def _audit(connection, invitation_id, event, detail=""):
    connection.execute(
        "INSERT INTO audit_log (at, invitation_id, event, detail) VALUES (?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), invitation_id, event, detail),
    )


def create_invitation(connection, campaign_id: str, campaign_name: str, days_valid: int = 14, clock=None):
    """Return (invitation_id, token). The token is shown once and not stored."""
    token = secrets.token_urlsafe(32)
    now = _now(clock)
    cursor = connection.execute(
        "INSERT INTO invitations (token_sha256, campaign_id, campaign_name, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (_hash(token), campaign_id, campaign_name, now.isoformat(), (now + timedelta(days=days_valid)).isoformat()),
    )
    _audit(connection, cursor.lastrowid, "created", campaign_id)
    return cursor.lastrowid, token


def _find(connection, token: str):
    if not token or len(token) > 200:
        raise IntakeError(404, "invitation not found")
    row = connection.execute("SELECT * FROM invitations WHERE token_sha256 = ?", (_hash(token),)).fetchone()
    if row is None:
        raise IntakeError(404, "invitation not found")
    return row


def _check_open(row, clock=None):
    if row["revoked_at"]:
        raise IntakeError(410, "this link was withdrawn")
    if row["submitted_at"]:
        raise IntakeError(409, "this form has already been submitted")
    if datetime.fromisoformat(row["expires_at"]) <= _now(clock):
        raise IntakeError(410, "this link has expired")


def lookup(connection, token: str, clock=None) -> dict:
    row = _find(connection, token)
    _check_open(row, clock)
    if not row["opened_at"]:
        connection.execute("UPDATE invitations SET opened_at = ? WHERE id = ?", (_now(clock).isoformat(), row["id"]))
        _audit(connection, row["id"], "opened")
    return {"campaign_name": row["campaign_name"], "expires_at": row["expires_at"],
            "required_confirmations": list(REQUIRED_CONFIRMATIONS)}


def normalize_phone(raw: str) -> str | None:
    """Minimal German normalization to E.164; see e164-normalizer for the full rules."""
    text = (raw or "").strip()
    if not text or not re.fullmatch(r"[\d\s()+/.-]+", text):
        return None
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        national = digits[2:] if digits.startswith("49") else None
    elif digits.startswith("0049"):
        national = digits[4:]
    elif digits.startswith("0"):
        national = digits[1:]
    else:
        return None
    if not national or national.startswith("0") or not 8 <= len(national) <= 13:
        return None
    return "+49" + national


def parse_contacts(csv_text: str) -> ParsedContacts:
    """Columns: company, phone, contact_name (optional). Header names are case-insensitive."""
    if len(csv_text.encode()) > MAX_CSV_BYTES:
        raise IntakeError(413, "file too large")
    sample = csv_text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("﻿")), dialect=dialect)
    fields = {(name or "").strip().lower(): name for name in reader.fieldnames or []}
    missing = [c for c in ("company", "phone") if c not in fields]
    if missing:
        raise IntakeError(422, "missing columns", [f"column '{c}' is required" for c in missing])

    contacts, rejected, seen, duplicates = [], [], set(), 0
    for number, record in enumerate(reader, start=2):  # row 1 is the header
        if number - 1 > MAX_ROWS:
            raise IntakeError(413, f"more than {MAX_ROWS} rows")
        company = (record.get(fields["company"]) or "").strip()
        phone = normalize_phone(record.get(fields["phone"]) or "")
        if not company:
            rejected.append({"row": number, "reason": "company is empty"})
            continue
        if phone is None:
            rejected.append({"row": number, "reason": "phone is not a dialable German number"})
            continue
        if phone in seen:
            duplicates += 1
            continue
        seen.add(phone)
        name = (record.get(fields.get("contact_name", ""), "") or "").strip() or None
        contacts.append({"company": company, "phone_e164": phone, "contact_name": name, "source_row": number})
    return ParsedContacts(contacts, rejected, duplicates)


def submit(connection, token: str, csv_text: str, confirmations: dict, clock=None) -> dict:
    missing = [c for c in REQUIRED_CONFIRMATIONS if confirmations.get(c) is not True]
    if missing:
        raise IntakeError(400, "all confirmations must be accepted", missing)
    parsed = parse_contacts(csv_text)  # validate before touching the token
    if not parsed.contacts:
        raise IntakeError(422, "no valid contacts", parsed.rejected[:20])

    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _find(connection, token)
        _check_open(row, clock)
        consumed = connection.execute(
            "UPDATE invitations SET submitted_at = ? WHERE id = ? AND submitted_at IS NULL AND revoked_at IS NULL",
            (_now(clock).isoformat(), row["id"]),
        ).rowcount
        if consumed != 1:  # lost a race with a parallel submission
            raise IntakeError(409, "this form has already been submitted")
        inserted = 0
        for contact in parsed.contacts:
            inserted += connection.execute(
                "INSERT OR IGNORE INTO contacts (campaign_id, phone_e164, company, contact_name, source_row) "
                "VALUES (?, ?, ?, ?, ?)",
                (row["campaign_id"], contact["phone_e164"], contact["company"], contact["contact_name"],
                 contact["source_row"]),
            ).rowcount
        _audit(connection, row["id"], "submitted",
               f"accepted={inserted} rejected={len(parsed.rejected)} duplicates={parsed.duplicates}")
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return {"accepted": inserted, "rejected": parsed.rejected, "duplicates_in_file": parsed.duplicates,
            "already_in_campaign": len(parsed.contacts) - inserted}


def revoke(connection, invitation_id: int) -> None:
    changed = connection.execute(
        "UPDATE invitations SET revoked_at = ? WHERE id = ? AND submitted_at IS NULL AND revoked_at IS NULL",
        (datetime.now(timezone.utc).isoformat(), invitation_id),
    ).rowcount
    if changed != 1:
        raise IntakeError(409, "invitation is already submitted or revoked, or does not exist")
    _audit(connection, invitation_id, "revoked")
