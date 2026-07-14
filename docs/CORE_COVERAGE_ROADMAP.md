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
- ✅ **Intent-to-use (1b) scoring** shipped 2026-07-13: `basisFiled` added to `_source`; 1b filings get `score_boost=18` vs 14 for use-in-commerce; `is_intent_to_use` flag carried on signal; description appended with "pre-launch intent-to-use" so enrichment prompt sees it.

## Workstream 2 — Cadence & backfill resilience
**2a — schedule the unscheduled signals  ✅ (shipped 2026-07)**
- `ctlogs` and `press_stealth` were **never run on any schedule** (manual API only) — so the funding detector shipped earlier was dormant, and the earliest-lead CT-log signal ran zero cadence. Added both to `run_scan_now` dispatch (`app/services/scheduler.py`) so the daily `full` sweep runs them; added both (+`newswire`) to `_VALID_SCAN_TYPES` so they're individually configurable.
- ⚠️ **Verify in prod:** effectiveness depends on an enabled `full` ScheduledScan existing. Confirm via `GET /api/scheduled-scans`.

**2b — backfill / downtime recovery  ✅ (shipped 2026-07-13)**
- `run_scan_now()` gained `days_back_override` param so the scheduler can widen the scan window without mutating the configured `scan.days_back`.
- `_run_all_scheduled()` reads `scan.last_run_at` (already persisted) as a watermark: if the gap exceeds `scan.days_back`, widens `days_back_override` to cover the full gap (+2-day overlap, capped at 30d). The May-2026 outage would now auto-recover on next boot.
- `_write_scheduler_heartbeat()` persists `{last_run}` to a `__scheduler_heartbeat__` system item after every daily run.
- `GET /api/admin/scheduler/status` → `{last_run, hours_since, is_healthy}` (healthy = < 30h).
- Settings → Schedules tab shows green/amber health pill; turns green after 06:00 UTC tomorrow.

## Workstream 3 — Scoring / surfacing calibration  ✅ (2026-07-14)
**Finding: the numeric score is NOT a hidden gate** — `GET /api/items` is unfiltered; frontend `minScore` defaults to 0. A mis-scored brand is buried, not hidden. The real *automatic* "scanned-but-invisible" bug is category-driven:
- ✅ **CONSUMER_CATEGORIES allow-list drop** fixed: added `'Pet'` to `CONSUMER_CATEGORIES` (`Dashboard.jsx:12`). Backend `categories.py` emits `"Pet"` for pet/dog/cat signals; dashboard now renders them.
- ✅ **Dead gate filter** fixed: `Dashboard.jsx:292` path corrected from `enrichment.gate_passed` (always undefined) to `enrichment.founder_score.gate_passed`. B2B gate now actually filters.
- ✅ **Delaware default category** fixed: `delaware.py` changed from `"Consumer AI"` to `"Home/Lifestyle"` fallback.
- ✅ **Delaware EDGAR foundational rewrite** (2026-07-14) — three root-cause bugs fixed:
  - `_FUND_ITEMS` incorrectly blocked 06a/06b/06c (standard Reg D codes used by every operating company incl. consumer brands). Fixed to only include true Investment Company Act codes (3c, 3c.1, 3c.5, 3c.7). This was rejecting ~99% of all consumer brand Form D filings.
  - Phase 1 keyword queries (food/beauty/etc.) removed — EDGAR full-text search matches officer names and addresses, returning 0-2 results per term with no consumer brand signal.
  - Broad sweep now probes for `total_found` first and covers ALL Form Ds in the window (was capped at 20 pages with no probe, missing 40-60% of universe).
  - Extended `_FUND_NUMERAL_RE` to Roman numerals II–XLIX (was only II–XII).
  - Added BVI (D8) and Luxembourg (N4) jurisdiction filter.
  - Expanded `_NON_CONSUMER_BLOCKLIST` with " lp", "pllc", "spv", "co-invest", "co-investing", "a series of", "gaingels", "holdco", "reit", "qozb", "bancorp", "villas", "scsp", "investco", "moonrock", "biosystems", " metals", "storwell", "credit union" and more.
  - **Net effect:** ~3 consumer brand candidates per 1000 Form Ds → ~20-25 per 1000 (~7-8x coverage increase with the same enrichment budget).
- ✅ **Form D full-coverage hardening** (2026-07-14) — three caps that silently dropped brands after the filter fixed:
  - `score_boost` raised from 5 → 12 (Delaware-incorporated) / 8 (other US states). DE incorporation is a deliberate VC-track choice; the differentiation gives dashboard ranking signal.
  - `search_recent_delaware_entities` default `max_results` raised from 200 → 2000 — never hits the cap for any realistic scan window (7-day window yields ~110 candidates; 30-day yields ~370).
  - Route default raised from 150 → 500, hard cap from 300 → 2000 (manual `/delaware` scans now cover 30-day windows without truncation).
  - Scheduler hardcoded `max_results=150` replaced with `max(500, scan.max_results)` — now consistent with trademark/producthunt/newswire behavior and never drops below 500 for Delaware.
- ⬜ Detection query: count saved `signal` items vs. count the dashboard actually renders — the delta is the invisible population.

## Workstream 4 — CT-logs net widening  ✅ (shipped 2026-07-13)
Three structural fixes to `app/services/ctlogs.py`:
- ✅ **Added `.com` patterns**: `SEARCH_PATTERNS` now includes `.com` counterparts for every prefix (get/try/join/sip/brew/snack/glow/balm/vita/ritual/paw). DTC `.com` universe now included.
- ✅ **Filter-before-cap bug fixed**: `_query_crtsh` now iterates all entries, filters by recency first, then caps on fresh results (`MAX_PER_PATTERN`). Return type changed to `list[tuple]` `(domain, not_before)` to carry timestamps upstream.
- ✅ **Recency sort**: outer function now sorts `all_domain_hits` by `not_before` descending before capping — newest certs surface first, not alphabetically-earliest brands.
- ⬜ Broaden keyword patterns (inherent limit: a keyword sieve can't catch arbitrary names like Fascent/Rivalz — that's what trademark/Form D are for).

---
_See project memory `stealth_finder_core_coverage` for the strategic thesis. Shipped audit patches (funding detector, Form D window) are in `stealth_finder_state`._
