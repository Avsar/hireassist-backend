"""
sync_ats_jobs.py -- Sync ATS job listings into the jobs intelligence table.

Pulls jobs from all ATS-based companies (Greenhouse, Lever, SmartRecruiters,
Recruitee) using the existing fetch logic in app.py, then upserts into the
`jobs` table via job_intel.py.

Usage:
    python sync_ats_jobs.py                    # sync all ATS companies
    python sync_ats_jobs.py --company "Adyen"  # sync one company
    python sync_ats_jobs.py --dry-run          # preview without writing
"""

import argparse
import sqlite3
import time
from pathlib import Path

from app import load_companies, normalize_jobs
from db_config import get_db_path

DB_FILE = get_db_path()
from job_intel import ensure_intel_tables, upsert_jobs

ATS_SOURCES = {"greenhouse", "lever", "smartrecruiters", "recruitee"}


def sync_all(company_filter: str | None = None, dry_run: bool = False):
    conn = sqlite3.connect(DB_FILE)
    ensure_intel_tables(conn)

    companies = load_companies()
    companies = [c for c in companies if c["source"] in ATS_SOURCES]

    if company_filter:
        companies = [c for c in companies if c["name"].lower() == company_filter.lower()]

    prefix = "[DRY RUN] " if dry_run else ""
    print(f"{prefix}Syncing {len(companies)} ATS companies ...\n")

    totals = {"attempted": 0, "new": 0, "updated": 0, "deactivated": 0, "errors": 0}

    for c in companies:
        totals["attempted"] += 1
        name, source, token = c["name"], c["source"], c["token"]
        label = f"  {name:<35} ({source})"
        print(label.encode("ascii", "replace").decode("ascii"), end=" ", flush=True)

        try:
            job_dicts = normalize_jobs(name, source, token)
            job_dicts = [j for j in job_dicts if j.get("title")]
        except Exception as e:
            print(f"[ERR] {e}")
            totals["errors"] += 1
            continue

        if dry_run:
            print(f"[OK] {len(job_dicts)} jobs (dry-run)")
            continue

        result = upsert_jobs(conn, source, name, job_dicts)
        print(f"[OK] {len(job_dicts)} jobs "
              f"(+{result['new']} new, ~{result['updated']} upd, -{result['deactivated']} closed)")

        totals["new"] += result["new"]
        totals["updated"] += result["updated"]
        totals["deactivated"] += result["deactivated"]

        time.sleep(0.5)

    conn.close()

    print(f"\n{'=' * 50}")
    print(f"  ATS SYNC SUMMARY")
    print(f"{'=' * 50}")
    print(f"  Attempted:    {totals['attempted']}")
    print(f"  New jobs:     {totals['new']}")
    print(f"  Updated:      {totals['updated']}")
    print(f"  Deactivated:  {totals['deactivated']}")
    print(f"  Errors:       {totals['errors']}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync ATS jobs into intelligence layer")
    parser.add_argument("--company", help="Sync a single company by name")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB")
    args = parser.parse_args()
    sync_all(company_filter=args.company, dry_run=args.dry_run)
