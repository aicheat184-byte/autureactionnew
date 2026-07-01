"""
Run this ONCE locally to get your SESSION_STRING for Render deploy.
IMPORTANT: Stop AutoReact.py first before running this!

py -3.14 export_session.py
"""
import sys
import os
import shutil

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID   = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'

async def main():
    # Copy session to avoid locking conflict
    src = 'session_autoreact.session'
    dst = 'session_export_tmp.session'

    if not os.path.exists(src):
        print(f"[!] Session file '{src}' not found!")
        print("[!] Please run login.py first.")
        return

    shutil.copy2(src, dst)

    client = TelegramClient('session_export_tmp', API_ID, API_HASH)
    await client.connect()

    session_string = StringSession.save(client.session)
    await client.disconnect()

    # Cleanup temp
    for f in ['session_export_tmp.session', 'session_export_tmp.session-journal']:
        if os.path.exists(f):
            os.remove(f)

    print("=" * 60)
    print("SESSION_STRING (copy this for Render env var):")
    print("=" * 60)
    print(session_string)
    print("=" * 60)
    print("[DONE] Paste this into Render -> Environment -> SESSION_STRING")

if __name__ == '__main__':
    asyncio.run(main())
