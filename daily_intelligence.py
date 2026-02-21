"""
daily_intelligence.py -- Daily intelligence pipeline orchestrator.

Runs the full pipeline in sequence:
  1. Discovery (agent_discover.py) -- find new companies via OSM
  2. Scrape (agent_scrape.py) -- scrape career pages
  3. ATS Sync (sync_ats_jobs.py) -- sync ATS job listings into jobs table
  4. Stats (job_intel.py) -- compute daily stats + momentum
  5. Export (export_bundle.py) -- export DB to JSON bundle
  6. Push to Render -- POST bundle to live site
  7. Git push -- commit + push bundle.json so Render cold starts get latest data

Usage:
    python daily_intelligence.py                          # full pipeline
    python daily_intelligence.py --skip-discover          # skip OSM discovery
    python daily_intelligence.py --skip-scrape            # skip career scraping
    python daily_intelligence.py --stats-only             # only recompute stats
    python daily_intelligence.py --skip-push              # skip Render push
    python daily_intelligence.py --skip-git               # skip git commit+push
    python daily_intelligence.py --region "Amsterdam"     # pass region to discovery
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from db_config import get_db_path

# Load .env if python-dotenv available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DB_FILE = get_db_path()
PROJECT_DIR = Path(__file__).parent


def run_step(label: str, cmd: list) -> bool:
    """Run a subprocess step and return True on success."""
    print(f"\n{'=' * 60}")
    print(f"  STEP: {label}")
    print(f"  CMD:  {' '.join(cmd)}")
    print(f"{'=' * 60}\n")

    start = time.time()
    result = subprocess.run(cmd, cwd=str(PROJECT_DIR))
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


def export_bundle() -> str | None:
    """Export DB to bundle JSON. Returns file path on success, None on failure."""
    print(f"\n{'=' * 60}")
    print(f"  STEP: Export bundle")
    print(f"{'=' * 60}\n")

    try:
        from export_bundle import export_bundle as _do_export
        out_path = str(PROJECT_DIR / "data" / "seed" / "bundle.json")
        _do_export(out_path)
        size_mb = Path(out_path).stat().st_size / (1024 * 1024)
        print(f"  Bundle: {out_path} ({size_mb:.1f} MB)")
        print(f"\n  [OK] Export complete")
        return out_path
    except Exception as e:
        print(f"  [FAILED] Export: {e}")
        return None


def push_to_render(bundle_path: str) -> bool:
    """POST bundle JSON to Render's /admin/import-bundle endpoint."""
    render_url = os.environ.get("RENDER_URL", "").rstrip("/")
    admin_token = os.environ.get("ADMIN_TOKEN", "")

    if not render_url:
        print("\n  RENDER_URL not set in .env -- skipping push")
        return True  # Not a failure, just skipped
    if not admin_token:
        print("\n  ADMIN_TOKEN not set in .env -- skipping push")
        return True

    print(f"\n{'=' * 60}")
    print(f"  STEP: Push to Render")
    print(f"  URL:  {render_url}/admin/import-bundle")
    print(f"{'=' * 60}\n")

    start = time.time()
    try:
        bundle_data = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
        payload = json.dumps(bundle_data).encode("utf-8")

        req = Request(
            f"{render_url}/admin/import-bundle",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Admin-Token": admin_token,
            },
            method="POST",
        )
        resp = urlopen(req, timeout=120)
        elapsed = time.time() - start
        result = json.loads(resp.read().decode("utf-8"))

        print(f"  [OK] Push successful ({elapsed:.1f}s)")
        summary = result.get("summary", {})
        for table, count in summary.items():
            print(f"    {table}: {count}")
        return True

    except HTTPError as e:
        print(f"  [FAILED] HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}")
        return False
    except URLError as e:
        print(f"  [FAILED] Connection error: {e.reason}")
        return False
    except Exception as e:
        print(f"  [FAILED] Push error: {e}")
        return False


def git_push_bundle(bundle_path: str) -> bool:
    """Commit and push the updated bundle.json so Render cold starts get latest data."""
    print(f"\n{'=' * 60}")
    print(f"  STEP: Git push bundle")
    print(f"{'=' * 60}\n")

    try:
        # Check if there are changes to commit
        result = subprocess.run(
            ["git", "diff", "--quiet", "--", bundle_path],
            cwd=str(PROJECT_DIR), capture_output=True,
        )
        if result.returncode == 0:
            print("  Bundle unchanged -- nothing to push")
            return True

        # Stage, commit, push
        today = date.today().isoformat()
        subprocess.run(
            ["git", "add", bundle_path],
            cwd=str(PROJECT_DIR), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"Daily bundle update ({today})"],
            cwd=str(PROJECT_DIR), check=True, capture_output=True,
        )
        result = subprocess.run(
            ["git", "push"],
            cwd=str(PROJECT_DIR), capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            print(f"  [OK] Bundle committed and pushed ({today})")
            return True
        else:
            print(f"  [FAILED] git push: {result.stderr.strip()}")
            return False

    except subprocess.TimeoutExpired:
        print("  [FAILED] git push timed out (60s)")
        return False
    except subprocess.CalledProcessError as e:
        print(f"  [FAILED] git error: {e}")
        return False
    except Exception as e:
        print(f"  [FAILED] {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Daily intelligence pipeline")
    parser.add_argument("--skip-discover", action="store_true", help="Skip OSM discovery")
    parser.add_argument("--skip-scrape", action="store_true", help="Skip career page scraping")
    parser.add_argument("--stats-only", action="store_true", help="Only recompute stats")
    parser.add_argument("--skip-push", action="store_true", help="Skip Render push")
    parser.add_argument("--skip-git", action="store_true", help="Skip git commit+push of bundle")
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

    # Step 5: Export bundle
    bundle_path = export_bundle()
    results["export"] = bundle_path is not None

    # Step 6: Push to Render (immediate update while service is warm)
    if not args.skip_push and bundle_path:
        ok = push_to_render(bundle_path)
        results["push"] = ok
    elif args.skip_push:
        print("\n  Skipping Render push (--skip-push)")

    # Step 7: Git push bundle (ensures Render cold starts get latest data)
    if not args.skip_git and bundle_path:
        ok = git_push_bundle(bundle_path)
        results["git_push"] = ok
    elif args.skip_git:
        print("\n  Skipping git push (--skip-git)")

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
