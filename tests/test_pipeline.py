import shutil

import pytest
from fastapi.testclient import TestClient

from app import db
from app.crm_client import CRMClient
from app.llm import FakeLLM, LLMError
from app.pipeline import ApprovalBlocked, Pipeline
from app.schema import MeetingExtraction
from mock_crm.main import app as crm_app, state as crm_state
from tests.conftest import FIXTURES, TRANSCRIPTS, load_fixture


@pytest.fixture
def crm_http():
    crm_state.reset()
    with TestClient(crm_app) as http:
        yield http
    crm_state.reset()


def make_pipeline(tmp_path, llm, crm_http=None):
    def crm_factory():
        c = CRMClient(http=crm_http, backoff_multiplier=0, backoff_max=0)
        c.close = lambda: None  # TestClient is shared across calls
        return c
    return Pipeline(conn=db.connect(tmp_path / "test.db"), llm_factory=lambda: llm,
                    crm_factory=crm_factory, outputs_dir=tmp_path / "out")


def approve_as_is(p, mid):
    m = db.get(p.conn, mid)
    return p.approve(mid, MeetingExtraction.model_validate(m["extraction_json"]), "Tester")


def test_ingest_extracts_and_needs_review(tmp_path):
    p = make_pipeline(tmp_path, FakeLLM.from_fixtures(FIXTURES))
    mid, created = p.ingest(TRANSCRIPTS / "normal.txt")
    m = db.get(p.conn, mid)
    assert created and m["status"] == db.NEEDS_REVIEW
    assert m["extraction_json"]["client_company"] == "Zelena Dolina d.o.o."
    statuses = [a["to_status"] for a in db.audit_log(p.conn, mid)]
    assert statuses == [db.RECEIVED, db.EXTRACTED, db.NEEDS_REVIEW]


def test_duplicate_ingest_is_ignored(tmp_path):
    llm = FakeLLM.from_fixtures(FIXTURES)
    p = make_pipeline(tmp_path, llm)
    first, _ = p.ingest(TRANSCRIPTS / "normal.txt")
    copy = tmp_path / "renamed_copy.txt"
    shutil.copy(TRANSCRIPTS / "normal.txt", copy)  # same content, different path
    second, created = p.ingest(copy)
    assert second == first and not created
    assert len(db.list_meetings(p.conn)) == 1
    assert len(llm.calls) == 1


def test_hallucinated_budget_blocks_approval(tmp_path):
    p = make_pipeline(tmp_path, FakeLLM([load_fixture("no_price_hallucinated")]))
    mid, _ = p.ingest(TRANSCRIPTS / "no_price.txt")
    m = db.get(p.conn, mid)
    assert any(f["severity"] == "error" and f["field"] == "key_facts[0].evidence"
               for f in m["flags_json"])
    with pytest.raises(ApprovalBlocked):
        approve_as_is(p, mid)
    # Reviewer deletes the invented budget -> approval passes
    data = MeetingExtraction.model_validate(m["extraction_json"])
    data.key_facts = []
    p.approve(mid, data, "Tester")
    assert db.get(p.conn, mid)["status"] == db.APPROVED


def test_schema_failure_retries_once_with_errors(tmp_path):
    bad = {"summary": "manjka vse ostalo"}
    llm = FakeLLM([bad, load_fixture("normal")])
    p = make_pipeline(tmp_path, llm)
    mid, _ = p.ingest(TRANSCRIPTS / "normal.txt")
    assert db.get(p.conn, mid)["status"] == db.NEEDS_REVIEW
    assert len(llm.calls) == 2
    assert "validacije" in llm.calls[1][-1]["content"]  # errors were sent back


def test_schema_failure_twice_marks_extraction_failed_then_retry(tmp_path):
    llm = FakeLLM([{"x": 1}, {"x": 2}, load_fixture("normal")])
    p = make_pipeline(tmp_path, llm)
    mid, _ = p.ingest(TRANSCRIPTS / "normal.txt")
    m = db.get(p.conn, mid)
    assert m["status"] == db.EXTRACTION_FAILED and m["last_error"]
    p.retry_failed()
    assert db.get(p.conn, mid)["status"] == db.NEEDS_REVIEW


def test_llm_api_error_marks_extraction_failed(tmp_path):
    p = make_pipeline(tmp_path, FakeLLM([LLMError("503 overloaded")]))
    mid, _ = p.ingest(TRANSCRIPTS / "normal.txt")
    assert db.get(p.conn, mid)["status"] == db.EXTRACTION_FAILED


def test_sync_failure_then_retry_works(tmp_path, crm_http):
    p = make_pipeline(tmp_path, FakeLLM.from_fixtures(FIXTURES), crm_http)
    mid, _ = p.ingest(TRANSCRIPTS / "normal.txt")
    approve_as_is(p, mid)
    approved_before = db.get(p.conn, mid)["approved_json"]

    crm_http.post("/admin/outage", json={"enabled": True})
    assert p.sync(mid) == db.SYNC_FAILED
    m = db.get(p.conn, mid)
    assert "CRMUnavailable" in m["last_error"]
    assert m["approved_json"] == approved_before  # approved data kept intact
    assert len(db.outbox_pending(p.conn)) == 1

    crm_http.post("/admin/outage", json={"enabled": False})
    p.retry_failed()
    m = db.get(p.conn, mid)
    assert m["status"] == db.EMAIL_DRAFTED
    assert db.outbox_pending(p.conn) == []
    assert len(crm_state.meetings) == 1
    assert (tmp_path / "out" / f"{mid}.eml").exists()


def test_sync_sends_approved_not_raw(tmp_path, crm_http):
    p = make_pipeline(tmp_path, FakeLLM.from_fixtures(FIXTURES), crm_http)
    mid, _ = p.ingest(TRANSCRIPTS / "normal.txt")
    data = MeetingExtraction.model_validate(db.get(p.conn, mid)["extraction_json"])
    data.summary = "Povzetek, ki ga je popravil pregledovalec."
    data.next_steps = data.next_steps[:1]
    p.approve(mid, data, "Tester")
    p.sync(mid)
    (crm_meeting,) = crm_state.meetings.values()
    assert crm_meeting["summary"] == "Povzetek, ki ga je popravil pregledovalec."
    assert len(crm_state.tasks) == 1


def test_unknown_client_needs_confirmation(tmp_path, crm_http):
    p = make_pipeline(tmp_path, FakeLLM.from_fixtures(FIXTURES), crm_http)
    mid, _ = p.ingest(TRANSCRIPTS / "normal.txt")
    data = MeetingExtraction.model_validate(db.get(p.conn, mid)["extraction_json"])
    data.client_company = "Nova Stranka d.o.o."
    p.approve(mid, data, "Tester")
    assert p.sync(mid) == db.SYNC_FAILED
    assert "ClientNotFound" in db.get(p.conn, mid)["last_error"]
    assert p.sync(mid, create_client=True) == db.EMAIL_DRAFTED


def test_cannot_sync_unapproved(tmp_path, crm_http):
    p = make_pipeline(tmp_path, FakeLLM.from_fixtures(FIXTURES), crm_http)
    mid, _ = p.ingest(TRANSCRIPTS / "normal.txt")
    with pytest.raises(db.InvalidTransition):
        p.sync(mid)
    assert not crm_state.meetings


def test_missing_llm_credentials_marks_extraction_failed(tmp_path):
    def no_key():
        raise LLMError("credentials missing")
    p = Pipeline(conn=db.connect(tmp_path / "t.db"), llm_factory=no_key)
    mid, _ = p.ingest(TRANSCRIPTS / "normal.txt")
    m = db.get(p.conn, mid)
    assert m["status"] == db.EXTRACTION_FAILED and "credentials" in m["last_error"]


def test_warnings_must_be_confirmed_before_approval(tmp_path):
    from app.validate import flag_key
    p = make_pipeline(tmp_path, FakeLLM.from_fixtures(FIXTURES))
    mid, _ = p.ingest(TRANSCRIPTS / "ambiguous.txt")  # approximate budget + suggested owner
    data = MeetingExtraction.model_validate(db.get(p.conn, mid)["extraction_json"])
    warnings = [f for f in p.revalidate(mid, data) if f.severity == "warning"]
    assert len(warnings) == 2

    with pytest.raises(ApprovalBlocked, match="Nepotrjena opozorila"):
        p.approve(mid, data, "Tester")
    with pytest.raises(ApprovalBlocked):  # confirming only one is not enough
        p.approve(mid, data, "Tester", acknowledged_warnings={flag_key(warnings[0])})

    p.approve(mid, data, "Tester", acknowledged_warnings={flag_key(f) for f in warnings})
    m = db.get(p.conn, mid)
    assert m["status"] == db.APPROVED
    assert all(f["confirmed"] for f in m["flags_json"] if f["severity"] == "warning")
    assert "potrjena opozorila: 2" in db.audit_log(p.conn, mid)[-1]["note"]
