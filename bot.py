import hashlib
import logging
import os
import random
import sqlite3
import time
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "replies.db"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
BACKUP_INTERVAL_HOURS = max(1, int(os.getenv("BACKUP_INTERVAL_HOURS", "24")))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("pro-reply-bot")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            chance INTEGER NOT NULL DEFAULT 100,
            cooldown INTEGER NOT NULL DEFAULT 3,
            match_mode TEXT NOT NULL DEFAULT 'contains',
            language TEXT NOT NULL DEFAULT 'both'
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            keyword TEXT NOT NULL,
            response TEXT NOT NULL DEFAULT '',
            media_type TEXT NOT NULL DEFAULT 'text',
            file_id TEXT NOT NULL DEFAULT '',
            caption TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS usage (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            last_reply REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id, user_id)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_replies_chat ON replies(chat_id)")


def get_settings(chat_id):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (chat_id,))
        row = conn.execute("SELECT * FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
        return dict(row)


def set_setting(chat_id, key, value):
    allowed = {"enabled", "chance", "cooldown", "match_mode", "language"}
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
    await update.effective_message.reply_text(
        "🤖 PRO Reply Bot Ultimate X\n"
        "Keyword သတ်မှတ်ထားတဲ့အတိုင်း အလိုအလျောက် Reply ပြန်ပေးပါတယ်။\n"
        "အသုံးပြုနည်း: /help"
    )


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
        + "Language: " + s["language"]
    )


async def stats(update, context):
    if not await require_admin(update, context):
        return
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) n FROM replies WHERE chat_id=?", (update.effective_chat.id,)).fetchone()["n"]
    await update.effective_message.reply_text("📊 ဒီ Group မှာ Reply " + str(count) + " ခု ရှိပါတယ်။")


async def panel(update, context):
    if not await require_admin(update, context):
        return
    keyboard = [
        [InlineKeyboardButton("📚 Reply စာရင်း", callback_data="panel:list"),
         InlineKeyboardButton("📊 Stats", callback_data="panel:stats")],
        [InlineKeyboardButton("✅ Auto ON", callback_data="panel:on"),
         InlineKeyboardButton("⛔ Auto OFF", callback_data="panel:off")],
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
    elif action == "settings":
        s = get_settings(chat.id)
        await q.edit_message_text(f'Auto: {s["enabled"]}\nChance: {s["chance"]}%\nCooldown: {s["cooldown"]}\nMatch: {s["match_mode"]}\nLanguage: {s["language"]}')
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


async def handle_text(update, context):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not msg.text or not is_group(chat) or not user or user.is_bot:
        return
    s = get_settings(chat.id)
    if not s["enabled"] or random.randint(1, 100) > s["chance"]:
        return
    now = time.time()
    with db() as conn:
        last = conn.execute("SELECT last_reply FROM usage WHERE chat_id=? AND user_id=?", (chat.id, user.id)).fetchone()
        if last and now - last["last_reply"] < s["cooldown"]:
            return
        rows = conn.execute("SELECT * FROM replies WHERE chat_id=?", (chat.id,)).fetchall()
    incoming = msg.text.casefold().strip()
    matches = []
    for row in rows:
        key = row["keyword"].casefold().strip()
        if not key:
            continue
        mode = s["match_mode"]
        matched = (
            incoming == key if mode == "exact"
            else incoming.startswith(key) if mode == "startswith"
            else key in incoming
        )
        if matched:
            matches.append(row)
    if not matches:
        return
    selected = random.choice(matches)
    try:
        await send_reply(msg, selected)
        with db() as conn:
            conn.execute(
                "INSERT INTO usage(chat_id,user_id,last_reply) VALUES(?,?,?) "
                "ON CONFLICT(chat_id,user_id) DO UPDATE SET last_reply=excluded.last_reply",
                (chat.id, user.id, now),
            )
    except Exception:
        logger.exception("Auto reply failed")


async def post_init(app):
    await app.bot.set_my_commands([
        ("start", "Start bot"),
        ("help", "Help and commands"),
        ("panel", "Admin dashboard"),
        ("addreply", "Add keyword reply"),
        ("listreplies", "List replies"),
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
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
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
    app.add_handler(CommandHandler("replysettings", reply_settings))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("backup", backup))
    app.add_handler(CallbackQueryHandler(panel_callback, pattern=r"^panel:"))
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
