from datetime import datetime
from ..extensions import db


class FounderProfile(db.Model):
    __tablename__ = "founder_profiles"

    id              = db.Column(db.Integer, primary_key=True)
    name            = db.Column(db.String(200), nullable=False, unique=True)
    known_brand     = db.Column(db.String(200))          # brand they're famous for
    tier            = db.Column(db.String(20))           # DEPARTURE / CONVICTION / ALUMNI / EXEC
    current_company = db.Column(db.String(200))          # null = not found / no role
    status          = db.Column(db.String(20))           # building / advisory / quiet / still_at_brand
    bio             = db.Column(db.Text)
    profile_url     = db.Column(db.String(500))          # NinjaPear profile page
    schools         = db.Column(db.JSON)                 # list of school names
    past_companies  = db.Column(db.JSON)                 # list of prior employers
    follower_count  = db.Column(db.Integer)
    x_handle        = db.Column(db.String(100))
    last_updated    = db.Column(db.DateTime, default=datetime.utcnow)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

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
        }

    def __repr__(self):
        return f"<FounderProfile {self.name}>"
