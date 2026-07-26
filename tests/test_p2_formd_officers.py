"""
P2 tests — Form D officer extraction coverage (Job 2: founders).

Context: `_enrich_related_persons` used to walk `signals[:50]` serially with a
0.3s sleep between filings. A 7-day window yields ~110 consumer candidates, so
more than half of every run got NO officer extraction: no related_persons, no
filer_name, no conviction/alumni check, no offering figures. Production agreed
— only 42 of 516 Form D signals carried a filer_name.

The fix covers the whole run behind a bounded ceiling, fetching on a small
worker pool paced under SEC's published 10 req/s limit.

All HTTP is faked (the suite forbids live network) and the throttle is asserted
on its declared bound, never on wall clock.
"""
import threading
import time

import pytest
from unittest.mock import patch

from app.services import delaware as dw

# Captured before any fixture can swap it — the shipped, production pacer.
_SHIPPED_PACER = dw._SEC_PACER
# Captured before any test patches time.sleep, so concurrency tests can create
# real overlap even while the module's own sleeps are faked out.
_REAL_SLEEP = time.sleep


@pytest.fixture(autouse=True)
def _disable_sec_pacing(monkeypatch):
    """
    Swap the process-wide SEC pacer for a disabled one for the duration of each
    test.

    These tests assert on the throttle's DECLARED bound (rate, pool size),
    never on wall clock — see the _RateLimiter tests below, which exercise the
    real pacing arithmetic directly. Leaving the live pacer in place would make
    a 500-filing batch sleep for 100 real seconds, and — because the pacer
    hands out slots on a monotonic clock — would leave slots scheduled minutes
    into the future for whatever test file runs next.
    """
    monkeypatch.setattr(dw, "_SEC_PACER", dw._RateLimiter(0))


# ── Fixture Form D XML ────────────────────────────────────────────────────────

_XML = """<?xml version="1.0"?>
<edgarSubmission>
  <offeringData>
    <offeringSalesAmounts>
      <totalOfferingAmount>{total}</totalOfferingAmount>
      <totalAmountSold>{sold}</totalAmountSold>
      <minimumInvestmentAccepted>{minimum}</minimumInvestmentAccepted>
    </offeringSalesAmounts>
  </offeringData>
  <relatedPersonsList>
{people}  </relatedPersonsList>
</edgarSubmission>
"""

_PERSON = """    <relatedPersonInfo>
      <relatedPersonName>
        <firstName>{first}</firstName>
        <lastName>{last}</lastName>
      </relatedPersonName>
      <relatedPersonRelationshipList>
        <relationship>{rel}</relationship>
      </relatedPersonRelationshipList>
    </relatedPersonInfo>
"""


def _xml(people=(("Jane", "Founder", "Executive Officer"),),
         total=1_000_000, sold=250_000, minimum=25_000) -> bytes:
    blocks = "".join(_PERSON.format(first=f, last=l, rel=r) for f, l, r in people)
    return _XML.format(
        people=blocks, total=total, sold=sold, minimum=minimum
    ).encode()


def _signal(i: int, inc_state: str = "NY") -> dict:
    """One Form D signal shaped exactly like _extract_signal's output."""
    return {
        "companyName": f"Brand {i}",
        "signal_type": "delaware",
        "category":    "Food/Beverage",
        "score_boost": 12 if inc_state == "DE" else 8,
        "_adsh":       f"000{i:07d}-26-000001",
        "_cik":        f"000{i:07d}",
        "_name":       f"Brand {i} LLC",
        "_inc_state":  inc_state,
    }


# ── FIX 1: every signal in a batch gets officer extraction ───────────────────

def test_all_signals_in_a_realistic_batch_get_officers_not_just_50():
    """
    The regression this whole task exists for: a ~110-filing run must enrich
    every filing, not the arbitrary first 50 of EDGAR's result order.
    """
    signals = [_signal(i) for i in range(110)]
    fetched = []
    lock = threading.Lock()

    def _fake_fetch(adsh, cik):
        with lock:
            fetched.append(adsh)
        return _xml()

    with patch("app.services.delaware._fetch_form_d_xml_content",
               side_effect=_fake_fetch), \
         patch("app.services.delaware.time.sleep"):
        dw._enrich_related_persons(signals)

    assert len(fetched) == 110, "every filing must be fetched exactly once"
    assert len(set(fetched)) == 110, "no filing fetched twice"
    assert all("related_persons" in s for s in signals)
    assert all(s.get("filer_name") == "Jane Founder" for s in signals)


def test_default_ceiling_covers_a_realistic_run():
    """The shipped default must be big enough that a normal week is uncapped."""
    assert dw.FORMD_ENRICH_CEILING >= 110
    import inspect
    sig = inspect.signature(dw.search_recent_delaware_entities)
    assert sig.parameters["related_persons_limit"].default == dw.FORMD_ENRICH_CEILING
    assert inspect.signature(dw._enrich_related_persons) \
        .parameters["limit"].default == dw.FORMD_ENRICH_CEILING


def test_ceiling_is_hard_even_when_a_caller_asks_for_more():
    """Work stays bounded: a caller cannot request an unbounded fan-out."""
    signals = [_signal(i) for i in range(dw.FORMD_ENRICH_CEILING + 25)]
    calls = []
    lock = threading.Lock()

    def _fake_fetch(adsh, cik):
        with lock:
            calls.append(adsh)
        return None

    with patch("app.services.delaware._fetch_form_d_xml_content",
               side_effect=_fake_fetch), \
         patch("app.services.delaware.time.sleep"):
        dw._enrich_related_persons(signals, limit=10_000)

    assert len(calls) == dw.FORMD_ENRICH_CEILING


def test_signals_without_edgar_ids_do_not_consume_budget():
    """
    The old signals[:limit] slice spent budget on rows it then skipped for a
    missing adsh. Eligibility is now selected before the budget is applied.
    """
    signals = [
        {"companyName": "No IDs", "signal_type": "delaware"},
        {"companyName": "No CIK", "signal_type": "delaware", "_adsh": "x"},
        _signal(1),
        _signal(2),
    ]

    with patch("app.services.delaware._fetch_form_d_xml_content",
               return_value=_xml()) as mock_fetch, \
         patch("app.services.delaware.time.sleep"):
        dw._enrich_related_persons(signals, limit=2)

    assert mock_fetch.call_count == 2
    assert "related_persons" in signals[2]
    assert "related_persons" in signals[3]
    assert "related_persons" not in signals[0]


# ── FIX 1: the SEC rate limit is respected ───────────────────────────────────

def test_declared_throttle_stays_under_sec_published_limit():
    """SEC publishes 10 req/s. We must sit clearly beneath it, with a pool cap."""
    assert 0 < dw.FORMD_REQUESTS_PER_SEC <= 5.0, "keep a 2x margin under 10 req/s"
    assert 1 <= dw.FORMD_MAX_WORKERS <= 8, "the pool must stay bounded"
    # Even if every worker fired the instant it was created, the pacer — not
    # the pool size — is what sets the request rate.
    assert _SHIPPED_PACER.rate == dw.FORMD_REQUESTS_PER_SEC


def test_rate_limiter_spaces_permits_by_the_declared_interval():
    """
    Assert on the pacer's own arithmetic (the slots it hands out), not on wall
    clock — a timing-based assertion would be flaky on CI.
    """
    limiter = dw._RateLimiter(5.0)
    waits = []
    with patch("app.services.delaware.time.monotonic", return_value=1000.0), \
         patch("app.services.delaware.time.sleep", side_effect=waits.append):
        for _ in range(5):
            limiter.acquire()

    # First call is free; each subsequent one waits one more 0.2s interval.
    assert waits == pytest.approx([0.2, 0.4, 0.6000000000000001, 0.8], abs=1e-6)
    # 5 permits never span less than 4 intervals => <= 5 req/s.
    assert waits[-1] >= (5 - 1) / dw.FORMD_REQUESTS_PER_SEC - 1e-9


def test_rate_limiter_is_thread_safe_under_the_pool():
    """
    Concurrent workers must not both grab the same slot. With sleep faked out,
    N acquires across N threads must still hand out N distinct slots spaced by
    the interval — i.e. the limiter, not the thread count, sets the rate.
    """
    limiter = dw._RateLimiter(5.0)
    n = 40
    with patch("app.services.delaware.time.monotonic", return_value=500.0), \
         patch("app.services.delaware.time.sleep"):
        threads = [threading.Thread(target=limiter.acquire) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    span = limiter._next_at - 500.0
    assert span == pytest.approx(n / dw.FORMD_REQUESTS_PER_SEC, abs=1e-6), \
        "a lost update would leave the window shorter than n/rate"


def test_pool_never_exceeds_the_declared_worker_bound():
    """The bounded pool is the second half of the courtesy contract."""
    signals = [_signal(i) for i in range(60)]
    in_flight = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def _fake_fetch(adsh, cik):
        with lock:
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        _REAL_SLEEP(0.002)          # hold the slot so overlap is observable
        with lock:
            in_flight["now"] -= 1
        return _xml()

    with patch("app.services.delaware._fetch_form_d_xml_content",
               side_effect=_fake_fetch):
        dw._enrich_related_persons(signals)

    assert in_flight["peak"] <= dw.FORMD_MAX_WORKERS
    assert all("related_persons" in s for s in signals)


def test_pool_is_not_wider_than_the_batch():
    """A 2-filing batch must not spin up FORMD_MAX_WORKERS threads."""
    seen = set()
    lock = threading.Lock()

    def _fake_fetch(adsh, cik):
        with lock:
            seen.add(threading.current_thread().name)
        _REAL_SLEEP(0.005)
        return _xml()

    with patch("app.services.delaware._fetch_form_d_xml_content",
               side_effect=_fake_fetch):
        dw._enrich_related_persons([_signal(1), _signal(2)])

    assert len(seen) <= 2


def test_courteous_user_agent_survives_on_the_fetch_path():
    """Do not get the fund blocked: SEC requires an identifying User-Agent."""
    captured = {}

    class _Resp:
        status_code = 200
        content = _xml()

    def _get(url, headers=None, timeout=None):
        captured.update(headers or {})
        return _Resp()

    with patch("app.services.delaware.requests.get", side_effect=_get):
        content = dw._fetch_form_d_xml_content("0001234567-26-000001", "0001234567")

    assert content
    assert "bullish.co" in captured.get("User-Agent", "").lower()


def test_pipeline_refetch_path_shares_the_same_pacer():
    """
    signal_pipeline.py re-fetches the same Form D XML per saved item. That path
    must draw on the SAME process-wide budget — SEC's limit applies to us as
    one client, and an unpaced fan-out of pipeline threads is how a client gets
    blocked.
    """
    acquired = {"n": 0}

    class _CountingPacer(dw._RateLimiter):
        def acquire(self):
            acquired["n"] += 1

    with patch.object(dw, "_SEC_PACER", _CountingPacer(0)), \
         patch("app.services.delaware._fetch_form_d_xml_content",
               return_value=_xml()):
        persons = dw.fetch_form_d_related_persons("0001234567-26-000001", "0001234567")

    assert persons and persons[0]["name"] == "Jane Founder"
    assert acquired["n"] == 1, "the pipeline re-fetch must take a rate-limit slot"


def test_429_retry_backoff_is_preserved():
    """The existing 429 retry/backoff in _fetch_form_d_xml_content must survive."""
    class _Resp:
        def __init__(self, status, content=b""):
            self.status_code = status
            self.content = content

    responses = [_Resp(429), _Resp(429), _Resp(200, _xml())]
    with patch("app.services.delaware.requests.get",
               side_effect=responses) as mock_get, \
         patch("app.services.delaware.time.sleep") as mock_sleep:
        content = dw._fetch_form_d_xml_content("0001234567-26-000001", "0001234567")

    assert content == _xml()
    assert mock_get.call_count == 3
    assert [c.args[0] for c in mock_sleep.call_args_list] == [1, 2]


# ── FIX 2: ordering is deliberate, not EDGAR's arbitrary order ───────────────

def test_delaware_incorporated_filings_win_when_the_run_is_capped():
    """
    If we must cap, cap deliberately: DE incorporation is the VC-track choice
    _extract_signal already rewards (score_boost 12 vs 8).
    """
    signals = (
        [_signal(i, inc_state="NY") for i in range(10)]
        + [_signal(100 + i, inc_state="DE") for i in range(3)]
    )

    with patch("app.services.delaware._fetch_form_d_xml_content",
               return_value=_xml()) as mock_fetch, \
         patch("app.services.delaware.time.sleep"):
        dw._enrich_related_persons(signals, limit=3)

    assert mock_fetch.call_count == 3
    enriched = [s["companyName"] for s in signals if "related_persons" in s]
    assert enriched == ["Brand 100", "Brand 101", "Brand 102"]


def test_priority_ordering_does_not_reorder_the_callers_list():
    """
    The DE-first sort must happen on an internal copy. search_recent_delaware_
    entities returns the same list object it enriched, and callers persist it
    in EDGAR order — silently resorting it would be an invisible behaviour
    change to both scan paths.
    """
    signals = (
        [_signal(i, inc_state="NY") for i in range(4)]
        + [_signal(100 + i, inc_state="DE") for i in range(2)]
    )
    before = [s["companyName"] for s in signals]

    with patch("app.services.delaware._fetch_form_d_xml_content",
               return_value=_xml()):
        dw._enrich_related_persons(signals)

    assert [s["companyName"] for s in signals] == before
    assert all("related_persons" in s for s in signals)


def test_ordering_is_stable_inside_a_tier():
    """Within one incorporation tier, EDGAR's own result order is preserved."""
    signals = [_signal(i, inc_state="TX") for i in range(6)]

    with patch("app.services.delaware._fetch_form_d_xml_content",
               return_value=_xml()), \
         patch("app.services.delaware.time.sleep"):
        dw._enrich_related_persons(signals, limit=3)

    enriched = [s["companyName"] for s in signals if "related_persons" in s]
    assert enriched == ["Brand 0", "Brand 1", "Brand 2"]


def test_inc_state_is_stripped_before_signals_are_returned():
    """_inc_state is an internal ordering key — it must not leak to callers."""
    src = {
        "display_names": ["GRAZA OLIVE OIL LLC (CIK 0001111111)"],
        "inc_states":    ["DE"],
        "biz_locations": ["Brooklyn, NY"],
        "items":         ["06b"],
        "file_date":     "2026-07-20",
        "adsh":          "0001111111-26-000001",
        "ciks":          ["0001111111"],
    }
    from datetime import datetime
    sig = dw._extract_signal(src, set(), datetime(2026, 7, 25))
    assert sig["_inc_state"] == "DE", "ordering needs it during the run"

    def _fake_get(url, params=None, headers=None, timeout=None):
        class _R:
            status_code = 200
            def json(self_inner):
                if (params or {}).get("size") == 1:
                    return {"hits": {"total": {"value": 1}, "hits": []}}
                if (params or {}).get("from") == 0:
                    return {"hits": {"total": {"value": 1},
                                     "hits": [{"_source": src}]}}
                return {"hits": {"total": {"value": 1}, "hits": []}}
            def raise_for_status(self_inner):
                pass
        return _R()

    with patch("app.services.delaware.requests.get", side_effect=_fake_get), \
         patch("app.services.delaware.time.sleep"):
        result = dw.search_recent_delaware_entities(
            days_back=7, max_results=10, related_persons_limit=0
        )

    assert len(result["signals"]) == 1
    assert "_inc_state" not in result["signals"][0]
    assert "_name" not in result["signals"][0]
    # _adsh/_cik intentionally survive — signal_pipeline needs them.
    assert result["signals"][0]["_adsh"] == "0001111111-26-000001"


# ── FIX 3: conviction-first precedence across the WHOLE officer list ─────────

_CONVICTION = {"name": "Ari Conviction", "reason": "Founder of Thing",
               "known_brands": ["Thing"], "designation": "founder"}
_ALUMNI     = {"name": "Sam Alumni", "reason": "VP at BigCo",
               "known_brands": ["BigCo"], "designation": "alumni"}


def _run(people, conviction_for=(), alumni_for=(), xml_bytes=None):
    """Enrich one signal with both watchlist matchers faked."""
    def _fake_conv(texts):
        return (dict(_CONVICTION)
                if any(n in (t or "") for t in texts for n in conviction_for)
                else None)

    def _fake_alum(texts):
        return (dict(_ALUMNI)
                if any(n in (t or "") for t in texts for n in alumni_for)
                else None)

    sig = _signal(1)
    with patch("app.services.delaware._fetch_form_d_xml_content",
               return_value=xml_bytes if xml_bytes is not None else _xml(people)), \
         patch("app.services.conviction.check_conviction_match_multi",
               side_effect=_fake_conv), \
         patch("app.services.exit_watch.check_exit_alumni_match_multi",
               side_effect=_fake_alum), \
         patch("app.services.delaware.time.sleep"):
        dw._enrich_related_persons([sig])
    return sig


def test_conviction_wins_even_when_listed_last_on_the_filing():
    """
    The P1 fix: precedence is across the whole officer list. An alumni named
    ABOVE a conviction founder must not win — that would silently downgrade
    the strongest signal this product has.
    """
    sig = _run(
        [("Sam", "Alumni", "Director"), ("Ari", "Conviction", "Executive Officer")],
        conviction_for=["Ari Conviction"],
        alumni_for=["Sam Alumni"],
    )

    assert sig["conviction_match"] == _CONVICTION
    assert "exit_alumni_match" not in sig, \
        "alumni must be promoted ONLY when no conviction exists anywhere"
    # Per-person values are untouched — promotion copies, it never moves.
    per_person = {p["name"]: p for p in sig["related_persons"]}
    assert per_person["Sam Alumni"]["exit_alumni_match"] == _ALUMNI
    assert per_person["Ari Conviction"]["conviction_match"] == _CONVICTION


def test_alumni_promoted_only_when_no_conviction_anywhere():
    sig = _run(
        [("Pat", "Nobody", "Director"), ("Sam", "Alumni", "Executive Officer")],
        alumni_for=["Sam Alumni"],
    )
    assert "conviction_match" not in sig
    assert sig["exit_alumni_match"] == _ALUMNI


def test_conviction_and_alumni_designations_stay_separate_per_person():
    """A person matching conviction is never also labelled alumni."""
    sig = _run(
        [("Ari", "Conviction", "Executive Officer")],
        conviction_for=["Ari Conviction"],
        alumni_for=["Ari Conviction"],
    )
    person = sig["related_persons"][0]
    assert person["conviction_match"] == _CONVICTION
    assert person["exit_alumni_match"] is None


def test_precedence_holds_across_a_full_parallel_batch():
    """
    Parallel fetching must not scramble precedence. Every signal in the batch
    carries an alumni listed before a conviction founder; every one must
    promote the conviction match.
    """
    signals = [_signal(i) for i in range(20)]
    people  = [("Sam", "Alumni", "Director"), ("Ari", "Conviction", "Executive Officer")]

    def _fake_conv(texts):
        return (dict(_CONVICTION)
                if any("Ari Conviction" in (t or "") for t in texts) else None)

    def _fake_alum(texts):
        return (dict(_ALUMNI)
                if any("Sam Alumni" in (t or "") for t in texts) else None)

    with patch("app.services.delaware._fetch_form_d_xml_content",
               return_value=_xml(people)), \
         patch("app.services.conviction.check_conviction_match_multi",
               side_effect=_fake_conv), \
         patch("app.services.exit_watch.check_exit_alumni_match_multi",
               side_effect=_fake_alum), \
         patch("app.services.delaware.time.sleep"):
        dw._enrich_related_persons(signals)

    assert all(s["conviction_match"] == _CONVICTION for s in signals)
    assert all("exit_alumni_match" not in s for s in signals)


# ── FIX 3: offering figures still merged (minimum_investment included) ───────

def test_offering_figures_including_minimum_investment_are_merged():
    signals = [_signal(i) for i in range(30)]
    with patch("app.services.delaware._fetch_form_d_xml_content",
               return_value=_xml(total=2_000_000, sold=750_000, minimum=50_000)), \
         patch("app.services.delaware.time.sleep"):
        dw._enrich_related_persons(signals)

    assert all(s["total_offering"] == 2_000_000 for s in signals)
    assert all(s["amount_sold"] == 750_000 for s in signals)
    assert all(s["minimum_investment"] == 50_000 for s in signals)


def test_offering_figures_never_gate_a_signal():
    """Raise size RANKS, it never excludes — a tiny raise still gets officers."""
    signals = [_signal(1)]
    with patch("app.services.delaware._fetch_form_d_xml_content",
               return_value=_xml(total=1_000, sold=1_000, minimum=1_000)), \
         patch("app.services.delaware.time.sleep"):
        dw._enrich_related_persons(signals)

    assert signals[0]["minimum_investment"] == 1_000
    assert signals[0]["related_persons"]


# ── Resilience: one bad filing never costs the batch ─────────────────────────

def test_one_fetch_failure_does_not_abort_the_rest():
    """A raising fetch for filing #7 must leave the other 19 fully enriched."""
    signals = [_signal(i) for i in range(20)]

    def _fake_fetch(adsh, cik):
        if adsh == signals[7]["_adsh"]:
            raise ConnectionError("EDGAR reset the connection")
        return _xml()

    with patch("app.services.delaware._fetch_form_d_xml_content",
               side_effect=_fake_fetch), \
         patch("app.services.delaware.time.sleep"):
        dw._enrich_related_persons(signals)

    enriched = [s for s in signals if "related_persons" in s]
    assert len(enriched) == 19
    assert "related_persons" not in signals[7]


def test_one_http_miss_does_not_abort_the_rest():
    """A None (404/500) for one filing is a hole of one, not a dead batch."""
    signals = [_signal(i) for i in range(20)]

    def _fake_fetch(adsh, cik):
        return None if adsh == signals[3]["_adsh"] else _xml()

    with patch("app.services.delaware._fetch_form_d_xml_content",
               side_effect=_fake_fetch), \
         patch("app.services.delaware.time.sleep"):
        dw._enrich_related_persons(signals)

    assert len([s for s in signals if "related_persons" in s]) == 19
    assert "related_persons" not in signals[3]


def test_one_malformed_xml_does_not_abort_the_rest():
    """Unparseable XML for one filing must not take the batch down."""
    signals = [_signal(i) for i in range(10)]

    def _fake_fetch(adsh, cik):
        return b"<<< not xml" if adsh == signals[5]["_adsh"] else _xml()

    with patch("app.services.delaware._fetch_form_d_xml_content",
               side_effect=_fake_fetch), \
         patch("app.services.delaware.time.sleep"):
        dw._enrich_related_persons(signals)

    assert len([s for s in signals if "related_persons" in s]) == 9
    assert "related_persons" not in signals[5]


def test_matcher_exception_on_one_filing_does_not_abort_the_rest():
    """A blow-up inside parsing/matching is contained to its own filing."""
    signals = [_signal(i) for i in range(8)]
    calls = {"n": 0}
    lock = threading.Lock()

    def _boom(texts):
        with lock:
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("watchlist blew up")
        return None

    with patch("app.services.delaware._fetch_form_d_xml_content",
               return_value=_xml()), \
         patch("app.services.conviction.check_conviction_match_multi",
               side_effect=_boom), \
         patch("app.services.exit_watch.check_exit_alumni_match_multi",
               return_value=None), \
         patch("app.services.delaware.time.sleep"):
        dw._enrich_related_persons(signals)

    assert len([s for s in signals if "related_persons" in s]) == 7


def test_zero_limit_still_disables_extraction():
    """related_persons_limit=0 is the documented off switch — keep it working."""
    signals = [_signal(i) for i in range(5)]
    with patch("app.services.delaware._fetch_form_d_xml_content") as mock_fetch:
        dw._enrich_related_persons(signals, limit=0)
    mock_fetch.assert_not_called()
    assert all("related_persons" not in s for s in signals)


def test_empty_batch_is_a_no_op():
    with patch("app.services.delaware._fetch_form_d_xml_content") as mock_fetch:
        dw._enrich_related_persons([])
    mock_fetch.assert_not_called()
