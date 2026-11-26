"""Admin operations: export/import, backups, avatars, roles and settings."""
from __future__ import annotations

import io
import json
import re
import sqlite3

from PIL import Image

from conftest import (add_relative, add_standalone, create_invite, csrf, login, redeem_invite,
                      start_setup, tree)


def _db(client):
    return sqlite3.connect(client.app.state.settings.db_path)


def _token_for(client, path):
    page = client.get(path).text
    return re.search(r'name="_csrf" value="([^"]+)"', page).group(1), page


def test_export_json_and_sqlite_roundtrip(admin):
    people = tree(admin)["persons"]
    export = admin.get("/admin/export/json")
    assert export.status_code == 200
    payload = json.loads(export.text)
    assert payload["format"] == "familytree.v1"
    assert len(payload["persons"]) == len(people)
    assert payload["root_union_id"]

    # JSON import replaces the tree with the same data.
    token, _ = _token_for(admin, "/admin/backups")
    response = admin.post("/admin/backups/import", data={
        "_csrf": token, "confirm": "REPLACE",
    }, files={"file": ("export.json", json.dumps(payload).encode(), "application/json")})
    assert response.status_code == 303
    assert len(tree(admin)["persons"]) == len(people)

    # SQLite snapshot is a real database file.
    snapshot = admin.get("/admin/export/sqlite")
    assert snapshot.status_code == 200
    assert snapshot.content[:16] == b"SQLite format 3\x00"


def test_import_rejects_bad_file_without_destroying_data(admin):
    before = len(tree(admin)["persons"])
    token, _ = _token_for(admin, "/admin/backups")
    response = admin.post("/admin/backups/import", data={
        "_csrf": token, "confirm": "REPLACE",
    }, files={"file": ("bad.json", b"{not json", "application/json")})
    assert response.status_code == 303
    assert len(tree(admin)["persons"]) == before

    # Wrong confirmation phrase is refused before reading the file.
    token, _ = _token_for(admin, "/admin/backups")
    response = admin.post("/admin/backups/import", data={
        "_csrf": token, "confirm": "no",
    }, files={"file": ("export.json", b"{}", "application/json")})
    assert response.status_code == 303
    assert len(tree(admin)["persons"]) == before


def test_backup_creation_and_download(admin):
    token, page = _token_for(admin, "/admin/backups")
    response = admin.post("/admin/backups/create", data={"_csrf": token})
    assert response.status_code == 303
    page = admin.get("/admin/backups").text
    match = re.search(r"<code>(family-[^<]+\.db)</code>", page)
    assert match, page[:1000]
    download = admin.get(f"/admin/backups/{match.group(1)}/download")
    assert download.status_code == 200
    assert download.content[:16] == b"SQLite format 3\x00"


def test_avatar_upload_served_and_can_be_private(admin):
    target = add_standalone(admin, "Photo", "Person", gender="other")
    image = Image.new("RGB", (600, 600), (90, 120, 140))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    image_bytes = buffer.getvalue()

    token, page = _token_for(admin, f"/persons/{target}/edit")
    version = re.search(r'name="_version" value="([^"]+)"', page).group(1)
    response = admin.post(f"/persons/{target}/edit", data={
        "_csrf": token, "_version": version, "action": "direct",
    }, files={"avatar": ("photo.png", image_bytes, "image/png")})
    assert response.status_code == 303
    thumb = admin.get(f"/media/avatar/{target}?size=thumb")
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/webp"

    # Make the photo private, then check another account cannot fetch it.
    token, page = _token_for(admin, f"/persons/{target}/edit")
    version = re.search(r'name="_version" value="([^"]+)"', page).group(1)
    response = admin.post(f"/persons/{target}/edit", data={
        "_csrf": token, "_version": version, "action": "direct",
        "_privacy_present": "1", "contact_visibility": "all",
        "private_fields": ["photo"],
    })
    assert response.status_code == 303

    outsider = add_standalone(admin, "NoPhoto", "Person")
    link = create_invite(admin, outsider)
    token, _ = _token_for(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    redeem_invite(admin, link, "Outsider", "outsider@example.com", "outsider-password-123")
    assert admin.get(f"/media/avatar/{target}").status_code == 404
    card = admin.get(f"/api/persons/{target}/card").text
    assert "/media/avatar/" not in card


def test_last_admin_cannot_be_demoted_or_disabled(admin):
    with _db(admin) as conn:
        admin_id = conn.execute("SELECT id FROM users").fetchone()[0]
    token, _ = _token_for(admin, "/admin/users")
    response = admin.post(f"/admin/users/{admin_id}/role", data={"_csrf": token, "role": "user"})
    assert response.status_code == 400
    response = admin.post(f"/admin/users/{admin_id}/status", data={"_csrf": token, "status": "disabled"})
    assert response.status_code == 400


def test_password_reset_revokes_old_session(admin):
    user = add_standalone(admin, "Reset", "Me")
    link = create_invite(admin, user)
    token, _ = _token_for(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    redeem_invite(admin, link, "Reset Me", "reset@example.com", "reset-password-123")
    # User is now signed in; capture their session cookie.
    user_cookie = admin.cookies.get("fam_session")
    assert user_cookie

    token, _ = _token_for(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    login(admin)
    with _db(admin) as conn:
        user_id = conn.execute("SELECT id FROM users WHERE email_norm = ?", ("reset@example.com",)).fetchone()[0]
    token, _ = _token_for(admin, "/admin/users")
    response = admin.post(f"/admin/users/{user_id}/reset-password", data={"_csrf": token})
    assert response.status_code == 200
    match = re.search(r'temp-password">([^<]+)</code>', response.text)
    assert match
    temporary = match.group(1).strip()

    old = admin.get("/api/tree", headers={"Cookie": f"fam_session={user_cookie}"})
    assert old.status_code == 401

    token, _ = _token_for(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    response = login(admin, "reset@example.com", temporary)
    assert response.status_code == 303


def test_invite_expiry_blocks_redemption(admin):
    link = create_invite(admin, "")
    with _db(admin) as conn:
        conn.execute("UPDATE invites SET expires_at = 0")
    path = "/invite/" + link.rsplit("/invite/", 1)[1]
    assert admin.get(path).status_code == 404


def test_settings_mode_and_passcode_registration(admin):
    token, _ = _token_for(admin, "/admin/settings")
    response = admin.post("/admin/settings/general", data={
        "_csrf": token, "site_title": "Iyer Family", "registration_mode": "passcode",
    })
    assert response.status_code == 303
    token, _ = _token_for(admin, "/admin/settings")
    response = admin.post("/admin/settings/passcode", data={"_csrf": token, "passcode": "family-pass-123"})
    assert response.status_code == 303

    token, _ = _token_for(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    page = admin.get("/register")
    assert page.status_code == 200
    assert "Family passcode" in page.text
    register_token = re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)
    response = admin.post("/register", data={
        "_csrf": register_token, "passcode": "wrong-pass", "display_name": "Cousin",
        "email": "cousin@example.com", "password": "cousin-password-123",
        "confirm": "cousin-password-123",
    })
    assert response.status_code == 400
    assert "passcode is not correct" in response.text


def test_founding_couple_setting(admin):
    token, page = _token_for(admin, "/admin/settings")
    response = admin.post("/admin/settings/root", data={
        "_csrf": token, "partner1_id": "", "partner2_id": "",
    })
    assert response.status_code == 303
    with _db(admin) as conn:
        assert conn.execute("SELECT value FROM app_settings WHERE key='root_union_id'").fetchone()[0] == ""


def test_invalid_image_upload_is_rejected(admin):
    target = add_standalone(admin, "Bad", "Photo")
    token, page = _token_for(admin, f"/persons/{target}/edit")
    version = re.search(r'name="_version" value="([^"]+)"', page).group(1)
    response = admin.post(f"/persons/{target}/edit", data={
        "_csrf": token, "_version": version, "action": "direct",
    }, files={"avatar": ("fake.png", b"not really a png", "image/png")})
    assert response.status_code == 400
    assert "unreadable" in response.text or "not a valid image" in response.text


def test_founding_couple_attaches_to_existing_single_parent_union(admin):
    one = add_standalone(admin, "Founder", "One")
    two = add_standalone(admin, "Founder", "Two")
    token, _ = _token_for(admin, "/admin/settings")
    admin.post("/admin/settings/root", data={"_csrf": token, "partner1_id": one, "partner2_id": ""})
    token, _ = _token_for(admin, "/admin/settings")
    admin.post("/admin/settings/root", data={"_csrf": token, "partner1_id": one, "partner2_id": two})
    with _db(admin) as conn:
        row = conn.execute(
            "SELECT partner1_id, partner2_id FROM unions WHERE partner1_id = ? AND partner2_id = ?",
            (one, two),
        ).fetchone()
    assert row is not None


def test_xss_is_escaped_in_person_card(admin):
    target = add_standalone(admin, "<script>alert(1)</script>", "Test")
    card = admin.get(f"/api/persons/{target}/card").text
    assert "<script>alert(1)</script>" not in card
    assert "&lt;script&gt;" in card
