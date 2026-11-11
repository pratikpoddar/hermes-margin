"""Margin behaviour against a fake Google: no credentials, no network."""

from __future__ import annotations

import pytest

from margin import logic
from margin.commands import run_subcommand
from margin.google_client import GoogleAPIError, GoogleClient
from margin.store import DictState, MarginStore
from tests.conftest import DOC_ID, OTHER



async def _baseline(adapter):
    await adapter.poll_once()  # first poll only records what already exists


def _event(adapter, n=0):
    return adapter.handle_message.await_args_list[n].args[0]


async def test_existing_comments_are_baselined_not_answered(adapter, google):
    google.add_comment("@hermes old request", quoted="Ship in Q3.")
    await _baseline(adapter)
    adapter.handle_message.assert_not_awaited()


async def test_new_hermes_comment_injects_message_with_doc_context(adapter, google):
    await _baseline(adapter)
    c = google.add_comment("@hermes make this sentence grammatical",
                           quoted="Our product is very good and people likes it.")
    await adapter.poll_once()
    adapter.handle_message.assert_awaited_once()
    event = _event(adapter)
    assert event.text.startswith("make this sentence grammatical")
    assert "people likes it" in event.text  # highlighted text
    assert "Ship in Q3." in event.text  # full document text
    assert "<<<DOCUMENT" in event.text and "\ufeff" not in event.text
    assert event.source.chat_id == DOC_ID
    assert event.source.thread_id == c["id"]
    assert event.source.chat_type == "group"
    assert event.source.user_id == "owner@example.com"
    assert event.allow_gateway_control is False
    # status reply posted and moved to "thinking"
    bot = google.bot_replies(c)
    assert len(bot) == 1 and bot[0]["content"] == f"{logic.BOT_MARKER} is thinking…"


async def test_comment_without_trigger_is_ignored(adapter, google):
    await _baseline(adapter)
    google.add_comment("Looks fine to me", quoted="Ship in Q3.")
    await adapter.poll_once()
    adapter.handle_message.assert_not_awaited()


async def test_reply_goes_to_right_thread_and_edits_status_in_place(adapter, google):
    await _baseline(adapter)
    google.add_comment("@hermes other thread", quoted="Launch plan")
    c = google.add_comment("@hermes tighten this", quoted="Ship in Q3.")
    await adapter.poll_once()
    event = next(e.args[0] for e in adapter.handle_message.await_args_list if e.args[0].source.thread_id == c["id"])
    status = google.bot_replies(c)[0]
    result = await adapter.send(DOC_ID, "**Shorter:** Ship Q3.", reply_to=event.message_id,
                                metadata={"thread_id": c["id"]})
    assert result.success
    assert result.message_id == f"{c['id']}:{status['id']}"
    bot = google.bot_replies(c)
    assert len(bot) == 1  # edited in place, not a second reply
    assert bot[0]["content"] == f"{logic.BOT_MARKER}: Shorter: Ship Q3."
    # a later message in the same thread is a new reply
    await adapter.send(DOC_ID, "One more thing.", metadata={"thread_id": c["id"]})
    assert len(google.bot_replies(c)) == 2
    # edit_message targets the reply by "<comment>:<reply>"
    edited = await adapter.edit_message(DOC_ID, result.message_id, "Edited answer")
    assert edited.success
    assert google.bot_replies(c)[0]["content"] == f"{logic.BOT_MARKER}: Edited answer"


async def test_follow_up_continues_same_session_without_doc_dump(adapter, google):
    await _baseline(adapter)
    c = google.add_comment("@hermes summarise", quoted="Ship in Q3.")
    await adapter.poll_once()
    await adapter.send(DOC_ID, "It says ship in Q3.", metadata={"thread_id": c["id"]})
    google.add_reply(c, "Can you make it more formal?")  # no @hermes needed in a joined thread
    await adapter.poll_once()
    assert adapter.handle_message.await_count == 2
    first, second = _event(adapter, 0), _event(adapter, 1)
    assert second.text == "Can you make it more formal?"
    assert adapter._source_session_key(first.source) == adapter._source_session_key(second.source)


async def test_apply_replaces_quoted_text_once(adapter, google):
    await _baseline(adapter)
    c = google.add_comment("@hermes fix grammar", quoted="people likes it")
    await adapter.poll_once()
    await adapter.send(DOC_ID, "Fixed:\n```replace\npeople like it\n```", metadata={"thread_id": c["id"]})
    answer = google.bot_replies(c)[0]["content"]
    assert "Proposed replacement:" in answer and "Reply \"apply\"" in answer and "```" not in answer
    google.add_reply(c, "apply")
    await adapter.poll_once()
    assert adapter.handle_message.await_count == 1  # "apply" is handled by Margin, not the agent
    assert "people like it." in google.text[DOC_ID]
    assert "likes" not in google.text[DOC_ID]
    batch = [call for call in google.calls if ":batchUpdate" in call[1]][0]
    assert batch[2]["requests"][0]["replaceAllText"]["containsText"] == {"text": "people likes it", "matchCase": True}
    assert google.bot_replies(c)[-1]["content"].startswith(f"{logic.BOT_MARKER}: Applied.")


async def test_apply_refuses_when_quoted_text_is_ambiguous(adapter, google):
    google.text[DOC_ID] = "Ship in Q3. Really, Ship in Q3."
    await _baseline(adapter)
    c = google.add_comment("@hermes reword", quoted="Ship in Q3.")
    await adapter.poll_once()
    await adapter.send(DOC_ID, "```replace\nLaunch in Q3.\n```", metadata={"thread_id": c["id"]})
    google.add_reply(c, "apply")
    await adapter.poll_once()
    assert google.text[DOC_ID] == "Ship in Q3. Really, Ship in Q3."
    assert not [call for call in google.calls if ":batchUpdate" in call[1]]
    assert "appears 2 times" in google.bot_replies(c)[-1]["content"]


async def test_apply_without_proposal_explains(adapter, google):
    await _baseline(adapter)
    c = google.add_comment("@hermes thoughts?", quoted="Ship in Q3.")
    await adapter.poll_once()
    await adapter.send(DOC_ID, "Looks fine.", metadata={"thread_id": c["id"]})
    google.add_reply(c, "apply")
    await adapter.poll_once()
    assert "no proposed replacement" in google.bot_replies(c)[-1]["content"]


async def test_dedupe_same_comment_is_dispatched_once(adapter, google):
    await _baseline(adapter)
    google.add_comment("@hermes once please", quoted="Ship in Q3.")
    await adapter.poll_once()
    await adapter.poll_once()
    await adapter.poll_once()
    assert adapter.handle_message.await_count == 1


async def test_dedupe_survives_restart(adapter, google):
    from gateway.config import PlatformConfig
    from margin.adapter import MarginAdapter
    from unittest.mock import AsyncMock

    await _baseline(adapter)
    google.add_comment("@hermes once please", quoted="Ship in Q3.")
    await adapter.poll_once()
    fresh = MarginAdapter(PlatformConfig(enabled=True, extra={}), state=adapter.store._state,
                          client=adapter.client)
    fresh.handle_message = AsyncMock()
    await fresh.poll_once()
    fresh.handle_message.assert_not_awaited()


async def test_self_authored_replies_are_ignored(adapter, google):
    await _baseline(adapter)
    c = google.add_comment("@hermes go", quoted="Ship in Q3.")
    await adapter.poll_once()
    await adapter.send(DOC_ID, "Answer that mentions @hermes", metadata={"thread_id": c["id"]})
    # a Margin-marked reply whose id we no longer remember (e.g. state lost) is ignored too
    google.add_reply(c, f"{logic.BOT_MARKER}: stray @hermes")
    await adapter.poll_once()
    assert adapter.handle_message.await_count == 1


async def test_other_commenters_are_ignored_by_default(adapter, google):
    await _baseline(adapter)
    google.add_comment("@hermes do my bidding", quoted="Ship in Q3.", author=OTHER)
    await adapter.poll_once()
    adapter.handle_message.assert_not_awaited()


async def test_resolved_comments_are_ignored(adapter, google):
    await _baseline(adapter)
    c = google.add_comment("@hermes too late", quoted="Ship in Q3.")
    c["resolved"] = True
    await adapter.poll_once()
    adapter.handle_message.assert_not_awaited()


async def test_backoff_retries_429_and_5xx(google):
    sleeps = []
    client = GoogleClient(transport=google, sleep=sleeps.append, base_delay=1.0)
    google.fail_next = [429, 503, 500]
    assert client.get_file(DOC_ID)["name"] == "Launch plan"
    assert len(sleeps) == 3
    assert 1.0 <= sleeps[0] < 1.2 and 2.0 <= sleeps[1] < 2.3 and 4.0 <= sleeps[2] < 4.5


async def test_backoff_gives_up_and_honours_retry_after(google):
    sleeps = []
    client = GoogleClient(transport=google, sleep=sleeps.append, max_retries=2)
    google.fail_next = [429, 429, 429]
    with pytest.raises(GoogleAPIError) as info:
        client.get_file(DOC_ID)
    assert info.value.status == 429 and len(sleeps) == 2

    from margin.google_client import Response
    sleeps.clear()
    calls = iter([Response(429, {"Retry-After": "7"}, b"{}"), Response(200, {}, b'{"ok": true}')])
    client = GoogleClient(transport=lambda m, u, b=None: next(calls), sleep=sleeps.append)
    assert client.request("GET", "https://www.googleapis.com/drive/v3/about") == {"ok": True}
    assert sleeps == [7.0]


async def test_non_retryable_error_is_raised_immediately(google):
    sleeps = []
    client = GoogleClient(transport=google, sleep=sleeps.append)
    google.fail_next = [403]
    with pytest.raises(GoogleAPIError):
        client.get_file(DOC_ID)
    assert sleeps == []


async def test_poll_loop_backs_off_on_failures(adapter, google):
    adapter.store.watch("missingFileId123456")
    errors = await adapter.poll_once()
    assert errors == 1
    assert adapter.store.file_state("missingFileId123456")["error"]


def test_commands_watch_list_unwatch():
    store = MarginStore(DictState())
    url = f"https://docs.google.com/document/d/{DOC_ID}/edit#heading=h.1"
    assert "Watching" in run_subcommand(store, f"watch {url}")
    assert store.watched() == [DOC_ID]
    assert "Already" in run_subcommand(store, f"watch {DOC_ID}")
    assert DOC_ID in run_subcommand(store, "list")
    assert "1 watched file" in run_subcommand(store, "status")
    assert "Stopped" in run_subcommand(store, f"unwatch {url}")
    assert store.watched() == []
    assert "Usage" in run_subcommand(store, "bogus")


def test_logic_helpers():
    assert logic.parse_file_id("https://docs.google.com/presentation/d/1AbCdEfGhIjKlMnOp/edit") == "1AbCdEfGhIjKlMnOp"
    assert logic.parse_file_id("not a url") is None
    assert logic.has_trigger("hey @hermes fix", "@hermes")
    assert not logic.has_trigger("email me at x@hermes.dev", "@hermes")
    assert logic.is_apply("Apply", "@hermes") and logic.is_apply("@hermes apply", "@hermes")
    assert not logic.is_apply("apply this to the intro please", "@hermes")
    assert logic.plain("people&#39;s &amp; things") == "people's & things"
    assert logic.extract_proposal("x\n```replace\nnew words\n```\ny") == "new words"


async def test_poll_loop_backoff_doubles_and_resets(adapter, monkeypatch):
    import margin.adapter as mod

    results = iter([1, 1, 1, 0])
    delays = []

    async def fake_poll():
        return next(results)

    async def fake_sleep(delay):
        delays.append(delay)
        if len(delays) == 4:
            adapter._running = False

    adapter.poll_interval = adapter._poll_delay = 10.0
    adapter.poll_once = fake_poll
    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    adapter._running = True
    await adapter._poll_loop()
    assert delays == [20.0, 40.0, 80.0, 10.0]


async def test_slides_apply_uses_presentations_batch_update(adapter, google):
    from tests.conftest import DOC_ID as _  # noqa: F401
    slides_id = "1SlidesDeckIdAbcdefghijk"
    google.files[slides_id] = {"id": slides_id, "name": "Deck",
                               "mimeType": "application/vnd.google-apps.presentation", "webViewLink": ""}
    google.text[slides_id] = "Title slide\nRevenue grew fast\n"
    google.comments[slides_id] = []
    adapter.store.watch(slides_id)
    await adapter.poll_once()
    c = google.add_comment("@hermes add a number", quoted="Revenue grew fast", file_id=slides_id)
    await adapter.poll_once()
    await adapter.send(slides_id, "```replace\nRevenue grew 40%\n```", metadata={"thread_id": c["id"]})
    google.add_reply(c, "apply")
    await adapter.poll_once()
    assert any("slides.googleapis.com/v1/presentations/" in call[1] for call in google.calls)


def test_register_wires_platform_command_and_cli(plugin_ctx):
    entry = plugin_ctx.platforms["margin"]
    assert entry["allowed_users_env"] == "MARGIN_ALLOWED_USERS"
    assert "```replace" in entry["platform_hint"]
    assert "margin" in plugin_ctx.commands and "margin" in plugin_ctx.cli


def test_cli_argparse_tree(plugin_ctx, capsys):
    import argparse

    setup_fn, handler = plugin_ctx.cli["margin"]
    parser = argparse.ArgumentParser(prog="hermes margin")
    setup_fn(parser)
    args = parser.parse_args(["watch", f"https://docs.google.com/document/d/{DOC_ID}/edit"])
    assert handler(args) == 0
    assert "Watching" in capsys.readouterr().out
    assert handler(parser.parse_args(["list"])) == 0
    assert DOC_ID in capsys.readouterr().out
    assert plugin_ctx.commands["margin"]("status").startswith("Margin: 1 watched file")


async def test_apply_multi_paragraph_in_docs_uses_index_range(adapter, google):
    google.text[DOC_ID] = "Title 🚀\nRoll it thin.\nUse a bottle.\nBake 10 min.\n"
    await _baseline(adapter)
    c = google.add_comment("@hermes merge these", quoted="Roll it thin.\nUse a bottle.")
    await adapter.poll_once()
    await adapter.send(DOC_ID, "```replace\nNo rolling pin? Roll it thin with a bottle.\n```",
                       metadata={"thread_id": c["id"]})
    google.add_reply(c, "apply")
    await adapter.poll_once()
    assert google.text[DOC_ID] == "Title 🚀\nNo rolling pin? Roll it thin with a bottle.\nBake 10 min.\n"
    batch = [call for call in google.calls if ":batchUpdate" in call[1]][0]
    assert batch[2]["requests"][0]["deleteContentRange"]["range"] == {"startIndex": 10, "endIndex": 37}
    assert "Applied." in google.bot_replies(c)[-1]["content"]


async def test_apply_multi_paragraph_refuses_when_ambiguous(adapter, google):
    google.text[DOC_ID] = "A\nB\nA\nB\n"
    await _baseline(adapter)
    c = google.add_comment("@hermes reword", quoted="A\nB")
    await adapter.poll_once()
    await adapter.send(DOC_ID, "```replace\nC\n```", metadata={"thread_id": c["id"]})
    google.add_reply(c, "apply")
    await adapter.poll_once()
    assert google.text[DOC_ID] == "A\nB\nA\nB\n"
    assert "appears 2 times" in google.bot_replies(c)[-1]["content"]


def test_find_unique_range_counts_utf16_units():
    from margin import logic as lg
    doc = {"body": {"content": [{"paragraph": {"elements": [
        {"startIndex": 1, "textRun": {"content": "😀ab\n"}}]}}, {"paragraph": {"elements": [
        {"startIndex": 6, "textRun": {"content": "cd\u000bef\n"}}]}}]}}
    assert lg.find_unique_range(doc, "b\ncd\nef") == (1, (4, 11))
