#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
import sqlite3
import os
import threading
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

# ---------- КОНФИГУРАЦИЯ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

# АДМИНИСТРАТОРЫ (укажите свои ID)
ADMIN_IDS = [8276815852, 8840342301]   # ваш ID и ID коллеги

SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@HowScad_support")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "+79027365759")
RECIPIENT_NAME = os.getenv("RECIPIENT_NAME", "Егор")
PORT = int(os.getenv("PORT", "8080"))

# Товары
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Flask для пинга (чтобы Render не усыплял бота)
flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return "OK", 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=PORT)

# ---------- БАЗА ДАННЫХ ----------
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
                status TEXT DEFAULT 'waiting_confirmation',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        conn.execute("UPDATE orders SET screenshot_file_id = ? WHERE id = ?", (file_id, order_id))

def confirm_order(order_id):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE orders SET status = 'confirmed' WHERE id = ?", (order_id,))

def get_pending_orders():
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM orders WHERE status = 'waiting_confirmation' ORDER BY id DESC").fetchall()

def get_all_orders():
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()

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
    return user_id in ADMIN_IDS

# ---------- КЛАВИАТУРЫ ----------
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
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"{info['name']} — {info['price']}₽", callback_data=f"country_{code}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")])
    return kb

def stars_keyboard():
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for stars, price in STARS_PACKS.items():
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"⭐ {stars} — {price}₽", callback_data=f"stars_{stars}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")])
    return kb

# ---------- FSM ----------
class OrderForm(StatesGroup):
    waiting_for_country = State()
    waiting_for_stars = State()
    waiting_for_screenshot = State()

# ---------- ОБРАБОТЧИКИ ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if is_user_blocked(message.from_user.id):
        await message.answer("⛔ Вы заблокированы.")
        return
    await message.answer(
        "🌟 *ДОБРО ПОЖАЛОВАТЬ В МАГАЗИН!*\n\n"
        "📱 Виртуальные номера Telegram\n⭐ Telegram Stars\n\n"
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=main_menu(is_admin(message.from_user.id))
    )

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
    await callback.message.answer(f"🆘 *Поддержка*\n\n{SUPPORT_USERNAME}", parse_mode="Markdown")

@dp.callback_query(F.data == "buy_number")
async def buy_number(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(OrderForm.waiting_for_country)
    await callback.message.edit_text("📱 *Выберите страну:*", parse_mode="Markdown", reply_markup=countries_keyboard())
    await callback.answer()

@dp.callback_query(OrderForm.waiting_for_country, F.data.startswith("country_"))
async def select_country(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split("_")[1]
    country = COUNTRIES.get(code)
    if not country:
        await callback.answer("❌ Неверный выбор", show_alert=True)
        return
    await state.update_data(product_type="number", product_name=country['name'], product_price=country['price'], product_details=code)
    await state.set_state(OrderForm.waiting_for_screenshot)
    await callback.message.edit_text(
        f"💳 *Оплата*\n\n📱 {country['name']}\n💰 Сумма: {country['price']} ₽\n\n"
        "📌 *Реквизиты:*\n📱 СБП: `{PHONE_NUMBER}`\n👤 Получатель: *{RECIPIENT_NAME}*\n\n"
        "✅ *Оплатили? Отправьте СКРИНШОТ чека!*",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "buy_stars")
async def buy_stars(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(OrderForm.waiting_for_stars)
    await callback.message.edit_text("⭐ *Выберите количество Stars:*", parse_mode="Markdown", reply_markup=stars_keyboard())
    await callback.answer()

@dp.callback_query(OrderForm.waiting_for_stars, F.data.startswith("stars_"))
async def select_stars(callback: types.CallbackQuery, state: FSMContext):
    stars = int(callback.data.split("_")[1])
    price = STARS_PACKS.get(stars)
    if not price:
        await callback.answer("❌ Неверный выбор", show_alert=True)
        return
    await state.update_data(product_type="stars", product_name=f"{stars}⭐", product_price=price, product_details=str(stars))
    await state.set_state(OrderForm.waiting_for_screenshot)
    await callback.message.edit_text(
        f"💳 *Оплата*\n\n⭐ {stars} Telegram Stars\n💰 Сумма: {price} ₽\n\n"
        "📌 *Реквизиты:*\n📱 СБП: `{PHONE_NUMBER}`\n👤 Получатель: *{RECIPIENT_NAME}*\n\n"
        "✅ *Оплатили? Отправьте СКРИНШОТ чека!*",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(OrderForm.waiting_for_screenshot, F.photo)
async def process_screenshot(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if not data:
        await message.answer("❌ Ошибка. Начните заказ заново.")
        await state.clear()
        return

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
        f"📦 {data['product_name']}\n💰 Сумма: {data['product_price']} ₽\n\n"
        f"🕒 Статус: На проверке\n\n📞 {SUPPORT_USERNAME}",
        parse_mode="Markdown"
    )

    # ==== УВЕДОМЛЕНИЕ АДМИНИСТРАТОРАМ ====
    admin_text = (
        f"🆕 *НОВЫЙ ЗАКАЗ #{order_id}!*\n\n"
        f"📱 *Товар:* {data['product_name']}\n"
        f"👤 *Пользователь:* @{message.from_user.username or 'нет username'} [id: {message.from_user.id}]\n"
        f"💰 *Сумма:* {data['product_price']} ₽\n"
        f"🕐 *Время:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_text, parse_mode="Markdown")
            await bot.send_photo(admin_id, photo.file_id, caption=f"📸 Чек к заказу #{order_id}")
            logger.info(f"Уведомление отправлено админу {admin_id}")
        except Exception as e:
            logger.error(f"Ошибка отправки админу {admin_id}: {e}")

@dp.message(OrderForm.waiting_for_screenshot)
async def invalid_screenshot(message: types.Message):
    await message.answer("❌ *Отправьте ФОТО чека!*", parse_mode="Markdown")

@dp.callback_query(F.data == "my_orders")
async def my_orders(callback: types.CallbackQuery):
    orders = get_user_orders(callback.from_user.id)
    if not orders:
        await callback.answer("📭 У вас пока нет заказов", show_alert=True)
        return
    text = "📦 *Мои заказы*\n\n"
    status_names = {'waiting_confirmation': '🔄 На проверке', 'confirmed': '✅ ПРИНЯТ'}
    for order in orders:
        status = status_names.get(order[8], order[8])
        created = datetime.strptime(order[9], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        text += f"#{order[0]} | {order[5]}₽ | {created}\n{status}\n\n"
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")]]))
    await callback.answer()

# ---------- АДМИН-ОБРАБОТЧИКИ ----------
@dp.callback_query(F.data == "admin_pending")
async def admin_pending_orders(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    orders = get_pending_orders()
    if not orders:
        await callback.message.edit_text("✅ *Нет заявок на проверке*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]]))
        return
    for order in orders:
        order_id, user_id, username, ptype, pname, price, details, screenshot, status, created_at = order[:10]
        text = f"📋 *ЗАЯВКА #{order_id}*\n\n👤 *Пользователь:* @{username or 'нет username'} [ID: {user_id}]\n📦 *Товар:* {pname}\n💰 *Сумма:* {price} ₽\n🕐 *Создан:* {created_at}\n\n📸 *Чек:*"
        await callback.message.answer_photo(
            screenshot,
            caption=text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ ПРИНЯТЬ", callback_data=f"confirm_{order_id}")],
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
        await callback.message.edit_text("📭 *Нет заказов*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]]))
        return
    text = "📜 *ВСЕ ЗАКАЗЫ*\n\n"
    for order in orders[:20]:
        emoji = '🔄' if order[8] == 'waiting_confirmation' else '✅'
        text += f"{emoji} #{order[0]} | {order[5]}₽ | @{order[2] or 'no name'}\n"
    text += f"\n📊 *Всего:* {len(orders)}"
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ НАЗАД", callback_data="admin_panel")]]))
    await callback.answer()

@dp.callback_query(F.data == "admin_block")
async def admin_block_user(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    await state.set_state("waiting_for_block_user")
    await callback.message.edit_text("🚫 *БЛОКИРОВКА*\n\nВведите ID пользователя:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ ОТМЕНА", callback_data="admin_panel")]]))
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
        except:
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
        await bot.send_message(uid, f"✅ *ЗАКАЗ #{order_id} ПРИНЯТ!*\n\n📦 {pname}\n💰 Сумма: {price} ₽\n🎉 Спасибо за покупку!\n📞 {SUPPORT_USERNAME}", parse_mode="Markdown")
    await callback.answer(f"✅ Заказ #{order_id} принят")
    if callback.message.caption:
        await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ **ПРИНЯТ**")

# ---------- ЗАПУСК ----------
async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    # Keep-alive пинг
    async def keep_alive():
        import aiohttp
        while True:
            await asyncio.sleep(240)
            try:
                async with aiohttp.ClientSession() as sess:
                    await sess.get(f"http://localhost:{PORT}/")
            except:
                pass
    asyncio.create_task(keep_alive())
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info(f"✅ Бот запущен! Администраторы: {ADMIN_IDS}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
