"""Persistent member utilities for PRO Reply Bot Ultimate X."""
import re
import time
from database import connect


def init_member_features():
    with connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS member_profiles (
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL DEFAULT '',
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL,
            message_count BIGINT NOT NULL DEFAULT 0,
            afk_text TEXT NOT NULL DEFAULT '',
            afk_since REAL,
            PRIMARY KEY(chat_id, user_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS member_notes (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            owner_id BIGINT NOT NULL,
            note TEXT NOT NULL,
            created_at REAL NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS member_reports (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            reporter_id BIGINT NOT NULL,
            target_id BIGINT NOT NULL,
            message_id BIGINT NOT NULL DEFAULT 0,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at REAL NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_member_notes_owner ON member_notes(chat_id,owner_id,id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_member_reports_chat ON member_reports(chat_id,status,id)")


def track_member_activity(update):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or user.is_bot or chat.type not in ("group", "supergroup"):
        return
    now = time.time()
    with connect() as conn:
        conn.execute(
            """INSERT INTO member_profiles
               (chat_id,user_id,display_name,username,first_seen,last_seen,message_count)
               VALUES(?,?,?,?,?,?,1)
               ON CONFLICT(chat_id,user_id) DO UPDATE SET
               display_name=excluded.display_name, username=excluded.username,
               last_seen=excluded.last_seen, message_count=member_profiles.message_count+1""",
            (chat.id, user.id, user.full_name, user.username or "", now, now),
        )
        conn.execute(
            "UPDATE member_profiles SET afk_text='',afk_since=NULL WHERE chat_id=? AND user_id=? AND afk_text<>''",
            (chat.id, user.id),
        )


async def id_command(update, context):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return
    target = msg.reply_to_message.from_user if msg.reply_to_message and msg.reply_to_message.from_user else user
    await msg.reply_text(
        f"🆔 User ID: {target.id}\n"
        f"👥 Chat ID: {chat.id}\n"
        f"👤 Name: {target.full_name}\n"
        f"🔗 Username: @{target.username}" if target.username else
        f"🆔 User ID: {target.id}\n👥 Chat ID: {chat.id}\n👤 Name: {target.full_name}"
    )


async def profile_command(update, context):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or not user:
        return
    target = msg.reply_to_message.from_user if msg.reply_to_message and msg.reply_to_message.from_user else user
    if chat.type in ("group", "supergroup"):
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM member_profiles WHERE chat_id=? AND user_id=?",
                (chat.id, target.id),
            ).fetchone()
        if row:
            seen = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(row["last_seen"]))
            count = row["message_count"]
            afk = "ON" if row["afk_text"] else "OFF"
        else:
            seen, count, afk = "မှတ်တမ်းမရှိသေးပါ", 0, "OFF"
    else:
        seen, count, afk = "Group ထဲတွင်သာ စာရင်းရှိသည်", 0, "OFF"
    await msg.reply_text(
        f"👤 Profile\nအမည်: {target.full_name}\nUser ID: {target.id}\n"
        f"Username: @{target.username or 'မရှိ'}\n"
        f"Group စာပို့အကြိမ်: {count}\nနောက်ဆုံးတွေ့ချိန်: {seen}\nAFK: {afk}"
    )


async def afk_command(update, context):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or not user or chat.type not in ("group", "supergroup"):
        if msg:
            await msg.reply_text("ဒီ command ကို Group ထဲမှာ သုံးပါ။")
        return
    reason = " ".join(context.args).strip()[:250] or "အကြောင်းပြချက် မဖော်ပြထားပါ"
    now = time.time()
    with connect() as conn:
        conn.execute(
            """INSERT INTO member_profiles
               (chat_id,user_id,display_name,username,first_seen,last_seen,message_count,afk_text,afk_since)
               VALUES(?,?,?,?,?,?,0,?,?)
               ON CONFLICT(chat_id,user_id) DO UPDATE SET
               display_name=excluded.display_name,username=excluded.username,
               afk_text=excluded.afk_text,afk_since=excluded.afk_since""",
            (chat.id, user.id, user.full_name, user.username or "", now, now, reason, now),
        )
    await msg.reply_text(f"💤 {user.mention_html()} AFK ဖွင့်ထားပါပြီ။\nအကြောင်းပြချက်: {reason}", parse_mode="HTML")


async def check_afk_mention(update, context):
    msg, chat = update.effective_message, update.effective_chat
    if not msg or not chat or chat.type not in ("group", "supergroup"):
        return

    # Detect replied-to users, Telegram text mentions, and @username mentions.
    candidates = {}
    replied_user = msg.reply_to_message.from_user if msg.reply_to_message else None
    if replied_user and not replied_user.is_bot:
        candidates[replied_user.id] = replied_user.full_name

    for entity in (msg.entities or []) + (msg.caption_entities or []):
        mentioned_user = getattr(entity, "user", None)
        if mentioned_user and not mentioned_user.is_bot:
            candidates[mentioned_user.id] = mentioned_user.full_name

    body = msg.text or msg.caption or ""
    usernames = list(dict.fromkeys(re.findall(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{5,32})", body)))[:5]

    with connect() as conn:
        for username in usernames:
            row = conn.execute(
                "SELECT user_id,display_name FROM member_profiles "
                "WHERE chat_id=? AND lower(username)=lower(?) LIMIT 1",
                (chat.id, username),
            ).fetchone()
            if row:
                candidates[row["user_id"]] = row["display_name"] or ("@" + username)

        active = []
        for target_id, display_name in candidates.items():
            row = conn.execute(
                "SELECT afk_text,afk_since FROM member_profiles "
                "WHERE chat_id=? AND user_id=?",
                (chat.id, target_id),
            ).fetchone()
            if row and row["afk_text"]:
                duration = max(0, int(time.time() - (row["afk_since"] or time.time())))
                active.append(
                    f"💤 {display_name} လက်ရှိ AFK ဖြစ်နေပါတယ်။\n"
                    f"အကြောင်းပြချက်: {row['afk_text']}\n"
                    f"ကြာချိန်: {duration // 60} မိနစ်"
                )

    if active:
        await msg.reply_text("\n\n".join(active[:5]))


async def note_command(update, context):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or not user:
        return
    note = " ".join(context.args).strip()
    if not note:
        await msg.reply_text("အသုံးပြုပုံ: /note မှတ်ထားချင်သောစာ")
        return
    if len(note) > 1000:
        await msg.reply_text("မှတ်စုသည် စာလုံး ၁၀၀၀ ထက် မကျော်ရပါ။")
        return
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO member_notes(chat_id,owner_id,note,created_at) VALUES(?,?,?,?)",
            (chat.id, user.id, note, time.time()),
        )
        note_id = cur.lastrowid
    await msg.reply_text(f"📝 မှတ်စု #{note_id} သိမ်းပြီးပါပြီ။")


async def notes_command(update, context):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or not user:
        return
    with connect() as conn:
        rows = conn.execute(
            "SELECT id,note FROM member_notes WHERE chat_id=? AND owner_id=? ORDER BY id DESC LIMIT 15",
            (chat.id, user.id),
        ).fetchall()
    if not rows:
        await msg.reply_text("📝 မှတ်စုမရှိသေးပါ။ /note စာသား ဖြင့် သိမ်းနိုင်ပါတယ်။")
        return
    await msg.reply_text("📝 သင့်မှတ်စုများ\n" + "\n".join(f"#{r['id']} — {r['note']}" for r in rows))


async def delnote_command(update, context):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or not user:
        return
    if not context.args or not context.args[0].isdigit():
        await msg.reply_text("အသုံးပြုပုံ: /delnote ID")
        return
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM member_notes WHERE id=? AND chat_id=? AND owner_id=?",
            (int(context.args[0]), chat.id, user.id),
        )
    await msg.reply_text("🗑️ မှတ်စုဖျက်ပြီးပါပြီ။" if cur.rowcount else "ဒီမှတ်စု ID ကို မတွေ့ပါ။")


async def report_command(update, context):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or not user or chat.type not in ("group", "supergroup"):
        if msg:
            await msg.reply_text("Report ကို Group ထဲမှာ အသုံးပြုပါ။")
        return
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("တိုင်ကြားလိုသောစာကို Reply လုပ်ပြီး /report အကြောင်းပြချက် ဟု ပို့ပါ။")
        return
    target = msg.reply_to_message.from_user
    if target.id == user.id or target.is_bot:
        await msg.reply_text("ဒီ User ကို Report မလုပ်နိုင်ပါ။")
        return
    reason = " ".join(context.args).strip() or "အကြောင်းပြချက် မဖော်ပြထားပါ"
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO member_reports
               (chat_id,reporter_id,target_id,message_id,reason,created_at)
               VALUES(?,?,?,?,?,?)""",
            (chat.id, user.id, target.id, msg.reply_to_message.message_id, reason[:500], time.time()),
        )
        report_id = cur.lastrowid
    await msg.reply_text(f"🚨 Report #{report_id} ကို Group Admin များထံ မှတ်တမ်းတင်ပို့ထားပါပြီ။")
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        for admin in admins:
            if admin.user.is_bot:
                continue
            try:
                await context.bot.send_message(
                    admin.user.id,
                    f"🚨 Group Report #{report_id}\nGroup: {chat.title} ({chat.id})\n"
                    f"တိုင်သူ: {user.full_name} ({user.id})\n"
                    f"တိုင်ခံရသူ: {target.full_name} ({target.id})\n"
                    f"အကြောင်းပြချက်: {reason[:500]}\n"
                    f"Message ID: {msg.reply_to_message.message_id}",
                )
            except Exception:
                pass
    except Exception:
        pass


async def reports_command(update, context):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or not user or chat.type not in ("group", "supergroup"):
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status not in ("administrator", "creator"):
            await msg.reply_text("Group Admin များသာ Report စာရင်းကြည့်နိုင်ပါတယ်။")
            return
    except Exception:
        await msg.reply_text("Admin အခွင့်အရေး စစ်ဆေးမရပါ။")
        return
    with connect() as conn:
        rows = conn.execute(
            "SELECT id,reporter_id,target_id,reason,created_at FROM member_reports "
            "WHERE chat_id=? AND status='open' ORDER BY id DESC LIMIT 15",
            (chat.id,),
        ).fetchall()
    if not rows:
        await msg.reply_text("🚨 ဖွင့်ထားသော Report မရှိပါ။")
        return
    lines = ["🚨 Open Reports (နောက်ဆုံး ၁၅ ခု)"]
    for row in rows:
        lines.append(
            f"#{row['id']} | တိုင်သူ {row['reporter_id']} → တိုင်ခံရသူ {row['target_id']}\n"
            f"အကြောင်းပြချက်: {row['reason']}"
        )
    lines.append("ပိတ်ရန်: /closereport ID")
    await msg.reply_text("\n\n".join(lines))


async def close_report_command(update, context):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or not user or chat.type not in ("group", "supergroup"):
        return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status not in ("administrator", "creator"):
            await msg.reply_text("Group Admin များသာ Report ပိတ်နိုင်ပါတယ်။")
            return
    except Exception:
        await msg.reply_text("Admin အခွင့်အရေး စစ်ဆေးမရပါ။")
        return
    if not context.args or not context.args[0].isdigit():
        await msg.reply_text("အသုံးပြုပုံ: /closereport ID")
        return
    with connect() as conn:
        cur = conn.execute(
            "UPDATE member_reports SET status='closed' WHERE id=? AND chat_id=? AND status='open'",
            (int(context.args[0]), chat.id),
        )
    await msg.reply_text("✅ Report ပိတ်ပြီးပါပြီ။" if cur.rowcount else "ဖွင့်ထားသော Report ID မတွေ့ပါ။")
