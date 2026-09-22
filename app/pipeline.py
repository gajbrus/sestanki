"""Orchestration + CLI.

    python -m app.pipeline ingest <path>
    python -m app.pipeline ingest-all
    python -m app.pipeline sync <id> [--create-client]
    python -m app.pipeline retry-failed
    python -m app.pipeline status
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Callable

from app import config, db
from app.crm_client import ClientAmbiguous, ClientNotFound, CRMClient, CRMError
from app.email_draft import build_email, save_draft
from app.extract import ExtractionResult, extract
from app.llm import LLMError, LLMProvider, get_llm
from app.schema import MeetingExtraction
from app.validate import Flag, has_errors, validate

log = logging.getLogger("pipeline")


class ApprovalBlocked(Exception):
    """Approval refused: validation errors present and no override reason given."""


class Pipeline:
    def __init__(self, conn: sqlite3.Connection | None = None,
                 llm_factory: Callable[[], LLMProvider] = get_llm,
                 crm_factory: Callable[[], CRMClient] = CRMClient,
                 outputs_dir: Path | None = None):
        self.conn = conn or db.connect()
        self.llm_factory = llm_factory
        self.crm_factory = crm_factory
        self.outputs_dir = outputs_dir or config.OUTPUTS_DIR
        self._llm: LLMProvider | None = None

    @property
    def llm(self) -> LLMProvider:
        if self._llm is None:
            self._llm = self.llm_factory()  # may raise LLMError (e.g. missing API key)
        return self._llm

    # --- ingest + extraction -------------------------------------------------------

    def ingest(self, path: str | Path) -> tuple[int, bool]:
        """Register a transcript and run extraction. Returns (meeting_id, created).
        The same content (hash) is never registered twice."""
        path = Path(path)
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        existing = db.get_by_hash(self.conn, content_hash)
        if existing:
            log.info("Duplicate of meeting %s (%s), skipped", existing["id"], existing["status"])
            return existing["id"], False
        meeting_id = db.create(self.conn, str(path), content_hash)
        self.run_extraction(meeting_id)
        return meeting_id, True

    def ingest_all(self, directory: Path | None = None) -> list[tuple[int, bool]]:
        directory = Path(directory or config.TRANSCRIPTS_DIR)
        return [self.ingest(p) for p in sorted(directory.glob("*.txt"))]

    def run_extraction(self, meeting_id: int) -> None:
        m = db.get(self.conn, meeting_id)
        transcript = Path(m["transcript_path"]).read_text(encoding="utf-8")
        try:
            result = extract(transcript, self.llm)
        except LLMError as e:  # provider could not even be constructed
            result = ExtractionResult(None, 0, f"LLM napaka: {e}")
        attempts = m["attempt_count"] + result.attempts
        if not result.ok:
            db.transition(self.conn, meeting_id, db.EXTRACTION_FAILED, result.error,
                          last_error=result.error, attempt_count=attempts)
            return
        flags = validate(result.extraction, transcript)
        db.transition(self.conn, meeting_id, db.EXTRACTED, f"LLM: {self.llm.name}",
                      extraction_json=result.extraction.model_dump(mode="json"),
                      flags_json=[f.model_dump() for f in flags],
                      last_error=None, attempt_count=attempts)
        # Nothing is auto-approved: every extraction goes to human review.
        n_err = sum(f.severity == "error" for f in flags)
        db.transition(self.conn, meeting_id, db.NEEDS_REVIEW,
                      f"{n_err} napak, {len(flags) - n_err} opozoril")

    # --- review ----------------------------------------------------------------

    def revalidate(self, meeting_id: int, data: MeetingExtraction) -> list[Flag]:
        transcript = Path(db.get(self.conn, meeting_id)["transcript_path"]).read_text(encoding="utf-8")
        return validate(data, transcript)

    def approve(self, meeting_id: int, approved: MeetingExtraction, reviewer: str,
                override_reason: str | None = None, confirm_new_client: bool = False) -> list[Flag]:
        """Store the reviewer-edited version. Blocked while errors exist unless overridden."""
        if not reviewer.strip():
            raise ApprovalBlocked("Manjka ime pregledovalca.")
        flags = self.revalidate(meeting_id, approved)
        override_reason = (override_reason or "").strip() or None
        if has_errors(flags) and not override_reason:
            raise ApprovalBlocked("Podatki vsebujejo napake. Popravite jih ali označite "
                                  "'override' in navedite razlog.")
        note = f"odobril {reviewer}" + (f"; override: {override_reason}" if override_reason else "")
        db.transition(self.conn, meeting_id, db.APPROVED, note,
                      approved_json=approved.model_dump(mode="json"),
                      flags_json=[f.model_dump() for f in flags],
                      reviewer=reviewer, override_reason=override_reason,
                      confirm_new_client=int(confirm_new_client))
        return flags

    # --- sync + email ----------------------------------------------------------------

    def sync(self, meeting_id: int, create_client: bool | None = None) -> str:
        """Push APPROVED data to the CRM, then draft the email. Returns final status."""
        m = db.get(self.conn, meeting_id)
        if m is None:
            raise KeyError(f"Sestanek {meeting_id} ne obstaja")
        if m["status"] in (db.APPROVED, db.SYNC_FAILED):
            self._push(m, create_client)
        m = db.get(self.conn, meeting_id)
        if m["status"] == db.SYNCED:
            self.draft_email(meeting_id)
        elif m["status"] not in (db.SYNC_FAILED, db.EMAIL_DRAFTED):
            raise db.InvalidTransition(f"Sestanek {meeting_id} je v stanju '{m['status']}'; "
                                       "sinhronizirati je mogoče le odobrene sestanke.")
        return db.get(self.conn, meeting_id)["status"]

    def _push(self, m: dict, create_client: bool | None) -> None:
        # Only the reviewer-approved version is ever sent, never raw LLM output.
        approved = MeetingExtraction.model_validate(m["approved_json"])
        if create_client is None:
            create_client = bool(m["confirm_new_client"])
        attempts = m["attempt_count"] + 1
        crm = self.crm_factory()
        try:
            result = crm.sync_meeting(m["id"], approved, create_client_if_missing=create_client)
        except CRMError as e:
            kind = "client_not_found" if isinstance(e, (ClientNotFound, ClientAmbiguous)) else "sync"
            error = f"{type(e).__name__}: {e}"
            db.transition(self.conn, m["id"], db.SYNC_FAILED, error,
                          last_error=error, attempt_count=attempts)
            db.outbox_add(self.conn, m["id"], kind, error)
            log.error("Sync of meeting %s failed: %s", m["id"], error)
            return
        finally:
            crm.close()
        db.transition(self.conn, m["id"], db.SYNCED, f"CRM sestanek {result['crm_meeting_id']}",
                      crm_result_json=result, last_error=None, attempt_count=attempts)
        db.outbox_resolve(self.conn, m["id"])

    def draft_email(self, meeting_id: int) -> Path:
        m = db.get(self.conn, meeting_id)
        approved = MeetingExtraction.model_validate(m["approved_json"])
        llm = None
        if config.LLM_POLISH_EMAIL:
            try:
                llm = self.llm
            except LLMError as e:
                log.warning("Email polish disabled: %s", e)
        msg = build_email(approved, config.employees_by_id(), llm)
        path = save_draft(meeting_id, msg, self.outputs_dir)
        db.transition(self.conn, meeting_id, db.EMAIL_DRAFTED, str(path),
                      email_draft=f"Od: {msg['From'] or ''}\nZadeva: {msg['Subject']}\n\n"
                                  f"{msg.get_content()}")
        return path

    # --- retries -----------------------------------------------------------------------

    def retry_failed(self) -> list[tuple[int, str]]:
        results = []
        # 'received' = extraction was interrupted (e.g. process crash)
        for m in db.list_meetings(self.conn, [db.RECEIVED, db.EXTRACTION_FAILED]):
            self.run_extraction(m["id"])
            results.append((m["id"], db.get(self.conn, m["id"])["status"]))
        for item in db.outbox_pending(self.conn):
            results.append((item["meeting_id"], self.sync(item["meeting_id"])))
        # A meeting left in 'synced' (email step interrupted) just needs its draft.
        for m in db.list_meetings(self.conn, [db.SYNCED]):
            results.append((m["id"], self.sync(m["id"])))
        return results


# --- CLI ------------------------------------------------------------------------

def _print_status(p: Pipeline) -> None:
    rows = db.list_meetings(p.conn)
    if not rows:
        print("Ni sestankov.")
        return
    print(f"{'ID':>3}  {'STATUS':<18} {'NAPAKE':>6} {'OPOZ.':>5} {'POSK.':>5}  {'DATOTEKA':<22} ZADNJA NAPAKA")
    for m in rows:
        flags = m["flags_json"] or []
        n_err = sum(f["severity"] == "error" for f in flags)
        print(f"{m['id']:>3}  {m['status']:<18} {n_err:>6} {len(flags) - n_err:>5} "
              f"{m['attempt_count']:>5}  {Path(m['transcript_path']).name:<22} "
              f"{(m['last_error'] or '')[:70]}")


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Slovenian characters on Windows consoles
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m app.pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest").add_argument("path")
    sub.add_parser("ingest-all")
    sync_p = sub.add_parser("sync")
    sync_p.add_argument("id", type=int)
    sync_p.add_argument("--create-client", action="store_true",
                        help="potrdi ustvarjanje nove stranke v sistemu")
    sub.add_parser("retry-failed")
    sub.add_parser("status")
    args = parser.parse_args(argv)

    p = Pipeline()
    if args.cmd == "ingest":
        mid, created = p.ingest(args.path)
        print(f"Sestanek {mid}: {'registriran' if created else 'že obstaja (duplikat preskočen)'}")
    elif args.cmd == "ingest-all":
        for mid, created in p.ingest_all():
            print(f"Sestanek {mid}: {'registriran' if created else 'duplikat preskočen'}")
    elif args.cmd == "sync":
        print(f"Sestanek {args.id}: {p.sync(args.id, create_client=args.create_client or None)}")
    elif args.cmd == "retry-failed":
        results = p.retry_failed()
        if not results:
            print("Ni neuspelih elementov.")
        for mid, status in results:
            print(f"Sestanek {mid}: {status}")
    if args.cmd != "status":
        print()
    _print_status(p)


if __name__ == "__main__":
    main()
