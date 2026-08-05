"""
The freeze: everything stays, nothing fires.

Brent froze Stealth Finder on 2026-08-05 to stop spend while the direction is
reconsidered. The requirement was specific — preserve the system exactly, stop
it doing anything that costs money. These tests pin that nothing slips through.
"""
import pytest

from app.services import cost


class _Usage:
    def __init__(self, inp=0, out=0):
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


@pytest.fixture(autouse=True)
def _clean():
    with cost._lock:
        cost._pending["usd"], cost._pending["calls"] = 0.0, 0
    yield
    with cost._lock:
        cost._pending["usd"], cost._pending["calls"] = 0.0, 0


class TestFreezeState:
    def test_defaults_to_thawed(self, db, app, admin_user):
        assert cost.is_frozen() is False

    def test_round_trips(self, db, app, admin_user):
        cost.set_frozen(True, reason="direction review")
        assert cost.is_frozen() is True
        cost.set_frozen(False)
        assert cost.is_frozen() is False

    def test_unreadable_state_is_treated_as_frozen(self, db, app, monkeypatch):
        """Fails closed. A wrong 'yes' costs a quiet night; a wrong 'no' spends
        money the owner explicitly stopped."""
        def _boom(conn):
            raise RuntimeError("ledger unreadable")
        monkeypatch.setattr(cost, "_read_ledger_raw", _boom)
        assert cost.is_frozen() is True

    def test_surfaces_in_the_spend_summary(self, db, app, admin_user):
        cost.set_frozen(True, reason="why")
        assert cost.summary()["frozen"] is True


class TestFreezeStopsSpend:
    def test_no_paid_call_is_made(self, db, app, admin_user):
        cost.set_frozen(True)

        class _Client:
            class messages:
                @staticmethod
                def create(**kw):
                    pytest.fail("a paid call was made while frozen")

        with pytest.raises(cost.BudgetExhausted):
            cost.metered_call(_Client(), model="claude-haiku-4-5", est_usd=0.001,
                              purpose="test", max_tokens=10,
                              messages=[{"role": "user", "content": "hi"}])

    def test_freeze_outranks_available_budget(self, db, app, admin_user, monkeypatch):
        """Money left in the month must not unfreeze anything."""
        monkeypatch.setenv("ANTHROPIC_MONTHLY_CAP_USD", "250")
        cost.set_frozen(True)
        assert cost.check_budget(0.01) is False

    def test_spend_mode_reports_frozen(self, db, app, admin_user, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_MONTHLY_CAP_USD", "250")
        cost.set_frozen(True)
        assert cost.spend_mode() == "frozen"

    def test_enrichment_refuses(self, db, app, admin_user, monkeypatch):
        from app.services import enrichment
        cost.set_frozen(True)
        monkeypatch.setattr(enrichment, "_get_client",
                            lambda: pytest.fail("scored while frozen"))
        out = enrichment.enrich_signal({"company_name": "Frozen Co"})
        assert out["enriched"] is False


class TestFreezeStopsJobs:
    def test_every_scheduled_job_skips(self, db, app, admin_user):
        """All seven jobs call _acquire_job_lock first, so one gate covers them."""
        from app.services import scheduler
        cost.set_frozen(True)
        for job in ("daily_scan", "weekly_digest", "press_monitor",
                    "watchlist_headline_sweep", "founder_news_monitor",
                    "quarterly_linkedin_poll_reminder",
                    "quarterly_founder_radar_poll_reminder"):
            assert scheduler._acquire_job_lock(app, job) is False, job

    def test_jobs_run_again_once_thawed(self, db, app, admin_user):
        from app.services import scheduler
        cost.set_frozen(True)
        assert scheduler._acquire_job_lock(app, "daily_scan") is False
        cost.set_frozen(False)
        assert scheduler._acquire_job_lock(app, "daily_scan") is True


class TestFreezeStopsManualScans:
    def test_manual_scan_routes_refuse(self, client, db, app, admin_user, admin_token):
        cost.set_frozen(True)
        r = client.post("/api/scans/trademark", json={},
                        headers={"Authorization": f"Bearer {admin_token}"})
        assert r.status_code == 409
        assert r.get_json()["error"] == "frozen"

    def test_reads_still_work_while_frozen(self, client, db, app, admin_user, admin_token):
        """Everything is still there — a freeze stops spending, not looking."""
        cost.set_frozen(True)
        r = client.get("/api/admin/corpus-stats",
                       headers={"Authorization": f"Bearer {admin_token}"})
        assert r.status_code == 200


class TestFreezeEndpoint:
    def test_toggles_and_reports(self, client, db, app, admin_user, admin_token):
        h = {"Authorization": f"Bearer {admin_token}"}
        assert client.get("/api/admin/freeze", headers=h).get_json()["frozen"] is False
        r = client.post("/api/admin/freeze", json={"frozen": True, "reason": "review"}, headers=h)
        assert r.status_code == 200 and r.get_json()["frozen"] is True
        assert client.get("/api/admin/freeze", headers=h).get_json()["frozen"] is True
        assert client.post("/api/admin/freeze", json={"frozen": False},
                           headers=h).get_json()["frozen"] is False

    def test_rejects_a_body_without_the_flag(self, client, db, app, admin_user, admin_token):
        r = client.post("/api/admin/freeze", json={"reason": "oops"},
                        headers={"Authorization": f"Bearer {admin_token}"})
        assert r.status_code == 400

    def test_requires_admin(self, client, db, user_token):
        assert client.post("/api/admin/freeze", json={"frozen": True},
                           headers={"Authorization": f"Bearer {user_token}"}).status_code in (401, 403)
