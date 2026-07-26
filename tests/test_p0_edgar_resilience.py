"""
P0 tests for EDGAR resilience in delaware.py (Job 1 — Form D coverage).

Context: the nightly Form D pass failed on 8 of 10 runs because efts.sec.gov
returns intermittent 500s during SEC's overnight maintenance window and
_edgar_query had ZERO retry — one transient blip zeroed the whole night, since
the count probe returned an empty result on any RequestException.

These are pure-function tests. All HTTP is faked (no live network — the suite
forbids live calls) and time.sleep is patched so backoff costs nothing.
"""
import pytest
import requests
from unittest.mock import patch

from app.services import delaware as dw


# ── Fake HTTP plumbing ────────────────────────────────────────────────────────

class _FakeResponse:
    """Minimal stand-in for requests.Response (status/json/raise_for_status)."""

    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload    = payload if payload is not None else {}
        self.headers     = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Server Error for url: {dw.EDGAR_SEARCH_URL}",
                response=self,
            )


def _hit(name, adsh="0001234567-26-000101", cik="0001234567", items=("06b",)):
    """One EDGAR full-text search hit shaped like the real _source payload."""
    return {
        "_source": {
            "display_names": [f"{name} (CIK {cik})"],
            "inc_states":    ["DE"],
            "biz_locations": ["Brooklyn, NY"],
            "items":         list(items),
            "file_date":     "2026-07-20",
            "adsh":          adsh,
            "ciks":          [cik],
        }
    }


def _page(hits, total=1400):
    return {"hits": {"total": {"value": total}, "hits": list(hits)}}


_CONSUMER_HITS = [
    _hit("GRAZA OLIVE OIL LLC",  adsh="0001111111-26-000001", cik="0001111111"),
    _hit("HARKEN SWEETS INC",    adsh="0002222222-26-000002", cik="0002222222"),
    _hit("ALGAE COOKING CLUB",   adsh="0003333333-26-000003", cik="0003333333"),
]


# ── _edgar_query: retry on transient failures ────────────────────────────────

def test_edgar_query_retries_on_500_then_succeeds():
    """Two maintenance-window 500s must not lose the query — the 3rd try wins."""
    payload   = _page(_CONSUMER_HITS)
    responses = [_FakeResponse(500), _FakeResponse(500), _FakeResponse(200, payload)]

    with patch("app.services.delaware.requests.get",
               side_effect=responses) as mock_get, \
         patch("app.services.delaware.time.sleep") as mock_sleep:
        result = dw._edgar_query("", "2026-07-18", "2026-07-25", 0, 100)

    assert result == payload
    assert mock_get.call_count == 3, "should have retried twice before succeeding"
    # Exponential backoff: 2s then 4s
    assert [c.args[0] for c in mock_sleep.call_args_list] == [2, 4]


def test_edgar_query_retries_on_connection_error_then_succeeds():
    """Connection/timeout errors are transient too — retry, don't bail."""
    payload   = _page(_CONSUMER_HITS)
    responses = [
        requests.ConnectionError("connection reset by peer"),
        requests.Timeout("read timed out"),
        _FakeResponse(200, payload),
    ]

    with patch("app.services.delaware.requests.get",
               side_effect=responses) as mock_get, \
         patch("app.services.delaware.time.sleep"):
        result = dw._edgar_query("", "2026-07-18", "2026-07-25", 0, 100)

    assert result == payload
    assert mock_get.call_count == 3


def test_edgar_query_honours_retry_after_on_429():
    """SEC's Retry-After wins over our backoff on a 429."""
    responses = [
        _FakeResponse(429, headers={"Retry-After": "7"}),
        _FakeResponse(200, _page([])),
    ]

    with patch("app.services.delaware.requests.get", side_effect=responses), \
         patch("app.services.delaware.time.sleep") as mock_sleep:
        dw._edgar_query("", "2026-07-18", "2026-07-25", 0, 100)

    assert mock_sleep.call_args_list[0].args[0] == 7.0


def test_retry_after_is_clamped_and_falls_back():
    """A missing/garbage/absurd Retry-After can never stall the nightly job."""
    assert dw._retry_after_seconds(_FakeResponse(429), 4) == 4
    assert dw._retry_after_seconds(_FakeResponse(429, headers={"Retry-After": "soon"}), 4) == 4
    assert dw._retry_after_seconds(
        _FakeResponse(429, headers={"Retry-After": "9999"}), 4
    ) == dw.EDGAR_MAX_BACKOFF


# ── _edgar_query: give up correctly ──────────────────────────────────────────

def test_edgar_query_gives_up_after_attempt_budget():
    """After the budget, the failure propagates as a RequestException."""
    responses = [_FakeResponse(500) for _ in range(dw.EDGAR_MAX_ATTEMPTS)]

    with patch("app.services.delaware.requests.get",
               side_effect=responses) as mock_get, \
         patch("app.services.delaware.time.sleep"):
        with pytest.raises(requests.RequestException):
            dw._edgar_query("", "2026-07-18", "2026-07-25", 0, 100)

    assert mock_get.call_count == dw.EDGAR_MAX_ATTEMPTS


def test_edgar_query_network_failure_propagates_after_budget():
    exc = requests.ConnectionError("dns failure")

    with patch("app.services.delaware.requests.get",
               side_effect=[exc] * dw.EDGAR_MAX_ATTEMPTS) as mock_get, \
         patch("app.services.delaware.time.sleep"):
        with pytest.raises(requests.RequestException):
            dw._edgar_query("", "2026-07-18", "2026-07-25", 0, 100)

    assert mock_get.call_count == dw.EDGAR_MAX_ATTEMPTS


def test_edgar_query_does_not_retry_client_errors():
    """A 400/404 is our bug, not SEC's — fail fast, don't hammer them."""
    with patch("app.services.delaware.requests.get",
               side_effect=[_FakeResponse(404)]) as mock_get, \
         patch("app.services.delaware.time.sleep") as mock_sleep:
        with pytest.raises(requests.HTTPError):
            dw._edgar_query("", "2026-07-18", "2026-07-25", 0, 100)

    assert mock_get.call_count == 1
    assert mock_sleep.call_count == 0


# ── search_recent_delaware_entities: probe outage degrades, never zeroes ─────

def _probe_fails_sweep_works(probe_exc=None):
    """requests.get double: the size=1 count probe always fails, pages work."""
    pages = {
        0:   _page(_CONSUMER_HITS),
        100: _page([_hit("MOSH BARS LLC", adsh="0004444444-26-000004",
                         cik="0004444444")]),
    }

    def _fake_get(url, params=None, headers=None, timeout=None):
        params = params or {}
        if params.get("size") == 1:                       # the count probe
            raise (probe_exc or requests.ConnectionError("EDGAR probe 500"))
        return _FakeResponse(200, pages.get(params.get("from"), _page([])))

    return _fake_get


def test_probe_failure_falls_back_to_fixed_sweep():
    """
    A dead count probe must NOT zero the night. We sweep a fixed page depth,
    return a well-formed dict, and leave `error` unset so the caller keeps the
    signals instead of discarding the collection.
    """
    with patch("app.services.delaware.requests.get",
               side_effect=_probe_fails_sweep_works()), \
         patch("app.services.delaware.time.sleep"):
        result = dw.search_recent_delaware_entities(
            days_back=7, max_results=500, related_persons_limit=0
        )

    # Shape is unchanged — callers depend on these exact keys
    assert set(result) == {"signals", "total_found", "fetched", "error"}
    assert result["error"] is None, "probe fallback must not look like a failure"
    assert len(result["signals"]) == 4, "all four filings should have survived"
    assert result["fetched"] == 4
    # total_found is a floor derived from filings actually swept, never 0
    assert result["total_found"] == 4
    assert all(s["signal_type"] == "delaware" for s in result["signals"])
    assert all("_name" not in s for s in result["signals"])


def test_probe_failure_sweep_depth_is_bounded():
    """The blind sweep is capped at the fallback depth, not max_filings."""
    calls = {"n": 0}

    def _fake_get(url, params=None, headers=None, timeout=None):
        params = params or {}
        if params.get("size") == 1:
            raise requests.ConnectionError("EDGAR probe 500")
        calls["n"] += 1
        # Every page full: only the page cap can stop the sweep
        return _FakeResponse(200, _page(_CONSUMER_HITS))

    with patch("app.services.delaware.requests.get", side_effect=_fake_get), \
         patch("app.services.delaware.time.sleep"):
        result = dw.search_recent_delaware_entities(
            days_back=7, max_results=100000, related_persons_limit=0
        )

    expected_pages = dw.EDGAR_PROBE_FALLBACK_FILINGS // 100
    assert calls["n"] == expected_pages
    assert result["error"] is None


def test_probe_success_path_is_unchanged():
    """Normal night: probe reports the total and paging follows it."""
    def _fake_get(url, params=None, headers=None, timeout=None):
        params = params or {}
        if params.get("size") == 1:
            return _FakeResponse(200, _page([], total=150))
        if params.get("from") == 0:
            return _FakeResponse(200, _page(_CONSUMER_HITS, total=150))
        return _FakeResponse(200, _page([], total=150))

    with patch("app.services.delaware.requests.get", side_effect=_fake_get), \
         patch("app.services.delaware.time.sleep"):
        result = dw.search_recent_delaware_entities(
            days_back=7, max_results=500, related_persons_limit=0
        )

    assert result["total_found"] == 150
    assert result["error"] is None
    assert len(result["signals"]) == 3


def test_mid_sweep_failure_returns_partial_results_with_error():
    """Page 2 dying keeps page 1's brands — partial coverage beats none."""
    def _fake_get(url, params=None, headers=None, timeout=None):
        params = params or {}
        if params.get("size") == 1:
            return _FakeResponse(200, _page([], total=400))
        if params.get("from") == 0:
            return _FakeResponse(200, _page(_CONSUMER_HITS, total=400))
        return _FakeResponse(500)          # every retry on page 2 fails

    with patch("app.services.delaware.requests.get", side_effect=_fake_get), \
         patch("app.services.delaware.time.sleep"):
        result = dw.search_recent_delaware_entities(
            days_back=7, max_results=500, related_persons_limit=0
        )

    assert result["error"], "a mid-sweep failure must still be reported"
    assert len(result["signals"]) == 3, "page-1 signals must survive the break"
    assert result["fetched"] == 3
    assert result["total_found"] == 400
