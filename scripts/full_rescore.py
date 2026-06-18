#!/usr/bin/env python3
"""
full_rescore.py
---------------
Re-enrich every signal in the database with the latest Bullish AI prompt.

Runs 4 parallel threads (matching gunicorn --workers 4) so enrichments
overlap instead of queuing. Real throughput: ~6–7 signals/min.
Estimated time for 2000 signals: ~5–6 hours.

Usage:
  python scripts/full_rescore.py
"""
import getpass
import json
import sys
import time
import threading
import requests
from concurrent.futures import ThreadPoolExecutor

API_BASE       = "https://web-production-801ed.up.railway.app/api"
ENRICH_TIMEOUT = 65   # seconds — Claude + optional Proxycurl can take ~45s
MAX_WORKERS    = 4    # must match gunicorn --workers in start.sh

_print_lock = threading.Lock()
_stats_lock = threading.Lock()
_enriched   = 0
_errors     = 0


def login(email: str, password: str) -> str:
    r = requests.post(f"{API_BASE}/auth/login",
                      json={"email": email, "password": password}, timeout=15)
    r.raise_for_status()
    token = r.json().get("access_token")
    if not token:
        print("Login failed — no access_token in response")
        sys.exit(1)
    return token


def fetch_all_signal_ids(token: str) -> list[int]:
    headers = {"Authorization": f"Bearer {token}"}
    ids = []
    page = 1
    while True:
        r = requests.get(f"{API_BASE}/items",
                         params={"page": page, "per_page": 100},
                         headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
        for item in data.get("items", []):
            try:
                meta = json.loads(item.get("description") or "{}")
                if meta.get("_type") == "signal":
                    ids.append(item["id"])
            except (json.JSONDecodeError, TypeError):
                continue
        if not data.get("pagination", {}).get("has_next"):
            break
        page += 1
    return ids


def enrich_one(token: str, item_id: int) -> bool:
    r = requests.post(
        f"{API_BASE}/enrich/signal/{item_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=ENRICH_TIMEOUT,
    )
    if r.status_code == 429:
        raise RuntimeError("rate-limited")
    r.raise_for_status()
    return r.json().get("enrichment", {}).get("enriched", False)


def _process(token: str, item_id: int, index: int, total: int) -> str:
    global _enriched, _errors
    pct    = round(index / total * 100)
    prefix = f"  [{index:4d}/{total}] {pct:3d}%  item={item_id}  "
    status = ""
    try:
        ok     = enrich_one(token, item_id)
        status = "✓" if ok else "- (skipped)"
        with _stats_lock:
            if ok:
                _enriched += 1
    except RuntimeError:
        time.sleep(30)
        try:
            ok     = enrich_one(token, item_id)
            status = "✓ (retry)" if ok else "- (rate-limited, skip)"
            with _stats_lock:
                if ok:
                    _enriched += 1
                else:
                    _errors += 1
        except Exception as e2:
            status = f"error: {e2}"
            with _stats_lock:
                _errors += 1
    except Exception as e:
        status = f"error: {e}"
        with _stats_lock:
            _errors += 1
    with _print_lock:
        print(prefix + status, flush=True)
    return status


def main():
    print("=" * 60)
    print("  Bullish Stealth Finder — Full Database Rescore")
    print("=" * 60)
    print()
    print("Re-enriches every signal with the latest AI prompt.")
    print("Established brands → gate_passed=false → off the dashboard.")
    print(f"Running {MAX_WORKERS} parallel workers.\n")

    email    = input("Admin email: ").strip()
    password = getpass.getpass("Admin password: ")

    print("\nLogging in...")
    token = login(email, password)
    print("✓ Authenticated\n")

    print("Fetching signal IDs (paging through all items)...")
    ids   = fetch_all_signal_ids(token)
    total = len(ids)
    # ~35s per signal / 4 parallel workers
    est_mins = round(total / MAX_WORKERS * 35 / 60, 0)
    est_cost = round(total * 0.03, 2)
    print(f"✓ {total} signals found")
    print(f"\nEstimated time : ~{int(est_mins)} min ({int(est_mins)//60}h {int(est_mins)%60}m)"
          f"  |  Estimated cost : ~${est_cost}\n")

    confirm = input("Proceed? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    print(f"\nStarting {MAX_WORKERS}-worker parallel rescore...\n")
    t0 = time.monotonic()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        work = [(token, iid, i, total) for i, iid in enumerate(ids, 1)]
        for _ in executor.map(lambda a: _process(*a), work):
            pass

    elapsed = time.monotonic() - t0
    print()
    print("=" * 60)
    print(f"  ✓ Done. Enriched {_enriched}/{total}, errors: {_errors}.")
    print(f"  Elapsed: {elapsed/60:.1f} min  |  Rate: {total/(elapsed/60):.1f} signals/min")
    print(f"  Refresh the dashboard to see updated scores.")
    print("=" * 60)


if __name__ == "__main__":
    main()
