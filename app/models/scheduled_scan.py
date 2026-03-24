from datetime import datetime, timezone
from ..extensions import db


class ScheduledScan(db.Model):
    __tablename__ = "scheduled_scans"

    id          = db.Column(db.Integer, primary_key=True)
    owner_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name        = db.Column(db.String(255), nullable=False)
    days_back   = db.Column(db.Integer,  default=7)    # scan window in days
    max_results = db.Column(db.Integer,  default=200)
    frequency   = db.Column(db.String(20), default="daily")   # daily | weekly
    enabled     = db.Column(db.Boolean,  default=True)
    last_run_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_run_new  = db.Column(db.Integer,     default=0)   # new signals from last run
    scan_type     = db.Column(db.String(50),  default='full')
    last_run_hot  = db.Column(db.Integer,     default=0)
    last_run_warm = db.Column(db.Integer,     default=0)
    last_run_cold = db.Column(db.Integer,     default=0)
    total_signals         = db.Column(db.Integer, default=0)
    total_hot             = db.Column(db.Integer, default=0)
    total_warm            = db.Column(db.Integer, default=0)
    last_alert_sent       = db.Column(db.Boolean, default=False)
    last_alert_emails     = db.Column(db.String(500), nullable=True)
    last_founders_queued  = db.Column(db.Integer, default=0)
    created_at  = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self):
        return {
            "id":           self.id,
            "name":         self.name,
            "days_back":    self.days_back,
            "max_results":  self.max_results,
            "frequency":    self.frequency,
            "enabled":      self.enabled,
            "last_run_at":  self.last_run_at.isoformat() if self.last_run_at else None,
            "last_run_new":  self.last_run_new,
            "scan_type":     self.scan_type or "full",
            "last_run_hot":  self.last_run_hot  or 0,
            "last_run_warm": self.last_run_warm or 0,
            "last_run_cold": self.last_run_cold or 0,
            "total_signals":        self.total_signals or 0,
            "total_hot":            self.total_hot or 0,
            "total_warm":           self.total_warm or 0,
            "last_alert_sent":      self.last_alert_sent or False,
            "last_alert_emails":    self.last_alert_emails or "",
            "last_founders_queued": self.last_founders_queued or 0,
            "created_at":   self.created_at.isoformat(),
        }

    def __repr__(self):
        return f"<ScheduledScan {self.name}>"
