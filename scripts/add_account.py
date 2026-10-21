"""افزودن اکانت از ترمینال سرور:  python -m scripts.add_account
کد ورود فقط در همین ترمینال تایپ می‌شود و از تلگرام عبور نمی‌کند."""
import asyncio
import getpass
from app.core.database import engine
from app.core.exceptions import AccountAuthError
from app.features.accounts.manager import account_manager


async def run():
    phone = input("Phone (e.g. +989123456789): ").strip()
    try:
        phone = await account_manager.begin_login(phone)
        print("Code sent to your Telegram app.")
        status = await account_manager.submit_code(phone, input("Code: "))
        if status == "password_required":
            status = await account_manager.submit_password(phone, getpass.getpass("2FA password: "))
        print("Account saved. Restart the service (or use the admin bot) to start it.")
    except AccountAuthError as e:
        print(f"Failed: {e}")
        await account_manager.cancel_login(phone)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
