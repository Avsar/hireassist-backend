"""
test_new_sources.py -- Live smoke test for the Ashby + HomeRun integrations.

Hits the real public endpoints (needs internet). Read-only: does NOT write
to the database.

Usage:
    python test_new_sources.py
"""

from app import ashby_list_jobs, homerun_list_jobs, normalize_jobs
from agent_discover import probe_ashby, probe_homerun, _fetch_board_name

# Known public boards to validate the mechanics against.
ASHBY_BOARDS = ["ashby", "ramp", "linear"]
HOMERUN_SLUGS = ["dopper", "breeze", "woonstad-rotterdam"]


def main():
    print("=" * 60)
    print("ASHBY (public posting API)")
    print("=" * 60)
    for board in ASHBY_BOARDS:
        try:
            count = probe_ashby(board)
            print(f"\n  probe '{board}': {count} jobs")
            if count:
                jobs = normalize_jobs(board.title(), "ashby", board)
                for j in jobs[:3]:
                    print(f"    - {j['title'][:50]:<50} | {j['location_raw'][:25]:<25} "
                          f"| {j['city'] or '?'}, {j['country'] or '?'}")
        except Exception as e:
            print(f"  probe '{board}' FAILED: {e}")

    print()
    print("=" * 60)
    print("HOMERUN (Atom feed / sitemap)")
    print("=" * 60)
    for slug in HOMERUN_SLUGS:
        try:
            count = probe_homerun(slug)
            board_name = _fetch_board_name("homerun", slug)
            print(f"\n  probe '{slug}': {count} jobs | board name: {board_name!r}")
            if count:
                jobs = normalize_jobs(board_name or slug, "homerun", slug)
                for j in jobs[:3]:
                    print(f"    - {j['title'][:50]:<50} | {j['city'] or '?'}, {j['country'] or '?'}")
        except Exception as e:
            print(f"  probe '{slug}' FAILED: {e}")

    print("\nIf both sections show jobs, the integrations work.")
    print("Next: python ats_reverse_discover.py  (picks up Ashby/HomeRun links)")
    print("      python agent_discover.py --region <city>  (probes both platforms)")


if __name__ == "__main__":
    main()
