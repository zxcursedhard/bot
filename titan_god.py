# =====================================================================
# TITAN Enterprise — монолитная сборка для Render
# =====================================================================
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
import logging
import hmac

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError, SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, ChatWriteForbiddenError, ChatAdminRequiredError,
)
from telethon.errors.rpcerrorlist import AccessTokenInvalidError
from flask import Flask, jsonify, request, abort, Response
from colorama import init
import aiosqlite

init(autoreset=True)

# =====================================================================
# 1. КОНФИГ
# =====================================================================
API_ID = int(os.getenv("API_ID", "2040"))
API_HASH = os.getenv("API_HASH", "b18441a1ff607e10a989891a5462e627")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8732669492:AAF6lfPEgcOp9iKzo74UGnPIPw9wfVa7YGo")
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "titan_super_secret_change_me_12345")

SUPER_ADMINS = {8655682794, 8773171051}
SUPPORT_CONTACT = "@apemiru"

DB_FILE = os.getenv("DB_FILE", "titan_database.db")
BACKUP_DIR = os.getenv("BACKUP_DIR", "database_backups")
PORT = int(os.getenv("PORT", "8080"))

REFERRAL_PERCENT = 30
MIN_DELAY_LIMIT = 1
MAX_DELAY_LIMIT = 60

# Persistent disk на Render монтируется в /var/data — если есть, используем его
if os.path.isdir("/var/data") and not os.getenv("DB_FILE"):
    DB_FILE = "/var/data/titan_database.db"
    BACKUP_DIR = "/var/data/database_backups"

PLAN_LIMITS = {
    "FREE": {
        "templates": 10, "min_delay": 50, "max_delay": 60,
        "max_targets": 1, "stats": False, "badge": "⚪", "title": "Новичок",
    },
    "PRO": {
        "templates": 100, "min_delay": 30, "max_delay": 60,
        "max_targets": 2, "stats": True, "badge": "💎", "title": "PRO-юзер",
    },
    "ULTIMATE": {
        "templates": 9999, "min_delay": 1, "max_delay": 60,
        "max_targets": 4, "stats": True, "badge": "👑", "title": "ULTIMATE",
    },
}

def plan_limits(plan: str) -> dict:
    return PLAN_LIMITS.get(plan, PLAN_LIMITS["FREE"])

# =====================================================================
# 2. ЛОГИ
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("TITAN")

# =====================================================================
# 3. БД
# =====================================================================
SCHEMA = """
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
    registered_at TEXT,
    balance INTEGER DEFAULT 0,
    referred_by INTEGER,
    spin_enabled INTEGER DEFAULT 0,
    schedule TEXT
);
CREATE TABLE IF NOT EXISTS spam_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    target TEXT,
    sent_messages_count INTEGER DEFAULT 0,
    status TEXT,
    started_at TEXT,
    ended_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_hist_user ON spam_history(user_id, started_at);
"""

MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN balance INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN referred_by INTEGER",
    "ALTER TABLE users ADD COLUMN spin_enabled INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN schedule TEXT",
]

USER_DEFAULTS = {
    "session": None, "templates": [], "min_delay": 50000, "max_delay": 60000,
    "is_approved": 0, "is_admin": 0, "plan": "FREE", "accepted_rules": 0,
    "balance": 0, "referred_by": None, "spin_enabled": 0, "schedule": None,
}

class TitanDB:
    def __init__(self, path, backup_dir):
        self.path = path
        self.backup_dir = backup_dir
        self._lock = asyncio.Lock()

    def conn(self):
        return aiosqlite.connect(self.path)

    async def bootstrap(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        os.makedirs(self.backup_dir, exist_ok=True)
        async with self.conn() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript(SCHEMA)
            for sql in MIGRATIONS:
                try:
                    await db.execute(sql)
                except aiosqlite.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            await db.commit()
        log.info("БД готова: %s", self.path)

    async def backup(self):
        async with self._lock:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            dst = os.path.join(self.backup_dir, f"backup_{ts}.db")
            if os.path.exists(self.path):
                await asyncio.to_thread(shutil.copyfile, self.path, dst)
                log.info("Бэкап: %s", dst)
                files = sorted(f for f in os.listdir(self.backup_dir) if f.startswith("backup_"))
                for old in files[:-10]:
                    try: os.remove(os.path.join(self.backup_dir, old))
                    except OSError: pass

    async def backup_loop(self, hours=6):
        while True:
            await asyncio.sleep(hours * 3600)
            try: await self.backup()
            except Exception: log.exception("Бэкап упал")


class UserRepo:
    def __init__(self, db: TitanDB):
        self.db = db

    async def get(self, uid):
        async with self.db.conn() as db:
            async with db.execute(
                "SELECT session_string, templates, min_delay, max_delay, is_approved,"
                " is_admin, plan, accepted_rules, balance, referred_by, spin_enabled, schedule"
                " FROM users WHERE user_id=?", (uid,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        d = dict(USER_DEFAULTS)
        d.update(
            session=row[0], templates=json.loads(row[1] or "[]"),
            min_delay=row[2], max_delay=row[3], is_approved=row[4],
            is_admin=row[5], plan=row[6] or "FREE", accepted_rules=row[7] or 0,
            balance=row[8] or 0, referred_by=row[9], spin_enabled=row[10] or 0,
            schedule=json.loads(row[11]) if row[11] else None,
        )
        return d

    async def save(self, uid, **kw):
        cur = await self.get(uid) or dict(USER_DEFAULTS)
        cur.update({k: v for k, v in kw.items() if v is not None})

        if int(uid) in SUPER_ADMINS:
            cur.update(is_approved=1, is_admin=1, plan="ULTIMATE", accepted_rules=1)

        async with self.db.conn() as db:
            await db.execute(
                """INSERT INTO users (user_id, session_string, templates, min_delay,
                    max_delay, is_approved, is_admin, plan, accepted_rules, registered_at,
                    balance, referred_by, spin_enabled, schedule)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    session_string=excluded.session_string,
                    templates=excluded.templates,
                    min_delay=excluded.min_delay,
                    max_delay=excluded.max_delay,
                    is_approved=excluded.is_approved,
                    is_admin=excluded.is_admin,
                    plan=excluded.plan,
                    accepted_rules=excluded.accepted_rules,
                    balance=excluded.balance,
                    referred_by=excluded.referred_by,
                    spin_enabled=excluded.spin_enabled,
                    schedule=excluded.schedule""",
                (uid, cur["session"], json.dumps(cur["templates"]),
                 cur["min_delay"], cur["max_delay"], cur["is_approved"],
                 cur["is_admin"], cur["plan"], cur["accepted_rules"],
                 datetime.datetime.now().isoformat(timespec="seconds"),
                 cur["balance"], cur["referred_by"], cur["spin_enabled"],
                 json.dumps(cur["schedule"]) if cur["schedule"] else None),
            )
            await db.commit()

    async def add_balance(self, uid, amount):
        p = await self.get(uid) or dict(USER_DEFAULTS)
        await self.save(uid, balance=(p["balance"] or 0) + amount)

# =====================================================================
# 4. ДВИЖОК
# =====================================================================
_TG_URL = re.compile(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})")
_NUMERIC = re.compile(r"^-?\d{5,}$")

def normalize_target(raw):
    raw = raw.strip()
    if not raw: return None
    m = _TG_URL.search(raw)
    if m: return "@" + m.group(1)
    if raw.startswith("@") and len(raw) > 1: return raw
    if _NUMERIC.match(raw): return raw
    if raw.startswith("+"): return raw
    return None


class AdaptivePacer:
    def __init__(self, min_ms, max_ms):
        self.base_min, self.base_max = min_ms, max_ms
        self.penalty = 0
        self.flood_count = 0

    def next_delay(self):
        lo, hi = self.base_min + self.penalty, self.base_max + self.penalty
        return random.randint(lo, hi) / 1000.0

    def on_flood(self, seconds):
        self.flood_count += 1
        self.penalty = min(self.base_max * 2, self.penalty + int(self.base_max * 0.05))
        return seconds + 5


class SpamEngine:
    def __init__(self, client, user_id, db_path, min_ms, max_ms, metrics):
        self.client = client
        self.user_id = user_id
        self.db_path = db_path
        self.pacer = AdaptivePacer(min_ms, max_ms)
        self._active = asyncio.Event()
        self._active.set()
        self._tasks = set()
        self.started_at = datetime.datetime.now()
        self.sent = 0
        self.short_id = f"{user_id:x}"[-4:]
        self.metrics = metrics

    @property
    def is_active(self):
        return self._active.is_set()

    async def _write_history(self, target, count, status):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO spam_history (user_id, target, sent_messages_count,"
                    " status, started_at, ended_at) VALUES (?,?,?,?,?,?)",
                    (self.user_id, target, count, status,
                     self.started_at.isoformat(timespec="seconds"),
                     datetime.datetime.now().isoformat(timespec="seconds")),
                )
                await db.commit()
        except Exception as e:
            log.warning("history: %s", e)

    async def _attack_one(self, target, templates):
        counter = 0
        log.info("[%s] -> %s", self.user_id, target)
        while self.is_active:
            try:
                # ⚠️ Шаблон уходит РОВНО как ввёл юзер
                msg = random.choice(templates)
                await self.client.send_message(target, msg)
                counter += 1
                self.sent += 1
                self.metrics["total_messages_sent"] += 1

                if counter % 10 == 0:
                    await self._write_history(target, counter, "RUNNING")

                delay = self.pacer.next_delay()
                try:
                    await asyncio.wait_for(self._active.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
            except FloodWaitError as e:
                self.metrics["total_flood_wait_events"] += 1
                wait = self.pacer.on_flood(e.seconds)
                log.warning("[%s] FloodWait %ss (%s)", self.user_id, e.seconds, target)
                await asyncio.sleep(wait)
            except ChatWriteForbiddenError:
                self.metrics["total_errors_caught"] += 1
                log.error("[%s] нет прав в %s", self.user_id, target); break
            except ChatAdminRequiredError:
                self.metrics["total_errors_caught"] += 1
                log.error("[%s] нужен админ в %s", self.user_id, target); break
            except Exception as e:
                self.metrics["total_errors_caught"] += 1
                log.exception("[%s] сбой %s: %s", self.user_id, target, e); break
        await self._write_history(target, counter, "STOPPED")

    async def start(self, targets, templates):
        self._active.set()
        tasks = [asyncio.create_task(self._attack_one(t, templates)) for t in targets]
        self._tasks.update(tasks)
        for t in tasks:
            t.add_done_callback(self._tasks.discard)
        await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()

    def stop(self):
        self._active.clear()
        for t in list(self._tasks):
            t.cancel()

    def stats_text(self):
        up = datetime.datetime.now() - self.started_at
        h, m, s = up.seconds // 3600, up.seconds % 3600 // 60, up.seconds % 60
        rate = self.sent / max(1, up.total_seconds()) * 60
        return (
            f"📊 <b>СЕССИЯ #{self.short_id}</b>\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"⏱ Работает: <code>{h:02d}:{m:02d}:{s:02d}</code>\n"
            f"📨 Отправлено: <b>{self.sent}</b>\n"
            f"⚡ Скорость: <b>{rate:.1f}/мин</b>\n"
            f"🛡 FloodWait: {self.pacer.flood_count}\n"
            f"⚙️ Задержка: {self.pacer.base_min // 1000}–{self.pacer.base_max // 1000}с\n"
        )

# =====================================================================
# 5. UI
# =====================================================================
RULES_TEXT = (
    "<b>📜 ПРАВИЛА ПОЛЬЗОВАНИЯ</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n\n"
    "<b>1. Запрещено:</b>\n"
    "• Массовая рассылка без согласия получателей\n"
    "• Спам, флуд, мошенничество\n"
    "• Использование бота для незаконных целей\n"
    "• Передача доступа третьим лицам\n\n"
    "<b>2. Ответственность:</b>\n"
    "• Администрация не несёт ответственности за блокировки аккаунтов Telegram\n"
    "• Все действия вы совершаете на свой страх и риск\n"
    "• При нарушении правил доступ будет заблокирован\n\n"
    "<b>3. Оплата:</b>\n"
    f"• Через {SUPPORT_CONTACT}\n"
    "• Возврат средств не предусмотрен\n\n"
    "<b>4. Конфиденциальность:</b>\n"
    "• Сессии хранятся локально, не передаются третьим лицам\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "Нажимая «✅ Принимаю», вы соглашаетесь с правилами."
)


def main_menu(is_admin):
    menu = [
        [Button.inline("👤 Аккаунт", b"nav_account"),
         Button.inline("📝 Шаблоны", b"nav_templates")],
        [Button.inline("⚙️ Задержка", b"nav_delay"),
         Button.inline("🚀 Запуск", b"nav_spam")],
        [Button.inline("🛑 Остановить", b"nav_stop"),
         Button.inline("📊 Статистика", b"nav_stats")],
        [Button.inline("💎 Подписка", b"nav_sub"),
         Button.inline("🎁 Рефералка", b"nav_ref")],
        [Button.inline("🛒 Купить аккаунт", b"nav_buy_acc"),
         Button.inline("ℹ️ Помощь", b"nav_help")],
    ]
    if is_admin:
        menu.append([Button.inline("👑 Админ-панель", b"nav_admin")])
    return menu


def main_text(uid, p):
    plan = (p or {}).get("plan", "FREE")
    limits = plan_limits(plan)
    status = "✅ Подключён" if p and p["session"] else "❌ Не подключён"
    tpl = len(p["templates"]) if p else 0
    mind = int((p["min_delay"] if p else 50000) / 1000)
    maxd = int((p["max_delay"] if p else 60000) / 1000)
    crown = "👑 " if uid in SUPER_ADMINS else ""
    balance = (p or {}).get("balance", 0) or 0
    return (
        f"<b>⚡ TITAN Enterprise</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>ID:</b> <code>{uid}</code> {crown}\n"
        f"🏅 <b>Статус:</b> {limits['badge']} {limits['title']}\n"
        f"📱 <b>Аккаунт:</b> {status}\n"
        f"📝 <b>Шаблонов:</b> {tpl}/{limits['templates']}\n"
        f"⚙️ <b>Задержка:</b> {mind}–{maxd} сек\n"
        f"🎯 <b>Лимит целей:</b> {limits['max_targets']}\n"
        f"💰 <b>Баланс:</b> {balance} ₽\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Выберите действие:"
    )


def sub_text(plan):
    return (
        f"💎 <b>Тарифные планы</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚪ <b>FREE</b> — 0 ₽\n"
        f"• 10 шаблонов, 1 цель, 50–60с\n\n"
        f"💎 <b>PRO</b> — <b>490 ₽/мес</b>\n"
        f"• 100 шаблонов, 2 цели, 30–60с\n"
        f"• Живая статистика\n"
        f"• Умная FloodWait-защита\n"
        f"• Экспорт истории\n"
        f"• Поддержка 24ч\n\n"
        f"👑 <b>ULTIMATE</b> — <b>1 490 ₽/мес</b>\n"
        f"• ∞ шаблонов, 4 цели, 1–60с\n"
        f"• A/B-тесты\n"
        f"• Расписание\n"
        f"• Персональный менеджер\n\n"
        f"🛒 <b>Доп. аккаунт (слот)</b> — 300 ₽/мес\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Твой тариф: <b>{plan}</b>\n"
        f"📩 Для покупки: {SUPPORT_CONTACT}\n\n"
        f"🎁 <b>Скидка 30% на первый месяц</b> — спроси у {SUPPORT_CONTACT}"
    )


def help_text():
    return (
        f"ℹ️ <b>Помощь</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>🔑 Вход в аккаунт</b>\n"
        f"Используй <b>отдельный</b> аккаунт для сессии.\n"
        f"Если код не приходит → QR-код или звонок.\n\n"
        f"<b>📝 Шаблоны</b>\n"
        f"Каждая строка = отдельный шаблон.\n"
        f"Отправляется <b>ровно как ты ввёл</b>.\n\n"
        f"<b>🎁 Реферальная программа</b>\n"
        f"• Пригласи друга → получи <b>{REFERRAL_PERCENT}%</b> с его платежей\n"
        f"• Баланс → на подписку или вывод от 500 ₽\n\n"
        f"<b>💎 Тарифы</b>\n"
        f"• FREE — 0 ₽\n"
        f"• PRO — 490 ₽/мес\n"
        f"• ULTIMATE — 1 490 ₽/мес\n"
        f"• Слот — 300 ₽/мес\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📩 Поддержка: {SUPPORT_CONTACT}"
    )

# =====================================================================
# 6. ГЛОБАЛЬНОЕ СОСТОЯНИЕ
# =====================================================================
active_clients = {}
user_steps = {}
spam_engines = {}
system_metrics = {
    "total_messages_sent": 0,
    "total_flood_wait_events": 0,
    "total_errors_caught": 0,
}

bot = TelegramClient("bot_master", API_ID, API_HASH)
repo = None  # инициализируется в main()


def is_super(uid):
    return int(uid) in SUPER_ADMINS


async def resolve_client(uid):
    c = active_clients.get(uid)
    if c is not None:
        try:
            if await c.is_user_authorized():
                return c
        except Exception:
            pass
        active_clients.pop(uid, None)
        try: await c.disconnect()
        except Exception: pass

    p = await repo.get(uid)
    if not p or not p["session"]:
        return None
    c = TelegramClient(StringSession(p["session"]), API_ID, API_HASH)
    await c.connect()
    if not await c.is_user_authorized():
        await c.disconnect()
        return None
    active_clients[uid] = c
    return c

# =====================================================================
# 7. ХЕНДЛЕРЫ БОТА
# =====================================================================
@bot.on(events.NewMessage(pattern="/start"))
async def cmd_start(e):
    uid = e.sender_id

    parts = e.text.split()
    if len(parts) == 2 and parts[1].startswith("ref_"):
        try:
            referrer = int(parts[1][4:])
            if referrer != uid and not is_super(uid):
                existing = await repo.get(uid)
                if not existing or not existing.get("referred_by"):
                    await repo.save(uid, referred_by=referrer)
                    try:
                        await bot.send_message(
                            referrer,
                            "🎉 <b>Новый реферал!</b>\n"
                            f"💰 Ты получишь {REFERRAL_PERCENT}% с его платежей.",
                            parse_mode="html",
                        )
                    except Exception:
                        pass
        except ValueError:
            pass

    p = await repo.get(uid)

    if is_super(uid):
        if not p or p["plan"] != "ULTIMATE" or p["is_admin"] != 1:
            await repo.save(uid, is_admin=1, is_approved=1,
                            plan="ULTIMATE", accepted_rules=1)
            p = await repo.get(uid)
            log.info("Создатель: %s", uid)

    is_admin = bool(p and p["is_admin"] == 1)
    is_approved = bool(p and p["is_approved"] == 1)

    if not is_admin and not is_approved:
        await e.reply(
            f"⛔ <b>Доступ закрыт</b>\n\n"
            f"Ваш ID: <code>{uid}</code>\n"
            f"Передайте его администратору.\n\n"
            f"Поддержка: {SUPPORT_CONTACT}",
            parse_mode="html",
        )
        return

    if not p or p.get("accepted_rules") != 1:
        await e.reply(
            RULES_TEXT,
            buttons=[
                [Button.inline("✅ Принимаю", b"accept_rules")],
                [Button.inline("❌ Отказываюсь", b"decline_rules")],
            ],
            parse_mode="html",
        )
        return

    user_steps[uid] = "IDLE"
    await e.reply(main_text(uid, p), buttons=main_menu(is_admin), parse_mode="html")


@bot.on(events.CallbackQuery)
async def cb_handler(e):
    uid = e.sender_id
    action = e.data.decode("utf-8")
    p = await repo.get(uid) or {}
    is_admin = p.get("is_admin") == 1 or is_super(uid)
    is_approved = p.get("is_approved") == 1 or is_super(uid)

    if not is_admin and not is_approved:
        return await e.answer("⛔ Доступ закрыт", alert=True)

    if not p.get("accepted_rules") and action not in ("accept_rules", "decline_rules"):
        return await e.answer("⚠️ Сначала примите правила", alert=True)

    back = [[Button.inline("🔙 Назад", b"nav_home")]]

    if action == "accept_rules":
        await repo.save(uid, accepted_rules=1)
        await e.answer("✅ Принято")
        p = await repo.get(uid)
        await e.edit(main_text(uid, p), buttons=main_menu(is_admin), parse_mode="html")

    elif action == "decline_rules":
        await e.edit(
            f"❌ Вы отказались от правил.\n\n"
            f"Напишите /start, если передумаете.\n"
            f"Поддержка: {SUPPORT_CONTACT}",
            parse_mode="html",
        )

    elif action == "nav_home":
        user_steps[uid] = "IDLE"
        p = await repo.get(uid)
        await e.edit(main_text(uid, p), buttons=main_menu(is_admin), parse_mode="html")

    elif action == "nav_account":
        p = await repo.get(uid) or {}
        if not p.get("session"):
            user_steps[uid] = "ST_PHONE"
            await e.edit(
                "📱 <b>Авторизация</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Отправьте номер: <code>+79991234567</code>\n\n"
                "💡 Если код не приходит — войдите по QR.",
                buttons=[[Button.inline("🔙 Назад", b"nav_home")]],
                parse_mode="html",
            )
        else:
            await e.edit("✅ <b>Аккаунт подключён</b>",
                         buttons=back, parse_mode="html")

    elif action == "nav_templates":
        tpls = p.get("templates", [])
        limits = plan_limits(p.get("plan", "FREE"))
        preview = "\n".join(f"• <code>{t[:60]}</code>" for t in tpls[:10]) or "<i>Пусто</i>"
        if len(tpls) > 10:
            preview += f"\n<i>...и ещё {len(tpls) - 10}</i>"
        await e.edit(
            f"📝 <b>Шаблоны ({len(tpls)}/{limits['templates']})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{preview}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Отправь текст — <b>каждая строка = шаблон</b>.\n"
            f"Отправляется ровно как ты ввёл.",
            buttons=[
                [Button.inline("🗑 Очистить все", b"nav_tpl_clear")],
                [Button.inline("🔙 Назад", b"nav_home")],
            ],
            parse_mode="html",
        )

    elif action == "nav_tpl_clear":
        await repo.save(uid, templates=[])
        await e.answer("✅ Очищено")
        await e.edit("📝 <b>Шаблоны (0)</b>", buttons=back, parse_mode="html")

    elif action == "nav_delay":
        user_steps[uid] = "ST_DELAY"
        limits = plan_limits(p.get("plan", "FREE"))
        mind = int(p.get("min_delay", 50000) / 1000)
        maxd = int(p.get("max_delay", 60000) / 1000)
        await e.edit(
            f"⚙️ <b>Задержка</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Текущая: <b>{mind}–{maxd} сек</b>\n"
            f"Диапазон тарифа <b>{p.get('plan','FREE')}</b>: "
            f"{limits['min_delay']}–{limits['max_delay']} сек\n\n"
            f"Формат: <code>{limits['min_delay']}-{limits['max_delay']}</code>",
            buttons=back, parse_mode="html",
        )

    elif action == "nav_spam":
        if not p.get("session"):
            return await e.answer("❌ Нет сессии", alert=True)
        if not p.get("templates"):
            return await e.answer("❌ Нет шаблонов", alert=True)
        limits = plan_limits(p.get("plan", "FREE"))
        user_steps[uid] = "ST_TARGET"
        await e.edit(
            f"🚀 <b>Запуск (до {limits['max_targets']} целей)</b>\n\n"
            f"Список целей — <b>каждая с новой строки</b>:\n"
            f"<code>@chat1\n@chat2\nhttps://t.me/chat3\n123456789</code>",
            buttons=back, parse_mode="html",
        )

    elif action == "nav_stop":
        if uid in spam_engines:
            spam_engines[uid].stop()
            del spam_engines[uid]
            await e.edit("🛑 <b>Остановлено</b>", buttons=back, parse_mode="html")
        else:
            await e.answer("⚠️ Активных рассылок нет", alert=True)

    elif action == "nav_stats":
        limits = plan_limits(p.get("plan", "FREE"))
        if not limits["stats"]:
            return await e.answer("💎 Доступно на PRO и выше", alert=True)
        if uid in spam_engines:
            await e.edit(
                spam_engines[uid].stats_text(),
                buttons=[
                    [Button.inline("🔄 Обновить", b"nav_stats"),
                     Button.inline("🛑 Стоп", b"nav_stop")],
                    [Button.inline("🔙 Назад", b"nav_home")],
                ],
                parse_mode="html",
            )
        else:
            await e.edit(
                "📊 <b>Статистика</b>\n\n"
                "<i>Нет активной сессии.</i>\n"
                "Запусти рассылку → здесь будет live-статистика.",
                buttons=back, parse_mode="html",
            )

    elif action == "nav_sub":
        await e.edit(sub_text(p.get("plan", "FREE")), buttons=back, parse_mode="html")

    elif action == "nav_ref":
        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start=ref_{uid}"
        await e.edit(
            f"🎁 <b>Реферальная программа</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Приглашай друзей → получай <b>{REFERRAL_PERCENT}%</b> "
            f"с их платежей на баланс.\n\n"
            f"💰 Твой баланс: <b>{p.get('balance', 0)} ₽</b>\n\n"
            f"🔗 Твоя ссылка:\n<code>{link}</code>\n\n"
            f"💡 Баланс → на подписку или вывод от 500 ₽.\n"
            f"По выводу: {SUPPORT_CONTACT}",
            buttons=back, parse_mode="html",
        )

    elif action == "nav_buy_acc":
        await e.edit(
            f"🛒 <b>Покупка аккаунта</b>\n\n"
            f"Готовые аккаунты — вручную, детали в ЛС.\n\n"
            f"📩 {SUPPORT_CONTACT}",
            buttons=back, parse_mode="html",
        )

    elif action == "nav_help":
        await e.edit(help_text(), buttons=back, parse_mode="html")

    elif action == "nav_admin" and is_admin:
        await e.edit(
            "👑 <b>Админ-панель</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<code>.approve &lt;ID&gt;</code> — доступ\n"
            "<code>.revoke &lt;ID&gt;</code> — забрать\n"
            "<code>.sub &lt;ID&gt; &lt;FREE|PRO|ULTIMATE&gt;</code>\n"
            "<code>.balance &lt;ID&gt; &lt;сумма&gt;</code>\n"
            "<code>.info &lt;ID&gt;</code> — инфа о юзере\n"
            "<code>.id</code> — свой ID",
            buttons=back, parse_mode="html",
        )

    elif action == "qr_start":
        await e.edit("🔄 Генерирую QR...")
        try:
            c = TelegramClient(StringSession(), API_ID, API_HASH)
            await c.connect()
            qr = await c.qr_login()
            active_clients[f"qr_{uid}"] = {"client": c, "qr": qr}
            user_steps[uid] = "ST_QR"
            qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={urllib.parse.quote(qr.url)}"
            await e.edit(
                "📷 <b>Вход по QR</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "1️⃣ Telegram → Настройки → Устройства\n"
                "2️⃣ Подключить устройство\n"
                "3️⃣ Сканируй QR\n\n"
                f"🔗 <a href='{qr_url}'>Открыть QR</a>\n\n"
                "⏳ 120 сек...",
                buttons=[[Button.inline("🔙 Отмена", b"qr_cancel")]],
                parse_mode="html",
            )
            try:
                await asyncio.wait_for(qr.wait(), timeout=120)
                await repo.save(uid, session=c.session.save())
                active_clients[uid] = c
                active_clients.pop(f"qr_{uid}", None)
                user_steps[uid] = "IDLE"
                await e.edit("✅ <b>Успешно авторизован!</b>\n\n/start", parse_mode="html")
            except asyncio.TimeoutError:
                active_clients.pop(f"qr_{uid}", None)
                user_steps[uid] = "IDLE"
                await e.edit("⏱ QR истёк. /start")
        except Exception as ex:
            user_steps[uid] = "IDLE"
            await e.edit(f"❌ Ошибка QR: <code>{str(ex)[:200]}</code>", parse_mode="html")

    elif action == "qr_cancel":
        b = active_clients.pop(f"qr_{uid}", None)
        if b:
            try: await b["client"].disconnect()
            except Exception: pass
        user_steps[uid] = "IDLE"
        await e.edit("❌ Отменено", buttons=back)

    elif action == "resend_call":
        b = active_clients.get(f"temp_{uid}")
        if not b:
            return await e.answer("❌ Нет активной попытки", alert=True)
        try:
            await b["client"].send_code_request(b["phone"], force_sms=False)
            await e.answer("📞 Повторный запрос", alert=True)
        except Exception as ex:
            await e.answer(f"❌ {str(ex)[:100]}", alert=True)


@bot.on(events.NewMessage)
async def text_handler(e):
    uid = e.sender_id
    text = e.text.strip()
    if text.startswith("/"):
        return

    p = await repo.get(uid) or {}
    is_admin = p.get("is_admin") == 1 or is_super(uid)
    is_approved = p.get("is_approved") == 1 or is_super(uid)

    if is_admin:
        try:
            if text.startswith(".approve"):
                t_id = int(text.split()[1])
                await repo.save(t_id, is_approved=1)
                await e.reply(f"✅ Доступ: <code>{t_id}</code>", parse_mode="html")
                return
            if text.startswith(".revoke"):
                t_id = int(text.split()[1])
                if is_super(t_id):
                    await e.reply("⚠️ Нельзя забрать у создателя")
                    return
                await repo.save(t_id, is_approved=0)
                await e.reply(f"🚫 Забран: <code>{t_id}</code>", parse_mode="html")
                return
            if text.startswith(".sub"):
                parts = text.split()
                t_id, plan = int(parts[1]), parts[2].upper()
                if plan not in ("FREE", "PRO", "ULTIMATE"):
                    await e.reply("❌ FREE / PRO / ULTIMATE")
                    return
                await repo.save(t_id, plan=plan, is_approved=1)
                try:
                    await bot.send_message(
                        t_id,
                        f"🎉 <b>Тариф обновлён!</b>\n\nТвой новый план: <b>{plan}</b>",
                        parse_mode="html",
                    )
                except Exception:
                    pass
                await e.reply(f"✅ <b>{plan}</b> → <code>{t_id}</code>", parse_mode="html")
                return
            if text.startswith(".balance"):
                parts = text.split()
                t_id, amount = int(parts[1]), int(parts[2])
                await repo.add_balance(t_id, amount)
                await e.reply(f"💰 +{amount} ₽ → <code>{t_id}</code>", parse_mode="html")
                return
            if text.startswith(".info"):
                t_id = int(text.split()[1])
                tp = await repo.get(t_id)
                if not tp:
                    await e.reply("❌ Не найден")
                    return
                await e.reply(
                    f"👤 <b>Юзер</b> <code>{t_id}</code>\n"
                    f"Тариф: <b>{tp['plan']}</b>\n"
                    f"Admin: {tp['is_admin']}, Approved: {tp['is_approved']}\n"
                    f"Шаблонов: {len(tp['templates'])}\n"
                    f"Баланс: {tp['balance']} ₽\n"
                    f"Реферер: {tp['referred_by'] or '—'}",
                    parse_mode="html",
                )
                return
            if text == ".id":
                await e.reply(f"Ваш ID: <code>{uid}</code>", parse_mode="html")
                return
        except (IndexError, ValueError):
            await e.reply("❌ Неверный формат команды")
            return

    if not is_admin and not is_approved:
        return
    if not p.get("accepted_rules"):
        return

    step = user_steps.get(uid, "IDLE")

    if step == "ST_PHONE":
        await e.reply("⏳ Запрашиваю код...")
        c = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await c.connect()
            req = await c.send_code_request(text)
            active_clients[f"temp_{uid}"] = {
                "client": c, "phone": text, "hash": req.phone_code_hash,
            }
            user_steps[uid] = "ST_CODE"
            await e.reply(
                "📩 <b>Код отправлен</b>\n\nВведите код:",
                buttons=[
                    [Button.inline("📷 Войти по QR", b"qr_start")],
                    [Button.inline("📞 Запросить звонок", b"resend_call")],
                    [Button.inline("🔙 Отмена", b"nav_home")],
                ],
                parse_mode="html",
            )
        except Exception as ex:
            await e.reply(f"❌ <code>{str(ex)[:200]}</code>", parse_mode="html")
            user_steps[uid] = "IDLE"

    elif step == "ST_CODE":
        b = active_clients.get(f"temp_{uid}")
        if not b:
            return
        try:
            await b["client"].sign_in(phone=b["phone"], code=text, phone_code_hash=b["hash"])
            await repo.save(uid, session=b["client"].session.save())
            active_clients[uid] = b["client"]
            active_clients.pop(f"temp_{uid}", None)
            user_steps[uid] = "IDLE"
            await e.reply("✅ Сессия создана! /start")
        except SessionPasswordNeededError:
            user_steps[uid] = "ST_2FA"
            await e.reply("🔐 Введите пароль 2FA:")
        except PhoneCodeInvalidError:
            await e.reply("❌ Неверный код")
        except PhoneCodeExpiredError:
            user_steps[uid] = "IDLE"
            await e.reply("❌ Код истёк, /start")
        except Exception as ex:
            user_steps[uid] = "IDLE"
            await e.reply(f"❌ <code>{str(ex)[:200]}</code>", parse_mode="html")

    elif step == "ST_2FA":
        b = active_clients.get(f"temp_{uid}")
        if not b:
            return
        try:
            await b["client"].sign_in(password=text)
            await repo.save(uid, session=b["client"].session.save())
            active_clients[uid] = b["client"]
            active_clients.pop(f"temp_{uid}", None)
            user_steps[uid] = "IDLE"
            await e.reply("✅ 2FA пройдена! /start")
        except Exception as ex:
            await e.reply(f"❌ <code>{str(ex)[:200]}</code>", parse_mode="html")

    elif step == "ST_TARGET":
        user_steps[uid] = "IDLE"
        limits = plan_limits(p.get("plan", "FREE"))

        raw = [line.strip() for line in text.split("\n") if line.strip()]
        targets, bad = [], []
        for r in raw:
            n = normalize_target(r)
            if n: targets.append(n)
            else: bad.append(r)

        if not targets:
            await e.reply("❌ Не удалось распознать цели")
            return
        if len(targets) > limits["max_targets"]:
            await e.reply(
                f"⚠️ Тариф <b>{p.get('plan','FREE')}</b> разрешает "
                f"<b>{limits['max_targets']}</b> целей. У тебя: {len(targets)}.\n\n"
                f"Повысь тариф: /start → 💎",
                parse_mode="html",
            )
            return

        if uid in spam_engines:
            spam_engines[uid].stop()
            await asyncio.sleep(0.5)

        c = await resolve_client(uid)
        if not c:
            await e.reply("❌ Сессия потеряна, /start → Аккаунт")
            return

        eng = SpamEngine(c, uid, DB_FILE, p["min_delay"], p["max_delay"], system_metrics)
        spam_engines[uid] = eng
        asyncio.create_task(eng.start(targets, p["templates"]))

        msg = "🚀 <b>Запущено</b>\n\n<b>Цели:</b>\n" + "\n".join(
            f"• <code>{t}</code>" for t in targets
        )
        if bad:
            msg += f"\n\n⚠️ Не распознано: {len(bad)}"
        msg += f"\n\nЗадержка: {int(p['min_delay']/1000)}–{int(p['max_delay']/1000)}с"
        await e.reply(msg, parse_mode="html")

    elif step == "ST_DELAY":
        limits = plan_limits(p.get("plan", "FREE"))
        try:
            low, high = map(int, text.split("-"))
            if low < limits["min_delay"]:
                await e.reply(
                    f"⚠️ Минимум {limits['min_delay']}с для <b>{p.get('plan')}</b>",
                    parse_mode="html",
                ); return
            if high > limits["max_delay"]:
                await e.reply(f"⚠️ Максимум {limits['max_delay']}с"); return
            if high < low:
                await e.reply("⚠️ Max < Min"); return
            await repo.save(uid, min_d=low * 1000, max_d=high * 1000)
            user_steps[uid] = "IDLE"
            await e.reply(
                f"✅ Задержка: <b>{low}–{high}с</b>",
                buttons=main_menu(is_admin), parse_mode="html",
            )
        except (ValueError, AttributeError):
            await e.reply(
                f"❌ Формат: <code>{limits['min_delay']}-{limits['max_delay']}</code>",
                parse_mode="html",
            )

    elif step == "IDLE":
        limits = plan_limits(p.get("plan", "FREE"))
        new_lines = [line for line in text.split("\n") if line.strip()]
        cur = list(p.get("templates", []))
        added, skipped = 0, 0
        for line in new_lines:
            # ⚠️ Шаблон сохраняется В ТОЧНОСТИ как ввёл юзер
            if line in cur:
                continue
            if len(cur) >= limits["templates"]:
                skipped += 1
                continue
            cur.append(line)
            added += 1

        if added > 0:
            await repo.save(uid, templates=cur)
            msg = f"📝 Добавлено: <b>{added}</b>. Всего: <b>{len(cur)}/{limits['templates']}</b>"
            if skipped:
                msg += f"\n⚠️ Пропущено {skipped} — лимит тарифа."
            await e.reply(msg, parse_mode="html")
        elif skipped:
            await e.reply(
                f"⚠️ Лимит шаблонов для <b>{p.get('plan')}</b> — {limits['templates']}\n"
                f"Повысь тариф: /start → 💎",
                parse_mode="html",
            )
        else:
            await e.reply("⚠️ Эти шаблоны уже есть")

# =====================================================================
# 8. WEB DASHBOARD
# =====================================================================
DASHBOARD_HTML = r"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TITAN Enterprise</title>
<style>
:root{--bg:#0a0e14;--card:#141a22;--line:#1f2933;--txt:#e8eef5;--muted:#7a8899;--acc:#4ea8de;--ok:#3ddc97;--warn:#ffb703;--err:#ef476f}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.wrap{width:100%;max-width:780px}
.header{text-align:center;margin-bottom:24px}
.logo{font-size:34px;font-weight:800;letter-spacing:-1px;background:linear-gradient(135deg,#4ea8de,#3ddc97);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.sub{color:var(--muted);font-size:13px;margin-top:4px;letter-spacing:1px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;transition:transform .15s,border-color .15s}
.card:hover{transform:translateY(-2px);border-color:var(--acc)}
.card .label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:10px}
.card .value{font-size:32px;font-weight:700;font-variant-numeric:tabular-nums}
.card.ok .value{color:var(--ok)}.card.warn .value{color:var(--warn)}
.card.err .value{color:var(--err)}.card.acc .value{color:var(--acc)}
.footer{text-align:center;color:var(--muted);font-size:12px;margin-top:20px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);margin-right:6px;animation:pulse 1.6s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
</style></head><body><div class="wrap">
<div class="header"><div class="logo">TITAN Enterprise</div><div class="sub">CONTROL PANEL</div></div>
<div class="grid">
<div class="card acc"><div class="label">Отправлено</div><div class="value" id="m">0</div></div>
<div class="card ok"><div class="label">Сессий</div><div class="value" id="s">0</div></div>
<div class="card err"><div class="label">Ошибок</div><div class="value" id="e">0</div></div>
<div class="card warn"><div class="label">FloodWait</div><div class="value" id="f">0</div></div>
</div>
<div class="footer"><span class="dot"></span>Uptime: <span id="u">—</span></div>
</div><script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
async function refresh(){
  try{
    const r = await fetch('/api/stats?token=' + encodeURIComponent(TOKEN));
    if(!r.ok) return;
    const d = await r.json();
    for(const [id,key] of [['m','messages'],['s','sessions'],['e','errors'],['f','flood'],['u','uptime']]){
      document.getElementById(id).textContent = d[key];
    }
  }catch(e){}
}
refresh(); setInterval(refresh, 3000);
</script></body></html>"""

app = Flask("titan")
app.config["JSON_AS_ASCII"] = False
START_TIME = datetime.datetime.now()

def _check_auth():
    if not DASHBOARD_TOKEN:
        abort(503, "DASHBOARD_TOKEN не задан")
    got = request.headers.get("X-Token") or request.args.get("token", "")
    if not hmac.compare_digest(got, DASHBOARD_TOKEN):
        abort(401)

@app.route("/healthz")
def health():
    return jsonify(status="OK"), 200

@app.route("/api/stats")
def stats_api():
    _check_auth()
    return jsonify(
        messages=system_metrics["total_messages_sent"],
        sessions=len([k for k in active_clients if isinstance(k, int)]),
        errors=system_metrics["total_errors_caught"],
        flood=system_metrics["total_flood_wait_events"],
        uptime=str(datetime.datetime.now() - START_TIME).split(".")[0],
    )

@app.route("/")
def index():
    _check_auth()
    return Response(DASHBOARD_HTML, mimetype="text/html")

def run_web():
    try:
        from waitress import serve
        log.info("Waitress на :%s", PORT)
        serve(app, host="0.0.0.0", port=PORT, threads=4)
    except ImportError:
        log.warning("waitress не установлен — Flask dev")
        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# =====================================================================
# 9. ЗАПУСК
# =====================================================================
async def main():
    global repo

    db = TitanDB(DB_FILE, BACKUP_DIR)
    await db.bootstrap()
    await db.backup()
    asyncio.create_task(db.backup_loop(6))

    repo = UserRepo(db)

    # Регистрируем супер-админов с максимальными правами
    for admin_id in SUPER_ADMINS:
        await repo.save(admin_id, is_admin=1, is_approved=1,
                        plan="ULTIMATE", accepted_rules=1)
        log.info("SUPER_ADMIN: %s", admin_id)

    threading.Thread(target=run_web, daemon=True).start()
    log.info("Web на :%s", PORT)

    await bot.start(bot_token=BOT_TOKEN)
    log.info("Бот запущен")
    try:
        await bot.run_until_disconnected()
    finally:
        for c in list(active_clients.values()):
            if hasattr(c, "disconnect"):
                try: await c.disconnect()
                except Exception: pass


if __name__ == "__main__":
    log.info("=" * 50)
    log.info("  TITAN Enterprise — старт")
    log.info("=" * 50)
    try:
        asyncio.run(main())
    except AccessTokenInvalidError:
        log.critical("Неверный BOT_TOKEN")
    except KeyboardInterrupt:
        log.info("Остановлено")
