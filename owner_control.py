"""Telegram Bot Owner control center (OWNER_ID only)."""
import time
from database import connect


def init_owner_control():
    with connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS owner_settings (
            setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS owner_action_logs (
            id BIGSERIAL PRIMARY KEY, actor_id BIGINT NOT NULL,
            action TEXT NOT NULL, details TEXT NOT NULL DEFAULT '',
            created_at DOUBLE PRECISION NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS owner_bans (
            user_id BIGINT PRIMARY KEY, reason TEXT NOT NULL DEFAULT '',
            created_at DOUBLE PRECISION NOT NULL, active INTEGER NOT NULL DEFAULT 1
        )""")
        conn.execute("""INSERT INTO owner_settings(setting_key,setting_value)
            VALUES('maintenance','off') ON CONFLICT(setting_key) DO NOTHING""")


def _owner(update, owner_id):
    user = update.effective_user
    return bool(user and owner_id and user.id == owner_id)


def _log(owner_id, action, details=''):
    with connect() as conn:
        conn.execute(
            "INSERT INTO owner_action_logs(actor_id,action,details,created_at) VALUES(?,?,?,?)",
            (owner_id, action[:80], details[:1000], time.time()),
        )


async def owner_command(update, context, owner_id):
    msg = update.effective_message
    if not _owner(update, owner_id):
        if msg:
            await msg.reply_text("⛔ Bot Owner သာ အသုံးပြုနိုင်ပါတယ်။")
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = [
        [InlineKeyboardButton("📊 Statistics", callback_data="owner:stats"),
         InlineKeyboardButton("👥 Groups", callback_data="owner:groups")],
        [InlineKeyboardButton("👤 Users", callback_data="owner:users"),
         InlineKeyboardButton("🚫 Ban List", callback_data="owner:bans")],
        [InlineKeyboardButton("💘 Relationship", callback_data="owner:love")],
        [InlineKeyboardButton("🛠 Maintenance", callback_data="owner:maintenance"),
         InlineKeyboardButton("📜 Action Logs", callback_data="owner:logs")],
    ]
    await msg.reply_text(
        "💎 𝗣𝗥𝗢 𝗥𝗘𝗣𝗟𝗬 💎\n"
        "╭━━━━━━━━━━━━━━━━━━━━╮\n"
        "    👑 𝗢𝗪𝗡𝗘𝗥 𝗖𝗢𝗡𝗧𝗥𝗢𝗟\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n"
        "🔐 Owner-only control panel",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def owner_callback(update, context, owner_id):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    query = update.callback_query
    if not query:
        return
    if not _owner(update, owner_id):
        await query.answer("Owner သာ အသုံးပြုနိုင်ပါတယ်။", show_alert=True)
        return
    await query.answer()
    key = query.data or ""
    with connect() as conn:
        if key == "owner:stats":
            groups = conn.execute("SELECT COUNT(*) AS n FROM known_groups").fetchone()["n"]
            active = conn.execute("SELECT COUNT(*) AS n FROM known_groups WHERE active=1").fetchone()["n"]
            users = conn.execute("SELECT COUNT(*) AS n FROM known_users").fetchone()["n"]
            replies = conn.execute("SELECT COUNT(*) AS n FROM replies").fetchone()["n"]
            text = f"📊 BOT STATISTICS\nGroups: {groups}\nActive Groups: {active}\nUsers: {users}\nAuto Replies: {replies}"
        elif key == "owner:groups":
            rows = conn.execute("SELECT chat_id,title,active FROM known_groups ORDER BY first_seen DESC LIMIT 20").fetchall()
            text = "👥 GROUPS (နောက်ဆုံး ၂၀ ခု)\n" + ("\n".join(f"{r['title'] or 'Untitled'} | {r['chat_id']} | {'Active' if r['active'] else 'Inactive'}" for r in rows) or "မရှိသေးပါ။")
        elif key == "owner:users":
            rows = conn.execute("SELECT user_id,ads_enabled FROM known_users ORDER BY first_seen DESC LIMIT 20").fetchall()
            text = "👤 USERS (နောက်ဆုံး ၂၀ ခု)\n" + ("\n".join(f"{r['user_id']} | Ads {'ON' if r['ads_enabled'] else 'OFF'}" for r in rows) or "မရှိသေးပါ။")
        elif key == "owner:bans":
            rows = conn.execute("SELECT user_id,reason FROM owner_bans WHERE active=1 ORDER BY created_at DESC LIMIT 30").fetchall()
            text = "🚫 ACTIVE BOT BANS\n" + ("\n".join(f"{r['user_id']} — {r['reason']}" for r in rows) or "Ban မရှိပါ။")
        elif key == "owner:love":
            profiles = conn.execute("SELECT COUNT(*) AS n FROM love_profiles WHERE active=1").fetchone()["n"]
            published = conn.execute("SELECT COUNT(*) AS n FROM love_profiles WHERE active=1 AND approved=1 AND consent_publish=1").fetchone()["n"]
            reports = conn.execute("SELECT COUNT(*) AS n FROM love_reports WHERE status='open'").fetchone()["n"]
            channels = conn.execute("SELECT COUNT(*) AS n FROM love_channels").fetchone()["n"]
            groups = conn.execute("SELECT COUNT(*) AS n FROM love_group_settings WHERE enabled=1").fetchone()["n"]
            dms = conn.execute("SELECT COUNT(*) AS n FROM love_dm_subscribers WHERE enabled=1").fetchone()["n"]
            text = f"💘 RELATIONSHIP CONTROL\nProfiles: {profiles}\nPublished/approved: {published}\nOpen reports: {reports}\nRequired channels: {channels}\nOpt-in groups: {groups}\nOpt-in DMs: {dms}\n\nManage channels: /lovechannel\nReview reports: /lovereports"
        elif key == "owner:maintenance":
            row = conn.execute("SELECT setting_value FROM owner_settings WHERE setting_key='maintenance'").fetchone()
            current = row["setting_value"] if row else "off"
            next_value = "off" if current == "on" else "on"
            conn.execute("UPDATE owner_settings SET setting_value=? WHERE setting_key='maintenance'", (next_value,))
            _log(owner_id, "maintenance", next_value)
            text = f"🛠 Maintenance Mode: {next_value.upper()}"
        elif key == "owner:logs":
            rows = conn.execute("SELECT action,details,created_at FROM owner_action_logs ORDER BY id DESC LIMIT 15").fetchall()
            text = "📜 OWNER ACTION LOGS\n" + ("\n".join(f"{r['action']} — {r['details']}" for r in rows) or "မှတ်တမ်းမရှိသေးပါ။")
        else:
            text = "မသိသောလုပ်ဆောင်ချက်ပါ။"
    keyboard = [[InlineKeyboardButton("⬅️ Owner Menu", callback_data="owner:home")]]
    if key == "owner:home":
        await owner_command(update, context, owner_id)
        return
    await query.edit_message_text(text[:3900], reply_markup=InlineKeyboardMarkup(keyboard))


def is_owner_banned(user_id):
    with connect() as conn:
        row = conn.execute("SELECT active FROM owner_bans WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row["active"])


def maintenance_enabled():
    with connect() as conn:
        row = conn.execute("SELECT setting_value FROM owner_settings WHERE setting_key='maintenance'").fetchone()
    return bool(row and row["setting_value"] == "on")


def get_user_restrictions(user_id):
    """Read maintenance and bot-level ban state in one database connection."""
    with connect() as conn:
        maintenance = conn.execute(
            "SELECT setting_value FROM owner_settings WHERE setting_key='maintenance'"
        ).fetchone()
        banned = conn.execute(
            "SELECT active FROM owner_bans WHERE user_id=?", (user_id,)
        ).fetchone()
    return bool(maintenance and maintenance["setting_value"] == "on"), bool(banned and banned["active"])


async def owner_ban_command(update, context, owner_id):
    msg = update.effective_message
    if not _owner(update, owner_id):
        if msg:
            await msg.reply_text("⛔ Bot Owner သာ အသုံးပြုနိုင်ပါတယ်။")
        return
    if not context.args or not context.args[0].isdigit():
        await msg.reply_text("အသုံးပြုပုံ: /ownerban USER_ID အကြောင်းပြချက်")
        return
    target_id = int(context.args[0])
    if target_id == owner_id:
        await msg.reply_text("Bot Owner ကို ကိုယ်တိုင် Ban မလုပ်နိုင်ပါ။")
        return
    reason = " ".join(context.args[1:]).strip() or "Bot policy violation"
    with connect() as conn:
        conn.execute(
            """INSERT INTO owner_bans(user_id,reason,created_at,active)
               VALUES(?,?,?,1)
               ON CONFLICT(user_id) DO UPDATE SET
               reason=excluded.reason,created_at=excluded.created_at,active=1""",
            (target_id, reason[:500], time.time()),
        )
    _log(owner_id, "user_ban", f"{target_id}: {reason}")
    await msg.reply_text(f"🚫 User {target_id} ကို Bot အသုံးပြုခွင့် ပိတ်ပြီးပါပြီ။")


async def owner_unban_command(update, context, owner_id):
    msg = update.effective_message
    if not _owner(update, owner_id):
        if msg:
            await msg.reply_text("⛔ Bot Owner သာ အသုံးပြုနိုင်ပါတယ်။")
        return
    if not context.args or not context.args[0].isdigit():
        await msg.reply_text("အသုံးပြုပုံ: /ownerunban USER_ID")
        return
    target_id = int(context.args[0])
    with connect() as conn:
        cur = conn.execute(
            "UPDATE owner_bans SET active=0 WHERE user_id=? AND active=1",
            (target_id,),
        )
    if cur.rowcount:
        _log(owner_id, "user_unban", str(target_id))
        await msg.reply_text(f"✅ User {target_id} ကို Bot အသုံးပြုခွင့် ပြန်ဖွင့်ပြီးပါပြီ။")
    else:
        await msg.reply_text("ဒီ User ကို Ban မလုပ်ထားပါ။")
