"""Mail delivery abstraction.

No SMTP is configured in this phase: links are created in the admin UI and
copied by hand. Setting SMTP_* environment variables switches the app to
SmtpMailer, which is the hook for later invite/reset/notification mail.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from .config import Settings

log = logging.getLogger("familytree.mail")


class Mailer:
    def send(self, to: str, subject: str, body: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def send_invite(self, to: str, link: str, inviter: str) -> bool:
        if not to:
            return False
        return self.send(to, "You are invited to the family tree",
                         f"{inviter} invited you to the family tree.\n\n{link}\n")

    def send_reset(self, to: str, link: str) -> bool:
        if not to:
            return False
        return self.send(to, "Your family tree password was reset",
                         f"Use this link to choose a new password:\n\n{link}\n")

    def send_pending_notice(self, to: str, who: str) -> bool:
        if not to:
            return False
        return self.send(to, "A family tree change is awaiting review",
                         f"{who} submitted a change that needs your approval.\n")


class NullMailer(Mailer):
    def send(self, to: str, subject: str, body: str) -> bool:
        log.info("mail disabled, would send %r to %s", subject, to)
        return False


class SmtpMailer(Mailer):
    def __init__(self, settings: Settings):
        self.settings = settings

    def send(self, to: str, subject: str, body: str) -> bool:
        if not self.settings.smtp_host or not to:
            return False
        message = EmailMessage()
        message["From"] = self.settings.smtp_from or self.settings.smtp_user
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        try:
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=15) as smtp:
                if self.settings.smtp_starttls:
                    smtp.starttls()
                if self.settings.smtp_user:
                    smtp.login(self.settings.smtp_user, self.settings.smtp_password)
                smtp.send_message(message)
            return True
        except Exception as exc:  # noqa: BLE001 - mail must never break a request
            log.warning("SMTP send failed: %s", exc)
            return False


def get_mailer(settings: Settings) -> Mailer:
    if settings.smtp_host:
        return SmtpMailer(settings)
    return NullMailer()
