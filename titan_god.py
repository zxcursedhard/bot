#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TITAN Enterprise v7.4 — single-file Telegram manager.
Исправлен запуск БД на Python 3.14. Добавлено авто-возобновление после снятия мута.
"""

from __future__ import annotations

import os
import sys
import io
import csv
import hmac
import html
import json
import time
import asyncio
import logging
import secrets
import shutil
import threading
import datetime as dt
import re
import random
from pathlib import Path
from typing import Optional
from collections import defaultdict, deque

import aiosqlite
from telethon import TelegramClient, events, Button
from telethon.errors import (
    FloodWaitError, RPCError, ChannelPrivateError, ChatForbiddenError,
    ChatWriteForbiddenError, ChatAdminRequiredError, UserBannedInChannelError,
    SlowModeWaitError, PeerFloodError
)
from flask import Flask, jsonify, request, abort, Response

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

API_ID = int(os.getenv("API_ID", "2040"))
API_HASH = os.getenv("API_HASH", "b18441a1ff607e10a989891a5462e627")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8732669492:AAF6lfPEgcOp9iKzo74UGnPIPw9wfVa7YGo")
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "titan_super_secret_change_me_12345")

SUPER_ADMINS = {
    int(item.strip())
    for item in os.getenv("SUPER_ADMINS", "8655682794,8773171051").split(",")
    if item.strip()
}

PORT = int(os.getenv("PORT", "8080"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
DB_FILE = Path(os.getenv("DB_FILE", "/var/data/titan_database.db" if Path("/var/data").is_dir() else "titan_database.db"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(DB_FILE.parent / "database_backups")))
BACKUP_INTERVAL_HOURS = max(1, int(os.getenv("BACKUP_INTERVAL_HOURS", "6")))
MAX_BROADCAST_PER_MINUTE = max(1, int(os.getenv("MAX_BROADCAST_PER_MINUTE", "20")))
BROADCAST_GAP_SECONDS = max(1.0, float(os.getenv("BROADCAST_GAP_SECONDS", "3")))
OWNER_CONTACT = os.getenv("OWNER_CONTACT", "@apemiru")
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", OWNER_CONTACT)

PLANS = {
    "FREE": {"title": "Новичок", "badge": "⚪", "price": 0, "daily_limit": 50, "max_targets": 1, "min_delay": 50},
    "PRO": {"title": "PRO-юзер", "badge": "💎", "price": 490, "daily_limit": 500, "max_targets": 2, "min_delay": 30},
    "ULTIMATE": {"title": "ULTIMATE", "badge": "👑", "price": 1490, "daily_limit": 2000, "max_targets": 10, "min_delay": 1},
}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("TITAN")

# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    plan TEXT NOT NULL DEFAULT 'FREE',
    is_admin INTEGER NOT NULL DEFAULT 0,
    is_approved INTEGER NOT NULL DEFAULT 1,
    subscribed INTEGER NOT NULL DEFAULT 0,
    accepted_rules INTEGER NOT NULL DEFAULT 0,
    balance INTEGER NOT NULL DEFAULT 0,
    registered_at TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    plan TEXT NOT NULL,
    amount INTEGER NOT NULL,
    method TEXT NOT NULL DEFAULT 'manual',
    admin_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    target_id INTEGER,
    details TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broadcast_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    body_preview TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    messages_sent INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(date, user_id)
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
"""

class Database:
    def __init__(self, path: Path, backup_dir: Path):
        self.path = path
        self.backup_dir = backup_dir
        self.lock = asyncio.Lock()

    def connect(self):
        # Возвращает контекстный менеджер. Никаких async def здесь не нужно.
        return aiosqlite.connect(str(self.path), timeout=30)

    async def init(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        # ИСПРАВЛЕНО: убран await перед self.connect()
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("PRAGMA busy_timeout=30000")
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript(SCHEMA)
            async with db.execute("PRAGMA table_info(users)") as cur:
                cols = {row[1] for row in await cur.fetchall()}
            additions = {
                "subscribed": "INTEGER NOT NULL DEFAULT 0",
                "last_seen": "TEXT",
                "registered_at": "TEXT",
                "is_approved": "INTEGER NOT NULL DEFAULT 1",
                "is_admin": "INTEGER NOT NULL DEFAULT 0",
                "plan": "TEXT NOT NULL DEFAULT 'FREE'",
                "accepted_rules": "INTEGER NOT NULL DEFAULT 0",
                "balance": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in additions.items():
                if name not in cols:
                    await db.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
            now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            await db.execute(
                "UPDATE users SET registered_at=COALESCE(registered_at, ?), "
                "last_seen=COALESCE(last_seen, ?)",
                (now, now),
            )
            await db.execute("CREATE INDEX IF NOT EXISTS idx_users_subscribed ON users(subscribed)")
            await db.commit()
        log.info("База данных готова: %s", self.path)

    async def ensure_user(self, uid: int):
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        async with self.connect() as db:
            await db.execute(
                """INSERT INTO users(user_id, plan, is_admin, is_approved,
                   subscribed, accepted_rules, balance, registered_at, last_seen)
                   VALUES(?, 'FREE', ?, 1, 0, 0, 0, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen""",
                (uid, int(uid in SUPER_ADMINS), now, now),
            )
            if uid in SUPER_ADMINS:
                await db.execute(
                    "UPDATE users SET is_admin=1, is_approved=1, plan='ULTIMATE', accepted_rules=1 WHERE user_id=?",
                    (uid,),
                )
            await db.commit()

    async def get_user(self, uid: int):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users WHERE user_id=?", (uid,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def set_user(self, uid: int, **fields):
        allowed = {"plan", "is_admin", "is_approved", "subscribed", "accepted_rules", "balance"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        columns = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [uid]
        async with self.connect() as db:
            await db.execute(f"UPDATE users SET {columns} WHERE user_id=?", values)
            await db.commit()

    async def subscribers(self):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT user_id FROM users WHERE subscribed=1 AND is_approved=1 ORDER BY user_id"
            ) as cur:
                return [row["user_id"] for row in await cur.fetchall()]

    async def count(self):
        async with self.connect() as db:
            async with db.execute("SELECT COUNT(*) AS n FROM users") as cur:
                row = await cur.fetchone()
                return row["n"] if row else 0

    async def payment_stats(self):
        async with self.connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT COALESCE(SUM(amount),0) AS total, COUNT(*) AS n FROM payments_log"
            ) as cur:
                row = await cur.fetchone()
                return (int(row["total"]), int(row["n"])) if row else (0, 0)

    async def log_audit(self, admin_id, action, target_id=None, details=""):
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO audit_log(admin_id, action, target_id, details, created_at) VALUES(?,?,?,?,?)",
                (admin_id, action, target_id, details[:1000],
                 dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")),
            )
            await db.commit()

    async def log_broadcast(self, admin_id, preview, sent, failed):
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO broadcast_log(admin_id, body_preview, sent, failed, created_at) VALUES(?,?,?,?,?)",
                (admin_id, preview[:300], sent, failed,
                 dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")),
            )
            await db.commit()

    async def backup(self):
        async with self.lock:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            destination = self.backup_dir / f"titan_{stamp}.db"
            async with self.connect() as source:
                target = await aiosqlite.connect(str(destination))
                try:
                    await source.backup(target)
                finally:
                    await target.close()
            backups = sorted(self.backup_dir.glob("titan_*.db"))
            for old in backups[:-10]:
                try:
                    old.unlink()
                except OSError:
                    log.warning("Не удалось удалить старый backup: %s", old)
            log.info("Резервная копия создана: %s", destination)

    async def backup_loop(self):
        while True:
            await asyncio.sleep(BACKUP_INTERVAL_HOURS * 3600)
            try:
                await self.backup()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка резервного копирования")

# ---------------------------------------------------------------------
# Bot state and helpers
# ---------------------------------------------------------------------

db: Optional[Database] = None
bot: Optional[TelegramClient] = TelegramClient("titan_bot", API_ID, API_HASH)
started_at = time.monotonic()
broadcast_lock = asyncio.Lock()
rate_buckets = defaultdict(deque)
metrics = {"sent": 0, "failed": 0, "flood_wait": 0, "broadcasts": 0}

spam_engines = {}

def is_admin(uid: int) -> bool:
    return uid in SUPER_ADMINS

def rate_limited(uid: int, limit=5, period=10) -> bool:
    now = time.monotonic()
    bucket = rate_buckets[uid]
    while bucket and now - bucket[0] > period:
        bucket.popleft()
    if len(bucket) >= limit:
        return True
    bucket.append(now)
    return False

def plan_text(plan: str) -> str:
    item = PLANS.get(plan, PLANS["FREE"])
    return f"{item['badge']} <b>{html.escape(plan)}</b> — {item['price']} ₽/мес"

_TG_URL = re.compile(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})")
_TG_RESOLVE = re.compile(r"(?:https?://)?t\.me/c/(\d+)")
_TG_APP_CHAT = re.compile(r"tg://(?:chat|resolve)\?id=(-?\d+)")
_TG_APP_OPEN = re.compile(r"tg://openmessage\?user_id=(-?\d+)")
_NUMERIC = re.compile(r"^-?\d{5,}$")

def parse_target(raw: str):
    raw = raw.strip()
    if not raw: return None
    m = _TG_APP_CHAT.search(raw)
    if m: return int(m.group(1))
    m = _TG_APP_OPEN.search(raw)
    if m: return int(m.group(1))
    m = _TG_RESOLVE.search(raw)
    if m: return int("-100" + m.group(1))
    m = _TG_URL.search(raw)
    if m: return "@" + m.group(1)
    if raw.startswith("@") and len(raw) > 1: return raw
    if _NUMERIC.match(raw): return int(raw)
    return None

class SpamEngine:
    def __init__(self, client, targets, templates, min_ms, max_ms, metrics):
        self.client = client
        self.targets = targets
        self.templates = templates
        self.min_ms = min_ms
        self.max_ms = max_ms
        self.metrics = metrics
        self._stop_event = asyncio.Event()
        self._tasks = set()
        self.sent = 0
        self.errors = 0
        self.started_at = dt.datetime.now()

    @property
    def is_active(self):
        return not self._stop_event.is_set()

    async def _sleep(self, seconds):
        if seconds <= 0: return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _attack_one(self, target):
        while not self._stop_event.is_set():
            try:
                msg = random.choice(self.templates)
                if isinstance(target, int):
                    try:
                        entity = await self.client.get_entity(target)
                        await self.client.send_message(entity, msg)
                    except (ValueError, ChannelPrivateError, ChatForbiddenError):
                        raise ChatWriteForbiddenError(request=None)
                else:
                    await self.client.send_message(target, msg)

                self.sent += 1
                self.metrics["sent"] += 1
                
                delay = random.randint(self.min_ms, self.max_ms) / 1000.0
                await self._sleep(delay)

            except FloodWaitError as e:
                self.metrics["flood_wait"] += 1
                log.warning(f"FloodWait в {target}: ждем {e.seconds} сек")
                await self._sleep(e.seconds + 5)
                
            except SlowModeWaitError as e:
                self.metrics["flood_wait"] += 1
                log.warning(f"SlowMode в {target}: ждем {e.seconds} сек")
                await self._sleep(e.seconds + 5)
                continue # После ожидания продолжаем писать
                
            except (ChatWriteForbiddenError, ChatAdminRequiredError) as e:
                # Скорее всего, аккаунт в муте. Ждем 5 минут и пробуем снова.
                self.errors += 1
                self.metrics["failed"] += 1
                log.warning(f"Мут/Запрет в {target}: ждем 5 минут...")
                await self._sleep(300)
                continue # Возобновляем после ожидания

            except UserBannedInChannelError:
                # Жесткий бан, писать дальше нет смысла
                self.errors += 1
                self.metrics["failed"] += 1
                log.warning(f"Аккаунт забанен в {target}. Остановка для этой цели.")
                break

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.errors += 1
                self.metrics["failed"] += 1
                log.error(f"Ошибка отправки в {target}: {e}")
                await self._sleep(5)

    async def start(self):
        self._stop_event.clear()
        tasks = [asyncio.create_task(self._attack_one(t)) for t in self.targets]
        self._tasks.update(tasks)
        for t in tasks: t.add_done_callback(self._tasks.discard)
        await asyncio.gather(*tasks, return_exceptions=True)

    def stop(self):
        self._stop_event.set()
        for t in list(self._tasks): t.cancel()

def main_keyboard(uid: int):
    rows = [
        [Button.inline("👤  ПРОФИЛЬ", b"profile"), Button.inline("💎  ТАРИФЫ", b"plans")],
        [Button.inline("🚀  СПАМ-ДВИЖОК", b"spam_menu"), Button.inline("⚙️  НАСТРОЙКИ", b"settings")],
        [Button.inline("📡  СТАТУС СИСТЕМЫ", b"status"), Button.inline("❔  ПОМОЩЬ", b"help")],
    ]
    if is_admin(uid):
        rows.append([Button.inline("👑  ЦЕНТР УПРАВЛЕНИЯ", b"admin")])
    return rows

async def show_home(event, uid):
    user = await db.get_user(uid)
    plan = user["plan"] if user else "FREE"
    subscribed = bool(user["subscribed"]) if user else False
    active = "🟢 АКТИВЕН" if uid in spam_engines and spam_engines[uid].is_active else "⚪ ВЫКЛ"
    await event.respond(
        "<b>ＴＩＴＡＮ  /  ENTERPRISE</b>\n"
        "<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        "<b>ПАНЕЛЬ ПОЛЬЗОВАТЕЛЯ</b>\n"
        f"┌ ID: <code>{uid}</code>\n"
        f"├ Тариф: {plan_text(plan)}\n"
        f"├ Спам: {active}\n"
        f"└ Уведомления: {'🟢 ON' if subscribed else '🔴 OFF'}\n\n"
        "<i>Выберите раздел управления ниже.</i>",
        buttons=main_keyboard(uid),
        parse_mode="html",
    )

# ---------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------

@bot.on(events.NewMessage(pattern=r"^/start(?:@\w+)?$"))
async def start_handler(event):
    uid = event.sender_id
    if not uid or rate_limited(uid): return
    await db.ensure_user(uid)
    user = await db.get_user(uid)
    if not user["accepted_rules"]:
        await event.respond(
            "<b>📜 Правила TITAN Enterprise</b>\n\n"
            "1. Новости и уведомления отправляются только подписчикам.\n"
            "2. Подписку можно отключить командой /unsubscribe.\n"
            "3. Запрещены спам, мошенничество и незаконное использование сервиса.\n\n"
            "Нажимая «Принимаю», вы соглашаетесь с правилами.",
            buttons=[[Button.inline("✅ Принимаю", b"accept")],
                     [Button.inline("❌ Отказываюсь", b"decline")]],
            parse_mode="html",
        )
        return
    await show_home(event, uid)

@bot.on(events.NewMessage(pattern=r"^/help(?:@\w+)?$"))
async def help_handler(event):
    await event.respond(
        "<b>Команды</b>\n"
        "/start — главное меню\n"
        "/profile — профиль\n"
        "/spam_start <цели> | <шаблоны> — бесконечный спам\n"
        "/spam_stop — остановить спам\n"
        "/help — помощь\n\n"
        "Администраторы: /broadcast ТЕКСТ, /approve ID, /revoke ID, /plan ID FREE|PRO|ULTIMATE",
        parse_mode="html",
    )

@bot.on(events.NewMessage(pattern=r"^/spam_start\s+([\s\S]+)$"))
async def spam_start_handler(event):
    uid = event.sender_id
    await db.ensure_user(uid)
    user = await db.get_user(uid)
    limits = PLANS.get(user["plan"], PLANS["FREE"])

    raw = event.pattern_match.group(1)
    if "|" not in raw:
        return await event.respond("❌ Формат: <code>/spam_start @user1, -100123 | Текст 1\nТекст 2</code>", parse_mode="html")
    
    targets_raw, templates_raw = raw.split("|", 1)
    targets = []
    for t in targets_raw.split(","):
        p = parse_target(t)
        if p: targets.append(p)
    
    if not targets: return await event.respond("❌ Не удалось распознать цели.")
    if len(targets) > limits["max_targets"]:
        return await event.respond(f"⚠️ Тариф <b>{user['plan']}</b> разрешает до <b>{limits['max_targets']}</b> целей.", parse_mode="html")

    templates = [line.strip() for line in templates_raw.split("\n") if line.strip()]
    if not templates: return await event.respond("❌ Нет шаблонов для отправки.")

    if uid in spam_engines:
        spam_engines[uid].stop()
        await asyncio.sleep(0.3)

    engine = SpamEngine(bot, targets, templates, limits["min_delay"] * 1000, 60 * 1000, metrics)
    spam_engines[uid] = engine
    asyncio.create_task(engine.start())

    msg = "🚀 <b>БЕСКОНЕЧНЫЙ СПАМ ЗАПУЩЕН</b>\n\n<b>Цели:</b>\n"
    for t in targets: msg += f"• <code>{t}</code>\n"
    msg += f"\n⚙️ Задержка: {limits['min_delay']}–60с\n🛑 Остановить: /spam_stop"
    await event.respond(msg, parse_mode="html")

@bot.on(events.NewMessage(pattern=r"^/spam_stop(?:@\w+)?$"))
async def spam_stop_handler(event):
    uid = event.sender_id
    if uid in spam_engines:
        spam_engines[uid].stop()
        del spam_engines[uid]
        await event.respond("🛑 <b>Спам остановлен.</b>", parse_mode="html")
    else:
        await event.respond("⚠️ У вас нет активных рассылок.")

@bot.on(events.NewMessage(pattern=r"^/profile(?:@\w+)?$"))
async def profile_handler(event):
    uid = event.sender_id
    await db.ensure_user(uid)
    user = await db.get_user(uid)
    await event.respond(
        "<b>👤 Профиль</b>\n"
        f"ID: <code>{uid}</code>\n"
        f"Тариф: {plan_text(user['plan'])}\n"
        f"Баланс: {user['balance']} ₽",
        parse_mode="html",
    )

@bot.on(events.NewMessage(pattern=r"^/broadcast(?:@\w+)?(?:\s+([\s\S]+))?$"))
async def broadcast_handler(event):
    uid = event.sender_id
    if not is_admin(uid): return await event.respond("⛔ Недостаточно прав.")
    body = (event.pattern_match.group(1) or "").strip()
    if not body: return await event.respond("Формат: <code>/broadcast Текст сообщения</code>", parse_mode="html")
    if broadcast_lock.locked(): return await event.respond("⚠️ Рассылка уже выполняется.")
    async with broadcast_lock:
        targets = await db.subscribers()
        if not targets: return await event.respond("Нет пользователей с активной подпиской.")
        sent = failed = 0
        status = await event.respond(f"📨 Начинаю отправку подписчикам: {len(targets)}")
        for index, target in enumerate(targets, 1):
            try:
                await bot.send_message(target, body, parse_mode=None)
                sent += 1
                metrics["sent"] += 1
            except Exception:
                failed += 1
                metrics["failed"] += 1
            await asyncio.sleep(BROADCAST_GAP_SECONDS)
        await db.log_broadcast(uid, body, sent, failed)
        await status.edit(f"✅ Рассылка завершена\nУспешно: {sent}\nОшибок: {failed}")

# ---------------------------------------------------------------------
# Callback buttons
# ---------------------------------------------------------------------

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    uid = event.sender_id
    action = event.data.decode("utf-8", errors="ignore")
    await db.ensure_user(uid)
    user = await db.get_user(uid)

    def back():
        return [[Button.inline("⌂  ГЛАВНОЕ МЕНЮ", b"home")]]

    if action == "accept":
        await db.set_user(uid, accepted_rules=1)
        await event.answer("Доступ активирован")
        await event.edit("<b>✅ ДОСТУП ОТКРЫТ</b>\nДобро пожаловать! Отправьте /start.", parse_mode="html")
    elif action == "decline":
        await db.set_user(uid, accepted_rules=0, subscribed=0)
        await event.edit("<b>ДОСТУП НЕ АКТИВИРОВАН</b>", parse_mode="html")
    elif action == "spam_menu":
        limits = PLANS.get(user["plan"], PLANS["FREE"])
        is_active = uid in spam_engines and spam_engines[uid].is_active
        status_text = "🟢 <b>РАБОТАЕТ</b>" if is_active else "⚪ <b>ОСТАНОВЛЕН</b>"
        text = (
            "<b>🚀  СПАМ-ДВИЖОК</b>\n"
            "<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
            f"Статус: {status_text}\n"
            f"Тариф: {plan_text(user['plan'])}\n"
            f"Задержка: от {limits['min_delay']} до 60 сек\n\n"
            "<b>Как запустить:</b>\n"
            "<code>/spam_start @user1, -100123 | Привет\nКак дела?</code>\n\n"
            "• Цели через запятую\n"
            "• Шаблоны с новой строки после символа <code>|</code>"
        )
        buttons = [
            [Button.inline("🛑  ОСТАНОВИТЬ", b"spam_stop")] if is_active else [],
            [Button.inline("⌂  Главное меню", b"home")]
        ]
        await event.edit(text, buttons=[b for b in buttons if b], parse_mode="html")
    elif action == "spam_stop":
        if uid in spam_engines:
            spam_engines[uid].stop()
            del spam_engines[uid]
            await event.answer("Остановлено", alert=True)
            await event.edit("<b>🛑 Спам остановлен.</b>", buttons=back(), parse_mode="html")
        else:
            await event.answer("Не активно", alert=True)
    elif action == "settings":
        enabled = bool(user["subscribed"])
        await event.edit(
            "<b>⚙️  ЦЕНТР НАСТРОЕК</b>\n"
            "<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
            "<b>УВЕДОМЛЕНИЯ</b>\n"
            f"Статус: {'🟢 АКТИВНЫ' if enabled else '🔴 ОТКЛЮЧЕНЫ'}\n\n"
            "Включите уведомления, чтобы получать новости TITAN.\n\n",
            buttons=[
                [Button.inline("🔔  Уведомления: ВКЛ" if enabled else "🔕  Уведомления: ВЫКЛ", b"toggle_notifications")],
                [Button.inline("👤  Профиль", b"profile"), Button.inline("💎  Тарифы", b"plans")],
                [Button.inline("⌂  Главное меню", b"home")]
            ], parse_mode="html")
    elif action == "toggle_notifications":
        enabled = not bool(user["subscribed"])
        await db.set_user(uid, subscribed=int(enabled))
        await event.answer("Сохранено")
        await event.edit(
            f"<b>⚙️  ЦЕНТР НАСТРОЕК</b>\n\n"
            f"Уведомления: {'🟢 ВКЛЮЧЕНЫ' if enabled else '🔴 ВЫКЛЮЧЕНЫ'}\n",
            buttons=[
                [Button.inline("🔔  Уведомления: ВКЛ" if enabled else "🔕  Уведомления: ВЫКЛ", b"toggle_notifications")],
                [Button.inline("⌂  Главное меню", b"home")]
            ], parse_mode="html")
    elif action == "profile":
        await event.edit(
            "<b>👤  ЛИЧНЫЙ ПРОФИЛЬ</b>\n"
            "<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
            f"🆔 ID: <code>{uid}</code>\n"
            f"💎 Тариф: {plan_text(user['plan'])}\n"
            f"💰 Баланс: <b>{user['balance']} ₽</b>\n"
            f"🛡 Статус: {'Администратор' if is_admin(uid) else 'Пользователь'}",
            buttons=[[Button.inline("⚙️ Настройки", b"settings"), Button.inline("💎 Тарифы", b"plans")], *back()],
            parse_mode="html")
    elif action == "plans":
        lines = ["<b>💎  ТАРИФНЫЕ ПЛАНЫ</b>", "<code>━━━━━━━━━━━━━━━━━━━━</code>"]
        for name, item in PLANS.items():
            lines.append(f"{item['badge']} <b>{name}</b> · {item['title']}\n   <b>{item['price']} ₽/мес</b> · задержка от {item['min_delay']}с")
        lines.append(f"\nСвязь: {html.escape(OWNER_CONTACT)}")
        await event.edit("\n".join(lines), buttons=back(), parse_mode="html")
    elif action == "help":
        await event.edit(
            "<b>❔  ЦЕНТР ПОМОЩИ</b>\n<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
            "/start — открыть меню\n/profile — профиль\n"
            "/spam_start @user, -100123 | Текст 1\nТекст 2 — запуск спама\n"
            "/spam_stop — остановить спам\n\n"
            f"Поддержка: {html.escape(SUPPORT_CONTACT)}",
            buttons=back(), parse_mode="html")
    elif action == "status":
        await event.edit(
            "<b>📡  СТАТУС TITAN</b>\n<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
            "🟢 Telegram API: доступен\n🟢 База данных: подключена\n"
            "🛡 Защита: активна\n⚡ Интерфейс: готов к работе",
            buttons=back(), parse_mode="html")
    elif action == "admin" and is_admin(uid):
        users = await db.count()
        revenue, payments = await db.payment_stats()
        subscribers = len(await db.subscribers())
        await event.edit(
            "<b>👑  TITAN / ADMIN CONTROL</b>\n<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
            f"👥 Пользователи: <b>{users}</b>\n📡 Подписчики: <b>{subscribers}</b>\n"
            f"💰 Выручка: <b>{revenue} ₽</b>\n🧾 Платежи: <b>{payments}</b>\n\n"
            "<b>КОМАНДЫ</b>\n<code>/broadcast ТЕКСТ</code> — объявление\n"
            "<code>/approve ID</code> — открыть доступ\n<code>/plan ID PLAN</code> — тариф",
            buttons=back(), parse_mode="html")
    elif action == "home":
        user = await db.get_user(uid)
        active = "🟢 АКТИВЕН" if uid in spam_engines and spam_engines[uid].is_active else "⚪ ВЫКЛ"
        await event.edit(
            "<b>ＴＩＴＡＮ  /  ENTERPRISE</b>\n<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
            "<b>ПАНЕЛЬ ПОЛЬЗОВАТЕЛЯ</b>\n"
            f"┌ ID: <code>{uid}</code>\n├ Тариф: {plan_text(user['plan'])}\n"
            f"├ Спам: {active}\n"
            f"└ Уведомления: {'🟢 ON' if user['subscribed'] else '🔴 OFF'}\n\n"
            "<i>Выберите нужный раздел.</i>",
            buttons=main_keyboard(uid), parse_mode="html")
    else:
        await event.answer("Недоступно", alert=True)

# ---------------------------------------------------------------------
# Flask dashboard
# ---------------------------------------------------------------------

app = Flask("titan")
app.config["JSON_AS_ASCII"] = False
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

def check_dashboard_auth():
    supplied = request.headers.get("X-Token", "")
    if not supplied or not hmac.compare_digest(supplied, DASHBOARD_TOKEN):
        abort(401)

@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response

@app.get("/healthz")
def healthz():
    return jsonify(status="ok")

def on_bot_loop(coro, timeout=15):
    loop = bot.loop
    if not loop or not loop.is_running():
        abort(503, "Bot loop is not ready")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout)
    except Exception:
        future.cancel()
        raise

@app.get("/")
def dashboard():
    html_page = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#08090d">
<title>TITAN Enterprise — Control Center</title>
<style>
:root{color-scheme:dark;--bg:#08090d;--panel:#10131b;--panel2:#151925;--line:#252b3a;--muted:#858da1;--text:#f4f5f8;--red:#f02d49;--red2:#a90e2b;--green:#37d99a}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 78% -10%,#79172b38,transparent 38%),radial-gradient(ellipse at 0% 80%,#1c253533,transparent 40%),var(--bg);color:var(--text);font:14px Inter,ui-sans-serif,system-ui,sans-serif;min-height:100vh}
.layout{max-width:1440px;margin:auto;padding:28px;display:grid;grid-template-columns:235px minmax(0,1fr);gap:22px}.side{background:#0d1017;border:1px solid var(--line);border-radius:22px;padding:22px;height:calc(100vh - 56px);min-height:600px;position:sticky;top:28px;display:flex;flex-direction:column}.brand{display:flex;align-items:center;gap:12px;margin-bottom:42px}.logo{width:44px;height:44px;border-radius:14px;display:grid;place-items:center;background:linear-gradient(145deg,#3a101c,#171923);border:1px solid #e52d4b88;color:#ff526b;font-size:24px;font-weight:950;box-shadow:0 0 28px #ed263d20}.brand b{letter-spacing:1.6px;font-size:14px}.brand small{display:block;color:var(--muted);font-size:9px;letter-spacing:2px;margin-top:4px}.navlabel{font-size:9px;letter-spacing:2px;color:#626b80;margin:0 0 12px 8px}.nav{display:grid;gap:7px}.nav a{padding:12px 13px;border-radius:11px;color:#9ca5b8;text-decoration:none;display:flex;gap:11px;align-items:center}.nav a.active,.nav a:hover{background:#28121b;color:#fff;border:1px solid #642334}.sidefoot{margin-top:auto;border-top:1px solid var(--line);padding-top:18px;color:#788196;font-size:11px;line-height:1.8}.online{color:var(--green)}main{min-width:0}.top{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:24px}.crumb{color:var(--muted);font-size:12px}.top h1{font-size:25px;letter-spacing:-.8px;margin:5px 0 0}.topright{display:flex;align-items:center;gap:10px}.pill{border:1px solid var(--line);border-radius:99px;padding:9px 12px;color:#aab2c3;font-size:10px;letter-spacing:1px;background:#10131bbd}.hero{padding:25px 28px;border:1px solid #612335;background:linear-gradient(110deg,#28111b,#13151e 64%,#19111a);border-radius:20px;margin-bottom:17px;position:relative;overflow:hidden}.hero:after{content:'T';position:absolute;right:34px;top:-62px;font-size:210px;font-weight:1000;color:#f02d490c}.eyebrow{color:#ff6378;font-size:9px;font-weight:800;letter-spacing:2.5px}.hero h2{font-size:clamp(21px,3vw,30px);margin:10px 0 7px}.hero p{margin:0;color:#a6adbd;max-width:620px;line-height:1.6;font-size:12px}.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:13px}.card,.panel{background:linear-gradient(145deg,#141824,#0f121a);border:1px solid var(--line);border-radius:16px;box-shadow:0 12px 30px #0003}.card{padding:19px;min-height:120px;position:relative;overflow:hidden}.card:after{content:'';position:absolute;width:90px;height:90px;border-radius:50%;right:-35px;bottom:-48px;background:#f02d4910;filter:blur(3px)}.kicker{font-size:9px;color:#858da1;letter-spacing:1.5px}.num{font-size:29px;font-weight:850;letter-spacing:-1px;margin-top:14px}.red{color:#ff536a}.green{color:var(--green)}.lower{display:grid;grid-template-columns:1.15fr .85fr;gap:14px;margin-top:14px}.panel{padding:19px;min-width:0}.panelhead{display:flex;align-items:center;justify-content:space-between;margin-bottom:15px}.panelhead b{font-size:13px}.panelhead span{font-size:9px;color:var(--muted);letter-spacing:1px}.authrow{display:flex;gap:9px;margin-top:14px}.authrow input{flex:1;min-width:0;background:#090c12;border:1px solid #303748;color:white;padding:12px 13px;border-radius:10px;outline:none}.authrow input:focus{border-color:var(--red)}button{background:linear-gradient(135deg,#fa3d59,#b91432);border:0;color:white;border-radius:10px;padding:11px 16px;font-weight:800;font-size:10px;letter-spacing:1px;cursor:pointer;box-shadow:0 7px 20px #ed263d25}button:hover{filter:brightness(1.15)}.muted{color:var(--muted);font-size:11px;line-height:1.6}#error{color:#ff7182;font-size:11px;margin-top:8px;white-space:pre-wrap}.tablewrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:11px;white-space:nowrap}td,th{text-align:left;padding:11px 9px;border-bottom:1px solid #252b38}th{color:#778196;font-size:9px;letter-spacing:1px;font-weight:700}td{color:#c5cad5}.tag{padding:4px 7px;border-radius:99px;background:#1b202c;color:#abb4c5;font-size:9px}.activity{display:grid;gap:10px}.event{padding:10px 11px;border-radius:10px;background:#0c1017;border:1px solid #202635;color:#aeb6c7;font-size:11px}.event b{color:#fff}.foot{color:#525c70;font-size:9px;letter-spacing:1.4px;margin:20px 2px}
@media(max-width:900px){.layout{grid-template-columns:1fr;padding:16px}.side{height:auto;min-height:0;position:static;padding:15px}.brand{margin-bottom:15px}.nav{grid-template-columns:repeat(4,1fr)}.navlabel,.sidefoot{display:none}.nav a{justify-content:center;font-size:11px;padding:10px 5px}.lower{grid-template-columns:1fr}}
@media(max-width:560px){.layout{padding:11px;gap:12px}.nav{grid-template-columns:repeat(2,1fr)}.nav a{justify-content:flex-start}.top{align-items:flex-start}.top h1{font-size:20px}.topright .pill{display:none}.hero{padding:20px}.stats{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.card{padding:14px;min-height:100px}.num{font-size:24px}.authrow{flex-direction:column}.authrow button{padding:13px}}
</style></head><body><div class="layout">
<aside class="side"><div class="brand"><div class="logo">T</div><div><b>TITAN ENTERPRISE</b><small>PRIVATE CONTROL</small></div></div>
<div class="navlabel">WORKSPACE</div><nav class="nav"><a class="active" href="#overview">◈ &nbsp; Обзор</a><a href="#users-section">♙ &nbsp; Пользователи</a><a href="#activity">◷ &nbsp; Активность</a><a href="#access">⚿ &nbsp; Доступ</a></nav>
<div class="sidefoot"><span class="online">● SYSTEM ONLINE</span><br>CONTROL CENTER / V7.4<br>SECURE ADMIN INTERFACE</div></aside>
<main id="overview"><header class="top"><div><div class="crumb">WORKSPACE / OVERVIEW</div><h1>Панель управления</h1></div><div class="topright"><div class="pill">● LIVE MONITORING</div><div class="pill">V7.4</div></div></header>
<section class="hero"><div class="eyebrow">TITAN COMMAND CENTER</div><h2>Полный контроль. Один интерфейс.</h2><p>Мониторинг пользователей, подписок и активности. Данные обновляются автоматически, доступ к API защищён отдельным ключом.</p></section>
<section class="stats"><article class="card"><div class="kicker">ВСЕГО ПОЛЬЗОВАТЕЛЕЙ</div><div class="num" id="users">—</div></article><article class="card"><div class="kicker">АКТИВНЫЕ ПОДПИСКИ</div><div class="num green" id="subs">—</div></article><article class="card"><div class="kicker">УСПЕШНЫЕ ОТПРАВКИ</div><div class="num red" id="sent">—</div></article><article class="card"><div class="kicker">ОШИБКИ ОТПРАВКИ</div><div class="num" id="failed">—</div></article><article class="card"><div class="kicker">FLOOD WAIT</div><div class="num" id="flood">—</div></article><article class="card"><div class="kicker">ОБОРОТ</div><div class="num" id="revenue">— ₽</div></article></section>
<section class="lower"><article class="panel" id="users-section"><div class="panelhead"><b>♙ Пользователи</b><span>LAST 500</span></div><div class="tablewrap"><table><thead><tr><th>USER ID</th><th>ТАРИФ</th><th>НОВОСТИ</th><th>ДОСТУП</th></tr></thead><tbody id="userRows"><tr><td colspan="4" class="muted">Подключите панель для загрузки данных</td></tr></tbody></table></div></article>
<article class="panel" id="activity"><div class="panelhead"><b>◷ Журнал активности</b><span>LAST 100</span></div><div class="activity" id="auditRows"><div class="muted">Здесь появятся последние действия администраторов.</div></div></article></section>
<section class="panel" id="access" style="margin-top:14px"><div class="panelhead"><b>⚿ Защищённое подключение</b><span>API AUTHENTICATION</span></div><div class="muted" id="state">Введите ключ доступа панели. Ключ хранится только в текущей сессии браузера.</div><div class="authrow"><input id="token" type="password" autocomplete="off" placeholder="Ключ доступа панели"><button onclick="saveToken()">ПОДКЛЮЧИТЬ</button><button onclick="logout()" style="background:#252b38;box-shadow:none">ВЫЙТИ</button></div><div id="error"></div></section>
<div class="foot">TITAN ENTERPRISE · PRIVATE ADMIN INTERFACE · AUTO REFRESH 10 SEC</div></main></div>
<script>
const $=id=>document.getElementById(id);function token(){return sessionStorage.getItem('titan_token')||''}
function saveToken(){const v=$('token').value.trim();if(v)sessionStorage.setItem('titan_token',v);$('token').value='';loadAll()}
function logout(){sessionStorage.removeItem('titan_token');$('state').textContent='Сессия завершена. Введите ключ доступа.';$('userRows').innerHTML='<tr><td colspan="4">Нет подключения</td></tr>';$('auditRows').innerHTML='';}
async function api(path){const r=await fetch(path,{headers:{'X-Token':token()},cache:'no-store'});if(!r.ok)throw Error(r.status===401?'Неверный ключ доступа':'HTTP '+r.status);return r.json()}
async function loadAll(){if(!token())return;try{const [d,u,a]=await Promise.all([api('/api/stats'),api('/api/users'),api('/api/audit')]);for(const k of ['users','subs','sent','failed','flood'])$(k).textContent=Number(d[k]).toLocaleString('ru-RU');$('revenue').textContent=Number(d.revenue).toLocaleString('ru-RU')+' ₽';$('state').textContent='● Подключено · uptime '+d.uptime;$('error').textContent='';$('userRows').innerHTML=u.users.map(x=>`<tr><td>${x.user_id}</td><td><span class="tag">${x.plan}</span></td><td>${x.subscribed?'🟢 ВКЛ':'—'}</td><td>${x.is_approved?'🟢':'🔴'}</td></tr>`).join('')||'<tr><td colspan="4">Пользователей пока нет</td></tr>';$('auditRows').innerHTML=a.audit.map(x=>`<div class="event"><b>${x.action}</b> · ${x.admin_id}${x.target_id?' → '+x.target_id:''}<br><span class="muted">${x.created_at}</span></div>`).join('')||'<div class="muted">Записей пока нет</div>';}catch(e){$('error').textContent='Ошибка подключения: '+e.message;$('state').textContent='● Нет подключения';}}
loadAll();setInterval(loadAll,10000);
</script></body></html>"""
    return Response(html_page, mimetype="text/html")

@app.get("/api/stats")
def api_stats():
    check_dashboard_auth()
    async def fetch():
        users = await db.count()
        subscribers = len(await db.subscribers())
        revenue, payments = await db.payment_stats()
        return users, subscribers, revenue, payments
    users, subscribers, revenue, payments = on_bot_loop(fetch())
    return jsonify(
        users=users, subs=subscribers, revenue=revenue, payments=payments,
        sent=metrics["sent"], failed=metrics["failed"], flood=metrics["flood_wait"],
        uptime=str(dt.timedelta(seconds=int(time.monotonic() - started_at))),
    )

@app.get("/api/users")
def api_users():
    check_dashboard_auth()
    async def fetch():
        async with await db.connect() as conn:
            async with conn.execute(
                "SELECT user_id, plan, subscribed, is_admin, is_approved, balance FROM users ORDER BY user_id DESC LIMIT 500"
            ) as cur:
                return [dict(row) for row in await cur.fetchall()]
    return jsonify(users=on_bot_loop(fetch()))

@app.get("/api/audit")
def api_audit():
    check_dashboard_auth()
    async def fetch():
        async with await db.connect() as conn:
            async with conn.execute(
                "SELECT created_at, admin_id, action, target_id, details FROM audit_log ORDER BY id DESC LIMIT 100"
            ) as cur:
                return [dict(row) for row in await cur.fetchall()]
    return jsonify(audit=on_bot_loop(fetch()))

def run_web():
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=PORT, threads=4)
    except ImportError:
        log.warning("waitress не установлен; используется встроенный Flask server")
        app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)

# ---------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------

async def main():
    global db, bot
    db = Database(DB_FILE, BACKUP_DIR)
    await db.init()
    await db.backup()

    await bot.start(bot_token=BOT_TOKEN)
    log.info("Telegram bot started")

    backup_task = asyncio.create_task(db.backup_loop(), name="database-backup")
    web_thread = threading.Thread(target=run_web, name="titan-web", daemon=True)
    web_thread.start()
    log.info("Dashboard started on port %s", PORT)

    try:
        await bot.run_until_disconnected()
    finally:
        backup_task.cancel()
        await asyncio.gather(backup_task, return_exceptions=True)
        await bot.disconnect()
        log.info("TITAN stopped")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")
    except Exception:
        log.exception("Критическая ошибка запуска")
        raise
