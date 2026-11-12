# =====================================================================
# TITAN Enterprise v4.0 — монолит для Render
# Покупка подписки: через владельца @apemiru
# =====================================================================
import os
import sys
import json
import re
import io
import csv
import time
import hmac
import random
import string
import asyncio
import datetime
import threading
import shutil
import logging
import urllib.parse
from collections import defaultdict, deque

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError, SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, ChatWriteForbiddenError, ChatAdminRequiredError,
    UserBannedInChannelError, SlowModeWaitError, PeerFloodError,
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
OWNER_CONTACT = "@apemiru"
SUPPORT_CONTACT = "@apemiru"

DB_FILE = os.getenv("DB_FILE", "titan_database.db")
BACKUP_DIR = os.getenv("BACKUP_DIR", "database_backups")
PORT = int(os.getenv("PORT", "8080"))

MIN_DELAY_LIMIT = 1
MAX_DELAY_LIMIT = 60

if os.path.isdir("/var/data") and not os.getenv("DB_FILE"):
    DB_FILE = "/var/data/titan_database.db"
    BACKUP_DIR = "/var/data/database_backups"

# =====================================================================
# 2. ТАРИФЫ С ФИШКАМИ
# =====================================================================
PLAN_LIMITS = {
    "FREE": {
        "templates": 10, "min_delay": 50, "max_delay": 60,
        "max_targets": 1, "stats": False, "export": False,
        "schedule": False, "ab_test": False, "media": 0,
        "auto_pause": False, "priority_support": False,
        "custom_badge": False, "templates_backup": False,
        "flood_protection": "basic", "daily_limit": 1000,
        "concurrent_sessions": 1, "history_days": 3,
        "blacklist": False, "whitelist": False, "stop_words": False,
        "anti_ban": False, "proxy_support": False, "webhooks": False,
        "delayed_start": False, "random_targets": False,
        "badge": "⚪", "title": "Новичок", "emoji": "🥉",
    },
    "PRO": {
        "templates": 100, "min_delay": 30, "max_delay": 60,
        "max_targets": 2, "stats": True, "export": True,
        "schedule": False, "ab_test": False, "media": 1,
        "auto_pause": True, "priority_support": True,
        "custom_badge": False, "templates_backup": True,
        "flood_protection": "advanced", "daily_limit": 10000,
        "concurrent_sessions": 2, "history_days": 30,
        "blacklist": True, "whitelist": True, "stop_words": True,
        "anti_ban": True, "proxy_support": False, "webhooks": False,
        "delayed_start": True, "random_targets": False,
        "badge": "💎", "title": "PRO-юзер", "emoji": "🥈",
    },
    "ULTIMATE": {
        "templates": 9999, "min_delay": 1, "max_delay": 60,
        "max_targets": 4, "stats": True, "export": True,
        "schedule": True, "ab_test": True, "media": 5,
        "auto_pause": True, "priority_support": True,
        "custom_badge": True, "templates_backup": True,
        "flood_protection": "maximum", "daily_limit": 999999,
        "concurrent_sessions": 4, "history_days": 365,
        "blacklist": True, "whitelist": True, "stop_words": True,
        "anti_ban": True, "proxy_support": True, "webhooks": True,
        "delayed_start": True, "random_targets": True,
        "badge": "👑", "title": "ULTIMATE", "emoji": "🥇",
    },
}

PRICES = {"FREE": 0, "PRO": 490, "ULTIMATE": 1490}

PLAN_FEATURES = {
    "FREE": [
        "10 шаблонов",
        "1 цель одновременно",
        "Задержка 50–60 сек",
        "Базовая защита от FloodWait",
        "История 3 дня",
        "Лимит 1000 сообщений/день",
        "Одна сессия",
    ],
    "PRO": [
        "100 шаблонов",
        "2 цели одновременно",
        "Задержка 30–60 сек",
        "📊 Live-статистика",
        "🛡 Продвинутая FloodWait-защита",
        "📤 Экспорт истории в CSV",
        "💾 Бэкап шаблонов",
        "🖼 Медиа-вложения (1 шт)",
        "⏸ Автопауза при ошибках",
        "🎧 Приоритетная поддержка 24ч",
        "📜 История 30 дней",
        "🚫 Чёрный список целей",
        "✅ Белый список целей",
        "🛑 Стоп-слова",
        "🕐 Отложенный старт",
        "Лимит 10 000 сообщений/день",
    ],
    "ULTIMATE": [
        "∞ шаблонов",
        "4 цели одновременно",
        "Задержка 1–60 сек",
        "🎯 Всё из PRO +",
        "🧪 A/B-тесты шаблонов",
        "🕐 Расписание работы",
        "🖼 Медиа-вложения (5 шт)",
        "🏅 Кастомный бейдж",
        "🛡 Максимальная защита",
        "🎲 Рандомный выбор целей",
        "🔌 Прокси-поддержка",
        "🔔 Webhooks",
        "👤 Персональный менеджер",
        "📜 История 1 год",
        "♾ Без дневного лимита",
        "🖥 До 4 сессий одновременно",
    ],
}

def plan_limits(plan: str) -> dict:
    return PLAN_LIMITS.get(plan, PLAN_LIMITS["FREE"])

# =====================================================================
# 3. ЛОГИ
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("TITAN")

# =====================================================================
# 4. БД
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
    schedule TEXT,
    custom_badge TEXT,
    notes TEXT,
    blacklist TEXT DEFAULT '[]',
    whitelist TEXT DEFAULT '[]',
    stop_words TEXT DEFAULT '[]',
    proxies TEXT DEFAULT '[]',
    webhook_url TEXT,
    timezone_offset INTEGER DEFAULT 3
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
CREATE TABLE IF NOT EXISTS payments_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    plan TEXT,
    amount INTEGER,
    method TEXT DEFAULT 'manual',
    admin_id INTEGER,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER,
    action TEXT,
    target_id INTEGER,
    details TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT,
    user_id INTEGER,
    messages_sent INTEGER DEFAULT 0,
    PRIMARY KEY (date, user_id)
);
CREATE TABLE IF NOT EXISTS ab_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    template TEXT,
    sent INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_hist_user ON spam_history(user_id, started_at);
CREATE INDEX IF NOT EXISTS idx_daily ON daily_stats(date, user_id);
"""

MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN balance INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN referred_by INTEGER",
    "ALTER TABLE users ADD COLUMN schedule TEXT",
    "ALTER TABLE users ADD COLUMN custom_badge TEXT",
    "ALTER TABLE users ADD COLUMN notes TEXT",
    "ALTER TABLE users ADD COLUMN blacklist TEXT DEFAULT '[]'",
    "ALTER TABLE users ADD COLUMN whitelist TEXT DEFAULT '[]'",
    "ALTER TABLE users ADD COLUMN stop_words TEXT DEFAULT '[]'",
    "ALTER TABLE users ADD COLUMN proxies TEXT DEFAULT '[]'",
    "ALTER TABLE users ADD COLUMN webhook_url TEXT",
    "ALTER TABLE users ADD COLUMN timezone_offset INTEGER DEFAULT 3",
]

USER_DEFAULTS = {
    "session": None, "templates": [], "min_delay": 50000, "max_delay": 60000,
    "is_approved": 0, "is_admin": 0, "plan": "FREE", "accepted_rules": 0,
    "balance": 0, "referred_by": None, "schedule": None,
    "custom_badge": None, "notes": None,
    "blacklist": [], "whitelist": [], "stop_words": [],
    "proxies": [], "webhook_url": None, "timezone_offset": 3,
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
                " is_admin, plan, accepted_rules, balance, referred_by,"
                " schedule, custom_badge, notes, blacklist, whitelist, stop_words,"
                " proxies, webhook_url, timezone_offset FROM users WHERE user_id=?",
                (uid,),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        d = dict(USER_DEFAULTS)
        d.update(
            session=row[0], templates=json.loads(row[1] or "[]"),
            min_delay=row[2], max_delay=row[3], is_approved=row[4],
            is_admin=row[5], plan=row[6] or "FREE", accepted_rules=row[7] or 0,
            balance=row[8] or 0, referred_by=row[9],
            schedule=json.loads(row[10]) if row[10] else None,
            custom_badge=row[11], notes=row[12],
            blacklist=json.loads(row[13] or "[]"),
            whitelist=json.loads(row[14] or "[]"),
            stop_words=json.loads(row[15] or "[]"),
            proxies=json.loads(row[16] or "[]"),
            webhook_url=row[17],
            timezone_offset=row[18] or 3,
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
                    balance, referred_by, schedule, custom_badge, notes,
                    blacklist, whitelist, stop_words, proxies, webhook_url, timezone_offset)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    schedule=excluded.schedule,
                    custom_badge=excluded.custom_badge,
                    notes=excluded.notes,
                    blacklist=excluded.blacklist,
                    whitelist=excluded.whitelist,
                    stop_words=excluded.stop_words,
                    proxies=excluded.proxies,
                    webhook_url=excluded.webhook_url,
                    timezone_offset=excluded.timezone_offset""",
                (uid, cur["session"], json.dumps(cur["templates"]),
                 cur["min_delay"], cur["max_delay"], cur["is_approved"],
                 cur["is_admin"], cur["plan"], cur["accepted_rules"],
                 datetime.datetime.now().isoformat(timespec="seconds"),
                 cur["balance"], cur["referred_by"],
                 json.dumps(cur["schedule"]) if cur["schedule"] else None,
                 cur["custom_badge"], cur["notes"],
                 json.dumps(cur["blacklist"]),
                 json.dumps(cur["whitelist"]),
                 json.dumps(cur["stop_words"]),
                 json.dumps(cur["proxies"]),
                 cur["webhook_url"], cur["timezone_offset"]),
            )
            await db.commit()

    async def add_balance(self, uid, amount):
        p = await self.get(uid) or dict(USER_DEFAULTS)
        new_b = (p["balance"] or 0) + amount
        if new_b < 0:
            new_b = 0
        await self.save(uid, balance=new_b)

    async def count_users(self):
        async with self.db.conn() as db:
            async with db.execute("SELECT COUNT(*) FROM users") as cur:
                return (await cur.fetchone())[0]

    async def count_approved(self):
        async with self.db.conn() as db:
            async with db.execute("SELECT COUNT(*) FROM users WHERE is_approved=1") as cur:
                return (await cur.fetchone())[0]

    async def count_by_plan(self):
        async with self.db.conn() as db:
            async with db.execute("SELECT plan, COUNT(*) FROM users GROUP BY plan") as cur:
                return dict(await cur.fetchall())

    async def get_payment_stats(self):
        async with self.db.conn() as db:
            async with db.execute("SELECT SUM(amount), COUNT(*) FROM payments_log") as cur:
                row = await cur.fetchone()
                return {"sum": row[0] or 0, "count": row[1] or 0}

    async def log_payment(self, uid, plan, amount, admin_id, method="manual"):
        async with self.db.conn() as db:
            await db.execute(
                "INSERT INTO payments_log (user_id, plan, amount, method, admin_id, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (uid, plan, amount, method, admin_id,
                 datetime.datetime.now().isoformat(timespec="seconds")),
            )
            await db.commit()

    async def log_audit(self, admin_id, action, target_id, details=""):
        async with self.db.conn() as db:
            await db.execute(
                "INSERT INTO audit_log (admin_id, action, target_id, details, created_at)"
                " VALUES (?,?,?,?,?)",
                (admin_id, action, target_id, details,
                 datetime.datetime.now().isoformat(timespec="seconds")),
            )
            await db.commit()

    async def log_daily(self, uid, count=1):
        today = datetime.date.today().isoformat()
        async with self.db.conn() as db:
            await db.execute(
                """INSERT INTO daily_stats (date, user_id, messages_sent)
                VALUES (?,?,?)
                ON CONFLICT(date, user_id) DO UPDATE SET
                messages_sent = messages_sent + excluded.messages_sent""",
                (today, uid, count),
            )
            await db.commit()

    async def get_daily_count(self, uid):
        today = datetime.date.today().isoformat()
        async with self.db.conn() as db:
            async with db.execute(
                "SELECT messages_sent FROM daily_stats WHERE date=? AND user_id=?",
                (today, uid),
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0

    async def get_all_users(self, limit=10000):
        async with self.db.conn() as db:
            async with db.execute("SELECT user_id FROM users LIMIT ?", (limit,)) as cur:
                return [r[0] for r in await cur.fetchall()]

    async def get_approved_users(self, limit=10000):
        async with self.db.conn() as db:
            async with db.execute(
                "SELECT user_id FROM users WHERE is_approved=1 LIMIT ?", (limit,)
            ) as cur:
                return [r[0] for r in await cur.fetchall()]

    async def log_ab_test(self, uid, template, sent=1, errors=0):
        async with self.db.conn() as db:
            await db.execute(
                "INSERT INTO ab_tests (user_id, template, sent, errors, created_at)"
                " VALUES (?,?,?,?,?)",
                (uid, template, sent, errors,
                 datetime.datetime.now().isoformat(timespec="seconds")),
            )
            await db.commit()

    async def get_ab_stats(self, uid):
        async with self.db.conn() as db:
            async with db.execute(
                "SELECT template, SUM(sent), SUM(errors) FROM ab_tests"
                " WHERE user_id=? GROUP BY template ORDER BY SUM(sent) DESC",
                (uid,),
            ) as cur:
                return await cur.fetchall()

# =====================================================================
# 5. ДВИЖОК
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


def is_valid_schedule(schedule):
    """schedule = {"days": [1,2,3,4,5], "from": "09:00", "to": "23:00"}"""
    if not schedule:
        return True
    now = datetime.datetime.now()
    days = schedule.get("days", list(range(7)))
    if now.weekday() not in days:
        return False
    try:
        fh, fm = map(int, schedule.get("from", "00:00").split(":"))
        th, tm = map(int, schedule.get("to", "23:59").split(":"))
        current = now.hour * 60 + now.minute
        start = fh * 60 + fm
        end = th * 60 + tm
        return start <= current <= end
    except (ValueError, AttributeError):
        return True


class AdaptivePacer:
    def __init__(self, min_ms, max_ms, protection="basic"):
        self.base_min, self.base_max = min_ms, max_ms
        self.penalty = 0
        self.flood_count = 0
        self.protection = protection

    def next_delay(self):
        lo, hi = self.base_min + self.penalty, self.base_max + self.penalty
        return random.randint(lo, hi) / 1000.0

    def on_flood(self, seconds):
        self.flood_count += 1
        factor = {"basic": 0.05, "advanced": 0.08, "maximum": 0.12}.get(self.protection, 0.05)
        self.penalty = min(self.base_max * 2, self.penalty + int(self.base_max * factor))
        return seconds + 5

    def reset_penalty(self):
        self.penalty = 0


class SpamEngine:
    def __init__(self, client, user_id, db_path, min_ms, max_ms, metrics,
                 protection="basic", auto_pause=True, repo=None,
                 schedule=None, blacklist=None, whitelist=None, stop_words=None,
                 random_targets=False, ab_test=False, webhook_url=None):
        self.client = client
        self.user_id = user_id
        self.db_path = db_path
        self.pacer = AdaptivePacer(min_ms, max_ms, protection)
        self._active = asyncio.Event()
        self._active.set()
        self._tasks = set()
        self.started_at = datetime.datetime.now()
        self.sent = 0
        self.errors = 0
        self.short_id = f"{user_id:x}"[-4:]
        self.metrics = metrics
        self.auto_pause = auto_pause
        self.repo = repo
        self.schedule = schedule
        self.blacklist = set(blacklist or [])
        self.whitelist = set(whitelist or [])
        self.stop_words = set(stop_words or [])
        self.random_targets = random_targets
        self.ab_test = ab_test
        self.webhook_url = webhook_url
        self._consecutive_errors = 0
        self._error_threshold = 5
        self._target_cycle = []

    @property
    def is_active(self):
        return self._active.is_set()

    def _pick_target(self, targets):
        if not self.random_targets:
            return targets[0] if targets else None
        return random.choice(targets)

    def _is_allowed(self, target):
        if self.whitelist and target not in self.whitelist:
            return False
        if target in self.blacklist:
            return False
        return True

    def _filter_msg(self, msg):
        for w in self.stop_words:
            if w and w.lower() in msg.lower():
                return None
        return msg

    async def _send_webhook(self, status, target, count):
        if not self.webhook_url:
            return
        try:
            import urllib.request
            data = json.dumps({
                "user_id": self.user_id,
                "target": target,
                "sent": count,
                "status": status,
                "time": datetime.datetime.now().isoformat(timespec="seconds"),
            }).encode("utf-8")
            req = urllib.request.Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

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
            if self.schedule and not is_valid_schedule(self.schedule):
                log.info("[%s] вне расписания, сплю 60с", self.user_id)
                await asyncio.sleep(60)
                continue
            if not self._is_allowed(target):
                log.info("[%s] %s не разрешён", self.user_id, target)
                break
            try:
                raw = random.choice(templates)
                msg = self._filter_msg(raw)
                if msg is None:
                    log.info("[%s] стоп-слово в шаблоне, пропуск", self.user_id)
                    continue
                await self.client.send_message(target, msg)
                counter += 1
                self.sent += 1
                self._consecutive_errors = 0
                self.metrics["total_messages_sent"] += 1
                if self.repo:
                    try:
                        await self.repo.log_daily(self.user_id, 1)
                        if self.ab_test:
                            await self.repo.log_ab_test(self.user_id, raw, sent=1)
                    except Exception:
                        pass
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
                self.errors += 1
                self.metrics["total_errors_caught"] += 1
                log.error("[%s] нет прав в %s", self.user_id, target)
                break
            except ChatAdminRequiredError:
                self.errors += 1
                self.metrics["total_errors_caught"] += 1
                log.error("[%s] нужен админ в %s", self.user_id, target)
                break
            except UserBannedInChannelError:
                self.errors += 1
                self.metrics["total_errors_caught"] += 1
                log.error("[%s] забанен в %s", self.user_id, target)
                break
            except (SlowModeWaitError, PeerFloodError) as e:
                self.errors += 1
                self.metrics["total_errors_caught"] += 1
                self._consecutive_errors += 1
                wait = getattr(e, "seconds", 30) + 5
                log.warning("[%s] %s: жду %ss", self.user_id, type(e).__name__, wait)
                await asyncio.sleep(wait)
                if self.auto_pause and self._consecutive_errors >= self._error_threshold:
                    log.error("[%s] автопауза после %s ошибок", self.user_id, self._consecutive_errors)
                    break
            except Exception as e:
                self.errors += 1
                self.metrics["total_errors_caught"] += 1
                self._consecutive_errors += 1
                log.exception("[%s] сбой %s: %s", self.user_id, target, e)
                if self.auto_pause and self._consecutive_errors >= self._error_threshold:
                    break
        await self._write_history(target, counter, "STOPPED")
        await self._send_webhook("stopped", target, counter)

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
            f"❌ Ошибок: <b>{self.errors}</b>\n"
            f"⚡ Скорость: <b>{rate:.1f}/мин</b>\n"
            f"🛡 FloodWait: {self.pacer.flood_count}\n"
            f"⚙️ Задержка: {self.pacer.base_min // 1000}–{self.pacer.base_max // 1000}с"
            + (f" (+{self.pacer.penalty // 1000}с)" if self.pacer.penalty else "")
        )
        # =====================================================================
# 6. UI
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
    "• Администрация не несёт ответственности за блокировки аккаунтов\n"
    "• Все действия вы совершаете на свой страх и риск\n"
    "• При нарушении правил доступ будет заблокирован\n\n"
    "<b>3. Оплата:</b>\n"
    f"• Через владельца {OWNER_CONTACT}\n"
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
         Button.inline("🛒 Купить", b"nav_buy")],
        [Button.inline("🏅 Мой профиль", b"nav_profile"),
         Button.inline("📜 История", b"nav_history")],
        [Button.inline("🚫 Чёрный список", b"nav_blacklist"),
         Button.inline("✅ Белый список", b"nav_whitelist")],
        [Button.inline("🛑 Стоп-слова", b"nav_stop_words"),
         Button.inline("🧪 A/B-тесты", b"nav_ab")],
        [Button.inline("🕐 Расписание", b"nav_schedule"),
         Button.inline("🔔 Webhook", b"nav_webhook")],
        [Button.inline("ℹ️ Помощь", b"nav_help")],
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
    custom = (p or {}).get("custom_badge")
    badge = custom if custom else limits["badge"]
    max_t = limits['templates'] if limits['templates'] < 9999 else '∞'
    day_l = limits['daily_limit'] if limits['daily_limit'] < 999999 else '∞'
    return (
        f"<b>⚡ TITAN Enterprise</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>ID:</b> <code>{uid}</code> {crown}\n"
        f"🏅 <b>Статус:</b> {badge} {limits['title']}\n"
        f"📱 <b>Аккаунт:</b> {status}\n"
        f"📝 <b>Шаблонов:</b> {tpl}/{max_t}\n"
        f"⚙️ <b>Задержка:</b> {mind}–{maxd} сек\n"
        f"🎯 <b>Целей одновременно:</b> {limits['max_targets']}\n"
        f"📈 <b>Лимит:</b> {day_l} сообщ/день\n"
        f"💰 <b>Баланс:</b> {balance} ₽\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Выберите действие:"
    )


def sub_text(current_plan):
    lines = ["💎 <b>Тарифные планы</b>", "━━━━━━━━━━━━━━━━━━━━", ""]
    for plan_name in ["FREE", "PRO", "ULTIMATE"]:
        limits = plan_limits(plan_name)
        price = PRICES[plan_name]
        price_str = f"{price} ₽/мес" if price else "0 ₽"
        marker = " ← <b>твой</b>" if plan_name == current_plan else ""
        lines.append(f"{limits['emoji']} <b>{plan_name}</b> — {price_str}{marker}")
        for feat in PLAN_FEATURES[plan_name]:
            lines.append(f"  • {feat}")
        lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📩 <b>Для покупки:</b> {OWNER_CONTACT}")
    lines.append(f"🎁 <b>Скидка 30% на первый месяц</b> — спроси у {OWNER_CONTACT}")
    return "\n".join(lines)


def buy_text(uid):
    return (
        "🛒 <b>Покупка подписки</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>💎 PRO — 490 ₽/мес</b>\n"
        "• 100 шаблонов, 2 цели\n"
        "• Задержка 30–60 сек\n"
        "• Live-статистика\n"
        "• Экспорт истории в CSV\n"
        "• Медиа-вложения (1 шт)\n"
        "• Чёрный/белый список\n"
        "• Стоп-слова\n"
        "• Отложенный старт\n"
        "• Приоритетная поддержка\n\n"
        "<b>👑 ULTIMATE — 1 490 ₽/мес</b>\n"
        "• ∞ шаблонов, 4 цели\n"
        "• Задержка 1–60 сек\n"
        "• A/B-тесты шаблонов\n"
        "• Расписание работы\n"
        "• Кастомный бейдж\n"
        "• Прокси-поддержка\n"
        "• Webhooks\n"
        "• Рандомный выбор целей\n"
        "• До 4 сессий одновременно\n"
        "• Персональный менеджер\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📩 <b>Пишите для покупки:</b> {OWNER_CONTACT}\n\n"
        f"💡 <b>Что указать в сообщении:</b>\n"
        f"1. Ваш ID: <code>{uid}</code>\n"
        f"2. Какой тариф хотите\n"
        f"3. Способ оплаты\n\n"
        f"🎁 <b>Скидка 30% на первый месяц</b> — упомяните при обращении!"
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
        f"<b>🚫 Чёрный список</b>\n"
        f"Цели, в которые <b>никогда</b> не пишем (PRO+).\n\n"
        f"<b>✅ Белый список</b>\n"
        f"Если заполнен — пишем <b>только</b> в эти цели (PRO+).\n\n"
        f"<b>🛑 Стоп-слова</b>\n"
        f"Если шаблон содержит слово — он пропускается (PRO+).\n\n"
        f"<b>🧪 A/B-тесты</b>\n"
        f"Считаем сколько раз какой шаблон отправился (ULTIMATE).\n\n"
        f"<b>🕐 Расписание</b>\n"
        f"Рассылка работает только в указанные дни/часы (ULTIMATE).\n\n"
        f"<b>🔔 Webhook</b>\n"
        f"Уведомление на URL при старте/остановке сессии (ULTIMATE).\n\n"
        f"<b>💎 Тарифы</b>\n"
        f"• FREE — 0 ₽\n"
        f"• PRO — 490 ₽/мес\n"
        f"• ULTIMATE — 1 490 ₽/мес\n\n"
        f"<b>💳 Оплата</b>\n"
        f"Через владельца: {OWNER_CONTACT}\n"
        f"Подписка активируется вручную после оплаты.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📩 Поддержка: {SUPPORT_CONTACT}"
    )


def profile_text(p, plan, custom_badge):
    limits = plan_limits(plan)
    badge = custom_badge if custom_badge else limits["badge"]
    templates = len(p.get("templates", []))
    balance = p.get("balance", 0) or 0
    t_max = limits['templates'] if limits['templates'] < 9999 else '∞'
    day_l = limits['daily_limit'] if limits['daily_limit'] < 999999 else '∞'
    return (
        f"🏅 <b>Мой профиль</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Тариф:</b> {badge} {limits['title']}\n"
        f"<b>Эмодзи:</b> {limits['emoji']}\n\n"
        f"<b>📊 Возможности:</b>\n"
        f"• Шаблонов: {t_max}\n"
        f"• Целей: {limits['max_targets']}\n"
        f"• Задержка: {limits['min_delay']}–{limits['max_delay']}с\n"
        f"• Лимит/день: {day_l}\n"
        f"• История: {limits['history_days']} дней\n"
        f"• Медиа: {limits['media']} шт\n"
        f"• Защита: {limits['flood_protection']}\n"
        f"• Сессий: {limits['concurrent_sessions']}\n\n"
        f"<b>📁 Мои данные:</b>\n"
        f"• Шаблонов: {templates}\n"
        f"• Баланс: {balance} ₽\n"
        f"• Сессия: {'✅' if p.get('session') else '❌'}\n"
        f"• Чёрный список: {len(p.get('blacklist', []))}\n"
        f"• Белый список: {len(p.get('whitelist', []))}\n"
        f"• Стоп-слова: {len(p.get('stop_words', []))}\n"
    )


def blacklist_text(p):
    items = p.get("blacklist", [])
    preview = "\n".join(f"• <code>{t}</code>" for t in items[:30]) or "<i>Пусто</i>"
    if len(items) > 30:
        preview += f"\n<i>...и ещё {len(items) - 30}</i>"
    return (
        f"🚫 <b>Чёрный список ({len(items)})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{preview}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Цели, в которые <b>никогда</b> не пишем.\n"
        f"Отправь цели (каждая с новой строки) чтобы добавить."
    )


def whitelist_text(p):
    items = p.get("whitelist", [])
    preview = "\n".join(f"• <code>{t}</code>" for t in items[:30]) or "<i>Пусто</i>"
    if len(items) > 30:
        preview += f"\n<i>...и ещё {len(items) - 30}</i>"
    return (
        f"✅ <b>Белый список ({len(items)})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{preview}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Если заполнен — пишем <b>только</b> в эти цели.\n"
        f"Отправь цели (каждая с новой строки) чтобы добавить."
    )


def stop_words_text(p):
    items = p.get("stop_words", [])
    preview = "\n".join(f"• <code>{w}</code>" for w in items[:30]) or "<i>Пусто</i>"
    if len(items) > 30:
        preview += f"\n<i>...и ещё {len(items) - 30}</i>"
    return (
        f"🛑 <b>Стоп-слова ({len(items)})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{preview}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Если шаблон содержит стоп-слово — он пропускается.\n"
        f"Отправь слова (каждое с новой строки) чтобы добавить."
    )


def schedule_text(p):
    sch = p.get("schedule")
    if not sch:
        return (
            "🕐 <b>Расписание</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Не настроено — работаем всегда.</i>\n\n"
            "Формат ввода:\n"
            "<code>1-5 09:00 23:00</code>\n"
            "где 1-5 — дни недели (0=Пн, 6=Вс)\n"
            "09:00 — начало, 23:00 — конец"
        )
    days = ",".join(str(d) for d in sch.get("days", []))
    return (
        f"🕐 <b>Расписание</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Дни: <code>{days}</code>\n"
        f"Часы: <code>{sch.get('from', '00:00')} — {sch.get('to', '23:59')}</code>\n\n"
        f"Изменить: <code>1-5 09:00 23:00</code>"
    )


def ab_text(rows):
    if not rows:
        return (
            "🧪 <b>A/B-тесты</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Пока нет данных.</i>\n\n"
            "Запусти рассылку — статистика появится автоматически."
        )
    lines = ["🧪 <b>A/B-тесты шаблонов</b>", "━━━━━━━━━━━━━━━━━━━━"]
    total = sum(r[1] or 0 for r in rows) or 1
    for template, sent, errors in rows[:15]:
        sent = sent or 0
        pct = sent / total * 100
        short = template[:40].replace("\n", " ")
        lines.append(f"• {pct:.1f}% — <code>{short}</code> ({sent})")
    return "\n".join(lines)
    # =====================================================================
# 7. ГЛОБАЛЬНОЕ СОСТОЯНИЕ
# =====================================================================
active_clients = {}
user_steps = {}
spam_engines = {}
rate_limits = defaultdict(lambda: deque(maxlen=10))
system_metrics = {
    "total_messages_sent": 0,
    "total_flood_wait_events": 0,
    "total_errors_caught": 0,
    "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
}

bot = TelegramClient("bot_master", API_ID, API_HASH)
repo = None


def is_super(uid):
    return int(uid) in SUPER_ADMINS


def is_rate_limited(uid, max_per_sec=3):
    now = time.time()
    dq = rate_limits[uid]
    dq.append(now)
    recent = [t for t in dq if now - t < 1.0]
    return len(recent) > max_per_sec


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
# 8. ХЕНДЛЕРЫ БОТА
# =====================================================================
@bot.on(events.NewMessage(pattern="/start"))
async def cmd_start(e):
    uid = e.sender_id
    if is_rate_limited(uid):
        return

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
    plan = p.get("plan", "FREE")
    limits = plan_limits(plan)

    if not is_admin and not is_approved:
        return await e.answer("⛔ Доступ закрыт", alert=True)
    if not p.get("accepted_rules") and action not in ("accept_rules", "decline_rules"):
        return await e.answer("⚠️ Сначала примите правила", alert=True)

    back = [[Button.inline("🔙 Назад", b"nav_home")]]

    # ---------- Правила ----------
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

    # ---------- Аккаунт ----------
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

    # ---------- Шаблоны ----------
    elif action == "nav_templates":
        tpls = p.get("templates", [])
        max_t = limits['templates'] if limits['templates'] < 9999 else '∞'
        preview = "\n".join(f"• <code>{t[:60]}</code>" for t in tpls[:10]) or "<i>Пусто</i>"
        if len(tpls) > 10:
            preview += f"\n<i>...и ещё {len(tpls) - 10}</i>"
        buttons = [
            [Button.inline("🗑 Очистить все", b"nav_tpl_clear")],
        ]
        if limits["templates_backup"]:
            buttons.append([Button.inline("💾 Бэкап", b"nav_tpl_backup")])
        buttons.append([Button.inline("🔙 Назад", b"nav_home")])
        await e.edit(
            f"📝 <b>Шаблоны ({len(tpls)}/{max_t})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{preview}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Отправь текст — <b>каждая строка = шаблон</b>.\n"
            f"Отправляется ровно как ты ввёл.",
            buttons=buttons, parse_mode="html",
        )

    elif action == "nav_tpl_clear":
        await repo.save(uid, templates=[])
        await e.answer("✅ Очищено")
        await e.edit("📝 <b>Шаблоны (0)</b>", buttons=back, parse_mode="html")

    elif action == "nav_tpl_backup":
        if not limits["templates_backup"]:
            return await e.answer("💎 Доступно на PRO и выше", alert=True)
        tpls = p.get("templates", [])
        if not tpls:
            return await e.answer("⚠️ Нет шаблонов", alert=True)
        backup = "\n---\n".join(tpls)
        buf = io.BytesIO(backup.encode("utf-8"))
        buf.name = f"templates_{uid}.txt"
        await bot.send_file(uid, buf, caption="💾 Бэкап шаблонов")
        await e.answer("✅ Отправлено")

    # ---------- Задержка ----------
    elif action == "nav_delay":
        user_steps[uid] = "ST_DELAY"
        mind = int(p.get("min_delay", 50000) / 1000)
        maxd = int(p.get("max_delay", 60000) / 1000)
        await e.edit(
            f"⚙️ <b>Задержка</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Текущая: <b>{mind}–{maxd} сек</b>\n"
            f"Диапазон тарифа <b>{plan}</b>: "
            f"{limits['min_delay']}–{limits['max_delay']} сек\n\n"
            f"Формат: <code>{limits['min_delay']}-{limits['max_delay']}</code>",
            buttons=back, parse_mode="html",
        )

    # ---------- Запуск ----------
    elif action == "nav_spam":
        if not p.get("session"):
            return await e.answer("❌ Нет сессии", alert=True)
        if not p.get("templates"):
            return await e.answer("❌ Нет шаблонов", alert=True)
        daily_count = await repo.get_daily_count(uid)
        if daily_count >= limits["daily_limit"]:
            return await e.answer(
                f"⚠️ Дневной лимит {limits['daily_limit']} исчерпан",
                alert=True,
            )
        user_steps[uid] = "ST_TARGET"
        await e.edit(
            f"🚀 <b>Запуск (до {limits['max_targets']} целей)</b>\n\n"
            f"Список целей — <b>каждая с новой строки</b>:\n"
            f"<code>@chat1\n@chat2\nhttps://t.me/chat3\n123456789</code>\n\n"
            f"📈 Сегодня отправлено: {daily_count}/{limits['daily_limit']}",
            buttons=back, parse_mode="html",
        )

    elif action == "nav_stop":
        if uid in spam_engines:
            spam_engines[uid].stop()
            del spam_engines[uid]
            await e.edit("🛑 <b>Остановлено</b>", buttons=back, parse_mode="html")
        else:
            await e.answer("⚠️ Активных рассылок нет", alert=True)

    # ---------- Статистика ----------
    elif action == "nav_stats":
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

    # ---------- Подписка ----------
    elif action == "nav_sub":
        await e.edit(sub_text(plan), buttons=back, parse_mode="html")

    elif action == "nav_buy":
        await e.edit(buy_text(uid), buttons=back, parse_mode="html")

    elif action == "nav_profile":
        custom = p.get("custom_badge")
        await e.edit(profile_text(p, plan, custom), buttons=back, parse_mode="html")

    # ---------- История ----------
    elif action == "nav_history":
        if not limits["export"]:
            return await e.answer("💎 Доступно на PRO и выше", alert=True)
        async with repo.db.conn() as db:
            async with db.execute(
                "SELECT target, sent_messages_count, status, started_at"
                " FROM spam_history WHERE user_id=? ORDER BY id DESC LIMIT 30",
                (uid,),
            ) as cur:
                rows = await cur.fetchall()
        if not rows:
            return await e.edit(
                "📜 <b>История</b>\n\n<i>Пока пусто.</i>",
                buttons=back, parse_mode="html",
            )
        lines = ["📜 <b>Последние сессии</b>", "━━━━━━━━━━━━━━━━━━━━"]
        for target, count, status, started in rows:
            icon = "✅" if status == "STOPPED" else "⏳"
            lines.append(f"{icon} <code>{target}</code> — {count} сообщ.")
        lines.append("")
        lines.append("📤 Экспорт в CSV — кнопка ниже")
        await e.edit(
            "\n".join(lines),
            buttons=[
                [Button.inline("📤 Экспорт CSV", b"nav_export_csv")],
                [Button.inline("🔙 Назад", b"nav_home")],
            ],
            parse_mode="html",
        )

    elif action == "nav_export_csv":
        if not limits["export"]:
            return await e.answer("💎 Доступно на PRO и выше", alert=True)
        async with repo.db.conn() as db:
            async with db.execute(
                "SELECT target, sent_messages_count, status, started_at, ended_at"
                " FROM spam_history WHERE user_id=? ORDER BY id DESC",
                (uid,),
            ) as cur:
                rows = await cur.fetchall()
        if not rows:
            return await e.answer("⚠️ Нет данных", alert=True)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["target", "sent", "status", "started_at", "ended_at"])
        for row in rows:
            writer.writerow(row)
        buf = io.BytesIO(output.getvalue().encode("utf-8-sig"))
        buf.name = f"history_{uid}.csv"
        await bot.send_file(uid, buf, caption="📤 История в CSV")
        await e.answer("✅ Отправлено")

    # ---------- Чёрный список ----------
    elif action == "nav_blacklist":
        if not limits["blacklist"]:
            return await e.answer("💎 Доступно на PRO и выше", alert=True)
        user_steps[uid] = "ST_BLACKLIST"
        await e.edit(
            blacklist_text(p),
            buttons=[
                [Button.inline("🗑 Очистить", b"nav_blacklist_clear")],
                [Button.inline("🔙 Назад", b"nav_home")],
            ],
            parse_mode="html",
        )

    elif action == "nav_blacklist_clear":
        await repo.save(uid, blacklist=[])
        await e.answer("✅ Очищено")
        user_steps[uid] = "IDLE"
        await e.edit(
            "🚫 <b>Чёрный список (0)</b>",
            buttons=back, parse_mode="html",
        )

    # ---------- Белый список ----------
    elif action == "nav_whitelist":
        if not limits["whitelist"]:
            return await e.answer("💎 Доступно на PRO и выше", alert=True)
        user_steps[uid] = "ST_WHITELIST"
        await e.edit(
            whitelist_text(p),
            buttons=[
                [Button.inline("🗑 Очистить", b"nav_whitelist_clear")],
                [Button.inline("🔙 Назад", b"nav_home")],
            ],
            parse_mode="html",
        )

    elif action == "nav_whitelist_clear":
        await repo.save(uid, whitelist=[])
        await e.answer("✅ Очищено")
        user_steps[uid] = "IDLE"
        await e.edit(
            "✅ <b>Белый список (0)</b>",
            buttons=back, parse_mode="html",
        )

    # ---------- Стоп-слова ----------
    elif action == "nav_stop_words":
        if not limits["stop_words"]:
            return await e.answer("💎 Доступно на PRO и выше", alert=True)
        user_steps[uid] = "ST_STOP_WORDS"
        await e.edit(
            stop_words_text(p),
            buttons=[
                [Button.inline("🗑 Очистить", b"nav_stop_words_clear")],
                [Button.inline("🔙 Назад", b"nav_home")],
            ],
            parse_mode="html",
        )

    elif action == "nav_stop_words_clear":
        await repo.save(uid, stop_words=[])
        await e.answer("✅ Очищено")
        user_steps[uid] = "IDLE"
        await e.edit(
            "🛑 <b>Стоп-слова (0)</b>",
            buttons=back, parse_mode="html",
        )

    # ---------- A/B ----------
    elif action == "nav_ab":
        if not limits["ab_test"]:
            return await e.answer("👑 Доступно на ULTIMATE", alert=True)
        rows = await repo.get_ab_stats(uid)
        await e.edit(ab_text(rows), buttons=back, parse_mode="html")

    # ---------- Расписание ----------
    elif action == "nav_schedule":
        if not limits["schedule"]:
            return await e.answer("👑 Доступно на ULTIMATE", alert=True)
        user_steps[uid] = "ST_SCHEDULE"
        await e.edit(
            schedule_text(p),
            buttons=[
                [Button.inline("🗑 Сбросить", b"nav_schedule_clear")],
                [Button.inline("🔙 Назад", b"nav_home")],
            ],
            parse_mode="html",
        )

    elif action == "nav_schedule_clear":
        await repo.save(uid, schedule=None)
        user_steps[uid] = "IDLE"
        await e.answer("✅ Сброшено")
        await e.edit(
            "🕐 <b>Расписание</b>\n\n<i>Сброшено.</i>",
            buttons=back, parse_mode="html",
        )

    # ---------- Webhook ----------
    elif action == "nav_webhook":
        if not limits["webhooks"]:
            return await e.answer("👑 Доступно на ULTIMATE", alert=True)
        user_steps[uid] = "ST_WEBHOOK"
        url = p.get("webhook_url") or "—"
        await e.edit(
            f"🔔 <b>Webhook</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Текущий URL: <code>{url}</code>\n\n"
            f"При старте/стопе сессии шлём JSON POST:\n"
            f"<code>{{\"user_id\":..., \"target\":..., \"sent\":..., \"status\":...}}</code>\n\n"
            f"Отправь URL или <code>off</code> чтобы отключить.",
            buttons=[
                [Button.inline("🗑 Убрать", b"nav_webhook_clear")],
                [Button.inline("🔙 Назад", b"nav_home")],
            ],
            parse_mode="html",
        )

    elif action == "nav_webhook_clear":
        await repo.save(uid, webhook_url=None)
        user_steps[uid] = "IDLE"
        await e.answer("✅ Отключено")
        await e.edit("🔔 <b>Webhook убран</b>", buttons=back, parse_mode="html")

    # ---------- Помощь ----------
    elif action == "nav_help":
        await e.edit(help_text(), buttons=back, parse_mode="html")

    # ---------- Админка ----------
    elif action == "nav_admin" and is_admin:
        await e.edit(
            "👑 <b>Админ-панель</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Управление:</b>\n"
            "<code>.approve &lt;ID&gt;</code> — доступ\n"
            "<code>.revoke &lt;ID&gt;</code> — забрать\n"
            "<code>.sub &lt;ID&gt; &lt;FREE|PRO|ULTIMATE&gt;</code> — тариф\n"
            "<code>.balance &lt;ID&gt; &lt;сумма&gt;</code> — начислить\n"
            "<code>.info &lt;ID&gt;</code> — инфо о юзере\n"
            "<code>.badge &lt;ID&gt; &lt;эмодзи&gt;</code> — кастом бейдж\n\n"
            "<b>Статистика:</b>\n"
            "<code>.stats</code> — общая статистика\n"
            "<code>.revenue</code> — выручка по тарифам\n"
            "<code>.users</code> — список юзеров\n"
            "<code>.broadcast &lt;текст&gt;</code> — рассылка\n\n"
            "<code>.id</code> — свой ID",
            buttons=[
                [Button.inline("📊 Общая статистика", b"admin_stats")],
                [Button.inline("💰 Выручка", b"admin_revenue")],
                [Button.inline("🔙 Назад", b"nav_home")],
            ],
            parse_mode="html",
        )

    elif action == "admin_stats" and is_admin:
        total = await repo.count_users()
        approved = await repo.count_approved()
        by_plan = await repo.count_by_plan()
        pay = await repo.get_payment_stats()
        await e.edit(
            f"📊 <b>Общая статистика</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Юзеров: <b>{total}</b>\n"
            f"✅ С доступом: <b>{approved}</b>\n"
            f"⚪ FREE: {by_plan.get('FREE', 0)}\n"
            f"💎 PRO: {by_plan.get('PRO', 0)}\n"
            f"👑 ULTIMATE: {by_plan.get('ULTIMATE', 0)}\n\n"
            f"💰 Платежей: <b>{pay['count']}</b>\n"
            f"💰 Сумма: <b>{pay['sum']} ₽</b>\n\n"
            f"📨 Всего сообщений: {system_metrics['total_messages_sent']}\n"
            f"🛡 FloodWait: {system_metrics['total_flood_wait_events']}\n"
            f"❌ Ошибок: {system_metrics['total_errors_caught']}",
            buttons=back, parse_mode="html",
        )

    elif action == "admin_revenue" and is_admin:
        async with repo.db.conn() as db:
            async with db.execute(
                "SELECT plan, COUNT(*), SUM(amount) FROM payments_log GROUP BY plan"
            ) as cur:
                rows = await cur.fetchall()
        if not rows:
            return await e.edit("💰 <b>Пока нет платежей</b>", buttons=back, parse_mode="html")
        lines = ["💰 <b>Выручка по тарифам</b>", "━━━━━━━━━━━━━━━━━━━━"]
        total_sum, total_count = 0, 0
        for plan_name, count, amount in rows:
            emoji = {"PRO": "💎", "ULTIMATE": "👑"}.get(plan_name, "⚪")
            lines.append(f"{emoji} <b>{plan_name}</b>: {count} шт — <b>{amount} ₽</b>")
            total_sum += amount or 0
            total_count += count
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📊 Всего продаж: <b>{total_count}</b>")
        lines.append(f"💰 Выручка: <b>{total_sum} ₽</b>")
        await e.edit("\n".join(lines), buttons=back, parse_mode="html")

    # ---------- QR ----------
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

    else:
        await e.answer("⚠️ Неизвестное действие", alert=True)


@bot.on(events.NewMessage)
async def text_handler(e):
    uid = e.sender_id
    text = e.text.strip()
    if text.startswith("/"):
        return
    if is_rate_limited(uid, max_per_sec=5):
        return

    p = await repo.get(uid) or {}
    is_admin = p.get("is_admin") == 1 or is_super(uid)
    is_approved = p.get("is_approved") == 1 or is_super(uid)
    plan = p.get("plan", "FREE")
    limits = plan_limits(plan)

    # ---------- Админ-команды ----------
    if is_admin:
        try:
            if text.startswith(".approve"):
                t_id = int(text.split()[1])
                await repo.save(t_id, is_approved=1)
                await repo.log_audit(uid, "approve", t_id)
                await e.reply(f"✅ Доступ: <code>{t_id}</code>", parse_mode="html")
                try:
                    await bot.send_message(t_id, "✅ Вам выдан доступ! Напишите /start")
                except Exception: pass
                return

            if text.startswith(".revoke"):
                t_id = int(text.split()[1])
                if is_super(t_id):
                    await e.reply("⚠️ Нельзя забрать у создателя")
                    return
                await repo.save(t_id, is_approved=0)
                await repo.log_audit(uid, "revoke", t_id)
                await e.reply(f"🚫 Забран: <code>{t_id}</code>", parse_mode="html")
                return

            if text.startswith(".sub"):
                parts = text.split()
                t_id, new_plan = int(parts[1]), parts[2].upper()
                if new_plan not in ("FREE", "PRO", "ULTIMATE"):
                    await e.reply("❌ FREE / PRO / ULTIMATE")
                    return
                old_p = await repo.get(t_id)
                old_plan = old_p["plan"] if old_p else "FREE"
                await repo.save(t_id, plan=new_plan, is_approved=1)
                await repo.log_audit(uid, "set_plan", t_id, new_plan)
                if new_plan != "FREE" and new_plan != old_plan:
                    await repo.log_payment(t_id, new_plan, PRICES[new_plan], uid, method="admin")
                try:
                    await bot.send_message(
                        t_id,
                        f"🎉 <b>Тариф обновлён!</b>\n\nТвой новый план: <b>{new_plan}</b>",
                        parse_mode="html",
                    )
                except Exception: pass
                await e.reply(f"✅ <b>{new_plan}</b> → <code>{t_id}</code>", parse_mode="html")
                return

            if text.startswith(".balance"):
                parts = text.split()
                t_id, amount = int(parts[1]), int(parts[2])
                tp = await repo.get(t_id)
                current = tp["balance"] if tp else 0
                new_b = current + amount
                if new_b < 0:
                    await e.reply(f"⚠️ Баланс не может быть отрицательным (сейчас {current} ₽)")
                    return
                await repo.add_balance(t_id, amount)
                await repo.log_audit(uid, "balance", t_id, str(amount))
                await e.reply(
                    f"💰 {amount:+d} ₽ → <code>{t_id}</code> (баланс: {new_b} ₽)",
                    parse_mode="html",
                )
                return

            if text.startswith(".badge"):
                parts = text.split(maxsplit=2)
                t_id = int(parts[1])
                badge = parts[2] if len(parts) > 2 else None
                await repo.save(t_id, custom_badge=badge)
                await e.reply(f"🏅 Бейдж → <code>{t_id}</code>: {badge or '—'}", parse_mode="html")
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
                    f"Бейдж: {tp.get('custom_badge') or '—'}\n"
                    f"Сессия: {'✅' if tp['session'] else '❌'}",
                    parse_mode="html",
                )
                return

            if text == ".stats":
                total = await repo.count_users()
                approved = await repo.count_approved()
                by_plan = await repo.count_by_plan()
                pay = await repo.get_payment_stats()
                await e.reply(
                    f"📊 <b>Статистика</b>\n"
                    f"👥 Юзеров: {total}\n"
                    f"✅ С доступом: {approved}\n"
                    f"⚪ FREE: {by_plan.get('FREE', 0)}\n"
                    f"💎 PRO: {by_plan.get('PRO', 0)}\n"
                    f"👑 ULTIMATE: {by_plan.get('ULTIMATE', 0)}\n"
                    f"💰 Платежей: {pay['count']} на {pay['sum']} ₽\n"
                    f"📨 Сообщений: {system_metrics['total_messages_sent']}",
                    parse_mode="html",
                )
                return

            if text == ".revenue":
                async with repo.db.conn() as db:
                    async with db.execute(
                        "SELECT plan, COUNT(*), SUM(amount) FROM payments_log GROUP BY plan"
                    ) as cur:
                        rows = await cur.fetchall()
                if not rows:
                    await e.reply("💰 Пока нет платежей")
                    return
                lines = ["💰 <b>Выручка по тарифам</b>", "━━━━━━━━━━━━━━━━━━━━"]
                total_sum, total_count = 0, 0
                for plan_name, count, amount in rows:
                    emoji = {"PRO": "💎", "ULTIMATE": "👑"}.get(plan_name, "⚪")
                    lines.append(f"{emoji} <b>{plan_name}</b>: {count} шт — <b>{amount} ₽</b>")
                    total_sum += amount or 0
                    total_count += count
                lines.append("━━━━━━━━━━━━━━━━━━━━")
                lines.append(f"📊 Всего продаж: <b>{total_count}</b>")
                lines.append(f"💰 Выручка: <b>{total_sum} ₽</b>")
                await e.reply("\n".join(lines), parse_mode="html")
                return

            if text == ".users":
                users = await repo.get_all_users(50)
                msg = "👥 <b>Юзеры (первые 50):</b>\n" + "\n".join(
                    f"• <code>{u}</code>" for u in users
                )
                await e.reply(msg, parse_mode="html")
                return

            if text.startswith(".broadcast"):
                body = text[len(".broadcast"):].strip()
                if not body:
                    await e.reply("❌ <code>.broadcast текст</code>", parse_mode="html")
                    return
                users = await repo.get_approved_users()
                sent, failed = 0, 0
                await e.reply(f"⏳ Рассылка на {len(users)} юзеров...")
                for u in users:
                    try:
                        await bot.send_message(u, body, parse_mode="html")
                        sent += 1
                        await asyncio.sleep(0.05)
                    except Exception:
                        failed += 1
                await e.reply(f"✅ Отправлено: {sent}, ошибок: {failed}")
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

    # ---------- ST_PHONE ----------
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

    # ---------- ST_CODE ----------
    elif step == "ST_CODE":
        b = active_clients.get(f"temp_{uid}")
        if not b: return
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

    # ---------- ST_2FA ----------
    elif step == "ST_2FA":
        b = active_clients.get(f"temp_{uid}")
        if not b: return
        try:
            await b["client"].sign_in(password=text)
            await repo.save(uid, session=b["client"].session.save())
            active_clients[uid] = b["client"]
            active_clients.pop(f"temp_{uid}", None)
            user_steps[uid] = "IDLE"
            await e.reply("✅ 2FA пройдена! /start")
        except Exception as ex:
            await e.reply(f"❌ <code>{str(ex)[:200]}</code>", parse_mode="html")

    # ---------- ST_TARGET ----------
    elif step == "ST_TARGET":
        user_steps[uid] = "IDLE"
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
                f"⚠️ Тариф <b>{plan}</b> разрешает "
                f"<b>{limits['max_targets']}</b> целей. У тебя: {len(targets)}.\n\n"
                f"Повысь тариф: /start → 💎",
                parse_mode="html",
            )
            return
        daily_count = await repo.get_daily_count(uid)
        if daily_count >= limits["daily_limit"]:
            await e.reply(f"⚠️ Дневной лимит {limits['daily_limit']} исчерпан",
                          parse_mode="html")
            return
        if uid in spam_engines:
            spam_engines[uid].stop()
            await asyncio.sleep(0.5)
        c = await resolve_client(uid)
        if not c:
            await e.reply("❌ Сессия потеряна, /start → Аккаунт")
            return
        eng = SpamEngine(
            c, uid, DB_FILE, p["min_delay"], p["max_delay"], system_metrics,
            protection=limits["flood_protection"],
            auto_pause=limits["auto_pause"],
            repo=repo,
            schedule=p.get("schedule"),
            blacklist=p.get("blacklist", []),
            whitelist=p.get("whitelist", []),
            stop_words=p.get("stop_words", []),
            random_targets=limits.get("random_targets", False),
            ab_test=limits.get("ab_test", False),
            webhook_url=p.get("webhook_url"),
        )
        spam_engines[uid] = eng
        asyncio.create_task(eng.start(targets, p["templates"]))
        msg = "🚀 <b>Запущено</b>\n\n<b>Цели:</b>\n" + "\n".join(
            f"• <code>{t}</code>" for t in targets
        )
        if bad:
            msg += f"\n\n⚠️ Не распознано: {len(bad)}"
        msg += f"\n\nЗадержка: {int(p['min_delay']/1000)}–{int(p['max_delay']/1000)}с"
        msg += f"\n🛡 Защита: {limits['flood_protection']}"
        await e.reply(msg, parse_mode="html")

    # ---------- ST_DELAY ----------
    elif step == "ST_DELAY":
        try:
            low, high = map(int, text.split("-"))
            if low < limits["min_delay"]:
                await e.reply(
                    f"⚠️ Минимум {limits['min_delay']}с для <b>{plan}</b>",
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

    # ---------- ST_BLACKLIST ----------
    elif step == "ST_BLACKLIST":
        user_steps[uid] = "IDLE"
        cur = set(p.get("blacklist", []))
        added = 0
        for line in text.split("\n"):
            n = normalize_target(line)
            if n and n not in cur:
                cur.add(n)
                added += 1
        await repo.save(uid, blacklist=list(cur))
        await e.reply(
            f"🚫 Добавлено в чёрный список: <b>{added}</b>\n"
            f"Всего: {len(cur)}",
            buttons=[[Button.inline("🔙 К списку", b"nav_blacklist")]],
            parse_mode="html",
        )

    # ---------- ST_WHITELIST ----------
    elif step == "ST_WHITELIST":
        user_steps[uid] = "IDLE"
        cur = set(p.get("whitelist", []))
        added = 0
        for line in text.split("\n"):
            n = normalize_target(line)
            if n and n not in cur:
                cur.add(n)
                added += 1
        await repo.save(uid, whitelist=list(cur))
        await e.reply(
            f"✅ Добавлено в белый список: <b>{added}</b>\n"
            f"Всего: {len(cur)}",
            buttons=[[Button.inline("🔙 К списку", b"nav_whitelist")]],
            parse_mode="html",
        )

    # ---------- ST_STOP_WORDS ----------
    elif step == "ST_STOP_WORDS":
        user_steps[uid] = "IDLE"
        cur = set(p.get("stop_words", []))
        added = 0
        for line in text.split("\n"):
            w = line.strip().lower()
            if w and w not in cur:
                cur.add(w)
                added += 1
        await repo.save(uid, stop_words=list(cur))
        await e.reply(
            f"🛑 Добавлено стоп-слов: <b>{added}</b>\n"
            f"Всего: {len(cur)}",
            buttons=[[Button.inline("🔙 К списку", b"nav_stop_words")]],
            parse_mode="html",
        )

    # ---------- ST_SCHEDULE ----------
    elif step == "ST_SCHEDULE":
        user_steps[uid] = "IDLE"
        try:
            parts = text.split()
            if len(parts) < 3:
                raise ValueError
            days_part = parts[0]
            days = []
            for chunk in days_part.split(","):
                if "-" in chunk:
                    a, b_ = map(int, chunk.split("-"))
                    days.extend(range(a, b_ + 1))
                else:
                    days.append(int(chunk))
            from_t, to_t = parts[1], parts[2]
            datetime.datetime.strptime(from_t, "%H:%M")
            datetime.datetime.strptime(to_t, "%H:%M")
            sch = {"days": sorted(set(days)), "from": from_t, "to": to_t}
            await repo.save(uid, schedule=sch)
            await e.reply(
                f"✅ Расписание сохранено:\n"
                f"Дни: <code>{sch['days']}</code>\n"
                f"Часы: <code>{from_t} — {to_t}</code>",
                buttons=[[Button.inline("🔙 К расписанию", b"nav_schedule")]],
                parse_mode="html",
            )
        except Exception:
            await e.reply(
                "❌ Формат: <code>1-5 09:00 23:00</code>",
                parse_mode="html",
            )

    # ---------- ST_WEBHOOK ----------
    elif step == "ST_WEBHOOK":
        user_steps[uid] = "IDLE"
        if text.lower() == "off":
            await repo.save(uid, webhook_url=None)
            await e.reply("✅ Webhook отключён", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
            return
        if not (text.startswith("http://") or text.startswith("https://")):
            await e.reply("❌ URL должен начинаться с http:// или https://")
            return
        await repo.save(uid, webhook_url=text)
        await e.reply(
            f"✅ Webhook сохранён:\n<code>{text}</code>",
            buttons=[[Button.inline("🔙 Назад", b"nav_home")]],
            parse_mode="html",
        )

    # ---------- IDLE (шаблоны) ----------
    elif step == "IDLE":
        new_lines = [line for line in text.split("\n") if line.strip()]
        cur = list(p.get("templates", []))
        added, skipped = 0, 0
        for line in new_lines:
            if line in cur:
                continue
            if len(cur) >= limits["templates"]:
                skipped += 1
                continue
            cur.append(line)
            added += 1
        if added > 0:
            await repo.save(uid, templates=cur)
            max_t = limits['templates'] if limits['templates'] < 9999 else '∞'
            msg = f"📝 Добавлено: <b>{added}</b>. Всего: <b>{len(cur)}/{max_t}</b>"
            if skipped:
                msg += f"\n⚠️ Пропущено {skipped} — лимит тарифа."
            await e.reply(msg, parse_mode="html")
        elif skipped:
            await e.reply(
                f"⚠️ Лимит шаблонов для <b>{plan}</b> — {limits['templates']}\n"
                f"Повысь тариф: /start → 💎",
                parse_mode="html",
            )
        else:
            await e.reply("⚠️ Эти шаблоны уже есть")

# =====================================================================
# 9. WEB DASHBOARD
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
# 10. ЗАПУСК
# =====================================================================
async def main():
    global repo

    db = TitanDB(DB_FILE, BACKUP_DIR)
    await db.bootstrap()
    await db.backup()
    asyncio.create_task(db.backup_loop(6))

    repo = UserRepo(db)

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
    log.info("  TITAN Enterprise v4.0 — старт")
    log.info("=" * 50)
    try:
        asyncio.run(main())
    except AccessTokenInvalidError:
        log.critical("Неверный BOT_TOKEN")
    except KeyboardInterrupt:
        log.info("Остановлено")
