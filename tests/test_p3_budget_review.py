"""
The elevated scoring budget must review itself.

Raised 2,000 -> 3,000 on 2026-08-03 for one month, to drain a backlog that could
not otherwise clear: runs were saving ~2,230 new signals a night against a 2,000
budget, so new arrivals ate the whole budget and 5,814 never-assessed signals
(29% of the corpus) were unreachable AND growing. Unassessed means invisible to
conviction matching, confluence and alerts — not merely unscored.

The reminder rides the Monday digest rather than living in a doc, because a
reminder nobody reads is not a reminder.
"""
from datetime import date, timedelta

import app.services.scheduler as sch


def test_budget_is_raised_and_drains_the_backlog():
    """Surplus per run must exceed zero or the backlog never clears."""
    assert sch._DEFAULT_ENRICH_BUDGET == 3000
    typical_new_per_run = 2230          # measured 2026-07-28
    assert sch._DEFAULT_ENRICH_BUDGET > typical_new_per_run, (
        "budget must exceed daily intake or the backlog can only grow"
    )


def test_review_date_is_a_month_after_the_raise():
    # Brent capped the run at two weeks on 2026-08-04; the budget trial now
    # ends with the cap rather than on its original one-month date.
    assert (sch._RUN_CAP_ON - sch._RUN_CAP_SET_ON).days == 14


def test_review_not_due_before_the_date(monkeypatch):
    class _Before(date):
        @classmethod
        def today(cls):
            return sch._RUN_CAP_ON - timedelta(days=1)
    monkeypatch.setattr(sch, "_date", _Before)
    assert sch._budget_review_due() is False


def test_review_due_on_and_after_the_date(monkeypatch):
    class _After(date):
        @classmethod
        def today(cls):
            return sch._RUN_CAP_ON + timedelta(days=3)
    monkeypatch.setattr(sch, "_date", _After)
    assert sch._budget_review_due() is True


def test_review_stops_nagging_once_the_budget_drops_back(monkeypatch):
    """Decision made = reminder silent, without needing anyone to delete it."""
    class _After(date):
        @classmethod
        def today(cls):
            return sch._RUN_CAP_ON + timedelta(days=30)
    monkeypatch.setattr(sch, "_date", _After)
    monkeypatch.setenv("SCAN_ENRICH_BUDGET", str(sch._BUDGET_TRIAL_FROM))
    assert sch._budget_review_due() is False


def test_env_override_still_wins(monkeypatch):
    monkeypatch.setenv("SCAN_ENRICH_BUDGET", "1234")
    assert sch._enrich_budget() == 1234


class TestRunCap:
    """The cap is a decision point, not a shutdown. Scans keep running; the
    digest just stops leading with brands and starts leading with the question."""

    def test_cap_is_two_weeks_from_when_it_was_set(self):
        from app.services import scheduler as sch
        assert (sch._RUN_CAP_ON - sch._RUN_CAP_SET_ON).days == 14

    def test_not_reached_before_the_date(self, monkeypatch):
        from datetime import date, timedelta
        from app.services import scheduler as sch

        class _D(date):
            @classmethod
            def today(cls):
                return sch._RUN_CAP_ON - timedelta(days=1)
        monkeypatch.setattr(sch, "_date", _D)
        assert sch._run_cap_reached() is False

    def test_reached_on_and_after_the_date(self, monkeypatch):
        from datetime import date, timedelta
        from app.services import scheduler as sch

        for offset in (0, 1, 90):
            class _D(date):
                _o = offset
                @classmethod
                def today(cls):
                    return sch._RUN_CAP_ON + timedelta(days=cls._o)
            monkeypatch.setattr(sch, "_date", _D)
            assert sch._run_cap_reached() is True, f"offset {offset}"

    def test_banner_keeps_appearing_until_answered(self, monkeypatch):
        """Deliberate: an unanswered cap should nag, not lapse quietly."""
        from datetime import date, timedelta
        from app.services import scheduler as sch

        class _D(date):
            @classmethod
            def today(cls):
                return sch._RUN_CAP_ON + timedelta(days=200)
        monkeypatch.setattr(sch, "_date", _D)
        assert sch._run_cap_reached() is True
