"""Jinja environment, filters and the shared page renderer."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.templating import Jinja2Templates

from . import auth, db, util
from .config import Settings

TEMPLATE_DIR = Path(__file__).parent / "templates"
if sys.version_info >= (3, 14):
    # Jinja2 <= 3.1.6 mis-resolves {% if %} blocks inside {% block %} content on
    # Python 3.14 (flask-templating bug 1634), which silently drops markup. The
    # production image runs on 3.12 where the behaviour is correct.
    import warnings
    warnings.warn(
        "Running on Python 3.14: Jinja block rendering inside templates may drop "
        "conditional markup. Use the Docker image (Python 3.12) for real use.",
        RuntimeWarning,
        stacklevel=2,
    )

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

templates.env.filters["lifespan"] = util.lifespan
templates.env.filters["birth_text"] = util.birth_text
templates.env.filters["death_text"] = util.death_text
templates.env.filters["full_name"] = util.full_name
templates.env.filters["display_name"] = util.display_name
templates.env.filters["initials"] = util.initials
templates.env.filters["rel_time"] = util.rel_time
templates.env.filters["fromjson"] = lambda v: json.loads(v) if v else []
templates.env.globals["gender_icon"] = lambda g: util.GENDER_ICONS.get(g, "◇")
templates.env.globals["gender_label"] = lambda g: util.GENDERS.get(g, "Other")

FLASH_COOKIE = "fam_flash"


def get_site_title(conn, settings: Settings) -> str:
    return db.get_setting(conn, "site_title", settings.site_title) or settings.site_title


def render(request, name: str, status_code: int = 200, **context):
    settings: Settings = request.app.state.settings
    conn = request.state.conn
    user = getattr(request.state, "user", None)
    pending_admin = 0
    if user and user["role"] == "admin":
        pending_admin = conn.execute(
            "SELECT COUNT(*) FROM change_sets WHERE status = 'pending'"
        ).fetchone()[0]
    base = {
        "request": request,
        "user": user,
        "csrf": auth.csrf_value(request),
        "token_refreshed": bool(getattr(request.state, "refresh_form_token", False)),
        "https_warning": getattr(request.app.state, "https_warning", ""),
        "flashes": getattr(request.state, "flashes", []),
        "settings": settings,
        "site_title": get_site_title(conn, settings),
        "pending_admin": pending_admin,
        "current_path": request.url.path,
    }
    base.update(context)
    return templates.TemplateResponse(request, name, base, status_code=status_code)


def set_flash(response, message: str, kind: str = "ok") -> None:
    payload = json.dumps([{"message": message, "kind": kind}], ensure_ascii=False)
    response.set_cookie(FLASH_COOKIE, payload, max_age=120, httponly=True,
                        samesite="lax", path="/")


def read_flashes(request) -> list[dict]:
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)][:5]
    except (ValueError, TypeError):
        return []
    return []
