"""
enrich_descriptions.py -- Backfill descriptions for careers_page (hidden-gem)
jobs that have none, by fetching each job's own page and extracting the body.

Runs on the web service (it already has the jobs + their apply URLs). It ONLY
UPDATEs the `description` column on existing rows -- it never inserts, deletes,
or deactivates anything, so it can't damage live data. Processes a bounded batch
per call, so it's safe to run in the daily cron and backfill gradually while
keeping up with newly-scraped gems.

Extraction is "readability" style with BeautifulSoup (no extra dependency, free):
strip non-content elements, take the main/article/body text. Pages that are
JS-rendered (little server HTML) yield too little text and are skipped -- those
remain blank until/unless we add an AI or headless pass later.
"""

import logging
import sqlite3
import time

import requests
from bs4 import BeautifulSoup

from db_config import get_db_path

logger = logging.getLogger(__name__)
DB_FILE = get_db_path()
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CubeABot/1.0; +https://cubea.nl)"}
MAX_LEN = 1500
MIN_LEN = 120  # below this we treat it as "no real description found" and skip


def _extract(html: str) -> str:
    """Readability-style main-text extraction. Returns plain text (capped)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "form",
                     "noscript", "svg", "iframe", "aside"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    lines = [ln.strip() for ln in main.get_text("\n", strip=True).splitlines() if ln.strip()]
    return "\n".join(lines).strip()[:MAX_LEN]


def enrich_descriptions(limit: int = 150) -> dict:
    """Fetch + extract descriptions for up to `limit` active careers_page jobs
    that currently have none. Column-only updates; returns a summary."""
    conn = sqlite3.connect(DB_FILE, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, url FROM jobs
           WHERE source = 'careers_page' AND is_active = 1
             AND (description IS NULL OR TRIM(description) = '')
             AND url IS NOT NULL AND url != ''
           ORDER BY first_seen_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()

    updated = skipped = errors = 0
    for r in rows:
        try:
            resp = requests.get(r["url"], headers=HEADERS, timeout=12)
            ctype = resp.headers.get("content-type", "")
            if resp.status_code != 200 or "text/html" not in ctype:
                skipped += 1
                continue
            desc = _extract(resp.text)
            if len(desc) < MIN_LEN:
                skipped += 1  # likely JS-rendered or not a real description page
                continue
            conn.execute("UPDATE jobs SET description = ? WHERE id = ?", (desc, r["id"]))
            conn.commit()
            updated += 1
        except Exception as e:
            errors += 1
            logger.debug("enrich error for job %s: %s", r["id"], e)
        time.sleep(0.2)

    remaining = conn.execute(
        """SELECT COUNT(*) FROM jobs
           WHERE source = 'careers_page' AND is_active = 1
             AND (description IS NULL OR TRIM(description) = '')"""
    ).fetchone()[0]
    conn.close()

    return {
        "ok": True,
        "checked": len(rows),
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "still_missing": remaining,
    }
