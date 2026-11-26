"""Application factory, request context, CSRF enforcement and error handling."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth, db, media, security, templating
from .config import Settings, client_ip, ensure_setup_token, load_settings
from .security import constant_eq, new_token
from .templating import FLASH_COOKIE, render

log = logging.getLogger("familytree")

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
STATIC_PREFIX = "/static/"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    security.configure(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db(settings)
        conn = db.connect(settings.db_path)
        try:
            auth.purge_expired_sessions(conn)
        finally:
            conn.close()
        media.cleanup_pending(settings)
        users = 0
        conn = db.connect(settings.db_path)
        try:
            users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            conn.close()
        if users == 0:
            token = ensure_setup_token(settings)
            log.warning("First-run setup token: %s  (open %s/setup)", token, settings.base_url)
        log.info("Family tree ready at %s (data: %s)", settings.base_url, settings.data_dir)
        yield

    app = FastAPI(
        title="Family Tree",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.https_warning = ""

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        path = request.url.path
        if path.startswith(STATIC_PREFIX) or path == "/healthz":
            return await call_next(request)

        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit():
            if int(content_length) > settings.max_upload_mb * 1024 * 1024 + 2 * 1024 * 1024:
                return _security_headers(JSONResponse(
                    {"detail": "Request body is too large."}, status_code=413), settings)

        if (settings.base_url.startswith("https://") and path != "/login"
                and not auth.https_detected(request, settings)
                and not app.state.https_warning):
            app.state.https_warning = (
                f"BASE_URL is {settings.base_url}, but this request arrived without HTTPS "
                f"(no X-Forwarded-Proto: https from a trusted proxy). Secure cookies will be "
                f"dropped and sign-in will appear to fail. Send X-Forwarded-Proto $scheme from "
                f"nginx, or set BASE_URL to the http:// address you are actually using."
            )
            log.warning(app.state.https_warning)

        conn = db.connect(settings.db_path)
        request.state.conn = conn
        request.state.user = None
        request.state.session = None
        request.state.flashes = templating.read_flashes(request)
        # Proxies sometimes send several Cookie headers, which some parsers drop.
        anon = request.cookies.get(auth.ANON_CSRF_COOKIE, "") or _raw_cookie(request, auth.ANON_CSRF_COOKIE)
        request.state.anon_csrf = anon
        new_anon = False

        try:
            token = request.cookies.get(auth.SESSION_COOKIE, "") or _raw_cookie(request, auth.SESSION_COOKIE)
            if token:
                session, user = auth.load_session(conn, token)
                if session and user:
                    request.state.session = session
                    request.state.user = user
                    auth.touch_session(conn, session)

            if request.state.session is None and not anon:
                anon = new_token(24)
                request.state.anon_csrf = anon
                new_anon = True

            response = await call_next(request)
        except Exception:
            conn.close()
            raise

        if new_anon:
            response.set_cookie(
                auth.ANON_CSRF_COOKIE, anon,
                max_age=settings.session_days * 86400,
                httponly=True,
                secure=auth.secure_cookie_for(request, settings),
                samesite="lax",
                domain=_cookie_domain(settings),
                path="/",
            )
        if FLASH_COOKIE in request.cookies:
            response.delete_cookie(FLASH_COOKIE, path="/")
        conn.close()
        return _security_headers(response, settings)

    app.mount(STATIC_PREFIX, StaticFiles(directory=str(templating.TEMPLATE_DIR.parent.parent / "static")),
              name="static")

    from .routes import admin_routes, auth_routes, moderation_routes, person_routes, setup_routes, tree_routes
    guarded = [Depends(auth.csrf_guard)]
    app.include_router(setup_routes.router, dependencies=guarded)
    app.include_router(auth_routes.router, dependencies=guarded)
    app.include_router(tree_routes.router, dependencies=guarded)
    app.include_router(person_routes.router, dependencies=guarded)
    app.include_router(moderation_routes.router, dependencies=guarded)
    app.include_router(admin_routes.router, dependencies=guarded)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        try:
            conn = db.connect(settings.db_path)
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
            return {"status": "ok"}
        except Exception:
            return JSONResponse({"status": "error"}, status_code=503)

    @app.exception_handler(auth.RedirectException)
    async def redirect_handler(request: Request, exc: auth.RedirectException):
        return RedirectResponse(exc.to, status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        if getattr(request.state, "conn", None) is None:
            return HTMLResponse(f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>",
                                status_code=exc.status_code)
        message = {
            401: "Please sign in to continue.",
            403: "You do not have permission to do that.",
            404: "That page or record could not be found.",
            413: "That upload is too large.",
            429: "Too many attempts. Please wait a moment and try again.",
        }.get(exc.status_code, str(exc.detail))
        if exc.status_code == 403:
            message = str(exc.detail) if exc.detail else message
        response = render(request, "error.html", status_code=exc.status_code,
                          code=exc.status_code, message=message)
        return _security_headers(response, request.app.state.settings)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Invalid request."}, status_code=422)
        return render(request, "error.html", status_code=422,
                      code=422, message="Some of the submitted values were not valid.")

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception):
        log.exception("Unhandled error on %s", request.url.path)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Internal server error"}, status_code=500)
        try:
            return render(request, "error.html", status_code=500,
                          code=500, message="Something went wrong on our side.")
        except Exception:
            return HTMLResponse("<h1>500</h1><p>Something went wrong.</p>", status_code=500)

    return app


def _csrf_error(request: Request, message: str):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": message}, status_code=403)
    if getattr(request.state, "conn", None) is None:
        return HTMLResponse(f"<h1>403</h1><p>{message}</p>", status_code=403)
    return render(request, "error.html", status_code=403, code=403, message=message)


def _security_headers(response, settings: Settings):
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; font-src 'self'; object-src 'none'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    )
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if settings.base_url.startswith("https://"):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


def _raw_cookie(request, name: str) -> str:
    for header in request.headers.getlist("cookie"):
        for part in header.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name and value:
                return value
    return ""


def _cookie_domain(settings: Settings) -> str | None:
    """Host-only cookies by default; explicit COOKIE_DOMAIN for split hosts."""
    return settings.cookie_domain or None


app = create_app()
