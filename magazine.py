#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
import os
import secrets
import time
from datetime import datetime, timedelta
from enum import Enum
from urllib.parse import unquote
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, StateFilter, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
import aiosqlite
import aiohttp

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

# ========== ХРАНИЛИЩЕ СЕССИЙ ==========
pending_sessions = {}

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
            source TEXT DEFAULT 'unknown'
        )
    ''')
    await db.execute('''
        CREATE TABLE IF NOT EXISTS blocked_users (
            user_id INTEGER PRIMARY KEY
        )
    ''')
    await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)")
    await db.commit()
    logger.info("База данных инициализирована")

async def save_order(user_id, username, product_type, product_name, price, details, source="unknown"):
    async with _db_lock:
        db = await get_db()
        cursor = await db.execute(
            "INSERT INTO orders (user_id, username, product_type, product_name, product_price, product_details, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, product_type, product_name, price, details, source)
        )
        await db.commit()
        return cursor.lastrowid

async def update_order_screenshot(order_id, file_id):
    async with _db_lock:
        db = await get_db()
        await db.execute("UPDATE orders SET screenshot_file_id = ? WHERE id = ?", (file_id, order_id))
        await db.commit()

async def process_order(order_id, new_status):
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
                "UPDATE orders SET status = ? WHERE id = ? AND status = ?",
                (new_status.value, order_id, OrderStatus.WAITING.value)
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

async def is_user_blocked(user_id):
    db = await get_db()
    cursor = await db.execute("SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,))
    row = await cursor.fetchone()
    return row is not None

async def block_user(user_id):
    async with _db_lock:
        db = await get_db()
        await db.execute("INSERT OR IGNORE INTO blocked_users (user_id) VALUES (?)", (user_id,))
        await db.commit()

async def unblock_user(user_id):
    async with _db_lock:
        db = await get_db()
        await db.execute("DELETE FROM blocked_users WHERE user_id = ?", (user_id,))
        await db.commit()

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

def is_admin(user_id):
    return user_id in ADMIN_IDS

# ========== ОЧИСТКА ПРОСРОЧЕННЫХ СЕССИЙ ==========
async def clean_expired_sessions():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        expired = [sid for sid, data in pending_sessions.items() if data["expires"] < now]
        for sid in expired:
            del pending_sessions[sid]
        if expired:
            logger.info(f"Удалено {len(expired)} просроченных сессий")

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

# ========== HTTP СЕРВЕР ==========
async def health_check(request):
    return web.Response(text="OK", status=200)

async def register_session(request):
    """Сайт вызывает этот эндпоинт для регистрации сессии"""
    try:
        data = await request.json()
        user_id = data.get("user_id")
        product_name = data.get("product_name")
        product_price = data.get("product_price")
        username = data.get("username")
        
        logger.info(f"Запрос на регистрацию сессии: user_id={user_id}, product={product_name}")
        
        if not user_id:
            return web.json_response({"status": "error", "error": "no user_id"}, status=400)
        
        # Генерируем уникальный session_id
        session_id = secrets.token_hex(16)
        
        # Сохраняем сессию (живёт 5 минут)
        pending_sessions[session_id] = {
            "user_id": user_id,
            "product_name": product_name,
            "product_price": product_price,
            "username": username,
            "expires": time.time() + 300,
            "used": False
        }
        
        logger.info(f"✅ Создана сессия {session_id[:16]}... для {user_id}")
        
        return web.json_response({
            "status": "ok",
            "session_id": session_id
        })
    except Exception as e:
        logger.error(f"Ошибка регистрации сессии: {e}")
        return web.json_response({"status": "error", "error": str(e)}, status=500)

async def run_aiohttp():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_post('/register_session', register_session)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"✅ HTTP сервер запущен на порту {PORT}")
    logger.info(f"✅ Эндпоинт: https://howscad-market.onrender.com/register_session")
    
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
        [InlineKeyboardButton(text="🚫 ЗАБЛОКИРОВАТЬ", callback_data="admin_block")],
        [InlineKeyboardButton(text="🔓 РАЗБЛОКИРОВАТЬ", callback_data="admin_unblock")],
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
async def notify_admins_new_order(order_id: int, user_display: str, price: int, product_name: str, source: str):
    source_icon = "🌐" if source == "website" else "🤖"
    source_text = "САЙТ" if source == "website" else "БОТ"
    text = f"📦 НОВЫЙ ЗАКАЗ #{order_id}\n\n👤 {user_display}\n📦 {product_name}\n💰 {price} ₽\n{source_icon} Источник: {source_text}"
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
        source_icon = "🌐" if order.get('source') == "website" else "🤖"
        source_text = "Сайт" if order.get('source') == "website" else "Бот"
        text = (f"📋 ЗАЯВКА #{order['id']}\n\n"
                f"👤 Пользователь: @{order['username'] or 'нет username'} [ID: {order['user_id']}]\n"
                f"📦 Товар: {order['product_name']}\n"
                f"💰 Сумма: {order['product_price']} ₽\n"
                f"🕐 Создан: {created_str}\n"
                f"{source_icon} Источник: {source_text}\n\n"
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

# ========== ГЛАВНЫЙ /start ==========
@dp.message(CommandStart())
async def cmd_start(message: types.Message, command: CommandObject):
    payload = command.args
    logger.info(f"START от {message.from_user.id}, payload: {payload}")
    
    # ===== ЗАКАЗ С САЙТА =====
    if payload and payload.startswith("order_"):
        try:
            order_data = payload.replace("order_", "", 1)
            parts = order_data.split("|", 4)
            
            if len(parts) >= 4:
                country = unquote(parts[0])
                price = parts[1]
                username = unquote(parts[2])
                session_id = parts[3]
                
                # Проверка сессии
                is_valid = False
                if session_id in pending_sessions:
                    session = pending_sessions[session_id]
                    if not session["used"] and session["expires"] > time.time():
                        is_valid = True
                        pending_sessions[session_id]["used"] = True
                        logger.info(f"✅ Сессия {session_id[:16]}... валидна для {message.from_user.id}")
                    else:
                        del pending_sessions[session_id]
                
                if not is_valid:
                    await message.answer(
                        "❌ *НЕДЕЙСТВИТЕЛЬНЫЙ ЗАКАЗ*\n\n"
                        "Заказы принимаются **ТОЛЬКО через сайт**:\n"
                        "🔗 https://howscad-market.onrender.com",
                        parse_mode="Markdown"
                    )
                    return
                
                # ✅ ПРИНЯТЬ ЗАКАЗ
                price_int = int(price) if price.isdigit() else 0
                
                await message.answer(
                    f"🛒 *ЗАКАЗ ИЗ МАГАЗИНА* 🌐\n\n"
                    f"📱 Товар: {country}\n"
                    f"💰 Сумма: {price} ₽\n"
                    f"👤 Telegram: {username}\n\n"
                    f"💳 *Реквизиты для оплаты:*\n"
                    f"📱 СБП: `{PHONE_NUMBER}`\n"
                    f"👤 Получатель: {RECIPIENT_NAME}\n\n"
                    f"📸 *ОПЛАТИТЕ И ОТПРАВЬТЕ СКРИНШОТ ЧЕКА*",
                    parse_mode="Markdown"
                )
                
                await message.answer(
                    f"📌 *Как отправить чек?*\n"
                    f"1️⃣ Переведите {price} ₽ на номер +79027365759\n"
                    f"2️⃣ Сделайте скриншот\n"
                    f"3️⃣ Отправьте фото в этот чат",
                    parse_mode="Markdown"
                )
                
                # Сохраняем в состояние
                state = dp.fsm.get_context(bot, message.from_user.id, message.chat.id)
                await state.update_data({
                    "product_type": "number",
                    "product_name": country,
                    "product_price": price_int,
                    "product_details": country,
                    "source": "website"
                })
                await state.set_state(OrderForm.waiting_for_screenshot)
                return
                
        except Exception as e:
            logger.error(f"Ошибка: {e}")
            await message.answer("❌ Ошибка обработки заказа")
            return

    # ===== ОБЫЧНЫЙ /start =====
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

    # ===== НЕИЗВЕСТНЫЙ PAYLOAD =====
    await message.answer(
        "❌ *НЕИЗВЕСТНАЯ КОМАНДА*\n\n"
        "Заказы принимаются **ТОЛЬКО через сайт**",
        parse_mode="Markdown"
    )

# ========== ОБРАБОТЧИКИ КНОПОК ==========
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
    except:
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
    await state.update_data(product_type="number", product_name=country['name'], product_price=country['price'], product_details=code, source="bot")
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
    await state.update_data(product_type="stars", product_name=f"{stars}⭐", product_price=price, product_details=str(stars), source="bot")
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
        source_icon = "🌐" if order.get('source') == "website" else "🤖"
        text += f"#{order['id']} | {order['product_price']}₽ | {created} {source_icon}\n{status}\n\n"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")]]))
    await callback.answer()

@dp.callback_query(F.data == "admin_all_orders")
async def admin_all_orders(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    orders = await get_all_orders(limit=50)
    if not orders:
        await callback.message.edit_text("📭 Нет заказов", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]]))
        return
    status_emoji = {
        OrderStatus.WAITING.value: '🔄',
        OrderStatus.CONFIRMED.value: '✅',
        OrderStatus.REJECTED.value: '❌'
    }
    text = "📜 ВСЕ ЗАКАЗЫ (последние 50)\n\n"
    for order in orders[:20]:
        emoji = status_emoji.get(order['status'], '❓')
        created = datetime.fromtimestamp(order['created_at']).strftime("%d.%m %H:%M")
        source_icon = "🌐" if order.get('source') == "website" else "🤖"
        text += f"{emoji} #{order['id']} | {order['product_price']}₽ | {created} | {source_icon} @{order['username'] or 'no name'}\n"
    if len(orders) > 20:
        text += f"\n... и ещё {len(orders)-20}"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]]))
    await callback.answer()

@dp.callback_query(F.data == "admin_block")
async def admin_block_user(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_block_user)
    await callback.message.edit_text("🚫 БЛОКИРОВКА\n\nВведите ID пользователя:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ ОТМЕНА", callback_data="admin_panel")]]))
    await callback.answer()

@dp.callback_query(F.data == "admin_unblock")
async def admin_unblock_user(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_unblock_user)
    await callback.message.edit_text("🔓 РАЗБЛОКИРОВКА\n\nВведите ID пользователя:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ ОТМЕНА", callback_data="admin_panel")]]))
    await callback.answer()

@dp.message(StateFilter(AdminState.waiting_for_block_user), F.text)
async def process_block_user(message: types.Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
        if uid in ADMIN_IDS:
            await message.answer("❌ Нельзя заблокировать администратора!")
            await state.clear()
            await cmd_start(message, command=CommandObject(args=None))
            return
        await block_user(uid)
        await message.answer(f"✅ Пользователь {uid} заблокирован!")
        await state.clear()
        await cmd_start(message, command=CommandObject(args=None))
    except ValueError:
        await message.answer("❌ Неверный ID. Введите число.")

@dp.message(StateFilter(AdminState.waiting_for_unblock_user), F.text)
async def process_unblock_user(message: types.Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
        await unblock_user(uid)
        await message.answer(f"✅ Пользователь {uid} разблокирован!")
        await state.clear()
        await cmd_start(message, command=CommandObject(args=None))
    except ValueError:
        await message.answer("❌ Неверный ID. Введите число.")

# ========== ОБРАБОТЧИКИ СКРИНШОТОВ ==========
@dp.message(StateFilter(OrderForm.waiting_for_screenshot), F.photo)
async def process_screenshot(message: types.Message, state: FSMContext):
    logger.info(f"Скриншот от {message.from_user.id}")
    
    if await is_user_blocked(message.from_user.id):
        await message.answer("⛔ Вы заблокированы.")
        return
    
    data = await state.get_data()
    if not data:
        await message.answer("❌ Ошибка. Начните заказ заново.")
        return
    
    source = data.get('source', 'unknown')
    
    order_id = await save_order(
        message.from_user.id,
        message.from_user.username or "без username",
        data.get('product_type', 'number'),
        data.get('product_name', 'Товар'),
        data.get('product_price', 0),
        data.get('product_details', ''),
        source
    )
    
    photo = message.photo[-1]
    await update_order_screenshot(order_id, photo.file_id)
    await state.clear()
    
    await message.answer(
        f"🔄 Ваш заказ #{order_id} принят!\n\n"
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
        product_name=data['product_name'],
        source=source
    )

@dp.message(StateFilter(OrderForm.waiting_for_screenshot))
async def invalid_screenshot(message: types.Message):
    await message.answer("❌ Отправьте ФОТО чека!")

# ========== ПОДТВЕРЖДЕНИЕ ЗАКАЗОВ ==========
@dp.callback_query(F.data.startswith("confirm_"))
async def confirm_order_admin(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    await callback.answer("🔄 Обработка...", show_alert=False)
    order_id = int(callback.data.split("_")[1])
    order = await process_order(order_id, OrderStatus.CONFIRMED)
    if not order:
        await callback.answer("❌ Заказ уже обработан", show_alert=True)
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

@dp.callback_query(F.data.startswith("reject_"))
async def reject_order_admin(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    await callback.answer("🔄 Обработка...", show_alert=False)
    order_id = int(callback.data.split("_")[1])
    order = await process_order(order_id, OrderStatus.REJECTED)
    if not order:
        await callback.answer("❌ Заказ уже обработан", show_alert=True)
        return
    await bot.send_message(
        order['user_id'],
        f"❌ ЗАКАЗ #{order_id} ОТКЛОНЁН\n\n"
        f"📦 {order['product_name']}\n"
        f"💰 Сумма: {order['product_price']} ₽\n"
        f"📞 {SUPPORT_USERNAME}"
    )
    await callback.answer(f"❌ Заказ #{order_id} отклонён")

# ========== ЗАПУСК ==========
async def main():
    await init_db()
    asyncio.create_task(run_aiohttp())
    asyncio.create_task(start_timeout_checker())
    asyncio.create_task(clean_expired_sessions())

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("✅ Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
