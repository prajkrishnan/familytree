"""Union-centric relationship engine: the only place graph edges are written."""
from __future__ import annotations

import json
from sqlite3 import Connection

from .util import full_name, now, search_text


class RelationshipError(ValueError):
    pass


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

def get_person(conn: Connection, person_id: str, include_deleted: bool = False):
    sql = "SELECT * FROM persons WHERE id = ?"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    return conn.execute(sql, (person_id,)).fetchone()


def get_union(conn: Connection, union_id: str, include_deleted: bool = False):
    sql = "SELECT * FROM unions WHERE id = ?"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    return conn.execute(sql, (union_id,)).fetchone()


def partners_of(union) -> list[str]:
    return [p for p in (union["partner1_id"], union["partner2_id"]) if p]


def unions_for_person(conn: Connection, person_id: str) -> list:
    return conn.execute(
        "SELECT * FROM unions WHERE deleted_at IS NULL AND (partner1_id = ? OR partner2_id = ?) "
        "ORDER BY COALESCE(start_year, 9999), created_at",
        (person_id, person_id),
    ).fetchall()


def birth_union_of(conn: Connection, person_id: str):
    return conn.execute(
        "SELECT u.* FROM unions u JOIN union_children uc ON uc.union_id = u.id "
        "WHERE uc.child_id = ? AND uc.relationship_type = 'birth' AND u.deleted_at IS NULL "
        "ORDER BY u.created_at LIMIT 1",
        (person_id,),
    ).fetchone()


def children_of_union(conn: Connection, union_id: str) -> list:
    return conn.execute(
        "SELECT p.*, uc.relationship_type AS link_type FROM union_children uc "
        "JOIN persons p ON p.id = uc.child_id "
        "WHERE uc.union_id = ? AND p.deleted_at IS NULL "
        "ORDER BY uc.sort_order, COALESCE(p.birth_year, 9999), p.given_name",
        (union_id,),
    ).fetchall()


def parent_links(conn: Connection, person_id: str) -> list:
    return conn.execute(
        "SELECT p.*, pc.relationship_type AS link_type, pc.union_id FROM parent_child pc "
        "JOIN persons p ON p.id = pc.parent_id "
        "WHERE pc.child_id = ? AND p.deleted_at IS NULL "
        "ORDER BY p.gender DESC, pc.relationship_type",
        (person_id,),
    ).fetchall()


def child_links(conn: Connection, person_id: str) -> list:
    return conn.execute(
        "SELECT DISTINCT p.*, pc.relationship_type AS link_type FROM parent_child pc "
        "JOIN persons p ON p.id = pc.child_id "
        "WHERE pc.parent_id = ? AND p.deleted_at IS NULL "
        "ORDER BY COALESCE(p.birth_year, 9999), p.given_name",
        (person_id,),
    ).fetchall()


def sibling_links(conn: Connection, person_id: str) -> list:
    rows = conn.execute(
        "SELECT DISTINCT p.* FROM parent_child mine "
        "JOIN parent_child other ON other.parent_id = mine.parent_id "
        "JOIN persons p ON p.id = other.child_id "
        "WHERE mine.child_id = ? AND other.child_id <> ? AND p.deleted_at IS NULL "
        "ORDER BY COALESCE(p.birth_year, 9999), p.given_name",
        (person_id, person_id),
    ).fetchall()
    seen, out = {person_id}, []
    for row in rows:
        if row["id"] not in seen:
            seen.add(row["id"])
            out.append(row)
    return out


def spouse_links(conn: Connection, person_id: str) -> list:
    unions = unions_for_person(conn, person_id)
    out = []
    seen = set()
    for union in unions:
        for partner_id in partners_of(union):
            if partner_id == person_id or partner_id in seen:
                continue
            person = get_person(conn, partner_id)
            if person is None:
                continue
            seen.add(partner_id)
            out.append((person, union))
    return out


# --------------------------------------------------------------------------
# Invariants
# --------------------------------------------------------------------------

def is_descendant(conn: Connection, ancestor_id: str, maybe_descendant_id: str) -> bool:
    row = conn.execute(
        "WITH RECURSIVE descendants(id) AS ("
        "  SELECT ?"
        "  UNION"
        "  SELECT pc.child_id FROM parent_child pc JOIN descendants d ON pc.parent_id = d.id"
        ") SELECT 1 FROM descendants WHERE id = ? LIMIT 1",
        (ancestor_id, maybe_descendant_id),
    ).fetchone()
    return row is not None


def assert_can_be_parent(conn: Connection, parent_id: str, child_id: str) -> None:
    if parent_id == child_id:
        raise RelationshipError("A person cannot be their own parent.")
    if is_descendant(conn, child_id, parent_id):
        raise RelationshipError(
            "That link would create a loop: this person is already a descendant of the child."
        )


def rebuild_parent_child(conn: Connection, union_id: str) -> None:
    conn.execute("DELETE FROM parent_child WHERE union_id = ?", (union_id,))
    union = conn.execute("SELECT * FROM unions WHERE id = ?", (union_id,)).fetchone()
    if union is None or union["deleted_at"] is not None:
        return
    for partner_id in partners_of(union):
        conn.execute(
            "INSERT OR IGNORE INTO parent_child(parent_id, child_id, union_id, relationship_type) "
            "SELECT ?, uc.child_id, ?, uc.relationship_type FROM union_children uc "
            "WHERE uc.union_id = ? AND uc.child_id <> ?",
            (partner_id, union_id, union_id, partner_id),
        )


def rebuild_all_parent_child(conn: Connection) -> None:
    conn.execute("DELETE FROM parent_child")
    conn.execute(
        "INSERT OR IGNORE INTO parent_child(parent_id, child_id, union_id, relationship_type) "
        "SELECT u.partner1_id, uc.child_id, u.id, uc.relationship_type "
        "FROM unions u JOIN union_children uc ON uc.union_id = u.id "
        "WHERE u.deleted_at IS NULL AND uc.child_id <> u.partner1_id"
    )
    conn.execute(
        "INSERT OR IGNORE INTO parent_child(parent_id, child_id, union_id, relationship_type) "
        "SELECT u.partner2_id, uc.child_id, u.id, uc.relationship_type "
        "FROM unions u JOIN union_children uc ON uc.union_id = u.id "
        "WHERE u.deleted_at IS NULL AND u.partner2_id IS NOT NULL "
        "AND u.partner2_id <> '' AND uc.child_id <> u.partner2_id"
    )


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

def create_person(conn: Connection, data: dict, actor_id: str, actor_name: str = "",
                  ip: str = "", user_agent: str = "", person_id: str | None = None) -> str:
    from .util import new_id

    person_id = person_id or new_id()
    timestamp = now()
    fields = {
        "given_name": "", "middle_names": "", "surname": "", "maiden_name": "", "nickname": "",
        "gender": "other", "is_living": 1,
        "birth_day": None, "birth_month": None, "birth_year": None, "birth_qualifier": "exact",
        "death_day": None, "death_month": None, "death_year": None, "death_qualifier": "exact",
        "resting_place": "", "memorial_note": "", "notes": "",
        "email": "", "phone": "", "phone_alt": "",
        "address_line1": "", "address_line2": "", "city": "", "region": "",
        "postal_code": "", "country": "",
        "avatar_key": None, "avatar_updated_at": None,
        "contact_visibility": "all", "private_fields": "[]",
    }
    fields.update({k: v for k, v in data.items() if k in fields})
    fields["search_text"] = search_text(fields)
    columns = list(fields.keys()) + ["id", "version", "created_by", "updated_by",
                                     "created_at", "updated_at"]
    values = list(fields.values()) + [person_id, 1, actor_id, actor_id, timestamp, timestamp]
    placeholders = ",".join("?" for _ in columns)
    conn.execute(f"INSERT INTO persons({','.join(columns)}) VALUES ({placeholders})", values)
    record_audit(conn, actor_id, actor_name, "person.create", "person", person_id,
                 None, fields, ip, user_agent)
    return person_id


def update_person(conn: Connection, person_id: str, data: dict, actor_id: str,
                  actor_name: str = "", ip: str = "", user_agent: str = "",
                  expected_version: int | None = None) -> int:
    person = get_person(conn, person_id)
    if person is None:
        raise RelationshipError("Person not found.")
    if expected_version is not None and person["version"] != expected_version:
        raise RelationshipError(
            "This record changed after the submission was created. It needs review."
        )
    allowed = {
        "given_name", "middle_names", "surname", "maiden_name", "nickname", "gender",
        "is_living", "birth_day", "birth_month", "birth_year", "birth_qualifier",
        "death_day", "death_month", "death_year", "death_qualifier",
        "resting_place", "memorial_note", "notes", "email", "phone", "phone_alt",
        "address_line1", "address_line2", "city", "region", "postal_code", "country",
        "avatar_key", "avatar_updated_at", "contact_visibility", "private_fields",
    }
    changes = {k: v for k, v in data.items() if k in allowed}
    if not changes:
        return person["version"]
    if "given_name" in changes or "surname" in changes or "nickname" in changes \
            or "middle_names" in changes or "maiden_name" in changes:
        merged = dict(person)
        merged.update(changes)
        changes["search_text"] = search_text(merged)
    changes["updated_at"] = now()
    changes["updated_by"] = actor_id
    new_version = person["version"] + 1
    changes["version"] = new_version
    assignments = ", ".join(f"{key} = ?" for key in changes)
    conn.execute(f"UPDATE persons SET {assignments} WHERE id = ?",
                 list(changes.values()) + [person_id])
    before = {k: person[k] for k in changes if k in person.keys()}
    record_audit(conn, actor_id, actor_name, "person.update", "person", person_id,
                 before, changes, ip, user_agent)
    return new_version


def create_union(conn: Connection, partner1_id: str, partner2_id: str | None,
                 kind: str = "unknown", status: str = "unknown",
                 actor_id: str = "", actor_name: str = "",
                 start: dict | None = None, end: dict | None = None,
                 union_id: str | None = None) -> str:
    from .util import new_id

    if not get_person(conn, partner1_id):
        raise RelationshipError("Partner not found.")
    if partner2_id:
        if partner2_id == partner1_id:
            raise RelationshipError("A person cannot be partnered with themselves.")
        if not get_person(conn, partner2_id):
            raise RelationshipError("Partner not found.")
    if kind not in ("marriage", "civil_union", "partnership", "unknown"):
        kind = "unknown"
    if status not in ("ongoing", "divorced", "widowed", "separated", "unknown"):
        status = "unknown"
    union_id = union_id or new_id()
    start = start or {}
    end = end or {}
    timestamp = now()
    conn.execute(
        "INSERT INTO unions(id, partner1_id, partner2_id, kind, status, "
        "start_day, start_month, start_year, start_qualifier, "
        "end_day, end_month, end_year, end_qualifier, notes, created_by, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'',?,?,?)",
        (union_id, partner1_id, partner2_id, kind, status,
         start.get("day"), start.get("month"), start.get("year"), start.get("qualifier", "exact"),
         end.get("day"), end.get("month"), end.get("year"), end.get("qualifier", "exact"),
         actor_id, timestamp, timestamp),
    )
    record_audit(conn, actor_id, actor_name, "union.create", "union", union_id, None,
                 {"partner1_id": partner1_id, "partner2_id": partner2_id, "kind": kind},
                 "", "")
    return union_id


def add_child(conn: Connection, union_id: str, child_id: str, relationship_type: str = "birth",
              actor_id: str = "", actor_name: str = "") -> None:
    union = get_union(conn, union_id)
    child = get_person(conn, child_id)
    if union is None:
        raise RelationshipError("Family not found.")
    if child is None:
        raise RelationshipError("Person not found.")
    if relationship_type not in ("birth", "adopted", "step", "foster"):
        relationship_type = "birth"
    if child_id in partners_of(union):
        raise RelationshipError("A partner cannot also be a child of the same family.")
    if relationship_type == "birth":
        existing = conn.execute(
            "SELECT union_id FROM union_children WHERE child_id = ? AND relationship_type = 'birth'",
            (child_id,),
        ).fetchone()
        if existing and existing["union_id"] != union_id:
            raise RelationshipError("This person already has birth parents recorded.")
    for parent_id in partners_of(union):
        assert_can_be_parent(conn, parent_id, child_id)
    already = conn.execute(
        "SELECT 1 FROM union_children WHERE union_id = ? AND child_id = ?", (union_id, child_id)
    ).fetchone()
    if already:
        raise RelationshipError("This person is already a child in that family.")
    conn.execute(
        "INSERT INTO union_children(union_id, child_id, relationship_type, sort_order, created_at) "
        "VALUES (?,?,?,?,?)",
        (union_id, child_id, relationship_type, 0, now()),
    )
    rebuild_parent_child(conn, union_id)
    record_audit(conn, actor_id, actor_name, "union.child.add", "union", union_id, None,
                 {"child_id": child_id, "relationship_type": relationship_type}, "", "")


def set_partner(conn: Connection, union_id: str, partner2_id: str,
                actor_id: str = "", actor_name: str = "") -> None:
    union = get_union(conn, union_id)
    if union is None:
        raise RelationshipError("Family not found.")
    if partner2_id and partner2_id == union["partner1_id"]:
        raise RelationshipError("A person cannot be partnered with themselves.")
    if partner2_id and not get_person(conn, partner2_id):
        raise RelationshipError("Person not found.")
    if partner2_id:
        already_child = conn.execute(
            "SELECT 1 FROM union_children WHERE union_id = ? AND child_id = ?",
            (union_id, partner2_id),
        ).fetchone()
        if already_child:
            raise RelationshipError("A child of this family cannot also be its partner.")
        for row in conn.execute(
            "SELECT child_id FROM union_children WHERE union_id = ?", (union_id,)
        ).fetchall():
            assert_can_be_parent(conn, partner2_id, row["child_id"])
    conn.execute("UPDATE unions SET partner2_id = ?, updated_at = ? WHERE id = ?",
                 (partner2_id or None, now(), union_id))
    rebuild_parent_child(conn, union_id)
    record_audit(conn, actor_id, actor_name, "union.update", "union", union_id,
                 {"partner2_id": union["partner2_id"]}, {"partner2_id": partner2_id}, "", "")


def delete_person(conn: Connection, person_id: str, actor_id: str, actor_name: str = "",
                  ip: str = "", user_agent: str = "") -> list[str]:
    """Soft-delete a person and detach them. Returns avatar keys to remove."""
    person = get_person(conn, person_id)
    if person is None:
        raise RelationshipError("Person not found.")
    old_avatar = person["avatar_key"]
    unions = conn.execute(
        "SELECT * FROM unions WHERE partner1_id = ? OR partner2_id = ?", (person_id, person_id)
    ).fetchall()
    conn.execute("DELETE FROM union_children WHERE child_id = ?", (person_id,))
    conn.execute("DELETE FROM parent_child WHERE child_id = ?", (person_id,))
    for union in unions:
        other = (union["partner2_id"] if union["partner1_id"] == person_id
                 else union["partner1_id"])
        if other:
            # Keep the surviving partner's link to the children.
            conn.execute(
                "UPDATE unions SET partner1_id = ?, partner2_id = NULL, updated_at = ? WHERE id = ?",
                (other, now(), union["id"]),
            )
            conn.execute("DELETE FROM parent_child WHERE parent_id = ?", (person_id,))
            rebuild_parent_child(conn, union["id"])
        else:
            conn.execute("DELETE FROM parent_child WHERE union_id = ?", (union["id"],))
            conn.execute("DELETE FROM union_children WHERE union_id = ?", (union["id"],))
            conn.execute("DELETE FROM unions WHERE id = ?", (union["id"],))
    conn.execute(
        "UPDATE persons SET deleted_at = ?, version = version + 1 WHERE id = ?",
        (now(), person_id),
    )
    conn.execute("UPDATE users SET person_id = NULL WHERE person_id = ?", (person_id,))
    conn.execute("UPDATE invites SET person_id = NULL WHERE person_id = ?", (person_id,))
    record_audit(conn, actor_id, actor_name, "person.delete", "person", person_id,
                 {"name": full_name(person)}, None, ip, user_agent)
    return [old_avatar] if old_avatar else []


def record_audit(conn: Connection, actor_id: str | None, actor_name: str, action: str,
                 entity_type: str, entity_id: str, before: dict | None, after: dict | None,
                 ip: str = "", user_agent: str = "") -> None:
    from .util import new_id

    def dump(value):
        if value is None:
            return ""
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return "{}"

    conn.execute(
        "INSERT INTO audit_events(id, actor_id, actor_name, action, entity_type, entity_id, "
        "before_json, after_json, ip, user_agent, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (new_id(), actor_id or "", actor_name or "", action, entity_type, entity_id or "",
         dump(before), dump(after), ip[:64], user_agent[:300], now()),
    )


# --------------------------------------------------------------------------
# Scope: who is "immediate family" for a given person
# --------------------------------------------------------------------------

def immediate_family_ids(conn: Connection, person_id: str) -> set[str]:
    ids: set[str] = {person_id}
    parents = {r["parent_id"] for r in conn.execute(
        "SELECT DISTINCT parent_id FROM parent_child WHERE child_id = ?", (person_id,))}
    children = {r["child_id"] for r in conn.execute(
        "SELECT DISTINCT child_id FROM parent_child WHERE parent_id = ?", (person_id,))}
    ids |= parents | children

    my_unions = unions_for_person(conn, person_id)
    birth_union_ids = {r["union_id"] for r in conn.execute(
        "SELECT union_id FROM union_children WHERE child_id = ?", (person_id,))}
    birth_unions = []
    for union_id in birth_union_ids:
        union = get_union(conn, union_id)
        if union:
            birth_unions.append(union)
            ids |= set(partners_of(union))
            ids |= {r["child_id"] for r in conn.execute(
                "SELECT child_id FROM union_children WHERE union_id = ?", (union_id,))}

    for union in my_unions:
        ids |= set(partners_of(union))
        ids |= {r["child_id"] for r in conn.execute(
            "SELECT child_id FROM union_children WHERE union_id = ?", (union["id"],))}

    # Step-parents and step-siblings: other unions of a parent.
    for parent_id in parents:
        for union in unions_for_person(conn, parent_id):
            if union["id"] in birth_union_ids:
                continue
            ids |= set(partners_of(union))
            ids |= {r["child_id"] for r in conn.execute(
                "SELECT child_id FROM union_children WHERE union_id = ?", (union["id"],))}

    # Step-children: children of a spouse in another union.
    for union in my_unions:
        for partner_id in partners_of(union):
            if partner_id == person_id:
                continue
            for other in unions_for_person(conn, partner_id):
                if other["id"] == union["id"]:
                    continue
                ids |= {r["child_id"] for r in conn.execute(
                    "SELECT child_id FROM union_children WHERE union_id = ?", (other["id"],))}

    ids.discard("")
    return ids


def graph_snapshot(conn: Connection) -> dict:
    persons = [dict(r) for r in conn.execute(
        "SELECT * FROM persons WHERE deleted_at IS NULL").fetchall()]
    unions = [dict(r) for r in conn.execute(
        "SELECT * FROM unions WHERE deleted_at IS NULL").fetchall()]
    children = [dict(r) for r in conn.execute(
        "SELECT * FROM union_children").fetchall()]
    return {"persons": persons, "unions": unions, "union_children": children}
