"""
Task 5 tests — standing coverage metrics.

No live network calls. Uses Flask test DB.
"""
import json
import pytest
from datetime import datetime, timedelta, timezone

from app.services.coverage import get_coverage_metrics, record_press_confirm, _brand_key


# ── record_press_confirm ──────────────────────────────────────────────────────

def test_record_press_confirm_persists(db, admin_user, app):
    """Press confirm is persisted to DB and returns recorded=True."""
    result = record_press_confirm("Poppi", "2026-06-01", app, source_url="https://example.com")
    assert result["recorded"] is True
    assert result["brand"] == "Poppi"
    assert result["confirmed_at"] == "2026-06-01"

    from app.models.item import Item
    with app.app_context():
        item = Item.query.filter_by(item_type="press_confirm", title="Poppi").first()
        assert item is not None
        meta = json.loads(item.description)
        assert meta["confirmed_at"] == "2026-06-01"
        assert meta["source_url"] == "https://example.com"


def test_record_press_confirm_upserts(db, admin_user, app):
    """Calling twice for the same brand updates the record, not duplicates."""
    from app.models.item import Item

    record_press_confirm("MoshBar", "2026-05-01", app)
    record_press_confirm("MoshBar", "2026-06-01", app)

    with app.app_context():
        count = Item.query.filter_by(item_type="press_confirm", title="MoshBar").count()
        assert count == 1
        item = Item.query.filter_by(item_type="press_confirm", title="MoshBar").first()
        meta = json.loads(item.description)
        assert meta["confirmed_at"] == "2026-06-01"


def test_record_press_confirm_invalid_date(db, admin_user, app):
    """Invalid date string returns an error dict."""
    result = record_press_confirm("Brand", "not-a-date", app)
    assert "error" in result
    assert "recorded" not in result


# ── get_coverage_metrics — inbox recall ───────────────────────────────────────

def test_inbox_recall_no_audit(db, admin_user, app):
    """With no audit run, inbox_recall returns zeros."""
    import app.services.inbox_audit as _ia_mod
    _ia_mod._latest_audit = None   # clear in-memory cache

    result = get_coverage_metrics(app, days_back=90)
    assert result["inbox_recall"]["coverage_pct"] == 0
    assert result["inbox_recall"]["as_of"] is None
    assert result["inbox_recall"]["total_checked"] == 0


def test_inbox_recall_from_audit(db, admin_user, app):
    """
    get_coverage_metrics reads coverage_pct from the latest PERSISTED audit.

    Previously this test poked a module-global cache (_latest_audit), which is the
    design that hid the real defect: the DB write had never once succeeded, and the
    global made it look like it had. The global was removed on 2026-07-25 — it was
    per-gunicorn-worker and died on deploy — so this now exercises the real path.
    """
    from app.services.inbox_audit import _store_audit_result

    _store_audit_result({
        "coverage_pct": 42.5,
        "found_count": 17,
        "missing_count": 23,
        "total_checked": 40,
        "run_at": "2026-06-15T10:00:00+00:00",
        "found": [],
        "missing": [],
    }, app)

    result = get_coverage_metrics(app, days_back=90)
    assert result["inbox_recall"]["coverage_pct"] == 42.5
    assert result["inbox_recall"]["found_count"] == 17
    assert result["inbox_recall"]["as_of"] == "2026-06-15T10:00:00+00:00"


# ── get_coverage_metrics — lead time ─────────────────────────────────────────

def test_lead_time_empty(db, admin_user, app):
    """With no signals, lead_time is empty and stats are None."""
    result = get_coverage_metrics(app, days_back=90)
    lt = result["lead_time"]
    assert lt["count"] == 0
    assert lt["median_days"] is None
    assert lt["mean_days"] is None
    assert lt["brands"] == []


def test_lead_time_early_before_press(db, admin_user, app):
    """An early signal followed by a press_stealth signal yields a positive lead time."""
    from app.models.item import Item
    from app.extensions import db as _db

    now = datetime.now(timezone.utc)

    with app.app_context():
        early = Item(
            title="Zestify",
            owner_id=admin_user.id,
            item_type="signal",
            description=json.dumps({"_type": "signal", "signal_type": "delaware"}),
        )
        early.created_at = now - timedelta(days=30)

        press = Item(
            title="Zestify",
            owner_id=admin_user.id,
            item_type="signal",
            description=json.dumps({"_type": "signal", "signal_type": "press_stealth"}),
        )
        press.created_at = now - timedelta(days=5)

        _db.session.add_all([early, press])
        _db.session.commit()

    result = get_coverage_metrics(app, days_back=90)
    lt = result["lead_time"]
    assert lt["count"] == 1
    brand = lt["brands"][0]
    assert brand["brand"] == "zestify"
    assert brand["days_lead"] == 25
    assert lt["median_days"] == 25
    assert lt["mean_days"] == 25.0


def test_lead_time_press_only_no_lead(db, admin_user, app):
    """A brand with only a press_stealth signal (no early signal) is excluded."""
    from app.models.item import Item
    from app.extensions import db as _db

    with app.app_context():
        _db.session.add(Item(
            title="PressOnly",
            owner_id=admin_user.id,
            item_type="signal",
            description=json.dumps({"_type": "signal", "signal_type": "press_stealth"}),
        ))
        _db.session.commit()

    result = get_coverage_metrics(app, days_back=90)
    brand_keys = [b["brand"] for b in result["lead_time"]["brands"]]
    assert "pressonly" not in brand_keys


def test_lead_time_press_before_early_excluded(db, admin_user, app):
    """If the press signal is older than the early signal, no lead time is attributed."""
    from app.models.item import Item
    from app.extensions import db as _db

    now = datetime.now(timezone.utc)

    with app.app_context():
        press = Item(
            title="TooLate",
            owner_id=admin_user.id,
            item_type="signal",
            description=json.dumps({"_type": "signal", "signal_type": "press_stealth"}),
        )
        press.created_at = now - timedelta(days=30)

        early = Item(
            title="TooLate",
            owner_id=admin_user.id,
            item_type="signal",
            description=json.dumps({"_type": "signal", "signal_type": "delaware"}),
        )
        early.created_at = now - timedelta(days=5)

        _db.session.add_all([press, early])
        _db.session.commit()

    result = get_coverage_metrics(app, days_back=90)
    brand_keys = [b["brand"] for b in result["lead_time"]["brands"]]
    assert "toolate" not in brand_keys


def test_lead_time_manual_press_confirm(db, admin_user, app):
    """A manual press-confirm record is included in lead-time computation."""
    from app.models.item import Item
    from app.extensions import db as _db

    # Pin the time-of-day to midday UTC. This test used to use datetime.now() and
    # failed whenever it ran between 00:00-04:00 UTC — see
    # test_lead_time_is_stable_across_utc_midnight_boundary below for the root cause.
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    early_dt = now - timedelta(days=45)

    with app.app_context():
        early = Item(
            title="ConfirmBrand",
            owner_id=admin_user.id,
            item_type="signal",
            description=json.dumps({"_type": "signal", "signal_type": "trademark"}),
        )
        early.created_at = early_dt
        _db.session.add(early)
        _db.session.commit()

    # Press confirm 30 days after first signal
    confirmed = (early_dt + timedelta(days=30)).date().isoformat()
    record_press_confirm("ConfirmBrand", confirmed, app)

    result = get_coverage_metrics(app, days_back=90)
    brand_keys = [b["brand"] for b in result["lead_time"]["brands"]]
    assert "confirmbrand" in brand_keys
    matched = next(b for b in result["lead_time"]["brands"] if b["brand"] == "confirmbrand")
    assert matched["days_lead"] == 30


@pytest.mark.parametrize("utc_hour", [0, 1, 2, 3, 4, 12, 23])
def test_lead_time_is_stable_across_utc_midnight_boundary(db, admin_user, app, utc_hour):
    """
    days_lead must not depend on the time of day a signal was recorded.

    Regression (2026-07-25): the DB returns created_at as timezone-AWARE but in the
    server's LOCAL zone (e.g. UTC-04:00). coverage.py only normalised NAIVE datetimes,
    so aware-but-local values reached .date() untouched. For signals timestamped
    between 00:00 and 04:00 UTC, the local date is the PREVIOUS day, which inflated
    days_lead by one — always in the direction that flatters the product.

    Without _as_utc(), the utc_hour < 4 cases fail with 31 != 30.
    """
    from app.models.item import Item
    from app.extensions import db as _db

    base = datetime.now(timezone.utc).replace(
        hour=utc_hour, minute=30, second=0, microsecond=0
    )
    early_dt = base - timedelta(days=45)
    title = f"BoundaryBrand{utc_hour}"

    with app.app_context():
        item = Item(
            title=title,
            owner_id=admin_user.id,
            item_type="signal",
            description=json.dumps({"_type": "signal", "signal_type": "trademark"}),
        )
        item.created_at = early_dt
        _db.session.add(item)
        _db.session.commit()

    record_press_confirm(title, (early_dt + timedelta(days=30)).date().isoformat(), app)

    result = get_coverage_metrics(app, days_back=90)
    matched = next(
        (b for b in result["lead_time"]["brands"] if b["brand"] == _brand_key(title)), None
    )
    assert matched is not None, f"{title} missing from lead_time output"
    assert matched["days_lead"] == 30, (
        f"signal recorded at {utc_hour:02d}:30 UTC produced days_lead="
        f"{matched['days_lead']}, expected 30 — timezone normalisation regressed"
    )


def test_coverage_response_shape(db, admin_user, app):
    """get_coverage_metrics always returns the expected top-level keys."""
    result = get_coverage_metrics(app, days_back=30)
    assert "inbox_recall" in result
    assert "lead_time" in result
    assert "computed_at" in result
    assert result["days_back"] == 30

    ir = result["inbox_recall"]
    assert "coverage_pct" in ir
    assert "found_count" in ir
    assert "total_checked" in ir
    assert "as_of" in ir

    lt = result["lead_time"]
    assert "brands" in lt
    assert "median_days" in lt
    assert "mean_days" in lt
    assert "count" in lt
