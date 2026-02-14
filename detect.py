#!/usr/bin/env python3
"""
detect.py — Career page URL finder for Hire Assist

Finds each company's career page URL and stores it in companies.db with
source='careers_page'.  The app then shows a direct "Browse open positions"
link for those companies instead of trying to pull individual job listings.

Works for ALL companies — no ATS detection, no API probing needed.

Usage:
    python detect.py                         # scan built-in NL list
    python detect.py domains.csv            # scan from CSV with name,domain columns
    python detect.py --domain mollie.com --name Mollie    # single company
    python detect.py --add                  # write to companies.db
    python detect.py --dry-run             # print without writing

CSV format:
    name,domain
    Mollie,mollie.com
    WeTransfer,wetransfer.com

Via Docker:
    docker exec jobs_api python detect.py --add
    docker exec jobs_api python detect.py --domain mollie.com --name "Mollie" --add
"""
import argparse
import csv
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

DB_FILE = Path("companies.db")

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "Mozilla/5.0 (compatible; HireAssist/1.0; +local-dev)"

# Career page paths tried in priority order
CAREER_PATHS = [
    "/careers",
    "/jobs",
    "/en/careers",
    "/vacatures",
]

# ---------------------------------------------------------------------------
# Built-in NL company list — primarily companies not covered by ATS APIs
# ---------------------------------------------------------------------------
BUILTIN_COMPANIES = [
    # Companies whose ATS tokens went stale / not found
    ("Mollie",              "mollie.com"),
    ("WeTransfer",          "wetransfer.com"),
    ("Picnic",              "picnic.app"),
    ("Channable",           "channable.com"),
    ("Bynder",              "bynder.com"),
    ("Miro",                "miro.com"),
    ("Vandebron",           "vandebron.nl"),
    ("CM.com",              "cm.com"),
    ("Guerrilla Games",     "guerrilla-games.com"),
    ("Nedap",               "nedap.com"),
    ("Prosus",              "prosus.com"),
    ("Templafy",            "templafy.com"),
    ("Confluent",           "confluent.io"),
    ("Aiven",               "aiven.io"),
    ("Sennder",             "sennder.com"),
    ("Treatwell",           "treatwell.com"),
    ("HashiCorp",           "hashicorp.com"),
    ("Uber NL",             "uber.com"),
    # Larger NL companies using Workday / custom ATS
    ("ASML",                "asml.com"),
    ("Philips",             "philips.com"),
    ("Booking.com",         "booking.com"),
    ("TomTom",              "tomtom.com"),
    ("ING",                 "ing.nl"),
    ("ABN AMRO",            "abnamro.nl"),
    ("Rabobank",            "rabobank.nl"),
    ("Nationale-Nederlanden", "nn-group.com"),
    ("PostNL",              "postnl.nl"),
    ("NS (Dutch Rail)",     "ns.nl"),
    ("Schiphol",            "schiphol.nl"),
    ("VodafoneZiggo",       "vodafoneziggo.nl"),
    ("NXP Semiconductors",  "nxp.com"),
    ("Lightspeed",          "lightspeedhq.com"),
    # Fresh NL candidates
    ("Otrium",              "otrium.com"),
    ("Swapfiets",           "swapfiets.com"),
    ("Rituals",             "rituals.com"),
    ("Fairphone",           "fairphone.com"),
    ("YoungCapital",        "youngcapital.nl"),
    ("SnappCar",            "snappcar.nl"),
    ("Temper",              "temper.nl"),
    ("Insify",              "insify.com"),
    ("Albelli",             "albelli.com"),
    ("Payvision",           "payvision.com"),
    ("Mendix",              "mendix.com"),
    ("AFAS Software",       "afas.nl"),
    ("Randstad NL",         "randstad.nl"),
    ("YoungCapital",        "youngcapital.nl"),
]


# ---------------------------------------------------------------------------
# Find career page URL
# ---------------------------------------------------------------------------
def find_careers_url(domain: str) -> str | None:
    """
    Try career page paths on the domain and return the first working URL.
    Returns the final URL after any redirects.
    """
    # Try paths on main domain only (skip www. to halve requests)
    base = f"https://{domain}"
    for path in CAREER_PATHS:
        url = base + path
        try:
            r = SESSION.get(url, timeout=6, allow_redirects=True)
            if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
                return r.url
        except Exception:
            pass

    # Try careers/jobs subdomain
    for sub in ("careers", "jobs"):
        url = f"https://{sub}.{domain}"
        try:
            r = SESSION.get(url, timeout=6, allow_redirects=True)
            if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
                return r.url
        except Exception:
            pass

    return None


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


def get_existing(conn: sqlite3.Connection) -> set:
    """Return set of company names already covered by a real ATS source."""
    rows = conn.execute(
        "SELECT name FROM companies WHERE active=1 AND source != 'careers_page'"
    ).fetchall()
    return {r[0].lower() for r in rows}


def get_existing_career_pages(conn: sqlite3.Connection) -> set:
    """Return set of (name.lower()) that already have a careers_page entry."""
    rows = conn.execute(
        "SELECT name FROM companies WHERE source='careers_page'"
    ).fetchall()
    return {r[0].lower() for r in rows}


def db_upsert(conn: sqlite3.Connection, name: str, url: str):
    now = datetime.now(timezone.utc).isoformat()
    # Update if exists, insert if not
    existing = conn.execute(
        "SELECT id FROM companies WHERE source='careers_page' AND name=?", (name,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE companies SET token=?, last_verified_at=?, active=1 WHERE source='careers_page' AND name=?",
            (url, now, name),
        )
    else:
        conn.execute(
            """INSERT OR IGNORE INTO companies
               (name, source, token, active, confidence, discovered_at, last_verified_at)
               VALUES (?, 'careers_page', ?, 1, 'detected', ?, ?)""",
            (name, url, now, now),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def run(
    companies: list[tuple[str, str]],
    add_to_db: bool = False,
    dry_run: bool = False,
):
    conn = None
    ats_covered: set = set()
    career_page_existing: set = set()

    if not dry_run:
        conn = sqlite3.connect(DB_FILE)
        ensure_db(conn)
        if add_to_db:
            ats_covered = get_existing(conn)
            career_page_existing = get_existing_career_pages(conn)

    stats = {"found": 0, "added": 0, "skipped_ats": 0, "already": 0, "not_found": 0}

    for company_name, domain in companies:
        name_lower = company_name.lower()

        # Skip if already covered by a real ATS integration
        if name_lower in ats_covered:
            print(f"  ~  {company_name:<28} — already has ATS entry, skipping")
            stats["skipped_ats"] += 1
            continue

        if name_lower in career_page_existing and not add_to_db:
            print(f"  ~  {company_name:<28} — already has careers_page entry")
            stats["already"] += 1
            continue

        print(f"  ?  {company_name:<28} ({domain}) ...", end=" ", flush=True)
        url = find_careers_url(domain)

        if url:
            print(f"✓  {url}")
            stats["found"] += 1
            if add_to_db and not dry_run and conn:
                db_upsert(conn, company_name, url)
                career_page_existing.add(name_lower)
                stats["added"] += 1
        else:
            print("✗  not found")
            stats["not_found"] += 1

        time.sleep(0.3)

    if conn:
        conn.close()

    print(f"\n{'─' * 50}")
    print(f"  Found:            {stats['found']}")
    print(f"  Added to DB:      {stats['added']}")
    print(f"  Skipped (ATS):    {stats['skipped_ats']}")
    print(f"  Already in DB:    {stats['already']}")
    print(f"  Not found:        {stats['not_found']}")
    if dry_run:
        print("\n  (dry-run — nothing written to DB)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Find career page URLs for companies and store in companies.db"
    )
    parser.add_argument(
        "csv_file", nargs="?",
        help="CSV file with 'name' and 'domain' columns"
    )
    parser.add_argument("--domain", help="Single domain (e.g. mollie.com)")
    parser.add_argument("--name",   help="Company name (used with --domain)")
    parser.add_argument("--add",     action="store_true",
                        help="Write found URLs to companies.db")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results without writing to DB")
    args = parser.parse_args()

    if args.domain:
        companies = [(args.name or args.domain, args.domain)]
    elif args.csv_file:
        with open(args.csv_file, newline="", encoding="utf-8") as f:
            companies = [(r["name"], r["domain"]) for r in csv.DictReader(f)]
    else:
        companies = BUILTIN_COMPANIES

    run(companies, add_to_db=args.add, dry_run=args.dry_run)
