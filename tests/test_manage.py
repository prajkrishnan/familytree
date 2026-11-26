"""Operator CLI (python -m app.manage) for locked-out installations."""
from __future__ import annotations

import sqlite3

from conftest import ADMIN_EMAIL, ADMIN_PASSWORD, csrf, login, start_setup
from app import manage


def _manage(tmp_path, args):
    from app.config import load_settings
    settings = load_settings(data_dir=str(tmp_path), setup_token="setup-token",
                             base_url="http://testserver", argon2_time_cost=1,
                             argon2_memory_kib=8192, argon2_parallelism=1)
    return manage.main(args, settings=settings)


def test_list_users_never_shows_hashes(admin, tmp_path, capsys):
    code = _manage(tmp_path, ["list-users"])
    out = capsys.readouterr().out
    assert code == 0
    assert ADMIN_EMAIL in out
    assert "argon2" not in out.lower()
    assert "$argon2" not in out


def test_reset_password_and_unlock(admin, tmp_path, capsys):
    # Lock the account the way repeated failures would.
    with sqlite3.connect(admin.app.state.settings.db_path) as conn:
        conn.execute("UPDATE users SET failed_attempts = 9, locked_until = 9999999999")
        conn.commit()
    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})
    response = login(admin)
    assert response.status_code == 429

    code = _manage(tmp_path, ["unlock", "--email", ADMIN_EMAIL])
    assert code == 0
    response = login(admin)
    assert response.status_code == 303
    # Undo the login so the reset check below starts from a clean state.
    token = csrf(admin, "/tree")
    admin.post("/logout", data={"_csrf": token})

    code = _manage(tmp_path, ["reset-password", "--email", "ADMIN@example.com",
                                        "--password", "recovered-password-1"])
    assert code == 0
    # The reset revokes sessions.
    assert admin.get("/api/tree").status_code == 401
    assert login(admin, password=ADMIN_PASSWORD).status_code == 400
    assert login(admin, password="recovered-password-1").status_code == 303


def test_reset_password_rejects_weak_password(admin, tmp_path):
    import pytest
    with pytest.raises(SystemExit):
        _manage(tmp_path, ["reset-password", "--email", ADMIN_EMAIL,
                                     "--password", "short"])


def test_reset_password_for_unknown_email_lists_accounts(admin, tmp_path, capsys):
    code = _manage(tmp_path, ["reset-password", "--email", "nobody@example.com",
                                        "--password", "recovered-password-1"])
    assert code == 1
    out = capsys.readouterr().out
    assert ADMIN_EMAIL in out


def test_create_admin_bootstraps_an_empty_install(tmp_path):
    code = _manage(tmp_path / "fresh",
                ["create-admin", "--email", "recover@example.com", "--name", "Recovery",
                 "--password", "recovery-password-1"])
    assert code == 0
    with sqlite3.connect(tmp_path / "fresh" / "family.db") as conn:
        row = conn.execute("SELECT role, status FROM users WHERE email_norm = ?",
                           ("recover@example.com",)).fetchone()
    assert row == ("admin", "active")
    # A second run refuses to overwrite the existing account.
    code = _manage(tmp_path / "fresh",
                ["create-admin", "--email", "recover@example.com",
                 "--password", "recovery-password-2"])
    assert code == 1


def test_create_admin_adds_another_admin(admin, tmp_path):
    code = _manage(tmp_path, ["create-admin", "--email", "other@example.com",
                                        "--name", "Other Admin", "--password", "other-password-1"])
    assert code == 0
    with sqlite3.connect(admin.app.state.settings.db_path) as conn:
        row = conn.execute("SELECT role, status FROM users WHERE email_norm = ?",
                           ("other@example.com",)).fetchone()
    assert row == ("admin", "active")
