import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message
from app.core.config import settings
from app.core.logger import logger
from app.core.database import AsyncSessionLocal
from app.repositories.message_repository import MessageRepository
from app.repositories.channel_repository import ChannelRepository
from app.repositories.flow_repository import FlowRepository
from app.repositories.settings_repository import get_destination_id
from app.features.monitor.sync import sync_monitored_channels
from app.features.monitor.scanner import perform_initial_scan, catch_up_missed
from app.features.detector.analyzer import analyzer
from app.features.bot_engine.flow import bot_flow_engine
from app.features.publisher.publisher import publisher

MAX_CONCURRENT_FLOWS = 3


class AccountWorker:
    def __init__(self, account_id: int, phone: str, session_string: str):
        self.account_id = account_id
        self.phone = phone
        self.client = Client(
            name=f"user_{account_id}", api_id=settings.api_id, api_hash=settings.api_hash,
            session_string=session_string, workdir="sessions/users"
        )
        self.is_running = False
        self._handlers_registered = False
        self._flow_semaphore = asyncio.Semaphore(MAX_CONCURRENT_FLOWS)
        self._monitored_ids: set[int] = set()
        self._active: set[int] = set()          # پیام‌هایی که همین الان در صف یا حال اجرا هستند
        self._tasks: set[asyncio.Task] = set()

    # ---------- راه‌اندازی ----------
    def _setup_handlers(self):
        if self._handlers_registered:
            return

        @self.client.on_message(filters.channel)
        async def handle_message(client: Client, message: Message):
            if self.is_running:
                await self.ingest_message(message)

        self._handlers_registered = True

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def start(self):
        logger.info(f"Starting Worker for Account {self.account_id}")
        if not self.client.is_initialized:
            await self.client.start()
        self.is_running = True
        self._setup_handlers()
        await self._sync_all()
        # این‌ها در پس‌زمینه اجرا می‌شوند تا شروع کارگر معطل نماند
        self._spawn(self._startup_recovery())
        self._spawn(self._resync_loop())

    async def _sync_all(self):
        await sync_monitored_channels(self.client, self.account_id)
        await perform_initial_scan(self.client, self.account_id)
        async with AsyncSessionLocal() as db:
            channels = await ChannelRepository(db).get_monitored_channels(self.account_id)
        self._monitored_ids = {c.channel_id for c in channels}

    async def sync_now(self):
        await self._sync_all()

    async def _startup_recovery(self):
        try:
            await catch_up_missed(self.client, self.account_id, self.ingest_message)
            await self.resume_stuck_flows()
        except Exception as e:
            logger.error(f"Startup recovery failed: {type(e).__name__}: {e}", exc_info=True)

    async def _resync_loop(self):
        while True:
            await asyncio.sleep(settings.resync_interval_minutes * 60)
            try:
                await self._sync_all()
            except Exception as e:
                logger.error(f"Periodic resync failed: {type(e).__name__}: {e}")

    # ---------- دریافت پیام ----------
    async def ingest_message(self, message: Message):
        chat_id = message.chat.id
        monitored = chat_id in self._monitored_ids
        try:
            analysis = analyzer.analyze_message(message)
            logger.info(f"Post {chat_id}/{message.id}: monitored={monitored} "
                        f"bot={analysis['bot_username'] or '-'}")
            if not monitored:
                return
            async with AsyncSessionLocal() as db:
                if analysis['is_media_request']:
                    p_msg = await MessageRepository(db).mark_as_pending_request(
                        channel_id=chat_id, message_id=message.id, account_id=self.account_id,
                        bot_username=analysis['bot_username'], start_param=analysis.get('start_param'))
                    if p_msg is not None:
                        logger.info(f"Bot link found in post {chat_id}/{message.id}: @{analysis['bot_username']}")
                        self._schedule(p_msg.id, chat_id, message.id, analysis['bot_username'])
                await ChannelRepository(db).update_last_processed_msg(chat_id, self.account_id, message.id)
        except Exception as e:
            logger.error(f"Failed to ingest message {chat_id}/{message.id}: {type(e).__name__}: {e}", exc_info=True)

    # ---------- اجرای جریان ----------
    def _schedule(self, processed_msg_id: int, channel_id: int, message_id: int, bot_username: str):
        if processed_msg_id in self._active:
            return
        self._active.add(processed_msg_id)
        self._spawn(self._run_pipeline(channel_id, message_id, bot_username, processed_msg_id))

    async def resume_stuck_flows(self) -> int:
        async with AsyncSessionLocal() as db:
            pending = await MessageRepository(db).get_pending_or_stuck_messages(self.account_id)
        count = 0
        for p in pending:
            if p.id not in self._active:
                self._schedule(p.id, p.channel_id, p.message_id, p.bot_username)
                count += 1
        if count:
            logger.info(f"Account {self.account_id}: resumed {count} unfinished request(s)")
        return count

    async def _run_pipeline(self, channel_id: int, message_id: int, bot_username: str, processed_msg_id: int):
        try:
            if await get_destination_id() is None:
                logger.warning("Destination not set; request stays pending until it is set in the admin bot.")
                return
            async with self._flow_semaphore:
                bot_media_msg, joined_count = await bot_flow_engine.process_request(
                    client=self.client, processed_msg_id=processed_msg_id,
                    bot_username=bot_username, account_id=self.account_id)
                if not bot_media_msg:
                    return
                await publisher.publish_media(
                    client=self.client, bot_media_msg=bot_media_msg,
                    source_channel_id=channel_id, source_msg_id=message_id,
                    account_id=self.account_id, target_bot_username=bot_username,
                    joined_channels_count=joined_count, processed_msg_id=processed_msg_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Critical error in pipeline: {type(e).__name__}: {e}", exc_info=True)
            try:
                async with AsyncSessionLocal() as db:
                    await FlowRepository(db).mark_message_failed(processed_msg_id, f"{type(e).__name__}: {e}")
            except Exception:
                logger.error("Could not record pipeline failure", exc_info=True)
        finally:
            self._active.discard(processed_msg_id)

    async def stop(self):
        self.is_running = False
        for task in list(self._tasks):
            task.cancel()
        if self.client.is_initialized:
            try:
                await self.client.stop()
            except Exception as e:
                logger.warning(f"Error stopping client {self.account_id}: {type(e).__name__}")
