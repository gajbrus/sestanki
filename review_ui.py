"""Human review & approval UI.   Run:  streamlit run review_ui.py"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import streamlit as st
from pydantic import ValidationError

from app import db
from app import config
from app.config import employees_by_id, load_employees
from app.pipeline import ApprovalBlocked, Pipeline
from app.schema import KeyFact, MeetingExtraction, NextStep, Participant, Requirement
from app.validate import flag_key, has_errors

st.set_page_config(page_title="Pregled sestankov", layout="wide")

BADGE = {
    db.RECEIVED: "⚪", db.EXTRACTED: "⚪", db.NEEDS_REVIEW: "🟡", db.APPROVED: "🔵",
    db.SYNCED: "🟢", db.EMAIL_DRAFTED: "✅", db.EXTRACTION_FAILED: "🔴", db.SYNC_FAILED: "🔴",
}
EMPLOYEES = load_employees()
EMP_IDS = [None] + [e["id"] for e in EMPLOYEES]
EMP_LABEL = {None: "— brez —", **{e["id"]: f"{e['full_name']} ({e['id']})" for e in EMPLOYEES}}
OWNER_ORIGIN = {
    "explicit": "🗣 Nalogo je prevzel/a na sestanku.",
    "suggested_by_domain": "💡 Naloga je bila dodeljena na podlagi tipa naloge.",
}


@st.cache_resource
def get_pipeline() -> Pipeline:
    return Pipeline()


p = get_pipeline()


def opt_index(options: list, value) -> int:
    return options.index(value) if value in options else 0


# Blank items the reviewer can add when the model missed something.
BLANK = {
    "participants": Participant(name="", side="unknown", employee_id=None),
    "client_requirements": Requirement(description="", priority="unknown", evidence=""),
    "key_facts": KeyFact(type="other", value="", amount=None, currency=None, certainty="stated",
                         evidence=""),
    "next_steps": NextStep(description="", owner_employee_id=None, owner_source="none",
                           due_date=None, evidence=""),
}


def with_added(prefix: str, section: str, existing: list, editable: bool) -> list:
    """Existing items followed by the blank items the reviewer added (only while editable)."""
    added = st.session_state.get(f"{prefix}add_{section}", 0) if editable else 0
    return list(existing) + [BLANK[section]] * added


def deleted(prefix: str, section: str) -> set[int]:
    """Indices of items the reviewer removed (kept in session state until approval)."""
    return st.session_state.setdefault(f"{prefix}del_{section}", set())


def delete_button(col, prefix: str, section: str, i: int, editable: bool) -> None:
    if editable and col.button("🗑", key=f"{prefix}delbtn_{section}{i}", help="Izbriši"):
        deleted(prefix, section).add(i)
        st.rerun()


def add_button(prefix: str, section: str, label: str, editable: bool) -> None:
    if not editable:
        return
    c1, c2 = st.columns([1, 1])
    if c1.button(f"➕ {label}", key=f"{prefix}addbtn_{section}"):
        key = f"{prefix}add_{section}"
        st.session_state[key] = st.session_state.get(key, 0) + 1
        st.rerun()
    removed = deleted(prefix, section)
    if removed and c2.button(f"↩ Obnovi izbrisane ({len(removed)})", key=f"{prefix}restore_{section}"):
        removed.clear()
        st.rerun()


def sync_and_report(meeting_id: int, prefix: str, create_client: bool | None = None) -> None:
    """Push approved data to the system (+ email draft) and show the outcome after rerun."""
    with st.spinner("Pošiljam v poslovni sistem ..."):
        status = p.sync(meeting_id, create_client=create_client)
    if status == db.EMAIL_DRAFTED:
        st.session_state["flash"] = ("success", prefix + "Podatki so v sistemu, osnutek e-pošte je pripravljen.")
    else:
        st.session_state["flash"] = ("error", prefix + "Sinhronizacija ni uspela – podatki so shranjeni, "
                                     "poskusi znova s 'Ponovi sinhronizacijo'.")
    st.rerun()


# --- sidebar: meeting list -------------------------------------------------------

with st.sidebar:
    # Simulated login: the reviewer is the logged-in employee (REVIEWER_EMPLOYEE_ID in .env).
    # In production this identity comes from SSO and cannot be changed in the UI.
    user = employees_by_id().get(config.REVIEWER_EMPLOYEE_ID)
    if user is None:
        st.error(f"Neznan REVIEWER_EMPLOYEE_ID '{config.REVIEWER_EMPLOYEE_ID}' v .env.")
        st.stop()
    reviewer = f"{user['full_name']} ({user['id']})"
    st.markdown(f"👤 **{user['full_name']}**")
    st.caption(f"{user['role']} · prijavljen pregledovalec")
    st.divider()
    st.header("Sestanki")
    meetings = db.list_meetings(p.conn)
    if not meetings:
        st.info("Ni sestankov. Zaženi `python -m app.pipeline ingest-all`.")
        st.stop()
    labels = {m["id"]: f"{BADGE.get(m['status'], '')} #{m['id']} {Path(m['transcript_path']).name} · {m['status']}"
              for m in meetings}
    # Stable key: the selection must survive label changes (status badges) after actions.
    selected = st.radio("Izberi sestanek", list(labels), format_func=labels.get, key="selected_meeting")
    st.divider()
    if st.button("🔁 Ponovi neuspele", use_container_width=True):
        with st.spinner("Ponavljam ..."):
            results = p.retry_failed()
        st.session_state["flash"] = ("info", "; ".join(f"#{i}: {s}" for i, s in results)
                                     or "Ni neuspelih elementov.")
        st.rerun()
    crm_ok = p.crm_factory().health()
    st.caption(f"Poslovni sistem: {'🟢 dosegljiv' if crm_ok else '🔴 nedosegljiv'}")

if flash := st.session_state.pop("flash", None):
    getattr(st, flash[0])(flash[1])

m = db.get(p.conn, selected)
transcript = Path(m["transcript_path"]).read_text(encoding="utf-8")
st.title(f"{BADGE.get(m['status'], '')} Sestanek #{m['id']} – {m['status']}")
if m["last_error"]:
    st.error(f"Zadnja napaka: {m['last_error']}")

left, right = st.columns([2, 3], gap="large")
with left:
    st.subheader("Transkript")
    st.text_area("transkript", transcript, height=900, disabled=True, label_visibility="collapsed")

source = m["approved_json"] or m["extraction_json"]
with right:
    if source is None:
        st.warning("Ni ekstrakcije (ekstrakcija ni uspela). Uporabi 'Ponovi neuspele'.")
        st.stop()
    data = MeetingExtraction.model_validate(source)
    editable = m["status"] == db.NEEDS_REVIEW
    k = f"m{m['id']}_"  # widget-key prefix per meeting
    slots: dict[str, object] = {}  # original item path -> container where its flags are shown
    kept: dict[str, list[int]] = {"participants": [], "client_requirements": [],
                                  "key_facts": [], "next_steps": []}

    if not editable:
        st.info(f"Odobril: **{m['reviewer']}**")

    # --- header fields ---
    st.subheader("Osnovni podatki")
    c1, c2 = st.columns(2)
    client_company = c1.text_input("Stranka", data.client_company or "", key=k + "client",
                                   disabled=not editable) or None
    meeting_date = c2.date_input("Datum sestanka", data.meeting_date, key=k + "date",
                                 disabled=not editable, format="DD.MM.YYYY")
    slots["client_company"] = slots["meeting_date"] = st.container()
    summary = st.text_area("Povzetek", data.summary, key=k + "summary", height=140,
                           disabled=not editable)

    # --- participants ---
    st.subheader("Udeleženci")
    participants = []
    for i, pt in enumerate(with_added(k, "participants", data.participants, editable)):
        if i in deleted(k, "participants"):
            continue
        c1, c2, c3, c4 = st.columns([3, 2, 3, 1])
        name = c1.text_input("Ime", pt.name, key=f"{k}p{i}name", disabled=not editable)
        side = c2.selectbox("Stran", ["client", "employee", "unknown"], key=f"{k}p{i}side",
                            index=["client", "employee", "unknown"].index(pt.side), disabled=not editable)
        emp = c3.selectbox("Zaposleni", EMP_IDS, index=opt_index(EMP_IDS, pt.employee_id),
                           format_func=EMP_LABEL.get, key=f"{k}p{i}emp", disabled=not editable)
        delete_button(c4, k, "participants", i, editable)
        slots[f"participants[{i}]"] = st.container()
        if name.strip():  # empty added rows are ignored
            kept["participants"].append(i)
            participants.append(Participant(name=name, side=side, employee_id=emp))
    add_button(k, "participants", "Dodaj udeleženca", editable)

    # --- requirements ---
    st.subheader("Zahteve stranke")
    requirements = []
    for i, r in enumerate(with_added(k, "client_requirements", data.client_requirements, editable)):
        if i in deleted(k, "client_requirements"):
            continue
        with st.container(border=True):
            c1, c2, c3 = st.columns([6, 2, 1])
            desc = c1.text_input("Opis", r.description, key=f"{k}r{i}desc", disabled=not editable)
            prio = c2.selectbox("Prioriteta", ["high", "medium", "low", "unknown"], key=f"{k}r{i}prio",
                                index=["high", "medium", "low", "unknown"].index(r.priority),
                                disabled=not editable)
            delete_button(c3, k, "client_requirements", i, editable)
            ev = st.text_input("📎 Dokaz (citat)", r.evidence, key=f"{k}r{i}ev", disabled=not editable)
            slots[f"client_requirements[{i}]"] = st.container()
            if desc.strip():
                kept["client_requirements"].append(i)
                requirements.append((i, Requirement(description=desc, priority=prio, evidence=ev)))
    add_button(k, "client_requirements", "Dodaj zahtevo", editable)

    # --- key facts ---
    st.subheader("Ključna dejstva")
    key_facts = []
    fact_types = ["budget", "deadline", "scope", "technology", "constraint", "other"]
    for i, f in enumerate(with_added(k, "key_facts", data.key_facts, editable)):
        if i in deleted(k, "key_facts"):
            continue
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([2, 5, 2, 1])
            ftype = c1.selectbox("Tip", fact_types, index=fact_types.index(f.type), key=f"{k}f{i}type",
                                 disabled=not editable)
            value = c2.text_input("Vrednost", f.value, key=f"{k}f{i}val", disabled=not editable)
            cert = c3.selectbox("Gotovost", ["stated", "approximate"], key=f"{k}f{i}cert",
                                index=["stated", "approximate"].index(f.certainty), disabled=not editable)
            delete_button(c4, k, "key_facts", i, editable)
            amount, currency = None, None
            if ftype == "budget":
                c1, c2 = st.columns(2)
                amount = c1.number_input("Znesek", value=f.amount, key=f"{k}f{i}amt", step=1000.0,
                                         disabled=not editable)
                currency = c2.text_input("Valuta", f.currency or "", key=f"{k}f{i}cur",
                                         disabled=not editable) or None
            ev = st.text_input("📎 Dokaz (citat)", f.evidence, key=f"{k}f{i}ev", disabled=not editable)
            slots[f"key_facts[{i}]"] = st.container()
            if value.strip():
                kept["key_facts"].append(i)
                key_facts.append((i, KeyFact(type=ftype, value=value, amount=amount, currency=currency,
                                             certainty=cert, evidence=ev)))
    add_button(k, "key_facts", "Dodaj ključno dejstvo", editable)

    # --- next steps ---
    st.subheader("Naslednji koraki")
    # Model's original next steps, matched by their verbatim evidence quote
    original_steps = {ns["evidence"]: ns for ns in (m["extraction_json"] or {}).get("next_steps", [])}
    next_steps = []
    for i, s in enumerate(with_added(k, "next_steps", data.next_steps, editable)):
        if i in deleted(k, "next_steps"):
            continue
        with st.container(border=True):
            c1, c2 = st.columns([9, 1])
            desc = c1.text_input("Opis", s.description, key=f"{k}s{i}desc", disabled=not editable)
            delete_button(c2, k, "next_steps", i, editable)
            c1, c2, c3 = st.columns([3, 2, 2])
            owner = c1.selectbox("Nosilec", EMP_IDS, index=opt_index(EMP_IDS, s.owner_employee_id),
                                 format_func=EMP_LABEL.get, key=f"{k}s{i}owner", disabled=not editable)
            # Changing the owner in the UI is an explicit reviewer decision.
            owner_source = s.owner_source
            if owner != s.owner_employee_id:
                owner_source = "explicit" if owner else "none"
            # Where the owner came from – shown only while the model's original owner is selected.
            orig = original_steps.get(s.evidence)
            if orig and orig["owner_employee_id"] and owner == orig["owner_employee_id"]:
                label = OWNER_ORIGIN.get(orig["owner_source"])
                if label:
                    c2.caption(label)
            due = c3.date_input("Rok", s.due_date, key=f"{k}s{i}due", disabled=not editable,
                                format="DD.MM.YYYY")
            ev = st.text_input("📎 Dokaz (citat)", s.evidence, key=f"{k}s{i}ev", disabled=not editable)
            slots[f"next_steps[{i}]"] = st.container()
            if desc.strip():
                kept["next_steps"].append(i)
                next_steps.append((i, NextStep(description=desc, owner_employee_id=owner,
                                               owner_source=owner_source, due_date=due, evidence=ev)))
    add_button(k, "next_steps", "Dodaj naslednji korak", editable)

    st.subheader("Odprta vprašanja in negotovosti")
    open_q = st.text_area("Odprta vprašanja (ena na vrstico)", "\n".join(data.open_questions),
                          key=k + "oq", disabled=not editable)
    uncert = st.text_area("Negotovosti modela (ena na vrstico)", "\n".join(data.uncertainties),
                          key=k + "unc", disabled=not editable)

    edited = MeetingExtraction(
        meeting_date=meeting_date if isinstance(meeting_date, date) else None,
        client_company=client_company, participants=participants, summary=summary,
        client_requirements=[r for _, r in requirements], key_facts=[f for _, f in key_facts],
        next_steps=[s for _, s in next_steps],
        open_questions=[q.strip() for q in open_q.splitlines() if q.strip()],
        uncertainties=[u.strip() for u in uncert.splitlines() if u.strip()],
    )

    # --- flags: live re-validation of the edited data, shown next to each item ---
    flags = p.revalidate(m["id"], edited)
    # Confirmed warnings, keyed by displayed item + message: survives deleting other items,
    # and editing the item (which changes the message) asks for confirmation again.
    confirmed: set[str] = st.session_state.setdefault(k + "confirmed", set())
    acknowledged: set[str] = set()  # the same warnings, keyed as the pipeline sees them
    pending: list[str] = []
    for f in flags:
        match = re.match(r"(\w+)\[(\d+)\]", f.field)
        if match:  # index in the edited list -> index of the original (displayed) item
            section, idx = match.group(1), int(match.group(2))
            path = f"{section}[{kept[section][idx]}]"
        else:
            path = f.field
        box = slots.get(path, st)
        if f.severity == "error":
            box.error(f"🟥 {f.message}")
            continue
        ui_key = f"{path}|{f.message}"
        if ui_key in confirmed or not editable:  # approved meetings had all warnings confirmed
            acknowledged.add(flag_key(f))
            box.success(f"✔ Potrjeno: {f.message}")
            continue
        pending.append(ui_key)
        c1, c2 = box.columns([5, 1], vertical_alignment="center")
        c1.warning(f"🟨 {f.message}")
        if c2.button("✔ Potrdi", key=f"{k}confirm|{ui_key}", help="Pregledal sem, je v redu"):
            confirmed.add(ui_key)
            st.rerun()
    n_err = sum(f.severity == "error" for f in flags)
    st.markdown(f"**Validacija:** {n_err} napak, {len(flags) - n_err} opozoril "
                f"({len(flags) - n_err - len(pending)} potrjenih)")

    # --- actions ---
    st.divider()
    if editable:
        errors_present = has_errors(flags)
        confirm_client = st.checkbox("Potrjujem ustvarjanje nove stranke, če je ni v sistemu",
                                     key=k + "newclient")
        if pending and st.button(f"✔ Potrdi vsa opozorila ({len(pending)})", key=k + "confirm_all"):
            confirmed.update(pending)
            st.rerun()
        if st.button("✅ Odobri in sinhroniziraj v sistem", type="primary",
                     disabled=errors_present or bool(pending)):
            try:
                p.approve(m["id"], edited, reviewer, confirm_new_client=confirm_client,
                          acknowledged_warnings=acknowledged)
            except (ApprovalBlocked, ValidationError) as e:
                st.error(str(e))
            else:
                sync_and_report(m["id"], "Odobreno. ")
        if errors_present:
            st.caption("Odobritev je blokirana, dokler obstajajo napake (popravi podatke ali izbriši postavko).")
        elif pending:
            st.caption("Pred odobritvijo potrdi vsa opozorila (✔ Potrdi) ali popravi podatke.")

    # Approved data can no longer change, so the only action left is retrying a failed sync.
    if m["status"] in (db.APPROVED, db.SYNC_FAILED, db.SYNCED):
        create_client = False
        if m["status"] == db.SYNC_FAILED and "ClientNotFound" in (m["last_error"] or ""):
            create_client = st.checkbox(f"Potrjujem ustvarjanje nove stranke '{data.client_company}'",
                                        key=k + "create")
        if st.button("🔁 Ponovi sinhronizacijo", type="primary"):
            sync_and_report(m["id"], "", create_client or None)

    if m["email_draft"]:
        with st.expander("✉️ Osnutek e-pošte (ni poslan)", expanded=True):
            st.code(m["email_draft"], language=None)
            st.caption(f"Shranjeno v outputs/{m['id']}.eml")

    with st.expander("Revizijska sled"):
        st.table([{k2: a[k2] for k2 in ("at", "from_status", "to_status", "note")}
                  for a in db.audit_log(p.conn, m["id"])])
