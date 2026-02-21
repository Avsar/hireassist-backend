from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
import requests
import csv
import json
import os
from pathlib import Path
from db_config import get_db_path
from html import escape
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse
import re
import logging
import sqlite3
from bs4 import BeautifulSoup

# Intelligence layer (optional -- endpoints degrade gracefully if not present)
try:
    import job_intel
    _HAS_INTEL = True
except ImportError:
    _HAS_INTEL = False

logger = logging.getLogger(__name__)

app = FastAPI()

# CORS
_cors_raw = os.environ.get("CORS_ORIGINS", "")
_cors_origins = [o.strip() for o in _cors_raw.split(",") if o.strip()] if _cors_raw else [
    "http://localhost:3000",
    "http://localhost:8000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Iframe embedding -- allow framing by cubea.nl and localhost
class _FrameHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        # Remove any X-Frame-Options that downstream code might set
        if "X-Frame-Options" in response.headers:
            del response.headers["X-Frame-Options"]
        response.headers["Content-Security-Policy"] = (
            "frame-ancestors https://cubea.nl https://www.cubea.nl http://localhost:* https://localhost:*"
        )
        return response


app.add_middleware(_FrameHeadersMiddleware)

SEED_FILE = Path("companies_seed.csv")  # committed starter set
BUNDLE_SEED = Path("data/seed/bundle.json")  # full dataset for Render cold starts
DB_FILE = get_db_path()

# ---- HTTP helper (timeouts + retries) ----
SESSION = requests.Session()
DEFAULT_HEADERS = {"User-Agent": "HireAssist/0.1 (+local dev)"}


def http_get_json(url: str, timeout: int = 45, retries: int = 2):
    last_err = None
    for _ in range(retries + 1):
        try:
            r = SESSION.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
    raise last_err


# In-memory cache for Greenhouse job detail (English-only)
DETAIL_CACHE = {}  # (source, token, job_id) -> {"text": str, "time": datetime}

# In-memory cache for job listings per company
JOB_CACHE = {}  # (source, token) -> {"jobs": list, "time": datetime}
JOB_CACHE_TTL = timedelta(minutes=10)


# ----------------------------
# Bundle import (reusable for startup + HTTP endpoint)
# ----------------------------
def _import_bundle_data(data: dict) -> dict:
    """Import bundle data dict into the database. Returns summary."""
    import time as _time
    t0 = _time.time()
    summary: dict = {}

    with sqlite3.connect(DB_FILE) as conn:
        # -- companies: upsert by UNIQUE(source, token) --
        rows = data.get("companies", [])
        for r in rows:
            conn.execute(
                """INSERT INTO companies (name, source, token, active, confidence, discovered_at, last_verified_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source, token) DO UPDATE SET
                     name=excluded.name, active=excluded.active,
                     confidence=excluded.confidence, last_verified_at=excluded.last_verified_at""",
                (r["name"], r["source"], r["token"],
                 r.get("active", 1), r.get("confidence", "manual"),
                 r.get("discovered_at"), r.get("last_verified_at")),
            )
        summary["companies"] = len(rows)

        # -- scraped_jobs: replace per company_name --
        rows = data.get("scraped_jobs", [])
        replaced_companies = set()
        for r in rows:
            cn = r["company_name"]
            if cn not in replaced_companies:
                conn.execute("DELETE FROM scraped_jobs WHERE company_name = ?", (cn,))
                replaced_companies.add(cn)
            conn.execute(
                """INSERT INTO scraped_jobs (company_name, career_url, title, location_raw, apply_url, scraped_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (cn, r["career_url"], r["title"],
                 r.get("location_raw", ""), r["apply_url"], r["scraped_at"]),
            )
        summary["scraped_jobs"] = len(rows)
        summary["scraped_companies_replaced"] = len(replaced_companies)

        # -- jobs: upsert by UNIQUE(source, job_key) --
        rows = data.get("jobs", [])
        if rows:
            if _HAS_INTEL:
                job_intel.ensure_intel_tables(conn)
            for r in rows:
                conn.execute(
                    """INSERT INTO jobs (source, company_name, job_key, title, location_raw,
                          country, city, url, posted_at, first_seen_at, last_seen_at, is_active, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source, job_key) DO UPDATE SET
                         company_name=excluded.company_name, title=excluded.title,
                         location_raw=excluded.location_raw, country=excluded.country,
                         city=excluded.city, url=excluded.url, posted_at=excluded.posted_at,
                         last_seen_at=excluded.last_seen_at, is_active=excluded.is_active,
                         raw_json=excluded.raw_json""",
                    (r["source"], r["company_name"], r["job_key"], r["title"],
                     r.get("location_raw", ""), r.get("country"), r.get("city"),
                     r.get("url", ""), r.get("posted_at"),
                     r["first_seen_at"], r["last_seen_at"],
                     r.get("is_active", 1), r.get("raw_json")),
                )
        summary["jobs"] = len(rows)

        # -- company_daily_stats: upsert by UNIQUE(stat_date, company_name, source) --
        rows = data.get("company_daily_stats", [])
        if rows:
            if _HAS_INTEL:
                job_intel.ensure_intel_tables(conn)
            for r in rows:
                conn.execute(
                    """INSERT INTO company_daily_stats
                          (stat_date, company_name, source, active_jobs, new_jobs, closed_jobs, net_change)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(stat_date, company_name, source) DO UPDATE SET
                         active_jobs=excluded.active_jobs, new_jobs=excluded.new_jobs,
                         closed_jobs=excluded.closed_jobs, net_change=excluded.net_change""",
                    (r["stat_date"], r["company_name"], r["source"],
                     r.get("active_jobs", 0), r.get("new_jobs", 0),
                     r.get("closed_jobs", 0), r.get("net_change", 0)),
                )
        summary["company_daily_stats"] = len(rows)

    elapsed_ms = int((_time.time() - t0) * 1000)
    JOB_CACHE.clear()
    logger.info("Bundle imported: %s (%dms)", summary, elapsed_ms)
    return {"summary": summary, "elapsed_ms": elapsed_ms}


# ----------------------------
# DB init + companies
# ----------------------------
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source TEXT NOT NULL,
                token TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                confidence TEXT NOT NULL DEFAULT 'manual',
                discovered_at TEXT,
                last_verified_at TEXT,
                UNIQUE(source, token)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scraped_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                career_url TEXT NOT NULL,
                title TEXT NOT NULL,
                location_raw TEXT NOT NULL DEFAULT '',
                apply_url TEXT NOT NULL,
                scraped_at TEXT NOT NULL
            )
        """)
        # Seed from CSV on first run if table is empty
        count = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        if count == 0 and SEED_FILE.exists():
            seeded = 0
            with SEED_FILE.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    name = (row.get("name") or "").strip()
                    source = (row.get("source") or "").strip().lower()
                    token = (row.get("token") or "").strip()
                    confidence = (row.get("confidence") or "manual").strip()
                    if name and source and token:
                        conn.execute(
                            "INSERT OR IGNORE INTO companies (name, source, token, confidence) VALUES (?, ?, ?, ?)",
                            (name, source, token, confidence)
                        )
                        seeded += 1
            logger.info("Seeded %d companies from %s", seeded, SEED_FILE)
        elif count == 0:
            logger.warning("companies.db is empty and no seed CSV found — run discover.py")

    # Auto-import bundle on fresh DB (Render cold starts)
    # Check scraped_jobs count — if 0, the bundle hasn't been imported yet
    with sqlite3.connect(DB_FILE) as conn:
        scraped = conn.execute("SELECT COUNT(*) FROM scraped_jobs").fetchone()[0]
    if scraped == 0 and BUNDLE_SEED.exists():
        try:
            bundle = json.loads(BUNDLE_SEED.read_text(encoding="utf-8"))
            result = _import_bundle_data(bundle.get("data", {}))
            logger.info("Auto-imported seed bundle: %s", result["summary"])
        except Exception as e:
            logger.warning("Failed to auto-import seed bundle: %s", e)


init_db()


def load_companies():
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name, source, token FROM companies WHERE active = 1 ORDER BY name"
        ).fetchall()
        return [{"name": r["name"], "source": r["source"], "token": r["token"]} for r in rows]


# ----------------------------
# Career page scraper
# ----------------------------
_JOB_URL_RE = re.compile(
    r'/(jobs?|careers?|positions?|openings?|vacatures?|rollen?|roles?|apply|solliciteer)/[\w%.\-]{3,}',
    re.IGNORECASE,
)

_SKIP_TITLES = {
    "jobs", "careers", "vacatures", "apply", "see all jobs", "view all jobs",
    "all jobs", "current openings", "open positions", "view openings",
    "job listings", "join us", "work with us", "browse jobs",
    "vacancies", "openings", "our story", "personal stories", "blog",
    "insights", "news", "team", "people", "culture", "about us",
    "life at", "benefits", "diversity", "students", "for students",
    "learn more", "read more", "show more", "see more", "view more",
    "find out more", "apply now", "get started", "explore", "discover",
    "blog post", "read the blog", "show me life at asml",
    "tech", "design", "marketing", "engineering", "finance",
    "see all open roles", "see all roles", "view all roles", "see open positions",
    "explore opportunities", "explore roles", "explore jobs",
}


def scrape_career_page(url: str, company_name: str) -> list[dict]:
    """
    Scrape individual job listings from a career page.

    Strategy 1 — JSON-LD: many modern career pages embed structured
      <script type="application/ld+json"> blocks with @type=JobPosting.
    Strategy 2 — HTML heuristics: find <a> tags whose href path matches
      common job-URL patterns (e.g. /jobs/123-title, /careers/openings/456).
    Returns an empty list if nothing useful is found (caller shows a browse card).
    """
    try:
        r = SESSION.get(url, headers=DEFAULT_HEADERS, timeout=10, allow_redirects=True)
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            return []
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return []

    jobs = []

    # --- Strategy 1: JSON-LD JobPosting ---
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = (script.string or "").strip()
            if not raw:
                continue
            data = json.loads(raw)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict) or item.get("@type") != "JobPosting":
                    continue
                title = (item.get("title") or "").strip()
                if not title:
                    continue
                loc = item.get("jobLocation") or {}
                if isinstance(loc, list):
                    loc = loc[0] if loc else {}
                addr = loc.get("address", {}) if isinstance(loc, dict) else {}
                if isinstance(addr, str):
                    location_raw = addr
                else:
                    parts = [addr.get("addressLocality", ""), addr.get("addressCountry", "")]
                    location_raw = ", ".join(p for p in parts if p)
                apply_url = item.get("url") or item.get("sameAs") or url
                date_posted = item.get("datePosted", "")
                jobs.append({
                    "company": company_name, "source": "careers_page", "token": url,
                    "id": apply_url,
                    "title": title,
                    "department": item.get("occupationalCategory", ""),
                    "job_type": item.get("employmentType", ""),
                    "snippet": make_snippet(item.get("description", "") or ""),
                    "location_raw": location_raw,
                    "city": None, "country": None,
                    "apply_url": apply_url,
                    "updated_at": date_posted,
                    "is_new_today": is_new_today(date_posted),
                })
        except Exception:
            pass
    if jobs:
        return jobs

    # --- Strategy 2: HTML heuristics ---
    base_netloc = urlparse(url).netloc
    _NAV_TAGS = {"nav", "header", "footer"}
    _STOP_TAGS = {"body", "html"}
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full_url = urljoin(url, href)
        parsed = urlparse(full_url)
        if parsed.netloc != base_netloc:
            continue
        if full_url in seen_urls:
            continue
        if not _JOB_URL_RE.search(parsed.path):
            continue
        path_parts = [p for p in parsed.path.split("/") if p]
        if len(path_parts) < 2:
            continue
        # The final segment must look like a job posting, not a category page:
        #   - at least 3 segments deep (after stripping language prefix), OR has a digit, OR slug ≥25 chars
        last_seg = path_parts[-1]
        # Strip leading language segment (e.g. "en", "nl", "en-us") for depth check
        effective_parts = path_parts[1:] if re.match(r'^[a-z]{2}(-[a-z]{2})?$', path_parts[0]) else path_parts
        if len(effective_parts) < 3 and not re.search(r'\d', last_seg) and len(last_seg) < 25:
            continue
        # Skip if any URL segment is a known category/navigation term
        slug_words = last_seg.lower().replace('-', ' ')
        if last_seg.lower() in _SKIP_TITLES or slug_words in _SKIP_TITLES:
            continue
        # Skip content/blog/navigation paths
        _CONTENT_SEGMENTS = {
            "insights", "blog", "news", "stories", "story", "people",
            "tech", "learn", "teams", "team", "hiring-101",
        }
        if any(seg.lower() in _CONTENT_SEGMENTS for seg in path_parts):
            continue
        # Skip "life-at-*" culture pages
        if any(seg.lower().startswith("life-at") for seg in path_parts):
            continue
        # Skip links inside navigation / header / footer
        if any(p.name in _NAV_TAGS for p in a.parents if p.name):
            continue

        seen_urls.add(full_url)
        title = None
        location_raw = ""

        # Get raw anchor text for later decision-making
        anchor_text_raw = re.sub(r'\s+', ' ', a.get_text(separator=" ", strip=True)).strip()

        # 1. Heading inside the anchor itself
        for htag in ("h1", "h2", "h3", "h4", "h5", "strong", "b"):
            el = a.find(htag)
            if el:
                t = el.get_text(strip=True)
                if 8 <= len(t) <= 120:
                    title = t
                    break

        # 2. Anchor text (when the anchor IS the title link)
        if not title:
            t = re.sub(r'\s+(apply(\s+now)?|apply here|bekijk)$', '', anchor_text_raw, flags=re.IGNORECASE).strip()
            if 8 <= len(t) <= 120 and t.lower() not in _SKIP_TITLES:
                title = t

        # 3. Walk up ancestors to find a heading sibling in a job container
        #    Only do this for icon/empty anchor links (< 4 chars), NOT for CTA buttons.
        #    CTA buttons ("See all open roles", "Browse jobs") may lead to category pages
        #    and their container heading is a section title, not a job title.
        if not title and len(anchor_text_raw) < 4:
            for ancestor in a.parents:
                aname = getattr(ancestor, 'name', None)
                if not aname or aname in _STOP_TAGS:
                    break
                heading = None
                for htag in ("h1", "h2", "h3", "h4", "h5"):
                    heading = ancestor.find(htag)
                    if heading:
                        break
                if heading:
                    t = heading.get_text(strip=True)
                    if 8 <= len(t) <= 120 and t.lower() not in _SKIP_TITLES:
                        title = t
                        # Grab location from the same container (text minus the title)
                        container_text = ancestor.get_text(separator=" | ", strip=True)
                        leftover = container_text.replace(t, "").strip(" |").strip()
                        if leftover and len(leftover) < 120:
                            location_raw = leftover
                    break

        if not title or title.lower() in _SKIP_TITLES:
            continue
        # Skip blog/news content (common false positive on culture pages)
        if re.search(r'\d+\s+min\s+read', title, re.IGNORECASE):
            continue
        if title in seen_titles:
            continue
        seen_titles.add(title)

        jobs.append({
            "company": company_name, "source": "careers_page", "token": url,
            "id": full_url,
            "title": title,
            "department": "", "job_type": "",
            "snippet": "",
            "location_raw": location_raw,
            "city": None, "country": None,
            "apply_url": full_url,
            "updated_at": "",
            "is_new_today": False,
        })

    return jobs


# ----------------------------
# Source: Greenhouse
# ----------------------------
def gh_list_jobs(board_token: str):
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
    data = http_get_json(url, timeout=45, retries=1)
    return data.get("jobs", [])


def gh_job_detail(board_token: str, job_id: int):
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}"
    return http_get_json(url, timeout=60, retries=1)


# ----------------------------
# Source: Lever
# ----------------------------
def lever_list_jobs(company: str):
    url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    data = http_get_json(url, timeout=60, retries=2)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("data", [])
    return []


# ----------------------------
# Source: SmartRecruiters
# ----------------------------
def sr_list_jobs(company_identifier: str):
    url = f"https://api.smartrecruiters.com/v1/companies/{company_identifier}/postings"
    data = http_get_json(url, timeout=60, retries=1)
    return data.get("content", [])


# ----------------------------
# Source: Recruitee
# ----------------------------
def recruitee_list_jobs(company: str):
    url = f"https://{company}.recruitee.com/api/offers/"
    data = http_get_json(url, timeout=60, retries=1)
    return data.get("offers", [])


# ----------------------------
# Helpers
# ----------------------------
def html_to_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def make_snippet(html: str, max_len: int = 220) -> str:
    text = html_to_text(html)
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "…"


def is_english(text: str) -> bool:
    t = (text or "").lower()
    dutch_markers = ["vacature", "solliciteer", "functie", "wij bieden", "wij vragen", "je bent", "nederlands"]
    return sum(w in t for w in dutch_markers) < 2


def is_new_today(updated_at: str):
    if not updated_at:
        return False
    try:
        dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - dt <= timedelta(hours=24)
    except Exception:
        return False


_NL_CITIES = frozenset({
    "amsterdam", "rotterdam", "utrecht", "eindhoven", "den haag", "the hague",
    "groningen", "tilburg", "almere", "breda", "nijmegen", "enschede",
    "haarlem", "arnhem", "delft", "maastricht", "apeldoorn", "leiden",
    "zwolle", "deventer", "helmond", "alkmaar", "zaandam", "amersfoort",
    "hilversum", "dordrecht", "zoetermeer", "leeuwarden", "ede", "emmen",
    "venlo", "schiedam", "purmerend", "gouda", "hoofddorp", "amstelveen",
})


def split_city_country(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None, None

    text = raw.lower()
    country = None
    if any(k in text for k in ["netherlands", "nederland", " nl", "(nl", "nl)"]):
        country = "Netherlands"
    elif any(city in text for city in _NL_CITIES):
        country = "Netherlands"

    if "," in raw:
        city = raw.split(",")[0].strip()
    elif " - " in raw:
        city = raw.split(" - ")[-1].strip()
    else:
        city = raw

    if "remote" in text:
        city = None

    return city, country


def unique(values):
    return sorted({v for v in values if v})


def soft_country_match(job, country: str):
    if not country:
        return True
    cl = country.lower()
    parsed = (job.get("country") or "").lower()
    raw = (job.get("location_raw") or "").lower()

    if parsed == cl:
        return True
    if cl in raw:
        return True

    if cl == "netherlands":
        if any(x in raw for x in ["netherlands", "nederland", " nl", "(nl", "nl)"]):
            return True

    return False


# ----------------------------
# Normalize jobs per source
# ----------------------------
def normalize_jobs(company_name: str, source: str, token: str):
    jobs = []

    if source == "greenhouse":
        for j in gh_list_jobs(token):
            loc_raw = (j.get("location") or {}).get("name", "") or ""
            city, country = split_city_country(loc_raw)
            depts = j.get("departments") or []
            jobs.append({
                "company": company_name,
                "source": source,
                "token": token,
                "id": j.get("id"),
                "title": j.get("title", "") or "",
                "department": depts[0].get("name", "") if depts else "",
                "job_type": "",
                "snippet": "",
                "location_raw": loc_raw,
                "city": city,
                "country": country,
                "apply_url": j.get("absolute_url", "") or "",
                "updated_at": j.get("updated_at") or "",
                "is_new_today": is_new_today(j.get("updated_at") or "")
            })

    elif source == "lever":
        for j in lever_list_jobs(token):
            title = j.get("text", "") or ""
            cats = j.get("categories") or {}
            loc_raw = cats.get("location", "") or ""
            city, country = split_city_country(loc_raw)
            created_ms = j.get("createdAt")
            updated_str = ""
            if created_ms:
                try:
                    updated_str = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).isoformat()
                except Exception:
                    pass
            jobs.append({
                "company": company_name,
                "source": source,
                "token": token,
                "id": j.get("id"),
                "title": title,
                "department": cats.get("department", "") or "",
                "job_type": cats.get("commitment", "") or "",
                "snippet": make_snippet(j.get("description", "") or ""),
                "location_raw": loc_raw,
                "city": city,
                "country": country,
                "apply_url": j.get("hostedUrl", "") or "",
                "updated_at": updated_str,
                "is_new_today": is_new_today(updated_str)
            })

    elif source == "smartrecruiters":
        for j in sr_list_jobs(token):
            title = j.get("name", "") or ""
            loc_obj = j.get("location") or {}
            loc_raw = ", ".join([x for x in [loc_obj.get("city"), loc_obj.get("country")] if x]) or ""
            city, country = split_city_country(loc_raw)
            ref = j.get("ref", "") or j.get("id", "")
            apply_url = ""
            if ref:
                apply_url = f"https://jobs.smartrecruiters.com/{token}/{ref}"
            jobs.append({
                "company": company_name,
                "source": source,
                "token": token,
                "id": ref,
                "title": title,
                "department": (j.get("department") or {}).get("label", "") or "",
                "job_type": (j.get("typeOfEmployment") or {}).get("label", "") or "",
                "snippet": "",
                "location_raw": loc_raw,
                "city": city,
                "country": country,
                "apply_url": apply_url,
                "updated_at": "",
                "is_new_today": False
            })

    elif source == "recruitee":
        for j in recruitee_list_jobs(token):
            title = j.get("title", "") or ""
            loc_raw = j.get("location", "") or ""
            city, country = split_city_country(loc_raw)
            apply_url = j.get("careers_url", "") or j.get("url", "") or ""
            created_at = j.get("created_at", "") or ""
            emp_type = (j.get("employment_type_code", "") or "").replace("_", " ").title()
            jobs.append({
                "company": company_name,
                "source": source,
                "token": token,
                "id": j.get("id") or j.get("slug", ""),
                "title": title,
                "department": j.get("category", "") or "",
                "job_type": emp_type,
                "snippet": make_snippet(j.get("description", "") or ""),
                "location_raw": loc_raw,
                "city": city,
                "country": country,
                "apply_url": apply_url,
                "updated_at": created_at,
                "is_new_today": is_new_today(created_at)
            })

    elif source == "careers_page":
        # Read pre-scraped jobs from scraped_jobs table (populated by agent_scrape.py)
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT title, location_raw, apply_url FROM scraped_jobs WHERE company_name=? AND career_url=?",
                (company_name, token),
            ).fetchall()
        for row in rows:
            loc_for_parse = row["location_raw"].split("|")[0].strip() if "|" in row["location_raw"] else row["location_raw"]
            city, country = split_city_country(loc_for_parse)
            if not country:
                country = "Netherlands"
            jobs.append({
                "company": company_name, "source": source, "token": token,
                "id": row["apply_url"],
                "title": row["title"],
                "department": "", "job_type": "",
                "snippet": "",
                "location_raw": row["location_raw"],
                "city": city,
                "country": country,
                "apply_url": row["apply_url"],
                "updated_at": "",
                "is_new_today": False,
            })
        if not jobs:
            # No scraped data yet — placeholder card until agent_scrape.py is run
            jobs.append({
                "company": company_name,
                "source": source,
                "token": token,
                "id": token,
                "title": "",
                "department": "",
                "job_type": "",
                "snippet": "",
                "location_raw": "Netherlands",
                "city": None,
                "country": "Netherlands",
                "apply_url": token,
                "updated_at": "",
                "is_new_today": False,
                "_placeholder": True,
            })

    return jobs


# ----------------------------
# Aggregate jobs + filters
# ----------------------------
def aggregate_jobs(company=None, q=None, country=None, city=None, english_only=False, new_today_only=False, limit=0):
    companies = load_companies()
    if company:
        companies = [c for c in companies if c["name"].lower() == company.lower()]

    now = datetime.now(timezone.utc)
    all_jobs = []
    for c in companies:
        key = (c["source"], c["token"])
        cached = JOB_CACHE.get(key)
        if cached and now - cached["time"] < JOB_CACHE_TTL:
            all_jobs.extend(cached["jobs"])
            continue
        try:
            jobs = normalize_jobs(c["name"], c["source"], c["token"])
            JOB_CACHE[key] = {"jobs": jobs, "time": now}
            all_jobs.extend(jobs)
        except Exception as e:
            logger.warning("Failed to fetch jobs for %s (%s): %s", c["name"], c["source"], e)
            continue

    if q:
        ql = q.lower()
        all_jobs = [j for j in all_jobs if ql in (j.get("title") or "").lower()]

    if new_today_only:
        all_jobs = [j for j in all_jobs if j.get("is_new_today")]

    if country:
        all_jobs = [j for j in all_jobs if soft_country_match(j, country)]

    if city:
        all_jobs = [j for j in all_jobs if (j.get("city") or "").lower() == city.lower()]

    # English-only: strict for Greenhouse, allow others (MVP)
    if english_only:
        filtered = []
        for j in all_jobs:
            if j["source"] != "greenhouse":
                filtered.append(j)
                continue

            try:
                key = ("greenhouse", j["token"], int(j["id"]))
                now = datetime.now(timezone.utc)
                cached = DETAIL_CACHE.get(key)
                if cached and now - cached["time"] < timedelta(hours=6):
                    text = cached["text"]
                else:
                    detail = gh_job_detail(j["token"], int(j["id"]))
                    text = html_to_text(detail.get("content", ""))
                    DETAIL_CACHE[key] = {"text": text, "time": now}

                if is_english(text):
                    filtered.append(j)
            except Exception:
                continue
        all_jobs = filtered

    all_jobs.sort(key=lambda x: (x.get("company", ""), x.get("title", "")))
    return all_jobs[:limit] if limit else all_jobs


# ----------------------------
# Endpoints
# ----------------------------
@app.get("/")
def home():
    return {"status": "Hire Assist is running", "ui": "/ui", "health": "/health"}


@app.get("/version")
def version():
    return {
        "app": "HireAssist Alpha",
        "git_sha": os.environ.get("GIT_SHA"),
        "build_time": os.environ.get("BUILD_TIME"),
    }


@app.get("/meta/freshness")
def meta_freshness():
    result = {}
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM companies")
        result["companies_count"] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM scraped_jobs")
        result["scraped_jobs_count"] = cur.fetchone()[0]
        try:
            cur.execute("SELECT count(*) FROM jobs")
            result["jobs_intel_count"] = cur.fetchone()[0]
        except sqlite3.OperationalError:
            result["jobs_intel_count"] = 0
        try:
            cur.execute("SELECT count(*) FROM company_daily_stats")
            result["stats_rows_count"] = cur.fetchone()[0]
        except sqlite3.OperationalError:
            result["stats_rows_count"] = 0
    return result


# ---------------------------------------------------------------------------
# Admin: bundle import (laptop -> Render sync)
# ---------------------------------------------------------------------------
_ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
_last_import: dict | None = None  # in-memory; reset on redeploy


def _check_admin(request: Request):
    if not _ADMIN_TOKEN:
        return {"error": "ADMIN_TOKEN not configured"}, 503
    token = request.headers.get("X-Admin-Token", "")
    if token != _ADMIN_TOKEN:
        return {"error": "unauthorized"}, 401
    return None, 0


@app.post("/admin/import-bundle")
async def import_bundle(request: Request):
    global _last_import
    err, code = _check_admin(request)
    if err:
        from fastapi.responses import JSONResponse
        return JSONResponse(err, status_code=code)

    body = await request.json()
    data = body.get("data", {})
    result = _import_bundle_data(data)
    _last_import = {
        "imported_at": datetime.now(timezone.utc).isoformat(),
        **result,
    }
    return {**_last_import}


@app.get("/admin/import-status")
def import_status(request: Request):
    err, code = _check_admin(request)
    if err:
        from fastapi.responses import JSONResponse
        return JSONResponse(err, status_code=code)
    if not _last_import:
        return {"last_import": None}
    return {"last_import": _last_import}


@app.get("/jobs")
def jobs(
    company: str | None = Query(default=None),
    q: str | None = Query(default=None),
    country: str | None = Query(default=None),
    city: str | None = Query(default=None),
    english_only: bool = Query(default=False),
    new_today_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=100, ge=1, le=500),
):
    all_jobs = [j for j in aggregate_jobs(company, q, country, city, english_only, new_today_only) if not j.get("_placeholder")]
    total = len(all_jobs)
    start = (page - 1) * per_page
    page_jobs = all_jobs[start:start + per_page]
    return {"count": total, "page": page, "per_page": per_page, "pages": (total + per_page - 1) // per_page if total else 0, "jobs": page_jobs}


@app.get("/health")
def health(full: bool = Query(default=False)):
    report = []
    total = 0
    now = datetime.now(timezone.utc)
    for c in load_companies():
        key = (c["source"], c["token"])
        cached = JOB_CACHE.get(key)
        cache_age_sec = int((now - cached["time"]).total_seconds()) if cached else None
        try:
            if cached and now - cached["time"] < JOB_CACHE_TTL:
                jobs = cached["jobs"]
            else:
                jobs = normalize_jobs(c["name"], c["source"], c["token"])
                JOB_CACHE[key] = {"jobs": jobs, "time": now}
                cache_age_sec = 0
            count = sum(1 for j in jobs if not j.get("_placeholder"))
            total += count
            report.append({"company": c["name"], "source": c["source"], "jobs": count, "status": "ok", "cache_age_sec": cache_age_sec})
        except Exception as e:
            report.append({"company": c["name"], "source": c["source"], "jobs": 0, "status": "error", "error": str(e), "cache_age_sec": cache_age_sec})
    result = {"total_jobs": total, "cache_ttl_sec": int(JOB_CACHE_TTL.total_seconds()), "companies": report}
    if full:
        with sqlite3.connect(DB_FILE) as conn:
            result["scraped_jobs_total"] = conn.execute("SELECT COUNT(*) FROM scraped_jobs").fetchone()[0]
    return result


# ---------------------------------------------------------------------------
# Intelligence / stats endpoints (require job_intel.py)
# ---------------------------------------------------------------------------

@app.get("/stats/companies")
def stats_companies(date: str | None = Query(default=None)):
    """Per-company stats for a given date, sorted by momentum descending."""
    if not _HAS_INTEL:
        return {"error": "Intelligence layer not available"}
    with sqlite3.connect(DB_FILE) as conn:
        job_intel.ensure_intel_tables(conn)
        data = job_intel.get_company_stats(conn, stat_date=date)
    return {"date": date or str(job_intel.date.today()), "companies": data}


@app.get("/stats/company/{company_name}")
def stats_company(company_name: str, days: int = Query(default=30, ge=1, le=365)):
    """Daily stats history for a single company."""
    if not _HAS_INTEL:
        return {"error": "Intelligence layer not available"}
    with sqlite3.connect(DB_FILE) as conn:
        job_intel.ensure_intel_tables(conn)
        history = job_intel.get_company_history(conn, company_name, days=days)
        today_stats = job_intel.get_company_stats(conn)
        company_today = next((c for c in today_stats if c["company_name"] == company_name), None)
    return {"company_name": company_name, "days": days, "current": company_today, "history": history}


@app.get("/stats/summary")
def stats_summary(days: int = Query(default=7, ge=1, le=90)):
    """Aggregate summary across all companies."""
    if not _HAS_INTEL:
        return {"error": "Intelligence layer not available"}
    with sqlite3.connect(DB_FILE) as conn:
        job_intel.ensure_intel_tables(conn)
        data = job_intel.get_summary_stats(conn, days=days)
    return data


@app.get("/ui/momentum", response_class=HTMLResponse)
def ui_momentum():
    """Company momentum page -- top 20 companies by hiring activity."""
    if not _HAS_INTEL:
        return HTMLResponse("<h1>Intelligence layer not configured</h1>")

    with sqlite3.connect(DB_FILE) as conn:
        job_intel.ensure_intel_tables(conn)
        companies = job_intel.get_company_stats(conn)

    top20 = companies[:20]

    rows_html = ""
    for i, c in enumerate(top20, 1):
        nc = c["net_change"]
        nc_cls = "positive" if nc > 0 else ("negative" if nc < 0 else "")
        nc_str = f"+{nc}" if nc > 0 else str(nc)
        rows_html += (
            f"<tr>"
            f"<td>{i}</td>"
            f"<td><a href=\"/stats/company/{escape(c['company_name'])}\">{escape(c['company_name'])}</a></td>"
            f"<td>{c['active_jobs']}</td>"
            f"<td>{c['new_jobs']}</td>"
            f"<td class=\"{nc_cls}\">{nc_str}</td>"
            f"<td><strong>{c['momentum']}</strong></td>"
            f"</tr>"
        )

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
  <meta charset="utf-8"/><title>Company Momentum - Hire Assist</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f3f2ef; color: #1d2226; margin: 0; }}
    .header {{ background: #fff; border-bottom: 1px solid #e0e0e0; padding: 0 24px; height: 52px; display: flex; align-items: center; gap: 16px; position: sticky; top: 0; z-index: 100; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
    .header-logo {{ font-size: 20px; font-weight: 700; color: #0A66C2; }}
    .header a {{ font-size: 14px; color: #0A66C2; text-decoration: none; }}
    .header a:hover {{ text-decoration: underline; }}
    .container {{ max-width: 900px; margin: 24px auto; padding: 0 16px; }}
    h1 {{ font-size: 22px; margin-bottom: 16px; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
    th {{ background: #f8f8f8; text-align: left; padding: 10px 14px; font-size: 12px; font-weight: 600; color: #666; text-transform: uppercase; border-bottom: 2px solid #e0e0e0; }}
    td {{ padding: 10px 14px; border-bottom: 1px solid #f0f0f0; font-size: 14px; }}
    td a {{ color: #0A66C2; text-decoration: none; }}
    td a:hover {{ text-decoration: underline; }}
    .positive {{ color: #057642; font-weight: 600; }}
    .negative {{ color: #c00; font-weight: 600; }}
    .empty {{ text-align: center; padding: 40px; color: #666; }}
  </style>
</head><body>
  <div class="header">
    <span class="header-logo">Hire Assist</span>
    <a href="/ui">Jobs</a>
    <a href="/ui/momentum"><strong>Momentum</strong></a>
  </div>
  <div style="background:#fff3cd;color:#856404;text-align:center;padding:6px 12px;font-size:13px;border-bottom:1px solid #ffc107">Alpha -- coverage may be incomplete. Please share feedback.</div>
  <div class="container">
    <h1>Company Momentum (Top 20)</h1>
    {f'''<table>
      <thead><tr><th>#</th><th>Company</th><th>Active Jobs</th><th>New Today</th><th>Net Change</th><th>Momentum</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table>''' if top20 else '<div class="empty">No stats yet. Run <code>python sync_ats_jobs.py</code> then <code>python daily_intelligence.py --stats-only</code> to populate.</div>'}
  </div>
</body></html>"""
    return HTMLResponse(html)


@app.get("/ui", response_class=HTMLResponse)
def ui(
    company: str | None = Query(default=None),
    q: str | None = Query(default=None),
    country: str = Query(default="Netherlands"),
    city: str | None = Query(default=None),
    english_only: bool = Query(default=False),
    new_today_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
):
    PER_PAGE = 100
    all_companies = load_companies()
    all_jobs = aggregate_jobs(company, q, None, None, english_only, new_today_only)
    countries = unique([j.get("country") for j in all_jobs if j.get("country")])
    country_jobs = [j for j in all_jobs if soft_country_match(j, country)] if country else all_jobs
    cities = unique([j.get("city") for j in country_jobs if j.get("city")])
    all_visible = [j for j in country_jobs
                   if not j.get("_placeholder")
                   and (not city or (j.get("city") or "").lower() == city.lower())]
    total_real = sum(1 for j in all_visible if not j.get("_placeholder"))
    total_visible = len(all_visible)
    total_pages = (total_visible + PER_PAGE - 1) // PER_PAGE if total_visible else 1
    page = min(page, total_pages)
    start = (page - 1) * PER_PAGE
    visible_jobs = all_visible[start:start + PER_PAGE]

    SOURCE_NAMES = {
        "greenhouse": "Greenhouse", "lever": "Lever",
        "smartrecruiters": "SmartRecruiters", "recruitee": "Recruitee",
        "careers_page": "Career Page",
    }

    def opt(val, cur, label=None):
        sel = "selected" if val == cur else ""
        return f'<option value="{escape(val)}" {sel}>{escape(label or val)}</option>'

    company_options = "".join(
        f'<option value="{escape(c["name"])}" {"selected" if c["name"] == company else ""}>{escape(c["name"])}</option>'
        for c in all_companies
    )
    country_options = "".join(opt(c, country) for c in countries if c != "Netherlands")
    city_options = "".join(opt(c, city) for c in cities)

    def render_card(j):
        apply_url = escape(j.get("apply_url") or "")

        # Placeholder card: careers_page company with 0 scraped jobs
        if j.get("_placeholder"):
            return (
                f'<div class="job-card card-browse">'
                f'<div class="card-header">'
                f'<span class="card-company-name">{escape(j["company"])}</span>'
                f'</div>'
                f'<div class="card-location" style="margin-top:4px;color:#999">0 jobs (not scraped yet)</div>'
                f'<div class="card-footer">'
                f'<a class="apply-btn" href="{apply_url}" target="_blank">Browse careers page</a>'
                f'<span class="via-label">careers page</span>'
                f'</div>'
                f'</div>'
            )

        tags = []
        if j.get("department"):
            tags.append(f'<span class="tag">{escape(j["department"])}</span>')
        if j.get("job_type"):
            tags.append(f'<span class="tag tag-type">{escape(j["job_type"])}</span>')
        tags_html = f'<div class="card-tags">{"".join(tags)}</div>' if tags else ""
        snippet_html = f'<p class="card-snippet">{escape(j.get("snippet") or "")}</p>' if j.get("snippet") else ""
        badge = "<span class='badge-new'>New today</span>" if j.get("is_new_today") else ""
        via = SOURCE_NAMES.get(j.get("source", ""), "")
        return (
            f'<div class="job-card">'
            f'<div class="card-header">'
            f'<a class="card-title" href="{apply_url}" target="_blank">{escape(j["title"])}</a>'
            f'{badge}'
            f'</div>'
            f'<div class="card-meta">{escape(j["company"])}</div>'
            f'<div class="card-location">{escape(j.get("location_raw") or "")}</div>'
            f'{tags_html}'
            f'{snippet_html}'
            f'<div class="card-footer">'
            f'<a class="apply-btn" href="{apply_url}" target="_blank">Apply</a>'
            f'<span class="via-label">via {escape(via)}</span>'
            f'</div>'
            f'</div>'
        )

    cards_html = "".join(render_card(j) for j in visible_jobs)

    # Build pagination controls
    def page_url(p):
        params = []
        if company: params.append(f"company={escape(company)}")
        if q: params.append(f"q={escape(q)}")
        if country: params.append(f"country={escape(country)}")
        if city: params.append(f"city={escape(city)}")
        if english_only: params.append("english_only=true")
        if new_today_only: params.append("new_today_only=true")
        params.append(f"page={p}")
        return "/ui?" + "&amp;".join(params)

    pagination_html = ""
    if total_pages > 1:
        parts = []
        if page > 1:
            parts.append(f'<a class="page-btn" href="{page_url(page - 1)}">&laquo; Prev</a>')
        for p in range(1, total_pages + 1):
            if p == page:
                parts.append(f'<span class="page-btn page-current">{p}</span>')
            elif abs(p - page) <= 2 or p == 1 or p == total_pages:
                parts.append(f'<a class="page-btn" href="{page_url(p)}">{p}</a>')
            elif abs(p - page) == 3:
                parts.append('<span class="page-dots">...</span>')
        if page < total_pages:
            parts.append(f'<a class="page-btn" href="{page_url(page + 1)}">Next &raquo;</a>')
        pagination_html = f'<div class="pagination">{"".join(parts)}</div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Hire Assist – Netherlands Tech Jobs</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f3f2ef; color: #1d2226; font-size: 14px; }}
    a {{ text-decoration: none; }}

    .header {{ background: #fff; border-bottom: 1px solid #e0e0e0; padding: 0 24px; height: 52px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 100; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
    .header-logo {{ font-size: 20px; font-weight: 700; color: #0A66C2; letter-spacing: -0.3px; }}
    .header-sub {{ font-size: 13px; color: #666; }}

    .container {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px; display: grid; grid-template-columns: 260px 1fr; gap: 20px; align-items: start; }}

    .sidebar {{ position: sticky; top: 64px; }}
    .filter-card {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px; }}
    .filter-card h3 {{ font-size: 15px; font-weight: 600; margin-bottom: 16px; }}
    .filter-group {{ margin-bottom: 14px; }}
    .filter-label {{ font-size: 11px; font-weight: 600; color: #777; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 5px; display: block; }}
    input[type="text"], select {{ width: 100%; padding: 8px 10px; border: 1px solid #c9c9c9; border-radius: 4px; font-size: 14px; color: #1d2226; background: #fff; }}
    input[type="text"]:focus, select:focus {{ outline: none; border-color: #0A66C2; box-shadow: 0 0 0 2px rgba(10,102,194,.15); }}
    .chk-label {{ display: flex; align-items: center; gap: 8px; font-size: 14px; cursor: pointer; margin-bottom: 10px; }}
    .chk-label:last-child {{ margin-bottom: 0; }}
    .search-btn {{ width: 100%; padding: 9px; background: #0A66C2; color: #fff; border: none; border-radius: 20px; font-size: 14px; font-weight: 600; cursor: pointer; margin-top: 6px; }}
    .search-btn:hover {{ background: #004182; }}

    .main {{ display: flex; flex-direction: column; gap: 8px; }}
    .results-meta {{ font-size: 13px; color: #666; padding-bottom: 4px; }}
    .results-meta strong {{ color: #1d2226; }}

    .job-card {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px 20px; transition: box-shadow .15s; }}
    .job-card:hover {{ box-shadow: 0 0 0 1px rgba(0,0,0,.1), 0 4px 12px rgba(0,0,0,.08); }}
    .card-header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }}
    .card-title {{ font-size: 16px; font-weight: 600; color: #0A66C2; line-height: 1.3; }}
    .card-title:hover {{ text-decoration: underline; }}
    .badge-new {{ flex-shrink: 0; font-size: 11px; font-weight: 600; background: #057642; color: #fff; padding: 2px 8px; border-radius: 12px; margin-top: 2px; }}
    .card-meta {{ font-size: 14px; color: #434649; margin-top: 4px; font-weight: 500; }}
    .card-location {{ font-size: 13px; color: #666; margin-top: 2px; }}
    .card-tags {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }}
    .tag {{ font-size: 12px; background: #f3f2ef; color: #434649; border: 1px solid #e0e0e0; border-radius: 4px; padding: 2px 8px; }}
    .tag-type {{ background: #edf3fb; color: #0A66C2; border-color: #c3d9f5; }}
    .card-snippet {{ font-size: 13px; color: #555; margin-top: 10px; line-height: 1.55; }}
    .card-browse {{ background: #f9f9f9; border-style: dashed; }}
    .card-company-name {{ font-size: 16px; font-weight: 600; color: #1d2226; }}
    .card-footer {{ display: flex; align-items: center; justify-content: space-between; margin-top: 14px; padding-top: 12px; border-top: 1px solid #f3f2ef; }}
    .apply-btn {{ font-size: 14px; font-weight: 600; color: #0A66C2; border: 1.5px solid #0A66C2; border-radius: 20px; padding: 5px 18px; }}
    .apply-btn:hover {{ background: rgba(10,102,194,.08); }}
    .via-label {{ font-size: 12px; color: #999; }}

    .pagination {{ display: flex; justify-content: center; align-items: center; gap: 6px; margin-top: 20px; padding: 16px 0; }}
    .page-btn {{ display: inline-flex; align-items: center; justify-content: center; min-width: 36px; height: 36px; padding: 0 10px; border: 1px solid #e0e0e0; border-radius: 8px; font-size: 14px; font-weight: 500; color: #0A66C2; background: #fff; cursor: pointer; }}
    .page-btn:hover {{ background: rgba(10,102,194,.08); border-color: #0A66C2; }}
    .page-current {{ background: #0A66C2; color: #fff; border-color: #0A66C2; cursor: default; }}
    .page-current:hover {{ background: #0A66C2; }}
    .page-dots {{ color: #999; padding: 0 4px; }}

    @media(max-width: 800px) {{
      .container {{ grid-template-columns: 1fr; }}
      .sidebar {{ position: static; }}
    }}
  </style>
</head>
<body>
  <div class="header">
    <span class="header-logo">Hire Assist</span>
    <span class="header-sub">Netherlands tech jobs</span>
    <a href="/ui/momentum" style="font-size:13px;color:#0A66C2;text-decoration:none;margin-left:auto">Company Momentum</a>
  </div>
  <div style="background:#fff3cd;color:#856404;text-align:center;padding:6px 12px;font-size:13px;border-bottom:1px solid #ffc107">Alpha -- coverage may be incomplete. Please share feedback.</div>
  <div class="container">
    <aside class="sidebar">
      <form method="get">
        <div class="filter-card">
          <h3>Filters</h3>
          <div class="filter-group">
            <label class="filter-label">Search</label>
            <input type="text" name="q" placeholder="Job title or keyword" value="{escape(q or '')}"/>
          </div>
          <div class="filter-group">
            <label class="filter-label">Company</label>
            <select name="company">
              <option value="">All companies</option>
              {company_options}
            </select>
          </div>
          <div class="filter-group">
            <label class="filter-label">Country</label>
            <select name="country">
              <option value="Netherlands" {"selected" if country == "Netherlands" else ""}>Netherlands</option>
              <option value="" {"selected" if not country else ""}>All countries</option>
              {country_options}
            </select>
          </div>
          <div class="filter-group">
            <label class="filter-label">City</label>
            <select name="city">
              <option value="">All cities</option>
              {city_options}
            </select>
          </div>
          <div class="filter-group">
            <label class="chk-label"><input type="checkbox" name="english_only" value="true" {"checked" if english_only else ""}/> English only</label>
            <label class="chk-label"><input type="checkbox" name="new_today_only" value="true" {"checked" if new_today_only else ""}/> New today only</label>
          </div>
          <button type="submit" class="search-btn">Search</button>
        </div>
      </form>
    </aside>
    <main class="main">
      <div class="results-meta">Showing <strong>{total_real}</strong> jobs from {len(all_companies)} companies (page {page}/{total_pages})</div>
      {cards_html}
      {pagination_html}
    </main>
  </div>
</body>
</html>"""
    return HTMLResponse(html)
