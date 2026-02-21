#!/usr/bin/env python3
"""
agent_discover.py - Company discovery pipeline using free data sources.

Discovers Dutch tech companies via OpenStreetMap (Overpass API), scores and
filters candidates, probes ATS endpoints with verification, falls back to
career-page detection with domain validation, and stores validated companies
in companies.db.

All discovery is FREE by default. An optional --use-ai-cleanup flag adds a
cheap Claude Haiku pass for name/website normalisation (costs < $0.03/run).

Usage:
    python agent_discover.py --region Eindhoven
    python agent_discover.py --region "Noord-Brabant" --limit 300
    python agent_discover.py --region Netherlands --daily-target 100
    python agent_discover.py --region Eindhoven --use-ai-cleanup
    python agent_discover.py --region Eindhoven --dry-run
    python agent_discover.py --region Eindhoven --min-score 20 --skip-ats

Via Docker:
    docker compose --profile discover-ai run --rm discover-ai
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# Optional: .env support (for AI cleanup key)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from osm_discover import discover_osm
from google_discover import discover_google
from candidate_filter import score_candidate, is_candidate_eligible
from db_config import get_db_path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_FILE = get_db_path()
LOG_DIR = Path("data/logs")
MIN_TOKEN_LEN = 5          # tokens shorter than this need strict verification

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "HireAssist/0.3 (discovery)"

# Known ATS hosting domains -- career-page redirects to these are OK
_ATS_DOMAINS = {
    "greenhouse.io", "boards.greenhouse.io",
    "lever.co", "jobs.lever.co",
    "smartrecruiters.com", "jobs.smartrecruiters.com",
    "recruitee.com",
    "workday.com", "myworkdayjobs.com",
    "careers-page.com", "breezy.hr", "personio.de", "join.com",
    "ashbyhq.com", "applytojob.com", "teamtailor.com",
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"discover_{datetime.now().strftime('%Y%m%d')}.log"

    logger = logging.getLogger("discover")
    if logger.handlers:
        return logger  # already set up (e.g. re-entry)
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)

    return logger


# ---------------------------------------------------------------------------
# ATS probing
# ---------------------------------------------------------------------------
def probe_greenhouse(token: str) -> int | None:
    try:
        r = SESSION.get(
            f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
            timeout=15,
        )
        if r.status_code == 200:
            return len(r.json().get("jobs", []))
    except Exception:
        pass
    return None


def probe_lever(token: str) -> int | None:
    try:
        r = SESSION.get(
            f"https://api.lever.co/v0/postings/{token}?mode=json",
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return len(data)
            if isinstance(data, dict):
                return len(data.get("data", []))
    except Exception:
        pass
    return None


def probe_smartrecruiters(token: str) -> int | None:
    try:
        r = SESSION.get(
            f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
            timeout=15,
        )
        if r.status_code == 200:
            return len(r.json().get("content", []))
    except Exception:
        pass
    return None


def probe_recruitee(token: str) -> int | None:
    try:
        r = SESSION.get(
            f"https://{token}.recruitee.com/api/offers/",
            timeout=15,
        )
        if r.status_code == 200:
            return len(r.json().get("offers", []))
    except Exception:
        pass
    return None


PROBERS = {
    "greenhouse": probe_greenhouse,
    "lever": probe_lever,
    "smartrecruiters": probe_smartrecruiters,
    "recruitee": probe_recruitee,
}


def probe_ats(company_name: str, possible_tokens: list[str]) -> list[dict]:
    """Try multiple tokens against all 4 ATS platforms. Return valid hits."""
    hits = []
    for token in possible_tokens:
        for source, prober in PROBERS.items():
            time.sleep(0.3)
            count = prober(token)
            if count is not None:
                hits.append({"source": source, "token": token, "jobs": count})
    return hits


# ---------------------------------------------------------------------------
# ATS verification  (NEW -- prevents token collision false positives)
# ---------------------------------------------------------------------------
def _normalise_for_match(text: str) -> str:
    """Lowercase, strip non-alnum, collapse whitespace."""
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def verify_ats_hit(
    hit: dict,
    candidate_name: str,
    candidate_domain: str | None,
    strict: bool = True,
) -> tuple[bool, str]:
    """
    After an ATS probe succeeds, verify the board actually belongs to
    *candidate_name* / *candidate_domain*.

    Returns (verified: bool, reason: str).
    """
    token = hit["token"]
    source = hit["source"]
    norm_name = _normalise_for_match(candidate_name)
    norm_token = _normalise_for_match(token)
    domain_base = candidate_domain.split(".")[0] if candidate_domain else ""

    # --- Rule 1: very short tokens are high-risk for collision ---
    if len(token) < MIN_TOKEN_LEN and strict:
        # Only accept if token exactly matches domain base or start of name
        first_word = norm_name.split()[0] if norm_name else ""
        if norm_token not in (domain_base, first_word, norm_name.replace(" ", "")):
            return False, f"short_token_{len(token)}ch"

    # --- Rule 2: Fetch board metadata and compare company name ---
    board_name = _fetch_board_name(source, token)
    if board_name:
        norm_board = _normalise_for_match(board_name)
        # Check overlap: board name contains candidate name or vice-versa,
        # or shares a significant word, or domain base matches
        if _names_match(norm_name, norm_board, domain_base):
            return True, "board_name_match"
        if strict:
            return False, f"board_mismatch:{board_name[:40]}"

    # --- Rule 3: token closely matches domain or normalised name ---
    if domain_base and (norm_token == domain_base
                        or domain_base.startswith(norm_token)
                        or norm_token.startswith(domain_base)):
        return True, "domain_match"

    name_slug = norm_name.replace(" ", "")
    if norm_token == name_slug or name_slug.startswith(norm_token):
        return True, "name_match"

    # If non-strict, allow anything
    if not strict:
        return True, "non_strict"

    return False, "no_match"


def _fetch_board_name(source: str, token: str) -> str | None:
    """Fetch the company/board display name from the ATS API."""
    try:
        if source == "greenhouse":
            r = SESSION.get(
                f"https://boards-api.greenhouse.io/v1/boards/{token}",
                timeout=10,
            )
            if r.status_code == 200:
                return r.json().get("name")

        elif source == "lever":
            r = SESSION.get(
                f"https://api.lever.co/v0/postings/{token}?mode=json&limit=1",
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data:
                    # Lever postings contain hostedUrl: jobs.lever.co/<company>/...
                    url = data[0].get("hostedUrl", "")
                    parts = url.replace("https://jobs.lever.co/", "").split("/")
                    if parts:
                        return parts[0]

        elif source == "smartrecruiters":
            # SmartRecruiters company info endpoint
            r = SESSION.get(
                f"https://api.smartrecruiters.com/v1/companies/{token}",
                timeout=10,
            )
            if r.status_code == 200:
                return r.json().get("name")

        elif source == "recruitee":
            # Recruitee: the token IS the subdomain, so it's self-verifying
            return token
    except Exception:
        pass
    return None


def _names_match(norm_name: str, norm_board: str, domain_base: str) -> bool:
    """Fuzzy check: do these names plausibly refer to the same company?"""
    # Direct containment
    if norm_name in norm_board or norm_board in norm_name:
        return True

    # Domain base in board name
    if domain_base and domain_base in norm_board:
        return True

    # Significant shared words (>= 4 chars)
    name_words = {w for w in norm_name.split() if len(w) >= 4}
    board_words = {w for w in norm_board.split() if len(w) >= 4}
    if name_words & board_words:
        return True

    return False


# ---------------------------------------------------------------------------
# Career page detection + domain verification
# ---------------------------------------------------------------------------
CAREER_PATHS = [
    "/careers", "/jobs", "/en/careers", "/en/jobs",
    "/vacatures", "/work-with-us", "/join-us",
    "/careers/open-positions", "/company/careers",
]


def _registrable_domain(domain: str) -> str:
    """
    Extract registrable domain:  careers.example.com -> example.com
    Simple heuristic -- handles .com, .nl, .io, .co.uk etc.
    """
    parts = domain.lower().split(".")
    if len(parts) <= 2:
        return domain.lower()
    # Handle .co.uk, .com.au style
    if parts[-2] in ("co", "com", "org", "net", "ac"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def find_careers_page_verified(
    domain: str,
    logger: logging.Logger | None = None,
) -> tuple[str | None, str | None]:
    """
    Try standard career-page paths on *domain*.
    Returns (career_url, reject_reason).

    Verifies that the final URL belongs to the same registrable domain
    or to a known ATS hosting domain.
    """
    expected_reg = _registrable_domain(domain)

    def _check(url: str) -> tuple[str | None, str | None]:
        try:
            r = SESSION.get(
                url, timeout=8, allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code != 200:
                return None, None
            if "text/html" not in r.headers.get("content-type", ""):
                return None, None

            final_domain = re.sub(r"^https?://", "", r.url.lower()).split("/")[0]
            final_domain = re.sub(r"^www\.", "", final_domain)
            final_reg = _registrable_domain(final_domain)

            # Same domain family?
            if final_reg == expected_reg:
                return r.url, None

            # Redirected to a known ATS platform? That's OK.
            if any(final_domain.endswith(ats) for ats in _ATS_DOMAINS):
                return r.url, None

            # Mismatch -- suspicious redirect
            if logger:
                logger.debug(
                    f"    Career page redirect mismatch: {url} -> {r.url} "
                    f"(expected *{expected_reg}, got {final_reg})"
                )
            return None, f"redirect_mismatch:{final_reg}"
        except Exception:
            return None, None

    base = f"https://{domain}"
    for path in CAREER_PATHS:
        result, reason = _check(base + path)
        if result:
            return result, None
        if reason:
            return None, reason

    for sub in ("careers", "jobs"):
        result, reason = _check(f"https://{sub}.{domain}")
        if result:
            return result, None
        if reason:
            return None, reason

    return None, None


# ---------------------------------------------------------------------------
# DB helpers -- companies table
# ---------------------------------------------------------------------------
def ensure_db(conn: sqlite3.Connection):
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
    conn.commit()


def company_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM companies WHERE LOWER(name) = LOWER(?) LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def domain_in_db(conn: sqlite3.Connection, domain: str) -> bool:
    """Check if *domain* already appears in any careers_page token."""
    row = conn.execute(
        "SELECT 1 FROM companies WHERE source='careers_page' AND LOWER(token) LIKE ? LIMIT 1",
        (f"%{domain.lower()}%",),
    ).fetchone()
    return row is not None


def add_company_to_db(
    conn: sqlite3.Connection, name: str, source: str, token: str,
    confidence: str = "auto",
) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            """INSERT INTO companies
               (name, source, token, active, confidence, discovered_at, last_verified_at)
               VALUES (?, ?, ?, 1, ?, ?, ?)""",
            (name, source, token, confidence, now, now),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


# ---------------------------------------------------------------------------
# DB helpers -- discovery_candidates table (with migration)
# ---------------------------------------------------------------------------
_NEW_COLUMNS = [
    ("score",          "INTEGER"),
    ("reject_reason",  "TEXT"),
    ("ats_verified",   "INTEGER DEFAULT 0"),
    ("website_domain", "TEXT"),
]


def ensure_candidates_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS discovery_candidates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            website     TEXT,
            city        TEXT,
            region      TEXT,
            source      TEXT NOT NULL DEFAULT 'osm',
            osm_id      TEXT,
            raw_json    TEXT,
            status      TEXT NOT NULL DEFAULT 'new',
            last_seen_at  TEXT,
            processed_at  TEXT,
            score         INTEGER,
            reject_reason TEXT,
            ats_verified  INTEGER DEFAULT 0,
            website_domain TEXT,
            UNIQUE(source, osm_id)
        )
    """)
    conn.commit()

    # Safe migration: add columns that might be missing from v1 tables
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(discovery_candidates)").fetchall()
    }
    for col_name, col_type in _NEW_COLUMNS:
        if col_name not in existing:
            conn.execute(
                f"ALTER TABLE discovery_candidates ADD COLUMN {col_name} {col_type}"
            )
    conn.commit()


def store_candidates(conn: sqlite3.Connection, candidates: list[dict]) -> int:
    """Upsert candidates into discovery_candidates. Return count of NEW rows."""
    now = datetime.now(timezone.utc).isoformat()
    new_count = 0
    for c in candidates:
        domain = normalize_domain(c.get("website"))
        cur = conn.execute(
            """INSERT INTO discovery_candidates
               (name, website, city, region, source, osm_id, raw_json,
                status, last_seen_at, score, website_domain)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)
               ON CONFLICT(source, osm_id) DO UPDATE SET
                   last_seen_at=excluded.last_seen_at,
                   score=excluded.score,
                   website_domain=excluded.website_domain""",
            (
                c["name"],
                c.get("website"),
                c.get("city", ""),
                c.get("region", ""),
                c.get("source", "osm"),
                c.get("osm_id", ""),
                json.dumps(c.get("raw_json") or c.get("osm_tags", {}), ensure_ascii=False),
                now,
                c.get("_score"),
                domain,
            ),
        )
        if cur.lastrowid and cur.rowcount == 1:
            new_count += 1
    conn.commit()
    return new_count


def get_unprocessed(conn: sqlite3.Connection, limit: int) -> list[dict]:
    """Return up to *limit* candidates with status='new', ordered by score desc."""
    rows = conn.execute(
        """SELECT id, name, website, city, region, source, osm_id, raw_json,
                  score, website_domain
           FROM discovery_candidates
           WHERE status = 'new'
           ORDER BY
               score DESC,
               CASE WHEN website IS NOT NULL AND website != '' THEN 0 ELSE 1 END,
               name
           LIMIT ?""",
        (limit,),
    ).fetchall()
    cols = ("id", "name", "website", "city", "region", "source", "osm_id",
            "raw_json", "_score", "_domain")
    return [dict(zip(cols, r)) for r in rows]


def update_candidate(
    conn: sqlite3.Connection,
    cand_id: int,
    status: str,
    reject_reason: str = "",
    ats_verified: int = 0,
    score: int | None = None,
):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE discovery_candidates
           SET status=?, processed_at=?, reject_reason=?, ats_verified=?,
               score=COALESCE(?, score)
           WHERE id=?""",
        (status, now, reject_reason, ats_verified, score, cand_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
_BIZ_SUFFIXES = re.compile(
    r"\b(b\.?v\.?|n\.?v\.?|v\.?o\.?f\.?|holding|group|nederland[s]?"
    r"|international|europe|gmbh|ltd|inc|llc|s\.?a\.?|s\.?r\.?l\.?)\b",
    re.IGNORECASE,
)


def normalize_name(name: str) -> str:
    """Strip business suffixes and extra whitespace for dedup comparison."""
    out = _BIZ_SUFFIXES.sub("", name)
    out = re.sub(r"[^\w\s-]", "", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def normalize_domain(url: str | None) -> str | None:
    """Extract a bare domain from a URL.  None for empty/social-media/invalid."""
    if not url:
        return None
    d = re.sub(r"^https?://", "", url.lower().strip())
    d = re.sub(r"^www\.", "", d)
    d = d.split("/")[0].split(":")[0]

    _SKIP = {
        "facebook.com", "fb.com", "linkedin.com", "twitter.com", "x.com",
        "instagram.com", "youtube.com", "wikipedia.org", "wikidata.org",
        "github.com", "google.com",
    }
    if d in _SKIP or any(d.endswith("." + s) for s in _SKIP):
        return None
    if "." not in d or len(d) < 4:
        return None
    return d


def generate_tokens(name: str, domain: str) -> list[str]:
    """Infer plausible ATS board tokens from *name* and *domain*."""
    tokens: set[str] = set()

    # Domain-based
    base = domain.split(".")[0]
    tokens.add(base)
    tokens.add(base.replace("-", ""))

    # Name-based
    clean = normalize_name(name).lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", clean).strip()
    tokens.add(slug.replace(" ", ""))
    tokens.add(slug.replace(" ", "-"))
    first_word = slug.split()[0] if slug else ""
    if first_word and first_word != base:
        tokens.add(first_word)

    # PascalCase variant (SmartRecruiters often uses this)
    pascal = name.split()[0] if name else ""
    if pascal and pascal.isalpha():
        tokens.add(pascal)

    # Common suffixes
    for t in list(tokens):
        if t and len(t) > 2:
            tokens.add(f"{t}hq")
            tokens.add(f"{t}-nl")

    return [t for t in tokens if t and len(t) > 1]


# ---------------------------------------------------------------------------
# Optional AI cleanup (Claude Haiku -- very cheap, runs AFTER filtering)
# ---------------------------------------------------------------------------
def ai_cleanup_batch(
    candidates: list[dict], logger: logging.Logger
) -> list[dict]:
    """
    Use Claude Haiku to normalise names, infer missing websites, and flag
    non-company entries.  Processes in batches of 20.  Costs < $0.03 total.
    Falls back gracefully if anything fails.
    """
    try:
        from anthropic import Anthropic
    except ImportError:
        logger.warning("  anthropic package not installed, skipping AI cleanup")
        return candidates

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("  ANTHROPIC_API_KEY not set, skipping AI cleanup")
        return candidates

    client = Anthropic(api_key=api_key)
    BATCH = 20
    updated = []

    for i in range(0, len(candidates), BATCH):
        batch = candidates[i : i + BATCH]
        payload = [
            {"name": c["name"], "website": c.get("website") or "",
             "city": c.get("city") or ""}
            for c in batch
        ]
        prompt = (
            "You are normalising company data from OpenStreetMap for a Dutch tech job aggregator.\n"
            "For each entry:\n"
            "1. Clean the company name (remove B.V., N.V., etc.).\n"
            "2. If website is empty and you KNOW the company, fill in the likely domain (e.g. asml.com).\n"
            "3. Set skip=true ONLY if this is clearly NOT a business (e.g. a church, school, park).\n"
            "Return ONLY a JSON array with the same length. Each object: "
            '{"name": "...", "website": "...", "city": "...", "skip": false}\n\n'
            f"Input:\n{json.dumps(payload, ensure_ascii=False)}"
        )
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if not match:
                raise ValueError("No JSON array in response")
            cleaned = json.loads(match.group())

            if len(cleaned) != len(batch):
                raise ValueError(f"Length mismatch: got {len(cleaned)}, expected {len(batch)}")

            for orig, fix in zip(batch, cleaned):
                if fix.get("skip"):
                    orig["_skip"] = True
                    logger.debug(f"  AI: skipping '{orig['name']}' (not a company)")
                else:
                    if fix.get("name"):
                        orig["name"] = fix["name"]
                    if fix.get("website") and not orig.get("website"):
                        orig["website"] = fix["website"]
                        logger.debug(f"  AI: inferred website '{fix['website']}' for '{orig['name']}'")
                updated.append(orig)

            logger.info(f"  AI cleanup batch {i // BATCH + 1}: {len(batch)} candidates processed")
        except Exception as e:
            logger.warning(f"  AI cleanup batch {i // BATCH + 1} failed ({e}), using originals")
            updated.extend(batch)

    before = len(updated)
    updated = [c for c in updated if not c.get("_skip")]
    if before != len(updated):
        logger.info(f"  AI cleanup removed {before - len(updated)} non-company entries")
    return updated


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def run(
    region: str,
    source: str = "osm",
    limit: int = 200,
    daily_target: int = 50,
    use_ai: bool = False,
    dry_run: bool = False,
    min_score: int = 30,
    require_website: bool = False,
    strict_ats: bool = True,
    skip_ats: bool = False,
):
    logger = setup_logging()
    tag = "[DRY RUN] " if dry_run else ""
    logger.info(f"{tag}Discovery pipeline starting  region={region}  source={source}")
    logger.info(f"  limit={limit}  daily_target={daily_target}  min_score={min_score}")
    logger.info(f"  strict_ats={strict_ats}  skip_ats={skip_ats}  ai_cleanup={use_ai}")

    conn = sqlite3.connect(DB_FILE)
    ensure_db(conn)
    ensure_candidates_table(conn)

    # ---- Counters ----
    stats = {
        "fetched_from_osm": 0,
        "eligible_after_filter": 0,
        "processed": 0,
        "added": 0,
        "skipped_dupe": 0,
        "rejected_non_company": 0,
        "rejected_no_website": 0,
        "rejected_ats_mismatch": 0,
        "rejected_no_match": 0,
        "errors": 0,
    }

    # ---- Step 1: Fetch candidates ----
    if source == "osm":
        raw = discover_osm(region, limit=limit * 3)
    elif source == "google":
        raw = discover_google(region)
    else:
        logger.error(f"Unknown source '{source}'. Use 'osm' or 'google'.")
        conn.close()
        return

    stats["fetched_from_osm"] = len(raw)  # reuse counter name for compatibility
    logger.info(f"Fetched {len(raw)} candidates from {source}")

    if not raw:
        logger.info("No candidates found. Try a broader region.")
        conn.close()
        return

    # ---- Step 2: Score and filter ----
    logger.info("Scoring and filtering candidates...")
    eligible = []
    filtered_out = 0
    for c in raw:
        sc = score_candidate(c)
        c["_score"] = sc
        ok, reason = is_candidate_eligible(c, min_score=min_score, require_website=require_website)
        if ok:
            eligible.append(c)
        else:
            filtered_out += 1
            logger.debug(f"  Filtered: {c['name']:<35}  score={sc:>4}  reason={reason}")

    stats["eligible_after_filter"] = len(eligible)
    logger.info(
        f"Eligible: {len(eligible)} / {len(raw)}  "
        f"(filtered out {filtered_out})"
    )

    if not eligible:
        logger.info("No eligible candidates after filtering.")
        conn.close()
        return

    # ---- Step 3: Store in discovery_candidates (upsert) ----
    new_count = store_candidates(conn, eligible)
    logger.info(f"Stored candidates: {new_count} new, {len(eligible) - new_count} already seen")

    # ---- Step 4: Load unprocessed candidates ----
    unprocessed = get_unprocessed(conn, limit=limit)
    logger.info(f"{len(unprocessed)} unprocessed candidates to evaluate")

    if not unprocessed:
        logger.info("Nothing new to process. Try a different region or wait for OSM updates.")
        conn.close()
        return

    # ---- Step 5: Optional AI cleanup (on filtered, smaller batch) ----
    if use_ai:
        logger.info("Running AI cleanup (Claude Haiku) on eligible candidates...")
        unprocessed = ai_cleanup_batch(unprocessed, logger)

    # ---- Step 6: Process each candidate ----
    for i, cand in enumerate(unprocessed, 1):
        if stats["added"] >= daily_target:
            logger.info(f"Reached daily target ({daily_target}). Stopping.")
            break

        cid = cand["id"]
        cname = cand["name"]
        score = cand.get("_score") or 0
        domain = cand.get("_domain") or normalize_domain(cand.get("website"))

        if dry_run:
            # Detailed dry-run output
            web_str = domain or "(none)"
            logger.info(
                f"  {i:>3}. {cname:<35}  web={web_str:<25}  score={score}"
            )

        try:
            result = _process_one(
                conn, cand, cid, cname, domain, score,
                strict_ats=strict_ats, skip_ats=skip_ats,
                dry_run=dry_run, logger=logger, stats=stats,
            )
        except Exception as e:
            logger.warning(f"  Error processing '{cname}': {e}")
            stats["errors"] += 1
            if not dry_run:
                update_candidate(conn, cid, "error", reject_reason=str(e)[:100])

    # ---- Summary ----
    conn.close()
    _print_summary(logger, region, stats, dry_run)


def _process_one(
    conn, cand, cid, cname, domain, score,
    strict_ats, skip_ats, dry_run, logger, stats,
) -> str:
    """Process a single candidate. Returns outcome string."""

    # Already in companies table?
    norm = normalize_name(cname)
    if company_exists(conn, cname) or company_exists(conn, norm):
        logger.info(f"  Already in DB, skipping: {cname}")
        if not dry_run:
            update_candidate(conn, cid, "processed", score=score)
        stats["skipped_dupe"] += 1
        return "dupe"

    # Need a domain to probe
    if not domain:
        logger.info(f"  No usable website: {cname}")
        if not dry_run:
            update_candidate(conn, cid, "rejected", reject_reason="no_website", score=score)
        stats["rejected_no_website"] += 1
        return "no_website"

    # Domain already represented?
    if domain_in_db(conn, domain):
        logger.info(f"  Domain '{domain}' already in DB: {cname}")
        if not dry_run:
            update_candidate(conn, cid, "processed", score=score)
        stats["skipped_dupe"] += 1
        return "dupe_domain"

    # ---- Probe ATS endpoints (unless --skip-ats) ----
    if not skip_ats:
        tokens = generate_tokens(cname, domain)
        logger.debug(f"  Trying tokens: {tokens}")
        hits = probe_ats(cname, tokens)

        if hits:
            best = max(hits, key=lambda h: h["jobs"])

            # Verify the hit belongs to this candidate
            verified, vreason = verify_ats_hit(
                best, cname, domain, strict=strict_ats
            )
            if verified:
                logger.info(
                    f"  + ATS verified: {best['source']}/{best['token']} "
                    f"({best['jobs']} jobs)  [{vreason}]  -- {cname}"
                )
                if not dry_run:
                    add_company_to_db(conn, cname, best["source"], best["token"])
                    update_candidate(
                        conn, cid, "processed", ats_verified=1, score=score
                    )
                stats["added"] += 1
                stats["processed"] += 1
                return "ats_added"
            else:
                logger.info(
                    f"  x ATS rejected: {best['source']}/{best['token']} "
                    f"({best['jobs']} jobs)  [{vreason}]  -- {cname}"
                )
                if not dry_run:
                    update_candidate(
                        conn, cid, "rejected",
                        reject_reason=f"ats_mismatch:{vreason}",
                        score=score,
                    )
                stats["rejected_ats_mismatch"] += 1
                return "ats_mismatch"

    # ---- Fallback: career page detection with domain verification ----
    logger.debug(f"  Trying career page on {domain}...")
    career_url, reject_reason = find_careers_page_verified(domain, logger)

    if career_url:
        logger.info(f"  + Career page: {career_url}  -- {cname}")
        if not dry_run:
            add_company_to_db(conn, cname, "careers_page", career_url)
            update_candidate(conn, cid, "processed", score=score)
        stats["added"] += 1
        stats["processed"] += 1
        return "career_added"

    if reject_reason:
        logger.info(f"  x Career page rejected ({reject_reason}): {cname}")
        if not dry_run:
            update_candidate(
                conn, cid, "rejected",
                reject_reason=f"career:{reject_reason}",
                score=score,
            )
        stats["rejected_no_match"] += 1
        return "career_mismatch"

    # Nothing found
    logger.info(f"  - No ATS or career page for '{domain}': {cname}")
    if not dry_run:
        update_candidate(conn, cid, "rejected", reject_reason="no_match", score=score)
    stats["rejected_no_match"] += 1
    return "no_match"


def _print_summary(logger, region, stats, dry_run):
    total_rejected = (
        stats["rejected_non_company"]
        + stats["rejected_no_website"]
        + stats["rejected_ats_mismatch"]
        + stats["rejected_no_match"]
    )
    logger.info("")
    logger.info("=" * 62)
    logger.info(f"  Region:                {region}")
    logger.info(f"  Fetched from OSM:      {stats['fetched_from_osm']}")
    logger.info(f"  Eligible after filter: {stats['eligible_after_filter']}")
    logger.info(f"  Skipped (dupes):       {stats['skipped_dupe']}")
    logger.info(f"  Rejected (no website): {stats['rejected_no_website']}")
    logger.info(f"  Rejected (ATS mismatch): {stats['rejected_ats_mismatch']}")
    logger.info(f"  Rejected (no match):   {stats['rejected_no_match']}")
    logger.info(f"  Errors:                {stats['errors']}")
    logger.info(f"  Companies {'would be ' if dry_run else ''}added: {stats['added']}")
    if dry_run:
        logger.info("  (dry-run -- nothing written)")
    logger.info("=" * 62)

    # One-line summary for quick scanning in logs
    summary = (
        f"SUMMARY region={region} "
        f"osm={stats['fetched_from_osm']} "
        f"eligible={stats['eligible_after_filter']} "
        f"added={stats['added']} "
        f"rejected={total_rejected} "
        f"errors={stats['errors']}"
    )
    logger.info(summary)

    log_file = LOG_DIR / f"discover_{datetime.now().strftime('%Y%m%d')}.log"
    logger.info(f"  Log: {log_file}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Company discovery pipeline using free data sources (OSM)"
    )
    parser.add_argument(
        "--region", type=str, default="Netherlands",
        help="Region to search (e.g. Eindhoven, Noord-Brabant, Netherlands)",
    )
    parser.add_argument(
        "--source", type=str, default="osm", choices=["osm", "google"],
        help="Discovery source: osm (free) or google (Places API, needs key)",
    )
    parser.add_argument(
        "--limit", type=int, default=200,
        help="Max candidates to process per run (default: 200)",
    )
    parser.add_argument(
        "--daily-target", type=int, default=50,
        help="Stop after adding this many companies (default: 50)",
    )
    parser.add_argument(
        "--min-score", type=int, default=30,
        help="Minimum candidate score to proceed (default: 30)",
    )
    parser.add_argument(
        "--require-website", action="store_true",
        help="Only process candidates that have an OSM website tag",
    )
    parser.add_argument(
        "--strict-ats-verify", action="store_true", default=True,
        dest="strict_ats",
        help="Verify ATS board names match candidate (default: on)",
    )
    parser.add_argument(
        "--no-strict-ats-verify", action="store_false", dest="strict_ats",
        help="Disable strict ATS verification",
    )
    parser.add_argument(
        "--skip-ats", action="store_true",
        help="Skip ATS probing (test filter + career detection only)",
    )
    parser.add_argument(
        "--use-ai-cleanup", action="store_true",
        help="Enable Claude Haiku cleanup pass (costs ~$0.03, needs ANTHROPIC_API_KEY)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without writing to DB",
    )
    parser.add_argument(
        "--reverse-ats", action="store_true",
        help="Run ATS reverse discovery (probe seed token list for NL companies)",
    )
    # Deprecated
    parser.add_argument("--max-rounds", type=int, default=None, help=argparse.SUPPRESS)

    args = parser.parse_args()

    if args.reverse_ats:
        from ats_reverse_discover import run_reverse_discovery
        run_reverse_discovery(dry_run=args.dry_run)
        sys.exit(0)

    if args.max_rounds is not None:
        print("Note: --max-rounds is deprecated. Use --limit instead.")

    run(
        region=args.region,
        source=args.source,
        limit=args.limit,
        daily_target=args.daily_target,
        use_ai=args.use_ai_cleanup,
        dry_run=args.dry_run,
        min_score=args.min_score,
        require_website=args.require_website,
        strict_ats=args.strict_ats,
        skip_ats=args.skip_ats,
    )
