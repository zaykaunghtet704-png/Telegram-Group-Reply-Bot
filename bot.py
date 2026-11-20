import hashlib
import logging
import os
import random
import sqlite3
import time
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "replies.db"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("group-auto-reply")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                chance INTEGER NOT NULL DEFAULT 100,
                cooldown INTEGER NOT NULL DEFAULT 3
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                keyword TEXT NOT NULL,
                response TEXT NOT NULL
            )
        """)


def get_settings(chat_id):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM settings WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (chat_id,)
            )
            row = conn.execute(
                "SELECT * FROM settings WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return dict(row)


def set_setting(chat_id, field, value):
    allowed = {"enabled", "chance", "cooldown"}
    if field not in allowed:
        raise ValueError("Invalid setting")
    with db() as conn:
        conn.execute(
            f"INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (chat_id,)
        )
        conn.execute(
            f"UPDATE settings SET {field} = ? WHERE chat_id = ?",
            (value, chat_id),
        )


async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.type not in ("group", "supergroup"):
        return False
    user = update.effective_user
    if not user:
        return False
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        logger.exception("Could not verify group administrator")
        return False


async def require_group_admin(update, context):
    if not update.effective_chat or update.effective_chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text("ဒီ command ကို Group ထဲမှာ အသုံးပြုပါ။")
        return False
    if not await is_group_admin(update, context):
        await update.effective_message.reply_text("ဒီ command ကို Group Admin ပဲ အသုံးပြုနိုင်ပါတယ်။")
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "မင်္ဂလာပါ။ ဒီ Bot က Keyword Auto Reply Bot ဖြစ်ပါတယ်။\n"
        "အသုံးပြုပုံကို /help မှာ ကြည့်နိုင်ပါတယ်။"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 Group Auto Reply Bot\n\n"
        "Admin Commands (Group ထဲမှာ)\n"
        "/addreply keyword | reply - Auto Reply အသစ်ထည့်ရန်\n"
        "/delreply ID - Reply ဖျက်ရန်\n"
        "/listreplies - Reply စာရင်းကြည့်ရန်\n"
        "/autoreply on|off - Auto Reply ဖွင့်/ပိတ်ရန်\n"
        "/setchance 0-100 - Reply ဖြစ်နိုင်ခြေ သတ်မှတ်ရန်\n"
        "/setcooldown seconds - User တစ်ယောက်ချင်းစီအတွက် စောင့်ချိန်\n"
        "/replysettings - လက်ရှိ Setting ကြည့်ရန်\n"
        "/stats - Reply အရေအတွက်ကြည့်ရန်\n\n"
        "Keyword ပါတဲ့ စာကို Group ထဲမှာ ပို့လိုက်ရင် သတ်မှတ်ထားတဲ့ Reply ကို Bot က ပြန်ပို့ပါမယ်။"
    )
    await update.effective_message.reply_text(text)


async def add_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update, context):
        return
    raw = update.effective_message.text.partition(" ")[2].strip()
    if "|" not in raw:
        await update.effective_message.reply_text(
            "အသုံးပြုပုံ: /addreply မင်္ဂလာပါ | မင်္ဂလာပါခင်ဗျာ"
        )
        return
    keyword, response = (part.strip() for part in raw.split("|", 1))
    if not keyword or not response:
        await update.effective_message.reply_text("Keyword နဲ့ Reply စာသား နှစ်ခုလုံး ထည့်ပေးပါ။")
        return
    if len(keyword) > 150 or len(response) > 3500:
        await update.effective_message.reply_text("Keyword 150 လုံး၊ Reply 3500 လုံးထက် မကျော်ရပါ။")
        return
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO replies(chat_id, keyword, response) VALUES(?, ?, ?)",
            (update.effective_chat.id, keyword, response),
        )
        reply_id = cur.lastrowid
    await update.effective_message.reply_text(f"✅ Reply ထည့်ပြီးပါပြီ။ ID: {reply_id}")


async def delete_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("အသုံးပြုပုံ: /delreply 12")
        return
    reply_id = int(context.args[0])
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM replies WHERE id = ? AND chat_id = ?",
            (reply_id, update.effective_chat.id),
        )
    await update.effective_message.reply_text(
        "🗑️ ဖျက်ပြီးပါပြီ။" if cur.rowcount else "အဲဒီ ID ကို မတွေ့ပါဘူး။"
    )


async def list_replies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update, context):
        return
    with db() as conn:
        rows = conn.execute(
            "SELECT id, keyword, response FROM replies WHERE chat_id = ? ORDER BY id DESC LIMIT 40",
            (update.effective_chat.id,),
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text("Reply မရှိသေးပါဘူး။")
        return
    lines = ["📚 Auto Reply စာရင်း (နောက်ဆုံး 40 ခု)"]
    for row in rows:
        response = row["response"].replace("\n", " ")
        if len(response) > 70:
            response = response[:67] + "..."
        lines.append(f'{row["id"]}. [{row["keyword"]}] → {response}')
    await update.effective_message.reply_text("\n".join(lines))


async def auto_reply_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text("အသုံးပြုပုံ: /autoreply on သို့မဟုတ် /autoreply off")
        return
    enabled = 1 if context.args[0].lower() == "on" else 0
    set_setting(update.effective_chat.id, "enabled", enabled)
    await update.effective_message.reply_text("✅ Auto Reply ဖွင့်လိုက်ပါပြီ။" if enabled else "⛔ Auto Reply ပိတ်လိုက်ပါပြီ။")


async def set_chance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("အသုံးပြုပုံ: /setchance 80 (0 မှ 100)")
        return
    value = int(context.args[0])
    if not 0 <= value <= 100:
        await update.effective_message.reply_text("0 မှ 100 အတွင်း ထည့်ပါ။")
        return
    set_setting(update.effective_chat.id, "chance", value)
    await update.effective_message.reply_text(f"🎯 Reply Chance ကို {value}% သတ်မှတ်ပြီးပါပြီ။")


async def set_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("အသုံးပြုပုံ: /setcooldown 5 (စက္ကန့်)")
        return
    value = int(context.args[0])
    if not 0 <= value <= 3600:
        await update.effective_message.reply_text("0 မှ 3600 စက္ကန့်အတွင်း ထည့်ပါ။")
        return
    set_setting(update.effective_chat.id, "cooldown", value)
    await update.effective_message.reply_text(f"⏱️ Cooldown ကို {value} စက္ကန့် သတ်မှတ်ပြီးပါပြီ။")


async def reply_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update, context):
        return
    settings = get_settings(update.effective_chat.id)
    status = "ဖွင့်ထားသည်" if settings["enabled"] else "ပိတ်ထားသည်"
    await update.effective_message.reply_text(
        f"⚙️ Group Settings\nAuto Reply: {status}\n"
        f"Chance: {settings['chance']}%\nCooldown: {settings['cooldown']} sec"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_group_admin(update, context):
        return
    with db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM replies WHERE chat_id = ?",
            (update.effective_chat.id,),
        ).fetchone()["n"]
    await update.effective_message.reply_text(f"📊 ဒီ Group မှာ Auto Reply {count} ခု ရှိပါတယ်။")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not message.text or not chat or not user:
        return
    if chat.type not in ("group", "supergroup") or user.is_bot:
        return

    settings = get_settings(chat.id)
    if not settings["enabled"]:
        return

    now = time.monotonic()
    cooldown_key = f"cooldown:{chat.id}:{user.id}"
    last_reply = context.application.bot_data.get(cooldown_key, 0)
    if now - last_reply < settings["cooldown"]:
        return
    if random.randint(1, 100) > settings["chance"]:
        return

    text_lower = message.text.casefold()
    with db() as conn:
        rows = conn.execute(
            "SELECT keyword, response FROM replies WHERE chat_id = ?",
            (chat.id,),
        ).fetchall()

    matches = [
        row for row in rows
        if row["keyword"].casefold() in text_lower
    ]
    if not matches:
        return

    selected = random.choice(matches)
    try:
        await message.reply_text(selected["response"], reply_to_message_id=message.message_id)
        context.application.bot_data[cooldown_key] = now
    except Exception:
        logger.exception("Failed to send auto reply")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("addreply", add_reply))
    app.add_handler(CommandHandler("delreply", delete_reply))
    app.add_handler(CommandHandler("listreplies", list_replies))
    app.add_handler(CommandHandler("autoreply", auto_reply_toggle))
    app.add_handler(CommandHandler("setchance", set_chance))
    app.add_handler(CommandHandler("setcooldown", set_cooldown))
    app.add_handler(CommandHandler("replysettings", reply_settings))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if not render_url:
        raise RuntimeError("RENDER_EXTERNAL_URL is missing. Deploy this bot as a Render Web Service.")

    webhook_path = hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest()
    port = int(os.getenv("PORT", "10000"))

    logger.info("Telegram Group Auto Reply Bot is starting in webhook mode")
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
