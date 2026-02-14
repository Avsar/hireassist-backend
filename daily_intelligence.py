"""
daily_intelligence.py -- Daily intelligence pipeline orchestrator.

Runs the full pipeline in sequence:
  1. Discovery (agent_discover.py) -- find new companies via OSM
  2. Scrape (agent_scrape.py) -- scrape career pages
  3. ATS Sync (sync_ats_jobs.py) -- sync ATS job listings into jobs table
  4. Stats (job_intel.py) -- compute daily stats + momentum

Usage:
    python daily_intelligence.py                          # full pipeline
    python daily_intelligence.py --skip-discover          # skip OSM discovery
    python daily_intelligence.py --skip-scrape            # skip career scraping
    python daily_intelligence.py --stats-only             # only recompute stats
    python daily_intelligence.py --region "Amsterdam"     # pass region to discovery
"""

import argparse
import sqlite3
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from db_config import get_db_path

DB_FILE = get_db_path()


def run_step(label: str, cmd: list) -> bool:
    """Run a subprocess step and return True on success."""
    print(f"\n{'=' * 60}")
    print(f"  STEP: {label}")
    print(f"  CMD:  {' '.join(cmd)}")
    print(f"{'=' * 60}\n")

    start = time.time()
    result = subprocess.run(cmd, cwd=str(Path(__file__).parent))
    elapsed = time.time() - start

    status = "OK" if result.returncode == 0 else "FAILED"
    print(f"\n  [{status}] {label} ({elapsed:.1f}s)")
    return result.returncode == 0


def compute_stats():
    """Compute daily stats directly (no subprocess)."""
    from job_intel import compute_daily_stats, ensure_intel_tables

    print(f"\n{'=' * 60}")
    print(f"  STEP: Compute daily stats")
    print(f"{'=' * 60}\n")

    conn = sqlite3.connect(DB_FILE)
    ensure_intel_tables(conn)
    today = date.today().isoformat()
    compute_daily_stats(conn, stat_date=today)

    # Print summary
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT COUNT(DISTINCT company_name) as companies,
               SUM(active_jobs) as active,
               SUM(new_jobs) as new_jobs,
               SUM(closed_jobs) as closed
        FROM company_daily_stats WHERE stat_date = ?
    """, (today,)).fetchone()

    conn.close()

    print(f"  Date:       {today}")
    print(f"  Companies:  {row['companies'] or 0}")
    print(f"  Active:     {row['active'] or 0}")
    print(f"  New today:  {row['new_jobs'] or 0}")
    print(f"  Closed:     {row['closed'] or 0}")
    print(f"\n  [OK] Stats computed")


def main():
    parser = argparse.ArgumentParser(description="Daily intelligence pipeline")
    parser.add_argument("--skip-discover", action="store_true", help="Skip OSM discovery")
    parser.add_argument("--skip-scrape", action="store_true", help="Skip career page scraping")
    parser.add_argument("--stats-only", action="store_true", help="Only recompute stats")
    parser.add_argument("--region", default="Netherlands", help="Region for discovery (default: Netherlands)")
    args = parser.parse_args()

    py = sys.executable
    start = time.time()
    results = {}

    if args.stats_only:
        compute_stats()
        return

    # Step 1: Discovery
    if not args.skip_discover:
        ok = run_step("Company Discovery (OSM)",
                       [py, "agent_discover.py", "--region", args.region, "--limit", "200"])
        results["discover"] = ok

    # Step 2: Scrape career pages
    if not args.skip_scrape:
        ok = run_step("Career Page Scrape", [py, "agent_scrape.py"])
        results["scrape"] = ok

    # Step 3: ATS sync
    ok = run_step("ATS Job Sync", [py, "sync_ats_jobs.py"])
    results["ats_sync"] = ok

    # Step 4: Stats
    compute_stats()
    results["stats"] = True

    # Final summary
    elapsed = time.time() - start
    print(f"\n{'=' * 60}")
    print(f"  DAILY INTELLIGENCE COMPLETE ({elapsed:.0f}s)")
    print(f"{'=' * 60}")
    for step, ok in results.items():
        status = "OK" if ok else "FAILED"
        print(f"  {step:<20} [{status}]")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
