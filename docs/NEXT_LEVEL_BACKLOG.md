# Next-level backlog — deferred work

*Written 2026-08-04, when Brent capped the run at two weeks. Nothing here should
start before the cap questions are answered on 2026-08-18.*

---

## Read this first

The system currently **finds** well and **orders** poorly, and nobody has yet
acted on a digest. That last fact outranks everything below.

Two weeks of running will answer a question no amount of building can: **does
anyone open these emails and do something?** If the answer is no, then none of
the items below are the problem, and the honest move is to stop rather than to
build item 1. Most of this list argues for itself; the list as a whole argues
for waiting.

Ranked by *what it unlocks*, not by effort.

---

## Tier 1 — the things that change what the product can claim

### 1. Historical backfill of the corpus
**The corpus has no memory before 2026-03-25.** Everything stored was collected
by scans since then; there is no backfill.

This is why the recall exercise died. 265 companies from the IC deck scored 4.5%
recall, and **zero of them had been reviewed inside the window the corpus
covers** — they were all seen by the committee before the Finder had collected a
single signal. The number measured nothing.

Until this is fixed the system cannot answer *"did we know about X?"* for
anything before late March, which is nearly every deal in the portfolio.

- Backfill USPTO trademark filings and SEC Form D for the preceding 24 months.
- Sizing unknown; USPTO bulk data is free, the cost is scoring time, and scoring
  is the binding constraint (a full night's budget is 3,000 signals).
- **Do not backfill and score indiscriminately.** Backfill the *index* cheaply,
  score only on demand — which is item 2.

**Unlocks:** recall becomes measurable at all. Confluence gets real depth,
because triangulation across 4 months of history is thin by construction.

### 2. Source-date lookup — recall without the corpus and without a human
Given a brand name, return the earliest public record: USPTO filing date, SEC
Form D date. Retrospective and complete, so it works for a 2012 deal as well as
a 2026 one.

This is the instrument the recall backtest actually needed. It answers *"was the
signal publicly there before a human heard of it?"* — which is the real
question, since if it was there the Finder would surface it.

**It needs no input from Mike or anyone else.** Brent's `heard_at` dates would
sharpen it, but a version keyed to *investment date* instead of first-hear works
today with data already in the fund deck. That was the fallback I should have
reached for the moment Mike's availability was in doubt.

**Unlocks:** the one number nobody has — how early this thing actually is.

---

## Tier 2 — known defects, small and real

### 3. The CT-logs engine is dark
`ctlogs.py` calls crt.sh, which returns 429/404 under throttling. Because
`_query_crtsh` swallows exceptions in a bare `except`, futures never raise, the
`errors` list is always empty, and the error-reporting branches are dead code.

**A silent zero is indistinguishable from a quiet day.** Either move to a source
with a real API contract (Censys, certstream) or delete the engine. Leaving a
dead collector wired in is worse than not having it, because the dashboard
implies coverage that does not exist.

### 4. 344 duplicate rows
Corpus hygiene. Inflates every count, including the ones quoted in the budget
argument. Cheap to fix, and worth doing before any figure is quoted externally.

### 5. Manual-run in-flight guard
A manually triggered scan can collide with a scheduled one. The advisory lock
covers the scheduled path; the manual path can still start work mid-flight.
Last open item from the 2026-07-27 verification sweep.

### 6. `brand_uncertain` retroactivity
Inconsistent drop-vs-mark handling: some paths drop an uncertain brand, others
mark it. Pick one. Also from the same sweep.

---

## Tier 3 — only if the cap questions come back positive

### 7. A feedback loop that costs nothing to use
Theme weights are currently derived from **24 picks on one Tuesday**, shrunk hard
toward neutral for exactly that reason. They are a stated preference, not a
learned one.

A one-click *"I'd open this"* / *"not for us"* in the digest itself would improve
ordering without ever scheduling another labelling session. **This is the highest
value-per-effort item on the page** — but it is worthless if nobody is opening
the digest, which is why it sits below the cap.

### 8. Ordering, properly
Score predicts membership (100% agreement on gate failures, ~93% above 45) and
carries **no information about order** (7/24 overlap against 30% random). Theme
weighting is v1 of a fix and is itself unvalidated — it encodes one afternoon's
preferences.

Next honest step is not a better model, it is *more signal*: item 7, or
open/click data from the digest.

### 9. Deeper confluence joins
Currently joins on `brand_key`, person keys, coined-term keys. Domain and Form D
officer-name joins would catch the Lightyear→Filament class of miss that
brand-name matching cannot.

Constrained by invariant 1: linking power may only ever **grow**.

### 10. Digest → action
There is no way to say *"track this one"* from the email. Every action requires
leaving it and finding the brand in the app. That friction is a plausible
explanation for zero recorded actions, and it is cheaper to fix than anything
upstream of it.

---

## Explicitly not doing

- **Rebuilding the two scan paths into one.** ~800 lines of live spend-and-email
  code; the grep-based drift tests are the cheaper control. See CLAUDE.md.
- **Chasing precision further.** It is already good and is not the bottleneck.
- **Another labelling exercise.** Two have been run. The marginal return is low
  and the ask on a human is high — item 7 replaces it.
- **Re-opening the CSP / tier-floor / weekly-digest decisions.** Settled.

---

## The measurement that is still missing

Everything measurable here is **precision** — of what we surface, how much is
good. Recall is unmeasured, and a brand the Finder never surfaced does not read
as a miss; it does not read at all.

Items 1 and 2 exist to close that. Until one of them lands, any claim that this
system is "early" rests on the fact that it *watches early sources*, not on
evidence that it *caught anything early*. That distinction should be stated
plainly to anyone who asks, including LPs.
