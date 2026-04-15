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
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
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
PENDING_PUSH_FILE = PROJECT_DIR / "data" / ".pending_push.json"


def check_host_reachable(host: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Resolve DNS for a host. Returns (ok, error_msg)."""
    try:
        socket.setdefaulttimeout(timeout)
        socket.gethostbyname(host)
        return True, ""
    except socket.gaierror as e:
        return False, f"DNS resolution failed: {e}"
    except Exception as e:
        return False, str(e)


def with_retry(fn, attempts: int = 3, base_delay: float = 2.0, label: str = "op"):
    """Call fn() up to `attempts` times with exponential backoff. Returns fn() result
    or raises the last exception."""
    last_exc = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if i < attempts - 1:
                delay = base_delay * (2 ** i)
                print(f"    retry {i + 1}/{attempts - 1} for {label} in {delay:.1f}s ({e})")
                time.sleep(delay)
    raise last_exc


def mark_pending_push(bundle_path: str, reason: str):
    """Persist a marker that a push is still pending so retry_push.py can pick it up."""
    PENDING_PUSH_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_PUSH_FILE.write_text(json.dumps({
        "bundle_path": bundle_path,
        "reason": reason,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    print(f"    Pending push marker written: {PENDING_PUSH_FILE}")


def clear_pending_push():
    if PENDING_PUSH_FILE.exists():
        PENDING_PUSH_FILE.unlink()


def send_health_alert(subject: str, body_text: str):
    """Send a failure notification email to the operator. No-op if email not configured."""
    to_addr = os.environ.get("ALERT_EMAIL", "amitavsar@gmail.com")
    try:
        from job_alerts import _send_email
        # Simple HTML wrapper preserving whitespace.
        html = "<pre style='font-family:monospace;white-space:pre-wrap'>{}</pre>".format(
            body_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        _send_email(to_addr, subject, html)
        print(f"  Health alert sent to {to_addr}")
    except Exception as e:
        print(f"  [WARN] Could not send health alert: {e}")


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

    host = urlparse(render_url).hostname or ""
    ok, err = check_host_reachable(host)
    if not ok:
        print(f"  [FAILED] Pre-check: {err}")
        mark_pending_push(bundle_path, f"render_dns_fail: {err}")
        return False

    start = time.time()
    bundle_data = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
    payload = json.dumps(bundle_data).encode("utf-8")

    def _do_push():
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
        return json.loads(resp.read().decode("utf-8"))

    try:
        result = with_retry(_do_push, attempts=3, base_delay=5.0, label="render_push")
        elapsed = time.time() - start
        print(f"  [OK] Push successful ({elapsed:.1f}s)")
        summary = result.get("summary", {})
        for table, count in summary.items():
            print(f"    {table}: {count}")
        clear_pending_push()
        return True

    except HTTPError as e:
        print(f"  [FAILED] HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}")
        mark_pending_push(bundle_path, f"render_http_{e.code}")
        return False
    except URLError as e:
        print(f"  [FAILED] Connection error: {e.reason}")
        mark_pending_push(bundle_path, f"render_url_error: {e.reason}")
        return False
    except Exception as e:
        print(f"  [FAILED] Push error: {e}")
        mark_pending_push(bundle_path, f"render_exception: {e}")
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

        # Stage + commit always (local commit is safe even if remote is unreachable)
        today = date.today().isoformat()
        subprocess.run(
            ["git", "add", bundle_path],
            cwd=str(PROJECT_DIR), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"Daily bundle update ({today})"],
            cwd=str(PROJECT_DIR), check=True, capture_output=True,
        )

        # DNS check before attempting push
        ok, err = check_host_reachable("github.com")
        if not ok:
            print(f"  [FAILED] Pre-check: {err} -- commit is local, push deferred")
            return False

        def _do_git_push():
            r = subprocess.run(
                ["git", "push"],
                cwd=str(PROJECT_DIR), capture_output=True, text=True, timeout=60,
            )
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip() or "git push failed")
            return r

        try:
            with_retry(_do_git_push, attempts=3, base_delay=5.0, label="git_push")
            print(f"  [OK] Bundle committed and pushed ({today})")
            return True
        except Exception as e:
            print(f"  [FAILED] git push: {e} -- commit is local, run retry_push.py later")
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
    parser.add_argument("--skip-alerts", action="store_true", help="Skip job alert digest emails")
    parser.add_argument("--region", default="Netherlands", help="Region for discovery (default: Netherlands)")
    args = parser.parse_args()

    py = sys.executable
    start = time.time()
    results = {}

    if args.stats_only:
        compute_stats()
        return

    # Step 1: Discovery (OSM, then Google Places fallback if OSM yields nothing)
    if not args.skip_discover:
        # Count companies before OSM run
        with sqlite3.connect(DB_FILE) as _conn:
            before_count = _conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]

        ok = run_step("Company Discovery (OSM)",
                       [py, "agent_discover.py", "--region", args.region, "--limit", "200"])
        results["discover"] = ok

        # Check if OSM added any new companies
        with sqlite3.connect(DB_FILE) as _conn:
            after_count = _conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        osm_added = after_count - before_count

        if osm_added == 0:
            print(f"\n  OSM added 0 companies -- falling back to Google Places")
            ok2 = run_step("Company Discovery (Google Places)",
                           [py, "agent_discover.py", "--source", "google",
                            "--region", args.region, "--limit", "200"])
            results["discover_google"] = ok2

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

    # Step 6.5: Send job alert digest emails
    if not args.skip_alerts:
        print(f"\n{'=' * 60}")
        print(f"  STEP: Send job alert digests")
        print(f"{'=' * 60}\n")
        try:
            from job_alerts import send_daily_digests
            alert_stats = send_daily_digests()
            print(f"  Alerts checked:  {alert_stats['alerts_checked']}")
            print(f"  Emails sent:     {alert_stats['emails_sent']}")
            print(f"  Jobs matched:    {alert_stats['total_jobs_matched']}")
            print(f"\n  [OK] Alert digests complete")
            results["alerts"] = True
        except Exception as e:
            print(f"  [FAILED] Alert digests: {e}")
            results["alerts"] = False
    elif args.skip_alerts:
        print("\n  Skipping alert digests (--skip-alerts)")

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

    # Send failure alert if any step failed
    failed = [step for step, ok in results.items() if not ok]
    if failed:
        log_tail = _tail_pipeline_log(80)
        body = (
            f"Daily pipeline completed with failures ({elapsed:.0f}s)\n"
            f"Failed steps: {', '.join(failed)}\n\n"
            f"Step results:\n"
            + "\n".join(f"  {s:<20} [{'OK' if ok else 'FAILED'}]" for s, ok in results.items())
            + "\n\n--- Log tail ---\n"
            + log_tail
        )
        send_health_alert(
            f"[Hire Assist] Pipeline failed: {', '.join(failed)}",
            body,
        )


def _tail_pipeline_log(n_lines: int = 80) -> str:
    """Read the last N lines of the pipeline log, best-effort."""
    log_path = PROJECT_DIR / "data" / "logs" / "pipeline.log"
    try:
        if not log_path.exists():
            return "(no pipeline.log found)"
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            # Read last ~32KB, which is plenty for 80 lines
            read_size = min(size, 32 * 1024)
            f.seek(size - read_size)
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        return "\n".join(lines[-n_lines:])
    except Exception as e:
        return f"(could not read log: {e})"


if __name__ == "__main__":
    main()
