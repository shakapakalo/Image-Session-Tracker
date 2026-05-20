from __future__ import annotations

import time
import uuid
from typing import Any, Iterable, Iterator

from fastapi import HTTPException

from services.protocol.conversation import (
    ConversationRequest,
    ImageOutput,
    collect_image_outputs,
    count_message_tokens,
    count_text_tokens,
    encode_images,
    normalize_messages,
    stream_image_outputs_with_pool,
    stream_text_deltas,
    stream_vision_outputs_with_pool,
    text_backend,
)
from utils.helper import (
    build_chat_image_markdown_content,
    extract_chat_image,
    extract_chat_prompt,
    has_vision_content,
    is_image_chat_request,
    parse_image_count,
)


# ── OpenAI-format helpers ────────────────────────────────────────────────────

def completion_chunk(
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
    completion_id: str = "",
    created: int | None = None,
) -> dict[str, Any]:
    return {
        "id": completion_id or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def completion_response(
    model: str,
    content: str,
    created: int | None = None,
    messages: list[dict[str, Any]] | None = None,
    chat_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Build a non-streaming chat.completion response.

    chat_id == conversation_id — both are the real ChatGPT conversation UUID.
    Only one ID to remember; pass it back as chat_id to continue the thread.
    """
    prompt_tokens = count_message_tokens(messages, model) if messages else 0
    completion_tokens = count_text_tokens(content, model) if messages else 0
    # Normalise: always the same UUID for both fields
    real_id = conversation_id or chat_id or None
    resp: dict[str, Any] = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    if real_id:
        resp["chat_id"] = real_id           # convenience alias for clients
        resp["conversation_id"] = real_id   # real ChatGPT conversation UUID
    return resp


# ── Text chat helpers ────────────────────────────────────────────────────────

def collect_chat_content(chunks: Iterable[dict[str, Any]]) -> str:
    """Collect text from streamed chat completion chunks (used by Anthropic adapter)."""
    parts: list[str] = []
    for chunk in chunks:
        choices = chunk.get("choices")
        first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        delta = first.get("delta") if isinstance(first.get("delta"), dict) else {}
        content = str(delta.get("content") or "")
        if content:
            parts.append(content)
    return "".join(parts)


def stream_text_chat_completion(
    backend,
    messages: list[dict[str, Any]],
    model: str,
) -> Iterator[dict[str, Any]]:
    """Backward-compatible wrapper used by the Anthropic adapter (no session)."""
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    sent_role = False
    request = ConversationRequest(model=model, messages=messages)
    for delta_text in stream_text_deltas(backend, request):
        if not sent_role:
            sent_role = True
            yield completion_chunk(model, {"role": "assistant", "content": delta_text}, None, completion_id, created)
        else:
            yield completion_chunk(model, {"content": delta_text}, None, completion_id, created)
    if not sent_role:
        yield completion_chunk(model, {"role": "assistant", "content": ""}, None, completion_id, created)
    yield completion_chunk(model, {}, "stop", completion_id, created)


def chat_messages_from_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages = body.get("messages")
    if isinstance(messages, list) and messages:
        return [m for m in messages if isinstance(m, dict)]
    prompt = str(body.get("prompt") or "").strip()
    if prompt:
        return [{"role": "user", "content": prompt}]
    raise HTTPException(status_code=400, detail={"error": "messages or prompt is required"})


def text_chat_parts(body: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    model = str(body.get("model") or "auto").strip() or "auto"
    messages = normalize_messages(chat_messages_from_body(body))
    return model, messages


def _continuation_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return system messages + last user turn only.

    ChatGPT already holds the conversation history server-side (because we use
    history_and_training_disabled=False).  Sending only the new message avoids
    duplicating earlier turns.
    """
    sys_msgs = [m for m in messages if m.get("role") == "system"]
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            return sys_msgs + [messages[i]]
    return messages


def _stream_text_with_session(
    messages: list[dict[str, Any]],
    model: str,
    prior_conversation_id: str,
) -> Iterator[dict[str, Any]]:
    """Stream chat completion chunks.

    prior_conversation_id — the real ChatGPT UUID from the previous turn
    (empty string for brand-new sessions).  After the stream completes we emit
    a chat.session object so streaming clients can capture the conversation_id.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    sent_role = False
    is_new_session = not prior_conversation_id

    send_messages = messages if is_new_session else _continuation_messages(messages)

    conv_id_out: list[str] = []
    request = ConversationRequest(
        model=model,
        messages=send_messages,
        conversation_id=prior_conversation_id,
        history_and_training_disabled=False,
    )
    for delta_text in stream_text_deltas(text_backend(), request, conv_id_out):
        if not sent_role:
            sent_role = True
            yield completion_chunk(model, {"role": "assistant", "content": delta_text}, None, completion_id, created)
        else:
            yield completion_chunk(model, {"content": delta_text}, None, completion_id, created)

    if not sent_role:
        yield completion_chunk(model, {"role": "assistant", "content": ""}, None, completion_id, created)
    yield completion_chunk(model, {}, "stop", completion_id, created)

    # The real ChatGPT conversation UUID (same for chat_id and conversation_id)
    real_id = conv_id_out[0] if conv_id_out else prior_conversation_id
    if real_id:
        yield {
            "object": "chat.session",
            "chat_id": real_id,
            "conversation_id": real_id,
            "is_new_session": is_new_session,
        }


# ── Image chat helpers ───────────────────────────────────────────────────────

def chat_image_args(body: dict[str, Any]) -> tuple[str, str, int, list[tuple[bytes, str, str]]]:
    model = str(body.get("model") or "gpt-image-2").strip() or "gpt-image-2"
    prompt = extract_chat_prompt(body)
    if not prompt:
        raise HTTPException(status_code=400, detail={"error": "prompt is required"})
    images = [
        (data, f"image_{idx}.png", mime)
        for idx, (data, mime) in enumerate(extract_chat_image(body), start=1)
    ]
    return model, prompt, parse_image_count(body.get("n")), images


def image_result_content(result: dict[str, Any]) -> str:
    data = result.get("data")
    if isinstance(data, list) and data:
        return build_chat_image_markdown_content(result)
    return str(result.get("message") or "Image generation completed.")


def image_chat_response(body: dict[str, Any]) -> dict[str, Any]:
    model, prompt, n, images = chat_image_args(body)
    result = collect_image_outputs(stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        n=n,
        response_format="url",
        images=encode_images(images) or None,
    )))
    data = result.get("data")
    if isinstance(data, list) and data:
        result = dict(result)
        result["data"] = [data[-1]]
    upstream_id = result.pop("_upstream_conversation_id", None) or None
    resp = completion_response(model, image_result_content(result),
                               int(result.get("created") or 0) or None,
                               conversation_id=upstream_id)
    return resp


def image_chat_events(body: dict[str, Any]) -> Iterator[dict[str, Any]]:
    model, prompt, n, images = chat_image_args(body)
    image_outputs = stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        n=n,
        response_format="url",
        images=encode_images(images) or None,
    ))
    yield from stream_image_chat_completion(image_outputs, model)


def stream_image_chat_completion(image_outputs: Iterable[ImageOutput], model: str) -> Iterator[dict[str, Any]]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    sent_role = False
    sent_text = ""
    conv_id = ""
    for output in image_outputs:
        if output.conversation_id:
            conv_id = output.conversation_id
        content = ""
        if output.kind == "progress":
            content = output.text
            sent_text += content
        elif output.kind == "result":
            content = build_chat_image_markdown_content({"data": output.data})
        elif output.kind == "message":
            content = output.text[len(sent_text):] if output.text.startswith(sent_text) else output.text
        if not content:
            continue
        if not sent_role:
            sent_role = True
            yield completion_chunk(model, {"role": "assistant", "content": content}, None, completion_id, created)
        else:
            yield completion_chunk(model, {"content": content}, None, completion_id, created)
    if not sent_role:
        yield completion_chunk(model, {"role": "assistant", "content": ""}, None, completion_id, created)
    yield completion_chunk(model, {}, "stop", completion_id, created)
    if conv_id:
        yield {"object": "chat.session", "chat_id": conv_id, "conversation_id": conv_id}


# ── Vision helpers (image-in → text or image-out) ────────────────────────────

def _wrap_vision_stream(
    outputs: Iterator[ImageOutput],
    model: str,
    prior_conversation_id: str,
) -> Iterator[dict[str, Any]]:
    """Convert ImageOutput stream from vision pipeline into chat completion chunks."""
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    sent_role = False
    sent_text = ""
    conv_id = ""

    for output in outputs:
        if output.conversation_id:
            conv_id = output.conversation_id

        content = ""
        if output.kind == "progress" and output.text:
            content = output.text
            sent_text += content
        elif output.kind == "result" and output.data:
            url = str(output.data[-1].get("url") or "") if output.data else ""
            content = url
        elif output.kind == "message":
            content = output.text[len(sent_text):] if output.text.startswith(sent_text) else output.text

        if not content:
            continue
        if not sent_role:
            sent_role = True
            yield completion_chunk(model, {"role": "assistant", "content": content}, None, completion_id, created)
        else:
            yield completion_chunk(model, {"content": content}, None, completion_id, created)

    if not sent_role:
        yield completion_chunk(model, {"role": "assistant", "content": ""}, None, completion_id, created)
    yield completion_chunk(model, {}, "stop", completion_id, created)

    real_id = conv_id or prior_conversation_id
    if real_id:
        yield {
            "object": "chat.session",
            "chat_id": real_id,
            "conversation_id": real_id,
            "is_new_session": not prior_conversation_id,
        }


def _stream_vision_with_session(
    body: dict[str, Any],
    prior_conversation_id: str,
) -> Iterator[dict[str, Any]]:
    model = str(body.get("model") or "auto").strip() or "auto"
    base_url = str(body.get("base_url") or "") or None
    is_new_session = not prior_conversation_id
    messages = normalize_messages(chat_messages_from_body(body))
    send_messages = messages if is_new_session else _continuation_messages(messages)

    request = ConversationRequest(
        model=model,
        messages=send_messages,
        conversation_id=prior_conversation_id,
        history_and_training_disabled=False,
        base_url=base_url,
        response_format="url",
    )
    outputs = stream_vision_outputs_with_pool(request)
    yield from _wrap_vision_stream(outputs, model, prior_conversation_id)


def _vision_response_with_session(
    body: dict[str, Any],
    prior_conversation_id: str,
) -> dict[str, Any]:
    """Non-streaming vision: auto-detects text vs image response.
    Returns the real ChatGPT conversation_id in both chat_id and conversation_id fields.
    """
    model = str(body.get("model") or "auto").strip() or "auto"
    base_url = str(body.get("base_url") or "") or None
    is_new_session = not prior_conversation_id
    messages = normalize_messages(chat_messages_from_body(body))
    send_messages = messages if is_new_session else _continuation_messages(messages)

    request = ConversationRequest(
        model=model,
        messages=send_messages,
        conversation_id=prior_conversation_id,
        history_and_training_disabled=False,
        base_url=base_url,
        response_format="url",
    )
    outputs = list(stream_vision_outputs_with_pool(request))

    real_id = next((o.conversation_id for o in reversed(outputs) if o.conversation_id), "") \
              or prior_conversation_id or None

    result = collect_image_outputs(iter(outputs))
    result.pop("_upstream_conversation_id", None)

    if result.get("data"):
        last_item = result["data"][-1]
        url = str(last_item.get("url") or "")
        resp = completion_response(model, url, messages=send_messages, conversation_id=real_id)
        resp["image_url"] = url
        resp["response_type"] = "image"
        return resp

    content = str(result.get("message") or "")
    resp = completion_response(model, content, messages=send_messages, conversation_id=real_id)
    resp["response_type"] = "text"
    return resp


# ── Main handler ─────────────────────────────────────────────────────────────

def handle(body: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
    # chat_id IS the real ChatGPT conversation_id — no separate lookup needed.
    # New session: send chat_id="" or omit it; response gives the real UUID.
    # Follow-up: send chat_id = that UUID → continues the same thread.
    prior_conv_id = str(body.get("chat_id") or body.get("conversation_id") or "").strip()

    # ── Image generation (explicit gpt-image-2 model or modalities=image) ──
    if is_image_chat_request(body):
        return image_chat_events(body) if body.get("stream") else image_chat_response(body)

    # ── Vision: image content in messages + regular model ──────────────────
    if has_vision_content(body):
        if body.get("stream"):
            return _stream_vision_with_session(body, prior_conv_id)
        return _vision_response_with_session(body, prior_conv_id)

    # ── Text chat ──────────────────────────────────────────────────────────
    model, messages = text_chat_parts(body)
    is_new_session = not prior_conv_id

    if body.get("stream"):
        return _stream_text_with_session(messages, model, prior_conv_id)

    # Non-streaming
    send_messages = messages if is_new_session else _continuation_messages(messages)
    conv_id_out: list[str] = []
    request = ConversationRequest(
        model=model,
        messages=send_messages,
        conversation_id=prior_conv_id,
        history_and_training_disabled=False,
    )
    content = "".join(stream_text_deltas(text_backend(), request, conv_id_out))

    real_id = conv_id_out[0] if conv_id_out else prior_conv_id or None
    return completion_response(model, content, messages=send_messages, conversation_id=real_id)
