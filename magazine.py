#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
import os
import json
from datetime import datetime, timedelta
from enum import Enum
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from aiogram.filters import CommandStart, StateFilter, CommandObject, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
import aiosqlite
import aiohttp
from aiohttp import web

load_dotenv()

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== КОНФИГУРАЦИЯ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "8276815852,8840342301").split(',')))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@HowScad_support")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "+79027365759")
RECIPIENT_NAME = os.getenv("RECIPIENT_NAME", "Егор")
PORT = int(os.getenv("PORT", "8080"))

ORDER_TIMEOUT_HOURS = 24

# Статусы заказов
class OrderStatus(str, Enum):
    WAITING = "waiting_confirmation"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"

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

STARS_PACKS = {
    50: 70, 100: 140, 250: 350, 400: 550, 500: 670, 750: 1000, 1000: 1400
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ========== БАЗА ДАННЫХ ==========
DB_NAME = "shop.db"
_db = None
_db_lock = asyncio.Lock()
_processing_orders = set()

async def get_db():
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_NAME)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA synchronous=NORMAL")
    return _db

async def init_db():
    db = await get_db()
    await db.execute('''
        CREATE TABLE IF NOT EXISTS orders (
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
            confirmed_by INTEGER DEFAULT NULL,
            confirmed_at INTEGER DEFAULT NULL
        )
    ''')
    await db.execute('''
        CREATE TABLE IF NOT EXISTS blocked_users (
            user_id INTEGER PRIMARY KEY,
            blocked_at INTEGER DEFAULT (strftime('%s', 'now')),
            reason TEXT DEFAULT NULL
        )
    ''')
    await db.execute('''
        CREATE TABLE IF NOT EXISTS admin_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            target_user INTEGER DEFAULT NULL,
            order_id INTEGER DEFAULT NULL,
            details TEXT,
            created_at INTEGER DEFAULT (strftime('%s', 'now'))
        )
    ''')
    await db.execute('''
        CREATE TABLE IF NOT EXISTS broadcast_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            message_text TEXT,
            recipients_count INTEGER,
            sent_at INTEGER DEFAULT (strftime('%s', 'now'))
        )
    ''')
    await db.execute('''
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id INTEGER PRIMARY KEY,
            total_orders INTEGER DEFAULT 0,
            total_spent INTEGER DEFAULT 0,
            last_order_at INTEGER DEFAULT NULL
        )
    ''')
    await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_user_stats_spent ON user_stats(total_spent)")
    await db.commit()
    logger.info("База данных инициализирована")

async def save_order(user_id, username, product_type, product_name, price, details):
    async with _db_lock:
        db = await get_db()
        cursor = await db.execute(
            "INSERT INTO orders (user_id, username, product_type, product_name, product_price, product_details) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, product_type, product_name, price, details)
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
    async with _db_lock:
        db = await get_db()
        await db.execute("UPDATE orders SET screenshot_file_id = ? WHERE id = ?", (file_id, order_id))
        await db.commit()

async def process_order(order_id, new_status, admin_id=None):
    async with _db_lock:
        if order_id in _processing_orders:
            return None
        _processing_orders.add(order_id)
        try:
            db = await get_db()
            cursor = await db.execute(
                "SELECT user_id, product_price, product_name FROM orders WHERE id = ? AND status = ?",
                (order_id, OrderStatus.WAITING.value)
            )
            order = await cursor.fetchone()
            if not order:
                return None
            await db.execute(
                "UPDATE orders SET status = ?, confirmed_by = ?, confirmed_at = strftime('%s', 'now') WHERE id = ? AND status = ?",
                (new_status.value, admin_id, order_id, OrderStatus.WAITING.value)
            )
            await db.commit()
            
            # Логируем действие админа
            if admin_id:
                await db.execute(
                    "INSERT INTO admin_logs (admin_id, action, order_id) VALUES (?, ?, ?)",
                    (admin_id, f"order_{new_status.value}", order_id)
                )
                await db.commit()
            
            return dict(order)
        finally:
            _processing_orders.discard(order_id)

async def get_pending_orders(limit=50, offset=0):
    async with _db_lock:
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM orders WHERE status = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (OrderStatus.WAITING.value, limit, offset)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def get_pending_count():
    async with _db_lock:
        db = await get_db()
        cursor = await db.execute("SELECT COUNT(*) as count FROM orders WHERE status = ?", (OrderStatus.WAITING.value,))
        row = await cursor.fetchone()
        return row[0] if row else 0

async def get_all_orders(limit=100, offset=0):
    async with _db_lock:
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def get_user_orders(user_id):
    async with _db_lock:
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def search_orders_by_user(query):
    async with _db_lock:
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM orders WHERE username LIKE ? OR user_id LIKE ? ORDER BY id DESC LIMIT 50",
            (f"%{query}%", f"%{query}%")
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def search_orders_by_id(order_id):
    async with _db_lock:
        db = await get_db()
        cursor = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

async def is_user_blocked(user_id):
    db = await get_db()
    cursor = await db.execute("SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,))
    row = await cursor.fetchone()
    return row is not None

async def block_user(user_id, reason=None, admin_id=None):
    async with _db_lock:
        db = await get_db()
        await db.execute("INSERT OR IGNORE INTO blocked_users (user_id, reason) VALUES (?, ?)", (user_id, reason))
        await db.commit()
        if admin_id:
            await db.execute(
                "INSERT INTO admin_logs (admin_id, action, target_user, details) VALUES (?, ?, ?, ?)",
                (admin_id, "block_user", user_id, reason)
            )
            await db.commit()

async def unblock_user(user_id, admin_id=None):
    async with _db_lock:
        db = await get_db()
        await db.execute("DELETE FROM blocked_users WHERE user_id = ?", (user_id,))
        await db.commit()
        if admin_id:
            await db.execute(
                "INSERT INTO admin_logs (admin_id, action, target_user) VALUES (?, ?, ?)",
                (admin_id, "unblock_user", user_id)
            )
            await db.commit()

async def get_blocked_users():
    async with _db_lock:
        db = await get_db()
        cursor = await db.execute("SELECT * FROM blocked_users ORDER BY blocked_at DESC")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def clean_old_orders(days=30):
    threshold = int((datetime.now() - timedelta(days=days)).timestamp())
    async with _db_lock:
        db = await get_db()
        await db.execute(
            "DELETE FROM orders WHERE status != ? AND created_at < ?",
            (OrderStatus.WAITING.value, threshold)
        )
        await db.commit()
    logger.info(f"Удалены старые заказы (старше {days} дней)")

async def get_admin_logs(limit=50):
    async with _db_lock:
        db = await get_db()
        cursor = await db.execute(
            "SELECT * FROM admin_logs ORDER BY created_at DESC LIMIT ?",
            (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def get_stats():
    async with _db_lock:
        db = await get_db()
        
        # Общая статистика заказов
        cursor = await db.execute("SELECT COUNT(*) as total, SUM(product_price) as total_sum FROM orders")
        total_row = await cursor.fetchone()
        
        # Статистика по статусам
        cursor = await db.execute("""
            SELECT status, COUNT(*) as count, SUM(product_price) as sum 
            FROM orders GROUP BY status
        """)
        status_stats = await cursor.fetchall()
        
        # Статистика по пользователям
        cursor = await db.execute("SELECT COUNT(DISTINCT user_id) as unique_users FROM orders")
        users_row = await cursor.fetchone()
        
        # Заказы за сегодня
        today_start = int(datetime.now().replace(hour=0, minute=0, second=0).timestamp())
        cursor = await db.execute(
            "SELECT COUNT(*) as today_orders, SUM(product_price) as today_sum FROM orders WHERE created_at >= ?",
            (today_start,)
        )
        today_row = await cursor.fetchone()
        
        # Заказы за неделю
        week_start = int((datetime.now() - timedelta(days=7)).timestamp())
        cursor = await db.execute(
            "SELECT COUNT(*) as week_orders, SUM(product_price) as week_sum FROM orders WHERE created_at >= ?",
            (week_start,)
        )
        week_row = await cursor.fetchone()
        
        # Заблокированные пользователи
        cursor = await db.execute("SELECT COUNT(*) as blocked FROM blocked_users")
        blocked_row = await cursor.fetchone()
        
        return {
            "total_orders": total_row[0] or 0,
            "total_sum": total_row[1] or 0,
            "unique_users": users_row[0] or 0,
            "today_orders": today_row[0] or 0,
            "today_sum": today_row[1] or 0,
            "week_orders": week_row[0] or 0,
            "week_sum": week_row[1] or 0,
            "blocked_users": blocked_row[0] or 0,
            "status_stats": [dict(s) for s in status_stats]
        }

async def get_top_users(limit=10):
    async with _db_lock:
        db = await get_db()
        cursor = await db.execute(
            "SELECT user_id, username, total_orders, total_spent FROM user_stats ORDER BY total_spent DESC LIMIT ?",
            (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def get_all_users():
    async with _db_lock:
        db = await get_db()
        cursor = await db.execute("SELECT DISTINCT user_id, username FROM orders ORDER BY user_id")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def save_broadcast_history(admin_id, message_text, recipients_count):
    async with _db_lock:
        db = await get_db()
        await db.execute(
            "INSERT INTO broadcast_history (admin_id, message_text, recipients_count) VALUES (?, ?, ?)",
            (admin_id, message_text, recipients_count)
        )
        await db.commit()

def is_admin(user_id):
    return user_id in ADMIN_IDS

# ========== ТАЙМАУТ ЗАЯВОК ==========
async def timeout_old_orders():
    timeout_time = int((datetime.now() - timedelta(hours=ORDER_TIMEOUT_HOURS)).timestamp())
    async with _db_lock:
        db = await get_db()
        cursor = await db.execute(
            "SELECT id, user_id FROM orders WHERE status = ? AND created_at < ?",
            (OrderStatus.WAITING.value, timeout_time)
        )
        old_orders = await cursor.fetchall()
        for order_id, user_id in old_orders:
            if order_id in _processing_orders:
                continue
            _processing_orders.add(order_id)
            try:
                await db.execute(
                    "UPDATE orders SET status = ? WHERE id = ? AND status = ?",
                    (OrderStatus.REJECTED.value, order_id, OrderStatus.WAITING.value)
                )
                await db.commit()
                try:
                    await bot.send_message(
                        user_id,
                        f"⏰ ЗАКАЗ #{order_id} АВТОМАТИЧЕСКИ ОТКЛОНЁН\n\n"
                        f"Причина: истекло время ожидания ({ORDER_TIMEOUT_HOURS} часов).\n"
                        f"Вы можете оформить новый заказ.\n\n"
                        f"📞 {SUPPORT_USERNAME}"
                    )
                except Exception as e:
                    logger.error(f"Ошибка уведомления: {e}")
            finally:
                _processing_orders.discard(order_id)

async def start_timeout_checker():
    while True:
        await asyncio.sleep(1800)
        await timeout_old_orders()
        await clean_old_orders(days=30)

# ========== AIOHTTP HEALTHCHECK ==========
async def health_check(request):
    return web.Response(text="OK", status=200)

async def run_aiohttp():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Healthcheck сервер запущен на порту {PORT}")
    while True:
        await asyncio.sleep(3600)

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
        [InlineKeyboardButton(text="📋 ЛОГИ АДМИНОВ", callback_data="admin_logs")],
        [InlineKeyboardButton(text="🏆 ТОП ПОКУПАТЕЛЕЙ", callback_data="admin_top")],
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

# ========== УВЕДОМЛЕНИЕ АДМИНОВ ==========
async def notify_admins_new_order(order_id: int, user_display: str, price: int, product_name: str):
    text = f"📦 НОВЫЙ ЗАКАЗ #{order_id}\n\n👤 {user_display}\n📦 {product_name}\n💰 {price} ₽"
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:
            logger.error(f"Ошибка уведомления админа {admin_id}: {e}")

# ========== ПАГИНАЦИЯ ==========
PENDING_PAGE_SIZE = 5

@dp.callback_query(F.data == "admin_pending")
async def admin_pending_orders(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    total_orders = await get_pending_count()
    if total_orders == 0:
        await callback.message.edit_text("✅ Нет заявок на проверке", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]]))
        return
    await show_pending_page(callback.from_user.id, callback.message, 0)

async def show_pending_page(user_id: int, message: types.Message, page: int):
    total_orders = await get_pending_count()
    total_pages = (total_orders + PENDING_PAGE_SIZE - 1) // PENDING_PAGE_SIZE if total_orders > 0 else 1
    orders = await get_pending_orders(limit=PENDING_PAGE_SIZE, offset=page * PENDING_PAGE_SIZE)
    
    for order in orders:
        created_str = datetime.fromtimestamp(order['created_at']).strftime("%d.%m %H:%M")
        text = (f"📋 ЗАЯВКА #{order['id']}\n\n"
                f"👤 Пользователь: @{order['username'] or 'нет username'} [ID: {order['user_id']}]\n"
                f"📦 Товар: {order['product_name']}\n"
                f"💰 Сумма: {order['product_price']} ₽\n"
                f"🕐 Создан: {created_str}\n\n"
                f"📸 Чек:")
        if order['screenshot_file_id']:
            await message.answer_photo(
                order['screenshot_file_id'],
                caption=text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ ПРИНЯТЬ", callback_data=f"confirm_{order['id']}")],
                    [InlineKeyboardButton(text="❌ ОТКАЗАТЬ", callback_data=f"reject_{order['id']}")],
                ])
            )
        else:
            await message.answer(
                text + "\n⚠️ Фото чека отсутствует",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ ПРИНЯТЬ", callback_data=f"confirm_{order['id']}")],
                    [InlineKeyboardButton(text="❌ ОТКАЗАТЬ", callback_data=f"reject_{order['id']}")],
                ])
            )
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"pending_page_{page-1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"pending_page_{page+1}"))
    nav_buttons.append(InlineKeyboardButton(text="◀ Выход", callback_data="admin_panel"))
    
    if nav_buttons:
        await message.answer(
            f"📄 Страница {page+1} из {total_pages} (всего заявок: {total_orders})",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[nav_buttons])
        )
    
    try:
        await message.delete()
    except Exception:
        pass

@dp.callback_query(F.data.startswith("pending_page_"))
async def pending_page_callback(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    page = int(callback.data.split("_")[2])
    await show_pending_page(callback.from_user.id, callback.message, page)
    await callback.answer()

# ========== ОСНОВНЫЕ ОБРАБОТЧИКИ ==========
@dp.message(CommandStart())
async def cmd_start(
    message: types.Message,
    command: CommandObject
):
    payload = command.args

    if not payload:
        admin_status = is_admin(message.from_user.id)
        await message.answer(
            "🌟 ДОБРО ПОЖАЛОВАТЬ В МАГАЗИН! 🌟\n\n"
            "📱 Виртуальные номера Telegram\n"
            "⭐ Telegram Stars\n\n"
            "Выберите действие:",
            reply_markup=main_menu(admin_status)
        )
        return

    if payload.startswith("order_"):
        try:
            order_data = payload.replace("order_", "", 1)
            parts = order_data.split("|", 2)
            if len(parts) >= 3:
                country = parts[0]
                price = parts[1]
                username = parts[2]
            else:
                country = "товар"
                price = "?"
                username = order_data

            await message.answer(
                f"🛒 *Ваш заказ:*\n\n"
                f"📱 Номер: {country}\n"
                f"💰 Цена: {price} ₽\n"
                f"👤 Telegram: {username}\n\n"
                f"📸 Теперь отправьте *скриншот чека* "
                f"для подтверждения оплаты.\n\n"
                f"💳 *Реквизиты:*\n"
                f"📱 СБП: `{PHONE_NUMBER}`\n"
                f"👤 Получатель: {RECIPIENT_NAME}",
                parse_mode="Markdown"
            )
        except Exception as e:
            await message.answer(f"❌ Ошибка обработки заказа:\n{e}")
        return

    await message.answer(f"⚠️ Неизвестный параметр:\n{payload}")

@dp.callback_query(F.data == "back_main")
async def back_main(callback: types.CallbackQuery):
    admin_status = is_admin(callback.from_user.id)
    try:
        await callback.message.edit_text(
            "🌟 ДОБРО ПОЖАЛОВАТЬ В МАГАЗИН! 🌟\n\n"
            "📱 Виртуальные номера Telegram\n"
            "⭐ Telegram Stars\n\n"
            "Выберите действие:",
            reply_markup=main_menu(admin_status)
        )
    except Exception:
        await callback.message.answer(
            "🌟 ДОБРО ПОЖАЛОВАТЬ В МАГАЗИН! 🌟\n\n"
            "📱 Виртуальные номера Telegram\n"
            "⭐ Telegram Stars\n\n"
            "Выберите действие:",
            reply_markup=main_menu(admin_status)
        )
    await callback.answer()

@dp.callback_query(F.data == "admin_panel")
async def open_admin_panel(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    await callback.message.edit_text(
        "👑 АДМИН-ПАНЕЛЬ\n\nВыберите действие:",
        reply_markup=admin_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "support")
async def support_callback(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(f"🆘 Поддержка\n\n{SUPPORT_USERNAME}")

@dp.callback_query(F.data == "buy_number")
async def buy_number(callback: types.CallbackQuery, state: FSMContext):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("⛔ Вы заблокированы!", show_alert=True)
        return
    await state.set_state(OrderForm.waiting_for_country)
    await callback.message.edit_text("📱 Выберите страну:", reply_markup=countries_keyboard())
    await callback.answer()

@dp.callback_query(OrderForm.waiting_for_country, F.data.startswith("country_"))
async def select_country(callback: types.CallbackQuery, state: FSMContext):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("⛔ Вы заблокированы!", show_alert=True)
        return
    code = callback.data.split("_")[1]
    country = COUNTRIES.get(code)
    if not country:
        await callback.answer("❌ Неверный выбор", show_alert=True)
        return
    await state.update_data(product_type="number", product_name=country['name'], product_price=country['price'], product_details=code)
    await state.set_state(OrderForm.waiting_for_screenshot)
    await callback.message.edit_text(
        f"💳 Оплата\n\n"
        f"📱 {country['name']}\n"
        f"💰 Сумма: {country['price']} ₽\n\n"
        f"📌 Реквизиты:\n"
        f"📱 СБП: {PHONE_NUMBER}\n"
        f"👤 Получатель: {RECIPIENT_NAME}\n\n"
        f"✅ Оплатили? Отправьте СКРИНШОТ чека!"
    )
    await callback.answer()

@dp.callback_query(F.data == "buy_stars")
async def buy_stars(callback: types.CallbackQuery, state: FSMContext):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("⛔ Вы заблокированы!", show_alert=True)
        return
    await state.set_state(OrderForm.waiting_for_stars)
    await callback.message.edit_text("⭐ Выберите количество Stars:", reply_markup=stars_keyboard())
    await callback.answer()

@dp.callback_query(OrderForm.waiting_for_stars, F.data.startswith("stars_"))
async def select_stars(callback: types.CallbackQuery, state: FSMContext):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("⛔ Вы заблокированы!", show_alert=True)
        return
    stars = int(callback.data.split("_")[1])
    price = STARS_PACKS.get(stars)
    if not price:
        await callback.answer("❌ Неверный выбор", show_alert=True)
        return
    await state.update_data(product_type="stars", product_name=f"{stars}⭐", product_price=price, product_details=str(stars))
    await state.set_state(OrderForm.waiting_for_screenshot)
    await callback.message.edit_text(
        f"💳 Оплата\n\n"
        f"⭐ {stars} Telegram Stars\n"
        f"💰 Сумма: {price} ₽\n\n"
        f"📌 Реквизиты:\n"
        f"📱 СБП: {PHONE_NUMBER}\n"
        f"👤 Получатель: {RECIPIENT_NAME}\n\n"
        f"✅ Оплатили? Отправьте СКРИНШОТ чека!"
    )
    await callback.answer()

@dp.message(StateFilter(OrderForm.waiting_for_screenshot), F.photo)
async def process_screenshot(message: types.Message, state: FSMContext):
    logger.info("Скриншот получен! Начинаем обработку...")
    if await is_user_blocked(message.from_user.id):
        await message.answer("⛔ Вы заблокированы.")
        return
    data = await state.get_data()
    if not data:
        logger.warning("Нет данных состояния. Пользователь сбросил FSM?")
        await message.answer("❌ Ошибка. Начните заказ заново через кнопки меню.")
        return
    await state.clear()
    order_id = await save_order(
        message.from_user.id,
        message.from_user.username or "без username",
        data['product_type'],
        data['product_name'],
        data['product_price'],
        data['product_details']
    )
    photo = message.photo[-1]
    await update_order_screenshot(order_id, photo.file_id)
    await message.answer(
        f"🔄 Ваш заказ #{order_id} принят и обрабатывается!\n\n"
        f"📦 {data['product_name']}\n"
        f"💰 Сумма: {data['product_price']} ₽\n\n"
        f"Ожидайте подтверждения оплаты.\n"
        f"📞 {SUPPORT_USERNAME}"
    )
    user_display = f"@{message.from_user.username}" if message.from_user.username else f"id:{message.from_user.id}"
    await notify_admins_new_order(
        order_id=order_id,
        user_display=user_display,
        price=data['product_price'],
        product_name=data['product_name']
    )

@dp.message(StateFilter(OrderForm.waiting_for_screenshot))
async def invalid_screenshot(message: types.Message):
    await message.answer("❌ Отправьте ФОТО чека!")

@dp.callback_query(F.data == "my_orders")
async def my_orders(callback: types.CallbackQuery):
    if await is_user_blocked(callback.from_user.id):
        await callback.answer("⛔ Вы заблокированы!", show_alert=True)
        return
    orders = await get_user_orders(callback.from_user.id)
    if not orders:
        await callback.answer("📭 У вас пока нет заказов", show_alert=True)
        return
    text = "📦 МОИ ЗАКАЗЫ\n\n"
    status_names = {
        OrderStatus.WAITING.value: '🔄 На проверке',
        OrderStatus.CONFIRMED.value: '✅ ПРИНЯТ',
        OrderStatus.REJECTED.value: '❌ ОТКЛОНЁН'
    }
    for order in orders:
        status = status_names.get(order['status'], order['status'])
        created = datetime.fromtimestamp(order['created_at']).strftime("%d.%m %H:%M")
        text += f"#{order['id']} | {order['product_price']}₽ | {created}\n{status}\n\n"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")]]))
    await callback.answer()

# ========== АДМИН ФУНКЦИИ ==========
@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    
    stats = await get_stats()
    text = (
        f"📊 СТАТИСТИКА МАГАЗИНА\n\n"
        f"📦 Всего заказов: {stats['total_orders']}\n"
        f"💰 Общая выручка: {stats['total_sum']} ₽\n"
        f"👥 Уникальных покупателей: {stats['unique_users']}\n"
        f"🚫 Заблокировано: {stats['blocked_users']}\n\n"
        f"📅 За сегодня: {stats['today_orders']} заказов / {stats['today_sum']} ₽\n"
        f"📆 За неделю: {stats['week_orders']} заказов / {stats['week_sum']} ₽\n\n"
        f"📌 Статусы заказов:\n"
    )
    
    for stat in stats['status_stats']:
        status_name = {
            OrderStatus.WAITING.value: "🔄 На проверке",
            OrderStatus.CONFIRMED.value: "✅ Подтверждены",
            OrderStatus.REJECTED.value: "❌ Отклонены"
        }.get(stat['status'], stat['status'])
        text += f"  {status_name}: {stat['count']} (на {stat['sum'] or 0} ₽)\n"
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]]))
    await callback.answer()

@dp.callback_query(F.data == "admin_top")
async def admin_top(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    
    top_users = await get_top_users(10)
    if not top_users:
        text = "🏆 ТОП ПОКУПАТЕЛЕЙ\n\nНет данных"
    else:
        text = "🏆 ТОП ПОКУПАТЕЛЕЙ (по сумме)\n\n"
        for i, user in enumerate(top_users, 1):
            username = f"@{user['username']}" if user['username'] else f"ID:{user['user_id']}"
            text += f"{i}. {username}\n   📦 {user['total_orders']} заказов | 💰 {user['total_spent']} ₽\n\n"
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]]))
    await callback.answer()

@dp.callback_query(F.data == "admin_logs")
async def admin_logs(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    
    logs = await get_admin_logs(20)
    if not logs:
        text = "📋 ЛОГИ АДМИНОВ\n\nНет записей"
    else:
        text = "📋 ЛОГИ АДМИНОВ (последние 20)\n\n"
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
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_search)
    await callback.message.edit_text(
        "🔍 ПОИСК ЗАКАЗА\n\n"
        "Введите номер заказа (#ID) или username пользователя:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ ОТМЕНА", callback_data="admin_panel")]])
    )
    await callback.answer()

@dp.message(StateFilter(AdminState.waiting_for_search))
async def process_search(message: types.Message, state: FSMContext):
    query = message.text.strip()
    
    if query.isdigit():
        order = await search_orders_by_id(int(query))
        if order:
            status_name = {
                OrderStatus.WAITING.value: "🔄 На проверке",
                OrderStatus.CONFIRMED.value: "✅ Принят",
                OrderStatus.REJECTED.value: "❌ Отклонён"
            }.get(order['status'], order['status'])
            created = datetime.fromtimestamp(order['created_at']).strftime("%d.%m.%Y %H:%M")
            text = (
                f"📋 ЗАКАЗ #{order['id']}\n\n"
                f"👤 Пользователь: @{order['username'] or 'нет username'} [ID: {order['user_id']}]\n"
                f"📦 Товар: {order['product_name']}\n"
                f"💰 Сумма: {order['product_price']} ₽\n"
                f"📌 Статус: {status_name}\n"
                f"🕐 Создан: {created}\n"
            )
            if order['confirmed_by']:
                text += f"👑 Подтверждён админом: {order['confirmed_by']}\n"
            await message.answer(text)
        else:
            await message.answer(f"❌ Заказ #{query} не найден")
    else:
        orders = await search_orders_by_user(query)
        if orders:
            text = f"🔍 РЕЗУЛЬТАТЫ ПО ЗАПРОСУ «{query}»\n\n"
            for order in orders[:10]:
                status_emoji = {
                    OrderStatus.WAITING.value: "🔄",
                    OrderStatus.CONFIRMED.value: "✅",
                    OrderStatus.REJECTED.value: "❌"
                }.get(order['status'], "❓")
                created = datetime.fromtimestamp(order['created_at']).strftime("%d.%m %H:%M")
                text += f"{status_emoji} #{order['id']} | {order['product_price']}₽ | {created} | @{order['username'] or 'no name'}\n"
            if len(orders) > 10:
                text += f"\n... и ещё {len(orders)-10} заказов"
            await message.answer(text[:4000])
        else:
            await message.answer(f"❌ По запросу «{query}» ничего не найдено")
    
    await state.clear()
    await cmd_start(message, command=CommandObject(args=None))

@dp.callback_query(F.data == "admin_all_orders")
async def admin_all_orders(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    orders = await get_all_orders(limit=100)
    if not orders:
        await callback.message.edit_text("📭 Нет заказов", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]]))
        return
    status_emoji = {
        OrderStatus.WAITING.value: '🔄',
        OrderStatus.CONFIRMED.value: '✅',
        OrderStatus.REJECTED.value: '❌'
    }
    text = "📜 ВСЕ ЗАКАЗЫ (последние 100)\n\n"
    for order in orders[:30]:
        emoji = status_emoji.get(order['status'], '❓')
        created = datetime.fromtimestamp(order['created_at']).strftime("%d.%m %H:%M")
        text += f"{emoji} #{order['id']} | {order['product_price']}₽ | {created} | @{order['username'] or 'no name'}\n"
    if len(orders) > 30:
        text += f"\n... и ещё {len(orders)-30}"
    text += f"\n\n📊 Всего: {len(orders)}"
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]]))
    await callback.answer()

@dp.callback_query(F.data == "admin_block")
async def admin_block_user(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_block_user)
    await callback.message.edit_text(
        "🚫 БЛОКИРОВКА\n\nВведите ID пользователя для блокировки (можно указать причину через пробел):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ ОТМЕНА", callback_data="admin_panel")]])
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_unblock")
async def admin_unblock_user(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_unblock_user)
    await callback.message.edit_text(
        "🔓 РАЗБЛОКИРОВКА\n\nВведите ID пользователя для разблокировки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ ОТМЕНА", callback_data="admin_panel")]])
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_broadcast)
    await callback.message.edit_text(
        "📢 РАССЫЛКА\n\n"
        "Введите текст сообщения для рассылки всем пользователям.\n"
        "Для отмены нажмите кнопку ниже.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ ОТМЕНА", callback_data="admin_panel")]])
    )
    await callback.answer()

@dp.message(StateFilter(AdminState.waiting_for_broadcast))
async def process_broadcast(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещен!")
        return
    
    broadcast_text = message.text
    users = await get_all_users()
    
    if not users:
        await message.answer("❌ Нет пользователей для рассылки")
        await state.clear()
        await cmd_start(message, command=CommandObject(args=None))
        return
    
    sent_count = 0
    failed_count = 0
    
    status_msg = await message.answer(f"⏳ Начинаю рассылку {len(users)} пользователям...")
    
    for user in users:
        try:
            await bot.send_message(user['user_id'], f"📢 *НОВОСТЬ МАГАЗИНА*\n\n{broadcast_text}", parse_mode="Markdown")
            sent_count += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            failed_count += 1
            logger.error(f"Ошибка отправки пользователю {user['user_id']}: {e}")
    
    await save_broadcast_history(message.from_user.id, broadcast_text[:500], sent_count)
    
    await status_msg.edit_text(
        f"✅ РАССЫЛКА ЗАВЕРШЕНА\n\n"
        f"📤 Отправлено: {sent_count}\n"
        f"❌ Ошибок: {failed_count}\n"
        f"👥 Всего: {len(users)}"
    )
    
    await state.clear()
    await cmd_start(message, command=CommandObject(args=None))

@dp.message(StateFilter(AdminState.waiting_for_block_user), F.text)
async def process_block_user(message: types.Message, state: FSMContext):
    try:
        parts = message.text.strip().split(maxsplit=1)
        uid = int(parts[0])
        reason = parts[1] if len(parts) > 1 else None
        
        if uid in ADMIN_IDS:
            await message.answer("❌ Нельзя заблокировать администратора!")
            await state.clear()
            await cmd_start(message, command=CommandObject(args=None))
            return
        
        await block_user(uid, reason, message.from_user.id)
        await message.answer(f"✅ Пользователь {uid} заблокирован!" + (f"\nПричина: {reason}" if reason else ""))
        await state.clear()
        await cmd_start(message, command=CommandObject(args=None))
    except ValueError:
        await message.answer("❌ Неверный ID. Введите число.")

@dp.message(StateFilter(AdminState.waiting_for_unblock_user), F.text)
async def process_unblock_user(message: types.Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
        await unblock_user(uid, message.from_user.id)
        await message.answer(f"✅ Пользователь {uid} разблокирован!")
        await state.clear()
        await cmd_start(message, command=CommandObject(args=None))
    except ValueError:
        await message.answer("❌ Неверный ID. Введите число.")

@dp.callback_query(F.data.startswith("confirm_"))
async def confirm_order_admin(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    await callback.answer("🔄 Обработка...", show_alert=False)
    order_id = int(callback.data.split("_")[1])
    order = await process_order(order_id, OrderStatus.CONFIRMED, callback.from_user.id)
    if not order:
        await callback.answer("❌ Заказ уже обработан или не найден", show_alert=True)
        return
    await bot.send_message(
        order['user_id'],
        f"✅ ЗАКАЗ #{order_id} ПРИНЯТ!\n\n"
        f"📦 {order['product_name']}\n"
        f"💰 Сумма: {order['product_price']} ₽\n"
        f"🎉 Спасибо за покупку!\n\n"
        f"📞 {SUPPORT_USERNAME}"
    )
    await callback.answer(f"✅ Заказ #{order_id} принят")
    try:
        if callback.message.caption:
            await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ ПРИНЯТ")
    except Exception:
        pass

@dp.callback_query(F.data.startswith("reject_"))
async def reject_order_admin(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    await callback.answer("🔄 Обработка...", show_alert=False)
    order_id = int(callback.data.split("_")[1])
    order = await process_order(order_id, OrderStatus.REJECTED, callback.from_user.id)
    if not order:
        await callback.answer("❌ Заказ уже обработан или не найден", show_alert=True)
        return
    await bot.send_message(
        order['user_id'],
        f"❌ ЗАКАЗ #{order_id} ОТКЛОНЁН\n\n"
        f"📦 {order['product_name']}\n"
        f"💰 Сумма: {order['product_price']} ₽\n"
        f"📞 Свяжитесь с поддержкой: {SUPPORT_USERNAME}"
    )
    await callback.answer(f"❌ Заказ #{order_id} отклонён")
    try:
        if callback.message.caption:
            await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ ОТКАЗАНО")
    except Exception:
        pass

# ========== ЗАПУСК ==========
_timeout_started = False

async def main():
    global _timeout_started
    await init_db()
    asyncio.create_task(run_aiohttp())
    if not _timeout_started:
        asyncio.create_task(start_timeout_checker())
        _timeout_started = True

    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if render_url:
        async def self_pinger():
            while True:
                await asyncio.sleep(300)
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(render_url, timeout=10) as resp:
                            if resp.status == 200:
                                logger.info("Пинг отправлен успешно")
                except Exception as e:
                    logger.error(f"Ошибка пингера: {e}")
        asyncio.create_task(self_pinger())
    else:
        logger.warning("RENDER_EXTERNAL_URL не задан. Пингер не запущен.")

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
