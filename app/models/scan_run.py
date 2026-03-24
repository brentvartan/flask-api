from datetime import datetime, timezone
from ..extensions import db


class ScanRun(db.Model):
    __tablename__ = "scan_runs"

    id               = db.Column(db.Integer, primary_key=True)
    scan_id          = db.Column(db.Integer, db.ForeignKey("scheduled_scans.id", ondelete="CASCADE"), nullable=False, index=True)
    owner_id         = db.Column(db.Integer, nullable=False)
    ran_at           = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    new_saved        = db.Column(db.Integer, default=0)
    hot_found        = db.Column(db.Integer, default=0)
    warm_found       = db.Column(db.Integer, default=0)
    cold_found       = db.Column(db.Integer, default=0)
    founders_queued  = db.Column(db.Integer, default=0)   # HOT brands that had founder enrichment triggered
    alert_sent       = db.Column(db.Boolean, default=False)
    alert_emails     = db.Column(db.String(500), nullable=True)   # comma-separated
    sources_ran      = db.Column(db.String(100), nullable=True)   # e.g. "trademark,delaware"
    error_message    = db.Column(db.String(500), nullable=True)   # partial failure note

    def to_dict(self):
        return {
            "id":              self.id,
            "scan_id":         self.scan_id,
            "ran_at":          self.ran_at.isoformat() if self.ran_at else None,
            "new_saved":       self.new_saved,
            "hot_found":       self.hot_found,
            "warm_found":      self.warm_found,
            "cold_found":      self.cold_found,
            "founders_queued": self.founders_queued,
            "alert_sent":      self.alert_sent,
            "alert_emails":    self.alert_emails,
            "sources_ran":     self.sources_ran,
            "error_message":   self.error_message,
        }
