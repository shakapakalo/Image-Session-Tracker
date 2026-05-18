from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from services.config import DATA_DIR

SESSION_TTL_SECONDS = 24 * 60 * 60  # 24 hours


class ChatSessionService:
    """Maps caller-visible chat_id values to session state.

    Image sessions:
        Stores the upstream ChatGPT conversation_id so that follow-up image
        requests continue inside the same conversation, preserving character /
        style consistency.

    Text sessions:
        Stores the full message history so that follow-up text chat requests
        automatically include prior turns without the caller needing to manage
        history themselves.  (The upstream ChatGPT endpoint has
        history_and_training_disabled=True, so conversation_ids are ephemeral
        and cannot be reused across requests.)

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

    def _get_entry(self, chat_id: str) -> dict | None:
        chat_id = str(chat_id or "").strip()
        if not chat_id:
            return None
        entry = self._sessions.get(chat_id)
        if not entry:
            return None
        if time.time() - float(entry.get("updated_at") or 0) > self.ttl_seconds:
            self._sessions.pop(chat_id, None)
            self._save_locked()
            return None
        return entry

    # ── Image session helpers ────────────────────────────────────────────────

    def get_conversation_id(self, chat_id: str) -> str | None:
        with self._lock:
            entry = self._get_entry(chat_id)
            if not entry:
                return None
            return str(entry.get("conversation_id") or "") or None

    def save_conversation_id(self, chat_id: str, conversation_id: str) -> None:
        chat_id = str(chat_id or "").strip()
        conversation_id = str(conversation_id or "").strip()
        if not chat_id or not conversation_id:
            return
        with self._lock:
            existing = self._sessions.get(chat_id, {})
            existing["conversation_id"] = conversation_id
            existing["updated_at"] = time.time()
            self._sessions[chat_id] = existing
            self._save_locked()

    # ── Text session helpers ─────────────────────────────────────────────────

    def get_history(self, chat_id: str) -> list[dict[str, Any]]:
        """Return stored message history for *chat_id*, or an empty list."""
        with self._lock:
            entry = self._get_entry(chat_id)
            if not entry:
                return []
            history = entry.get("history")
            if isinstance(history, list):
                return list(history)
            return []

    def save_history(self, chat_id: str, history: list[dict[str, Any]]) -> None:
        """Persist *history* (all messages including the latest exchange)."""
        chat_id = str(chat_id or "").strip()
        if not chat_id:
            return
        with self._lock:
            existing = self._sessions.get(chat_id, {})
            existing["history"] = history
            existing["updated_at"] = time.time()
            self._sessions[chat_id] = existing
            self._save_locked()

    # ── Persistence ──────────────────────────────────────────────────────────

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
