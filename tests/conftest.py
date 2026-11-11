"""Test fixtures: a fake Google (Drive comments, export, Docs/Slides batchUpdate) behind the
injectable transport, so no credentials or network are needed."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

# Import the plugin as the package ``margin`` (the repository root is the plugin directory).
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT.parent))
if _ROOT.name != "margin":  # cloned under another name: alias it
    import importlib.util
    spec = importlib.util.spec_from_file_location("margin", _ROOT / "__init__.py",
                                                  submodule_search_locations=[str(_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["margin"] = module
    spec.loader.exec_module(module)

from margin.google_client import GoogleClient, Response  # noqa: E402

DOC_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
ME = {"displayName": "Pratik", "emailAddress": "owner@example.com", "permissionId": "111", "me": True}
OTHER = {"displayName": "Stranger", "permissionId": "999", "me": False}


class FakeGoogle:
    def __init__(self):
        self.files = {DOC_ID: {"id": DOC_ID, "name": "Launch plan",
                               "mimeType": "application/vnd.google-apps.document",
                               "webViewLink": f"https://docs.google.com/document/d/{DOC_ID}/edit"}}
        self.text = {DOC_ID: "Launch plan\nOur product is very good and people likes it.\nShip in Q3.\n"}
        self.comments: dict[str, list[dict]] = {DOC_ID: []}
        self.calls: list[tuple[str, str, object]] = []
        self.fail_next: list[int] = []  # statuses to return before succeeding
        self._n = 0
        self.clock = 0

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}{self._n}"

    def _ts(self):
        self.clock += 1
        return f"2026-11-11T10:00:{self.clock:02d}.000Z"

    # helpers used by tests
    def add_comment(self, content, *, quoted=None, author=ME, file_id=DOC_ID):
        c = {"id": self._id("C"), "content": content, "author": dict(author), "resolved": False,
             "deleted": False, "replies": [], "createdTime": self._ts()}
        c["modifiedTime"] = c["createdTime"]
        if quoted is not None:
            c["quotedFileContent"] = {"mimeType": "text/html", "value": quoted}
        self.comments[file_id].append(c)
        return c

    def add_reply(self, comment, content, *, author=ME):
        r = {"id": self._id("R"), "content": content, "author": dict(author), "deleted": False,
             "createdTime": self._ts()}
        r["modifiedTime"] = r["createdTime"]
        comment["replies"].append(r)
        comment["modifiedTime"] = r["modifiedTime"]
        return r

    def find_comment(self, comment_id, file_id=DOC_ID):
        return next(c for c in self.comments[file_id] if c["id"] == comment_id)

    def bot_replies(self, comment):
        return [r for r in comment["replies"] if r["author"].get("bot")]

    # the transport
    def __call__(self, method, url, body=None):
        self.calls.append((method, url, body))
        if self.fail_next:
            status = self.fail_next.pop(0)
            return Response(status, {}, json.dumps({"error": {"message": "slow down"}}).encode())
        parsed = urlparse(url)
        path = unquote(parsed.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        ok = lambda data: Response(200, {}, json.dumps(data).encode())  # noqa: E731
        if path == "/drive/v3/about":
            return ok({"user": {k: v for k, v in ME.items() if k != "me"}})
        m = re.fullmatch(r"/drive/v3/files/([^/]+)", path)
        if m and method == "GET":
            return ok(self.files[m.group(1)])
        m = re.fullmatch(r"/drive/v3/files/([^/]+)/export", path)
        if m:
            return Response(200, {}, ("\ufeff" + self.text[m.group(1)]).encode())
        m = re.fullmatch(r"/drive/v3/files/([^/]+)/comments", path)
        if m and method == "GET":
            since = query.get("startModifiedTime")
            items = [c for c in self.comments[m.group(1)] if not since or c["modifiedTime"] >= since]
            return ok({"comments": json.loads(json.dumps(items))})
        m = re.fullmatch(r"/drive/v3/files/([^/]+)/comments/([^/]+)/replies", path)
        if m and method == "POST":
            comment = self.find_comment(m.group(2), m.group(1))
            r = self.add_reply(comment, body["content"], author={"displayName": "Pratik", "me": True, "bot": True})
            return ok({"id": r["id"], "content": r["content"]})
        m = re.fullmatch(r"/drive/v3/files/([^/]+)/comments/([^/]+)/replies/([^/]+)", path)
        if m and method == "PATCH":
            comment = self.find_comment(m.group(2), m.group(1))
            reply = next(r for r in comment["replies"] if r["id"] == m.group(3))
            reply["content"] = body["content"]
            reply["modifiedTime"] = comment["modifiedTime"] = self._ts()
            return ok({"id": reply["id"], "content": reply["content"]})
        m = re.fullmatch(r"/v1/(?:documents|presentations)/([^/:]+):batchUpdate", path)
        if m:
            req = body["requests"][0]["replaceAllText"]
            find, repl = req["containsText"]["text"], req["replaceText"]
            n = self.text[m.group(1)].count(find)
            self.text[m.group(1)] = self.text[m.group(1)].replace(find, repl)
            return ok({"replies": [{"replaceAllText": {"occurrencesChanged": n}}]})
        return Response(404, {}, b'{"error": {"message": "not found"}}')


class FakeCtx:
    """Just enough PluginContext to run ``register(ctx)`` in tests."""

    def __init__(self):
        from margin.store import DictState
        self.state = DictState()
        self.platforms, self.commands, self.cli = {}, {}, {}

    def register_platform(self, **kwargs):
        from gateway.platform_registry import PlatformEntry, platform_registry
        self.platforms[kwargs["name"]] = kwargs
        platform_registry.register(PlatformEntry(**kwargs), scope="margin-tests")
        platform_registry.register(PlatformEntry(**kwargs))

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = (setup_fn, handler_fn)


@pytest.fixture(scope="session")
def plugin_ctx():
    import margin
    ctx = FakeCtx()
    margin.register(ctx)
    return ctx


@pytest.fixture
def google():
    return FakeGoogle()


@pytest.fixture
def adapter(google, plugin_ctx):
    from unittest.mock import AsyncMock

    from gateway.config import PlatformConfig
    from margin.adapter import MarginAdapter
    from margin.store import DictState

    a = MarginAdapter(PlatformConfig(enabled=True, extra={}), state=DictState(),
                      client=GoogleClient(transport=google, sleep=lambda s: None))
    a.handle_message = AsyncMock()
    a._me = {k: v for k, v in ME.items() if k != "me"}
    a.store.watch(DOC_ID)
    return a
