"""Graph edge cases: remarriage, half-siblings, deletion and date validation."""
from __future__ import annotations

import re

from conftest import (add_relative, add_standalone, create_invite, csrf, login, redeem_invite,
                      start_setup, tree)


def _setup_family(admin):
    people = {p["given"]: p for p in tree(admin)["persons"]}
    mohan = add_relative(admin, people["Raman"]["id"], "son", {
        "given_name": "Mohan", "surname": "Iyer", "gender": "male", "birth_year": "1955",
    })
    anita = add_relative(admin, mohan, "spouse", {
        "given_name": "Anita", "surname": "Rao", "gender": "female",
    })
    lakshmi = add_relative(admin, mohan, "spouse", {
        "given_name": "Lakshmi", "surname": "Nair", "gender": "female",
    })
    data = tree(admin)
    unions = [u for u in data["unions"] if mohan in (u["partner1_id"], u["partner2_id"])]
    union_anita = next(u for u in unions if anita in (u["partner1_id"], u["partner2_id"]))
    union_lakshmi = next(u for u in unions if lakshmi in (u["partner1_id"], u["partner2_id"]))
    kabir = add_relative(admin, mohan, "son", {
        "given_name": "Kabir", "surname": "Iyer", "gender": "male",
    }, union_id=union_anita["id"])
    arjun = add_relative(admin, mohan, "son", {
        "given_name": "Arjun", "surname": "Iyer", "gender": "male",
    }, union_id=union_lakshmi["id"])
    return mohan, anita, lakshmi, kabir, arjun, union_anita, union_lakshmi


def test_remarriage_creates_two_unions_and_half_siblings(admin):
    mohan, anita, lakshmi, kabir, arjun, union_a, union_l = _setup_family(admin)
    data = tree(admin)
    assert len([u for u in data["unions"] if mohan in (u["partner1_id"], u["partner2_id"])]) == 2
    assert any(c["u"] == union_a["id"] and c["c"] == kabir for c in data["children"])
    assert any(c["u"] == union_l["id"] and c["c"] == arjun for c in data["children"])

    card = admin.get(f"/api/persons/{kabir}/card").text
    assert "Arjun" in card
    assert "Half-brother" in card
    # Both mothers are recorded as distinct partners.
    mohan_card = admin.get(f"/api/persons/{mohan}/card").text
    assert "Anita" in mohan_card and "Lakshmi" in mohan_card


def test_user_scope_includes_step_and_half_family(admin):
    mohan, anita, lakshmi, kabir, arjun, union_a, union_l = _setup_family(admin)
    outsider = add_standalone(admin, "Not", "Family")
    link = create_invite(admin, mohan)
    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    redeem_invite(admin, link, "Mohan", "mohan@example.com", "mohan-password-123")

    for allowed in (mohan, anita, lakshmi, kabir, arjun):
        assert admin.get(f"/persons/{allowed}/edit").status_code == 200
    assert admin.get(f"/persons/{outsider}/edit").status_code == 403


def test_deleting_a_partner_preserves_the_other_parent(admin):
    mohan, anita, lakshmi, kabir, arjun, union_a, union_l = _setup_family(admin)
    token = csrf(admin, "/tree")
    response = admin.post(f"/persons/{mohan}/delete", data={"_csrf": token})
    assert response.status_code == 303

    data = tree(admin)
    assert not any(p["id"] == mohan for p in data["persons"])
    # Anita survives and remains linked to Kabir.
    assert any(p["id"] == anita for p in data["persons"])
    assert any(c["c"] == kabir for c in data["children"])
    anita_card = admin.get(f"/api/persons/{anita}/card").text
    assert "Kabir" in anita_card
    kabir_card = admin.get(f"/api/persons/{kabir}/card").text
    assert "Anita" in kabir_card
    assert "Mother" in kabir_card


def test_invalid_calendar_dates_are_rejected(admin):
    people = {p["given"]: p for p in tree(admin)["persons"]}
    raman = people["Raman"]["id"]
    page = admin.get(f"/persons/{raman}/edit").text
    token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    version = re.search(r'name="_version" value="([^"]+)"', page).group(1)
    response = admin.post(f"/persons/{raman}/edit", data={
        "_csrf": token, "_version": version, "action": "direct",
        "birth_day": "31", "birth_month": "2", "birth_year": "1930",
    })
    assert response.status_code == 400
    assert "does not exist" in response.text

    page = admin.get(f"/persons/{raman}/edit").text
    token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    version = re.search(r'name="_version" value="([^"]+)"', page).group(1)
    response = admin.post(f"/persons/{raman}/edit", data={
        "_csrf": token, "_version": version, "action": "direct",
        "birth_year": "3000",
    })
    assert response.status_code == 400


def test_death_cannot_precede_birth(admin):
    people = {p["given"]: p for p in tree(admin)["persons"]}
    raman = people["Raman"]["id"]
    page = admin.get(f"/persons/{raman}/edit").text
    token = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
    version = re.search(r'name="_version" value="([^"]+)"', page).group(1)
    response = admin.post(f"/persons/{raman}/edit", data={
        "_csrf": token, "_version": version, "action": "direct",
        "_living_present": "1", "birth_year": "1950",
        "death_day": "1", "death_month": "1", "death_year": "1940",
    })
    assert response.status_code == 400
    assert "before date of birth" in response.text


def test_foreign_origin_is_rejected(admin):
    token = csrf(admin, "/tree")
    response = admin.post("/logout", data={"_csrf": token},
                          headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    response = admin.post("/logout", data={"_csrf": token},
                          headers={"Origin": "http://testserver"})
    assert response.status_code == 303
