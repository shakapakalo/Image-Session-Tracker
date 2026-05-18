from __future__ import annotations

from typing import Any, Iterator

from services.chat_session_service import chat_session_service
from services.protocol.conversation import (
    ConversationRequest,
    ImageGenerationError,
    collect_image_outputs,
    encode_images,
    stream_image_chunks,
    stream_image_outputs_with_pool,
)


def _wrap_stream_with_chat_id(
    chunks: Iterator[dict[str, Any]],
    chat_id: str,
    is_new_session: bool,
) -> Iterator[dict[str, Any]]:
    upstream_conversation_id = ""
    for chunk in chunks:
        if isinstance(chunk, dict):
            conv_id = chunk.pop("_upstream_conversation_id", "")
            if conv_id:
                upstream_conversation_id = conv_id
        yield chunk
    if upstream_conversation_id:
        chat_session_service.save_conversation_id(chat_id, upstream_conversation_id)
    if is_new_session:
        yield {"object": "image.generation.session", "chat_id": chat_id}


def handle(body: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
    prompt = str(body.get("prompt") or "")
    images = body.get("images") or []
    model = str(body.get("model") or "gpt-image-2")
    n = int(body.get("n") or 1)
    size = body.get("size")
    response_format = str(body.get("response_format") or "b64_json")
    base_url = str(body.get("base_url") or "") or None

    chat_id = str(body.get("chat_id") or "").strip()
    is_new_session = not chat_id
    if is_new_session:
        chat_id = chat_session_service.generate_id()

    prior_conversation_id: str = (
        chat_session_service.get_conversation_id(chat_id)
        if not is_new_session
        else None
    ) or ""

    encoded_images = encode_images(images)
    if not encoded_images:
        raise ImageGenerationError("image is required")

    outputs = stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        n=n,
        size=size,
        response_format=response_format,
        base_url=base_url,
        images=encoded_images,
        message_as_error=True,
        conversation_id=prior_conversation_id,
    ))

    if body.get("stream"):
        return _wrap_stream_with_chat_id(stream_image_chunks(outputs), chat_id, is_new_session)

    result = collect_image_outputs(outputs)
    upstream_conversation_id = result.pop("_upstream_conversation_id", "")
    if upstream_conversation_id:
        chat_session_service.save_conversation_id(chat_id, upstream_conversation_id)
    if is_new_session and result.get("data"):
        result["chat_id"] = chat_id
    return result
