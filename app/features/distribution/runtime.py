"""اجرای زنده: دریافت پیام ربات‌ها (polling)، حذف زمان‌دار و انتشار خودکار. یک نمونه‌ی مشترک: `distribution`."""
import asyncio
import logging
from typing import Optional

from app.features.distribution.botapi import BotApi, BotApiError, BotApiNetworkError
from app.features.distribution.poster import PostPublisher
from app.features.distribution.receiver import DeleteScheduler, ReceiverService
from app.features.distribution.store import DistStore

log = logging.getLogger("Collector.distribution")


class BotRunner:
    """دریافت پیام‌های یک ربات با getUpdates. offset در دیتابیس ذخیره می‌شود تا بعد از ری‌استارت
    پیام‌های پردازش‌شده دوباره اجرا نشوند."""

    def __init__(self, bot_pk: int, api: BotApi, store: DistStore):
        self.bot_pk = bot_pk
        self.api = api
        self.store = store
        self.service = ReceiverService(api, bot_pk, store)
        self.task: Optional[asyncio.Task] = None

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name=f"receiver-bot-{self.bot_pk}")

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
            self.task = None

    async def _safe_handle(self, update: dict):
        try:
            await self.service.handle_update(update)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"receiver bot {self.bot_pk}: update failed: {type(e).__name__}: {e}", exc_info=True)

    async def _run(self):
        key = f"rb_off_{self.bot_pk}"
        try:
            await self.api.delete_webhook()      # اگر وبهوک فعال باشد getUpdates کار نمی‌کند
        except (BotApiError, BotApiNetworkError) as e:
            log.warning(f"receiver bot {self.bot_pk}: deleteWebhook failed: {type(e).__name__}")
        raw = await self.store.get_setting(key, "0")
        offset = int(raw) if (raw or "").isdigit() and int(raw) > 0 else None
        backoff = 1
        log.info(f"receiver bot {self.bot_pk} started")
        while True:
            try:
                updates = await self.api.get_updates(offset, timeout=25)
                backoff = 1
            except asyncio.CancelledError:
                raise
            except BotApiError as e:
                if e.code == 401:
                    log.error(f"receiver bot {self.bot_pk}: token rejected by Telegram; deactivating")
                    await self.store.set_bot_active(self.bot_pk, False)
                    await self.store.save_health(self.bot_pk, False, "توکن نامعتبر شد (401)")
                    return
                wait = 30 if e.code == 409 else min(backoff, 30)
                if e.code == 409:
                    log.warning(f"receiver bot {self.bot_pk}: another process is polling this bot (409)")
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, 30)
                continue
            except BotApiNetworkError:
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)
                continue
            if not updates:
                continue
            await asyncio.gather(*(self._safe_handle(u) for u in updates))
            offset = updates[-1]["update_id"] + 1
            try:
                await self.store.set_setting(key, str(offset))
            except Exception as e:
                log.error(f"could not save offset: {type(e).__name__}")


class DistributionRuntime:
    def __init__(self):
        self.store = DistStore()
        self._apis: dict[int, BotApi] = {}
        self._runners: dict[int, BotRunner] = {}
        self._tasks: list[asyncio.Task] = []
        self.deleter = DeleteScheduler(self.store, self.api_for)
        self.publisher = PostPublisher(self.store, self.api_for)

    async def api_for(self, bot_pk: int) -> BotApi:
        api = self._apis.get(bot_pk)
        if api is None:
            from app.core.crypto import crypto
            bot = await self.store.get_bot(bot_pk)
            if bot is None:
                raise RuntimeError("receiver bot not found")
            api = BotApi(crypto.decrypt(bot.token_enc))
            self._apis[bot_pk] = api
        return api

    def forget_api(self, bot_pk: int):
        self._apis.pop(bot_pk, None)

    async def sync_bots(self):
        """ربات‌های فعال را روشن و بقیه را خاموش می‌کند (بعد از هر تغییر از ربات مدیریت صدا زده می‌شود)."""
        active = {b.id for b in await self.store.list_bots() if b.is_active}
        for pk in list(self._runners):
            if pk not in active:
                await self._runners.pop(pk).stop()
        for pk in active:
            if pk not in self._runners:
                self._runners[pk] = BotRunner(pk, await self.api_for(pk), self.store)
            self._runners[pk].start()

    async def _delete_loop(self):
        while True:
            try:
                n = await self.deleter.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"delete loop error: {type(e).__name__}: {e}")
                n = 0
            await asyncio.sleep(1 if n >= 50 else 10)

    async def start(self):
        posts, deliveries = await self.store.reconcile_startup()
        if posts or deliveries:
            log.warning(f"Interrupted by restart → marked unknown: {posts} post(s), {deliveries} delivery(ies)")
        await self.sync_bots()
        self._tasks = [asyncio.create_task(self._delete_loop(), name="delete-loop"),
                       asyncio.create_task(self.publisher.run_forever(), name="post-publisher")]
        log.info("Distribution runtime started")

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        for r in list(self._runners.values()):
            await r.stop()
        self._runners.clear()


distribution = DistributionRuntime()
