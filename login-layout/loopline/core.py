"""Small shared helpers used by every module.

* The simulated clock ("Time Machine"): the whole app asks today(db) instead of
  date.today(), so the admin can fast-forward a billing month during a demo.
* Billing period arithmetic.
* Validation rules (the same rules are mirrored in static/app.js for instant feedback).
* Password hashing with PBKDF2 from the standard library (no extra dependency).
"""
import calendar
import hashlib
import hmac
import re
import secrets
from datetime import date, datetime, timedelta

from .db import get_setting, set_setting

ISO = "%Y-%m-%d"

# --------------------------------------------------------------------------- clock


def today(db):
    value = get_setting(db, "sim_date")
    return date.fromisoformat(value) if value else date.today()


def set_today(db, d):
    set_setting(db, "sim_date", d.isoformat())


def d(value):
    """Parse an ISO date string (or pass a date through)."""
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)


def now_stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ period math

PERIODS = ("daily", "weekly", "monthly", "yearly")
# Multiply a per-period price by this to get its monthly value (used for MRR).
MONTHLY_FACTOR = {"daily": 30.0, "weekly": 52.0 / 12.0, "monthly": 1.0, "yearly": 1.0 / 12.0}


def add_months(d0, months):
    month_index = d0.month - 1 + months
    year = d0.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d0.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def add_period(d0, period, count=1):
    if period == "daily":
        return d0 + timedelta(days=count)
    if period == "weekly":
        return d0 + timedelta(weeks=count)
    if period == "monthly":
        return add_months(d0, count)
    if period == "yearly":
        return add_months(d0, 12 * count)
    raise ValueError(f"Unknown billing period {period!r}")


def money(x):
    return round(float(x) + 1e-9, 2)


# ------------------------------------------------------------------ validation

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
SPECIAL_RE = re.compile(r"[^A-Za-z0-9]")


def password_problems(pw):
    """Rules from the brief: more than eight characters, upper, lower, special."""
    problems = []
    if len(pw) <= 8:
        problems.append("Use more than 8 characters.")
    if not re.search(r"[A-Z]", pw):
        problems.append("Add an uppercase letter.")
    if not re.search(r"[a-z]", pw):
        problems.append("Add a lowercase letter.")
    if not SPECIAL_RE.search(pw):
        problems.append("Add a special character, such as ! or #.")
    return problems


class Form:
    """Collects cleaned values and field errors from request.form.

    Usage:
        f = Form(request.form)
        name = f.text("name", required=True, max_len=80)
        if f.ok: ...
    """

    def __init__(self, data):
        self.data = data
        self.errors = {}

    @property
    def ok(self):
        return not self.errors

    def _raw(self, key):
        return (self.data.get(key) or "").strip()

    def text(self, key, required=False, max_len=200, label=None):
        v = self._raw(key)
        label = label or key.replace("_", " ").capitalize()
        if required and not v:
            self.errors[key] = f"{label} is required."
        elif len(v) > max_len:
            self.errors[key] = f"{label} must be at most {max_len} characters."
        return v

    def email(self, key="email"):
        v = self._raw(key).lower()
        if not v:
            self.errors[key] = "Email is required."
        elif not EMAIL_RE.match(v):
            self.errors[key] = "Enter a valid email address, like name@company.com."
        return v

    def number(self, key, required=True, minimum=None, maximum=None, integer=False, label=None):
        v = self._raw(key)
        label = label or key.replace("_", " ").capitalize()
        if not v:
            if required:
                self.errors[key] = f"{label} is required."
            return None
        try:
            num = int(v) if integer else float(v)
        except ValueError:
            self.errors[key] = f"{label} must be a {'whole ' if integer else ''}number."
            return None
        if num != num or num in (float("inf"), float("-inf")):
            self.errors[key] = f"{label} must be a real number."
            return None
        if minimum is not None and num < minimum:
            self.errors[key] = f"{label} must be at least {minimum}."
        if maximum is not None and num > maximum:
            self.errors[key] = f"{label} must be at most {maximum}."
        return num

    def date(self, key, required=True, label=None):
        v = self._raw(key)
        label = label or key.replace("_", " ").capitalize()
        if not v:
            if required:
                self.errors[key] = f"{label} is required."
            return None
        try:
            return date.fromisoformat(v)
        except ValueError:
            self.errors[key] = f"{label} must be a date (YYYY-MM-DD)."
            return None

    def choice(self, key, options, label=None):
        v = self._raw(key)
        label = label or key.replace("_", " ").capitalize()
        if v not in options:
            self.errors[key] = f"Choose a valid {label.lower()}."
        return v

    def flag(self, key):
        return 1 if self.data.get(key) in ("1", "on", "true", "yes") else 0

    def ref(self, key, db, table, required=True, label=None):
        """An id that must exist in the given table."""
        label = label or key.replace("_id", "").replace("_", " ").capitalize()
        v = self._raw(key)
        if not v:
            if required:
                self.errors[key] = f"Choose a {label.lower()}."
            return None
        if not v.isdigit() or not db.execute(f"SELECT 1 FROM {table} WHERE id = ?", (int(v),)).fetchone():
            self.errors[key] = f"Choose a valid {label.lower()}."
            return None
        return int(v)

    def check(self, condition, key, message):
        if not condition and key not in self.errors:
            self.errors[key] = message


# ------------------------------------------------------------------- passwords

_ITERATIONS = 120_000


def hash_password(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), _ITERATIONS).hex()
    return f"pbkdf2${_ITERATIONS}${salt}${digest}"


def verify_password(pw, stored):
    try:
        _, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    test = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), int(iterations)).hex()
    return hmac.compare_digest(test, digest)


# ---------------------------------------------------------------- side effects


def send_email(db, to_email, subject, body):
    """Emails go to a local outbox (shown at /mailbox) so the app works offline."""
    if get_setting(db, "seeding") == "1":  # no emails for generated history
        return
    db.execute(
        "INSERT INTO outbox(to_email, subject, body, created_at) VALUES (?,?,?,?)",
        (to_email, subject, body, now_stamp()),
    )


def log_activity(db, message, actor="system", subscription_id=None, invoice_id=None, on=None):
    db.execute(
        "INSERT INTO activity(subscription_id, invoice_id, actor, message, at) VALUES (?,?,?,?,?)",
        (subscription_id, invoice_id, actor, message, (on or today(db)).isoformat()),
    )
