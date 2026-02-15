"""
export_bundle.py -- Export local DB tables to a JSON bundle for Render import.

Exports: companies, scraped_jobs, jobs (if exists), company_daily_stats (if exists).

Usage:
    python export_bundle.py                           # -> data/exports/bundle.json
    python export_bundle.py --out my_bundle.json      # custom path
"""

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from db_config import get_db_path

TABLES = ["companies", "scraped_jobs", "jobs", "company_daily_stats"]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _export_table(conn: sqlite3.Connection, name: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM {name}").fetchall()  # noqa: S608
    return [dict(r) for r in rows]


def export_bundle(out_path: str = "data/exports/bundle.json"):
    conn = sqlite3.connect(get_db_path())
    bundle: dict = {
        "meta": {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "tables": {},
        },
        "data": {},
    }

    for table in TABLES:
        if not _table_exists(conn, table):
            bundle["meta"]["tables"][table] = 0
            continue
        rows = _export_table(conn, table)
        bundle["data"][table] = rows
        bundle["meta"]["tables"][table] = len(rows)

    conn.close()

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"Exported to {out}")
    for table, count in bundle["meta"]["tables"].items():
        print(f"  {table}: {count} rows")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export DB to JSON bundle")
    parser.add_argument("--out", default="data/exports/bundle.json")
    args = parser.parse_args()
    export_bundle(args.out)
