
import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI

app = Flask(__name__)

CORS(app, resources={
    r"/api/*": {"origins": [
        "https://jwright83me.github.io"
    ]}
})

SYSTEM_PROMPT = """
You are Parts Genius AI, a knowledgeable automotive
diagnostic and parts assistant.

Help customers troubleshoot vehicle problems, interpret
OBD-II codes, and identify possible replacement parts.

Ask for year, make, model, engine, and symptoms when needed.
Explain likely causes, diagnostic tests, and repair options.

Never invent OEM numbers, retailer inventory, prices,
or confirmed vehicle fitment.

Prioritize safety. Warn users about dangerous repairs,
high-voltage hybrid systems, and unsafe driving conditions.
"""

from flask import send_file

@app.get("/")
def home():
    return send_file("index.html")
@app.get("/IMG_8594.png")
def nova_image():
    return send_file("IMG_8594.png")

@app.post("/api/diagnose")
def diagnose():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()
    history = data.get("history", [])
    vehicle = str(data.get("vehicle") or "")[:300]
    if not isinstance(history, list):
        history = []
    if not message:
        return jsonify({"error": "Enter a vehicle question."}), 400

    if len(message) > 4000:
        return jsonify({"error": "Message is too long."}), 400

    if not os.getenv("OPENAI_API_KEY"):
        return jsonify({"error": "AI is not configured."}), 503

    try:
        client = OpenAI()
        response = client.responses.create(
            model="gpt-4.1-mini",
            
            instructions=SYSTEM_PROMPT + ("\nCustomer's saved vehicle: " + vehicle if vehicle else ""),

                        input=[
                *history[-20:],
                {"role": "user", "content": message}
            ],
            max_output_tokens=700
        )
        return jsonify({"reply": response.output_text})

    except Exception:
        app.logger.exception("AI request failed")
        return jsonify({
            "error": "AI is temporarily unavailable."
        }), 502

@app.get("/api/voice/check")
def check_voice_setup():
    try:
        from openai import OpenAI
        client = OpenAI(api_key="test-key")
        supported = hasattr(client.realtime, "client_secrets")
        return jsonify({
            "voice_library_ready": supported,
            "voice_enabled": False
        })
    except Exception:
        return jsonify({
            "voice_library_ready": False,
            "voice_enabled": False
        })
@app.post("/api/voice/security-test")
def test_voice_security():
    import hmac
    from flask import request

    expected = os.getenv("VOICE_TEST_TOKEN")
    provided = request.headers.get("X-Voice-Test-Password", "")

    if not expected or not hmac.compare_digest(provided, expected):
        return jsonify({"error": "Unauthorized"}), 401

    return jsonify({"security": "passed"})
@app.post("/api/voice/session")
def create_voice_session():
    import hmac
    from flask import request

    expected = os.getenv("VOICE_TEST_TOKEN")
    provided = request.headers.get("X-Voice-Test-Password", "")

    if not expected or not hmac.compare_digest(provided, expected):
        return jsonify({"error": "Unauthorized"}), 401
    if os.getenv("VOICE_ENABLED") != "true":
        return jsonify({"error": "Voice is not enabled yet"}), 403
    if not os.getenv("OPENAI_API_KEY"):
        return jsonify({"error": "Voice AI is not configured"}), 503

    try:
            client = OpenAI()
            session = client.realtime.client_secrets.create(
            session={
                "type": "realtime",
                "model": "gpt-realtime",
                "instructions": SYSTEM_PROMPT,
                "audio": {
                    "output": {"voice": "marin"}
                }
            }
        )
            return jsonify({"client_secret": session.value})
    except Exception:
            app.logger.exception("Voice session error")
            return jsonify({"error": "Unable to start voice session"}), 502

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
