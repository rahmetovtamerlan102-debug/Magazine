#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
import sqlite3
import aiohttp
import threading
from datetime import datetime
from flask import Flask
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = "8815785936:AAE4k5koBf7EWWECIZZL1-OHLG9fzz9ZF04"
ADMIN_ID = 8840342301
SUPPORT_USERNAME = "@HowScad_support"
PHONE_NUMBER = "+79027365759"
RECIPIENT_NAME = "Егор"  # Имя получателя
REQUIRED_CHANNEL = "@Howscard"
REQUIRED_CHANNEL_LINK = "https://t.me/Howscard"
PORT = 8080

# Стоимость аккаунта с разными сроками Telegram Premium
FTZ_PRICE = 200

TELEGRAM_PREMIUM_PRICES = {
    "no_premium": {"name": "❌ Без Premium", "premium_price": 0},
    "1_month": {"name": "⭐ Telegram Premium 1 месяц", "premium_price": 300},
    "3_month": {"name": "⭐ Telegram Premium 3 месяца", "premium_price": 900},
    "6_month": {"name": "⭐ Telegram Premium 6 месяцев", "premium_price": 1200},
    "1_year": {"name": "⭐ Telegram Premium 1 год", "premium_price": 2000}
}
# ====================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ==================== FLASK + SELF-PING ====================
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
                premium_plan TEXT,
                premium_price INTEGER,
                ftz_price INTEGER,
                total_price INTEGER,
                address TEXT,
                screenshot_file_id TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id INTEGER PRIMARY KEY
            )
        ''')
init_db()

def save_order(user_id, username, premium_name, premium_price, total_price, address):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute(
            "INSERT INTO orders (user_id, username, premium_plan, premium_price, ftz_price, total_price, address) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, premium_name, premium_price, FTZ_PRICE, total_price, address)
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

def update_order_screenshot(order_id, file_id):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE orders SET screenshot_file_id = ? WHERE id = ?", (file_id, order_id))

def update_order_status(order_id, status):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))

def get_user_orders(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC", (user_id,)).fetchall()

def is_user_blocked(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,)).fetchone() is not None

def block_user(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO blocked_users (user_id) VALUES (?)", (user_id,))

# ==================== ПРОВЕРКА ПОДПИСКИ ====================
async def is_subscribed(user_id: int) -> bool:
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
    waiting_for_premium = State()
    waiting_for_address = State()
    waiting_for_screenshot = State()

# ==================== КЛАВИАТУРЫ ====================
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 КУПИТЬ АККАУНТ", callback_data="buy_number")],
        [InlineKeyboardButton(text="📦 МОИ ЗАКАЗЫ", callback_data="my_orders")],
        [InlineKeyboardButton(text="🆘 ПОДДЕРЖКА", callback_data="support")]
    ])

def premium_keyboard():
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for code, data in TELEGRAM_PREMIUM_PRICES.items():
        total = FTZ_PRICE + data["premium_price"]
        kb.inline_keyboard.append([
            InlineKeyboardButton(
                text=f"{data['name']} — {total} ₽",
                callback_data=f"premium_{code}"
            )
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")])
    return kb

# ==================== ОБРАБОТЧИКИ ====================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if is_user_blocked(message.from_user.id):
        await message.answer("⛔ Вы заблокированы.")
        return
    
    if not await is_subscribed(message.from_user.id):
        await message.answer(
            "📢 *ДОСТУП К МАГАЗИНУ ТОЛЬКО ДЛЯ ПОДПИСЧИКОВ КАНАЛА!*\n\n"
            f"👉 Подпишитесь: {REQUIRED_CHANNEL}\n\n"
            "После подписки нажмите «ПРОВЕРИТЬ ПОДПИСКУ».",
            parse_mode="Markdown",
            reply_markup=subscription_keyboard()
        )
        return
    
    await message.answer(
        "🌟 *ДОБРО ПОЖАЛОВАТЬ В МАГАЗИН АККАУНТОВ TELEGRAM!* 🌟\n\n"
        "📱 Здесь вы можете приобрести готовые аккаунты Telegram с возможностью добавить Premium любого срока.\n\n"
        "💎 *Почему выбирают нас?*\n"
        "✅ Быстрая выдача (2-4 часа)\n"
        "✅ Бесплатная поддержка\n"
        "✅ Гарантия качества\n\n"
        "🔽 *Чтобы сделать заказ, нажмите на кнопку ниже:*",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )

@dp.callback_query(F.data == "check_sub")
async def check_subscription(callback: types.CallbackQuery):
    if await is_subscribed(callback.from_user.id):
        await callback.message.edit_text(
            "✅ *Подписка подтверждена!*\n\nДобро пожаловать в магазин.",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )
    else:
        await callback.answer("❌ Вы ещё не подписались на канал!", show_alert=True)

@dp.callback_query(F.data == "back_main")
async def back_main(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "🌟 *Магазин аккаунтов Telegram*\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )
    await callback.answer()

@dp.callback_query(F.data == "support")
async def support_callback(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        f"🆘 *Техническая поддержка*\n\n{SUPPORT_USERNAME}",
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "buy_number")
async def buy_number(callback: types.CallbackQuery, state: FSMContext):
    if not await is_subscribed(callback.from_user.id):
        await callback.answer("❌ Подпишитесь на канал!", show_alert=True)
        return
    await callback.answer()
    await state.set_state(OrderForm.waiting_for_premium)
    await callback.message.edit_text(
        f"💰 *Базовая цена аккаунта:* {FTZ_PRICE} ₽\n\n"
        f"⭐ *Выберите опцию Telegram Premium:*",
        parse_mode="Markdown",
        reply_markup=premium_keyboard()
    )

@dp.callback_query(OrderForm.waiting_for_premium, F.data.startswith("premium_"))
async def select_premium(callback: types.CallbackQuery, state: FSMContext):
    premium_code = callback.data.split("_")[1]
    premium_data = TELEGRAM_PREMIUM_PRICES[premium_code]
    total_price = FTZ_PRICE + premium_data["premium_price"]
    
    await state.update_data(
        premium_name=premium_data["name"],
        premium_price=premium_data["premium_price"],
        total_price=total_price
    )
    await state.set_state(OrderForm.waiting_for_address)
    
    await callback.message.edit_text(
        f"✅ *Ваш выбор:*\n"
        f"📱 Аккаунт — {FTZ_PRICE} ₽\n"
        f"⭐ {premium_data['name']} — +{premium_data['premium_price']} ₽\n\n"
        f"💰 *ИТОГО К ОПЛАТЕ:* {total_price} ₽\n\n"
        f"📬 *Введите ваш Telegram username для доставки:*\n"
        f"(например, @durov)",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(OrderForm.waiting_for_address)
async def process_address(message: types.Message, state: FSMContext):
    address = message.text.strip()
    if not address.startswith("@"):
        address = "@" + address
    if len(address) < 3:
        await message.answer("❌ Пожалуйста, введите корректный username (например, @durov)")
        return
    
    data = await state.get_data()
    order_id = save_order(
        message.from_user.id,
        message.from_user.username or "без username",
        data["premium_name"],
        data["premium_price"],
        data["total_price"],
        address
    )
    await state.update_data(order_id=order_id, address=address)
    await state.set_state(OrderForm.waiting_for_screenshot)
    
    await message.answer(
        f"💳 *Оплата заказа #{order_id}*\n\n"
        f"💰 *Сумма к оплате:* {data['total_price']} ₽\n\n"
        f"📋 *Состав заказа:*\n"
        f"• Аккаунт Telegram — {FTZ_PRICE} ₽\n"
        f"• {data['premium_name']} — +{data['premium_price']} ₽\n\n"
        "📌 *Реквизиты для перевода:*\n"
        f"📱 По СБП: `{PHONE_NUMBER}`\n"
        f"👤 Получатель: *{RECIPIENT_NAME}*\n\n"
        "✅ *Оплатили? Отправьте СКРИНШОТ чека сюда!*\n"
        "Без скриншота заказ не будет обработан.\n\n"
        f"📞 Вопросы: {SUPPORT_USERNAME}",
        parse_mode="Markdown"
    )

@dp.message(OrderForm.waiting_for_screenshot, F.photo)
async def process_screenshot(message: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    
    if not order_id:
        await message.answer("❌ Ошибка: заказ не найден. Начните заказ заново через /start")
        await state.clear()
        return
    
    photo = message.photo[-1]
    file_id = photo.file_id
    update_order_screenshot(order_id, file_id)
    update_order_status(order_id, "processing")
    
    await state.clear()
    
    await message.answer(
        f"✅ *Заказ #{order_id} принят!*\n\n"
        f"📋 {data['premium_name']}\n"
        f"💰 Сумма: {data['total_price']} ₽\n"
        f"📬 Доставка: {data.get('address')}\n\n"
        f"🕒 *Аккаунт будет отправлен в течение 2-4 часов.*\n\n"
        f"📞 По вопросам: {SUPPORT_USERNAME}",
        parse_mode="Markdown"
    )
    
    await bot.send_message(
        ADMIN_ID,
        f"🆕 *НОВЫЙ ЗАКАЗ #{order_id}!*\n"
        f"👤 Пользователь: {message.from_user.id} (@{message.from_user.username})\n"
        f"⭐ {data['premium_name']}\n"
        f"💰 Сумма: {data['total_price']} ₽\n"
        f"📬 Доставка: {data.get('address')}\n"
        f"📸 Скриншот получен!",
        parse_mode="Markdown"
    )

@dp.message(OrderForm.waiting_for_screenshot)
async def invalid_screenshot(message: types.Message):
    await message.answer(
        "❌ *Пожалуйста, отправьте ФОТО (скриншот чека) после оплаты.*\n\n"
        "Текстовые сообщения не принимаются.",
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "my_orders")
async def my_orders(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    orders = get_user_orders(user_id)
    
    if not orders:
        await callback.answer("📭 У вас пока нет заказов", show_alert=True)
        return
    
    text = "📦 *Мои заказы*\n\n"
    for order in orders:
        status_emoji = "✅" if order[9] == "completed" else "⏳"
        created = datetime.strptime(order[10], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        text += f"{status_emoji} *Заказ #{order[0]}* | {order[5]} ₽ | {created}\n"
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")]
    ]))
    await callback.answer()

# ==================== ЗАПУСК ====================
async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.create_task(self_pinger())
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Магазин аккаунтов Telegram запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
