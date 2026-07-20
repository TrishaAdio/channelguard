# Group Logger Bot

An aiogram (Telegram Bot API) bot with a single **owner**. When added to groups
it logs to the owner, generates approval-required invite links, tracks join
requests in SQLite, and lets the owner approve/reject users and fetch links by
group name.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # set BOT_TOKEN and OWNER_ID
python bot.py
```

Add the bot to a group and promote it to **admin** with **"Invite users via
link"** and **"Ban users"** rights.

## Owner commands (bot DM, owner only)

| Command | Effect |
|---------|--------|
| `/groups` | list every group where the bot is admin, with short forms |
| `/pending` | list pending join requests |
| `/link <name>` or just sending `<name>` | approval invite links for admin groups matching `<name>` (e.g. `Lom`); `all` = every admin group; each link is revoked after one join |
| `/add <amount> <account> <keyword>` | approve the one pending request matching `<keyword>`, record the order, and **edit that request's log message** into `ADD_TEMPLATE` (with `{link}`) |
| `/remove <username\|id>` | decline the user's pending requests and kick them from every admin group; future requests auto-declined |

## Behaviour

- **Added / promoted / removed** in a group → owner gets a log with the short form and, when admin, an approval invite link.
- **Join request** (someone uses an approval link) → stored and forwarded to the owner.
- **Someone joins** through a `/link` link → that link is revoked (`REVOKE_AFTER_JOIN`).
- **Service messages** (`X joined` / `X left`) are deleted (`CLEAN_SERVICE`).

## Short form

`"Lom And Som Op"` → `"Lm"`. Each kept word becomes its first letter plus the
consonants of the rest (`Lom` → `Lm`), filler words (`and`, `the`, ...) are
skipped, and by default only the first meaningful word is used. Set
`SHORT_FORM_WORDS=2` to combine two words (`Lom Som` → `LmSm`).

## `ADD_TEMPLATE` placeholders

`{amount}` `{account}` `{keyword}` `{link}` `{order}` `{group}` `{short}`
`{user}` `{name}`. HTML formatting (including `<blockquote>`) is supported.

## `.env`

| Var | Meaning |
|-----|---------|
| `BOT_TOKEN` | bot token from @BotFather |
| `OWNER_ID` | your numeric Telegram user id |
| `DB_PATH` | SQLite path (default `data/bot.db`) |
| `SHORT_FORM_WORDS` | leading words folded into the short form (default 1) |
| `CLEAN_SERVICE` | delete join/leave service messages (default on) |
| `REVOKE_AFTER_JOIN` | revoke a `/link` invite after one join (default on) |
| `ORDER_PREFIX` / `ORDER_ID_LENGTH` | `{order}` id format (default `ANI` + 6) |
| `ADD_TEMPLATE` | message posted after `/add` |

## Behaviour notes

- **`/add`** approves only the matching user's request ("only his request
  accepts") and **edits the bot's join-request log message** into the template
  with `{link}`. A bot cannot edit your own `/add` message, so it edits the
  message it sent for that request. If several users match, it lists them
  instead of approving the wrong person.
- **`all`** (`/link all` or sending `all`) targets every group where the bot is
  admin.
- **`/remove`** kicks by ban-then-unban (removed but rejoinable) and auto-
  declines the user's future join requests. Username → id resolution works for
  users the bot has already seen (Bot API limitation on arbitrary usernames).

## Roadmap ("more features soon")

Editable per-group templates, payment stats/split, and richer link management
can be layered on the existing SQLite schema.
