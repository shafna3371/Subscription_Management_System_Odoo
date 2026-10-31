"""Customer self-service portal: customers only ever see their own records."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import billing
from ..billing import BillingError
from ..core import d, today
from ..db import get_db
from ..security import login_required
from .finance import render_print

bp = Blueprint("portal", __name__, url_prefix="/portal")


def own_subscription(db, sub_id):
    sub = billing.subscription(db, sub_id)
    if sub is None or sub["customer_id"] != g.user["id"]:
        abort(404)  # 404, not 403: don't reveal that someone else's record exists
    return sub


@bp.route("/", methods=["GET", "POST"])
@login_required("customer")
def index():
    db = get_db()
    if request.method == "POST":
        sub = own_subscription(db, request.form.get("sub_id", type=int))
        action = request.form.get("action")
        try:
            if action == "accept":
                if sub["status"] != "quotation":
                    raise BillingError("Only open quotations can be accepted.")
                billing.set_status(db, sub["id"], "confirmed", g.user["name"])
                if d(sub["start_date"]) <= today(db):
                    billing.set_status(db, sub["id"], "active", g.user["name"])
                flash("Thanks! Your subscription is confirmed.", "ok")
            elif action == "pause":
                billing.set_status(db, sub["id"], "paused", g.user["name"], reason="paused by customer")
                flash("Subscription paused. Billing stops until you resume.", "ok")
            elif action == "resume":
                if sub["paused_reason"] == "unpaid invoices":
                    raise BillingError("This subscription resumes automatically once overdue invoices are paid.")
                billing.set_status(db, sub["id"], "active", g.user["name"])
                flash("Subscription resumed.", "ok")
            elif action == "close":
                billing.set_status(db, sub["id"], "closed", g.user["name"], reason="closed by customer")
                flash("Subscription closed.", "ok")
            else:
                raise BillingError("Unknown action.")
            db.commit()
        except BillingError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("portal.index"))
    subs = db.execute(
        "SELECT s.*, p.name AS plan_name, p.billing_period, p.pausable, p.closable, "
        "(SELECT COALESCE(SUM(quantity * unit_price), 0) FROM subscription_lines l WHERE l.subscription_id = s.id) "
        "AS value FROM subscriptions s JOIN plans p ON p.id = s.plan_id WHERE s.customer_id = ? AND s.status <> 'draft' "
        "ORDER BY s.id DESC", (g.user["id"],)).fetchall()
    lines = {s["id"]: billing.subscription_lines(db, s["id"]) for s in subs}
    invoices = db.execute("SELECT * FROM invoices WHERE customer_id = ? AND status <> 'draft' "
                          "ORDER BY invoice_date DESC LIMIT 50", (g.user["id"],)).fetchall()
    owed = sum(i["total"] - i["amount_paid"] for i in invoices if i["status"] == "confirmed")
    return render_template("portal/index.html", subs=subs, lines=lines, invoices=invoices, owed=owed,
                           today=today(db))


@bp.route("/invoices/<int:inv_id>/print")
@login_required("customer")
def print_invoice(inv_id):
    db = get_db()
    inv = billing.invoice(db, inv_id)
    if inv is None or inv["customer_id"] != g.user["id"] or inv["status"] == "draft":
        abort(404)
    return render_print(db, inv_id)
