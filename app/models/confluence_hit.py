"""
ConfluenceHit — records when a brand crosses a multi-signal threshold.
A new record is created each time the distinct signal_type count increases
(1→2, 2→3, etc.), triggering an alert email.
"""
import json
from datetime import datetime, timezone
from ..extensions import db


class ConfluenceHit(db.Model):
    __tablename__ = "confluence_hits"

    id            = db.Column(db.Integer, primary_key=True)
    owner_id      = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    brand_key     = db.Column(db.String(255), nullable=False, index=True)
    brand_name    = db.Column(db.String(255), nullable=False)
    signal_count  = db.Column(db.Integer,     nullable=False)   # how many distinct signal types now
    signal_types  = db.Column(db.Text,        nullable=False)   # JSON array e.g. '["trademark","delaware"]'
    bullish_score = db.Column(db.Integer,     nullable=True)    # score at time of confluence (if enriched)
    watch_level   = db.Column(db.String(20),  nullable=True)    # hot|warm|cold
    alert_sent    = db.Column(db.Boolean,     nullable=False, default=False)
    alert_sent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at    = db.Column(db.DateTime(timezone=True), nullable=False,
                              default=lambda: datetime.now(timezone.utc))

    def get_signal_types(self) -> list:
        try:
            return json.loads(self.signal_types or "[]")
        except Exception:
            return []

    def to_dict(self):
        return {
            "id":            self.id,
            "brand_key":     self.brand_key,
            "brand_name":    self.brand_name,
            "signal_count":  self.signal_count,
            "signal_types":  self.get_signal_types(),
            "bullish_score": self.bullish_score,
            "watch_level":   self.watch_level,
            "alert_sent":    self.alert_sent,
            "created_at":    self.created_at.isoformat(),
        }

    def __repr__(self):
        return f"<ConfluenceHit {self.brand_key} {self.signal_count} signals>"
