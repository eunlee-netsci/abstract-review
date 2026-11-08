import json
import re
from datetime import datetime

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def split_keywords(text):
    return [k.strip() for k in re.split(r"[;\n]", text or "") if k.strip()]


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False, default="")
    affiliation = db.Column(db.String(300), default="")
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_reviewer = db.Column(db.Boolean, default=False, nullable=False)
    keywords = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)

    @property
    def keyword_list(self):
        return split_keywords(self.keywords)


class Submission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(500), nullable=False)
    authors_json = db.Column(db.Text, nullable=False, default="[]")
    abstract = db.Column(db.Text, nullable=False)
    keywords = db.Column(db.Text, default="")
    pdf_data = db.deferred(db.Column(db.LargeBinary))
    pdf_name = db.Column(db.String(300))
    pdf_size = db.Column(db.Integer)
    status = db.Column(db.String(20), default="submitted", nullable=False)  # submitted | withdrawn
    decision = db.Column(db.String(20), default="")
    decision_note = db.Column(db.Text, default="")  # internal note, never shown to authors
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    owner = db.relationship("User", backref="submissions")
    assignments = db.relationship("Assignment", backref="submission",
                                  cascade="all, delete-orphan", lazy="select")

    @property
    def authors(self):
        try:
            return json.loads(self.authors_json or "[]")
        except ValueError:
            return []

    @authors.setter
    def authors(self, value):
        self.authors_json = json.dumps(value, ensure_ascii=False)

    @property
    def keyword_list(self):
        return split_keywords(self.keywords)

    @property
    def active(self):
        return self.status == "submitted"

    def review_stats(self):
        active = [a for a in self.assignments if not a.conflict]
        done = [a.review for a in active if a.review]
        scores = [r.score for r in done]
        avg = sum(scores) / len(scores) if scores else None
        spread = (max(scores) - min(scores)) if len(scores) > 1 else 0
        return {"assigned": len(active), "done": len(done), "avg": avg,
                "spread": spread, "scores": scores,
                "conflicts": sum(1 for a in self.assignments if a.conflict)}


class Assignment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("submission.id"), nullable=False, index=True)
    reviewer_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    conflict = db.Column(db.Boolean, default=False, nullable=False)  # declared by reviewer
    conflict_note = db.Column(db.String(500), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    reviewer = db.relationship("User", backref="assignments")
    review = db.relationship("Review", backref="assignment", uselist=False,
                             cascade="all, delete-orphan")

    __table_args__ = (db.UniqueConstraint("submission_id", "reviewer_id", name="uq_sub_rev"),)


class Review(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignment.id"), unique=True, nullable=False)
    score = db.Column(db.Integer, nullable=False)        # 1..5
    confidence = db.Column(db.Integer, nullable=False)   # 1..3
    comments_author = db.Column(db.Text, default="")
    comments_committee = db.Column(db.Text, default="")
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class Setting(db.Model):
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, default="")


class EmailLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    to = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(500), nullable=False)
    body = db.Column(db.Text, nullable=False)
    kind = db.Column(db.String(50), default="")
    status = db.Column(db.String(100), default="queued")
    error = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    sent_at = db.Column(db.DateTime)
