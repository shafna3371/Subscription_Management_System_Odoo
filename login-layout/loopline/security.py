"""Role-based access control.

admin    - everything, including products, plans, taxes, discounts, staff and the Time Machine
staff    - day-to-day work: subscriptions, invoices, payments, reconciliation, reports
customer - only their own subscriptions and invoices, through the portal
"""
from functools import wraps

from flask import abort, flash, g, redirect, request, url_for

STAFF = ("admin", "staff")


def login_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("auth.login", next=request.path))
            if roles and g.user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def form_errors(errors):
    for message in errors.values():
        flash(message, "error")
