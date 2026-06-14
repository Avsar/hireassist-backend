"""
feedback.py -- Stores user feedback and emails a notification (via Resend).
Reuses the same SQLite DB and email pipeline as the job-alert system.
"""

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from html import escape

from db_config import get_db_path
from job_alerts import _send_email

logger = logging.getLogger(__name__)
DB_FILE = get_db_path()

# Where feedback notifications are sent. Override with the FEEDBACK_TO env var.
FEEDBACK_TO = os.environ.get("FEEDBACK_TO", "success@cubea.nl")


def ensure_feedback_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            message     TEXT NOT NULL,
            email       TEXT,
            page        TEXT,
            user_agent  TEXT,
            created_at  TEXT NOT NULL
        )
    """)
    conn.commit()


def save_feedback(message: str, email: str = "", page: str = "", user_agent: str = "") -> dict:
    message = (message or "").strip()
    if not message:
        return {"ok": False, "message": "Please enter a message."}
    message = message[:5000]
    email = (email or "").strip()[:200]
    page = (page or "").strip()[:500]
    user_agent = (user_agent or "").strip()[:300]
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(DB_FILE)
    ensure_feedback_table(conn)
    conn.execute(
        "INSERT INTO feedback (message, email, page, user_agent, created_at) VALUES (?, ?, ?, ?, ?)",
        (message, email, page, user_agent, now),
    )
    conn.commit()
    conn.close()

    # Email the notification in the background so the API responds immediately.
    def _notify():
        try:
            html = f"""\
<div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;">
  <h2 style="color:#0d9488;">New CubeA feedback</h2>
  <p style="white-space:pre-wrap;font-size:14px;color:#111;">{escape(message)}</p>
  <hr style="border:none;border-top:1px solid #e5e7eb;margin:16px 0;">
  <p style="font-size:12px;color:#6b7280;">
    From: {escape(email) if email else "(anonymous)"}<br>
    Page: {escape(page) if page else "(unknown)"}<br>
    {escape(now)}
  </p>
</div>"""
            _send_email(FEEDBACK_TO, "New CubeA feedback", html)
        except Exception as e:
            logger.error("Failed to send feedback notification: %s", e)

    threading.Thread(target=_notify, daemon=True).start()
    return {"ok": True, "message": "Thanks! Your feedback has been sent."}
