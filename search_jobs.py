"""
search_jobs.py -- Targeted job search by department, city, and keywords.

Usage:
    python search_jobs.py --department hr --city eindhoven
    python search_jobs.py --department engineering --city amsterdam
    python search_jobs.py --title "python developer" --city eindhoven
    python search_jobs.py --department hr --city eindhoven --discover
    python search_jobs.py --list-departments
    python search_jobs.py --list-departments --city eindhoven
"""

import argparse
import sqlite3
import subprocess
import sys
from collections import Counter

from db_config import get_db_path
from job_intel import ensure_intel_tables

DB_FILE = get_db_path()

# ---------------------------------------------------------------------------
# Department alias map for fuzzy matching
# ---------------------------------------------------------------------------

DEPARTMENT_ALIASES = {
    "hr": ["human resources", "people", "people & culture", "people operations",
            "talent", "talent acquisition", "recruiting", "recruitment"],
    "engineering": ["software engineering", "engineering", "development",
                    "software development", "tech", "technology", "r&d",
                    "research & development", "product development"],
    "marketing": ["marketing", "growth", "brand", "communications",
                  "content", "digital marketing"],
    "sales": ["sales", "business development", "account management",
              "revenue", "commercial"],
    "finance": ["finance", "accounting", "financial", "fp&a", "treasury"],
    "design": ["design", "ux", "ui", "product design", "creative"],
    "data": ["data", "data science", "data engineering", "analytics",
             "machine learning", "ai", "artificial intelligence"],
    "operations": ["operations", "ops", "supply chain", "logistics"],
    "product": ["product", "product management"],
    "legal": ["legal", "compliance", "regulatory"],
    "support": ["customer support", "customer success", "customer service",
                "support", "helpdesk"],
}


def match_department(query: str, department: str) -> bool:
    """Fuzzy match a search query against a department name."""
    q = query.lower().strip()
    dept = department.lower().strip()

    if not dept:
        return False

    # Direct substring match
    if q in dept or dept in q:
        return True

    # Check aliases
    for _canonical, aliases in DEPARTMENT_ALIASES.items():
        all_terms = [_canonical] + aliases
        query_matches = q in all_terms or any(q in a for a in all_terms)
        if query_matches:
            dept_matches = any(a in dept or dept in a for a in all_terms)
            if dept_matches:
                return True

    return False


# ---------------------------------------------------------------------------
# Core search
# ---------------------------------------------------------------------------

def search_jobs(department: str = None, city: str = None,
                title_query: str = None, active_only: bool = True) -> list[dict]:
    """Search jobs by department, city, and/or title keyword."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    ensure_intel_tables(conn)

    clauses = []
    params = []

    if active_only:
        clauses.append("is_active = 1")

    if city:
        clauses.append("LOWER(city) = LOWER(?)")
        params.append(city)

    if title_query:
        clauses.append("LOWER(title) LIKE ?")
        params.append(f"%{title_query.lower()}%")

    where = " AND ".join(clauses) if clauses else "1=1"
    rows = conn.execute(
        f"SELECT * FROM jobs WHERE {where} ORDER BY company_name, title",
        params,
    ).fetchall()

    results = [dict(r) for r in rows]

    # Apply department fuzzy matching in Python
    if department:
        results = [r for r in results if match_department(department, r.get("department", ""))]

    conn.close()
    return results


def list_departments(city: str = None) -> list[tuple[str, int]]:
    """List all unique departments with job counts."""
    conn = sqlite3.connect(DB_FILE)
    ensure_intel_tables(conn)

    if city:
        rows = conn.execute(
            "SELECT department, COUNT(*) as cnt FROM jobs "
            "WHERE is_active = 1 AND department != '' AND LOWER(city) = LOWER(?) "
            "GROUP BY department ORDER BY cnt DESC",
            (city,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT department, COUNT(*) as cnt FROM jobs "
            "WHERE is_active = 1 AND department != '' "
            "GROUP BY department ORDER BY cnt DESC",
        ).fetchall()

    conn.close()
    return [(r[0], r[1]) for r in rows]


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def safe_print(text: str):
    """Print with ASCII fallback for Windows cp1252."""
    print(text.encode("ascii", "replace").decode("ascii"))


def print_results(results: list[dict], limit: int = 50):
    if not results:
        print("\n  No jobs found matching your criteria.\n")
        return

    shown = min(limit, len(results))
    print(f"\n{'=' * 80}")
    safe_print(f"  Found {len(results)} matching jobs (showing {shown})")
    print(f"{'=' * 80}\n")

    for i, job in enumerate(results[:limit], 1):
        dept = job.get("department", "") or "-"
        city = job.get("city", "") or "-"
        jtype = job.get("job_type", "") or ""
        type_str = f" | {jtype}" if jtype else ""
        safe_print(f"  {i:3}. {job['title']}")
        safe_print(f"       {job['company_name']} | {city} | {dept}{type_str}")
        if job.get("url"):
            safe_print(f"       {job['url']}")
        print()

    # Summary by company
    companies = Counter(r["company_name"] for r in results)
    print(f"{'=' * 80}")
    safe_print(f"  {len(results)} jobs across {len(companies)} companies:")
    for name, count in companies.most_common(20):
        safe_print(f"    {name}: {count} jobs")
    if len(companies) > 20:
        print(f"    ... and {len(companies) - 20} more companies")
    print(f"{'=' * 80}")


def print_departments(departments: list[tuple[str, int]], city: str = None):
    label = f" in {city}" if city else ""
    print(f"\n{'=' * 60}")
    safe_print(f"  Departments with active jobs{label}")
    print(f"{'=' * 60}\n")

    for dept, count in departments[:50]:
        safe_print(f"  {count:5} jobs  {dept}")

    total = sum(c for _, c in departments)
    print(f"\n  Total: {total} jobs with department data across {len(departments)} departments")
    print(f"{'=' * 60}")


# ---------------------------------------------------------------------------
# Discover pipeline
# ---------------------------------------------------------------------------

def run_discover_pipeline(city: str):
    """Run discover -> sync pipeline for a city."""
    print(f"\n  Step 1/2: Discovering companies in {city}...")
    subprocess.run(
        [sys.executable, "agent_discover.py",
         "--source", "google", "--region", city,
         "--limit", "20", "--daily-target", "20"],
        check=False,
    )

    print(f"\n  Step 2/2: Syncing ATS jobs...")
    subprocess.run(
        [sys.executable, "sync_ats_jobs.py"],
        check=False,
    )
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Search jobs by department, city, and keywords",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python search_jobs.py --department hr --city eindhoven
  python search_jobs.py --department engineering --city amsterdam
  python search_jobs.py --title "python developer" --city eindhoven
  python search_jobs.py --department hr --city eindhoven --discover
  python search_jobs.py --list-departments
  python search_jobs.py --list-departments --city eindhoven
        """,
    )
    parser.add_argument("--department", "-d", help="Department (e.g., hr, engineering, data, sales)")
    parser.add_argument("--city", "-c", help="City name (e.g., eindhoven, amsterdam)")
    parser.add_argument("--title", "-t", help="Title keyword (e.g., 'python developer')")
    parser.add_argument("--discover", action="store_true",
                        help="Run discovery + ATS sync for the city first")
    parser.add_argument("--limit", type=int, default=50, help="Max results to display (default: 50)")
    parser.add_argument("--list-departments", action="store_true",
                        help="List all departments with job counts")
    args = parser.parse_args()

    if args.list_departments:
        depts = list_departments(args.city)
        print_departments(depts, args.city)
        sys.exit(0)

    if not args.department and not args.city and not args.title:
        parser.print_help()
        sys.exit(1)

    if args.discover:
        if not args.city:
            print("  --discover requires --city")
            sys.exit(1)
        run_discover_pipeline(args.city)

    results = search_jobs(
        department=args.department,
        city=args.city,
        title_query=args.title,
    )
    print_results(results, limit=args.limit)
