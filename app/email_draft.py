"""Follow-up email DRAFT built only from APPROVED data. Never sent.

Facts, names, amounts, dates and contacts are inserted deterministically; contact
details come from employees.json, never from LLM output. The LLM may optionally
polish only the two free-text sentences (opening and closing).
"""
from __future__ import annotations

import logging
from email.message import EmailMessage
from pathlib import Path

from app import config
from app.llm import LLMError, LLMProvider
from app.schema import MeetingExtraction

log = logging.getLogger(__name__)

POLISH_SYSTEM = (
    "Izboljšaj slog podanega besedila v vljudni poslovni slovenščini. Ohrani pomen, ne "
    "dodajaj novih dejstev, imen, zneskov ali datumov. Vrni samo izboljšano besedilo."
)


def _fmt_date(d) -> str:
    return f"{d.day}. {d.month}. {d.year}"


def _polish(text: str, llm: LLMProvider | None) -> str:
    if llm is None or not config.LLM_POLISH_EMAIL:
        return text
    try:
        polished = llm.complete(POLISH_SYSTEM, text).strip()
    except LLMError as e:
        log.warning("Email polish failed, using template text: %s", e)
        return text
    return polished or text


def build_email_body(approved: MeetingExtraction, employees: dict[str, dict],
                     llm: LLMProvider | None = None) -> str:
    date_part = f" dne {_fmt_date(approved.meeting_date)}" if approved.meeting_date else ""
    opening = _polish(f"najlepša hvala za vaš čas in prijeten sestanek{date_part}.", llm)
    closing = _polish("Če imate kakršnakoli vprašanja ali dopolnitve, nam prosim sporočite.", llm)

    lines = ["Spoštovani,", "", opening, "", "Kratek povzetek sestanka:", approved.summary.strip()]

    if approved.key_facts:
        lines += ["", "Ključne točke:"]
        lines += [f"- {k.value}" for k in approved.key_facts]

    owners: list[dict] = []
    if approved.next_steps:
        lines += ["", "Dogovorjeni naslednji koraki:"]
        for s in approved.next_steps:
            emp = employees.get(s.owner_employee_id or "")
            extra = []
            if emp:
                extra.append(f"odgovorna oseba: {emp['full_name']}")
                if emp not in owners:
                    owners.append(emp)
            if s.due_date:
                extra.append(f"rok: {_fmt_date(s.due_date)}")
            lines.append(f"- {s.description}" + (f" ({', '.join(extra)})" if extra else ""))

    if owners:
        lines += ["", "Kontaktne osebe:"]
        lines += [f"- {e['full_name']}, {e['role']} – {e['email']}, {e['phone']}" for e in owners]

    sender = _sender(approved, employees)
    lines += ["", closing, "", "Lep pozdrav,", ""]
    if sender:
        lines += [sender["full_name"], sender["role"]]
    lines.append(config.COMPANY_NAME)
    return "\n".join(lines) + "\n"


def _sender(approved: MeetingExtraction, employees: dict[str, dict]) -> dict | None:
    for p in approved.participants:
        if p.side == "employee" and p.employee_id in employees:
            return employees[p.employee_id]
    return None


def build_email(approved: MeetingExtraction, employees: dict[str, dict],
                llm: LLMProvider | None = None) -> EmailMessage:
    msg = EmailMessage()
    sender = _sender(approved, employees)
    if sender:
        msg["From"] = f"{sender['full_name']} <{sender['email']}>"
    msg["To"] = ""  # client contact is not part of the extraction; filled in by the sender
    date_part = f" {_fmt_date(approved.meeting_date)}" if approved.meeting_date else ""
    msg["Subject"] = f"Povzetek sestanka{date_part} – {approved.client_company or ''}".strip()
    msg["X-Unsent"] = "1"  # opens as a draft in Outlook
    msg.set_content(build_email_body(approved, employees, llm))
    return msg


def save_draft(meeting_id: int, msg: EmailMessage, outputs_dir: Path | None = None) -> Path:
    outputs_dir = Path(outputs_dir or config.OUTPUTS_DIR)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    path = outputs_dir / f"{meeting_id}.eml"
    path.write_bytes(bytes(msg))
    return path
