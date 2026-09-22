from app.validate import amount_in_evidence, has_errors, numbers_in_text, validate
from tests.conftest import load_extraction, load_transcript


def errors(flags):
    return [f for f in flags if f.severity == "error"]


def test_clean_extraction_has_no_errors(normal):
    extraction, transcript = normal
    assert errors(validate(extraction, transcript)) == []


def test_fabricated_evidence_is_caught(normal):
    extraction, transcript = normal
    extraction.client_requirements[0].evidence = "Stranka želi mobilno aplikacijo za voznike."
    flags = validate(extraction, transcript)
    assert any(f.field == "client_requirements[0].evidence" and f.severity == "error" for f in flags)


def test_evidence_tolerates_whitespace_and_case(normal):
    extraction, transcript = normal
    extraction.client_requirements[0].evidence = "  TOČNO tako,   to je za nas\nabsolutna prioriteta številka ena "
    assert errors(validate(extraction, transcript)) == []


def test_wrong_amount_is_caught(normal):
    extraction, transcript = normal
    extraction.key_facts[0].amount = 54000  # evidence says 45.000
    flags = validate(extraction, transcript)
    assert any(f.field == "key_facts[0].amount" and f.severity == "error" for f in flags)


def test_unknown_employee_id_is_caught(normal):
    extraction, transcript = normal
    extraction.next_steps[0].owner_employee_id = "E999"
    extraction.participants[0].employee_id = "E998"
    fields = {f.field for f in errors(validate(extraction, transcript))}
    assert "next_steps[0].owner_employee_id" in fields
    assert "participants[0].employee_id" in fields


def test_approximate_budget_produces_warning_not_error():
    flags = validate(load_extraction("ambiguous"), load_transcript("ambiguous"))
    assert any(f.field == "key_facts[0].certainty" and f.severity == "warning" for f in flags)
    # "dvajset tisoč" (words) must match amount 20000
    assert not any(f.field == "key_facts[0].amount" for f in flags)


def test_suggested_owner_produces_warning():
    flags = validate(load_extraction("ambiguous"), load_transcript("ambiguous"))
    assert any(f.field == "next_steps[2].owner_employee_id" and f.severity == "warning" for f in flags)
    assert not has_errors(flags)


def test_hallucinated_budget_for_no_price_is_caught():
    flags = validate(load_extraction("no_price_hallucinated"), load_transcript("no_price"))
    assert any(f.field == "key_facts[0].evidence" and f.severity == "error" for f in flags)


def test_due_date_before_meeting_is_error(normal):
    from datetime import date
    extraction, transcript = normal
    extraction.next_steps[0].due_date = date(2026, 9, 1)
    assert any(f.field == "next_steps[0].due_date" for f in errors(validate(extraction, transcript)))


def test_missing_client_company_is_error(normal):
    extraction, transcript = normal
    extraction.client_company = None
    assert any(f.field == "client_company" for f in errors(validate(extraction, transcript)))


def test_non_employee_marked_as_employee_warns(normal):
    extraction, transcript = normal
    extraction.participants[3].side = "employee"  # Irena Golob is a client
    flags = validate(extraction, transcript)
    assert any(f.field == "participants[3].name" and f.severity == "warning" for f in flags)


def test_number_parsing():
    assert 45000 in numbers_in_text("Proračun je 45.000 EUR")
    assert 45000 in numbers_in_text("45 000 €")
    assert 20000 in numbers_in_text("nekje okoli dvajset tisoč")
    assert 25000 in numbers_in_text("petindvajset tisoč evrov")
    assert amount_in_evidence(1250.5, "znesek 1.250,50 EUR")
    assert not amount_in_evidence(30000, "Proračun je 45.000 EUR")
