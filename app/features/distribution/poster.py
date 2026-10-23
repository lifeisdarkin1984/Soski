"""انتشار خودکار در کانال نهایی با تخصیص چرخشی ربات.
مسیر: (ذخیره در مخزن انجام شده) ← صف ← انتخاب ربات ← کپی از مخزن به کانال نهایی با دکمه‌ی اختصاصی."""
import asyncio
import logging
from datetime import timedelta
from typing import Awaitable, Callable

from app.features.distribution.botapi import BotApi, BotApiError, BotApiNetworkError
from app.features.distribution.logic import (
    DEFAULT_POST_TEXT, DEFAULT_TPL_BUTTON, POST_TEXT_LIMIT, TemplateTooLong, VIDEO_CAPTION_LIMIT,
    build_deep_link, compose_final, utcnow,
)

log = logging.getLogger("Collector.distribution")

MAX_PUBLISH_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 60


async def enqueue_post(store, delivery_id: int, storage_chat_id: int, storage_msg_ids: list, caption: str) -> bool:
    """بعد از ذخیره‌ی موفق ویدیوها در مخزن صدا زده می‌شود (یک «بسته» = همه‌ی ویدیوهای یک پست).
    تکراری بی‌اثر است (idempotent)."""
    from app.features.distribution.logic import make_token
    return await store.enqueue_post(delivery_id, storage_chat_id, list(storage_msg_ids), caption or "", make_token())


class PostPublisher:
    def __init__(self, store, api_for: Callable[[int], Awaitable[BotApi]], now_fn: Callable = utcnow, flags=None):
        self.store = store
        self.api_for = api_for
        self.now = now_fn
        self.flags = flags              # FlagCache (اختیاری)

    async def run_once(self) -> bool:
        """یک بسته را منتشر می‌کند؛ True یعنی کاری انجام شد."""
        if self.flags is not None and not await self.flags.get("distribute_enabled"):
            return False                # توزیع خاموش: صف می‌ماند
        final_raw = await self.store.get_setting("final_chat_id")
        if not final_raw:
            return False
        final_chat = int(final_raw)
        claim = await self.store.claim_next_post(self.now())
        if claim is None:
            return False
        post, bot = claim

        suffix = await self.store.get_setting("caption_suffix") or ""
        button = await self.store.get_setting("tpl_button") or DEFAULT_TPL_BUTTON
        try:
            # کانال نهایی فقط کپشن + دکمه می‌گیرد؛ ویدیوها فقط داخل ربات دریافت‌اند
            post_text, _ = compose_final(post.caption, suffix, POST_TEXT_LIMIT)
            video_caption, trimmed = compose_final(post.caption, suffix, VIDEO_CAPTION_LIMIT)
        except TemplateTooLong as e:
            await self.store.mark_post_failed(post.id, str(e))
            log.error(f"Post {post.id}: {e}")
            return True

        keyboard = {"inline_keyboard": [[{"text": button, "url": build_deep_link(bot.username, post.token)}]]}
        try:
            api = await self.api_for(bot.id)
            res = await api.send_message(final_chat, post_text or DEFAULT_POST_TEXT, reply_markup=keyboard)
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

        await self.store.mark_post_published(post.id, final_chat, int(res["message_id"]), video_caption, trimmed,
                                             self.now())
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
