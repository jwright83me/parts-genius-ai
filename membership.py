"""Nova membership and Stripe-hosted billing."""
import hmac
import os
import re
from datetime import datetime, timedelta, timezone
from functools import wraps
from uuid import uuid4

import httpx
import psycopg
import stripe
from flask import Blueprint, current_app, g, jsonify, request, session

membership = Blueprint("membership", __name__, url_prefix="/api/membership")
ACTIVE = {"trialing", "active"}
PACK_PRICE_CENTS = 499
PACK_USAGE_CENTS = 200
PREMIUM_PRICE_CENTS = 1999
PREMIUM_LOOKUP_KEY = "nova_premium_monthly_sandbox_v1"


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
                db.execute("CREATE SCHEMA IF NOT EXISTS nova_private")
                db.execute("""
                    CREATE TABLE IF NOT EXISTS nova_private.ai_usage (
                        response_id text PRIMARY KEY,
                        user_id uuid NOT NULL,
                        recorded_at timestamptz NOT NULL DEFAULT now(),
                        feature text NOT NULL,
                        model text NOT NULL,
                        input_tokens bigint NOT NULL CHECK (input_tokens >= 0),
                        cached_input_tokens bigint NOT NULL CHECK (cached_input_tokens >= 0),
                        output_tokens bigint NOT NULL CHECK (output_tokens >= 0),
                        web_search_calls integer NOT NULL CHECK (web_search_calls >= 0)
                    )
                """)
                db.execute("""
                    CREATE INDEX IF NOT EXISTS nova_ai_usage_user_time
                        ON nova_private.ai_usage (user_id, recorded_at)
                """)
                db.execute("""
                    ALTER TABLE nova_private.ai_usage ADD COLUMN IF NOT EXISTS cycle_start timestamptz,
                        ADD COLUMN IF NOT EXISTS cost_nano bigint
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS nova_private.ai_reservations (
                        id uuid PRIMARY KEY,
                        user_id uuid NOT NULL,
                        cycle_start timestamptz NOT NULL,
                        amount_nano bigint NOT NULL CHECK (amount_nano > 0)
                    )
                """)
                db.execute("""
                    ALTER TABLE nova_private.ai_reservations
                    ADD COLUMN IF NOT EXISTS feature text NOT NULL DEFAULT 'diagnose',
                    ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now(),
                    ADD COLUMN IF NOT EXISTS call_id text,
                    ADD COLUMN IF NOT EXISTS closing boolean NOT NULL DEFAULT false,
                    ADD COLUMN IF NOT EXISTS ended boolean NOT NULL DEFAULT false,
                    ADD COLUMN IF NOT EXISTS released_at timestamptz
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS nova_private.usage_packs (
                        id uuid PRIMARY KEY,
                        user_id uuid NOT NULL,
                        stripe_session text UNIQUE,
                        price_cents integer NOT NULL CHECK (price_cents > 0),
                        grant_cents integer NOT NULL CHECK (grant_cents > 0),
                        created_at timestamptz NOT NULL DEFAULT now(),
                        cycle_start timestamptz,
                        state text NOT NULL DEFAULT 'pending'
                            CHECK (state IN ('pending', 'expired', 'granted'))
                    )
                """)
                db.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS nova_pending_usage_pack
                    ON nova_private.usage_packs (user_id) WHERE state = 'pending'
                """)
                db.execute("""
                    CREATE INDEX IF NOT EXISTS nova_ai_reservations_user_cycle
                        ON nova_private.ai_reservations (user_id, cycle_start)
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


def is_premium_price(price):
    recurring = getattr(price, "recurring", None)
    return (
        getattr(price, "lookup_key", None) == PREMIUM_LOOKUP_KEY
        and getattr(price, "livemode", None) is False
        and getattr(price, "unit_amount", None) == PREMIUM_PRICE_CENTS
        and getattr(price, "currency", None) == "usd"
        and getattr(recurring, "interval", None) == "month"
        and getattr(recurring, "interval_count", None) == 1
    )


def nova_subscription_item(subscription):
    allow_premium = current_app.config["NOVA_STRIPE_MODE"] == "test"
    return next((item for item in subscription["items"].data
                 if item.price.id == price_id() or (allow_premium and is_premium_price(item.price))), None)


def subscription_state(customer, *, include_invoice=False):
    if not customer:
        return None, False, False
    had_membership = False
    payment_pending = False
    options = {"expand": ["data.latest_invoice"]} if include_invoice else {}
    subscriptions = stripe.Subscription.list(customer=customer, status="all", limit=100, **options)
    for subscription in subscriptions.auto_paging_iter():
        if nova_subscription_item(subscription) is None:
            continue
        had_membership = True
        if subscription.status in ACTIVE:
            return subscription, True, False
        if subscription.status in {"past_due", "unpaid", "incomplete", "paused"}:
            payment_pending = True
    return None, had_membership, payment_pending


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
    if not email or not re.fullmatch(r"(?:[0-9]{6}|[0-9]{8})", token):
        return jsonify(error="Enter the code from your email."), 400
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
    item = nova_subscription_item(active) if active else None
    premium = item is not None and is_premium_price(item.price)
    return jsonify(enabled=True, signedIn=True, active=bool(active), trialEligible=not previous,
                   premium=premium, showPremium=current_app.config["NOVA_STRIPE_MODE"] == "test",
                   canUpgrade=bool(active and active.status == "active" and not premium),
                   canManage=bool(customer), paymentPending=payment_pending, email=session["membership_email"],
                   usagePacks=bool(active) and current_app.config["NOVA_STRIPE_MODE"] == "test")


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
        checkout_options = {}
        if current_app.config["NOVA_STRIPE_MODE"] == "live":
            checkout_options = {
                "automatic_tax": {"enabled": True},
                "billing_address_collection": "required",
                "customer_update": {"address": "auto"},
            }
        # The v2 payload omits the legacy payment_method_types field.
        checkout_session = stripe.checkout.Session.create(
            customer=customer, mode="subscription", payment_method_collection="always",
            line_items=[{"price": price_id(), "quantity": 1}],
            subscription_data={} if previous else {"trial_period_days": 3},
            success_url=site_url() + "/?membership=complete",
            cancel_url=site_url() + "/?membership=return",
            idempotency_key=f"nova-checkout-v2-{customer}-{pending or 'first'}",
            **checkout_options,
        )
        db.execute("UPDATE nova_membership SET checkout_session = %s WHERE user_id = %s",
                   (checkout_session.id, user_id))
    return jsonify(url=checkout_session.url)


def pack_window():
    # Imported here because usage shares the membership database connection.
    from usage import UsageUnavailable, billing_window
    if current_app.config["NOVA_STRIPE_MODE"] != "test" or not account_id():
        return None, None
    customer = membership_customer(account_id())
    subscription, _, _ = subscription_state(customer, include_invoice=True)
    try:
        return customer, billing_window(subscription)
    except UsageUnavailable:
        return customer, None


def reconcile_usage_pack(db, user_id, customer, window):
    order = db.execute("""
        SELECT id, stripe_session, price_cents, grant_cents
        FROM nova_private.usage_packs
        WHERE user_id = %s AND state = 'pending' FOR UPDATE
        """, (user_id,)).fetchone()
    if order is None or order[1] is None:
        return order, None
    payment = stripe.checkout.Session.retrieve(order[1])
    if payment.status == "expired":
        db.execute("UPDATE nova_private.usage_packs SET state = 'expired' WHERE id = %s", (order[0],))
        return None, None
    if payment.status != "complete" or payment.payment_status != "paid":
        return order, payment

    metadata = payment.metadata
    if (payment.livemode or payment.mode != "payment" or payment.customer != customer
            or payment.currency != "usd" or payment.amount_total != order[2]
            or getattr(metadata, "nova_usage_pack", None) != str(order[0])):
        raise ValueError("Stripe usage pack payment did not match its recorded order.")

    db.execute("""
        UPDATE nova_private.usage_packs SET state = 'granted', cycle_start = %s
        WHERE id = %s AND state = 'pending'
        """, (window.start, order[0]))
    return None, None


@membership.get("/usage-packs")
def usage_packs_status():
    customer, window = pack_window()
    if window is None:
        return jsonify(enabled=False)
    try:
        with connect() as db:
            _, payment = reconcile_usage_pack(db, account_id(), customer, window)
            granted = db.execute("""
                SELECT coalesce(sum(grant_cents), 0)
                FROM nova_private.usage_packs
                WHERE user_id = %s AND cycle_start = %s AND state = 'granted'
                """, (account_id(), window.start)).fetchone()[0]
    except ValueError:
        current_app.logger.warning("Nova could not verify a Stripe usage pack payment")
        return jsonify(error="Nova could not verify the credit purchase."), 503
    return jsonify(enabled=True, grantedCents=granted, end=window.end.isoformat(),
                   canBuy=window.end - datetime.now(timezone.utc) >= timedelta(minutes=35),
                   pending=payment is not None and payment.status == "complete")


@membership.post("/usage-packs")
def usage_pack_checkout():
    customer, window = pack_window()
    if window is None:
        return jsonify(error="An active Nova Test membership is required."), 403
    now = datetime.now(timezone.utc)
    if window.end - now < timedelta(minutes=35):
        return jsonify(error="You can add credits once your next membership period begins."), 409

    user_id = account_id()
    try:
        with connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (user_id,))
            order, payment = reconcile_usage_pack(db, user_id, customer, window)
            if payment is not None:
                if payment.status == "open":
                    return jsonify(url=payment.url)
                return jsonify(error="Your previous credit payment is still pending."), 409
            if order is None:
                order = db.execute("""
                    INSERT INTO nova_private.usage_packs (id, user_id, price_cents, grant_cents)
                    VALUES (%s, %s, %s, %s) RETURNING id, stripe_session, price_cents, grant_cents
                    """, (uuid4(), user_id, PACK_PRICE_CENTS, PACK_USAGE_CENTS)).fetchone()
    except ValueError:
        current_app.logger.warning("Nova could not verify a Stripe usage pack payment")
        return jsonify(error="Nova could not verify the previous credit purchase."), 503

    # Keep the order ID committed before sending it to Stripe, so a timeout or
    # interrupted database write can retry with the same Checkout Session key.
    with connect() as db:
        row = db.execute("""
            SELECT stripe_session, created_at FROM nova_private.usage_packs
            WHERE id = %s AND state = 'pending' FOR UPDATE
            """, (order[0],)).fetchone()
        if row is None:
            return jsonify(error="Open Membership to check your credit purchase."), 409
        if row[0]:
            existing = stripe.checkout.Session.retrieve(row[0])
            if existing.status == "open":
                return jsonify(url=existing.url)
            return jsonify(error="Open Membership to check your credit purchase."), 409
        # Use stable request parameters for retries within the Stripe idempotency window.
        if now - row[1] > timedelta(hours=23):
            db.execute("UPDATE nova_private.usage_packs SET state = 'expired' WHERE id = %s", (order[0],))
            return jsonify(error="The unfinished checkout expired. Please try again."), 409
        payment = stripe.checkout.Session.create(
            customer=customer, mode="payment",
            line_items=[{"price_data": {
                "currency": "usd", "unit_amount": order[2],
                "product_data": {"name": "Nova extra AI usage (test)"},
            }, "quantity": 1}],
            metadata={"nova_usage_pack": str(order[0])},
            success_url=site_url() + "/?membership=credits",
            cancel_url=site_url() + "/?membership=return",
            idempotency_key=f"nova-usage-pack-v1-{order[0]}",
        )
        db.execute("UPDATE nova_private.usage_packs SET stripe_session = %s WHERE id = %s",
                   (payment.id, order[0]))
    return jsonify(url=payment.url)


def premium_price(base_price):
    matches = stripe.Price.list(lookup_keys=[PREMIUM_LOOKUP_KEY], limit=1).data
    if matches:
        return matches[0]
    return stripe.Price.create(
        currency="usd", unit_amount=PREMIUM_PRICE_CENTS,
        recurring={"interval": "month"}, product_data={"name": "Nova Premium (sandbox)"},
        lookup_key=PREMIUM_LOOKUP_KEY,
        tax_behavior=getattr(base_price, "tax_behavior", None) or "unspecified",
        idempotency_key=PREMIUM_LOOKUP_KEY,
    )


def premium_portal_configuration(price):
    marker = f"nova-premium-upgrade-v1-{price.id}"
    configs = stripe.billing_portal.Configuration.list(active=True, limit=100)
    for config in configs.auto_paging_iter():
        if getattr(config.metadata, "nova_flow", None) == marker:
            return config
    return stripe.billing_portal.Configuration.create(
        features={"payment_method_update": {"enabled": True}, "subscription_update": {
            "enabled": True, "default_allowed_updates": ["price"],
            "proration_behavior": "always_invoice",
            "products": [{"product": price.product, "prices": [price.id]}],
        }},
        metadata={"nova_flow": marker},
        idempotency_key=marker,
    )


@membership.post("/premium")
def upgrade_membership():
    if current_app.config["NOVA_STRIPE_MODE"] != "test":
        return jsonify(error="Premium upgrades are available in Nova Test only."), 404
    if not account_id():
        return jsonify(error="Sign in first."), 401
    customer = membership_customer(account_id())
    subscription, _, _ = subscription_state(customer, include_invoice=True)
    if not subscription or subscription.status != "active":
        return jsonify(error="Premium upgrades are available once the paid membership starts."), 409
    item = nova_subscription_item(subscription)
    if is_premium_price(item.price):
        return jsonify(error="Your account already has Premium."), 409
    if (len(subscription["items"].data) != 1 or subscription["items"].has_more
            or getattr(subscription.latest_invoice, "status", None) != "paid"
            or getattr(subscription, "cancel_at_period_end", False)
            or getattr(subscription, "cancel_at", None)
            or getattr(subscription, "schedule", None)
            or getattr(subscription, "pending_update", None)):
        return jsonify(error="Resolve any pending billing changes in Manage Subscription first."), 409

    price = premium_price(item.price)
    if not is_premium_price(price) or not price.active:
        return jsonify(error="Premium pricing is temporarily unavailable."), 503
    config = premium_portal_configuration(price)
    return_url = site_url() + "/?membership=premium"
    portal_session = stripe.billing_portal.Session.create(
        customer=customer, configuration=config.id, return_url=return_url,
        flow_data={
            "type": "subscription_update_confirm",
            "subscription_update_confirm": {
                "subscription": subscription.id, "items": [{"id": item.id, "price": price.id}],
            },
            "after_completion": {"type": "redirect", "redirect": {"return_url": return_url}},
        },
    )
    return jsonify(url=portal_session.url)


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
                active, _, _ = subscription_state(
                    membership_customer(account_id()),
                    include_invoice=request.endpoint in {"diagnose", "find_verified_parts", "create_voice_session"},
                )
            except (stripe.StripeError, psycopg.Error) as error:
                return billing_error(error)
            if not active:
                return jsonify(error="Start or renew your Nova membership to continue.", code="membership_required"), 402
            g.nova_subscription = active
        return view(*args, **kwargs)
    return gated
