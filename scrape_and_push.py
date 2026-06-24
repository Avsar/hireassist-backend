"""
scrape_and_push.py -- SAFE scraper entrypoint for the Railway scraper service.

Runs discovery + career-page scrape + ATS sync into a LOCAL throwaway DB, then
pushes ONLY the jobs found/re-confirmed THIS run (all is_active=1) to the web
app's additive-only /admin/ingest-jobs endpoint.

WHY THIS EXISTS (post-incident 2026-06-16):
The old path (daily_intelligence.py -> export full DB -> /admin/import-bundle)
exported the whole local DB, including is_active=0 for everything the scrape
didn't re-confirm, and the import applied those deactivations -> wiped the live
site. This script NEVER exports a full dump and NEVER sends deactivations. It
sends only the jobs it positively found, and the /admin/ingest-jobs endpoint
can only add/refresh -- so even a failed or empty scrape is a safe no-op. The
web service alone expires stale jobs (its 14-day rule) and handles ATS closures.

Railway scraper service start command:
    python scrape_and_push.py
Required env: RENDER_URL (web app base URL), ADMIN_TOKEN.
Optional env: GOOGLE_PLACES_API_KEY, KVK_API_KEY, ANTHROPIC_API_KEY (discovery),
    CRON_REGION (default "Netherlands"), CRON_DISCOVER_LIMIT (OSM, default "400"),
    CRON_KVK_LIMIT (KVK registry, default "400", "0" disables),
    DISCOVER_TIMEOUT, SCRAPE_TIMEOUT, ATS_TIMEOUT, INGEST_BATCH (default 500),
    SKIP_DISCOVER=1 to skip discovery.
Do NOT set a Railway volume or DB_PATH=/data here -- the local DB is disposable.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# Importing app runs init_db(): creates the schema, seeds companies_seed.csv,
# and (on Railway, empty DB) kicks off the committed-bundle import in a daemon
# thread so we have companies to scrape. We wait for _bundle_ready below.
import app  # noqa: E402,F401
from app import _bundle_ready  # noqa: E402
import sqlite3  # noqa: E402
from db_config import get_db_path  # noqa: E402

DB_FILE = get_db_path()
RENDER_URL = os.environ.get("RENDER_URL", "").rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
REGION = os.environ.get("CRON_REGION", "Netherlands")
DISC_LIMIT = os.environ.get("CRON_DISCOVER_LIMIT", "400")  # OSM candidates probed per run
KVK_LIMIT = os.environ.get("CRON_KVK_LIMIT", "400")        # KVK candidates probed per run ("0" disables)
BATCH = int(os.environ.get("INGEST_BATCH", "500"))


def run_step(name: str, cmd: list, timeout: int) -> None:
    print(f"\n=== STEP: {name} ===", flush=True)
    try:
        rc = subprocess.run(cmd, timeout=timeout).returncode
        print(f"=== {name}: {'OK' if rc == 0 else 'rc=' + str(rc)} ===", flush=True)
    except subprocess.TimeoutExpired:
        print(f"=== {name}: TIMEOUT after {timeout}s ===", flush=True)
    except Exception as e:
        print(f"=== {name}: ERROR {e} ===", flush=True)


def post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = Request(
        f"{RENDER_URL}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Admin-Token": ADMIN_TOKEN},
    )
    with urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    if not RENDER_URL or not ADMIN_TOKEN:
        print("RENDER_URL / ADMIN_TOKEN not set -- refusing to run (nowhere safe to push).", flush=True)
        return 1

    print("Waiting for local DB init + bundle seed...", flush=True)
    _bundle_ready.wait(300)

    # Capture the moment before scraping. Anything with last_seen_at >= this was
    # positively found/re-confirmed THIS run -> safe to push as active.
    run_start = datetime.now(timezone.utc).isoformat()
    py = sys.executable

    if os.environ.get("SKIP_DISCOVER", "").strip() != "1":
        disc_timeout = int(os.environ.get("DISCOVER_TIMEOUT", "1800"))
        # OSM: businesses mapped on OpenStreetMap (a thin, biased slice).
        run_step("Discovery (OSM)",
                 [py, "agent_discover.py", "--source", "osm", "--region", REGION, "--limit", DISC_LIMIT],
                 disc_timeout)
        # KVK: the official Dutch business registry -- far more small/obscure
        # employers, exactly the ones whose jobs don't reach LinkedIn. No-op if
        # KVK_API_KEY is unset; set CRON_KVK_LIMIT=0 to disable.
        if KVK_LIMIT != "0":
            run_step("Discovery (KVK)",
                     [py, "agent_discover.py", "--source", "kvk", "--region", REGION, "--limit", KVK_LIMIT],
                     disc_timeout)
    run_step("Career Page Scrape", [py, "agent_scrape.py"],
             int(os.environ.get("SCRAPE_TIMEOUT", "3600")))
    run_step("ATS Sync", [py, "sync_ats_jobs.py"],
             int(os.environ.get("ATS_TIMEOUT", "1800")))

    # Collect ONLY what we found this run. We never read or send is_active=0.
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    job_rows = conn.execute(
        """SELECT source, company_name, job_key, title, location_raw, country, city,
                  url, department, job_type, tech_tags, hidden_score, hidden_tier,
                  description, posted_at
           FROM jobs WHERE is_active = 1 AND last_seen_at >= ?""",
        (run_start,),
    ).fetchall()
    comp_rows = conn.execute(
        "SELECT name, source, token, confidence FROM companies WHERE active = 1"
    ).fetchall()
    conn.close()

    jobs = [dict(r) for r in job_rows]
    companies = [dict(r) for r in comp_rows]
    print(f"\nFound {len(jobs)} jobs to push this run; {len(companies)} companies.", flush=True)

    if not jobs:
        print("No jobs found this run -- pushing nothing (safe no-op). "
              "Live data is untouched.", flush=True)
        return 0

    # Push companies once (additive), then jobs in batches.
    try:
        print("Companies push:", post("/admin/ingest-jobs", {"companies": companies, "jobs": []}), flush=True)
    except Exception as e:
        print(f"Company push failed (continuing to jobs): {e}", flush=True)

    totals = {"new": 0, "refreshed": 0, "skipped": 0}
    n_batches = (len(jobs) + BATCH - 1) // BATCH
    for i in range(0, len(jobs), BATCH):
        chunk = jobs[i:i + BATCH]
        try:
            res = post("/admin/ingest-jobs", {"jobs": chunk})
            for k in totals:
                totals[k] += int(res.get(k, 0))
            print(f"  batch {i // BATCH + 1}/{n_batches}: {res}", flush=True)
        except (HTTPError, URLError) as e:
            print(f"  batch {i // BATCH + 1}/{n_batches} FAILED (continuing): {e}", flush=True)
        time.sleep(0.3)

    print(f"\nDONE. Pushed totals: {totals}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
