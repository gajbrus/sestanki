"""Deterministic post-extraction checks (no LLM).

Every check produces a Flag. Errors block approval until the reviewer fixes the
data or deletes the item; warnings must be confirmed by the reviewer.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel
from rapidfuzz import fuzz

from app.config import load_employees
from app.schema import MeetingExtraction

EVIDENCE_MIN_SCORE = 90


class Flag(BaseModel):
    severity: Literal["error", "warning"]
    field: str
    message: str


def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[\"'„“”‘’«»]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def evidence_found(quote: str, transcript: str) -> bool:
    q, t = normalize(quote), normalize(transcript)
    if not q:
        return False
    if q in t:
        return True
    return fuzz.partial_ratio(q, t) >= EVIDENCE_MIN_SCORE


# --- amounts -----------------------------------------------------------------

_UNITS = {
    "nič": 0, "ena": 1, "en": 1, "eno": 1, "dva": 2, "dve": 2, "tri": 3, "štiri": 4,
    "pet": 5, "šest": 6, "sedem": 7, "osem": 8, "devet": 9,
}
_TEENS = {
    "deset": 10, "enajst": 11, "dvanajst": 12, "trinajst": 13, "štirinajst": 14,
    "petnajst": 15, "šestnajst": 16, "sedemnajst": 17, "osemnajst": 18, "devetnajst": 19,
}
_TENS = {
    "dvajset": 20, "trideset": 30, "štirideset": 40, "petdeset": 50,
    "šestdeset": 60, "sedemdeset": 70, "osemdeset": 80, "devetdeset": 90,
}
_HUNDREDS = {
    "sto": 100, "dvesto": 200, "tristo": 300, "štiristo": 400, "petsto": 500,
    "šeststo": 600, "sedemsto": 700, "osemsto": 800, "devetsto": 900,
}
_SCALES = {"tisoč": 1_000, "milijon": 1_000_000, "milijona": 1_000_000, "milijonov": 1_000_000}


def _word_value(word: str) -> int | None:
    for table in (_UNITS, _TEENS, _TENS, _HUNDREDS):
        if word in table:
            return table[word]
    if "in" in word:  # e.g. "petindvajset" = pet + in + dvajset
        unit, _, tens = word.partition("in")
        if unit in _UNITS and tens in _TENS:
            return _UNITS[unit] + _TENS[tens]
    return None


def numbers_in_text(text: str) -> set[float]:
    """Numbers written with digits ("45.000", "45 000", "45000,50") and simple
    Slovenian number words ("dvajset tisoč")."""
    found: set[float] = set()
    for raw in re.findall(r"\d[\d.,\s]*\d|\d", text):
        raw = raw.strip()
        # Slovenian format: '.' or space = thousands separator, ',' = decimal separator
        cleaned = re.sub(r"[.\s]", "", raw).replace(",", ".")
        try:
            found.add(float(cleaned))
        except ValueError:
            pass
        found.add(float(re.sub(r"\D", "", raw)))

    total, current = 0, None
    for word in re.findall(r"[a-zčšž]+", text.lower()):
        value = _word_value(word)
        if value is not None:
            current = (current or 0) + value
        elif word in _SCALES:
            total += (current or 1) * _SCALES[word]
            current = None
            found.add(float(total))
        else:
            if current is not None:
                found.add(float(total + current))
            total, current = 0, None
    if current is not None:
        found.add(float(total + current))
    return found


def amount_in_evidence(amount: float, evidence: str) -> bool:
    return any(abs(n - amount) < 0.005 for n in numbers_in_text(evidence))


# --- employees ---------------------------------------------------------------

def _employee_names(emp: dict) -> set[str]:
    return {normalize(emp["full_name"]), *(normalize(a) for a in emp.get("aliases", []))}


def match_employee(name: str, employees: list[dict]) -> list[dict]:
    n = normalize(name)
    return [e for e in employees if n in _employee_names(e)]


# --- main entry point --------------------------------------------------------

def validate(extraction: MeetingExtraction, transcript: str,
             employees: list[dict] | None = None) -> list[Flag]:
    employees = employees if employees is not None else load_employees()
    known_ids = {e["id"] for e in employees}
    flags: list[Flag] = []

    def err(field: str, msg: str):
        flags.append(Flag(severity="error", field=field, message=msg))

    def warn(field: str, msg: str):
        flags.append(Flag(severity="warning", field=field, message=msg))

    if not extraction.client_company:
        err("client_company", "Manjka ime stranke (client_company).")

    # participants
    for i, p in enumerate(extraction.participants):
        f = f"participants[{i}]"
        if p.employee_id and p.employee_id not in known_ids:
            err(f + ".employee_id", f"Neznan employee_id '{p.employee_id}'.")
        if p.side == "employee":
            matches = match_employee(p.name, employees)
            if not matches:
                warn(f + ".name", f"'{p.name}' je označen kot zaposleni, a se ne ujema z nobenim imenom/vzdevkom.")
            elif p.employee_id and p.employee_id not in {m["id"] for m in matches}:
                warn(f + ".employee_id", f"'{p.name}' se ne ujema z zaposlenim {p.employee_id}.")

    # evidence quotes
    evidence_items = (
        [(f"client_requirements[{i}]", r.evidence) for i, r in enumerate(extraction.client_requirements)]
        + [(f"key_facts[{i}]", k.evidence) for i, k in enumerate(extraction.key_facts)]
        + [(f"next_steps[{i}]", s.evidence) for i, s in enumerate(extraction.next_steps)]
    )
    for field, quote in evidence_items:
        if not evidence_found(quote, transcript):
            err(field + ".evidence", f"Dokaz (citat) ni najden v transkriptu: \"{quote[:120]}\"")

    # budget facts
    for i, k in enumerate(extraction.key_facts):
        if k.type != "budget":
            continue
        f = f"key_facts[{i}]"
        if k.amount is not None and not amount_in_evidence(k.amount, k.evidence):
            err(f + ".amount", f"Znesek {k.amount:g} se ne pojavi v dokazu \"{k.evidence[:120]}\".")
        if k.certainty == "approximate":
            warn(f + ".certainty", "Proračun je le okviren – potrebna je človeška potrditev.")

    # next steps
    for i, s in enumerate(extraction.next_steps):
        f = f"next_steps[{i}]"
        if s.owner_employee_id and s.owner_employee_id not in known_ids:
            err(f + ".owner_employee_id", f"Neznan owner_employee_id '{s.owner_employee_id}'.")
        if s.owner_source != "explicit":
            warn(f + ".owner_employee_id", "Nosilec je le predlog (ni se izrecno zavezal).")
        if s.due_date and extraction.meeting_date and s.due_date < extraction.meeting_date:
            err(f + ".due_date", f"Rok {s.due_date} je pred datumom sestanka {extraction.meeting_date}.")

    return flags


def flag_key(flag: Flag) -> str:
    """Identity of a flag, used to record which warnings the reviewer confirmed."""
    return f"{flag.field}|{flag.message}"


def has_errors(flags: list[Flag]) -> bool:
    return any(f.severity == "error" for f in flags)
