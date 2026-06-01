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

# Цены на страны (Premium убран)
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ==================== FLASK + SELF-PING (для Render) ====================
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
        conn.execute('''CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            country_name TEXT,
            price INTEGER,
            address TEXT,
            screenshot TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS blocked_users (user_id INTEGER PRIMARY KEY)''')
init_db()

def save_order(user_id, username, country_name, price, address):
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        cur.execute('''INSERT INTO orders (user_id, username, country_name, price, address) VALUES (?, ?, ?, ?, ?)''',
                    (user_id, username, country_name, price, address))
        return cur.lastrowid

def update_screenshot(order_id, file_id):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE orders SET screenshot = ? WHERE id = ?", (file_id, order_id))

def update_status(order_id, status):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))

def get_orders(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC", (user_id,)).fetchall()

def is_blocked(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,)).fetchone() is not None

# ==================== ПРОВЕРКА ПОДПИСКИ ====================
async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

def sub_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 ПОДПИСАТЬСЯ", url=REQUIRED_CHANNEL_LINK)],
        [InlineKeyboardButton(text="🔄 ПРОВЕРИТЬ", callback_data="check_sub")]
    ])

# ==================== FSM ====================
class Order(StatesGroup):
    country = State()
    address = State()
    screenshot = State()

# ==================== КЛАВИАТУРЫ ====================
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 КУПИТЬ АККАУНТ", callback_data="buy")],
        [InlineKeyboardButton(text="📦 МОИ ЗАКАЗЫ", callback_data="orders")],
        [InlineKeyboardButton(text="🆘 ПОДДЕРЖКА", callback_data="support")]
    ])

def country_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for code, c in COUNTRY_PRICES.items():
        text = f"{c['name']} — {c['price']}₽"
        if c.get('warning'):
            text += " ⚠️"
        kb.inline_keyboard.append([InlineKeyboardButton(text=text, callback_data=f"cnt_{code}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ НАЗАД", callback_data="back")])
    return kb

# ==================== ХЕНДЛЕРЫ ====================
@dp.message(Command("start"))
async def start(m: types.Message):
    if is_blocked(m.from_user.id):
        await m.answer("⛔ Вы заблокированы.")
        return
    if not await is_subscribed(m.from_user.id):
        await m.answer("📢 Подпишитесь на канал, чтобы пользоваться магазином!", reply_markup=sub_kb())
        return
    await m.answer("🌟 Добро пожаловать в магазин аккаунтов Telegram!\nВыберите действие:", reply_markup=main_kb())

@dp.callback_query(F.data == "check_sub")
async def check_sub(c: types.CallbackQuery):
    if await is_subscribed(c.from_user.id):
        await c.message.edit_text("✅ Подписка подтверждена! Добро пожаловать.", reply_markup=main_kb())
    else:
        await c.answer("❌ Вы не подписаны на канал!", show_alert=True)

@dp.callback_query(F.data == "back")
async def back_main(c: types.CallbackQuery):
    await c.message.edit_text("🌟 Главное меню", reply_markup=main_kb())

@dp.callback_query(F.data == "support")
async def support(c: types.CallbackQuery):
    await c.answer()
    await c.message.answer(f"🆘 Связь с поддержкой: {SUPPORT_USERNAME}")

@dp.callback_query(F.data == "orders")
async def orders(c: types.CallbackQuery):
    ords = get_orders(c.from_user.id)
    if not ords:
        await c.answer("У вас пока нет заказов", show_alert=True)
        return
    text = "📦 *Мои заказы*\n\n"
    for o in ords:
        status_emoji = "✅" if o[7] == "completed" else "⏳"
        created = datetime.strptime(o[8], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        text += f"{status_emoji} *Заказ #{o[0]}* | {o[4]}₽ | {o[2]} | {created}\n"
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="back")]]))

@dp.callback_query(F.data == "buy")
async def buy(c: types.CallbackQuery, state: FSMContext):
    if not await is_subscribed(c.from_user.id):
        await c.answer("Подпишитесь на канал!", show_alert=True)
        return
    await state.set_state(Order.country)
    await c.message.edit_text("🌍 *ВЫБОР СТРАНЫ*\n\nВыберите страну аккаунта:", parse_mode="Markdown", reply_markup=country_kb())

@dp.callback_query(Order.country, F.data.startswith("cnt_"))
async def select_country(c: types.CallbackQuery, state: FSMContext):
    code = c.data.split("_")[1]
    country = COUNTRY_PRICES[code]
    await state.update_data(
        country_name=country["name"],
        price=country["price"]
    )
    await state.set_state(Order.address)
    warning = f"\n\n{country['warning']}" if country.get("warning") else ""
    await c.message.edit_text(
        f"✅ *Страна:* {country['name']} — {country['price']}₽{warning}\n\n"
        f"💰 *ИТОГО К ОПЛАТЕ:* {country['price']}₽\n\n"
        f"📬 *ВВЕДИТЕ ВАШ TELEGRAM USERNAME ДЛЯ ДОСТАВКИ*\n"
        f"Например: @durov",
        parse_mode="Markdown"
    )
    await c.answer()

@dp.message(Order.address)
async def get_address(m: types.Message, state: FSMContext):
    addr = m.text.strip()
    if not addr.startswith("@"):
        addr = "@" + addr
    if len(addr) < 3:
        await m.answer("❌ Слишком короткий username. Пример: @durov")
        return
    
    data = await state.get_data()
    order_id = save_order(
        m.from_user.id,
        m.from_user.username or "no_username",
        data["country_name"],
        data["price"],
        addr
    )
    await state.update_data(order_id=order_id)
    await state.set_state(Order.screenshot)
    
    await m.answer(
        f"💳 *ЗАКАЗ #{order_id}*\n"
        f"💰 Сумма: {data['price']}₽\n\n"
        f"📌 *РЕКВИЗИТЫ ДЛЯ ОПЛАТЫ:*\n"
        f"📱 СБП: `{PHONE_NUMBER}`\n"
        f"👤 Получатель: {RECIPIENT_NAME}\n\n"
        f"✅ ОПЛАТИЛИ? Отправьте СКРИНШОТ чека сюда.\n"
        f"Без скриншота заказ не будет обработан.\n\n"
        f"📞 Вопросы: {SUPPORT_USERNAME}",
        parse_mode="Markdown"
    )

@dp.message(Order.screenshot, F.photo)
async def get_screenshot(m: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    if not order_id:
        await m.answer("❌ Ошибка: заказ не найден. Начните заново командой /start")
        await state.clear()
        return
    
    file_id = m.photo[-1].file_id
    update_screenshot(order_id, file_id)
    update_status(order_id, "processing")
    
    await m.answer(
        f"✅ *ЗАКАЗ #{order_id} ПРИНЯТ!*\n\n"
        f"Скриншот получен. Аккаунт будет отправлен в течение 2‑4 часов на {data.get('address')}.\n"
        f"По вопросам: {SUPPORT_USERNAME}",
        parse_mode="Markdown"
    )
    await bot.send_message(
        ADMIN_ID,
        f"🆕 *НОВЫЙ ЗАКАЗ #{order_id}*\n"
        f"👤 Пользователь: {m.from_user.id} (@{m.from_user.username})\n"
        f"📦 {data['country_name']}\n"
        f"💰 {data['price']}₽\n"
        f"📬 Доставка: {data.get('address')}\n"
        f"📸 Скриншот получен",
        parse_mode="Markdown"
    )
    await state.clear()

@dp.message(Order.screenshot)
async def wrong_screenshot(m: types.Message):
    await m.answer("❌ Отправьте именно ФОТО (скриншот чека). Текстовые сообщения не принимаются.")

# ==================== ЗАПУСК ====================
async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.create_task(self_pinger())
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
