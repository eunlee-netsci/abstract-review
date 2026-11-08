import csv
import io
import os
import secrets
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, send_file, session, url_for)
from flask_login import (LoginManager, current_user, login_required, login_user,
                         logout_user)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import mailer
from assign import auto_assign, has_coi, keyword_overlap
from models import (Assignment, EmailLog, Review, Setting, Submission, User, db,
                    split_keywords)

KST = ZoneInfo("Asia/Seoul")

DECISIONS = [("", "— undecided —"), ("oral", "Accept (Oral)"),
             ("poster", "Accept (Poster)"), ("reject", "Reject")]
DECISION_LABEL = dict(DECISIONS)
SCORE_LABEL = {5: "5 · Strong accept", 4: "4 · Accept", 3: "3 · Borderline",
               2: "2 · Weak reject", 1: "1 · Reject"}
CONF_LABEL = {3: "3 · Expert", 2: "2 · Knowledgeable", 1: "1 · Outsider"}

DEFAULTS = {
    "conf_name": "Conference 2028",
    "conf_info": "",
    "submissions_open": "1",
    "submission_deadline": "",
    "review_deadline": "",
    "keywords": "Network science\nComputational social science\nComplex systems\nData science",
    "word_limit": "300",
    "max_pdf_mb": "5",
    "pdf_required": "1",
    "reviews_per_paper": "2",
    "decisions_released": "0",
    "share_reviews": "1",
}

# ---------------------------------------------------------------- app setup
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

db_url = os.environ.get("DATABASE_URL", "sqlite:///local.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or "dev-only-change-me",
    SQLALCHEMY_DATABASE_URI=db_url,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=db_url.startswith("postgresql"),
)
db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in first."

serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"])


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


# ---------------------------------------------------------------- helpers
def S(key):
    if "_settings" not in g:
        g._settings = {s.key: s.value for s in Setting.query.all()}
    return g._settings.get(key, DEFAULTS.get(key, ""))


def set_setting(key, value):
    row = db.session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value
    g.pop("_settings", None)


def now_kst():
    return datetime.now(KST).replace(tzinfo=None)


def parse_dt(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M") if s else None
    except ValueError:
        return None


def fmt_dt(s):
    d = parse_dt(s) if isinstance(s, str) else s
    return d.strftime("%Y-%m-%d %H:%M KST") if d else "not set"


def submissions_open():
    if S("submissions_open") != "1":
        return False
    dl = parse_dt(S("submission_deadline"))
    return dl is None or now_kst() <= dl


def reviews_open():
    dl = parse_dt(S("review_deadline"))
    return dl is None or now_kst() <= dl


def conf_keywords():
    return split_keywords(S("keywords"))


def ext_url(endpoint, **kw):
    base = os.environ.get("BASE_URL")
    if base:
        return base.rstrip("/") + url_for(endpoint, **kw)
    return url_for(endpoint, _external=True, **kw)


def make_token(user):
    return serializer.dumps({"uid": user.id, "ph": user.password_hash[-12:]}, salt="setpw")


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def csrf_field():
    return Markup(f'<input type="hidden" name="_csrf" value="{csrf_token()}">')


@app.before_request
def csrf_protect():
    if request.method == "POST":
        tok = request.form.get("_csrf")
        if not tok or tok != session.get("_csrf"):
            abort(400, "Your session expired or the form is stale. Please go back, reload and try again.")


@app.context_processor
def inject():
    return dict(S=S, csrf_field=csrf_field, fmt_dt=fmt_dt, DECISIONS=DECISIONS,
                DECISION_LABEL=DECISION_LABEL, SCORE_LABEL=SCORE_LABEL, CONF_LABEL=CONF_LABEL,
                submissions_open=submissions_open, reviews_open=reviews_open,
                conf_keywords=conf_keywords)


@app.errorhandler(413)
def too_large(_e):
    flash("The uploaded file is too large.", "error")
    return redirect(request.referrer or url_for("index"))


@app.errorhandler(400)
def bad_request(e):
    return render_template("message.html", title="Bad request", message=e.description), 400


def admin_required(f):
    @wraps(f)
    @login_required
    def wrapper(*a, **kw):
        if not current_user.is_admin:
            abort(403)
        return f(*a, **kw)
    return wrapper


def reviewer_required(f):
    @wraps(f)
    @login_required
    def wrapper(*a, **kw):
        if not current_user.is_reviewer:
            abort(403)
        return f(*a, **kw)
    return wrapper


def send_and_commit(logs):
    db.session.commit()
    mailer.dispatch(app, logs)


# ---------------------------------------------------------------- bootstrap
def bootstrap():
    with app.app_context():
        db.create_all()
        email = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
        pw = os.environ.get("ADMIN_PASSWORD") or ""
        if email:
            u = User.query.filter_by(email=email).first()
            if u is None and pw:
                db.session.add(User(email=email, name="Admin", password_hash=generate_password_hash(pw),
                                    is_admin=True, is_reviewer=False))
            elif u is not None and not u.is_admin:
                u.is_admin = True
            db.session.commit()


bootstrap()


# ---------------------------------------------------------------- public / auth
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("author_home"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        name = request.form.get("name", "").strip()
        aff = request.form.get("affiliation", "").strip()
        pw = request.form.get("password", "")
        if "@" not in email or not name or not aff:
            flash("Please fill in name, affiliation and a valid email.", "error")
        elif len(pw) < 8:
            flash("Password must be at least 8 characters.", "error")
        elif pw != request.form.get("password2", ""):
            flash("Passwords do not match.", "error")
        elif User.query.filter_by(email=email).first():
            flash("An account with this email already exists. Use 'Forgot password' to set a password.", "error")
        else:
            u = User(email=email, name=name, affiliation=aff, password_hash=generate_password_hash(pw),
                     last_login=datetime.utcnow())
            db.session.add(u)
            db.session.commit()
            login_user(u, remember=True)
            flash("Account created.", "ok")
            return redirect(url_for("author_home"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        u = User.query.filter_by(email=email).first()
        if u and check_password_hash(u.password_hash, request.form.get("password", "")):
            u.last_login = datetime.utcnow()
            db.session.commit()
            login_user(u, remember=True)
            nxt = request.args.get("next", "")
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            if u.is_admin:
                return redirect(url_for("admin_home"))
            if u.is_reviewer and u.assignments:
                return redirect(url_for("reviewer_home"))
            return redirect(url_for("author_home"))
        flash("Wrong email or password.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    logout_user()
    return redirect(url_for("index"))


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        u = User.query.filter_by(email=email).first()
        if u:
            link = ext_url("set_password", token=make_token(u))
            log = mailer.queue(u.email, f"[{S('conf_name')}] Set your password",
                               f"Hello {u.name},\n\nUse the link below to set your password "
                               f"(valid for 14 days):\n{link}\n\n{S('conf_name')}", "reset")
            send_and_commit([log])
        flash("If an account exists for that email, a link to set a password has been sent.", "ok")
        return redirect(url_for("login"))
    return render_template("forgot.html")


@app.route("/set-password/<token>", methods=["GET", "POST"])
def set_password(token):
    try:
        data = serializer.loads(token, salt="setpw", max_age=14 * 24 * 3600)
    except SignatureExpired:
        return render_template("message.html", title="Link expired",
                               message="This link has expired. Use 'Forgot password' to get a new one.")
    except BadSignature:
        abort(404)
    u = db.session.get(User, data.get("uid"))
    if u is None or u.password_hash[-12:] != data.get("ph"):
        return render_template("message.html", title="Link already used",
                               message="This link was already used. Use 'Forgot password' if needed.")
    if request.method == "POST":
        pw = request.form.get("password", "")
        if len(pw) < 8:
            flash("Password must be at least 8 characters.", "error")
        elif pw != request.form.get("password2", ""):
            flash("Passwords do not match.", "error")
        else:
            u.password_hash = generate_password_hash(pw)
            if request.form.get("name", "").strip():
                u.name = request.form["name"].strip()
            if request.form.get("affiliation", "").strip():
                u.affiliation = request.form["affiliation"].strip()
            u.last_login = datetime.utcnow()
            db.session.commit()
            login_user(u, remember=True)
            flash("Password set. Welcome!", "ok")
            return redirect(url_for("reviewer_home" if u.is_reviewer else "author_home"))
    return render_template("set_password.html", u=u)


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    u = current_user
    if request.method == "POST":
        u.name = request.form.get("name", u.name).strip() or u.name
        u.affiliation = request.form.get("affiliation", u.affiliation).strip()
        if u.is_reviewer:
            kws = request.form.getlist("kw")
            extra = split_keywords(request.form.get("kw_extra", ""))
            u.keywords = "\n".join(dict.fromkeys(kws + extra))
        new = request.form.get("new_password", "")
        if new:
            if not check_password_hash(u.password_hash, request.form.get("current_password", "")):
                flash("Current password is wrong; password not changed.", "error")
                db.session.commit()
                return redirect(url_for("profile"))
            if len(new) < 8:
                flash("New password must be at least 8 characters.", "error")
                db.session.commit()
                return redirect(url_for("profile"))
            u.password_hash = generate_password_hash(new)
        db.session.commit()
        flash("Profile saved.", "ok")
        return redirect(url_for("profile"))
    return render_template("profile.html")


# ---------------------------------------------------------------- author
@app.route("/my")
@login_required
def author_home():
    subs = Submission.query.filter_by(owner_id=current_user.id).order_by(Submission.id).all()
    return render_template("author_home.html", subs=subs,
                           released=S("decisions_released") == "1",
                           share=S("share_reviews") == "1")


def parse_authors(form):
    out = []
    for n, a, e in zip(form.getlist("author_name"), form.getlist("author_aff"),
                       form.getlist("author_email")):
        n, a, e = n.strip(), a.strip(), e.strip().lower()
        if n or a or e:
            out.append({"name": n, "affiliation": a, "email": e})
    return out


def handle_submission_form(sub):
    """Validate the posted form into `sub`. Returns list of errors (empty if OK)."""
    f = request.form
    errors = []
    title = f.get("title", "").strip()
    authors = parse_authors(f)
    abstract = f.get("abstract", "").strip()
    kws = [k for k in f.getlist("kw") if k in conf_keywords()]
    limit = int(S("word_limit") or 0)
    words = len(abstract.split())

    if not title:
        errors.append("Title is required.")
    if not authors or any(not a["name"] or not a["affiliation"] for a in authors):
        errors.append("Each author needs a name and an affiliation.")
    if not abstract:
        errors.append("Abstract is required.")
    elif limit and words > limit:
        errors.append(f"Abstract has {words} words; the limit is {limit}.")
    if conf_keywords() and not kws:
        errors.append("Choose at least one topic.")

    pdf = request.files.get("pdf")
    pdf_bytes = None
    if pdf and pdf.filename:
        pdf_bytes = pdf.read()
        max_mb = float(S("max_pdf_mb") or 5)
        if not pdf_bytes.startswith(b"%PDF"):
            errors.append("The uploaded file is not a PDF.")
        elif len(pdf_bytes) > max_mb * 1024 * 1024:
            errors.append(f"PDF is larger than {max_mb:g} MB.")
    elif S("pdf_required") == "1" and not sub.pdf_name:
        errors.append("A PDF file is required.")

    # keep what the user typed even on error
    sub.title, sub.abstract, sub.keywords = title, abstract, "\n".join(kws)
    sub.authors = authors
    if not errors and pdf_bytes:
        sub.pdf_data, sub.pdf_size = pdf_bytes, len(pdf_bytes)
        sub.pdf_name = os.path.basename(pdf.filename)[:290] or "abstract.pdf"
    return errors


@app.route("/submit", methods=["GET", "POST"])
@login_required
def submit():
    if not submissions_open() and not current_user.is_admin:
        flash("Submission is closed.", "error")
        return redirect(url_for("author_home"))
    sub = Submission(owner_id=current_user.id, title="", abstract="")
    if request.method == "GET":
        sub.authors = [{"name": current_user.name, "affiliation": current_user.affiliation,
                        "email": current_user.email}]
        return render_template("submit.html", sub=sub, new=True)
    errors = handle_submission_form(sub)
    if errors:
        for e in errors:
            flash(e, "error")
        return render_template("submit.html", sub=sub, new=True)
    sub.created_at = sub.updated_at = datetime.utcnow()
    db.session.add(sub)
    db.session.flush()
    log = mailer.queue(current_user.email, f"[{S('conf_name')}] Submission #{sub.id} received",
                       f"Dear {current_user.name},\n\nWe received your abstract.\n\n"
                       f"  #{sub.id}: {sub.title}\n\nYou can edit it until "
                       f"{fmt_dt(S('submission_deadline'))}:\n{ext_url('author_home')}\n\n{S('conf_name')}",
                       "confirmation")
    send_and_commit([log])
    flash(f"Submission #{sub.id} received. A confirmation email has been sent.", "ok")
    return redirect(url_for("author_home"))


def get_owned_submission(sid):
    sub = db.session.get(Submission, sid) or abort(404)
    if sub.owner_id != current_user.id and not current_user.is_admin:
        abort(403)
    return sub


@app.route("/submission/<int:sid>/edit", methods=["GET", "POST"])
@login_required
def edit_submission(sid):
    sub = get_owned_submission(sid)
    if not current_user.is_admin and (not submissions_open() or not sub.active):
        flash("This submission can no longer be edited.", "error")
        return redirect(url_for("author_home"))
    if request.method == "POST":
        errors = handle_submission_form(sub)
        if errors:
            db.session.rollback()
            for e in errors:
                flash(e, "error")
            sub_view = Submission(id=sub.id, owner_id=sub.owner_id, title=request.form.get("title", ""),
                                  abstract=request.form.get("abstract", ""),
                                  keywords="\n".join(request.form.getlist("kw")),
                                  pdf_name=sub.pdf_name, pdf_size=sub.pdf_size)
            sub_view.authors = parse_authors(request.form)
            return render_template("submit.html", sub=sub_view, new=False)
        sub.updated_at = datetime.utcnow()
        db.session.commit()
        flash("Saved.", "ok")
        return redirect(url_for("admin_submission", sid=sid) if current_user.is_admin and
                        sub.owner_id != current_user.id else url_for("author_home"))
    return render_template("submit.html", sub=sub, new=False)


@app.route("/submission/<int:sid>/withdraw", methods=["POST"])
@login_required
def withdraw_submission(sid):
    sub = get_owned_submission(sid)
    sub.status = "submitted" if request.form.get("restore") and current_user.is_admin else "withdrawn"
    db.session.commit()
    flash(f"Submission #{sid} {'restored' if sub.active else 'withdrawn'}.", "ok")
    return redirect(request.referrer or url_for("author_home"))


@app.route("/submission/<int:sid>/pdf")
@login_required
def submission_pdf(sid):
    sub = db.session.get(Submission, sid) or abort(404)
    blind = False
    if sub.owner_id == current_user.id or current_user.is_admin:
        pass
    elif current_user.is_reviewer and Assignment.query.filter_by(
            submission_id=sid, reviewer_id=current_user.id).first() and sub.active:
        blind = True
    else:
        abort(403)
    if not sub.pdf_data:
        abort(404)
    name = f"abstract_{sid}.pdf" if blind else (sub.pdf_name or f"abstract_{sid}.pdf")
    return send_file(io.BytesIO(sub.pdf_data), mimetype="application/pdf",
                     download_name=name, as_attachment=False)


# ---------------------------------------------------------------- reviewer
@app.route("/reviews")
@reviewer_required
def reviewer_home():
    asg = (Assignment.query.filter_by(reviewer_id=current_user.id)
           .order_by(Assignment.submission_id).all())
    return render_template("reviewer_home.html", asg=asg)


def get_my_assignment(aid):
    a = db.session.get(Assignment, aid) or abort(404)
    if a.reviewer_id != current_user.id:
        abort(403)
    return a


@app.route("/review/<int:aid>", methods=["GET", "POST"])
@reviewer_required
def review(aid):
    a = get_my_assignment(aid)
    sub = a.submission
    if not sub.active:
        flash("This submission was withdrawn; no review is needed.", "error")
        return redirect(url_for("reviewer_home"))
    can_edit = reviews_open() and not a.conflict
    if request.method == "POST":
        if not can_edit:
            flash("Reviews are closed.", "error")
            return redirect(url_for("reviewer_home"))
        try:
            score = int(request.form.get("score", ""))
            conf = int(request.form.get("confidence", ""))
            assert score in SCORE_LABEL and conf in CONF_LABEL
        except (ValueError, AssertionError):
            flash("Please choose a score and a confidence level.", "error")
            return render_template("review.html", a=a, sub=sub, can_edit=can_edit, form=request.form)
        r = a.review or Review(assignment_id=a.id, score=score, confidence=conf,
                               submitted_at=datetime.utcnow())
        r.score, r.confidence = score, conf
        r.comments_author = request.form.get("comments_author", "").strip()
        r.comments_committee = request.form.get("comments_committee", "").strip()
        r.updated_at = datetime.utcnow()
        db.session.add(r)
        db.session.commit()
        flash(f"Review for #{sub.id} saved. You can edit it until the review deadline.", "ok")
        return redirect(url_for("reviewer_home"))
    form = {}
    if a.review:
        form = {"score": str(a.review.score), "confidence": str(a.review.confidence),
                "comments_author": a.review.comments_author,
                "comments_committee": a.review.comments_committee}
    return render_template("review.html", a=a, sub=sub, can_edit=can_edit, form=form)


@app.route("/review/<int:aid>/conflict", methods=["POST"])
@reviewer_required
def declare_conflict(aid):
    a = get_my_assignment(aid)
    if a.review:
        flash("You already submitted a review for this paper. Contact the organizers.", "error")
    else:
        a.conflict = True
        a.conflict_note = request.form.get("note", "").strip()[:500]
        db.session.commit()
        flash("Conflict declared. The organizers will reassign this abstract.", "ok")
    return redirect(url_for("reviewer_home"))


# ---------------------------------------------------------------- admin
@app.route("/admin")
@admin_required
def admin_home():
    subs = Submission.query.all()
    active = [s for s in subs if s.active]
    asg = Assignment.query.all()
    real = [a for a in asg if not a.conflict and a.submission.active]
    done = sum(1 for a in real if a.review)
    dec = {k: sum(1 for s in active if (s.decision or "") == k) for k, _ in DECISIONS}
    stats = dict(n_active=len(active), n_withdrawn=len(subs) - len(active),
                 n_reviewers=User.query.filter_by(is_reviewer=True).count(),
                 n_asg=len(real), n_done=done,
                 n_conflict=sum(1 for a in asg if a.conflict and a.submission.active),
                 n_under=sum(1 for s in active if s.review_stats()["assigned"] <
                             int(S("reviews_per_paper") or 2)),
                 n_users=User.query.count(), dec=dec)
    return render_template("admin/home.html", st=stats, mail_backend=mailer.backend())


@app.route("/admin/submissions")
@admin_required
def admin_submissions():
    subs = Submission.query.order_by(Submission.id).all()
    rows = [(s, s.review_stats()) for s in subs]
    sort = request.args.get("sort", "id")
    if sort == "avg":
        rows.sort(key=lambda x: (x[1]["avg"] is None, -(x[1]["avg"] or 0)))
    elif sort == "spread":
        rows.sort(key=lambda x: -x[1]["spread"])
    elif sort == "progress":
        rows.sort(key=lambda x: (x[1]["done"] - x[1]["assigned"], x[0].id))
    status = request.args.get("status", "active")
    if status == "active":
        rows = [r for r in rows if r[0].active]
    elif status == "withdrawn":
        rows = [r for r in rows if not r[0].active]
    return render_template("admin/submissions.html", rows=rows, sort=sort, status=status)


@app.route("/admin/submission/<int:sid>")
@admin_required
def admin_submission(sid):
    sub = db.session.get(Submission, sid) or abort(404)
    assigned_ids = {a.reviewer_id for a in sub.assignments}
    load = {}
    for a in Assignment.query.filter_by(conflict=False).all():
        load[a.reviewer_id] = load.get(a.reviewer_id, 0) + 1
    cands = []
    for r in User.query.filter_by(is_reviewer=True).all():
        if r.id in assigned_ids:
            continue
        cands.append({"r": r, "coi": has_coi(r, sub), "overlap": keyword_overlap(r, sub),
                      "load": load.get(r.id, 0)})
    cands.sort(key=lambda c: (c["coi"] is not None, -c["overlap"], c["load"], c["r"].name.lower()))
    return render_template("admin/submission.html", sub=sub, st=sub.review_stats(), cands=cands)


@app.route("/admin/submission/<int:sid>/assign", methods=["POST"])
@admin_required
def admin_assign_one(sid):
    sub = db.session.get(Submission, sid) or abort(404)
    rid = request.form.get("reviewer_id", type=int)
    r = db.session.get(User, rid) if rid else None
    if not r or not r.is_reviewer:
        flash("Pick a reviewer.", "error")
    elif Assignment.query.filter_by(submission_id=sid, reviewer_id=rid).first():
        flash("Already assigned.", "error")
    else:
        db.session.add(Assignment(submission_id=sid, reviewer_id=rid))
        logs = []
        if request.form.get("notify"):
            logs.append(assignment_email(r, 1))
        send_and_commit(logs)
        flash(f"Assigned {r.name}." + (" (Warning: possible conflict)" if has_coi(r, sub) else ""), "ok")
    return redirect(url_for("admin_submission", sid=sid))


@app.route("/admin/assignment/<int:aid>/delete", methods=["POST"])
@admin_required
def admin_unassign(aid):
    a = db.session.get(Assignment, aid) or abort(404)
    sid = a.submission_id
    db.session.delete(a)
    db.session.commit()
    flash("Assignment removed (and its review, if any).", "ok")
    return redirect(url_for("admin_submission", sid=sid))


@app.route("/admin/submission/<int:sid>/decision", methods=["POST"])
@admin_required
def admin_decision(sid):
    sub = db.session.get(Submission, sid) or abort(404)
    d = request.form.get("decision", "")
    if d not in DECISION_LABEL:
        abort(400)
    sub.decision = d
    sub.decision_note = request.form.get("decision_note", "").strip()
    db.session.commit()
    flash(f"Decision for #{sid}: {DECISION_LABEL[d]}", "ok")
    nxt = request.form.get("next")
    return redirect(nxt if nxt and nxt.startswith("/") else url_for("admin_submission", sid=sid))


@app.route("/admin/decisions", methods=["POST"])
@admin_required
def admin_bulk_decisions():
    n = 0
    for key, val in request.form.items():
        if key.startswith("dec_") and val in DECISION_LABEL:
            sub = db.session.get(Submission, int(key[4:]))
            if sub and sub.decision != val:
                sub.decision = val
                n += 1
    db.session.commit()
    flash(f"{n} decision(s) updated.", "ok")
    return redirect(request.referrer or url_for("admin_submissions"))


def assignment_email(reviewer, n):
    dl = fmt_dt(S("review_deadline"))
    link = ext_url("reviewer_home")
    if reviewer.last_login is None:
        link = ext_url("set_password", token=make_token(reviewer))
        how = "First set your password with this link (valid 14 days):"
    else:
        how = "Log in here:"
    return mailer.queue(reviewer.email, f"[{S('conf_name')}] {n} abstract(s) assigned for review",
                        f"Dear {reviewer.name},\n\nYou have been assigned {n} new abstract(s) to review "
                        f"for {S('conf_name')}.\nReview deadline: {dl}\n\n{how}\n{link}\n\n"
                        f"If you have a conflict of interest with any abstract, please declare it on "
                        f"the review page.\n\nThank you,\n{S('conf_name')} Program Committee",
                        "assignment")


@app.route("/admin/assign", methods=["GET", "POST"])
@admin_required
def admin_assign():
    subs = Submission.query.filter_by(status="submitted").order_by(Submission.id).all()
    reviewers = User.query.filter_by(is_reviewer=True).all()
    result = None
    if request.method == "POST":
        k = max(1, min(10, request.form.get("k", type=int) or 2))
        assigned_all, assigned_active = {}, {}
        for a in Assignment.query.all():
            assigned_all.setdefault(a.submission_id, set()).add(a.reviewer_id)
            if not a.conflict:
                assigned_active.setdefault(a.submission_id, set()).add(a.reviewer_id)
        pairs, unfilled, cap = auto_assign(subs, reviewers, assigned_all, assigned_active, k)
        per_rev = {}
        for sid, rid in pairs:
            db.session.add(Assignment(submission_id=sid, reviewer_id=rid))
            per_rev[rid] = per_rev.get(rid, 0) + 1
        logs = []
        if request.form.get("notify"):
            for rid, n in per_rev.items():
                logs.append(assignment_email(db.session.get(User, rid), n))
        send_and_commit(logs)
        result = dict(n_new=len(pairs), n_rev=len(per_rev), cap=cap, unfilled=unfilled,
                      notified=len(logs))
        flash(f"Created {len(pairs)} assignments for {len(per_rev)} reviewers.", "ok")
    load = {r.id: 0 for r in reviewers}
    for a in Assignment.query.filter_by(conflict=False).all():
        if a.reviewer_id in load and a.submission.active:
            load[a.reviewer_id] += 1
    return render_template("admin/assign.html", subs=subs, reviewers=reviewers, load=load,
                           result=result)


@app.route("/admin/assign/notify-all", methods=["POST"])
@admin_required
def admin_notify_all():
    """Send one email to every reviewer who has at least one pending assignment."""
    logs = []
    for r in User.query.filter_by(is_reviewer=True).all():
        n = sum(1 for a in r.assignments if not a.conflict and a.submission.active and not a.review)
        if n:
            logs.append(assignment_email(r, n))
    send_and_commit(logs)
    flash(f"Queued {len(logs)} assignment email(s).", "ok")
    return redirect(url_for("admin_assign"))


@app.route("/admin/remind", methods=["POST"])
@admin_required
def admin_remind():
    logs = []
    for r in User.query.filter_by(is_reviewer=True).all():
        pending = [a for a in r.assignments if not a.conflict and a.submission.active and not a.review]
        if not pending:
            continue
        link = ext_url("reviewer_home") if r.last_login else ext_url("set_password", token=make_token(r))
        logs.append(mailer.queue(
            r.email, f"[{S('conf_name')}] Reminder: {len(pending)} review(s) pending",
            f"Dear {r.name},\n\nThis is a friendly reminder that {len(pending)} review(s) are still "
            f"pending. Review deadline: {fmt_dt(S('review_deadline'))}\n\n{link}\n\n"
            f"Thank you,\n{S('conf_name')} Program Committee", "reminder"))
    send_and_commit(logs)
    flash(f"Queued {len(logs)} reminder(s).", "ok")
    return redirect(request.referrer or url_for("admin_home"))


@app.route("/admin/reviewers", methods=["GET", "POST"])
@admin_required
def admin_reviewers():
    if request.method == "POST":
        text = request.form.get("csv", "")
        created, updated, bad = [], [], []
        for row in csv.reader(io.StringIO(text)):
            row = [c.strip() for c in row]
            if not row or not any(row) or row[0].lower() == "name":
                continue
            if len(row) < 2 or "@" not in row[1]:
                bad.append(",".join(row))
                continue
            name, email = row[0], row[1].lower()
            aff = row[2] if len(row) > 2 else ""
            kws = "\n".join(split_keywords(row[3])) if len(row) > 3 else ""
            u = User.query.filter_by(email=email).first()
            if u is None:
                u = User(email=email, name=name, affiliation=aff, keywords=kws, is_reviewer=True,
                         password_hash=generate_password_hash(secrets.token_urlsafe(24)))
                db.session.add(u)
                created.append(u)
            else:
                u.is_reviewer = True
                if aff:
                    u.affiliation = aff
                if kws:
                    u.keywords = kws
                updated.append(u)
        db.session.flush()
        logs = []
        if request.form.get("invite"):
            for u in created:
                logs.append(invite_email(u))
        send_and_commit(logs)
        flash(f"Added {len(created)} new reviewer(s), updated {len(updated)}."
              + (f" Invitations queued: {len(logs)}." if logs else ""), "ok")
        if bad:
            flash("Skipped lines (need: name,email[,affiliation[,keywords]]): " + " | ".join(bad[:10]),
                  "error")
        return redirect(url_for("admin_reviewers"))
    reviewers = User.query.filter_by(is_reviewer=True).order_by(User.name).all()
    rows = []
    for r in reviewers:
        act = [a for a in r.assignments if a.submission.active]
        rows.append({"r": r, "assigned": sum(1 for a in act if not a.conflict),
                     "done": sum(1 for a in act if a.review and not a.conflict),
                     "conflicts": sum(1 for a in act if a.conflict)})
    return render_template("admin/reviewers.html", rows=rows, unknown_kw=_unknown_keywords(reviewers))


def _unknown_keywords(reviewers):
    known = {k.lower() for k in conf_keywords()}
    found = set()
    for r in reviewers:
        for k in r.keyword_list:
            if k.lower() not in known:
                found.add(k)
    return sorted(found)


def invite_email(u):
    link = ext_url("set_password", token=make_token(u))
    return mailer.queue(u.email, f"[{S('conf_name')}] Invitation to review abstracts",
                        f"Dear {u.name},\n\nYou are invited to serve as a reviewer for "
                        f"{S('conf_name')}.\n\nPlease set your password and check your topic "
                        f"keywords with this link (valid 14 days):\n{link}\n\n"
                        f"Thank you,\n{S('conf_name')} Program Committee", "invite")


@app.route("/admin/reviewer/<int:uid>/invite", methods=["POST"])
@admin_required
def admin_invite(uid):
    u = db.session.get(User, uid) or abort(404)
    send_and_commit([invite_email(u)])
    flash(f"Invitation queued for {u.email}.", "ok")
    return redirect(url_for("admin_reviewers"))


@app.route("/admin/reviewers/invite-pending", methods=["POST"])
@admin_required
def admin_invite_pending():
    users = User.query.filter_by(is_reviewer=True).filter(User.last_login.is_(None)).all()
    send_and_commit([invite_email(u) for u in users])
    flash(f"Invitations queued for {len(users)} reviewer(s) who have never logged in.", "ok")
    return redirect(url_for("admin_reviewers"))


@app.route("/admin/reviewer/<int:uid>/remove", methods=["POST"])
@admin_required
def admin_remove_reviewer(uid):
    u = db.session.get(User, uid) or abort(404)
    if any(a.review for a in u.assignments):
        flash("This reviewer already submitted reviews; remove those assignments first.", "error")
    else:
        for a in list(u.assignments):
            db.session.delete(a)
        u.is_reviewer = False
        db.session.commit()
        flash(f"{u.name} is no longer a reviewer.", "ok")
    return redirect(url_for("admin_reviewers"))


@app.route("/admin/reviewer/<int:uid>/keywords", methods=["POST"])
@admin_required
def admin_reviewer_keywords(uid):
    u = db.session.get(User, uid) or abort(404)
    u.keywords = "\n".join(split_keywords(request.form.get("keywords", "")))
    u.affiliation = request.form.get("affiliation", u.affiliation).strip()
    db.session.commit()
    flash(f"Updated {u.name}.", "ok")
    return redirect(url_for("admin_reviewers"))


@app.route("/admin/users")
@admin_required
def admin_users():
    users = User.query.order_by(User.id).all()
    return render_template("admin/users.html", users=users)


@app.route("/admin/user/<int:uid>/role", methods=["POST"])
@admin_required
def admin_user_role(uid):
    u = db.session.get(User, uid) or abort(404)
    role, on = request.form.get("role"), request.form.get("on") == "1"
    if role == "admin":
        if u.id == current_user.id and not on:
            flash("You cannot remove your own admin role.", "error")
        else:
            u.is_admin = on
    elif role == "reviewer":
        if not on and any(a.review for a in u.assignments):
            flash("Has submitted reviews; cannot remove reviewer role.", "error")
        else:
            if not on:
                for a in list(u.assignments):
                    db.session.delete(a)
            u.is_reviewer = on
    db.session.commit()
    return redirect(url_for("admin_users"))


@app.route("/admin/release", methods=["POST"])
@admin_required
def admin_release():
    if request.form.get("action") == "hide":
        set_setting("decisions_released", "0")
        db.session.commit()
        flash("Decisions hidden from authors.", "ok")
        return redirect(url_for("admin_home"))
    subs = Submission.query.filter_by(status="submitted").all()
    undecided = [s for s in subs if not s.decision]
    if undecided and not request.form.get("force"):
        flash(f"{len(undecided)} submission(s) have no decision yet. Tick 'release anyway' to proceed.",
              "error")
        return redirect(url_for("admin_home"))
    set_setting("decisions_released", "1")
    logs = []
    if request.form.get("notify"):
        for s in subs:
            if not s.decision:
                continue
            label = DECISION_LABEL[s.decision]
            accepted = s.decision in ("oral", "poster")
            opening = ("We are pleased to inform you that your abstract has been accepted"
                       f" ({label.split('(')[-1].rstrip(')')} presentation)."
                       if accepted else
                       "We regret to inform you that your abstract could not be accepted this time.")
            extra = (f"\nReviewer comments are available after logging in:\n{ext_url('author_home')}\n"
                     if S("share_reviews") == "1" else "")
            recipients = {s.owner.email}
            logs.extend(mailer.queue(addr, f"[{S('conf_name')}] Decision on abstract #{s.id}",
                                     f"Dear {s.owner.name},\n\n{opening}\n\n  #{s.id}: {s.title}\n"
                                     f"{extra}\nBest regards,\n{S('conf_name')} Program Committee",
                                     "decision") for addr in recipients)
    send_and_commit(logs)
    flash("Decisions released to authors." + (f" {len(logs)} email(s) queued." if logs else ""), "ok")
    return redirect(url_for("admin_home"))


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    keys = ["conf_name", "conf_info", "submission_deadline", "review_deadline", "keywords",
            "word_limit", "max_pdf_mb", "reviews_per_paper"]
    checks = ["submissions_open", "pdf_required", "share_reviews"]
    if request.method == "POST":
        for k in keys:
            set_setting(k, request.form.get(k, "").strip())
        for k in checks:
            set_setting(k, "1" if request.form.get(k) else "0")
        db.session.commit()
        flash("Settings saved.", "ok")
        return redirect(url_for("admin_settings"))
    return render_template("admin/settings.html")


@app.route("/admin/outbox")
@admin_required
def admin_outbox():
    status = request.args.get("status", "")
    q = EmailLog.query
    if status == "problem":
        q = q.filter(EmailLog.status != "sent")
    logs = q.order_by(EmailLog.id.desc()).limit(500).all()
    return render_template("admin/outbox.html", logs=logs, status=status,
                           mail_backend=mailer.backend())


@app.route("/admin/outbox/retry", methods=["POST"])
@admin_required
def admin_outbox_retry():
    logs = EmailLog.query.filter(EmailLog.status != "sent").all()
    for l in logs:
        l.status = "queued"
    send_and_commit(logs)
    flash(f"Retrying {len(logs)} email(s).", "ok")
    return redirect(url_for("admin_outbox"))


def csv_response(rows, filename):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/admin/export/submissions.csv")
@admin_required
def export_submissions():
    rows = [["id", "status", "title", "authors", "affiliations", "emails", "submitter_email",
             "keywords", "abstract", "has_pdf", "reviews_done", "reviews_assigned", "scores",
             "avg_score", "decision", "created_kst"]]
    for s in Submission.query.order_by(Submission.id).all():
        st = s.review_stats()
        created = s.created_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(KST).strftime("%Y-%m-%d %H:%M")
        rows.append([s.id, s.status, s.title, "; ".join(a["name"] for a in s.authors),
                     "; ".join(a["affiliation"] for a in s.authors),
                     "; ".join(a.get("email", "") for a in s.authors), s.owner.email,
                     "; ".join(s.keyword_list), s.abstract, "yes" if s.pdf_name else "no",
                     st["done"], st["assigned"], " ".join(map(str, st["scores"])),
                     f"{st['avg']:.2f}" if st["avg"] is not None else "",
                     DECISION_LABEL.get(s.decision or "", ""), created])
    return csv_response(rows, "submissions.csv")


@app.route("/admin/export/reviews.csv")
@admin_required
def export_reviews():
    rows = [["submission_id", "title", "reviewer", "reviewer_email", "conflict", "score",
             "confidence", "comments_to_authors", "comments_to_committee"]]
    for a in Assignment.query.order_by(Assignment.submission_id).all():
        r = a.review
        rows.append([a.submission_id, a.submission.title, a.reviewer.name, a.reviewer.email,
                     "yes" if a.conflict else "", r.score if r else "", r.confidence if r else "",
                     r.comments_author if r else "", r.comments_committee if r else ""])
    return csv_response(rows, "reviews.csv")


@app.route("/admin/export/reviewers.csv")
@admin_required
def export_reviewers():
    rows = [["name", "email", "affiliation", "keywords", "assigned", "done", "last_login"]]
    for r in User.query.filter_by(is_reviewer=True).order_by(User.name).all():
        act = [a for a in r.assignments if a.submission.active and not a.conflict]
        rows.append([r.name, r.email, r.affiliation, "; ".join(r.keyword_list), len(act),
                     sum(1 for a in act if a.review), r.last_login or ""])
    return csv_response(rows, "reviewers.csv")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
