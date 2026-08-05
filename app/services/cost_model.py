"""
The declared cost model behind the Settings → Spend page.

WHY A REGISTRY AND NOT PROSE ON THE PAGE
Every number a user reads should carry how it is known. Exactly one line on that
page is measured — Anthropic spend on Stealth Finder, metered per call in
cost.py. Everything else is a list price, a plan ceiling, or an estimate derived
from a row count. Presenting them in the same typeface with the same confidence
is how a dashboard starts lying: the reader cannot tell the $18.45 we counted
from the $101.91 we guessed.

So every entry declares a `basis`:

  metered   — counted from real API usage as it happened. Trustworthy.
  derived   — a row count times a fixed per-unit guess. Directionally useful,
              wrong in detail, and wrong in a specific direction (see below).
  plan      — a published subscription price. Accurate but only if the plan is
              what we think it is.
  estimate  — a judgement. Verify on the provider's dashboard before acting.

VERIFY_ON is the staleness marker. Third-party pricing moves and nothing here
watches it; a figure with an old date should be re-checked, not trusted.

NOT BILLING. This never sees an invoice. It is a model of what we believe we
spend, useful for spotting a runaway, useless for reconciling a statement.
"""
from datetime import date

# Last time a human checked these against the providers' own pricing pages.
# Bump it when you re-verify; do not bump it when you only edit wording.
VERIFIED_ON = date(2026, 8, 5)

# ─── Stealth Startup Finder ───────────────────────────────────────────────────

_STEALTH_FINDER = [
    {
        "service": "Anthropic (Claude)",
        "basis": "metered",
        "cap_usd": 250.0,
        "cap_kind": "hard",
        "what_it_pays_for": "Scoring every collected signal against the Bullish thesis, "
                            "the cheap Haiku triage pass in front of it, founder lookups, "
                            "and the chat assistant.",
        "what_makes_it_grow": [
            "Signals scored per night — the single biggest lever. Each scored signal "
            "costs about $0.015.",
            "How long the model's answer is. Output tokens are billed 5x input and are "
            "roughly two thirds of the cost of a scored signal.",
            "Cache misses on the ~7,600-token thesis prompt. It is identical on every "
            "call and billed at ~10% when cached; editing it mid-run makes every "
            "following call pay full price.",
            "Collection volume, indirectly — everything collected gets triaged, and "
            "triage is cheap but not free.",
        ],
        "controls": [
            "Hard stop at the cap. Scoring refuses to run; collection continues and the "
            "backlog is picked up next month.",
            "Past 80% the run conserves: Form D and confluence candidates are scored "
            "before the high-volume trademark stream.",
            "SCAN_ENRICH_BUDGET caps signals per run; the dollar cap overrides it.",
        ],
    },
    {
        "service": "Railway (hosting)",
        "basis": "plan",
        "cap_usd": None,
        "cap_kind": "none",
        "what_it_pays_for": "The always-on API, the Postgres database, and Redis.",
        "what_makes_it_grow": [
            "Usage-based: container uptime, memory, and database size.",
            "Corpus growth. The database is ~26,000 signals and grows every night.",
        ],
        "controls": [
            "No cap. A trial expiry took the app offline in May 2026 — the failure mode "
            "here is the service stopping, not a surprise bill.",
        ],
    },
    {
        "service": "SerpAPI (founder search)",
        "basis": "plan",
        "cap_usd": 0.0,
        "cap_kind": "quota",
        "what_it_pays_for": "Finding a founder's name from a brand-only signal.",
        "what_makes_it_grow": ["Searches per month. Free plan allows 250."],
        "controls": ["Opt-in and default-off. Skipped entirely when a Form D already "
                     "names the filer, which is most of the time."],
    },
    {
        "service": "Proxycurl / FreshData (LinkedIn)",
        "basis": "derived",
        "cap_usd": None,
        "cap_kind": "none",
        "what_it_pays_for": "LinkedIn profile enrichment for founders on hot signals.",
        "what_makes_it_grow": ["Per-lookup, about $0.01 each."],
        "controls": ["Currently returning HTTP 401, so nothing is being spent — but "
                     "there is no cap on it if the credential is fixed."],
    },
    {
        "service": "Resend (email)",
        "basis": "plan",
        "cap_usd": 0.0,
        "cap_kind": "quota",
        "what_it_pays_for": "The Monday digest and confluence alerts.",
        "what_makes_it_grow": ["Emails sent. Free tier covers 3,000/month."],
        "controls": ["One digest a week to a short list. Nowhere near the tier."],
    },
]

# ─── Brand Manager ────────────────────────────────────────────────────────────
# A separate application on a separate stack. Nothing here is metered from this
# app — these are list prices and estimates, shown so the whole picture sits in
# one place rather than because we can measure them.

_BRAND_MANAGER = [
    {
        "service": "Vercel (hosting)",
        "basis": "estimate",
        "cap_usd": None,
        "cap_kind": "none",
        "what_it_pays_for": "Hosting the Next.js app, its API routes, and cron jobs.",
        "what_makes_it_grow": [
            "Function execution time. Agent runs are configured up to 120s, the Inngest "
            "handler up to 600s — long-running functions are the main driver.",
            "Invocations and bandwidth as usage grows.",
            "Seats, if more people are added to the team.",
        ],
        "controls": [
            "maxDuration is set per route in vercel.json, which bounds the worst case "
            "per invocation but not the number of invocations.",
            "No spend cap configured. Vercel's own spend controls would have to be set "
            "in their dashboard.",
        ],
    },
    {
        "service": "Supabase (database + auth)",
        "basis": "estimate",
        "cap_usd": None,
        "cap_kind": "none",
        "what_it_pays_for": "Postgres, authentication, and row-level security for every brand.",
        "what_makes_it_grow": [
            "Database size and egress. Free tier is 500MB and 5GB egress; Pro is $25/mo "
            "with 8GB included.",
            "Per-brand data. Each brand onboarded adds rows across every table.",
        ],
        "controls": ["No cap. Overage on the paid tier is billed, not blocked."],
    },
    {
        "service": "Anthropic (Claude)",
        "basis": "estimate",
        "cap_usd": None,
        "cap_kind": "none",
        "what_it_pays_for": "The marketing agents — briefings, monthly reviews, strategy work.",
        "what_makes_it_grow": [
            "Agent runs per brand per month.",
            "How much context each run loads. Agents that read a whole brand's history "
            "cost far more per run than ones that read a summary.",
        ],
        "controls": [
            "NOT capped. The $250 ceiling covers Stealth Finder only — it is enforced in "
            "Stealth Finder's own code and knows nothing about this app.",
        ],
    },
    {
        "service": "Inngest (background jobs)",
        "basis": "estimate",
        "cap_usd": None,
        "cap_kind": "none",
        "what_it_pays_for": "Queued and scheduled agent work that outlives a web request.",
        "what_makes_it_grow": ["Steps executed per month; free tier then usage-based."],
        "controls": ["No cap configured."],
    },
    {
        "service": "Sentry (error tracking)",
        "basis": "plan",
        "cap_usd": None,
        "cap_kind": "quota",
        "what_it_pays_for": "Error and performance monitoring.",
        "what_makes_it_grow": ["Events per month. A noisy new bug can burn the quota fast."],
        "controls": ["Quota-based — events are dropped past the tier rather than billed."],
    },
    {
        "service": "Resend, Stripe, Klaviyo, Gorgias, Meta, Google Ads",
        "basis": "estimate",
        "cap_usd": None,
        "cap_kind": "none",
        "what_it_pays_for": "Email, billing, and the customer integrations the agents read from.",
        "what_makes_it_grow": [
            "Mostly per-brand and mostly on the customer's own accounts rather than ours "
            "— but worth listing so the surface is visible.",
        ],
        "controls": ["Varies by provider; none of it is capped from our side."],
    },
]


def cost_model(metered: dict = None) -> dict:
    """
    The full declared picture, with the one measured number folded in.

    `metered` is cost.summary(); when present its real figures replace the
    Anthropic/Stealth Finder entry's placeholders so the page shows the counted
    number rather than a repeated guess.
    """
    sf = [dict(e) for e in _STEALTH_FINDER]
    if metered and "error" not in metered:
        for e in sf:
            if e["basis"] == "metered":
                e["spent_usd"] = metered.get("spent_usd")
                e["cap_usd"] = metered.get("cap_usd")
                e["remaining_usd"] = metered.get("remaining_usd")
                e["pct_used"] = metered.get("pct_used")
                e["exhausted"] = metered.get("exhausted")
                e["month"] = metered.get("month")
    return {
        "verified_on": VERIFIED_ON.isoformat(),
        "products": [
            {"product": "Stealth Startup Finder",
             "note": "Anthropic spend here is measured per call. Everything else on this "
                     "list is a plan price or an estimate.",
             "services": sf},
            {"product": "Brand Manager",
             "note": "A separate application on a separate stack. Nothing here is "
                     "measured from this app — these are list prices and estimates, "
                     "shown so the whole surface sits in one place.",
             "services": [dict(e) for e in _BRAND_MANAGER]},
        ],
        "basis_key": {
            "metered":  "Counted from real API usage as it happened.",
            "derived":  "A row count times a fixed per-unit guess — directionally "
                        "useful, wrong in detail.",
            "plan":     "A published subscription price or included quota.",
            "estimate": "A judgement. Verify on the provider's dashboard before acting.",
        },
    }
