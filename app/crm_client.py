"""Typed client for the internal business system (CRM) API.

- Retries (tenacity, exponential backoff, max 4 attempts) only on timeouts,
  connection errors and 5xx. 4xx is never retried.
- Every POST carries an Idempotency-Key "{meeting_id}:{entity}:{index}", so a
  retried or repeated sync never creates duplicates.
- Only APPROVED data is ever sent (the caller passes the approved extraction).
"""
from __future__ import annotations

import logging
import re

import httpx
from tenacity import (Retrying, retry_if_exception, stop_after_attempt,
                      wait_exponential)

from app import config
from app.schema import MeetingExtraction

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 4


class CRMError(Exception):
    """Sync failed; message is stored as last_error."""


class CRMUnavailable(CRMError):
    """Transient failure (timeout, connection error, 5xx) after all retries."""


class CRMRejected(CRMError):
    """Permanent 4xx failure – not retried."""


class ClientNotFound(CRMError):
    """Client is not in the CRM; reviewer must confirm creating it."""


class ClientAmbiguous(CRMError):
    """Several CRM clients match the name; reviewer must resolve."""


class _RetryableStatus(Exception):
    def __init__(self, response: httpx.Response):
        super().__init__(f"HTTP {response.status_code}: {response.text[:200]}")
        self.response = response


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError, _RetryableStatus))


def idempotency_key(meeting_id: int | str, entity: str, index: int = 0) -> str:
    return f"{meeting_id}:{entity}:{index}"


def _norm_company(name: str) -> str:
    return re.sub(r"[^a-z0-9čšž]+", " ", name.lower()).strip()


class CRMClient:
    def __init__(self, base_url: str | None = None, *, http: httpx.Client | None = None,
                 transport: httpx.BaseTransport | None = None, timeout: float | None = None,
                 backoff_multiplier: float = 0.5, backoff_max: float = 8.0):
        # `http` lets tests inject FastAPI's TestClient; `transport` an httpx.MockTransport.
        self.http = http or httpx.Client(base_url=base_url or config.CRM_BASE_URL, transport=transport,
                                         timeout=timeout or config.CRM_TIMEOUT_SECONDS)
        self.wait = wait_exponential(multiplier=backoff_multiplier, max=backoff_max)

    def close(self):
        self.http.close()

    # --- low level -----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        retrying = Retrying(stop=stop_after_attempt(MAX_ATTEMPTS), wait=self.wait,
                            retry=retry_if_exception(_is_retryable), reraise=True)

        def attempt() -> httpx.Response:
            resp = self.http.request(method, path, **kwargs)
            if resp.status_code >= 500:
                log.warning("CRM %s %s -> %s, will retry", method, path, resp.status_code)
                raise _RetryableStatus(resp)
            return resp

        try:
            resp = retrying(attempt)
        except _RetryableStatus as e:
            raise CRMUnavailable(f"{method} {path}: {e} (after {MAX_ATTEMPTS} attempts)") from e
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise CRMUnavailable(f"{method} {path}: {type(e).__name__}: {e} "
                                 f"(after {MAX_ATTEMPTS} attempts)") from e

        if 400 <= resp.status_code < 500:
            log.error("CRM %s %s rejected: %s %s", method, path, resp.status_code, resp.text[:200])
            raise CRMRejected(f"{method} {path}: HTTP {resp.status_code}: {resp.text[:200]}")
        return resp

    def _post(self, path: str, body: dict, key: str) -> dict:
        return self._request("POST", path, json=body, headers={"Idempotency-Key": key}).json()

    # --- endpoints ---------------------------------------------------------------

    def health(self) -> bool:
        try:
            return self.http.get("/health").status_code == 200
        except httpx.HTTPError:
            return False

    def find_client(self, name: str) -> dict:
        """Resolve a client by name. Raises ClientNotFound / ClientAmbiguous."""
        results = self._request("GET", "/clients", params={"name": name}).json()
        exact = [c for c in results if _norm_company(c["name"]) == _norm_company(name)]
        if len(exact) == 1:
            return exact[0]
        if len(results) == 1:
            return results[0]
        if not results:
            raise ClientNotFound(f"Stranka '{name}' ne obstaja v sistemu. "
                                 "Ustvarjanje nove stranke zahteva potrditev pregledovalca.")
        raise ClientAmbiguous(f"Več strank se ujema z '{name}': "
                              + ", ".join(c["name"] for c in results))

    def create_client(self, name: str, key: str) -> dict:
        return self._post("/clients", {"name": name}, key)

    # --- high level ----------------------------------------------------------------

    def sync_meeting(self, meeting_id: int, approved: MeetingExtraction,
                     create_client_if_missing: bool = False) -> dict:
        """Push an approved meeting. Safe to call repeatedly (idempotent)."""
        if not approved.client_company:
            raise CRMRejected("Odobreni podatki nimajo imena stranke.")
        try:
            client = self.find_client(approved.client_company)
        except ClientNotFound:
            if not create_client_if_missing:
                raise
            client = self.create_client(approved.client_company,
                                        idempotency_key(meeting_id, "client"))

        meeting = self._post("/meetings", {
            "client_id": client["id"],
            "date": approved.meeting_date.isoformat() if approved.meeting_date else None,
            "summary": approved.summary,
            "key_facts": [k.model_dump(mode="json", exclude={"evidence"}) for k in approved.key_facts],
        }, idempotency_key(meeting_id, "meeting"))

        requirements = [
            self._post(f"/meetings/{meeting['id']}/requirements",
                       {"description": r.description, "priority": r.priority},
                       idempotency_key(meeting_id, "requirement", i))
            for i, r in enumerate(approved.client_requirements)
        ]
        tasks = [
            self._post("/tasks", {
                "meeting_id": meeting["id"],
                "description": s.description,
                "owner_employee_id": s.owner_employee_id,
                "due_date": s.due_date.isoformat() if s.due_date else None,
            }, idempotency_key(meeting_id, "task", i))
            for i, s in enumerate(approved.next_steps)
        ]
        return {"client_id": client["id"], "crm_meeting_id": meeting["id"],
                "requirement_ids": [r["id"] for r in requirements],
                "task_ids": [t["id"] for t in tasks]}
