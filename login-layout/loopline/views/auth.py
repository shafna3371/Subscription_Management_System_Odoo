import secrets
from datetime import datetime, timedelta

from flask import (Blueprint, flash, g, redirect, render_template, request,
                   session, url_for)

from ..core import (Form, hash_password, now_stamp, password_problems,
                    send_email, verify_password)
from ..db import get_db
from ..security import form_errors, login_required

bp = Blueprint("auth", __name__)


@bp.route("/")
def home():
    if g.user is None:
        return redirect(url_for("auth.login"))
    if g.user["role"] == "customer":
        return redirect(url_for("portal.index"))
    return redirect(url_for("insights.dashboard"))


def _safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") else None


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user and verify_password(request.form.get("password") or "", user["password_hash"]):
            session.clear()
            session["uid"] = user["id"]
            return redirect(_safe_next(request.args.get("next")) or url_for("auth.home"))
        flash("That email and password don't match an account.", "error")
    return render_template("auth/login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


def create_user(db, form, role):
    """Shared by public signup (customers) and admin 'add staff'. Returns (id, errors)."""
    f = Form(form)
    name = f.text("name", required=True, max_len=80)
    email = f.email()
    company = f.text("company", max_len=120)
    password = form.get("password") or ""
    for problem in password_problems(password):
        f.errors.setdefault("password", problem)
    f.check(password == (form.get("confirm") or ""), "confirm", "The two passwords don't match.")
    if email and db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        f.errors["email"] = "An account with this email already exists."
    if not f.ok:
        return None, f.errors
    cur = db.execute(
        "INSERT INTO users(name, email, password_hash, role, company, created_at) VALUES (?,?,?,?,?,?)",
        (name, email, hash_password(password), role, company or None, now_stamp()),
    )
    return cur.lastrowid, {}


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        db = get_db()
        uid, errors = create_user(db, request.form, "customer")
        if errors:
            form_errors(errors)
            return render_template("auth/signup.html", form=request.form, errors=errors), 422
        db.commit()
        session.clear()
        session["uid"] = uid
        flash("Welcome to Brightloop. Your account is ready.", "ok")
        return redirect(url_for("portal.index"))
    return render_template("auth/signup.html", form={}, errors={})


@bp.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        db = get_db()
        email = (request.form.get("email") or "").strip().lower()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user:
            token = secrets.token_urlsafe(32)
            expires = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            db.execute("INSERT INTO password_resets(token, user_id, expires_at) VALUES (?,?,?)",
                       (token, user["id"], expires))
            link = url_for("auth.reset", token=token, _external=True)
            send_email(db, email, "Reset your Brightloop password",
                       f"Hello {user['name']},\n\nUse this link within one hour to choose a new password:\n{link}\n\n"
                       "If you didn't ask for this, you can ignore this email.")
            db.commit()
        # Same message either way, so the form can't be used to discover accounts.
        flash("If that email has an account, a reset link is on its way.", "ok")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot.html")


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset(token):
    db = get_db()
    row = db.execute("SELECT * FROM password_resets WHERE token = ? AND used = 0 AND expires_at > ?",
                     (token, now_stamp())).fetchone()
    if not row:
        flash("This reset link has expired or was already used. Ask for a new one.", "error")
        return redirect(url_for("auth.forgot"))
    if request.method == "POST":
        password = request.form.get("password") or ""
        problems = password_problems(password)
        if password != request.form.get("confirm"):
            problems.append("The two passwords don't match.")
        if problems:
            for p in problems:
                flash(p, "error")
            return render_template("auth/reset.html", token=token), 422
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), row["user_id"]))
        db.execute("UPDATE password_resets SET used = 1 WHERE token = ?", (token,))
        db.commit()
        flash("Password updated. Log in with your new password.", "ok")
        return redirect(url_for("auth.login"))
    return render_template("auth/reset.html", token=token)


@bp.route("/users", methods=["GET", "POST"])
@login_required("admin")
def users():
    db = get_db()
    if request.method == "POST":
        uid, errors = create_user(db, request.form, "staff")
        if errors:
            form_errors(errors)
        else:
            db.commit()
            flash("Staff member added.", "ok")
            return redirect(url_for("auth.users"))
    team = db.execute("SELECT * FROM users WHERE role IN ('admin','staff') ORDER BY role, name").fetchall()
    customers = db.execute("SELECT COUNT(*) FROM users WHERE role = 'customer'").fetchone()[0]
    return render_template("auth/users.html", team=team, customers=customers, form=request.form)
