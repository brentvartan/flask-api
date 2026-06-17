#!/usr/bin/env python3
"""
full_rescore.py
---------------
Re-enrich every signal in the database with the latest Bullish AI prompt.

This catches:
  - Established brands (IPSY, AG1, Glossier) → gate_passed=false → hidden from dashboard
  - Signals enriched before the stage filter, founder scoring, or calibration updates
  - Any signal with a stale score that doesn't reflect current thesis anchors

Batch size is 10 (not 50) to stay under Railway's ~60s HTTP timeout.
Rate limit is 5 calls/min on the batch endpoint → 13s sleep between calls.
Throughput: ~46 signals/min.

Usage:
  python scripts/full_rescore.py
"""
import getpass
import sys
import time
import requests

API_BASE   = "https://web-production-801ed.up.railway.app/api"
BATCH_SIZE = 5        # 5 Claude calls ≈ 45s, well under Gunicorn's 300s worker timeout
SLEEP_SEC  = 13       # 5 calls/min rate limit → 13s gives headroom


def login(email: str, password: str) -> str:
    r = requests.post(f"{API_BASE}/auth/login",
                      json={"email": email, "password": password}, timeout=10)
    r.raise_for_status()
    token = r.json().get("access_token")
    if not token:
        print("Login failed — no access_token in response")
        sys.exit(1)
    return token


def count_signals(token: str) -> int:
    r = requests.get(f"{API_BASE}/items",
                     params={"page": 1, "per_page": 1},
                     headers={"Authorization": f"Bearer {token}"}, timeout=15)
    r.raise_for_status()
    return r.json().get("pagination", {}).get("total", 0)


def rescore_page(token: str, offset: int) -> dict:
    r = requests.post(
        f"{API_BASE}/enrich/batch",
        json={"rescore_all": True, "limit": BATCH_SIZE, "offset": offset},
        headers={"Authorization": f"Bearer {token}"},
        timeout=290,   # Gunicorn worker timeout is 300s; leave 10s for network
    )
    r.raise_for_status()
    return r.json()


def main():
    print("=" * 60)
    print("  Bullish Stealth Finder — Full Database Rescore")
    print("=" * 60)
    print()
    print("Re-enriches every signal with the latest AI prompt.")
    print("Established brands will be gated out. ~46 signals/min.\n")

    email    = input("Admin email: ").strip()
    password = getpass.getpass("Admin password: ")

    print("\nLogging in...")
    token = login(email, password)
    print("✓ Authenticated")

    total = count_signals(token)
    est_batches = (total // BATCH_SIZE) + 2
    est_mins    = round((est_batches * SLEEP_SEC) / 60, 1)
    est_cost    = round(total * 0.03, 2)
    print(f"✓ {total} items in database")
    print(f"\nEstimated time : ~{est_mins} min  |  Estimated cost : ~${est_cost}\n")

    confirm = input("Proceed? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    print()
    offset         = 0
    total_enriched = 0
    total_errors   = 0
    batch_num      = 0

    while True:
        batch_num += 1
        print(f"  Batch {batch_num:3d} | offset={offset:4d} | ", end="", flush=True)

        try:
            result = rescore_page(token, offset)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                print("rate-limited — sleeping 60s...")
                time.sleep(60)
                continue   # retry same offset
            print(f"HTTP {e.response.status_code if e.response else '?'}: {e}")
            break
        except requests.Timeout:
            print(f"timed out after 290s — skipping batch, advancing offset")
            total_errors += BATCH_SIZE
            offset += BATCH_SIZE
            continue
        except Exception as e:
            print(f"error: {e}")
            break

        enriched  = result.get("enriched", 0)
        errors    = result.get("errors", 0)
        processed = result.get("total_processed", 0)
        has_more  = result.get("has_more", False)

        total_enriched += enriched
        total_errors   += errors
        offset         += processed

        print(f"enriched={enriched:2d}  errors={errors:2d}  "
              f"running total={total_enriched:4d}  "
              f"{'→ more' if has_more else '✓ done'}")

        if not has_more or processed == 0:
            break

        time.sleep(SLEEP_SEC)

    print()
    print("=" * 60)
    print(f"  ✓ Done. Re-enriched {total_enriched} signals, {total_errors} errors.")
    print(f"  Refresh the Stealth Finder dashboard to see updated scores.")
    print("=" * 60)


if __name__ == "__main__":
    main()
