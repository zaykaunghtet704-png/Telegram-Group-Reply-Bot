"""Guilds, shared World Boss raids and RPG equipment combat."""
import random
import time
from datetime import date

from database import connect as db

GEAR = {
    "flame_sword": {"name": "Flame Sword", "emoji": "🔥", "slot": "weapon", "price": 20000, "power": 12},
    "dragon_armor": {"name": "Dragon Armor", "emoji": "🛡️", "slot": "armor", "price": 18000, "power": 10},
    "phoenix_pet": {"name": "Phoenix Pet", "emoji": "🦅", "slot": "pet", "price": 30000, "power": 15},
    "shadow_blade": {"name": "Shadow Blade", "emoji": "⚔️", "slot": "weapon", "price": 50000, "power": 25},
}

def init_rpg_expansion():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS rpg_guilds (
            guild_id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            owner_id BIGINT NOT NULL, total_xp BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS rpg_guild_members (
            user_id BIGINT PRIMARY KEY,
            guild_id BIGINT NOT NULL REFERENCES rpg_guilds(guild_id) ON DELETE CASCADE,
            joined_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rpg_guild_members_guild ON rpg_guild_members(guild_id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS rpg_world_boss (
            boss_id BIGINT PRIMARY KEY, boss_date DATE NOT NULL, hp BIGINT NOT NULL,
            max_hp BIGINT NOT NULL, defeated BIGINT NOT NULL DEFAULT 0,
            total_damage BIGINT NOT NULL DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS rpg_boss_cooldowns (
            user_id BIGINT PRIMARY KEY, last_attack DOUBLE PRECISION NOT NULL DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS rpg_boss_damage (
            user_id BIGINT NOT NULL, boss_date DATE NOT NULL, damage BIGINT NOT NULL DEFAULT 0,
            attacks BIGINT NOT NULL DEFAULT 0, PRIMARY KEY(user_id,boss_date))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS rpg_equipment (
            user_id BIGINT NOT NULL, item_id TEXT NOT NULL, quantity BIGINT NOT NULL DEFAULT 1,
            PRIMARY KEY(user_id,item_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS rpg_equipment_loadout (
            user_id BIGINT NOT NULL, slot TEXT NOT NULL, item_id TEXT NOT NULL,
            PRIMARY KEY(user_id,slot))""")

async def guild_create_command(update, context):
    user = update.effective_user
    name = " ".join(context.args).strip()[:32] if context.args else ""
    if not user: return
    if len(name) < 3:
        await update.effective_message.reply_text("အသုံးပြုပုံ: /guildcreate GuildName (အနည်းဆုံး ၃ လုံး)")
        return
    with db() as conn:
        if conn.execute("SELECT 1 FROM rpg_guild_members WHERE user_id=?", (user.id,)).fetchone():
            await update.effective_message.reply_text("Guild တစ်ခုထဲမှာ ရှိပြီးသားပါ။ /guild")
            return
        if conn.execute("SELECT 1 FROM rpg_guilds WHERE LOWER(name)=LOWER(?)", (name,)).fetchone():
            await update.effective_message.reply_text("ဒီ Guild အမည်ကို အသုံးပြုပြီးသားပါ။")
            return
        row = conn.execute("INSERT INTO rpg_guilds(name,owner_id) VALUES(?,?) RETURNING guild_id", (name,user.id)).fetchone()
        conn.execute("INSERT INTO rpg_guild_members(user_id,guild_id) VALUES(?,?)", (user.id,row["guild_id"]))
    await update.effective_message.reply_text(f"🛡️ GUILD CREATED\n🏰 {name}\n🆔 ID: {int(row['guild_id'])}\nဝင်ရန် /guildjoin {int(row['guild_id'])}")

async def guild_join_command(update, context):
    user = update.effective_user
    if not user: return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("အသုံးပြုပုံ: /guildjoin GUILD_ID"); return
    gid = int(context.args[0])
    with db() as conn:
        if conn.execute("SELECT 1 FROM rpg_guild_members WHERE user_id=?", (user.id,)).fetchone():
            await update.effective_message.reply_text("Guild တစ်ခုထဲမှာ ရှိပြီးသားပါ။ /guildleave"); return
        guild = conn.execute("SELECT name FROM rpg_guilds WHERE guild_id=?", (gid,)).fetchone()
        if not guild:
            await update.effective_message.reply_text("Guild ID မတွေ့ပါ။ /guildtop"); return
        count = conn.execute("SELECT COUNT(*) AS n FROM rpg_guild_members WHERE guild_id=?", (gid,)).fetchone()
        if int(count["n"]) >= 30:
            await update.effective_message.reply_text("Guild အဖွဲ့ဝင် ၃၀ ပြည့်နေပါပြီ။"); return
        conn.execute("INSERT INTO rpg_guild_members(user_id,guild_id) VALUES(?,?)", (user.id,gid))
    await update.effective_message.reply_text(f"✅ {guild['name']} Guild ထဲ ဝင်ပြီးပါပြီ။")

async def guild_leave_command(update, context):
    user = update.effective_user
    if not user: return
    with db() as conn:
        row = conn.execute("SELECT g.name,g.owner_id FROM rpg_guild_members m JOIN rpg_guilds g USING(guild_id) WHERE m.user_id=?", (user.id,)).fetchone()
        if not row:
            await update.effective_message.reply_text("Guild ထဲမှာ မရှိသေးပါ။"); return
        if int(row["owner_id"]) == user.id:
            await update.effective_message.reply_text("Guild Owner က မထွက်နိုင်ပါ။ အဖွဲ့ဝင်တွေကို အရင်စီမံပါ။"); return
        conn.execute("DELETE FROM rpg_guild_members WHERE user_id=?", (user.id,))
    await update.effective_message.reply_text(f"👋 {row['name']} Guild မှ ထွက်ပြီးပါပြီ။")

async def guild_command(update, context):
    user = update.effective_user
    if not user: return
    with db() as conn:
        row = conn.execute("""SELECT g.guild_id,g.name,g.owner_id,g.total_xp,COUNT(m.user_id) AS members
            FROM rpg_guild_members mine JOIN rpg_guilds g USING(guild_id)
            JOIN rpg_guild_members m ON m.guild_id=g.guild_id
            WHERE mine.user_id=? GROUP BY g.guild_id""", (user.id,)).fetchone()
    if not row:
        await update.effective_message.reply_text("Guild မရှိသေးပါ။ /guildcreate NAME သို့မဟုတ် /guildtop"); return
    await update.effective_message.reply_text(
        f"🛡️ GUILD: {row['name']}\n🆔 ID: {int(row['guild_id'])}\n👑 Owner ID: {int(row['owner_id'])}\n"
        f"👥 Members: {int(row['members'])}/30\n✨ Guild XP: {int(row['total_xp']):,}\n/guildleave")

async def guild_top_command(update, context):
    with db() as conn:
        rows = conn.execute("""SELECT g.guild_id,g.name,g.total_xp,COUNT(m.user_id) AS members
            FROM rpg_guilds g LEFT JOIN rpg_guild_members m USING(guild_id)
            GROUP BY g.guild_id ORDER BY g.total_xp DESC,g.guild_id LIMIT 10""").fetchall()
    if not rows:
        await update.effective_message.reply_text("Guild မရှိသေးပါ။ /guildcreate NAME"); return
    lines = ["🏆 MYAE GUILD TOP 10","━━━━━━━━━━━━━━━━━━━━"]
    for i,row in enumerate(rows,1):
        lines.append(f"{i}. {row['name']} (ID {int(row['guild_id'])}) • 👥 {int(row['members'])} • ✨ {int(row['total_xp']):,} XP")
    await update.effective_message.reply_text("\n".join(lines))

async def gear_command(update, context):
    lines = ["⚔️ MYAE EQUIPMENT FORGE","━━━━━━━━━━━━━━━━━━━━"]
    for key,item in GEAR.items():
        lines.append(f"{item['emoji']} {item['name']} • +{item['power']} Power\nID: {key} • {item['price']:,} Coin")
    lines.append("ဝယ်/တပ်ရန် /gearequip ITEM_ID (မပိုင်သေးလျှင် အရင်ဝယ်ရန် /gearbuy ITEM_ID)")
    await update.effective_message.reply_text("\n".join(lines))

async def gear_buy_command(update, context):
    user = update.effective_user
    item_id = context.args[0] if context.args else ""
    item = GEAR.get(item_id)
    if not user: return
    if not item:
        await update.effective_message.reply_text("Item ID မမှန်ပါ။ /gear"); return
    with db() as conn:
        conn.execute("INSERT INTO game_wallets(user_id,coins) VALUES(?,100) ON CONFLICT(user_id) DO NOTHING",(user.id,))
        debit = conn.execute("UPDATE game_wallets SET coins=coins-? WHERE user_id=? AND coins>=?",(item["price"],user.id,item["price"]))
        if debit.rowcount != 1:
            await update.effective_message.reply_text("Coin မလုံလောက်ပါ။ /balance"); return
        conn.execute("""INSERT INTO rpg_equipment(user_id,item_id,quantity) VALUES(?,?,1)
            ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=rpg_equipment.quantity+1""",(user.id,item_id))
        wallet = conn.execute("SELECT coins FROM game_wallets WHERE user_id=?",(user.id,)).fetchone()
        conn.execute("UPDATE game_accounts SET coins=? WHERE user_id=?",(int(wallet["coins"]),user.id))
    await update.effective_message.reply_text(f"✅ {item['name']} ဝယ်ပြီးပါပြီ။\n🪙 လက်ကျန် {int(wallet['coins']):,} Coin\nတပ်ရန် /gearequip {item_id}")

async def gear_equip_command(update, context):
    user = update.effective_user
    item_id = context.args[0] if context.args else ""
    item = GEAR.get(item_id)
    if not user: return
    if not item:
        await update.effective_message.reply_text("အသုံးပြုပုံ: /gearequip ITEM_ID"); return
    with db() as conn:
        owned = conn.execute("SELECT quantity FROM rpg_equipment WHERE user_id=? AND item_id=?",(user.id,item_id)).fetchone()
        if not owned or int(owned["quantity"]) < 1:
            await update.effective_message.reply_text("ဒီ Equipment ကို မပိုင်သေးပါ။ /gearbuy "+item_id); return
        conn.execute("""INSERT INTO rpg_equipment_loadout(user_id,slot,item_id) VALUES(?,?,?)
            ON CONFLICT(user_id,slot) DO UPDATE SET item_id=EXCLUDED.item_id""",(user.id,item["slot"],item_id))
    await update.effective_message.reply_text(f"✨ {item['emoji']} {item['name']} တပ်ဆင်ပြီးပါပြီ။ Combat Power +{item['power']}")

def _gear_power(conn, user_id):
    rows = conn.execute("SELECT item_id FROM rpg_equipment_loadout WHERE user_id=?",(user_id,)).fetchall()
    return sum(GEAR.get(row["item_id"],{}).get("power",0) for row in rows)

async def world_boss_command(update, context):
    user = update.effective_user
    if not user: return
    today = date.today()
    with db() as conn:
        conn.execute("""INSERT INTO rpg_world_boss(boss_id,boss_date,hp,max_hp,defeated,total_damage)
            VALUES(1,?,100000,100000,0,0) ON CONFLICT(boss_id) DO UPDATE SET
            boss_date=EXCLUDED.boss_date,hp=EXCLUDED.hp,max_hp=EXCLUDED.max_hp,defeated=0,total_damage=0
            WHERE rpg_world_boss.boss_date<EXCLUDED.boss_date""",(today,))
        boss = conn.execute("SELECT * FROM rpg_world_boss WHERE boss_id=1").fetchone()
        rows = conn.execute("""SELECT d.damage,COALESCE(a.display_name,'Adventurer') AS display_name
            FROM rpg_boss_damage d LEFT JOIN game_accounts a USING(user_id)
            WHERE d.boss_date=? ORDER BY d.damage DESC LIMIT 3""",(today,)).fetchall()
    pct = int(boss["hp"])*100//max(1,int(boss["max_hp"]))
    lines = ["🐉 MYAE WORLD BOSS","━━━━━━━━━━━━━━━━━━━━","👹 Ancient Shadow Dragon",
        f"❤️ HP: {int(boss['hp']):,}/{int(boss['max_hp']):,}",f"🩸 Remaining: {pct}%",
        f"⚔️ Total Damage: {int(boss['total_damage']):,}","တိုက်ခိုက်ရန် /bossattack"]
    if rows:
        lines += ["","🏅 TOP DAMAGE"]+[f"{i}. {r['display_name']} — {int(r['damage']):,}" for i,r in enumerate(rows,1)]
    if int(boss["defeated"]): lines.append("💀 ဒီနေ့ Boss ကို အနိုင်ယူပြီးပါပြီ။")
    await update.effective_message.reply_text("\n".join(lines))

async def world_boss_attack_command(update, context):
    user = update.effective_user
    if not user: return
    now,today = time.time(),date.today()
    with db() as conn:
        conn.execute("""INSERT INTO rpg_world_boss(boss_id,boss_date,hp,max_hp,defeated,total_damage)
            VALUES(1,?,100000,100000,0,0) ON CONFLICT(boss_id) DO UPDATE SET
            boss_date=EXCLUDED.boss_date,hp=EXCLUDED.hp,max_hp=EXCLUDED.max_hp,defeated=0,total_damage=0
            WHERE rpg_world_boss.boss_date<EXCLUDED.boss_date""",(today,))
        conn.execute("INSERT INTO rpg_boss_cooldowns(user_id,last_attack) VALUES(?,0) ON CONFLICT(user_id) DO NOTHING",(user.id,))
        allowed = conn.execute("UPDATE rpg_boss_cooldowns SET last_attack=? WHERE user_id=? AND last_attack<=? RETURNING user_id",(now,user.id,now-10)).fetchone()
        if not allowed:
            await update.effective_message.reply_text("⏳ နောက်တစ်ကြိမ်တိုက်ရန် ၁၀ စက္ကန့် စောင့်ပါ။"); return
        boss = conn.execute("SELECT * FROM rpg_world_boss WHERE boss_id=1 FOR UPDATE").fetchone()
        if int(boss["defeated"]) or int(boss["hp"]) <= 0:
            await update.effective_message.reply_text("💀 ဒီနေ့ Boss ကို အနိုင်ယူပြီးပါပြီ။"); return
        stats = conn.execute("SELECT total_xp FROM rpg_stats WHERE user_id=?",(user.id,)).fetchone()
        level = int(stats["total_xp"])//100+1 if stats else 1
        damage = random.randint(35,65)+(level*2+_gear_power(conn,user.id))*5
        result = conn.execute("""UPDATE rpg_world_boss SET hp=GREATEST(0,hp-?),
            total_damage=total_damage+?,defeated=CASE WHEN hp-?<=0 THEN 1 ELSE 0 END
            WHERE boss_id=1 RETURNING hp,defeated""",(damage,damage,damage)).fetchone()
        conn.execute("""INSERT INTO rpg_boss_damage(user_id,boss_date,damage,attacks) VALUES(?,?,?,1)
            ON CONFLICT(user_id,boss_date) DO UPDATE SET damage=rpg_boss_damage.damage+EXCLUDED.damage,
            attacks=rpg_boss_damage.attacks+1""",(user.id,today,damage))
        member = conn.execute("SELECT guild_id FROM rpg_guild_members WHERE user_id=?",(user.id,)).fetchone()
        if member: conn.execute("UPDATE rpg_guilds SET total_xp=total_xp+? WHERE guild_id=?",(damage,member["guild_id"]))
        xp = 12
        conn.execute("""INSERT INTO rpg_stats(user_id,games_played,total_xp) VALUES(?,1,?)
            ON CONFLICT(user_id) DO UPDATE SET games_played=rpg_stats.games_played+1,
            total_xp=rpg_stats.total_xp+EXCLUDED.total_xp,updated_at=CURRENT_TIMESTAMP""",(user.id,xp))
        conn.execute("INSERT INTO game_wallets(user_id,coins) VALUES(?,100) ON CONFLICT(user_id) DO NOTHING",(user.id,))
        bonus = 500 if int(result["defeated"]) else 0
        reward = 40+damage//4+bonus
        wallet = conn.execute("UPDATE game_wallets SET coins=coins+? WHERE user_id=? RETURNING coins",(reward,user.id)).fetchone()
        conn.execute("UPDATE game_accounts SET coins=? WHERE user_id=?",(int(wallet["coins"]),user.id))
    await update.effective_message.reply_text(
        f"⚔️ WORLD BOSS ATTACK\n👤 {user.full_name}\n💥 Damage: {damage:,}\n"
        f"🐉 Boss HP: {int(result['hp']):,}/100,000\n✨ RPG XP: +{xp}\n🪙 Reward: +{reward:,} Coin\n"
        + ("🏆 BOSS DEFEATED! FINAL HIT BONUS +500 COIN!" if bonus else "ထပ်တိုက်ရန် /bossattack")
    )
