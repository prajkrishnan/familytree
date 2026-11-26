"""Login, logout, registration, invitation redemption and account settings."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from .. import auth, db, security
from ..config import client_ip
from ..relationships import get_person, record_audit
from ..templating import render, set_flash
from ..util import clean_text, new_id, now, safe_next

router = APIRouter()


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------

@router.get("/login")
def login_page(request: Request, next: str = "/tree"):
    if request.state.user:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "login.html", next=safe_next(next))


@router.post("/login")
async def login_submit(request: Request):
    conn = request.state.conn
    settings = request.app.state.settings
    form = await request.form()
    email = clean_text(str(form.get("email", ""))).lower()
    password = str(form.get("password", ""))
    next_url = safe_next(str(form.get("next", "/tree")))
    ip = client_ip(request)

    if not limiter_allows(ip, email):
        return render(request, "login.html", next=next_url, error=
                      "Too many sign-in attempts. Please wait a minute and try again.",
                      email=email, status_code=429)

    user = conn.execute("SELECT * FROM users WHERE email_norm = ?", (email,)).fetchone()
    if user is None:
        security.burn_time(password)
        return render(request, "login.html", next=next_url, email=email,
                      error="Email or password is incorrect.", status_code=400)

    if user["locked_until"] and user["locked_until"] > now():
        minutes = max(1, (user["locked_until"] - now()) // 60 + 1)
        return render(request, "login.html", next=next_url, email=email,
                      error=f"This account is temporarily locked. Try again in {minutes} minute(s).",
                      status_code=429)

    valid, needs_rehash = security.verify_password(password, user["password_hash"])
    if not valid:
        failures = user["failed_attempts"] + 1
        locked_until = now() + 900 if failures >= 8 else None
        conn.execute("UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                     (failures, locked_until, user["id"]))
        return render(request, "login.html", next=next_url, email=email,
                      error="Email or password is incorrect.", status_code=400)

    if user["status"] == "pending":
        return render(request, "login.html", next=next_url, email=email,
                      error="Your account is waiting for an administrator to approve it.",
                      status_code=403)
    if user["status"] != "active":
        return render(request, "login.html", next=next_url, email=email,
                      error="This account has been deactivated. Contact an administrator.",
                      status_code=403)

    if needs_rehash:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (security.hash_password(password), user["id"]))
    conn.execute(
        "UPDATE users SET failed_attempts = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
        (now(), user["id"]),
    )
    token = auth.create_session(conn, user["id"], settings, ip=ip,
                                user_agent=request.headers.get("user-agent", ""))
    record_audit(conn, user["id"], user["display_name"], "auth.login", "user", user["id"], None,
                 None, ip, request.headers.get("user-agent", ""))
    response = RedirectResponse(next_url, status_code=303)
    auth.set_session_cookie(response, token, settings, secure=auth.secure_cookie_for(request, settings))
    return response


@router.post("/logout")
def logout(request: Request):
    settings = request.app.state.settings
    session = request.state.session
    if session:
        auth.revoke_session(request.state.conn, session["id"])
    response = RedirectResponse("/login", status_code=303)
    auth.clear_session_cookie(response, settings)
    set_flash(response, "You have been signed out.")
    return response


def limiter_allows(ip: str, email: str) -> bool:
    ok_ip = security.limiter.allow(f"login:ip:{ip}", 15, 60)
    ok_email = security.limiter.allow(f"login:email:{email or ip}", 6, 60)
    ok_slow = security.limiter.allow(f"login:slow:{ip}", 40, 3600)
    return ok_ip and ok_email and ok_slow


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

@router.get("/register")
def register_page(request: Request):
    if request.state.user:
        return RedirectResponse("/tree", status_code=303)
    settings = request.app.state.settings
    mode = db.get_setting(request.state.conn, "registration_mode", settings.registration_mode)
    return render(request, "register.html", mode=mode, form={})


@router.post("/register")
async def register_submit(request: Request):
    conn = request.state.conn
    settings = request.app.state.settings
    mode = db.get_setting(conn, "registration_mode", settings.registration_mode)
    if mode == "invite_only":
        return render(request, "register.html", mode=mode, form={},
                      error="Registration is invite-only. Ask an administrator for an invite link.",
                      status_code=403)

    form = await request.form()
    display_name = clean_text(str(form.get("display_name", "")))
    email = clean_text(str(form.get("email", ""))).lower()
    password = str(form.get("password", ""))
    confirm = str(form.get("confirm", ""))
    passcode = str(form.get("passcode", ""))
    ip = client_ip(request)

    if not security.limiter.allow(f"register:{ip}", 5, 3600):
        return render(request, "register.html", mode=mode, form=form,
                      error="Too many registration attempts from this address.", status_code=429)

    errors = []
    if mode == "passcode":
        stored = db.get_setting(conn, "passcode_hash")
        if not stored:
            errors.append("Registration is not open: the family passcode has not been configured.")
        elif not security.verify_password(passcode, stored)[0]:
            errors.append("The family passcode is not correct.")
    if not display_name or len(display_name) > 120:
        errors.append("Please enter your name.")
    if not security.EMAIL_RE.match(email or ""):
        errors.append("Please enter a valid email address.")
    pw_error = security.password_error(password)
    if pw_error:
        errors.append(pw_error)
    if password != confirm:
        errors.append("The two passwords do not match.")
    if conn.execute("SELECT 1 FROM users WHERE email_norm = ?", (email,)).fetchone():
        errors.append("An account with that email already exists.")
    if errors:
        return render(request, "register.html", mode=mode, form=form, error=" ".join(errors),
                      status_code=400)

    conn.execute(
        "INSERT INTO users(id, email, email_norm, password_hash, display_name, role, status, created_at) "
        "VALUES (?,?,?,?,?, 'user', 'pending', ?)",
        (new_id(), email, email, security.hash_password(password), display_name, now()),
    )
    return render(request, "pending.html", email=email)


# ---------------------------------------------------------------------------
# Invitation redemption
# ---------------------------------------------------------------------------

def _find_invite(conn, raw_token: str):
    if not raw_token or len(raw_token) > 200:
        return None
    return conn.execute(
        "SELECT * FROM invites WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
        (security.token_hash(raw_token), now()),
    ).fetchone()


@router.get("/invite/{token}")
def invite_page(request: Request, token: str):
    conn = request.state.conn
    invite = _find_invite(conn, token)
    if invite is None:
        return render(request, "invite.html", invalid=True, token=token, form={}, status_code=404)
    person = get_person(conn, invite["person_id"]) if invite["person_id"] else None
    return render(request, "invite.html", invalid=False, token=token, invite=invite, person=person,
                  form={})


@router.post("/invite/{token}")
async def invite_submit(request: Request, token: str):
    conn = request.state.conn
    settings = request.app.state.settings
    invite = _find_invite(conn, token)
    if invite is None:
        return render(request, "invite.html", invalid=True, token=token, form={}, status_code=404)
    form = await request.form()
    display_name = clean_text(str(form.get("display_name", "")))
    email = clean_text(str(form.get("email", ""))).lower()
    password = str(form.get("password", ""))
    confirm = str(form.get("confirm", ""))
    errors: list[str] = []
    ip = client_ip(request)

    if not security.limiter.allow(f"invite:{ip}", 6, 3600):
        return render(request, "invite.html", invalid=False, token=token, invite=invite,
                      form=form, error="Too many attempts from this address.", status_code=429)
    if not display_name or len(display_name) > 120:
        errors.append("Please enter your name.")
    if not security.EMAIL_RE.match(email or ""):
        errors.append("Please enter a valid email address.")
    pw_error = security.password_error(password)
    if pw_error:
        errors.append(pw_error)
    if password != confirm:
        errors.append("The two passwords do not match.")
    if conn.execute("SELECT 1 FROM users WHERE email_norm = ?", (email,)).fetchone():
        errors.append("An account with that email already exists. Try signing in instead.")
    if invite["person_id"]:
        claimed = conn.execute(
            "SELECT 1 FROM users WHERE person_id = ? AND status = 'active'", (invite["person_id"],)
        ).fetchone()
        if claimed:
            errors.append("That family record has already been claimed by another account.")
    if errors:
        person = get_person(conn, invite["person_id"]) if invite["person_id"] else None
        return render(request, "invite.html", invalid=False, token=token, invite=invite,
                      person=person, form=form, error=" ".join(errors), status_code=400)

    with db.tx(conn):
        fresh = conn.execute(
            "SELECT * FROM invites WHERE id = ? AND used_at IS NULL AND expires_at > ?",
            (invite["id"], now()),
        ).fetchone()
        if fresh is None:
            return render(request, "invite.html", invalid=True, token=token, form={}, status_code=404)
        user_id = new_id()
        conn.execute(
            "INSERT INTO users(id, email, email_norm, password_hash, display_name, role, status, "
            "person_id, created_at, approved_at) VALUES (?,?,?,?,?,?, 'active', ?, ?, ?)",
            (user_id, email, email, security.hash_password(password), display_name,
             fresh["role"], fresh["person_id"], now(), now()),
        )
        conn.execute("UPDATE invites SET used_at = ?, used_by = ? WHERE id = ?",
                     (now(), user_id, fresh["id"]))
        record_audit(conn, user_id, display_name, "auth.invite_redeem", "invite", fresh["id"],
                     None, None, ip, request.headers.get("user-agent", ""))

    session = auth.create_session(conn, user_id, settings, ip=ip,
                                  user_agent=request.headers.get("user-agent", ""))
    response = RedirectResponse("/tree", status_code=303)
    auth.set_session_cookie(response, session, settings, secure=auth.secure_cookie_for(request, settings))
    set_flash(response, "Welcome to the family tree.")
    return response


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

@router.get("/account")
def account_page(request: Request):
    auth.require_user(request)
    conn = request.state.conn
    person = get_person(conn, request.state.user["person_id"]) if request.state.user["person_id"] else None
    submissions = conn.execute(
        "SELECT status, COUNT(*) AS n FROM change_sets WHERE submitted_by = ? GROUP BY status",
        (request.state.user["id"],),
    ).fetchall()
    return render(request, "account.html", person=person, submissions=submissions)


@router.post("/account/password")
async def change_password(request: Request):
    user = auth.require_user(request)
    conn = request.state.conn
    settings = request.app.state.settings
    form = await request.form()
    current = str(form.get("current_password", ""))
    new = str(form.get("new_password", ""))
    confirm = str(form.get("confirm", ""))
    if not security.limiter.allow(f"pwchange:{user['id']}", 5, 3600):
        set_flash_mode = None
        response = RedirectResponse("/account", status_code=303)
        set_flash(response, "Too many password change attempts. Try again later.", "error")
        return response
    errors = []
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
    if not security.verify_password(current, row["password_hash"])[0]:
        errors.append("Current password is incorrect.")
    pw_error = security.password_error(new)
    if pw_error:
        errors.append(pw_error)
    if new != confirm:
        errors.append("The two new passwords do not match.")
    if errors:
        response = RedirectResponse("/account", status_code=303)
        set_flash(response, " ".join(errors), "error")
        return response

    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (security.hash_password(new), user["id"]))
    auth.revoke_all_sessions(conn, user["id"])
    token = auth.create_session(conn, user["id"], settings,
                                ip=client_ip(request),
                                user_agent=request.headers.get("user-agent", ""))
    record_audit(conn, user["id"], user["display_name"], "auth.password_change", "user",
                 user["id"], None, None, client_ip(request), "")
    response = RedirectResponse("/account", status_code=303)
    auth.set_session_cookie(response, token, settings, secure=auth.secure_cookie_for(request, settings))
    set_flash(response, "Password updated. Other devices have been signed out.")
    return response


@router.post("/account/logout-all")
def logout_all(request: Request):
    settings = request.app.state.settings
    user = auth.require_user(request)
    auth.revoke_all_sessions(request.state.conn, user["id"])
    response = RedirectResponse("/login", status_code=303)
    auth.clear_session_cookie(response, settings)
    set_flash(response, "All sessions have been signed out.")
    return response
