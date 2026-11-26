"""Operator CLI for locked-out or headless installations.

Run inside the container (or a local virtualenv):

    python -m app.manage list-users
    python -m app.manage reset-password --email admin@example.com
    python -m app.manage create-admin --email admin@example.com --name Admin
    python -m app.manage unlock --email admin@example.com
    python -m app.manage setup-token

Passwords are never recoverable: only the Argon2 hash is stored. This tool
resets a password and revokes that account's sessions.
"""
from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys

from . import db, security
from .config import Settings, ensure_setup_token, load_settings
from .util import new_id, now


def _settings() -> Settings:
    settings = load_settings()
    security.configure(settings)
    return settings


def _open(settings: Settings, init: bool = False) -> sqlite3.Connection:
    if init:
        db.init_db(settings)
    if not settings.db_path.exists():
        raise SystemExit(
            f"No database at {settings.db_path}. Start the app once and use the "
            "/setup page, or run: python -m app.manage create-admin ..."
        )
    return db.connect(settings.db_path)


def _require_password(raw: str | None) -> str:
    password = raw
    if not password:
        if not sys.stdin.isatty():
            raise SystemExit("No TTY for a prompt; pass --password (or run with docker compose exec app ...).")
        password = getpass.getpass("New password: ")
        confirm = getpass.getpass("Confirm new password: ")
        if password != confirm:
            raise SystemExit("Passwords do not match.")
    problem = security.password_error(password)
    if problem:
        raise SystemExit(problem)
    return password


def _find_user(conn: sqlite3.Connection, email: str):
    return conn.execute(
        "SELECT * FROM users WHERE email_norm = ?", (email.strip().lower(),)
    ).fetchone()


def _print_users(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT u.id, u.email, u.display_name, u.role, u.status, u.failed_attempts, "
        "u.locked_until, p.given_name, p.surname "
        "FROM users u LEFT JOIN persons p ON p.id = u.person_id "
        "ORDER BY u.created_at"
    ).fetchall()
    if not rows:
        print("No user accounts exist yet. Open /setup and use the setup token.")
        return
    print(f"{'EMAIL':<32} {'NAME':<22} {'ROLE':<6} {'STATUS':<9} LINKED PERSON")
    print("-" * 92)
    for row in rows:
        linked = " ".join(x for x in (row["given_name"], row["surname"]) if x) or "—"
        if row["locked_until"] and row["locked_until"] > now():
            status = "locked"
        else:
            status = row["status"]
        print(f"{row['email']:<32} {row['display_name'][:21]:<22} {row['role']:<6} {status:<9} {linked}")
    print("\nUse the email address above with reset-password. Hashes are one-way and "
          "cannot be read back.")


def cmd_list(args, settings: Settings) -> int:
    conn = _open(settings)
    try:
        _print_users(conn)
    finally:
        conn.close()
    return 0


def cmd_reset(args, settings: Settings) -> int:
    conn = _open(settings)
    try:
        user = _find_user(conn, args.email)
        if user is None:
            print(f"No account with email {args.email!r}. Existing accounts:", file=sys.stderr)
            _print_users(conn)
            return 1
        password = _require_password(args.password)
        timestamp = now()
        conn.execute(
            "UPDATE users SET password_hash = ?, failed_attempts = 0, locked_until = NULL, "
            "status = CASE WHEN ? THEN 'active' ELSE status END, "
            "token_version = token_version + 1 WHERE id = ?",
            (security.hash_password(password), 1 if args.activate else 0, user["id"]),
        )
        conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (timestamp, user["id"]),
        )
        conn.commit()
        status = "active" if args.activate else user["status"]
        print(f"Password reset for {user['email']} ({user['role']}, {status}). "
              "Existing sessions were signed out.")
        if status != "active":
            print(f"Note: the account status is {status!r}. Use --activate if it should sign in now.")
        return 0
    finally:
        conn.close()


def cmd_create_admin(args, settings: Settings) -> int:
    conn = _open(settings, init=True)
    try:
        email = args.email.strip().lower()
        if not security.EMAIL_RE.match(email):
            print("Invalid email address.", file=sys.stderr)
            return 1
        if _find_user(conn, email):
            print(f"An account with email {email!r} already exists.", file=sys.stderr)
            return 1
        password = _require_password(args.password)
        name = args.name.strip() or "Administrator"
        user_id = new_id()
        timestamp = now()
        conn.execute(
            "INSERT INTO users(id, email, email_norm, password_hash, display_name, role, "
            "status, created_at, approved_at) VALUES (?,?,?,?,?, 'admin', 'active', ?, ?)",
            (user_id, email, email, security.hash_password(password), name, timestamp, timestamp),
        )
        conn.commit()
        print(f"Administrator created: {email} (id {user_id}). Sign in and link it to a person "
              "from Admin → Users if needed.")
        return 0
    finally:
        conn.close()


def cmd_unlock(args, settings: Settings) -> int:
    conn = _open(settings)
    try:
        user = _find_user(conn, args.email)
        if user is None:
            print(f"No account with email {args.email!r}.", file=sys.stderr)
            return 1
        conn.execute(
            "UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = ?",
            (user["id"],),
        )
        conn.commit()
        print(f"Unlocked {user['email']}.")
        return 0
    finally:
        conn.close()


def cmd_setup_token(args, settings: Settings) -> int:
    conn = _open(settings, init=True)
    try:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()
    if count:
        print("Setup is already complete; /setup is disabled. Use list-users/reset-password.")
        return 1
    token = ensure_setup_token(settings)
    print(f"Setup token: {token}")
    print(f"Open {settings.base_url}/setup and paste it.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.manage",
                                     description="Family Tree operator commands")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-users", help="List accounts (never shows password hashes)")

    reset = sub.add_parser("reset-password", help="Set a new password and revoke sessions")
    reset.add_argument("--email", required=True)
    reset.add_argument("--password", help="Omit to be prompted securely")
    reset.add_argument("--activate", action="store_true",
                       help="Also set status to active (use for disabled/pending accounts)")

    create = sub.add_parser("create-admin", help="Create or recover an administrator account")
    create.add_argument("--email", required=True)
    create.add_argument("--name", default="Administrator")
    create.add_argument("--password", help="Omit to be prompted securely")

    unlock = sub.add_parser("unlock", help="Clear a brute-force lockout")
    unlock.add_argument("--email", required=True)

    sub.add_parser("setup-token", help="Print the first-run setup token (no accounts only)")
    return parser


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = settings or _settings()
    security.configure(settings)
    handlers = {
        "list-users": cmd_list,
        "reset-password": cmd_reset,
        "create-admin": cmd_create_admin,
        "unlock": cmd_unlock,
        "setup-token": cmd_setup_token,
    }
    return handlers[args.command](args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
