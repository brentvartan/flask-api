# Stealth Finder — Implementation Brief for Claude Code (v3)

*Work order. Hand this to Claude Code inside the `flask-api` repo. Build-side companion to "Stealth Startup Finder — Fresh Look" (the strategic rationale). Where this brief and the Fresh Look disagree on detail, follow this brief.*

**What changed in v3:** the watchlist keeps its **two designations — founder and alumni** — as an explicit tag on every entry (Task 8). Added a "Protected invariants" section making the triangulation/confluence design a do-not-remove rule.

**What changed in v2:** Crunchbase is now a full removal (not a demotion). Added Task 8 — reset the tracked-people lists down to top consumer-exit names of the last 10 years and consolidate them into a single hand-curated watchlist. Added an explicit "Wiring to remove or consolidate" section mapping every trim to the two jobs.

---

## Protected invariants — do NOT remove or weaken

These are load-bearing. No task, refactor, or cleanup may remove or bypass them; changes may only strengthen them.

1. **Multi-signal triangulation (`confluence.py`) is the heart of the tool and is CRITICAL.** A company seen in 2+ independent channels = real; that cross-signal confluence is the whole edge. Every task that touches confluence (notably Task 2) *adds* linking power (person + coined-term keys) — none removes the brand-name matching or the "fires on a new distinct signal type" logic. If a change would reduce what triangulation can link, it is wrong.
2. **The two designations — founder and alumni — are permanent.** The watchlist always distinguishes company founders from the alumni operators who helped build those brands (Task 8). Never collapse them into one undifferentiated list.
3. **The corrected consumer gate:** default-include, exclude only obvious B2B. Codes/classes/raise-size are ranking features, never exclusion filters.
4. **People change rank, never visibility.** An unknown first-time founder still surfaces, just lower.

---

## The two jobs — the lens for every decision

Everything in this repo should serve one of exactly two jobs. If a piece of wiring serves neither, it's a candidate for removal.

1. **Airtight on Form D / Delaware** — catch every consumer company's first capital raise from the SEC filing, before press.
2. **Airtight on identifying and tracking high-potential founders** — the people going into, or already working in, stealth: repeat consumer founders and senior operators from big exits.

Corroborating signals (trademark, CT logs, press) earn their place only by making those two jobs sharper. "Nice to have" collectors that don't feed Job 1 or Job 2 are weight, and weight is why coverage sits at 30.4%.

---

## Context you need before touching anything

This repo is the backend for **Bullish's Stealth Startup Finder**. **Read these first, in order:**
1. `app/__init__.py`, `app/services/scheduler.py` (`run_scan_now`, `_run_all_scheduled`) — how scans dispatch.
2. `app/services/delaware.py` — the Form D collector (Job 1).
3. `app/services/confluence.py` — triangulation.
4. `app/services/conviction.py`, `app/services/exit_watch.py`, `app/services/watchlist.py` — the three places people-names currently live (Job 2). `check_exit_alumni_match(text)` is the reference matcher.
5. `app/services/founder_enrichment.py`, `founder_discovery.py` — how founders attach to signals. `run_founder_enrichment(...)` **already accepts `filer_name`.**
6. `app/services/trademark_assignments.py` — links stealth-name filings to operating entities. **Central to both jobs; do not remove.**
7. `app/cli.py` — `export-form-d-officers` already calls `fetch_form_d_related_persons`; reuse it.

The repo's existing `CLAUDE.md` is stale (describes a generic CRUD API). Task 0 fixes that first.

---

## Ground rules (read every time)

- **Do not add new data sources.** Every task sharpens or removes; none adds a collector.
- **Work one task at a time, in order.** Each is independently shippable. Finish, test, stop for review.
- **Tests are part of "done."** Run `pytest` before declaring a task complete. No partial wiring, no failing tests.
- **Preserve the corrected consumer gate:** default-include, exclude only obvious B2B. Industry codes, trademark classes, and raise size are ranking features, never exclusion filters.
- **People change rank, never visibility.** An unknown first-time founder still surfaces, just lower.
- **Removals must be reversible.** Archive before deleting (git history plus, where noted, an exported CSV). Never hard-delete curated data without a saved copy.
- **Keep diffs tight.** No opportunistic refactors or renames.
- **Verify redundancy before deleting a module.** Read callers; confirm nothing in the spine depends on it.

---

## Task 0 — Rewrite `CLAUDE.md` to describe the real system

**Why:** every future Claude Code session starts blind today. Cheapest high-leverage fix; do it first.

**Do:** replace `flask-api/CLAUDE.md` with an accurate map: the product, the signal model (`Item` with `item_type` in {`signal`, `watchlist`}, `SignalEvent`, `ConfluenceHit`), the daily scan flow and its collectors, the enrichment layers, **the two jobs above**, the keep/remove tiering (see "Wiring to remove or consolidate"), and the consumer-gate rule. Keep still-accurate deploy/env sections.

**Acceptance:** a new session reading only `CLAUDE.md` can name the collectors that survive this brief, explain the consumer gate, state the two jobs, and locate the confluence engine. No behavior change.

---

## Task 1 — Pull Form D related persons inline + match against the watchlist  *(Job 1 + Job 2)*

**Why:** the founders/officers named in each Form D are the highest-value field and the core jockey signal, but today they're only fetched in the manual `export-form-d-officers` CLI. The live scan discards them. Biggest deal-recall lever in the codebase.

**Do:**
1. In `delaware.py`, attach a `related_persons` list to each Form D signal via `fetch_form_d_related_persons(adsh, cik)`, in a **bounded, throttled** pass (respect SEC 10 req/s; reuse existing retry/backoff), **only for signals that pass the consumer gate**, capped per scan (parameter, sane default).
2. When persons are present, run the consolidated watchlist matcher (see Task 8) over the names — a Form D naming a watchlist person jumps to the top of the digest with the boost/flag.
3. Set `filer_name` when a related person `looks_like_person`, and flow it into `run_founder_enrichment(..., filer_name=...)` so founder discovery **skips the SerpAPI cascade** when the filer is known.

**Acceptance:** Form D signals carry `related_persons`; a Form D whose officer matches the watchlist surfaces with the boost (test with a fixed EDGAR XML fixture, no live network); a person-named filer skips SerpAPI (asserted in test). Files: `tests/test_delaware_related_persons.py`.

---

## Task 2 — Add person + coined-term join keys to confluence  *(Job 1 + Job 2)*

**Why:** confluence links across channels **only** by `normalize_brand(brand_name)` today. The Filament/Board backtest proved this fails for the best targets (Filament's early trademark was under "Lightyear"; Mirror's Form D entity was "Curiouser Products").

**Do:** generalize `record_signal_and_check_confluence` so a brand clusters on **any of**: normalized brand name, a normalized **person key** (Form D related persons, trademark owners/signatories, press), or a distinctive **coined-term key** (invented product/brand tokens that survive rebrands). Keep the "fires only on a NEW distinct signal *type*" rule.

**Guardrails:** person keys require both first and last name (reuse the both-names rule). Coined-term keys must be distinctive non-dictionary tokens — filter aggressively; a wrong merge is worse than a missed one.

**Acceptance:** a Form D under "Lightyear Incorporated" naming person X and a trademark owned by "Filament Sciences" naming person X cluster into one confluence hit (test with two synthetic signals sharing only a person key).

---

## Task 3 — Capture Form D raise amount from the XML  *(Job 1)*

**Why:** you already fetch `primary_doc.xml` for persons (Task 1); read `offeringData` in the same pass for free.

**Do:** parse `offeringData` → total offering / amount sold / minimum investment; attach `total_offering` and `amount_sold` to the signal; surface in description/notes. **Ranking feature only, never a gate.**

**Acceptance:** raise fields present when in the XML (fixture test); no filtering on raise size anywhere.

---

## Task 4 — Remove the redundant DomainsDB domain check  *(Job 1)*

**Why:** `delaware.py`'s domain corroboration uses DomainsDB (unreliable, exact-slug `.com` only, capped at 60) while a real CT-log collector already exists.

**Do:** remove the DomainsDB path (`check_domain`, the inline domain-append loop, and `check_domains_in_background` if unused — verify callers). CT logs own domain corroboration.

**Acceptance:** no remaining DomainsDB import/use; Delaware still returns Form D signals; CT-log domain signals still triangulate. Tests green.

---

## Task 5 — Stand up a standing coverage metric  *(both jobs)*

**Why:** the July inbox audit (30.4%) was a one-off. Tuning needs a live target. Note `app/services/inbox_audit.py` already exists — build on it rather than starting fresh.

**Do:** surface two metrics on the dashboard / an admin endpoint: **inbox recall** (of deals the team marked "seen" in a period, the % the finder surfaced first — needs a lightweight "we saw this" mark on a brand) and **days-before-newsletter** (lead time for each caught brand later confirmed by press). Keep it a computed endpoint + a tile, not a new subsystem.

**Acceptance:** admin endpoint returns both for a date range; dashboard tile shows current inbox recall; backfill from existing timestamps where possible.

---

## Task 6 — Remove Crunchbase entirely  *(serves neither job)*

**Why:** Crunchbase is post-announcement by definition — it can't beat a newsletter, so it never helps you get *ahead*. It's pure weight.

**Do:** delete `app/services/crunchbase.py`; remove the Crunchbase injection block in `founder_enrichment.py` (the `crunchbase_available()` / `lookup_company()` path and the `_crunchbase_text` / `crunchbase_enriched` handling); remove the `CRUNCHBASE_API_KEY` env var and any references in config, `.env.example`, docs, and tests. Confirm nothing else imports it.

**Acceptance:** no remaining import or reference to Crunchbase anywhere; founder enrichment still runs end-to-end without it; tests green.

---

## Task 7 — Fix or retire NinjaPear founder enrichment  ⚠️ needs Brent's decision  *(Job 2)*

**Why:** `proxycurl.py` now calls NinjaPear/Nubela, which aggregates from **X/Twitter, not LinkedIn**, returns no LinkedIn URLs, and 400s on invented brand names. The batch pull proves the gap (`found: false` for Martin Hoffmann, Tara Bosch, Morgan Zanotti). It silently degrades every founder score.

**Decision required before implementing — do not guess:**
- **Option A (free-first — recommended default):** drop NinjaPear from scoring. Score founders from Form D related persons + watchlist matches + Haiku-on-press. No paid founder data.
- **Option B (paid):** replace NinjaPear with a real LinkedIn-grade provider (People Data Labs, Coresignal, or Proxycurl's actual LinkedIn API) behind the same interface.

**Until the decision lands:** flag NinjaPear-sourced founder data as low-confidence in output so it stops masquerading as solid. Implement A or B only on Brent's word.

---

## Task 8 — Reset the tracked-people lists and consolidate into ONE hand-curated watchlist  *(Job 2)*

**Why:** the people Bullish tracks currently live in three overlapping places — `CONVICTION_FOUNDERS` (`conviction.py`), `EXIT_ALUMNI` (`exit_watch.py`), and `watchlist` rows in the DB. Brent wants to **start over**: keep only names tied to the **top consumer brand exits of the last ~10 years (2016–2026)**, hand-rebuild from there, and never again scatter the list across code and database. This is the reach-out / relationship-building list, so it must be deliberate and curated.

**The list has exactly two designations, and both are kept (see Protected invariant #2):**
- **`founder`** — a person who founded the company behind a top consumer exit.
- **`alumni`** — an operator (a #2–4 seat: President / CMO / CPO / CBO / etc.) who helped that founder build that brand.

Nothing else belongs on the list. No investors, board-only members, category "authorities" without an exit, or speculative names.

**Do:**

1. **Archive first (reversible).** Before removing anything: copy the current `CONVICTION_FOUNDERS` and `EXIT_ALUMNI` dicts into `docs/archive/people_lists_pre_reset_2026-07.md` (verbatim), and export all current DB `watchlist` items to `docs/archive/watchlist_export_pre_reset_2026-07.csv`.

2. **Create one source of truth.** Add a single hand-curated data file — `data/watchlist_seed.csv` — with columns: `name, designation (founder|alumni), role, exit_brand, exit_type (acquisition|IPO|majority), exit_year, category, notes`. This file, not the code dicts, becomes the authoritative watchlist. Load it via a `flask load-watchlist` CLI command into the existing watchlist mechanism. Carry `designation` through matching so the UI can badge a hit **FOUNDER** vs **ALUMNI** (the ALUMNI badge already exists in `exit_watch.py`).

3. **Keep rule (apply to the current lists, then discard the rest):** retain a person **only if** they cleanly fit one of the two designations above **and** the brand had a **major liquidity event (acquisition, IPO, or majority/control sale) in 2016–2026.** `founder` = they founded it; `alumni` = they held a #2–4 operating seat and helped build it. Drop everyone else — authorities with no exit, pre-2016 exits, generic portfolio adds, investors, speculative names. When in doubt, drop it; the list is meant to be small and deliberate, and Brent will add back by hand.

4. **Consolidate the code dicts.** Reduce `CONVICTION_FOUNDERS` and `EXIT_ALUMNI` to thin shims that read from `watchlist_seed.csv` (or empty them and route all matching through the seed-backed watchlist), so there is exactly one place names live — while preserving the founder/alumni split as the `designation` field. Purge DB `watchlist` rows not in the new seed.

5. **Keep the matching mechanism intact.** The both-names, grep-on-flowing-text matcher stays exactly as-is (it's free and unbiased). Only the *contents* reset. A watchlist match remains a **rank boost, never a visibility gate**, and it still feeds triangulation as a person-key (Task 2).

**Note for Brent:** until the seed is repopulated, Form D / press hits won't get founder/alumni boosts — that's the intended clean slate. The seed CSV is where you hand-pick who Bullish tracks and reaches out to. *(Claude can generate a researched starter seed of top 2016–2026 consumer exits → founders + alumni operators, pre-tagged with the two designations, to drop straight into `watchlist_seed.csv` — ask for it separately.)*

**Acceptance:** exactly one authoritative watchlist source (`data/watchlist_seed.csv`) with a `designation` column; code dicts empty or CSV-backed; archives saved; `flask load-watchlist` populates the watchlist; a `founder` and an `alumni` seed entry each trigger the rank boost with the correct badge on a matching signal (test); a person NOT in the seed does not.

---

## Wiring to remove or consolidate — mapped to the two jobs

Handle these as their own small PRs after Tasks 1–8. Order: confident removals first, then verify-then-merge, then pause.

**Remove (confident — serve neither job):**
- **Crunchbase** — Task 6. Post-announcement.
- **DomainsDB check in `delaware.py`** — Task 4. Redundant with CT logs.
- **NinjaPear** — Task 7, pending A/B.

**Verify overlap, then merge to one (likely redundant):**
- **`domain_checker.py` vs `ctlogs.py`** — both touch domains. Confirm what each does; keep the CT-log path (real cert transparency), fold or delete the other.
- **`press_monitor.py` / `newswire.py` vs `press_stealth.py`** — three press-ish services. `press_stealth` is the keeper (it detects "stealth / building something new / unannounced" — the cheap, reliable Job-2 departure signal). Audit `press_monitor` and `newswire` for overlap; if they mostly catch *announced* funding PR (post-announcement, like Crunchbase), demote or merge them into one lean press module feeding confluence.

**Pause (non-core corroborators — keep code, default off; Brent: confirm):**
- **`app_store.py`** and **`producthunt.py`** — "a consumer app is launching" confirmations. Useful someday, but they serve neither core job directly. Put behind default-off flags to cut scan surface. Easy to re-enable.

**Make opt-in (keep as fallback):**
- **SerpAPI founder discovery (`founder_discovery.py`)** — expensive per-brand, but it's the fallback that identifies a founder from a brand-only signal (trademark/domain/press) when Form D didn't name one. Default off; on-demand for high-score brands. Do **not** delete — for Job 2 it's the only bridge from a brand with no filer to a name.

**Explicitly KEEP and lean on (spine):**
- `delaware.py` (+ Task 1 people), `trademarks.py`, **`trademark_assignments.py`** (stealth-name → operating-entity link — critical, elevate), `confluence.py` (+ Task 2 keys), `ctlogs.py`, `enrichment.py`, `press_stealth.py`, the consolidated **watchlist** mechanism (Task 8), `founder_enrichment.py` (simplified per Tasks 6–7), `inbox_audit.py` (Task 5), `scheduler.py`, `email.py` / `slack.py` (output), auth/tokens.

---

## Suggested order & PRs

1. Task 0 — `CLAUDE.md`. Standalone, first.
2. Task 8 — watchlist reset + consolidation. Do early; several later tasks match against it.
3. Task 1 — Form D people + watchlist match. The big recall lever.
4. Task 2 — confluence person/coined-term keys.
5. Tasks 3 & 4 — raise amount + remove DomainsDB (can share a PR).
6. Task 6 — remove Crunchbase.
7. Task 5 — coverage metric.
8. "Wiring to remove or consolidate" — the verify-then-merge and pause PRs.
9. Task 7 — NinjaPear, after Brent picks A or B.

Stop after each task and report: what changed, what the tests cover, and the measured effect where observable (consumer candidates per 1,000 Form Ds, watchlist hits per scan, services still firing on a full scan).
