import asyncio
import threading
import json
import os
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    FloodWaitError, PhoneCodeExpiredError
)

app = Flask(__name__)
app.secret_key = 'autoreact-dashboard-2026'

# ── Config ─────────────────────────────────────────────────
API_ID         = 2040
API_HASH       = 'b18441a1ff607e10a989891a5462e627'
TARGET_CHAT_ID = int(os.environ.get('TARGET_CHAT_ID', '-1002199457550'))
REACTION       = os.environ.get('REACTION', '\U0001f64f')
DELAY          = float(os.environ.get('DELAY', '0.5'))
ACCOUNTS_FILE  = 'accounts.json'
PORT           = int(os.environ.get('PORT', 5000))

# ── Global State ───────────────────────────────────────────
running_bots   = {}   # phone -> {loop, thread, client, status, react_count}
pending_logins = {}   # phone -> {client, loop}


# ── Storage ────────────────────────────────────────────────
def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_accounts(data):
    with open(ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── Bot Runner ─────────────────────────────────────────────
async def run_bot(phone, session_string):
    client = TelegramClient(
        StringSession(session_string), API_ID, API_HASH,
        device_model='Desktop', system_version='Windows 10',
        app_version='5.3.1', lang_code='km', system_lang_code='en'
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            running_bots[phone]['status'] = 'auth_error'
            return

        running_bots[phone]['client'] = client
        running_bots[phone]['status'] = 'running'

        @client.on(events.NewMessage(chats=TARGET_CHAT_ID))
        async def react(event):
            msg = event.message
            if not msg or not msg.id:
                return
            await asyncio.sleep(DELAY)
            try:
                await client(SendReactionRequest(
                    peer=await event.get_input_chat(),
                    msg_id=msg.id,
                    reaction=[ReactionEmoji(emoticon=REACTION)]
                ))
                running_bots[phone]['react_count'] = running_bots[phone].get('react_count', 0) + 1
                s = await event.get_sender()
                name = getattr(s, 'first_name', str(event.sender_id))
                print(f"[{phone}] {REACTION} Msg#{msg.id} {name}")
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
            except Exception as ex:
                err = str(ex)
                if 'REACTION_INVALID' not in err and 'same' not in err.lower():
                    print(f"[{phone}] Err: {err}")

        print(f"[Bot] {phone} started")
        await client.run_until_disconnected()
    except Exception as e:
        print(f"[Bot] {phone} error: {e}")
    finally:
        if phone in running_bots:
            running_bots[phone]['status'] = 'stopped'
        print(f"[Bot] {phone} stopped")


def _start_bot_thread(phone, session_string):
    loop = asyncio.new_event_loop()
    def run():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_bot(phone, session_string))
        except Exception as e:
            print(f"[Thread] {phone}: {e}")
            if phone in running_bots:
                running_bots[phone]['status'] = 'error'

    thread = threading.Thread(target=run, daemon=True)
    running_bots[phone] = {
        'loop': loop, 'thread': thread,
        'client': None, 'status': 'starting', 'react_count': 0
    }
    thread.start()


def _stop_bot(phone):
    bot = running_bots.get(phone)
    if not bot:
        return
    client = bot.get('client')
    loop   = bot.get('loop')
    if client and loop and loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(
                client.disconnect(), loop
            ).result(timeout=5)
        except Exception:
            pass
    if phone in running_bots:
        running_bots[phone]['status'] = 'stopped'


# ── Web Routes ─────────────────────────────────────────────
@app.route('/')
def index():
    accounts = load_accounts()
    for phone, data in accounts.items():
        bot = running_bots.get(phone)
        data['status']       = bot['status'] if bot else 'stopped'
        data['react_count']  = bot.get('react_count', 0) if bot else 0
    total   = len(accounts)
    running = sum(1 for p in accounts if running_bots.get(p, {}).get('status') == 'running')
    return render_template('index.html',
                           accounts=accounts, total=total,
                           running=running, target=TARGET_CHAT_ID,
                           reaction=REACTION)


@app.route('/api/status')
def api_status():
    accounts = load_accounts()
    out = {}
    for phone, data in accounts.items():
        bot = running_bots.get(phone)
        out[phone] = {
            'name':        data.get('name', ''),
            'username':    data.get('username', ''),
            'status':      bot['status'] if bot else 'stopped',
            'react_count': bot.get('react_count', 0) if bot else 0,
        }
    return jsonify(out)


@app.route('/api/start/<path:phone>', methods=['POST'])
def api_start(phone):
    accounts = load_accounts()
    if phone not in accounts:
        return jsonify({'error': 'Account not found'}), 404
    bot = running_bots.get(phone)
    if bot and bot['status'] in ('running', 'starting'):
        return jsonify({'status': bot['status']})
    _start_bot_thread(phone, accounts[phone]['session_string'])
    return jsonify({'status': 'starting'})


@app.route('/api/stop/<path:phone>', methods=['POST'])
def api_stop(phone):
    _stop_bot(phone)
    return jsonify({'status': 'stopped'})


@app.route('/api/delete/<path:phone>', methods=['DELETE'])
def api_delete(phone):
    _stop_bot(phone)
    accounts = load_accounts()
    accounts.pop(phone, None)
    save_accounts(accounts)
    running_bots.pop(phone, None)
    return jsonify({'status': 'deleted'})


# ── Login Flow ─────────────────────────────────────────────
@app.route('/api/login/send-code', methods=['POST'])
def send_code():
    phone = request.json.get('phone', '').strip()
    if not phone:
        return jsonify({'error': 'Phone required'}), 400

    # Cleanup old pending
    old = pending_logins.pop(phone, None)
    if old:
        try:
            asyncio.run_coroutine_threadsafe(
                old['client'].disconnect(), old['loop']
            ).result(timeout=3)
            old['loop'].call_soon_threadsafe(old['loop'].stop)
        except Exception:
            pass

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    client = TelegramClient(
        StringSession(), API_ID, API_HASH,
        device_model='Desktop', system_version='Windows 10',
        app_version='5.3.1', lang_code='km', system_lang_code='en'
    )
    try:
        asyncio.run_coroutine_threadsafe(client.connect(), loop).result(timeout=10)
        asyncio.run_coroutine_threadsafe(client.send_code_request(phone), loop).result(timeout=15)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    pending_logins[phone] = {'client': client, 'loop': loop}
    return jsonify({'status': 'code_sent'})


@app.route('/api/login/verify-otp', methods=['POST'])
def verify_otp():
    data  = request.json
    phone = data.get('phone', '').strip()
    code  = data.get('code', '').strip()
    p     = pending_logins.get(phone)
    if not p:
        return jsonify({'error': 'Session expired. Resend code.'}), 400
    try:
        asyncio.run_coroutine_threadsafe(
            p['client'].sign_in(phone, code), p['loop']
        ).result(timeout=15)
    except SessionPasswordNeededError:
        return jsonify({'status': 'need_2fa'})
    except PhoneCodeInvalidError:
        return jsonify({'error': 'Invalid code'}), 400
    except PhoneCodeExpiredError:
        return jsonify({'error': 'Code expired. Resend.'}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return _finish_login(phone, p['client'], p['loop'])


@app.route('/api/login/verify-2fa', methods=['POST'])
def verify_2fa():
    data     = request.json
    phone    = data.get('phone', '').strip()
    password = data.get('password', '').strip()
    p        = pending_logins.get(phone)
    if not p:
        return jsonify({'error': 'Session expired'}), 400
    try:
        asyncio.run_coroutine_threadsafe(
            p['client'].sign_in(password=password), p['loop']
        ).result(timeout=15)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return _finish_login(phone, p['client'], p['loop'])


def _finish_login(phone, client, loop):
    try:
        me = asyncio.run_coroutine_threadsafe(client.get_me(), loop).result(timeout=10)
        ss = StringSession.save(client.session)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    name = f"{me.first_name or ''} {me.last_name or ''}".strip()
    accounts = load_accounts()
    accounts[phone] = {
        'phone':          phone,
        'name':           name,
        'username':       me.username or '',
        'user_id':        me.id,
        'session_string': ss,
        'added_at':       datetime.utcnow().isoformat()
    }
    save_accounts(accounts)

    pending_logins.pop(phone, None)
    try:
        loop.call_soon_threadsafe(loop.stop)
    except Exception:
        pass

    _start_bot_thread(phone, ss)
    return jsonify({'status': 'success', 'name': name,
                    'username': me.username or '', 'phone': phone})


@app.route('/health')
def health():
    return 'OK', 200


# ── Auto-Start ─────────────────────────────────────────────
def autostart():
    accounts = load_accounts()
    if not accounts:
        ss    = os.environ.get('SESSION_STRING', '')
        phone = os.environ.get('PHONE', '+85593687814')
        if ss:
            accounts[phone] = {
                'phone': phone, 'name': 'Default Account',
                'username': '', 'user_id': 0,
                'session_string': ss,
                'added_at': datetime.utcnow().isoformat()
            }
            save_accounts(accounts)
    for phone, data in accounts.items():
        ss = data.get('session_string')
        if ss:
            print(f"[AutoStart] {phone}")
            _start_bot_thread(phone, ss)


if __name__ == '__main__':
    autostart()
    app.run(host='0.0.0.0', port=PORT, debug=False)
