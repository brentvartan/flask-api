"""
The hard $250/month Anthropic cap (Brent, 2026-08-04).

Before this, every call site read response.usage, logged it, and threw it away —
the app ran for months with no idea what it spent. A budget denominated in
SIGNALS is not a budget denominated in DOLLARS; the conversion factor is output
length, which nothing was measuring.
"""
import json

import pytest

from app.extensions import db as _db
from app.models.item import Item
from app.services import cost


class _Usage:
    """Mirrors the shape of anthropic's response.usage."""
    def __init__(self, inp=0, out=0, cache_read=0, cache_write=0):
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = cache_write


@pytest.fixture(autouse=True)
def _reset_pending():
    with cost._lock:
        cost._pending["usd"], cost._pending["calls"] = 0.0, 0
    yield
    with cost._lock:
        cost._pending["usd"], cost._pending["calls"] = 0.0, 0


class TestPricing:
    def test_sonnet_input_and_output(self):
        # 1M in @ $3, 1M out @ $15
        assert cost.price_call("claude-sonnet-4-6",
                               _Usage(inp=1_000_000)) == pytest.approx(3.00)
        assert cost.price_call("claude-sonnet-4-6",
                               _Usage(out=1_000_000)) == pytest.approx(15.00)

    def test_haiku_is_a_third_of_sonnet_input(self):
        assert cost.price_call("claude-haiku-4-5",
                               _Usage(inp=1_000_000)) == pytest.approx(1.00)

    def test_cache_read_is_a_tenth_of_input(self):
        assert cost.price_call("claude-sonnet-4-6",
                               _Usage(cache_read=1_000_000)) == pytest.approx(0.30)

    def test_cache_write_is_1_25x_input(self):
        assert cost.price_call("claude-sonnet-4-6",
                               _Usage(cache_write=1_000_000)) == pytest.approx(3.75)

    def test_input_tokens_is_the_uncached_remainder_not_the_total(self):
        """The field that most invites a wrong reading. A cached call reports the
        cached span in cache_read_input_tokens and only the rest in input_tokens;
        treating input_tokens as the whole prompt understates a cached call."""
        cached = cost.price_call("claude-sonnet-4-6",
                                 _Usage(inp=800, cache_read=7600, out=700))
        uncached = cost.price_call("claude-sonnet-4-6",
                                   _Usage(inp=8400, out=700))
        assert cached < uncached
        assert cached == pytest.approx(800*3e-6 + 7600*3e-6*0.1 + 700*15e-6)

    def test_unknown_model_is_metered_not_free(self):
        """A model nobody added to the table must not be free — that is how a
        cap silently stops capping."""
        assert cost.price_call("claude-something-new", _Usage(inp=1_000_000)) > 0

    def test_missing_usage_is_zero_not_a_crash(self):
        assert cost.price_call("claude-sonnet-4-6", None) == 0.0
        assert cost.price_call("claude-sonnet-4-6", {}) == 0.0


class TestLedger:
    def test_records_and_accumulates(self, db, app, admin_user):
        cost.record("claude-sonnet-4-6", _Usage(inp=1_000_000), flush=True)
        assert cost.month_to_date_usd() == pytest.approx(3.00)
        cost.record("claude-sonnet-4-6", _Usage(inp=1_000_000), flush=True)
        assert cost.month_to_date_usd() == pytest.approx(6.00)

    def test_pending_counts_before_flush(self, db, app, admin_user):
        """Unflushed spend must still bind the cap, or four workers could blow
        past it in the window between flushes."""
        cost.record("claude-sonnet-4-6", _Usage(inp=1_000_000))
        assert cost.month_to_date_usd() == pytest.approx(3.00)

    def test_ledger_row_is_internal(self, db, app, admin_user):
        cost.record("claude-haiku-4-5", _Usage(inp=1000), flush=True)
        row = Item.query.filter_by(title=cost.LEDGER_TITLE).first()
        assert row is not None
        assert row.item_type == "system"
        assert row.owner_id is not None, "Item.owner_id is NOT NULL — a ledger row without it never persists, and the cap silently resets on every restart"
        assert cost._month_key() in json.loads(row.description)["months"]


class TestCap:
    def test_allows_spending_under_the_cap(self, db, app, admin_user, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_MONTHLY_CAP_USD", "250")
        assert cost.check_budget(0.02) is True

    def test_blocks_at_the_cap(self, db, app, admin_user, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_MONTHLY_CAP_USD", "10")
        cost.record("claude-sonnet-4-6", _Usage(inp=4_000_000), flush=True)  # $12
        assert cost.check_budget(0.02) is False

    def test_fails_closed_when_the_ledger_is_unreadable(self, db, app, admin_user, monkeypatch):
        """The whole point of a hard cap: an unknown spend is treated as
        exhausted, never as zero."""
        def _boom():
            raise RuntimeError("db down")
        monkeypatch.setattr(cost, "month_to_date_usd", _boom)
        assert cost.check_budget(0.01) is False

    def test_zero_cap_blocks_everything(self, db, app, admin_user, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_MONTHLY_CAP_USD", "0")
        assert cost.check_budget(0.0) is False

    def test_summary_reports_headroom(self, db, app, admin_user, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_MONTHLY_CAP_USD", "100")
        cost.record("claude-sonnet-4-6", _Usage(inp=10_000_000), flush=True)  # $30
        s = cost.summary()
        assert s["spent_usd"] == pytest.approx(30.0)
        assert s["remaining_usd"] == pytest.approx(70.0)
        assert s["pct_used"] == pytest.approx(30.0)
        assert s["exhausted"] is False


class TestWiring:
    def test_scheduler_imports_resolve(self):
        """The dollar gate imports cost.summary and enrichment._EST_SCORE_USD at
        runtime inside a try/except that falls back to 'skip scoring'. A typo in
        either name would therefore disable ALL scoring silently while looking
        exactly like the cap working. Import them for real."""
        from app.services.cost import summary          # noqa: F401
        from app.services.enrichment import _EST_SCORE_USD
        assert _EST_SCORE_USD > 0

    def test_enrich_refuses_when_the_cap_is_reached(self, db, app, monkeypatch):
        from app.services import enrichment
        monkeypatch.setattr(enrichment, "_get_client",
                            lambda: pytest.fail("must not call the API when capped"))
        monkeypatch.setenv("ANTHROPIC_MONTHLY_CAP_USD", "0")
        out = enrichment.enrich_signal({"company_name": "Capped Co"})
        assert out["enriched"] is False
        assert out["error"] == "monthly_budget_exhausted"


class TestLedgerOwnership:
    """The trap that already bit scheduler bookkeeping once: Item.owner_id is a
    NOT NULL FK, an insert without it raises IntegrityError, and a broad except
    swallows it. Here that failure mode would mean the ledger never persists —
    month-to-date resets on every deploy and the cap never binds."""

    def test_spend_is_retained_when_it_cannot_be_persisted(self, db, app):
        """No users row -> cannot own the ledger. The spend must stay pending,
        not vanish: losing it would under-report and let the cap be exceeded."""
        cost.record("claude-sonnet-4-6", _Usage(inp=1_000_000), flush=True)
        with cost._lock:
            assert cost._pending["usd"] == pytest.approx(3.00), "spend was dropped"

    def test_persists_once_an_owner_exists(self, db, app, admin_user):
        cost.record("claude-sonnet-4-6", _Usage(inp=1_000_000), flush=True)
        with cost._lock:
            assert cost._pending["usd"] == 0.0
        assert cost.month_to_date_usd() == pytest.approx(3.00)
