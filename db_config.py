"""Centralised database path helper for Hire Assist."""

import os
from pathlib import Path


def get_db_path() -> Path:
    """Return the SQLite database path from DB_PATH env var (default: companies.db)."""
    return Path(os.environ.get("DB_PATH", "companies.db"))
