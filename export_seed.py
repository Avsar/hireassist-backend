"""
export_seed.py -- Export active companies from local DB to companies_seed.csv.

Usage:
    python export_seed.py                  # writes companies_seed.csv
    python export_seed.py --out other.csv  # custom output path
"""

import argparse
import csv
import sqlite3
from db_config import get_db_path

COLUMNS = ["name", "source", "token", "confidence"]


def export_seed(out_path: str = "companies_seed.csv"):
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT name, source, token, confidence FROM companies WHERE active = 1 ORDER BY source, name"
    ).fetchall()
    conn.close()

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r[c] for c in COLUMNS})

    print(f"Exported {len(rows)} active companies to {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export active companies to seed CSV")
    parser.add_argument("--out", default="companies_seed.csv", help="Output CSV path")
    args = parser.parse_args()
    export_seed(args.out)
