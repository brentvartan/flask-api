"""
P1 tests — confluence alert timeline + watchlist match alert rendering.

Two defects are covered here.

1. confluence.send_confluence_alert_for_hit rebuilt its timeline with a
   brand_key-only query, while _find_cluster deliberately clusters across
   brand_key OR shared person key OR shared coined-term key. For exactly the
   cluster the feature exists to catch — Form D "Lightyear Incorporated" and
   trademark "Filament Sciences" both naming the same founder — the email
   rendered a one-row timeline and span_days=0 while its own header claimed two
   signals.

2. email.send_watchlist_match_alert had never fired in production (its only
   caller read a key nothing set), so its rendering had never been exercised:
   the CONVICTION/ALUMNI split, the None-score degradation, and HTML escaping of
   brand/person names that arrive from USPTO, SEC EDGAR and RSS feeds.

No live network and no real email sends — the Resend client is patched and
MAIL_SUPPRESS_SEND is forced off only so the HTML is actually built.
"""
from datetime import datetime, timedelta, timezone

import pytest
import resend

from app.services.confluence import (
    record_signal_and_check_confluence,
    send_confluence_alert_for_hit,
)
from app.services.email import send_watchlist_match_alert


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def sent_emails(monkeypatch):
    """Capture Resend payloads instead of sending. No network, no real email."""
    captured = []

    def _fake_send(params, options=None):
        captured.append(params)
        return {"id": "test-message-id"}

    monkeypatch.setenv("MAIL_SUPPRESS_SEND", "false")
    monkeypatch.setenv("RESEND_API_KEY", "test-key-never-used")
    monkeypatch.setattr(resend.Emails, "send", staticmethod(_fake_send))
    return captured


@pytest.fixture
def confluence_alerts(monkeypatch):
    """Capture the kwargs send_confluence_alert_for_hit hands to the email builder."""
    calls = []

    def _fake_alert(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("app.services.email.send_confluence_alert", _fake_alert)
    return calls


def _make_signal_item(db, owner_id, title):
    import json
    from app.models.item import Item

    item = Item(title=title, owner_id=owner_id, item_type="signal",
                description=json.dumps({"_type": "signal"}))
    db.session.add(item)
    db.session.commit()
    return item


def _backdate_event(db, item_id, days):
    """Push a recorded SignalEvent into the past so span_days is measurable."""
    from app.models.signal_event import SignalEvent

    event = SignalEvent.query.filter_by(item_id=item_id).one()
    event.detected_at = datetime.now(timezone.utc) - timedelta(days=days)
    db.session.commit()
    return event


# ── Fix 1: the alert timeline must reflect the cluster that fired the hit ─────

def test_person_key_cluster_timeline_spans_both_brand_keys(db, admin_user, confluence_alerts):
    """
    The headline case. Form D "Lightyear Incorporated" (brand_key=lightyear) and
    trademark "Filament Sciences" (brand_key=filament) cluster ONLY via the shared
    person key. The alert must show both rows and a real span, not one row and 0d.
    """
    item_a = _make_signal_item(db, admin_user.id, "Lightyear Incorporated")
    item_b = _make_signal_item(db, admin_user.id, "Filament Sciences")

    record_signal_and_check_confluence(
        item_id=item_a.id,
        owner_id=admin_user.id,
        brand_name="Lightyear Incorporated",
        signal_type="delaware",
        source_url="https://sec.gov/lightyear",
        person_keys=["john testfounder"],
    )
    _backdate_event(db, item_a.id, days=40)

    result = record_signal_and_check_confluence(
        item_id=item_b.id,
        owner_id=admin_user.id,
        brand_name="Filament Sciences",
        signal_type="trademark",
        source_url="https://uspto.gov/filament",
        person_keys=["john testfounder"],
    )
    assert result["is_confluence"] is True
    assert result["hit_id"] is not None

    assert send_confluence_alert_for_hit(result["hit_id"], ["alerts@test.com"]) is True
    assert len(confluence_alerts) == 1
    call = confluence_alerts[0]

    # The two brand_keys differ — a brand_key-only query returns exactly one row.
    types = {row["signal_type"] for row in call["timeline"]}
    assert types == {"delaware", "trademark"}, (
        f"timeline dropped the cross-key evidence: {call['timeline']}"
    )
    assert len(call["timeline"]) == 2
    assert call["span_days"] == 40
    # The email must not contradict itself: signal_count and timeline agree.
    assert call["signal_count"] == len(call["timeline"])


def test_coined_term_cluster_timeline_spans_both_brand_keys(db, admin_user, confluence_alerts):
    """Same requirement for the coined-term join (Popzyx / Popzyx Beverages LLC)."""
    item_a = _make_signal_item(db, admin_user.id, "Popzyx")
    item_b = _make_signal_item(db, admin_user.id, "Popzyx Beverages LLC")

    record_signal_and_check_confluence(
        item_id=item_a.id,
        owner_id=admin_user.id,
        brand_name="Popzyx",
        signal_type="trademark",
        coined_term_keys=["popzyx"],
    )
    _backdate_event(db, item_a.id, days=12)

    result = record_signal_and_check_confluence(
        item_id=item_b.id,
        owner_id=admin_user.id,
        brand_name="Popzyx Beverages LLC",
        signal_type="delaware",
        coined_term_keys=["popzyx"],
    )
    assert result["is_confluence"] is True

    send_confluence_alert_for_hit(result["hit_id"], ["alerts@test.com"])
    call = confluence_alerts[0]
    assert {row["signal_type"] for row in call["timeline"]} == {"trademark", "delaware"}
    assert call["span_days"] == 12


def test_chained_cluster_timeline_matches_signal_count(db, admin_user, confluence_alerts):
    """
    Three signals chained THROUGH the triggering signal, not through the canonical
    brand_key: Aeroblix and Cyclobrix share no key with each other, but Bexoplan
    names both founders and merges all three. The alert header says 3 signals, so
    the timeline must contain 3 rows.
    """
    item_a = _make_signal_item(db, admin_user.id, "Aeroblix")
    item_c = _make_signal_item(db, admin_user.id, "Cyclobrix")
    item_b = _make_signal_item(db, admin_user.id, "Bexoplan")

    record_signal_and_check_confluence(
        item_id=item_a.id, owner_id=admin_user.id, brand_name="Aeroblix",
        signal_type="delaware", person_keys=["kim first"],
    )
    _backdate_event(db, item_a.id, days=30)

    record_signal_and_check_confluence(
        item_id=item_c.id, owner_id=admin_user.id, brand_name="Cyclobrix",
        signal_type="ctlogs", person_keys=["lee second"],
    )
    _backdate_event(db, item_c.id, days=20)

    result = record_signal_and_check_confluence(
        item_id=item_b.id, owner_id=admin_user.id, brand_name="Bexoplan",
        signal_type="trademark", person_keys=["kim first", "lee second"],
    )
    assert result["signal_count"] == 3

    send_confluence_alert_for_hit(result["hit_id"], ["alerts@test.com"])
    call = confluence_alerts[0]
    assert {row["signal_type"] for row in call["timeline"]} == {
        "delaware", "ctlogs", "trademark",
    }
    assert call["signal_count"] == len(call["timeline"]) == 3
    assert call["span_days"] == 30


def test_timeline_excludes_events_the_cluster_would_not_merge(db, admin_user, confluence_alerts):
    """
    A wrong merge is worse than a missed link. An unrelated brand belonging to the
    same owner — no shared brand_key, person key or coined-term key — must never
    appear in the timeline.
    """
    item_a = _make_signal_item(db, admin_user.id, "Zorbyx")
    item_b = _make_signal_item(db, admin_user.id, "Zorbyx Holdings")
    item_c = _make_signal_item(db, admin_user.id, "Unrelatedbrand")

    record_signal_and_check_confluence(
        item_id=item_c.id,
        owner_id=admin_user.id,
        brand_name="Unrelatedbrand",
        signal_type="ctlogs",
        person_keys=["alice stranger"],
        coined_term_keys=["unrelatedbrand"],
    )
    record_signal_and_check_confluence(
        item_id=item_a.id,
        owner_id=admin_user.id,
        brand_name="Zorbyx",
        signal_type="trademark",
        person_keys=["bob testfounder"],
        coined_term_keys=["zorbyx"],
    )
    result = record_signal_and_check_confluence(
        item_id=item_b.id,
        owner_id=admin_user.id,
        brand_name="Zorbyx Holdings",
        signal_type="delaware",
        person_keys=["bob testfounder"],
        coined_term_keys=["zorbyx"],
    )
    assert result["is_confluence"] is True

    send_confluence_alert_for_hit(result["hit_id"], ["alerts@test.com"])
    call = confluence_alerts[0]
    assert "ctlogs" not in {row["signal_type"] for row in call["timeline"]}


def test_same_brand_key_timeline_unchanged(db, admin_user, confluence_alerts):
    """Regression guard: the original same-brand_key path still renders both rows."""
    item_a = _make_signal_item(db, admin_user.id, "Neuro Gum")
    item_b = _make_signal_item(db, admin_user.id, "Neuro Gum LLC")

    record_signal_and_check_confluence(
        item_id=item_a.id, owner_id=admin_user.id,
        brand_name="Neuro Gum", signal_type="trademark",
    )
    _backdate_event(db, item_a.id, days=5)

    result = record_signal_and_check_confluence(
        item_id=item_b.id, owner_id=admin_user.id,
        brand_name="Neuro Gum LLC", signal_type="delaware",
    )
    send_confluence_alert_for_hit(result["hit_id"], ["alerts@test.com"])
    call = confluence_alerts[0]
    assert {row["signal_type"] for row in call["timeline"]} == {"trademark", "delaware"}
    assert call["span_days"] == 5


def test_alert_marks_hit_sent_once(db, admin_user, confluence_alerts):
    """A hit already alerted on must not re-alert."""
    item_a = _make_signal_item(db, admin_user.id, "Quibbix")
    item_b = _make_signal_item(db, admin_user.id, "Quibbix LLC")

    record_signal_and_check_confluence(
        item_id=item_a.id, owner_id=admin_user.id,
        brand_name="Quibbix", signal_type="trademark",
    )
    result = record_signal_and_check_confluence(
        item_id=item_b.id, owner_id=admin_user.id,
        brand_name="Quibbix LLC", signal_type="delaware",
    )
    assert send_confluence_alert_for_hit(result["hit_id"], ["alerts@test.com"]) is True
    assert send_confluence_alert_for_hit(result["hit_id"], ["alerts@test.com"]) is False
    assert len(confluence_alerts) == 1


# ── Fix 2: watchlist match alert renders for BOTH designations ───────────────

def test_conviction_alert_renders_conviction_badge(sent_emails):
    send_watchlist_match_alert(
        "alerts@test.com",
        person_name="Jane Convictionfounder",
        match_type="CONVICTION",
        brand_name="Lightyear Incorporated",
        signal_type="delaware",
        brand_score=78,
        watch_level="hot",
        thesis="Repeat founder re-entering functional beverage.",
        match_details={"role": "Managing Member"},
    )
    assert len(sent_emails) == 1
    html = sent_emails[0]["html"]
    assert "CONVICTION FOUNDER" in html
    assert "#7C3AED" in html                 # conviction purple
    assert "#B45309" not in html             # never the alumni amber
    assert "EXIT ALUMNI" not in html
    assert "Jane Convictionfounder" in html
    assert "Managing Member" in html


def test_alumni_alert_renders_alumni_badge(sent_emails):
    send_watchlist_match_alert(
        "alerts@test.com",
        person_name="Sam Exitalumnus",
        match_type="ALUMNI",
        brand_name="Filament Sciences",
        signal_type="trademark",
        brand_score=61,
        watch_level="warm",
        thesis="Senior operator from a category-defining exit.",
        match_details={"role": "Director"},
    )
    assert len(sent_emails) == 1
    html = sent_emails[0]["html"]
    assert "EXIT ALUMNI" in html
    assert "#B45309" in html                 # alumni amber
    assert "#7C3AED" not in html             # never the conviction purple
    assert "CONVICTION FOUNDER" not in html
    assert "USPTO Trademark Filing" in html


def test_conviction_and_alumni_render_differently(sent_emails):
    """The two designations are permanent and must never render identically."""
    common = dict(
        person_name="Pat Watchlisted",
        brand_name="Somebrandx",
        signal_type="delaware",
        brand_score=70,
        watch_level="hot",
    )
    send_watchlist_match_alert("a@test.com", match_type="CONVICTION", **common)
    send_watchlist_match_alert("a@test.com", match_type="ALUMNI", **common)
    assert sent_emails[0]["html"] != sent_emails[1]["html"]


def test_alert_degrades_gracefully_with_no_enrichment(sent_emails):
    """A brand-new signal has no score, watch level or thesis. Must not raise."""
    send_watchlist_match_alert(
        "alerts@test.com",
        person_name="Unknown Person",
        match_type="ALUMNI",
        brand_name="Brandnewbrandx",
        signal_type="ctlogs",
        brand_score=None,
        watch_level=None,
        thesis=None,
        match_details=None,
    )
    assert len(sent_emails) == 1
    html = sent_emails[0]["html"]
    assert "None" not in html
    assert "Bullish Score" not in html      # score block omitted, not rendered empty
    assert "One-Line Thesis" not in html
    assert "Brandnewbrandx" in html


def test_alert_handles_unknown_signal_type(sent_emails):
    """An unmapped (or missing) signal_type must fall back, not raise."""
    send_watchlist_match_alert(
        "alerts@test.com",
        person_name="Unknown Person",
        match_type="CONVICTION",
        brand_name="Brandx",
        signal_type=None,
    )
    assert len(sent_emails) == 1


# ── Fix 2: untrusted values are HTML-escaped ─────────────────────────────────

_INJECTION = '<script>alert(1)</script>'


def test_brand_name_with_html_is_escaped(sent_emails):
    """Brand names come from USPTO/SEC and are not ours. They must not inject markup."""
    send_watchlist_match_alert(
        "alerts@test.com",
        person_name="Jane Founder",
        match_type="CONVICTION",
        brand_name=f"Acme {_INJECTION} Labs",
        signal_type="delaware",
    )
    html = sent_emails[0]["html"]
    assert _INJECTION not in html
    assert "&lt;script&gt;" in html


def test_person_name_thesis_and_role_are_escaped(sent_emails):
    send_watchlist_match_alert(
        "alerts@test.com",
        person_name=f"Jane {_INJECTION}",
        match_type="ALUMNI",
        brand_name="Cleanbrandx",
        signal_type="delaware",
        brand_score=55,
        watch_level="warm",
        thesis=f"Thesis {_INJECTION}",
        match_details={"role": f"Manager {_INJECTION}"},
    )
    html = sent_emails[0]["html"]
    assert "<script>" not in html
    assert html.count("&lt;script&gt;") == 3


def test_confluence_alert_escapes_brand_and_source_url(sent_emails):
    """The confluence builder interpolates a source_url straight into href=."""
    from app.services.email import send_confluence_alert

    send_confluence_alert(
        to_email="alerts@test.com",
        brand_name=f"Evil {_INJECTION} Brand",
        brand_key="evil",
        signal_count=2,
        signal_types=["delaware", "trademark"],
        timeline=[
            {"signal_type": "delaware", "detected_at": "Jan 01, 2026",
             "source_url": 'https://x.test/"><script>alert(1)</script>'},
            {"signal_type": "trademark", "detected_at": "Feb 01, 2026", "source_url": None},
        ],
        span_days=31,
        bullish_score=72,
        watch_level="hot",
    )
    html = sent_emails[0]["html"]
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_founder_news_alert_escapes_rss_fields(sent_emails):
    """Article titles, snippets and links come from RSS feeds."""
    from app.services.email import send_founder_news_alert

    send_founder_news_alert(
        "alerts@test.com",
        founder_name=f"Jane {_INJECTION}",
        company="Brandx",
        bullish_score=70,
        new_articles=[{
            "link": 'https://x.test/"><img src=x onerror=alert(1)>',
            "title": f"Headline {_INJECTION}",
            "source": "Feed",
            "date": "2026-07-25",
            "snippet": f"Snippet {_INJECTION}",
        }],
    )
    html = sent_emails[0]["html"]
    assert "<script>" not in html
    assert "onerror=alert(1)>" not in html


def test_hot_alert_escapes_brand_fields(sent_emails):
    from app.services.email import send_hot_alert

    send_hot_alert(
        "alerts@test.com",
        hot_brands=[{
            "score": 80,
            "name": f"Brand {_INJECTION}",
            "category": "Food",
            "thesis": f"Thesis {_INJECTION}",
            "theme": "2026 Functional",
        }],
        scan_name="Nightly",
    )
    html = sent_emails[0]["html"]
    assert "<script>" not in html
    assert html.count("&lt;script&gt;") == 2
