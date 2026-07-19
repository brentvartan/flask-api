# Stealth Finder Rebuild — Claude Code Runbook

How to drive the rebuild yourself in Claude Code. Full detail lives in `docs/REBUILD_BRIEF.md`; this is the driver's guide — the order, the exact prompt to paste for each step, and what to check before moving on.

## Before you start

Open a terminal in the repo and launch Code:
```bash
cd ~/Desktop/Desktop/Work/Bullish/Products/Apps/flask-api
claude
```
Everything Code needs is already in the repo: `docs/REBUILD_BRIEF.md`, `docs/FRESH_LOOK.md`, `data/watchlist_seed.csv`.

**Rule of thumb: one task per prompt.** Review the diff, let it run `pytest`, then move on. Don't batch tasks.

## Hold these four rules yourself (Code has them in the brief too)

1. Triangulation (`confluence.py`) is never removed — only strengthened.
2. Founder + alumni are permanent designations.
3. Consumer gate = default-include, exclude only obvious B2B.
4. People change rank, never visibility.

## Kickoff — paste this first

> Read `docs/REBUILD_BRIEF.md` and `docs/FRESH_LOOK.md` in full, plus the files under "Context you need before touching anything." Confirm back to me: the two jobs, the protected invariants, and the task order. Don't change any code yet.

It should parrot the two jobs + invariants back. That tells you it's grounded before it touches anything.

## Then run the tasks in this order — one prompt each

**1 · Task 0 — rewrite CLAUDE.md**
> Do Task 0 only: rewrite `CLAUDE.md` to describe the real system per the brief. Show me the diff and a 3-line summary. Don't touch anything else.

Check: CLAUDE.md now names the real services, the two jobs, and the gate. No code changed.

**2 · Task 8 — consolidate the watchlist**
> Do Task 8: consolidate the people lists into the seed-backed watchlist. `data/watchlist_seed.csv` already exists — archive the old `CONVICTION_FOUNDERS`/`EXIT_ALUMNI`, build the `flask load-watchlist` command, keep the founder/alumni designations. Run pytest. Show diff + summary.

Check: one source of truth; old lists archived; tests green. (LinkedIn URLs can still be blank here.)

**3 · Task 1 — Form D people (the big recall lever)**
> Do Task 1: pull Form D related persons inline and match them against the watchlist; set `filer_name` so founder discovery skips SerpAPI when the filer is known. Add the fixture test. Run pytest.

Check: Form D signals carry `related_persons`; a watchlisted officer surfaces boosted; test passes.

**4 · Task 2 — confluence person/coined-term keys**
> Do Task 2: add person + coined-term join keys to confluence without weakening brand-name matching. Add the two-signal test.

Check: a Form D under a placeholder entity + a trademark sharing a person cluster together.

**5 · Tasks 3 & 4 — raise amount + drop DomainsDB**
> Do Tasks 3 and 4: capture Form D raise amount from the XML, and remove the DomainsDB domain check (CT logs own domain corroboration). Run pytest.

**6 · Task 6 — remove Crunchbase**
> Do Task 6: remove Crunchbase entirely — module, the injection in `founder_enrichment.py`, the env var, tests. Confirm nothing else imports it. Run pytest.

**7 · Task 5 — coverage metric**
> Do Task 5: stand up the coverage metric (inbox recall + days-before-newsletter) building on `inbox_audit.py`. Admin endpoint + dashboard tile.

**8 · Wiring trims**
> Do the "Wiring to remove or consolidate" section: verify `domain_checker` vs `ctlogs` and `press_monitor`/`newswire` vs `press_stealth`, merge the redundant ones, and put `app_store` + `producthunt` behind default-off flags. Show me what you're removing before you delete.

Your call here: confirm you're OK pausing `app_store` + `producthunt`.

**9 · Task 7 — LinkedIn source (needs your two inputs first — see below)**
> Do Task 7: replace NinjaPear with the by-URL LinkedIn source behind `proxycurl.py`'s interface, add the monthly watchlist sweep keyed on the seed `linkedin_url`, keep free signals primary. Run pytest.

## The two things only you decide (for Task 7)

- **Provider:** pick a by-URL LinkedIn scraper (ScrapIn / Scrapingdog class — cheapest that reads *real* LinkedIn) and put its API key in `.env`.
- **`linkedin_url` column:** fill it in `data/watchlist_seed.csv` before the Task 7 sweep runs (not needed for Tasks 0–6). *Claude/Cowork can take a pass at populating these for you anytime — just ask.*

## Between every task

- Read the diff. If it touched confluence, confirm it only *added* linking power.
- Let it run `pytest`; don't accept "done" with failing tests.
- Commit when happy: `git add -A && git commit -m "task N: ..."` — or just tell Code to commit.

## If you get stuck

- Point Code back at the specific task section in `docs/REBUILD_BRIEF.md`.
- The brief's **acceptance criteria are the definition of done** for each task — hold Code to them.
- If it starts sprawling (adding sources, big refactors), stop it and re-read it the Ground rules.
