# Loopline: subscription management for Brightloop Software

Loopline handles the whole life of a subscription business in one place: products and plans, quotations, subscriptions, automatic invoicing with taxes and discounts, payments, overdue reminders and a finance dashboard. It also includes small, explainable machine-learning features.

**Stack:** Python 3.10+, Flask, SQLite and scikit-learn. There are no web fonts, CDNs or cloud services, so after installation it runs fully offline.

For a walkthrough of how the code works, module by module, read **[MODULE_GUIDE.md](MODULE_GUIDE.md)**.

---

## 1. Setup

> **Manual task:** install Python 3.10 or newer from python.org. On Windows, tick "Add Python to PATH" during installation.

```bash
# 1. Create a virtual environment (recommended)
python -m venv .venv
# Windows:          .venv\Scripts\activate
# macOS / Linux:    source .venv/bin/activate

# 2. Install the four dependencies (needs internet ONCE)
pip install -r requirements.txt

# 3. Build the demo database: 300 customers, 14 months of simulated history, trained models (~5 seconds)
flask --app loopline seed

# 4. Start the app
python run.py
```

Then open **http://127.0.0.1:5000**.

> **Manual task (before an offline venue):** step 2 downloads packages. If the venue might have no internet, install everything at home beforehand. You can also run `pip download -r requirements.txt -d wheels/`, then install at the venue with `pip install --no-index --find-links wheels/ -r requirements.txt`.

### Demo accounts (created by `seed`)

| Role | Email | Password |
|---|---|---|
| Admin | admin@brightloop.test | Admin@12345 |
| Staff | staff@brightloop.test | Staff@12345 |
| Customer | customer@brightloop.test | Customer@123 |

### Starting with an empty database instead

```bash
flask --app loopline reset          # empty database
flask --app loopline create-admin   # asks for name, email and password (the brief's password rules apply)
```

The admin then creates taxes, products, plans and templates in the UI. Customers sign up themselves at `/signup`.

---

## 2. Useful commands

| Command | What it does |
|---|---|
| `flask --app loopline seed` | Wipe and rebuild the demo data. Options: `--customers 300 --months 14 --bulk 0` |
| `flask --app loopline seed --bulk 20000` | Also add 20,000 subscriptions to prove pages stay under 2 seconds |
| `flask --app loopline train` | Retrain all models and re-score open invoices and subscriptions |
| `flask --app loopline run-day` | Run today's automatic jobs (billing, renewals, reminders) |
| `flask --app loopline clock-reset` | Stop using the Time Machine date and follow the real calendar |
| `flask --app loopline create-admin` | Create an admin account |
| `flask --app loopline reset` | Delete all data |
| `python -m unittest discover -s tests -v` | Run the 14 automated tests |

---

## 3. A 7-minute demo script

The script follows the story in the problem statement: "a month at Brightloop".

1. **A new customer signs up.** Open `/signup` in a private window. Type a weak password and watch the live rule checklist. Try an email that's already used and see the error. Then create the account.
2. **Roles.** Log in as staff and show that Discounts can't be created, and that the Time Machine and Team pages are refused. Log in as the customer and show they only see the portal.
3. **Launching a plan (admin).** Open Products, then Brightloop CRM. Show the variant *Brand: Odoo +560*. Open Plans and show the period, minimum quantity, availability dates and per-plan rules.
4. **Quote to subscription (staff).** Go to New subscription, choose the "Enterprise CRM, yearly" template, then Create draft. Lines are pre-filled. Send the quotation, then accept it from the customer portal. The subscription moves through Quotation, Confirmed and Active, and every step appears in its activity timeline.
5. **Billing day (admin).** In the Time Machine, click **+1 week**. Invoices are created with taxes and discounts already applied. One invoice is **held for review** because its quantity is 20× the usual, and you can show the reasons.
6. **Prove billing is safe.** In the Time Machine, click *Run billing 3 times* (no new invoices), *Simulate a crash* (no duplicates) and *Simulate downtime* (missed periods caught up, each once).
7. **A better price.** Show the "Volume: 10+ CRM seats" discount applying automatically to an invoice line. Open Customers at risk, pick a high-risk subscription, and apply a retention offer. Show the reasons behind its risk score.
8. **Money comes in.** In Bank reconciliation, download the sample statement and upload it. Approve the high-confidence matches, then correct one by hand and note that the matcher learns from it. Record a manual payment on an invoice.
9. **The finance lead.** Open the Dashboard and walk through MRR and ARR, the MRR movement waterfall, unpaid invoices by age, the expected-cash forecast and revenue at risk. Point to the page time in the footer.

---

## 4. How the Odoo hackathon rules are met

| Rule | Where |
|---|---|
| Real-time, dynamic data (no static JSON) | Every page reads live from SQLite (`loopline/db.py`). The seed script generates data through the same business logic. |
| Responsive, clean UI | One stylesheet with colour tokens and light/dark mode; layout collapses to a single column with a Menu button below 760 px (`static/style.css`). |
| Robust input validation | Browser checks, server checks (the `Form` class in `core.py`), and database CHECK, UNIQUE and FOREIGN KEY constraints. CSRF protection on every form. |
| Intuitive navigation | Sidebar grouped by job (Overview, Sales, Billing, Catalog, System), filtered by role, with the current page highlighted. |
| Proper Git use by everyone | **Manual task**, see section 6. |
| Backend APIs, data model, local DB | JSON API under `/api/v1/...` (`views/api.py`), 20-table schema (`db.py`), local SQLite file. |
| Understand AI code before using it | **Manual task**: every team member reads MODULE_GUIDE.md and the module they present. |
| Offline / local first | No CDN, no web fonts, no cloud APIs. Emails go to a local outbox at `/mailbox`. |
| Trendy tech only if it adds value | ML is limited to four small models, each tied to a pain point in the brief, with visible reasons. |

---

## 5. Manual tasks, in one place

These need a person. The code can't do them for you.

1. **Install Python and dependencies** (section 1). Do it before the event if the internet is uncertain.
2. **Commit to Git as a team** (section 6). Judges check that every member contributed.
3. **Read and understand the code.** Each member should be able to explain their module line by line (MODULE_GUIDE.md).
4. **Prepare the demo.** Run `seed` shortly before presenting so the dates are fresh, and rehearse the script above.
5. **Before any real use** (not needed for the hackathon):
   - Set a secret key with the environment variable `LOOPLINE_SECRET`. A random one is generated in `instance/secret_key` by default.
   - Change or delete the demo accounts.
   - Turn off the Time Machine date with `flask --app loopline clock-reset`.
   - Schedule `flask --app loopline run-day` once a day using cron (macOS/Linux) or Task Scheduler (Windows). Without it, billing only runs when someone presses a button.
   - **Real email** isn't included. Emails go to the local `/mailbox`. To send them, connect an SMTP server inside `send_email()` in `loopline/core.py`.
   - **Card or UPI payment gateways** aren't included. Payments are recorded by staff, through the API, or from bank statement uploads.
   - **Real bank exports** use different column names. Rename the columns to `date, amount, reference, payer` before uploading, or adapt `import_bank_csv()` in `views/finance.py`.
   - Run `flask --app loopline train` once real history exists, so the models learn from real customers instead of simulated ones.
   - Run behind a production server (for example `waitress-serve --call loopline:create_app`) instead of `python run.py`.

---

## 6. Git plan for a team of four (manual task)

The rule says "one member managing the repo is not enough." One way to split the commits so everyone commits their own work:

| Member | Owns | Files |
|---|---|---|
| A | Project setup, login and roles, validation, layout, navigation, charts | `.gitignore`, `requirements.txt`, `run.py`, `README.md`, `loopline/__init__.py`, `core.py`, `security.py`, `charts.py`, `views/auth.py`, `templates/base.html`, `_macros.html`, `error.html`, `templates/auth/`, `static/` |
| B | Products, variants, plans, quotation templates, subscriptions, customer portal, JSON API | `views/catalog.py`, `views/subs.py`, `views/portal.py`, `views/api.py`, `templates/catalog/`, `templates/subs/`, `templates/portal/` |
| C | Database schema, billing engine, invoices, taxes, discounts, payments, reconciliation, CLI | `db.py`, `billing.py`, `cli.py`, `views/finance.py`, `templates/finance/` |
| D | Dashboard, metrics, ML, simulation, seed data, Time Machine, tests | `metrics.py`, `ml.py`, `simulation.py`, `seed.py`, `views/insights.py`, `templates/insights/`, `tests/`, `MODULE_GUIDE.md` |

The app runs once all four parts are merged. Merge order: A, then C, then B, then D.

```bash
git init && git add .gitignore README.md requirements.txt && git commit -m "chore: project setup"
git checkout -b feat/auth      # member A adds and commits their files, opens a pull request
git checkout -b feat/catalog   # member B ...
```

Use small commits with clear messages (`feat:`, `fix:`, `test:`), and merge through pull requests that another member reviews.

---

## 7. Project structure

```
loopline/
  __init__.py      app factory: settings, CSRF, timing header, error pages, blueprints
  db.py            schema (20 tables, constraints, indexes) and connection helpers
  core.py          simulated clock, period maths, validation, password hashing, outbox, activity log
  security.py      login_required(roles) decorator
  billing.py       lifecycle, invoice generation, discounts, taxes, payments, dunning, daily job
  metrics.py       MRR, MRR movements, KPIs, aging, cash forecast
  ml.py            anomaly guard, late-payment and churn models, reconciliation matcher
  charts.py        server-side SVG charts
  simulation.py    persona-based simulated customers
  seed.py          demo data builder
  cli.py           flask commands
  views/           auth, catalog, subs, finance, insights, portal, api  (one blueprint each)
templates/         Jinja pages, grouped like the views
static/            style.css, app.js
tests/             14 automated tests
run.py             starts the web server
```
