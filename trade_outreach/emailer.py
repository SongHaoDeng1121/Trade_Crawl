from __future__ import annotations

import smtplib
from email.message import EmailMessage

from .config import Settings


def build_email(settings: Settings, recipient: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.mail_from
    msg["To"] = recipient
    if settings.mail_cc:
        msg["Cc"] = settings.mail_cc
    if settings.mail_bcc:
        msg["Bcc"] = settings.mail_bcc
    msg["Subject"] = subject
    msg["Reply-To"] = settings.mail_reply_to
    msg.set_content(body)
    return msg


def send_email(settings: Settings, recipient: str, subject: str, body: str) -> None:
    if not settings.smtp_host:
        raise RuntimeError("SMTP_HOST is not configured")

    msg = build_email(settings, recipient, subject, body)
    with open_smtp(settings) as smtp:
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


def test_smtp(settings: Settings) -> None:
    if not settings.smtp_host:
        raise RuntimeError("SMTP_HOST is not configured")
    if not settings.mail_from or "@" not in settings.mail_from:
        raise RuntimeError("MAIL_FROM must be a valid sender email")

    with open_smtp(settings) as smtp:
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.noop()


def open_smtp(settings: Settings) -> smtplib.SMTP:
    security = settings.smtp_security
    if security == "ssl":
        smtp = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30)
        smtp.ehlo()
        return smtp

    smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    smtp.ehlo()
    if security == "starttls":
        smtp.starttls()
        smtp.ehlo()
    elif security != "none":
        smtp.close()
        raise RuntimeError("SMTP_SECURITY must be starttls, ssl, or none")
    return smtp
