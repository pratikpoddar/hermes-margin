"""``/margin`` slash command and ``hermes margin`` CLI.

Both only edit Margin's watch list in the plugin's own state; the gateway adapter re-reads it on
every poll, so a newly watched file is picked up within one poll interval.
"""

from __future__ import annotations

from typing import Any, Callable

from . import logic
from .store import MarginStore

USAGE = (
    "Usage: /margin watch <doc url or id> | unwatch <doc url or id> | list | status\n"
    "Then comment \"@hermes <request>\" on highlighted text in the doc."
)
SIGN_IN = "hermes auth add margin:google"


def run_subcommand(store: MarginStore, raw_args: str, *, env_files: Callable[[], list[str]] = list) -> str:
    """Shared by the slash command and the CLI. Returns the text to show."""
    parts = (raw_args or "").strip().split(None, 1)
    action = parts[0].lower() if parts else "status"
    arg = parts[1].strip() if len(parts) > 1 else ""
    if action in ("help", "-h", "--help"):
        return USAGE
    if action in ("watch", "unwatch"):
        file_id = logic.parse_file_id(arg)
        if not file_id:
            return f"Give a Google Docs, Slides or Sheets URL or file id.\n{USAGE}"
        if action == "watch":
            added = store.watch(file_id)
            return (f"Watching {file_id}. Comment \"@hermes ...\" on highlighted text to start a thread."
                    if added else f"Already watching {file_id}.")
        removed = store.unwatch(file_id)
        return f"Stopped watching {file_id}." if removed else f"{file_id} was not being watched."
    if action in ("list", "ls"):
        ids = store.watched()
        extra = [i for i in env_files() if i not in ids]
        if not ids and not extra:
            return "Margin is not watching any files. Use /margin watch <doc url>."
        lines = [f"- {i}" for i in ids] + [f"- {i} (from MARGIN_WATCH_FILES)" for i in extra]
        return "Watched files:\n" + "\n".join(lines)
    if action == "status":
        files = store.files()
        ids = store.watched() + [i for i in env_files() if i not in store.watched()]
        threads = store.threads()
        lines = [f"Margin: {len(ids)} watched file(s), {len(threads)} comment thread(s) joined."]
        for file_id in ids:
            fs = files.get(file_id) or {}
            state = "error: " + fs["error"] if fs.get("error") else (
                f"last poll {fs['last_poll']}" if fs.get("last_poll") else "not polled yet (is the gateway running?)")
            lines.append(f"- {file_id}: {state}")
        lines.append(f"Google sign-in: `{SIGN_IN}` (check with `hermes auth status margin:google`).")
        return "\n".join(lines)
    return USAGE


def make_slash_handler(store_factory: Callable[[], MarginStore], env_files: Callable[[], list[str]]):
    def _handle(raw_args: str) -> str:
        return run_subcommand(store_factory(), raw_args, env_files=env_files)
    return _handle


def setup_cli(subparser: Any) -> None:
    subs = subparser.add_subparsers(dest="margin_command")
    subs.add_parser("auth", help="Show how to sign in to Google for Margin and check the sign-in")
    watch = subs.add_parser("watch", help="Watch a Doc/Slides/Sheet for @hermes comments")
    watch.add_argument("target", help="Google Docs/Slides/Sheets URL or file id")
    unwatch = subs.add_parser("unwatch", help="Stop watching a file")
    unwatch.add_argument("target", help="Google Docs/Slides/Sheets URL or file id")
    subs.add_parser("list", help="List watched files")
    subs.add_parser("status", help="Show poll status per watched file")


def make_cli_handler(store_factory: Callable[[], MarginStore], env_files: Callable[[], list[str]],
                     check_sign_in: Callable[[], str]):
    def _handle(args: Any) -> int:
        command = getattr(args, "margin_command", None)
        if command == "auth":
            print(auth_text(check_sign_in()))
            return 0
        if command in ("watch", "unwatch"):
            print(run_subcommand(store_factory(), f"{command} {args.target}", env_files=env_files))
            return 0
        if command in ("list", "status"):
            print(run_subcommand(store_factory(), command, env_files=env_files))
            return 0
        print("Usage: hermes margin {auth|watch <url>|unwatch <url>|list|status}")
        return 1
    return _handle


def auth_text(sign_in_state: str) -> str:
    return "\n".join([
        "Margin uses your own Google Cloud OAuth client, signed in and stored by Hermes:",
        "  1. Google Cloud console: enable the Drive, Docs and Slides APIs.",
        "  2. Create an OAuth client of type \"Desktop app\" and add yourself as a test user.",
        "  3. Put its id and secret in MARGIN_GOOGLE_CLIENT_ID / MARGIN_GOOGLE_CLIENT_SECRET",
        "     (hermes config, or the prompt at plugin install).",
        f"  4. Run: {SIGN_IN}",
        "",
        f"Current sign-in: {sign_in_state}",
    ])
