import re
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

DB_PATH = Path(__file__).resolve().parent / "replies.db"
SPAM_WINDOW = defaultdict(deque)

def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def init_security_db():
    with _db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS security_settings(
            chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
            anti_link INTEGER NOT NULL DEFAULT 0, anti_spam INTEGER NOT NULL DEFAULT 0,
            raid_limit INTEGER NOT NULL DEFAULT 3)""")
        c.execute("""CREATE TABLE IF NOT EXISTS raid_actions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
            actor_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
            action TEXT NOT NULL, created_at REAL NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS owner_cases(
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
            actor_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
            action TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_raid_actor ON raid_actions(chat_id,actor_id,created_at)")

def get_security(chat_id):
    with _db() as c:
        c.execute("INSERT OR IGNORE INTO security_settings(chat_id) VALUES(?)",(chat_id,))
        return dict(c.execute("SELECT * FROM security_settings WHERE chat_id=?",(chat_id,)).fetchone())

def set_security(chat_id,key,value):
    if key not in {"enabled","anti_link","anti_spam","raid_limit"}: raise ValueError("invalid setting")
    with _db() as c:
        c.execute("INSERT OR IGNORE INTO security_settings(chat_id) VALUES(?)",(chat_id,))
        c.execute(f"UPDATE security_settings SET {key}=? WHERE chat_id=?",(value,chat_id))

def contains_link(text):
    if not text: return False
    return bool(re.search(r"(https?://|www\\.|t\\.me/|telegram\\.me/|\\b[a-z0-9-]+\\.(com|net|org|io|me|xyz|site|online|info)\\b)",text,re.I))

def is_admin_member(member):
    return member.status in ("administrator", "creator")

async def moderate_message(update, context, owner_id):
    msg=update.effective_message
    chat=update.effective_chat
    user=update.effective_user
    if not msg or not chat or chat.type not in ("group","supergroup") or not user or user.is_bot:
        return
    cfg=get_security(chat.id)
    if not cfg["enabled"]: return
    try:
        member=await context.bot.get_chat_member(chat.id,user.id)
        if is_admin_member(member): return
    except Exception:
        return
    text=msg.text or msg.caption or ""
    reason=None
    if cfg["anti_link"] and contains_link(text): reason="Link ခွင့်မပြုပါ"
    if cfg["anti_spam"]:
        now=time.time(); key=(chat.id,user.id); q=SPAM_WINDOW[key]
        while q and now-q[0][0]>12: q.popleft()
        normalized=" ".join(text.casefold().split())
        if normalized and any(item[1]==normalized for item in q): reason="ထပ်ခါထပ်ခါ စာပို့ခြင်း (Spam)"
        q.append((now,normalized))
    if reason:
        try:
            await msg.delete()
            await context.bot.send_message(chat.id,f"🛡️ {user.mention_html()} — {reason}",parse_mode="HTML")
        except Exception:
            pass

async def monitor_member_change(update, context, owner_id):
    change=update.chat_member
    if not change or not owner_id: return
    chat=change.chat
    if chat.type not in ("group","supergroup"): return
    actor=change.from_user
    target=change.new_chat_member.user
    if actor.is_bot or actor.id==owner_id or actor.id==target.id: return
    old=change.old_chat_member.status
    new=change.new_chat_member.status
    removal=(new in ("kicked", "left") and old not in ("kicked", "left"))
    demotion=(old in ("administrator", "creator") and new not in ("administrator", "creator"))
    if not (removal or demotion): return
    action="member removal" if removal else "admin demotion"
    now=time.time()
    with _db() as c:
        c.execute("INSERT INTO raid_actions(chat_id,actor_id,target_id,action,created_at) VALUES(?,?,?,?,?)",(chat.id,actor.id,target.id,action,now))
        c.execute("DELETE FROM raid_actions WHERE created_at<?",(now-120,))
        cfg=get_security(chat.id)
        if not cfg["enabled"]: return
        n=c.execute("SELECT COUNT(*) n FROM raid_actions WHERE chat_id=? AND actor_id=? AND created_at>=?",(chat.id,actor.id,now-60)).fetchone()["n"]
        if n < cfg["raid_limit"]: return
        recent=c.execute("SELECT id FROM owner_cases WHERE chat_id=? AND actor_id=? AND status='pending' AND created_at>=?",(chat.id,actor.id,now-300)).fetchone()
        if recent: return
        cur=c.execute("INSERT INTO owner_cases(chat_id,actor_id,target_id,action,created_at) VALUES(?,?,?,?,?)",(chat.id,actor.id,target.id,action,now))
        case_id=cur.lastrowid
    keyboard=InlineKeyboardMarkup([[
        InlineKeyboardButton("🔨 Ban လုပ်မယ်",callback_data=f"raid:ban:{case_id}"),
        InlineKeyboardButton("🛡️ မဘန်းပါ",callback_data=f"raid:ignore:{case_id}")
    ]])
    try:
        await context.bot.send_message(owner_id,
            f"🚨 Anti-Nuke သတိပေးချက်\\nGroup: {chat.title} ({chat.id})\\nလုပ်ဆောင်သူ: {actor.full_name} ({actor.id})\\nနောက်ဆုံး ၆၀ စက္ကန့်အတွင်း Member ဖယ်ရှား/ Admin ဖြုတ်မှု {n} ကြိမ်။\\nဆုံးဖြတ်ပါ။",
            reply_markup=keyboard)
    except Exception:
        pass

async def owner_raid_decision(update, context, owner_id):
    q=update.callback_query
    if not q or not q.data.startswith("raid:"): return False
    if not owner_id or q.from_user.id!=owner_id:
        await q.answer("Bot Owner သာ ဆုံးဖြတ်နိုင်ပါတယ်။",show_alert=True); return True
    _,decision,raw_id=q.data.split(":")
    with _db() as c:
        case=c.execute("SELECT * FROM owner_cases WHERE id=?",(int(raw_id),)).fetchone()
        if not case or case["status"]!="pending":
            await q.answer("ဒီအမှုကို ဆုံးဖြတ်ပြီးပါပြီ။",show_alert=True); return True
        c.execute("UPDATE owner_cases SET status=? WHERE id=?",("ban" if decision=="ban" else "ignored",int(raw_id)))
    if decision=="ban":
        try:
            await context.bot.ban_chat_member(case["chat_id"],case["actor_id"])
            result="✅ သံသယရှိသူကို Ban လုပ်ပြီးပါပြီ။"
        except Exception as e:
            result=f"❌ Ban မအောင်မြင်ပါ။ Bot ကို Admin + Ban permission ပေးထားပါ။ ({type(e).__name__})"
    else:
        result="🛡️ Owner ဆုံးဖြတ်ချက်အရ Ban မလုပ်ပါ။"
    await q.answer("ဆုံးဖြတ်ချက် သိမ်းပြီးပါပြီ။")
    await q.edit_message_text((q.message.text or "")+"\\n\\n"+result)
    return True
