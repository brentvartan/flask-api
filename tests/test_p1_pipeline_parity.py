"""
P1 — scan-path parity.

The regression this file exists for: app/api/scans/routes.py (manual scans) and
app/services/scheduler.py (nightly scans) independently implemented the same
post-save work and silently diverged. The nightly scan — the one that has to be
airtight — ended up doing strictly LESS than a human clicking "Run Scan": no
trademark-assignment resolution, no conviction/alumni text matching, no Form D
related-persons extraction, no acquisition detection, and no 'owner' in the
enrichment prompt.

Both paths now call app/services/signal_pipeline.py::process_saved_signal.

Coverage here:
  1. The same raw signal, saved by the manual route and by the scheduled scan,
     produces EQUIVALENT enriched metadata and an identical enrichment payload.
  2. signal_types / signal_count actually reach enrich_signal (they gate the tier
     floors and the confluence prompt boost, and reached it from neither path before).
  3. A conviction founder named in a USPTO trademark OWNER field is matched on the
     scheduled path.
  4. exit_alumni_match survives to the top level and stays separate from conviction.
  5. The confluence alert is score-gated and fires only AFTER enrichment.
  6. A failing stage is recorded but does not abort the rest of the pipeline.

No live network and no paid API calls: enrich_signal, the USPTO assignment lookup,
the EDGAR related-persons fetch, and every email/alert sender are mocked.
"""
import json
from pathlib import Path

import pytest

from app.models.item import Item
from app.models.scheduled_scan import ScheduledScan
from app.services import signal_pipeline

# A real conviction-list founder and a real exit-alumni operator, so the matchers
# run against data/watchlist_seed.csv rather than a fixture of themselves.
CONVICTION_NAME = "Joey Zwillinger"
ALUMNI_NAME     = "Mike Bufano"

RAW_TRADEMARK = {
    "companyName":      "PARITYFORGE",
    "signal_type":      "trademark",
    "category":         "Wellness",
    "score_boost":      18,
    "is_intent_to_use": True,
    "description":      "PARITYFORGE — Class 005 — Filed Jul 01, 2026 · pre-launch intent-to-use",
    "url":              "https://tmsearch.uspto.gov/search/search-results?searchInput=PARITYFORGE",
    "owner":            CONVICTION_NAME,
    "notes":            f"Owner: {CONVICTION_NAME}. Nutritional supplements and functional beverages.",
    "timestamp":        "2026-07-01T00:00:00+00:00",
}

RAW_FORM_D = {
    "companyName": "PARITYFORGE LABS INC",
    "signal_type": "delaware",
    "category":    "Wellness",
    "score_boost": 20,
    "description": "PARITYFORGE LABS INC — Form D — $2,000,000 offering",
    "url":         "https://www.sec.gov/Archives/edgar/data/1/x.htm",
    "notes":       "Reg D 506(b) offering",
    "timestamp":   "2026-07-02T00:00:00+00:00",
    "_adsh":       "0001234567-26-000123",
    "_cik":        "1234567",
}

# Keys that carry the pipeline's work. Everything else in meta is collector
# bookkeeping (fp, score_boost, url) and legitimately differs per source.
_PARITY_KEYS = (
    "owner",
    "conviction_match",
    "exit_alumni_match",
    "stealth_entity",
    "resolved_owner",
    "related_persons",
    "enrichment",
)


# ── Helpers ───────────────────────────────────────────────────────────────────

class _SyncThread:
    """Runs the target inline so background side effects are deterministic."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args   = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class _SyncThreading:
    Thread = _SyncThread


def _parity_view(meta: dict) -> dict:
    """
    The subset of a signal's metadata that the shared pipeline is responsible for.

    Absent and empty collapse to a single sentinel: the collector whitelists
    legitimately differ on whether they pre-seed an empty "related_persons": []
    (Form D bookkeeping) versus omitting the key, and every consumer reads it as
    `meta.get(...) or []`. A path that actually FOUND something the other missed
    still fails this comparison, which is the divergence the test is for.
    """
    return {
        k: (None if meta.get(k) in (None, [], {}, "") else meta.get(k))
        for k in _PARITY_KEYS
    }


def _signal_metas(db):
    """Every signal item's parsed metadata, freshest read."""
    db.session.expire_all()
    rows = Item.query.filter_by(item_type="signal").all()
    return [json.loads(r.description or "{}") for r in rows]


def _clear_signals(db):
    """Wipe signals + confluence state so the second path starts from the same slate."""
    from app.models.confluence_hit import ConfluenceHit
    from app.models.signal_event import SignalEvent

    SignalEvent.query.delete()
    ConfluenceHit.query.delete()
    Item.query.filter_by(item_type="signal").delete()
    db.session.commit()


@pytest.fixture
def enrich_calls(monkeypatch):
    """Capture every enrich_signal payload; return a fixed WARM enrichment."""
    calls = []

    def _fake_enrich(signal):
        calls.append(dict(signal))
        return {
            "enriched":        True,
            "bullish_score":   62,
            "watch_level":     "warm",
            "one_line_thesis": "Functional wellness brand from a proven operator.",
            "cultural_theme":  "Ubiquitous Wellness",
            "founder":         {"name": None, "confidence": "unknown"},
            "founder_score":   {"total": None},
        }

    import app.services.enrichment as enrichment_mod
    monkeypatch.setattr(enrichment_mod, "enrich_signal", _fake_enrich)
    return calls


@pytest.fixture
def no_network(monkeypatch):
    """Neutralise every outbound call the pipeline can make."""
    import app.services.delaware as delaware_mod
    import app.services.email as email_mod
    import app.services.exit_watch as exit_watch_mod
    import app.services.founder_enrichment as founder_mod
    import app.services.trademark_assignments as assign_mod
    import app.services.watchlist as watchlist_mod

    sent = {"watchlist_alerts": [], "founder_jobs": [], "watchlist_adds": [], "exit_brands": []}

    monkeypatch.setattr(assign_mod, "resolve_brand_name", lambda **kw: {
        "resolved_name": None, "stealth_entity": None,
        "serial_number": None, "has_assignment": False,
    })
    monkeypatch.setattr(delaware_mod, "fetch_form_d_related_persons", lambda adsh, cik: [])
    monkeypatch.setattr(exit_watch_mod, "add_exit_brand",
                        lambda brand, **kw: sent["exit_brands"].append(brand))
    monkeypatch.setattr(email_mod, "send_watchlist_match_alert",
                        lambda *a, **kw: sent["watchlist_alerts"].append(kw))
    monkeypatch.setattr(founder_mod, "run_founder_enrichment_in_background",
                        lambda **kw: sent["founder_jobs"].append(kw))
    monkeypatch.setattr(watchlist_mod, "auto_add_to_watchlist",
                        lambda *a, **kw: sent["watchlist_adds"].append(a))
    monkeypatch.setattr(signal_pipeline, "threading", _SyncThreading)
    return sent


@pytest.fixture
def sync_spawn(monkeypatch):
    """Make the manual route's background spawn run inline."""
    import app.api.scans.routes as scans_routes
    monkeypatch.setattr(scans_routes, "_spawn", lambda fn, *args: fn(*args))
    return scans_routes


def _save_signal(db, owner_id, raw, extra=None):
    """Persist one signal item directly, as both scan paths do before the pipeline."""
    meta = {
        "_type":        "signal",
        "fp":           f"fp-{raw['companyName']}-{raw['signal_type']}",
        "company_name": raw["companyName"],
        "signal_type":  raw["signal_type"],
        "category":     raw.get("category", ""),
        "description":  raw.get("description", ""),
        "url":          raw.get("url", ""),
        "notes":        raw.get("notes", ""),
        "timestamp":    raw["timestamp"],
    }
    for key in ("owner", "_adsh", "_cik"):
        if raw.get(key):
            meta[key] = raw[key]
    meta.update(extra or {})
    item = Item(
        title=raw["companyName"],
        owner_id=owner_id,
        item_type="signal",
        description=json.dumps(meta, separators=(",", ":")),
    )
    db.session.add(item)
    db.session.commit()
    return item


# ── 1. The headline parity test ───────────────────────────────────────────────

def test_manual_and_scheduled_paths_produce_equivalent_meta(
    app, db, client, regular_user, user_token, enrich_calls, no_network, sync_spawn, monkeypatch,
):
    """
    The regression that caused P1: the same USPTO filing, run through a manual
    scan and through the nightly scan, must come out identical.
    """
    import app.services.trademarks as trademarks_mod

    collector_result = {
        "signals": [dict(RAW_TRADEMARK)], "total_found": 1, "fetched": 1, "error": None,
    }
    monkeypatch.setattr(sync_spawn, "search_recent_trademarks",
                        lambda **kw: dict(collector_result))
    monkeypatch.setattr(trademarks_mod, "search_recent_trademarks",
                        lambda **kw: dict(collector_result))

    # ── Manual path: POST /api/scans/trademark ────────────────────────────────
    resp = client.post(
        "/api/scans/trademark",
        json={"days_back": 7, "max_results": 10},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["new_saved"] == 1

    manual_metas = _signal_metas(db)
    assert len(manual_metas) == 1
    manual_view = _parity_view(manual_metas[0])
    manual_enrich_payload = dict(enrich_calls[-1])

    _clear_signals(db)
    enrich_calls.clear()

    # ── Scheduled path: run_scan_now ──────────────────────────────────────────
    from app.services.scheduler import run_scan_now

    scan = ScheduledScan(
        owner_id=regular_user.id, name="parity", days_back=7,
        max_results=10, frequency="daily", enabled=True, scan_type="trademark",
    )
    db.session.add(scan)
    db.session.commit()

    result = run_scan_now(scan, regular_user.id)
    assert result["new_saved"] == 1

    scheduled_metas = _signal_metas(db)
    assert len(scheduled_metas) == 1
    scheduled_view = _parity_view(scheduled_metas[0])
    scheduled_enrich_payload = dict(enrich_calls[-1])

    assert manual_view == scheduled_view, (
        "Manual and scheduled scans produced different enriched metadata for the "
        "same filing. The two paths have diverged again — fix signal_pipeline.py, "
        "not one caller."
    )
    assert manual_enrich_payload == scheduled_enrich_payload, (
        "The two paths sent different payloads to enrich_signal."
    )
    # And the work actually happened, rather than both paths doing nothing.
    assert manual_view["conviction_match"]["name"] == CONVICTION_NAME
    assert manual_view["enrichment"]["bullish_score"] == 62


# ── 2. Confluence context reaches the scorer ──────────────────────────────────

def test_signal_types_and_count_reach_enrich_signal(
    db, regular_user, enrich_calls, no_network,
):
    """
    enrichment.py's tier floors read signal["signal_types"] and its SIGNAL
    CONFLUENCE prompt boost reads signal_count >= 2. Neither scan path passed
    them before P1, so both were dead code at scan time.
    """
    first = _save_signal(db, regular_user.id, RAW_TRADEMARK)
    signal_pipeline.process_saved_signal(first.id, owner_id=regular_user.id, alert_emails=[])

    payload = enrich_calls[-1]
    assert payload["signal_types"] == ["trademark"]
    assert payload["signal_count"] == 1

    # A second, different signal type for the same brand → confluence context.
    second = _save_signal(db, regular_user.id, RAW_FORM_D)
    signal_pipeline.process_saved_signal(second.id, owner_id=regular_user.id, alert_emails=[])

    payload = enrich_calls[-1]
    assert payload["signal_count"] == 2
    assert set(payload["signal_types"]) == {"trademark", "delaware"}


def test_owner_is_passed_to_enrich_signal(db, regular_user, enrich_calls, no_network):
    """'owner' unlocks the FOUNDER RESEARCH PRIORITY block; the scheduled path never sent it."""
    item = _save_signal(db, regular_user.id, RAW_TRADEMARK)
    signal_pipeline.process_saved_signal(item.id, owner_id=regular_user.id, alert_emails=[])
    assert enrich_calls[-1]["owner"] == CONVICTION_NAME


# ── 3. Conviction in a trademark OWNER field ──────────────────────────────────

def test_conviction_in_trademark_owner_is_matched_on_scheduled_path(
    app, db, regular_user, enrich_calls, no_network, monkeypatch,
):
    """
    The concrete miss P1 fixes: a conviction founder named in a USPTO owner field
    was caught by a manual scan and missed by the nightly one.
    """
    import app.services.trademarks as trademarks_mod
    from app.services.scheduler import run_scan_now

    monkeypatch.setattr(trademarks_mod, "search_recent_trademarks", lambda **kw: {
        "signals": [dict(RAW_TRADEMARK)], "total_found": 1, "fetched": 1, "error": None,
    })

    scan = ScheduledScan(
        owner_id=regular_user.id, name="conviction", days_back=7,
        max_results=10, frequency="daily", enabled=True, scan_type="trademark",
    )
    db.session.add(scan)
    db.session.commit()
    run_scan_now(scan, regular_user.id)

    meta = _signal_metas(db)[0]
    assert meta["owner"] == CONVICTION_NAME
    assert meta["conviction_match"]["name"] == CONVICTION_NAME
    assert meta["conviction_match"]["designation"] == "founder"
    # Passed into the prompt so the scorer sees the founder context.
    assert enrich_calls[-1]["conviction_match"]["name"] == CONVICTION_NAME


def test_scheduled_path_resolves_trademark_assignments(
    app, db, regular_user, enrich_calls, no_network, monkeypatch,
):
    """
    The Lightyear → Filament case. Assignment resolution ran on manual scans only;
    nightly scans never resolved a stealth entity to its operating company.
    """
    import app.services.trademark_assignments as assign_mod
    import app.services.trademarks as trademarks_mod
    from app.services.scheduler import run_scan_now

    monkeypatch.setattr(assign_mod, "resolve_brand_name", lambda **kw: {
        "resolved_name":  "Filament Sciences",
        "stealth_entity": kw["original_owner"],
        "serial_number":  "97123456",
        "has_assignment": True,
    })
    monkeypatch.setattr(trademarks_mod, "search_recent_trademarks", lambda **kw: {
        "signals": [dict(RAW_TRADEMARK)], "total_found": 1, "fetched": 1, "error": None,
    })

    scan = ScheduledScan(
        owner_id=regular_user.id, name="assign", days_back=7,
        max_results=10, frequency="daily", enabled=True, scan_type="trademark",
    )
    db.session.add(scan)
    db.session.commit()
    run_scan_now(scan, regular_user.id)

    meta = _signal_metas(db)[0]
    assert meta["resolved_owner"] == "Filament Sciences"
    assert meta["stealth_entity"] == CONVICTION_NAME
    # The resolved entity — not the stealth filer — is what gets scored.
    assert enrich_calls[-1]["companyName"] == "Filament Sciences"


def test_acquisition_detection_grows_the_exit_watch_list(
    db, regular_user, enrich_calls, no_network, monkeypatch,
):
    """Press/newswire acquisitions must feed exit_watch on BOTH paths."""
    import app.services.exit_watch as exit_watch_mod
    monkeypatch.setattr(exit_watch_mod, "detect_acquisition_in_text", lambda text: "Mirror")

    raw = dict(RAW_TRADEMARK, signal_type="newswire", companyName="PARITYWIRE")
    item = _save_signal(db, regular_user.id, raw)
    signal_pipeline.process_saved_signal(item.id, owner_id=regular_user.id, alert_emails=[])

    assert no_network["exit_brands"] == ["Mirror"]


def test_scheduled_save_persists_pipeline_inputs(
    app, db, regular_user, enrich_calls, no_network, monkeypatch,
):
    """
    Whitelist regression. The scheduled path's hardcoded json.dumps whitelist
    dropped owner / _adsh / _cik / exit_alumni_match, so Form D related-persons
    extraction could never run nightly and the ALUMNI alert branch was unreachable.
    """
    import app.services.delaware as delaware_mod
    from app.services.scheduler import run_scan_now

    monkeypatch.setattr(delaware_mod, "search_recent_delaware_entities", lambda **kw: {
        "signals": [dict(RAW_FORM_D)], "total_found": 1, "fetched": 1, "error": None,
    })

    scan = ScheduledScan(
        owner_id=regular_user.id, name="formd", days_back=7,
        max_results=10, frequency="daily", enabled=True, scan_type="delaware",
    )
    db.session.add(scan)
    db.session.commit()
    run_scan_now(scan, regular_user.id)

    meta = _signal_metas(db)[0]
    assert meta["_adsh"] == RAW_FORM_D["_adsh"]
    assert meta["_cik"] == RAW_FORM_D["_cik"]
    assert "owner" in meta
    assert "exit_alumni_match" in meta


# ── 4. Alumni stays a separate designation and survives to the top level ──────

def test_exit_alumni_match_survives_to_top_level(db, regular_user, enrich_calls, no_network):
    raw = dict(RAW_TRADEMARK, owner=ALUMNI_NAME,
               notes=f"Owner: {ALUMNI_NAME}. Nutritional supplements.")
    item = _save_signal(db, regular_user.id, raw)

    outcome = signal_pipeline.process_saved_signal(
        item.id, owner_id=regular_user.id, alert_emails=[],
    )
    meta = _signal_metas(db)[0]

    assert meta["exit_alumni_match"]["name"] == ALUMNI_NAME
    assert meta["exit_alumni_match"]["designation"] == "alumni"
    assert meta.get("conviction_match") is None
    assert outcome["exit_alumni_match"]["name"] == ALUMNI_NAME


def test_conviction_wins_and_alumni_is_not_also_set(db, regular_user, enrich_calls, no_network):
    """Founder and alumni designations never collapse into one another."""
    item = _save_signal(db, regular_user.id, RAW_TRADEMARK)
    signal_pipeline.process_saved_signal(item.id, owner_id=regular_user.id, alert_emails=[])

    meta = _signal_metas(db)[0]
    assert meta["conviction_match"]["designation"] == "founder"
    assert meta.get("exit_alumni_match") is None


def test_watchlist_match_alert_fires_for_alumni(db, regular_user, enrich_calls, no_network):
    """The ALUMNI branch of the immediate watchlist alert must be reachable."""
    raw = dict(RAW_TRADEMARK, owner=ALUMNI_NAME,
               notes=f"Owner: {ALUMNI_NAME}. Nutritional supplements.")
    item = _save_signal(db, regular_user.id, raw)

    signal_pipeline.process_saved_signal(
        item.id, owner_id=regular_user.id, alert_emails=["brent@bullish.co"],
    )
    alerts = no_network["watchlist_alerts"]
    assert len(alerts) == 1
    assert alerts[0]["match_type"] == "ALUMNI"
    assert alerts[0]["person_name"] == ALUMNI_NAME
    # Enrichment ran first, so the alert carries a score rather than None.
    assert alerts[0]["brand_score"] == 62


# ── 5. Form D related persons ─────────────────────────────────────────────────

def test_form_d_related_person_conviction_is_promoted(
    db, regular_user, enrich_calls, no_network, monkeypatch,
):
    """A conviction founder named as a Form D officer becomes the signal's match."""
    import app.services.delaware as delaware_mod
    monkeypatch.setattr(delaware_mod, "fetch_form_d_related_persons", lambda adsh, cik: [
        {"name": "Unknown Person", "relationships": ["Director"]},
        {"name": CONVICTION_NAME, "relationships": ["Executive Officer"]},
    ])

    item = _save_signal(db, regular_user.id, RAW_FORM_D)
    signal_pipeline.process_saved_signal(item.id, owner_id=regular_user.id, alert_emails=[])

    meta = _signal_metas(db)[0]
    assert len(meta["related_persons"]) == 2
    assert meta["conviction_match"]["name"] == CONVICTION_NAME


def test_form_d_extraction_skipped_without_filing_ids(
    db, regular_user, enrich_calls, no_network, monkeypatch,
):
    """No _adsh/_cik → no EDGAR call at all (this is what the old whitelist caused)."""
    import app.services.delaware as delaware_mod
    called = []
    monkeypatch.setattr(delaware_mod, "fetch_form_d_related_persons",
                        lambda adsh, cik: called.append((adsh, cik)) or [])

    raw = {k: v for k, v in RAW_FORM_D.items() if k not in ("_adsh", "_cik")}
    item = _save_signal(db, regular_user.id, raw)
    signal_pipeline.process_saved_signal(item.id, owner_id=regular_user.id, alert_emails=[])

    assert called == []


# ── 6. Confluence alert: score-gated, post-enrichment ─────────────────────────

def _run_confluence_pair(db, owner_id, monkeypatch, score, watch_level):
    """
    Save a trademark then a Form D for the same brand, run both through the
    pipeline, and return (call_order, alerts_sent).
    """
    import app.services.confluence as confluence_mod
    import app.services.enrichment as enrichment_mod

    order, alerts = [], []

    real_record = confluence_mod.record_signal_and_check_confluence

    def _spy_record(**kwargs):
        order.append("confluence")
        return real_record(**kwargs)

    def _fake_enrich(signal):
        order.append("enrich")
        return {
            "enriched": True, "bullish_score": score, "watch_level": watch_level,
            "one_line_thesis": "t", "cultural_theme": "c",
            "founder": {"name": None, "confidence": "unknown"},
            "founder_score": {"total": None},
        }

    def _fake_send(hit_id, emails):
        order.append("alert")
        alerts.append(hit_id)
        return True

    monkeypatch.setattr(confluence_mod, "record_signal_and_check_confluence", _spy_record)
    monkeypatch.setattr(confluence_mod, "send_confluence_alert_for_hit", _fake_send)
    monkeypatch.setattr(enrichment_mod, "enrich_signal", _fake_enrich)

    first  = _save_signal(db, owner_id, RAW_TRADEMARK)
    second = _save_signal(db, owner_id, RAW_FORM_D)
    signal_pipeline.process_saved_signal(first.id, owner_id=owner_id,
                                         alert_emails=["brent@bullish.co"])
    order.clear()
    outcome = signal_pipeline.process_saved_signal(second.id, owner_id=owner_id,
                                                   alert_emails=["brent@bullish.co"])
    return order, alerts, outcome


def test_confluence_alert_sent_after_enrichment_when_scored_above_gate(
    db, regular_user, no_network, monkeypatch,
):
    order, alerts, outcome = _run_confluence_pair(db, regular_user.id, monkeypatch, 80, "hot")

    assert outcome["confluence"]["is_confluence"] is True
    assert alerts, "confluence alert should have been sent for a score above the gate"
    assert order == ["confluence", "enrich", "alert"], (
        "Confluence must be RECORDED before enrichment (so signal_types reach the "
        "scorer) and ALERTED after it (so the email carries a real score)."
    )


def test_confluence_alert_suppressed_below_gate(db, regular_user, no_network, monkeypatch):
    """The PACIFIC PAPER CUPS case: real confluence, no investment relevance."""
    order, alerts, outcome = _run_confluence_pair(db, regular_user.id, monkeypatch, 12, "cold")

    assert outcome["confluence"]["is_confluence"] is True
    assert alerts == []
    assert order == ["confluence", "enrich"]


def test_confluence_hit_is_backfilled_with_the_score(db, regular_user, no_network, monkeypatch):
    """Confluence runs pre-enrichment, so the hit row must be scored afterwards."""
    from app.models.confluence_hit import ConfluenceHit

    _order, _alerts, outcome = _run_confluence_pair(db, regular_user.id, monkeypatch, 80, "hot")

    db.session.expire_all()
    hit = db.session.get(ConfluenceHit, outcome["confluence"]["hit_id"])
    assert hit.bullish_score == 80
    assert hit.watch_level == "hot"


# ── 7. Stage isolation ────────────────────────────────────────────────────────

def test_failing_stage_does_not_abort_the_pipeline(
    db, regular_user, enrich_calls, no_network, monkeypatch,
):
    """A blown assignment lookup must not cost us the enrichment."""
    import app.services.trademark_assignments as assign_mod

    def _boom(**kwargs):
        raise RuntimeError("USPTO assignment service down")

    monkeypatch.setattr(assign_mod, "resolve_brand_name", _boom)

    item = _save_signal(db, regular_user.id, RAW_TRADEMARK)
    outcome = signal_pipeline.process_saved_signal(
        item.id, owner_id=regular_user.id, alert_emails=[],
    )

    assert any("trademark_assignment" in e for e in outcome["errors"])
    assert outcome["enriched"] is True
    assert outcome["conviction_match"]["name"] == CONVICTION_NAME


def test_missing_item_is_reported_not_raised(db, regular_user, enrich_calls, no_network):
    outcome = signal_pipeline.process_saved_signal(999999, owner_id=regular_user.id,
                                                   alert_emails=[])
    assert outcome["enriched"] is False
    assert outcome["errors"]


# ── 8. Founder-enrichment trigger reconciliation ──────────────────────────────

def test_newswire_uses_the_lower_founder_threshold():
    assert signal_pipeline.founder_enrichment_threshold("newswire") == 50
    assert signal_pipeline.founder_enrichment_threshold("trademark") == 70


def test_hot_triggers_founder_enrichment_even_below_the_numeric_floor():
    """A conviction tier-floor can force HOT at a low score — Job 2 must still fire."""
    enrichment = {"enriched": True, "watch_level": "hot", "bullish_score": 40}
    assert signal_pipeline.should_run_founder_enrichment(enrichment, "trademark") is True


def test_warm_newswire_above_fifty_triggers_founder_enrichment():
    enrichment = {"enriched": True, "watch_level": "warm", "bullish_score": 55}
    assert signal_pipeline.should_run_founder_enrichment(enrichment, "newswire") is True
    assert signal_pipeline.should_run_founder_enrichment(enrichment, "trademark") is False


def test_unenriched_signal_never_triggers_founder_enrichment():
    assert signal_pipeline.should_run_founder_enrichment({"enriched": False}, "newswire") is False


# ── 9. Drift guards ───────────────────────────────────────────────────────────

_APP = Path(__file__).resolve().parent.parent / "app"
_MANUAL_PATH    = _APP / "api" / "scans" / "routes.py"
_SCHEDULED_PATH = _APP / "services" / "scheduler.py"
_PIPELINE_PATH  = _APP / "services" / "signal_pipeline.py"


@pytest.mark.parametrize("path,label", [
    (_MANUAL_PATH,    "manual scan path"),
    (_SCHEDULED_PATH, "scheduled scan path"),
])
def test_both_scan_paths_call_the_shared_pipeline(path, label):
    source = path.read_text()
    assert "process_saved_signal" in source, (
        f"{label} ({path.name}) no longer calls process_saved_signal. Both scan "
        "paths must run the same post-save pipeline — they have diverged three times."
    )


@pytest.mark.parametrize("path,label", [
    (_MANUAL_PATH,    "manual scan path"),
    (_SCHEDULED_PATH, "scheduled scan path"),
])
def test_scan_paths_do_not_re_inline_enrichment(path, label):
    """Neither caller may call enrich_signal directly again."""
    source = path.read_text()
    assert "enrich_signal(" not in source, (
        f"{label} ({path.name}) calls enrich_signal directly. Enrichment belongs "
        "to signal_pipeline.process_saved_signal so both paths score identically."
    )


def test_pipeline_owns_the_confluence_gate():
    assert "should_send_confluence_alert" in _PIPELINE_PATH.read_text()
