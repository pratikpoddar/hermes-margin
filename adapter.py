"""Margin gateway adapter: Google Docs / Slides / Sheets comment threads as Hermes chats.

Routing
    chat_id   = Drive file id
    thread_id = Drive comment id   -> one Hermes session per comment thread
    message   = "<comment_id>:<reply_id>" for replies Margin posts (so edit_message can find them)

Inbound
    Long-polls Drive ``comments.list`` for each watched file (default every 10 s, exponential
    backoff on errors). A new unresolved comment containing the trigger (default ``@hermes``)
    opens a thread; any later reply in a thread Margin joined continues it. The first turn of a
    thread carries the document text and the highlighted (quoted) text. Replies "apply" in a
    thread replace the highlighted text with the last proposal (Docs / Slides only).

Outbound
    ``send`` posts a reply in the comment thread, editing Margin's status reply in place when one
    is pending; ``edit_message`` edits a reply Margin posted.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import extra_or_secret, get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType

from . import logic
from .google_client import (
    MIME_DOC, MIME_SLIDES, GoogleAPIError, GoogleClient, NotSignedIn, RETRY_STATUSES,
)
from .store import MarginStore

logger = logging.getLogger(__name__)

PLATFORM_NAME = "margin"
DEFAULT_TRIGGER = "@hermes"
DEFAULT_POLL_SECONDS = 10.0
MAX_POLL_BACKOFF_SECONDS = 300.0
DEFAULT_MAX_DOC_CHARS = 60000
STATUS_READING = f"{logic.BOT_MARKER} is reading the doc…"
STATUS_THINKING = f"{logic.BOT_MARKER} is thinking…"
_TRUTHY = {"1", "true", "yes", "on"}


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def _csv(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def env_watch_files() -> list[str]:
    """File ids from MARGIN_WATCH_FILES (comma-separated ids or URLs)."""
    ids = []
    for raw in _csv(get_scoped_secret("MARGIN_WATCH_FILES", "") or ""):
        file_id = logic.parse_file_id(raw)
        if file_id:
            ids.append(file_id)
    return ids


class MarginAdapter(BasePlatformAdapter):
    """Drive comment threads <-> Hermes sessions."""

    def __init__(self, config: PlatformConfig, *, state: Any = None, client: Optional[GoogleClient] = None):
        super().__init__(config, Platform(PLATFORM_NAME))
        # Gateway setup notices ("no home channel", etc.) are chat-app concepts. Route them to the
        # private path, which Margin keeps in the log, so they never land in a document's comments.
        if isinstance(getattr(config, "extra", None), dict):
            config.extra.setdefault("notice_delivery", "private")
        extra = config.extra if isinstance(config.extra, dict) else {}
        if not isinstance(config.extra, dict):
            config.extra = extra
        self.trigger = str(extra_or_secret(extra, "trigger", "MARGIN_TRIGGER", DEFAULT_TRIGGER) or DEFAULT_TRIGGER)
        self.poll_interval = max(2.0, float(
            extra_or_secret(extra, "poll_interval", "MARGIN_POLL_INTERVAL", DEFAULT_POLL_SECONDS) or DEFAULT_POLL_SECONDS))
        self.max_doc_chars = int(
            extra_or_secret(extra, "max_doc_chars", "MARGIN_MAX_DOC_CHARS", DEFAULT_MAX_DOC_CHARS) or DEFAULT_MAX_DOC_CHARS)
        self.allow_all = _truthy(extra_or_secret(extra, "allow_all_users", "MARGIN_ALLOW_ALL_USERS", ""))
        self.allowed_users = set(_csv(extra_or_secret(extra, "allowed_users", "MARGIN_ALLOWED_USERS", "")))
        self.store = MarginStore(state)
        self.client = client or GoogleClient()
        self._me: dict = {}
        self._status: dict[tuple[str, str], str] = {}  # (file_id, comment_id) -> pending status reply id
        self._file_meta: dict[str, dict] = {}
        self._poll_task: Optional[asyncio.Task] = None
        self._poll_delay = self.poll_interval

    # -- lifecycle -------------------------------------------------------------------------
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        try:
            about = await self._call(self.client.about_me)
            self._me = dict((about or {}).get("user") or {})
        except NotSignedIn as exc:
            message = f"Margin is not signed in to Google: {exc}"
            logger.error(message)
            self._set_fatal_error("not_signed_in", message, retryable=False)
            return False
        except Exception as exc:  # network blip: keep going, the poll loop retries with backoff
            logger.warning("margin: could not read the signed-in Google account yet: %s", exc)
        own = self._own_principal()
        if not self.allowed_users and not self.allow_all:
            # Default policy: only the signed-in Google account may drive the agent.
            self.config.extra["allowed_users"] = [own]
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._mark_connected()
        logger.info("margin: connected as %s, watching %d file(s)", own, len(self.watched_files()))
        return True

    async def disconnect(self) -> None:
        self._running = False
        task, self._poll_task = self._poll_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._mark_disconnected()

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        meta = self._file_meta.get(chat_id) or {}
        return {"name": meta.get("name") or chat_id, "type": "group"}

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None  # the status reply is Margin's typing indicator

    # -- helpers ---------------------------------------------------------------------------
    async def _call(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    def watched_files(self) -> list[str]:
        ids = list(self.store.watched())
        for file_id in env_watch_files():
            if file_id not in ids:
                ids.append(file_id)
        return ids

    def _own_principal(self) -> str:
        return str(self._me.get("emailAddress") or self._me.get("permissionId") or "me")

    def _principal(self, author: dict) -> str:
        if author.get("me"):
            return self._own_principal()
        return str(author.get("emailAddress") or author.get("permissionId")
                   or f"name:{author.get('displayName') or 'unknown'}")

    def _authorized(self, author: dict) -> bool:
        if author.get("me"):
            return True
        if self.allow_all:
            return True
        principal = self._principal(author)
        return principal in self.allowed_users

    def _is_self(self, item_id: Optional[str], text: str) -> bool:
        return (item_id is not None and item_id in self.store.posted()) or logic.is_bot_text(text)

    async def _meta(self, file_id: str, refresh: bool = False) -> dict:
        if refresh or file_id not in self._file_meta:
            self._file_meta[file_id] = await self._call(self.client.get_file, file_id)
        return self._file_meta[file_id]

    async def _doc_text(self, file_id: str, mime_type: str) -> str:
        text = await self._call(self.client.export_text, file_id, mime_type)
        return (text or "").lstrip("\ufeff")

    async def _post(self, file_id: str, comment_id: str, text: str) -> str:
        reply = await self._call(self.client.create_reply, file_id, comment_id, text)
        reply_id = str((reply or {}).get("id") or "")
        if reply_id:
            self.store.add_posted([reply_id])
        return reply_id

    async def _edit(self, file_id: str, comment_id: str, reply_id: str, text: str) -> None:
        await self._call(self.client.update_reply, file_id, comment_id, reply_id, text)

    async def _say(self, file_id: str, comment_id: str, text: str) -> None:
        """A Margin-authored reply that does not go through the agent (apply results, errors)."""
        body = text if logic.is_bot_text(text) else f"{logic.BOT_MARKER}: {text}"
        pending = self._status.pop((file_id, comment_id), None)
        if pending:
            await self._edit(file_id, comment_id, pending, body)
        else:
            await self._post(file_id, comment_id, body)

    # -- polling ---------------------------------------------------------------------------
    async def _poll_loop(self) -> None:
        while self._running:
            try:
                errors = await self.poll_once()
                self._poll_delay = self.poll_interval if not errors else min(
                    self._poll_delay * 2, MAX_POLL_BACKOFF_SECONDS)
            except asyncio.CancelledError:
                raise
            except NotSignedIn as exc:
                logger.error("margin: Google sign-in missing or revoked (%s); run `hermes auth add margin:google`", exc)
                self._poll_delay = MAX_POLL_BACKOFF_SECONDS
            except Exception:
                logger.exception("margin: poll cycle failed")
                self._poll_delay = min(self._poll_delay * 2, MAX_POLL_BACKOFF_SECONDS)
            await asyncio.sleep(self._poll_delay)

    async def poll_once(self) -> int:
        """Poll every watched file once; returns how many files failed."""
        errors = 0
        for file_id in self.watched_files():
            try:
                await self._poll_file(file_id)
            except NotSignedIn:
                raise
            except Exception as exc:
                errors += 1
                logger.warning("margin: polling %s failed: %s", file_id, exc)
                fs = self.store.file_state(file_id)
                fs["error"] = str(exc)[:300]
                fs["last_poll"] = _now()
                self.store.save_file_state(file_id, fs)
        return errors

    async def _poll_file(self, file_id: str) -> None:
        fs = self.store.file_state(file_id)
        seen_list: list[str] = list(fs.get("seen") or [])
        seen = set(seen_list)
        baseline = not fs.get("baselined")
        cursor = fs.get("cursor")
        comments = await self._call(self.client.list_comments, file_id, cursor)
        newest = cursor
        work: list[tuple[dict, Optional[dict]]] = []
        for comment in sorted(comments, key=lambda c: c.get("createdTime") or ""):
            modified = comment.get("modifiedTime") or ""
            if modified and (newest is None or modified > newest):
                newest = modified
            items: list[tuple[str, Optional[dict]]] = [(f"c:{comment.get('id')}", None)]
            for reply in sorted(comment.get("replies") or [], key=lambda r: r.get("createdTime") or ""):
                items.append((f"r:{reply.get('id')}", reply))
            for key, reply in items:
                if key in seen:
                    continue
                seen.add(key)
                seen_list.append(key)
                if baseline or comment.get("resolved") or comment.get("deleted"):
                    continue
                if reply is not None and reply.get("deleted"):
                    continue
                work.append((comment, reply))
        fs.update(seen=seen_list, cursor=newest, baselined=True, error=None, last_poll=_now())
        self.store.save_file_state(file_id, fs)  # persist before acting: a crash never replays a turn
        for comment, reply in work:
            try:
                await self._handle_item(file_id, comment, reply)
            except NotSignedIn:
                raise
            except Exception:
                logger.exception("margin: handling comment %s on %s failed", comment.get("id"), file_id)

    async def _handle_item(self, file_id: str, comment: dict, reply: Optional[dict]) -> None:
        comment_id = str(comment.get("id"))
        item = reply if reply is not None else comment
        item_id = str(item.get("id"))
        text = logic.plain(item.get("content"))
        author = dict(item.get("author") or {})
        if self._is_self(item_id, text):
            return
        if reply is not None and not text:  # resolve / reopen actions carry no text
            return
        thread = self.store.thread(file_id, comment_id)
        if thread is None and not logic.has_trigger(text, self.trigger):
            return
        if not self._authorized(author):
            logger.info("margin: ignoring comment from unauthorized author %s on %s",
                        author.get("displayName"), file_id)
            return
        if thread is not None and logic.is_apply(text, self.trigger):
            await self.apply_proposal(file_id, comment_id)
            return
        await self._dispatch(file_id, comment, item_id, text, author, new_thread=thread is None)

    async def _dispatch(self, file_id: str, comment: dict, item_id: str, text: str, author: dict,
                        *, new_thread: bool) -> None:
        comment_id = str(comment.get("id"))
        key = (file_id, comment_id)
        if key not in self._status:
            status_id = await self._post(file_id, comment_id, STATUS_READING)
            if status_id:
                self._status[key] = status_id
        try:
            meta = await self._meta(file_id, refresh=new_thread)
        except Exception as exc:
            await self._say(file_id, comment_id, f"I couldn't open this file ({exc}).")
            return
        mime = meta.get("mimeType") or ""
        title = meta.get("name") or file_id
        request = logic.strip_trigger(text, self.trigger)
        if new_thread:
            quoted = logic.plain((comment.get("quotedFileContent") or {}).get("value"))
            try:
                doc_text = await self._doc_text(file_id, mime)
            except Exception as exc:
                await self._say(file_id, comment_id, f"I couldn't read the document text ({exc}).")
                return
            prompt = logic.build_first_prompt(
                request=request, title=title, mime_type=mime, link=meta.get("webViewLink") or "",
                quoted=quoted, doc_text=doc_text, max_chars=self.max_doc_chars,
                can_apply=mime in (MIME_DOC, MIME_SLIDES))
            self.store.save_thread(file_id, comment_id, {
                "quoted": quoted, "mime": mime, "title": title, "proposal": None, "started": _now()})
        else:
            prompt = request or text
        status_id = self._status.get(key)
        if status_id:
            try:
                await self._edit(file_id, comment_id, status_id, STATUS_THINKING)
            except Exception as exc:
                logger.debug("margin: status edit failed: %s", exc)
        source = self.build_source(
            chat_id=file_id, chat_name=title, chat_type="group",
            user_id=self._principal(author), user_name=author.get("displayName") or None,
            thread_id=comment_id, message_id=item_id)
        event = MessageEvent(
            text=prompt, message_type=MessageType.TEXT, source=source, message_id=item_id,
            user_id=source.user_id, user_name=source.user_name,
            raw_message={"file_id": file_id, "comment_id": comment_id, "item_id": item_id},
            reply_expected=True,
            # Document text is untrusted: it must never resolve gateway commands or prompts.
            allow_gateway_control=False,
        )
        await self.handle_message(event)

    # -- apply -----------------------------------------------------------------------------
    async def apply_proposal(self, file_id: str, comment_id: str) -> str:
        """Replace the thread's highlighted text with its last proposal; returns the reply posted."""
        thread = self.store.thread(file_id, comment_id) or {}
        proposal = thread.get("proposal")
        quoted = thread.get("quoted") or ""
        mime = thread.get("mime") or ""
        if mime not in (MIME_DOC, MIME_SLIDES):
            message = "\"apply\" works in Google Docs and Slides only; please copy the suggestion by hand."
        elif not quoted:
            message = "This comment isn't attached to highlighted text, so there's nothing to replace."
        elif not proposal:
            message = "There's no proposed replacement in this thread yet. Ask me for new wording first."
        elif "\n" in quoted.strip() and mime == MIME_DOC:
            message = await self._apply_doc_range(file_id, comment_id, thread, quoted, proposal)
        elif "\n" in quoted.strip():
            message = ("The highlighted text spans more than one paragraph, which Margin can only replace "
                       "in Google Docs. Nothing was changed; please paste the suggestion by hand.")
        else:
            doc_text = await self._doc_text(file_id, mime)
            count = logic.count_occurrences(doc_text, quoted)
            if count == 0:
                message = ("The highlighted text no longer appears exactly as quoted (it may have been "
                           "edited), so nothing was changed.")
            elif count > 1:
                message = (f"The highlighted text appears {count} times in the document. Margin only "
                           "replaces text that occurs exactly once, so nothing was changed.")
            else:
                fn = (self.client.docs_replace_all_text if mime == MIME_DOC
                      else self.client.slides_replace_all_text)
                try:
                    changed = await self._call(fn, file_id, quoted, proposal)
                except GoogleAPIError as exc:
                    changed, message = -1, f"Google refused the edit ({exc}). Nothing was changed."
                if changed == 1:
                    message = "Applied. The highlighted text now reads the proposed replacement."
                    thread.update(quoted=proposal, proposal=None, applied=_now())
                    self.store.save_thread(file_id, comment_id, thread)
                elif changed == 0:
                    message = "Google found nothing to replace (the text may span formatting changes). Nothing was changed."
                elif changed > 1:
                    message = f"Applied, but Google replaced {changed} occurrences."
        await self._say(file_id, comment_id, message)
        return message

    async def _apply_doc_range(self, file_id: str, comment_id: str, thread: dict,
                               quoted: str, proposal: str) -> str:
        """Multi-paragraph apply for Docs: find the quote's exact index range, then delete + insert."""
        try:
            document = await self._call(self.client.get_document, file_id)
            count, span = logic.find_unique_range(document, quoted)
        except GoogleAPIError as exc:
            return f"Google refused to read the document ({exc}). Nothing was changed."
        if count == 0:
            return ("The highlighted text no longer appears exactly as quoted (it may have been "
                    "edited), so nothing was changed.")
        if count > 1 or not span:
            return (f"The highlighted text appears {count} times in the document. Margin only "
                    "replaces text that occurs exactly once, so nothing was changed.")
        try:
            await self._call(self.client.docs_replace_range, file_id, span[0], span[1], proposal)
        except GoogleAPIError as exc:
            return f"Google refused the edit ({exc}). Nothing was changed."
        thread.update(quoted=proposal, proposal=None, applied=_now())
        self.store.save_thread(file_id, comment_id, thread)
        return "Applied. The highlighted paragraphs now read the proposed replacement."

    # -- outbound --------------------------------------------------------------------------
    def _comment_for(self, reply_to: Optional[str], metadata: Optional[dict]) -> Optional[str]:
        thread_id = (metadata or {}).get("thread_id")
        if thread_id:
            return str(thread_id)
        if reply_to and ":" in str(reply_to):
            return str(reply_to).split(":", 1)[0]
        return None

    async def send_private_notice(self, chat_id: str, user_id: Optional[str], content: str,
                                  reply_to: Optional[str] = None,
                                  metadata: Optional[dict[str, Any]] = None) -> SendResult:
        """Comment threads are shared with every collaborator and have no private channel, so
        operational notices go to the gateway log instead of the document."""
        logger.info("Margin: notice kept out of %s: %s", chat_id, (content or "")[:200])
        return SendResult(success=True)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        comment_id = self._comment_for(reply_to, metadata)
        if not comment_id:
            return SendResult(success=False, error="Margin replies need a comment thread (thread_id)",
                              error_kind="not_found")
        thread = self.store.thread(chat_id, comment_id) or {}
        proposal = logic.extract_proposal(content)
        can_apply = bool(thread.get("quoted")) and thread.get("mime") in (MIME_DOC, MIME_SLIDES)
        if proposal is not None and thread:
            thread["proposal"] = proposal
            self.store.save_thread(chat_id, comment_id, thread)
        text = logic.render_reply(content, proposal=proposal, can_apply=can_apply)
        try:
            pending = self._status.pop((chat_id, comment_id), None)
            if pending:
                await self._edit(chat_id, comment_id, pending, text)
                reply_id = pending
            else:
                reply_id = await self._post(chat_id, comment_id, text)
        except GoogleAPIError as exc:
            return SendResult(success=False, error=str(exc), retryable=exc.status in RETRY_STATUSES,
                              error_kind="rate_limited" if exc.status == 429 else "unknown")
        except NotSignedIn as exc:
            return SendResult(success=False, error=str(exc), error_kind="forbidden")
        return SendResult(success=True, message_id=f"{comment_id}:{reply_id}")

    async def edit_message(self, chat_id: str, message_id: str, content: str, *,
                           finalize: bool = False) -> SendResult:
        if ":" not in str(message_id):
            return SendResult(success=False, error="unknown Margin message id")
        comment_id, reply_id = str(message_id).split(":", 1)
        thread = self.store.thread(chat_id, comment_id) or {}
        proposal = logic.extract_proposal(content)
        if proposal is not None and thread:
            thread["proposal"] = proposal
            self.store.save_thread(chat_id, comment_id, thread)
        can_apply = bool(thread.get("quoted")) and thread.get("mime") in (MIME_DOC, MIME_SLIDES)
        try:
            await self._edit(chat_id, comment_id, reply_id,
                             logic.render_reply(content, proposal=proposal, can_apply=can_apply))
        except (GoogleAPIError, NotSignedIn) as exc:
            return SendResult(success=False, error=str(exc))
        return SendResult(success=True, message_id=message_id)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
