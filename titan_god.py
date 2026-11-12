import os
import sys
import json
import re
import asyncio
import random
import datetime
import threading
import shutil
from colorama import init, Fore, Style
from flask import Flask, jsonify
import aiosqlite

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError, SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, AuthRestartError
)
from telethon.errors.rpcerrorlist import AccessTokenInvalidError, ChatWriteForbiddenError

init(autoreset=True)

# =====================================================================
# 1. КОНСТАНТЫ
# =====================================================================
API_ID = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'
BOT_TOKEN = '8732669492:AAF6lfPEgcOp9iKzo74UGnPIPw9wfVa7YGo'

SUPER_ADMINS = {8655682794, 8773171051}
SUPPORT_CONTACT = "@apemiru"

DB_FILE = 'titan_database.db'
BACKUP_DIR = 'database_backups'

MIN_DELAY_LIMIT = 3
MAX_DELAY_LIMIT = 60

active_clients = {}
user_steps = {}
spam_engines = {}
qr_tasks = {}

system_metrics = {
    "total_messages_sent": 0,
    "total_flood_wait_events": 0,
    "total_errors_caught": 0,
}

# =====================================================================
# 2. ЛОГИ
# =====================================================================
class TitanLogger:
    @staticmethod
    def log(tag, msg, color=Fore.WHITE):
        t = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{Fore.LIGHTBLACK_EX}[{t}]{Fore.RESET} {color}[{tag}]{Fore.RESET} {msg}")

    @staticmethod
    def sys(msg): TitanLogger.log("SYS", msg, Fore.CYAN)
    @staticmethod
    def info(msg): TitanLogger.log("INFO", msg, Fore.GREEN)
    @staticmethod
    def warning(msg): TitanLogger.log("WARNING", msg, Fore.YELLOW)
    @staticmethod
    def error(msg): TitanLogger.log("ERROR", msg, Fore.RED)
    @staticmethod
    def critical(msg): print(f"{Fore.RED}{Style.BRIGHT}[CRITICAL] {msg}")

# =====================================================================
# 3. БД
# =====================================================================
class TitanDB:
    @staticmethod
    async def bootstrap():
        if not os.path.exists(BACKUP_DIR):
            os.makedirs(BACKUP_DIR)
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    session_string TEXT,
                    templates TEXT DEFAULT '[]',
                    min_delay INTEGER DEFAULT 5000,
                    max_delay INTEGER DEFAULT 15000,
                    is_approved INTEGER DEFAULT 0,
                    is_admin INTEGER DEFAULT 0,
                    plan TEXT DEFAULT 'FREE',
                    registered_at TEXT
                )
            ''')
            try:
                await db.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'FREE'")
            except:
                pass
            await db.execute('''
                CREATE TABLE IF NOT EXISTS spam_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    target TEXT,
                    sent_messages_count INTEGER DEFAULT 0,
                    status TEXT,
                    started_at TEXT,
                    ended_at TEXT
                )
            ''')
            await db.commit()
        TitanLogger.info("БД готова.")

    @staticmethod
    async def backup():
        try:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(BACKUP_DIR, f"backup_{ts}.db")
            if os.path.exists(DB_FILE):
                shutil.copyfile(DB_FILE, path)
                TitanLogger.info(f"Бэкап: {path}")
        except Exception as e:
            TitanLogger.error(f"Бэкап: {e}")

# =====================================================================
# 4. ЮЗЕРЫ
# =====================================================================
async def get_user(uid):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT session_string, templates, min_delay, max_delay, is_approved, is_admin, plan FROM users WHERE user_id = ?",
            (uid,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    'session': row[0],
                    'templates': json.loads(row[1]),
                    'min_delay': row[2],
                    'max_delay': row[3],
                    'is_approved': row[4],
                    'is_admin': row[5],
                    'plan': row[6] or 'FREE',
                }
    return None

async def save_user(uid, session=None, templates=None, min_d=None, max_d=None, approved=None, admin=None, plan=None):
    cur = await get_user(uid)
    s = session if session is not None else (cur['session'] if cur else None)
    t = templates if templates is not None else (cur['templates'] if cur else [])
    mind = min_d if min_d is not None else (cur['min_delay'] if cur else 5000)
    maxd = max_d if max_d is not None else (cur['max_delay'] if cur else 15000)
    a = approved if approved is not None else (cur['is_approved'] if cur else 0)
    adm = admin if admin is not None else (cur['is_admin'] if cur else 0)
    pl = plan if plan is not None else (cur['plan'] if cur else 'FREE')

    if uid in SUPER_ADMINS:
        a = 1
        adm = 1

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''
            INSERT INTO users (user_id, session_string, templates, min_delay, max_delay, is_approved, is_admin, plan, registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
            session_string=excluded.session_string,
            templates=excluded.templates,
            min_delay=excluded.min_delay,
            max_delay=excluded.max_delay,
            is_approved=excluded.is_approved,
            is_admin=excluded.is_admin,
            plan=excluded.plan
        ''', (uid, s, json.dumps(t), mind, maxd, a, adm, pl, str(datetime.datetime.now())))
        await db.commit()

def is_super(uid):
    return int(uid) in SUPER_ADMINS

async def resolve_client(uid):
    if uid in active_clients:
        return active_clients[uid]
    p = await get_user(uid)
    if not p or not p['session']:
        return None
    try:
        c = TelegramClient(StringSession(p['session']), API_ID, API_HASH)
        await c.connect()
        if await c.is_user_authorized():
            active_clients[uid] = c
            return c
    except Exception as e:
        TitanLogger.error(f"Сессия {uid}: {e}")
    return None

# =====================================================================
# 5. СПАМ-ДВИЖОК
# =====================================================================
class SpamEngine:
    def __init__(self, client, user_id):
        self.client = client
        self.user_id = user_id
        self.is_active = False
        self.counter = 0

    async def attack(self, target, templates, min_delay, max_delay):
        self.is_active = True
        self.counter = 0
        start_time = str(datetime.datetime.now())

        if "t.me/" in target:
            target = "@" + target.split("t.me/")[-1].split("/")[0].split("?")[0]

        current_min = max(MIN_DELAY_LIMIT * 1000, min(min_delay, MAX_DELAY_LIMIT * 1000))
        current_max = max(MIN_DELAY_LIMIT * 1000, min(max_delay, MAX_DELAY_LIMIT * 1000))
        if current_max < current_min:
            current_max = current_min

        TitanLogger.info(f"Старт [{self.user_id}] -> {target} | {current_min/1000}-{current_max/1000} сек")

        while self.is_active:
            try:
                if not templates:
                    break
                msg = random.choice(templates)
                await self.client.send_message(target, msg)
                self.counter += 1
                system_metrics["total_messages_sent"] += 1
                TitanLogger.log("SPAM", f"[{self.user_id}] #{self.counter} -> {target}", Fore.BLUE)

                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute('''
                        INSERT INTO spam_history (user_id, target, sent_messages_count, status, started_at, ended_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (self.user_id, str(target), self.counter, 'RUNNING', start_time, str(datetime.datetime.now())))
                    await db.commit()

                delay = random.randint(current_min, current_max) / 1000.0
                await asyncio.sleep(delay)

            except FloodWaitError as e:
                system_metrics["total_flood_wait_events"] += 1
                TitanLogger.warning(f"FloodWait {e.seconds} сек.")
                await asyncio.sleep(e.seconds + 5)
            except ChatWriteForbiddenError:
                system_metrics["total_errors_caught"] += 1
                TitanLogger.error(f"Нет прав писать в {target}.")
                self.is_active = False
                break
            except Exception as e:
                system_metrics["total_errors_caught"] += 1
                TitanLogger.error(f"Сбой [{self.user_id}]: {e}")
                self.is_active = False
                break

        self.is_active = False
        TitanLogger.info(f"Стоп [{self.user_id}]. Отправлено: {self.counter}")

# =====================================================================
# 6. БОТ
# =====================================================================
bot = TelegramClient('bot_master', API_ID, API_HASH)

def main_menu(is_admin):
    menu = [
        [Button.inline("👤 Аккаунт", b"nav_account"), Button.inline("📝 Шаблоны", b"nav_templates")],
        [Button.inline("⚙️ Задержка", b"nav_delay"), Button.inline("🚀 Запуск", b"nav_spam")],
        [Button.inline("🛑 Остановить", b"nav_stop")],
        [Button.inline("💎 Подписка", b"nav_sub"), Button.inline("ℹ️ Помощь", b"nav_help")],
    ]
    if is_admin:
        menu.append([Button.inline("👑 Админ-панель", b"nav_admin")])
    return menu

def main_text(uid, p):
    status = "✅ Подключён" if p and p['session'] else "❌ Не подключён"
    tpl = len(p['templates']) if p else 0
    mind = int(p['min_delay'] / 1000) if p else 5
    maxd = int(p['max_delay'] / 1000) if p else 15
    active = "🟢 Активен" if uid in spam_engines else "⚪ Не запущен"
    crown = "👑 " if is_super(uid) else ""
    plan = p.get('plan', 'FREE') if p else 'FREE'
    plan_emoji = {'FREE': '⚪', 'PRO': '💎', 'ULTIMATE': '👑'}.get(plan, '⚪')

    return (
        f"<b>⚡ TITAN Enterprise</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>ID:</b> <code>{uid}</code> {crown}\n"
        f"📱 <b>Аккаунт:</b> {status}\n"
        f"📝 <b>Шаблонов:</b> {tpl}\n"
        f"⚙️ <b>Задержка:</b> {mind}–{maxd} сек\n"
        f"🚀 <b>Статус:</b> {active}\n"
        f"💎 <b>Тариф:</b> {plan_emoji} <b>{plan}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Выберите действие:"
    )

@bot.on(events.NewMessage(pattern='/start'))
async def cmd_start(e):
    uid = e.sender_id
    p = await get_user(uid)

    if is_super(uid):
        if not p or p['is_admin'] != 1 or p['is_approved'] != 1:
            await save_user(uid, admin=1, approved=1)
            TitanLogger.sys(f"Супер-админ авторизован: {uid}")
            p = await get_user(uid)

    is_admin = p and p['is_admin'] == 1
    is_approved = p and p['is_approved'] == 1

    if not is_admin and not is_approved:
        await e.reply(
            f"⛔ <b>Доступ закрыт</b>\n\n"
            f"Ваш ID: <code>{uid}</code>\n"
            f"Передайте его администратору.\n\n"
            f"По всем вопросам: {SUPPORT_CONTACT}",
            parse_mode='html'
        )
        return

    user_steps[uid] = "IDLE"
    await e.reply(main_text(uid, p), buttons=main_menu(is_admin), parse_mode='html')

# ---------- АВТОРИЗАЦИЯ: SMS / ЗВОНОК / QR ----------
@bot.on(events.CallbackQuery(data="nav_account"))
async def cb_account(e):
    uid = e.sender_id
    p = await get_user(uid) or {}
    if not p.get('session'):
        user_steps[uid] = "ST_PHONE"
        await e.edit(
            "📱 <b>Авторизация аккаунта</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Отправьте номер телефона в формате <code>+79991234567</code>\n\n"
            "💡 <b>Код не приходит?</b> После ввода номера выберите способ доставки:\n"
            "• 📩 SMS\n"
            "• 📞 Звонок\n"
            "• 📷 QR-код (самый надёжный)\n\n"
            f"По вопросам: {SUPPORT_CONTACT}",
            buttons=[[Button.inline("🔙 Назад", b"nav_home")]],
            parse_mode='html'
        )
    else:
        await e.edit(
            f"✅ <b>Аккаунт подключён</b>\n\n"
            f"Сессия активна и готова к работе.\n\n"
            f"По вопросам: {SUPPORT_CONTACT}",
            buttons=[[Button.inline("🔙 Назад", b"nav_home")]],
            parse_mode='html'
        )

@bot.on(events.CallbackQuery(data="qr_start"))
async def cb_qr_start(e):
    uid = e.sender_id
    await e.edit("🔄 Генерирую QR-код...")
    try:
        c = TelegramClient(StringSession(), API_ID, API_HASH)
        await c.connect()
        qr = await c.qr_login()
        active_clients[f"qr_{uid}"] = {'client': c, 'qr': qr}
        user_steps[uid] = "ST_QR"

        # Генерируем картинку QR через Google Chart API (быстрый способ без зависимостей)
        import urllib.parse
        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={urllib.parse.quote(qr.url)}"

        await e.edit(
            "📷 <b>Вход по QR-коду</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ Открой Telegram на телефоне\n"
            "2️⃣ Настройки → Устройства → Подключить устройство\n"
            "3️⃣ Отсканируй QR ниже\n\n"
            f"🔗 <a href='{qr_url}'>Открыть QR-код</a>\n\n"
            "⏳ Жду сканирования (60 сек)...",
            buttons=[[Button.inline("🔙 Отмена", b"qr_cancel")]],
            parse_mode='html'
        )

        # Ждём авторизации
        try:
            await asyncio.wait_for(qr.wait(), timeout=120)
            await save_user(uid, session=c.session.save())
            active_clients[uid] = c
            if f"qr_{uid}" in active_clients:
                del active_clients[f"qr_{uid}"]
            user_steps[uid] = "IDLE"
            await e.edit(
                "✅ <b>Успешно авторизован по QR!</b>\n\n"
                "Возвращайтесь в меню: /start",
                parse_mode='html'
            )
        except asyncio.TimeoutError:
            await e.edit("⏱ Время QR истекло. Попробуйте заново: /start")
            user_steps[uid] = "IDLE"
    except Exception as ex:
        await e.edit(
            f"❌ Ошибка QR: <code>{str(ex)[:200]}</code>\n\n"
            f"По вопросам: {SUPPORT_CONTACT}",
            parse_mode='html'
        )
        user_steps[uid] = "IDLE"

@bot.on(events.CallbackQuery(data="qr_cancel"))
async def cb_qr_cancel(e):
    uid = e.sender_id
    if f"qr_{uid}" in active_clients:
        try:
            await active_clients[f"qr_{uid}"]['client'].disconnect()
        except:
            pass
        del active_clients[f"qr_{uid}"]
    user_steps[uid] = "IDLE"
    await e.edit("❌ QR-вход отменён.", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])

@bot.on(events.CallbackQuery(data="resend_call"))
async def cb_resend_call(e):
    uid = e.sender_id
    b = active_clients.get(f"temp_{uid}")
    if not b:
        return await e.answer("❌ Нет активной попытки входа", alert=True)
    try:
        await b['client'].send_code_request(b['phone'], force_sms=False)
        # force_sms=False → Telegram может выбрать звонок вместо SMS
        await e.answer("📞 Повторный запрос отправлен (возможно, звонком)", alert=True)
    except Exception as ex:
        await e.answer(f"❌ {str(ex)[:100]}", alert=True)

# ---------- ГЛАВНЫЕ РОУТЫ ----------
@bot.on(events.CallbackQuery)
async def cb_handler(e):
    uid = e.sender_id
    action = e.data.decode('utf-8')
    p = await get_user(uid) or {}
    is_admin = p.get('is_admin') == 1 or is_super(uid)
    is_approved = p.get('is_approved') == 1 or is_super(uid)

    if not is_admin and not is_approved:
        return await e.answer("⛔ Доступ закрыт", alert=True)

    back = [[Button.inline("🔙 Назад", b"nav_home")]]

    if action == "nav_home":
        user_steps[uid] = "IDLE"
        p = await get_user(uid) or {}
        is_admin = p.get('is_admin') == 1 or is_super(uid)
        await e.edit(main_text(uid, p), buttons=main_menu(is_admin), parse_mode='html')

    elif action == "nav_templates":
        tpls = p.get('templates', [])
        preview = "\n".join([f"• {t[:50]}" for t in tpls[:10]])
        if len(tpls) > 10:
            preview += f"\n<i>...и ещё {len(tpls)-10}</i>"
        if not preview:
            preview = "<i>Пусто</i>"
        buttons = [
            [Button.inline("🗑 Очистить все", b"nav_tpl_clear")],
            [Button.inline("🔙 Назад", b"nav_home")],
        ]
        await e.edit(
            f"📝 <b>Шаблоны ({len(tpls)} шт.)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{preview}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Отправьте текст — каждая строка станет отдельным шаблоном.",
            buttons=buttons, parse_mode='html'
        )

    elif action == "nav_tpl_clear":
        await save_user(uid, templates=[])
        await e.answer("✅ Все шаблоны удалены")
        await e.edit(
            "📝 <b>Шаблоны (0 шт.)</b>\n\n"
            "<i>Список очищен.</i>",
            buttons=[[Button.inline("🔙 Назад", b"nav_home")]], parse_mode='html'
        )

    elif action == "nav_delay":
        user_steps[uid] = "ST_DELAY"
        mind = int(p.get('min_delay', 5000) / 1000)
        maxd = int(p.get('max_delay', 15000) / 1000)
        await e.edit(
            f"⚙️ <b>Задержка</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Текущая: <b>{mind}–{maxd} сек</b>\n\n"
            f"Диапазон: <b>{MIN_DELAY_LIMIT}–{MAX_DELAY_LIMIT}</b> сек\n"
            f"Формат: <code>5-15</code>",
            buttons=back, parse_mode='html'
        )

    elif action == "nav_spam":
        if not p.get('session'):
            return await e.answer("❌ Нет сессии", alert=True)
        if not p.get('templates'):
            return await e.answer("❌ Нет шаблонов", alert=True)
        user_steps[uid] = "ST_TARGET"
        await e.edit(
            "🚀 <b>Запуск</b>\n\n"
            "Цель: <code>@username</code> / <code>https://t.me/username</code> / <code>123456789</code>",
            buttons=back, parse_mode='html'
        )

    elif action == "nav_stop":
        if uid in spam_engines:
            spam_engines[uid].is_active = False
            del spam_engines[uid]
        await e.edit("🛑 <b>Остановлено</b>", buttons=back, parse_mode='html')

    elif action == "nav_sub":
        plan = p.get('plan', 'FREE')
        await e.edit(
            f"💎 <b>Тарифные планы</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚪ <b>FREE</b> — базовый\n"
            f"• 1 аккаунт, до 10 шаблонов\n"
            f"• Задержка 15–60 сек\n\n"
            f"💎 <b>PRO</b> — расширенный\n"
            f"• 1 аккаунт, до 100 шаблонов\n"
            f"• Задержка 5–30 сек\n"
            f"• Приоритетная поддержка\n\n"
            f"👑 <b>ULTIMATE</b> — максимум\n"
            f"• Все функции, без лимитов шаблонов\n"
            f"• Задержка 3–15 сек\n"
            f"• Персональный менеджер\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Ваш тариф: <b>{plan}</b>\n\n"
            f"📩 Активация: {SUPPORT_CONTACT}",
            buttons=back, parse_mode='html'
        )

    elif action == "nav_help":
        await e.edit(
            f"ℹ️ <b>Помощь</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>Не приходит код?</b>\n"
            f"• Введите номер → выберите QR-код\n"
            f"• Или запросите звонок вместо SMS\n"
            f"• Проверьте, что Telegram активен на номере\n"
            f"• Обратитесь в @smsteam_bot\n\n"
            f"<b>Задержка:</b> {MIN_DELAY_LIMIT}–{MAX_DELAY_LIMIT} сек\n\n"
            f"<b>Цель:</b> @username / ссылка / ID\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📩 <b>По всем вопросам:</b> {SUPPORT_CONTACT}",
            buttons=back, parse_mode='html'
        )

    elif action == "nav_admin" and is_admin:
        await e.edit(
            "👑 <b>Админ-панель</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<code>.approve &lt;ID&gt;</code> — выдать доступ\n"
            "<code>.revoke &lt;ID&gt;</code> — забрать доступ\n"
            "<code>.plan &lt;ID&gt; &lt;FREE|PRO|ULTIMATE&gt;</code> — тариф\n"
            "<code>.id</code> — свой ID",
            buttons=back, parse_mode='html'
        )

# ---------- ТЕКСТОВЫЙ ОБРАБОТЧИК ----------
@bot.on(events.NewMessage)
async def text_handler(e):
    uid = e.sender_id
    text = e.text.strip()

    if text.startswith('/'):
        return

    p = await get_user(uid) or {}
    is_admin = p.get('is_admin') == 1 or is_super(uid)
    is_approved = p.get('is_approved') == 1 or is_super(uid)

    if is_admin:
        if text.startswith('.approve'):
            try:
                t_id = int(text.split(' ')[1])
                await save_user(t_id, approved=1)
                await e.reply(f"✅ Доступ выдан: <code>{t_id}</code>", parse_mode='html')
            except:
                await e.reply("❌ <code>.approve 1234567</code>", parse_mode='html')
            return
        if text.startswith('.revoke'):
            try:
                t_id = int(text.split(' ')[1])
                if is_super(t_id):
                    await e.reply("⚠️ Нельзя забрать доступ у супер-админа.", parse_mode='html')
                    return
                await save_user(t_id, approved=0)
                await e.reply(f"🚫 Доступ забран: <code>{t_id}</code>", parse_mode='html')
            except:
                await e.reply("❌ <code>.revoke 1234567</code>", parse_mode='html')
            return
        if text.startswith('.plan'):
            try:
                parts = text.split(' ')
                t_id = int(parts[1])
                new_plan = parts[2].upper()
                if new_plan not in ('FREE', 'PRO', 'ULTIMATE'):
                    await e.reply("❌ Тарифы: FREE / PRO / ULTIMATE", parse_mode='html')
                    return
                await save_user(t_id, plan=new_plan)
                await e.reply(f"✅ Тариф <code>{new_plan}</code> выдан <code>{t_id}</code>", parse_mode='html')
            except:
                await e.reply("❌ <code>.plan 1234567 PRO</code>", parse_mode='html')
            return
        if text == '.id':
            await e.reply(f"Ваш ID: <code>{uid}</code>", parse_mode='html')
            return

    if not is_admin and not is_approved:
        return

    step = user_steps.get(uid, "IDLE")

    if step == "ST_PHONE":
        await e.reply("⏳ Запрашиваю код...")
        c = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await c.connect()
            req = await c.send_code_request(text)
            active_clients[f"temp_{uid}"] = {'client': c, 'phone': text, 'hash': req.phone_code_hash}
            user_steps[uid] = "ST_CODE"
            await e.reply(
                "📩 <b>Код отправлен</b>\n\n"
                "Введите код из Telegram.\n\n"
                "💡 <b>Если код не приходит:</b>",
                buttons=[
                    [Button.inline("📷 Войти по QR-коду", b"qr_start")],
                    [Button.inline("📞 Запросить звонок", b"resend_call")],
                    [Button.inline("🔙 Отмена", b"nav_home")],
                ],
                parse_mode='html'
            )
        except Exception as ex:
            await e.reply(
                f"❌ Ошибка: <code>{str(ex)[:200]}</code>\n\n"
                f"Попробуйте QR-код:",
                buttons=[
                    [Button.inline("📷 Войти по QR-коду", b"qr_start")],
                    [Button.inline("🔙 Назад", b"nav_home")],
                ],
                parse_mode='html'
            )
            user_steps[uid] = "IDLE"

    elif step == "ST_CODE":
        b = active_clients.get(f"temp_{uid}")
        if not b:
            return
        try:
            await b['client'].sign_in(phone=b['phone'], code=text, phone_code_hash=b['hash'])
            await save_user(uid, session=b['client'].session.save())
            active_clients[uid] = b['client']
            del active_clients[f"temp_{uid}"]
            user_steps[uid] = "IDLE"
            await e.reply("✅ Сессия создана! /start")
        except SessionPasswordNeededError:
            user_steps[uid] = "ST_2FA"
            await e.reply("🔐 Введите пароль 2FA:")
        except PhoneCodeInvalidError:
            await e.reply("❌ Неверный код. Попробуйте снова или запросите новый:")
        except PhoneCodeExpiredError:
            await e.reply("❌ Код истёк. Запросите новый:")
            user_steps[uid] = "IDLE"
        except Exception as ex:
            await e.reply(
                f"❌ Ошибка: <code>{str(ex)[:200]}</code>\n\n"
                f"По вопросам: {SUPPORT_CONTACT}",
                parse_mode='html'
            )
            user_steps[uid] = "IDLE"

    elif step == "ST_2FA":
        b = active_clients.get(f"temp_{uid}")
        if not b:
            return
        try:
            await b['client'].sign_in(password=text)
            await save_user(uid, session=b['client'].session.save())
            active_clients[uid] = b['client']
            del active_clients[f"temp_{uid}"]
            user_steps[uid] = "IDLE"
            await e.reply("✅ 2FA пройдена! /start")
        except Exception as ex:
            await e.reply(f"❌ Ошибка: <code>{str(ex)[:200]}</code>", parse_mode='html')

    elif step == "ST_TARGET":
        user_steps[uid] = "IDLE"
        c = await resolve_client(uid)
        if not c:
            await e.reply("❌ Сессия потеряна.")
            return
        eng = SpamEngine(c, uid)
        spam_engines[uid] = eng
        asyncio.create_task(eng.attack(text, p['templates'], p['min_delay'], p['max_delay']))
        await e.reply(
            f"🚀 <b>Запущено</b>\n"
            f"Цель: <code>{text}</code>\n"
            f"Задержка: {int(p['min_delay']/1000)}–{int(p['max_delay']/1000)} сек",
            parse_mode='html'
        )

    elif step == "ST_DELAY":
        try:
            low, high = map(int, text.split('-'))
            if low < MIN_DELAY_LIMIT:
                await e.reply(f"⚠️ Минимум — <b>{MIN_DELAY_LIMIT} сек</b>", parse_mode='html'); return
            if high > MAX_DELAY_LIMIT:
                await e.reply(f"⚠️ Максимум — <b>{MAX_DELAY_LIMIT} сек</b>", parse_mode='html'); return
            if high < low:
                await e.reply("⚠️ Максимум меньше минимума.", parse_mode='html'); return
            await save_user(uid, min_d=low * 1000, max_d=high * 1000)
            user_steps[uid] = "IDLE"
            await e.reply(f"✅ Задержка: <b>{low}–{high} сек</b>", buttons=main_menu(is_admin), parse_mode='html')
        except:
            await e.reply("❌ Формат: <code>5-15</code>", parse_mode='html')

    elif step == "IDLE":
        new_lines = [line.strip() for line in text.split('\n') if line.strip()]
        cur = p.get('templates', [])
        added = 0
        for line in new_lines:
            if line not in cur:
                cur.append(line)
                added += 1
        if added > 0:
            await save_user(uid, templates=cur)
            await e.reply(f"📝 Добавлено: <b>{added}</b>. Всего: <b>{len(cur)}</b>", parse_mode='html')
        else:
            await e.reply("⚠️ Эти шаблоны уже есть.")

# =====================================================================
# 7. WEB DASHBOARD
# =====================================================================
app = Flask(__name__)
START_TIME = datetime.datetime.now()

DASHBOARD = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TITAN Enterprise</title>
<style>
:root{--bg:#0a0e14;--card:#141a22;--line:#1f2933;--txt:#e8eef5;--muted:#7a8899;--acc:#4ea8de;--ok:#3ddc97;--warn:#ffb703;--err:#ef476f}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.wrap{width:100%;max-width:760px}
.header{text-align:center;margin-bottom:24px}
.logo{font-size:32px;font-weight:800;letter-spacing:-1px;background:linear-gradient(135deg,#4ea8de,#3ddc97);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.sub{color:var(--muted);font-size:13px;margin-top:4px;letter-spacing:1px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;transition:transform .15s,border-color .15s}
.card:hover{transform:translateY(-2px);border-color:var(--acc)}
.card .label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:10px}
.card .value{font-size:32px;font-weight:700;font-variant-numeric:tabular-nums}
.card.ok .value{color:var(--ok)}
.card.warn .value{color:var(--warn)}
.card.err .value{color:var(--err)}
.card.acc .value{color:var(--acc)}
.footer{text-align:center;color:var(--muted);font-size:12px;margin-top:20px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);margin-right:6px;animation:pulse 1.6s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="logo">TITAN Enterprise</div>
    <div class="sub">CONTROL PANEL</div>
  </div>
  <div class="grid">
    <div class="card acc"><div class="label">Отправлено</div><div class="value" id="m">0</div></div>
    <div class="card ok"><div class="label">Сессий</div><div class="value" id="s">0</div></div>
    <div class="card err"><div class="label">Ошибок</div><div class="value" id="e">0</div></div>
    <div class="card warn"><div class="label">FloodWait</div><div class="value" id="f">0</div></div>
  </div>
  <div class="footer"><span class="dot"></span>Uptime: <span id="u">—</span></div>
</div>
<script>
async function refresh(){
  try{
    const r = await fetch('/api/stats');
    const d = await r.json();
    document.getElementById('m').textContent = d.messages;
    document.getElementById('s').textContent = d.sessions;
    document.getElementById('e').textContent = d.errors;
    document.getElementById('f').textContent = d.flood;
    document.getElementById('u').textContent = d.uptime;
  }catch(e){}
}
refresh(); setInterval(refresh, 3000);
</script>
</body>
</html>"""

@app.route('/')
def index():
    return DASHBOARD

@app.route('/api/stats')
def stats():
    uptime = str(datetime.datetime.now() - START_TIME).split('.')[0]
    return jsonify({
        "messages": system_metrics["total_messages_sent"],
        "sessions": len([k for k in active_clients if not k.startswith("temp_") and not k.startswith("qr_")]),
        "errors": system_metrics["total_errors_caught"],
        "flood": system_metrics["total_flood_wait_events"],
        "uptime": uptime,
    })

@app.route('/healthz')
def health():
    return jsonify({"status": "OK"}), 200

def run_web():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# =====================================================================
# 8. ЗАПУСК
# =====================================================================
async def main():
    await TitanDB.bootstrap()
    await TitanDB.backup()
    threading.Thread(target=run_web, daemon=True).start()
    TitanLogger.sys(f"Web: http://0.0.0.0:{os.environ.get('PORT', 8080)}")

    try:
        await bot.start(bot_token=BOT_TOKEN)
        TitanLogger.info("Бот запущен.")
        await bot.run_until_disconnected()
    except AccessTokenInvalidError:
        TitanLogger.critical("Неверный токен!")
        while True:
            await asyncio.sleep(3600)
    except Exception as e:
        TitanLogger.critical(f"Сбой: {e}")

if __name__ == '__main__':
    TitanLogger.sys("=" * 50)
    TitanLogger.sys("  TITAN Enterprise — старт")
    TitanLogger.sys("=" * 50)
    asyncio.run(main())
