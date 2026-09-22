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

### Bug: the same signal got forwarded up to 5 times

Reported by the operator in production: one SAGA/USDT signal appeared
multiple times in the target chat. Postgres showed the row as `status='failed'`
with `attempts=5` and no `target_message_id` — meaning our own bookkeeping
never once considered it delivered, yet it visibly arrived in the chat
repeatedly. Root cause: `process_entry`'s retry loop calls `send_signal()`
(and therefore `client.forward_messages()` / `client.send_message()`) as a
fresh call on every attempt. Telegram's protocol has a client-supplied
`random_id` specifically so a retried request — e.g. one where the response
was lost on a flaky connection even though Telegram already delivered the
message server-side — can be recognized as a duplicate and return the
existing message instead of creating a new one. But Telethon's high-level
`forward_messages`/`send_message` generate a **new** random_id on every call,
so our outer retry loop (an independent, fresh call each time from Telethon's
perspective) got none of that protection: every "the response didn't come
back in time" retry created a genuinely new, real message, while our code —
having never received a successful response from *any* attempt — kept
retrying and eventually dead-lettered it as failed. Very plausible trigger:
the VPS's Docker daemon, network, and containers were all considerably
unstable on deploy day (see the git/snap/socket saga in the conversation this
was found in), exactly the kind of environment that produces "request
succeeded, response lost" connection drops mid-RPC.

Fixed by dropping to Telethon's raw API (`functions.messages.ForwardMessagesRequest`
/ `SendMessageRequest`) directly instead of the high-level wrappers, so an
explicit `random_id` can be supplied — deterministic per `(signal_id, mode)`
via a SHA-256-derived value (`_stable_random_id`), so every retry of the same
signal (within one `process_entry` call, or even across a crash/restart that
redelivers the same Redis entry) reuses the identical random_id. This makes
Telegram's own server-side dedup do the actual work, closing the gap
regardless of *why* a response got lost, rather than trying to more carefully
guess which exceptions are safe to retry.

This requires two Telethon internals that aren't part of the public API:
`client._parse_message_text(text, parse_mode)` (HTML → text + formatting
entities, for `template` mode) and `client._get_response_message(request,
result, input_chat)` (extracts the resulting `Message` from the raw
`Updates`, matched by `random_id`) — both are exactly what Telethon's own
`send_message`/`forward_messages` call internally, so they're not fragile
reverse-engineering, but they are unversioned/private and could change in a
future Telethon release without notice. Verified the whole mechanism two
ways before shipping: a unit test that captures the `random_id` seen across
all `MAX_ATTEMPTS` retries and asserts they're identical (and correctly
equal to `_stable_random_id(...)`), and a standalone script exercising
`_parse_message_text` and `_get_response_message` against a **real**
`TelegramClient` instance (not a mock) with a hand-built `Updates` response,
confirming both the correct-random_id and mismatched-random_id cases resolve
exactly as expected against Telethon's actual internal matching logic.

## Multiple source channels

The operator asked to watch a second source channel. §16's "future work" list
named this explicitly as out of v1 scope ("more than one source or target,
with a routing config per source"), but flagged that "the design should make
this easy to add later" — and it was: every downstream piece (Redis queue,
dedupe keys, catch-up checkpoints, the `signals` table, the publisher) is
already keyed by `source_chat_id`, which comes from each individual message,
not a fixed setting. Only the listener itself assumed a single `SOURCE_CHAT`.

`SOURCE_CHAT` now accepts a comma-separated list, reusing the exact pattern
`QUOTE_ASSETS` already established (and a `split_csv()` helper extracted into
`shared/` so both settings classes, and their tests, share one implementation
rather than duplicating the comma-splitting logic). One listener process
watches all configured sources: `events.NewMessage(chats=entities)` accepts a
list of entities directly (confirmed via Telethon's own signature), so no
extra client connections, containers, or sessions are needed. Catch-up runs
once per source (sequentially, to avoid amplifying flood-wait risk), and the
live event handler now derives `chat_id` from `event.chat_id` per message
(confirmed via Telethon's source to return the exact same marked-id value as
`utils.get_peer_id()`) instead of a single closed-over constant, since events
can now arrive from any of the configured sources. If any configured source
fails to resolve at startup, the listener exits(3) rather than silently
starting with a partial source list.

No test exercises `listener/main.py`'s multi-source wiring directly: `make
test` runs from the publisher's Docker test stage (see the "Test
infrastructure" decision above), which doesn't have the `listener` package
available at all. The one genuinely new piece of *logic* (parsing the
comma-separated list) was extracted into the shared, already-tested
`split_csv()` helper specifically so it has coverage despite that constraint;
the wiring itself (multiple `get_entity` calls, looped catch-up, the event
filter) was verified by building the actual listener image and constructing
`ListenerSettings` with a multi-value `SOURCE_CHAT` inside the container.

## A second channel format: "PAIR:"/"T1:"/"SL:" instead of "#"/"TP1:"/"Stop:"

Adding a second source channel (Suhaib AlMashhadani) surfaced a signal format
the classifier didn't recognize: `PAIR: ARB/USDT` instead of `#ARB/USDT`,
`T1:`/`T2:` instead of `TP1:`/`TP2:`, and `SL:` instead of `Stop:`. Asked the
operator whether to support exactly this second known format or loosen the
patterns generally for whatever a future third channel might use; chose the
former — precise, low false-positive risk, at the cost of needing another
update if a third channel shows up with yet another style.

`_build_pair_pattern()` now matches `#SYMBOL/QUOTE` OR `\bPAIR\s*:\s*SYMBOL/QUOTE`;
the TP regex is `\bT(?:P)?\s*\d+\s*:\s*...` (matches `TP1:` via the literal
"TP" branch, `T1:` via "T" with the optional "P" absent); the stop regex is
`\b(?:Stop|SL)\s*:\s*...`. All three were checked against the *existing*
update fixtures (`update_entry_hit.txt`, `update_tp_hit.txt`) to confirm the
broadened patterns don't turn those into false positives — they still
correctly classify as non-signals, since `\b` word-boundaries mean "T1:"-like
patterns don't match inside words like "Time" or "GMT+3".

One parsing detail worth noting: this channel's stop line has *two*
parenthesised groups (`SL: 0.12685 (4h) (-5.34%)`, a duration then a signed
P&L). The stop/TP regexes only capture the first parenthesised group as
`stop_note`/`percent` — the second is simply left unmatched, not merged in or
dropped-with-a-warning. Verified explicitly in
`test_signal_arb_alternate_format` (`stop_note == "4h"`, not "4h) (-5.34").

## Trade-volume limits: daily cap and per-pair cooldown

With 3 source channels and no volume limit, the operator hit 32 forwarded
trades in one day (Sep 21), all correlated altcoin longs, including the same
pair (PHA) re-entered 4 times. The operator confirmed the execution bot
already blocks a *true* simultaneous duplicate (a second signal for a pair
while that pair's trade is still open) — so PHA×4 could only have happened
via the bot accepting a *fresh* PHA signal each time the *previous* PHA trade
had already closed. That reframes "avoid duplicates" as a same-day re-entry
cooldown, not a same-pair-while-open guard (already handled elsewhere) --
confirmed with the operator before building it, since it's a real behavior
change (a good new signal for a pair gets skipped if that pair already
traded earlier that day, even after the earlier trade closed).

Implemented as a new `publisher/tradelimits.py::TradeLimits`, deliberately
separate from `ratelimit.py::RateLimiter` (§6.7) -- they're independent
concerns (pacing vs. volume) with different natural units (rolling windows
vs. calendar day / pair identity), and conflating them would make either
harder to reason about. Two Redis-backed checks, either disabled by setting
it to `0`:

- **Daily cap** (`MAX_TRADES_PER_DAY`, default 12): a per-calendar-day
  counter key. "Calendar day" is `clock().date()`, where the default clock is
  `datetime.now()` -- deliberately relying on the container's `TZ` env var
  (already configured stack-wide) for local-time conversion rather than
  reimplementing timezone handling with `zoneinfo` in Python.
- **Pair cooldown** (`PAIR_COOLDOWN_HOURS`, default 24): a per-pair Redis key
  with a TTL, existence-checked. Blocks re-forwarding that pair until the TTL
  expires, regardless of whether the earlier trade closed.

A skip is a real, visible outcome, not a silent drop: `signals.status` gained
a fifth value, `'skipped'`, with the reason (`daily_cap` / `cooldown`) reused
from the existing `last_error` column rather than adding new columns per
reason -- consistent with how `record_failed` already stores free-text
reasons there, and leaves room for future skip reasons (e.g. a Phase-2
concurrent-open cap) without further schema changes.

Checks run before dry-run's branch (so `DRY_RUN=true` previews accurately
reflect what would be skipped) but only `record()` (marking the pair
cooling-down / incrementing the day's count) runs on the real send path --
matching how dry-run already skips `RateLimiter.record_send()` too, so
dry-run stays fully side-effect-free.

### This is a live-production schema change: a real migration, not just editing 001_init.sql

Earlier schema changes in this project (pre-launch) were made by directly
editing `001_init.sql`, safe at the time since `CREATE TABLE IF NOT EXISTS`
against an empty/nonexistent database picks up any edit. That stopped being
true once the stack went live on the VPS with real data: `IF NOT EXISTS`
is a no-op against a table that already exists, so editing 001's CHECK
constraint text would do nothing on the already-running database while still
affecting fresh installs -- a real correctness bug if it shipped that way.
Added `002_add_skipped_status.sql` instead: `ALTER TABLE ... DROP CONSTRAINT
IF EXISTS ... ADD CONSTRAINT ...` (idempotent -- safe to run every startup,
per §6.5.1). Verified three ways before shipping, not just reasoned about:
(1) the constraint's actual Postgres-assigned name (`signals_status_check`)
confirmed against a real container rather than assumed, (2) running the
migration file twice in a row to confirm idempotency, (3) simulating the
operator's exact upgrade path -- a table with only migration 001 applied,
then invoking the app's own `run_migrations()` (not just running the SQL
directly) to confirm it correctly reaches and applies 002.

## Concurrent-open-trades cap, driven by the execution bot's own status messages

The operator named this the most important of the three trade-volume limits.
Unlike the daily cap and cooldown (pure bookkeeping we already had the data
for), a *concurrent* cap needs to know when a trade actually closes -- which
nothing in this system observed before. The operator's execution bot happens
to post detailed status messages (fills, TP/SL hits, cancellations, failures)
back into the same chat it's forwarded into (`TARGET_CHAT`), which the
operator confirmed and provided real examples of. `publisher/positions.py`
watches that chat via a second Telethon `events.NewMessage` handler on the
publisher's existing client (no extra session, no extra container -- the
client is already connected and reading `TARGET_CHAT` is just another
subscription) and tracks open/closed state from it.

### A slot opens at forward-time, not at fill confirmation

Considered two options: open the slot only once the bot confirms a fill
("Entry N filled"), or open it the moment *we* forward the signal. Chose the
latter: a pending limit order still represents committed, earmarked risk,
and waiting for confirmation would let a burst of signals slip past the cap
before any fills (or non-fills) are confirmed -- exactly the kind of gap
that would undermine "control how many trades are open at once," the
operator's stated goal. This also means the cap logic never needed to parse
"New Signal Detected" / "Entry filled" messages at all -- out of scope for
this pass, revisit if/when per-channel P&L tracking needs them.

### Closing events, and what does *not* close a slot

Four message types release a slot (matched via `parse_close_event`, tested
against the operator's exact real message examples in
`tests/unit/test_positions.py`): a stop-loss hit, a TP-hit message that
*also* contains "All TPs filled" (the bot puts this in the same message as
the final TP, not a separate one), a manual cancellation, and a trade-failed
message (never really opened, but still needs its reserved slot released).
Two message types were deliberately tested to confirm they do *not* close a
slot, since both are easy to mis-match against a TP-hit-shaped regex: an
*intermediate* TP hit (e.g. TP1 of 5 -- position stays open) and the "🔁
stop moved" notification the bot sends alongside a TP hit (superficially
similar wording, no `✅`/`for X!` structure, purely informational).

### Pair-matching between "ADA/USDT" and "ADAUSDT"

Our own `Signal.pair` always has a slash; the execution bot's log messages
never do. `normalize_pair()` strips everything but alphanumerics and
uppercases before comparing, so `PositionTracker.open("ADA/USDT")` and
`.close("ADAUSDT")` correctly refer to the same slot.

### Storage: a Redis sorted set, reusing RateLimiter's existing pattern

`publisher:positions:open`, member=normalized pair, score=opened_at --
exactly `RateLimiter`'s rolling-hourly-window structure, reused here for a
different reason: Redis sets don't support a per-member TTL, but a sorted
set's score lets a periodic `ZREMRANGEBYSCORE` prune stale entries the same
way. That prune is the safety net for a slot whose closing message is missed
or doesn't match any recognized pattern (an unanticipated bot message
format, a crash mid-way) -- it auto-releases after 7 days rather than
permanently consuming a concurrent-trade slot forever.

### No schema migration needed

Reuses the `status='skipped'` value and `last_error`-as-reason convention
already added for the daily cap / cooldown (reason: `concurrent_cap`) --
exactly the extensibility that decision's rationale anticipated.
