# Channel Guard

This repo now ships **two** independent tools:

- **ChannelGuard admin bot** (`bot/`) — a BotFather **bot-token** bot
  (aiogram + SQLite). Add it as an admin to any group/channel and it onboards
  itself: derives a short code from the title, mints an approval-required
  invite link, stores everything, and DMs the owner. The owner drives link
  distribution, join-request approval, templates, and member removal from the
  bot's DM. **Start here if you want a bot you add as admin.** See
  [`bot/README`](#channelguard-admin-bot-bot) below.
- **Channel Guard userbot** (repo root) — the original Telethon **userbot**
  (logs in as your account). Documented directly after.

---

## ChannelGuard admin bot (`bot/`)

A single-owner Telegram **bot** (bot token, not a login). What it does:

1. **Auto-onboard.** The moment you promote it to admin in a group/channel it
   derives a **short code** from the title (`Lom And Som Op` -> `Lm`), mints an
   **approval-required** ("join request") invite link, saves it to SQLite, and
   **DMs the owner** a compact card (title, short code, id, type, member count,
   link).
2. **Paid orders (single-use links).** `/add <amount> <account> <keyword>`
   mints a **single-use** invite link (`member_limit=1`) for each matched group
   — only one buyer can use it. Each `/add` gets an **order id** (`ANI0001`,
   `ANI0002`, ...) and the post is sent to you and (optionally) to a
   **payment channel**. Invite URLs are delivered only in the owner's private
   chat; the payment channel receives metadata, group names/counts, and
   unavailable details without reusable invite URLs. When the buyer joins, the
   bot ties them to the order and **revokes the spent link** automatically.
   Need another seat for the same group? Just run `/add` again — each order is
   an independent link.
3. **`/revoke <orderid>`** kills every link in that order and removes any buyer
   who joined through it (kick + unban, so no permanent ban is left behind).
4. **`/remove <orderid>`** kills every link and permanently bans each buyer
   who joined a group through that order. Failed cleanup is retried from
   SQLite. To undo one, send `.unban <id>` from the **userbot**: it lifts the
   ban in every group that account admins, and the bot then cancels its
   journalled bans for that user so no retry re-applies them.
5. **Join enforcement.** Every join request (on the general approval
   link) is stored and forwarded with inline **Approve / Decline** buttons.
   General-link rotation after approval is optional (`ROTATE_ON_JOIN`). Direct joins without
   the matching approved request/order link, and use of a buyer-bound link by
   the wrong account, are permanently banned with durable retry state.
6. **Owner control from DM** (owner id only):

   | Command | Effect |
   |---------|--------|
   | `/add <amount> <account> <keyword>` | mint a single-use link per matched group + one order id; private chat gets URLs, payment channel gets a safe summary |
   | `/revoke <orderid>` | revoke links and remove joined buyers without a lasting ban |
   | `/remove <orderid>` | revoke links and permanently ban joined buyers in those groups |
   | `/orders` | list recent orders and their status |
   | `/tpl <keyword> [body]` | set the post format for a keyword (reply to a formatted message to keep its HTML) |
   | `/groups` | list registered groups + short codes + admin/link status |
   | `/list` | list saved templates |
   | `/pending` | list pending join requests with Approve/Decline |
   | `/remove <keyword \| @user \| id>` | also delete a template, **or** decline that user's requests and remove them from every group |
   | send a short code / name / `all` | reply with the approval-required link(s) |

   Template tokens: `{link} {title} {short} {amount} {name} {keyword} {orderid}`.
   Group lookup is font/case-insensitive and prioritizes human title words:
   `in`, `ind`, `indi`, or `indian` select titles containing a word beginning
   with `Indian`. Exact title-word matches can intentionally select multiple
   groups, while generated short codes never pollute those results. If no
   title word/prefix matches, typo fallback still resolves `Lolsia`/`lolsa` to
   `LOLsi` and rejects genuinely ambiguous typo matches.
7. **Clean service.** Join/leave system messages in groups are deleted so the
   chat stays clean (needs the Delete Messages right).

### Run

```bash
cd bot
pip install -r requirements.txt
cp .env.example .env      # fill in BOT_TOKEN + OWNER_ID
cd ..
python -m bot
```

The owner must press **Start** in the bot's DM once so it can message them. The
bot needs the **Invite Users**, **Delete Messages**, and **Ban Users** admin
rights in each group. `bot/.env` and `bot/data/` are gitignored.

---

# Channel Guard (Telethon userbot)

Locks down a channel's access:

1. Every **`ROTATE_MINUTES`** (default 5) it **revokes** the channel's invite
   link, issues a **fresh** one, and **DMs it to the owner**.
2. Anyone who **joins directly** is **permanently banned** (owner and admins
   are exempt). Existing bans are preserved across restarts.

So a leaked link dies within minutes, and an account that slips in cannot
immediately reuse another leaked link.

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
minutes, and direct joiners are permanently banned.

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
| reply to an image + `/add <amount> <name> [group ... \| all]` | record the payment (INR), privately deliver buyer-bound links, and auto-post the image + safe metadata caption to your channel |
| `/setdone <template>` | the message the paying user receives in the private chat |
| `/setchannelpostofpayment <template>` | the caption used for the channel post |
| `.setchannel` (typed in a channel) or `.setchannel <id \| @username \| t.me link>` (from any chat) | set the payment upload channel, persist it as `PAYMENT_CHANNEL` in `.env`, and re-queue every receipt that has not posted yet |
| `.unban <id \| @username>` (or reply to the user) | lift that user's ban in every group/channel this account admins, and tell the admin bot to drop its durable ban records so nothing re-bans them |
| `/stats` | today's total (INR), payment count, and Rio/Marco split |
| reply to a payment post + `/cancel` (in the upload channel) | mark the post **FAKE PAYMENT**, exclude it from stats, revoke every buyer-bound link, and remove a buyer who already joined |
| `/clear` | reset today's stats to zero |
| `.setproofs` (typed in a channel) or `.setproofs <id \| @username \| t.me link>` (from any chat) | set the channel `.proof` posts to, and persist it as `PROOF_CHANNEL` in `.env` |
| `.proof` | capture a full screenful of this chat as a macOS-style window screenshot and post it to the proof channel |
| `.proof <n>` | exactly the latest `n` messages, uncropped (max 40) |
| reply + `.proof` | the capture ends at the replied message, with its context above it |
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
command from loading. If `data/pay.json` is missing after a clean deployment,
quickreply restores the upload target from `PAYMENT_CHANNEL` in `.env`.
Every new `/add` upload is linked to its payment record; replying
`/cancel` to that generated post marks it **FAKE PAYMENT** while retaining an
audit record, and excludes it from `/stats` and all later daily-total template
values. Posts generated before `/cancel` support cannot be matched retroactively.

**Where receipts are delivered.** The upload target has one meaning at a time:
`PAYMENT_CHANNEL` in `.env` is the declared parameter and `data/pay.json` holds
the live value. `.setchannel` writes both, and on startup a parameter that was
edited by hand wins over `pay.json` — so the channel a `.ping` reports is always
the channel receipts are sent to. Deliveries always target the current channel,
never the one that was configured when the payment was recorded, so moving the
channel also moves everything still queued or retrying (both the old and the new
chat are checked first, so a receipt that already landed is never duplicated).
The only case where `.env` does not win is a `.setchannel` whose `.env` write
failed: it warns, keeps the new target for this data folder, and `.ping` then
shows both values. The admin bot re-reads `PAYMENT_CHANNEL` for every order
summary it posts, so `.setchannel` moves both halves of the system at once
without a restart (`.env` is replaced atomically, so a concurrent read never
sees a half-written file).
Buyer-bound invite URLs are intentionally private: even when `/add ... all`
creates many links, the payment channel receives only the delivery count/group
names and unavailable details. Failed/ambiguous channel uploads are reconciled
by order id and retried from `pay.json`.
The revenue split, timezone, and share basis are configured via
`RIO_PCT`, `MARCO_PCT`, `SHARE_BASE`, and `TZ` in `.env`.

### Proof screenshots (`chatshot/`)

`.proof` renders real messages from the chat you run it in as a **macOS-style
Telegram screenshot** and posts the PNG to the channel set by `.setproofs`.

![Example .proof output](docs/example.png)

*Actual renderer output ([dark theme](docs/example-dark.png)). Regenerate both
with `python3 -m chatshot.make_example`.*

Telethon cannot take a screenshot — it is a protocol client with no UI to
capture. So `chatshot` reads the messages through Telethon, rebuilds them as
HTML/CSS, and photographs that with headless Chromium at a 3x device pixel
ratio. Text shaping, wrapping and emoji fallback are done by the browser, which
is what makes the output crisp rather than hand-drawn.

```
.proof            one full screenful of this chat  (the normal case)
.proof 5          exactly the newest 5 messages, uncropped
reply + .proof    a screenful ending at the replied message
```

Bare `.proof` renders **a page, not a message**: a fixed 900x620 landscape pane
with the newest message at the bottom, older ones filling upward, and whatever
does not fit clipped at the top edge — which is what a screenshot of a scrolled
chat window actually looks like. `.proof <n>` instead sizes the image to exactly
those `n` messages, so nothing you asked for gets cropped away.

The `.proof` command message is always excluded from its own screenshot.

What it reproduces: sender grouping with the tail on the last bubble of a run,
Telegram's per-peer name/avatar colours, real profile photos, reply and forward
headers, date separators, sent/read ticks, the timestamp tucked into the last
line of the bubble, light and dark themes, and every entity type — bold,
italic, underline, strike, spoiler, code, pre with its language label,
blockquote, links, mentions and custom emoji. Photos, stickers, videos,
documents and voice notes render too, with voice waveforms decoded from
Telegram's actual 5-bit amplitude data rather than faked.

**Setup.** `.proof` is the only feature needing a browser; everything else runs
without it.

```bash
python3 -m pip install -r requirements.txt
python3 -m chatshot.setup_assets    # Chromium + fonts, then self-verifies
```

`setup_assets` installs Inter, Noto Color Emoji, JetBrains Mono and Noto Sans
Devanagari — all SIL Open Font License. The colour emoji font matters: without
it Chromium draws emoji as monochrome boxes. Apple's **SF Pro** is the genuine
macOS typeface but its licence forbids redistribution, so it is never
downloaded; if you have it locally, drop the `.otf` files into
`~/.local/share/fonts/chatshot/` and set `RenderOptions.font` to
`"SF Pro Text"`.

Rendering is separated from fetching, so the look can be changed and tested
from fixtures with no Telegram account:

```bash
python3 -c "from chatshot.fixtures import sample_conversation as s; \
from chatshot.render import render_png_sync as r; \
open('out.png','wb').write(r(s('dark')))"
```

`RenderOptions` covers `theme`, `width`, `scale`, `window_chrome`,
`wallpaper_preset`, `clock24`, and privacy switches `hide_sender_names` /
`hide_avatars` for posting a proof without exposing the buyer's identity.

## Files

| File            | Role                                                          |
|-----------------|---------------------------------------------------------------|
| `setup.py`      | Login + pick channel + owner, writes `.env`                   |
| `config.py`     | Loads `.env`, helpers                                         |
| `resolve.py`    | Channel resolver + interactive picker                         |
| `guard.py`      | Link rotation (DM owner) + auto-kick joiners                  |
| `quickreply.py` | 2nd userbot: keep `/demo` quick reply = the latest link       |
| `chatshot/`     | `.proof` renderer: Telegram messages -> macOS-style PNG       |
| `ui.py`         | Colored terminal output (colorama, with no-color fallback)    |
| `infra.py`      | Supervises guard, quick-reply, and admin-bot processes         |

The setup, channel picker, and runtime output are **colorized** via `colorama`
(auto-falls back to plain text if it isn't installed).

## Run all three services

Prepare both dependency sets and complete the first-login/configuration steps
before using the combined launcher:

```bash
python3 -m pip install -r requirements.txt
python3 -m pip install -r bot/requirements.txt
python3 setup.py
cp bot/.env.example bot/.env   # fill BOT_TOKEN and OWNER_ID
python3 quickreply.py           # first login for the second userbot; then stop it
```

Then launch the complete stack from the repository root:

```bash
python3 infra.py
```

`infra.py` starts exactly `guard.py`, `quickreply.py`, and `python -m bot`,
prefixes each service's output with a distinct color, restarts unexpected exits
with bounded backoff, and shuts all three down on Ctrl+C or SIGTERM. A
host-wide lock prevents two infrastructure launchers from running at once.

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
| `PAYMENT_CHANNEL`| (payment logger/admin bot) numeric target channel id; authoritative — editing it moves the upload target on the next start |
| `PROOF_CHANNEL`  | (`.proof`) numeric channel id for rendered screenshots; same authoritative precedence as `PAYMENT_CHANNEL` |
| `RIO_PCT`        | (payment logger) Rio's split percent (default 55)   |
| `MARCO_PCT`      | (payment logger) Marco's split percent (default 45) |
| `SHARE_BASE`     | (payment logger) split base: `today` or `transaction` (default `today`) |
| `TZ`             | (payment logger) timezone for the "today" boundary (default `Asia/Kolkata`) |

## Notes

- The owner is resolved by `get_entity` — a **@username** always works. A bare
  user id works only if the account already shares a chat with the userbot
  (e.g. the owner is in the channel).
- **Security sweep**: on startup and every `SWEEP_MINUTES` (default 5), all
  non-admin members are permanently banned — this catches anyone who joined
  while the guard was offline. `SWEEP_MINUTES=0` runs the sweep only at
  startup.
- Admins cannot be banned by another admin (Telegram restriction); those
  attempts are ignored.
- `.env` and `*.session` are gitignored — never commit them.
