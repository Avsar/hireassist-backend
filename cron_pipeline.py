"""
cron_pipeline.py -- Daily refresh that runs INSIDE the deployed app.

Runs on Railway via APScheduler (see app.py `_start_keep_alive`), independent
of the local machine. Writes directly to the live DB (DB_PATH) -- with a Railway
Volume that DB is persistent, so no bundle export/push/git is needed.

Steps (lightweight, always run):
  1. ATS sync  -- pull fresh job lists from Greenhouse/Lever/Recruitee/etc.
  2. Stats     -- recompute company_daily_stats
  3. Alerts    -- send daily digest emails

Heavy steps (only when full=True, i.e. env CRON_FULL=1):
  0a. Discovery       -- find new companies via OSM/Google (agent_discover.py)
  0b. Career scraping -- Playwright scrape of career pages (agent_scrape.py)
These shell out to subprocesses so their memory is isolated from the web process
and freed when they exit. They need Chromium in the image (see Dockerfile) and,
for discovery, GOOGLE_PLACES_API_KEY / KVK_API_KEY / ANTHROPIC_API_KEY env vars.

Usage:
    python cron_pipeline.py                 # run once (ATS + stats + alerts)
    python cron_pipeline.py --full          # also discovery + career scraping
    python cron_pipeline.py --skip-alerts   # skip emails (useful for local test)

Invoked from app.py as cron_pipeline.run(full=...) via APScheduler.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from db_config import get_db_path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DB_FILE = get_db_path()
PROJECT_DIR = Path(__file__).parent
LAST_RUN_FILE = PROJECT_DIR / "data" / ".cron_last_run.json"


def _write_last_run(result: dict):
    """Persist last-run state so /admin/cron-status can show it."""
    try:
        LAST_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_RUN_FILE.write_text(json.dumps(result, default=str, indent=2), encoding="utf-8")
    except Exception:
        pass


def read_last_run() -> dict | None:
    """Load last-run state (used by /admin/cron-status)."""
    try:
        if LAST_RUN_FILE.exists():
            return json.loads(LAST_RUN_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _run_subprocess_step(results: dict, name: str, cmd: list, timeout: int) -> None:
    """Run a heavy step as an isolated subprocess; record ok/error in results."""
    try:
        print(f"[cron] {name} starting: {' '.join(cmd)}")
        proc = subprocess.run(cmd, cwd=str(PROJECT_DIR), timeout=timeout)
        ok = proc.returncode == 0
        results["steps"][name] = {"ok": ok, "returncode": proc.returncode}
        print(f"[cron] {name} {'OK' if ok else 'FAILED'} (rc={proc.returncode})")
    except subprocess.TimeoutExpired:
        results["steps"][name] = {"ok": False, "error": f"timeout after {timeout}s"}
        print(f"[cron] {name} TIMED OUT after {timeout}s")
    except Exception as e:
        results["steps"][name] = {"ok": False, "error": str(e)}
        print(f"[cron] {name} FAILED: {e}")


def run(skip_alerts: bool = False, full: bool = False) -> dict:
    """Run the cron pipeline once. Returns a results dict.

    Safe to call from APScheduler or CLI. Catches per-step exceptions so a
    failure in one step doesn't prevent the others from running.

    When full=True, also runs company discovery + Playwright career-page
    scraping (as isolated subprocesses) before the ATS sync.
    """
    from sync_ats_jobs import sync_all
    from job_intel import compute_daily_stats, ensure_intel_tables

    started_at = datetime.now(timezone.utc)
    results: dict = {
        "started_at": started_at.isoformat(),
        "full": full,
        "steps": {},
        "db_path": str(DB_FILE),
    }
    start_t = time.time()

    # Heavy steps (full mode only): discovery + career-page scraping.
    if full:
        region = os.environ.get("CRON_REGION", "Netherlands")
        disc_limit = os.environ.get("CRON_DISCOVER_LIMIT", "200")
        _run_subprocess_step(
            results, "discovery",
            [sys.executable, "agent_discover.py", "--region", region, "--limit", disc_limit],
            timeout=int(os.environ.get("CRON_DISCOVER_TIMEOUT", "1800")),
        )
        _run_subprocess_step(
            results, "scrape",
            [sys.executable, "agent_scrape.py"],
            timeout=int(os.environ.get("CRON_SCRAPE_TIMEOUT", "3600")),
        )

    # Step 1: ATS sync
    try:
        print("[cron] ATS sync starting")
        sync_all()  # uses default DB path via db_config
        results["steps"]["ats_sync"] = {"ok": True}
        print("[cron] ATS sync OK")
    except Exception as e:
        results["steps"]["ats_sync"] = {"ok": False, "error": str(e)}
        print(f"[cron] ATS sync FAILED: {e}")

    # Step 2: Stats
    try:
        print("[cron] Stats starting")
        conn = sqlite3.connect(DB_FILE)
        ensure_intel_tables(conn)
        today = date.today().isoformat()
        compute_daily_stats(conn, stat_date=today)
        conn.close()
        results["steps"]["stats"] = {"ok": True, "date": today}
        print(f"[cron] Stats OK ({today})")
    except Exception as e:
        results["steps"]["stats"] = {"ok": False, "error": str(e)}
        print(f"[cron] Stats FAILED: {e}")

    # Step 3: Alerts
    if skip_alerts:
        results["steps"]["alerts"] = {"ok": True, "skipped": True}
    else:
        try:
            print("[cron] Alerts starting")
            from job_alerts import send_daily_digests
            alert_stats = send_daily_digests()
            results["steps"]["alerts"] = {"ok": True, **alert_stats}
            print(f"[cron] Alerts OK: {alert_stats}")
        except Exception as e:
            results["steps"]["alerts"] = {"ok": False, "error": str(e)}
            print(f"[cron] Alerts FAILED: {e}")

    # Summary
    elapsed = time.time() - start_t
    failed = [s for s, r in results["steps"].items() if not r.get("ok")]
    results["elapsed_sec"] = round(elapsed, 1)
    results["finished_at"] = datetime.now(timezone.utc).isoformat()
    results["ok"] = len(failed) == 0
    results["failed_steps"] = failed

    # Post-run counts for visibility
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        results["companies"] = c.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        results["active_jobs"] = c.execute("SELECT COUNT(*) FROM jobs WHERE is_active=1").fetchone()[0]
        results["total_jobs"] = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.close()
    except Exception:
        pass

    _write_last_run(results)
    print(f"[cron] Done in {elapsed:.1f}s -- failed: {failed or 'none'}")
    return results


def main():
    parser = argparse.ArgumentParser(description="Cron refresh pipeline (ATS + stats + alerts)")
    parser.add_argument("--skip-alerts", action="store_true", help="Skip alert digest emails")
    parser.add_argument("--full", action="store_true",
                        help="Also run discovery + career-page scraping")
    args = parser.parse_args()
    result = run(skip_alerts=args.skip_alerts, full=args.full)
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
