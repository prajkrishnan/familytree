"""Tree page, tree graph API, search, person drawer and avatar serving."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .. import auth, db, media, policy, util
from ..relationships import (birth_union_of, children_of_union, child_links, get_person,
                             parent_links, sibling_links, spouse_links, unions_for_person)
from ..templating import render

router = APIRouter()

CARD_PRIVATE_GROUPS = {"birth", "death", "photo", "memorial"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def default_focus(conn, user) -> str | None:
    if user and user["person_id"]:
        if get_person(conn, user["person_id"]):
            return user["person_id"]
    root_union_id = db.get_setting(conn, "root_union_id")
    if root_union_id:
        union = conn.execute("SELECT * FROM unions WHERE id = ? AND deleted_at IS NULL",
                             (root_union_id,)).fetchone()
        if union:
            return union["partner1_id"]
    row = conn.execute(
        "SELECT id FROM persons WHERE deleted_at IS NULL ORDER BY created_at, id LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def root_person_id(conn) -> str | None:
    root_union_id = db.get_setting(conn, "root_union_id")
    if root_union_id:
        union = conn.execute("SELECT * FROM unions WHERE id = ? AND deleted_at IS NULL",
                             (root_union_id,)).fetchone()
        if union:
            return union["partner1_id"]
    row = conn.execute(
        "SELECT id FROM persons WHERE deleted_at IS NULL ORDER BY created_at, id LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def resolve_root(conn, focus_id: str | None) -> tuple[str | None, str | None]:
    """Return (root_union_id, root_person_id)."""
    if not focus_id:
        root_id = root_person_id(conn)
        if not root_id:
            return None, None
        focus_id = root_id
    union = birth_union_of(conn, focus_id)
    if union:
        return union["id"], None
    unions = unions_for_person(conn, focus_id)
    if unions:
        def score(item):
            count = conn.execute("SELECT COUNT(*) FROM union_children WHERE union_id = ?",
                                 (item["id"],)).fetchone()[0]
            return (count, 1 if item["partner2_id"] else 0)
        best = sorted(unions, key=score, reverse=True)[0]
        return best["id"], None
    return None, focus_id


def _card_hidden(person, viewer) -> set[str]:
    if viewer is None:
        return set(util.PRIVACY_GROUPS)
    if viewer["role"] == "admin" or (viewer["person_id"] and viewer["person_id"] == person["id"]):
        return set()
    try:
        raw = json.loads(person["private_fields"] or "[]")
        return {g for g in raw if g in CARD_PRIVATE_GROUPS} if isinstance(raw, list) else set()
    except (ValueError, TypeError):
        return set()


def _avatar_url(person, thumb: bool = False) -> str | None:
    if not person["avatar_key"]:
        return None
    suffix = "&size=thumb" if thumb else ""
    return f"/media/avatar/{person['id']}?v={person['avatar_updated_at'] or 0}{suffix}"


def _tree_person(person, viewer, pending_ids: set[str]) -> dict:
    hidden = _card_hidden(person, viewer)
    birth_year = None if "birth" in hidden else person["birth_year"]
    death_year = None if "death" in hidden else person["death_year"]
    masked = {
        "birth_year": birth_year,
        "death_year": death_year,
        "birth_qualifier": "exact" if "birth" in hidden else person["birth_qualifier"],
        "death_qualifier": "exact" if "death" in hidden else person["death_qualifier"],
        "is_living": person["is_living"],
    }
    return {
        "id": person["id"],
        "name": util.display_name(person),
        "given": person["given_name"],
        "surname": person["surname"],
        "nickname": person["nickname"],
        "gender": person["gender"],
        "living": bool(person["is_living"]),
        "lifespan": util.lifespan(masked),
        "birth_year": birth_year,
        "death_year": death_year,
        "avatar": None if "photo" in hidden else _avatar_url(person, thumb=True),
        "pending": person["id"] in pending_ids,
        "self": bool(viewer and viewer["person_id"] == person["id"]),
        "private_birth": "birth" in hidden,
        "private_death": "death" in hidden,
    }


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@router.get("/tree")
def tree_page(request: Request, focus: str = "", open: str = ""):
    user = auth.require_user(request)
    conn = request.state.conn
    focus_id = focus if focus and get_person(conn, focus) else default_focus(conn, user)
    has_people = conn.execute(
        "SELECT 1 FROM persons WHERE deleted_at IS NULL LIMIT 1").fetchone() is not None
    root_person = root_person_id(conn)
    return render(request, "tree.html", focus_id=focus_id or "", open_id=open,
                  has_people=has_people, root_person_id=root_person or "",
                  is_admin=user["role"] == "admin", user_person_id=user["person_id"] or "")


@router.get("/api/tree")
def tree_api(request: Request, focus: str = ""):
    user = auth.require_user(request)
    conn = request.state.conn
    focus_id = focus if focus and get_person(conn, focus) else default_focus(conn, user)
    if not focus_id:
        return JSONResponse({"persons": [], "unions": [], "children": [], "root": None,
                             "focus": None, "authenticated": True, "root_person_id": None})
    root_union_id, root_person = resolve_root(conn, focus_id)
    pending_ids = policy.pending_entity_ids(conn, user)

    persons = conn.execute(
        "SELECT * FROM persons WHERE deleted_at IS NULL ORDER BY COALESCE(birth_year, 9999), given_name"
    ).fetchall()
    nodes = [_tree_person(person, user, pending_ids) for person in persons]
    unions = conn.execute(
        "SELECT id, partner1_id, partner2_id, kind, status FROM unions WHERE deleted_at IS NULL"
    ).fetchall()
    children = conn.execute(
        "SELECT uc.union_id, uc.child_id, uc.relationship_type FROM union_children uc "
        "JOIN persons p ON p.id = uc.child_id AND p.deleted_at IS NULL "
        "JOIN unions u ON u.id = uc.union_id AND u.deleted_at IS NULL"
    ).fetchall()
    updated = conn.execute(
        "SELECT COALESCE(MAX(updated_at), 0) AS ts FROM persons WHERE deleted_at IS NULL"
    ).fetchone()["ts"]
    payload = {
        "persons": nodes,
        "unions": [dict(u) for u in unions],
        "children": [{"u": c["union_id"], "c": c["child_id"], "r": c["relationship_type"]}
                     for c in children],
        "root": {"union_id": root_union_id, "person_id": root_person},
        "focus": focus_id,
        "self": user["person_id"],
        "is_admin": user["role"] == "admin",
        "root_person_id": root_person_id(conn),
        "updated": updated,
        "total": len(nodes),
    }
    response = JSONResponse(payload)
    response.headers["ETag"] = f'W/"{updated}-{len(nodes)}"'
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.get("/api/search")
def search_api(request: Request, q: str = ""):
    user = auth.require_user(request)
    conn = request.state.conn
    query = q.strip().lower()[:80]
    if len(query) < 1:
        return JSONResponse({"results": []})
    rows = conn.execute(
        "SELECT * FROM persons WHERE deleted_at IS NULL AND search_text LIKE ? "
        "ORDER BY CASE WHEN search_text LIKE ? THEN 0 ELSE 1 END, surname, given_name LIMIT 15",
        (f"%{query}%", f"{query}%"),
    ).fetchall()
    results = []
    for person in rows:
        hidden = _card_hidden(person, user)
        masked = {
            "birth_year": None if "birth" in hidden else person["birth_year"],
            "death_year": None if "death" in hidden else person["death_year"],
            "birth_qualifier": "exact", "death_qualifier": "exact",
            "is_living": person["is_living"],
        }
        results.append({
            "id": person["id"],
            "name": util.full_name(person),
            "subtitle": util.lifespan(masked),
            "gender": person["gender"],
            "living": bool(person["is_living"]),
            "avatar": None if "photo" in hidden else _avatar_url(person, thumb=True),
        })
    return JSONResponse({"results": results})


@router.get("/api/persons/{person_id}/card")
def person_card(request: Request, person_id: str):
    user = auth.require_user(request)
    conn = request.state.conn
    person = get_person(conn, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    view = policy.person_view(conn, person, user)
    hidden = set(view["hidden_groups"])
    family = _family_links(conn, person, user)
    can_submit = policy.can_submit_for(conn, user, person_id)
    pending = conn.execute(
        "SELECT COUNT(*) FROM change_items ci JOIN change_sets cs ON cs.id = ci.set_id "
        "WHERE cs.status = 'pending' AND ci.entity_type = 'person' AND ci.entity_id = ?",
        (person_id,),
    ).fetchone()[0]
    history_count = 0
    if user["role"] == "admin":
        history_count = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE entity_type = 'person' AND entity_id = ?",
            (person_id,),
        ).fetchone()[0]
    return render(request, "partials/person_card.html", person=view, hidden=hidden,
                  family=family, can_submit=can_submit, is_admin=user["role"] == "admin",
                  is_self=bool(user["person_id"] == person_id), pending_count=pending,
                  history_count=history_count)


def _family_links(conn, person, viewer) -> dict:
    parents = parent_links(conn, person["id"])
    children = child_links(conn, person["id"])
    siblings = sibling_links(conn, person["id"])
    spouses = spouse_links(conn, person["id"])
    my_parents = {p["id"] for p in parents}

    def parent_label(p):
        base = "Father" if p["gender"] == "male" else "Mother" if p["gender"] == "female" else "Parent"
        if p["link_type"] == "adopted":
            return "Adoptive " + base.lower()
        if p["link_type"] == "step":
            return "Step-" + base.lower()
        return base

    def sibling_label(s):
        base = "Brother" if s["gender"] == "male" else "Sister" if s["gender"] == "female" else "Sibling"
        sib_parents = {r["parent_id"] for r in conn.execute(
            "SELECT parent_id FROM parent_child WHERE child_id = ?", (s["id"],))}
        if my_parents and not my_parents.issubset(sib_parents):
            return "Half-" + base.lower()
        return base

    def child_label(c):
        base = "Son" if c["gender"] == "male" else "Daughter" if c["gender"] == "female" else "Child"
        if c["link_type"] == "adopted":
            return "Adopted " + base.lower()
        if c["link_type"] == "step":
            return "Step" + base.lower()
        return base

    def spouse_label(union):
        if union["status"] in ("divorced", "separated"):
            return "Former spouse"
        if union["status"] == "widowed":
            return "Late spouse"
        return "Spouse"

    return {
        "parents": [(p, parent_label(p)) for p in parents],
        "siblings": [(s, sibling_label(s)) for s in siblings],
        "spouses": [(s, spouse_label(u), u) for s, u in spouses],
        "children": [(c, child_label(c)) for c in children],
    }


@router.get("/media/avatar/{person_id}")
def avatar(request: Request, person_id: str, size: str = "full", v: str = ""):
    user = auth.require_user(request)
    conn = request.state.conn
    person = get_person(conn, person_id)
    if person is None or not person["avatar_key"]:
        raise HTTPException(status_code=404)
    hidden = policy.hidden_groups(conn, person, user)
    if "photo" in hidden:
        raise HTTPException(status_code=404)
    path = media.avatar_file(request.app.state.settings, person["avatar_key"], thumb=(size == "thumb"))
    if path is None:
        raise HTTPException(status_code=404)
    etag = f'"{person["avatar_key"]}-{size}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=86400", "ETag": etag})
