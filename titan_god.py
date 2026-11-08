# =====================================================================
# TITAN SaaS ENTERPRISE v12.0
# Архитектура: Flask (Web UI) + Telethon (Userbot) + SQLite (DB)
# Полностью автономная система с веб-авторизацией и админ-панелью.
# =====================================================================

import os
import sys
import json
import re
import asyncio
import random
import datetime
import threading
import uuid
import time
import shutil
from colorama import init, Fore, Style
from flask import Flask, jsonify, render_template_string, request, redirect, url_for
import aiosqlite

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.errors.rpcerrorlist import AccessTokenInvalidError

init(autoreset=True)

# =====================================================================
# 1. КОНСТАНТЫ И КОНФИГУРАЦИЯ
# =====================================================================
API_ID = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'
BOT_TOKEN = '8732669492:AAGiMPHbCs8ntMzFhkfS8466iIVVa67w47g' 

# ТВОИ ДВА ID (Доступ к Админке)
ADMIN_IDS = [9058235147, 8773171051]

DB_FILE = 'titan_saas.db'
BACKUP_DIR = 'db_backups'
START_TIME = datetime.datetime.now()

# Глобальные состояния
active_clients = {}     # Активные клиенты Telethon
spam_engines = {}       # Активные движки спама
web_auth_cache = {}     # Временное хранилище для кодов веб-авторизации
user_states = {}        # Состояния FSM для бота

# Глобальные метрики для веб-панели
system_metrics = {
    "total_messages_sent": 0, 
    "total_errors_caught": 0, 
    "flood_waits": 0
}

def log(tag, msg, color=Fore.WHITE):
    t = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{Fore.LIGHTBLACK_EX}[{t}]{Fore.RESET} {color}[{tag}]{Fore.RESET} {msg}")

# =====================================================================
# 2. МОДУЛЬ БАЗЫ ДАННЫХ (SQLITE ORM)
# =====================================================================
async def init_db():
    if not os.path.exists(BACKUP_DIR): os.makedirs(BACKUP_DIR)
    async with aiosqlite.connect(DB_FILE) as db:
        # Таблица пользователей
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, 
            phone TEXT,
            session_string TEXT, 
            templates TEXT DEFAULT '[]',
            min_delay INTEGER DEFAULT 100, 
            max_delay INTEGER DEFAULT 300,
            is_banned INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            web_token TEXT,
            registered_at TEXT
        )''')
        # Таблица задач от веб-интерфейса
        await db.execute('''CREATE TABLE IF NOT EXISTS web_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            payload TEXT,
            status TEXT DEFAULT 'PENDING',
            created_at TEXT
        )''')
        await db.commit()
        log("DB", "Схема базы данных успешно развернута.", Fore.GREEN)

async def get_user(uid):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT session_string, templates, min_delay, max_delay, is_banned, is_admin, web_token FROM users WHERE user_id = ?", (uid,)) as cursor:
            row = await cursor.fetchone()
            if row: 
                return {
                    'session': row[0], 'templates': json.loads(row[1]), 
                    'min_delay': row[2], 'max_delay': row[3], 
                    'is_banned': row[4], 'is_admin': row[5], 'web_token': row[6]
                }
    return None

async def get_user_by_token(token):
    if not token: return None
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT user_id, session_string, templates, min_delay, max_delay, is_banned FROM users WHERE web_token = ?", (token,)) as cursor:
            row = await cursor.fetchone()
            if row: 
                return {
                    'user_id': row[0], 'session': row[1], 'templates': json.loads(row[2]), 
                    'min_delay': row[3], 'max_delay': row[4], 'is_banned': row[5]
                }
    return None

async def save_user(uid, **kwargs):
    current = await get_user(uid) or {'session': None, 'templates': [], 'min_delay': 100, 'max_delay': 300, 'is_banned': 0, 'is_admin': 0, 'web_token': None}
    
    # Безопасное обновление полей
    s = kwargs.get('session', current['session'])
    t = kwargs.get('templates', current['templates'])
    mind = kwargs.get('min_d', current['min_delay'])
    maxd = kwargs.get('max_d', current['max_delay'])
    b = kwargs.get('is_banned', current['is_banned'])
    adm = kwargs.get('is_admin', current['is_admin'])
    token = kwargs.get('token', current['web_token'])
    phone = kwargs.get('phone', None)
    
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''INSERT INTO users (user_id, phone, session_string, templates, min_delay, max_delay, is_banned, is_admin, web_token, registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
            phone=COALESCE(excluded.phone, users.phone),
            session_string=excluded.session_string, 
            templates=excluded.templates, 
            min_delay=excluded.min_delay, 
            max_delay=excluded.max_delay,
            is_banned=excluded.is_banned,
            is_admin=excluded.is_admin,
            web_token=excluded.web_token''',
            (uid, phone, s, json.dumps(t), mind, maxd, b, adm, token, str(datetime.datetime.now())))
        await db.commit()

# =====================================================================
# 3. ДВИЖОК СПАМА (С АНТИ-ДЕТЕКТОМ)
# =====================================================================
class SpamEngine:
    def __init__(self, uid, target, templates, min_d, max_d, session_str):
        self.uid = uid
        self.target = target
        self.templates = templates
        self.min_d = min_d
        self.max_d = max_d
        self.session_str = session_str
        self.is_active = True
        self.counter = 0
        self.client = None

    async def run(self):
        try:
            self.client = TelegramClient(StringSession(self.session_str), API_ID, API_HASH)
            await self.client.connect()
            log("SPAM", f"Движок [{self.uid}] запущен по цели: {self.target}", Fore.YELLOW)
            
            while self.is_active:
                try:
                    if not self.templates:
                        break
                        
                    # Берем случайный шаблон
                    text = random.choice(self.templates)
                    
                    # Добавляем невидимые символы для обхода спам-фильтров
                    stealth = "".join([random.choice(['\u200b', '\u200c', '\u200d']) for _ in range(random.randint(2, 6))])
                    final_text = f"{text}{stealth}"
                    
                    await self.client.send_message(self.target, final_text)
                    self.counter += 1
                    system_metrics["total_messages_sent"] += 1
                    
                    log("SPAM", f"[{self.uid}] [{self.counter}] -> {self.target}", Fore.BLUE)
                    
                    # Рандомная задержка
                    wait_time = random.randint(self.min_d, self.max_d) / 1000.0
                    await asyncio.sleep(wait_time)
                    
                except FloodWaitError as e:
                    system_metrics["flood_waits"] += 1
                    log("SPAM", f"[{self.uid}] FloodWait: {e.seconds} сек.", Fore.RED)
                    await asyncio.sleep(e.seconds + 10)
                except Exception as e:
                    system_metrics["total_errors_caught"] += 1
                    log("SPAM", f"[{self.uid}] Сбой: {e}", Fore.RED)
                    break
                    
        except Exception as e:
            log("SPAM", f"Критический сбой клиента: {e}", Fore.RED)
        finally:
            if self.client: await self.client.disconnect()
            self.is_active = False
            log("SPAM", f"Движок [{self.uid}] остановлен.", Fore.YELLOW)

# =====================================================================
# 4. ВОРКЕР ЗАДАЧ ИЗ ВЕБ-ИНТЕРФЕЙСА
# =====================================================================
async def web_task_worker():
    """Фоновый процесс, который слушает команды от сайта и запускает спам."""
    while True:
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute("SELECT id, user_id, action, payload FROM web_tasks WHERE status = 'PENDING'") as cursor:
                    tasks = await cursor.fetchall()
                    for t_id, u_id, action, payload in tasks:
                        payload = json.loads(payload)
                        if action == "START_SPAM":
                            p = await get_user(u_id)
                            if p and not p['is_banned']:
                                eng = SpamEngine(u_id, payload['target'], p['templates'], p['min_delay'], p['max_delay'], p['session'])
                                spam_engines[u_id] = eng
                                asyncio.run_coroutine_threadsafe(eng.run(), loop_manager)
                        
                        await db.execute("UPDATE web_tasks SET status = 'DONE' WHERE id = ?", (t_id,))
                        await db.commit()
        except Exception as e:
            log("WEB_TASK", f"Ошибка воркера: {e}", Fore.RED)
        await asyncio.sleep(2)

# =====================================================================
# 5. TELEGRAM БОТ (ДЛЯ ГЕНЕРАЦИИ ССЫЛОК И УПРАВЛЕНИЯ)
# =====================================================================
bot = TelegramClient('bot_saas', API_ID, API_HASH)

@bot.on(events.NewMessage(pattern='/start'))
async def cmd_start(e):
    uid = e.sender_id
    p = await get_user(uid)
    
    # Генерируем токен, если его нет
    if not p or not p['web_token']:
        token = uuid.uuid4().hex
        await save_user(uid, token=token)
        p = await get_user(uid)
    
    is_admin = uid in ADMIN_IDS
    link = f"https://bot-1-dk7y.onrender.com/login?token={p['web_token']}"
    
    text = f"⚡ **TITAN SaaS v12.0**\n\n"
    text += f"Ваш ID: `{uid}`\n"
    text += f"Статус: {'👑 Админ' if is_admin else '👤 Пользователь'}\n\n"
    text += f"[👉 Войти в веб-панель]({link})"
    
    await e.reply(text, parse_mode='md')

# =====================================================================
# 6. ВЕБ-ИНТЕРФЕЙС (FLASK APP)
# =====================================================================
app = Flask(__name__)

# --- HTML: СТРАНИЦА ВХОДА (ЛОГИН) ---
HTML_LOGIN = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TITAN | Authorization</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap" rel="stylesheet">
    <style>
        :root { --bg: #090a0f; --card: #13151d; --text: #fff; --acc: #4f8cff; --err: #ff4d4d; }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', sans-serif; }
        body { background: var(--bg); color: var(--text); display: flex; justify-content: center; align-items: center; height: 100vh; }
        .login-box { background: var(--card); padding: 40px; border-radius: 16px; width: 340px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); border: 1px solid #242836; text-align: center; }
        h2 { color: var(--acc); margin-bottom: 30px; font-weight: 600; font-size: 24px; }
        input { width: 100%; padding: 14px; margin: 10px 0; background: #0c0e14; border: 1px solid #2a2f3a; color: #fff; border-radius: 8px; font-size: 14px; outline: none; transition: 0.2s; }
        input:focus { border-color: var(--acc); }
        button { width: 100%; padding: 14px; background: var(--acc); border: none; color: #fff; border-radius: 8px; cursor: pointer; font-weight: 600; font-size: 15px; margin-top: 10px; transition: 0.2s; }
        button:hover { background: #3a7af2; }
        .step2 { display: none; }
        .error-text { color: var(--err); font-size: 13px; margin-top: 15px; min-height: 20px; }
    </style>
</head>
<body>
    <div class="login-box">
        <h2>⚡ TITAN LOGIN</h2>
        <div class="step1" id="step1">
            <input type="text" id="phone" placeholder="Ваш номер (+7999...)">
            <button onclick="sendCode()">Получить код</button>
        </div>
        <div class="step2" id="step2">
            <input type="text" id="code" placeholder="Код из Telegram">
            <button onclick="verifyCode()">Войти в панель</button>
        </div>
        <div class="error-text" id="error"></div>
    </div>
    <script>
        const TOKEN = "{{ token }}";
        
        async function sendCode() {
            const phone = document.getElementById('phone').value;
            if(!phone) return document.getElementById('error').innerText = "Введите номер!";
            
            const res = await fetch('/api/send_code', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({token: TOKEN, phone: phone})
            });
            const data = await res.json();
            
            if(data.status === 'ok') {
                document.getElementById('step1').style.display = 'none';
                document.getElementById('step2').style.display = 'block';
            } else {
                document.getElementById('error').innerText = data.error;
            }
        }
        
        async function verifyCode() {
            const code = document.getElementById('code').value;
            const res = await fetch('/api/verify_code', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({token: TOKEN, code: code})
            });
            const data = await res.json();
            
            if(data.status === 'ok') {
                window.location.href = '/dashboard?token=' + TOKEN;
            } else {
                document.getElementById('error').innerText = data.error;
            }
        }
    </script>
</body>
</html>
"""

# --- HTML: ДАШБОРД ---
HTML_DASHBOARD = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TITAN | Enterprise Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #090a0f; --card: #13151d; --text: #e8e8ea; --mut: #8b92a5;
            --acc: #4f8cff; --ok: #39c07a; --danger: #ff4d4d; --warn: #ff9800;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', sans-serif; }
        body { background: var(--bg); color: var(--text); display: flex; height: 100vh; overflow: hidden; }

        /* Sidebar */
        .sidebar { width: 260px; background: var(--card); border-right: 1px solid #1f2330; display: flex; flex-direction: column; padding: 20px; }
        .logo { font-size: 20px; font-weight: 700; color: var(--acc); margin-bottom: 30px; display: flex; align-items: center; gap: 10px; }
        .nav-item { padding: 12px 15px; border-radius: 8px; color: var(--mut); cursor: pointer; margin-bottom: 5px; font-weight: 500; transition: 0.2s; display: flex; align-items: center; gap: 10px; }
        .nav-item:hover, .nav-item.active { background: #1f2536; color: #fff; }
        .nav-item.active { color: var(--acc); border-left: 3px solid var(--acc); }

        /* Main Content */
        .main { flex: 1; padding: 30px; overflow-y: auto; }
        .top-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 30px; }
        .top-bar h1 { font-size: 24px; font-weight: 600; }
        .status-badge { padding: 6px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; }
        .status-badge.ok { background: rgba(57,192,122,0.1); color: var(--ok); border: 1px solid var(--ok); }
        .status-badge.err { background: rgba(255,77,77,0.1); color: var(--danger); border: 1px solid var(--danger); }

        /* Grid & Cards */
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        .card { background: var(--card); border-radius: 12px; padding: 25px; border: 1px solid #1f2330; }
        .card h3 { font-size: 16px; margin-bottom: 15px; color: #fff; display: flex; align-items: center; gap: 8px; }
        .stat-val { font-size: 32px; font-weight: 700; color: var(--acc); }
        .stat-name { font-size: 13px; color: var(--mut); margin-top: 5px; }

        /* Forms & Inputs */
        label { display: block; font-size: 13px; color: var(--mut); margin-bottom: 8px; }
        input, textarea { width: 100%; background: #0c0e14; border: 1px solid #2a2f3a; color: #fff; padding: 14px; border-radius: 8px; font-size: 14px; margin-bottom: 15px; outline: none; transition: 0.2s; }
        input:focus, textarea:focus { border-color: var(--acc); }
        button { width: 100%; padding: 14px; border: none; border-radius: 8px; font-weight: 600; cursor: pointer; font-size: 14px; transition: 0.2s; margin-bottom: 10px; }
        .btn-primary { background: var(--acc); color: #fff; }
        .btn-primary:hover { background: #3a7af2; }
        .btn-danger { background: var(--danger); color: #fff; }
        .btn-danger:hover { background: #e63946; }
        .btn-ok { background: var(--ok); color: #fff; }
        .btn-ok:hover { background: #2ea064; }

        /* Admin Table */
        table { width: 100%; border-collapse: collapse; margin-top: 15px; }
        th, td { padding: 15px; text-align: left; border-bottom: 1px solid #1f2330; font-size: 14px; }
        th { color: var(--mut); font-weight: 500; }
        .btn-sm { padding: 8px 14px; font-size: 12px; border-radius: 6px; width: auto; margin: 0; }

        /* Utilities */
        .hidden { display: none !important; }
        #toast { position: fixed; bottom: 20px; right: 20px; background: var(--ok); color: #fff; padding: 14px 24px; border-radius: 8px; font-weight: 600; opacity: 0; transform: translateY(20px); transition: 0.3s; pointer-events: none; z-index: 9999; }
        #toast.show { opacity: 1; transform: translateY(0); }
    </style>
</head>
<body>

<div class="sidebar">
    <div class="logo">⚡ TITAN <span style="font-size:12px;color:var(--mut);font-weight:400;">v12.0</span></div>
    <div class="nav-item active" id="nav-dash" onclick="showTab('dash')">📊 Дашборд</div>
    <div class="nav-item" id="nav-spam" onclick="showTab('spam')">🚀 Рассылка</div>
    <div class="nav-item" id="nav-settings" onclick="showTab('settings')">⚙️ Настройки</div>
    {% if is_admin %}
    <div class="nav-item" id="nav-admin" onclick="showTab('admin')" style="color:var(--warn);margin-top:20px;">👑 Админ-панель</div>
    {% endif %}
</div>

<div class="main">
    <div class="top-bar">
        <h1 id="pageTitle">Дашборд</h1>
        <div class="status-badge {% if session %}ok{% else %}err{% endif %}" id="sessionStatus">
            {% if session %}Сессия активна{% else %}Нет сессии{% endif %}
        </div>
    </div>

    <!-- ВКЛАДКА: ДАШБОРД -->
    <div id="tab-dash">
        <div class="grid">
            <div class="card">
                <h3>📈 Статистика (Глобальная)</h3>
                <div class="grid" style="grid-template-columns:1fr 1fr;gap:10px;">
                    <div><div class="stat-val" id="statSent">0</div><div class="stat-name">Отправлено</div></div>
                    <div><div class="stat-val" id="statErr">0</div><div class="stat-name">Ошибок</div></div>
                    <div><div class="stat-val" id="statFlood">0</div><div class="stat-name">FloodWait</div></div>
                    <div><div class="stat-val" id="statUp">0с</div><div class="stat-name">Аптайм</div></div>
                </div>
            </div>
            <div class="card">
                <h3>📝 Твои Шаблоны</h3>
                <textarea id="templates" style="height:120px;">{{ templates }}</textarea>
                <button class="btn-primary" onclick="saveTpl()">Сохранить шаблоны</button>
            </div>
        </div>
    </div>

    <!-- ВКЛАДКА: РАССЫЛКА -->
    <div id="tab-spam" class="hidden">
        <div class="card" style="max-width: 500px;">
            <h3>🚀 Запуск атаки</h3>
            <label>Цель (@username или ID)</label>
            <input type="text" id="target" placeholder="@username">
            <button class="btn-ok" onclick="startSpam()">Запустить</button>
            <button class="btn-danger" onclick="stopSpam()">Остановить</button>
        </div>
    </div>

    <!-- ВКЛАДКА: НАСТРОЙКИ -->
    <div id="tab-settings" class="hidden">
        <div class="card" style="max-width: 500px;">
            <h3>⚙️ Конфигурация</h3>
            <label>Задержка (мин - макс, мс)</label>
            <input type="text" id="delay" value="{{ min_d }}-{{ max_d }}">
            <button class="btn-primary" onclick="saveDelay()">Сохранить настройки</button>
        </div>
    </div>

    <!-- ВКЛАДКА: АДМИНКА -->
    {% if is_admin %}
    <div id="tab-admin" class="hidden">
        <div class="card">
            <h3>👑 Управление пользователями</h3>
            <table>
                <thead><tr><th>ID</th><th>Телефон</th><th>Статус</th><th>Действие</th></tr></thead>
                <tbody id="userList"></tbody>
            </table>
        </div>
    </div>
    {% endif %}
</div>

<div id="toast"></div>

<script>
    const TOKEN = "{{ token }}";
    const IS_ADMIN = {{ 'true' if is_admin else 'false' }};
    
    function showToast(msg, color = "#39c07a") {
        const t = document.getElementById('toast');
        t.innerText = msg; t.style.background = color;
        t.classList.add('show'); setTimeout(() => t.classList.remove('show'), 3000);
    }

    function showTab(id) {
        // Навигация
        document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
        document.getElementById('nav-' + id).classList.add('active');
        // Контент
        document.querySelectorAll('.main > div[id^="tab-"]').forEach(el => el.classList.add('hidden'));
        document.getElementById('tab-' + id).classList.remove('hidden');
        // Заголовок
        document.getElementById('pageTitle').innerText = document.getElementById('nav-' + id).innerText.replace(/[^\w\sа-яА-Я]/g, '').trim();
        
        if(id === 'admin') loadUsers();
    }

    async function saveTpl() {
        const txt = document.getElementById('templates').value;
        const res = await fetch('/api/save_templates', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({token: TOKEN, templates: txt})});
        if(res.ok) showToast("Шаблоны сохранены!");
    }

    async function saveDelay() {
        const val = document.getElementById('delay').value;
        const res = await fetch('/api/save_delay', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({token: TOKEN, delay: val})});
        if(res.ok) showToast("Задержка обновлена!");
    }

    async function startSpam() {
        const tg = document.getElementById('target').value;
        if(!tg) return showToast("Укажи цель!", "#ff9800");
        const res = await fetch('/api/start_spam', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({token: TOKEN, target: tg})});
        if(res.ok) showToast("🚀 Рассылка запущена!");
    }

    async function stopSpam() {
        const res = await fetch('/api/stop_spam', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({token: TOKEN})});
        if(res.ok) showToast("🛑 Процессы остановлены!", "#ff4d4d");
    }

    // --- АДМИН ЛОГИКА ---
    if(IS_ADMIN) {
        async function loadUsers() {
            const res = await fetch('/api/admin/users');
            const users = await res.json();
            const tb = document.getElementById('userList');
            tb.innerHTML = '';
            users.forEach(u => {
                const tr = document.createElement('tr');
                tr.innerHTML = `<td>${u.user_id}</td>
                                <td>${u.phone || '-'}</td>
                                <td style="color:${u.is_banned ? '#ff4d4d' : '#39c07a'}; font-weight:600;">${u.is_banned ? 'Забанен' : 'Активен'}</td>
                                <td><button class="btn-sm ${u.is_banned ? 'btn-ok' : 'btn-danger'}" onclick="toggleBan(${u.user_id}, ${u.is_banned ? 0 : 1})">${u.is_banned ? 'Разбанить' : 'Забанить'}</button></td>`;
                tb.appendChild(tr);
            });
        }

        async function toggleBan(uid, ban) {
            const res = await fetch('/api/admin/ban', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({uid: uid, ban: ban})});
            if(res.ok) {
                loadUsers();
                showToast("Статус пользователя обновлен", ban ? "#ff4d4d" : "#39c07a");
            }
        }
    }

    // Поллинг статистики
    setInterval(async () => {
        const res = await fetch(`/api/stats?token=${TOKEN}`);
        if(res.ok) {
            const data = await res.json();
            document.getElementById('statSent').innerText = data.messages;
            document.getElementById('statErr').innerText = data.errors;
            document.getElementById('statFlood').innerText = data.floods;
            document.getElementById('statUp').innerText = data.uptime;
        }
    }, 2000);
</script>
</body>
</html>
"""

@app.route('/')
def index(): return "Server is running."

@app.route('/login')
def login():
    token = request.args.get('token')
    if not token: return "403: No token provided"
    return render_template_string(HTML_LOGIN, token=token)

@app.route('/dashboard')
def dashboard():
    token = request.args.get('token')
    loop = asyncio.new_event_loop()
    p = loop.run_until_complete(get_user_by_token(token))
    loop.close()
    
    if not p: return "403 ACCESS DENIED"
    if p['is_banned']: return "403 YOU ARE BANNED"
    
    is_admin = p['user_id'] in ADMIN_IDS
    tpl_str = "\n".join(p['templates'])
    
    return render_template_string(HTML_DASHBOARD, 
        token=token, 
        templates=tpl_str, 
        min_d=p['min_delay'], 
        max_d=p['max_delay'],
        session=p['session'],
        is_admin=is_admin
    )

# --- WEB API ---
@app.route('/api/send_code', methods=['POST'])
def api_send_code():
    data = request.json
    token, phone = data.get('token'), data.get('phone')
    
    loop = asyncio.new_event_loop()
    p = loop.run_until_complete(get_user_by_token(token))
    
    if not p: 
        loop.close()
        return jsonify({"status":"error","error":"Токен недействителен"})
    if p['is_banned']: 
        loop.close()
        return jsonify({"status":"error","error":"Вы забанены"})
    
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        loop.run_until_complete(client.connect())
        req = loop.run_until_complete(client.send_code_request(phone))
        
        web_auth_cache[token] = {'client': client, 'phone': phone, 'hash': req.phone_code_hash, 'loop': loop}
        return jsonify({"status":"ok"})
    except Exception as e:
        loop.close()
        return jsonify({"status":"error","error": str(e)})

@app.route('/api/verify_code', methods=['POST'])
def api_verify_code():
    data = request.json
    token, code = data.get('token'), data.get('code')
    
    if token not in web_auth_cache: return jsonify({"status":"error","error":"Сессия истекла"})
    
    c_data = web_auth_cache[token]
    try:
        loop = c_data['loop']
        loop.run_until_complete(c_data['client'].sign_in(c_data['phone'], code=code, phone_code_hash=c_data['hash']))
        session_str = c_data['client'].session.save()
        me = loop.run_until_complete(c_data['client'].get_me())
        
        # Сохраняем сессию в базу (привязываем к ID телеграма)
        loop.run_until_complete(save_user(me.id, session=session_str, phone=c_data['phone']))
        
        # Перезаписываем токен на актуальный ID пользователя
        loop.run_until_complete(save_user(me.id, token=token))
        
        del web_auth_cache[token]
        return jsonify({"status":"ok"})
    except Exception as e:
        return jsonify({"status":"error","error":"Неверный код или ошибка"})

@app.route('/api/save_templates', methods=['POST'])
def api_save_templates():
    data = request.json
    loop = asyncio.new_event_loop()
    p = loop.run_until_complete(get_user_by_token(data.get('token')))
    if p:
        tpls = [l.strip() for l in data.get('templates','').split('\n') if l.strip()]
        loop.run_until_complete(save_user(p['user_id'], templates=tpls))
    loop.close()
    return jsonify({"status":"ok"})

@app.route('/api/save_delay', methods=['POST'])
def api_save_delay():
    data = request.json
    loop = asyncio.new_event_loop()
    p = loop.run_until_complete(get_user_by_token(data.get('token')))
    if p:
        try:
            low, high = map(int, data.get('delay').split('-'))
            loop.run_until_complete(save_user(p['user_id'], min_d=low, max_d=high))
        except: pass
    loop.close()
    return jsonify({"status":"ok"})

@app.route('/api/start_spam', methods=['POST'])
def api_start_spam():
    data = request.json
    loop = asyncio.new_event_loop()
    p = loop.run_until_complete(get_user_by_token(data.get('token')))
    if p and not p['is_banned']:
        eng = SpamEngine(p['user_id'], data.get('target'), p['templates'], p['min_delay'], p['max_delay'], p['session'])
        spam_engines[p['user_id']] = eng
        asyncio.run_coroutine_threadsafe(eng.run(), loop_manager)
    loop.close()
    return jsonify({"status":"ok"})

@app.route('/api/stop_spam', methods=['POST'])
def api_stop_spam():
    data = request.json
    loop = asyncio.new_event_loop()
    p = loop.run_until_complete(get_user_by_token(data.get('token')))
    if p and p['user_id'] in spam_engines:
        spam_engines[p['user_id']].is_active = False
        del spam_engines[p['user_id']]
    loop.close()
    return jsonify({"status":"ok"})

@app.route('/api/stats')
def api_stats():
    return jsonify({
        "messages": system_metrics["total_messages_sent"],
        "errors": system_metrics["total_errors_caught"],
        "floods": system_metrics["flood_waits"],
        "uptime": str(datetime.datetime.now() - START_TIME).split('.')[0]
    })

# --- ADMIN API ---
@app.route('/api/admin/users')
def admin_users():
    loop = asyncio.new_event_loop()
    async def fetch():
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT user_id, phone, is_banned FROM users") as cursor:
                return [{"user_id": r[0], "phone": r[1], "is_banned": r[2]} for r in await cursor.fetchall()]
    users = loop.run_until_complete(fetch())
    loop.close()
    return jsonify(users)

@app.route('/api/admin/ban', methods=['POST'])
def admin_ban():
    data = request.json
    uid, ban = data.get('uid'), data.get('ban')
    loop = asyncio.new_event_loop()
    loop.run_until_complete(save_user(uid, is_banned=ban))
    if ban and uid in spam_engines:
        spam_engines[uid].is_active = False
        del spam_engines[uid]
    loop.close()
    return jsonify({"status":"ok"})

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# =====================================================================
# 7. ЗАПУСК
# =====================================================================
loop_manager = asyncio.new_event_loop()

async def main():
    await init_db()
    
    # Запускаем Flask в отдельном потоке
    threading.Thread(target=run_flask, daemon=True).start()
    log("SYS", "Flask Web Server запущен на порту 8080", Fore.GREEN)
    
    # Запускаем воркер задач
    asyncio.create_task(web_task_worker())
    
    try:
        await bot.start(bot_token=BOT_TOKEN)
        log("SYS", "Telegram Bot успешно запущен!", Fore.GREEN)
        await bot.run_until_disconnected()
    except AccessTokenInvalidError:
        log("CRITICAL", "Токен неверный, но сайт работает.", Fore.RED)
        while True: await asyncio.sleep(3600)
    except Exception as e:
        log("CRITICAL", f"Ошибка: {e}", Fore.RED)

if __name__ == '__main__':
    asyncio.set_event_loop(loop_manager)
    loop_manager.run_until_complete(main())
