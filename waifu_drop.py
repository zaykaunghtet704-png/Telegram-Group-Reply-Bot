"""PostgreSQL-backed Waifu card drops and collection."""
import logging
import random
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

from database import connect as db

log = logging.getLogger(__name__)
BOT_OWNER_ID = int(__import__("os").getenv("OWNER_ID", "7974865879") or 7974865879)
DEFAULT_DROP_EVERY = 85


def init_waifu():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS waifu_cards (
            id BIGINT PRIMARY KEY,
            name TEXT NOT NULL,
            rarity TEXT NOT NULL DEFAULT 'Common',
            attack BIGINT NOT NULL DEFAULT 10,
            defense BIGINT NOT NULL DEFAULT 10,
            hp BIGINT NOT NULL DEFAULT 100,
            image_file_id TEXT NOT NULL,
            created_by BIGINT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS waifu_inventory (
            user_id BIGINT NOT NULL,
            card_id BIGINT NOT NULL REFERENCES waifu_cards(id) ON DELETE CASCADE,
            quantity BIGINT NOT NULL DEFAULT 1,
            first_claimed_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY(user_id, card_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS waifu_group_settings (
            chat_id BIGINT PRIMARY KEY,
            enabled BIGINT NOT NULL DEFAULT 1,
            drop_every BIGINT NOT NULL DEFAULT 85,
            message_count BIGINT NOT NULL DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS waifu_active_drops (
            chat_id BIGINT PRIMARY KEY,
            card_id BIGINT NOT NULL REFERENCES waifu_cards(id) ON DELETE CASCADE,
            dropped_at DOUBLE PRECISION NOT NULL,
            claimed_by BIGINT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_waifu_inventory_user ON waifu_inventory(user_id)")


def _group(chat):
    return bool(chat and chat.type in ("group", "supergroup"))


async def _is_admin(update, context):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    if user.id == BOT_OWNER_ID:
        return True
    if not _group(chat):
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def _choose_card():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, rarity, attack, defense, hp, image_file_id "
            "FROM waifu_cards ORDER BY id"
        ).fetchall()
    return random.choice(rows) if rows else None


async def _drop_card(chat_id, bot, card=None):
    card = card or _choose_card()
    if not card:
        return False
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎴 Claim Card", callback_data=f"waifu:claim:{chat_id}:{card['id']}")
    ]])
    caption = (
        "✨ **WAIFU CARD DROP!** ✨\n\n"
        f"💖 **{card['name']}**\n"
        f"💎 Rarity: {card['rarity']}\n"
        f"⚔️ ATK: {card['attack']}  🛡 DEF: {card['defense']}\n"
        f"❤️ HP: {card['hp']}\n\n"
        "အရင်ဆုံး Claim လုပ်နိုင်သူက ဒီကတ်ကို ရရှိပါမယ်။"
    )
    with db() as conn:
        conn.execute(
            "INSERT INTO waifu_active_drops(chat_id, card_id, dropped_at, claimed_by) "
            "VALUES(?,?,?,NULL) ON CONFLICT(chat_id) DO UPDATE SET "
            "card_id=excluded.card_id, dropped_at=excluded.dropped_at, claimed_by=NULL",
            (chat_id, card["id"], time.time()),
        )
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=card["image_file_id"],
            caption=caption,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return True
    except Exception:
        log.exception("Could not send waifu drop to %s", chat_id)
        with db() as conn:
            conn.execute("DELETE FROM waifu_active_drops WHERE chat_id=?", (chat_id,))
        return False


async def waifu_track_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not _group(chat) or not user or user.is_bot:
        return
    if message.text and message.text.lstrip().startswith("/"):
        return

    should_drop = False
    with db() as conn:
        conn.execute(
            "INSERT INTO waifu_group_settings(chat_id) VALUES(?) ON CONFLICT(chat_id) DO NOTHING",
            (chat.id,),
        )
        conn.execute(
            "UPDATE waifu_group_settings SET message_count=message_count+1 "
            "WHERE chat_id=? AND enabled=1",
            (chat.id,),
        )
        row = conn.execute(
            "SELECT enabled, drop_every, message_count FROM waifu_group_settings WHERE chat_id=?",
            (chat.id,),
        ).fetchone()
        if row and row["enabled"] and row["message_count"] >= row["drop_every"]:
            conn.execute(
                "UPDATE waifu_group_settings SET message_count=0 WHERE chat_id=?",
                (chat.id,),
            )
            should_drop = True
    if should_drop:
        await _drop_card(chat.id, context.bot)


async def waifu_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.effective_message
    if not user or user.id != BOT_OWNER_ID:
        await msg.reply_text("ဒီ Command ကို Bot Owner သာ အသုံးပြုနိုင်ပါတယ်။")
        return
    reply = msg.reply_to_message
    if not reply or not reply.photo:
        await msg.reply_text(
            "ကတ်ပုံကို Reply လုပ်ပြီး ဒီလိုရေးပါ:\n"
            "/waifuadd Name | Rarity | ATK | DEF | HP"
        )
        return
    raw = " ".join(context.args).strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 5 or not parts[0] or not parts[1]:
        await msg.reply_text("အသုံးပြုပုံ: /waifuadd Name | Rarity | ATK | DEF | HP")
        return
    try:
        name, rarity = parts[0][:80], parts[1][:30]
        attack, defense, hp = [max(1, min(100000, int(x))) for x in parts[2:5]]
    except ValueError:
        await msg.reply_text("ATK, DEF, HP တို့ကို ကိန်းဂဏန်းနဲ့ ထည့်ပါ။")
        return
    image_id = reply.photo[-1].file_id
    with db() as conn:
        row = conn.execute("SELECT COALESCE(MAX(id),0)+1 AS next_id FROM waifu_cards").fetchone()
        card_id = int(row["next_id"])
        conn.execute(
            "INSERT INTO waifu_cards(id,name,rarity,attack,defense,hp,image_file_id,created_by) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (card_id, name, rarity, attack, defense, hp, image_id, user.id),
        )
    await msg.reply_text(f"✅ Waifu Card ထည့်ပြီးပါပြီ။\nID: {card_id}\n💖 {name} ({rarity})")


async def waifu_drop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not _group(chat):
        await msg.reply_text("Group ထဲမှာ အသုံးပြုပါ။")
        return
    if not await _is_admin(update, context):
        await msg.reply_text("Group Admin များသာ Manual Drop လုပ်နိုင်ပါတယ်။")
        return
    if not await _drop_card(chat.id, context.bot):
        await msg.reply_text("ကတ်မရှိသေးပါ။ Bot Owner ကို /waifuadd နဲ့ ကတ်ထည့်ခိုင်းပါ။")


async def waifu_auto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not _group(chat):
        await msg.reply_text("Group ထဲမှာ အသုံးပြုပါ။")
        return
    if not await _is_admin(update, context):
        await msg.reply_text("Group Admin များသာ ပြင်ဆင်နိုင်ပါတယ်။")
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await msg.reply_text("အသုံးပြုပုံ: /waifuauto on သို့မဟုတ် /waifuauto off")
        return
    enabled = 1 if context.args[0].lower() == "on" else 0
    with db() as conn:
        conn.execute(
            "INSERT INTO waifu_group_settings(chat_id,enabled) VALUES(?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled",
            (chat.id, enabled),
        )
    await msg.reply_text("🎴 Auto Waifu Drop ဖွင့်ပါပြီ။" if enabled else "⛔ Auto Waifu Drop ပိတ်ပါပြီ။")


async def waifu_interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not _group(chat):
        await msg.reply_text("Group ထဲမှာ အသုံးပြုပါ။")
        return
    if not await _is_admin(update, context):
        await msg.reply_text("Group Admin များသာ ပြင်ဆင်နိုင်ပါတယ်။")
        return
    if not context.args or not context.args[0].isdigit():
        await msg.reply_text("အသုံးပြုပုံ: /waifuinterval 85")
        return
    interval = int(context.args[0])
    if not 10 <= interval <= 10000:
        await msg.reply_text("Message အရေအတွက်ကို 10 မှ 10000 အတွင်းထားပါ။")
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO waifu_group_settings(chat_id,drop_every) VALUES(?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET drop_every=excluded.drop_every",
            (chat.id, interval),
        )
    await msg.reply_text(f"⚙️ Message {interval} ခုတိုင်း Waifu Drop ကျပါမယ်။")


async def waifu_harem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    with db() as conn:
        rows = conn.execute(
            "SELECT c.id,c.name,c.rarity,i.quantity FROM waifu_inventory i "
            "JOIN waifu_cards c ON c.id=i.card_id WHERE i.user_id=? ORDER BY c.id DESC LIMIT 30",
            (user.id,),
        ).fetchall()
        total = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) AS total FROM waifu_inventory WHERE user_id=?",
            (user.id,),
        ).fetchone()["total"]
    if not rows:
        await update.effective_message.reply_text("🎴 သင့် Harem ထဲမှာ ကတ်မရှိသေးပါ။ Group Drop မှာ Claim လုပ်ပါ။")
        return
    lines = [f"💖 **{user.first_name}'s Waifu Harem**", f"🎴 Total Cards: {total}", ""]
    lines.extend(f"#{r['id']} · {r['name']} — {r['rarity']} ×{r['quantity']}" for r in rows)
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


async def waifu_claim_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        _, _, raw_chat, raw_card = query.data.split(":")
        chat_id, card_id = int(raw_chat), int(raw_card)
    except Exception:
        await query.answer("Claim data မမှန်ပါ။", show_alert=True)
        return
    user = query.from_user
    if query.message.chat.id != chat_id:
        await query.answer("ဒီ Drop က အခြား Group အတွက်ပါ။", show_alert=True)
        return
    with db() as conn:
        drop = conn.execute(
            "SELECT card_id,claimed_by FROM waifu_active_drops WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
        if not drop or int(drop["card_id"]) != card_id:
            await query.answer("ဒီကတ် Drop မရှိတော့ပါ။", show_alert=True)
            return
        if drop["claimed_by"] is not None:
            await query.answer("ဒီကတ်ကို အခြားသူ Claim လုပ်ပြီးပါပြီ။", show_alert=True)
            return
        conn.execute(
            "UPDATE waifu_active_drops SET claimed_by=? WHERE chat_id=? AND claimed_by IS NULL",
            (user.id, chat_id),
        )
        card = conn.execute(
            "SELECT name,rarity FROM waifu_cards WHERE id=?",
            (card_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO waifu_inventory(user_id,card_id,quantity,first_claimed_at) VALUES(?,?,1,?) "
            "ON CONFLICT(user_id,card_id) DO UPDATE SET quantity=waifu_inventory.quantity+1",
            (user.id, card_id, time.time()),
        )
    await query.answer("🎉 Claim အောင်မြင်ပါပြီ!", show_alert=True)
    try:
        await query.edit_message_caption(
            caption=f"🏆 CLAIMED!\n\n💖 {card['name']}\n💎 {card['rarity']}\n"
                    f"👤 Claimed by: {user.first_name}",
            reply_markup=None,
        )
    except Exception:
        log.exception("Could not update claimed waifu card message")


def waifu_handlers():
    return [
        CommandHandler("waifuadd", waifu_add_command),
        CommandHandler("waifudrop", waifu_drop_command),
        CommandHandler("waifuauto", waifu_auto_command),
        CommandHandler("waifuinterval", waifu_interval_command),
        CommandHandler("harem", waifu_harem_command),
        CallbackQueryHandler(waifu_claim_callback, pattern=r"^waifu:claim:"),
        MessageHandler(filters.ALL, waifu_track_message, block=False),
    ]
