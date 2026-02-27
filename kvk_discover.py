"""
kvk_discover.py - Discover companies via the KVK Zoeken API.

Searches the Dutch Chamber of Commerce (Kamer van Koophandel) registry
for companies in a given city.  Returns candidates in the same format as
google_discover.py / osm_discover.py so they can be fed into
agent_discover.py's pipeline.

After discovery, enrich_with_basisprofiel() can fetch registered websites,
SBI codes, and employee counts from the KVK Basisprofiel API.

Requires KVK_API_KEY in .env.

Usage (standalone test):
    python kvk_discover.py --city Eindhoven
    python kvk_discover.py --city Eindhoven --enrich
    python kvk_discover.py --city Eindhoven --queries "software,IT,tech"
"""

import argparse
import logging
import os
import time

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger("kvk_discover")

ZOEKEN_URL = "https://api.kvk.nl/api/v2/zoeken"
BASISPROFIEL_URL = "https://api.kvk.nl/api/v1/basisprofielen"

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


def _search(api_key: str, naam: str, plaats: str,
            page: int = 1, per_page: int = 100) -> dict:
    """Run a single KVK Zoeken request."""
    resp = requests.get(
        ZOEKEN_URL,
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
                "website": None,  # filled by enrich_with_basisprofiel()
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


# ---------------------------------------------------------------------------
# Basisprofiel enrichment
# ---------------------------------------------------------------------------

def enrich_with_basisprofiel(candidates: list[dict]) -> int:
    """
    Enrich KVK candidates with website, SBI codes, and employee count
    from the KVK Basisprofiel API.

    Modifies candidates in-place.  Returns number of candidates enriched
    with a website.
    """
    api_key = os.environ.get("KVK_API_KEY", "")
    if not api_key:
        logger.error("KVK_API_KEY not set -- skipping enrichment")
        return 0

    to_enrich = [
        c for c in candidates
        if not c.get("website") and c.get("raw_json", {}).get("kvk_nummer")
    ]
    if not to_enrich:
        return 0

    logger.info("Enriching %d KVK candidates via Basisprofiel API...", len(to_enrich))
    enriched = 0

    for i, cand in enumerate(to_enrich, 1):
        kvk_nr = cand["raw_json"]["kvk_nummer"]
        try:
            resp = requests.get(
                f"{BASISPROFIEL_URL}/{kvk_nr}",
                headers={"apikey": api_key},
                timeout=15,
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.debug("  Basisprofiel error for KVK %s: %s", kvk_nr, e)
            continue

        # Extract website from hoofdvestiging
        hv = data.get("_embedded", {}).get("hoofdvestiging", {})
        websites = hv.get("websites", []) if hv else []
        if websites:
            cand["website"] = websites[0]
            enriched += 1

        # Extract SBI codes
        sbi = data.get("sbiActiviteiten", [])
        if sbi:
            cand["raw_json"]["sbi"] = [
                {"code": s.get("sbiCode", ""), "desc": s.get("sbiOmschrijving", "")}
                for s in sbi
            ]

        # Extract employee count
        emp = data.get("totaalWerkzamePersonen")
        if emp is not None:
            cand["raw_json"]["employees"] = emp

        if i % 100 == 0:
            logger.info("  Enriched %d/%d candidates (%d websites found)",
                        i, len(to_enrich), enriched)

        time.sleep(0.3)  # rate limit

    logger.info("Enrichment done: %d/%d candidates got websites",
                enriched, len(to_enrich))
    return enriched


# ---------------------------------------------------------------------------
# Main discovery function
# ---------------------------------------------------------------------------

def discover_kvk(
    city: str,
    queries: list[str] | None = None,
    max_pages: int = 3,
) -> list[dict]:
    """
    Discover companies in a city using the KVK Zoeken API.

    Args:
        city: City name to search in
        queries: Custom keyword list (overrides DEFAULT_QUERIES)
        max_pages: Max pages per query (default 3, max 10)

    Returns list of dicts compatible with agent_discover.py's candidate format:
        {name, website, city, region, source, osm_id, raw_json}
    """
    api_key = os.environ.get("KVK_API_KEY", "")
    if not api_key:
        logger.error("KVK_API_KEY not set in environment")
        return []

    seen_kvk: set[str] = set()
    candidates: list[dict] = []

    sweep_queries = queries if queries is not None else DEFAULT_QUERIES
    pages_per_query = min(max_pages, MAX_API_PAGES)

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
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="KVK company discovery")
    parser.add_argument("--city", required=True, help="City to search in")
    parser.add_argument("--queries", help="Comma-separated search terms (keyword mode)")
    parser.add_argument("--max-pages", type=int, default=3, help="Max pages per query (default: 3)")
    parser.add_argument("--enrich", action="store_true",
                        help="Enrich candidates with websites via Basisprofiel API")
    args = parser.parse_args()

    queries = None
    if args.queries:
        queries = [q.strip() for q in args.queries.split(",")]

    results = discover_kvk(args.city, queries=queries, max_pages=args.max_pages)

    if args.enrich:
        enrich_with_basisprofiel(results)

    print(f"\n{'='*70}")
    print(f"Found {len(results)} unique companies in {args.city}")
    with_web = sum(1 for c in results if c.get("website"))
    print(f"  {with_web} with websites")
    print(f"{'='*70}")
    for i, c in enumerate(results, 1):
        kvk = c["raw_json"].get("kvk_nummer", "")
        web = c.get("website") or ""
        print(f"  {i:>3}. {c['name']:<45} KVK {kvk}  {web}")
