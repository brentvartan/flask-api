# Stealth Startup Finder — Fresh Look & Recommendations

*July 19, 2026 · reviewed against the live `flask-api` build on your machine + the June rebuild spec and Filament/Board backtest in Drive*

## Bottom line up front

The finder is real now — a deployed multi-signal engine (SEC Form D, trademarks, CT logs, press, Product Hunt, App Store, newswire), not the fake-data UI demo the February teardown found. That's the good news, and it's a lot of good news.

The bad news is the thing you already feel: it has drifted from *depth* toward *breadth*. There are ~25 service modules now, and by the tool's own **July 2026 inbox audit it surfaced only 30.4% of your deal inbox — 16 of 23 brands you actually saw never showed up.** That number is the whole story. You are not missing deals because you have too few sources; you're missing them because the two or three sources that matter most aren't airtight, and effort is spread across a dozen that don't move the needle.

So the answer to "how can Claude Code make it work harder" is mostly **subtraction and sharpening, not addition.** Make Form D + trademark + one founder-linking key excellent; demote everything else to supporting cast; and instrument coverage so tuning has a target. Three specific, high-leverage fixes below would, on the evidence, move that 30.4% more than any new source could.

---

## What it actually is today (so we're grounded)

**Daily "full" scan runs 7 collectors:** trademark, delaware (Form D via EDGAR, all 50 states), Product Hunt, App Store, newswire, CT logs, press-stealth. On top of that sit enrichment layers: an LLM brand-scorer (`enrichment.py`), a confluence/triangulation engine, a conviction-founders list, an exit-alumni list, per-brand founder discovery (SerpAPI + Claude Haiku), founder profile enrichment (NinjaPear/Nubela), optional Crunchbase, plus email digests, Slack, and a chat interface. Frontend is a React/Capacitor app (iOS-wrapped) on GitHub Pages; backend is Flask on Railway with Postgres + Redis.

The **core design is sound and worth protecting**: multi-signal triangulation, a persistent signal store, exclusion-based consumer gating (default-include, drop only obvious B2B — exactly the corrected intent from your backtest), and people-as-a-ranking-layer via conviction/alumni name matching. The bones are right. The execution has thinned out because there's too much of it.

---

## The drift, named

Three concrete drifts, in order of how much they cost you:

**1. Breadth over depth.** Seven daily collectors and a dozen enrichment services, each half-tuned. The coverage roadmap itself diagnoses the shared disease: every engine *caps results before sorting by recency and under-targets the query*, so each inspects a small, often stale slice. Some of that has been patched (trademark, Delaware, CT logs in July), but the pattern recurs because there's simply a lot of surface area to keep tuned. The 30.4% is the symptom of spreading attention thin.

**2. The context file lies to Claude Code.** `flask-api/CLAUDE.md` still describes a "generic items resource" CRUD API — auth, items, admin. It mentions *none* of the 25 real services, the signal model, the scan architecture, EDGAR, trademarks, confluence, or the consumer thesis. **Every time you point Claude Code at this repo, it starts blind and half-reinvents context.** This is quietly the biggest tax on making the tool "work harder," because it degrades every future session. It's also the cheapest thing on this list to fix.

**3. Triangulation quietly regressed to the exact failure your backtest predicted.** The confluence engine matches signals across channels *only* by normalized brand name (`normalize_brand`). Your own Filament/Board post-mortem said in plain terms: **entity name ≠ brand name** — Filament's earliest trademark was filed under "Lightyear"; Mirror's Form D entity was "Curiouser Products." Brand-name-only matching cannot link those. The spec's fix — match on *people and coined product terms* — was written down and not built. So the highest-value linking mechanism is missing precisely for the stealthiest, most valuable targets.

---

## Concern 1 — Make Form D / Delaware "plucking" airtight

This is in better shape than you fear. **Keep:** the full-universe EDGAR sweep (it probes the true filing count and pages the whole window now, no silent cap), the exclusion-based consumer gate (blocklist + fund-code filter — correctly "prove it's *not* consumer," matching your final gate rule), and the Delaware-incorporation score boost. The July rewrite that stopped rejecting 06b Reg D filings was a genuinely important fix — that one bug was dropping ~99% of consumer Form Ds.

**Fix, highest leverage first:**

**A. Pull Form D *related persons* inline during the scan — this is the single biggest miss.** Right now the founders/officers named in each filing (the highest-value field in a Form D, and *the* jockey signal you care about) are only fetched in a manual CLI export to a Clay/Apollo CSV. The live scan throws them away. Wire `fetch_form_d_related_persons` into the scan so every Form D signal carries its people, then immediately (a) match those names against your conviction + alumni lists — this is the mechanism that's supposed to ping the day a "Curiouser Products"-style entity files listing a Brynn Putnam, and it currently doesn't fire; and (b) pass the person straight into founder enrichment as the filer, which is both *more accurate* and *skips 3–5 SerpAPI calls per brand*.

**B. Add people + coined-term join keys to confluence.** Fixes the Lightyear→Filament / Curiouser→Board class of miss directly. A brand should triangulate if the *same person* or *same coined term* (Tensilyx, PieceSense) shows up across Form D, trademark, and press — even when the display names differ.

**C. Capture the raise amount while you're already in the XML.** You fetch `primary_doc.xml` for related persons anyway; read `offeringData` in the same pass to get total offering / amount sold. Free raise-size ranking and a "priced round" flag.

**D. Delete the weak domain check.** The Delaware service's own domain corroboration uses DomainsDB (unreliable), exact-slug `.com` only, capped at the first 60 signals — and you already run a real CT-log service. Drop the redundant path and let CT logs do domain corroboration.

---

## Concern 2 — De-overload: cut to a spine + supporting cast

You don't need fewer *ideas*; you need a clear tier so attention lands where recall is won.

**Spine — make these three airtight, tune weekly:** Form D (+ inline people, per Concern 1), Trademark (intent-to-use 1b filings are your 6–28-month lead — already scored, keep sharpening owner targeting), and one founder-linking layer (the free name-harvest below).

**Corroborators — keep, keep cheap, never let them gate:** CT logs, press-stealth, Product Hunt, App Store, newswire. These raise confidence and occasionally originate; they should add score, never exclude.

**Candidates to pause or cut:** Crunchbase (post-announcement by definition — it can't beat a newsletter, so it's confirmation at best); per-brand SerpAPI founder *discovery* (expensive and largely redundant once Form D hands you the actual filer); NinjaPear enrichment (see Concern 3 — it's silently degrading scores). Pausing these reduces the tuning surface without touching recall.

**Instrument coverage so this stops being guesswork.** The 30.4% audit was a one-off. Make two numbers standing, weekly, on the dashboard: **inbox recall** (of deals you saw this week, how many did the finder surface first) and **days-before-newsletter** per catch. Without a target, every "make it work harder" is a vibe; with one, Claude Code can tune against it and you can see the line move.

---

## Concern 3 — Best / best-cost founder & future-founder tracking

Today there are **four overlapping mechanisms**, and they're not clearly divided into "who to watch" vs. "how much do we pay":

- **Conviction + exit-alumni lists** (hand-curated, matched by free grep on signal text). *Cheap, unbiased in cost, good.* This is exactly the "free grep on data already flowing" the spec endorsed. Keep and grow it — but it stays a *rank* booster, never a visibility gate.
- **Per-brand SerpAPI + Haiku founder discovery.** Moderate per-brand cost, and mostly redundant once Form D gives you the filer. Demote.
- **NinjaPear / Nubela profile enrichment** (~$0.04/founder). Cheap, but it aggregates from **X/Twitter, not LinkedIn**, doesn't return LinkedIn URLs, and 400s on invented brand names. Your own batch pull is the proof: `found: false` for Martin Hoffmann, Tara Bosch, Morgan Zanotti — the exact operator-departure cases the tool exists to catch. It's quietly dragging founder scores down.
- **The manual quarterly LinkedIn/departure pull** (`founder_linkedin_pull.py` → `phase1_departures.json`). Right idea — track top operators, watch for a move to something new — but it's manual and runs on the same spotty NinjaPear data.

**The best-cost architecture I'd point Claude Code at:**

*Discovery (who to watch) should be free and unbiased.* Harvest people from records already flowing — Form D related persons + trademark owners/signatories + press mentions. You're already exporting 765 officers to a seed list; that instinct is right, just wire it into the pipeline instead of a manual export. Names never decide what gets *looked at*, only what's the *same company* and how it *ranks*.

*The jockey layer stays free grep.* Conviction + alumni matching on that flowing text. Grow the lists; they're your thesis encoded.

*For "future founders leaving jobs," don't buy LinkedIn scraping to do discovery.* NinjaPear already shows the coverage isn't there for the people you care about. The cheap, reliable departure signal is press-phrase monitoring you already run (`press_stealth`: "stealth", "building something new", "unannounced"). **Then, and only if you want active departure alerts on named operators, buy one real people-data provider** — and this is the one genuine spend decision on the table:

- **Free-first (recommended default):** Form D people + trademark owners + press-phrase monitoring + conviction grep. $0 incremental. Prove recall against the new weekly metric before spending.
- **Clay / Apollo** on the officer seed list (you're already formatted for it) — enrichment and outreach, modest cost, good for *reaching* people you've found.
- **A LinkedIn-grade provider** (People Data Labs, Coresignal, or Proxycurl's actual LinkedIn API) *only* if you want true departure monitoring on a watchlist — real cost, real coverage. Worth it only after free-first proves the watchlist earns it.

**Either way, fix or retire NinjaPear.** Point founder enrichment at a real LinkedIn source or drop it and score founders from Form D people + Haiku-on-press. Leaving it as-is means every founder score is quietly built on Twitter-grade, gap-riddled data.

---

## What I'd have Claude Code do first (concrete, ordered)

1. **Rewrite `flask-api/CLAUDE.md` to describe the real system** — services, signal model, scan flow, the consumer thesis, the spine-vs-corroborator tiering. ~30 minutes, and it makes every future Claude Code session sharper. Do this before any code change.
2. **Wire Form D related-persons inline + conviction match** (Concern 1A/1B). Highest deal-recall leverage in the codebase.
3. **Add person + coined-term keys to confluence** (Concern 1B). Closes the Filament/Board miss class.
4. **Stand up the weekly coverage metric** (inbox recall + days-before-newsletter). Gives every later change a target.
5. **Retire the redundant/degraded paths** — Delaware's DomainsDB check, and NinjaPear (pending your founder-tracking decision).

## The one thing that needs your call

Founder-tracking spend: **free-first** (Form D people + press + conviction grep, $0) vs. **paid departure monitoring** ($ per month for a real people-data provider). My recommendation is free-first — build the free net, prove recall against the new metric, then buy the provider only if the watchlist has earned it. But that's a budget/urgency judgment that's yours, so I've left it as a decision rather than assuming it.

---

*Sources: live `flask-api` codebase (delaware, confluence, categories, proxycurl, founder_discovery, founder_enrichment, exit_watch, conviction, scheduler, cli) and data files (`form_d_officers_seed_list.csv`, `phase1_departures.json`, `founder_pull_results.json`) on your machine; `stealth-finder-rebuild-spec.md` and the Filament/Board backtest, and `CORE_COVERAGE_ROADMAP.md`.*
