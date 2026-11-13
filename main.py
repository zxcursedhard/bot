import os
import re
import csv
import io
import json
import asyncio
import random
import threading
import datetime as dt
import urllib.parse
from functools import wraps

import aiosqlite
from flask import Flask, jsonify, request, render_template_string, session, redirect, url_for, Response
from waitress import serve

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError, SlowModeWaitError, ChatWriteForbiddenError,
    UserBannedInChannelError, SessionPasswordNeededError,
    PhoneCodeInvalidError, PasswordHashInvalidError
)

# ---------------- CONFIG ----------------

API_ID = int(os.getenv("API_ID", "2040"))
API_HASH = os.getenv("API_HASH", "b18441a1ff607e10a989891a5462e627")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8732669492:AAFocBFBmGu6XL0R0w7VYAOkxpnq1N18ef8")
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "titan_super_secret_change_me_12345")
FLASK_SECRET = os.getenv("FLASK_SECRET", "")
OWNER_CONTACT = "@apemiru"
SUPPORT_CONTACT = "@apemiru"
SUPER_ADMINS = {8655682794, 8773171051}

DB_PATH = os.getenv("DB_PATH", "/var/data/titan_enterprise.db")
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("PORT", "10000"))
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("PORT", "10000"))
SUBSCRIPTION_PRICE = 400
SUBSCRIPTION_DAYS = 60
MAX_TARGETS = 10

if not FLASK_SECRET:
    FLASK_SECRET = os.urandom(32).hex()

app = Flask(__name__)
app.secret_key = FLASK_SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

bot = None
loop = None
db = None
states = {}
phone_flows = {}
qr_flows = {}
dispatchers = {}
active_clients = {}

IDLE = "IDLE"
WAIT_PHONE = "WAIT_PHONE"
WAIT_CODE = "WAIT_CODE"
WAIT_2FA = "WAIT_2FA"
WAIT_TARGETS = "WAIT_TARGETS"
WAIT_DELAY = "WAIT_DELAY"
WAIT_SUB = "WAIT_SUB"


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


# ---------------- DATABASE ----------------

class Database:
    def __init__(self, path):
        self.path = path

    def connect(self):
        # Fixed for Python 3.14
        return aiosqlite.connect(self.path)

    async def initialize(self):
        async with self.connect() as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users(
                    user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    session_string TEXT NOT NULL DEFAULT '',
                    approved INTEGER NOT NULL DEFAULT 0,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    balance INTEGER NOT NULL DEFAULT 0,
                    subscription_until TEXT NOT NULL DEFAULT '',
                    templates TEXT NOT NULL DEFAULT '[]',
                    targets TEXT NOT NULL DEFAULT '[]',
                    min_delay INTEGER NOT NULL DEFAULT 3,
                    max_delay INTEGER NOT NULL DEFAULT 10,
                    created_at TEXT NOT NULL
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    sent INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0,
                    revenue INTEGER NOT NULL DEFAULT 0
                )
            """)
            await conn.execute("INSERT OR IGNORE INTO metrics(id) VALUES(1)")
            await conn.commit()

        expiry = (utcnow() + dt.timedelta(days=3650)).isoformat()
        async with self.connect() as conn:
            for uid in SUPER_ADMINS:
                await conn.execute("""
                    INSERT OR IGNORE INTO users(user_id, approved, is_admin, subscription_until, created_at)
                    VALUES(?,1,1,?,?)
                """, (uid, expiry, utcnow().isoformat()))
                await conn.execute("""
                    UPDATE users SET approved=1, is_admin=1, subscription_until=?
                    WHERE user_id=?
                """, (expiry, uid))
            await conn.commit()

    async def ensure_user(self, uid, username=""):
        async with self.connect() as conn:
            await conn.execute("""
                INSERT OR IGNORE INTO users(user_id, username, created_at)
                VALUES(?,?,?)
            """, (uid, username or "", utcnow().isoformat()))
            if username:
                await conn.execute("UPDATE users SET username=? WHERE user_id=?", (username, uid))
            await conn.commit()

    async def user(self, uid):
        async with self.connect() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT * FROM users WHERE user_id=?", (uid,))
            return await cur.fetchone()

    async def audit(self, uid, action, details=""):
        async with self.connect() as conn:
            await conn.execute(
                "INSERT INTO audit(user_id,action,details,created_at) VALUES(?,?,?,?)",
                (uid, action, str(details)[:1000], utcnow().isoformat())
            )
            await conn.commit()

    async def templates_add(self, uid, text):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        async with self.connect() as conn:
            cur = await conn.execute("SELECT templates FROM users WHERE user_id=?", (uid,))
            row = await cur.fetchone()
            values = json.loads(row[0] or "[]") if row else []
            existing = set(values)
            added = 0
            for line in lines:
                if line not in existing:
                    values.append(line)
                    existing.add(line)
                    added += 1
            await conn.execute("UPDATE users SET templates=? WHERE user_id=?",
                               (json.dumps(values, ensure_ascii=False), uid))
            await conn.commit()
        return added, len(values)

    async def templates_clear(self, uid):
        async with self.connect() as conn:
            await conn.execute("UPDATE users SET templates='[]' WHERE user_id=?", (uid,))
            await conn.commit()

    async def set_session(self, uid, value):
        async with self.connect() as conn:
            await conn.execute("UPDATE users SET session_string=? WHERE user_id=?", (value, uid))
            await conn.commit()

    async def set_targets(self, uid, targets):
        async with self.connect() as conn:
            await conn.execute("UPDATE users SET targets=? WHERE user_id=?",
                               (json.dumps(targets, ensure_ascii=False), uid))
            await conn.commit()

    async def set_delay(self, uid, low, high):
        async with self.connect() as conn:
            await conn.execute("UPDATE users SET min_delay=?, max_delay=? WHERE user_id=?",
                               (low, high, uid))
            await conn.commit()

    async def subscription_active(self, uid):
        row = await self.user(uid)
        if not row or not row["subscription_until"]:
            return False
        try:
            return dt.datetime.fromisoformat(row["subscription_until"]) > utcnow()
        except ValueError:
            return False

    async def grant_subscription(self, uid, days=60):
        await self.ensure_user(uid)
        row = await self.user(uid)
        start = utcnow()
        if row and row["subscription_until"]:
            try:
                old = dt.datetime.fromisoformat(row["subscription_until"])
                if old > start:
                    start = old
            except ValueError:
                pass
        expiry = (start + dt.timedelta(days=days)).isoformat()
        async with self.connect() as conn:
            await conn.execute("UPDATE users SET approved=1, subscription_until=? WHERE user_id=?",
                               (expiry, uid))
            await conn.commit()
        return expiry

    async def approve(self, uid, enabled):
        await self.ensure_user(uid)
        async with self.connect() as conn:
            await conn.execute("UPDATE users SET approved=? WHERE user_id=?", (int(enabled), uid))
            await conn.commit()

    async def add_balance(self, uid, amount):
        await self.ensure_user(uid)
        async with self.connect() as conn:
            await conn.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, uid))
            await conn.commit()

    async def all_users(self):
        async with self.connect() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT * FROM users ORDER BY user_id DESC")
            return await cur.fetchall()


# ---------------- TARGET PARSING ----------------

def parse_target(raw):
    value = raw.strip()
    if not value:
        return None
    if value.startswith("tg://chat?id="):
        return int(value.split("=", 1)[1])
    if re.fullmatch(r"-100\d+", value) or re.fullmatch(r"-?\d+", value):
        return int(value)
    value = re.sub(r"^https?://t\.me/", "", value).strip("/")
    value = re.sub(r"\?.*$", "", value)
    if value.startswith("c/"):
        parts = value.split("/")
        if len(parts) >= 2 and parts[1].isdigit():
            return int("-100" + parts[1])
    if value.startswith("@"):
        return value
    if re.fullmatch(r"[A-Za-z0-9_]{5,}", value):
        return "@" + value
    return None


# ---------------- DISPATCHER ----------------

class Dispatcher:
    def __init__(self, uid, client, targets, templates, min_delay=3, max_delay=10):
        self.uid = uid
        self.client = client
        self.targets = list(targets)[:MAX_TARGETS]
        self.templates = list(templates)
        self.min_delay = max(1, min(60, int(min_delay)))
        self.max_delay = max(self.min_delay, min(60, int(max_delay)))
        self.stop_event = asyncio.Event()
        self.task = None
        self.sent = 0
        self.errors = 0
        self.disabled_targets = set()

    async def start(self):
        if self.task and not self.task.done():
            return False
        self.stop_event.clear()
        self.task = asyncio.create_task(self.run(), name=f"dispatcher-{self.uid}")
        return True

    async def stop(self):
        self.stop_event.set()
        if self.task:
            try:
                await asyncio.wait_for(self.task, timeout=10)
            except asyncio.TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)

    async def interruptible_sleep(self, seconds):
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=max(0, seconds))
        except asyncio.TimeoutError:
            pass

    async def record_metric(self, column):
        if column not in ("sent", "errors"):
            return
        async with db.connect() as conn:
            await conn.execute(f"UPDATE metrics SET {column}={column}+1 WHERE id=1")
            await conn.commit()

    async def run(self):
        if not self.targets or not self.templates:
            return
        while not self.stop_event.is_set():
            for target in self.targets:
                if self.stop_event.is_set():
                    break
                if target in self.disabled_targets:
                    continue

                message = random.choice(self.templates)
                try:
                    await self.client.send_message(target, message)
                    self.sent += 1
                    await self.record_metric("sent")
                    await db.audit(self.uid, "MESSAGE_SENT", str(target))

                except FloodWaitError as exc:
                    await db.audit(self.uid, "FLOOD_WAIT", f"{target}: {exc.seconds}s")
                    await self.interruptible_sleep(exc.seconds)
                    continue

                except SlowModeWaitError as exc:
                    await db.audit(self.uid, "SLOW_MODE_WAIT", f"{target}: {exc.seconds}s")
                    await self.interruptible_sleep(exc.seconds)
                    continue

                except ChatWriteForbiddenError:
                    await db.audit(self.uid, "WRITE_FORBIDDEN", str(target))
                    # Ждём 5 минут, если не остановили
                    await self.interruptible_sleep(300)
                    if self.stop_event.is_set():
                        break
                    try:
                        await self.client.get_entity(target)
                        await self.client.send_message(target, message)
                        self.sent += 1
                        await self.record_metric("sent")
                        await db.audit(self.uid, "RETRY_SUCCESS", str(target))
                    except ChatWriteForbiddenError:
                        self.disabled_targets.add(target)
                        await db.audit(self.uid, "RETRY_STILL_FORBIDDEN", str(target))
                    except Exception as retry_error:
                        self.errors += 1
                        await self.record_metric("errors")
                        await db.audit(self.uid, "RETRY_ERROR", str(retry_error)[:500])
                    continue

                except UserBannedInChannelError:
                    self.disabled_targets.add(target)
                    await db.audit(self.uid, "TARGET_BANNED", str(target))
                    continue

                except asyncio.CancelledError:
                    raise

                except Exception as exc:
                    self.errors += 1
                    await self.record_metric("errors")
                    await db.audit(self.uid, "SEND_ERROR", f"{target}: {str(exc)[:400]}")

                await self.interruptible_sleep(random.randint(self.min_delay, self.max_delay))


# ---------------- TELEGRAM HELPERS ----------------

def is_admin(uid):
    return uid in SUPER_ADMINS

def menu_buttons(uid):
    rows = [
        [Button.inline("📱 Аккаунт", b"account"), Button.inline("📝 Шаблоны", b"templates")],
        [Button.inline("🎯 Цели", b"targets"), Button.inline("⏱ Задержка", b"delay")],
        [Button.inline("🚀 Запустить", b"start"), Button.inline("🛑 Остановить", b"stop")],
        [Button.inline("📊 Статистика", b"stats"), Button.inline("💎 Подписка", b"subscription")],
        [Button.inline("🏠 Главное меню", b"home")]
    ]
    if is_admin(uid):
        rows.append([Button.inline("🛡 Админ-панель", b"admin")])
    return rows

async def show_menu(event, text="**TITAN Enterprise**\nСистема корпоративных уведомлений."):
    await event.respond(text, buttons=menu_buttons(event.sender_id), parse_mode="md")

async def get_account_client(uid):
    row = await db.user(uid)
    if not row or not row["session_string"]:
        return None
    cached = active_clients.get(uid)
    if cached and cached.is_connected():
        return cached
    client = TelegramClient(StringSession(row["session_string"]), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        return None
    active_clients[uid] = client
    return client

async def finish_phone_login(uid, client):
    await db.set_session(uid, client.session.save())
    active_clients[uid] = client
    phone_flows.pop(uid, None)
    states[uid] = IDLE


# ---------------- TELEGRAM BOT ----------------

async def setup_bot():
    global bot
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN environment variable before starting.")
    bot = TelegramClient("titan_bot", API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)

    @bot.on(events.NewMessage(pattern=r"^/start$"))
    async def start_handler(event):
        uid = event.sender_id
        sender = await event.get_sender()
        await db.ensure_user(uid, getattr(sender, "username", "") or "")
        row = await db.user(uid)
        if not row["approved"] and not is_admin(uid):
            await event.respond(
                f"Доступ пока не активирован.\nВаш ID: `{uid}`\n"
                f"Для подключения: {SUPPORT_CONTACT}"
            )
            return
        states[uid] = IDLE
        await show_menu(event)

    @bot.on(events.CallbackQuery)
    async def callback_handler(event):
        uid = event.sender_id
        data = event.data.decode(errors="ignore")
        await db.ensure_user(uid)
        row = await db.user(uid)

        if not row["approved"] and not is_admin(uid):
            await event.answer("Доступ не активирован", alert=True)
            return

        if data == "home":
            states[uid] = IDLE
            await event.edit("**TITAN Enterprise — главное меню**",
                             buttons=menu_buttons(uid), parse_mode="md")

        elif data == "account":
            states[uid] = IDLE
            connected = bool(row["session_string"])
            await event.edit(
                f"**Аккаунт Telegram**\nСтатус: {'Подключён' if connected else 'Не подключён'}\n\n"
                "Выберите способ авторизации.",
                buttons=[
                    [Button.inline("📞 Войти по номеру", b"phone_login")],
                    [Button.inline("🔳 Войти по QR", b"qr_login")],
                    [Button.inline("🏠 Меню", b"home")]
                ], parse_mode="md"
            )

        elif data == "phone_login":
            states[uid] = WAIT_PHONE
            await event.respond("Отправьте номер телефона в международном формате, например +49123456789.")

        elif data == "qr_login":
            await event.answer("Запускаю генерацию QR...")
            await start_qr_login(uid, event)

        elif data == "templates":
            templates = json.loads(row["templates"] or "[]")
            preview = "\n".join(
                f"{i+1}. {x[:180]}" for i, x in enumerate(templates[:10])
            ) or "Список пуст."
            await event.edit(
                f"**Ваши шаблоны: {len(templates)}**\n\n{preview}",
                buttons=[
                    [Button.inline("➕ Добавить", b"add_templates"),
                     Button.inline("🗑 Очистить все", b"clear_templates")],
                    [Button.inline("🏠 Меню", b"home")]
                ], parse_mode="md"
            )

        elif data == "add_templates":
            states[uid] = IDLE
            await event.respond("Отправьте шаблоны: каждая строка станет отдельным шаблоном. Для выхода нажмите «Главное меню».")

        elif data == "clear_templates":
            await db.templates_clear(uid)
            await db.audit(uid, "TEMPLATES_CLEARED")
            await event.answer("Шаблоны очищены", alert=True)

        elif data == "targets":
            states[uid] = WAIT_TARGETS
            current = json.loads(row["targets"] or "[]")
            await event.respond(
                "Отправьте цели, каждую с новой строки (до 10).\n"
                "Поддерживаются @username, t.me-ссылки, tg://chat?id= и ID.\n"
                "Текущие цели:\n" + ("\n".join(map(str, current)) if current else "нет")
            )

        elif data == "delay":
            states[uid] = WAIT_DELAY
            await event.respond(
                f"Текущая задержка: {row['min_delay']}–{row['max_delay']} сек.\n"
                "Отправьте диапазон, например 3-10 (от 1 до 60 секунд)."
            )

        elif data == "start":
            if not await db.subscription_active(uid) and not is_admin(uid):
                await event.answer("Подписка истекла. Продление: @apemiru", alert=True)
                return
            client = await get_account_client(uid)
            if not client:
                await event.answer("Сначала подключите Telegram-аккаунт.", alert=True)
                return
            targets = json.loads(row["targets"] or "[]")
            templates = json.loads(row["templates"] or "[]")
            if not targets or not templates:
                await event.answer("Добавьте цели и шаблоны.", alert=True)
                return
            old = dispatchers.get(uid)
            if old:
                await old.stop()
            worker = Dispatcher(uid, client, targets, templates,
                                 row["min_delay"], row["max_delay"])
            dispatchers[uid] = worker
            await worker.start()
            await db.audit(uid, "DISPATCH_STARTED", f"{len(targets)} targets")
            await event.answer("Диспетчер запущен")

        elif data == "stop":
            worker = dispatchers.get(uid)
            if worker:
                await worker.stop()
                dispatchers.pop(uid, None)
            await db.audit(uid, "DISPATCH_STOPPED")
            await event.answer("Диспетчер остановлен", alert=True)

        elif data == "stats":
            worker = dispatchers.get(uid)
            sent = worker.sent if worker else 0
            errors = worker.errors if worker else 0
            await event.edit(
                f"**Статистика текущей задачи**\nОтправлено: {sent}\nОшибок: {errors}",
                buttons=[[Button.inline("🏠 Меню", b"home")]], parse_mode="md"
            )

        elif data == "subscription":
            active = await db.subscription_active(uid)
            row = await db.user(uid)
            await event.edit(
                f"**TITAN FULL ACCESS**\n400 ₽ / 60 дней\n"
                f"Статус: {'Активна' if active else 'Не активна'}\n"
                f"Окончание: {row['subscription_until'] or 'нет'}\n\n"
                f"Продление: {OWNER_CONTACT}",
                buttons=[[Button.inline("🏠 Меню", b"home")]], parse_mode="md"
            )

        elif data == "admin":
            if not is_admin(uid):
                await event.answer("Недостаточно прав", alert=True)
                return
            await event.edit(
                "**Администрирование**\n"
                "/approve ID — открыть доступ\n"
                "/revoke ID — закрыть доступ\n"
                "/sub ID — выдать 60 дней\n"
                "/stats — общая статистика\n"
                "/broadcast текст — объявление пользователям\n"
                ".balance ID сумма — начислить баланс",
                buttons=[[Button.inline("🏠 Меню", b"home")]], parse_mode="md"
            )

    @bot.on(events.NewMessage)
    async def text_handler(event):
        if not event.is_private or event.raw_text.startswith("/"):
            return
        uid = event.sender_id
        await db.ensure_user(uid)
        row = await db.user(uid)
        if not row["approved"] and not is_admin(uid):
            return

        state = states.get(uid, IDLE)
        text = event.raw_text.strip()

        if text in ("🏠 Главное меню", "меню", "отмена", "/cancel"):
            states[uid] = IDLE
            await show_menu(event)
            return

        if state == WAIT_PHONE:
            if not re.fullmatch(r"\+[1-9]\d{6,14}", text):
                await event.respond("Неверный формат. Пример: +49123456789")
                return
            client = TelegramClient(StringSession(), API_ID, API_HASH)
            await client.connect()
            try:
                sent = await client.send_code_request(text)
                phone_flows[uid] = {
                    "client": client, "phone": text, "hash": sent.phone_code_hash
                }
                states[uid] = WAIT_CODE
                await event.respond("Код отправлен. Введите код Telegram.")
            except Exception as exc:
                await client.disconnect()
                states[uid] = IDLE
                await event.respond(f"Не удалось отправить код: {str(exc)[:250]}")
            return

        if state == WAIT_CODE:
            flow = phone_flows.get(uid)
            if not flow:
                states[uid] = IDLE
                await event.respond("Сессия авторизации истекла. Начните заново.")
                return
            try:
                await flow["client"].sign_in(
                    phone=flow["phone"], code=text, phone_code_hash=flow["hash"]
                )
                await finish_phone_login(uid, flow["client"])
                await event.respond("Telegram-аккаунт подключён.")
            except SessionPasswordNeededError:
                states[uid] = WAIT_2FA
                await event.respond("Включена двухэтапная защита. Введите облачный пароль.")
            except (PhoneCodeInvalidError, Exception) as exc:
                await event.respond(f"Ошибка входа: {str(exc)[:250]}")
            return

        if state == WAIT_2FA:
            flow = phone_flows.get(uid)
            if not flow:
                states[uid] = IDLE
                await event.respond("Сессия авторизации истекла.")
                return
            try:
                await flow["client"].sign_in(password=text)
                await finish_phone_login(uid, flow["client"])
                await event.respond("Telegram-аккаунт подключён.")
            except PasswordHashInvalidError:
                await event.respond("Неверный облачный пароль. Попробуйте ещё раз.")
            except Exception as exc:
                await event.respond(f"Ошибка 2FA: {str(exc)[:250]}")
            return

        if state == WAIT_TARGETS:
            parsed = []
            invalid = []
            for line in text.splitlines():
                target = parse_target(line)
                if target is None:
                    invalid.append(line)
                elif target not in parsed:
                    parsed.append(target)
            if len(parsed) > MAX_TARGETS:
                await event.respond("Можно сохранить максимум 10 целей.")
                return
            if not parsed:
                await event.respond("Не удалось распознать цели.")
                return
            await db.set_targets(uid, parsed)
            await db.audit(uid, "TARGETS_UPDATED", json.dumps(parsed))
            states[uid] = IDLE
            await event.respond(
                f"Сохранено целей: {len(parsed)}."
                + (f"\nНе распознано: {len(invalid)}" if invalid else "")
            )
            return

        if state == WAIT_DELAY:
            match = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", text)
            if not match:
                await event.respond("Формат: 3-10")
                return
            low, high = map(int, match.groups())
            if not (1 <= low <= high <= 60):
                await event.respond("Допустимый диапазон: 1–60 секунд.")
                return
            await db.set_delay(uid, low, high)
            states[uid] = IDLE
            await event.respond(f"Задержка сохранена: {low}–{high} секунд.")
            return

        # IDLE: each non-empty line is an independent template.
        added, total = await db.templates_add(uid, text)
        if added:
            await db.audit(uid, "TEMPLATES_ADDED", f"added={added}")
            await event.respond(
                f"Добавлено шаблонов: {added}. Всего: {total}.\n"
                "Каждая строка сохранена отдельно.",
                buttons=menu_buttons(uid)
            )

    @bot.on(events.NewMessage(pattern=r"^/approve\s+(\d+)$"))
    async def approve_cmd(event):
        if not is_admin(event.sender_id):
            return
        uid = int(event.pattern_match.group(1))
        await db.approve(uid, True)
        await db.audit(event.sender_id, "USER_APPROVED", str(uid))
        await event.respond(f"Доступ выдан: {uid}")

    @bot.on(events.NewMessage(pattern=r"^/revoke\s+(\d+)$"))
    async def revoke_cmd(event):
        if not is_admin(event.sender_id):
            return
        uid = int(event.pattern_match.group(1))
        worker = dispatchers.pop(uid, None)
        if worker:
            await worker.stop()
        await db.approve(uid, False)
        await db.audit(event.sender_id, "USER_REVOKED", str(uid))
        await event.respond(f"Доступ закрыт: {uid}")

    @bot.on(events.NewMessage(pattern=r"^/sub\s+(\d+)$"))
    async def sub_cmd(event):
        if not is_admin(event.sender_id):
            return
        uid = int(event.pattern_match.group(1))
        expiry = await db.grant_subscription(uid, SUBSCRIPTION_DAYS)
        await db.audit(event.sender_id, "SUBSCRIPTION_GRANTED", f"{uid}: {expiry}")
        await event.respond(f"Подписка выдана пользователю {uid} до {expiry}")

    @bot.on(events.NewMessage(pattern=r"^/stats$"))
    async def stats_cmd(event):
        if not is_admin(event.sender_id):
            return
        async with db.connect() as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM users")
            users = (await cur.fetchone())[0]
            cur = await conn.execute("SELECT sent,errors,revenue FROM metrics WHERE id=1")
            sent, errors, revenue = await cur.fetchone()
        await event.respond(
            f"Пользователи: {users}\nОтправки: {sent}\nОшибки: {errors}\nВыручка: {revenue} ₽"
        )

    @bot.on(events.NewMessage(pattern=r"^\.balance\s+(\d+)\s+(-?\d+)$"))
    async def balance_cmd(event):
        if not is_admin(event.sender_id):
            return
        uid, amount = map(int, event.pattern_match.groups())
        await db.add_balance(uid, amount)
        await db.audit(event.sender_id, "BALANCE_CHANGED", f"{uid}: {amount}")
        await event.respond(f"Баланс пользователя {uid} изменён на {amount}")

    @bot.on(events.NewMessage(pattern=r"^/broadcast\s+([\s\S]+)$"))
    async def broadcast_cmd(event):
        if not is_admin(event.sender_id):
            return
        message = event.pattern_match.group(1)
        users = await db.all_users()
        sent = 0
        for user in users:
            try:
                await bot.send_message(user["user_id"], message)
                sent += 1
            except Exception:
                pass
            await asyncio.sleep(0.05)
        await db.audit(event.sender_id, "BROADCAST", f"delivered={sent}")
        await event.respond(f"Объявление отправлено: {sent}")

    return bot


async def start_qr_login(uid, event):
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    try:
        qr = await client.qr_login()
        qr_flows[uid] = {"client": client, "qr": qr}
        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={urllib.parse.quote(qr.url)}"
        await event.respond(
            "Откройте Telegram на устройстве с уже авторизованным аккаунтом → "
            "Настройки → Устройства → Подключить устройство и отсканируйте QR.\n\n"
            f"[Кликните сюда, чтобы открыть QR-код]({qr_url})\n\n"
            "Ссылка действует ограниченное время. После сканирования бот сохранит сессию."
        )
        asyncio.create_task(finish_qr_login(uid, event))
    except Exception as exc:
        await client.disconnect()
        await event.respond(f"Не удалось создать QR: {str(exc)[:250]}")


async def finish_qr_login(uid, event):
    flow = qr_flows.get(uid)
    if not flow:
        return
    client = flow["client"]
    try:
        await flow["qr"].wait()
        await finish_phone_login(uid, client)
        await event.respond("QR-авторизация завершена. Аккаунт подключён.")
    except SessionPasswordNeededError:
        states[uid] = WAIT_2FA
        phone_flows[uid] = {"client": client, "phone": "", "hash": ""}
        await event.respond("На аккаунте включена 2FA. Отправьте облачный пароль.")
    except Exception as exc:
        await event.respond(f"QR-авторизация не завершена: {str(exc)[:250]}")
        await client.disconnect()
    finally:
        qr_flows.pop(uid, None)


# ---------------- WEB DASHBOARD ----------------

DASHBOARD_HTML = """
<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TITAN Enterprise — Dashboard</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#08090d;color:#e9edf5;font:14px Inter,Arial}
.wrap{display:flex;min-height:100vh}.side{width:235px;background:#10131c;padding:26px 18px;border-right:1px solid #252b3a}
.logo{font-size:21px;font-weight:800;letter-spacing:2px;color:#ff354f;margin-bottom:35px}
.side a{display:block;padding:12px;color:#aeb7c9;text-decoration:none;border-radius:9px;margin:5px 0}
.side a:hover{background:#1b2231;color:white}.main{padding:30px;flex:1;min-width:0}
h1{margin:0 0 8px;font-size:27px}.muted{color:#8792a8}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin:25px 0}
.card{background:#141824;border:1px solid #252c3b;border-radius:13px;padding:20px}
.value{font-size:27px;font-weight:800;margin-top:12px;color:#ff5268}
table{width:100%;border-collapse:collapse;background:#141824;border-radius:12px;overflow:hidden}
th,td{text-align:left;padding:12px;border-bottom:1px solid #252c3b;word-break:break-word}
th{color:#9da9bd;font-weight:600}button{padding:9px 12px;border-radius:8px;border:0;background:#d92f49;color:white;cursor:pointer}
.section{margin:28px 0;overflow:auto}.top{display:flex;justify-content:space-between;gap:15px;align-items:center;flex-wrap:wrap}
@media(max-width:700px){.wrap{display:block}.side{width:100%;border:0}.main{padding:18px}.side a{display:inline-block}}
</style></head><body><div class="wrap"><aside class="side">
<div class="logo">TITAN ENTERPRISE</div><a href="#overview">Обзор</a><a href="#users">Пользователи</a>
<a href="#audit">Журнал активности</a><a href="/export.csv">CSV-экспорт</a><a href="/logout">Выйти</a>
<p class="muted">Support: @apemiru</p></aside><main class="main">
<div class="top"><div><h1>Панель управления</h1><div class="muted">Мониторинг корпоративных уведомлений</div></div>
<button onclick="loadAll()">Обновить данные</button></div>
<div id="overview" class="grid">
<div class="card">Всего пользователей<div class="value" id="users">—</div></div>
<div class="card">Активные сессии<div class="value" id="sessions">—</div></div>
<div class="card">Успешные отправки<div class="value" id="sent">—</div></div>
<div class="card">Ошибки<div class="value" id="errors">—</div></div>
<div class="card">Выручка<div class="value" id="revenue">—</div></div></div>
<div class="section" id="users"><h2>Пользователи</h2><table><thead><tr><th>ID</th><th>Username</th><th>Подписка до</th><th>Баланс</th><th>Сессия</th><th>Действия</th></tr></thead><tbody id="userRows"></tbody></table></div>
<div class="section" id="audit"><h2>Журнал активности</h2><table><thead><tr><th>Дата</th><th>User ID</th><th>Действие</th><th>Подробности</th></tr></thead><tbody id="auditRows"></tbody></table></div>
</main></div>
<script>
async function api(path, opts={}){let r=await fetch(path,{...opts,headers:{'Content-Type':'application/json',...(opts.headers||{})}});if(r.status===401){location.href='/login';return null}return await r.json()}
async function loadAll(){
 let s=await api('/api/stats');if(s){for(let k of ['users','sessions','sent','errors','revenue'])document.getElementById(k).textContent=s[k]??'—';}
 let u=await api('/api/users');if(u){document.getElementById('userRows').innerHTML=u.map(x=>`<tr><td>${x.user_id}</td><td>${x.username||''}</td><td>${x.subscription_until||'—'}</td><td>${x.balance}</td><td>${x.has_session?'Да':'Нет'}</td><td><button onclick="grant(${x.user_id})">+60 дней</button></td></tr>`).join('')}
 let a=await api('/api/audit');if(a){document.getElementById('auditRows').innerHTML=a.map(x=>`<tr><td>${x.created_at}</td><td>${x.user_id}</td><td>${x.action}</td><td>${x.details||''}</td></tr>`).join('')}
}
async function grant(id){if(!confirm('Выдать 60 дней пользователю '+id+'?'))return;let r=await api('/api/grant',{method:'POST',body:JSON.stringify({user_id:id})});if(r&&r.ok)loadAll()}
loadAll();setInterval(loadAll,30000);
</script></body></html>
"""

LOGIN_HTML = """
<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TITAN Enterprise</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#08090d;color:#e9edf5;font:14px Inter,Arial}
.wrap{display:flex;min-height:100vh}.side{width:235px;background:#10131c;padding:26px 18px;border-right:1px solid #252b3a}
.logo{font-size:21px;font-weight:800;letter-spacing:2px;color:#ff354f;margin-bottom:35px}
.side a{display:block;padding:12px;color:#aeb7c9;text-decoration:none;border-radius:9px;margin:5px 0}
.side a:hover{background:#1b2231;color:white}.main{padding:30px;flex:1;max-width:1500px}
h1{margin:0 0 8px;font-size:27px}.muted{color:#8792a8}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin:25px 0}
.card{background:#141824;border:1px solid #252c3b;border-radius:13px;padding:20px}
.value{font-size:27px;font-weight:800;margin-top:12px;color:#ff5268}
table{width:100%;border-collapse:collapse;background:#141824;border-radius:12px;overflow:hidden}
th,td{text-align:left;padding:13px;border-bottom:1px solid #252c3b;word-break:break-word}
th{color:#9da9bd;font-weight:600}input,button{padding:11px;border-radius:8px;border:1px solid #30394b;background:#0c1018;color:white}
button{background:#d92f49;border:0;cursor:pointer;font-weight:700}.section{margin:28px 0}
.login{max-width:390px;margin:12vh auto;background:#141824;padding:28px;border-radius:14px;border:1px solid #252c3b}
.login input{width:100%;margin:10px 0}.error{color:#ff697b}
@media(max-width:700px){.wrap{display:block}.side{width:100%;border:0}.main{padding:18px}.side a{display:inline-block}}
</style></head><body>
<div class="login"><div class="logo">TITAN / ADMIN</div><h2>Вход в панель</h2>
<p class="muted">Используйте серверный DASHBOARD_TOKEN.</p>
<form method="post"><input type="password" name="token" placeholder="Токен доступа" required>
<button style="width:100%">Войти</button></form>{% if error %}<p class="error">{{error}}</p>{% endif %}</div>
</body></html>
"""

def require_dashboard(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("dashboard_admin"):
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapped

@app.route("/")
def index():
    if not session.get("dashboard_admin"):
        return redirect(url_for("login"))
    return render_template_string(DASHBOARD_HTML)

@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if request.form.get("token", "") == DASHBOARD_TOKEN:
            session["dashboard_admin"] = True
            return redirect(url_for("index"))
        error = "Неверный токен."
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/api/stats")
@require_dashboard
def api_stats():
    async def get_data():
        async with db.connect() as conn:
            cur = await conn.execute("SELECT COUNT(*), SUM(CASE WHEN session_string!='' THEN 1 ELSE 0 END) FROM users")
            users, sessions_count = await cur.fetchone()
            cur = await conn.execute("SELECT sent,errors,revenue FROM metrics WHERE id=1")
            sent, errors, revenue = await cur.fetchone()
        return {"users": users or 0, "sessions": sessions_count or 0,
                "sent": sent, "errors": errors, "revenue": revenue}
    return jsonify(run_async(get_data()))

@app.route("/api/users")
@require_dashboard
def api_users():
    rows = run_async(db.all_users())
    return jsonify([{
        "user_id": r["user_id"], "username": r["username"],
        "subscription_until": r["subscription_until"],
        "balance": r["balance"], "has_session": bool(r["session_string"])
    } for r in rows])

@app.route("/api/audit")
@require_dashboard
def api_audit():
    async def fetch():
        async with db.connect() as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200")
            return [dict(r) for r in await cur.fetchall()]
    return jsonify(run_async(fetch()))

@app.route("/api/grant", methods=["POST"])
@require_dashboard
def api_grant():
    payload = request.get_json(silent=True) or {}
    try:
        uid = int(payload["user_id"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"ok": False, "error": "invalid user_id"}), 400
    expiry = run_async(db.grant_subscription(uid, 60))
    run_async(db.audit(0, "WEB_SUBSCRIPTION_GRANTED", f"{uid}: {expiry}"))
    return jsonify({"ok": True, "subscription_until": expiry})

@app.route("/export.csv")
@require_dashboard
def export_csv():
    async def get_rows():
        return await db.all_users()
    rows = run_async(get_rows())
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["user_id", "username", "subscription_until", "balance", "has_session"])
    for r in rows:
        writer.writerow([r["user_id"], r["username"], r["subscription_until"],
                         r["balance"], bool(r["session_string"])])
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=titan_users.csv"})


# Flask runs in a separate thread; bridge API coroutines to the bot's asyncio loop.
def run_async(coro):
    if loop is None:
        raise RuntimeError("Async event loop is not ready")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=20)

def run_web():
    serve(app, host=WEB_HOST, port=WEB_PORT, threads=8)


# ---------------- MAIN ----------------

async def main():
    global db, loop
    loop = asyncio.get_running_loop()
    db = Database(DB_PATH)
    await db.initialize()

    web_thread = threading.Thread(target=run_web, daemon=True, name="titan-web")
    web_thread.start()

    await setup_bot()
    print("TITAN Enterprise started.")
    print(f"Dashboard: http://0.0.0.0:{WEB_PORT}")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("TITAN Enterprise stopped.")
