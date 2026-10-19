"""Email the brief. The full brief goes to BRIEF_TO; each team's part goes to
BRIEF_TO_<TEAM> (e.g. BRIEF_TO_MARKETING) when set. Sends only when SMTP settings
and recipients are configured; otherwise it's a no-op and briefs stay in reports/."""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def email_configured(to_var: str = "BRIEF_TO") -> bool:
    return bool(os.getenv("SMTP_HOST") and os.getenv(to_var) and os.getenv("BRIEF_FROM"))


def team_recipients_var(team: str) -> str:
    return f"BRIEF_TO_{team.upper()}"


def send_brief(markdown: str, subject: str, to_var: str = "BRIEF_TO") -> bool:
    if not email_configured(to_var):
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["BRIEF_FROM"]
    msg["To"] = ", ".join(x.strip() for x in os.environ[to_var].split(",") if x.strip())
    msg.set_content(markdown)

    host, port = os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        if os.getenv("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        s.send_message(msg)
    return True
