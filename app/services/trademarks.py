"""
USPTO Trademark Center service.

Queries the undocumented Elasticsearch-backed API used by
https://tmsearch.uspto.gov to fetch recent consumer trademark filings.
No API key required — this is the same endpoint the public search UI uses.
"""
import re
import time
import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

USPTO_SEARCH_URL = "https://tmsearch.uspto.gov/prod-stage-v1-0-0/tmsearch"
REQUEST_TIMEOUT = 20  # seconds

# ── Consumer IC class → category mapping ──────────────────────────────────────
IC_CATEGORY_MAP = {
    "IC 003": "Beauty",           # Cosmetics, skincare, haircare, toiletries
    "IC 005": "Health/Wellness",  # Supplements, pharma, nutraceuticals, medical
    "IC 009": "Consumer AI",      # Electronics, wearables, consumer tech, software
    "IC 014": "Apparel",          # Jewelry, watches, precious metals
    "IC 016": "Home/Lifestyle",   # Paper goods, stationery, notebooks, prints
    "IC 018": "Apparel",          # Leather goods, bags, handbags, backpacks
    "IC 020": "Home/Lifestyle",   # Furniture, mirrors, picture frames
    "IC 021": "Home/Lifestyle",   # Kitchen tools, cookware, housewares, glassware
    "IC 024": "Home/Lifestyle",   # Textiles, fabric goods, bed/bath linens
    "IC 025": "Apparel",          # Clothing, footwear, headwear
    "IC 028": "Sports",           # Toys, games, sporting goods
    "IC 029": "CPG/Food/Drink",   # Meat, dairy, preserved / processed foods
    "IC 030": "CPG/Food/Drink",   # Coffee, tea, bakery, confectionery
    "IC 031": "CPG/Food/Drink",   # Fresh fruit, vegetables, live animals
    "IC 032": "CPG/Food/Drink",   # Beer, soft drinks, mineral water
    "IC 033": "CPG/Food/Drink",   # Wine, spirits, liqueurs
    "IC 035": "Home/Lifestyle",   # Retail / e-commerce services
    "IC 041": "Education",        # Education, entertainment, fitness training
    "IC 043": "CPG/Food/Drink",   # Food/beverage services, restaurants, cafes
    "IC 044": "Health/Wellness",  # Medical, beauty, spa, veterinary
}

CONSUMER_CLASSES = list(IC_CATEGORY_MAP.keys())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _infer_category(ic_classes: list) -> str:
    """Return the best consumer category for a list of IC class strings."""
    for ic in ic_classes:
        if ic in IC_CATEGORY_MAP:
            return IC_CATEGORY_MAP[ic]
    return "Other"


# Owner-name terms that mark an institutional / non-brand filer. The consumer-IC
# query still returns tons of trademarks owned by law firms, holding companies,
# banks, universities, etc. We reject those BEFORE enrichment so the bounded
# per-run signal budget is spent on plausible venture-track brands. Do NOT list
# LLC/Inc/Corp/Co — those are exactly how a new brand entity files.
_NON_BRAND_OWNER = {
    "holdings", "holding", "capital", "ventures", "venture", "partners",
    "l.p.", " lp", "trust", "properties", "property", "realty", "real estate",
    "bank", "bancorp", "insurance", "university", "college", "hospital",
    "health system", "church", "ministries", "diocese", "law", "attorneys",
    "attorney", "llp", "pllc", "consulting", "advisors", "advisory",
    "management", "staffing", "logistics", "associates", "foundation",
}


def _is_brand_candidate(owner: str, wordmark: str) -> bool:
    """Reject obvious institutional / non-brand trademark owners."""
    if not wordmark or not wordmark.strip():
        return False
    o = f" {(owner or '').lower()} "
    # Strip entity suffixes so 'Capital' in 'Capital Foods LLC' still counts but
    # the LLC/Inc themselves never trigger a reject.
    o = re.sub(r"\b(llc|inc|corp|co|ltd|company|corporation)\b\.?", " ", o)
    return not any(term in o for term in _NON_BRAND_OWNER)


# ── Design descriptions masquerading as wordmarks ─────────────────────────────
# USPTO sometimes fills the wordmark field with the DESIGN DESCRIPTION rather
# than the mark itself:
#   THE MARK CONSISTS OF THE WORDING "SHELL SHOK" ABOVE A CRACKED EGG...
# The brand is right there in quotes, but the record reaches the team looking
# like clerical noise.
#
# Found in calibration 2026-08-03: Brent rejected a brand the model scored 72 —
# thesis "the founder-voiced Southern-identity seasoning brand that McCormick
# can never be, a Fly by Jing moment for the American South" — because the NAME
# on the card was sixty characters of trademark-office boilerplate. A
# presentation failure read as a judgement, and a real HOT lead was lost to it.
_DESIGN_DESC_RE = re.compile(r"^\s*THE MARK CONSISTS OF\b", re.I)
_QUOTED_MARK_RE = re.compile(u"[\"'\u201c\u2018]([^\"'\u201d\u2019]{2,60})[\"'\u201d\u2019]")


def _clean_wordmark(raw):
    """Recover the actual mark when USPTO gave us a design description."""
    if not raw or not _DESIGN_DESC_RE.match(raw):
        return raw
    for m in _QUOTED_MARK_RE.finditer(raw):
        candidate = m.group(1).strip(" ,.;:-")
        # Skip single letters and stray punctuation captured from phrases like
        # 'THE LARGE STYLIZED SERIF LETTERS "E", "B"'.
        if len(candidate) >= 2 and any(c.isalpha() for c in candidate):
            return candidate
    return raw


def _owner_is_individual(raw: str) -> bool:
    """
    True when USPTO annotates this owner as a natural person.

    USPTO appends an entity-type annotation to every ownerName —
    'Raymond A Jimenez (INDIVIDUAL; USA)' vs
    'Acme Labs LLC (LIMITED LIABILITY COMPANY; Delaware, USA)' — and roughly
    half of consumer-class filings are individuals.

    This matters for confluence: the owner is fed into person-key clustering,
    and normalize_person only rejects names carrying a legal suffix. A company
    named "Sunset Beverage" therefore normalises to the person key
    'sunset beverage', so two unrelated brands filed by similarly-named
    entities would merge into one cluster and fire a false multi-signal alert.
    A wrong merge is worse than a missed link, so only owners USPTO calls
    INDIVIDUAL are offered as people; everything else is left out.
    """
    if not raw:
        return False
    match = re.search(r"\(([^)]*)\)\s*$", raw)
    if not match:
        return False
    return "INDIVIDUAL" in match.group(1).upper()


def _clean_owner(raw: str) -> str:
    """
    Strip the entity-type / jurisdiction annotation USPTO appends.
    'Acme Labs LLC (LIMITED LIABILITY COMPANY; Delaware, USA)' → 'Acme Labs LLC'
    """
    if not raw:
        return "Unknown"
    cleaned = re.sub(r"\s*\([^)]+\)\s*$", "", raw).strip()
    return cleaned or raw


def _gs_snippet(goods_services: list) -> str:
    """Return a short human-readable snippet of the goods/services."""
    if not goods_services:
        return ""
    first = goods_services[0]
    # Remove the "IC XXX: " prefix and truncate at first semicolon
    first = re.sub(r"^IC \d{3}:\s*", "", first)
    return first.split(";")[0].strip()[:100]


def _fmt_ts(dt: datetime) -> str:
    """Format a datetime the way this index stores its date fields."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# ── Measured volumes and the bounds they justify ──────────────────────────────
# Live probe of this endpoint, 2026-07-26. Consumer-IC-class filings actually
# present in the index, counted per filedDate:
#     weekdays 1,659-1,873/day   weekends 407-525/day
#     → a 7-day window holds ~9,450 and a 30-day window ~40,900.
# ~92% of those clear the owner/category filters above, so ONE day yields
# roughly 1,050 genuine brand candidates and a full 7-day sweep ~8,700.

PAGE_SIZE = 100

# The endpoint enforces Elasticsearch's default result window: from + size must
# be <= 10,000 or it answers HTTP 400 "Result window is too large". Verified
# live 2026-07-26 — from=9,900 succeeds, from=10,000 fails. A SINGLE query
# therefore cannot reach past 10,000 hits, which is far less than a 30-day
# window holds. That is why the sweep is sliced one filedDate at a time
# (_iter_day_slices): each slice holds <= ~1,900 hits and is fully reachable.
ES_MAX_RESULT_WINDOW = 10000

# How many raw filings one run may page through. Bounded on purpose — never
# unbounded. 25,000 covers ~13 full days, so a normal daily run can sweep its
# whole window and a post-outage catch-up still recovers most of a long one.
MAX_FILINGS_INSPECTED = 25000

# How many SIGNALS one run may return — the downstream triage/enrichment budget.
#
# Sized to cover the WHOLE requested window, not one day. The scan runs a 7-day
# window and a day holds ~1,050 genuine consumer candidates (measured live
# 2026-07-26), so ~7,350 per run, and this carries ~20% margin over that.
#
# It has to cover the window because the sweep runs filedDate DESC and stops on
# this bound: a budget that truncates mid-window leaves days unswept, which now
# correctly WITHHOLDS the watermark (see search_recent_trademarks) — and a
# watermark that never advances leaves the late-arrival pass permanently inert.
# A too-small budget therefore disables the very mechanism that closes the
# permanent-miss hole, on every single run.
#
# Dedup is by fingerprint downstream, so re-returning already-saved filings is
# free; only genuinely new signals reach triage. Steady state is ~1,050 new/day.
DEFAULT_SIGNAL_BUDGET = 9000

# ── Late indexing — the permanent-miss problem ────────────────────────────────
# Every record carries `recordLoadDate`, the moment USPTO loaded it into this
# search index. Sampling filedDate → recordLoadDate lag across three weeks of
# filedDates (live, 2026-07-26) shows the index is NOT filled in filing order:
#     ~94% of records land 1 day after filing, ~5% at 2-4 days, then a long tail
#     filedDate 07-19 → lags to  7d      filedDate 07-11 → lags to 15d
#     filedDate 07-14 → lags to 11d      filedDate 07-07 → lags to 18d
#     filedDate 07-09 → lags to 16d      filedDate 07-06 → lags to 19d
# Roughly 2-2.5% of filings land more than 7 days after they were filed. That
# tail keeps growing as a cohort ages and is RIGHT-CENSORED — a filing not yet
# loaded cannot be observed — so 19 days is a floor on the true tail, not a cap.
#
# Why this is permanent rather than merely late: the sweep is a filedDate window
# sorted DESC. A filing loaded N days after filing is simply absent from every
# run until it lands, and if N exceeds the window it lands BEHIND the horizon,
# where no later run ever looks again. At ~1,050 candidates/day, the >7-day tail
# alone is ~25 brands/day silently lost forever.
#
# The fix: remember when we last swept (a watermark), and each run additionally
# asks for everything LOADED since then whose filedDate already fell out of the
# fresh window. Dedup is by fingerprint downstream, so overlap costs nothing.
LATE_ARRIVAL_FLOOR_DAYS = 45   # comfortably past the observed (censored) 19d tail
WATERMARK_OVERLAP_DAYS = 2     # deliberate re-sweep margin: clock skew, part-runs

# The late pass runs FIRST (its records are the only irrecoverable ones) so it
# must never be able to starve the fresh window. Measured live 2026-07-26, the
# late window holds ~550 records — 63% of them status 630, "record initialized,
# not assigned to examiner", i.e. genuine first-time arrivals; the rest are
# older filings whose recordLoadDate was bumped by a later examiner event and
# which dedup drops downstream. Half of a 2,000 budget is ample headroom for
# that volume while making starvation structurally impossible.
LATE_ARRIVAL_BUDGET_SHARE = 0.5

# Transient-failure handling. Deep paging means 25+ requests per run instead of
# 2, and this endpoint throttles: a live probe drew HTTP 429 after ~90 rapid
# requests. Retry only on throttle/transient-upstream codes, and only then do we
# sleep — a healthy run never pays the delay.
_RETRY_STATUSES = (429, 502, 503, 504)
_MAX_PAGE_RETRIES = 3

# Deliberate pacing between page requests. A live probe drew HTTP 429 after ~90
# rapid requests, and a full 7-day sweep now issues far more than that — so
# without a pace the deep sweep would spend its run fighting the throttle it
# provoked, and the post-outage catch-up run (the one where coverage matters
# most) is both the longest and the most likely to abort. Retry-with-backoff is
# recovery; this is avoidance. ~7 req/s costs well under a minute across a full
# sweep and keeps us a courteous client of a free public endpoint.
_INTER_REQUEST_PAUSE_SECONDS = 0.15
_RETRY_BACKOFF_SECONDS = 2.0


# ── Main service function ─────────────────────────────────────────────────────

def _es_query(start_dt: datetime, end_dt: datetime, size: int, frm: int,
              loaded_since: datetime = None) -> dict:
    """Build the consumer-class Form/trademark ES query for one page.

    Sorted by filedDate DESC so paging is deterministic and returns the FRESHEST
    filings first (the pre-2026-07 query had no sort → the capped page was
    relevance-ranked, not recent).

    `loaded_since` adds a floor on recordLoadDate, which selects records the
    index gained since our last sweep regardless of how old their filedDate is.
    That is the late-arrival pass; leave it None for a plain filedDate sweep.
    """
    must = [
        {"range": {"filedDate": {"gte": _fmt_ts(start_dt),
                                 "lte": _fmt_ts(end_dt)}}}
    ]
    if loaded_since is not None:
        must.append({"range": {"recordLoadDate": {"gte": _fmt_ts(loaded_since)}}})

    return {
        "query": {
            "bool": {
                "must": must,
                "filter": [{"term": {"alive": True}}],
                # At least one consumer IC class must appear in goodsAndServices
                "should": [
                    {"match_phrase": {"goodsAndServices": ic}}
                    for ic in CONSUMER_CLASSES
                ],
                "minimum_should_match": 1,
            }
        },
        "sort": [{"filedDate": {"order": "desc"}}],
        "size": size,
        "from": frm,
        "track_total_hits": True,
        "_source": [
            "filedDate", "wordmark", "ownerName",
            "goodsAndServices", "internationalClass", "registrationId",
            "currentBasis",   # list: ['1b'] = intent-to-use (pre-launch), ['1a'] = in commerce
            "recordLoadDate", # when USPTO loaded this record — drives the watermark
        ],
    }


def _iter_day_slices(start_dt: datetime, end_dt: datetime):
    """
    Yield (slice_start, slice_end) one calendar day at a time, NEWEST first.

    Day slicing is what makes a deep sweep possible at all: the endpoint refuses
    from + size > 10,000 (ES_MAX_RESULT_WINDOW), so a multi-day range large
    enough to matter cannot be paged to the end. One day holds ~1,900 filings at
    most, which is fully reachable, and newest-first preserves the previous
    freshest-filings-first ordering of the returned signals.
    """
    day = end_dt.date()
    first = start_dt.date()
    while day >= first:
        yield (datetime(day.year, day.month, day.day, 0, 0, 0),
               datetime(day.year, day.month, day.day, 23, 59, 59))
        day -= timedelta(days=1)


def _post_page(body: dict, headers: dict):
    """
    POST one page, retrying only on throttling / transient upstream failures.

    Raises the underlying requests exception when retries are exhausted so the
    caller keeps the existing "record the error and stop sweeping" behaviour.
    """
    last_exc = None
    for attempt in range(_MAX_PAGE_RETRIES):
        try:
            resp = requests.post(USPTO_SEARCH_URL, json=body, headers=headers,
                                 timeout=REQUEST_TIMEOUT)
            if resp.status_code in _RETRY_STATUSES and attempt < _MAX_PAGE_RETRIES - 1:
                delay = _RETRY_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    "USPTO returned %s — backing off %.1fs (attempt %d/%d)",
                    resp.status_code, delay, attempt + 1, _MAX_PAGE_RETRIES,
                )
                time.sleep(delay)
                continue
            resp.raise_for_status()
            payload = resp.json()
            time.sleep(_INTER_REQUEST_PAUSE_SECONDS)
            return payload
        except requests.RequestException as exc:
            last_exc = exc
            # A hard 4xx (a malformed or out-of-window query) will fail exactly
            # the same way three times — fail fast rather than sleeping 6s on it.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status and status not in _RETRY_STATUSES and 400 <= status < 500:
                raise
            if attempt >= _MAX_PAGE_RETRIES - 1:
                raise
            time.sleep(_RETRY_BACKOFF_SECONDS * (2 ** attempt))
    raise last_exc


def _harvest(raw_hits: list, state: dict, max_results: int) -> bool:
    """
    Turn one page of raw hits into signals, in place on `state`.

    Ordering is load-bearing: design-only marks, non-consumer categories and
    institutional owners are all rejected BEFORE a slot of the `max_results`
    budget is spent, so the budget buys plausible venture-track brands rather
    than whatever happened to be freshest.

    Returns True once the signal budget is full.
    """
    # Guard on ENTRY, not only after appending: the late-arrival pass can fill
    # the budget before the fresh pass starts, and without this the first
    # qualifying hit of the next pass would push the run one signal over.
    if len(state["signals"]) >= max_results:
        return True

    for hit in raw_hits:
        state["inspected"] += 1
        src = hit.get("source", {})
        wordmark = _clean_wordmark(src.get("wordmark"))

        # Skip design-only marks (no text wordmark)
        if not wordmark:
            continue

        key = wordmark.strip().lower()
        if key in state["seen"]:
            continue

        ic_classes = src.get("internationalClass", [])
        category = _infer_category(ic_classes)
        # Skip any non-consumer trademarks that slipped through the filter
        if category == "Other":
            continue

        owners = src.get("ownerName", [])
        _raw_owner = owners[0] if owners else ""
        owner = _clean_owner(_raw_owner) if owners else "Unknown"
        owner_is_person = _owner_is_individual(_raw_owner)
        # Reject institutional / non-brand owners before spending enrich budget
        if not _is_brand_candidate(owner, wordmark):
            continue

        state["seen"].add(key)

        filed_date = src.get("filedDate", "")
        filed_label = filed_date[:10] if filed_date else "unknown date"
        primary_class = next(
            (ic for ic in ic_classes if ic in IC_CATEGORY_MAP),
            ic_classes[0] if ic_classes else ""
        )
        snippet = _gs_snippet(src.get("goodsAndServices", []))
        search_url = (
            f"https://tmsearch.uspto.gov/search/search-results"
            f"?searchInput={requests.utils.quote(wordmark)}&dateOption=custom"
        )

        # currentBasis: ['1b'] = intent-to-use (pre-launch), ['1a'] = already in commerce
        basis_raw = src.get("currentBasis") or []
        is_itu = any(str(b).lower().strip() == "1b" for b in basis_raw)
        score_boost = 18 if is_itu else 14
        basis_note = " · pre-launch intent-to-use" if is_itu else ""
        desc = f"{wordmark} — {primary_class} — Filed {filed_label}{basis_note}"

        # `owner` is emitted as a first-class field AND left embedded in
        # `notes`. Both are load-bearing and must stay in sync:
        #   • the field feeds person-key confluence (scheduler.py and
        #     scans/routes.py build person keys from meta["owner"]) and lets
        #     enrichment.py receive the owner without re-parsing prose;
        #   • the notes prefix format "Owner: NAME. <goods>" is what
        #     enrichment.py strips by exact match, and is the shape already
        #     persisted on stored rows — do not change it.
        state["signals"].append({
            "companyName":       wordmark,
            "signal_type":       "trademark",
            "category":          category,
            "score_boost":       score_boost,
            "is_intent_to_use":  is_itu,
            "description":       desc,
            "url":               search_url,
            "owner":             owner,
            "owner_is_person":   owner_is_person,
            "notes":             f"Owner: {owner}. {snippet}".strip(". "),
            "timestamp":         filed_date or state["now"].isoformat(),
        })

        if len(state["signals"]) >= max_results:
            return True

    return False


def _sweep_range(start_dt: datetime, end_dt: datetime, headers: dict, state: dict,
                 max_results: int, max_filings: int,
                 loaded_since: datetime = None) -> bool:
    """
    Page one date range until it is exhausted or a bound is hit.

    Returns True when the caller should stop sweeping entirely (budget full,
    inspection ceiling reached, or the upstream failed).
    """
    total_in_range = 0

    for page in range(-(-max_filings // PAGE_SIZE)):
        frm = page * PAGE_SIZE
        # Never issue a request the endpoint will reject outright.
        if frm + PAGE_SIZE > ES_MAX_RESULT_WINDOW:
            logger.warning(
                "USPTO sweep hit the %d result-window ceiling for %s — "
                "%d of %d filings in this slice were unreachable",
                ES_MAX_RESULT_WINDOW, start_dt.date(), total_in_range - frm, total_in_range,
            )
            break

        try:
            data = _post_page(
                _es_query(start_dt, end_dt, PAGE_SIZE, frm, loaded_since), headers,
            )
        except requests.RequestException as exc:
            if not state["any_success"]:
                state["fatal"] = str(exc)
            else:
                state["error"] = str(exc)
            return True

        state["any_success"] = True
        hits_obj = data.get("hits", {})
        if page == 0:
            total_in_range = hits_obj.get("totalValue", 0)
            state["total_found"] += total_in_range
        raw_hits = hits_obj.get("hits", [])
        if not raw_hits:
            break

        if _harvest(raw_hits, state, max_results):
            return True
        if state["inspected"] >= max_filings:
            return True
        if frm + PAGE_SIZE >= total_in_range:
            break

    return False


def search_recent_trademarks(
    days_back: int = 30,
    max_results: int = DEFAULT_SIGNAL_BUDGET,
    max_filings: int = MAX_FILINGS_INSPECTED,
    loaded_since: datetime = None,
) -> dict:
    """
    Query USPTO for consumer trademark filings in the last *days_back* days.

    Trademark is the app's longest-lead signal (6-28 months pre-launch) and the
    most under-sampled. Two independent losses used to throw most of it away:

    1. DEPTH. Paging was capped at 20 pages — at most 2,000 filings inspected
       against the ~9,450 a 7-day window holds, so a run saw only the freshest
       ~20% and every older filing in its own window went uninspected. The cap is
       now MAX_FILINGS_INSPECTED (still bounded, never unbounded) and the sweep
       is day-sliced, because the endpoint refuses from + size > 10,000 and a
       single range query therefore cannot be paged to the end at all.

    2. LATE ARRIVALS. USPTO does not index in filing order — see the
       LATE_ARRIVAL_FLOOR_DAYS block above for the measured lag tail. Because
       the sweep is a filedDate window sorted DESC, a filing indexed after its
       filedDate has left the window is invisible to every later run, forever.
       Pass `loaded_since` (the previous run's watermark) and this run also
       sweeps everything LOADED since then whose filedDate is older than the
       fresh window — bounded by LATE_ARRIVAL_FLOOR_DAYS.

    The late-arrival pass runs FIRST and deliberately so: those records are the
    only ones that cannot be recovered by a later run, whereas a fresh filing
    truncated by the budget is still sitting at the top of tomorrow's sweep.

    `max_results` is the returned-signal budget; `max_filings` is how deep we
    page. Institutional/non-brand owners are rejected before either is spent.

    Returns:
        {"signals": [...], "total_found": int, "fetched": int, "error": str|None,
         "inspected": int,       # raw filings paged through
         "late_arrivals": int,   # signals recovered by the watermark pass
         "swept_at": str}        # watermark to persist for the next run
    """
    now = datetime.utcnow()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://tmsearch.uspto.gov",
        "Referer": "https://tmsearch.uspto.gov/search/search-results",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    state = {
        "signals": [], "seen": set(), "inspected": 0, "total_found": 0,
        "error": None, "fatal": None, "any_success": False, "now": now,
    }

    # ── Range total: one cheap probe, so total_found keeps its documented meaning ──
    # The sweep pages day-by-day and stops when a bound is hit, so accumulating
    # per-slice totals under-reports by up to the number of days never queried —
    # a 21-day window reported 446 against a true ~20,000. That field is part of
    # the scan API response and the UI shows it, so it must mean "matches in the
    # requested range", not "matches in the days we happened to reach".
    try:
        probe = _post_page(_es_query(now - timedelta(days=days_back), now, 1, 0), headers)
        state["range_total"] = (probe.get("hits") or {}).get("totalValue") or 0
    except requests.RequestException as exc:
        # Non-fatal: the sweep itself is what matters. Fall back to the summed
        # per-slice totals rather than failing the run over a reporting field.
        logger.warning("USPTO range-total probe failed (%s) — reporting a floor", exc)
        state["range_total"] = None

    # ── Pass 1: late arrivals (only when we have a watermark to work from) ────
    late_arrivals = 0
    late_truncated = False
    if loaded_since is not None:
        # Re-ask from slightly before the watermark; dedup is by fingerprint
        # downstream, so overlapping is free and closes boundary/clock gaps.
        since = loaded_since - timedelta(days=WATERMARK_OVERLAP_DAYS)
        late_end = now - timedelta(days=days_back)
        late_start = now - timedelta(days=LATE_ARRIVAL_FLOOR_DAYS)
        if late_start < late_end:
            before = state["total_found"]
            late_budget = max(1, int(max_results * LATE_ARRIVAL_BUDGET_SHARE))
            _sweep_range(late_start, late_end, headers, state,
                         late_budget, max_filings, loaded_since=since)
            late_arrivals = len(state["signals"])
            # The late window sits OUTSIDE the requested days_back range, so it
            # must not inflate total_found — that field means "matches in the
            # requested date range" and the manual scan route reports it as such.
            state["total_found"] = before
            if state["fatal"] is not None:
                # This pass is SUPPLEMENTARY. Letting its failure abort the run
                # would trade a partial loss for a total one — the fresh window
                # is the bulk of the value and may well be fine. Demote to a
                # recorded error (which also withholds the watermark) and go on.
                logger.warning("USPTO late-arrival pass failed: %s", state["fatal"])
                state["error"] = state["fatal"]
                state["fatal"] = None
            if late_arrivals:
                logger.info(
                    "USPTO late-arrival pass recovered %d filings indexed since %s "
                    "whose filedDate had already left the %d-day window",
                    late_arrivals, since.date(), days_back,
                )
            # Truncated by its own cap means there is late ground we did NOT
            # reach. The pass runs filedDate DESC, so what it drops is the
            # OLDEST — precisely the longest-lag records this whole mechanism
            # exists to save. Advancing the watermark past them would discard
            # them forever, which is the exact bug being fixed here, so the
            # watermark is withheld and the next run re-sweeps the same ground.
            if late_arrivals >= late_budget:
                late_truncated = True
                logger.warning(
                    "USPTO late-arrival pass hit its %d-signal cap — holding the "
                    "watermark so the unswept remainder is retried next run",
                    late_budget,
                )

    # ── Pass 2: the fresh window, one calendar day at a time, newest first ────
    fresh_truncated = False
    if state["fatal"] is None:
        start_dt = now - timedelta(days=days_back)
        for slice_start, slice_end in _iter_day_slices(start_dt, now):
            if _sweep_range(slice_start, slice_end, headers, state,
                            max_results, max_filings):
                # Stopped on a bound rather than running the window dry, so
                # there are days in the requested range we never reached. Same
                # rule as the late pass: a run that did not sweep everything it
                # promised must NOT advance the watermark, or the unswept
                # remainder becomes the permanent miss this mechanism exists to
                # prevent. Withholding costs nothing — dedup makes the repeat
                # sweep free.
                fresh_truncated = True
                logger.warning(
                    "USPTO fresh sweep stopped on a bound with %d signals — "
                    "holding the watermark so the unswept days are retried",
                    len(state["signals"]),
                )
                break

    if state["fatal"] is not None:
        return {"signals": [], "total_found": 0, "fetched": 0, "inspected": 0,
                "late_arrivals": 0, "truncated": True, "swept_at": None,
                "error": state["fatal"]}

    return {
        "signals":       state["signals"],
        "total_found":   state["range_total"] if state.get("range_total") is not None
                         else state["total_found"],
        "fetched":       len(state["signals"]),
        "inspected":     state["inspected"],
        "late_arrivals": late_arrivals,
        # Persist this only on a run that actually swept everything it promised.
        # A run that failed part-way, or whose late pass was cut short by its
        # cap, has NOT seen everything loaded before `now`; advancing the
        # watermark past those records would recreate the permanent miss we
        # just closed. Withholding it is always safe — dedup makes the repeat
        # sweep free.
        "truncated":     bool(late_truncated or fresh_truncated),
        "swept_at":      None if (state["error"] or late_truncated or fresh_truncated)
                         else now.isoformat(),
        "error":         state["error"],
    }
