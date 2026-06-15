"""
verify_hidden.py -- Verify whether "hidden gem" jobs actually appear on the big
job boards (LinkedIn / Indeed), using the Serper SERP API.

We never scrape the boards directly: we query Google via Serper and inspect the
result URLs. Results are stored in gem_verifications so we don't re-check, and so
the read path can later expose a trustworthy "verified hidden" flag.
"""

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

import requests

from db_config import get_db_path

logger = logging.getLogger(__name__)
DB_FILE = get_db_path()
SERPER_KEY = os.environ.get("SERPER_API_KEY", "")
SERPER_URL = "https://google.serper.dev/search"


def ensure_verif_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gem_verifications (
            job_key      TEXT PRIMARY KEY,
            on_boards    INTEGER NOT NULL,
            source_found TEXT,
            checked_at   TEXT NOT NULL
        )
    """)
    conn.commit()


def _on_boards(title: str, company: str) -> tuple[bool, str]:
    """One Serper query: is this exact role findable on LinkedIn or Indeed?
    Returns (found, which_board). Raises on API error."""
    q = f'"{title}" {company} (site:linkedin.com OR site:indeed.com OR site:nl.indeed.com)'
    resp = requests.post(
        SERPER_URL,
        headers={"X-API-KEY": SERPER_KEY, "Content-Type": "application/json"},
        json={"q": q, "gl": "nl", "num": 10},
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Serper {resp.status_code}: {resp.text[:200]}")
    for r in resp.json().get("organic", []):
        link = (r.get("link") or "").lower()
        if "linkedin.com" in link:
            return True, "linkedin"
        if "indeed.com" in link:
            return True, "indeed"
    return False, ""


def verify_gems(limit: int = 50, recheck: bool = False) -> dict:
    """Check up to `limit` active hidden gems (hidden_tier >= 2) against the boards.
    Skips already-checked jobs unless recheck=True. Returns a summary with the
    measured leak rate for this run and cumulative totals."""
    if not SERPER_KEY:
        return {"ok": False, "error": "SERPER_API_KEY not set"}

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    ensure_verif_table(conn)

    rows = conn.execute(
        "SELECT job_key, title, company_name FROM jobs "
        "WHERE is_active = 1 AND hidden_tier >= 2 ORDER BY first_seen_at DESC"
    ).fetchall()

    checked = on_boards = errors = 0
    for r in rows:
        if checked >= limit:
            break
        if not recheck:
            seen = conn.execute(
                "SELECT 1 FROM gem_verifications WHERE job_key = ?", (r["job_key"],)
            ).fetchone()
            if seen:
                continue
        try:
            found, src = _on_boards(r["title"], r["company_name"])
        except Exception as e:
            errors += 1
            logger.error("verify error for %s: %s", r["job_key"], e)
            time.sleep(1.0)
            continue
        conn.execute(
            "INSERT OR REPLACE INTO gem_verifications (job_key, on_boards, source_found, checked_at) "
            "VALUES (?, ?, ?, ?)",
            (r["job_key"], 1 if found else 0, src, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        checked += 1
        if found:
            on_boards += 1
        time.sleep(0.2)

    total = conn.execute("SELECT COUNT(*) FROM gem_verifications").fetchone()[0]
    total_on = conn.execute("SELECT COUNT(*) FROM gem_verifications WHERE on_boards = 1").fetchone()[0]
    conn.close()

    return {
        "ok": True,
        "checked_this_run": checked,
        "on_boards_this_run": on_boards,
        "errors": errors,
        "leak_pct_this_run": round(100 * on_boards / checked, 1) if checked else 0,
        "total_verified": total,
        "total_on_boards": total_on,
        "total_leak_pct": round(100 * total_on / total, 1) if total else 0,
    }
