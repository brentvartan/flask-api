# Bullish Stealth Startup Finder — Claude Code Context

*Last updated: 2026-07-19. This file describes the REAL system, not a generic CRUD API.*

---

## What this project is

The **Bullish Stealth Startup Finder** finds consumer brands before public announcement. It monitors public legal and digital signals — SEC Form D filings, USPTO trademarks, CT logs, press — and surfaces brands at the earliest possible moment, typically 6–24 months before a funding announcement.

**Live URL:** https://web-production-801ed.up.railway.app  
**Health check:** https://web-production-801ed.up.railway.app/health → `{"status": "ok"}`  
**Frontend:** React/Capacitor on GitHub Pages (`stealth-finder-frontend` repo)

---

## The two jobs — the lens for every decision

**Everything in this repo serves one of exactly two jobs.** Wiring that serves neither is a candidate for removal.

1. **Airtight on Form D / Delaware** — catch every consumer company's first capital raise from SEC EDGAR before press. Source: `delaware.py`.
2. **Airtight on identifying and tracking high-potential founders** — repeat consumer founders and senior operators from big exits who may be going into stealth. Source: `data/watchlist_seed.csv` loaded via `flask load-watchlist`, matched by `conviction.py` + `exit_watch.py`.

---

## Protected invariants — never remove or weaken

1. **Multi-signal triangulation (`confluence.py`) is the core edge.** A brand seen in 2+ independent signal types = real. Never reduce what confluence can link; changes may only add linking power.
2. **Two designations — founder and alumni — are permanent.** `data/watchlist_seed.csv` (193 people, authoritative) distinguishes founders from alumni operators. Never collapse into one list.
3. **Consumer gate = default-include.** Exclude only obvious B2B (blocklist + fund-code filter). Industry codes, trademark classes, and raise size are *ranking features, never exclusion filters*.
4. **People change rank, never visibility.** An unknown first-time founder still surfaces, just lower.

---

## Signal model

| Model | Purpose |
|---|---|
| `Item` (`item_type='signal'`) | A surfaced consumer brand signal — one row per brand per scan batch |
| `Item` (`item_type='watchlist'`) | A manually tracked brand |
| `SignalEvent` | One record per (brand_key, signal_type) pair; drives confluence |
| `ConfluenceHit` | Created when a brand's 2nd+ distinct signal type appears |
| `ScanRun` | Log of each scan execution |
| `ScheduledScan` | User-configured recurring scan job |
| `FounderProfile` | Enriched founder data from LinkedIn / Form D |
| `User`, `TokenBlocklist` | Auth |

---

## Daily scan — 7 collectors

`scheduler.py → run_scan_now()` dispatches these in order:

| Collector | Module | Signal type | Lead time |
|---|---|---|---|
| USPTO trademarks | `trademarks.py` | `trademark` | 6–28 mo pre-press |
| SEC Form D (EDGAR) | `delaware.py` | `delaware` | Pre-announcement; first capital raise |
| Product Hunt | `producthunt.py` | `producthunt` | Launch day |
| App Store | `app_store.py` | `app_store` | App launch |
| PR Newswire / BusinessWire | `newswire.py` | `newswire` | First press release |
| CT Logs (crt.sh) | `ctlogs.py` | `ctlogs` | New SSL cert; landing page going up |
| Stealth press | `press_stealth.py` | `press_stealth` | "Building something new", "unannounced", etc. |

**`press_monitor.py`** and **`domain_checker.py`** are partially overlapping services (verify before editing; may be consolidated or removed).

---

## Enrichment layers

After raw signals are saved, enrichment attaches meaning:

1. **`enrichment.py`** — Claude AI (Anthropic) brand scoring against Bullish thesis. Returns `bullish_score` (0–100): HOT ≥70, WARM 55–69, COLD <50. Score is thesis fit, not investment decision.
2. **`founder_enrichment.py`** — orchestrates founder discovery → LinkedIn enrichment. Accepts `filer_name` to skip SerpAPI when the Form D already names the founder.
3. **`founder_discovery.py`** — SerpAPI + Claude Haiku; finds a founder name from a brand-only signal. Default-on but demoted; skipped when `filer_name` is known from Form D.
4. **`proxycurl.py`** — LinkedIn profile enrichment. Currently calls NinjaPear/Nubela (Twitter-based — **being replaced in Task 7** with a by-URL LinkedIn scraper). Do not add new callers.
5. **`trademark_assignments.py`** — resolves a stealth brand-name trademark filing to its operating legal entity. Critical for both jobs — do not remove.

---

## People-matching layer

Two matchers run on every flowing text field (title, description, related persons):

- **`conviction.py` → `check_conviction_match(text)`** — matches against `CONVICTION_FOUNDERS` dict. Gives +boost + CONVICTION badge (⚡). Currently loaded from code dict; will be CSV-backed after Task 8.
- **`exit_watch.py` → `check_exit_alumni_match(text)`** — matches against `EXIT_ALUMNI` dict. Gives +boost + ALUMNI badge (🏆). Same migration path.

**Both-names rule:** a match requires both first and last name to appear. Free grep on flowing text; never blocks visibility, only boosts rank.

**Authoritative source after Task 8:** `data/watchlist_seed.csv` — 193 people; columns: `name, designation (founder|alumni), role, exit_brand, exit_type, exit_year, category, notes, source_url, linkedin_url`. Loaded by `flask load-watchlist`.

---

## Triangulation engine

`confluence.py → record_signal_and_check_confluence(item_id, owner_id, brand_name, signal_type, ...)`

- Normalises brand name via `normalize_brand()` (strips legal suffixes, punctuation) → `brand_key`
- Records a `SignalEvent` row
- If a NEW distinct `signal_type` appears for the same `brand_key` → creates a `ConfluenceHit` and fires an alert

Current join key: normalised brand name only. **Task 2 adds person-key and coined-term-key joins** to close the Lightyear→Filament / Curiouser→Mirror class of miss.

---

## Consumer gate

In `delaware.py` (and analogues in other collectors):

- **Default: include** — assume consumer unless proven otherwise
- **Exclude** if: entity name matches `_NON_CONSUMER_BLOCKLIST` (holdings, capital, consulting, etc.) OR all item codes are in `_FUND_ITEMS` (Investment Company Act fund exemptions — *not* 06b, which covers all operating companies)
- Industry codes, trademark classes, and raise size **rank** signals; they never gate them

---

## Watchlist mechanism

`watchlist.py`:
- `auto_add_to_watchlist()` — adds a brand when it hits a threshold or is manually added
- `trigger_rescore_if_watchlisted()` — re-enriches a watchlist brand when a new signal arrives

---

## CLI commands

```bash
flask re-enrich              # Re-run AI enrichment on signals
flask export-form-d-officers # Export Form D officers to CSV (manual; Task 1 wires this inline)
flask watchlist-mute         # Mute/unmute a founder from digest alerts
flask load-watchlist         # Load data/watchlist_seed.csv into watchlist (Task 8)
flask create-admin           # Create an admin user
```

---

## Wiring status — keep / remove / consolidate

**Spine — keep and strengthen:**
`delaware.py`, `trademarks.py`, `trademark_assignments.py`, `confluence.py`, `ctlogs.py`, `press_stealth.py`, `enrichment.py`, `watchlist.py`, `founder_enrichment.py` (simplified), `inbox_audit.py`, `scheduler.py`, `email.py`, `slack.py`

**Remove (confident):**
- `crunchbase.py` — post-announcement by definition (Task 6)
- DomainsDB path in `delaware.py` (`check_domain`, domain-append loop) — CT logs own this (Task 4)
- NinjaPear path in `proxycurl.py` — Twitter-based, returns found:false for our key targets (Task 7)

**Verify overlap, then decide:**
- `domain_checker.py` vs `ctlogs.py` — confirm what each does; keep CT-log path
- `press_monitor.py` / `newswire.py` vs `press_stealth.py` — audit for overlap; `press_stealth.py` is the keeper (detects stealth language); others may be post-announcement duplicates

**Default-off flags (keep code, disable in full scan):**
- `app_store.py`, `producthunt.py` — corroborators; not core to either job

**Opt-in (default off; on-demand for high-score brands):**
- `founder_discovery.py` (SerpAPI) — expensive per-brand; largely redundant once Form D names the filer

---

## Stack

| Layer | Technology |
|---|---|
| Framework | Flask 3.1 |
| Database | PostgreSQL (Flask-SQLAlchemy + Flask-Migrate) |
| Auth | Flask-JWT-Extended (access + refresh tokens) |
| Scheduler | APScheduler (in-process; job dedup via pg_try_advisory_xact_lock) |
| Rate limiting | Flask-Limiter + Redis |
| Email | Resend SDK |
| LLM scoring | Anthropic API (Claude) — `ANTHROPIC_API_KEY` |
| Founder discovery | SerpAPI — `SERPAPI_API_KEY` |
| LinkedIn enrichment | NinjaPear → **Task 7: replace** — `PROXYCURL_API_KEY` |
| Crunchbase | **Task 6: remove** — `CRUNCHBASE_API_KEY` |
| Error tracking | Sentry |
| Testing | pytest + pytest-flask |
| WSGI | Gunicorn |

---

## Project structure

```
flask-api/
├── app/
│   ├── __init__.py          # App factory (create_app)
│   ├── config.py            # Dev / Test / Production configs
│   ├── extensions.py        # db, migrate, jwt, bcrypt, limiter
│   ├── cli.py               # Flask CLI commands (including flask load-watchlist after Task 8)
│   ├── api/
│   │   ├── auth/routes.py
│   │   ├── items/routes.py
│   │   ├── admin/routes.py  # Admin tools, scan triggers, inbox audit
│   │   ├── enrich/routes.py
│   │   ├── scans/routes.py
│   │   └── ...
│   ├── models/
│   │   ├── item.py          # Item (signal + watchlist rows)
│   │   ├── signal_event.py  # One per (brand_key, signal_type)
│   │   ├── confluence_hit.py
│   │   ├── scan_run.py
│   │   ├── scheduled_scan.py
│   │   └── founder_profile.py
│   ├── services/
│   │   ├── delaware.py      # Form D collector — Job 1 spine
│   │   ├── trademarks.py    # USPTO collector — Job 1 spine
│   │   ├── trademark_assignments.py  # Stealth-name → entity resolver — CRITICAL
│   │   ├── confluence.py    # Triangulation engine — the core edge
│   │   ├── enrichment.py    # Claude AI brand scorer
│   │   ├── conviction.py    # Conviction-founders matcher (FOUNDER badge)
│   │   ├── exit_watch.py    # Exit-alumni matcher (ALUMNI badge)
│   │   ├── watchlist.py     # Watchlist auto-add + rescore triggers
│   │   ├── founder_enrichment.py  # Founder enrichment orchestrator
│   │   ├── founder_discovery.py   # SerpAPI + Haiku founder finder (opt-in)
│   │   ├── proxycurl.py     # LinkedIn profile fetcher (being replaced Task 7)
│   │   ├── press_stealth.py # Stealth-language press monitor — keep
│   │   ├── ctlogs.py        # CT log SSL cert monitor — keep
│   │   ├── newswire.py      # PR Newswire/BusinessWire RSS
│   │   ├── press_monitor.py # Press monitor (verify overlap before editing)
│   │   ├── domain_checker.py # Domain check (verify vs ctlogs before editing)
│   │   ├── producthunt.py   # (default-off after wiring trim)
│   │   ├── app_store.py     # (default-off after wiring trim)
│   │   ├── crunchbase.py    # REMOVING — Task 6
│   │   ├── inbox_audit.py   # Coverage metric (Task 5 builds on this)
│   │   ├── scheduler.py     # APScheduler + scan dispatch
│   │   ├── email.py         # Resend email sending
│   │   └── slack.py         # Slack webhook notifications
│   └── utils/
│       └── categories.py    # CATEGORY_KEYWORDS used by consumer gate
├── data/
│   └── watchlist_seed.csv   # 193 hand-curated founders + alumni — authoritative (Task 8)
├── docs/
│   ├── REBUILD_BRIEF.md     # Implementation work order — read before editing
│   ├── FRESH_LOOK.md        # Strategic rationale — read before editing
│   └── archive/             # Pre-reset archives (people lists, old watchlist export)
├── tests/
├── migrations/
├── requirements.txt
├── Dockerfile
├── railway.json
└── Procfile
```

---

## Environment variables

### Local dev (`.env`)
```
FLASK_ENV=development
SECRET_KEY=<dev>
JWT_SECRET_KEY=<dev>
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/flask_api_dev
REDIS_URL=redis://localhost:6379/0
MAIL_SUPPRESS_SEND=true
ANTHROPIC_API_KEY=<dev or Railway>
```

### Production (Railway — set via dashboard Raw Editor)
```
FLASK_ENV=production
SECRET_KEY=<Railway>
JWT_SECRET_KEY=<Railway>
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
SENTRY_DSN=<Railway>
RESEND_API_KEY=<Railway>
MAIL_FROM=noreply@mail.bullish.co
FRONTEND_URL=https://brentvartan.github.io/stealth-finder-frontend
ANTHROPIC_API_KEY=<Railway>       # Claude brand scoring
SERPAPI_API_KEY=<Railway>         # Founder discovery (SerpAPI)
PROXYCURL_API_KEY=<Railway>       # LinkedIn enrichment (NinjaPear → Task 7 replaces)
CRUNCHBASE_API_KEY=<Railway>      # REMOVING — Task 6
```

---

## Railway deployment

- **Dashboard:** https://railway.com/project/9d4dcc81-48e1-4485-8039-f16f3d54f928
- **Service ID:** 4af5b3e6-6258-4300-94e3-4663d9d37be5
- **Deploy trigger:** push to `main` OR manual deploy from dashboard
- **Use Raw Editor** to add env vars with special chars

---

## Testing

```bash
pytest                               # Full suite
pytest --cov=app --cov-report=term-missing
pytest tests/test_delaware_related_persons.py -v  # Task 1 fixture test
```

- Test config: `MAIL_SUPPRESS_SEND=true`, rate limiting disabled, `flask_api_test` Postgres DB required
- No live network calls in tests — fixture data only

---

## Key design decisions

- **Job lock:** `pg_try_advisory_xact_lock` + TTL row prevents duplicate scheduled jobs across Gunicorn workers
- **Confluence dedup:** signals cluster on `brand_key` (normalised brand name); confluence fires only on NEW distinct `signal_type` — not on repeated signals from the same source
- **Consumer gate:** default-include is intentional; a false positive (HOT brand not funded) is far less costly than a false negative (HOT brand never seen)
- **People are rankers, not gatekeepers:** conviction/alumni matches boost score and badge; they never hide an unknown founder
- **Form D scope:** covers all 50 US states (federal SEC filing); `06b` Reg D exemption is the standard consumer-brand filing — do not exclude it
- **SerpAPI is opt-in:** expensive per-brand; skipped when `filer_name` is known from Form D (Task 1 wires this)
