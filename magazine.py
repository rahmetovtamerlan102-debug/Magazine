#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
import sqlite3
import os
import threading
import csv
import io
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

ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "5895176743,8276815852").split(',')))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "@HowScad_support")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "+79027365759")
RECIPIENT_NAME = os.getenv("RECIPIENT_NAME", "Егор")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@Howscard")
REQUIRED_CHANNEL_LINK = os.getenv("REQUIRED_CHANNEL_LINK", "https://t.me/Howscard")
PORT = int(os.getenv("PORT", "8080"))

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

STARS_PRICES = {
    "50": {"name": "⭐ 50 Stars", "stars": 50, "price": 70},
    "100": {"name": "⭐ 100 Stars", "stars": 100, "price": 140},
    "250": {"name": "⭐ 250 Stars", "stars": 250, "price": 350},
    "400": {"name": "⭐ 400 Stars", "stars": 400, "price": 550},
    "500": {"name": "⭐ 500 Stars", "stars": 500, "price": 670},
    "750": {"name": "⭐ 750 Stars", "stars": 750, "price": 1000},
    "1000": {"name": "⭐ 1000 Stars", "stars": 1000, "price": 1400}
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ==================== FLASK + PINGER ====================
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
            product_type TEXT DEFAULT 'account',
            product_name TEXT,
            price INTEGER,
            address TEXT,
            screenshot TEXT,
            status TEXT DEFAULT 'pending',
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS stars_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            stars INTEGER,
            price INTEGER,
            stars_username TEXT,
            screenshot TEXT,
            status TEXT DEFAULT 'pending',
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS blocked (user_id INTEGER PRIMARY KEY)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS admin_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            action TEXT,
            target TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
init_db()

def log_admin_action(admin_id, action, target=""):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT INTO admin_logs (admin_id, action, target) VALUES (?,?,?)", (admin_id, action, target))

# ==================== ФУНКЦИИ ДЛЯ АККАУНТОВ ====================
def save_order(uid, uname, product_name, price, addr):
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO orders (user_id, username, product_type, product_name, price, address) VALUES (?,?,?,?,?,?)",
                    (uid, uname, 'account', product_name, price, addr))
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

def get_all_orders_for_export():
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()

# ==================== ФУНКЦИИ ДЛЯ ЗВЁЗД ====================
def save_stars_order(uid, uname, stars, price, stars_username):
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO stars_orders (user_id, username, stars, price, stars_username) VALUES (?,?,?,?,?)",
                    (uid, uname, stars, price, stars_username))
        return cur.lastrowid

def update_stars_screenshot(oid, fid):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE stars_orders SET screenshot = ? WHERE id = ?", (fid, oid))

def update_stars_status(oid, status):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE stars_orders SET status = ? WHERE id = ?", (status, oid))

def get_stars_orders_by_status(status, limit=None, offset=None):
    with sqlite3.connect(DB_NAME) as conn:
        if limit is not None and offset is not None:
            return conn.execute("SELECT * FROM stars_orders WHERE status = ? ORDER BY id DESC LIMIT ? OFFSET ?", (status, limit, offset)).fetchall()
        return conn.execute("SELECT * FROM stars_orders WHERE status = ? ORDER BY id DESC", (status,)).fetchall()

def get_stars_orders_count_by_status(status):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT COUNT(*) FROM stars_orders WHERE status = ?", (status,)).fetchone()[0]

def get_stars_order(oid):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM stars_orders WHERE id = ?", (oid,)).fetchone()

def get_user_stars_orders(uid):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM stars_orders WHERE user_id = ? ORDER BY id DESC", (uid,)).fetchall()

def get_all_stars_orders_for_export():
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM stars_orders ORDER BY id DESC").fetchall()

# ==================== СТАТИСТИКА ====================
def get_stats():
    with sqlite3.connect(DB_NAME) as conn:
        # Аккаунты
        total_accounts = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        total_accounts_sum = conn.execute("SELECT SUM(price) FROM orders WHERE status='completed'").fetchone()[0] or 0
        pending_accounts = conn.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0]
        processing_accounts = conn.execute("SELECT COUNT(*) FROM orders WHERE status='processing'").fetchone()[0]
        completed_accounts = conn.execute("SELECT COUNT(*) FROM orders WHERE status='completed'").fetchone()[0]
        rejected_accounts = conn.execute("SELECT COUNT(*) FROM orders WHERE status='rejected'").fetchone()[0]
        # Звёзды
        total_stars = conn.execute("SELECT COUNT(*) FROM stars_orders").fetchone()[0]
        total_stars_sum = conn.execute("SELECT SUM(price) FROM stars_orders WHERE status='completed'").fetchone()[0] or 0
        pending_stars = conn.execute("SELECT COUNT(*) FROM stars_orders WHERE status='pending'").fetchone()[0]
        processing_stars = conn.execute("SELECT COUNT(*) FROM stars_orders WHERE status='processing'").fetchone()[0]
        completed_stars = conn.execute("SELECT COUNT(*) FROM stars_orders WHERE status='completed'").fetchone()[0]
        rejected_stars = conn.execute("SELECT COUNT(*) FROM stars_orders WHERE status='rejected'").fetchone()[0]
        # Блокировки
        blocked_count = conn.execute("SELECT COUNT(*) FROM blocked").fetchone()[0]
    return {
        "accounts": {
            "total": total_accounts,
            "sum": total_accounts_sum,
            "pending": pending_accounts,
            "processing": processing_accounts,
            "completed": completed_accounts,
            "rejected": rejected_accounts,
        },
        "stars": {
            "total": total_stars,
            "sum": total_stars_sum,
            "pending": pending_stars,
            "processing": processing_stars,
            "completed": completed_stars,
            "rejected": rejected_stars,
        },
        "blocked": blocked_count,
    }

# ==================== БЛОКИРОВКА ====================
def block_user(uid):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO blocked (user_id) VALUES (?)", (uid,))

def unblock_user(uid):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("DELETE FROM blocked WHERE user_id = ?", (uid,))

def is_blocked(uid):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT 1 FROM blocked WHERE user_id = ?", (uid,)).fetchone() is not None

def get_all_users():
    """Собирает уникальные user_id из заказов аккаунтов и звёзд"""
    with sqlite3.connect(DB_NAME) as conn:
        users = set()
        for row in conn.execute("SELECT DISTINCT user_id FROM orders"):
            users.add(row[0])
        for row in conn.execute("SELECT DISTINCT user_id FROM stars_orders"):
            users.add(row[0])
        return list(users)

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
    account_country = State()
    account_address = State()
    stars_amount = State()
    stars_username = State()
    screenshot = State()

class MailingForm(StatesGroup):
    waiting_for_message = State()

# ==================== КЛАВИАТУРЫ ====================
def main_kb(user_id):
    buttons = [
        [InlineKeyboardButton(text="📱 Купить аккаунт", callback_data="buy_account")],
        [InlineKeyboardButton(text="⭐ Купить звёзды", callback_data="buy_stars")],
        [InlineKeyboardButton(text="📦 Мои заказы", callback_data="my_orders")],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="support")]
    ]
    if user_id in ADMIN_IDS:
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

def stars_kb():
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for code, s in STARS_PRICES.items():
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"{s['name']} — {s['price']}₽", callback_data=f"stars_{code}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="back")])
    return kb

def admin_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟡 Аккаунты на проверке", callback_data="admin_accounts_processing_page_0")],
        [InlineKeyboardButton(text="⭐ Звёзды на проверке", callback_data="admin_stars_processing_page_0")],
        [InlineKeyboardButton(text="✅ Подтверждённые", callback_data="admin_completed_page_0")],
        [InlineKeyboardButton(text="❌ Отклонённые", callback_data="admin_rejected_page_0")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📁 Экспорт CSV", callback_data="admin_export_menu")],
        [InlineKeyboardButton(text="🔒 Блокировка", callback_data="admin_block_menu")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="admin_mailing")],
        [InlineKeyboardButton(text="📜 Логи", callback_data="admin_logs")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="back")]
    ])

def admin_export_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Экспорт аккаунтов (CSV)", callback_data="export_accounts")],
        [InlineKeyboardButton(text="⭐ Экспорт звёзд (CSV)", callback_data="export_stars")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")]
    ])

def admin_block_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔒 Заблокировать", callback_data="block_user")],
        [InlineKeyboardButton(text="🔓 Разблокировать", callback_data="unblock_user")],
        [InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")]
    ])

def admin_order_controls_kb(order_id, order_type):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_{order_type}_{order_id}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{order_type}_{order_id}")],
        [InlineKeyboardButton(text="◀ Назад", callback_data=f"admin_{order_type}_processing_page_0")]
    ])

def admin_accounts_pagination_kb(status, current_page, total_pages):
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀◀", callback_data=f"admin_accounts_{status}_page_{current_page-1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"{current_page+1}/{total_pages}", callback_data="ignore"))
    if current_page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="▶▶", callback_data=f"admin_accounts_{status}_page_{current_page+1}"))
    if nav_buttons:
        kb.inline_keyboard.append(nav_buttons)
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")])
    return kb

def admin_stars_pagination_kb(current_page, total_pages):
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀◀", callback_data=f"admin_stars_processing_page_{current_page-1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"{current_page+1}/{total_pages}", callback_data="ignore"))
    if current_page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="▶▶", callback_data=f"admin_stars_processing_page_{current_page+1}"))
    if nav_buttons:
        kb.inline_keyboard.append(nav_buttons)
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")])
    return kb

# ==================== ОСНОВНЫЕ ХЕНДЛЕРЫ ====================
@dp.message(Command("start"))
async def start(m: types.Message, state: FSMContext):
    await state.clear()
    if is_blocked(m.from_user.id):
        await m.answer("⛔ Вы заблокированы. Обратитесь к администратору.")
        return
    if not await is_subscribed(m.from_user.id):
        await m.answer("📢 Подпишитесь на канал, чтобы пользоваться магазином!", reply_markup=sub_kb())
        return
    await m.answer("🌟 Добро пожаловать в магазин!\n\n📱 Аккаунты Telegram\n⭐ Telegram Stars", reply_markup=main_kb(m.from_user.id))

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

@dp.callback_query(F.data == "my_orders")
async def my_orders(c: types.CallbackQuery):
    account_orders = get_orders(c.from_user.id)
    stars_orders = get_user_stars_orders(c.from_user.id)
    if not account_orders and not stars_orders:
        await c.answer("У вас пока нет заказов", show_alert=True)
        return
    text = "📦 *Мои заказы*\n\n"
    if account_orders:
        text += "📱 *Аккаунты:*\n"
        for o in account_orders:
            status_emoji = "✅" if o[8] == "completed" else "⏳" if o[8] == "processing" else "❌"
            status_text = "Оплачено" if o[8] == "completed" else "На проверке" if o[8] == "processing" else "Отклонён"
            created = datetime.strptime(o[9], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
            text += f"{status_emoji} *#{o[0]}* | {o[5]}₽ | {o[4]} | {status_text} | {created}\n"
    if stars_orders:
        text += "\n⭐ *Звёзды:*\n"
        for o in stars_orders:
            status_emoji = "✅" if o[7] == "completed" else "⏳" if o[7] == "processing" else "❌"
            status_text = "Оплачено" if o[7] == "completed" else "На проверке" if o[7] == "processing" else "Отклонён"
            created = datetime.strptime(o[8], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
            text += f"{status_emoji} *#{o[0]}* | {o[4]}₽ | {o[3]}⭐ | {status_text} | {created}\n"
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Назад", callback_data="back")]]))

# ==================== ПОКУПКА АККАУНТА ====================
@dp.callback_query(F.data == "buy_account")
async def buy_account(c: types.CallbackQuery, state: FSMContext):
    if not await is_subscribed(c.from_user.id):
        await c.answer("Подпишитесь на канал!", show_alert=True)
        return
    await state.set_state(OrderForm.account_country)
    await c.message.edit_text("🌍 *Выберите страну аккаунта:*", parse_mode="Markdown", reply_markup=country_kb())

@dp.callback_query(OrderForm.account_country, F.data.startswith("cnt_"))
async def select_country(c: types.CallbackQuery, state: FSMContext):
    code = c.data.split("_")[1]
    country = COUNTRY_PRICES[code]
    await state.update_data(product_name=country["name"], price=country["price"])
    await state.set_state(OrderForm.account_address)
    warning = f"\n\n{country['warning']}" if country.get("warning") else ""
    await c.message.edit_text(
        f"✅ *Страна:* {country['name']} — {country['price']}₽{warning}\n\n"
        f"💰 *Итого к оплате:* {country['price']}₽\n\n"
        f"📬 *Введите ваш Telegram username для доставки аккаунта:*\nПример: @durov",
        parse_mode="Markdown"
    )
    await c.answer()

@dp.message(OrderForm.account_address)
async def get_account_address(m: types.Message, state: FSMContext):
    addr = m.text.strip()
    if not addr.startswith("@"):
        addr = "@" + addr
    if len(addr) < 3:
        await m.answer("❌ Слишком короткий username. Пример: @durov")
        return
    data = await state.get_data()
    if not data.get("product_name"):
        await m.answer("❌ Ошибка. Начните заново /start")
        await state.clear()
        return
    order_id = save_order(m.from_user.id, m.from_user.username or "no_username", data["product_name"], data["price"], addr)
    await state.update_data(order_id=order_id, order_type="account", address=addr, product_name=data["product_name"], price=data["price"])
    await state.set_state(OrderForm.screenshot)
    await m.answer(
        f"💳 *ЗАКАЗ АККАУНТА #{order_id}*\n"
        f"💰 Сумма: {data['price']}₽\n\n"
        f"📌 *Реквизиты для оплаты:*\n"
        f"📱 СБП: `{PHONE_NUMBER}`\n"
        f"👤 Получатель: {RECIPIENT_NAME}\n\n"
        f"✅ *После оплаты отправьте СКРИНШОТ чека сюда.*",
        parse_mode="Markdown"
    )
    for admin_id in ADMIN_IDS:
        await bot.send_message(admin_id, f"📦 Новый заказ аккаунта #{order_id} от @{m.from_user.username or m.from_user.id} на {data['price']}₽")

# ==================== ПОКУПКА ЗВЁЗД ====================
@dp.callback_query(F.data == "buy_stars")
async def buy_stars(c: types.CallbackQuery, state: FSMContext):
    if not await is_subscribed(c.from_user.id):
        await c.answer("Подпишитесь на канал!", show_alert=True)
        return
    await state.set_state(OrderForm.stars_amount)
    await c.message.edit_text("⭐ *Выберите количество звёзд:*", parse_mode="Markdown", reply_markup=stars_kb())

@dp.callback_query(OrderForm.stars_amount, F.data.startswith("stars_"))
async def select_stars(c: types.CallbackQuery, state: FSMContext):
    code = c.data.split("_")[1]
    stars_data = STARS_PRICES[code]
    await state.update_data(stars=stars_data["stars"], price=stars_data["price"])
    await state.set_state(OrderForm.stars_username)
    await c.message.edit_text(
        f"✅ *Вы выбрали:* {stars_data['name']} — {stars_data['price']}₽\n\n"
        f"💰 *Итого к оплате:* {stars_data['price']}₽\n\n"
        f"📬 *Введите Telegram username, на который нужно начислить звёзды:*\nПример: @durov\n\n"
        f"⚠️ Внимание: звёзды будут отправлены именно на этот аккаунт!",
        parse_mode="Markdown"
    )
    await c.answer()

@dp.message(OrderForm.stars_username)
async def get_stars_username(m: types.Message, state: FSMContext):
    stars_username = m.text.strip()
    if not stars_username.startswith("@"):
        stars_username = "@" + stars_username
    if len(stars_username) < 3:
        await m.answer("❌ Слишком короткий username. Пример: @durov")
        return
    data = await state.get_data()
    if not data.get("stars"):
        await m.answer("❌ Ошибка. Начните заново /start")
        await state.clear()
        return
    order_id = save_stars_order(m.from_user.id, m.from_user.username or "no_username", data["stars"], data["price"], stars_username)
    await state.update_data(order_id=order_id, order_type="stars", stars_username=stars_username, stars=data["stars"], price=data["price"])
    await state.set_state(OrderForm.screenshot)
    await m.answer(
        f"💳 *ЗАКАЗ ЗВЁЗД #{order_id}*\n"
        f"⭐ {data['stars']} Stars\n"
        f"💰 Сумма: {data['price']}₽\n"
        f"📬 Начисление на: {stars_username}\n\n"
        f"📌 *Реквизиты для оплаты:*\n"
        f"📱 СБП: `{PHONE_NUMBER}`\n"
        f"👤 Получатель: {RECIPIENT_NAME}\n\n"
        f"✅ *После оплаты отправьте СКРИНШОТ чека сюда.*",
        parse_mode="Markdown"
    )
    for admin_id in ADMIN_IDS:
        await bot.send_message(admin_id, f"⭐ Новый заказ звёзд #{order_id} от @{m.from_user.username or m.from_user.id} на {data['stars']}⭐ ({data['price']}₽)")

# ==================== ОБРАБОТКА СКРИНШОТОВ ====================
@dp.message(OrderForm.screenshot, F.photo)
async def handle_screenshot(m: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    order_type = data.get("order_type")
    if not order_id or not order_type:
        await m.answer("❌ Ошибка. Начните заново /start")
        await state.clear()
        return
    file_id = m.photo[-1].file_id
    if order_type == "account":
        update_screenshot(order_id, file_id)
        update_status(order_id, "processing")
        product_info = f"аккаунта #{order_id}"
        product_details = f"{data.get('product_name')} — {data.get('price')}₽"
        address_info = f"Ваш username для доставки: {data.get('address')}"
    else:
        update_stars_screenshot(order_id, file_id)
        update_stars_status(order_id, "processing")
        product_info = f"звёзд #{order_id}"
        product_details = f"{data.get('stars')} Stars — {data.get('price')}₽"
        address_info = f"Начисление на: {data.get('stars_username')}"
    await m.answer(
        f"📸 *Скриншот для заказа {product_info} получен!*\n"
        f"**Ваш заказ принят, ждите подтверждения.**\n\n"
        f"Вы купили: {product_details}\n"
        f"{address_info}\n\n"
        f"Ожидайте подтверждения администратора (обычно до 2 часов).\n"
        f"Проверить статус можно в разделе «Мои заказы».",
        parse_mode="Markdown"
    )
    for admin_id in ADMIN_IDS:
        await bot.send_message(admin_id, f"🆕 Поступил новый чек для заказа {product_info}! Зайдите в админ-панель.")
    await state.clear()

@dp.message(OrderForm.screenshot)
async def wrong_screenshot(m: types.Message):
    await m.answer("❌ Отправьте именно ФОТО (скриншот чека). Текстовые сообщения не принимаются.")

# ==================== АДМИН-ПАНЕЛЬ ====================
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("Доступ запрещён", show_alert=True)
        return
    await c.message.edit_text("🔐 *Админ панель*\nВыберите категорию:", parse_mode="Markdown", reply_markup=admin_main_kb())

# -------------------- Статистика --------------------
@dp.callback_query(F.data == "admin_stats")
async def admin_stats(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    stats = get_stats()
    text = (
        "📊 *СТАТИСТИКА*\n\n"
        "📱 *Аккаунты:*\n"
        f"• Всего заказов: {stats['accounts']['total']}\n"
        f"• На проверке: {stats['accounts']['processing']}\n"
        f"• Ожидают оплаты: {stats['accounts']['pending']}\n"
        f"• Подтверждено: {stats['accounts']['completed']}\n"
        f"• Отклонено: {stats['accounts']['rejected']}\n"
        f"• Общая сумма (подтверждённых): {stats['accounts']['sum']}₽\n\n"
        "⭐ *Звёзды:*\n"
        f"• Всего заказов: {stats['stars']['total']}\n"
        f"• На проверке: {stats['stars']['processing']}\n"
        f"• Ожидают оплаты: {stats['stars']['pending']}\n"
        f"• Подтверждено: {stats['stars']['completed']}\n"
        f"• Отклонено: {stats['stars']['rejected']}\n"
        f"• Общая сумма (подтверждённых): {stats['stars']['sum']}₽\n\n"
        f"🔒 Заблокировано пользователей: {stats['blocked']}"
    )
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")]]))

# -------------------- Экспорт CSV --------------------
@dp.callback_query(F.data == "admin_export_menu")
async def admin_export_menu(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    await c.message.edit_text("📁 *Экспорт данных*", parse_mode="Markdown", reply_markup=admin_export_kb())

@dp.callback_query(F.data == "export_accounts")
async def export_accounts(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    orders = get_all_orders_for_export()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "User ID", "Username", "Товар", "Цена", "Адрес", "Статус", "Дата"])
    for o in orders:
        writer.writerow([o[0], o[1], o[2], o[4], o[5], o[6], o[8], o[9]])
    output.seek(0)
    file = io.BytesIO(output.getvalue().encode('utf-8-sig'))
    await c.message.answer_document(types.BufferedInputFile(file.getvalue(), filename="accounts_export.csv"), caption="📱 Экспорт заказов аккаунтов")
    log_admin_action(c.from_user.id, "export_accounts")

@dp.callback_query(F.data == "export_stars")
async def export_stars(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    orders = get_all_stars_orders_for_export()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "User ID", "Username", "Stars", "Цена", "Username получателя", "Статус", "Дата"])
    for o in orders:
        writer.writerow([o[0], o[1], o[2], o[3], o[4], o[5], o[7], o[8]])
    output.seek(0)
    file = io.BytesIO(output.getvalue().encode('utf-8-sig'))
    await c.message.answer_document(types.BufferedInputFile(file.getvalue(), filename="stars_export.csv"), caption="⭐ Экспорт заказов звёзд")
    log_admin_action(c.from_user.id, "export_stars")

# -------------------- Блокировка / разблокировка --------------------
@dp.callback_query(F.data == "admin_block_menu")
async def admin_block_menu(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    await c.message.edit_text("🔒 *Блокировка / разблокировка пользователя*\nВведите ID пользователя и действие.", parse_mode="Markdown", reply_markup=admin_block_kb())

@dp.callback_query(F.data == "block_user")
async def block_user_prompt(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    await state.set_state("waiting_block_id")
    await c.message.edit_text("Введите Telegram ID пользователя для блокировки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Отмена", callback_data="admin_panel")]]))

@dp.callback_query(F.data == "unblock_user")
async def unblock_user_prompt(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    await state.set_state("waiting_unblock_id")
    await c.message.edit_text("Введите Telegram ID пользователя для разблокировки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Отмена", callback_data="admin_panel")]]))

@dp.message(lambda m: m.state and m.state == "waiting_block_id")
async def process_block(m: types.Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return
    try:
        uid = int(m.text.strip())
        block_user(uid)
        await m.answer(f"✅ Пользователь {uid} заблокирован.")
        log_admin_action(m.from_user.id, "block_user", str(uid))
    except:
        await m.answer("❌ Неверный ID. Введите число.")
    await state.clear()
    await back_to_admin(m)

@dp.message(lambda m: m.state and m.state == "waiting_unblock_id")
async def process_unblock(m: types.Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return
    try:
        uid = int(m.text.strip())
        unblock_user(uid)
        await m.answer(f"✅ Пользователь {uid} разблокирован.")
        log_admin_action(m.from_user.id, "unblock_user", str(uid))
    except:
        await m.answer("❌ Неверный ID.")
    await state.clear()
    await back_to_admin(m)

async def back_to_admin(m: types.Message):
    await m.answer("🔐 Админ панель", reply_markup=admin_main_kb())

# -------------------- Рассылка --------------------
@dp.callback_query(F.data == "admin_mailing")
async def admin_mailing(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(MailingForm.waiting_for_message)
    await c.message.edit_text("📨 *Рассылка*\nОтправьте сообщение, которое будет доставлено всем пользователям.\n\nДля отмены нажмите /cancel", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Отмена", callback_data="admin_panel")]]))

@dp.message(MailingForm.waiting_for_message)
async def process_mailing(m: types.Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return
    text = m.text
    users = get_all_users()
    success = 0
    for uid in users:
        try:
            await bot.send_message(uid, text)
            success += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await m.answer(f"✅ Рассылка завершена. Отправлено {success} из {len(users)} пользователей.")
    log_admin_action(m.from_user.id, "mailing", f"sent to {success}")
    await state.clear()
    await back_to_admin(m)

# -------------------- Логи --------------------
@dp.callback_query(F.data == "admin_logs")
async def admin_logs(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    with sqlite3.connect(DB_NAME) as conn:
        logs = conn.execute("SELECT admin_id, action, target, timestamp FROM admin_logs ORDER BY id DESC LIMIT 30").fetchall()
    if not logs:
        text = "📜 *Логи действий*\nПока нет записей."
    else:
        text = "📜 *Последние 30 действий администраторов:*\n\n"
        for log in logs:
            text += f"👤 {log[0]} | {log[1]} {log[2]} | {log[3]}\n"
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")]]))

# -------------------- Просмотр заказов (аккаунты) --------------------
@dp.callback_query(F.data.startswith("admin_accounts_processing_page_"))
async def admin_accounts_processing(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    page = int(c.data.split("_")[-1])
    total = get_orders_count_by_status("processing")
    total_pages = (total + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE if total > 0 else 1
    orders = get_all_orders_by_status("processing", ORDERS_PER_PAGE, page * ORDERS_PER_PAGE)
    if not orders:
        await c.message.edit_text("📭 Нет заказов аккаунтов на проверке.", reply_markup=admin_accounts_pagination_kb("processing", page, total_pages))
        return
    text = f"🟡 *Аккаунты на проверке* (страница {page+1} из {total_pages})\n\n"
    for o in orders:
        text += f"┌ #{o[0]} | {o[5]}₽ | @{o[2] or o[1]} (id:{o[1]})\n"
        text += f"└ 📬 {o[6]}\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for o in orders:
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"📦 Заказ #{o[0]}", callback_data=f"view_account_{o[0]}")])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀◀ Назад", callback_data=f"admin_accounts_processing_page_{page-1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ▶▶", callback_data=f"admin_accounts_processing_page_{page+1}"))
    if nav_buttons:
        kb.inline_keyboard.append(nav_buttons)
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")])
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)

# -------------------- Звёзды на проверке --------------------
@dp.callback_query(F.data.startswith("admin_stars_processing_page_"))
async def admin_stars_processing(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    page = int(c.data.split("_")[-1])
    total = get_stars_orders_count_by_status("processing")
    total_pages = (total + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE if total > 0 else 1
    orders = get_stars_orders_by_status("processing", ORDERS_PER_PAGE, page * ORDERS_PER_PAGE)
    if not orders:
        await c.message.edit_text("📭 Нет заказов звёзд на проверке.", reply_markup=admin_stars_pagination_kb(page, total_pages))
        return
    text = f"⭐ *Звёзды на проверке* (страница {page+1} из {total_pages})\n\n"
    for o in orders:
        text += f"┌ #{o[0]} | {o[4]}₽ | {o[3]}⭐ | @{o[2] or o[1]} (id:{o[1]})\n"
        text += f"└ 📬 Начисление: @{o[5]}\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for o in orders:
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"⭐ Заказ #{o[0]}", callback_data=f"view_stars_{o[0]}")])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀◀ Назад", callback_data=f"admin_stars_processing_page_{page-1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ▶▶", callback_data=f"admin_stars_processing_page_{page+1}"))
    if nav_buttons:
        kb.inline_keyboard.append(nav_buttons)
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")])
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)

# -------------------- Подтверждённые / отклонённые --------------------
@dp.callback_query(F.data.startswith("admin_completed_page_"))
async def admin_completed(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    page = int(c.data.split("_")[-1])
    account_orders = get_all_orders_by_status("completed", ORDERS_PER_PAGE, page * ORDERS_PER_PAGE)
    stars_orders = get_stars_orders_by_status("completed", ORDERS_PER_PAGE, page * ORDERS_PER_PAGE)
    total_accounts = get_orders_count_by_status("completed")
    total_stars = get_stars_orders_count_by_status("completed")
    total = total_accounts + total_stars
    total_pages = (total + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE if total > 0 else 1
    text = f"✅ *Подтверждённые заказы* (страница {page+1} из {total_pages})\n\n"
    for o in account_orders:
        created = datetime.strptime(o[9], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        text += f"┌ [#{o[0]}] Аккаунт | {o[5]}₽ | {o[4]} | id:{o[1]}\n└ 📅 {created} | 📬 {o[6]}\n\n"
    for o in stars_orders:
        created = datetime.strptime(o[8], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        text += f"┌ [#{o[0]}] Звёзды | {o[4]}₽ | {o[3]}⭐ | id:{o[1]}\n└ 📅 {created} | 📬 {o[5]}\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀◀ Назад", callback_data=f"admin_completed_page_{page-1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ▶▶", callback_data=f"admin_completed_page_{page+1}"))
    if nav_buttons:
        kb.inline_keyboard.append(nav_buttons)
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")])
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data.startswith("admin_rejected_page_"))
async def admin_rejected(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    page = int(c.data.split("_")[-1])
    account_orders = get_all_orders_by_status("rejected", ORDERS_PER_PAGE, page * ORDERS_PER_PAGE)
    stars_orders = get_stars_orders_by_status("rejected", ORDERS_PER_PAGE, page * ORDERS_PER_PAGE)
    total_accounts = get_orders_count_by_status("rejected")
    total_stars = get_stars_orders_count_by_status("rejected")
    total = total_accounts + total_stars
    total_pages = (total + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE if total > 0 else 1
    text = f"❌ *Отклонённые заказы* (страница {page+1} из {total_pages})\n\n"
    for o in account_orders:
        created = datetime.strptime(o[9], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        text += f"┌ [#{o[0]}] Аккаунт | {o[5]}₽ | {o[4]} | id:{o[1]}\n└ 📅 {created} | 📬 {o[6]}\n\n"
    for o in stars_orders:
        created = datetime.strptime(o[8], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        text += f"┌ [#{o[0]}] Звёзды | {o[4]}₽ | {o[3]}⭐ | id:{o[1]}\n└ 📅 {created} | 📬 {o[5]}\n\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀◀ Назад", callback_data=f"admin_rejected_page_{page-1}"))
    if page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ▶▶", callback_data=f"admin_rejected_page_{page+1}"))
    if nav_buttons:
        kb.inline_keyboard.append(nav_buttons)
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀ Назад", callback_data="admin_panel")])
    await c.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)

# -------------------- Просмотр конкретного заказа --------------------
@dp.callback_query(F.data.startswith("view_account_"))
async def view_account_order(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    order_id = int(c.data.split("_")[-1])
    order = get_order(order_id)
    if not order:
        await c.answer("Заказ не найден", show_alert=True)
        return
    order_id, user_id, username, product_type, product_name, price, address, screenshot, status, created = order
    caption = (
        f"🆕 *Заказ аккаунта #{order_id}*\n"
        f"👤 Пользователь: @{username or user_id} (id: `{user_id}`)\n"
        f"📦 Товар: {product_name}\n"
        f"💰 Сумма: {price}₽\n"
        f"📬 Доставка: {address}\n"
        f"🕒 Создан: {created}\n"
        f"📊 Статус: {'На проверке' if status == 'processing' else status}\n\n"
        f"📸 Чек:"
    )
    if screenshot:
        await c.message.answer_photo(photo=screenshot, caption=caption, parse_mode="Markdown", reply_markup=admin_order_controls_kb(order_id, "account"))
    else:
        await c.message.answer(caption, reply_markup=admin_order_controls_kb(order_id, "account"))

@dp.callback_query(F.data.startswith("view_stars_"))
async def view_stars_order(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return
    order_id = int(c.data.split("_")[-1])
    order = get_stars_order(order_id)
    if not order:
        await c.answer("Заказ не найден", show_alert=True)
        return
    order_id, user_id, username, stars, price, stars_username, screenshot, status, created = order
    caption = (
        f"⭐ *Заказ звёзд #{order_id}*\n"
        f"👤 Пользователь: @{username or user_id} (id: `{user_id}`)\n"
        f"⭐ Количество: {stars} Stars\n"
        f"💰 Сумма: {price}₽\n"
        f"📬 Начисление на: {stars_username}\n"
        f"🕒 Создан: {created}\n"
        f"📊 Статус: {'На проверке' if status == 'processing' else status}\n\n"
        f"📸 Чек:"
    )
    if screenshot:
        await c.message.answer_photo(photo=screenshot, caption=caption, parse_mode="Markdown", reply_markup=admin_order_controls_kb(order_id, "stars"))
    else:
        await c.message.answer(caption, reply_markup=admin_order_controls_kb(order_id, "stars"))

# -------------------- Подтверждение / отклонение --------------------
@dp.callback_query(F.data.startswith("confirm_account_"))
async def confirm_account_order(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("Только для администратора", show_alert=True)
        return
    order_id = int(c.data.split("_")[-1])
    update_status(order_id, "completed")
    order = get_order(order_id)
    if order:
        user_id = order[1]
        product_name = order[4]
        try:
            await bot.send_message(user_id, f"✅ *Заказ аккаунта #{order_id} подтверждён!*\n\n📦 {product_name}\n💰 {order[5]}₽\n\nАккаунт будет отправлен в течение 2‑4 часов.\nПо вопросам: {SUPPORT_USERNAME}", parse_mode="Markdown")
        except:
            pass
    await c.message.edit_caption(caption=c.message.caption + "\n\n✅ ОПЛАЧЕНО И ПОДТВЕРЖДЕНО")
    log_admin_action(c.from_user.id, "confirm_account", str(order_id))
    await c.answer("Оплата подтверждена!")

@dp.callback_query(F.data.startswith("confirm_stars_"))
async def confirm_stars_order(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("Только для администратора", show_alert=True)
        return
    order_id = int(c.data.split("_")[-1])
    update_stars_status(order_id, "completed")
    order = get_stars_order(order_id)
    if order:
        user_id = order[1]
        stars = order[3]
        stars_username = order[5]
        try:
            await bot.send_message(user_id, f"✅ *Заказ звёзд #{order_id} подтверждён!*\n\n⭐ {stars} Stars\n💰 {order[4]}₽\n📬 Начисление на: {stars_username}\n\nЗвёзды будут начислены в течение 2‑4 часов.\nПо вопросам: {SUPPORT_USERNAME}", parse_mode="Markdown")
        except:
            pass
    await c.message.edit_caption(caption=c.message.caption + "\n\n✅ ОПЛАЧЕНО И ПОДТВЕРЖДЕНО")
    log_admin_action(c.from_user.id, "confirm_stars", str(order_id))
    await c.answer("Оплата подтверждена!")

@dp.callback_query(F.data.startswith("reject_account_"))
async def reject_account_order(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("Только для администратора", show_alert=True)
        return
    order_id = int(c.data.split("_")[-1])
    update_status(order_id, "rejected")
    order = get_order(order_id)
    if order:
        user_id = order[1]
        try:
            await bot.send_message(user_id, f"❌ *Заказ аккаунта #{order_id} отклонён*\n\nПричина: чек не соответствует оплате или нечитаем.\nПожалуйста, отправьте корректный скриншот чека, начав заказ заново с /start", parse_mode="Markdown")
        except:
            pass
    await c.message.edit_caption(caption=c.message.caption + "\n\n❌ ОТКЛОНЁН")
    log_admin_action(c.from_user.id, "reject_account", str(order_id))
    await c.answer("Платёж отклонён!")

@dp.callback_query(F.data.startswith("reject_stars_"))
async def reject_stars_order(c: types.CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("Только для администратора", show_alert=True)
        return
    order_id = int(c.data.split("_")[-1])
    update_stars_status(order_id, "rejected")
    order = get_stars_order(order_id)
    if order:
        user_id = order[1]
        try:
            await bot.send_message(user_id, f"❌ *Заказ звёзд #{order_id} отклонён*\n\nПричина: чек не соответствует оплате или нечитаем.\nПожалуйста, отправьте корректный скриншот чека, начав заказ заново с /start", parse_mode="Markdown")
        except:
            pass
    await c.message.edit_caption(caption=c.message.caption + "\n\n❌ ОТКЛОНЁН")
    log_admin_action(c.from_user.id, "reject_stars", str(order_id))
    await c.answer("Платёж отклонён!")

@dp.callback_query(F.data == "ignore")
async def ignore_callback(c: types.CallbackQuery):
    await c.answer()

# ==================== ЗАПУСК ====================
async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.create_task(self_pinger())
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен с расширенной админ-панелью")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
