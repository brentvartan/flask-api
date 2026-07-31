"""
One big Monday email (Brent, 2026-07-30) — per-event confluence and watchlist
alerts defer into the weekly digest instead of sending immediately.

The queue is ConfluenceHit.alert_sent=False plus the matches already persisted
on signals; the digest drains it. Nothing is dropped, only batched — and
"realtime" in __bullish_settings__ restores the old per-event behaviour.
"""
import json
from datetime import datetime, timezone

import pytest

from app.models.confluence_hit import ConfluenceHit
from app.models.item import Item
from app.services.signal_pipeline import alert_delivery_mode


def _settings_row(db, admin_user, extra=None):
    payload = {"_type": "settings", "alert_emails": ["team@bullish.co"]}
    payload.update(extra or {})
    row = Item.query.filter_by(title="__bullish_settings__").first()
    if row:
        row.description = json.dumps(payload)
    else:
        row = Item(title="__bullish_settings__", item_type="settings",
                   owner_id=admin_user.id, description=json.dumps(payload))
        db.session.add(row)
    db.session.commit()
    return row


class TestDeliveryMode:
    def test_default_is_weekly(self, db):
        assert alert_delivery_mode() == "weekly"

    def test_realtime_opt_in(self, db, admin_user):
        _settings_row(db, admin_user, {"alert_delivery": "realtime"})
        assert alert_delivery_mode() == "realtime"

    def test_garbage_value_falls_back_to_weekly(self, db, admin_user):
        _settings_row(db, admin_user, {"alert_delivery": "sometimes?"})
        assert alert_delivery_mode() == "weekly"


class TestPipelineDefersInWeeklyMode:
    def _run_signal(self, db, admin_user, monkeypatch, brand, sig_type, sent_log):
        """Save a minimal signal and run it through the pipeline with all paid
        and outbound calls stubbed."""
        import app.services.email as email_mod
        import app.services.confluence as conf_mod
        import app.services.enrichment as enr_mod

        monkeypatch.setattr(enr_mod, "triage_signal",
                            lambda payload: {"worth_scoring": True, "triaged": True})
        monkeypatch.setattr(enr_mod, "enrich_signal", lambda payload: {
            "enriched": True, "bullish_score": 80, "watch_level": "hot",
            "one_line_thesis": "t", "cultural_theme": "c", "founder": {},
        })
        monkeypatch.setattr(conf_mod, "send_confluence_alert_for_hit",
                            lambda hit_id, emails: sent_log.append(("confluence", hit_id)))
        monkeypatch.setattr(email_mod, "send_watchlist_match_alert",
                            lambda *a, **kw: sent_log.append(("watchlist", kw.get("person_name"))))
        # keep HOT handling quiet
        import app.services.founder_enrichment as fe_mod
        monkeypatch.setattr(fe_mod, "run_founder_enrichment_in_background",
                            lambda **kw: None)
        import app.services.watchlist as wl_mod
        monkeypatch.setattr(wl_mod, "auto_add_to_watchlist", lambda *a, **kw: None)

        item = Item(title=brand, owner_id=admin_user.id, item_type="signal",
                    description=json.dumps({
                        "_type": "signal", "company_name": brand,
                        "signal_type": sig_type, "category": "Beauty",
                        "description": "d", "notes": "", "url": "u",
                        "timestamp": "2026-07-29T00:00:00",
                    }))
        db.session.add(item)
        db.session.commit()

        from app.services.signal_pipeline import process_saved_signal
        return process_saved_signal(item.id, owner_id=admin_user.id,
                                    alert_emails=["team@bullish.co"])

    def test_confluence_email_is_deferred_not_sent(self, db, admin_user, monkeypatch):
        sent = []
        self._run_signal(db, admin_user, monkeypatch, "GLOWBAR", "trademark", sent)
        out = self._run_signal(db, admin_user, monkeypatch, "GLOWBAR", "delaware", sent)

        assert out["confluence"]["is_confluence"] is True
        hit = ConfluenceHit.query.first()
        assert hit is not None
        assert hit.alert_sent is False, "the unsent hit IS the Monday queue"
        assert ("confluence", hit.id) not in sent, "weekly mode must not email per event"

    def test_watchlist_email_is_deferred_but_match_persists(self, db, admin_user, monkeypatch):
        sent = []
        import app.services.conviction as conv_mod
        monkeypatch.setattr(conv_mod, "check_conviction_match_multi",
                            lambda fields: {"name": "Lance Collins", "reason": "r",
                                            "known_brands": []})
        self._run_signal(db, admin_user, monkeypatch, "ZEN BEVERAGE", "delaware", sent)

        assert not any(k == "watchlist" for k, _ in sent), "deferred, not sent"
        row = Item.query.filter(Item.title == "ZEN BEVERAGE").first()
        meta = json.loads(row.description)
        assert meta.get("conviction_match", {}).get("name") == "Lance Collins", \
            "the match itself must persist — that is what the digest reads"

    def test_realtime_mode_still_sends_per_event(self, db, admin_user, monkeypatch):
        _settings_row(db, admin_user, {"alert_delivery": "realtime"})
        sent = []
        self._run_signal(db, admin_user, monkeypatch, "OLIPOP", "trademark", sent)
        self._run_signal(db, admin_user, monkeypatch, "OLIPOP", "delaware", sent)
        assert any(k == "confluence" for k, _ in sent), \
            "realtime opt-in must restore the old behaviour"


class TestDigestCarriesTheQueue:
    def test_digest_includes_and_drains_deferred_hits(self, db, admin_user, monkeypatch):
        _settings_row(db, admin_user)
        monkeypatch.setenv("ALERT_EMAILS", "team@bullish.co")

        hit = ConfluenceHit(owner_id=admin_user.id, brand_key="glowbar",
                            brand_name="GLOWBAR", signal_count=2,
                            signal_types=json.dumps(["trademark", "delaware"]),
                            bullish_score=74, watch_level="hot", alert_sent=False)
        db.session.add(hit)
        sig = Item(title="ZEN BEVERAGE", owner_id=admin_user.id, item_type="signal",
                   description=json.dumps({
                       "_type": "signal", "company_name": "ZEN BEVERAGE",
                       "signal_type": "delaware",
                       "conviction_match": {"name": "Lance Collins"},
                   }))
        db.session.add(sig)
        db.session.commit()

        captured = {}
        import app.services.email as email_mod
        def _capture(addr, hot, warm, label, confluence_hits=None, people_matches=None):
            captured.update(confluence=confluence_hits, people=people_matches)
        monkeypatch.setattr(email_mod, "send_weekly_digest_email", _capture)

        from flask import current_app
        from app.services.scheduler import _send_weekly_digest
        _send_weekly_digest(current_app._get_current_object())

        assert captured.get("confluence"), "digest must carry the deferred confluence hits"
        assert captured["confluence"][0]["brand"] == "GLOWBAR"
        assert captured.get("people"), "digest must carry the watchlist people"
        assert captured["people"][0]["person"] == "Lance Collins"

        db.session.expire_all()
        assert ConfluenceHit.query.first().alert_sent is True, \
            "a delivered hit leaves the queue"

    def test_digest_not_skipped_when_only_deferred_items_exist(self, db, admin_user, monkeypatch):
        """No new HOT/WARM brands, but a queued hit — the digest must still go."""
        _settings_row(db, admin_user)
        monkeypatch.setenv("ALERT_EMAILS", "team@bullish.co")
        db.session.add(ConfluenceHit(
            owner_id=admin_user.id, brand_key="mosh", brand_name="MOSH",
            signal_count=2, signal_types=json.dumps(["trademark", "newswire"]),
            bullish_score=None, watch_level=None, alert_sent=False))
        db.session.commit()

        sent = {"n": 0}
        import app.services.email as email_mod
        monkeypatch.setattr(email_mod, "send_weekly_digest_email",
                            lambda *a, **kw: sent.update(n=sent["n"] + 1))

        from flask import current_app
        from app.services.scheduler import _send_weekly_digest
        _send_weekly_digest(current_app._get_current_object())
        assert sent["n"] > 0
