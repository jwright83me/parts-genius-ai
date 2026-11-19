"""Meter OpenAI Realtime WebRTC calls from their server-side event stream."""
import json
import logging
from threading import Thread
from time import monotonic

import httpx
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK, WebSocketException
from websockets.sync.client import connect as connect_events

logger = logging.getLogger(__name__)
REALTIME_URL = "https://api.openai.com/v1/realtime/calls"


def response_cost(usage):
    """Price a Realtime response in billionths of a USD."""
    inputs = usage.get("input_token_details") or {}
    outputs = usage.get("output_token_details") or {}
    cached = inputs.get("cached_tokens_details") or {}
    cost = 0
    known_input = 0
    for modality, rate, cached_rate in (("text", 4000, 400), ("audio", 32000, 400), ("image", 5000, 500)):
        tokens = inputs.get(f"{modality}_tokens") or 0
        cached_tokens = min(cached.get(f"{modality}_tokens") or 0, tokens)
        known_input += tokens
        cost += (tokens - cached_tokens) * rate + cached_tokens * cached_rate
    cost += max(0, usage["input_tokens"] - known_input) * 32000
    text_output = outputs.get("text_tokens") or 0
    cost += text_output * 16000 + max(0, usage["output_tokens"] - text_output) * 64000
    return cost


def hang_up(call_id, headers):
    for _ in range(2):
        try:
            response = httpx.post(f"{REALTIME_URL}/{call_id}/hangup", headers=headers, timeout=5)
            if response.is_success or response.status_code == 404:
                return True
        except httpx.RequestError:
            continue
    logger.warning("Nova could not confirm voice hangup")
    return False


def start_meter(call_id, headers, meter, reservation, budget, seconds):
    try:
        events = connect_events(
            f"wss://api.openai.com/v1/realtime?call_id={call_id}",
            additional_headers=headers, open_timeout=8, close_timeout=2,
        )
    except (OSError, TimeoutError, WebSocketException) as error:
        logger.warning("Nova voice meter could not connect: type=%s", type(error).__name__)
        if hang_up(call_id, headers):
            meter.release(reservation)
        return False
    try:
        Thread(target=monitor, args=(events, call_id, headers, meter, reservation, budget, seconds),
               name="nova-voice-meter", daemon=True).start()
    except RuntimeError:
        events.close()
        if hang_up(call_id, headers):
            meter.release(reservation)
        return False
    return True


def monitor(events, call_id, headers, meter, reservation, budget, seconds):
    deadline = monotonic() + seconds
    cost = 0
    seen = set()
    uncertain = False
    in_progress = set()
    try:
        while cost < budget and (remaining := deadline - monotonic()) > 0:
            event = json.loads(events.recv(timeout=remaining))
            event_type = event.get("type")
            if event_type not in {"response.created", "response.done"}:
                continue
            response = event["response"]
            response_id = response["id"]
            if event_type == "response.created":
                in_progress.add(response_id)
                continue
            in_progress.discard(response_id)
            if response_id in seen:
                continue
            seen.add(response_id)
            usage = response.get("usage")
            if usage:
                cost += response_cost(usage)
            elif response.get("status") != "failed":
                uncertain = True
    except (TimeoutError, ConnectionClosedOK):
        pass
    except (ConnectionClosed, OSError, ValueError, KeyError, TypeError):
        uncertain = True
        logger.warning("Nova voice metering ended without a complete event stream")
    finally:
        ended = hang_up(call_id, headers)
        events.close()
        if ended:
            if uncertain or in_progress:
                cost = max(cost, budget)
            meter.settle_voice(call_id, reservation, cost)
