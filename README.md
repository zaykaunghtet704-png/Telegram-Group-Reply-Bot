# PRO Reply Bot Ultimate X

Traditional Telegram group keyword auto-reply bot. This is **not an AI chatbot**: admins define the trigger keyword and the reply content.

## Features
- Myanmar and English command/help text
- Per-group SQLite reply rules and settings
- Keyword matching: `contains`, `exact`, `startswith`
- Random selection when multiple rules match
- Text, photo, GIF/animation, video, sticker, voice, audio and document replies
- Admin-only Telegram dashboard with inline buttons
- Add, edit, delete and list replies
- Enable/disable, reply chance, per-user and group cooldown, language setting
- Per-rule priority (higher priority rules are checked first)
- Flood protection (message burst limit)
- Welcome and goodbye messages with `{name}` placeholder
- Optional group conversation learning with fuzzy matching (OFF by default)
- Admin review/deletion of learned prompt/response pairs
- Manual database backup with `/backup`
- Scheduled database backup to the owner when `OWNER_ID` is configured
- Render Web Service webhook deployment

## Conversation Learning Auto Reply

- Admins enable it with `/learn on` and pause it with `/learn off`.
- When enabled, the bot stores consecutive messages from different group members as prompt/response examples if they arrive within 90 seconds.
- Later messages are compared with stored prompts using fuzzy text similarity; a reply is sent only when the similarity threshold is met.
- Default threshold: 72%. Change it with `/setlearnthreshold 72` (allowed: 50-95).
- Review examples with `/learned`; remove a bad pair with `/forgetlearned ID`.
- Learning is OFF by default. Get group members' consent before enabling it because message text is stored in the bot database.
- This is local text matching, not an AI model. It can learn incorrect pairs, so review the learned list.

## Telegram commands
- `/start`, `/help`
- `/panel` — admin dashboard
- `/addreply keyword | reply` — add a text rule
- Reply to a photo/GIF/video/sticker/etc. with `/addreply keyword` — save a media rule
- `/editreply ID | new text`
- `/delreply ID`
- `/listreplies`
- `/autoreply on|off`
- `/setmode exact|contains|startswith`
- `/setchance 0-100`
- `/setcooldown 0-3600`
- `/setlang my|en|both`
- `/setpriority ID | -100..100`
- `/setgroupcooldown 0-3600`
- `/setflood 0-30` (0 disables)
- `/setwelcome on|off`
- `/learn on|off` — toggle conversation learning
- `/learned` — list learned conversation pairs
- `/forgetlearned ID` — delete a learned pair
- `/setlearnthreshold 50-95` — set fuzzy-match threshold
- `/setgoodbye on|off`
- `/replysettings`, `/stats`, `/backup`

Commands that change group rules/settings are restricted to group administrators (or the configured owner).

## Render deployment
- Runtime: Python 3
- Build command: `pip install -r requirements.txt`
- Start command: `python bot.py`
- Environment variables:
  - `BOT_TOKEN` — token from @BotFather
  - `OWNER_ID` — optional numeric Telegram owner user ID; enables scheduled backup delivery
  - `BACKUP_INTERVAL_HOURS` — optional, defaults to 24

Render provides `PORT` and `RENDER_EXTERNAL_URL` to a Web Service. The app uses Telegram webhook mode.

## Telegram setup
1. Add the bot to the group.
2. Disable BotFather privacy mode with `/setprivacy` if it must read ordinary group messages; then remove and re-add the bot if Telegram asks.
3. Give it permission to send messages and media.
4. Use a group administrator account to run `/panel` or add reply rules.
5. To receive automatic backups, set `OWNER_ID` in Render and start a private chat with the bot using `/start`.

## Important storage note
The database is SQLite at `replies.db`. Render Free Web Services use an ephemeral filesystem, so database contents may be lost after a restart, redeploy, or instance replacement. Use `/backup` regularly and configure `OWNER_ID` for scheduled backups. For reliable permanent storage, move the database to persistent storage or a managed database.

## Local run
Install dependencies with `pip install -r requirements.txt`, set `BOT_TOKEN`, and run `python bot.py` only in an environment that also provides `RENDER_EXTERNAL_URL`; this project is currently configured for Render webhook deployment.
