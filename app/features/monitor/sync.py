from pyrogram import Client
from pyrogram.enums import ChatType
from app.core.logger import logger
from app.core.database import AsyncSessionLocal
from app.repositories.channel_repository import ChannelRepository


async def sync_monitored_channels(client: Client, account_id: int):
    """همه‌ی کانال‌های اکانت را در دیتابیس ثبت می‌کند (هیچ کانالی کنار گذاشته نمی‌شود)."""
    try:
        async with AsyncSessionLocal() as db:
            repo = ChannelRepository(db)
            existing = {ch.channel_id: ch for ch in await repo.get_monitored_channels(account_id)}
            async for dialog in client.get_dialogs():
                chat = dialog.chat
                if chat.type != ChatType.CHANNEL:
                    continue
                old = existing.get(chat.id)
                if old is not None:
                    if old.channel_title != chat.title or old.channel_username != chat.username:
                        await repo.update_meta(chat.id, account_id, chat.username, chat.title)
                    continue
                await repo.add_monitored_channel(chat.id, chat.username, account_id, chat.title)
    except Exception as e:
        logger.error(f"Error syncing channels: {type(e).__name__}: {e}", exc_info=True)
