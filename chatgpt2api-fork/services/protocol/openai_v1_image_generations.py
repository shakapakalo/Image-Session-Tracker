from __future__ import annotations

from typing import Any, Iterator

from services.protocol.conversation import (
    ConversationRequest,
    collect_image_outputs,
    stream_image_chunks,
    stream_image_outputs_with_pool,
)


def _keep_last_image(result: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *result* containing only the most recently generated image URL."""
    data = result.get("data")
    if isinstance(data, list) and data:
        result = dict(result)
        result["data"] = [data[-1]]
    return result


def _inject_ids(result: dict[str, Any], conv_id: str) -> dict[str, Any]:
    """Add chat_id and conversation_id (same real GPT UUID) to the result."""
    if conv_id:
        result = dict(result)
        result["chat_id"] = conv_id
        result["conversation_id"] = conv_id
    return result


def _wrap_stream(
    chunks: Iterator[dict[str, Any]],
    prior_conv_id: str,
) -> Iterator[dict[str, Any]]:
    """Wrap image chunk stream: capture the real conversation_id and emit a
    session chunk at the end so streaming clients know the chat_id."""
    conv_id = prior_conv_id
    for chunk in chunks:
        if isinstance(chunk, dict):
            cid = chunk.pop("_upstream_conversation_id", "")
            if cid:
                conv_id = cid
        yield chunk
    if conv_id:
        yield {
            "object": "image.generation.session",
            "chat_id": conv_id,
            "conversation_id": conv_id,
        }


def handle(body: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
    prompt = str(body.get("prompt") or "")
    model = str(body.get("model") or "gpt-image-2")
    n = int(body.get("n") or 1)
    size = body.get("size")
    response_format = "url"  # always URL — never base64
    base_url = str(body.get("base_url") or "") or None

    # chat_id IS the real ChatGPT conversation_id.
    # New session: omit chat_id; response returns the real UUID as chat_id.
    # Follow-up: pass that UUID back as chat_id → continues the same thread.
    prior_conv_id = str(body.get("chat_id") or body.get("conversation_id") or "").strip()

    outputs = stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        n=n,
        size=size,
        response_format=response_format,
        base_url=base_url,
        message_as_error=True,
        conversation_id=prior_conv_id,
    ))

    if body.get("stream"):
        return _wrap_stream(stream_image_chunks(outputs), prior_conv_id)

    result = collect_image_outputs(outputs)
    conv_id = result.pop("_upstream_conversation_id", "") or prior_conv_id
    result = _keep_last_image(result)
    result = _inject_ids(result, conv_id)
    return result
