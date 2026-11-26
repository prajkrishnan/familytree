"""Password hashing, opaque tokens, rate limiting and CSRF primitives."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError

from .config import Settings

_hasher: PasswordHasher | None = None
_dummy_hash: str | None = None
_hasher_lock = threading.Lock()

PASSWORD_MIN = 12
PASSWORD_MAX = 256
COMMON_PASSWORDS = {
    "password", "password123", "123456789012", "qwertyuiop12", "letmein12345",
    "iloveyou1234", "admin1234567", "welcome12345", "changeme1234", "familytree123",
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def configure(settings: Settings) -> None:
    global _hasher, _dummy_hash
    with _hasher_lock:
        _hasher = PasswordHasher(
            time_cost=settings.argon2_time_cost,
            memory_cost=settings.argon2_memory_kib,
            parallelism=settings.argon2_parallelism,
            hash_len=32,
            salt_len=16,
        )
        _dummy_hash = _hasher.hash("timing-equalizer-not-a-real-password")


def _require_hasher() -> PasswordHasher:
    if _hasher is None:
        raise RuntimeError("security.configure() was not called")
    return _hasher


def hash_password(password: str) -> str:
    return _require_hasher().hash(password)


def verify_password(password: str, password_hash: str) -> tuple[bool, bool]:
    """Return (valid, needs_rehash). Unknown users share the dummy hash path."""
    hasher = _require_hasher()
    try:
        hasher.verify(password_hash, password)
        return True, hasher.check_needs_rehash(password_hash)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False
    except Exception:
        return False, False


def burn_time(password: str = "dummy") -> None:
    """Spend comparable CPU on nonexistent accounts to limit user enumeration."""
    global _dummy_hash
    if _dummy_hash is None:
        return
    try:
        _require_hasher().verify(_dummy_hash, password)
    except Exception:
        pass


def password_error(password: str) -> str | None:
    if len(password) < PASSWORD_MIN:
        return f"Password must be at least {PASSWORD_MIN} characters."
    if len(password) > PASSWORD_MAX:
        return "Password is too long."
    if password.strip().lower() in COMMON_PASSWORDS:
        return "That password is too common. Choose something unique."
    return None


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class RateLimiter:
    """In-process sliding-window limiter.

    The app deliberately runs a single worker (SQLite), so process-local state
    is authoritative. Behind a reverse proxy the IP key is the resolved
    X-Forwarded-For client, not the proxy address.
    """

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            q = self._events[key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._events) > 20000:
                self._prune(cutoff)
            return True

    def retry_after(self, key: str, window_seconds: int) -> int:
        with self._lock:
            q = self._events.get(key)
            if not q:
                return 0
            return max(0, int(q[0] + window_seconds - time.monotonic()))

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)

    def reset_all(self) -> None:
        with self._lock:
            self._events.clear()

    def _prune(self, cutoff: float) -> None:
        stale = [k for k, q in self._events.items() if not q or q[-1] < cutoff]
        for k in stale:
            self._events.pop(k, None)


limiter = RateLimiter()
