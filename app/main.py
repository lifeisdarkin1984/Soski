import asyncio
import signal

# پایروگرام هنگام ساخت Client (موقع import) حلقه‌ی asyncio را می‌گیرد؛ پس حلقه را قبل از import ها می‌سازیم
# و همان را اجرا می‌کنیم تا «attached to a different loop» پیش نیاید.
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

from app.core.logger import logger
from app.core.config import settings
from app.core.database import engine, AsyncSessionLocal
from app.repositories.flow_repository import FlowRepository
from app.repositories.settings_repository import get_destination_info
from app.features.accounts.manager import worker_manager, account_manager
from app.admin.bot import admin_bot
from app.admin import handlers  # noqa: F401  (ثبت هندلرهای پنل)

shutdown_event = asyncio.Event()


async def main():
    logger.info("=" * 50)
    logger.info("Starting Telegram Media Collector")
    logger.info("=" * 50)

    if not settings.admin_id_list:
        logger.error("ADMIN_IDS is empty. Set at least one admin Telegram user id in .env")
        return

    running_loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            running_loop.add_signal_handler(sig, shutdown_event.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: shutdown_event.set())

    try:
        # ارسال‌های نیمه‌تمامِ اجرای قبلی: نتیجه‌شان معلوم نیست، پس دوباره فرستاده نمی‌شوند
        async with AsyncSessionLocal() as db:
            unknown = await FlowRepository(db).reconcile_sending_deliveries()
        if unknown:
            logger.warning(f"{unknown} delivery(ies) were interrupted mid-send; marked 'unknown' (not resent).")

        dest_id, _ = await get_destination_info()
        if dest_id is None:
            logger.warning("Destination chat is not set yet. Set it from the admin bot (📤 مقصد ارسال).")
        await admin_bot.start()
        logger.info("Admin bot is running")

        started = await account_manager.start_all_active()
        if started == 0:
            logger.info("No active accounts yet. Add one from the admin bot or: python -m scripts.add_account")
        logger.info("System is fully running.")
        await shutdown_event.wait()
    except asyncio.CancelledError:
        logger.info("Cancelled")
    except Exception as e:
        logger.error(f"Fatal error: {type(e).__name__}: {e}", exc_info=True)
    finally:
        logger.info("Shutting down...")
        for account_id in list(worker_manager.workers.keys()):
            try:
                await worker_manager.stop_worker(account_id)
            except Exception as e:
                logger.warning(f"Error stopping worker {account_id}: {type(e).__name__}")
        try:
            if admin_bot.is_initialized:
                await admin_bot.stop()
        except Exception as e:
            logger.warning(f"Error stopping admin bot: {type(e).__name__}")
        await engine.dispose()
        logger.info("Stopped.")


if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
