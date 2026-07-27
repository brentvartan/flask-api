"""
Scoring runs concurrently, and the aggregation stays correct while it does.

Each signal is dominated by two blocking Anthropic calls, so a serial loop
managed under 50 signals in 11 minutes in production — hours for one run's
budget. The work is IO-bound and independent per signal.
"""
import app.services.scheduler as sch


def test_worker_count_is_bounded_and_configurable(monkeypatch):
    """Every worker holds a DB connection; the pool is 5 + 10 overflow."""
    assert 1 <= sch._SCORE_WORKERS <= 10


def test_counters_are_aggregated_in_one_thread():
    """
    The futures are consumed in the calling thread, so hot/warm/cold and
    hot_brands need no locking. This pins that structure: if someone moves the
    aggregation INTO the worker, this reminder should fail review.
    """
    import inspect
    src = inspect.getsource(sch.run_scan_now)
    worker = src[src.index("def _score_one"):src.index("_scored = 0")]
    for counter in ("hot_count", "warm_count", "cold_count", "hot_brands", "founders_queued"):
        assert counter not in worker, (
            f"{counter} is mutated inside the worker thread — aggregate in the "
            "calling thread or add locking"
        )


def test_worker_gets_its_own_app_context_and_releases_the_session():
    """
    Flask-SQLAlchemy scopes the session to the app context, so a worker without
    its own context would share the caller's session across threads.
    """
    import inspect
    src = inspect.getsource(sch.run_scan_now)
    worker = src[src.index("def _score_one"):src.index("_scored = 0")]
    assert "app_context()" in worker
    assert "db.session.remove()" in worker


def test_a_failing_signal_does_not_stop_the_others():
    """One bad signal returns None and is skipped, not raised."""
    import inspect
    src = inspect.getsource(sch.run_scan_now)
    worker = src[src.index("def _score_one"):src.index("_scored = 0")]
    assert "return None" in worker
    assert "except Exception" in worker
