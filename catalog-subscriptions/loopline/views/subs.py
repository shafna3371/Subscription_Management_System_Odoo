from flask import (Blueprint, abort, flash, g, redirect, render_template,
                   request, url_for)

from .. import billing
from ..billing import BillingError
from ..core import Form, add_period, d, log_activity, now_stamp, today
from ..db import get_db
from ..security import STAFF, form_errors, login_required

bp = Blueprint("subs", __name__)
PAGE = 25
STATUSES = ("draft", "quotation", "confirmed", "active", "paused", "closed")


def page_args():
    try:
        return max(1, int(request.args.get("page", 1)))
    except ValueError:
        return 1


@bp.route("/subscriptions")
@login_required(*STAFF)
def index():
    db = get_db()
    where, params = [], []
    status = request.args.get("status", "")
    plan_id = request.args.get("plan", "")
    q = (request.args.get("q") or "").strip()
    risk = request.args.get("risk") == "1"
    if status in STATUSES:
        where.append("s.status = ?")
        params.append(status)
    if plan_id.isdigit():
        where.append("s.plan_id = ?")
        params.append(int(plan_id))
    if q:
        where.append("(s.number LIKE ? OR u.name LIKE ? OR u.company LIKE ?)")
        params += [f"%{q}%"] * 3
    if risk:
        where.append("pr.score >= 0.5")
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    base = (f"FROM subscriptions s JOIN users u ON u.id = s.customer_id JOIN plans p ON p.id = s.plan_id "
            f"LEFT JOIN predictions pr ON pr.entity_type = 'subscription' AND pr.entity_id = s.id "
            f"AND pr.kind = 'churn' {sql_where}")
    total = db.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
    page = page_args()
    rows = db.execute(
        f"SELECT s.*, u.name AS customer_name, u.company, p.name AS plan_name, p.billing_period, pr.score AS risk, "
        f"(SELECT COALESCE(SUM(quantity * unit_price), 0) FROM subscription_lines l WHERE l.subscription_id = s.id) "
        f"AS value {base} ORDER BY s.id DESC LIMIT ? OFFSET ?",
        (*params, PAGE, (page - 1) * PAGE),
    ).fetchall()
    return render_template("subs/index.html", rows=rows, total=total, page=page, pages=max(1, -(-total // PAGE)),
                           statuses=STATUSES, plans=db.execute("SELECT id, name FROM plans ORDER BY name").fetchall(),
                           args=request.args)


@bp.route("/subscriptions/new", methods=["GET", "POST"])
@login_required(*STAFF)
def new():
    db = get_db()
    now = today(db)
    if request.method == "POST":
        f = Form(request.form)
        customer_id = f.ref("customer_id", db, "users", label="Customer")
        template_id = f.ref("template_id", db, "quotation_templates", required=False, label="Template")
        plan_id = f.ref("plan_id", db, "plans", required=not template_id, label="Plan")
        start = f.date("start_date", label="Start date")
        terms = f.number("payment_terms_days", minimum=0, maximum=120, integer=True, label="Payment terms")
        periods = f.number("term_periods", required=False, minimum=1, maximum=120, integer=True,
                           label="Contract length")
        auto_renew = f.flag("auto_renew")
        if customer_id and db.execute("SELECT role FROM users WHERE id = ?", (customer_id,)).fetchone()[0] != "customer":
            f.errors["customer_id"] = "Choose a customer account."
        template = None
        if template_id:
            template = db.execute("SELECT * FROM quotation_templates WHERE id = ?", (template_id,)).fetchone()
            plan_id = template["plan_id"]
            periods = periods or template["term_periods"]
        plan = db.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone() if plan_id else None
        if plan and start:
            f.check(d(plan["start_date"]) <= start, "start_date", f"The plan is available from {plan['start_date']}.")
            if plan["end_date"]:
                f.check(start <= d(plan["end_date"]), "start_date",
                        f"The plan stopped being available on {plan['end_date']}.")
        if not f.ok:
            form_errors(f.errors)
        else:
            expiry = add_period(start, plan["billing_period"], periods) if periods else None
            cur = db.execute(
                "INSERT INTO subscriptions(customer_id, plan_id, status, start_date, expiry_date, term_days, "
                "payment_terms_days, auto_renew, created_at) VALUES (?,?, 'draft', ?,?,?,?,?,?)",
                (customer_id, plan_id, start.isoformat(), expiry.isoformat() if expiry else None,
                 (expiry - start).days if expiry else None, terms, auto_renew, now_stamp()),
            )
            sub_id = cur.lastrowid
            db.execute("UPDATE subscriptions SET number = ? WHERE id = ?", (f"SUB/{sub_id:05d}", sub_id))
            if template:
                lines = db.execute("SELECT * FROM quotation_template_lines WHERE template_id = ?",
                                   (template_id,)).fetchall()
                for l in lines:
                    price = billing.line_unit_price(db, plan, l["product_id"], l["variant_id"])
                    db.execute("INSERT INTO subscription_lines(subscription_id, product_id, variant_id, quantity, "
                               "unit_price) VALUES (?,?,?,?,?)", (sub_id, l["product_id"], l["variant_id"],
                                                                 l["quantity"], price))
            else:
                db.execute("INSERT INTO subscription_lines(subscription_id, product_id, quantity, unit_price) "
                           "VALUES (?,?,?,?)", (sub_id, plan["product_id"], plan["min_qty"], plan["price"]))
            log_activity(db, "Subscription created" + (f" from template '{template['name']}'" if template else ""),
                         g.user["name"], subscription_id=sub_id)
            db.commit()
            flash("Draft subscription created. Review the lines, then send the quotation.", "ok")
            return redirect(url_for("subs.detail", sub_id=sub_id))
    return render_template(
        "subs/new.html", now=now, form=request.form,
        customers=db.execute("SELECT id, name, company FROM users WHERE role = 'customer' ORDER BY name").fetchall(),
        plans=db.execute("SELECT * FROM plans WHERE start_date <= ? AND (end_date IS NULL OR end_date >= ?) "
                         "ORDER BY name", (now.isoformat(), now.isoformat())).fetchall(),
        templates=db.execute("SELECT t.*, p.name AS plan_name FROM quotation_templates t JOIN plans p "
                             "ON p.id = t.plan_id ORDER BY t.name").fetchall())


@bp.route("/subscriptions/<int:sub_id>", methods=["GET", "POST"])
@login_required(*STAFF)
def detail(sub_id):
    db = get_db()
    sub = billing.subscription(db, sub_id) or abort(404)
    if request.method == "POST":
        try:
            message = handle_action(db, sub, request.form)
            db.commit()
            if message:
                flash(message, "ok")
        except BillingError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("subs.detail", sub_id=sub_id))
    plan = db.execute("SELECT * FROM plans WHERE id = ?", (sub["plan_id"],)).fetchone()
    risk = db.execute("SELECT * FROM predictions WHERE entity_type = 'subscription' AND entity_id = ? "
                      "AND kind = 'churn'", (sub_id,)).fetchone()
    offers = db.execute("SELECT * FROM discounts WHERE audience = 'assigned' AND active = 1 AND "
                        "(end_date IS NULL OR end_date >= ?) AND (usage_limit IS NULL OR used_count < usage_limit) "
                        "AND id NOT IN (SELECT discount_id FROM subscription_discounts WHERE subscription_id = ?)",
                        (today(db).isoformat(), sub_id)).fetchall()
    return render_template(
        "subs/detail.html", sub=sub, plan=plan, lines=billing.subscription_lines(db, sub_id),
        value=billing.recurring_value(db, sub_id), mrr=billing.monthly_value(db, sub), risk=risk, offers=offers,
        assigned=db.execute("SELECT d.* FROM discounts d JOIN subscription_discounts sd ON sd.discount_id = d.id "
                            "WHERE sd.subscription_id = ?", (sub_id,)).fetchall(),
        invoices=db.execute("SELECT * FROM invoices WHERE subscription_id = ? ORDER BY period_start DESC LIMIT 24",
                            (sub_id,)).fetchall(),
        activity=db.execute("SELECT * FROM activity WHERE subscription_id = ? ORDER BY id DESC LIMIT 40",
                            (sub_id,)).fetchall(),
        products=db.execute("SELECT id, name FROM products ORDER BY name").fetchall(),
        variants=db.execute("SELECT v.*, p.name AS product_name FROM product_variants v JOIN products p "
                            "ON p.id = v.product_id ORDER BY p.name").fetchall(),
        editable=sub["status"] in ("draft", "quotation", "active"), today=today(db))


def handle_action(db, sub, form):
    action = form.get("action")
    actor = g.user["name"]
    sub_id = sub["id"]
    if action == "add_line":
        f = Form(form)
        product_id = f.ref("product_id", db, "products", label="Product")
        variant_id = f.ref("variant_id", db, "product_variants", required=False, label="Variant")
        qty = f.number("quantity", minimum=1, maximum=100000, integer=True, label="Quantity")
        if not f.ok:
            raise BillingError(next(iter(f.errors.values())))
        if sub["status"] not in ("draft", "quotation", "active"):
            raise BillingError("Lines can only change on draft, quotation or active subscriptions.")
        plan = db.execute("SELECT * FROM plans WHERE id = ?", (sub["plan_id"],)).fetchone()
        price = billing.line_unit_price(db, plan, product_id, variant_id)
        db.execute("INSERT INTO subscription_lines(subscription_id, product_id, variant_id, quantity, unit_price) "
                   "VALUES (?,?,?,?,?)", (sub_id, product_id, variant_id, qty, price))
        log_activity(db, f"Line added: {qty} x product #{product_id} at {price:.2f}", actor, subscription_id=sub_id)
        billing.log_mrr(db, sub_id)
        return "Line added."
    if action == "set_qty":
        f = Form(form)
        qty = f.number("quantity", minimum=1, maximum=100000, integer=True, label="Quantity")
        if not f.ok:
            raise BillingError(next(iter(f.errors.values())))
        line = db.execute("SELECT * FROM subscription_lines WHERE id = ? AND subscription_id = ?",
                          (form.get("line_id"), sub_id)).fetchone()
        if not line:
            raise BillingError("Line not found.")
        db.execute("UPDATE subscription_lines SET quantity = ? WHERE id = ?", (qty, line["id"]))
        if sub["status"] == "active":
            billing.check_min_qty(db, sub)
        log_activity(db, f"Quantity changed from {line['quantity']} to {qty}", actor, subscription_id=sub_id)
        billing.log_mrr(db, sub_id)
        return "Quantity updated. The next invoice uses the new quantity."
    if action == "remove_line":
        if db.execute("SELECT COUNT(*) FROM subscription_lines WHERE subscription_id = ?", (sub_id,)).fetchone()[0] <= 1:
            raise BillingError("A subscription needs at least one line.")
        db.execute("DELETE FROM subscription_lines WHERE id = ? AND subscription_id = ?", (form.get("line_id"), sub_id))
        if sub["status"] == "active":
            billing.check_min_qty(db, sub)
        log_activity(db, "Line removed", actor, subscription_id=sub_id)
        billing.log_mrr(db, sub_id)
        return "Line removed."
    if action == "send_quote":
        days = db.execute("SELECT COALESCE(MAX(validity_days), 30) FROM quotation_templates WHERE plan_id = ?",
                          (sub["plan_id"],)).fetchone()[0]
        billing.send_quotation(db, sub_id, days, actor)
        return "Quotation sent to the customer's portal and email."
    if action == "confirm":
        billing.set_status(db, sub_id, "confirmed", actor)
        if d(sub["start_date"]) <= today(db):
            billing.set_status(db, sub_id, "active", actor)
            return "Confirmed and active. The first invoice is created on the next billing run."
        return f"Confirmed. It becomes active on {sub['start_date']}."
    if action in ("pause", "resume", "close"):
        target = {"pause": "paused", "resume": "active", "close": "closed"}[action]
        billing.set_status(db, sub_id, target, actor)
        return f"Subscription {target}."
    if action == "renew":
        billing.renew(db, sub_id, actor)
        return "Subscription renewed."
    if action == "bill_now":
        created = billing.run_billing(db, actor=actor)
        return f"Billing run finished: {len(created)} new invoice(s) across all due subscriptions."
    if action == "apply_offer":
        disc = db.execute("SELECT * FROM discounts WHERE id = ? AND audience = 'assigned'",
                          (form.get("discount_id"),)).fetchone()
        if not disc:
            raise BillingError("Choose one of the retention offers set up by the admin.")
        db.execute("INSERT OR IGNORE INTO subscription_discounts(subscription_id, discount_id) VALUES (?,?)",
                   (sub_id, disc["id"]))
        log_activity(db, f"Retention offer '{disc['name']}' applied", actor, subscription_id=sub_id)
        return "Offer applied. It appears on the next invoice automatically."
    raise BillingError("Unknown action.")
