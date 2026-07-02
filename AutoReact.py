import asyncio
import sys
import os
import threading

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

from flask import Flask
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji
from telethon.utils import get_peer_id
from telethon.errors import FloodWaitError

# ====================================================
#  AUTO REACT BOT — Web Service for Render
# ====================================================

API_ID   = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'

PHONE          = '+85593687814'
TARGET_CHAT_ID = -1002199457550
REACTION       = '\U0001f64f'  # 🙏
DELAY          = 0.5
PORT           = int(os.environ.get('PORT', 10000))

# ====================================================
# Session: StringSession from env var (Render)
# Falls back to file session (local)
# ====================================================
SESSION_STRING = os.environ.get('SESSION_STRING', '')

if SESSION_STRING:
    print("[*] Using StringSession from environment variable")
    session = StringSession(SESSION_STRING)
else:
    print("[*] Using local session file: session_autoreact")
    session = 'session_autoreact'

# ====================================================
# Flask Web Server (keeps Render Web Service alive)
# ====================================================
app = Flask(__name__)

@app.route('/')
def home():
    return f'<h2>Auto React Bot is Running! 🙏</h2><p>Target: {TARGET_CHAT_ID}</p>'

@app.route('/health')
def health():
    return 'OK', 200

def run_flask():
    app.run(host='0.0.0.0', port=PORT)

# ====================================================
# Telethon Bot
# ====================================================
client = TelegramClient(
    session, API_ID, API_HASH,
    device_model     = 'Desktop',
    system_version   = 'Windows 10',
    app_version      = '5.3.1',
    lang_code        = 'km',
    system_lang_code = 'en'
)

@client.on(events.NewMessage)
async def auto_react(event):
    msg = event.message
    if not msg or not msg.id:
        return

    chat_id = event.chat_id
    if chat_id is None:
        try:
            chat = await event.get_input_chat()
            chat_id = get_peer_id(chat)
        except Exception:
            chat_id = None

    if chat_id != TARGET_CHAT_ID:
        return

    print(f"[Debug] NewMessage in target chat {chat_id} msg={msg.id} sender={event.sender_id}")
    await asyncio.sleep(DELAY)

    try:
        await client(SendReactionRequest(
            peer     = await event.get_input_chat(),
            msg_id   = msg.id,
            reaction = [ReactionEmoji(emoticon=REACTION)]
        ))
        sender = await event.get_sender()
        name   = getattr(sender, 'first_name', None) or str(event.sender_id)
        print(f"[React] {REACTION}  Msg#{msg.id}  From: {name}")

    except FloodWaitError as e:
        print(f"[FloodWait] Waiting {e.seconds}s ...")
        await asyncio.sleep(e.seconds)

    except Exception as e:
        err = str(e)
        if 'REACTION_INVALID' in err or 'same' in err.lower():
            pass
        else:
            print(f"[Error] Msg#{msg.id}: {err}")

async def main():
    # Start Flask in background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"[*] Flask server started on port {PORT}")

    # Start Telegram bot
    print("[*] Connecting to Telegram...")
    if SESSION_STRING:
        # StringSession — just connect, already authenticated
        await client.connect()
    else:
        # Local file session — use start() for login prompt
        await client.start(phone=PHONE)

    me = await client.get_me()
    print("=" * 50)
    print(f"[OK] Logged in  : {me.first_name} (@{me.username})")
    print(f"[OK] Target Chat: {TARGET_CHAT_ID}")
    print(f"[OK] Reaction   : {REACTION}")
    print("=" * 50)
    print("[RUN] Auto React Bot running...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())