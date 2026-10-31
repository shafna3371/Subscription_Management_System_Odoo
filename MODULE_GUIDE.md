# Module guide: how the code works

This guide walks through the eight modules from the problem statement, then the shared pieces (database, request flow, Time Machine, ML, dashboard). Each section names the files, explains the important code, and lists questions judges are likely to ask.

**Manual tasks** are marked like this:

> **Manual task:** something a person has to do.

---

## 0. The big picture

### How a request flows

```
Browser ──> Flask route (loopline/views/*.py)
              │  1. @login_required(...) checks role          (security.py)
              │  2. Form(...) validates input                 (core.py)
              │  3. calls business logic                      (billing.py, ml.py, metrics.py)
              │  4. SQLite enforces constraints               (db.py)
              │  5. db.commit()  or  db.rollback() on error
              └─> render_template("...html")  ──> HTML back to browser
```

**Separation of concerns.** Views (`views/*.py`) only handle HTTP: read the form, call a function, show a message. All business rules live in `billing.py`, so the web pages, the JSON API, the Time Machine, the seed script and the tests all use the same functions. A rule can never be bypassed by taking a different route.

### Three layers of validation

1. **Browser:** HTML attributes (`required`, `min`, `type="email"`) and the live password checklist in `static/app.js`. This is for convenience only.
2. **Server:** the `Form` class in `core.py`, plus rule checks in `billing.py` that raise `BillingError` with a readable message. This is the real gatekeeper.
3. **Database:** `CHECK`, `UNIQUE` and `FOREIGN KEY` constraints in `db.py`. This is the last line of defence, even against bugs in your own code.

### Security basics (`loopline/__init__.py`)

- **CSRF protection.** Every form includes a hidden `csrf` token stored in the session. `before_request` rejects any POST whose token doesn't match, which stops other websites from submitting forms as a logged-in user.
- **Password hashing.** PBKDF2-SHA256 with a random salt and 120,000 iterations (`core.hash_password`). Passwords are never stored in plain text.
- **Login sessions.** Signed cookies, `HttpOnly` and `SameSite=Lax`.
- **Response timing.** `after_request` adds an `X-Response-Time-ms` header, and the footer shows how long each page took to build.

---

## Module 1: Login and roles

**Files:** `views/auth.py`, `security.py`, `core.py` (`password_problems`, `hash_password`, `Form.email`), `templates/auth/*`

### Sign-up rules from the brief

```python
def password_problems(pw):
    problems = []
    if len(pw) <= 8:                       # "more than eight characters" means 9 or more
        problems.append("Use more than 8 characters.")
    if not re.search(r"[A-Z]", pw): ...    # an uppercase letter
    if not re.search(r"[a-z]", pw): ...    # a lowercase letter
    if not SPECIAL_RE.search(pw): ...      # a special character: anything not a letter or digit
```

`static/app.js` runs the same four tests as you type, so the checklist turns green live. The server still re-checks everything, because JavaScript can be switched off.

**One account per email** is enforced twice: `create_user()` checks first so it can show a friendly message, and the database has `email TEXT NOT NULL UNIQUE`. Emails are lower-cased first, so `Asha@X.com` and `asha@x.com` count as the same account.

### Password reset

1. `/forgot` creates a random token (`secrets.token_urlsafe(32)`) that expires in one hour, and emails a link.
2. It shows **the same message whether or not the email exists**, so attackers can't use the form to discover accounts.
3. `/reset/<token>` checks the token is unused and not expired, saves the new password, and marks the token used, so each link works only once.

> **Manual task:** the emails go to the local outbox at `/mailbox`, because the app is offline. To send real emails, add SMTP code inside `send_email()` in `core.py`.

### Roles

```python
@bp.route("/users", methods=["GET", "POST"])
@login_required("admin")          # only admins can add staff
def users(): ...
```

`login_required(*roles)` sends logged-out users to the login page and returns **403** if the role is wrong. The customer portal returns **404**, not 403, for another customer's invoice, so customers can't even confirm that someone else's record exists.

| Role | Can do |
|---|---|
| Admin | Everything, including creating products, plans, taxes and discounts, adding staff, and the Time Machine |
| Staff | Subscriptions, invoices, payments, reconciliation and reports. Can view the catalog and discounts but not change them |
| Customer | Only their own subscriptions and invoices, in the portal |

**Questions judges may ask**
- *Why PBKDF2 instead of storing a plain hash?* It's slow on purpose and salted, so stolen hashes are expensive to crack, and it's in Python's standard library.
- *How do you stop staff creating discounts?* The route checks `g.user["role"] != "admin"` on POST and returns 403. There's a test for this.

---

## Module 2: Products and variants

**Files:** `views/catalog.py` (`products`, `product`, `taxes`), `templates/catalog/products.html`, `product.html`, `_product_fields.html`

- A product has a name, type, sales price, cost price, tax and notes. The page also shows the margin (sales price minus cost).
- A **variant** is an `(attribute, value, extra_price)` row, for example `Brand / Odoo / 560`. It's `UNIQUE(product_id, attribute, value)`, so you can't add the same variant twice.
- The final price is the base price plus the variant's extra price, calculated in `billing.line_unit_price()`:

```python
if product_id == plan["product_id"]:
    base = plan["price"]            # the plan's main product is priced by the plan
else:
    base = <product sales_price>    # add-ons use their own price
extra = <variant extra_price>       # and the variant must belong to that product
return money(base + extra)
```

In forms, the variant dropdown only shows variants of the chosen product (`data-variants-for` in `app.js`). The server re-checks this anyway, because a variant from a different product raises a `BillingError`.

---

## Module 3: Recurring plans

**Files:** `views/catalog.py` (`plans`, `plan`), `templates/catalog/plans.html`, `_plan_fields.html`

A plan has a price per unit per period, a billing period (`daily | weekly | monthly | yearly`), a minimum quantity, availability dates, and four rule switches chosen per plan:

| Switch | Effect in code |
|---|---|
| `auto_close` | `process_expiries()` closes the subscription at its end date |
| `closable` | `set_status(..., "closed")` refuses early closing if off |
| `pausable` | Pausing is refused if off. Unpaid-invoice pausing also respects it |
| `renewable` | `renew()` refuses if off. Auto-renew only happens if this is on |

**Period arithmetic** (`core.add_period`) handles real calendar months. Adding one month to 31 January gives 28 or 29 February, not 3 March:

```python
def add_months(d0, months):
    ...
    day = min(d0.day, calendar.monthrange(year, month)[1])   # clamp to the month's last day
```

Minimum quantity is enforced when a subscription is confirmed and when quantities change (`check_min_qty`). Availability dates are checked when a subscription is created.

---

## Module 4: Quotation templates

**Files:** `views/catalog.py` (`templates`, `template`), `views/subs.py` (`new`), `templates/catalog/template*.html`

- A template stores a plan, product lines (product, variant, quantity), how many days the quotation stays valid, and the contract length.
- Creating a template automatically adds the plan's main product at its minimum quantity, so it's never empty.
- On **New subscription**, choosing a template copies its plan and lines, and prices each line with `line_unit_price()` **at today's prices**.
- **Send quotation** sets `quote_valid_until = today + validity_days`. If the customer tries to accept after that date, `set_status` refuses: *"This quotation has expired."*

---

## Module 5: Subscriptions

**Files:** `billing.py` (`set_status`, `TRANSITIONS`, `send_quotation`, `renew`, `process_expiries`, `log_mrr`), `views/subs.py`, `views/portal.py`, `templates/subs/*`

### The state machine

```python
TRANSITIONS = {
    "draft":     {"quotation", "confirmed", "closed"},
    "quotation": {"confirmed", "draft", "closed"},
    "confirmed": {"active", "closed"},
    "active":    {"paused", "closed"},
    "paused":    {"active", "closed"},
    "closed":    set(),               # final: nothing can reopen it
}
```

**Every** status change goes through `set_status()`, which:
1. refuses moves not in the table (for example closed to active);
2. checks business rules (lines exist, quotation not expired, minimum quantity, plan allows pause or close);
3. updates the row, writes an **activity log** entry (the timeline on each subscription page), and records the new MRR in `mrr_log`.

The database also has `CHECK (status IN (...))`, so a typo in code can't store a bad status.

**Paused** is an extra state beyond the brief's five. It covers the plan's "can be paused" rule and pausing for non-payment.

### Numbers

`SUB/00001` and `INV/2026/000001` are generated from the row id, so they're unique and readable.

### Why `mrr_log` exists

Every time a subscription's monthly value changes (activated, seats added, paused, closed), a row is added. The dashboard can then compute **MRR at any past date** and explain *why* it changed (new customers, expansion, contraction, churn) without storing snapshots.

---

## Module 6: Invoices and taxes (the billing engine)

**Files:** `billing.py` (`run_billing`, `create_invoice`, `confirm_invoice`, `cancel_invoice`, `send_invoice`), `views/finance.py`, `templates/finance/invoice*.html`, `print.html`

### "Nothing billed twice or skipped": how it's guaranteed

**1. The database refuses duplicates.**

```sql
UNIQUE (subscription_id, period_start)   -- in the invoices table
```

**2. `create_invoice` treats that refusal as "already done".**

```python
try:
    cur = db.execute("INSERT INTO invoices(...) VALUES (...)")
except sqlite3.IntegrityError:
    return None          # this period already has an invoice, so skip it
```

**3. `run_billing` catches up period by period.**

```python
while period_start <= on and (expiry is None or period_start < expiry):
    create_invoice(db, sub, period_start, on)        # creates it, or safely skips it
    period_start = add_period(period_start, sub["billing_period"])
db.execute("UPDATE subscriptions SET next_billing_date = ? ...", (period_start, ...))
```

If the server was down for five days, the loop bills each missed period once. If it crashed after creating invoices but before saving `next_billing_date`, the next run re-tries those periods and the database refuses them. The Time Machine page has buttons that demonstrate all three cases.

A **cancelled** invoice keeps its row (status `cancelled`), so its period is never billed again by accident.

### Taxes

Each product points to a tax rule the admin created. For each invoice line:

```
gross    = quantity × unit price
taxable  = gross − this line's share of discounts
tax      = taxable × rate / 100
amount   = taxable + tax
```

Totals are rounded to 2 decimals with `money()`. Tax is charged on the discounted amount, as is normal practice.

### Invoice states

`draft → confirmed → paid` (or `cancelled`). New invoices are confirmed and emailed automatically **unless the anomaly guard holds them** (see section 9). Staff can confirm, cancel, send or print from the invoice page. **Print** opens a clean page where the browser's print dialog can also save a PDF, with no PDF library needed.

---

## Module 7: Discounts

**Files:** `billing.py` (`eligible_discounts`, discount part of `create_invoice`), `views/finance.py` (`discounts`), `templates/finance/discounts.html`

Only the admin can create a discount rule. It can be:
- a **fixed amount** or a **percentage** (a database CHECK stops percentages over 100);
- conditional on a **minimum quantity** or **minimum purchase**;
- limited by **start and end dates** and a **usage limit**;
- for **one product** (`product_id` set) or the **whole subscription** (`product_id` empty);
- for **everyone who qualifies** (`audience = all`) or **only subscriptions staff assign it to** (`audience = assigned`). The second kind is used for retention offers.

### Applied automatically, stops automatically

On every new invoice, `eligible_discounts()` finds rules that are active, in date, not used up, aimed at this subscription, and whose conditions are met. Expired or used-up rules simply stop matching, so nobody has to remember to remove them.

### The usage limit can never be exceeded

```python
claimed = db.execute(
    "UPDATE discounts SET used_count = used_count + 1 "
    "WHERE id = ? AND (usage_limit IS NULL OR used_count < usage_limit)", (disc["id"],)).rowcount
if not claimed:
    continue        # someone else used the last one: don't apply it
```

The check and the increment happen in **one SQL statement**, so two invoices can't both take the last use. A subscription-level discount is spread across the lines in proportion to their value, which keeps the tax on each line correct.

---

## Module 8: Payments and reports

### Payments

**Files:** `billing.py` (`record_payment`, `resume_if_cleared`, `run_dunning`), `views/finance.py` (`invoice`, `payments`, `reconcile`), `views/api.py`

`record_payment()` refuses:
- invoices that aren't confirmed;
- amounts of zero or less;
- **overpayment** (more than what's owed);
- future dates;
- unknown payment methods.

Partial payments are allowed. When the amount paid reaches the total, the invoice becomes `paid`, and if the subscription was paused for non-payment, it resumes automatically.

**Automatic chasing (dunning)** runs every day:

| Level | When | Effect |
|---|---|---|
| 0 | 3 days before due, **only if the model predicts late payment** | Friendly heads-up |
| 1 | 1 day overdue | Reminder |
| 2 | 7 days overdue | Second reminder |
| 3 | 14 days overdue | Final notice, and the subscription pauses (if the plan allows) |

`UNIQUE(invoice_id, level)` means each reminder is sent once, even if the job runs twice.

**Bank reconciliation:** upload a CSV, and each line gets a suggested invoice and a confidence score. Approve it, pick a different invoice, or ignore it. See section 9.

### Reports (the finance lead's dashboard)

**Files:** `metrics.py`, `charts.py`, `views/insights.py` (`dashboard`, `risk`), `templates/insights/dashboard.html`

| Number | How it's calculated |
|---|---|
| Active subscriptions | `COUNT(*) WHERE status = 'active'` |
| MRR | Latest `mrr_log` value per subscription, summed. Each plan period is converted to monthly: daily ×30, weekly ×52/12, yearly ÷12 |
| ARR | MRR × 12 |
| Collected this month | Sum of payments dated this month |
| Still owed / overdue | Confirmed invoices' `total − amount_paid`, and those past their due date |
| MRR movements | Compare each subscription's MRR at the start and end of the month: 0 to more is new, more to 0 is churn, up is expansion, down is contraction |
| Net revenue retention | (start + expansion − contraction − churn) ÷ start |
| Aging | Unpaid amounts grouped by days overdue |

The latest-value-per-subscription query uses a SQL **window function**:

```sql
SELECT subscription_id, mrr FROM (
  SELECT subscription_id, mrr,
         ROW_NUMBER() OVER (PARTITION BY subscription_id ORDER BY effective_date DESC, id DESC) AS rn
  FROM mrr_log WHERE effective_date <= ?)
WHERE rn = 1
```

**Filters:** the subscription list filters by status, plan, search text and "at risk only". The invoice list filters by outstanding, overdue, draft, paid, cancelled and search text. The dashboard has a month picker for the MRR movement chart.

Charts are **SVG drawn on the server** (`charts.py`), with no JavaScript chart library and no internet needed. Their colours come from CSS, so they follow light and dark mode.

### Under 2 seconds with thousands of subscriptions

- **Indexes** on every column used to filter or join (bottom of `db.py`).
- **Pagination:** 25 rows per page on lists, 50 in the API.
- **A 30-second cache** for dashboard numbers, cleared after any change (`metrics.cached`, `after_request`).
- **Batch ML scoring:** predictions are computed once and stored in a table, so pages just read them.

Measured with 20,345 subscriptions (`seed --bulk 20000`): dashboard 167 ms on first load and 3 ms when cached; subscription list 20 ms; page 300 of a search 24 ms.

---

## 9. The AI / ML features (`ml.py`)

All four are small scikit-learn models that train in seconds and run offline. Each shows **reasons**, not just a score.

### 9a. Invoice anomaly guard

It runs on every new draft invoice before it's sent. First come **plain rules**:
- the total is more than 5× this subscription's usual (the median of its last 6 invoices);
- a quantity is more than 5× the usual and at least 5 units higher;
- the tax rate changed for the same product;
- the discount is over 50% of the subtotal;
- the total is zero.

If no rule fires, an **IsolationForest** trained on all past invoices flags the rarest 0.5% of invoice shapes. A flagged invoice stays in **Review before sending** with its reasons. The seed script plants one fat-fingered quantity so you can show this.

### 9b. Late-payment predictor

A **logistic regression** predicts the chance an invoice is paid after its due date.

- **Features:** the customer's past average days late, the share of past invoices paid late, number of past invoices, invoice amount (log), payment terms, whether the plan is yearly, and how long they've been a customer.
- **No peeking at the future:** each training row only uses invoices that were already due *before* that invoice's date.
- **Testing:** the model is trained on the oldest 75% of invoices and tested on the newest 25%, reported as **AUC**.
- **What it drives:** the smart pre-due reminder, the risk column on invoices, and the **expected cash in** forecast (each open invoice gets an expected payment date).

### 9c. Churn risk

A **logistic regression** predicts the chance a subscription closes within 90 days. Training rows are monthly "snapshots" of every subscription at past dates, labelled by whether it closed in the next 90 days. Features: late payments in the last 6 months, invoices overdue now, seats removed or added recently, tenure, days until contract end, and MRR.

**Where the reasons come from:**

```python
z = (x - scaler.mean_) / scaler.scale_     # how unusual each value is
contrib = z * model.coef_[0]               # how much it pushed the score up
```

The largest positive contributions become reason chips such as "2 late payments in the last 6 months". The **Customers at risk** page sorts by revenue at stake (risk × MRR × 12), and staff can apply a retention discount in one click.

### 9d. Bank reconciliation matcher

For each bank line and candidate invoice, four features are scored from 0 to 1: amount match, reference match (invoice number found in the reference), payer name similarity (`difflib`), and date closeness. At first it uses fixed weights. **Every staff approval or correction is saved as a training example**, and after 8 decisions with both outcomes a logistic regression replaces the fixed weights.

### Training data

> **Manual task (be honest in the pitch):** there is no real Brightloop data, so `seed.py` creates customers with hidden **personas** (reliable, late payer, churner, grower, non-payer), with some randomness, and replays 14 months day by day through the real billing code. The models learn from that history. With real data, run `flask --app loopline train` or press **Retrain** on the Models page.

The **Models** page shows each model's quality, number of training rows, features and last training time.

---

## 10. The Time Machine (`core.today`, `views/insights.time_machine`)

The whole app asks `today(db)` instead of the real date. It reads a `sim_date` setting if one exists. The admin can move the date forward by a day, a week or a month, and for each day `billing.run_day()` runs the same jobs a real server would run nightly:

```
activate subscriptions starting today → renewals and auto-close → billing
→ simulated customers act (pay, grow, cancel) → reminders and pausing
```

> **Manual task (real use only):** run `flask --app loopline clock-reset` to return to the real calendar, and schedule `flask --app loopline run-day` daily with cron or Task Scheduler.

---

## 11. JSON API (`views/api.py`)

| Method and path | Who | Returns |
|---|---|---|
| `GET /api/v1/kpis` | Staff and admin | Dashboard numbers |
| `GET /api/v1/subscriptions?status=&page=` | Everyone (customers see their own) | Paged list |
| `GET /api/v1/invoices?status=&page=` | Everyone (customers see their own) | Paged list |
| `POST /api/v1/invoices/<id>/payments` | Staff and admin | Records a payment. Body: `{"method":"upi","amount":1200,"date":"2026-11-02","note":"..."}` |

It uses the same login session. POST requests must send the CSRF token in an `X-CSRF` header. Errors come back as `{"error": "..."}` with status 401, 403 or 422.

---

## 12. Tests (`tests/test_loopline.py`)

Run `python -m unittest discover -s tests -v`. The 14 tests cover:

- password rules, including the exactly-8-characters edge case;
- billing re-runs, crash recovery, the database refusing duplicates, and outage catch-up;
- tax calculation and discount usage limits;
- payment rules: overpayment and future dates;
- invalid status changes;
- sign-up validation and duplicate emails;
- every staff page loading in under 2 seconds;
- role limits for staff and customers;
- customers only seeing their own data;
- the full password-reset flow, including one-time links.

Each test class builds its own temporary database, so your demo data is never touched.
