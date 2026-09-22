from app.config import employees_by_id
from app.email_draft import build_email, build_email_body
from tests.conftest import load_extraction


def test_email_contains_facts_and_contacts_from_employees_json():
    approved = load_extraction("normal")
    body = build_email_body(approved, employees_by_id())
    assert "45.000 EUR za prvo fazo" in body
    assert "luka.horvat@svetovanje-plus.si" in body and "+386 41 111 203" in body
    assert "rok: 25. 9. 2026" in body


def test_contacts_never_come_from_llm_output():
    approved = load_extraction("normal")
    approved.summary += " Kontakt: fake@evil.example"
    approved.next_steps[0].owner_employee_id = "E999"  # unknown -> no contact line
    body = build_email_body(approved, employees_by_id())
    contacts = body.split("Kontaktne osebe:")[1]
    assert "E999" not in body and "fake@evil.example" not in contacts
    assert "Luka Horvat" not in contacts


def test_email_is_a_draft():
    msg = build_email(load_extraction("normal"), employees_by_id())
    assert msg["X-Unsent"] == "1"
    assert "Zelena Dolina" in msg["Subject"]
