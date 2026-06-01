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

# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

ADMIN_ID = int(os.getenv("ADMIN_ID", "8840342301"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@HowScad_support")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "+79027365759")
RECIPIENT_NAME = os.getenv("RECIPIENT_NAME", "Егор")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@Howscard")
REQUIRED_CHANNEL_LINK = os.getenv("REQUIRED_CHANNEL_LINK", "https://t.me/Howscard")
PORT = int(os.getenv("PORT", "8080"))

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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ==================== FLASK ДЛЯ RENDER ====================
flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return "OK", 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=PORT)

async def self_pinger():
    url = f"http://localhost:{PORT}/"
    while True:
        await asyncio.sleep(240)
        try:
            async with aiohttp.ClientSession() as s:
                await s.get(url)
                logger.info("Ping")
        except:
            pass

# ==================== БАЗА ДАННЫХ ====================
DB_NAME = "shop.db"

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            country TEXT,
            price INTEGER,
            address TEXT,
            screenshot TEXT,
            status TEXT DEFAULT 'pending',
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS blocked (user_id INTEGER PRIMARY KEY)''')
init_db()

def save_order(uid, uname, country, price, addr):
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO orders (user_id, username, country, price, address) VALUES (?,?,?,?,?)",
                    (uid, uname, country, price, addr))
        return cur.lastrowid

def update_screenshot(oid, fid):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE orders SET screenshot = ? WHERE id = ?", (fid, oid))

def update_status(oid, status):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, oid))

def get_orders(uid):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC", (uid,)).fetchall()

def is_blocked(uid):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT 1 FROM blocked WHERE user_id = ?", (uid,)).fetchone() is not None

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
class Order(StatesGroup):
    country = State()
    address = State()
    screenshot = State()

# ==================== КЛАВИАТУРЫ ====================
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 КУПИТЬ", callback_data="buy")],
        [InlineKeyboardButton(text="📦 ЗАКАЗЫ", callback_data="orders")],
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
async def start(m: types.Message, state: FSMContext):
    await state.clear()
    if is_blocked(m.from_user.id):
        await m.answer("⛔ Заблокирован")
        return
    if not await is_subscribed(m.from_user.id):
        await m.answer("📢 Подпишись на канал!", reply_markup=sub_kb())
        return
    await m.answer("🌟 Главное меню", reply_markup=main_kb())

@dp.callback_query(F.data == "check_sub")
async def check(c: types.CallbackQuery):
    if await is_subscribed(c.from_user.id):
        await c.message.edit_text("✅ Доступ открыт", reply_markup=main_kb())
    else:
        await c.answer("❌ Не подписан", show_alert=True)

@dp.callback_query(F.data == "back")
async def back(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("🌟 Главное меню", reply_markup=main_kb())

@dp.callback_query(F.data == "support")
async def support(c: types.CallbackQuery):
    await c.answer()
    await c.message.answer(f"🆘 {SUPPORT_USERNAME}")

@dp.callback_query(F.data == "orders")
async def orders(c: types.CallbackQuery):
    ords = get_orders(c.from_user.id)
    if not ords:
        await c.answer("Нет заказов", show_alert=True)
        return
    text = "📦 *Заказы*\n\n"
    for o in ords:
        emoji = "✅" if o[7] == "completed" else "⏳"
        text += f"{emoji} #{o[0]} | {o[4]}₽ | {o[2]}\n"
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Назад", callback_data="back")]]))

@dp.callback_query(F.data == "buy")
async def buy(c: types.CallbackQuery, state: FSMContext):
    if not await is_subscribed(c.from_user.id):
        await c.answer("Подпишись!", show_alert=True)
        return
    await state.set_state(Order.country)
    await c.message.edit_text("🌍 *Выбери страну:*", parse_mode="Markdown", reply_markup=country_kb())

@dp.callback_query(Order.country, F.data.startswith("cnt_"))
async def choose_country(c: types.CallbackQuery, state: FSMContext):
    code = c.data.split("_")[1]
    country = COUNTRY_PRICES[code]
    await state.update_data(country_name=country["name"], price=country["price"])
    await state.set_state(Order.address)
    warning = f"\n\n{country['warning']}" if country.get("warning") else ""
    await c.message.edit_text(
        f"✅ *Страна:* {country['name']} — {country['price']}₽{warning}\n\n"
        f"💰 *Итого:* {country['price']}₽\n\n"
        f"📬 *Введи свой @username:*\nПример: @durov",
        parse_mode="Markdown"
    )
    await c.answer()

@dp.message(Order.address)
async def get_address(m: types.Message, state: FSMContext):
    logger.info(f"Получен username: {m.text} от {m.from_user.id} в состоянии address")
    addr = m.text.strip()
    if not addr.startswith("@"):
        addr = "@" + addr
    if len(addr) < 3:
        await m.answer("❌ Слишком короткий username. Пример: @durov")
        return
    
    data = await state.get_data()
    if not data.get("country_name"):
        await m.answer("❌ Что-то пошло не так. Начни заново /start")
        await state.clear()
        return
    
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
        f"📌 *РЕКВИЗИТЫ:*\n"
        f"СБП: `{PHONE_NUMBER}`\n"
        f"Получатель: {RECIPIENT_NAME}\n\n"
        f"✅ ОПЛАТИЛ? → Отправь СКРИНШОТ чека",
        parse_mode="Markdown"
    )

@dp.message(Order.screenshot, F.photo)
async def handle_screenshot(m: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    if not order_id:
        await m.answer("❌ Ошибка. Начни заново /start")
        await state.clear()
        return
    
    update_screenshot(order_id, m.photo[-1].file_id)
    update_status(order_id, "processing")
    await m.answer(f"✅ *Заказ #{order_id} принят!* Выдадим в течение 2‑4 часов.", parse_mode="Markdown")
    await bot.send_message(ADMIN_ID, f"🆕 Заказ #{order_id}\n{m.from_user.id}\n💰 {data['price']}₽")
    await state.clear()

@dp.message(Order.screenshot)
async def wrong(m: types.Message):
    await m.answer("❌ Отправь ФОТО чека")

# ==================== ЗАПУСК ====================
async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.create_task(self_pinger())
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
