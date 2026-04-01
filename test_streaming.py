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


def test_endpoint(label: str, url: str, payload: dict | None = None):
    headers = {}
    if API_SECRET:
        headers["Authorization"] = f"Bearer {API_SECRET}"
    else:
        print("  ⚠️  API_SECRET not set — requests will be rejected if auth is enabled")
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  {url}")
    print(f"{'='*60}")

    chunk_times: list[float] = []
    chunks: list[bytes] = []
    start = time.monotonic()
    first_byte_time: float | None = None
    response_headers: dict = {}

    try:
        with httpx.stream("POST", url, json=payload or {}, headers=headers, timeout=30.0) as r:
            response_headers = dict(r.headers)

            print("\nHEADERS:")
            for key in [
                "content-type", "transfer-encoding", "content-length",
                "cache-control", "cf-cache-status", "cf-ray", "server",
            ]:
                val = r.headers.get(key)
                if val:
                    print(f"  {key}: {val}")

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
                preview = chunk[:60].decode("utf-8", errors="replace")
                print(f"  #{len(chunks):>3}  +{elapsed:.3f}s  {len(chunk):>5}B  {preview!r}")

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
    has_content_length = "content-length" in response_headers
    via_cloudflare = "cf-ray" in response_headers
    cf_cache = response_headers.get("cf-cache-status", "")

    print(f"\nANALYSIS:")
    print(f"  TTFB:            {ttfb:.3f}s")
    print(f"  Total time:      {total_time:.3f}s")
    print(f"  TTFB ratio:      {ttfb/total_time:.0%} of total")
    print(f"  Chunks:          {len(chunks)}")
    print(f"  Bytes:           {total_bytes}")
    print(f"  Chunk spread:    {spread:.3f}s")
    print(f"  Transfer-Enc:    {'chunked ✓' if chunked else 'missing'}")
    print(f"  Content-Length:  {'present ⚠️' if has_content_length else 'absent ✓'}")
    print(f"  Via Cloudflare:  {'yes — CF-Ray: ' + response_headers['cf-ray'] if via_cloudflare else 'no'}")
    if cf_cache:
        cached = cf_cache.upper() == "HIT"
        print(f"  CF-Cache:        {cf_cache} {'🚫 CACHED' if cached else '✓'}")

    print(f"\nVERDICT: ", end="")
    if len(chunks) == 1 and total_bytes > 100:
        print("⚠️  Single chunk — almost certainly buffered somewhere.")
    elif ttfb / total_time > 0.85:
        print("⚠️  TTFB is >85% of total time — likely buffered (long wait, fast dump).")
    elif spread < 0.05 and len(chunks) > 1:
        print("⚠️  All chunks arrived within 50ms of each other — may be buffered.")
    else:
        print(f"✓  {len(chunks)} chunks over {spread:.2f}s — genuine streaming.")


if __name__ == "__main__":
    # 1. Mock endpoint (no API key needed)
    test_endpoint(
        "MOCK STREAM",
        f"{BASE_URL}/stream/mock",
    )

    # 2. Claude endpoint (needs ANTHROPIC_API_KEY on the server)
    test_endpoint(
        "CLAUDE STREAM (haiku)",
        f"{BASE_URL}/stream/claude",
        payload={"prompt": "Count slowly from 1 to 10, one number per line."},
    )
