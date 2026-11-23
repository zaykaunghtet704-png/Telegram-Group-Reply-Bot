"""Group command games and persistent virtual-coin economy."""
import json
import random
import time

from telegram import Update
from telegram.ext import ContextTypes

from database import connect as db

STARTING_COINS = 100
DAILY_COINS = 50
QUIZ_REWARD = 25
DUEL_REWARD = 40

QUIZZES = [
    ("မြန်မာနိုင်ငံ၏ မြို့တော်က ဘယ်မြို့လဲ?", ["ရန်ကုန်", "နေပြည်တော်", "မန္တလေး", "ပဲခူး"], 2),
    ("တစ်ပတ်မှာ ရက်ဘယ်နှစ်ရက်ရှိလဲ?", ["၅", "၆", "၇", "၈"], 3),
    ("ကမ္ဘာ့အကြီးဆုံး သမုဒ္ဒရာက ဘာလဲ?", ["အတ္တလန္တိတ်", "အိန္ဒိယ", "အာတိတ်", "ပစိဖိတ်"], 4),
    ("ရေ၏ ဓာတုဖော်မြူလာက ဘာလဲ?", ["CO2", "H2O", "O2", "NaCl"], 2),
    ("တစ်နာရီမှာ မိနစ်ဘယ်နှစ်မိနစ်ရှိလဲ?", ["၃၀", "၄၅", "၆၀", "၉၀"], 3),
    ("နေကို ကမ္ဘာက ဘယ်လိုပတ်လဲ?", ["ပတ်လမ်းဖြင့်", "မပတ်ပါ", "တစ်ရက်တစ်ပတ်", "တစ်ပတ်တစ်ကြိမ်"], 1),
]

FISH = [
    ("ငါးသေး", 5, 12, 55),
    ("ငါးကြင်း", 12, 22, 28),
    ("ငါးကြီး", 20, 35, 13),
    ("ရွှေငါး", 35, 60, 4),
]


def init_games():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS game_groups (
            chat_id BIGINT PRIMARY KEY,
            enabled BIGINT NOT NULL DEFAULT 1
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS game_accounts (
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            coins BIGINT NOT NULL DEFAULT 100,
            xp BIGINT NOT NULL DEFAULT 0,
            wins BIGINT NOT NULL DEFAULT 0,
            losses BIGINT NOT NULL DEFAULT 0,
            last_daily DOUBLE PRECISION NOT NULL DEFAULT 0,
            PRIMARY KEY(chat_id, user_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS game_cooldowns (
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            action TEXT NOT NULL,
            last_used DOUBLE PRECISION NOT NULL,
            PRIMARY KEY(chat_id, user_id, action)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS game_quizzes (
            chat_id BIGINT PRIMARY KEY,
            question TEXT NOT NULL,
            options TEXT NOT NULL,
            answer BIGINT NOT NULL,
            expires_at DOUBLE PRECISION NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS game_duels (
            chat_id BIGINT PRIMARY KEY,
            challenger_id BIGINT NOT NULL,
            opponent_id BIGINT NOT NULL,
            challenger_name TEXT NOT NULL,
            opponent_name TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL
        )""")


def set_games_enabled(chat_id, enabled):
    with db() as conn:
        conn.execute(
            "INSERT INTO game_groups(chat_id,enabled) VALUES(?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled",
            (chat_id, int(bool(enabled))),
        )


def games_enabled(chat_id):
    with db() as conn:
        row = conn.execute("SELECT enabled FROM game_groups WHERE chat_id=?", (chat_id,)).fetchone()
    return True if not row else bool(row["enabled"])


def _display(user):
    return (user.full_name or user.username or str(user.id))[:80]


def _account(chat_id, user):
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO game_accounts(chat_id,user_id,username,display_name,coins) VALUES(?,?,?,?,?)",
            (chat_id, user.id, user.username or "", _display(user), STARTING_COINS),
        )
        conn.execute(
            "UPDATE game_accounts SET username=?,display_name=? WHERE chat_id=? AND user_id=?",
            (user.username or "", _display(user), chat_id, user.id),
        )
        return conn.execute(
            "SELECT * FROM game_accounts WHERE chat_id=? AND user_id=?",
            (chat_id, user.id),
        ).fetchone()


def _change(chat_id, user, coins=0, xp=0, wins=0, losses=0):
    _account(chat_id, user)
    with db() as conn:
        conn.execute(
            "UPDATE game_accounts SET coins=MAX(0,coins+?), xp=xp+?, wins=wins+?, losses=losses+? "
            "WHERE chat_id=? AND user_id=?",
            (int(coins), int(xp), int(wins), int(losses), chat_id, user.id),
        )
        return conn.execute(
            "SELECT * FROM game_accounts WHERE chat_id=? AND user_id=?",
            (chat_id, user.id),
        ).fetchone()


def _level(xp):
    return 1 + int(max(0, xp) // 100)


def _group(update):
    chat = update.effective_chat
    return bool(chat and chat.type in ("group", "supergroup"))


async def _ready(update):
    msg = update.effective_message
    if not _group(update):
        await msg.reply_text("ဒီဂိမ်းကို Group ထဲမှာပဲ ကစားနိုင်ပါတယ်။")
        return False
    if not games_enabled(update.effective_chat.id):
        await msg.reply_text("🎮 ဒီ Group မှာ ဂိမ်းစနစ် ပိတ်ထားပါတယ်။ Admin ကို /game on လုပ်ခိုင်းပါ။")
        return False
    return True


def _cooldown(chat_id, user_id, action, seconds):
    now = time.time()
    with db() as conn:
        row = conn.execute(
            "SELECT last_used FROM game_cooldowns WHERE chat_id=? AND user_id=? AND action=?",
            (chat_id, user_id, action),
        ).fetchone()
        if row and now - float(row["last_used"]) < seconds:
            return max(1, int(seconds - (now - float(row["last_used"]))))
        conn.execute(
            "INSERT INTO game_cooldowns(chat_id,user_id,action,last_used) VALUES(?,?,?,?) "
            "ON CONFLICT(chat_id,user_id,action) DO UPDATE SET last_used=excluded.last_used",
            (chat_id, user_id, action, now),
        )
    return 0


async def game_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    await update.effective_message.reply_text(
        "🎮 GROUP GAME CENTER\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "/balance — Coin လက်ကျန်\n"
        "/daily — နေ့စဉ် Coin 50 ယူရန်\n"
        "/topcoins — Coin အများဆုံး Top 10\n"
        "/quiz — Quiz မေးခွန်းစရန်\n"
        "/answer 1-4 — Quiz ဖြေရန်\n"
        "/fish — ငါးဖမ်းရန်\n"
        "/hunt — Monster တိုက်ရန်\n"
        "/spin — Lucky Spin (12 နာရီတစ်ကြိမ်)\n"
        "/duel — Reply လုပ်ထားသူကို Duel စိန်ခေါ်ရန်\n"
        "/duelaccept — Duel လက်ခံရန်\n"
        "/dueldecline — Duel ငြင်းရန်\n\n"
        "🪙 စတင် Coin 100 | Level တက်ရန် XP 100 လိုပါတယ်။"
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    row = _account(update.effective_chat.id, update.effective_user)
    await update.effective_message.reply_text(
        f"👤 {row['display_name']}\n🪙 Coin: {row['coins']:,}\n"
        f"⭐ Level: {_level(row['xp'])} | XP: {row['xp']:,}\n"
        f"🏆 အနိုင်: {row['wins']} | ရှုံး: {row['losses']}"
    )


async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    chat_id, user = update.effective_chat.id, update.effective_user
    row = _account(chat_id, user)
    now = time.time()
    if now - float(row["last_daily"]) < 86400:
        remain = int(86400 - (now - float(row["last_daily"])))
        hours, minutes = divmod(remain // 60, 60)
        await update.effective_message.reply_text(f"⏳ Daily Reward ပြန်ယူရန် {hours} နာရီ {minutes} မိနစ် စောင့်ပါ။")
        return
    with db() as conn:
        conn.execute(
            "UPDATE game_accounts SET coins=coins+?, xp=xp+10, last_daily=? WHERE chat_id=? AND user_id=?",
            (DAILY_COINS, now, chat_id, user.id),
        )
    await update.effective_message.reply_text(f"🎁 Daily Reward ရပါပြီ။ +{DAILY_COINS} Coin၊ +10 XP")


async def topcoins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    with db() as conn:
        rows = conn.execute(
            "SELECT display_name,coins,xp,wins FROM game_accounts WHERE chat_id=? "
            "ORDER BY coins DESC, xp DESC LIMIT 10",
            (update.effective_chat.id,),
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text("🏆 Leaderboard မရှိသေးပါ။")
        return
    lines = ["🏆 COIN LEADERBOARD — TOP 10"]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {row['display_name']} — 🪙 {row['coins']:,} | Lv.{_level(row['xp'])}")
    await update.effective_message.reply_text("\n".join(lines))


async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    chat_id = update.effective_chat.id
    question, options, answer = random.choice(QUIZZES)
    with db() as conn:
        conn.execute(
            "INSERT INTO game_quizzes(chat_id,question,options,answer,expires_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET question=excluded.question,options=excluded.options,"
            "answer=excluded.answer,expires_at=excluded.expires_at",
            (chat_id, question, json.dumps(options, ensure_ascii=False), answer, time.time() + 90),
        )
    lines = ["🧠 QUIZ BATTLE", question]
    lines.extend(f"{i}. {option}" for i, option in enumerate(options, 1))
    lines.append("\n/answer 1-4 ဖြေပါ။ ပထမဆုံးမှန်သူ +25 Coin / +10 XP ရမယ်။ (90 စက္ကန့်)")
    await update.effective_message.reply_text("\n".join(lines))


async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    if not context.args or not context.args[0].isdigit() or not 1 <= int(context.args[0]) <= 4:
        await update.effective_message.reply_text("အသုံးပြုပုံ: /answer 1 (1 မှ 4 အတွင်း)")
        return
    chat_id, user = update.effective_chat.id, update.effective_user
    with db() as conn:
        row = conn.execute("SELECT * FROM game_quizzes WHERE chat_id=?", (chat_id,)).fetchone()
        if not row or float(row["expires_at"]) < time.time():
            conn.execute("DELETE FROM game_quizzes WHERE chat_id=?", (chat_id,))
            await update.effective_message.reply_text("⏰ Quiz မရှိတော့ပါ။ /quiz နဲ့ အသစ်စပါ။")
            return
        if int(row["answer"]) != int(context.args[0]):
            await update.effective_message.reply_text("❌ မမှန်သေးပါဘူး။ ထပ်စမ်းကြည့်ပါ။")
            return
        conn.execute("DELETE FROM game_quizzes WHERE chat_id=?", (chat_id,))
    account = _change(chat_id, user, coins=QUIZ_REWARD, xp=10, wins=1)
    await update.effective_message.reply_text(
        f"🎉 {user.full_name} မှန်ပါတယ်။ +{QUIZ_REWARD} Coin / +10 XP\n"
        f"🪙 လက်ကျန်: {account['coins']:,} Coin"
    )


async def fish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    chat_id, user = update.effective_chat.id, update.effective_user
    wait = _cooldown(chat_id, user.id, "fish", 60)
    if wait:
        await update.effective_message.reply_text(f"🎣 နောက်တစ်ကြိမ် ငါးဖမ်းရန် {wait} စက္ကန့် စောင့်ပါ။")
        return
    roll = random.randint(1, 100)
    chosen = next((x for x in FISH if roll <= x[3]), None)
    if chosen:
        name, low, high, _ = chosen
        reward = random.randint(low, high)
        row = _change(chat_id, user, coins=reward, xp=5)
        await update.effective_message.reply_text(
            f"🎣 {name} ဖမ်းမိပြီ။ +{reward} Coin / +5 XP\n🪙 လက်ကျန် {row['coins']:,}"
        )
    else:
        await update.effective_message.reply_text("🌊 ဒီတစ်ခေါက် ငါးမမိပါဘူး။ နောက်တစ်ကြိမ် ကြိုးစားပါ။")


async def hunt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    chat_id, user = update.effective_chat.id, update.effective_user
    wait = _cooldown(chat_id, user.id, "hunt", 120)
    if wait:
        await update.effective_message.reply_text(f"⚔️ နောက်တစ်ကြိမ် Hunt လုပ်ရန် {wait} စက္ကန့် စောင့်ပါ။")
        return
    monster = random.choice(["Goblin", "Wolf", "Shadow Beast", "Dragonling"])
    if random.randint(1, 100) <= 65:
        reward = random.randint(20, 55)
        row = _change(chat_id, user, coins=reward, xp=20, wins=1)
        await update.effective_message.reply_text(
            f"⚔️ {monster} ကို အနိုင်ယူလိုက်ပြီ။ +{reward} Coin / +20 XP\n"
            f"🪙 လက်ကျန်: {row['coins']:,} | Level: {_level(row['xp'])}"
        )
    else:
        _change(chat_id, user, xp=5, losses=1)
        await update.effective_message.reply_text(f"💥 {monster} က အနိုင်ရသွားတယ်။ +5 XP ရပါတယ်။ နောက်တစ်ကြိမ် ကြိုးစားပါ။")


async def spin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    chat_id, user = update.effective_chat.id, update.effective_user
    wait = _cooldown(chat_id, user.id, "spin", 43200)
    if wait:
        hours, mins = divmod(wait // 60, 60)
        await update.effective_message.reply_text(f"🎰 Spin ပြန်လှည့်ရန် {hours} နာရီ {mins} မိနစ် စောင့်ပါ။")
        return
    reward = random.choices([0, 10, 25, 50, 100, 200], weights=[20, 25, 25, 18, 10, 2], k=1)[0]
    if reward:
        row = _change(chat_id, user, coins=reward, xp=3)
        await update.effective_message.reply_text(f"🎰 Lucky Spin! +{reward} Coin ရပါပြီ။\n🪙 လက်ကျန်: {row['coins']:,}")
    else:
        await update.effective_message.reply_text("🎰 ဒီတစ်ကြိမ် ဆုမပေါက်ပါဘူး။ နောက်တစ်ကြိမ် ကံစမ်းပါ။")


async def duel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    msg, user = update.effective_message, update.effective_user
    target = msg.reply_to_message.from_user if msg.reply_to_message else None
    if not target or target.is_bot or target.id == user.id:
        await msg.reply_text("🥊 Duel စိန်ခေါ်ရန် ပြိုင်ဘက်ရဲ့ Message ကို Reply လုပ်ပြီး /duel ပို့ပါ။")
        return
    _account(update.effective_chat.id, user)
    _account(update.effective_chat.id, target)
    with db() as conn:
        conn.execute(
            "INSERT INTO game_duels(chat_id,challenger_id,opponent_id,challenger_name,opponent_name,created_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET "
            "challenger_id=excluded.challenger_id,opponent_id=excluded.opponent_id,"
            "challenger_name=excluded.challenger_name,opponent_name=excluded.opponent_name,created_at=excluded.created_at",
            (update.effective_chat.id, user.id, target.id, _display(user), _display(target), time.time()),
        )
    await msg.reply_text(
        f"🥊 {_display(user)} က {_display(target)} ကို Duel စိန်ခေါ်လိုက်ပြီ။\n"
        f"လက်ခံရန် /duelaccept | ငြင်းရန် /dueldecline\n"
        "အနိုင်ရသူ +40 Coin / +20 XP ရမယ်။"
    )


async def duel_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    chat_id, user = update.effective_chat.id, update.effective_user
    with db() as conn:
        row = conn.execute("SELECT * FROM game_duels WHERE chat_id=?", (chat_id,)).fetchone()
        if not row or float(row["created_at"]) < time.time() - 300:
            conn.execute("DELETE FROM game_duels WHERE chat_id=?", (chat_id,))
            await update.effective_message.reply_text("🥊 လက်ခံနိုင်တဲ့ Duel မရှိပါ။ /duel နဲ့ စိန်ခေါ်ပါ။")
            return
        if int(row["opponent_id"]) != user.id:
            await update.effective_message.reply_text("ဒီ Duel ကို စိန်ခေါ်ခံထားရသူသာ လက်ခံနိုင်ပါတယ်။")
            return
        conn.execute("DELETE FROM game_duels WHERE chat_id=?", (chat_id,))
    winner_id = random.choice([int(row["challenger_id"]), int(row["opponent_id"])])
    winner = update.effective_user if winner_id == user.id else None
    if winner is None:
        from telegram import User
        winner = User(id=int(row["challenger_id"]), first_name=row["challenger_name"], is_bot=False)
    _change(chat_id, winner, coins=DUEL_REWARD, xp=20, wins=1)
    loser_id = int(row["opponent_id"]) if winner_id == int(row["challenger_id"]) else int(row["challenger_id"])
    with db() as conn:
        conn.execute(
            "UPDATE game_accounts SET losses=losses+1 WHERE chat_id=? AND user_id=?",
            (chat_id, loser_id),
        )
    await update.effective_message.reply_text(
        f"🏆 Duel ပြီးဆုံးပါပြီ။\nအနိုင်ရသူ: {winner.full_name}\n"
        f"🎁 ဆု: +{DUEL_REWARD} Coin / +20 XP"
    )


async def duel_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    chat_id, user = update.effective_chat.id, update.effective_user
    with db() as conn:
        row = conn.execute("SELECT * FROM game_duels WHERE chat_id=?", (chat_id,)).fetchone()
        if not row or int(row["opponent_id"]) != user.id:
            await update.effective_message.reply_text("သင့်ကို စိန်ခေါ်ထားတဲ့ Duel မရှိပါ။")
            return
        conn.execute("DELETE FROM game_duels WHERE chat_id=?", (chat_id,))
    await update.effective_message.reply_text("🚫 Duel ကို ငြင်းဆိုလိုက်ပါပြီ။")
