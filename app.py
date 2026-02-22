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
import threading
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

# Background bundle import readiness flag (set immediately on non-Render envs)
_bundle_ready = threading.Event()

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
        conn.executemany(
            """INSERT INTO companies (name, source, token, active, confidence, discovered_at, last_verified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, token) DO UPDATE SET
                 name=excluded.name, active=excluded.active,
                 confidence=excluded.confidence, last_verified_at=excluded.last_verified_at""",
            [(r["name"], r["source"], r["token"],
              r.get("active", 1), r.get("confidence", "manual"),
              r.get("discovered_at"), r.get("last_verified_at")) for r in rows],
        )
        summary["companies"] = len(rows)

        # -- scraped_jobs: replace per company_name --
        rows = data.get("scraped_jobs", [])
        replaced_companies = {r["company_name"] for r in rows}
        for cn in replaced_companies:
            conn.execute("DELETE FROM scraped_jobs WHERE company_name = ?", (cn,))
        conn.executemany(
            """INSERT INTO scraped_jobs (company_name, career_url, title, location_raw, apply_url, scraped_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(r["company_name"], r["career_url"], r["title"],
              r.get("location_raw", ""), r["apply_url"], r["scraped_at"]) for r in rows],
        )
        summary["scraped_jobs"] = len(rows)
        summary["scraped_companies_replaced"] = len(replaced_companies)

        # -- jobs: upsert by UNIQUE(source, job_key) --
        rows = data.get("jobs", [])
        if rows:
            if _HAS_INTEL:
                job_intel.ensure_intel_tables(conn)
            conn.executemany(
                """INSERT INTO jobs (source, company_name, job_key, title, location_raw,
                      country, city, url, posted_at, first_seen_at, last_seen_at, is_active, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source, job_key) DO UPDATE SET
                     company_name=excluded.company_name, title=excluded.title,
                     location_raw=excluded.location_raw, country=excluded.country,
                     city=excluded.city, url=excluded.url, posted_at=excluded.posted_at,
                     last_seen_at=excluded.last_seen_at, is_active=excluded.is_active,
                     raw_json=excluded.raw_json""",
                [(r["source"], r["company_name"], r["job_key"], r["title"],
                  r.get("location_raw", ""), r.get("country"), r.get("city"),
                  r.get("url", ""), r.get("posted_at"),
                  r["first_seen_at"], r["last_seen_at"],
                  r.get("is_active", 1), r.get("raw_json")) for r in rows],
            )
        summary["jobs"] = len(rows)

        # -- company_daily_stats: upsert by UNIQUE(stat_date, company_name, source) --
        rows = data.get("company_daily_stats", [])
        if rows:
            if _HAS_INTEL:
                job_intel.ensure_intel_tables(conn)
            conn.executemany(
                """INSERT INTO company_daily_stats
                      (stat_date, company_name, source, active_jobs, new_jobs, closed_jobs, net_change)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(stat_date, company_name, source) DO UPDATE SET
                     active_jobs=excluded.active_jobs, new_jobs=excluded.new_jobs,
                     closed_jobs=excluded.closed_jobs, net_change=excluded.net_change""",
                [(r["stat_date"], r["company_name"], r["source"],
                  r.get("active_jobs", 0), r.get("new_jobs", 0),
                  r.get("closed_jobs", 0), r.get("net_change", 0)) for r in rows],
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

    # Auto-import bundle on Render (ephemeral filesystem = always fresh DB)
    # Render sets RENDER=true automatically; locally we never auto-import
    # Runs in background thread so the app can start accepting requests immediately
    is_render = os.environ.get("RENDER", "").lower() in ("true", "1")
    if is_render and BUNDLE_SEED.exists():
        def _bg_import():
            try:
                bundle = json.loads(BUNDLE_SEED.read_text(encoding="utf-8"))
                result = _import_bundle_data(bundle.get("data", {}))
                _bundle_ready.set()
                logger.info("Render startup: imported bundle -- %s", result["summary"])
            except Exception as e:
                _bundle_ready.set()
                logger.warning("Render startup: failed to import bundle -- %s", e)
        threading.Thread(target=_bg_import, daemon=True).start()
    else:
        _bundle_ready.set()


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


def _normalize_city(city: str) -> str | None:
    """Clean up city name: strip parentheticals, qualifiers, and title-case."""
    if not city:
        return None
    # Remove parenthetical suffixes: (on-site), (hybrid), (p), (NL), etc.
    city = re.sub(r"\s*\(.*?\)", "", city).strip()
    # Remove trailing qualifiers
    city = re.sub(r"\s+(?:HQ|Office|Campus|Area|Region|Center|Centre)$", "", city, flags=re.IGNORECASE).strip()
    if not city:
        return None
    # Match against known NL cities for consistent casing
    cl = city.lower()
    for known in _NL_CITIES:
        if cl == known:
            return city.title()
    return city


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

    city = _normalize_city(city)

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


@app.get("/ping")
def ping():
    """Lightweight keep-alive endpoint for uptime monitors (e.g. UptimeRobot).
    Returns instantly -- no DB or ATS calls."""
    return {"status": "ok", "ready": _bundle_ready.is_set()}


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


# ----------------------------
# UI helpers
# ----------------------------
def time_ago(dt_str: str) -> str:
    """Convert ISO timestamp to human-readable 'X ago' string."""
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = datetime.now(timezone.utc) - dt
        secs = int(diff.total_seconds())
        if secs < 0:
            return ""
        if secs < 3600:
            m = max(1, secs // 60)
            return f"{m} min ago"
        if secs < 86400:
            h = secs // 3600
            return f"{h} hour{'s' if h != 1 else ''} ago"
        days = secs // 86400
        if days == 1:
            return "1 day ago"
        if days < 30:
            return f"{days} days ago"
        return f"{days // 30} month{'s' if days // 30 != 1 else ''} ago"
    except Exception:
        return ""


def company_initials(name: str) -> str:
    """Get 2-letter initials from company name for logo placeholder."""
    if not name:
        return "?"
    words = name.split()
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return name[:2].upper()


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

    table_html = (
        f'<table>'
        f'<thead><tr><th>#</th><th>Company</th><th>Active Jobs</th><th>New Today</th><th>Net Change</th><th>Momentum</th></tr></thead>'
        f'<tbody>{rows_html}</tbody>'
        f'</table>'
    ) if top20 else '<div class="empty">No stats yet. Run <code>python sync_ats_jobs.py</code> then <code>python daily_intelligence.py --stats-only</code> to populate.</div>'

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
  <meta charset="utf-8"/><title>Company Momentum - HireAssist</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Inter', sans-serif; background: #f9fafb; color: #111827; font-size: 14px; line-height: 1.5; }}
    a {{ text-decoration: none; }}
    .topbar {{ background: #fff; border-bottom: 1px solid #e5e7eb; padding: 0 32px; display: flex; align-items: center; justify-content: space-between; height: 52px; position: sticky; top: 0; z-index: 100; }}
    .logo {{ font-size: 18px; font-weight: 700; color: #1a56db; letter-spacing: -0.4px; }}
    .logo-tag {{ font-size: 12px; color: #9ca3af; font-weight: 400; margin-left: 6px; }}
    .nav-right {{ display: flex; align-items: center; gap: 24px; }}
    .nav-right a {{ color: #4b5563; font-size: 13px; font-weight: 500; transition: color 0.15s; }}
    .nav-right a:hover {{ color: #1a56db; }}
    .nav-right a.active {{ color: #1a56db; font-weight: 600; }}
    .alpha-bar {{ background: #fefce8; border-bottom: 1px solid #fde68a; text-align: center; padding: 9px 32px; font-size: 13px; color: #92400e; }}
    .container {{ max-width: 900px; margin: 24px auto; padding: 0 32px; }}
    h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 16px; letter-spacing: -0.3px; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
    th {{ background: #f9fafb; text-align: left; padding: 10px 14px; font-size: 11px; font-weight: 600; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.8px; border-bottom: 1px solid #e5e7eb; }}
    td {{ padding: 10px 14px; border-bottom: 1px solid #f3f4f6; font-size: 14px; }}
    td a {{ color: #1a56db; }}
    td a:hover {{ text-decoration: underline; }}
    .positive {{ color: #0e9f6e; font-weight: 600; }}
    .negative {{ color: #c00; font-weight: 600; }}
    .empty {{ text-align: center; padding: 40px; color: #9ca3af; }}
  </style>
</head><body>
  <div class="topbar">
    <div style="display:flex;align-items:baseline;gap:6px">
      <span class="logo">HireAssist</span>
      <span class="logo-tag">Netherlands tech jobs</span>
    </div>
    <div class="nav-right">
      <a href="/ui">Jobs</a>
      <a href="#" style="color:#9ca3af;cursor:default">Companies</a>
      <a href="/ui/momentum" class="active">Company Momentum</a>
    </div>
  </div>
  <div class="alpha-bar">Alpha -- coverage may be incomplete. Please share feedback.</div>
  <div class="container">
    <h1>Company Momentum (Top 20)</h1>
    {table_html}
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

    # Extra stats for the hero strip
    new_today_count = sum(1 for j in all_visible if j.get("is_new_today"))
    distinct_cities = len(cities)

    # Momentum top 5 for sidebar widget
    momentum_html = ""
    if _HAS_INTEL:
        try:
            with sqlite3.connect(DB_FILE) as conn:
                job_intel.ensure_intel_tables(conn)
                m_companies = job_intel.get_company_stats(conn)
            top5 = m_companies[:5]
            if top5:
                max_m = top5[0]["momentum"] if top5[0]["momentum"] > 0 else 1
                m_items = ""
                for i, mc in enumerate(top5, 1):
                    bar_w = int(mc["momentum"] / max_m * 100)
                    delta = f"+{mc['new_jobs']}" if mc["new_jobs"] > 0 else str(mc["new_jobs"])
                    m_items += (
                        f'<div class="m-item">'
                        f'<div class="m-rank">{i}</div>'
                        f'<div class="m-name">{escape(mc["company_name"])}</div>'
                        f'<div class="m-bar-wrap"><div class="m-bar" style="width:{bar_w}%"></div></div>'
                        f'<div class="m-delta">{delta}</div>'
                        f'</div>'
                    )
                momentum_html = (
                    '<div class="momentum-box">'
                    '<div class="momentum-header">'
                    '<div class="momentum-title-text">Company Momentum <span class="momentum-badge">LIVE</span></div>'
                    '</div>'
                    '<div class="momentum-sub-text">Most actively hiring this week</div>'
                    f'{m_items}'
                    '<div style="margin-top:12px;text-align:center">'
                    '<a href="/ui/momentum" style="font-size:12px;color:#1a56db;font-weight:500;text-decoration:none">View full leaderboard &#8594;</a>'
                    '</div></div>'
                )
        except Exception:
            pass

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
            return ""

        initials = company_initials(j.get("company", ""))
        is_new = j.get("is_new_today", False)
        new_class = ' is-new' if is_new else ''
        ago = time_ago(j.get("updated_at", ""))
        ago_html = f'<div class="job-date-label">{escape(ago)}</div>' if ago else ''

        # Meta row
        loc = j.get("location_raw") or ""
        jtype = j.get("job_type") or ""
        meta_parts = ""
        if loc:
            meta_parts += (
                f'<div class="meta-pill">'
                f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/></svg>'
                f'{escape(loc)}'
                f'</div>'
            )
        if jtype:
            meta_parts += (
                f'<div class="meta-pill">'
                f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="20" height="14" x="2" y="7" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg>'
                f'{escape(jtype)}'
                f'</div>'
            )

        snippet = j.get("snippet") or ""
        snippet_html = f'<div class="job-snippet-text">{escape(snippet)}</div>' if snippet else ''

        # Footer tags
        tags = ""
        if jtype:
            tags += f'<span class="pill pill-type">{escape(jtype)}</span>'
        if is_new:
            tags += '<span class="pill pill-new">New today</span>'
        via = SOURCE_NAMES.get(j.get("source", ""), "")
        if via:
            tags += f'<span class="pill pill-source">via {escape(via)}</span>'

        return (
            f'<div class="job-card{new_class}">'
            f'{ago_html}'
            f'<div class="company-row">'
            f'<div class="co-logo">{escape(initials)}</div>'
            f'<span class="co-name">{escape(j.get("company", ""))}</span>'
            f'</div>'
            f'<a class="job-title-text" href="{apply_url}" target="_blank">{escape(j["title"])}</a>'
            f'<div class="job-meta-row">{meta_parts}</div>'
            f'{snippet_html}'
            f'<div class="job-footer-row">'
            f'<div class="tags-left">{tags}</div>'
            f'<div class="actions-right">'
            f'<button class="btn-save" onclick="return false">Save</button>'
            f'<a class="btn-apply" href="{apply_url}" target="_blank">Apply &#8594;</a>'
            f'</div>'
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
            parts.append(f'<a class="pg-btn" href="{page_url(page - 1)}">&#8592;</a>')
        for p in range(1, total_pages + 1):
            if p == page:
                parts.append(f'<span class="pg-btn active">{p}</span>')
            elif abs(p - page) <= 2 or p == 1 or p == total_pages:
                parts.append(f'<a class="pg-btn" href="{page_url(p)}">{p}</a>')
            elif abs(p - page) == 3:
                parts.append('<span class="pg-dots">...</span>')
        if page < total_pages:
            parts.append(f'<a class="pg-btn" href="{page_url(page + 1)}">&#8594;</a>')
        pagination_html = f'<div class="pagination">{"".join(parts)}</div>'

    # Quick filter pill helpers
    def qf_active(label, is_active):
        cls = "qf-tag active" if is_active else "qf-tag"
        return f'<div class="{cls}">{label}</div>'

    no_filters = not q and not company and not english_only and not new_today_only and not city and country == "Netherlands"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>HireAssist - Netherlands Tech Jobs</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {{
      --blue: #1a56db; --blue-light: #e8f0fe; --blue-mid: #3b7de8;
      --green: #0e9f6e; --green-light: #e8f8f3; --orange: #ff6b35;
      --text: #111827; --text-mid: #4b5563; --text-light: #9ca3af;
      --border: #e5e7eb; --bg: #f9fafb; --white: #ffffff; --tag-bg: #f3f4f6;
      --shadow: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.04);
      --shadow-md: 0 4px 12px rgba(0,0,0,0.08);
    }}
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); font-size: 14px; line-height: 1.5; }}
    a {{ text-decoration: none; color: inherit; }}

    /* TOPBAR */
    .topbar {{ background: var(--white); border-bottom: 1px solid var(--border); padding: 0 32px; display: flex; align-items: center; justify-content: space-between; height: 52px; position: sticky; top: 0; z-index: 100; }}
    .logo-area {{ display: flex; align-items: baseline; gap: 6px; }}
    .logo {{ font-size: 18px; font-weight: 700; color: var(--blue); letter-spacing: -0.4px; }}
    .logo-tag {{ font-size: 12px; color: var(--text-light); font-weight: 400; }}
    .nav-right {{ display: flex; align-items: center; gap: 24px; }}
    .nav-right a {{ color: var(--text-mid); font-size: 13px; font-weight: 500; transition: color 0.15s; }}
    .nav-right a:hover {{ color: var(--blue); }}
    .nav-right a.active {{ color: var(--blue); font-weight: 600; }}
    .btn-post {{ background: var(--blue); color: white !important; padding: 7px 16px; border-radius: 6px; font-size: 13px !important; font-weight: 600 !important; }}
    .btn-post:hover {{ background: #1649c0 !important; }}

    /* ALPHA BAR */
    .alpha-bar {{ background: #fefce8; border-bottom: 1px solid #fde68a; text-align: center; padding: 9px 32px; font-size: 13px; color: #92400e; }}

    /* HERO STRIP */
    .hero-strip {{ background: linear-gradient(135deg, #1a56db 0%, #2563eb 50%, #1d4ed8 100%); padding: 40px 32px 36px; color: white; }}
    .hero-inner {{ max-width: 1100px; margin: 0 auto; }}
    .hero-strip h1 {{ font-size: 26px; font-weight: 700; letter-spacing: -0.5px; margin-bottom: 6px; }}
    .hero-strip p {{ font-size: 14px; opacity: 0.82; margin-bottom: 24px; font-weight: 400; }}

    /* SEARCH BAR */
    .search-bar {{ background: white; border-radius: 10px; display: flex; align-items: center; overflow: hidden; box-shadow: 0 4px 20px rgba(0,0,0,0.15); max-width: 820px; }}
    .search-field {{ display: flex; align-items: center; gap: 8px; padding: 13px 16px; flex: 1; border-right: 1px solid var(--border); }}
    .search-field svg {{ color: #9ca3af; flex-shrink: 0; }}
    .search-field input {{ border: none; outline: none; font-family: 'Inter', sans-serif; font-size: 14px; color: var(--text); width: 100%; }}
    .search-field input::placeholder {{ color: var(--text-light); }}
    .search-select-wrap {{ display: flex; align-items: center; gap: 8px; padding: 13px 16px; border-right: 1px solid var(--border); }}
    .search-select-wrap svg {{ color: #9ca3af; flex-shrink: 0; }}
    .search-select-wrap select {{ border: none; outline: none; font-family: 'Inter', sans-serif; font-size: 14px; color: var(--text); background: none; cursor: pointer; min-width: 130px; }}
    .search-btn {{ background: var(--blue); color: white; border: none; padding: 13px 28px; font-family: 'Inter', sans-serif; font-size: 14px; font-weight: 600; cursor: pointer; white-space: nowrap; transition: background 0.15s; }}
    .search-btn:hover {{ background: #1649c0; }}

    /* STATS ROW */
    .stats-strip {{ background: rgba(255,255,255,0.12); border-radius: 8px; display: inline-flex; gap: 0; margin-top: 20px; overflow: hidden; border: 1px solid rgba(255,255,255,0.2); }}
    .stat-item {{ padding: 10px 20px; border-right: 1px solid rgba(255,255,255,0.15); display: flex; flex-direction: column; gap: 1px; }}
    .stat-item:last-child {{ border-right: none; }}
    .stat-num {{ font-size: 18px; font-weight: 700; color: white; letter-spacing: -0.5px; }}
    .stat-label {{ font-size: 11px; color: rgba(255,255,255,0.65); font-weight: 400; }}

    /* QUICK FILTERS */
    .quick-filters {{ max-width: 1100px; margin: 0 auto; padding: 14px 32px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
    .qf-label {{ font-size: 12px; color: var(--text-light); font-weight: 500; margin-right: 4px; }}
    .qf-tag {{ background: white; border: 1px solid var(--border); color: var(--text-mid); padding: 5px 12px; border-radius: 100px; font-size: 12px; cursor: pointer; transition: all 0.15s; font-weight: 500; }}
    .qf-tag:hover, .qf-tag.active {{ border-color: var(--blue); color: var(--blue); background: var(--blue-light); }}

    /* MAIN LAYOUT */
    .main {{ max-width: 1100px; margin: 0 auto; padding: 0 32px 60px; display: grid; grid-template-columns: 240px 1fr; gap: 24px; align-items: start; }}

    /* SIDEBAR */
    .sidebar {{ display: flex; flex-direction: column; gap: 16px; }}
    .filter-box {{ background: white; border: 1px solid var(--border); border-radius: 10px; padding: 14px; box-shadow: var(--shadow); }}
    .filter-box-title {{ font-size: 11px; font-weight: 600; color: var(--text-light); letter-spacing: 0.8px; text-transform: uppercase; margin-bottom: 10px; }}
    .filter-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 10px; }}
    .filter-grid .full {{ grid-column: 1 / -1; }}
    .filter-select {{ width: 100%; border: 1px solid var(--border); border-radius: 6px; padding: 7px 8px; font-family: 'Inter', sans-serif; font-size: 12px; color: var(--text); outline: none; background: white; }}
    .filter-select:focus {{ border-color: var(--blue); }}
    .chk-row {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
    .chk-label {{ display: flex; align-items: center; gap: 5px; cursor: pointer; font-size: 12px; color: var(--text); white-space: nowrap; }}
    .chk-label input {{ accent-color: var(--blue); width: 14px; height: 14px; }}
    .sidebar-search-btn {{ width: 100%; margin-top: 10px; background: var(--blue); color: white; border: none; padding: 9px; border-radius: 6px; font-family: 'Inter', sans-serif; font-size: 13px; font-weight: 600; cursor: pointer; transition: background 0.15s; }}
    .sidebar-search-btn:hover {{ background: #1649c0; }}

    /* MOMENTUM BOX */
    .momentum-box {{ background: white; border: 1px solid var(--border); border-radius: 10px; padding: 16px; box-shadow: var(--shadow); }}
    .momentum-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; }}
    .momentum-title-text {{ font-size: 13px; font-weight: 600; color: var(--text); display: flex; align-items: center; gap: 6px; }}
    .momentum-badge {{ background: var(--green); color: white; font-size: 9px; font-weight: 700; padding: 1px 5px; border-radius: 3px; letter-spacing: 0.3px; }}
    .momentum-sub-text {{ font-size: 11px; color: var(--text-light); margin-bottom: 14px; }}
    .m-item {{ display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--border); }}
    .m-item:last-child {{ border-bottom: none; }}
    .m-rank {{ font-size: 11px; font-weight: 600; color: var(--text-light); width: 14px; text-align: center; }}
    .m-name {{ flex: 1; font-size: 12px; color: var(--text); font-weight: 500; }}
    .m-bar-wrap {{ width: 50px; height: 3px; background: var(--border); border-radius: 2px; overflow: hidden; }}
    .m-bar {{ height: 100%; background: var(--blue); border-radius: 2px; }}
    .m-delta {{ font-size: 11px; color: var(--green); font-weight: 600; min-width: 28px; text-align: right; }}

    /* JOBS AREA */
    .jobs-area {{ display: flex; flex-direction: column; gap: 12px; }}
    .jobs-header {{ display: flex; align-items: center; justify-content: space-between; padding: 4px 0; }}
    .jobs-count {{ font-size: 13px; color: var(--text-mid); }}
    .jobs-count strong {{ color: var(--text); font-weight: 600; }}
    .sort-wrap {{ display: flex; align-items: center; gap: 8px; }}
    .sort-label {{ font-size: 12px; color: var(--text-light); }}
    .sort-select {{ border: 1px solid var(--border); background: white; color: var(--text); font-family: 'Inter', sans-serif; font-size: 12px; padding: 5px 10px; border-radius: 6px; outline: none; cursor: pointer; }}

    /* ALERT BANNER */
    .alert-banner {{ background: var(--blue-light); border: 1px solid #bfdbfe; border-radius: 10px; padding: 12px 16px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
    .alert-text {{ font-size: 13px; color: #1e40af; display: flex; align-items: center; gap: 8px; }}
    .btn-alert {{ background: var(--blue); color: white; border: none; padding: 7px 16px; border-radius: 6px; font-size: 12px; font-weight: 600; font-family: 'Inter', sans-serif; cursor: pointer; white-space: nowrap; transition: background 0.15s; flex-shrink: 0; }}
    .btn-alert:hover {{ background: #1649c0; }}

    /* JOB CARD */
    .job-card {{ background: white; border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px; box-shadow: var(--shadow); cursor: default; transition: border-color 0.15s, box-shadow 0.15s; position: relative; }}
    .job-card:hover {{ border-color: var(--blue); box-shadow: var(--shadow-md); }}
    .job-card.is-new {{ border-left: 3px solid var(--green); }}
    .job-date-label {{ position: absolute; top: 18px; right: 20px; font-size: 11px; color: var(--text-light); }}
    .company-row {{ display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }}
    .co-logo {{ width: 30px; height: 30px; border-radius: 6px; background: var(--blue-light); border: 1px solid #dbeafe; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 700; color: var(--blue); flex-shrink: 0; }}
    .co-name {{ font-size: 13px; color: var(--text-mid); font-weight: 500; }}
    .job-title-text {{ display: block; font-size: 16px; font-weight: 600; color: var(--blue); letter-spacing: -0.2px; margin-bottom: 8px; line-height: 1.3; }}
    .job-title-text:hover {{ text-decoration: underline; }}
    .job-meta-row {{ display: flex; align-items: center; gap: 16px; margin-bottom: 10px; flex-wrap: wrap; }}
    .meta-pill {{ display: flex; align-items: center; gap: 4px; font-size: 12px; color: var(--text-mid); }}
    .meta-pill svg {{ width: 12px; height: 12px; color: var(--text-light); }}
    .job-snippet-text {{ font-size: 13px; color: var(--text-mid); line-height: 1.6; margin-bottom: 14px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
    .job-footer-row {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; }}
    .tags-left {{ display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }}
    .pill {{ font-size: 11px; padding: 3px 10px; border-radius: 100px; font-weight: 500; border: 1px solid; }}
    .pill-type {{ background: #eff6ff; color: #2563eb; border-color: #bfdbfe; }}
    .pill-new {{ background: var(--green-light); color: var(--green); border-color: #a7f3d0; }}
    .pill-source {{ background: var(--tag-bg); color: var(--text-light); border-color: var(--border); }}
    .actions-right {{ display: flex; align-items: center; gap: 8px; flex-shrink: 0; }}
    .btn-save {{ background: white; border: 1px solid var(--border); color: var(--text-mid); padding: 6px 14px; border-radius: 6px; font-size: 12px; font-family: 'Inter', sans-serif; font-weight: 500; cursor: pointer; transition: all 0.15s; }}
    .btn-save:hover {{ border-color: var(--blue); color: var(--blue); }}
    .btn-apply {{ display: inline-flex; align-items: center; gap: 4px; background: var(--blue); color: white; border: none; padding: 7px 18px; border-radius: 6px; font-size: 12px; font-weight: 600; font-family: 'Inter', sans-serif; cursor: pointer; transition: background 0.15s; }}
    .btn-apply:hover {{ background: #1649c0; }}

    /* PAGINATION */
    .pagination {{ display: flex; align-items: center; justify-content: center; gap: 4px; margin-top: 8px; }}
    .pg-btn {{ min-width: 34px; height: 34px; padding: 0 8px; display: inline-flex; align-items: center; justify-content: center; border-radius: 6px; font-size: 13px; cursor: pointer; background: white; border: 1px solid var(--border); color: var(--text-mid); font-family: 'Inter', sans-serif; font-weight: 500; transition: all 0.15s; }}
    .pg-btn.active {{ background: var(--blue); border-color: var(--blue); color: white; font-weight: 600; cursor: default; }}
    .pg-btn:hover:not(.active) {{ border-color: var(--blue); color: var(--blue); }}
    .pg-dots {{ color: var(--text-light); padding: 0 4px; font-size: 13px; }}

    /* MOBILE HAMBURGER */
    .mobile-menu-btn {{
      display: none;
      background: none;
      border: none;
      cursor: pointer;
      padding: 4px;
      color: var(--text);
    }}
    .mobile-nav {{
      display: none;
      position: fixed;
      top: 52px;
      left: 0; right: 0; bottom: 0;
      background: white;
      z-index: 99;
      padding: 20px;
      flex-direction: column;
      gap: 16px;
      overflow-y: auto;
    }}
    .mobile-nav.open {{ display: flex; }}
    .mobile-nav a {{
      font-size: 16px;
      font-weight: 500;
      color: var(--text);
      padding: 12px 0;
      border-bottom: 1px solid var(--border);
    }}
    .mobile-nav a.active {{ color: var(--blue); }}
    .mobile-nav .btn-post-mobile {{
      background: var(--blue);
      color: white;
      border: none;
      padding: 14px;
      border-radius: 8px;
      font-size: 15px;
      font-weight: 600;
      font-family: 'Inter', sans-serif;
      cursor: pointer;
      margin-top: 8px;
      text-align: center;
    }}

    /* MOBILE FILTER TOGGLE */
    .mobile-filter-toggle {{
      display: none;
      width: 100%;
      background: white;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 11px 16px;
      font-family: 'Inter', sans-serif;
      font-size: 14px;
      font-weight: 500;
      color: var(--text);
      cursor: pointer;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 12px;
    }}

    @media (max-width: 768px) {{
      .topbar {{ padding: 0 16px; }}
      .logo-tag {{ display: none; }}
      .nav-right {{ display: none; }}
      .mobile-menu-btn {{ display: block; }}

      .alpha-bar {{ font-size: 12px; padding: 8px 16px; }}

      .hero-strip {{ padding: 24px 16px 20px; }}
      .hero-strip h1 {{ font-size: 20px; letter-spacing: -0.3px; margin-bottom: 8px; }}
      .hero-strip p {{ font-size: 13px; margin-bottom: 16px; }}

      .search-bar {{
        flex-direction: column;
        border-radius: 10px;
        overflow: visible;
        background: transparent;
        box-shadow: none;
        gap: 8px;
        max-width: 100%;
      }}
      .search-field {{
        background: white;
        border-radius: 8px;
        border: 1px solid var(--border) !important;
        border-right: 1px solid var(--border) !important;
        width: 100%;
      }}
      .search-select-wrap {{
        background: white;
        border-radius: 8px;
        border: 1px solid var(--border) !important;
        border-right: 1px solid var(--border) !important;
        width: 100%;
      }}
      .search-select-wrap select {{ min-width: unset; width: 100%; }}
      .search-btn {{
        border-radius: 8px;
        width: 100%;
        padding: 14px;
        font-size: 15px;
      }}

      .stats-strip {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        width: 100%;
        border-radius: 10px;
        margin-top: 16px;
      }}
      .stat-item {{ padding: 10px 14px; }}
      .stat-num {{ font-size: 16px; }}

      .quick-filters {{
        padding: 10px 16px;
        gap: 6px;
        flex-wrap: nowrap;
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        scrollbar-width: none;
      }}
      .quick-filters::-webkit-scrollbar {{ display: none; }}
      .qf-tag {{ font-size: 11px; padding: 4px 10px; white-space: nowrap; flex-shrink: 0; }}
      .qf-label {{ white-space: nowrap; flex-shrink: 0; }}

      .main {{
        grid-template-columns: 1fr;
        padding: 0 16px 40px;
        gap: 0;
      }}
      .sidebar {{ position: static; gap: 0; }}
      .mobile-filter-toggle {{ display: flex; }}
      .filter-box, .momentum-box {{ display: none; margin-bottom: 12px; }}
      .filter-box.mobile-open, .momentum-box.mobile-open {{ display: block; }}

      .alert-banner {{
        flex-direction: column;
        align-items: flex-start;
        gap: 10px;
        padding: 12px 14px;
      }}
      .btn-alert {{ width: 100%; text-align: center; padding: 10px; }}

      .jobs-header {{ flex-direction: column; align-items: flex-start; gap: 8px; }}

      .job-card {{ padding: 14px; }}
      .job-date-label {{ position: static; display: inline-block; margin-bottom: 6px; font-size: 11px; color: var(--text-light); }}
      .job-title-text {{ font-size: 15px; }}
      .job-meta-row {{ gap: 10px; }}
      .meta-pill {{ font-size: 11px; }}

      .job-footer-row {{ flex-direction: column; align-items: flex-start; gap: 10px; }}
      .actions-right {{ width: 100%; justify-content: flex-end; }}
      .btn-apply {{ flex: 1; justify-content: center; }}

      .pagination {{ gap: 3px; }}
      .pg-btn {{ min-width: 32px; height: 32px; font-size: 12px; }}
    }}
    @media (max-width: 400px) {{
      .hero-strip h1 {{ font-size: 18px; }}
      .pill {{ font-size: 10px; padding: 2px 8px; }}
    }}
  </style>
</head>
<body>

<!-- TOPBAR -->
<div class="topbar">
  <div class="logo-area">
    <div class="logo">HireAssist</div>
    <div class="logo-tag">Netherlands tech jobs</div>
  </div>
  <div class="nav-right">
    <a href="/ui" class="active">Jobs</a>
    <a href="#" style="color:var(--text-light);cursor:default">Companies</a>
    <a href="/ui/momentum">Company Momentum</a>
    <a href="#" style="color:var(--text-light);cursor:default">For Employers</a>
    <a href="#" class="btn-post">Post a Job</a>
  </div>
  <button class="mobile-menu-btn" onclick="document.getElementById('mobileNav').classList.toggle('open')" aria-label="Menu">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
  </button>
</div>

<div class="mobile-nav" id="mobileNav">
  <a href="/ui" class="active">Jobs</a>
  <a href="#">Companies</a>
  <a href="/ui/momentum">Company Momentum</a>
  <a href="#">For Employers</a>
  <button class="btn-post-mobile">Post a Job</button>
</div>

<!-- ALPHA BAR -->
<div class="alpha-bar">
  Alpha -- coverage may be incomplete. Please share feedback.
</div>

<!-- HERO STRIP -->
<div class="hero-strip">
  <div class="hero-inner">
    <h1>Find jobs that nobody else shows you</h1>
    <p>We crawl company career pages directly -- not just what's on LinkedIn or Indeed. Discover hidden jobs across the Netherlands.</p>

    <form class="search-bar" method="get" action="/ui">
      <div class="search-field">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
        <input type="text" name="q" placeholder="Job title or keyword..." value="{escape(q or '')}">
      </div>
      <div class="search-select-wrap">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/></svg>
        <select name="city">
          <option value="">All cities</option>
          {city_options}
        </select>
      </div>
      <input type="hidden" name="country" value="{escape(country or 'Netherlands')}">
      <button type="submit" class="search-btn">Search</button>
    </form>

    <div class="stats-strip">
      <div class="stat-item">
        <div class="stat-num">{total_real:,}</div>
        <div class="stat-label">Jobs indexed</div>
      </div>
      <div class="stat-item">
        <div class="stat-num">{len(all_companies):,}</div>
        <div class="stat-label">Companies crawled</div>
      </div>
      <div class="stat-item">
        <div class="stat-num" style="color:#6ee7b7">+{new_today_count}</div>
        <div class="stat-label">New today</div>
      </div>
      <div class="stat-item">
        <div class="stat-num">{distinct_cities}</div>
        <div class="stat-label">Cities covered</div>
      </div>
    </div>
  </div>
</div>

<!-- QUICK FILTERS -->
<div class="quick-filters">
  <span class="qf-label">Quick filters:</span>
  <a class="qf-tag {"active" if no_filters else ""}" href="/ui">All</a>
  <a class="qf-tag {"active" if english_only and not new_today_only else ""}" href="/ui?english_only=true">English only</a>
  <a class="qf-tag {"active" if new_today_only and not english_only else ""}" href="/ui?new_today_only=true">New today</a>
  <span class="qf-tag" style="cursor:default;opacity:0.5">Not on LinkedIn</span>
  <span class="qf-tag" style="cursor:default;opacity:0.5">Remote friendly</span>
  <a class="qf-tag {"active" if city and city.lower() == "eindhoven" else ""}" href="/ui?city=Eindhoven">Eindhoven</a>
  <a class="qf-tag {"active" if city and city.lower() == "amsterdam" else ""}" href="/ui?city=Amsterdam">Amsterdam</a>
</div>

<!-- MAIN -->
<div class="main">

  <!-- SIDEBAR -->
  <div class="sidebar">
    <button class="mobile-filter-toggle" id="mobileFilterToggle">
      <span>Filters &amp; Company Momentum</span>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
    </button>
    <form method="get" action="/ui">
      <div class="filter-box">
        <div class="filter-box-title">Filters</div>
        <div class="filter-grid">
          <input type="text" name="q" class="filter-select full" placeholder="Job title or keyword..." value="{escape(q or '')}">

          <select name="company" class="filter-select" onchange="this.form.submit()">
            <option value="">All companies</option>
            {company_options}
          </select>

          <select name="city" class="filter-select" onchange="this.form.submit()">
            <option value="">All cities</option>
            {city_options}
          </select>

          <select name="country" class="filter-select" onchange="this.form.submit()">
            <option value="Netherlands" {"selected" if country == "Netherlands" else ""}>Netherlands</option>
            <option value="" {"selected" if not country else ""}>All countries</option>
            {country_options}
          </select>
        </div>
        <div class="chk-row">
          <label class="chk-label"><input type="checkbox" name="english_only" value="true" {"checked" if english_only else ""} onchange="this.form.submit()"> English only</label>
          <label class="chk-label"><input type="checkbox" name="new_today_only" value="true" {"checked" if new_today_only else ""} onchange="this.form.submit()"> New today</label>
        </div>
        <button type="submit" class="sidebar-search-btn">Search</button>
      </div>
    </form>

    {momentum_html}
  </div>

  <!-- JOBS -->
  <div class="jobs-area">

    <div class="alert-banner">
      <div class="alert-text">
        <span>Get notified when new hidden jobs match your search</span>
      </div>
      <button class="btn-alert" onclick="return false">Set Job Alert</button>
    </div>

    <div class="jobs-header">
      <div class="jobs-count">Showing <strong>{total_real:,} jobs</strong> from <strong>{len(all_companies)} companies</strong> (page {page}/{total_pages})</div>
      <div class="sort-wrap">
        <span class="sort-label">Sort by:</span>
        <select class="sort-select" disabled>
          <option>Company name</option>
        </select>
      </div>
    </div>

    {cards_html}
    {pagination_html}

  </div>
</div>

<script>
document.getElementById('mobileFilterToggle').addEventListener('click', function() {{
  document.querySelectorAll('.filter-box,.momentum-box').forEach(function(el) {{
    el.classList.toggle('mobile-open');
  }});
}});
</script>
</body>
</html>"""
    return HTMLResponse(html)
