"""First-run setup: create the administrator and optionally the founding couple."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .. import auth, db, security
from ..config import ensure_setup_token
from ..relationships import create_person, create_union, record_audit
from ..templating import render, set_flash
from ..util import clean_text, new_id, now

router = APIRouter()
log = logging.getLogger("familytree.setup")


def user_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


@router.get("/")
def index(request: Request):
    conn = request.state.conn
    if user_count(conn) == 0:
        return RedirectResponse("/setup", status_code=303)
    if request.state.user is None:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/tree", status_code=303)


@router.get("/setup")
def setup_page(request: Request):
    if user_count(request.state.conn) > 0:
        return RedirectResponse("/login", status_code=303)
    ensure_setup_token(request.app.state.settings)
    return render(request, "setup.html", form={}, address_note=_address_note(request))


def _address_note(request: Request) -> str:
    """Explain a BASE_URL that does not match the address in the browser."""
    settings = request.app.state.settings
    from urllib.parse import urlsplit

    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    if not host:
        return ""
    expected = urlsplit(settings.base_url).netloc
    if host == expected:
        return ""
    return (
        f"This page was opened at {host}, but BASE_URL is set to {settings.base_url}. "
        f"Fix BASE_URL in .env to exactly {host} and restart the container. "
        "Setup can complete anyway after a single confirmation below."
    )


@router.post("/setup")
async def setup_submit(request: Request):
    conn = request.state.conn
    settings = request.app.state.settings
    if user_count(conn) > 0:
        return RedirectResponse("/login", status_code=303)

    if request.url.scheme == "https" and not db.get_setting(conn, "external_https") \
            and not settings.allow_insecure_setup:
        note = _address_note(request)
        return render(request, "setup.html", form={}, address_note=note, error=(
            "Refusing to finish setup: the browser reached the app over HTTPS, but BASE_URL is "
            f"{settings.base_url}, so the app would issue cookies the browser will never send "
            "back — you would be unable to sign in afterwards. Set BASE_URL to the exact https:// "
            "address you use (this page was opened at "
            f"{(request.headers.get('x-forwarded-host') or request.headers.get('host') or 'unknown')}), "
            "restart the container and reload this page. Only if HTTPS is terminated somewhere the "
            "app cannot detect it, set ALLOW_INSECURE_SETUP=true."), status_code=400)

    form = await request.form()
    token = str(form.get("token", "")).strip()
    display_name = clean_text(str(form.get("display_name", "")))
    email = clean_text(str(form.get("email", ""))).lower()
    password = str(form.get("password", ""))
    confirm = str(form.get("confirm", ""))
    errors: list[str] = []

    expected = ensure_setup_token(settings)
    if not token or not security.constant_eq(token, expected):
        errors.append(f"The setup token is not correct. It is in the container log: "
                      f"docker compose logs app | grep -i 'setup token'. If it was not printed, "
                      f"the token is also stored in the data volume at /data/setup_token.")
    note = _address_note(request)
    if note and str(form.get("address_ok", "")) != "1":
        errors.append(note + " If the app is configured correctly, open it at that address "
                      "instead, or tick the confirmation box to continue anyway.")
    if not display_name or len(display_name) > 120:
        errors.append("Please enter your name (max 120 characters).")
    if not security.EMAIL_RE.match(email or ""):
        errors.append("Please enter a valid email address.")
    pw_error = security.password_error(password)
    if pw_error:
        errors.append(pw_error)
    if password != confirm:
        errors.append("The two passwords do not match.")

    patriarch = {
        "given_name": clean_text(str(form.get("patriarch_given", ""))),
        "surname": clean_text(str(form.get("patriarch_surname", ""))),
        "birth_year": _year(form.get("patriarch_year")),
        "is_living": 0 if form.get("patriarch_deceased") else 1,
    }
    matriarch = {
        "given_name": clean_text(str(form.get("matriarch_given", ""))),
        "surname": clean_text(str(form.get("matriarch_surname", ""))),
        "birth_year": _year(form.get("matriarch_year")),
        "is_living": 0 if form.get("matriarch_deceased") else 1,
    }
    for label, person in (("Patriarch", patriarch), ("Matriarch", matriarch)):
        if person["birth_year"] is None and str(form.get("patriarch_year" if label == "Patriarch" else "matriarch_year", "")).strip():
            errors.append(f"{label} birth year must be between 1000 and 2200.")

    if errors:
        return render(request, "setup.html", form=form, error=" ".join(errors),
                      address_note=note, status_code=400)

    with db.tx(conn):
        admin_id = new_id()
        timestamp = now()
        conn.execute(
            "INSERT INTO users(id, email, email_norm, password_hash, display_name, role, status, "
            "created_at, approved_at) VALUES (?,?,?,?,?, 'admin', 'active', ?, ?)",
            (admin_id, email, email, security.hash_password(password), display_name, timestamp, timestamp),
        )
        admin = {"id": admin_id, "display_name": display_name}
        root_union_id = ""
        partner_ids = []
        for label, person in (("patriarch", patriarch), ("matriarch", matriarch)):
            if not person["given_name"] and not person["surname"]:
                continue
            gender = "male" if label == "patriarch" else "female"
            data = {
                "given_name": person["given_name"],
                "surname": person["surname"],
                "gender": gender,
                "is_living": person["is_living"],
                "birth_year": person["birth_year"],
                "birth_qualifier": "exact",
            }
            pid = create_person(conn, data, admin_id, display_name)
            partner_ids.append(pid)
        if partner_ids:
            root_union_id = create_union(
                conn, partner_ids[0], partner_ids[1] if len(partner_ids) > 1 else None,
                kind="marriage", status="unknown",
                actor_id=admin_id, actor_name=display_name,
            )
            db.set_setting(conn, "root_union_id", root_union_id)
        db.set_setting(conn, "setup_done", "1")
        if request.url.scheme == "https":
            db.set_setting(conn, "external_https", "1")
        record_audit(conn, admin_id, display_name, "setup.complete", "instance", new_id(),
                     None, {"root_union_id": root_union_id}, "", "")

    try:
        (settings.data_dir / "setup_token").unlink()
    except OSError:
        pass

    response = RedirectResponse("/tree", status_code=303)
    token_value = auth.create_session(conn, admin_id, settings,
                                      ip=request.client.host if request.client else "",
                                      user_agent=request.headers.get("user-agent", ""))
    auth.set_session_cookie(response, token_value, settings, secure=auth.secure_cookie_for(request, settings))
    set_flash(response, "Welcome. Your family tree is ready.")
    return response


def _year(value) -> int | None:
    try:
        year = int(str(value).strip()) if str(value).strip() else None
    except (TypeError, ValueError):
        return None
    if year is None:
        return None
    return year if 1000 <= year <= 2200 else None
