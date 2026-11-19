"""Nova membership and Stripe-hosted billing."""
import hmac
import os
import re
from datetime import timedelta
from functools import wraps

import httpx
import psycopg
import stripe
from flask import Blueprint, current_app, jsonify, request, session

membership = Blueprint("membership", __name__, url_prefix="/api/membership")
ACTIVE = {"trialing", "active"}


def install_membership(app):
    enabled = os.getenv("NOVA_MEMBERSHIP_ENABLED") == "1"
    app.config["NOVA_MEMBERSHIP_ENABLED"] = enabled
    if enabled:
        for key in (
            "NOVA_SESSION_SECRET", "NOVA_SITE_URL",
            "SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY", "STRIPE_SECRET_KEY", "STRIPE_PRICE_ID",
        ):
            if not os.getenv(key):
                raise RuntimeError(f"Missing membership configuration: {key}")
        secret = os.environ["NOVA_SESSION_SECRET"]
        if len(secret) < 32:
            raise RuntimeError("NOVA_SESSION_SECRET must have at least 32 characters")
        app.config.update(
            SECRET_KEY=secret, SESSION_COOKIE_NAME="nova_membership",
            SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=timedelta(days=7),
            SESSION_REFRESH_EACH_REQUEST=False,
        )
        stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
        mode = os.getenv("NOVA_STRIPE_MODE", "test")
        if mode not in {"test", "live"}:
            raise RuntimeError("NOVA_STRIPE_MODE must be test or live")
        if not stripe.api_key.startswith((f"sk_{mode}_", f"rk_{mode}_")):
            raise RuntimeError("Stripe credentials must match NOVA_STRIPE_MODE")
        app.config["NOVA_STRIPE_MODE"] = mode
        stripe.default_http_client = stripe.HTTPXClient(timeout=15, allow_sync_methods=True)
        try:
            with connect() as db:
                db.execute("SELECT pg_advisory_xact_lock(38204912)")
                db.execute("""
                    CREATE TABLE IF NOT EXISTS nova_membership (
                        user_id uuid PRIMARY KEY,
                        stripe_customer text UNIQUE,
                        checkout_session text
                    )
                """)
        except psycopg.Error as error:
            detail = str(error).lower()
            if error.sqlstate == "28P01" or "password authentication failed" in detail:
                reason = "database password rejected"
            elif "tenant or user not found" in detail:
                reason = "pooler does not recognize the database user"
            elif "timed out" in detail or "timeout expired" in detail:
                reason = "database connection timed out"
            elif isinstance(error, psycopg.ProgrammingError):
                reason = "invalid database configuration or query"
            else:
                reason = "database connection or initialization error"
            raise RuntimeError(f"Nova membership database initialization failed: {reason}.") from None
    app.register_blueprint(membership)


def connect():
    password = os.getenv("NOVA_BILLING_DATABASE_PASSWORD")
    if password:
        host = os.getenv("NOVA_BILLING_DATABASE_HOST")
        user = os.getenv("NOVA_BILLING_DATABASE_USER")
        if not host or not user:
            raise psycopg.ProgrammingError("Set the database host and user in Railway.")
        return psycopg.connect(
            host=host, port=5432, dbname="postgres", user=user, password=password,
            sslmode="require", connect_timeout=5,
        )

    url = os.getenv("NOVA_BILLING_DATABASE_URL", "")
    if not url.startswith(("postgresql://", "postgres://")):
        raise psycopg.ProgrammingError("Set the database password or a PostgreSQL connection URL.")
    return psycopg.connect(url, connect_timeout=5)


def site_url():
    return os.environ["NOVA_SITE_URL"].rstrip("/")


def price_id():
    return os.environ["STRIPE_PRICE_ID"]


def account_id():
    return session.get("membership_user")


def auth_call(path, data):
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/auth/v1/" + path
    return httpx.post(url, json=data, headers={
        "apikey": os.environ["SUPABASE_PUBLISHABLE_KEY"],
    }, timeout=10)


def subscription_state(customer):
    if not customer:
        return False, False, False
    had_membership = False
    payment_pending = False
    subscriptions = stripe.Subscription.list(customer=customer, status="all", limit=100)
    for subscription in subscriptions.auto_paging_iter():
        if not any(item.price.id == price_id() for item in subscription["items"].data):
            continue
        had_membership = True
        if subscription.status in ACTIVE:
            return True, True, False
        if subscription.status in {"past_due", "unpaid", "incomplete", "paused"}:
            payment_pending = True
    return False, had_membership, payment_pending


def membership_customer(user_id):
    with connect() as db:
        row = db.execute(
            "SELECT stripe_customer FROM nova_membership WHERE user_id = %s", (user_id,)
        ).fetchone()
    return row[0] if row else None


@membership.before_request
def validate_request():
    if not current_app.config["NOVA_MEMBERSHIP_ENABLED"]:
        if request.endpoint == "membership.status":
            return jsonify(enabled=False)
        return jsonify(error="Membership is unavailable."), 404
    if request.method == "POST":
        if not hmac.compare_digest(request.headers.get("Origin", ""), site_url()):
            return jsonify(error="Open membership from the Nova website."), 403
        if not request.is_json:
            return jsonify(error="Expected JSON."), 415


@membership.after_request
def prevent_caching(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def data():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def email_address():
    return str(data().get("email", "")).strip().lower()[:255]


@membership.post("/code")
def send_code():
    email = email_address()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify(error="Enter a valid email address."), 400
    try:
        result = auth_call("otp", {"email": email, "create_user": True})
        if result.status_code == 429:
            return jsonify(error="Please wait before requesting another code."), 429
        result.raise_for_status()
    except httpx.HTTPStatusError as error:
        response = error.response
        provider_code = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                provider_code = payload.get("error_code") or payload.get("code")
        except ValueError:
            pass
        if not isinstance(provider_code, str) or not re.fullmatch(r"[a-z0-9_]{1,60}", provider_code):
            provider_code = "unknown"
        current_app.logger.warning("Nova email code failed: auth_status=%s auth_code=%s",
                                   response.status_code, provider_code)
        return jsonify(error="Could not send a code. Try again shortly."), 502
    except httpx.RequestError as error:
        current_app.logger.warning("Nova email code could not reach auth: %s", type(error).__name__)
        return jsonify(error="Could not send a code. Try again shortly."), 502
    return jsonify(ok=True)


@membership.post("/verify")
def verify_code():
    email, token = email_address(), str(data().get("code", "")).strip()
    if not email or not re.fullmatch(r"[0-9]{6}", token):
        return jsonify(error="Enter the six-digit code from your email."), 400
    try:
        response = auth_call("verify", {"email": email, "token": token, "type": "email"})
        if response.status_code in (400, 401, 403, 422):
            return jsonify(error="This code is invalid or expired."), 400
        if response.status_code == 429:
            return jsonify(error="Too many attempts. Try again shortly."), 429
        response.raise_for_status()
        user = response.json()["user"]
    except (httpx.HTTPError, ValueError, KeyError):
        return jsonify(error="Sign-in is temporarily unavailable."), 502
    session.clear()
    session.permanent = True
    session["membership_user"] = user["id"]
    session["membership_email"] = user["email"]
    return jsonify(ok=True)


@membership.post("/signout")
def signout():
    session.clear()
    return jsonify(ok=True)


@membership.get("/status")
def status():
    if not account_id():
        return jsonify(enabled=True, signedIn=False)
    customer = membership_customer(account_id())
    active, previous, payment_pending = subscription_state(customer)
    return jsonify(enabled=True, signedIn=True, active=active, trialEligible=not previous,
                   canManage=bool(customer), paymentPending=payment_pending, email=session["membership_email"])


@membership.post("/checkout")
def checkout():
    user_id = account_id()
    if not user_id:
        return jsonify(error="Sign in first."), 401
    with connect() as db:
        db.execute("INSERT INTO nova_membership (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
        customer, pending = db.execute(
            "SELECT stripe_customer, checkout_session FROM nova_membership WHERE user_id = %s FOR UPDATE",
            (user_id,),
        ).fetchone()
        if not customer:
            customer = stripe.Customer.create(
                email=session["membership_email"], metadata={"nova_user_id": user_id},
                idempotency_key=f"nova-customer-{user_id}",
            ).id
            db.execute("UPDATE nova_membership SET stripe_customer = %s WHERE user_id = %s", (customer, user_id))
        active, previous, payment_pending = subscription_state(customer)
        if active:
            return jsonify(error="Your membership already has access."), 409
        if payment_pending:
            return jsonify(error="Open Manage Subscription to review your payment."), 409
        if previous and data().get("acceptMonthlyCharge") is not True:
            return jsonify(error="Confirm the monthly price before restarting your membership."), 409
        if pending:
            existing = stripe.checkout.Session.retrieve(pending)
            if existing.status == "open":
                return jsonify(url=existing.url)
        price = stripe.Price.retrieve(price_id())
        if (price.livemode != (current_app.config["NOVA_STRIPE_MODE"] == "live")
                or not price.active or price.currency != "usd" or price.unit_amount != 999
                or not price.recurring or price.recurring.interval != "month"
                or price.recurring.interval_count != 1):
            return jsonify(error="Membership pricing is unavailable."), 503
        # The v2 payload omits the legacy payment_method_types field.
        checkout_session = stripe.checkout.Session.create(
            customer=customer, mode="subscription", payment_method_collection="always",
            line_items=[{"price": price_id(), "quantity": 1}],
            subscription_data={} if previous else {"trial_period_days": 3},
            success_url=site_url() + "/?membership=complete",
            cancel_url=site_url() + "/?membership=return",
            idempotency_key=f"nova-checkout-v2-{customer}-{pending or 'first'}",
        )
        db.execute("UPDATE nova_membership SET checkout_session = %s WHERE user_id = %s",
                   (checkout_session.id, user_id))
    return jsonify(url=checkout_session.url)


@membership.post("/portal")
def portal():
    if not account_id():
        return jsonify(error="Sign in first."), 401
    customer = membership_customer(account_id())
    if not customer:
        return jsonify(error="There is no subscription to manage yet."), 404
    portal_session = stripe.billing_portal.Session.create(customer=customer, return_url=site_url() + "/?membership=return")
    return jsonify(url=portal_session.url)


@membership.errorhandler(stripe.StripeError)
@membership.errorhandler(psycopg.Error)
def billing_error(error):
    if isinstance(error, stripe.StripeError):
        fields = (error.code, getattr(error, "param", None), error.request_id)
        safe_fields = [
            value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_\[\].-]{1,100}", value)
            else "-"
            for value in fields
        ]
        current_app.logger.warning(
            "Membership Stripe error: endpoint=%s type=%s status=%s code=%s param=%s request_id=%s",
            request.endpoint, type(error).__name__, error.http_status, *safe_fields,
        )
    else:
        current_app.logger.warning("Membership database error: %s", type(error).__name__)
    return jsonify(error="Membership is temporarily unavailable. Try again shortly."), 503


def membership_required(view):
    @wraps(view)
    def gated(*args, **kwargs):
        if current_app.config["NOVA_MEMBERSHIP_ENABLED"]:
            if not account_id():
                return jsonify(error="Sign in and start a Nova membership to continue.", code="membership_required"), 401
            try:
                active, _, _ = subscription_state(membership_customer(account_id()))
            except (stripe.StripeError, psycopg.Error) as error:
                return billing_error(error)
            if not active:
                return jsonify(error="Start or renew your Nova membership to continue.", code="membership_required"), 402
        return view(*args, **kwargs)
    return gated
