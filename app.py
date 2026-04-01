import os
import time
from functools import wraps

import anthropic
from flask import Flask, Response, jsonify, request, stream_with_context

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def require_bearer(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        secret = os.environ.get("API_SECRET")
        if not secret:
            return jsonify({"error": "API_SECRET is not configured on the server"}), 500

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != secret:
            return jsonify({"error": "Unauthorized"}), 401

        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# Mock streaming
# ---------------------------------------------------------------------------

MOCK_TEXT = (
    "This is a mock streaming response. "
    "Each word is yielded as a separate chunk with a short delay "
    "so you can verify that your proxy stack is passing chunks through "
    "incrementally rather than buffering the full response."
)


def mock_generate():
    for word in MOCK_TEXT.split():
        yield word + " "
        time.sleep(0.08)


# ---------------------------------------------------------------------------
# Claude streaming (cheapest model: haiku)
# ---------------------------------------------------------------------------

def get_anthropic_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")
    return anthropic.Anthropic(api_key=api_key)


def claude_generate(prompt: str, system: str | None = None):
    client = get_anthropic_client()

    kwargs = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    with client.messages.stream(**kwargs) as stream:
        for text in stream.text_stream:
            yield text


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/stream/mock")
@require_bearer
def stream_mock():
    """Always works, no API key needed. Good for proxy/infra testing."""
    return Response(
        stream_with_context(mock_generate()),
        content_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/stream/claude")
@require_bearer
def stream_claude():
    """Real Claude output via Haiku. Requires ANTHROPIC_API_KEY."""
    body = request.get_json(silent=True) or {}
    prompt = body.get("prompt", "Say hello and introduce yourself in a few sentences.")
    system = body.get("system")

    try:
        generator = claude_generate(prompt, system)
        return Response(
            stream_with_context(generator),
            content_type="text/plain; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # threaded=True is required for concurrent streaming requests in dev
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
