import hashlib
import asyncio
from io import BytesIO
import logging
import os
import random
import time
from collections import defaultdict, deque
from pathlib import Path
from database import connect as db, DB_PATH, migrate_sqlite_once, export_database_json

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.error import TelegramError, RetryAfter, Forbidden
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ChatMemberHandler, filters, ApplicationHandlerStop, TypeHandler,
)
from security import init_security_db, get_security, set_security, moderate_message, monitor_member_change, owner_raid_decision, add_blocked_word, list_blocked_words, delete_blocked_word
from moderation import init_moderation_db, get_settings as get_moderation_settings, set_setting as set_moderation_setting, moderate_content, manual_warn, show_warnings, reset_warnings
from group_features import init_group_features, set_rules_command, rules_command, lock_command, unlock_command
from owner_control import (init_owner_control, owner_command, owner_callback,
                           get_user_restrictions, owner_ban_command, owner_unban_command)
from relationship import init_relationship, relationship_handlers
from member_features import (
    init_member_features, track_member_activity, id_command, profile_command,
    afk_command, check_afk_mention, note_command, notes_command,
    delnote_command, report_command, reports_command, close_report_command,
)
from learning import (
    init_learning_db, get_learning_settings, set_learning_setting,
    record_conversation, find_learned_reply, list_learned,
    delete_learned, clear_pending,
)
from coin_codes import (init_coin_codes, create_coin_code, redeem_coin_code, revoke_coin_code, coin_code_stats)
from rpg import (init_rpg, rpg_profile, missions_command, mission_claim_command, achievements_command, shop_command, buy_command, inventory_command, equip_command, rpg_daily_command, adventure_command, rpg_top_command)
from rpg_expansion import (init_rpg_expansion, guild_create_command, guild_join_command, guild_leave_command, guild_command, guild_top_command, gear_command, gear_buy_command, gear_equip_command, world_boss_command, world_boss_attack_command)
from rpg_ui import rpg_menu_callback
from games import (
    init_games, set_games_enabled, game_help, balance, rank, toprank, daily, topcoins,
    quiz, answer, fish, hunt, mines, mine_callback, pick_mine, cashout_mines, spin, duel, duel_accept, duel_decline,
    addcoins, takecoins,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "7974865879") or 7974865879)
GAME_WEBAPP_URL = os.getenv("GAME_WEBAPP_URL", "").strip().rstrip("/")
if GAME_WEBAPP_URL and not GAME_WEBAPP_URL.startswith(("https://", "http://")):
    GAME_WEBAPP_URL = "https://" + GAME_WEBAPP_URL
BACKUP_INTERVAL_HOURS = max(1, int(os.getenv("BACKUP_INTERVAL_HOURS", "24")))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("pro-reply-bot")
FLOOD_TRACKER = defaultdict(deque)
GROUP_LAST_REPLY = defaultdict(float)


async def global_access_guard(update, context):
    """Apply Bot Owner bans and maintenance mode before normal handlers."""
    user = update.effective_user
    if not user or user.is_bot or (OWNER_ID and user.id == OWNER_ID):
        return

    maintenance, banned = get_user_restrictions(user.id)

    # A Bot-level ban always blocks the user, including game commands.
    if banned:
        notice = "🚫 သင့်အကောင့်ကို Bot အသုံးပြုခွင့် ပိတ်ထားပါတယ်။"
    elif maintenance:
        # Keep every group game command and interactive Minefield button usable
        # while unrelated bot features are in maintenance mode.
        query = update.callback_query
        if query and (query.data or "").startswith("mine:"):
            return
        msg = update.effective_message
        raw = (msg.text or "").strip() if msg else ""
        command = raw.split(maxsplit=1)[0].split("@", 1)[0].lower() if raw.startswith("/") else ""
        game_commands = {
            "/games", "/balance", "/rank", "/daily", "/topcoins", "/toprank",
            "/quiz", "/answer", "/fish", "/hunt", "/spin", "/mines",
            "/pick", "/cashout", "/duel", "/duelaccept", "/dueldecline", "/redeem",
            "/rpg", "/missions", "/missionclaim", "/achievements", "/shop", "/buy", "/inventory", "/equip", "/rpgdaily", "/adventure", "/rpgtop", "/guildcreate", "/guildjoin", "/guildleave", "/guild", "/guildtop", "/gear", "/gearbuy", "/gearequip", "/boss", "/bossattack",
            "/game",
        }
        if command in game_commands:
            return
        parts = raw.split(maxsplit=1)
        if command == "/start" and len(parts) > 1 and parts[1].startswith("game_"):
            return
        notice = "🛠 Bot ကို Maintenance လုပ်နေပါတယ်။ ခဏနေမှ ပြန်စမ်းပါ။"
    else:
        return

    query = update.callback_query
    if query:
        await query.answer(notice, show_alert=True)
    else:
        msg = update.effective_message
        chat = update.effective_chat
        if msg and chat and (chat.type == "private" or (msg.text or "").startswith("/")):
            await msg.reply_text(notice)
    raise ApplicationHandlerStop


def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            chance INTEGER NOT NULL DEFAULT 100,
            cooldown INTEGER NOT NULL DEFAULT 3,
            match_mode TEXT NOT NULL DEFAULT 'contains',
            language TEXT NOT NULL DEFAULT 'both',
            group_cooldown INTEGER NOT NULL DEFAULT 0,
            flood_limit INTEGER NOT NULL DEFAULT 0,
            welcome_enabled INTEGER NOT NULL DEFAULT 1,
            goodbye_enabled INTEGER NOT NULL DEFAULT 0,
            welcome_text TEXT NOT NULL DEFAULT 'မင်္ဂလာပါ {name}။ Group မှ ကြိုဆိုပါတယ်။',
            goodbye_text TEXT NOT NULL DEFAULT '{name} Group မှ ထွက်ခွာသွားပါပြီ။'
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            keyword TEXT NOT NULL,
            response TEXT NOT NULL DEFAULT '',
            media_type TEXT NOT NULL DEFAULT 'text',
            file_id TEXT NOT NULL DEFAULT '',
            caption TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 0,
            hits INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS usage (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            last_reply REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id, user_id)
        )""")
        for table, column, definition in [
            ("settings", "group_cooldown", "INTEGER NOT NULL DEFAULT 0"),
            ("settings", "flood_limit", "INTEGER NOT NULL DEFAULT 0"),
            ("settings", "welcome_enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("settings", "goodbye_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("settings", "welcome_text", "TEXT NOT NULL DEFAULT 'မင်္ဂလာပါ {name}။ Group မှ ကြိုဆိုပါတယ်။'"),
            ("settings", "goodbye_text", "TEXT NOT NULL DEFAULT '{name} Group မှ ထွက်ခွာသွားပါပြီ။'"),
            ("replies", "priority", "INTEGER NOT NULL DEFAULT 0"),
            ("replies", "hits", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_replies_chat ON replies(chat_id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS known_users (
            user_id INTEGER PRIMARY KEY, first_seen REAL NOT NULL,
            ads_enabled INTEGER NOT NULL DEFAULT 1
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS known_groups (
            chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
            first_seen REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1
        )""")


def get_settings(chat_id):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (chat_id,))
        row = conn.execute("SELECT * FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
        return dict(row)


def set_setting(chat_id, key, value):
    allowed = {"enabled", "chance", "cooldown", "match_mode", "language", "group_cooldown", "flood_limit", "welcome_enabled", "goodbye_enabled", "welcome_text", "goodbye_text"}
    if key not in allowed:
        raise ValueError("Invalid setting")
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (chat_id,))
        conn.execute("UPDATE settings SET " + key + "=? WHERE chat_id=?", (value, chat_id))


def is_group(chat):
    return bool(chat and chat.type in ("group", "supergroup"))


async def is_admin(update, context):
    chat, user = update.effective_chat, update.effective_user
    if not chat or not user:
        return False
    if OWNER_ID and user.id == OWNER_ID:
        return True
    if not is_group(chat):
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        logger.exception("Admin check failed")
        return False


async def require_admin(update, context):
    if not is_group(update.effective_chat):
        await update.effective_message.reply_text("ဒီ command ကို Group ထဲမှာ သုံးပါ။")
        return False
    if not await is_admin(update, context):
        await update.effective_message.reply_text("Group Admin များသာ အသုံးပြုနိုင်ပါတယ်။")
        return False
    return True


async def game_toggle(update, context):
    if not await require_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text("အသုံးပြုပုံ: /game on သို့မဟုတ် /game off")
        return
    enabled = context.args[0].lower() == "on"
    set_games_enabled(update.effective_chat.id, enabled)
    await update.effective_message.reply_text(
        "🎮 Group Game ဖွင့်ပြီးပါပြီ။ /games ဖြင့် Command ကြည့်ပါ။"
        if enabled else "⛔ Group Game ပိတ်ပြီးပါပြီ။"
    )


def media_from_message(message):
    if message.photo:
        return "photo", message.photo[-1].file_id, message.caption or ""
    if message.animation:
        return "animation", message.animation.file_id, message.caption or ""
    if message.video:
        return "video", message.video.file_id, message.caption or ""
    if message.document:
        return "document", message.document.file_id, message.caption or ""
    if message.sticker:
        return "sticker", message.sticker.file_id, ""
    if message.voice:
        return "voice", message.voice.file_id, message.caption or ""
    if message.audio:
        return "audio", message.audio.file_id, message.caption or ""
    return None


async def send_reply(message, row):
    kind = row["media_type"]
    file_id = row["file_id"]
    caption = row["caption"] or None
    text = row["response"] or None
    if kind == "text" or not file_id:
        if text:
            await message.reply_text(text, reply_to_message_id=message.message_id)
        return
    kwargs = {"reply_to_message_id": message.message_id}
    if caption:
        kwargs["caption"] = caption
    if kind == "photo":
        await message.reply_photo(file_id, **kwargs)
    elif kind == "animation":
        await message.reply_animation(file_id, **kwargs)
    elif kind == "video":
        await message.reply_video(file_id, **kwargs)
    elif kind == "sticker":
        await message.reply_sticker(file_id, reply_to_message_id=message.message_id)
    elif kind == "voice":
        await message.reply_voice(file_id, **kwargs)
    elif kind == "audio":
        await message.reply_audio(file_id, **kwargs)
    elif kind == "document":
        await message.reply_document(file_id, **kwargs)


def start_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👑 Owner", url="https://t.me/Alcohol_1752"),
            InlineKeyboardButton("📢 Channel", url="https://t.me/myaayech"),
        ],
        [InlineKeyboardButton("📖 Bot Help", callback_data="start:help")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user and update.effective_chat and update.effective_chat.type == "private":
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO known_users(user_id,first_seen) VALUES(?,?)", (user.id, time.time()))
    if context.args and context.args[0].startswith("game_"):
        try:
            group_id = int(context.args[0][5:])
        except ValueError:
            group_id = 0
        if group_id and GAME_WEBAPP_URL:
            await update.effective_message.reply_text(
                "💗 မြအေး 3D GAME HUB\nGroup ထဲက သင့် Coin နဲ့ Level ကို အသုံးပြုပြီး ကစားနိုင်ပါပြီ။",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🚀 PLAY 3D GAME", web_app=WebAppInfo(url=f"{GAME_WEBAPP_URL}/?chat_id={group_id}"))
                ]])
            )
            return
        if group_id and not GAME_WEBAPP_URL:
            await update.effective_message.reply_text("3D Game Service URL မသတ်မှတ်ရသေးပါ။ Render Blueprint ကို Sync လုပ်ပါ။")
            return
    await update.effective_message.reply_text(
        "💗 𝗠𝗬𝗔𝗬𝗘 • မြအေး 💗\n"
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "       ✨ 𝗬𝗢𝗨𝗥 𝗚𝗥𝗢𝗨𝗣 𝗙𝗥𝗜𝗘𝗡𝗗 ✨\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "💬 မြအေးနဲ့ စကားပြောလို့ရတယ်နော်။\n"
        "🎮 Coin စု၊ Level တက်ပြီး ဂိမ်းတွေလည်း ဆော့လို့ရတယ်။\n"
        "🎰 Spin • 🎣 Fish • ⚔️ Hunt • 💣 Mines • 🧠 Quiz • 🥊 Duel\n"
        "🛡️ Auto Reply • Security • Admin Tools လည်း ပါတယ်။\n\n"
        "မြအေးကို Group ထဲမှာ ခေါ်လိုက်ပါနော် 💗\n"
        "ဂိမ်းစာရင်းကြည့်ရန် /games ကိုနှိပ်ပါ။",
        reply_markup=start_keyboard(),
    )


async def start_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "start:home":
        await query.edit_message_text(
            "💗 𝗠𝗬𝗔𝗬𝗘 • မြအေး 💗\n"
            "╭━━━━━━━━━━━━━━━━━━━━╮\n"
            "       ✨ 𝗬𝗢𝗨𝗥 𝗚𝗥𝗢𝗨𝗣 𝗙𝗥𝗜𝗘𝗡𝗗 ✨\n"
            "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
            "💬 မြအေးနဲ့ စကားပြောလို့ရတယ်နော်။\n"
            "🎮 Coin စု၊ Level တက်ပြီး ဂိမ်းတွေလည်း ဆော့လို့ရတယ်။\n"
            "🎰 Spin • 🎣 Fish • ⚔️ Hunt • 💣 Mines • 🧠 Quiz • 🥊 Duel\n"
            "🛡️ Auto Reply • Security • Admin Tools လည်း ပါတယ်။\n\n"
            "မြအေးကို Group ထဲမှာ ခေါ်လိုက်ပါနော် 💗\n"
            "ဂိမ်းစာရင်းကြည့်ရန် /games ကိုနှိပ်ပါ။",
            reply_markup=start_keyboard(),
        )
        return

    help_text = (
        "📖 PRO Reply Bot Ultimate X — အပြည့်အစုံ လမ်းညွှန်\n\n"
        "🤖 1. Smart Auto Reply\n"
        "• Keyword သတ်မှတ်ပြီး စာသား၊ ဓာတ်ပုံ၊ GIF၊ Video စသည်တို့ကို အလိုအလျောက် Reply ပြန်နိုင်သည်။\n"
        "• Exact / Contains / Startswith စနစ်၊ Reply ဦးစားပေးမှု၊ Reply ဖြစ်နိုင်ခြေနှင့် Cooldown သတ်မှတ်နိုင်သည်။\n"
        "• Group စကားဝိုင်းမှ သင်ယူပြီး Reply ပြန်နိုင်သည့် Learning စနစ် ပါဝင်သည်။\n\n"
        "🛡️ 2. Group Security & Moderation\n"
        "• Anti-Spam၊ Anti-Link၊ Flood ကာကွယ်မှု၊ Raid / Anti-Nuke စောင့်ကြည့်မှု။\n"
        "• တားမြစ်စကားလုံး၊ 18+ စာသား Filter၊ Warn၊ Mute၊ Kick၊ Ban စနစ်များ။\n"
        "• Group Admin များက စည်းကမ်း၊ Lock / Unlock နှင့် Welcome / Goodbye ကို စီမံနိုင်သည်။\n\n"
        "👥 3. Member Tools\n"
        "• Member ID၊ Profile၊ AFK၊ ကိုယ်ပိုင် Notes နှင့် Admin ထံ Report ပို့ခြင်း။\n\n"
        "💘 4. Relationship System (18+)\n"
        "• Private Chat မှ Profile ဖန်တီး/ပြင်ဆင်ပြီး သဘောတူညီချက်ဖြင့် Publish လုပ်နိုင်သည်။\n"
        "• Channel Join စစ်ဆေးမှု၊ Profile ရှာဖွေမှု၊ Like၊ Report၊ Block / Unblock ပါဝင်သည်။\n"
        "• Group နှင့် DM များတွင် လက်ခံရန် ကိုယ်တိုင် Opt-in လုပ်ထားမှသာ Post ပို့မည်။\n\n"
        "👑 5. Bot Owner Tools\n"
        "• /owner — Owner Control Center\n"
        "• /ad groups|users|all — ကြော်ငြာပို့ရန် (ကြော်ငြာစာကို Reply လုပ်ပြီး သုံးပါ)\n"
        "• /adstats — ကြော်ငြာပို့နိုင်သည့် Group/User စာရင်း\n"
        "• /backup — Database Backup ယူရန်\n\n"
        "⚙️ 6. အရေးကြီး Commands\n"
        "• /help — Command စာရင်းအပြည့်အစုံ\n"
        "• /panel — Group Admin Dashboard\n"
        "• /addreply keyword | reply — Auto Reply ထည့်ရန်\n"
        "• /listreplies — Reply စာရင်းကြည့်ရန်\n"
        "• /autoreply on|off — Auto Reply ဖွင့်/ပိတ်ရန်\n"
        "• /security on|off — Security ဖွင့်/ပိတ်ရန်\n"
        "• /rules — Group စည်းကမ်းကြည့်ရန်\n"
        "• /love — Relationship Menu\n\n"
        "🎁 Coin Code ရရှိထားလျှင် /redeem CODE ဖြင့် Coin ရယူနိုင်ပါတယ်။\n\n"        "အသုံးပြုနိုင်သော Command အားလုံးကို /help မှာ ကြည့်နိုင်ပါတယ်။"
    )
    await query.edit_message_text(
        help_text,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ နောက်သို့", callback_data="start:home")
        ]]),
    )



def register_seen_update(update):
    chat = update.effective_chat
    user = update.effective_user
    if chat and chat.type in ("group", "supergroup"):
        with db() as conn:
            conn.execute("INSERT INTO known_groups(chat_id,title,first_seen,active) VALUES(?,?,?,1) "
                         "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,active=1",
                         (chat.id, chat.title or "", time.time()))
    elif chat and chat.type == "private" and user:
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO known_users(user_id,first_seen) VALUES(?,?)",
                         (user.id, time.time()))

async def track_chats(update, context):
    register_seen_update(update)
    track_member_activity(update)

async def owner_ad_command(update, context):
    user, msg = update.effective_user, update.effective_message
    if not OWNER_ID or not user or user.id != OWNER_ID:
        if msg: await msg.reply_text("⛔ Bot Owner သာ ကြော်ငြာပို့နိုင်ပါတယ်။")
        return
    if not msg.reply_to_message:
        await msg.reply_text("ကြော်ငြာစာ/ပုံ/Video ကို Reply လုပ်ပြီး /ad groups|users|all ဟု ပို့ပါ။")
        return
    target = context.args[0].lower() if context.args else ""
    if target not in ("groups", "users", "all"):
        await msg.reply_text("အသုံးပြုပုံ: ကြော်ငြာကို Reply လုပ်ပြီး /ad groups|users|all")
        return
    source = msg.reply_to_message
    with db() as conn:
        groups = [r["chat_id"] for r in conn.execute("SELECT chat_id FROM known_groups WHERE active=1").fetchall()]
        users = [r["user_id"] for r in conn.execute("SELECT user_id FROM known_users WHERE ads_enabled=1").fetchall()]
    targets = [("group", x) for x in groups] if target == "groups" else [("user", x) for x in users] if target == "users" else [("group", x) for x in groups] + [("user", x) for x in users]
    if not targets:
        await msg.reply_text("ပို့ရန် Group/User မရှိသေးပါ။")
        return
    status = await msg.reply_text(f"📢 ကြော်ငြာပို့နေပါပြီ။\nTarget: {target}\nစုစုပေါင်း: {len(targets)}")
    sent = failed = 0
    for kind, chat_id in targets:
        delivered = False
        for attempt in range(3):
            try:
                await context.bot.copy_message(
                    chat_id=chat_id,
                    from_chat_id=source.chat_id,
                    message_id=source.message_id,
                )
                sent += 1
                delivered = True
                break
            except RetryAfter as exc:
                if attempt == 2:
                    failed += 1
                else:
                    await asyncio.sleep(float(exc.retry_after) + 0.5)
            except Forbidden:
                failed += 1
                if kind == "group":
                    with db() as conn:
                        conn.execute("UPDATE known_groups SET active=0 WHERE chat_id=?", (chat_id,))
                else:
                    with db() as conn:
                        conn.execute("UPDATE known_users SET ads_enabled=0 WHERE user_id=?", (chat_id,))
                break
            except TelegramError:
                failed += 1
                logger.exception("Owner broadcast failed for target %s", chat_id)
                break
        await asyncio.sleep(0.1)
    await status.edit_text(f"📢 ကြော်ငြာပို့ပြီးပါပြီ။\nTarget: {target}\nအောင်မြင်: {sent}\nမအောင်မြင်: {failed}")

async def owner_ad_stats(update, context):
    msg, user = update.effective_message, update.effective_user
    if not user or user.id != OWNER_ID:
        if msg: await msg.reply_text("Bot Owner သာ ကြည့်နိုင်ပါတယ်。")
        return
    with db() as conn:
        groups = conn.execute("SELECT COUNT(*) n FROM known_groups WHERE active=1").fetchone()["n"]
        users = conn.execute("SELECT COUNT(*) n FROM known_users WHERE ads_enabled=1").fetchone()["n"]
    await msg.reply_text(f"📊 ကြော်ငြာ Target စာရင်း\nGroups: {groups}\nBot DM Users: {users}\nAll: {groups+users}")

async def ads_opt_out(update, context):
    msg, user = update.effective_message, update.effective_user
    if not user or not msg or update.effective_chat.type != "private": return
    with db() as conn:
        conn.execute("INSERT INTO known_users(user_id,first_seen,ads_enabled) VALUES(?,?,0) "
                     "ON CONFLICT(user_id) DO UPDATE SET ads_enabled=0", (user.id, time.time()))
    await msg.reply_text("ကြော်ငြာ DM ကို ပိတ်ပြီးပါပြီ။ ပြန်ဖွင့်ရန် /ads on")

async def ads_opt_in(update, context):
    msg, user = update.effective_message, update.effective_user
    if not user or not msg or update.effective_chat.type != "private": return
    with db() as conn:
        conn.execute("INSERT INTO known_users(user_id,first_seen,ads_enabled) VALUES(?,?,1) "
                     "ON CONFLICT(user_id) DO UPDATE SET ads_enabled=1", (user.id, time.time()))
    await msg.reply_text("ကြော်ငြာ DM ကို ပြန်ဖွင့်ပြီးပါပြီ။")

async def ads_command(update, context):
    if context.args and context.args[0].lower() == "on":
        await ads_opt_in(update, context)
    else:
        await ads_opt_out(update, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    language = get_settings(chat.id)["language"] if is_group(chat) else "both"
    if language == "en":
        english_help = (
            "💎 𝗣𝗥𝗢 𝗥𝗘𝗣𝗟𝗬 𝗕𝗢𝗧 — 𝗛𝗘𝗟𝗣\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🤖 AUTO REPLY\n"
            "/panel — Admin dashboard\n"
            "/addreply keyword | reply — Add a text reply\n"
            "/editreply ID | text — Edit a reply\n"
            "/delreply ID — Delete a reply\n"
            "/listreplies — List saved replies\n"
            "/autoreply on|off — Enable or disable replies\n"
            "/setmode exact|contains|startswith — Match mode\n"
            "/setchance 0-100 — Reply probability\n"
            "/setcooldown 0-3600 — Per-user cooldown\n"
            "/setlang my|en|both — Help language\n"
            "/setgroupcooldown 0-3600 — Group cooldown\n"
            "/setflood 0-30 — Flood reply limit\n\n"
            "🎮 GROUP GAMES & COINS\n"
            "/games /balance /daily /topcoins\n"
            "/quiz /answer 1-4 /fish /hunt /spin\n"
            "/duel (reply) /duelaccept /dueldecline /game on|off (admin)\n\n"
            "🛡️ SECURITY & MODERATION\n"
            "/security on|off — Master security switch\n"
            "/antilink on|off — Link filter\n"
            "/antispam on|off — Duplicate text filter\n"
            "/adultfilter on|off — 18+ text filter\n"
            "/warn — Warn a replied-to member\n"
            "/warns /resetwarn — Warning history / reset\n"
            "/addfilterword /listfilterwords /delfilterword — Phrase filter\n\n"
            "👥 MEMBER & GROUP TOOLS\n"
            "/id /profile /afk reason /report reason\n"
            "/reports /closereport ID — Report review\n"
            "/note text /notes /delnote ID — Personal notes\n"
            "/setrules text /rules /lock /unlock\n"
            "/setwelcome on|off /setgoodbye on|off\n"
            "/setwelcometext /setgoodbyetext — Custom messages\n\n"
            "💘 RELATIONSHIP (18+)\n"
            "/love — Relationship menu\n"
            "/loveprofile — Create an 18+ profile in private chat\n"
            "/lovepublish — Publish after consent and channel checks\n"
            "/lovesearch — Browse profiles\n"
            "/lovedeleteposts — Delete your published posts\n"
            "/loveposts on|off — Group opt-in (admin)\n"
            "/lovedm on|off — Private DM opt-in\n"
            "/loveblock /loveunblock USER_ID — Block or unblock\n"
            "/lovechannel add|remove|list — Owner channel settings\n"
            "/lovereports — Owner report review\n\n"
            "🧠 LEARNING & OWNER\n"
            "/learn on|off /learned /forgetlearned ID\n"
            "/setlearnthreshold 50-95 — Similarity threshold\n"
            "/owner — Owner panel\n"
            "/ownerban USER_ID reason /ownerunban USER_ID\n"
            "/ad groups|users|all /adstats — Owner broadcast\n"
            "/backup — Private database backup\n\n"
            "💡 Media reply: reply to a photo/video/GIF/sticker and send /addreply keyword.\n"
            "Some protections require the bot to be a group admin with the relevant permissions."
        )
        await update.effective_message.reply_text(english_help)
        return
    text = (
        "💎 𝗣𝗥𝗢 𝗥𝗘𝗣𝗟𝗬 𝗕𝗢𝗧 — 𝗖𝗢𝗠𝗠𝗔𝗡𝗗 𝗖𝗘𝗡𝗧𝗘𝗥\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Relationship (18+) Commands:\n"
        "/love — Relationship Menu\n"
        "/loveprofile — Profile ထည့်/ပြင် (Private Chat)\n"
        "/lovepublish — Join စစ်ပြီး Profile Post တင်ရန်\n"
        "/lovesearch — Profile ရှာရန်\n"
        "/loveposts on|off — Group Post လက်ခံမှု (Admin)\n"
        "/lovedm on|off — DM Post လက်ခံမှု\n"
        "/loveblock USER_ID /loveunblock USER_ID — Block/Unblock\n"
        "/lovedeleteposts — ကိုယ့် Post များဖျက်ရန်\n"
        "/lovechannel add|remove|list — Owner Channel စီမံရန်\n"
        "/lovereports — Owner Report စာရင်း\n\n"
        "Group Admin Commands:\n"
        "/panel — Dashboard ဖွင့်ရန်\n"
        "/addreply keyword | reply — စာသား Reply ထည့်ရန်\n"
        "Media Reply: ဓာတ်ပုံ/GIF/Video စာကို Reply လုပ်ပြီး /addreply keyword ဟု ပို့ရန်\n"
        "/editreply ID | စာသား — ပြင်ရန်\n"
        "/delreply ID — ဖျက်ရန်\n"
        "/listreplies — စာရင်းကြည့်ရန်\n"
        "/autoreply on|off — ဖွင့်/ပိတ်ရန်\n"
        "/setmode exact|contains|startswith — Keyword စစ်နည်း\n"
        "/setchance 0-100 — Reply ဖြစ်နိုင်ခြေ\n"
        "/setcooldown 0-3600 — User တစ်ယောက်ချင်းစီ စောင့်ချိန်\n"
        "/setlang my|en|both — Help Center ဘာသာစကား\n"
        "/setpriority ID | -100..100 — ဦးစားပေးမှု\n"
        "/setgroupcooldown 0-3600 — Group စောင့်ချိန်\n"
        "/setflood 0-30 — Flood ကာကွယ်မှု\n"
        "/setwelcome on|off — Welcome\n"
        "/setgoodbye on|off — Goodbye\n"
        "/setwelcometext စာသား — Welcome စာသားပြင်ရန်\n"
        "/setgoodbyetext စာသား — Goodbye စာသားပြင်ရန်\n"
        "/welcomepreview / /goodbyepreview — Preview ကြည့်ရန်\n"
        "/resetwelcome / /resetgoodbye — မူလစာသားပြန်ထားရန်\n"
        "/learn on|off — Group စကားမှ သင်ယူမှု ဖွင့်/ပိတ်ရန်\n"
        "/security on|off — Group Security ဖွင့်/ပိတ်ရန်\n"
        "/antilink on|off — Link ဖျက်ခြင်း\n"
        "/antispam on|off — ထပ်ခါထပ်ခါစာ ဖျက်ခြင်း\n"
        "/setraidlimit 3 — Anti-Nuke သတိပေးအဆင့်\n"
        "/addfilterword စာသား — တားမြစ်စာသားထည့်ရန်\n"
        "/listfilterwords — တားမြစ်စာသားစာရင်း\n"
        "/delfilterword ID — တားမြစ်စာသားဖျက်ရန်\n"
        "/adultfilter on|off — 18+ စာသား Filter\n"
        "/warn အကြောင်းပြချက် — Reply လုပ်ထားသူကို Warn\n"
        "/warns — Warn History ကြည့်ရန်\n"
        "/resetwarn — Warn အားလုံးရှင်းရန်\n"
        "/setwarnlimit 3 — Warn အများဆုံးအရေအတွက်\n"
        "/setwarnaction mute|kick|ban — Warn ပြည့်လျှင် အရေးယူမှု\n"
        "/learned — သင်ယူထားသော စကားတွဲကြည့်ရန်\n"
        "/forgetlearned ID — သင်ယူထားသော စကားတွဲဖျက်ရန်\n"
        "/setlearnthreshold 50-95 — တူညီမှုအဆင့် သတ်မှတ်ရန်\n"
        "/replysettings — Setting ကြည့်ရန်\n"
        "/stats — စာရင်းအင်း\n"
        "/ownerban USER_ID အကြောင်းပြချက် — Bot အသုံးပြုခွင့်ပိတ်ရန် (Owner)\n"
        "/ownerunban USER_ID — Bot အသုံးပြုခွင့်ပြန်ဖွင့်ရန် (Owner)\n"
        "/backup — Database Backup ယူရန်\n"
        "/id — User ID / Chat ID ကြည့်ရန်\n"
        "/profile — Member Profile ကြည့်ရန်\n"
        "/report — Reply လုပ်ထားသောစာကို Admin ထံ တိုင်ကြားရန်\n"
        "/reports — Admin အတွက် Report စာရင်း\n"
        "/closereport ID — Report ပိတ်ရန်\n"
        "/afk အကြောင်းပြချက် — AFK ဖွင့်ရန်\n"
        "/note စာသား — ကိုယ်ပိုင်မှတ်စုသိမ်းရန်\n"
        "/notes — မှတ်စုစာရင်း\n"
        "/delnote ID — မှတ်စုဖျက်ရန်\n"
        "/setrules စည်းကမ်း — Group စည်းကမ်းသတ်မှတ်ရန် (Admin)\n"
        "/rules — Group စည်းကမ်းကြည့်ရန်\n"
        "/lock — Group စာပို့ခွင့်ပိတ်ရန် (Admin)\n"
        "/unlock — Group စာပို့ခွင့်ဖွင့်ရန် (Admin)\n\n"
        "🎮 Group Games & Coin Commands:\n"
        "/games — ဂိမ်း Command စာရင်း\n"
        "/balance — Coin / Level ကြည့်ရန်\n"
        "/daily — နေ့စဉ် Coin 50 ယူရန်\n"
        "/topcoins — Coin Top 10\n"
        "/quiz /answer 1-4 — Quiz Battle\n"
        "/fish — ငါးဖမ်းရန်\n"
        "/hunt — Monster တိုက်ရန်\n"
        "/spin — Lucky Spin (12 နာရီတစ်ကြိမ်)\n"
        "/duel — Reply လုပ်ပြီး Duel စိန်ခေါ်ရန်\n"
        "/duelaccept /dueldecline — Duel လက်ခံ/ငြင်းရန်\n"
        "/game on|off — Admin: ဂိမ်းဖွင့်/ပိတ်ရန်\n\n"
        "Keyword ပါဝင်တဲ့ Group စာကို Bot က သတ်မှတ်ထားတဲ့ Reply ဖြင့် ပြန်ဖြေပါမယ်။"
    )
    await update.effective_message.reply_text(text)


async def add_reply(update, context):
    if not await require_admin(update, context):
        return
    msg = update.effective_message
    raw = msg.text.partition(" ")[2].strip()
    if not raw:
        await msg.reply_text("အသုံးပြုပုံ: /addreply keyword | reply\nMedia အတွက် Media ကို Reply လုပ်ပြီး /addreply keyword ဟု ပို့ပါ။")
        return
    if msg.reply_to_message:
        media = media_from_message(msg.reply_to_message)
        if media:
            keyword = raw.split("|", 1)[0].strip()
            if not keyword:
                await msg.reply_text("Keyword ထည့်ပါ။")
                return
            kind, file_id, caption = media
            with db() as conn:
                cur = conn.execute(
                    "INSERT INTO replies(chat_id,keyword,media_type,file_id,caption) VALUES(?,?,?,?,?)",
                    (msg.chat_id, keyword, kind, file_id, caption),
                )
            await msg.reply_text("✅ Media Reply သိမ်းပြီးပါပြီ။ ID: " + str(cur.lastrowid))
            return
    if "|" not in raw:
        await msg.reply_text("အသုံးပြုပုံ: /addreply မင်္ဂလာပါ | မင်္ဂလာပါခင်ဗျာ")
        return
    keyword, response = (x.strip() for x in raw.split("|", 1))
    if not keyword or not response:
        await msg.reply_text("Keyword နဲ့ Reply စာသား နှစ်ခုလုံးလိုပါတယ်။")
        return
    if len(keyword) > 150 or len(response) > 3500:
        await msg.reply_text("Keyword 150 လုံး၊ Reply 3500 လုံးထက် မကျော်ရပါ။")
        return
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO replies(chat_id,keyword,response) VALUES(?,?,?)",
            (msg.chat_id, keyword, response),
        )
    await msg.reply_text("✅ Reply ထည့်ပြီးပါပြီ။ ID: " + str(cur.lastrowid))


async def edit_reply(update, context):
    if not await require_admin(update, context):
        return
    raw = update.effective_message.text.partition(" ")[2].strip()
    if "|" not in raw:
        await update.effective_message.reply_text("အသုံးပြုပုံ: /editreply 3 | ပြင်လိုသောစာသား")
        return
    left, response = (x.strip() for x in raw.split("|", 1))
    if not left.isdigit() or not response:
        await update.effective_message.reply_text("ID နဲ့ စာသား မှန်ကန်စွာ ထည့်ပါ။")
        return
    with db() as conn:
        cur = conn.execute(
            "UPDATE replies SET response=?, media_type='text', file_id='', caption='' WHERE id=? AND chat_id=?",
            (response, int(left), update.effective_chat.id),
        )
    await update.effective_message.reply_text("✏️ ပြင်ပြီးပါပြီ။" if cur.rowcount else "ဒီ ID ကို မတွေ့ပါဘူး။")


async def delete_reply(update, context):
    if not await require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("အသုံးပြုပုံ: /delreply 12")
        return
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM replies WHERE id=? AND chat_id=?",
            (int(context.args[0]), update.effective_chat.id),
        )
    await update.effective_message.reply_text("🗑️ ဖျက်ပြီးပါပြီ။" if cur.rowcount else "ဒီ ID ကို မတွေ့ပါဘူး။")


async def list_replies(update, context):
    if not await require_admin(update, context):
        return
    with db() as conn:
        rows = conn.execute(
            "SELECT id,keyword,response,media_type FROM replies WHERE chat_id=? ORDER BY id DESC LIMIT 50",
            (update.effective_chat.id,),
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text("Reply မရှိသေးပါဘူး။")
        return
    lines = ["📚 Reply စာရင်း (နောက်ဆုံး 50 ခု)"]
    for row in rows:
        desc = row["response"].replace("\n", " ")[:55]
        if row["media_type"] != "text":
            desc = "[" + row["media_type"] + "]"
        lines.append(f'{row["id"]}. {row["keyword"]} → {desc}')
    await update.effective_message.reply_text("\n".join(lines))


async def toggle(update, context):
    if not await require_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text("အသုံးပြုပုံ: /autoreply on သို့မဟုတ် /autoreply off")
        return
    value = int(context.args[0].lower() == "on")
    set_setting(update.effective_chat.id, "enabled", value)
    await update.effective_message.reply_text("✅ Auto Reply ဖွင့်ထားပါပြီ။" if value else "⛔ Auto Reply ပိတ်ထားပါပြီ။")


async def set_mode(update, context):
    if not await require_admin(update, context):
        return
    modes = {"exact", "contains", "startswith"}
    if not context.args or context.args[0].lower() not in modes:
        await update.effective_message.reply_text("အသုံးပြုပုံ: /setmode exact|contains|startswith")
        return
    set_setting(update.effective_chat.id, "match_mode", context.args[0].lower())
    await update.effective_message.reply_text("✅ Keyword စစ်နည်း ပြောင်းပြီးပါပြီ။")


async def set_chance(update, context):
    if not await require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit() or not 0 <= int(context.args[0]) <= 100:
        await update.effective_message.reply_text("အသုံးပြုပုံ: /setchance 80 (0-100)")
        return
    set_setting(update.effective_chat.id, "chance", int(context.args[0]))
    await update.effective_message.reply_text("🎯 Chance ကို " + context.args[0] + "% သတ်မှတ်ပြီးပါပြီ။")


async def set_cooldown(update, context):
    if not await require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit() or not 0 <= int(context.args[0]) <= 3600:
        await update.effective_message.reply_text("အသုံးပြုပုံ: /setcooldown 5 (စက္ကန့်)")
        return
    set_setting(update.effective_chat.id, "cooldown", int(context.args[0]))
    await update.effective_message.reply_text("⏱️ Cooldown သတ်မှတ်ပြီးပါပြီ။")


async def set_lang(update, context):
    if not await require_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("my", "en", "both"):
        await update.effective_message.reply_text("အသုံးပြုပုံ: /setlang my|en|both")
        return
    set_setting(update.effective_chat.id, "language", context.args[0].lower())
    await update.effective_message.reply_text(
        "🌐 Help language: " + {"my": "မြန်မာ", "en": "English", "both": "မြန်မာ + English"}[context.args[0].lower()]
        + " သတ်မှတ်ပြီးပါပြီ။ /help ကို ပြန်ဖွင့်ကြည့်ပါ။"
    )


async def set_priority(update, context):
    if not await require_admin(update, context):
        return
    raw = update.effective_message.text.partition(" ")[2].strip()
    if "|" not in raw:
        await update.effective_message.reply_text("အသုံးပြုပုံ: /setpriority 5 | 20")
        return
    left, right = (x.strip() for x in raw.split("|", 1))
    if not left.isdigit() or not right.lstrip("-").isdigit() or not -100 <= int(right) <= 100:
        await update.effective_message.reply_text("ID မှန်ကန်စွာထည့်ပြီး Priority ကို -100 မှ 100 အတွင်းထားပါ။")
        return
    with db() as conn:
        cur = conn.execute("UPDATE replies SET priority=? WHERE id=? AND chat_id=?", (int(right), int(left), update.effective_chat.id))
    await update.effective_message.reply_text("⭐ Priority ပြောင်းပြီးပါပြီ။" if cur.rowcount else "ဒီ ID ကို မတွေ့ပါဘူး။")


async def set_group_cooldown(update, context):
    if not await require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit() or not 0 <= int(context.args[0]) <= 3600:
        await update.effective_message.reply_text("အသုံးပြုပုံ: /setgroupcooldown 10 (0-3600)")
        return
    set_setting(update.effective_chat.id, "group_cooldown", int(context.args[0]))
    await update.effective_message.reply_text("⏱️ Group Cooldown သတ်မှတ်ပြီးပါပြီ။")


async def set_flood(update, context):
    if not await require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit() or not 0 <= int(context.args[0]) <= 30:
        await update.effective_message.reply_text("အသုံးပြုပုံ: /setflood 6 (0=OFF၊ 1-30)")
        return
    set_setting(update.effective_chat.id, "flood_limit", int(context.args[0]))
    await update.effective_message.reply_text("🛡️ Flood protection ပြင်ဆင်ပြီးပါပြီ။")


async def set_welcome(update, context):
    if not await require_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text("အသုံးပြုပုံ: /setwelcome on|off")
        return
    enabled = int(context.args[0].lower() == "on")
    set_setting(update.effective_chat.id, "welcome_enabled", enabled)
    await update.effective_message.reply_text("👋 Welcome ဖွင့်ပြီးပါပြီ။" if enabled else "Welcome ပိတ်ပြီးပါပြီ။")


async def set_goodbye(update, context):
    if not await require_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text("အသုံးပြုပုံ: /setgoodbye on|off")
        return
    enabled = int(context.args[0].lower() == "on")
    set_setting(update.effective_chat.id, "goodbye_enabled", enabled)
    await update.effective_message.reply_text("🚪 Goodbye ဖွင့်ပြီးပါပြီ။" if enabled else "🚪 Goodbye ပိတ်ပြီးပါပြီ။")


DEFAULT_WELCOME_TEXT = "🎉 မင်္ဂလာပါ {name} ရေ!\n💖 {chat} မှ နွေးထွေးစွာ ကြိုဆိုပါတယ်။\n👥 အဖွဲ့ဝင်အမှတ်: {count}\n📌 စည်းကမ်းများကို လိုက်နာပေးပါနော်။"
DEFAULT_GOODBYE_TEXT = "👋 {name} တစ်ယောက် Group မှ ထွက်ခွာသွားပါပြီ။\n💬 ပြန်လာချင်တဲ့အခါ တံခါးဖွင့်ထားပါတယ်။\n👥 ကျန်ရှိသူ: {count} ဦး"


def render_member_message(template, member, chat, count):
    username = ("@" + member.username) if getattr(member, "username", None) else "မရှိပါ"
    values = {
        "{name}": member.full_name or "မသိရှိသူ",
        "{username}": username,
        "{id}": str(member.id),
        "{chat}": chat.title or "Group",
        "{count}": str(max(0, count)),
    }
    result = template or ""
    for key, value in values.items():
        result = result.replace(key, value)
    return result[:4000]


async def set_member_message_text(update, context, kind):
    if not await require_admin(update, context):
        return
    msg = update.effective_message
    value = msg.text.partition(" ")[2].strip()
    if not value:
        command = "/setwelcometext" if kind == "welcome" else "/setgoodbyetext"
        await msg.reply_text(
            f"အသုံးပြုပုံ: {command} သင့်စာသား\n\n"
            "အသုံးပြုနိုင်သော အစားထိုးစာလုံးများ:\n"
            "{name} — ဝင်လာ/ထွက်သွားသူအမည်\n"
            "{username} — Telegram Username\n"
            "{id} — User ID\n"
            "{chat} — Group အမည်\n"
            "{count} — လက်ရှိ Member အရေအတွက်\n\n"
            "စာသားပြင်ရန် Command နောက်မှာ စာသားရေးပို့ပါ။"
        )
        return
    if len(value) > 3500:
        await msg.reply_text("စာသားသည် ၃၅၀၀ စာလုံးထက် မကျော်ရပါ။")
        return
    column = "welcome_text" if kind == "welcome" else "goodbye_text"
    set_setting(update.effective_chat.id, column, value)
    await msg.reply_text(
        ("🎉 Welcome" if kind == "welcome" else "🚪 Goodbye")
        + " စာသားအသစ်ကို သိမ်းပြီးပါပြီ။\n"
        + "စမ်းကြည့်ရန်: /" + ("welcomepreview" if kind == "welcome" else "goodbyepreview")
    )


async def preview_member_message(update, context, kind):
    if not await require_admin(update, context):
        return
    chat = update.effective_chat
    user = update.effective_user
    settings = get_settings(chat.id)
    column = "welcome_text" if kind == "welcome" else "goodbye_text"
    sample_count = 101 if kind == "welcome" else 100
    rendered = render_member_message(settings[column], user, chat, sample_count)
    await update.effective_message.reply_text(
        ("🎉 Welcome Preview\n\n" if kind == "welcome" else "🚪 Goodbye Preview\n\n")
        + rendered
    )


async def reset_member_message_text(update, context, kind):
    if not await require_admin(update, context):
        return
    column = "welcome_text" if kind == "welcome" else "goodbye_text"
    default = DEFAULT_WELCOME_TEXT if kind == "welcome" else DEFAULT_GOODBYE_TEXT
    set_setting(update.effective_chat.id, column, default)
    await update.effective_message.reply_text("✅ မူလစာသားကို ပြန်ထားပြီးပါပြီ။")


async def member_update(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not is_group(chat):
        return
    # Keep an explicit trace so missed Telegram service updates can be diagnosed in Render logs.
    if msg.new_chat_members:
        logger.info("New-member service update received: chat_id=%s count=%s", chat.id, len(msg.new_chat_members))
    if msg.left_chat_member:
        logger.info("Left-member service update received: chat_id=%s user_id=%s", chat.id, msg.left_chat_member.id)
    try:
        settings = get_settings(chat.id)
        if msg.new_chat_members and settings["welcome_enabled"]:
            for member in msg.new_chat_members:
                if not member.is_bot:
                    try:
                        count = await context.bot.get_chat_member_count(chat.id)
                    except TelegramError:
                        logger.exception("Could not get member count for welcome: chat_id=%s", chat.id)
                        count = 0
                    rendered = render_member_message(settings["welcome_text"], member, chat, count)
                    await msg.reply_text(rendered)
                    logger.info("Welcome message sent: chat_id=%s user_id=%s", chat.id, member.id)
        if msg.left_chat_member and settings["goodbye_enabled"] and not msg.left_chat_member.is_bot:
            try:
                count = await context.bot.get_chat_member_count(chat.id)
            except TelegramError:
                logger.exception("Could not get member count for goodbye: chat_id=%s", chat.id)
                count = 0
            rendered = render_member_message(settings["goodbye_text"], msg.left_chat_member, chat, count)
            await msg.reply_text(rendered)
            logger.info("Goodbye message sent: chat_id=%s user_id=%s", chat.id, msg.left_chat_member.id)
    except Exception:
        logger.exception("Welcome/Goodbye handler failed: chat_id=%s", chat.id)


async def bot_membership_update(update, context):
    """Notify only the configured Bot Owner when this bot joins or is promoted."""
    change = update.my_chat_member
    if not change or not OWNER_ID:
        return
    chat = change.chat
    if not is_group(chat):
        return
    old_status = change.old_chat_member.status
    new_status = change.new_chat_member.status
    joined = old_status in ("left", "kicked") and new_status in ("member", "administrator")
    promoted = old_status in ("member", "restricted") and new_status == "administrator"
    demoted = old_status == "administrator" and new_status == "member"
    removed = old_status in ("member", "administrator") and new_status in ("left", "kicked")
    if not (joined or promoted or demoted or removed):
        return

    actor = change.from_user
    if joined or promoted:
        with db() as conn:
            conn.execute(
                "INSERT INTO known_groups(chat_id,title,first_seen,active) VALUES(?,?,?,1) "
                "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,active=1",
                (chat.id, chat.title or "", time.time()),
            )
    if removed:
        with db() as conn:
            conn.execute("UPDATE known_groups SET active=0 WHERE chat_id=?", (chat.id,))
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"⚠️ Bot ကို Group မှ ဖယ်ရှားလိုက်ပါပြီ။\nGroup: {chat.title or 'Unknown'}\nGroup ID: {chat.id}\nလုပ်ဆောင်သူ: {actor.full_name} ({actor.id})",
            )
        except TelegramError:
            logger.exception("Could not notify Bot Owner about bot removal")
        return
    if demoted:
        try:
            await context.bot.send_message(
                OWNER_ID,
                f"⚠️ Bot ကို Group Admin အဖြစ်မှ ဖြုတ်လိုက်ပါပြီ။\nGroup: {chat.title or 'Unknown'}\nGroup ID: {chat.id}\nလုပ်ဆောင်သူ: {actor.full_name} ({actor.id})",
            )
        except TelegramError:
            logger.exception("Could not notify Bot Owner about bot demotion")
        return
    bot_member = change.new_chat_member
    admin_text = "Admin" if new_status == "administrator" else "Admin မဟုတ်သေးပါ"
    group_link = "မရနိုင်သေးပါ (Private Group သို့မဟုတ် Link မရနိုင်)"
    try:
        if chat.username:
            group_link = "https://t.me/" + chat.username
        elif new_status == "administrator":
            invite = await context.bot.create_chat_invite_link(chat.id, name="Bot Owner group notice")
            group_link = invite.invite_link
    except TelegramError:
        pass

    creator_text = "မရနိုင်သေးပါ"
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        creator = next((m.user for m in admins if m.status == "creator"), None)
        if creator:
            creator_text = f"{creator.full_name} (ID: {creator.id})"
    except TelegramError:
        pass

    actor_text = f"{actor.full_name} (ID: {actor.id})" if actor else "မသိရှိနိုင်ပါ"
    message = (
        "🆕 Bot ကို Group အသစ်ထဲ ထည့်သွင်းလိုက်ပါပြီ။\n\n"
        f"📌 Group: {chat.title or 'Unknown'}\n"
        f"🆔 Group ID: {chat.id}\n"
        f"🔗 Group Link: {group_link}\n"
        f"👑 Group Owner: {creator_text}\n"
        f"➕ Bot ထည့်သူ: {actor_text}\n"
        f"🤖 Bot Status: {admin_text}\n\n"
        "ဤအသိပေးချက်ကို Bot Owner ထံသို့သာ ပို့ထားပါသည်။"
    )
    try:
        await context.bot.send_message(chat_id=OWNER_ID, text=message, disable_web_page_preview=True)
    except TelegramError:
        logger.exception("Could not notify Bot Owner about new group")


async def reply_settings(update, context):
    if not await require_admin(update, context):
        return
    s = get_settings(update.effective_chat.id)
    await update.effective_message.reply_text(
        "⚙️ Group Settings\n"
        + "Auto Reply: " + ("ON" if s["enabled"] else "OFF") + "\n"
        + "Chance: " + str(s["chance"]) + "%\n"
        + "Cooldown: " + str(s["cooldown"]) + " sec\n"
        + "Match: " + s["match_mode"] + "\n"
        + "Language: " + s["language"] + "\n"
        + "Learning: " + ("ON" if get_learning_settings(update.effective_chat.id)["enabled"] else "OFF") + "\n"
        + "Learning threshold: " + str(get_learning_settings(update.effective_chat.id)["threshold"]) + "%"
    )


async def stats(update, context):
    if not await require_admin(update, context):
        return
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) n FROM replies WHERE chat_id=?", (update.effective_chat.id,)).fetchone()["n"]
        hits = conn.execute("SELECT COALESCE(SUM(hits),0) n FROM replies WHERE chat_id=?", (update.effective_chat.id,)).fetchone()["n"]
        top = conn.execute("SELECT keyword,hits FROM replies WHERE chat_id=? ORDER BY hits DESC LIMIT 5", (update.effective_chat.id,)).fetchall()
    top_text = "\n".join(f'{r["keyword"]}: {r["hits"]} ကြိမ်' for r in top) or "မရှိသေးပါ"
    await update.effective_message.reply_text(f"📊 Group Statistics\nReply စည်းမျဉ်း: {count} ခု\nပို့ပြီးအကြိမ်: {hits}\n\n🏆 လူသုံးများသော Keyword\n{top_text}")


async def panel(update, context):
    if not await require_admin(update, context):
        return
    keyboard = [
        [InlineKeyboardButton("📚 Reply စာရင်း", callback_data="panel:list"),
         InlineKeyboardButton("📊 Stats", callback_data="panel:stats")],
        [InlineKeyboardButton("✅ Auto ON", callback_data="panel:on"),
         InlineKeyboardButton("⛔ Auto OFF", callback_data="panel:off")],
        [InlineKeyboardButton("👋 Welcome ON/OFF", callback_data="panel:welcome"),
         InlineKeyboardButton("🚪 Goodbye ON/OFF", callback_data="panel:goodbye")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="panel:settings"),
         InlineKeyboardButton("❔ Help", callback_data="panel:help")],
    ]
    await update.effective_message.reply_text(
        "💎 𝗣𝗥𝗢 𝗥𝗘𝗣𝗟𝗬 — 𝗔𝗗𝗠𝗜𝗡 𝗖𝗢𝗡𝗧𝗥𝗢𝗟\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "အောက်က ခလုတ်များကို ရွေးချယ်ပါ။",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def panel_callback(update, context):
    q = update.callback_query
    chat = q.message.chat
    user = q.from_user
    if OWNER_ID and user.id == OWNER_ID:
        allowed = True
    elif is_group(chat):
        try:
            member = await context.bot.get_chat_member(chat.id, user.id)
            allowed = member.status in ("administrator", "creator")
        except Exception:
            allowed = False
    else:
        allowed = False
    if not allowed:
        await q.answer("Group Admin များသာ အသုံးပြုနိုင်ပါတယ်။", show_alert=True)
        return
    await q.answer()
    action = q.data.split(":", 1)[1]
    if action == "on":
        set_setting(chat.id, "enabled", 1)
        await q.edit_message_text("✅ Auto Reply ဖွင့်ပြီးပါပြီ။")
    elif action == "off":
        set_setting(chat.id, "enabled", 0)
        await q.edit_message_text("⛔ Auto Reply ပိတ်ပြီးပါပြီ။")
    elif action == "list":
        with db() as conn:
            rows = conn.execute("SELECT id,keyword,media_type FROM replies WHERE chat_id=? ORDER BY id DESC LIMIT 30", (chat.id,)).fetchall()
        body = "\n".join(f'{r["id"]}. {r["keyword"]} [{r["media_type"]}]' for r in rows) or "Reply မရှိသေးပါ။"
        await q.edit_message_text("📚 Reply List\n" + body)
    elif action == "stats":
        with db() as conn:
            n = conn.execute("SELECT COUNT(*) n FROM replies WHERE chat_id=?", (chat.id,)).fetchone()["n"]
        await q.edit_message_text("📊 Reply အရေအတွက်: " + str(n))
    elif action == "welcome":
        s = get_settings(chat.id)
        value = 0 if s["welcome_enabled"] else 1
        set_setting(chat.id, "welcome_enabled", value)
        await q.edit_message_text("👋 Welcome: " + ("ON" if value else "OFF"))
    elif action == "goodbye":
        s = get_settings(chat.id)
        value = 0 if s["goodbye_enabled"] else 1
        set_setting(chat.id, "goodbye_enabled", value)
        await q.edit_message_text("🚪 Goodbye: " + ("ON" if value else "OFF"))
    elif action == "settings":
        s = get_settings(chat.id)
        await q.edit_message_text(f'Auto: {s["enabled"]}\nChance: {s["chance"]}%\nUser cooldown: {s["cooldown"]} sec\nGroup cooldown: {s["group_cooldown"]} sec\nFlood limit: {s["flood_limit"]}\nMatch: {s["match_mode"]}\nLanguage: {s["language"]}\nWelcome: {s["welcome_enabled"]}\nGoodbye: {s["goodbye_enabled"]}')
    else:
        await q.edit_message_text("အသုံးပြုနည်းအတွက် /help ကိုနှိပ်ပါ။")


async def import_sqlite_command(update, context):
    user = update.effective_user
    msg = update.effective_message
    if not user or user.id != OWNER_ID:
        await msg.reply_text("⛔ Bot Owner သာ SQLite Backup တင်သွင်းနိုင်ပါတယ်။")
        return
    source = msg.reply_to_message
    document = source.document if source else None
    if not document:
        await msg.reply_text("SQLite Backup ဖိုင်ကို Bot ဆီ ပို့ပြီး အဲဒီဖိုင်ကို Reply လုပ်ကာ /importsqlite ပို့ပါ။")
        return
    filename = (document.file_name or "").lower()
    if not filename.endswith((".db", ".sqlite", ".sqlite3")):
        await msg.reply_text("❌ .db, .sqlite သို့မဟုတ် .sqlite3 ဖိုင်သာ လက်ခံပါတယ်။")
        return
    if update.effective_chat.type != "private":
        await msg.reply_text("လုံခြုံရေးအတွက် Bot Owner ရဲ့ Private Chat ထဲကနေသာ Import လုပ်ပါ။")
        return
    try:
        # Reuse an already-downloaded backup after a failed attempt; never overwrite it.
        if not DB_PATH.exists():
            telegram_file = await context.bot.get_file(document.file_id)
            await telegram_file.download_to_drive(custom_path=str(DB_PATH))
        result = migrate_sqlite_once()
        if result.get("status") == "imported":
            details = chr(10).join(f"{name}: {count}" for name, count in result.get("tables", {}).items())
            await msg.reply_text("✅ SQLite Data ကို PostgreSQL ထဲ ပြောင်းသွင်းပြီးပါပြီ။" + chr(10) + (details or "Table Data မတွေ့ပါ။"))
        elif result.get("status") == "already_done":
            await msg.reply_text("ℹ️ SQLite Data ကို အရင်က ပြောင်းသွင်းပြီးသားပါ။")
        else:
            await msg.reply_text("❌ Import မလုပ်နိုင်ပါ။ " + str(result.get("reason", "Unknown error")))
    except Exception:
        logger.exception("SQLite import failed")
        await msg.reply_text("❌ SQLite Import မအောင်မြင်ပါ။ Render Logs ကို စစ်ဆေးပါ။")


async def backup(update, context):
    user = update.effective_user
    msg = update.effective_message
    if not user or user.id != OWNER_ID:
        await msg.reply_text("⛔ Bot Owner သာ Database Backup ယူနိုင်ပါတယ်။")
        return
    if update.effective_chat.type != "private":
        await msg.reply_text("🔐 လုံခြုံရေးအတွက် Bot ကို Private Chat မှာ /backup ပို့ပါ။")
        return
    try:
        data = BytesIO(export_database_json())
        data.name = "pro-reply-postgresql-backup.json"
        await context.bot.send_document(
            chat_id=OWNER_ID,
            document=data,
            filename=data.name,
            caption="🗄️ PostgreSQL Database Backup",
        )
    except Exception:
        logger.exception("Manual database backup failed")
        await msg.reply_text("❌ Database Backup မအောင်မြင်ပါ။ Render Logs ကို စစ်ဆေးပါ။")


async def automatic_backup(context):
    if not OWNER_ID:
        return
    try:
        data = BytesIO(export_database_json())
        data.name = "automatic-pro-reply-postgresql-backup.json"
        await context.bot.send_document(
            chat_id=OWNER_ID,
            document=data,
            filename=data.name,
            caption="🗄️ Automatic PostgreSQL database backup",
        )
    except Exception:
        logger.exception("Automatic backup failed")




async def set_learning(update, context):
    if not await require_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text("အသုံးပြုပုံ: /learn on သို့မဟုတ် /learn off")
        return
    enabled = int(context.args[0].lower() == "on")
    set_learning_setting(update.effective_chat.id, "enabled", enabled)
    message = "🧠 Learning ON ဖြစ်ပါပြီ။ Group ထဲက လူနှစ်ယောက် ဆက်တိုက်ပြောဆိုတဲ့ စကားတွေကို မေးခွန်း/အဖြေ စကားတွဲအဖြစ် မှတ်သားပါမယ်။ Group အဖွဲ့ဝင်များကို အသိပေးထားပါ။" if enabled else "🧠 Learning OFF ဖြစ်ပါပြီ။ သိမ်းထားပြီးသား စကားတွဲများ မပျက်ပါ။"
    await update.effective_message.reply_text(message)


async def learned_list_command(update, context):
    if not await require_admin(update, context):
        return
    total, rows = list_learned(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text("🧠 သင်ယူထားသော စကားတွဲ မရှိသေးပါ။ /learn on ဖြင့် စတင်ပါ။")
        return
    lines = [f"🧠 သင်ယူထားသော စကားတွဲ ({total} ခု၊ နောက်ဆုံး 30 ခု)"]
    for row in rows:
        lines.append(f'{row["id"]}. {row["prompt"][:45]} → {row["response"][:45]} ({row["hits"]} ကြိမ်)')
    await update.effective_message.reply_text("\n".join(lines))


async def forget_learned_command(update, context):
    if not await require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("အသုံးပြုပုံ: /forgetlearned 12")
        return
    deleted = delete_learned(update.effective_chat.id, int(context.args[0]))
    await update.effective_message.reply_text("🗑️ ဖျက်ပြီးပါပြီ။" if deleted else "ဒီ ID ကို မတွေ့ပါဘူး။")


async def set_learn_threshold(update, context):
    if not await require_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit() or not 50 <= int(context.args[0]) <= 95:
        await update.effective_message.reply_text("အသုံးပြုပုံ: /setlearnthreshold 72 (50-95)")
        return
    set_learning_setting(update.effective_chat.id, "threshold", int(context.args[0]))
    await update.effective_message.reply_text("🎯 Learning တူညီမှုအဆင့်ကို " + context.args[0] + "% သတ်မှတ်ပြီးပါပြီ။")


async def code_create_command(update, context):
    user = update.effective_user
    msg = update.effective_message
    if not user or user.id != OWNER_ID:
        await msg.reply_text("⛔ Coin Code ဖန်တီးခွင့်ကို Bot Owner သာ ရရှိပါတယ်။")
        return
    if not context.args or not context.args[0].isdigit():
        await msg.reply_text(
            "💎 MYAE COIN CODE STUDIO\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "အသုံးပြုပုံ: /codecreate AMOUNT [USES] [EXPIRY_HOURS]\n\n"
            "ဥပမာများ:\n"
            "• /codecreate 5000 — လူတစ်ယောက်သာ ရယူနိုင်\n"
            "• /codecreate 5000 20 — လူ ၂၀ ရယူနိုင်\n"
            "• /codecreate 10000 50 24 — လူ ၅၀၊ ၂၄ နာရီအတွင်းသာ\n\n"
            "Code တစ်ခုကို User တစ်ယောက်လျှင် တစ်ကြိမ်သာ ရယူနိုင်ပါတယ်။\n"
            "ဖန်တီးပြီးတာနဲ့ Bot ရှိတဲ့ Group အားလုံးကို အလိုအလျောက် ပို့ပေးပါမယ်။"
        )
        return
    amount = int(context.args[0])
    uses = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else (1 if len(context.args) == 1 else 0)
    expiry = int(context.args[2]) if len(context.args) > 2 and context.args[2].isdigit() else (0 if len(context.args) < 3 else -1)
    if not 1 <= amount <= 1_000_000_000 or not 1 <= uses <= 100_000 or not 0 <= expiry <= 8760:
        await msg.reply_text("❌ Amount 1–1,000,000,000၊ Uses 1–100,000၊ Expiry 0–8760 နာရီ အတွင်း ထည့်ပါ။")
        return
    try:
        code = create_coin_code(amount, uses, user.id, expiry)
    except Exception:
        logger.exception("Coin code creation failed")
        await msg.reply_text("❌ Coin Code ဖန်တီးမရပါ။ Database ကို စစ်ဆေးပါ။")
        return

    expiry_text = f"{expiry} နာရီ" if expiry else "သက်တမ်းမကုန်"
    announcement = (
        "✨ 𝗠𝗬𝗔𝗘 • GLOBAL COIN DROP ✨\n"
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        f"💎 Reward: +{amount:,} COINS\n"
        f"🎟️ အသုံးပြုခွင့်: {uses:,} ယောက်\n"
        f"⏳ သက်တမ်း: {expiry_text}\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
        "🎁 COIN REWARD CODE\n"
        f"<code>{code}</code>\n\n"
        "🎯 ရယူရန် အောက်က Command ကို ပို့ပါ။\n"
        f"<code>/redeem {code}</code>\n\n"
        "⚠️ လူဦးရေကန့်သတ်ချက် ပြည့်သွားရင် ရယူလို့မရတော့ပါဘူး။"
    )

    # Send the code to the chat where the owner created it, then to every
    # other group currently recorded as active in known_groups.
    try:
        await msg.reply_text(announcement, parse_mode="HTML")
    except TelegramError:
        logger.exception("Could not send coin code announcement to creator chat")
        return

    with db() as conn:
        groups = [row["chat_id"] for row in conn.execute(
            "SELECT chat_id FROM known_groups WHERE active=1"
        ).fetchall()]

    current_chat = update.effective_chat.id if update.effective_chat else None
    sent = 1 if current_chat in groups else 0
    failed = 0
    for chat_id in groups:
        if chat_id == current_chat:
            continue
        for attempt in range(3):
            try:
                await context.bot.send_message(chat_id=chat_id, text=announcement, parse_mode="HTML")
                sent += 1
                break
            except RetryAfter as exc:
                if attempt == 2:
                    failed += 1
                else:
                    await asyncio.sleep(float(exc.retry_after) + 0.5)
            except Forbidden:
                failed += 1
                with db() as conn:
                    conn.execute("UPDATE known_groups SET active=0 WHERE chat_id=?", (chat_id,))
                break
            except TelegramError:
                failed += 1
                logger.exception("Coin code broadcast failed for group %s", chat_id)
                break
        await asyncio.sleep(0.1)

    await msg.reply_text(
        "📡 𝗠𝗬𝗔𝗘 • GLOBAL COIN DROP REPORT\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ ပို့နိုင်သော Group: {sent:,}\n"
        f"❌ မပို့နိုင်သော Group: {failed:,}\n"
        f"🎯 စာရင်းရှိ Active Group: {len(groups):,}\n\n"
        "Member များသည် /redeem CODE ဖြင့် Coin ရယူနိုင်ပါပြီ။"
    )


async def redeem_code_command(update, context):
    user = update.effective_user
    msg = update.effective_message
    if not user or not context.args:
        await msg.reply_text("🎁 Code ရယူရန်: /redeem MYAE-XXXX-XXXX-XXXX")
        return
    result = redeem_coin_code(context.args[0], user.id)
    if not result["ok"]:
        messages = {
            "invalid": "❌ ဒီ Coin Code ကို ရှာမတွေ့ပါ။ စာလုံး/နံပါတ် ပြန်စစ်ပါ။",
            "inactive": "⛔ ဒီ Code ကို ပိတ်ထားပါတယ်။",
            "expired": "⌛ ဒီ Code သက်တမ်းကုန်သွားပါပြီ။",
            "used_up": "🎟️ ဒီ Code အသုံးပြုခွင့် ပြည့်သွားပါပြီ။",
            "already": "⚠️ ဒီ Code ကို သင် အရင်က ရယူပြီးသားပါ။",
        }
        await msg.reply_text("💗 MYAE COIN REDEEM\n" + messages.get(result["reason"], "❌ Code အသုံးမပြုနိုင်ပါ။"))
        return
    await msg.reply_text(
        "🎉 𝗖𝗢𝗜𝗡 𝗖𝗢𝗗𝗘 𝗥𝗘𝗗𝗘𝗘𝗠𝗘𝗗! 🎉\n"
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        f"💎 ရရှိသော Coin: +{result['amount']:,}\n"
        f"🪙 လက်ကျန် Coin: {result['balance']:,}\n"
        f"🎟️ Code ကျန်အသုံးပြုခွင့်: {result['remaining']:,}\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n"
        "💗 မြအေးနဲ့ ဂိမ်းဆက်ဆော့လို့ရပါပြီ။ /games"
    )


async def code_revoke_command(update, context):
    user = update.effective_user
    msg = update.effective_message
    if not user or user.id != OWNER_ID:
        await msg.reply_text("⛔ Bot Owner သာ Code ပိတ်နိုင်ပါတယ်။")
        return
    if update.effective_chat.type != "private":
        await msg.reply_text("🔐 Private Chat မှာ /coderevoke CODE သုံးပါ။")
        return
    if not context.args:
        await msg.reply_text("အသုံးပြုပုံ: /coderevoke MYAE-XXXX-XXXX-XXXX")
        return
    ok = revoke_coin_code(context.args[0])
    await msg.reply_text("🛑 Coin Code ကို ပိတ်ပြီးပါပြီ။" if ok else "❌ အသုံးပြုနိုင်ဆဲ Code မတွေ့ပါ။")


async def code_stats_command(update, context):
    user = update.effective_user
    msg = update.effective_message
    if not user or user.id != OWNER_ID:
        await msg.reply_text("⛔ Bot Owner သာ စာရင်းကြည့်နိုင်ပါတယ်။")
        return
    stats = coin_code_stats()
    await msg.reply_text(
        "📊 MYAE COIN CODE STATISTICS\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🎟️ ဖန်တီးထားသော Code: {stats['total']:,}\n"
        f"🟢 အသုံးပြုနိုင်ဆဲ Code: {stats['active']:,}\n"
        f"👥 Redeem အကြိမ်: {stats['redeemed']:,}\n"
        f"💎 ဖြန့်ဝေပြီး Coin: {stats['coins_sent']:,}"
    )


async def handle_text(update, context):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not msg.text or not is_group(chat) or not user or user.is_bot:
        return

    # Respond whenever a group member calls the bot by its chosen name.
    normalized_text = " ".join(msg.text.casefold().split())
    if "မြအေး" in normalized_text:
        call_replies = [
            "ရှင် 😄 မြအေး ဒီမှာရှိတယ်နော်။ စကားပြောလို့ရတယ်၊ Coin နဲ့ ဂိမ်းတွေလည်း အတူဆော့လို့ရတယ်။ /games လို့ရိုက်ပြီး ဂိမ်းတွေကြည့်လိုက်နော် 💗",
            "ဟုတ်ကဲ့ရှင် 💗 မြအေးကို ခေါ်တာလား။ ဘာပြောမလဲ။ Quiz / Spin / Fish / Hunt / Mines / Duel လည်း အတူဆော့ကြမယ်နော်။",
            "မြအေးရှိပါတယ်ရှင် 🥰 စကားလာပြောလို့ရတယ်နော်။ ဂိမ်းဆော့ချင်ရင် /games ကိုနှိပ်လိုက်ပါ။",
        ]
        await msg.reply_text(random.choice(call_replies), reply_to_message_id=msg.message_id)
        return

    s = get_settings(chat.id)
    now = time.time()
    limit = int(s.get("flood_limit", 0))
    if s["enabled"] and limit:
        queue = FLOOD_TRACKER[(chat.id, user.id)]
        while queue and now - queue[0] > 8:
            queue.popleft()
        queue.append(now)
        if len(queue) > limit:
            return
    learning = get_learning_settings(chat.id)
    if learning["enabled"]:
        try:
            learned = find_learned_reply(chat.id, msg.text, learning["threshold"])
            if learned and s["enabled"] and random.randint(1, 100) <= s["chance"]:
                with db() as conn:
                    last = conn.execute("SELECT last_reply FROM usage WHERE chat_id=? AND user_id=?", (chat.id, user.id)).fetchone()
                cooldown_ok = not last or now - last["last_reply"] >= s["cooldown"]
                group_cd = int(s.get("group_cooldown", 0))
                group_ok = not group_cd or now - GROUP_LAST_REPLY[chat.id] >= group_cd
                if cooldown_ok and group_ok:
                    await msg.reply_text(learned["response"], reply_to_message_id=msg.message_id)
                    GROUP_LAST_REPLY[chat.id] = now
                    with db() as conn:
                        conn.execute("UPDATE learned_pairs SET hits=hits+1 WHERE id=?", (learned["id"],))
                        conn.execute(
                            "INSERT INTO usage(chat_id,user_id,last_reply) VALUES(?,?,?) "
                            "ON CONFLICT(chat_id,user_id) DO UPDATE SET last_reply=excluded.last_reply",
                            (chat.id, user.id, now),
                        )
                    clear_pending(chat.id)
                    return
            replied_user = msg.reply_to_message.from_user if msg.reply_to_message else None
            record_conversation(
                chat.id, user.id, msg.text,
                reply_to_user_id=(replied_user.id if replied_user else None),
            )
        except Exception:
            logger.exception("Conversation learning failed")
    if not s["enabled"] or random.randint(1, 100) > s["chance"]:
        return
    with db() as conn:
        last = conn.execute("SELECT last_reply FROM usage WHERE chat_id=? AND user_id=?", (chat.id, user.id)).fetchone()
        if last and now - last["last_reply"] < s["cooldown"]:
            return
        rows = conn.execute("SELECT * FROM replies WHERE chat_id=? ORDER BY priority DESC,id DESC", (chat.id,)).fetchall()
    group_cd = int(s.get("group_cooldown", 0))
    if group_cd and now - GROUP_LAST_REPLY[chat.id] < group_cd:
        return
    incoming = " ".join(msg.text.casefold().split())
    matches = []
    for row in rows:
        key = " ".join(row["keyword"].casefold().split())
        if not key:
            continue
        mode = s["match_mode"]
        if mode == "exact":
            matched = incoming == key
            score = 10000 + len(key) if matched else -1
        elif mode == "startswith":
            matched = incoming.startswith(key)
            score = 5000 + len(key) if matched else -1
        else:
            # Whole phrase matching prevents short fragments matching inside unrelated words.
            matched = key in incoming
            score = 1000 + len(key) if matched else -1
        if matched:
            matches.append((int(row["priority"]), score, -int(row["id"]), row))
    if not matches:
        return
    # Highest priority first; then strongest/most specific keyword; stable ID breaks ties.
    selected = max(matches, key=lambda item: item[:3])[3]
    try:
        await send_reply(msg, selected)
        GROUP_LAST_REPLY[chat.id] = now
        with db() as conn:
            conn.execute("UPDATE replies SET hits=hits+1 WHERE id=?", (selected["id"],))
            conn.execute(
                "INSERT INTO usage(chat_id,user_id,last_reply) VALUES(?,?,?) "
                "ON CONFLICT(chat_id,user_id) DO UPDATE SET last_reply=excluded.last_reply",
                (chat.id, user.id, now),
            )
    except Exception:
        logger.exception("Auto reply failed")



async def adult_filter_command(update, context):
    if not await require_admin(update, context): return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text("အသုံးပြုပုံ: /adultfilter on|off"); return
    value = int(context.args[0].lower() == "on")
    set_moderation_setting(update.effective_chat.id, "adult_filter", value)
    await update.effective_message.reply_text("🔞 18+ စာသား Filter ဖွင့်ပြီးပါပြီ။" if value else "18+ Filter ပိတ်ပြီးပါပြီ။")

async def warn_command(update, context):
    if not await require_admin(update, context): return
    await manual_warn(update, context)

async def warns_command(update, context):
    if not await require_admin(update, context): return
    await show_warnings(update, context)

async def reset_warn_command(update, context):
    if not await require_admin(update, context): return
    await reset_warnings(update, context)

async def set_warn_limit_command(update, context):
    if not await require_admin(update, context): return
    if not context.args or not context.args[0].isdigit() or not 2 <= int(context.args[0]) <= 20:
        await update.effective_message.reply_text("အသုံးပြုပုံ: /setwarnlimit 3 (2-20)"); return
    set_moderation_setting(update.effective_chat.id, "warn_limit", int(context.args[0]))
    await update.effective_message.reply_text("⚠️ Warn limit ပြင်ပြီးပါပြီ။")

async def set_warn_action_command(update, context):
    if not await require_admin(update, context): return
    if not context.args or context.args[0].lower() not in ("mute", "kick", "ban"):
        await update.effective_message.reply_text("အသုံးပြုပုံ: /setwarnaction mute|kick|ban"); return
    set_moderation_setting(update.effective_chat.id, "warn_action", context.args[0].lower())
    await update.effective_message.reply_text("⚖️ Warn ပြည့်လျှင် အရေးယူမည့်ပုံစံ ပြင်ပြီးပါပြီ။")

async def security_command(update, context):
    if not await require_admin(update, context): return
    if not context.args or context.args[0].lower() not in ("on","off"):
        await update.effective_message.reply_text("အသုံးပြုပုံ: /security on|off")
        return
    value=int(context.args[0].lower()=="on")
    set_security(update.effective_chat.id,"enabled",value)
    await update.effective_message.reply_text("🛡️ Group Security ဖွင့်ပြီးပါပြီ။" if value else "Group Security ပိတ်ပြီးပါပြီ။")

async def antilink_command(update, context):
    if not await require_admin(update, context): return
    if not context.args or context.args[0].lower() not in ("on","off"):
        await update.effective_message.reply_text("အသုံးပြုပုံ: /antilink on|off"); return
    value=int(context.args[0].lower()=="on")
    set_security(update.effective_chat.id,"anti_link",value)
    await update.effective_message.reply_text("🔗 Anti-Link ပြင်ဆင်ပြီးပါပြီ။")

async def antispam_command(update, context):
    if not await require_admin(update, context): return
    if not context.args or context.args[0].lower() not in ("on","off"):
        await update.effective_message.reply_text("အသုံးပြုပုံ: /antispam on|off"); return
    value=int(context.args[0].lower()=="on")
    set_security(update.effective_chat.id,"anti_spam",value)
    await update.effective_message.reply_text("🚫 Anti-Spam ပြင်ဆင်ပြီးပါပြီ။")

async def set_raid_limit_command(update, context):
    if not await require_admin(update, context): return
    if not context.args or not context.args[0].isdigit() or not 2 <= int(context.args[0]) <= 20:
        await update.effective_message.reply_text("အသုံးပြုပုံ: /setraidlimit 3 (2-20 ကြိမ်/60 စက္ကန့်)"); return
    set_security(update.effective_chat.id,"raid_limit",int(context.args[0]))
    await update.effective_message.reply_text("🚨 Anti-Nuke သတိပေးအဆင့် ပြင်ပြီးပါပြီ။")


async def add_filter_word_command(update, context):
    if not await require_admin(update, context): return
    word=update.effective_message.text.partition(" ")[2].strip()
    if not word:
        await update.effective_message.reply_text("အသုံးပြုပုံ: /addfilterword စစ်ဆေးလိုသောစာသား")
        return
    ok=add_blocked_word(update.effective_chat.id,word)
    await update.effective_message.reply_text("🚫 Filter စာသားထည့်ပြီးပါပြီ။" if ok else "စာသားမမှန်ပါ သို့မဟုတ် ရှိပြီးသားပါ။")

async def list_filter_words_command(update, context):
    if not await require_admin(update, context): return
    rows=list_blocked_words(update.effective_chat.id)
    await update.effective_message.reply_text("🚫 Filter စာရင်း\n"+("\n".join(f'{r["id"]}. {r["word"]}' for r in rows) or "မရှိသေးပါ။"))

async def delete_filter_word_command(update, context):
    if not await require_admin(update, context): return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("အသုံးပြုပုံ: /delfilterword ID"); return
    ok=delete_blocked_word(update.effective_chat.id,int(context.args[0]))
    await update.effective_message.reply_text("🗑️ Filter ဖျက်ပြီးပါပြီ။" if ok else "ID မတွေ့ပါ။")

async def owner_security_callback(update, context):
    handled=await owner_raid_decision(update,context,OWNER_ID)
    if not handled:
        await update.callback_query.answer()

async def post_shutdown(app):
    """Return PostgreSQL pool resources cleanly when the service shuts down."""
    try:
        from database import close_pool
        close_pool()
    except Exception:
        logger.exception("Could not close PostgreSQL connection pool")


async def post_init(app):
    # Keep the Telegram profile identity synchronized with the in-chat persona.
    try:
        await app.bot.set_my_name("မြအေး")
        await app.bot.set_my_description(
            "မြအေးပါ 💗 Group ထဲမှာ စကားပြောနိုင်ပြီး Coin စနစ်နဲ့ Quiz, Spin, Fish, Hunt, Mines, Duel ဂိမ်းတွေ ဆော့နိုင်ပါတယ်။"
        )
        await app.bot.set_my_short_description(
            "မြအေးနဲ့ စကားပြောမယ်၊ Coin နဲ့ ဂိမ်းတွေဆော့မယ် 💗"
        )
    except TelegramError:
        logger.exception("Could not update Telegram bot profile name/description")

    await app.bot.set_my_commands([
        ("start", "Start bot"),
        ("owner", "Bot Owner control center"),
        ("ownerban", "Owner: block a user from using the bot"),
        ("ownerunban", "Owner: restore bot access"),
        ("love", "Relationship system menu"),
        ("loveprofile", "Create or edit 18+ profile"),
        ("lovepublish", "Publish your relationship profile"),
        ("lovesearch", "Find relationship profiles"),
        ("loveposts", "Group relationship post opt-in"),
        ("lovedm", "Relationship DM opt-in"),
        ("loveblock", "Block a relationship profile"),
        ("loveunblock", "Unblock a relationship profile"),
        ("lovedeleteposts", "Delete your relationship posts"),
        ("lovechannel", "Owner: configure required channels"),
        ("lovechannels", "Owner: list required channels"),
        ("lovereports", "Owner: review relationship reports"),
        ("importsqlite", "Bot Owner: import old SQLite backup"),
        ("ad", "Bot Owner: broadcast an ad"),
        ("adstats", "Bot Owner: advertising targets"),
        ("ads", "Manage private ad messages"),
        ("help", "Help and commands"),
        ("games", "Group games and coin commands"),
        ("redeem", "Redeem a promotional coin code"),
        ("codecreate", "Owner: create coin reward codes"),
        ("coderevoke", "Owner: disable a coin code"),
        ("codestats", "Owner: coin code statistics"),
        ("balance", "Show your game coins, level and rank"),
        ("rank", "Show player rank profile"),
        ("toprank", "XP and rank leaderboard"),
        ("daily", "Claim daily game coins"),
        ("topcoins", "Global coin leaderboard"),
        ("quiz", "Start a group quiz"),
        ("answer", "Answer the active quiz"),
        ("fish", "Go fishing for coins"),
        ("hunt", "Fight a monster for rewards"),
        ("mines", "Play the 5x5 minefield"),
        ("pick", "Open a minefield cell 1-25"),
        ("cashout", "Collect minefield winnings"),
        ("spin", "Use the lucky spin"),
        ("duel", "Challenge a member to a duel"),
        ("duelaccept", "Accept a duel challenge"),
        ("dueldecline", "Decline a duel challenge"),
        ("rpg", "Show your RPG profile"),
        ("rpgdaily", "Claim RPG login streak rewards"),
        ("adventure", "Explore a solo RPG dungeon"),
        ("rpgtop", "Show the RPG XP leaderboard"),
        ("game", "Admin: enable or disable group games"),
        ("panel", "Admin dashboard"),
        ("addreply", "Add keyword reply"),
        ("editreply", "Edit a saved reply"),
        ("delreply", "Delete a saved reply"),
        ("listreplies", "List replies"),
        ("autoreply", "Enable or disable auto replies"),
        ("setmode", "Set keyword matching mode"),
        ("setchance", "Set auto reply probability"),
        ("setcooldown", "Set per-user cooldown"),
        ("setlang", "Set help language"),
        ("setpriority", "Set reply priority"),
        ("adultfilter", "Toggle 18+ text filter"),
        ("warn", "Warn a replied-to member"),
        ("warns", "Show warning history"),
        ("resetwarn", "Reset warning history"),
        ("setwarnlimit", "Set warning threshold"),
        ("setwarnaction", "Set automatic warning action"),
        ("setflood", "Set flood protection"),
        ("setwelcome", "Toggle welcome messages"),
        ("setgoodbye", "Toggle goodbye messages"),
        ("setwelcometext", "Customize welcome message"),
        ("setgoodbyetext", "Customize goodbye message"),
        ("welcomepreview", "Preview welcome message"),
        ("goodbyepreview", "Preview goodbye message"),
        ("resetwelcome", "Reset welcome message"),
        ("resetgoodbye", "Reset goodbye message"),
        ("setgroupcooldown", "Set group cooldown"),
        ("replysettings", "Show auto reply settings"),
        ("stats", "Show group reply statistics"),
        ("backup", "Owner: download database backup"),
        ("security", "Toggle group security"),
        ("antilink", "Delete unauthorized links"),
        ("antispam", "Delete repeated spam"),
        ("setraidlimit", "Set anti-nuke alert threshold"),
        ("addfilterword", "Add blocked phrase"),
        ("listfilterwords", "List blocked phrases"),
        ("delfilterword", "Delete blocked phrase"),
        ("learn", "Toggle conversation learning"),
        ("learned", "List learned conversations"),
        ("forgetlearned", "Delete learned pair"),
        ("setlearnthreshold", "Set learning similarity"),
        ("id", "Show Telegram IDs"),
        ("profile", "Show member profile"),
        ("report", "Report a message to group admins"),
        ("afk", "Set AFK status"),
        ("note", "Save a group note"),
        ("notes", "List your saved notes"),
        ("delnote", "Delete one of your notes"),
        ("reports", "Admin: review open reports"),
        ("closereport", "Admin: close a report"),
        ("setrules", "Admin: set group rules"),
        ("rules", "Show group rules"),
        ("lock", "Admin: lock group messages"),
        ("unlock", "Admin: unlock group messages"),
    ])
    if OWNER_ID:
        app.job_queue.run_repeating(
            automatic_backup,
            interval=BACKUP_INTERVAL_HOURS * 3600,
            first=BACKUP_INTERVAL_HOURS * 3600,
            name="automatic-database-backup",
        )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")
    render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if not render_url:
        raise RuntimeError("RENDER_EXTERNAL_URL is missing. Deploy this bot as a Render Web Service.")
    init_db()
    init_learning_db()
    init_security_db()
    init_moderation_db()
    init_member_features()
    init_group_features()
    init_owner_control()
    init_relationship()
    init_games()
    init_rpg()
    init_rpg_expansion()
    init_coin_codes()
    migration = migrate_sqlite_once()
    logger.info("SQLite migration result: %s", migration)
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(12).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(TypeHandler(Update, global_access_guard), group=-20)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(start_menu_callback, pattern=r"^start:"))
    app.add_handler(CallbackQueryHandler(rpg_menu_callback, pattern=r"^rpg:"))
    app.add_handler(CallbackQueryHandler(mine_callback, pattern=r"^mine:"))
    app.add_handler(CommandHandler("owner", lambda u, c: owner_command(u, c, OWNER_ID)))
    app.add_handler(CommandHandler("ownerban", lambda u, c: owner_ban_command(u, c, OWNER_ID)))
    app.add_handler(CommandHandler("ownerunban", lambda u, c: owner_unban_command(u, c, OWNER_ID)))
    for love_handler in relationship_handlers():
        app.add_handler(love_handler)
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("reports", reports_command))
    app.add_handler(CommandHandler("closereport", close_report_command))
    app.add_handler(CommandHandler("afk", afk_command))
    app.add_handler(CommandHandler("note", note_command))
    app.add_handler(CommandHandler("notes", notes_command))
    app.add_handler(CommandHandler("delnote", delnote_command))
    app.add_handler(CommandHandler("setrules", set_rules_command))
    app.add_handler(CommandHandler("rules", rules_command))
    app.add_handler(CommandHandler("lock", lock_command))
    app.add_handler(CommandHandler("unlock", unlock_command))
    app.add_handler(CommandHandler("ad", owner_ad_command))
    app.add_handler(CommandHandler("adstats", owner_ad_stats))
    app.add_handler(CommandHandler("ads", ads_command))
    app.add_handler(MessageHandler(filters.ALL, track_chats), group=-10)
    app.add_handler(MessageHandler(filters.ALL, check_afk_mention), group=-9)
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("games", game_help))
    app.add_handler(CommandHandler("rpg", rpg_profile))
    app.add_handler(CommandHandler("missions", missions_command))
    app.add_handler(CommandHandler("missionclaim", mission_claim_command))
    app.add_handler(CommandHandler("achievements", achievements_command))
    app.add_handler(CommandHandler("shop", shop_command))
    app.add_handler(CommandHandler("buy", buy_command))
    app.add_handler(CommandHandler("inventory", inventory_command))
    app.add_handler(CommandHandler("equip", equip_command))
    app.add_handler(CommandHandler("rpgdaily", rpg_daily_command))
    app.add_handler(CommandHandler("adventure", adventure_command))
    app.add_handler(CommandHandler("rpgtop", rpg_top_command))
    app.add_handler(CommandHandler("guildcreate", guild_create_command))
    app.add_handler(CommandHandler("guildjoin", guild_join_command))
    app.add_handler(CommandHandler("guildleave", guild_leave_command))
    app.add_handler(CommandHandler("guild", guild_command))
    app.add_handler(CommandHandler("guildtop", guild_top_command))
    app.add_handler(CommandHandler("gear", gear_command))
    app.add_handler(CommandHandler("gearbuy", gear_buy_command))
    app.add_handler(CommandHandler("gearequip", gear_equip_command))
    app.add_handler(CommandHandler("boss", world_boss_command))
    app.add_handler(CommandHandler("bossattack", world_boss_attack_command))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("rank", rank))
    app.add_handler(CommandHandler("toprank", toprank))
    app.add_handler(CommandHandler("daily", daily))
    app.add_handler(CommandHandler("topcoins", topcoins))
    app.add_handler(CommandHandler("redeem", redeem_code_command))
    app.add_handler(CommandHandler("codecreate", code_create_command))
    app.add_handler(CommandHandler("coderevoke", code_revoke_command))
    app.add_handler(CommandHandler("codestats", code_stats_command))
    app.add_handler(CommandHandler("quiz", quiz))
    app.add_handler(CommandHandler("answer", answer))
    app.add_handler(CommandHandler("fish", fish))
    app.add_handler(CommandHandler("hunt", hunt))
    app.add_handler(CommandHandler("mines", mines))
    app.add_handler(CommandHandler("pick", pick_mine))
    app.add_handler(CommandHandler("cashout", cashout_mines))
    app.add_handler(CommandHandler("spin", spin))
    app.add_handler(CommandHandler("duel", duel))
    app.add_handler(CommandHandler("duelaccept", duel_accept))
    app.add_handler(CommandHandler("dueldecline", duel_decline))
    app.add_handler(CommandHandler("addcoins", addcoins))
    app.add_handler(CommandHandler("takecoins", takecoins))
    app.add_handler(CommandHandler("game", game_toggle))
    app.add_handler(CommandHandler("panel", panel))
    app.add_handler(CommandHandler("addreply", add_reply))
    app.add_handler(CommandHandler("editreply", edit_reply))
    app.add_handler(CommandHandler("delreply", delete_reply))
    app.add_handler(CommandHandler("listreplies", list_replies))
    app.add_handler(CommandHandler("autoreply", toggle))
    app.add_handler(CommandHandler("setmode", set_mode))
    app.add_handler(CommandHandler("setchance", set_chance))
    app.add_handler(CommandHandler("setcooldown", set_cooldown))
    app.add_handler(CommandHandler("setlang", set_lang))
    app.add_handler(CommandHandler("setpriority", set_priority))
    app.add_handler(CommandHandler("setgroupcooldown", set_group_cooldown))
    app.add_handler(CommandHandler("setflood", set_flood))
    app.add_handler(CommandHandler("setwelcome", set_welcome))
    app.add_handler(CommandHandler("setgoodbye", set_goodbye))
    app.add_handler(CommandHandler("setwelcometext", lambda u, ctx: set_member_message_text(u, ctx, "welcome")))
    app.add_handler(CommandHandler("setgoodbyetext", lambda u, ctx: set_member_message_text(u, ctx, "goodbye")))
    app.add_handler(CommandHandler("welcomepreview", lambda u, ctx: preview_member_message(u, ctx, "welcome")))
    app.add_handler(CommandHandler("goodbyepreview", lambda u, ctx: preview_member_message(u, ctx, "goodbye")))
    app.add_handler(CommandHandler("resetwelcome", lambda u, ctx: reset_member_message_text(u, ctx, "welcome")))
    app.add_handler(CommandHandler("resetgoodbye", lambda u, ctx: reset_member_message_text(u, ctx, "goodbye")))
    app.add_handler(CommandHandler("adultfilter", adult_filter_command))
    app.add_handler(CommandHandler("warn", warn_command))
    app.add_handler(CommandHandler("warns", warns_command))
    app.add_handler(CommandHandler("resetwarn", reset_warn_command))
    app.add_handler(CommandHandler("setwarnlimit", set_warn_limit_command))
    app.add_handler(CommandHandler("setwarnaction", set_warn_action_command))
    app.add_handler(CommandHandler("security", security_command))
    app.add_handler(CommandHandler("antilink", antilink_command))
    app.add_handler(CommandHandler("antispam", antispam_command))
    app.add_handler(CommandHandler("setraidlimit", set_raid_limit_command))
    app.add_handler(CommandHandler("addfilterword", add_filter_word_command))
    app.add_handler(CommandHandler("listfilterwords", list_filter_words_command))
    app.add_handler(CommandHandler("delfilterword", delete_filter_word_command))
    app.add_handler(CommandHandler("learn", set_learning))
    app.add_handler(CommandHandler("learned", learned_list_command))
    app.add_handler(CommandHandler("forgetlearned", forget_learned_command))
    app.add_handler(CommandHandler("setlearnthreshold", set_learn_threshold))
    app.add_handler(CommandHandler("replysettings", reply_settings))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("backup", backup))
    app.add_handler(CommandHandler("importsqlite", import_sqlite_command))
    app.add_handler(CallbackQueryHandler(owner_security_callback, pattern=r"^raid:"))
    app.add_handler(CallbackQueryHandler(lambda u, c: owner_callback(u, c, OWNER_ID), pattern=r"^owner:"))
    app.add_handler(CallbackQueryHandler(panel_callback, pattern=r"^panel:"))
    app.add_handler(ChatMemberHandler(lambda u, ctx: monitor_member_change(u, ctx, OWNER_ID), ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(bot_membership_update, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.CAPTION, lambda u, ctx: moderate_content(u, ctx, OWNER_ID)), group=-2)
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.CAPTION, lambda u, ctx: moderate_message(u, ctx, OWNER_ID), block=False), group=-1)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER, member_update), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    webhook_path = hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest()
    port = int(os.getenv("PORT", "10000"))
    logger.info("PRO Reply Bot Ultimate X starting in webhook mode")
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=webhook_path,
        webhook_url=f"{render_url}/{webhook_path}",
        secret_token=webhook_path,
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=-1,
    )


if __name__ == "__main__":
    main()
