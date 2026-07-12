# Channel Guard (Telethon userbot)

Locks down a channel's access:

1. Every **`ROTATE_MINUTES`** (default 5) it **revokes** the channel's invite
   link, issues a **fresh** one, and **DMs it to the owner**.
2. Anyone who **joins** the channel is **kicked and immediately unbanned** —
   removed but free to rejoin later, never a lasting ban (owner and admins are
   exempt). On startup it also **clears every existing ban** in the channel.

So a leaked link dies within minutes, nobody who slips in stays, and your
channel never accumulates a banned-users list.

## Setup

```bash
pip install -r requirements.txt
python setup.py
```

`setup.py` logs in your account (phone -> OTP -> 2FA), lets you **pick the
channel from a list**, asks for the **owner** (username or id) who receives the
links, and writes `.env`.

> The logged-in account must be an **admin** of the channel with **"Invite
> users via link"** and **"Ban users"** rights.

## Run

```bash
python guard.py
```

Leave it running. The owner starts getting a fresh invite link every few
minutes, and joiners get removed.

## Quick-reply userbot (`quickreply.py`) — optional second account

Keeps your **Business quick reply** (default `/demo`) pointing at the **latest
invite link**. It swaps **only the link** inside your post and leaves everything
else exactly as-is — your text, markdown formatting, and premium (custom) emoji
are preserved (entity offsets are re-aligned automatically). Set up your
`/demo` post once (link + whatever text/emoji you want); the bot only rewrites
the link part on each rotation.

How it fits together:

```
guard.py  (account A)  --DMs the link-->  owner (account B)
quickreply.py (account B, the owner)  --sees the link-->  updates /demo
```

- Run `quickreply.py` on the **owner** account (the one that receives the
  guard's DMs and holds the quick reply). It's a **separate login** (its own
  `quickreply.session`), same `API_ID`/`API_HASH`.
- Set `LINK_SOURCE` to the **guard account** (username/id) so it only trusts the
  guard's messages. Leave blank to accept an invite link from any private chat.

```bash
python quickreply.py      # first run: logs in this account + asks LINK_SOURCE
```

On each new link it swaps the link inside the shortcut's messages in place
(`getQuickReplies` -> `getQuickReplyMessages` -> `editMessage` with
`quick_reply_shortcut_id`), preserving all other text and entities.

With `GREET_NEW=1` it also acts as a **first-contact auto-responder**: when
someone starts a **brand-new** DM conversation (their first-ever message), it
sends them your **greeting post**. Existing chats are never greeted, and each
user is greeted at most once (`data/greeted.json`).

Set the greeting from your own **Saved Messages** — no Premium needed:

| Saved Messages command | Effect                                  |
|------------------------|-----------------------------------------|
| reply to a post + `/set` | use that post as the greeting         |
| `/unset`               | clear the greeting                      |
| `/show`                | whether a greeting is set               |

The greeting post can be anything (text, media, markdown, premium emoji) and its
invite link is kept current on each rotation too. If no greeting is set, it
falls back to the Business away message (`business_away_message.shortcut_id`).

> Business quick replies require **Telegram Premium** on that account.

### Payment logger

`quickreply.py` also logs payments and auto-posts the proof image. **Reply to an
image** with `/add <amount> [name]` and it:

1. records the payment (amount in **INR**, with the name),
2. posts that image + a **templated caption** to your **post channel**, and
3. updates today's running total and payment count.

No Premium needed for this part. Commands (send them yourself — they only react
to **your own** messages):

| Command | Effect |
|---|---|
| `/add <amount> [name]` | reply to an image → log payment + post it |
| `/setdone <template>` | set the caption template (or reply to a post with `/setdone`) |
| `/setpostchannel [id]` | set the post channel (no argument = current chat) |
| `/stats` | today's total (₹), payment count, and Rio/Marco split |
| `/scan` | print your chats/channels in the terminal (to find a channel id) |
| `.help` | show every command and template parameter |

**Caption template parameters** (use inside `/setdone`):

| Parameter | Value |
|---|---|
| `{amount}` | this payment's amount, INR-formatted (e.g. `₹1,00,000`) |
| `{name}` | the name passed to `/add` |
| `{rioshare}` | Rio's share (`RIO_PCT`, default 55%), INR |
| `{marco}` | Marco's share (`MARCO_PCT`, default 45%), INR |
| `{total}` | number of payments received today |

`{rioshare}`/`{marco}` are a percentage of **today's total** by default
(`SHARE_BASE=today`); set `SHARE_BASE=transaction` to split each single payment
instead. "Today" is bounded by `TZ` (default `Asia/Kolkata`). State lives in
`data/pay.json`.

## Files

| File            | Role                                                          |
|-----------------|---------------------------------------------------------------|
| `setup.py`      | Login + pick channel + owner, writes `.env`                   |
| `config.py`     | Loads `.env`, helpers                                         |
| `resolve.py`    | Channel resolver + interactive picker                         |
| `guard.py`      | Link rotation (DM owner) + auto-kick joiners                  |
| `quickreply.py` | 2nd userbot: keep `/demo` link current + payment logger        |
| `ui.py`         | Colored terminal output (colorama, with no-color fallback)    |

The setup, channel picker, and runtime output are **colorized** via `colorama`
(auto-falls back to plain text if it isn't installed).

## `.env`

| Var              | Meaning                                             |
|------------------|-----------------------------------------------------|
| `CHANNEL`        | Channel to guard (@username / -100 id / link)       |
| `OWNER`          | Who receives the links (@username or user id)       |
| `ROTATE_MINUTES` | How often to revoke + reissue the link (default 5)  |
| `LINK_SOURCE`    | (quickreply) account that sends the link; blank = any |
| `SHORTCUT`       | (quickreply) quick reply name (default `demo`)      |
| `GREET_NEW`      | (quickreply) send away msg to first-time DMs (default 1) |
| `RIO_PCT` / `MARCO_PCT` | (payment) revenue split % (default 55 / 45)   |
| `SHARE_BASE`     | (payment) `today` or `transaction` split base (default today) |
| `TZ`             | (payment) timezone for "today" (default Asia/Kolkata) |
| `PAY_PARSE_MODE` | (payment) caption parse mode: `html` or `none`      |

## Notes

- The owner is resolved by `get_entity` — a **@username** always works. A bare
  user id works only if the account already shares a chat with the userbot
  (e.g. the owner is in the channel).
- Kicks are ban-then-unban, so removed users are **not** left banned and can
  rejoin (via a future link). Startup clears any pre-existing bans too.
- **Security sweep**: on startup and every `SWEEP_MINUTES` (default 5), all
  non-admin members are kicked — this catches anyone who joined while the guard
  was offline (the live handler only sees new joins). `SWEEP_MINUTES=0` runs the
  sweep only at startup.
- Admins can't be kicked (Telegram restriction); those attempts are ignored.
- `.env` and `*.session` are gitignored — never commit them.
