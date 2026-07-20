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

With `GREET_NEW=1` it also acts as a **first-contact away responder**: when
someone starts a **brand-new** DM conversation (their first-ever message), it
sends the greeting only while your account is offline. Live Telegram presence
is checked, and any manual outgoing message from any logged-in session keeps the
account active for `ONLINE_MINUTES` (default 2). Existing chats are never greeted,
and each user is handled at most once (`data/greeted.json`).

Away replies can be enabled or disabled live from Saved Messages and the choice
is saved to `.env`; no restart is required.

Set the greeting from your own **Saved Messages** — no Premium needed:

| Saved Messages command | Effect                                  |
|------------------------|-----------------------------------------|
| reply to a post + `/set` | use that post as the greeting         |
| `/unset`               | clear the custom greeting               |
| `/show`                | show greeting and away-message status   |
| `/away on`             | enable first-contact away replies       |
| `/away off`            | disable first-contact away replies      |
| `/away status`         | show whether away replies are enabled   |
| `/help`                | list all Saved Messages commands        |
| reply + `/broadcast 9:30 AM 18 JUL THANKS FOR` | copy the replied post to every chat where you sent `THANKS FOR` from that IST time through the command time |

The `/broadcast` search uses Telegram history directly, so no local message
archive is needed. Matching is case-insensitive, only messages sent by your
account count, and each matching chat receives one copy. The start time is
always IST. Add a year when needed, for example
`/broadcast 9:30 AM 18 JUL 2026 THANKS FOR`; without a year, the current IST
calendar year is used. Future start times are rejected to prevent an accidental
wide broadcast. The command only works in your own Saved Messages and must
reply to the text/media post you want to broadcast.

The greeting post can be anything (text, media, markdown, premium emoji), and
its invite link is kept current on each rotation. If no custom greeting is set,
it falls back to the Business away message
(`business_away_message.shortcut_id`). Sending exactly uppercase `L` in any
private chat clears the full conversation for both sides and blocks that user.
`L` in Saved Messages does nothing.

> Business quick replies require **Telegram Premium** on that account.

### Payment logger (`quickreply.py`)

Send these yourself (outgoing). Unlike the greeting commands, the payment
commands work in **any** chat, not only Saved Messages. The payment logger does
**not** require Premium.

| Command | Effect |
|---------|--------|
| reply to an image + `/add <amount> [name]` | record the payment (INR), message the user in that private chat, and auto-post the image + caption to your channel |
| `/setdone <template>` | the message the paying user receives in the private chat |
| `/setchannelpostofpayment <template>` | the caption used for the channel post |
| `.setchannel` (typed in a channel) | set that channel as the post target |
| `/stats` | today's total (INR), payment count, and Rio/Marco split |
| reply to a payment post + `/cancel` (in the upload channel) | mark the post **FAKE PAYMENT** and exclude that payment from today's count, total, and split |
| `/clear` | reset today's stats to zero |
| `.ping` | verify that `quickreply.py` is running and receiving outgoing commands |
| `.help` | show every command and template parameter |

Template parameters (usable in both `/setdone` and `/setchannelpostofpayment`):
`{amount}` (this payment), `{name}`, `{orderid}` (a unique per-payment id like
`ANI7F3K9Q`, generated once and shared by the user message and channel post),
`{rioshare}`, `{marco}`, `{total}` (payments today), `{todaytotal}` (collected
today). The `{orderid}` prefix and suffix length are set by `ORDER_PREFIX` and
`ORDER_ID_LENGTH` in `.env`. Reply to a formatted post
with `/setdone` or `/setchannelpostofpayment` to keep bold, links, and premium
emoji verbatim. `pay.json` is validated and repaired at startup; malformed manual
edits are backed up as `data/pay.recovery-*.json` instead of preventing every
command from loading. Every new `/add` upload is linked to its payment record; replying
`/cancel` to that generated post marks it **FAKE PAYMENT** while retaining an
audit record, and excludes it from `/stats` and all later daily-total template
values. Posts generated before `/cancel` support cannot be matched retroactively.
The revenue split, timezone, and share basis are configured via
`RIO_PCT`, `MARCO_PCT`, `SHARE_BASE`, and `TZ` in `.env`.

## Files

| File            | Role                                                          |
|-----------------|---------------------------------------------------------------|
| `setup.py`      | Login + pick channel + owner, writes `.env`                   |
| `config.py`     | Loads `.env`, helpers                                         |
| `resolve.py`    | Channel resolver + interactive picker                         |
| `guard.py`      | Link rotation (DM owner) + auto-kick joiners                  |
| `quickreply.py` | 2nd userbot: keep `/demo` quick reply = the latest link       |
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
| `GREET_NEW`      | (quickreply) first-contact away replies (`1`/`0`)    |
| `ONLINE_MINUTES` | (quickreply) active window after manual sends (default 2) |
| `ORDER_PREFIX`   | (payment logger) `{orderid}` prefix (default `ANI`) |
| `ORDER_ID_LENGTH`| (payment logger) `{orderid}` random suffix length (default 6) |
| `RIO_PCT`        | (payment logger) Rio's split percent (default 55)   |
| `MARCO_PCT`      | (payment logger) Marco's split percent (default 45) |
| `SHARE_BASE`     | (payment logger) split base: `today` or `transaction` (default `today`) |
| `TZ`             | (payment logger) timezone for the "today" boundary (default `Asia/Kolkata`) |

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
