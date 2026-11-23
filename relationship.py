"""18+ opt-in relationship profiles, channel join gate and consent-based publishing."""
import os
import time
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import (
    CommandHandler, CallbackQueryHandler, ConversationHandler,
    MessageHandler, filters,
)
from database import connect

AGE, GENDER, CITY, LOOKING_FOR, BIO, PHOTO = range(6)
OWNER_ID = int(os.getenv("OWNER_ID", "7974865879") or 7974865879)


def init_relationship():
    with connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS love_profiles (
            user_id BIGINT PRIMARY KEY, display_name TEXT NOT NULL,
            age INTEGER NOT NULL, gender TEXT NOT NULL, city TEXT NOT NULL,
            looking_for TEXT NOT NULL, bio TEXT NOT NULL DEFAULT '',
            photo_file_id TEXT NOT NULL DEFAULT '', approved INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1, consent_publish INTEGER NOT NULL DEFAULT 0,
            created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS love_channels (
            channel_id BIGINT PRIMARY KEY, title TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '', added_at DOUBLE PRECISION NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS love_group_settings (
            chat_id BIGINT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS love_dm_subscribers (
            user_id BIGINT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
            updated_at DOUBLE PRECISION NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS love_likes (
            from_user BIGINT NOT NULL, to_user BIGINT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL, PRIMARY KEY(from_user,to_user)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS love_blocks (
            blocker_id BIGINT NOT NULL, blocked_id BIGINT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL, PRIMARY KEY(blocker_id,blocked_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS love_reports (
            id BIGSERIAL PRIMARY KEY, reporter_id BIGINT NOT NULL,
            reported_id BIGINT NOT NULL, reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open', created_at DOUBLE PRECISION NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS love_deliveries (
            id BIGSERIAL PRIMARY KEY, owner_id BIGINT NOT NULL,
            target_chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL
        )""")


def _user(update):
    return update.effective_user


async def love_home(update, context):
    msg = update.effective_message
    keys = [
        [InlineKeyboardButton("📝 Profile ထည့်/ပြင်", callback_data="love:profile")],
        [InlineKeyboardButton("🔎 ရည်းစားရှာ", callback_data="love:search")],
        [InlineKeyboardButton("📣 ကိုယ့် Post တင်မယ်", callback_data="love:publish")],
        [InlineKeyboardButton("💌 Match လက်ခံမှု ON/OFF", callback_data="love:dm")],
        [InlineKeyboardButton("🗑️ ကိုယ့် Profile ဖျက်မယ်", callback_data="love:delete")],
    ]
    await msg.reply_text(
        "💘 RELATIONSHIP SYSTEM\nအသက် 18 နှစ်နှင့်အထက် အသုံးပြုသူများအတွက်သာ။\n"
        "Profile ကို Private Chat မှာ ဖြည့်ပါ။ ဖြန့်ဝေမည့်နေရာများကို မိမိသဘောတူမှသာ Post တင်ပါ။",
        reply_markup=InlineKeyboardMarkup(keys),
    )


async def profile_start(update, context):
    if update.effective_chat.type != "private":
        me = await context.bot.get_me()
        await update.effective_message.reply_text(
            f"🔒 ကိုယ်ရေးအချက်အလက် လုံခြုံရေးအတွက် Private Chat မှာ ဖြည့်ပါ။\n"
            f"https://t.me/{me.username} ကိုဖွင့်ပြီး /loveprofile ပို့ပါ။"
        )
        return ConversationHandler.END
    await update.effective_message.reply_text("အသက် ဘယ်နှစ်နှစ်ရှိပြီလဲ။ (18+ သာ)\nပယ်ဖျက်ရန် /cancel")
    return AGE


async def age_step(update, context):
    raw = (update.effective_message.text or "").strip()
    if not raw.isdigit() or not 18 <= int(raw) <= 100:
        await update.effective_message.reply_text("18 မှ 100 အတွင်း အသက်ကို ဂဏန်းဖြင့် ထည့်ပါ။")
        return AGE
    context.user_data["love_age"] = int(raw)
    await update.effective_message.reply_text("ကျား / မ / မဖော်ပြလို — ဘယ်ဟာလဲ။")
    return GENDER


async def gender_step(update, context):
    value = (update.effective_message.text or "").strip()
    allowed = {"ကျား", "မ", "မဖော်ပြလို", "male", "female", "other"}
    if value.casefold() not in {x.casefold() for x in allowed}:
        await update.effective_message.reply_text("ကျား၊ မ၊ မဖော်ပြလို ထဲမှ ရွေးထည့်ပါ။")
        return GENDER
    context.user_data["love_gender"] = value[:20]
    await update.effective_message.reply_text("နေထိုင်ရာ မြို့ကို ထည့်ပါ။")
    return CITY


async def city_step(update, context):
    value = (update.effective_message.text or "").strip()
    if not 2 <= len(value) <= 60:
        await update.effective_message.reply_text("မြို့အမည်ကို 2-60 လုံးအတွင်း ထည့်ပါ။")
        return CITY
    context.user_data["love_city"] = value
    await update.effective_message.reply_text("ဘယ်သူကို ရှာချင်ပါသလဲ။ ဥပမာ - ကျား / မ / အားလုံး")
    return LOOKING_FOR


async def looking_step(update, context):
    value = (update.effective_message.text or "").strip()
    if not 2 <= len(value) <= 60:
        await update.effective_message.reply_text("ရှာဖွေလိုသူကို ထည့်ပါ။")
        return LOOKING_FOR
    context.user_data["love_looking"] = value
    await update.effective_message.reply_text("ကိုယ့်အကြောင်း မိတ်ဆက်စာ ထည့်ပါ။ (အများဆုံး 500 လုံး)")
    return BIO


async def bio_step(update, context):
    value = (update.effective_message.text or "").strip()
    if len(value) > 500:
        await update.effective_message.reply_text("မိတ်ဆက်စာ 500 လုံးထက် မကျော်ရပါ။")
        return BIO
    context.user_data["love_bio"] = value
    await update.effective_message.reply_text("Profile ဓာတ်ပုံ ပို့ပါ။ မထည့်လိုပါက skip ဟု ရေးပါ။")
    return PHOTO


async def photo_step(update, context):
    msg, user = update.effective_message, _user(update)
    photo_id = ""
    if msg.photo:
        photo_id = msg.photo[-1].file_id
    elif (msg.text or "").strip().lower() != "skip":
        await msg.reply_text("ဓာတ်ပုံပို့ပါ၊ မထည့်လိုပါက skip ဟု ရေးပါ။")
        return PHOTO
    now = time.time()
    name = (user.full_name or "Member")[:80]
    with connect() as conn:
        conn.execute("""INSERT INTO love_profiles
            (user_id,display_name,age,gender,city,looking_for,bio,photo_file_id,
             approved,active,consent_publish,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,0,1,0,?,?)
            ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name,
            age=excluded.age,gender=excluded.gender,city=excluded.city,
            looking_for=excluded.looking_for,bio=excluded.bio,
            photo_file_id=excluded.photo_file_id,approved=0,active=1,
            consent_publish=0,updated_at=excluded.updated_at""",
            (user.id,name,context.user_data["love_age"],context.user_data["love_gender"],
             context.user_data["love_city"],context.user_data["love_looking"],
             context.user_data["love_bio"],photo_id,now,now))
    for key in list(context.user_data):
        if key.startswith("love_"):
            context.user_data.pop(key, None)
    keyboard = [[InlineKeyboardButton("✅ စည်းကမ်းသဘောတူပြီး အတည်ပြုမယ်", callback_data="love:consent")],
                [InlineKeyboardButton("❌ မတင်တော့ပါ", callback_data="love:cancel")]]
    await msg.reply_text(
        "Profile ကို Draft အဖြစ် သိမ်းပြီးပါပြီ။\n"
        "အတည်ပြုပါက သင့်အသက်၊ မြို့၊ မိတ်ဆက်စာနှင့် ရွေးချယ်ထားသော ဓာတ်ပုံကို "
        "သင်သဘောတူထားသည့် Channel/Group/DM များတွင် ပြသနိုင်ပါမည်။ "
        "Telegram username/ဖုန်းနံပါတ်ကို အလိုအလျောက် မဖော်ပြပါ။",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END


async def cancel_profile(update, context):
    await update.effective_message.reply_text("Profile ဖြည့်ခြင်း ပယ်ဖျက်လိုက်ပါပြီ။")
    return ConversationHandler.END


async def _required_channels(bot):
    with connect() as conn:
        return conn.execute("SELECT channel_id,title,username FROM love_channels ORDER BY added_at").fetchall()


async def _joined_all(bot, user_id):
    channels = await _required_channels(bot)
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch["channel_id"], user_id)
            if member.status not in ("creator", "administrator", "member"):
                return False, ch["title"]
        except TelegramError:
            return False, ch["title"]
    return True, ""


async def _publish_profile(bot, user_id):
    with connect() as conn:
        profile = conn.execute("SELECT * FROM love_profiles WHERE user_id=? AND active=1 AND consent_publish=1", (user_id,)).fetchone()
        channels = conn.execute("SELECT channel_id,title FROM love_channels").fetchall()
        groups = conn.execute("""SELECT g.chat_id FROM known_groups g
            JOIN love_group_settings s ON s.chat_id=g.chat_id
            WHERE g.active=1 AND s.enabled=1""").fetchall()
        subscribers = conn.execute("SELECT user_id FROM love_dm_subscribers WHERE enabled=1 AND user_id<>?", (user_id,)).fetchall()
    if not profile:
        return 0, 0
    caption = (f"💘 ရည်းစားရှာ Profile\n\n"
               f"အသက်: {profile['age']}\nလိင်: {profile['gender']}\n"
               f"မြို့: {profile['city']}\nရှာဖွေလိုသူ: {profile['looking_for']}\n"
               f"မိတ်ဆက်: {profile['bio'] or 'မဖော်ပြထားပါ'}\n\n"
               f"စိတ်ဝင်စားပါက Like ပေးနိုင်ပါတယ်။")
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("❤️ Like", callback_data=f"love:like:{user_id}"),
        InlineKeyboardButton("🚩 Report", callback_data=f"love:report:{user_id}")
    ]])
    targets = [int(x["channel_id"]) for x in channels] + [int(x["chat_id"]) for x in groups] + [int(x["user_id"]) for x in subscribers]
    sent = failed = 0
    for target in dict.fromkeys(targets):
        try:
            if profile["photo_file_id"]:
                message = await bot.send_photo(target, profile["photo_file_id"], caption=caption, reply_markup=markup)
            else:
                message = await bot.send_message(target, caption, reply_markup=markup)
            with connect() as conn:
                conn.execute("INSERT INTO love_deliveries(owner_id,target_chat_id,message_id,created_at) VALUES(?,?,?,?)",
                             (user_id,target,message.message_id,time.time()))
            sent += 1
        except TelegramError:
            failed += 1
    return sent, failed


async def publish_command(update, context):
    user, msg = _user(update), update.effective_message
    if update.effective_chat.type != "private":
        await msg.reply_text("🔒 /lovepublish ကို Bot Private Chat မှာ သုံးပါ။")
        return
    channels = await _required_channels(context.bot)
    if not channels:
        await msg.reply_text("⛔ Bot Owner က Required Channel မသတ်မှတ်ရသေးပါ။")
        return
    with connect() as conn:
        consent = conn.execute("SELECT consent_publish FROM love_profiles WHERE user_id=? AND active=1", (user.id,)).fetchone()
    if not consent or not consent["consent_publish"]:
        await msg.reply_text("Profile Post မတင်မီ /love မှာ Profile ဖြည့်ပြီး ဖြန့်ဝေမှုကို သဘောတူပါ။")
        return
    joined, missing = await _joined_all(context.bot, user.id)
    if not joined:
        channels = await _required_channels(context.bot)
        buttons = []
        for ch in channels:
            link = ("https://t.me/" + ch["username"]) if ch["username"] else None
            if link:
                buttons.append([InlineKeyboardButton("📢 Join " + ch["title"][:40], url=link)])
        await msg.reply_text("Post တင်ခွင့်ရဖို့ သတ်မှတ်ထားတဲ့ Channel အားလုံး Join လုပ်ပါ။ ပြီးရင် /lovepublish ပြန်နှိပ်ပါ။",
                             reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)
        return
    with connect() as conn:
        conn.execute("UPDATE love_profiles SET approved=1,updated_at=? WHERE user_id=? AND active=1",
                     (time.time(),user.id))
        row = conn.execute("SELECT consent_publish FROM love_profiles WHERE user_id=? AND active=1", (user.id,)).fetchone()
    if not row:
        await msg.reply_text("အရင်ဆုံး /loveprofile နဲ့ Profile ထည့်ပါ။")
        return
    sent, failed = await _publish_profile(context.bot,user.id)
    await msg.reply_text(f"📣 Post ဖြန့်ဝေမှု ပြီးပါပြီ။\nအောင်မြင်: {sent}\nမအောင်မြင်: {failed}")


async def delete_posts_command(update, context):
    user, msg = _user(update), update.effective_message
    if not user or not msg or update.effective_chat.type != "private":
        if msg:
            await msg.reply_text("🔒 /lovedeleteposts ကို Private Chat မှာ သုံးပါ။")
        return

    with connect() as conn:
        deliveries = conn.execute(
            "SELECT id,target_chat_id,message_id FROM love_deliveries "
            "WHERE owner_id=? ORDER BY id DESC",
            (user.id,),
        ).fetchall()
        conn.execute(
            "UPDATE love_profiles SET active=0,consent_publish=0,updated_at=? WHERE user_id=?",
            (time.time(), user.id),
        )
        conn.execute(
            "DELETE FROM love_likes WHERE from_user=? OR to_user=?",
            (user.id, user.id),
        )

    if not deliveries:
        await msg.reply_text("🗑️ ဖျက်ရန် ယခင် Post မရှိပါ။ Profile ကို ပိတ်ထားပြီးပါပြီ။")
        return

    deleted = failed = 0
    for delivery in deliveries:
        try:
            await context.bot.delete_message(
                chat_id=delivery["target_chat_id"],
                message_id=delivery["message_id"],
            )
            deleted += 1
            with connect() as conn:
                conn.execute("DELETE FROM love_deliveries WHERE id=?", (delivery["id"],))
        except TelegramError:
            failed += 1

    await msg.reply_text(
        "🗑️ Relationship Post ဖျက်ခြင်း ပြီးပါပြီ။\n"
        f"ဖျက်နိုင်: {deleted} ခု\\n"
        f"မဖျက်နိုင်: {failed} ခု\\n"
        "မဖျက်နိုင်သော Post များသည် Bot Permission သို့မဟုတ် Telegram သတ်မှတ်ချက်ကြောင့် ဖြစ်နိုင်ပါသည်။"
    )


async def channel_admin_command(update, context):
    user, msg = _user(update), update.effective_message
    if not user or user.id != OWNER_ID:
        await msg.reply_text("⛔ Bot Owner သာ Channel များ သတ်မှတ်နိုင်ပါတယ်။")
        return
    if not context.args or context.args[0].lower() not in ("add","remove","list"):
        await msg.reply_text("အသုံးပြုပုံ:\n/lovechannel add @channelusername\n/lovechannel remove @channelusername\n/lovechannel list")
        return
    action = context.args[0].lower()
    if action == "list":
        await channel_list_command(update, context)
        return
    if len(context.args) < 2:
        await msg.reply_text("Channel @username ထည့်ပါ။")
        return
    target = context.args[1]
    if action == "add":
        try:
            chat = await context.bot.get_chat(target)
            member = await context.bot.get_chat_member(chat.id, context.bot.id)
            if member.status not in ("administrator", "creator"):
                await msg.reply_text("အဲဒီ Channel မှာ Bot ကို Admin ပေးပါ။")
                return
            with connect() as conn:
                conn.execute("INSERT INTO love_channels(channel_id,title,username,added_at) VALUES(?,?,?,?) "
                             "ON CONFLICT(channel_id) DO UPDATE SET title=excluded.title,username=excluded.username",
                             (chat.id,chat.title or target,chat.username or "",time.time()))
            await msg.reply_text(f"✅ Join Required Channel ထည့်ပြီးပါပြီ: {chat.title}")
        except TelegramError as exc:
            await msg.reply_text(f"Channel ကို မရယူနိုင်ပါ။ Bot ကို Channel Admin ပေးထားကြောင်း စစ်ပါ။ ({type(exc).__name__})")
    else:
        try:
            chat = await context.bot.get_chat(target)
            with connect() as conn:
                conn.execute("DELETE FROM love_channels WHERE channel_id=?", (chat.id,))
            await msg.reply_text("🗑️ Channel ဖယ်ရှားပြီးပါပြီ။")
        except TelegramError:
            await msg.reply_text("Channel မတွေ့ပါ။ @username ကို စစ်ပါ။")


async def channel_list_command(update, context):
    if not _user(update) or _user(update).id != OWNER_ID:
        await update.effective_message.reply_text("Bot Owner သာ ကြည့်နိုင်ပါတယ်။")
        return
    rows = await _required_channels(context.bot)
    await update.effective_message.reply_text("📢 Required Channels\n" + ("\n".join(f"{r['title']} ({r['channel_id']})" for r in rows) or "မသတ်မှတ်ရသေးပါ။"))


async def group_optin_command(update, context):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type not in ("group","supergroup"):
        await msg.reply_text("ဒီ command ကို Group ထဲမှာ သုံးပါ။")
        return
    member = await context.bot.get_chat_member(chat.id, _user(update).id)
    if member.status not in ("administrator","creator") and _user(update).id != OWNER_ID:
        await msg.reply_text("Group Admin သာ ဖွင့်/ပိတ်နိုင်ပါတယ်။")
        return
    if not context.args or context.args[0].lower() not in ("on","off"):
        await msg.reply_text("အသုံးပြုပုံ: /loveposts on|off")
        return
    enabled = int(context.args[0].lower()=="on")
    with connect() as conn:
        conn.execute("INSERT INTO love_group_settings(chat_id,enabled) VALUES(?,?) "
                     "ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled",(chat.id,enabled))
    await msg.reply_text("💘 Relationship Post လက်ခံမှု ဖွင့်ပြီးပါပြီ။" if enabled else "Relationship Post လက်ခံမှု ပိတ်ပြီးပါပြီ။")


async def dm_optin_command(update, context):
    user, msg = _user(update), update.effective_message
    if update.effective_chat.type != "private":
        await msg.reply_text("ဒီ command ကို Bot Private Chat မှာ သုံးပါ။")
        return
    enabled = int(bool(context.args and context.args[0].lower()=="on"))
    with connect() as conn:
        conn.execute("INSERT INTO love_dm_subscribers(user_id,enabled,updated_at) VALUES(?,?,?) "
                     "ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at",
                     (user.id,enabled,time.time()))
    await msg.reply_text("💌 Relationship Post DM လက်ခံမှု " + ("ဖွင့်ပြီးပါပြီ။" if enabled else "ပိတ်ပြီးပါပြီ။"))


async def search_command(update, context):
    user, msg = _user(update), update.effective_message
    if update.effective_chat.type != "private":
        await msg.reply_text("🔒 /lovesearch ကို Private Chat မှာ သုံးပါ။")
        return
    with connect() as conn:
        rows = conn.execute("""SELECT p.* FROM love_profiles p
            WHERE p.active=1 AND p.approved=1 AND p.user_id<>?
            AND NOT EXISTS(SELECT 1 FROM love_blocks b WHERE
                (b.blocker_id=? AND b.blocked_id=p.user_id) OR
                (b.blocker_id=p.user_id AND b.blocked_id=?))
            ORDER BY RANDOM() LIMIT 1""",(user.id,user.id,user.id)).fetchall()
    if not rows:
        await msg.reply_text("လက်ရှိ ရှာဖွေနိုင်တဲ့ Profile မရှိသေးပါ။")
        return
    p=rows[0]
    caption=f"💘 {p['display_name']}\nအသက်: {p['age']}\nလိင်: {p['gender']}\nမြို့: {p['city']}\nရှာဖွေလိုသူ: {p['looking_for']}\nမိတ်ဆက်: {p['bio']}"
    markup=InlineKeyboardMarkup([[InlineKeyboardButton("❤️ Like",callback_data=f"love:like:{p['user_id']}"),
                                  InlineKeyboardButton("🚩 Report",callback_data=f"love:report:{p['user_id']}")]])
    if p["photo_file_id"]:
        await msg.reply_photo(p["photo_file_id"],caption=caption,reply_markup=markup)
    else:
        await msg.reply_text(caption,reply_markup=markup)


async def love_callback(update, context):
    q=update.callback_query
    user=_user(update)
    data=q.data or ""
    if data=="love:profile":
        await q.answer()
        await q.message.reply_text("Profile ဖြည့်ရန် Private Chat မှာ /loveprofile ကို ပို့ပါ။")
        return
    if data=="love:publish":
        await q.answer()
        await publish_command(update,context)
        return
    if data=="love:search":
        await q.answer()
        await search_command(update,context)
        return
    if data=="love:dm":
        await q.answer()
        with connect() as conn:
            row=conn.execute("SELECT enabled FROM love_dm_subscribers WHERE user_id=?",(user.id,)).fetchone()
        enabled=not bool(row and row["enabled"])
        with connect() as conn:
            conn.execute("INSERT INTO love_dm_subscribers(user_id,enabled,updated_at) VALUES(?,?,?) "
                         "ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at",
                         (user.id,int(enabled),time.time()))
        await q.edit_message_text("DM Relationship Post " + ("ဖွင့်ပြီးပါပြီ။" if enabled else "ပိတ်ပြီးပါပြီ။"))
        return
    if data=="love:delete":
        with connect() as conn:
            conn.execute("UPDATE love_profiles SET active=0,consent_publish=0,updated_at=? WHERE user_id=?",(time.time(),user.id))
            conn.execute("DELETE FROM love_likes WHERE from_user=? OR to_user=?",(user.id,user.id))
        await q.answer("Profile ကို ပိတ်လိုက်ပါပြီ။",show_alert=True)
        await q.edit_message_text("Profile ကို ပိတ်ပြီးပါပြီ။ ယခင် Post များကို ဖျက်ရန် /lovedeleteposts ကို သုံးပါ။")
        return
    if data=="love:cancel":
        with connect() as conn:
            conn.execute("UPDATE love_profiles SET active=0,consent_publish=0 WHERE user_id=?",(user.id,))
        await q.answer("မတင်တော့ပါ။")
        await q.edit_message_text("Profile ကို မတင်တော့ပါ။")
        return
    if data=="love:consent":
        with connect() as conn:
            conn.execute("UPDATE love_profiles SET consent_publish=1,updated_at=? WHERE user_id=? AND active=1",
                         (time.time(),user.id))
        await q.answer("သဘောတူညီမှု သိမ်းပြီးပါပြီ။")
        await q.edit_message_text("သဘောတူညီမှု သိမ်းပြီးပါပြီ။ သတ်မှတ် Channel Join ပြီး /lovepublish နှိပ်ပါ။")
        return
    parts=data.split(":")
    if len(parts)!=3:
        await q.answer()
        return
    action,target_raw=parts[1],parts[2]
    if not target_raw.isdigit():
        await q.answer()
        return
    target=int(target_raw)
    if action=="like":
        if target==user.id:
            await q.answer("ကိုယ့် Profile ကို Like မပေးနိုင်ပါ။",show_alert=True)
            return
        with connect() as conn:
            blocked=conn.execute("SELECT 1 FROM love_blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)",(user.id,target,target,user.id)).fetchone()
            profile=conn.execute("SELECT 1 FROM love_profiles WHERE user_id=? AND active=1 AND approved=1 AND consent_publish=1",(target,)).fetchone()
        if blocked:
            await q.answer("ဒီ Profile နဲ့ အပြန်အလှန်ဆက်သွယ်မှု ပိတ်ထားပါတယ်။",show_alert=True)
            return
        if not profile:
            await q.answer("ဒီ Profile ကို မတွေ့ပါ သို့မဟုတ် မတက်ကြွတော့ပါ။",show_alert=True)
            return
        with connect() as conn:
            conn.execute("INSERT INTO love_likes(from_user,to_user,created_at) VALUES(?,?,?) ON CONFLICT DO NOTHING",(user.id,target,time.time()))
            mutual=conn.execute("SELECT 1 FROM love_likes WHERE from_user=? AND to_user=?",(target,user.id)).fetchone()
        await q.answer("Like ပေးပြီးပါပြီ ❤️")
        if mutual:
            try:
                await context.bot.send_message(user.id,f"💞 Match ဖြစ်သွားပါပြီ။ Telegram User ID: {target}\nနှစ်ဦးစလုံး သဘောတူမှသာ ဆက်သွယ်ပါ။")
                await context.bot.send_message(target,f"💞 Match ဖြစ်သွားပါပြီ။ Telegram User ID: {user.id}\nနှစ်ဦးစလုံး သဘောတူမှသာ ဆက်သွယ်ပါ။")
            except TelegramError:
                pass
        return
    if action=="report":
        with connect() as conn:
            conn.execute("INSERT INTO love_reports(reporter_id,reported_id,reason,created_at) VALUES(?,?,?,?)",(user.id,target,"Reported from profile card",time.time()))
        with connect() as conn:
            exists = conn.execute("SELECT 1 FROM love_profiles WHERE user_id=? AND active=1 AND approved=1", (target,)).fetchone()
        if not exists:
            await q.answer("ဒီ Profile ကို မတွေ့ပါ။", show_alert=True)
            return
        await q.answer("Report လက်ခံရရှိပါပြီ။",show_alert=True)
        if OWNER_ID:
            try: await context.bot.send_message(OWNER_ID,f"🚩 Relationship Report\nReporter: {user.id}\nReported: {target}")
            except TelegramError: pass


async def block_command(update, context):
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("အသုံးပြုပုံ: /loveblock USER_ID")
        return
    target=int(context.args[0]); user=_user(update)
    with connect() as conn:
        conn.execute("INSERT INTO love_blocks(blocker_id,blocked_id,created_at) VALUES(?,?,?) ON CONFLICT DO NOTHING",(user.id,target,time.time()))
    await update.effective_message.reply_text("🚫 Block လုပ်ပြီးပါပြီ။")


async def unblock_command(update, context):
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("အသုံးပြုပုံ: /loveunblock USER_ID")
        return
    target = int(context.args[0])
    with connect() as conn:
        cur = conn.execute("DELETE FROM love_blocks WHERE blocker_id=? AND blocked_id=?", (_user(update).id,target))
    await update.effective_message.reply_text("Block ဖြုတ်ပြီးပါပြီ။" if cur.rowcount else "ဒီ User ကို Block မလုပ်ထားပါ။")


async def report_list_command(update, context):
    user=_user(update)
    if not user or user.id!=OWNER_ID:
        await update.effective_message.reply_text("Bot Owner သာ ကြည့်နိုင်ပါတယ်။"); return
    with connect() as conn:
        rows=conn.execute("SELECT id,reporter_id,reported_id,reason FROM love_reports WHERE status='open' ORDER BY id DESC LIMIT 30").fetchall()
    await update.effective_message.reply_text("🚩 Relationship Reports\n"+("\n".join(f"{r['id']}. {r['reporter_id']} → {r['reported_id']}: {r['reason']}" for r in rows) or "Report မရှိပါ။"))


def relationship_handlers():
    conversation = ConversationHandler(
        entry_points=[CommandHandler("loveprofile", profile_start)],
        states={
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, age_step)],
            GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, gender_step)],
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, city_step)],
            LOOKING_FOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, looking_step)],
            BIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, bio_step)],
            PHOTO: [MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), photo_step)],
        },
        fallbacks=[CommandHandler("cancel", cancel_profile)],
        name="love-profile-wizard", persistent=False,
    )
    return [
        conversation,
        CommandHandler("love", love_home),
        CommandHandler("lovepublish", publish_command),
        CommandHandler("lovedeleteposts", delete_posts_command),
        CommandHandler("lovelove", search_command),
        CommandHandler("lovesearch", search_command),
        CommandHandler("lovechannel", channel_admin_command),
        CommandHandler("lovechannels", channel_list_command),
        CommandHandler("loveposts", group_optin_command),
        CommandHandler("lovedm", dm_optin_command),
        CommandHandler("loveblock", block_command),
        CommandHandler("loveunblock", unblock_command),
        CommandHandler("lovereports", report_list_command),
        CallbackQueryHandler(love_callback, pattern=r"^love:"),
    ]
