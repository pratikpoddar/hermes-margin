"""End-to-end through the real BasePlatformAdapter.handle_message: the gateway's reply lands in
the comment thread by editing Margin's status reply."""

import asyncio

from margin import logic
from tests.conftest import DOC_ID


async def test_real_handle_message_routes_reply_into_thread(adapter, google):
    from margin.adapter import MarginAdapter

    adapter.handle_message = MarginAdapter.handle_message.__get__(adapter)  # undo the fixture mock
    seen = {}

    async def agent(event):
        seen["key"] = adapter._event_session_key(event)
        seen["text"] = event.text
        return "Try: ```replace\npeople like it\n```"

    adapter.set_message_handler(agent)
    await adapter.poll_once()
    c = google.add_comment("@hermes fix grammar", quoted="people likes it")
    await adapter.poll_once()
    for _ in range(100):
        await asyncio.sleep(0.02)
        bot = google.bot_replies(c)
        if bot and "Proposed replacement" in bot[0]["content"]:
            break
    bot = google.bot_replies(c)
    assert len(bot) == 1, bot
    assert bot[0]["content"].startswith(f"{logic.BOT_MARKER}: Try:")
    assert "people like it" in bot[0]["content"]
    assert seen["key"].endswith(f"margin:group:{DOC_ID}:{c['id']}")
    assert "people likes it" in seen["text"]
