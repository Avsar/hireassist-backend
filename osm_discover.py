#!/usr/bin/env python3
"""
osm_discover.py - Discover companies via OpenStreetMap Overpass API.

Free, no API key required. Queries for offices/companies in a named region
and returns structured candidate data.

Usage (as module):
    from osm_discover import discover_osm
    candidates = discover_osm("Eindhoven", limit=200)

Usage (standalone test):
    python osm_discover.py Eindhoven
    python osm_discover.py "Noord-Brabant" --limit 50
    python osm_discover.py Netherlands
"""

import argparse
import json
import re
import sys
import time

import requests

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# ---- Region name resolution ------------------------------------------------

# English -> Dutch/OSM name mapping
REGION_ALIASES = {
    "netherlands": "Nederland",
    "the netherlands": "Nederland",
    "holland": "Nederland",
    "nl": "Nederland",
    "north brabant": "Noord-Brabant",
    "south holland": "Zuid-Holland",
    "north holland": "Noord-Holland",
    "the hague": "'s-Gravenhage",
    "den haag": "'s-Gravenhage",
}

# Provinces where the name is unambiguous (not also a major city)
_PROVINCE_ONLY = {
    "Drenthe", "Flevoland", "Friesland", "Gelderland", "Limburg",
    "Noord-Brabant", "Noord-Holland", "Overijssel", "Zeeland", "Zuid-Holland",
}

# Major Dutch cities (admin_level=8). For "Utrecht" and "Groningen" which are
# both city and province, we default to city -- users can use the province
# name explicitly (e.g. "Provincie Utrecht") or it will be in _PROVINCE_ONLY.
_CITIES = {
    "Amsterdam", "Rotterdam", "'s-Gravenhage", "Eindhoven", "Tilburg",
    "Almere", "Breda", "Nijmegen", "Enschede", "Haarlem", "Arnhem",
    "Zaanstad", "Amersfoort", "Delft", "Leiden", "Maastricht",
    "Utrecht", "Groningen", "Leeuwarden", "Apeldoorn", "Hilversum",
    "Dordrecht", "Zwolle", "Deventer", "Helmond", "Veldhoven",
    "Wageningen", "Ede", "Venlo",
}


def _resolve_region(region: str) -> tuple[str, int | None]:
    """Map a user-supplied region string to (osm_name, admin_level)."""
    key = region.lower().strip()
    name = REGION_ALIASES.get(key, region.strip())

    if name == "Nederland":
        return name, 2
    if name in _PROVINCE_ONLY:
        return name, 4
    if name in _CITIES:
        return name, 8
    # Unknown region -- let Overpass try without admin_level constraint
    return name, None


# ---- Overpass query building ------------------------------------------------

def _build_query(region: str, timeout: int = 120) -> str:
    """Build an Overpass QL query for offices/companies in *region*."""
    name, admin_level = _resolve_region(region)

    if admin_level is not None:
        area_line = (
            f'area["name"="{name}"]["admin_level"="{admin_level}"]->.a;'
        )
    else:
        area_line = f'area["name"="{name}"]->.a;'

    # Tags requested: office=*, company=*, industrial=*, brand=*, amenity=company
    # We require ["name"] on every selector so unnamed polygons are skipped.
    return f"""[out:json][timeout:{timeout}];
{area_line}
(
  node["office"]["name"](area.a);
  way["office"]["name"](area.a);
  relation["office"]["name"](area.a);
  node["company"]["name"](area.a);
  way["company"]["name"](area.a);
  node["industrial"]["name"](area.a);
  way["industrial"]["name"](area.a);
  node["brand"]["name"](area.a);
  way["brand"]["name"](area.a);
  node["amenity"="company"]["name"](area.a);
  way["amenity"="company"]["name"](area.a);
);
out body center;"""


# ---- Overpass HTTP client ---------------------------------------------------

def _query_overpass(query: str, max_retries: int = 3) -> list[dict]:
    """POST *query* to Overpass API, return list of elements."""
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                OVERPASS_URL,
                data={"data": query},
                timeout=180,
                headers={"User-Agent": "HireAssist/0.2 (osm-discovery)"},
            )
            if resp.status_code == 200:
                return resp.json().get("elements", [])
            if resp.status_code == 429 or resp.status_code == 504:
                wait = 30 * (attempt + 1)
                print(f"  Overpass {resp.status_code}, waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"  Overpass HTTP {resp.status_code}: {resp.text[:200]}")
                if attempt < max_retries - 1:
                    time.sleep(10)
        except requests.Timeout:
            print(f"  Overpass timeout (attempt {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                time.sleep(10)
        except Exception as e:
            print(f"  Overpass error: {e}")
            break
    return []


# ---- Element parsing --------------------------------------------------------

# Social-media / non-company domains to skip
_SKIP_DOMAINS = {
    "facebook.com", "fb.com", "linkedin.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "wikipedia.org", "wikidata.org",
    "github.com", "google.com", "apple.com", "microsoft.com",
}


def _clean_website(raw: str | None) -> str | None:
    """Normalise a raw website tag value, return None if useless."""
    if not raw:
        return None
    url = raw.strip()
    # Add scheme if missing (OSM tags sometimes lack it)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    # Extract domain for skip check
    domain = re.sub(r"^https?://", "", url).lower().split("/")[0]
    domain = re.sub(r"^www\.", "", domain)
    if domain in _SKIP_DOMAINS or any(domain.endswith("." + d) for d in _SKIP_DOMAINS):
        return None
    if "." not in domain or len(domain) < 4:
        return None
    return url


def _parse_element(el: dict, region: str) -> dict | None:
    """Convert one OSM element into a candidate dict, or None."""
    tags = el.get("tags", {})
    name = tags.get("name")
    if not name or len(name) < 2:
        return None

    # Coordinates
    lat = el.get("lat")
    lon = el.get("lon")
    if lat is None and "center" in el:
        lat = el["center"].get("lat")
        lon = el["center"].get("lon")

    # Website (try multiple tag keys)
    website = _clean_website(
        tags.get("website") or tags.get("contact:website") or tags.get("url")
    )

    # City from address tags
    city = (
        tags.get("addr:city")
        or tags.get("addr:municipality")
        or tags.get("addr:town")
        or ""
    )

    osm_type = el.get("type", "node")
    osm_id = f"{osm_type}/{el.get('id', 0)}"

    return {
        "name": name,
        "website": website,
        "city": city,
        "region": region,
        "lat": lat,
        "lon": lon,
        "osm_id": osm_id,
        "osm_tags": {k: v for k, v in tags.items() if k != "name"},
        "source": "osm",
    }


# ---- Public API -------------------------------------------------------------

def discover_osm(region: str, limit: int = 500) -> list[dict]:
    """
    Discover company candidates in *region* via the Overpass API.

    Returns at most *limit* candidates sorted by usefulness (those with a
    website first).
    """
    print(f"  Querying OSM for companies in '{region}'...")
    query = _build_query(region)
    elements = _query_overpass(query)

    if not elements:
        # Fallback: try English name variant
        name, _ = _resolve_region(region)
        fallback = query.replace(f'"name"="{name}"', f'"name:en"="{name}"')
        print(f"  No results with native name, trying name:en fallback...")
        elements = _query_overpass(fallback)

    print(f"  Got {len(elements)} raw OSM elements")

    # Parse & dedupe within this query
    candidates = []
    seen_lower = set()
    for el in elements:
        cand = _parse_element(el, region)
        if cand is None:
            continue
        key = cand["name"].lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        candidates.append(cand)

    print(f"  Parsed {len(candidates)} unique named candidates")

    # Sort: websites first, then alphabetical
    candidates.sort(key=lambda c: (0 if c.get("website") else 1, c["name"]))
    return candidates[:limit]


# ---- Standalone mode --------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test OSM company discovery")
    parser.add_argument("region", help="Region name (e.g. Eindhoven, Noord-Brabant, Netherlands)")
    parser.add_argument("--limit", type=int, default=50, help="Max candidates (default 50)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    results = discover_osm(args.region, limit=args.limit)

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print(f"\n{'='*70}")
        print(f"  {len(results)} candidates in '{args.region}'")
        print(f"  {sum(1 for r in results if r.get('website'))} have a website")
        print(f"{'='*70}")
        for i, c in enumerate(results, 1):
            web = c.get("website") or "(no website)"
            city = c.get("city") or "?"
            print(f"  {i:>3}. {c['name']:<40} {city:<20} {web}")
