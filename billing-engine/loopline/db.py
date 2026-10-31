"""SQLite connection helpers and the full schema.

Every table lives in one local SQLite file (no server, works offline).
Business rules that must never break are enforced by the database itself:
  * UNIQUE(subscription_id, period_start) on invoices  -> no double billing
  * CHECK constraints on every status column            -> no invalid states
  * UNIQUE(email)                                       -> one account per email
"""
import os
import sqlite3

from flask import current_app, g

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin','staff','customer')),
    company       TEXT,
    persona       TEXT,              -- hidden behaviour profile, only for simulated customers
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS password_resets (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS taxes (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    rate REAL NOT NULL CHECK (rate >= 0 AND rate <= 100)
);

CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    type        TEXT NOT NULL CHECK (type IN ('service','software','addon','goods')),
    sales_price REAL NOT NULL CHECK (sales_price >= 0),
    cost_price  REAL NOT NULL CHECK (cost_price >= 0),
    tax_id      INTEGER REFERENCES taxes(id),
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS product_variants (
    id          INTEGER PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    attribute   TEXT NOT NULL,
    value       TEXT NOT NULL,
    extra_price REAL NOT NULL DEFAULT 0 CHECK (extra_price >= 0),
    UNIQUE (product_id, attribute, value)
);

CREATE TABLE IF NOT EXISTS plans (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    product_id     INTEGER NOT NULL REFERENCES products(id),
    price          REAL NOT NULL CHECK (price >= 0),
    billing_period TEXT NOT NULL CHECK (billing_period IN ('daily','weekly','monthly','yearly')),
    min_qty        INTEGER NOT NULL DEFAULT 1 CHECK (min_qty >= 1),
    start_date     TEXT NOT NULL,
    end_date       TEXT,
    auto_close     INTEGER NOT NULL DEFAULT 0,
    closable       INTEGER NOT NULL DEFAULT 1,
    pausable       INTEGER NOT NULL DEFAULT 1,
    renewable      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS quotation_templates (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    plan_id       INTEGER NOT NULL REFERENCES plans(id),
    validity_days INTEGER NOT NULL CHECK (validity_days BETWEEN 1 AND 365),
    term_periods  INTEGER NOT NULL DEFAULT 12 CHECK (term_periods >= 1)
);

CREATE TABLE IF NOT EXISTS quotation_template_lines (
    id          INTEGER PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES quotation_templates(id) ON DELETE CASCADE,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    variant_id  INTEGER REFERENCES product_variants(id),
    quantity    INTEGER NOT NULL CHECK (quantity >= 1)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id                  INTEGER PRIMARY KEY,
    number              TEXT UNIQUE,
    customer_id         INTEGER NOT NULL REFERENCES users(id),
    plan_id             INTEGER NOT NULL REFERENCES plans(id),
    status              TEXT NOT NULL CHECK (status IN ('draft','quotation','confirmed','active','paused','closed')),
    start_date          TEXT NOT NULL,
    expiry_date         TEXT,
    term_days           INTEGER,
    payment_terms_days  INTEGER NOT NULL DEFAULT 15 CHECK (payment_terms_days BETWEEN 0 AND 120),
    auto_renew          INTEGER NOT NULL DEFAULT 0,
    next_billing_date   TEXT,
    quote_valid_until   TEXT,
    paused_reason       TEXT,
    created_at          TEXT NOT NULL,
    closed_at           TEXT
);

CREATE TABLE IF NOT EXISTS subscription_lines (
    id              INTEGER PRIMARY KEY,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    product_id      INTEGER NOT NULL REFERENCES products(id),
    variant_id      INTEGER REFERENCES product_variants(id),
    quantity        INTEGER NOT NULL CHECK (quantity >= 1),
    unit_price      REAL NOT NULL CHECK (unit_price >= 0)
);

-- MRR history: one row whenever a subscription's recurring value changes.
-- Lets us compute MRR at any past date and the New/Expansion/Contraction/Churn waterfall.
CREATE TABLE IF NOT EXISTS mrr_log (
    id              INTEGER PRIMARY KEY,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    effective_date  TEXT NOT NULL,
    mrr             REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS discounts (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL CHECK (kind IN ('fixed','percent')),
    value        REAL NOT NULL CHECK (value > 0),
    min_purchase REAL NOT NULL DEFAULT 0 CHECK (min_purchase >= 0),
    min_qty      INTEGER NOT NULL DEFAULT 0 CHECK (min_qty >= 0),
    start_date   TEXT NOT NULL,
    end_date     TEXT,
    usage_limit  INTEGER CHECK (usage_limit IS NULL OR usage_limit >= 1),
    used_count   INTEGER NOT NULL DEFAULT 0,
    product_id   INTEGER REFERENCES products(id),     -- NULL = whole subscription
    audience     TEXT NOT NULL DEFAULT 'all' CHECK (audience IN ('all','assigned')),
    active       INTEGER NOT NULL DEFAULT 1,
    CHECK (kind <> 'percent' OR value <= 100)
);

CREATE TABLE IF NOT EXISTS subscription_discounts (
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    discount_id     INTEGER NOT NULL REFERENCES discounts(id) ON DELETE CASCADE,
    PRIMARY KEY (subscription_id, discount_id)
);

CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY,
    number          TEXT UNIQUE,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
    customer_id     INTEGER NOT NULL REFERENCES users(id),
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    invoice_date    TEXT NOT NULL,
    due_date        TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('draft','confirmed','paid','cancelled')),
    subtotal        REAL NOT NULL DEFAULT 0,
    discount_total  REAL NOT NULL DEFAULT 0,
    tax_total       REAL NOT NULL DEFAULT 0,
    total           REAL NOT NULL DEFAULT 0,
    amount_paid     REAL NOT NULL DEFAULT 0,
    paid_date       TEXT,
    sent_at         TEXT,
    anomaly_flag    INTEGER NOT NULL DEFAULT 0,
    anomaly_reasons TEXT,
    UNIQUE (subscription_id, period_start)          -- the "never bill twice" guarantee
);

CREATE TABLE IF NOT EXISTS invoice_lines (
    id          INTEGER PRIMARY KEY,
    invoice_id  INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    product_id  INTEGER REFERENCES products(id),
    description TEXT NOT NULL,
    quantity    INTEGER NOT NULL,
    unit_price  REAL NOT NULL,
    discount    REAL NOT NULL DEFAULT 0,
    tax_rate    REAL NOT NULL DEFAULT 0,
    tax_amount  REAL NOT NULL DEFAULT 0,
    amount      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS discount_usages (
    id          INTEGER PRIMARY KEY,
    discount_id INTEGER NOT NULL REFERENCES discounts(id),
    invoice_id  INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    amount      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id         INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    method     TEXT NOT NULL CHECK (method IN ('bank_transfer','card','upi','cash','cheque')),
    amount     REAL NOT NULL CHECK (amount > 0),
    date       TEXT NOT NULL,
    note       TEXT,
    created_by INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    level      INTEGER NOT NULL,          -- 0 = smart pre-due, 1 = day 1, 2 = day 7, 3 = day 14 (pause)
    sent_on    TEXT NOT NULL,
    UNIQUE (invoice_id, level)
);

CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY,
    to_email   TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
    id              INTEGER PRIMARY KEY,
    subscription_id INTEGER REFERENCES subscriptions(id) ON DELETE CASCADE,
    invoice_id      INTEGER REFERENCES invoices(id) ON DELETE CASCADE,
    actor           TEXT NOT NULL,
    message         TEXT NOT NULL,
    at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS predictions (
    entity_type TEXT NOT NULL,     -- 'invoice' or 'subscription'
    entity_id   INTEGER NOT NULL,
    kind        TEXT NOT NULL,     -- 'late_payment' or 'churn'
    score       REAL NOT NULL,
    reasons     TEXT,
    extra       TEXT,
    computed_on TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity_id, kind)
);

CREATE TABLE IF NOT EXISTS model_cards (
    name       TEXT PRIMARY KEY,
    metric     TEXT,
    n_samples  INTEGER,
    features   TEXT,
    trained_at TEXT
);

CREATE TABLE IF NOT EXISTS bank_lines (
    id                 INTEGER PRIMARY KEY,
    batch              TEXT NOT NULL,
    date               TEXT NOT NULL,
    amount             REAL NOT NULL,
    reference          TEXT,
    payer              TEXT,
    suggested_invoice  INTEGER REFERENCES invoices(id),
    confidence         REAL,
    status             TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','matched','ignored'))
);

CREATE TABLE IF NOT EXISTS match_feedback (
    id       INTEGER PRIMARY KEY,
    features TEXT NOT NULL,        -- JSON list of the 4 match features
    label    INTEGER NOT NULL      -- 1 = staff confirmed this pair, 0 = staff rejected it
);

CREATE INDEX IF NOT EXISTS ix_sub_status      ON subscriptions(status);
CREATE INDEX IF NOT EXISTS ix_sub_customer    ON subscriptions(customer_id);
CREATE INDEX IF NOT EXISTS ix_sub_nextbill    ON subscriptions(status, next_billing_date);
CREATE INDEX IF NOT EXISTS ix_subline_sub     ON subscription_lines(subscription_id);
CREATE INDEX IF NOT EXISTS ix_inv_status_due  ON invoices(status, due_date);
CREATE INDEX IF NOT EXISTS ix_inv_customer    ON invoices(customer_id);
CREATE INDEX IF NOT EXISTS ix_inv_sub         ON invoices(subscription_id);
CREATE INDEX IF NOT EXISTS ix_invline_inv     ON invoice_lines(invoice_id);
CREATE INDEX IF NOT EXISTS ix_pay_invoice     ON payments(invoice_id);
CREATE INDEX IF NOT EXISTS ix_pay_date        ON payments(date);
CREATE INDEX IF NOT EXISTS ix_mrr_sub_date    ON mrr_log(subscription_id, effective_date);
CREATE INDEX IF NOT EXISTS ix_mrr_date        ON mrr_log(effective_date);
CREATE INDEX IF NOT EXISTS ix_act_sub         ON activity(subscription_id);
CREATE INDEX IF NOT EXISTS ix_pred_kind       ON predictions(kind, score);
"""


def connect(path):
    conn = sqlite3.connect(path, detect_types=0, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def get_db():
    """One connection per request, closed automatically at teardown."""
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def ensure_database(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = connect(path)
    init_schema(conn)
    conn.close()


def get_setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
