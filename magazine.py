#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
import os
import csv
import io
import time
import threading
from datetime import datetime
from flask import Flask
import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

load_dotenv()

# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "5895176743,6109321916").split(',')))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@HowScad_support")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "+79027365759")
RECIPIENT_NAME = os.getenv("RECIPIENT_NAME", "Егор")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@Howscard")
REQUIRED_CHANNEL_LINK = os.getenv("REQUIRED_CHANNEL_LINK", "https://t.me/Howscard")
PORT = int(os.getenv("PORT", "8080"))
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не задан (PostgreSQL)")

ORDERS_PER_PAGE = 3
PUBLIC_URL = os.getenv("PUBLIC_URL", f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME', 'localhost')}")

# ==================== КУРС ВАЛЮТ ====================
USD_RUB_RATE = float(os.getenv("USD_RUB_RATE", "90"))

# ==================== АДРЕС КОШЕЛЬКА ДЛЯ ОПЛАТЫ (USDT TRC20) ====================
USDT_ADDRESS = "TGWC7HxfPviogzscLJTUMzRjHt22t6NSbE"
USDT_NETWORK = "TRC20"

# ==================== ЦЕНЫ ====================
COUNTRY_PRICES = {
    "russia": {
        "name": "🇷🇺 Россия (новорег)", 
        "price_autoreg": 140,
        "price_phishing": None,
        "only_autoreg": True
    },
    "ukraine": {
        "name": "🇺🇦 Украина", 
        "price_autoreg": 160,
        "price_phishing": 105
    },
    "kazakhstan": {
        "name": "🇰🇿 Казахстан", 
        "price_autoreg": 135,
        "price_phishing": 90
    },
    "myanmar": {
        "name": "🇲🇲 Мьянма (Бирма)", 
        "price_autoreg": 65,
        "price_phishing": 50
    },
    "colombia": {
        "name": "🇨🇴 Колумбия", 
        "price_autoreg": 75,
        "price_phishing": 55
    },
    "thailand": {
        "name": "🇹🇭 Таиланд", 
        "price_autoreg": 90,
        "price_phishing": 65
    },
    "vietnam": {
        "name": "🇻🇳 Вьетнам", 
        "price_autoreg": 70,
        "price_phishing": 55
    },
    "uk": {
        "name": "🇬🇧 Великобритания", 
        "price_autoreg": 115,
        "price_phishing": 80
    },
    "iran": {
        "name": "🇮🇷 Иран", 
        "price_autoreg": 90,
        "price_phishing": 45
    },
    "brazil": {
        "name": "🇧🇷 Бразилия", 
        "price_autoreg": 95,
        "price_phishing": 70
    },
    "ethiopia": {
        "name": "🇪🇹 Эфиопия", 
        "price_autoreg": 90,
        "price_phishing": 65
    },
    "us": {
        "name": "🇺🇸 США", 
        "price_autoreg": 80,
        "price_phishing": 45
    },
    "canada": {
        "name": "🇨🇦 Канада", 
        "price_autoreg": 75,
        "price_phishing": 50
    },
    "india": {
        "name": "🇮🇳 Индия", 
        "price_autoreg": 60,
        "price_phishing": 45,
        "warning": "⚠️ Высокий шанс слёта"
    },
    "iraq": {
        "name": "🇮🇶 Ирак", 
        "price_autoreg": 90,
        "price_phishing": 45
    },
    "bangladesh": {
        "name": "🇧🇩 Бангладеш", 
        "price_autoreg": 90,
        "price_phishing": 55
    },
    "africa": {
        "name": "🌍 Африка", 
        "price_autoreg": 105,
        "price_phishing": 55
    },
    "philippines": {
        "name": "🇵🇭 Филиппины", 
        "price_autoreg": 75,
        "price_phishing": 45
    },
    "nigeria": {
        "name": "🇳🇬 Нигерия", 
        "price_autoreg": 75,
        "price_phishing": 45
    }
}

STARS_PRICES = {
    "50": {"name": "⭐ 50 Stars", "stars": 50, "price": 80},
    "100": {"name": "⭐ 100 Stars", "stars": 100, "price": 160},
    "250": {"name": "⭐ 250 Stars", "stars": 250, "price": 370},
    "400": {"name": "⭐ 400 Stars", "stars": 400, "price": 570},
    "500": {"name": "⭐ 500 Stars", "stars": 500, "price": 690},
    "750": {"name": "⭐ 750 Stars", "stars": 750, "price": 1020},
    "1000": {"name": "⭐ 1000 Stars", "stars": 1000, "price": 1420}
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ==================== СОСТОЯНИЯ FSM ====================
class OrderForm(StatesGroup):
    account_country = State()
    account_type = State()
    currency_choice = State()
    account_address = State()
    stars_recipient_type = State()
    stars_amount = State()
    stars_username = State()
    stars_currency_choice = State()
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

class BlockState(StatesGroup):
    waiting_block_id = State()
    waiting_unblock_id = State()

class DeletePromoState(StatesGroup):
    waiting_code = State()

# ==================== ПУЛ СОЕДИНЕНИЙ POSTGRESQL ====================
db_pool = None

async def init_db_pool():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=20, command_timeout=10)
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username TEXT,
                product_type TEXT DEFAULT 'account',
                product_name TEXT,
                price INTEGER,
                original_price INTEGER DEFAULT 0,
                discount_percent INTEGER DEFAULT 0,
                currency TEXT DEFAULT 'RUB',
                address TEXT,
                screenshot TEXT,
                status TEXT DEFAULT 'pending',
                payment_method TEXT,
                created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS currency TEXT DEFAULT 'RUB'")
        await conn.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_method TEXT")
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS stars_orders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username TEXT,
                stars INTEGER,
                price INTEGER,
                original_price INTEGER DEFAULT 0,
                discount_percent INTEGER DEFAULT 0,
                currency TEXT DEFAULT 'RUB',
                stars_username TEXT,
                screenshot TEXT,
                status TEXT DEFAULT 'pending',
                payment_method TEXT,
                created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute("ALTER TABLE stars_orders ADD COLUMN IF NOT EXISTS currency TEXT DEFAULT 'RUB'")
        await conn.execute("ALTER TABLE stars_orders ADD COLUMN IF NOT EXISTS payment_method TEXT")
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_promocodes (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                promo_code TEXT,
                discount_percent INTEGER,
                product_type TEXT,
                used BOOLEAN DEFAULT FALSE,
                activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute("ALTER TABLE user_promocodes ADD COLUMN IF NOT EXISTS used BOOLEAN DEFAULT FALSE")
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS blocked (
                user_id BIGINT PRIMARY KEY
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS admin_logs (
                id SERIAL PRIMARY KEY,
                admin_id BIGINT,
                action TEXT,
                target TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS promocodes (
                id SERIAL PRIMARY KEY,
                code TEXT UNIQUE,
                discount_percent INTEGER,
                product_type TEXT CHECK (product_type IN ('account', 'stars')),
                max_uses INTEGER,
                used_count INTEGER DEFAULT 0,
                created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_stars_status ON stars_orders(status)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_stars_user ON stars_orders(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_promocodes_user ON user_promocodes(user_id)")
        
    logger.info("База данных PostgreSQL инициализирована")

# ==================== КЭШИ ====================
_stats_cache = {}
_stats_cache_time = 0
_users_cache = []
_users_cache_time = 0
_promos_cache = []
_promos_cache_time = 0

# ==================== ФУНКЦИИ ДЛЯ РАБОТЫ С БД ====================
async def log_admin_action(admin_id, action, target=""):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO admin_logs (admin_id, action, target) VALUES ($1, $2, $3)", admin_id, action, target)

async def get_admin_logs(limit=30):
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT admin_id, action, target, timestamp FROM admin_logs ORDER BY id DESC LIMIT $1", limit)

async def save_order(uid, uname, product_name, price, original_price, discount_percent, addr, currency='RUB', payment_method=None):
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO orders (user_id, username, product_type, product_name, price, original_price, discount_percent, address, currency, payment_method) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id",
            uid, uname, 'account', product_name, price, original_price, discount_percent, addr, currency, payment_method
        )

async def update_screenshot(oid, fid):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE orders SET screenshot = $1 WHERE id = $2", fid, oid)

async def update_status(oid, status):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE orders SET status = $1 WHERE id = $2", status, oid)

async def get_orders(uid):
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM orders WHERE user_id = $1 ORDER BY id DESC", uid)

async def get_all_orders_by_status(status, limit=None, offset=None):
    async with db_pool.acquire() as conn:
        if limit is not None and offset is not None:
            return await conn.fetch("SELECT * FROM orders WHERE status = $1 ORDER BY id DESC LIMIT $2 OFFSET $3", status, limit, offset)
        else:
            return await conn.fetch("SELECT * FROM orders WHERE status = $1 ORDER BY id DESC", status)

async def get_orders_count_by_status(status):
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM orders WHERE status = $1", status)

async def get_order(oid):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM orders WHERE id = $1", oid)

async def get_all_orders_for_export():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM orders ORDER BY id DESC")

async def save_stars_order(uid, uname, stars, price, original_price, discount_percent, stars_username, currency='RUB', payment_method=None):
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO stars_orders (user_id, username, stars, price, original_price, discount_percent, stars_username, currency, payment_method) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id",
            uid, uname, stars, price, original_price, discount_percent, stars_username, currency, payment_method
        )

async def update_stars_screenshot(oid, fid):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE stars_orders SET screenshot = $1 WHERE id = $2", fid, oid)

async def update_stars_status(oid, status):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE stars_orders SET status = $1 WHERE id = $2", status, oid)

async def get_stars_orders_by_status(status, limit=None, offset=None):
    async with db_pool.acquire() as conn:
        if limit is not None and offset is not None:
            return await conn.fetch("SELECT * FROM stars_orders WHERE status = $1 ORDER BY id DESC LIMIT $2 OFFSET $3", status, limit, offset)
        else:
            return await conn.fetch("SELECT * FROM stars_orders WHERE status = $1 ORDER BY id DESC", status)

async def get_stars_orders_count_by_status(status):
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM stars_orders WHERE status = $1", status)

async def get_stars_order(oid):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM stars_orders WHERE id = $1", oid)

async def get_user_stars_orders(uid):
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM stars_orders WHERE user_id = $1 ORDER BY id DESC", uid)

async def get_all_stars_orders_for_export():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM stars_orders ORDER BY id DESC")

async def create_promocode(code, discount_percent, product_type, max_uses):
    async with db_pool.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO promocodes (code, discount_percent, product_type, max_uses) VALUES ($1, $2, $3, $4)",
                code.upper(), discount_percent, product_type, max_uses
            )
            return True
        except:
            return False

async def get_all_promocodes_cached():
    global _promos_cache, _promos_cache_time
    if time.time() - _promos_cache_time > 60:
        async with db_pool.acquire() as conn:
            _promos_cache = await conn.fetch("SELECT * FROM promocodes ORDER BY id DESC")
        _promos_cache_time = time.time()
    return _promos_cache

async def delete_promocode(code):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM promocodes WHERE code = $1", code.upper())
    global _promos_cache_time
    _promos_cache_time = 0

async def activate_promocode_for_user(user_id, code, product_type):
    async with db_pool.acquire() as conn:
        promo = await conn.fetchrow("SELECT code, discount_percent, max_uses, used_count FROM promocodes WHERE code = $1 AND product_type = $2", code.upper(), product_type)
        if not promo:
            return False, "Промокод не найден"
        max_uses = promo['max_uses']
        used = promo['used_count']
        if max_uses != 0 and used >= max_uses:
            return False, "Промокод уже использован максимальное количество раз"
        user_has = await conn.fetchval("SELECT 1 FROM user_promocodes WHERE user_id = $1 AND promo_code = $2 AND product_type = $3 AND used = FALSE", user_id, code.upper(), product_type)
        if user_has:
            return False, "Вы уже активировали этот промокод (и ещё не использовали)"
        await conn.execute("INSERT INTO user_promocodes (user_id, promo_code, discount_percent, product_type, used) VALUES ($1, $2, $3, $4, FALSE)",
                           user_id, code.upper(), promo['discount_percent'], product_type)
        await conn.execute("UPDATE promocodes SET used_count = used_count + 1 WHERE code = $1", code.upper())
        global _promos_cache_time
        _promos_cache_time = 0
        return True, f"Промокод {code.upper()} активирован! Скидка {promo['discount_percent']}% на {'аккаунты' if product_type == 'account' else 'звёзды'}"

async def get_user_discount(user_id, product_type):
    async with db_pool.acquire() as conn:
        res = await conn.fetchval(
            "SELECT discount_percent FROM user_promocodes WHERE user_id = $1 AND product_type = $2 AND used = FALSE LIMIT 1",
            user_id, product_type
        )
        return res or 0

async def mark_discount_used(user_id, product_type):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_promocodes SET used = TRUE WHERE user_id = $1 AND product_type = $2 AND used = FALSE",
            user_id, product_type
        )

async def get_stats():
    global _stats_cache, _stats_cache_time
    if time.time() - _stats_cache_time < 60 and _stats_cache:
        return _stats_cache
    async with db_pool.acquire() as conn:
        total_accounts = await conn.fetchval("SELECT COUNT(*) FROM orders")
        total_accounts_sum = await conn.fetchval("SELECT COALESCE(SUM(price), 0) FROM orders WHERE status='completed'")
        pending_accounts = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE status='pending'")
        processing_accounts = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE status='processing'")
        completed_accounts = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE status='completed'")
        rejected_accounts = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE status='rejected'")
        total_stars = await conn.fetchval("SELECT COUNT(*) FROM stars_orders")
        total_stars_sum = await conn.fetchval("SELECT COALESCE(SUM(price), 0) FROM stars_orders WHERE status='completed'")
        pending_stars = await conn.fetchval("SELECT COUNT(*) FROM stars_orders WHERE status='pending'")
        processing_stars = await conn.fetchval("SELECT COUNT(*) FROM stars_orders WHERE status='processing'")
        completed_stars = await conn.fetchval("SELECT COUNT(*) FROM stars_orders WHERE status='completed'")
        rejected_stars = await conn.fetchval("SELECT COUNT(*) FROM stars_orders WHERE status='rejected'")
        blocked_count = await conn.fetchval("SELECT COUNT(*) FROM blocked")
    _stats_cache = {
        "accounts": {"total": total_accounts, "sum": total_accounts_sum, "pending": pending_accounts, "processing": processing_accounts, "completed": completed_accounts, "rejected": rejected_accounts},
        "stars": {"total": total_stars, "sum": total_stars_sum, "pending": pending_stars, "processing": processing_stars, "completed": completed_stars, "rejected": rejected_stars},
        "blocked": blocked_count,
    }
    _stats_cache_time = time.time()
    return _stats_cache

async def block_user(uid):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO blocked (user_id) VALUES ($1) ON CONFLICT DO NOTHING", uid)

async def unblock_user(uid):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM blocked WHERE user_id = $1", uid)

async def is_blocked(uid):
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT 1 FROM blocked WHERE user_id = $1", uid) is not None

async def get_all_users_cached():
    global _users_cache, _users_cache_time
    if time.time() - _users_cache_time < 300:
        return _users_cache
    async with db_pool.acquire() as conn:
        rows1 = await conn.fetch("SELECT DISTINCT user_id FROM orders")
        rows2 = await conn.fetch("SELECT DISTINCT user_id FROM stars_orders")
        users = {r['user_id'] for r in rows1} | {r['user_id'] for r in rows2}
        _users_cache = list(users)
        _users_cache_time = time.time()
    return _users_cache

# ==================== ОТПРАВКА СООБЩЕНИЙ ====================
async def safe_send_message(user_id, text, parse_mode="Markdown", reply_markup=None):
    try:
        await bot.send_message(user_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Ошибка отправки пользователю {user_id}: {e}")

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

# ==================== ГЛАВНОЕ МЕНЮ ====================
def main_kb(user_id):
    buttons = [
        [InlineKeyboardButton(text="📱 Купить аккаунт", callback_data="buy_account")],
        [InlineKeyboardButton(text="⭐ Купить звёзды", callback_data="buy_stars")],
        [InlineKeyboardButton(text="👤 Личный кабинет", callback_data="profile")],
        [InlineKeyboardButton(text="📦 Мои заказы", callback_data="my_orders")],
        [InlineKeyboardButton(text="⭐ Отзывы", url="https://t.me/otzvrutake")],
        [InlineKeyboardButton(text="🆘 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME.replace('@', '')}")],
    ]
    if user_id in ADMIN_IDS:
        buttons.append([InlineKeyboardButton(text="🔐 Админ панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def show_main_menu(message_or_callback, user_id):
    text = "🚀 *Добро пожаловать в HowScard!*\n\n🛒 Ваш надёжный магазин цифровых товаров\n\n👇 Выберите нужный раздел:"
    
    if hasattr(message_or_callback, 'message'):
        await message_or_callback.message.edit_text(text, parse_mode="Markdown", reply_markup=main_kb(user_id))
    else:
        await message_or_callback.answer(text, parse_mode="Markdown", reply_markup=main_kb(user_id))

# ==================== КЛАВИАТУРЫ ====================
def country_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    country_keys = list(COUNTRY_PRICES.keys())
    for i in range(0, len(country_keys), 2):
        row = []
        key1 = country_keys[i]
        c1 = COUNTRY_PRICES[key1]
        text1 = c1['name']
        if c1.get('warning'):
            text1 += " ⚠️"
        row.append(InlineKeyboardButton(text=text1, callback_data=f"cnt_{key1}"))
        if i + 1 < len(country_keys):
            key2 = country_keys[i+1]
            c2 = COUNTRY_PRICES[key2]
            text2 = c2['name']
            if c2.get('warning'):
                text2 += " ⚠️"
            row.append(InlineKeyboardButton(text=text2, callback_data=f"cnt_{key2}"))
        kb.inline_keyboard.append(row)
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="back")])
    return kb

def account_type_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Авторег", callback_data="type_autoreg")],
        [InlineKeyboardButton(text="🎣 Фишинг", callback_data="type_phishing")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_to_country")]
    ])

def currency_kb(product_type="account"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Рубли (СБП)", callback_data=f"currency_rub_{product_type}")],
        [InlineKeyboardButton(text="💎 Доллары (USDT TRC20)", callback_data=f"currency_usd_{product_type}")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back_to_country")]
    ])

def stars_currency_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Рубли (СБП)", callback_data="stars_currency_rub")],
        [InlineKeyboardButton(text="💎 Доллары (USDT TRC20)", callback_data="stars_currency_usd")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back")]
    ])

# Кнопка оплаты
def send_payment_kb(order_id, order_type):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📋 Скопировать адрес",
            callback_data="copy_address"
        )],
        [InlineKeyboardButton(
            text="💳 Открыть @send",
            url="https://t.me/send"
        )],
        [InlineKeyboardButton(
            text="✅ Я оплатил", 
            callback_data=f"paid_{order_type}_{order_id}"
        )],
        [InlineKeyboardButton(text="◀ Отмена", callback_data="back")]
    ])

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

# ==================== ОСНОВНЫЕ ХЕНДЛЕРЫ ====================
@dp.message(Command("start"))
async def start(m: types.Message, state: FSMContext):
    await state.clear()
    if await is_blocked(m.from_user.id):
        await m.answer("⛔ Вы заблокированы. Обратитесь к администратору.")
        return
    if not await is_subscribed(m.from_user.id):
        await m.answer("📢 Подпишитесь на канал, чтобы пользоваться магазином!", reply_markup=sub_kb())
        return
    await show_main_menu(m, m.from_user.id)

@dp.callback_query(F.data == "check_sub")
async def check_sub(c: types.CallbackQuery):
    if await is_subscribed(c.from_user.id):
        await show_main_menu(c, c.from_user.id)
    else:
        await c.answer("❌ Не подписан!", show_alert=True)

@dp.callback_query(F.data == "back")
async def back(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_main_menu(c, c.from_user.id)

@dp.callback_query(F.data == "back_to_country")
async def back_to_country(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(OrderForm.account_country)
    await c.message.edit_text("🌍 *Выберите страну аккаунта:*", parse_mode="Markdown", reply_markup=country_kb())

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
            status_emoji = "✅" if o['status'] == "completed" else "⏳" if o['status'] == "processing" else "❌"
            status_text = "Оплачено" if o['status'] == "completed" else "На проверке" if o['status'] == "processing" else "Отклонён"
            created = o['created'].strftime("%d.%m %H:%M")
            currency = o.get('currency', 'RUB')
            currency_symbol = "$" if currency == "USD" else "₽"
            price_info = ""
            if o['discount_percent'] > 0:
                price_info = f"~~{o['original_price']}{currency_symbol}~~ → {o['price']}{currency_symbol} (скидка {o['discount_percent']}%)"
            else:
                price_info = f"{o['price']}{currency_symbol}"
            text += f"{status_emoji} *#{o['id']}* | {price_info} | {o['product_name']} | {status_text} | {created}\n"
    if stars_orders:
        text += "\n⭐ *Звёзды:*\n"
        for o in stars_orders:
            status_emoji = "✅" if o['status'] == "completed" else "⏳" if o['status'] == "processing" else "❌"
            status_text = "Оплачено" if o['status'] == "completed" else "На проверке" if o['status'] == "processing" else "Отклонён"
            created = o['created'].strftime("%d.%m %H:%M")
            currency = o.get('currency', 'RUB')
            currency_symbol = "$" if currency == "USD" else "₽"
            price_info = ""
            if o['discount_percent'] > 0:
                price_info = f"~~{o['original_price']}{currency_symbol}~~ → {o['price']}{currency_symbol} (скидка {o['discount_percent']}%)"
            else:
                price_info = f"{o['price']}{currency_symbol}"
            text += f"{status_emoji} *#{o['id']}* | {price_info} | {o['stars']}⭐ | {status_text} | {created}\n"
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Назад", callback_data="back")]]))

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

@dp.message(StateFilter(UserPromoForm.waiting_for_code))
async def process_user_promo(m: types.Message, state: FSMContext):
    code = m.text.strip().upper()
    success, message = await activate_promocode_for_user(m.from_user.id, code, "account")
    if not success:
        success, message = await activate_promocode_for_user(m.from_user.id, code, "stars")
    if success:
        await m.answer(f"✅ {message}\n\n⚠️ Скидка будет применена к следующему заказу и сгорит после использования.", parse_mode="Markdown")
        await log_admin_action(m.from_user.id, "activate_promo", f"{code}")
        await state.clear()
    else:
        await m.answer(f"❌ {message}", parse_mode="Markdown")
    await state.clear()
    await profile(m)

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
    user_id = c.from_user.id
    discount = await get_user_discount(user_id, "account")
    
    await state.update_data(
        country_code=code,
        country_name=country["name"],
        discount=discount
    )
    
    if country.get("only_autoreg"):
        original_price = country["price_autoreg"]
        await state.update_data(
            price_autoreg=original_price,
            original_price_autoreg=original_price,
            price_phishing=None,
            original_price_phishing=None,
            account_type="autoreg"
        )
        await state.set_state(OrderForm.currency_choice)
        await c.message.edit_text(
            f"✅ *Страна:* {country['name']}\n"
            f"🤖 Авторег — {original_price}₽\n\n"
            f"💳 *Выберите способ оплаты:*",
            parse_mode="Markdown",
            reply_markup=currency_kb("account")
        )
        await c.answer()
        return
    
    original_price_autoreg = country["price_autoreg"]
    original_price_phishing = country["price_phishing"]
    
    await state.update_data(
        original_price_autoreg=original_price_autoreg,
        original_price_phishing=original_price_phishing,
        price_autoreg=original_price_autoreg,
        price_phishing=original_price_phishing
    )
    await state.set_state(OrderForm.account_type)
    
    warning = f"\n\n{country['warning']}" if country.get("warning") else ""
    text = (
        f"✅ *Страна:* {country['name']}{warning}\n\n"
        f"🔍 *Выберите тип аккаунта:*\n\n"
        f"🤖 Авторег — {original_price_autoreg}₽"
    )
    if original_price_phishing is not None:
        text += f"\n🎣 Фишинг — {original_price_phishing}₽"
    
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=account_type_kb())
    await c.answer()

@dp.callback_query(OrderForm.account_type, F.data.startswith("type_"))
async def select_account_type(c: types.CallbackQuery, state: FSMContext):
    type_ = c.data.split("_")[1]
    data = await state.get_data()
    country_name = data.get("country_name")
    
    if type_ == "autoreg":
        price = data.get("original_price_autoreg")
        type_label = "Авторег"
    else:
        price = data.get("original_price_phishing")
        type_label = "Фишинг"
    
    await state.update_data(
        account_type=type_,
        price=price,
        original_price=price,
        product_name=f"{country_name} ({type_label})"
    )
    await state.set_state(OrderForm.currency_choice)
    
    await c.message.edit_text(
        f"✅ *Страна:* {country_name}\n"
        f"🔍 *Тип:* {type_label}\n"
        f"💰 *Сумма:* {price}₽\n\n"
        f"💳 *Выберите способ оплаты:*",
        parse_mode="Markdown",
        reply_markup=currency_kb("account")
    )
    await c.answer()

@dp.callback_query(OrderForm.currency_choice, F.data.startswith("currency_"))
async def select_currency(c: types.CallbackQuery, state: FSMContext):
    data = c.data.split("_")
    currency = data[1]
    product_type = data[2]
    
    state_data = await state.get_data()
    price_rub = state_data.get("price") or state_data.get("original_price")
    
    if currency == "usd":
        # Конвертируем в USD по курсу
        price_usd = round(price_rub / USD_RUB_RATE, 2)
        
        discount = state_data.get("discount", 0)
        if discount > 0:
            discounted_price = int(price_rub * (100 - discount) / 100)
            discounted_price_usd = round(discounted_price / USD_RUB_RATE, 2)
            discount_text = f"\n🎫 Скидка {discount}%: ~~{price_usd}$~~ → *{discounted_price_usd}$*"
            final_price = discounted_price_usd
        else:
            discount_text = ""
            final_price = price_usd
        
        product_name = state_data.get("product_name", "Аккаунт")
        
        # Сохраняем заказ
        order_id = await save_order(
            c.from_user.id,
            c.from_user.username or "no_username",
            product_name,
            final_price,
            price_rub,
            discount,
            "Оплата через USDT TRC20",
            "USD",
            "crypto"
        )
        
        if discount > 0:
            await mark_discount_used(c.from_user.id, "account")
        
        await state.update_data(order_id=order_id, order_type="account")
        
        await c.message.edit_text(
            f"💎 *Оплата в долларах (USDT на {USDT_NETWORK})*\n\n"
            f"📬 *Адрес для оплаты:*\n"
            f"`{USDT_ADDRESS}`\n"
            f"🌐 Сеть: *{USDT_NETWORK}*\n\n"
            f"📌 *Как оплатить:*\n"
            f"1️⃣ Откройте @send\n"
            f"2️⃣ Нажмите «Отправить» → выберите USDT\n"
            f"3️⃣ Вставьте адрес выше\n"
            f"4️⃣ Введите сумму\n"
            f"5️⃣ Отправьте и нажмите «✅ Я оплатил»\n\n"
            f"⚠️ *Важно:* Отправляйте ТОЛЬКО USDT в сети {USDT_NETWORK}!",
            parse_mode="Markdown",
            reply_markup=send_payment_kb(order_id, "account")
        )
        return
    
    else:
        # Рубли - СБП
        await state.update_data(
            currency="RUB",
            payment_method="sbp"
        )
        await state.set_state(OrderForm.account_address)
        
        discount = state_data.get("discount", 0)
        if discount > 0:
            discounted_price = int(price_rub * (100 - discount) / 100)
            discount_text = f"\n🎫 Скидка {discount}%: ~~{price_rub}₽~~ → *{discounted_price}₽*"
            final_price = discounted_price
        else:
            discount_text = ""
            final_price = price_rub
        
        await state.update_data(price=final_price)
        
        product_name = state_data.get("product_name", "Аккаунт")
        
        await c.message.edit_text(
            f"✅ *Товар:* {product_name}\n"
            f"🇷🇺 *Оплата в рублях (СБП)*\n\n"
            f"💰 Сумма: *{final_price}₽{discount_text}*\n\n"
            f"📱 СБП: `{PHONE_NUMBER}`\n"
            f"👤 Получатель: {RECIPIENT_NAME}\n\n"
            f"📬 *Введите ваш Telegram username для доставки аккаунта:*\n"
            f"Пример: @durov",
            parse_mode="Markdown"
        )
        await c.answer()

@dp.message(StateFilter(OrderForm.account_address))
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
    
    # Рублёвый заказ
    if data.get("currency") == "RUB":
        final_price = data.get("price", data.get("original_price"))
        discount_percent = data.get("discount", 0)
        original_price = data.get("original_price", final_price)
        
        order_id = await save_order(
            m.from_user.id, 
            m.from_user.username or "no_username", 
            data["product_name"], 
            final_price, 
            original_price, 
            discount_percent, 
            addr,
            "RUB",
            "sbp"
        )
        
        if discount_percent > 0:
            await mark_discount_used(m.from_user.id, "account")
        
        await state.update_data(order_id=order_id, order_type="account", address=addr)
        await state.set_state(OrderForm.screenshot)
        
        await m.answer(
            f"💳 *ЗАКАЗ АККАУНТА #{order_id}*\n"
            f"📦 {data['product_name']}\n"
            f"📱 СБП: `{PHONE_NUMBER}`\n"
            f"👤 Получатель: {RECIPIENT_NAME}\n"
            f"💰 Сумма: {final_price}₽\n\n"
            f"✅ *После оплаты отправьте СКРИНШОТ чека сюда.*",
            parse_mode="Markdown"
        )
    else:
        await m.answer(
            f"✅ Username {addr} сохранён!\n"
            f"Ожидайте подтверждения оплаты.",
            parse_mode="Markdown"
        )
        await state.clear()

@dp.callback_query(F.data.startswith("paid_account_"))
async def paid_account(c: types.CallbackQuery):
    """Пользователь нажал 'Я оплатил' для аккаунта"""
    order_id = int(c.data.split("_")[-1])
    
    order = await get_order(order_id)
    if not order or order['user_id'] != c.from_user.id:
        await c.answer("❌ Заказ не найден", show_alert=True)
        return
    
    # Меняем статус на processing
    await update_status(order_id, "processing")
    
    await c.message.edit_text(
        f"✅ *Оплата подтверждена!*\n"
        f"Заказ аккаунта #{order_id} отправлен на проверку.\n"
        f"Ожидайте подтверждения администратора.\n\n"
        f"Аккаунт будет доставлен в течение 2-4 часов.",
        parse_mode="Markdown"
    )
    
    # Уведомляем админов
    for admin_id in ADMIN_IDS:
        await safe_send_message(
            admin_id,
            f"💳 *Поступила оплата через @send*\n"
            f"📦 Заказ аккаунта #{order_id}\n"
            f"👤 Пользователь: @{c.from_user.username or c.from_user.id}\n"
            f"💰 Сумма: {order['price']}$",
            parse_mode="Markdown"
        )
    
    await c.answer("✅ Оплата подтверждена!")

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

@dp.message(StateFilter(OrderForm.stars_username))
async def get_stars_username(m: types.Message, state: FSMContext):
    stars_username = m.text.strip()
    if not stars_username.startswith("@"):
        stars_username = "@" + stars_username
    if len(stars_username) < 3:
        await m.answer("❌ Слишком короткий username. Пример: @durov")
        return
    data = await state.get_data()
    recipient_type = data.get("recipient_type")
    
    await state.update_data(stars_username=stars_username)
    await state.set_state(OrderForm.stars_amount)
    
    if recipient_type == "self":
        await m.answer(
            f"✅ *Начисление на:* {stars_username} (ваш аккаунт)\n\n"
            f"⭐ *Выберите количество звёзд:*",
            parse_mode="Markdown",
            reply_markup=stars_kb()
        )
    else:
        await m.answer(
            f"✅ *Начисление на:* {stars_username}\n\n"
            f"⭐ *Выберите количество звёзд:*",
            parse_mode="Markdown",
            reply_markup=stars_kb()
        )
        try:
            await m.answer(
                f"📨 *Уведомление*\n\n"
                f"Вы покупаете звёзды для @{stars_username.replace('@', '')}!\n"
                f"После подтверждения оплаты звёзды будут начислены.",
                parse_mode="Markdown"
            )
        except:
            pass

@dp.callback_query(OrderForm.stars_amount, F.data.startswith("stars_"))
async def select_stars(c: types.CallbackQuery, state: FSMContext):
    code = c.data.split("_")[1]
    stars_data = STARS_PRICES[code]
    original_price_rub = stars_data["price"]
    user_id = c.from_user.id
    discount = await get_user_discount(user_id, "stars")
    
    await state.update_data(
        stars=stars_data["stars"],
        original_price_rub=original_price_rub,
        discount=discount
    )
    
    await state.set_state(OrderForm.stars_currency_choice)
    await c.message.edit_text(
        f"⭐ *Звёзды:* {stars_data['stars']} Stars\n"
        f"💰 *Сумма:* {original_price_rub}₽\n\n"
        f"💳 *Выберите способ оплаты:*",
        parse_mode="Markdown",
        reply_markup=stars_currency_kb()
    )
    await c.answer()

@dp.callback_query(OrderForm.stars_currency_choice, F.data.startswith("stars_currency_"))
async def select_stars_currency(c: types.CallbackQuery, state: FSMContext):
    currency = c.data.split("_")[-1]
    
    state_data = await state.get_data()
    price_rub = state_data.get("original_price_rub")
    stars = state_data.get("stars")
    stars_username = state_data.get("stars_username")
    discount = state_data.get("discount", 0)
    
    if currency == "usd":
        price_usd = round(price_rub / USD_RUB_RATE, 2)
        
        if discount > 0:
            discounted_price_rub = int(price_rub * (100 - discount) / 100)
            discounted_price_usd = round(discounted_price_rub / USD_RUB_RATE, 2)
            discount_text = f"\n🎫 Скидка {discount}%: ~~{price_usd}$~~ → *{discounted_price_usd}$*"
            final_price = discounted_price_usd
        else:
            discount_text = ""
            final_price = price_usd
        
        # Создаём заказ
        order_id = await save_stars_order(
            c.from_user.id,
            c.from_user.username or "no_username",
            stars,
            final_price,
            price_rub,
            discount,
            stars_username,
            "USD",
            "crypto"
        )
        
        if discount > 0:
            await mark_discount_used(c.from_user.id, "stars")
        
        await state.update_data(order_id=order_id, order_type="stars")
        
        await c.message.edit_text(
            f"⭐ *Звёзды:* {stars} Stars\n"
            f"💎 *Оплата в долларах (USDT на {USDT_NETWORK})*\n\n"
            f"📬 *Адрес для оплаты:*\n"
            f"`{USDT_ADDRESS}`\n"
            f"🌐 Сеть: *{USDT_NETWORK}*\n\n"
            f"📌 *Как оплатить:*\n"
            f"1️⃣ Откройте @send\n"
            f"2️⃣ Нажмите «Отправить» → выберите USDT\n"
            f"3️⃣ Вставьте адрес выше\n"
            f"4️⃣ Введите сумму\n"
            f"5️⃣ Отправьте и нажмите «✅ Я оплатил»\n\n"
            f"⚠️ *Важно:* Отправляйте ТОЛЬКО USDT в сети {USDT_NETWORK}!",
            parse_mode="Markdown",
            reply_markup=send_payment_kb(order_id, "stars")
        )
        return
    
    else:
        # Рубли
        if discount > 0:
            discounted_price_rub = int(price_rub * (100 - discount) / 100)
            discount_text = f"\n🎫 Скидка {discount}%: ~~{price_rub}₽~~ → *{discounted_price_rub}₽*"
            final_price = discounted_price_rub
        else:
            discount_text = ""
            final_price = price_rub
        
        order_id = await save_stars_order(
            c.from_user.id,
            c.from_user.username or "no_username",
            stars,
            final_price,
            price_rub,
            discount,
            stars_username,
            "RUB",
            "sbp"
        )
        
        if discount > 0:
            await mark_discount_used(c.from_user.id, "stars")
        
        await state.update_data(order_id=order_id, order_type="stars", price=final_price)
        await state.set_state(OrderForm.screenshot)
        
        await c.message.edit_text(
            f"💳 *ЗАКАЗ ЗВЁЗД #{order_id}*\n"
            f"⭐ {stars} Stars\n"
            f"🇷🇺 *Оплата в рублях (СБП)*\n\n"
            f"💰 Сумма: *{final_price}₽{discount_text}*\n"
            f"📱 СБП: `{PHONE_NUMBER}`\n"
            f"👤 Получатель: {RECIPIENT_NAME}\n"
            f"📬 Начисление на: {stars_username}\n\n"
            f"✅ *После оплаты отправьте СКРИНШОТ чека сюда.*",
            parse_mode="Markdown"
        )
        await c.answer()

@dp.callback_query(F.data.startswith("paid_stars_"))
async def paid_stars(c: types.CallbackQuery):
    """Пользователь нажал 'Я оплатил' для звёзд"""
    order_id = int(c.data.split("_")[-1])
    
    order = await get_stars_order(order_id)
    if not order or order['user_id'] != c.from_user.id:
        await c.answer("❌ Заказ не найден", show_alert=True)
        return
    
    await update_stars_status(order_id, "processing")
    
    await c.message.edit_text(
        f"✅ *Оплата подтверждена!*\n"
        f"Заказ звёзд #{order_id} отправлен на проверку.\n"
        f"Ожидайте подтверждения администратора.\n\n"
        f"Звёзды будут начислены в течение 2-4 часов.",
        parse_mode="Markdown"
    )
    
    for admin_id in ADMIN_IDS:
        await safe_send_message(
            admin_id,
            f"💳 *Поступила оплата через @send*\n"
            f"⭐ Заказ звёзд #{order_id}\n"
            f"👤 Пользователь: @{c.from_user.username or c.from_user.id}\n"
            f"💰 Сумма: {order['price']}$",
            parse_mode="Markdown"
        )
    
    await c.answer("✅ Оплата подтверждена!")

@dp.message(StateFilter(OrderForm.screenshot), F.photo)
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
            await safe_send_message(admin_id, f"📦 *Новый заказ аккаунта #{order_id}*\n👤 От: @{m.from_user.username or m.from_user.id}\n📦 Товар: {product_name}\n💰 Сумма: {price}₽\n📬 Доставка: {address}", parse_mode="Markdown")
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
            await safe_send_message(admin_id, f"⭐ *Новый заказ звёзд #{order_id}*\n👤 От: @{m.from_user.username or m.from_user.id}\n⭐ Количество: {stars} Stars\n💰 Сумма: {price}₽\n📬 Начисление на: {stars_username}", parse_mode="Markdown")
    await state.clear()

@dp.message(StateFilter(OrderForm.screenshot))
async def wrong_screenshot(m: types.Message):
    await m.answer("❌ Отправьте именно ФОТО (скриншот чека).")

@dp.callback_query(F.data == "copy_address")
async def copy_address(c: types.CallbackQuery):
    await c.answer(f"✅ Адрес скопирован!\n\n{USDT_ADDRESS}", show_alert=True)

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
    writer.writerow(["ID", "User ID", "Username", "Товар", "Цена", "Было", "Скидка %", "Валюта", "Адрес", "Статус", "Дата"])
    for o in orders:
        currency = o.get('currency', 'RUB')
        writer.writerow([o['id'], o['user_id'], o['username'], o['product_name'], o['price'], o['original_price'], o['discount_percent'], currency, o['address'], o['status'], o['created'].strftime("%Y-%m-%d %H:%M:%S")])
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
    writer.writerow(["ID", "User ID", "Username", "Stars", "Цена", "Было", "Скидка %", "Валюта", "Username получателя", "Статус", "Дата"])
    for o in orders:
        currency = o.get('currency', 'RUB')
        writer.writerow([o['id'], o['user_id'], o['username'], o['stars'], o['price'], o['original_price'], o['discount_percent'], currency, o['stars_username'], o['status'], o['created'].strftime("%Y-%m-%d %H:%M:%S")])
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
    await state.set_state(BlockState.waiting_block_id)
    await c.message.edit_text("Введите Telegram ID пользователя для блокировки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Отмена", callback_data="admin_panel")]]))

@dp.callback_query(F.data == "unblock_user")
async def unblock_user_prompt(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(BlockState.waiting_unblock_id)
    await c.message.edit_text("Введите Telegram ID пользователя для разблокировки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Отмена", callback_data="admin_panel")]]))

@dp.message(StateFilter(BlockState.waiting_block_id))
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

@dp.message(StateFilter(BlockState.waiting_unblock_id))
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

async def send_mass_messages(users, text):
    for i in range(0, len(users), 10):
        chunk = users[i:i+10]
        tasks = [safe_send_message(uid, text) for uid in chunk]
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(1)

@dp.callback_query(F.data == "admin_mailing")
async def admin_mailing(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(MailingForm.waiting_for_message)
    await c.message.edit_text("📨 *Рассылка*\nОтправьте сообщение для всех пользователей.\nДля отмены /cancel", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Отмена", callback_data="admin_panel")]]))

@dp.message(StateFilter(MailingForm.waiting_for_message))
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
            text += f"👤 {log['admin_id']} | {log['action']} {log['target']} | {log['timestamp']}\n"
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")]]))

# ==================== ПРОСМОТР ЗАКАЗОВ ====================
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
        currency = o.get('currency', 'RUB')
        currency_symbol = "$" if currency == "USD" else "₽"
        price_info = ""
        if o['discount_percent'] > 0:
            price_info = f"~~{o['original_price']}{currency_symbol}~~ → {o['price']}{currency_symbol} (скидка {o['discount_percent']}%)"
        else:
            price_info = f"{o['price']}{currency_symbol}"
        text += f"┌ #{o['id']} | {price_info} | @{o['username'] or o['user_id']} (id:{o['user_id']})\n└ 📬 {o['address']}\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for o in orders:
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"📦 Заказ #{o['id']}", callback_data=f"view_account_{o['id']}")])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀◀", callback_data=f"admin_accounts_processing_page_{page-1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="▶▶", callback_data=f"admin_accounts_processing_page_{page+1}"))
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
    currency = order.get('currency', 'RUB')
    currency_symbol = "$" if currency == "USD" else "₽"
    price_info = ""
    if order['discount_percent'] > 0:
        price_info = f"\n💰 *Было:* ~~{order['original_price']}{currency_symbol}~~\n💰 *Стало:* {order['price']}{currency_symbol}\n💸 *Скидка:* {order['discount_percent']}%"
    else:
        price_info = f"\n💰 *Сумма:* {order['price']}{currency_symbol}"
    
    caption = (
        f"🆕 *Заказ аккаунта #{order['id']}*\n"
        f"👤 Пользователь: @{order['username'] or order['user_id']} (id: `{order['user_id']}`)\n"
        f"📦 Товар: {order['product_name']}\n"
        f"{price_info}\n"
        f"💳 *Валюта:* {currency}\n"
        f"📬 Доставка: {order['address']}\n"
        f"🕒 Создан: {order['created']}\n"
        f"📊 Статус: {'На проверке' if order['status'] == 'processing' else order['status']}\n\n"
        f"📸 Чек:"
    )
    if order['screenshot']:
        await c.message.answer_photo(photo=order['screenshot'], caption=caption, parse_mode="Markdown", reply_markup=admin_order_controls_kb(order_id, "account"))
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
        currency = o.get('currency', 'RUB')
        currency_symbol = "$" if currency == "USD" else "₽"
        price_info = ""
        if o['discount_percent'] > 0:
            price_info = f"~~{o['original_price']}{currency_symbol}~~ → {o['price']}{currency_symbol} (скидка {o['discount_percent']}%)"
        else:
            price_info = f"{o['price']}{currency_symbol}"
        text += f"┌ #{o['id']} | {price_info} | {o['stars']}⭐ | @{o['username'] or o['user_id']} (id:{o['user_id']})\n└ 📬 Начисление: {o['stars_username']}\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for o in orders:
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"⭐ Заказ #{o['id']}", callback_data=f"view_stars_{o['id']}")])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀◀", callback_data=f"admin_stars_processing_page_{page-1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="▶▶", callback_data=f"admin_stars_processing_page_{page+1}"))
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
    currency = order.get('currency', 'RUB')
    currency_symbol = "$" if currency == "USD" else "₽"
    price_info = ""
    if order['discount_percent'] > 0:
        price_info = f"\n💰 *Было:* ~~{order['original_price']}{currency_symbol}~~\n💰 *Стало:* {order['price']}{currency_symbol}\n💸 *Скидка:* {order['discount_percent']}%"
    else:
        price_info = f"\n💰 *Сумма:* {order['price']}{currency_symbol}"
    
    caption = (
        f"⭐ *Заказ звёзд #{order['id']}*\n"
        f"👤 Пользователь: @{order['username'] or order['user_id']} (id: `{order['user_id']}`)\n"
        f"⭐ Количество: {order['stars']} Stars\n"
        f"{price_info}\n"
        f"💳 *Валюта:* {currency}\n"
        f"📬 Начисление на: {order['stars_username']}\n"
        f"🕒 Создан: {order['created']}\n"
        f"📊 Статус: {'На проверке' if order['status'] == 'processing' else order['status']}\n\n"
        f"📸 Чек:"
    )
    if order['screenshot']:
        await c.message.answer_photo(photo=order['screenshot'], caption=caption, parse_mode="Markdown", reply_markup=admin_order_controls_kb(order_id, "stars"))
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
        currency = order.get('currency', 'RUB')
        currency_symbol = "$" if currency == "USD" else "₽"
        price_info = ""
        if order['discount_percent'] > 0:
            price_info = f"~~{order['original_price']}{currency_symbol}~~ → {order['price']}{currency_symbol} (скидка {order['discount_percent']}%)"
        else:
            price_info = f"{order['price']}{currency_symbol}"
        await safe_send_message(order['user_id'], f"✅ *Заказ аккаунта #{order_id} подтверждён!*\n\n📦 {order['product_name']}\n💰 {price_info}\n\nАккаунт придёт в течение 2-4 часов.\nПо вопросам: {SUPPORT_USERNAME}", parse_mode="Markdown")
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
        currency = order.get('currency', 'RUB')
        currency_symbol = "$" if currency == "USD" else "₽"
        price_info = ""
        if order['discount_percent'] > 0:
            price_info = f"~~{order['original_price']}{currency_symbol}~~ → {order['price']}{currency_symbol} (скидка {order['discount_percent']}%)"
        else:
            price_info = f"{order['price']}{currency_symbol}"
        await safe_send_message(order['user_id'], f"✅ *Заказ звёзд #{order_id} подтверждён!*\n\n⭐ {order['stars']} Stars\n💰 {price_info}\n📬 Начисление на: {order['stars_username']}\n\nЗвёзды будут начислены в течение 2-4 часов.\nПо вопросам: {SUPPORT_USERNAME}", parse_mode="Markdown")
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
        await safe_send_message(order['user_id'], f"❌ *Заказ аккаунта #{order_id} отклонён*\n\nЧек некорректен. Попробуйте снова /start", parse_mode="Markdown")
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
        await safe_send_message(order['user_id'], f"❌ *Заказ звёзд #{order_id} отклонён*\n\nЧек некорректен. Попробуйте снова /start", parse_mode="Markdown")
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
        created = o['created'].strftime("%d.%m %H:%M")
        currency = o.get('currency', 'RUB')
        currency_symbol = "$" if currency == "USD" else "₽"
        price_info = ""
        if o['discount_percent'] > 0:
            price_info = f"~~{o['original_price']}{currency_symbol}~~ → {o['price']}{currency_symbol} (скидка {o['discount_percent']}%)"
        else:
            price_info = f"{o['price']}{currency_symbol}"
        text += f"┌ [#{o['id']}] Аккаунт | {price_info} | {o['product_name']} | id:{o['user_id']}\n└ 📅 {created} | 📬 {o['address']}\n\n"
    for o in stars_orders:
        created = o['created'].strftime("%d.%m %H:%M")
        currency = o.get('currency', 'RUB')
        currency_symbol = "$" if currency == "USD" else "₽"
        price_info = ""
        if o['discount_percent'] > 0:
            price_info = f"~~{o['original_price']}{currency_symbol}~~ → {o['price']}{currency_symbol} (скидка {o['discount_percent']}%)"
        else:
            price_info = f"{o['price']}{currency_symbol}"
        text += f"┌ [#{o['id']}] Звёзды | {price_info} | {o['stars']}⭐ | id:{o['user_id']}\n└ 📅 {created} | 📬 {o['stars_username']}\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀◀", callback_data=f"admin_completed_page_{page-1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="▶▶", callback_data=f"admin_completed_page_{page+1}"))
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
        created = o['created'].strftime("%d.%m %H:%M")
        currency = o.get('currency', 'RUB')
        currency_symbol = "$" if currency == "USD" else "₽"
        price_info = ""
        if o['discount_percent'] > 0:
            price_info = f"~~{o['original_price']}{currency_symbol}~~ → {o['price']}{currency_symbol} (скидка {o['discount_percent']}%)"
        else:
            price_info = f"{o['price']}{currency_symbol}"
        text += f"┌ [#{o['id']}] Аккаунт | {price_info} | {o['product_name']} | id:{o['user_id']}\n└ 📅 {created} | 📬 {o['address']}\n\n"
    for o in stars_orders:
        created = o['created'].strftime("%d.%m %H:%M")
        currency = o.get('currency', 'RUB')
        currency_symbol = "$" if currency == "USD" else "₽"
        price_info = ""
        if o['discount_percent'] > 0:
            price_info = f"~~{o['original_price']}{currency_symbol}~~ → {o['price']}{currency_symbol} (скидка {o['discount_percent']}%)"
        else:
            price_info = f"{o['price']}{currency_symbol}"
        text += f"┌ [#{o['id']}] Звёзды | {price_info} | {o['stars']}⭐ | id:{o['user_id']}\n└ 📅 {created} | 📬 {o['stars_username']}\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀◀", callback_data=f"admin_rejected_page_{page-1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="▶▶", callback_data=f"admin_rejected_page_{page+1}"))
    if nav_buttons:
        kb.inline_keyboard.append(nav_buttons)
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")])
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)

# ==================== ПРОМОКОДЫ ====================
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

@dp.message(StateFilter(PromoCreationForm.code))
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

@dp.message(StateFilter(PromoCreationForm.discount))
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
    product_type = c.data.split("_")[-1]
    await state.update_data(product_type=product_type)
    await state.set_state(PromoCreationForm.max_uses)
    await c.message.edit_text(
        "4️⃣ Введите лимит активаций:\n"
        "• `0` = навсегда (безлимит)\n"
        "• `1` = один раз (рекомендуется)\n"
        "• `10` = 10 раз\n\n"
        "Введите число (рекомендуется 1 для одноразового промокода):"
    )
    await c.answer()

@dp.message(StateFilter(PromoCreationForm.max_uses))
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
            uses_left = "∞" if p['max_uses'] == 0 else f"{p['max_uses'] - p['used_count']} из {p['max_uses']}"
            product_text = "аккаунты" if p['product_type'] == "account" else "звёзды"
            text += f"┌ `{p['code']}` | скидка {p['discount_percent']}% | для {product_text}\n"
            text += f"└ активаций: {uses_left} | создан: {p['created'].strftime('%Y-%m-%d')}\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить промокод", callback_data="admin_delete_promo")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="admin_promocodes")]
    ])
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data == "admin_delete_promo")
async def admin_delete_promo_prompt(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(DeletePromoState.waiting_code)
    await c.message.edit_text("Введите название промокода для удаления:")

@dp.message(StateFilter(DeletePromoState.waiting_code))
async def process_delete_promo(m: types.Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return
    code = m.text.strip().upper()
    promos = await get_all_promocodes_cached()
    exists = any(p['code'] == code for p in promos)
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

# ==================== FLASK ДЛЯ HEALTHCHECK ====================
flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return "OK", 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

async def self_pinger():
    url = f"{PUBLIC_URL}/"
    while True:
        await asyncio.sleep(240)
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        logger.info("Self-ping отправлен")
        except Exception as e:
            logger.error(f"Self-ping ошибка: {e}")

# ==================== ЗАПУСК ====================
async def main():
    await init_db_pool()
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.create_task(self_pinger())
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен с кошельком USDT TRC20 и оплатой через @send")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
