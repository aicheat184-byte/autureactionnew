import asyncio
import sys
import os

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji
from telethon.errors import FloodWaitError

# ====================================================
#  AUTO REACT BOT — Deploy on Render / Local
# ====================================================

API_ID   = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'

PHONE          = '+85593687814'
TARGET_CHAT_ID = -1002199457550
REACTION       = '\U0001f64f'  # 🙏
DELAY          = 0.5

# ====================================================
# Use StringSession from env var (for Render deploy)
# Falls back to file session (for local use)
# ====================================================
SESSION_STRING = os.environ.get('SESSION_STRING', '')

if SESSION_STRING:
    print("[*] Using StringSession from environment variable")
    session = StringSession(SESSION_STRING)
else:
    print("[*] Using local session file: session_autoreact")
    session = 'session_autoreact'

client = TelegramClient(
    session, API_ID, API_HASH,
    device_model     = 'Desktop',
    system_version   = 'Windows 10',
    app_version      = '5.3.1',
    lang_code        = 'km',
    system_lang_code = 'en'
)

@client.on(events.NewMessage(chats=TARGET_CHAT_ID))
async def auto_react(event):
    msg = event.message
    if not msg or not msg.id:
        return

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
    print("[*] Connecting...")
    await client.start(phone=PHONE if not SESSION_STRING else None)

    me = await client.get_me()
    print("=" * 50)
    print(f"[OK] Logged in  : {me.first_name} (@{me.username})")
    print(f"[OK] Target Chat: {TARGET_CHAT_ID}")
    print(f"[OK] Reaction   : {REACTION}")
    print("=" * 50)
    print("[RUN] Auto React Bot running... Ctrl+C to stop.")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())