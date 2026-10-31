import csv
import io
import random
import secrets
from datetime import timedelta

from flask import (Blueprint, Response, abort, flash, g, redirect,
                   render_template, request, url_for)

from .. import billing, ml
from ..billing import PAYMENT_METHODS, BillingError
from ..core import Form, d, money, today
from ..db import get_db
from ..security import STAFF, form_errors, login_required
from .subs import PAGE, page_args

bp = Blueprint("finance", __name__)


# ---------------------------------------------------------------- invoices

@bp.route("/invoices")
@login_required(*STAFF)
def invoices():
    db = get_db()
    now = today(db).isoformat()
    where, params = [], []
    status = request.args.get("status", "")
    q = (request.args.get("q") or "").strip()
    if status == "overdue":
        where.append("i.status = 'confirmed' AND i.due_date < ?")
        params.append(now)
    elif status == "outstanding":
        where.append("i.status = 'confirmed'")
    elif status in ("draft", "confirmed", "paid", "cancelled"):
        where.append("i.status = ?")
        params.append(status)
    if q:
        where.append("(i.number LIKE ? OR u.name LIKE ? OR u.company LIKE ?)")
        params += [f"%{q}%"] * 3
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    base = f"FROM invoices i JOIN users u ON u.id = i.customer_id {sql_where}"
    total, owed = db.execute(f"SELECT COUNT(*), COALESCE(SUM(i.total - i.amount_paid), 0) {base}", params).fetchone()
    page = page_args()
    rows = db.execute(
        f"SELECT i.*, u.name AS customer_name, u.company, "
        f"(SELECT score FROM predictions WHERE entity_type = 'invoice' AND entity_id = i.id "
        f" AND kind = 'late_payment') AS late_risk {base} ORDER BY i.invoice_date DESC, i.id DESC LIMIT ? OFFSET ?",
        (*params, PAGE, (page - 1) * PAGE)).fetchall()
    return render_template("finance/invoices.html", rows=rows, total=total, owed=owed, page=page,
                           pages=max(1, -(-total // PAGE)), args=request.args, now=now)


@bp.route("/invoices/<int:inv_id>", methods=["GET", "POST"])
@login_required(*STAFF)
def invoice(inv_id):
    db = get_db()
    inv = billing.invoice(db, inv_id) or abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "confirm":
                billing.confirm_invoice(db, inv_id, g.user["name"])
                flash("Invoice confirmed.", "ok")
            elif action == "cancel":
                billing.cancel_invoice(db, inv_id, g.user["name"])
                flash("Invoice cancelled. This period will not be billed again.", "ok")
            elif action == "send":
                billing.send_invoice(db, inv_id, g.user["name"])
                flash("Invoice sent to the customer.", "ok")
            elif action == "pay":
                f = Form(request.form)
                method = f.choice("method", PAYMENT_METHODS, label="Payment method")
                amount = f.number("amount", minimum=0.01, label="Amount")
                pay_date = f.date("date", label="Payment date")
                note = f.text("note", max_len=300)
                if not f.ok:
                    raise BillingError(next(iter(f.errors.values())))
                billing.record_payment(db, inv_id, method, amount, pay_date, note, g.user["id"], g.user["name"])
                flash("Payment recorded.", "ok")
            else:
                raise BillingError("Unknown action.")
            db.commit()
        except BillingError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("finance.invoice", inv_id=inv_id))
    return render_template(
        "finance/invoice.html", inv=inv, methods=PAYMENT_METHODS, today=today(db),
        lines=db.execute("SELECT * FROM invoice_lines WHERE invoice_id = ?", (inv_id,)).fetchall(),
        payments=db.execute("SELECT * FROM payments WHERE invoice_id = ? ORDER BY date", (inv_id,)).fetchall(),
        reminders=db.execute("SELECT * FROM reminders WHERE invoice_id = ? ORDER BY level", (inv_id,)).fetchall(),
        discounts=db.execute("SELECT du.amount, d.name FROM discount_usages du JOIN discounts d ON d.id = du.discount_id "
                             "WHERE du.invoice_id = ?", (inv_id,)).fetchall(),
        late=db.execute("SELECT * FROM predictions WHERE entity_type = 'invoice' AND entity_id = ? "
                        "AND kind = 'late_payment'", (inv_id,)).fetchone())


def render_print(db, inv_id):
    inv = billing.invoice(db, inv_id) or abort(404)
    return render_template("finance/print.html", inv=inv,
                           lines=db.execute("SELECT * FROM invoice_lines WHERE invoice_id = ?", (inv_id,)).fetchall())


@bp.route("/invoices/<int:inv_id>/print")
@login_required(*STAFF)
def print_invoice(inv_id):
    return render_print(get_db(), inv_id)


@bp.route("/review", methods=["GET", "POST"])
@login_required(*STAFF)
def review():
    db = get_db()
    rows = db.execute("SELECT i.*, u.name AS customer_name FROM invoices i JOIN users u ON u.id = i.customer_id "
                      "WHERE i.status = 'draft' AND i.anomaly_flag = 1 ORDER BY i.invoice_date").fetchall()
    return render_template("finance/review.html", rows=rows)


@bp.route("/payments")
@login_required(*STAFF)
def payments():
    db = get_db()
    page = page_args()
    total = db.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    rows = db.execute("SELECT p.*, i.number, u.name AS customer_name FROM payments p JOIN invoices i "
                      "ON i.id = p.invoice_id JOIN users u ON u.id = i.customer_id ORDER BY p.date DESC, p.id DESC "
                      "LIMIT ? OFFSET ?", (PAGE, (page - 1) * PAGE)).fetchall()
    return render_template("finance/payments.html", rows=rows, page=page, pages=max(1, -(-total // PAGE)),
                           methods=PAYMENT_METHODS, args=request.args)


# --------------------------------------------------------------- discounts

@bp.route("/discounts", methods=["GET", "POST"])
@login_required(*STAFF)
def discounts():
    db = get_db()
    if request.method == "POST":
        if g.user["role"] != "admin":
            abort(403)
        if request.form.get("action") == "toggle":
            db.execute("UPDATE discounts SET active = 1 - active WHERE id = ?", (request.form.get("id"),))
            db.commit()
            return redirect(url_for("finance.discounts"))
        f = Form(request.form)
        v = {
            "name": f.text("name", required=True, max_len=80),
            "kind": f.choice("kind", ("fixed", "percent"), label="Type"),
            "value": f.number("value", minimum=0.01, label="Value"),
            "min_purchase": f.number("min_purchase", required=False, minimum=0, label="Minimum purchase") or 0,
            "min_qty": f.number("min_qty", required=False, minimum=0, integer=True, label="Minimum quantity") or 0,
            "start_date": f.date("start_date", label="Start date"),
            "end_date": f.date("end_date", required=False, label="End date"),
            "usage_limit": f.number("usage_limit", required=False, minimum=1, integer=True, label="Usage limit"),
            "product_id": f.ref("product_id", db, "products", required=False, label="Product"),
            "audience": f.choice("audience", ("all", "assigned"), label="Who gets it"),
        }
        if v["kind"] == "percent" and v["value"]:
            f.check(v["value"] <= 100, "value", "A percentage discount can be at most 100.")
        if v["start_date"] and v["end_date"]:
            f.check(v["end_date"] >= v["start_date"], "end_date", "End date must be on or after the start date.")
        if v["name"] and db.execute("SELECT 1 FROM discounts WHERE name = ?", (v["name"],)).fetchone():
            f.errors["name"] = "A discount with this name already exists."
        if f.ok:
            v["start_date"] = v["start_date"].isoformat()
            v["end_date"] = v["end_date"].isoformat() if v["end_date"] else None
            db.execute("INSERT INTO discounts(name, kind, value, min_purchase, min_qty, start_date, end_date, "
                       "usage_limit, product_id, audience) VALUES (:name,:kind,:value,:min_purchase,:min_qty,"
                       ":start_date,:end_date,:usage_limit,:product_id,:audience)", v)
            db.commit()
            flash("Discount rule created. It applies on the next matching invoices automatically.", "ok")
            return redirect(url_for("finance.discounts"))
        form_errors(f.errors)
    rows = db.execute("SELECT d.*, p.name AS product_name, (SELECT COALESCE(SUM(amount), 0) FROM discount_usages u "
                      "WHERE u.discount_id = d.id) AS given FROM discounts d LEFT JOIN products p ON p.id = d.product_id "
                      "ORDER BY d.active DESC, d.id DESC").fetchall()
    return render_template("finance/discounts.html", rows=rows, form=request.form, today=today(db),
                           products=db.execute("SELECT id, name FROM products ORDER BY name").fetchall())


# ---------------------------------------------------------- reconciliation

@bp.route("/reconcile", methods=["GET", "POST"])
@login_required(*STAFF)
def reconcile():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "upload":
                count = import_bank_csv(db, request.files.get("file"))
                flash(f"Imported {count} bank lines and suggested a match for each.", "ok")
            elif action in ("approve", "choose"):
                line = db.execute("SELECT * FROM bank_lines WHERE id = ? AND status = 'pending'",
                                  (request.form.get("line_id"),)).fetchone()
                if not line:
                    raise BillingError("That bank line was already handled.")
                chosen = request.form.get("invoice_id") if action == "choose" else line["suggested_invoice"]
                chosen = int(chosen) if chosen and str(chosen).isdigit() else None
                if not chosen:
                    raise BillingError("Choose an invoice for this bank line.")
                apply_bank_line(db, line, chosen)
                flash("Payment recorded and the matcher learned from your decision.", "ok")
            elif action == "approve_confident":
                lines = db.execute("SELECT * FROM bank_lines WHERE status = 'pending' AND confidence >= 0.85 "
                                   "AND suggested_invoice IS NOT NULL").fetchall()
                done = 0
                for line in lines:
                    try:
                        apply_bank_line(db, line, line["suggested_invoice"], learn=False)
                        done += 1
                    except BillingError:
                        continue
                flash(f"Approved {done} high-confidence matches.", "ok")
            elif action == "ignore":
                db.execute("UPDATE bank_lines SET status = 'ignored' WHERE id = ?", (request.form.get("line_id"),))
            db.commit()
        except BillingError as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("finance.reconcile"))
    lines = db.execute(
        "SELECT b.*, i.number, i.total - i.amount_paid AS outstanding, u.name AS customer_name "
        "FROM bank_lines b LEFT JOIN invoices i ON i.id = b.suggested_invoice LEFT JOIN users u ON u.id = i.customer_id "
        "WHERE b.status = 'pending' ORDER BY b.confidence DESC LIMIT 200").fetchall()
    open_invoices = db.execute("SELECT i.id, i.number, i.total - i.amount_paid AS owed, u.name FROM invoices i "
                               "JOIN users u ON u.id = i.customer_id WHERE i.status = 'confirmed' "
                               "ORDER BY u.name LIMIT 500").fetchall()
    card = db.execute("SELECT * FROM model_cards WHERE name = 'Bank reconciliation matcher'").fetchone()
    return render_template("finance/reconcile.html", lines=lines, open_invoices=open_invoices, card=card)


def import_bank_csv(db, file):
    if file is None or not file.filename:
        raise BillingError("Choose a CSV file to upload.")
    if not file.filename.lower().endswith(".csv"):
        raise BillingError("Upload a .csv file with columns date, amount, reference, payer.")
    try:
        text = file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise BillingError("The file isn't readable text. Save it as UTF-8 CSV and try again.")
    reader = csv.DictReader(io.StringIO(text))
    needed = {"date", "amount", "reference", "payer"}
    if not reader.fieldnames or not needed.issubset({h.strip().lower() for h in reader.fieldnames}):
        raise BillingError("The CSV needs these columns: date, amount, reference, payer.")
    batch = secrets.token_hex(4)
    count = 0
    for n, raw in enumerate(reader, start=2):
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        try:
            amount = float(row["amount"].replace(",", ""))
            on = d(row["date"])
        except (ValueError, TypeError):
            raise BillingError(f"Row {n}: amount must be a number and date must be YYYY-MM-DD.")
        if amount <= 0:
            continue  # outgoing money is not a customer payment
        line = {"date": on.isoformat(), "amount": amount, "reference": row["reference"][:120],
                "payer": row["payer"][:120]}
        suggested, confidence = ml.suggest_match(db, line)
        db.execute("INSERT INTO bank_lines(batch, date, amount, reference, payer, suggested_invoice, confidence) "
                   "VALUES (?,?,?,?,?,?,?)", (batch, line["date"], amount, line["reference"], line["payer"],
                                              suggested, confidence))
        count += 1
    if count == 0:
        raise BillingError("No incoming payments found in that file.")
    return count


def apply_bank_line(db, line, invoice_id, learn=True):
    inv = billing.invoice(db, invoice_id)
    if inv is None or inv["status"] != "confirmed":
        raise BillingError("That invoice is not open any more.")
    if learn:  # before paying, while the invoice is still an open candidate
        ml.record_feedback(db, dict(line), invoice_id, line["suggested_invoice"])
    amount = min(line["amount"], money(inv["total"] - inv["amount_paid"]))
    billing.record_payment(db, invoice_id, "bank_transfer", amount, d(line["date"]),
                           f"Bank: {line['reference'] or line['payer']}", g.user["id"], g.user["name"])
    db.execute("UPDATE bank_lines SET status = 'matched', suggested_invoice = ? WHERE id = ?", (invoice_id, line["id"]))


@bp.route("/reconcile/sample.csv")
@login_required(*STAFF)
def sample_csv():
    """A realistic bank statement built from today's open invoices, with messy details."""
    db = get_db()
    rng = random.Random(today(db).isoformat())
    rows = db.execute("SELECT i.number, i.total - i.amount_paid AS owed, i.due_date, u.name, u.company "
                      "FROM invoices i JOIN users u ON u.id = i.customer_id WHERE i.status = 'confirmed' "
                      "ORDER BY RANDOM() LIMIT 25").fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["date", "amount", "reference", "payer"])
    now = today(db)
    for r in rows:
        style = rng.random()
        reference = r["number"] if style < 0.5 else (f"PAYMENT {r['number'].split('/')[-1]}" if style < 0.75 else "")
        payer = (r["company"] or r["name"]).upper() if style < 0.8 else r["name"].split()[0]
        w.writerow([(now - timedelta(days=rng.randint(0, 3))).isoformat(), f"{r['owed']:.2f}", reference, payer])
    w.writerow([now.isoformat(), "-1200.00", "OFFICE RENT", "LANDLORD LTD"])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=sample_bank_statement.csv"})
