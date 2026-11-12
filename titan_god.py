import os
import sys
import json
import re
import asyncio
import random
import datetime
import threading
import shutil
import urllib.parse
from colorama import init, Fore, Style
from flask import Flask, jsonify
import aiosqlite

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError, SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError
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

MIN_DELAY_LIMIT = 1
MAX_DELAY_LIMIT = 60

# Теперь добавили max_targets — сколько целей можно запустить одновременно
PLAN_LIMITS = {
    'FREE':     {'templates': 10,  'min_delay': 50, 'max_delay': 60, 'max_targets': 1},
    'PRO':      {'templates': 100, 'min_delay': 30, 'max_delay': 60, 'max_targets': 2},
    'ULTIMATE': {'templates': 9999,'min_delay': 1,  'max_delay': 60, 'max_targets': 4},
}

active_clients = {}
user_steps = {}
spam_engines = {}  # {uid: SpamEngine} — один движок на пользователя, внутри может быть несколько целей

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
                    min_delay INTEGER DEFAULT 50000,
                    max_delay INTEGER DEFAULT 60000,
                    is_approved INTEGER DEFAULT 0,
                    is_admin INTEGER DEFAULT 0,
                    plan TEXT DEFAULT 'FREE',
                    accepted_rules INTEGER DEFAULT 0,
                    registered_at TEXT
                )
            ''')
            try:
                await db.execute("ALTER TABLE users ADD COLUMN accepted_rules INTEGER DEFAULT 0")
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
            "SELECT session_string, templates, min_delay, max_delay, is_approved, is_admin, plan, accepted_rules FROM users WHERE user_id = ?",
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
                    'accepted_rules': row[7] or 0,
                }
    return None

async def save_user(uid, session=None, templates=None, min_d=None, max_d=None, approved=None, admin=None, plan=None, accepted_rules=None):
    cur = await get_user(uid)
    s = session if session is not None else (cur['session'] if cur else None)
    t = templates if templates is not None else (cur['templates'] if cur else [])
    mind = min_d if min_d is not None else (cur['min_delay'] if cur else 50000)
    maxd = max_d if max_d is not None else (cur['max_delay'] if cur else 60000)
    a = approved if approved is not None else (cur['is_approved'] if cur else 0)
    adm = admin if admin is not None else (cur['is_admin'] if cur else 0)
    pl = plan if plan is not None else (cur['plan'] if cur else 'FREE')
    ar = accepted_rules if accepted_rules is not None else (cur['accepted_rules'] if cur else 0)

    if uid in SUPER_ADMINS:
        a = 1
        adm = 1
        pl = 'ULTIMATE'
        ar = 1

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''
            INSERT INTO users (user_id, session_string, templates, min_delay, max_delay, is_approved, is_admin, plan, accepted_rules, registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
            session_string=excluded.session_string,
            templates=excluded.templates,
            min_delay=excluded.min_delay,
            max_delay=excluded.max_delay,
            is_approved=excluded.is_approved,
            is_admin=excluded.is_admin,
            plan=excluded.plan,
            accepted_rules=excluded.accepted_rules
        ''', (uid, s, json.dumps(t), mind, maxd, a, adm, pl, ar, str(datetime.datetime.now())))
        await db.commit()

def is_super(uid):
    return int(uid) in SUPER_ADMINS

def plan_limits(plan):
    return PLAN_LIMITS.get(plan, PLAN_LIMITS['FREE'])

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
# 5. СПАМ-ДВИЖОК (МУЛЬТИ-ЦЕЛЬ)
# =====================================================================
class SpamEngine:
    def __init__(self, client, user_id):
        self.client = client
        self.user_id = user_id
        self.is_active = False
        self.tasks = []  # список корутин/задач для каждой цели

    async def attack_single(self, target, templates, min_delay, max_delay):
        """Атака одного чата. Используется внутри gather."""
        counter = 0
        start_time = str(datetime.datetime.now())

        # Приводим цель к нормальному виду
        if "t.me/" in target:
            target = "@" + target.split("t.me/")[-1].split("/")[0].split("?")[0]
        target = target.strip()

        current_min = max(MIN_DELAY_LIMIT * 1000, min(min_delay, MAX_DELAY_LIMIT * 1000))
        current_max = max(MIN_DELAY_LIMIT * 1000, min(max_delay, MAX_DELAY_LIMIT * 1000))
        if current_max < current_min:
            current_max = current_min

        TitanLogger.info(f"Старт [{self.user_id}] -> {target} | {current_min/1000}-{current_max/1000} сек")

        while self.is_active:
            try:
                if not templates:
                    break
                if not self.is_active:
                    break

                msg = random.choice(templates)
                await self.client.send_message(target, msg)
                counter += 1
                system_metrics["total_messages_sent"] += 1
                TitanLogger.log("SPAM", f"[{self.user_id}] -> {target} #{counter}", Fore.BLUE)

                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute('''
                        INSERT INTO spam_history (user_id, target, sent_messages_count, status, started_at, ended_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (self.user_id, str(target), counter, 'RUNNING', start_time, str(datetime.datetime.now())))
                    await db.commit()

                delay = random.randint(current_min, current_max) / 1000.0
                slept = 0
                while slept < delay and self.is_active:
                    chunk = min(1.0, delay - slept)
                    await asyncio.sleep(chunk)
                    slept += chunk

            except FloodWaitError as e:
                system_metrics["total_flood_wait_events"] += 1
                TitanLogger.warning(f"[{self.user_id}] FloodWait {e.seconds} сек ({target}).")
                await asyncio.sleep(e.seconds + 5)
            except ChatWriteForbiddenError:
                system_metrics["total_errors_caught"] += 1
                TitanLogger.error(f"[{self.user_id}] Нет прав писать в {target}.")
                break
            except Exception as e:
                system_metrics["total_errors_caught"] += 1
                TitanLogger.error(f"[{self.user_id}] Сбой {target}: {e}")
                break

        TitanLogger.info(f"Стоп цели {target} [{self.user_id}]. Отправлено: {counter}")

    async def attack(self, targets, templates, min_delay, max_delay):
        self.is_active = True
        tasks = [self.attack_single(t, templates, min_delay, max_delay) for t in targets]
        await asyncio.gather(*tasks, return_exceptions=True)
        self.is_active = False
        TitanLogger.info(f"Движок [{self.user_id}] полностью остановлен.")

    def stop(self):
        self.is_active = False

# =====================================================================
# 6. БОТ
# =====================================================================
bot = TelegramClient('bot_master', API_ID, API_HASH)

RULES_TEXT = (
    "<b>📜 ПРАВИЛА ПОЛЬЗОВАНИЯ</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "Перед использованием бота ознакомьтесь с правилами:\n\n"
    "<b>1. Запрещено:</b>\n"
    "• Массовая рассылка без согласия получателей\n"
    "• Спам, флуд, мошенничество\n"
    "• Использование бота для незаконных целей\n"
    "• Передача доступа к боту третьим лицам\n\n"
    "<b>2. Ответственность:</b>\n"
    "• Администрация не несёт ответственности за блокировки ваших аккаунтов Telegram\n"
    "• Все действия вы совершаете на свой страх и риск\n"
    "• При нарушении правил доступ будет заблокирован без возврата средств\n\n"
    "<b>3. Оплата:</b>\n"
    f"• Оплата подписок и аккаунтов через {SUPPORT_CONTACT}\n"
    "• Возврат средств не предусмотрен\n\n"
    "<b>4. Конфиденциальность:</b>\n"
    "• Сессии аккаунтов хранятся локально и не передаются третьим лицам\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "Нажимая «✅ Принимаю», вы соглашаетесь с правилами."
)

def main_menu(is_admin):
    menu = [
        [Button.inline("👤 Аккаунт", b"nav_account"), Button.inline("📝 Шаблоны", b"nav_templates")],
        [Button.inline("⚙️ Задержка", b"nav_delay"), Button.inline("🚀 Запуск", b"nav_spam")],
        [Button.inline("🛑 Остановить", b"nav_stop")],
        [Button.inline("💎 Подписка", b"nav_sub"), Button.inline("🛒 Купить аккаунт", b"nav_buy_acc")],
        [Button.inline("ℹ️ Помощь", b"nav_help")],
    ]
    if is_admin:
        menu.append([Button.inline("👑 Админ-панель", b"nav_admin")])
    return menu

def main_text(uid, p):
    status = "✅ Подключён" if p and p['session'] else "❌ Не подключён"
    tpl = len(p['templates']) if p else 0
    mind = int(p['min_delay'] / 1000) if p else 50
    maxd = int(p['max_delay'] / 1000) if p else 60
    active = "🟢 Активен" if uid in spam_engines else "⚪ Не запущен"
    crown = "👑 " if is_super(uid) else ""
    plan = p.get('plan', 'FREE') if p else 'FREE'
    plan_emoji = {'FREE': '⚪', 'PRO': '💎', 'ULTIMATE': '👑'}.get(plan, '⚪')
    limits = plan_limits(plan)

    return (
        f"<b>⚡ TITAN Enterprise</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>ID:</b> <code>{uid}</code> {crown}\n"
        f"📱 <b>Аккаунт:</b> {status}\n"
        f"📝 <b>Шаблонов:</b> {tpl}\n"
        f"⚙️ <b>Задержка:</b> {mind}–{maxd} сек\n"
        f"🎯 <b>Лимит целей:</b> {limits['max_targets']}\n"
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
        if not p or p['is_admin'] != 1 or p['is_approved'] != 1 or p['plan'] != 'ULTIMATE':
            await save_user(uid, admin=1, approved=1, plan='ULTIMATE')
            TitanLogger.sys(f"Создатель авторизован: {uid}")
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

    if not p or p.get('accepted_rules') != 1:
        await e.reply(
            RULES_TEXT,
            buttons=[
                [Button.inline("✅ Принимаю", b"accept_rules")],
                [Button.inline("❌ Отказываюсь", b"decline_rules")],
            ],
            parse_mode='html'
        )
        return

    user_steps[uid] = "IDLE"
    await e.reply(main_text(uid, p), buttons=main_menu(is_admin), parse_mode='html')

@bot.on(events.CallbackQuery(data="accept_rules"))
async def cb_accept_rules(e):
    uid = e.sender_id
    await save_user(uid, accepted_rules=1)
    await e.answer("✅ Правила приняты")
    p = await get_user(uid)
    is_admin = p.get('is_admin') == 1 or is_super(uid)
    await e.edit(main_text(uid, p), buttons=main_menu(is_admin), parse_mode='html')

@bot.on(events.CallbackQuery(data="decline_rules"))
async def cb_decline_rules(e):
    await e.edit(
        "❌ <b>Вы отказались от правил</b>\n\n"
        "Без принятия правил использование бота невозможно.\n"
        f"Если передумаете — напишите /start\n\n"
        f"По вопросам: {SUPPORT_CONTACT}",
        parse_mode='html'
    )

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
            f"Сессия активна.\n\n"
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

        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={urllib.parse.quote(qr.url)}"

        await e.edit(
            "📷 <b>Вход по QR-коду</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ Открой Telegram на телефоне\n"
            "2️⃣ Настройки → Устройства → Подключить устройство\n"
            "3️⃣ Отсканируй QR ниже\n\n"
            f"🔗 <a href='{qr_url}'>Открыть QR-код</a>\n\n"
            "⏳ Жду сканирования (120 сек)...",
            buttons=[[Button.inline("🔙 Отмена", b"qr_cancel")]],
            parse_mode='html'
        )

        try:
            await asyncio.wait_for(qr.wait(), timeout=120)
            await save_user(uid, session=c.session.save())
            active_clients[uid] = c
            if f"qr_{uid}" in active_clients:
                del active_clients[f"qr_{uid}"]
            user_steps[uid] = "IDLE"
            await e.edit(
                "✅ <b>Успешно авторизован по QR!</b>\n\n/start",
                parse_mode='html'
            )
        except asyncio.TimeoutError:
            await e.edit("⏱ Время QR истекло. /start")
            user_steps[uid] = "IDLE"
    except Exception as ex:
        await e.edit(f"❌ Ошибка QR: <code>{str(ex)[:200]}</code>", parse_mode='html')
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
        await e.answer("📞 Повторный запрос (возможно, звонком)", alert=True)
    except Exception as ex:
        await e.answer(f"❌ {str(ex)[:100]}", alert=True)

@bot.on(events.CallbackQuery)
async def cb_handler(e):
    uid = e.sender_id
    action = e.data.decode('utf-8')
    p = await get_user(uid) or {}
    is_admin = p.get('is_admin') == 1 or is_super(uid)
    is_approved = p.get('is_approved') == 1 or is_super(uid)

    if not is_admin and not is_approved:
        return await e.answer("⛔ Доступ закрыт", alert=True)

    if not p.get('accepted_rules') and action not in ("accept_rules", "decline_rules"):
        return await e.answer("⚠️ Сначала примите правила", alert=True)

    back = [[Button.inline("🔙 Назад", b"nav_home")]]

    if action == "nav_home":
        user_steps[uid] = "IDLE"
        p = await get_user(uid) or {}
        is_admin = p.get('is_admin') == 1 or is_super(uid)
        await e.edit(main_text(uid, p), buttons=main_menu(is_admin), parse_mode='html')

    elif action == "nav_templates":
        tpls = p.get('templates', [])
        limits = plan_limits(p.get('plan', 'FREE'))
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
            f"📝 <b>Шаблоны ({len(tpls)}/{limits['templates']})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{preview}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Отправьте текст — каждая строка = шаблон.",
            buttons=buttons, parse_mode='html'
        )

    elif action == "nav_tpl_clear":
        await save_user(uid, templates=[])
        await e.answer("✅ Все шаблоны удалены")
        await e.edit(
            "📝 <b>Шаблоны (0 шт.)</b>\n\n<i>Список очищен.</i>",
            buttons=[[Button.inline("🔙 Назад", b"nav_home")]], parse_mode='html'
        )

    elif action == "nav_delay":
        user_steps[uid] = "ST_DELAY"
        limits = plan_limits(p.get('plan', 'FREE'))
        mind = int(p.get('min_delay', 50000) / 1000)
        maxd = int(p.get('max_delay', 60000) / 1000)
        await e.edit(
            f"⚙️ <b>Задержка</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Текущая: <b>{mind}–{maxd} сек</b>\n"
            f"Доступный диапазон для тарифа <b>{p.get('plan','FREE')}</b>:\n"
            f"<b>{limits['min_delay']}–{limits['max_delay']}</b> сек\n\n"
            f"Формат: <code>{limits['min_delay']}-{limits['max_delay']}</code>",
            buttons=back, parse_mode='html'
        )

    elif action == "nav_spam":
        if not p.get('session'):
            return await e.answer("❌ Нет сессии", alert=True)
        if not p.get('templates'):
            return await e.answer("❌ Нет шаблонов", alert=True)
        limits = plan_limits(p.get('plan', 'FREE'))
        user_steps[uid] = "ST_TARGET"
        await e.edit(
            f"🚀 <b>Запуск (до {limits['max_targets']} целей)</b>\n\n"
            f"Отправьте список целей — <b>каждая с новой строки</b>.\n"
            f"Например:\n"
            f"<code>@chat1\n@chat2\n@chat3</code>\n\n"
            f"Или ссылки: <code>https://t.me/chat1</code>\n"
            f"Или ID: <code>123456789</code>",
            buttons=back, parse_mode='html'
        )

    elif action == "nav_stop":
        if uid in spam_engines:
            spam_engines[uid].stop()
            del spam_engines[uid]
            await e.edit(
                "🛑 <b>Процесс остановлен</b>\n\n"
                "Все активные рассылки завершены.",
                buttons=back, parse_mode='html'
            )
        else:
            await e.answer("⚠️ Активных рассылок не найдено", alert=True)

    elif action == "nav_sub":
        plan = p.get('plan', 'FREE')
        await e.edit(
            f"💎 <b>Тарифные планы</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚪ <b>FREE</b> — 0 ₽\n"
            f"• До 10 шаблонов\n"
            f"• Задержка 50–60 сек\n"
            f"• Одновременно: 1 цель\n\n"
            f"💎 <b>PRO</b> — 490 ₽/мес\n"
            f"• До 100 шаблонов\n"
            f"• Задержка 30–60 сек\n"
            f"• Одновременно: 2 цели\n"
            f"• Приоритетная поддержка\n\n"
            f"👑 <b>ULTIMATE</b> — 1 490 ₽/мес\n"
            f"• Без лимитов шаблонов\n"
            f"• Задержка 1–60 сек\n"
            f"• Одновременно: 4 цели\n"
            f"• Персональный менеджер\n\n"
            f"🛒 <b>Доп. аккаунт (слот)</b> — 300 ₽/мес\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Ваш тариф: <b>{plan}</b>\n\n"
            f"📩 <b>Для покупки:</b> {SUPPORT_CONTACT}",
            buttons=back, parse_mode='html'
        )

    elif action == "nav_buy_acc":
        await e.edit(
            f"🛒 <b>Покупка аккаунта</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Мы предоставляем готовые аккаунты для работы с ботом.\n\n"
            f"<b>Что нужно знать:</b>\n"
            f"• Аккаунт передаётся вручную\n"
            f"• Оплата обсуждается индивидуально\n"
            f"• Все детали — в личных сообщениях\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📩 <b>Для покупки пишите:</b> {SUPPORT_CONTACT}",
            buttons=back, parse_mode='html'
        )

    elif action == "nav_help":
        await e.edit(
            f"ℹ️ <b>Помощь и инструкция</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<b>🔑 ВАЖНО: Как правильно заходить в аккаунт</b>\n"
            f"Если вы заходите со своего <b>основного</b> номера (на котором уже есть Telegram), Telegram может заблокировать вход бота и не прислать код.\n\n"
            f"✅ <b>Что делать:</b>\n"
            f"1. Используйте <b>другой (дополнительный)</b> аккаунт/номер для создания сессии.\n"
            f"2. Или заходите через <b>QR-код</b> (кнопка появляется после ввода номера).\n"
            f"3. Если код не приходит — попробуйте запросить <b>звонок</b> вместо SMS.\n\n"
            f"<b>💎 Тарифы и цены</b>\n"
            f"• ⚪ FREE — 0 ₽ (1 цель, 10 шаблонов)\n"
            f"• 💎 PRO — 490 ₽/мес (2 цели, 100 шаблонов)\n"
            f"• 👑 ULTIMATE — 1 490 ₽/мес (4 цели, без лимита шаблонов)\n"
            f"• 🛒 Доп. аккаунт (слот) — 300 ₽/мес\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📩 <b>По всем вопросам и покупкам:</b> {SUPPORT_CONTACT}",
            buttons=back, parse_mode='html'
        )

    elif action == "nav_admin" and is_admin:
        await e.edit(
            "👑 <b>Админ-панель</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<code>.approve &lt;ID&gt;</code> — выдать доступ\n"
            "<code>.revoke &lt;ID&gt;</code> — забрать доступ\n"
            "<code>.sub &lt;ID&gt; &lt;FREE|PRO|ULTIMATE&gt;</code> — тариф\n"
            "<code>.id</code> — свой ID",
            buttons=back, parse_mode='html'
        )

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
                    await e.reply("⚠️ Нельзя забрать доступ у создателя.", parse_mode='html')
                    return
                await save_user(t_id, approved=0)
                await e.reply(f"🚫 Доступ забран: <code>{t_id}</code>", parse_mode='html')
            except:
                await e.reply("❌ <code>.revoke 1234567</code>", parse_mode='html')
            return

        if text.startswith('.sub'):
            try:
                parts = text.split(' ')
                t_id = int(parts[1])
                new_plan = parts[2].upper()
                if new_plan not in ('FREE', 'PRO', 'ULTIMATE'):
                    await e.reply("❌ Тарифы: FREE / PRO / ULTIMATE", parse_mode='html')
                    return
                await save_user(t_id, plan=new_plan)
                await e.reply(
                    f"✅ Подписка <b>{new_plan}</b> выдана <code>{t_id}</code>",
                    parse_mode='html'
                )
            except:
                await e.reply("❌ <code>.sub 1234567 ULTIMATE</code>", parse_mode='html')
            return

        if text == '.id':
            await e.reply(f"Ваш ID: <code>{uid}</code>", parse_mode='html')
            return

    if not is_admin and not is_approved:
        return

    if not p.get('accepted_rules'):
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
                f"❌ Ошибка: <code>{str(ex)[:200]}</code>",
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
            await e.reply("❌ Неверный код.")
        except PhoneCodeExpiredError:
            await e.reply("❌ Код истёк. Запросите новый.")
            user_steps[uid] = "IDLE"
        except Exception as ex:
            await e.reply(f"❌ Ошибка: <code>{str(ex)[:200]}</code>", parse_mode='html')
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
        limits = plan_limits(p.get('plan', 'FREE'))

        # Разбиваем ввод на цели
        raw_targets = [line.strip() for line in text.split('\n') if line.strip()]
        if not raw_targets:
            await e.reply("❌ Вы не указали ни одной цели.")
            return

        # Проверка лимита
        if len(raw_targets) > limits['max_targets']:
            await e.reply(
                f"⚠️ Ваш тариф <b>{p.get('plan','FREE')}</b> разрешает максимум <b>{limits['max_targets']}</b> целей одновременно.\n"
                f"Вы указали: <b>{len(raw_targets)}</b>.\n\n"
                f"Уберите лишние строки или повысьте тариф: /start → 💎 Подписка",
                parse_mode='html'
            )
            return

        # Проверка, что нет активной рассылки
        if uid in spam_engines:
            spam_engines[uid].stop()
            try:
                await spam_engines[uid].client.disconnect()
            except:
                pass
            del spam_engines[uid]

        c = await resolve_client(uid)
        if not c:
            await e.reply("❌ Сессия потеряна.")
            return

        eng = SpamEngine(c, uid)
        spam_engines[uid] = eng
        asyncio.create_task(eng.attack(raw_targets, p['templates'], p['min_delay'], p['max_delay']))

        targets_str = "\n".join([f"• <code>{t}</code>" for t in raw_targets])
        await e.reply(
            f"🚀 <b>Запущено</b>\n\n"
            f"<b>Цели ({len(raw_targets)}):</b>\n{targets_str}\n\n"
            f"Задержка: {int(p['min_delay']/1000)}–{int(p['max_delay']/1000)} сек\n\n"
            f"Остановить: /start → 🛑 Остановить",
            parse_mode='html'
        )

    elif step == "ST_DELAY":
        limits = plan_limits(p.get('plan', 'FREE'))
        try:
            low, high = map(int, text.split('-'))
            if low < limits['min_delay']:
                await e.reply(
                    f"⚠️ Минимум для тарифа <b>{p.get('plan','FREE')}</b> — <b>{limits['min_delay']} сек</b>\n"
                    f"Повысьте тариф: /start → 💎 Подписка",
                    parse_mode='html'
                ); return
            if high > limits['max_delay']:
                await e.reply(
                    f"⚠️ Максимум для тарифа <b>{p.get('plan','FREE')}</b> — <b>{limits['max_delay']} сек</b>",
                    parse_mode='html'
                ); return
            if high < low:
                await e.reply("⚠️ Максимум меньше минимума.", parse_mode='html'); return
            await save_user(uid, min_d=low * 1000, max_d=high * 1000)
            user_steps[uid] = "IDLE"
            await e.reply(f"✅ Задержка: <b>{low}–{high} сек</b>", buttons=main_menu(is_admin), parse_mode='html')
        except:
            await e.reply(
                f"❌ Формат: <code>{limits['min_delay']}-{limits['max_delay']}</code>",
                parse_mode='html'
            )

    elif step == "IDLE":
        limits = plan_limits(p.get('plan', 'FREE'))
        new_lines = [line.strip() for line in text.split('\n') if line.strip()]
        cur = p.get('templates', [])
        added = 0
        skipped = 0
        for line in new_lines:
            if line in cur:
                continue
            if len(cur) >= limits['templates']:
                skipped += 1
                continue
            cur.append(line)
            added += 1

        if added > 0:
            await save_user(uid, templates=cur)
            msg = f"📝 Добавлено: <b>{added}</b>. Всего: <b>{len(cur)}/{limits['templates']}</b>"
            if skipped > 0:
                msg += f"\n⚠️ Пропущено {skipped} — лимит тарифа."
            await e.reply(msg, parse_mode='html')
        elif skipped > 0:
            await e.reply(
                f"⚠️ Лимит шаблонов для тарифа <b>{p.get('plan','FREE')}</b> — {limits['templates']}.\n"
                f"Повысьте тариф: /start → 💎 Подписка",
                parse_mode='html'
            )
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
