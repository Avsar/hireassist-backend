from fastapi import FastAPI, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
import requests
import csv
import json
import os
from pathlib import Path
from db_config import get_db_path
from html import escape
from collections import Counter, defaultdict
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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

# Serve static files (logos, etc.)
from fastapi.staticfiles import StaticFiles
_static_dir = Path(__file__).parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

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

    # Auto-import bundle on cloud platforms (ephemeral filesystem = always fresh DB)
    # Render sets RENDER=true; Railway sets RAILWAY_ENVIRONMENT
    # Runs in background thread so the app can start accepting requests immediately
    is_cloud = (os.environ.get("RENDER", "").lower() in ("true", "1")
                or os.environ.get("RAILWAY_ENVIRONMENT", "") != "")
    if is_cloud and BUNDLE_SEED.exists():
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
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
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


_DUTCH_TITLE_WORDS = [
    "medewerker", "stagiair", "stage", "assistent", "adviseur",
    "monteur", "chauffeur", "verkoper", "boekhouder", "beheerder",
    "leidinggevende", "directeur", "hoofd ", "verzorgende",
    "verpleegkundige", "docent", "leraar", "begeleider",
    "schoonmaker", "magazijnmedewerker", "receptioniste",
    "administratief", "bijrijder", "heftruckchauffeur",
    "werkvoorbereider", "constructeur", "tekenaar", "planner",
    "calculator", "uitvoerder", "voorman", "timmerman",
    "schilder", "elektricien", "loodgieter", "installateur",
    "automonteur", "fietsenmaker", "kok ", "souschef",
    "afdelingshoofd", "teamleider", "coördinator",
    "vrijwilliger", "ervaringsdeskundige", "pedagogisch",
    "allround", "vacature", "solliciteer",
]


def title_looks_dutch(title: str) -> bool:
    """Quick check if a job title is likely Dutch."""
    t = (title or "").lower()
    return any(w in t for w in _DUTCH_TITLE_WORDS)


def title_looks_english(title: str) -> bool:
    """Quick check if a job title is likely English."""
    return not title_looks_dutch(title)


def is_new_today(updated_at: str):
    if not updated_at:
        return False
    try:
        dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - dt <= timedelta(hours=24)
    except Exception:
        return False


def is_stale(first_seen: str, days: int = 60) -> bool:
    if not first_seen:
        return False
    try:
        dt = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - dt > timedelta(days=days)
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


_JUNK_CITIES = frozenset({
    "hybrid", "remote", "in-office", "all offices", "n/a", "na",
    "distributed", "hybrid; in-office", "distributed; hybrid",
    "united states", "us-rem", "us-remote",
})

_CITY_ALIASES = {
    "den bosch": "'s-Hertogenbosch",
    "s-hertogenbosch": "'s-Hertogenbosch",
    "'s-hertogenbosch": "'s-Hertogenbosch",
    "'s- hertogenbosch": "'s-Hertogenbosch",
    "\u2019s-hertogenbosch": "'s-Hertogenbosch",
    "the hague": "Den Haag",
    "den hague": "Den Haag",
    "capelle a/d ijssel": "Capelle aan den IJssel",
    "capelle aan den ijssel": "Capelle aan den IJssel",
    "rijswijk (zh)": "Rijswijk",
    "rijswijk (zh.)": "Rijswijk",
    "'t harde": "'t Harde",
}

# NL city -> province mapping (top ~150 cities)
CITY_TO_PROVINCE = {
    # Noord-Holland
    "amsterdam": "Noord-Holland", "haarlem": "Noord-Holland",
    "zaandam": "Noord-Holland", "hilversum": "Noord-Holland",
    "alkmaar": "Noord-Holland", "hoofddorp": "Noord-Holland",
    "amstelveen": "Noord-Holland", "purmerend": "Noord-Holland",
    "hoorn": "Noord-Holland", "heerhugowaard": "Noord-Holland",
    "beverwijk": "Noord-Holland", "den helder": "Noord-Holland",
    "schiphol": "Noord-Holland", "diemen": "Noord-Holland",
    "bussum": "Noord-Holland", "naarden": "Noord-Holland",
    "weesp": "Noord-Holland", "uithoorn": "Noord-Holland",
    "aalsmeer": "Noord-Holland", "castricum": "Noord-Holland",
    "heemskerk": "Noord-Holland", "huizen": "Noord-Holland",
    "ijmuiden": "Noord-Holland", "enkhuizen": "Noord-Holland",
    "muiden": "Noord-Holland", "badhoevedorp": "Noord-Holland",
    "schagen": "Noord-Holland", "laren": "Noord-Holland",
    "blaricum": "Noord-Holland", "landsmeer": "Noord-Holland",
    # Zuid-Holland
    "rotterdam": "Zuid-Holland", "den haag": "Zuid-Holland",
    "delft": "Zuid-Holland", "leiden": "Zuid-Holland",
    "dordrecht": "Zuid-Holland", "zoetermeer": "Zuid-Holland",
    "schiedam": "Zuid-Holland", "gouda": "Zuid-Holland",
    "vlaardingen": "Zuid-Holland", "capelle aan den ijssel": "Zuid-Holland",
    "alphen aan den rijn": "Zuid-Holland", "rijswijk": "Zuid-Holland",
    "spijkenisse": "Zuid-Holland", "leidschendam": "Zuid-Holland",
    "voorburg": "Zuid-Holland", "wassenaar": "Zuid-Holland",
    "katwijk": "Zuid-Holland", "gorinchem": "Zuid-Holland",
    "nieuwegein": "Zuid-Holland", "papendrecht": "Zuid-Holland",
    "barendrecht": "Zuid-Holland", "voorschoten": "Zuid-Holland",
    "leiderdorp": "Zuid-Holland", "waddinxveen": "Zuid-Holland",
    "sassenheim": "Zuid-Holland", "naaldwijk": "Zuid-Holland",
    "maassluis": "Zuid-Holland", "hellevoetsluis": "Zuid-Holland",
    # Noord-Brabant
    "eindhoven": "Noord-Brabant", "tilburg": "Noord-Brabant",
    "breda": "Noord-Brabant", "'s-hertogenbosch": "Noord-Brabant",
    "helmond": "Noord-Brabant", "oss": "Noord-Brabant",
    "roosendaal": "Noord-Brabant", "bergen op zoom": "Noord-Brabant",
    "waalwijk": "Noord-Brabant", "uden": "Noord-Brabant",
    "veghel": "Noord-Brabant", "best": "Noord-Brabant",
    "veldhoven": "Noord-Brabant", "valkenswaard": "Noord-Brabant",
    "boxtel": "Noord-Brabant", "dongen": "Noord-Brabant",
    "eersel": "Noord-Brabant", "geldrop": "Noord-Brabant",
    "son": "Noord-Brabant", "nuenen": "Noord-Brabant",
    # Gelderland
    "arnhem": "Gelderland", "nijmegen": "Gelderland",
    "apeldoorn": "Gelderland", "ede": "Gelderland",
    "deventer": "Gelderland", "zutphen": "Gelderland",
    "doetinchem": "Gelderland", "harderwijk": "Gelderland",
    "wageningen": "Gelderland", "barneveld": "Gelderland",
    "tiel": "Gelderland", "zevenaar": "Gelderland",
    "elst": "Gelderland", "veenendaal": "Gelderland",
    "bennekom": "Gelderland", "culemborg": "Gelderland",
    "winterswijk": "Gelderland", "ermelo": "Gelderland",
    "duiven": "Gelderland", "nunspeet": "Gelderland",
    "'t harde": "Gelderland", "aalten": "Gelderland",
    # Utrecht
    "utrecht": "Utrecht", "amersfoort": "Utrecht",
    "nieuwegein": "Utrecht", "veenendaal": "Utrecht",
    "zeist": "Utrecht", "de bilt": "Utrecht",
    "bilthoven": "Utrecht", "driebergen": "Utrecht",
    "soest": "Utrecht", "woerden": "Utrecht",
    "ijsselstein": "Utrecht", "maarssen": "Utrecht",
    "houten": "Utrecht", "vianen": "Utrecht",
    "breukelen": "Utrecht", "bunnik": "Utrecht",
    "baarn": "Utrecht",
    # Overijssel
    "enschede": "Overijssel", "zwolle": "Overijssel",
    "hengelo": "Overijssel", "almelo": "Overijssel",
    "kampen": "Overijssel", "oldenzaal": "Overijssel",
    "hardenberg": "Overijssel", "raalte": "Overijssel",
    "rijssen": "Overijssel", "borne": "Overijssel",
    "steenwijk": "Overijssel", "vriezenveen": "Overijssel",
    # Limburg
    "maastricht": "Limburg", "venlo": "Limburg",
    "heerlen": "Limburg", "sittard": "Limburg",
    "roermond": "Limburg", "weert": "Limburg",
    "kerkrade": "Limburg", "geleen": "Limburg",
    "brunssum": "Limburg", "venray": "Limburg",
    # Groningen
    "groningen": "Groningen", "hoogezand": "Groningen",
    "veendam": "Groningen", "stadskanaal": "Groningen",
    "winschoten": "Groningen", "delfzijl": "Groningen",
    # Friesland
    "leeuwarden": "Friesland", "drachten": "Friesland",
    "sneek": "Friesland", "heerenveen": "Friesland",
    "harlingen": "Friesland",
    # Flevoland
    "almere": "Flevoland", "lelystad": "Flevoland",
    "emmeloord": "Flevoland", "dronten": "Flevoland",
    "zeewolde": "Flevoland",
    # Drenthe
    "emmen": "Drenthe", "assen": "Drenthe",
    "hoogeveen": "Drenthe", "meppel": "Drenthe",
    "coevorden": "Drenthe",
    # Zeeland
    "middelburg": "Zeeland", "vlissingen": "Zeeland",
    "goes": "Zeeland", "terneuzen": "Zeeland",
    # Additional cities from data
    "bergeijk": "Noord-Brabant", "bladel": "Noord-Brabant",
    "boxmeer": "Noord-Brabant", "budel": "Noord-Brabant",
    "eersel": "Noord-Brabant", "oisterwijk": "Noord-Brabant",
    "drunen": "Noord-Brabant", "someren": "Noord-Brabant",
    "son en breugel": "Noord-Brabant",
    "bleiswijk": "Zuid-Holland", "waddinxveen": "Zuid-Holland",
    "buren": "Gelderland", "epe": "Gelderland",
    "lochem": "Gelderland", "putten": "Gelderland",
    "wezep": "Gelderland", "hattem": "Gelderland",
    "de bilt": "Utrecht", "bilthoven": "Utrecht",
    "bunschoten": "Utrecht", "leusden": "Utrecht",
    "wijk bij duurstede": "Utrecht",
    "oldenzaal": "Overijssel", "raalte": "Overijssel",
    "vroomshoop": "Overijssel",
}


def _normalize_city(city: str) -> str | None:
    """Clean up city name: strip junk, split multi-city, apply aliases."""
    if not city:
        return None
    city = city.strip()
    # Drop junk values
    if city.lower() in _JUNK_CITIES:
        return None
    # Remove parenthetical suffixes: (on-site), (hybrid), (p), (NL), etc.
    city = re.sub(r"\s*\(.*?\)", "", city).strip()
    # Remove trailing qualifiers
    city = re.sub(r"\s+(?:HQ|Office|Campus|Area|Region|Center|Centre)$", "", city, flags=re.IGNORECASE).strip()
    # Remove leading "US > State > " prefixes
    if city.startswith("US >") or city.startswith("US-"):
        return None
    if not city:
        return None
    # Multi-city: take first city only
    for sep in [";", "|", "/"]:
        if sep in city:
            # Exception: keep " a/d " in "Capelle a/d IJssel"
            if sep == "/" and " a/d " in city.lower():
                continue
            city = city.split(sep)[0].strip()
    # Trailing punctuation
    city = city.rstrip(";.,")
    if not city:
        return None
    # Apply aliases
    cl = city.lower()
    if cl in _CITY_ALIASES:
        return _CITY_ALIASES[cl]
    # Match against known NL cities for consistent casing
    if cl in _NL_CITIES or cl in CITY_TO_PROVINCE:
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
                "department": j.get("department", "") or j.get("category", "") or "",
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
def aggregate_jobs(company=None, q=None, country=None, city=None, english_only=False, new_today_only=False, lang=None, limit=0):
    """Query the jobs table directly -- no live ATS API calls."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        if _HAS_INTEL:
            job_intel.ensure_intel_tables(conn)

        clauses = ["is_active = 1"]
        params = []

        if company:
            clauses.append("company_name = ?")
            params.append(company)
        if q:
            clauses.append("LOWER(title) LIKE ?")
            params.append(f"%{q.lower()}%")

        where = " AND ".join(clauses)
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE {where} ORDER BY company_name, title",
            params,
        ).fetchall()

    all_jobs = []
    for r in rows:
        all_jobs.append({
            "company": r["company_name"],
            "source": r["source"],
            "title": r["title"],
            "department": r["department"] or "",
            "job_type": r["job_type"] or "",
            "location_raw": r["location_raw"] or "",
            "city": _normalize_city(r["city"]) or "",
            "country": r["country"] or "",
            "apply_url": r["url"] or "",
            "updated_at": r["posted_at"] or r["first_seen_at"] or "",
            "is_new_today": is_new_today(r["first_seen_at"] or ""),
            "is_stale": is_stale(r["first_seen_at"] or ""),
            "tech_tags": (r["tech_tags"] if "tech_tags" in r.keys() else "") or "",
            "snippet": "",
        })

    if new_today_only:
        all_jobs = [j for j in all_jobs if j.get("is_new_today")]

    if country:
        all_jobs = [j for j in all_jobs if soft_country_match(j, country)]

    if city:
        all_jobs = [j for j in all_jobs if (j.get("city") or "").lower() == city.lower()]

    # Language filter: title-based for all sources
    effective_lang = lang or ("en" if english_only else None)
    if effective_lang == "en":
        all_jobs = [j for j in all_jobs if title_looks_english(j.get("title", ""))]
    elif effective_lang == "nl":
        all_jobs = [j for j in all_jobs if title_looks_dutch(j.get("title", ""))]

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
    with sqlite3.connect(DB_FILE) as conn:
        if _HAS_INTEL:
            job_intel.ensure_intel_tables(conn)
        rows = conn.execute(
            "SELECT company_name, source, COUNT(*) as cnt "
            "FROM jobs WHERE is_active = 1 "
            "GROUP BY company_name, source ORDER BY company_name"
        ).fetchall()
        report = [{"company": r[0], "source": r[1], "jobs": r[2], "status": "ok"} for r in rows]
        total = sum(r[2] for r in rows)
        result = {"total_jobs": total, "companies": report}
        if full:
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


@app.get("/stats/alerts")
def stats_alerts():
    """Smart hiring alerts detected from daily stats."""
    if not _HAS_INTEL:
        return {"error": "Intelligence layer not available"}
    with sqlite3.connect(DB_FILE) as conn:
        job_intel.ensure_intel_tables(conn)
        alerts = job_intel.detect_alerts(conn)
    return {"alerts": alerts, "count": len(alerts)}


# ----------------------------
# Job alert subscriptions
# ----------------------------
import job_alerts as _job_alerts


@app.post("/api/alerts")
def api_create_alert(
    email: str = Form(...),
    filters_json: str = Form("{}"),
):
    """Create a new job alert subscription (sends confirmation email)."""
    result = _job_alerts.create_alert(email, filters_json)
    status = 200 if result["ok"] else 400
    return JSONResponse(result, status_code=status)


@app.get("/api/alerts/confirm", response_class=HTMLResponse)
def api_confirm_alert(token: str = Query(...)):
    """Confirm an alert subscription via email link."""
    email = _job_alerts.confirm_alert(token)
    if email:
        return _alert_page("Alert confirmed",
                           f"Your job alert for <strong>{escape(email)}</strong> is now active. "
                           "You'll receive a daily email when new jobs match your filters.",
                           success=True)
    return _alert_page("Invalid link",
                       "This confirmation link is invalid or has expired.",
                       success=False)


@app.get("/api/alerts/unsubscribe", response_class=HTMLResponse)
def api_unsubscribe_alert(token: str = Query(...)):
    """Unsubscribe from a job alert via email link."""
    email = _job_alerts.unsubscribe_alert(token)
    if email:
        return _alert_page("Unsubscribed",
                           f"<strong>{escape(email)}</strong> has been unsubscribed. "
                           "You won't receive any more alerts.",
                           success=True)
    return _alert_page("Invalid link",
                       "This unsubscribe link is invalid or has already been used.",
                       success=False)


@app.get("/api/alerts/smtp-test")
def api_smtp_test():
    """Temporary diagnostic: test SMTP connectivity from this server."""
    import smtplib, socket
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    result = {"host": host, "port": port, "user": user, "pass_set": bool(password)}
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=10) as srv:
                srv.login(user, password)
                result["status"] = "ok"
                result["message"] = "SMTP_SSL connection and login succeeded"
        else:
            with smtplib.SMTP(host, port, timeout=10) as srv:
                srv.starttls()
                srv.login(user, password)
                result["status"] = "ok"
                result["message"] = "SMTP+STARTTLS connection and login succeeded"
    except socket.timeout:
        result["status"] = "error"
        result["message"] = f"Connection timed out to {host}:{port} (port likely blocked)"
    except Exception as e:
        result["status"] = "error"
        result["message"] = f"{type(e).__name__}: {e}"
    return JSONResponse(result)


def _alert_page(title: str, message: str, success: bool = True) -> str:
    """Render a minimal standalone HTML page for alert confirm/unsubscribe."""
    icon = "&#10003;" if success else "&#10007;"
    color = "#0d9488" if success else "#dc2626"
    return f"""\
<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - HireAssist</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; display: flex;
         align-items: center; justify-content: center; min-height: 100vh; margin: 0; background: #f8fafc; }}
  .card {{ background: white; border-radius: 12px; padding: 40px; max-width: 420px; text-align: center;
           box-shadow: 0 4px 20px rgba(0,0,0,0.08); }}
  .icon {{ font-size: 48px; color: {color}; margin-bottom: 16px; }}
  h1 {{ font-size: 20px; color: #0f172a; margin: 0 0 12px; }}
  p {{ font-size: 14px; color: #64748b; line-height: 1.6; margin: 0 0 24px; }}
  a {{ display: inline-block; background: #0d9488; color: white; padding: 10px 24px; border-radius: 8px;
       text-decoration: none; font-weight: 600; font-size: 14px; }}
  a:hover {{ background: #0f766e; }}
</style></head><body>
<div class="card">
  <div class="icon">{icon}</div>
  <h1>{title}</h1>
  <p>{message}</p>
  <a href="/ui">Back to HireAssist</a>
</div></body></html>"""


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
        alerts = job_intel.detect_alerts(conn)

    top20 = companies[:20]

    # Build alerts HTML
    alerts_html = ""
    if alerts:
        TYPE_COLORS = {"surge": "#0e9f6e", "slowdown": "#dc2626", "new_entrant": "#2563eb", "gone_dark": "#6b7280"}
        cards = ""
        for a in alerts:
            color = TYPE_COLORS.get(a["type"], "#6b7280")
            cards += (
                f'<div class="alert-card" style="border-left:3px solid {color}">'
                f'<div class="alert-type" style="color:{color}">{escape(a["headline"])}</div>'
                f'<div class="alert-company"><a href="/stats/company/{escape(a["company_name"])}">{escape(a["company_name"])}</a></div>'
                f'<div class="alert-detail">{escape(a["detail"])}</div>'
                f'<div class="alert-jobs">{a["active_jobs"]} active jobs</div>'
                f'</div>'
            )
        alerts_html = (
            f'<div class="alerts-section">'
            f'<div class="alerts-title">Smart Alerts <span class="alerts-count">{len(alerts)}</span></div>'
            f'<div class="alerts-grid">{cards}</div>'
            f'</div>'
        )

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
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Sora:wght@600;800&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Inter', sans-serif; background: #f9fafb; color: #111827; font-size: 14px; line-height: 1.5; }}
    a {{ text-decoration: none; }}
    .topbar {{ background: #fff; border-bottom: 1px solid #e5e7eb; padding: 0 32px; display: flex; align-items: center; justify-content: space-between; height: 52px; position: sticky; top: 0; z-index: 100; }}
    .logo {{ font-size: 18px; font-weight: 700; color: #0d9488; letter-spacing: -0.4px; }}
    .logo-by {{ font-size: 13px; color: #6b7280; font-weight: 500; margin-left: 10px; display: inline-flex; align-items: center; gap: 5px; }}
    .logo-by svg {{ vertical-align: middle; }}
    .cubea-text {{ font-family: 'Sora', sans-serif; font-weight: 600; letter-spacing: -0.3px; }}
    .nav-right {{ display: flex; align-items: center; gap: 24px; }}
    .nav-right a {{ color: #4b5563; font-size: 13px; font-weight: 500; transition: color 0.15s; }}
    .nav-right a:hover {{ color: #0d9488; }}
    .nav-right a.active {{ color: #0d9488; font-weight: 600; }}
    .alpha-bar {{ background: #fefce8; border-bottom: 1px solid #fde68a; text-align: center; padding: 9px 32px; font-size: 13px; color: #92400e; }}
    .container {{ max-width: 900px; margin: 24px auto; padding: 0 32px; }}
    h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 16px; letter-spacing: -0.3px; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
    th {{ background: #f9fafb; text-align: left; padding: 10px 14px; font-size: 11px; font-weight: 600; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.8px; border-bottom: 1px solid #e5e7eb; }}
    td {{ padding: 10px 14px; border-bottom: 1px solid #f3f4f6; font-size: 14px; }}
    td a {{ color: #0d9488; }}
    td a:hover {{ text-decoration: underline; }}
    .positive {{ color: #0e9f6e; font-weight: 600; }}
    .negative {{ color: #c00; font-weight: 600; }}
    .empty {{ text-align: center; padding: 40px; color: #9ca3af; }}
    .alerts-section {{ margin-bottom: 24px; }}
    .alerts-title {{ font-size: 13px; font-weight: 600; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }}
    .alerts-count {{ background: #0e9f6e; color: white; font-size: 10px; font-weight: 700; padding: 1px 6px; border-radius: 10px; }}
    .alerts-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }}
    .alert-card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
    .alert-type {{ font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }}
    .alert-company {{ font-size: 14px; font-weight: 600; color: #111827; margin-bottom: 2px; }}
    .alert-company a {{ color: #0d9488; }}
    .alert-detail {{ font-size: 12px; color: #6b7280; }}
    .alert-jobs {{ font-size: 11px; color: #9ca3af; margin-top: 4px; }}
  </style>
</head><body>
  <div class="topbar">
    <div style="display:flex;align-items:baseline;gap:6px">
      <span class="logo">HireAssist</span>
      <span class="logo-by">by <a href="https://cubea.nl/" target="_blank" rel="noopener" style="display:inline-flex;align-items:center;gap:5px;color:inherit;text-decoration:none"><svg width="20" height="20" viewBox="0 0 70 70" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M35 8 L62 22 L35 36 L8 22 Z" fill="#99f6e4"/><path d="M8 22 L35 36 L35 64 L8 50 Z" fill="#0d9488"/><path d="M35 36 L62 22 L62 50 L35 64 Z" fill="#14b8a6"/></svg> <span class="cubea-text">Cube <span style="color:#0d9488;font-weight:800">A</span></span></a></span>
    </div>
    <div class="nav-right">
      <a href="/ui">Jobs</a>
      <a href="/ui/candidates">Candidates</a>
      <a href="/ui/momentum" class="active">Momentum</a>
      <a href="/ui/report">Report</a>
    </div>
  </div>
  <div class="alpha-bar">Alpha -- coverage may be incomplete. Please share feedback.</div>
  <div class="container">
    <h1>Company Momentum (Top 20)</h1>
    {alerts_html}
    {table_html}
  </div>
</body></html>"""
    return HTMLResponse(html)


# ── Coverage Report ────────────────────────────────────────────
@app.get("/ui/report", response_class=HTMLResponse)
def ui_report():
    """Coverage report dashboard -- discovery progress and gaps."""
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        if _HAS_INTEL:
            job_intel.ensure_intel_tables(conn)

        # --- Overall stats ---
        total_companies = conn.execute("SELECT COUNT(*) FROM companies WHERE active=1").fetchone()[0]
        active_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE is_active=1").fetchone()[0]
        new_today = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE is_active=1 AND DATE(first_seen_at)=DATE('now')"
        ).fetchone()[0]

        # Discovery candidates queued
        try:
            queued_candidates = conn.execute(
                "SELECT COUNT(*) FROM discovery_candidates WHERE status='new'"
            ).fetchone()[0]
        except Exception:
            queued_candidates = 0

        # --- Province coverage from jobs ---
        city_rows = conn.execute(
            "SELECT LOWER(city) as c, COUNT(DISTINCT company_name) as cos, COUNT(*) as jobs "
            "FROM jobs WHERE is_active=1 AND city!='' GROUP BY c"
        ).fetchall()

        province_data = {}
        for prov in sorted(set(CITY_TO_PROVINCE.values())):
            province_data[prov] = {"companies": 0, "jobs": 0, "cities_covered": 0, "cities_total": 0}

        # Count total cities per province
        for _city, prov in CITY_TO_PROVINCE.items():
            province_data[prov]["cities_total"] += 1

        for row in city_rows:
            prov = CITY_TO_PROVINCE.get(row["c"], "")
            if prov and prov in province_data:
                province_data[prov]["companies"] += row["cos"]
                province_data[prov]["jobs"] += row["jobs"]
                province_data[prov]["cities_covered"] += 1

        # --- Discovery pipeline by region ---
        try:
            disc_rows = conn.execute(
                "SELECT LOWER(region) as reg, status, COUNT(*) as cnt "
                "FROM discovery_candidates GROUP BY reg, status"
            ).fetchall()
            disc_pipeline = {}
            for row in disc_rows:
                reg = row["reg"] or "unknown"
                if reg not in disc_pipeline:
                    disc_pipeline[reg] = {"new": 0, "processed": 0, "rejected": 0}
                disc_pipeline[reg][row["status"]] = row["cnt"]
        except Exception:
            disc_pipeline = {}

        # --- ATS breakdown ---
        ats_rows = conn.execute(
            "SELECT source, COUNT(*) as cnt FROM companies WHERE active=1 GROUP BY source ORDER BY cnt DESC"
        ).fetchall()

        # --- Eindhoven deep dive ---
        ehv_companies = conn.execute(
            "SELECT company_name, COUNT(*) as cnt FROM jobs "
            "WHERE is_active=1 AND LOWER(city)='eindhoven' "
            "GROUP BY company_name ORDER BY cnt DESC"
        ).fetchall()

        ehv_departments = conn.execute(
            "SELECT department, COUNT(*) as cnt FROM jobs "
            "WHERE is_active=1 AND LOWER(city)='eindhoven' AND department!='' "
            "GROUP BY department ORDER BY cnt DESC LIMIT 15"
        ).fetchall()

        ehv_disc = disc_pipeline.get("eindhoven", {"new": 0, "processed": 0, "rejected": 0})

        # --- Zero-job companies count ---
        zero_job_cos = conn.execute(
            "SELECT COUNT(*) FROM companies WHERE active=1 AND name NOT IN "
            "(SELECT DISTINCT company_name FROM jobs WHERE is_active=1)"
        ).fetchone()[0]

    # --- Build province rows ---
    province_rows_html = ""
    for prov in sorted(province_data.keys()):
        d = province_data[prov]
        if d["jobs"] >= 100:
            color = "#0e9f6e"
            bg = "#e8f8f3"
        elif d["jobs"] > 0:
            color = "#d97706"
            bg = "#fef3c7"
        else:
            color = "#dc2626"
            bg = "#fef2f2"
        badge = f'<span style="background:{bg};color:{color};padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">{d["jobs"]}</span>'
        province_rows_html += (
            f"<tr>"
            f"<td style='font-weight:600'>{escape(prov)}</td>"
            f"<td>{d['companies']}</td>"
            f"<td>{badge}</td>"
            f"<td>{d['cities_covered']} / {d['cities_total']}</td>"
            f"</tr>"
        )

    # --- ATS breakdown rows ---
    ats_html = ""
    for row in ats_rows:
        ats_html += f"<tr><td style='font-weight:500'>{escape(row['source'])}</td><td>{row['cnt']}</td></tr>"

    # --- Eindhoven companies rows ---
    ehv_html = ""
    for row in ehv_companies[:15]:
        ehv_html += f"<tr><td>{escape(row['company_name'])}</td><td>{row['cnt']}</td></tr>"

    # --- Eindhoven departments ---
    dept_html = ""
    for row in ehv_departments:
        dept_html += f"<tr><td>{escape(row['department'])}</td><td>{row['cnt']}</td></tr>"

    # --- Action items ---
    actions = []
    if ehv_disc["new"] > 0:
        actions.append(f"<strong>{ehv_disc['new']}</strong> discovery candidates queued in Eindhoven -- run <code>agent_discover.py</code> to process")
    if queued_candidates > 0:
        actions.append(f"<strong>{queued_candidates}</strong> total candidates queued across all regions")
    if zero_job_cos > 0:
        actions.append(f"<strong>{zero_job_cos}</strong> tracked companies have 0 active jobs -- may need ATS re-sync or verification")
    # Brabant cities with no coverage
    brabant_empty = [c.title() for c, p in CITY_TO_PROVINCE.items()
                     if p == "Noord-Brabant" and not any(r["c"] == c for r in city_rows)]
    if brabant_empty:
        actions.append(f"<strong>{len(brabant_empty)}</strong> Brabant cities with no job coverage: {', '.join(brabant_empty[:8])}{'...' if len(brabant_empty) > 8 else ''}")

    actions_html = "".join(f"<li style='margin-bottom:8px'>{a}</li>" for a in actions)
    if not actions_html:
        actions_html = "<li>No action items -- coverage looks good!</li>"

    # --- Discovery pipeline table ---
    disc_html = ""
    for reg in sorted(disc_pipeline.keys()):
        d = disc_pipeline[reg]
        total = d["new"] + d["processed"] + d["rejected"]
        disc_html += (
            f"<tr>"
            f"<td style='font-weight:500'>{escape(reg.title())}</td>"
            f"<td>{total}</td>"
            f"<td>{d['processed']}</td>"
            f"<td>{d['new']}</td>"
            f"<td>{d['rejected']}</td>"
            f"</tr>"
        )

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
  <meta charset="utf-8"/><title>Coverage Report - HireAssist</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Sora:wght@600;800&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Inter', sans-serif; background: #f9fafb; color: #111827; font-size: 14px; line-height: 1.5; }}
    a {{ text-decoration: none; }}
    .topbar {{ background: #fff; border-bottom: 1px solid #e5e7eb; padding: 0 32px; display: flex; align-items: center; justify-content: space-between; height: 52px; position: sticky; top: 0; z-index: 100; }}
    .logo {{ font-size: 18px; font-weight: 700; color: #0d9488; letter-spacing: -0.4px; }}
    .logo-by {{ font-size: 13px; color: #6b7280; font-weight: 500; margin-left: 10px; display: inline-flex; align-items: center; gap: 5px; }}
    .logo-by svg {{ vertical-align: middle; }}
    .cubea-text {{ font-family: 'Sora', sans-serif; font-weight: 600; letter-spacing: -0.3px; }}
    .nav-right {{ display: flex; align-items: center; gap: 24px; }}
    .nav-right a {{ color: #4b5563; font-size: 13px; font-weight: 500; transition: color 0.15s; }}
    .nav-right a:hover {{ color: #0d9488; }}
    .nav-right a.active {{ color: #0d9488; font-weight: 600; }}
    .alpha-bar {{ background: #fefce8; border-bottom: 1px solid #fde68a; text-align: center; padding: 9px 32px; font-size: 13px; color: #92400e; }}
    .container {{ max-width: 1100px; margin: 24px auto; padding: 0 32px; }}
    h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 20px; letter-spacing: -0.3px; }}
    h2 {{ font-size: 16px; font-weight: 600; margin-bottom: 12px; color: #111827; }}

    .stat-cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 32px; }}
    .stat-card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
    .stat-card .label {{ font-size: 12px; color: #9ca3af; font-weight: 500; text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 4px; }}
    .stat-card .value {{ font-size: 28px; font-weight: 700; color: #111827; letter-spacing: -0.5px; }}
    .stat-card .value.teal {{ color: #0d9488; }}

    .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 32px; }}
    .card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}

    table {{ width: 100%; border-collapse: collapse; }}
    th {{ text-align: left; padding: 8px 12px; font-size: 11px; font-weight: 600; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.8px; border-bottom: 1px solid #e5e7eb; }}
    td {{ padding: 8px 12px; border-bottom: 1px solid #f3f4f6; font-size: 13px; }}
    tr:last-child td {{ border-bottom: none; }}

    .actions {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); margin-bottom: 32px; }}
    .actions ul {{ list-style: none; padding: 0; }}
    .actions li {{ padding: 8px 0 8px 24px; position: relative; font-size: 13px; color: #4b5563; }}
    .actions li::before {{ content: "->"; position: absolute; left: 0; color: #0d9488; font-weight: 700; }}
    code {{ background: #f3f4f6; padding: 2px 6px; border-radius: 4px; font-size: 12px; }}
  </style>
</head><body>
  <div class="topbar">
    <div style="display:flex;align-items:center;gap:6px">
      <span class="logo">HireAssist</span>
      <span class="logo-by">by <a href="https://cubea.nl/" target="_blank" rel="noopener" style="display:inline-flex;align-items:center;gap:5px;color:inherit;text-decoration:none"><svg width="20" height="20" viewBox="0 0 70 70" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M35 8 L62 22 L35 36 L8 22 Z" fill="#99f6e4"/><path d="M8 22 L35 36 L35 64 L8 50 Z" fill="#0d9488"/><path d="M35 36 L62 22 L62 50 L35 64 Z" fill="#14b8a6"/></svg> <span class="cubea-text">Cube <span style="color:#0d9488;font-weight:800">A</span></span></a></span>
    </div>
    <div class="nav-right">
      <a href="/ui">Jobs</a>
      <a href="/ui/candidates">Candidates</a>
      <a href="/ui/momentum">Momentum</a>
      <a href="/ui/report" class="active">Report</a>
    </div>
  </div>
  <div class="alpha-bar">Alpha -- coverage may be incomplete. Please share feedback.</div>
  <div class="container">
    <h1>Coverage Report</h1>

    <!-- STAT CARDS -->
    <div class="stat-cards">
      <div class="stat-card"><div class="label">Companies Tracked</div><div class="value">{total_companies:,}</div></div>
      <div class="stat-card"><div class="label">Active Jobs</div><div class="value teal">{active_jobs:,}</div></div>
      <div class="stat-card"><div class="label">New Today</div><div class="value">{new_today:,}</div></div>
      <div class="stat-card"><div class="label">Candidates Queued</div><div class="value">{queued_candidates:,}</div></div>
    </div>

    <!-- ACTION ITEMS -->
    <div class="actions">
      <h2>Action Items</h2>
      <ul>{actions_html}</ul>
    </div>

    <!-- PROVINCE TABLE -->
    <div class="card" style="margin-bottom:32px">
      <h2>Province Coverage</h2>
      <table>
        <tr><th>Province</th><th>Companies</th><th>Active Jobs</th><th>City Coverage</th></tr>
        {province_rows_html}
      </table>
    </div>

    <!-- 2-COL GRID -->
    <div class="grid-2">
      <div class="card">
        <h2>ATS Platform Breakdown</h2>
        <table>
          <tr><th>Platform</th><th>Companies</th></tr>
          {ats_html}
        </table>
      </div>

      <div class="card">
        <h2>Discovery Pipeline by Region</h2>
        <table>
          <tr><th>Region</th><th>Total</th><th>Processed</th><th>Queued</th><th>Rejected</th></tr>
          {disc_html if disc_html else "<tr><td colspan='5' style='color:#9ca3af;text-align:center;padding:20px'>No discovery data yet</td></tr>"}
        </table>
      </div>
    </div>

    <!-- EINDHOVEN DEEP DIVE -->
    <h1 style="margin-top:8px">Eindhoven Deep Dive</h1>
    <div class="stat-cards" style="grid-template-columns:repeat(3,1fr);margin-bottom:24px">
      <div class="stat-card"><div class="label">Companies with Jobs</div><div class="value teal">{len(ehv_companies)}</div></div>
      <div class="stat-card"><div class="label">Candidates Queued</div><div class="value">{ehv_disc['new']}</div></div>
      <div class="stat-card"><div class="label">Candidates Processed</div><div class="value">{ehv_disc['processed']}</div></div>
    </div>

    <div class="grid-2">
      <div class="card">
        <h2>Top Hiring Companies</h2>
        <table>
          <tr><th>Company</th><th>Jobs</th></tr>
          {ehv_html if ehv_html else "<tr><td colspan='2' style='color:#9ca3af;text-align:center;padding:20px'>No jobs in Eindhoven</td></tr>"}
        </table>
      </div>

      <div class="card">
        <h2>Departments</h2>
        <table>
          <tr><th>Department</th><th>Jobs</th></tr>
          {dept_html if dept_html else "<tr><td colspan='2' style='color:#9ca3af;text-align:center;padding:20px'>No department data</td></tr>"}
        </table>
      </div>
    </div>

  </div>
</body></html>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# /ui/candidates -- discovery candidates browser
# ---------------------------------------------------------------------------
@app.get("/ui/candidates", response_class=HTMLResponse)
def ui_candidates(
    status: str | None = Query(default=None),
    city: str | None = Query(default=None),
    source: str | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
):
    """Browsable, filterable table of discovery_candidates."""
    SOURCE_LABELS = {"osm": "OSM", "kvk": "KVK", "google_places": "Google Places"}

    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row

        # -- summary stats (unfiltered) --
        try:
            status_rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM discovery_candidates GROUP BY status"
            ).fetchall()
            stats = {r["status"]: r["cnt"] for r in status_rows}
        except Exception:
            stats = {}

        total = sum(stats.values())
        new_count = stats.get("new", 0)
        processed_count = stats.get("processed", 0)
        rejected_count = stats.get("rejected", 0)

        # -- distinct cities & sources for dropdowns --
        try:
            all_cities = [r["city"] for r in conn.execute(
                "SELECT DISTINCT city FROM discovery_candidates WHERE city IS NOT NULL AND city != '' ORDER BY city"
            ).fetchall()]
        except Exception:
            all_cities = []

        try:
            all_sources = [r["source"] for r in conn.execute(
                "SELECT DISTINCT source FROM discovery_candidates ORDER BY source"
            ).fetchall()]
        except Exception:
            all_sources = []

        # -- build filtered query --
        where_clauses: list[str] = []
        params: list = []

        if status:
            where_clauses.append("status = ?")
            params.append(status)
        if city:
            where_clauses.append("LOWER(city) = LOWER(?)")
            params.append(city)
        if source:
            where_clauses.append("source = ?")
            params.append(source)
        if q:
            where_clauses.append("LOWER(name) LIKE LOWER(?)")
            params.append(f"%{q}%")

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        PER_PAGE = 50
        try:
            filtered_total = conn.execute(
                f"SELECT COUNT(*) FROM discovery_candidates{where_sql}", params
            ).fetchone()[0]
        except Exception:
            filtered_total = 0

        total_pages = max(1, (filtered_total + PER_PAGE - 1) // PER_PAGE)
        page = min(page, total_pages)
        offset = (page - 1) * PER_PAGE

        try:
            candidates = conn.execute(
                f"""SELECT id, name, website, city, source, status, score,
                           reject_reason, ats_verified, website_domain, processed_at
                    FROM discovery_candidates{where_sql}
                    ORDER BY
                        CASE status WHEN 'new' THEN 0 WHEN 'processed' THEN 1 ELSE 2 END,
                        score DESC,
                        name ASC
                    LIMIT ? OFFSET ?""",
                params + [PER_PAGE, offset],
            ).fetchall()
        except Exception:
            candidates = []

    # -- helper: pagination urls --
    def page_url(p):
        parts = []
        if status:
            parts.append(f"status={escape(status)}")
        if city:
            parts.append(f"city={escape(city)}")
        if source:
            parts.append(f"source={escape(source)}")
        if q:
            parts.append(f"q={escape(q)}")
        parts.append(f"page={p}")
        return "/ui/candidates?" + "&amp;".join(parts)

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

    # -- dropdown helpers --
    sel = lambda val, cur: "selected" if val == cur else ""  # noqa: E731
    active_cls = lambda val: "active-filter" if status == val else ""  # noqa: E731

    city_options = "".join(
        f'<option value="{escape(c)}" {sel(c, city)}>{escape(c)}</option>'
        for c in all_cities
    )
    source_options = "".join(
        f'<option value="{escape(s)}" {sel(s, source)}>{escape(SOURCE_LABELS.get(s, s))}</option>'
        for s in all_sources
    )

    # -- table rows --
    rows_html = ""
    for c in candidates:
        name_esc = escape(c["name"])
        city_esc = escape(c["city"] or "")
        src_label = SOURCE_LABELS.get(c["source"], escape(c["source"]))
        st = c["status"]
        st_cls = f"status-{st}"
        status_pill = f'<span class="status-pill {st_cls}">{escape(st.title())}</span>'

        website = c["website"] or ""
        domain = c["website_domain"] or ""
        if website:
            website_html = f'<a class="website-link" href="{escape(website)}" target="_blank" rel="noopener">{escape(domain or website[:40])}</a>'
        else:
            website_html = '<span style="color:#d1d5db">--</span>'

        score_val = c["score"]
        score_html = str(score_val) if score_val is not None else '<span style="color:#d1d5db">--</span>'

        reject = escape(c["reject_reason"] or "") if st == "rejected" else ""
        if reject:
            short = reject[:40] + ("..." if len(reject) > 40 else "")
            reject_html = f'<span style="font-size:12px;color:#6b7280" title="{reject}">{short}</span>'
        else:
            reject_html = '<span style="color:#d1d5db">--</span>'

        rows_html += (
            f"<tr>"
            f"<td style='font-weight:500'>{name_esc}</td>"
            f"<td>{city_esc}</td>"
            f"<td><span class='source-badge'>{src_label}</span></td>"
            f"<td>{status_pill}</td>"
            f"<td>{website_html}</td>"
            f"<td style='text-align:center'>{score_html}</td>"
            f"<td>{reject_html}</td>"
            f"</tr>"
        )

    if not rows_html:
        rows_html = '<tr><td colspan="7" style="color:#9ca3af;text-align:center;padding:30px">No candidates found matching your filters</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
  <meta charset="utf-8"/><title>Discovery Candidates - HireAssist</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Sora:wght@600;800&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Inter', sans-serif; background: #f9fafb; color: #111827; font-size: 14px; line-height: 1.5; }}
    a {{ text-decoration: none; }}
    .topbar {{ background: #fff; border-bottom: 1px solid #e5e7eb; padding: 0 32px; display: flex; align-items: center; justify-content: space-between; height: 52px; position: sticky; top: 0; z-index: 100; }}
    .logo {{ font-size: 18px; font-weight: 700; color: #0d9488; letter-spacing: -0.4px; }}
    .logo-by {{ font-size: 13px; color: #6b7280; font-weight: 500; margin-left: 10px; display: inline-flex; align-items: center; gap: 5px; }}
    .logo-by svg {{ vertical-align: middle; }}
    .cubea-text {{ font-family: 'Sora', sans-serif; font-weight: 600; letter-spacing: -0.3px; }}
    .nav-right {{ display: flex; align-items: center; gap: 24px; }}
    .nav-right a {{ color: #4b5563; font-size: 13px; font-weight: 500; transition: color 0.15s; }}
    .nav-right a:hover {{ color: #0d9488; }}
    .nav-right a.active {{ color: #0d9488; font-weight: 600; }}
    .alpha-bar {{ background: #fefce8; border-bottom: 1px solid #fde68a; text-align: center; padding: 9px 32px; font-size: 13px; color: #92400e; }}
    .container {{ max-width: 1200px; margin: 24px auto; padding: 0 32px; }}
    h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 20px; letter-spacing: -0.3px; }}

    .stat-cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }}
    .stat-card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); transition: border-color 0.15s; }}
    .stat-card a {{ text-decoration: none; color: inherit; display: block; }}
    .stat-card:hover {{ border-color: #0d9488; }}
    .stat-card.active-filter {{ border-color: #0d9488; border-width: 2px; }}
    .stat-card .label {{ font-size: 12px; color: #9ca3af; font-weight: 500; text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 4px; }}
    .stat-card .value {{ font-size: 28px; font-weight: 700; color: #111827; letter-spacing: -0.5px; }}
    .stat-card .value.teal {{ color: #0d9488; }}

    .filter-bar {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; align-items: center; }}
    .filter-bar select, .filter-bar input[type="text"] {{
      border: 1px solid #e5e7eb; border-radius: 6px; padding: 7px 10px;
      font-family: 'Inter', sans-serif; font-size: 13px; color: #111827; outline: none; background: #fff;
    }}
    .filter-bar select:focus, .filter-bar input:focus {{ border-color: #0d9488; }}
    .filter-bar .search-input {{ min-width: 200px; }}
    .filter-bar .btn-search {{ background: #0d9488; color: #fff; border: none; border-radius: 6px; padding: 7px 16px; font-size: 13px; font-weight: 600; cursor: pointer; }}
    .filter-bar .btn-search:hover {{ background: #0f766e; }}
    .filter-bar .btn-clear {{ color: #6b7280; font-size: 12px; font-weight: 500; }}
    .filter-bar .btn-clear:hover {{ color: #0d9488; }}

    .card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 0; box-shadow: 0 1px 3px rgba(0,0,0,0.06); overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{ text-align: left; padding: 10px 14px; font-size: 11px; font-weight: 600; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.8px; border-bottom: 1px solid #e5e7eb; background: #fafafa; }}
    td {{ padding: 9px 14px; border-bottom: 1px solid #f3f4f6; font-size: 13px; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover {{ background: #f9fafb; }}

    .status-pill {{ display: inline-block; padding: 2px 10px; border-radius: 10px; font-size: 11px; font-weight: 600; }}
    .status-new {{ background: #dbeafe; color: #1d4ed8; }}
    .status-processed {{ background: #d1fae5; color: #065f46; }}
    .status-rejected {{ background: #fee2e2; color: #991b1b; }}
    .source-badge {{ font-size: 11px; color: #6b7280; font-weight: 500; }}
    .website-link {{ color: #0d9488; font-size: 12px; }}
    .website-link:hover {{ text-decoration: underline; }}

    .pagination {{ display: flex; justify-content: center; gap: 4px; margin-top: 20px; margin-bottom: 20px; }}
    .pg-btn {{ display: inline-flex; align-items: center; justify-content: center; min-width: 36px; height: 36px; border-radius: 6px; font-size: 13px; font-weight: 500; color: #4b5563; background: #fff; border: 1px solid #e5e7eb; cursor: pointer; text-decoration: none; }}
    .pg-btn:hover {{ background: #f3f4f6; }}
    .pg-btn.active {{ background: #0d9488; color: #fff; border-color: #0d9488; }}
    .pg-dots {{ padding: 0 4px; color: #9ca3af; align-self: center; }}

    .results-meta {{ margin-bottom: 12px; font-size: 13px; color: #6b7280; }}

    @media (max-width: 768px) {{
      .topbar {{ padding: 0 16px; }}
      .container {{ padding: 0 16px; }}
      .stat-cards {{ grid-template-columns: repeat(2, 1fr); gap: 10px; }}
      .filter-bar {{ flex-direction: column; }}
      .filter-bar select, .filter-bar input[type="text"] {{ width: 100%; }}
    }}
  </style>
</head><body>
  <div class="topbar">
    <div style="display:flex;align-items:center;gap:6px">
      <span class="logo">HireAssist</span>
      <span class="logo-by">by <a href="https://cubea.nl/" target="_blank" rel="noopener" style="display:inline-flex;align-items:center;gap:5px;color:inherit;text-decoration:none"><svg width="20" height="20" viewBox="0 0 70 70" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M35 8 L62 22 L35 36 L8 22 Z" fill="#99f6e4"/><path d="M8 22 L35 36 L35 64 L8 50 Z" fill="#0d9488"/><path d="M35 36 L62 22 L62 50 L35 64 Z" fill="#14b8a6"/></svg> <span class="cubea-text">Cube <span style="color:#0d9488;font-weight:800">A</span></span></a></span>
    </div>
    <div class="nav-right">
      <a href="/ui">Jobs</a>
      <a href="/ui/candidates" class="active">Candidates</a>
      <a href="/ui/momentum">Momentum</a>
      <a href="/ui/report">Report</a>
    </div>
  </div>
  <div class="alpha-bar">Alpha -- coverage may be incomplete. Please share feedback.</div>

  <div class="container">
    <h1>Discovery Candidates</h1>

    <div class="stat-cards">
      <div class="stat-card {active_cls(None)}"><a href="/ui/candidates">
        <div class="label">Total</div><div class="value">{total:,}</div>
      </a></div>
      <div class="stat-card {active_cls('new')}"><a href="/ui/candidates?status=new">
        <div class="label">New</div><div class="value teal">{new_count:,}</div>
      </a></div>
      <div class="stat-card {active_cls('processed')}"><a href="/ui/candidates?status=processed">
        <div class="label">Processed</div><div class="value">{processed_count:,}</div>
      </a></div>
      <div class="stat-card {active_cls('rejected')}"><a href="/ui/candidates?status=rejected">
        <div class="label">Rejected</div><div class="value">{rejected_count:,}</div>
      </a></div>
    </div>

    <form method="get" action="/ui/candidates" class="filter-bar">
      <input type="text" name="q" class="search-input" placeholder="Search by name..." value="{escape(q or '')}">
      <select name="status" onchange="this.form.submit()">
        <option value="">All statuses</option>
        <option value="new" {sel('new', status)}>New</option>
        <option value="processed" {sel('processed', status)}>Processed</option>
        <option value="rejected" {sel('rejected', status)}>Rejected</option>
      </select>
      <select name="city" onchange="this.form.submit()">
        <option value="">All cities</option>
        {city_options}
      </select>
      <select name="source" onchange="this.form.submit()">
        <option value="">All sources</option>
        {source_options}
      </select>
      <button type="submit" class="btn-search">Search</button>
      <a href="/ui/candidates" class="btn-clear">Clear filters</a>
    </form>

    <div class="results-meta">
      Showing {filtered_total:,} candidates (page {page} of {total_pages})
    </div>

    <div class="card">
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>City</th>
            <th>Source</th>
            <th>Status</th>
            <th>Website</th>
            <th>Score</th>
            <th>Reject Reason</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </div>

    {pagination_html}
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
    hide_stale: bool = Query(default=False),
    lang: str | None = Query(default=None),
    tech: str | None = Query(default=None),
    sort: str = Query(default="newest"),
    page: int = Query(default=1, ge=1),
):
    PER_PAGE = 100
    all_companies = load_companies()
    all_jobs = aggregate_jobs(company, q, None, None, english_only, new_today_only, lang=lang)
    if hide_stale:
        all_jobs = [j for j in all_jobs if not j.get("is_stale")]
    countries = unique([j.get("country") for j in all_jobs if j.get("country")])
    country_jobs = [j for j in all_jobs if soft_country_match(j, country)] if country else all_jobs
    cities = unique([j.get("city") for j in country_jobs if j.get("city")])
    # Resolve province filter: city=province:Noord-Holland -> match all cities in that province
    _province_filter = None
    _city_filter = city
    if city and city.startswith("province:"):
        _province_filter = city[len("province:"):]
        _city_filter = None

    if _province_filter:
        prov_lower = _province_filter.lower()
        prov_cities = {c for c, p in CITY_TO_PROVINCE.items() if p.lower() == prov_lower}
        all_visible = [j for j in country_jobs
                       if not j.get("_placeholder")
                       and (j.get("city") or "").lower() in prov_cities]
    else:
        all_visible = [j for j in country_jobs
                       if not j.get("_placeholder")
                       and (not _city_filter or (j.get("city") or "").lower() == _city_filter.lower())]

    # Tech stack filter
    if tech:
        tech_lower = tech.lower()
        all_visible = [j for j in all_visible
                       if tech_lower in (j.get("tech_tags") or "").lower().split("|")]

    # Sort
    if sort == "newest":
        all_visible.sort(key=lambda j: j.get("updated_at") or "", reverse=True)
    elif sort == "company":
        all_visible.sort(key=lambda j: (j.get("company", ""), j.get("title", "")))

    total_real = sum(1 for j in all_visible if not j.get("_placeholder"))
    total_visible = len(all_visible)
    total_pages = (total_visible + PER_PAGE - 1) // PER_PAGE if total_visible else 1
    page = min(page, total_pages)
    start = (page - 1) * PER_PAGE
    visible_jobs = all_visible[start:start + PER_PAGE]

    # Extra stats for the hero strip
    new_today_count = sum(1 for j in all_visible if j.get("is_new_today"))
    filtered_company_count = len(set(j.get("company") for j in all_visible if j.get("company")))
    distinct_cities = len(set(j.get("city") for j in all_visible if j.get("city")))

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
                    '<a href="/ui/momentum" style="font-size:12px;color:#0d9488;font-weight:500;text-decoration:none">View full leaderboard &#8594;</a>'
                    '</div></div>'
                )
        except Exception:
            pass

    # Smart alerts for sidebar (compact, max 3)
    sidebar_alerts_html = ""
    if _HAS_INTEL:
        try:
            with sqlite3.connect(DB_FILE) as conn:
                job_intel.ensure_intel_tables(conn)
                _alerts = job_intel.detect_alerts(conn)
            top3 = _alerts[:3]
            if top3:
                TYPE_ICONS = {"surge": ">>", "slowdown": "vv", "new_entrant": "NEW", "gone_dark": "--"}
                TYPE_COLORS = {"surge": "#0e9f6e", "slowdown": "#dc2626", "new_entrant": "#2563eb", "gone_dark": "#6b7280"}
                a_items = ""
                for a in top3:
                    icon = TYPE_ICONS.get(a["type"], "?")
                    color = TYPE_COLORS.get(a["type"], "#6b7280")
                    short_detail = a["detail"][:25]
                    a_items += (
                        f'<div class="sa-item">'
                        f'<div class="sa-icon" style="color:{color}">{icon}</div>'
                        f'<div class="sa-name">{escape(a["company_name"])}</div>'
                        f'<div class="sa-detail">{escape(short_detail)}</div>'
                        f'</div>'
                    )
                sidebar_alerts_html = (
                    '<div class="alerts-box">'
                    '<div class="momentum-header">'
                    '<div class="momentum-title-text">Smart Alerts <span class="momentum-badge">LIVE</span></div>'
                    '</div>'
                    '<div class="momentum-sub-text">Hiring signals this week</div>'
                    f'{a_items}'
                    '<div style="margin-top:12px;text-align:center">'
                    '<a href="/ui/momentum" style="font-size:12px;color:#0d9488;font-weight:500;text-decoration:none">View all alerts &#8594;</a>'
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

    # Build province + city dropdowns (cascading for NL, flat for other countries)
    is_nl = (country or "").lower() in ("netherlands", "")
    province_options = ""
    province_cities_js = "{}"
    if is_nl and cities:
        province_cities = defaultdict(list)
        for c in cities:
            prov = CITY_TO_PROVINCE.get(c.lower())
            if prov:
                province_cities[prov].append(c)
        prov_order = [
            "Noord-Holland", "Zuid-Holland", "Utrecht", "Noord-Brabant",
            "Gelderland", "Overijssel", "Limburg", "Groningen",
            "Friesland", "Flevoland", "Drenthe", "Zeeland",
        ]
        # Determine selected province
        _selected_province = ""
        if city and city.startswith("province:"):
            _selected_province = city[len("province:"):]
        elif city:
            _selected_province = CITY_TO_PROVINCE.get(city.lower(), "")

        # Province dropdown options
        for prov in prov_order:
            pcs = sorted(province_cities.get(prov, []))
            if pcs:
                prov_val = f"province:{prov}"
                prov_sel = "selected" if _selected_province == prov else ""
                prov_cities_set = {c.lower() for c in pcs}
                job_count = sum(1 for j in country_jobs if (j.get("city") or "").lower() in prov_cities_set)
                province_options += f'<option value="{escape(prov_val)}" {prov_sel}>{escape(prov)} ({job_count})</option>'

        # City dropdown options -- flat list with data-province attribute
        city_options = ""
        for prov in prov_order:
            pcs = sorted(province_cities.get(prov, []))
            for c in pcs:
                sel = "selected" if c == city else ""
                city_options += f'<option value="{escape(c)}" data-province="{escape(prov)}" {sel}>{escape(c)}</option>'

        # JS mapping for cascade behaviour
        js_parts = []
        for prov in prov_order:
            pcs = sorted(province_cities.get(prov, []))
            if pcs:
                cities_json = ",".join(f'"{c}"' for c in pcs)
                js_parts.append(f'"{prov}":[{cities_json}]')
        province_cities_js = "{" + ",".join(js_parts) + "}"
    else:
        city_options = "".join(opt(c, city) for c in cities)

    # Tech stack dropdown options (top 20 tags from visible jobs)
    _tag_counts = Counter()
    for j in all_visible:
        for t in (j.get("tech_tags") or "").split("|"):
            if t:
                _tag_counts[t] += 1
    tech_options = ""
    for tag_name, tag_cnt in _tag_counts.most_common(20):
        sel = "selected" if tag_name == tech else ""
        tech_options += f'<option value="{escape(tag_name)}" {sel}>{escape(tag_name)} ({tag_cnt})</option>'

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
        if j.get("is_stale"):
            tags += '<span class="pill pill-stale">60+ days</span>'
        via = SOURCE_NAMES.get(j.get("source", ""), "")
        if via:
            tags += f'<span class="pill pill-source">via {escape(via)}</span>'
        # Tech stack pills (max 4)
        _tech_list = [t for t in (j.get("tech_tags") or "").split("|") if t]
        for _tt in _tech_list[:4]:
            tags += f'<span class="pill pill-tech">{escape(_tt)}</span>'

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

    # Base params for sort dropdown (excludes sort and page)
    _base = []
    if company: _base.append(f"company={escape(company)}")
    if q: _base.append(f"q={escape(q)}")
    if country: _base.append(f"country={escape(country)}")
    if city: _base.append(f"city={escape(city)}")
    if english_only: _base.append("english_only=true")
    if new_today_only: _base.append("new_today_only=true")
    if hide_stale: _base.append("hide_stale=true")
    if lang: _base.append(f"lang={escape(lang)}")
    if tech: _base.append(f"tech={escape(tech)}")
    sort_base_params = "&amp;".join(_base) if _base else "country=Netherlands"

    # Build pagination controls
    def page_url(p):
        params = []
        if company: params.append(f"company={escape(company)}")
        if q: params.append(f"q={escape(q)}")
        if country: params.append(f"country={escape(country)}")
        if city: params.append(f"city={escape(city)}")
        if english_only: params.append("english_only=true")
        if new_today_only: params.append("new_today_only=true")
        if hide_stale: params.append("hide_stale=true")
        if lang: params.append(f"lang={escape(lang)}")
        if tech: params.append(f"tech={escape(tech)}")
        if sort != "newest": params.append(f"sort={escape(sort)}")
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

    no_filters = not q and not company and not english_only and not new_today_only and not hide_stale and not city and country == "Netherlands"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>HireAssist - Jobs in NL</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Sora:wght@600;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --primary: #0d9488; --primary-light: #f0fdfa; --primary-mid: #14b8a6;
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
    .logo-area {{ display: flex; align-items: center; gap: 6px; }}
    .logo {{ font-size: 18px; font-weight: 700; color: var(--primary); letter-spacing: -0.4px; }}
    .logo-by {{ font-size: 13px; color: var(--text-light); font-weight: 500; margin-left: 10px; display: inline-flex; align-items: center; gap: 5px; }}
    .logo-by svg {{ vertical-align: middle; }}
    .cubea-text {{ font-family: 'Sora', sans-serif; font-weight: 600; letter-spacing: -0.3px; }}
    .nav-right {{ display: flex; align-items: center; gap: 24px; }}
    .nav-right a {{ color: var(--text-mid); font-size: 13px; font-weight: 500; transition: color 0.15s; }}
    .nav-right a:hover {{ color: var(--primary); }}
    .nav-right a.active {{ color: var(--primary); font-weight: 600; }}
    .btn-post {{ background: var(--primary); color: white !important; padding: 7px 16px; border-radius: 6px; font-size: 13px !important; font-weight: 600 !important; }}
    .btn-post:hover {{ background: #0f766e !important; }}

    /* ALPHA BAR */
    .alpha-bar {{ background: #fefce8; border-bottom: 1px solid #fde68a; text-align: center; padding: 9px 32px; font-size: 13px; color: #92400e; }}

    /* HERO STRIP */
    .hero-strip {{ background: linear-gradient(135deg, #111827 0%, #1f2937 50%, #111827 100%); padding: 40px 32px 36px; color: white; }}
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
    .search-btn {{ background: var(--primary); color: white; border: none; padding: 13px 28px; font-family: 'Inter', sans-serif; font-size: 14px; font-weight: 600; cursor: pointer; white-space: nowrap; transition: background 0.15s; }}
    .search-btn:hover {{ background: #0f766e; }}

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
    .qf-tag:hover, .qf-tag.active {{ border-color: var(--primary); color: var(--primary); background: var(--primary-light); }}

    /* MAIN LAYOUT */
    .main {{ max-width: 1100px; margin: 0 auto; padding: 0 32px 60px; display: grid; grid-template-columns: 240px 1fr; gap: 24px; align-items: start; }}

    /* SIDEBAR */
    .sidebar {{ display: flex; flex-direction: column; gap: 16px; }}
    .filter-box {{ background: white; border: 1px solid var(--border); border-radius: 10px; padding: 14px; box-shadow: var(--shadow); }}
    .filter-box-title {{ font-size: 11px; font-weight: 600; color: var(--text-light); letter-spacing: 0.8px; text-transform: uppercase; margin-bottom: 10px; }}
    .filter-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 10px; }}
    .filter-grid .full {{ grid-column: 1 / -1; }}
    .filter-select {{ width: 100%; border: 1px solid var(--border); border-radius: 6px; padding: 7px 8px; font-family: 'Inter', sans-serif; font-size: 12px; color: var(--text); outline: none; background: white; }}
    .filter-select:focus {{ border-color: var(--primary); }}
    .chk-row {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
    .chk-label {{ display: flex; align-items: center; gap: 5px; cursor: pointer; font-size: 12px; color: var(--text); white-space: nowrap; }}
    .chk-label input {{ accent-color: var(--primary); width: 14px; height: 14px; }}
    .sidebar-search-btn {{ width: 100%; margin-top: 10px; background: var(--primary); color: white; border: none; padding: 9px; border-radius: 6px; font-family: 'Inter', sans-serif; font-size: 13px; font-weight: 600; cursor: pointer; transition: background 0.15s; }}
    .sidebar-search-btn:hover {{ background: #0f766e; }}

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
    .m-bar {{ height: 100%; background: var(--primary); border-radius: 2px; }}
    .m-delta {{ font-size: 11px; color: var(--green); font-weight: 600; min-width: 28px; text-align: right; }}

    .alerts-box {{ background: white; border: 1px solid var(--border); border-radius: 10px; padding: 16px; box-shadow: var(--shadow); margin-top: 12px; }}
    .sa-item {{ display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid var(--border); }}
    .sa-item:last-child {{ border-bottom: none; }}
    .sa-icon {{ font-size: 10px; font-weight: 700; width: 28px; text-align: center; flex-shrink: 0; }}
    .sa-name {{ flex: 1; font-size: 12px; color: var(--text); font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .sa-detail {{ font-size: 11px; color: var(--text-light); min-width: 60px; text-align: right; white-space: nowrap; }}

    /* JOBS AREA */
    .jobs-area {{ display: flex; flex-direction: column; gap: 12px; }}
    .jobs-header {{ display: flex; align-items: center; justify-content: space-between; padding: 4px 0; }}
    .jobs-count {{ font-size: 13px; color: var(--text-mid); }}
    .jobs-count strong {{ color: var(--text); font-weight: 600; }}
    .sort-wrap {{ display: flex; align-items: center; gap: 8px; }}
    .sort-label {{ font-size: 12px; color: var(--text-light); }}
    .sort-select {{ border: 1px solid var(--border); background: white; color: var(--text); font-family: 'Inter', sans-serif; font-size: 12px; padding: 5px 10px; border-radius: 6px; outline: none; cursor: pointer; }}

    /* ALERT BANNER */
    .alert-banner {{ background: var(--bg); border: 1px solid var(--border); border-radius: 10px; padding: 12px 16px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
    .alert-text {{ font-size: 13px; color: var(--text-mid); display: flex; align-items: center; gap: 8px; }}
    .btn-alert {{ background: var(--primary); color: white; border: none; padding: 7px 16px; border-radius: 6px; font-size: 12px; font-weight: 600; font-family: 'Inter', sans-serif; cursor: pointer; white-space: nowrap; transition: background 0.15s; flex-shrink: 0; }}
    .btn-alert:hover {{ background: #0f766e; }}

    /* ALERT MODAL */
    .alert-modal {{ position:fixed; top:0; left:0; right:0; bottom:0; z-index:1000; display:flex; align-items:center; justify-content:center; }}
    .alert-modal-backdrop {{ position:absolute; top:0; left:0; right:0; bottom:0; background:rgba(0,0,0,0.4); }}
    .alert-modal-card {{ position:relative; background:white; border-radius:12px; width:90%; max-width:440px; box-shadow:0 8px 30px rgba(0,0,0,0.15); overflow:hidden; }}
    .alert-modal-header {{ display:flex; align-items:center; justify-content:space-between; padding:16px 20px; border-bottom:1px solid var(--border); }}
    .alert-modal-title {{ font-size:16px; font-weight:700; color:var(--text); }}
    .alert-modal-close {{ background:none; border:none; font-size:22px; color:var(--text-light); cursor:pointer; padding:4px 8px; line-height:1; }}
    .alert-modal-close:hover {{ color:var(--text); }}
    .alert-modal-body {{ padding:20px; }}
    .alert-modal-desc {{ font-size:13px; color:var(--text-mid); margin-bottom:14px; }}
    .alert-filters-preview {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:16px; }}
    .alert-filter-pill {{ background:#f0fdfa; border:1px solid #99f6e4; border-radius:20px; padding:4px 12px; font-size:12px; color:#0d9488; }}
    .alert-input-row {{ display:flex; gap:8px; }}
    .alert-email-input {{ flex:1; padding:10px 14px; border:1px solid var(--border); border-radius:8px; font-size:14px; font-family:'Inter',sans-serif; outline:none; }}
    .alert-email-input:focus {{ border-color:var(--primary); box-shadow:0 0 0 3px rgba(13,148,136,0.1); }}
    .alert-submit-btn {{ background:var(--primary); color:white; border:none; padding:10px 20px; border-radius:8px; font-size:13px; font-weight:600; font-family:'Inter',sans-serif; cursor:pointer; white-space:nowrap; }}
    .alert-submit-btn:hover {{ background:#0f766e; }}
    .alert-submit-btn:disabled {{ opacity:0.6; cursor:not-allowed; }}
    .alert-msg {{ margin-top:12px; font-size:13px; padding:10px; border-radius:6px; }}
    .alert-msg.success {{ background:#f0fdf4; color:#166534; border:1px solid #bbf7d0; }}
    .alert-msg.error {{ background:#fef2f2; color:#991b1b; border:1px solid #fecaca; }}

    /* JOB CARD */
    .job-card {{ background: white; border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px; box-shadow: var(--shadow); cursor: default; transition: border-color 0.15s, box-shadow 0.15s; position: relative; }}
    .job-card:hover {{ border-color: var(--border); box-shadow: var(--shadow-md); }}
    .job-card.is-new {{ border-left: 3px solid var(--green); }}
    .job-date-label {{ position: absolute; top: 18px; right: 20px; font-size: 11px; color: var(--text-light); }}
    .company-row {{ display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }}
    .co-logo {{ width: 30px; height: 30px; border-radius: 6px; background: var(--tag-bg); border: 1px solid var(--border); display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 700; color: var(--text-mid); flex-shrink: 0; }}
    .co-name {{ font-size: 13px; color: var(--text-mid); font-weight: 500; }}
    .job-title-text {{ display: block; font-size: 16px; font-weight: 600; color: var(--text); letter-spacing: -0.2px; margin-bottom: 8px; line-height: 1.3; }}
    .job-title-text:hover {{ text-decoration: underline; }}
    .job-meta-row {{ display: flex; align-items: center; gap: 16px; margin-bottom: 10px; flex-wrap: wrap; }}
    .meta-pill {{ display: flex; align-items: center; gap: 4px; font-size: 12px; color: var(--text-mid); }}
    .meta-pill svg {{ width: 12px; height: 12px; color: var(--text-light); }}
    .job-snippet-text {{ font-size: 13px; color: var(--text-mid); line-height: 1.6; margin-bottom: 14px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
    .job-footer-row {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; }}
    .tags-left {{ display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }}
    .pill {{ font-size: 11px; padding: 3px 10px; border-radius: 100px; font-weight: 500; border: 1px solid; }}
    .pill-type {{ background: var(--tag-bg); color: var(--text-mid); border-color: var(--border); }}
    .pill-new {{ background: var(--green-light); color: var(--green); border-color: #a7f3d0; }}
    .pill-stale {{ background: #fef3c7; color: #92400e; border-color: #fde68a; }}
    .pill-source {{ background: var(--tag-bg); color: var(--text-light); border-color: var(--border); }}
    .pill-tech {{ background: #eff6ff; color: #2563eb; border-color: #bfdbfe; }}
    .actions-right {{ display: flex; align-items: center; gap: 8px; flex-shrink: 0; }}
    .btn-save {{ background: white; border: 1px solid var(--border); color: var(--text-mid); padding: 6px 14px; border-radius: 6px; font-size: 12px; font-family: 'Inter', sans-serif; font-weight: 500; cursor: pointer; transition: all 0.15s; }}
    .btn-save:hover {{ border-color: var(--primary); color: var(--primary); }}
    .btn-apply {{ display: inline-flex; align-items: center; gap: 4px; background: var(--primary); color: white; border: none; padding: 7px 18px; border-radius: 6px; font-size: 12px; font-weight: 600; font-family: 'Inter', sans-serif; cursor: pointer; transition: background 0.15s; }}
    .btn-apply:hover {{ background: #0f766e; }}

    /* PAGINATION */
    .pagination {{ display: flex; align-items: center; justify-content: center; gap: 4px; margin-top: 8px; }}
    .pg-btn {{ min-width: 34px; height: 34px; padding: 0 8px; display: inline-flex; align-items: center; justify-content: center; border-radius: 6px; font-size: 13px; cursor: pointer; background: white; border: 1px solid var(--border); color: var(--text-mid); font-family: 'Inter', sans-serif; font-weight: 500; transition: all 0.15s; }}
    .pg-btn.active {{ background: var(--primary); border-color: var(--primary); color: white; font-weight: 600; cursor: default; }}
    .pg-btn:hover:not(.active) {{ border-color: var(--primary); color: var(--primary); }}
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
    .mobile-nav a.active {{ color: var(--primary); }}
    .mobile-nav .btn-post-mobile {{
      background: var(--primary);
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
      .logo-by {{ font-size: 0; }} .logo-by svg {{ display: none; }}
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
      .filter-box, .momentum-box, .alerts-box {{ display: none; margin-bottom: 12px; }}
      .filter-box.mobile-open, .momentum-box.mobile-open, .alerts-box.mobile-open {{ display: block; }}

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

    /* ===== CHAT WIDGET ===== */
    .chat-widget {{
      position: fixed;
      bottom: 24px;
      right: 24px;
      z-index: 200;
      font-family: 'Inter', sans-serif;
    }}
    .chat-bubble {{
      height: 44px;
      padding: 0 18px;
      border-radius: 100px;
      background: var(--primary);
      border: none;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      box-shadow: 0 4px 16px rgba(13, 148, 136, 0.4);
      transition: transform 0.2s, box-shadow 0.2s;
      position: relative;
      color: white;
    }}
    .chat-bubble:hover {{
      transform: scale(1.05);
      box-shadow: 0 6px 24px rgba(13, 148, 136, 0.5);
    }}
    .chat-bubble-label {{
      font-size: 13px;
      font-weight: 600;
      font-family: 'Inter', sans-serif;
      white-space: nowrap;
    }}
    .chat-bubble-close {{ display: none; }}
    .chat-bubble.open .chat-bubble-icon {{ display: none; }}
    .chat-bubble.open .chat-bubble-label {{ display: none; }}
    .chat-bubble.open .chat-bubble-close {{ display: block; }}
    .chat-bubble.open {{
      width: 44px;
      padding: 0;
      border-radius: 50%;
    }}
    .chat-bubble::before {{
      content: '';
      position: absolute;
      inset: -4px;
      border-radius: 100px;
      background: var(--primary);
      opacity: 0;
      animation: chatPulse 3s ease-in-out infinite;
      z-index: -1;
    }}
    @keyframes chatPulse {{
      0%, 100% {{ opacity: 0; transform: scale(1); }}
      50% {{ opacity: 0.2; transform: scale(1.25); }}
    }}
    .chat-bubble.opened-once::before {{
      animation: none;
      opacity: 0;
    }}
    .chat-panel {{
      position: absolute;
      bottom: 68px;
      right: 0;
      width: 380px;
      max-height: 520px;
      background: var(--white);
      border-radius: 16px;
      border: 1px solid var(--border);
      box-shadow: 0 12px 40px rgba(0,0,0,0.12), 0 4px 12px rgba(0,0,0,0.06);
      display: flex;
      flex-direction: column;
      overflow: hidden;
      opacity: 0;
      transform: translateY(16px) scale(0.95);
      pointer-events: none;
      transition: opacity 0.25s ease, transform 0.25s ease;
    }}
    .chat-panel.open {{
      opacity: 1;
      transform: translateY(0) scale(1);
      pointer-events: auto;
    }}
    .chat-panel-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 14px 16px;
      border-bottom: 1px solid var(--border);
      background: var(--primary);
      color: white;
    }}
    .chat-panel-title {{
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 14px;
      font-weight: 600;
    }}
    .chat-panel-close-btn {{
      background: none;
      border: none;
      color: rgba(255,255,255,0.8);
      cursor: pointer;
      padding: 2px;
      display: flex;
      border-radius: 4px;
      transition: background 0.15s;
    }}
    .chat-panel-close-btn:hover {{
      background: rgba(255,255,255,0.15);
      color: white;
    }}
    .chat-messages {{
      flex: 1;
      overflow-y: auto;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      min-height: 200px;
      max-height: 340px;
    }}
    .chat-msg {{
      max-width: 88%;
      padding: 10px 14px;
      border-radius: 12px;
      font-size: 13px;
      line-height: 1.5;
      animation: chatMsgIn 0.25s ease;
    }}
    @keyframes chatMsgIn {{
      from {{ opacity: 0; transform: translateY(6px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    .chat-msg.bot {{
      background: var(--bg);
      color: var(--text);
      align-self: flex-start;
      border-bottom-left-radius: 4px;
    }}
    .chat-msg.user {{
      background: var(--primary);
      color: white;
      align-self: flex-end;
      border-bottom-right-radius: 4px;
    }}
    .chat-typing {{
      display: flex;
      gap: 4px;
      align-self: flex-start;
      padding: 12px 16px;
      background: var(--bg);
      border-radius: 12px;
      border-bottom-left-radius: 4px;
    }}
    .chat-typing span {{
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--text-light);
      animation: chatDot 1.2s ease-in-out infinite;
    }}
    .chat-typing span:nth-child(2) {{ animation-delay: 0.2s; }}
    .chat-typing span:nth-child(3) {{ animation-delay: 0.4s; }}
    @keyframes chatDot {{
      0%, 60%, 100% {{ opacity: 0.3; transform: translateY(0); }}
      30% {{ opacity: 1; transform: translateY(-4px); }}
    }}
    .chat-input {{
      padding: 12px 16px;
      border-top: 1px solid var(--border);
      background: var(--white);
      min-height: 20px;
    }}
    .chat-chips {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }}
    .chat-chip {{
      background: white;
      border: 1px solid var(--border);
      color: var(--text);
      padding: 7px 14px;
      border-radius: 100px;
      font-size: 12px;
      font-weight: 500;
      font-family: 'Inter', sans-serif;
      cursor: pointer;
      transition: all 0.15s;
      white-space: nowrap;
    }}
    .chat-chip:hover {{
      border-color: var(--primary);
      color: var(--primary);
      background: var(--primary-light);
    }}
    .chat-chip.selected {{
      background: var(--primary);
      color: white;
      border-color: var(--primary);
    }}
    .chat-chip-confirm {{
      background: var(--primary);
      color: white;
      border: none;
      padding: 8px 20px;
      border-radius: 100px;
      font-size: 12px;
      font-weight: 600;
      font-family: 'Inter', sans-serif;
      cursor: pointer;
      transition: background 0.15s;
      margin-top: 4px;
    }}
    .chat-chip-confirm:hover {{
      background: #0f766e;
    }}
    .chat-text-input {{
      width: 100%;
      box-sizing: border-box;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 9px 12px;
      font-family: 'Inter', sans-serif;
      font-size: 13px;
      color: var(--text);
      outline: none;
      margin-top: 6px;
    }}
    .chat-text-input:focus {{
      border-color: var(--primary);
    }}
    .chat-start-over {{
      display: inline-block;
      margin-top: 8px;
      font-size: 12px;
      color: var(--text-light);
      cursor: pointer;
      transition: color 0.15s;
    }}
    .chat-start-over:hover {{
      color: var(--primary);
    }}
    @media (max-width: 768px) {{
      .chat-widget {{
        bottom: 16px;
        right: 16px;
      }}
      .chat-bubble {{
        height: 40px;
        padding: 0 14px;
      }}
      .chat-bubble-label {{
        font-size: 12px;
      }}
      .chat-panel {{
        position: fixed;
        bottom: 0;
        left: 0;
        right: 0;
        width: 100%;
        max-height: 85vh;
        border-radius: 16px 16px 0 0;
      }}
      .chat-messages {{
        max-height: calc(85vh - 140px);
      }}
    }}
  </style>
</head>
<body>

<!-- TOPBAR -->
<div class="topbar">
  <div class="logo-area">
    <div class="logo">HireAssist</div>
    <div class="logo-by">by <a href="https://cubea.nl/" target="_blank" rel="noopener" style="display:inline-flex;align-items:center;gap:5px;color:inherit;text-decoration:none"><svg width="20" height="20" viewBox="0 0 70 70" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M35 8 L62 22 L35 36 L8 22 Z" fill="#99f6e4"/><path d="M8 22 L35 36 L35 64 L8 50 Z" fill="#0d9488"/><path d="M35 36 L62 22 L62 50 L35 64 Z" fill="#14b8a6"/></svg> <span class="cubea-text">Cube <span style="color:#0d9488;font-weight:800">A</span></span></a></div>
  </div>
  <div class="nav-right">
    <a href="/ui" class="active">Jobs</a>
    <a href="/ui/candidates">Candidates</a>
    <a href="/ui/momentum">Momentum</a>
    <a href="/ui/report">Report</a>
    <a href="#" style="color:var(--text-light);cursor:default">For Employers</a>
    <a href="#" class="btn-post">Post a Job</a>
  </div>
  <button class="mobile-menu-btn" onclick="document.getElementById('mobileNav').classList.toggle('open')" aria-label="Menu">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
  </button>
</div>

<div class="mobile-nav" id="mobileNav">
  <a href="/ui" class="active">Jobs</a>
  <a href="/ui/candidates">Candidates</a>
  <a href="/ui/momentum">Momentum</a>
  <a href="/ui/report">Report</a>
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
        <div class="stat-num">{filtered_company_count:,}</div>
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

          <select id="provinceSelect" class="filter-select" onchange="onProvinceChange(this)">
            <option value="">All provinces</option>
            {province_options}
          </select>

          <select name="city" id="citySelect" class="filter-select" onchange="onCityChange(this)">
            <option value="">All cities</option>
            {city_options}
          </select>

          <select name="country" class="filter-select" onchange="this.form.submit()">
            <option value="Netherlands" {"selected" if country == "Netherlands" else ""}>Netherlands</option>
            <option value="" {"selected" if not country else ""}>All countries</option>
            {country_options}
          </select>

          <select name="tech" class="filter-select full" onchange="this.form.submit()">
            <option value="">All tech stacks</option>
            {tech_options}
          </select>
        </div>
        <div class="chk-row">
          <label class="chk-label"><input type="checkbox" name="english_only" value="true" {"checked" if english_only else ""} onchange="this.form.submit()"> English only</label>
          <label class="chk-label"><input type="checkbox" name="new_today_only" value="true" {"checked" if new_today_only else ""} onchange="this.form.submit()"> New today</label>
          <label class="chk-label"><input type="checkbox" name="hide_stale" value="true" {"checked" if hide_stale else ""} onchange="this.form.submit()"> Hide stale (60+ days)</label>
        </div>
        <button type="submit" class="sidebar-search-btn">Search</button>
      </div>
    </form>

    {momentum_html}
    {sidebar_alerts_html}
  </div>

  <!-- JOBS -->
  <div class="jobs-area">

    <div class="alert-banner">
      <div class="alert-text">
        <span>Get notified when new hidden jobs match your search</span>
      </div>
      <button class="btn-alert" id="btnSetAlert">Set Job Alert</button>
    </div>

    <div class="jobs-header">
      <div class="jobs-count">Showing <strong>{total_real:,} jobs</strong> from <strong>{filtered_company_count} companies</strong> (page {page}/{total_pages})</div>
      <div class="sort-wrap">
        <span class="sort-label">Sort by:</span>
        <select class="sort-select" onchange="window.location=this.value">
          <option value="/ui?{sort_base_params}&amp;sort=newest" {"selected" if sort == "newest" else ""}>Newest first</option>
          <option value="/ui?{sort_base_params}&amp;sort=company" {"selected" if sort == "company" else ""}>Company name</option>
        </select>
      </div>
    </div>

    {cards_html}
    {pagination_html}

  </div>
</div>

<!-- CHAT WIDGET -->
<div id="chatWidget" class="chat-widget">
  <button id="chatBubble" class="chat-bubble" aria-label="Job search assistant">
    <svg class="chat-bubble-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
      <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
    <span class="chat-bubble-label">Find jobs</span>
    <svg class="chat-bubble-close" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
    </svg>
  </button>
  <div id="chatPanel" class="chat-panel">
    <div class="chat-panel-header">
      <div class="chat-panel-title">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        Find Your Next Job
      </div>
      <button id="chatPanelClose" class="chat-panel-close-btn" aria-label="Close">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
        </svg>
      </button>
    </div>
    <div id="chatMessages" class="chat-messages"></div>
    <div id="chatInput" class="chat-input"></div>
  </div>
</div>

<script>
(function() {{
  var bubble = document.getElementById('chatBubble');
  var panel = document.getElementById('chatPanel');
  var panelClose = document.getElementById('chatPanelClose');
  var messagesEl = document.getElementById('chatMessages');
  var inputEl = document.getElementById('chatInput');

  var answers = {{ role: '', city: '', tech: [], lang: '' }};
  var currentStep = 0;
  var isOpen = false;

  var steps = [
    {{
      question: "Hi! I can help you find the right job. What kind of role are you looking for?",
      type: 'single',
      chips: [
        {{ label: 'Software Engineer', value: 'Software Engineer' }},
        {{ label: 'Data / AI', value: 'Data' }},
        {{ label: 'DevOps / Cloud', value: 'DevOps' }},
        {{ label: 'Design / UX', value: 'Design' }},
        {{ label: 'Product / PM', value: 'Product' }},
        {{ label: 'Other...', value: '__other__' }}
      ],
      onAnswer: function(val) {{ answers.role = val; }}
    }},
    {{
      question: "Great choice! Where would you like to work?",
      type: 'single',
      chips: [
        {{ label: 'Eindhoven', value: 'Eindhoven' }},
        {{ label: 'Amsterdam', value: 'Amsterdam' }},
        {{ label: 'Rotterdam', value: 'Rotterdam' }},
        {{ label: 'Utrecht', value: 'Utrecht' }},
        {{ label: 'Remote', value: '' }},
        {{ label: 'Anywhere in NL', value: '' }}
      ],
      onAnswer: function(val) {{ answers.city = val; }}
    }},
    {{
      question: "Any preferred tech stack? Pick as many as you like.",
      type: 'multi',
      chips: [
        {{ label: 'Python', value: 'Python' }},
        {{ label: 'Java', value: 'Java' }},
        {{ label: 'JavaScript / React', value: 'React' }},
        {{ label: '.NET / C#', value: 'C#' }},
        {{ label: 'Go / Rust', value: 'Go' }},
        {{ label: "Don't care", value: '' }}
      ],
      onAnswer: function(val) {{
        answers.tech = val.filter(function(v) {{ return v !== ''; }});
      }}
    }},
    {{
      question: "Last one -- what language should the job postings be in?",
      type: 'single',
      chips: [
        {{ label: 'English', value: 'en' }},
        {{ label: 'Dutch', value: 'nl' }},
        {{ label: 'Show all', value: '' }}
      ],
      onAnswer: function(val) {{ answers.lang = val; }}
    }}
  ];

  function togglePanel() {{
    isOpen = !isOpen;
    panel.classList.toggle('open', isOpen);
    bubble.classList.toggle('open', isOpen);
    if (isOpen) {{
      bubble.classList.add('opened-once');
      if (currentStep === 0 && messagesEl.children.length === 0) {{
        startConversation();
      }}
    }}
    if (window.innerWidth <= 768) {{
      document.body.style.overflow = isOpen ? 'hidden' : '';
    }}
  }}

  bubble.addEventListener('click', togglePanel);
  panelClose.addEventListener('click', togglePanel);

  function addMessage(text, sender) {{
    var div = document.createElement('div');
    div.className = 'chat-msg ' + sender;
    div.textContent = text;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }}

  function showTyping() {{
    var div = document.createElement('div');
    div.className = 'chat-typing';
    div.id = 'chatTypingIndicator';
    div.innerHTML = '<span></span><span></span><span></span>';
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }}

  function hideTyping() {{
    var el = document.getElementById('chatTypingIndicator');
    if (el) el.remove();
  }}

  function clearInput() {{
    inputEl.innerHTML = '';
  }}

  function renderChips(step) {{
    clearInput();
    var wrapper = document.createElement('div');
    wrapper.className = 'chat-chips';

    if (step.type === 'single') {{
      step.chips.forEach(function(chip) {{
        var btn = document.createElement('button');
        btn.className = 'chat-chip';
        btn.textContent = chip.label;
        btn.addEventListener('click', function() {{
          if (chip.value === '__other__') {{
            showOtherInput(step);
            return;
          }}
          addMessage(chip.label, 'user');
          step.onAnswer(chip.value);
          clearInput();
          advanceStep();
        }});
        wrapper.appendChild(btn);
      }});
    }}

    if (step.type === 'multi') {{
      var selected = [];
      step.chips.forEach(function(chip) {{
        var btn = document.createElement('button');
        btn.className = 'chat-chip';
        btn.textContent = chip.label;
        btn.addEventListener('click', function() {{
          if (chip.value === '') {{
            addMessage(chip.label, 'user');
            step.onAnswer([]);
            clearInput();
            advanceStep();
            return;
          }}
          var idx = selected.indexOf(chip.value);
          if (idx > -1) {{
            selected.splice(idx, 1);
            btn.classList.remove('selected');
          }} else {{
            selected.push(chip.value);
            btn.classList.add('selected');
          }}
          var existing = inputEl.querySelector('.chat-chip-confirm');
          if (selected.length > 0 && !existing) {{
            var confirm = document.createElement('button');
            confirm.className = 'chat-chip-confirm';
            confirm.textContent = 'Continue';
            confirm.addEventListener('click', function() {{
              addMessage(selected.join(', '), 'user');
              step.onAnswer(selected.slice());
              clearInput();
              advanceStep();
            }});
            inputEl.appendChild(confirm);
          }} else if (selected.length === 0 && existing) {{
            existing.remove();
          }}
        }});
        wrapper.appendChild(btn);
      }});
    }}

    inputEl.insertBefore(wrapper, inputEl.firstChild);
  }}

  function showOtherInput(step) {{
    clearInput();
    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'chat-text-input';
    input.placeholder = 'Type the role you are looking for...';
    input.addEventListener('keydown', function(e) {{
      if (e.key === 'Enter' && input.value.trim()) {{
        var val = input.value.trim();
        addMessage(val, 'user');
        step.onAnswer(val);
        clearInput();
        advanceStep();
      }}
    }});
    inputEl.appendChild(input);

    var submitBtn = document.createElement('button');
    submitBtn.className = 'chat-chip-confirm';
    submitBtn.textContent = 'Continue';
    submitBtn.addEventListener('click', function() {{
      if (input.value.trim()) {{
        var val = input.value.trim();
        addMessage(val, 'user');
        step.onAnswer(val);
        clearInput();
        advanceStep();
      }}
    }});
    inputEl.appendChild(submitBtn);
    input.focus();
  }}

  var techRoles = ['Software Engineer', 'Data', 'DevOps'];

  function advanceStep() {{
    currentStep++;
    // Skip tech stack step (index 2) for non-technical roles
    if (currentStep === 2 && techRoles.indexOf(answers.role) === -1) {{
      currentStep++;
    }}
    if (currentStep >= steps.length) {{
      showTyping();
      setTimeout(function() {{
        hideTyping();
        showResults();
      }}, 800);
    }} else {{
      showTyping();
      setTimeout(function() {{
        hideTyping();
        addMessage(steps[currentStep].question, 'bot');
        renderChips(steps[currentStep]);
      }}, 600);
    }}
  }}

  function showResults() {{
    var params = [];
    if (answers.role) {{
      params.push('q=' + encodeURIComponent(answers.role));
    }}
    if (answers.city) {{
      params.push('city=' + encodeURIComponent(answers.city));
    }}
    params.push('country=Netherlands');
    if (answers.lang) {{
      params.push('lang=' + encodeURIComponent(answers.lang));
    }}
    if (answers.tech && answers.tech.length > 0) {{
      params.push('tech=' + encodeURIComponent(answers.tech[0]));
    }}
    var url = '/ui' + (params.length > 0 ? '?' + params.join('&') : '');

    addMessage("Here are your personalized results. Taking you there now!", 'bot');
    clearInput();

    var startOver = document.createElement('span');
    startOver.className = 'chat-start-over';
    startOver.textContent = 'Start over';
    startOver.addEventListener('click', resetChat);
    inputEl.appendChild(startOver);

    setTimeout(function() {{
      window.location.href = url;
    }}, 1200);
  }}

  function startConversation() {{
    currentStep = 0;
    answers = {{ role: '', city: '', tech: [], englishOnly: false }};
    messagesEl.innerHTML = '';
    clearInput();
    showTyping();
    setTimeout(function() {{
      hideTyping();
      addMessage(steps[0].question, 'bot');
      renderChips(steps[0]);
    }}, 500);
  }}

  function resetChat() {{
    startConversation();
  }}
}})();
</script>

<script>
document.getElementById('mobileFilterToggle').addEventListener('click', function() {{
  document.querySelectorAll('.filter-box,.momentum-box,.alerts-box').forEach(function(el) {{
    el.classList.toggle('mobile-open');
  }});
}});
</script>

<script>
// Province -> City cascade filter
var provinceCitiesMap = {province_cities_js};
var allCityOptions = [];
(function() {{
  var citySelect = document.getElementById('citySelect');
  if (!citySelect) return;
  var opts = citySelect.querySelectorAll('option');
  for (var i = 1; i < opts.length; i++) {{
    allCityOptions.push(opts[i].cloneNode(true));
  }}
  // On-load: if province selected, filter city dropdown
  var provSelect = document.getElementById('provinceSelect');
  if (provSelect && provSelect.value && provSelect.value.indexOf('province:') === 0) {{
    var provName = provSelect.value.substring(9);
    var cities = provinceCitiesMap[provName] || [];
    var currentCity = citySelect.value;
    citySelect.innerHTML = '<option value="">All cities</option>';
    cities.forEach(function(c) {{
      var o = document.createElement('option');
      o.value = c;
      o.textContent = c;
      if (c === currentCity) o.selected = true;
      citySelect.appendChild(o);
    }});
  }}
}})();

function onProvinceChange(sel) {{
  var citySelect = document.getElementById('citySelect');
  var prov = sel.value;
  citySelect.innerHTML = '<option value="">All cities</option>';
  if (prov && prov.indexOf('province:') === 0) {{
    var provName = prov.substring(9);
    var cities = provinceCitiesMap[provName] || [];
    cities.forEach(function(c) {{
      var o = document.createElement('option');
      o.value = c;
      o.textContent = c;
      citySelect.appendChild(o);
    }});
    // Submit with province filter
    citySelect.value = '';
    // Set city to province:X for backend
    var provOpt = document.createElement('option');
    provOpt.value = prov;
    provOpt.textContent = 'All ' + provName;
    provOpt.selected = true;
    citySelect.insertBefore(provOpt, citySelect.options[1]);
    citySelect.value = prov;
  }} else {{
    allCityOptions.forEach(function(o) {{
      citySelect.appendChild(o.cloneNode(true));
    }});
  }}
  citySelect.form.submit();
}}

function onCityChange(sel) {{
  var city = sel.value;
  if (city && city.indexOf('province:') !== 0) {{
    var provSelect = document.getElementById('provinceSelect');
    if (provSelect) {{
      for (var prov in provinceCitiesMap) {{
        var found = provinceCitiesMap[prov].some(function(c) {{ return c.toLowerCase() === city.toLowerCase(); }});
        if (found) {{
          provSelect.value = 'province:' + prov;
          break;
        }}
      }}
    }}
  }}
  sel.form.submit();
}}
</script>

<!-- ALERT MODAL -->
<div id="alertModal" class="alert-modal" style="display:none">
  <div class="alert-modal-backdrop"></div>
  <div class="alert-modal-card">
    <div class="alert-modal-header">
      <div class="alert-modal-title">Set Job Alert</div>
      <button class="alert-modal-close" aria-label="Close">&times;</button>
    </div>
    <div class="alert-modal-body">
      <p class="alert-modal-desc">Get a daily email when new jobs match your current filters.</p>
      <div id="alertFiltersPreview" class="alert-filters-preview"></div>
      <div class="alert-input-row">
        <input type="email" id="alertEmail" class="alert-email-input" placeholder="your@email.com">
        <button id="alertSubmitBtn" class="alert-submit-btn">Subscribe</button>
      </div>
      <div id="alertMessage" class="alert-msg" style="display:none"></div>
    </div>
  </div>
</div>

<script>
(function() {{
  var modal = document.getElementById('alertModal');
  var emailInput = document.getElementById('alertEmail');
  var submitBtn = document.getElementById('alertSubmitBtn');
  var msgDiv = document.getElementById('alertMessage');
  var filtersPreview = document.getElementById('alertFiltersPreview');

  function getCurrentFilters() {{
    var f = {{}};
    var el;
    el = document.querySelector('input[name="q"]'); if (el) f.q = el.value || '';
    el = document.querySelector('select[name="company"]'); if (el) f.company = el.value || '';
    el = document.getElementById('citySelect'); if (el) f.city = el.value || '';
    el = document.querySelector('select[name="country"]'); if (el) f.country = el.value || '';
    el = document.querySelector('select[name="tech"]'); if (el) f.tech = el.value || '';
    el = document.querySelector('select[name="lang"]'); if (el) f.lang = el.value || '';
    el = document.querySelector('input[name="english_only"]'); if (el) f.english_only = el.checked;
    el = document.querySelector('input[name="hide_stale"]'); if (el) f.hide_stale = el.checked;
    return f;
  }}

  function renderPills(filters) {{
    filtersPreview.innerHTML = '';
    var labels = {{q:'Keyword', company:'Company', city:'City', country:'Country', tech:'Tech', lang:'Language', english_only:'English only', hide_stale:'Hide stale'}};
    for (var k in filters) {{
      var v = filters[k];
      if (!v || v === '' || v === false) continue;
      if (k === 'country' && v.toLowerCase() === 'netherlands') continue;
      var text = labels[k] || k;
      if (typeof v === 'string' && v) text = text + ': ' + v;
      var pill = document.createElement('span');
      pill.className = 'alert-filter-pill';
      pill.textContent = text;
      filtersPreview.appendChild(pill);
    }}
    if (!filtersPreview.children.length) {{
      var pill = document.createElement('span');
      pill.className = 'alert-filter-pill';
      pill.textContent = 'All jobs in Netherlands';
      filtersPreview.appendChild(pill);
    }}
  }}

  document.getElementById('btnSetAlert').addEventListener('click', function(e) {{
    e.preventDefault();
    renderPills(getCurrentFilters());
    modal.style.display = 'flex';
    msgDiv.style.display = 'none';
    submitBtn.disabled = false;
    submitBtn.textContent = 'Subscribe';
    emailInput.value = '';
    emailInput.focus();
  }});

  modal.querySelector('.alert-modal-close').addEventListener('click', function() {{ modal.style.display = 'none'; }});
  modal.querySelector('.alert-modal-backdrop').addEventListener('click', function() {{ modal.style.display = 'none'; }});

  emailInput.addEventListener('keydown', function(e) {{
    if (e.key === 'Enter') {{ e.preventDefault(); submitBtn.click(); }}
  }});

  submitBtn.addEventListener('click', function() {{
    var email = emailInput.value.trim();
    if (!email || email.indexOf('@') < 1) {{
      showMsg('Please enter a valid email address.', 'error');
      return;
    }}
    submitBtn.disabled = true;
    submitBtn.textContent = 'Saving...';

    var filters = getCurrentFilters();
    var fd = new FormData();
    fd.append('email', email);
    fd.append('filters_json', JSON.stringify(filters));

    fetch('/api/alerts', {{ method: 'POST', body: fd }})
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        if (data.ok) {{
          showMsg('Check your inbox at ' + email + ' to confirm your alert.', 'success');
          emailInput.value = '';
        }} else {{
          showMsg(data.message || 'Something went wrong.', 'error');
        }}
      }})
      .catch(function() {{
        showMsg('Network error. Please try again.', 'error');
      }})
      .finally(function() {{
        submitBtn.disabled = false;
        submitBtn.textContent = 'Subscribe';
      }});
  }});

  function showMsg(text, type) {{
    msgDiv.textContent = text;
    msgDiv.className = 'alert-msg ' + type;
    msgDiv.style.display = 'block';
  }}
}})();
</script>
</body>
</html>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Self-ping scheduler -- keeps cloud deploys warm by hitting own public URL
# ---------------------------------------------------------------------------
def _start_keep_alive():
    """Start a background job that pings the app's public URL every 5 minutes."""
    public_url = (os.environ.get("RENDER_URL")
                  or os.environ.get("RAILWAY_PUBLIC_DOMAIN"))
    if not public_url:
        return
    # RAILWAY_PUBLIC_DOMAIN is just the hostname, not a full URL
    if not public_url.startswith("http"):
        public_url = f"https://{public_url}"
    public_url = public_url.rstrip("/")
    from apscheduler.schedulers.background import BackgroundScheduler

    def _self_ping():
        try:
            r = requests.get(f"{public_url}/ping", timeout=10)
            logger.info("Keep-alive ping: %s %s", r.status_code, r.json())
        except Exception as e:
            logger.warning("Keep-alive ping failed: %s", e)

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(_self_ping, "interval", minutes=5)
    scheduler.start()
    logger.info("Keep-alive scheduler started (every 5 min -> %s/ping)", public_url)


_start_keep_alive()
