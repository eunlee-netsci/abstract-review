"""Email queue. Every message is stored in EmailLog first (visible in Admin > Outbox),
then sent in a background thread via Brevo HTTP API or SMTP, whichever is configured.
If neither is configured, messages stay in the outbox so links can be copied manually."""
import os
import smtplib
import ssl
import threading
from datetime import datetime
from email.message import EmailMessage

import requests

from models import EmailLog, db


def backend():
    if os.environ.get("BREVO_API_KEY"):
        return "brevo"
    if os.environ.get("SMTP_HOST"):
        return "smtp"
    return None


def queue(to, subject, body, kind=""):
    log = EmailLog(to=to, subject=subject, body=body, kind=kind, status="queued")
    db.session.add(log)
    return log


def dispatch(app, logs):
    """Call after db.session.commit() so the log rows have ids."""
    ids = [l.id for l in logs if l.id]
    if ids:
        threading.Thread(target=_worker, args=(app, ids), daemon=True).start()


def _sender():
    addr = os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER") or ""
    name = os.environ.get("MAIL_FROM_NAME", "")
    return addr, name


def _worker(app, ids):
    with app.app_context():
        logs = EmailLog.query.filter(EmailLog.id.in_(ids)).order_by(EmailLog.id).all()
        kind = backend()
        if kind is None:
            for l in logs:
                l.status = "not sent (no mail backend)"
            db.session.commit()
            return
        if kind == "brevo":
            _send_brevo(logs)
        else:
            _send_smtp(logs)


def _send_brevo(logs):
    addr, name = _sender()
    headers = {"api-key": os.environ["BREVO_API_KEY"], "accept": "application/json",
               "content-type": "application/json"}
    for l in logs:
        payload = {"sender": {"email": addr, "name": name or addr},
                   "to": [{"email": l.to}], "subject": l.subject, "textContent": l.body}
        try:
            r = requests.post("https://api.brevo.com/v3/smtp/email", json=payload,
                              headers=headers, timeout=30)
            if r.status_code < 300:
                l.status, l.sent_at, l.error = "sent", datetime.utcnow(), ""
            else:
                l.status, l.error = "failed", f"{r.status_code} {r.text[:400]}"
        except Exception as e:  # noqa: BLE001
            l.status, l.error = "failed", str(e)[:500]
        db.session.commit()


def _send_smtp(logs):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    pw = os.environ.get("SMTP_PASSWORD", "")
    addr, name = _sender()
    try:
        ctx = ssl.create_default_context()
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
        else:
            s = smtplib.SMTP(host, port, timeout=30)
            s.starttls(context=ctx)
        if user:
            s.login(user, pw)
    except Exception as e:  # noqa: BLE001
        for l in logs:
            l.status, l.error = "failed", f"SMTP connect/login: {e}"[:500]
        db.session.commit()
        return
    for l in logs:
        msg = EmailMessage()
        msg["From"] = f"{name} <{addr}>" if name else addr
        msg["To"] = l.to
        msg["Subject"] = l.subject
        msg.set_content(l.body)
        try:
            s.send_message(msg)
            l.status, l.sent_at, l.error = "sent", datetime.utcnow(), ""
        except Exception as e:  # noqa: BLE001
            l.status, l.error = "failed", str(e)[:500]
        db.session.commit()
    try:
        s.quit()
    except Exception:  # noqa: BLE001
        pass
