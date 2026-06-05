#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
import os
import secrets
import time
import csv
import io
from datetime import datetime, timedelta
from enum import Enum
from urllib.parse import unquote
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import CommandStart, StateFilter, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
import aiosqlite
import aiohttp

load_dotenv()

# ========== НАСТРОЙКА ==========
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "8276815852,8840342301").split(',')))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@HowScad_support")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "+79027365759")
RECIPIENT_NAME = os.getenv("RECIPIENT_NAME", "Егор")
PORT = int(os.getenv("PORT", "8080"))

# Настройки
ORDER_TIMEOUT_HOURS = 24

COUNTRIES = {
    "us": {"name": "🇺🇸 США (+1)", "price": 80},
    "ca": {"name": "🇨🇦 Канада (+1)", "price": 65},
    "ru": {"name": "🇷🇺 Россия (новорег)", "price": 140},
    "bd": {"name": "🇧🇩 Бангладеш (+880)", "price": 90},
    "ph": {"name": "🇵🇭 Филиппины", "price": 65},
    "ng": {"name": "🇳🇬 Нигерия", "price": 60},
    "iq": {"name": "🇮🇶 Ирак", "price": 60},
    "af": {"name": "🌍 Африка", "price": 60},
    "in": {"name": "🇮🇳 Индия", "price": 50, "warning": "⚠️ Высокий шанс слёта"}
}
STARS_PACKS = {50:70, 100:140, 250:350, 400:550, 500:670, 750:1000, 1000:1400}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ========== БАЗА ДАННЫХ ==========
DB_NAME = "shop.db"
_db = None

async def get_db():
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_NAME)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
    return _db

async def init_db():
    db = await get_db()
    await db.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        product_type TEXT,
        product_name TEXT,
        product_price INTEGER,
        product_details TEXT,
        screenshot_file_id TEXT,
        status TEXT DEFAULT 'waiting_confirmation',
        created_at INTEGER DEFAULT (strftime('%s', 'now')),
        source TEXT DEFAULT 'unknown',
        confirmed_by INTEGER DEFAULT NULL,
        confirmed_at INTEGER DEFAULT NULL
    )''')
    await db.execute('''CREATE TABLE IF NOT EXISTS blocked_users (
        user_id INTEGER PRIMARY KEY,
        reason TEXT,
        blocked_at INTEGER DEFAULT (strftime('%s', 'now'))
    )''')
    await db.execute('''CREATE TABLE IF NOT EXISTS admin_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        target_user INTEGER DEFAULT NULL,
        order_id INTEGER DEFAULT NULL,
        details TEXT,
        created_at INTEGER DEFAULT (strftime('%s', 'now'))
    )''')
    await db.execute('''CREATE TABLE IF NOT EXISTS broadcast_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER NOT NULL,
        message_text TEXT,
        recipients_count INTEGER,
        sent_at INTEGER DEFAULT (strftime('%s', 'now'))
    )''')
    await db.execute('''CREATE TABLE IF NOT EXISTS user_stats (
        user_id INTEGER PRIMARY KEY,
        total_orders INTEGER DEFAULT 0,
        total_spent INTEGER DEFAULT 0,
        last_order_at INTEGER DEFAULT NULL
    )''')
    await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_user_stats_spent ON user_stats(total_spent)")
    await db.commit()
    logger.info("База данных инициализирована")

async def save_order(user_id, username, product_type, product_name, price, details, source):
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO orders (user_id, username, product_type, product_name, product_price, product_details, source) VALUES (?,?,?,?,?,?,?)",
        (user_id, username, product_type, product_name, price, details, source)
    )
    await db.commit()
    order_id = cursor.lastrowid
    
    # Обновляем статистику пользователя
    await db.execute('''
        INSERT INTO user_stats (user_id, total_orders, total_spent, last_order_at)
        VALUES (?, 1, ?, strftime('%s', 'now'))
        ON CONFLICT(user_id) DO UPDATE SET
            total_orders = total_orders + 1,
            total_spent = total_spent + ?,
            last_order_at = strftime('%s', 'now')
    ''', (user_id, price, price))
    await db.commit()
    return order_id

async def update_order_screenshot(order_id, file_id):
    db = await get_db()
    await db.execute("UPDATE orders SET screenshot_file_id = ? WHERE id = ?", (file_id, order_id))
    await db.commit()

async def process_order(order_id, new_status, admin_id=None):
    db = await get_db()
    await db.execute(
        "UPDATE orders SET status = ?, confirmed_by = ?, confirmed_at = strftime('%s', 'now') WHERE id = ? AND status = 'waiting_confirmation'",
        (new_status, admin_id, order_id)
    )
    await db.commit()
    cursor = await db.execute("SELECT user_id, product_name, product_price FROM orders WHERE id = ?", (order_id,))
    return await cursor.fetchone()

async def get_pending_orders():
    db = await get_db()
    cursor = await db.execute("SELECT * FROM orders WHERE status='waiting_confirmation' ORDER BY id DESC")
    return await cursor.fetchall()

async def get_pending_count():
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) as count FROM orders WHERE status='waiting_confirmation'")
    row = await cursor.fetchone()
    return row[0] if row else 0

async def get_all_orders(limit=200):
    db = await get_db()
    cursor = await db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))
    return await cursor.fetchall()

async def get_user_orders(user_id):
    db = await get_db()
    cursor = await db.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC", (user_id,))
    return await cursor.fetchall()

async def search_orders_by_query(query):
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM orders WHERE username LIKE ? OR user_id LIKE ? ORDER BY id DESC LIMIT 50",
        (f"%{query}%", f"%{query}%")
    )
    return await cursor.fetchall()

async def search_order_by_id(order_id):
    db = await get_db()
    cursor = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    return await cursor.fetchone()

async def is_user_blocked(user_id):
    db = await get_db()
    cursor = await db.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (user_id,))
    return await cursor.fetchone() is not None

async def block_user(user_id, reason=None, admin_id=None):
    db = await get_db()
    await db.execute("INSERT OR IGNORE INTO blocked_users (user_id, reason) VALUES (?, ?)", (user_id, reason))
    await db.commit()
    if admin_id:
        await db.execute("INSERT INTO admin_logs (admin_id, action, target_user, details) VALUES (?, 'block_user', ?, ?)",
                        (admin_id, user_id, reason))
        await db.commit()

async def unblock_user(user_id, admin_id=None):
    db = await get_db()
    await db.execute("DELETE FROM blocked_users WHERE user_id=?", (user_id,))
    await db.commit()
    if admin_id:
        await db.execute("INSERT INTO admin_logs (admin_id, action, target_user) VALUES (?, 'unblock_user', ?)",
                        (admin_id, user_id))
        await db.commit()

async def get_all_users():
    db = await get_db()
    cursor = await db.execute("SELECT DISTINCT user_id, username FROM orders ORDER BY user_id")
    return await cursor.fetchall()

async def get_stats():
    db = await get_db()
    
    cursor = await db.execute("SELECT COUNT(*) as total, SUM(product_price) as total_sum FROM orders")
    total = await cursor.fetchone()
    
    cursor = await db.execute("""
        SELECT status, COUNT(*) as count, SUM(product_price) as sum 
        FROM orders GROUP BY status
    """)
    status_stats = await cursor.fetchall()
    
    cursor = await db.execute("SELECT COUNT(DISTINCT user_id) as unique_users FROM orders")
    users = await cursor.fetchone()
    
    today_start = int(datetime.now().replace(hour=0, minute=0, second=0).timestamp())
    cursor = await db.execute("SELECT COUNT(*) as today_orders, SUM(product_price) as today_sum FROM orders WHERE created_at >= ?", (today_start,))
    today = await cursor.fetchone()
    
    week_start = int((datetime.now() - timedelta(days=7)).timestamp())
    cursor = await db.execute("SELECT COUNT(*) as week_orders, SUM(product_price) as week_sum FROM orders WHERE created_at >= ?", (week_start,))
    week = await cursor.fetchone()
    
    month_start = int((datetime.now() - timedelta(days=30)).timestamp())
    cursor = await db.execute("SELECT COUNT(*) as month_orders, SUM(product_price) as month_sum FROM orders WHERE created_at >= ?", (month_start,))
    month = await cursor.fetchone()
    
    cursor = await db.execute("SELECT COUNT(*) as blocked FROM blocked_users")
    blocked = await cursor.fetchone()
    
    cursor = await db.execute("SELECT AVG(product_price) as avg_price FROM orders WHERE status='confirmed'")
    avg = await cursor.fetchone()
    
    return {
        "total_orders": total[0] or 0,
        "total_sum": total[1] or 0,
        "unique_users": users[0] or 0,
        "today_orders": today[0] or 0,
        "today_sum": today[1] or 0,
        "week_orders": week[0] or 0,
        "week_sum": week[1] or 0,
        "month_orders": month[0] or 0,
        "month_sum": month[1] or 0,
        "blocked_users": blocked[0] or 0,
        "avg_check": avg[0] or 0,
        "status_stats": [dict(s) for s in status_stats]
    }

async def get_top_users(limit=10):
    db = await get_db()
    cursor = await db.execute(
        "SELECT user_id, username, total_orders, total_spent FROM user_stats ORDER BY total_spent DESC LIMIT ?",
        (limit,)
    )
    return await cursor.fetchall()

async def get_admin_logs(limit=50):
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM admin_logs ORDER BY created_at DESC LIMIT ?",
        (limit,)
    )
    return await cursor.fetchall()

async def save_broadcast_history(admin_id, message_text, recipients_count):
    db = await get_db()
    await db.execute(
        "INSERT INTO broadcast_history (admin_id, message_text, recipients_count) VALUES (?, ?, ?)",
        (admin_id, message_text[:500], recipients_count)
    )
    await db.commit()

def is_admin(user_id):
    return user_id in ADMIN_IDS

# ========== ХРАНИЛИЩЕ СЕССИЙ ==========
pending_sessions = {}

# ========== AIOHTTP СЕРВЕР ==========
from aiohttp.web import middleware

@middleware
async def cors_middleware(request, handler):
    if request.method == 'OPTIONS':
        resp = web.Response()
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, GET, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp
    resp = await handler(request)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

async def health_check(request):
    return web.Response(text="OK", status=200)

async def register_session(request):
    try:
        data = await request.json()
        user_id = data.get("user_id")
        product_name = data.get("product_name")
        product_price = data.get("product_price")
        username = data.get("username")
        
        if not user_id:
            return web.json_response({"status": "error", "error": "no user_id"}, status=400)
        
        session_id = secrets.token_hex(16)
        pending_sessions[session_id] = {
            "user_id": user_id,
            "product_name": product_name,
            "product_price": product_price,
            "username": username,
            "expires": time.time() + 300,
            "used": False
        }
        logger.info(f"Сессия {session_id[:8]} создана для {user_id}")
        return web.json_response({"status": "ok", "session_id": session_id})
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return web.json_response({"status": "error", "error": str(e)}, status=500)

async def run_aiohttp():
    app = web.Application()
    app.middlewares.append(cors_middleware)
    app.router.add_get('/', health_check)
    app.router.add_post('/register_session', register_session)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"HTTP сервер на порту {PORT}")

# ========== СОСТОЯНИЯ ==========
class OrderForm(StatesGroup):
    waiting_for_country = State()
    waiting_for_stars = State()
    waiting_for_screenshot = State()

class AdminState(StatesGroup):
    waiting_for_block_user = State()
    waiting_for_unblock_user = State()
    waiting_for_broadcast = State()
    waiting_for_search = State()

# ========== КЛАВИАТУРЫ ==========
def main_menu(is_admin_user=False):
    buttons = [
        [InlineKeyboardButton(text="📱 КУПИТЬ НОМЕР", callback_data="buy_number")],
        [InlineKeyboardButton(text="⭐ КУПИТЬ ЗВЁЗДЫ", callback_data="buy_stars")],
        [InlineKeyboardButton(text="📦 МОИ ЗАКАЗЫ", callback_data="my_orders")],
        [InlineKeyboardButton(text="🏆 ТОП ПОКУПАТЕЛЕЙ", callback_data="top_buyers")],
        [InlineKeyboardButton(text="🆘 ПОДДЕРЖКА", callback_data="support")]
    ]
    if is_admin_user:
        buttons.append([InlineKeyboardButton(text="👑 АДМИН-ПАНЕЛЬ", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 ЗАЯВКИ", callback_data="admin_pending")],
        [InlineKeyboardButton(text="📜 ВСЕ ЗАКАЗЫ", callback_data="admin_all_orders")],
        [InlineKeyboardButton(text="🔍 ПОИСК ЗАКАЗА", callback_data="admin_search")],
        [InlineKeyboardButton(text="📊 СТАТИСТИКА", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🚫 ЗАБЛОКИРОВАТЬ", callback_data="admin_block")],
        [InlineKeyboardButton(text="🔓 РАЗБЛОКИРОВАТЬ", callback_data="admin_unblock")],
        [InlineKeyboardButton(text="📢 РАССЫЛКА", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="📋 ЛОГИ", callback_data="admin_logs")],
        [InlineKeyboardButton(text="🏆 ТОП", callback_data="admin_top")],
        [InlineKeyboardButton(text="📎 ЭКСПОРТ CSV", callback_data="admin_export")],
        [InlineKeyboardButton(text="🛒 В МАГАЗИН", callback_data="back_main")]
    ])

def countries_keyboard():
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for code, info in COUNTRIES.items():
        warning = " ⚠️" if "warning" in info else ""
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"{info['name']} — {info['price']}₽{warning}", callback_data=f"country_{code}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")])
    return kb

def stars_keyboard():
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for stars, price in STARS_PACKS.items():
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"⭐ {stars} — {price}₽", callback_data=f"stars_{stars}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")])
    return kb

async def notify_admins_new_order(order_id, user_display, price, product_name, source):
    src_icon = "🌐" if source == "website" else "🤖"
    src_text = "САЙТ" if source == "website" else "БОТ"
    text = f"📦 НОВЫЙ ЗАКАЗ #{order_id}\n👤 {user_display}\n📦 {product_name}\n💰 {price} ₽\n{src_icon} {src_text}"
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except:
            pass

# ========== ОСНОВНЫЕ ОБРАБОТЧИКИ ==========
@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    payload = command.args
    logger.info(f"START от {message.from_user.id}, payload={payload}")

    if payload and payload.startswith("order_"):
        try:
            order_data = payload.replace("order_", "", 1)
            parts = order_data.split("|")
            if len(parts) < 4:
                await message.answer("❌ Неверный формат заказа. Используйте сайт.")
                return
            country = unquote(parts[0])
            price = parts[1]
            username = unquote(parts[2])
            session_id = parts[3]

            session = pending_sessions.get(session_id)
            if not session or session["used"] or session["expires"] < time.time():
                await message.answer("❌ Недействительный заказ. Оформите заново на сайте.")
                return

            pending_sessions[session_id]["used"] = True
            
            price_int = int(price) if price.isdigit() else 0
            
            await message.answer(
                f"🛒 *ЗАКАЗ ИЗ МАГАЗИНА* 🌐\n\n"
                f"📱 Товар: {country}\n💰 Сумма: {price} ₽\n👤 Telegram: {username}\n\n"
                f"💳 *Реквизиты:*\n📱 СБП: `{PHONE_NUMBER}`\n👤 Получатель: {RECIPIENT_NAME}\n\n"
                f"📸 *Оплатите и отправьте СКРИНШОТ чека*",
                parse_mode="Markdown"
            )
            state = dp.fsm.get_context(bot, message.from_user.id, message.chat.id)
            await state.update_data({
                "product_type": "number",
                "product_name": country,
                "product_price": price_int,
                "product_details": country,
                "source": "website"
            })
            await state.set_state(OrderForm.waiting_for_screenshot)
            await message.answer(
                f"📌 *Как отправить чек?*\n1️⃣ Переведите {price} ₽\n2️⃣ Сделайте скриншот\n3️⃣ Отправьте фото сюда",
                parse_mode="Markdown"
            )
            return
        except Exception as e:
            logger.error(f"Ошибка: {e}")
            await message.answer("❌ Ошибка заказа. Попробуйте снова.")
            return

    if not payload:
        admin_status = is_admin(message.from_user.id)
        await message.answer(
            "🌟 ДОБРО ПОЖАЛОВАТЬ В МАГАЗИН! 🌟\n\n📱 Виртуальные номера Telegram\n⭐ Telegram Stars\n\nВыберите действие:",
            reply_markup=main_menu(admin_status)
        )
        return

    await message.answer("❌ *НЕИЗВЕСТНАЯ КОМАНДА*\nЗаказы принимаются ТОЛЬКО через сайт", parse_mode="Markdown")

@dp.callback_query(F.data == "back_main")
async def back_main(callback: types.CallbackQuery):
    admin_status = is_admin(callback.from_user.id)
    await callback.message.edit_text("🌟 ДОБРО ПОЖАЛОВАТЬ В МАГАЗИН!\n\nВыберите действие:", reply_markup=main_menu(admin_status))
    await callback.answer()

@dp.callback_query(F.data == "admin_panel")
async def admin_panel(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён")
        return
    await callback.message.edit_text("👑 АДМИН-ПАНЕЛЬ", reply_markup=admin_menu())
    await callback.answer()

@dp.callback_query(F.data == "support")
async def support(callback: types.CallbackQuery):
    await callback.message.answer(f"🆘 Поддержка: {SUPPORT_USERNAME}")
    await callback.answer()

@dp.callback_query(F.data == "top_buyers")
async def top_buyers(callback: types.CallbackQuery):
    users = await get_top_users(10)
    if not users:
        await callback.answer("Нет данных")
        return
    text = "🏆 ТОП ПОКУПАТЕЛЕЙ 🏆\n\n"
    for i, u in enumerate(users, 1):
        username = f"@{u['username']}" if u['username'] else f"ID:{u['user_id']}"
        text += f"{i}. {username}\n   📦 {u['total_orders']} заказов | 💰 {u['total_spent']} ₽\n\n"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")]]))
    await callback.answer()

@dp.callback_query(F.data == "buy_number")
async def buy_number(callback: types.CallbackQuery, state: FSMContext):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("Вы заблокированы")
        return
    await state.set_state(OrderForm.waiting_for_country)
    await callback.message.edit_text("📱 Выберите страну:", reply_markup=countries_keyboard())
    await callback.answer()

@dp.callback_query(OrderForm.waiting_for_country, F.data.startswith("country_"))
async def select_country(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split("_")[1]
    country = COUNTRIES.get(code)
    if not country:
        await callback.answer("Неверный выбор")
        return
    await state.update_data(product_type="number", product_name=country['name'], product_price=country['price'], product_details=code, source="bot")
    await state.set_state(OrderForm.waiting_for_screenshot)
    await callback.message.edit_text(
        f"💳 Оплата\n📱 {country['name']}\n💰 Сумма: {country['price']} ₽\n\n"
        f"📌 Реквизиты:\n📱 СБП: {PHONE_NUMBER}\n👤 Получатель: {RECIPIENT_NAME}\n\n✅ Оплатили? Отправьте СКРИНШОТ чека!"
    )
    await callback.answer()

@dp.callback_query(F.data == "buy_stars")
async def buy_stars(callback: types.CallbackQuery, state: FSMContext):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("Вы заблокированы")
        return
    await state.set_state(OrderForm.waiting_for_stars)
    await callback.message.edit_text("⭐ Выберите количество Stars:", reply_markup=stars_keyboard())
    await callback.answer()

@dp.callback_query(OrderForm.waiting_for_stars, F.data.startswith("stars_"))
async def select_stars(callback: types.CallbackQuery, state: FSMContext):
    stars = int(callback.data.split("_")[1])
    price = STARS_PACKS.get(stars)
    if not price:
        await callback.answer("Неверный выбор")
        return
    await state.update_data(product_type="stars", product_name=f"{stars}⭐", product_price=price, product_details=str(stars), source="bot")
    await state.set_state(OrderForm.waiting_for_screenshot)
    await callback.message.edit_text(
        f"💳 Оплата\n⭐ {stars} Telegram Stars\n💰 Сумма: {price} ₽\n\n"
        f"📌 Реквизиты:\n📱 СБП: {PHONE_NUMBER}\n👤 Получатель: {RECIPIENT_NAME}\n\n✅ Оплатили? Отправьте СКРИНШОТ чека!"
    )
    await callback.answer()

@dp.callback_query(F.data == "my_orders")
async def my_orders(callback: types.CallbackQuery):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("Вы заблокированы")
        return
    orders = await get_user_orders(callback.from_user.id)
    if not orders:
        await callback.answer("Нет заказов")
        return
    text = "📦 МОИ ЗАКАЗЫ\n\n"
    status_names = {
        "waiting_confirmation": "🔄 На проверке",
        "confirmed": "✅ Принят",
        "rejected": "❌ Отклонён"
    }
    for o in orders:
        status = status_names.get(o['status'], o['status'])
        created = datetime.fromtimestamp(o['created_at']).strftime("%d.%m %H:%M")
        src = "🌐" if o['source']=='website' else "🤖"
        text += f"#{o['id']} | {o['product_price']}₽ | {created} {src}\n{status}\n\n"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")]]))
    await callback.answer()

# ========== АДМИН-ФУНКЦИИ ==========
@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён")
        return
    stats = await get_stats()
    text = (
        f"📊 СТАТИСТИКА МАГАЗИНА 📊\n\n"
        f"📦 Всего заказов: {stats['total_orders']}\n"
        f"💰 Общая выручка: {stats['total_sum']} ₽\n"
        f"👥 Покупателей: {stats['unique_users']}\n"
        f"🚫 Заблокировано: {stats['blocked_users']}\n"
        f"📊 Средний чек: {stats['avg_check']:.0f} ₽\n\n"
        f"📅 За сегодня: {stats['today_orders']} заказов / {stats['today_sum']} ₽\n"
        f"📆 За неделю: {stats['week_orders']} заказов / {stats['week_sum']} ₽\n"
        f"📆 За месяц: {stats['month_orders']} заказов / {stats['month_sum']} ₽\n\n"
        f"📌 Статусы:\n"
    )
    for stat in stats['status_stats']:
        name = {"waiting_confirmation": "🔄 На проверке", "confirmed": "✅ Подтверждены", "rejected": "❌ Отклонены"}.get(stat['status'], stat['status'])
        text += f"  {name}: {stat['count']} (на {stat['sum'] or 0} ₽)\n"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]]))
    await callback.answer()

@dp.callback_query(F.data == "admin_top")
async def admin_top(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён")
        return
    users = await get_top_users(10)
    if not users:
        await callback.message.edit_text("Нет данных", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]]))
        return
    text = "🏆 ТОП ПОКУПАТЕЛЕЙ (по сумме) 🏆\n\n"
    for i, u in enumerate(users, 1):
        username = f"@{u['username']}" if u['username'] else f"ID:{u['user_id']}"
        text += f"{i}. {username}\n   📦 {u['total_orders']} заказов | 💰 {u['total_spent']} ₽\n\n"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]]))
    await callback.answer()

@dp.callback_query(F.data == "admin_logs")
async def admin_logs(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён")
        return
    logs = await get_admin_logs(30)
    if not logs:
        await callback.message.edit_text("Нет записей", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]]))
        return
    text = "📋 ЛОГИ АДМИНОВ (последние 30)\n\n"
    for log in logs:
        created = datetime.fromtimestamp(log['created_at']).strftime("%d.%m %H:%M")
        action = log['action'].replace("_", " ").upper()
        text += f"[{created}] Админ {log['admin_id']}: {action}"
        if log['target_user']:
            text += f" | user:{log['target_user']}"
        if log['order_id']:
            text += f" | order:#{log['order_id']}"
        text += "\n"
    await callback.message.edit_text(text[:4000], reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]]))
    await callback.answer()

@dp.callback_query(F.data == "admin_search")
async def admin_search(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён")
        return
    await state.set_state(AdminState.waiting_for_search)
    await callback.message.edit_text("🔍 Введите номер заказа или username:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ ОТМЕНА", callback_data="admin_panel")]]))
    await callback.answer()

@dp.message(StateFilter(AdminState.waiting_for_search))
async def process_search(message: types.Message, state: FSMContext):
    query = message.text.strip()
    
    if query.isdigit():
        order = await search_order_by_id(int(query))
        if order:
            status_name = {
                "waiting_confirmation": "🔄 На проверке",
                "confirmed": "✅ Принят",
                "rejected": "❌ Отклонён"
            }.get(order['status'], order['status'])
            created = datetime.fromtimestamp(order['created_at']).strftime("%d.%m.%Y %H:%M")
            src = "🌐" if order['source'] == 'website' else "🤖"
            text = (
                f"📋 ЗАКАЗ #{order['id']}\n\n"
                f"👤 Пользователь: @{order['username'] or 'нет username'} [ID: {order['user_id']}]\n"
                f"📦 Товар: {order['product_name']}\n"
                f"💰 Сумма: {order['product_price']} ₽\n"
                f"📌 Статус: {status_name}\n"
                f"🕐 Создан: {created}\n"
                f"{src} Источник: {'Сайт' if order['source']=='website' else 'Бот'}\n"
            )
            if order['confirmed_by']:
                text += f"👑 Подтверждён админом: {order['confirmed_by']}\n"
            await message.answer(text)
        else:
            await message.answer(f"❌ Заказ #{query} не найден")
    else:
        orders = await search_orders_by_query(query)
        if orders:
            text = f"🔍 РЕЗУЛЬТАТЫ ПО ЗАПРОСУ «{query}»\n\n"
            for o in orders[:10]:
                emoji = {"waiting_confirmation": "🔄", "confirmed": "✅", "rejected": "❌"}.get(o['status'], "❓")
                created = datetime.fromtimestamp(o['created_at']).strftime("%d.%m %H:%M")
                src = "🌐" if o['source']=='website' else "🤖"
                text += f"{emoji} #{o['id']} | {o['product_price']}₽ | {created} | {src} @{o['username'] or 'no name'}\n"
            if len(orders) > 10:
                text += f"\n... и ещё {len(orders)-10} заказов"
            await message.answer(text[:4000])
        else:
            await message.answer(f"❌ По запросу «{query}» ничего не найдено")
    await state.clear()
    await cmd_start(message, CommandObject(args=None))

@dp.callback_query(F.data == "admin_all_orders")
async def admin_all_orders(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён")
        return
    orders = await get_all_orders()
    if not orders:
        await callback.message.edit_text("Нет заказов", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]]))
        return
    status_emoji = {"waiting_confirmation": "🔄", "confirmed": "✅", "rejected": "❌"}
    text = "📜 ВСЕ ЗАКАЗЫ (последние 100)\n\n"
    for o in orders[:25]:
        emoji = status_emoji.get(o['status'], "❓")
        created = datetime.fromtimestamp(o['created_at']).strftime("%d.%m %H:%M")
        src = "🌐" if o['source']=='website' else "🤖"
        text += f"{emoji} #{o['id']} | {o['product_price']}₽ | {created} | {src} @{o['username'] or 'no name'}\n"
    if len(orders) > 25:
        text += f"\n... и ещё {len(orders)-25}"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]]))
    await callback.answer()

@dp.callback_query(F.data == "admin_pending")
async def admin_pending(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён")
        return
    orders = await get_pending_orders()
    if not orders:
        await callback.message.edit_text("✅ Нет заявок", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]]))
        return
    for o in orders:
        text = (f"📋 ЗАЯВКА #{o['id']}\n"
                f"👤 @{o['username'] or 'без username'} [ID:{o['user_id']}]\n"
                f"📦 {o['product_name']}\n"
                f"💰 Сумма: {o['product_price']} ₽\n"
                f"🕐 {datetime.fromtimestamp(o['created_at']).strftime('%d.%m %H:%M')}\n"
                f"🌐 Источник: {'Сайт' if o['source']=='website' else 'Бот'}\n\n"
                f"📸 Чек:")
        if o['screenshot_file_id']:
            await callback.message.answer_photo(o['screenshot_file_id'], caption=text, 
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ ПРИНЯТЬ", callback_data=f"confirm_{o['id']}"),
                     InlineKeyboardButton(text="❌ ОТКАЗАТЬ", callback_data=f"reject_{o['id']}")]
                ]))
        else:
            await callback.message.answer(text+"\n⚠️ Нет чека", 
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ ПРИНЯТЬ", callback_data=f"confirm_{o['id']}"),
                     InlineKeyboardButton(text="❌ ОТКАЗАТЬ", callback_data=f"reject_{o['id']}")]
                ]))
    try:
        await callback.message.delete()
    except:
        pass
    await callback.answer()

@dp.callback_query(F.data == "admin_export")
async def admin_export(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён")
        return
    
    orders = await get_all_orders(limit=1000)
    if not orders:
        await callback.answer("Нет данных для экспорта")
        return
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "User ID", "Username", "Товар", "Цена", "Статус", "Источник", "Создан", "Чек ID"])
    
    for o in orders:
        writer.writerow([
            o['id'], o['user_id'], o['username'], o['product_name'],
            o['product_price'], o['status'], o['source'],
            datetime.fromtimestamp(o['created_at']).strftime("%Y-%m-%d %H:%M:%S"),
            o['screenshot_file_id'] or "-"
        ])
    
    temp_file = f"/tmp/orders_export_{int(time.time())}.csv"
    with open(temp_file, 'w', encoding='utf-8') as f:
        f.write(output.getvalue())
    
    await callback.message.answer_document(FSInputFile(temp_file, filename="orders_export.csv"))
    await callback.answer("Экспорт выполнен")
    os.remove(temp_file)

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён")
        return
    await state.set_state(AdminState.waiting_for_broadcast)
    await callback.message.edit_text("📢 Введите текст рассылки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ ОТМЕНА", callback_data="admin_panel")]]))
    await callback.answer()

@dp.message(StateFilter(AdminState.waiting_for_broadcast))
async def process_broadcast(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён")
        return
    
    broadcast_text = message.text
    users = await get_all_users()
    
    if not users:
        await message.answer("❌ Нет пользователей для рассылки")
        await state.clear()
        return
    
    sent = 0
    failed = 0
    status_msg = await message.answer(f"⏳ Начинаю рассылку {len(users)} пользователям...")
    
    for user in users:
        try:
            await bot.send_message(user['user_id'], f"📢 *НОВОСТЬ МАГАЗИНА*\n\n{broadcast_text}", parse_mode="Markdown")
            sent += 1
            await asyncio.sleep(0.05)
        except:
            failed += 1
    
    await save_broadcast_history(message.from_user.id, broadcast_text[:500], sent)
    await status_msg.edit_text(f"✅ Рассылка завершена!\n📤 Отправлено: {sent}\n❌ Ошибок: {failed}\n👥 Всего: {len(users)}")
    await state.clear()

@dp.callback_query(F.data == "admin_block")
async def admin_block(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён")
        return
    await state.set_state(AdminState.waiting_for_block_user)
    await callback.message.edit_text("🚫 Введите ID пользователя и причину (через пробел):\nПример: `123456789 Нарушение правил`", 
                                     parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ ОТМЕНА", callback_data="admin_panel")]]))
    await callback.answer()

@dp.callback_query(F.data == "admin_unblock")
async def admin_unblock(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён")
        return
    await state.set_state(AdminState.waiting_for_unblock_user)
    await callback.message.edit_text("🔓 Введите ID пользователя для разблокировки:", 
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ ОТМЕНА", callback_data="admin_panel")]]))
    await callback.answer()

@dp.message(StateFilter(AdminState.waiting_for_block_user), F.text)
async def process_block(message: types.Message, state: FSMContext):
    parts = message.text.strip().split(maxsplit=1)
    try:
        uid = int(parts[0])
        reason = parts[1] if len(parts) > 1 else None
        
        if uid in ADMIN_IDS:
            await message.answer("❌ Нельзя заблокировать администратора!")
        else:
            await block_user(uid, reason, message.from_user.id)
            await message.answer(f"✅ Пользователь {uid} заблокирован" + (f"\nПричина: {reason}" if reason else ""))
    except ValueError:
        await message.answer("❌ Неверный ID. Введите число.")
    await state.clear()
    await cmd_start(message, CommandObject(args=None))

@dp.message(StateFilter(AdminState.waiting_for_unblock_user), F.text)
async def process_unblock(message: types.Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
        await unblock_user(uid, message.from_user.id)
        await message.answer(f"✅ Пользователь {uid} разблокирован")
    except ValueError:
        await message.answer("❌ Неверный ID")
    await state.clear()
    await cmd_start(message, CommandObject(args=None))

# ========== ОБРАБОТЧИКИ СКРИНШОТОВ ==========
@dp.message(StateFilter(OrderForm.waiting_for_screenshot), F.photo)
async def process_screenshot(message: types.Message, state: FSMContext):
    if await is_user_blocked(message.from_user.id):
        await message.answer("⛔ Вы заблокированы")
        return
    
    data = await state.get_data()
    if not data:
        await message.answer("❌ Ошибка. Начните заказ заново")
        return
    
    source = data.get("source", "unknown")
    
    order_id = await save_order(
        message.from_user.id,
        message.from_user.username or "без username",
        data['product_type'],
        data['product_name'],
        data['product_price'],
        data['product_details'],
        source
    )
    
    photo = message.photo[-1]
    await update_order_screenshot(order_id, photo.file_id)
    await state.clear()
    
    await message.answer(f"🔄 Ваш заказ #{order_id} принят!\n\n📦 {data['product_name']}\n💰 Сумма: {data['product_price']} ₽\n\nОжидайте подтверждения.\n📞 {SUPPORT_USERNAME}")
    
    user_display = f"@{message.from_user.username}" if message.from_user.username else f"id:{message.from_user.id}"
    await notify_admins_new_order(order_id, user_display, data['product_price'], data['product_name'], source)

@dp.message(StateFilter(OrderForm.waiting_for_screenshot))
async def invalid_screenshot(message: types.Message):
    await message.answer("❌ Отправьте ФОТО чека!")

# ========== ПОДТВЕРЖДЕНИЕ ЗАКАЗОВ ==========
@dp.callback_query(F.data.startswith("confirm_"))
async def confirm_order(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён")
        return
    
    order_id = int(callback.data.split("_")[1])
    order = await process_order(order_id, "confirmed", callback.from_user.id)
    
    if not order:
        await callback.answer("❌ Заказ уже обработан")
        return
    
    await bot.send_message(order['user_id'], 
        f"✅ ЗАКАЗ #{order_id} ПРИНЯТ!\n\n📦 {order['product_name']}\n💰 Сумма: {order['product_price']} ₽\n🎉 Спасибо за покупку!\n\n📞 {SUPPORT_USERNAME}")
    await callback.answer(f"✅ Заказ #{order_id} принят")
    
    try:
        if callback.message.caption:
            await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ ПРИНЯТ")
    except:
        pass

@dp.callback_query(F.data.startswith("reject_"))
async def reject_order(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён")
        return
    
    order_id = int(callback.data.split("_")[1])
    order = await process_order(order_id, "rejected", callback.from_user.id)
    
    if not order:
        await callback.answer("❌ Заказ уже обработан")
        return
    
    await bot.send_message(order['user_id'],
        f"❌ ЗАКАЗ #{order_id} ОТКЛОНЁН\n\n📦 {order['product_name']}\n💰 Сумма: {order['product_price']} ₽\n📞 Свяжитесь с поддержкой: {SUPPORT_USERNAME}")
    await callback.answer(f"❌ Заказ #{order_id} отклонён")
    
    try:
        if callback.message.caption:
            await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ ОТКАЗАНО")
    except:
        pass

# ========== ОЧИСТКА ПРОСРОЧЕННЫХ СЕССИЙ ==========
async def clean_expired_sessions():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        expired = [sid for sid, s in pending_sessions.items() if s["expires"] < now]
        for sid in expired:
            del pending_sessions[sid]

# ========== АВТОУДАЛЕНИЕ СТАРЫХ ЗАКАЗОВ ==========
async def clean_old_orders():
    while True:
        await asyncio.sleep(86400)  # Раз в сутки
        threshold = int((datetime.now() - timedelta(days=30)).timestamp())
        db = await get_db()
        await db.execute("DELETE FROM orders WHERE status != 'waiting_confirmation' AND created_at < ?", (threshold,))
        await db.commit()
        logger.info("Очистка старых заказов выполнена")

# ========== ЗАПУСК ==========
async def main():
    await init_db()
    asyncio.create_task(run_aiohttp())
    asyncio.create_task(clean_expired_sessions())
    asyncio.create_task(clean_old_orders())
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("✅ Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
