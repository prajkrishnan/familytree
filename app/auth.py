"""Opaque server-side sessions and route guards."""
from __future__ import annotations

import ipaddress
import sqlite3

from fastapi import Request

from .config import Settings
from .security import constant_eq, new_token, token_hash
from .util import now

SESSION_COOKIE = "fam_session"
ANON_CSRF_PREFIX = "anon:"
SESSION_CSRF_PREFIX = "sess:"
ANON_CSRF_COOKIE = "fam_csrf"
SESSION_LAST_SEEN_WRITE = 300


class RedirectException(Exception):
    def __init__(self, to: str = "/login"):
        self.to = to
        super().__init__(to)


def load_session(conn: sqlite3.Connection, token: str):
    """Return (session dict, user dict) or (None, None)."""
    if not token or len(token) > 200:
        return None, None
    row = conn.execute(
        "SELECT s.id AS session_id, s.user_id AS session_user_id, s.csrf_token, s.expires_at, "
        "s.last_seen_at, s.revoked_at, "
        "u.id, u.email, u.display_name, u.role, u.status, u.person_id, u.token_version "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (token_hash(token),),
    ).fetchone()
    if row is None:
        return None, None
    if row["revoked_at"] is not None or row["expires_at"] <= now():
        return None, None
    if row["status"] != "active":
        return None, None
    session = {
        "id": row["session_id"],
        "user_id": row["session_user_id"],
        "csrf_token": row["csrf_token"],
        "expires_at": row["expires_at"],
        "last_seen_at": row["last_seen_at"],
    }
    user = {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "role": row["role"],
        "status": row["status"],
        "person_id": row["person_id"],
        "token_version": row["token_version"],
    }
    return session, user


def touch_session(conn: sqlite3.Connection, session) -> None:
    if now() - session["last_seen_at"] > SESSION_LAST_SEEN_WRITE:
        conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (now(), session["id"]))
        session["last_seen_at"] = now()


def create_session(conn: sqlite3.Connection, user_id: str, settings: Settings,
                   ip: str = "", user_agent: str = "") -> str:
    token = new_token(32)
    timestamp = now()
    conn.execute(
        "INSERT INTO sessions(id, user_id, token_hash, csrf_token, created_at, last_seen_at, "
        "expires_at, ip, user_agent) VALUES (?,?,?,?,?,?,?,?,?)",
        (new_token(12), user_id, token_hash(token), new_token(24), timestamp, timestamp,
         timestamp + settings.session_days * 86400, ip[:64], user_agent[:300]),
    )
    return token


def revoke_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("UPDATE sessions SET revoked_at = ? WHERE id = ?", (now(), session_id))


def revoke_all_sessions(conn: sqlite3.Connection, user_id: str) -> None:
    conn.execute("UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                 (now(), user_id))
    conn.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (user_id,))


def purge_expired_sessions(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at < ? OR revoked_at IS NOT NULL",
                 (now() - 86400,))


def https_detected(request, settings: Settings) -> bool:
    """True when the browser is using HTTPS.

    Trusted proxies only: an untrusted client cannot fake X-Forwarded-Proto
    (the app must not be reachable except through the proxy anyway).
    """
    from .config import client_ip
    if settings.forwarded_proto in ("http", "https"):
        return settings.forwarded_proto == "https"
    try:
        peer = request.client.host if request.client else ""
        trusted = any(ipaddress.ip_address(peer) in net
                      for net in settings.trusted_networks)
    except ValueError:
        trusted = False
    if trusted:
        forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        if forwarded in ("https", "http"):
            return forwarded == "https"
        if request.headers.get("x-forwarded-ssl", "").lower() == "on":
            return True
    return settings.base_url.startswith("https://")


def secure_cookie_for(request, settings: Settings) -> bool:
    if settings.cookie_secure is not None:
        return settings.cookie_secure
    return https_detected(request, settings)


def set_session_cookie(response, token: str, settings: Settings, secure: bool | None = None) -> None:
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=settings.session_days * 86400,
        httponly=True,
        secure=settings.secure_cookies if secure is None else secure,
        samesite="lax",
        domain=settings.cookie_domain or None,
        path="/",
    )


def clear_session_cookie(response, settings: Settings | None = None) -> None:
    domain = (settings.cookie_domain or None) if settings else None
    response.delete_cookie(SESSION_COOKIE, path="/", domain=domain)


def require_user(request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        if request.url.path.startswith("/api/"):
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Authentication required")
        raise RedirectException("/login?next=" + _quote(target))
    return user


def require_admin(request) -> dict:
    user = require_user(request)
    if user["role"] != "admin":
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user


async def csrf_guard(request: "Request") -> None:
    """Validate CSRF for unsafe methods.

    This must run as a dependency, not HTTP middleware: parsing the form in a
    BaseHTTPMiddleware consumes the body before the endpoint can read it.
    """
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    from fastapi import HTTPException

    settings = request.app.state.settings
    origin, origin_problem = check_origin(request, settings)
    if origin_problem:
        raise HTTPException(status_code=403, detail=origin_problem)

    session = getattr(request.state, "session", None)
    anon = getattr(request.state, "anon_csrf", "") or ""
    expected = session["csrf_token"] if session else anon
    provided = request.headers.get("x-csrf-token", "")
    if not provided:
        try:
            form = await request.form()
            value = form.get("_csrf")
            provided = str(value) if value else ""
        except Exception:
            provided = ""
    if not provided:
        raise HTTPException(
            status_code=403,
            detail="The security token was missing from this form. Reload the page and submit again.",
        )
    if provided.startswith(SESSION_CSRF_PREFIX):
        provided_value = provided[len(SESSION_CSRF_PREFIX):]
    elif provided.startswith(ANON_CSRF_PREFIX):
        provided_value = provided[len(ANON_CSRF_PREFIX):]
    else:
        provided_value = provided
    if not expected or not constant_eq(expected, provided_value):
        session_stale = False
        if provided.startswith(SESSION_CSRF_PREFIX):
            cleaned, _ = load_session(request.state.conn, provided_value)
            session_stale = cleaned is not None
        hint = (" Your sign-in session had already expired, so it was refreshed; "
                "please submit the form once more.") if session_stale else                " Reload the page and submit again."
        raise HTTPException(
            status_code=403,
            detail="The security token did not match (this usually means the page was "
                   "open for a long time, or cookies were cleared)." + hint,
        )


def csrf_value(request) -> str:
    """Token rendered into forms.

    It carries a short prefix so a submission made after the session expired
    (or after the anonymous token was replaced) can be recognised, and the
    browser is simply asked to resubmit.
    """
    session = getattr(request.state, "session", None)
    if session:
        return SESSION_CSRF_PREFIX + session["csrf_token"]
    return ANON_CSRF_PREFIX + (getattr(request.state, "anon_csrf", "") or "")


def check_origin(request, settings):
    """Return (ok, problem). Validates Origin/Referer on unsafe requests."""
    from urllib.parse import urlsplit

    allowed = {urlsplit(settings.base_url).netloc}
    for header in ("host", "x-forwarded-host"):
        value = request.headers.get(header, "")
        if value:
            allowed.update(part.strip() for part in value.split(",") if part.strip())
    public = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    scheme = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.url.scheme

    origin = request.headers.get("origin")
    if origin and origin != "null":
        if urlsplit(origin).netloc in allowed:
            return True, ""
        return False, (f"Cross-site request refused: this page was sent from {origin}, but the app "
                       f"expects {settings.base_url}. Set BASE_URL to the address you use in the "
                       f"browser (currently it also accepts host {public or 'unknown'}).")
    referer = request.headers.get("referer")
    if referer:
        if urlsplit(referer).netloc in allowed:
            return True, ""
    return True, ""


def csrf_token(request) -> str:
    return csrf_value(request)


def _quote(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="/?=&")
