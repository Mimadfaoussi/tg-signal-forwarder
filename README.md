# tg-signal-forwarder

Watches a Telegram channel you're a member of, detects complete trade signals, and
re-sends them **as your own Telegram user account** to a group or chat you can
write in — via a `listener` and a `publisher`, each logged in as its own session
on your account, connected by Redis Streams, with delivery history in Postgres.

See the technical requirements document (v1.1) for the full design. This file
covers setup from zero.

## Why two sessions, and why the target can't be a channel you're not admin of

Both `listener` and `publisher` act as **your Telegram user account** — not a
bot. That's deliberate:

- **listener** reads the source channel. Bots can't read channels they weren't
  explicitly added to as admins; your account, as a member, can.
- **publisher** sends into the target. In a broadcast **channel**, only admins
  can post — so if you're not an admin there, a bot posting on your behalf
  wouldn't work either. The target instead has to be a **group, supergroup,
  forum topic, or private chat** where ordinary members (including you) can
  send messages.

Each service gets its **own session file** (`listener.session` /
`publisher.session`) — two logged-in "devices" on the same account, matching
what you'd see under Telegram → Settings → Devices. They must never share a
session: using one auth key from two connections at once can get the session
killed (`AUTH_KEY_DUPLICATED`).

```
source channel --(your account, MTProto)--> listener --> Redis Streams --> publisher --(your account, MTProto)--> target chat
                                                                               |
                                                                               v
                                                                           Postgres (history)
```

## 1. Prerequisites

- A Linux VPS (or local machine) with Docker Engine 24+ and Docker Compose v2.
- A Telegram account that is already a **member** of the source channel.
- A target chat your account can already write in: a group/supergroup you're a
  member of, a forum topic, or a private chat (including with a bot you've
  already pressed **Start** on — see below).

## 2. Get Telegram API credentials

1. Go to <https://my.telegram.org>, log in with the phone number of the
   account you'll use for both sessions.
2. Open **API development tools** and create an application (any name/platform
   is fine).
3. Note the **App api_id** and **App api_hash** — these become `TG_API_ID` and
   `TG_API_HASH`. Both services share the same api_id/api_hash; they just log
   in with separate session files.

## 3. Pick your target

The target must be somewhere your account can already send messages:

- A **group or supergroup** you're a member of (not an announcement-only one
  you're muted/restricted in).
- A **forum topic** inside a supergroup (set `TARGET_TOPIC_ID` to the topic's
  id).
- A **private chat**, including with a bot — if you send signals to a bot, you
  must have already pressed **Start** on it (Telegram won't deliver messages
  to a bot that hasn't been started).

A broadcast **channel** only works if your account is an admin with post
rights there — if not, use one of the options above instead.

## 4. Configure

```bash
git clone <this repo>
cd tg-signal-forwarder
make init
```

`make init` copies `.env.example` to `.env` and creates `data/session/listener/`
and `data/session/publisher/` (mode 700). Edit `.env` and fill in at minimum:

- `TG_API_ID`, `TG_API_HASH` — from step 2.
- `TARGET_CHAT` — leave blank for now if you're not sure of the id; step 6
  below helps you find and verify it.
- `SOURCE_CHAT` — also fine to leave blank for now.
- `POSTGRES_PASSWORD` — change from the placeholder.

`.env` is never committed (it's git-ignored) and should be mode 600.

## 5. Build and log in

```bash
make build
make login
```

`make login` runs **two** interactive logins back to back — `login-listener`
then `login-publisher` — so you'll get two login codes from Telegram (this is
normal: it's the same account signing in as two separate sessions). Each
prompts for your phone number, the code, and your 2FA password if you have one
set. On success each prints the account and its 30 most recent chats as
`id<TAB>type<TAB>title<TAB>can_send` — use this to find `SOURCE_CHAT` (any
`type`) and confirm a candidate `TARGET_CHAT` shows `can_send yes`.

The publisher's login also runs the **target preflight** automatically once
`TARGET_CHAT` is set in `.env`, and prints whether the account can actually
send there before you go any further.

## 6. Set SOURCE_CHAT / TARGET_CHAT and start

Fill in `SOURCE_CHAT` and `TARGET_CHAT` in `.env` using the dialog listings
from step 5, then double-check the target:

`SOURCE_CHAT` accepts more than one channel — comma-separated, e.g.
`SOURCE_CHAT=-100111,-100222,@somechannel`. One listener watches all of them
at once (no extra sessions or containers needed); signals from every source
go through the same classifier, queue, and publisher, `CATCHUP_LIMIT` applies
per source, and each source keeps its own catch-up checkpoint.

```bash
make check-target
```

This runs only the target preflight and exits — useful any time you change
`TARGET_CHAT` without re-running the full login. Then:

```bash
make up
make ps
```

All four services (`redis`, `postgres`, `listener`, `publisher`) should show as
`healthy` within about a minute. If the publisher exits instead, check
`make logs s=publisher` — a `target_not_writable` line means the account can't
actually send to `TARGET_CHAT` (see §6.5 of the TRD for the exact reasons this
can fail, e.g. `broadcast_channel_requires_admin`, `restricted_from_sending`,
`blocked_by_account`).

## Everyday operations

| Command | What it does |
|---|---|
| `make ps` / `make logs` | Check status / tail logs |
| `make restart` | Restart the whole stack |
| `make check-target` | Re-run only the target preflight check |
| `make dry-run` | Start with `DRY_RUN=true` — publisher logs what it *would* send instead of actually sending (the preflight still runs) |
| `make stats` | Signal counts by status, plus stream lengths |
| `make replay-dead` | Move everything in `signals.dead` back to `signals.raw` for retry |
| `make backup` | `pg_dump` + copies of both session files, into `backups/` |
| `make db-shell` / `make redis-cli` | Open a shell into Postgres / Redis |

## Output format

`OUTPUT_MODE=copy` (default) does a real Telegram **forward** of the original
source message — not a re-authored copy. Formatting, media, and custom emoji
all come through exactly as posted, since it's the same message, just
delivered into the target chat. If the source channel has "Restrict Saving
Content" enabled, forwarding is blocked by Telegram entirely — that signal
dead-letters (see Troubleshooting) rather than falling back to anything else.
Note: forwarding doesn't support targeting a specific forum topic, so
`TARGET_TOPIC_ID` is ignored in this mode (a warning is logged once at
startup if both are set) — use `template` mode if you need that.

`OUTPUT_MODE=template` renders `services/publisher/templates/signal.txt.j2`
from the *parsed* signal fields instead (as HTML), composing a brand-new
message rather than forwarding — useful if you want a normalized look
regardless of how the source channel formats things, or if you need
`TARGET_TOPIC_ID` targeting.

## Account safety and rate limiting

Because sending happens from your own account, the publisher paces itself like
a careful human: at least `MIN_SEND_INTERVAL` seconds between sends (default
5, or the group's slow-mode delay if larger), random jitter on top
(`SEND_JITTER`, default 2s), and a hard cap of `MAX_SENDS_PER_HOUR` (default
30) — anything beyond that queues rather than drops. This state lives in Redis
so a restart doesn't reset it. The publisher's only write action, ever, is
`send_message` to `TARGET_CHAT` — nothing joins, leaves, forwards, reacts, or
messages any other chat.

## Trade-volume limits

Separate from the account-safety pacing above, two independent caps control
trade *volume* rather than send *rate* — set either to `0` to disable it:

- `MAX_TRADES_PER_DAY` (default 12) — resets at midnight in `TZ`. Once
  reached, further signals that day are skipped, not queued for tomorrow
  (a trade signal delayed a day is stale).
- `PAIR_COOLDOWN_HOURS` (default 24) — blocks forwarding another signal for
  a pair that was already forwarded within this window, even if that
  earlier trade already closed. This exists because the execution bot only
  blocks a duplicate while the *same* trade is still open — it happily
  accepts a fresh signal for a pair whose earlier trade already closed, so
  without this a busy day of signals can mean re-entering the same coin
  repeatedly.

A signal skipped by either check gets `status='skipped'` in Postgres with the
reason (`daily_cap` or `cooldown`) in `last_error` — visible via `make stats`
or `make db-shell`, not silently dropped.

## Development

```bash
make test              # unit tests, no Telegram credentials needed
make test-integration  # real Redis + Postgres, Telegram mocked
make lint
make fmt
```

`docker-compose.override.example.yml` shows how to mount source code for live
editing — copy it to `docker-compose.override.yml` to use it.

## Troubleshooting

- **Listener or publisher exits with `session_not_authorized` (code 2):** run
  `make login-listener` or `make login-publisher` respectively.
- **Listener exits with code 3:** one of the entries in `SOURCE_CHAT`
  couldn't be resolved — check the `source_chat_unresolvable` log line for
  which one, and make sure the account is still a member of it. With
  multiple sources, all of them must resolve for the listener to start.
- **Publisher exits with code 4 (`target_not_writable`):** the account can't
  send to `TARGET_CHAT` right now. Run `make check-target` for the specific
  reason, fix it (join the group, get unbanned, un-block the bot, switch off a
  broadcast channel to a group, etc.), then `make up` again.
- **A signal gets dead-lettered with a permanent error mid-run:** the
  publisher pauses sending for 10 minutes and automatically redoes the
  preflight check before resuming — you don't need to restart it manually,
  though fixing the underlying permission issue sooner means less delay. Once
  fixed, `make replay-dead` retries what fell through.
- **Every signal dead-letters with `ChatForwardsRestrictedError`:** the source
  channel has "Restrict Saving Content" enabled, which blocks forwarding via
  the API entirely — no amount of retrying fixes this. There's no workaround
  in `copy` mode; switch to `OUTPUT_MODE=template` if you need to keep
  forwarding signals from a protected channel (it composes a new message from
  the parsed fields instead of forwarding the original).
- **`AUTH_KEY_DUPLICATED` / session revoked:** something used the same session
  from two places at once, or you revoked it in Telegram → Settings →
  Devices. Re-run the affected service's login.
- **Nothing happens when a signal is posted:** check `make logs s=listener`
  for `message_skipped` (didn't match the classifier) vs. `signal_enqueued`
  (it should show up in the publisher next, subject to the rate limiter's
  pacing).

## Known limitations

- Edited or deleted source messages are ignored (v1 scope).
- One source channel, one target chat.
- No web UI, no exchange integration.
- `text_mention` formatting entities are rebuilt with only a user id, not a
  full Telegram `User` object (see `docs/DECISIONS.md`).
