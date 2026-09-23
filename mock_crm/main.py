"""Mock of the internal business system (CRM). In-memory, for demos and tests.

Run:  uvicorn mock_crm.main:app --port 8001
Outage simulation:
  MOCK_CRM_FAIL_RATE=0.3      -> ~30 % of requests return 503
  POST /admin/outage {"enabled": true}  -> every request (except /admin) returns 503
"""
from __future__ import annotations

import os
import random
import threading
import datetime as dt
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

load_dotenv()  # MOCK_CRM_FAIL_RATE may come from .env

SEED_CLIENTS = [
    {"id": "C001", "name": "Zelena Dolina d.o.o."},
    {"id": "C002", "name": "Termoplast Celje d.o.o."},
    {"id": "C003", "name": "Lipa Logistika d.o.o."},
]


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.clients: dict[str, dict] = {c["id"]: dict(c) for c in SEED_CLIENTS}
        self.meetings: dict[str, dict] = {}
        self.requirements: list[dict] = []
        self.tasks: list[dict] = []
        self.idempotency: dict[str, tuple[int, Any]] = {}
        self.outage = False
        self.fail_rate = float(os.getenv("MOCK_CRM_FAIL_RATE", "0") or 0)
        self.counter = 0

    def next_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter:04d}"


state = State()
app = FastAPI(title="Mock internal system")


# --- models ------------------------------------------------------------------

class ClientIn(BaseModel):
    name: str


class MeetingIn(BaseModel):
    client_id: str
    date: dt.date | None = None
    summary: str
    key_facts: list[dict] = []


class RequirementIn(BaseModel):
    description: str
    priority: str = "unknown"


class TaskIn(BaseModel):
    meeting_id: str
    description: str
    owner_employee_id: str | None = None
    due_date: dt.date | None = None


class OutageIn(BaseModel):
    enabled: bool


# --- outage simulation ---------------------------------------------------------

@app.middleware("http")
async def simulate_outage(request: Request, call_next):
    if not request.url.path.startswith("/admin"):
        if state.outage or (state.fail_rate > 0 and random.random() < state.fail_rate):
            return JSONResponse(status_code=503, content={"detail": "Service unavailable (simulated)"})
    return await call_next(request)


@app.post("/admin/outage")
def set_outage(body: OutageIn):
    state.outage = body.enabled
    return {"outage": state.outage}


@app.post("/admin/reset")
def reset():
    state.reset()
    return {"ok": True}


@app.get("/admin/dump")
def dump():
    return {"clients": list(state.clients.values()), "meetings": list(state.meetings.values()),
            "requirements": state.requirements, "tasks": state.tasks}


# --- idempotency helper --------------------------------------------------------

def idempotent(key: str | None, create) -> JSONResponse:
    """Run `create()` once per Idempotency-Key; repeats return the stored response."""
    if not key:
        raise HTTPException(status_code=400, detail="Missing Idempotency-Key header")
    with state.lock:
        if key in state.idempotency:
            status, body = state.idempotency[key]
            return JSONResponse(status_code=status, content=body)
        body = create()
        state.idempotency[key] = (201, body)
        return JSONResponse(status_code=201, content=body)


# --- endpoints -------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    return {"service": "mock internal system", "docs": "/docs", "health": "/health",
            "data": "/admin/dump", "review_ui": "streamlit run review_ui.py (http://localhost:8501)"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/clients")
def find_clients(name: str = ""):
    q = name.strip().lower()
    return [c for c in state.clients.values() if q and q in c["name"].lower()]


@app.post("/clients", status_code=201)
def create_client(body: ClientIn, idempotency_key: str | None = Header(default=None)):
    def create():
        client = {"id": state.next_id("C"), "name": body.name}
        state.clients[client["id"]] = client
        return client
    return idempotent(idempotency_key, create)


@app.post("/meetings", status_code=201)
def create_meeting(body: MeetingIn, idempotency_key: str | None = Header(default=None)):
    if body.client_id not in state.clients:
        raise HTTPException(status_code=422, detail=f"Unknown client_id {body.client_id}")

    def create():
        meeting = {"id": state.next_id("M"), **body.model_dump(mode="json")}
        state.meetings[meeting["id"]] = meeting
        return meeting
    return idempotent(idempotency_key, create)


@app.post("/meetings/{meeting_id}/requirements", status_code=201)
def add_requirement(meeting_id: str, body: RequirementIn,
                    idempotency_key: str | None = Header(default=None)):
    if meeting_id not in state.meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")

    def create():
        req = {"id": state.next_id("R"), "meeting_id": meeting_id, **body.model_dump(mode="json")}
        state.requirements.append(req)
        return req
    return idempotent(idempotency_key, create)


@app.post("/tasks", status_code=201)
def create_task(body: TaskIn, idempotency_key: str | None = Header(default=None)):
    if body.meeting_id not in state.meetings:
        raise HTTPException(status_code=404, detail="Meeting not found")

    def create():
        task = {"id": state.next_id("T"), **body.model_dump(mode="json")}
        state.tasks.append(task)
        return task
    return idempotent(idempotency_key, create)
