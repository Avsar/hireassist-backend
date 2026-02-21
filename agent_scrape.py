#!/usr/bin/env python3
"""
agent_scrape.py -- Playwright-based career page scraper for Hire Assist

Renders JavaScript-heavy career pages (React / Next.js / Framer) using headless
Chromium, extracts job listings using JSON-LD and HTML heuristics, and stores
results in the scraped_jobs table for app.py to read.

Supports Workday portals (network interception), global portal detection
(Greenhouse, Lever, SmartRecruiters, iCIMS, Taleo redirects), enhanced JSON-LD
parsing, and safe per-company job replacement.

Usage:
    python agent_scrape.py                              # scrape all careers_page companies
    python agent_scrape.py --dry-run                    # print results without writing to DB
    python agent_scrape.py --company "Mollie"           # scrape a single company
    python agent_scrape.py --debug-company "Vanderlande"  # detailed debug for one company

Via Docker:
    docker compose --profile scrape run --rm scrape
    docker compose --profile scrape run --rm scrape python agent_scrape.py --company "Mollie"
"""
import argparse
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page, Response
from db_config import get_db_path

# Intelligence layer (optional -- degrades gracefully if not present)
try:
    from job_intel import ensure_intel_tables, upsert_jobs
    _HAS_INTEL = True
except ImportError:
    _HAS_INTEL = False

# Load .env for API keys
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Anthropic API for AI fallback scraper (Tier 2)
_ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

DB_FILE = get_db_path()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Domains of known ATS iframe embeds
_ATS_IFRAME_DOMAINS = [
    "boards.greenhouse.io", "jobs.lever.co", "jobs.smartrecruiters.com",
    "recruitee.com", "myworkdayjobs.com", "jobs.ashbyhq.com",
]

# Domains of known ATS portals (for redirect detection)
_ATS_PORTAL_DOMAINS = {
    "greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "smartrecruiters.com": "smartrecruiters",
    "myworkdayjobs.com": "workday",
    "icims.com": "icims",
    "taleo.net": "taleo",
    "recruitee.com": "recruitee",
    "ashbyhq.com": "ashby",
    "workday.com": "workday",
}

# Workday detection patterns
_WORKDAY_URL_RE = re.compile(r'myworkdayjobs\.com|workday\.com/.*?/jobs', re.IGNORECASE)
_WORKDAY_HTML_MARKERS = ["myworkdayjobs.com", "jobPostingInfo", "wd-Application"]

# Playwright selectors for "view all jobs" links
_JOB_NAV_SELECTORS = [
    "a:text-matches('view.*(jobs|positions|openings|roles)', 'i')",
    "a:text-matches('see.*(jobs|positions|openings|roles)', 'i')",
    "a:text-matches('find.*job', 'i')",
    "a:text-matches('go to job', 'i')",
    "a:text-matches('open positions', 'i')",
    "a:text-matches('current openings', 'i')",
    "a:text-matches('browse.*jobs', 'i')",
    "a:text-matches('all (open )?jobs', 'i')",
    "a:text-matches('job search', 'i')",
    "a:text-matches('explore.*roles', 'i')",
    "a:text-matches('search.*jobs', 'i')",
]

# href substrings that suggest a job listing sub-page
_JOB_HREF_KEYWORDS = [
    "/jobs", "/openings", "/positions", "/vacatures", "/vacancies",
    "/open-positions", "/find-your-job", "/career-opportunities",
    "/opportunities",
]

# Playwright selectors for load-more buttons
_LOAD_MORE_SELECTORS = [
    "button:text-matches('load more', 'i')",
    "button:text-matches('show more', 'i')",
    "button:text-matches('show all', 'i')",
    "button:text-matches('view more', 'i')",
    "button:text-matches('more jobs', 'i')",
    "button:text-matches('more positions', 'i')",
    "a:text-matches('load more', 'i')",
    "a:text-matches('show more', 'i')",
    "a:text-matches('show all', 'i')",
    "a:text-matches('view more', 'i')",
]

_JOB_URL_RE = re.compile(
    r'/(jobs?|careers?|career-opportunities|positions?|openings?|opportunities|vacatures?|vacancies|rollen?|roles?|apply|solliciteer)/[\w%.\-]{3,}',
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

_CONTENT_SEGMENTS = {
    "insights", "blog", "news", "stories", "story", "people",
    "tech", "learn", "teams", "team", "hiring-101",
}


# ---------------------------------------------------------------------------
# Portal detection helpers
# ---------------------------------------------------------------------------

def _detect_ats_domain(url: str) -> str | None:
    """Return ATS name if url belongs to a known ATS domain, else None."""
    netloc = urlparse(url).netloc.lower()
    for domain, name in _ATS_PORTAL_DOMAINS.items():
        if domain in netloc:
            return name
    return None


_UPGRADABLE_ATS = {"greenhouse", "lever", "smartrecruiters", "recruitee"}


def _extract_ats_token(ats_name: str, url: str) -> str | None:
    """Extract the ATS board token from a portal redirect URL."""
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]

    if ats_name == "greenhouse" and "greenhouse.io" in netloc:
        # boards.greenhouse.io/{token} or boards.greenhouse.io/{token}/jobs/...
        return path_parts[0] if path_parts else None

    if ats_name == "lever" and "lever.co" in netloc:
        # jobs.lever.co/{token} or jobs.eu.lever.co/{token}
        return path_parts[0] if path_parts else None

    if ats_name == "smartrecruiters" and "smartrecruiters.com" in netloc:
        # jobs.smartrecruiters.com/{token}
        return path_parts[0] if path_parts else None

    if ats_name == "recruitee" and "recruitee.com" in netloc:
        # {token}.recruitee.com/o or {token}.recruitee.com
        subdomain = netloc.split(".recruitee.com")[0]
        return subdomain if subdomain and subdomain != "www" else None

    return None


def _upgrade_company_source(conn: sqlite3.Connection, company_name: str,
                            old_token: str, new_source: str, new_token: str):
    """Upgrade a careers_page company to a proper ATS source in the DB."""
    now = datetime.now(timezone.utc).isoformat()
    # Check if the new source+token already exists (avoid UNIQUE constraint violation)
    existing = conn.execute(
        "SELECT id FROM companies WHERE source=? AND token=?",
        (new_source, new_token),
    ).fetchone()
    if existing:
        # ATS entry already exists -- just deactivate the old careers_page row
        conn.execute(
            "UPDATE companies SET active=0 WHERE source='careers_page' AND token=?",
            (old_token,),
        )
    else:
        # Update in-place: switch source and token
        conn.execute(
            """UPDATE companies SET source=?, token=?, last_verified_at=?
               WHERE name=? AND source='careers_page' AND token=?""",
            (new_source, new_token, now, company_name, old_token),
        )
    conn.commit()
    print(f"    >> UPGRADED: {company_name}: careers_page -> {new_source} (token: {new_token})")


def _is_workday_page(url: str, html: str) -> bool:
    """Check if a page is a Workday career portal."""
    if _WORKDAY_URL_RE.search(url):
        return True
    html_lower = html.lower() if html else ""
    return any(marker.lower() in html_lower for marker in _WORKDAY_HTML_MARKERS)


def _find_workday_url(html: str) -> str | None:
    """Extract a Workday portal URL from page HTML (e.g. linked from a WordPress careers site)."""
    m = re.search(r'href=["\']([^"\']*myworkdayjobs\.com[^"\']*)["\']', html, re.IGNORECASE)
    if m:
        url = m.group(1)
        # Strip login suffixes -- we want the job listing root
        url = re.sub(r'/login\s*$', '', url)
        return url
    return None


# ---------------------------------------------------------------------------
# Workday scraper (Feature 1)
# ---------------------------------------------------------------------------

def scrape_workday_jobs(page: Page, base_url: str, debug: bool = False) -> list[dict]:
    """Scrape jobs from a Workday career portal via network interception.

    Workday SPAs fetch job data from /wday/cxs/.../jobs endpoints as JSON.
    We intercept these responses and parse the structured data directly.
    """
    intercepted_jobs: list[dict] = []
    api_endpoints: list[str] = []

    def _on_response(response: Response):
        try:
            url = response.url
            # Workday job API endpoints contain /wday/cxs/ and return JSON
            if "/wday/cxs/" not in url:
                return
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return
            api_endpoints.append(url)
            body = response.json()
            _extract_workday_json(body, base_url, intercepted_jobs)
        except Exception:
            pass

    page.on("response", _on_response)

    try:
        page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        # If no jobs intercepted yet, try clicking "Search" or scrolling to trigger API
        if not intercepted_jobs:
            for selector in [
                "button:text-matches('search', 'i')",
                "button:text-matches('view all', 'i')",
                "button[data-automation-id='jobSearchButton']",
            ]:
                try:
                    btn = page.locator(selector).first
                    if btn.count() > 0 and btn.is_visible(timeout=2000):
                        btn.click(timeout=3000)
                        page.wait_for_timeout(4000)
                        break
                except Exception:
                    pass

        # Try paginating (Workday uses "Show More" or infinite scroll)
        for _ in range(3):
            if not intercepted_jobs:
                break
            try:
                more_btn = page.locator("button[data-automation-id='loadMoreButton'], "
                                        "button:text-matches('show more', 'i')").first
                if more_btn.count() > 0 and more_btn.is_visible(timeout=1000):
                    more_btn.click(timeout=3000)
                    page.wait_for_timeout(3000)
                else:
                    break
            except Exception:
                break

    except Exception as e:
        if debug:
            print(f"    [workday] Navigation error: {e}")
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

    if debug:
        print(f"    [workday] API endpoints intercepted: {len(api_endpoints)}")
        for ep in api_endpoints[:5]:
            print(f"      {ep[:120]}")
        print(f"    [workday] Jobs extracted: {len(intercepted_jobs)}")

    return intercepted_jobs


def _extract_workday_json(data: dict, base_url: str, out: list[dict]):
    """Parse a Workday JSON API response and append jobs to out."""
    # Workday responses have jobPostings at various paths
    postings = []

    if isinstance(data, dict):
        # Standard path: data.jobPostings[]
        postings = data.get("jobPostings", [])
        if not postings:
            # Alternative: data.body.children[].children[]... with jobPostingInfo
            postings = data.get("body", {}).get("jobPostings", [])
        if not postings:
            # Search result format: data.body.dataEntities[]
            postings = _find_nested_key(data, "listItems") or []
        if not postings:
            postings = _find_nested_key(data, "jobPostings") or []

    base_parsed = urlparse(base_url)
    base_origin = f"{base_parsed.scheme}://{base_parsed.netloc}"

    seen_titles = {j["title"] for j in out}
    for posting in postings:
        if not isinstance(posting, dict):
            continue
        # Workday formats: direct fields or nested under bulletFields/title
        title = (
            posting.get("title", "")
            or posting.get("bulletFields", [None])[0]
            or ""
        ).strip()
        if not title or len(title) < 5 or title in seen_titles:
            continue
        seen_titles.add(title)

        # Location
        loc_parts = posting.get("locationsText", "") or posting.get("bulletFields", ["", ""])[1] if len(posting.get("bulletFields", [])) > 1 else ""
        if isinstance(loc_parts, list):
            loc_parts = ", ".join(str(p) for p in loc_parts if p)
        location_raw = str(loc_parts).strip() if loc_parts else ""

        # URL
        external_path = posting.get("externalPath", "")
        if external_path:
            apply_url = f"{base_origin}{external_path}" if external_path.startswith("/") else external_path
        else:
            apply_url = base_url

        out.append({
            "title": title,
            "location_raw": location_raw,
            "apply_url": apply_url,
        })


def _find_nested_key(data, key, depth=0):
    """Recursively find a key in nested dicts/lists (max depth 5)."""
    if depth > 5:
        return None
    if isinstance(data, dict):
        if key in data and isinstance(data[key], list) and data[key]:
            return data[key]
        for v in data.values():
            result = _find_nested_key(v, key, depth + 1)
            if result:
                return result
    elif isinstance(data, list):
        for item in data[:10]:  # limit breadth
            result = _find_nested_key(item, key, depth + 1)
            if result:
                return result
    return None


# ---------------------------------------------------------------------------
# JSON-LD parser (Feature 3 -- enhanced)
# ---------------------------------------------------------------------------

def _parse_jsonld(soup: BeautifulSoup, url: str) -> list[dict]:
    """Parse ALL JSON-LD blocks, flatten lists and @graph, normalize locations."""
    jobs = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = (script.string or "").strip()
            if not raw:
                continue
            data = json.loads(raw)

            # Flatten: could be a single object, a list, or contain @graph
            items = []
            if isinstance(data, list):
                items.extend(data)
            elif isinstance(data, dict):
                if "@graph" in data and isinstance(data["@graph"], list):
                    items.extend(data["@graph"])
                else:
                    items.append(data)

            for item in items:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("@type", "")
                # Handle both "JobPosting" and ["JobPosting", ...]
                if isinstance(item_type, list):
                    if "JobPosting" not in item_type:
                        continue
                elif item_type != "JobPosting":
                    continue

                title = (item.get("title") or item.get("name") or "").strip()
                if not title:
                    continue

                location_raw = _normalize_jsonld_location(item)
                apply_url = item.get("url") or item.get("sameAs") or url
                jobs.append({"title": title, "location_raw": location_raw, "apply_url": apply_url})
        except Exception:
            pass
    return jobs


def _normalize_jsonld_location(item: dict) -> str:
    """Extract and normalize location from a JSON-LD JobPosting item."""
    loc = item.get("jobLocation")
    if not loc:
        return ""

    locations = loc if isinstance(loc, list) else [loc]
    parts = []
    for loc_item in locations:
        if isinstance(loc_item, str):
            parts.append(loc_item)
            continue
        if not isinstance(loc_item, dict):
            continue
        addr = loc_item.get("address", loc_item)
        if isinstance(addr, str):
            parts.append(addr)
            continue
        if isinstance(addr, dict):
            city = addr.get("addressLocality", "")
            region = addr.get("addressRegion", "")
            country = addr.get("addressCountry", "")
            # addressCountry can be a dict with "name"
            if isinstance(country, dict):
                country = country.get("name", "")
            loc_str = ", ".join(p for p in [city, region, country] if p)
            if loc_str:
                parts.append(loc_str)

    return " | ".join(parts) if len(parts) > 1 else (parts[0] if parts else "")


# ---------------------------------------------------------------------------
# HTML heuristic parser
# ---------------------------------------------------------------------------

def _parse_html_heuristics(soup: BeautifulSoup, url: str) -> list[dict]:
    """Parse rendered HTML for job listings using link/URL heuristics."""
    jobs = []
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
        last_seg = path_parts[-1]
        effective_parts = path_parts[1:] if re.match(r'^[a-z]{2}(-[a-z]{2})?$', path_parts[0]) else path_parts
        if len(effective_parts) < 3 and not re.search(r'\d', last_seg) and len(last_seg) < 25:
            continue
        slug_words = last_seg.lower().replace('-', ' ')
        if last_seg.lower() in _SKIP_TITLES or slug_words in _SKIP_TITLES:
            continue
        if any(seg.lower() in _CONTENT_SEGMENTS for seg in path_parts):
            continue
        if any(seg.lower().startswith("life-at") for seg in path_parts):
            continue
        if any(p.name in _NAV_TAGS for p in a.parents if p.name):
            continue

        seen_urls.add(full_url)
        title = None
        location_raw = ""
        anchor_text_raw = re.sub(r'\s+', ' ', a.get_text(separator=" ", strip=True)).strip()

        for htag in ("h1", "h2", "h3", "h4", "h5", "strong", "b"):
            el = a.find(htag)
            if el:
                t = el.get_text(strip=True)
                if 8 <= len(t) <= 120:
                    title = t
                    break

        if not title:
            t = re.sub(r'\s+(apply(\s+now)?|apply here|bekijk)$', '', anchor_text_raw, flags=re.IGNORECASE).strip()
            if 8 <= len(t) <= 120 and t.lower() not in _SKIP_TITLES:
                title = t

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
                        container_text = ancestor.get_text(separator=" | ", strip=True)
                        leftover = container_text.replace(t, "").strip(" |").strip()
                        if leftover and len(leftover) < 120:
                            location_raw = leftover
                    break

        if not title or title.lower() in _SKIP_TITLES:
            continue
        if re.search(r'\d+\s+min\s+read', title, re.IGNORECASE):
            continue
        if title in seen_titles:
            continue
        seen_titles.add(title)
        jobs.append({"title": title, "location_raw": location_raw, "apply_url": full_url})

    return jobs


def parse_html(html: str, url: str) -> list[dict]:
    """Parse rendered HTML for job listings using JSON-LD then HTML heuristics."""
    soup = BeautifulSoup(html, "html.parser")

    # Strategy 1: Enhanced JSON-LD
    jobs = _parse_jsonld(soup, url)
    if jobs:
        return jobs

    # Strategy 2: HTML heuristics
    return _parse_html_heuristics(soup, url)


# ---------------------------------------------------------------------------
# SPA-aware page scraper
# ---------------------------------------------------------------------------

def _try_parse(page: Page, url: str) -> tuple[list[dict], str]:
    """Get page HTML and try to parse jobs from it."""
    html = page.content()
    return parse_html(html, url), html


def _check_iframes(page: Page) -> tuple[list[dict], str]:
    """Look for ATS iframes (Greenhouse, Lever, etc.) and scrape their content."""
    for frame in page.frames:
        frame_url = frame.url
        if not frame_url or frame_url == "about:blank":
            continue
        frame_netloc = urlparse(frame_url).netloc
        is_ats = any(d in frame_netloc for d in _ATS_IFRAME_DOMAINS)
        is_job_path = any(kw in frame_url for kw in ("/jobs", "/careers", "/positions"))
        if is_ats or is_job_path:
            try:
                frame.wait_for_load_state("domcontentloaded", timeout=10000)
                html = frame.content()
                jobs = parse_html(html, frame_url)
                if jobs:
                    return jobs, html
            except Exception:
                pass
    return [], ""


def _follow_job_links(page: Page, career_url: str) -> tuple[list[dict], str]:
    """Look for 'view all jobs' style links and navigate to them."""
    base_netloc = urlparse(career_url).netloc

    # Strategy A: match by link text patterns
    for selector in _JOB_NAV_SELECTORS:
        try:
            link = page.locator(selector).first
            if link.count() > 0:
                href = link.get_attribute("href", timeout=2000)
                if href:
                    target = urljoin(career_url, href)
                    if urlparse(target).netloc == base_netloc and target != career_url:
                        page.goto(target, wait_until="domcontentloaded", timeout=20000)
                        page.wait_for_timeout(3000)
                        jobs, html = _try_parse(page, target)
                        if jobs:
                            return jobs, html
        except Exception:
            pass

    # Strategy B: scan all <a> tags for href keywords pointing to sub-pages
    try:
        anchors = page.locator("a[href]")
        count = anchors.count()
        visited: set[str] = set()
        for i in range(min(count, 200)):
            try:
                href = anchors.nth(i).get_attribute("href", timeout=1000)
            except Exception:
                continue
            if not href:
                continue
            target = urljoin(career_url, href)
            parsed = urlparse(target)
            if parsed.netloc != base_netloc or target == career_url or target in visited:
                continue
            if any(kw in parsed.path.lower() for kw in _JOB_HREF_KEYWORDS):
                visited.add(target)
                try:
                    page.goto(target, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(3000)
                    jobs, html = _try_parse(page, target)
                    if jobs:
                        return jobs, html
                except Exception:
                    pass
                # Only try the first 3 sub-page candidates
                if len(visited) >= 3:
                    break
    except Exception:
        pass

    return [], ""


def _click_load_more(page: Page, url: str) -> tuple[list[dict], str]:
    """Click 'load more' / 'show more' buttons to reveal hidden jobs."""
    clicked = False
    for selector in _LOAD_MORE_SELECTORS:
        try:
            btn = page.locator(selector).first
            if btn.count() > 0 and btn.is_visible(timeout=1000):
                for _ in range(5):
                    try:
                        btn.click(timeout=2000)
                        page.wait_for_timeout(2000)
                        clicked = True
                        if btn.count() == 0 or not btn.is_visible(timeout=500):
                            break
                    except Exception:
                        break
                break
        except Exception:
            pass
    if clicked:
        return _try_parse(page, url)
    return [], ""


def _scroll_to_load(page: Page, url: str) -> tuple[list[dict], str]:
    """Scroll the page to trigger lazy-loaded / infinite-scroll content."""
    prev_height = page.evaluate("document.body.scrollHeight")
    for _ in range(5):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == prev_height:
            break
        prev_height = new_height
    return _try_parse(page, url)


# ---------------------------------------------------------------------------
# Global portal detection (Feature 2)
# ---------------------------------------------------------------------------

def _detect_portal_redirect(page: Page, career_url: str, debug: bool = False) -> tuple[str | None, str]:
    """After page load, check if we redirected to a known ATS portal.

    Returns (ats_name, final_url) or (None, final_url).
    """
    final_url = page.url
    ats = _detect_ats_domain(final_url)
    if debug and ats:
        print(f"    [portal] Redirect detected: {career_url} -> {final_url} ({ats})")
    return ats, final_url


def _scrape_portal_by_type(page: Page, ats_name: str, portal_url: str, debug: bool = False) -> list[dict]:
    """Dispatch to the appropriate scraper for a detected portal type."""
    if ats_name == "workday":
        return scrape_workday_jobs(page, portal_url, debug=debug)
    # For Greenhouse/Lever/SmartRecruiters -- parse the rendered page (they render HTML)
    if ats_name in ("greenhouse", "lever", "smartrecruiters", "recruitee", "ashby"):
        jobs, _ = _try_parse(page, portal_url)
        return jobs
    # iCIMS / Taleo -- try HTML parse as best effort
    if ats_name in ("icims", "taleo"):
        jobs, _ = _try_parse(page, portal_url)
        return jobs
    return []


# ---------------------------------------------------------------------------
# Tier 1: Recruitee custom domain detection
# ---------------------------------------------------------------------------

def _detect_recruitee_custom_domain(html: str) -> bool:
    """Detect Recruitee custom domains by looking for /o/ link pattern."""
    # Recruitee uses /o/job-slug URL pattern universally
    return bool(re.search(r'href=["\'][^"\']*?/o/[a-z0-9][a-z0-9\-]+["\']', html, re.IGNORECASE))


def _scrape_recruitee_api(base_url: str, debug: bool = False) -> list[dict]:
    """Call Recruitee API at a custom domain to get job listings."""
    parsed = urlparse(base_url)
    api_url = f"{parsed.scheme}://{parsed.netloc}/api/offers/"
    if debug:
        print(f"    [recruitee-api] Trying {api_url}")
    try:
        req = Request(api_url, headers={"Accept": "application/json",
                                        "User-Agent": "Mozilla/5.0"})
        resp = urlopen(req, timeout=15)
        data = json.loads(resp.read().decode("utf-8"))
        offers = data.get("offers", [])
        jobs = []
        for j in offers:
            title = j.get("title", "") or ""
            location = j.get("location", "") or ""
            careers_url = j.get("careers_url", "") or j.get("url", "") or ""
            if title:
                jobs.append({
                    "title": title,
                    "location_raw": location,
                    "apply_url": careers_url,
                })
        if debug:
            print(f"    [recruitee-api] Got {len(jobs)} jobs from API")
        return jobs
    except Exception as e:
        if debug:
            print(f"    [recruitee-api] Failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Tier 1: Ashby API support
# ---------------------------------------------------------------------------

def _detect_ashby_token(html: str) -> str | None:
    """Detect Ashby job board from page HTML and extract the board token."""
    # Look for ashbyhq.com links or API references
    m = re.search(r'ashbyhq\.com/(?:posting-api/job-board/)?([a-zA-Z0-9_\-]+)', html)
    if m:
        return m.group(1)
    # Also check for Ashby embed script references
    m = re.search(r'api\.ashbyhq\.com/posting-api/job-board/([a-zA-Z0-9_\-]+)', html)
    if m:
        return m.group(1)
    return None


def _scrape_ashby_api(token: str, debug: bool = False) -> list[dict]:
    """Call Ashby posting API to get job listings."""
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    if debug:
        print(f"    [ashby-api] Trying {api_url}")
    try:
        req = Request(api_url, headers={"Accept": "application/json",
                                        "User-Agent": "Mozilla/5.0"})
        resp = urlopen(req, timeout=15)
        data = json.loads(resp.read().decode("utf-8"))
        raw_jobs = data.get("jobs", [])
        jobs = []
        for j in raw_jobs:
            title = j.get("title", "") or ""
            location = j.get("location", "") or ""
            job_url = j.get("jobUrl", "") or ""
            if title:
                jobs.append({
                    "title": title,
                    "location_raw": location,
                    "apply_url": job_url,
                })
        if debug:
            print(f"    [ashby-api] Got {len(jobs)} jobs from API")
        return jobs
    except Exception as e:
        if debug:
            print(f"    [ashby-api] Failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Tier 2: AI fallback scraper (Claude Haiku)
# ---------------------------------------------------------------------------

def _scrape_with_ai(html: str, page_url: str, debug: bool = False) -> list[dict]:
    """Use Claude Haiku to extract job listings from arbitrary HTML."""
    if not _ANTHROPIC_API_KEY:
        if debug:
            print("    [ai-fallback] No ANTHROPIC_API_KEY, skipping")
        return []

    # Trim HTML to reduce token usage -- keep body content only
    soup = BeautifulSoup(html, "html.parser")
    # Remove script, style, nav, header, footer elements
    for tag in soup.find_all(["script", "style", "nav", "header", "footer", "noscript", "svg"]):
        tag.decompose()
    body = soup.find("body")
    text = body.get_text(separator="\n", strip=True) if body else soup.get_text(separator="\n", strip=True)
    # Limit to ~15K chars to control cost
    text = text[:15000]

    if len(text.strip()) < 100:
        if debug:
            print("    [ai-fallback] Page text too short, skipping")
        return []

    prompt = f"""Extract ALL job openings from this career page text. For each job, return a JSON object with:
- "title": job title
- "location": location (city, country) or "" if unknown
- "url": full URL to the job posting, or "" if not available

Return ONLY a JSON array. If there are no jobs, return [].
Do NOT include open applications, blog posts, or team pages.

Page URL: {page_url}

Page text:
{text}"""

    if debug:
        print(f"    [ai-fallback] Sending {len(text)} chars to Claude Haiku")

    try:
        payload = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")

        req = Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": _ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        resp = urlopen(req, timeout=30)
        result = json.loads(resp.read().decode("utf-8"))
        content = result.get("content", [{}])[0].get("text", "")

        # Extract JSON array from response
        # Find the first [ and last ] to handle any surrounding text
        start = content.find("[")
        end = content.rfind("]")
        if start == -1 or end == -1:
            if debug:
                print(f"    [ai-fallback] No JSON array in response")
            return []

        raw_jobs = json.loads(content[start:end + 1])
        jobs = []
        for j in raw_jobs:
            title = j.get("title", "") or ""
            location = j.get("location", "") or ""
            url = j.get("url", "") or ""
            # Resolve relative URLs
            if url and not url.startswith("http"):
                url = urljoin(page_url, url)
            if title and title.lower() not in ("open application", "open sollicitatie"):
                jobs.append({
                    "title": title,
                    "location_raw": location,
                    "apply_url": url or page_url,
                })

        if debug:
            print(f"    [ai-fallback] AI extracted {len(jobs)} jobs")
        return jobs

    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200] if hasattr(e, "read") else ""
        if "credit balance" in body.lower() or "billing" in body.lower():
            if debug:
                print(f"    [ai-fallback] API credits exhausted, skipping")
        else:
            if debug:
                print(f"    [ai-fallback] HTTP {e.code}: {body}")
        return []
    except Exception as e:
        if debug:
            print(f"    [ai-fallback] Failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Main scrape orchestrator
# ---------------------------------------------------------------------------

def scrape_career_page(page: Page, career_url: str,
                       debug: bool = False) -> tuple[list[dict], str, str]:
    """Load a career page and try multiple strategies to extract job listings.

    Returns (jobs, final_url, portal_type) where:
      - final_url is the page we actually scraped
      - portal_type is 'workday', 'greenhouse', etc. or 'html' for generic
    """
    # Step 1: Load the page with smarter waiting
    page.goto(career_url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(2000)

    final_url = page.url
    html = page.content()

    # Step 1b: Check for portal redirect (Feature 2)
    ats_name, final_url = _detect_portal_redirect(page, career_url, debug=debug)
    if ats_name:
        jobs = _scrape_portal_by_type(page, ats_name, final_url, debug=debug)
        if jobs:
            return jobs, final_url, ats_name

    # Step 1c: Check for Workday (URL or HTML markers) -- even without redirect
    if _is_workday_page(career_url, html):
        if debug:
            print(f"    [portal] Workday detected in original URL")
        jobs = scrape_workday_jobs(page, career_url, debug=debug)
        if jobs:
            return jobs, career_url, "workday"

    # Step 1d: Check for Workday link embedded in page (e.g. WordPress -> Workday)
    workday_link = _find_workday_url(html)
    if workday_link:
        if debug:
            print(f"    [portal] Workday link found in page: {workday_link}")
        try:
            jobs = scrape_workday_jobs(page, workday_link, debug=debug)
            if jobs:
                return jobs, workday_link, "workday"
        except Exception as e:
            if debug:
                print(f"    [portal] Workday link scrape failed: {e}")

    # Step 2: Try parsing the initial page (enhanced JSON-LD + heuristics)
    jobs, _ = _try_parse(page, career_url)
    if jobs:
        if debug:
            print(f"    [strategy] Initial parse: {len(jobs)} jobs")
        return jobs, career_url, "html"

    # Step 3: Check for ATS iframes
    jobs, _ = _check_iframes(page)
    if jobs:
        if debug:
            print(f"    [strategy] Iframe: {len(jobs)} jobs")
        return jobs, career_url, "iframe"

    # Step 4: Follow "view all jobs" links
    jobs, _ = _follow_job_links(page, career_url)
    if jobs:
        nav_url = page.url
        # Check if we navigated to an ATS portal
        ats_at_target = _detect_ats_domain(nav_url)
        portal = ats_at_target or "html"
        if debug:
            print(f"    [strategy] Follow links -> {nav_url}: {len(jobs)} jobs")
        return jobs, nav_url, portal

    # Navigate back to original page for remaining strategies
    try:
        page.goto(career_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)
    except Exception:
        pass

    # Step 5: Click "load more" / "show more" buttons
    jobs, _ = _click_load_more(page, career_url)
    if jobs:
        if debug:
            print(f"    [strategy] Click load-more: {len(jobs)} jobs")
        return jobs, career_url, "html"

    # Step 6: Scroll to trigger lazy loading
    jobs, _ = _scroll_to_load(page, career_url)
    if jobs:
        if debug:
            print(f"    [strategy] Scroll: {len(jobs)} jobs")
        return jobs, career_url, "html"

    # Step 7: Recruitee custom domain detection (Tier 1)
    # Refresh html after possible navigation in earlier steps
    try:
        page.goto(career_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2000)
        html = page.content()
    except Exception:
        pass
    if _detect_recruitee_custom_domain(html):
        if debug:
            print(f"    [strategy] Recruitee custom domain detected (/o/ links)")
        jobs = _scrape_recruitee_api(career_url, debug=debug)
        if jobs:
            return jobs, career_url, "recruitee-custom"

    # Step 8: Ashby detection (Tier 1)
    ashby_token = _detect_ashby_token(html)
    if ashby_token:
        if debug:
            print(f"    [strategy] Ashby detected (token: {ashby_token})")
        jobs = _scrape_ashby_api(ashby_token, debug=debug)
        if jobs:
            return jobs, career_url, "ashby"

    # Step 9: AI fallback (Tier 2) -- send page to Claude Haiku
    if _ANTHROPIC_API_KEY:
        if debug:
            print(f"    [strategy] Trying AI fallback (Claude Haiku)")
        jobs = _scrape_with_ai(html, career_url, debug=debug)
        if jobs:
            return jobs, career_url, "ai-fallback"

    # All strategies exhausted
    if debug:
        print(f"    [strategy] All strategies exhausted -- 0 jobs")
    return [], career_url, "none"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def ensure_scraped_jobs_table(conn: sqlite3.Connection):
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
    conn.commit()


def get_careers_page_companies(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT name, token FROM companies WHERE active=1 AND source='careers_page' ORDER BY name"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def save_jobs(conn: sqlite3.Connection, company_name: str, career_url: str,
              jobs: list[dict], debug: bool = False):
    """Safe per-company job replacement (Feature 4).

    - If jobs found: delete old jobs for this company, insert fresh set
    - If 0 jobs: keep existing data untouched
    """
    now = datetime.now(timezone.utc).isoformat()

    # Count existing jobs for this company
    existing = conn.execute(
        "SELECT COUNT(*) FROM scraped_jobs WHERE company_name=?",
        (company_name,),
    ).fetchone()[0]

    if jobs:
        # Delete ONLY this company's old jobs, then insert fresh
        conn.execute("DELETE FROM scraped_jobs WHERE company_name=?", (company_name,))
        for j in jobs:
            conn.execute(
                """INSERT INTO scraped_jobs (company_name, career_url, title, location_raw, apply_url, scraped_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (company_name, career_url, j["title"], j.get("location_raw", ""), j["apply_url"], now),
            )
        conn.commit()
        if debug or existing > 0:
            print(f"    -> Replaced {existing} old jobs with {len(jobs)} fresh jobs for {company_name}")

        # Sync to intelligence layer (jobs table)
        if _HAS_INTEL:
            try:
                ensure_intel_tables(conn)
                intel_dicts = [
                    {"company": company_name, "title": j["title"],
                     "location_raw": j.get("location_raw", ""),
                     "apply_url": j["apply_url"]}
                    for j in jobs
                ]
                result = upsert_jobs(conn, "careers_page", company_name, intel_dicts)
                if debug:
                    print(f"    -> Intel: +{result['new']} new, ~{result['updated']} upd, -{result['deactivated']} closed")
            except Exception as e:
                if debug:
                    print(f"    -> Intel warning: {e}")
    else:
        # 0 jobs found -- preserve existing data
        if debug:
            print(f"    -> No jobs found, kept {existing} existing jobs for {company_name}")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run(company_filter: str | None = None, dry_run: bool = False,
        debug_company: str | None = None):
    conn = sqlite3.connect(DB_FILE)
    ensure_scraped_jobs_table(conn)
    companies = get_careers_page_companies(conn)

    # --debug-company implies single company + verbose
    debug = False
    if debug_company:
        company_filter = debug_company
        debug = True

    if company_filter:
        companies = [(n, u) for n, u in companies if n.lower() == company_filter.lower()]
        if not companies:
            print(f"Company '{company_filter}' not found in DB with source='careers_page'")
            conn.close()
            return

    print(f"{'[DRY RUN] ' if dry_run else ''}Scraping {len(companies)} career pages with Playwright...\n")

    # Metrics (Feature 6)
    stats = {
        "attempted": 0,
        "success_with_jobs": 0,
        "success_zero_jobs": 0,
        "failed": 0,
        "total_jobs_inserted": 0,
        "upgraded_to_ats": 0,
    }

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        # Global navigation timeout guard (Feature 7)
        context.set_default_timeout(30000)
        page = context.new_page()

        for company_name, career_url in companies:
            stats["attempted"] += 1
            if debug:
                print(f"\n{'='*60}")
                print(f"  DEBUG: {company_name}")
                print(f"  URL:   {career_url}")
                print(f"{'='*60}")
            else:
                print(f"  .. {company_name:<28} ...", end=" ", flush=True)

            try:
                jobs, final_url, portal_type = scrape_career_page(
                    page, career_url, debug=debug
                )
            except Exception as e:
                if debug:
                    print(f"  [ERR] {type(e).__name__}: {e}")
                else:
                    print(f"[ERR] {e}")
                stats["failed"] += 1
                # Scrape FAILED -- leave existing jobs untouched
                if debug:
                    existing = conn.execute(
                        "SELECT COUNT(*) FROM scraped_jobs WHERE company_name=?",
                        (company_name,),
                    ).fetchone()[0]
                    print(f"    -> Scrape failed, preserved {existing} old jobs for {company_name}")
                continue

            # Auto-upgrade: if career page redirected to a supported ATS,
            # update the company source+token so future runs use the API
            if portal_type in _UPGRADABLE_ATS and not dry_run:
                ats_token = _extract_ats_token(portal_type, final_url)
                if ats_token:
                    _upgrade_company_source(
                        conn, company_name, career_url,
                        portal_type, ats_token,
                    )
                    stats["upgraded_to_ats"] += 1

            if jobs:
                if debug:
                    print(f"  Result: {len(jobs)} jobs via {portal_type}")
                    print(f"  Final URL: {final_url}")
                    for j in jobs[:5]:
                        print(f"    - {j['title'][:60]:<60} {j['location_raw'][:30]}")
                    if len(jobs) > 5:
                        print(f"    ... and {len(jobs) - 5} more")
                else:
                    suffix = f" (via {final_url})" if final_url != career_url else ""
                    portal_tag = f" [{portal_type}]" if portal_type not in ("html", "none") else ""
                    print(f"[OK] {len(jobs)} jobs{portal_tag}{suffix}")

                stats["success_with_jobs"] += 1
                stats["total_jobs_inserted"] += len(jobs)
                if not dry_run:
                    save_jobs(conn, company_name, career_url, jobs, debug=debug)
            else:
                if debug:
                    print(f"  Result: 0 jobs (portal_type={portal_type})")
                else:
                    print("[--] 0 jobs (all strategies exhausted)")
                stats["success_zero_jobs"] += 1
                # Keep existing data -- don't delete on 0-result scrape

            time.sleep(1)

        context.close()
        browser.close()

    conn.close()

    # Metrics summary (Feature 6)
    print(f"\n{'='*50}")
    print(f"  SCRAPE SUMMARY")
    print(f"{'='*50}")
    print(f"  Total companies attempted:   {stats['attempted']}")
    print(f"  Success with jobs:           {stats['success_with_jobs']}")
    print(f"  Success with 0 jobs:         {stats['success_zero_jobs']}")
    print(f"  Failed (errors):             {stats['failed']}")
    print(f"  Total jobs inserted:         {stats['total_jobs_inserted']}")
    if stats["upgraded_to_ats"]:
        print(f"  Upgraded to ATS:             {stats['upgraded_to_ats']}")
    if dry_run:
        print(f"\n  (dry-run -- nothing written to DB)")
    print(f"{'='*50}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Playwright career page scraper for Hire Assist")
    parser.add_argument("--company", help="Scrape a single company by name")
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing to DB")
    parser.add_argument("--debug-company", metavar="NAME",
                        help="Scrape a single company with detailed debug output")
    args = parser.parse_args()
    run(company_filter=args.company, dry_run=args.dry_run,
        debug_company=args.debug_company)
