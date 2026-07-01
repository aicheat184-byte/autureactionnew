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
from telethon.utils import get_peer_id
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    FloodWaitError, PhoneCodeExpiredError
)

app = Flask(__name__)
app.secret_key = 'cheatz-autoreact-2026'

# ── Config ─────────────────────────────────────────────────
API_ID        = 2040
API_HASH      = 'b18441a1ff607e10a989891a5462e627'
DEFAULT_REACT = os.environ.get('REACTION', '\U0001f64f')
DELAY         = float(os.environ.get('DELAY', '0.5'))
ACCOUNTS_FILE = 'accounts.json'
TARGETS_FILE  = 'targets.json'   # format: {phone: {key: {id, name}}}
CONFIG_FILE   = 'config.json'
PORT          = int(os.environ.get('PORT', 5000))
CONTACT       = 'https://t.me/User_88881'

# ── Global State ───────────────────────────────────────────
running_bots   = {}
pending_logins = {}


# ── Storage helpers ────────────────────────────────────────
def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_accounts(data):
    with open(ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_targets():
    """Returns {phone: {key: {id, name}}}"""
    if os.path.exists(TARGETS_FILE):
        with open(TARGETS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_targets(data):
    with open(TARGETS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_acc_targets(phone):
    return load_targets().get(phone, {})

def set_acc_target(phone, key, target):
    t = load_targets()
    if phone not in t:
        t[phone] = {}
    t[phone][key] = target
    save_targets(t)

def del_acc_target_db(phone, key):
    t = load_targets()
    if phone in t:
        t[phone].pop(key, None)
    save_targets(t)

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    cfg = {'reaction': DEFAULT_REACT}
    save_config(cfg)
    return cfg

def save_config(data):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
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

        @client.on(events.NewMessage)
        async def react(event):
            # Per-account target check
            acc_targets = get_acc_targets(phone)
            if not acc_targets:
                return  # No targets set — don't react
            target_ids = {t['id'] for t in acc_targets.values()}
            if event.chat_id not in target_ids:
                return

            msg = event.message
            if not msg or not msg.id:
                return
            await asyncio.sleep(DELAY)
            try:
                cur_reaction = load_config().get('reaction', DEFAULT_REACT)
                await client(SendReactionRequest(
                    peer=await event.get_input_chat(),
                    msg_id=msg.id,
                    reaction=[ReactionEmoji(emoticon=cur_reaction)]
                ))
                running_bots[phone]['react_count'] = running_bots[phone].get('react_count', 0) + 1
                s = await event.get_sender()
                name = getattr(s, 'first_name', str(event.sender_id))
                print(f"[{phone}] {cur_reaction} Msg#{msg.id} {name}")
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
    running_bots[phone] = {'loop': loop, 'thread': thread,
                           'client': None, 'status': 'starting', 'react_count': 0}
    thread.start()


def _stop_bot(phone):
    bot = running_bots.get(phone)
    if not bot:
        return
    client, loop = bot.get('client'), bot.get('loop')
    if client and loop and loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(client.disconnect(), loop).result(timeout=5)
        except Exception:
            pass
    if phone in running_bots:
        running_bots[phone]['status'] = 'stopped'


# ── Web Routes ─────────────────────────────────────────────
@app.route('/')
def index():
    accounts = load_accounts()
    all_targets = load_targets()
    config  = load_config()
    for phone, data in accounts.items():
        bot = running_bots.get(phone)
        data['status']      = bot['status'] if bot else 'stopped'
        data['react_count'] = bot.get('react_count', 0) if bot else 0
        data['targets']     = all_targets.get(phone, {})
    total   = len(accounts)
    running = sum(1 for p in accounts if running_bots.get(p, {}).get('status') == 'running')
    return render_template('index.html', accounts=accounts,
                           config=config, total=total, running=running,
                           reaction=config.get('reaction', DEFAULT_REACT),
                           contact=CONTACT)


@app.route('/api/status')
def api_status():
    accounts   = load_accounts()
    all_targets = load_targets()
    out = {}
    for phone, data in accounts.items():
        bot = running_bots.get(phone)
        out[phone] = {
            'name':        data.get('name', ''),
            'username':    data.get('username', ''),
            'status':      bot['status'] if bot else 'stopped',
            'react_count': bot.get('react_count', 0) if bot else 0,
            'target_count': len(all_targets.get(phone, {})),
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
    # remove targets for this account
    t = load_targets()
    t.pop(phone, None)
    save_targets(t)
    return jsonify({'status': 'deleted'})


# ── Per-Account Target Routes ──────────────────────────────
@app.route('/api/accounts/<path:phone>/targets', methods=['GET'])
def get_targets_api(phone):
    return jsonify(get_acc_targets(phone))


@app.route('/api/accounts/<path:phone>/targets', methods=['POST'])
def add_target_api(phone):
    data      = request.json
    input_val = data.get('input', '').strip()
    name      = data.get('name', '').strip()

    if not input_val:
        return jsonify({'error': 'Chat ID or link required'}), 400

    # ── Direct numeric Chat ID ────────────────────────────
    clean = input_val.lstrip('-')
    if clean.isdigit():
        chat_id     = int(input_val)
        target_name = name or f'Group {chat_id}'
        key         = str(chat_id)
        set_acc_target(phone, key, {'id': chat_id, 'name': target_name})
        return jsonify({'status': 'added', 'id': chat_id, 'name': target_name, 'key': key})

    # ── Resolve link / @username using running bot ─────────
    bot = running_bots.get(phone)
    if not bot or not bot.get('client'):
        # Try any running bot to resolve
        for p, b in running_bots.items():
            if b.get('client') and b.get('status') == 'running':
                bot = b
                break
    if not bot or not bot.get('client'):
        return jsonify({'error': 'Start the bot first to resolve links/usernames'}), 400

    client = bot['client']
    loop   = bot['loop']

    try:
        entity = asyncio.run_coroutine_threadsafe(
            client.get_entity(input_val), loop
        ).result(timeout=15)

        chat_id     = get_peer_id(entity)
        entity_name = getattr(entity, 'title', None) or getattr(entity, 'username', str(chat_id))
        target_name = name or entity_name
        key         = str(chat_id)
        set_acc_target(phone, key, {'id': chat_id, 'name': target_name})
        return jsonify({'status': 'added', 'id': chat_id, 'name': target_name, 'key': key})

    except Exception as e:
        return jsonify({'error': f'Cannot resolve: {e}'}), 400


@app.route('/api/accounts/<path:phone>/targets/<key>', methods=['DELETE'])
def remove_target_api(phone, key):
    del_acc_target_db(phone, key)
    return jsonify({'status': 'deleted'})


# ── Config Routes ──────────────────────────────────────────
@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(load_config())

@app.route('/api/config', methods=['POST'])
def set_config():
    data   = request.json
    config = load_config()
    if 'reaction' in data:
        config['reaction'] = data['reaction']
    save_config(config)
    return jsonify({'status': 'saved', **config})


# ── Login Flow ─────────────────────────────────────────────
@app.route('/api/login/send-code', methods=['POST'])
def send_code():
    phone = request.json.get('phone', '').strip()
    if not phone:
        return jsonify({'error': 'Phone required'}), 400
    old = pending_logins.pop(phone, None)
    if old:
        try:
            asyncio.run_coroutine_threadsafe(old['client'].disconnect(), old['loop']).result(timeout=3)
            old['loop'].call_soon_threadsafe(old['loop'].stop)
        except Exception:
            pass
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    client = TelegramClient(StringSession(), API_ID, API_HASH,
                            device_model='Desktop', system_version='Windows 10',
                            app_version='5.3.1', lang_code='km', system_lang_code='en')
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
        asyncio.run_coroutine_threadsafe(p['client'].sign_in(phone, code), p['loop']).result(timeout=15)
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
        asyncio.run_coroutine_threadsafe(p['client'].sign_in(password=password), p['loop']).result(timeout=15)
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
    accounts[phone] = {'phone': phone, 'name': name, 'username': me.username or '',
                       'user_id': me.id, 'session_string': ss,
                       'added_at': datetime.utcnow().isoformat()}
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
            accounts[phone] = {'phone': phone, 'name': 'Default Account',
                               'username': '', 'user_id': 0,
                               'session_string': ss,
                               'added_at': datetime.utcnow().isoformat()}
            save_accounts(accounts)
            # Default target
            default_target = int(os.environ.get('TARGET_CHAT_ID', '-1002199457550'))
            set_acc_target(phone, str(default_target),
                           {'id': default_target, 'name': 'Default Group'})
    load_config()
    for phone, data in accounts.items():
        ss = data.get('session_string')
        if ss:
            print(f"[AutoStart] {phone}")
            _start_bot_thread(phone, ss)


if __name__ == '__main__':
    autostart()
    app.run(host='0.0.0.0', port=PORT, debug=False)
