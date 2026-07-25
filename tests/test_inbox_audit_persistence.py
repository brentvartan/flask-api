"""
Inbox-audit persistence.

The coverage number (30.4% as of the 2026-07 audit) is the metric this product is
judged by, and it had never once been written to the database. _store_audit_result
passed `name=` to Item, which has only `title`; the create branch raised TypeError,
a bare `except` downgraded it to a warning, and since nothing ever persisted the
update branch was unreachable — so it failed identically every run. The figure lived
in a module global that is per-gunicorn-worker (4 of them) and died on every deploy.

These tests assert the two properties that were missing: a row is actually written,
and successive runs accumulate into a series instead of overwriting.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.extensions import db as _db
from app.models.item import Item
from app.services.inbox_audit import (
    _store_audit_result,
    get_latest_audit,
    get_audit_history,
)


def _result(coverage_pct, run_at=None, total=23, found=7):
    return {
        "run_at": (run_at or datetime.now(timezone.utc)).isoformat(),
        "total_checked": total,
        "found_count": found,
        "missing_count": total - found,
        "coverage_pct": coverage_pct,
        "found": [],
        "missing": [],
    }


def test_audit_result_is_actually_written(db, admin_user, app):
    """The original defect: this row never existed."""
    _store_audit_result(_result(30.4), app)
    assert Item.query.filter_by(item_type="inbox_audit").count() == 1


def test_stored_row_uses_title_not_name(db, admin_user, app):
    """Item has no `name` column; passing name= is what raised TypeError."""
    _store_audit_result(_result(30.4), app)
    row = Item.query.filter_by(item_type="inbox_audit").first()
    assert row.title.startswith("Inbox Audit — ")


def test_runs_accumulate_rather_than_overwrite(db, admin_user, app):
    """A single coverage number proves nothing; a series is the point."""
    _store_audit_result(_result(30.4), app)
    _store_audit_result(_result(41.2), app)
    _store_audit_result(_result(55.0), app)
    assert Item.query.filter_by(item_type="inbox_audit").count() == 3


def test_get_latest_returns_most_recent_run(db, admin_user, app):
    now = datetime.now(timezone.utc)
    _store_audit_result(_result(30.4, run_at=now - timedelta(days=60)), app)
    _store_audit_result(_result(55.0, run_at=now), app)

    # created_at is set by the DB default; order the rows explicitly so the test
    # asserts retrieval order rather than insertion timing.
    rows = Item.query.filter_by(item_type="inbox_audit").order_by(Item.id).all()
    rows[0].created_at = now - timedelta(days=60)
    rows[1].created_at = now
    _db.session.commit()

    assert get_latest_audit(app)["coverage_pct"] == 55.0


def test_history_returns_series_newest_first(db, admin_user, app):
    now = datetime.now(timezone.utc)
    for pct in (30.4, 41.2, 55.0):
        _store_audit_result(_result(pct), app)

    rows = Item.query.filter_by(item_type="inbox_audit").order_by(Item.id).all()
    for offset, row in enumerate(reversed(rows)):
        row.created_at = now - timedelta(days=offset * 30)
    _db.session.commit()

    history = get_audit_history(app)
    assert [h["coverage_pct"] for h in history] == [55.0, 41.2, 30.4]


def test_history_survives_an_unparseable_row(db, admin_user, app):
    _store_audit_result(_result(30.4), app)
    _db.session.add(Item(
        title="Inbox Audit — corrupt", description="{not json",
        item_type="inbox_audit", owner_id=admin_user.id,
    ))
    _db.session.commit()
    assert [h["coverage_pct"] for h in get_audit_history(app)] == [30.4]


def test_no_results_yet_returns_none_not_error(db, admin_user, app):
    assert get_latest_audit(app) is None
    assert get_audit_history(app) == []


def test_storage_failure_is_not_swallowed(db, admin_user, app, monkeypatch):
    """
    A silent failure here is what hid the bug for months: the original code caught
    Exception and logged a warning, so a write that never worked looked like a
    working system. If serialisation or persistence breaks again it must surface.

    Patches json.dumps inside the module rather than session.commit, so the failure
    happens within _store_audit_result without breaking the db fixture's teardown.
    """
    import app.services.inbox_audit as audit_mod

    def boom(*_args, **_kwargs):
        raise RuntimeError("cannot serialise audit result")

    monkeypatch.setattr(audit_mod.json, "dumps", boom)
    with pytest.raises(RuntimeError, match="cannot serialise"):
        _store_audit_result(_result(30.4), app)
