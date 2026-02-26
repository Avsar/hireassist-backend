"""
kvk_discover.py - Discover companies via the KVK Zoeken API.

Searches the Dutch Chamber of Commerce (Kamer van Koophandel) registry
for companies in a given city.  Returns candidates in the same format as
google_discover.py / osm_discover.py so they can be fed into
agent_discover.py's pipeline.

Two modes:
  - keyword mode (default): searches for tech-related keywords in company names
  - broad mode (--broad):   sweeps A-Z + "B.V." to find ALL companies regardless
                            of name, then lets candidate_filter score them

Requires KVK_API_KEY in .env.

Usage (standalone test):
    python kvk_discover.py --city Eindhoven
    python kvk_discover.py --city Eindhoven --broad
    python kvk_discover.py --city Eindhoven --queries "software,IT,tech"
"""

import argparse
import logging
import os
import string
import time

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger("kvk_discover")

API_URL = "https://api.kvk.nl/api/v2/zoeken"

# Search terms to discover companies across all industries.
# KVK API does whole-word matching, so include both short and full forms.
DEFAULT_QUERIES = [
    # --- Legal entity suffixes (catches most registered companies) ---
    "N.V.", "V.O.F.", "C.V.",
    # NOTE: "B.V." returns 18k+ results (over API cap) -- covered via keywords below
    # --- Common Dutch words (appear in thousands of company names) ---
    "de", "van", "en", "het",
    # --- Tech / IT ---
    "software", "IT", "tech", "technology", "technologies",
    "engineering", "data", "cyber", "digital", "cloud", "AI",
    "semiconductor", "electronics", "automation", "robotics",
    "analytics", "platform", "labs", "innovation", "innovations",
    # --- General business ---
    "consulting", "solutions", "systems", "development",
    "services", "management", "international", "partners", "bureau",
    "groep", "group", "ventures", "capital",
    # --- Dutch business terms ---
    "beheer", "advies", "diensten", "techniek", "automatisering",
    # --- Professional services ---
    "advocaten", "notaris", "accountant", "finance", "juridisch", "legal",
    "makelaars", "verzekeringen", "hypotheek",
    # --- Healthcare ---
    "zorg", "medical", "health", "pharma", "kliniek",
    # --- Manufacturing / industrial ---
    "productie", "manufacturing", "fabriek", "industrie", "metaal",
    # --- Logistics / transport ---
    "logistics", "transport",
    # --- Construction / real estate ---
    "bouw", "vastgoed", "architecten", "installatie",
    # --- Energy ---
    "energie", "energy", "solar", "power",
    # --- Media / creative ---
    "media", "design", "creative", "marketing", "communicatie",
    # --- Education / research ---
    "research", "onderwijs", "training", "academy",
]

# KVK API hard limit: max 10 pages x 100 results = 1,000 per query
MAX_API_PAGES = 10

# Letters that overflow 1,000 results when searched alone -- need two-letter drilldown
_OVERFLOW_LETTERS = {"B", "O", "V"}


def _build_broad_queries() -> list[str]:
    """Build broad sweep query list: single letters + two-letter drilldown for overflow letters."""
    queries = []
    for letter in string.ascii_uppercase:
        if letter in _OVERFLOW_LETTERS:
            # Drill down to two-letter combos (skip BV -- matches everything via "B.V.")
            for second in string.ascii_uppercase:
                combo = letter + second
                if combo == "BV":
                    continue  # "BV" matches all B.V. companies, caught by other queries
                queries.append(combo)
        else:
            queries.append(letter)
    # Also add digits 0-9
    queries.extend(str(d) for d in range(10))
    return queries


def _search(api_key: str, naam: str, plaats: str,
            page: int = 1, per_page: int = 100) -> dict:
    """Run a single KVK Zoeken request."""
    resp = requests.get(
        API_URL,
        params={
            "naam": naam,
            "plaats": plaats,
            "type": "hoofdvestiging",
            "resultatenPerPagina": per_page,
            "pagina": page,
        },
        headers={"apikey": api_key},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_query(api_key: str, query: str, city: str, max_pages: int,
                 seen_kvk: set, candidates: list) -> int:
    """Fetch all pages for a single query. Returns number of new candidates added."""
    added = 0
    for page in range(1, max_pages + 1):
        try:
            data = _search(api_key, query, city, page=page)
        except requests.RequestException as e:
            logger.warning("  KVK API error for '%s' page %d: %s", query, page, e)
            break

        results = data.get("resultaten", [])
        if not results:
            break

        for item in results:
            kvk_nr = item.get("kvkNummer", "")
            if not kvk_nr or kvk_nr in seen_kvk:
                continue
            seen_kvk.add(kvk_nr)

            name = item.get("naam", "")
            if not name:
                continue

            # Extract address
            addr = item.get("adres", {})
            if isinstance(addr, dict):
                inner = addr.get("binnenlandsAdres", {})
                plaats = inner.get("plaats", city) if isinstance(inner, dict) else city
            else:
                plaats = city

            candidates.append({
                "name": name,
                "website": None,  # KVK doesn't return websites
                "city": plaats or city,
                "region": city,
                "source": "kvk",
                "osm_id": f"kvk_{kvk_nr}",
                "raw_json": {
                    "kvk_nummer": kvk_nr,
                    "type": item.get("type", ""),
                    "actief": item.get("actief", ""),
                    "plaats": plaats,
                },
            })
            added += 1

        total = data.get("totaal", 0)
        fetched = page * 100
        if fetched >= total:
            break
        time.sleep(0.3)

    return added


def discover_kvk(
    city: str,
    queries: list[str] | None = None,
    max_pages: int = 3,
    broad: bool = False,
) -> list[dict]:
    """
    Discover companies in a city using the KVK Zoeken API.

    Args:
        city: City name to search in
        queries: Custom keyword list (keyword mode only)
        max_pages: Max pages per query (keyword mode, default 3)
        broad: If True, do a broad A-Z + B.V. sweep (max 10 pages each)

    Returns list of dicts compatible with agent_discover.py's candidate format:
        {name, website, city, region, source, osm_id, raw_json}
    """
    api_key = os.environ.get("KVK_API_KEY", "")
    if not api_key:
        logger.error("KVK_API_KEY not set in environment")
        return []

    seen_kvk: set[str] = set()
    candidates: list[dict] = []

    if broad:
        # Broad sweep: single letters + two-letter drilldown for overflow letters
        sweep_queries = _build_broad_queries()
        pages_per_query = MAX_API_PAGES
        logger.info("KVK broad sweep: %d queries x up to %d pages for %s",
                     len(sweep_queries), pages_per_query, city)
    else:
        # Keyword mode (original behavior)
        sweep_queries = queries if queries is not None else DEFAULT_QUERIES
        pages_per_query = max_pages

    for i, query in enumerate(sweep_queries, 1):
        logger.info("  [%d/%d] KVK query: naam='%s' plaats='%s'",
                     i, len(sweep_queries), query, city)

        added = _fetch_query(api_key, query, city, pages_per_query,
                             seen_kvk, candidates)

        if added:
            logger.info("    +%d new (total: %d)", added, len(candidates))

        time.sleep(0.3)  # rate limit between queries

    logger.info("KVK: %d unique candidates for %s", len(candidates), city)
    return candidates


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="KVK company discovery")
    parser.add_argument("--city", required=True, help="City to search in")
    parser.add_argument("--queries", help="Comma-separated search terms (keyword mode)")
    parser.add_argument("--max-pages", type=int, default=3, help="Max pages per query (default: 3)")
    parser.add_argument("--broad", action="store_true",
                        help="Broad sweep: A-Z + B.V. + 0-9 to find ALL companies")
    args = parser.parse_args()

    queries = None
    if args.queries:
        queries = [q.strip() for q in args.queries.split(",")]

    results = discover_kvk(args.city, queries=queries, max_pages=args.max_pages,
                           broad=args.broad)

    print(f"\n{'='*70}")
    print(f"Found {len(results)} unique companies in {args.city}")
    print(f"{'='*70}")
    for i, c in enumerate(results, 1):
        kvk = c["raw_json"].get("kvk_nummer", "")
        print(f"  {i:>3}. {c['name']:<50} KVK {kvk}")
