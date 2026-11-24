import re
import time
from urllib.parse import urlparse
from collections import defaultdict, deque
from database import connect as _db
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

SPAM_WINDOW = defaultdict(deque)
FLOOD_WINDOW = defaultdict(deque)

def init_security_db():
    with _db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS security_settings(
            chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
            anti_link INTEGER NOT NULL DEFAULT 0, anti_spam INTEGER NOT NULL DEFAULT 0,
            raid_limit INTEGER NOT NULL DEFAULT 3)""")
        c.execute("""CREATE TABLE IF NOT EXISTS blocked_words(
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, word TEXT NOT NULL,
            UNIQUE(chat_id,word))""")
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
        c.execute("""CREATE TABLE IF NOT EXISTS allowed_link_domains(chat_id INTEGER NOT NULL, domain TEXT NOT NULL, PRIMARY KEY(chat_id,domain))""")

def get_security(chat_id):
    with _db() as c:
        c.execute("INSERT OR IGNORE INTO security_settings(chat_id) VALUES(?)",(chat_id,))
        return dict(c.execute("SELECT * FROM security_settings WHERE chat_id=?",(chat_id,)).fetchone())

def set_security(chat_id,key,value):
    if key not in {"enabled","anti_link","anti_spam","raid_limit"}: raise ValueError("invalid setting")
    with _db() as c:
        c.execute("INSERT OR IGNORE INTO security_settings(chat_id) VALUES(?)",(chat_id,))
        c.execute(f"UPDATE security_settings SET {key}=? WHERE chat_id=?",(value,chat_id))

def add_blocked_word(chat_id, word):
    word = word.strip().casefold()
    if not word or len(word) > 100:
        return False
    with _db() as c:
        cur = c.execute("INSERT OR IGNORE INTO blocked_words(chat_id,word) VALUES(?,?)",(chat_id,word))
        return cur.rowcount > 0

def list_blocked_words(chat_id):
    with _db() as c:
        return [dict(row) for row in c.execute("SELECT id,word FROM blocked_words WHERE chat_id=? ORDER BY id",(chat_id,)).fetchall()]

def delete_blocked_word(chat_id, word_id):
    with _db() as c:
        cur = c.execute("DELETE FROM blocked_words WHERE chat_id=? AND id=?",(chat_id,word_id))
        return cur.rowcount > 0

def _extract_domains(text, entities=None):
    found = set()
    patterns = re.findall(r"(?i)(?:https?://|www\.)[^\s<>]+|(?:t\.me/|telegram\.me/)[^\s<>]+|\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|me|xyz|site|online|info|app|dev|gg|co|tv)\b[^\s<>]*", text or "")
    for raw in patterns:
        parsed = urlparse(raw.rstrip(".,!?)]}") if "://" in raw else "https://" + raw.rstrip(".,!?)]}"))
        host = (parsed.hostname or "").casefold().removeprefix("www.")
        if host: found.add(host)
    for entity in entities or []:
        url = getattr(entity, "url", None)
        if url:
            parsed = urlparse(url if "://" in url else "https://" + url)
            host = (parsed.hostname or "").casefold().removeprefix("www.")
            if host: found.add(host)
    return found

def contains_link(text):
    return bool(_extract_domains(text))

def add_allowed_domain(chat_id, domain):
    value = (domain or "").strip().casefold().removeprefix("https://").removeprefix("http://").removeprefix("www.").split("/")[0].split(":")[0].strip(".")
    if not value or len(value) > 253 or not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", value): return False
    with _db() as c:
        cur = c.execute("INSERT OR IGNORE INTO allowed_link_domains(chat_id,domain) VALUES(?,?)", (chat_id, value))
        return cur.rowcount > 0

def list_allowed_domains(chat_id):
    with _db() as c:
        return [row["domain"] for row in c.execute("SELECT domain FROM allowed_link_domains WHERE chat_id=? ORDER BY domain", (chat_id,)).fetchall()]

def delete_allowed_domain(chat_id, domain):
    value = (domain or "").strip().casefold().removeprefix("www.")
    with _db() as c:
        cur = c.execute("DELETE FROM allowed_link_domains WHERE chat_id=? AND domain=?", (chat_id, value))
        return cur.rowcount > 0

def _domain_allowed(domain, allowlist):
    return any(domain == allowed or domain.endswith("." + allowed) for allowed in allowlist)

def is_admin_member(member):
    return member.status in ("administrator", "creator")

async def moderate_message(update, context, owner_id):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or chat.type not in ("group", "supergroup") or not user or user.is_bot or (owner_id and user.id == owner_id): return
    cfg = get_security(chat.id)
    if not cfg["enabled"]: return
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if is_admin_member(member): return
    except Exception: return
    body = msg.text or msg.caption or ""
    entities = list(msg.entities or []) + list(msg.caption_entities or [])
    reason = None
    if cfg["anti_link"]:
        allowed = list_allowed_domains(chat.id)
        blocked = sorted(d for d in _extract_domains(body, entities) if not _domain_allowed(d, allowed))
        if blocked: reason = "ခွင့်မပြုထားသော Link (" + blocked[0] + ")"
    if not reason and body:
        with _db() as c:
            words = [row["word"] for row in c.execute("SELECT word FROM blocked_words WHERE chat_id=?", (chat.id,)).fetchall()]
        normalized = " ".join(body.casefold().split())
        if any(word and word in normalized for word in words): reason = "ခွင့်မပြုထားသော စာသား ပါဝင်နေပါသည်"
    if not reason and cfg["anti_spam"]:
        now, key = time.time(), (chat.id, user.id)
        flood = FLOOD_WINDOW[key]
        while flood and now - flood[0] > 10: flood.popleft()
        flood.append(now)
        if len(flood) >= 7: reason = "စာတိုများ အလွန်မြန်ဆန်စွာ ပို့ခြင်း (Flood)"
        if not reason:
            media_signature = ""
            for attr in ("photo", "animation", "video", "sticker", "voice", "audio", "document"):
                item = getattr(msg, attr, None)
                if item:
                    obj = item[-1] if attr == "photo" else item
                    media_signature = attr + ":" + obj.file_unique_id
                    break
            normalized = " ".join((body or media_signature).casefold().split())
            recent = SPAM_WINDOW[key]
            while recent and now - recent[0][0] > 20: recent.popleft()
            if normalized and sum(1 for _, value in recent if value == normalized) >= 2:
                reason = "တူညီသောစာ/Media ကို ထပ်ခါထပ်ခါ ပို့ခြင်း (Spam)"
            if normalized: recent.append((now, normalized))
    if not reason: return
    try: await msg.delete()
    except Exception: return
    try:
        await context.bot.send_message(chat.id, f"🛡️ {user.mention_html()} — {reason}။ စည်းကမ်းကို လိုက်နာပေးပါ။", parse_mode="HTML", disable_web_page_preview=True)
    except Exception: pass

async def monitor_member_change(update, context, owner_id):
    change=update.chat_member
    if not change or not owner_id: return
    chat=change.chat
    if chat.type not in ("group","supergroup"): return
    actor=change.from_user
    target=change.new_chat_member.user
    if actor.is_bot or actor.id==owner_id or target.id==owner_id or actor.id==target.id: return
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
            chr(10).join([f"🚨 Anti-Nuke သတိပေးချက်", f"Group: {chat.title} ({chat.id})", f"လုပ်ဆောင်သူ: {actor.full_name} ({actor.id})", f"နောက်ဆုံး ၆၀ စက္ကန့်အတွင်း Member ဖယ်ရှား/ Admin ဖြုတ်မှု {n} ကြိမ်။", "ဆုံးဖြတ်ပါ။"]),
            reply_markup=keyboard)
    except Exception:
        pass

async def owner_raid_decision(update, context, owner_id):
    q=update.callback_query
    if not q or not q.data.startswith("raid:"):
        return False
    if not owner_id or q.from_user.id != owner_id:
        await q.answer("Bot Owner သာ ဆုံးဖြတ်နိုင်ပါတယ်။", show_alert=True)
        return True
    _, decision, raw_id = q.data.split(":")
    case_id = int(raw_id)
    with _db() as c:
        case = c.execute("SELECT * FROM owner_cases WHERE id=?", (case_id,)).fetchone()
    if not case or case["status"] != "pending":
        await q.answer("ဒီအမှုကို ဆုံးဖြတ်ပြီးပါပြီ။", show_alert=True)
        return True
    if decision == "ban":
        try:
            await context.bot.ban_chat_member(case["chat_id"], case["actor_id"])
            with _db() as c:
                c.execute("UPDATE owner_cases SET status='ban' WHERE id=?", (case_id,))
            result = "✅ သံသယရှိသူကို Ban လုပ်ပြီးပါပြီ။"
        except Exception as e:
            result = ("❌ Ban မအောင်မြင်ပါ။ Bot ကို Ban Users permission ပေးပါ။ "
                      "သံသယရှိသူက Group Admin ဖြစ်နေပါက Group Creator က အရင် Demote လုပ်ရန်လိုနိုင်ပါတယ်။ "
                      f"({type(e).__name__})")
    else:
        with _db() as c:
            c.execute("UPDATE owner_cases SET status='ignored' WHERE id=?", (case_id,))
        result = "🛡️ Owner ဆုံးဖြတ်ချက်အရ Ban မလုပ်ပါ။"
    await q.answer("ဆုံးဖြတ်ချက် ဆောင်ရွက်ပြီးပါပြီ။")
    keep_buttons = q.message.reply_markup if decision == "ban" and result.startswith("❌") else None
    await q.edit_message_text((q.message.text or "") + chr(10)*2 + result, reply_markup=keep_buttons)
    return True
