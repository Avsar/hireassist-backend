"""
ci_bootstrap_db.py -- Rebuild companies.db from the committed seed bundle.

Used by the GitHub Actions daily-refresh workflow: a fresh runner has no
companies.db, so we reconstruct it from data/seed/bundle.json (the last
durable snapshot) before running the pipeline. This preserves first_seen_at
history so incremental discovery / job-close tracking stays correct.

Reuses app._import_bundle_data so the import logic never drifts from the
production cold-start path. RENDER_URL / RAILWAY_PUBLIC_DOMAIN are popped
before importing app so the keep-alive scheduler does NOT start here.

Usage:
    python ci_bootstrap_db.py                       # data/seed/bundle.json
    python ci_bootstrap_db.py path/to/bundle.json
"""

import json
import os
import sys
from pathlib import Path

# Ensure importing app does not start the background keep-alive scheduler.
os.environ.pop("RENDER_URL", None)
os.environ.pop("RAILWAY_PUBLIC_DOMAIN", None)
os.environ.pop("RAILWAY_ENVIRONMENT", None)

import app  # noqa: E402  (runs init_db(); keep-alive no-ops without a public URL)


def main() -> int:
    bundle_path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/seed/bundle.json")
    if not bundle_path.exists():
        print(f"[ci-bootstrap] No bundle at {bundle_path} -- starting from empty/CSV seed")
        return 0

    data = json.loads(bundle_path.read_text(encoding="utf-8")).get("data", {})
    result = app._import_bundle_data(data)
    print(f"[ci-bootstrap] Imported from {bundle_path}: {result['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
