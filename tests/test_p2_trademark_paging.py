"""
P2 tests for the trademark sweep: depth, budget, filter-before-budget ordering,
and the late-arrival watermark.

Two independent losses are covered here, both measured live against USPTO on
2026-07-26 before the fix:

1. DEPTH. Paging was capped at `min(20, ...)` pages = 2,000 filings inspected,
   against the ~9,450 consumer-IC-class filings a 7-day window actually holds.
   A run therefore saw only the freshest ~20% of its own window.

2. LATE ARRIVALS. Each record carries `recordLoadDate` (when USPTO loaded it
   into the search index). Sampling filedDate → recordLoadDate lag showed ~94%
   of filings land 1 day after filing but the tail runs long: filedDate 07-19
   showed lags to 7d, 07-14 to 11d, 07-11 to 15d, 07-07 to 18d, 07-06 to 19d,
   and the tail is right-censored so 19d is a floor not a cap. Because the sweep
   is a filedDate window sorted DESC, anything indexed after its filedDate left
   the window was invisible to every later run — permanently.

All HTTP is faked; the suite forbids live network.
"""
import requests
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.services import trademarks as tm


@pytest.fixture(autouse=True)
def _no_request_pacing(monkeypatch):
    """The live sweep paces itself to stay under USPTO's throttle; tests must not pay it."""
    monkeypatch.setattr(tm, "_INTER_REQUEST_PAUSE_SECONDS", 0)


# ── Fake HTTP plumbing ────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


def _hit(wordmark, owner="Graza LLC", ic=("IC 030",), filed="2026-07-20T00:00:00",
         goods="IC 030: Coffee beans; roasted coffee", loaded=None):
    src = {
        "wordmark":           wordmark,
        "ownerName":          [owner],
        "goodsAndServices":   [goods],
        "internationalClass": list(ic),
        "filedDate":          filed,
        "currentBasis":       ["1b"],
    }
    if loaded:
        src["recordLoadDate"] = loaded
    return {"source": src}


class _FakeUSPTO:
    """
    Records every query it is asked and answers from a per-day corpus.

    Mirrors the real endpoint closely enough for paging tests: it honours
    from/size, reports a real totalValue, and rejects from + size > 10,000 the
    way the live index does.
    """

    def __init__(self, corpus, enforce_window=True):
        # corpus: {"YYYY-MM-DD": [hit, ...]} keyed by filedDate day
        self.corpus = corpus
        self.enforce_window = enforce_window
        self.queries = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        body = json
        self.queries.append(body)
        rng = body["query"]["bool"]["must"][0]["range"]["filedDate"]
        frm, size = body["from"], body["size"]

        if self.enforce_window and frm + size > tm.ES_MAX_RESULT_WINDOW:
            return _FakeResponse(400, {"error": "Result window is too large"})

        loaded_floor = None
        for clause in body["query"]["bool"]["must"][1:]:
            if "recordLoadDate" in clause.get("range", {}):
                loaded_floor = clause["range"]["recordLoadDate"]["gte"]

        pool = []
        for day, hits in sorted(self.corpus.items(), reverse=True):
            if not (rng["gte"][:10] <= day <= rng["lte"][:10]):
                continue
            for h in hits:
                if loaded_floor and (h["source"].get("recordLoadDate") or "") < loaded_floor:
                    continue
                pool.append(h)

        return _FakeResponse(200, {"hits": {"totalValue": len(pool),
                                            "hits": pool[frm:frm + size]}})


def _corpus(days, per_day, owner="Graza LLC", start_day=None, loaded=None):
    """A dense corpus: `per_day` uniquely-named filings on each of `days` days."""
    base = start_day or datetime.utcnow().date()
    out = {}
    for d in range(days):
        day = base - timedelta(days=d)
        key = day.isoformat()
        out[key] = [
            _hit(f"MARK{d}_{i}", owner=owner, filed=f"{key}T00:00:00",
                 loaded=loaded or f"{key}T09:00:00")
            for i in range(per_day)
        ]
    return out


# ── Depth: paging continues past the old 20-page ceiling ─────────────────────

def test_sweep_pages_far_past_the_old_20_page_ceiling():
    """
    The old cap was `min(20, ...)` pages = 2,000 filings. A 7-day window holds
    ~9,450 in production, so that cap left ~78% of the window uninspected.
    """
    fake = _FakeUSPTO(_corpus(days=7, per_day=1400))

    with patch("app.services.trademarks.requests.post", side_effect=fake):
        result = tm.search_recent_trademarks(days_back=7, max_results=100000,
                                             max_filings=25000)

    assert result["error"] is None
    # 7 days x 1,400 = 9,800 filings — all inspected, versus 2,000 before.
    assert result["inspected"] == 9800, result["inspected"]
    assert result["fetched"] == 9800
    assert len(fake.queries) > 20, "must issue far more than the old 20 pages"


def test_inspection_ceiling_is_bounded_not_unbounded():
    """Depth is raised, not removed — max_filings still stops the sweep."""
    fake = _FakeUSPTO(_corpus(days=30, per_day=1900))

    with patch("app.services.trademarks.requests.post", side_effect=fake):
        result = tm.search_recent_trademarks(days_back=30, max_results=100000,
                                             max_filings=3000)

    assert result["inspected"] <= 3000 + tm.PAGE_SIZE
    assert result["inspected"] >= 3000


def test_day_slicing_keeps_every_page_inside_the_result_window():
    """
    The endpoint rejects from + size > 10,000 (verified live: from=9,900 is fine,
    from=10,000 returns HTTP 400). A single 30-day range query therefore cannot
    be paged to the end at all, which is why the sweep is sliced per filedDate.
    """
    fake = _FakeUSPTO(_corpus(days=30, per_day=1900), enforce_window=True)

    with patch("app.services.trademarks.requests.post", side_effect=fake):
        result = tm.search_recent_trademarks(days_back=30, max_results=100000,
                                             max_filings=25000)

    assert result["error"] is None, "no page may exceed the ES result window"
    for q in fake.queries:
        assert q["from"] + q["size"] <= tm.ES_MAX_RESULT_WINDOW
    # And it really did go deeper than one query could ever reach.
    assert result["inspected"] > tm.ES_MAX_RESULT_WINDOW


def test_each_slice_covers_exactly_one_filed_date():
    fake = _FakeUSPTO(_corpus(days=5, per_day=10))

    with patch("app.services.trademarks.requests.post", side_effect=fake):
        tm.search_recent_trademarks(days_back=5, max_results=100000)

    # The size=1 range probe deliberately spans the WHOLE window — it exists so
    # total_found keeps meaning "matches in the requested range" rather than
    # "matches in the days the sweep happened to reach". It is not a slice.
    spans = {
        (q["query"]["bool"]["must"][0]["range"]["filedDate"]["gte"][:10],
         q["query"]["bool"]["must"][0]["range"]["filedDate"]["lte"][:10])
        for q in fake.queries if q.get("size") != 1
    }
    assert spans, "expected at least one sweep slice besides the range probe"
    for gte, lte in spans:
        assert gte == lte, f"slice {gte}..{lte} spans more than one day"


# ── Budget: max_results still bounds returned signals ────────────────────────

def test_max_results_still_bounds_returned_signals():
    fake = _FakeUSPTO(_corpus(days=7, per_day=1400))

    with patch("app.services.trademarks.requests.post", side_effect=fake):
        result = tm.search_recent_trademarks(days_back=7, max_results=250,
                                             max_filings=25000)

    assert result["fetched"] == 250
    assert len(result["signals"]) == 250


def test_budget_is_filled_freshest_first():
    """Day slices run newest-first, so a truncated budget keeps the newest."""
    today = datetime.utcnow().date()
    fake = _FakeUSPTO(_corpus(days=5, per_day=100, start_day=today))

    with patch("app.services.trademarks.requests.post", side_effect=fake):
        result = tm.search_recent_trademarks(days_back=5, max_results=100,
                                             max_filings=25000)

    stamps = {s["timestamp"][:10] for s in result["signals"]}
    assert stamps == {today.isoformat()}, stamps


# ── Filter-before-budget ordering ────────────────────────────────────────────

def test_institutional_owners_rejected_before_the_budget_is_spent():
    """
    The rejects must not consume budget slots. 300 institutional filings sit
    ahead of 5 real brands; a budget of 5 must return the 5 brands, not zero.
    """
    today = datetime.utcnow().date()
    day = today.isoformat()
    hits = [
        _hit(f"FUND{i}", owner="Blackstone Capital Partners LP",
             filed=f"{day}T00:00:00", loaded=f"{day}T09:00:00")
        for i in range(300)
    ] + [
        _hit(f"BRAND{i}", owner="Graza LLC", filed=f"{day}T00:00:00",
             loaded=f"{day}T09:00:00")
        for i in range(5)
    ]
    fake = _FakeUSPTO({day: hits})

    with patch("app.services.trademarks.requests.post", side_effect=fake):
        result = tm.search_recent_trademarks(days_back=1, max_results=5,
                                             max_filings=25000)

    assert result["fetched"] == 5
    assert all(s["companyName"].startswith("BRAND") for s in result["signals"])
    assert result["inspected"] >= 305, "rejects are inspected, just not banked"


def test_non_consumer_categories_rejected_before_the_budget():
    today = datetime.utcnow().date()
    day = today.isoformat()
    hits = [
        _hit(f"B2B{i}", owner="Graza LLC", ic=("IC 042",),
             goods="IC 042: SaaS platform", filed=f"{day}T00:00:00",
             loaded=f"{day}T09:00:00")
        for i in range(50)
    ] + [_hit("REALBRAND", owner="Graza LLC", filed=f"{day}T00:00:00",
              loaded=f"{day}T09:00:00")]
    fake = _FakeUSPTO({day: hits})

    with patch("app.services.trademarks.requests.post", side_effect=fake):
        result = tm.search_recent_trademarks(days_back=1, max_results=1,
                                             max_filings=25000)

    assert [s["companyName"] for s in result["signals"]] == ["REALBRAND"]


# ── Watermark / late-arrival behaviour ───────────────────────────────────────

def test_no_watermark_means_no_late_arrival_pass():
    """A first run has nothing to catch up on and must not query older ground."""
    fake = _FakeUSPTO(_corpus(days=3, per_day=5))

    with patch("app.services.trademarks.requests.post", side_effect=fake):
        result = tm.search_recent_trademarks(days_back=3, loaded_since=None)

    assert result["late_arrivals"] == 0
    for q in fake.queries:
        clauses = q["query"]["bool"]["must"]
        assert not any("recordLoadDate" in c.get("range", {}) for c in clauses)


def test_late_indexed_filing_outside_the_window_is_recovered():
    """
    The permanent-miss case. A filing filed 20 days ago is loaded into the index
    only today. A 7-day filedDate sweep can never see it — but the watermark
    pass asks for everything LOADED since the last run and does.
    """
    today = datetime.utcnow().date()
    old_day = (today - timedelta(days=20)).isoformat()

    corpus = _corpus(days=7, per_day=3)
    corpus[old_day] = [
        _hit("LATEBRAND", owner="Graza LLC", filed=f"{old_day}T00:00:00",
             loaded=f"{today.isoformat()}T04:00:00")
    ]
    fake = _FakeUSPTO(corpus)

    # Without a watermark: invisible.
    with patch("app.services.trademarks.requests.post", side_effect=fake):
        plain = tm.search_recent_trademarks(days_back=7, loaded_since=None)
    assert "LATEBRAND" not in {s["companyName"] for s in plain["signals"]}

    # With one: recovered.
    fake2 = _FakeUSPTO(corpus)
    with patch("app.services.trademarks.requests.post", side_effect=fake2):
        caught = tm.search_recent_trademarks(
            days_back=7,
            loaded_since=datetime.utcnow() - timedelta(days=1),
        )

    assert "LATEBRAND" in {s["companyName"] for s in caught["signals"]}
    assert caught["late_arrivals"] == 1


def test_late_pass_applies_the_overlap_margin_and_the_floor():
    fake = _FakeUSPTO(_corpus(days=7, per_day=2))
    watermark = datetime.utcnow() - timedelta(days=1)

    with patch("app.services.trademarks.requests.post", side_effect=fake):
        tm.search_recent_trademarks(days_back=7, loaded_since=watermark)

    late = [q for q in fake.queries
            if any("recordLoadDate" in c.get("range", {})
                   for c in q["query"]["bool"]["must"])]
    assert late, "a watermark must produce a late-arrival query"

    floor = late[0]["query"]["bool"]["must"][1]["range"]["recordLoadDate"]["gte"]
    expected = (watermark - timedelta(days=tm.WATERMARK_OVERLAP_DAYS))
    assert floor[:10] == expected.strftime("%Y-%m-%d"), floor

    # It searches older ground than the fresh window, bounded by the floor.
    span = late[0]["query"]["bool"]["must"][0]["range"]["filedDate"]
    oldest = (datetime.utcnow() - timedelta(days=tm.LATE_ARRIVAL_FLOOR_DAYS))
    assert span["gte"][:10] == oldest.strftime("%Y-%m-%d")
    assert span["lte"][:10] == (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")


def test_late_arrivals_do_not_inflate_total_found():
    """total_found means 'matches in the requested date range' — the manual scan
    route reports it to the UI as exactly that."""
    today = datetime.utcnow().date()
    old_day = (today - timedelta(days=20)).isoformat()
    corpus = _corpus(days=7, per_day=3)
    corpus[old_day] = [_hit("LATEBRAND", filed=f"{old_day}T00:00:00",
                            loaded=f"{today.isoformat()}T04:00:00")]

    fake = _FakeUSPTO(corpus)
    with patch("app.services.trademarks.requests.post", side_effect=fake):
        result = tm.search_recent_trademarks(
            days_back=7, loaded_since=datetime.utcnow() - timedelta(days=1))

    assert result["total_found"] == 7 * 3, result["total_found"]


def test_watermark_is_returned_on_a_clean_run_and_withheld_on_a_failed_one():
    fake = _FakeUSPTO(_corpus(days=3, per_day=2))
    with patch("app.services.trademarks.requests.post", side_effect=fake):
        ok = tm.search_recent_trademarks(days_back=3)
    assert ok["swept_at"] and ok["error"] is None
    datetime.fromisoformat(ok["swept_at"])   # parseable

    # First slice succeeds, a later one fails → partial run, watermark withheld.
    good = _FakeUSPTO(_corpus(days=3, per_day=2))
    calls = {"n": 0}

    def flaky(url, json=None, headers=None, timeout=None):
        # Let the size=1 range probe through untouched — it is not a sweep page,
        # and failing it would only degrade the reported total. The fault is
        # injected after the FIRST real page so there are gathered signals to
        # assert on.
        if json.get("size") == 1:
            return good(url, json=json, headers=headers, timeout=timeout)
        calls["n"] += 1
        if calls["n"] > 1:
            raise requests.ConnectionError("upstream blip")
        return good(url, json=json, headers=headers, timeout=timeout)

    with patch("app.services.trademarks.requests.post", side_effect=flaky), \
         patch("app.services.trademarks.time.sleep"):
        partial = tm.search_recent_trademarks(days_back=3)

    assert partial["error"] is not None
    assert partial["swept_at"] is None, "a partial sweep must not advance the watermark"
    assert partial["signals"], "signals gathered before the failure are still kept"


def test_total_upstream_failure_still_returns_the_legacy_error_shape():
    def dead(url, json=None, headers=None, timeout=None):
        raise requests.ConnectionError("dns failure")

    with patch("app.services.trademarks.requests.post", side_effect=dead), \
         patch("app.services.trademarks.time.sleep"):
        result = tm.search_recent_trademarks(days_back=7)

    assert result["signals"] == []
    assert result["fetched"] == 0 and result["total_found"] == 0
    assert result["swept_at"] is None
    assert "dns failure" in result["error"]


def test_throttling_is_retried_rather_than_aborting_the_sweep():
    """Deep paging means many more requests; a 429 must not end the run."""
    fake = _FakeUSPTO(_corpus(days=2, per_day=5))
    calls = {"n": 0}

    def throttled_once(url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(429, {})
        return fake(url, json=json, headers=headers, timeout=timeout)

    with patch("app.services.trademarks.requests.post", side_effect=throttled_once), \
         patch("app.services.trademarks.time.sleep") as slept:
        result = tm.search_recent_trademarks(days_back=2)

    assert result["error"] is None
    assert result["fetched"] == 10
    assert slept.called, "a 429 must back off before retrying"


def test_late_pass_cannot_starve_the_fresh_window():
    """
    The late pass runs first, so an unusually large late window could otherwise
    consume the whole budget and leave yesterday's filings uncollected. It is
    capped at LATE_ARRIVAL_BUDGET_SHARE of the budget.
    """
    today = datetime.utcnow().date()
    old_day = (today - timedelta(days=20)).isoformat()
    corpus = _corpus(days=7, per_day=50)
    corpus[old_day] = [
        _hit(f"LATE{i}", filed=f"{old_day}T00:00:00",
             loaded=f"{today.isoformat()}T04:00:00")
        for i in range(500)          # far more late arrivals than the budget
    ]

    fake = _FakeUSPTO(corpus)
    with patch("app.services.trademarks.requests.post", side_effect=fake):
        result = tm.search_recent_trademarks(
            days_back=7, max_results=20,
            loaded_since=datetime.utcnow() - timedelta(days=1))

    assert result["late_arrivals"] == 10, "late pass must stop at half the budget"
    assert result["fetched"] == 20, "the fresh window must still get its half"
    fresh = result["signals"][result["late_arrivals"]:]
    assert fresh and all(not s["companyName"].startswith("LATE") for s in fresh)


def test_truncated_late_pass_withholds_the_watermark():
    """
    The late pass runs filedDate DESC, so a budget cut drops the OLDEST — the
    longest-lag records this mechanism exists to save. Advancing the watermark
    past them would lose them forever, recreating the very bug being fixed.
    """
    today = datetime.utcnow().date()
    old_day = (today - timedelta(days=20)).isoformat()
    corpus = _corpus(days=7, per_day=5)
    corpus[old_day] = [
        _hit(f"LATE{i}", filed=f"{old_day}T00:00:00",
             loaded=f"{today.isoformat()}T04:00:00")
        for i in range(500)
    ]

    fake = _FakeUSPTO(corpus)
    with patch("app.services.trademarks.requests.post", side_effect=fake):
        truncated = tm.search_recent_trademarks(
            days_back=7, max_results=20,
            loaded_since=datetime.utcnow() - timedelta(days=1))

    assert truncated["late_arrivals"] == 10          # hit the cap
    assert truncated["swept_at"] is None, "capped late pass must hold the watermark"
    assert truncated["error"] is None, "a capped pass is not an error"

    # A late pass that comfortably fits advances the watermark as normal.
    corpus[old_day] = corpus[old_day][:2]
    fake2 = _FakeUSPTO(corpus)
    with patch("app.services.trademarks.requests.post", side_effect=fake2):
        # Budget comfortably above the whole corpus, so this run really does
        # sweep everything it promised. With max_results=20 against a 35-signal
        # fresh window it would stop on the budget instead — leaving days
        # unswept, which must ALSO withhold the watermark.
        complete = tm.search_recent_trademarks(
            days_back=7, max_results=500,
            loaded_since=datetime.utcnow() - timedelta(days=1))

    assert complete["late_arrivals"] == 2
    assert complete["swept_at"] is not None


def test_budget_is_never_overrun_when_both_passes_run():
    """
    The budget guard has to run on ENTRY to each page, not only after appending.
    With the late pass filling its share first, a post-append-only check let the
    fresh pass bank one extra signal.
    """
    today = datetime.utcnow().date()
    old_day = (today - timedelta(days=20)).isoformat()
    corpus = _corpus(days=7, per_day=40)
    corpus[old_day] = [
        _hit(f"LATE{i}", filed=f"{old_day}T00:00:00",
             loaded=f"{today.isoformat()}T04:00:00")
        for i in range(40)
    ]

    for budget in (1, 2, 9, 10, 11, 50):
        fake = _FakeUSPTO(corpus)
        with patch("app.services.trademarks.requests.post", side_effect=fake):
            result = tm.search_recent_trademarks(
                days_back=7, max_results=budget,
                loaded_since=datetime.utcnow() - timedelta(days=1))
        assert result["fetched"] == budget, f"budget {budget} overrun/undershot"
        assert len(result["signals"]) == budget


def test_failing_late_pass_does_not_abort_the_fresh_sweep():
    """
    The late pass is supplementary. When it was allowed to set the fatal flag,
    one transient blip on it wiped out the whole trademark collection for the
    run — trading a partial loss for a total one.
    """
    fake = _FakeUSPTO(_corpus(days=3, per_day=4))
    calls = {"n": 0}

    def late_fails(url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        clauses = json["query"]["bool"]["must"]
        if any("recordLoadDate" in c.get("range", {}) for c in clauses):
            raise requests.ConnectionError("late pass blip")
        return fake(url, json=json, headers=headers, timeout=timeout)

    with patch("app.services.trademarks.requests.post", side_effect=late_fails), \
         patch("app.services.trademarks.time.sleep"):
        result = tm.search_recent_trademarks(
            days_back=3, loaded_since=datetime.utcnow() - timedelta(days=1))

    assert result["fetched"] == 12, "fresh window must still be swept"
    assert result["error"] is not None, "the degraded pass stays visible"
    assert result["swept_at"] is None, "a degraded run must not advance the watermark"


def test_hard_4xx_is_not_retried():
    """A malformed/out-of-window query fails identically every time."""
    def bad_request(url, json=None, headers=None, timeout=None):
        return _FakeResponse(400, {"error": "Result window is too large"})

    with patch("app.services.trademarks.requests.post", side_effect=bad_request), \
         patch("app.services.trademarks.time.sleep") as slept:
        result = tm.search_recent_trademarks(days_back=2)

    assert result["error"] is not None
    assert not slept.called, "a hard 400 must fail fast, not sleep through retries"


# ── Result-dict contract ─────────────────────────────────────────────────────

def test_result_dict_keeps_every_key_callers_read():
    fake = _FakeUSPTO(_corpus(days=2, per_day=2))
    with patch("app.services.trademarks.requests.post", side_effect=fake):
        result = tm.search_recent_trademarks(days_back=2)

    for key in ("signals", "total_found", "fetched", "inspected", "error"):
        assert key in result, f"caller-visible key {key} disappeared"
