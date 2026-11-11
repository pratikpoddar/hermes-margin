"""Thin Google Drive / Docs / Slides REST client for Margin.

Every request goes through Hermes's ``credentialed_provider_request`` with the ``google`` OAuth
client declared in ``plugin.yaml`` (catalog rule 11): Hermes runs the sign-in
(``hermes auth add margin:google``), stores and refreshes the token and attaches the bearer
itself. Margin never sees, stores or refreshes a Google token.

The transport is injectable so the tests can run against a fake Google without credentials.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import quote, urlencode

logger = logging.getLogger(__name__)

PROVIDER_ID = "google"  # the ``id`` of the oauth entry in plugin.yaml

DRIVE = "https://www.googleapis.com/drive/v3"
DOCS = "https://docs.googleapis.com/v1"
SLIDES = "https://slides.googleapis.com/v1"

MIME_DOC = "application/vnd.google-apps.document"
MIME_SLIDES = "application/vnd.google-apps.presentation"
MIME_SHEET = "application/vnd.google-apps.spreadsheet"

COMMENT_FIELDS = (
    "nextPageToken,comments(id,content,htmlContent,createdTime,modifiedTime,resolved,deleted,"
    "author(displayName,me,emailAddress,permissionId),quotedFileContent(mimeType,value),"
    "replies(id,content,createdTime,modifiedTime,deleted,action,author(displayName,me,emailAddress,permissionId)))"
)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class GoogleAPIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Google API error {status}: {message}")
        self.status = status


class NotSignedIn(RuntimeError):
    """No usable Google sign-in for Margin in this Hermes profile."""


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body or b"{}")

    @property
    def text(self) -> str:
        return (self.body or b"").decode("utf-8", "replace")


Transport = Callable[[str, str, Any], Response]


def hermes_transport(method: str, url: str, body: Any = None) -> Response:
    """Send one request with the user's Google sign-in attached by Hermes.

    Called from this plugin's own frame so Hermes can identify the calling plugin
    (``credentialed_provider_request`` refuses calls with no plugin frame on the stack).
    """
    from hermes_cli.auth_constants import AuthError
    from hermes_cli.plugin_provider_requests import ProviderNotSignedIn, credentialed_provider_request

    try:
        resp = credentialed_provider_request(PROVIDER_ID, method, url, json=body, timeout=30.0)
    except ProviderNotSignedIn as exc:
        raise NotSignedIn(str(exc)) from None
    except AuthError as exc:
        raise NotSignedIn(f"{getattr(exc, 'code', 'auth_error')}: {exc}") from None
    return Response(resp.status, dict(resp.headers or {}), resp.body)


class GoogleClient:
    """Drive comments + file text + Docs/Slides replaceAllText, with retry/backoff."""

    def __init__(self, transport: Optional[Transport] = None, *, max_retries: int = 5,
                 base_delay: float = 1.0, max_delay: float = 32.0,
                 sleep: Callable[[float], None] = time.sleep):
        self._transport = transport or hermes_transport
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._sleep = sleep

    # -- plumbing ---------------------------------------------------------------------------
    def _backoff(self, attempt: int, resp: Optional[Response]) -> float:
        retry_after = None
        if resp is not None:
            for key, value in (resp.headers or {}).items():
                if key.lower() == "retry-after":
                    try:
                        retry_after = float(value)
                    except (TypeError, ValueError):
                        retry_after = None
        if retry_after is not None:
            return min(retry_after, self.max_delay)
        delay = min(self.base_delay * (2 ** attempt), self.max_delay)
        return delay + random.uniform(0, delay * 0.1)

    def request(self, method: str, url: str, body: Any = None) -> Any:
        """One API call; retries 429 and 5xx with exponential backoff. Returns parsed JSON (or text)."""
        attempt = 0
        while True:
            resp = self._transport(method, url, body)
            if resp.status < 300:
                if not resp.body:
                    return {}
                try:
                    return resp.json()
                except ValueError:
                    return resp.text
            if resp.status in RETRY_STATUSES and attempt < self.max_retries:
                delay = self._backoff(attempt, resp)
                logger.info("margin: Google %s on %s %s; retrying in %.1fs", resp.status, method,
                            url.split("?")[0], delay)
                self._sleep(delay)
                attempt += 1
                continue
            message = resp.text[:300]
            try:
                message = resp.json().get("error", {}).get("message", message)
            except (ValueError, AttributeError):
                pass
            raise GoogleAPIError(resp.status, message)

    # -- Drive -----------------------------------------------------------------------------
    def about_me(self) -> dict:
        return self.request("GET", f"{DRIVE}/about?fields=" + quote("user(displayName,emailAddress,permissionId)"))

    def get_file(self, file_id: str) -> dict:
        q = urlencode({"fields": "id,name,mimeType,webViewLink", "supportsAllDrives": "true"})
        return self.request("GET", f"{DRIVE}/files/{quote(file_id)}?{q}")

    def export_text(self, file_id: str, mime_type: str) -> str:
        export = "text/csv" if mime_type == MIME_SHEET else "text/plain"
        q = urlencode({"mimeType": export})
        result = self.request("GET", f"{DRIVE}/files/{quote(file_id)}/export?{q}")
        return result if isinstance(result, str) else json.dumps(result)

    def list_comments(self, file_id: str, start_modified_time: Optional[str] = None) -> list[dict]:
        comments: list[dict] = []
        page_token = None
        while True:
            params = {"fields": COMMENT_FIELDS, "pageSize": "100", "includeDeleted": "false"}
            if start_modified_time:
                params["startModifiedTime"] = start_modified_time
            if page_token:
                params["pageToken"] = page_token
            data = self.request("GET", f"{DRIVE}/files/{quote(file_id)}/comments?{urlencode(params)}")
            comments.extend(data.get("comments") or [])
            page_token = data.get("nextPageToken")
            if not page_token:
                return comments

    def create_reply(self, file_id: str, comment_id: str, content: str) -> dict:
        url = f"{DRIVE}/files/{quote(file_id)}/comments/{quote(comment_id)}/replies?fields=id,content"
        return self.request("POST", url, {"content": content})

    def update_reply(self, file_id: str, comment_id: str, reply_id: str, content: str) -> dict:
        url = (f"{DRIVE}/files/{quote(file_id)}/comments/{quote(comment_id)}/replies/"
               f"{quote(reply_id)}?fields=id,content")
        return self.request("PATCH", url, {"content": content})

    # -- editors ---------------------------------------------------------------------------
    def _replace_request(self, find: str, replace: str) -> dict:
        return {"requests": [{"replaceAllText": {
            "containsText": {"text": find, "matchCase": True}, "replaceText": replace}}]}

    def docs_replace_all_text(self, document_id: str, find: str, replace: str) -> int:
        data = self.request("POST", f"{DOCS}/documents/{quote(document_id)}:batchUpdate",
                            self._replace_request(find, replace))
        return _occurrences_changed(data)

    def slides_replace_all_text(self, presentation_id: str, find: str, replace: str) -> int:
        data = self.request("POST", f"{SLIDES}/presentations/{quote(presentation_id)}:batchUpdate",
                            self._replace_request(find, replace))
        return _occurrences_changed(data)


def _occurrences_changed(data: Any) -> int:
    try:
        return int(data["replies"][0]["replaceAllText"].get("occurrencesChanged", 0))
    except (KeyError, IndexError, TypeError, ValueError):
        return 0
