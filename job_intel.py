"""
job_intel.py -- Job intelligence layer for Hire Assist.

Owns the `jobs` and `company_daily_stats` tables.
Provides: schema migration, stable job-key generation, upsert lifecycle,
daily stats computation, momentum scoring, and query helpers.
"""

import hashlib
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from db_config import get_db_path

DB_FILE = get_db_path()


# ---------------------------------------------------------------------------
# A) Schema & migrations (idempotent)
# ---------------------------------------------------------------------------

def ensure_intel_tables(conn: sqlite3.Connection):
    """Create jobs + company_daily_stats tables and indexes. Safe to call repeatedly."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source          TEXT NOT NULL,
            company_name    TEXT NOT NULL,
            job_key         TEXT NOT NULL,
            title           TEXT NOT NULL,
            location_raw    TEXT NOT NULL DEFAULT '',
            country         TEXT,
            city            TEXT,
            url             TEXT NOT NULL DEFAULT '',
            posted_at       TEXT,
            first_seen_at   TEXT NOT NULL,
            last_seen_at    TEXT NOT NULL,
            is_active       INTEGER NOT NULL DEFAULT 1,
            raw_json        TEXT,
            UNIQUE(source, job_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS company_daily_stats (
            stat_date       TEXT NOT NULL,
            company_name    TEXT NOT NULL,
            source          TEXT NOT NULL,
            active_jobs     INTEGER NOT NULL DEFAULT 0,
            new_jobs        INTEGER NOT NULL DEFAULT 0,
            closed_jobs     INTEGER NOT NULL DEFAULT 0,
            net_change      INTEGER NOT NULL DEFAULT 0,
            UNIQUE(stat_date, company_name, source)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_jobs_company_active
        ON jobs(company_name, is_active)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_jobs_source_active
        ON jobs(source, is_active)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_jobs_first_seen
        ON jobs(first_seen_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_stats_date
        ON company_daily_stats(stat_date)
    """)

    # Migration: add department + job_type columns (idempotent)
    for col, col_def in [("department", "TEXT DEFAULT ''"), ("job_type", "TEXT DEFAULT ''"), ("tech_tags", "TEXT DEFAULT ''")]:
        try:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {col_def}")
        except sqlite3.OperationalError:
            pass  # column already exists

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_jobs_department
        ON jobs(department)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_jobs_city_active
        ON jobs(city, is_active)
    """)
    conn.commit()

    # One-time backfill: extract tech_tags for existing jobs
    backfill_needed = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE is_active=1 AND title != '' AND (tech_tags IS NULL OR tech_tags = '')"
    ).fetchone()[0]
    if backfill_needed > 100:  # only run if significant number needs backfill
        rows = conn.execute("SELECT id, title FROM jobs WHERE tech_tags IS NULL OR tech_tags = ''").fetchall()
        for row_id, title in rows:
            tags = extract_tech_tags(title)
            if tags:
                conn.execute("UPDATE jobs SET tech_tags = ? WHERE id = ?", (tags, row_id))
        conn.commit()


# ---------------------------------------------------------------------------
# B) Title-based department inference (fallback when ATS has no department)
# ---------------------------------------------------------------------------

# Each tuple: (department_label, [keywords that match in lowercase title])
_DEPT_RULES = [
    ("Engineering",     ["software engineer", "backend engineer", "frontend engineer",
                         "full stack engineer", "fullstack engineer", "devops engineer",
                         "site reliability", "sre ", "platform engineer",
                         "cloud engineer", "infrastructure engineer",
                         "embedded engineer", "firmware engineer",
                         "qa engineer", "test engineer", "quality engineer",
                         "mobile engineer", "ios engineer", "android engineer",
                         "machine learning engineer", "ml engineer",
                         "software developer", "web developer",
                         "backend developer", "frontend developer",
                         "full stack developer", "fullstack developer",
                         "java developer", "python developer", ".net developer",
                         "c++ developer", "rust developer", "golang developer",
                         "react developer", "angular developer", "vue developer",
                         "mobile developer", "ios developer", "android developer",
                         "developer", "entwickler", "ontwikkelaar"]),
    ("Data",            ["data scientist", "data engineer", "data analyst",
                         "data architect", "analytics engineer",
                         "machine learning", "ml ", " ai ", "artificial intelligence",
                         "business intelligence", " bi ", "data platform"]),
    ("Design",          ["ux design", "ui design", "product design",
                         "graphic design", "visual design", "interaction design",
                         "ux researcher", "ux writer", "creative director"]),
    ("Product",         ["product manager", "product owner", "product lead",
                         "product director", "product analyst", "scrum master",
                         "agile coach"]),
    ("HR",              ["human resource", "people operations", "people partner",
                         "people manager", "talent acqui", "recruiter",
                         "recruiting", "recruitment", "hr manager",
                         "hr business partner", "hrbp", "people & culture",
                         "employer brand", "compensation", "payroll"]),
    ("Marketing",       ["marketing manager", "marketing director",
                         "content market", "digital market", "growth market",
                         "seo ", "sem ", "social media", "brand manager",
                         "communications manager", "pr manager",
                         "community manager", "marketing specialist",
                         "marketing coordinator", "copywriter"]),
    ("Sales",           ["sales manager", "sales director", "sales represent",
                         "account executive", "account manager",
                         "business development", "sales engineer",
                         "sales consultant", "inside sales", "field sales",
                         "revenue ", "commercial manager"]),
    ("Finance",         ["financial analyst", "finance manager", "accountant",
                         "controller", "financial controller", "cfo ",
                         "treasury", "tax ", "audit", "bookkeeper",
                         "finance director", "fp&a"]),
    ("Legal",           ["legal counsel", "lawyer", "attorney", "jurist",
                         "compliance officer", "compliance manager",
                         "regulatory", "legal advisor", "paralegal",
                         "privacy officer", "dpo "]),
    ("Operations",      ["operations manager", "operations director",
                         "supply chain", "logistics", "procurement",
                         "facility", "warehouse", "inventory",
                         "office manager", "chief operating"]),
    ("Customer Support", ["customer support", "customer service",
                          "customer success", "helpdesk", "help desk",
                          "support engineer", "support specialist",
                          "technical support", "service desk"]),
    ("IT",              ["system admin", "sysadmin", "it manager",
                         "it support", "network engineer", "security engineer",
                         "cybersecurity", "information security",
                         "it director", "ciso", "it specialist"]),
]


def infer_department(title: str) -> str:
    """Infer department from job title keywords. Returns '' if no match."""
    if not title:
        return ""
    t = title.lower()
    for dept, keywords in _DEPT_RULES:
        for kw in keywords:
            if kw in t:
                return dept
    return ""


# ---------------------------------------------------------------------------
# B2) Title-based tech stack extraction
# ---------------------------------------------------------------------------

# (display_name, [patterns that match in lowercase padded title])
_TECH_RULES = [
    # Languages
    ("Python",       [" python "," python,", " python/", " python."]),
    ("JavaScript",   ["javascript"]),
    ("TypeScript",   ["typescript"]),
    ("Java",         [" java ", " java,", " java/", "java developer", "java engineer",
                      "java software", "java backend", "senior java", "lead java",
                      "junior java", "medior java"]),
    ("C#",           [" c# ", " c#,", ".net developer", ".net engineer", "dotnet"]),
    ("C++",          ["c++", " cpp "]),
    ("Go",           [" go ", " go,", " go/", "golang"]),
    ("Rust",         [" rust ", " rust,", " rust/"]),
    ("Kotlin",       ["kotlin"]),
    ("Scala",        [" scala ", " scala,"]),
    ("Ruby",         [" ruby ", " ruby,"]),
    ("PHP",          [" php ", " php,", " php/"]),
    ("Swift",        [" swift ", " swift,"]),
    # Frontend
    ("React",        ["react"]),
    ("Vue",          ["vue.js", "vuejs", " vue ", " vue,"]),
    ("Angular",      ["angular"]),
    ("Next.js",      ["next.js", "nextjs"]),
    ("Svelte",       ["svelte"]),
    # Backend / Frameworks
    ("Node.js",      ["node.js", "nodejs"]),
    ("Django",       ["django"]),
    ("FastAPI",      ["fastapi"]),
    ("Spring",       ["spring boot", " spring "]),
    (".NET",         [" .net ", " .net,", "dotnet", "asp.net"]),
    ("Laravel",      ["laravel"]),
    ("Rails",        [" rails ", "ruby on rails"]),
    # Data
    ("SQL",          [" sql ", " sql,", " sql/"]),
    ("PostgreSQL",   ["postgresql", "postgres"]),
    ("MongoDB",      ["mongodb", "mongo "]),
    ("Redis",        ["redis"]),
    ("Elasticsearch", ["elasticsearch"]),
    ("Kafka",        ["kafka"]),
    ("Spark",        [" spark ", " spark,", "apache spark"]),
    ("Snowflake",    ["snowflake"]),
    ("Databricks",   ["databricks"]),
    # Cloud / DevOps
    ("AWS",          [" aws ", " aws,", " aws/", "amazon web services"]),
    ("Azure",        ["azure"]),
    ("GCP",          [" gcp ", "google cloud"]),
    ("Docker",       ["docker"]),
    ("Kubernetes",   ["kubernetes", " k8s "]),
    ("Terraform",    ["terraform"]),
    ("CI/CD",        ["ci/cd", " cicd "]),
    # Other
    ("GraphQL",      ["graphql"]),
    ("Machine Learning", ["machine learning"]),
    ("AI",           [" ai ", " ai,", " ai/", "artificial intelligence",
                      "generative ai", " genai ", "llm "]),
    ("DevOps",       ["devops"]),
    ("SAP",          [" sap ", " sap,", " sap/"]),
    ("Salesforce",   ["salesforce"]),
    ("Power BI",     ["power bi", "powerbi"]),
    ("Tableau",      ["tableau"]),
]


def extract_tech_tags(title: str) -> str:
    """Extract tech stack tags from job title. Returns pipe-delimited string or ''."""
    if not title:
        return ""
    # Normalize: replace common delimiters with spaces, then pad
    t = title.lower()
    for ch in "()[]{}|/\\&":
        t = t.replace(ch, " ")
    t = f" {t} "  # pad for boundary matching
    tags = []
    seen: set[str] = set()
    for display_name, patterns in _TECH_RULES:
        if display_name in seen:
            continue
        for pat in patterns:
            if pat in t:
                tags.append(display_name)
                seen.add(display_name)
                break
    return "|".join(tags)


# ---------------------------------------------------------------------------
# C) Stable dedupe key per source
# ---------------------------------------------------------------------------

def make_job_key(source: str, job_dict: dict) -> str:
    """Generate a stable dedupe key for a job based on its source.

    ATS sources use the provider's unique ID.
    Scraped/careers_page jobs use sha1(company|title|normalized_url).
    """
    if source in ("greenhouse", "lever", "smartrecruiters", "recruitee"):
        jid = job_dict.get("id")
        if jid is not None:
            return str(jid)
        # Fallback: hash title+url (should not normally happen)

    # careers_page or fallback
    company = (job_dict.get("company") or job_dict.get("company_name") or "").strip().lower()
    title = (job_dict.get("title") or "").strip().lower()
    raw_url = job_dict.get("apply_url") or job_dict.get("url") or ""
    parsed = urlparse(raw_url)
    norm_url = (parsed.netloc + parsed.path).lower().rstrip("/")
    composite = f"{company}|{title}|{norm_url}"
    return hashlib.sha1(composite.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# C) Upsert lifecycle
# ---------------------------------------------------------------------------

def upsert_jobs(conn: sqlite3.Connection, source: str, company_name: str,
                job_dicts: list, now: str | None = None) -> dict:
    """Upsert a batch of jobs for one (company, source) pair.

    - New jobs: INSERT with first_seen_at = last_seen_at = now.
    - Existing jobs: UPDATE last_seen_at, re-activate.
    - Jobs NOT in batch: SET is_active = 0.
    - Empty batch: do nothing (safe; mirrors agent_scrape.py zero-result logic).

    Returns dict: {new, updated, deactivated, total}
    """
    if not job_dicts:
        return {"new": 0, "updated": 0, "deactivated": 0, "total": 0}

    if now is None:
        now = datetime.now(timezone.utc).isoformat()

    stats = {"new": 0, "updated": 0, "deactivated": 0, "total": len(job_dicts)}
    seen_keys = set()

    for jd in job_dicts:
        job_key = make_job_key(source, jd)
        seen_keys.add(job_key)

        title = jd.get("title") or ""
        location_raw = jd.get("location_raw") or ""
        country = jd.get("country") or None
        city = jd.get("city") or None
        url = jd.get("apply_url") or jd.get("url") or ""
        posted_at = jd.get("updated_at") or jd.get("posted_at") or None
        department = jd.get("department") or ""
        if not department:
            department = infer_department(title)
        job_type = jd.get("job_type") or ""
        tech_tags = jd.get("tech_tags") or extract_tech_tags(title)

        existing = conn.execute(
            "SELECT id, is_active FROM jobs WHERE source=? AND job_key=?",
            (source, job_key),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE jobs SET
                    title=?, location_raw=?, country=?, city=?, url=?,
                    department=?, job_type=?, tech_tags=?,
                    posted_at=COALESCE(?, posted_at),
                    last_seen_at=?, is_active=1
                WHERE source=? AND job_key=?""",
                (title, location_raw, country, city, url,
                 department, job_type, tech_tags,
                 posted_at, now, source, job_key),
            )
            stats["updated"] += 1
        else:
            conn.execute(
                """INSERT INTO jobs
                    (source, company_name, job_key, title, location_raw, country, city,
                     url, department, job_type, tech_tags,
                     posted_at, first_seen_at, last_seen_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (source, company_name, job_key, title, location_raw, country, city,
                 url, department, job_type, tech_tags,
                 posted_at, now, now),
            )
            stats["new"] += 1

    # Deactivate jobs for this company+source that were NOT in this batch
    placeholders = ",".join("?" for _ in seen_keys)
    result = conn.execute(
        f"""UPDATE jobs SET is_active=0, last_seen_at=?
            WHERE company_name=? AND source=? AND is_active=1
            AND job_key NOT IN ({placeholders})""",
        [now, company_name, source] + list(seen_keys),
    )
    stats["deactivated"] = result.rowcount

    conn.commit()
    return stats


# ---------------------------------------------------------------------------
# D) Daily stats computation
# ---------------------------------------------------------------------------

def compute_daily_stats(conn: sqlite3.Connection, stat_date: str | None = None):
    """Compute and store daily stats for every (company, source) pair.

    Uses the `jobs` table to derive active/new/closed counts, then UPSERTs
    into company_daily_stats. Safe to run multiple times for the same date.
    """
    if stat_date is None:
        stat_date = date.today().isoformat()

    next_day = (date.fromisoformat(stat_date) + timedelta(days=1)).isoformat()
    prev_date = (date.fromisoformat(stat_date) - timedelta(days=1)).isoformat()

    # Active jobs per (company, source)
    active_rows = conn.execute("""
        SELECT company_name, source, COUNT(*) as cnt
        FROM jobs WHERE is_active = 1
        GROUP BY company_name, source
    """).fetchall()

    # New jobs today: first_seen_at falls within [stat_date, stat_date+1)
    new_rows = conn.execute("""
        SELECT company_name, source, COUNT(*) as cnt
        FROM jobs
        WHERE first_seen_at >= ? AND first_seen_at < ?
        GROUP BY company_name, source
    """, (stat_date, next_day)).fetchall()
    new_map = {(r[0], r[1]): r[2] for r in new_rows}

    # Previous day's active counts for net_change / closed calculation
    prev_rows = conn.execute("""
        SELECT company_name, source, active_jobs
        FROM company_daily_stats WHERE stat_date = ?
    """, (prev_date,)).fetchall()
    prev_map = {(r[0], r[1]): r[2] for r in prev_rows}

    for row in active_rows:
        company_name, source, active_jobs = row[0], row[1], row[2]
        new_jobs = new_map.get((company_name, source), 0)
        prev_active = prev_map.get((company_name, source), 0)
        net_change = active_jobs - prev_active
        closed_jobs = max(0, prev_active + new_jobs - active_jobs)

        conn.execute("""
            INSERT INTO company_daily_stats
                (stat_date, company_name, source, active_jobs, new_jobs, closed_jobs, net_change)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stat_date, company_name, source) DO UPDATE SET
                active_jobs=excluded.active_jobs,
                new_jobs=excluded.new_jobs,
                closed_jobs=excluded.closed_jobs,
                net_change=excluded.net_change
        """, (stat_date, company_name, source, active_jobs, new_jobs, closed_jobs, net_change))

    conn.commit()


# ---------------------------------------------------------------------------
# E) Momentum score
# ---------------------------------------------------------------------------

def momentum_score(new_jobs: int, net_change: int, active_jobs: int) -> float:
    """Momentum score (0-100). Higher = more hiring activity."""
    raw = 10 * new_jobs + 2 * net_change + math.log(active_jobs + 1) * 5
    return max(0.0, min(100.0, raw))


# ---------------------------------------------------------------------------
# F) Query helpers (for API endpoints)
# ---------------------------------------------------------------------------

def get_company_stats(conn: sqlite3.Connection, stat_date: str | None = None) -> list:
    """Per-company stats for a date, sorted by momentum descending."""
    if stat_date is None:
        stat_date = date.today().isoformat()

    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT company_name,
               SUM(active_jobs) as active_jobs,
               SUM(new_jobs) as new_jobs,
               SUM(closed_jobs) as closed_jobs,
               SUM(net_change) as net_change
        FROM company_daily_stats
        WHERE stat_date = ?
        GROUP BY company_name
    """, (stat_date,)).fetchall()

    results = []
    for r in rows:
        m = momentum_score(r["new_jobs"], r["net_change"], r["active_jobs"])
        results.append({
            "company_name": r["company_name"],
            "active_jobs": r["active_jobs"],
            "new_jobs": r["new_jobs"],
            "closed_jobs": r["closed_jobs"],
            "net_change": r["net_change"],
            "momentum": round(m, 1),
        })

    results.sort(key=lambda x: x["momentum"], reverse=True)
    return results


def get_company_history(conn: sqlite3.Connection, company_name: str,
                        days: int = 30) -> list:
    """Daily time series for one company over N days."""
    since = (date.today() - timedelta(days=days)).isoformat()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT stat_date,
               SUM(active_jobs) as active_jobs,
               SUM(new_jobs) as new_jobs,
               SUM(closed_jobs) as closed_jobs,
               SUM(net_change) as net_change
        FROM company_daily_stats
        WHERE company_name = ? AND stat_date >= ?
        GROUP BY stat_date
        ORDER BY stat_date
    """, (company_name, since)).fetchall()

    return [dict(r) for r in rows]


def get_summary_stats(conn: sqlite3.Connection, days: int = 7) -> dict:
    """Aggregate summary across all companies for N days."""
    since = (date.today() - timedelta(days=days)).isoformat()
    conn.row_factory = sqlite3.Row

    row = conn.execute("""
        SELECT COUNT(DISTINCT company_name) as companies_tracked,
               SUM(new_jobs) as total_new_jobs,
               SUM(closed_jobs) as total_closed_jobs
        FROM company_daily_stats
        WHERE stat_date >= ?
    """, (since,)).fetchone()

    active_row = conn.execute(
        "SELECT COUNT(*) as total_active FROM jobs WHERE is_active = 1"
    ).fetchone()

    return {
        "period_days": days,
        "since": since,
        "companies_tracked": row["companies_tracked"] or 0,
        "total_active_jobs": active_row["total_active"] or 0,
        "total_new_jobs": row["total_new_jobs"] or 0,
        "total_closed_jobs": row["total_closed_jobs"] or 0,
    }


# ---------------------------------------------------------------------------
# G.  Smart Alerts – hiring surges, slowdowns, new entrants, gone dark
# ---------------------------------------------------------------------------

def _detect_surges(conn, min_active: int) -> list[dict]:
    rows = conn.execute("""
        WITH latest AS (SELECT MAX(stat_date) as d FROM company_daily_stats),
        today AS (
            SELECT company_name,
                   SUM(new_jobs) as today_new,
                   SUM(active_jobs) as today_active
            FROM company_daily_stats, latest
            WHERE stat_date = latest.d
            GROUP BY company_name
            HAVING today_active >= ? AND today_new >= 3
        ),
        baseline AS (
            SELECT company_name, AVG(day_new) as avg_new_7d
            FROM (
                SELECT company_name, stat_date, SUM(new_jobs) as day_new
                FROM company_daily_stats, latest
                WHERE stat_date >= date(latest.d, '-7 days') AND stat_date < latest.d
                GROUP BY company_name, stat_date
            ) GROUP BY company_name
        )
        SELECT t.company_name, t.today_new, t.today_active,
               COALESCE(b.avg_new_7d, 0) as avg_new_7d
        FROM today t LEFT JOIN baseline b ON t.company_name = b.company_name
        WHERE t.today_new >= 3
          AND (b.avg_new_7d IS NULL OR b.avg_new_7d < 1 OR t.today_new >= b.avg_new_7d * 3)
        ORDER BY t.today_new DESC LIMIT 10
    """, (min_active,)).fetchall()
    return [{
        "type": "surge", "headline": "Hiring Surge",
        "company_name": r[0],
        "detail": f"+{r[1]} new today (avg {r[3]:.0f})",
        "active_jobs": r[2],
        "severity": "high" if r[1] >= 10 else "medium",
    } for r in rows]


def _detect_slowdowns(conn, min_active: int) -> list[dict]:
    rows = conn.execute("""
        WITH latest AS (SELECT MAX(stat_date) as d FROM company_daily_stats),
        recent AS (
            SELECT company_name, SUM(total_net) as net_3d, MAX(total_active) as current_active
            FROM (
                SELECT company_name, stat_date,
                       SUM(net_change) as total_net, SUM(active_jobs) as total_active
                FROM company_daily_stats, latest
                WHERE stat_date >= date(latest.d, '-2 days')
                GROUP BY company_name, stat_date
            ) GROUP BY company_name
            HAVING current_active >= ?
        )
        SELECT company_name, net_3d, current_active FROM recent
        WHERE net_3d <= -5 AND ABS(net_3d) >= current_active * 0.05
        ORDER BY net_3d ASC LIMIT 10
    """, (min_active,)).fetchall()
    return [{
        "type": "slowdown", "headline": "Hiring Slowdown",
        "company_name": r[0],
        "detail": f"{r[1]:+d} net (3d), {r[2]} active",
        "active_jobs": r[2],
        "severity": "high" if abs(r[1]) >= r[2] * 0.1 else "medium",
    } for r in rows]


def _detect_new_entrants(conn, min_active: int) -> list[dict]:
    rows = conn.execute("""
        WITH latest AS (SELECT MAX(stat_date) as d FROM company_daily_stats)
        SELECT company_name, MIN(stat_date) as first_seen, MAX(active_jobs) as active_jobs
        FROM company_daily_stats, latest
        GROUP BY company_name
        HAVING first_seen >= date(latest.d, '-2 days') AND active_jobs >= ?
        ORDER BY active_jobs DESC LIMIT 10
    """, (min_active,)).fetchall()
    return [{
        "type": "new_entrant", "headline": "New Entrant",
        "company_name": r[0],
        "detail": f"{r[2]} jobs, first seen {r[1]}",
        "active_jobs": r[2],
        "severity": "medium",
    } for r in rows]


def _detect_gone_dark(conn, min_active: int) -> list[dict]:
    rows = conn.execute("""
        WITH latest AS (SELECT MAX(stat_date) as d FROM company_daily_stats),
        prev AS (
            SELECT company_name, MAX(active_jobs) as peak_active
            FROM company_daily_stats, latest
            WHERE stat_date < latest.d
            GROUP BY company_name HAVING peak_active >= ?
        ),
        current AS (
            SELECT company_name FROM company_daily_stats, latest
            WHERE stat_date = latest.d AND active_jobs > 0
        )
        SELECT p.company_name, p.peak_active FROM prev p
        WHERE p.company_name NOT IN (SELECT company_name FROM current)
        ORDER BY p.peak_active DESC LIMIT 10
    """, (min_active,)).fetchall()
    return [{
        "type": "gone_dark", "headline": "Gone Dark",
        "company_name": r[0],
        "detail": f"Was {r[1]} jobs, now 0",
        "active_jobs": 0,
        "severity": "high",
    } for r in rows]


def detect_alerts(conn: sqlite3.Connection, min_active: int = 5) -> list[dict]:
    """Detect hiring alerts from company_daily_stats. Returns list of alert dicts."""
    seen: set[str] = set()
    all_alerts: list[dict] = []
    # Priority order: surge > gone_dark > slowdown > new_entrant
    for fn in (_detect_surges, _detect_gone_dark, _detect_slowdowns, _detect_new_entrants):
        for alert in fn(conn, min_active):
            if alert["company_name"] not in seen:
                seen.add(alert["company_name"])
                all_alerts.append(alert)
    return all_alerts
