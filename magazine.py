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

# Количество заказов на странице в админ-панели
ORDERS_PER_PAGE = 3

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
    flask_app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

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

def get_all_orders_by_status(status, limit=None, offset=None):
    with sqlite3.connect(DB_NAME) as conn:
        if limit is not None and offset is not None:
            return conn.execute("SELECT * FROM orders WHERE status = ? ORDER BY id DESC LIMIT ? OFFSET ?", (status, limit, offset)).fetchall()
        return conn.execute("SELECT * FROM orders WHERE status = ? ORDER BY id DESC", (status,)).fetchall()

def get_orders_count_by_status(status):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT COUNT(*) FROM orders WHERE status = ?", (status,)).fetchone()[0]

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
    buttons = [
        [InlineKeyboardButton(text="📱 Купить аккаунт", callback_data="buy")],
        [InlineKeyboardButton(text="📦 Мои заказы", callback_data="orders")],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="support")]
    ]
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
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟡 Заказы на проверке", callback_data="admin_processing_page_0")],
        [InlineKeyboardButton(text="✅ Подтверждённые", callback_data="admin_completed_page_0")],
        [InlineKeyboardButton(text="❌ Отклонённые", callback_data="admin_rejected_page_0")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back")]
    ])

def admin_pagination_kb(status, current_page, total_pages):
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    # Навигация по страницам
    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀◀", callback_data=f"admin_{status}_page_{current_page-1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"{current_page+1}/{total_pages}", callback_data="ignore"))
    if current_page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="▶▶", callback_data=f"admin_{status}_page_{current_page+1}"))
    if nav_buttons:
        kb.inline_keyboard.append(nav_buttons)
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад в админ-панель", callback_data="admin_panel")])
    return kb

def admin_order_controls_kb(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_{order_id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{order_id}")],
        [InlineKeyboardButton(text="◀ Назад к списку", callback_data="admin_processing_page_0")]
    ])

# ==================== ХЕНДЛЕРЫ ====================
@dp.message(Command("start"))
async def start(m: types.Message, state: FSMContext):
    await state.clear()
    if is_blocked(m.from_user.id):
        await m.answer("⛔ Вы заблокированы.")
        return
    if not await is_subscribed(m.from_user.id):
        await m.answer("📢 Подпишитесь на канал!", reply_markup=sub_kb())
        return
    await m.answer("🌟 Добро пожаловать в магазин аккаунтов Telegram!", reply_markup=main_kb(m.from_user.id))

@dp.callback_query(F.data == "check_sub")
async def check_sub(c: types.CallbackQuery):
    if await is_subscribed(c.from_user.id):
        await c.message.edit_text("✅ Доступ открыт", reply_markup=main_kb(c.from_user.id))
    else:
        await c.answer("❌ Не подписан!", show_alert=True)

@dp.callback_query(F.data == "back")
async def back(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("🌟 Главное меню", reply_markup=main_kb(c.from_user.id))

@dp.callback_query(F.data == "support")
async def support(c: types.CallbackQuery):
    await c.answer()
    await c.message.answer(f"🆘 Поддержка: {SUPPORT_USERNAME}")

@dp.callback_query(F.data == "orders")
async def my_orders(c: types.CallbackQuery):
    orders = get_orders(c.from_user.id)
    if not orders:
        await c.answer("Нет заказов", show_alert=True)
        return
    text = "📦 *Мои заказы*\n\n"
    for o in orders:
        status_emoji = "✅" if o[7] == "completed" else "⏳" if o[7] == "processing" else "❌"
        status_text = "Оплачено" if o[7] == "completed" else "На проверке" if o[7] == "processing" else "Отклонён"
        created = datetime.strptime(o[8], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        text += f"{status_emoji} *#{o[0]}* | {o[4]}₽ | {o[2]} | {status_text} | {created}\n"
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Назад", callback_data="back")]]))

@dp.callback_query(F.data == "buy")
async def buy(c: types.CallbackQuery, state: FSMContext):
    if not await is_subscribed(c.from_user.id):
        await c.answer("Подпишитесь!", show_alert=True)
        return
    await state.set_state(OrderForm.country)
    await c.message.edit_text("🌍 *Выберите страну аккаунта:*", parse_mode="Markdown", reply_markup=country_kb())

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
        f"Без скриншота заказ не будет обработан.",
        parse_mode="Markdown"
    )
    await bot.send_message(ADMIN_ID, f"📦 Новый заказ #{order_id} от @{m.from_user.username or m.from_user.id} на сумму {data['price']}₽")

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
    update_status(order_id, "processing")
    
    await m.answer(
        f"📸 *Скриншот для заказа #{order_id} получен!*\n"
        f"Ожидайте подтверждения администратора (обычно до 2 часов).\n"
        f"Проверить статус можно в разделе «Мои заказы».",
        parse_mode="Markdown"
    )
    await bot.send_message(ADMIN_ID, f"🆕 Поступил новый чек для заказа #{order_id}! Зайдите в админ-панель.")
    await state.clear()

@dp.message(OrderForm.screenshot)
async def wrong_screenshot(m: types.Message):
    await m.answer("❌ Отправьте именно ФОТО (скриншот чека). Текстовые сообщения не принимаются.")

# ==================== АДМИН-ПАНЕЛЬ ====================
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Доступ запрещён", show_alert=True)
        return
    await c.message.edit_text("🔐 *Админ панель*\nВыберите категорию заказов:", parse_mode="Markdown", reply_markup=admin_main_kb())

@dp.callback_query(F.data.startswith("admin_processing_page_"))
async def admin_processing(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    page = int(c.data.split("_")[-1])
    total = get_orders_count_by_status("processing")
    total_pages = (total + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE if total > 0 else 1
    
    orders = get_all_orders_by_status("processing", ORDERS_PER_PAGE, page * ORDERS_PER_PAGE)
    
    if not orders:
        await c.message.edit_text("📭 Нет заказов на проверке.", reply_markup=admin_pagination_kb("processing", page, total_pages))
        return
    
    text = f"🟡 *Заказы на проверке* (страница {page+1} из {total_pages})\n\n"
    for o in orders:
        text += f"┌ #{o[0]} | {o[4]}₽ | @{o[2] or o[1]}\n"
        text += f"└ 📬 {o[5]}\n\n"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    # Кнопки для каждого заказа
    for o in orders:
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"📦 Заказ #{o[0]}", callback_data=f"view_order_{o[0]}")])
    # Навигация
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀◀ Назад", callback_data=f"admin_processing_page_{page-1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ▶▶", callback_data=f"admin_processing_page_{page+1}"))
    if nav_buttons:
        kb.inline_keyboard.append(nav_buttons)
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")])
    
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data.startswith("view_order_"))
async def view_order(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    order_id = int(c.data.split("_")[-1])
    order = get_order(order_id)
    if not order:
        await c.answer("Заказ не найден", show_alert=True)
        return
    
    order_id, user_id, username, country, price, address, screenshot, status, created = order
    
    caption = (
        f"🆕 *Заказ #{order_id}*\n"
        f"👤 Пользователь: @{username or user_id} (id: `{user_id}`)\n"
        f"🌍 Страна: {country}\n"
        f"💰 Сумма: {price}₽\n"
        f"📬 Доставка: {address}\n"
        f"🕒 Создан: {created}\n"
        f"📊 Статус: {'На проверке' if status == 'processing' else status}\n\n"
        f"📸 Чек:"
    )
    
    if screenshot:
        await c.message.answer_photo(photo=screenshot, caption=caption, parse_mode="Markdown", reply_markup=admin_order_controls_kb(order_id))
    else:
        await c.message.answer(caption, reply_markup=admin_order_controls_kb(order_id))

@dp.callback_query(F.data.startswith("confirm_"))
async def confirm_payment(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Только для администратора", show_alert=True)
        return
    order_id = int(c.data.split("_")[1])
    update_status(order_id, "completed")
    order = get_order(order_id)
    if order:
        user_id = order[1]
        try:
            await bot.send_message(
                user_id,
                f"✅ *Ваш заказ #{order_id} подтверждён!*\n\n"
                f"Спасибо за покупку! Аккаунт будет отправлен в течение 2‑4 часов на указанный username.\n"
                f"По вопросам: {SUPPORT_USERNAME}",
                parse_mode="Markdown"
            )
        except:
            pass
    await c.message.edit_caption(caption=c.message.caption + "\n\n✅ ОПЛАЧЕНО И ПОДТВЕРЖДЕНО")
    await c.answer("Оплата подтверждена! Заказ помечен как выполненный.")

@dp.callback_query(F.data.startswith("reject_"))
async def reject_payment(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Только для администратора", show_alert=True)
        return
    order_id = int(c.data.split("_")[1])
    update_status(order_id, "rejected")
    order = get_order(order_id)
    if order:
        user_id = order[1]
        try:
            await bot.send_message(
                user_id,
                f"❌ *Ваш заказ #{order_id} отклонён*\n\n"
                f"Причина: чек не соответствует оплате или нечитаем.\n"
                f"Пожалуйста, отправьте корректный скриншот чека, начав заказ заново с /start",
                parse_mode="Markdown"
            )
        except:
            pass
    await c.message.edit_caption(caption=c.message.caption + "\n\n❌ ОТКЛОНЁН")
    await c.answer("Платёж отклонён!")

@dp.callback_query(F.data.startswith("admin_completed_page_"))
async def admin_completed(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    page = int(c.data.split("_")[-1])
    total = get_orders_count_by_status("completed")
    total_pages = (total + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE if total > 0 else 1
    
    orders = get_all_orders_by_status("completed", ORDERS_PER_PAGE, page * ORDERS_PER_PAGE)
    
    if not orders:
        await c.message.edit_text("✅ Нет подтверждённых заказов.", reply_markup=admin_pagination_kb("completed", page, total_pages))
        return
    
    text = f"✅ *Подтверждённые заказы* (страница {page+1} из {total_pages})\n\n"
    for o in orders:
        created = datetime.strptime(o[8], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        text += f"┌ #{o[0]} | {o[4]}₽ | @{o[2] or o[1]}\n"
        text += f"└ 📅 {created} | 📬 {o[5]}\n\n"
    
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_pagination_kb("completed", page, total_pages))

@dp.callback_query(F.data.startswith("admin_rejected_page_"))
async def admin_rejected(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    page = int(c.data.split("_")[-1])
    total = get_orders_count_by_status("rejected")
    total_pages = (total + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE if total > 0 else 1
    
    orders = get_all_orders_by_status("rejected", ORDERS_PER_PAGE, page * ORDERS_PER_PAGE)
    
    if not orders:
        await c.message.edit_text("❌ Нет отклонённых заказов.", reply_markup=admin_pagination_kb("rejected", page, total_pages))
        return
    
    text = f"❌ *Отклонённые заказы* (страница {page+1} из {total_pages})\n\n"
    for o in orders:
        created = datetime.strptime(o[8], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        text += f"┌ #{o[0]} | {o[4]}₽ | @{o[2] or o[1]}\n"
        text += f"└ 📅 {created} | 📬 {o[5]}\n\n"
    
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=admin_pagination_kb("rejected", page, total_pages))

@dp.callback_query(F.data == "ignore")
async def ignore_callback(c: types.CallbackQuery):
    await c.answer()

# ==================== ЗАПУСК ====================
async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.create_task(self_pinger())
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен с админ-панелью и постраничным выводом заказов")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
