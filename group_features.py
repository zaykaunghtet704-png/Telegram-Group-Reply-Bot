"""Persistent group rules and basic chat controls."""
from database import connect
from telegram import ChatPermissions


def init_group_features():
    with connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS group_rules (
            chat_id BIGINT PRIMARY KEY,
            rules_text TEXT NOT NULL DEFAULT ''
        )""")


async def set_rules_command(update, context):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or chat.type not in ("group", "supergroup"):
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status not in ("administrator", "creator"):
            await msg.reply_text("Group Admin များသာ စည်းကမ်းပြင်နိုင်ပါတယ်။")
            return
    except Exception:
        await msg.reply_text("Admin အခွင့်အရေး စစ်ဆေးမရပါ။")
        return
    rules = msg.text.partition(" ")[2].strip()
    if not rules:
        await msg.reply_text("အသုံးပြုပုံ: /setrules Group စည်းကမ်းစာသား")
        return
    if len(rules) > 3500:
        await msg.reply_text("စည်းကမ်းစာသားသည် ၃၅၀၀ စာလုံးထက် မကျော်ရပါ။")
        return
    with connect() as conn:
        conn.execute(
            "INSERT INTO group_rules(chat_id,rules_text) VALUES(?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET rules_text=excluded.rules_text",
            (chat.id, rules),
        )
    await msg.reply_text("📜 Group စည်းကမ်းကို သိမ်းပြီးပါပြီ။ /rules ဖြင့် ကြည့်နိုင်ပါတယ်။")


async def rules_command(update, context):
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not chat or chat.type not in ("group", "supergroup"):
        if msg:
            await msg.reply_text("ဒီ command ကို Group ထဲမှာ သုံးပါ။")
        return
    with connect() as conn:
        row = conn.execute("SELECT rules_text FROM group_rules WHERE chat_id=?", (chat.id,)).fetchone()
    rules = row["rules_text"] if row else ""
    await msg.reply_text("📜 Group စည်းကမ်း\n\n" + (rules or "စည်းကမ်း မသတ်မှတ်ရသေးပါ။ Admin ကို ဆက်သွယ်ပါ။"))


def _all_permissions(value):
    return ChatPermissions(
        can_send_messages=value,
        can_send_audios=value,
        can_send_documents=value,
        can_send_photos=value,
        can_send_videos=value,
        can_send_video_notes=value,
        can_send_voice_notes=value,
        can_send_polls=value,
        can_send_other_messages=value,
        can_add_web_page_previews=value,
    )


async def _set_group_lock(update, context, locked):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or chat.type not in ("group", "supergroup"):
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status not in ("administrator", "creator"):
            await msg.reply_text("Group Admin များသာ အသုံးပြုနိုင်ပါတယ်။")
            return
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if bot_member.status != "creator" and not getattr(bot_member, "can_restrict_members", False):
            await msg.reply_text("Bot ကို Restrict Members permission ပေးပါ။")
            return
        await context.bot.set_chat_permissions(chat.id, _all_permissions(not locked))
        await msg.reply_text("🔒 Group ကို Lock လုပ်ပြီးပါပြီ။" if locked else "🔓 Group ကို Unlock လုပ်ပြီးပါပြီ။")
    except Exception as exc:
        await msg.reply_text(f"လုပ်ဆောင်မှု မအောင်မြင်ပါ။ Bot Permission စစ်ပါ။ ({type(exc).__name__})")


async def lock_command(update, context):
    await _set_group_lock(update, context, True)


async def unlock_command(update, context):
    await _set_group_lock(update, context, False)
