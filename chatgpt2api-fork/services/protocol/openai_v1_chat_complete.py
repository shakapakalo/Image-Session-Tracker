from __future__ import annotations

import time
import uuid
from typing import Any, Iterable, Iterator

from fastapi import HTTPException

from services.chat_session_service import chat_session_service
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
    prompt_tokens = count_message_tokens(messages, model) if messages else 0
    completion_tokens = count_text_tokens(content, model) if messages else 0
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
    if chat_id:
        resp["chat_id"] = chat_id
    if conversation_id:
        resp["conversation_id"] = conversation_id
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
    """For conversation_id continuation: return system messages + last user turn only.

    ChatGPT already holds the prior history server-side, so sending only the new
    message avoids duplicating earlier turns in the conversation.
    """
    sys_msgs = [m for m in messages if m.get("role") == "system"]
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            return sys_msgs + [messages[i]]
    return messages


def _stream_text_with_session(
    messages: list[dict[str, Any]],
    model: str,
    chat_id: str,
    is_new_session: bool,
    prior_conversation_id: str,
) -> Iterator[dict[str, Any]]:
    """Stream chat completion chunks.

    Uses a real ChatGPT conversation_id so each API chat appears as a
    session inside the ChatGPT web app, just like image sessions do.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    sent_role = False

    send_messages = messages if is_new_session else _continuation_messages(messages)

    conv_id_out: list[str] = []
    request = ConversationRequest(
        model=model,
        messages=send_messages,
        conversation_id=prior_conversation_id,
        history_and_training_disabled=False,  # visible in ChatGPT app
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

    # Persist conversation_id so follow-up requests continue the same chat
    new_conv_id = conv_id_out[0] if conv_id_out else ""
    if new_conv_id:
        chat_session_service.save_conversation_id(chat_id, new_conv_id)

    # Emit session chunk for new sessions so streaming clients can capture chat_id
    if is_new_session and new_conv_id:
        yield {"object": "chat.session", "chat_id": chat_id, "conversation_id": new_conv_id}


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
        response_format="url",  # always URL — no base64
        images=encode_images(images) or None,
    )))
    # Return only the most recently generated image
    data = result.get("data")
    if isinstance(data, list) and data:
        result = dict(result)
        result["data"] = [data[-1]]
    return completion_response(model, image_result_content(result), int(result.get("created") or 0) or None)


def image_chat_events(body: dict[str, Any]) -> Iterator[dict[str, Any]]:
    model, prompt, n, images = chat_image_args(body)
    image_outputs = stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        n=n,
        response_format="url",  # always URL — no base64
        images=encode_images(images) or None,
    ))
    yield from stream_image_chat_completion(image_outputs, model)


def stream_image_chat_completion(image_outputs: Iterable[ImageOutput], model: str) -> Iterator[dict[str, Any]]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    sent_role = False
    sent_text = ""
    for output in image_outputs:
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


# ── Vision helpers (image-in → text or image-out) ────────────────────────────

def _wrap_vision_stream(
    outputs: Iterator[ImageOutput],
    model: str,
    chat_id: str,
    is_new_session: bool,
) -> Iterator[dict[str, Any]]:
    """Convert ImageOutput stream from vision pipeline into chat completion chunks.

    Handles both text responses (progress/message kinds) and generated image
    responses (result kind — yields the saved URL as content).
    Saves conversation_id to session after stream is consumed.
    """
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
            # Image was generated — yield the saved URL
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

    if conv_id:
        chat_session_service.save_conversation_id(chat_id, conv_id)
    if is_new_session and conv_id:
        yield {"object": "chat.session", "chat_id": chat_id, "conversation_id": conv_id}


def _stream_vision_with_session(
    body: dict[str, Any],
    chat_id: str,
    is_new_session: bool,
    prior_conversation_id: str,
) -> Iterator[dict[str, Any]]:
    model = str(body.get("model") or "auto").strip() or "auto"
    base_url = str(body.get("base_url") or "") or None
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
    yield from _wrap_vision_stream(outputs, model, chat_id, is_new_session)


def _vision_response_with_session(
    body: dict[str, Any],
    chat_id: str,
    is_new_session: bool,
    prior_conversation_id: str,
) -> dict[str, Any]:
    """Non-streaming vision: returns text content or image URL depending on what
    ChatGPT returned, always including the real conversation_id.
    """
    model = str(body.get("model") or "auto").strip() or "auto"
    base_url = str(body.get("base_url") or "") or None
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

    # Extract conversation_id from whichever output carries it
    conv_id = next((o.conversation_id for o in reversed(outputs) if o.conversation_id), "")
    if conv_id:
        chat_session_service.save_conversation_id(chat_id, conv_id)

    result = collect_image_outputs(iter(outputs))
    result.pop("_upstream_conversation_id", None)

    if result.get("data"):
        # Image was generated — return the URL of the last image only
        last_item = result["data"][-1]
        url = str(last_item.get("url") or "")
        resp = completion_response(
            model, url,
            messages=send_messages,
            chat_id=chat_id if is_new_session and conv_id else None,
            conversation_id=conv_id or None,
        )
        resp["image_url"] = url
        resp["response_type"] = "image"
        return resp

    # Text response
    content = str(result.get("message") or "")
    resp = completion_response(
        model, content,
        messages=send_messages,
        chat_id=chat_id if is_new_session and conv_id else None,
        conversation_id=conv_id or None,
    )
    resp["response_type"] = "text"
    return resp


# ── Main handler ─────────────────────────────────────────────────────────────

def handle(body: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
    chat_id = str(body.get("chat_id") or "").strip()
    is_new_session = not chat_id
    if is_new_session:
        chat_id = chat_session_service.generate_id()

    # Retrieve stored conversation_id for existing sessions (used by all branches)
    prior_conversation_id: str = (
        chat_session_service.get_conversation_id(chat_id)
        if not is_new_session else ""
    ) or ""

    # ── Image generation (explicit gpt-image-2 model or modalities=image) ──
    if is_image_chat_request(body):
        return image_chat_events(body) if body.get("stream") else image_chat_response(body)

    # ── Vision: image content in messages + regular model ──────────────────
    # Send to ChatGPT with the image attached; auto-detect text vs image response.
    if has_vision_content(body):
        if body.get("stream"):
            return _stream_vision_with_session(body, chat_id, is_new_session, prior_conversation_id)
        return _vision_response_with_session(body, chat_id, is_new_session, prior_conversation_id)

    # ── Text chat: use real ChatGPT conversation_id ────────────────────────
    model, new_messages = text_chat_parts(body)

    if body.get("stream"):
        return _stream_text_with_session(
            messages=new_messages,
            model=model,
            chat_id=chat_id,
            is_new_session=is_new_session,
            prior_conversation_id=prior_conversation_id,
        )

    # Non-streaming text chat
    send_messages = new_messages if is_new_session else _continuation_messages(new_messages)

    conv_id_out: list[str] = []
    request = ConversationRequest(
        model=model,
        messages=send_messages,
        conversation_id=prior_conversation_id,
        history_and_training_disabled=False,  # visible in ChatGPT app
    )
    content = "".join(stream_text_deltas(text_backend(), request, conv_id_out))

    new_conv_id = conv_id_out[0] if conv_id_out else ""
    if new_conv_id:
        chat_session_service.save_conversation_id(chat_id, new_conv_id)

    return completion_response(
        model, content, messages=send_messages,
        chat_id=chat_id if is_new_session and new_conv_id else None,
        conversation_id=new_conv_id or None,
    )
