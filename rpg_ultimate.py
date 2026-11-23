"""Ultimate RPG features: animated-feel dungeon raids, hero summons and treasure runs.

All rewards use the existing shared game_wallets balance. Database writes are
short and atomic; no network calls or sleeps are performed inside transactions.
"""
import random
import time

from database import connect as db
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

HEROES = {
    "ember_knight": ("🔥 Ember Knight", "Epic", 18),
    "moon_archer": ("🌙 Moon Archer", "Epic", 16),
    "storm_mage": ("⚡ Storm Mage", "Legendary", 24),
    "abyss_dragon": ("🐉 Abyss Dragon", "Mythic", 35),
    "forest_guardian": ("🌿 Forest Guardian", "Rare", 12),
    "frost_assassin": ("❄️ Frost Assassin", "Legendary", 25),
}
SUMMON_COST = 5000


def init_rpg_ultimate():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS rpg_ultimate_runs (
            user_id BIGINT PRIMARY KEY,
            enemy_name TEXT NOT NULL,
            player_hp BIGINT NOT NULL,
            enemy_hp BIGINT NOT NULL,
            floor BIGINT NOT NULL DEFAULT 1,
            started_at DOUBLE PRECISION NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS rpg_hero_inventory (
            user_id BIGINT NOT NULL,
            hero_id TEXT NOT NULL,
            quantity BIGINT NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id,hero_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS rpg_treasure_runs (
            user_id BIGINT PRIMARY KEY,
            created_at DOUBLE PRECISION NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS rpg_ultimate_stats (
            user_id BIGINT PRIMARY KEY,
            dungeon_wins BIGINT NOT NULL DEFAULT 0,
            treasure_runs BIGINT NOT NULL DEFAULT 0,
            summons BIGINT NOT NULL DEFAULT 0
        )""")


def _wallet(conn, user_id):
    conn.execute(
        "INSERT INTO game_wallets(user_id,coins) VALUES(?,100) ON CONFLICT(user_id) DO NOTHING",
        (user_id,),
    )
    row = conn.execute("SELECT coins FROM game_wallets WHERE user_id=?", (user_id,)).fetchone()
    return int(row["coins"])


def _sync_legacy(conn, user_id, coins):
    conn.execute("UPDATE game_accounts SET coins=? WHERE user_id=?", (coins, user_id))


def _level(conn, user_id):
    row = conn.execute("SELECT total_xp FROM rpg_stats WHERE user_id=?", (user_id,)).fetchone()
    return int(row["total_xp"]) // 100 + 1 if row else 1


def _add_progress(conn, user_id, xp, won=True):
    conn.execute(
        """INSERT INTO rpg_stats(user_id,games_played,wins,losses,total_xp)
           VALUES(?,1,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
           games_played=rpg_stats.games_played+1,
           wins=rpg_stats.wins+EXCLUDED.wins,
           losses=rpg_stats.losses+EXCLUDED.losses,
           total_xp=rpg_stats.total_xp+EXCLUDED.total_xp,
           updated_at=CURRENT_TIMESTAMP""",
        (user_id, 1 if won else 0, 0 if won else 1, xp),
    )


def _bar(value, maximum, length=12):
    filled = max(0, min(length, int(value * length / max(1, maximum))))
    return "🟩" * filled + "⬛" * (length - filled)


def _dungeon_keyboard(user_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⚔️ ATTACK", callback_data=f"rpgx:attack:{user_id}"),
        InlineKeyboardButton("🏃 Retreat", callback_data=f"rpgx:retreat:{user_id}"),
    ]])


async def dungeon_command(update, context):
    user = update.effective_user
    if not user:
        return
    with db() as conn:
        active = conn.execute("SELECT * FROM rpg_ultimate_runs WHERE user_id=?", (user.id,)).fetchone()
        if active:
            await update.effective_message.reply_text(
                f"🏰 DUNGEON FLOOR {int(active['floor'])}\n"
                f"👹 {active['enemy_name']} • HP {int(active['enemy_hp']):,}\n"
                f"❤️ Your HP: {int(active['player_hp']):,}\n"
                "တိုက်ခိုက်မှု ဆက်လုပ်ပါ။",
                reply_markup=_dungeon_keyboard(user.id),
            )
            return
        coins = _wallet(conn, user.id)
        entry = 1500
        if coins < entry:
            await update.effective_message.reply_text(f"🪙 Dungeon ဝင်ကြေး {entry:,} Coin လိုပါတယ်။ လက်ကျန် {coins:,} Coin")
            return
        conn.execute("UPDATE game_wallets SET coins=coins-? WHERE user_id=? AND coins>=?", (entry,user.id,entry))
        _sync_legacy(conn, user.id, coins-entry)
        level = _level(conn, user.id)
        enemy = random.choice(["🦇 Cave Wraith", "🗿 Stone Golem", "🐺 Shadow Wolf", "🦂 Venom Scorpion"])
        hp = 100 + level * 8
        enemy_hp = 75 + level * 12
        conn.execute(
            "INSERT INTO rpg_ultimate_runs(user_id,enemy_name,player_hp,enemy_hp,floor,started_at) VALUES(?,?,?,?,1,?)",
            (user.id,enemy,hp,enemy_hp,time.time()),
        )
    await update.effective_message.reply_text(
        "╭━━ 🏰 MYAE DUNGEON RAID ━━╮\n"
        f"👹 Enemy: {enemy}\n"
        f"❤️ Your HP: {hp:,}\n"
        f"💀 Enemy HP: {enemy_hp:,}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚔️ တိုက်ခိုက်ပြီး Dungeon ကို ရှင်းလင်းပါ။\n"
        f"🎟️ Entry: {entry:,} Coin",
        reply_markup=_dungeon_keyboard(),
    )


async def summon_command(update, context):
    user = update.effective_user
    if not user:
        return
    with db() as conn:
        coins = _wallet(conn, user.id)
        if coins < SUMMON_COST:
            await update.effective_message.reply_text(
                f"🌟 HERO SUMMON\nလိုအပ်သော Coin: {SUMMON_COST:,}\nလက်ကျန်: {coins:,}"
            )
            return
        conn.execute("UPDATE game_wallets SET coins=coins-? WHERE user_id=? AND coins>=?", (SUMMON_COST,user.id,SUMMON_COST))
        _sync_legacy(conn, user.id, coins-SUMMON_COST)
        hero_id = random.choices(
            ["forest_guardian","ember_knight","moon_archer","storm_mage","frost_assassin","abyss_dragon"],
            weights=[42,25,18,9,5,1], k=1
        )[0]
        hero_name, rarity, power = HEROES[hero_id]
        conn.execute(
            """INSERT INTO rpg_hero_inventory(user_id,hero_id,quantity) VALUES(?,?,1)
               ON CONFLICT(user_id,hero_id) DO UPDATE SET quantity=rpg_hero_inventory.quantity+1""",
            (user.id,hero_id),
        )
        conn.execute(
            """INSERT INTO rpg_ultimate_stats(user_id,summons) VALUES(?,1)
               ON CONFLICT(user_id) DO UPDATE SET summons=rpg_ultimate_stats.summons+1""",
            (user.id,),
        )
        balance = coins-SUMMON_COST
    await update.effective_message.reply_text(
        "✨╭━━ HERO SUMMON ━━╮✨\n"
        "🎴 NEW HERO ACQUIRED!\n"
        f"{hero_name}\n"
        f"💠 Rarity: {rarity}\n"
        f"⚔️ Power: +{power}\n"
        f"🪙 Cost: {SUMMON_COST:,} Coin\n"
        f"💰 Balance: {balance:,} Coin\n"
        "╰━━━━━━━━━━━━━━╯",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎒 My Heroes", callback_data="rpgx:heroes"),
            InlineKeyboardButton("🔮 Summon Again", callback_data="rpgx:summon"),
        ]]),
    )


async def heroes_command(update, context):
    user = update.effective_user
    if not user:
        return
    with db() as conn:
        rows = conn.execute(
            "SELECT hero_id,quantity FROM rpg_hero_inventory WHERE user_id=? ORDER BY quantity DESC,hero_id",
            (user.id,),
        ).fetchall()
    if not rows:
        text = "🎒 HERO COLLECTION\n\nHero မရှိသေးပါ။ /summon ဖြင့် Hero စုဆောင်းပါ။"
    else:
        lines = ["🎒 MY HERO COLLECTION", "━━━━━━━━━━━━━━━━━━━━"]
        for row in rows:
            name, rarity, power = HEROES.get(row["hero_id"], ("Unknown Hero","Common",0))
            lines.append(f"{name}\n💠 {rarity} • ⚔️ {power} Power • ×{int(row['quantity'])}")
        text = "\n".join(lines)
    await update.effective_message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔮 Summon Hero", callback_data="rpgx:summon"),
            InlineKeyboardButton("🏠 RPG Menu", callback_data="rpg:profile"),
        ]]),
    )


async def treasure_command(update, context):
    user = update.effective_user
    if not user:
        return
    fee = 700
    with db() as conn:
        coins = _wallet(conn, user.id)
        if coins < fee:
            await update.effective_message.reply_text(f"💎 Treasure Expedition ဝင်ကြေး {fee:,} Coin လိုပါတယ်။")
            return
        conn.execute("UPDATE game_wallets SET coins=coins-? WHERE user_id=? AND coins>=?", (fee,user.id,fee))
        _sync_legacy(conn, user.id, coins-fee)
        conn.execute(
            "INSERT INTO rpg_treasure_runs(user_id,created_at) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET created_at=EXCLUDED.created_at",
            (user.id,time.time()),
        )
        conn.execute(
            """INSERT INTO rpg_ultimate_stats(user_id,treasure_runs) VALUES(?,1)
               ON CONFLICT(user_id) DO UPDATE SET treasure_runs=rpg_ultimate_stats.treasure_runs+1""",
            (user.id,),
        )
    await update.effective_message.reply_text(
        "🗺️╭━━ TREASURE EXPEDITION ━━╮\n"
        "ကျွန်းပေါ်မှာ ရတနာသေတ္တာ ၃ လုံး တွေ့ပြီ!\n"
        "တစ်လုံးထဲမှာ ရတနာကြီးရှိတယ်။ တစ်လုံးမှာ ထောင်ချောက်ရှိနိုင်တယ်။\n"
        "ဘယ်သေတ္တာကို ဖွင့်မလဲ?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📦 A", callback_data=f"rpgx:treasure:{user.id}:a"),
            InlineKeyboardButton("🎁 B", callback_data=f"rpgx:treasure:{user.id}:b"),
            InlineKeyboardButton("💎 C", callback_data=f"rpgx:treasure:{user.id}:c"),
        ]]),
    )


async def rpg_ultimate_callback(update, context):
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    await query.answer()
    action = (query.data or "").partition(":")[2]
    parts = action.split(":")
    action_name = parts[0]

    if action_name == "menu":
        await query.edit_message_text(
            "🌌╭━━ MYAE ULTIMATE RPG ━━╮\\n"
            "⚔️ Dungeon Raid • 🌟 Hero Summon\\n"
            "💎 Treasure Expedition • 🎒 Hero Collection\\n"
            "━━━━━━━━━━━━━━━━━━━━\\n"
            "ကစားနည်းတစ်ခုကို ရွေးချယ်ပါ။",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏰 Dungeon Raid", callback_data="rpgx:dungeon")],
                [InlineKeyboardButton("🌟 Hero Summon", callback_data="rpgx:summon"),
                 InlineKeyboardButton("🎒 My Heroes", callback_data="rpgx:heroes")],
                [InlineKeyboardButton("💎 Treasure Hunt", callback_data="rpgx:treasure")],
                [InlineKeyboardButton("🏠 RPG Profile", callback_data="rpg:profile")],
            ]),
        )
        return
    if action_name == "summon":
        await summon_command(update, context)
        return
    if action_name == "heroes":
        await heroes_command(update, context)
        return
    if action_name == "dungeon":
        await dungeon_command(update, context)
        return
    if action_name == "treasure":
        await treasure_command(update, context)
        return

    if action_name == "treasure" and len(parts) == 3:
        try:
            owner_id = int(parts[1])
        except ValueError:
            return
        chest = parts[2]
        if owner_id != user.id or chest not in {"a", "b", "c"}:
            return
        with db() as conn:
            consumed = conn.execute(
                "DELETE FROM rpg_treasure_runs WHERE user_id=? AND created_at>=? RETURNING user_id",
                (user.id, time.time()-600),
            ).fetchone()
        if not consumed:
            await query.edit_message_text("⌛ ဒီ Treasure ရွေးချယ်မှုကို အသုံးပြုပြီးပါပြီ သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ။")
            return
        prize = random.choices([0,500,1200,2500,6000], weights=[18,38,27,14,3], k=1)[0]
        with db() as conn:
            conn.execute("UPDATE game_wallets SET coins=coins+? WHERE user_id=?", (prize,user.id))
            row = conn.execute("SELECT coins FROM game_wallets WHERE user_id=?", (user.id,)).fetchone()
            balance = int(row["coins"]) if row else 0
            _sync_legacy(conn, user.id, balance)
            _add_progress(conn,user.id,8,prize>0)
        result = "🪤 ထောင်ချောက်မိသွားတယ်။" if prize == 0 else f"💎 ရတနာတွေ့ပြီ! +{prize:,} Coin"
        await query.edit_message_text(
            f"🗺️ TREASURE RESULT\n📦 Chest {chest.upper()}\n{result}\n🪙 Balance: {balance:,} Coin\n✨ +8 RPG XP",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔁 ထပ်ရှာမယ်", callback_data="rpgx:treasure"),
                InlineKeyboardButton("🏠 RPG Menu", callback_data="rpg:profile"),
            ]]),
        )
        return

    if action_name == "retreat" and len(parts) == 2 and parts[1].isdigit():
        if int(parts[1]) != user.id:
            return
        with db() as conn:
            conn.execute("DELETE FROM rpg_ultimate_runs WHERE user_id=?", (user.id,))
        await query.edit_message_text("🏃 Dungeon မှ ဆုတ်ခွာပြီးပါပြီ။ Entry fee ပြန်မရပါ။")
        return

    if action_name == "attack" and len(parts) == 2 and parts[1].isdigit():
        if int(parts[1]) != user.id:
            return
        with db() as conn:
            run = conn.execute("SELECT * FROM rpg_ultimate_runs WHERE user_id=?", (user.id,)).fetchone()
            if not run:
                await query.edit_message_text("Dungeon Run မရှိတော့ပါ။ /dungeon ဖြင့် အသစ်စပါ။")
                return
            level = _level(conn,user.id)
            hero_power = conn.execute(
                """SELECT COALESCE(SUM(h.quantity * CASE h.hero_id
                   WHEN 'abyss_dragon' THEN 35 WHEN 'frost_assassin' THEN 25
                   WHEN 'storm_mage' THEN 24 WHEN 'ember_knight' THEN 18
                   WHEN 'moon_archer' THEN 16 ELSE 12 END),0) AS power
                   FROM rpg_hero_inventory h WHERE h.user_id=?""", (user.id,)
            ).fetchone()
            attack = random.randint(22,36) + level*2 + int(hero_power["power"])//5
            remaining_enemy = max(0,int(run["enemy_hp"])-attack)
            counter = random.randint(7,16) + level
            remaining_player = max(0,int(run["player_hp"])-counter) if remaining_enemy else int(run["player_hp"])
            if remaining_enemy <= 0:
                reward = 2200 + int(run["floor"])*350 + level*25
                conn.execute("DELETE FROM rpg_ultimate_runs WHERE user_id=?", (user.id,))
                conn.execute("UPDATE game_wallets SET coins=coins+? WHERE user_id=?", (reward,user.id))
                balance = _wallet(conn,user.id)
                _sync_legacy(conn,user.id,balance)
                _add_progress(conn,user.id,35,True)
                conn.execute(
                    """INSERT INTO rpg_ultimate_stats(user_id,dungeon_wins) VALUES(?,1)
                       ON CONFLICT(user_id) DO UPDATE SET dungeon_wins=rpg_ultimate_stats.dungeon_wins+1""",
                    (user.id,),
                )
                result = (
                    "🏆 DUNGEON CLEARED!\n"
                    f"💥 Final Damage: {attack}\n🪙 Reward: +{reward:,} Coin\n"
                    f"✨ RPG XP: +35\n💰 Balance: {balance:,} Coin"
                )
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔁 Next Dungeon", callback_data="rpgx:dungeon"),
                    InlineKeyboardButton("🏠 RPG Menu", callback_data="rpg:profile"),
                ]])
            elif remaining_player <= 0:
                conn.execute("DELETE FROM rpg_ultimate_runs WHERE user_id=?", (user.id,))
                _add_progress(conn,user.id,5,False)
                result = f"💀 YOU WERE DEFEATED\n💥 Damage: {attack}\n👹 Enemy HP left: {remaining_enemy}\n✨ Consolation XP: +5"
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔁 Retry Dungeon", callback_data="rpgx:dungeon"),
                    InlineKeyboardButton("🏠 RPG Menu", callback_data="rpg:profile"),
                ]])
            else:
                conn.execute(
                    "UPDATE rpg_ultimate_runs SET player_hp=?,enemy_hp=? WHERE user_id=?",
                    (remaining_player,remaining_enemy,user.id),
                )
                result = (
                    f"⚔️ HIT! -{attack} Enemy HP\n"
                    f"👹 {run['enemy_name']}\n"
                    f"❤️ Your HP [{_bar(remaining_player,int(run['player_hp']))}] {remaining_player:,}\n"
                    f"💀 Enemy HP [{_bar(remaining_enemy,int(run['enemy_hp']))}] {remaining_enemy:,}\n"
                    f"🩸 Counter Attack: -{counter} HP"
                )
                keyboard = _dungeon_keyboard(user.id)
        await query.edit_message_text("🏰 MYAE DUNGEON RAID\n━━━━━━━━━━━━━━━━━━━━\n"+result, reply_markup=keyboard)


async def ultimate_menu_command(update, context):
    await update.effective_message.reply_text(
        "🌌╭━━ MYAE ULTIMATE RPG ━━╮\n"
        "⚔️ Dungeon Raid • 🌟 Hero Summon\n"
        "💎 Treasure Expedition • 🎒 Hero Collection\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "ကစားရန် အောက်က Menu ကိုနှိပ်ပါ။",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏰 Dungeon Raid",callback_data="rpgx:dungeon")],
            [InlineKeyboardButton("🌟 Hero Summon",callback_data="rpgx:summon"),
             InlineKeyboardButton("🎒 My Heroes",callback_data="rpgx:heroes")],
            [InlineKeyboardButton("💎 Treasure Hunt",callback_data="rpgx:treasure")],
            [InlineKeyboardButton("🏠 RPG Profile",callback_data="rpg:profile")],
        ]),
    )
