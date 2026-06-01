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

def get_all_orders_by_status(status):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM orders WHERE status = ? ORDER BY id DESC", (status,)).fetchall()

def get_order(oid):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()

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
class OrderForm(StatesGroup):
    country = State()
    address = State()
    screenshot = State()

# ==================== КЛАВИАТУРЫ ====================
def main_kb(user_id):
    # Базовые кнопки для всех
    buttons = [
        [InlineKeyboardButton(text="📱 Купить аккаунт", callback_data="buy")],
        [InlineKeyboardButton(text="📦 Мои заказы", callback_data="orders")],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="support")]
    ]
    # Если пользователь админ, добавляем кнопку админ-панели
    if user_id == ADMIN_ID:
        buttons.append([InlineKeyboardButton(text="🔐 Админ панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def country_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for code, c in COUNTRY_PRICES.items():
        text = f"{c['name']} — {c['price']}₽"
        if c.get('warning'):
            text += " ⚠️"
        kb.inline_keyboard.append([InlineKeyboardButton(text=text, callback_data=f"cnt_{code}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="back")])
    return kb

def admin_main_kb():
    """Клавиатура админ-панели"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟡 Заказы на проверке", callback_data="admin_processing")],
        [InlineKeyboardButton(text="✅ Подтверждённые", callback_data="admin_completed")],
        [InlineKeyboardButton(text="❌ Отклонённые", callback_data="admin_rejected")],
        [InlineKeyboardButton(text="◀ Назад в главное меню", callback_data="back")]
    ])

def admin_order_kb(order_id):
    """Клавиатура для отдельного заказа в админ-панели"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_{order_id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{order_id}")],
        [InlineKeyboardButton(text="◀ Назад к списку", callback_data="admin_processing")]
    ])

def admin_back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀ Назад в админ-панель", callback_data="admin_panel")]
    ])

# ==================== ХЕНДЛЕРЫ ====================
@dp.message(Command("start"))
async def start(m: types.Message, state: FSMContext):
    await state.clear()
    if is_blocked(m.from_user.id):
        await m.answer("⛔ Вы заблокированы.")
        return
    if not await is_subscribed(m.from_user.id):
        await m.answer("📢 Подпишитесь на канал, чтобы пользоваться магазином!", reply_markup=sub_kb())
        return
    await m.answer("🌟 Добро пожаловать в магазин аккаунтов Telegram!", reply_markup=main_kb(m.from_user.id))

@dp.callback_query(F.data == "check_sub")
async def check_sub(c: types.CallbackQuery):
    if await is_subscribed(c.from_user.id):
        await c.message.edit_text("✅ Доступ открыт", reply_markup=main_kb(c.from_user.id))
    else:
        await c.answer("❌ Вы не подписаны!", show_alert=True)

@dp.callback_query(F.data == "back")
async def back(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("🌟 Главное меню", reply_markup=main_kb(c.from_user.id))

@dp.callback_query(F.data == "support")
async def support(c: types.CallbackQuery):
    await c.answer()
    await c.message.answer(f"🆘 Связь с поддержкой: {SUPPORT_USERNAME}")

@dp.callback_query(F.data == "orders")
async def my_orders(c: types.CallbackQuery):
    orders = get_orders(c.from_user.id)
    if not orders:
        await c.answer("У вас пока нет заказов", show_alert=True)
        return
    text = "📦 *Мои заказы*\n\n"
    for o in orders:
        status_emoji = "✅" if o[7] == "completed" else "⏳" if o[7] == "processing" else "❌"
        status_text = "Оплачено" if o[7] == "completed" else "На проверке" if o[7] == "processing" else "Отклонён"
        text += f"{status_emoji} *Заказ #{o[0]}* | {o[4]}₽ | {o[2]} | {status_text}\n"
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Назад", callback_data="back")]]))

@dp.callback_query(F.data == "buy")
async def buy(c: types.CallbackQuery, state: FSMContext):
    if not await is_subscribed(c.from_user.id):
        await c.answer("Подпишитесь на канал!", show_alert=True)
        return
    await state.set_state(OrderForm.country)
    await c.message.edit_text("🌍 *Выберите страну аккаунта:*", parse_mode="Markdown", reply_markup=country_kb())

# ==================== АДМИН-ПАНЕЛЬ ====================
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Доступ запрещён", show_alert=True)
        return
    await c.message.edit_text("🔐 *Админ панель*\nВыберите категорию:", parse_mode="Markdown", reply_markup=admin_main_kb())

@dp.callback_query(F.data == "admin_processing")
async def admin_processing(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    orders = get_all_orders_by_status("processing")
    if not orders:
        await c.message.edit_text("Нет заказов на проверке.", reply_markup=admin_back_kb())
        return
    # Показываем первый заказ
    await show_order_for_admin(c.message, orders[0])

@dp.callback_query(F.data == "admin_completed")
async def admin_completed(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    orders = get_all_orders_by_status("completed")
    if not orders:
        await c.message.edit_text("Нет подтверждённых заказов.", reply_markup=admin_back_kb())
        return
    text = "✅ *Подтверждённые заказы*\n\n"
    for o in orders:
        text += f"#{o[0]} | {o[4]}₽ | {o[2]} | {o[5]}\n"
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_back_kb())

@dp.callback_query(F.data == "admin_rejected")
async def admin_rejected(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    orders = get_all_orders_by_status("rejected")
    if not orders:
        await c.message.edit_text("Нет отклонённых заказов.", reply_markup=admin_back_kb())
        return
    text = "❌ *Отклонённые заказы*\n\n"
    for o in orders:
        text += f"#{o[0]} | {o[4]}₽ | {o[2]} | {o[5]}\n"
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_back_kb())

async def show_order_for_admin(message: types.Message, order):
    """Показать один заказ с кнопками подтверждения/отклонения"""
    order_id = order[0]
    user_id = order[1]
    username = order[2]
    country = order[3]
    price = order[4]
    address = order[5]
    screenshot = order[6]
    created = order[8]
    
    caption = (
        f"🆕 *Заказ #{order_id}*\n"
        f"👤 Пользователь: `{user_id}` @{username}\n"
        f"🌍 Страна: {country}\n"
        f"💰 Сумма: {price}₽\n"
        f"📬 Доставка: {address}\n"
        f"🕒 Создан: {created}\n"
        f"📸 Скриншот:"
    )
    if screenshot:
        await message.answer_photo(photo=screenshot, caption=caption, parse_mode="Markdown", reply_markup=admin_order_kb(order_id))
    else:
        await message.answer(caption, reply_markup=admin_order_kb(order_id))

# ==================== ПОДТВЕРЖДЕНИЕ/ОТКЛОНЕНИЕ (админ) ====================
@dp.callback_query(F.data.startswith("confirm_"))
async def confirm_payment(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer("❌ Только для администратора", show_alert=True)
        return
    order_id = int(c.data.split("_")[1])
    update_status(order_id, "completed")
    order = get_order(order_id)
    if order:
        user_id = order[1]
        country = order[3]
        price = order[4]
        address = order[5]
        try:
            await bot.send_message(
                user_id,
                f"✅ *Ваш заказ #{order_id} подтверждён!*\n\n"
                f"🌍 {country}\n"
                f"💰 {price}₽\n"
                f"📬 Доставка: {address}\n\n"
                f"Аккаунт будет отправлен в течение 2‑4 часов.\n"
                f"По вопросам: {SUPPORT_USERNAME}",
                parse_mode="Markdown"
            )
        except:
            pass
    await c.message.edit_caption(caption=c.message.caption + "\n\n✅ ОПЛАЧЕНО (подтверждено администратором)")
    # Показать следующий заказ, если есть
    orders = get_all_orders_by_status("processing")
    if orders:
        await show_order_for_admin(c.message, orders[0])
    else:
        await c.message.answer("Больше нет заказов на проверке.", reply_markup=admin_back_kb())
    await c.answer("Оплата подтверждена!")

@dp.callback_query(F.data.startswith("reject_"))
async def reject_payment(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer("❌ Только для администратора", show_alert=True)
        return
    order_id = int(c.data.split("_")[1])
    update_status(order_id, "rejected")
    order = get_order(order_id)
    if order:
        user_id = order[1]
        try:
            await bot.send_message(
                user_id,
                f"❌ *Заказ #{order_id} отклонён*\n\n"
                f"Причина: чек не соответствует оплате или нечитаем.\n"
                f"Пожалуйста, отправьте корректный скриншот чека заново.\n"
                f"Для этого начните оформление заново с /start",
                parse_mode="Markdown"
            )
        except:
            pass
    await c.message.edit_caption(caption=c.message.caption + "\n\n❌ ОТКЛОНЁН (чек некорректен)")
    orders = get_all_orders_by_status("processing")
    if orders:
        await show_order_for_admin(c.message, orders[0])
    else:
        await c.message.answer("Больше нет заказов на проверке.", reply_markup=admin_back_kb())
    await c.answer("Платёж отклонён!")

# ==================== ВЫБОР СТРАНЫ И ПОЛУЧЕНИЕ АДРЕСА ====================
@dp.callback_query(OrderForm.country, F.data.startswith("cnt_"))
async def select_country(c: types.CallbackQuery, state: FSMContext):
    code = c.data.split("_")[1]
    country = COUNTRY_PRICES[code]
    await state.update_data(country_name=country["name"], price=country["price"])
    await state.set_state(OrderForm.address)
    warning = f"\n\n{country['warning']}" if country.get("warning") else ""
    await c.message.edit_text(
        f"✅ *Страна:* {country['name']} — {country['price']}₽{warning}\n\n"
        f"💰 *Итого к оплате:* {country['price']}₽\n\n"
        f"📬 *Введите ваш Telegram username для доставки:*\nПример: @durov",
        parse_mode="Markdown"
    )
    await c.answer()

@dp.message(OrderForm.address)
async def get_address(m: types.Message, state: FSMContext):
    addr = m.text.strip()
    if not addr.startswith("@"):
        addr = "@" + addr
    if len(addr) < 3:
        await m.answer("❌ Слишком короткий username. Пример: @durov")
        return
    
    data = await state.get_data()
    if not data.get("country_name"):
        await m.answer("❌ Ошибка. Начните заново /start")
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
    await state.set_state(OrderForm.screenshot)
    
    await m.answer(
        f"💳 *ЗАКАЗ #{order_id}*\n"
        f"💰 Сумма: {data['price']}₽\n\n"
        f"📌 *Реквизиты для оплаты:*\n"
        f"📱 СБП: `{PHONE_NUMBER}`\n"
        f"👤 Получатель: {RECIPIENT_NAME}\n\n"
        f"✅ *После оплаты отправьте СКРИНШОТ чека сюда.*\n"
        f"Без скриншота заказ не будет обработан.\n\n"
        f"📞 Вопросы: {SUPPORT_USERNAME}",
        parse_mode="Markdown"
    )

@dp.message(OrderForm.screenshot, F.photo)
async def handle_screenshot(m: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    if not order_id:
        await m.answer("❌ Ошибка. Начните заново /start")
        await state.clear()
        return
    
    file_id = m.photo[-1].file_id
    update_screenshot(order_id, file_id)
    update_status(order_id, "processing")  # на проверке
    
    await m.answer(
        f"📸 *Скриншот для заказа #{order_id} получен!*\n"
        f"Ожидайте подтверждения администратора. Обычно это занимает до 2 часов.\n"
        f"Статус заказа можно проверить в разделе «Мои заказы».",
        parse_mode="Markdown"
    )
    
    # Отправляем админу уведомление + фото (дублируем, но в админ-панели уже видно)
    order = get_order(order_id)
    await bot.send_message(
        ADMIN_ID,
        f"🆕 Поступил новый чек для заказа #{order_id}\nПроверьте админ-панель."
    )
    await state.clear()

@dp.message(OrderForm.screenshot)
async def wrong_input(m: types.Message):
    await m.answer("❌ Пожалуйста, отправьте ФОТО (скриншот чека). Текстовые сообщения не принимаются.")

# ==================== ЗАПУСК ====================
async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.create_task(self_pinger())
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен с админ-панелью")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
