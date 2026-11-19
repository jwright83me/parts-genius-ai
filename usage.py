"""Track Nova Responses usage and reserve budget before starting AI work."""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
from openai import APIStatusError

from membership import connect, price_id

logger = logging.getLogger(__name__)
CENT = 10_000_000  # Store costs in billionths of a US dollar.


class UsageLimit(Exception):
    pass


class UsageUnavailable(Exception):
    pass


@dataclass(frozen=True)
class BillingWindow:
    start: datetime
    end: datetime
    budget: int
    text_reserve: int


def billing_window(subscription):
    if subscription is None:
        return None
    if subscription.status == "trialing":
        start, end = getattr(subscription, "trial_start", None), getattr(subscription, "trial_end", None)
        budget, text_reserve = 35, 10
    else:
        invoice = getattr(subscription, "latest_invoice", None)
        if getattr(invoice, "status", None) != "paid":
            raise UsageUnavailable()
        item = next((i for i in subscription["items"].data if i.price.id == price_id()), None)
        if item is None:
            raise UsageUnavailable()
        start = getattr(item, "current_period_start", None) or getattr(subscription, "current_period_start", None)
        end = getattr(item, "current_period_end", None) or getattr(subscription, "current_period_end", None)
        budget, text_reserve = 400, 50
    if not isinstance(start, int) or not isinstance(end, int) or not start <= datetime.now(timezone.utc).timestamp() < end:
        raise UsageUnavailable()
    return BillingWindow(datetime.fromtimestamp(start, timezone.utc),
                         datetime.fromtimestamp(end, timezone.utc),
                         budget * CENT, text_reserve * CENT)


class UsageMeter:
    def __init__(self, user_id, feature, window):
        self.user_id = user_id
        self.feature = feature
        self.window = window

    def reserve(self, estimate):
        reservation = uuid4()
        window = self.window
        limit = window.budget - (window.text_reserve if self.feature in {"voice", "parts"} else 0)
        try:
            with connect() as db:
                db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (self.user_id,))
                spent, held, voice_active = db.execute(
                    """
                    SELECT
                        (SELECT coalesce(sum(CASE
                            WHEN cost_nano IS NOT NULL THEN cost_nano
                            WHEN model LIKE 'gpt-4.1-mini%%' THEN
                                input_tokens * 400 - least(cached_input_tokens, input_tokens) * 300
                                + output_tokens * 1600 + web_search_calls * 13200000
                            WHEN model LIKE 'gpt-4.1%%' THEN
                                input_tokens * 2000 - least(cached_input_tokens, input_tokens) * 1500
                                + output_tokens * 8000 + web_search_calls * 10000000
                            ELSE %s END), 0)
                         FROM nova_private.ai_usage
                         WHERE user_id = %s
                           AND coalesce(cycle_start, recorded_at) >= %s
                           AND coalesce(cycle_start, recorded_at) < %s),
                        (SELECT coalesce(sum(amount_nano), 0)
                         FROM nova_private.ai_reservations WHERE user_id = %s AND cycle_start = %s),
                        EXISTS (SELECT 1 FROM nova_private.ai_reservations
                                WHERE user_id = %s AND cycle_start = %s AND feature = 'voice'
                                  AND created_at > now() - interval '5 minutes')
                    """,
                    (limit, self.user_id, window.start, window.end, self.user_id, window.start,
                     self.user_id, window.start),
                ).fetchone()
                if self.feature == "voice" and voice_active:
                    raise UsageLimit("A voice call is already open or closing. You can still type a question.")
                if spent + held + estimate > limit:
                    message = f"Your AI allowance renews on {window.end:%b %d} (UTC)."
                    if self.feature == "voice":
                        message = "Voice has reached its allowance. You can still type a question if you have usage left. " + message
                    elif self.feature == "parts":
                        message = "Live parts research has reached its allowance. You can still ask a typed question if you have usage left. " + message
                    else:
                        message = "Your account has reached its AI allowance. " + message
                    raise UsageLimit(message)
                db.execute(
                    "INSERT INTO nova_private.ai_reservations (id, user_id, cycle_start, amount_nano, feature) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (reservation, self.user_id, window.start, estimate, self.feature),
                )
        except psycopg.Error:
            logger.exception("Nova AI allowance reservation failed")
            raise UsageUnavailable() from None
        return reservation

    def release(self, reservation):
        try:
            with connect() as db:
                db.execute("DELETE FROM nova_private.ai_reservations WHERE id = %s", (reservation,))
        except psycopg.Error:
            logger.warning("Nova AI usage reservation could not be released")

    def settle_voice(self, call_id, reservation, cost):
        try:
            with connect() as db:
                db.execute("""
                    INSERT INTO nova_private.ai_usage (
                        response_id, user_id, feature, model, input_tokens,
                        cached_input_tokens, output_tokens, web_search_calls, cycle_start, cost_nano
                    ) VALUES (%s, %s, 'voice', 'gpt-realtime', 0, 0, 0, 0, %s, %s)
                    ON CONFLICT (response_id) DO NOTHING
                    """, (call_id, self.user_id, self.window.start, cost))
                db.execute("DELETE FROM nova_private.ai_reservations WHERE id = %s", (reservation,))
        except psycopg.Error:
            logger.exception("Nova voice usage settlement failed")

    def settle(self, reservation, response):
        usage = response.usage
        if usage is None:
            logger.warning("Nova AI usage unavailable: feature=%s", self.feature)
            return

        search_calls = sum(item.type == "web_search_call" for item in response.output)
        try:
            with connect() as db:
                db.execute(
                    """
                    INSERT INTO nova_private.ai_usage (
                        response_id, user_id, feature, model, input_tokens,
                        cached_input_tokens, output_tokens, web_search_calls, cycle_start
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (response_id) DO NOTHING
                    """,
                    (
                        response.id, self.user_id, self.feature, response.model,
                        usage.input_tokens,
                        getattr(usage.input_tokens_details, "cached_tokens", 0) or 0,
                        usage.output_tokens, search_calls, self.window.start,
                    ),
                )
                db.execute("DELETE FROM nova_private.ai_reservations WHERE id = %s", (reservation,))
        except psycopg.Error as error:
            logger.warning("Nova AI usage settlement failed: feature=%s type=%s",
                           self.feature, type(error).__name__)


class MeteredResponses(UsageMeter):
    def __init__(self, responses, user_id, feature, window):
        super().__init__(user_id, feature, window)
        self.responses = responses

    def create(self, **kwargs):
        if not self.user_id:
            return self.responses.create(**kwargs)
        if self.window is None:
            raise UsageUnavailable()

        is_search = bool(kwargs.get("tools"))
        if is_search:
            kwargs["max_tool_calls"] = 3
        # Estimate a reservation before the provider reports actual token and
        # search usage. Charge for large prompts as well as the base estimate.
        prompt_bytes = len(json.dumps([kwargs.get("input"), kwargs.get("instructions")]).encode())
        input_rate = {"gpt-4.1": 2000, "gpt-4.1-mini": 400}[kwargs["model"]]
        estimate = (10 if is_search else 2) * CENT + prompt_bytes * input_rate
        reservation = self.reserve(estimate)
        try:
            response = self.responses.create(**kwargs)
        except APIStatusError as error:
            if error.status_code in {400, 401, 403, 404, 422, 429}:
                self.release(reservation)
            raise
        self.settle(reservation, response)
        return response
