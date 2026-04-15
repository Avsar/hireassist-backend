"""
retry_push.py -- Re-push the latest bundle to Railway + git without re-running the pipeline.

Use when the daily pipeline completed locally but push or git_push failed
(usually due to a transient network issue). Reads data/seed/bundle.json and
pushes it; also runs `git push` if there are unpushed commits.

Usage:
    python retry_push.py                 # push bundle + git push
    python retry_push.py --skip-render   # only git push
    python retry_push.py --skip-git      # only bundle push
"""

import argparse
import sys
from pathlib import Path

# Reuse the hardened helpers from the main pipeline.
from daily_intelligence import (
    push_to_render,
    git_push_bundle,
    PENDING_PUSH_FILE,
    clear_pending_push,
)

PROJECT_DIR = Path(__file__).parent
BUNDLE_PATH = PROJECT_DIR / "data" / "seed" / "bundle.json"


def main():
    parser = argparse.ArgumentParser(description="Re-push last bundle to Railway + git")
    parser.add_argument("--skip-render", action="store_true", help="Skip Render/Railway push")
    parser.add_argument("--skip-git", action="store_true", help="Skip git push")
    args = parser.parse_args()

    if not BUNDLE_PATH.exists():
        print(f"[ERROR] Bundle not found: {BUNDLE_PATH}")
        print("Run daily_intelligence.py first.")
        sys.exit(1)

    print(f"Bundle: {BUNDLE_PATH}  ({BUNDLE_PATH.stat().st_size / (1024 * 1024):.1f} MB)")

    if PENDING_PUSH_FILE.exists():
        print(f"Found pending push marker: {PENDING_PUSH_FILE.read_text(encoding='utf-8')}")

    ok_render = True
    ok_git = True

    if not args.skip_render:
        ok_render = push_to_render(str(BUNDLE_PATH))

    if not args.skip_git:
        ok_git = git_push_bundle(str(BUNDLE_PATH))

    if ok_render and ok_git:
        clear_pending_push()
        print("\n[OK] All re-push steps succeeded")
        sys.exit(0)
    else:
        print("\n[PARTIAL] Some steps still failing -- check output above")
        sys.exit(1)


if __name__ == "__main__":
    main()
