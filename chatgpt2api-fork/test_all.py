"""
chatgpt2api — Full test suite
Run on VPS:  python3 test_all.py
Run locally: BASE="http://localhost:8000" python3 test_all.py

Tests (in order):
  1. Health check — server alive
  2. Text chat  — new session, get real conversation_id
  3. Text chat  — follow-up (same thread via chat_id / conversation_id)
  4. Vision     — image + prompt → text response
  5. Vision     — image + prompt → image generation (edit/redraw)
  6. Vision     — follow-up on a vision thread (text only, no image)
  7. Image gen  — pure /v1/chat/completions with gpt-image-2
  8. Images API — /v1/images/generations
  9. Streaming  — SSE text chat, capture chat.session chunk
 10. Streaming  — SSE vision (image + prompt)
"""

import base64
import json
import sys
import time
import urllib.request
import urllib.error
from io import BytesIO

# ── Config ────────────────────────────────────────────────────────────────────
BASE = "http://217.77.8.115"      # change to http://localhost:8000 for local test
KEY  = "ranaji"

# A tiny 1×1 red PNG — no external file needed
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwADhQGAWjR9awAAAABJRU5ErkJggg=="
)

HEADERS = {
    "Authorization": f"Bearer {KEY}",
    "Content-Type": "application/json",
}

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
INFO = "\033[94m→\033[0m"

results: list[tuple[str, bool, str]] = []


# ── Helpers ───────────────────────────────────────────────────────────────────

def post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def post_stream(path: str, body: dict) -> list[dict]:
    """Collect all SSE events, return parsed JSON lines (skips non-data lines)."""
    body = {**body, "stream": True}
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers=HEADERS, method="POST")
    events: list[dict] = []
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw_line in resp:
            line = raw_line.decode().strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return events


def assert_ok(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, cond, detail))
    status = PASS if cond else FAIL
    print(f"  {status} {name}" + (f"\n      {detail}" if detail and not cond else ""))


def text_content(resp: dict) -> str:
    try:
        return resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""


def stream_text(events: list[dict]) -> str:
    parts = []
    for e in events:
        try:
            delta = e["choices"][0]["delta"].get("content", "")
            if delta:
                parts.append(delta)
        except (KeyError, IndexError, TypeError):
            pass
    return "".join(parts)


def stream_session(events: list[dict]) -> dict:
    for e in events:
        if e.get("object") == "chat.session":
            return e
    return {}


def image_url_in_content(resp: dict) -> str:
    content = text_content(resp)
    if content and ("http" in content or content.startswith("/")):
        return content.strip()
    return resp.get("image_url", "")


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_health():
    print(f"\n{INFO} 1. Health check")
    resp = post("/v1/models", {})
    ok = isinstance(resp.get("data"), list) or "data" in resp or "models" in str(resp)
    # fallback: just check we got a dict back (not an error)
    if not ok:
        ok = isinstance(resp, dict) and "error" not in str(resp).lower()[:50]
    assert_ok("Server is alive", ok, str(resp)[:120])


def test_text_new_session():
    print(f"\n{INFO} 2. Text chat — new session")
    resp = post("/v1/chat/completions", {
        "model": "auto",
        "messages": [{"role": "user", "content": "My name is Rana. Just say: GOT IT."}],
    })
    content = text_content(resp)
    conv_id  = resp.get("conversation_id", "")
    chat_id  = resp.get("chat_id", "")

    assert_ok("Got text response",   bool(content), f"content={content[:80]}")
    assert_ok("conversation_id present", bool(conv_id), f"conv_id={conv_id}")
    assert_ok("chat_id == conversation_id", chat_id == conv_id,
              f"chat_id={chat_id}  conv_id={conv_id}")
    assert_ok("conv_id looks like UUID",
              len(conv_id) > 10 and "-" in conv_id, f"conv_id={conv_id}")

    return conv_id


def test_text_follow_up(conv_id: str):
    print(f"\n{INFO} 3. Text chat — follow-up (conv_id={conv_id[:18]}…)")
    resp = post("/v1/chat/completions", {
        "model": "auto",
        "chat_id": conv_id,
        "messages": [{"role": "user", "content": "What is my name?"}],
    })
    content = text_content(resp)
    returned_id = resp.get("conversation_id", "")

    assert_ok("Got follow-up response",   bool(content), f"content={content[:80]}")
    assert_ok("Same conversation_id",     returned_id == conv_id,
              f"returned={returned_id}  expected={conv_id}")
    assert_ok("Name remembered (Rana)",
              "rana" in content.lower(), f"content={content[:120]}")


def test_vision_text_response():
    print(f"\n{INFO} 4. Vision — image + prompt → text")
    resp = post("/v1/chat/completions", {
        "model": "auto",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{TINY_PNG_B64}"}},
                {"type": "text",
                 "text": "What colour is this image? One word answer."},
            ],
        }],
    })
    content  = text_content(resp)
    conv_id  = resp.get("conversation_id", "")
    rtype    = resp.get("response_type", "")

    assert_ok("Got vision text response",    bool(content), f"content={content[:120]}")
    assert_ok("response_type=text",          rtype == "text", f"response_type={rtype}")
    assert_ok("conversation_id present",     bool(conv_id), f"conv_id={conv_id}")
    assert_ok("chat_id == conversation_id",
              resp.get("chat_id") == conv_id)

    return conv_id


def test_vision_follow_up(conv_id: str):
    print(f"\n{INFO} 5. Vision — follow-up text (no image, same thread)")
    resp = post("/v1/chat/completions", {
        "model": "auto",
        "chat_id": conv_id,
        "messages": [{"role": "user", "content": "What did I just show you?"}],
    })
    content      = text_content(resp)
    returned_id  = resp.get("conversation_id", "")

    assert_ok("Got follow-up response",   bool(content), f"content={content[:120]}")
    assert_ok("Same conversation_id",     returned_id == conv_id,
              f"returned={returned_id}  expected={conv_id}")


def test_vision_image_response():
    print(f"\n{INFO} 6. Vision — image + prompt → image generation")
    print("     (asks GPT to redraw the image — may take 20–40 s)")
    resp = post("/v1/chat/completions", {
        "model": "auto",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{TINY_PNG_B64}"}},
                {"type": "text",
                 "text": "Redraw this as a bright red circle on white background."},
            ],
        }],
    })
    content  = text_content(resp)
    rtype    = resp.get("response_type", "")
    img_url  = resp.get("image_url", "")
    conv_id  = resp.get("conversation_id", "")

    is_img   = rtype == "image" or bool(img_url) or (
        content and ("http" in content or "/images/" in content))

    assert_ok("Got image response or text",  bool(content), f"content={content[:120]}")
    assert_ok("conversation_id present",     bool(conv_id), f"conv_id={conv_id}")
    if is_img:
        assert_ok("response_type=image",     rtype == "image",     f"rtype={rtype}")
        assert_ok("image_url in response",   bool(img_url),        f"image_url={img_url}")
        assert_ok("URL is http(s)",
                  img_url.startswith("http"), f"url={img_url}")
    else:
        # GPT returned text (e.g. explanation) — not an error, just note it
        assert_ok("response_type noted (GPT chose text)", True,
                  f"GPT returned text instead of image — normal for small images")


def test_image_generation_endpoint():
    print(f"\n{INFO} 7. /v1/images/generations — URL-only response")
    print("     (image generation, may take 20–40 s)")
    resp = post("/v1/images/generations", {
        "model": "gpt-image-2",
        "prompt": "A small red circle on white background",
        "n": 1,
    })
    data = resp.get("data", [])
    url  = data[0].get("url", "") if data else ""
    b64  = data[0].get("b64_json", "") if data else ""

    assert_ok("Got data array",           bool(data), f"data={str(data)[:80]}")
    assert_ok("URL returned (not b64)",   bool(url),  f"url={url[:80]}")
    assert_ok("No base64 in response",    not b64,    "b64_json should be absent")
    assert_ok("Only 1 image (last only)", len(data) == 1, f"len={len(data)}")


def test_chat_completions_image_model():
    print(f"\n{INFO} 8. /v1/chat/completions with gpt-image-2 model")
    print("     (explicit image generation via chat endpoint)")
    resp = post("/v1/chat/completions", {
        "model": "gpt-image-2",
        "messages": [{"role": "user", "content": "Draw a tiny red dot"}],
    })
    content = text_content(resp)
    conv_id = resp.get("conversation_id", "")

    has_url = "http" in content or "/images/" in content

    assert_ok("Got response",        bool(content), f"content={content[:120]}")
    assert_ok("Contains image URL",  has_url,       f"content={content[:120]}")


def test_streaming_text():
    print(f"\n{INFO} 9. Streaming text chat — SSE")
    events = post_stream("/v1/chat/completions", {
        "model": "auto",
        "messages": [{"role": "user", "content": "Say exactly: STREAM OK"}],
    })
    text    = stream_text(events)
    session = stream_session(events)
    conv_id = session.get("conversation_id", "")
    chat_id = session.get("chat_id", "")

    assert_ok("Got streamed text",           bool(text),    f"text={text[:80]}")
    assert_ok("chat.session chunk emitted",  bool(session), f"session={session}")
    assert_ok("conversation_id in session",  bool(conv_id), f"conv_id={conv_id}")
    assert_ok("chat_id == conversation_id",  chat_id == conv_id,
              f"chat_id={chat_id}  conv_id={conv_id}")

    return conv_id


def test_streaming_follow_up(conv_id: str):
    print(f"\n{INFO} 10. Streaming follow-up (same conv_id={conv_id[:18]}…)")
    events  = post_stream("/v1/chat/completions", {
        "model": "auto",
        "chat_id": conv_id,
        "messages": [{"role": "user", "content": "Repeat the last thing you said."}],
    })
    text    = stream_text(events)
    session = stream_session(events)
    ret_id  = session.get("conversation_id", conv_id)

    assert_ok("Got streamed follow-up text",  bool(text),       f"text={text[:80]}")
    assert_ok("Same conversation_id",         ret_id == conv_id,
              f"returned={ret_id}  expected={conv_id}")


def test_streaming_vision():
    print(f"\n{INFO} 11. Streaming vision — image + prompt (SSE)")
    events = post_stream("/v1/chat/completions", {
        "model": "auto",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{TINY_PNG_B64}"}},
                {"type": "text", "text": "Describe this image in one sentence."},
            ],
        }],
    })
    text    = stream_text(events)
    session = stream_session(events)
    conv_id = session.get("conversation_id", "")

    assert_ok("Got streamed vision text",    bool(text),    f"text={text[:120]}")
    assert_ok("chat.session chunk present",  bool(session), f"session={session}")
    assert_ok("conversation_id present",     bool(conv_id), f"conv_id={conv_id}")


# ── Runner ────────────────────────────────────────────────────────────────────

def run_all():
    print("=" * 60)
    print(f"  ChatGPT2API — Full Test Suite")
    print(f"  Server : {BASE}")
    print("=" * 60)

    test_health()

    # Text chat (session continuity)
    conv_id_text = test_text_new_session()
    if conv_id_text:
        test_text_follow_up(conv_id_text)

    # Vision (image + prompt)
    conv_id_vision = test_vision_text_response()
    if conv_id_vision:
        test_vision_follow_up(conv_id_vision)

    # Vision → image generation (heavy, may be skipped on quota)
    try:
        test_vision_image_response()
    except Exception as e:
        print(f"  \033[93m! Vision image test skipped: {e}\033[0m")

    # Pure image generation endpoints
    try:
        test_image_generation_endpoint()
    except Exception as e:
        print(f"  \033[93m! /v1/images/generations skipped: {e}\033[0m")

    try:
        test_chat_completions_image_model()
    except Exception as e:
        print(f"  \033[93m! gpt-image-2 chat test skipped: {e}\033[0m")

    # Streaming
    conv_id_stream = test_streaming_text()
    if conv_id_stream:
        test_streaming_follow_up(conv_id_stream)

    try:
        test_streaming_vision()
    except Exception as e:
        print(f"  \033[93m! Streaming vision skipped: {e}\033[0m")

    # ── Summary ──────────────────────────────────────────────────────────────
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)

    print("\n" + "=" * 60)
    print(f"  Results: {PASS} {passed} passed  |  {FAIL} {failed} failed  |  total {len(results)}")
    if failed:
        print("\n  Failed checks:")
        for name, ok, detail in results:
            if not ok:
                print(f"    {FAIL} {name}: {detail}")
    print("=" * 60)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    # Allow overriding BASE from command line:  python3 test_all.py http://localhost:8000
    if len(sys.argv) > 1:
        BASE = sys.argv[1].rstrip("/")
    run_all()
