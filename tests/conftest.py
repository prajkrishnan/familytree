"""Shared test fixtures: isolated data dir and a small Argon2 cost."""
from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")

from app import security  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.main import create_app  # noqa: E402

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "admin-password-123"


def csrf(client: TestClient, path: str) -> str:
    response = client.get(path)
    match = re.search(r'name="_csrf" value="([^"]+)"', response.text)
    assert match, f"No CSRF token on {path} (status {response.status_code})"
    return match.group(1)


def start_setup(client: TestClient, with_couple: bool = True):
    token = csrf(client, "/setup")
    data = {
        "_csrf": token,
        "token": "setup-token",
        "display_name": "Test Admin",
        "email": ADMIN_EMAIL,
        "password": ADMIN_PASSWORD,
        "confirm": ADMIN_PASSWORD,
    }
    if with_couple:
        data.update({
            "patriarch_given": "Raman", "patriarch_surname": "Iyer", "patriarch_year": "1928",
            "patriarch_deceased": "on",
            "matriarch_given": "Sita", "matriarch_surname": "Iyer", "matriarch_year": "1931",
            "matriarch_deceased": "on",
        })
    response = client.post("/setup", data=data)
    assert response.status_code == 303, response.text[:800]
    return response


def login(client: TestClient, email: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD):
    token = csrf(client, "/login")
    return client.post("/login", data={"_csrf": token, "email": email, "password": password})


def tree(client: TestClient) -> dict:
    response = client.get("/api/tree")
    assert response.status_code == 200, response.text
    return response.json()


def person_by_name(client: TestClient, given: str) -> dict:
    for person in tree(client)["persons"]:
        if person["given"] == given:
            return person
    raise AssertionError(f"{given} not in tree")


def add_relative(client: TestClient, anchor: str, relation: str, fields: dict,
                 direct: bool = True, union_id: str = "") -> str:
    token = csrf(client, f"/persons/new?anchor={anchor}&relation={relation}")
    data = {
        "_csrf": token,
        "anchor": anchor,
        "relation": relation,
        "action": "direct" if direct else "submit",
        "_living_present": "1",
        "living": "on",
        "birth_qualifier": "exact",
        "death_qualifier": "exact",
    }
    data.update(fields)
    if union_id:
        data["union_id"] = union_id
    response = client.post("/persons/new", data=data)
    assert response.status_code == 303, response.text[:1200]
    location = response.headers.get("location", "")
    match = re.search(r"focus=([0-9a-f]+)", location)
    assert match, location
    return match.group(1)


def add_standalone(client: TestClient, given: str, surname: str = "Standalone", **fields) -> str:
    token = csrf(client, "/persons/new")
    data = {
        "_csrf": token,
        "action": "direct",
        "_living_present": "1",
        "living": "on",
        "birth_qualifier": "exact",
        "given_name": given,
        "surname": surname,
    }
    data.update(fields)
    response = client.post("/persons/new", data=data)
    assert response.status_code == 303, response.text[:1000]
    match = re.search(r"focus=([0-9a-f]+)", response.headers.get("location", ""))
    assert match, response.headers.get("location")
    return match.group(1)


def create_invite(client: TestClient, person_id: str = "", role: str = "user") -> str:
    token = csrf(client, "/admin/invites")
    response = client.post("/admin/invites", data={
        "_csrf": token, "days": "7", "role": role, "person_id": person_id, "note": "test",
    })
    assert response.status_code == 200
    match = re.search(r'value="(http://[^"]+/invite/[^"]+)"', response.text)
    assert match, "Invite link missing"
    return match.group(1)


def redeem_invite(client: TestClient, link: str, name: str, email: str, password: str):
    path = "/invite/" + link.rsplit("/invite/", 1)[1]
    token = csrf(client, path)
    return client.post(path, data={
        "_csrf": token, "display_name": name, "email": email,
        "password": password, "confirm": password,
    })


@pytest.fixture
def client(tmp_path):
    settings = load_settings(
        data_dir=str(tmp_path),
        testing=True,
        setup_token="setup-token",
        base_url="http://testserver",
        argon2_time_cost=1,
        argon2_memory_kib=8192,
        argon2_parallelism=1,
        registration_mode="open",
    )
    security.limiter.reset_all()
    app = create_app(settings)
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


@pytest.fixture
def admin(client):
    start_setup(client)
    return client
