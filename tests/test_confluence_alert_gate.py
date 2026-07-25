"""
Confluence alert gating — the score threshold that decides whether a confluence
hit interrupts someone by email.

Background (2026-07-25): this gate was believed shipped on 2026-07-22, but was live
on the SCHEDULED-scan path only. The MANUAL-scan path ran enrichment and confluence
as parallel threads, so confluence always won the race and emailed before any score
existed — flooding the operator's inbox with COLD brands (paper cups, BOLI accounts,
etc.). An earlier divergence between the same two files (independent hardcoded
json.dumps field whitelists) had the identical shape.

These tests exist to make that class of bug loud:
  1. The gate itself behaves correctly, including on unscored signals.
  2. NEITHER scan path re-inlines its own threshold — both must call the shared
     helper, so there is only one implementation that can drift.
"""
import re
from pathlib import Path

import pytest

from app.services.confluence import (
    CONFLUENCE_ALERT_MIN_SCORE,
    should_send_confluence_alert,
)

_APP = Path(__file__).resolve().parent.parent / "app"
_MANUAL_PATH    = _APP / "api" / "scans" / "routes.py"
_SCHEDULED_PATH = _APP / "services" / "scheduler.py"


# ── The gate itself ───────────────────────────────────────────────────────────

def test_hot_brand_alerts():
    assert should_send_confluence_alert(85) is True


def test_warm_brand_at_threshold_alerts():
    assert should_send_confluence_alert(CONFLUENCE_ALERT_MIN_SCORE) is True


def test_cold_brand_does_not_alert():
    """The PACIFIC PAPER CUPS case — real confluence, no investment relevance."""
    assert should_send_confluence_alert(12) is False


def test_just_below_threshold_does_not_alert():
    assert should_send_confluence_alert(CONFLUENCE_ALERT_MIN_SCORE - 1) is False


def test_unscored_signal_does_not_alert():
    """
    The original bug. Enrichment hadn't run (or failed), so the score was None —
    and the old scheduler check `if _conf_score is None or _conf_score >= 50`
    treated unknown as sendable. Unknown must mean silent.
    """
    assert should_send_confluence_alert(None) is False


def test_zero_score_does_not_alert():
    """0 is a real score (a failed thesis gate), not a missing one — still no email."""
    assert should_send_confluence_alert(0) is False


def test_float_scores_handled():
    assert should_send_confluence_alert(49.9) is False
    assert should_send_confluence_alert(50.1) is True


def test_string_score_from_json_roundtrip_handled():
    """Scores survive a json.dumps/loads trip through Item.description as strings."""
    assert should_send_confluence_alert("75") is True
    assert should_send_confluence_alert("30") is False


def test_garbage_score_does_not_alert():
    """Malformed enrichment output must fail closed, not spam."""
    assert should_send_confluence_alert("n/a") is False
    assert should_send_confluence_alert({}) is False


# ── Divergence guard ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,label", [
    (_MANUAL_PATH,    "manual scan path"),
    (_SCHEDULED_PATH, "scheduled scan path"),
])
def test_scan_path_uses_shared_gate(path, label):
    """Both paths must call the shared helper, not their own comparison."""
    source = path.read_text()
    assert "should_send_confluence_alert" in source, (
        f"{label} ({path.name}) no longer calls should_send_confluence_alert. "
        "Both scan paths must share one gate — they have silently diverged twice before."
    )


@pytest.mark.parametrize("path,label", [
    (_MANUAL_PATH,    "manual scan path"),
    (_SCHEDULED_PATH, "scheduled scan path"),
])
def test_scan_path_does_not_inline_threshold(path, label):
    """
    Catch a re-inlined threshold near confluence logic. This is what regressed
    before: someone adds `if score >= 50` locally and the two paths drift apart
    again without any test noticing.
    """
    for line in path.read_text().splitlines():
        code = line.split("#", 1)[0]  # prose about the threshold is fine; code is not
        if "_conf_score" not in code and "bullish_score" not in code:
            continue
        if re.search(r'(>=|>|<|<=)\s*\d+', code):
            pytest.fail(
                f"{label} ({path.name}) inlines a numeric score comparison:\n"
                f"    {line.strip()}\n"
                "Use should_send_confluence_alert() so both scan paths stay in lockstep."
            )


def test_threshold_is_defined_once():
    """The literal 50 should live in exactly one place."""
    assert CONFLUENCE_ALERT_MIN_SCORE == 50
    for path in (_MANUAL_PATH, _SCHEDULED_PATH):
        assert "CONFLUENCE_ALERT_MIN_SCORE = " not in path.read_text(), (
            f"{path.name} redefines the threshold instead of importing it."
        )
