"""
P0 tests for ctlogs.py — the two stacked faults that kept CT-log domain
discovery at ZERO signals for its entire existence.

Fault 1 (the killer): crt.sh returns not_before WITHOUT a timezone designator
("2026-07-20T14:03:11"), so datetime.fromisoformat produced a NAIVE datetime and
comparing it to the tz-aware cutoff raised TypeError. TypeError was not in the
per-cert except tuple, so it escaped the loop and aborted the whole pattern
query. Every pattern that actually returned certs died; only patterns returning
nothing "succeeded".

Fault 2: per-pattern failures were swallowed inside the fetch helper, so a fully
dark source (crt.sh has been 502-ing for 12+ days) still returned a
success-shaped result with error=None and the scheduler logged a clean run.

Pure-function tests — no network, no Flask app, no DB. The fetch layer
(_fetch_crtsh) is monkeypatched.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services import ctlogs


# ── Fixture data — crt.sh-shaped records ─────────────────────────────────────

def _naive_ts(days_ago: int) -> str:
    """A crt.sh timestamp: ISO-8601 with NO timezone designator."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _aware_ts(days_ago: int) -> str:
    """A timestamp that already carries an offset (defensive: format may change)."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _cert(not_before: str, domain: str) -> dict:
    return {"not_before": not_before, "common_name": domain, "name_value": domain}


FRESH_NAIVE = _cert(_naive_ts(2),  "getbrightly.com")
FRESH_AWARE = _cert(_aware_ts(1),  "tryglowbar.co")
OLD_NAIVE   = _cert(_naive_ts(90), "oldstalebrew.com")
UNPARSEABLE = _cert("not-a-date",  "junkbrand.com")


@pytest.fixture
def patch_fetch(monkeypatch):
    """Replace the crt.sh fetch layer with a canned payload or an exception."""
    def _apply(payload=None, exc=None):
        def _fake(pattern):
            if exc is not None:
                raise exc
            return list(payload or [])
        monkeypatch.setattr(ctlogs, "_fetch_crtsh", _fake)
    return _apply


# ── Fault 1: naive/aware datetime handling ───────────────────────────────────

def test_naive_not_before_is_parsed_and_recent_cert_is_kept(patch_fetch):
    """A tz-less crt.sh timestamp must compare cleanly and the cert must survive."""
    patch_fetch(payload=[FRESH_NAIVE])
    hits = dict(ctlogs._query_crtsh("get%.com", 14))
    assert "getbrightly.com" in hits, "recent naive-timestamp cert was dropped"
    assert hits["getbrightly.com"].tzinfo is not None, "not_before must be normalised to aware UTC"


def test_aware_not_before_still_works(patch_fetch):
    """An offset-carrying timestamp must keep working after normalisation."""
    patch_fetch(payload=[FRESH_AWARE])
    hits = dict(ctlogs._query_crtsh("try%.co", 14))
    assert "tryglowbar.co" in hits
    assert hits["tryglowbar.co"].tzinfo == timezone.utc


def test_old_cert_is_filtered_out(patch_fetch):
    """A cert issued before the cutoff must not survive the recency filter."""
    patch_fetch(payload=[OLD_NAIVE])
    hits = dict(ctlogs._query_crtsh("%brew%.com", 14))
    assert "oldstalebrew.com" not in hits


def test_one_bad_record_does_not_abort_the_pattern(patch_fetch):
    """
    The regression guard: a malformed record must be skipped, not kill the query.
    Before the fix a single record could raise out of the per-cert loop and
    silently discard every good hit in the same pattern.
    """
    patch_fetch(payload=[
        UNPARSEABLE,
        {"not_before": None, "common_name": "nullstamp.com", "name_value": "nullstamp.com"},
        {"not_before": _naive_ts(1), "common_name": None, "name_value": None},
        "totally malformed row",
        FRESH_NAIVE,
    ])
    hits = dict(ctlogs._query_crtsh("get%.com", 14))
    assert "getbrightly.com" in hits, "good hit lost to a neighbouring bad record"
    assert "junkbrand.com" not in hits
    assert "nullstamp.com" not in hits


# ── Filter-then-cap ordering and newest-first sort ───────────────────────────

def test_recency_filter_runs_before_the_cap(patch_fetch):
    """Stale certs must not consume the per-pattern budget."""
    patch_fetch(payload=[OLD_NAIVE, FRESH_NAIVE, OLD_NAIVE, FRESH_AWARE])
    hits = dict(ctlogs._query_crtsh("%glow%.com", 14))
    assert set(hits) == {"getbrightly.com", "tryglowbar.co"}


def test_cap_keeps_the_newest_domain(patch_fetch):
    """max_results is applied AFTER the newest-first sort, never before."""
    patch_fetch(payload=[FRESH_NAIVE, FRESH_AWARE, OLD_NAIVE])
    result = ctlogs.search_recent_ct_domains(days_back=14, max_results=1)
    assert len(result["signals"]) == 1
    # FRESH_AWARE (1 day old) is newer than FRESH_NAIVE (2 days old)
    assert result["signals"][0]["url"] == "https://tryglowbar.co"
    assert result["total_found"] == 2, "total_found must count the pre-cap population"


# ── Fault 2: a dead source must not look healthy ─────────────────────────────

def test_all_patterns_failed_returns_non_none_error(patch_fetch):
    """Every pattern 502s → the result must carry a named, non-None error."""
    patch_fetch(exc=ctlogs.CrtShError("HTTP 502", status=502))
    result = ctlogs.search_recent_ct_domains(days_back=14, max_results=100)

    assert result["error"] is not None, "a fully dark source reported a clean run"
    assert "502" in result["error"], "error must name the HTTP status seen"
    assert str(len(ctlogs.SEARCH_PATTERNS)) in result["error"], "error must name the failure count"
    assert result["signals"] == []
    assert result["patterns_failed"] == len(ctlogs.SEARCH_PATTERNS)
    assert result["patterns_succeeded"] == 0


def test_all_patterns_failed_without_http_status_still_errors(patch_fetch):
    """A network-level outage (no HTTP status) must still surface an error."""
    patch_fetch(exc=ctlogs.CrtShError("timed out"))
    result = ctlogs.search_recent_ct_domains(days_back=14, max_results=100)
    assert result["error"] is not None
    assert "timed out" in result["error"]


def test_healthy_scan_reports_no_error(patch_fetch):
    """The success path must stay clean — no false alarm."""
    patch_fetch(payload=[FRESH_NAIVE, FRESH_AWARE])
    result = ctlogs.search_recent_ct_domains(days_back=14, max_results=100)

    assert result["error"] is None
    assert len(result["signals"]) == 2
    assert result["patterns_failed"] == 0
    assert result["patterns_succeeded"] == len(ctlogs.SEARCH_PATTERNS)


def test_return_keys_are_additive(patch_fetch):
    """Existing callers (scheduler.py, scans/routes.py) read these keys."""
    patch_fetch(payload=[FRESH_NAIVE])
    result = ctlogs.search_recent_ct_domains(days_back=14, max_results=100)
    assert {"signals", "total_found", "fetched", "error"} <= set(result)
    assert {"patterns_attempted", "patterns_succeeded", "patterns_failed"} <= set(result)


# ── Fetch layer raises instead of swallowing ─────────────────────────────────

def test_fetch_raises_on_non_list_payload(monkeypatch):
    """A malformed crt.sh body is a failure, not an empty success."""
    class _Resp:
        def read(self):
            return b'{"error": "backend down"}'
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ctlogs.urllib.request, "urlopen", lambda *a, **kw: _Resp())
    with pytest.raises(ctlogs.CrtShError):
        ctlogs._fetch_crtsh("get%.com")


def test_fetch_raises_with_status_on_http_error(monkeypatch):
    """HTTP 502 must propagate as a CrtShError carrying the status code."""
    import urllib.error

    def _boom(*a, **kw):
        raise urllib.error.HTTPError("https://crt.sh/", 502, "Bad Gateway", {}, None)

    monkeypatch.setattr(ctlogs.urllib.request, "urlopen", _boom)
    with pytest.raises(ctlogs.CrtShError) as exc_info:
        ctlogs._fetch_crtsh("get%.com")
    assert exc_info.value.status == 502
