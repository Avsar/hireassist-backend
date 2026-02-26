"""
kvk_discover.py - Discover companies via the KVK Zoeken API.

Searches the Dutch Chamber of Commerce (Kamer van Koophandel) registry
for companies in a given city.  Returns candidates in the same format as
google_discover.py / osm_discover.py so they can be fed into
agent_discover.py's pipeline.

Requires KVK_API_KEY in .env.

Usage (standalone test):
    python kvk_discover.py --city Eindhoven
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

API_URL = "https://api.kvk.nl/api/v2/zoeken"

# Search terms to find tech/IT/software companies
DEFAULT_QUERIES = [
    "software",
    "IT",
    "tech",
    "engineering",
    "data",
    "cyber",
    "digital",
    "cloud",
    "AI",
    "semiconductor",
    "electronics",
    "consulting",
    "solutions",
    "systems",
    "development",
]


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


def discover_kvk(
    city: str,
    queries: list[str] | None = None,
    max_pages: int = 3,
) -> list[dict]:
    """
    Discover companies in a city using the KVK Zoeken API.

    Returns list of dicts compatible with agent_discover.py's candidate format:
        {name, website, city, region, source, osm_id, raw_json}
    """
    api_key = os.environ.get("KVK_API_KEY", "")
    if not api_key:
        logger.error("KVK_API_KEY not set in environment")
        return []

    if queries is None:
        queries = DEFAULT_QUERIES

    seen_kvk: set[str] = set()
    candidates: list[dict] = []

    for query in queries:
        logger.info("  KVK query: naam='%s' plaats='%s'", query, city)

        for page in range(1, max_pages + 1):
            try:
                data = _search(api_key, query, city, page=page)
            except requests.RequestException as e:
                logger.warning("  KVK API error for '%s': %s", query, e)
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

            total = data.get("totaal", 0)
            fetched = page * 100
            if fetched >= total:
                break
            time.sleep(0.3)

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
    parser.add_argument("--queries", help="Comma-separated search terms")
    parser.add_argument("--max-pages", type=int, default=3, help="Max pages per query (default: 3)")
    args = parser.parse_args()

    queries = None
    if args.queries:
        queries = [q.strip() for q in args.queries.split(",")]

    results = discover_kvk(args.city, queries=queries, max_pages=args.max_pages)

    print(f"\n{'='*70}")
    print(f"Found {len(results)} unique companies in {args.city}")
    print(f"{'='*70}")
    for i, c in enumerate(results, 1):
        kvk = c["raw_json"].get("kvk_nummer", "")
        print(f"  {i:>3}. {c['name']:<50} KVK {kvk}")
