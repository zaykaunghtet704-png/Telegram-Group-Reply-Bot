# Telegram Group Auto Reply Bot

Keyword-based Telegram group auto-reply bot. This is **not an AI chatbot**: it looks for configured keywords in group messages and sends one of the matching saved replies.

## Features

- Add, list, and delete keyword/reply pairs
- Enable or disable auto-replies per group
- Set reply probability (0–100%)
- Set per-user cooldown (0–3600 seconds)
- SQLite storage
- Admin-only configuration commands
- Runs with Telegram long polling; suitable for a Render Background Worker

## Commands

- `/start` – start message
- `/help` – command guide
- `/addreply keyword | reply` – add a reply
- `/delreply ID` – delete a reply
- `/listreplies` – list saved replies
- `/autoreply on|off` – toggle replies
- `/setchance 80` – set chance to 80 percent
- `/setcooldown 5` – wait 5 seconds before replying again to the same user
- `/replysettings` – show settings
- `/stats` – count saved replies

Example:

```text
/addreply မင်္ဂလာပါ | မင်္ဂလာပါခင်ဗျာ 👋
```

If a group message contains the keyword `မင်္ဂလာပါ`, the bot replies with the saved text.

## Run locally

1. Create a bot with [@BotFather](https://t.me/BotFather).
2. Copy `.env.example` to `.env` and put your bot token in it, or set the `BOT_TOKEN` environment variable directly.
3. Install dependencies: `pip install -r requirements.txt`.
4. Run: `python bot.py`.

Do not commit your real `.env` file or bot token.

## Telegram group setup

1. Add the bot to your group.
2. Make it an administrator if you want it to reliably read and reply to group messages.
3. In @BotFather, use `/setprivacy`, choose your bot, then select **Disable** so it can receive ordinary group messages. Re-add the bot to the group if Telegram asks you to.
4. Use the admin commands in the group to add replies.

## Deploy on Render

1. Create a **Background Worker** from this repository.
2. Build command: `pip install -r requirements.txt`.
3. Start command: `python bot.py`.
4. Add an environment variable named `BOT_TOKEN` with your BotFather token.
5. Deploy.

### Important storage note

The bot uses a local SQLite database file named `replies.db`. Some hosting plans may use temporary or non-persistent disks. Attach a persistent disk to keep saved replies across redeploys, or migrate the database to a persistent database service before using an ephemeral worker.

## Notes

- Each group's replies and settings are separated by group ID.
- Matching is case-insensitive for languages supported by Python's Unicode case-folding.
- The bot chooses randomly if more than one saved keyword matches.
- The bot does not send unsolicited private messages.
