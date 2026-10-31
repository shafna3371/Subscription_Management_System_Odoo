import os

import click
from flask import current_app

from . import db as dbmod


def reset_database():
    path = current_app.config["DATABASE"]
    dbmod.close_db()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    models = current_app.config.get("MODEL_DIR") or os.path.join(current_app.instance_path, "models")
    if os.path.isdir(models):
        for name in os.listdir(models):
            os.remove(os.path.join(models, name))
    dbmod.ensure_database(path)


def register(app):
    @app.cli.command("seed")
    @click.option("--customers", default=300, show_default=True, help="Simulated customers.")
    @click.option("--months", default=14, show_default=True, help="Months of history to simulate.")
    @click.option("--bulk", default=0, show_default=True, help="Extra subscriptions for load testing.")
    def seed_command(customers, months, bulk):
        """Wipe the database and build the demo data with simulated history."""
        from .seed import seed
        reset_database()
        click.echo(f"Seeding {customers} customers over {months} months (bulk extra: {bulk})...")
        seed(dbmod.get_db(), customers=customers, months=months, bulk=bulk, echo=click.echo)

    @app.cli.command("reset")
    def reset_command():
        """Delete all data and start with an empty database."""
        reset_database()
        click.echo("Database is empty. Run 'flask --app loopline seed' for demo data.")

    @app.cli.command("train")
    def train_command():
        """Retrain every model and re-score open invoices and subscriptions."""
        from . import ml
        db = dbmod.get_db()
        ml.train_all(db)
        ml.score_all(db)
        db.commit()
        click.echo("Models trained and predictions refreshed.")

    @app.cli.command("run-day")
    def run_day_command():
        """Run today's automatic jobs (activation, renewals, billing, reminders) and re-score.
        Schedule this once a day with cron (Linux/macOS) or Task Scheduler (Windows)."""
        from . import billing, ml
        from .core import today
        db = dbmod.get_db()
        before = db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        billing.run_day(db, today(db))
        ml.score_all(db)
        db.commit()
        created = db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] - before
        click.echo(f"Daily jobs done for {today(db)}: {created} new invoice(s).")

    @app.cli.command("clock-reset")
    def clock_reset_command():
        """Stop using the Time Machine date and follow the real calendar again."""
        db = dbmod.get_db()
        db.execute("DELETE FROM settings WHERE key = 'sim_date'")
        db.execute("INSERT INTO settings(key, value) VALUES ('simulate_customers', '0') "
                   "ON CONFLICT(key) DO UPDATE SET value = '0'")
        db.commit()
        click.echo("The app now uses the real date, and simulated customers are frozen.")

    @app.cli.command("create-admin")
    @click.option("--name", prompt="Full name")
    @click.option("--email", prompt="Email")
    @click.password_option()
    def create_admin_command(name, email, password):
        """Create the first admin account (needed when you start without demo data)."""
        from .views.auth import create_user
        db = dbmod.get_db()
        uid, errors = create_user(db, {"name": name, "email": email, "password": password, "confirm": password},
                                  "admin")
        if errors:
            for message in errors.values():
                click.echo(f"  {message}")
            raise SystemExit(1)
        db.commit()
        click.echo(f"Admin account {email.lower()} created.")
