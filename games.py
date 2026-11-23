"""Group command games and persistent virtual-coin economy."""
import asyncio
import json
import random
from rpg import record_game_event
import time

from telegram import Update
from telegram.ext import ContextTypes

from database import connect as db

STARTING_COINS = 100
DAILY_COINS = 50
QUIZ_REWARD = 25
DUEL_REWARD = 40

# Chance-game tuning: approximate theoretical coin return (RTP) is 94–98%.
# PvP Duel remains an even 50/50 contest with no house edge.

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
        # One wallet per Telegram user, shared across every group.
        conn.execute("""CREATE TABLE IF NOT EXISTS game_wallets (
            user_id BIGINT PRIMARY KEY,
            coins BIGINT NOT NULL DEFAULT 100,
            last_daily DOUBLE PRECISION NOT NULL DEFAULT 0
        )""")
        conn.execute("ALTER TABLE game_wallets ADD COLUMN IF NOT EXISTS last_daily DOUBLE PRECISION NOT NULL DEFAULT 0")
        # Merge old group balances without multiplying duplicate starter balances.
        conn.execute("""
            INSERT INTO game_wallets(user_id, coins, last_daily)
            SELECT user_id, MAX(coins), MAX(last_daily) FROM game_accounts GROUP BY user_id
            ON CONFLICT(user_id) DO NOTHING
        """)
        # If an earlier rollout created wallets before last_daily was added, backfill it.
        conn.execute("""
            UPDATE game_wallets w
               SET last_daily=legacy.last_daily
              FROM (
                  SELECT user_id, MAX(last_daily) AS last_daily
                    FROM game_accounts GROUP BY user_id
              ) legacy
             WHERE legacy.user_id=w.user_id AND w.last_daily=0
        """)
        # Keep legacy game_accounts coin columns synchronized for older game logic.
        conn.execute("""CREATE OR REPLACE FUNCTION sync_game_wallet_from_account()
            RETURNS trigger AS $wallet_sync$
            BEGIN
                INSERT INTO game_wallets(user_id, coins)
                VALUES (NEW.user_id, NEW.coins)
                ON CONFLICT(user_id) DO UPDATE SET coins=EXCLUDED.coins;
                UPDATE game_accounts
                   SET coins=NEW.coins
                 WHERE user_id=NEW.user_id
                   AND (chat_id<>NEW.chat_id OR coins<>NEW.coins);
                RETURN NEW;
            END;
            $wallet_sync$ LANGUAGE plpgsql
        """)
        conn.execute("DROP TRIGGER IF EXISTS game_accounts_wallet_sync ON game_accounts")
        conn.execute("""CREATE TRIGGER game_accounts_wallet_sync
            AFTER UPDATE OF coins ON game_accounts
            FOR EACH ROW WHEN (OLD.coins IS DISTINCT FROM NEW.coins)
            EXECUTE FUNCTION sync_game_wallet_from_account()
        """)
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
        conn.execute("""CREATE TABLE IF NOT EXISTS game_quiz_players (
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            bet BIGINT NOT NULL,
            joined_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY(chat_id, user_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS game_mines (
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            bet BIGINT NOT NULL,
            mines TEXT NOT NULL,
            opened TEXT NOT NULL DEFAULT '[]',
            created_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY(chat_id, user_id)
        )""")
        try:
            conn.execute("ALTER TABLE game_duels ADD COLUMN IF NOT EXISTS wager BIGINT NOT NULL DEFAULT 0")
        except Exception:
            pass


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
        # Existing users keep their highest old group balance; new users start once.
        conn.execute(
            "INSERT INTO game_wallets(user_id,coins) "
            "SELECT user_id,MAX(coins) FROM game_accounts WHERE user_id=? GROUP BY user_id "
            "ON CONFLICT(user_id) DO NOTHING",
            (user.id,),
        )
        wallet = conn.execute("SELECT coins FROM game_wallets WHERE user_id=?", (user.id,)).fetchone()
        conn.execute(
            "UPDATE game_accounts SET coins=? WHERE user_id=? AND coins<>?",
            (int(wallet["coins"]), user.id, int(wallet["coins"])),
        )
        conn.execute(
            "UPDATE game_accounts SET username=?,display_name=? WHERE chat_id=? AND user_id=?",
            (user.username or "", _display(user), chat_id, user.id),
        )
        return conn.execute(
            "SELECT ga.chat_id,ga.user_id,ga.username,ga.display_name,gw.coins AS coins,"
            "ga.xp,ga.wins,ga.losses,gw.last_daily AS last_daily FROM game_accounts ga "
            "JOIN game_wallets gw ON gw.user_id=ga.user_id "
            "WHERE ga.chat_id=? AND ga.user_id=?",
            (chat_id, user.id),
        ).fetchone()


def _wallet_adjust(conn, user_id, amount):
    """Atomically adjust one user's global wallet and mirror its balance to legacy rows."""
    row = conn.execute(
        "UPDATE game_wallets SET coins=GREATEST(0,coins+?) WHERE user_id=? RETURNING coins",
        (int(amount), int(user_id)),
    ).fetchone()
    if not row:
        return None
    balance = int(row["coins"])
    conn.execute(
        "UPDATE game_accounts SET coins=? WHERE user_id=? AND coins<>?",
        (balance, int(user_id), balance),
    )
    return balance


def _change(chat_id, user, coins=0, xp=0, wins=0, losses=0):
    _account(chat_id, user)
    with db() as conn:
        _wallet_adjust(conn, user.id, int(coins))
        conn.execute(
            "UPDATE game_accounts SET xp=xp+?, wins=wins+?, losses=losses+? "
            "WHERE chat_id=? AND user_id=?",
            (int(xp), int(wins), int(losses), chat_id, user.id),
        )
        return conn.execute(
            "SELECT * FROM game_accounts WHERE chat_id=? AND user_id=?",
            (chat_id, user.id),
        ).fetchone()


def _hud_text(value):
    """Render command replies as compact faux-3D game panels for Telegram chat."""
    body = str(value).strip()
    if body.startswith("╭━━━ 💠 MYAE 3D"):
        return body

    # Telegram messages cannot render real-time 3D models. Layered borders,
    # depth shadows, status rails and game glyphs give commands a 3D-like HUD.
    lines = body.splitlines() or ["⚔️ ARENA READY"]
    title = lines[0][:34]
    details = lines[1:]
    top = "╭━━━ 💠 MYAE 3D ARENA ━━━╮"
    header = f"┃  ✦ {title}"
    divider = "┣━━━━━━━━━━━━━━━━━━━━━━┫"
    footer = "╰━━━━━━━━━━━━━━━━━━━━━━╯"
    depth = "   ▰ ▰ ▰  ✧  NEXUS SYSTEM"
    content = "\n".join(f"┃  {line}" if line else "┃" for line in details)
    return "\n".join([top, header, divider, content, footer, depth])


async def _animate_hud(message, frames, delay=0.12):
    """Use one brief animation frame to keep busy group games responsive."""
    if frames:
        await message.edit_text(_hud_text(frames[0]))
        if delay > 0:
            await asyncio.sleep(min(delay, 0.12))

def _level(xp):
    return 1 + int(max(0, xp) // 100)



def _rank(xp):
    level = _level(xp)
    tiers = [
        (1, "🥉 Bronze"), (5, "🥈 Silver"), (10, "🥇 Gold"),
        (20, "💠 Platinum"), (30, "💎 Diamond"),
        (50, "👑 Master"), (75, "🔥 Grandmaster"), (100, "🌌 Mythic"),
    ]
    current = tiers[0]
    following = None
    for i, tier in enumerate(tiers):
        if level >= tier[0]:
            current = tier
            following = tiers[i + 1] if i + 1 < len(tiers) else None
    return current[1], level, following


def _valid_bet(context, minimum=10, maximum=1_000_000_000):
    if not context.args or not context.args[0].isdigit():
        return None
    amount = int(context.args[0])
    return amount if minimum <= amount <= maximum else None


def _stake(chat_id, user, amount):
    _account(chat_id, user)
    with db() as conn:
        result = conn.execute(
            "UPDATE game_wallets SET coins=coins-? WHERE user_id=? AND coins>=?",
            (amount, user.id, amount),
        )
        if result.rowcount != 1:
            return None
        conn.execute(
            "UPDATE game_accounts SET coins=(SELECT coins FROM game_wallets WHERE user_id=?) "
            "WHERE user_id=? AND coins<>(SELECT coins FROM game_wallets WHERE user_id=?)",
            (user.id, user.id, user.id),
        )
        return conn.execute(
            "SELECT ga.*,gw.coins AS coins FROM game_accounts ga "
            "JOIN game_wallets gw ON gw.user_id=ga.user_id WHERE ga.chat_id=? AND ga.user_id=?",
            (chat_id, user.id),
        ).fetchone()


def _expire_quiz(chat_id):
    with db() as conn:
        players = conn.execute(
            "SELECT user_id FROM game_quiz_players WHERE chat_id=?", (chat_id,)
        ).fetchall()
        for player in players:
            conn.execute(
                "UPDATE game_accounts SET losses=losses+1,xp=xp+1 WHERE chat_id=? AND user_id=?",
                (chat_id, int(player["user_id"])),
            )
        conn.execute("DELETE FROM game_quiz_players WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM game_quizzes WHERE chat_id=?", (chat_id,))

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
        "💗 မြအေး GAME HUB • ELITE ARENA\n"
        "╔════════════════════════════╗\n"
        "   🪙 COIN • ⚡ XP • 👑 RANK\n"
        "╚════════════════════════════╝\n"
        "🎯 ကစားပုံ: Bet ထည့် → ကစား → ဆုကြေးယူ → Level တက်\n"
        "⚡ Fish / Hunt / Spin / Mines မှာ Cooldown မရှိပါ။\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💣 /mines 100 — Button နဲ့ 5×5 Minefield\n"
        "🎰 /spin 100 — Lucky Slot Machine\n"
        "🎣 /fish 100 — Fishing Treasure\n"
        "⚔️ /hunt 100 — Monster Battle\n"
        "🧠 /quiz 100 — Quiz Arena | /answer 1-4\n"
        "🥊 Member ကို Reply + /duel 100 — PvP Duel\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "👛 /balance — Coin • XP • Level • Rank\n"
        "🏅 /rank — Player Profile\n"
        "🥇 /topcoins — Coin Champions Top 10\n"
        "👑 /toprank — XP / Rank Hall of Fame\n"
        "🎁 /daily — Daily Coin + XP Reward\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🧬 /rpg — RPG Player Profile\n"
        "🎯 /missions — Daily Missions | /missionclaim play3/win2/xp30\n"
        "🎖️ /achievements — Achievement Collection\n"
        "🛒 /shop — Cosmetic Shop | /buy ITEM_ID\n"
        "🎒 /inventory — Items | /equip ITEM_ID\n"
        "🔥 /rpgdaily — Daily Streak Reward (Coin + RPG XP)\n"
        "⚔️ /adventure — Solo Dungeon Monster Battle (30 min cooldown)\n"
        "🏆 /rpgtop — RPG XP Champions Top 10\n"        "🛡️ /guildcreate NAME — Guild ဖွဲ့ | /guildjoin ID — Guild ဝင်\n"
        "🏰 /guild — ကိုယ့် Guild | /guildtop — Guild Ranking | /guildleave\n"
        "⚔️ /gear — Equipment Shop | /gearbuy ID | /gearequip ID\n"
        "🐉 /boss — World Boss Status | /bossattack — ဝိုင်းတိုက်ခိုက်ရန်\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📚 Quiz Bank: 20,000 Questions\n"
        "🧬 100 XP = Level တစ်ဆင့် | 🏆 အနိုင်ရတိုင်း XP ရမယ်။"
    ), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Open 3D Web Game", url=launch_url)]]))

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    row = _account(update.effective_chat.id, update.effective_user)
    title, level, following = _rank(row["xp"])
    await update.effective_message.reply_text(_hud_text(
        f"👤 {row['display_name']}\n🪙 Coin: {row['coins']:,}\n"
        f"🏅 Rank: {title}\n⭐ Level: {level} | XP: {row['xp']:,}\n"
        f"🏆 အနိုင်: {row['wins']} | ရှုံး: {row['losses']}"
    ))

async def rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    target = update.effective_message.reply_to_message.from_user if update.effective_message.reply_to_message else update.effective_user
    row = _account(update.effective_chat.id, target)
    title, level, following = _rank(row["xp"])
    progress = int(row["xp"]) % 100
    next_line = f"➡️ နောက် Rank: {following[1]} (Lv.{following[0]})" if following else "🌌 အမြင့်ဆုံး Rank ရောက်ရှိပါပြီ!"
    await update.effective_message.reply_text(_hud_text(
        f"🏅 PLAYER RANK PROFILE\n━━━━━━━━━━━━━━━━━━\n👤 {row['display_name']}\n"
        f"🏆 Rank: {title}\n⭐ Level: {level}\n✨ XP: {row['xp']:,} ({progress}/100)\n"
        f"{next_line}\n🪙 Coin: {row['coins']:,}\n⚔️ Wins: {row['wins']} | Losses: {row['losses']}"
    ))


async def toprank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    with db() as conn:
        rows = conn.execute(
            "SELECT MAX(display_name) AS display_name,SUM(xp) AS xp,SUM(wins) AS wins "
            "FROM game_accounts GROUP BY user_id "
            "ORDER BY SUM(xp) DESC,SUM(wins) DESC LIMIT 10"
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text(_hud_text("🏆 Rank စာရင်း မရှိသေးပါ။"))
        return
    lines = ["👑✨ NEXUS RANKING HALL OF FAME ✨👑", "━━━━━━━━━━━━━━━━━━"]
    medals = ["🥇", "🥈", "🥉", "🏅", "🎖️", "💠", "💎", "🔥", "⚡", "🌟"]
    for i, row in enumerate(rows, 1):
        title, level, _ = _rank(row["xp"])
        lines.append(f"{medals[i-1]} {row['display_name']}\n   {title} • Lv.{level} • {row['xp']:,} XP")
    await update.effective_message.reply_text(_hud_text("\n".join(lines)))

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
        _wallet_adjust(conn, user.id, DAILY_COINS)
        conn.execute("UPDATE game_wallets SET last_daily=? WHERE user_id=?", (now, user.id))
        conn.execute(
            "UPDATE game_accounts SET last_daily=? WHERE user_id=?",
            (now, user.id),
        )
        conn.execute(
            "UPDATE game_accounts SET xp=xp+10 WHERE chat_id=? AND user_id=?",
            (chat_id, user.id),
        )
    await update.effective_message.reply_text(_hud_text(f"🎁 Daily Reward ရပါပြီ။ +{DAILY_COINS} Coin၊ +10 XP"))


async def topcoins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    with db() as conn:
        rows = conn.execute(
            "SELECT names.display_name, w.coins, COALESCE(stats.xp,0) AS xp "
            "FROM game_wallets w "
            "LEFT JOIN (SELECT user_id, MAX(display_name) AS display_name FROM game_accounts GROUP BY user_id) names "
            "ON names.user_id=w.user_id "
            "LEFT JOIN (SELECT user_id, SUM(xp) AS xp FROM game_accounts GROUP BY user_id) stats "
            "ON stats.user_id=w.user_id "
            "ORDER BY w.coins DESC, COALESCE(stats.xp,0) DESC LIMIT 10"
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text(_hud_text("🏆 Leaderboard မရှိသေးပါ။"))
        return
    lines = ["💰👑 NEXUS RICHEST PLAYERS 👑💰", "━━━━━━━━━━━━━━━━━━"]
    medals = ["🥇", "🥈", "🥉", "🏅", "🎖️", "💠", "💎", "🔥", "⚡", "🌟"]
    for i, row in enumerate(rows, 1):
        lines.append(f"{medals[i-1]} {row['display_name']}\n   🪙 {row['coins']:,} • ⭐ Lv.{_level(row['xp'])}")
    await update.effective_message.reply_text(_hud_text("\n".join(lines)))


async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update): return
    chat_id,user=update.effective_chat.id,update.effective_user
    bet=_valid_bet(context)
    if bet is None:
        await update.effective_message.reply_text(_hud_text("🧠 /quiz 100 ဖြင့် Coin လောင်းပြီး Quiz ဝင်ပါ။ အနည်းဆုံး 10 Coin။")); return
    with db() as conn: active=conn.execute("SELECT * FROM game_quizzes WHERE chat_id=?",(chat_id,)).fetchone()
    if active and float(active["expires_at"])<time.time(): _expire_quiz(chat_id); active=None
    if active:
        with db() as conn: joined=conn.execute("SELECT bet FROM game_quiz_players WHERE chat_id=? AND user_id=?",(chat_id,user.id)).fetchone()
        if joined:
            await update.effective_message.reply_text(_hud_text(f"Quiz ထဲဝင်ပြီးသား။ Bet {int(joined['bet']):,} Coin။ /answer 1-4")); return
    else:
        q,opts,ans=random.choice(QUIZZES)
        with db() as conn:
            conn.execute("INSERT INTO game_quizzes(chat_id,question,options,answer,expires_at) VALUES(?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET question=excluded.question,options=excluded.options,answer=excluded.answer,expires_at=excluded.expires_at",(chat_id,q,json.dumps(opts,ensure_ascii=False),ans,time.time()+90))
        active={"question":q,"options":json.dumps(opts,ensure_ascii=False)}
    if _stake(chat_id,user,bet) is None:
        with db() as conn: bal=conn.execute("SELECT coins FROM game_wallets WHERE user_id=?",(user.id,)).fetchone()
        await update.effective_message.reply_text(_hud_text(f"❌ Coin မလုံလောက်ပါ။ လက်ကျန် {int(bal['coins']):,}")); return
    with db() as conn: conn.execute("INSERT INTO game_quiz_players(chat_id,user_id,bet,joined_at) VALUES(?,?,?,?)",(chat_id,user.id,bet,time.time()))
    msg=await update.effective_message.reply_text(_hud_text("🧠 QUIZ ARENA\n🔍 မေးခွန်း Load လုပ်နေသည်..."))
    await _animate_hud(msg,["🧠 QUIZ ARENA\n🎯 Bet အတည်ပြုနေသည်...\n▰▱▱","🧠 QUIZ ARENA\n🚀 CHALLENGE START!\n▰▰▰"],0.35)
    lines=["🧠 NEXUS QUIZ ARENA","━━━━━━━━━━━━━━━━━━","❓ "+active["question"]]
    lines.extend(f"{i}. {x}" for i,x in enumerate(json.loads(active["options"]),1))
    lines.append("\nဝင်ကစားသူများ /quiz BET ဖြင့်ဝင်ပါ။\n/answer 1-4 ဖြေပါ။")
    await msg.edit_text(_hud_text("\n".join(lines)))

async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update): return
    if not context.args or not context.args[0].isdigit() or not 1<=int(context.args[0])<=4:
        await update.effective_message.reply_text(_hud_text("အသုံးပြုပုံ: /answer 1")); return
    chat_id,user=update.effective_chat.id,update.effective_user
    with db() as conn:
        q=conn.execute("SELECT * FROM game_quizzes WHERE chat_id=?",(chat_id,)).fetchone()
        player=conn.execute("SELECT bet FROM game_quiz_players WHERE chat_id=? AND user_id=?",(chat_id,user.id)).fetchone()
    if not q or float(q["expires_at"])<time.time():
        if q: _expire_quiz(chat_id)
        await update.effective_message.reply_text(_hud_text("⏰ Quiz ပြီးဆုံးသွားပါပြီ။ /quiz BET ဖြင့်ပြန်စပါ။")); return
    if not player:
        await update.effective_message.reply_text(_hud_text("ဒီ Quiz မှာမဝင်ရသေးပါ။ /quiz 100 ဖြင့်ဝင်ပါ။")); return
    if int(q["answer"])!=int(context.args[0]):
        m=await update.effective_message.reply_text(_hud_text("🧠 QUIZ CHECKER\nအဖြေစစ်ဆေးနေသည်..."))
        await _animate_hud(m,["❌ WRONG ANSWER\nထပ်ကြိုးစားပါ!"],0.3); return
    with db() as conn:
        players=conn.execute("SELECT user_id FROM game_quiz_players WHERE chat_id=?",(chat_id,)).fetchall()
        conn.execute("DELETE FROM game_quiz_players WHERE chat_id=?",(chat_id,))
        conn.execute("DELETE FROM game_quizzes WHERE chat_id=?",(chat_id,))
        for p in players:
            if int(p["user_id"])!=user.id: conn.execute("UPDATE game_accounts SET losses=losses+1 WHERE chat_id=? AND user_id=?",(chat_id,int(p["user_id"])))
        payout=int(round(int(player["bet"])*3.6))
        _wallet_adjust(conn, user.id, payout)
        conn.execute("UPDATE game_accounts SET xp=xp+25,wins=wins+1 WHERE chat_id=? AND user_id=?",(chat_id,user.id))
        row=conn.execute("SELECT * FROM game_accounts WHERE chat_id=? AND user_id=?",(chat_id,user.id)).fetchone()
    for player_row in players:
        if int(player_row["user_id"]) != user.id:
            record_game_event(int(player_row["user_id"]), False, 0)
    record_game_event(user.id, True, 25)
    m=await update.effective_message.reply_text(_hud_text("🧠 QUIZ RESULT\n🏆 Winner detected..."))
    await _animate_hud(m,["🔓 REWARD UNLOCKED!","🎉 CORRECT ANSWER!"],0.35)
    await m.edit_text(_hud_text(f"🏆 {user.full_name} အနိုင်ရ!\n🎁 {int(player['bet']):,} × 3.6 = +{payout:,} Coin\n⭐ +25 XP\n🪙 လက်ကျန် {row['coins']:,}"))

async def fish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update): return
    chat_id,user=update.effective_chat.id,update.effective_user
    bet=_valid_bet(context)
    if bet is None:
        await update.effective_message.reply_text(_hud_text("🎣 /fish 100 ဖြင့် လောင်းကြေးထည့်ပါ။ အနည်းဆုံး 10 Coin။")); return
    if _stake(chat_id,user,bet) is None:
        await update.effective_message.reply_text(_hud_text("❌ Coin မလုံလောက်ပါ။ /balance စစ်ပါ။")); return
    name,_,_,_=random.choices(FISH,weights=[x[3] for x in FISH],k=1)[0]
    mult={"ငါးသေး":0.5,"ငါးကြင်း":1.1,"ငါးကြီး":1.8,"ရွှေငါး":4.0}.get(name,0.5)
    payout=int(bet*mult); won=payout>bet
    m=await update.effective_message.reply_text(_hud_text("🎣 NEXUS FISHING ZONE\n🌊 ငါးမျှားချိတ်ချလိုက်ပြီ..."))
    await _animate_hud(m,["🌊 ရေပြင်လှုပ်ရှားနေသည်...","🐟 ငါးချိတ်မိပြီ! 💦 SPLASH!","⬆️ ငါးကိုဆွဲတင်နေသည်..."],0.5)
    with db() as conn:
        _wallet_adjust(conn, user.id, payout)
        conn.execute("UPDATE game_accounts SET xp=xp+?,wins=wins+?,losses=losses+? WHERE chat_id=? AND user_id=?",(5 if won else 3,1 if won else 0,0 if won else 1,chat_id,user.id))
        row=conn.execute("SELECT * FROM game_accounts WHERE chat_id=? AND user_id=?",(chat_id,user.id)).fetchone()
    record_game_event(user.id, won, 5 if won else 3)
    await m.edit_text(_hud_text(f"🎣 FISHING RESULT • {'🏆 WIN' if won else '💥 LOSS'}\n🐠 {name} | {mult:g}x\n🎯 Bet: {bet:,}\n💰 ပြန်ရငွေ: {payout:,} Coin\n⭐ XP +{5 if won else 3}\n🪙 လက်ကျန်: {row['coins']:,}"))

async def hunt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update): return
    chat_id,user=update.effective_chat.id,update.effective_user
    bet=_valid_bet(context)
    if bet is None:
        await update.effective_message.reply_text(_hud_text("⚔️ /hunt 100 ဖြင့် လောင်းကြေးထည့်ပါ။ အနည်းဆုံး 10 Coin။")); return
    if _stake(chat_id,user,bet) is None:
        await update.effective_message.reply_text(_hud_text("❌ Coin မလုံလောက်ပါ။ /balance စစ်ပါ။")); return
    monster=random.choice(["Goblin","Wolf","Shadow Beast","Dragonling"])
    won=random.randint(1,100)<=47; payout=bet*2 if won else 0
    m=await update.effective_message.reply_text(_hud_text(f"⚔️ MONSTER HUNT\n👹 {monster}\n⚡ BATTLE START!"))
    await _animate_hud(m,[f"👹 {monster} ပေါ်လာပြီ!\n🛡️ DEFEND!","🗡️ PLAYER ATTACK!\n💥 CLASH!","🔥 FINAL STRIKE!\n⚡ FINISH MOVE!"],0.55)
    with db() as conn:
        _wallet_adjust(conn, user.id, payout)
        conn.execute("UPDATE game_accounts SET xp=xp+?,wins=wins+?,losses=losses+? WHERE chat_id=? AND user_id=?",(20 if won else 5,1 if won else 0,0 if won else 1,chat_id,user.id))
        row=conn.execute("SELECT * FROM game_accounts WHERE chat_id=? AND user_id=?",(chat_id,user.id)).fetchone()
    record_game_event(user.id, won, 20 if won else 5)
    await m.edit_text(_hud_text(f"⚔️ HUNT RESULT • {'🏆 VICTORY' if won else '💥 DEFEAT'}\n👹 {monster}\n{'💰 Payout: '+format(payout,',')+' Coin (2x)' if won else '📉 Bet lost: '+format(bet,',')+' Coin'}\n⭐ XP +{20 if won else 5}\n🪙 Balance: {row['coins']:,}"))


MINES_MULTIPLIERS = [1.08, 1.23, 1.42, 1.64, 1.92, 2.25, 2.68, 3.21, 3.90, 4.80]


def _mine_keyboard(user_id, opened):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = []
    for r in range(5):
        rows.append([
            InlineKeyboardButton("💎" if n in opened else "❔",
                                 callback_data=f"mine:open:{n}:{user_id}")
            for n in range(r * 5 + 1, r * 5 + 6)
        ])
    if opened:
        rows.append([InlineKeyboardButton("💰 CASH OUT", callback_data=f"mine:cash:0:{user_id}")])
    return InlineKeyboardMarkup(rows)


def _mine_multiplier(count):
    return MINES_MULTIPLIERS[min(max(1, count), len(MINES_MULTIPLIERS)) - 1]


async def mines(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update): return
    chat_id, user = update.effective_chat.id, update.effective_user
    bet = _valid_bet(context)
    if bet is None:
        await update.effective_message.reply_text(_hud_text(
            "💣 NEXUS MINEFIELD • 5×5\nအသုံးပြုပုံ: /mines 100\n"
            "အကွက်တွေကို Button နှိပ်ပြီးဖွင့်ပါ။ 💎 Safe ဖြစ်ရင် CASH OUT လုပ်နိုင်ပါတယ်။"
        ))
        return
    with db() as conn:
        active = conn.execute("SELECT bet,opened FROM game_mines WHERE chat_id=? AND user_id=?", (chat_id, user.id)).fetchone()
    if active:
        opened = set(json.loads(active["opened"]))
        await update.effective_message.reply_text(
            _hud_text(f"💣 MINEFIELD • Bet {int(active['bet']):,} Coin\n💎 Safe: {len(opened)}\nButton နဲ့ ဆက်ရွေးပါ။"),
            reply_markup=_mine_keyboard(user.id, opened))
        return
    if _stake(chat_id, user, bet) is None:
        await update.effective_message.reply_text(_hud_text("❌ Coin မလုံလောက်ပါ။ /balance စစ်ပါ။"))
        return
    mine_cells = sorted(random.sample(range(1, 26), 3))
    with db() as conn:
        conn.execute("INSERT INTO game_mines(chat_id,user_id,username,bet,mines,opened,created_at) VALUES(?,?,?,?,?,'[]',?)",
                     (chat_id, user.id, _display(user), bet, json.dumps(mine_cells), time.time()))
    await update.effective_message.reply_text(
        _hud_text(f"💣 NEXUS MINEFIELD • 5×5\n👤 {user.full_name}\n🪙 Bet: {bet:,} Coin\n"
                  "🎯 အောက်က Button ၂၅ ခုထဲက Safe အကွက်ရွေးပါ။\n💥 Mine ထိရင် Bet ဆုံးမယ်။"),
        reply_markup=_mine_keyboard(user.id, set()))


async def mine_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        _, action, value, owner = query.data.split(":")
        owner_id, cell = int(owner), int(value)
        chat_id = query.message.chat_id
    except (ValueError, AttributeError):
        await query.answer()
        return
    if query.from_user.id != owner_id:
        await query.answer("ဒီ Mine Game ကစားသူအတွက်ပဲ ဖြစ်ပါတယ်။", show_alert=True)
        return
    with db() as conn:
        game = conn.execute("SELECT * FROM game_mines WHERE chat_id=? AND user_id=?", (chat_id, owner_id)).fetchone()
    if not game:
        await query.answer("ဒီ Mine Game ပြီးဆုံးသွားပါပြီ။", show_alert=True)
        await query.edit_message_text(_hud_text("💣 ဒီ Mine Game ပြီးဆုံးသွားပါပြီ။ /mines BET ဖြင့် အသစ်စပါ။"))
        return
    mines_set, opened = set(json.loads(game["mines"])), set(json.loads(game["opened"]))
    if action == "cash" and not opened:
        await query.answer("အနည်းဆုံး Safe အကွက် ၁ ခု ဖွင့်ပါ။", show_alert=True)
        return
    await query.answer()
    if action == "open":
        if not 1 <= cell <= 25:
            return
        if cell in opened:
            await query.edit_message_reply_markup(reply_markup=_mine_keyboard(owner_id, opened))
            return
        if cell in mines_set:
            with db() as conn:
                conn.execute("DELETE FROM game_mines WHERE chat_id=? AND user_id=?", (chat_id, owner_id))
                conn.execute("UPDATE game_accounts SET xp=xp+2,losses=losses+1 WHERE chat_id=? AND user_id=?", (chat_id, owner_id))
                row = conn.execute("SELECT coins FROM game_accounts WHERE chat_id=? AND user_id=?", (chat_id, owner_id)).fetchone()
            record_game_event(owner_id, False, 2)
            board = "\n".join(" ".join("💣" if n in mines_set else ("💎" if n in opened else "❔")
                                       for n in range(r*5+1, r*5+6)) for r in range(5))
            await query.edit_message_text(_hud_text(
                f"💥 BOOM! MINE HIT! • #{cell}\n{board}\n\n📉 Bet ဆုံးရှုံး: {int(game['bet']):,} Coin"
                f"\n⭐ +2 XP | 🪙 လက်ကျန်: {row['coins']:,}\nနောက်တစ်ပွဲ /mines BET"))
            return
        opened.add(cell)
        multiplier = _mine_multiplier(len(opened))
        with db() as conn:
            conn.execute("UPDATE game_mines SET opened=? WHERE chat_id=? AND user_id=?",
                         (json.dumps(sorted(opened)), chat_id, owner_id))
        payout = int(int(game["bet"]) * multiplier)
        board = "\n".join(" ".join("💎" if n in opened else "❔"
                                   for n in range(r*5+1, r*5+6)) for r in range(5))
        await query.edit_message_text(
            _hud_text(f"💎 SAFE PICK! • #{cell}\n{board}\n\n✅ Safe: {len(opened)}"
                      f"\n📈 Multiplier: {multiplier:g}x\n💰 Cashout: {payout:,} Coin"),
            reply_markup=_mine_keyboard(owner_id, opened))
        return
    if action == "cash":
        multiplier = _mine_multiplier(len(opened))
        payout = int(int(game["bet"]) * multiplier)
        with db() as conn:
            conn.execute("DELETE FROM game_mines WHERE chat_id=? AND user_id=?", (chat_id, owner_id))
            _wallet_adjust(conn, owner_id, payout)
            conn.execute("UPDATE game_accounts SET xp=xp+?,wins=wins+1 WHERE chat_id=? AND user_id=?",
                         (8 + len(opened)*2, chat_id, owner_id))
            row = conn.execute("SELECT coins FROM game_accounts WHERE chat_id=? AND user_id=?", (chat_id, owner_id)).fetchone()
        record_game_event(owner_id, True, 8 + len(opened)*2)
        await query.edit_message_text(_hud_text(
            f"💰✨ CASH OUT SUCCESS! ✨💰\n💎 Safe: {len(opened)} | {multiplier:g}x"
            f"\n🎁 Payout: {payout:,} Coin\n⭐ +{8 + len(opened)*2} XP"
            f"\n🪙 Balance: {row['coins']:,}\nနောက်တစ်ပွဲ /mines BET"))


async def pick_mine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    if not context.args or not context.args[0].isdigit() or not 1 <= int(context.args[0]) <= 25:
        await update.effective_message.reply_text(_hud_text("အသုံးပြုပုံ: /pick 1 မှ 25 အတွင်း အကွက်နံပါတ်"))
        return
    chat_id, user = update.effective_chat.id, update.effective_user
    cell = int(context.args[0])
    with db() as conn:
        game = conn.execute(
            "SELECT * FROM game_mines WHERE chat_id=? AND user_id=?",
            (chat_id, user.id),
        ).fetchone()
    if not game:
        await update.effective_message.reply_text(_hud_text("💣 Active Mine Game မရှိပါ။ /mines 100 ဖြင့် စတင်ပါ။"))
        return
    mines_set = set(json.loads(game["mines"]))
    opened = set(json.loads(game["opened"]))
    if cell in opened:
        await update.effective_message.reply_text(_hud_text("ဒီအကွက်ကို ဖွင့်ပြီးသားပါ။ အခြားအကွက်ရွေးပါ။"))
        return
    if cell in mines_set:
        with db() as conn:
            conn.execute("DELETE FROM game_mines WHERE chat_id=? AND user_id=?", (chat_id, user.id))
            conn.execute(
                "UPDATE game_accounts SET xp=xp+2,losses=losses+1 WHERE chat_id=? AND user_id=?",
                (chat_id, user.id),
            )
            wallet = conn.execute("SELECT coins FROM game_wallets WHERE user_id=?", (user.id,)).fetchone()
        record_game_event(user.id, False, 2)
        board = "\n".join(
            " ".join("💣" if n in mines_set else ("💎" if n in opened else "❔")
                    for n in range(r * 5 + 1, r * 5 + 6))
            for r in range(5)
        )
        balance_now = int(wallet["coins"]) if wallet else 0
        await update.effective_message.reply_text(_hud_text(
            f"💥 BOOM! MINE HIT! • #{cell}\n{board}\n"
            f"📉 Bet ဆုံးရှုံး: {int(game['bet']):,} Coin\n⭐ +2 XP"
            f"\n🪙 လက်ကျန်: {balance_now:,} Coin"
        ))
        return
    opened.add(cell)
    with db() as conn:
        conn.execute(
            "UPDATE game_mines SET opened=? WHERE chat_id=? AND user_id=?",
            (json.dumps(sorted(opened)), chat_id, user.id),
        )
        wallet = conn.execute("SELECT coins FROM game_wallets WHERE user_id=?", (user.id,)).fetchone()
    multiplier = _mine_multiplier(len(opened))
    payout = int(int(game["bet"]) * multiplier)
    board = "\n".join(
        " ".join("💎" if n in opened else "❔" for n in range(r * 5 + 1, r * 5 + 6))
        for r in range(5)
    )
    balance_now = int(wallet["coins"]) if wallet else 0
    await update.effective_message.reply_text(
        _hud_text(
            f"💎 SAFE PICK! • #{cell}\n{board}\n"
            f"✅ Safe: {len(opened)} | 📈 Multiplier: {multiplier:g}x"
            f"\n💰 Cashout: {payout:,} Coin | 🪙 လက်ကျန်: {balance_now:,}"
            f"\nနောက်အကွက် /pick 1-25 သို့မဟုတ် /cashout"
        ),
        reply_markup=_mine_keyboard(user.id, opened),
    )


async def cashout_mines(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update): return
    chat_id, user = update.effective_chat.id, update.effective_user
    with db() as conn:
        game = conn.execute("SELECT * FROM game_mines WHERE chat_id=? AND user_id=?", (chat_id, user.id)).fetchone()
    if not game:
        await update.effective_message.reply_text(_hud_text("💣 Active Mine game မရှိပါ။")); return
    opened = json.loads(game["opened"])
    if not opened:
        await update.effective_message.reply_text(_hud_text("အနည်းဆုံး Safe အကွက် ၁ ခု ဖွင့်ပါ။")); return
    multiplier, payout = _mine_multiplier(len(opened)), int(int(game["bet"]) * _mine_multiplier(len(opened)))
    with db() as conn:
        conn.execute("DELETE FROM game_mines WHERE chat_id=? AND user_id=?", (chat_id, user.id))
        _wallet_adjust(conn, user.id, payout)
        conn.execute("UPDATE game_accounts SET xp=xp+?,wins=wins+1 WHERE chat_id=? AND user_id=?",
                     (8 + len(opened)*2, chat_id, user.id))
        row = conn.execute("SELECT coins FROM game_accounts WHERE chat_id=? AND user_id=?", (chat_id, user.id)).fetchone()
    record_game_event(user.id, True, 8 + len(opened)*2)
    await update.effective_message.reply_text(_hud_text(
        f"💰 CASHOUT SUCCESS!\n💎 Safe: {len(opened)} | {multiplier:g}x"
        f"\n🎁 Payout: {payout:,} Coin | ⭐ +{8 + len(opened)*2} XP\n🪙 Balance: {row['coins']:,}"))

async def spin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update):
        return
    chat_id, user = update.effective_chat.id, update.effective_user

    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(_hud_text(
            "🎰 Slot ကစားရန် လောင်းကြေးထည့်ပါ။\n"
            "အသုံးပြုပုံ: /spin 100\n"
            "အနည်းဆုံး 10 Coin လောင်းနိုင်ပါတယ်။\n"
            "❌ ရှုံးရင် လောင်းကြေးဆုံးရှုံးမယ်။\n"
            "🏆 နိုင်ရင် 1.2x မှ 5x အထိ ပြန်ရနိုင်ပါတယ်။"
        ))
        return

    bet = int(context.args[0])
    if bet < 10 or bet > 1_000_000_000:
        await update.effective_message.reply_text(_hud_text("❌ လောင်းကြေးသည် 10 Coin မှ 1,000,000,000 Coin အတွင်း ဖြစ်ရပါမယ်။"))
        return

    # Atomically reserve the stake so simultaneous spins cannot spend the same coins.
    _account(chat_id, user)
    with db() as conn:
        debit = conn.execute(
            "UPDATE game_wallets SET coins=coins-? WHERE user_id=? AND coins>=?",
            (bet, user.id, bet),
        )
        if debit.rowcount == 1:
            conn.execute(
                "UPDATE game_accounts SET coins=(SELECT coins FROM game_wallets WHERE user_id=?) "
                "WHERE user_id=? AND coins<>(SELECT coins FROM game_wallets WHERE user_id=?)",
                (user.id, user.id, user.id),
            )
        if debit.rowcount != 1:
            row = conn.execute(
                "SELECT coins FROM game_wallets WHERE user_id=?",
                (user.id,),
            ).fetchone()
            balance_now = int(row["coins"]) if row else 0
            await update.effective_message.reply_text(_hud_text(
                f"🪙 Coin မလုံလောက်ပါ။\nလက်ကျန်: {balance_now:,} Coin\nလောင်းကြေး: {bet:,} Coin"
            ))
            return

    # Published slot odds target about 93.8% theoretical return before XP rewards.
    reward = random.choices([0, 10, 25, 50, 100, 200], weights=[43, 24, 18, 9, 5, 1], k=1)[0]
    prize_icons = {10: "🍒", 25: "🍋", 50: "🍉", 100: "🍇", 200: "💎"}
    multipliers = {10: 1.2, 25: 1.5, 50: 2.0, 100: 3.0, 200: 5.0}
    symbols = ["🍒", "🍋", "🍉", "🍇", "🍊", "⭐", "💎"]
    message = await update.effective_message.reply_text(_hud_text(
        "🎰 NEXUS SLOT MACHINE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "╭─────── 🎰 ───────╮\n"
        "│   ❔  │  ❔  │  ❔   │\n"
        "╰─────────────────╯\n"
        f"🪙 လောင်းကြေး: {bet:,} Coin\n"
        "⚡ REELS STARTING..."
    ))

    for frame in range(6):
        reels = [random.choice(symbols) for _ in range(3)]
        speed = "▰" * (6 - frame) + "▱" * frame
        await message.edit_text(_hud_text(
            "🎰 NEXUS SLOT MACHINE\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"╭─────── 🎰 ───────╮\n"
            f"│   {reels[0]}  │  {reels[1]}  │  {reels[2]}   │\n"
            "╰─────────────────╯\n"
            f"🪙 BET: {bet:,} COIN\n"
            f"⚡ SPINNING  {speed}"
        ))
        await asyncio.sleep(1.1)

    if reward:
        icon = prize_icons[reward]
        final_reels = [icon, icon, icon]
        multiplier = multipliers[reward]
        payout = int(bet * multiplier)
        with db() as conn:
            _wallet_adjust(conn, user.id, payout)
            conn.execute(
                "UPDATE game_accounts SET xp=xp+3, wins=wins+1 WHERE chat_id=? AND user_id=?",
                (chat_id, user.id),
            )
            row = conn.execute(
                "SELECT coins FROM game_accounts WHERE chat_id=? AND user_id=?",
                (chat_id, user.id),
            ).fetchone()
        net = payout - bet
        result = (
            f"🎉 WIN! {multiplier:g}x PAYOUT\n"
            f"💰 ပြန်ရငွေ: +{payout:,} Coin\n"
            f"📈 အသားတင်အမြတ်: +{net:,} Coin\n"
            f"🪙 လက်ကျန်: {row['coins']:,} Coin\n⭐ +3 XP"
        )
    else:
        final_reels = random.sample(symbols[:-1], 3)
        if len(set(final_reels)) == 1:
            final_reels[2] = "⭐"
        with db() as conn:
            conn.execute(
                "UPDATE game_accounts SET losses=losses+1 WHERE chat_id=? AND user_id=?",
                (chat_id, user.id),
            )
            row = conn.execute(
                "SELECT coins FROM game_accounts WHERE chat_id=? AND user_id=?",
                (chat_id, user.id),
            ).fetchone()
        result = (
            f"💥 LOSE! -{bet:,} Coin\n"
            f"🪙 လက်ကျန်: {row['coins']:,} Coin"
        )

    record_game_event(user.id, bool(reward), 3 if reward else 1)
    await message.edit_text(_hud_text(
        "🎰 NEXUS SLOT MACHINE • RESULT\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"╭─────── 🎰 ───────╮\n"
        f"│   {final_reels[0]}  │  {final_reels[1]}  │  {final_reels[2]}   │\n"
        "╰─────────────────╯\n"
        f"🎯 လောင်းကြေး: {bet:,} Coin\n"
        f"{result}"
    ))

async def duel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update): return
    msg,user=update.effective_message,update.effective_user
    target=msg.reply_to_message.from_user if msg.reply_to_message else None
    bet=_valid_bet(context)
    if not target or target.is_bot or target.id==user.id:
        await msg.reply_text(_hud_text("🥊 Member Message ကို Reply လုပ်ပြီး /duel 100 ပို့ပါ။")); return
    if bet is None:
        await msg.reply_text(_hud_text("🥊 /duel 100 ဖြင့် နှစ်ဦးလောင်းကြေး သတ်မှတ်ပါ။")); return
    _account(update.effective_chat.id,user); _account(update.effective_chat.id,target)
    with db() as conn:
        row=conn.execute("SELECT coins FROM game_wallets WHERE user_id=?",(user.id,)).fetchone()
        if int(row["coins"])<bet:
            await msg.reply_text(_hud_text("❌ သင့် Coin မလုံလောက်ပါ။")); return
        conn.execute("INSERT INTO game_duels(chat_id,challenger_id,opponent_id,challenger_name,opponent_name,created_at,wager) VALUES(?,?,?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET challenger_id=excluded.challenger_id,opponent_id=excluded.opponent_id,challenger_name=excluded.challenger_name,opponent_name=excluded.opponent_name,created_at=excluded.created_at,wager=excluded.wager",(update.effective_chat.id,user.id,target.id,_display(user),_display(target),time.time(),bet))
    await msg.reply_text(_hud_text(f"🥊 {_display(user)} က {_display(target)} ကို Duel စိန်ခေါ်သည်!\nနှစ်ဦးလောင်းကြေး {bet:,} Coin\nလက်ခံရန် /duelaccept | ငြင်းရန် /dueldecline\n🏆 Winner ယူငွေ {bet*2:,} Coin"))

async def duel_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _ready(update): return
    chat_id,user=update.effective_chat.id,update.effective_user
    with db() as conn:
        row=conn.execute("SELECT * FROM game_duels WHERE chat_id=?",(chat_id,)).fetchone()
        if not row or float(row["created_at"])<time.time()-300:
            conn.execute("DELETE FROM game_duels WHERE chat_id=?",(chat_id,))
            await update.effective_message.reply_text(_hud_text("🥊 လက်ခံနိုင်တဲ့ Duel မရှိပါ။")); return
        if int(row["opponent_id"])!=user.id:
            await update.effective_message.reply_text(_hud_text("စိန်ခေါ်ခံရသူသာ လက်ခံနိုင်ပါတယ်။")); return
        wager=int(row["wager"] or 0); challenger=int(row["challenger_id"])
        d1=conn.execute("UPDATE game_wallets SET coins=coins-? WHERE user_id=? AND coins>=?",(wager,challenger,wager))
        if d1.rowcount!=1:
            conn.execute("DELETE FROM game_duels WHERE chat_id=?",(chat_id,))
            await update.effective_message.reply_text(_hud_text("❌ စိန်ခေါ်သူ Coin မလုံလောက်လို့ Duel ပယ်ဖျက်သည်။")); return
        d2=conn.execute("UPDATE game_wallets SET coins=coins-? WHERE user_id=? AND coins>=?",(wager,user.id,wager))
        if d2.rowcount!=1:
            conn.execute("UPDATE game_wallets SET coins=coins+? WHERE user_id=?",(wager,challenger))
            wallet = conn.execute("SELECT coins FROM game_wallets WHERE user_id=?", (challenger,)).fetchone()
            if wallet:
                conn.execute("UPDATE game_accounts SET coins=? WHERE user_id=?", (int(wallet["coins"]), challenger))
            conn.execute("DELETE FROM game_duels WHERE chat_id=?", (chat_id,))
            await update.effective_message.reply_text(_hud_text("❌ သင့် Coin မလုံလောက်ပါ။ Duel ပယ်ဖျက်လိုက်ပါပြီ။")); return
        conn.execute("DELETE FROM game_duels WHERE chat_id=?",(chat_id,))
        for wallet_user_id in (challenger, user.id):
            wallet = conn.execute("SELECT coins FROM game_wallets WHERE user_id=?", (wallet_user_id,)).fetchone()
            if wallet:
                conn.execute(
                    "UPDATE game_accounts SET coins=? WHERE user_id=? AND coins<>?",
                    (int(wallet["coins"]), wallet_user_id, int(wallet["coins"])),
                )
    winner=random.choice([challenger,user.id]); loser=user.id if winner==challenger else challenger
    winner_name=row["challenger_name"] if winner==challenger else row["opponent_name"]; payout=wager*2
    m=await update.effective_message.reply_text(_hud_text(f"🥊 NEXUS DUEL\n🔴 {row['challenger_name']} VS 🔵 {row['opponent_name']}\nFIGHT!"))
    await _animate_hud(m,["🥊 ROUND 1\n⚔️ FIGHT!","🥊 ROUND 2\n💥 CLASH!","🥊 FINAL ROUND\n🔥 FINISH MOVE!"],0.55)
    with db() as conn:
        _wallet_adjust(conn, winner, payout)
        conn.execute("UPDATE game_accounts SET xp=xp+30,wins=wins+1 WHERE chat_id=? AND user_id=?",(chat_id,winner))
        conn.execute("UPDATE game_accounts SET xp=xp+10,losses=losses+1 WHERE chat_id=? AND user_id=?",(chat_id,loser))
        bal=conn.execute("SELECT coins FROM game_wallets WHERE user_id=?",(winner,)).fetchone()
    record_game_event(winner, True, 30)
    record_game_event(loser, False, 10)
    await m.edit_text(_hud_text(f"🥊 DUEL RESULT\n🏆 Winner: {winner_name}\n💰 +{payout:,} Coin\n⭐ Winner +30 XP / Loser +10 XP\n🪙 Winner Balance: {bal['coins']:,}"))

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
    amount_index = 0

    # No reply + one amount means the Owner manages their own wallet.
    if not target and context.args and context.args[0].isdigit():
        if len(context.args) == 1:
            target = user
        else:
            target_id = int(context.args[0])
            with db() as conn:
                row = conn.execute(
                    "SELECT user_id, MAX(display_name) AS display_name FROM game_accounts "
                    "WHERE user_id=? GROUP BY user_id",
                    (target_id,),
                ).fetchone()
            if not row:
                await msg.reply_text(_hud_text("❌ ဒီ Group ရဲ့ Game Account ထဲမှာ အဲဒီ USER_ID ကို မတွေ့ပါ။"))
                return
            from telegram import User
            target = User(id=int(row["user_id"]), first_name=row["display_name"], is_bot=False)
            amount_index = 1

    if not target or target.is_bot:
        await msg.reply_text(_hud_text(
            "အသုံးပြုပုံ:\n"
            "• ကိုယ့် Coin ထည့်ရန်: /addcoins 1000000\n"
            "• ကိုယ့် Coin နှုတ်ရန်: /takecoins 500\n"
            "• Member ကို Reply လုပ်ပြီး /addcoins 500\n"
            "• USER_ID နဲ့: /addcoins USER_ID 500"
        ))
        return

    if len(context.args) <= amount_index or not context.args[amount_index].isdigit():
        await msg.reply_text(_hud_text("Coin ပမာဏကို အပေါင်းကိန်းပြည့်နဲ့ ထည့်ပါ။ ဥပမာ: /addcoins 1000000"))
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
