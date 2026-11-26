"""Configuration loaded from environment variables (12-factor style)."""
from __future__ import annotations

import ipaddress
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass
class Settings:
    data_dir: Path
    base_url: str = "http://localhost:8000"
    secret_key: str = ""
    trusted_proxies: list[str] = field(default_factory=lambda: ["127.0.0.1", "::1"])
    setup_token: str | None = None
    registration_mode: str = "invite_only"
    max_upload_mb: int = 5
    session_days: int = 14
    site_title: str = "Family Tree"
    cookie_secure: bool | None = None
    argon2_time_cost: int = 3
    argon2_memory_kib: int = 32768
    argon2_parallelism: int = 2
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    allow_insecure_setup: bool = False
    cookie_domain: str = ""
    # Simulates an explicitly forwarded scheme (TESTING only). Empty means
    # "detect from trusted proxy headers or BASE_URL".
    forwarded_proto: str = ""
    testing: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "family.db"

    @property
    def avatar_dir(self) -> Path:
        return self.data_dir / "avatars"

    @property
    def pending_dir(self) -> Path:
        return self.data_dir / "pending"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def using_public_https(self) -> bool:
        """BASE_URL is https but the browser reached the app without HTTPS.

        That combination silently breaks Secure cookies and is the most common
        login problem behind a reverse proxy.
        """
        return False

    @property
    def secure_cookies(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.base_url.startswith("https://")

    @property
    def base_path(self) -> str:
        path = urlsplit(self.base_url).path.rstrip("/")
        return path

    @property
    def trusted_networks(self):
        nets = []
        for item in self.trusted_proxies:
            try:
                if "/" not in item:
                    item = item + "/32"
                nets.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                continue
        return nets


def load_settings(**overrides) -> Settings:
    """Build settings from (possibly patched) environment variables and overrides."""
    raw_dir = overrides.pop("data_dir", None) or _env("DATA_DIR", "./data")
    data_dir = Path(raw_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    base_url = (overrides.pop("base_url", None) or _env("BASE_URL", "http://localhost:8000")).rstrip("/")

    secret = overrides.pop("secret_key", None) or _env("SECRET_KEY")
    if not secret:
        secret_file = data_dir / ".secret_key"
        if secret_file.exists():
            secret = secret_file.read_text().strip()
        else:
            secret = secrets.token_urlsafe(48)
            secret_file.write_text(secret)
            try:
                secret_file.chmod(0o600)
            except OSError:
                pass

    proxies = _env("TRUSTED_PROXIES", "127.0.0.1,::1")
    proxy_list = [p.strip() for p in proxies.split(",") if p.strip()]

    cookie_secure_env = _env("COOKIE_SECURE").lower()
    cookie_secure = None if cookie_secure_env not in ("1", "0", "true", "false") else cookie_secure_env in ("1", "true")

    settings = Settings(
        data_dir=data_dir,
        base_url=base_url,
        secret_key=secret,
        trusted_proxies=proxy_list,
        setup_token=overrides.pop("setup_token", None) or _env("SETUP_TOKEN") or None,
        allow_insecure_setup=overrides.pop("allow_insecure_setup", None)
        if "allow_insecure_setup" in overrides else
        _env("ALLOW_INSECURE_SETUP").lower() in ("1", "true", "yes"),
        registration_mode=overrides.pop("registration_mode", None) or _env("REGISTRATION_MODE", "invite_only"),
        max_upload_mb=overrides.pop("max_upload_mb", None) or _env_int("MAX_UPLOAD_MB", 5),
        session_days=overrides.pop("session_days", None) or _env_int("SESSION_DAYS", 14),
        site_title=overrides.pop("site_title", None) or _env("SITE_TITLE", "Family Tree"),
        cookie_secure=cookie_secure,
        argon2_time_cost=overrides.pop("argon2_time_cost", None) or _env_int("ARGON2_TIME_COST", 3),
        argon2_memory_kib=overrides.pop("argon2_memory_kib", None) or _env_int("ARGON2_MEMORY_KIB", 32768),
        argon2_parallelism=overrides.pop("argon2_parallelism", None) or _env_int("ARGON2_PARALLELISM", 2),
        smtp_host=_env("SMTP_HOST"),
        smtp_port=_env_int("SMTP_PORT", 587),
        smtp_user=_env("SMTP_USER"),
        smtp_password=_env("SMTP_PASSWORD"),
        smtp_from=_env("SMTP_FROM"),
        smtp_starttls=_env("SMTP_STARTTLS", "true").lower() not in ("0", "false", "no"),
        cookie_domain=overrides.pop("cookie_domain", None) or _env("COOKIE_DOMAIN"),
        forwarded_proto=overrides.pop("forwarded_proto", None) or "",
        testing=bool(overrides.pop("testing", False)),
    )
    # Drop unknown overrides rather than silently ignoring typos in tests.
    for key in overrides:
        raise TypeError(f"Unknown setting override: {key}")

    for directory in (settings.avatar_dir, settings.pending_dir, settings.backup_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def ensure_setup_token(settings: Settings) -> str:
    """Return the first-run setup token, creating it on disk if necessary."""
    if settings.setup_token:
        return settings.setup_token
    if settings.testing:
        settings.setup_token = "test-setup-token"
        return settings.setup_token
    token_file = settings.data_dir / "setup_token"
    if token_file.exists():
        token = token_file.read_text().strip()
    else:
        token = secrets.token_urlsafe(18)
        token_file.write_text(token + "\n")
        try:
            token_file.chmod(0o600)
        except OSError:
            pass
    settings.setup_token = token
    return token


def client_ip(request) -> str:
    """Resolve the real client IP, trusting X-Forwarded-For only from proxies."""
    settings: Settings = request.app.state.settings
    peer = request.client.host if request.client else "0.0.0.0"
    try:
        peer_addr = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    trusted = settings.trusted_networks
    if not any(peer_addr in net for net in trusted):
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    if not forwarded:
        return peer
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            addr = ipaddress.ip_address(hop)
        except ValueError:
            continue
        if not any(addr in net for net in trusted):
            return str(addr)
    return hops[0] if hops else peer
