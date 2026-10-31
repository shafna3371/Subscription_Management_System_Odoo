"""Loopline - subscription management for Brightloop Software.

Flask + SQLite + scikit-learn. Runs fully offline.
"""
import json
import os
import secrets
import time

from flask import Flask, abort, g, request, session

from . import db as dbmod
from .core import today


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True,
                template_folder="../templates", static_folder="../static")
    os.makedirs(app.instance_path, exist_ok=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("LOOPLINE_SECRET") or _instance_secret(app.instance_path),
        DATABASE=os.environ.get("LOOPLINE_DB") or os.path.join(app.instance_path, "loopline.sqlite"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,   # bank CSV uploads
    )
    if test_config:
        app.config.update(test_config)

    dbmod.ensure_database(app.config["DATABASE"])
    app.teardown_appcontext(dbmod.close_db)

    @app.before_request
    def before():
        g.t0 = time.perf_counter()
        g.user = None
        uid = session.get("uid")
        if uid:
            g.user = dbmod.get_db().execute("SELECT id, name, email, role FROM users WHERE id = ?",
                                            (uid,)).fetchone()
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        if request.method == "POST" and not app.config.get("TESTING_NO_CSRF"):
            sent = request.form.get("csrf") or request.headers.get("X-CSRF")
            if sent != session["csrf"]:
                abort(400, "The form expired. Reload the page and try again.")

    @app.after_request
    def after(resp):
        if "t0" in g:
            resp.headers["X-Response-Time-ms"] = f"{(time.perf_counter() - g.t0) * 1000:.0f}"
        if request.method == "POST":
            from .metrics import clear_cache
            clear_cache()
        return resp

    @app.context_processor
    def helpers():
        def elapsed_ms():
            return f"{(time.perf_counter() - g.t0) * 1000:.0f}" if "t0" in g else "?"

        user = g.get("user")
        review = 0
        if user and user["role"] in ("admin", "staff"):
            review = dbmod.get_db().execute(
                "SELECT COUNT(*) FROM invoices WHERE status = 'draft' AND anomaly_flag = 1").fetchone()[0]
        return {
            "nav_review": review,
            "csrf_token": session.get("csrf", ""),
            "current_user": g.get("user"),
            "sim_today": today(dbmod.get_db()),
            "elapsed_ms": elapsed_ms,
            "from_json": lambda s: json.loads(s) if s else [],
        }

    @app.template_filter("money")
    def money_filter(v):
        return f"{(v or 0):,.2f}"

    @app.template_filter("pct")
    def pct_filter(v):
        return "-" if v is None else f"{v * 100:.0f}%"

    from flask import render_template

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def error_page(err):
        messages = {400: "That request couldn't be processed.", 403: "Your role doesn't have access to this page.",
                    404: "That page or record doesn't exist."}
        detail = getattr(err, "description", "") if err.code == 400 else ""
        return render_template("error.html", code=err.code, message=messages[err.code], detail=detail), err.code

    from .views import api, auth, catalog, finance, insights, portal, subs
    for bp in (auth.bp, catalog.bp, subs.bp, finance.bp, insights.bp, portal.bp, api.bp):
        app.register_blueprint(bp)

    from . import cli
    cli.register(app)
    return app


def _instance_secret(instance_path):
    path = os.path.join(instance_path, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write(secrets.token_hex(32))
    with open(path) as fh:
        return fh.read().strip()
