# Stealth Finder — Core Coverage Roadmap

**Why this exists:** the 2026-07 Deals Inbox Audit scored **30.4% coverage** (16/23 deal-inbox brands never surfaced). Patching brand-by-brand is whack-a-mole. This doc tracks the *foundation* fix: every signal engine shares one disease —

> **The root pattern:** scans pull a time window, **hard-cap the result count (often before the recency filter)**, and **under-target / mis-sort the query** — so each engine inspects a tiny, frequently non-fresh slice and silently drops the rest.

Measured under-sampling: delaware saw **200 of ~1,355 Form Ds/wk (15%)**; trademark **200 of ~38,904 consumer filings/mo (0.5%), unsorted**; ctlogs a ~13-pattern keyword sieve that never queries `.com`.

Status legend: ✅ shipped · 🔨 in progress · ⬜ planned

---

## Workstream 1 — Trademark targeting  ✅ (shipped 2026-07)
**Longest-lead signal (6–28 mo). Was the worst under-sample: 0.5%, no sort.**
- `app/services/trademarks.py` — added `filedDate` DESC sort (freshest, deterministic paging), bounded deep pagination (`max_filings=1200`, page_size 100), and `_is_brand_candidate()` owner filter that drops institutional/non-brand owners (holdings/capital/bank/university/law…) **before** the `max_results=250` enrich budget is spent. Kept the enrich cap near old levels — this is a *targeting* upgrade, not a volume/cost blowout.
- Verified live: 38,904 in-window, now returns newest-first, owner filter active. Tests: `tests/test_trademarks_targeting.py`.
- ⬜ Follow-up (higher precision): filter to **intent-to-use (1b)** filing basis = pre-launch brands (need to confirm the field is exposed); prioritize owners matching the conviction/exit-alumni lists.

## Workstream 2 — Cadence & backfill resilience
**2a — schedule the unscheduled signals  ✅ (shipped 2026-07)**
- `ctlogs` and `press_stealth` were **never run on any schedule** (manual API only) — so the funding detector shipped earlier was dormant, and the earliest-lead CT-log signal ran zero cadence. Added both to `run_scan_now` dispatch (`app/services/scheduler.py`) so the daily `full` sweep runs them; added both (+`newswire`) to `_VALID_SCAN_TYPES` so they're individually configurable.
- ⚠️ **Verify in prod:** effectiveness depends on an enabled `full` ScheduledScan existing. Confirm via `GET /api/scheduled-scans`.

**2b — backfill / downtime recovery  ⬜ (planned, HIGH)**
- In-process APScheduler (`app/__init__.py`), daily 06:00 UTC, `misfire_grace_time=3600`, **no persistent jobstore, no catch-up**. Scheduled window is a flat `days_back=7` for all sources (service defaults are dead in the scheduled path). An outage **> 7 days** (e.g. the May-2026 Railway trial expiry) **permanently loses** the middle days — no recovery.
- Fix: persist a **last-successful-run watermark** per source; on first run after a gap, widen `days_back` to cover the actual gap. Add an **external heartbeat** (Railway cron / external ping to a run endpoint) so downtime ≠ silent total stop.

## Workstream 3 — Scoring / surfacing calibration  ⬜ (planned; mostly FRONTEND repo)
**Finding: the numeric score is NOT a hidden gate** — `GET /api/items` is unfiltered; frontend `minScore` defaults to 0. A mis-scored brand is buried, not hidden. The real *automatic* "scanned-but-invisible" bug is category-driven:
- ⬜ **CONSUMER_CATEGORIES allow-list drop** (`frontend/src/components/Dashboard.jsx:54`, list at :11-14) silently filters out any signal categorized **`Pet`** (emitted by the backend `app/utils/categories.py` mapper) — scanned, enriched, then never rendered. Reconcile the frontend allow-list against the backend category map; add `Pet`, decide a bucket for `Other`.
- ⬜ **Dead gate filter** (`Dashboard.jsx:292`) reads `enrichment.gate_passed` (always undefined); real path is `enrichment.founder_score.gate_passed`. No-op → decide intended behavior.
- ⬜ **Delaware default category** `"Consumer AI"` is never corrected post-enrichment despite the code comment (`delaware.py:72-77`) — keyword-miss Form Ds stick as Consumer AI. Have enrichment write category back, or fix the default.
- ⬜ Detection query: count saved `signal` items vs. count the dashboard actually renders — the delta is the invisible population.

## Workstream 4 — CT-logs net widening  ⬜ (planned)
Independent discovery net, but a **narrow keyword sieve** (`app/services/ctlogs.py`):
- ⬜ **Never queries `.com`** — `SEARCH_PATTERNS` (`:41-55`) only hit `.co/.fun/.health` despite `CONSUMER_TLDS` listing .com/.io/.shop. The entire `.com` DTC universe is invisible. Add `.com` patterns.
- ⬜ **Head-slice-before-filter bug** (`:133-137`): `data[:MAX_PER_PATTERN]` truncates to 200 raw certs *before* the recency filter → stale certs can consume the window and yield zero recent hits (same disease as the Form D cap). Filter, then cap.
- ⬜ **Alphabetical cap** (`:192`): `sorted(all_domains)[:100]` biases to early letters. Cap by recency instead.
- ⬜ Broaden keyword patterns (inherent limit: a keyword sieve can't catch arbitrary names like Fascent/Rivalz — that's what trademark/Form D are for).

---
_See project memory `stealth_finder_core_coverage` for the strategic thesis. Shipped audit patches (funding detector, Form D window) are in `stealth_finder_state`._
