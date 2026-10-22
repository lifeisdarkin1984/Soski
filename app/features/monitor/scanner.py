from typing import Awaitable, Callable
from pyrogram import Client
from pyrogram.types import Message
from app.core.config import settings
from app.core.logger import logger
from app.core.database import AsyncSessionLocal
from app.repositories.channel_repository import ChannelRepository
from app.repositories.message_repository import MessageRepository


async def perform_initial_scan(client: Client, account_id: int,
                               ingest: Callable[[Message], Awaitable[None]]):
    """کانال‌های تازه‌ثبت‌شده: آخرین N پست (پیش‌فرض ۱۰۰) از قدیمی به جدید پردازش می‌شوند."""
    async with AsyncSessionLocal() as db:
        channels = await ChannelRepository(db).get_monitored_channels(account_id)
    for channel in channels:
        if channel.last_processed_msg_id > 0:
            continue
        try:
            messages = [m async for m in client.get_chat_history(
                channel.channel_id, limit=settings.initial_scan_count)]
            if not messages:
                continue
            for msg in reversed(messages):
                await ingest(msg)
            logger.info(f"Initial scan: {len(messages)} post(s) checked in channel {channel.channel_id}")
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
