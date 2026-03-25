"""
Crunchbase Basic API — optional founder enrichment source.
Only active when CRUNCHBASE_API_KEY is set.
Docs: https://data.crunchbase.com/docs/using-the-api
"""
import os
import logging
import requests

logger = logging.getLogger(__name__)

CRUNCHBASE_API_KEY = os.environ.get("CRUNCHBASE_API_KEY", "")
BASE_URL = "https://api.crunchbase.com/api/v4"


def is_available() -> bool:
    return bool(CRUNCHBASE_API_KEY)


def lookup_company(brand_name: str) -> dict | None:
    """
    Search Crunchbase for a company by name.
    Returns a dict with: name, founders (list of names), total_funding_usd, last_funding_type, or None.
    Uses POST /searches/organizations endpoint.
    """
    if not is_available():
        return None
    try:
        resp = requests.post(
            f"{BASE_URL}/searches/organizations",
            params={"user_key": CRUNCHBASE_API_KEY},
            json={
                "field_ids": ["identifier", "short_description", "founder_identifiers",
                              "funding_total", "last_funding_type", "categories"],
                "query": [{"type": "predicate", "field_id": "facet_ids",
                           "operator_id": "includes", "values": ["company"]},
                          {"type": "predicate", "field_id": "identifier",
                           "operator_id": "contains", "values": [brand_name]}],
                "limit": 3
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        entities = data.get("entities", [])
        if not entities:
            return None
        # Take first result
        props = entities[0].get("properties", {})
        founders = [f.get("value", "") for f in props.get("founder_identifiers", [])]
        return {
            "_crunchbase_hit": True,
            "name": props.get("identifier", {}).get("value", brand_name),
            "founders": founders,
            "total_funding": props.get("funding_total", {}).get("value_usd"),
            "last_funding_type": props.get("last_funding_type"),
            "description": props.get("short_description", ""),
        }
    except Exception as exc:
        logger.warning("Crunchbase lookup failed for %s: %s", brand_name, exc)
        return None
