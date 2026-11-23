"""Group command games and persistent virtual-coin economy."""
import asyncio
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

def _build_quiz_bank():
    """Generate exactly 20,000 deterministic arithmetic questions."""
    bank = []
    rng = random.Random(20261123)
    for n in range(1, 5001):
        divisor = (n % 19) + 2
        cases = [
            (f"🧮 {n+137} + {n*3+29} = ?", n + 137 + n*3 + 29),
            (f"🧮 {n*5+900} - {n+17} = ?", n*5 + 900 - n - 17),
            (f"✖️ {n+11} × {(n % 17)+2} = ?", (n+11) * ((n % 17)+2)),
            (f"➗ {(n+13)*divisor} ÷ {divisor} = ?", n+13),
        ]
        for question, correct in cases:
            distractors = set()
            while len(distractors) < 3:
                candidate = max(0, correct + rng.choice([-19,-13,-7,-3,2,5,9,14,21]))
                if candidate != correct:
                    distractors.add(candidate)
            options = [correct, *distractors]
            rng.shuffle(options)
            bank.append((question, [str(x) for x in options], options.index(correct)+1))
    return bank


QUIZZES = _build_quiz_bank()

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
            "UPDATE game_accounts SET coins=GREATEST(0,coins+?), xp=xp+?, wins=wins+?, losses=losses+? "
            "WHERE chat_id=? AND user_id=?",
            (int(coins), int(xp), int(wins), int(losses), chat_id, user.id),
        )
        return conn.execute(
            "SELECT * FROM game_accounts WHERE chat_id=? AND user_id=?",
            (chat_id, user.id),
        ).fetchone()


def _hud_text(value):
    """Give every command-game response a consistent text-based 3D arena HUD."""
    body = str(value)
    if body.startswith("╔══════════════════════╗"):
        return body
    return (
        "╔══════════════════════════╗\n"
        "   ⚔️ NEXUS 3D ARENA • SYSTEM\n"
        "╠══════════════════════════╣\n"
        + body
        + "\n╚══════════════════════════╝"
    )


def _level(xp):
    return 1 + int(max(0, xp) // 100)


def _group(update):
    chat = update.effective_chat
    return bool(chat and chat.type in ("group", "supergroup"))


async def _ready(update):
    msg = update.effective_message
    if not _group(update):
        await msg.reply_text(_hud_text("ဒီဂိမ်းကို Group ထဲမှာပဲ ကစားနိုင်ပါတယ်။"))
        return False
    if not games_enabled(update.effective_chat.id):
        await msg.reply_text(_hud_text("🎮 ဒီ Group မှာ ဂိမ်းစနစ် ပိတ်ထားပါတယ်။ Admin ကို /game on လုပ်ခိုင်းပါ။"))
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
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    bot_username = context.bot.username
    chat_id = update.effective_chat.id
    launch_url = f"https://t.me/{bot_username}?start=game_{chat_id}"
    await update.effective_message.reply_text(_hud_text(
        "🎮 NEXUS 3D GAME HUB\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "3D World ထဲမှာ Character ကို Joystick နဲ့ လှုပ်ရှားပြီး ကစားနိုင်ပါပြီ။\n\n"
        "🎣 Fishing · ⚔️ Monster Hunt\n"
        "🧠 Quiz Battle · 🎰 Lucky Spin\n📚 Quiz Bank: 20,000 Questions\n"
        "🥊 1v1 Duel — Group Member နဲ့ စိန်ခေါ်ကစားရန်\n\n"
        "🪙 Group Coin / Level / XP ကို လက်ရှိ Bot Database နဲ့ ချိတ်ဆက်ထားပါတယ်။\n"
        "အောက်ကခလုတ်ကိုနှိပ်ပြီး Bot Private Chat ထဲမှာ Game ကိုဖွင့်ပါ။"),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Open 3D Game", url=launch_url)]])
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    row = _account(update.effective_chat.id, update.effective_user)
    await update.effective_message.reply_text(_hud_text(
        f"👤 {row['display_name']}\n🪙 Coin: {row['coins']:,}\n"
        f"⭐ Level: {_level(row['xp'])} | XP: {row['xp']:,}\n"
        f"🏆 အနိုင်: {row['wins']} | ရှုံး: {row['losses']}"
    ))


async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    chat_id, user = update.effective_chat.id, update.effective_user
    row = _account(chat_id, user)
    now = time.time()
    if now - float(row["last_daily"]) < 86400:
        remain = int(86400 - (now - float(row["last_daily"])))
        hours, minutes = divmod(remain // 60, 60)
        await update.effective_message.reply_text(_hud_text(f"⏳ Daily Reward ပြန်ယူရန် {hours} နာရီ {minutes} မိနစ် စောင့်ပါ။"))
        return
    with db() as conn:
        conn.execute(
            "UPDATE game_accounts SET coins=coins+?, xp=xp+10, last_daily=? WHERE chat_id=? AND user_id=?",
            (DAILY_COINS, now, chat_id, user.id),
        )
    await update.effective_message.reply_text(_hud_text(f"🎁 Daily Reward ရပါပြီ။ +{DAILY_COINS} Coin၊ +10 XP"))


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
        await update.effective_message.reply_text(_hud_text("🏆 Leaderboard မရှိသေးပါ။"))
        return
    lines = ["🏆 COIN LEADERBOARD — TOP 10"]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {row['display_name']} — 🪙 {row['coins']:,} | Lv.{_level(row['xp'])}")
    await update.effective_message.reply_text(_hud_text("\n".join(lines)))


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
    lines = ["╔══════════════════════╗", "   🧠 3D ARENA • QUIZ", "╚══════════════════════╝", "🎮 PLAYER: " + _display(update.effective_user), "━━━━━━━━━━━━━━━━━━━━", "❓ " + question]
    lines.extend(f"{i}. {option}" for i, option in enumerate(options, 1))
    lines.append("\n/answer 1-4 ဖြေပါ။ ပထမဆုံးမှန်သူ +25 Coin / +10 XP ရမယ်။ (90 စက္ကန့်)")
    await update.effective_message.reply_text(_hud_text("\n".join(lines)))


async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    if not context.args or not context.args[0].isdigit() or not 1 <= int(context.args[0]) <= 4:
        await update.effective_message.reply_text(_hud_text("အသုံးပြုပုံ: /answer 1 (1 မှ 4 အတွင်း)"))
        return
    chat_id, user = update.effective_chat.id, update.effective_user
    with db() as conn:
        row = conn.execute("SELECT * FROM game_quizzes WHERE chat_id=?", (chat_id,)).fetchone()
        if not row or float(row["expires_at"]) < time.time():
            conn.execute("DELETE FROM game_quizzes WHERE chat_id=?", (chat_id,))
            await update.effective_message.reply_text(_hud_text("⏰ Quiz မရှိတော့ပါ။ /quiz နဲ့ အသစ်စပါ။"))
            return
        if int(row["answer"]) != int(context.args[0]):
            await update.effective_message.reply_text(_hud_text("❌ မမှန်သေးပါဘူး။ ထပ်စမ်းကြည့်ပါ။"))
            return
        conn.execute("DELETE FROM game_quizzes WHERE chat_id=?", (chat_id,))
    account = _change(chat_id, user, coins=QUIZ_REWARD, xp=10, wins=1)
    await update.effective_message.reply_text(_hud_text(
        f"🎉 {user.full_name} မှန်ပါတယ်။ +{QUIZ_REWARD} Coin / +10 XP\n"
        f"🪙 လက်ကျန်: {account['coins']:,} Coin"
    ))


async def fish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    chat_id, user = update.effective_chat.id, update.effective_user
    wait = _cooldown(chat_id, user.id, "fish", 60)
    if wait:
        await update.effective_message.reply_text(_hud_text(f"🎣 နောက်တစ်ကြိမ် ငါးဖမ်းရန် {wait} စက္ကန့် စောင့်ပါ။"))
        return
    chosen = random.choices(FISH, weights=[x[3] for x in FISH], k=1)[0]
    if chosen:
        name, low, high, _ = chosen
        reward = random.randint(low, high)
        row = _change(chat_id, user, coins=reward, xp=5)
        await update.effective_message.reply_text(_hud_text(
            f"🎣 {name} ဖမ်းမိပြီ။ +{reward} Coin / +5 XP\n🪙 လက်ကျန် {row['coins']:,}"
        ))
    else:
        await update.effective_message.reply_text(_hud_text("🌊 ဒီတစ်ခေါက် ငါးမမိပါဘူး။ နောက်တစ်ကြိမ် ကြိုးစားပါ။"))


async def hunt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    chat_id, user = update.effective_chat.id, update.effective_user
    wait = _cooldown(chat_id, user.id, "hunt", 120)
    if wait:
        await update.effective_message.reply_text(_hud_text(f"⚔️ နောက်တစ်ကြိမ် Hunt လုပ်ရန် {wait} စက္ကန့် စောင့်ပါ။"))
        return
    monster = random.choice(["Goblin", "Wolf", "Shadow Beast", "Dragonling"])
    if random.randint(1, 100) <= 65:
        reward = random.randint(20, 55)
        row = _change(chat_id, user, coins=reward, xp=20, wins=1)
        await update.effective_message.reply_text(_hud_text(
            f"⚔️ {monster} ကို အနိုင်ယူလိုက်ပြီ။ +{reward} Coin / +20 XP\n"
            f"🪙 လက်ကျန်: {row['coins']:,} | Level: {_level(row['xp'])}"
        ))
    else:
        _change(chat_id, user, xp=5, losses=1)
        await update.effective_message.reply_text(_hud_text(f"💥 {monster} က အနိုင်ရသွားတယ်။ +5 XP ရပါတယ်။ နောက်တစ်ကြိမ် ကြိုးစားပါ။"))


async def spin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    chat_id, user = update.effective_chat.id, update.effective_user
    # No daily cooldown: members can spin again whenever they want.
    # Keep the existing reward odds and reveal the result with animated slot reels.
    reward = random.choices([0, 10, 25, 50, 100, 200], weights=[20, 25, 25, 18, 10, 2], k=1)[0]
    prize_icons = {10: "🍒", 25: "🍋", 50: "🍉", 100: "🍇", 200: "💎"}
    symbols = ["🍒", "🍋", "🍉", "🍇", "🍊", "⭐", "💎"]
    message = await update.effective_message.reply_text(_hud_text(
        "🎰 NEXUS SLOT MACHINE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "╭─────── 🎰 ───────╮\n"
        "│   ❔  │  ❔  │  ❔   │\n"
        "╰─────────────────╯\n"
        "⚡ REELS STARTING..."
    ))

    # Telegram displays each edit as the fruit reels roll in the group chat.
    for frame in range(6):
        reels = [random.choice(symbols) for _ in range(3)]
        speed = "▰" * (6 - frame) + "▱" * frame
        await message.edit_text(_hud_text(
            "🎰 NEXUS SLOT MACHINE\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"╭─────── 🎰 ───────╮\n"
            f"│   {reels[0]}  │  {reels[1]}  │  {reels[2]}   │\n"
            "╰─────────────────╯\n"
            f"⚡ SPINNING  {speed}"
        ))
        await asyncio.sleep(1.1)

    if reward:
        icon = prize_icons[reward]
        final_reels = [icon, icon, icon]
    else:
        final_reels = random.sample(symbols[:-1], 3)
        if len(set(final_reels)) == 1:
            final_reels[2] = "⭐"

    if reward:
        row = _change(chat_id, user, coins=reward, xp=3)
        result = f"🎉 JACKPOT! +{reward} COIN\n🪙 လက်ကျန်: {row['coins']:,}\n⭐ +3 XP"
    else:
        result = "😅 ဒီတစ်ကြိမ် ဆုမပေါက်ပါဘူး။ နောက်တစ်ကြိမ် ကံစမ်းပါ။"

    await message.edit_text(_hud_text(
        "🎰 NEXUS SLOT MACHINE • RESULT\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"╭─────── 🎰 ───────╮\n"
        f"│   {final_reels[0]}  │  {final_reels[1]}  │  {final_reels[2]}   │\n"
        "╰─────────────────╯\n"
        f"{result}"
    ))


async def duel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    msg, user = update.effective_message, update.effective_user
    target = msg.reply_to_message.from_user if msg.reply_to_message else None
    if not target or target.is_bot or target.id == user.id:
        await msg.reply_text(_hud_text("🥊 Duel စိန်ခေါ်ရန် ပြိုင်ဘက်ရဲ့ Message ကို Reply လုပ်ပြီး /duel ပို့ပါ။"))
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
    await msg.reply_text(_hud_text(
        f"🥊 {_display(user)} က {_display(target)} ကို Duel စိန်ခေါ်လိုက်ပြီ။\n"
        f"လက်ခံရန် /duelaccept | ငြင်းရန် /dueldecline\n"
        "အနိုင်ရသူ +40 Coin / +20 XP ရမယ်။"
    ))


async def duel_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    chat_id, user = update.effective_chat.id, update.effective_user
    with db() as conn:
        row = conn.execute("SELECT * FROM game_duels WHERE chat_id=?", (chat_id,)).fetchone()
        if not row or float(row["created_at"]) < time.time() - 300:
            conn.execute("DELETE FROM game_duels WHERE chat_id=?", (chat_id,))
            await update.effective_message.reply_text(_hud_text("🥊 လက်ခံနိုင်တဲ့ Duel မရှိပါ။ /duel နဲ့ စိန်ခေါ်ပါ။"))
            return
        if int(row["opponent_id"]) != user.id:
            await update.effective_message.reply_text(_hud_text("ဒီ Duel ကို စိန်ခေါ်ခံထားရသူသာ လက်ခံနိုင်ပါတယ်။"))
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
    await update.effective_message.reply_text(_hud_text(
        f"🏆 Duel ပြီးဆုံးပါပြီ။\nအနိုင်ရသူ: {winner.full_name}\n"
        f"🎁 ဆု: +{DUEL_REWARD} Coin / +20 XP"
    ))


async def duel_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    chat_id, user = update.effective_chat.id, update.effective_user
    with db() as conn:
        row = conn.execute("SELECT * FROM game_duels WHERE chat_id=?", (chat_id,)).fetchone()
        if not row or int(row["opponent_id"]) != user.id:
            await update.effective_message.reply_text(_hud_text("သင့်ကို စိန်ခေါ်ထားတဲ့ Duel မရှိပါ။"))
            return
        conn.execute("DELETE FROM game_duels WHERE chat_id=?", (chat_id,))
    await update.effective_message.reply_text(_hud_text("🚫 Duel ကို ငြင်းဆိုလိုက်ပါပြီ။"))


# Only the Bot Owner (OWNER_ID), never group admins, may adjust game coins.
import os

BOT_OWNER_ID = int(os.getenv("OWNER_ID", "7974865879") or 7974865879)


async def _owner_coin_change(update: Update, context: ContextTypes.DEFAULT_TYPE, direction: int):
    msg = update.effective_message
    user = update.effective_user
    if not user or user.id != BOT_OWNER_ID:
        await msg.reply_text(_hud_text("⛔ ဒီလုပ်ဆောင်ချက်ကို Bot Owner သာ အသုံးပြုနိုင်ပါတယ်။"))
        return
    target = msg.reply_to_message.from_user if msg.reply_to_message else None
    if not target and context.args:
        raw = context.args[0].lstrip("@")
        if raw.isdigit():
            target_id = int(raw)
            with db() as conn:
                row = conn.execute(
                    "SELECT user_id, username, display_name FROM game_accounts WHERE chat_id=? AND user_id=?",
                    (update.effective_chat.id, target_id),
                ).fetchone()
            if row:
                from telegram import User
                target = User(id=int(row["user_id"]), first_name=row["display_name"], is_bot=False)
    amount_index = 1 if context.args and context.args[0].lstrip("@").isdigit() and not msg.reply_to_message else 0
    if not target and msg.reply_to_message:
        target = msg.reply_to_message.from_user
    if not target or target.is_bot or target.id == BOT_OWNER_ID:
        await msg.reply_text(_hud_text(
            "အသုံးပြုပုံ:\n"
            "• Member ရဲ့ Message ကို Reply လုပ်ပြီး /addcoins 500 သို့ /takecoins 500\n"
            "• သို့မဟုတ် /addcoins USER_ID 500"
        ))
        return
    if len(context.args) <= amount_index or not context.args[amount_index].isdigit():
        await msg.reply_text(_hud_text("Coin ပမာဏကို အပေါင်းကိန်းပြည့်နဲ့ ထည့်ပါ။ ဥပမာ: /addcoins 500"))
        return
    amount = int(context.args[amount_index])
    if amount < 1 or amount > 1_000_000_000:
        await msg.reply_text(_hud_text("Coin ပမာဏသည် 1 မှ 1,000,000,000 အတွင်း ဖြစ်ရပါမယ်။"))
        return
    chat_id = update.effective_chat.id
    account = _account(chat_id, target)
    if direction < 0 and int(account["coins"]) < amount:
        await msg.reply_text(_hud_text(f"❌ လက်ကျန် Coin မလုံလောက်ပါ။ လက်ကျန်: {account['coins']:,}"))
        return
    result = _change(chat_id, target, coins=direction * amount)
    verb = "ထည့်ပေးလိုက်ပါပြီ" if direction > 0 else "နှုတ်ယူလိုက်ပါပြီ"
    await msg.reply_text(_hud_text(
        f"✅ {result['display_name']} ကို {amount:,} Coin {verb}။\n"
        f"🪙 လက်ကျန်: {result['coins']:,} Coin"
    ))


async def addcoins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _owner_coin_change(update, context, 1)


async def takecoins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _owner_coin_change(update, context, -1)
