"""Administration: users, invites, settings, backups, import/export and audit."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from .. import auth, db, media, moderation, policy, security, util
from ..config import Settings
from ..relationships import (get_person, get_union, rebuild_all_parent_child, record_audit,
                             unions_for_person, RelationshipError)
from ..templating import get_site_title, render, set_flash
from ..util import clean_text, new_id, now, search_text

router = APIRouter()

BACKUP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.db$")


def _active_admin_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND status = 'active'"
    ).fetchone()[0]


@router.get("/admin")
def admin_home(request: Request):
    user = auth.require_admin(request)
    conn = request.state.conn
    stats = {
        "people": conn.execute("SELECT COUNT(*) FROM persons WHERE deleted_at IS NULL").fetchone()[0],
        "unions": conn.execute("SELECT COUNT(*) FROM unions WHERE deleted_at IS NULL").fetchone()[0],
        "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "pending_users": conn.execute(
            "SELECT COUNT(*) FROM users WHERE status = 'pending'").fetchone()[0],
        "pending_changes": moderation.pending_count(conn),
        "invites": conn.execute(
            "SELECT COUNT(*) FROM invites WHERE used_at IS NULL AND expires_at > ?",
            (now(),)).fetchone()[0],
    }
    recent = conn.execute(
        "SELECT * FROM audit_events ORDER BY created_at DESC LIMIT 12").fetchall()
    return render(request, "admin/overview.html", stats=stats, recent=recent, is_admin=True)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def _users_context(request, reset_password: str = "", reset_user: str = "") -> dict:
    conn = request.state.conn
    users = conn.execute(
        "SELECT u.*, p.given_name AS person_given, p.surname AS person_surname "
        "FROM users u LEFT JOIN persons p ON p.id = u.person_id "
        "ORDER BY CASE u.status WHEN 'pending' THEN 0 ELSE 1 END, u.created_at"
    ).fetchall()
    people_count = conn.execute(
        "SELECT COUNT(*) FROM persons WHERE deleted_at IS NULL").fetchone()[0]
    people = [{"id": r["id"], "name": util.display_name(r)}
              for r in conn.execute(
                  "SELECT * FROM persons WHERE deleted_at IS NULL ORDER BY surname, given_name")]
    return {"users": users, "reset_password": reset_password, "reset_user": reset_user,
            "people_count": people_count, "is_admin": True, "people": people,
            "active_admins": _active_admin_count(conn)}


@router.get("/admin/users")
def users_page(request: Request):
    auth.require_admin(request)
    return render(request, "admin/users.html", **_users_context(request))


@router.post("/admin/users/{user_id}/approve")
async def approve_user(request: Request, user_id: str):
    admin = auth.require_admin(request)
    conn = request.state.conn
    form = await request.form()
    person_id = str(form.get("person_id", "")).strip()
    target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if target is None:
        raise HTTPException(status_code=404)
    if person_id:
        person = get_person(conn, person_id)
        if person is None:
            return _users_error(request, "That person record could not be found.")
        claimed = conn.execute(
            "SELECT id FROM users WHERE person_id = ? AND status = 'active' AND id <> ?",
            (person_id, user_id),
        ).fetchone()
        if claimed:
            return _users_error(request, "That family record is already linked to another account.")
    conn.execute(
        "UPDATE users SET status = 'active', person_id = ?, approved_by = ?, approved_at = ? "
        "WHERE id = ?",
        (person_id or None, admin["id"], now(), user_id),
    )
    record_audit(conn, admin["id"], admin["display_name"], "user.approve", "user", user_id,
                 None, {"person_id": person_id}, "", "")
    response = RedirectResponse("/admin/users", status_code=303)
    set_flash(response, f"{target['display_name']} approved.")
    return response


def _users_error(request, message: str):
    context = _users_context(request)
    return render(request, "admin/users.html", error=message, **context, status_code=400)


@router.post("/admin/users/{user_id}/reject")
def reject_user(request: Request, user_id: str):
    admin = auth.require_admin(request)
    conn = request.state.conn
    conn.execute("UPDATE users SET status = 'disabled' WHERE id = ?", (user_id,))
    record_audit(conn, admin["id"], admin["display_name"], "user.reject", "user", user_id,
                 None, None, "", "")
    response = RedirectResponse("/admin/users", status_code=303)
    set_flash(response, "Registration rejected.")
    return response


@router.post("/admin/users/{user_id}/role")
async def set_role(request: Request, user_id: str):
    admin = auth.require_admin(request)
    conn = request.state.conn
    form = await request.form()
    role = str(form.get("role", "user"))
    if role not in ("admin", "user"):
        raise HTTPException(status_code=400)
    target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if target is None:
        raise HTTPException(status_code=404)
    if target["role"] == "admin" and role == "user":
        if target["status"] == "active" and _active_admin_count(conn) <= 1:
            return _users_error(request, "You cannot demote the last active administrator.")
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    record_audit(conn, admin["id"], admin["display_name"], "user.role", "user", user_id,
                 {"role": target["role"]}, {"role": role}, "", "")
    response = RedirectResponse("/admin/users", status_code=303)
    set_flash(response, f"{target['display_name']} is now {'an administrator' if role == 'admin' else 'a regular user'}.")
    return response


@router.post("/admin/users/{user_id}/status")
async def set_status(request: Request, user_id: str):
    admin = auth.require_admin(request)
    conn = request.state.conn
    form = await request.form()
    status = str(form.get("status", "active"))
    if status not in ("active", "disabled"):
        raise HTTPException(status_code=400)
    target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if target is None:
        raise HTTPException(status_code=404)
    if target["id"] == admin["id"] and status == "disabled":
        return _users_error(request, "You cannot deactivate your own account.")
    if target["role"] == "admin" and status == "disabled" and _active_admin_count(conn) <= 1:
        return _users_error(request, "You cannot deactivate the last active administrator.")
    conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
    if status == "disabled":
        auth.revoke_all_sessions(conn, user_id)
    record_audit(conn, admin["id"], admin["display_name"], "user.status", "user", user_id,
                 {"status": target["status"]}, {"status": status}, "", "")
    response = RedirectResponse("/admin/users", status_code=303)
    set_flash(response, "Account updated.")
    return response


@router.post("/admin/users/{user_id}/link")
async def link_person(request: Request, user_id: str):
    admin = auth.require_admin(request)
    conn = request.state.conn
    form = await request.form()
    person_id = str(form.get("person_id", "")).strip()
    target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if target is None:
        raise HTTPException(status_code=404)
    if person_id:
        if get_person(conn, person_id) is None:
            return _users_error(request, "That person record could not be found.")
        claimed = conn.execute(
            "SELECT id FROM users WHERE person_id = ? AND status = 'active' AND id <> ?",
            (person_id, user_id),
        ).fetchone()
        if claimed:
            return _users_error(request, "That family record is already linked to another account.")
    conn.execute("UPDATE users SET person_id = ? WHERE id = ?", (person_id or None, user_id))
    record_audit(conn, admin["id"], admin["display_name"], "user.link", "user", user_id,
                 {"person_id": target["person_id"]}, {"person_id": person_id}, "", "")
    response = RedirectResponse("/admin/users", status_code=303)
    set_flash(response, "Account link updated.")
    return response


@router.post("/admin/users/{user_id}/reset-password")
def reset_password(request: Request, user_id: str):
    admin = auth.require_admin(request)
    conn = request.state.conn
    target = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if target is None:
        raise HTTPException(status_code=404)
    temporary = "fam-" + security.new_token(9)
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (security.hash_password(temporary), user_id))
    auth.revoke_all_sessions(conn, user_id)
    record_audit(conn, admin["id"], admin["display_name"], "user.password_reset", "user", user_id,
                 None, None, "", "")
    context = _users_context(request, reset_password=temporary, reset_user=target["display_name"])
    return render(request, "admin/users.html", **context)


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------

def _invites_context(request) -> dict:
    conn = request.state.conn
    invites = conn.execute(
        "SELECT i.*, u.display_name AS creator_name, p.given_name, p.surname "
        "FROM invites i LEFT JOIN users u ON u.id = i.created_by "
        "LEFT JOIN persons p ON p.id = i.person_id "
        "ORDER BY i.created_at DESC LIMIT 100"
    ).fetchall()
    people = [{"id": r["id"], "name": util.display_name(r)}
              for r in conn.execute(
                  "SELECT * FROM persons WHERE deleted_at IS NULL ORDER BY surname, given_name")]
    return {"invites": invites, "is_admin": True, "people": people, "now_ts": now()}


@router.get("/admin/invites")
def invites_page(request: Request):
    auth.require_admin(request)
    return render(request, "admin/invites.html", **_invites_context(request))


@router.post("/admin/invites")
async def create_invite(request: Request):
    admin = auth.require_admin(request)
    conn = request.state.conn
    settings: Settings = request.app.state.settings
    form = await request.form()
    days = _int_between(str(form.get("days", "7")), 1, 90, 7)
    person_id = str(form.get("person_id", "")).strip()
    note = clean_text(str(form.get("note", "")))[:240]
    role = str(form.get("role", "user"))
    if role not in ("admin", "user"):
        role = "user"
    if person_id and get_person(conn, person_id) is None:
        context = _invites_context(request)
        return render(request, "admin/invites.html", error="That person record could not be found.",
                      **context, status_code=400)
    token = security.new_token(24)
    conn.execute(
        "INSERT INTO invites(id, token_hash, created_by, person_id, note, role, created_at, expires_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (new_id(), security.token_hash(token), admin["id"], person_id or None, note, role,
         now(), now() + days * 86400),
    )
    record_audit(conn, admin["id"], admin["display_name"], "invite.create", "invite", "", None,
                 {"days": days, "role": role}, "", "")
    link = settings.base_url + "/invite/" + token
    context = _invites_context(request)
    context["new_link"] = link
    return render(request, "admin/invites.html", **context)


@router.post("/admin/invites/{invite_id}/revoke")
def revoke_invite(request: Request, invite_id: str):
    admin = auth.require_admin(request)
    conn = request.state.conn
    conn.execute("DELETE FROM invites WHERE id = ? AND used_at IS NULL", (invite_id,))
    record_audit(conn, admin["id"], admin["display_name"], "invite.revoke", "invite", invite_id,
                 None, None, "", "")
    response = RedirectResponse("/admin/invites", status_code=303)
    set_flash(response, "Invite link revoked.")
    return response


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _settings_context(request) -> dict:
    conn = request.state.conn
    settings = request.app.state.settings
    root_union_id = db.get_setting(conn, "root_union_id")
    root_union = get_union(conn, root_union_id) if root_union_id else None
    root_people = []
    if root_union:
        for pid in (root_union["partner1_id"], root_union["partner2_id"]):
            if pid:
                person = get_person(conn, pid)
                if person:
                    root_people.append(person)
    return {
        "is_admin": True,
        "registration_mode": db.get_setting(conn, "registration_mode", settings.registration_mode),
        "passcode_set": bool(db.get_setting(conn, "passcode_hash")),
        "site_title": get_site_title(conn, settings),
        "root_people": root_people,
        "root_union": root_union,
        "people_count": conn.execute(
            "SELECT COUNT(*) FROM persons WHERE deleted_at IS NULL").fetchone()[0],
        "people": [{"id": r["id"], "name": util.display_name(r)}
                   for r in conn.execute(
                       "SELECT * FROM persons WHERE deleted_at IS NULL ORDER BY surname, given_name")],
    }


@router.get("/admin/settings")
def settings_page(request: Request):
    auth.require_admin(request)
    return render(request, "admin/settings.html", **_settings_context(request))


@router.post("/admin/settings/general")
async def settings_general(request: Request):
    admin = auth.require_admin(request)
    conn = request.state.conn
    form = await request.form()
    title = clean_text(str(form.get("site_title", "")))[:80] or "Family Tree"
    mode = str(form.get("registration_mode", "invite_only"))
    if mode not in ("invite_only", "passcode", "open"):
        mode = "invite_only"
    db.set_setting(conn, "site_title", title)
    db.set_setting(conn, "registration_mode", mode)
    record_audit(conn, admin["id"], admin["display_name"], "settings.update", "instance", "",
                 None, {"site_title": title, "registration_mode": mode}, "", "")
    response = RedirectResponse("/admin/settings", status_code=303)
    set_flash(response, "Settings saved.")
    return response


@router.post("/admin/settings/passcode")
async def settings_passcode(request: Request):
    admin = auth.require_admin(request)
    conn = request.state.conn
    form = await request.form()
    passcode = str(form.get("passcode", ""))
    if str(form.get("clear", "")) == "1" or not passcode:
        db.set_setting(conn, "passcode_hash", "")
        message = "Family passcode cleared."
    else:
        if len(passcode) < 8:
            response = RedirectResponse("/admin/settings", status_code=303)
            set_flash(response, "Passcode must be at least 8 characters.", "error")
            return response
        db.set_setting(conn, "passcode_hash", security.hash_password(passcode))
        message = "Family passcode updated."
    record_audit(conn, admin["id"], admin["display_name"], "settings.passcode", "instance", "",
                 None, None, "", "")
    response = RedirectResponse("/admin/settings", status_code=303)
    set_flash(response, message)
    return response


@router.post("/admin/settings/root")
async def settings_root(request: Request):
    admin = auth.require_admin(request)
    conn = request.state.conn
    form = await request.form()
    p1 = str(form.get("partner1_id", "")).strip()
    p2 = str(form.get("partner2_id", "")).strip()
    if not p1:
        db.set_setting(conn, "root_union_id", "")
        response = RedirectResponse("/admin/settings", status_code=303)
        set_flash(response, "Founding couple cleared.")
        return response
    if get_person(conn, p1) is None or (p2 and get_person(conn, p2) is None):
        response = RedirectResponse("/admin/settings", status_code=303)
        set_flash(response, "Choose valid people for the founding couple.", "error")
        return response
    if p2 and p1 == p2:
        response = RedirectResponse("/admin/settings", status_code=303)
        set_flash(response, "Choose two different people.", "error")
        return response
    union = conn.execute(
        "SELECT * FROM unions WHERE deleted_at IS NULL AND ("
        "(partner1_id = ? AND partner2_id = ?) OR (partner1_id = ? AND partner2_id = ?))",
        (p1, p2, p2, p1),
    ).fetchone()
    if union is None:
        single = None
        if p2:
            single = conn.execute(
                "SELECT * FROM unions WHERE deleted_at IS NULL AND partner1_id = ? "
                "AND partner2_id IS NULL ORDER BY created_at LIMIT 1",
                (p1,)).fetchone()
        if single is not None:
            from ..relationships import set_partner
            try:
                with db.tx(conn):
                    set_partner(conn, single["id"], p2, admin["id"], admin["display_name"])
                    db.set_setting(conn, "root_union_id", single["id"])
            except RelationshipError as exc:
                response = RedirectResponse("/admin/settings", status_code=303)
                set_flash(response, str(exc), "error")
                return response
            record_audit(conn, admin["id"], admin["display_name"], "settings.root", "instance", "",
                         None, {"union_id": single["id"]}, "", "")
            response = RedirectResponse("/admin/settings", status_code=303)
            set_flash(response, "Founding couple saved.")
            return response
        from ..relationships import create_union
        with db.tx(conn):
            new_union = create_union(conn, p1, p2 or None, "marriage", "unknown",
                                     admin["id"], admin["display_name"])
            db.set_setting(conn, "root_union_id", new_union)
        record_audit(conn, admin["id"], admin["display_name"], "settings.root", "instance", "",
                     None, {"union_id": new_union}, "", "")
        response = RedirectResponse("/admin/settings", status_code=303)
        set_flash(response, "Founding couple saved.")
        return response
    db.set_setting(conn, "root_union_id", union["id"])
    record_audit(conn, admin["id"], admin["display_name"], "settings.root", "instance", "",
                 None, {"union_id": union["id"]}, "", "")
    response = RedirectResponse("/admin/settings", status_code=303)
    set_flash(response, "Founding couple saved.")
    return response


# ---------------------------------------------------------------------------
# Export / import / backups
# ---------------------------------------------------------------------------

@router.get("/admin/backups")
def backups_page(request: Request):
    auth.require_admin(request)
    conn = request.state.conn
    files = []
    for path in sorted(request.app.state.settings.backup_dir.glob("*.db"), reverse=True):
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append({"name": path.name, "size": stat.st_size,
                      "modified": int(stat.st_mtime)})
    return render(request, "admin/backups.html", files=files, is_admin=True,
                  export_json_size=len(json.dumps(_export_payload(conn))))


def _export_payload(conn) -> dict:
    persons = []
    for row in conn.execute("SELECT * FROM persons WHERE deleted_at IS NULL").fetchall():
        person = dict(row)
        person.pop("search_text", None)
        persons.append(person)
    unions = [dict(r) for r in conn.execute(
        "SELECT * FROM unions WHERE deleted_at IS NULL").fetchall()]
    children = [dict(r) for r in conn.execute(
        "SELECT * FROM union_children").fetchall()]
    return {
        "format": "familytree.v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "root_union_id": db.get_setting(conn, "root_union_id"),
        "persons": persons,
        "unions": unions,
        "union_children": children,
    }


@router.get("/admin/export/json")
def export_json(request: Request):
    auth.require_admin(request)
    conn = request.state.conn
    payload = _export_payload(conn)
    body = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
    filename = "family-tree-" + datetime.now().strftime("%Y%m%d-%H%M") + ".json"
    return Response(body, media_type="application/json",
                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _make_snapshot(conn, settings: Settings, label: str = "family") -> Path:
    filename = f"{label}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    path = settings.backup_dir / filename
    counter = 1
    while path.exists():
        path = settings.backup_dir / f"{label}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{counter}.db"
        counter += 1
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM INTO ?", (str(path),))
    return path


@router.get("/admin/export/sqlite")
def export_sqlite(request: Request):
    auth.require_admin(request)
    settings = request.app.state.settings
    path = _make_snapshot(request.state.conn, settings)
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=path.name)


@router.post("/admin/backups/create")
def create_backup(request: Request):
    admin = auth.require_admin(request)
    path = _make_snapshot(request.state.conn, request.app.state.settings)
    record_audit(conn=request.state.conn, actor_id=admin["id"], actor_name=admin["display_name"],
                 action="backup.create", entity_type="instance", entity_id=path.name,
                 before=None, after={"name": path.name}, ip="", user_agent="")
    response = RedirectResponse("/admin/backups", status_code=303)
    set_flash(response, f"Backup created: {path.name}")
    return response


@router.get("/admin/backups/{name}/download")
def download_backup(request: Request, name: str):
    auth.require_admin(request)
    if not BACKUP_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid backup name")
    path = (request.app.state.settings.backup_dir / name).resolve()
    if path.parent != request.app.state.settings.backup_dir.resolve() or not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=name)


@router.post("/admin/backups/import")
async def import_json(request: Request):
    admin = auth.require_admin(request)
    conn = request.state.conn
    settings: Settings = request.app.state.settings
    form = await request.form()
    confirm = str(form.get("confirm", "")).strip()
    upload = form.get("file")
    if confirm != "REPLACE":
        response = RedirectResponse("/admin/backups", status_code=303)
        set_flash(response, 'Type REPLACE in the confirmation box to import.', "error")
        return response
    if not upload or not getattr(upload, "filename", ""):
        response = RedirectResponse("/admin/backups", status_code=303)
        set_flash(response, "Choose a JSON export file first.", "error")
        return response
    raw = upload.file.read(40 * 1024 * 1024 + 1)
    if len(raw) > 40 * 1024 * 1024:
        response = RedirectResponse("/admin/backups", status_code=303)
        set_flash(response, "That import file is too large.", "error")
        return response
    try:
        payload = json.loads(raw.decode("utf-8"))
        _import_payload(conn, payload, admin)
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, sqlite3.Error) as exc:
        response = RedirectResponse("/admin/backups", status_code=303)
        set_flash(response, f"Import failed: {exc}", "error")
        return response
    record_audit(conn, admin["id"], admin["display_name"], "backup.import", "instance", "",
                 None, {"persons": len(payload.get("persons", []))}, "", "")
    response = RedirectResponse("/tree", status_code=303)
    set_flash(response, "Tree data imported.")
    return response


def _import_payload(conn, payload: dict, admin) -> None:
    if payload.get("format") != "familytree.v1":
        raise ValueError("This file is not a Family Tree JSON export.")
    persons = payload.get("persons") or []
    unions = payload.get("unions") or []
    children = payload.get("union_children") or []
    if not isinstance(persons, list) or not isinstance(unions, list) or not isinstance(children, list):
        raise ValueError("Malformed export data.")

    person_ids = set()
    for person in persons:
        pid = str(person.get("id", ""))
        if not pid or pid in person_ids:
            raise ValueError("Person IDs must be unique and non-empty.")
        person_ids.add(pid)
        if person.get("gender", "other") not in ("male", "female", "other"):
            raise ValueError("Invalid gender in import.")
    union_ids = set()
    for union in unions:
        uid = str(union.get("id", ""))
        if not uid or uid in union_ids:
            raise ValueError("Family IDs must be unique and non-empty.")
        union_ids.add(uid)
        if str(union.get("partner1_id", "")) not in person_ids:
            raise ValueError("A family references a missing partner.")
        partner2 = union.get("partner2_id")
        if partner2 and str(partner2) not in person_ids:
            raise ValueError("A family references a missing partner.")
        if partner2 and partner2 == union.get("partner1_id"):
            raise ValueError("A family cannot have the same person twice.")
    seen_children = set()
    for link in children:
        uid, cid = str(link.get("union_id", "")), str(link.get("child_id", ""))
        if uid not in union_ids or cid not in person_ids:
            raise ValueError("A child link references a missing record.")
        if (uid, cid) in seen_children:
            raise ValueError("Duplicate child link in import.")
        seen_children.add((uid, cid))

    timestamp = now()
    with db.tx(conn):
        conn.execute("DELETE FROM parent_child")
        conn.execute("DELETE FROM union_children")
        conn.execute("DELETE FROM unions")
        conn.execute("DELETE FROM persons")
        conn.execute("UPDATE users SET person_id = NULL")
        conn.execute("UPDATE invites SET person_id = NULL")
        conn.execute("DELETE FROM change_sets")

        for person in persons:
            row = _person_row(person, admin, timestamp)
            columns = ",".join(row.keys())
            placeholders = ",".join("?" for _ in row)
            conn.execute(f"INSERT INTO persons({columns}) VALUES ({placeholders})", list(row.values()))
        for union in unions:
            row = _union_row(union, admin, timestamp)
            columns = ",".join(row.keys())
            placeholders = ",".join("?" for _ in row)
            conn.execute(f"INSERT INTO unions({columns}) VALUES ({placeholders})", list(row.values()))
        for link in children:
            conn.execute(
                "INSERT INTO union_children(union_id, child_id, relationship_type, sort_order, created_at) "
                "VALUES (?,?,?,?,?)",
                (str(link["union_id"]), str(link["child_id"]),
                 link.get("relationship_type") or "birth", int(link.get("sort_order") or 0), timestamp),
            )
        rebuild_all_parent_child(conn)
        cycle = conn.execute(
            "WITH RECURSIVE walk(start, id, depth) AS ("
            "  SELECT parent_id, child_id, 1 FROM parent_child"
            "  UNION ALL"
            "  SELECT w.start, pc.child_id, w.depth + 1 FROM walk w"
            "  JOIN parent_child pc ON pc.parent_id = w.id WHERE w.depth < 60"
            ") SELECT 1 FROM walk WHERE start = id LIMIT 1"
        ).fetchone()
        if cycle:
            raise ValueError("The imported data contains a parent/child loop.")
        root_union_id = payload.get("root_union_id") or ""
        if root_union_id and root_union_id not in union_ids:
            root_union_id = ""
        db.set_setting(conn, "root_union_id", root_union_id)


def _person_row(person: dict, admin, timestamp: int) -> dict:
    data = {
        "given_name": "", "middle_names": "", "surname": "", "maiden_name": "", "nickname": "",
        "gender": "other", "is_living": 1,
        "birth_day": None, "birth_month": None, "birth_year": None, "birth_qualifier": "exact",
        "death_day": None, "death_month": None, "death_year": None, "death_qualifier": "exact",
        "resting_place": "", "memorial_note": "", "notes": "", "email": "", "phone": "",
        "phone_alt": "", "address_line1": "", "address_line2": "", "city": "", "region": "",
        "postal_code": "", "country": "", "avatar_key": None, "avatar_updated_at": None,
        "contact_visibility": "all", "private_fields": "[]",
        "version": 1, "created_by": admin["id"], "updated_by": admin["id"],
        "created_at": timestamp, "updated_at": timestamp, "deleted_at": None,
    }
    data.update({k: v for k, v in person.items() if k in data})
    data["id"] = str(person["id"])
    data["avatar_key"] = None  # binary media lives in the SQLite snapshot, not JSON
    data["avatar_updated_at"] = None
    data["search_text"] = search_text(data)
    if isinstance(data["private_fields"], list):
        data["private_fields"] = json.dumps(data["private_fields"])
    return data


def _union_row(union: dict, admin, timestamp: int) -> dict:
    data = {
        "partner1_id": "", "partner2_id": None, "kind": "unknown", "status": "unknown",
        "start_day": None, "start_month": None, "start_year": None, "start_qualifier": "exact",
        "end_day": None, "end_month": None, "end_year": None, "end_qualifier": "exact",
        "notes": "", "created_by": admin["id"], "created_at": timestamp,
        "updated_at": timestamp, "deleted_at": None,
    }
    data.update({k: v for k, v in union.items() if k in data})
    data["id"] = str(union["id"])
    if data["kind"] not in ("marriage", "civil_union", "partnership", "unknown"):
        data["kind"] = "unknown"
    if data["status"] not in ("ongoing", "divorced", "widowed", "separated", "unknown"):
        data["status"] = "unknown"
    return data


@router.get("/admin/audit")
def audit_log(request: Request):
    auth.require_admin(request)
    conn = request.state.conn
    events = conn.execute("SELECT * FROM audit_events ORDER BY created_at DESC LIMIT 300").fetchall()
    return render(request, "admin/audit.html", events=events, is_admin=True)


def _int_between(raw: str, lo: int, hi: int, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if lo <= value <= hi else default
