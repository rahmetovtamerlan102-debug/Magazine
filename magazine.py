#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
import sqlite3
import os
import threading
from datetime import datetime
from flask import Flask
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

load_dotenv()

# ==================== КОНФИГУРАЦИЯ (из .env) ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env")

ADMIN_ID = int(os.getenv("ADMIN_ID", "8840342301"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@HowScad_support")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "+79027365759")
RECIPIENT_NAME = os.getenv("RECIPIENT_NAME", "Егор")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@Howscard")
REQUIRED_CHANNEL_LINK = os.getenv("REQUIRED_CHANNEL_LINK", "https://t.me/Howscard")
PORT = int(os.getenv("PORT", "8080"))

# Цены на страны
COUNTRY_PRICES = {
    "us": {"name": "🇺🇸 США", "price": 80},
    "canada": {"name": "🇨🇦 Канада", "price": 65},
    "russia": {"name": "🇷🇺 Россия (новорег)", "price": 140},
    "bangladesh": {"name": "🇧🇩 Бангладеш", "price": 90},
    "philippines": {"name": "🇵🇭 Филиппины", "price": 65},
    "nigeria": {"name": "🇳🇬 Нигерия", "price": 60},
    "iraq": {"name": "🇮🇶 Ирак", "price": 60},
    "africa": {"name": "🌍 Африка", "price": 60},
    "india": {"name": "🇮🇳 Индия", "price": 50, "warning": "⚠️ Высокий шанс слёта аккаунта!"}
}

# Telegram Premium (сначала выбирается это)
PREMIUM_PLANS = {
    "no_premium": {"name": "❌ Без Premium", "price_add": 0},
    "1_month": {"name": "⭐ Telegram Premium 1 месяц", "price_add": 300},
    "3_month": {"name": "⭐ Telegram Premium 3 месяца", "price_add": 900},
    "6_month": {"name": "⭐ Telegram Premium 6 месяцев", "price_add": 1200},
    "1_year": {"name": "⭐ Telegram Premium 1 год", "price_add": 2000}
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
    flask_app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

async def self_pinger():
    url = f"http://localhost:{PORT}/"
    while True:
        await asyncio.sleep(240)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        logger.info("Self-ping отправлен")
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
                country TEXT,
                country_price INTEGER,
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

def save_order(user_id, username, premium_name, premium_price, country_name, country_price, total_price, address):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute(
            "INSERT INTO orders (user_id, username, premium_plan, premium_price, country, country_price, total_price, address) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, premium_name, premium_price, country_name, country_price, total_price, address)
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
    waiting_for_country = State()
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
    for code, data in PREMIUM_PLANS.items():
        plus_text = f" +{data['price_add']} ₽" if data['price_add'] > 0 else ""
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"{data['name']}{plus_text}", callback_data=f"premium_{code}")
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")])
    return kb

def countries_keyboard():
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for code, data in COUNTRY_PRICES.items():
        warning = " ⚠️" if data.get("warning") else ""
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=f"{data['name']} — {data['price']} ₽{warning}", callback_data=f"country_{code}")
        ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_premium")])
    return kb

# ==================== ОБРАБОТЧИКИ ====================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if is_user_blocked(message.from_user.id):
        await message.answer("⛔ Вы заблокированы.")
        return
    
    if not await is_subscribed(message.from_user.id):
        await message.answer(
            f"📢 *Для доступа подпишитесь:*\n{REQUIRED_CHANNEL}\n\nПосле подписки нажмите «ПРОВЕРИТЬ»",
            parse_mode="Markdown",
            reply_markup=subscription_keyboard()
        )
        return
    
    await message.answer(
        "📱 *Магазин аккаунтов Telegram*\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )

@dp.callback_query(F.data == "check_sub")
async def check_subscription(callback: types.CallbackQuery):
    if await is_subscribed(callback.from_user.id):
        await callback.message.edit_text("✅ Доступ открыт", reply_markup=main_menu())
    else:
        await callback.answer("❌ Не подписан!", show_alert=True)

@dp.callback_query(F.data == "back_main")
async def back_main(callback: types.CallbackQuery):
    await callback.message.edit_text("📱 *Главное меню*", parse_mode="Markdown", reply_markup=main_menu())

@dp.callback_query(F.data == "back_premium")
async def back_premium(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(OrderForm.waiting_for_premium)
    await callback.message.edit_text("💰 *Выберите Premium:*", parse_mode="Markdown", reply_markup=premium_keyboard())

@dp.callback_query(F.data == "support")
async def support_callback(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(f"🆘 Поддержка: {SUPPORT_USERNAME}")

@dp.callback_query(F.data == "buy_number")
async def buy_number(callback: types.CallbackQuery, state: FSMContext):
    if not await is_subscribed(callback.from_user.id):
        await callback.answer("❌ Подпишитесь!", show_alert=True)
        return
    await state.set_state(OrderForm.waiting_for_premium)
    await callback.message.edit_text("💰 *ШАГ 1: ВЫБОР PREMIUM*", parse_mode="Markdown", reply_markup=premium_keyboard())

@dp.callback_query(OrderForm.waiting_for_premium, F.data.startswith("premium_"))
async def select_premium(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split("_")[1]
    data = PREMIUM_PLANS[code]
    await state.update_data(premium_name=data["name"], premium_price=data["price_add"])
    await state.set_state(OrderForm.waiting_for_country)
    await callback.message.edit_text("🌍 *ШАГ 2: ВЫБОР СТРАНЫ*", parse_mode="Markdown", reply_markup=countries_keyboard())

@dp.callback_query(OrderForm.waiting_for_country, F.data.startswith("country_"))
async def select_country(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split("_")[1]
    country = COUNTRY_PRICES[code]
    user_data = await state.get_data()
    
    total = country["price"] + user_data.get("premium_price", 0)
    await state.update_data(
        country_name=country["name"],
        country_price=country["price"],
        total_price=total
    )
    await state.set_state(OrderForm.waiting_for_address)
    
    warning = f"\n\n⚠️ {country['warning']}" if country.get("warning") else ""
    await callback.message.edit_text(
        f"✅ *Premium:* {user_data['premium_name']} +{user_data['premium_price']} ₽\n"
        f"✅ *Страна:* {country['name']} — {country['price']} ₽{warning}\n\n"
        f"💰 *ИТОГО: {total} ₽*\n\n📬 *Введите ваш @username для доставки:*",
        parse_mode="Markdown"
    )

@dp.message(OrderForm.waiting_for_address)
async def process_address(message: types.Message, state: FSMContext):
    addr = message.text.strip()
    if not addr.startswith("@"):
        addr = "@" + addr
    if len(addr) < 3:
        await message.answer("❌ Введите корректный username (например, @durov)")
        return
    
    data = await state.get_data()
    order_id = save_order(
        message.from_user.id,
        message.from_user.username or "нет",
        data["premium_name"],
        data["premium_price"],
        data["country_name"],
        data["country_price"],
        data["total_price"],
        addr
    )
    await state.update_data(order_id=order_id)
    await state.set_state(OrderForm.waiting_for_screenshot)
    
    await message.answer(
        f"💳 *ЗАКАЗ #{order_id}*\n💰 Сумма: {data['total_price']} ₽\n\n"
        f"📌 *Реквизиты:*\n📱 СБП: `{PHONE_NUMBER}`\n👤 Получатель: {RECIPIENT_NAME}\n\n"
        f"✅ *Оплатили? Отправьте СКРИНШОТ чека!*",
        parse_mode="Markdown"
    )

@dp.message(OrderForm.waiting_for_screenshot, F.photo)
async def process_screenshot(message: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    if not order_id:
        await message.answer("❌ Ошибка, начните заново /start")
        await state.clear()
        return
    
    update_order_screenshot(order_id, message.photo[-1].file_id)
    update_order_status(order_id, "processing")
    
    await message.answer(f"✅ *Заказ #{order_id} принят!* Выдадим в течение 2-4 часов.", parse_mode="Markdown")
    await bot.send_message(ADMIN_ID, f"🆕 Новый заказ #{order_id}\n👤 {message.from_user.id}\n💰 {data['total_price']} ₽")
    await state.clear()

@dp.message(OrderForm.waiting_for_screenshot)
async def invalid_screenshot(message: types.Message):
    await message.answer("❌ Отправьте ФОТО чека, текстом не принимаем.")

@dp.callback_query(F.data == "my_orders")
async def my_orders(callback: types.CallbackQuery):
    orders = get_user_orders(callback.from_user.id)
    if not orders:
        await callback.answer("Нет заказов", show_alert=True)
        return
    text = "📦 *Мои заказы*\n\n"
    for o in orders:
        emoji = "✅" if o[10] == "completed" else "⏳"
        text += f"{emoji} #{o[0]} | {o[7]} ₽ | {o[4]}\n"
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")]]))

# ==================== ЗАПУСК ====================
async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.create_task(self_pinger())
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
