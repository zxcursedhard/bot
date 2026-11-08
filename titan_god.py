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
# 1. КОНСТАНТЫ И НАСТРОЙКИ
# =====================================================================
API_ID = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'
BOT_TOKEN = '8732669492:AAGiMPHbCs8ntMzFhkfS8466iIVVa67w47g' 

# ТВОИ ДВА ID (Доступ к Админке)
ADMIN_IDS = [9058235147, 8773171051]

DB_FILE = 'titan_saas.db'
BACKUP_DIR = 'db_backups'
START_TIME = datetime.datetime.now()

active_clients = {}     
spam_engines = {}       
web_auth_cache = {}     
user_states = {}        

system_metrics = {"total_messages_sent": 0, "total_errors_caught": 0, "flood_waits": 0}

def log(tag, msg, color=Fore.WHITE):
    t = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{Fore.LIGHTBLACK_EX}[{t}]{Fore.RESET} {color}[{tag}]{Fore.RESET} {msg}")

# =====================================================================
# 2. МОДУЛЬ БАЗЫ ДАННЫХ (SQLITE)
# =====================================================================
async def init_db():
    if not os.path.exists(BACKUP_DIR): os.makedirs(BACKUP_DIR)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, phone TEXT, session_string TEXT, 
            templates TEXT DEFAULT '[]', min_delay INTEGER DEFAULT 100, max_delay INTEGER DEFAULT 300,
            is_banned INTEGER DEFAULT 0, is_admin INTEGER DEFAULT 0, web_token TEXT, registered_at TEXT
        )''')
        await db.execute('''CREATE TABLE IF NOT EXISTS web_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT,
            payload TEXT, status TEXT DEFAULT 'PENDING', created_at TEXT
        )''')
        await db.commit()
        log("DB", "База данных готова.", Fore.GREEN)

async def get_user(uid):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT session_string, templates, min_delay, max_delay, is_banned, is_admin, web_token FROM users WHERE user_id = ?", (uid,)) as cursor:
            row = await cursor.fetchone()
            if row: 
                return {
                    'session': row[0], 'templates': json.loads(row[1]), 'min_delay': row[2], 'max_delay': row[3], 
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
    s = kwargs.get('session', current['session']); t = kwargs.get('templates', current['templates'])
    mind = kwargs.get('min_d', current['min_delay']); maxd = kwargs.get('max_d', current['max_delay'])
    b = kwargs.get('is_banned', current['is_banned']); adm = kwargs.get('is_admin', current['is_admin'])
    token = kwargs.get('token', current['web_token']); phone = kwargs.get('phone', None)
    
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''INSERT INTO users (user_id, phone, session_string, templates, min_delay, max_delay, is_banned, is_admin, web_token, registered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET
            phone=COALESCE(excluded.phone, users.phone), session_string=excluded.session_string, templates=excluded.templates, 
            min_delay=excluded.min_delay, max_delay=excluded.max_delay, is_banned=excluded.is_banned, is_admin=excluded.is_admin, web_token=excluded.web_token''',
            (uid, phone, s, json.dumps(t), mind, maxd, b, adm, token, str(datetime.datetime.now())))
        await db.commit()

# =====================================================================
# 3. ДВИЖОК СПАМА
# =====================================================================
class SpamEngine:
    def __init__(self, uid, target, templates, min_d, max_d, session_str):
        self.uid = uid; self.target = target; self.templates = templates
        self.min_d = min_d; self.max_d = max_d; self.session_str = session_str
        self.is_active = True; self.counter = 0; self.client = None

    async def run(self):
        try:
            self.client = TelegramClient(StringSession(self.session_str), API_ID, API_HASH)
            await self.client.connect()
            while self.is_active:
                try:
                    if not self.templates: break
                    text = random.choice(self.templates)
                    stealth = "".join([random.choice(['\u200b', '\u200c', '\u200d']) for _ in range(random.randint(2, 6))])
                    await self.client.send_message(self.target, f"{text}{stealth}")
                    self.counter += 1
                    system_metrics["total_messages_sent"] += 1
                    await asyncio.sleep(random.randint(self.min_d, self.max_d) / 1000.0)
                except FloodWaitError as e:
                    system_metrics["flood_waits"] += 1
                    await asyncio.sleep(e.seconds + 10)
                except Exception:
                    system_metrics["total_errors_caught"] += 1
                    break
        except Exception:
            pass
        finally:
            if self.client: await self.client.disconnect()
            self.is_active = False

# =====================================================================
# 4. ВОРКЕР ЗАДАЧ
# =====================================================================
async def web_task_worker():
    while True:
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute("SELECT id, user_id, action, payload FROM web_tasks WHERE status = 'PENDING'") as cursor:
                    for t_id, u_id, action, payload in await cursor.fetchall():
                        payload = json.loads(payload)
                        if action == "START_SPAM":
                            p = await get_user(u_id)
                            if p and not p['is_banned']:
                                eng = SpamEngine(u_id, payload['target'], p['templates'], p['min_delay'], p['max_delay'], p['session'])
                                spam_engines[u_id] = eng
                                asyncio.run_coroutine_threadsafe(eng.run(), loop_manager)
                        await db.execute("UPDATE web_tasks SET status = 'DONE' WHERE id = ?", (t_id,))
                        await db.commit()
        except Exception: pass
        await asyncio.sleep(2)

# =====================================================================
# 5. БОТ
# =====================================================================
bot = TelegramClient('bot_saas', API_ID, API_HASH)

@bot.on(events.NewMessage(pattern='/start'))
async def cmd_start(e):
    uid = e.sender_id
    p = await get_user(uid)
    if not p or not p['web_token']:
        token = uuid.uuid4().hex
        await save_user(uid, token=token)
        p = await get_user(uid)
    link = f"https://bot-1-dk7y.onrender.com/login?token={p['web_token']}"
    await e.reply(f"⚡ **TITAN SaaS**\n\n[👉 Войти в панель]({link})", parse_mode='md')

# =====================================================================
# 6. WEB UI (FLASK) - PREMIUM DESIGN
# =====================================================================
app = Flask(__name__)

# --- СТРАНИЦА ЛОГИНА ---
HTML_LOGIN = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TITAN | Authentication</title>
    <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root { --bg: #050508; --card: rgba(20, 22, 30, 0.7); --text: #fff; --acc: #6366f1; --err: #ef4444; }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Manrope', sans-serif; }
        body { 
            background: var(--bg); color: var(--text); display: flex; justify-content: center; align-items: center; height: 100vh;
            background-image: radial-gradient(circle at 15% 50%, rgba(99, 102, 241, 0.15), transparent 25%), 
                              radial-gradient(circle at 85% 30%, rgba(236, 72, 153, 0.15), transparent 25%);
        }
        .login-box { 
            background: var(--card); backdrop-filter: blur(20px); border: 1px solid rgba(255,255,255,0.1);
            padding: 50px 40px; border-radius: 24px; width: 380px; text-align: center;
            box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5); position: relative; overflow: hidden;
        }
        .login-box::before { content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 4px; background: linear-gradient(90deg, #6366f1, #ec4899); }
        h2 { font-weight: 800; font-size: 28px; margin-bottom: 10px; letter-spacing: -0.5px; }
        p.sub { color: #94a3b8; font-size: 14px; margin-bottom: 30px; }
        .input-group { position: relative; margin-bottom: 20px; text-align: left; }
        .input-group i { position: absolute; left: 18px; top: 18px; color: #64748b; font-size: 18px; }
        input { 
            width: 100%; padding: 16px 16px 16px 50px; background: rgba(0,0,0,0.4); border: 1px solid rgba(255,255,255,0.05); 
            color: #fff; border-radius: 12px; font-size: 15px; outline: none; transition: 0.3s; font-family: 'Manrope';
        }
        input:focus { border-color: var(--acc); box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.2); }
        button { 
            width: 100%; padding: 16px; background: linear-gradient(135deg, #6366f1, #8b5cf6); border: none; color: #fff; 
            border-radius: 12px; cursor: pointer; font-weight: 700; font-size: 16px; transition: 0.3s; margin-top: 10px;
            box-shadow: 0 10px 20px -10px rgba(99, 102, 241, 0.5);
        }
        button:hover { transform: translateY(-2px); box-shadow: 0 15px 25px -10px rgba(99, 102, 241, 0.6); }
        .step2 { display: none; }
        .error-text { color: var(--err); font-size: 13px; margin-top: 15px; min-height: 20px; font-weight: 600; }
    </style>
</head>
<body>
    <div class="login-box">
        <h2>TITAN SAAS</h2>
        <p class="sub">Вход в панель управления</p>
        <div class="step1" id="step1">
            <div class="input-group"><i class="fa-solid fa-phone"></i><input type="text" id="phone" placeholder="+7 (999) 000-00-00"></div>
            <button onclick="sendCode()">Получить код</button>
        </div>
        <div class="step2" id="step2">
            <div class="input-group"><i class="fa-solid fa-lock"></i><input type="text" id="code" placeholder="Код из Telegram"></div>
            <button onclick="verifyCode()">Войти в систему</button>
        </div>
        <div class="error-text" id="error"></div>
    </div>
    <script>
        const TOKEN = "{{ token }}";
        async function sendCode() {
            const phone = document.getElementById('phone').value;
            if(!phone) return document.getElementById('error').innerText = "Введите номер!";
            const res = await fetch('/api/send_code', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token: TOKEN, phone: phone}) });
            const data = await res.json();
            if(data.status === 'ok') { document.getElementById('step1').style.display = 'none'; document.getElementById('step2').style.display = 'block'; } 
            else { document.getElementById('error').innerText = data.error; }
        }
        async function verifyCode() {
            const code = document.getElementById('code').value;
            const res = await fetch('/api/verify_code', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token: TOKEN, code: code}) });
            const data = await res.json();
            if(data.status === 'ok') { window.location.href = '/dashboard?token=' + TOKEN; } 
            else { document.getElementById('error').innerText = data.error; }
        }
    </script>
</body>
</html>
"""

# --- ДАШБОРД ---
HTML_DASHBOARD = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TITAN | Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root { --bg: #050508; --side: rgba(15, 17, 26, 0.8); --card: rgba(22, 25, 35, 0.6); --text: #fff; --mut: #94a3b8; --acc: #6366f1; --ok: #10b981; --err: #ef4444; --warn: #f59e0b; }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Manrope', sans-serif; }
        body { background: var(--bg); color: var(--text); display: flex; height: 100vh; overflow: hidden; background-image: radial-gradient(circle at 50% 0%, rgba(99, 102, 241, 0.1), transparent 40%); }

        /* Sidebar */
        .sidebar { width: 270px; background: var(--side); backdrop-filter: blur(20px); border-right: 1px solid rgba(255,255,255,0.05); padding: 30px 20px; display: flex; flex-direction: column; }
        .logo { font-size: 22px; font-weight: 800; color: #fff; margin-bottom: 40px; display: flex; align-items: center; gap: 12px; letter-spacing: -0.5px; }
        .logo i { color: var(--acc); }
        .nav-item { display: flex; align-items: center; gap: 15px; padding: 14px 18px; border-radius: 12px; color: var(--mut); font-weight: 600; font-size: 15px; cursor: pointer; transition: 0.2s; margin-bottom: 8px; border: 1px solid transparent; }
        .nav-item:hover { background: rgba(255,255,255,0.05); color: #fff; }
        .nav-item.active { background: linear-gradient(90deg, rgba(99, 102, 241, 0.15), transparent); color: var(--acc); border-left: 3px solid var(--acc); border-radius: 4px 12px 12px 4px; }

        /* Main Content */
        .main { flex: 1; padding: 40px; overflow-y: auto; }
        .top-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 40px; }
        .top-bar h1 { font-size: 32px; font-weight: 800; letter-spacing: -1px; }
        .badge { padding: 8px 16px; border-radius: 30px; font-size: 13px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
        .badge.ok { background: rgba(16, 185, 129, 0.1); color: var(--ok); border: 1px solid rgba(16, 185, 129, 0.2); }
        .badge.err { background: rgba(239, 68, 68, 0.1); color: var(--err); border: 1px solid rgba(239, 68, 68, 0.2); }

        /* Cards & Grid */
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 25px; }
        .card { background: var(--card); backdrop-filter: blur(10px); border-radius: 20px; padding: 30px; border: 1px solid rgba(255,255,255,0.05); box-shadow: 0 10px 30px -10px rgba(0,0,0,0.3); }
        .card h3 { font-size: 18px; font-weight: 700; margin-bottom: 25px; display: flex; align-items: center; gap: 10px; color: #fff; }
        .card h3 i { color: var(--acc); }
        
        /* Stats */
        .stat-box { background: rgba(0,0,0,0.3); border-radius: 16px; padding: 20px; text-align: center; border: 1px solid rgba(255,255,255,0.03); }
        .stat-val { font-size: 36px; font-weight: 800; background: linear-gradient(135deg, #fff, #94a3b8); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .stat-name { font-size: 13px; color: var(--mut); font-weight: 600; margin-top: 5px; text-transform: uppercase; letter-spacing: 0.5px; }

        /* Forms */
        label { display: block; font-size: 13px; color: var(--mut); margin-bottom: 10px; font-weight: 600; }
        input, textarea { width: 100%; background: rgba(0,0,0,0.4); border: 1px solid rgba(255,255,255,0.1); color: #fff; padding: 16px; border-radius: 12px; font-size: 15px; margin-bottom: 20px; outline: none; transition: 0.3s; font-family: 'Manrope'; }
        input:focus, textarea:focus { border-color: var(--acc); box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.2); }
        button { padding: 16px 24px; border: none; border-radius: 12px; font-weight: 700; font-size: 15px; cursor: pointer; transition: 0.3s; display: flex; justify-content: center; align-items: center; gap: 10px; width: 100%; color: #fff; }
        button:hover { transform: translateY(-2px); }
        .btn-primary { background: linear-gradient(135deg, #6366f1, #8b5cf6); box-shadow: 0 10px 20px -10px rgba(99, 102, 241, 0.4); }
        .btn-danger { background: linear-gradient(135deg, #ef4444, #b91c1c); box-shadow: 0 10px 20px -10px rgba(239, 68, 68, 0.4); }
        .btn-ok { background: linear-gradient(135deg, #10b981, #047857); box-shadow: 0 10px 20px -10px rgba(16, 185, 129, 0.4); }

        /* Admin Table */
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 18px; text-align: left; border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 15px; font-weight: 500; }
        th { color: var(--mut); font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; }
        tr:hover td { background: rgba(255,255,255,0.02); }
        .btn-sm { padding: 8px 16px; font-size: 13px; border-radius: 8px; width: auto; display: inline-flex; }
        .badge-admin { background: rgba(245, 158, 11, 0.2); color: var(--warn); padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: 700; }
        
        .hidden { display: none !important; }
        #toast { position: fixed; bottom: 30px; right: 30px; background: var(--ok); color: #fff; padding: 16px 28px; border-radius: 12px; font-weight: 700; box-shadow: 0 10px 30px -10px rgba(0,0,0,0.5); opacity: 0; transform: translateY(20px); transition: 0.3s; pointer-events: none; z-index: 9999; display: flex; align-items: center; gap: 10px; }
        #toast.show { opacity: 1; transform: translateY(0); }
    </style>
</head>
<body>

<div class="sidebar">
    <div class="logo"><i class="fa-solid fa-bolt"></i> TITAN <span style="font-size:12px;color:var(--mut);font-weight:400;">v12.0</span></div>
    <div class="nav-item active" id="nav-dash" onclick="showTab('dash')"><i class="fa-solid fa-chart-pie"></i> Дашборд</div>
    <div class="nav-item" id="nav-spam" onclick="showTab('spam')"><i class="fa-solid fa-rocket"></i> Рассылка</div>
    <div class="nav-item" id="nav-settings" onclick="showTab('settings')"><i class="fa-solid fa-gear"></i> Настройки</div>
    {% if is_admin %}
    <div class="nav-item" id="nav-admin" onclick="showTab('admin')" style="color:var(--warn);margin-top:20px;"><i class="fa-solid fa-crown"></i> Админ-панель</div>
    {% endif %}
</div>

<div class="main">
    <div class="top-bar">
        <h1 id="pageTitle">Дашборд</h1>
        <div class="badge {% if session %}ok{% else %}err{% endif %}" id="sessionStatus">
            <i class="fa-solid {% if session %}fa-check-circle{% else %}fa-exclamation-circle{% endif %}"></i>
            {% if session %}Сессия активна{% else %}Нет сессии{% endif %}
        </div>
    </div>

    <!-- ДАШБОРД -->
    <div id="tab-dash">
        <div class="grid">
            <div class="card">
                <h3><i class="fa-solid fa-chart-simple"></i> Статистика</h3>
                <div class="grid" style="grid-template-columns:1fr 1fr;gap:15px;">
                    <div class="stat-box"><div class="stat-val" id="statSent">0</div><div class="stat-name">Отправлено</div></div>
                    <div class="stat-box"><div class="stat-val" id="statErr">0</div><div class="stat-name">Ошибок</div></div>
                    <div class="stat-box"><div class="stat-val" id="statFlood">0</div><div class="stat-name">FloodWait</div></div>
                    <div class="stat-box"><div class="stat-val" id="statUp">0с</div><div class="stat-name">Аптайм</div></div>
                </div>
            </div>
            <div class="card">
                <h3><i class="fa-solid fa-file-lines"></i> Шаблоны сообщений</h3>
                <textarea id="templates" style="height:120px;">{{ templates }}</textarea>
                <button class="btn-primary" onclick="saveTpl()"><i class="fa-solid fa-floppy-disk"></i> Сохранить</button>
            </div>
        </div>
    </div>

    <!-- РАССЫЛКА -->
    <div id="tab-spam" class="hidden">
        <div class="card" style="max-width: 600px;">
            <h3><i class="fa-solid fa-bullseye"></i> Запуск</h3>
            <label>Цель (@username или ID чата)</label>
            <input type="text" id="target" placeholder="@username">
            <button class="btn-ok" onclick="startSpam()"><i class="fa-solid fa-play"></i> Запустить</button>
            <button class="btn-danger" style="margin-top:15px;" onclick="stopSpam()"><i class="fa-solid fa-stop"></i> Остановить</button>
        </div>
    </div>

    <!-- НАСТРОЙКИ -->
    <div id="tab-settings" class="hidden">
        <div class="card" style="max-width: 600px;">
            <h3><i class="fa-solid fa-sliders"></i> Конфигурация</h3>
            <label>Задержка (мин - макс, мс)</label>
            <input type="text" id="delay" value="{{ min_d }}-{{ max_d }}">
            <button class="btn-primary" onclick="saveDelay()"><i class="fa-solid fa-floppy-disk"></i> Сохранить</button>
        </div>
    </div>

    <!-- АДМИНКА -->
    {% if is_admin %}
    <div id="tab-admin" class="hidden">
        <div class="card">
            <h3><i class="fa-solid fa-users-gear"></i> Управление пользователями</h3>
            <table>
                <thead><tr><th>ID</th><th>Телефон</th><th>Статус</th><th>Действие</th></tr></thead>
                <tbody id="userList"></tbody>
            </table>
        </div>
    </div>
    {% endif %}
</div>

<div id="toast"><i class="fa-solid fa-check-circle"></i> <span id="toastText">Сохранено!</span></div>

<script>
    const TOKEN = "{{ token }}";
    const IS_ADMIN = {{ 'true' if is_admin else 'false' }};
    
    function showToast(msg, isError = false) {
        const t = document.getElementById('toast');
        const txt = document.getElementById('toastText');
        const icon = t.querySelector('i');
        txt.innerText = msg;
        t.style.background = isError ? 'var(--err)' : 'var(--ok)';
        icon.className = isError ? 'fa-solid fa-circle-exclamation' : 'fa-solid fa-check-circle';
        t.classList.add('show'); setTimeout(() => t.classList.remove('show'), 3000);
    }

    function showTab(id) {
        document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
        document.getElementById('nav-' + id).classList.add('active');
        document.querySelectorAll('.main > div[id^="tab-"]').forEach(el => el.classList.add('hidden'));
        document.getElementById('tab-' + id).classList.remove('hidden');
        document.getElementById('pageTitle').innerText = document.getElementById('nav-' + id).innerText.trim();
        if(id === 'admin') loadUsers();
    }

    async function saveTpl() {
        const txt = document.getElementById('templates').value;
        await fetch('/api/save_templates', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({token: TOKEN, templates: txt})});
        showToast("Шаблоны сохранены!");
    }
    async function saveDelay() {
        const val = document.getElementById('delay').value;
        await fetch('/api/save_delay', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({token: TOKEN, delay: val})});
        showToast("Задержка обновлена!");
    }
    async function startSpam() {
        const tg = document.getElementById('target').value;
        if(!tg) return showToast("Укажи цель!", true);
        await fetch('/api/start_spam', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({token: TOKEN, target: tg})});
        showToast("🚀 Рассылка запущена!");
    }
    async function stopSpam() {
        await fetch('/api/stop_spam', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({token: TOKEN})});
        showToast("🛑 Остановлено!", true);
    }

    if(IS_ADMIN) {
        async function loadUsers() {
            const res = await fetch('/api/admin/users');
            const users = await res.json();
            const tb = document.getElementById('userList');
            tb.innerHTML = '';
            users.forEach(u => {
                const tr = document.createElement('tr');
                tr.innerHTML = `<td><b>${u.user_id}</b></td>
                                <td>${u.phone || '<span style="color:var(--mut)">-</span>'}</td>
                                <td><span style="color:${u.is_banned ? 'var(--err)' : 'var(--ok)'}; font-weight:700;"><i class="fa-solid ${u.is_banned ? 'fa-ban' : 'fa-check'}"></i> ${u.is_banned ? 'Забанен' : 'Активен'}</span></td>
                                <td><button class="btn-sm ${u.is_banned ? 'btn-ok' : 'btn-danger'}" onclick="toggleBan(${u.user_id}, ${u.is_banned ? 0 : 1})"><i class="fa-solid ${u.is_banned ? 'fa-unlock' : 'fa-lock'}"></i> ${u.is_banned ? 'Разбанить' : 'Забанить'}</button></td>`;
                tb.appendChild(tr);
            });
        }
        async function toggleBan(uid, ban) {
            await fetch('/api/admin/ban', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({uid: uid, ban: ban})});
            loadUsers();
            showToast(ban ? "Пользователь забанен" : "Пользователь разбанен", ban);
        }
    }

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
    return render_template_string(HTML_DASHBOARD, token=token, templates="\n".join(p['templates']), min_d=p['min_delay'], max_d=p['max_delay'], session=p['session'], is_admin=is_admin)

# --- WEB API ---
@app.route('/api/send_code', methods=['POST'])
def api_send_code():
    data = request.json
    token, phone = data.get('token'), data.get('phone')
    loop = asyncio.new_event_loop()
    p = loop.run_until_complete(get_user_by_token(token))
    
    if not p: 
        loop.close(); return jsonify({"status":"error","error":"Токен недействителен"})
    if p['is_banned']: 
        loop.close(); return jsonify({"status":"error","error":"Вы забанены"})
    
    try:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        loop.run_until_complete(client.connect())
        req = loop.run_until_complete(client.send_code_request(phone))
        web_auth_cache[token] = {'client': client, 'phone': phone, 'hash': req.phone_code_hash, 'loop': loop}
        return jsonify({"status":"ok"})
    except Exception as e:
        loop.close(); return jsonify({"status":"error","error": str(e)})

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
        
        loop.run_until_complete(save_user(me.id, session=session_str, phone=c_data['phone'], token=token))
        del web_auth_cache[token]
        return jsonify({"status":"ok"})
    except Exception:
        return jsonify({"status":"error","error":"Неверный код"})

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
    return jsonify({"messages": system_metrics["total_messages_sent"], "errors": system_metrics["total_errors_caught"], "floods": system_metrics["flood_waits"], "uptime": str(datetime.datetime.now() - START_TIME).split('.')[0]})

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
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.create_task(web_task_worker())
    try:
        await bot.start(bot_token=BOT_TOKEN)
        await bot.run_until_disconnected()
    except AccessTokenInvalidError:
        while True: await asyncio.sleep(3600)

if __name__ == '__main__':
    asyncio.set_event_loop(loop_manager)
    loop_manager.run_until_complete(main())
