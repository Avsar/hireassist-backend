#!/usr/bin/env python3
"""
Discover NL tech companies on Greenhouse / Lever / SmartRecruiters
and persist validated entries to companies.db.

Usage:
    python discover.py             # probe all candidates, add new valid ones
    python discover.py --dry-run   # show what would be added without writing
    python discover.py --reverify  # re-probe companies already in DB to update last_verified_at

Via Docker:
    docker exec jobs_api python discover.py
"""
import argparse
import sqlite3
import time
import requests
from pathlib import Path
from datetime import datetime, timezone

DB_FILE = Path("companies.db")

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "HireAssist/0.1 (discovery)"

# ---------------------------------------------------------------------------
# Candidate list: (display_name, [(source, token), ...])
#
# Ordering within each company matters — first hit wins.
# Sources tried: greenhouse > lever > smartrecruiters (most → least common
# for NL tech companies).
# ---------------------------------------------------------------------------
CANDIDATES = [
    # NL-headquartered tech companies
    ("Adyen",              [("greenhouse", "adyen")]),
    ("Booking.com",        [("greenhouse", "booking"), ("greenhouse", "bookingcom"), ("smartrecruiters", "Booking")]),
    ("TomTom",             [("greenhouse", "tomtom"), ("smartrecruiters", "TomTomNV")]),
    ("Picnic",             [("lever", "picnic"), ("greenhouse", "picnic")]),
    ("Mollie",             [("greenhouse", "mollie"), ("lever", "mollie")]),
    ("WeTransfer",         [("greenhouse", "wetransfer"), ("lever", "wetransfer")]),
    ("Catawiki",           [("greenhouse", "catawiki"), ("lever", "catawiki")]),
    ("MessageBird",        [("greenhouse", "bird"), ("greenhouse", "messagebird"), ("lever", "messagebird")]),
    ("Backbase",           [("greenhouse", "backbase"), ("lever", "backbase")]),
    ("Miro",               [("greenhouse", "miro"), ("lever", "miro")]),
    ("Sendcloud",          [("greenhouse", "sendcloud"), ("lever", "sendcloud")]),
    ("Channable",          [("greenhouse", "channable"), ("lever", "channable")]),
    ("Bynder",             [("greenhouse", "bynder"), ("lever", "bynder")]),
    ("Mendix",             [("greenhouse", "mendix"), ("lever", "mendix")]),
    ("Otrium",             [("greenhouse", "otrium"), ("lever", "otrium")]),
    ("Lightyear",          [("greenhouse", "lightyear"), ("lever", "lightyear")]),
    ("Vandebron",          [("greenhouse", "vandebron"), ("lever", "vandebron")]),
    ("CM.com",             [("greenhouse", "cmcom"), ("lever", "cm")]),
    ("bol.com",            [("smartrecruiters", "Bol"), ("greenhouse", "bolcom")]),
    ("Coolblue",           [("smartrecruiters", "Coolblue"), ("greenhouse", "coolblue")]),
    ("Guerrilla Games",    [("greenhouse", "guerrilla"), ("lever", "guerrillagames")]),
    ("Nedap",              [("greenhouse", "nedap"), ("lever", "nedap")]),
    ("ASML",               [("greenhouse", "asml"), ("smartrecruiters", "ASML")]),
    ("Philips",            [("smartrecruiters", "Philips"), ("greenhouse", "philips")]),
    ("ING",                [("smartrecruiters", "ING-Bank"), ("greenhouse", "ing")]),
    ("ABN AMRO",           [("smartrecruiters", "ABNAMROBank"), ("greenhouse", "abnamro")]),
    ("Randstad",           [("smartrecruiters", "Randstad"), ("greenhouse", "randstad")]),
    ("Wolters Kluwer",     [("smartrecruiters", "WoltersKluwer"), ("greenhouse", "wolterskluwer")]),
    ("JustEat Takeaway",   [("smartrecruiters", "JustEatTakeaway"), ("greenhouse", "takeaway")]),
    ("Prosus",             [("greenhouse", "prosus"), ("lever", "prosus")]),
    ("Templafy",           [("greenhouse", "templafy"), ("lever", "templafy")]),

    # Global tech with large NL presence / NL jobs
    ("Elastic",            [("greenhouse", "elastic")]),
    ("GitLab",             [("greenhouse", "gitlab")]),
    ("Datadog",            [("greenhouse", "datadog")]),
    ("Cloudflare",         [("greenhouse", "cloudflare")]),
    ("Stripe",             [("greenhouse", "stripe")]),
    ("Netflix",            [("lever", "netflix")]),
    ("Uber",               [("greenhouse", "uber")]),
    ("Databricks",         [("greenhouse", "databricks")]),
    ("Confluent",          [("greenhouse", "confluent")]),
    ("HashiCorp",          [("greenhouse", "hashicorp")]),
    ("Aiven",              [("greenhouse", "aiven"), ("lever", "aiven")]),
    ("Mimecast",           [("greenhouse", "mimecast"), ("lever", "mimecast")]),
    ("PagerDuty",          [("greenhouse", "pagerduty")]),
    ("Twilio",             [("greenhouse", "twilio")]),
    ("Okta",               [("greenhouse", "okta")]),
    ("Wrike",              [("greenhouse", "wrike"), ("lever", "wrike")]),
    ("Contentful",         [("greenhouse", "contentful"), ("lever", "contentful")]),
    ("Personio",           [("greenhouse", "personio"), ("lever", "personio")]),
    ("Sennder",            [("greenhouse", "sennder"), ("lever", "sennder")]),
    ("Treatwell",          [("greenhouse", "treatwell"), ("lever", "treatwell")]),

    # --- NL companies on Recruitee ---
    ("Bunq",               [("recruitee", "bunq")]),
    ("Rituals",            [("recruitee", "rituals")]),
    ("Tiqets",             [("recruitee", "tiqets")]),
    ("SnappCar",           [("recruitee", "snappcar")]),
    ("Springbok Agency",   [("recruitee", "springbok")]),
    ("AFAS Software",      [("recruitee", "afas")]),
    ("Cimpress",           [("recruitee", "cimpress"), ("greenhouse", "cimpress")]),
    ("Helloprint",         [("recruitee", "helloprint")]),
    ("Vinted",             [("recruitee", "vinted"), ("greenhouse", "vinted")]),
    ("YoungCapital",       [("recruitee", "youngcapital")]),
    ("Insify",             [("recruitee", "insify")]),
    ("Fairphone",          [("recruitee", "fairphone")]),
    ("Sendbird",           [("recruitee", "sendbird"), ("greenhouse", "sendbird")]),
    ("Temper",             [("recruitee", "temper")]),
    ("Yoast",              [("recruitee", "yoast"), ("greenhouse", "yoast")]),
    ("Albelli",            [("recruitee", "albelli")]),
    ("Travix",             [("recruitee", "travix"), ("greenhouse", "travix")]),
    ("Payvision",          [("recruitee", "payvision")]),
    ("Spryker",            [("recruitee", "spryker"), ("greenhouse", "spryker")]),
    ("Packlink",           [("recruitee", "packlink"), ("greenhouse", "packlink")]),
    ("ChannelEngine",      [("recruitee", "channelengine")]),
    ("Highstreet.io",      [("recruitee", "highstreet")]),
    ("Tele2 NL",           [("recruitee", "tele2nl"), ("recruitee", "tele2")]),
    ("Vattenfall NL",      [("recruitee", "vattenfall")]),
    ("NS (Dutch Rail)",    [("recruitee", "ns"), ("recruitee", "nederlandse-spoorwegen")]),
    ("a.s.r.",             [("recruitee", "asr")]),
    ("Nationale-Nederlanden", [("recruitee", "nationale-nederlanden"), ("recruitee", "nn")]),
    ("PostNL",             [("recruitee", "postnl"), ("greenhouse", "postnl")]),
    ("Action",             [("recruitee", "action"), ("greenhouse", "action")]),
    ("Schiphol Group",     [("recruitee", "schiphol")]),

    # --- More Greenhouse (NL-relevant) ---
    ("Lightspeed",         [("greenhouse", "lightspeedpos"), ("greenhouse", "lightspeed")]),
    ("Messagebird / Bird", [("greenhouse", "bird")]),
    ("Coda",               [("greenhouse", "coda")]),
    ("Crisp",              [("greenhouse", "crisp")]),
    ("Docker",             [("greenhouse", "docker")]),
    ("dbt Labs",           [("greenhouse", "dbtlabs")]),
    ("Miro (EU)",          [("greenhouse", "miro")]),
    ("Exact Software",     [("smartrecruiters", "Exact"), ("greenhouse", "exact")]),
    ("Signify",            [("smartrecruiters", "Signify")]),
    ("NN Group",           [("smartrecruiters", "NNGroup")]),
    ("VodafoneZiggo",      [("smartrecruiters", "VodafoneZiggo")]),
    ("TomTom (alt)",       [("smartrecruiters", "TomTom")]),
    ("NXP Semiconductors", [("smartrecruiters", "NXP")]),
    ("Nuon / Vattenfall",  [("greenhouse", "vattenfall")]),
    ("OMP",                [("greenhouse", "omp")]),
    ("Siemens NL",         [("greenhouse", "siemens")]),
    ("Pricewise",          [("greenhouse", "pricewise")]),
    ("Speakap",            [("greenhouse", "speakap")]),
    ("TriFact365",         [("greenhouse", "trifact365")]),
    ("WeAre8",             [("greenhouse", "weare8")]),
    ("Swapfiets",          [("greenhouse", "swapfiets"), ("lever", "swapfiets")]),
    ("Otrium (alt)",       [("lever", "otrium")]),
    ("Usabilla",           [("greenhouse", "usabilla"), ("lever", "usabilla")]),
]


# ---------------------------------------------------------------------------
# Probe functions: return job count (int >= 0) if board exists, None if not
# ---------------------------------------------------------------------------
def probe_greenhouse(token: str) -> int | None:
    try:
        r = SESSION.get(
            f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
            timeout=15,
        )
        if r.status_code == 200:
            return len(r.json().get("jobs", []))
    except Exception:
        pass
    return None


def probe_lever(token: str) -> int | None:
    try:
        r = SESSION.get(
            f"https://api.lever.co/v0/postings/{token}?mode=json",
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return len(data)
            if isinstance(data, dict):
                return len(data.get("data", []))
    except Exception:
        pass
    return None


def probe_smartrecruiters(token: str) -> int | None:
    try:
        r = SESSION.get(
            f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
            timeout=15,
        )
        if r.status_code == 200:
            return len(r.json().get("content", []))
    except Exception:
        pass
    return None


def probe_recruitee(token: str) -> int | None:
    try:
        r = SESSION.get(
            f"https://{token}.recruitee.com/api/offers/",
            timeout=15,
        )
        if r.status_code == 200:
            return len(r.json().get("offers", []))
    except Exception:
        pass
    return None


PROBERS = {
    "greenhouse": probe_greenhouse,
    "lever": probe_lever,
    "smartrecruiters": probe_smartrecruiters,
    "recruitee": probe_recruitee,
}


def probe(source: str, token: str) -> int | None:
    return PROBERS[source](token)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def ensure_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            source TEXT NOT NULL,
            token TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            confidence TEXT NOT NULL DEFAULT 'manual',
            discovered_at TEXT,
            last_verified_at TEXT,
            UNIQUE(source, token)
        )
    """)
    conn.commit()


def get_existing(conn: sqlite3.Connection) -> dict:
    """Returns {(source, token): row_id} for all existing entries."""
    rows = conn.execute("SELECT id, source, token FROM companies").fetchall()
    return {(r[1], r[2]): r[0] for r in rows}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def discover(dry_run: bool = False, reverify: bool = False):
    conn = sqlite3.connect(DB_FILE)
    ensure_db(conn)
    existing = get_existing(conn)
    now = datetime.now(timezone.utc).isoformat()

    stats = {"added": 0, "skipped": 0, "updated": 0, "not_found": 0}

    print(f"{'[DRY RUN] ' if dry_run else ''}Probing {len(CANDIDATES)} companies...\n")

    for company_name, candidates in CANDIDATES:
        found = False
        for source, token in candidates:
            key = (source, token)

            if key in existing and not reverify:
                print(f"  SKIP   {company_name:<25} ({source}/{token}) — already in DB")
                stats["skipped"] += 1
                found = True
                break

            print(f"  PROBE  {company_name:<25} ({source}/{token}) ... ", end="", flush=True)
            count = probe(source, token)
            time.sleep(0.3)  # polite rate-limiting

            if count is not None:
                print(f"✓  {count} jobs")
                if not dry_run:
                    if key in existing:
                        conn.execute(
                            "UPDATE companies SET last_verified_at = ? WHERE source = ? AND token = ?",
                            (now, source, token),
                        )
                        stats["updated"] += 1
                    else:
                        conn.execute(
                            """INSERT OR IGNORE INTO companies
                               (name, source, token, active, confidence, discovered_at, last_verified_at)
                               VALUES (?, ?, ?, 1, 'auto', ?, ?)""",
                            (company_name, source, token, now, now),
                        )
                        existing[key] = True
                        stats["added"] += 1
                    conn.commit()
                else:
                    if key not in existing:
                        stats["added"] += 1
                found = True
                break  # first working ATS per company is enough
            else:
                print("✗  not found")

        if not found:
            stats["not_found"] += 1

    conn.close()

    print(f"\n{'─' * 50}")
    print(f"  Added:     {stats['added']}")
    print(f"  Updated:   {stats['updated']}")
    print(f"  Skipped:   {stats['skipped']}")
    print(f"  Not found: {stats['not_found']}")
    if dry_run:
        print("\n  (dry-run — nothing was written to DB)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Discover NL tech companies for Hire Assist")
    parser.add_argument("--dry-run", action="store_true", help="Show results without writing to DB")
    parser.add_argument("--reverify", action="store_true", help="Re-probe existing entries to refresh last_verified_at")
    args = parser.parse_args()
    discover(dry_run=args.dry_run, reverify=args.reverify)
