"""
Every caller of enrich_signal must use the shared payload builder.

This is the dual-path divergence trap (CLAUDE.md), on its FOURTH recurrence.
enrichment.py reads `signal_types` for its tier floors and `signal_count` for
the multi-signal prompt boost; a caller that hand-rolls the payload and omits
them silently disables both. The two /api/enrich routes were doing exactly that
— shipped working on the scan paths, dead on the on-demand ones.

The control is the same pattern the repo already uses for the confluence score
gate: one shared function plus a grep-based drift alarm.
"""
import inspect
import re
from pathlib import Path

import pytest

from app.services.signal_pipeline import build_scoring_payload

_APP = Path(__file__).resolve().parent.parent / "app"
_CALLERS = [
    _APP / "services" / "signal_pipeline.py",
    _APP / "api" / "enrich" / "routes.py",
]

# The keys enrichment.py actually reads. If this list grows, the builder is the
# single place that must learn about it.
_REQUIRED = {
    "companyName", "category", "signal_type", "description", "notes",
    "owner", "conviction_match", "signal_types", "signal_count",
}


def test_builder_emits_every_key_enrichment_reads():
    payload = build_scoring_payload({"signal_type": "trademark"}, "ACME")
    assert set(payload) == _REQUIRED


def test_builder_defaults_are_safe_for_a_bare_signal():
    """A signal with almost no meta must still produce a usable payload."""
    p = build_scoring_payload({}, "ACME")
    assert p["companyName"] == "ACME"
    assert p["signal_types"] == ["trademark"]
    assert p["signal_count"] == 1
    assert p["conviction_match"] is None


def test_builder_prefers_the_resolved_stealth_entity():
    """Lightyear -> Filament: the resolved operating entity is the brand."""
    p = build_scoring_payload(
        {"company_name": "Lightyear Inc", "resolved_owner": "Filament Sciences"},
        "Lightyear Inc",
    )
    assert p["companyName"] == "Filament Sciences"


@pytest.mark.parametrize("path", _CALLERS, ids=lambda p: p.name)
def test_no_caller_hand_rolls_the_payload(path):
    """
    enrich_signal({...}) with a dict literal is the divergence. It must be
    enrich_signal(build_scoring_payload(...)).
    """
    src = path.read_text()
    bad = re.findall(r"enrich_signal\(\s*\{", src)
    assert not bad, (
        f"{path.name} builds an enrich_signal payload inline. Use "
        f"build_scoring_payload() — see the dual-path divergence trap in CLAUDE.md."
    )


@pytest.mark.parametrize("path", _CALLERS, ids=lambda p: p.name)
def test_callers_actually_pass_signal_types(path):
    """A caller that never mentions signal_types cannot be honouring the floors."""
    src = path.read_text()
    if "enrich_signal(" not in src:
        pytest.skip("not an enrichment caller")
    assert "signal_types" in src, (
        f"{path.name} calls enrich_signal but never supplies signal_types — "
        "the tier floors are dead on that path."
    )


def test_signal_types_lookup_is_resilient(db, admin_user):
    """A brand with no recorded events must not break an on-demand enrich."""
    from app.services.signal_pipeline import signal_types_for_brand
    types, count = signal_types_for_brand(admin_user.id, "NEVER SEEN BRAND")
    assert types == [] and count == 1
