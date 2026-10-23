import asyncio
import os
import re
from pyrogram import Client
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from app.core.logger import logger
from app.core.database import AsyncSessionLocal
from app.repositories.flow_repository import FlowRepository
from app.repositories.settings_repository import get_destination_id
from app.features.processor.caption_cleaner import caption_cleaner
from app.features.bot_engine.helpers import get_media

# خطاهایی که نتیجه‌ی ارسال را «نامشخص» می‌کنند (ممکن است پیام رفته باشد)
AMBIGUOUS_ERRORS = (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)


async def _send_with_flood_retry(factory):
    for attempt in range(3):
        try:
            return await factory()
        except FloodWait as e:
            if attempt == 2:
                raise
            await asyncio.sleep(min(int(e.value) + 1, 300))


class Publisher:
    @staticmethod
    async def _send_cached(client: Client, destination: int, kind: str, file_id: str, caption: str):
        # ارسال با file_id: بدون دانلود و آپلود دوباره
        return await _send_with_flood_retry(lambda: client.send_cached_media(
            chat_id=destination, file_id=file_id, caption=caption))

    @staticmethod
    async def _send_uploaded(client: Client, destination: int, bot_media_msg: Message, kind: str,
                             unique_id: str, caption: str):
        safe = re.sub(r"[^A-Za-z0-9_-]", "", unique_id) or "file"
        path = None
        try:
            path = await bot_media_msg.download(file_name=f"temp_{safe}")
            if not path:
                raise RuntimeError("Download returned no file")
            sender = {
                "video": lambda: client.send_video(destination, path, caption=caption, supports_streaming=True),
                "photo": lambda: client.send_photo(destination, path, caption=caption),
                "audio": lambda: client.send_audio(destination, path, caption=caption),
                "voice": lambda: client.send_voice(destination, path, caption=caption),
                "animation": lambda: client.send_animation(destination, path, caption=caption),
                "document": lambda: client.send_document(destination, path, caption=caption),
            }[kind]
            return await _send_with_flood_retry(sender)
        finally:
            if path and os.path.exists(str(path)):
                try:
                    os.remove(path)
                except OSError:
                    pass

    @staticmethod
    async def publish_media(client: Client, bot_media_msg: Message, source_channel_id: int, source_msg_id: int,
                            account_id: int, target_bot_username: str, joined_channels_count: int,
                            processed_msg_id: int) -> bool:
        found = get_media(bot_media_msg)
        async with AsyncSessionLocal() as db:
            flow_repo = FlowRepository(db)
            if not found:
                await flow_repo.mark_message_failed(processed_msg_id, "Bot message has no media")
                return False
            kind, media_obj = found
            file_id, file_unique_id = media_obj.file_id, media_obj.file_unique_id

            # کپشن از پست اصلی کانال (نه از پیام ربات)
            try:
                original = await client.get_messages(source_channel_id, source_msg_id)
                text = original.text or original.caption or ""
                entities = original.entities or original.caption_entities or []
            except Exception as e:
                logger.warning(f"Could not re-read source post: {type(e).__name__}")
                text, entities = "", []
            caption = caption_cleaner.clean(text, entities)

            outcome, delivery_id = await flow_repo.claim_delivery({
                'source_channel_id': source_channel_id, 'source_msg_id': source_msg_id,
                'account_id': account_id, 'target_bot_username': target_bot_username,
                'joined_channels_count': joined_channels_count, 'file_id': file_id,
                'file_unique_id': file_unique_id,
            })
            if outcome != "claimed":
                if outcome == "busy":
                    logger.warning(f"File {file_unique_id} is being sent or in unknown state; skipping resend.")
                await flow_repo.mark_message_completed(processed_msg_id)
                return True

            destination = await get_destination_id()
            if destination is None:
                await flow_repo.release_delivery(delivery_id)
                await flow_repo.mark_message_failed(processed_msg_id, "Destination chat is not set")
                return False
            sent = None
            try:
                try:
                    sent = await Publisher._send_cached(client, destination, kind, file_id, caption)
                except AMBIGUOUS_ERRORS:
                    raise
                except Exception as e:
                    logger.info(f"Cached send failed ({type(e).__name__}); falling back to download+upload")
                    sent = await Publisher._send_uploaded(client, destination, bot_media_msg, kind,
                                                          file_unique_id, caption)
            except AMBIGUOUS_ERRORS as e:
                # شاید پیام رفته باشد؛ دوباره نمی‌فرستیم
                await flow_repo.mark_delivery_unknown(delivery_id)
                await flow_repo.mark_message_failed(
                    processed_msg_id, f"Send result unknown ({type(e).__name__}); not resent automatically")
                logger.error(f"Ambiguous send result for {file_unique_id}: {type(e).__name__}")
                return False
            except Exception as e:
                await flow_repo.release_delivery(delivery_id)
                await flow_repo.mark_message_failed(processed_msg_id, f"Publish error: {type(e).__name__}: {e}")
                logger.error(f"Publish failed: {type(e).__name__}: {e}", exc_info=True)
                return False

            sent_msg = sent[0] if isinstance(sent, list) else sent
            sent_id = getattr(sent_msg, "id", None)
            when = getattr(sent_msg, "date", None)
            report = (
                f"Status: Success | Source: {source_channel_id} (msg {source_msg_id}) | Account: {account_id} | "
                f"Bot: {target_bot_username} | Joined: {joined_channels_count} | "
                f"Time: {when.strftime('%Y-%m-%d %H:%M:%S') if when else '-'}"
            )
            await flow_repo.mark_delivery_sent(delivery_id, sent_id, report)
            await flow_repo.mark_message_completed(processed_msg_id)
            # ذخیره در مخزن موفق بود ← پست برای انتشار در کانال نهایی صف می‌شود (تکراری بی‌اثر است)
            if sent_id:
                try:
                    from app.features.distribution.runtime import distribution
                    from app.features.distribution.poster import enqueue_post
                    await enqueue_post(distribution.store, delivery_id, destination, int(sent_id), caption)
                except Exception as e:
                    logger.error(f"Could not queue final post for delivery {delivery_id}: {type(e).__name__}: {e}")
            return True


publisher = Publisher()
