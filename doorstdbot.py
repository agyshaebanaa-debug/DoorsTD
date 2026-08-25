import asyncio
import logging
import json
import os
import random
import time
import io
import urllib.request
import sqlite3
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message, 
    CallbackQuery, 
    ReplyKeyboardMarkup, 
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    BufferedInputFile,
    BotCommand
)
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest

# ==========================================
# НАСТРОЙКА PIL (ДЛЯ ОТРИСОВКИ БОЯ)
# ==========================================
try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    logging.warning("Библиотека Pillow не найдена! Установите её: pip install Pillow")
    HAS_PIL = False

FONT_FILE = "bot_font.ttf"
FONT_URL = "https://github.com/googlefonts/roboto/raw/main/src/hinted/Roboto-Bold.ttf"

if HAS_PIL and not os.path.exists(FONT_FILE):
    try:
        urllib.request.urlretrieve(FONT_URL, FONT_FILE)
    except Exception as e:
        logging.error(f"Не удалось скачать шрифт: {e}")

# ==========================================
# 1. КОНФИГУРАЦИЯ БОТА И БД
# ==========================================
TOKEN = "8937370112:AAEH_2CFYFcP1w_9hukUavQbhP8-jBWYYPw"
MAIN_ADMIN_ID = "5341904332"

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "game_data.db")

admins_db = {MAIN_ADMIN_ID}
rarities_db = [] 
units_db = {}    
unit_id_counter = 1
mobs_db = {}
mob_id_counter = 1
user_inventory = {}  
user_equipped = {}   
user_balances = {} 
user_free_crate_times = {} 
currencies_db = ["💰 Монеты", "💎 Гемы"] 
maps_db = {}       
map_id_counter = 1
crates_db = {}     
crate_id_counter = 1

bot_settings = {
    "coins_per_damage": 0.5,
    "turn_time_skip": 5,
    "turn_time_noskip": 10
}

ATTACK_TYPES = ["Одиночный", "Сплеш", "АОЕ", "Саппорт", "Ферма", "Замедление", "Оглушение", "Горение"]
UNIT_TARGET_TYPES = ["Наземный", "Анти-Воздух", "Детектор", "Универсал"]
MOB_TRAITS = ["Обычный", "Летающий", "Камуфляж"]

elevators_db = {}
elevator_id_counter = 1
active_battles = {}
battle_id_counter = 1
user_to_battle = {}
active_tasks = {}
panel_owners = {} 
image_cache = {}

# ==========================================
# 2. СИСТЕМА SQLITE
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS storage (key TEXT PRIMARY KEY, data TEXT)")
    conn.commit()
    conn.close()

def db_get(key, default=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT data FROM storage WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return json.loads(row[0]) if row else default

def db_set(key, data):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO storage (key, data) VALUES (?, ?)", (key, json.dumps(data, ensure_ascii=False)))
    conn.commit()
    conn.close()

def load_data():
    global admins_db, rarities_db, units_db, unit_id_counter, user_inventory, user_equipped
    global mobs_db, mob_id_counter, currencies_db, maps_db, map_id_counter
    global user_balances, crates_db, crate_id_counter, user_free_crate_times, bot_settings
    
    init_db()
    data = db_get("full_state", {})
    if not data: return
        
    try:
        admins_db = set(data.get("admins_db", [MAIN_ADMIN_ID]))
        if MAIN_ADMIN_ID not in admins_db: admins_db.add(MAIN_ADMIN_ID)
        rarities_db = data.get("rarities_db", [])
        
        loaded_settings = data.get("bot_settings", {})
        bot_settings["coins_per_damage"] = loaded_settings.get("coins_per_damage", 0.5)
        bot_settings["turn_time_skip"] = loaded_settings.get("turn_time_skip", 5)
        bot_settings["turn_time_noskip"] = loaded_settings.get("turn_time_noskip", 10)
        
        units_db = data.get("units_db", {})
        unit_id_counter = data.get("unit_id_counter", 1)
        mobs_db = data.get("mobs_db", {})
        mob_id_counter = data.get("mob_id_counter", 1)
        
        currencies_db = data.get("currencies_db", ["💰 Монеты", "💎 Гемы"])
        if "💰 Монеты" not in currencies_db: currencies_db.insert(0, "💰 Монеты")
        
        maps_db = data.get("maps_db", {})
        map_id_counter = data.get("map_id_counter", 1)
        crates_db = data.get("crates_db", {})
        crate_id_counter = data.get("crate_id_counter", 1)
        
        user_inventory = {str(k): set(v) for k, v in data.get("user_inventory", {}).items()}
        user_equipped = {str(k): list(v) for k, v in data.get("user_equipped", {}).items()}
        user_balances = data.get("user_balances", {})
        user_free_crate_times = {str(k): float(v) for k, v in data.get("user_free_crate_times", {}).items()}
        
    except Exception as e:
        logging.error(f"⚠️ Ошибка загрузки из SQLite: {e}")

def save_data():
    data = {
        "admins_db": list(admins_db),
        "rarities_db": rarities_db,
        "units_db": units_db,
        "unit_id_counter": unit_id_counter,
        "mobs_db": mobs_db,
        "mob_id_counter": mob_id_counter,
        "currencies_db": currencies_db,
        "maps_db": maps_db,
        "map_id_counter": map_id_counter,
        "crates_db": crates_db,
        "crate_id_counter": crate_id_counter,
        "user_inventory": {str(k): list(v) for k, v in user_inventory.items()},
        "user_equipped": {str(k): list(v) for k, v in user_equipped.items()},
        "user_balances": user_balances,
        "user_free_crate_times": user_free_crate_times,
        "bot_settings": bot_settings
    }
    db_set("full_state", data)

# ==========================================
# 3. ЛОГИКА ЮНИТОВ, ТАРГЕТИНГА И СТАТИСТИКИ
# ==========================================
def can_hit(unit_target: str, mob_trait: str) -> bool:
    if unit_target == "Универсал": return True
    if mob_trait == "Обычный": return True
    if unit_target == "Анти-Воздух" and mob_trait == "Летающий": return True
    if unit_target == "Детектор" and mob_trait == "Камуфляж": return True
    return False

def get_unit_stats(uid: str, is_shiny: bool = False) -> dict | None:
    u = units_db.get(str(uid))
    if not u: return None
    if not is_shiny: return u
    
    su = u.copy()
    su["name"] = f"✨ {su.get('name', f'Юнит №{uid}')} ✨"
    if "damage" in su: su["damage"] = round(su["damage"] * 1.25, 2)
    if "income" in su: su["income"] = int(su["income"] * 1.10)
    if "cd_boost" in su: su["cd_boost"] = round(su["cd_boost"] * 0.90, 2)
    if "dmg_boost" in su: su["dmg_boost"] = round(su["dmg_boost"] * 1.10, 2)
    if "deploy_cost" in su: su["deploy_cost"] = int(su["deploy_cost"] * 1.20)
    if "slow_percent" in su: su["slow_percent"] += 5
    if "slow_duration" in su: su["slow_duration"] += 2
    if "burn_damage" in su: su["burn_damage"] = round(su["burn_damage"] * 1.25, 2)
    if "stun_chance" in su: su["stun_chance"] = min(100, su["stun_chance"] + 5)
    return su

def format_unit_stats(u):
    utypes = u.get("unit_types", [])
    types_str = ", ".join(utypes) if utypes else "Нет класса"
    target_str = u.get("target_type", "Наземный")
    
    res = f"├ 🏷 Классы: <b>{types_str}</b>\n├ 🎯 Цели: <b>{target_str}</b>\n├ 💰 Цена: {u.get('deploy_cost', 50)} | 🛑 Лимит: {u.get('supply_limit', '∞')}\n"
    
    if any(t in utypes for t in ["Одиночный", "Сплеш", "АОЕ"]):
        res += f"├ ⚔️ Атака: 💥 {u.get('damage', 10)} | ⏱ КД: {u.get('cd', 1.0)}с\n"
    if "Саппорт" in utypes:
        res += f"├ ✨ Саппорт: ⏱ КД x{u.get('cd_boost', 1.0)} | 💥 Урон x{u.get('dmg_boost', 1.0)}\n"
    if "Ферма" in utypes:
        res += f"├ 🌾 Ферма: 💰 +{u.get('income', 0)}/волна\n"
    if "Замедление" in utypes:
        res += f"├ ❄️ Замедление: -{u.get('slow_percent', 20)}% скорости | ⏳ {u.get('slow_duration', 5)}с (КД: {u.get('slow_cd', 15)}с)\n"
    if "Оглушение" in utypes:
        res += f"├ ⚡ Оглушение: {u.get('stun_chance', 10)}% шанс | ⏳ {u.get('stun_duration', 1)} х.\n"
    if "Горение" in utypes:
        res += f"├ 🔥 Горение: {u.get('burn_chance', 20)}% шанс | 💥 {u.get('burn_damage', 5)}/х. | ⏳ {u.get('burn_duration', 3)} х.\n"
        
    res += "└──────────────────"
    return res

# ==========================================
# MIDDLEWARE И СОСТОЯНИЯ
# ==========================================
class PanelMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery) and event.message:
            if event.message.chat.type in {"group", "supergroup"}:
                public_cb = ["el_", "b_dep_", "b_toggle_", "b_surr_", "lobby_"]
                if not any(event.data.startswith(p) for p in public_cb):
                    key = f"{event.message.chat.id}_{event.message.message_id}"
                    if key in panel_owners and panel_owners[key] != event.from_user.id:
                        await event.answer("🚫 Это меню вызвал другой игрок! Напишите /panel", show_alert=True)
                        return
        return await handler(event, data)

class AdminGiveCur(StatesGroup):
    select_cur = State()
    target_id = State()
    amount = State()

class AdminMapAdd(StatesGroup):
    name = State()
    photo = State()
    wave_builder = State()
    waiting_mob_count = State()
    waiting_wave_turns = State()
    waiting_reward_coins = State()
    waiting_reward_gems = State()

class AdminRarityAdd(StatesGroup):
    waiting_for_name = State()

class AdminUnitAdd(StatesGroup):
    unit_types = State()
    target_type = State()
    photo = State()
    name = State()
    rarity = State()
    supply_limit = State()
    deploy_cost = State()
    cd = State()
    damage = State()
    cd_boost = State()
    dmg_boost = State()
    income = State()
    slow_percent = State()
    slow_duration = State()
    slow_cd = State()
    stun_chance = State()
    stun_duration = State()
    burn_chance = State()
    burn_damage = State()
    burn_duration = State()

class AdminMobAdd(StatesGroup):
    name = State()
    photo = State()
    hp = State()
    trait = State()
    effect = State()
    defense_percent = State()

class AdminCrateAdd(StatesGroup):
    name = State()
    price = State()
    photo = State()
    unit_builder = State()
    waiting_unit_weight = State()

class AdminSettingsEdit(StatesGroup):
    waiting_for_coins_per_damage = State()
    waiting_for_turn_time_skip = State()
    waiting_for_turn_time_noskip = State()

class AdminCurAdd(StatesGroup):
    name = State()

class AdminManage(StatesGroup):
    add_id = State()

class AdminEditGeneric(StatesGroup):
    waiting_for_value = State()

# ==========================================
# UI КЛАВИАТУРЫ
# ==========================================
reply_bottom_menu = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🔙 В Главное Меню")]],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Управление в меню"
)

def get_main_menu_kb(chat_type: str = "private") -> InlineKeyboardMarkup:
    kb = []
    if maps_db: kb.append([InlineKeyboardButton(text="⚔️ ИГРАТЬ (Создать Лобби) ⚔️", callback_data="battle_select_map")])
    if crates_db: kb.append([InlineKeyboardButton(text="📦 Магазин Крейтов 📦", callback_data="crates_list")])
    if chat_type in {"group", "supergroup"}:
        kb.append([InlineKeyboardButton(text="🎁 Бесплатный Крейт 🎁", callback_data="free_crate")])
        
    kb.append([
        InlineKeyboardButton(text="📖 Энциклопедия", callback_data="main_index"), 
        InlineKeyboardButton(text="🎒 Мой Инвентарь", callback_data="main_inventory")
    ])
    kb.append([InlineKeyboardButton(text="⚙️ Админ панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Юнит", callback_data="admin_add_unit"), InlineKeyboardButton(text="➖ Удал. Юнит", callback_data="admin_del_unit")],
        [InlineKeyboardButton(text="✏️ Изменить Юнит", callback_data="admin_edit_unit_list")],
        [InlineKeyboardButton(text="👾 Доб. Моба", callback_data="admin_add_mob"), InlineKeyboardButton(text="👾 Удал. Моба", callback_data="admin_del_mob")],
        [InlineKeyboardButton(text="✏️ Изменить Моба", callback_data="admin_edit_mob_list")],
        [InlineKeyboardButton(text="🗺 Доб. Карту", callback_data="admin_add_map"), InlineKeyboardButton(text="🗺 Удал. Карту", callback_data="admin_del_map")],
        [InlineKeyboardButton(text="✏️ Изменить Карту", callback_data="admin_edit_map_list")],
        [InlineKeyboardButton(text="📦 Доб. Крейт", callback_data="admin_add_crate"), InlineKeyboardButton(text="📦 Удал. Крейт", callback_data="admin_del_crate")],
        [InlineKeyboardButton(text="✏️ Изменить Крейт", callback_data="admin_edit_crate_list")],
        [InlineKeyboardButton(text="🪙 Доб. Валюту", callback_data="admin_add_cur"), InlineKeyboardButton(text="🪙 Удал. Валюту", callback_data="admin_del_cur")],
        [InlineKeyboardButton(text="💸 Выдать Валюту", callback_data="admin_give_cur")],
        [InlineKeyboardButton(text="✨ Доб. Редкость", callback_data="admin_add_rarity"), InlineKeyboardButton(text="✨ Удал. Редкость", callback_data="admin_del_rarity")],
        [InlineKeyboardButton(text="👨‍💻 Назначить Адм", callback_data="admin_add"), InlineKeyboardButton(text="🚫 Снять Адм", callback_data="admin_remove")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin_settings"), InlineKeyboardButton(text="💾 Бэкап БД", callback_data="admin_backup_db")]
    ])

def get_unit_types_kb(selected: list) -> InlineKeyboardMarkup:
    kb = []
    for t in ATTACK_TYPES:
        mark = "✅" if t in selected else "❌"
        kb.append([InlineKeyboardButton(text=f"{mark} {t}", callback_data=f"toggleutype_{t}")])
    kb.append([InlineKeyboardButton(text="💾 Продолжить", callback_data="saveutypes")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================
def extract_user_identifier(message: Message) -> str | None:
    if message.forward_origin and message.forward_origin.type == "user": return str(message.forward_origin.sender_user.id)
    if message.text and not message.text.startswith("/"): return message.text.strip()
    return None

def init_user_balance(user_id_str: str):
    if user_id_str not in user_balances:
        user_balances[user_id_str] = {"💰 Монеты": 100, "💎 Гемы": 0}
        save_data()

def get_welcome_text(user_id_str: str, user_name: str) -> str:
    bal = user_balances.get(user_id_str, {"💰 Монеты": 100, "💎 Гемы": 0})
    
    bal_text = ""
    for cur in currencies_db:
        amount = bal.get(cur, 0)
        bal_text += f" ├ {cur}: <b>{amount}</b>\n"
    if not bal_text: bal_text = " └ <i>Пусто</i>\n"
    else:
        parts = bal_text.rsplit('├', 1)
        bal_text = parts[0] + '└' + parts[1]
        
    # FIX BAG: Проверяем, что юнит еще существует в базе
    unlocked_base = set([item.split(":")[0] for item in user_inventory.get(user_id_str, set()) if item.split(":")[0] in units_db])
    unlocked_count = len(unlocked_base)
    total_units = len(units_db)
    
    return (
        f"👑 <b>ПРОФИЛЬ ИГРОКА: {user_name}</b> 👑\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💳 <b>Ваши финансы:</b>\n"
        f"{bal_text}"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Прогресс коллекции:</b>\n"
        f" └ 🧩 Открыто юнитов: <b>{unlocked_count} из {total_units}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "👇 <i>Выберите действие в меню ниже:</i>"
    )

async def send_main_screen(target: Message, header_text: str | None = None):
    user_id_str = str(target.from_user.id)
    user_name = target.from_user.first_name or "Игрок"
    first_text = header_text if header_text else "🏠 <i>Вы вернулись в главное меню</i>"
    await target.answer(first_text, reply_markup=reply_bottom_menu)
    msg = await target.answer(get_welcome_text(user_id_str, user_name), reply_markup=get_main_menu_kb(target.chat.type))
    if target.chat.type in {"group", "supergroup"}:
        panel_owners[f"{msg.chat.id}_{msg.message_id}"] = target.from_user.id

def render_inventory(user_id_str: str) -> tuple[str, InlineKeyboardMarkup]:
    # FIX BAG: Очищаем инвентарь и экипировку от удаленных юнитов "на лету"
    raw_unlocked = user_inventory.get(user_id_str, set())
    unlocked = {item for item in raw_unlocked if item.split(":")[0] in units_db}
    
    raw_equipped = user_equipped.get(user_id_str, [])
    equipped = [item for item in raw_equipped if item.split(":")[0] in units_db]
    
    if len(raw_unlocked) != len(unlocked) or len(raw_equipped) != len(equipped):
        user_inventory[user_id_str] = unlocked
        user_equipped[user_id_str] = equipped
        save_data()

    bal = user_balances.get(user_id_str, {"💰 Монеты": 100, "💎 Гемы": 0})
    bal_lines = [f"<b>{bal.get(c, 0)}</b> {c}" for c in currencies_db]
    bal_text = " | ".join(bal_lines)
    
    text = (
        f"🎒 <b>ИНВЕНТАРЬ ИГРОКА</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💳 <b>Баланс:</b> {bal_text}\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"⚔️ <b>ЭКИПИРОВАНО ({len(equipped)}/5):</b>\n"
    )
    
    if not equipped: 
        text += " └ <i>Пусто. Выберите юнитов из списка ниже для добавления в колоду.</i>\n\n"
    else:
        for i, item_str in enumerate(equipped, 1):
            uid, is_shiny_str = item_str.split(":")
            is_shiny = (is_shiny_str == "1")
            
            u = get_unit_stats(uid, is_shiny)
            stat_str = format_unit_stats(u).replace("├", " ").replace("└", " ")
            text += f"{i}. <b>{u.get('name')}</b> | {u.get('rarity', 'Обычный')}\n   {stat_str}\n"
        text += "\n"
        
    text += "📦 <b>ВАША КОЛЛЕКЦИЯ (НАЖМИТЕ ЧТОБЫ ЭКИПИРОВАТЬ):</b>\n"
    
    buttons = []
    if equipped: buttons.append([InlineKeyboardButton(text="❌ Снять всех юнитов ❌", callback_data="inv_unequip_all")])
    row = []
    
    sorted_unlocked = sorted(list(unlocked), key=lambda x: (int(x.split(":")[0]), x.split(":")[1]))
    for item_str in sorted_unlocked:
        uid, is_shiny_str = item_str.split(":")
        is_shiny = (is_shiny_str == "1")
        mark = "✅ " if item_str in equipped else "🔹 "
        u = get_unit_stats(uid, is_shiny)
        row.append(InlineKeyboardButton(text=f"{mark}{u.get('name')}", callback_data=f"inv_t_{uid}_{is_shiny_str}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row: buttons.append(row)
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else InlineKeyboardMarkup(inline_keyboard=[])
    return text, kb

# ==========================================
# БОЕВОЙ ИНТЕРФЕЙС И РЕНДЕР
# ==========================================
def _draw_battle_image_sync(img_bytes, total_mobs_hp, current_wave, waves_total, current_turn, turns_total, slow_pct=0):
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
        draw = ImageDraw.Draw(img)
        font = None
        if os.path.exists(FONT_FILE):
            try: font = ImageFont.truetype(FONT_FILE, size=max(20, img.height // 12))
            except: pass
        if not font:
            try: font = ImageFont.load_default()
            except: pass

        def draw_outlined_text(d, txt, pos, anchor_y="center", fill_col="white"):
            try: bbox = d.textbbox((0, 0), txt, font=font)
            except AttributeError:
                w, h = d.textsize(txt, font=font)
                bbox = (0, 0, w, h)
                
            w = bbox[2] - bbox[0]; h = bbox[3] - bbox[1]; x = pos[0] - w / 2
            y = pos[1] if anchor_y == "top" else (pos[1] - h / 2 if anchor_y == "center" else pos[1] - h)
            
            for dx in [-2, -1, 0, 1, 2]:
                for dy in [-2, -1, 0, 1, 2]:
                    if dx != 0 or dy != 0: d.text((x + dx, y + dy), txt, font=font, fill="black")
            d.text((x, y), txt, font=font, fill=fill_col)

        draw_outlined_text(draw, f"HP Врагов: {total_mobs_hp}", (img.width // 2, 10), anchor_y="top")
        draw_outlined_text(draw, f"Волна: {current_wave}/{waves_total}", (img.width // 2, img.height // 2), anchor_y="center")
        draw_outlined_text(draw, f"Ход: {current_turn} / {turns_total}", (img.width // 2, img.height - 10), anchor_y="bottom")
        if slow_pct > 0:
            draw_outlined_text(draw, f"❄️ Замедлен: -{slow_pct}%", (img.width // 2, img.height // 4), anchor_y="center", fill_col="#00FFFF")
            
        out_bio = io.BytesIO()
        img.convert("RGB").save(out_bio, format="JPEG", quality=80, optimize=True)
        return out_bio.getvalue()
    except Exception: return None

async def render_battle_ui(battle_id: str, bot: Bot) -> tuple:
    battle = active_battles[battle_id]
    map_data = maps_db[battle["map_id"]]
    wave_info = map_data["waves"][battle["current_wave"] - 1]
    
    timer_delay = bot_settings["turn_time_skip"] if battle["auto_skip"] else bot_settings["turn_time_noskip"]
    total_mobs_hp = round(sum(m["hp"] for m in battle["mobs"]), 2)
    
    current_slow_pct = sum(e["percent"] for e in battle.get("slow_effects", []))
    current_slow_pct = min(80, current_slow_pct) 
    
    photo_file = map_data.get("photo", "")
    if not photo_file and wave_info.get("mobs"):
        first_mob_id = wave_info["mobs"][0]["id"]
        photo_file = mobs_db.get(str(first_mob_id), {}).get("photo", "")

    if HAS_PIL and photo_file:
        if photo_file not in image_cache:
            try:
                bio = io.BytesIO()
                await bot.download(photo_file, destination=bio)
                image_cache[photo_file] = bio.getvalue()
            except Exception: pass
                
        if photo_file in image_cache:
            drawn_bytes = await asyncio.to_thread(_draw_battle_image_sync, image_cache[photo_file], total_mobs_hp, battle['current_wave'], map_data['waves_total'], battle['current_turn'], wave_info['turns'], current_slow_pct)
            if drawn_bytes: photo_file = BufferedInputFile(drawn_bytes, filename="render.jpg")

    text = ""
    if not photo_file or isinstance(photo_file, str):
        text += f"❤️ <b>Суммарное ХП мобов: {total_mobs_hp}</b>\n━━━━━━━━━━━━━━━\n🌊 <b>Волна: {battle['current_wave']} / {map_data['waves_total']}</b>\n━━━━━━━━━━━━━━━\n\n"
        
    if current_slow_pct > 0:
        text += f"❄️ <b>СТАТУС: ЗАМЕДЛЕН НА {current_slow_pct}%!</b>\n"
        
    living_mobs = len(battle["mobs"])
    for m in battle["mobs"][:5]: 
        def_txt = f" | 🛡 {m['def']}%" if m['def'] > 0 else ""
        trait_icon = "🦇" if m.get('trait') == "Летающий" else ("👻" if m.get('trait') == "Камуфляж" else "👾")
        status_txt = ""
        if m.get("burn_duration", 0) > 0: status_txt += "🔥 "
        if m.get("stun_duration", 0) > 0: status_txt += "⚡ "
        
        text += f"{trait_icon} {m['name']}: ❤️ {m['hp']}/{m['max_hp']}{def_txt} {status_txt}\n"
    if living_mobs > 5: text += f"<i>...и еще {living_mobs - 5} шт.</i>\n"
        
    text += "=====================\n"
    text += f"🏰 <b>ВАША БАЗА</b>\n❤️ Прочность: <b>{battle['base_hp']}/100</b>\n\n"
    text += "👥 <b>Игроки:</b>\n"
    for uid, p in battle["players"].items():
        disp_coins = int(p['coins']) if p['coins'] == int(p['coins']) else round(p['coins'], 1)
        text += f"• {p['name']}: 💰 {disp_coins}\n"
    
    text += "\n🛡 <b>Юниты на поле:</b>\n"
    total_deployed = 0
    for uid, p in battle["players"].items():
        deployed_counts = {}
        for dep in p["deployed"]:
            item_str = f"{dep['uid']}:{1 if dep.get('is_shiny') else 0}"
            deployed_counts[item_str] = deployed_counts.get(item_str, 0) + 1
            
        for item_str, count in deployed_counts.items():
            total_deployed += count
            dep_uid, is_shiny_str = item_str.split(":")
            u = get_unit_stats(dep_uid, is_shiny_str == "1")
            if not u: continue
            
            types = u.get("unit_types", [])
            stats_list = []
            if any(t in types for t in ["Одиночный", "Сплеш", "АОЕ"]): stats_list.append(f"💥 {u.get('damage', 10)}")
            if "Саппорт" in types: stats_list.append(f"✨ Саппорт")
            if "Ферма" in types: stats_list.append(f"🌾 Ферма")
            if "Замедление" in types: stats_list.append(f"❄️ Зам.")
            if "Оглушение" in types: stats_list.append(f"⚡ Стан")
            if "Горение" in types: stats_list.append(f"🔥 Огонь")
            text += f"• {u.get('name')} (x{count}) | {' | '.join(stats_list)}\n"
            
    if total_deployed == 0: text += "<i>Поле боя пустует</i>\n"
    text += "=====================\n"
    
    if not photo_file or isinstance(photo_file, str):
        text += f"⏱ <b>Ход: {battle['current_turn']} / {wave_info['turns']}</b> (След. ход через {timer_delay}с)"
    
    skip_status = "🟢 Вкл" if battle["auto_skip"] else "🔴 Выкл"
    buttons = [[
        InlineKeyboardButton(text=f"⏩ Авто-скип ({skip_status})", callback_data=f"b_toggle_{battle_id}"),
        InlineKeyboardButton(text="🏳 Сдаться", callback_data=f"b_surr_{battle_id}")
    ]]
    return photo_file, text, InlineKeyboardMarkup(inline_keyboard=buttons)

def get_player_kb(battle_id: str, user_id_str: str) -> InlineKeyboardMarkup:
    battle = active_battles[battle_id]
    p = battle["players"][user_id_str]
    buttons, row = [], []
    
    for item_str in user_equipped.get(user_id_str, []):
        uid, is_shiny_str = item_str.split(":")
        is_shiny = (is_shiny_str == "1")
        
        if uid in units_db:
            unit = get_unit_stats(uid, is_shiny)
            cost = unit.get("deploy_cost", 50)
            limit = unit.get("supply_limit", 99)
            deployed_count = sum(1 for d in p["deployed"] if d["uid"] == uid and d.get("is_shiny") == is_shiny)
            
            row.append(InlineKeyboardButton(text=f"🔸 {unit.get('name')} | 💰{cost} | ({deployed_count}/{limit})", callback_data=f"b_dep_{battle_id}_{uid}_{is_shiny_str}"))
            if len(row) == 1: 
                buttons.append(row)
                row = []
    if row: buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ==========================================
# ОСНОВНАЯ ЛОГИКА И РОУТЕРЫ
# ==========================================
dp = Dispatcher()
dp.callback_query.middleware(PanelMiddleware())

@dp.message(StateFilter('*'), Command("panel"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_panel(message: Message, state: FSMContext):
    await state.clear()
    init_user_balance(str(message.from_user.id))
    msg = await message.answer(get_welcome_text(str(message.from_user.id), message.from_user.first_name), reply_markup=get_main_menu_kb(message.chat.type))
    panel_owners[f"{msg.chat.id}_{msg.message_id}"] = message.from_user.id

@dp.message(StateFilter('*'), F.text.in_({"🔙 Назад", "🔙Назад🔙", "🔙 В Главное Меню"}))
async def global_back_button(message: Message, state: FSMContext):
    await state.clear()
    user_id_str = str(message.from_user.id)
    battle_id = user_to_battle.get(user_id_str)
    
    if battle_id and battle_id in active_battles:
        battle = active_battles[battle_id]
        p_msg_id = battle["player_msg_ids"].get(user_id_str)
        if p_msg_id:
            try: await message.bot.delete_message(chat_id=battle["chat_id"], message_id=p_msg_id)
            except: pass
        battle["players"].pop(user_id_str, None)
        del user_to_battle[user_id_str]
        
        if not battle["players"]:
            if battle_id in active_tasks: active_tasks[battle_id].cancel()
            try: await message.bot.delete_message(chat_id=battle["chat_id"], message_id=battle["main_msg_id"])
            except: pass
            del active_battles[battle_id]
            
        return await send_main_screen(message, "🏳 <b>Вы покинули поле боя и вернулись в меню.</b>")
        
    init_user_balance(user_id_str)
    await send_main_screen(message)

@dp.message(StateFilter('*'), CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    init_user_balance(str(message.from_user.id))
    await send_main_screen(message, "🔄 <i>Инициализация интерфейса завершена...</i>")

# --- ОТКРЫТИЕ КРЕЙТОВ ---
@dp.callback_query(StateFilter('*'), F.data == "crates_list")
async def cq_crates_list(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if not crates_db: return await callback.answer("Магазин пока пуст!", show_alert=True)
    kb = [[InlineKeyboardButton(text=f"📦 {c.get('name', 'Крейт')} | {c['price']} {c.get('currency', '💰 Монеты')}", callback_data=f"crate_info_{cid}")] for cid, c in crates_db.items()]
    await callback.message.edit_text("📦 <b>Магазин Крейтов</b>\n━━━━━━━━━━━━━━━━━━\n👇 Выберите крейт для просмотра шансов:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@dp.callback_query(StateFilter('*'), F.data.startswith("crate_info_"))
async def cq_crate_info(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    cid = callback.data.split("_")[2]
    crate = crates_db[cid]
    unlocked_base = set([item.split(":")[0] for item in user_inventory.get(str(callback.fromuser.id), set()) if item.split(":")[0] in units_db])
    total_weight = sum(crate.get("units", {}).values())
    
    text = f"📦 <b>{crate.get('name', 'Крейт')}</b>\n━━━━━━━━━━━━━━━━━━\n💰 <b>Цена:</b> {crate['price']} {crate.get('currency', '💰 Монеты')}\n\n🎲 <b>Шансы выпадения:</b>\n"
    if total_weight > 0:
        for uid, weight in crate["units"].items():
            if str(uid) not in units_db: continue
            chance = (weight / total_weight) * 100
            text += f"• <b>{units_db[str(uid)].get('name') if str(uid) in unlocked_base else '??? (Неизвестно)'}</b> — {chance:.1f}%\n"
    else:
        text += "<i>В этом крейте нет юнитов!</i>\n"
    
    text += "\n✨ <i>Любой выпавший юнит имеет 5% шанс стать Шайни!</i>\n━━━━━━━━━━━━━━━━━━\nСколько крейтов открыть?"
    
    kb = [[InlineKeyboardButton(text="Откр. 1", callback_data=f"crate_open_{cid}_1"), InlineKeyboardButton(text="Откр. 5", callback_data=f"crate_open_{cid}_5")],
          [InlineKeyboardButton(text="Откр. 10", callback_data=f"crate_open_{cid}_10"), InlineKeyboardButton(text="Откр. 50", callback_data=f"crate_open_{cid}_50")],
          [InlineKeyboardButton(text="🔙 Назад в магазин", callback_data="crates_list")]]
    
    try: await callback.message.delete()
    except: pass
    
    if crate.get("photo"):
        msg = await callback.message.answer_photo(photo=crate["photo"], caption=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    else:
        msg = await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        
    if callback.message.chat.type in {"group", "supergroup"}: panel_owners[f"{msg.chat.id}_{msg.message_id}"] = callback.from_user.id
    await callback.answer()

@dp.callback_query(StateFilter('*'), F.data.startswith("crate_open_"))
async def cq_crate_open(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    _, _, cid, amt_str = callback.data.split("_")
    amount = int(amt_str)
    crate = crates_db[cid]
    user_id_str = str(callback.from_user.id)
    
    # Фильтруем пулы от удаленных админом юнитов
    valid_units = {k: v for k, v in crate.get("units", {}).items() if k in units_db}
    if not valid_units: return await callback.answer("Этот крейт пуст или юниты удалены!", show_alert=True)
    
    total_cost = crate["price"] * amount
    req_cur = crate.get("currency", "💰 Монеты")
    bal = user_balances.get(user_id_str, {"💰 Монеты": 100, "💎 Гемы": 0})
    
    if bal.get(req_cur, 0) < total_cost:
        return await callback.answer(f"Недостаточно средств! Требуется {total_cost} {req_cur}.", show_alert=True)
        
    bal[req_cur] -= total_cost
    user_balances[user_id_str] = bal
    results = random.choices(list(valid_units.keys()), weights=list(valid_units.values()), k=amount)
    
    counts = {}
    if user_id_str not in user_inventory: user_inventory[user_id_str] = set()
    
    for uid in results:
        is_shiny = 1 if random.random() <= 0.05 else 0
        item_str = f"{uid}:{is_shiny}"
        counts[item_str] = counts.get(item_str, 0) + 1
        user_inventory[user_id_str].add(item_str)
    save_data()
    
    text = f"🎉 <b>{callback.from_user.first_name}</b>, вы открыли <b>{crate.get('name')}</b> ({amount} шт.)!\n━━━━━━━━━━━━━━━━━━\n<b>Вам выпало:</b>\n"
    for item_str, cnt in counts.items():
        uid, is_shiny_str = item_str.split(":")
        if uid in units_db: 
            u = get_unit_stats(uid, is_shiny_str == "1")
            text += f"• {u.get('name')} (x{cnt})\n"
            
    await callback.message.answer(text)
    await callback.answer("Успешно открыто!")

# --- ИНВЕНТАРЬ И ИНДЕКС ---
@dp.callback_query(StateFilter('*'), F.data == "main_inventory")
async def cq_main_inventory(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    init_user_balance(str(callback.from_user.id))
    text, kb = render_inventory(str(callback.from_user.id))
    try: await callback.message.edit_text(text, reply_markup=kb)
    except:
        try: await callback.message.delete()
        except: pass
        msg = await callback.message.answer(text, reply_markup=kb)
        if callback.message.chat.type in {"group", "supergroup"}: panel_owners[f"{msg.chat.id}_{msg.message_id}"] = callback.from_user.id
    await callback.answer()

@dp.callback_query(StateFilter('*'), F.data == "inv_unequip_all")
async def cq_inv_unequip_all(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id_str = str(callback.from_user.id)
    user_equipped[user_id_str] = [] 
    save_data()
    text, kb = render_inventory(user_id_str)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer("Вся колода снята!")

@dp.callback_query(StateFilter('*'), F.data.startswith("inv_t_"))
async def cq_inv_toggle(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = callback.data.split("_")
    item_str = f"{parts[2]}:{parts[3]}"
    user_id_str = str(callback.from_user.id)
    equipped = user_equipped.get(user_id_str, [])
    
    if item_str in equipped: equipped.remove(item_str)
    else:
        if len(equipped) >= 5: return await callback.answer("⚠️ В колоде может быть максимум 5 юнитов!", show_alert=True)
        equipped.append(item_str)
        
    user_equipped[user_id_str] = equipped
    save_data()
    text, kb = render_inventory(user_id_str)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(StateFilter('*'), F.data == "main_index")
async def cq_main_index(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    unlocked_base = set([item.split(":")[0] for item in user_inventory.get(str(callback.from_user.id), set()) if item.split(":")[0] in units_db])
    if not units_db: return await callback.answer("Энциклопедия пуста.", show_alert=True)
            
    text = f"📖 <b>ЭНЦИКЛОПЕДИЯ (Открыто: {len(unlocked_base)}/{len(units_db)})</b>\n━━━━━━━━━━━━━━━━━━\n\n"
    for uid, unit in units_db.items():
        if uid in unlocked_base:
            text += f"✅ <b>{unit.get('name', f'Юнит №{uid}')}</b>\n{format_unit_stats(unit)}\n"
        else: 
            text += "❓ <b>Неизвестный Юнит</b>\n └ <i>Откройте его в крейтах, чтобы увидеть характеристики</i>\n\n"
            
    try: await callback.message.edit_text(text)
    except:
        try: await callback.message.delete()
        except: pass
        msg = await callback.message.answer(text)
        if callback.message.chat.type in {"group", "supergroup"}: panel_owners[f"{msg.chat.id}_{msg.message_id}"] = callback.from_user.id
    await callback.answer()

# ==========================================
# СОЗДАНИЕ КАТОК И ДВИЖОК БОЯ
# ==========================================
def get_lobby_text(bid: str) -> str:
    battle = active_battles[bid]
    m_data = maps_db[battle["map_id"]]
    text = f"⚔️ <b>Лобби создано!</b>\nКарта: {m_data.get('name')}\n"
    text += f"\n👥 <b>Игроки ({len(battle['players'])}/4):</b>\n"
    for uid, p in battle["players"].items():
        text += f"• {p['name']}\n"
    text += "\nИгроки могут нажать «Присоединиться», а хост — «Начать»."
    return text

@dp.callback_query(StateFilter('*'), F.data == "battle_select_map")
async def lobby_select_map(callback: CallbackQuery):
    if not maps_db: return await callback.answer("Нет доступных карт!", show_alert=True)
    kb = [[InlineKeyboardButton(text=f"🗺 {m.get('name', f'Карта {mid}')}", callback_data=f"lobby_create_{mid}")] for mid, m in maps_db.items()]
    await callback.message.edit_text("Выбор Карты для игры:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@dp.callback_query(StateFilter('*'), F.data.startswith("lobby_create_"))
async def lobby_create(callback: CallbackQuery):
    mid = callback.data.split("_")[2]
    global battle_id_counter
    bid = str(battle_id_counter)
    battle_id_counter += 1
    
    user_id_str = str(callback.from_user.id)
    if user_to_battle.get(user_id_str): return await callback.answer("Вы уже в бою!", show_alert=True)
    
    m_data = maps_db[mid]
    
    active_battles[bid] = {
        "map_id": mid,
        "chat_id": callback.message.chat.id,
        "host_id": callback.from_user.id,
        "players": {
            user_id_str: {"name": callback.from_user.first_name, "coins": m_data.get("starting_coins", 100), "deployed": []}
        },
        "base_hp": 100,
        "current_wave": 1,
        "current_turn": 1,
        "mobs": [],
        "auto_skip": False,
        "is_started": False,
        "slow_effects": [] 
    }
    user_to_battle[user_id_str] = bid
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Присоединиться", callback_data=f"lobby_join_{bid}")],
        [InlineKeyboardButton(text="▶️ НАЧАТЬ БОЙ", callback_data=f"lobby_start_{bid}")]
    ])
    try: await callback.message.delete()
    except: pass
    
    msg = await callback.message.answer(get_lobby_text(bid), reply_markup=kb)
    active_battles[bid]["main_msg_id"] = msg.message_id
    await callback.answer()

@dp.callback_query(StateFilter('*'), F.data.startswith("lobby_join_"))
async def lobby_join(callback: CallbackQuery):
    bid = callback.data.split("_")[2]
    if bid not in active_battles or active_battles[bid].get("is_started"): return await callback.answer("Бой уже начался или не существует!", show_alert=True)
    
    user_id_str = str(callback.from_user.id)
    if user_to_battle.get(user_id_str): return await callback.answer("Вы уже в другом бою!", show_alert=True)
    
    battle = active_battles[bid]
    if user_id_str in battle["players"]: return await callback.answer("Вы уже в этом лобби!", show_alert=True)
    if len(battle["players"]) >= 4: return await callback.answer("Лобби заполнено (макс 4)!", show_alert=True)
    
    m_data = maps_db[battle["map_id"]]
    battle["players"][user_id_str] = {"name": callback.from_user.first_name, "coins": m_data.get("starting_coins", 100), "deployed": []}
    user_to_battle[user_id_str] = bid
    
    try: await callback.message.edit_text(get_lobby_text(bid), reply_markup=callback.message.reply_markup)
    except: pass

    await callback.answer("Вы успешно присоединились!")

@dp.callback_query(StateFilter('*'), F.data.startswith("lobby_start_"))
async def lobby_start_match(callback: CallbackQuery):
    bid = callback.data.split("_")[2]
    if bid not in active_battles: return await callback.answer("Бой не найден!", show_alert=True)
    battle = active_battles[bid]
    
    if callback.from_user.id != battle["host_id"] and str(callback.from_user.id) not in admins_db:
        return await callback.answer("Только создатель лобби может начать бой!", show_alert=True)
        
    battle["is_started"] = True
    m_data = maps_db[battle["map_id"]]
    w_info = m_data["waves"][0]
    
    battle["mobs"] = []
    for m_entry in w_info.get("mobs", []):
        mob = mobs_db.get(str(m_entry["id"]), {"name": "Моб", "hp": 100, "defense_percent": 0, "trait": "Обычный"})
        for _ in range(m_entry.get("count", 1)):
            battle["mobs"].append({
                "id": m_entry["id"],
                "name": mob.get("name", "Моб"),
                "hp": mob["hp"],
                "max_hp": mob["hp"],
                "def": mob.get("defense_percent", 0),
                "trait": mob.get("trait", "Обычный"),
                "stun_duration": 0,
                "burn_duration": 0,
                "burn_damage": 0
            })
    
    await callback.message.edit_text("🚀 <b>Бой начинается... Рассылаем клавиатуры игрокам!</b>")
    
    battle["player_msg_ids"] = {}
    for p_uid in list(battle["players"].keys()):
        try:
            p_msg = await callback.bot.send_message(chat_id=callback.message.chat.id, text=f"🎮 Пульт управления для: <b>{battle['players'][p_uid]['name']}</b>", reply_markup=get_player_kb(bid, p_uid))
            battle["player_msg_ids"][p_uid] = p_msg.message_id
        except Exception as e:
            logging.error(f"Не удалось отправить пульт: {e}")
            
    photo_file, text, main_kb = await render_battle_ui(bid, callback.bot)
    try:
        if photo_file: msg = await callback.bot.send_photo(chat_id=battle["chat_id"], photo=photo_file, caption=text[:1024], reply_markup=main_kb, parse_mode="HTML")
        else: msg = await callback.bot.send_message(chat_id=battle["chat_id"], text=text[:4096], reply_markup=main_kb, parse_mode="HTML")
        battle["main_msg_id"] = msg.message_id
    except Exception as e:
        logging.error(f"Не удалось отправить главное сообщение боя: {e}")
        
    active_tasks[bid] = asyncio.create_task(battle_loop(bid, callback.bot))
    await callback.answer()

async def battle_loop(battle_id: str, bot: Bot):
    while battle_id in active_battles:
        battle = active_battles[battle_id]
        delay = bot_settings["turn_time_skip"] if battle["auto_skip"] else bot_settings["turn_time_noskip"]
        await asyncio.sleep(delay)
        await process_battle_turn(battle_id, bot)

async def update_main_battle_message(battle_id: str, bot: Bot):
    if battle_id not in active_battles: return
    battle = active_battles[battle_id]
    photo_file, text, main_kb = await render_battle_ui(battle_id, bot)
    chat_id = battle["chat_id"]
    try:
        if photo_file:
            try: await bot.edit_message_media(chat_id=chat_id, message_id=battle["main_msg_id"], media=InputMediaPhoto(media=photo_file, caption=text[:1024], parse_mode="HTML"), reply_markup=main_kb)
            except: pass
        else:
            try: await bot.edit_message_caption(chat_id=chat_id, message_id=battle["main_msg_id"], caption=text[:1024], reply_markup=main_kb, parse_mode="HTML")
            except:
                try: await bot.edit_message_text(chat_id=chat_id, message_id=battle["main_msg_id"], text=text[:4096], reply_markup=main_kb, parse_mode="HTML")
                except: pass
    except TelegramBadRequest: pass

async def process_battle_turn(battle_id: str, bot: Bot):
    if battle_id not in active_battles: return
    battle = active_battles[battle_id]
    m_data = maps_db[battle["map_id"]]
    w_info = m_data["waves"][battle["current_wave"] - 1]
    
    best_cd_mult, best_dmg_mult = 1.0, 1.0
    
    for uid, p in battle["players"].items():
        for dep in p["deployed"]:
            u_stats = get_unit_stats(dep["uid"], dep.get("is_shiny", False))
            if not u_stats: continue
            utypes = u_stats.get("unit_types", [])
            
            if "Саппорт" in utypes:
                if float(u_stats.get("cd_boost", 1.0)) < best_cd_mult: best_cd_mult = float(u_stats.get("cd_boost", 1.0))
                if float(u_stats.get("dmg_boost", 1.0)) > best_dmg_mult: best_dmg_mult = float(u_stats.get("dmg_boost", 1.0))
                
            if "Замедление" in utypes and battle["mobs"]:
                slow_cd = float(u_stats.get("slow_cd", 15.0))
                dep["slow_timer"] = dep.get("slow_timer", slow_cd) + 1.0
                if dep["slow_timer"] >= slow_cd:
                    battle["slow_effects"].append({"percent": float(u_stats.get("slow_percent", 20.0)), "turns_left": int(u_stats.get("slow_duration", 5.0))})
                    dep["slow_timer"] = 0.0

    current_slow_pct = min(80.0, sum(e["percent"] for e in battle.get("slow_effects", [])))
    time_gain_per_turn = 1.0 * (1.0 + (current_slow_pct / 100.0))

    # --- Эффекты Горения (ДоТ) в начале хода ---
    for m in battle["mobs"]:
        if m.get("burn_duration", 0) > 0:
            bdmg = m.get("burn_damage", 0)
            actual_dmg = min(m["hp"], bdmg)
            m["hp"] = round(m["hp"] - bdmg, 2)
            m["burn_duration"] -= 1
            
            # Распределяем монеты за горение случайному игроку, чтобы не усложнять трекинг
            if battle["players"] and actual_dmg > 0:
                rand_p = random.choice(list(battle["players"].values()))
                rand_p["coins"] += actual_dmg * bot_settings["coins_per_damage"]
                
    battle["mobs"] = [m for m in battle["mobs"] if m["hp"] > 0]

    # --- Атаки юнитов ---
    for uid, p in battle["players"].items():
        for dep in p["deployed"]:
            if not battle["mobs"]: break 
            
            u_stats = get_unit_stats(dep["uid"], dep.get("is_shiny", False))
            if not u_stats: continue 
            utypes = u_stats.get("unit_types", [])
            target_t = u_stats.get("target_type", "Наземный")
            
            if not any(t in utypes for t in ["Одиночный", "Сплеш", "АОЕ", "Оглушение", "Горение"]): continue
                
            base_cd = float(u_stats.get("cd", 1.0))
            actual_cd = max(0.01, round(base_cd * best_cd_mult, 2))
            dmg_base = float(u_stats.get("damage", 10)) * best_dmg_mult
            
            dep["time_bank"] = dep.get("time_bank", 0.0) + time_gain_per_turn
            
            while dep["time_bank"] >= actual_cd and battle["mobs"]:
                dep["time_bank"] -= actual_cd
                coins_earned = 0.0

                # Ищем доступные цели
                valid_mobs = [m for m in battle["mobs"] if can_hit(target_t, m.get("trait", "Обычный"))]
                if not valid_mobs: continue # Этот юнит не может бить текущих мобов
                
                # Обработка статусов от атаки
                def apply_status_effects(target_mob):
                    if "Оглушение" in utypes:
                        if random.randint(1, 100) <= u_stats.get("stun_chance", 10):
                            target_mob["stun_duration"] = u_stats.get("stun_duration", 1)
                    if "Горение" in utypes:
                        if random.randint(1, 100) <= u_stats.get("burn_chance", 20):
                            target_mob["burn_duration"] = u_stats.get("burn_duration", 3)
                            target_mob["burn_damage"] = u_stats.get("burn_damage", 5)

                if any(t in utypes for t in ["Одиночный", "Оглушение", "Горение"]): 
                    m = valid_mobs[0]
                    dmg_actual = round(dmg_base * (1 - m["def"] / 100), 2)
                    actual_dmg_done = min(m["hp"], dmg_actual)
                    coins_earned += actual_dmg_done * bot_settings["coins_per_damage"]
                    m["hp"] = round(m["hp"] - dmg_actual, 2)
                    apply_status_effects(m)
                    
                if "Сплеш" in utypes:
                    for i in range(min(5, len(valid_mobs))):
                        m = valid_mobs[i]
                        dmg_actual = round(dmg_base * (1 - m["def"] / 100), 2)
                        actual_dmg_done = min(m["hp"], dmg_actual)
                        coins_earned += actual_dmg_done * bot_settings["coins_per_damage"]
                        m["hp"] = round(m["hp"] - dmg_actual, 2)
                        apply_status_effects(m)
                    
                if "АОЕ" in utypes:
                    for i in range(min(10, len(valid_mobs))):
                        m = valid_mobs[i]
                        dmg_actual = round(dmg_base * (1 - m["def"] / 100), 2)
                        actual_dmg_done = min(m["hp"], dmg_actual)
                        coins_earned += actual_dmg_done * bot_settings["coins_per_damage"]
                        m["hp"] = round(m["hp"] - dmg_actual, 2)
                        apply_status_effects(m)
                        
                battle["mobs"] = [m for m in battle["mobs"] if m["hp"] > 0]
                p["coins"] += coins_earned

    chat_id = battle["chat_id"]
    
    # Обновление таймеров глобального замедления и стана мобов
    new_slows = []
    for se in battle.get("slow_effects", []):
        se["turns_left"] -= 1
        if se["turns_left"] > 0: new_slows.append(se)
    battle["slow_effects"] = new_slows
    
    for m in battle["mobs"]:
        if m.get("stun_duration", 0) > 0: m["stun_duration"] -= 1

    if not battle["mobs"]:
        for p_uid, p_data in battle["players"].items():
            wave_income = sum(get_unit_stats(dep["uid"], dep.get("is_shiny", False)).get("income", 0) for dep in p_data["deployed"] if get_unit_stats(dep["uid"], dep.get("is_shiny", False)) and "Ферма" in get_unit_stats(dep["uid"], dep.get("is_shiny", False)).get("unit_types", []))
            if wave_income > 0: p_data["coins"] += wave_income

        battle["current_wave"] += 1
        battle["current_turn"] = 1
        if battle["current_wave"] > m_data["waves_total"]:
            return await finish_battle(battle_id, bot, True) 
            
        w_info = m_data["waves"][battle["current_wave"] - 1]
        battle["mobs"] = []
        for m_entry in w_info.get("mobs", []):
            mob = mobs_db.get(str(m_entry["id"]), {"name": "Моб", "hp": 100, "defense_percent": 0, "trait": "Обычный"})
            for _ in range(m_entry.get("count", 1)):
                battle["mobs"].append({"id": m_entry["id"], "name": mob.get("name", "Моб"), "hp": mob["hp"], "max_hp": mob["hp"], "def": mob.get("defense_percent", 0), "trait": mob.get("trait", "Обычный"), "stun_duration": 0, "burn_duration": 0})
    else:
        battle["current_turn"] += 1
        if battle["current_turn"] > w_info["turns"]:
            # Мобы дошли до базы. Оглушенные мобы пропускают ход и не наносят урон базе в этот раз.
            active_mobs = [m for m in battle["mobs"] if m.get("stun_duration", 0) <= 0]
            battle["base_hp"] = round(battle["base_hp"] - sum(m["hp"]/10 for m in active_mobs), 2)
            battle["mobs"] = [] # В конце волны все мобы исчезают
            
            if battle["base_hp"] <= 0: return await finish_battle(battle_id, bot, False) 
                
            battle["current_wave"] += 1
            battle["current_turn"] = 1
            if battle["current_wave"] > m_data["waves_total"]: return await finish_battle(battle_id, bot, True) 
                
            w_info = m_data["waves"][battle["current_wave"] - 1]
            battle["mobs"] = []
            for m_entry in w_info.get("mobs", []):
                mob = mobs_db.get(str(m_entry["id"]), {"name": "Моб", "hp": 100, "defense_percent": 0, "trait": "Обычный"})
                for _ in range(m_entry.get("count", 1)):
                    battle["mobs"].append({"id": m_entry["id"], "name": mob.get("name", "Моб"), "hp": mob["hp"], "max_hp": mob["hp"], "def": mob.get("defense_percent", 0), "trait": mob.get("trait", "Обычный"), "stun_duration": 0, "burn_duration": 0})

    await update_main_battle_message(battle_id, bot)

async def finish_battle(battle_id: str, bot: Bot, is_win: bool):
    battle = active_battles[battle_id]
    chat_id = battle["chat_id"]
    m_data = maps_db[battle["map_id"]]
    rew = m_data.get("rewards", {})
    
    text = "❇️❇️❇️ <b>ПОБЕДА В БОЮ!</b> ❇️❇️❇️\n\nНаграды:\n" if is_win else "📛📛📛 <b>ПОРАЖЕНИЕ (БАЗА УНИЧТОЖЕНА)</b> 📛📛📛\n\nУтешительный приз:\n"
    for k, v in rew.items():
        amt = v if is_win else max(1, int(v * 0.1))
        text += f"+{amt} {k}\n"
        for p_uid in battle["players"].keys():
            bal = user_balances.get(p_uid, {"💰 Монеты": 100, "💎 Гемы": 0})
            bal[k] = bal.get(k, 0) + amt
            user_balances[p_uid] = bal
            
    save_data()
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    await cleanup_battle(battle_id, bot)

async def cleanup_battle(battle_id: str, bot: Bot):
    battle = active_battles[battle_id]
    for p_uid, p_msg_id in battle.get("player_msg_ids", {}).items():
        try: await bot.delete_message(chat_id=battle["chat_id"], message_id=p_msg_id)
        except: pass
    try: await bot.delete_message(chat_id=battle["chat_id"], message_id=battle["main_msg_id"])
    except: pass
    
    for p_uid in battle["players"].keys(): user_to_battle.pop(p_uid, None)
    if battle_id in active_tasks:
        active_tasks[battle_id].cancel()
        del active_tasks[battle_id]
    del active_battles[battle_id]

# --- ОБРАБОТЧИКИ БОЯ ---
@dp.callback_query(StateFilter('*'), F.data.startswith("b_dep_"))
async def battle_deploy(callback: CallbackQuery):
    parts = callback.data.split("_")
    battle_id, uid, is_shiny_str = parts[2], parts[3], parts[4]
    user_id_str = str(callback.from_user.id)
    
    if battle_id not in active_battles: return await callback.answer("Бой окончен!", show_alert=True)
    battle = active_battles[battle_id]
    if user_id_str not in battle["players"]: return await callback.answer("Вы не в этом бою!", show_alert=True)
    
    p = battle["players"][user_id_str]
    is_shiny = (is_shiny_str == "1")
    unit = get_unit_stats(uid, is_shiny)
    if not unit: return await callback.answer("Юнит не найден!", show_alert=True)
    
    cost = unit.get("deploy_cost", 50)
    if p["coins"] < cost: return await callback.answer(f"Не хватает монет! Нужно: {cost}", show_alert=True)
    
    limit = unit.get("supply_limit", 99)
    deployed_count = sum(1 for d in p["deployed"] if d["uid"] == uid and d.get("is_shiny") == is_shiny)
    if deployed_count >= limit: return await callback.answer("Лимит юнитов этого типа достигнут!", show_alert=True)
    
    p["coins"] -= cost
    p["deployed"].append({"uid": uid, "is_shiny": is_shiny})
    
    try: await callback.message.edit_reply_markup(reply_markup=get_player_kb(battle_id, user_id_str))
    except: pass
    
    await update_main_battle_message(battle_id, callback.bot)
    
    await callback.answer(f"Юнит размещен! (-{cost} монет)")

@dp.callback_query(StateFilter('*'), F.data.startswith("b_toggle_"))
async def battle_toggle_skip(callback: CallbackQuery):
    battle_id = callback.data.split("_")[2]
    if battle_id not in active_battles: return await callback.answer("Бой окончен!", show_alert=True)
    battle = active_battles[battle_id]
    if callback.from_user.id != battle["host_id"]: return await callback.answer("Только хост может переключать авто-скип!", show_alert=True)
    
    battle["auto_skip"] = not battle["auto_skip"]
    await callback.answer(f"Авто-скип: {'Вкл' if battle['auto_skip'] else 'Выкл'}")

@dp.callback_query(StateFilter('*'), F.data.startswith("b_surr_"))
async def battle_surrender(callback: CallbackQuery):
    battle_id = callback.data.split("_")[2]
    if battle_id not in active_battles: return await callback.answer("Бой окончен!", show_alert=True)
    battle = active_battles[battle_id]
    if callback.from_user.id != battle["host_id"]: return await callback.answer("Только хост может сдаться!", show_alert=True)
    await finish_battle(battle_id, callback.bot, False)
    await callback.answer("Вы сдались!")

# ==========================================
# АДМИН ПАНЕЛЬ И РЕДАКТОРЫ
# ==========================================
@dp.callback_query(StateFilter('*'), F.data == "admin_panel")
async def cq_admin_panel(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) not in admins_db: return await callback.answer("⛔️ У вас нет прав!", show_alert=True)
    await state.clear()
    text = "👑 <b>ПАНЕЛЬ АДМИНИСТРАТОРА</b>\n━━━━━━━━━━━━━━━━━━\nВыберите действие:"
    try: await callback.message.edit_text(text, reply_markup=get_admin_panel_kb())
    except:
        try: await callback.message.delete()
        except: pass
        await callback.message.answer(text, reply_markup=get_admin_panel_kb())
    await callback.answer()

# --- ДОБАВЛЕНИЕ ЮНИТА ---
@dp.callback_query(StateFilter('*'), F.data == "admin_add_unit")
async def cq_u_add(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if not rarities_db: return await callback.answer("Сначала создайте редкости!", show_alert=True)
    await state.set_state(AdminUnitAdd.unit_types)
    await state.update_data(temp_types=[])
    await callback.message.edit_text("➕ <b>Создание Юнита</b>\n\nВыберите классы (Мультикласс):", reply_markup=get_unit_types_kb([]))
    await callback.answer()

@dp.callback_query(AdminUnitAdd.unit_types, F.data.startswith("toggleutype_"))
async def u_toggle_type(cb: CallbackQuery, state: FSMContext):
    t_name = cb.data.split("_")[1]
    data = await state.get_data()
    types = data.get("temp_types", [])
    if t_name in types: types.remove(t_name)
    else: types.append(t_name)
    await state.update_data(temp_types=types)
    await cb.message.edit_reply_markup(reply_markup=get_unit_types_kb(types))
    await cb.answer()

@dp.callback_query(AdminUnitAdd.unit_types, F.data == "saveutypes")
async def u_save_types(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("temp_types"): return await cb.answer("Выберите хотя бы один класс!", show_alert=True)
    
    # Теперь спрашиваем цель
    await state.set_state(AdminUnitAdd.target_type)
    kb = [[InlineKeyboardButton(text=t, callback_data=f"selutarg_{idx}")] for idx, t in enumerate(UNIT_TARGET_TYPES)]
    await cb.message.edit_text("🎯 Выберите кого может бить юнит:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()

@dp.callback_query(AdminUnitAdd.target_type, F.data.startswith("selutarg_"))
async def u_step_target(cb: CallbackQuery, state: FSMContext):
    target_name = UNIT_TARGET_TYPES[int(cb.data.split("_")[1])]
    await state.update_data(target_type=target_name)
    await state.set_state(AdminUnitAdd.photo)
    await cb.message.edit_text("📸 Отправьте фото юнита (или напишите 'Пропустить'):")
    await cb.answer()

@dp.message(AdminUnitAdd.photo)
async def u_step_photo(m: Message, state: FSMContext):
    if m.photo: await state.update_data(photo=m.photo[-1].file_id)
    elif m.text and m.text.lower() == "пропустить": await state.update_data(photo=None)
    else: return await m.answer("⚠️ Требуется отправить фото или написать 'Пропустить'.")
    
    await state.set_state(AdminUnitAdd.name)
    await m.answer("📝 Введите название юнита:")

@dp.message(AdminUnitAdd.name)
async def u_step_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await state.set_state(AdminUnitAdd.rarity)
    kb = [[InlineKeyboardButton(text=f"🔸 {r} 🔸", callback_data=f"selrar_{idx}")] for idx, r in enumerate(rarities_db)]
    await m.answer("💎 Выберите редкость:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(AdminUnitAdd.rarity, F.data.startswith("selrar_"))
async def u_step_rarity(cb: CallbackQuery, state: FSMContext):
    await state.update_data(rarity=rarities_db[int(cb.data.split("_")[1])])
    await state.set_state(AdminUnitAdd.supply_limit)
    await cb.message.edit_text("🛑 Введите лимит поставки на поле (например, 5):")
    await cb.answer()

@dp.message(AdminUnitAdd.supply_limit)
async def u_step_limit(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("⚠️ Введите число.")
    await state.update_data(supply_limit=int(m.text))
    await state.set_state(AdminUnitAdd.deploy_cost)
    await m.answer("💰 Введите цену размещения в бою:")

@dp.message(AdminUnitAdd.deploy_cost)
async def u_step_cost(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("⚠️ Введите число.")
    await state.update_data(deploy_cost=int(m.text))
    await jump_next_unit_stat(m, state)

async def jump_next_unit_stat(m: Message, state: FSMContext):
    data = await state.get_data()
    types = data.get("temp_types", [])
    has_atk = any(t in types for t in ["Одиночный", "Сплеш", "АОЕ", "Замедление", "Оглушение", "Горение"])
    
    if has_atk and "cd" not in data:
        await state.set_state(AdminUnitAdd.cd)
        return await m.answer("⏱ <b>[Атака/Особое]</b> КД атаки (сек, например 1.0):")
    if has_atk and "damage" not in data:
        await state.set_state(AdminUnitAdd.damage)
        return await m.answer("💥 <b>[Атака/Особое]</b> Урон:")
    if "Саппорт" in types and "cd_boost" not in data:
        await state.set_state(AdminUnitAdd.cd_boost)
        return await m.answer("✨ <b>[Саппорт]</b> Буст КД (например 0.80 для -20% КД):")
    if "Саппорт" in types and "dmg_boost" not in data:
        await state.set_state(AdminUnitAdd.dmg_boost)
        return await m.answer("💪 <b>[Саппорт]</b> Буст Урона (например 1.20 для +20%):")
    if "Ферма" in types and "income" not in data:
        await state.set_state(AdminUnitAdd.income)
        return await m.answer("🌾 <b>[Ферма]</b> Доход монет за волну:")
    if "Замедление" in types and "slow_percent" not in data:
        await state.set_state(AdminUnitAdd.slow_percent)
        return await m.answer("❄️ <b>[Замедление]</b> % замедления (например 20):")
    if "Замедление" in types and "slow_duration" not in data:
        await state.set_state(AdminUnitAdd.slow_duration)
        return await m.answer("⏳ <b>[Замедление]</b> Длительность замедления (в ходах):")
    if "Замедление" in types and "slow_cd" not in data:
        await state.set_state(AdminUnitAdd.slow_cd)
        return await m.answer("⏱ <b>[Замедление]</b> КД на каст замедления (в ходах):")
        
    if "Оглушение" in types and "stun_chance" not in data:
        await state.set_state(AdminUnitAdd.stun_chance)
        return await m.answer("⚡ <b>[Оглушение]</b> Шанс оглушить цель (%, например 15):")
    if "Оглушение" in types and "stun_duration" not in data:
        await state.set_state(AdminUnitAdd.stun_duration)
        return await m.answer("⏳ <b>[Оглушение]</b> Длительность стана (в ходах, например 1):")

    if "Горение" in types and "burn_chance" not in data:
        await state.set_state(AdminUnitAdd.burn_chance)
        return await m.answer("🔥 <b>[Горение]</b> Шанс поджечь цель (%, например 20):")
    if "Горение" in types and "burn_damage" not in data:
        await state.set_state(AdminUnitAdd.burn_damage)
        return await m.answer("💥 <b>[Горение]</b> Урон от огня за каждый ход:")
    if "Горение" in types and "burn_duration" not in data:
        await state.set_state(AdminUnitAdd.burn_duration)
        return await m.answer("⏳ <b>[Горение]</b> Длительность горения (в ходах):")

    global unit_id_counter
    uid = str(unit_id_counter)
    u_dict = {
        "photo": data["photo"], 
        "name": data["name"], 
        "rarity": data["rarity"], 
        "unit_types": types, 
        "target_type": data.get("target_type", "Наземный"),
        "supply_limit": data["supply_limit"], 
        "deploy_cost": data["deploy_cost"]
    }
    if has_atk:
        u_dict["cd"] = data["cd"]
        u_dict["damage"] = data["damage"]
    if "Саппорт" in types:
        u_dict["cd_boost"] = data["cd_boost"]
        u_dict["dmg_boost"] = data["dmg_boost"]
    if "Ферма" in types: u_dict["income"] = data["income"]
    if "Замедление" in types:
        u_dict["slow_percent"] = data["slow_percent"]
        u_dict["slow_duration"] = data["slow_duration"]
        u_dict["slow_cd"] = data["slow_cd"]
    if "Оглушение" in types:
        u_dict["stun_chance"] = data["stun_chance"]
        u_dict["stun_duration"] = data["stun_duration"]
    if "Горение" in types:
        u_dict["burn_chance"] = data["burn_chance"]
        u_dict["burn_damage"] = data["burn_damage"]
        u_dict["burn_duration"] = data["burn_duration"]
        
    units_db[uid] = u_dict
    unit_id_counter += 1
    save_data()
    await state.clear()
    await send_main_screen(m, f"✅ Юнит «{data['name']}» создан!")

@dp.message(AdminUnitAdd.cd)
async def u_rec_cd(m: Message, state: FSMContext):
    try: val = float(m.text.replace(",", "."))
    except: return await m.answer("⚠️ Число (например 1.5).")
    await state.update_data(cd=val)
    await jump_next_unit_stat(m, state)

@dp.message(AdminUnitAdd.damage)
async def u_rec_dmg(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("⚠️ Целое число.")
    await state.update_data(damage=int(m.text))
    await jump_next_unit_stat(m, state)

@dp.message(AdminUnitAdd.cd_boost)
async def u_rec_cdb(m: Message, state: FSMContext):
    try: val = float(m.text.replace(",", "."))
    except: return await m.answer("⚠️ Число.")
    await state.update_data(cd_boost=val)
    await jump_next_unit_stat(m, state)

@dp.message(AdminUnitAdd.dmg_boost)
async def u_rec_dmgb(m: Message, state: FSMContext):
    try: val = float(m.text.replace(",", "."))
    except: return await m.answer("⚠️ Число.")
    await state.update_data(dmg_boost=val)
    await jump_next_unit_stat(m, state)

@dp.message(AdminUnitAdd.income)
async def u_rec_inc(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("⚠️ Целое число.")
    await state.update_data(income=int(m.text))
    await jump_next_unit_stat(m, state)

@dp.message(AdminUnitAdd.slow_percent)
async def u_rec_sp(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("⚠️ Целое число.")
    await state.update_data(slow_percent=int(m.text))
    await jump_next_unit_stat(m, state)

@dp.message(AdminUnitAdd.slow_duration)
async def u_rec_sd(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("⚠️ Целое число.")
    await state.update_data(slow_duration=int(m.text))
    await jump_next_unit_stat(m, state)

@dp.message(AdminUnitAdd.slow_cd)
async def u_rec_scd(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("⚠️ Целое число.")
    await state.update_data(slow_cd=int(m.text))
    await jump_next_unit_stat(m, state)

@dp.message(AdminUnitAdd.stun_chance)
async def u_rec_stunch(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("⚠️ Целое число.")
    await state.update_data(stun_chance=int(m.text))
    await jump_next_unit_stat(m, state)

@dp.message(AdminUnitAdd.stun_duration)
async def u_rec_stund(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("⚠️ Целое число.")
    await state.update_data(stun_duration=int(m.text))
    await jump_next_unit_stat(m, state)
    
@dp.message(AdminUnitAdd.burn_chance)
async def u_rec_burnch(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("⚠️ Целое число.")
    await state.update_data(burn_chance=int(m.text))
    await jump_next_unit_stat(m, state)

@dp.message(AdminUnitAdd.burn_damage)
async def u_rec_burndmg(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("⚠️ Целое число.")
    await state.update_data(burn_damage=int(m.text))
    await jump_next_unit_stat(m, state)
    
@dp.message(AdminUnitAdd.burn_duration)
async def u_rec_burndur(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("⚠️ Целое число.")
    await state.update_data(burn_duration=int(m.text))
    await jump_next_unit_stat(m, state)

@dp.callback_query(StateFilter('*'), F.data == "admin_del_unit")
async def a_del_unit(cb: CallbackQuery):
    kb = [[InlineKeyboardButton(text=f"❌ {u.get('name')}", callback_data=f"delunit_{uid}")] for uid, u in units_db.items()]
    kb.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_panel")])
    await cb.message.edit_text("Выберите юнита для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(StateFilter('*'), F.data.startswith("delunit_"))
async def a_del_unit_act(cb: CallbackQuery):
    uid = cb.data.split("_")[1]
    if uid in units_db:
        name = units_db[uid].get('name')
        del units_db[uid]
        save_data()
        await cb.answer(f"Удален: {name}", show_alert=True)
    await cq_admin_panel(cb, None)

# --- ДОБАВЛЕНИЕ МОБА ---
@dp.callback_query(StateFilter('*'), F.data == "admin_add_mob")
async def admin_add_mob(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminMobAdd.name)
    await cb.message.edit_text("Введите название Моба:")

@dp.message(AdminMobAdd.name)
async def admin_mob_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text)
    await state.set_state(AdminMobAdd.trait)
    kb = [[InlineKeyboardButton(text=t, callback_data=f"selmtrait_{idx}")] for idx, t in enumerate(MOB_TRAITS)]
    await m.answer("🧬 Выберите особенность моба (Наземный, Летающий и т.д.):", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(AdminMobAdd.trait, F.data.startswith("selmtrait_"))
async def admin_mob_trait(cb: CallbackQuery, state: FSMContext):
    t_name = MOB_TRAITS[int(cb.data.split("_")[1])]
    await state.update_data(trait=t_name)
    await state.set_state(AdminMobAdd.photo)
    await cb.message.edit_text("📸 Отправьте фото Моба (или напишите 'Пропустить'):")
    await cb.answer()

@dp.message(AdminMobAdd.photo)
async def admin_mob_photo(m: Message, state: FSMContext):
    if m.photo: await state.update_data(photo=m.photo[-1].file_id)
    elif m.text and m.text.lower() == "пропустить": await state.update_data(photo=None)
    else: return await m.answer("⚠️ Отправьте фото или напишите 'Пропустить'.")
    
    await state.set_state(AdminMobAdd.hp)
    await m.answer("❤️ Введите базовое ХП моба (число):")

@dp.message(AdminMobAdd.hp)
async def admin_mob_hp(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Введите число.")
    await state.update_data(hp=int(m.text))
    await state.set_state(AdminMobAdd.effect)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Без эффекта", callback_data="mobeff_none")],
        [InlineKeyboardButton(text="🛡 Броня (Поглощение %)", callback_data="mobeff_defense")]
    ])
    await m.answer("Выберите эффект моба:", reply_markup=kb)

@dp.callback_query(AdminMobAdd.effect, F.data.startswith("mobeff_"))
async def admin_mob_effect(cb: CallbackQuery, state: FSMContext):
    eff = cb.data.split("_")[1]
    await state.update_data(effect=eff)
    if eff == "none":
        await save_mob(cb.message, state)
    else:
        await state.set_state(AdminMobAdd.defense_percent)
        await cb.message.edit_text("🛡 Введите процент защиты (например 20 для 20%):")
        await cb.answer()

@dp.message(AdminMobAdd.defense_percent)
async def admin_mob_def(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Введите число.")
    await state.update_data(defense_percent=int(m.text))
    await save_mob(m, state)

async def save_mob(m: Message, state: FSMContext):
    data = await state.get_data()
    global mob_id_counter
    mid = str(mob_id_counter)
    mobs_db[mid] = {
        "name": data["name"],
        "hp": data["hp"],
        "trait": data.get("trait", "Обычный"),
        "effect": data["effect"],
        "defense_percent": data.get("defense_percent", 0),
        "photo": data.get("photo", "")
    }
    mob_id_counter += 1
    save_data()
    await state.clear()
    await send_main_screen(m, f"✅ Моб «{data['name']}» добавлен (ID: {mid})!")

@dp.callback_query(StateFilter('*'), F.data == "admin_del_mob")
async def a_del_mob(cb: CallbackQuery):
    kb = [[InlineKeyboardButton(text=f"❌ {m.get('name')}", callback_data=f"delmob_{mid}")] for mid, m in mobs_db.items()]
    kb.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_panel")])
    await cb.message.edit_text("Выберите моба для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(StateFilter('*'), F.data.startswith("delmob_"))
async def a_del_mob_act(cb: CallbackQuery):
    mid = cb.data.split("_")[1]
    if mid in mobs_db:
        name = mobs_db[mid].get('name')
        del mobs_db[mid]
        save_data()
        await cb.answer(f"Удален: {name}", show_alert=True)
    await cq_admin_panel(cb, None)

# --- ДОБАВЛЕНИЕ КАРТЫ И ВОЛН ---
@dp.callback_query(StateFilter('*'), F.data == "admin_add_map")
async def admin_add_map(cb: CallbackQuery, state: FSMContext):
    if not mobs_db: return await cb.answer("Сначала создайте мобов!", show_alert=True)
    await state.set_state(AdminMapAdd.name)
    await state.update_data(waves=[], current_mobs=[])
    await cb.message.edit_text("🗺 Введите название Карты:")

@dp.message(AdminMapAdd.name)
async def admin_map_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text)
    await state.set_state(AdminMapAdd.photo)
    await m.answer("📸 Отправьте фото/фон Карты (или 'Пропустить'):")

@dp.message(AdminMapAdd.photo)
async def admin_map_photo(m: Message, state: FSMContext):
    if m.photo: await state.update_data(photo=m.photo[-1].file_id)
    elif m.text and m.text.lower() == "пропустить": await state.update_data(photo=None)
    else: return await m.answer("⚠️ Отправьте фото или напишите 'Пропустить'.")
    await show_wave_builder(m, state)

async def show_wave_builder(m_or_cb, state: FSMContext):
    data = await state.get_data()
    waves = data.get("waves", [])
    current_mobs = data.get("current_mobs", [])
    wave_num = len(waves) + 1
    
    await state.set_state(AdminMapAdd.wave_builder)
    
    text = f"🗺 <b>Настройка Карты: {data['name']}</b>\n━━━━━━━━━━━━━━\n"
    text += f"🌊 <b>Волна {wave_num}</b>\nМобы в этой волне:\n"
    if not current_mobs: text += " └ <i>Пусто</i>\n"
    else:
        for cm in current_mobs:
            mob = mobs_db.get(str(cm["id"]), {})
            text += f" ├ {mob.get('name', 'Моб')} (x{cm['count']})\n"
            
    kb = [[InlineKeyboardButton(text="➕ Добавить моба", callback_data="mapb_add_mob")]]
    if current_mobs: kb.append([InlineKeyboardButton(text="➡️ Сохранить Волну", callback_data="mapb_save_wave")])
    if waves: kb.append([InlineKeyboardButton(text="💾 Завершить Карту", callback_data="mapb_finish")])
        
    rm = InlineKeyboardMarkup(inline_keyboard=kb)
    if isinstance(m_or_cb, Message): await m_or_cb.answer(text, reply_markup=rm)
    else: await m_or_cb.message.edit_text(text, reply_markup=rm)

@dp.callback_query(AdminMapAdd.wave_builder, F.data == "mapb_add_mob")
async def mapb_add_mob_list(cb: CallbackQuery, state: FSMContext):
    kb = []
    row = []
    for mid, mob in mobs_db.items():
        row.append(InlineKeyboardButton(text=mob.get("name", f"Моб {mid}"), callback_data=f"mapb_selmob_{mid}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row: kb.append(row)
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="mapb_back")])
    
    await cb.message.edit_text("Выберите моба для добавления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()

@dp.callback_query(AdminMapAdd.wave_builder, F.data == "mapb_back")
async def mapb_back_handler(cb: CallbackQuery, state: FSMContext):
    await show_wave_builder(cb, state)

@dp.callback_query(AdminMapAdd.wave_builder, F.data.startswith("mapb_selmob_"))
async def mapb_select_mob(cb: CallbackQuery, state: FSMContext):
    mid = cb.data.split("_")[2]
    await state.update_data(selected_mob=mid)
    await state.set_state(AdminMapAdd.waiting_mob_count)
    await cb.message.edit_text("🔢 Введите количество этих мобов для волны:")
    await cb.answer()

@dp.message(AdminMapAdd.waiting_mob_count)
async def mapb_mob_count(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Введите число.")
    data = await state.get_data()
    current_mobs = data.get("current_mobs", [])
    current_mobs.append({"id": data["selected_mob"], "count": int(m.text)})
    await state.update_data(current_mobs=current_mobs)
    await show_wave_builder(m, state)

@dp.callback_query(AdminMapAdd.wave_builder, F.data == "mapb_save_wave")
async def mapb_save_wave(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminMapAdd.waiting_wave_turns)
    await cb.message.edit_text("⏳ Введите длительность волны (в ходах):")
    await cb.answer()

@dp.message(AdminMapAdd.waiting_wave_turns)
async def mapb_wave_turns(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Введите число.")
    data = await state.get_data()
    waves = data.get("waves", [])
    waves.append({"turns": int(m.text), "mobs": data["current_mobs"]})
    await state.update_data(waves=waves, current_mobs=[])
    await show_wave_builder(m, state)

@dp.callback_query(AdminMapAdd.wave_builder, F.data == "mapb_finish")
async def mapb_finish(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminMapAdd.waiting_reward_coins)
    await cb.message.edit_text("💰 Введите награду за победу (Количество Монет):")
    await cb.answer()

@dp.message(AdminMapAdd.waiting_reward_coins)
async def map_reward_coins(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Введите число.")
    await state.update_data(reward_coins=int(m.text))
    await state.set_state(AdminMapAdd.waiting_reward_gems)
    await m.answer("💎 Введите награду за победу (Количество Гемов):")

@dp.message(AdminMapAdd.waiting_reward_gems)
async def map_reward_gems(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Введите число.")
    data = await state.get_data()
    global map_id_counter
    mid = str(map_id_counter)
    maps_db[mid] = {
        "name": data["name"],
        "photo": data.get("photo"),
        "starting_coins": 100,
        "waves_total": len(data["waves"]),
        "waves": data["waves"],
        "rewards": {"💰 Монеты": data["reward_coins"], "💎 Гемы": int(m.text)}
    }
    map_id_counter += 1
    save_data()
    await state.clear()
    await send_main_screen(m, f"✅ Карта «{data['name']}» добавлена!")

@dp.callback_query(StateFilter('*'), F.data == "admin_del_map")
async def a_del_map(cb: CallbackQuery):
    kb = [[InlineKeyboardButton(text=f"❌ {m.get('name')}", callback_data=f"delmap_{mid}")] for mid, m in maps_db.items()]
    kb.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_panel")])
    await cb.message.edit_text("Выберите карту для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(StateFilter('*'), F.data.startswith("delmap_"))
async def a_del_map_act(cb: CallbackQuery):
    mid = cb.data.split("_")[1]
    if mid in maps_db:
        name = maps_db[mid].get('name')
        del maps_db[mid]
        save_data()
        await cb.answer(f"Удалена: {name}", show_alert=True)
    await cq_admin_panel(cb, None)

# --- ДОБАВЛЕНИЕ КРЕЙТА ---
@dp.callback_query(StateFilter('*'), F.data == "admin_add_crate")
async def admin_add_crate(cb: CallbackQuery, state: FSMContext):
    if not units_db: return await cb.answer("Сначала создайте юнитов!", show_alert=True)
    await state.set_state(AdminCrateAdd.name)
    await state.update_data(units={})
    await cb.message.edit_text("📦 Введите название Крейта:")

@dp.message(AdminCrateAdd.name)
async def admin_crate_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text)
    await state.set_state(AdminCrateAdd.price)
    await m.answer("💰 Введите цену крейта (в монетах):")

@dp.message(AdminCrateAdd.price)
async def admin_crate_price(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Введите число.")
    await state.update_data(price=int(m.text))
    await state.set_state(AdminCrateAdd.photo)
    await m.answer("📸 Отправьте фото Крейта (или 'Пропустить'):")

@dp.message(AdminCrateAdd.photo)
async def admin_crate_photo(m: Message, state: FSMContext):
    if m.photo: await state.update_data(photo=m.photo[-1].file_id)
    elif m.text and m.text.lower() == "пропустить": await state.update_data(photo=None)
    else: return await m.answer("⚠️ Отправьте фото или напишите 'Пропустить'.")
    await show_crate_builder(m, state)

async def show_crate_builder(m_or_cb, state: FSMContext):
    data = await state.get_data()
    crate_units = data.get("units", {})
    await state.set_state(AdminCrateAdd.unit_builder)
    
    text = f"📦 <b>Крейт: {data['name']}</b>\nСодержимое:\n"
    if not crate_units: text += " └ <i>Пусто</i>\n"
    else:
        for uid, weight in crate_units.items():
            u = units_db.get(str(uid), {})
            text += f" ├ {u.get('name', 'Юнит')} (Вес: {weight})\n"
            
    kb = []
    row = []
    for uid, u in units_db.items():
        row.append(InlineKeyboardButton(text=u.get("name", f"Юнит {uid}"), callback_data=f"crb_selu_{uid}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row: kb.append(row)
    
    if crate_units: kb.append([InlineKeyboardButton(text="💾 Завершить Крейт", callback_data="crb_finish")])
        
    rm = InlineKeyboardMarkup(inline_keyboard=kb)
    if isinstance(m_or_cb, Message): await m_or_cb.answer(text, reply_markup=rm)
    else: await m_or_cb.message.edit_text(text, reply_markup=rm)

@dp.callback_query(AdminCrateAdd.unit_builder, F.data.startswith("crb_selu_"))
async def crb_select_unit(cb: CallbackQuery, state: FSMContext):
    uid = cb.data.split("_")[2]
    await state.update_data(selected_unit=uid)
    await state.set_state(AdminCrateAdd.waiting_unit_weight)
    await cb.message.edit_text("⚖️ Введите ВЕС (шанс) выпадения этого юнита (чем больше число, тем чаще падает):")
    await cb.answer()

@dp.message(AdminCrateAdd.waiting_unit_weight)
async def crb_unit_weight(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Введите число.")
    data = await state.get_data()
    crate_units = data.get("units", {})
    crate_units[data["selected_unit"]] = int(m.text)
    await state.update_data(units=crate_units)
    await show_crate_builder(m, state)

@dp.callback_query(AdminCrateAdd.unit_builder, F.data == "crb_finish")
async def crb_finish(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    global crate_id_counter
    cid = str(crate_id_counter)
    crates_db[cid] = {
        "name": data["name"],
        "price": data["price"],
        "currency": "💰 Монеты",
        "units": data["units"],
        "photo": data.get("photo")
    }
    crate_id_counter += 1
    save_data()
    await state.clear()
    await send_main_screen(cb.message, f"✅ Крейт «{data['name']}» добавлен!")
    await cb.answer()

@dp.callback_query(StateFilter('*'), F.data == "admin_del_crate")
async def a_del_crate(cb: CallbackQuery):
    kb = [[InlineKeyboardButton(text=f"❌ {c.get('name')}", callback_data=f"delcrate_{cid}")] for cid, c in crates_db.items()]
    kb.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_panel")])
    await cb.message.edit_text("Выберите крейт для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(StateFilter('*'), F.data.startswith("delcrate_"))
async def a_del_crate_act(cb: CallbackQuery):
    cid = cb.data.split("_")[1]
    if cid in crates_db:
        name = crates_db[cid].get('name')
        del crates_db[cid]
        save_data()
        await cb.answer(f"Удален: {name}", show_alert=True)
    await cq_admin_panel(cb, None)

# ==========================================
# НОВАЯ ФУНКЦИЯ: РЕДАКТИРОВАНИЕ ОБЪЕКТОВ
# ==========================================
@dp.callback_query(StateFilter('*'), F.data == "admin_edit_unit_list")
async def edit_unit_list(cb: CallbackQuery):
    if not units_db: return await cb.answer("Нет юнитов!", show_alert=True)
    kb = [[InlineKeyboardButton(text=u.get('name'), callback_data=f"ed_u_{uid}")] for uid, u in units_db.items()]
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")])
    await cb.message.edit_text("Выберите юнита для изменения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(StateFilter('*'), F.data.startswith("ed_u_"))
async def edit_unit_menu(cb: CallbackQuery):
    uid = cb.data.split("_")[2]
    u = units_db.get(uid)
    if not u: return await cb.answer("Юнит не найден!", show_alert=True)
    
    text = f"✏️ <b>Редактирование: {u.get('name')}</b>\nВыберите параметр для изменения:\n"
    kb = []
    # Основные статы
    row1 = [
        InlineKeyboardButton(text=f"Название ({u.get('name')})", callback_data=f"set_u_{uid}_name"),
        InlineKeyboardButton(text=f"Урон ({u.get('damage', '-')})", callback_data=f"set_u_{uid}_damage")
    ]
    row2 = [
        InlineKeyboardButton(text=f"КД ({u.get('cd', '-')})", callback_data=f"set_u_{uid}_cd"),
        InlineKeyboardButton(text=f"Цена ({u.get('deploy_cost')})", callback_data=f"set_u_{uid}_deploy_cost")
    ]
    kb.extend([row1, row2])
    
    if "Замедление" in u.get("unit_types", []):
        kb.append([InlineKeyboardButton(text=f"Замедление % ({u.get('slow_percent')})", callback_data=f"set_u_{uid}_slow_percent")])
    if "Оглушение" in u.get("unit_types", []):
        kb.append([InlineKeyboardButton(text=f"Шанс Стана ({u.get('stun_chance')}%)", callback_data=f"set_u_{uid}_stun_chance")])
    if "Горение" in u.get("unit_types", []):
        kb.append([InlineKeyboardButton(text=f"Урон Огня ({u.get('burn_damage')})", callback_data=f"set_u_{uid}_burn_damage")])
        
    kb.append([InlineKeyboardButton(text="🔙 К списку", callback_data="admin_edit_unit_list")])
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(StateFilter('*'), F.data == "admin_edit_mob_list")
async def edit_mob_list(cb: CallbackQuery):
    if not mobs_db: return await cb.answer("Нет мобов!", show_alert=True)
    kb = [[InlineKeyboardButton(text=m.get('name'), callback_data=f"ed_m_{mid}")] for mid, m in mobs_db.items()]
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")])
    await cb.message.edit_text("Выберите моба для изменения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(StateFilter('*'), F.data.startswith("ed_m_"))
async def edit_mob_menu(cb: CallbackQuery):
    mid = cb.data.split("_")[2]
    m = mobs_db.get(mid)
    if not m: return await cb.answer("Моб не найден!", show_alert=True)
    
    text = f"✏️ <b>Редактирование Моба: {m.get('name')}</b>\nВыберите параметр:\n"
    kb = [
        [InlineKeyboardButton(text=f"Название ({m.get('name')})", callback_data=f"set_m_{mid}_name")],
        [InlineKeyboardButton(text=f"Здоровье ({m.get('hp')})", callback_data=f"set_m_{mid}_hp")],
        [InlineKeyboardButton(text=f"Броня % ({m.get('defense_percent')})", callback_data=f"set_m_{mid}_defense_percent")],
        [InlineKeyboardButton(text="🔙 К списку", callback_data="admin_edit_mob_list")]
    ]
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(StateFilter('*'), F.data == "admin_edit_map_list")
async def edit_map_list(cb: CallbackQuery):
    if not maps_db: return await cb.answer("Нет карт!", show_alert=True)
    kb = [[InlineKeyboardButton(text=m.get('name'), callback_data=f"ed_map_{mid}")] for mid, m in maps_db.items()]
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")])
    await cb.message.edit_text("Выберите карту для изменения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(StateFilter('*'), F.data.startswith("ed_map_"))
async def edit_map_menu(cb: CallbackQuery, state: FSMContext):
    mid = cb.data.split("_")[2]
    m = maps_db.get(mid)
    if not m: return await cb.answer("Карта не найдена!", show_alert=True)
    
    text = f"✏️ <b>Карта: {m.get('name')}</b>\nВнимание: Редактировать волны нужно через перезапись."
    kb = [
        [InlineKeyboardButton(text="🔄 Перезаписать Волны", callback_data=f"remap_{mid}")],
        [InlineKeyboardButton(text="🔙 К списку", callback_data="admin_edit_map_list")]
    ]
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(StateFilter('*'), F.data.startswith("remap_"))
async def edit_map_waves_start(cb: CallbackQuery, state: FSMContext):
    mid = cb.data.split("_")[1]
    m_data = maps_db.get(mid)
    await state.set_state(AdminMapAdd.wave_builder)
    await state.update_data(name=m_data['name'], photo=m_data.get('photo'), waves=[], current_mobs=[], editing_map_id=mid)
    await show_wave_builder(cb, state)

@dp.callback_query(StateFilter('*'), F.data == "admin_edit_crate_list")
async def edit_crate_list(cb: CallbackQuery):
    if not crates_db: return await cb.answer("Нет крейтов!", show_alert=True)
    kb = [[InlineKeyboardButton(text=c.get('name'), callback_data=f"ed_c_{cid}")] for cid, c in crates_db.items()]
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")])
    await cb.message.edit_text("Выберите крейт для изменения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(StateFilter('*'), F.data.startswith("ed_c_"))
async def edit_crate_menu(cb: CallbackQuery):
    cid = cb.data.split("_")[2]
    c = crates_db.get(cid)
    if not c: return await cb.answer("Крейт не найден!", show_alert=True)
    
    text = f"✏️ <b>Крейт: {c.get('name')}</b>\n"
    kb = [
        [InlineKeyboardButton(text=f"Название ({c.get('name')})", callback_data=f"set_c_{cid}_name")],
        [InlineKeyboardButton(text=f"Цена ({c.get('price')})", callback_data=f"set_c_{cid}_price")],
        [InlineKeyboardButton(text="🔄 Изменить содержимое (перезапись)", callback_data=f"recrate_{cid}")],
        [InlineKeyboardButton(text="🔙 К списку", callback_data="admin_edit_crate_list")]
    ]
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(StateFilter('*'), F.data.startswith("recrate_"))
async def edit_crate_units_start(cb: CallbackQuery, state: FSMContext):
    cid = cb.data.split("_")[1]
    c_data = crates_db.get(cid)
    await state.set_state(AdminCrateAdd.unit_builder)
    await state.update_data(name=c_data['name'], price=c_data['price'], photo=c_data.get('photo'), units={}, editing_crate_id=cid)
    await show_crate_builder(cb, state)


# --- ГЛОБАЛЬНЫЙ ПЕРЕХВАТЧИК УСТАНОВКИ ЗНАЧЕНИЙ ---
@dp.callback_query(StateFilter('*'), F.data.startswith("set_"))
async def generic_edit_trigger(cb: CallbackQuery, state: FSMContext):
    # format: set_{type}_{id}_{field}
    parts = cb.data.split("_", 3)
    e_type, e_id, field = parts[1], parts[2], parts[3]
    
    await state.set_state(AdminEditGeneric.waiting_for_value)
    await state.update_data(e_type=e_type, e_id=e_id, field=field)
    
    await cb.message.edit_text(f"✏️ Введите новое значение для поля <b>{field}</b>:\n<i>(Для отмены напишите /cancel)</i>")
    await cb.answer()

@dp.message(AdminEditGeneric.waiting_for_value)
async def generic_edit_receive(m: Message, state: FSMContext):
    if m.text == "/cancel":
        await state.clear()
        return await send_main_screen(m, "Действие отменено.")
        
    data = await state.get_data()
    e_type, e_id, field = data['e_type'], data['e_id'], data['field']
    val = m.text.strip()
    
    # Пытаемся конвертировать в число, если это логично
    if val.isdigit(): val = int(val)
    else:
        try: val = float(val.replace(",", "."))
        except: pass # Оставляем строкой
        
    # Применяем в базу
    if e_type == "u" and e_id in units_db:
        units_db[e_id][field] = val
    elif e_type == "m" and e_id in mobs_db:
        mobs_db[e_id][field] = val
    elif e_type == "c" and e_id in crates_db:
        crates_db[e_id][field] = val
        
    save_data()
    await state.clear()
    await send_main_screen(m, f"✅ Значение успешно изменено на {val}!")

# ==========================================
# ОСТАЛЬНЫЕ КОМАНДЫ И НАСТРОЙКИ
# ==========================================
@dp.callback_query(StateFilter('*'), F.data == "admin_give_cur")
async def cq_admin_give_cur(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AdminGiveCur.select_cur)
    kb = [[InlineKeyboardButton(text=f"🔸 {c} 🔸", callback_data=f"givecur_{idx}")] for idx, c in enumerate(currencies_db)]
    await cb.message.edit_text("💸 <b>Выдача валюты игрокам</b>\nВыберите валюту:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()

@dp.callback_query(AdminGiveCur.select_cur, F.data.startswith("givecur_"))
async def admin_give_cur_step2(cb: CallbackQuery, state: FSMContext):
    cur_name = currencies_db[int(cb.data.split("_")[1])]
    await state.update_data(give_cur_name=cur_name)
    await state.set_state(AdminGiveCur.target_id)
    await cb.message.edit_text("👤 Введите <b>ID игрока</b> (или перешлите его сообщение):")
    await cb.answer()

@dp.message(AdminGiveCur.target_id)
async def admin_give_cur_step3(m: Message, state: FSMContext):
    uid = extract_user_identifier(m)
    if not uid: return await m.answer("⚠️ Не удалось распознать ID. Попробуйте вручную.")
    await state.update_data(target_id=uid)
    await state.set_state(AdminGiveCur.amount)
    data = await state.get_data()
    await m.answer(f"🔢 Сколько <b>{data['give_cur_name']}</b> выдать игроку <code>{uid}</code>?\n<i>(Отрицательные числа для штрафа)</i>")

@dp.message(AdminGiveCur.amount)
async def admin_give_cur_step4(m: Message, state: FSMContext):
    try: amt = int(m.text)
    except ValueError: return await m.answer("⚠️ Введите целое число!")
    
    data = await state.get_data()
    cur_name, target_id = data["give_cur_name"], data["target_id"]
    
    init_user_balance(target_id)
    user_balances[target_id][cur_name] = user_balances[target_id].get(cur_name, 0) + amt
    save_data()
    
    try:
        if amt > 0: await m.bot.send_message(chat_id=target_id, text=f"🎁 <b>СИСТЕМНОЕ УВЕДОМЛЕНИЕ</b>\nАдмин вручил вам: <b>{amt} {cur_name}</b>!")
        else: await m.bot.send_message(chat_id=target_id, text=f"📉 <b>СИСТЕМНОЕ УВЕДОМЛЕНИЕ</b>\nАдмин списал у вас: <b>{abs(amt)} {cur_name}</b>.")
        notify_status = "✅ Игрок успешно уведомлен."
    except: notify_status = "⚠️ Игрок не получил уведомление (заблокировал бота)."

    await state.clear()
    await m.answer(f"✅ Баланс игрока <code>{target_id}</code> обновлен!\nИзменение: <b>{amt} {cur_name}</b>\n\n{notify_status}")
    await send_main_screen(m)

@dp.callback_query(StateFilter('*'), F.data == "admin_add_rarity")
async def admin_add_rarity(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminRarityAdd.waiting_for_name)
    await cb.message.edit_text("Введите название редкости:")

@dp.message(AdminRarityAdd.waiting_for_name)
async def admin_save_rarity(m: Message, state: FSMContext):
    rarities_db.append(m.text.strip())
    save_data()
    await state.clear()
    await send_main_screen(m, f"✅ Редкость «{m.text}» добавлена!")

@dp.callback_query(StateFilter('*'), F.data == "admin_del_rarity")
async def a_del_rarity(cb: CallbackQuery):
    kb = [[InlineKeyboardButton(text=f"❌ {r}", callback_data=f"delrar_{idx}")] for idx, r in enumerate(rarities_db)]
    kb.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_panel")])
    await cb.message.edit_text("Выберите редкость для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(StateFilter('*'), F.data.startswith("delrar_"))
async def a_del_rarity_act(cb: CallbackQuery):
    idx = int(cb.data.split("_")[1])
    if 0 <= idx < len(rarities_db):
        r = rarities_db.pop(idx)
        save_data()
        await cb.answer(f"Удалено: {r}", show_alert=True)
    await cq_admin_panel(cb, None)

@dp.callback_query(StateFilter('*'), F.data == "admin_add_cur")
async def a_add_cur(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminCurAdd.name)
    await cb.message.edit_text("🪙 Введите название новой валюты (например '💎 Алмазы'):")

@dp.message(AdminCurAdd.name)
async def a_add_cur_name(m: Message, state: FSMContext):
    currencies_db.append(m.text.strip())
    save_data()
    await state.clear()
    await send_main_screen(m, f"✅ Валюта {m.text} добавлена!")

@dp.callback_query(StateFilter('*'), F.data == "admin_del_cur")
async def a_del_cur(cb: CallbackQuery):
    kb = [[InlineKeyboardButton(text=f"❌ {c}", callback_data=f"delcur_{idx}")] for idx, c in enumerate(currencies_db)]
    kb.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_panel")])
    await cb.message.edit_text("Выберите валюту для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(StateFilter('*'), F.data.startswith("delcur_"))
async def a_del_cur_act(cb: CallbackQuery):
    idx = int(cb.data.split("_")[1])
    if 0 <= idx < len(currencies_db):
        c = currencies_db.pop(idx)
        save_data()
        await cb.answer(f"Удалено: {c}", show_alert=True)
    await cq_admin_panel(cb, None)

@dp.callback_query(StateFilter('*'), F.data == "admin_add")
async def a_add_adm(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminManage.add_id)
    await cb.message.edit_text("👨‍💻 Введите ID пользователя для назначения админом:")

@dp.message(AdminManage.add_id)
async def a_add_adm_do(m: Message, state: FSMContext):
    uid = extract_user_identifier(m)
    if uid:
        admins_db.add(uid)
        save_data()
        await m.answer(f"✅ Пользователь {uid} стал админом.")
    await state.clear()
    await send_main_screen(m)

@dp.callback_query(StateFilter('*'), F.data == "admin_remove")
async def a_rem_adm(cb: CallbackQuery):
    kb = [[InlineKeyboardButton(text=f"❌ {a}", callback_data=f"deladm_{a}")] for a in admins_db if a != MAIN_ADMIN_ID]
    kb.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_panel")])
    await cb.message.edit_text("🚫 Выберите админа для снятия (Главного снять нельзя):", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(StateFilter('*'), F.data.startswith("deladm_"))
async def a_rem_adm_act(cb: CallbackQuery):
    uid = cb.data.split("_")[1]
    if uid in admins_db and uid != MAIN_ADMIN_ID:
        admins_db.remove(uid)
        save_data()
        await cb.answer(f"Админ снят: {uid}", show_alert=True)
    await cq_admin_panel(cb, None)

def get_settings_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🪙 Монет за урон: {bot_settings.get('coins_per_damage', 0.5)}", callback_data="aset_cpd")],
        [InlineKeyboardButton(text=f"⏩ Время хода (скип): {bot_settings.get('turn_time_skip', 5)}с", callback_data="aset_tts")],
        [InlineKeyboardButton(text=f"⏱ Время хода (обыч.): {bot_settings.get('turn_time_noskip', 10)}с", callback_data="aset_ttns")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
    ])

@dp.callback_query(StateFilter('*'), F.data == "admin_settings")
async def a_settings(cb: CallbackQuery):
    await cb.message.edit_text("⚙️ <b>Настройки Бота</b>\nНажмите на параметр для изменения:", reply_markup=get_settings_kb())

@dp.callback_query(StateFilter('*'), F.data.startswith("aset_"))
async def a_set_edit(cb: CallbackQuery, state: FSMContext):
    action = cb.data
    if action == "aset_cpd":
        await state.set_state(AdminSettingsEdit.waiting_for_coins_per_damage)
        await cb.message.edit_text("🪙 Введите количество монет за 1 ед. урона (например 0.5):")
    elif action == "aset_tts":
        await state.set_state(AdminSettingsEdit.waiting_for_turn_time_skip)
        await cb.message.edit_text("⏩ Введите длительность хода при авто-скипе (в секундах):")
    elif action == "aset_ttns":
        await state.set_state(AdminSettingsEdit.waiting_for_turn_time_noskip)
        await cb.message.edit_text("⏱ Введите длительность хода БЕЗ авто-скипа (в секундах):")
    await cb.answer()

@dp.message(AdminSettingsEdit.waiting_for_coins_per_damage)
async def a_set_cpd_do(m: Message, state: FSMContext):
    try: bot_settings["coins_per_damage"] = float(m.text.replace(",", "."))
    except: return await m.answer("⚠️ Введите число (например 0.5).")
    save_data()
    await state.clear()
    await send_main_screen(m, "✅ Настройка (Монеты за урон) сохранена.")

@dp.message(AdminSettingsEdit.waiting_for_turn_time_skip)
async def a_set_tts_do(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("⚠️ Введите целое число.")
    bot_settings["turn_time_skip"] = int(m.text)
    save_data()
    await state.clear()
    await send_main_screen(m, "✅ Настройка (Время хода при авто-скипе) сохранена.")

@dp.message(AdminSettingsEdit.waiting_for_turn_time_noskip)
async def a_set_ttns_do(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("⚠️ Введите целое число.")
    bot_settings["turn_time_noskip"] = int(m.text)
    save_data()
    await state.clear()
    await send_main_screen(m, "✅ Настройка (Время хода без скипа) сохранена.")

@dp.callback_query(StateFilter('*'), F.data == "admin_backup_db")
async def admin_backup(cb: CallbackQuery):
    data = db_get("full_state", {})
    file = BufferedInputFile(json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8'), filename="game_data_backup.json")
    await cb.message.answer_document(file, caption="📂 <b>Вот полный бэкап базы данных.</b>\n\nЧтобы восстановить её, отправьте этот файл боту и в подписи (caption) укажите команду <code>/restore_db</code>")
    await cb.answer()

@dp.message(StateFilter('*'), F.document, F.caption == "/restore_db")
async def admin_restore(m: Message):
    if str(m.from_user.id) not in admins_db: return
    bio = io.BytesIO()
    await m.bot.download(m.document, destination=bio)

    try:
        data = json.loads(bio.getvalue().decode('utf-8'))
        db_set("full_state", data)
        load_data()
        await m.answer("✅ База данных успешно восстановлена! Все настройки, пользователи и коллекции загружены из бэкапа.")
    except Exception as e:
        await m.answer(f"❌ Ошибка восстановления: {e}")

# ==========================================
# УНИВЕРСАЛЬНЫЙ ПЕРЕХВАТЧИК
# ==========================================
async def safe_exit_and_menu(m: Message, state: FSMContext, alert_text=None):
    await state.clear()
    init_user_balance(str(m.from_user.id))
    await send_main_screen(m, alert_text)

@dp.message(StateFilter('*'), F.text.startswith("/"))
async def handle_unknown_command(m: Message, state: FSMContext):
    await safe_exit_and_menu(m, state, "⚠️ Неизвестная команда. Вы возвращены в меню.")

@dp.message(StateFilter('*'))
async def handle_any_text(m: Message, state: FSMContext):
    if m.chat.type in {"group", "supergroup"}: return
    await safe_exit_and_menu(m, state)

# ==========================================
# ЗАПУСК БОТА
# ==========================================
async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    load_data() 
            
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands([
        BotCommand(command="panel", description="Открыть меню (только для групп)"),
        BotCommand(command="start", description="Запустить/Перезапустить бота")
    ])
    await bot.delete_webhook(drop_pending_updates=True)
    try: await dp.start_polling(bot)
    finally: await bot.session.close()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
