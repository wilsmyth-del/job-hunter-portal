"""
app.py — job-hunter-portal Flask app.

Routes:
    GET  /                  Landing page
    GET  /signup            Invite code entry form
    POST /signup            Validate code, render registration form
    POST /register          Create account
    GET  /dashboard         User dashboard (queries, schedule, location all inline)
    POST /queries           Save search queries
    POST /schedule          Save delivery days
    POST /location          Save location
    POST /search/test       Run saved searches on demand, show results inline
    GET  /forgot-password   Request a password reset email
    POST /forgot-password   Send reset email if the address matches an account
    GET  /reset/<token>     Show new-password form (valid token only)
    POST /reset/<token>     Set new password, invalidate token
    POST /account/password  Change password (logged in, current password required)
    POST /account/name      Update display name
    POST /account/close     Pause digests or delete account (logged in)
    POST /account/resume    Resume digests after a pause (logged in)
    GET  /unsubscribe/<token>   Pause-or-delete choice page (from digest email footer, no login needed)
    POST /unsubscribe/<token>   Perform the chosen action
    GET  /api/users         Scraper polling endpoint — X-Api-Key auth, returns active users + queries
    GET  /admin/login       Admin passphrase form
    POST /admin/login       Check passphrase, start admin session
    GET  /admin/logout      End admin session
    GET  /admin             Admin panel — seed codes, view users
    POST /admin/generate    Generate N seed codes
"""

import os
import secrets
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash
from dotenv import load_dotenv
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash

import db
from scraper import fetch_linkedin, is_blocked
from tokens import unsubscribe_token, user_id_from_token

load_dotenv(Path(__file__).parent / ".env")

app = Flask(__name__)

_secret_key = os.getenv("SECRET_KEY")
if not _secret_key or _secret_key == "dev-change-this":
    raise RuntimeError(
        "SECRET_KEY environment variable must be set to a real secret value "
        "(not unset, not 'dev-change-this') before this app will start."
    )
app.secret_key = _secret_key

HOUR_CHOICES = [(h, f"{h % 12 or 12}:00 {'AM' if h < 12 else 'PM'}") for h in range(5, 11)]

LOCATION_CHOICES = [
    "Vancouver, BC",
    "Burnaby, BC",
    "Surrey, BC",
    "New Westminster, BC",
    "Richmond, BC",
    "Coquitlam, BC",
    "British Columbia",
    "Canada",
]


@app.before_request
def ensure_db():
    db.init_db()


@app.context_processor
def inject_current_user():
    user_id = session.get("user_id")
    if user_id:
        return {"current_user": db.get_user_by_id(user_id)}
    return {"current_user": None}


# ── Public ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template("index.html")


def _safe_next(next_url: str) -> str:
    """Only allow redirecting back into the unsubscribe flow — anything else
    is ignored, to avoid this becoming an open redirect."""
    if next_url and next_url.startswith("/unsubscribe/"):
        return next_url
    return ""


@app.route("/login", methods=["GET"])
def login_get():
    next_url = _safe_next(request.args.get("next", ""))
    if session.get("user_id"):
        return redirect(next_url or url_for("dashboard"))
    return render_template("login.html", error=None, next=next_url)


@app.route("/login", methods=["POST"])
def login_post():
    email    = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    next_url = _safe_next(request.form.get("next", ""))
    user = db.get_user_by_email(email)
    if not user or not user["password_hash"] or not check_password_hash(user["password_hash"], password):
        return render_template("login.html", error="Email or password is incorrect.", next=next_url)
    session["user_id"] = user["id"]
    return redirect(next_url or url_for("dashboard"))


@app.route("/signup", methods=["GET"])
def signup_get():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template("signup.html", error=None)


@app.route("/signup", methods=["POST"])
def signup_post():
    code = request.form.get("invite_code", "").strip().upper()
    row = db.get_invite_code(code)
    if not row:
        return render_template("signup.html", error="Invalid invite code.")
    if row["used_by_user_id"]:
        return render_template("signup.html", error="That invite code has already been used.")
    return render_template("register.html", invite_code=code, error=None)


@app.route("/register", methods=["POST"])
def register():
    code     = request.form.get("invite_code", "").strip().upper()
    name     = request.form.get("name", "").strip()
    email    = request.form.get("email", "").strip().lower()
    location = request.form.get("location", "").strip()
    password = request.form.get("password", "")
    confirm  = request.form.get("confirm_password", "")

    row = db.get_invite_code(code)
    if not row or row["used_by_user_id"]:
        return render_template("signup.html", error="Invalid or already-used invite code.")

    if not name or not email or not location or not password:
        return render_template("register.html", invite_code=code, error="Please fill in all fields.")

    if password != confirm:
        return render_template("register.html", invite_code=code, error="Passwords do not match.")

    if len(password) < 8:
        return render_template("register.html", invite_code=code, error="Password must be at least 8 characters.")

    if db.get_user_by_email(email):
        return render_template("register.html", invite_code=code, error="An account with that email already exists.")

    password_hash = generate_password_hash(password)
    user_id = db.create_user(name, email, location, code, password_hash)
    db.use_invite_code(code, user_id)
    db.generate_codes(2, created_by_user_id=user_id)

    session["user_id"] = user_id
    return redirect(url_for("queries"))


# ── Authenticated ─────────────────────────────────────────────────────────────

@app.route("/dashboard")
def dashboard():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("signup_get"))
    user = db.get_user_by_id(user_id)
    if not user:
        session.clear()
        return redirect(url_for("signup_get"))
    queries = db.get_queries_for_user(user_id)
    codes   = db.get_codes_for_user(user_id)
    return render_template(
        "dashboard.html", user=user, queries=queries, codes=codes,
        location_choices=LOCATION_CHOICES, hour_choices=HOUR_CHOICES, test_results=None,
    )


@app.route("/queries", methods=["POST"])
def queries():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login_get"))
    query_list = []
    for i in range(1, 7):
        enabled = request.form.get(f"query_{i}_enabled")
        val = request.form.get(f"query_{i}", "").strip()
        if enabled and val:
            query_list.append(val)
    if not query_list:
        flash("Please enable and fill in at least one search query.", "queries")
        return redirect(url_for("dashboard"))
    db.set_queries_for_user(user_id, query_list)
    return redirect(url_for("dashboard"))


@app.route("/search/test", methods=["POST"])
def search_test():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login_get"))
    user = db.get_user_by_id(user_id)
    query_list = db.get_queries_for_user(user_id)
    if not query_list:
        flash("Save at least one search query first.", "queries")
        return redirect(url_for("dashboard"))

    seen_urls = set()
    test_results = []
    for q in query_list[:6]:
        for job in fetch_linkedin(q, user["location"]):
            if job["url"] in seen_urls or is_blocked(job):
                continue
            seen_urls.add(job["url"])
            test_results.append(job)

    codes = db.get_codes_for_user(user_id)
    return render_template(
        "dashboard.html", user=user, queries=query_list, codes=codes,
        location_choices=LOCATION_CHOICES, hour_choices=HOUR_CHOICES, test_results=test_results,
    )


@app.route("/schedule", methods=["POST"])
def schedule():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login_get"))
    bitmask = "".join(
        "1" if request.form.get(f"day_{i}") else "0"
        for i in range(7)
    )
    if "1" not in bitmask:
        flash("Please select at least one day.", "schedule")
        return redirect(url_for("dashboard"))

    valid_hours = {h for h, _ in HOUR_CHOICES}
    try:
        hour = int(request.form.get("delivery_hour", ""))
    except ValueError:
        hour = None
    if hour not in valid_hours:
        flash("Please choose a valid delivery time.", "schedule")
        return redirect(url_for("dashboard"))

    db.set_delivery_days(user_id, bitmask)
    db.set_delivery_hour(user_id, hour)
    return redirect(url_for("dashboard"))


@app.route("/location", methods=["POST"])
def location():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login_get"))
    selected = request.form.get("location_select", "")
    if selected == "other":
        new_location = request.form.get("location_other", "").strip()
    else:
        new_location = selected.strip()
    if not new_location or len(new_location) > 100:
        flash("Please choose or enter a valid location.", "location")
        return redirect(url_for("dashboard"))
    db.set_location(user_id, new_location)
    return redirect(url_for("dashboard"))


@app.route("/feedback", methods=["GET", "POST"])
def feedback():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login_get"))
    user = db.get_user_by_id(user_id)
    if request.method == "POST":
        subject = request.form.get("subject", "").strip()
        message = request.form.get("message", "").strip()
        if not subject or not message:
            return render_template("feedback.html", user=user, error="Please fill in both fields.", sent=False)
        gmail_user = os.getenv("GMAIL_USER", "")
        gmail_pass = os.getenv("GMAIL_APP_PASSWORD", "")
        if gmail_user and gmail_pass:
            try:
                msg = MIMEText(f"From: {user['name']} <{user['email']}>\n\n{message}")
                msg["Subject"] = f"[Job Portal] {subject}"
                msg["From"]    = gmail_user
                msg["To"]      = gmail_user
                msg["Reply-To"] = user["email"]
                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                    smtp.login(gmail_user, gmail_pass)
                    smtp.send_message(msg)
            except Exception:
                pass
        return render_template("feedback.html", user=user, error=None, sent=True)
    return render_template("feedback.html", user=user, error=None, sent=False)


def _send_mail(to_email: str, subject: str, plain_body: str) -> bool:
    gmail_user = os.getenv("GMAIL_USER", "")
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD", "")
    if not gmail_user or not gmail_pass:
        return False
    try:
        msg = MIMEText(plain_body)
        msg["Subject"] = subject
        msg["From"] = f"Job Finder <{gmail_user}>"
        msg["To"] = to_email
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(gmail_user, gmail_pass)
            smtp.send_message(msg)
        return True
    except Exception:
        return False


def _reset_token_valid(user) -> bool:
    expires = user["reset_token_expires"] if user else None
    if not expires:
        return False
    try:
        return datetime.utcnow() < datetime.fromisoformat(expires)
    except ValueError:
        return False


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = db.get_user_by_email(email)
        if user:
            token = secrets.token_urlsafe(32)
            expires = (datetime.utcnow() + timedelta(hours=1)).isoformat()
            db.set_reset_token(user["id"], token, expires)
            reset_url = url_for("reset_password", token=token, _external=True)
            _send_mail(
                user["email"],
                "Reset your Job Finder password",
                f"Hi {user['name']},\n\n"
                f"Someone (hopefully you) asked to reset your Job Finder password.\n"
                f"This link works for 1 hour:\n\n{reset_url}\n\n"
                f"If you didn't request this, you can ignore this email.",
            )
        # Same response either way — don't reveal whether the email exists.
        return render_template("forgot_password.html", sent=True)
    return render_template("forgot_password.html", sent=False)


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    user = db.get_user_by_reset_token(token)
    if not _reset_token_valid(user):
        return render_template("reset_password.html", valid=False, error=None)
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if password != confirm:
            return render_template("reset_password.html", valid=True, error="Passwords do not match.")
        if len(password) < 8:
            return render_template("reset_password.html", valid=True, error="Password must be at least 8 characters.")
        db.set_password(user["id"], generate_password_hash(password))
        db.clear_reset_token(user["id"])
        flash("Password updated — please log in.")
        return redirect(url_for("login_get"))
    return render_template("reset_password.html", valid=True, error=None)


@app.route("/account/password", methods=["POST"])
def change_password():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login_get"))
    user = db.get_user_by_id(user_id)
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_new_password", "")
    if not user["password_hash"] or not check_password_hash(user["password_hash"], current):
        flash("Current password is incorrect.", "account")
        return redirect(url_for("dashboard"))
    if new != confirm:
        flash("New passwords do not match.", "account")
        return redirect(url_for("dashboard"))
    if len(new) < 8:
        flash("New password must be at least 8 characters.", "account")
        return redirect(url_for("dashboard"))
    db.set_password(user_id, generate_password_hash(new))
    flash("Password updated.", "account")
    return redirect(url_for("dashboard"))


@app.route("/account/name", methods=["POST"])
def update_name():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login_get"))
    name = request.form.get("name", "").strip()
    if not name or len(name) > 100:
        flash("Please enter a valid name.", "account")
        return redirect(url_for("dashboard"))
    db.set_name(user_id, name)
    return redirect(url_for("dashboard"))


def _close_account(user, mode: str):
    if mode == "pause":
        db.set_active(user["id"], False)
        return
    codes = db.generate_codes(1, created_by_user_id=None)
    _send_mail(
        user["email"],
        "Sorry to see you go — here's a way back in",
        f"Hi {user['name']},\n\n"
        f"Your Job Finder account has been deleted, along with your saved searches.\n"
        f"If you ever want to come back, here's a fresh invite code just for you:\n\n"
        f"  {codes[0]}\n\n"
        f"No rush — it doesn't expire.",
    )
    db.delete_user(user["id"])


@app.route("/account/close", methods=["POST"])
def account_close():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login_get"))
    user = db.get_user_by_id(user_id)
    mode = request.form.get("mode")
    if mode not in ("pause", "delete"):
        flash("Please choose an option.", "account")
        return redirect(url_for("dashboard"))
    _close_account(user, mode)
    if mode == "delete":
        session.clear()
        return redirect(url_for("index"))
    return redirect(url_for("dashboard"))


@app.route("/account/resume", methods=["POST"])
def account_resume():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login_get"))
    db.set_active(user_id, True)
    return redirect(url_for("dashboard"))


@app.route("/unsubscribe/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    user_id = user_id_from_token(token)
    user = db.get_user_by_id(user_id) if user_id else None
    if not user:
        return render_template("unsubscribe.html", valid=False)
    if request.method == "POST":
        mode = request.form.get("mode")
        if mode not in ("pause", "delete"):
            return render_template("unsubscribe.html", valid=True, user=user, error="Please choose an option.")
        # Deletion is permanent (queries/seen_jobs are gone), so the link
        # alone isn't enough — if the email got forwarded, the forwarder
        # shouldn't be able to delete an account that isn't theirs. Pausing
        # is fully reversible, so it stays link-only for the low-friction
        # "forgot my password, just let me leave" case.
        if mode == "delete" and session.get("user_id") != user["id"]:
            return render_template("unsubscribe.html", valid=True, user=user, needs_login=True, token=token)
        _close_account(user, mode)
        return render_template("unsubscribe.html", valid=True, done=True, mode=mode)
    return render_template("unsubscribe.html", valid=True, user=user, error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ── API (scraper polling) ─────────────────────────────────────────────────────

@app.route("/api/users")
def api_users():
    api_key = os.getenv("PORTAL_API_KEY", "")
    if not api_key or request.headers.get("X-Api-Key") != api_key:
        return jsonify({"error": "unauthorized"}), 401

    users = db.get_all_active_users()
    payload = []
    for u in users:
        payload.append({
            "name":     u["name"],
            "email":    u["email"],
            "location": u["location"],
            "queries":  db.get_queries_for_user(u["id"]),
        })
    return jsonify(payload)


# ── Admin ─────────────────────────────────────────────────────────────────────

def _admin_authed():
    return session.get("is_admin", False)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        admin_key = os.getenv("ADMIN_KEY", "")
        if admin_key and request.form.get("key") == admin_key:
            session["is_admin"] = True
            return redirect(url_for("admin"))
        return render_template("admin_login.html", error="Wrong key.")
    return render_template("admin_login.html", error=None)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
def admin():
    if not _admin_authed():
        return redirect(url_for("admin_login"))
    users = db.get_all_users()
    codes = db.get_all_codes()
    new_codes = request.args.getlist("new_codes")
    return render_template("admin.html", users=users, codes=codes, new_codes=new_codes)


@app.route("/admin/generate", methods=["POST"])
def admin_generate():
    if not _admin_authed():
        return redirect(url_for("admin_login"))
    try:
        n = min(int(request.form.get("count", 1)), 20)
    except ValueError:
        n = 1
    new_codes = db.generate_codes(n, created_by_user_id=None)
    return redirect(url_for("admin", new_codes=new_codes))


if __name__ == "__main__":
    db.init_db()
    app.run(debug=True, host="100.91.201.73", port=5020)
