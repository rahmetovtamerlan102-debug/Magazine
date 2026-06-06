#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
import sqlite3
import os
import csv
import io
import time
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

load_dotenv()

# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "5895176743,8276815852").split(',')))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@HowScad_support")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "+79027365759")
RECIPIENT_NAME = os.getenv("RECIPIENT_NAME", "Егор")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@Howscard")
REQUIRED_CHANNEL_LINK = os.getenv("REQUIRED_CHANNEL_LINK", "https://t.me/Howscard")
PORT = int(os.getenv("PORT", "8080"))

RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")
if not RENDER_EXTERNAL_HOSTNAME:
    WEBHOOK_BASE_URL = f"http://localhost:{PORT}"
else:
    WEBHOOK_BASE_URL = f"https://{RENDER_EXTERNAL_HOSTNAME}"

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_BASE_URL}{WEBHOOK_PATH}"

ORDERS_PER_PAGE = 3

COUNTRY_PRICES = {
    "us": {"name": "🇺🇸 США", "price": 80},
    "canada": {"name": "🇨🇦 Канада", "price": 65},
    "russia": {"name": "🇷🇺 Россия (новорег)", "price": 140},
    "bangladesh": {"name": "🇧🇩 Бангладеш", "price": 90},
    "philippines": {"name": "🇵🇭 Филиппины", "price": 65},
    "nigeria": {"name": "🇳🇬 Нигерия", "price": 60},
    "iraq": {"name": "🇮🇶 Ирак", "price": 60},
    "africa": {"name": "🌍 Африка", "price": 60},
    "india": {"name": "🇮🇳 Индия", "price": 50, "warning": "⚠️ Высокий шанс слёта"}
}

STARS_PRICES = {
    "50": {"name": "⭐ 50 Stars", "stars": 50, "price": 70},
    "100": {"name": "⭐ 100 Stars", "stars": 100, "price": 140},
    "250": {"name": "⭐ 250 Stars", "stars": 250, "price": 350},
    "400": {"name": "⭐ 400 Stars", "stars": 400, "price": 550},
    "500": {"name": "⭐ 500 Stars", "stars": 500, "price": 670},
    "750": {"name": "⭐ 750 Stars", "stars": 750, "price": 1000},
    "1000": {"name": "⭐ 1000 Stars", "stars": 1000, "price": 1400}
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ==================== АСИНХРОННАЯ РАБОТА С SQLITE ====================
DB_NAME = "shop.db"
# Кэши
_stats_cache = {}
_stats_cache_time = 0
_users_cache = []
_users_cache_time = 0
_promos_cache = []
_promos_cache_time = 0

def get_db_connection():
    """Создаёт синхронное соединение (для использования в пуле потоков)"""
    return sqlite3.connect(DB_NAME, timeout=10, check_same_thread=False)

async def run_sync_query(query, params=None, fetch=True):
    """Выполняет SQL-запрос в отдельном потоке, не блокируя event loop"""
    def _sync():
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            if params:
                cur.execute(query, params)
            else:
                cur.execute(query)
            if fetch:
                return cur.fetchall()
            else:
                conn.commit()
                return cur.lastrowid
        finally:
            cur.close()
            conn.close()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync)

# ==================== ИНИЦИАЛИЗАЦИЯ БД ====================
async def init_db():
    queries = [
        '''CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            product_type TEXT DEFAULT 'account',
            product_name TEXT,
            price INTEGER,
            address TEXT,
            screenshot TEXT,
            status TEXT DEFAULT 'pending',
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        '''CREATE TABLE IF NOT EXISTS stars_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            stars INTEGER,
            price INTEGER,
            stars_username TEXT,
            screenshot TEXT,
            status TEXT DEFAULT 'pending',
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        '''CREATE TABLE IF NOT EXISTS blocked (user_id INTEGER PRIMARY KEY)''',
        '''CREATE TABLE IF NOT EXISTS admin_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            action TEXT,
            target TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        '''CREATE TABLE IF NOT EXISTS promocodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            discount_percent INTEGER,
            product_type TEXT CHECK(product_type IN ('account', 'stars')),
            max_uses INTEGER,
            used_count INTEGER DEFAULT 0,
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        '''CREATE TABLE IF NOT EXISTS user_promocodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            promo_code TEXT,
            discount_percent INTEGER,
            product_type TEXT,
            activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)",
        "CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_stars_status ON stars_orders(status)",
        "CREATE INDEX IF NOT EXISTS idx_stars_user ON stars_orders(user_id)",
    ]
    for q in queries:
        await run_sync_query(q, fetch=False)

# Инициализируем БД при старте
asyncio.create_task(init_db())

# ==================== ФУНКЦИИ ДЛЯ АККАУНТОВ (АСИНХРОННЫЕ) ====================
async def save_order(uid, uname, product_name, price, addr):
    q = "INSERT INTO orders (user_id, username, product_type, product_name, price, address) VALUES (?,?,?,?,?,?)"
    return await run_sync_query(q, (uid, uname, 'account', product_name, price, addr), fetch=False)

async def update_screenshot(oid, fid):
    q = "UPDATE orders SET screenshot = ? WHERE id = ?"
    await run_sync_query(q, (fid, oid), fetch=False)

async def update_status(oid, status):
    q = "UPDATE orders SET status = ? WHERE id = ?"
    await run_sync_query(q, (status, oid), fetch=False)

async def get_orders(uid):
    q = "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC"
    return await run_sync_query(q, (uid,))

async def get_all_orders_by_status(status, limit=None, offset=None):
    if limit is not None and offset is not None:
        q = "SELECT * FROM orders WHERE status = ? ORDER BY id DESC LIMIT ? OFFSET ?"
        return await run_sync_query(q, (status, limit, offset))
    else:
        q = "SELECT * FROM orders WHERE status = ? ORDER BY id DESC"
        return await run_sync_query(q, (status,))

async def get_orders_count_by_status(status):
    q = "SELECT COUNT(*) FROM orders WHERE status = ?"
    res = await run_sync_query(q, (status,))
    return res[0][0] if res else 0

async def get_order(oid):
    q = "SELECT * FROM orders WHERE id = ?"
    res = await run_sync_query(q, (oid,))
    return res[0] if res else None

async def get_all_orders_for_export():
    q = "SELECT * FROM orders ORDER BY id DESC"
    return await run_sync_query(q)

# ==================== ФУНКЦИИ ДЛЯ ЗВЁЗД (АСИНХРОННЫЕ) ====================
async def save_stars_order(uid, uname, stars, price, stars_username):
    q = "INSERT INTO stars_orders (user_id, username, stars, price, stars_username) VALUES (?,?,?,?,?)"
    return await run_sync_query(q, (uid, uname, stars, price, stars_username), fetch=False)

async def update_stars_screenshot(oid, fid):
    q = "UPDATE stars_orders SET screenshot = ? WHERE id = ?"
    await run_sync_query(q, (fid, oid), fetch=False)

async def update_stars_status(oid, status):
    q = "UPDATE stars_orders SET status = ? WHERE id = ?"
    await run_sync_query(q, (status, oid), fetch=False)

async def get_stars_orders_by_status(status, limit=None, offset=None):
    if limit is not None and offset is not None:
        q = "SELECT * FROM stars_orders WHERE status = ? ORDER BY id DESC LIMIT ? OFFSET ?"
        return await run_sync_query(q, (status, limit, offset))
    else:
        q = "SELECT * FROM stars_orders WHERE status = ? ORDER BY id DESC"
        return await run_sync_query(q, (status,))

async def get_stars_orders_count_by_status(status):
    q = "SELECT COUNT(*) FROM stars_orders WHERE status = ?"
    res = await run_sync_query(q, (status,))
    return res[0][0] if res else 0

async def get_stars_order(oid):
    q = "SELECT * FROM stars_orders WHERE id = ?"
    res = await run_sync_query(q, (oid,))
    return res[0] if res else None

async def get_user_stars_orders(uid):
    q = "SELECT * FROM stars_orders WHERE user_id = ? ORDER BY id DESC"
    return await run_sync_query(q, (uid,))

async def get_all_stars_orders_for_export():
    q = "SELECT * FROM stars_orders ORDER BY id DESC"
    return await run_sync_query(q)

# ==================== ПРОМОКОДЫ ====================
async def create_promocode(code, discount_percent, product_type, max_uses):
    q = "INSERT INTO promocodes (code, discount_percent, product_type, max_uses) VALUES (?,?,?,?)"
    try:
        return await run_sync_query(q, (code.upper(), discount_percent, product_type, max_uses), fetch=False)
    except:
        return False

async def get_all_promocodes_cached():
    global _promos_cache, _promos_cache_time
    if time.time() - _promos_cache_time > 60:
        q = "SELECT * FROM promocodes ORDER BY id DESC"
        _promos_cache = await run_sync_query(q)
        _promos_cache_time = time.time()
    return _promos_cache

async def delete_promocode(code):
    q = "DELETE FROM promocodes WHERE code = ?"
    await run_sync_query(q, (code.upper(),), fetch=False)
    global _promos_cache_time
    _promos_cache_time = 0  # сбросить кэш

async def activate_promocode_for_user(user_id, code, product_type):
    # Проверяем существование
    q_promo = "SELECT code, discount_percent, max_uses, used_count FROM promocodes WHERE code = ? AND product_type = ?"
    promo = await run_sync_query(q_promo, (code.upper(), product_type))
    if not promo:
        return False, "Промокод не найден"
    promo = promo[0]
    max_uses = promo[2]
    used = promo[3]
    if max_uses != 0 and used >= max_uses:
        return False, "Промокод уже использован максимальное количество раз"
    # Проверяем, не активировал ли пользователь
    q_check = "SELECT 1 FROM user_promocodes WHERE user_id = ? AND promo_code = ? AND product_type = ?"
    if await run_sync_query(q_check, (user_id, code.upper(), product_type)):
        return False, "Вы уже активировали этот промокод"
    # Активация
    q_insert = "INSERT INTO user_promocodes (user_id, promo_code, discount_percent, product_type) VALUES (?,?,?,?)"
    await run_sync_query(q_insert, (user_id, code.upper(), promo[1], product_type), fetch=False)
    q_update = "UPDATE promocodes SET used_count = used_count + 1 WHERE code = ?"
    await run_sync_query(q_update, (code.upper(),), fetch=False)
    global _promos_cache_time
    _promos_cache_time = 0
    return True, f"Промокод {code.upper()} активирован! Скидка {promo[1]}% на {'аккаунты' if product_type == 'account' else 'звёзды'}"

async def get_user_discount(user_id, product_type):
    q = "SELECT MAX(discount_percent) FROM user_promocodes WHERE user_id = ? AND product_type = ?"
    res = await run_sync_query(q, (user_id, product_type))
    return res[0][0] or 0 if res else 0

# ==================== СТАТИСТИКА С КЭШЕМ ====================
async def get_stats():
    global _stats_cache, _stats_cache_time
    if time.time() - _stats_cache_time < 60 and _stats_cache:
        return _stats_cache
    q_total_accounts = "SELECT COUNT(*) FROM orders"
    q_total_accounts_sum = "SELECT SUM(price) FROM orders WHERE status='completed'"
    q_pending_accounts = "SELECT COUNT(*) FROM orders WHERE status='pending'"
    q_processing_accounts = "SELECT COUNT(*) FROM orders WHERE status='processing'"
    q_completed_accounts = "SELECT COUNT(*) FROM orders WHERE status='completed'"
    q_rejected_accounts = "SELECT COUNT(*) FROM orders WHERE status='rejected'"
    q_total_stars = "SELECT COUNT(*) FROM stars_orders"
    q_total_stars_sum = "SELECT SUM(price) FROM stars_orders WHERE status='completed'"
    q_pending_stars = "SELECT COUNT(*) FROM stars_orders WHERE status='pending'"
    q_processing_stars = "SELECT COUNT(*) FROM stars_orders WHERE status='processing'"
    q_completed_stars = "SELECT COUNT(*) FROM stars_orders WHERE status='completed'"
    q_rejected_stars = "SELECT COUNT(*) FROM stars_orders WHERE status='rejected'"
    q_blocked = "SELECT COUNT(*) FROM blocked"

    results = await asyncio.gather(
        run_sync_query(q_total_accounts),
        run_sync_query(q_total_accounts_sum),
        run_sync_query(q_pending_accounts),
        run_sync_query(q_processing_accounts),
        run_sync_query(q_completed_accounts),
        run_sync_query(q_rejected_accounts),
        run_sync_query(q_total_stars),
        run_sync_query(q_total_stars_sum),
        run_sync_query(q_pending_stars),
        run_sync_query(q_processing_stars),
        run_sync_query(q_completed_stars),
        run_sync_query(q_rejected_stars),
        run_sync_query(q_blocked),
    )
    total_accounts = results[0][0][0] if results[0] else 0
    total_accounts_sum = results[1][0][0] if results[1] and results[1][0][0] else 0
    pending_accounts = results[2][0][0] if results[2] else 0
    processing_accounts = results[3][0][0] if results[3] else 0
    completed_accounts = results[4][0][0] if results[4] else 0
    rejected_accounts = results[5][0][0] if results[5] else 0
    total_stars = results[6][0][0] if results[6] else 0
    total_stars_sum = results[7][0][0] if results[7] and results[7][0][0] else 0
    pending_stars = results[8][0][0] if results[8] else 0
    processing_stars = results[9][0][0] if results[9] else 0
    completed_stars = results[10][0][0] if results[10] else 0
    rejected_stars = results[11][0][0] if results[11] else 0
    blocked_count = results[12][0][0] if results[12] else 0

    _stats_cache = {
        "accounts": {"total": total_accounts, "sum": total_accounts_sum, "pending": pending_accounts, "processing": processing_accounts, "completed": completed_accounts, "rejected": rejected_accounts},
        "stars": {"total": total_stars, "sum": total_stars_sum, "pending": pending_stars, "processing": processing_stars, "completed": completed_stars, "rejected": rejected_stars},
        "blocked": blocked_count,
    }
    _stats_cache_time = time.time()
    return _stats_cache

# ==================== БЛОКИРОВКА ====================
async def block_user(uid):
    q = "INSERT OR IGNORE INTO blocked (user_id) VALUES (?)"
    await run_sync_query(q, (uid,), fetch=False)

async def unblock_user(uid):
    q = "DELETE FROM blocked WHERE user_id = ?"
    await run_sync_query(q, (uid,), fetch=False)

async def is_blocked(uid):
    q = "SELECT 1 FROM blocked WHERE user_id = ?"
    res = await run_sync_query(q, (uid,))
    return len(res) > 0

async def get_all_users_cached():
    global _users_cache, _users_cache_time
    if time.time() - _users_cache_time < 300:
        return _users_cache
    q1 = "SELECT DISTINCT user_id FROM orders"
    q2 = "SELECT DISTINCT user_id FROM stars_orders"
    res1 = await run_sync_query(q1)
    res2 = await run_sync_query(q2)
    users = set()
    for row in res1:
        users.add(row[0])
    for row in res2:
        users.add(row[0])
    _users_cache = list(users)
    _users_cache_time = time.time()
    return _users_cache

# ==================== ПОДПИСКА ====================
async def is_subscribed(uid):
    try:
        m = await bot.get_chat_member(REQUIRED_CHANNEL, uid)
        return m.status in ["member", "administrator", "creator"]
    except:
        return False

def sub_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 ПОДПИСАТЬСЯ", url=REQUIRED_CHANNEL_LINK)],
        [InlineKeyboardButton(text="🔄 ПРОВЕРИТЬ", callback_data="check_sub")]
    ])

# ==================== FSM ====================
class OrderForm(StatesGroup):
    account_country = State()
    account_address = State()
    stars_recipient_type = State()
    stars_amount = State()
    stars_username = State()
    screenshot = State()

class MailingForm(StatesGroup):
    waiting_for_message = State()

class PromoCreationForm(StatesGroup):
    code = State()
    discount = State()
    product_type = State()
    max_uses = State()

class UserPromoForm(StatesGroup):
    waiting_for_code = State()

# ==================== КЛАВИАТУРЫ ====================
def main_kb(user_id):
    buttons = [
        [InlineKeyboardButton(text="📱 Купить аккаунт", callback_data="buy_account")],
        [InlineKeyboardButton(text="⭐ Купить звёзды", callback_data="buy_stars")],
        [InlineKeyboardButton(text="👤 Личный кабинет", callback_data="profile")],
        [InlineKeyboardButton(text="📦 Мои заказы", callback_data="my_orders")],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="support")]
    ]
    if user_id in ADMIN_IDS:
        buttons.append([InlineKeyboardButton(text="🔐 Админ панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def country_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for code, c in COUNTRY_PRICES.items():
        text = f"{c['name']} — {c['price']}₽"
        if c.get('warning'):
            text += " ⚠️"
        kb.inline_keyboard.append([InlineKeyboardButton(text=text, callback_data=f"cnt_{code}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="back")])
    return kb

def stars_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for code, s in STARS_PRICES.items():
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"{s['name']} — {s['price']}₽", callback_data=f"stars_{code}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="back")])
    return kb

def stars_recipient_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Купить себе", callback_data="recipient_self")],
        [InlineKeyboardButton(text="👥 Купить другу", callback_data="recipient_friend")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back")]
    ])

def profile_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎫 Активировать промокод", callback_data="activate_promo")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back")]
    ])

def promo_type_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Для аккаунтов", callback_data="promo_type_account")],
        [InlineKeyboardButton(text="⭐ Для звёзд", callback_data="promo_type_stars")],
        [InlineKeyboardButton(text="◀ Отмена", callback_data="admin_panel")]
    ])

def admin_promocodes_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin_create_promo")],
        [InlineKeyboardButton(text="📋 Список промокодов", callback_data="admin_list_promos")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")]
    ])

def admin_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟡 Аккаунты на проверке", callback_data="admin_accounts_processing_page_0")],
        [InlineKeyboardButton(text="⭐ Звёзды на проверке", callback_data="admin_stars_processing_page_0")],
        [InlineKeyboardButton(text="✅ Подтверждённые", callback_data="admin_completed_page_0")],
        [InlineKeyboardButton(text="❌ Отклонённые", callback_data="admin_rejected_page_0")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📁 Экспорт CSV", callback_data="admin_export_menu")],
        [InlineKeyboardButton(text="🔒 Блокировка", callback_data="admin_block_menu")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_mailing")],
        [InlineKeyboardButton(text="📜 Логи", callback_data="admin_logs")],
        [InlineKeyboardButton(text="🎫 Промокоды", callback_data="admin_promocodes")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back")]
    ])

def admin_export_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Экспорт аккаунтов (CSV)", callback_data="export_accounts")],
        [InlineKeyboardButton(text="⭐ Экспорт звёзд (CSV)", callback_data="export_stars")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")]
    ])

def admin_block_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔒 Заблокировать", callback_data="block_user")],
        [InlineKeyboardButton(text="🔓 Разблокировать", callback_data="unblock_user")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")]
    ])

def admin_order_controls_kb(order_id, order_type):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_{order_type}_{order_id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{order_type}_{order_id}")],
        [InlineKeyboardButton(text="◀ Назад", callback_data=f"admin_{order_type}_processing_page_0")]
    ])

def admin_accounts_pagination_kb(status, current_page, total_pages):
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀◀", callback_data=f"admin_accounts_{status}_page_{current_page-1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"{current_page+1}/{total_pages}", callback_data="ignore"))
    if current_page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="▶▶", callback_data=f"admin_accounts_{status}_page_{current_page+1}"))
    if nav_buttons:
        kb.inline_keyboard.append(nav_buttons)
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")])
    return kb

def admin_stars_pagination_kb(current_page, total_pages):
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀◀", callback_data=f"admin_stars_processing_page_{current_page-1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"{current_page+1}/{total_pages}", callback_data="ignore"))
    if current_page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="▶▶", callback_data=f"admin_stars_processing_page_{current_page+1}"))
    if nav_buttons:
        kb.inline_keyboard.append(nav_buttons)
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")])
    return kb

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
async def log_admin_action(admin_id, action, target=""):
    q = "INSERT INTO admin_logs (admin_id, action, target) VALUES (?,?,?)"
    await run_sync_query(q, (admin_id, action, target), fetch=False)

async def get_admin_logs(limit=30):
    q = "SELECT admin_id, action, target, timestamp FROM admin_logs ORDER BY id DESC LIMIT ?"
    return await run_sync_query(q, (limit,))

# ==================== ХЕНДЛЕРЫ ====================
@dp.message(Command("start"))
async def start(m: types.Message, state: FSMContext):
    await state.clear()
    if await is_blocked(m.from_user.id):
        await m.answer("⛔ Вы заблокированы. Обратитесь к администратору.")
        return
    if not await is_subscribed(m.from_user.id):
        await m.answer("📢 Подпишитесь на канал, чтобы пользоваться магазином!", reply_markup=sub_kb())
        return
    await m.answer("🌟 Добро пожаловать в магазин!\n\n📱 Аккаунты Telegram\n⭐ Telegram Stars", reply_markup=main_kb(m.from_user.id))

@dp.callback_query(F.data == "check_sub")
async def check_sub(c: types.CallbackQuery):
    if await is_subscribed(c.from_user.id):
        await c.message.edit_text("✅ Доступ открыт", reply_markup=main_kb(c.from_user.id))
    else:
        await c.answer("❌ Не подписан!", show_alert=True)

@dp.callback_query(F.data == "back")
async def back(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("🌟 Главное меню", reply_markup=main_kb(c.from_user.id))

@dp.callback_query(F.data == "support")
async def support(c: types.CallbackQuery):
    await c.answer()
    await c.message.answer(f"🆘 Поддержка: {SUPPORT_USERNAME}")

@dp.callback_query(F.data == "my_orders")
async def my_orders(c: types.CallbackQuery):
    account_orders = await get_orders(c.from_user.id)
    stars_orders = await get_user_stars_orders(c.from_user.id)
    if not account_orders and not stars_orders:
        await c.answer("У вас пока нет заказов", show_alert=True)
        return
    text = "📦 *Мои заказы*\n\n"
    if account_orders:
        text += "📱 *Аккаунты:*\n"
        for o in account_orders:
            status_emoji = "✅" if o[8] == "completed" else "⏳" if o[8] == "processing" else "❌"
            status_text = "Оплачено" if o[8] == "completed" else "На проверке" if o[8] == "processing" else "Отклонён"
            created = datetime.strptime(o[9], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
            text += f"{status_emoji} *#{o[0]}* | {o[5]}₽ | {o[4]} | {status_text} | {created}\n"
    if stars_orders:
        text += "\n⭐ *Звёзды:*\n"
        for o in stars_orders:
            status_emoji = "✅" if o[7] == "completed" else "⏳" if o[7] == "processing" else "❌"
            status_text = "Оплачено" if o[7] == "completed" else "На проверке" if o[7] == "processing" else "Отклонён"
            created = datetime.strptime(o[8], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
            text += f"{status_emoji} *#{o[0]}* | {o[4]}₽ | {o[3]}⭐ | {status_text} | {created}\n"
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Назад", callback_data="back")]]))

# ==================== ЛИЧНЫЙ КАБИНЕТ ====================
@dp.callback_query(F.data == "profile")
async def profile(c: types.CallbackQuery):
    account_discount = await get_user_discount(c.from_user.id, "account")
    stars_discount = await get_user_discount(c.from_user.id, "stars")
    text = (
        f"👤 *Личный кабинет*\n\n"
        f"🆔 Ваш ID: `{c.from_user.id}`\n"
        f"👤 Username: @{c.from_user.username or 'не указан'}\n"
        f"📱 Скидка на аккаунты: {account_discount}%\n"
        f"⭐ Скидка на звёзды: {stars_discount}%\n\n"
        f"Выберите действие:"
    )
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=profile_kb())

@dp.callback_query(F.data == "activate_promo")
async def activate_promo_prompt(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserPromoForm.waiting_for_code)
    await c.message.edit_text(
        "🎫 *Активация промокода*\n\n"
        "Введите промокод. Если промокод для аккаунтов — скидка будет на аккаунты, если для звёзд — на звёзды.\n\n"
        "Пример: `SUMMER10`\n\n"
        "Для отмены нажмите /cancel",
        parse_mode="Markdown"
    )
    await c.answer()

@dp.message(UserPromoForm.waiting_for_code)
async def process_user_promo(m: types.Message, state: FSMContext):
    code = m.text.strip().upper()
    success, message = await activate_promocode_for_user(m.from_user.id, code, "account")
    if not success:
        success, message = await activate_promocode_for_user(m.from_user.id, code, "stars")
    if success:
        await m.answer(f"✅ {message}", parse_mode="Markdown")
        await log_admin_action(m.from_user.id, "activate_promo", f"{code}")
    else:
        await m.answer(f"❌ {message}", parse_mode="Markdown")
    await state.clear()
    await profile(m)

# ==================== ПОКУПКА АККАУНТА ====================
@dp.callback_query(F.data == "buy_account")
async def buy_account(c: types.CallbackQuery, state: FSMContext):
    if not await is_subscribed(c.from_user.id):
        await c.answer("Подпишитесь на канал!", show_alert=True)
        return
    await state.set_state(OrderForm.account_country)
    await c.message.edit_text("🌍 *Выберите страну аккаунта:*", parse_mode="Markdown", reply_markup=country_kb())

@dp.callback_query(OrderForm.account_country, F.data.startswith("cnt_"))
async def select_country(c: types.CallbackQuery, state: FSMContext):
    code = c.data.split("_")[1]
    country = COUNTRY_PRICES[code]
    original_price = country["price"]
    user_id = c.from_user.id
    discount = await get_user_discount(user_id, "account")
    if discount > 0:
        price = int(original_price * (100 - discount) / 100)
        discount_text = f"\n\n🎫 *Применена скидка {discount}%*"
        price_text = f"\n💰 *Было:* {original_price}₽\n💰 *Стало:* {price}₽"
    else:
        price = original_price
        discount_text = ""
        price_text = f"\n💰 *Итого к оплате:* {price}₽"
    await state.update_data(product_name=country["name"], price=price, original_price=original_price, discount=discount)
    await state.set_state(OrderForm.account_address)
    warning = f"\n\n{country['warning']}" if country.get("warning") else ""
    await c.message.edit_text(
        f"✅ *Страна:* {country['name']} — {original_price}₽{warning}{discount_text}{price_text}\n\n"
        f"📬 *Введите ваш Telegram username для доставки аккаунта:*\nПример: @durov",
        parse_mode="Markdown"
    )
    await c.answer()

@dp.message(OrderForm.account_address)
async def get_account_address(m: types.Message, state: FSMContext):
    addr = m.text.strip()
    if not addr.startswith("@"):
        addr = "@" + addr
    if len(addr) < 3:
        await m.answer("❌ Слишком короткий username. Пример: @durov")
        return
    data = await state.get_data()
    if not data.get("product_name"):
        await m.answer("❌ Ошибка. Начните заново /start")
        await state.clear()
        return
    final_price = data.get("price", data["original_price"])
    order_id = await save_order(m.from_user.id, m.from_user.username or "no_username", data["product_name"], final_price, addr)
    await state.update_data(order_id=order_id, order_type="account", address=addr, product_name=data["product_name"], price=final_price)
    await state.set_state(OrderForm.screenshot)
    await m.answer(
        f"💳 *ЗАКАЗ АККАУНТА #{order_id}*\n"
        f"💰 Сумма: {final_price}₽\n\n"
        f"📌 *Реквизиты для оплаты:*\n"
        f"📱 СБП: `{PHONE_NUMBER}`\n"
        f"👤 Получатель: {RECIPIENT_NAME}\n\n"
        f"✅ *После оплаты отправьте СКРИНШОТ чека сюда.*",
        parse_mode="Markdown"
    )

# ==================== ПОКУПКА ЗВЁЗД ====================
@dp.callback_query(F.data == "buy_stars")
async def buy_stars(c: types.CallbackQuery, state: FSMContext):
    if not await is_subscribed(c.from_user.id):
        await c.answer("Подпишитесь на канал!", show_alert=True)
        return
    await state.set_state(OrderForm.stars_recipient_type)
    await c.message.edit_text(
        "⭐ *Покупка звёзд*\n\n"
        "👤 *Кому начислить звёзды?*",
        parse_mode="Markdown",
        reply_markup=stars_recipient_kb()
    )
    await c.answer()

@dp.callback_query(OrderForm.stars_recipient_type, F.data.startswith("recipient_"))
async def select_recipient(c: types.CallbackQuery, state: FSMContext):
    recipient_type = c.data.split("_")[1]
    await state.update_data(recipient_type=recipient_type)
    if recipient_type == "self":
        if c.from_user.username:
            stars_username = "@" + c.from_user.username
            await state.update_data(stars_username=stars_username)
            await state.set_state(OrderForm.stars_amount)
            await c.message.edit_text(
                f"✅ *Начисление на:* {stars_username} (ваш аккаунт)\n\n"
                f"⭐ *Выберите количество звёзд:*",
                parse_mode="Markdown",
                reply_markup=stars_kb()
            )
        else:
            await state.set_state(OrderForm.stars_username)
            await c.message.edit_text(
                f"✅ *Вы выбрали «Купить себе»*\n\n"
                f"📬 *У вас нет username в Telegram.*\n"
                f"Пожалуйста, введите ваш @username для начисления звёзд:\n"
                f"Пример: @durov",
                parse_mode="Markdown"
            )
    else:
        await state.set_state(OrderForm.stars_username)
        await c.message.edit_text(
            f"✅ *Вы выбрали «Купить другу»*\n\n"
            f"📬 *Введите @username друга, на которого начислить звёзды:*\n"
            f"Пример: @durov",
            parse_mode="Markdown"
        )
    await c.answer()

@dp.message(OrderForm.stars_username)
async def get_stars_username(m: types.Message, state: FSMContext):
    stars_username = m.text.strip()
    if not stars_username.startswith("@"):
        stars_username = "@" + stars_username
    if len(stars_username) < 3:
        await m.answer("❌ Слишком короткий username. Пример: @durov")
        return
    data = await state.get_data()
    recipient_type = data.get("recipient_type")
    if recipient_type == "self":
        await state.update_data(stars_username=stars_username)
        await state.set_state(OrderForm.stars_amount)
        await m.answer(
            f"✅ *Начисление на:* {stars_username} (ваш аккаунт)\n\n"
            f"⭐ *Выберите количество звёзд:*",
            parse_mode="Markdown",
            reply_markup=stars_kb()
        )
    else:
        await state.update_data(stars_username=stars_username)
        await state.set_state(OrderForm.stars_amount)
        await m.answer(
            f"✅ *Начисление на:* {stars_username}\n\n"
            f"⭐ *Выберите количество звёзд:*",
            parse_mode="Markdown",
            reply_markup=stars_kb()
        )

@dp.callback_query(OrderForm.stars_amount, F.data.startswith("stars_"))
async def select_stars(c: types.CallbackQuery, state: FSMContext):
    code = c.data.split("_")[1]
    stars_data = STARS_PRICES[code]
    original_price = stars_data["price"]
    user_id = c.from_user.id
    discount = await get_user_discount(user_id, "stars")
    if discount > 0:
        price = int(original_price * (100 - discount) / 100)
        discount_text = f"\n\n🎫 *Применена скидка {discount}%*"
        price_text = f"\n💰 *Было:* {original_price}₽\n💰 *Стало:* {price}₽"
    else:
        price = original_price
        discount_text = ""
        price_text = f"\n💰 *Итого к оплате:* {price}₽"
    data = await state.get_data()
    stars_username = data.get("stars_username")
    await state.update_data(stars=stars_data["stars"], price=price, original_price=original_price, discount=discount)
    order_id = await save_stars_order(c.from_user.id, c.from_user.username or "no_username", stars_data["stars"], price, stars_username)
    await state.update_data(order_id=order_id, order_type="stars")
    await state.set_state(OrderForm.screenshot)
    await c.message.edit_text(
        f"💳 *ЗАКАЗ ЗВЁЗД #{order_id}*\n"
        f"⭐ {stars_data['stars']} Stars\n"
        f"💰 Сумма: {price}₽{discount_text}{price_text}\n"
        f"📬 Начисление на: {stars_username}\n\n"
        f"📌 *Реквизиты для оплаты:*\n"
        f"📱 СБП: `{PHONE_NUMBER}`\n"
        f"👤 Получатель: {RECIPIENT_NAME}\n\n"
        f"✅ *После оплаты отправьте СКРИНШОТ чека сюда.*",
        parse_mode="Markdown"
    )
    await c.answer()

# ==================== ОБРАБОТКА СКРИНШОТОВ ====================
@dp.message(OrderForm.screenshot, F.photo)
async def handle_screenshot(m: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    order_type = data.get("order_type")
    if not order_id or not order_type:
        await m.answer("❌ Ошибка. Начните заново /start")
        await state.clear()
        return
    file_id = m.photo[-1].file_id
    if order_type == "account":
        await update_screenshot(order_id, file_id)
        await update_status(order_id, "processing")
        product_name = data.get("product_name")
        price = data.get("price")
        address = data.get("address")
        await m.answer(
            f"📸 *Скриншот для заказа аккаунта #{order_id} получен!*\n\n"
            f"Вы купили: {product_name} — {price}₽\n"
            f"Доставка: {address}\n\n"
            f"Ожидайте проверки администратором.\n"
            f"Статус можно проверить в «Мои заказы».",
            parse_mode="Markdown"
        )
        for admin_id in ADMIN_IDS:
            await bot.send_message(
                admin_id,
                f"📦 *Новый заказ аккаунта #{order_id}*\n"
                f"👤 От: @{m.from_user.username or m.from_user.id}\n"
                f"📦 Товар: {product_name}\n"
                f"💰 Сумма: {price}₽\n"
                f"📬 Доставка: {address}",
                parse_mode="Markdown"
            )
    else:
        await update_stars_screenshot(order_id, file_id)
        await update_stars_status(order_id, "processing")
        stars = data.get("stars")
        price = data.get("price")
        stars_username = data.get("stars_username")
        await m.answer(
            f"📸 *Скриншот для заказа звёзд #{order_id} получен!*\n\n"
            f"Вы купили: {stars} Stars — {price}₽\n"
            f"Начисление на: {stars_username}\n\n"
            f"Ожидайте проверки администратором.\n"
            f"Статус можно проверить в «Мои заказы».",
            parse_mode="Markdown"
        )
        for admin_id in ADMIN_IDS:
            await bot.send_message(
                admin_id,
                f"⭐ *Новый заказ звёзд #{order_id}*\n"
                f"👤 От: @{m.from_user.username or m.from_user.id}\n"
                f"⭐ Количество: {stars} Stars\n"
                f"💰 Сумма: {price}₽\n"
                f"📬 Начисление на: {stars_username}",
                parse_mode="Markdown"
            )
    await state.clear()

@dp.message(OrderForm.screenshot)
async def wrong_screenshot(m: types.Message):
    await m.answer("❌ Отправьте именно ФОТО (скриншот чека).")

# ==================== АДМИН-ПАНЕЛЬ ====================
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("Доступ запрещён", show_alert=True)
        return
    await c.message.edit_text("🔐 *Админ панель*", parse_mode="Markdown", reply_markup=admin_main_kb())

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    stats = await get_stats()
    text = (
        "📊 *СТАТИСТИКА*\n\n"
        "📱 *Аккаунты:*\n"
        f"• Всего заказов: {stats['accounts']['total']}\n"
        f"• На проверке: {stats['accounts']['processing']}\n"
        f"• Ожидают оплаты: {stats['accounts']['pending']}\n"
        f"• Подтверждено: {stats['accounts']['completed']}\n"
        f"• Отклонено: {stats['accounts']['rejected']}\n"
        f"• Общая сумма (подтверждённых): {stats['accounts']['sum']}₽\n\n"
        "⭐ *Звёзды:*\n"
        f"• Всего заказов: {stats['stars']['total']}\n"
        f"• На проверке: {stats['stars']['processing']}\n"
        f"• Ожидают оплаты: {stats['stars']['pending']}\n"
        f"• Подтверждено: {stats['stars']['completed']}\n"
        f"• Отклонено: {stats['stars']['rejected']}\n"
        f"• Общая сумма (подтверждённых): {stats['stars']['sum']}₽\n\n"
        f"🔒 Заблокировано пользователей: {stats['blocked']}"
    )
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")]]))

@dp.callback_query(F.data == "admin_export_menu")
async def admin_export_menu(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    await c.message.edit_text("📁 *Экспорт данных*", parse_mode="Markdown", reply_markup=admin_export_kb())

@dp.callback_query(F.data == "export_accounts")
async def export_accounts(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    orders = await get_all_orders_for_export()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "User ID", "Username", "Товар", "Цена", "Адрес", "Статус", "Дата"])
    for o in orders:
        writer.writerow([o[0], o[1], o[2], o[4], o[5], o[6], o[8], o[9]])
    output.seek(0)
    file = io.BytesIO(output.getvalue().encode('utf-8-sig'))
    await c.message.answer_document(types.BufferedInputFile(file.getvalue(), filename="accounts_export.csv"), caption="📱 Экспорт заказов аккаунтов")
    await log_admin_action(c.from_user.id, "export_accounts")

@dp.callback_query(F.data == "export_stars")
async def export_stars(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    orders = await get_all_stars_orders_for_export()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "User ID", "Username", "Stars", "Цена", "Username получателя", "Статус", "Дата"])
    for o in orders:
        writer.writerow([o[0], o[1], o[2], o[3], o[4], o[5], o[7], o[8]])
    output.seek(0)
    file = io.BytesIO(output.getvalue().encode('utf-8-sig'))
    await c.message.answer_document(types.BufferedInputFile(file.getvalue(), filename="stars_export.csv"), caption="⭐ Экспорт заказов звёзд")
    await log_admin_action(c.from_user.id, "export_stars")

@dp.callback_query(F.data == "admin_block_menu")
async def admin_block_menu(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    await c.message.edit_text("🔒 *Блокировка / разблокировка*", parse_mode="Markdown", reply_markup=admin_block_kb())

@dp.callback_query(F.data == "block_user")
async def block_user_prompt(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    await state.set_state("waiting_block_id")
    await c.message.edit_text("Введите Telegram ID пользователя для блокировки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Отмена", callback_data="admin_panel")]]))

@dp.callback_query(F.data == "unblock_user")
async def unblock_user_prompt(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    await state.set_state("waiting_unblock_id")
    await c.message.edit_text("Введите Telegram ID пользователя для разблокировки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Отмена", callback_data="admin_panel")]]))

@dp.message(lambda m: m.state == "waiting_block_id")
async def process_block(m: types.Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return
    try:
        uid = int(m.text.strip())
        await block_user(uid)
        await m.answer(f"✅ Пользователь {uid} заблокирован.")
        await log_admin_action(m.from_user.id, "block_user", str(uid))
    except:
        await m.answer("❌ Неверный ID.")
    await state.clear()
    await m.answer("🔐 Админ панель", reply_markup=admin_main_kb())

@dp.message(lambda m: m.state == "waiting_unblock_id")
async def process_unblock(m: types.Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return
    try:
        uid = int(m.text.strip())
        await unblock_user(uid)
        await m.answer(f"✅ Пользователь {uid} разблокирован.")
        await log_admin_action(m.from_user.id, "unblock_user", str(uid))
    except:
        await m.answer("❌ Неверный ID.")
    await state.clear()
    await m.answer("🔐 Админ панель", reply_markup=admin_main_kb())

# ==================== РАССЫЛКА (ФОНОВАЯ, НЕ БЛОКИРУЕТ) ====================
async def send_mass_messages(users, text):
    """Отправляет сообщения пачками по 10, не блокируя event loop"""
    for i in range(0, len(users), 10):
        chunk = users[i:i+10]
        tasks = [bot.send_message(uid, text) for uid in chunk]
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(1)

@dp.callback_query(F.data == "admin_mailing")
async def admin_mailing(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(MailingForm.waiting_for_message)
    await c.message.edit_text("📨 *Рассылка*\nОтправьте сообщение для всех пользователей.\nДля отмены /cancel", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Отмена", callback_data="admin_panel")]]))

@dp.message(MailingForm.waiting_for_message)
async def process_mailing(m: types.Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return
    text = m.text
    users = await get_all_users_cached()
    if not users:
        await m.answer("Нет пользователей для рассылки.")
        await state.clear()
        return
    await m.answer(f"✅ Рассылка запущена. Будет отправлено {len(users)} сообщений. Это может занять некоторое время.")
    # Запускаем фоновую задачу, чтобы не блокировать ответ
    asyncio.create_task(send_mass_messages(users, text))
    await log_admin_action(m.from_user.id, "mailing", f"sent to {len(users)}")
    await state.clear()
    await m.answer("🔐 Админ панель", reply_markup=admin_main_kb())

@dp.callback_query(F.data == "admin_logs")
async def admin_logs(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    logs = await get_admin_logs(30)
    if not logs:
        text = "📜 *Логи действий*\nПока нет записей."
    else:
        text = "📜 *Последние 30 действий:*\n\n"
        for log in logs:
            text += f"👤 {log[0]} | {log[1]} {log[2]} | {log[3]}\n"
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")]]))

# ==================== ПРОСМОТР ЗАКАЗОВ НА ПРОВЕРКЕ (АККАУНТЫ) ====================
@dp.callback_query(F.data.startswith("admin_accounts_processing_page_"))
async def admin_accounts_processing(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    page = int(c.data.split("_")[-1])
    total = await get_orders_count_by_status("processing")
    total_pages = (total + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE if total > 0 else 1
    orders = await get_all_orders_by_status("processing", ORDERS_PER_PAGE, page * ORDERS_PER_PAGE)
    if not orders:
        await c.message.edit_text("📭 Нет заказов аккаунтов на проверке.", reply_markup=admin_accounts_pagination_kb("processing", page, total_pages))
        return
    text = f"🟡 *Аккаунты на проверке* (страница {page+1} из {total_pages})\n\n"
    for o in orders:
        text += f"┌ #{o[0]} | {o[5]}₽ | @{o[2] or o[1]} (id:{o[1]})\n└ 📬 {o[6]}\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for o in orders:
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"📦 Заказ #{o[0]}", callback_data=f"view_account_{o[0]}")])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀◀ Назад", callback_data=f"admin_accounts_processing_page_{page-1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ▶▶", callback_data=f"admin_accounts_processing_page_{page+1}"))
    if nav_buttons:
        kb.inline_keyboard.append(nav_buttons)
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")])
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data.startswith("view_account_"))
async def view_account_order(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    order_id = int(c.data.split("_")[-1])
    order = await get_order(order_id)
    if not order:
        await c.answer("Заказ не найден")
        return
    order_id, user_id, username, _, product_name, price, address, screenshot, status, created = order
    caption = f"🆕 *Заказ аккаунта #{order_id}*\n👤 @{username or user_id} (id:{user_id})\n📦 {product_name}\n💰 {price}₽\n📬 {address}\n🕒 {created}\n📸 Чек:"
    if screenshot:
        await c.message.answer_photo(photo=screenshot, caption=caption, parse_mode="Markdown", reply_markup=admin_order_controls_kb(order_id, "account"))
    else:
        await c.message.answer(caption, reply_markup=admin_order_controls_kb(order_id, "account"))

@dp.callback_query(F.data.startswith("admin_stars_processing_page_"))
async def admin_stars_processing(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    page = int(c.data.split("_")[-1])
    total = await get_stars_orders_count_by_status("processing")
    total_pages = (total + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE if total > 0 else 1
    orders = await get_stars_orders_by_status("processing", ORDERS_PER_PAGE, page * ORDERS_PER_PAGE)
    if not orders:
        await c.message.edit_text("📭 Нет заказов звёзд на проверке.", reply_markup=admin_stars_pagination_kb(page, total_pages))
        return
    text = f"⭐ *Звёзды на проверке* (страница {page+1} из {total_pages})\n\n"
    for o in orders:
        text += f"┌ #{o[0]} | {o[4]}₽ | {o[3]}⭐ | @{o[2] or o[1]} (id:{o[1]})\n└ 📬 Начисление: {o[5]}\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for o in orders:
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"⭐ Заказ #{o[0]}", callback_data=f"view_stars_{o[0]}")])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀◀ Назад", callback_data=f"admin_stars_processing_page_{page-1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ▶▶", callback_data=f"admin_stars_processing_page_{page+1}"))
    if nav_buttons:
        kb.inline_keyboard.append(nav_buttons)
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")])
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data.startswith("view_stars_"))
async def view_stars_order(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    order_id = int(c.data.split("_")[-1])
    order = await get_stars_order(order_id)
    if not order:
        await c.answer("Заказ не найден")
        return
    order_id, user_id, username, stars, price, stars_username, screenshot, status, created = order
    caption = f"⭐ *Заказ звёзд #{order_id}*\n👤 @{username or user_id} (id:{user_id})\n⭐ {stars} Stars\n💰 {price}₽\n📬 Начисление: {stars_username}\n🕒 {created}\n📸 Чек:"
    if screenshot:
        await c.message.answer_photo(photo=screenshot, caption=caption, parse_mode="Markdown", reply_markup=admin_order_controls_kb(order_id, "stars"))
    else:
        await c.message.answer(caption, reply_markup=admin_order_controls_kb(order_id, "stars"))

# ==================== ПОДТВЕРЖДЕНИЕ/ОТКЛОНЕНИЕ ====================
@dp.callback_query(F.data.startswith("confirm_account_"))
async def confirm_account_order(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("Только админ", show_alert=True)
        return
    order_id = int(c.data.split("_")[-1])
    await update_status(order_id, "completed")
    order = await get_order(order_id)
    if order:
        user_id = order[1]
        try:
            await bot.send_message(user_id, f"✅ *Заказ аккаунта #{order_id} подтверждён!*\nАккаунт придёт в течение 2-4 часов.\nПо вопросам: {SUPPORT_USERNAME}", parse_mode="Markdown")
        except:
            pass
    await c.message.edit_caption(caption=c.message.caption + "\n\n✅ ОПЛАЧЕНО")
    await log_admin_action(c.from_user.id, "confirm_account", str(order_id))
    await c.answer("Подтверждено")

@dp.callback_query(F.data.startswith("confirm_stars_"))
async def confirm_stars_order(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("Только админ", show_alert=True)
        return
    order_id = int(c.data.split("_")[-1])
    await update_stars_status(order_id, "completed")
    order = await get_stars_order(order_id)
    if order:
        user_id = order[1]
        try:
            await bot.send_message(user_id, f"✅ *Заказ звёзд #{order_id} подтверждён!*\nЗвёзды будут начислены в течение 2-4 часов.\nПо вопросам: {SUPPORT_USERNAME}", parse_mode="Markdown")
        except:
            pass
    await c.message.edit_caption(caption=c.message.caption + "\n\n✅ ОПЛАЧЕНО")
    await log_admin_action(c.from_user.id, "confirm_stars", str(order_id))
    await c.answer("Подтверждено")

@dp.callback_query(F.data.startswith("reject_account_"))
async def reject_account_order(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("Только админ", show_alert=True)
        return
    order_id = int(c.data.split("_")[-1])
    await update_status(order_id, "rejected")
    order = await get_order(order_id)
    if order:
        user_id = order[1]
        try:
            await bot.send_message(user_id, f"❌ *Заказ аккаунта #{order_id} отклонён*\nЧек некорректен. Попробуйте снова /start", parse_mode="Markdown")
        except:
            pass
    await c.message.edit_caption(caption=c.message.caption + "\n\n❌ ОТКЛОНЁН")
    await log_admin_action(c.from_user.id, "reject_account", str(order_id))
    await c.answer("Отклонён")

@dp.callback_query(F.data.startswith("reject_stars_"))
async def reject_stars_order(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("Только админ", show_alert=True)
        return
    order_id = int(c.data.split("_")[-1])
    await update_stars_status(order_id, "rejected")
    order = await get_stars_order(order_id)
    if order:
        user_id = order[1]
        try:
            await bot.send_message(user_id, f"❌ *Заказ звёзд #{order_id} отклонён*\nЧек некорректен. Попробуйте снова /start", parse_mode="Markdown")
        except:
            pass
    await c.message.edit_caption(caption=c.message.caption + "\n\n❌ ОТКЛОНЁН")
    await log_admin_action(c.from_user.id, "reject_stars", str(order_id))
    await c.answer("Отклонён")

# ==================== ПОДТВЕРЖДЁННЫЕ/ОТКЛОНЁННЫЕ ====================
@dp.callback_query(F.data.startswith("admin_completed_page_"))
async def admin_completed(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    page = int(c.data.split("_")[-1])
    account_orders = await get_all_orders_by_status("completed", ORDERS_PER_PAGE, page * ORDERS_PER_PAGE)
    stars_orders = await get_stars_orders_by_status("completed", ORDERS_PER_PAGE, page * ORDERS_PER_PAGE)
    total = await get_orders_count_by_status("completed") + await get_stars_orders_count_by_status("completed")
    total_pages = (total + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE if total > 0 else 1
    text = f"✅ *Подтверждённые заказы* (стр {page+1}/{total_pages})\n\n"
    for o in account_orders:
        created = datetime.strptime(o[9], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        text += f"┌ [#{o[0]}] Аккаунт | {o[5]}₽ | {o[4]} | id:{o[1]}\n└ 📅 {created} | 📬 {o[6]}\n\n"
    for o in stars_orders:
        created = datetime.strptime(o[8], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        text += f"┌ [#{o[0]}] Звёзды | {o[4]}₽ | {o[3]}⭐ | id:{o[1]}\n└ 📅 {created} | 📬 {o[5]}\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀◀ Назад", callback_data=f"admin_completed_page_{page-1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ▶▶", callback_data=f"admin_completed_page_{page+1}"))
    if nav_buttons:
        kb.inline_keyboard.append(nav_buttons)
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")])
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data.startswith("admin_rejected_page_"))
async def admin_rejected(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    page = int(c.data.split("_")[-1])
    account_orders = await get_all_orders_by_status("rejected", ORDERS_PER_PAGE, page * ORDERS_PER_PAGE)
    stars_orders = await get_stars_orders_by_status("rejected", ORDERS_PER_PAGE, page * ORDERS_PER_PAGE)
    total = await get_orders_count_by_status("rejected") + await get_stars_orders_count_by_status("rejected")
    total_pages = (total + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE if total > 0 else 1
    text = f"❌ *Отклонённые заказы* (стр {page+1}/{total_pages})\n\n"
    for o in account_orders:
        created = datetime.strptime(o[9], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        text += f"┌ [#{o[0]}] Аккаунт | {o[5]}₽ | {o[4]} | id:{o[1]}\n└ 📅 {created} | 📬 {o[6]}\n\n"
    for o in stars_orders:
        created = datetime.strptime(o[8], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        text += f"┌ [#{o[0]}] Звёзды | {o[4]}₽ | {o[3]}⭐ | id:{o[1]}\n└ 📅 {created} | 📬 {o[5]}\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀◀ Назад", callback_data=f"admin_rejected_page_{page-1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ▶▶", callback_data=f"admin_rejected_page_{page+1}"))
    if nav_buttons:
        kb.inline_keyboard.append(nav_buttons)
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")])
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)

# ==================== ПРОМОКОДЫ (АДМИН) ====================
@dp.callback_query(F.data == "admin_promocodes")
async def admin_promocodes_menu(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    await c.message.edit_text("🎫 *Управление промокодами*", parse_mode="Markdown", reply_markup=admin_promocodes_kb())

@dp.callback_query(F.data == "admin_create_promo")
async def admin_create_promo(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(PromoCreationForm.code)
    await c.message.edit_text(
        "🎫 *Создание промокода*\n\n"
        "1️⃣ Введите название промокода (буквы и цифры, без пробелов):\n"
        "Пример: `SUMMER10`\n\n"
        "Для отмены: /cancel",
        parse_mode="Markdown"
    )

@dp.message(PromoCreationForm.code)
async def promo_get_code(m: types.Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return
    code = m.text.strip().upper()
    if not code or not code.replace("_", "").isalnum():
        await m.answer("❌ Только буквы, цифры и _")
        return
    await state.update_data(code=code)
    await state.set_state(PromoCreationForm.discount)
    await m.answer("2️⃣ Введите процент скидки (1-100):\nПример: `10`")

@dp.message(PromoCreationForm.discount)
async def promo_get_discount(m: types.Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return
    try:
        discount = int(m.text.strip())
        if discount < 1 or discount > 100:
            raise ValueError
    except:
        await m.answer("❌ Введите число от 1 до 100")
        return
    await state.update_data(discount=discount)
    await state.set_state(PromoCreationForm.product_type)
    await m.answer(
        "3️⃣ Выберите тип товара, для которого будет действовать промокод:",
        reply_markup=promo_type_kb()
    )

@dp.callback_query(PromoCreationForm.product_type, F.data.startswith("promo_type_"))
async def promo_get_product_type(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    product_type = c.data.split("_")[-1]  # 'account' или 'stars'
    await state.update_data(product_type=product_type)
    await state.set_state(PromoCreationForm.max_uses)
    await c.message.edit_text(
        "4️⃣ Введите лимит активаций:\n"
        "• `0` = навсегда (безлимит)\n"
        "• `10` = можно использовать 10 раз\n\n"
        "Введите число:"
    )
    await c.answer()

@dp.message(PromoCreationForm.max_uses)
async def promo_get_max_uses(m: types.Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return
    try:
        max_uses = int(m.text.strip())
        if max_uses < 0:
            raise ValueError
    except:
        await m.answer("❌ Введите число (0 или больше)")
        return
    data = await state.get_data()
    code = data["code"]
    discount = data["discount"]
    product_type = data["product_type"]
    success = await create_promocode(code, discount, product_type, max_uses)
    if success:
        uses_text = "безлимит" if max_uses == 0 else f"{max_uses} раз"
        product_text = "аккаунты" if product_type == "account" else "звёзды"
        await m.answer(f"✅ Промокод `{code}` создан!\nСкидка {discount}% на {product_text}\nАктиваций: {uses_text}", parse_mode="Markdown")
        await log_admin_action(m.from_user.id, "create_promo", f"{code} ({discount}%, {product_text})")
    else:
        await m.answer(f"❌ Промокод `{code}` уже существует!", parse_mode="Markdown")
    await state.clear()
    await admin_promocodes_menu(m)

@dp.callback_query(F.data == "admin_list_promos")
async def admin_list_promos(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    promos = await get_all_promocodes_cached()
    if not promos:
        text = "📭 Список промокодов пуст"
    else:
        text = "🎫 *Список промокодов:*\n\n"
        for p in promos:
            uses_left = "∞" if p[4] == 0 else f"{p[4] - p[5]} из {p[4]}"
            product_text = "аккаунты" if p[3] == "account" else "звёзды"
            text += f"┌ `{p[1]}` | скидка {p[2]}% | для {product_text}\n"
            text += f"└ активаций: {uses_left} | создан: {p[6][:10]}\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить промокод", callback_data="admin_delete_promo")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="admin_promocodes")]
    ])
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data == "admin_delete_promo")
async def admin_delete_promo_prompt(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    await state.set_state("waiting_delete_promo")
    await c.message.edit_text("Введите название промокода для удаления:")

@dp.message(lambda m: m.state == "waiting_delete_promo")
async def process_delete_promo(m: types.Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return
    code = m.text.strip().upper()
    promos = await get_all_promocodes_cached()
    exists = any(p[1] == code for p in promos)
    if exists:
        await delete_promocode(code)
        await m.answer(f"✅ Промокод `{code}` удалён.", parse_mode="Markdown")
        await log_admin_action(m.from_user.id, "delete_promo", code)
    else:
        await m.answer(f"❌ Промокод `{code}` не найден.", parse_mode="Markdown")
    await state.clear()
    await admin_list_promos(m)

@dp.callback_query(F.data == "ignore")
async def ignore_callback(c: types.CallbackQuery):
    await c.answer()

# ==================== ЗАПУСК ВЕБХУКА ====================
async def on_startup():
    await bot.delete_webhook()
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Webhook set to {WEBHOOK_URL}")

async def handle_webhook(request):
    update = await request.json()
    await dp.feed_update(bot, types.Update(**update))
    return web.Response(status=200)

async def handle_root(request):
    return web.Response(text="OK", status=200)

def main():
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.on_startup.append(lambda _: asyncio.create_task(on_startup()))
    web.run_app(app, host='0.0.0.0', port=PORT)

if __name__ == "__main__":
    main()
