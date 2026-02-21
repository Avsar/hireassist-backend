"""
google_discover.py - Discover companies via Google Places API (New).

Searches for tech/IT/software companies in a given city/region using the
Google Places Text Search API.  Returns candidates in the same format as
osm_discover.py so they can be fed into agent_discover.py's pipeline.

Requires GOOGLE_PLACES_API_KEY in .env.

Usage (standalone test):
    python google_discover.py --city Eindhoven
    python google_discover.py --city Eindhoven --queries "software,IT,tech,engineering"
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

logger = logging.getLogger("google_discover")

API_URL = "https://places.googleapis.com/v1/places:searchText"

# Search queries to run per city -- each finds different companies
DEFAULT_QUERIES = [
    "technology company {city}",
    "software company {city}",
    "IT company {city}",
    "software development {city}",
    "engineering company {city}",
    "tech startup {city}",
    "data company {city}",
    "cybersecurity company {city}",
    "semiconductor company {city}",
    "electronics company {city}",
]

FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.websiteUri",
    "places.types",
    "places.primaryType",
    "nextPageToken",
])


def _search(api_key: str, query: str, page_token: str | None = None) -> dict:
    """Run a single Text Search request."""
    body: dict = {
        "textQuery": query,
        "languageCode": "en",
        "maxResultCount": 20,
    }
    if page_token:
        body["pageToken"] = page_token

    resp = requests.post(
        API_URL,
        headers={
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        },
        json=body,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def discover_google(
    city: str,
    queries: list[str] | None = None,
    max_pages: int = 2,
) -> list[dict]:
    """
    Discover companies in a city using Google Places API.

    Returns list of dicts compatible with agent_discover.py's candidate format:
        {name, website, city, region, source, osm_id, raw_json}
    """
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "")
    if not api_key:
        logger.error("GOOGLE_PLACES_API_KEY not set in environment")
        return []

    if queries is None:
        queries = DEFAULT_QUERIES

    seen_ids: set[str] = set()
    candidates: list[dict] = []

    for q_template in queries:
        query = q_template.format(city=city)
        logger.info("  Query: %s", query)

        page_token = None
        for page in range(max_pages):
            try:
                data = _search(api_key, query, page_token)
            except requests.RequestException as e:
                logger.warning("  API error for '%s': %s", query, e)
                break

            places = data.get("places", [])
            if not places:
                break

            for p in places:
                pid = p.get("id", "")
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)

                name = p.get("displayName", {}).get("text", "")
                if not name:
                    continue

                website = p.get("websiteUri", "")
                address = p.get("formattedAddress", "")

                candidates.append({
                    "name": name,
                    "website": website or None,
                    "city": city,
                    "region": city,
                    "source": "google_places",
                    "osm_id": f"gp_{pid}",
                    "raw_json": {
                        "google_place_id": pid,
                        "types": p.get("types", []),
                        "primary_type": p.get("primaryType", ""),
                        "address": address,
                    },
                })

            page_token = data.get("nextPageToken")
            if not page_token:
                break
            time.sleep(0.3)

        time.sleep(0.3)  # rate limit between queries

    logger.info("Google Places: %d unique candidates for %s", len(candidates), city)
    return candidates


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Google Places company discovery")
    parser.add_argument("--city", required=True, help="City to search in")
    parser.add_argument("--queries", help="Comma-separated query templates (use {city} placeholder)")
    parser.add_argument("--max-pages", type=int, default=2, help="Max pages per query (default: 2)")
    args = parser.parse_args()

    queries = None
    if args.queries:
        queries = [q.strip() + " {city}" if "{city}" not in q.strip() else q.strip()
                   for q in args.queries.split(",")]

    results = discover_google(args.city, queries=queries, max_pages=args.max_pages)

    print(f"\n{'='*70}")
    print(f"Found {len(results)} unique companies in {args.city}")
    print(f"{'='*70}")
    for i, c in enumerate(results, 1):
        web = c.get("website") or "(no website)"
        types = c["raw_json"].get("primary_type", "")
        print(f"  {i:>3}. {c['name']:<40} {web:<45} {types}")
