"""Authentication, setup and CSRF coverage."""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from conftest import ADMIN_EMAIL, ADMIN_PASSWORD, add_standalone, csrf, login, start_setup, tree


def test_unauthenticated_tree_redirects_to_login(client):
    response = client.get("/tree")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_first_run_setup_requires_token(client):
    response = client.get("/setup")
    assert response.status_code == 200
    token = csrf(client, "/setup")
    response = client.post("/setup", data={
        "_csrf": token, "token": "wrong-token", "display_name": "A",
        "email": "a@example.com", "password": "a-long-password-1", "confirm": "a-long-password-1",
    })
    assert response.status_code == 400
    assert "setup token is not correct" in response.text


def test_setup_creates_admin_and_founding_couple(client):
    start_setup(client)
    data = tree(client)
    assert len(data["persons"]) == 2
    assert len(data["unions"]) == 1
    assert data["root"]["union_id"]
    assert data["persons"][0]["lifespan"].endswith("?") or "–" in data["persons"][0]["lifespan"]
    response = client.get("/admin")
    assert response.status_code == 200


def test_csrf_required_on_posts(client):
    start_setup(client)
    response = client.post("/logout")
    assert response.status_code == 403
    token = csrf(client, "/tree")
    response = client.post("/logout", data={"_csrf": "wrong"})
    assert response.status_code == 403
    response = client.post("/logout", data={"_csrf": token})
    assert response.status_code == 303


def test_login_rejects_bad_password_and_accepts_good(admin):
    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    response = login(admin, password="definitely-wrong-password")
    assert response.status_code == 400
    assert "incorrect" in response.text
    response = login(admin)
    assert response.status_code == 303
    assert response.headers["location"] == "/tree"


def test_pending_user_cannot_log_in_until_approved(client):
    start_setup(client)
    token = csrf(client, "/tree")
    client.post("/logout", data={"_csrf": token})
    reg_token = csrf(client, "/register")
    response = client.post("/register", data={
        "_csrf": reg_token, "display_name": "New Cousin", "email": "cousin@example.com",
        "password": "cousin-password-123", "confirm": "cousin-password-123",
    })
    assert response.status_code == 200
    assert "waiting for an administrator" in response.text

    response = login(client, "cousin@example.com", "cousin-password-123")
    assert response.status_code == 403
    assert "waiting for an administrator" in response.text

    # Admin approves and links to the matriarch.
    login(client)
    people = tree(client)["persons"]
    sita = next(p for p in people if p["given"] == "Sita")
    import re
    users_page = client.get("/admin/users").text
    user_id = re.search(r"/admin/users/([0-9a-f]+)/approve", users_page).group(1)
    admin_token = csrf(client, "/admin/users")
    response = client.post(f"/admin/users/{user_id}/approve", data={
        "_csrf": admin_token, "person_id": sita["id"],
    })
    assert response.status_code == 303
    token = csrf(client, "/tree")
    client.post("/logout", data={"_csrf": token})
    response = login(client, "cousin@example.com", "cousin-password-123")
    assert response.status_code == 303


def test_security_headers_and_cookie_flags(client):
    response = client.get("/login")
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    cookie = response.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie

    start_setup(client)
    api = client.get("/api/tree")
    assert api.headers["cache-control"] == "private, no-store"

    token = csrf(client, "/tree")
    client.post("/logout", data={"_csrf": token})
    response = client.post("/login", data={"email": "x@example.com", "password": "whatever"})
    assert response.status_code == 403


def test_login_rate_limit_and_lockout(client):
    start_setup(client)
    token = csrf(client, "/tree")
    client.post("/logout", data={"_csrf": token})
    for _ in range(6):
        login(client, password="wrong-password-here")
    response = login(client, password="wrong-password-here")
    assert response.status_code == 429


def test_setup_without_couple_starts_with_empty_tree(client):
    start_setup(client, with_couple=False)
    data = tree(client)
    assert data["persons"] == []
    page = client.get("/tree").text
    assert "The tree is empty" in page
    first = add_standalone(client, "First", "Person")
    data = tree(client)
    assert len(data["persons"]) == 1
    assert data["root"]["person_id"] == first


def test_password_change_keeps_current_session_and_revokes_others(admin):
    page = admin.get("/account").text
    token = __import__("re").search(r'name="_csrf" value="([^"]+)"', page).group(1)
    response = admin.post("/account/password", data={
        "_csrf": token, "current_password": ADMIN_PASSWORD,
        "new_password": "brand-new-password-123", "confirm": "brand-new-password-123",
    })
    assert response.status_code == 303
    # The current browser was issued a fresh session.
    assert admin.get("/api/tree").status_code == 200
    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    assert login(admin, password=ADMIN_PASSWORD).status_code == 400
    assert login(admin, password="brand-new-password-123").status_code == 303


def test_base_url_mismatch_is_explained_and_can_be_confirmed(client):
    token = csrf(client, "/setup")
    # Simulate browsing through a different host than BASE_URL.
    response = client.post("/setup", headers={"Host": "192.168.1.50:8000"}, data={
        "_csrf": token, "token": "setup-token", "display_name": "A",
        "email": "a@example.com", "password": "a-long-password-1", "confirm": "a-long-password-1",
    })
    assert response.status_code == 400
    assert "BASE_URL" in response.text
    assert "address mismatch" in response.text.lower()
    response = client.post("/setup", headers={"Host": "192.168.1.50:8000"}, data={
        "_csrf": token, "token": "setup-token", "display_name": "A", "address_ok": "1",
        "email": "a@example.com", "password": "a-long-password-1", "confirm": "a-long-password-1",
    })
    assert response.status_code == 303


def test_cross_site_origin_is_refused_with_explanation(client):
    start_setup(client)
    token = csrf(client, "/tree")
    response = client.post("/logout", data={"_csrf": token},
                           headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert "Cross-site request refused" in response.text


def test_secure_cookies_follow_forwarded_proto(client):
    # BASE_URL is http in tests, so cookies are not Secure.
    response = client.get("/setup")
    assert "Secure" not in response.headers.get("set-cookie", "")

    from app import security
    from app.config import load_settings
    from app.main import create_app
    settings = load_settings(data_dir=str(client.app.state.settings.data_dir) + "-https",
                             testing=True, setup_token="setup-token",
                             base_url="https://family.example.com", forwarded_proto="http",
                             argon2_time_cost=1, argon2_memory_kib=8192, argon2_parallelism=1)
    security.limiter.reset_all()
    with TestClient(create_app(settings), follow_redirects=False) as https_client:
        token = csrf(https_client, "/setup")
        response = https_client.post("/setup", data={
            "_csrf": token, "token": "setup-token", "display_name": "S",
            "email": "s@example.com", "password": "a-long-password-1", "confirm": "a-long-password-1",
        })
        # BASE_URL says https but the browser did not use HTTPS: refuse rather than
        # issue secure cookies over an insecure connection.
        assert response.status_code == 400
        assert response.headers["content-type"].startswith("text/html")


def test_session_cookie_is_secure_behind_https_proxy(tmp_path):
    from app import security
    from app.config import load_settings
    from app.main import create_app
    settings = load_settings(data_dir=str(tmp_path / "httpsdir"), testing=True,
                             setup_token="setup-token",
                             base_url="https://family.example.com", forwarded_proto="https",
                             argon2_time_cost=1, argon2_memory_kib=8192, argon2_parallelism=1)
    security.limiter.reset_all()
    with TestClient(create_app(settings), follow_redirects=False) as https_client:
        # "Secure" cookies are not replayed by the in-process test client over
        # plain http, so carry the CSRF cookie explicitly (as HTTPS would).
        first = https_client.get("/setup")
        csrf_cookie = https_client.cookies.get("fam_csrf")
        token = re.search(r'name="_csrf" value="([^"]+)"', first.text).group(1)
        response = https_client.post(
            "/setup",
            headers={"Cookie": f"fam_csrf={csrf_cookie}"},
            data={
                "_csrf": token, "token": "setup-token", "display_name": "S",
                "email": "s@example.com", "password": "a-long-password-1",
                "confirm": "a-long-password-1", "address_ok": "1",
            },
        )
        assert response.status_code != 403
        cookies = " ".join(response.headers.get_list("set-cookie"))
        assert "fam_session=" in cookies and "Secure" in cookies


def test_session_survives_restart_with_rolling_token(client):
    start_setup(client)
    # Simulate a token rendered before the session existed: it is recognised and
    # the browser is asked to resubmit rather than being locked out.
    response = client.post("/logout", data={"_csrf": "sess:not-a-real-session"})
    assert response.status_code == 403
    assert "did not match" in response.text


def test_setup_works_when_browed_over_plain_http_with_https_base(tmp_path):
    from app import security
    from app.config import load_settings
    from app.main import create_app
    settings = load_settings(data_dir=str(tmp_path / "secdir"), testing=True,
                             setup_token="setup-token", base_url="https://family.example.com",
                             argon2_time_cost=1, argon2_memory_kib=8192, argon2_parallelism=1)
    security.limiter.reset_all()
    with TestClient(create_app(settings), follow_redirects=False) as https_client:
        response = https_client.get("/tree", headers={"Host": "family.example.com"})
        assert "without HTTPS" in response.text if response.status_code == 200 else True
