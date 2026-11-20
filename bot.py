import hashlib
import asyncio
import logging
import os
import random
import time
from collections import defaultdict, deque
from pathlib import Path
from database import connect as db, DB_PATH, migrate_sqlite_once

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError, RetryAfter, Forbidden
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ChatMemberHandler, filters,
)
from security import init_security_db, get_security, set_security, moderate_message, monitor_member_change, owner_raid_decision, add_blocked_word, list_blocked_words, delete_blocked_word
from moderation import init_moderation_db, get_settings as get_moderation_settings, set_setting as set_moderation_setting, moderate_content, manual_warn, show_warnings, reset_warnings
from learning import (
    init_learning_db, get_learning_settings, set_learning_setting,
    record_conversation, find_learned_reply, list_learned,
    delete_learned, clear_pending,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "7974865879") or 7974865879)
BACKUP_INTERVAL_HOURS = max(1, int(os.getenv("BACKUP_INTERVAL_HOURS", "24")))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("pro-reply-bot")
FLOOD_TRACKER = defaultdict(deque)
GROUP_LAST_REPLY = defaultdict(float)


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user and update.effective_chat and update.effective_chat.type == "private":
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO known_users(user_id,first_seen) VALUES(?,?)", (user.id, time.time()))
    await update.effective_message.reply_text(
        "🤖 PRO Reply Bot Ultimate X\n"
        "Keyword သတ်မှတ်ထားတဲ့အတိုင်း အလိုအလျောက် Reply ပြန်ပေးပါတယ်။\n"
        "အသုံးပြုနည်း: /help"
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
        try:
            await context.bot.copy_message(chat_id=chat_id, from_chat_id=source.chat_id, message_id=source.message_id)
            sent += 1
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
            try:
                await context.bot.copy_message(chat_id=chat_id, from_chat_id=source.chat_id, message_id=source.message_id)
                sent += 1
            except TelegramError:
                failed += 1
        except (Forbidden, TelegramError):
            failed += 1
            if kind == "group":
                with db() as conn: conn.execute("UPDATE known_groups SET active=0 WHERE chat_id=?", (chat_id,))
            else:
                with db() as conn: conn.execute("UPDATE known_users SET ads_enabled=0 WHERE user_id=?", (chat_id,))
        await asyncio.sleep(0.05)
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
    text = (
        "🤖 PRO Reply Bot Ultimate X — Commands\n\n"
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
        "/setlang my|en|both — ဘာသာစကား\n"
        "/setpriority ID | -100..100 — ဦးစားပေးမှု\n"
        "/setgroupcooldown 0-3600 — Group စောင့်ချိန်\n"
        "/setflood 0-30 — Flood ကာကွယ်မှု\n"
        "/setwelcome on|off — Welcome\n"
        "/setgoodbye on|off — Goodbye\n"
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
        "/backup — Database Backup ယူရန်\n\n"
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
    await update.effective_message.reply_text("🌐 Language setting saved.")


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
    await update.effective_message.reply_text("👋 Goodbye ဖွင့်ပြီးပါပြီ။" if enabled else "Goodbye ပိတ်ပြီးပါပြီ။")


async def member_update(update, context):
    msg = update.effective_message
    if not msg or not is_group(update.effective_chat):
        return
    s = get_settings(update.effective_chat.id)
    if msg.new_chat_members and s["welcome_enabled"]:
        for member in msg.new_chat_members:
            if not member.is_bot:
                text = s["welcome_text"].replace("{name}", member.full_name)
                await msg.reply_text(text)
    if msg.left_chat_member and s["goodbye_enabled"] and not msg.left_chat_member.is_bot:
        text = s["goodbye_text"].replace("{name}", msg.left_chat_member.full_name)
        await msg.reply_text(text)


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
        "🛠 PRO Reply Bot Dashboard\nအောက်က ခလုတ်များကို ရွေးပါ။",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def panel_callback(update, context):
    q = update.callback_query
    await q.answer()
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


async def backup(update, context):
    if not await require_admin(update, context):
        return
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=open(DB_PATH, "rb"),
        filename="replies-backup.sqlite3",
        caption="🗄️ Database Backup",
    )


async def automatic_backup(context):
    if not OWNER_ID or not DB_PATH.exists():
        return
    try:
        with DB_PATH.open("rb") as f:
            await context.bot.send_document(
                chat_id=OWNER_ID,
                document=f,
                filename="automatic-replies-backup.sqlite3",
                caption="🗄️ Automatic database backup",
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


async def handle_text(update, context):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not msg.text or not is_group(chat) or not user or user.is_bot:
        return
    s = get_settings(chat.id)
    now = time.time()
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
            record_conversation(chat.id, user.id, msg.text)
        except Exception:
            logger.exception("Conversation learning failed")
    if not s["enabled"] or random.randint(1, 100) > s["chance"]:
        return
    with db() as conn:
        last = conn.execute("SELECT last_reply FROM usage WHERE chat_id=? AND user_id=?", (chat.id, user.id)).fetchone()
        if last and now - last["last_reply"] < s["cooldown"]:
            return
        rows = conn.execute("SELECT * FROM replies WHERE chat_id=? ORDER BY priority DESC,id DESC", (chat.id,)).fetchall()
    limit = int(s.get("flood_limit", 0))
    if limit:
        queue = FLOOD_TRACKER[(chat.id, user.id)]
        while queue and now - queue[0] > 8:
            queue.popleft()
        queue.append(now)
        if len(queue) > limit:
            return
    group_cd = int(s.get("group_cooldown", 0))
    if group_cd and now - GROUP_LAST_REPLY[chat.id] < group_cd:
        return
    incoming = msg.text.casefold().strip()
    matches = []
    for row in rows:
        key = row["keyword"].casefold().strip()
        if not key:
            continue
        mode = s["match_mode"]
        matched = incoming == key if mode == "exact" else incoming.startswith(key) if mode == "startswith" else key in incoming
        if matched:
            matches.append(row)
    if not matches:
        return
    highest_priority = max(row["priority"] for row in matches)
    selected = random.choice([row for row in matches if row["priority"] == highest_priority])
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

async def post_init(app):
    await app.bot.set_my_commands([
        ("start", "Start bot"),
        ("ad", "Bot Owner: broadcast an ad"),
        ("adstats", "Bot Owner: advertising targets"),
        ("ads", "Manage private ad messages"),
        ("help", "Help and commands"),
        ("panel", "Admin dashboard"),
        ("addreply", "Add keyword reply"),
        ("listreplies", "List replies"),
        ("setpriority", "Set reply priority"),
        ("setflood", "Set flood protection"),
        ("setwelcome", "Toggle welcome messages"),
        ("setgoodbye", "Toggle goodbye messages"),
        ("setgroupcooldown", "Set group cooldown"),
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
    migration = migrate_sqlite_once()
    logger.info("SQLite migration result: %s", migration)
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ad", owner_ad_command))
    app.add_handler(CommandHandler("adstats", owner_ad_stats))
    app.add_handler(CommandHandler("ads", ads_command))
    app.add_handler(MessageHandler(filters.ALL, track_chats), group=-10)
    app.add_handler(CommandHandler("help", help_command))
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
    app.add_handler(CallbackQueryHandler(owner_security_callback, pattern=r"^raid:"))
    app.add_handler(CallbackQueryHandler(panel_callback, pattern=r"^panel:"))
    app.add_handler(ChatMemberHandler(lambda u, ctx: monitor_member_change(u, ctx, OWNER_ID), ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.CAPTION, lambda u, ctx: moderate_content(u, ctx, OWNER_ID)), group=-2)
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.CAPTION, lambda u, ctx: moderate_message(u, ctx, OWNER_ID), block=False), group=-1)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER, member_update))
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
