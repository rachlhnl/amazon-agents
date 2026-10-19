"""Email the brief to the team. Sends only when SMTP settings and recipients are
configured; otherwise it's a no-op and the brief stays in reports/."""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def email_configured() -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv("BRIEF_TO") and os.getenv("BRIEF_FROM"))


def send_brief(markdown: str, subject: str) -> bool:
    if not email_configured():
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["BRIEF_FROM"]
    msg["To"] = ", ".join(x.strip() for x in os.environ["BRIEF_TO"].split(",") if x.strip())
    msg.set_content(markdown)

    host, port = os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        if os.getenv("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)
    return True
