"""Run locally (e.g. Termux) to generate the Music Assistant session string.

Do not run this on a public/shared machine. Do not commit the generated string.
"""
import asyncio
import getpass

from pyrogram import Client


async def main():
    api_id = int(input("Telegram API ID: ").strip())
    api_hash = getpass.getpass("Telegram API HASH: ").strip()
    phone = input("Assistant account phone number (international format): ").strip()

    app = Client(
        "music_assistant_login",
        api_id=api_id,
        api_hash=api_hash,
        phone_number=phone,
        in_memory=True,
    )
    try:
        await app.start()
        session_string = await app.export_session_string()
        print("\n=== MUSIC_SESSION_STRING (KEEP SECRET) ===")
        print(session_string)
        print("=== END SESSION STRING ===")
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
