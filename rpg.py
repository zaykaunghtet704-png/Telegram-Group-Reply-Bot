"""Persistent RPG progression, daily missions, achievements and cosmetic inventory."""
import logging
import random
import time
from datetime import date

from database import connect as db
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger("myaaye-rpg")

SHOP = {
    "frame_neon": {"name": "Neon Profile Frame", "emoji": "🟣", "price": 5000, "slot": "frame"},
    "frame_dragon": {"name": "Dragon Profile Frame", "emoji": "🐉", "price": 15000, "slot": "frame"},
    "title_champion": {"name": "Champion Title", "emoji": "🏆", "price": 10000, "slot": "title"},
    "title_legend": {"name": "Legend Title", "emoji": "👑", "price": 25000, "slot": "title"},
}
MISSIONS = {
    "play3": {"name": "Play 3 Games", "goal": 3, "reward": 300, "kind": "play"},
    "win2": {"name": "Win 2 Games", "goal": 2, "reward": 500, "kind": "win"},
    "xp30": {"name": "Earn 30 XP", "goal": 30, "reward": 400, "kind": "xp"},
}


def init_rpg():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS rpg_stats (
            user_id BIGINT PRIMARY KEY,
            games_played BIGINT NOT NULL DEFAULT 0,
            wins BIGINT NOT NULL DEFAULT 0,
            losses BIGINT NOT NULL DEFAULT 0,
            total_xp BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS rpg_missions (
            user_id BIGINT NOT NULL,
            mission_date DATE NOT NULL,
            mission_key TEXT NOT NULL,
            progress BIGINT NOT NULL DEFAULT 0,
            claimed BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id, mission_date, mission_key)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS rpg_inventory (
            user_id BIGINT NOT NULL,
            item_id TEXT NOT NULL,
            quantity BIGINT NOT NULL DEFAULT 0,
            acquired_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, item_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS rpg_loadout (
            user_id BIGINT NOT NULL,
            slot TEXT NOT NULL,
            item_id TEXT NOT NULL,
            equipped_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, slot)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS rpg_daily_claims (
            user_id BIGINT PRIMARY KEY,
            last_claim DATE NOT NULL,
            streak BIGINT NOT NULL DEFAULT 1
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS rpg_adventure_cooldowns (
            user_id BIGINT PRIMARY KEY,
            last_run DOUBLE PRECISION NOT NULL DEFAULT 0
        )""")


def _ensure_missions(conn, user_id):
    today = date.today()
    for key in MISSIONS:
        conn.execute(
            "INSERT INTO rpg_missions(user_id,mission_date,mission_key) VALUES(?,?,?) ON CONFLICT DO NOTHING",
            (int(user_id), today, key),
        )


def record_game_event(user_id, won, xp_earned):
    """Record a completed round. Fail closed so RPG tracking never breaks a game."""
    try:
        user_id, xp_earned = int(user_id), max(0, int(xp_earned))
        with db() as conn:
            conn.execute(
                """INSERT INTO rpg_stats(user_id,games_played,wins,losses,total_xp)
                   VALUES(?,1,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                   games_played=rpg_stats.games_played+1,
                   wins=rpg_stats.wins+EXCLUDED.wins,
                   losses=rpg_stats.losses+EXCLUDED.losses,
                   total_xp=rpg_stats.total_xp+EXCLUDED.total_xp,
                   updated_at=CURRENT_TIMESTAMP""",
                (user_id, 1 if won else 0, 0 if won else 1, xp_earned),
            )
            _ensure_missions(conn, user_id)
            increments = {"play": 1, "win": 1 if won else 0, "xp": xp_earned}
            for key, mission in MISSIONS.items():
                amount = increments[mission["kind"]]
                if amount:
                    conn.execute(
                        """UPDATE rpg_missions SET progress=LEAST(?,progress+?)
                           WHERE user_id=? AND mission_date=? AND mission_key=?""",
                        (mission["goal"], amount, user_id, date.today(), key),
                    )
    except Exception:
        log.exception("Could not record RPG game event for user %s", user_id)


async def rpg_profile(update, context):
    user = update.effective_user
    if not user:
        return
    with db() as conn:
        stats = conn.execute("SELECT * FROM rpg_stats WHERE user_id=?", (user.id,)).fetchone()
        wallet = conn.execute("SELECT coins FROM game_wallets WHERE user_id=?", (user.id,)).fetchone()
        loadout = conn.execute("SELECT slot,item_id FROM rpg_loadout WHERE user_id=?", (user.id,)).fetchall()
        daily = conn.execute("SELECT streak FROM rpg_daily_claims WHERE user_id=?", (user.id,)).fetchone()
    stats = stats or {"games_played": 0, "wins": 0, "losses": 0, "total_xp": 0}
    daily_streak = int(daily["streak"]) if daily else 0
    coins = int(wallet["coins"]) if wallet else 0
    xp = int(stats["total_xp"])
    level = xp // 100 + 1
    level_xp = xp % 100
    rank_title = next(
        (name for threshold, name in reversed([
            (1, "Novice Adventurer"), (5, "Battle Fighter"),
            (10, "Elite Warrior"), (20, "Dungeon Master"),
            (30, "Dragon Slayer"), (50, "Mythic Hero"),
            (75, "Divine Champion"), (100, "Immortal Legend"),
        ]) if level >= threshold),
        "Novice Adventurer",
    )
    progress_bar = "🟩" * (level_xp // 10) + "⬛" * (10 - level_xp // 10)
    win_rate = (int(stats["wins"]) * 100 / int(stats["games_played"])) if int(stats["games_played"]) else 0
    equipped = {row["slot"]: SHOP.get(row["item_id"], {}).get("name", "") for row in loadout}
    title = equipped.get("title") or "New Adventurer"
    frame = equipped.get("frame") or "Default Frame"
    await update.effective_message.reply_text(
        "╭━━━ 💗 MYAE RPG PROFILE ━━━╮\n"
        f"👤 {user.full_name}\n🏷️ {title}\n🖼️ {frame}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🏅 Class: {rank_title}\n⭐ RPG Level: {level}\n✨ Total XP: {xp:,}\n"
        f"📊 Level Progress: [{progress_bar}] {level_xp}/100 XP\n"
        f"🎮 Games: {int(stats['games_played']):,}\n"
        f"🏆 Wins: {int(stats['wins']):,} | 💥 Losses: {int(stats['losses']):,}\n"
        f"📈 Win Rate: {win_rate:.1f}%\n🔥 Daily Streak: {daily_streak} day(s)\n"
        f"🪙 Global Coins: {coins:,}\n"
        "╰━━━━━━━━━━━━━━━━━━━━╯\n"
        "\n💡 အောက်က Menu ခလုတ်တွေကို နှိပ်ပြီး RPG ကို လွယ်လွယ်ကူကူ သုံးနိုင်ပါတယ်။",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎁 Daily Reward", callback_data="rpg:daily"),
             InlineKeyboardButton("⚔️ Adventure", callback_data="rpg:adventure")],
            [InlineKeyboardButton("🎯 Missions", callback_data="rpg:missions"),
             InlineKeyboardButton("🎒 Inventory", callback_data="rpg:inventory")],
            [InlineKeyboardButton("🛒 Shop", callback_data="rpg:shop"),
             InlineKeyboardButton("🏆 Leaderboard", callback_data="rpg:top")],
            [InlineKeyboardButton("🐉 World Boss", callback_data="rpg:boss"),
             InlineKeyboardButton("🛡 Guild", callback_data="rpg:guild")],
            [InlineKeyboardButton("📖 ဘယ်လိုကစားမလဲ?", callback_data="rpg:help")],
        ]),
    )


async def missions_command(update, context):
    user = update.effective_user
    if not user:
        return
    with db() as conn:
        _ensure_missions(conn, user.id)
        rows = conn.execute(
            "SELECT mission_key,progress,claimed FROM rpg_missions WHERE user_id=? AND mission_date=? ORDER BY mission_key",
            (user.id, date.today()),
        ).fetchall()
    lines = ["🎯 MYAE DAILY MISSIONS", "━━━━━━━━━━━━━━━━━━━━"]
    for row in rows:
        mission = MISSIONS[row["mission_key"]]
        progress = min(int(row["progress"]), mission["goal"])
        if row["claimed"]:
            status = "✅ CLAIMED"
        elif progress >= mission["goal"]:
            status = "🎁 READY"
        else:
            status = f"{progress}/{mission['goal']}"
        lines.append(f"{mission['name']}\n   {status} • Reward {mission['reward']:,} Coin")
    lines.extend(["━━━━━━━━━━━━━━━━━━━━", "ဆုယူရန် /missionclaim <mission_id>", "ID: play3, win2, xp30"])
    await update.effective_message.reply_text("\n".join(lines))


async def mission_claim_command(update, context):
    user = update.effective_user
    if not user:
        return
    if not context.args or context.args[0] not in MISSIONS:
        await update.effective_message.reply_text("အသုံးပြုပုံ: /missionclaim play3 | win2 | xp30")
        return
    key = context.args[0]
    mission = MISSIONS[key]
    with db() as conn:
        _ensure_missions(conn, user.id)
        row = conn.execute(
            """UPDATE rpg_missions SET claimed=1
               WHERE user_id=? AND mission_date=? AND mission_key=? AND progress>=? AND claimed=0
               RETURNING mission_key""",
            (user.id, date.today(), key, mission["goal"]),
        ).fetchone()
        if not row:
            await update.effective_message.reply_text("❌ Mission မပြီးသေးပါ၊ သို့မဟုတ် ဆုယူပြီးသားပါ။ /missions")
            return
        conn.execute(
            "INSERT INTO game_wallets(user_id,coins) VALUES(?,100) ON CONFLICT(user_id) DO NOTHING",
            (user.id,),
        )
        wallet = conn.execute(
            "UPDATE game_wallets SET coins=coins+? WHERE user_id=? RETURNING coins",
            (mission["reward"], user.id),
        ).fetchone()
        conn.execute(
            "UPDATE game_accounts SET coins=? WHERE user_id=?",
            (int(wallet["coins"]), user.id),
        )
    await update.effective_message.reply_text(
        f"🎉 MISSION REWARD CLAIMED!\n+{mission['reward']:,} Coin\n🪙 လက်ကျန် {int(wallet['coins']):,} Coin"
    )


async def achievements_command(update, context):
    user = update.effective_user
    if not user:
        return
    with db() as conn:
        stats = conn.execute("SELECT * FROM rpg_stats WHERE user_id=?", (user.id,)).fetchone()
    stats = stats or {"games_played": 0, "wins": 0, "total_xp": 0}
    checks = [
        ("🎮 First Steps", int(stats["games_played"]) >= 1, "Play your first game"),
        ("⚔️ Battle Veteran", int(stats["games_played"]) >= 25, "Play 25 games"),
        ("🏆 Rising Champion", int(stats["wins"]) >= 10, "Win 10 games"),
        ("👑 Arena Legend", int(stats["wins"]) >= 100, "Win 100 games"),
        ("✨ XP Collector", int(stats["total_xp"]) >= 1000, "Earn 1,000 RPG XP"),
        ("🌟 Mythic Journey", int(stats["total_xp"]) >= 10000, "Earn 10,000 RPG XP"),
    ]
    lines = ["🎖️ ACHIEVEMENT COLLECTION", "━━━━━━━━━━━━━━━━━━━━"]
    for name, unlocked, description in checks:
        lines.append(f"{'✅' if unlocked else '🔒'} {name}\n   {description}")
    await update.effective_message.reply_text("\n".join(lines))


async def shop_command(update, context):
    lines = ["🛒 MYAE COSMETIC SHOP", "━━━━━━━━━━━━━━━━━━━━"]
    for item_id, item in SHOP.items():
        lines.append(f"{item['emoji']} {item['name']}\n   ID: {item_id} • {item['price']:,} Coin")
    lines.extend(["━━━━━━━━━━━━━━━━━━━━", "ဝယ်ရန် /buy ITEM_ID", "တပ်ဆင်ရန် /equip ITEM_ID"])
    await update.effective_message.reply_text("\n".join(lines))


async def buy_command(update, context):
    user = update.effective_user
    if not user:
        return
    item_id = context.args[0] if context.args else ""
    item = SHOP.get(item_id)
    if not item:
        await update.effective_message.reply_text("❌ Item ID မမှန်ပါ။ /shop ကြည့်ပါ။")
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO game_wallets(user_id,coins) VALUES(?,100) ON CONFLICT(user_id) DO NOTHING",
            (user.id,),
        )
        debit = conn.execute(
            "UPDATE game_wallets SET coins=coins-? WHERE user_id=? AND coins>=?",
            (item["price"], user.id, item["price"]),
        )
        if debit.rowcount != 1:
            await update.effective_message.reply_text("🪙 Coin မလုံလောက်ပါ။ /balance စစ်ပါ။")
            return
        conn.execute(
            """INSERT INTO rpg_inventory(user_id,item_id,quantity) VALUES(?,?,1)
               ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=rpg_inventory.quantity+1""",
            (user.id, item_id),
        )
        wallet = conn.execute("SELECT coins FROM game_wallets WHERE user_id=?", (user.id,)).fetchone()
        conn.execute("UPDATE game_accounts SET coins=? WHERE user_id=?", (int(wallet["coins"]), user.id))
    await update.effective_message.reply_text(
        f"✅ ဝယ်ယူပြီးပါပြီ!\n{item['emoji']} {item['name']}\n🪙 လက်ကျန် {int(wallet['coins']):,} Coin\n/equip {item_id} ဖြင့် တပ်ဆင်ပါ။"
    )


async def inventory_command(update, context):
    user = update.effective_user
    if not user:
        return
    with db() as conn:
        rows = conn.execute("SELECT item_id,quantity FROM rpg_inventory WHERE user_id=? AND quantity>0 ORDER BY item_id", (user.id,)).fetchall()
        loadout = conn.execute("SELECT slot,item_id FROM rpg_loadout WHERE user_id=?", (user.id,)).fetchall()
    equipped = {row["item_id"] for row in loadout}
    lines = ["🎒 MYAE INVENTORY", "━━━━━━━━━━━━━━━━━━━━"]
    if not rows:
        lines.append("ပစ္စည်းမရှိသေးပါ။ /shop မှာ ဝယ်ယူနိုင်ပါတယ်။")
    for row in rows:
        item = SHOP.get(row["item_id"], {})
        marker = " • EQUIPPED" if row["item_id"] in equipped else ""
        lines.append(f"{item.get('emoji','🎁')} {item.get('name',row['item_id'])} ×{int(row['quantity'])}{marker}\n   ID: {row['item_id']}")
    await update.effective_message.reply_text("\n".join(lines))


async def equip_command(update, context):
    user = update.effective_user
    if not user:
        return
    item_id = context.args[0] if context.args else ""
    item = SHOP.get(item_id)
    if not item:
        await update.effective_message.reply_text("အသုံးပြုပုံ: /equip ITEM_ID")
        return
    with db() as conn:
        owned = conn.execute(
            "SELECT quantity FROM rpg_inventory WHERE user_id=? AND item_id=?",
            (user.id, item_id),
        ).fetchone()
        if not owned or int(owned["quantity"]) < 1:
            await update.effective_message.reply_text("❌ ဒီ Item ကို မပိုင်ဆိုင်သေးပါ။ /shop")
            return
        conn.execute(
            """INSERT INTO rpg_loadout(user_id,slot,item_id) VALUES(?,?,?)
               ON CONFLICT(user_id,slot) DO UPDATE SET item_id=EXCLUDED.item_id,equipped_at=CURRENT_TIMESTAMP""",
            (user.id, item["slot"], item_id),
        )
    await update.effective_message.reply_text(f"✨ {item['name']} ကို Profile မှာ တပ်ဆင်ပြီးပါပြီ။ /rpg")


async def rpg_daily_command(update, context):
    """Claim a once-per-day RPG login reward with a persistent streak."""
    user = update.effective_user
    if not user:
        return
    today = date.today()
    with db() as conn:
        claim = conn.execute(
            """INSERT INTO rpg_daily_claims(user_id,last_claim,streak) VALUES(?,?,1)
               ON CONFLICT(user_id) DO UPDATE SET
               streak=CASE
                   WHEN rpg_daily_claims.last_claim=EXCLUDED.last_claim - 1
                   THEN rpg_daily_claims.streak+1 ELSE 1 END,
               last_claim=EXCLUDED.last_claim
               WHERE rpg_daily_claims.last_claim < EXCLUDED.last_claim
               RETURNING streak""",
            (user.id, today),
        ).fetchone()
        if not claim:
            await update.effective_message.reply_text(
                "⏳ ဒီနေ့ RPG Daily Reward ကို ယူပြီးပါပြီ။ မနက်ဖြန် ပြန်လာခဲ့နော်။"
            )
            return
        streak = int(claim["streak"])
        coins = min(1000, 200 + (streak - 1) * 50)
        xp = min(50, 20 + (streak - 1) * 5)
        conn.execute(
            "INSERT INTO game_wallets(user_id,coins) VALUES(?,100) ON CONFLICT(user_id) DO NOTHING",
            (user.id,),
        )
        wallet = conn.execute(
            "UPDATE game_wallets SET coins=coins+? WHERE user_id=? RETURNING coins",
            (coins, user.id),
        ).fetchone()
        conn.execute(
            "UPDATE game_accounts SET coins=? WHERE user_id=?",
            (int(wallet["coins"]), user.id),
        )
        conn.execute(
            """INSERT INTO rpg_stats(user_id,total_xp) VALUES(?,?)
               ON CONFLICT(user_id) DO UPDATE SET
               total_xp=rpg_stats.total_xp+EXCLUDED.total_xp,
               updated_at=CURRENT_TIMESTAMP""",
            (user.id, xp),
        )
        _ensure_missions(conn, user.id)
        conn.execute(
            """UPDATE rpg_missions SET progress=LEAST(?,progress+?)
               WHERE user_id=? AND mission_date=? AND mission_key='xp30'""",
            (MISSIONS["xp30"]["goal"], xp, user.id, today),
        )
    await update.effective_message.reply_text(
        "╭━━ 💗 MYAE RPG DAILY ━━╮\n"
        f"🔥 Login Streak: {streak} day(s)\n"
        f"🪙 Reward: +{coins:,} Coins\n"
        f"✨ RPG XP: +{xp} XP\n"
        f"💰 Wallet: {int(wallet['coins']):,} Coins\n"
        "╰━━━━━━━━━━━━━━━━━━╯\n"
        "မနက်ဖြန်လာရင် Streak ဆက်တိုးမယ်နော်။"
    )


async def adventure_command(update, context):
    """Resolve a short solo RPG encounter, limited to one attempt per 30 minutes."""
    user = update.effective_user
    if not user:
        return
    now = time.time()
    with db() as conn:
        conn.execute(
            "INSERT INTO rpg_adventure_cooldowns(user_id,last_run) VALUES(?,0) ON CONFLICT(user_id) DO NOTHING",
            (user.id,),
        )
        started = conn.execute(
            """UPDATE rpg_adventure_cooldowns SET last_run=?
               WHERE user_id=? AND last_run<=? RETURNING last_run""",
            (now, user.id, now - 1800),
        ).fetchone()
        if not started:
            row = conn.execute(
                "SELECT last_run FROM rpg_adventure_cooldowns WHERE user_id=?",
                (user.id,),
            ).fetchone()
            remaining = max(1, int(1800 - (now - float(row["last_run"]))))
            minutes, seconds = divmod(remaining, 60)
            await update.effective_message.reply_text(
                f"⏳ နောက် Adventure အတွက် {minutes} မိနစ် {seconds} စက္ကန့် စောင့်ပါ။"
            )
            return
        stats = conn.execute(
            "SELECT total_xp FROM rpg_stats WHERE user_id=?", (user.id,)
        ).fetchone()
        level = (int(stats["total_xp"]) // 100 + 1) if stats else 1

        monsters = [
            ("🦇 Shadow Bat", 7), ("🐺 Dark Wolf", 10),
            ("👹 Goblin King", 14), ("🐉 Baby Dragon", 19),
            ("💀 Bone Knight", 16),
        ]
        monster, base_power = random.choice(monsters)
        equipped = conn.execute(
            "SELECT item_id FROM rpg_equipment_loadout WHERE user_id=?",
            (user.id,),
        ).fetchall()
        gear_power = sum(
            {"flame_sword": 12, "dragon_armor": 10, "phoenix_pet": 15, "shadow_blade": 25}.get(row["item_id"], 0)
            for row in equipped
        )
        player_power = level * 2 + gear_power + random.randint(1, 20)
        enemy_power = base_power + level + random.randint(1, 12)
        won = player_power >= enemy_power
        xp = random.randint(25, 45) if won else random.randint(8, 15)
        reward = min(2500, 200 + level * 15 + random.randint(50, 250)) if won else 0
        if reward:
            conn.execute(
                "INSERT INTO game_wallets(user_id,coins) VALUES(?,100) ON CONFLICT(user_id) DO NOTHING",
                (user.id,),
            )
            wallet = conn.execute(
                "UPDATE game_wallets SET coins=coins+? WHERE user_id=? RETURNING coins",
                (reward, user.id),
            ).fetchone()
            conn.execute(
                "UPDATE game_accounts SET coins=? WHERE user_id=?",
                (int(wallet["coins"]), user.id),
            )
        else:
            wallet = conn.execute(
                "SELECT coins FROM game_wallets WHERE user_id=?", (user.id,)
            ).fetchone()
    record_game_event(user.id, won, xp)
    balance = int(wallet["coins"]) if wallet else 0
    result = "🏆 VICTORY!" if won else "💥 DEFEAT!"
    reward_line = f"🪙 Coin Reward: +{reward:,}" if reward else "🪙 Coin Reward: +0"
    await update.effective_message.reply_text(
        "╭━━ ⚔️ MYAE DUNGEON ━━╮\n"
        f"👤 Adventurer: {user.full_name}\n"
        f"🧟 Enemy: {monster}\n"
        f"⚔️ Your Power: {player_power}\n"
        f"💀 Enemy Power: {enemy_power}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{result}\n✨ RPG XP: +{xp}\n{reward_line}\n"
        f"💰 Wallet: {balance:,} Coins\n"
        "╰━━━━━━━━━━━━━━━━━━╯\n"
        "Adventure ပြန်ဝင်ရန် မိနစ် ၃၀ စောင့်ပါ။ /rpg"
    )


async def rpg_top_command(update, context):
    with db() as conn:
        rows = conn.execute(
            """SELECT s.user_id,s.total_xp,
                      COALESCE((SELECT g.display_name FROM game_accounts g
                                WHERE g.user_id=s.user_id AND g.display_name<>''
                                ORDER BY g.display_name LIMIT 1), 'Adventurer') AS display_name
               FROM rpg_stats s ORDER BY s.total_xp DESC,s.user_id ASC LIMIT 10"""
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text("🌟 RPG Leaderboard မှာ ကစားသမား မရှိသေးပါ။")
        return
    lines = ["🏆 MYAE RPG TOP 10", "━━━━━━━━━━━━━━━━━━━━"]
    for index, row in enumerate(rows, 1):
        level = int(row["total_xp"]) // 100 + 1
        lines.append(
            f"{index}. {row['display_name']}\n"
            f"   ⭐ Level {level} • ✨ {int(row['total_xp']):,} XP"
        )
    lines.append("━━━━━━━━━━━━━━━━━━━━\nကိုယ့် Profile: /rpg")
    await update.effective_message.reply_text("\n".join(lines))
