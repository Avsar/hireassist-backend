#!/usr/bin/env python3
"""
ats_reverse_discover.py - Discover NL tech companies by probing ATS board tokens.

Complements the OSM-based pipeline (agent_discover.py) by finding companies that
use Greenhouse, Lever, SmartRecruiters, or Recruitee but may not have a physical
presence in OpenStreetMap (e.g. digital-first startups).

Strategy:
  1. Probe a curated seed list of ~150 NL tech company tokens
  2. Optionally mine tokens from existing scraped job URLs
  3. For each valid board, check if any jobs are located in the Netherlands
  4. Add NL-relevant companies to companies.db

Usage:
    python ats_reverse_discover.py                          # all platforms
    python ats_reverse_discover.py --ats greenhouse         # Greenhouse only
    python ats_reverse_discover.py --mine-tokens            # also mine from scraped data
    python ats_reverse_discover.py --dry-run --limit 20     # preview first 20
    python ats_reverse_discover.py --all                    # same as no flag (all)

Via agent_discover.py:
    python agent_discover.py --reverse-ats --dry-run
"""

import argparse
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# Reuse ATS probing, verification, normalisation, and DB helpers
from agent_discover import (
    probe_greenhouse,
    probe_lever,
    probe_smartrecruiters,
    probe_recruitee,
    PROBERS,
    _fetch_board_name,
    normalize_name,
    normalize_domain,
    ensure_db,
    company_exists,
    domain_in_db,
    add_company_to_db,
    SESSION,
)

# NL cities list from app.py (36 cities)
from app import _NL_CITIES

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_FILE = Path("companies.db")
LOG_DIR = Path("data/logs")

# NL location keywords (beyond city names)
_NL_KEYWORDS = (
    "netherlands", "nederland", "the netherlands", "dutch",
    " nl", "(nl)", "nl)", ",nl", ", nl",
)

# Regex patterns to extract ATS tokens from URLs
_ATS_URL_PATTERNS = [
    (re.compile(r"boards-api\.greenhouse\.io/v1/boards/([^/]+)", re.I), "greenhouse"),
    (re.compile(r"boards\.greenhouse\.io/([^/]+)", re.I), "greenhouse"),
    (re.compile(r"jobs\.lever\.co/([^/?#]+)", re.I), "lever"),
    (re.compile(r"jobs\.eu\.lever\.co/([^/?#]+)", re.I), "lever"),
    (re.compile(r"jobs\.smartrecruiters\.com/([^/?#]+)", re.I), "smartrecruiters"),
    (re.compile(r"([a-z0-9][a-z0-9-]+)\.recruitee\.com", re.I), "recruitee"),
]


# ---------------------------------------------------------------------------
# Logging  (own logger, separate from agent_discover)
# ---------------------------------------------------------------------------
def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"ats_reverse_{datetime.now().strftime('%Y%m%d')}.log"

    logger = logging.getLogger("ats_reverse")
    if logger.handlers:
        return logger
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
# DB table for tracking reverse-discovered candidates
# ---------------------------------------------------------------------------
def ensure_reverse_candidates_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ats_reverse_candidates (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ats_type        TEXT NOT NULL,
            board_token     TEXT NOT NULL,
            company_name    TEXT,
            domain          TEXT,
            nl_job_count    INTEGER DEFAULT 0,
            total_job_count INTEGER DEFAULT 0,
            nl_confidence   TEXT,
            detected_at     TEXT,
            processed_at    TEXT,
            status          TEXT NOT NULL DEFAULT 'new',
            reject_reason   TEXT,
            UNIQUE(ats_type, board_token)
        )
    """)
    conn.commit()

    # Safe migration: add nl_confidence column if missing (pre-existing tables)
    existing = {
        row[1]
        for row in conn.execute("PRAGMA table_info(ats_reverse_candidates)").fetchall()
    }
    if "nl_confidence" not in existing:
        conn.execute(
            "ALTER TABLE ats_reverse_candidates ADD COLUMN nl_confidence TEXT"
        )
        conn.commit()


# ---------------------------------------------------------------------------
# NL Seed List  (~150 NL tech companies NOT in discover.py CANDIDATES)
#
# Format mirrors discover.py: (company_hint, [(ats_type, token), ...])
# First hit per company wins.  Tokens are lowercase slugs.
# ---------------------------------------------------------------------------
NL_SEED_TOKENS = [
    # ---- Fintech / Trading ----
    ("Bitvavo",             [("greenhouse", "bitvavo"), ("lever", "bitvavo"), ("recruitee", "bitvavo")]),
    ("Bux",                 [("greenhouse", "bux"), ("lever", "bux"), ("recruitee", "bux")]),
    ("Ohpen",               [("greenhouse", "ohpen"), ("lever", "ohpen"), ("recruitee", "ohpen")]),
    ("Mambu",               [("greenhouse", "mambu"), ("lever", "mambu")]),
    ("Tink",                [("greenhouse", "tink"), ("lever", "tink")]),
    ("Flow Traders",        [("greenhouse", "flowtraders"), ("lever", "flowtraders"), ("smartrecruiters", "FlowTraders")]),
    ("IMC Trading",         [("greenhouse", "imc"), ("greenhouse", "imctrading"), ("lever", "imc"), ("smartrecruiters", "IMC")]),
    ("Optiver",             [("greenhouse", "optiver"), ("lever", "optiver"), ("smartrecruiters", "Optiver")]),
    ("All Options",         [("greenhouse", "alloptions"), ("lever", "alloptions")]),
    ("DRW",                 [("greenhouse", "drw"), ("lever", "drw")]),
    ("Finaps",              [("greenhouse", "finaps"), ("lever", "finaps"), ("recruitee", "finaps")]),
    ("Cobase",              [("greenhouse", "cobase"), ("lever", "cobase"), ("recruitee", "cobase")]),
    ("Five Degrees",        [("greenhouse", "fivedegrees"), ("lever", "fivedegrees"), ("recruitee", "fivedegrees")]),
    ("Moneybird",           [("greenhouse", "moneybird"), ("lever", "moneybird"), ("recruitee", "moneybird")]),
    ("Nmbrs",               [("greenhouse", "nmbrs"), ("lever", "nmbrs"), ("recruitee", "nmbrs")]),
    ("Peak Capital",        [("greenhouse", "peakcapital"), ("lever", "peakcapital")]),
    ("Transsmart",          [("greenhouse", "transsmart"), ("lever", "transsmart"), ("recruitee", "transsmart")]),

    # ---- SaaS / B2B ----
    ("Whereby",             [("greenhouse", "whereby"), ("lever", "whereby")]),
    ("Trengo",              [("greenhouse", "trengo"), ("lever", "trengo"), ("recruitee", "trengo")]),
    ("Mews",                [("greenhouse", "mews"), ("lever", "mews")]),
    ("Bloomreach",          [("greenhouse", "bloomreach"), ("lever", "bloomreach")]),
    ("Spotler",             [("greenhouse", "spotler"), ("lever", "spotler"), ("recruitee", "spotler")]),
    ("Effectory",           [("greenhouse", "effectory"), ("lever", "effectory"), ("recruitee", "effectory")]),
    ("Copernica",           [("greenhouse", "copernica"), ("lever", "copernica"), ("recruitee", "copernica")]),
    ("Graydon",             [("greenhouse", "graydon"), ("lever", "graydon"), ("smartrecruiters", "Graydon")]),
    ("Framer",              [("greenhouse", "framer"), ("lever", "framer")]),
    ("Studocu",             [("greenhouse", "studocu"), ("lever", "studocu"), ("recruitee", "studocu")]),
    ("Magnet.me",           [("greenhouse", "magnetme"), ("lever", "magnetme"), ("recruitee", "magnetme")]),
    ("Crobox",              [("greenhouse", "crobox"), ("lever", "crobox"), ("recruitee", "crobox")]),
    ("Teamblue",            [("greenhouse", "teamblue"), ("lever", "teamblue"), ("smartrecruiters", "Teamblue")]),
    ("TransIP",             [("greenhouse", "transip"), ("lever", "transip"), ("recruitee", "transip")]),
    ("Foleon",              [("greenhouse", "foleon"), ("lever", "foleon"), ("recruitee", "foleon")]),
    ("Hatch",               [("greenhouse", "hatch"), ("lever", "hatch")]),
    ("Productboard",        [("greenhouse", "productboard"), ("lever", "productboard")]),
    ("Qualified",           [("greenhouse", "qualified"), ("lever", "qualified")]),
    ("Leadfeeder",          [("greenhouse", "leadfeeder"), ("lever", "leadfeeder")]),
    ("Plek",                [("greenhouse", "plek"), ("lever", "plek"), ("recruitee", "plek")]),
    ("Found.co",            [("greenhouse", "found"), ("lever", "found"), ("recruitee", "found")]),
    ("CloudSuite",          [("greenhouse", "cloudsuite"), ("lever", "cloudsuite"), ("recruitee", "cloudsuite")]),

    # ---- E-commerce / Marketplace ----
    ("Fonq",                [("greenhouse", "fonq"), ("lever", "fonq"), ("recruitee", "fonq")]),
    ("Bloomon",             [("greenhouse", "bloomon"), ("lever", "bloomon"), ("recruitee", "bloomon")]),
    ("Ace & Tate",          [("greenhouse", "aceandtate"), ("lever", "aceandtate"), ("recruitee", "aceandtate")]),
    ("Werkspot",            [("greenhouse", "werkspot"), ("lever", "werkspot"), ("recruitee", "werkspot")]),
    ("Productsup",          [("greenhouse", "productsup"), ("lever", "productsup")]),
    ("EffectConnect",       [("greenhouse", "effectconnect"), ("lever", "effectconnect"), ("recruitee", "effectconnect")]),
    ("Thuisbezorgd",        [("greenhouse", "thuisbezorgd"), ("lever", "thuisbezorgd")]),
    ("Greetz",              [("greenhouse", "greetz"), ("lever", "greetz"), ("recruitee", "greetz")]),
    ("Omoda",               [("greenhouse", "omoda"), ("lever", "omoda"), ("smartrecruiters", "Omoda")]),
    ("Ottonova",            [("greenhouse", "ottonova"), ("lever", "ottonova")]),
    ("Debijenkorf",         [("greenhouse", "debijenkorf"), ("lever", "debijenkorf"), ("smartrecruiters", "DeBijenkorf")]),

    # ---- Healthtech ----
    ("Luscii",              [("greenhouse", "luscii"), ("lever", "luscii"), ("recruitee", "luscii")]),
    ("Siilo",               [("greenhouse", "siilo"), ("lever", "siilo"), ("recruitee", "siilo")]),
    ("Aidence",             [("greenhouse", "aidence"), ("lever", "aidence"), ("recruitee", "aidence")]),
    ("Castor",              [("greenhouse", "castoredc"), ("greenhouse", "castor"), ("lever", "castor")]),
    ("SkinVision",          [("greenhouse", "skinvision"), ("lever", "skinvision"), ("recruitee", "skinvision")]),
    ("Healthblocks",        [("greenhouse", "healthblocks"), ("lever", "healthblocks"), ("recruitee", "healthblocks")]),
    ("Orikami",             [("greenhouse", "orikami"), ("lever", "orikami"), ("recruitee", "orikami")]),
    ("Pharmi",              [("greenhouse", "pharmi"), ("lever", "pharmi"), ("recruitee", "pharmi")]),

    # ---- Logistics / Mobility ----
    ("Trunkrs",             [("greenhouse", "trunkrs"), ("lever", "trunkrs"), ("recruitee", "trunkrs")]),
    ("Paazl",               [("greenhouse", "paazl"), ("lever", "paazl"), ("recruitee", "paazl")]),
    ("Wuunder",             [("greenhouse", "wuunder"), ("lever", "wuunder"), ("recruitee", "wuunder")]),
    ("Quicargo",            [("greenhouse", "quicargo"), ("lever", "quicargo"), ("recruitee", "quicargo")]),
    ("Felyx",               [("greenhouse", "felyx"), ("lever", "felyx"), ("recruitee", "felyx")]),
    ("VanMoof",             [("greenhouse", "vanmoof"), ("lever", "vanmoof")]),
    ("Dott",                [("greenhouse", "dott"), ("lever", "dott")]),
    ("Flitsmeister",        [("greenhouse", "flitsmeister"), ("lever", "flitsmeister"), ("recruitee", "flitsmeister")]),
    ("Saloodo",             [("greenhouse", "saloodo"), ("lever", "saloodo")]),

    # ---- Deep Tech / Hardware ----
    ("QuantWare",           [("greenhouse", "quantware"), ("lever", "quantware"), ("recruitee", "quantware")]),
    ("Qblox",               [("greenhouse", "qblox"), ("lever", "qblox"), ("recruitee", "qblox")]),
    ("PhotonDelta",         [("greenhouse", "photondelta"), ("lever", "photondelta"), ("recruitee", "photondelta")]),
    ("Delft Circuits",      [("greenhouse", "delftcircuits"), ("lever", "delftcircuits"), ("recruitee", "delftcircuits")]),
    ("Applied Nanolayers",  [("greenhouse", "appliednanolayers"), ("lever", "appliednanolayers")]),
    ("Mapper",              [("greenhouse", "mapper"), ("lever", "mapper")]),
    ("Hardt Hyperloop",     [("greenhouse", "hardt"), ("lever", "hardt"), ("recruitee", "hardt")]),
    ("Nearfield Instruments", [("greenhouse", "nearfieldinstruments"), ("lever", "nearfieldinstruments")]),
    ("Scyfer",              [("greenhouse", "scyfer"), ("lever", "scyfer")]),
    ("Plumerai",            [("greenhouse", "plumerai"), ("lever", "plumerai"), ("recruitee", "plumerai")]),

    # ---- HR Tech / Recruitment ----
    ("Homerun",             [("greenhouse", "homerun"), ("lever", "homerun"), ("recruitee", "homerun")]),
    ("TestGorilla",         [("greenhouse", "testgorilla"), ("lever", "testgorilla")]),
    ("Harver",              [("greenhouse", "harver"), ("lever", "harver")]),
    ("HoorayHR",            [("greenhouse", "hoorayhr"), ("lever", "hoorayhr"), ("recruitee", "hoorayhr")]),
    ("Recruitee",           [("greenhouse", "recruitee"), ("lever", "recruitee")]),
    ("RecruitNow",          [("greenhouse", "recruitnow"), ("lever", "recruitnow"), ("recruitee", "recruitnow")]),
    ("Joboti",              [("greenhouse", "joboti"), ("lever", "joboti"), ("recruitee", "joboti")]),

    # ---- Cybersecurity ----
    ("Zivver",              [("greenhouse", "zivver"), ("lever", "zivver"), ("recruitee", "zivver")]),
    ("Hadrian",             [("greenhouse", "hadrian"), ("lever", "hadrian")]),
    ("EyeSecurity",         [("greenhouse", "eyesecurity"), ("lever", "eyesecurity"), ("recruitee", "eyesecurity")]),

    # ---- Gaming / Media ----
    ("Nixxes Software",     [("greenhouse", "nixxes"), ("lever", "nixxes")]),
    ("Triumph Studios",     [("greenhouse", "triumphstudios"), ("lever", "triumphstudios")]),
    ("Vertigo Games",       [("greenhouse", "vertigogames"), ("lever", "vertigogames")]),
    ("Force Field",         [("greenhouse", "forcefield"), ("lever", "forcefield")]),
    ("Rogue Games",         [("greenhouse", "roguegames"), ("lever", "roguegames")]),
    ("Paladin Studios",     [("greenhouse", "paladinstudios"), ("lever", "paladinstudios"), ("recruitee", "paladinstudios")]),

    # ---- Sustainability / Energy ----
    ("Greenchoice",         [("greenhouse", "greenchoice"), ("lever", "greenchoice"), ("smartrecruiters", "Greenchoice"), ("recruitee", "greenchoice")]),
    ("Eneco",               [("greenhouse", "eneco"), ("lever", "eneco"), ("smartrecruiters", "Eneco")]),
    ("EnergyWorx",          [("greenhouse", "energyworx"), ("lever", "energyworx"), ("recruitee", "energyworx")]),
    ("Solynta",             [("greenhouse", "solynta"), ("lever", "solynta"), ("recruitee", "solynta")]),

    # ---- Travel / Hospitality ----
    ("TravelBird",          [("greenhouse", "travelbird"), ("lever", "travelbird"), ("recruitee", "travelbird")]),
    ("Bidroom",             [("greenhouse", "bidroom"), ("lever", "bidroom"), ("recruitee", "bidroom")]),
    ("Corendon",            [("greenhouse", "corendon"), ("lever", "corendon"), ("smartrecruiters", "Corendon")]),

    # ---- Other NL Tech ----
    ("Habitos",             [("greenhouse", "habitos"), ("lever", "habitos"), ("recruitee", "habitos")]),
    ("StudentJob",          [("greenhouse", "studentjob"), ("lever", "studentjob"), ("recruitee", "studentjob")]),
    ("3D Hubs",             [("greenhouse", "3dhubs"), ("lever", "3dhubs")]),
    ("Hubs",                [("greenhouse", "hubs"), ("lever", "hubs")]),
    ("Speakap",             [("lever", "speakap"), ("recruitee", "speakap")]),
    ("Revue",               [("greenhouse", "revue"), ("lever", "revue")]),
    ("Karma",               [("greenhouse", "karma"), ("lever", "karma")]),
    ("Fixico",              [("greenhouse", "fixico"), ("lever", "fixico"), ("recruitee", "fixico")]),
    ("Sana Commerce",       [("greenhouse", "sanacommerce"), ("lever", "sanacommerce")]),
    ("Infosys NL",          [("greenhouse", "infosys"), ("smartrecruiters", "Infosys")]),
    ("Capgemini NL",        [("greenhouse", "capgemini"), ("smartrecruiters", "Capgemini")]),
    ("Cognizant NL",        [("greenhouse", "cognizant"), ("smartrecruiters", "Cognizant")]),
    ("Accenture NL",        [("greenhouse", "accenture"), ("smartrecruiters", "Accenture")]),
    ("Ordina",              [("greenhouse", "ordina"), ("lever", "ordina"), ("smartrecruiters", "Ordina")]),
    ("Atos NL",             [("greenhouse", "atos"), ("smartrecruiters", "Atos")]),
    ("Sogeti NL",           [("greenhouse", "sogeti"), ("smartrecruiters", "Sogeti")]),
    ("CGI NL",              [("greenhouse", "cgi"), ("smartrecruiters", "CGI")]),

    # ---- Global tech with likely NL offices (not in discover.py) ----
    ("Spotify",             [("greenhouse", "spotify"), ("lever", "spotify")]),
    ("Atlassian",           [("greenhouse", "atlassian"), ("lever", "atlassian")]),
    ("Figma",               [("greenhouse", "figma"), ("lever", "figma")]),
    ("Notion",              [("greenhouse", "notion"), ("lever", "notion")]),
    ("Canva",               [("greenhouse", "canva"), ("lever", "canva")]),
    ("Toast",               [("greenhouse", "toast"), ("lever", "toast")]),
    ("Vercel",              [("greenhouse", "vercel"), ("lever", "vercel")]),
    ("Grafana Labs",        [("greenhouse", "grafanalabs"), ("lever", "grafanalabs")]),
    ("JetBrains",           [("greenhouse", "jetbrains"), ("lever", "jetbrains")]),
    ("Snyk",                [("greenhouse", "snyk"), ("lever", "snyk")]),
    ("Auth0",               [("greenhouse", "auth0"), ("lever", "auth0")]),
    ("Cockroach Labs",      [("greenhouse", "cockroachlabs"), ("lever", "cockroachlabs")]),
    ("CircleCI",            [("greenhouse", "circleci"), ("lever", "circleci")]),
    ("LaunchDarkly",        [("greenhouse", "launchdarkly"), ("lever", "launchdarkly")]),
    ("Kong",                [("greenhouse", "kong"), ("lever", "kong")]),
    ("Segment",             [("greenhouse", "segment"), ("lever", "segment")]),
    ("Algolia",             [("greenhouse", "algolia"), ("lever", "algolia")]),
    ("MessageBird/Bird",    [("greenhouse", "bird")]),
    ("Airbnb",              [("greenhouse", "airbnb"), ("lever", "airbnb")]),
    ("Salesforce",          [("greenhouse", "salesforce"), ("smartrecruiters", "Salesforce")]),
    ("Oracle NL",           [("smartrecruiters", "Oracle"), ("greenhouse", "oracle")]),
    ("SAP NL",              [("greenhouse", "sap"), ("smartrecruiters", "SAP")]),
    ("Red Hat NL",          [("greenhouse", "redhat"), ("smartrecruiters", "RedHat")]),
    ("VMware NL",           [("greenhouse", "vmware"), ("smartrecruiters", "VMware")]),
    ("ServiceNow",          [("greenhouse", "servicenow"), ("smartrecruiters", "ServiceNow")]),
    ("Snowflake",           [("greenhouse", "snowflake"), ("lever", "snowflake")]),
    ("Palantir",            [("greenhouse", "palantir"), ("lever", "palantir")]),
    ("Zoom",                [("greenhouse", "zoom"), ("lever", "zoom")]),
    ("Unity",               [("greenhouse", "unity"), ("lever", "unity")]),
    ("Epic Games",          [("greenhouse", "epicgames"), ("lever", "epicgames")]),
    ("Booking Holdings",    [("greenhouse", "bookingholdings"), ("smartrecruiters", "BookingHoldings")]),
    ("Didi",                [("greenhouse", "didi"), ("lever", "didi")]),
    ("Yandex NL",           [("greenhouse", "yandex"), ("lever", "yandex")]),

    # ---- Additional NL scale-ups ----
    ("Elastic Path",        [("greenhouse", "elasticpath"), ("lever", "elasticpath")]),
    ("WePayIt",             [("greenhouse", "wepayit"), ("lever", "wepayit"), ("recruitee", "wepayit")]),
    ("Nextail",             [("greenhouse", "nextail"), ("lever", "nextail")]),
    ("Docplanner",          [("greenhouse", "docplanner"), ("lever", "docplanner")]),
    ("Roamler",             [("greenhouse", "roamler"), ("lever", "roamler"), ("recruitee", "roamler")]),
    ("Loyals",              [("greenhouse", "loyals"), ("lever", "loyals"), ("recruitee", "loyals")]),
    ("Lightyear Solar",     [("greenhouse", "lightyearsolar"), ("lever", "lightyearsolar")]),
    ("VIBES",               [("greenhouse", "vibes"), ("lever", "vibes"), ("recruitee", "vibes")]),
    ("Kega",                [("greenhouse", "kega"), ("lever", "kega"), ("recruitee", "kega")]),
    ("ActiveCampaign NL",   [("greenhouse", "activecampaign"), ("lever", "activecampaign")]),
    ("monday.com NL",       [("greenhouse", "mondaycom"), ("lever", "mondaycom")]),
    ("HubSpot NL",          [("greenhouse", "hubspot"), ("lever", "hubspot")]),
    ("Typeform NL",         [("greenhouse", "typeform"), ("lever", "typeform")]),
    ("Pipedrive NL",        [("greenhouse", "pipedrive"), ("lever", "pipedrive")]),
]


# ---------------------------------------------------------------------------
# Token mining from existing scraped data
# ---------------------------------------------------------------------------
def mine_tokens_from_db(
    conn: sqlite3.Connection, logger: logging.Logger
) -> list[tuple[str, str, str]]:
    """
    Extract ATS board tokens from URLs stored in scraped_jobs and companies tables.
    Returns list of (ats_type, board_token, source_url).
    """
    all_urls: set[str] = set()

    # scraped_jobs URLs
    try:
        rows = conn.execute(
            "SELECT DISTINCT apply_url FROM scraped_jobs WHERE apply_url IS NOT NULL"
        ).fetchall()
        for r in rows:
            if r[0]:
                all_urls.add(r[0])

        rows = conn.execute(
            "SELECT DISTINCT career_url FROM scraped_jobs WHERE career_url IS NOT NULL"
        ).fetchall()
        for r in rows:
            if r[0]:
                all_urls.add(r[0])
    except sqlite3.OperationalError:
        logger.debug("  scraped_jobs table not found, skipping URL mining from scraped jobs")

    # careers_page tokens (some are ATS redirect URLs)
    try:
        rows = conn.execute(
            "SELECT token FROM companies WHERE source='careers_page'"
        ).fetchall()
        for r in rows:
            if r[0]:
                all_urls.add(r[0])
    except sqlite3.OperationalError:
        pass

    # jobs table URLs
    try:
        rows = conn.execute(
            "SELECT DISTINCT url FROM jobs WHERE url IS NOT NULL AND url != ''"
        ).fetchall()
        for r in rows:
            if r[0]:
                all_urls.add(r[0])
    except sqlite3.OperationalError:
        logger.debug("  jobs table not found, skipping URL mining from jobs")

    logger.debug(f"  Mining tokens from {len(all_urls)} unique URLs")

    mined: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    for url in all_urls:
        for pattern, ats_type in _ATS_URL_PATTERNS:
            m = pattern.search(url)
            if m:
                token = m.group(1).lower().strip()
                if token and len(token) > 1:
                    key = (ats_type, token)
                    if key not in seen:
                        seen.add(key)
                        mined.append((ats_type, token, url))
                break

    logger.info(f"  Mined {len(mined)} unique ATS tokens from existing data")
    return mined


# ---------------------------------------------------------------------------
# Netherlands location detection
# ---------------------------------------------------------------------------
def _is_nl_location(location_raw: str) -> bool:
    """Check if a location string refers to the Netherlands."""
    if not location_raw:
        return False
    text = location_raw.lower()

    # Direct country/code mentions
    for kw in _NL_KEYWORDS:
        if kw in text:
            return True

    # Dutch city names
    for city in _NL_CITIES:
        if city in text:
            return True

    return False


# ---------------------------------------------------------------------------
# NL confidence scoring
# ---------------------------------------------------------------------------
def _nl_confidence(
    nl_jobs: int,
    total_jobs: int,
    min_nl_jobs: int = 2,
    min_nl_ratio: float = 0.10,
    max_total_jobs: int = 500,
) -> str:
    """
    Classify NL relevance as HIGH / MED / LOW.

    HIGH = strong NL presence (NL-native or large NL office)
    MED  = meaningful NL presence (worth tracking)
    LOW  = incidental NL jobs (global company with tiny NL footprint)
    """
    if total_jobs == 0 or nl_jobs == 0:
        return "low"

    ratio = nl_jobs / total_jobs

    # Huge boards with few NL jobs are low-signal unless they beat the ratio
    if total_jobs > max_total_jobs and ratio < min_nl_ratio:
        return "low"

    # HIGH: >= 5 NL jobs, OR (>= 25% NL ratio AND >= 2 NL jobs)
    if nl_jobs >= 5 or (ratio >= 0.25 and nl_jobs >= 2):
        return "high"

    # MED: >= min_nl_jobs, OR (>= min_nl_ratio AND >= 1 NL job)
    if nl_jobs >= min_nl_jobs or (ratio >= min_nl_ratio and nl_jobs >= 1):
        return "med"

    return "low"


# ---------------------------------------------------------------------------
# Combined probe + NL job check  (single API call per token)
# ---------------------------------------------------------------------------
def probe_and_check_nl(
    ats_type: str, token: str
) -> tuple[bool, str | None, int, int]:
    """
    Probe an ATS board AND count NL-located jobs in one API call.

    Returns:
        (board_exists, board_name, nl_job_count, total_job_count)
    """
    try:
        if ats_type == "greenhouse":
            r = SESSION.get(
                f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
                timeout=15,
            )
            if r.status_code != 200:
                return (False, None, 0, 0)
            data = r.json()
            jobs = data.get("jobs", [])
            total = len(jobs)
            nl = sum(
                1 for j in jobs
                if _is_nl_location((j.get("location") or {}).get("name", ""))
            )
            # Board name requires separate call; defer to caller
            return (True, None, nl, total)

        elif ats_type == "lever":
            r = SESSION.get(
                f"https://api.lever.co/v0/postings/{token}?mode=json",
                timeout=15,
            )
            if r.status_code != 200:
                return (False, None, 0, 0)
            data = r.json()
            jobs = data if isinstance(data, list) else data.get("data", [])
            total = len(jobs)
            nl = sum(
                1 for j in jobs
                if _is_nl_location(
                    (j.get("categories") or {}).get("location", "")
                )
            )
            return (True, None, nl, total)

        elif ats_type == "smartrecruiters":
            r = SESSION.get(
                f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
                timeout=15,
            )
            if r.status_code != 200:
                return (False, None, 0, 0)
            data = r.json()
            jobs = data.get("content", [])
            total = len(jobs)
            nl = 0
            for j in jobs:
                loc = j.get("location") or {}
                city = loc.get("city", "")
                country = loc.get("country", "")
                loc_raw = f"{city}, {country}".strip(", ")
                if _is_nl_location(loc_raw):
                    nl += 1
            return (True, None, nl, total)

        elif ats_type == "recruitee":
            r = SESSION.get(
                f"https://{token}.recruitee.com/api/offers/",
                timeout=15,
            )
            if r.status_code != 200:
                return (False, None, 0, 0)
            data = r.json()
            jobs = data.get("offers", [])
            total = len(jobs)
            nl = 0
            for j in jobs:
                loc = j.get("location", "")
                country = j.get("country", "")
                if _is_nl_location(loc) or _is_nl_location(country):
                    nl += 1
            return (True, None, nl, total)

    except Exception:
        pass

    return (False, None, 0, 0)


# ---------------------------------------------------------------------------
# Exclusion set builder
# ---------------------------------------------------------------------------
def build_exclusion_set(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """
    Build a set of (source, token) pairs that are already known,
    so we don't re-probe them.
    """
    excluded: set[tuple[str, str]] = set()

    # From companies table
    try:
        rows = conn.execute("SELECT source, token FROM companies").fetchall()
        for source, token in rows:
            excluded.add((source, token.lower()))
    except sqlite3.OperationalError:
        pass

    # From ats_reverse_candidates (already processed/rejected)
    try:
        rows = conn.execute(
            "SELECT ats_type, board_token FROM ats_reverse_candidates WHERE status != 'new'"
        ).fetchall()
        for ats_type, board_token in rows:
            excluded.add((ats_type, board_token.lower()))
    except sqlite3.OperationalError:
        pass

    return excluded


# ---------------------------------------------------------------------------
# DB tracking helper
# ---------------------------------------------------------------------------
def _upsert_candidate(
    conn: sqlite3.Connection,
    ats_type: str,
    token: str,
    company_name: str | None = None,
    domain: str | None = None,
    nl_job_count: int = 0,
    total_job_count: int = 0,
    nl_confidence: str | None = None,
    status: str = "new",
    reject_reason: str | None = None,
):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO ats_reverse_candidates
               (ats_type, board_token, company_name, domain,
                nl_job_count, total_job_count, nl_confidence,
                detected_at, processed_at, status, reject_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(ats_type, board_token) DO UPDATE SET
               company_name = COALESCE(excluded.company_name, company_name),
               domain = COALESCE(excluded.domain, domain),
               nl_job_count = excluded.nl_job_count,
               total_job_count = excluded.total_job_count,
               nl_confidence = excluded.nl_confidence,
               processed_at = excluded.processed_at,
               status = excluded.status,
               reject_reason = excluded.reject_reason""",
        (
            ats_type, token, company_name, domain,
            nl_job_count, total_job_count, nl_confidence,
            now, now, status, reject_reason,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def run_reverse_discovery(
    ats_filter: str = "all",
    mine_tokens: bool = False,
    limit: int = 0,
    dry_run: bool = False,
    min_nl_jobs: int = 2,
    min_nl_ratio: float = 0.10,
    max_total_jobs: int = 500,
    allow_low_signal: bool = False,
):
    logger = setup_logging()
    tag = "[DRY RUN] " if dry_run else ""
    logger.info(f"{tag}ATS reverse discovery starting  ats={ats_filter}  mine={mine_tokens}")
    logger.info(
        f"  Thresholds: min_nl_jobs={min_nl_jobs}  min_nl_ratio={min_nl_ratio}  "
        f"max_total_jobs={max_total_jobs}  allow_low={allow_low_signal}"
    )

    conn = sqlite3.connect(DB_FILE)
    ensure_db(conn)
    ensure_reverse_candidates_table(conn)

    stats = {
        "total_probed": 0,
        "valid_boards": 0,
        "nl_matches": 0,
        "nl_high": 0,
        "nl_med": 0,
        "nl_low": 0,
        "added": 0,
        "skipped_known": 0,
        "rejected_not_found": 0,
        "rejected_zero_jobs": 0,
        "rejected_no_nl": 0,
        "rejected_low_signal": 0,
        "errors": 0,
    }

    # ---- Step 1: Build exclusion set ----
    excluded = build_exclusion_set(conn)
    logger.info(f"Exclusion set: {len(excluded)} known (source, token) pairs")

    # ---- Step 2: Build candidate list ----
    candidates: list[tuple[str, str, str]] = []  # (ats_type, token, company_hint)

    # From seed list
    ats_types = (
        list(PROBERS.keys()) if ats_filter == "all"
        else [ats_filter]
    )

    for company_hint, token_list in NL_SEED_TOKENS:
        for ats_type, token in token_list:
            if ats_filter != "all" and ats_type != ats_filter:
                continue
            if (ats_type, token.lower()) in excluded:
                stats["skipped_known"] += 1
                continue
            candidates.append((ats_type, token, company_hint))

    seed_count = len(candidates)
    logger.info(f"Seed list: {seed_count} tokens to probe (after excluding known)")

    # ---- Step 3: Optionally mine tokens from DB ----
    if mine_tokens:
        mined = mine_tokens_from_db(conn, logger)
        for ats_type, token, source_url in mined:
            if ats_filter != "all" and ats_type != ats_filter:
                continue
            if (ats_type, token.lower()) in excluded:
                continue
            # Avoid duplicating seed list entries
            if (ats_type, token) not in {(c[0], c[1]) for c in candidates}:
                candidates.append((ats_type, token, f"mined:{source_url[:50]}"))

        mined_added = len(candidates) - seed_count
        if mined_added > 0:
            logger.info(f"Mined tokens: {mined_added} additional candidates")

    # Apply limit
    if limit > 0 and len(candidates) > limit:
        candidates = candidates[:limit]
        logger.info(f"Limited to first {limit} candidates")

    logger.info(f"Total candidates to probe: {len(candidates)}")
    if not candidates:
        logger.info("Nothing to probe. All tokens already known.")
        conn.close()
        return

    # ---- Step 4: Probe each candidate ----
    for i, (ats_type, token, hint) in enumerate(candidates, 1):
        stats["total_probed"] += 1

        try:
            board_exists, _, nl_count, total = probe_and_check_nl(ats_type, token)
        except Exception as e:
            logger.warning(f"  {i:>4}. ERROR  {ats_type}/{token}  ({hint})  -- {e}")
            stats["errors"] += 1
            if not dry_run:
                _upsert_candidate(
                    conn, ats_type, token,
                    status="error", reject_reason=str(e)[:100],
                )
            time.sleep(0.5)
            continue

        if not board_exists:
            logger.debug(f"  {i:>4}. --     {ats_type}/{token}  ({hint})  board not found")
            stats["rejected_not_found"] += 1
            if not dry_run:
                _upsert_candidate(
                    conn, ats_type, token,
                    company_name=hint, status="rejected",
                    reject_reason="board_not_found",
                )
            time.sleep(0.5)
            continue

        stats["valid_boards"] += 1

        if total == 0:
            logger.info(f"  {i:>4}. EMPTY  {ats_type}/{token}  ({hint})  0 jobs")
            stats["rejected_zero_jobs"] += 1
            if not dry_run:
                _upsert_candidate(
                    conn, ats_type, token,
                    company_name=hint, total_job_count=0,
                    status="rejected", reject_reason="zero_jobs",
                )
            time.sleep(0.5)
            continue

        if nl_count == 0:
            logger.info(
                f"  {i:>4}. NO-NL  {ats_type}/{token}  ({hint})  "
                f"{total} jobs, 0 NL"
            )
            stats["rejected_no_nl"] += 1
            if not dry_run:
                _upsert_candidate(
                    conn, ats_type, token,
                    company_name=hint, nl_job_count=0,
                    total_job_count=total,
                    status="rejected", reject_reason="no_nl_jobs",
                )
            time.sleep(0.5)
            continue

        # ---- NL match! Compute confidence tier ----
        stats["nl_matches"] += 1
        tier = _nl_confidence(
            nl_count, total,
            min_nl_jobs=min_nl_jobs,
            min_nl_ratio=min_nl_ratio,
            max_total_jobs=max_total_jobs,
        )
        stats[f"nl_{tier}"] += 1
        ratio_pct = nl_count / total * 100 if total else 0

        # Get display name from ATS
        board_name = _fetch_board_name(ats_type, token)
        company_name = board_name or hint
        if company_name.startswith("mined:"):
            company_name = token

        # Reject LOW unless --allow-low-signal
        if tier == "low" and not allow_low_signal:
            logger.info(
                f"  {i:>4}. ~LOW   {ats_type}/{token}  \"{company_name}\"  "
                f"{nl_count}/{total} NL ({ratio_pct:.0f}%) -- rejected"
            )
            stats["rejected_low_signal"] += 1
            if not dry_run:
                _upsert_candidate(
                    conn, ats_type, token,
                    company_name=company_name, nl_job_count=nl_count,
                    total_job_count=total, nl_confidence=tier,
                    status="rejected", reject_reason="nl_low_signal",
                )
            time.sleep(0.5)
            continue

        tier_tag = tier.upper()
        logger.info(
            f"  {i:>4}. +{tier_tag:<4}  {ats_type}/{token}  \"{company_name}\"  "
            f"{nl_count}/{total} NL ({ratio_pct:.0f}%)"
        )

        if dry_run:
            stats["added"] += 1
            time.sleep(0.5)
            continue

        # Check duplicates before inserting
        if company_exists(conn, company_name) or company_exists(conn, normalize_name(company_name)):
            logger.info(f"         Already in DB by name: {company_name}")
            stats["skipped_known"] += 1
            _upsert_candidate(
                conn, ats_type, token,
                company_name=company_name, nl_job_count=nl_count,
                total_job_count=total, nl_confidence=tier,
                status="processed", reject_reason="already_in_db",
            )
        else:
            added = add_company_to_db(
                conn, company_name, ats_type, token, confidence=tier,
            )
            if added:
                stats["added"] += 1
                _upsert_candidate(
                    conn, ats_type, token,
                    company_name=company_name, nl_job_count=nl_count,
                    total_job_count=total, nl_confidence=tier,
                    status="processed",
                )
            else:
                logger.info(f"         Already in DB by token: {ats_type}/{token}")
                stats["skipped_known"] += 1
                _upsert_candidate(
                    conn, ats_type, token,
                    company_name=company_name, nl_job_count=nl_count,
                    total_job_count=total, nl_confidence=tier,
                    status="processed", reject_reason="token_exists",
                )

        time.sleep(0.5)

    # ---- Summary ----
    conn.close()
    _print_summary(logger, stats, dry_run)


def _print_summary(logger: logging.Logger, stats: dict, dry_run: bool):
    add_verb = "Would add" if dry_run else "Added"
    logger.info("")
    logger.info("=" * 62)
    logger.info(f"  Total boards probed:        {stats['total_probed']}")
    logger.info(f"  Valid boards found:          {stats['valid_boards']}")
    logger.info(f"  NL matches:                  {stats['nl_matches']}")
    logger.info(f"    HIGH confidence:           {stats['nl_high']}")
    logger.info(f"    MED  confidence:           {stats['nl_med']}")
    logger.info(f"    LOW  confidence:           {stats['nl_low']}")
    logger.info(f"  {add_verb} to companies:    {stats['added']}")
    logger.info(f"  Skipped (already known):     {stats['skipped_known']}")
    logger.info(f"  Rejected (board not found):  {stats['rejected_not_found']}")
    logger.info(f"  Rejected (0 jobs):           {stats['rejected_zero_jobs']}")
    logger.info(f"  Rejected (no NL jobs):       {stats['rejected_no_nl']}")
    logger.info(f"  Rejected (low NL signal):    {stats['rejected_low_signal']}")
    logger.info(f"  Errors:                      {stats['errors']}")
    if dry_run:
        logger.info("  (dry-run -- nothing written)")
    logger.info("=" * 62)

    summary = (
        f"SUMMARY probed={stats['total_probed']} "
        f"valid={stats['valid_boards']} "
        f"nl={stats['nl_matches']}(H{stats['nl_high']}/M{stats['nl_med']}/L{stats['nl_low']}) "
        f"added={stats['added']} "
        f"low_rejected={stats['rejected_low_signal']} "
        f"skipped={stats['skipped_known']} "
        f"errors={stats['errors']}"
    )
    logger.info(summary)

    log_file = LOG_DIR / f"ats_reverse_{datetime.now().strftime('%Y%m%d')}.log"
    logger.info(f"  Log: {log_file}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="ATS reverse discovery -- find NL tech companies by probing ATS board tokens"
    )
    parser.add_argument(
        "--ats", type=str, default="all",
        choices=["greenhouse", "lever", "smartrecruiters", "recruitee", "all"],
        help="Which ATS platform(s) to probe (default: all)",
    )
    parser.add_argument(
        "--mine-tokens", action="store_true",
        help="Also extract tokens from existing scraped job URLs",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Max number of tokens to probe (0 = unlimited)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show results without writing to DB",
    )
    parser.add_argument(
        "--min-nl-jobs", type=int, default=2,
        help="Minimum NL jobs for MED confidence (default: 2)",
    )
    parser.add_argument(
        "--min-nl-ratio", type=float, default=0.10,
        help="Minimum NL ratio for MED confidence (default: 0.10)",
    )
    parser.add_argument(
        "--max-total-jobs", type=int, default=500,
        help="Boards above this size need strong NL ratio to pass (default: 500)",
    )
    parser.add_argument(
        "--allow-low-signal", action="store_true",
        help="Also add LOW confidence companies (default: reject them)",
    )
    # Alias for --ats all
    parser.add_argument(
        "--all", action="store_true",
        help="Probe all ATS platforms (same as --ats all)",
    )

    args = parser.parse_args()

    run_reverse_discovery(
        ats_filter=args.ats,
        mine_tokens=args.mine_tokens,
        limit=args.limit,
        dry_run=args.dry_run,
        min_nl_jobs=args.min_nl_jobs,
        min_nl_ratio=args.min_nl_ratio,
        max_total_jobs=args.max_total_jobs,
        allow_low_signal=args.allow_low_signal,
    )
