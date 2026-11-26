"""Object-level authorization and privacy masking."""
from __future__ import annotations

import json

from .relationships import immediate_family_ids
from .util import FIELD_GROUP, PRIVACY_GROUPS


def is_admin(user) -> bool:
    return bool(user) and user["role"] == "admin" and user["status"] == "active"


def can_direct_edit(user) -> bool:
    return is_admin(user)


def can_submit_for(conn, user, person_id: str) -> bool:
    if not user or user["status"] != "active":
        return False
    if is_admin(user):
        return True
    if not user["person_id"]:
        return False
    return person_id in immediate_family_ids(conn, user["person_id"])


def can_manage_privacy(user, person_id: str) -> bool:
    if not user:
        return False
    return is_admin(user) or (user["person_id"] == person_id)


def hidden_groups(conn, person, viewer) -> set[str]:
    """Privacy groups that must not be sent to this viewer."""
    if viewer is None:
        return set(PRIVACY_GROUPS)
    if viewer["role"] == "admin" and viewer["status"] == "active":
        return set()
    if viewer["person_id"] and viewer["person_id"] == person["id"]:
        return set()
    hidden: set[str] = set()
    try:
        raw = json.loads(person["private_fields"] or "[]")
        if isinstance(raw, list):
            hidden = {g for g in raw if g in PRIVACY_GROUPS}
    except (ValueError, TypeError):
        hidden = set()
    visibility = person["contact_visibility"]
    if visibility == "private":
        hidden.add("contact")
    elif visibility == "family":
        viewer_person = viewer["person_id"]
        if not viewer_person or viewer_person not in immediate_family_ids(conn, person["id"]):
            hidden.add("contact")
    return hidden


def person_view(conn, person, viewer) -> dict:
    """Return a copy of a person row with restricted fields blanked out."""
    hidden = hidden_groups(conn, person, viewer)
    view = dict(person)
    for field, group in FIELD_GROUP.items():
        if group in hidden and field in view:
            if field.endswith("_day") or field.endswith("_month") or field.endswith("_year"):
                view[field] = None
            else:
                view[field] = ""
    view["hidden_groups"] = sorted(hidden)
    view["avatar_visible"] = "photo" not in hidden and bool(person["avatar_key"])
    view["is_self"] = bool(viewer and viewer["person_id"] == person["id"])
    return view


def group_visible(conn, person, viewer, group: str) -> bool:
    return group not in hidden_groups(conn, person, viewer)


def can_edit_group(conn, person, viewer, group: str) -> bool:
    """Only the person themselves (or an admin) may edit a private group."""
    if viewer is None:
        return False
    if is_admin(viewer):
        return True
    if viewer["person_id"] != person["id"]:
        return False
    return group not in hidden_groups(conn, person, viewer)


def editable_groups(conn, person, viewer) -> set[str]:
    """Groups a viewer may see and propose changes for on this person."""
    if viewer is None:
        return set()
    hidden = hidden_groups(conn, person, viewer)
    return set(PRIVACY_GROUPS) - hidden


def pending_entity_ids(conn, user) -> set[str]:
    if not user:
        return set()
    rows = conn.execute(
        "SELECT DISTINCT ci.entity_id FROM change_items ci "
        "JOIN change_sets cs ON cs.id = ci.set_id "
        "WHERE cs.submitted_by = ? AND cs.status = 'pending' "
        "AND ci.entity_type = 'person' AND ci.entity_id IS NOT NULL",
        (user["id"],),
    ).fetchall()
    return {r["entity_id"] for r in rows}
