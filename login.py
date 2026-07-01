import asyncio
import sys
import os

# Fix Windows console Unicode
if sys.platform == 'win32':
    os.system('chcp 65001 > nul')
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError

# ====================================================
#  LOGIN SCRIPT
#  ប្រើ Telegram Desktop Official Credentials
#  (មិនចាំបាច់ API ផ្ទាល់ខ្លួន)
# ====================================================

# Telegram Desktop Official Credentials (built-in / public)
API_ID   = 2040
API_HASH = 'b18441a1ff607e10a989891a5462e627'

# ← ប្ដូរតែ PHONE ម្នាក់ឯង
PHONE = '+85593687814'

SESSION_NAME = 'session_autoreact'

# ====================================================

async def main():
    client = TelegramClient(
        SESSION_NAME, API_ID, API_HASH,
        device_model   = 'Desktop',
        system_version = 'Windows 10',
        app_version    = '5.3.1',
        lang_code      = 'km',
        system_lang_code = 'en'
    )

    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"[INFO] Already logged in as: {me.first_name} (@{me.username})")
        await client.disconnect()
        return

    # Request OTP
    print(f"[*] Sending OTP code to {PHONE} ...")
    try:
        await client.send_code_request(PHONE)
    except FloodWaitError as e:
        print(f"[!] FloodWait: Please wait {e.seconds} seconds before trying again.")
        await client.disconnect()
        return

    # Enter OTP
    code = input("[?] Enter OTP Code from Telegram: ").strip()

    try:
        await client.sign_in(PHONE, code)

    except PhoneCodeInvalidError:
        print("[!] Invalid code. Please try again.")
        await client.disconnect()
        return

    except SessionPasswordNeededError:
        # 2FA enabled
        password = input("[?] Enter your 2FA Password: ").strip()
        try:
            await client.sign_in(password=password)
        except Exception as e:
            print(f"[!] 2FA Error: {e}")
            await client.disconnect()
            return

    except Exception as e:
        print(f"[!] Login Error: {e}")
        await client.disconnect()
        return

    # Success
    me = await client.get_me()
    print("=" * 50)
    print(f"[OK] Login SUCCESS!")
    print(f"     Name     : {me.first_name} {me.last_name or ''}")
    print(f"     Username : @{me.username}")
    print(f"     User ID  : {me.id}")
    print("=" * 50)
    print(f"[SAVED] Session file -> {SESSION_NAME}.session")
    print("[NEXT]  Now run:  py -3.14 AutoReact.py")

    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())