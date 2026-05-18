from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from services.config import DATA_DIR

SESSION_TTL_SECONDS = 24 * 60 * 60  # 24 hours


class ChatSessionService:
    """Maps caller-visible chat_id values to upstream ChatGPT conversation_ids.

    On the first image request (no chat_id supplied) a new UUID is generated
    and returned to the caller in the response body.  On every subsequent
    request the caller passes that same chat_id back; the service resolves the
    stored conversation_id so that the upstream generation continues inside the
    same ChatGPT conversation, preserving character / style consistency.

    Sessions expire after SESSION_TTL_SECONDS of inactivity and are persisted
    to ``data/chat_sessions.json`` so they survive server restarts.
    """

    def __init__(self, path: Path, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._sessions = self._load_locked()

    def generate_id(self) -> str:
        return str(uuid.uuid4())

    def get_conversation_id(self, chat_id: str) -> str | None:
        chat_id = str(chat_id or "").strip()
        if not chat_id:
            return None
        with self._lock:
            entry = self._sessions.get(chat_id)
            if not entry:
                return None
            if time.time() - float(entry.get("updated_at") or 0) > self.ttl_seconds:
                self._sessions.pop(chat_id, None)
                self._save_locked()
                return None
            return str(entry.get("conversation_id") or "") or None

    def save_conversation_id(self, chat_id: str, conversation_id: str) -> None:
        chat_id = str(chat_id or "").strip()
        conversation_id = str(conversation_id or "").strip()
        if not chat_id or not conversation_id:
            return
        with self._lock:
            self._sessions[chat_id] = {
                "conversation_id": conversation_id,
                "updated_at": time.time(),
            }
            self._save_locked()

    def _load_locked(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        cutoff = time.time() - self.ttl_seconds
        return {
            k: v
            for k, v in raw.items()
            if isinstance(v, dict) and float(v.get("updated_at") or 0) > cutoff
        }

    def _save_locked(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._sessions, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)


chat_session_service = ChatSessionService(DATA_DIR / "chat_sessions.json")
