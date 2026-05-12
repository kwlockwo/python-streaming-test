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
# Helpers
# ---------------------------------------------------------------------------

SSE_CONTENT_TYPE = "text/event-stream"

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def as_sse(generator):
    for chunk in generator:
        text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
        for line in text.splitlines(keepends=False):
            yield f"data: {line}\n"
        yield "\n"
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return jsonify({"status": "ok"})


STORY_FILE = os.path.join(os.path.dirname(__file__), "red_panda_story.txt")
CHUNK_SIZE = 4096


def file_generate():
    with open(STORY_FILE, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            yield chunk


@app.get("/stream/file")
@require_bearer
def stream_file():
    """Streams red_panda_story.txt in 4KB chunks."""
    return Response(
        stream_with_context(file_generate()),
        content_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/stream/mock")
@require_bearer
def stream_mock():
    """Always works, no API key needed. Good for proxy/infra testing."""
    return Response(
        stream_with_context(mock_generate()),
        content_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# SSE routes
# ---------------------------------------------------------------------------

@app.get("/stream/sse/mock")
@require_bearer
def stream_sse_mock():
    return Response(
        stream_with_context(as_sse(mock_generate())),
        content_type=SSE_CONTENT_TYPE,
        headers=SSE_HEADERS,
    )


@app.get("/stream/sse/file")
@require_bearer
def stream_sse_file():
    return Response(
        stream_with_context(as_sse(file_generate())),
        content_type=SSE_CONTENT_TYPE,
        headers=SSE_HEADERS,
    )


@app.post("/stream/sse/claude")
@require_bearer
def stream_sse_claude():
    body = request.get_json(silent=True) or {}
    prompt = body.get("prompt", "Say hello and introduce yourself in a few sentences.")
    system = body.get("system")

    try:
        return Response(
            stream_with_context(as_sse(claude_generate(prompt, system))),
            content_type=SSE_CONTENT_TYPE,
            headers=SSE_HEADERS,
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # threaded=True is required for concurrent streaming requests in dev
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
