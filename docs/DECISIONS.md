# Implementation decisions

Notes on places where the TRD was silent, ambiguous, or where testing surfaced a
real bug that changed the implementation from what was originally specified.

## v1.0 (Bot API publisher)

The sections below predate v1.1, which replaced the Bot API publisher with a
second Telethon user session (see "v1.1" further down). They're kept because
most of the reasoning — the `.env` comment bug, the Redis socket timeout race,
the `uv sync` venv bug, the config-error secret leak, the image size note —
still applies unchanged to the current code.

## Signal model fields are optional, with an explicit `parse_ok`

§6.2 shows `Signal` with fields like `pair: str`, `stop: Decimal` looking required,
but also says a message that fails to fully parse must still be enqueued "with the
fields that could be parsed plus `parse_ok=false`" rather than being dropped. Those
two statements only work together if the model's fields are optional. `pair`,
`base`, `quote`, `stop`, `stop_note` and `signal_date` are `| None`, `entries` and
`take_profits` default to `[]`, and a `parse_ok: bool` field was added (not shown
in the TRD's code block, but required by the prose in the same section).

## `.env.example` comments moved off the value line

`python-dotenv` does not strip an inline `# comment` when the value before it is
empty — `BOT_TOKEN=                 # from @BotFather` parses as
`BOT_TOKEN = "                # from @BotFather"`, not an empty string. This was
caught by actually running the stack: the bot's HTTP requests went to
`.../bot#%20from%20@BotFather.../sendMessage`. Every comment in `.env.example` now
sits on its own line above the variable instead of trailing it.

## `describe_config_error()` to avoid leaking secrets in validation errors

NFR-4 requires that a missing required variable be logged "without printing secret
values." pydantic's default `ValidationError.__str__()` includes an `input_value`
dump of the *entire* settings dict on every error — so if `TG_API_HASH` was set but
`SOURCE_CHAT` was missing, the raw exception text would print the hash anyway. All
three entry points (`listener.main`, `listener.login`, `publisher.main`) now format
config errors through `signal_shared.settings.describe_config_error()`, which only
emits `field (error_type)` pairs, never values.

## Redis client `socket_timeout` must exceed `XREADGROUP`'s `BLOCK`

`redis-py`'s default `socket_timeout` is 5 seconds — the same as the `BLOCK 5000`
used in the publisher's `XREADGROUP` call. When the stream is empty, the socket
read timeout and Redis's own block timeout race, and redis-py raises
`redis.exceptions.TimeoutError` instead of returning "no data," crashing the
publisher in a restart loop. Found by actually running the stack end to end.
Fixed by passing `socket_timeout=15.0` explicitly when constructing the publisher's
Redis client (`services/publisher/publisher/main.py`).

## Dockerfile: `uv sync` installs `shared/` directly, no separate install step

§10.1 describes "`uv sync --frozen --no-dev` into `/opt/venv`, then install
`shared/` into that venv" as two steps. Since each service's `pyproject.toml`
already lists `signal-shared` as a dependency with `tool.uv.sources` pointing at
`../../shared`, a single `uv sync --frozen --no-dev` installs it already — verified
by import-testing the built image. An earlier version that also ran
`VIRTUAL_ENV=/opt/venv uv pip install --no-deps /build/shared` was actively wrong:
`uv sync` was silently creating its own `.venv` instead of honoring `VIRTUAL_ENV`
(it obeys `UV_PROJECT_ENVIRONMENT`, not `VIRTUAL_ENV`), so the shipped `/opt/venv`
had none of the real dependencies (`telethon`, `httpx`, etc.) — only the package
installed by the second, `uv pip`-driven step. Caught by running
`python -c "import telethon"` against the built image.

## Test infrastructure built from the publisher image, not the listener's

The Makefile description for `test` says a container "built from the listener
builder stage." But `tests/unit/test_entities.py` imports `publisher.entities`
(entity conversion lives under `services/publisher/` per §8's layout), so a
listener-only build can't run the full unit suite. `make test` and
`make test-integration` both build from a dedicated `test` stage in
`services/publisher/Dockerfile` (the `builder` stage plus dev dependencies:
pytest, pytest-asyncio, fakeredis as of v1.1). `tests/` stays out of every image per §10.2's
`.dockerignore` rule and is bind-mounted at `docker run` time instead of copied in
at build time.

## Root-level `pyproject.toml` for tool configuration

Not in §8's layout, but `ruff check .` with zero configuration pulled in a much
broader rule set on the dev machine than the classic pyflakes/pycodestyle
defaults (including opinionated rules like blind-except and naive-datetime
checks unrelated to correctness here). Added `pyproject.toml` at the repo root
with `[tool.ruff]` pinned to `select = ["E", "F", "I", "UP", "B"]` and
`[tool.pytest.ini_options]` for `asyncio_mode = "strict"`, so `make lint` and
`make test` behave the same on any machine. This file is never copied into either
service's Docker build context.

## Chat id normalization via `telethon.utils.get_peer_id`

`signal_id`, the dedupe key, and the `-100xxxxxxxxxx` form operators put in
`SOURCE_CHAT` all need the same "marked" chat id. Rather than hand-rolling the
channel/group/user id conversion, the listener calls
`telethon.utils.get_peer_id(entity)`, which is Telethon's own implementation of
that exact conversion.

## Docker image size on the dev (arm64) machine exceeds the 200 MB target

`python:3.12-slim` itself measured ~214 MB (and the built listener/publisher
images ~236 MB / ~249 MB) when built locally on macOS/arm64 — over budget before
counting app code. This appears to be an arm64-specific Debian package size
difference; the official amd64 slim image is typically smaller. Not chased further
since the target runtime (§ Target runtime) is a Linux amd64 VPS, not this dev
machine. **Verify the built image size on the actual deployment architecture**
(`docker images` after `make build`) and slim further if it's still over 200 MB
there.

## `make clean` and `data/session`

§11 requires `clean` to "Never delete `data/session` without typing `yes`."
`docker compose down -v` only removes named volumes (`redis-data`, `pg-data`);
`data/session` is a host bind mount that compose never touches regardless, so the
requirement is satisfied by construction — the confirmation prompt guards the
volume wipe, and the session directory is simply never in the blast radius of this
target.

## v1.1 (operator's account replaces the Bot API)

The operator asked to remove the Bot API entirely: `publisher` now sends as
their own Telegram user account via a second Telethon session
(`publisher.session`), separate from the listener's, with account-safety rate
limiting. TARGET_CHAT is a bot's `@username` the operator has already started.

### Permission checks reuse Telethon's own `get_permissions`, not raw banned_rights

`shared/signal_shared/telegram.py`'s `describe_chat_permissions()` determines
whether the account can send to a target. For (super)groups it calls
`client.get_permissions(entity, "me")` — Telethon's own high-level helper,
which already merges a participant's individual `banned_rights` with the
chat's `default_banned_rights` into simple `is_admin` / `is_banned` /
`has_left` booleans — rather than re-deriving that from the raw TL objects by
hand. The one thing it doesn't expose is a plain "can send" flag for an
*ordinary* (non-admin, non-banned) participant, so that case falls back to
reading the chat entity's own `default_banned_rights.send_messages` directly.
Broadcast channels are handled separately (need `admin_rights.post_messages`
or `creator`), and private chats with a user via `GetFullUserRequest(...).blocked`.

### A bot user is a valid target, checked only for "blocked by the account"

The operator's target is a bot's `@username`, DM'd after pressing Start on it.
The TRD's own §6.5 preflight bullets only spell out "broadcast channel" and
"group/supergroup" explicitly, but §3's glossary already lists "private chat"
as a valid target kind, and the operator explicitly asked that a `User` entity
(bot or human) be treated as writable unless the operator's account has
blocked them, or the entity can't be resolved at all. `describe_chat_permissions()`
handles `User` entities (setting `chat_type` to `"bot"` when `entity.bot` is
true) by calling `GetFullUserRequest` and checking `full_user.blocked` — that
field reflects whether *this account* has blocked the target, which is what
"fail only if the account has blocked the bot" means. There's no supported way
to check the reverse (whether the bot/user has blocked *us*) from the API
ahead of time; that failure mode surfaces at send time as a `PeerIdInvalidError`
or similar, handled by the existing permanent-error path.

### Entity conversion direction reversed: dict → Telethon objects, via a generic registry

v1.0's `entities.py` converted Telethon dicts to Bot-API entity dicts. v1.1
needs the opposite: Telethon's own `MessageEntity*` TL objects, since
`client.send_message(..., formatting_entities=[...])` expects real TLObjects,
not dicts. Rather than hand-maintain a per-type mapping, `rebuild_entities()`
builds a registry of every `MessageEntity*` class from `telethon.tl.types` at
import time and instantiates directly from each raw dict's fields (minus the
`_` key naming the class) — verified empirically that `SomeEntity(**raw_dict_kwargs)`
round-trips correctly for Bold/Italic/TextUrl/CustomEmoji/etc., since Telethon's
own `.to_dict()` (used by the listener to build `raw_entities`) already emits
exactly those constructor kwargs.

### Rate limiter state uses a Redis sorted set for the rolling hourly window

§6.7 needs a `MAX_SENDS_PER_HOUR` cap where sends more than an hour old stop
counting, backed by Redis so a restart doesn't reset it. `ratelimit.py` uses a
sorted set (`publisher:ratelimit:sends`, timestamp as both score and member)
with `ZREMRANGEBYSCORE` to prune anything older than an hour before `ZCARD`
counts what's left — an accurate rolling window rather than a fixed-bucket
approximation, and naturally restart-safe since it's just Redis state, not
in-process counters. Verified with a frozen-clock unit test using `fakeredis`
(added as a publisher dev dependency) so the rolling-window logic is tested
without needing a real Redis server or wall-clock sleeps.

### `httpx`/`respx` removed; Telethon added to `shared/`

With the Bot API gone, `httpx` is no longer a publisher dependency (nothing
makes HTTP calls anymore), and `respx` (which only mocks `httpx`) is gone from
dev dependencies too — publisher tests now mock the Telethon client directly
with `unittest.mock.AsyncMock`. `telethon` moved into `shared/`'s own
dependencies because `signal_shared.telegram` (the client factory and
permission-checking logic, used by both services) and `signal_shared.login`
(the shared interactive-login routine from §6.4) both import it directly.

### mypy needs an explicit override for Telethon

Telethon ships no `py.typed` marker or stubs, so `mypy --strict shared/`
failed with `import-untyped` the moment `signal_shared.telegram` imported it.
Added `[[tool.mypy.overrides]]` for `module = "telethon.*"` with
`ignore_missing_imports = true` in the root `pyproject.toml`.

### Two session bind mounts, not one

§10.3 now mounts `./data/session/listener:/data/session` for the listener and
`./data/session/publisher:/data/session` for the publisher — each container
only ever sees its own session file on disk, which is what actually enforces
"these must never share a session" at the filesystem level rather than just by
convention. `make init` creates both directories at mode 700; `make backup`
copies both session files out at mode 600.

### `check-target` doesn't log in

`make check-target` runs `python -m publisher.sender` directly (its `__main__`
block), which connects with the *existing* session and calls `is_user_authorized()`
before running the preflight — it deliberately does not perform an interactive
login, since the Makefile description says "run only the target preflight
check," and an unattended `make check-target` shouldn't block waiting on a
phone code. If the session isn't authorized yet, it exits(2) with a hint to
run `make login-publisher` first, same as the main service would.

## v1.2 (operator request: transfer instead of copy)

After v1.1 shipped, the operator asked for `OUTPUT_MODE=copy` to actually
*transfer* (forward) the source message rather than re-authoring a new one
from `raw_text`/`raw_entities`. Confirmed with the operator that a source
channel with "Restrict Saving Content" enabled should dead-letter (permanent
error, no retry) rather than silently falling back to a copy-send.

### `copy` mode now does a real Telethon forward

`sender.send_signal()` calls `client.forward_messages(target, signal.source_message_id,
from_peer=signal.source_chat_id)` for `copy` mode instead of composing
`send_message(text, formatting_entities=...)`. `template` mode is unchanged —
it still composes and sends a new message, since there's no "original" to
forward once the content has been reduced to parsed fields. This made
`rebuild_entities()` and `ACCOUNT_IS_PREMIUM` (only ever used by the old
copy-mode path) dead code; both were removed along with `entities.py` and its
tests, rather than left unused.

### `ChatForwardsRestrictedError` joins `PERMANENT_ERRORS`

A source channel with "Restrict Saving Content" enabled makes Telegram reject
forwarding entirely at the API level (`ChatForwardsRestrictedError`) — no
retry schedule fixes that. Per the operator's explicit choice, this is treated
like the other permanent errors: dead-lettered immediately, no retry, 10-minute
pause before redoing preflight (§6.5) — not a silent fallback to composing a
new message instead.

### The publisher's session needs to warm its own entity cache before forwarding

`from_peer=signal.source_chat_id` only resolves if Telethon's *local* session
cache already knows that chat's `access_hash` — and the publisher's session
has never otherwise interacted with the source chat (only the listener's
session has). Confirmed by testing forwarding a numeric `TARGET_CHAT` id right
after a fresh login: `Cannot find any entity corresponding to "..."`, the same
failure mode as an uncached numeric `TARGET_CHAT`. Fixed by calling
`client.get_dialogs()` once at publisher startup (only when `OUTPUT_MODE=copy`)
before the main loop starts — the account is necessarily a member of the
source chat already, so this warms the cache for it (and everything else the
account is in) without needing `SOURCE_CHAT` duplicated into `PublisherSettings`.

### `TARGET_TOPIC_ID` isn't supported by `forward_messages` in `copy` mode

Telethon's high-level `forward_messages()` has no forum-topic-targeting
parameter (unlike `send_message`'s `reply_to`), so a forwarded message lands
in the target's default topic regardless of `TARGET_TOPIC_ID`. The publisher
logs a one-time `target_topic_id_ignored_in_copy_mode` warning at startup when
both are set together, rather than silently ignoring the setting. `template`
mode is unaffected — it still honors `TARGET_TOPIC_ID` via `reply_to`.

### Bug: `claim_loop`'s `xautoclaim` call used the wrong keyword argument

Found on the VPS, in production, roughly a minute after v1.2 went live:
`claim_loop_error` / `TypeError: StreamCommands.xautoclaim() got an
unexpected keyword argument 'start'`, repeating every 60s. `redis-py`'s
`Redis.xautoclaim()` names that parameter `start_id`, not `start` — a plain
typo that no test caught, because nothing exercised `claim_loop`'s
`xautoclaim` call at all: every prior test either mocked `redis.asyncio.Redis`
entirely (never touching the real method signature) or ran the stack for only
a few seconds, well under `CLAIM_INTERVAL_S` (60s). The bug was harmless by
design — it's caught and logged (`claim_loop_error`), so `main_loop` kept
processing new signals fine — but it silently disabled recovery of pending
entries from crashed consumers, undermining NFR-2 without anyone the wiser.

Fixed the typo and added `tests/unit/test_sender_errors.py::test_claim_loop_reclaims_and_sends_a_pending_entry`,
which runs `claim_loop` itself (against `fakeredis`, with `CLAIM_IDLE_MS` and
`asyncio.sleep` patched to run immediately) and asserts the pending entry
actually gets forwarded — confirmed this test fails with the original typo
and passes with the fix. Additionally re-verified the fix against a **real**
Redis server (not `fakeredis`'s reimplementation) before shipping, since
that's the gap that let the bug through in the first place: fakeredis or a
mock can silently diverge from the real library's actual parameter names.
