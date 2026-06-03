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

# Список администраторов (через запятую в .env)
ADMINS_IDS = list(map(int, os.getenv("ADMINS_IDS", "8276815852,8840342301").split(',')))

SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@HowScad_support")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "+79027365759")
RECIPIENT_NAME = os.getenv("RECIPIENT_NAME", "Егор")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@Howscard")
REQUIRED_CHANNEL_LINK = os.getenv("REQUIRED_CHANNEL_LINK", "https://t.me/Howscard")
PORT = int(os.getenv("PORT", "8080"))

FTZ_PRICE = 200

TELEGRAM_PREMIUM_PRICES = {
    "no_premium": {"name": "❌ Без Premium", "premium_price": 0},
    "1_month": {"name": "⭐ Telegram Premium 1 месяц", "premium_price": 300},
    "3_month": {"name": "⭐ Telegram Premium 3 месяца", "premium_price": 900},
    "6_month": {"name": "⭐ Telegram Premium 6 месяцев", "premium_price": 1200},
    "1_year": {"name": "⭐ Telegram Premium 1 год", "premium_price": 2000}
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
                premium_plan TEXT,
                premium_price INTEGER,
                ftz_price INTEGER,
                total_price INTEGER,
                address TEXT,
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

def save_order(user_id, username, premium_name, premium_price, total_price, address):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.execute(
            "INSERT INTO orders (user_id, username, premium_plan, premium_price, ftz_price, total_price, address) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, premium_name, premium_price, FTZ_PRICE, total_price, address)
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

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def is_admin(user_id: int) -> bool:
    return user_id in ADMINS_IDS

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

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 ЗАЯВКИ НА ПОДТВЕРЖДЕНИЕ", callback_data="admin_pending")],
        [InlineKeyboardButton(text="📜 ВСЕ ЗАКАЗЫ", callback_data="admin_all_orders")],
        [InlineKeyboardButton(text="🚫 ЗАБЛОКИРОВАТЬ ПОЛЬЗОВАТЕЛЯ", callback_data="admin_block")],
        [InlineKeyboardButton(text="🔄 ОТКРЫТЬ МЕНЮ", callback_data="back_main")]
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

# ==================== ОБРАБОТЧИКИ ПОЛЬЗОВАТЕЛЯ ====================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if is_user_blocked(message.from_user.id):
        await message.answer("⛔ Вы заблокированы.")
        return
    
    # Если пользователь - администратор, показываем админ-панель
    if is_admin(message.from_user.id):
        await message.answer(
            "👑 *АДМИН-ПАНЕЛЬ*\n\nВыберите действие:",
            parse_mode="Markdown",
            reply_markup=admin_menu()
        )
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
        "🌟 *ДОБРО ПОЖАЛОВАТЬ В МАГАЗИН!* 🌟\n\n"
        "📱 Аккаунты Telegram с Premium\n\n"
        "🔽 *Чтобы сделать заказ, нажмите на кнопку:*",
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
    if is_admin(callback.from_user.id):
        await callback.message.edit_text(
            "👑 *АДМИН-ПАНЕЛЬ*\n\nВыберите действие:",
            parse_mode="Markdown",
            reply_markup=admin_menu()
        )
    else:
        await callback.message.edit_text(
            "🌟 *Магазин аккаунтов*\n\nВыберите действие:",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )
    await callback.answer()

@dp.callback_query(F.data == "support")
async def support_callback(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        f"🆘 *Поддержка*\n\n{SUPPORT_USERNAME}",
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "buy_number")
async def buy_number(callback: types.CallbackQuery, state: FSMContext):
    if not await is_subscribed(callback.from_user.id):
        await callback.answer("❌ Подпишитесь на канал!", show_alert=True)
        return
    await state.set_state(OrderForm.waiting_for_premium)
    await callback.message.edit_text(
        f"💰 *Базовая цена:* {FTZ_PRICE} ₽\n\n"
        f"⭐ *Выберите опцию Premium:*",
        parse_mode="Markdown",
        reply_markup=premium_keyboard()
    )
    await callback.answer()

@dp.callback_query(OrderForm.waiting_for_premium, F.data.startswith("premium_"))
async def select_premium(callback: types.CallbackQuery, state: FSMContext):
    premium_code = callback.data.replace("premium_", "")
    
    if premium_code not in TELEGRAM_PREMIUM_PRICES:
        await callback.answer("❌ Неверный выбор", show_alert=True)
        return
    
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
        f"💰 *ИТОГО:* {total_price} ₽\n\n"
        f"📬 *Введите ваш username для доставки:*",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(OrderForm.waiting_for_address)
async def process_address(message: types.Message, state: FSMContext):
    address = message.text.strip()
    if not address.startswith("@"):
        address = "@" + address
    
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
        f"💳 *ЗАКАЗ #{order_id}*\n\n"
        f"💰 *Сумма:* {data['total_price']} ₽\n\n"
        f"📋 *Состав:*\n"
        f"• Аккаунт — {FTZ_PRICE} ₽\n"
        f"• {data['premium_name']} — +{data['premium_price']} ₽\n\n"
        "📌 *Реквизиты:*\n"
        f"📱 СБП: `{PHONE_NUMBER}`\n"
        f"👤 Получатель: *{RECIPIENT_NAME}*\n\n"
        "✅ *Оплатили? Отправьте СКРИНШОТ чека!*",
        parse_mode="Markdown"
    )

@dp.message(OrderForm.waiting_for_screenshot, F.photo)
async def process_screenshot(message: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    
    if not order_id:
        await message.answer("❌ Ошибка. Начните заказ заново через /start")
        await state.clear()
        return
    
    photo = message.photo[-1]
    update_order_screenshot(order_id, photo.file_id)
    
    await state.clear()
    
    await message.answer(
        f"✅ *ЗАКАЗ #{order_id} ПРИНЯТ!*\n\n"
        f"⏳ Ожидайте подтверждения оплаты.\n"
        f"📞 Вопросы: {SUPPORT_USERNAME}",
        parse_mode="Markdown"
    )
    
    # Уведомление ВСЕМ администраторам
    for admin_id in ADMINS_IDS:
        await bot.send_photo(
            admin_id,
            photo.file_id,
            caption=f"🆕 *НОВЫЙ ЗАКАЗ #{order_id}!*\n\n"
            f"👤 Пользователь: @{message.from_user.username or 'нет username'} (ID: {message.from_user.id})\n"
            f"⭐ {data['premium_name']}\n"
            f"💰 Сумма: {data['total_price']} ₽\n"
            f"📬 Доставка: {data.get('address')}\n\n"
            f"➡️ Для подтверждения используйте кнопки ниже:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ ПОДТВЕРДИТЬ", callback_data=f"confirm_{order_id}")],
                [InlineKeyboardButton(text="❌ ОТКЛОНИТЬ", callback_data=f"reject_{order_id}")]
            ])
        )

@dp.message(OrderForm.waiting_for_screenshot)
async def invalid_screenshot(message: types.Message):
    await message.answer("❌ *Отправьте ФОТО чека!*", parse_mode="Markdown")

@dp.callback_query(F.data == "my_orders")
async def my_orders(callback: types.CallbackQuery):
    orders = get_user_orders(callback.from_user.id)
    
    if not orders:
        await callback.answer("📭 У вас пока нет заказов", show_alert=True)
        return
    
    text = "📦 *МОИ ЗАКАЗЫ*\n\n"
    status_names = {
        'pending': '⏳ Ожидает оплаты',
        'waiting_confirmation': '🔄 Ожидает подтверждения',
        'confirmed': '✅ ПОДТВЕРЖДЕН',
        'rejected': '❌ Отклонен'
    }
    
    for order in orders:
        status = status_names.get(order[9], order[9])
        created = datetime.strptime(order[10], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        text += f"#{order[0]} | {order[6]} ₽ | {created}\n{status}\n\n"
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")]
    ]))
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
            "✅ *Нет заявок на подтверждение*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")]
            ])
        )
        await callback.answer()
        return
    
    for order in orders:
        order_id, user_id, username, premium_plan, premium_price, ftz_price, total_price, address, screenshot_file_id, status, created_at = order[:11]
        
        text = f"📋 *ЗАЯВКА #{order_id}*\n\n"
        text += f"👤 Пользователь: @{username or 'нет username'} (ID: {user_id})\n"
        text += f"⭐ {premium_plan}\n"
        text += f"💰 Сумма: {total_price} ₽\n"
        text += f"📬 Доставка: {address}\n"
        text += f"🕐 Создан: {created_at}\n\n"
        
        if screenshot_file_id:
            await callback.message.answer_photo(
                screenshot_file_id,
                caption=text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ ПОДТВЕРДИТЬ", callback_data=f"confirm_{order_id}")],
                    [InlineKeyboardButton(text="❌ ОТКЛОНИТЬ", callback_data=f"reject_{order_id}")],
                    [InlineKeyboardButton(text="◀ НАЗАД В МЕНЮ", callback_data="back_main")]
                ])
            )
    
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
                [InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")]
            ])
        )
        await callback.answer()
        return
    
    status_emoji = {
        'pending': '⏳',
        'waiting_confirmation': '🔄',
        'confirmed': '✅',
        'rejected': '❌'
    }
    
    text = "📜 *ВСЕ ЗАКАЗЫ (последние 50)*\n\n"
    for order in orders[:20]:
        emoji = status_emoji.get(order[9], '❓')
        text += f"{emoji} #{order[0]} | {order[6]}₽ | @{order[2] or 'no name'}\n"
    
    text += f"\n📊 *Всего заказов:* {len(orders)}"
    
    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 ЗАЯВКИ НА ПОДТВЕРЖДЕНИЕ", callback_data="admin_pending")],
            [InlineKeyboardButton(text="◀ НАЗАД", callback_data="back_main")]
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
        "🚫 *БЛОКИРОВКА ПОЛЬЗОВАТЕЛЯ*\n\n"
        "Введите ID пользователя для блокировки:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀ ОТМЕНА", callback_data="back_main")]
        ])
    )
    await callback.answer()

@dp.message(F.text, lambda m: is_admin(m.from_user.id))
async def process_block_user(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state == "waiting_for_block_user":
        try:
            user_id = int(message.text.strip())
            block_user(user_id)
            await message.answer(f"✅ Пользователь {user_id} заблокирован!")
            await state.clear()
            await cmd_start(message)
        except ValueError:
            await message.answer("❌ Неверный ID! Введите число.")

@dp.callback_query(F.data.startswith("confirm_"))
async def confirm_order_admin(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    
    order_id = int(callback.data.split("_")[1])
    confirm_order(order_id)
    
    # Получаем данные заказа
    with sqlite3.connect(DB_NAME) as conn:
        order = conn.execute("SELECT user_id, total_price FROM orders WHERE id = ?", (order_id,)).fetchone()
    
    if order:
        user_id, total_price = order
        # Уведомляем пользователя
        await bot.send_message(
            user_id,
            f"✅ *ВАШ ЗАКАЗ #{order_id} ПОДТВЕРЖДЕН!*\n\n"
            f"💰 Сумма: {total_price} ₽\n"
            f"🎉 Спасибо за покупку!\n\n"
            f"📞 Вопросы: {SUPPORT_USERNAME}",
            parse_mode="Markdown"
        )
    
    await callback.answer(f"✅ Заказ #{order_id} подтвержден!")
    
    # Обновляем сообщение (если это фото с подписью)
    if callback.message.caption:
        await callback.message.edit_caption(
            caption=callback.message.caption + "\n\n✅ **ПОДТВЕРЖДЕН**",
            parse_mode="Markdown"
        )

@dp.callback_query(F.data.startswith("reject_"))
async def reject_order_admin(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещен!", show_alert=True)
        return
    
    order_id = int(callback.data.split("_")[1])
    reject_order(order_id)
    
    # Получаем данные заказа
    with sqlite3.connect(DB_NAME) as conn:
        order = conn.execute("SELECT user_id, total_price FROM orders WHERE id = ?", (order_id,)).fetchone()
    
    if order:
        user_id, total_price = order
        # Уведомляем пользователя
        await bot.send_message(
            user_id,
            f"❌ *ЗАКАЗ #{order_id} ОТКЛОНЕН*\n\n"
            f"💰 Сумма: {total_price} ₽\n"
            f"📞 Свяжитесь с поддержкой: {SUPPORT_USERNAME}",
            parse_mode="Markdown"
        )
    
    await callback.answer(f"❌ Заказ #{order_id} отклонен!")
    
    if callback.message.caption:
        await callback.message.edit_caption(
            caption=callback.message.caption + "\n\n❌ **ОТКЛОНЕН**",
            parse_mode="Markdown"
        )

# ==================== ЗАПУСК ====================
async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.create_task(self_pinger())
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info(f"Бот запущен! Администраторы: {ADMINS_IDS}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
