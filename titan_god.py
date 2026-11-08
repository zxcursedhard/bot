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

# Если 0, первый зашедший станет админом. Если знаешь свой ID, впиши сюда.
ADMIN_ID = 0 

DB_FILE = 'titan_database.db'
active_clients = {}
spam_engines = {}
user_steps = {}

def log(tag, msg, color=Fore.WHITE):
    t = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{Fore.LIGHTBLACK_EX}[{t}]{Fore.RESET} {color}[{tag}]{Fore.RESET} {msg}")

# =====================================================================
# 2. БАЗА ДАННЫХ
# =====================================================================
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        # Таблица пользователей
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, 
            session_string TEXT, 
            templates TEXT DEFAULT '[]',
            min_delay INTEGER DEFAULT 100, 
            max_delay INTEGER DEFAULT 300,
            is_approved INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0
        )''')
        # Очередь задач от сайта
        await db.execute('''CREATE TABLE IF NOT EXISTS web_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            payload TEXT,
            status TEXT DEFAULT 'PENDING'
        )''')
        await db.commit()

async def get_user(uid):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT session_string, templates, min_delay, max_delay, is_approved, is_admin FROM users WHERE user_id = ?", (uid,)) as cursor:
            row = await cursor.fetchone()
            if row: 
                return {
                    'session': row[0], 
                    'templates': json.loads(row[1]), 
                    'min_delay': row[2], 
                    'max_delay': row[3],
                    'is_approved': row[4],
                    'is_admin': row[5]
                }
    return None

async def save_user(uid, session=None, templates=None, min_d=None, max_d=None, approved=None, admin=None):
    current = await get_user(uid) or {'session': None, 'templates': [], 'min_delay': 100, 'max_delay': 300, 'is_approved': 0, 'is_admin': 0}
    s = session if session is not None else current['session']
    t = templates if templates is not None else current['templates']
    mind = min_d if min_d is not None else current['min_delay']
    maxd = max_d if max_d is not None else current['max_delay']
    a = approved if approved is not None else current['is_approved']
    adm = admin if admin is not None else current['is_admin']
    
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''INSERT INTO users (user_id, session_string, templates, min_delay, max_delay, is_approved, is_admin)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
            session_string=excluded.session_string, templates=excluded.templates, 
            min_delay=excluded.min_delay, max_delay=excluded.max_delay,
            is_approved=excluded.is_approved, is_admin=excluded.is_admin''',
            (uid, s, json.dumps(t), mind, maxd, a, adm))
        await db.commit()

async def create_web_task(uid, action, payload):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO web_tasks (user_id, action, payload) VALUES (?, ?, ?)", (uid, action, json.dumps(payload)))
        await db.commit()

# =====================================================================
# 3. ДВИЖОК СПАМА
# =====================================================================
async def spam_engine(uid, target, templates, min_d, max_d):
    p = await get_user(uid)
    if not p or not p['session']: return
    
    c = TelegramClient(StringSession(p['session']), API_ID, API_HASH)
    await c.connect()
    
    while True:
        try:
            text = random.choice(templates) + "\u200b"
            await c.send_message(target, text)
            log("SPAM", f"[{uid}] -> {target}", Fore.BLUE)
            await asyncio.sleep(random.randint(min_d, max_d) / 1000.0)
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 5)
        except Exception:
            break
    await c.disconnect()

# =====================================================================
# 4. ОБРАБОТЧИК WEB-ЗАДАЧ
# =====================================================================
async def task_worker():
    while True:
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute("SELECT id, user_id, action, payload FROM web_tasks WHERE status = 'PENDING'") as cursor:
                    tasks = await cursor.fetchall()
                    for t_id, u_id, action, payload in tasks:
                        payload = json.loads(payload)
                        if action == "START_SPAM":
                            p = await get_user(u_id)
                            if p:
                                asyncio.create_task(spam_engine(u_id, payload['target'], p['templates'], p['min_delay'], p['max_delay']))
                        
                        await db.execute("UPDATE web_tasks SET status = 'DONE' WHERE id = ?", (t_id,))
                        await db.commit()
        except Exception as e:
            log("WEB", f"Ошибка таски: {e}", Fore.RED)
        await asyncio.sleep(2)

# =====================================================================
# 5. БОТ TELEGRAM
# =====================================================================
bot = TelegramClient('bot_master', API_ID, API_HASH)

def main_menu(uid, is_admin):
    menu = [
        [Button.inline("👤 Аккаунт", b"nav_account"), Button.inline("📝 Шаблоны", b"nav_templates")],
        [Button.inline("⚙️ Задержка", b"nav_delay"), Button.inline("🚀 Спам", b"nav_spam")]
    ]
    if is_admin:
        menu.append([Button.inline("👑 Админка", b"nav_admin")])
    return menu

@bot.on(events.NewMessage(pattern='/start'))
async def cmd_start(e):
    global ADMIN_ID
    uid = e.sender_id
    p = await get_user(uid)
    
    if ADMIN_ID == 0:
        ADMIN_ID = uid
        await save_user(uid, admin=1, approved=1)
        log("SYS", f"Главный админ: {ADMIN_ID}", Fore.YELLOW)
        p = await get_user(uid)

    is_admin = p and p['is_admin'] == 1
    is_approved = p and p['is_approved'] == 1

    if not is_admin and not is_approved:
        await e.reply(f"⛔ **Доступ закрыт.**\n\nВаш ID: `{uid}`\nПередайте его администратору.", parse_mode='md')
        return

    user_steps[uid] = "IDLE"
    status = "✅ Подключен" if p and p['session'] else "❌ Нет сессии"
    templates_count = len(p['templates']) if p else 0
    
    text = f"⚡ **TITAN v9.1 Enterprise**\n\n"
    text += f"🆔 ID: `{uid}`\n"
    text += f"📱 Аккаунт: {status}\n"
    text += f"📝 Шаблонов: {templates_count}\n\n"
    text += f"[🌐 Открыть веб-панель](https://bot-1-dk7y.onrender.com/login?id={uid})\n\n"
    text += f"Выбери действие:"
    
    await e.reply(text, buttons=main_menu(uid, is_admin), parse_mode='md')

@bot.on(events.CallbackQuery)
async def cb_handler(e):
    uid = e.sender_id
    action = e.data.decode('utf-8')
    p = await get_user(uid) or {}
    
    is_admin = p.get('is_admin') == 1
    is_approved = p.get('is_approved') == 1

    if not is_admin and not is_approved:
        return await e.answer("⛔ Доступ заблокирован!", alert=True)

    if action == "nav_account":
        if not p.get('session'):
            user_steps[uid] = "ST_PHONE"
            await e.edit("📱 Введите номер телефона (с +):", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
        else:
            await e.edit("✅ Ваш аккаунт авторизован.", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
    elif action == "nav_templates":
        tpls = p.get('templates', [])
        txt = "\n".join([f"• {t}" for t in tpls[:15]])
        if not txt: txt = "Список пуст."
        await e.edit(f"📝 Шаблоны:\n\n{txt}\n\nОтправьте текст (с новой строки).", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
    elif action == "nav_delay":
        await e.edit(f"⚙️ Задержка: {p.get('min_delay', 100)}-{p.get('max_delay', 300)} мс\nОтправьте новые (500-1000):", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
        user_steps[uid] = "ST_DELAY"
    elif action == "nav_spam":
        if not p.get('session'): return await e.answer("❌ Нет сессии!", alert=True)
        if not p.get('templates'): return await e.answer("❌ Нет шаблонов!", alert=True)
        user_steps[uid] = "ST_TARGET"
        await e.edit("🚀 Введите цель (@username или ID):")
    elif action == "nav_admin" and is_admin:
        await e.edit("👑 **Админка**\n\nВыдать доступ:\n`.approve <ID>`\n\nЗабрать:\n`.revoke <ID>`", buttons=[[Button.inline("🔙 Назад", b"nav_home")]])
    elif action == "nav_home":
        user_steps[uid] = "IDLE"
        await e.edit("👋 Главное меню", buttons=main_menu(uid, is_admin))

@bot.on(events.NewMessage)
async def text_handler(e):
    uid = e.sender_id
    text = e.text.strip()
    if text.startswith('/'): return
    
    p = await get_user(uid) or {}
    is_admin = p.get('is_admin') == 1
    is_approved = p.get('is_approved') == 1

    if is_admin:
        if text.startswith('.approve'):
            try:
                t_id = int(text.split(' ')[1])
                await save_user(t_id, approved=1)
                await e.reply(f"✅ Доступ выдан: `{t_id}`")
            except: await e.reply("❌ Формат: .approve 1234567")
            return
        elif text.startswith('.revoke'):
            try:
                t_id = int(text.split(' ')[1])
                await save_user(t_id, approved=0)
                await e.reply(f"🚫 Доступ забран: `{t_id}`")
            except: await e.reply("❌ Формат: .revoke 1234567")
            return

    if not is_admin and not is_approved: return

    step = user_steps.get(uid, "IDLE")

    if step == "ST_PHONE":
        await e.reply("⏳ Отправляю код...")
        c = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await c.connect()
            req = await c.send_code_request(text)
            active_clients[f"temp_{uid}"] = {'client': c, 'phone': text, 'hash': req.phone_code_hash}
            user_steps[uid] = "ST_CODE"
            await e.reply("📩 Введите код:")
        except Exception as ex:
            await e.reply(f"❌ Ошибка: {ex}")
            user_steps[uid] = "IDLE"
    elif step == "ST_CODE":
        b = active_clients.get(f"temp_{uid}")
        if not b: return
        try:
            await b['client'].sign_in(phone=b['phone'], code=text, phone_code_hash=b['hash'])
            await save_user(uid, session=b['client'].session.save())
            active_clients[uid] = b['client']
            del active_clients[f"temp_{uid}"]
            user_steps[uid] = "IDLE"
            await e.reply("✅ Сессия создана!")
        except SessionPasswordNeededError:
            user_steps[uid] = "ST_2FA"
            await e.reply("🔐 Пароль 2FA:")
        except Exception as ex:
            await e.reply(f"❌ Ошибка: {ex}")
            user_steps[uid] = "IDLE"
    elif step == "ST_2FA":
        b = active_clients.get(f"temp_{uid}")
        if not b: return
        try:
            await b['client'].sign_in(password=text)
            await save_user(uid, session=b['client'].session.save())
            active_clients[uid] = b['client']
            del active_clients[f"temp_{uid}"]
            user_steps[uid] = "IDLE"
            await e.reply("✅ 2FA пройдена!")
        except Exception as ex:
            await e.reply(f"❌ Ошибка: {ex}")
    elif step == "ST_TARGET":
        user_steps[uid] = "IDLE"
        await e.reply(f"🚀 Запуск по {text}...")
        asyncio.create_task(spam_engine(uid, text, p['templates'], p['min_delay'], p['max_delay']))
    elif step == "ST_DELAY":
        try:
            low, high = map(int, text.split('-'))
            await save_user(uid, min_d=low, max_d=high)
            await e.reply(f"✅ Задержка: {low}-{high} мс")
        except: await e.reply("❌ Формат: 500-1000")
        user_steps[uid] = "IDLE"
    elif step == "IDLE":
        new_lines = [line.strip() for line in text.split('\n') if line.strip()]
        current_templates = p.get('templates', [])
        added = 0
        for line in new_lines:
            if line not in current_templates:
                current_templates.append(line)
                added += 1
        if added > 0:
            await save_user(uid, templates=current_templates)
            await e.reply(f"📝 Добавлено: {added}. Всего: {len(current_templates)}")

# =====================================================================
# 6. ВЕБ-САЙТ
# =====================================================================
app = Flask(__name__)

HTML_LOGIN = """
<!DOCTYPE html><html><head><meta charset="utf-8"><title>LOGIN</title>
<style>body{background:#0e1621;color:#fff;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;}
.box{background:#17212b;padding:40px;border-radius:10px;text-align:center;width:300px;}
input{width:100%;padding:10px;margin:10px 0;background:#24303f;border:none;color:#fff;border-radius:5px;}
button{width:100%;padding:10px;background:#5288c1;border:none;color:#fff;border-radius:5px;cursor:pointer;font-weight:bold;}</style></head>
<body><div class="box"><h2>TITAN CONTROL</h2>
<form action="/dashboard" method="get"><input type="number" name="id" placeholder="Telegram ID" required>
<button type="submit">Войти</button></form></div></body></html>
"""

HTML_DASHBOARD = """
<!DOCTYPE html><html><head><meta charset="utf-8"><title>PANEL</title>
<style>body{background:#0e1621;color:#fff;font-family:sans-serif;padding:20px;}
.container{max-width:800px;margin:auto;background:#17212b;padding:20px;border-radius:10px;}
h1{color:#5288c1;}.section{background:#24303f;padding:15px;margin-bottom:15px;border-radius:8px;}
input,textarea{width:100%;padding:8px;margin:5px 0;background:#1c2532;border:none;color:#fff;border-radius:5px;}
button{padding:10px 15px;background:#2ec4b6;border:none;color:#fff;border-radius:5px;cursor:pointer;font-weight:bold;margin-top:5px;}</style></head>
<body><div class="container"><h1>⚡ TITAN v9.1</h1><p>ID: {{ uid }}</p>
<div class="section"><h3>📱 Аккаунт</h3>
{% if session %}<p style="color:#2ec4b6;">✅ Активен</p>{% else %}<p style="color:#e63946;">❌ Не подключен (используйте бота)</p>{% endif %}</div>
<div class="section"><h3>📝 Шаблоны</h3>
<form action="/api/save_templates" method="POST"><input type="hidden" name="uid" value="{{ uid }}">
<textarea name="templates" rows="6">{{ templates }}</textarea>
<button type="submit">Сохранить</button></form></div>
<div class="section"><h3>🚀 Спам</h3>
<form action="/api/start_spam" method="POST"><input type="hidden" name="uid" value="{{ uid }}">
<input type="text" name="target" placeholder="@username или ID" required>
<button type="submit">Запустить</button></form></div>
<div class="section"><h3>⚙️ Задержка</h3>
<form action="/api/save_delay" method="POST"><input type="hidden" name="uid" value="{{ uid }}">
<input type="text" name="delay" value="{{ min_d }}-{{ max_d }}">
<button type="submit">Сохранить</button></form></div>
</div></body></html>
"""

@app.route('/')
def index(): return HTML_LOGIN

@app.route('/login')
def login_redirect():
    uid = request.args.get('id')
    return redirect(url_for('dashboard', id=uid))

@app.route('/dashboard')
def dashboard():
    uid = int(request.args.get('id', 0))
    loop = asyncio.new_event_loop()
    p = loop.run_until_complete(get_user(uid))
    loop.close()
    if not p: p = {'session': None, 'templates': [], 'min_delay': 100, 'max_delay': 300}
    tpl_str = "\n".join(p['templates'])
    return render_template_string(HTML_DASHBOARD, uid=uid, session=p['session'], templates=tpl_str, min_d=p['min_delay'], max_d=p['max_delay'])

@app.route('/api/save_templates', methods=['POST'])
def api_save_templates():
    uid = int(request.form['uid'])
    raw = request.form['templates']
    tpls = [line.strip() for line in raw.split('\n') if line.strip()]
    loop = asyncio.new_event_loop()
    loop.run_until_complete(save_user(uid, templates=tpls))
    loop.close()
    return redirect(url_for('dashboard', id=uid))

@app.route('/api/start_spam', methods=['POST'])
def api_start_spam():
    uid = int(request.form['uid'])
    target = request.form['target']
    loop = asyncio.new_event_loop()
    loop.run_until_complete(create_web_task(uid, "START_SPAM", {"target": target}))
    loop.close()
    return f"<h1>Задача отправлена!</h1><a href='/dashboard?id={uid}'>Назад</a>"

@app.route('/api/save_delay', methods=['POST'])
def api_save_delay():
    uid = int(request.form['uid'])
    delay_str = request.form['delay']
    try:
        low, high = map(int, delay_str.split('-'))
        loop = asyncio.new_event_loop()
        loop.run_until_complete(save_user(uid, min_d=low, max_d=high))
        loop.close()
    except: pass
    return redirect(url_for('dashboard', id=uid))

@app.route('/healthz')
def health(): return jsonify({"status": "OK"}), 200

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# =====================================================================
# 7. ЗАПУСК
# =====================================================================
async def main():
    await init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.create_task(task_worker())
    try:
        await bot.start(bot_token=BOT_TOKEN)
        log("SYS", "Бот запущен!", Fore.GREEN)
        await bot.run_until_disconnected()
    except AccessTokenInvalidError:
        log("CRITICAL", "Токен неверный, но сайт работает.", Fore.RED)
        while True: await asyncio.sleep(3600)

if __name__ == '__main__':
    asyncio.run(main())
