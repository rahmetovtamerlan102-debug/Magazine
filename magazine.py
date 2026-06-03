#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
import sqlite3
import aiohttp
import threading
import os
from datetime import datetime
from flask import Flask
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
    raise ValueError("BOT_TOKEN не задан в .env")

# АДМИНИСТРАТОРЫ (укажите свой ID)
ADMINS_IDS = [8276815852, 8840342301]

SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@HowScad_support")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "+79027365759")
RECIPIENT_NAME = os.getenv("RECIPIENT_NAME", "Егор")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@Howscard")
REQUIRED_CHANNEL_LINK = os.getenv("REQUIRED_CHANNEL_LINK", "https://t.me/Howscard")
PORT = int(os.getenv("PORT", "8080"))

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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ==================== FLASK ====================
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "Bot is running", 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=PORT)

async def self_pinger():
    url = f"http://localhost:{PORT}/"
    while True:
        await asyncio.sleep(240)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        logger.info("🏓 Self-ping отправлен")
        except Exception as e:
            logger.error(f"Self-ping ошибка: {e}")

# ==================== БАЗА ДАННЫХ ====================
DB_NAME = "shop.db"

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                product_type TEXT,
                product_name TEXT,
                product_price INTEGER,
                product_details TEXT,
                screenshot_file_id TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmed_at TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id INTEGER PRIMARY KEY
            )
        ''')
init_db()

def save_order(user_id, username, product_type, product_name, price, details):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.execute(
            "INSERT INTO orders (user_id, username, product_type, product_name, product_price, product_details) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, product_type, product_name, price, details)
        )
        return cursor.lastrowid

def update_order_screenshot(order_id, file_id):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE orders SET screenshot_file_id = ?, status = 'waiting_confirmation' WHERE id = ?", (file_id, order_id))

def confirm_order(order_id):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE orders SET status = 'confirmed', confirmed_at = CURRENT_TIMESTAMP WHERE id = ?", (order_id,))

def reject_order(order_id):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE orders SET status = 'rejected' WHERE id = ?", (order_id,))

def get_pending_orders():
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM orders WHERE status = 'waiting_confirmation' ORDER BY id DESC").fetchall()

def get_all_orders():
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 50").fetchall()

def get_user_orders(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC", (user_id,)).fetchall()

def is_user_blocked(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,)).fetchone() is not None

def block_user(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO blocked_users (user_id) VALUES (?)", (user_id,))

def is_admin(user_id):
    return user_id in ADMINS_IDS

# ==================== ПРОВЕРКА ПОДПИСКИ ====================
async def is_subscribed(user_id):
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

def subscription_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 ПОДПИСАТЬСЯ", url=REQUIRED_CHANNEL_LINK)],
        [InlineKeyboardButton(text="🔄 ПРОВЕРИТЬ ПОДПИСКУ", callback_data="check_sub")]
    ])

# ==================== FSM ====================
class OrderForm(StatesGroup):
    waiting_for_country = State()
    waiting_for_stars = State()
    waiting_for_screenshot = State()

# ==================== КЛАВИАТУРЫ ====================
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
        [InlineKeyboardButton(text="📋 ЗАЯВКИ НА ПОДТВЕРЖДЕНИЕ", callback_data="admin_pending")],
        [InlineKeyboardButton(text="📜 ВСЕ ЗАКАЗЫ", callback_data="admin_all_orders")],
        [InlineKeyboardButton(text="🚫 ЗАБЛОКИРОВАТЬ", callback_data="admin_block")],
        [InlineKeyboardButton(text="🛒 МАГАЗИН", callback_data="back_main")]
    ])

def countries_keyboard():
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for code, info in COUNTRIES.items():
        warning = " ⚠️" if "warning" in info else ""
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"{info['name']} — {info['price']}₽{warning}", callback_data=f"country_{code}")
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")])
    return kb

def stars_keyboard():
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for stars, price in STARS_PACKS.items():
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"⭐ {stars} — {price}₽", callback_data=f"stars_{stars}")
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")])
    return kb

# ==================== ОСНОВНЫЕ ОБРАБОТЧИКИ ====================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if is_user_blocked(message.from_user.id):
        await message.answer("⛔ Вы заблокированы.")
        return

    if not is_admin(message.from_user.id) and not await is_subscribed(message.from_user.id):
        await message.answer(
            "📢 *ДОСТУП К МАГАЗИНУ ТОЛЬКО ДЛЯ ПОДПИСЧИКОВ КАНАЛА!*\n\n"
            f"👉 Подпишитесь: {REQUIRED_CHANNEL}\n\n"
            "После подписки нажмите «ПРОВЕРИТЬ ПОДПИСКУ».",
            parse_mode="Markdown",
            reply_markup=subscription_keyboard()
        )
        return

    await message.answer(
        "🌟 *ДОБРО ПОЖАЛОВАТЬ В МАГАЗИН!* 🌟\n\n"
        "📱 Виртуальные номера Telegram\n⭐ Telegram Stars\n\n"
        "🔽 Выберите действие:",
        parse_mode="Markdown",
        reply_markup=main_menu(is_admin(message.from_user.id))
    )

@dp.callback_query(F.data == "check_sub")
async def check_subscription(callback: types.CallbackQuery):
    if await is_subscribed(callback.from_user.id):
        await callback.message.edit_text(
            "✅ *Подписка подтверждена!*\n\nДобро пожаловать в магазин.",
            parse_mode="Markdown",
            reply_markup=main_menu(is_admin(callback.from_user.id))
        )
    else:
        await callback.answer("❌ Вы ещё не подписались на канал!", show_alert=True)

@dp.callback_query(F.data == "back_main")
async def back_main(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "🌟 *Магазин*\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=main_menu(is_admin(callback.from_user.id))
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_panel")
async def open_admin_panel(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    await callback.message.edit_text(
        "👑 *АДМИН-ПАНЕЛЬ*\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=admin_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "support")
async def support_callback(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        f"🆘 *Поддержка*\n\n{SUPPORT_USERNAME}",
        parse_mode="Markdown"
    )

# -------------------- ПОКУПКА НОМЕРА --------------------
@dp.callback_query(F.data == "buy_number")
async def buy_number(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id) and not await is_subscribed(callback.from_user.id):
        await callback.answer("❌ Подпишитесь на канал!", show_alert=True)
        return
    await state.set_state(OrderForm.waiting_for_country)
    await callback.message.edit_text(
        "📱 *Выберите страну:*",
        parse_mode="Markdown",
        reply_markup=countries_keyboard()
    )
    await callback.answer()

@dp.callback_query(OrderForm.waiting_for_country, F.data.startswith("country_"))
async def select_country(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split("_")[1]
    if code not in COUNTRIES:
        await callback.answer("❌ Неверный выбор", show_alert=True)
        return
    country = COUNTRIES[code]
    warning_text = f"\n\n{country['warning']}" if "warning" in country else ""
    await state.update_data(
        product_type="number",
        product_name=country['name'],
        product_price=country['price'],
        product_details=code
    )
    await state.set_state(OrderForm.waiting_for_screenshot)
    await callback.message.edit_text(
        f"💳 *Оплата*\n\n"
        f"📱 {country['name']}\n"
        f"💰 Сумма: {country['price']} ₽{warning_text}\n\n"
        "📌 *Реквизиты:*\n"
        f"📱 СБП: `{PHONE_NUMBER}`\n"
        f"👤 Получатель: *{RECIPIENT_NAME}*\n\n"
        "✅ *Оплатили? Отправьте СКРИНШОТ чека!*",
        parse_mode="Markdown"
    )
    await callback.answer()

# -------------------- ПОКУПКА ЗВЁЗД --------------------
@dp.callback_query(F.data == "buy_stars")
async def buy_stars(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id) and not await is_subscribed(callback.from_user.id):
        await callback.answer("❌ Подпишитесь на канал!", show_alert=True)
        return
    await state.set_state(OrderForm.waiting_for_stars)
    await callback.message.edit_text(
        "⭐ *Выберите количество Stars:*",
        parse_mode="Markdown",
        reply_markup=stars_keyboard()
    )
    await callback.answer()

@dp.callback_query(OrderForm.waiting_for_stars, F.data.startswith("stars_"))
async def select_stars(callback: types.CallbackQuery, state: FSMContext):
    stars = int(callback.data.split("_")[1])
    if stars not in STARS_PACKS:
        await callback.answer("❌ Неверный выбор", show_alert=True)
        return
    price = STARS_PACKS[stars]
    await state.update_data(
        product_type="stars",
        product_name=f"{stars}⭐",
        product_price=price,
        product_details=str(stars)
    )
    await state.set_state(OrderForm.waiting_for_screenshot)
    await callback.message.edit_text(
        f"💳 *Оплата*\n\n"
        f"⭐ {stars} Telegram Stars\n"
        f"💰 Сумма: {price} ₽\n\n"
        "📌 *Реквизиты:*\n"
        f"📱 СБП: `{PHONE_NUMBER}`\n"
        f"👤 Получатель: *{RECIPIENT_NAME}*\n\n"
        "✅ *Оплатили? Отправьте СКРИНШОТ чека!*",
        parse_mode="Markdown"
    )
    await callback.answer()

# -------------------- ПРИЁМ СКРИНШОТА И ОТПРАВКА УВЕДОМЛЕНИЯ --------------------
@dp.message(OrderForm.waiting_for_screenshot, F.photo)
async def process_screenshot(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if not data:
        await message.answer("❌ Ошибка. Начните заказ заново через /start")
        await state.clear()
        return

    # Сохраняем заказ
    order_id = save_order(
        message.from_user.id,
        message.from_user.username or "без username",
        data['product_type'],
        data['product_name'],
        data['product_price'],
        data['product_details']
    )
    photo = message.photo[-1]
    update_order_screenshot(order_id, photo.file_id)
    await state.clear()

    # Ответ пользователю
    await message.answer(
        f"✅ *Заказ #{order_id} принят!*\n\n"
        f"📦 {data['product_name']}\n"
        f"💰 Сумма: {data['product_price']} ₽\n\n"
        f"🕒 Статус: На проверке\n\n"
        f"📞 {SUPPORT_USERNAME}",
        parse_mode="Markdown"
    )

    # ========== УВЕДОМЛЕНИЕ АДМИНИСТРАТОРУ ==========
    product_icon = "📱" if data['product_type'] == "number" else "⭐"
    admin_text = (
        f"🆕 *НОВЫЙ ЗАКАЗ #{order_id}!*\n\n"
        f"{product_icon} *Товар:* {data['product_name']}\n"
        f"👤 *Пользователь:* @{message.from_user.username or 'нет username'}\n"
        f"🆔 *ID:* {message.from_user.id}\n"
        f"💰 *Сумма:* {data['product_price']} ₽\n"
        f"📦 *Детали:* {data['product_details']}\n"
        f"🕐 *Время:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    logger.info(f"📤 Отправка уведомления о заказе #{order_id} админам: {ADMINS_IDS}")

    for admin_id in ADMINS_IDS:
        try:
            await bot.send_message(admin_id, admin_text, parse_mode="Markdown")
            logger.info(f"✅ Уведомление отправлено админу {admin_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка при отправке админу {admin_id}: {e}")

    # Также отправляем фото чека отдельно (опционально)
    for admin_id in ADMINS_IDS:
        try:
            await bot.send_photo(
                admin_id,
                photo.file_id,
                caption=f"📸 Чек к заказу #{order_id}",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"❌ Ошибка при отправке фото админу {admin_id}: {e}")

@dp.message(OrderForm.waiting_for_screenshot)
async def invalid_screenshot(message: types.Message):
    await message.answer("❌ *Отправьте ФОТО чека!*", parse_mode="Markdown")

# -------------------- МОИ ЗАКАЗЫ --------------------
@dp.callback_query(F.data == "my_orders")
async def my_orders(callback: types.CallbackQuery):
    orders = get_user_orders(callback.from_user.id)
    if not orders:
        await callback.answer("📭 У вас пока нет заказов", show_alert=True)
        return

    text = "📦 *МОИ ЗАКАЗЫ*\n\n"
    status_names = {
        'pending': '⏳ Ожидает оплаты',
        'waiting_confirmation': '🔄 На проверке',
        'confirmed': '✅ ПРИНЯТ',
        'rejected': '❌ Отклонен'
    }
    for order in orders:
        status = status_names.get(order[8], order[8])
        created = datetime.strptime(order[9], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        text += f"#{order[0]} | {order[5]}₽ | {created}\n{status}\n\n"

    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")]
        ])
    )
    await callback.answer()

# ==================== АДМИН-ОБРАБОТЧИКИ ====================
@dp.callback_query(F.data == "admin_pending")
async def admin_pending_orders(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    orders = get_pending_orders()
    if not orders:
        await callback.message.edit_text(
            "✅ *Нет заказов на проверке*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]
            ])
        )
        await callback.answer()
        return

    for order in orders:
        order_id, user_id, username, ptype, pname, price, details, screenshot, status, created_at = order[:10]
        product_icon = "📱" if ptype == "number" else "⭐"
        text = (
            f"📋 *ЗАЯВКА #{order_id}*\n\n"
            f"{product_icon} *Товар:* {pname}\n"
            f"👤 *Пользователь:* @{username or 'нет username'} [ID: {user_id}]\n"
            f"💰 *Сумма:* {price} ₽\n"
            f"📬 *Детали:* {details}\n"
            f"🕐 *Создан:* {created_at}\n"
            f"📋 *Статус:* На проверке\n\n"
            f"📸 *Чек:*"
        )
        if screenshot:
            await callback.message.answer_photo(
                screenshot,
                caption=text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ ПРИНЯТЬ", callback_data=f"confirm_{order_id}")],
                    [InlineKeyboardButton(text="❌ ОТКЛОНИТЬ", callback_data=f"reject_{order_id}")],
                    [InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]
                ])
            )
    await callback.message.delete()
    await callback.answer()

@dp.callback_query(F.data == "admin_all_orders")
async def admin_all_orders(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    orders = get_all_orders()
    if not orders:
        await callback.message.edit_text(
            "📭 *Нет заказов*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]
            ])
        )
        await callback.answer()
        return
    status_emoji = {'pending': '⏳', 'waiting_confirmation': '🔄', 'confirmed': '✅', 'rejected': '❌'}
    text = "📜 *ВСЕ ЗАКАЗЫ*\n\n"
    for order in orders[:20]:
        emoji = status_emoji.get(order[8], '❓')
        text += f"{emoji} #{order[0]} | {order[5]}₽ | @{order[2] or 'no name'}\n"
    text += f"\n📊 *Всего:* {len(orders)}"
    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 ЗАЯВКИ", callback_data="admin_pending")],
            [InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_block")
async def admin_block_user(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    await state.set_state("waiting_for_block_user")
    await callback.message.edit_text(
        "🚫 *БЛОКИРОВКА*\n\nВведите ID пользователя:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀ ОТМЕНА", callback_data="admin_panel")]
        ])
    )
    await callback.answer()

@dp.message(F.text, lambda m: is_admin(m.from_user.id))
async def process_block_user(message: types.Message, state: FSMContext):
    if await state.get_state() == "waiting_for_block_user":
        try:
            uid = int(message.text.strip())
            block_user(uid)
            await message.answer(f"✅ Пользователь {uid} заблокирован!")
            await state.clear()
            await cmd_start(message)
        except ValueError:
            await message.answer("❌ Неверный ID")

@dp.callback_query(F.data.startswith("confirm_"))
async def confirm_order_admin(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    order_id = int(callback.data.split("_")[1])
    confirm_order(order_id)
    
    with sqlite3.connect(DB_NAME) as conn:
        order = conn.execute("SELECT user_id, product_price, product_name FROM orders WHERE id = ?", (order_id,)).fetchone()
    
    if order:
        uid, price, pname = order
        await bot.send_message(
            uid,
            f"✅ *ЗАКАЗ #{order_id} ПРИНЯТ!*\n\n"
            f"📦 {pname}\n"
            f"💰 Сумма: {price} ₽\n"
            f"🎉 Спасибо за покупку!\n\n"
            f"📞 {SUPPORT_USERNAME}",
            parse_mode="Markdown"
        )
    
    await callback.answer(f"✅ Заказ #{order_id} принят!")
    if callback.message.caption:
        new_caption = callback.message.caption.replace("📸 *Чек:*", "✅ **ПРИНЯТ**")
        await callback.message.edit_caption(caption=new_caption, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("reject_"))
async def reject_order_admin(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    order_id = int(callback.data.split("_")[1])
    reject_order(order_id)
    
    with sqlite3.connect(DB_NAME) as conn:
        order = conn.execute("SELECT user_id, product_price, product_name FROM orders WHERE id = ?", (order_id,)).fetchone()
    
    if order:
        uid, price, pname = order
        await bot.send_message(
            uid,
            f"❌ *ЗАКАЗ #{order_id} ОТКЛОНЕН*\n\n"
            f"📦 {pname}\n"
            f"💰 Сумма: {price} ₽\n"
            f"📞 Свяжитесь с поддержкой: {SUPPORT_USERNAME}",
            parse_mode="Markdown"
        )
    
    await callback.answer(f"❌ Заказ #{order_id} отклонён!")
    if callback.message.caption:
        new_caption = callback.message.caption.replace("📸 *Чек:*", "❌ **ОТКЛОНЕН**")
        await callback.message.edit_caption(caption=new_caption, parse_mode="Markdown")

# ==================== ЗАПУСК ====================
async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.create_task(self_pinger())
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info(f"✅ Бот запущен! Администраторы: {ADMINS_IDS}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
