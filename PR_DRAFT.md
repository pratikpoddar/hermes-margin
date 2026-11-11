# DRAFT: Add Margin to the plugin catalog

> Not submitted. Before opening: push the repo to `pratikpoddar/hermes-margin`, pin the release commit in
> `plugin-catalog/margin.yaml` (`sha`), replace `OWNER`, and run
> `hermes plugins validate . --install-deps` at that commit.

**Title:** `plugin-catalog: add margin (Google Docs/Slides/Sheets comment threads as a gateway platform)`

## What it does

Margin is a gateway platform plugin that turns Google Drive comment threads into Hermes chats.
Highlight text in a Doc, Slides deck or Sheet and comment `@hermes <request>`. Margin picks the
comment up, opens a Hermes session for that thread (`chat_id` = file id, `thread_id` = comment id),
and gives the agent the request, the highlighted text and the document text. Hermes's answer is
posted in the same thread by editing Margin's "reading the doc…" status reply in place. Follow-ups
in the thread continue the same session. When Hermes proposes new wording (a fenced `replace`
block), the user can reply `apply` and Margin replaces the highlighted text through Docs
(`documents.batchUpdate`) or Slides (`presentations.batchUpdate`) `replaceAllText` with
`matchCase`, only when the text occurs exactly once; otherwise it explains why nothing changed.

Repo: https://github.com/pratikpoddar/hermes-margin · MIT · author Tinkerer (Pratik Poddar)

## Hermes surfaces used

Note for reviewers: the manifest is `kind: standalone` although the plugin registers a gateway
platform. With `kind: platform`, Hermes adds every `requires_env` name to its own platform env
registry, and `plugin_oauth` then refuses `MARGIN_GOOGLE_CLIENT_ID/SECRET` as `client_id_env` /
`client_secret_env` ("a variable Hermes uses itself"). `hermes plugins validate` passes on a fresh
clone but fails once the plugin is installed, and `credentialed_provider_request` would no longer
find the `google` client at runtime. `ctx.register_platform` does not require the kind, and the
gateway loads and connects the platform normally (verified with `hermes status` and
`hermes gateway run`). Happy to switch back if there is a sanctioned way for a platform plugin to
own an `oauth:` client.

- `ctx.register_platform(...)`: `BasePlatformAdapter` subclass (`connect`, `disconnect`, `send`,
  `edit_message`, `get_chat_info`, `send_typing`), `build_source(chat_type="group", thread_id=...)`,
  `MessageEvent(..., allow_gateway_control=False, reply_expected=True)` and `handle_message`.
  Registry fields: `allowed_users_env`, `allow_all_env`, `env_enablement_fn`, `validate_config`,
  `platform_hint`, `max_message_length`; feature-detected `display_tier="minimal"`,
  `allowlist_suppresses_pairing`, `config_connected_fn`, `docs_url`.
- `oauth:` manifest entry (`id: google`, the user's own Desktop client) signed in with
  `hermes auth add margin:google` and used only through
  `hermes_cli.plugin_provider_requests.credentialed_provider_request`.
- `ctx.state` for the watch list, poll cursors, dedupe ids and per-thread state.
- `ctx.register_command("margin", ...)` (`/margin watch|unwatch|list|status`) and
  `ctx.register_cli_command("margin", ...)` (`hermes margin auth|watch|unwatch|list|status`).
- Public helpers only: `gateway.platforms.base`, `gateway.platforms.event`, `gateway.config`,
  `gateway.platforms._shared` (`extra_or_secret`, `get_scoped_secret`, `seed_extra_from_env`).
  No tools, hooks or middleware. No Python dependencies.

`hermes plugins validate` at the pinned commit: all checks pass, security scan `safe`, no warnings
(in-process only, as every platform adapter is).

## Disclosure (catalog rule 13)

- **Third-party network calls:** Google only: `www.googleapis.com` (Drive v3: about, files.get,
  files.export, comments.list, replies.create/update), `docs.googleapis.com` and
  `slides.googleapis.com` (batchUpdate on `apply`). All through `credentialed_provider_request`;
  the bearer is attached by Hermes for those declared hosts only.
- **Data sent to the model provider:** the text of each watched file (up to
  `MARGIN_MAX_DOC_CHARS`, default 60,000 characters), the highlighted text and the thread's
  comments go to the user's configured model at the start of each thread, and follow-up replies
  on later turns.
- **Writes to user content:** posts and edits its own comment replies; changes document text only
  on an explicit `apply` reply from an allowed commenter, only for a single-paragraph highlight
  that occurs exactly once.
- **Who can drive the agent:** by default only the signed-in Google account (Margin filters at
  intake and seeds the adapter allowlist with that account). `MARGIN_ALLOWED_USERS` adds
  principals; `MARGIN_ALLOW_ALL_USERS=true` opens it to every commenter on watched files. Margin
  turns can never resolve gateway slash commands (`allow_gateway_control=False`), and the
  document is framed as untrusted data in the prompt.
- **Long-running background work:** one polling loop inside the gateway process
  (default every 10 s per watched file, exponential backoff to 5 min on errors).
- **Stored credentials:** none by the plugin. The OAuth grant is stored and refreshed by Hermes in
  the profile's auth store (`hermes auth logout margin:google` removes it). The client id/secret
  live in the Hermes `.env` like any `requires_env` value.
- **Stored data:** Hermes plugin state under `<HERMES_HOME>/plugin-data/` (watch list, cursors,
  handled comment/reply ids, per-thread highlighted text and last proposal). No document text.
- **Not present:** shell commands, subprocesses, reads outside its own plugin state, telemetry,
  self-update, binaries.
- **Scopes:** `drive` (restricted scope: needed to read comments and post replies on existing
  user files), `documents`, `presentations`.

## Testing

- `pytest`: 24 tests with a fake Google transport (new `@hermes` comment → event with doc
  context; reply lands in the right thread; status edited in place; `apply` replaces text
  exactly once and refuses ambiguous text; dedupe across polls and restarts; self-replies and
  other commenters ignored; 429/5xx backoff with `Retry-After`; poll-loop backoff; one test
  through the real `BasePlatformAdapter.handle_message` path).
- Real Hermes (this repo's main at b50d007e): plugin installed under `HERMES_HOME/plugins/margin`,
  `hermes plugins enable margin`, `hermes margin watch|status|auth`, `hermes auth status margin:google`,
  `hermes status` (lists the platform) and `hermes gateway run` (adapter connects and, without a
  sign-in, parks with "Not signed in … Run `hermes auth add margin:google`").
- Manual run against a real Google account: _to do before submitting_.
