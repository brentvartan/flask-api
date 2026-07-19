#!/usr/bin/env python3
"""
backfill_tier_floors.py
-----------------------
One-time migration: apply minimum tier floors to existing cold signals
without calling Claude.

Rules (mirrors enrichment.py post-processing added June 2026):
  - conviction_match present + watch_level != hot  →  hot
  - trademark OR delaware in brand's signal set
    + watch_level == cold                           →  warm

Cost:  $0  —  no Claude calls, no external APIs.
Time:  ~1 minute against the live Railway API.

Usage:
    python scripts/backfill_tier_floors.py
"""
import getpass
import json
import sys
import requests

API_BASE     = "https://web-production-801ed.up.railway.app/api"
LEGAL_SIGNALS = {"trademark", "delaware"}


# ── Auth ──────────────────────────────────────────────────────────────────────

def login(email: str, password: str) -> str:
    r = requests.post(f"{API_BASE}/auth/login",
                      json={"email": email, "password": password}, timeout=10)
    r.raise_for_status()
    token = r.json().get("access_token")
    if not token:
        print("Login failed — no access_token in response")
        sys.exit(1)
    return token


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_all_signals(token: str) -> list:
    headers  = {"Authorization": f"Bearer {token}"}
    signals, page = [], 1
    while True:
        r = requests.get(f"{API_BASE}/items",
                         params={"page": page, "per_page": 100},
                         headers=headers, timeout=15)
        r.raise_for_status()
        data  = r.json()
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


# ── Floor logic ───────────────────────────────────────────────────────────────

def compute_floor(signal_types: set, enrichment: dict, has_top_level_conviction: bool):
    """
    Return updated enrichment dict if a floor applies, else None.
    Mirrors the post-processing block in enrichment.py.
    """
    has_conviction = has_top_level_conviction or bool(enrichment.get("conviction_match"))
    level          = enrichment.get("watch_level", "cold")

    if has_conviction and level != "hot":
        return {**enrichment, "watch_level": "hot", "tier_floor_reason": "conviction_match"}

    if (signal_types & LEGAL_SIGNALS) and level == "cold":
        has_tm = "trademark" in signal_types
        has_de = "delaware"  in signal_types
        reason = "tm_plus_form_d" if (has_tm and has_de) else ("trademark" if has_tm else "form_d")
        return {**enrichment, "watch_level": "warm", "tier_floor_reason": reason}

    return None


# ── Update ────────────────────────────────────────────────────────────────────

def update_item(token: str, item_id: int, meta: dict) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.put(f"{API_BASE}/items/{item_id}",
                     json={"description": json.dumps(meta)},
                     headers=headers, timeout=10)
    r.raise_for_status()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== Bullish Stealth Finder — Tier Floor Backfill ===")
    print("Bumps cold signals with TM or Form D to warm. No Claude calls.\n")

    email    = input("Admin email: ").strip()
    password = getpass.getpass("Admin password: ")

    print("\nLogging in...")
    token = login(email, password)
    print("✓ Authenticated\n")

    print("Fetching all signals...")
    signals = fetch_all_signals(token)
    print(f"✓ {len(signals)} signal items loaded\n")

    # Group by brand title to collect the full signal_types set per brand
    by_brand = {}
    for item in signals:
        brand = (item.get("title") or "").strip().lower()
        if brand:
            by_brand.setdefault(brand, []).append(item)

    # Identify items that need updating
    candidates = []   # (item, meta, new_enrichment, reason_label)

    for brand, group in by_brand.items():
        signal_types = {item["_meta"].get("signal_type") for item in group
                        if item["_meta"].get("signal_type")}

        for item in group:
            meta       = item["_meta"]
            enrichment = meta.get("enrichment")
            if not enrichment or not enrichment.get("enriched"):
                continue

            has_conviction = bool(meta.get("conviction_match"))
            new_enrichment = compute_floor(signal_types, enrichment, has_conviction)
            if new_enrichment is None:
                continue

            candidates.append((item, meta, new_enrichment))

    if not candidates:
        print("Nothing to update — no cold signals qualify for a floor bump.")
        return

    # Preview
    REASON_LABELS = {
        "conviction_match": "Conviction match → HOT",
        "tm_plus_form_d":   "TM + Form D → WARM",
        "trademark":        "Trademark → WARM",
        "form_d":           "Form D → WARM",
    }
    print(f"{'ID':>6}  {'Brand':<42}  {'Was':<6}  Reason")
    print("─" * 72)
    for item, meta, new_enrichment in candidates:
        reason = new_enrichment.get("tier_floor_reason", "?")
        old    = meta.get("enrichment", {}).get("watch_level", "?")
        print(f"{item['id']:>6}  {item.get('title','?')[:42]:<42}  {old:<6}  {REASON_LABELS.get(reason, reason)}")

    print(f"\n→ {len(candidates)} signals will be updated. $0 cost.\n")
    confirm = input("Proceed? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    updated = errors = 0
    for item, meta, new_enrichment in candidates:
        meta["enrichment"] = new_enrichment
        try:
            update_item(token, item["id"], meta)
            updated += 1
            print(f"  ✓ [{item['id']:5d}] {item.get('title','?')[:50]}")
        except Exception as e:
            errors += 1
            print(f"  ✗ [{item['id']:5d}] {item.get('title','?')[:50]} — {e}")

    print(f"\n✓ Done. Updated {updated}, errors {errors}.")
    print("Refresh the dashboard to see updated tiers.\n")


if __name__ == "__main__":
    main()
