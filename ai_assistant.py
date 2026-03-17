"""
ai_assistant.py -- AI Job Assistant for HireAssist.

Uses Claude Haiku with tool_use to answer natural language job queries
by searching the local jobs database and the web.
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import time as _time
from datetime import date, datetime, timezone
from urllib.parse import urlparse

from db_config import get_db_path

logger = logging.getLogger(__name__)
DB_FILE = get_db_path()

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are the HireAssist job search assistant for the Netherlands tech job market.
You help users find hidden jobs directly from company career pages — not from LinkedIn or Indeed.

Guidelines:
- Always search the local database first using search_jobs.
- If search_jobs returns fewer than 3 results, ALSO use discover_jobs to find hidden jobs
  from company career pages. discover_jobs searches for Greenhouse, Lever, SmartRecruiters,
  Recruitee, and Ashby career pages and calls their APIs directly. It also scrapes non-ATS
  career pages. New companies found are added for daily monitoring.
- Use web_search for salary info, company reviews, market trends, or general questions.
- Default to Netherlands jobs. Only mention non-NL if the user asks.
- Keep responses concise (2-4 sentences + job list if relevant).
- When showing jobs, include the top 5-10 most relevant results.
- If no results found anywhere, suggest broadening the search.
- Format job titles, company names, and cities clearly.
- Do NOT invent jobs or companies. Only report what the tools return.
"""

# ---------------------------------------------------------------------------
# Tool definitions (Claude tool_use format)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "search_jobs",
        "description": "Search for job listings in the Netherlands. Returns matching jobs with title, company, city, and apply URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "q": {
                    "type": "string",
                    "description": "Keyword to search in job titles (e.g. 'python developer', 'data engineer', 'recruiter')",
                },
                "city": {
                    "type": "string",
                    "description": "City name to filter by (e.g. 'Amsterdam', 'Eindhoven', 'Rotterdam')",
                },
                "company": {
                    "type": "string",
                    "description": "Company name to filter by (e.g. 'Adyen', 'Booking.com')",
                },
                "lang": {
                    "type": "string",
                    "enum": ["en", "nl"],
                    "description": "Filter by posting language: 'en' for English, 'nl' for Dutch",
                },
                "new_today": {
                    "type": "boolean",
                    "description": "If true, only return jobs first seen today",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 10, max 20)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "search_companies",
        "description": "Search for companies in the database. Can filter by city or whether they have active job listings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "Filter companies by city of their job listings",
                },
                "has_jobs_only": {
                    "type": "boolean",
                    "description": "If true, only return companies with active job listings",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 20)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "company_momentum",
        "description": "Get hiring momentum and stats for a specific company. Shows active jobs, new jobs, trend over recent days.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {
                    "type": "string",
                    "description": "Company name (e.g. 'Adyen', 'Booking.com')",
                },
            },
            "required": ["company_name"],
        },
    },
    {
        "name": "market_summary",
        "description": "Get overall job market summary: total active jobs, new today, companies tracked, trends.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "web_search",
        "description": "Search the web for general information: salary ranges, company reviews, industry trends, career advice for the Netherlands job market. Use when the local database cannot answer the question.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (e.g. 'average Python developer salary Netherlands 2025')",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "discover_jobs",
        "description": "Discover hidden jobs by searching for company career pages (not job boards). Finds companies with Greenhouse, Lever, SmartRecruiters, Recruitee, Ashby career pages and calls their APIs directly. Also scrapes non-ATS career pages. Results are saved to the database. Use when local database has few results for a query.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": "Job role to search for (e.g. 'data engineer', 'recruiter')",
                },
                "city": {
                    "type": "string",
                    "description": "City in Netherlands (e.g. 'Eindhoven', 'Amsterdam')",
                },
            },
            "required": ["role"],
        },
    },
]

# ---------------------------------------------------------------------------
# Web search helpers
# ---------------------------------------------------------------------------

# Rate limiting: max 10 web searches per minute globally
_web_search_ts: list[float] = []
_WEB_RATE_LIMIT = 10


def _check_web_rate() -> bool:
    now = _time.time()
    _web_search_ts[:] = [t for t in _web_search_ts if now - t < 60]
    if len(_web_search_ts) >= _WEB_RATE_LIMIT:
        return False
    _web_search_ts.append(now)
    return True


# ATS URL patterns for detection
_ATS_PATTERNS = {
    "greenhouse": re.compile(r"boards\.greenhouse\.io/([a-zA-Z0-9_\-]+)"),
    "lever": re.compile(r"jobs\.lever\.co/([a-zA-Z0-9_\-]+)"),
    "smartrecruiters": re.compile(r"jobs\.smartrecruiters\.com/([a-zA-Z0-9_\-]+)"),
    "recruitee": re.compile(r"([a-zA-Z0-9_\-]+)\.recruitee\.com"),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([a-zA-Z0-9_\-]+)"),
}

# Domains to exclude from DDG results (aggregators, not direct career pages)
_EXCLUDED_DOMAINS = {
    "linkedin.com", "indeed.com", "indeed.nl", "glassdoor.com", "glassdoor.nl",
    "monster.com", "monster.nl", "jooble.org", "ziprecruiter.com",
    "nationalevacaturebank.nl", "simplyhired.nl", "adzuna.nl", "jobbird.com",
    "werk.nl", "intermediair.nl", "jobted.nl", "expatjobs.eu",
}


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


def _execute_tool(name: str, args: dict) -> str:
    """Execute a tool and return JSON string result."""
    try:
        if name == "search_jobs":
            return _tool_search_jobs(args)
        elif name == "search_companies":
            return _tool_search_companies(args)
        elif name == "company_momentum":
            return _tool_company_momentum(args)
        elif name == "market_summary":
            return _tool_market_summary(args)
        elif name == "web_search":
            return _tool_web_search(args)
        elif name == "discover_jobs":
            return _tool_discover_jobs(args)
        else:
            return json.dumps({"error": f"Unknown tool: {name}"})
    except Exception as e:
        logger.error("Tool %s failed: %s", name, e)
        return json.dumps({"error": str(e)})


def _tool_search_jobs(args: dict) -> str:
    # Import here to avoid circular imports (app.py imports us)
    from app import aggregate_jobs, soft_country_match

    limit = min(args.get("limit", 10), 20)
    jobs = aggregate_jobs(
        company=args.get("company"),
        q=args.get("q"),
        country="Netherlands",
        city=args.get("city"),
        lang=args.get("lang"),
        new_today_only=args.get("new_today", False),
    )
    # Trim to limit
    jobs = jobs[:limit]
    # Return compact format for Claude
    results = []
    for j in jobs:
        results.append({
            "title": j["title"],
            "company": j["company"],
            "city": j.get("city", ""),
            "url": j.get("apply_url", ""),
            "new_today": j.get("is_new_today", False),
        })
    return json.dumps({"count": len(results), "total_matching": len(jobs), "jobs": results})


def _tool_search_companies(args: dict) -> str:
    city = args.get("city", "")
    has_jobs = args.get("has_jobs_only", True)
    limit = min(args.get("limit", 20), 50)

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row

    if has_jobs:
        sql = """
            SELECT company_name, COUNT(*) as job_count
            FROM jobs WHERE is_active = 1
        """
        params = []
        if city:
            sql += " AND LOWER(city) = ?"
            params.append(city.lower())
        sql += " GROUP BY company_name ORDER BY job_count DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        results = [{"company": r["company_name"], "active_jobs": r["job_count"]} for r in rows]
    else:
        sql = "SELECT name, source FROM companies WHERE active = 1"
        params = []
        sql += " ORDER BY name LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        results = [{"company": r["name"], "source": r["source"]} for r in rows]

    conn.close()
    return json.dumps({"count": len(results), "companies": results})


def _tool_company_momentum(args: dict) -> str:
    import job_intel

    company = args["company_name"]
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    job_intel.ensure_intel_tables(conn)

    # Current active jobs for this company
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM jobs WHERE is_active = 1 AND company_name = ?",
        (company,),
    ).fetchone()
    active = row["cnt"] if row else 0

    # Try fuzzy match if exact match returns 0
    if active == 0:
        row = conn.execute(
            "SELECT company_name, COUNT(*) as cnt FROM jobs WHERE is_active = 1 AND LOWER(company_name) LIKE ? GROUP BY company_name ORDER BY cnt DESC LIMIT 1",
            (f"%{company.lower()}%",),
        ).fetchone()
        if row:
            company = row["company_name"]
            active = row["cnt"]

    history = job_intel.get_company_history(conn, company, days=14)
    conn.close()

    if active == 0:
        return json.dumps({"error": f"No active jobs found for '{args['company_name']}'"})

    return json.dumps({
        "company": company,
        "active_jobs": active,
        "history_14d": history[-7:] if history else [],
    })


def _tool_market_summary(args: dict) -> str:
    import job_intel

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    job_intel.ensure_intel_tables(conn)

    summary = job_intel.get_summary_stats(conn, days=7)

    today = date.today().isoformat()
    new_today = conn.execute(
        "SELECT COUNT(*) as cnt FROM jobs WHERE is_active = 1 AND DATE(first_seen_at) = ?",
        (today,),
    ).fetchone()["cnt"]

    total_companies = conn.execute("SELECT COUNT(*) FROM companies WHERE active = 1").fetchone()[0]

    conn.close()

    summary["new_today"] = new_today
    summary["total_companies"] = total_companies
    return json.dumps(summary)


# ---------------------------------------------------------------------------
# Web search tools
# ---------------------------------------------------------------------------


def _tool_web_search(args: dict) -> str:
    """General web search via DuckDuckGo."""
    query = args.get("query", "")
    if not query:
        return json.dumps({"error": "No query provided"})
    if not _check_web_rate():
        return json.dumps({"error": "Web search rate limit reached. Try again in a moment."})

    try:
        from ddgs import DDGS
        results = DDGS().text(query, max_results=5)
    except Exception as e:
        logger.error("Web search failed: %s", e)
        return json.dumps({"error": f"Web search failed: {e}"})

    compact = []
    for r in results or []:
        compact.append({
            "title": r.get("title", ""),
            "url": r.get("href", ""),
            "snippet": (r.get("body", "") or "")[:300],
        })
    return json.dumps({"count": len(compact), "results": compact})


def _detect_ats(url: str) -> tuple[str, str] | None:
    """Detect ATS platform and extract token from a URL.
    Returns (source, token) or None.
    """
    for source, pattern in _ATS_PATTERNS.items():
        m = pattern.search(url)
        if m:
            return (source, m.group(1))
    return None


def _is_excluded_domain(url: str) -> bool:
    """Check if URL belongs to an excluded job board."""
    try:
        host = urlparse(url).netloc.lower()
        return any(host == d or host.endswith("." + d) for d in _EXCLUDED_DOMAINS)
    except Exception:
        return False


def _fetch_ats_jobs(source: str, token: str) -> list[dict]:
    """Fetch jobs from an ATS API using existing normalize_jobs().
    Returns list of normalized job dicts.
    """
    try:
        from app import normalize_jobs
        return normalize_jobs("_pending_", source, token)
    except Exception as e:
        logger.warning("ATS fetch failed (%s/%s): %s", source, token, e)
        return []


def _try_lightweight_scrape(url: str) -> list[dict]:
    """Lightweight HTTP scrape of a career page — JSON-LD + HTML heuristics."""
    try:
        import requests as _req
        from bs4 import BeautifulSoup
        from agent_scrape import _parse_jsonld, _parse_html_heuristics

        resp = _req.get(url, timeout=10, headers={"User-Agent": "HireAssist/0.3"})
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        jobs = _parse_jsonld(soup, url)
        if not jobs:
            jobs = _parse_html_heuristics(soup, url)
        return jobs
    except Exception as e:
        logger.debug("Lightweight scrape failed (%s): %s", url, e)
        return []


def _filter_jobs(jobs: list[dict], role: str, city: str) -> list[dict]:
    """Filter job list by role keyword in title and optional city match."""
    role_lower = role.lower()
    role_words = role_lower.split()
    matched = []
    for j in jobs:
        title = (j.get("title") or "").lower()
        # Check if any role word appears in the title
        if not any(w in title for w in role_words):
            continue
        # City filter (if specified)
        if city:
            job_city = (j.get("city") or j.get("location_raw") or "").lower()
            if city.lower() not in job_city:
                continue
        matched.append(j)
    return matched


def _ingest_discovered_jobs(company_name: str, source: str, token: str,
                            jobs: list[dict]) -> dict:
    """Insert discovered jobs into DB and add company to companies table."""
    import job_intel

    if not jobs:
        return {"new": 0, "updated": 0}

    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    job_intel.ensure_intel_tables(conn)

    # Add company to companies table if not exists (for daily pipeline)
    if source in ("greenhouse", "lever", "smartrecruiters", "recruitee", "ashby"):
        existing_co = conn.execute(
            "SELECT id FROM companies WHERE source=? AND token=?",
            (source, token),
        ).fetchone()
        if not existing_co:
            conn.execute(
                """INSERT INTO companies (name, source, token, active, discovered_at)
                   VALUES (?, ?, ?, 1, ?)""",
                (company_name, source, token, now),
            )
            logger.info("Added new company: %s (%s/%s)", company_name, source, token)

    # Insert/update jobs without deactivation (discover context — we may not
    # have the full job list, so upsert_jobs deactivation would be destructive)
    stats = {"new": 0, "updated": 0}
    for jd in jobs:
        job_key = job_intel.make_job_key(source, jd)
        title = jd.get("title") or ""
        if not title:
            continue
        existing = conn.execute(
            "SELECT id FROM jobs WHERE source=? AND job_key=?",
            (source, job_key),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE jobs SET last_seen_at=?, is_active=1 WHERE source=? AND job_key=?",
                (now, source, job_key),
            )
            stats["updated"] += 1
        else:
            city = jd.get("city") or None
            url = jd.get("apply_url") or jd.get("url") or ""
            department = jd.get("department") or job_intel.infer_department(title)
            tech_tags = jd.get("tech_tags") or job_intel.extract_tech_tags(title)
            conn.execute(
                """INSERT INTO jobs
                   (source, company_name, job_key, title, location_raw, country, city,
                    url, department, job_type, tech_tags,
                    posted_at, first_seen_at, last_seen_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (source, company_name, job_key, title,
                 jd.get("location_raw", ""), "Netherlands", city,
                 url, department, "", tech_tags, None, now, now),
            )
            stats["new"] += 1

    conn.commit()
    conn.close()
    return stats


def _tool_discover_jobs(args: dict) -> str:
    """Discover hidden jobs from company career pages (not job boards)."""
    role = args.get("role", "")
    city = args.get("city", "")
    if not role:
        return json.dumps({"error": "No role specified"})
    if not _check_web_rate():
        return json.dumps({"error": "Rate limit reached. Try again in a moment."})

    try:
        from ddgs import DDGS
        ddgs = DDGS()
    except Exception as e:
        logger.error("DDG search failed: %s", e)
        return json.dumps({"error": f"Search failed: {e}"})

    # Build queries targeting career pages, NOT job boards
    city_part = f" {city}" if city else ""
    nl_part = f" {city} Netherlands" if city else " Netherlands"
    queries = [
        f'{role}{city_part} careers werken-bij vacature',
        f'{role}{nl_part} site:boards.greenhouse.io',
        f'{role}{nl_part} site:jobs.lever.co',
        f'{role}{nl_part} site:jobs.smartrecruiters.com',
        f'{role}{nl_part} site:recruitee.com',
    ]

    # Collect unique URLs from DDG
    seen_urls = set()
    career_urls = []  # (url, title)
    for q in queries:
        try:
            n = 5 if "site:" in q else 10
            hits = ddgs.text(q, max_results=n) or []
            for r in hits:
                url = r.get("href", "")
                if url and url not in seen_urls and not _is_excluded_domain(url):
                    seen_urls.add(url)
                    career_urls.append((url, r.get("title", "")))
        except Exception as e:
            logger.warning("DDG query failed: %s", e)

    if not career_urls:
        return json.dumps({"jobs": [], "companies_found": 0, "count": 0})

    # Process career pages (max 5 to stay within time budget)
    all_matched_jobs = []
    companies_found = 0
    seen_ats = set()  # Avoid duplicate ATS calls

    for url, ddg_title in career_urls[:8]:
        ats = _detect_ats(url)

        if ats:
            source, token = ats
            ats_key = f"{source}:{token}"
            if ats_key in seen_ats:
                continue
            seen_ats.add(ats_key)

            # Fetch all jobs from this ATS board
            if source == "ashby":
                from agent_scrape import _scrape_ashby_api
                raw_jobs = _scrape_ashby_api(token)
                # Ashby returns {title, location_raw, apply_url} — needs company name
                for j in raw_jobs:
                    j["company"] = ddg_title.split(" - ")[0].split("|")[0].strip() or token
            else:
                raw_jobs = _fetch_ats_jobs(source, token)

            if not raw_jobs:
                continue

            # Derive company name: prefer DDG title, fall back to token
            ddg_name = ddg_title.split(" - ")[0].split("|")[0].strip()
            # DDG title for ATS often contains the job title, not company
            # Token is more reliable for company name
            company_name = token.replace("-", " ").replace("_", " ").title()
            if ddg_name and len(ddg_name) < 40 and not any(
                w in ddg_name.lower() for w in ["job", "hiring", "career", "open position"]
            ):
                company_name = ddg_name
            # Update company name in jobs (normalize_jobs uses placeholder)
            for j in raw_jobs:
                j["company"] = company_name

            # Filter by role and city
            matched = _filter_jobs(raw_jobs, role, city)
            if matched:
                companies_found += 1
                _ingest_discovered_jobs(company_name, source, token, raw_jobs)
                for j in matched:
                    all_matched_jobs.append({
                        "title": j.get("title", ""),
                        "company": company_name,
                        "city": j.get("city") or j.get("location_raw", ""),
                        "url": j.get("apply_url") or j.get("url", ""),
                        "source": source,
                    })
        else:
            # Non-ATS career page — try lightweight scrape
            raw_jobs = _try_lightweight_scrape(url)
            if not raw_jobs:
                continue

            # Derive company from domain
            host = urlparse(url).netloc
            company_name = host.replace("www.", "").split(".")[0].title()

            matched = _filter_jobs(raw_jobs, role, city)
            if matched:
                companies_found += 1
                _ingest_discovered_jobs(company_name, "careers_page", url, matched)
                for j in matched:
                    all_matched_jobs.append({
                        "title": j.get("title", ""),
                        "company": company_name,
                        "city": j.get("city") or j.get("location_raw", ""),
                        "url": j.get("apply_url") or "",
                        "source": "careers_page",
                    })

    return json.dumps({
        "jobs": all_matched_jobs,
        "companies_found": companies_found,
        "count": len(all_matched_jobs),
    })


# ---------------------------------------------------------------------------
# Chat handler
# ---------------------------------------------------------------------------


def handle_chat(messages: list[dict]) -> dict:
    """Process a chat conversation. Returns {"reply": str, "jobs": list}."""
    try:
        from anthropic import Anthropic
    except ImportError:
        return {
            "reply": "AI assistant is not available (anthropic package not installed).",
            "jobs": [],
        }

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {
            "reply": "AI assistant is not configured. Please set ANTHROPIC_API_KEY.",
            "jobs": [],
        }

    client = Anthropic(api_key=api_key)

    # Trim conversation to last 10 messages
    trimmed = messages[-10:]

    collected_jobs = []
    current_messages = list(trimmed)

    # Tool use loop (max 4 iterations — DB search, then web if needed)
    for _ in range(4):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=current_messages,
        )

        # Check if Claude wants to use tools
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        text_blocks = [b for b in resp.content if b.type == "text"]

        if not tool_uses:
            # No tool calls -- return the text response
            reply = text_blocks[0].text if text_blocks else "I couldn't find an answer."
            break

        # Execute tool calls and feed results back
        # First, add the assistant's response (with tool_use blocks) to messages
        current_messages.append({"role": "assistant", "content": resp.content})

        tool_results = []
        for tu in tool_uses:
            result_str = _execute_tool(tu.name, tu.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": result_str,
            })

            # Collect jobs from search_jobs and discover_jobs results
            if tu.name in ("search_jobs", "discover_jobs"):
                try:
                    data = json.loads(result_str)
                    collected_jobs.extend(data.get("jobs", []))
                except (json.JSONDecodeError, KeyError):
                    pass

        current_messages.append({"role": "user", "content": tool_results})
    else:
        # Exhausted iterations -- get whatever text we have
        reply = text_blocks[0].text if text_blocks else "I found some results but had trouble summarizing them."

    return {"reply": reply, "jobs": collected_jobs}
