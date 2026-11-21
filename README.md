# PRO Reply Bot Ultimate X

Traditional Telegram group keyword auto-reply bot. This is **not an AI chatbot**: admins define the trigger keyword and the reply content.

## Features
- Myanmar and English command/help text
- Per-group SQLite reply rules and settings
- Keyword matching: `contains`, `exact`, `startswith`
- Deterministic keyword matching: priority first, then exact/longer match specificity
- Text, photo, GIF/animation, video, sticker, voice, audio and document replies
- Admin-only Telegram dashboard with inline buttons
- Add, edit, delete and list replies
- Enable/disable, reply chance, per-user and group cooldown, language setting
- Per-rule priority (higher priority rules are checked first)
- Flood protection (message burst limit)
- Welcome and goodbye messages with `{name}` placeholder
- Optional group conversation learning with fuzzy matching (OFF by default)
- Group security controls: anti-link and duplicate-message anti-spam (OFF by default)
- Anti-nuke alerts for repeated member removals/admin demotions, with owner-only Ban / Do not ban decision buttons
- Admin review/deletion of learned prompt/response pairs
- Manual database backup with `/backup`
- Scheduled database backup to the owner when `OWNER_ID` is configured
- Render Web Service webhook deployment

## Conversation Learning Auto Reply

- Admins enable it with `/learn on` and pause it with `/learn off`.
- When enabled, the bot learns only when a member explicitly replies to another member's message; the reply target must match the stored previous message author, and the reply must arrive within five minutes.
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
- `/security on|off` — enable/disable group security checks
- `/antilink on|off` — delete messages containing common links (requires delete-message permission)
- `/antispam on|off` — delete repeated identical messages
- `/setraidlimit 2-20` — alert owner after this many member removals/admin demotions by one actor within 60 seconds
- `/addfilterword phrase` — add a group phrase to the deletion filter
- `/listfilterwords` — list blocked phrases
- `/delfilterword ID` — remove a blocked phrase
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
  - `OWNER_ID` — numeric Telegram owner user ID; required for anti-nuke owner alerts and scheduled backup delivery
  - `BACKUP_INTERVAL_HOURS` — optional, defaults to 24

Render provides `PORT` and `RENDER_EXTERNAL_URL` to a Web Service. The app uses Telegram webhook mode.

## Telegram setup
1. Add the bot to the group.
2. Disable BotFather privacy mode with `/setprivacy` if it must read ordinary group messages; then remove and re-add the bot if Telegram asks.
3. Give it permission to send messages and media.
4. Promote the bot to group admin and grant Delete Messages, Ban Users, and Manage Chat permissions as needed. Use a group administrator account to run `/panel` or add reply rules.\n5. Set `OWNER_ID` in Render and start a private chat with the bot using `/start`, so anti-nuke alerts can reach the owner.
5. To receive automatic backups, set `OWNER_ID` in Render and start a private chat with the bot using `/start`.

## Security limitations\n- Telegram does not provide bots with users’ personal profile bio text, so the bot cannot scan or delete a member’s private Bio. It can moderate links and repeated text posted as group messages.\n- Telegram does not provide a general feed of messages deleted by other admins, nor a reliable event for every group description/profile change.\n- Anti-nuke monitoring uses Telegram chat-member updates. The bot must be a group admin and must receive chat-member updates. It detects repeated removals/admin demotions; it cannot undo an action before Telegram reports it.\n- The owner decision button is only sent when `OWNER_ID` is configured and the owner has opened the bot privately. If the suspected actor is still a group admin, Telegram may prevent the bot from banning them; the group creator may need to demote them first.\n- These protections are OFF by default. Enable `/security on`, then `/antilink on` and/or `/antispam on` per group. Add recruitment/scam phrases with `/addfilterword phrase`; the bot deletes group messages containing those phrases.\n\n## Important storage note
The primary database is PostgreSQL through `DATABASE_URL`. The old `replies.db` is retained as a one-time migration source and is not the live database. Use `/backup` regularly and configure `OWNER_ID` for scheduled PostgreSQL JSON backups. Keep the PostgreSQL database attached to both the bot and dashboard services.

## Local run
Install dependencies with `pip install -r requirements.txt`, set `BOT_TOKEN`, and run `python bot.py` only in an environment that also provides `RENDER_EXTERNAL_URL`; this project is currently configured for Render webhook deployment.


## Content moderation and warnings
- `/adultfilter on|off` — delete messages/captions matching the built-in 18+ text keyword list (OFF by default).
- `/warn` — reply to a member's message with an optional reason to add a warning.
- `/warns` — show the warning history for yourself, or reply to a member to view theirs.
- `/resetwarn` — clear active warnings for yourself, or reply to a member to clear theirs.
- `/setwarnlimit 2-20` — set the warning threshold (default 3).
- `/setwarnaction mute|kick|ban` — choose the automatic action after the threshold (default mute for 60 minutes).
- Automated 18+ text detections are logged to the configured owner where private messages are available.
- The built-in filter checks text and captions only. It does not visually classify image/video content and may miss obfuscated wording or produce false positives. Admins are exempt.
- The bot needs Delete Messages permission for deletion and Restrict Members / Ban Users permission for escalation.


## Bot-owner advertising and role separation
- The configured global Bot Owner ID is `7974865879` (also set in `render.yaml` and `.env.example`).
- Bot Owner can broadcast a replied-to text, photo, video, or other Telegram-copyable message with `/ad groups`, `/ad users`, or `/ad all`.
- `/adstats` shows active known groups and opted-in private users.
- A user must first open the bot and press Start before the bot can send private messages. Users can opt out with `/ads off` and opt back in with `/ads on`.
- Group owners and group admins can manage only their own group's reply and moderation settings. They cannot use global advertising commands or control other groups.
- The Bot Owner is exempt from the bot's automatic group text moderation and cannot be targeted by its warning/anti-nuke actions.
- The bot only tracks groups after it receives an update from them, and private users after they start the bot. Broadcast delivery is subject to Telegram limits, blocked DMs, and the bot's current database. Render Free ephemeral storage may erase this audience list after redeploy/restart.
- Send ads responsibly and only to users who have started the bot and have not opted out.


## Ultimate X member tools (added)

- `/id` — show your Telegram user ID and current chat ID; reply to a member's message to inspect their ID.
- `/profile` — show the member profile and message count tracked by the bot.
- `/report reason` — reply to a message to create a persistent report for the group.
- `/reports` — group admins can review the latest open reports.
- `/closereport ID` — group admins can close a report.
- `/afk reason` — set your AFK status in a group; it clears when you next send a message.
- `/note text`, `/notes`, `/delnote ID` — save, list and delete your own group-scoped notes.

Member profiles, reports, AFK state and notes are stored in PostgreSQL and survive bot restarts. Message counts begin when this version is deployed; the bot cannot reconstruct earlier message history. Reports are saved even if an administrator has not opened a private chat with the bot; use `/reports` in the group to review them. Private report notifications are best-effort only.

- `/setrules text` — group admins save persistent group rules.
- `/rules` — members view the saved group rules.
- `/lock` and `/unlock` — group admins lock/unlock default member messaging permissions (bot needs Restrict Members permission).

## Owner Web Dashboard

A separate Flask dashboard is included at `dashboard.py`. When the Render Blueprint is synced, it defines a second Web Service named `pro-reply-owner-dashboard`.

Set these Environment variables on that dashboard service:
- `DATABASE_URL` — use the same PostgreSQL Internal Database URL as the bot service.
- `DASHBOARD_PASSWORD` — create a long, unique password; do not reuse your Telegram or GitHub password.
- `SECRET_KEY` — generate a long random secret (at least 32 random bytes).

The dashboard provides owner login, group counts, private-user counts, saved reply counts, and per-group Auto Reply ON/OFF controls. It does not expose the Telegram bot token. Use HTTPS on Render and keep the dashboard URL private. The `/health` endpoint is public for hosting health checks; all management pages require the dashboard password.

If the dashboard service is not created automatically, open Render Blueprint → review the new `pro-reply-owner-dashboard` service and create it. Do not change or delete the existing bot service or PostgreSQL database.


## Relationship System (18+)

- `/love` — Relationship menu.
- `/loveprofile` — Create/edit a profile in private chat (age 18+).
- `/lovepublish` — Publish after joining every Bot Owner-required channel and explicitly consenting.
- `/lovesearch` — Browse an active profile and Like/Report.
- `/loveposts on|off` — Group admins opt a group into relationship posts.
- `/lovedm on|off` — Users opt in/out of receiving profile posts by DM.
- `/lovedeleteposts` — Remove the user's recorded published posts and deactivate the profile.
- `/lovechannel add @channel`, `/lovechannel remove @channel`, `/lovechannel list` — Bot Owner manages required channels (bot must be a channel admin).
- `/loveblock USER_ID` — Block a profile.
- `/lovereports` — Bot Owner reviews profile reports.

Profile details are stored in PostgreSQL. Group and DM distribution is opt-in; users must start the bot before receiving DMs. The bot never publishes a phone number or Telegram username in the profile card. Configure required public channels and grant the bot administrator permissions before enabling publishing.


## Custom Welcome & Goodbye Messages

Group admins can customize both messages independently. Settings are stored in PostgreSQL and survive restarts.

- `/setwelcome on|off` and `/setgoodbye on|off` — toggle each message.
- `/setwelcometext your message` — customize the join message.
- `/setgoodbyetext your message` — customize the leave message.
- `/welcomepreview` and `/goodbyepreview` — preview with sample member details.
- `/resetwelcome` and `/resetgoodbye` — restore the built-in default text.

Placeholders: `{name}` (member name), `{username}` (Telegram username), `{id}` (Telegram user ID), `{chat}` (group title), and `{count}` (current member count). Messages are plain text and can contain emoji and line breaks. Maximum custom message length: 3,500 characters.
