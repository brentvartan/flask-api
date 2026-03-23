"""
SignalEvent — records every individual signal detected for a brand.
Used to build a timeline and detect confluence across signal types.
"""
from datetime import datetime, timezone
from ..extensions import db


class SignalEvent(db.Model):
    __tablename__ = "signal_events"

    id          = db.Column(db.Integer, primary_key=True)
    item_id     = db.Column(db.Integer, db.ForeignKey("items.id", ondelete="SET NULL"), nullable=True, index=True)
    owner_id    = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    brand_key   = db.Column(db.String(255), nullable=False, index=True)   # normalized slug for matching
    brand_name  = db.Column(db.String(255), nullable=False)               # display name
    signal_type = db.Column(db.String(50),  nullable=False)               # trademark|delaware|domain|producthunt
    source_url  = db.Column(db.Text, nullable=True)
    detected_at = db.Column(db.DateTime(timezone=True), nullable=False,
                            default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "id":          self.id,
            "item_id":     self.item_id,
            "brand_key":   self.brand_key,
            "brand_name":  self.brand_name,
            "signal_type": self.signal_type,
            "source_url":  self.source_url,
            "detected_at": self.detected_at.isoformat(),
        }

    def __repr__(self):
        return f"<SignalEvent {self.brand_key} / {self.signal_type}>"
