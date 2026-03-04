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
You help users find jobs, explore companies, and understand hiring trends.

Guidelines:
- Always search the local database first using search_jobs.
- If search_jobs returns fewer than 3 results, ALSO use web_search_jobs to find more
  from the internet. Those results get saved for future users automatically.
- Use web_search for salary info, company reviews, market trends, or any question
  the local database cannot answer.
- Default to Netherlands jobs. Only mention non-NL if the user asks.
- Keep responses concise (2-4 sentences + job list if relevant).
- When showing jobs, include the top 5-10 most relevant results.
- If no results found anywhere, suggest broadening the search.
- You can answer general career advice briefly, but steer back to job search.
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
        "name": "web_search_jobs",
        "description": "Search the web for job listings in the Netherlands. Finds jobs on LinkedIn, Indeed, career pages, and other job boards. Results are automatically saved to the database for future users. Use when local database search returns few results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "role": {
                    "type": "string",
                    "description": "Job role/title to search for (e.g. 'data engineer', 'React developer')",
                },
                "city": {
                    "type": "string",
                    "description": "City in Netherlands (e.g. 'Amsterdam', 'Rotterdam')",
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


# URL patterns for individual job listings (parseable + ingestible)
_INDIVIDUAL_PATTERNS = [
    r"linkedin\.com/jobs/view/",
    r"indeed\.\w+/viewjob",
    r"indeed\.\w+/rc/clk",
    r"glassdoor\.\w+/job-listing/",
    r"boards\.greenhouse\.io/.+/jobs/\d+",
    r"jobs\.lever\.co/.+/[a-f0-9-]+",
    r"jobs\.smartrecruiters\.com/.+/\d+",
    r"/careers?/.+/\d+",
    r"/jobs?/\d+",
]

# Aggregator/index pages -- return as reference, don't ingest
_AGGREGATOR_PATTERNS = [
    r"indeed\.\w+/q-.*-vacatures",
    r"indeed\.\w+/jobs\?q=",
    r"linkedin\.com/jobs/search",
    r"glassdoor\.\w+/Job/",
]


def _parse_job_from_result(title: str, url: str, body: str) -> dict | None:
    """Extract structured job data from a DuckDuckGo search result."""
    parsed = None

    if "linkedin.com" in url:
        # English: "Senior Data Engineer - Amsterdam | Booking.com | LinkedIn"
        # Dutch: "KPN zoekt een Python Developer in Amsterdam, Noord-Holland"
        # Also: "Company hiring Job Title in City | LinkedIn"
        dutch_m = re.match(
            r"(.+?)\s+zoekt een\s+(.+?)(?:\s+in\s+(.+?))?$", title
        )
        hiring_m = re.match(
            r"(.+?)\s+hiring\s+(.+?)(?:\s+in\s+(.+?))?(?:\s*\|.*)?$", title
        )
        if dutch_m:
            parsed = {
                "title": dutch_m.group(2).strip(),
                "company": dutch_m.group(1).strip(),
                "city": dutch_m.group(3) or "",
            }
        elif hiring_m:
            parsed = {
                "title": hiring_m.group(2).strip(),
                "company": hiring_m.group(1).strip(),
                "city": hiring_m.group(3) or "",
            }
        else:
            parts = title.split(" | ")
            if len(parts) >= 2:
                job_part = parts[0]
                company = parts[-2] if len(parts) >= 3 else ""
                segments = job_part.rsplit(" - ", 1)
                job_title = segments[0].strip()
                city = segments[1].strip() if len(segments) > 1 else ""
                parsed = {"title": job_title, "company": company.strip(), "city": city}

    elif "indeed" in url:
        # "Data Engineer - Amsterdam - Booking.com | Indeed.nl"
        clean = re.sub(r"\s*\|\s*Indeed.*$", "", title)
        parts = clean.rsplit(" - ", 2)
        if len(parts) >= 2:
            job_title = parts[0].strip()
            city = parts[1].strip() if len(parts) >= 3 else ""
            company = parts[2].strip() if len(parts) >= 3 else parts[1].strip()
            parsed = {"title": job_title, "company": company, "city": city}

    elif "glassdoor" in url:
        # "Data Engineer - Company Name | Glassdoor"
        clean = re.sub(r"\s*\|\s*Glassdoor.*$", "", title)
        parts = clean.split(" - ", 1)
        if len(parts) >= 2:
            parsed = {"title": parts[0].strip(), "company": parts[1].strip(), "city": ""}

    else:
        # Generic: "Title at Company in City" or "Title - Company"
        m = re.match(r"(.+?)\s+(?:at|bij|@)\s+(.+?)(?:\s+in\s+(.+))?$", title)
        if m:
            parsed = {"title": m.group(1), "company": m.group(2), "city": m.group(3) or ""}

    if parsed:
        parsed["url"] = url
        # Clean city: strip province, country, and trailing junk
        if parsed["city"]:
            c = parsed["city"]
            # Remove ", Noord-Holland, Nederland" etc.
            c = re.sub(
                r",?\s*(Noord-Holland|Zuid-Holland|North Holland|South Holland|"
                r"Utrecht|Gelderland|Noord-Brabant|Limburg|Overijssel|Flevoland|"
                r"Groningen|Friesland|Drenthe|Zeeland|Netherlands|Nederland|NL|"
                r"the Netherlands).*$",
                "", c, flags=re.IGNORECASE,
            ).strip().rstrip(",. ")
            # Remove " | LinkedIn" suffix
            c = re.sub(r"\s*\|.*$", "", c).strip()
            parsed["city"] = c if c else ""

    return parsed


def _ingest_web_jobs(job_dicts: list[dict]) -> dict:
    """Insert web-found jobs into DB without deactivating existing ones."""
    import job_intel

    if not job_dicts:
        return {"new": 0, "skipped": 0}

    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    job_intel.ensure_intel_tables(conn)

    stats = {"new": 0, "skipped": 0}
    source = "web_search"

    for jd in job_dicts:
        job_key = job_intel.make_job_key(source, jd)
        company = jd.get("company") or "Unknown"
        title = jd.get("title", "")
        if not title:
            continue
        city = jd.get("city") or None
        url = jd.get("url", "")
        department = job_intel.infer_department(title)
        tech_tags = job_intel.extract_tech_tags(title)

        existing = conn.execute(
            "SELECT id FROM jobs WHERE source=? AND job_key=?",
            (source, job_key),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE jobs SET last_seen_at=?, is_active=1 WHERE source=? AND job_key=?",
                (now, source, job_key),
            )
            stats["skipped"] += 1
        else:
            conn.execute(
                """INSERT INTO jobs
                   (source, company_name, job_key, title, location_raw, country, city,
                    url, department, job_type, tech_tags,
                    posted_at, first_seen_at, last_seen_at, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (source, company, job_key, title, "", "Netherlands", city,
                 url, department, "", tech_tags, None, now, now),
            )
            stats["new"] += 1

    conn.commit()
    conn.close()
    logger.info("Web job ingestion: %d new, %d existing", stats["new"], stats["skipped"])
    return stats


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
        elif name == "web_search_jobs":
            return _tool_web_search_jobs(args)
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


def _tool_web_search_jobs(args: dict) -> str:
    """Search web for job listings, parse results, ingest into DB."""
    role = args.get("role", "")
    city = args.get("city", "")
    if not role:
        return json.dumps({"error": "No role specified"})
    if not _check_web_rate():
        return json.dumps({"error": "Web search rate limit reached. Try again in a moment."})

    try:
        from ddgs import DDGS
        ddgs = DDGS()
    except Exception as e:
        logger.error("Web job search failed: %s", e)
        return json.dumps({"error": f"Search failed: {e}"})

    # Two-pass search: general query + targeted LinkedIn for individual listings
    city_part = f" {city}" if city else ""
    queries = [
        f"{role} jobs{city_part} Netherlands",
        f"{role}{city_part} Netherlands site:linkedin.com/jobs/view",
    ]

    all_results = []
    seen_urls = set()
    for q in queries:
        try:
            hits = ddgs.text(q, max_results=10) or []
            for r in hits:
                url = r.get("href", "")
                if url not in seen_urls:
                    seen_urls.add(url)
                    all_results.append(r)
        except Exception as e:
            logger.warning("DDG query failed (%s): %s", q[:40], e)

    if not all_results:
        return json.dumps({"jobs": [], "references": [], "ingested_new": 0, "count": 0})

    parsed_jobs = []
    references = []

    for r in all_results:
        url = r.get("href", "")
        title = r.get("title", "")
        body = r.get("body", "") or ""

        # Skip aggregator pages
        if any(re.search(pat, url) for pat in _AGGREGATOR_PATTERNS):
            references.append({"title": title, "url": url, "snippet": body[:200]})
            continue

        # Try to parse individual job listings
        is_individual = any(re.search(pat, url) for pat in _INDIVIDUAL_PATTERNS)
        if is_individual:
            parsed = _parse_job_from_result(title, url, body)
            if parsed and parsed.get("title"):
                parsed_jobs.append(parsed)
                continue

        # Fallback: add as reference
        references.append({"title": title, "url": url, "snippet": body[:200]})

    # Ingest parsed jobs into DB
    ingestion = _ingest_web_jobs(parsed_jobs)

    # Return results for Claude
    job_results = []
    for j in parsed_jobs:
        job_results.append({
            "title": j["title"],
            "company": j.get("company", ""),
            "city": j.get("city", ""),
            "url": j["url"],
            "source": "web",
        })

    return json.dumps({
        "jobs": job_results,
        "references": references[:5],
        "ingested_new": ingestion["new"],
        "count": len(job_results),
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

            # Collect jobs from search_jobs and web_search_jobs results
            if tu.name in ("search_jobs", "web_search_jobs"):
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
