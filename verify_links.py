"""
verify_links.py -- Detect and deactivate dead apply links.

Checks active career-page jobs (the ones we scraped ourselves -- ATS API
links are provider-hosted and rarely break) and deactivates any that
return 404/410. Run weekly, or after user reports of broken links.

Usage:
    python verify_links.py                      # check careers_page jobs
    python verify_links.py --dry-run            # report only
    python verify_links.py --company "Prodrive Technologies"
    python verify_links.py --limit 500
"""

import argparse
import sqlite3
import time

import requests

from db_config import get_db_path

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HireAssist link checker)"}
DEAD_STATUSES = {404, 410}


def check_url(url: str) -> int | None:
    """Return HTTP status, or None on network error (treated as inconclusive)."""
    try:
        r = requests.head(url, headers=HEADERS, timeout=12, allow_redirects=True)
        # Many servers reject HEAD; retry with GET before judging
        if r.status_code in (403, 405) or r.status_code >= 500:
            r = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True, stream=True)
            r.close()
        return r.status_code
    except requests.RequestException:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--company", help="Check a single company")
    ap.add_argument("--limit", type=int, default=3000)
    ap.add_argument("--delay", type=float, default=0.4)
    args = ap.parse_args()

    conn = sqlite3.connect(get_db_path())
    clauses = ["is_active = 1", "source = 'careers_page'", "url != ''"]
    params: list = []
    if args.company:
        clauses.append("company_name = ?")
        params.append(args.company)

    rows = conn.execute(
        f"SELECT id, company_name, title, url FROM jobs WHERE {' AND '.join(clauses)} "
        f"ORDER BY company_name LIMIT ?",
        params + [args.limit],
    ).fetchall()

    print(f"Checking {len(rows)} career-page job links "
          f"({'dry-run' if args.dry_run else 'will deactivate dead ones'})...\n")

    dead, alive, unknown = [], 0, 0
    last_company = None
    for job_id, company, title, url in rows:
        if company != last_company:
            print(f"  {company}")
            last_company = company
        status = check_url(url)
        if status in DEAD_STATUSES:
            dead.append((job_id, company, title, url, status))
            print(f"    [DEAD {status}] {title[:45]}")
        elif status is None:
            unknown += 1
        else:
            alive += 1
        time.sleep(args.delay)

    print(f"\nAlive: {alive}   Dead: {len(dead)}   Inconclusive: {unknown}")

    if dead and not args.dry_run:
        conn.executemany(
            "UPDATE jobs SET is_active = 0 WHERE id = ?",
            [(d[0],) for d in dead],
        )
        conn.commit()
        print(f"Deactivated {len(dead)} dead jobs.")
        companies = sorted({d[1] for d in dead})
        print("Affected companies (consider --debug-company in agent_scrape.py):")
        for c in companies:
            print(f"  - {c}")

    conn.close()


if __name__ == "__main__":
    main()
