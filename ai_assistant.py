"""
ai_assistant.py -- AI Job Assistant for HireAssist.

Uses Claude Haiku with tool_use to answer natural language job queries
by searching the local jobs database.
"""

import json
import logging
import os
import sqlite3
from datetime import date

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
- Always search the database before answering job-related questions.
- Default to Netherlands jobs. Only mention non-NL if the user asks.
- Keep responses concise (2-4 sentences + job list if relevant).
- When showing jobs, include the top 5-10 most relevant results.
- If no results found, suggest broadening the search.
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
]

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

    # Tool use loop (max 3 iterations)
    for _ in range(3):
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

            # Collect jobs from search_jobs results
            if tu.name == "search_jobs":
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
