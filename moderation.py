import re
import sqlite3
import time
from pathlib import Path

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import ContextTypes, ApplicationHandlerStop

DB_PATH = Path(__file__).resolve().parent / "replies.db"

# Text-only keyword screening. Image/video classification is intentionally not claimed.
ADULT_PATTERNS = [
    r"\bporn\b", r"\bxxx\b", r"\bnude(s)?\b", r"\bonlyfans\b",
    r"\bsex\s*(video|photo|pic|clip)?\b", r"\b18\s*\+\b",
    r"ညစ်ညမ်း", r"အပြာကား", r"အပြာဗီဒီယို", r"လိင်ပိုင်းဆိုင်ရာ",
    r"အဝတ်မပါ", r"ကိုယ်လုံးတီး",
]
COMPILED_ADULT = [re.compile(p, re.IGNORECASE) for p in ADULT_PATTERNS]

def connect():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def init_moderation_db():
    with connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS moderation_settings(
            chat_id INTEGER PRIMARY KEY,
            adult_filter INTEGER NOT NULL DEFAULT 0,
            warn_limit INTEGER NOT NULL DEFAULT 3,
            warn_action TEXT NOT NULL DEFAULT 'mute',
            mute_seconds INTEGER NOT NULL DEFAULT 3600
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS warnings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            actor_id INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL,
            created_at REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_warnings_user ON warnings(chat_id,user_id,active)")

def get_settings(chat_id):
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO moderation_settings(chat_id) VALUES(?)", (chat_id,))
        return dict(c.execute("SELECT * FROM moderation_settings WHERE chat_id=?", (chat_id,)).fetchone())

def set_setting(chat_id, key, value):
    if key not in {"adult_filter", "warn_limit", "warn_action", "mute_seconds"}:
        raise ValueError("Invalid moderation setting")
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO moderation_settings(chat_id) VALUES(?)", (chat_id,))
        c.execute(f"UPDATE moderation_settings SET {key}=? WHERE chat_id=?", (value, chat_id))

def is_admin(member):
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER, "administrator", "creator")

def count_warnings(chat_id, user_id):
    with connect() as c:
        return c.execute("SELECT COUNT(*) n FROM warnings WHERE chat_id=? AND user_id=? AND active=1",
                         (chat_id, user_id)).fetchone()["n"]

async def add_warning(bot, chat_id, user_id, actor_id, reason, settings, display_name=None):
    with connect() as c:
        cur = c.execute("INSERT INTO warnings(chat_id,user_id,actor_id,reason,created_at) VALUES(?,?,?,?,?)",
                        (chat_id, user_id, actor_id, reason[:300], time.time()))
        warning_id = cur.lastrowid
    total = count_warnings(chat_id, user_id)
    action_result = "သတိပေးထားပါသည်။"
    if total >= settings["warn_limit"]:
        try:
            if settings["warn_action"] == "ban":
                await bot.ban_chat_member(chat_id, user_id)
                action_result = "သတ်မှတ် Warn ပြည့်သဖြင့် Ban လုပ်ပြီးပါပြီ။"
            elif settings["warn_action"] == "kick":
                await bot.ban_chat_member(chat_id, user_id)
                await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
                action_result = "သတ်မှတ် Warn ပြည့်သဖြင့် Kick လုပ်ပြီးပါပြီ။"
            else:
                until = int(time.time()) + int(settings["mute_seconds"])
                await bot.restrict_chat_member(chat_id, user_id,
                    permissions=__import__("telegram").ChatPermissions(can_send_messages=False),
                    until_date=until)
                action_result = f'Warn ပြည့်သဖြင့် {int(settings["mute_seconds"]) // 60} မိနစ် Mute လုပ်ပြီးပါပြီ။'
        except TelegramError as exc:
            action_result = f"အလိုအလျောက်အရေးယူမှု မအောင်မြင်ပါ ({type(exc).__name__})။ Bot Admin Permission စစ်ပါ။"
    return warning_id, total, action_result

async def moderate_content(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_id: int):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or chat.type not in ("group", "supergroup") or not user or user.is_bot:
        return
    cfg = get_settings(chat.id)
    if not cfg["adult_filter"]:
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if is_admin(member):
            return
    except TelegramError:
        return
    body = msg.text or msg.caption or ""
    if not body or not any(pattern.search(body) for pattern in COMPILED_ADULT):
        return
    try:
        await msg.delete()
    except TelegramError:
        return
    _, total, result = await add_warning(context.bot, chat.id, user.id, 0, "18+ / မသင့်လျော်သော စာသား", cfg, user.full_name)
    try:
        await context.bot.send_message(chat.id,
            f"🔞 {user.mention_html()} — 18+ မသင့်လျော်သောစာသားကို ဖျက်ပြီး Warn {total}/{cfg['warn_limit']} ပေးထားသည်။\n{result}",
            parse_mode="HTML")
    except TelegramError:
        pass
    if owner_id:
        try:
            await context.bot.send_message(owner_id,
                f"🛡️ Moderation Log\nGroup: {chat.title} ({chat.id})\nUser: {user.full_name} ({user.id})\nအကြောင်းရင်း: 18+ မသင့်လျော်သောစာသား\nWarn: {total}/{cfg['warn_limit']}")
        except TelegramError:
            pass
    raise ApplicationHandlerStop

async def manual_warn(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or chat.type not in ("group", "supergroup"):
        return
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("အသုံးပြုပုံ: User ရဲ့စာကို Reply လုပ်ပြီး /warn အကြောင်းပြချက်")
        return
    target = msg.reply_to_message.from_user
    if target.is_bot:
        await msg.reply_text("Bot ကို Warn မပေးနိုင်ပါ။")
        return
    reason = " ".join(context.args).strip() or "Group စည်းကမ်းချိုးဖောက်မှု"
    cfg = get_settings(chat.id)
    _, total, result = await add_warning(context.bot, chat.id, target.id, update.effective_user.id, reason, cfg)
    await msg.reply_text(f"⚠️ {target.full_name} ကို Warn ပေးပြီးပါပြီ။\nWarn: {total}/{cfg['warn_limit']}\nအကြောင်းရင်း: {reason}\n{result}")

async def show_warnings(update, context):
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not chat or chat.type not in ("group", "supergroup"):
        return
    target = msg.reply_to_message.from_user if msg.reply_to_message and msg.reply_to_message.from_user else update.effective_user
    total = count_warnings(chat.id, target.id)
    with connect() as c:
        rows = c.execute("SELECT id,reason,created_at FROM warnings WHERE chat_id=? AND user_id=? AND active=1 ORDER BY id DESC LIMIT 10",
                         (chat.id, target.id)).fetchall()
    lines = [f"⚠️ Warn History: {target.full_name} ({target.id})", f"စုစုပေါင်း Warn: {total}"]
    lines += [f'#{r["id"]} — {r["reason"]}' for r in rows]
    await msg.reply_text("\n".join(lines))

async def reset_warnings(update, context):
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not chat or chat.type not in ("group", "supergroup"):
        return
    target = msg.reply_to_message.from_user if msg.reply_to_message and msg.reply_to_message.from_user else update.effective_user
    with connect() as c:
        cur = c.execute("UPDATE warnings SET active=0 WHERE chat_id=? AND user_id=? AND active=1", (chat.id, target.id))
    await msg.reply_text(f"🧹 {target.full_name} ရဲ့ Warn {cur.rowcount} ခု ရှင်းပြီးပါပြီ။")
