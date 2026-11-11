"""Margin's durable state on top of Hermes's per-plugin state (``ctx.state``).

``ctx.state`` lives under ``<HERMES_HOME>/plugin-data/`` for the active profile, is atomically
replaced and safe across processes, so the ``/margin`` and ``hermes margin`` commands (which
write the watch list) and the gateway adapter (which writes poll cursors and thread state)
share it without talking to each other. No credentials are ever stored here.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

MAX_SEEN_PER_FILE = 3000
MAX_POSTED = 3000


class DictState:
    """In-memory stand-in with the ``get``/``set`` surface of Hermes's ``PluginState`` (tests, fallback)."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class MarginStore:
    def __init__(self, state: Any):
        self._state = state if state is not None else DictState()

    # -- watch list (shared with the commands) ---------------------------------------------
    def watched(self) -> list[str]:
        return list(self._state.get("watch", default=[]) or [])

    def watch(self, file_id: str) -> bool:
        ids = self.watched()
        if file_id in ids:
            return False
        self._state.set("watch", ids + [file_id])
        return True

    def unwatch(self, file_id: str) -> bool:
        ids = self.watched()
        if file_id not in ids:
            return False
        self._state.set("watch", [i for i in ids if i != file_id])
        files = self.files()
        if files.pop(file_id, None) is not None:
            self._state.set("files", files)
        return True

    # -- per-file poll state ---------------------------------------------------------------
    def files(self) -> dict[str, dict]:
        return dict(self._state.get("files", default={}) or {})

    def file_state(self, file_id: str) -> dict:
        return dict(self.files().get(file_id) or {"seen": [], "baselined": False})

    def save_file_state(self, file_id: str, fs: dict) -> None:
        seen = list(fs.get("seen") or [])
        if len(seen) > MAX_SEEN_PER_FILE:
            fs["seen"] = seen[-MAX_SEEN_PER_FILE:]
        files = self.files()
        files[file_id] = fs
        self._state.set("files", files)

    # -- threads Margin has joined ---------------------------------------------------------
    def threads(self) -> dict[str, dict]:
        return dict(self._state.get("threads", default={}) or {})

    def thread(self, file_id: str, comment_id: str) -> Optional[dict]:
        return self.threads().get(f"{file_id}:{comment_id}")

    def save_thread(self, file_id: str, comment_id: str, data: dict) -> None:
        threads = self.threads()
        threads[f"{file_id}:{comment_id}"] = data
        self._state.set("threads", threads)

    # -- ids of replies Margin itself posted (self-comment filter) --------------------------
    def posted(self) -> set[str]:
        return set(self._state.get("posted", default=[]) or [])

    def add_posted(self, reply_ids: Iterable[str]) -> None:
        current = list(self._state.get("posted", default=[]) or [])
        for rid in reply_ids:
            if rid and rid not in current:
                current.append(rid)
        self._state.set("posted", current[-MAX_POSTED:])
