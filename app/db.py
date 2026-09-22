"""SQLite persistence: meetings + status, audit log of transitions, outbox of failed syncs."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app import config

# Status machine
RECEIVED = "received"
EXTRACTED = "extracted"
NEEDS_REVIEW = "needs_review"
APPROVED = "approved"
SYNCED = "synced"
EMAIL_DRAFTED = "email_drafted"
EXTRACTION_FAILED = "extraction_failed"
SYNC_FAILED = "sync_failed"

TRANSITIONS: dict[str, set[str]] = {
    RECEIVED: {EXTRACTED, EXTRACTION_FAILED},
    EXTRACTION_FAILED: {EXTRACTED, EXTRACTION_FAILED},
    EXTRACTED: {NEEDS_REVIEW},
    NEEDS_REVIEW: {APPROVED},
    APPROVED: {SYNCED, SYNC_FAILED},
    SYNC_FAILED: {SYNCED, SYNC_FAILED},
    SYNCED: {EMAIL_DRAFTED},
    EMAIL_DRAFTED: set(),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    transcript_path  TEXT NOT NULL,
    content_hash     TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL,
    extraction_json  TEXT,
    flags_json       TEXT,
    approved_json    TEXT,
    reviewer         TEXT,
    override_reason  TEXT,
    confirm_new_client INTEGER NOT NULL DEFAULT 0,
    crm_result_json  TEXT,
    email_draft      TEXT,
    last_error       TEXT,
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id  INTEGER NOT NULL REFERENCES meetings(id),
    from_status TEXT,
    to_status   TEXT NOT NULL,
    note        TEXT,
    at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id  INTEGER NOT NULL UNIQUE REFERENCES meetings(id),
    operation   TEXT NOT NULL,
    last_error  TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    resolved_at TEXT
);
"""

JSON_FIELDS = {"extraction_json", "flags_json", "approved_json", "crm_result_json"}


class InvalidTransition(Exception):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or config.DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _dump(field: str, value):
    if field in JSON_FIELDS and value is not None and not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for f in JSON_FIELDS:
        if d.get(f):
            d[f] = json.loads(d[f])
    return d


def get_by_hash(conn, content_hash: str) -> dict | None:
    return row_to_dict(conn.execute("SELECT * FROM meetings WHERE content_hash=?",
                                    (content_hash,)).fetchone())


def get(conn, meeting_id: int) -> dict | None:
    return row_to_dict(conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone())


def list_meetings(conn, statuses: list[str] | None = None) -> list[dict]:
    sql, args = "SELECT * FROM meetings", ()
    if statuses:
        sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
        args = tuple(statuses)
    return [row_to_dict(r) for r in conn.execute(sql + " ORDER BY id", args)]


def create(conn, transcript_path: str, content_hash: str) -> int:
    ts = now()
    with conn:
        cur = conn.execute(
            "INSERT INTO meetings (transcript_path, content_hash, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?)", (transcript_path, content_hash, RECEIVED, ts, ts))
        conn.execute("INSERT INTO audit_log (meeting_id, from_status, to_status, note, at) "
                     "VALUES (?,?,?,?,?)", (cur.lastrowid, None, RECEIVED, transcript_path, ts))
    return cur.lastrowid


def update(conn, meeting_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn:
        conn.execute(f"UPDATE meetings SET {cols} WHERE id=?",
                     (*(_dump(k, v) for k, v in fields.items()), meeting_id))


def transition(conn, meeting_id: int, to_status: str, note: str | None = None, **fields) -> None:
    """Change status (validated against TRANSITIONS), update fields, write audit row – atomically."""
    current = conn.execute("SELECT status FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    if current is None:
        raise KeyError(f"Meeting {meeting_id} not found")
    from_status = current["status"]
    if to_status not in TRANSITIONS[from_status]:
        raise InvalidTransition(f"{from_status} -> {to_status} ni dovoljen prehod")
    fields["status"] = to_status
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn:
        conn.execute(f"UPDATE meetings SET {cols} WHERE id=?",
                     (*(_dump(k, v) for k, v in fields.items()), meeting_id))
        conn.execute("INSERT INTO audit_log (meeting_id, from_status, to_status, note, at) "
                     "VALUES (?,?,?,?,?)", (meeting_id, from_status, to_status, note, fields["updated_at"]))


def audit_log(conn, meeting_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM audit_log WHERE meeting_id=? ORDER BY id", (meeting_id,))]


# --- outbox --------------------------------------------------------------------

def outbox_add(conn, meeting_id: int, operation: str, error: str) -> None:
    ts = now()
    with conn:
        conn.execute(
            "INSERT INTO outbox (meeting_id, operation, last_error, attempts, created_at, updated_at) "
            "VALUES (?,?,?,1,?,?) ON CONFLICT(meeting_id) DO UPDATE SET "
            "last_error=excluded.last_error, attempts=attempts+1, updated_at=excluded.updated_at, "
            "resolved_at=NULL", (meeting_id, operation, error, ts, ts))


def outbox_resolve(conn, meeting_id: int) -> None:
    with conn:
        conn.execute("UPDATE outbox SET resolved_at=?, updated_at=? WHERE meeting_id=? "
                     "AND resolved_at IS NULL", (now(), now(), meeting_id))


def outbox_pending(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM outbox WHERE resolved_at IS NULL ORDER BY id")]
