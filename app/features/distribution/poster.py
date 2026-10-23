"""انتشار خودکار در کانال نهایی با تخصیص چرخشی ربات.
مسیر: (ذخیره در مخزن انجام شده) ← صف ← انتخاب ربات ← کپی از مخزن به کانال نهایی با دکمه‌ی اختصاصی."""
import asyncio
import logging
from datetime import timedelta
from typing import Awaitable, Callable

from app.features.distribution.botapi import BotApi, BotApiError, BotApiNetworkError
from app.features.distribution.logic import (
    DEFAULT_TPL_BUTTON, DEFAULT_TPL_CAPTION, TemplateTooLong, build_deep_link, compose_caption, utcnow,
)

log = logging.getLogger("Collector.distribution")

MAX_PUBLISH_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 60


async def enqueue_post(store, delivery_id: int, storage_chat_id: int, storage_msg_id: int, caption: str) -> bool:
    """بعد از ذخیره‌ی موفق ویدیو در مخزن صدا زده می‌شود. تکراری بی‌اثر است (idempotent)."""
    from app.features.distribution.logic import make_token
    return await store.enqueue_post(delivery_id, storage_chat_id, storage_msg_id, caption or "", make_token())


class PostPublisher:
    def __init__(self, store, api_for: Callable[[int], Awaitable[BotApi]], now_fn: Callable = utcnow):
        self.store = store
        self.api_for = api_for
        self.now = now_fn
        self._last_no_bot_warn = None

    async def run_once(self) -> bool:
        """یک پست را پردازش می‌کند؛ True یعنی کاری انجام شد."""
        final_raw = await self.store.get_setting("final_chat_id")
        if not final_raw:
            return False
        final_chat = int(final_raw)
        claim = await self.store.claim_next_post(self.now())
        if claim is None:
            return False
        post, bot = claim

        tpl = await self.store.get_setting("tpl_caption") or DEFAULT_TPL_CAPTION
        promo = await self.store.get_setting("tpl_promo") or ""
        button = await self.store.get_setting("tpl_button") or DEFAULT_TPL_BUTTON
        try:
            caption, trimmed = compose_caption(tpl, post.caption, promo)
        except TemplateTooLong as e:
            await self.store.mark_post_failed(post.id, str(e))
            log.error(f"Post {post.id}: {e}")
            return True

        keyboard = {"inline_keyboard": [[{"text": button, "url": build_deep_link(bot.username, post.token)}]]}
        try:
            api = await self.api_for(bot.id)
            res = await api.copy_message(final_chat, post.storage_chat_id, post.storage_msg_id,
                                         caption=caption, reply_markup=keyboard)
        except BotApiNetworkError:
            # شاید منتشر شده باشد؛ بدون بررسی دستی دوباره نمی‌فرستیم
            await self.store.mark_post_unknown(post.id, "network error; result unknown")
            log.error(f"Post {post.id}: publish result unknown (network); not resent")
            return True
        except BotApiError as e:
            reason = f"bot @{bot.username}: {e.code} {e.description}"[:500]
            await self.store.bump_bot(bot.id, errors=1)
            if post.attempts < MAX_PUBLISH_ATTEMPTS:
                await self.store.mark_post_retry(post.id, reason, self.now() + timedelta(seconds=RETRY_DELAY_SECONDS))
            else:
                await self.store.mark_post_failed(post.id, reason)
            log.error(f"Post {post.id}: publish failed ({reason})")
            return True

        final_msg_id = int(res["message_id"])
        await self.store.mark_post_published(post.id, final_chat, final_msg_id, caption, trimmed, self.now())
        if trimmed and post.caption:
            try:   # کپشن بلند بی‌صدا حذف نمی‌شود: متن کامل زیر پست می‌آید
                await api.send_message(final_chat, post.caption, reply_to_message_id=final_msg_id)
            except (BotApiError, BotApiNetworkError) as e:
                log.warning(f"Post {post.id}: full caption message failed: {type(e).__name__}")
        return True

    async def run_forever(self):
        while True:
            try:
                did = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(f"post publisher error: {type(e).__name__}: {e}", exc_info=True)
                await asyncio.sleep(10)
                continue
            await asyncio.sleep(3 if did else 5)      # فاصله بین انتشارها؛ جلوگیری از FloodWait کانال
