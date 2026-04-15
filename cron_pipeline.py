"""
cron_pipeline.py -- Lightweight daily refresh that runs INSIDE the deployed app.

Runs on Railway via APScheduler (see app.py `_start_keep_alive`), independent
of the local machine. Scope: only steps that are safe to run on ephemeral
infra and don't need paid APIs.

Steps:
  1. ATS sync  -- pull fresh job lists from Greenhouse/Lever/Recruitee/etc.
  2. Stats     -- recompute company_daily_stats
  3. Alerts    -- send daily digest emails

Explicitly NOT in scope (stays on local pipeline):
  - Discovery (OSM/Google Places/KVK) -- needs paid APIs + laptop is fine
  - Career-page scraping (Playwright) -- slow, heavy, flaky on cloud
  - Bundle export + push + git push    -- Railway IS the target, no need to push

Usage:
    python cron_pipeline.py                 # run once, stdout logs
    python cron_pipeline.py --skip-alerts   # skip emails (useful for local test)

Invoked from app.py as cron_pipeline.run() via APScheduler.
"""

import argparse
import json
import os
import sqlite3
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


def run(skip_alerts: bool = False) -> dict:
    """Run the cron pipeline once. Returns a results dict.

    Safe to call from APScheduler or CLI. Catches per-step exceptions so a
    failure in one step doesn't prevent the others from running.
    """
    from sync_ats_jobs import sync_all
    from job_intel import compute_daily_stats, ensure_intel_tables

    started_at = datetime.now(timezone.utc)
    results: dict = {
        "started_at": started_at.isoformat(),
        "steps": {},
        "db_path": str(DB_FILE),
    }
    start_t = time.time()

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
    args = parser.parse_args()
    result = run(skip_alerts=args.skip_alerts)
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
