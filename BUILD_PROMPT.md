# ChannelGuard — Full Rebuild Specification

This document is a complete, self-contained build prompt. Give it to a fresh
implementation. It describes **two programs** (an admin **bot** and a
**userbot**) plus an existing **guard** that must be left untouched, how they
talk to each other, every command, the data model, the templating rules
(`{link}` and `{links}`), and every edge case that has caused problems before.

Build it correctly the first time by following this exactly.

---

## 1. The big picture (read this first)

There are **three separate processes**. They must all run from the **same
folder** so they can share a `data/` directory on disk.

| Process | Account | Job |
|---|---|---|
| **bot** (`python -m bot`) | a **Bot API** account (BotFather token) | Owns groups. It is added as **admin** in groups. It **searches groups**, **creates invite links**, **approves/declines join requests**, and **revokes links**. |
| **userbot** (`quickreply.py`) | the **owner's personal** Telegram account (Telethon) | Talks to paying customers. Runs `/add`, logs payments, posts to a payment channel, sends the "thanks for paying" message with the link(s), quick replies, greeting, broadcast. **It never searches groups.** |
| **guard** (`guard.py`) | a second personal account (Telethon) | Rotates a demo channel's invite link and kicks non-members. **Already works. Do not change its behaviour.** |

### The one rule that keeps breaking

- **The BOT searches groups and makes links. The USERBOT never touches groups.**
  The userbot only asks the bot "give me link(s) for keyword X, reserved for
  user Y" and pastes whatever the bot returns into a template.
- **The guard's demo link and the bot's payment links are completely separate.**
  The bot's payment links must **never** overwrite the userbot's `/demo`
  Business quick reply. Only the guard updates `/demo`.

---

## 2. The core flow (what the owner actually does)

1. Owner adds the **bot** as admin to a group. Bot DMs the owner: "Added to
   *GroupTitle*, short code `GT`, link ready." Bot stores the group.
2. A customer pays the owner and DMs the owner's personal account.
3. Owner (in the **customer's DM**, on the userbot) types:
   ```
   /add 252 Nana cp
   ```
   - `252` = amount, `Nana` = account name, `cp` = group keyword.
4. The userbot sends **one request** to the bot: *"reserve a link for keyword
   `cp`, bound to this customer's user id."*
5. The **bot**:
   - finds the group whose short code / title matches `cp`,
   - creates a **fresh approval-required invite link**,
   - stores a **reservation**: (this link → this group → this one customer id),
   - sends the link back to the userbot through the shared `data/` bridge.
6. The **userbot** takes the returned link and **replaces `{link}` / `{links}`**
   in the "done" template, then edits its `/add` message into that finished
   text and sends it to the customer.
7. The customer taps the link and requests to join. The **bot** sees the join
   request:
   - if the requester **is** the reserved customer **and** used the **exact
     reserved link** → **approve**, then **revoke** that link,
   - otherwise → **decline** (the link is reserved for someone else).
8. `/revoke ANI0007` (on the bot, owner only) revokes that order's link(s) **and
   bans** the customer who joined through it.

### Multi-group in one command

```
/add 252 Nana cp op lp fp
```
- Reserve **one link per keyword** (4 groups here), each bound to the same
  customer.
- `all` as the keyword = **every group the bot is admin in**.
- The finished message lists the links **numbered 1, 2, 3, 4 … to the end**
  (see `{links}` below).

---

## 3. `{link}` vs `{links}` (templating — get this exact)

Templates are plain text with `{tokens}`. Two link tokens exist:

- **`{link}`** → a **single** link. Used when exactly one group was reserved.
  Rendered as the raw URL (no numbering).
- **`{links}`** → a **numbered, bold-marked, blockquoted list** of *all* the
  reserved links, one per line:
  ```
  1. https://t.me/+AAA
  2. https://t.me/+BBB
  3. https://t.me/+CCC
  4. https://t.me/+DDD
  ```
  - Numbering starts at `1` and runs to the last link.
  - Each `N.` prefix is **bold**.
  - The whole block is wrapped in a **blockquote**.
  - Works for any count, including a single link (then it's just `1. <url>`).

**Rendering rules:**
- If the template contains `{links}`, always use the numbered block there.
- If the template contains only `{link}` and there are **multiple** links,
  render the numbered `{links}` block at the `{link}` position anyway (never
  silently drop the extra links).
- If the template contains **neither** token but links exist, append the
  numbered block at the end.
- Replace **every** occurrence of the token (a template may repeat it).
- Offsets are counted in **UTF-16 code units** (this is how Telegram counts
  entity positions). Any bold/blockquote/link/custom-emoji entities already in
  the template must be shifted correctly when the block is inserted.

**Other template tokens** (all optional): `{amount}`, `{name}`, `{orderid}`,
`{keyword}`, `{title}`, `{short}`, `{total}` (payments today), `{todaytotal}`
(sum today), `{rioshare}`, `{marco}`. Keep whatever split tokens already exist.

---

## 4. Formatting & premium emoji (do NOT regress this)

- Templates are set by **replying to a formatted message** with `/setdone` (or
  `/setchannelpost`). Store the message's **entities** (bold, links, spoilers,
  **premium/custom emoji**) so the formatting survives verbatim.
- When sending, keep **all** entities including custom (premium) emoji.
- Fallback ladder when Telegram rejects the entity set:
  1. try with all entities,
  2. if — and only if — Telegram raises an **entity/premium** error, retry
     without custom-emoji entities (keep bold/blockquote/links),
  3. last resort, plain text.
- **Never** treat an unrelated network error as "entity problem" and silently
  strip formatting. Only degrade on a genuine entity/premium rejection; on any
  other error, surface it.
- Do not print "your account isn't Premium" unless the error was actually a
  premium-emoji rejection. **The owner's account IS Premium.**
- **No italics** in bot/userbot status messages. Use plain text, `<b>` for
  emphasis, `<code>` for ids/values, `<blockquote>` for hints.

---

## 5. Font folding (group name matching)

Group titles are often styled (`ɴɪᴄᴇ ʙʀᴏ`, `𝐍𝐢𝐜𝐞 𝐁𝐫𝐨`). When matching a typed
keyword against a title:
- Fold both sides to plain lowercase ASCII first.
- NFKD normalization handles bold/italic/script/fraktur/double-struck/mono/
  fullwidth/circled variants.
- A small-caps lookup table handles letters NFKD leaves alone
  (`ᴀ→a`, `ʙ→b`, `ᴄ→c`, `ᴅ→d`, `ᴇ→e`, `ɢ→g`, `ʜ→h`, `ɪ→i`, `ᴋ→k`, `ʟ→l`,
  `ᴍ→m`, `ɴ→n`, `ᴏ→o`, `ᴘ→p`, `ʀ→r`, `ᴛ→t`, `ᴜ→u`, `ᴠ→v`, `ᴡ→w`, `ʏ→y`,
  `ᴢ→z`, etc.).
- So typing `nice bro` matches a group titled `ɴɪᴄᴇ ʙʀᴏ`.

Matching priority: **exact short-code match first**, then substring match on
folded title / @username. If a keyword is **ambiguous** (matches several groups
and none by exact short code), **do not guess** — return a failure listing the
candidates so the owner can be specific.

---

## 6. The bridge (how bot and userbot talk)

They share files under `data/`. Requirements:

- **One batched request per `/add`.** Send all keywords together (not one file
  write per keyword). Request record:
  `{id, queries:[...], user_id, status, attempts, lease, ts}`.
- **Atomic, cross-process locking** on every read-modify-write (use `fcntl`
  file locks on Linux). Never do a bare read-then-write.
- **Leasing:** the bot claims a request atomically, gets a unique **lease
  token**, and marks it `processing`. Only the holder of the current lease
  token may publish the result or release it. This prevents a duplicate/stale
  bot process from corrupting an in-flight request.
- **Retry, don't drop:** if fulfilling a request throws, release it back to
  `pending` (do **not** mark it done). A transient error must be retried, not
  lost. (This was a real past bug: the old poller marked requests "seen" before
  doing the work, so any error lost the request forever.)
- **Result record:** `{id → {entries:[{link,title,keyword}], failures:
  [{keyword,reason}], ts}}`. Always report **per-keyword failures explicitly**
  so the userbot can tell the owner "cp ok, op failed: not admin".
- **Cancellation fencing:** if the userbot times out and cancels, the bot must
  not later flip a cancelled request to done; and any links it already minted
  for that cancelled request must be revoked.
- TTL-prune old requests/results (e.g. 1 hour).

The userbot side is simple: write one request, poll the result file until the
result appears or a timeout (~45s), then render.

---

## 7. Join approval / revocation lifecycle (security-critical)

Store reservations in SQLite, **keyed by the exact invite link** (not by
`chat+user`, because the same user may buy the same group twice and each link
must stay independent):

```
reservations(
  id, request_id, keyword, chat_id, user_id,
  invite_link UNIQUE, status, last_error,
  created_at, approved_at, revoked_at
)
status ∈ pending → approving → approved_revoke_pending → completed
                 ↘ cancelled
```

On a **join request** the bot must:
1. Look up a reservation by **exact (chat_id, user_id, invite_link) that is
   still pending**. Only that exact match is auto-approved. A customer using a
   different/general link is **never** auto-approved from a reservation.
2. Before calling Telegram approve, persist `approving` (so a crash after
   Telegram accepts doesn't leave the link reusable).
3. Approve → set `approved_revoke_pending` → revoke the link. If revoke
   succeeds → `completed`. If revoke fails → keep `approved_revoke_pending` and
   a **background retry loop** keeps trying until the link is dead.
4. If someone else used a link that still has an **active** reservation →
   **decline** them.
5. Anything with no reservation → normal manual Approve/Decline buttons to the
   owner.

Never write a buyer-specific reserved link into the group's general/stored link
field — keep "general link" and "reserved link" separate so distribution never
hands out a buyer's single-use link.

---

## 8. Commands

### Bot (owner-only, DM)
| Command | Effect |
|---|---|
| bot added to a group | DM owner: title, short code, member count, link. Store group. |
| `/groups` | List known groups: short code, title, admin?, link? |
| `/revoke <orderid>` | Revoke every link in that order **and ban** the buyer(s) who joined through it. |
| `/orders` | Recent orders + status. |
| `/id` | Reply with the caller's id (debug owner mismatch). |
| `/doctor` | Self-check: token ok, owner id, bridge folder writable, poller running. |
| `<keyword>:<userid>` (bare text) | Manual reserve: make a link for that group bound to that one user id (same as an `/add` reservation, but issued from the bot directly). |

### Userbot (owner sends these; `/add` in the customer's DM)
| Command | Effect |
|---|---|
| `/add <amount> <name> <kw...>` | Ask bot to reserve link(s) for the customer, fill `{link}`/`{links}` in the **done** template, edit the command into that message. `all` = every admin group. Optionally reply to a proof image to also post it to the payment channel. |
| `/setdone` | Set the customer message template (reply to a formatted post to keep formatting + premium emoji). |
| `/setchannelpost` | Set the payment-channel caption template. |
| `.setchannel` | Run inside a channel to mark it the payment channel. |
| `/revoke <orderid>` *(optional mirror)* | Ask the bot to revoke+ban for that order. |
| `/stats` / `/clear` | Today's total/count/split; reset today's stats. |
| `/cancel` | Reply to a payment-channel post → mark it fake, exclude from stats. |
| `.ping` / `.help` | Liveness / help. |
| greeting: `/set` `/unset` `/show` `/away on|off` | First-DM greeting while owner offline. |
| `/broadcast TIME DATE [YEAR] KEYWORD` | Reply to a post → copy it to every chat where the owner sent KEYWORD in that IST window. |
| `L` (uppercase, in a private chat) | Clear the chat for both sides and block. |

### Order ids
- Format: prefix + zero-padded counter, e.g. `ANI0001`, `ANI0002` (prefix
  configurable, default `ANI`). Shown in payment post and used by `/revoke`.

---

## 9. Guard / demo isolation (leave guard alone)

- `guard.py` keeps doing exactly what it does now: rotate the demo channel link,
  DM it to the owner, kick non-members. **No behaviour change.**
- The userbot updates its Business `/demo` quick reply **only** from the guard's
  link. Implement it so the userbot accepts a demo link **only** when it matches
  the value the guard published (guard writes `data/demo_link.json` right before
  DMing it; userbot validates against that, plus optional `LINK_SOURCE` sender
  check). A payment link from the bot must never be able to change `/demo`.
- Payment rendering must have **no fallback** to the demo/current link. If the
  bot returns no link, `{link}`/`{links}` are empty and the failure is reported
  — never silently substitute the demo link.

---

## 10. Data & tech

- **bot**: `aiogram` v3 + `aiosqlite`. SQLite tables: `groups`, `templates`,
  `orders`, `order_links`, `reservations`, `join_requests`, `events`.
- **userbot / guard**: `Telethon`. Payment log persisted to a JSON file
  (amount, name, orderid, ts, status, links, payer id, keywords).
- **logging**: colored console logging (`colorama`) with clear tags like
  `[reserve]`, `[approve]`, `[revoke]`, `[pay]`.
- **run**: everything from repo root so `import linkstore` (the bridge module)
  resolves and all three share `data/`.
  - bot: `python -m bot` (NOT `python bot/app.py` — relative import fails).
  - userbot: `python quickreply.py`
  - guard: `python guard.py`
- Single-instance lock on the userbot; the bot's background tasks (poller +
  revoke-retry) must be tracked and cancelled cleanly on shutdown.

---

## 11. Config (.env)

```
# bot/.env
BOT_TOKEN=            # from BotFather
OWNER_ID=            # the one owner user id (controls bot, gets logs)
ORDER_PREFIX=ANI
PAYMENT_CHANNEL=     # optional: id/@username to also post orders to

# root .env (userbot + guard)
API_ID=
API_HASH=
QR_SESSION=quickreply     # userbot session name
SESSION=guard             # guard session name
LINK_SOURCE=              # guard account id/username; blank = trust guard's local demo_link.json
SHORTCUT=demo             # Business quick reply name kept equal to the demo link
```

---

## 12. Acceptance tests (must pass)

1. `/add 252 Nana cp` in a customer DM → customer receives the done message with
   the **one** reserved link in place of `{link}`.
2. `/add 252 Nana cp op lp fp` → done message shows a **numbered 1–4**
   blockquoted list at `{links}`. If one keyword fails, the others still send
   and the failure is reported.
3. Only the reserved customer, using the reserved link, is auto-approved; the
   link is revoked right after; a second person on that link is declined.
4. Same customer buying `cp` twice gets **two independent** links; approving one
   never invalidates the other.
5. Bot restart mid-flow: a pending request is retried, not lost; an approved
   link with a failed revoke is eventually revoked by the retry loop.
6. Fancy-font group titles (`ɴɪᴄᴇ ʙʀᴏ`) match a plain keyword (`nice bro`).
7. Premium emoji + bold + blockquote survive in the done and channel messages.
8. `/revoke ANI0007` revokes the order's link(s) and bans the buyer.
9. The bot's payment links **never** change the userbot's `/demo`; guard still
   controls `/demo` normally.
10. Running the bot with `python -m bot` from repo root prints
    `Reservation poller started` and the bridge files appear in `data/`.

---

## 13. Known past failures to avoid (don't repeat these)

- Userbot trying to search groups itself — **wrong**; the bot searches.
- One bridge file-write per keyword and a 15s wait each — caused 60s hangs.
  Use **one batched request**.
- Poller marking a request "seen" before doing the work — lost requests on any
  error. Use **lease + retry**.
- Binding keyed by `(chat,user)` and overwriting the link — broke repeat buys
  and let the wrong link approve. Key by **exact invite link**.
- Approving before persisting state, or marking done before revoke succeeded —
  left reusable links. Persist `approving` first; retry revoke.
- Broad `except: strip entities` — silently killed premium emoji on unrelated
  errors. Only degrade on real entity errors.
- Payment link leaking into the general link / demo link — keep them separate.
- Running `python bot/app.py` — relative-import crash. Use `python -m bot`.
