"""ChannelGuard admin bot (bot-token, aiogram v3).

A single-owner Telegram bot that auto-onboards into groups/channels the moment
it is promoted to admin: it derives a short code from the title, mints a
join-request ("admin approval") invite link, stores everything in SQLite, and
DMs the owner. The owner drives link distribution, join-request approval,
templates, and member removal entirely from the bot's DM.
"""

__all__ = ["__version__"]
__version__ = "1.0.0"
