#!/usr/bin/env python3
"""
candidate_filter.py - Score and filter OSM candidates before ATS probing.

Deterministic scoring based on OSM tags and name heuristics.
Eliminates restaurants, shops, schools, etc. before we waste HTTP requests
on ATS probes and career-page checks.

Usage:
    from candidate_filter import score_candidate, is_candidate_eligible
    score = score_candidate(candidate)
    ok    = is_candidate_eligible(candidate, min_score=30)
"""

import re

# ---------------------------------------------------------------------------
# Tag-based scoring tables
# ---------------------------------------------------------------------------

# Tags whose mere presence is a strong negative signal.
# Map: (tag_key, tag_value_regex | None) -> penalty
# None for value means "any value".
_EXCLUDE_TAGS: list[tuple[str, str | None, int]] = [
    # Food / hospitality
    ("amenity", r"restaurant|cafe|bar|fast_food|pub|ice_cream|food_court|biergarten", -60),
    ("amenity", r"nightclub|cinema|theatre|casino|gambling", -60),
    ("amenity", r"school|kindergarten|college|university|library", -50),
    ("amenity", r"hospital|clinic|pharmacy|dentist|doctors|veterinary", -50),
    ("amenity", r"place_of_worship|monastery|church", -60),
    ("amenity", r"bank|atm|bureau_de_change|post_office", -30),
    ("amenity", r"fuel|car_wash|parking|bicycle_parking|charging_station", -50),
    ("amenity", r"childcare|social_facility|nursing_home|retirement_home", -50),
    # Shop
    ("shop", None, -50),
    # Tourism / leisure / sport
    ("tourism", None, -50),
    ("leisure", None, -40),
    ("sport", None, -40),
    # Craft trades (baker, butcher, etc.)
    ("craft", r"baker|butcher|carpenter|plumber|electrician|painter|tailor|hairdresser|shoemaker|photographer", -50),
    # Healthcare
    ("healthcare", None, -40),
    # Religion
    ("religion", None, -60),
    # Man-made things that aren't companies
    ("man_made", r"tower|mast|chimney|lighthouse|windmill", -40),
    # Landuse that isn't corporate
    ("landuse", r"residential|farmland|cemetery|forest|meadow|recreation_ground", -40),
]

# Tags that are positive signals for real companies / employers.
_PREFER_TAGS: list[tuple[str, str | None, int]] = [
    ("office", r"company", 20),
    ("office", r"it|ngo|co_?working", 25),  # IT offices
    ("office", r"research|architect|engineer", 20),
    ("office", r"financial|insurance|consulting|lawyer|accountant", 10),
    ("office", r"government|diplomatic|political_party|religion", -15),
    # Industrial / manufacturing (good for tech)
    ("industrial", None, 15),
    ("man_made", r"works", 10),
    # Explicit tags
    ("company", None, 10),
    ("operator", None, 5),
    ("brand", None, 5),
]

# ---------------------------------------------------------------------------
# Name-based exclusion words  (Dutch + English)
# ---------------------------------------------------------------------------
_EXCLUDE_NAME_WORDS = re.compile(
    r"\b("
    r"restaurant|eetcafe|eetcafé|cafe|café|koffie|coffee|bistro|brasserie"
    r"|bakker|bakkerij|bakery|slagerij|butcher|vishandel"
    r"|kapsalon|kapper|hairdresser|barber|schoonheidssalon|beauty"
    r"|hotel|hostel|pension|bed\s*&?\s*breakfast|b&b"
    r"|bar|pub|lounge|cocktail|tapas|pizzeria|pizza|sushi|wok|grill|kebab"
    r"|supermarkt|supermarket|drogisterij|apotheek|pharmacy"
    r"|kerk|church|moskee|mosque|synagoge"
    r"|school|scholengemeenschap|basisschool|middelbare"
    r"|tandarts|dentist|huisarts|fysio|osteo|chiropract"
    r"|garage|autoservice|autowas|carwash|parkeer"
    r"|sportschool|gym|fitness|zwembad|tennis|voetbal"
    r"|dierenarts|veterinair"
    r"|camping|vakantie|holiday|resort"
    r"|stichting\s+vrienden|dorpshuis|wijkcentrum|buurthuis"
    r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Corporate indicator words (suggest a real employer)
# ---------------------------------------------------------------------------
_CORPORATE_INDICATORS = re.compile(
    r"\b("
    r"b\.?v\.?|n\.?v\.?|v\.?o\.?f\.?"
    r"|group|holding|technologies|technology|systems|engineering"
    r"|industrial|robotics|semiconductor|photonics|electronics"
    r"|automation|logistics|manufacturing|consultancy|consulting"
    r"|solutions|software|digital|data|analytics|cyber"
    r"|aerospace|aviation|energy|pharma|biotech|medtech"
    r"|ventures|capital|partners|labs?|research"
    r"|international|global"
    r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_candidate(candidate: dict) -> int:
    """
    Score a discovery candidate from roughly -100 to +100.

    Higher = more likely to be a real company worth probing.
    Uses OSM tags + name heuristics.  Deterministic and fast.
    """
    score = 0
    tags = candidate.get("osm_tags", {})
    name = candidate.get("name", "")

    # ---- Source bonus (KVK = verified business registry) ----
    if candidate.get("source") == "kvk":
        score += 10

    # ---- Tag-based scoring ----
    for key, val_re, points in _EXCLUDE_TAGS:
        tag_val = tags.get(key)
        if tag_val is not None:
            if val_re is None or re.search(val_re, tag_val, re.IGNORECASE):
                score += points

    for key, val_re, points in _PREFER_TAGS:
        tag_val = tags.get(key)
        if tag_val is not None:
            if val_re is None or re.search(val_re, tag_val, re.IGNORECASE):
                score += points

    # ---- Website bonus ----
    if candidate.get("website"):
        score += 20

    # ---- Name-based scoring ----
    if _EXCLUDE_NAME_WORDS.search(name):
        score -= 50

    if _CORPORATE_INDICATORS.search(name):
        score += 20

    # Short names without website are suspicious
    if len(name) < 4 and not candidate.get("website"):
        score -= 20

    return score


def is_candidate_eligible(
    candidate: dict,
    min_score: int = 30,
    require_website: bool = False,
) -> tuple[bool, str]:
    """
    Decide whether a candidate should proceed to ATS probing.

    Returns (eligible: bool, reason: str).
    reason is empty for eligible candidates, or a short tag for rejected ones.
    """
    score = candidate.get("_score", score_candidate(candidate))
    name = candidate.get("name", "")

    # Hard exclusion on very negative scores
    if score < -20:
        return False, "non_company_tags"

    # Name exclusion
    if _EXCLUDE_NAME_WORDS.search(name):
        return False, "excluded_name"

    # Minimum score gate
    if score < min_score:
        # Allow through if it has a corporate indicator even with low score
        if _CORPORATE_INDICATORS.search(name):
            pass  # override low score
        elif candidate.get("website"):
            # Has website but low score -- borderline, let it through with
            # a lower bar (score >= 0)
            if score < 0:
                return False, "low_score"
        else:
            return False, "low_score"

    # Website requirement
    has_website = bool(candidate.get("website"))
    has_corporate = bool(_CORPORATE_INDICATORS.search(name))

    if require_website and not has_website:
        return False, "no_website"

    # Default eligibility: must have website OR corporate indicator
    if not has_website and not has_corporate:
        return False, "no_website_or_indicator"

    return True, ""
