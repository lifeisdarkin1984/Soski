from pyrogram import Client
from pyrogram.enums import ChatType
from app.core.logger import logger
from app.core.database import AsyncSessionLocal
from app.repositories.channel_repository import ChannelRepository
from app.repositories.flow_repository import FlowRepository


async def sync_monitored_channels(client: Client, account_id: int):
    """کانال‌های اکانت را در دیتابیس ثبت می‌کند؛ کانال‌هایی که فقط برای عضویت اجباری جوین شده‌ایم نادیده گرفته می‌شوند."""
    try:
        async with AsyncSessionLocal() as db:
            repo = ChannelRepository(db)
            excluded = await FlowRepository(db).get_force_joined_channel_ids()
            existing_ids = {ch.channel_id for ch in await repo.get_monitored_channels(account_id)}
            async for dialog in client.get_dialogs():
                chat = dialog.chat
                if chat.type != ChatType.CHANNEL or chat.id in existing_ids or chat.id in excluded:
                    continue
                await repo.add_monitored_channel(chat.id, chat.username, account_id)
                existing_ids.add(chat.id)
    except Exception as e:
        logger.error(f"Error syncing channels: {type(e).__name__}: {e}", exc_info=True)
