from datetime import datetime
from ..extensions import db


class FounderProfile(db.Model):
    __tablename__ = "founder_profiles"

    id               = db.Column(db.Integer, primary_key=True)
    name             = db.Column(db.String(200), nullable=False, unique=True)
    known_brand      = db.Column(db.String(200))
    tier             = db.Column(db.String(20))           # DEPARTURE / CONVICTION / ALUMNI / EXEC
    current_company  = db.Column(db.String(200))
    status           = db.Column(db.String(20))           # building / advisory / quiet / still_at_brand
    bio              = db.Column(db.Text)
    profile_url      = db.Column(db.String(500))
    schools          = db.Column(db.JSON)
    past_companies   = db.Column(db.JSON)
    follower_count   = db.Column(db.Integer)
    x_handle         = db.Column(db.String(100))
    last_updated     = db.Column(db.DateTime, default=datetime.utcnow)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    # ── Relationship / outreach CRM ──────────────────────────────────────────
    # cold → email_sent → connected → had_call → in_their_corner
    outreach_status  = db.Column(db.String(30), default='cold')
    outreach_notes   = db.Column(db.Text)
    last_contact     = db.Column(db.Date)
    next_action      = db.Column(db.String(500))

    def to_dict(self):
        return {
            "id":              self.id,
            "name":            self.name,
            "known_brand":     self.known_brand,
            "tier":            self.tier,
            "current_company": self.current_company,
            "status":          self.status,
            "bio":             self.bio,
            "profile_url":     self.profile_url,
            "schools":         self.schools or [],
            "past_companies":  self.past_companies or [],
            "follower_count":  self.follower_count,
            "x_handle":        self.x_handle,
            "last_updated":    self.last_updated.isoformat() if self.last_updated else None,
            "created_at":      self.created_at.isoformat() if self.created_at else None,
            "outreach_status": self.outreach_status or "cold",
            "outreach_notes":  self.outreach_notes,
            "last_contact":    self.last_contact.isoformat() if self.last_contact else None,
            "next_action":     self.next_action,
        }

    def __repr__(self):
        return f"<FounderProfile {self.name}>"
