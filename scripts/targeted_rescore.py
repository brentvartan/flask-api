#!/usr/bin/env python3
"""
targeted_rescore.py
-------------------
Re-enrich borderline WARM signals (bullish_score 62–70) in categories
most affected by the May 2026 calibration update:
  beauty / body care / skincare / personal care
  functional beverages / drinks
  food identity / condiments / CPG food
  consumer fintech / insurance / banking

Usage:
  python scripts/targeted_rescore.py
  # prompts for admin email + password, then dry-runs first
"""
import getpass
import json
import sys
import requests

API_BASE = "https://web-production-801ed.up.railway.app/api"

# Categories likely affected by new HOT/WARM anchors
CATEGORY_KEYWORDS = [
    "beauty", "skin", "body", "care", "cosmetic", "fragrance", "grooming",
    "beverage", "drink", "juice", "water", "bev", "soda", "brew", "coffee", "tea",
    "food", "snack", "sauce", "condiment", "spice", "nutrition", "supplement",
    "health", "wellness", "fitness",
    "fintech", "financial", "insurance", "bank", "lending", "credit", "payment",
]

# Score range to target — borderline that new anchors may shift
SCORE_LOW  = 60
SCORE_HIGH = 72


def login(email: str, password: str) -> str:
    r = requests.post(f"{API_BASE}/auth/login",
                      json={"email": email, "password": password}, timeout=10)
    r.raise_for_status()
    token = r.json().get("access_token")
    if not token:
        print("Login failed — no access_token in response")
        sys.exit(1)
    return token


def fetch_all_signals(token: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    signals, page = [], 1
    while True:
        r = requests.get(f"{API_BASE}/items",
                         params={"page": page, "per_page": 100},
                         headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])
        for item in items:
            if item.get("item_type") != "signal":
                continue
            try:
                meta = json.loads(item.get("description") or "{}")
                if meta.get("_type") != "signal":
                    continue
                item["_meta"] = meta
                signals.append(item)
            except (json.JSONDecodeError, TypeError):
                continue
        if page >= data.get("pages", 1):
            break
        page += 1
    return signals


def is_target(item: dict) -> tuple[bool, str]:
    meta = item.get("_meta", {})
    enrichment = meta.get("enrichment")
    if not enrichment or not enrichment.get("enriched"):
        return False, "not enriched"

    score = enrichment.get("bullish_score")
    if score is None or not (SCORE_LOW <= score <= SCORE_HIGH):
        return False, f"score {score} out of range"

    category = (meta.get("category") or "").lower()
    company  = (meta.get("company_name") or item.get("title") or "").lower()
    haystack = f"{category} {company}"
    matched  = next((kw for kw in CATEGORY_KEYWORDS if kw in haystack), None)
    if not matched:
        return False, "category not in target list"

    return True, f"score={score}, category='{category}', matched='{matched}'"


def rescore_batch(token: str, item_ids: list[int]) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.post(f"{API_BASE}/enrich/batch",
                      json={"item_ids": item_ids},
                      headers=headers, timeout=120)
    r.raise_for_status()
    return r.json()


def main():
    print("=== Bullish Stealth Finder — Targeted Rescore ===")
    print(f"Target: bullish_score {SCORE_LOW}–{SCORE_HIGH} in beauty/bev/food/fintech categories\n")

    email    = input("Admin email: ").strip()
    password = getpass.getpass("Admin password: ")

    print("\nLogging in...")
    token = login(email, password)
    print("✓ Authenticated\n")

    print("Fetching all signals (this may take a moment)...")
    signals = fetch_all_signals(token)
    print(f"✓ Found {len(signals)} total signals\n")

    targets = []
    for item in signals:
        ok, reason = is_target(item)
        if ok:
            targets.append(item)
            enrichment = item["_meta"].get("enrichment", {})
            print(f"  [{item['id']:5d}] {item.get('title','?')[:40]:<40}  "
                  f"score={enrichment.get('bullish_score')}  "
                  f"level={enrichment.get('watch_level','?')}")

    if not targets:
        print("\nNo signals match the target criteria — nothing to rescore.")
        return

    print(f"\n→ {len(targets)} signals identified for rescore")
    print(f"  Estimated cost: ~${len(targets) * 0.03:.2f} (${0.03}/signal)\n")

    confirm = input("Proceed? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    # Batch in groups of 20 (API limit)
    ids = [item["id"] for item in targets]
    total_enriched = 0
    for i in range(0, len(ids), 20):
        batch = ids[i:i+20]
        print(f"  Rescoring items {i+1}–{min(i+20, len(ids))}...", end=" ", flush=True)
        result = rescore_batch(token, batch)
        enriched = result.get("enriched", 0)
        errors   = result.get("errors", 0)
        total_enriched += enriched
        print(f"enriched={enriched}, errors={errors}")

    print(f"\n✓ Done. Total re-enriched: {total_enriched}/{len(targets)}")
    print("Refresh the Stealth Finder dashboard to see updated scores.\n")


if __name__ == "__main__":
    main()
