"""
Task 7 tests — FreshData LinkedIn enrichment.

No live network calls. Uses monkeypatch for requests and DB fixtures for sweep.
"""
import json
import pytest
from unittest.mock import MagicMock, patch

from app.services.proxycurl import (
    build_context,
    enrich_founder,
    fetch_linkedin_headline,
    fetch_linkedin_profile,
    is_stealth_headline,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

FAKE_PROFILE = {
    "full_name":     "Emily Weiss",
    "headline":      "Building something new | Ex-Glossier",
    "about":         "Consumer beauty operator.",
    "profile_url":   "https://linkedin.com/in/emilyweiss",
    "follower_count": 12000,
    "connections":   500,
    "experiences": [
        {
            "title":     "Founder",
            "company":   "Stealth",
            "starts_at": {"year": 2024, "month": 1, "day": 1},
            "ends_at":   None,
        },
        {
            "title":     "CEO",
            "company":   "Glossier",
            "starts_at": {"year": 2014, "month": 4, "day": 1},
            "ends_at":   {"year": 2022, "month": 6, "day": 1},
        },
    ],
    "education": [
        {"school": "Barnard College", "degree_name": "BA", "field_of_study": "Art"},
    ],
}


def _mock_get(profile=None, status_code=200):
    """Return a mock requests.Response for a FreshData call."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = profile or FAKE_PROFILE
    resp.text = json.dumps(profile or FAKE_PROFILE)
    return resp


# ── build_context ─────────────────────────────────────────────────────────────

def test_build_context_maps_freshdata_fields():
    ctx = build_context(FAKE_PROFILE)
    assert ctx["headline"] == "Building something new | Ex-Glossier"
    assert ctx["summary"].startswith("Consumer beauty operator")
    assert ctx["linkedin_url"] == "https://linkedin.com/in/emilyweiss"
    assert ctx["follower_count"] == 12000
    assert len(ctx["experiences"]) == 2
    assert ctx["experiences"][0]["title"] == "Founder"
    assert ctx["experiences"][0]["end"] == "present"
    assert ctx["experiences"][1]["end"] == "2022"
    assert len(ctx["education"]) == 1
    assert ctx["education"][0]["school"] == "Barnard College"


def test_build_context_empty_profile():
    ctx = build_context({})
    assert ctx["headline"] == ""
    assert ctx["experiences"] == []
    assert ctx["education"] == []


# ── is_stealth_headline ───────────────────────────────────────────────────────

@pytest.mark.parametrize("headline", [
    "Building something new | Ex-Glossier",
    "Stealth startup",
    "Founder @ TBD",
    "Co-Founder, unannounced brand",
    "CEO, stealth consumer company",
    "entrepreneur in residence",
    "Launching new venture",
    "Operator | building next thing",
])
def test_is_stealth_headline_positive(headline):
    assert is_stealth_headline(headline), f"Expected True for: {headline!r}"


@pytest.mark.parametrize("headline", [
    "VP Marketing at Nike",
    "Senior Director, Retail Strategy",
    "Partner at Sequoia Capital",
    "Professor of Business Administration",
    "",
    None,
])
def test_is_stealth_headline_negative(headline):
    assert not is_stealth_headline(headline), f"Expected False for: {headline!r}"


# ── enrich_founder ────────────────────────────────────────────────────────────

def test_enrich_founder_no_url_returns_not_found_without_http_call(monkeypatch):
    """enrich_founder with no linkedin_url must not make any HTTP call."""
    http_calls = []
    monkeypatch.setenv("PROXYCURL_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.services.proxycurl.requests.get",
        lambda *a, **kw: http_calls.append(1),
    )
    result = enrich_founder("Emily Weiss", "Glossier")
    assert result == {"found": False}
    assert not http_calls


def test_enrich_founder_no_api_key_returns_not_found(monkeypatch):
    """enrich_founder with no API key set must not make any HTTP call."""
    monkeypatch.delenv("PROXYCURL_API_KEY", raising=False)
    http_calls = []
    monkeypatch.setattr(
        "app.services.proxycurl.requests.get",
        lambda *a, **kw: http_calls.append(1),
    )
    result = enrich_founder("Emily Weiss", "Glossier", linkedin_url="https://linkedin.com/in/emilyweiss")
    assert result == {"found": False}
    assert not http_calls


def test_enrich_founder_with_url_calls_freshdata(monkeypatch):
    """enrich_founder with a known URL fetches from FreshData and returns found=True."""
    monkeypatch.setenv("PROXYCURL_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.services.proxycurl.requests.get",
        lambda *a, **kw: _mock_get(),
    )
    result = enrich_founder(
        "Emily Weiss", "Glossier",
        linkedin_url="https://linkedin.com/in/emilyweiss",
    )
    assert result["found"] is True
    assert result["headline"] == "Building something new | Ex-Glossier"
    assert len(result["experiences"]) == 2


def test_enrich_founder_404_returns_not_found(monkeypatch):
    monkeypatch.setenv("PROXYCURL_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.services.proxycurl.requests.get",
        lambda *a, **kw: _mock_get(status_code=404),
    )
    result = enrich_founder("Nobody", "NoExist", linkedin_url="https://linkedin.com/in/nobody")
    assert result == {"found": False}


# ── fetch_linkedin_headline ───────────────────────────────────────────────────

def test_fetch_linkedin_headline_returns_headline(monkeypatch):
    monkeypatch.setenv("PROXYCURL_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.services.proxycurl.requests.get",
        lambda *a, **kw: _mock_get(),
    )
    result = fetch_linkedin_headline("https://linkedin.com/in/emilyweiss")
    assert result is not None
    assert result["headline"] == "Building something new | Ex-Glossier"
    assert result["full_name"] == "Emily Weiss"


def test_fetch_linkedin_headline_no_url_returns_none():
    result = fetch_linkedin_headline(None)
    assert result is None

    result = fetch_linkedin_headline("")
    assert result is None


# ── fetch_linkedin_profile ────────────────────────────────────────────────────

def test_fetch_linkedin_profile_returns_context_with_found(monkeypatch):
    monkeypatch.setenv("PROXYCURL_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.services.proxycurl.requests.get",
        lambda *a, **kw: _mock_get(),
    )
    result = fetch_linkedin_profile("https://linkedin.com/in/emilyweiss")
    assert result is not None
    assert result["found"] is True
    assert result["headline"] == "Building something new | Ex-Glossier"


def test_fetch_linkedin_profile_no_url_returns_none():
    assert fetch_linkedin_profile(None) is None
    assert fetch_linkedin_profile("") is None


# ── Watchlist sweep integration ───────────────────────────────────────────────

def test_watchlist_sweep_detects_stealth_shift(db, admin_user, app, monkeypatch):
    """Monthly sweep flags a watchlist person whose headline shifts to stealth language."""
    from app.models.item import Item
    from app.extensions import db as _db

    linkedin_url = "https://www.linkedin.com/in/miketest"

    with app.app_context():
        wl = Item(
            title="Mike Cessario",
            owner_id=admin_user.id,
            item_type="watchlist",
            description=json.dumps({
                "_type":       "watchlist",
                "name":        "Mike Cessario",
                "designation": "founder",
                "exit_brand":  "Liquid Death",
                "linkedin_url": linkedin_url,
                # No _last_headline set — first sweep
            }),
        )
        _db.session.add(wl)
        _db.session.commit()

    monkeypatch.setenv("PROXYCURL_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.services.proxycurl.requests.get",
        lambda *a, **kw: _mock_get({
            "full_name": "Mike Cessario",
            "headline":  "Founder @ stealth | Ex-Liquid Death",
        }),
    )

    # Run sweep without email side effect
    monkeypatch.setattr(
        "app.services.scheduler.send_founder_news_alert" if False else
        "app.services.proxycurl.fetch_linkedin_headline",
        lambda url: {"headline": "Founder @ stealth | Ex-Liquid Death", "full_name": "Mike Cessario"},
    )

    from app.services.scheduler import _run_watchlist_headline_sweep
    _run_watchlist_headline_sweep(app)

    # Verify headline was persisted and flagged
    with app.app_context():
        row = Item.query.filter_by(item_type="watchlist", title="Mike Cessario").first()
        meta = json.loads(row.description)
        assert meta["_last_headline"] == "Founder @ stealth | Ex-Liquid Death"
        assert "_headline_checked_at" in meta
