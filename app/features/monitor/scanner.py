from typing import Awaitable, Callable
from pyrogram import Client
from pyrogram.types import Message
from app.core.config import settings
from app.core.logger import logger
from app.core.database import AsyncSessionLocal
from app.repositories.channel_repository import ChannelRepository
from app.repositories.message_repository import MessageRepository


async def perform_initial_scan(client: Client, account_id: int):
    """کانال‌های تازه‌ثبت‌شده: پست‌های قدیمی فقط «دیده‌شده» علامت می‌خورند و پردازش نمی‌شوند."""
    async with AsyncSessionLocal() as db:
        channel_repo = ChannelRepository(db)
        message_repo = MessageRepository(db)
        for channel in await channel_repo.get_monitored_channels(account_id):
            if channel.last_processed_msg_id > 0:
                continue
            try:
                messages = [m async for m in client.get_chat_history(
                    channel.channel_id, limit=settings.initial_scan_count)]
                last_id = 0
                for msg in reversed(messages):
                    await message_repo.mark_as_scanned(channel.channel_id, msg.id, account_id)
                    last_id = max(last_id, msg.id)
                if last_id:
                    await channel_repo.update_last_processed_msg(channel.channel_id, account_id, last_id)
            except Exception as e:
                logger.error(f"Initial scan failed for channel {channel.channel_id}: {type(e).__name__}: {e}")


async def catch_up_missed(client: Client, account_id: int, ingest: Callable[[Message], Awaitable[None]]):
    """پست‌هایی که وقتی برنامه خاموش بود منتشر شده‌اند را از روی آخرین پست پردازش‌شده پیدا و پردازش می‌کند."""
    async with AsyncSessionLocal() as db:
        channels = await ChannelRepository(db).get_monitored_channels(account_id)
    for channel in channels:
        if channel.last_processed_msg_id <= 0:
            continue
        try:
            fresh = []
            async for msg in client.get_chat_history(channel.channel_id, limit=settings.catch_up_limit):
                if msg.id <= channel.last_processed_msg_id:
                    break
                fresh.append(msg)
            for msg in reversed(fresh):
                await ingest(msg)
            if fresh:
                logger.info(f"Catch-up: {len(fresh)} missed post(s) in channel {channel.channel_id}")
        except Exception as e:
            logger.error(f"Catch-up failed for channel {channel.channel_id}: {type(e).__name__}: {e}")
