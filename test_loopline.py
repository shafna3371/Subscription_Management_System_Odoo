"""Run with:  python -m unittest discover -s tests -v

Each test class builds its own small database in a temporary folder,
so tests never touch your demo data.
"""
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loopline import billing, create_app  # noqa: E402
from loopline.billing import BillingError  # noqa: E402
from loopline.core import password_problems, set_today, today  # noqa: E402
from loopline.db import get_db, set_setting  # noqa: E402
from loopline.seed import seed  # noqa: E402


def make_app():
    tmp = tempfile.mkdtemp()
    app = create_app({"TESTING": True, "TESTING_NO_CSRF": True, "DATABASE": os.path.join(tmp, "test.sqlite"),
                      "MODEL_DIR": os.path.join(tmp, "models")})
    with app.app_context():
        seed(get_db(), customers=40, months=5, echo=lambda *_: None)
        set_setting(get_db(), "simulate_customers", "0")   # keep tests deterministic
        get_db().commit()
    return app


class PasswordRules(unittest.TestCase):
    def test_rules_from_the_brief(self):
        self.assertTrue(password_problems("Short1!"))           # too short
        self.assertTrue(password_problems("alllowercase1!"))    # no uppercase
        self.assertTrue(password_problems("ALLUPPERCASE1!"))    # no lowercase
        self.assertTrue(password_problems("NoSpecialChar1"))    # no special character
        self.assertEqual(password_problems("Exactly9!"), [])    # 9 chars: more than eight
        self.assertTrue(password_problems("Eight8!a"))          # exactly 8 is not enough


class Billing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = make_app()

    def setUp(self):
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.db = get_db()

    def tearDown(self):
        self.db.rollback()
        self.ctx.pop()

    def invoice_count(self):
        return self.db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]

    def test_running_billing_again_creates_nothing(self):
        billing.run_billing(self.db)
        before = self.invoice_count()
        for _ in range(3):
            self.assertEqual(billing.run_billing(self.db), [])
        self.assertEqual(self.invoice_count(), before)

    def test_crash_recovery_never_duplicates(self):
        billing.run_billing(self.db)
        self.db.execute("UPDATE subscriptions SET next_billing_date = (SELECT MAX(period_start) FROM invoices "
                        "WHERE subscription_id = subscriptions.id) WHERE status = 'active'")
        stats = {}
        self.assertEqual(billing.run_billing(self.db, stats=stats), [])
        self.assertGreater(stats.get("already_billed", 0), 0)
        dupes = self.db.execute("SELECT COUNT(*) FROM (SELECT 1 FROM invoices GROUP BY subscription_id, "
                                "period_start HAVING COUNT(*) > 1)").fetchone()[0]
        self.assertEqual(dupes, 0)

    def test_database_rejects_duplicate_period(self):
        row = self.db.execute("SELECT subscription_id, period_start FROM invoices LIMIT 1").fetchone()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO invoices(subscription_id, customer_id, period_start, period_end, "
                            "invoice_date, due_date, status) SELECT subscription_id, customer_id, period_start, "
                            "period_end, invoice_date, due_date, 'draft' FROM invoices WHERE subscription_id = ? "
                            "AND period_start = ?", (row["subscription_id"], row["period_start"]))

    def test_outage_catch_up_bills_every_missed_period_once(self):
        sub = self.db.execute("SELECT s.id FROM subscriptions s JOIN plans p ON p.id = s.plan_id WHERE "
                              "s.status = 'active' AND p.billing_period = 'weekly' AND s.expiry_date IS NULL "
                              "LIMIT 1").fetchone()
        if sub is None:
            self.skipTest("no open-ended weekly subscription in this seed")
        set_today(self.db, today(self.db) + timedelta(days=21))
        created = billing.run_billing(self.db)
        mine = self.db.execute(f"SELECT period_start FROM invoices WHERE subscription_id = ? AND id IN "
                               f"({','.join('?' * len(created))})", (sub["id"], *created)).fetchall()
        self.assertGreaterEqual(len(mine), 3)              # three weeks missed, three invoices
        self.assertEqual(len(mine), len({r[0] for r in mine}))

    def test_tax_is_calculated_from_rules(self):
        line = self.db.execute("SELECT * FROM invoice_lines WHERE tax_rate > 0 AND discount = 0 LIMIT 1").fetchone()
        expected = round(line["quantity"] * line["unit_price"] * line["tax_rate"] / 100, 2)
        self.assertAlmostEqual(line["tax_amount"], expected, places=2)

    def test_discount_usage_limit_is_never_exceeded(self):
        self.db.execute("INSERT INTO discounts(name, kind, value, start_date, usage_limit, audience) "
                        "VALUES ('Only once', 'percent', 5, '2000-01-01', 1, 'all')")
        set_today(self.db, today(self.db) + timedelta(days=40))
        billing.run_billing(self.db)
        used = self.db.execute("SELECT used_count FROM discounts WHERE name = 'Only once'").fetchone()[0]
        applied = self.db.execute("SELECT COUNT(*) FROM discount_usages du JOIN discounts d ON d.id = "
                                  "du.discount_id WHERE d.name = 'Only once'").fetchone()[0]
        self.assertEqual((used, applied), (1, 1))

    def test_payments(self):
        inv = self.db.execute("SELECT * FROM invoices WHERE status = 'confirmed' LIMIT 1").fetchone()
        owed = round(inv["total"] - inv["amount_paid"], 2)
        with self.assertRaises(BillingError):
            billing.record_payment(self.db, inv["id"], "card", owed + 1, today(self.db))     # overpayment
        with self.assertRaises(BillingError):
            billing.record_payment(self.db, inv["id"], "card", 1, today(self.db) + timedelta(days=1))  # future
        billing.record_payment(self.db, inv["id"], "upi", owed, today(self.db), "test")
        self.assertEqual(billing.invoice(self.db, inv["id"])["status"], "paid")

    def test_invalid_status_change_is_refused(self):
        closed = self.db.execute("SELECT id FROM subscriptions WHERE status = 'closed' LIMIT 1").fetchone()
        if closed is None:
            self.skipTest("no closed subscription in this seed")
        with self.assertRaises(BillingError):
            billing.set_status(self.db, closed["id"], "active")


class WebAndRoles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = make_app()

    def login(self, email, password):
        c = self.app.test_client()
        r = c.post("/login", data={"email": email, "password": password})
        self.assertEqual(r.status_code, 302, email)
        return c

    def test_signup_validation(self):
        c = self.app.test_client()
        weak = c.post("/signup", data={"name": "A", "email": "a@x.test", "password": "weak", "confirm": "weak"})
        self.assertEqual(weak.status_code, 422)
        taken = c.post("/signup", data={"name": "A", "email": "admin@brightloop.test", "password": "Strong@123",
                                        "confirm": "Strong@123"})
        self.assertIn(b"already exists", taken.data)
        ok = c.post("/signup", data={"name": "New Person", "email": "new@x.test", "password": "Strong@123",
                                     "confirm": "Strong@123"})
        self.assertEqual(ok.status_code, 302)

    def test_every_staff_page_loads_fast(self):
        c = self.login("admin@brightloop.test", "Admin@12345")
        for url in ["/dashboard", "/risk", "/subscriptions", "/subscriptions/new", "/invoices", "/payments",
                    "/discounts", "/reconcile", "/products", "/plans", "/templates", "/taxes", "/models",
                    "/time-machine", "/review", "/users", "/mailbox", "/api/v1/kpis"]:
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertLess(int(r.headers["X-Response-Time-ms"]), 2000, url)

    def test_role_limits(self):
        staff = self.login("staff@brightloop.test", "Staff@12345")
        self.assertEqual(staff.get("/discounts").status_code, 200)
        self.assertEqual(staff.post("/discounts", data={"name": "x"}).status_code, 403)   # only admin creates
        self.assertEqual(staff.get("/time-machine").status_code, 403)
        self.assertEqual(staff.get("/users").status_code, 403)                          # only admin adds staff
        customer = self.login("customer@brightloop.test", "Customer@123")
        self.assertEqual(customer.get("/dashboard").status_code, 403)
        self.assertEqual(customer.get("/portal/").status_code, 200)

    def test_customer_sees_only_own_data(self):
        with self.app.app_context():
            db = get_db()
            me = db.execute("SELECT id FROM users WHERE email = 'customer@brightloop.test'").fetchone()["id"]
            other = db.execute("SELECT id FROM invoices WHERE customer_id <> ? AND status <> 'draft' LIMIT 1",
                               (me,)).fetchone()["id"]
        c = self.login("customer@brightloop.test", "Customer@123")
        self.assertEqual(c.get(f"/portal/invoices/{other}/print").status_code, 404)
        items = c.get("/api/v1/invoices").get_json()["items"]
        names = {i["customer"] for i in items}
        self.assertLessEqual(len(names), 1)

    def test_password_reset_flow(self):
        c = self.app.test_client()
        c.post("/signup", data={"name": "Reset Me", "email": "reset@x.test", "password": "First@1234",
                                "confirm": "First@1234"})
        c = self.app.test_client()
        c.post("/forgot", data={"email": "reset@x.test"})
        with self.app.app_context():
            body = get_db().execute("SELECT body FROM outbox WHERE subject LIKE 'Reset%' ORDER BY id DESC").fetchone()[0]
        token = re.search(r"/reset/([\w-]+)", body).group(1)
        r = c.post(f"/reset/{token}", data={"password": "Brand@New99", "confirm": "Brand@New99"})
        self.assertEqual(r.status_code, 302)
        self.login("reset@x.test", "Brand@New99")
        self.assertEqual(c.post(f"/reset/{token}", data={"password": "Again@New99", "confirm": "Again@New99"})
                         .status_code, 302)   # second use bounces back to /forgot


if __name__ == "__main__":
    unittest.main()
