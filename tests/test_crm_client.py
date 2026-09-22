import httpx
import pytest
from fastapi.testclient import TestClient

from app.crm_client import (MAX_ATTEMPTS, ClientNotFound, CRMClient, CRMRejected,
                            CRMUnavailable)
from mock_crm.main import app, state
from tests.conftest import load_extraction


def mock_client(handler) -> CRMClient:
    return CRMClient("http://crm.test", transport=httpx.MockTransport(handler),
                     backoff_multiplier=0, backoff_max=0)


@pytest.fixture
def crm():
    state.reset()
    with TestClient(app) as http:
        yield CRMClient(http=http, backoff_multiplier=0, backoff_max=0)
    state.reset()


def test_retries_on_503_then_succeeds():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503, json={"detail": "down"})
        return httpx.Response(201, json={"id": "C9", "name": "X"})

    assert mock_client(handler).create_client("X", "1:client:0") == {"id": "C9", "name": "X"}
    assert len(calls) == 3
    assert all(c.headers["Idempotency-Key"] == "1:client:0" for c in calls)


def test_gives_up_after_max_attempts():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    with pytest.raises(CRMUnavailable):
        mock_client(handler).create_client("X", "1:client:0")
    assert len(calls) == MAX_ATTEMPTS


def test_retries_on_connection_error():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(201, json={"id": "C9", "name": "X"})

    mock_client(handler).create_client("X", "1:client:0")
    assert len(calls) == 2


def test_no_retry_on_400():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"detail": "bad"})

    with pytest.raises(CRMRejected):
        mock_client(handler).create_client("X", "1:client:0")
    assert len(calls) == 1


def test_idempotency_prevents_duplicates(crm):
    approved = load_extraction("normal")
    first = crm.sync_meeting(7, approved)
    second = crm.sync_meeting(7, approved)  # e.g. a retry after a lost response
    assert first == second
    assert len(state.meetings) == 1
    assert len(state.requirements) == len(approved.client_requirements)
    assert len(state.tasks) == len(approved.next_steps)
    assert first["client_id"] == "C001"


def test_mock_requires_idempotency_key(crm):
    resp = crm.http.post("/clients", json={"name": "Brez ključa"})
    assert resp.status_code == 400


def test_unknown_client_is_not_created_silently(crm):
    approved = load_extraction("normal")
    approved.client_company = "Nova Firma d.o.o."
    with pytest.raises(ClientNotFound):
        crm.sync_meeting(8, approved)
    assert len(state.clients) == 3 and not state.meetings

    result = crm.sync_meeting(8, approved, create_client_if_missing=True)
    assert state.clients[result["client_id"]]["name"] == "Nova Firma d.o.o."


def test_full_outage_then_recovery(crm):
    crm.http.post("/admin/outage", json={"enabled": True})
    with pytest.raises(CRMUnavailable):
        crm.sync_meeting(9, load_extraction("normal"))
    crm.http.post("/admin/outage", json={"enabled": False})
    crm.sync_meeting(9, load_extraction("normal"))
    assert len(state.meetings) == 1
