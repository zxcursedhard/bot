# =====================================================================
# TITAN Enterprise v6.0 — монолит для Render
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
        "random_targets": False, "webhooks": False,
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
        "random_targets": False, "webhooks": False,
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
        "random_targets": True, "webhooks": True,
        "badge": "👑", "title": "ULTIMATE", "emoji": "🥇",
    },
}

PRICES = {"FREE": 0, "PRO": 490, "ULTIMATE": 1490}

PLAN_FEATURES = {
    "FREE": ["10 шаблонов", "1 цель", "Задержка 50–60 сек", "Лимит 1000 сообщ/день"],
    "PRO": ["100 шаблонов", "2 цели", "Задержка 30–60 сек", "📊 Live-статистика", "📤 Экспорт CSV", "💾 Бэкап шаблонов", "🚫 Ч/Б списки", "🛑 Стоп-слова", "Лимит 10 000 сообщ/день"],
    "ULTIMATE": ["∞ шаблонов", "4 цели", "Задержка 1–60 сек", "🎯 Всё из PRO", "🧪 A/B-тесты", "🕐 Расписание", "🎲 Рандом целей", "🔔 Webhooks", "♾ Без лимита"],
}

def plan_limits(plan: str) -> dict:
    return PLAN_LIMITS.get(plan, PLAN_LIMITS["FREE"])

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S", stream=sys.stdout)
log = logging.getLogger("TITAN")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, session_string TEXT, templates TEXT DEFAULT '[]',
    min_delay INTEGER DEFAULT 50000, max_delay INTEGER DEFAULT 60000,
    is_approved INTEGER DEFAULT 0, is_admin INTEGER DEFAULT 0, plan TEXT DEFAULT 'FREE',
    accepted_rules INTEGER DEFAULT 0, registered_at TEXT, balance INTEGER DEFAULT 0,
    schedule TEXT, custom_badge TEXT, notes TEXT, blacklist TEXT DEFAULT '[]',
    whitelist TEXT DEFAULT '[]', stop_words TEXT DEFAULT '[]', webhook_url TEXT
);
CREATE TABLE IF NOT EXISTS spam_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, target TEXT,
    sent_messages_count INTEGER DEFAULT 0, status TEXT, started_at TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS payments_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan TEXT,
    amount INTEGER, method TEXT DEFAULT 'manual', admin_id INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, action TEXT,
    target_id INTEGER, details TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT, user_id INTEGER, messages_sent INTEGER DEFAULT 0, PRIMARY KEY (date, user_id)
);
CREATE TABLE IF NOT EXISTS ab_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, template TEXT,
    sent INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_hist_user ON spam_history(user_id, started_at);
CREATE INDEX IF NOT EXISTS idx_daily ON daily_stats(date, user_id);
"""

MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN balance INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN schedule TEXT",
    "ALTER TABLE users ADD COLUMN custom_badge TEXT",
    "ALTER TABLE users ADD COLUMN notes TEXT",
    "ALTER TABLE users ADD COLUMN blacklist TEXT DEFAULT '[]'",
    "ALTER TABLE users ADD COLUMN whitelist TEXT DEFAULT '[]'",
    "ALTER TABLE users ADD COLUMN stop_words TEXT DEFAULT '[]'",
    "ALTER TABLE users ADD COLUMN webhook_url TEXT",
]

USER_DEFAULTS = {
    "session": None, "templates": [], "min_delay": 50000, "max_delay": 60000,
    "is_approved": 0, "is_admin": 0, "plan": "FREE", "accepted_rules": 0,
    "balance": 0, "schedule": None, "custom_badge": None, "notes": None,
    "blacklist": [], "whitelist": [], "stop_words": [], "webhook_url": None,
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
                files = sorted(f for f in os.listdir(self.backup_dir) if f.startswith("backup_"))
                for old in files[:-10]:
                    try: os.remove(os.path.join(self.backup_dir, old))
                    except OSError: pass

    async def backup_loop(self, hours=6):
        while True:
            await asyncio.sleep(hours * 3600)
            try: await self.backup()
            except Exception: pass

class UserRepo:
    def __init__(self, db: TitanDB):
        self.db = db

    async def get(self, uid):
        async with self.db.conn() as db:
            async with db.execute(
                "SELECT session_string, templates, min_delay, max_delay, is_approved,"
                " is_admin, plan, accepted_rules, balance, schedule, custom_badge,"
                " notes, blacklist, whitelist, stop_words, webhook_url"
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
            balance=row[8] or 0, schedule=json.loads(row[9]) if row[9] else None,
            custom_badge=row[10], notes=row[11],
            blacklist=json.loads(row[12] or "[]"), whitelist=json.loads(row[13] or "[]"),
            stop_words=json.loads(row[14] or "[]"), webhook_url=row[15],
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
                    balance, schedule, custom_badge, notes,
                    blacklist, whitelist, stop_words, webhook_url)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    session_string=excluded.session_string, templates=excluded.templates,
                    min_delay=excluded.min_delay, max_delay=excluded.max_delay,
                    is_approved=excluded.is_approved, is_admin=excluded.is_admin,
                    plan=excluded.plan, accepted_rules=excluded.accepted_rules,
                    balance=excluded.balance, schedule=excluded.schedule,
                    custom_badge=excluded.custom_badge, notes=excluded.notes,
                    blacklist=excluded.blacklist, whitelist=excluded.whitelist,
                    stop_words=excluded.stop_words, webhook_url=excluded.webhook_url""",
                (uid, cur["session"], json.dumps(cur["templates"]),
                 cur["min_delay"], cur["max_delay"], cur["is_approved"],
                 cur["is_admin"], cur["plan"], cur["accepted_rules"],
                 datetime.datetime.now().isoformat(timespec="seconds"),
                 cur["balance"], json.dumps(cur["schedule"]) if cur["schedule"] else None,
                 cur["custom_badge"], cur["notes"], json.dumps(cur["blacklist"]),
                 json.dumps(cur["whitelist"]), json.dumps(cur["stop_words"]),
                 cur["webhook_url"]),
            )
            await db.commit()

    async def add_balance(self, uid, amount):
        p = await self.get(uid) or dict(USER_DEFAULTS)
        new_b = max(0, (p["balance"] or 0) + amount)
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
                "INSERT INTO payments_log (user_id, plan, amount, method, admin_id, created_at) VALUES (?,?,?,?,?,?)",
                (uid, plan, amount, method, admin_id, datetime.datetime.now().isoformat(timespec="seconds")),
            )
            await db.commit()

    async def log_audit(self, admin_id, action, target_id, details=""):
        async with self.db.conn() as db:
            await db.execute(
                "INSERT INTO audit_log (admin_id, action, target_id, details, created_at) VALUES (?,?,?,?,?)",
                (admin_id, action, target_id, details, datetime.datetime.now().isoformat(timespec="seconds")),
            )
            await db.commit()

    async def log_daily(self, uid, count=1):
        today = datetime.date.today().isoformat()
        async with self.db.conn() as db:
            await db.execute(
                "INSERT INTO daily_stats (date, user_id, messages_sent) VALUES (?,?,?) ON CONFLICT(date, user_id) DO UPDATE SET messages_sent = messages_sent + excluded.messages_sent",
                (today, uid, count),
            )
            await db.commit()

    async def get_daily_count(self, uid):
        today = datetime.date.today().isoformat()
        async with self.db.conn() as db:
            async with db.execute("SELECT messages_sent FROM daily_stats WHERE date=? AND user_id=?", (today, uid)) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0

    async def get_all_users(self, limit=10000):
        async with self.db.conn() as db:
            async with db.execute("SELECT user_id FROM users LIMIT ?", (limit,)) as cur:
                return [r[0] for r in await cur.fetchall()]

    async def get_approved_users(self, limit=10000):
        async with self.db.conn() as db:
            async with db.execute("SELECT user_id FROM users WHERE is_approved=1 LIMIT ?", (limit,)) as cur:
                return [r[0] for r in await cur.fetchall()]

    async def log_ab_test(self, uid, template, sent=1, errors=0):
        async with self.db.conn() as db:
            await db.execute(
                "INSERT INTO ab_tests (user_id, template, sent, errors, created_at) VALUES (?,?,?,?,?)",
                (uid, template, sent, errors, datetime.datetime.now().isoformat(timespec="seconds")),
            )
            await db.commit()

    async def get_ab_stats(self, uid):
        async with self.db.conn() as db:
            async with db.execute("SELECT template, SUM(sent), SUM(errors) FROM ab_tests WHERE user_id=? GROUP BY template ORDER BY SUM(sent) DESC", (uid,)) as cur:
                return await cur.fetchall()

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
    if not schedule: return True
    now = datetime.datetime.now()
    days = schedule.get("days", list(range(7)))
    if now.weekday() not in days: return False
    try:
        fh, fm = map(int, schedule.get("from", "00:00").split(":"))
        th, tm = map(int, schedule.get("to", "23:59").split(":"))
        current = now.hour * 60 + now.minute
        return fh * 60 + fm <= current <= th * 60 + tm
    except: return True

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

class SpamEngine:
    def __init__(self, client, user_id, db_path, min_ms, max_ms, metrics, protection="basic", auto_pause=True, repo=None, schedule=None, blacklist=None, whitelist=None, stop_words=None, random_targets=False, ab_test=False, webhook_url=None):
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

    @property
    def is_active(self):
        return self._active.is_set()

    def _is_allowed(self, target):
        if self.whitelist and target not in self.whitelist: return False
        if target in self.blacklist: return False
        return True

    def _filter_msg(self, msg):
        for w in self.stop_words:
            if w and w.lower() in msg.lower(): return None
        return msg

    async def _send_webhook(self, status, target, count):
        if not self.webhook_url: return
        try:
            import urllib.request
            data = json.dumps({"user_id": self.user_id, "target": target, "sent": count, "status": status}).encode("utf-8")
            req = urllib.request.Request(self.webhook_url, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except: pass

    async def _write_history(self, target, count, status):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO spam_history (user_id, target, sent_messages_count, status, started_at, ended_at) VALUES (?,?,?,?,?,?)",
                    (self.user_id, target, count, status, self.started_at.isoformat(timespec="seconds"), datetime.datetime.now().isoformat(timespec="seconds")),
                )
                await db.commit()
        except Exception as e:
            log.warning("history: %s", e)

    async def _attack_one(self, target, templates):
        counter = 0
        while self.is_active:
            if self.schedule and not is_valid_schedule(self.schedule):
                await asyncio.sleep(60)
                continue
            if not self._is_allowed(target): break
            try:
                raw = random.choice(templates)
                msg = self._filter_msg(raw)
                if msg is None: continue
                await self.client.send_message(target, msg)
                counter += 1
                self.sent += 1
                self._consecutive_errors = 0
                self.metrics["total_messages_sent"] += 1
                if self.repo:
                    try:
                        await self.repo.log_daily(self.user_id, 1)
                        if self.ab_test: await self.repo.log_ab_test(self.user_id, raw, sent=1)
                    except: pass
                if counter % 10 == 0: await self._write_history(target, counter, "RUNNING")
                
                delay = self.pacer.next_delay()
                try: await asyncio.wait_for(self._active.wait(), timeout=delay)
                except asyncio.TimeoutError: pass
            except FloodWaitError as e:
                self.metrics["total_flood_wait_events"] += 1
                await asyncio.sleep(self.pacer.on_flood(e.seconds))
            except (ChatWriteForbiddenError, ChatAdminRequiredError, UserBannedInChannelError):
                self.errors += 1; self.metrics["total_errors_caught"] += 1; break
            except (SlowModeWaitError, PeerFloodError) as e:
                self.errors += 1; self._consecutive_errors += 1
                await asyncio.sleep(getattr(e, "seconds", 30) + 5)
                if self.auto_pause and self._consecutive_errors >= self._error_threshold: break
            except Exception as e:
                self.errors += 1; self.metrics["total_errors_caught"] += 1; self._consecutive_errors += 1
                if self.auto_pause and self._consecutive_errors >= self._error_threshold: break
        await self._write_history(target, counter, "STOPPED")
        await self._send_webhook("stopped", target, counter)

    async def start(self, targets, templates):
        self._active.set()
        tasks = [asyncio.create_task(self._attack_one(t, templates)) for t in targets]
        self._tasks.update(tasks)
        for t in tasks: t.add_done_callback(self._tasks.discard)
        await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()

    def stop(self):
        self._active.clear()
        for t in list(self._tasks): t.cancel()

    def update_delay(self, min_ms, max_ms):
        self.pacer.base_min = min_ms
        self.pacer.base_max = max_ms
        self.pacer.penalty = 0
        log.info("[%s] Задержка обновлена: %d-%d сек", self.user_id, min_ms // 1000, max_ms // 1000)

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
# TITAN Enterprise v6.0 — монолит для Render
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
        "random_targets": False, "webhooks": False,
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
        "random_targets": False, "webhooks": False,
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
        "random_targets": True, "webhooks": True,
        "badge": "👑", "title": "ULTIMATE", "emoji": "🥇",
    },
}

PRICES = {"FREE": 0, "PRO": 490, "ULTIMATE": 1490}

PLAN_FEATURES = {
    "FREE": ["10 шаблонов", "1 цель", "Задержка 50–60 сек", "Лимит 1000 сообщ/день"],
    "PRO": ["100 шаблонов", "2 цели", "Задержка 30–60 сек", "📊 Live-статистика", "📤 Экспорт CSV", "💾 Бэкап шаблонов", "🚫 Ч/Б списки", "🛑 Стоп-слова", "Лимит 10 000 сообщ/день"],
    "ULTIMATE": ["∞ шаблонов", "4 цели", "Задержка 1–60 сек", "🎯 Всё из PRO", "🧪 A/B-тесты", "🕐 Расписание", "🎲 Рандом целей", "🔔 Webhooks", "♾ Без лимита"],
}

def plan_limits(plan: str) -> dict:
    return PLAN_LIMITS.get(plan, PLAN_LIMITS["FREE"])

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S", stream=sys.stdout)
log = logging.getLogger("TITAN")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, session_string TEXT, templates TEXT DEFAULT '[]',
    min_delay INTEGER DEFAULT 50000, max_delay INTEGER DEFAULT 60000,
    is_approved INTEGER DEFAULT 0, is_admin INTEGER DEFAULT 0, plan TEXT DEFAULT 'FREE',
    accepted_rules INTEGER DEFAULT 0, registered_at TEXT, balance INTEGER DEFAULT 0,
    schedule TEXT, custom_badge TEXT, notes TEXT, blacklist TEXT DEFAULT '[]',
    whitelist TEXT DEFAULT '[]', stop_words TEXT DEFAULT '[]', webhook_url TEXT
);
CREATE TABLE IF NOT EXISTS spam_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, target TEXT,
    sent_messages_count INTEGER DEFAULT 0, status TEXT, started_at TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS payments_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan TEXT,
    amount INTEGER, method TEXT DEFAULT 'manual', admin_id INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, action TEXT,
    target_id INTEGER, details TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT, user_id INTEGER, messages_sent INTEGER DEFAULT 0, PRIMARY KEY (date, user_id)
);
CREATE TABLE IF NOT EXISTS ab_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, template TEXT,
    sent INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_hist_user ON spam_history(user_id, started_at);
CREATE INDEX IF NOT EXISTS idx_daily ON daily_stats(date, user_id);
"""

MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN balance INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN schedule TEXT",
    "ALTER TABLE users ADD COLUMN custom_badge TEXT",
    "ALTER TABLE users ADD COLUMN notes TEXT",
    "ALTER TABLE users ADD COLUMN blacklist TEXT DEFAULT '[]'",
    "ALTER TABLE users ADD COLUMN whitelist TEXT DEFAULT '[]'",
    "ALTER TABLE users ADD COLUMN stop_words TEXT DEFAULT '[]'",
    "ALTER TABLE users ADD COLUMN webhook_url TEXT",
]

USER_DEFAULTS = {
    "session": None, "templates": [], "min_delay": 50000, "max_delay": 60000,
    "is_approved": 0, "is_admin": 0, "plan": "FREE", "accepted_rules": 0,
    "balance": 0, "schedule": None, "custom_badge": None, "notes": None,
    "blacklist": [], "whitelist": [], "stop_words": [], "webhook_url": None,
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
                files = sorted(f for f in os.listdir(self.backup_dir) if f.startswith("backup_"))
                for old in files[:-10]:
                    try: os.remove(os.path.join(self.backup_dir, old))
                    except OSError: pass

    async def backup_loop(self, hours=6):
        while True:
            await asyncio.sleep(hours * 3600)
            try: await self.backup()
            except Exception: pass

class UserRepo:
    def __init__(self, db: TitanDB):
        self.db = db

    async def get(self, uid):
        async with self.db.conn() as db:
            async with db.execute(
                "SELECT session_string, templates, min_delay, max_delay, is_approved,"
                " is_admin, plan, accepted_rules, balance, schedule, custom_badge,"
                " notes, blacklist, whitelist, stop_words, webhook_url"
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
            balance=row[8] or 0, schedule=json.loads(row[9]) if row[9] else None,
            custom_badge=row[10], notes=row[11],
            blacklist=json.loads(row[12] or "[]"), whitelist=json.loads(row[13] or "[]"),
            stop_words=json.loads(row[14] or "[]"), webhook_url=row[15],
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
                    balance, schedule, custom_badge, notes,
                    blacklist, whitelist, stop_words, webhook_url)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    session_string=excluded.session_string, templates=excluded.templates,
                    min_delay=excluded.min_delay, max_delay=excluded.max_delay,
                    is_approved=excluded.is_approved, is_admin=excluded.is_admin,
                    plan=excluded.plan, accepted_rules=excluded.accepted_rules,
                    balance=excluded.balance, schedule=excluded.schedule,
                    custom_badge=excluded.custom_badge, notes=excluded.notes,
                    blacklist=excluded.blacklist, whitelist=excluded.whitelist,
                    stop_words=excluded.stop_words, webhook_url=excluded.webhook_url""",
                (uid, cur["session"], json.dumps(cur["templates"]),
                 cur["min_delay"], cur["max_delay"], cur["is_approved"],
                 cur["is_admin"], cur["plan"], cur["accepted_rules"],
                 datetime.datetime.now().isoformat(timespec="seconds"),
                 cur["balance"], json.dumps(cur["schedule"]) if cur["schedule"] else None,
                 cur["custom_badge"], cur["notes"], json.dumps(cur["blacklist"]),
                 json.dumps(cur["whitelist"]), json.dumps(cur["stop_words"]),
                 cur["webhook_url"]),
            )
            await db.commit()

    async def add_balance(self, uid, amount):
        p = await self.get(uid) or dict(USER_DEFAULTS)
        new_b = max(0, (p["balance"] or 0) + amount)
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
                "INSERT INTO payments_log (user_id, plan, amount, method, admin_id, created_at) VALUES (?,?,?,?,?,?)",
                (uid, plan, amount, method, admin_id, datetime.datetime.now().isoformat(timespec="seconds")),
            )
            await db.commit()

    async def log_audit(self, admin_id, action, target_id, details=""):
        async with self.db.conn() as db:
            await db.execute(
                "INSERT INTO audit_log (admin_id, action, target_id, details, created_at) VALUES (?,?,?,?,?)",
                (admin_id, action, target_id, details, datetime.datetime.now().isoformat(timespec="seconds")),
            )
            await db.commit()

    async def log_daily(self, uid, count=1):
        today = datetime.date.today().isoformat()
        async with self.db.conn() as db:
            await db.execute(
                "INSERT INTO daily_stats (date, user_id, messages_sent) VALUES (?,?,?) ON CONFLICT(date, user_id) DO UPDATE SET messages_sent = messages_sent + excluded.messages_sent",
                (today, uid, count),
            )
            await db.commit()

    async def get_daily_count(self, uid):
        today = datetime.date.today().isoformat()
        async with self.db.conn() as db:
            async with db.execute("SELECT messages_sent FROM daily_stats WHERE date=? AND user_id=?", (today, uid)) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0

    async def get_all_users(self, limit=10000):
        async with self.db.conn() as db:
            async with db.execute("SELECT user_id FROM users LIMIT ?", (limit,)) as cur:
                return [r[0] for r in await cur.fetchall()]

    async def get_approved_users(self, limit=10000):
        async with self.db.conn() as db:
            async with db.execute("SELECT user_id FROM users WHERE is_approved=1 LIMIT ?", (limit,)) as cur:
                return [r[0] for r in await cur.fetchall()]

    async def log_ab_test(self, uid, template, sent=1, errors=0):
        async with self.db.conn() as db:
            await db.execute(
                "INSERT INTO ab_tests (user_id, template, sent, errors, created_at) VALUES (?,?,?,?,?)",
                (uid, template, sent, errors, datetime.datetime.now().isoformat(timespec="seconds")),
            )
            await db.commit()

    async def get_ab_stats(self, uid):
        async with self.db.conn() as db:
            async with db.execute("SELECT template, SUM(sent), SUM(errors) FROM ab_tests WHERE user_id=? GROUP BY template ORDER BY SUM(sent) DESC", (uid,)) as cur:
                return await cur.fetchall()

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
    if not schedule: return True
    now = datetime.datetime.now()
    days = schedule.get("days", list(range(7)))
    if now.weekday() not in days: return False
    try:
        fh, fm = map(int, schedule.get("from", "00:00").split(":"))
        th, tm = map(int, schedule.get("to", "23:59").split(":"))
        current = now.hour * 60 + now.minute
        return fh * 60 + fm <= current <= th * 60 + tm
    except: return True

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

class SpamEngine:
    def __init__(self, client, user_id, db_path, min_ms, max_ms, metrics, protection="basic", auto_pause=True, repo=None, schedule=None, blacklist=None, whitelist=None, stop_words=None, random_targets=False, ab_test=False, webhook_url=None):
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

    @property
    def is_active(self):
        return self._active.is_set()

    def _is_allowed(self, target):
        if self.whitelist and target not in self.whitelist: return False
        if target in self.blacklist: return False
        return True

    def _filter_msg(self, msg):
        for w in self.stop_words:
            if w and w.lower() in msg.lower(): return None
        return msg

    async def _send_webhook(self, status, target, count):
        if not self.webhook_url: return
        try:
            import urllib.request
            data = json.dumps({"user_id": self.user_id, "target": target, "sent": count, "status": status}).encode("utf-8")
            req = urllib.request.Request(self.webhook_url, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except: pass

    async def _write_history(self, target, count, status):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO spam_history (user_id, target, sent_messages_count, status, started_at, ended_at) VALUES (?,?,?,?,?,?)",
                    (self.user_id, target, count, status, self.started_at.isoformat(timespec="seconds"), datetime.datetime.now().isoformat(timespec="seconds")),
                )
                await db.commit()
        except Exception as e:
            log.warning("history: %s", e)

    async def _attack_one(self, target, templates):
        counter = 0
        while self.is_active:
            if self.schedule and not is_valid_schedule(self.schedule):
                await asyncio.sleep(60)
                continue
            if not self._is_allowed(target): break
            try:
                raw = random.choice(templates)
                msg = self._filter_msg(raw)
                if msg is None: continue
                await self.client.send_message(target, msg)
                counter += 1
                self.sent += 1
                self._consecutive_errors = 0
                self.metrics["total_messages_sent"] += 1
                if self.repo:
                    try:
                        await self.repo.log_daily(self.user_id, 1)
                        if self.ab_test: await self.repo.log_ab_test(self.user_id, raw, sent=1)
                    except: pass
                if counter % 10 == 0: await self._write_history(target, counter, "RUNNING")
                
                delay = self.pacer.next_delay()
                try: await asyncio.wait_for(self._active.wait(), timeout=delay)
                except asyncio.TimeoutError: pass
            except FloodWaitError as e:
                self.metrics["total_flood_wait_events"] += 1
                await asyncio.sleep(self.pacer.on_flood(e.seconds))
            except (ChatWriteForbiddenError, ChatAdminRequiredError, UserBannedInChannelError):
                self.errors += 1; self.metrics["total_errors_caught"] += 1; break
            except (SlowModeWaitError, PeerFloodError) as e:
                self.errors += 1; self._consecutive_errors += 1
                await asyncio.sleep(getattr(e, "seconds", 30) + 5)
                if self.auto_pause and self._consecutive_errors >= self._error_threshold: break
            except Exception as e:
                self.errors += 1; self.metrics["total_errors_caught"] += 1; self._consecutive_errors += 1
                if self.auto_pause and self._consecutive_errors >= self._error_threshold: break
        await self._write_history(target, counter, "STOPPED")
        await self._send_webhook("stopped", target, counter)

    async def start(self, targets, templates):
        self._active.set()
        tasks = [asyncio.create_task(self._attack_one(t, templates)) for t in targets]
        self._tasks.update(tasks)
        for t in tasks: t.add_done_callback(self._tasks.discard)
        await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()

    def stop(self):
        self._active.clear()
        for t in list(self._tasks): t.cancel()

    def update_delay(self, min_ms, max_ms):
        self.pacer.base_min = min_ms
        self.pacer.base_max = max_ms
        self.pacer.penalty = 0
        log.info("[%s] Задержка обновлена: %d-%d сек", self.user_id, min_ms // 1000, max_ms // 1000)

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
            elif action == "nav_spam":
        if not p.get("session"):
            return await e.answer("❌ Нет сессии", alert=True)
        if not p.get("templates"):
            return await e.answer("❌ Нет шаблонов", alert=True)
        daily_count = await repo.get_daily_count(uid)
        if daily_count >= limits["daily_limit"]:
            return await e.answer(f"⚠️ Дневной лимит {limits['daily_limit']} исчерпан", alert=True)
        user_steps[uid] = "ST_TARGET"
        await e.edit(
            f"🚀 <b>Запуск (до {limits['max_targets']} целей)</b>\n\n"
            f"Список целей — <b>каждая с новой строки</b>:\n"
            f"<code>@chat1\n@chat2\nhttps://t.me/chat3\n123456789</code>\n\n"
            f"📈 Сегодня: {daily_count}/{limits['daily_limit']}",
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
        if not limits["stats"]:
            return await e.answer("💎 Доступно на PRO и выше", alert=True)
        if uid in spam_engines:
            await e.edit(
                spam_engines[uid].stats_text(),
                buttons=[[Button.inline("🔄 Обновить", b"nav_stats"), Button.inline("🛑 Стоп", b"nav_stop")], [Button.inline("🔙 Назад", b"nav_home")]],
                parse_mode="html",
            )
        else:
            await e.edit("📊 <b>Статистика</b>\n\n<i>Нет активной сессии.</i>", buttons=back, parse_mode="html")
    elif action == "nav_sub":
        await e.edit(sub_text(plan), buttons=back, parse_mode="html")
    elif action == "nav_buy":
        await e.edit(buy_text(uid), buttons=back, parse_mode="html")
    elif action == "nav_profile":
        await e.edit(profile_text(p, plan, p.get("custom_badge")), buttons=back, parse_mode="html")
    elif action == "nav_history":
        if not limits["export"]:
            return await e.answer("💎 Доступно на PRO и выше", alert=True)
        async with repo.db.conn() as db:
            async with db.execute("SELECT target, sent_messages_count, status FROM spam_history WHERE user_id=? ORDER BY id DESC LIMIT 30", (uid,)) as cur:
                rows = await cur.fetchall()
        if not rows:
            return await e.edit("📜 <b>История</b>\n\n<i>Пока пусто.</i>", buttons=back, parse_mode="html")
        lines = ["📜 <b>Последние сессии</b>", "━━━━━━━━━━━━━━━━━━━━"]
        for target, count, status in rows:
            icon = "✅" if status == "STOPPED" else "⏳"
            lines.append(f"{icon} <code>{target}</code> — {count} сообщ.")
        await e.edit("\n".join(lines), buttons=[[Button.inline("📤 Экспорт CSV", b"nav_export_csv")], [Button.inline("🔙 Назад", b"nav_home")]], parse_mode="html")
    elif action == "nav_export_csv":
        if not limits["export"]: return await e.answer("💎 Доступно на PRO и выше", alert=True)
        async with repo.db.conn() as db:
            async with db.execute("SELECT target, sent_messages_count, status, started_at, ended_at FROM spam_history WHERE user_id=? ORDER BY id DESC", (uid,)) as cur:
                rows = await cur.fetchall()
        if not rows: return await e.answer("⚠️ Нет данных", alert=True)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["target", "sent", "status", "started_at", "ended_at"])
        writer.writerows(rows)
        buf = io.BytesIO(output.getvalue().encode("utf-8-sig"))
        buf.name = f"history_{uid}.csv"
        await bot.send_file(uid, buf, caption="📤 История в CSV")
        await e.answer("✅ Отправлено")
    elif action == "nav_blacklist":
        if not limits["blacklist"]: return await e.answer("💎 Доступно на PRO и выше", alert=True)
        user_steps[uid] = "ST_BLACKLIST"
        await e.edit(blacklist_text(p), buttons=[[Button.inline("🗑 Очистить", b"nav_blacklist_clear")], [Button.inline("🔙 Назад", b"nav_home")]], parse_mode="html")
    elif action == "nav_blacklist_clear":
        await repo.save(uid, blacklist=[])
        await e.answer("✅ Очищено"); user_steps[uid] = "IDLE"
        await e.edit("🚫 <b>Чёрный список (0)</b>", buttons=back, parse_mode="html")
    elif action == "nav_whitelist":
        if not limits["whitelist"]: return await e.answer("💎 Доступно на PRO и выше", alert=True)
        user_steps[uid] = "ST_WHITELIST"
        await e.edit(whitelist_text(p), buttons=[[Button.inline("🗑 Очистить", b"nav_whitelist_clear")], [Button.inline("🔙 Назад", b"nav_home")]], parse_mode="html")
    elif action == "nav_whitelist_clear":
        await repo.save(uid, whitelist=[])
        await e.answer("✅ Очищено"); user_steps[uid] = "IDLE"
        await e.edit("✅ <b>Белый список (0)</b>", buttons=back, parse_mode="html")
    elif action == "nav_stop_words":
        if not limits["stop_words"]: return await e.answer("💎 Доступно на PRO и выше", alert=True)
        user_steps[uid] = "ST_STOP_WORDS"
        await e.edit(stop_words_text(p), buttons=[[Button.inline("🗑 Очистить", b"nav_stop_words_clear")], [Button.inline("🔙 Назад", b"nav_home")]], parse_mode="html")
    elif action == "nav_stop_words_clear":
        await repo.save(uid, stop_words=[])
        await e.answer("✅ Очищено"); user_steps[uid] = "IDLE"
        await e.edit("🛑 <b>Стоп-слова (0)</b>", buttons=back, parse_mode="html")
    elif action == "nav_ab":
        if not limits["ab_test"]: return await e.answer("👑 Доступно на ULTIMATE", alert=True)
        rows = await repo.get_ab_stats(uid)
        await e.edit(ab_text(rows), buttons=back, parse_mode="html")
    elif action == "nav_schedule":
        if not limits["schedule"]: return await e.answer("👑 Доступно на ULTIMATE", alert=True)
        user_steps[uid] = "ST_SCHEDULE"
        await e.edit(schedule_text(p), buttons=[[Button.inline("🗑 Сбросить", b"nav_schedule_clear")], [Button.inline("🔙 Назад", b"nav_home")]], parse_mode="html")
    elif action == "nav_schedule_clear":
        await repo.save(uid, schedule=None); user_steps[uid] = "IDLE"
        await e.answer("✅ Сброшено")
        await e.edit("🕐 <b>Расписание</b>\n\n<i>Сброшено.</i>", buttons=back, parse_mode="html")
    elif action == "nav_webhook":
        if not limits["webhooks"]: return await e.answer("👑 Доступно на ULTIMATE", alert=True)
        user_steps[uid] = "ST_WEBHOOK"
        url = p.get("webhook_url") or "—"
        await e.edit(f"🔔 <b>Webhook</b>\n━━━━━━━━━━━━━━━━━━━━\nТекущий: <code>{url}</code>\n\nОтправь URL или <code>off</code> чтобы отключить.", buttons=[[Button.inline("🗑 Убрать", b"nav_webhook_clear")], [Button.inline("🔙 Назад", b"nav_home")]], parse_mode="html")
    elif action == "nav_webhook_clear":
        await repo.save(uid, webhook_url=None); user_steps[uid] = "IDLE"
        await e.answer("✅ Отключено")
        await e.edit("🔔 <b>Webhook убран</b>", buttons=back, parse_mode="html")
    elif action == "nav_help":
        await e.edit(help_text(), buttons=back, parse_mode="html")
    elif action == "nav_admin" and is_admin:
        await e.edit(
            "👑 <b>Админ-панель</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<code>.approve &lt;ID&gt;</code> — доступ\n"
            "<code>.revoke &lt;ID&gt;</code> — забрать\n"
            "<code>.sub &lt;ID&gt; &lt;PLAN&gt;</code> — тариф\n"
            "<code>.balance &lt;ID&gt; &lt;сумма&gt;</code> — начислить\n"
            "<code>.info &lt;ID&gt;</code> — инфо\n"
            "<code>.badge &lt;ID&gt; &lt;эмодзи&gt;</code> — бейдж\n"
            "<code>.stats</code> / <code>.revenue</code> / <code>.users</code>\n"
            "<code>.broadcast &lt;текст&gt;</code> — рассылка\n"
            "<code>.id</code> — свой ID",
            buttons=[[Button.inline("📊 Общая статистика", b"admin_stats")], [Button.inline("💰 Выручка", b"admin_revenue")], [Button.inline("🔙 Назад", b"nav_home")]],
            parse_mode="html",
        )
    elif action == "admin_stats" and is_admin:
        total = await repo.count_users(); approved = await repo.count_approved(); by_plan = await repo.count_by_plan(); pay = await repo.get_payment_stats()
        await e.edit(
            f"📊 <b>Общая статистика</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Юзеров: <b>{total}</b>\n"
            f"✅ С доступом: <b>{approved}</b>\n"
            f"⚪ FREE: {by_plan.get('FREE', 0)}\n"
            f"💎 PRO: {by_plan.get('PRO', 0)}\n"
            f"👑 ULTIMATE: {by_plan.get('ULTIMATE', 0)}\n\n"
            f"💰 Платежей: <b>{pay['count']}</b> на <b>{pay['sum']} ₽</b>\n"
            f"📨 Сообщений: {system_metrics['total_messages_sent']}",
            buttons=back, parse_mode="html",
        )
    elif action == "admin_revenue" and is_admin:
        async with repo.db.conn() as db:
            async with db.execute("SELECT plan, COUNT(*), SUM(amount) FROM payments_log GROUP BY plan") as cur:
                rows = await cur.fetchall()
        if not rows: return await e.edit("💰 <b>Пока нет платежей</b>", buttons=back, parse_mode="html")
        lines = ["💰 <b>Выручка по тарифам</b>", "━━━━━━━━━━━━━━━━━━━━"]
        total_sum, total_count = 0, 0
        for plan_name, count, amount in rows:
            emoji = {"PRO": "💎", "ULTIMATE": "👑"}.get(plan_name, "⚪")
            lines.append(f"{emoji} <b>{plan_name}</b>: {count} шт — <b>{amount} ₽</b>")
            total_sum += amount or 0; total_count += count
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📊 Всего продаж: <b>{total_count}</b>")
        lines.append(f"💰 Выручка: <b>{total_sum} ₽</b>")
        await e.edit("\n".join(lines), buttons=back, parse_mode="html")
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
                buttons=[[Button.inline("🔙 Отмена", b"qr_cancel")]], parse_mode="html",
            )
            try:
                await asyncio.wait_for(qr.wait(), timeout=120)
                await repo.save(uid, session=c.session.save())
                active_clients[uid] = c
                active_clients.pop(f"qr_{uid}", None)
                user_steps[uid] = "IDLE"
                await e.edit("✅ <b>Успешно авторизован!</b>\n\n/start", parse_mode="html")
            except asyncio.TimeoutError:
                active_clients.pop(f"qr_{uid}", None); user_steps[uid] = "IDLE"
                await e.edit("⏱ QR истёк. /start")
        except Exception as ex:
            user_steps[uid] = "IDLE"
            await e.edit(f"❌ Ошибка QR: <code>{str(ex)[:200]}</code>", parse_mode="html")
    elif action == "qr_cancel":
        b = active_clients.pop(f"qr_{uid}", None)
        if b:
            try: await b["client"].disconnect()
            except: pass
        user_steps[uid] = "IDLE"
        await e.edit("❌ Отменено", buttons=back)
    elif action == "resend_call":
        b = active_clients.get(f"temp_{uid}")
        if not b: return await e.answer("❌ Нет активной попытки", alert=True)
        try:
            await b["client"].send_code_request(b["phone"], force_sms=False)
            await e.answer("📞 Повторный запрос", alert=True)
        except Exception as ex: await e.answer(f"❌ {str(ex)[:100]}", alert=True)
    else:
        await e.answer("⚠️ Неизвестное действие", alert=True)


@bot.on(events.NewMessage)
async def text_handler(e):
    uid = e.sender_id
    text = e.text.strip()
    if text.startswith("/") or is_rate_limited(uid, max_per_sec=5): return

    p = await repo.get(uid) or {}
    is_admin = p.get("is_admin") == 1 or is_super(uid)
    is_approved = p.get("is_approved") == 1 or is_super(uid)
    plan = p.get("plan", "FREE")
    limits = plan_limits(plan)

    # Админ-команды
    if is_admin:
        try:
            if text.startswith(".approve"):
                t_id = int(text.split()[1])
                await repo.save(t_id, is_approved=1); await repo.log_audit(uid, "approve", t_id)
                await e.reply(f"✅ Доступ: <code>{t_id}</code>", parse_mode="html")
                try: await bot.send_message(t_id, "✅ Вам выдан доступ! Напишите /start")
                except: pass
                return
            if text.startswith(".revoke"):
                t_id = int(text.split()[1])
                if is_super(t_id): return await e.reply("⚠️ Нельзя забрать у создателя")
                await repo.save(t_id, is_approved=0); await repo.log_audit(uid, "revoke", t_id)
                await e.reply(f"🚫 Забран: <code>{t_id}</code>", parse_mode="html")
                return
            if text.startswith(".sub"):
                parts = text.split(); t_id, new_plan = int(parts[1]), parts[2].upper()
                if new_plan not in ("FREE", "PRO", "ULTIMATE"): return await e.reply("❌ FREE / PRO / ULTIMATE")
                old_p = await repo.get(t_id); old_plan = old_p["plan"] if old_p else "FREE"
                await repo.save(t_id, plan=new_plan, is_approved=1); await repo.log_audit(uid, "set_plan", t_id, new_plan)
                if new_plan != "FREE" and new_plan != old_plan: await repo.log_payment(t_id, new_plan, PRICES[new_plan], uid)
                try: await bot.send_message(t_id, f"🎉 <b>Тариф обновлён!</b>\n\nТвой новый план: <b>{new_plan}</b>", parse_mode="html")
                except: pass
                await e.reply(f"✅ <b>{new_plan}</b> → <code>{t_id}</code>", parse_mode="html")
                return
            if text.startswith(".balance"):
                parts = text.split(); t_id, amount = int(parts[1]), int(parts[2])
                tp = await repo.get(t_id); current = tp["balance"] if tp else 0
                if current + amount < 0: return await e.reply(f"⚠️ Баланс не может быть отрицательным (сейчас {current} ₽)")
                await repo.add_balance(t_id, amount); await repo.log_audit(uid, "balance", t_id, str(amount))
                await e.reply(f"💰 {amount:+d} ₽ → <code>{t_id}</code> (баланс: {current+amount} ₽)", parse_mode="html")
                return
            if text.startswith(".badge"):
                parts = text.split(maxsplit=2); t_id = int(parts[1]); badge = parts[2] if len(parts) > 2 else None
                await repo.save(t_id, custom_badge=badge)
                await e.reply(f"🏅 Бейдж → <code>{t_id}</code>: {badge or '—'}", parse_mode="html")
                return
            if text.startswith(".info"):
                tp = await repo.get(int(text.split()[1]))
                if not tp: return await e.reply("❌ Не найден")
                await e.reply(f"👤 <b>Юзер</b> <code>{tp.get('user_id', '')}</code>\nТариф: <b>{tp['plan']}</b>\nШаблонов: {len(tp['templates'])}\nБаланс: {tp['balance']} ₽\nСессия: {'✅' if tp['session'] else '❌'}", parse_mode="html")
                return
            if text == ".stats":
                total = await repo.count_users(); approved = await repo.count_approved(); by_plan = await repo.count_by_plan(); pay = await repo.get_payment_stats()
                await e.reply(f"📊 <b>Статистика</b>\n👥 Юзеров: {total}\n✅ Доступ: {approved}\n⚪ FREE: {by_plan.get('FREE', 0)}\n💎 PRO: {by_plan.get('PRO', 0)}\n👑 ULTIMATE: {by_plan.get('ULTIMATE', 0)}\n💰 Платежей: {pay['count']} на {pay['sum']} ₽", parse_mode="html")
                return
            if text == ".revenue":
                async with repo.db.conn() as db:
                    async with db.execute("SELECT plan, COUNT(*), SUM(amount) FROM payments_log GROUP BY plan") as cur: rows = await cur.fetchall()
                if not rows: return await e.reply("💰 Пока нет платежей")
                lines = ["💰 <b>Выручка</b>"]
                for plan_name, count, amount in rows:
                    emoji = {"PRO": "💎", "ULTIMATE": "👑"}.get(plan_name, "⚪")
                    lines.append(f"{emoji} <b>{plan_name}</b>: {count} шт — <b>{amount} ₽</b>")
                await e.reply("\n".join(lines), parse_mode="html")
                return
            if text == ".users":
                users = await repo.get_all_users(50)
                await e.reply("👥 <b>Юзеры:</b>\n" + "\n".join(f"• <code>{u}</code>" for u in users), parse_mode="html")
                return
            if text.startswith(".broadcast"):
                body = text[len(".broadcast"):].strip()
                if not body: return await e.reply("❌ <code>.broadcast текст</code>", parse_mode="html")
                users = await repo.get_approved_users(); sent = failed = 0
                await e.reply(f"⏳ Рассылка на {len(users)} юзеров...")
                for u in users:
                    try:
                        await bot.send_message(u, body, parse_mode="html"); sent += 1; await asyncio.sleep(0.05)
                    except: failed += 1
                await e.reply(f"✅ Отправлено: {sent}, ошибок: {failed}")
                return
            if text == ".id": return await e.reply(f"Ваш ID: <code>{uid}</code>", parse_mode="html")
        except (IndexError, ValueError):
            return await e.reply("❌ Неверный формат команды")

    if not is_admin and not is_approved: return
    if not p.get("accepted_rules"): return

    step = user_steps.get(uid, "IDLE")

    if step == "ST_PHONE":
        await e.reply("⏳ Запрашиваю код...")
        c = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await c.connect(); req = await c.send_code_request(text)
            active_clients[f"temp_{uid}"] = {"client": c, "phone": text, "hash": req.phone_code_hash}
            user_steps[uid] = "ST_CODE"
            await e.reply("📩 <b>Код отправлен</b>\nВведите код:", buttons=[[Button.inline("📷 Войти по QR", b"qr_start")], [Button.inline("📞 Запросить звонок", b"resend_call")], [Button.inline("🔙 Отмена", b"nav_home")]], parse_mode="html")
        except Exception as ex:
            await e.reply(f"❌ <code>{str(ex)[:200]}</code>", parse_mode="html"); user_steps[uid] = "IDLE"

    elif step == "ST_CODE":
        b = active_clients.get(f"temp_{uid}")
        if not b: return
        try:
            await b["client"].sign_in(phone=b["phone"], code=text, phone_code_hash=b["hash"])
            await repo.save(uid, session=b["client"].session.save())
            active_clients[uid] = b["client"]; active_clients.pop(f"temp_{uid}", None); user_steps[uid] = "IDLE"
            await e.reply("✅ Сессия создана! /start")
        except SessionPasswordNeededError:
            user_steps[uid] = "ST_2FA"; await e.reply("🔐 Введите пароль 2FA:")
        except PhoneCodeInvalidError: await e.reply("❌ Неверный код")
        except PhoneCodeExpiredError: user_steps[uid] = "IDLE"; await e.reply("❌ Код истёк, /start")
        except Exception as ex: user_steps[uid] = "IDLE"; await e.reply(f"❌ <code>{str(ex)[:200]}</code>", parse_mode="html")

    elif step == "ST_2FA":
        b = active_clients.get(f"temp_{uid}")
        if not b: return
        try:
            await b["client"].sign_in(password=text)
            await repo.save(uid, session=b["client"].session.save())
            active_clients[uid] = b["client"]; active_clients.pop(f"temp_{uid}", None); user_steps[uid] = "IDLE"
            await e.reply("✅ 2FA пройдена! /start")
        except Exception as ex: await e.reply(f"❌ <code>{str(ex)[:200]}</code>", parse_mode="html")

    elif step == "ST_TARGET":
        user_steps[uid] = "IDLE"
        raw = [line.strip() for line in text.split("\n") if line.strip()]
        targets, bad = [], []
        for r in raw:
            n = normalize_target(r)
            if n: targets.append(n)
            else: bad.append(r)
        if not targets: return await e.reply("❌ Не удалось распознать цели")
        if len(targets) > limits["max_targets"]: return await e.reply(f"⚠️ Тариф <b>{plan}</b> разрешает <b>{limits['max_targets']}</b> целей. У тебя: {len(targets)}.\n\nПовысь тариф: /start → 💎", parse_mode="html")
        daily_count = await repo.get_daily_count(uid)
        if daily_count >= limits["daily_limit"]: return await e.reply(f"⚠️ Дневной лимит {limits['daily_limit']} исчерпан", parse_mode="html")
        if uid in spam_engines:
            spam_engines[uid].stop(); await asyncio.sleep(0.5)
        c = await resolve_client(uid)
        if not c: return await e.reply("❌ Сессия потеряна, /start → Аккаунт")
        eng = SpamEngine(c, uid, DB_FILE, p["min_delay"], p["max_delay"], system_metrics, protection=limits["flood_protection"], auto_pause=limits["auto_pause"], repo=repo, schedule=p.get("schedule"), blacklist=p.get("blacklist", []), whitelist=p.get("whitelist", []), stop_words=p.get("stop_words", []), random_targets=limits.get("random_targets", False), ab_test=limits.get("ab_test", False), webhook_url=p.get("webhook_url"))
        spam_engines[uid] = eng; asyncio.create_task(eng.start(targets, p["templates"]))
        msg = "🚀 <b>Запущено</b>\n\n<b>Цели:</b>\n" + "\n".join(f"• <code>{t}</code>" for t in targets)
        if bad: msg += f"\n\n⚠️ Не распознано: {len(bad)}"
        msg += f"\n\nЗадержка: {int(p['min_delay']/1000)}–{int(p['max_delay']/1000)}с"
        await e.reply(msg, parse_mode="html")

    elif step == "ST_DELAY":
        try:
            parts = text.replace(" ", "").split("-")
            if len(parts) != 2: raise ValueError
            low, high = map(int, parts)
            if low < limits["min_delay"]: return await e.reply(f"⚠️ Минимум {limits['min_delay']}с для <b>{plan}</b>", parse_mode="html")
            if high > limits["max_delay"]: return await e.reply(f"⚠️ Максимум {limits['max_delay']}с")
            if high < low: return await e.reply("⚠️ Максимум меньше минимума")
            
            await repo.save(uid, min_delay=low * 1000, max_delay=high * 1000)
            if uid in spam_engines and spam_engines[uid].is_active:
                spam_engines[uid].update_delay(low * 1000, high * 1000)
                msg_extra = "\n\n⚡ <b>Применено к активной сессии без остановки!</b>"
            else:
                msg_extra = ""
                
            user_steps[uid] = "IDLE"
            await e.reply(f"✅ Задержка: <b>{low}–{high}с</b>" + msg_extra, buttons=main_menu(is_admin), parse_mode="html")
        except (ValueError, AttributeError):
            await e.reply(f"❌ Формат: <code>{limits['min_delay']}-{limits['max_delay']}</code>", parse_mode="html")

    elif step == "ST_BLACKLIST":
        user_steps[uid] = "IDLE"; cur = set(p.get("blacklist", [])); added = 0
        for line in text.split("\n"):
            n = normalize_target(line)
            if n and n not in cur: cur.add(n); added += 1
        await repo.save(uid, blacklist=list(cur))
        await e.reply(f"🚫 Добавлено: <b>{added}</b>. Всего: {len(cur)}", buttons=[[Button.inline("🔙 К списку", b"nav_blacklist")]], parse_mode="html")
    elif step == "ST_WHITELIST":
        user_steps[uid] = "IDLE"; cur = set(p.get("whitelist", [])); added = 0
        for line in text.split("\n"):
            n = normalize_target(line)
            if n and n not in cur: cur.add(n); added += 1
        await repo.save(uid, whitelist=list(cur))
        await e.reply(f"✅ Добавлено: <b>{added}</b>. Всего: {len(cur)}", buttons=[[Button.inline("🔙 К списку", b"nav_whitelist")]], parse_mode="html")
    elif step == "ST_STOP_WORDS":
        user_steps[uid] = "IDLE"; cur = set(p.get("stop_words", [])); added = 0
        for line in text.split("\n"):
            w = line.strip().lower()
            if w and w not in cur: cur.add(w); added += 1
        await repo.save(uid, stop_words=list(cur))
        await e.reply(f"🛑 Добавлено: <b>{added}</b>. Всего: {len(cur)}", buttons=[[Button.inline("🔙 К списку", b"nav_stop_words")]], parse_mode="html")
    elif step == "ST_SCHEDULE":
        user_steps[uid] = "IDLE"
        try:
            parts = text.split()
            days_part = parts[0]; days = []
            for chunk in days_part.split(","):
                if "-" in chunk: a, b_ = map(int, chunk.split("-")); days.extend(range(a, b_ + 1))
                else: days.append(int(chunk))
            from_t, to_t = parts[1], parts[2]
            datetime.datetime.strptime(from_t, "%H:%M"); datetime.datetime.strptime(to_t, "%H:%M")
            sch = {"days": sorted(set(days)), "from": from_t, "to": to_t}
            await repo.save(uid, schedule=sch)
            await e.reply(f"✅ Сохранено: дни <code>{sch['days']}</code>, часы <code>{from_t}—{to_t}</code>", buttons=[[Button.inline("🔙 К расписанию", b"nav_schedule")]], parse_mode="html")
        except: await e.reply("❌ Формат: <code>1-5 09:00 23:00</code>", parse_mode="html")
    elif step == "ST_WEBHOOK":
        user_steps[uid] = "IDLE"
        if text.lower() == "off":
            await repo.save(uid, webhook_url=None); return await e.reply("✅ Webhook отключён", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
        if not text.startswith("http"): return await e.reply("❌ URL должен начинаться с http:// или https://")
        await repo.save(uid, webhook_url=text)
        await e.reply(f"✅ Webhook сохранён:\n<code>{text}</code>", buttons=[[Button.inline("🔙 Назад", b"nav_home")]], parse_mode="html")
    elif step == "IDLE":
        new_lines = [line for line in text.split("\n") if line.strip()]; cur = list(p.get("templates", [])); added = skipped = 0
        for line in new_lines:
            if line in cur: continue
            if len(cur) >= limits["templates"]: skipped += 1; continue
            cur.append(line); added += 1
        if added > 0:
            await repo.save(uid, templates=cur)
            max_t = limits['templates'] if limits['templates'] < 9999 else '∞'
            msg = f"📝 Добавлено: <b>{added}</b>. Всего: <b>{len(cur)}/{max_t}</b>"
            if skipped: msg += f"\n⚠️ Пропущено {skipped} — лимит тарифа."
            await e.reply(msg, parse_mode="html")
        elif skipped: await e.reply(f"⚠️ Лимит шаблонов для <b>{plan}</b> — {limits['templates']}\nПовысь тариф: /start → 💎", parse_mode="html")
        else: await e.reply("⚠️ Эти шаблоны уже есть")


# =====================================================================
# 9. WEB DASHBOARD + API
# =====================================================================
DASHBOARD_HTML = r"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TITAN Admin</title>
<style>
:root{--bg:#0a0e14;--card:#141a22;--line:#1f2933;--txt:#e8eef5;--muted:#7a8899;--acc:#4ea8de;--ok:#3ddc97;--warn:#ffb703;--err:#ef476f;--gold:#f4c542}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;min-height:100vh;padding:20px}
.wrap{width:100%;max-width:1200px;margin:0 auto}
.header{text-align:center;margin-bottom:24px}
.logo{font-size:36px;font-weight:800;letter-spacing:-1px;background:linear-gradient(135deg,#4ea8de,#3ddc97);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.sub{color:var(--muted);font-size:13px;margin-top:4px;letter-spacing:2px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px}
.card .label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:10px}
.card .value{font-size:30px;font-weight:700;font-variant-numeric:tabular-nums}
.card.ok .value{color:var(--ok)}.card.warn .value{color:var(--warn)}
.card.err .value{color:var(--err)}.card.acc .value{color:var(--acc)}.card.gold .value{color:var(--gold)}
.section{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:20px}
.section h2{font-size:16px;text-transform:uppercase;letter-spacing:1.5px;color:var(--muted);margin-bottom:16px;font-weight:600}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--muted);font-weight:500;padding:8px;border-bottom:1px solid var(--line);font-size:11px;text-transform:uppercase;letter-spacing:1px}
td{padding:10px 8px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(78,168,222,.05)}
.plan{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:600}
.plan.FREE{background:#1f2933;color:#8899aa}
.plan.PRO{background:#1e3a5f;color:#4ea8de}
.plan.ULTIMATE{background:#3d2f00;color:var(--gold)}
.ok{color:var(--ok)}.err{color:var(--err)}.muted{color:var(--muted)}
.tabs{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}
.tab{padding:8px 16px;border-radius:8px;background:var(--line);cursor:pointer;font-size:13px}
.tab.active{background:var(--acc);color:#0a0e14;font-weight:600}
.hidden{display:none}
.footer{text-align:center;color:var(--muted);font-size:12px;margin-top:20px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);margin-right:6px;animation:pulse 1.6s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.refresh-btn{background:var(--acc);color:#0a0e14;border:none;padding:6px 14px;border-radius:8px;font-weight:600;cursor:pointer;font-size:12px;margin-left:8px}
</style></head><body><div class="wrap">
<div class="header"><div class="logo">TITAN Enterprise</div><div class="sub">ADMIN CONTROL PANEL</div></div>
<div class="grid">
  <div class="card acc"><div class="label">Отправлено</div><div class="value" id="m">0</div></div>
  <div class="card ok"><div class="label">Активных сессий</div><div class="value" id="s">0</div></div>
  <div class="card gold"><div class="label">Выручка</div><div class="value" id="r">0 ₽</div></div>
  <div class="card warn"><div class="label">Юзеров</div><div class="value" id="u">0</div></div>
  <div class="card err"><div class="label">Ошибок</div><div class="value" id="e">0</div></div>
  <div class="card"><div class="label">FloodWait</div><div class="value" id="f">0</div></div>
</div>
<div class="tabs">
  <div class="tab active" data-tab="users">👥 Юзеры</div>
  <div class="tab" data-tab="payments">💰 Платежи</div>
  <div class="tab" data-tab="sessions">🔴 Сессии</div>
  <div class="tab" data-tab="audit">📜 Аудит</div>
</div>
<div class="section" id="tab-users">
  <h2>👥 Все юзеры <button class="refresh-btn" onclick="loadUsers()">🔄</button></h2>
  <table><thead><tr>
    <th>ID</th><th>Тариф</th><th>Баланс</th><th>Шаблонов</th><th>Сессия</th><th>Доступ</th><th>Admin</th>
  </tr></thead><tbody id="users-tbody"><tr><td colspan="7" class="muted">Загрузка...</td></tr></tbody></table>
</div>
<div class="section hidden" id="tab-payments">
  <h2>💰 Платежи <button class="refresh-btn" onclick="loadPayments()">🔄</button></h2>
  <table><thead><tr>
    <th>Дата</th><th>Юзер</th><th>Тариф</th><th>Сумма</th><th>Метод</th>
  </tr></thead><tbody id="payments-tbody"><tr><td colspan="5" class="muted">Загрузка...</td></tr></tbody></table>
</div>
<div class="section hidden" id="tab-sessions">
  <h2>🔴 Активные сессии <button class="refresh-btn" onclick="loadSessions()">🔄</button></h2>
  <table><thead><tr>
    <th>Юзер</th><th>Отправлено</th><th>Ошибок</th><th>Flood</th><th>Задержка</th><th>Работает</th>
  </tr></thead><tbody id="sessions-tbody"><tr><td colspan="6" class="muted">Загрузка...</td></tr></tbody></table>
</div>
<div class="section hidden" id="tab-audit">
  <h2>📜 Аудит <button class="refresh-btn" onclick="loadAudit()">🔄</button></h2>
  <table><thead><tr>
    <th>Дата</th><th>Админ</th><th>Действие</th><th>Цель</th><th>Детали</th>
  </tr></thead><tbody id="audit-tbody"><tr><td colspan="5" class="muted">Загрузка...</td></tr></tbody></table>
</div>
<div class="footer"><span class="dot"></span>Uptime: <span id="up">—</span></div>
<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const API = (path) => fetch(path + '?token=' + encodeURIComponent(TOKEN)).then(r => r.ok ? r.json() : null);

async function loadStats(){
  const d = await API('/api/stats');
  if(!d) return;
  document.getElementById('m').textContent = d.messages;
  document.getElementById('s').textContent = d.sessions;
  document.getElementById('r').textContent = d.revenue + ' ₽';
  document.getElementById('u').textContent = d.users;
  document.getElementById('e').textContent = d.errors;
  document.getElementById('f').textContent = d.flood;
  document.getElementById('up').textContent = d.uptime;
}

async function loadUsers(){
  const tbody = document.getElementById('users-tbody');
  tbody.innerHTML = '<tr><td colspan="7" class="muted">Загрузка...</td></tr>';
  const d = await API('/api/users');
  if(!d || !d.users.length){ tbody.innerHTML = '<tr><td colspan="7" class="muted">Пусто</td></tr>'; return; }
  tbody.innerHTML = d.users.map(u => `
    <tr>
      <td><code>${u.user_id}</code></td>
      <td><span class="plan ${u.plan}">${u.plan}</span></td>
      <td>${u.balance} ₽</td>
      <td>${u.templates}</td>
      <td>${u.has_session ? '<span class="ok">✅</span>' : '<span class="muted">—</span>'}</td>
      <td>${u.is_approved ? '<span class="ok">✅</span>' : '<span class="err">❌</span>'}</td>
      <td>${u.is_admin ? '👑' : ''}</td>
    </tr>
  `).join('');
}

async function loadPayments(){
  const tbody = document.getElementById('payments-tbody');
  tbody.innerHTML = '<tr><td colspan="5" class="muted">Загрузка...</td></tr>';
  const d = await API('/api/payments');
  if(!d || !d.payments.length){ tbody.innerHTML = '<tr><td colspan="5" class="muted">Пока нет платежей</td></tr>'; return; }
  tbody.innerHTML = d.payments.map(p => `
    <tr>
      <td>${p.created_at}</td>
      <td><code>${p.user_id}</code></td>
      <td><span class="plan ${p.plan}">${p.plan}</span></td>
      <td><b>${p.amount} ₽</b></td>
      <td class="muted">${p.method}</td>
    </tr>
  `).join('');
}

async function loadSessions(){
  const tbody = document.getElementById('sessions-tbody');
  tbody.innerHTML = '<tr><td colspan="6" class="muted">Загрузка...</td></tr>';
  const d = await API('/api/sessions');
  if(!d || !d.sessions.length){ tbody.innerHTML = '<tr><td colspan="6" class="muted">Нет активных рассылок</td></tr>'; return; }
  tbody.innerHTML = d.sessions.map(s => `
    <tr>
      <td><code>${s.user_id}</code></td>
      <td><b>${s.sent}</b></td>
      <td>${s.errors}</td>
      <td>${s.flood}</td>
      <td>${s.min_s}–${s.max_s}с</td>
      <td>${s.uptime}</td>
    </tr>
  `).join('');
}

async function loadAudit(){
  const tbody = document.getElementById('audit-tbody');
  tbody.innerHTML = '<tr><td colspan="5" class="muted">Загрузка...</td></tr>';
  const d = await API('/api/audit');
  if(!d || !d.audit.length){ tbody.innerHTML = '<tr><td colspan="5" class="muted">Пока пусто</td></tr>'; return; }
  tbody.innerHTML = d.audit.map(a => `
    <tr>
      <td>${a.created_at}</td>
      <td><code>${a.admin_id}</code></td>
      <td><b>${a.action}</b></td>
      <td><code>${a.target_id || '—'}</code></td>
      <td class="muted">${a.details || '—'}</td>
    </tr>
  `).join('');
}

document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.section').forEach(x => x.classList.add('hidden'));
    t.classList.add('active');
    document.getElementById('tab-' + t.dataset.tab).classList.remove('hidden');
    const tab = t.dataset.tab;
    if(tab === 'users') loadUsers();
    if(tab === 'payments') loadPayments();
    if(tab === 'sessions') loadSessions();
    if(tab === 'audit') loadAudit();
  });
});

loadStats(); loadUsers();
setInterval(loadStats, 5000);
setInterval(() => {
  const active = document.querySelector('.tab.active');
  if(!active) return;
  if(active.dataset.tab === 'sessions') loadSessions();
  if(active.dataset.tab === 'users') loadUsers();
}, 5000);
</script></body></html>"""

app = Flask("titan")
app.config["JSON_AS_ASCII"] = False
START_TIME = datetime.datetime.now()

def _check_auth():
    if not DASHBOARD_TOKEN: abort(503, "DASHBOARD_TOKEN не задан")
    got = request.headers.get("X-Token") or request.args.get("token", "")
    if not hmac.compare_digest(got, DASHBOARD_TOKEN): abort(401)

@app.route("/healthz")
def health(): return jsonify(status="OK"), 200

@app.route("/api/stats")
def stats_api():
    _check_auth()
    async def _fetch():
        async with repo.db.conn() as db:
            async with db.execute("SELECT COUNT(*) FROM users") as cur: users = (await cur.fetchone())[0]
            async with db.execute("SELECT COALESCE(SUM(amount),0) FROM payments_log") as cur: revenue = (await cur.fetchone())[0]
        return users, revenue
    users, revenue = asyncio.run_coroutine_threadsafe(_fetch(), bot.loop).result()
    return jsonify(messages=system_metrics["total_messages_sent"], sessions=len(spam_engines), errors=system_metrics["total_errors_caught"], flood=system_metrics["total_flood_wait_events"], uptime=str(datetime.datetime.now() - START_TIME).split(".")[0], users=users, revenue=int(revenue))

@app.route("/api/users")
def api_users():
    _check_auth()
    async def _fetch():
        async with repo.db.conn() as db:
            async with db.execute("SELECT user_id, plan, balance, templates, session_string, is_approved, is_admin FROM users ORDER BY user_id LIMIT 500") as cur: return await cur.fetchall()
    rows = asyncio.run_coroutine_threadsafe(_fetch(), bot.loop).result()
    return jsonify(users=[{"user_id": r[0], "plan": r[1] or "FREE", "balance": r[2] or 0, "templates": len(json.loads(r[3] or "[]")), "has_session": bool(r[4]), "is_approved": r[5], "is_admin": r[6]} for r in rows])

@app.route("/api/payments")
def api_payments():
    _check_auth()
    async def _fetch():
        async with repo.db.conn() as db:
            async with db.execute("SELECT created_at, user_id, plan, amount, method FROM payments_log ORDER BY id DESC LIMIT 100") as cur: return await cur.fetchall()
    rows = asyncio.run_coroutine_threadsafe(_fetch(), bot.loop).result()
    return jsonify(payments=[{"created_at": r[0], "user_id": r[1], "plan": r[2], "amount": r[3], "method": r[4]} for r in rows])

@app.route("/api/sessions")
def api_sessions():
    _check_auth(); items = []
    for uid, eng in spam_engines.items():
        up = datetime.datetime.now() - eng.started_at
        h, m, s = up.seconds // 3600, up.seconds % 3600 // 60, up.seconds % 60
        items.append({"user_id": uid, "sent": eng.sent, "errors": eng.errors, "flood": eng.pacer.flood_count, "min_s": eng.pacer.base_min // 1000, "max_s": eng.pacer.base_max // 1000, "uptime": f"{h:02d}:{m:02d}:{s:02d}"})
    return jsonify(sessions=items)

@app.route("/api/audit")
def api_audit():
    _check_auth()
    async def _fetch():
        async with repo.db.conn() as db:
            async with db.execute("SELECT created_at, admin_id, action, target_id, details FROM audit_log ORDER BY id DESC LIMIT 100") as cur: return await cur.fetchall()
    rows = asyncio.run_coroutine_threadsafe(_fetch(), bot.loop).result()
    return jsonify(audit=[{"created_at": r[0], "admin_id": r[1], "action": r[2], "target_id": r[3], "details": r[4]} for r in rows])

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
        await repo.save(admin_id, is_admin=1, is_approved=1, plan="ULTIMATE", accepted_rules=1)
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
                except: pass

if __name__ == "__main__":
    log.info("=" * 50)
    log.info("  TITAN Enterprise v6.0 — старт")
    log.info("=" * 50)
    try:
        asyncio.run(main())
    except AccessTokenInvalidError:
        log.critical("Неверный BOT_TOKEN")
    except KeyboardInterrupt:
        log.info("Остановлено")
