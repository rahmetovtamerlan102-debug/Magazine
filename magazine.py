#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
import sqlite3
import os
from datetime import datetime
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

# Цены стран
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

# Premium планы
PREMIUM_PLANS = {
    "no_premium": {"name": "❌ Без Premium", "add": 0},
    "1_month": {"name": "⭐ Telegram Premium 1 месяц", "add": 300},
    "3_month": {"name": "⭐ Telegram Premium 3 месяца", "add": 900},
    "6_month": {"name": "⭐ Telegram Premium 6 месяцев", "add": 1200},
    "1_year": {"name": "⭐ Telegram Premium 1 год", "add": 2000}
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ==================== БАЗА ДАННЫХ ====================
DB_NAME = "shop.db"

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            premium_name TEXT,
            premium_price INTEGER,
            country_name TEXT,
            country_price INTEGER,
            total_price INTEGER,
            address TEXT,
            screenshot TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS blocked_users (user_id INTEGER PRIMARY KEY)''')
init_db()

def save_order(user_id, username, premium_name, premium_price, country_name, country_price, total, address):
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        cur.execute('''INSERT INTO orders (user_id, username, premium_name, premium_price, country_name, country_price, total_price, address) 
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (user_id, username, premium_name, premium_price, country_name, country_price, total, address))
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
    premium = State()
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

def premium_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for code, p in PREMIUM_PLANS.items():
        text = p['name']
        if p['add'] > 0:
            text += f" +{p['add']}₽"
        kb.inline_keyboard.append([InlineKeyboardButton(text=text, callback_data=f"prem_{code}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ НАЗАД", callback_data="back")])
    return kb

def country_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for code, c in COUNTRY_PRICES.items():
        text = f"{c['name']} — {c['price']}₽"
        if c.get('warning'):
            text += " ⚠️"
        kb.inline_keyboard.append([InlineKeyboardButton(text=text, callback_data=f"cnt_{code}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_prem")])
    return kb

# ==================== ХЕНДЛЕРЫ ====================
@dp.message(Command("start"))
async def start(m: types.Message):
    if is_blocked(m.from_user.id):
        await m.answer("⛔ Заблокирован")
        return
    if not await is_subscribed(m.from_user.id):
        await m.answer("📢 Подпишитесь на канал!", reply_markup=sub_kb())
        return
    await m.answer("📱 Добро пожаловать!", reply_markup=main_kb())

@dp.callback_query(F.data == "check_sub")
async def check_sub(c: types.CallbackQuery):
    if await is_subscribed(c.from_user.id):
        await c.message.edit_text("✅ Доступ открыт", reply_markup=main_kb())
    else:
        await c.answer("❌ Не подписан!", show_alert=True)

@dp.callback_query(F.data == "back")
async def back_main(c: types.CallbackQuery):
    await c.message.edit_text("📱 Главное меню", reply_markup=main_kb())

@dp.callback_query(F.data == "back_prem")
async def back_to_premium(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(Order.premium)
    await c.message.edit_text("💰 ШАГ 1: ВЫБОР PREMIUM", reply_markup=premium_kb())

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
    text = "📦 *Мои заказы*\n\n"
    for o in ords:
        status = "✅" if o[10] == "completed" else "⏳"
        text += f"{status} #{o[0]} | {o[7]}₽ | {o[4]}\n"
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="back")]]))

@dp.callback_query(F.data == "buy")
async def buy(c: types.CallbackQuery, state: FSMContext):
    if not await is_subscribed(c.from_user.id):
        await c.answer("Подпишись!", show_alert=True)
        return
    await state.set_state(Order.premium)
    await c.message.edit_text("💰 *ШАГ 1: ВЫБОР PREMIUM*", parse_mode="Markdown", reply_markup=premium_kb())

@dp.callback_query(Order.premium, F.data.startswith("prem_"))
async def select_premium(c: types.CallbackQuery, state: FSMContext):
    code = c.data.split("_")[1]
    prem = PREMIUM_PLANS[code]
    await state.update_data(premium_name=prem["name"], premium_price=prem["add"])
    await state.set_state(Order.country)
    await c.message.edit_text("🌍 *ШАГ 2: ВЫБОР СТРАНЫ*", parse_mode="Markdown", reply_markup=country_kb())
    await c.answer()

@dp.callback_query(Order.country, F.data.startswith("cnt_"))
async def select_country(c: types.CallbackQuery, state: FSMContext):
    code = c.data.split("_")[1]
    country = COUNTRY_PRICES[code]
    data = await state.get_data()
    total = country["price"] + data.get("premium_price", 0)
    await state.update_data(
        country_name=country["name"],
        country_price=country["price"],
        total_price=total
    )
    await state.set_state(Order.address)
    
    warning = f"\n\n{country['warning']}" if country.get("warning") else ""
    await c.message.edit_text(
        f"✅ Premium: {data['premium_name']} +{data['premium_price']}₽\n"
        f"✅ Страна: {country['name']} — {country['price']}₽{warning}\n\n"
        f"💰 *ИТОГО: {total}₽*\n\n"
        f"📬 Введите ваш @username для доставки:",
        parse_mode="Markdown"
    )
    await c.answer()

@dp.message(Order.address)
async def get_address(m: types.Message, state: FSMContext):
    addr = m.text.strip()
    if not addr.startswith("@"):
        addr = "@" + addr
    if len(addr) < 3:
        await m.answer("❌ Введите корректный username (например, @durov)")
        return
    
    data = await state.get_data()
    order_id = save_order(
        m.from_user.id,
        m.from_user.username or "no_username",
        data["premium_name"],
        data["premium_price"],
        data["country_name"],
        data["country_price"],
        data["total_price"],
        addr
    )
    await state.update_data(order_id=order_id)
    await state.set_state(Order.screenshot)
    
    await m.answer(
        f"💳 *ЗАКАЗ #{order_id}*\n"
        f"💰 Сумма: {data['total_price']}₽\n\n"
        f"📌 *РЕКВИЗИТЫ:*\n"
        f"СБП: `{PHONE_NUMBER}`\n"
        f"Получатель: {RECIPIENT_NAME}\n\n"
        f"✅ ОПЛАТИЛ? → Отправь СКРИНШОТ чека!",
        parse_mode="Markdown"
    )

@dp.message(Order.screenshot, F.photo)
async def get_screenshot(m: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    if not order_id:
        await m.answer("❌ Ошибка, начни заново /start")
        await state.clear()
        return
    
    update_screenshot(order_id, m.photo[-1].file_id)
    update_status(order_id, "processing")
    
    await m.answer(f"✅ *ЗАКАЗ #{order_id} ПРИНЯТ!*\nВыдадим в течение 2-4 часов.", parse_mode="Markdown")
    await bot.send_message(ADMIN_ID, f"🆕 НОВЫЙ ЗАКАЗ #{order_id}\n👤 {m.from_user.id}\n💰 {data['total_price']}₽")
    await state.clear()

@dp.message(Order.screenshot)
async def wrong_screenshot(m: types.Message):
    await m.answer("❌ Отправь ФОТО чека!")

# ==================== ЗАПУСК ====================
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
