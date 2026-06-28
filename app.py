"""
app.py — job-hunter-portal Flask app.

Routes:
    GET  /                  Landing page
    GET  /signup            Invite code entry form
    POST /signup            Validate code, render registration form
    POST /register          Create account
    GET  /dashboard         User dashboard
    GET  /api/users         Scraper polling endpoint — X-Api-Key auth, returns active users + queries
    GET  /admin             Admin panel — seed codes, view users
    POST /admin/generate    Generate N seed codes
"""

import os
from flask import Flask, render_template, request, redirect, url_for, jsonify, session
from dotenv import load_dotenv
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash

import db

load_dotenv(Path(__file__).parent / ".env")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-change-this")


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


@app.route("/login", methods=["GET"])
def login_get():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template("login.html", error=None)


@app.route("/login", methods=["POST"])
def login_post():
    email    = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    user = db.get_user_by_email(email)
    if not user or not user["password_hash"] or not check_password_hash(user["password_hash"], password):
        return render_template("login.html", error="Email or password is incorrect.")
    session["user_id"] = user["id"]
    return redirect(url_for("dashboard"))


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
    return render_template("dashboard.html", user=user, queries=queries, codes=codes)


@app.route("/queries", methods=["GET", "POST"])
def queries():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login_get"))
    if request.method == "POST":
        query_list = []
        for i in range(1, 7):
            enabled = request.form.get(f"query_{i}_enabled")
            val = request.form.get(f"query_{i}", "").strip()
            if enabled and val:
                query_list.append(val)
        if not query_list:
            existing = [request.form.get(f"query_{i}", "") for i in range(1, 7)]
            return render_template("queries.html", queries=existing, error="Please enable and fill in at least one search query.")
        db.set_queries_for_user(user_id, query_list)
        return redirect(url_for("dashboard"))
    existing = db.get_queries_for_user(user_id)
    return render_template("queries.html", queries=existing, error=None)


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
    admin_key = os.getenv("ADMIN_KEY", "")
    return admin_key and request.args.get("key") == admin_key


@app.route("/admin")
def admin():
    if not _admin_authed():
        return "Forbidden", 403
    users = db.get_all_users()
    codes = db.get_all_codes()
    new_codes = request.args.getlist("new_codes")
    return render_template("admin.html", users=users, codes=codes, new_codes=new_codes)


@app.route("/admin/generate", methods=["POST"])
def admin_generate():
    if not _admin_authed():
        return "Forbidden", 403
    try:
        n = min(int(request.form.get("count", 1)), 20)
    except ValueError:
        n = 1
    new_codes = db.generate_codes(n, created_by_user_id=None)
    key = request.args.get("key", "")
    return redirect(url_for("admin", key=key, new_codes=new_codes))


if __name__ == "__main__":
    db.init_db()
    app.run(debug=True, host="100.91.201.73", port=5020)
