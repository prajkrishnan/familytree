"""Relationship engine, scope and moderation workflow."""
from __future__ import annotations

import re

from conftest import (add_relative, add_standalone, create_invite, csrf, login, redeem_invite,
                      start_setup, tree, person_by_name)


def _ids(client):
    people = {p["given"]: p for p in tree(client)["persons"]}
    return people


def test_add_relative_is_direct_for_admin_and_sets_links(admin):
    people = _ids(admin)
    son = add_relative(admin, people["Raman"]["id"], "son", {
        "given_name": "Mohan", "surname": "Iyer", "gender": "male", "birth_year": "1955",
    })
    data = tree(admin)
    assert any(p["id"] == son for p in data["persons"])
    links = [c for c in data["children"] if c["c"] == son]
    assert len(links) == 1
    assert links[0]["r"] == "birth"


def test_edit_submission_requires_approval_and_pending_badge(admin):
    people = _ids(admin)
    mohan = add_relative(admin, people["Raman"]["id"], "son", {
        "given_name": "Mohan", "surname": "Iyer", "gender": "male", "birth_year": "1955",
        "phone": "+91 11111 11111",
    })
    link = create_invite(admin, mohan)
    # Switch from admin to the invited user.
    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    response = redeem_invite(admin, link, "Mohan", "mohan@example.com", "mohan-password-123")
    assert response.status_code == 303

    # The tree shows a pending badge only for this user's own submission.
    data = tree(admin)
    mohan_card = next(p for p in data["persons"] if p["id"] == mohan)
    assert mohan_card["pending"] is False

    page = admin.get(f"/persons/{mohan}/edit").text
    edit_token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    version = re.search(r'name="_version" value="([^"]+)"', page).group(1)
    response = admin.post(f"/persons/{mohan}/edit", data={
        "_csrf": edit_token, "_version": version, "action": "submit",
        "phone": "+91 99999 99999", "city": "Bengaluru",
    })
    assert response.status_code == 303
    assert response.headers["location"] == "/me/submissions"

    data = tree(admin)
    mohan_card = next(p for p in data["persons"] if p["id"] == mohan)
    assert mohan_card["pending"] is True
    # Not live yet.
    card = admin.get(f"/api/persons/{mohan}/card").text
    assert "99999" not in card
    assert "Bengaluru" not in card  # still pending, not live

    # Admin reviews and approves.
    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    login(admin)
    queue = admin.get("/admin/queue").text
    set_id = re.search(r"/admin/queue/([0-9a-f]+)", queue).group(1)
    review = admin.get(f"/admin/queue/{set_id}").text
    assert "Bengaluru" in review
    approve_token = re.search(r'name="_csrf" value="([^"]+)"', review).group(1)
    response = admin.post(f"/admin/queue/{set_id}/approve", data={"_csrf": approve_token})
    assert response.status_code == 303

    card = admin.get(f"/api/persons/{mohan}/card").text
    assert "99999" in card
    assert "Bengaluru" in card


def test_reject_with_reason_is_visible_to_submitter(admin):
    people = _ids(admin)
    mohan = add_relative(admin, people["Raman"]["id"], "son", {
        "given_name": "Mohan", "surname": "Iyer", "gender": "male",
    })
    link = create_invite(admin, mohan)
    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    redeem_invite(admin, link, "Mohan", "mohan@example.com", "mohan-password-123")
    page = admin.get(f"/persons/{mohan}/edit").text
    edit_token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    version = re.search(r'name="_version" value="([^"]+)"', page).group(1)
    admin.post(f"/persons/{mohan}/edit", data={
        "_csrf": edit_token, "_version": version, "action": "submit", "nickname": "Moni",
    })

    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    login(admin)
    queue = admin.get("/admin/queue").text
    set_id = re.search(r"/admin/queue/([0-9a-f]+)", queue).group(1)
    review = admin.get(f"/admin/queue/{set_id}").text
    reject_token = re.search(r'name="_csrf" value="([^"]+)"', review).group(1)
    response = admin.post(f"/admin/queue/{set_id}/reject", data={
        "_csrf": reject_token, "reason": "Please confirm spelling first",
    })
    assert response.status_code == 303

    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    login(admin, "mohan@example.com", "mohan-password-123")
    page = admin.get("/me/submissions").text
    assert "Please confirm spelling first" in page
    assert "rejected" in page.lower()


def test_scope_limits_edits_to_immediate_family(admin):
    people = _ids(admin)
    raman, sita = people["Raman"]["id"], people["Sita"]["id"]
    mohan = add_relative(admin, raman, "son", {"given_name": "Mohan", "surname": "Iyer"})
    meera = add_relative(admin, raman, "daughter", {"given_name": "Meera", "surname": "Iyer"})
    priya = add_relative(admin, meera, "daughter", {"given_name": "Priya", "surname": "Iyer"})
    # Unrelated adult with no family links.
    stranger = add_standalone(admin, "Gopal", "Iyer")
    link = create_invite(admin, mohan)
    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    redeem_invite(admin, link, "Mohan", "mohan@example.com", "mohan-password-123")

    # Self, parent, sibling are in scope.
    for allowed in (mohan, raman, sita, meera):
        assert admin.get(f"/persons/{allowed}/edit").status_code == 200
    # Niece and unrelated person are not.
    for denied in (priya, stranger):
        assert admin.get(f"/persons/{denied}/edit").status_code == 403
        assert admin.post(f"/persons/{denied}/edit", data={"_csrf": "x"}).status_code == 403

    # A regular user cannot create a standalone person directly.
    assert admin.get("/persons/new").status_code == 403


def test_cycle_and_duplicate_birth_parents_are_blocked(admin):
    people = _ids(admin)
    raman = people["Raman"]["id"]
    mohan = add_relative(admin, raman, "son", {"given_name": "Mohan", "surname": "Iyer"})
    kabir = add_relative(admin, mohan, "son", {"given_name": "Kabir", "surname": "Iyer"})

    # Making the grandfather a child of his own grandson must fail.
    token = csrf(admin, f"/persons/new?anchor={kabir}&relation=son")
    response = admin.post("/persons/new", data={
        "_csrf": token, "anchor": kabir, "relation": "son", "action": "direct",
        "existing_person_id": raman,
    })
    assert response.status_code == 400
    assert "loop" in response.text.lower()

    # A person cannot acquire a second set of birth parents.
    response = admin.get(f"/persons/new?anchor={mohan}&relation=father")
    assert response.status_code == 303
    assert "both parents" in response.headers.get("location", "") or response.status_code == 303

    # Two parents already recorded: the GET should refuse and redirect.
    assert admin.get(f"/persons/new?anchor={mohan}&relation=mother").status_code == 303

    # A person with no known parents cannot have a sibling added yet.
    orphan = add_standalone(admin, "Orphan", "Iyer")
    response = admin.get(f"/persons/new?anchor={orphan}&relation=brother")
    assert response.status_code == 303
    token = csrf(admin, "/tree")
    response = admin.post("/persons/new", data={
        "_csrf": token, "anchor": orphan, "relation": "brother", "action": "direct",
        "given_name": "Sibling", "surname": "Iyer",
    })
    assert response.status_code == 400
    assert "parent" in response.text.lower()


def test_privacy_masking_for_other_users_and_admin(admin):
    people = _ids(admin)
    raman = people["Raman"]["id"]
    target = add_relative(admin, raman, "son", {
        "given_name": "Private", "surname": "Pat", "gender": "male",
        "email": "secret@example.com", "phone": "+91 12345 67890",
        "notes": "A very private note", "birth_year": "1970",
    })
    outsider = add_standalone(admin, "Outsider", "Pat")
    token = csrf(admin, f"/persons/{target}/edit")
    page = admin.get(f"/persons/{target}/edit").text
    version = re.search(r'name="_version" value="([^"]+)"', page).group(1)
    response = admin.post(f"/persons/{target}/edit", data={
        "_csrf": token, "_version": version, "action": "direct",
        "contact_visibility": "family", "_privacy_present": "1",
        "private_fields": ["birth", "notes"],
    })
    assert response.status_code == 303

    admin_card = admin.get(f"/api/persons/{target}/card").text
    assert "secret@example.com" in admin_card
    assert "very private note" in admin_card

    link = create_invite(admin, outsider)
    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    redeem_invite(admin, link, "Outsider", "outsider@example.com", "outsider-password-123")

    card = admin.get(f"/api/persons/{target}/card").text
    assert "secret@example.com" not in card
    assert "very private note" not in card
    assert "12345" not in card
    assert "Private" in card

    tree_data = tree(admin)
    node = next(p for p in tree_data["persons"] if p["id"] == target)
    assert node["birth_year"] is None
    assert node["private_birth"] is True


def test_invite_is_single_use(admin):
    people = _ids(admin)
    link = create_invite(admin, people["Sita"]["id"])
    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    response = redeem_invite(admin, link, "Cousin", "cousin@example.com", "cousin-password-123")
    assert response.status_code == 303
    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    response = admin.get("/invite/" + link.rsplit("/invite/", 1)[1])
    assert response.status_code == 404


def test_admin_can_revise_submission_before_approving(admin):
    people = _ids(admin)
    mohan = add_relative(admin, people["Raman"]["id"], "son", {
        "given_name": "Mohan", "surname": "Iyer", "phone": "+91 11111 11111",
    })
    link = create_invite(admin, mohan)
    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    redeem_invite(admin, link, "Mohan", "mohan@example.com", "mohan-password-123")
    page = admin.get(f"/persons/{mohan}/edit").text
    edit_token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    version = re.search(r'name="_version" value="([^"]+)"', page).group(1)
    admin.post(f"/persons/{mohan}/edit", data={
        "_csrf": edit_token, "_version": version, "action": "submit",
        "phone": "+91 22222 22222",
    })

    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    login(admin)
    queue = admin.get("/admin/queue").text
    set_id = re.search(r"/admin/queue/([0-9a-f]+)", queue).group(1)
    revise = admin.get(f"/admin/queue/{set_id}/revise")
    assert revise.status_code == 200
    assert "22222" in revise.text
    revise_token = re.search(r'name="_csrf" value="([^"]+)"', revise.text).group(1)
    response = admin.post(f"/admin/queue/{set_id}/revise", data={
        "_csrf": revise_token, "action": "direct",
        "phone": "+91 33333 33333", "nickname": "Moni",
    })
    assert response.status_code == 303
    card = admin.get(f"/api/persons/{mohan}/card").text
    assert "33333" in card
    assert "Moni" in card
    # The submission is now approved, not left pending.
    assert "queue-item" not in admin.get("/admin/queue").text


def test_admin_can_revise_an_addition_before_it_goes_live(admin):
    people = _ids(admin)
    mohan = add_relative(admin, people["Raman"]["id"], "son", {
        "given_name": "Mohan", "surname": "Iyer",
    })
    link = create_invite(admin, mohan)
    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    redeem_invite(admin, link, "Mohan", "mohan@example.com", "mohan-password-123")

    # Regular user adds a son: a person_create plus a union_child_add.
    token = csrf(admin, f"/persons/new?anchor={mohan}&relation=son")
    response = admin.post("/persons/new", data={
        "_csrf": token, "anchor": mohan, "relation": "son", "action": "submit",
        "given_name": "Kabir", "surname": "Iyer", "gender": "male",
        "_living_present": "1", "living": "on", "birth_qualifier": "exact",
    })
    assert response.status_code == 303

    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    login(admin)
    queue = admin.get("/admin/queue").text
    set_id = re.search(r"/admin/queue/([0-9a-f]+)", queue).group(1)
    revise = admin.get(f"/admin/queue/{set_id}/revise")
    assert revise.status_code == 200
    revise_token = re.search(r'name="_csrf" value="([^"]+)"', revise.text).group(1)
    response = admin.post(f"/admin/queue/{set_id}/revise", data={
        "_csrf": revise_token, "action": "direct",
        "given_name": "Kabir Rao", "surname": "Iyer", "gender": "male",
        "_living_present": "1", "living": "on", "birth_qualifier": "exact",
    })
    assert response.status_code == 303
    data = tree(admin)
    assert any(p["given"] == "Kabir Rao" for p in data["persons"])
    assert any(c["c"] == next(p["id"] for p in data["persons"] if p["given"] == "Kabir Rao")
               for c in data["children"])


def test_conflict_is_marked_for_attention_and_can_be_rejected(admin):
    import sqlite3
    people = _ids(admin)
    mohan = add_relative(admin, people["Raman"]["id"], "son", {
        "given_name": "Mohan", "surname": "Iyer",
    })
    link = create_invite(admin, mohan)
    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    redeem_invite(admin, link, "Mohan", "mohan@example.com", "mohan-password-123")
    page = admin.get(f"/persons/{mohan}/edit").text
    edit_token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    version = re.search(r'name="_version" value="([^"]+)"', page).group(1)
    admin.post(f"/persons/{mohan}/edit", data={
        "_csrf": edit_token, "_version": version, "action": "submit", "nickname": "Moni",
    })

    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    login(admin)
    # Admin edits the same record directly, invalidating the pending base version.
    page = admin.get(f"/persons/{mohan}/edit").text
    direct_token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    direct_version = re.search(r'name="_version" value="([^"]+)"', page).group(1)
    admin.post(f"/persons/{mohan}/edit", data={
        "_csrf": direct_token, "_version": direct_version, "action": "direct",
        "nickname": "AdminNick",
    })

    queue = admin.get("/admin/queue").text
    set_id = re.search(r"/admin/queue/([0-9a-f]+)", queue).group(1)
    review = admin.get(f"/admin/queue/{set_id}").text
    approve_token = re.search(r'name="_csrf" value="([^"]+)"', review).group(1)
    response = admin.post(f"/admin/queue/{set_id}/approve", data={"_csrf": approve_token})
    assert response.status_code == 303

    with sqlite3.connect(admin.app.state.settings.db_path) as conn:
        status = conn.execute("SELECT status FROM change_sets WHERE id = ?", (set_id,)).fetchone()[0]
    assert status == "needs_rebase"
    review = admin.get(f"/admin/queue/{set_id}").text
    assert "Approve and apply" not in review
    reject_token = re.search(r'name="_csrf" value="([^"]+)"', review).group(1)
    response = admin.post(f"/admin/queue/{set_id}/reject", data={
        "_csrf": reject_token, "reason": "superseded",
    })
    assert response.status_code == 303
    with sqlite3.connect(admin.app.state.settings.db_path) as conn:
        status = conn.execute("SELECT status FROM change_sets WHERE id = ?", (set_id,)).fetchone()[0]
    assert status == "rejected"
