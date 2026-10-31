"""The billing engine: subscription lifecycle, invoice generation, discounts,
taxes, payments and dunning (automatic payment chasing).

Correctness rules
-----------------
1. An invoice is identified by (subscription_id, period_start). The database
   rejects a second invoice for the same period, so running billing twice,
   or after a crash, can never double-bill.
2. Billing walks forward period by period until it catches up with today, so
   if the server was down for days, every missed period is billed exactly once.
3. Discount usage is counted with a conditional UPDATE inside the same
   transaction, so a usage limit can never be exceeded.
"""
import json
import sqlite3
from datetime import timedelta

from .core import (MONTHLY_FACTOR, add_period, d, log_activity, money,
                   send_email, today)
from .db import get_setting

# Which status changes are allowed. Anything else is refused.
TRANSITIONS = {
    "draft": {"quotation", "confirmed", "closed"},
    "quotation": {"confirmed", "draft", "closed"},
    "confirmed": {"active", "closed"},
    "active": {"paused", "closed"},
    "paused": {"active", "closed"},
    "closed": set(),
}


class BillingError(Exception):
    """A rule was broken; the message is safe to show to the user."""


# ------------------------------------------------------------------ lookups


def subscription(db, sub_id):
    return db.execute(
        "SELECT s.*, p.name AS plan_name, p.billing_period, p.min_qty, p.closable, p.pausable, "
        "p.renewable, p.auto_close, u.name AS customer_name, u.email AS customer_email "
        "FROM subscriptions s JOIN plans p ON p.id = s.plan_id JOIN users u ON u.id = s.customer_id "
        "WHERE s.id = ?",
        (sub_id,),
    ).fetchone()


def subscription_lines(db, sub_id):
    return db.execute(
        "SELECT l.*, pr.name AS product_name, v.attribute, v.value AS variant_value, "
        "COALESCE(t.rate, 0) AS tax_rate "
        "FROM subscription_lines l JOIN products pr ON pr.id = l.product_id "
        "LEFT JOIN product_variants v ON v.id = l.variant_id "
        "LEFT JOIN taxes t ON t.id = pr.tax_id WHERE l.subscription_id = ? ORDER BY l.id",
        (sub_id,),
    ).fetchall()


def line_unit_price(db, plan, product_id, variant_id):
    """Plan's own product is priced by the plan; add-ons by their sales price; variants add extra."""
    if product_id == plan["product_id"]:
        base = plan["price"]
    else:
        base = db.execute("SELECT sales_price FROM products WHERE id = ?", (product_id,)).fetchone()[0]
    extra = 0.0
    if variant_id:
        row = db.execute(
            "SELECT extra_price FROM product_variants WHERE id = ? AND product_id = ?",
            (variant_id, product_id),
        ).fetchone()
        if not row:
            raise BillingError("That variant does not belong to the chosen product.")
        extra = row[0]
    return money(base + extra)


def recurring_value(db, sub_id):
    """Pre-tax, pre-discount value of one billing period."""
    row = db.execute(
        "SELECT COALESCE(SUM(quantity * unit_price), 0) FROM subscription_lines WHERE subscription_id = ?",
        (sub_id,),
    ).fetchone()
    return money(row[0])


def monthly_value(db, sub):
    return money(recurring_value(db, sub["id"]) * MONTHLY_FACTOR[sub["billing_period"]])


def log_mrr(db, sub_id, on=None):
    """Record the subscription's current MRR (0 unless active) for revenue history."""
    sub = subscription(db, sub_id)
    value = monthly_value(db, sub) if sub["status"] == "active" else 0.0
    last = db.execute(
        "SELECT mrr FROM mrr_log WHERE subscription_id = ? ORDER BY effective_date DESC, id DESC LIMIT 1",
        (sub_id,),
    ).fetchone()
    if last is None and value == 0:
        return
    if last is not None and abs(last["mrr"] - value) < 0.005:
        return
    db.execute(
        "INSERT INTO mrr_log(subscription_id, effective_date, mrr) VALUES (?,?,?)",
        (sub_id, (on or today(db)).isoformat(), value),
    )


# ------------------------------------------------------------ lifecycle


def set_status(db, sub_id, new_status, actor="system", reason=None):
    sub = subscription(db, sub_id)
    if sub is None:
        raise BillingError("Subscription not found.")
    old = sub["status"]
    if new_status not in TRANSITIONS[old]:
        raise BillingError(f"A {old} subscription cannot move to {new_status}.")
    now = today(db)
    if new_status == "confirmed":
        if not db.execute("SELECT 1 FROM subscription_lines WHERE subscription_id = ?", (sub_id,)).fetchone():
            raise BillingError("Add at least one product line before confirming.")
        if old == "quotation" and sub["quote_valid_until"] and d(sub["quote_valid_until"]) < now:
            raise BillingError("This quotation has expired. Send a new quotation first.")
        check_min_qty(db, sub)
    if new_status == "paused" and not sub["pausable"]:
        raise BillingError(f"The {sub['plan_name']} plan does not allow pausing.")
    if new_status == "closed" and old in ("active", "paused") and not sub["closable"] and actor != "system":
        raise BillingError(f"The {sub['plan_name']} plan cannot be closed early.")

    fields = {"status": new_status}
    if new_status == "closed":
        fields["closed_at"] = now.isoformat()
    if new_status == "paused":
        fields["paused_reason"] = reason or "manual"
    if new_status == "active":
        fields["paused_reason"] = None
        if old == "confirmed":
            fields["next_billing_date"] = sub["start_date"]
        elif sub["next_billing_date"] and d(sub["next_billing_date"]) < now:
            # Resuming after a pause: don't bill the paused weeks.
            fields["next_billing_date"] = now.isoformat()
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE subscriptions SET {sets} WHERE id = ?", (*fields.values(), sub_id))
    log_activity(db, f"Status changed from {old} to {new_status}" + (f" ({reason})" if reason else ""),
                 actor, subscription_id=sub_id)
    log_mrr(db, sub_id)


def check_min_qty(db, sub):
    plan = db.execute("SELECT * FROM plans WHERE id = ?", (sub["plan_id"],)).fetchone()
    qty = db.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM subscription_lines WHERE subscription_id = ? AND product_id = ?",
        (sub["id"], plan["product_id"]),
    ).fetchone()[0]
    if qty < plan["min_qty"]:
        raise BillingError(f"The {plan['name']} plan needs a minimum quantity of {plan['min_qty']}.")


def send_quotation(db, sub_id, validity_days, actor):
    sub = subscription(db, sub_id)
    if sub["status"] not in ("draft", "quotation"):
        raise BillingError("Only draft subscriptions can be sent as a quotation.")
    if not db.execute("SELECT 1 FROM subscription_lines WHERE subscription_id = ?", (sub_id,)).fetchone():
        raise BillingError("Add at least one product line before sending a quotation.")
    valid_until = today(db) + timedelta(days=validity_days)
    db.execute("UPDATE subscriptions SET quote_valid_until = ? WHERE id = ?", (valid_until.isoformat(), sub_id))
    if sub["status"] == "draft":
        set_status(db, sub_id, "quotation", actor)
    send_email(db, sub["customer_email"], f"Your quotation {sub['number']}",
               f"Hello {sub['customer_name']},\n\nYour quotation {sub['number']} for the {sub['plan_name']} plan "
               f"is ready in your portal and is valid until {valid_until}.\n\nBrightloop Software")


def activate_due(db, on):
    """Confirmed subscriptions become active on their start date."""
    rows = db.execute(
        "SELECT id FROM subscriptions WHERE status = 'confirmed' AND start_date <= ?", (on.isoformat(),)
    ).fetchall()
    for r in rows:
        set_status(db, r["id"], "active")


def renew(db, sub_id, actor="system"):
    sub = subscription(db, sub_id)
    if not sub["renewable"]:
        raise BillingError(f"The {sub['plan_name']} plan cannot be renewed.")
    if not sub["expiry_date"]:
        raise BillingError("This subscription has no end date, so it renews automatically.")
    term = sub["term_days"] or (d(sub["expiry_date"]) - d(sub["start_date"])).days
    new_expiry = d(sub["expiry_date"]) + timedelta(days=term)
    db.execute("UPDATE subscriptions SET expiry_date = ? WHERE id = ?", (new_expiry.isoformat(), sub_id))
    log_activity(db, f"Renewed until {new_expiry}", actor, subscription_id=sub_id)


def process_expiries(db, on):
    rows = db.execute(
        "SELECT s.id, s.auto_renew, p.renewable, p.auto_close FROM subscriptions s JOIN plans p ON p.id = s.plan_id "
        "WHERE s.status IN ('active','paused') AND s.expiry_date IS NOT NULL AND s.expiry_date <= ?",
        (on.isoformat(),),
    ).fetchall()
    for r in rows:
        if r["auto_renew"] and r["renewable"]:
            renew(db, r["id"])
        elif r["auto_close"]:
            set_status(db, r["id"], "closed", reason="reached end date")


# ------------------------------------------------------------- discounts


def eligible_discounts(db, sub, lines, on):
    """Discounts that apply to this subscription for a period starting on `on`."""
    rows = db.execute(
        "SELECT * FROM discounts WHERE active = 1 AND start_date <= ? AND (end_date IS NULL OR end_date >= ?) "
        "AND (usage_limit IS NULL OR used_count < usage_limit) "
        "AND (audience = 'all' OR id IN (SELECT discount_id FROM subscription_discounts WHERE subscription_id = ?)) "
        "ORDER BY id",
        (on.isoformat(), on.isoformat(), sub["id"]),
    ).fetchall()
    result = []
    for disc in rows:
        if disc["product_id"]:
            target = [l for l in lines if l["product_id"] == disc["product_id"]]
        else:
            target = list(lines)
        if not target:
            continue
        qty = sum(l["quantity"] for l in target)
        base = sum(l["quantity"] * l["unit_price"] for l in target)
        if qty < disc["min_qty"] or base < disc["min_purchase"] or base <= 0:
            continue
        amount = base * disc["value"] / 100.0 if disc["kind"] == "percent" else min(disc["value"], base)
        result.append((disc, target, money(amount)))
    return result


# --------------------------------------------------------------- invoices


def next_invoice_number(db, on):
    n = db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM invoices").fetchone()[0]
    return f"INV/{on.year}/{n:06d}"


def create_invoice(db, sub, period_start, on, actor="system"):
    """Create the invoice for one billing period. Returns the invoice id, or None
    if this period was already invoiced (the UNIQUE constraint said no)."""
    period_end = add_period(period_start, sub["billing_period"])
    lines = subscription_lines(db, sub["id"])
    if not lines:
        return None
    due = on + timedelta(days=sub["payment_terms_days"])
    try:
        cur = db.execute(
            "INSERT INTO invoices(number, subscription_id, customer_id, period_start, period_end, invoice_date, "
            "due_date, status) VALUES (?,?,?,?,?,?,?, 'draft')",
            (next_invoice_number(db, on), sub["id"], sub["customer_id"], period_start.isoformat(),
             period_end.isoformat(), on.isoformat(), due.isoformat()),
        )
    except sqlite3.IntegrityError:
        return None  # already billed: idempotent by design
    invoice_id = cur.lastrowid

    # Spread every discount across its target lines in proportion to their value.
    line_discount = {l["id"]: 0.0 for l in lines}
    for disc, target, amount in eligible_discounts(db, sub, lines, period_start):
        claimed = db.execute(
            "UPDATE discounts SET used_count = used_count + 1 "
            "WHERE id = ? AND (usage_limit IS NULL OR used_count < usage_limit)",
            (disc["id"],),
        ).rowcount
        if not claimed:
            continue
        base = sum(l["quantity"] * l["unit_price"] for l in target)
        for l in target:
            line_discount[l["id"]] += amount * (l["quantity"] * l["unit_price"]) / base
        db.execute("INSERT INTO discount_usages(discount_id, invoice_id, amount) VALUES (?,?,?)",
                   (disc["id"], invoice_id, amount))
        log_activity(db, f"Discount '{disc['name']}' applied: -{amount:.2f}", actor,
                     subscription_id=sub["id"], invoice_id=invoice_id, on=on)

    subtotal = discount_total = tax_total = 0.0
    for l in lines:
        gross = money(l["quantity"] * l["unit_price"])
        disc_amt = money(min(line_discount[l["id"]], gross))
        taxable = gross - disc_amt
        tax = money(taxable * l["tax_rate"] / 100.0)
        label = l["product_name"] + (f" ({l['attribute']}: {l['variant_value']})" if l["variant_value"] else "")
        db.execute(
            "INSERT INTO invoice_lines(invoice_id, product_id, description, quantity, unit_price, discount, "
            "tax_rate, tax_amount, amount) VALUES (?,?,?,?,?,?,?,?,?)",
            (invoice_id, l["product_id"], f"{label}, {period_start} to {period_end}", l["quantity"],
             l["unit_price"], disc_amt, l["tax_rate"], tax, money(taxable + tax)),
        )
        subtotal += gross
        discount_total += disc_amt
        tax_total += tax
    total = money(subtotal - discount_total + tax_total)
    db.execute(
        "UPDATE invoices SET subtotal = ?, discount_total = ?, tax_total = ?, total = ? WHERE id = ?",
        (money(subtotal), money(discount_total), money(tax_total), total, invoice_id),
    )
    log_activity(db, f"Invoice for {period_start} to {period_end} created ({total:.2f})", actor,
                 subscription_id=sub["id"], invoice_id=invoice_id, on=on)
    return invoice_id


def run_billing(db, on=None, actor="system", stats=None):
    """Bill every active subscription that is due, catching up missed periods.
    Safe to run any number of times. Returns the ids of new invoices."""
    from . import ml  # local import: ml depends on billing helpers

    on = on or today(db)
    created = []
    due_subs = db.execute(
        "SELECT id FROM subscriptions WHERE status = 'active' AND next_billing_date IS NOT NULL "
        "AND next_billing_date <= ?",
        (on.isoformat(),),
    ).fetchall()
    for row in due_subs:
        sub = subscription(db, row["id"])
        period_start = d(sub["next_billing_date"])
        expiry = d(sub["expiry_date"])
        while period_start <= on and (expiry is None or period_start < expiry):
            new_id = create_invoice(db, sub, period_start, on, actor)
            if new_id:
                created.append(new_id)
            elif stats is not None:
                stats["already_billed"] = stats.get("already_billed", 0) + 1
            period_start = add_period(period_start, sub["billing_period"])
        db.execute("UPDATE subscriptions SET next_billing_date = ? WHERE id = ?",
                   (period_start.isoformat(), sub["id"]))

    auto_confirm = get_setting(db, "auto_confirm", "1") == "1"
    seeding = get_setting(db, "seeding") == "1"  # generated demo history is not reviewed
    for inv_id in created:
        reasons = [] if seeding else ml.invoice_anomaly_reasons(db, inv_id)
        if reasons:
            db.execute("UPDATE invoices SET anomaly_flag = 1, anomaly_reasons = ? WHERE id = ?",
                       (json.dumps(reasons), inv_id))
        elif auto_confirm:
            confirm_invoice(db, inv_id, actor)
            send_invoice(db, inv_id, actor)
    return created


def invoice(db, inv_id):
    return db.execute(
        "SELECT i.*, u.name AS customer_name, u.email AS customer_email, u.company, s.number AS sub_number "
        "FROM invoices i JOIN users u ON u.id = i.customer_id JOIN subscriptions s ON s.id = i.subscription_id "
        "WHERE i.id = ?",
        (inv_id,),
    ).fetchone()


def confirm_invoice(db, inv_id, actor="system"):
    inv = invoice(db, inv_id)
    if inv["status"] != "draft":
        raise BillingError("Only draft invoices can be confirmed.")
    db.execute("UPDATE invoices SET status = 'confirmed', anomaly_flag = 0 WHERE id = ?", (inv_id,))
    log_activity(db, f"Invoice {inv['number']} confirmed", actor, inv["subscription_id"], inv_id)


def cancel_invoice(db, inv_id, actor="system"):
    inv = invoice(db, inv_id)
    if inv["status"] not in ("draft", "confirmed"):
        raise BillingError("Paid or cancelled invoices cannot be cancelled.")
    if inv["amount_paid"] > 0:
        raise BillingError("This invoice has payments recorded against it, so it cannot be cancelled.")
    # The row stays (status 'cancelled'), so the period is never billed again.
    db.execute("UPDATE invoices SET status = 'cancelled', anomaly_flag = 0 WHERE id = ?", (inv_id,))
    log_activity(db, f"Invoice {inv['number']} cancelled", actor, inv["subscription_id"], inv_id)


def send_invoice(db, inv_id, actor="system"):
    inv = invoice(db, inv_id)
    if inv["status"] not in ("confirmed", "paid"):
        raise BillingError("Confirm the invoice before sending it.")
    send_email(db, inv["customer_email"], f"Invoice {inv['number']}",
               f"Hello {inv['customer_name']},\n\nInvoice {inv['number']} for {inv['total']:.2f} is due on "
               f"{inv['due_date']}. You can view and print it in your portal.\n\nBrightloop Software")
    db.execute("UPDATE invoices SET sent_at = ? WHERE id = ?", (today(db).isoformat(), inv_id))


# --------------------------------------------------------------- payments

PAYMENT_METHODS = {"bank_transfer": "Bank transfer", "card": "Card", "upi": "UPI", "cash": "Cash",
                   "cheque": "Cheque"}


def record_payment(db, inv_id, method, amount, pay_date, note="", user_id=None, actor="system"):
    inv = invoice(db, inv_id)
    if inv is None:
        raise BillingError("Invoice not found.")
    if inv["status"] != "confirmed":
        raise BillingError("Payments can only be recorded against confirmed, unpaid invoices.")
    outstanding = money(inv["total"] - inv["amount_paid"])
    amount = money(amount)
    if amount <= 0:
        raise BillingError("Payment amount must be more than zero.")
    if amount > outstanding + 0.005:
        raise BillingError(f"Payment is more than the {outstanding:.2f} still owed on this invoice.")
    if pay_date > today(db):
        raise BillingError("Payment date cannot be in the future.")
    if method not in PAYMENT_METHODS:
        raise BillingError("Choose a valid payment method.")
    db.execute("INSERT INTO payments(invoice_id, method, amount, date, note, created_by) VALUES (?,?,?,?,?,?)",
               (inv_id, method, amount, pay_date.isoformat(), note, user_id))
    paid = money(inv["amount_paid"] + amount)
    if paid >= inv["total"] - 0.005:
        db.execute("UPDATE invoices SET amount_paid = ?, status = 'paid', paid_date = ? WHERE id = ?",
                   (paid, pay_date.isoformat(), inv_id))
        log_activity(db, f"Invoice {inv['number']} paid in full", actor, inv["subscription_id"], inv_id)
        resume_if_cleared(db, inv["subscription_id"])
    else:
        db.execute("UPDATE invoices SET amount_paid = ? WHERE id = ?", (paid, inv_id))
        log_activity(db, f"Partial payment of {amount:.2f} on {inv['number']}", actor,
                     inv["subscription_id"], inv_id)


def resume_if_cleared(db, sub_id):
    """A subscription paused for non-payment comes back once nothing is overdue."""
    sub = subscription(db, sub_id)
    if sub["status"] != "paused" or sub["paused_reason"] != "unpaid invoices":
        return
    overdue = db.execute(
        "SELECT 1 FROM invoices WHERE subscription_id = ? AND status = 'confirmed' AND due_date < ?",
        (sub_id, today(db).isoformat()),
    ).fetchone()
    if not overdue:
        set_status(db, sub_id, "active", reason="overdue invoices paid")


# ---------------------------------------------------------------- dunning

DUNNING_STEPS = [  # (level, days after due date, subject)
    (1, 1, "Friendly reminder: invoice {n} is now due"),
    (2, 7, "Second reminder: invoice {n} is 7 days overdue"),
    (3, 14, "Final notice: invoice {n} is 14 days overdue"),
]


def run_dunning(db, on):
    open_invoices = db.execute(
        "SELECT i.id, i.number, i.due_date, i.total, i.amount_paid, i.subscription_id, u.email, u.name, "
        "(SELECT score FROM predictions WHERE entity_type = 'invoice' AND entity_id = i.id "
        " AND kind = 'late_payment') AS late_risk "
        "FROM invoices i JOIN users u ON u.id = i.customer_id WHERE i.status = 'confirmed'"
    ).fetchall()
    for inv in open_invoices:
        due = d(inv["due_date"])
        owed = inv["total"] - inv["amount_paid"]
        days_late = (on - due).days
        steps = list(DUNNING_STEPS)
        # Smart pre-due nudge, only for invoices the model expects to be paid late.
        if inv["late_risk"] is not None and inv["late_risk"] >= 0.6:
            steps.insert(0, (0, -3, "Heads up: invoice {n} is due in 3 days"))
        for level, offset, subject in steps:
            if days_late < offset:
                continue
            try:
                db.execute("INSERT INTO reminders(invoice_id, level, sent_on) VALUES (?,?,?)",
                           (inv["id"], level, on.isoformat()))
            except sqlite3.IntegrityError:
                continue  # this reminder level was already sent
            send_email(db, inv["email"], subject.format(n=inv["number"]),
                       f"Hello {inv['name']},\n\n{owed:.2f} is outstanding on invoice {inv['number']} "
                       f"(due {inv['due_date']}). Please arrange payment.\n\nBrightloop Software")
            log_activity(db, f"Reminder level {level} sent for {inv['number']}", "dunning",
                         inv["subscription_id"], inv["id"], on=on)
            if level == 3:
                sub = subscription(db, inv["subscription_id"])
                if sub["status"] == "active" and sub["pausable"]:
                    set_status(db, sub["id"], "paused", reason="unpaid invoices")


# ------------------------------------------------------------- daily job


def run_day(db, on):
    """Everything that happens automatically on one day, in a fixed order."""
    from . import simulation

    activate_due(db, on)
    process_expiries(db, on)
    run_billing(db, on)
    if get_setting(db, "simulate_customers", "1") == "1":
        simulation.simulate_customers(db, on)
    run_dunning(db, on)
