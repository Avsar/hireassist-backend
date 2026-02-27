"""
job_alerts.py -- Job alert email system for HireAssist.

Handles:
  - Database schema for alert subscriptions
  - Sending confirmation emails (double opt-in)
  - Matching new jobs to alert filter criteria
  - Sending daily digest emails via SMTP
"""

import json
import logging
import os
import re
import secrets
import smtplib
import sqlite3
from datetime import date, datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from db_config import get_db_path

logger = logging.getLogger(__name__)
DB_FILE = get_db_path()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def ensure_alerts_table(conn: sqlite3.Connection):
    """Create the job_alerts table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_alerts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            email           TEXT NOT NULL,
            token           TEXT NOT NULL UNIQUE,
            filters_json    TEXT NOT NULL DEFAULT '{}',
            is_confirmed    INTEGER NOT NULL DEFAULT 0,
            is_active       INTEGER NOT NULL DEFAULT 1,
            created_at      TEXT NOT NULL,
            confirmed_at    TEXT,
            last_sent_at    TEXT,
            last_match_count INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_token ON job_alerts(token)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_active ON job_alerts(is_confirmed, is_active)")
    conn.commit()


def create_alert(email: str, filters_json: str) -> dict:
    """Create a new unconfirmed alert. Returns {"ok": bool, "message": str}."""
    email = email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return {"ok": False, "message": "Please enter a valid email address."}

    conn = sqlite3.connect(DB_FILE)
    ensure_alerts_table(conn)

    # Max 3 active alerts per email
    count = conn.execute(
        "SELECT COUNT(*) FROM job_alerts WHERE email = ? AND is_active = 1",
        (email,),
    ).fetchone()[0]
    if count >= 3:
        conn.close()
        return {"ok": False, "message": "You already have 3 active alerts. Unsubscribe from one first."}

    token = secrets.token_hex(16)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO job_alerts (email, token, filters_json, is_confirmed, is_active, created_at) VALUES (?, ?, ?, 0, 1, ?)",
        (email, token, filters_json, now),
    )
    conn.commit()
    conn.close()

    # Send confirmation email (non-blocking on failure)
    try:
        send_confirmation_email(email, token)
    except Exception as e:
        logger.error("Failed to send confirmation email to %s: %s", email, e)
        return {"ok": True, "message": "Alert saved but we could not send the confirmation email. Please try again later."}

    return {"ok": True, "message": "Check your email to confirm your alert."}


def confirm_alert(token: str) -> str | None:
    """Confirm an alert by token. Returns the email or None if not found."""
    conn = sqlite3.connect(DB_FILE)
    ensure_alerts_table(conn)
    row = conn.execute("SELECT id, email, is_confirmed FROM job_alerts WHERE token = ?", (token,)).fetchone()
    if not row:
        conn.close()
        return None
    if not row[2]:  # not yet confirmed
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("UPDATE job_alerts SET is_confirmed = 1, confirmed_at = ? WHERE id = ?", (now, row[0]))
        conn.commit()
    conn.close()
    return row[1]


def unsubscribe_alert(token: str) -> str | None:
    """Unsubscribe an alert by token. Returns the email or None if not found."""
    conn = sqlite3.connect(DB_FILE)
    ensure_alerts_table(conn)
    row = conn.execute("SELECT id, email FROM job_alerts WHERE token = ?", (token,)).fetchone()
    if not row:
        conn.close()
        return None
    conn.execute("UPDATE job_alerts SET is_active = 0 WHERE id = ?", (row[0],))
    conn.commit()
    conn.close()
    return row[1]


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------

def _get_base_url() -> str:
    return (os.environ.get("ALERTS_BASE_URL")
            or os.environ.get("RENDER_URL", "").rstrip("/")
            or "http://localhost:8000")


def _send_email(to: str, subject: str, html_body: str):
    """Send an HTML email via SMTP."""
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    from_addr = os.environ.get("SMTP_FROM", user)

    if not host or not user or not password:
        logger.warning("SMTP not configured -- skipping email to %s", to)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"HireAssist <{from_addr}>"
    msg["To"] = to

    plain = f"{subject}\n\nPlease view this email in an HTML-capable client."
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(from_addr, [to], msg.as_string())

    logger.info("Email sent to %s: %s", to, subject)


# ---------------------------------------------------------------------------
# Confirmation email
# ---------------------------------------------------------------------------

def send_confirmation_email(email: str, token: str):
    base = _get_base_url()
    confirm_url = f"{base}/api/alerts/confirm?token={token}"

    html = f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:500px;margin:0 auto;padding:20px;">
  <h2 style="color:#0d9488;margin-bottom:16px;">Confirm your job alert</h2>
  <p style="color:#374151;font-size:14px;line-height:1.6;">
    You requested a job alert on HireAssist. Click the button below to activate it:
  </p>
  <div style="text-align:center;margin:24px 0;">
    <a href="{confirm_url}" style="background:#0d9488;color:white;padding:12px 32px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;display:inline-block;">
      Confirm my alert
    </a>
  </div>
  <p style="color:#6b7280;font-size:12px;">
    If you did not request this, you can safely ignore this email.
  </p>
</div>"""
    _send_email(email, "Confirm your HireAssist job alert", html)


# ---------------------------------------------------------------------------
# Job matching
# ---------------------------------------------------------------------------

def match_jobs_for_alert(conn: sqlite3.Connection, filters: dict, today: str | None = None) -> list[dict]:
    """Find new jobs (first_seen_at = today) matching alert filters."""
    if today is None:
        today = date.today().isoformat()

    # Lazy imports to avoid circular dependency at module load time
    from app import CITY_TO_PROVINCE, title_looks_dutch, title_looks_english, soft_country_match, _normalize_city

    conn.row_factory = sqlite3.Row

    clauses = ["is_active = 1", "DATE(first_seen_at) = ?"]
    params: list = [today]

    company = filters.get("company", "")
    if company:
        clauses.append("company_name = ?")
        params.append(company)

    q = filters.get("q", "")
    if q:
        clauses.append("LOWER(title) LIKE ?")
        params.append(f"%{q.lower()}%")

    where = " AND ".join(clauses)
    rows = conn.execute(f"SELECT * FROM jobs WHERE {where} ORDER BY company_name, title", params).fetchall()

    jobs = []
    for r in rows:
        jobs.append({
            "company": r["company_name"],
            "title": r["title"],
            "city": _normalize_city(r["city"]) or "",
            "country": r["country"] or "",
            "location_raw": r["location_raw"] or "",
            "url": r["url"] or "",
            "tech_tags": (r["tech_tags"] if "tech_tags" in r.keys() else "") or "",
        })

    # Country filter
    country = filters.get("country", "")
    if country:
        jobs = [j for j in jobs if soft_country_match(j, country)]

    # City / province filter
    city = filters.get("city", "")
    if city and city.startswith("province:"):
        prov = city[len("province:"):]
        prov_cities = {c.lower() for c, p in CITY_TO_PROVINCE.items() if p.lower() == prov.lower()}
        jobs = [j for j in jobs if (j.get("city") or "").lower() in prov_cities]
    elif city:
        jobs = [j for j in jobs if (j.get("city") or "").lower() == city.lower()]

    # Tech filter
    tech = filters.get("tech", "")
    if tech:
        tech_lower = tech.lower()
        jobs = [j for j in jobs if tech_lower in (j.get("tech_tags") or "").lower().split("|")]

    # Language filter
    lang = filters.get("lang", "")
    english_only = filters.get("english_only", False)
    effective_lang = lang or ("en" if english_only else "")
    if effective_lang == "en":
        jobs = [j for j in jobs if title_looks_english(j.get("title", ""))]
    elif effective_lang == "nl":
        jobs = [j for j in jobs if title_looks_dutch(j.get("title", ""))]

    # Hide stale (shouldn't matter for today's jobs, but respect the filter)
    if filters.get("hide_stale"):
        pass  # jobs first_seen today are never stale

    return jobs


# ---------------------------------------------------------------------------
# Digest email
# ---------------------------------------------------------------------------

def _build_digest_html(jobs: list[dict], filters: dict, token: str) -> str:
    base = _get_base_url()
    unsubscribe_url = f"{base}/api/alerts/unsubscribe?token={token}"

    # Filter summary
    parts = []
    if filters.get("q"):
        parts.append(f'Keyword: "{filters["q"]}"')
    if filters.get("company"):
        parts.append(f'Company: {filters["company"]}')
    if filters.get("city"):
        parts.append(f'Location: {filters["city"]}')
    if filters.get("tech"):
        parts.append(f'Tech: {filters["tech"]}')
    if filters.get("english_only"):
        parts.append("English only")
    filter_summary = ", ".join(parts) if parts else "All jobs in Netherlands"

    # Job rows (max 25)
    display_jobs = jobs[:25]
    remaining = len(jobs) - 25 if len(jobs) > 25 else 0

    job_rows = ""
    for j in display_jobs:
        title = j.get("title", "")
        company = j.get("company", "")
        city = j.get("city", "")
        url = j.get("url", "")
        loc = f" -- {city}" if city else ""
        job_rows += f"""\
<tr><td style="padding:12px 0;border-bottom:1px solid #f3f4f6;">
  <a href="{url}" style="color:#0d9488;font-weight:600;font-size:14px;text-decoration:none;">{title}</a>
  <div style="color:#6b7280;font-size:12px;margin-top:2px;">{company}{loc}</div>
</td></tr>
"""

    more_text = ""
    if remaining:
        more_text = f'<p style="color:#6b7280;font-size:13px;">...and {remaining} more. <a href="{base}/ui" style="color:#0d9488;">View all on HireAssist</a></p>'

    count = len(jobs)
    s = "s" if count != 1 else ""
    return f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:600px;margin:0 auto;padding:20px;">
  <div style="text-align:center;margin-bottom:20px;">
    <span style="font-size:20px;font-weight:800;color:#0f172a;">HireAssist</span>
  </div>
  <h2 style="color:#0f172a;font-size:18px;margin-bottom:4px;">{count} new job{s} matching your alert</h2>
  <p style="color:#6b7280;font-size:13px;margin-bottom:20px;">Filters: {filter_summary}</p>
  <table style="width:100%;border-collapse:collapse;">{job_rows}</table>
  {more_text}
  <div style="margin-top:24px;text-align:center;">
    <a href="{base}/ui" style="background:#0d9488;color:white;padding:10px 28px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;display:inline-block;">View all jobs</a>
  </div>
  <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0;">
  <p style="color:#9ca3af;font-size:11px;text-align:center;">
    You're receiving this because you set up a job alert on HireAssist.<br>
    <a href="{unsubscribe_url}" style="color:#9ca3af;">Unsubscribe</a>
  </p>
</div>"""


def send_daily_digests(today: str | None = None) -> dict:
    """Send daily digest emails for all confirmed, active alerts.

    Returns summary: {"alerts_checked", "emails_sent", "total_jobs_matched"}.
    """
    if today is None:
        today = date.today().isoformat()

    stats = {"alerts_checked": 0, "emails_sent": 0, "total_jobs_matched": 0}

    conn = sqlite3.connect(DB_FILE)
    ensure_alerts_table(conn)
    conn.row_factory = sqlite3.Row

    alerts = conn.execute(
        "SELECT * FROM job_alerts WHERE is_confirmed = 1 AND is_active = 1"
    ).fetchall()
    stats["alerts_checked"] = len(alerts)

    for alert in alerts:
        try:
            filters = json.loads(alert["filters_json"])
            matched = match_jobs_for_alert(conn, filters, today)
            if not matched:
                continue

            html = _build_digest_html(matched, filters, alert["token"])
            count = len(matched)
            s = "s" if count != 1 else ""
            subject = f"{count} new job{s} matching your HireAssist alert"
            _send_email(alert["email"], subject, html)

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE job_alerts SET last_sent_at = ?, last_match_count = ? WHERE id = ?",
                (now, count, alert["id"]),
            )
            conn.commit()

            stats["emails_sent"] += 1
            stats["total_jobs_matched"] += count
        except Exception as e:
            logger.error("Alert %d (%s) failed: %s", alert["id"], alert["email"], e)

    conn.close()
    return stats
