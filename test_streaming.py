"""
Run this against the Flask backend to verify streaming is working end-to-end.

    pip install httpx
    python test_streaming.py [base_url]

Default base_url: http://localhost:5000
"""

import sys
import time
import os
import httpx

BASE_URL = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:5000"
API_SECRET = os.environ.get("API_SECRET", "")

DISPLAY_HEADERS = [
    "content-type", "transfer-encoding", "content-length",
    "cache-control", "x-accel-buffering", "cf-cache-status", "cf-ray", "server",
]

LONG_PROMPT = "Write a detailed description of the four seasons, spending at least two paragraphs on each one."


def auth_headers() -> dict:
    if not API_SECRET:
        print("  ⚠️  API_SECRET not set — requests will be rejected if auth is enabled")
        return {}
    return {"Authorization": f"Bearer {API_SECRET}"}


def print_header(label: str, url: str, suffix: str = "") -> None:
    print(f"\n{'='*60}")
    print(f"  {label}{suffix}")
    print(f"  {url}")
    print(f"{'='*60}")


def print_response_headers(r: httpx.Response) -> None:
    print("\nHEADERS:")
    for key in DISPLAY_HEADERS:
        val = r.headers.get(key)
        if val:
            print(f"  {key}: {val}")


def streaming_verdict(n: int, total_bytes: int, ttfb: float, total_time: float, spread: float) -> str:
    if n == 1 and total_bytes > 100:
        return "⚠️  Single chunk — almost certainly buffered somewhere."
    if ttfb / total_time > 0.85:
        return "⚠️  TTFB is >85% of total time — likely buffered (long wait, fast dump)."
    if spread < 0.05 and n > 1:
        return "⚠️  All chunks arrived within 50ms of each other — may be buffered."
    return f"✓  {n} chunks over {spread:.2f}s — genuine streaming."


def sse_verdict(n: int, total_time: float, ttfb: float, spread: float) -> str:
    if n <= 2 and total_time > 1:
        return "⚠️  Very few events — likely buffered."
    if ttfb / total_time > 0.85:
        return "⚠️  TTFB is >85% of total time — likely buffered (long wait, fast dump)."
    if spread < 0.05 and n > 5:
        return "⚠️  All events arrived within 50ms — may be buffered."
    return f"✓  {n} events over {spread:.2f}s — genuine streaming."


def print_cf(response_headers: dict) -> None:
    via_cloudflare = "cf-ray" in response_headers
    cf_cache = response_headers.get("cf-cache-status", "")
    print(f"  Via Cloudflare:  {'yes — CF-Ray: ' + response_headers['cf-ray'] if via_cloudflare else 'no'}")
    if cf_cache:
        cached = cf_cache.upper() == "HIT"
        print(f"  CF-Cache:        {cf_cache} {'🚫 CACHED' if cached else '✓'}")


def collect_chunks(r: httpx.Response, start: float) -> tuple[list[bytes], list[float], float | None]:
    chunks: list[bytes] = []
    chunk_times: list[float] = []
    first_byte_time: float | None = None

    print("\nCHUNKS:")
    for chunk in r.iter_bytes():
        if not chunk:
            continue
        now = time.monotonic()
        if first_byte_time is None:
            first_byte_time = now
        chunk_times.append(now - start)
        chunks.append(chunk)
        elapsed = chunk_times[-1]
        preview = chunk.decode("utf-8", errors="replace")
        print(f"  #{len(chunks):>3}  +{elapsed:.3f}s  {len(chunk):>5}B  {preview!r}")

    return chunks, chunk_times, first_byte_time


def parse_sse_events(r: httpx.Response, start: float) -> tuple[list[str], list[float], float | None]:
    events: list[str] = []
    chunk_times: list[float] = []
    first_byte_time: float | None = None
    buffer = ""

    print("\nEVENTS:")
    for chunk in r.iter_text():
        if not chunk:
            continue
        now = time.monotonic()
        if first_byte_time is None:
            first_byte_time = now
        chunk_times.append(now - start)
        buffer += chunk
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            data_lines = [l[6:] for l in event.splitlines() if l.startswith("data: ")]
            if data_lines:
                events.append("\n".join(data_lines))
                elapsed = chunk_times[-1]
                preview = events[-1][:80].replace("\n", "\\n")
                print(f"  #{len(events):>3}  +{elapsed:.3f}s  {preview!r}")

    return events, chunk_times, first_byte_time


def test_endpoint(label: str, url: str, payload: dict | None = None) -> None:
    print_header(label, url)
    start = time.monotonic()
    response_headers: dict = {}

    try:
        with httpx.stream("POST", url, json=payload or {}, headers=auth_headers(), timeout=30.0) as r:
            response_headers = dict(r.headers)
            print_response_headers(r)
            chunks, chunk_times, first_byte_time = collect_chunks(r, start)
    except httpx.RequestError as e:
        print(f"\n  ERROR: {e}")
        return

    if not chunks:
        print("\n  ERROR: No chunks received")
        return

    total_time = time.monotonic() - start
    ttfb = first_byte_time - start if first_byte_time else total_time
    total_bytes = sum(len(c) for c in chunks)
    spread = chunk_times[-1] - chunk_times[0] if len(chunk_times) > 1 else 0
    chunked = response_headers.get("transfer-encoding", "").lower() == "chunked"

    print(f"\nANALYSIS:")
    print(f"  TTFB:            {ttfb:.3f}s")
    print(f"  Total time:      {total_time:.3f}s")
    print(f"  TTFB ratio:      {ttfb/total_time:.0%} of total")
    print(f"  Chunks:          {len(chunks)}")
    print(f"  Bytes:           {total_bytes}")
    print(f"  Chunk spread:    {spread:.3f}s")
    print(f"  Transfer-Enc:    {'chunked ✓' if chunked else 'missing'}")
    print(f"  Content-Length:  {'present ⚠️' if 'content-length' in response_headers else 'absent ✓'}")
    print_cf(response_headers)
    print(f"\nVERDICT: {streaming_verdict(len(chunks), total_bytes, ttfb, total_time, spread)}")


def test_sse_endpoint(label: str, url: str, method: str = "GET", payload: dict | None = None) -> None:
    print_header(label, url, suffix=" [SSE]")
    start = time.monotonic()
    response_headers: dict = {}

    try:
        body = payload if method == "POST" else None
        with httpx.stream(method, url, json=body, headers=auth_headers(), timeout=60.0) as r:
            response_headers = dict(r.headers)
            print_response_headers(r)
            events, chunk_times, first_byte_time = parse_sse_events(r, start)
    except httpx.RequestError as e:
        print(f"\n  ERROR: {e}")
        return

    if not events:
        print("\n  ERROR: No events received")
        return

    total_time = time.monotonic() - start
    ttfb = first_byte_time - start if first_byte_time else total_time
    spread = chunk_times[-1] - chunk_times[0] if len(chunk_times) > 1 else 0
    correct_content_type = "text/event-stream" in response_headers.get("content-type", "")
    content_type_display = "text/event-stream ✓" if correct_content_type else response_headers.get("content-type", "missing") + " ⚠️"

    print(f"\nANALYSIS:")
    print(f"  TTFB:            {ttfb:.3f}s")
    print(f"  Total time:      {total_time:.3f}s")
    print(f"  TTFB ratio:      {ttfb/total_time:.0%} of total")
    print(f"  Events:          {len(events)}")
    print(f"  Event spread:    {spread:.3f}s")
    print(f"  Content-Type:    {content_type_display}")
    print(f"  [DONE] event:    {'✓' if events[-1] == '[DONE]' else '⚠️  missing'}")
    print_cf(response_headers)
    print(f"\nVERDICT: {sse_verdict(len(events), total_time, ttfb, spread)}")


if __name__ == "__main__":
    test_endpoint("MOCK STREAM", f"{BASE_URL}/stream/mock")
    test_endpoint("FILE STREAM (red_panda_story.txt)", f"{BASE_URL}/stream/file")
    test_endpoint("CLAUDE STREAM (haiku)", f"{BASE_URL}/stream/claude", payload={"prompt": LONG_PROMPT})
    test_sse_endpoint("SSE MOCK STREAM", f"{BASE_URL}/stream/sse/mock")
    test_sse_endpoint("SSE FILE STREAM (red_panda_story.txt)", f"{BASE_URL}/stream/sse/file")
    test_sse_endpoint("SSE CLAUDE STREAM (haiku)", f"{BASE_URL}/stream/sse/claude", method="POST", payload={"prompt": LONG_PROMPT})
