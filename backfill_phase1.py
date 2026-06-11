"""
backfill_phase1.py -- One-shot Phase 1 data-quality backfill.

1. Re-parses location_raw for all active jobs to fill missing city/country
   (using the improved foreign-country detection in app.split_city_country).
2. Recomputes hidden_score / hidden_tier for all active jobs.
3. Prints before/after stats.

Safe to run repeatedly. Only fills empty city/country fields -- never
overwrites a non-empty value.

Usage:
    python backfill_phase1.py            # apply
    python backfill_phase1.py --dry-run  # report only
"""

import argparse
import sqlite3

from db_config import get_db_path
import job_intel
from app import split_city_country  # also pulls in foreign-country markers


def stats(conn):
    total = conn.execute("SELECT COUNT(*) FROM jobs WHERE is_active=1").fetchone()[0]
    no_city = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE is_active=1 AND (city IS NULL OR city='')"
    ).fetchone()[0]
    no_country = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE is_active=1 AND (country IS NULL OR country='')"
    ).fetchone()[0]
    nl = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE is_active=1 AND country='Netherlands'"
    ).fetchone()[0]
    foreign = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE is_active=1 AND country NOT IN ('', 'Netherlands') AND country IS NOT NULL"
    ).fetchone()[0]
    return {"active": total, "no_city": no_city, "no_country": no_country,
            "nl": nl, "foreign": foreign}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(get_db_path())
    job_intel.ensure_intel_tables(conn)

    print("=== Before ===")
    before = stats(conn)
    for k, v in before.items():
        print(f"  {k}: {v:,}")

    # --- 1. city/country backfill from location_raw ---
    rows = conn.execute(
        """SELECT id, location_raw, city, country FROM jobs
           WHERE is_active=1
             AND location_raw != ''
             AND ((city IS NULL OR city='') OR (country IS NULL OR country=''))"""
    ).fetchall()

    updates = []
    filled_city = filled_country = 0
    for job_id, loc_raw, city, country in rows:
        loc = loc_raw.split("|")[0].strip() if "|" in loc_raw else loc_raw
        p_city, p_country = split_city_country(loc)
        new_city = city if city else (p_city or "")
        new_country = country if country else (p_country or "")
        if new_city != (city or "") or new_country != (country or ""):
            if not city and new_city:
                filled_city += 1
            if not country and new_country:
                filled_country += 1
            updates.append((new_city or None, new_country or None, job_id))

    print(f"\nCity/country backfill: {len(rows):,} candidates, "
          f"{filled_city:,} cities filled, {filled_country:,} countries filled")

    if not args.dry_run and updates:
        conn.executemany("UPDATE jobs SET city=?, country=? WHERE id=?", updates)
        conn.commit()

    # --- 2. hidden tier recompute ---
    if args.dry_run:
        print("\n(dry-run: skipping hidden tier recompute)")
    else:
        summary = job_intel.recompute_hidden_tiers(conn)
        print(f"\nHidden tiers: gem={summary[2]:,}  low-visibility={summary[1]:,}  "
              f"unlabeled={summary[0]:,}")

    print("\n=== After ===")
    after = stats(conn)
    for k, v in after.items():
        delta = v - before[k]
        print(f"  {k}: {v:,} ({delta:+,})")

    conn.close()


if __name__ == "__main__":
    main()
