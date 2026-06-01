"""
Generate a Telethon StringSession for hosted deployments.
"""

import asyncio
import os
from getpass import getpass

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from .utils import async_input, cast_to_int


async def main() -> None:
    api_id = (
        cast_to_int(os.environ["TG_API_ID"], "TG_API_ID")
        if os.environ.get("TG_API_ID")
        else cast_to_int(await async_input("Enter your API ID: "), "api_id")
    )
    api_hash = os.environ.get("TG_API_HASH") or await async_input("Enter your API hash: ")

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        phone = await async_input("Enter your phone: ")
        await client.send_code_request(phone)
        code = await async_input("Enter the code you just received: ")
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            await client.sign_in(password=getpass("Two step verification password: "))

        print("\nCopy this value into Koyeb as TG_SESSION_STRING:\n")
        print(client.session.save())
    finally:
        await client.disconnect()


def entry() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    entry()
