"""A small JSON API over the same business logic the web pages use.

Session login is reused (log in through /login first). Write calls must send
the CSRF token in an X-CSRF header. Customers only ever see their own data.

GET  /api/v1/kpis                         staff/admin
GET  /api/v1/subscriptions?status=&page=  everyone (customers: own only)
GET  /api/v1/invoices?status=&page=       everyone (customers: own only)
POST /api/v1/invoices/<id>/payments       staff/admin  {"method","amount","date","note"}
"""
from datetime import date

from flask import Blueprint, g, jsonify, request

from .. import billing, metrics
from ..billing import PAYMENT_METHODS, BillingError
from ..db import get_db

bp = Blueprint("api", __name__, url_prefix="/api/v1")
PAGE = 50


def _err(message, code):
    return jsonify({"error": message}), code


def _page():
    try:
        return max(1, int(request.args.get("page", 1)))
    except ValueError:
        return 1


@bp.before_request
def require_login():
    if g.user is None:
        return _err("Log in first.", 401)


@bp.route("/kpis")
def kpis():
    if g.user["role"] == "customer":
        return _err("Staff only.", 403)
    return jsonify(metrics.kpis(get_db()))


@bp.route("/subscriptions")
def subscriptions():
    db = get_db()
    where, params = [], []
    if g.user["role"] == "customer":
        where.append("s.customer_id = ?")
        params.append(g.user["id"])
    status = request.args.get("status")
    if status:
        where.append("s.status = ?")
        params.append(status)
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    page = _page()
    rows = db.execute(
        f"SELECT s.id, s.number, s.status, s.start_date, s.expiry_date, s.next_billing_date, p.name AS plan, "
        f"u.name AS customer FROM subscriptions s JOIN plans p ON p.id = s.plan_id JOIN users u "
        f"ON u.id = s.customer_id {sql_where} ORDER BY s.id DESC LIMIT ? OFFSET ?",
        (*params, PAGE, (page - 1) * PAGE)).fetchall()
    return jsonify({"page": page, "items": [dict(r) for r in rows]})


@bp.route("/invoices")
def invoices():
    db = get_db()
    where, params = [], []
    if g.user["role"] == "customer":
        where.append("i.customer_id = ? AND i.status <> 'draft'")
        params.append(g.user["id"])
    status = request.args.get("status")
    if status:
        where.append("i.status = ?")
        params.append(status)
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    page = _page()
    rows = db.execute(
        f"SELECT i.id, i.number, i.status, i.invoice_date, i.due_date, i.total, i.amount_paid, "
        f"u.name AS customer FROM invoices i JOIN users u ON u.id = i.customer_id {sql_where} "
        f"ORDER BY i.id DESC LIMIT ? OFFSET ?", (*params, PAGE, (page - 1) * PAGE)).fetchall()
    return jsonify({"page": page, "items": [dict(r) for r in rows]})


@bp.route("/invoices/<int:inv_id>/payments", methods=["POST"])
def add_payment(inv_id):
    if g.user["role"] == "customer":
        return _err("Staff only.", 403)
    body = request.get_json(silent=True) or {}
    method = body.get("method")
    if method not in PAYMENT_METHODS:
        return _err(f"method must be one of {sorted(PAYMENT_METHODS)}.", 422)
    try:
        amount = float(body.get("amount"))
        pay_date = date.fromisoformat(body.get("date") or "")
    except (TypeError, ValueError):
        return _err("amount must be a number and date must be YYYY-MM-DD.", 422)
    db = get_db()
    try:
        billing.record_payment(db, inv_id, method, amount, pay_date, str(body.get("note", ""))[:300],
                               g.user["id"], g.user["name"])
        db.commit()
    except BillingError as exc:
        db.rollback()
        return _err(str(exc), 422)
    return jsonify(dict(billing.invoice(db, inv_id)) | {"ok": True}), 201
