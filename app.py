import asyncio
import threading
import json
import os
import csv
import io
from datetime import datetime, time as dtime
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, Response
from werkzeug.security import generate_password_hash, check_password_hash
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
app.secret_key = os.environ.get('SECRET_KEY', 'cheatz-autoreact-secure-2026')

# ── Config ─────────────────────────────────────────────────
API_ID        = 2040
API_HASH      = 'b18441a1ff607e10a989891a5462e627'
DEFAULT_REACT = os.environ.get('REACTION', '\U0001f64f')
DEFAULT_EMOJI_LIST = ['\U0001f64f', '\u2764\ufe0f', '\U0001f525', '\U0001f44d', '\U0001f389']
DELAY         = float(os.environ.get('DELAY', '0.5'))
ACCOUNTS_FILE = 'accounts.json'
TARGETS_FILE  = 'targets.json'
CONFIG_FILE   = 'config.json'
USERS_FILE    = 'users.json'
PORT          = int(os.environ.get('PORT', 5000))
CONTACT       = 'https://t.me/User_88881'
REGISTER_CODE = os.environ.get('REGISTER_CODE', '')  # empty = open registration

# ── Global State ───────────────────────────────────────────
running_bots   = {}
pending_logins = {}
activity_log   = []     # [{phone, emoji, msg_id, chat_id, sender, ts}, …]
MAX_LOG        = 500

# ── Emoji rotation counters per phone ─────────────────────
_emoji_idx = {}


# ── Storage ────────────────────────────────────────────────
def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_accounts(data):
    with open(ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_targets():
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
    t.setdefault(phone, {})[key] = target
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
    cfg = {'reaction': DEFAULT_REACT, 'emoji_list': DEFAULT_EMOJI_LIST}
    save_config(cfg)
    return cfg

def save_config(data):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── Schedule Helpers ───────────────────────────────────────
def _in_schedule(acc_data):
    """Return True if current UTC time is within the account's reaction schedule."""
    sched = acc_data.get('schedule')
    if not sched or not sched.get('enabled'):
        return True  # no schedule → always active
    try:
        now = datetime.utcnow()
        # Day check (0=Mon … 6=Sun), stored as list of ints
        days = sched.get('days', list(range(7)))
        if now.weekday() not in days:
            return False
        # Time range check
        t_from = dtime.fromisoformat(sched.get('from', '00:00'))
        t_to   = dtime.fromisoformat(sched.get('to',   '23:59'))
        cur    = now.time().replace(second=0, microsecond=0)
        if t_from <= t_to:
            return t_from <= cur <= t_to
        else:  # overnight range e.g. 22:00–06:00
            return cur >= t_from or cur <= t_to
    except Exception:
        return True


def _pick_emoji(phone, acc_data, chat_id=None, target_key=None):
    """Pick the next emoji for this phone — supports rotation & per-target override."""
    # Per-target emoji override
    if target_key:
        targets = get_acc_targets(phone)
        tgt = targets.get(target_key, {})
        if tgt.get('emoji'):
            return tgt['emoji']
    # Per-account emoji list (rotation)
    emoji_list = acc_data.get('emoji_list') or []
    if emoji_list and len(emoji_list) > 1:
        idx = _emoji_idx.get(phone, 0)
        emoji = emoji_list[idx % len(emoji_list)]
        _emoji_idx[phone] = (idx + 1) % len(emoji_list)
        return emoji
    # Single per-account emoji
    single = acc_data.get('reaction')
    if single:
        return single
    # Global config
    cfg = load_config()
    cfg_list = cfg.get('emoji_list') or []
    if cfg_list and len(cfg_list) > 1:
        idx = _emoji_idx.get('__global__', 0)
        emoji = cfg_list[idx % len(cfg_list)]
        _emoji_idx['__global__'] = (idx + 1) % len(cfg_list)
        return emoji
    return cfg.get('reaction', DEFAULT_REACT)

# ── User Management ────────────────────────────────────────
def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    admin_user = os.environ.get('ADMIN_USERNAME', 'admin')
    admin_pass = os.environ.get('ADMIN_PASSWORD', 'cheatz2026')
    users = {
        admin_user: {
            'password': generate_password_hash(admin_pass),
            'role': 'admin',
            'phone': None,
            'display_name': 'Administrator'
        }
    }
    save_users(users)
    return users

def save_users(data):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── Auth Decorators ────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized', 'login': True}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'admin':
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': 'Admin required'}), 403
            return redirect('/')
        return f(*args, **kwargs)
    return decorated

def get_visible_accounts():
    """Accounts visible to current session user."""
    all_acc = load_accounts()
    if session.get('role') == 'admin':
        return all_acc
    user_phone = session.get('phone')
    if user_phone and user_phone in all_acc:
        return {user_phone: all_acc[user_phone]}
    return {}


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
        _reacted = set()

        @client.on(events.NewMessage)
        async def react(event):
            acc_targets = get_acc_targets(phone)
            if not acc_targets:
                return
            # Match target and find its key
            target_key  = None
            target_ids  = {}
            for k, t in acc_targets.items():
                target_ids[t['id']] = k
            if event.chat_id not in target_ids:
                return
            target_key = target_ids[event.chat_id]
            msg = event.message
            if not msg or not msg.id:
                return
            if hasattr(msg, 'action') and msg.action:
                return
            key = (event.chat_id, msg.id)
            if key in _reacted:
                return
            _reacted.add(key)
            if len(_reacted) > 2000:
                _reacted.clear()

            # ── Schedule check ────────────────────────────
            acc_data = load_accounts().get(phone, {})
            if not _in_schedule(acc_data):
                return

            await asyncio.sleep(DELAY)
            try:
                # Emoji selection: per-target override → rotation → global
                cur_reaction = _pick_emoji(phone, acc_data, event.chat_id, target_key)
                await client(SendReactionRequest(
                    peer=await event.get_input_chat(),
                    msg_id=msg.id,
                    reaction=[ReactionEmoji(emoticon=cur_reaction)]
                ))
                running_bots[phone]['react_count'] = \
                    running_bots[phone].get('react_count', 0) + 1
                sender = getattr(event, 'sender_id', 'ch')
                # Log activity
                activity_log.append({
                    'phone': phone,
                    'emoji': cur_reaction,
                    'msg_id': msg.id,
                    'chat_id': event.chat_id,
                    'sender': str(sender),
                    'ts': datetime.utcnow().isoformat() + 'Z'
                })
                if len(activity_log) > MAX_LOG:
                    activity_log.pop(0)
                print(f"[{phone}] {cur_reaction} #{msg.id} from={sender}")
            except FloodWaitError as e:
                print(f"[{phone}] FloodWait {e.seconds}s")
                await asyncio.sleep(e.seconds)
            except Exception as ex:
                err = str(ex)
                if 'REACTION_INVALID' not in err and 'same' not in err.lower():
                    print(f"[{phone}] Err: {err}")

        print(f"[Bot] {phone} started — {len(get_acc_targets(phone))} target(s)")
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


# ══════════════════════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════════════════════
@app.route('/login', methods=['GET'])
def login_page():
    if 'username' in session:
        return redirect('/')
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def do_login():
    data     = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    users = load_users()
    user  = users.get(username)
    if not user or not check_password_hash(user['password'], password):
        return jsonify({'error': 'Invalid username or password'}), 401
    session.permanent = True
    session['username']     = username
    session['role']         = user['role']
    session['phone']        = user.get('phone')
    session['display_name'] = user.get('display_name', username)
    return jsonify({'status': 'ok', 'role': user['role']})

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


# ── Register Routes ──────────────────────────────────────
@app.route('/register', methods=['GET'])
def register_page():
    if 'username' in session:
        return redirect('/')
    return render_template('register.html', require_code=bool(REGISTER_CODE))

@app.route('/api/register', methods=['POST'])
def do_register():
    data         = request.json or {}
    username     = data.get('username', '').strip()
    password     = data.get('password', '').strip()
    confirm      = data.get('confirm', '').strip()
    display_name = data.get('display_name', '').strip() or username
    code         = data.get('code', '').strip()

    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    if len(username) < 3:
        return jsonify({'error': 'Username must be at least 3 characters'}), 400
    if not username.replace('_','').replace('-','').isalnum():
        return jsonify({'error': 'Username: letters, numbers, _ and - only'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    if password != confirm:
        return jsonify({'error': 'Passwords do not match'}), 400
    if REGISTER_CODE and code != REGISTER_CODE:
        return jsonify({'error': 'Invalid registration code'}), 400

    users = load_users()
    if username in users:
        return jsonify({'error': 'Username already taken'}), 409

    users[username] = {
        'password':     generate_password_hash(password),
        'role':         'user',
        'phone':        None,
        'display_name': display_name
    }
    save_users(users)
    print(f"[Register] New user: {username}")
    return jsonify({'status': 'created', 'username': username})


# ══════════════════════════════════════════════════════════
# MAIN ROUTES
# ══════════════════════════════════════════════════════════
@app.route('/')
@login_required
def index():
    accounts    = get_visible_accounts()
    all_targets = load_targets()
    config      = load_config()
    users       = load_users()
    for phone, data in accounts.items():
        bot = running_bots.get(phone)
        data['status']      = bot['status'] if bot else 'stopped'
        data['react_count'] = bot.get('react_count', 0) if bot else 0
        data['targets']     = all_targets.get(phone, {})
    total   = len(accounts)
    running = sum(1 for p in accounts if running_bots.get(p, {}).get('status') == 'running')
    # Per-role template
    my_phone   = session.get('phone')
    my_account = accounts.get(my_phone) if my_phone else None
    template   = 'admin.html' if session.get('role') == 'admin' else 'user.html'
    # Per-account emoji for user, global for admin
    my_reaction = (my_account.get('reaction') if my_account else None) or config.get('reaction', DEFAULT_REACT)
    return render_template(template,
                           accounts=accounts,
                           my_account=my_account,
                           my_phone=my_phone,
                           config=config,
                           total=total, running=running,
                           reaction=my_reaction if template == 'user.html' else config.get('reaction', DEFAULT_REACT),
                           contact=CONTACT,
                           current_user=session.get('username'),
                           current_role=session.get('role'),
                           display_name=session.get('display_name', session.get('username')),
                           web_users_count=len(users))


@app.route('/api/accounts')
@login_required
def api_accounts_list():
    """Return visible accounts with live status merged — used by admin dashboard JS."""
    accounts    = get_visible_accounts()
    all_targets = load_targets()
    out = {}
    for phone, data in accounts.items():
        bot = running_bots.get(phone)
        out[phone] = {
            'name':         data.get('name', ''),
            'username':     data.get('username', ''),
            'phone':        phone,
            'status':       bot['status'] if bot else 'stopped',
            'react_count':  bot.get('react_count', 0) if bot else 0,
            'targets':      all_targets.get(phone, {}),
            'schedule':     data.get('schedule'),
            'emoji_list':   data.get('emoji_list', []),
            'reaction':     data.get('reaction', ''),
        }
    return jsonify(out)


@app.route('/api/status')
@login_required
def api_status():
    accounts    = get_visible_accounts()
    all_targets = load_targets()
    out = {}
    for phone, data in accounts.items():
        bot = running_bots.get(phone)
        out[phone] = {
            'name':         data.get('name', ''),
            'username':     data.get('username', ''),
            'status':       bot['status'] if bot else 'stopped',
            'react_count':  bot.get('react_count', 0) if bot else 0,
            'target_count': len(all_targets.get(phone, {})),
        }
    return jsonify(out)


@app.route('/api/activity')
@login_required
def api_activity():
    """Recent reaction activity log."""
    limit = min(int(request.args.get('limit', 50)), MAX_LOG)
    visible = get_visible_accounts()
    logs = [e for e in reversed(activity_log) if e['phone'] in visible][:limit]
    # Enrich with account name
    accounts = load_accounts()
    targets  = load_targets()
    for entry in logs:
        acc = accounts.get(entry['phone'], {})
        entry['acc_name'] = acc.get('name', entry['phone'])
        # Try to resolve target name
        phone_targets = targets.get(entry['phone'], {})
        chat_key = str(entry['chat_id'])
        tgt = phone_targets.get(chat_key, {})
        entry['target_name'] = tgt.get('name', f"Chat {entry['chat_id']}")
    return jsonify(logs)


@app.route('/api/stats')
@login_required
def api_stats():
    """Aggregate stats for charts."""
    visible = get_visible_accounts()
    all_targets = load_targets()
    # Per-account stats
    per_acc = {}
    total_reacted = 0
    for phone in visible:
        bot = running_bots.get(phone)
        rc  = bot.get('react_count', 0) if bot else 0
        total_reacted += rc
        acc = visible[phone]
        per_acc[phone] = {
            'name':         acc.get('name', phone),
            'react_count':  rc,
            'status':       bot['status'] if bot else 'stopped',
            'target_count': len(all_targets.get(phone, {})),
        }
    # Timeline: group activity_log by minute (last 60 min)
    from collections import defaultdict
    timeline = defaultdict(int)
    for entry in activity_log:
        if entry['phone'] in visible:
            minute_key = entry['ts'][:16]  # YYYY-MM-DDTHH:MM
            timeline[minute_key] += 1
    timeline_sorted = sorted(timeline.items())[-60:]  # last 60 data points
    return jsonify({
        'total_accounts':  len(visible),
        'total_running':   sum(1 for p in visible if running_bots.get(p, {}).get('status') == 'running'),
        'total_reacted':   total_reacted,
        'total_targets':   sum(len(all_targets.get(p, {})) for p in visible),
        'per_account':     per_acc,
        'timeline':        timeline_sorted,
    })


@app.route('/api/start/<path:phone>', methods=['POST'])
@login_required
def api_start(phone):
    accounts = get_visible_accounts()
    if phone not in accounts:
        return jsonify({'error': 'Not found or no permission'}), 403
    bot = running_bots.get(phone)
    if bot and bot['status'] in ('running', 'starting'):
        return jsonify({'status': bot['status']})
    _start_bot_thread(phone, load_accounts()[phone]['session_string'])
    return jsonify({'status': 'starting'})


@app.route('/api/stop/<path:phone>', methods=['POST'])
@login_required
def api_stop(phone):
    if phone not in get_visible_accounts():
        return jsonify({'error': 'No permission'}), 403
    _stop_bot(phone)
    return jsonify({'status': 'stopped'})


@app.route('/api/delete/<path:phone>', methods=['DELETE'])
@login_required
@admin_required
def api_delete(phone):
    _stop_bot(phone)
    accounts = load_accounts()
    accounts.pop(phone, None)
    save_accounts(accounts)
    running_bots.pop(phone, None)
    t = load_targets()
    t.pop(phone, None)
    save_targets(t)
    return jsonify({'status': 'deleted'})


# ── Per-Account Target Routes ──────────────────────────────
@app.route('/api/accounts/<path:phone>/targets', methods=['GET'])
@login_required
def get_targets_api(phone):
    if phone not in get_visible_accounts():
        return jsonify({'error': 'No permission'}), 403
    return jsonify(get_acc_targets(phone))


@app.route('/api/accounts/<path:phone>/targets', methods=['POST'])
@login_required
def add_target_api(phone):
    if phone not in get_visible_accounts():
        return jsonify({'error': 'No permission'}), 403
    data      = request.json
    input_val = data.get('input', '').strip()
    name      = data.get('name', '').strip()
    if not input_val:
        return jsonify({'error': 'Chat ID or link required'}), 400
    clean = input_val.lstrip('-')
    if clean.isdigit():
        chat_id     = int(input_val)
        target_name = name or f'Group {chat_id}'
        key         = str(chat_id)
        set_acc_target(phone, key, {'id': chat_id, 'name': target_name})
        return jsonify({'status': 'added', 'id': chat_id, 'name': target_name, 'key': key})
    # Resolve via running bot
    bot = running_bots.get(phone) or next(
        (b for b in running_bots.values() if b.get('client') and b.get('status') == 'running'), None)
    if not bot or not bot.get('client'):
        return jsonify({'error': 'Start the bot first to resolve links'}), 400
    try:
        entity      = asyncio.run_coroutine_threadsafe(
            bot['client'].get_entity(input_val), bot['loop']).result(timeout=15)
        chat_id     = get_peer_id(entity)
        entity_name = getattr(entity, 'title', None) or getattr(entity, 'username', str(chat_id))
        target_name = name or entity_name
        key         = str(chat_id)
        set_acc_target(phone, key, {'id': chat_id, 'name': target_name})
        return jsonify({'status': 'added', 'id': chat_id, 'name': target_name, 'key': key})
    except Exception as e:
        return jsonify({'error': f'Cannot resolve: {e}'}), 400


@app.route('/api/accounts/<path:phone>/targets/<key>', methods=['DELETE'])
@login_required
def remove_target_api(phone, key):
    if phone not in get_visible_accounts():
        return jsonify({'error': 'No permission'}), 403
    del_acc_target_db(phone, key)
    return jsonify({'status': 'deleted'})


# ── Config Routes ──────────────────────────────────────────
@app.route('/api/config', methods=['GET'])
@login_required
def get_config():
    return jsonify(load_config())

@app.route('/api/config', methods=['POST'])
@login_required
@admin_required
def set_config():
    data   = request.json
    config = load_config()
    if 'reaction' in data:
        config['reaction'] = data['reaction']
    if 'emoji_list' in data:
        elist = data['emoji_list']
        if isinstance(elist, list):
            config['emoji_list'] = [e.strip() for e in elist if e.strip()]
    save_config(config)
    return jsonify({'status': 'saved', **config})


# ── Reset Reaction Count ───────────────────────────────────
@app.route('/api/accounts/<path:phone>/reset-count', methods=['POST'])
@login_required
def reset_count(phone):
    if phone not in get_visible_accounts():
        return jsonify({'error': 'No permission'}), 403
    bot = running_bots.get(phone)
    if bot:
        bot['react_count'] = 0
    return jsonify({'status': 'reset', 'phone': phone})


# ── Bot Health Ping ────────────────────────────────────────
@app.route('/api/ping/<path:phone>')
@login_required
def ping_bot(phone):
    if phone not in get_visible_accounts():
        return jsonify({'error': 'No permission'}), 403
    bot = running_bots.get(phone)
    if not bot or not bot.get('client') or not bot.get('loop'):
        return jsonify({'phone': phone, 'connected': False, 'status': 'no_bot'})
    try:
        connected = asyncio.run_coroutine_threadsafe(
            bot['client'].is_connected(), bot['loop']
        ).result(timeout=5)
        return jsonify({'phone': phone, 'connected': connected, 'status': bot.get('status')})
    except Exception as e:
        return jsonify({'phone': phone, 'connected': False, 'error': str(e)})


# ── Activity Export (CSV) ──────────────────────────────────
@app.route('/api/activity/export')
@login_required
def export_activity():
    visible  = get_visible_accounts()
    accounts = load_accounts()
    targets  = load_targets()
    logs     = [e for e in reversed(activity_log) if e['phone'] in visible]
    output   = io.StringIO()
    writer   = csv.DictWriter(output, fieldnames=[
        'ts', 'phone', 'acc_name', 'emoji', 'msg_id', 'chat_id', 'target_name', 'sender'
    ])
    writer.writeheader()
    for entry in logs:
        acc  = accounts.get(entry['phone'], {})
        ptgt = targets.get(entry['phone'], {})
        tgt  = ptgt.get(str(entry['chat_id']), {})
        writer.writerow({
            'ts':          entry['ts'],
            'phone':       entry['phone'],
            'acc_name':    acc.get('name', entry['phone']),
            'emoji':       entry['emoji'],
            'msg_id':      entry['msg_id'],
            'chat_id':     entry['chat_id'],
            'target_name': tgt.get('name', f"Chat {entry['chat_id']}"),
            'sender':      entry['sender'],
        })
    csv_bytes = output.getvalue().encode('utf-8-sig')
    return Response(
        csv_bytes,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=autoreact_activity.csv'}
    )


# ── Per-Account Schedule ───────────────────────────────────
@app.route('/api/accounts/<path:phone>/schedule', methods=['POST'])
@login_required
def set_account_schedule(phone):
    if phone not in get_visible_accounts():
        return jsonify({'error': 'No permission'}), 403
    data = request.json or {}
    accounts = load_accounts()
    if phone not in accounts:
        return jsonify({'error': 'Account not found'}), 404
    accounts[phone]['schedule'] = {
        'enabled': bool(data.get('enabled', False)),
        'from':    data.get('from', '00:00'),
        'to':      data.get('to', '23:59'),
        'days':    data.get('days', list(range(7)))
    }
    save_accounts(accounts)
    return jsonify({'status': 'saved', 'schedule': accounts[phone]['schedule']})


# ── Per-Account Emoji List (Rotation) ─────────────────────
@app.route('/api/accounts/<path:phone>/emoji-list', methods=['POST'])
@login_required
def set_account_emoji_list(phone):
    if phone not in get_visible_accounts():
        return jsonify({'error': 'No permission'}), 403
    data = request.json or {}
    elist = data.get('emoji_list', [])
    if not isinstance(elist, list):
        return jsonify({'error': 'emoji_list must be array'}), 400
    elist = [e.strip() for e in elist if str(e).strip()]
    accounts = load_accounts()
    if phone not in accounts:
        return jsonify({'error': 'Account not found'}), 404
    accounts[phone]['emoji_list'] = elist
    # Reset rotation index
    _emoji_idx.pop(phone, None)
    save_accounts(accounts)
    return jsonify({'status': 'saved', 'emoji_list': elist})


# ── Per-Target Emoji Override ──────────────────────────────
@app.route('/api/accounts/<path:phone>/targets/<key>/emoji', methods=['POST'])
@login_required
def set_target_emoji(phone, key):
    if phone not in get_visible_accounts():
        return jsonify({'error': 'No permission'}), 403
    data  = request.json or {}
    emoji = data.get('emoji', '').strip()
    t     = load_targets()
    if phone not in t or key not in t[phone]:
        return jsonify({'error': 'Target not found'}), 404
    if emoji:
        t[phone][key]['emoji'] = emoji
    else:
        t[phone][key].pop('emoji', None)  # remove override
    save_targets(t)
    return jsonify({'status': 'saved', 'emoji': emoji or None})


# ── Per-Account Emoji ──────────────────────────────────────
@app.route('/api/accounts/<path:phone>/emoji', methods=['POST'])
@login_required
def set_account_emoji(phone):
    if phone not in get_visible_accounts():
        return jsonify({'error': 'No permission'}), 403
    data = request.json or {}
    emoji = data.get('reaction', '').strip()
    if not emoji:
        return jsonify({'error': 'Emoji required'}), 400
    accounts = load_accounts()
    if phone not in accounts:
        return jsonify({'error': 'Account not found'}), 404
    accounts[phone]['reaction'] = emoji
    save_accounts(accounts)
    return jsonify({'status': 'saved', 'reaction': emoji})


# ── User Management (Admin Only) ───────────────────────────
@app.route('/api/users', methods=['GET'])
@login_required
@admin_required
def get_users():
    users = load_users()
    return jsonify({u: {k: v for k, v in d.items() if k != 'password'}
                    for u, d in users.items()})

@app.route('/api/users', methods=['POST'])
@login_required
@admin_required
def create_user():
    data     = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    role     = data.get('role', 'user')
    phone    = data.get('phone', None) or None
    display  = data.get('display_name', username)
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    if role not in ('admin', 'user'):
        return jsonify({'error': 'Role must be admin or user'}), 400
    users = load_users()
    if username in users:
        return jsonify({'error': 'Username already exists'}), 409
    users[username] = {
        'password':     generate_password_hash(password),
        'role':         role,
        'phone':        phone,
        'display_name': display
    }
    save_users(users)
    return jsonify({'status': 'created', 'username': username, 'role': role})

@app.route('/api/users/<username>', methods=['DELETE'])
@login_required
@admin_required
def delete_user(username):
    if username == session.get('username'):
        return jsonify({'error': 'Cannot delete yourself'}), 400
    users = load_users()
    users.pop(username, None)
    save_users(users)
    return jsonify({'status': 'deleted'})

@app.route('/api/users/<username>/password', methods=['POST'])
@login_required
@admin_required
def change_password(username):
    new_pass = request.json.get('password', '').strip()
    if not new_pass:
        return jsonify({'error': 'Password required'}), 400
    users = load_users()
    if username not in users:
        return jsonify({'error': 'User not found'}), 404
    users[username]['password'] = generate_password_hash(new_pass)
    save_users(users)
    return jsonify({'status': 'updated'})


# ── Login Flow (any logged-in user can connect Telegram) ───
@app.route('/api/login/send-code', methods=['POST'])
@login_required
def send_code():
    phone = request.json.get('phone', '').strip()
    if not phone:
        return jsonify({'error': 'Phone required'}), 400
    # Non-admin users: check they don't already have a phone linked
    if session.get('role') != 'admin':
        existing_phone = session.get('phone')
        if existing_phone and existing_phone in load_accounts():
            return jsonify({'error': 'You already have a Telegram account connected'}), 400
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
@login_required
def verify_otp():
    data, phone, code = request.json, request.json.get('phone','').strip(), request.json.get('code','').strip()
    p = pending_logins.get(phone)
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
@login_required
def verify_2fa():
    data, phone, password = request.json, request.json.get('phone','').strip(), request.json.get('password','').strip()
    p = pending_logins.get(phone)
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
    # Auto-link phone to the current web user's account
    username = session.get('username')
    if username:
        users = load_users()
        if username in users:
            users[username]['phone'] = phone
            save_users(users)
            session['phone'] = phone
    _start_bot_thread(phone, ss)
    return jsonify({'status': 'success', 'name': name,
                    'username': me.username or '', 'phone': phone})


@app.route('/health')
def health():
    return 'OK', 200


# ── Auto-Start ─────────────────────────────────────────────
def autostart():
    load_users()  # init users file
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
            default_target = int(os.environ.get('TARGET_CHAT_ID', '-1002199457550'))
            set_acc_target(phone, str(default_target),
                           {'id': default_target, 'name': 'Default Group'})
    load_config()
    for phone, data in accounts.items():
        ss = data.get('session_string')
        if ss:
            print(f"[AutoStart] {phone}")
            _start_bot_thread(phone, ss)


# ── Module-level autostart (runs under gunicorn/waitress too) ──────────────────
autostart()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
