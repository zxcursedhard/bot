import os
import sys
import json
import re
import asyncio
import random
import datetime
import threading
import shutil
import uuid
import time
from colorama import init, Fore, Style
from flask import Flask, jsonify, render_template_string, request
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

# Впиши свой ID, чтобы сразу стать админом (или оставь 0)
ADMIN_ID = 0 

DB_FILE = 'titan_database.db'
active_clients = {}
spam_engines = {}
user_steps = {}
START_TIME = datetime.datetime.now()

system_metrics = {"total_messages_sent": 0, "total_errors_caught": 0, "flood_waits": 0}

def log(tag, msg, color=Fore.WHITE):
    t = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{Fore.LIGHTBLACK_EX}[{t}]{Fore.RESET} {color}[{tag}]{Fore.RESET} {msg}")

# =====================================================================
# 2. БАЗА ДАННЫХ
# =====================================================================
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, 
            session_string TEXT, 
            templates TEXT DEFAULT '[]',
            min_delay INTEGER DEFAULT 100, 
            max_delay INTEGER DEFAULT 300,
            is_approved INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            web_token TEXT
        )''')
        await db.commit()

async def get_user(uid):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT session_string, templates, min_delay, max_delay, is_approved, is_admin, web_token FROM users WHERE user_id = ?", (uid,)) as cursor:
            row = await cursor.fetchone()
            if row: 
                return {'session': row[0], 'templates': json.loads(row[1]), 'min_delay': row[2], 'max_delay': row[3], 'is_approved': row[4], 'is_admin': row[5], 'web_token': row[6]}
    return None

async def get_user_by_token(token):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT user_id, session_string, templates, min_delay, max_delay FROM users WHERE web_token = ?", (token,)) as cursor:
            row = await cursor.fetchone()
            if row: 
                return {'user_id': row[0], 'session': row[1], 'templates': json.loads(row[2]), 'min_delay': row[3], 'max_delay': row[4]}
    return None

async def save_user(uid, session=None, templates=None, min_d=None, max_d=None, approved=None, admin=None, token=None):
    current = await get_user(uid) or {'session': None, 'templates': [], 'min_delay': 100, 'max_delay': 300, 'is_approved': 0, 'is_admin': 0, 'web_token': None}
    s = session if session is not None else current['session']
    t = templates if templates is not None else current['templates']
    mind = min_d if min_d is not None else current['min_delay']
    maxd = max_d if max_d is not None else current['max_delay']
    a = approved if approved is not None else current['is_approved']
    adm = admin if admin is not None else current['is_admin']
    tok = token if token is not None else current['web_token']
    
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''INSERT INTO users (user_id, session_string, templates, min_delay, max_delay, is_approved, is_admin, web_token)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
            session_string=excluded.session_string, templates=excluded.templates, 
            min_delay=excluded.min_delay, max_delay=excluded.max_delay,
            is_approved=excluded.is_approved, is_admin=excluded.is_admin, web_token=excluded.web_token''',
            (uid, s, json.dumps(t), mind, maxd, a, adm, tok))
        await db.commit()

# =====================================================================
# 3. ДВИЖОК СПАМА
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
        self.client = None

    async def run(self):
        try:
            self.client = TelegramClient(StringSession(self.session_str), API_ID, API_HASH)
            await self.client.connect()
            
            while self.is_active:
                try:
                    text = random.choice(self.templates) + "\u200b"
                    await self.client.send_message(self.target, text)
                    system_metrics["total_messages_sent"] += 1
                    log("SPAM", f"[{self.uid}] -> {self.target}", Fore.BLUE)
                    await asyncio.sleep(random.randint(self.min_d, self.max_d) / 1000.0)
                except FloodWaitError as e:
                    system_metrics["flood_waits"] += 1
                    log("FLOOD", f"Ждем {e.seconds} сек", Fore.RED)
                    await asyncio.sleep(e.seconds + 5)
                except Exception as e:
                    system_metrics["total_errors_caught"] += 1
                    log("ERROR", f"Сбой: {e}", Fore.RED)
                    break
        except Exception as e:
            log("ERROR", f"Ошибка клиента: {e}")
        finally:
            if self.client:
                await self.client.disconnect()
            self.is_active = False

# =====================================================================
# 4. БОТ TELEGRAM
# =====================================================================
bot = TelegramClient('bot_master', API_ID, API_HASH)

@bot.on(events.NewMessage(pattern='/start'))
async def cmd_start(e):
    global ADMIN_ID
    uid = e.sender_id
    p = await get_user(uid)
    
    if ADMIN_ID == 0:
        ADMIN_ID = uid
        await save_user(uid, admin=1, approved=1)
        p = await get_user(uid)

    is_admin = p and p['is_admin'] == 1
    is_approved = p and p['is_approved'] == 1

    if (is_admin or is_approved) and (not p or not p['web_token']):
        new_token = uuid.uuid4().hex
        await save_user(uid, token=new_token)
        p = await get_user(uid)

    if not is_admin and not is_approved:
        await e.reply(f"⛔ **Доступ закрыт.**\nВаш ID: `{uid}`\nПередайте его админу.", parse_mode='md')
        return

    link = f"https://bot-1-dk7y.onrender.com/dashboard?token={p['web_token']}"
    await e.reply(f"⚡ **Панель управления TITAN**\n\n[👉 Открыть веб-панель]({link})", parse_mode='md')

@bot.on(events.NewMessage)
async def text_handler(e):
    uid = e.sender_id
    text = e.text.strip()
    if text.startswith('/'): return
    
    p = await get_user(uid) or {}
    is_admin = p.get('is_admin') == 1

    if is_admin:
        if text.startswith('.approve'):
            try:
                t_id = int(text.split(' ')[1])
                await save_user(t_id, approved=1)
                await e.reply(f"✅ Доступ выдан: `{t_id}`")
            except: pass
            return

# =====================================================================
# 5. WEB API И БЭКЕНД (FLASK)
# =====================================================================
app = Flask(__name__)

# --- HTML ШАБЛОН ПРЕМИУМ КЛАССА ---
HTML_DASHBOARD = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TITAN | Enterprise Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #0f1115; --card: #1a1d24; --text: #e8e8ea; --mut: #9aa0a6;
            --acc: #4f8cff; --ok: #39c07a; --danger: #ff4d4d;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', sans-serif; }
        body { background: var(--bg); color: var(--text); min-height: 100vh; display: flex; flex-direction: column; }
        
        /* Header */
        .header { background: var(--card); padding: 15px 30px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #2a2f3a; }
        .logo { font-size: 20px; font-weight: 700; color: var(--acc); display: flex; align-items: center; gap: 10px; }
        .status-badge { background: rgba(57,192,122,0.1); color: var(--ok); padding: 5px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; border: 1px solid var(--ok); }
        
        /* Layout */
        .container { display: grid; grid-template-columns: 300px 1fr; gap: 20px; padding: 20px; max-width: 1200px; margin: 0 auto; width: 100%; }
        @media (max-width: 800px) { .container { grid-template-columns: 1fr; } }
        
        /* Cards */
        .card { background: var(--card); border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 4px 15px rgba(0,0,0,0.2); border: 1px solid #2a2f3a; }
        .card h3 { font-size: 16px; margin-bottom: 15px; color: #fff; display: flex; align-items: center; gap: 8px; }
        
        /* Stats */
        .stats-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }
        .stat-box { background: #11141a; padding: 15px; border-radius: 8px; text-align: center; }
        .stat-val { font-size: 24px; font-weight: 700; color: var(--acc); }
        .stat-name { font-size: 12px; color: var(--mut); margin-top: 5px; }
        
        /* Forms */
        label { display: block; font-size: 12px; color: var(--mut); margin-bottom: 5px; }
        input, textarea { width: 100%; background: #11141a; border: 1px solid #2a2f3a; color: #fff; padding: 10px; border-radius: 8px; font-size: 14px; margin-bottom: 10px; outline: none; transition: 0.2s; }
        input:focus, textarea:focus { border-color: var(--acc); }
        textarea { resize: vertical; min-height: 100px; }
        
        /* Buttons */
        button { width: 100%; padding: 12px; border: none; border-radius: 8px; font-weight: 600; cursor: pointer; transition: 0.2s; display: flex; justify-content: center; align-items: center; gap: 8px; }
        .btn-primary { background: var(--acc); color: #fff; }
        .btn-primary:hover { background: #3a7af2; }
        .btn-danger { background: var(--danger); color: #fff; }
        .btn-danger:hover { background: #e63946; }
        .btn-success { background: var(--ok); color: #fff; }
        .btn-success:hover { background: #2ea064; }
        
        /* Toast */
        #toast { position: fixed; bottom: 20px; right: 20px; background: var(--ok); color: #fff; padding: 12px 24px; border-radius: 8px; font-weight: 600; opacity: 0; transform: translateY(20px); transition: 0.3s; pointer-events: none; }
        #toast.show { opacity: 1; transform: translateY(0); }
    </style>
</head>
<body>

<div class="header">
    <div class="logo">⚡ TITAN <span style="font-weight:300; font-size:14px; color:var(--mut)">v10.5</span></div>
    <div class="status-badge" id="sessionStatus">Проверка сессии...</div>
</div>

<div class="container">
    <!-- Левая колонка: Статистика и Управление -->
    <div>
        <div class="card">
            <h3>📊 Мониторинг</h3>
            <div class="stats-grid">
                <div class="stat-box"><div class="stat-val" id="statSent">0</div><div class="stat-name">Отправлено</div></div>
                <div class="stat-box"><div class="stat-val" id="statErrors">0</div><div class="stat-name">Ошибок</div></div>
                <div class="stat-box"><div class="stat-val" id="statFloods">0</div><div class="stat-name">FloodWait</div></div>
                <div class="stat-box"><div class="stat-val" id="statUptime">0с</div><div class="stat-name">Аптайм</div></div>
            </div>
        </div>

        <div class="card">
            <h3>⚙️ Настройки</h3>
            <label>Задержка (мин - макс, мс)</label>
            <input type="text" id="delayInput" value="{{ min_d }}-{{ max_d }}">
            <button class="btn-primary" onclick="saveDelay()">Сохранить настройки</button>
        </div>
        
        <div class="card">
            <h3>🎯 Запуск</h3>
            <label>Цель (@username или ID)</label>
            <input type="text" id="targetInput" placeholder="@username">
            <button class="btn-success" onclick="startSpam()">🚀 Запустить рассылку</button>
            <button class="btn-danger" style="margin-top:10px;" onclick="stopSpam()">🛑 Остановить</button>
        </div>
    </div>

    <!-- Правая колонка: Шаблоны -->
    <div>
        <div class="card" style="height: 100%; display: flex; flex-direction: column;">
            <h3>📝 Ваши шаблоны (построчно)</h3>
            <p style="font-size:12px; color:var(--mut); margin-bottom:10px;">Каждая строка будет отправлена как отдельное сообщение.</p>
            <textarea id="templatesInput" style="flex: 1; min-height: 400px;">{{ templates }}</textarea>
            <button class="btn-primary" style="margin-top:15px;" onclick="saveTemplates()">💾 Сохранить шаблоны</button>
        </div>
    </div>
</div>

<div id="toast">Сохранено!</div>

<script>
    const TOKEN = "{{ token }}";
    
    // Функция для показа уведомлений
    function showToast(msg, color = "#39c07a") {
        const t = document.getElementById('toast');
        t.innerText = msg;
        t.style.background = color;
        t.classList.add('show');
        setTimeout(() => t.classList.remove('show'), 3000);
    }

    // Сохранение шаблонов
    async function saveTemplates() {
        const text = document.getElementById('templatesInput').value;
        const res = await fetch('/api/save_templates', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({token: TOKEN, templates: text})
        });
        if(res.ok) showToast("Шаблоны сохранены!");
        else showToast("Ошибка сохранения!", "#ff4d4d");
    }

    // Сохранение задержки
    async function saveDelay() {
        const val = document.getElementById('delayInput').value;
        const res = await fetch('/api/save_delay', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({token: TOKEN, delay: val})
        });
        if(res.ok) showToast("Задержка обновлена!");
        else showToast("Ошибка!", "#ff4d4d");
    }

    // Запуск
    async function startSpam() {
        const target = document.getElementById('targetInput').value;
        if(!target) return showToast("Укажи цель!", "#ff9800");
        
        const res = await fetch('/api/start_spam', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({token: TOKEN, target: target})
        });
        if(res.ok) showToast("🚀 Рассылка запущена!");
        else showToast("Ошибка запуска!", "#ff4d4d");
    }

    // Остановка
    async function stopSpam() {
        const res = await fetch('/api/stop_spam', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({token: TOKEN})
        });
        if(res.ok) showToast("🛑 Процессы остановлены!", "#ff4d4d");
    }

    // Поллинг статистики каждые 2 секунды
    setInterval(async () => {
        const res = await fetch(`/api/stats?token=${TOKEN}`);
        if(res.ok) {
            const data = await res.json();
            document.getElementById('statSent').innerText = data.messages;
            document.getElementById('statErrors').innerText = data.errors;
            document.getElementById('statFloods').innerText = data.floods;
            document.getElementById('statUptime').innerText = data.uptime;
            
            const statusEl = document.getElementById('sessionStatus');
            if(data.has_session) {
                statusEl.innerText = "Сессия активна";
                statusEl.style.color = "#39c07a";
                statusEl.style.borderColor = "#39c07a";
                statusEl.style.background = "rgba(57,192,122,0.1)";
            } else {
                statusEl.innerText = "Нет сессии (зайди в бота)";
                statusEl.style.color = "#ff4d4d";
                statusEl.style.borderColor = "#ff4d4d";
                statusEl.style.background = "rgba(255,77,77,0.1)";
            }
        }
    }, 2000);
</script>
</body>
</html>
"""

HTML_DENIED = "<h1 style='color:red; text-align:center; margin-top:50px; font-family:sans-serif;'>403 ACCESS DENIED</h1>"

@app.route('/')
def index(): return HTML_DENIED

@app.route('/dashboard')
def dashboard():
    token = request.args.get('token')
    if not token: return HTML_DENIED
    
    loop = asyncio.new_event_loop()
    p = loop.run_until_complete(get_user_by_token(token))
    loop.close()
    
    if not p: return HTML_DENIED
    
    tpl_str = "\n".join(p['templates'])
    return render_template_string(HTML_DASHBOARD, token=token, templates=tpl_str, min_d=p['min_delay'], max_d=p['max_delay'])

@app.route('/api/save_templates', methods=['POST'])
def api_save_templates():
    data = request.json
    token = data.get('token')
    raw = data.get('templates', '')
    tpls = [line.strip() for line in raw.split('\n') if line.strip()]
    
    loop = asyncio.new_event_loop()
    p = loop.run_until_complete(get_user_by_token(token))
    if p:
        loop.run_until_complete(save_user(p['user_id'], templates=tpls))
    loop.close()
    return jsonify({"status": "ok"})

@app.route('/api/save_delay', methods=['POST'])
def api_save_delay():
    data = request.json
    token = data.get('token')
    delay_str = data.get('delay', '')
    try:
        low, high = map(int, delay_str.split('-'))
        loop = asyncio.new_event_loop()
        p = loop.run_until_complete(get_user_by_token(token))
        if p:
            loop.run_until_complete(save_user(p['user_id'], min_d=low, max_d=high))
        loop.close()
    except: pass
    return jsonify({"status": "ok"})

@app.route('/api/start_spam', methods=['POST'])
def api_start_spam():
    data = request.json
    token = data.get('token')
    target = data.get('target')
    
    loop = asyncio.new_event_loop()
    p = loop.run_until_complete(get_user_by_token(token))
    if p:
        # Запускаем движок
        eng = SpamEngine(p['user_id'], target, p['templates'], p['min_delay'], p['max_delay'], p['session'])
        spam_engines[p['user_id']] = eng
        asyncio.run_coroutine_threadsafe(eng.run(), loop_manager)
    loop.close()
    return jsonify({"status": "ok"})

@app.route('/api/stop_spam', methods=['POST'])
def api_stop_spam():
    data = request.json
    token = data.get('token')
    loop = asyncio.new_event_loop()
    p = loop.run_until_complete(get_user_by_token(token))
    if p and p['user_id'] in spam_engines:
        spam_engines[p['user_id']].is_active = False
        del spam_engines[p['user_id']]
    loop.close()
    return jsonify({"status": "ok"})

@app.route('/api/stats')
def api_stats():
    token = request.args.get('token')
    loop = asyncio.new_event_loop()
    p = loop.run_until_complete(get_user_by_token(token))
    loop.close()
    
    uptime = str(datetime.datetime.now() - START_TIME).split('.')[0]
    
    return jsonify({
        "messages": system_metrics["total_messages_sent"],
        "errors": system_metrics["total_errors_caught"],
        "floods": system_metrics["flood_waits"],
        "uptime": uptime,
        "has_session": bool(p and p['session'])
    })

@app.route('/healthz')
def health(): return jsonify({"status": "OK"}), 200

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
    try:
        await bot.start(bot_token=BOT_TOKEN)
        log("SYS", "Бот запущен!", Fore.GREEN)
        await bot.run_until_disconnected()
    except AccessTokenInvalidError:
        log("CRITICAL", "Токен неверный, но сайт работает.", Fore.RED)
        while True: await asyncio.sleep(3600)

if __name__ == '__main__':
    asyncio.set_event_loop(loop_manager)
    loop_manager.run_until_complete(main())
