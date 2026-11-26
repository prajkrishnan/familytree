"""Relative-centric person creation and editing, deletion and history."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import auth, db, media, moderation, policy, util
from ..relationships import (add_child, birth_union_of, create_person, create_union, delete_person,
                             get_person, get_union, partners_of, set_partner, update_person,
                             unions_for_person)
from ..templating import render, set_flash
from ..util import new_id, now

router = APIRouter()

RELATIONS = {"father", "mother", "spouse", "son", "daughter", "brother", "sister"}
RELATION_GENDER = {"father": "male", "mother": "female", "son": "male", "daughter": "female"}
RELATION_NOUN = {
    "father": "father", "mother": "mother", "spouse": "spouse", "son": "son",
    "daughter": "daughter", "brother": "brother", "sister": "sister",
}
ALLOWED_FIELDS = set(util.TEXT_FIELDS) | {
    "gender", "is_living", "birth_day", "birth_month", "birth_year", "birth_qualifier",
    "death_day", "death_month", "death_year", "death_qualifier", "contact_visibility",
    "private_fields",
}


def _values(form, person=None) -> dict:
    keys = list(util.TEXT_FIELDS) + [
        "gender", "birth_day", "birth_month", "birth_year", "birth_qualifier",
        "death_day", "death_month", "death_year", "death_qualifier", "contact_visibility",
    ]
    values = {}
    for key in keys:
        raw = person[key] if person is not None else ""
        values[key] = "" if raw is None else str(raw)
    values["living"] = bool(person["is_living"]) if person is not None else True
    try:
        values["private_fields"] = json.loads(person["private_fields"]) if person is not None else []
    except (ValueError, TypeError):
        values["private_fields"] = []
    if form is not None and len(form.keys()) > 0:
        for key in keys:
            if key in form:
                values[key] = str(form.get(key) or "")
        if "_living_present" in form:
            values["living"] = bool(form.get("living"))
        if "_privacy_present" in form:
            values["private_fields"] = [g for g in form.getlist("private_fields")
                                        if g in util.PRIVACY_GROUPS]
    return values


def _form_page(request, *, mode, values, title, action, person=None, anchor=None, relation="",
               error="", partners=None, unions=None, hidden=None, can_privacy=True,
               can_direct=False, existing=None, status_code=200, revise=False,
               cancel_url=""):
    return render(request, "person_form.html", mode=mode, values=values, title=title,
                  action=action, person=person, anchor=anchor, relation=relation, error=error,
                  partners=partners or [], unions=unions or [], hidden=hidden or set(),
                  can_manage_privacy=can_privacy, can_direct=can_direct, existing=existing,
                  relations=RELATIONS, months=util.MONTHS, groups=util.PRIVACY_GROUPS,
                  status_code=status_code, revise=revise, cancel_url=cancel_url)


def _person_title(anchor, relation) -> str:
    noun = RELATION_NOUN.get(relation, "relative")
    if relation in ("father", "mother", "spouse", "brother", "sister"):
        return f"Add {noun} of {util.display_name(anchor)}"
    return f"Add {noun} to {util.display_name(anchor)}"


def _relation_partners(conn, anchor) -> list[dict]:
    out = []
    for union in unions_for_person(conn, anchor["id"]):
        other_id = union["partner2_id"] if union["partner1_id"] == anchor["id"] else union["partner1_id"]
        if not other_id:
            continue
        other = get_person(conn, other_id)
        if other:
            out.append({"union_id": union["id"], "person": other})
    return out


def _union_between(conn, a: str, b: str):
    return conn.execute(
        "SELECT * FROM unions WHERE deleted_at IS NULL AND "
        "((partner1_id = ? AND partner2_id = ?) OR (partner1_id = ? AND partner2_id = ?))",
        (a, b, b, a),
    ).fetchone()


def _strip_forbidden(data: dict, allowed_groups: set[str]) -> dict:
    for field in list(data.keys()):
        group = util.FIELD_GROUP.get(field)
        if group and group not in allowed_groups:
            data.pop(field)
    return data


def _upload_avatar(form, settings, person_id: str, direct: bool) -> tuple[str | None, str | None]:
    upload = form.get("avatar")
    if not upload or not getattr(upload, "filename", ""):
        return None, None
    try:
        if direct:
            return "direct", media.save_direct(upload, settings, person_id)
        return "pending", media.save_pending(upload, settings)
    except media.ImageError as exc:
        return "error", str(exc)


# ---------------------------------------------------------------------------
# Add relative / person
# ---------------------------------------------------------------------------

@router.get("/persons/new")
def new_person_page(request: Request, anchor: str = "", relation: str = ""):
    user = auth.require_user(request)
    conn = request.state.conn
    anchor_person = get_person(conn, anchor) if anchor else None
    if anchor_person is None:
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Only administrators can add people directly.")
        if not conn.execute("SELECT 1 FROM persons WHERE deleted_at IS NULL LIMIT 1").fetchone() \
                and relation == "":
            pass
        values = _values(None)
        return _form_page(request, mode="new", values=values, title="Add a person",
                          action="/persons/new", can_direct=True)
    if relation not in RELATIONS:
        raise HTTPException(status_code=400, detail="Choose a valid relationship.")
    if not policy.can_submit_for(conn, user, anchor_person["id"]):
        raise HTTPException(status_code=403, detail="That person is outside your family scope.")

    birth = birth_union_of(conn, anchor_person["id"])
    if relation in ("brother", "sister") and birth is None:
        response = RedirectResponse("/tree?focus=" + anchor_person["id"], status_code=303)
        set_flash(response, "Add a parent first: a sibling needs at least one recorded parent.", "error")
        return response
    if relation in ("father", "mother") and birth is not None and birth["partner1_id"] and birth["partner2_id"]:
        response = RedirectResponse("/tree?focus=" + anchor_person["id"], status_code=303)
        set_flash(response, "Both parents are already recorded for this person.", "error")
        return response

    values = _values(None)
    if relation in RELATION_GENDER:
        values["gender"] = RELATION_GENDER[relation]
    return _form_page(
        request, mode="new", values=values, title=_person_title(anchor_person, relation),
        action="/persons/new", anchor=anchor_person, relation=relation,
        partners=_relation_partners(conn, anchor_person),
        can_direct=user["role"] == "admin",
    )


@router.post("/persons/new")
async def new_person_submit(request: Request):
    user = auth.require_user(request)
    conn = request.state.conn
    settings = request.app.state.settings
    form = await request.form()
    anchor_id = str(form.get("anchor", ""))
    relation = str(form.get("relation", ""))
    anchor_person = get_person(conn, anchor_id) if anchor_id else None
    direct = user["role"] == "admin" and str(form.get("action", "")) == "direct"

    if anchor_person is None:
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Only administrators can add people directly.")
        if relation:
            raise HTTPException(status_code=400, detail="Invalid relationship context.")
    else:
        if relation not in RELATIONS:
            raise HTTPException(status_code=400, detail="Choose a valid relationship.")
        if not policy.can_submit_for(conn, user, anchor_person["id"]):
            raise HTTPException(status_code=403, detail="That person is outside your family scope.")

    existing_id = str(form.get("existing_person_id", "")).strip()
    existing = get_person(conn, existing_id) if existing_id else None
    if existing_id and existing is None:
        return _form_page(request, mode="new", values=_values(form), title="Add relative",
                          action="/persons/new", anchor=anchor_person, relation=relation,
                          error="That existing person could not be found.", status_code=400,
                          can_direct=user["role"] == "admin")
    if existing and anchor_person and existing["id"] == anchor_person["id"]:
        return _form_page(request, mode="new", values=_values(form), title="Add relative",
                          action="/persons/new", anchor=anchor_person, relation=relation,
                          error="A person cannot be their own relative.", status_code=400)

    data: dict = {}
    errors: list[str] = []
    if existing is None:
        data, errors = util.validate_person_form(form, partial=False)
        if relation == "father":
            data.setdefault("gender", "male")
        elif relation == "mother":
            data.setdefault("gender", "female")
    if errors:
        values = _values(form)
        values["gender"] = data.get("gender", values["gender"])
        return _form_page(request, mode="new", values=values,
                          title=_person_title(anchor_person, relation) if anchor_person else "Add a person",
                          action="/persons/new", anchor=anchor_person, relation=relation,
                          error=" ".join(errors), status_code=400,
                          partners=_relation_partners(conn, anchor_person) if anchor_person else [],
                          can_direct=user["role"] == "admin")

    items: list[dict] = []
    target_id = existing["id"] if existing else new_id()
    pending_avatar = None
    if existing is None:
        data["_pending_avatar"] = None  # placeholder, replaced below
        upload = form.get("avatar")
        if upload and getattr(upload, "filename", ""):
            try:
                data["_pending_avatar"] = media.save_pending(upload, settings)
            except media.ImageError as exc:
                return _form_page(request, mode="new", values=_values(form),
                                  title=_person_title(anchor_person, relation) if anchor_person else "Add a person",
                                  action="/persons/new", anchor=anchor_person, relation=relation,
                                  error=str(exc), status_code=400,
                                  partners=_relation_partners(conn, anchor_person) if anchor_person else [],
                                  can_direct=user["role"] == "admin")
        items.append({"op": "person_create", "entity_type": "person", "entity_id": target_id,
                      "payload": data})

    title = _person_title(anchor_person, relation) if anchor_person else f"Add {data.get('given_name', '')} {data.get('surname', '')}".strip()

    try:
        if anchor_person is not None:
            items.extend(_relationship_items(conn, anchor_person, relation, target_id, existing,
                                             form, user))
        if not items:
            raise ValueError("Nothing to change.")
    except ValueError as exc:
        if data.get("_pending_avatar"):
            media.discard_pending(settings, data["_pending_avatar"])
        return _form_page(request, mode="new", values=_values(form),
                          title=title, action="/persons/new", anchor=anchor_person,
                          relation=relation, error=str(exc), status_code=400,
                          partners=_relation_partners(conn, anchor_person) if anchor_person else [],
                          can_direct=user["role"] == "admin")

    try:
        with db.tx(conn):
            if direct:
                moderation.apply_items(conn, items, settings, user, strict_version=False)
            else:
                moderation.submit(conn, user, title, items)
    except Exception as exc:
        if data.get("_pending_avatar"):
            media.discard_pending(settings, data["_pending_avatar"])
        return _form_page(request, mode="new", values=_values(form), title=title,
                          action="/persons/new", anchor=anchor_person, relation=relation,
                          error=str(exc), status_code=400,
                          partners=_relation_partners(conn, anchor_person) if anchor_person else [],
                          can_direct=user["role"] == "admin")

    response = RedirectResponse(
        f"/tree?focus={target_id}&open={target_id}" if direct else "/me/submissions",
        status_code=303,
    )
    set_flash(response, "Saved." if direct else "Submitted for administrator approval.")
    return response


def _relationship_items(conn, anchor, relation: str, target_id: str, existing,
                        form, user) -> list[dict]:
    items: list[dict] = []
    if relation in ("father", "mother"):
        birth = birth_union_of(conn, anchor["id"])
        if birth is None:
            union_id = new_id()
            items.append({"op": "union_create", "entity_type": "union", "entity_id": union_id,
                          "payload": {"partner1_id": target_id, "partner2_id": None,
                                      "kind": "unknown", "status": "unknown"}})
            items.append({"op": "union_child_add", "entity_type": "union_child", "entity_id": None,
                          "payload": {"union_id": union_id, "child_id": anchor["id"],
                                      "relationship_type": "birth"}})
        else:
            if target_id in partners_of(birth):
                raise ValueError("That person is already recorded as a parent.")
            if birth["partner1_id"] and birth["partner2_id"]:
                raise ValueError("Both parents are already recorded.")
            if birth["partner2_id"]:
                raise ValueError("This family already has two partners.")
            items.append({"op": "union_update", "entity_type": "union", "entity_id": birth["id"],
                          "payload": {"partner2_id": target_id}})

    elif relation == "spouse":
        if _union_between(conn, anchor["id"], target_id):
            raise ValueError("These two people are already linked as partners.")
        union_id = new_id()
        kind = str(form.get("union_kind", "marriage"))
        status = str(form.get("union_status", "ongoing"))
        items.append({"op": "union_create", "entity_type": "union", "entity_id": union_id,
                      "payload": {"partner1_id": anchor["id"], "partner2_id": target_id,
                                  "kind": kind, "status": status}})

    elif relation in ("son", "daughter"):
        chosen = str(form.get("union_id", ""))
        union = get_union(conn, chosen) if chosen else None
        if union is None or anchor["id"] not in partners_of(union):
            anchor_unions = unions_for_person(conn, anchor["id"])
            union = anchor_unions[0] if anchor_unions else None
        if union is None:
            union_id = new_id()
            items.append({"op": "union_create", "entity_type": "union", "entity_id": union_id,
                          "payload": {"partner1_id": anchor["id"], "partner2_id": None,
                                      "kind": "unknown", "status": "unknown"}})
        else:
            union_id = union["id"]
        items.append({"op": "union_child_add", "entity_type": "union_child", "entity_id": None,
                      "payload": {"union_id": union_id, "child_id": target_id,
                                  "relationship_type": "birth"}})

    elif relation in ("brother", "sister"):
        birth = birth_union_of(conn, anchor["id"])
        if birth is None:
            raise ValueError("Add a parent before adding a sibling.")
        items.append({"op": "union_child_add", "entity_type": "union_child", "entity_id": None,
                      "payload": {"union_id": birth["id"], "child_id": target_id,
                                  "relationship_type": "birth"}})
    return items


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------

@router.get("/persons/{person_id}")
def person_redirect(request: Request, person_id: str):
    auth.require_user(request)
    if get_person(request.state.conn, person_id) is None:
        raise HTTPException(status_code=404)
    return RedirectResponse(f"/tree?focus={person_id}&open={person_id}", status_code=303)


@router.get("/persons/{person_id}/edit")
def edit_person_page(request: Request, person_id: str):
    user = auth.require_user(request)
    conn = request.state.conn
    person = get_person(conn, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    if not policy.can_submit_for(conn, user, person_id):
        raise HTTPException(status_code=403, detail="That person is outside your family scope.")
    hidden = policy.hidden_groups(conn, person, user)
    return _form_page(
        request, mode="edit", values=_values(None, person),
        title=f"Edit {util.display_name(person)}", action=f"/persons/{person_id}/edit",
        person=person, hidden=hidden,
        can_privacy=policy.can_manage_privacy(user, person_id),
        can_direct=user["role"] == "admin",
    )


@router.post("/persons/{person_id}/edit")
async def edit_person_submit(request: Request, person_id: str):
    user = auth.require_user(request)
    conn = request.state.conn
    settings = request.app.state.settings
    person = get_person(conn, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    if not policy.can_submit_for(conn, user, person_id):
        raise HTTPException(status_code=403, detail="That person is outside your family scope.")

    form = await request.form()
    data, errors = util.validate_person_form(form, partial=True)
    allowed = policy.editable_groups(conn, person, user)
    data = _strip_forbidden(data, allowed)
    can_privacy = policy.can_manage_privacy(user, person_id)
    if not can_privacy:
        data.pop("contact_visibility", None)
        data.pop("private_fields", None)
    merged_name = (data.get("given_name", person["given_name"]) or "").strip()
    merged_surname = (data.get("surname", person["surname"]) or "").strip()
    if not merged_name and not merged_surname:
        errors.append("Please keep at least a given name or a family name.")

    version_raw = str(form.get("_version", ""))
    base_version = int(version_raw) if version_raw.isdigit() else person["version"]
    if base_version != person["version"]:
        errors.append("This record changed while you were editing. Reload the page and try again.")

    direct = user["role"] == "admin" and str(form.get("action", "")) == "direct"
    pending_avatar = None
    upload = form.get("avatar")
    if upload and getattr(upload, "filename", ""):
        if "photo" not in allowed:
            errors.append("You cannot change this person's photo.")
        else:
            try:
                pending_avatar = media.save_pending(upload, settings)
            except media.ImageError as exc:
                errors.append(str(exc))

    if errors:
        if pending_avatar:
            media.discard_pending(settings, pending_avatar)
        return _form_page(request, mode="edit", values=_values(form, person),
                          title=f"Edit {util.display_name(person)}",
                          action=f"/persons/{person_id}/edit", person=person,
                          hidden=policy.hidden_groups(conn, person, user),
                          can_privacy=can_privacy, can_direct=user["role"] == "admin",
                          error=" ".join(errors), status_code=400)

    payload = {k: v for k, v in data.items() if k in ALLOWED_FIELDS}
    if pending_avatar:
        payload["_pending_avatar"] = pending_avatar
    if not payload:
        response = RedirectResponse(f"/tree?focus={person_id}&open={person_id}", status_code=303)
        set_flash(response, "No changes were submitted.")
        return response

    items = [{"op": "person_update", "entity_type": "person", "entity_id": person_id,
              "base_version": person["version"], "payload": payload}]
    title = f"Edit {util.display_name(person)}"
    try:
        with db.tx(conn):
            if direct:
                moderation.apply_items(conn, items, settings, user, strict_version=False)
            else:
                moderation.submit(conn, user, title, items)
    except Exception as exc:
        if pending_avatar:
            media.discard_pending(settings, pending_avatar)
        return _form_page(request, mode="edit", values=_values(form, person),
                          title=title, action=f"/persons/{person_id}/edit", person=person,
                          hidden=policy.hidden_groups(conn, person, user),
                          can_privacy=can_privacy, can_direct=user["role"] == "admin",
                          error=str(exc), status_code=400)

    response = RedirectResponse(
        f"/tree?focus={person_id}&open={person_id}" if direct else "/me/submissions",
        status_code=303,
    )
    set_flash(response, "Saved." if direct else "Submitted for administrator approval.")
    return response


@router.post("/persons/{person_id}/delete")
def delete_person_route(request: Request, person_id: str):
    user = auth.require_admin(request)
    conn = request.state.conn
    person = get_person(conn, person_id)
    if person is None:
        raise HTTPException(status_code=404)
    target = util.display_name(person)
    with db.tx(conn):
        avatars = delete_person(conn, person_id, user["id"], user["display_name"])
    for key in avatars:
        media.delete_avatar(request.app.state.settings, key)
    response = RedirectResponse("/tree", status_code=303)
    set_flash(response, f"{target} was removed from the tree.")
    return response


@router.get("/persons/{person_id}/history")
def person_history(request: Request, person_id: str):
    user = auth.require_admin(request)
    conn = request.state.conn
    person = get_person(conn, person_id, include_deleted=True)
    if person is None:
        raise HTTPException(status_code=404)
    events = conn.execute(
        "SELECT * FROM audit_events WHERE entity_type = 'person' AND entity_id = ? "
        "ORDER BY created_at DESC LIMIT 200",
        (person_id,),
    ).fetchall()
    parsed = []
    for event in events:
        parsed.append({
            "row": event,
            "before": _pretty_json(event["before_json"]),
            "after": _pretty_json(event["after_json"]),
        })
    return render(request, "admin/history.html", person=person, events=parsed,
                  is_admin=True)


def _pretty_json(raw: str):
    if not raw:
        return ""
    try:
        return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        return raw
