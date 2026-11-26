"""Submission status pages and the administrator moderation queue."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import auth, db, media, moderation
from ..relationships import get_person, RelationshipError
from ..templating import render, set_flash
from ..util import rel_time, validate_person_form

router = APIRouter()


@router.get("/me/submissions")
def my_submissions(request: Request):
    user = auth.require_user(request)
    conn = request.state.conn
    sets = conn.execute(
        "SELECT * FROM change_sets WHERE submitted_by = ? ORDER BY submitted_at DESC LIMIT 100",
        (user["id"],),
    ).fetchall()
    items = {}
    for change_set in sets:
        rows = conn.execute(
            "SELECT * FROM change_items WHERE set_id = ? ORDER BY sort_order", (change_set["id"],)
        ).fetchall()
        items[change_set["id"]] = rows
    return render(request, "submissions.html", sets=sets, items=items)


@router.post("/me/submissions/{set_id}/withdraw")
def withdraw(request: Request, set_id: str):
    user = auth.require_user(request)
    conn = request.state.conn
    try:
        with db.tx(conn):
            moderation.withdraw(conn, set_id, user, request.app.state.settings)
    except RelationshipError as exc:
        response = RedirectResponse("/me/submissions", status_code=303)
        set_flash(response, str(exc), "error")
        return response
    response = RedirectResponse("/me/submissions", status_code=303)
    set_flash(response, "Submission withdrawn.")
    return response


# ---------------------------------------------------------------------------
# Admin queue
# ---------------------------------------------------------------------------

@router.get("/admin/queue")
def queue(request: Request, status: str = "pending"):
    user = auth.require_admin(request)
    conn = request.state.conn
    if status not in ("pending", "needs_rebase", "approved", "rejected", "withdrawn", "all"):
        status = "pending"
    where = "" if status == "all" else "WHERE cs.status = ?"
    params = () if status == "all" else (status,)
    sets = conn.execute(
        "SELECT cs.*, u.display_name AS submitter_name, "
        "(SELECT COUNT(*) FROM change_items ci WHERE ci.set_id = cs.id) AS item_count "
        "FROM change_sets cs JOIN users u ON u.id = cs.submitted_by "
        f"{where} ORDER BY cs.submitted_at DESC LIMIT 200",
        params,
    ).fetchall()
    counts = {row["status"]: row["n"] for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM change_sets GROUP BY status")}
    return render(request, "admin/queue.html", sets=sets, filter=status, counts=counts,
                  is_admin=True)


@router.get("/admin/queue/{set_id}")
def review(request: Request, set_id: str):
    user = auth.require_admin(request)
    conn = request.state.conn
    change_set = moderation.get_set(conn, set_id)
    if change_set is None:
        raise HTTPException(status_code=404, detail="Change request not found")
    submitter = conn.execute("SELECT * FROM users WHERE id = ?",
                             (change_set["submitted_by"],)).fetchone()
    items = []
    for row in moderation.set_items(conn, set_id):
        payload = moderation.item_payload(row)
        try:
            diff = json.loads(row["diff_json"] or "[]")
        except (ValueError, TypeError):
            diff = []
        subject = None
        if row["entity_type"] == "person" and row["entity_id"]:
            subject = get_person(conn, row["entity_id"], include_deleted=True)
        items.append({"row": row, "payload": payload, "diff": diff, "subject": subject})
    person_items = [i for i in items
                    if i["row"]["entity_type"] == "person"
                    and i["row"]["op"] in ("person_create", "person_update")]
    return render(request, "admin/review.html", change_set=change_set, submitter=submitter,
                  items=items, can_revise=len(person_items) == 1, is_admin=True)


@router.post("/admin/queue/{set_id}/approve")
async def approve(request: Request, set_id: str):
    user = auth.require_admin(request)
    conn = request.state.conn
    try:
        moderation.approve(conn, set_id, user, request.app.state.settings)
    except RelationshipError as exc:
        response = RedirectResponse(f"/admin/queue/{set_id}", status_code=303)
        set_flash(response, str(exc), "error")
        return response
    response = RedirectResponse("/admin/queue", status_code=303)
    set_flash(response, "Change approved and applied.")
    return response


@router.post("/admin/queue/{set_id}/reject")
async def reject(request: Request, set_id: str):
    user = auth.require_admin(request)
    conn = request.state.conn
    form = await request.form()
    reason = str(form.get("reason", "")).strip()[:1000]
    try:
        moderation.reject(conn, set_id, user, reason, request.app.state.settings)
    except RelationshipError as exc:
        response = RedirectResponse(f"/admin/queue/{set_id}", status_code=303)
        set_flash(response, str(exc), "error")
        return response
    response = RedirectResponse("/admin/queue", status_code=303)
    set_flash(response, "Change rejected.")
    return response


# ---------------------------------------------------------------------------
# Edit a submission before approving it
# ---------------------------------------------------------------------------

def _person_item(conn, set_id):
    change_set = moderation.get_set(conn, set_id)
    if change_set is None or change_set["status"] != "pending":
        raise RelationshipError("This change request is no longer pending.")
    person_items = [i for i in moderation.set_items(conn, set_id)
                    if i["entity_type"] == "person"
                    and i["op"] in ("person_create", "person_update")]
    if len(person_items) != 1:
        raise RelationshipError("Only submissions with a single person record can be revised.")
    return change_set, person_items[0]


def _revise_values(conn, item):
    payload = moderation.item_payload(item)
    if item["op"] == "person_update":
        person = get_person(conn, item["entity_id"])
        if person is None:
            raise RelationshipError("The person in this submission no longer exists.")
        merged = dict(person)
        merged.update({k: v for k, v in payload.items() if not k.startswith("_")})
        from .person_routes import _values
        return person, _values(None, merged), payload
    from .person_routes import _values
    person = None
    values = _values(None)
    for key, value in payload.items():
        if key.startswith("_"):
            continue
        if key == "private_fields":
            try:
                values[key] = json.loads(value) if isinstance(value, str) else (value or [])
            except (ValueError, TypeError):
                values[key] = []
        elif key == "is_living":
            values["living"] = bool(value)
        elif key in values:
            values[key] = "" if value is None else str(value)
    return person, values, payload


@router.get("/admin/queue/{set_id}/revise")
def revise_page(request: Request, set_id: str):
    user = auth.require_admin(request)
    conn = request.state.conn
    try:
        change_set, item = _person_item(conn, set_id)
        person, values, payload = _revise_values(conn, item)
    except RelationshipError as exc:
        response = RedirectResponse(f"/admin/queue/{set_id}", status_code=303)
        set_flash(response, str(exc), "error")
        return response
    from .person_routes import _form_page
    mode = "edit" if item["op"] == "person_update" else "new"
    return _form_page(
        request, mode=mode, values=values,
        title="Revise: " + (change_set["title"] or "submission"),
        action=f"/admin/queue/{set_id}/revise", person=person, hidden=set(),
        can_privacy=True, can_direct=True, revise=True,
        cancel_url=f"/admin/queue/{set_id}",
    )


@router.post("/admin/queue/{set_id}/revise")
async def revise_submit(request: Request, set_id: str):
    user = auth.require_admin(request)
    conn = request.state.conn
    settings = request.app.state.settings
    try:
        change_set, item = _person_item(conn, set_id)
        person, values, original_payload = _revise_values(conn, item)
    except RelationshipError as exc:
        response = RedirectResponse(f"/admin/queue/{set_id}", status_code=303)
        set_flash(response, str(exc), "error")
        return response

    form = await request.form()
    partial = item["op"] == "person_update"
    data, errors = validate_person_form(form, partial=partial)
    pending_avatar = original_payload.get("_pending_avatar") if isinstance(original_payload.get("_pending_avatar"), str) else None

    upload = form.get("avatar")
    if upload and getattr(upload, "filename", ""):
        try:
            new_avatar = media.save_pending(upload, settings)
            if pending_avatar:
                media.discard_pending(settings, pending_avatar)
            pending_avatar = new_avatar
        except media.ImageError as exc:
            errors.append(str(exc))

    if errors:
        if pending_avatar and pending_avatar != original_payload.get("_pending_avatar"):
            media.discard_pending(settings, pending_avatar)
        from .person_routes import _form_page, _values
        mode = "edit" if partial else "new"
        return _form_page(
            request, mode=mode, values=_values(form, person) if person else _values(form),
            title="Revise: " + (change_set["title"] or "submission"),
            action=f"/admin/queue/{set_id}/revise", person=person, hidden=set(),
            can_privacy=True, can_direct=True, revise=True,
            cancel_url=f"/admin/queue/{set_id}",
            error=" ".join(errors), status_code=400,
        )

    payload = dict(data)
    if pending_avatar:
        payload["_pending_avatar"] = pending_avatar
    diff = moderation.build_diff(conn, item["op"], item["entity_id"], payload)
    conn.execute(
        "UPDATE change_items SET payload_json = ?, diff_json = ? WHERE id = ?",
        (json.dumps(payload, ensure_ascii=False, default=str),
         json.dumps(diff, ensure_ascii=False, default=str), item["id"]),
    )
    try:
        moderation.approve(conn, set_id, user, settings)
    except RelationshipError as exc:
        response = RedirectResponse(f"/admin/queue/{set_id}", status_code=303)
        set_flash(response, str(exc), "error")
        return response
    response = RedirectResponse("/admin/queue", status_code=303)
    set_flash(response, "Submission revised and approved.")
    return response
