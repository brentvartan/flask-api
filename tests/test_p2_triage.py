"""
P2 — two-stage enrichment (cheap triage gate in front of expensive thesis scoring).

The problem this exists for: measured against live USPTO, ~1,047 consumer-class
trademark candidates a day pass the app's own filters, and the nightly scan could
only afford to score the newest ~200 (the query sorts filedDate DESC). Everything
below that horizon was never revisited — on the product's LONGEST-LEAD signal.
Separately ~88% of what did get scored came back COLD, so most of the Sonnet spend
proved the obvious.

Stage 1 (enrichment.triage_signal, claude-haiku-4-5) is a GATE, not an analyst.
Stage 2 (enrichment.enrich_signal, claude-sonnet-4-6) is unchanged.

The invariant under test is asymmetric and load-bearing: a false positive costs
one scoring call, a false negative means a HOT brand is never seen by anyone. So
the gate must fail OPEN on every failure path, must be bypassed entirely for the
highest-value signals, must be switchable off with one env var, and a triaged-out
signal must remain a coherent, visible, re-scorable row.

Coverage here:
  1. Triage rejects an obvious B2B record.
  2. Triage passes a plausible consumer brand.
  3. Every failure path FAILS OPEN — API error, unparseable JSON, non-boolean
     verdict, missing API key — and never raises.
  4. A conviction match bypasses triage entirely.
  5. An exit-alumni match bypasses triage entirely.
  6. A brand confluence has seen in 2+ signal types bypasses triage entirely.
  7. ENABLE_TRIAGE=0 restores full scoring for everything.
  8. A triaged-out signal is persisted coherently, stays visible, breaks no
     downstream stage, and is still reachable by the re-score escape hatches.

No live network and no paid API calls: the Anthropic client, enrich_signal, the
USPTO assignment lookup, the EDGAR fetch and every alert sender are mocked.
"""
import json
from types import SimpleNamespace

import pytest

from app.models.item import Item
from app.services import enrichment as enrichment_mod
from app.services import signal_pipeline

# Real entries from data/watchlist_seed.csv, so the matchers run against the
# authoritative list rather than a fixture of themselves.
CONVICTION_NAME = "Joey Zwillinger"
ALUMNI_NAME     = "Mike Bufano"

B2B_SIGNAL = {
    "companyName": "DATAFORGE PIPELINE",
    "category":    "Other Technology",
    "signal_type": "trademark",
    "description": "DATAFORGE PIPELINE — Class 042 — Filed Jul 01, 2026",
    "notes":       "Owner: Dataforge Systems LLC. Software as a service featuring "
                   "software for enterprise data pipeline orchestration and ETL "
                   "monitoring for developer teams.",
    "owner":       "Dataforge Systems LLC",
}

CONSUMER_SIGNAL = {
    "companyName": "MOONWATER",
    "category":    "Beverage",
    "signal_type": "trademark",
    "description": "MOONWATER — Class 032 — Filed Jul 01, 2026 · pre-launch intent-to-use",
    "notes":       "Owner: Moonwater Beverage Co. Non-alcoholic sparkling beverages "
                   "containing adaptogens; functional drinks.",
    "owner":       "Moonwater Beverage Co",
}


# ── Fake Anthropic client ─────────────────────────────────────────────────────

class _FakeMessages:
    """Records every create() call and defers the response to a behaviour fn."""

    def __init__(self, behaviour):
        self._behaviour = behaviour
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._behaviour(kwargs)


class _FakeClient:
    def __init__(self, behaviour):
        self.messages = _FakeMessages(behaviour)


def _reply(text):
    """A response object shaped like the SDK's, carrying `text`."""
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        usage=SimpleNamespace(input_tokens=310, output_tokens=38),
    )


@pytest.fixture
def fake_anthropic(monkeypatch):
    """
    Install a fake Anthropic client for triage. Returns a setter:
        client = fake_anthropic(lambda kwargs: _reply('{"worth_scoring": true}'))
    The behaviour fn may also raise, to simulate an API failure.
    """
    holder = {}

    def _install(behaviour):
        client = _FakeClient(behaviour)
        holder["client"] = client
        monkeypatch.setattr(enrichment_mod, "_get_client", lambda: client)
        return client

    return _install


# ── 1-2. The gate makes the obvious calls ─────────────────────────────────────

def test_triage_rejects_obvious_b2b(fake_anthropic):
    """An enterprise data-pipeline SaaS filing is exactly what Stage 1 is for."""
    client = fake_anthropic(lambda kw: _reply(json.dumps({
        "worth_scoring": False,
        "reason":        "enterprise data pipeline SaaS sold to developer teams",
        "confidence":    "high",
    })))

    verdict = enrichment_mod.triage_signal(B2B_SIGNAL)

    assert verdict["worth_scoring"] is False
    assert verdict["triaged"] is True
    assert verdict["confidence"] == "high"
    assert verdict["reason"]
    assert "error" not in verdict

    # The gate must be cheap: Haiku, a small output cap, and the goods text
    # with the "Owner: NAME." prefix already stripped.
    call = client.messages.calls[0]
    assert call["model"] == "claude-haiku-4-5"
    assert call["max_tokens"] <= 200
    assert "Software as a service" in call["messages"][0]["content"]
    assert "Goods & Services: Owner:" not in call["messages"][0]["content"]


def test_triage_passes_plausible_consumer_brand(fake_anthropic):
    """A functional beverage is a consumer brand — it must reach Stage 2."""
    fake_anthropic(lambda kw: _reply(json.dumps({
        "worth_scoring": True,
        "reason":        "non-alcoholic functional beverage sold to consumers",
        "confidence":    "high",
    })))

    verdict = enrichment_mod.triage_signal(CONSUMER_SIGNAL)

    assert verdict["worth_scoring"] is True
    assert verdict["triaged"] is True


def test_triage_strips_markdown_fences(fake_anthropic):
    """Same fence robustness enrich_signal has — a fenced reject still rejects."""
    fake_anthropic(lambda kw: _reply(
        '```json\n{"worth_scoring": false, "reason": "B2B", "confidence": "high"}\n```'
    ))
    assert enrichment_mod.triage_signal(B2B_SIGNAL)["worth_scoring"] is False


# ── 3. Every failure path fails OPEN ──────────────────────────────────────────

def _raise(exc):
    def _behaviour(kw):
        raise exc
    return _behaviour


@pytest.mark.parametrize("behaviour, label", [
    (_raise(RuntimeError("connection reset by peer")), "api error"),
    (_raise(TimeoutError("read timeout")),             "timeout"),
    (lambda kw: _reply("I think this one is probably fine?"), "unparseable json"),
    (lambda kw: _reply('{"reason": "no verdict field"}'),     "missing verdict"),
    (lambda kw: _reply('{"worth_scoring": "maybe"}'),         "non-boolean verdict"),
    (lambda kw: _reply('{"worth_scoring": null}'),            "null verdict"),
    (lambda kw: SimpleNamespace(content=[], usage=None),      "empty content"),
])
def test_triage_fails_open(fake_anthropic, behaviour, label):
    """
    A triage outage must NEVER silently shrink coverage. Every failure mode
    returns worth_scoring=True, marks triaged=False so an audit can tell the two
    apart, and does not raise.
    """
    fake_anthropic(behaviour)

    verdict = enrichment_mod.triage_signal(B2B_SIGNAL)

    assert verdict["worth_scoring"] is True, f"{label} did not fail open"
    assert verdict["triaged"] is False, f"{label} was recorded as a real verdict"
    assert verdict.get("error"), f"{label} recorded no error"


def test_triage_fails_open_without_api_key(monkeypatch):
    """No ANTHROPIC_API_KEY is a config failure, not a reason to drop a brand."""
    def _no_key():
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")

    monkeypatch.setattr(enrichment_mod, "_get_client", _no_key)

    verdict = enrichment_mod.triage_signal(B2B_SIGNAL)
    assert verdict["worth_scoring"] is True
    assert verdict["triaged"] is False


def test_triage_coerces_string_booleans(fake_anthropic):
    """A stringified boolean is still an unambiguous verdict — honour it."""
    fake_anthropic(lambda kw: _reply('{"worth_scoring": "false", "reason": "B2B SaaS"}'))
    verdict = enrichment_mod.triage_signal(B2B_SIGNAL)
    assert verdict["worth_scoring"] is False
    assert verdict["triaged"] is True
    assert verdict["confidence"] == "medium"   # absent → normalised, not dropped


# ── Bypass + kill-switch unit coverage ────────────────────────────────────────

def test_bypass_reasons():
    """The three signals that must never be gated by a cheap model."""
    solo = {"signal_count": 1, "signal_types": ["trademark"]}

    assert signal_pipeline.triage_bypass_reason({"name": CONVICTION_NAME}, None, solo) \
        == "conviction_match"
    assert signal_pipeline.triage_bypass_reason(None, {"name": ALUMNI_NAME}, solo) \
        == "exit_alumni_match"
    assert signal_pipeline.triage_bypass_reason(
        None, None, {"signal_count": 2, "signal_types": ["trademark", "delaware"]}
    ) == "multi_signal"
    # A malformed count must not silently disable the multi-signal bypass when
    # the type list itself proves triangulation.
    assert signal_pipeline.triage_bypass_reason(
        None, None, {"signal_count": None, "signal_types": ["trademark", "delaware"]}
    ) == "multi_signal"
    assert signal_pipeline.triage_bypass_reason(None, None, solo) is None


@pytest.mark.parametrize("value, expected", [
    (None,     True),
    ("1",      True),
    ("true",   True),
    ("",       True),      # set-but-empty is not an explicit "off"
    ("0",      False),
    ("false",  False),
    ("FALSE",  False),
    ("off",    False),
    ("no",     False),
])
def test_kill_switch_parsing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("ENABLE_TRIAGE", raising=False)
    else:
        monkeypatch.setenv("ENABLE_TRIAGE", value)
    assert signal_pipeline.triage_enabled() is expected


# ── Pipeline integration ──────────────────────────────────────────────────────

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


@pytest.fixture
def pipeline_env(monkeypatch):
    """
    Neutralise every outbound call the pipeline can make, and spy on both stages.

    Returns a dict with:
        triage_calls / enrich_calls  — payloads each stage received
        triage_verdict               — mutable; what the fake gate returns
        sent                         — captured alerts / background jobs
    """
    import app.services.delaware as delaware_mod
    import app.services.email as email_mod
    import app.services.exit_watch as exit_watch_mod
    import app.services.founder_enrichment as founder_mod
    import app.services.trademark_assignments as assign_mod
    import app.services.watchlist as watchlist_mod

    state = {
        "triage_calls":   [],
        "enrich_calls":   [],
        "triage_verdict": {
            "worth_scoring": False,
            "reason":        "enterprise data pipeline SaaS",
            "confidence":    "high",
            "triaged":       True,
            "model":         "claude-haiku-4-5",
        },
        "sent": {"watchlist_alerts": [], "confluence_alerts": [],
                 "founder_jobs": [], "watchlist_adds": [], "exit_brands": []},
    }

    def _fake_triage(signal):
        state["triage_calls"].append(dict(signal))
        return dict(state["triage_verdict"])

    def _fake_enrich(signal):
        state["enrich_calls"].append(dict(signal))
        return {
            "enriched":        True,
            "bullish_score":   62,
            "watch_level":     "warm",
            "one_line_thesis": "Functional wellness brand from a proven operator.",
            "cultural_theme":  "Ubiquitous Wellness",
            "founder":         {"name": None, "confidence": "unknown"},
            "founder_score":   {"total": None},
        }

    monkeypatch.setattr(enrichment_mod, "triage_signal", _fake_triage)
    monkeypatch.setattr(enrichment_mod, "enrich_signal", _fake_enrich)

    monkeypatch.setattr(assign_mod, "resolve_brand_name", lambda **kw: {
        "resolved_name": None, "stealth_entity": None,
        "serial_number": None, "has_assignment": False,
    })
    monkeypatch.setattr(delaware_mod, "fetch_form_d_related_persons", lambda adsh, cik: [])
    monkeypatch.setattr(exit_watch_mod, "add_exit_brand",
                        lambda brand, **kw: state["sent"]["exit_brands"].append(brand))
    monkeypatch.setattr(email_mod, "send_watchlist_match_alert",
                        lambda *a, **kw: state["sent"]["watchlist_alerts"].append(kw))
    monkeypatch.setattr(founder_mod, "run_founder_enrichment_in_background",
                        lambda **kw: state["sent"]["founder_jobs"].append(kw))
    monkeypatch.setattr(watchlist_mod, "auto_add_to_watchlist",
                        lambda *a, **kw: state["sent"]["watchlist_adds"].append(a))
    monkeypatch.setattr(signal_pipeline, "threading", _SyncThreading)

    import app.services.confluence as confluence_mod
    monkeypatch.setattr(confluence_mod, "send_confluence_alert_for_hit",
                        lambda hit_id, emails: state["sent"]["confluence_alerts"].append(hit_id))

    monkeypatch.delenv("ENABLE_TRIAGE", raising=False)
    return state


def _save_signal(db, owner_id, raw, extra=None):
    """Persist one signal item exactly as both scan paths do before the pipeline."""
    meta = {
        "_type":        "signal",
        "fp":           f"fp-{raw['companyName']}-{raw['signal_type']}",
        "company_name": raw["companyName"],
        "signal_type":  raw["signal_type"],
        "category":     raw.get("category", ""),
        "description":  raw.get("description", ""),
        "url":          raw.get("url", ""),
        "notes":        raw.get("notes", ""),
        "timestamp":    "2026-07-01T00:00:00+00:00",
    }
    if raw.get("owner"):
        meta["owner"] = raw["owner"]
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


def _meta_of(db, item_id):
    db.session.expire_all()
    return json.loads(db.session.get(Item, item_id).description or "{}")


# ── 8. A triaged-out signal stays a coherent, visible, recoverable row ────────

def test_triaged_out_signal_is_persisted_coherently_and_breaks_nothing(
    db, regular_user, pipeline_env,
):
    item = _save_signal(db, regular_user.id, B2B_SIGNAL)

    outcome = signal_pipeline.process_saved_signal(item.id, alert_emails=["ops@bullish.co"])

    # Stage 2 was skipped — that is the whole point.
    assert len(pipeline_env["triage_calls"]) == 1
    assert pipeline_env["enrich_calls"] == []

    # The outcome shape is the one every caller already handles.
    assert outcome["triage_skipped"] is True
    assert outcome["enriched"] is False
    assert outcome["watch_level"] is None
    assert outcome["bullish_score"] is None
    assert outcome["errors"] == [], f"triage path degraded the pipeline: {outcome['errors']}"

    # Nothing downstream fired on an unscored signal.
    assert pipeline_env["sent"]["confluence_alerts"] == []
    assert pipeline_env["sent"]["founder_jobs"] == []
    assert pipeline_env["sent"]["watchlist_adds"] == []
    assert outcome["founder_queued"] is False
    assert outcome["watchlist_queued"] is False

    meta = _meta_of(db, item.id)

    # The decision is recorded compactly and legibly.
    assert meta["triage"]["worth_scoring"] is False
    assert meta["triage"]["reason"]
    assert meta["triage"]["confidence"] == "high"
    assert meta["triage"]["triaged"] is True
    assert meta["triage"]["at"]

    # The row is UN-ENRICHED, not hidden. It is still a signal, still visible
    # through /api/items, and still carries its collector metadata.
    assert "enrichment" not in meta
    assert meta["_type"] == "signal"
    assert meta["company_name"] == B2B_SIGNAL["companyName"]

    row = db.session.get(Item, item.id)
    assert row is not None
    assert row.item_type == "signal"

    # Downstream consumers key off the literal '"enrichment"' substring
    # (weekly digest) or the absence of it (the re-score escape hatch). Both
    # must behave, or a triaged-out brand either emails or becomes unreachable.
    assert '"enrichment"' not in row.description
    assert '"enriched":true' not in row.description

    unenriched = (
        Item.query
        .filter(Item.item_type == "signal")
        .filter(~Item.description.contains('"enrichment"'))
        .all()
    )
    assert item.id in [r.id for r in unenriched], (
        "A triaged-out signal must remain reachable by /api/enrich unenriched_only "
        "and `flask re-enrich` — otherwise a wrong gate call is unrecoverable."
    )


def test_weekly_digest_skips_a_triaged_out_signal(db, regular_user, pipeline_env):
    """
    The digest reads meta['enrichment'] and filters on enriched. A triaged-out
    signal has neither, so it must simply not appear — never as a phantom COLD.
    """
    item = _save_signal(db, regular_user.id, B2B_SIGNAL)
    signal_pipeline.process_saved_signal(item.id, alert_emails=["ops@bullish.co"])

    meta = _meta_of(db, item.id)
    assert (meta.get("enrichment") or {}).get("enriched") is not True


def test_triage_passing_reaches_full_scoring(db, regular_user, pipeline_env):
    """The pass path is unchanged behaviour: Stage 1 yes → Stage 2 runs."""
    pipeline_env["triage_verdict"] = {
        "worth_scoring": True, "reason": "functional beverage",
        "confidence": "high", "triaged": True, "model": "claude-haiku-4-5",
    }
    item = _save_signal(db, regular_user.id, CONSUMER_SIGNAL)

    outcome = signal_pipeline.process_saved_signal(item.id, alert_emails=["ops@bullish.co"])

    assert len(pipeline_env["triage_calls"]) == 1
    assert len(pipeline_env["enrich_calls"]) == 1
    assert outcome["triage_skipped"] is False
    assert outcome["enriched"] is True
    assert outcome["bullish_score"] == 62

    meta = _meta_of(db, item.id)
    assert meta["enrichment"]["enriched"] is True
    assert meta["triage"]["worth_scoring"] is True

    # Both stages saw the same record — no drift between gate and scorer.
    assert pipeline_env["triage_calls"][0] == pipeline_env["enrich_calls"][0]


# ── 4-6. Bypasses: the signals that must never be gated ───────────────────────

def test_conviction_match_bypasses_triage(db, regular_user, pipeline_env):
    """A conviction-list founder in the trademark owner field is Job 2 itself."""
    raw = dict(CONSUMER_SIGNAL)
    raw["owner"] = CONVICTION_NAME
    raw["notes"] = f"Owner: {CONVICTION_NAME}. Non-alcoholic sparkling beverages."
    item = _save_signal(db, regular_user.id, raw)

    outcome = signal_pipeline.process_saved_signal(item.id, alert_emails=["ops@bullish.co"])

    assert outcome["conviction_match"], "fixture no longer matches the seed list"
    assert pipeline_env["triage_calls"] == [], (
        "A conviction founder was put through the cheap gate. Conviction signals "
        "must always reach the full thesis scorer."
    )
    assert len(pipeline_env["enrich_calls"]) == 1
    assert outcome["enriched"] is True
    assert outcome["triage"] is None
    assert outcome["triage_skipped"] is False


def test_exit_alumni_match_bypasses_triage(db, regular_user, pipeline_env):
    """Exit alumni are the other half of Job 2 and get the same protection."""
    raw = dict(CONSUMER_SIGNAL)
    raw["owner"] = ALUMNI_NAME
    raw["notes"] = f"Owner: {ALUMNI_NAME}. Non-alcoholic sparkling beverages."
    item = _save_signal(db, regular_user.id, raw)

    outcome = signal_pipeline.process_saved_signal(item.id, alert_emails=["ops@bullish.co"])

    assert outcome["exit_alumni_match"], "fixture no longer matches the seed list"
    assert outcome["conviction_match"] is None, "founder and alumni must stay separate"
    assert pipeline_env["triage_calls"] == []
    assert len(pipeline_env["enrich_calls"]) == 1


def test_multi_signal_brand_bypasses_triage(db, regular_user, pipeline_env):
    """
    Triangulation is the core edge. Once confluence has seen a brand in a second
    distinct signal type, it is real by definition — never gate it.

    The first (single-signal) filing IS triaged, which also proves the bypass is
    driven by confluence state rather than by the brand name.
    """
    tm = _save_signal(db, regular_user.id, B2B_SIGNAL)
    signal_pipeline.process_saved_signal(tm.id, alert_emails=["ops@bullish.co"])
    assert len(pipeline_env["triage_calls"]) == 1, "the first, solo signal should be triaged"

    form_d = _save_signal(db, regular_user.id, {
        "companyName": B2B_SIGNAL["companyName"],
        "category":    "Other Technology",
        "signal_type": "delaware",
        "description": f"{B2B_SIGNAL['companyName']} INC — Form D — $2,000,000 offering",
        "notes":       "Reg D 506(b) offering",
    })
    outcome = signal_pipeline.process_saved_signal(form_d.id, alert_emails=["ops@bullish.co"])

    assert outcome["confluence"]["signal_count"] >= 2, "confluence did not triangulate"
    assert len(pipeline_env["triage_calls"]) == 1, (
        "A brand seen in 2+ distinct signal types was put through the cheap gate. "
        "Confluence is the core edge — triangulated brands always get full scoring."
    )
    assert len(pipeline_env["enrich_calls"]) == 1
    assert outcome["enriched"] is True


# ── 7. The kill switch ────────────────────────────────────────────────────────

def test_kill_switch_restores_full_scoring_for_everything(
    db, regular_user, pipeline_env, monkeypatch,
):
    """
    ENABLE_TRIAGE=0 must restore exactly the pre-P2 behaviour: no gate call, full
    Sonnet scoring on every saved signal — including one the gate would reject.
    """
    monkeypatch.setenv("ENABLE_TRIAGE", "0")
    item = _save_signal(db, regular_user.id, B2B_SIGNAL)

    outcome = signal_pipeline.process_saved_signal(item.id, alert_emails=["ops@bullish.co"])

    assert pipeline_env["triage_calls"] == [], "kill switch did not disable Stage 1"
    assert len(pipeline_env["enrich_calls"]) == 1
    assert outcome["enriched"] is True
    assert outcome["triage_skipped"] is False
    assert outcome["triage"] is None

    meta = _meta_of(db, item.id)
    assert "triage" not in meta
    assert meta["enrichment"]["enriched"] is True


# ── A raising gate must still not cost coverage ───────────────────────────────

def test_pipeline_scores_anyway_when_triage_raises(db, regular_user, pipeline_env, monkeypatch):
    """
    triage_signal is written never to raise. If it somehow does, the pipeline
    records the stage failure and scores the signal regardless — a broken gate
    must never be able to make a brand invisible.
    """
    def _boom(signal):
        raise RuntimeError("haiku exploded")

    monkeypatch.setattr(enrichment_mod, "triage_signal", _boom)
    item = _save_signal(db, regular_user.id, B2B_SIGNAL)

    outcome = signal_pipeline.process_saved_signal(item.id, alert_emails=["ops@bullish.co"])

    assert any("triage" in e for e in outcome["errors"])
    assert len(pipeline_env["enrich_calls"]) == 1
    assert outcome["enriched"] is True
    assert outcome["triage_skipped"] is False
