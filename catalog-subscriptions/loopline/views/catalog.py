from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..core import PERIODS, Form, today
from ..db import get_db
from ..security import STAFF, form_errors, login_required

bp = Blueprint("catalog", __name__)

PRODUCT_TYPES = ("software", "addon", "service", "goods")


# ------------------------------------------------------------------- taxes

@bp.route("/taxes", methods=["GET", "POST"])
@login_required("admin")
def taxes():
    db = get_db()
    if request.method == "POST":
        f = Form(request.form)
        name = f.text("name", required=True, max_len=60)
        rate = f.number("rate", minimum=0, maximum=100, label="Rate")
        if name and db.execute("SELECT 1 FROM taxes WHERE name = ?", (name,)).fetchone():
            f.errors["name"] = "A tax with this name already exists."
        if f.ok:
            db.execute("INSERT INTO taxes(name, rate) VALUES (?,?)", (name, rate))
            db.commit()
            flash("Tax rule added.", "ok")
            return redirect(url_for("catalog.taxes"))
        form_errors(f.errors)
    rows = db.execute("SELECT t.*, (SELECT COUNT(*) FROM products p WHERE p.tax_id = t.id) AS used "
                      "FROM taxes t ORDER BY name").fetchall()
    return render_template("catalog/taxes.html", taxes=rows)


# ---------------------------------------------------------------- products

def _product_form(db, product_id=None):
    f = Form(request.form)
    values = {
        "name": f.text("name", required=True, max_len=80),
        "type": f.choice("type", PRODUCT_TYPES, label="Type"),
        "sales_price": f.number("sales_price", minimum=0, label="Sales price"),
        "cost_price": f.number("cost_price", minimum=0, label="Cost price"),
        "tax_id": f.ref("tax_id", db, "taxes", required=False, label="Tax"),
        "notes": f.text("notes", max_len=1000),
    }
    clash = db.execute("SELECT id FROM products WHERE name = ?", (values["name"],)).fetchone()
    if clash and clash["id"] != product_id:
        f.errors["name"] = "A product with this name already exists."
    return f, values


@bp.route("/products", methods=["GET", "POST"])
@login_required(*STAFF)
def products():
    db = get_db()
    if request.method == "POST":
        if g_role() != "admin":
            abort(403)
        f, v = _product_form(db)
        if f.ok:
            cur = db.execute("INSERT INTO products(name, type, sales_price, cost_price, tax_id, notes) "
                             "VALUES (:name,:type,:sales_price,:cost_price,:tax_id,:notes)", v)
            db.commit()
            flash("Product added. Add variants below if it comes in options.", "ok")
            return redirect(url_for("catalog.product", product_id=cur.lastrowid))
        form_errors(f.errors)
    rows = db.execute(
        "SELECT p.*, t.name AS tax_name, (SELECT COUNT(*) FROM product_variants v WHERE v.product_id = p.id) "
        "AS variants FROM products p LEFT JOIN taxes t ON t.id = p.tax_id ORDER BY p.name").fetchall()
    return render_template("catalog/products.html", products=rows, types=PRODUCT_TYPES,
                           taxes=db.execute("SELECT * FROM taxes ORDER BY name").fetchall(), form=request.form)


def g_role():
    from flask import g
    return g.user["role"]


@bp.route("/products/<int:product_id>", methods=["GET", "POST"])
@login_required(*STAFF)
def product(product_id):
    db = get_db()
    item = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone() or abort(404)
    if request.method == "POST":
        if g_role() != "admin":
            abort(403)
        if request.form.get("action") == "variant":
            f = Form(request.form)
            attribute = f.text("attribute", required=True, max_len=40)
            value = f.text("value", required=True, max_len=60)
            extra = f.number("extra_price", minimum=0, label="Extra price")
            if f.ok and db.execute("SELECT 1 FROM product_variants WHERE product_id = ? AND attribute = ? "
                                   "AND value = ?", (product_id, attribute, value)).fetchone():
                f.errors["value"] = "This variant already exists."
            if f.ok:
                db.execute("INSERT INTO product_variants(product_id, attribute, value, extra_price) VALUES (?,?,?,?)",
                           (product_id, attribute, value, extra))
                db.commit()
                flash("Variant added.", "ok")
                return redirect(url_for("catalog.product", product_id=product_id))
            form_errors(f.errors)
        else:
            f, v = _product_form(db, product_id)
            if f.ok:
                db.execute("UPDATE products SET name=:name, type=:type, sales_price=:sales_price, "
                           "cost_price=:cost_price, tax_id=:tax_id, notes=:notes WHERE id=:id", {**v, "id": product_id})
                db.commit()
                flash("Product saved.", "ok")
                return redirect(url_for("catalog.product", product_id=product_id))
            form_errors(f.errors)
    variants = db.execute("SELECT * FROM product_variants WHERE product_id = ? ORDER BY attribute, value",
                          (product_id,)).fetchall()
    return render_template("catalog/product.html", p=item, variants=variants, types=PRODUCT_TYPES,
                           taxes=db.execute("SELECT * FROM taxes ORDER BY name").fetchall())


# ------------------------------------------------------------------- plans

def _plan_form(db, plan_id=None):
    f = Form(request.form)
    v = {
        "name": f.text("name", required=True, max_len=80),
        "product_id": f.ref("product_id", db, "products", label="Product"),
        "price": f.number("price", minimum=0, label="Price"),
        "billing_period": f.choice("billing_period", PERIODS, label="Billing period"),
        "min_qty": f.number("min_qty", minimum=1, maximum=100000, integer=True, label="Minimum quantity"),
        "start_date": f.date("start_date", label="Available from"),
        "end_date": f.date("end_date", required=False, label="Available until"),
        "auto_close": f.flag("auto_close"), "closable": f.flag("closable"),
        "pausable": f.flag("pausable"), "renewable": f.flag("renewable"),
    }
    if v["start_date"] and v["end_date"]:
        f.check(v["end_date"] > v["start_date"], "end_date", "Available until must be after available from.")
    clash = db.execute("SELECT id FROM plans WHERE name = ?", (v["name"],)).fetchone()
    if clash and clash["id"] != plan_id:
        f.errors["name"] = "A plan with this name already exists."
    for k in ("start_date", "end_date"):
        v[k] = v[k].isoformat() if v[k] else None
    return f, v


@bp.route("/plans", methods=["GET", "POST"])
@login_required(*STAFF)
def plans():
    db = get_db()
    if request.method == "POST":
        if g_role() != "admin":
            abort(403)
        f, v = _plan_form(db)
        if f.ok:
            db.execute("INSERT INTO plans(name, product_id, price, billing_period, min_qty, start_date, end_date, "
                       "auto_close, closable, pausable, renewable) VALUES (:name,:product_id,:price,:billing_period,"
                       ":min_qty,:start_date,:end_date,:auto_close,:closable,:pausable,:renewable)", v)
            db.commit()
            flash("Plan created.", "ok")
            return redirect(url_for("catalog.plans"))
        form_errors(f.errors)
    rows = db.execute(
        "SELECT pl.*, p.name AS product_name, (SELECT COUNT(*) FROM subscriptions s WHERE s.plan_id = pl.id "
        "AND s.status = 'active') AS active FROM plans pl JOIN products p ON p.id = pl.product_id ORDER BY pl.name"
    ).fetchall()
    return render_template("catalog/plans.html", plans=rows, periods=PERIODS, form=request.form,
                           products=db.execute("SELECT id, name FROM products ORDER BY name").fetchall(),
                           today=today(db))


@bp.route("/plans/<int:plan_id>", methods=["GET", "POST"])
@login_required("admin")
def plan(plan_id):
    db = get_db()
    item = db.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone() or abort(404)
    if request.method == "POST":
        f, v = _plan_form(db, plan_id)
        if f.ok:
            db.execute("UPDATE plans SET name=:name, product_id=:product_id, price=:price, "
                       "billing_period=:billing_period, min_qty=:min_qty, start_date=:start_date, end_date=:end_date, "
                       "auto_close=:auto_close, closable=:closable, pausable=:pausable, renewable=:renewable "
                       "WHERE id=:id", {**v, "id": plan_id})
            db.commit()
            flash("Plan saved. Existing subscriptions keep their current prices.", "ok")
            return redirect(url_for("catalog.plans"))
        form_errors(f.errors)
    return render_template("catalog/plan.html", plan=item, periods=PERIODS,
                           products=db.execute("SELECT id, name FROM products ORDER BY name").fetchall())


# ----------------------------------------------------- quotation templates

@bp.route("/templates", methods=["GET", "POST"])
@login_required(*STAFF)
def templates():
    db = get_db()
    if request.method == "POST":
        if g_role() != "admin":
            abort(403)
        f = Form(request.form)
        name = f.text("name", required=True, max_len=80)
        plan_id = f.ref("plan_id", db, "plans", label="Plan")
        validity = f.number("validity_days", minimum=1, maximum=365, integer=True, label="Valid for (days)")
        term = f.number("term_periods", minimum=1, maximum=120, integer=True, label="Contract length")
        if name and db.execute("SELECT 1 FROM quotation_templates WHERE name = ?", (name,)).fetchone():
            f.errors["name"] = "A template with this name already exists."
        if f.ok:
            cur = db.execute("INSERT INTO quotation_templates(name, plan_id, validity_days, term_periods) "
                             "VALUES (?,?,?,?)", (name, plan_id, validity, term))
            plan_row = db.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
            db.execute("INSERT INTO quotation_template_lines(template_id, product_id, quantity) VALUES (?,?,?)",
                       (cur.lastrowid, plan_row["product_id"], plan_row["min_qty"]))
            db.commit()
            flash("Template created with the plan's product. Add more lines below.", "ok")
            return redirect(url_for("catalog.template", template_id=cur.lastrowid))
        form_errors(f.errors)
    rows = db.execute("SELECT t.*, p.name AS plan_name, p.billing_period, (SELECT COUNT(*) FROM "
                      "quotation_template_lines l WHERE l.template_id = t.id) AS lines FROM quotation_templates t "
                      "JOIN plans p ON p.id = t.plan_id ORDER BY t.name").fetchall()
    return render_template("catalog/templates.html", templates=rows, form=request.form,
                           plans=db.execute("SELECT id, name FROM plans ORDER BY name").fetchall())


@bp.route("/templates/<int:template_id>", methods=["GET", "POST"])
@login_required(*STAFF)
def template(template_id):
    db = get_db()
    t = db.execute("SELECT t.*, p.name AS plan_name FROM quotation_templates t JOIN plans p ON p.id = t.plan_id "
                   "WHERE t.id = ?", (template_id,)).fetchone() or abort(404)
    if request.method == "POST":
        if g_role() != "admin":
            abort(403)
        if request.form.get("action") == "remove":
            db.execute("DELETE FROM quotation_template_lines WHERE id = ? AND template_id = ?",
                       (request.form.get("line_id"), template_id))
            db.commit()
            return redirect(url_for("catalog.template", template_id=template_id))
        f = Form(request.form)
        product_id = f.ref("product_id", db, "products", label="Product")
        variant_id = f.ref("variant_id", db, "product_variants", required=False, label="Variant")
        qty = f.number("quantity", minimum=1, maximum=100000, integer=True, label="Quantity")
        if variant_id and product_id and not db.execute(
                "SELECT 1 FROM product_variants WHERE id = ? AND product_id = ?", (variant_id, product_id)).fetchone():
            f.errors["variant_id"] = "That variant belongs to a different product."
        if f.ok:
            db.execute("INSERT INTO quotation_template_lines(template_id, product_id, variant_id, quantity) "
                       "VALUES (?,?,?,?)", (template_id, product_id, variant_id, qty))
            db.commit()
            flash("Line added.", "ok")
            return redirect(url_for("catalog.template", template_id=template_id))
        form_errors(f.errors)
    lines = db.execute("SELECT l.*, p.name AS product_name, v.attribute, v.value AS variant_value "
                       "FROM quotation_template_lines l JOIN products p ON p.id = l.product_id "
                       "LEFT JOIN product_variants v ON v.id = l.variant_id WHERE l.template_id = ?",
                       (template_id,)).fetchall()
    return render_template("catalog/template.html", t=t, lines=lines,
                           products=db.execute("SELECT id, name FROM products ORDER BY name").fetchall(),
                           variants=db.execute("SELECT v.*, p.name AS product_name FROM product_variants v "
                                               "JOIN products p ON p.id = v.product_id ORDER BY p.name").fetchall())
