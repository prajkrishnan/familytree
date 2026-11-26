"""Pending-change sets: submission, diffs, approval and rejection."""
from __future__ import annotations

import json
import sqlite3

from . import media
from .config import Settings
from .relationships import (add_child, create_person, create_union, get_person, get_union,
                            record_audit, set_partner, update_person, RelationshipError)
from .util import (MONTHS, format_parts, full_name, new_id, now)

FIELD_LABELS = {
    "given_name": "Given name", "middle_names": "Middle names", "surname": "Family name",
    "maiden_name": "Maiden name", "nickname": "Nickname", "gender": "Gender",
    "is_living": "Living status", "resting_place": "Resting place",
    "memorial_note": "Memorial note", "notes": "Notes / additional information",
    "email": "Email", "phone": "Phone", "phone_alt": "Alternate phone",
    "address_line1": "Address line 1", "address_line2": "Address line 2", "city": "City",
    "region": "Region / state", "postal_code": "Postal code", "country": "Country",
    "contact_visibility": "Contact visibility", "private_fields": "Private fields",
    "avatar_key": "Profile photo",
}

DATE_FIELDS = {
    "birth_day", "birth_month", "birth_year", "birth_qualifier",
    "death_day", "death_month", "death_year", "death_qualifier",
}


def _loads(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def person_has_changes(person, payload: dict) -> bool:
    for key, value in payload.items():
        if key.startswith("_"):
            continue
        if key in ("avatar_key", "avatar_updated_at") and value:
            return True
        if key in person.keys() and str(person[key] if person[key] is not None else "") != str(value if value is not None else ""):
            return True
    return False


def _fmt_name(person) -> str:
    return full_name(person)


def _fmt_date(row, prefix: str) -> str:
    row = dict(row)
    text = format_parts(row[f"{prefix}_day"], row[f"{prefix}_month"], row[f"{prefix}_year"],
                        row.get(f"{prefix}_qualifier", "exact"))
    return text or "—"


def _fmt_value(field: str, value, row=None) -> str:
    if field == "is_living":
        return "Living" if value else "Deceased"
    if field == "gender":
        return {"male": "Male", "female": "Female", "other": "Other"}.get(value or "other", "Other")
    if field == "contact_visibility":
        return {"all": "All members", "family": "Immediate family", "private": "Private"}.get(value, "All members")
    if field == "private_fields":
        try:
            groups = json.loads(value or "[]")
            return ", ".join(groups) if groups else "None"
        except (ValueError, TypeError):
            return "None"
    if field == "avatar_key":
        return "Photo attached" if value else "No photo"
    if value in (None, ""):
        return "—"
    return str(value)


def build_diff(conn: sqlite3.Connection, op: str, entity_id: str | None, payload: dict) -> list[dict]:
    rows: list[dict] = []
    if op == "person_create":
        temp = {
            "given_name": "", "middle_names": "", "surname": "", "maiden_name": "",
            "nickname": "", "gender": "other", "is_living": 1,
            "birth_day": None, "birth_month": None, "birth_year": None, "birth_qualifier": "exact",
            "death_day": None, "death_month": None, "death_year": None, "death_qualifier": "exact",
            "resting_place": "", "memorial_note": "", "notes": "", "email": "", "phone": "",
            "phone_alt": "", "address_line1": "", "address_line2": "", "city": "", "region": "",
            "postal_code": "", "country": "", "contact_visibility": "all", "private_fields": "[]",
            "avatar_key": "", "hidden_groups": [],
        }
        temp.update({k: v for k, v in payload.items() if not k.startswith("_")})
        rows.append({"group": "name", "label": "Name",
                     "old": "—", "new": f"{temp['given_name']} {temp['surname']}".strip() or "Unnamed",
                     "changed": True})
        if temp["middle_names"] or temp["maiden_name"] or temp["nickname"]:
            extras = ", ".join(x for x in [
                f"middle: {temp['middle_names']}" if temp["middle_names"] else "",
                f"maiden: {temp['maiden_name']}" if temp["maiden_name"] else "",
                f"nickname: {temp['nickname']}" if temp["nickname"] else "",
            ] if x)
            rows.append({"group": "name", "label": "Other names", "old": "—", "new": extras,
                         "changed": True})
        rows.append({"group": "identity", "label": "Gender", "old": "—",
                     "new": _fmt_value("gender", temp["gender"]), "changed": True})
        rows.append({"group": "identity", "label": "Living status", "old": "—",
                     "new": _fmt_value("is_living", temp["is_living"]), "changed": True})
        if any(temp.get(k) for k in ("birth_day", "birth_month", "birth_year")):
            rows.append({"group": "birth", "label": "Date of birth", "old": "—",
                         "new": _fmt_date(temp, "birth"), "changed": True})
        if not temp["is_living"] and any(temp.get(k) for k in ("death_day", "death_month", "death_year")):
            rows.append({"group": "death", "label": "Date of passing", "old": "—",
                         "new": _fmt_date(temp, "death"), "changed": True})
        for field in ("resting_place", "memorial_note", "email", "phone", "phone_alt",
                      "address_line1", "address_line2", "city", "region", "postal_code",
                      "country", "notes"):
            if temp.get(field):
                rows.append({"group": "detail", "label": FIELD_LABELS[field], "old": "—",
                             "new": _fmt_value(field, temp[field]), "changed": True})
        if payload.get("_pending_avatar"):
            rows.append({"group": "photo", "label": "Profile photo", "old": "—",
                         "new": "Photo attached", "changed": True})
        return rows

    if op == "person_update":
        person = get_person(conn, entity_id or "")
        if person is None:
            return [{"group": "error", "label": "Record", "old": "—", "new": "Record no longer exists",
                     "changed": True}]
        merged = dict(person)
        merged.update({k: v for k, v in payload.items() if not k.startswith("_")})
        merged["hidden_groups"] = []
        rows.append({"group": "name", "label": "Name",
                     "old": f"{person['given_name']} {person['surname']}".strip() or "Unnamed",
                     "new": f"{merged['given_name']} {merged['surname']}".strip() or "Unnamed",
                     "changed": (person["given_name"], person["surname"]) != (merged["given_name"], merged["surname"])})
        for field in ("middle_names", "maiden_name", "nickname"):
            if str(person[field] or "") != str(merged[field] or ""):
                rows.append({"group": "name", "label": FIELD_LABELS[field],
                             "old": _fmt_value(field, person[field]),
                             "new": _fmt_value(field, merged[field]), "changed": True})
        if person["gender"] != merged["gender"]:
            rows.append({"group": "identity", "label": "Gender", "old": _fmt_value("gender", person["gender"]),
                         "new": _fmt_value("gender", merged["gender"]), "changed": True})
        if bool(person["is_living"]) != bool(merged["is_living"]):
            rows.append({"group": "identity", "label": "Living status",
                         "old": _fmt_value("is_living", person["is_living"]),
                         "new": _fmt_value("is_living", merged["is_living"]), "changed": True})
        if any(str(person[k] or "") != str(merged[k] or "") for k in DATE_FIELDS if k.startswith("birth")):
            rows.append({"group": "birth", "label": "Date of birth", "old": _fmt_date(person, "birth"),
                         "new": _fmt_date(merged, "birth"), "changed": True})
        if any(str(person[k] or "") != str(merged[k] or "") for k in DATE_FIELDS if k.startswith("death")):
            rows.append({"group": "death", "label": "Date of passing", "old": _fmt_date(person, "death"),
                         "new": _fmt_date(merged, "death"), "changed": True})
        for field in ("resting_place", "memorial_note", "email", "phone", "phone_alt",
                      "address_line1", "address_line2", "city", "region", "postal_code",
                      "country", "notes", "contact_visibility", "private_fields"):
            if str(person[field] or "") != str(merged[field] or ""):
                rows.append({"group": "detail", "label": FIELD_LABELS.get(field, field),
                             "old": _fmt_value(field, person[field]),
                             "new": _fmt_value(field, merged[field]), "changed": True})
        if payload.get("_pending_avatar"):
            rows.append({"group": "photo", "label": "Profile photo", "old": "Current photo" if person["avatar_key"] else "—",
                         "new": "New photo", "changed": True})
        return [r for r in rows if r["changed"]]

    if op == "union_create":
        p1 = get_person(conn, payload.get("partner1_id", ""))
        p2 = get_person(conn, payload.get("partner2_id", "")) if payload.get("partner2_id") else None
        names = " and ".join(n for n in [_fmt_name(p1) if p1 else "Unknown",
                                        _fmt_name(p2) if p2 else ""] if n)
        return [{"group": "union", "label": "New family / partnership",
                 "old": "—", "new": names, "changed": True}]

    if op == "union_update":
        union = get_union(conn, entity_id or "")
        person = get_person(conn, payload.get("partner2_id", "")) if payload.get("partner2_id") else None
        p1 = get_person(conn, union["partner1_id"]) if union else None
        return [{"group": "union", "label": "Add partner",
                 "old": _fmt_name(p1) if p1 else "—",
                 "new": f"{_fmt_name(p1) if p1 else '—'} and {_fmt_name(person) if person else '—'}",
                 "changed": True}]

    if op == "union_child_add":
        child = get_person(conn, payload.get("child_id", ""))
        union = get_union(conn, payload.get("union_id", ""))
        parents = []
        if union:
            for pid in (union["partner1_id"], union["partner2_id"]):
                if pid:
                    parent = get_person(conn, pid)
                    if parent:
                        parents.append(_fmt_name(parent))
        return [{"group": "link", "label": "Relationship link",
                 "old": "—",
                 "new": f"{_fmt_name(child) if child else 'Unknown'} is a child of "
                        f"{' and '.join(parents) if parents else 'this family'}",
                 "changed": True}]
    return []


def summary_for(conn: sqlite3.Connection, items: list[dict]) -> str:
    parts = []
    for item in items:
        payload = item["payload"]
        if item["op"] == "person_create":
            name = f"{payload.get('given_name', '')} {payload.get('surname', '')}".strip() or "New person"
            parts.append(f"Add {name}")
        elif item["op"] == "person_update":
            person = get_person(conn, item.get("entity_id") or "")
            parts.append(f"Edit {_fmt_name(person) if person else 'record'}")
        elif item["op"] == "union_create":
            parts.append("New family")
        elif item["op"] == "union_update":
            parts.append("Add partner")
        elif item["op"] == "union_child_add":
            child = get_person(conn, payload.get("child_id", ""))
            parts.append(f"Link {_fmt_name(child) if child else 'child'}")
    return " · ".join(parts) or "Change request"


def submit(conn: sqlite3.Connection, user, title: str, items: list[dict], note: str = "") -> str:
    """Persist a pending change set. Items are already policy-checked by routes."""
    set_id = new_id()
    timestamp = now()
    conn.execute(
        "INSERT INTO change_sets(id, submitted_by, title, note, status, submitted_at) "
        "VALUES (?,?,?,?, 'pending', ?)",
        (set_id, user["id"], title[:200], note[:2000], timestamp),
    )
    for index, item in enumerate(items):
        payload = item["payload"]
        diff = build_diff(conn, item["op"], item.get("entity_id"), payload)
        conn.execute(
            "INSERT INTO change_items(id, set_id, op, entity_type, entity_id, base_version, "
            "payload_json, diff_json, sort_order) VALUES (?,?,?,?,?,?,?,?,?)",
            (new_id(), set_id, item["op"], item["entity_type"], item.get("entity_id"),
             item.get("base_version"), json.dumps(payload, ensure_ascii=False, default=str),
             json.dumps(diff, ensure_ascii=False, default=str), index),
        )
    record_audit(conn, user["id"], user["display_name"], "change_set.submit", "change_set",
                 set_id, None, {"title": title, "items": len(items)}, "", "")
    return set_id


def apply_items(conn: sqlite3.Connection, items: list[dict], settings: Settings,
                actor, strict_version: bool = True) -> None:
    """Apply ordered change items. Caller supplies the transaction."""
    for item in items:
        try:
            raw_payload = item["payload_json"]
        except (KeyError, IndexError, TypeError):
            raw_payload = None
        if raw_payload is not None:
            payload = _loads(raw_payload)
        else:
            payload = dict(item["payload"])
        op = item["op"]
        entity_id = item["entity_id"]
        pending_avatar = payload.pop("_pending_avatar", None)
        if op == "person_create":
            person_id = create_person(conn, payload, actor["id"], actor["display_name"],
                                      person_id=entity_id)
            if pending_avatar:
                new_key = media.commit_pending(settings, pending_avatar)
                update_person(conn, person_id, {"avatar_key": new_key, "avatar_updated_at": now()},
                              actor["id"], actor["display_name"])
        elif op == "person_update":
            person = get_person(conn, entity_id or "")
            if person is None:
                raise RelationshipError("The record being edited no longer exists.")
            if pending_avatar:
                new_key = media.commit_pending(settings, pending_avatar)
                payload["avatar_key"] = new_key
                payload["avatar_updated_at"] = now()
            try:
                base_version = item["base_version"]
            except (KeyError, IndexError, TypeError):
                base_version = None
            expected = base_version if strict_version else None
            update_person(conn, entity_id, payload, actor["id"], actor["display_name"],
                          expected_version=expected)
            if pending_avatar and person["avatar_key"]:
                media.delete_avatar(settings, person["avatar_key"])
        elif op == "union_create":
            create_union(conn, payload["partner1_id"], payload.get("partner2_id"),
                         payload.get("kind", "unknown"), payload.get("status", "unknown"),
                         actor["id"], actor["display_name"], union_id=entity_id)
        elif op == "union_update":
            set_partner(conn, entity_id, payload.get("partner2_id", ""),
                        actor["id"], actor["display_name"])
        elif op == "union_child_add":
            add_child(conn, payload["union_id"], payload["child_id"],
                      payload.get("relationship_type", "birth"),
                      actor["id"], actor["display_name"])
        else:
            raise RelationshipError(f"Unsupported change type: {op}")


def get_set(conn: sqlite3.Connection, set_id: str):
    return conn.execute("SELECT * FROM change_sets WHERE id = ?", (set_id,)).fetchone()


def set_items(conn: sqlite3.Connection, set_id: str) -> list:
    return conn.execute(
        "SELECT * FROM change_items WHERE set_id = ? ORDER BY sort_order", (set_id,)
    ).fetchall()


def item_payload(item) -> dict:
    return _loads(item["payload_json"])


def pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM change_sets WHERE status = 'pending'").fetchone()[0]


def approve(conn: sqlite3.Connection, set_id: str, admin, settings: Settings) -> None:
    change_set = get_set(conn, set_id)
    if change_set is None or change_set["status"] != "pending":
        raise RelationshipError("This change request is no longer pending.")
    items = set_items(conn, set_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        apply_items(conn, items, settings, admin, strict_version=True)
        conn.execute(
            "UPDATE change_sets SET status = 'approved', reviewed_by = ?, reviewed_at = ? WHERE id = ?",
            (admin["id"], now(), set_id),
        )
        record_audit(conn, admin["id"], admin["display_name"], "change_set.approve",
                     "change_set", set_id, None, {"title": change_set["title"]}, "", "")
        conn.execute("COMMIT")
    except Exception as exc:
        conn.execute("ROLLBACK")
        message = str(exc) or "This change could not be applied."
        conn.execute(
            "UPDATE change_sets SET status = 'needs_rebase', conflict_json = ? WHERE id = ?",
            (message[:1000], set_id),
        )
        raise RelationshipError(message) from exc


def reject(conn: sqlite3.Connection, set_id: str, admin, reason: str, settings: Settings) -> None:
    change_set = get_set(conn, set_id)
    if change_set is None or change_set["status"] not in ("pending", "needs_rebase"):
        raise RelationshipError("This change request cannot be rejected in its current state.")
    items = set_items(conn, set_id)
    for item in items:
        payload = item_payload(item)
        pending_avatar = payload.get("_pending_avatar")
        if pending_avatar:
            media.discard_pending(settings, pending_avatar)
    conn.execute(
        "UPDATE change_sets SET status = 'rejected', reviewed_by = ?, reviewed_at = ?, "
        "decision_reason = ? WHERE id = ?",
        (admin["id"], now(), reason[:1000], set_id),
    )
    record_audit(conn, admin["id"], admin["display_name"], "change_set.reject", "change_set",
                 set_id, None, {"reason": reason}, "", "")


def withdraw(conn: sqlite3.Connection, set_id: str, user, settings: Settings) -> None:
    change_set = get_set(conn, set_id)
    if change_set is None or change_set["status"] != "pending" or change_set["submitted_by"] != user["id"]:
        raise RelationshipError("You cannot withdraw this request.")
    for item in set_items(conn, set_id):
        pending_avatar = item_payload(item).get("_pending_avatar")
        if pending_avatar:
            media.discard_pending(settings, pending_avatar)
    conn.execute(
        "UPDATE change_sets SET status = 'withdrawn', reviewed_at = ? WHERE id = ?",
        (now(), set_id),
    )
