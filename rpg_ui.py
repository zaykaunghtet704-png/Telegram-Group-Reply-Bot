"""Beginner-friendly inline navigation for the RPG system."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from rpg import (
    rpg_profile, rpg_daily_command, adventure_command, missions_command,
    achievements_command, shop_command, inventory_command, rpg_top_command,
)
from rpg_expansion import (
    guild_command, gear_command, world_boss_command,
)

HELP_TEXT = (
    "🌟 RPG ကို စတင်ကစားနည်း\n\n"
    "1️⃣ 🎁 Daily Reward ကို နေ့တိုင်းယူပါ။\n"
    "2️⃣ ⚔️ Adventure ဝင်ပြီး Monster ကိုတိုက်ပါ။\n"
    "3️⃣ ✨ XP ရလာရင် Level တက်ပါမယ်။\n"
    "4️⃣ 🪙 Coin စုဆောင်းပြီး Gear ဝယ်ပါ။\n"
    "5️⃣ 🐉 World Boss ကို တခြားကစားသမားတွေနဲ့ ဝိုင်းတိုက်ပါ။\n"
    "6️⃣ 🛡 Guild ဖွဲ့ပြီး အသင်းလိုက်ကစားပါ။\n\n"
    "အရင်ဆုံး 🎁 Daily Reward ယူပြီး ⚔️ Adventure ကစားကြည့်ပါ။"
)

async def rpg_menu_callback(update, context):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    action = (query.data or "").split(":", 1)[-1]
    handlers = {
        "profile": rpg_profile,
        "daily": rpg_daily_command,
        "adventure": adventure_command,
        "missions": missions_command,
        "achievements": achievements_command,
        "shop": shop_command,
        "inventory": inventory_command,
        "top": rpg_top_command,
        "boss": world_boss_command,
        "guild": guild_command,
        "gear": gear_command,
    }
    if action == "help":
        await query.message.reply_text(
            HELP_TEXT,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 RPG Menu", callback_data="rpg:profile")
            ]]),
        )
        return
    handler = handlers.get(action)
    if handler:
        await handler(update, context)
