"""دسترسی به دیتابیس برای توزیع (SQLAlchemy). همه‌ی زمان‌ها UTC."""
import asyncio
import json
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, update, func, or_, delete
from sqlalchemy.exc import IntegrityError

from app.core.database import AsyncSessionLocal
from app.models.setting import CollectorSetting
from app.models.distribution import ReceiverBot, MediaPost, ForcedChannel, VideoDelivery
from app.features.distribution.logic import pick_next_bot, utcnow


class DistStore:
    def __init__(self):
        self._claim_lock = asyncio.Lock()      # برنامه یک پردازه است؛ تخصیص چرخشی پشت این قفل + قفل ردیف دیتابیس

    # ---------- تنظیمات (همان جدول collector_settings) ----------
    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        async with AsyncSessionLocal() as db:
            row = await db.get(CollectorSetting, key)
            return row.setting_value if row else default

    async def set_setting(self, key: str, value: str):
        async with AsyncSessionLocal() as db:
            row = await db.get(CollectorSetting, key)
            if row:
                row.setting_value = value
            else:
                db.add(CollectorSetting(setting_key=key, setting_value=value))
            await db.commit()

    # ---------- ربات‌های دریافت ----------
    async def list_bots(self) -> list[ReceiverBot]:
        async with AsyncSessionLocal() as db:
            return list((await db.execute(select(ReceiverBot).order_by(ReceiverBot.id))).scalars().all())

    async def get_bot(self, pk: int) -> Optional[ReceiverBot]:
        async with AsyncSessionLocal() as db:
            return await db.get(ReceiverBot, pk)

    async def get_bot_by_tg_id(self, tg_id: int) -> Optional[ReceiverBot]:
        async with AsyncSessionLocal() as db:
            return (await db.execute(select(ReceiverBot).where(ReceiverBot.bot_id == tg_id))).scalar_one_or_none()

    async def add_bot(self, tg_id: int, username: str, token_enc: str) -> ReceiverBot:
        async with AsyncSessionLocal() as db:
            bot = ReceiverBot(bot_id=tg_id, username=username, token_enc=token_enc, is_active=False)
            db.add(bot)
            await db.commit()
            return bot

    async def set_bot_active(self, pk: int, active: bool):
        async with AsyncSessionLocal() as db:
            await db.execute(update(ReceiverBot).where(ReceiverBot.id == pk).values(is_active=active))
            await db.commit()

    async def delete_bot(self, pk: int) -> bool:
        """فقط اگر هیچ پستی به این ربات وابسته نیست؛ وگرنه False (فقط غیرفعال‌سازی)."""
        async with AsyncSessionLocal() as db:
            used = (await db.execute(select(func.count()).select_from(MediaPost)
                                     .where(MediaPost.assigned_bot_id == pk))).scalar_one()
            used += (await db.execute(select(func.count()).select_from(VideoDelivery)
                                      .where(VideoDelivery.bot_id == pk))).scalar_one()
            if used:
                return False
            await db.execute(delete(ReceiverBot).where(ReceiverBot.id == pk))
            await db.commit()
            return True

    async def bump_bot(self, pk: int, requests: int = 0, errors: int = 0):
        async with AsyncSessionLocal() as db:
            await db.execute(update(ReceiverBot).where(ReceiverBot.id == pk).values(
                request_count=ReceiverBot.request_count + requests,
                error_count=ReceiverBot.error_count + errors))
            await db.commit()

    async def count_published_for_bot(self, pk: int) -> int:
        async with AsyncSessionLocal() as db:
            return (await db.execute(select(func.count()).select_from(MediaPost).where(
                MediaPost.assigned_bot_id == pk, MediaPost.status == "published"))).scalar_one()

    async def save_health(self, pk: int, ok: bool, error: Optional[str]):
        async with AsyncSessionLocal() as db:
            await db.execute(update(ReceiverBot).where(ReceiverBot.id == pk).values(
                last_health_at=utcnow(), last_health_ok=ok, last_error=(error or None) and error[:255]))
            await db.commit()

    # ---------- عضویت اجباری ----------
    async def list_forced(self, only_active: bool = False) -> list[ForcedChannel]:
        async with AsyncSessionLocal() as db:
            q = select(ForcedChannel).order_by(ForcedChannel.id)
            if only_active:
                q = q.where(ForcedChannel.is_active == True)   # noqa: E712
            return list((await db.execute(q)).scalars().all())

    async def add_forced(self, chat_id: int, title: str, link: Optional[str]) -> ForcedChannel:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(ForcedChannel).where(ForcedChannel.chat_id == chat_id))).scalar_one_or_none()
            if row:
                row.title, row.link, row.is_active = title, link, True
            else:
                row = ForcedChannel(chat_id=chat_id, title=title, link=link, is_active=True)
                db.add(row)
            await db.commit()
            return row

    async def set_forced_active(self, pk: int, active: bool):
        async with AsyncSessionLocal() as db:
            await db.execute(update(ForcedChannel).where(ForcedChannel.id == pk).values(is_active=active))
            await db.commit()

    async def delete_forced(self, pk: int):
        async with AsyncSessionLocal() as db:
            await db.execute(delete(ForcedChannel).where(ForcedChannel.id == pk))
            await db.commit()

    # ---------- پست‌های کانال نهایی ----------
    async def enqueue_post(self, delivery_id: int, storage_chat_id: int, storage_msg_ids: list,
                           caption: str, token: str) -> bool:
        async with AsyncSessionLocal() as db:
            db.add(MediaPost(delivery_id=delivery_id, token=token, storage_chat_id=storage_chat_id,
                             storage_msg_id=storage_msg_ids[0], storage_msg_ids=json.dumps(list(storage_msg_ids)),
                             caption=caption, status="pending"))
            try:
                await db.commit()
                return True
            except IntegrityError:
                await db.rollback()
                return False        # برای این تحویل قبلاً صف شده

    async def claim_next_post(self, now: datetime):
        """قدیمی‌ترین پستِ آماده + انتخاب چرخشی ربات، در یک تراکنش. بدون ربات فعال: None و بدون تغییر."""
        async with self._claim_lock:
            async with AsyncSessionLocal() as db:
                post = (await db.execute(
                    select(MediaPost)
                    .where(MediaPost.status == "pending",
                           or_(MediaPost.next_attempt_at.is_(None), MediaPost.next_attempt_at <= now))
                    .order_by(MediaPost.id).limit(1).with_for_update())).scalar_one_or_none()
                if post is None:
                    return None
                bots = list((await db.execute(
                    select(ReceiverBot).where(ReceiverBot.is_active == True)   # noqa: E712
                    .with_for_update())).scalars().all())
                bot = pick_next_bot(bots)
                if bot is None:
                    await db.rollback()
                    return None
                max_seq = (await db.execute(select(func.max(ReceiverBot.last_assigned_seq)))).scalar_one() or 0
                bot.last_assigned_seq = max_seq + 1
                bot.assigned_count += 1
                post.status = "publishing"
                post.assigned_bot_id = bot.id
                post.attempts += 1
                await db.commit()
                return post, bot

    async def _set_post(self, post_id: int, **values):
        async with AsyncSessionLocal() as db:
            await db.execute(update(MediaPost).where(MediaPost.id == post_id).values(**values))
            await db.commit()

    async def mark_post_published(self, post_id, chat_id, msg_id, final_caption, trimmed, now):
        await self._set_post(post_id, status="published", final_chat_id=chat_id, final_msg_id=msg_id,
                             final_caption=final_caption, caption_trimmed=trimmed, published_at=now,
                             error_message=None)

    async def mark_post_retry(self, post_id, error, retry_at):
        await self._set_post(post_id, status="pending", error_message=error[:500], next_attempt_at=retry_at)

    async def mark_post_failed(self, post_id, error):
        await self._set_post(post_id, status="failed", error_message=error[:500])

    async def mark_post_unknown(self, post_id, error):
        await self._set_post(post_id, status="unknown", error_message=error[:500])

    async def requeue_posts(self, statuses: list[str]) -> int:
        async with AsyncSessionLocal() as db:
            res = await db.execute(update(MediaPost).where(MediaPost.status.in_(statuses)).values(
                status="pending", attempts=0, next_attempt_at=None))
            await db.commit()
            return res.rowcount or 0

    async def get_post_by_token(self, token: str) -> Optional[MediaPost]:
        async with AsyncSessionLocal() as db:
            return (await db.execute(select(MediaPost).where(MediaPost.token == token))).scalar_one_or_none()

    async def list_posts(self, statuses: list[str], limit: int = 10) -> list[MediaPost]:
        async with AsyncSessionLocal() as db:
            return list((await db.execute(select(MediaPost).where(MediaPost.status.in_(statuses))
                                          .order_by(MediaPost.id.desc()).limit(limit))).scalars().all())

    async def post_counts(self) -> dict:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(MediaPost.status, func.count()).group_by(MediaPost.status))).all()
        return {s: n for s, n in rows}

    async def delivery_counts(self) -> dict:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(VideoDelivery.status, func.count())
                                     .group_by(VideoDelivery.status))).all()
        return {s: n for s, n in rows}

    async def reconcile_startup(self) -> tuple[int, int]:
        """کارهای نیمه‌تمامِ اجرای قبل: نتیجه‌شان معلوم نیست ← «unknown» (خودکار دوباره فرستاده نمی‌شوند)."""
        async with AsyncSessionLocal() as db:
            p = await db.execute(update(MediaPost).where(MediaPost.status == "publishing").values(
                status="unknown", error_message="interrupted by restart; check the channel before retry"))
            d = await db.execute(update(VideoDelivery).where(VideoDelivery.status == "sending").values(
                status="unknown", error="interrupted by restart"))
            await db.commit()
            return p.rowcount or 0, d.rowcount or 0

    # ---------- تحویل به کاربران ----------
    async def recent_delivery_exists(self, user_id: int, media_post_id: int, seconds: int) -> bool:
        since = utcnow() - timedelta(seconds=seconds)
        async with AsyncSessionLocal() as db:
            n = (await db.execute(select(func.count()).select_from(VideoDelivery).where(
                VideoDelivery.user_id == user_id, VideoDelivery.media_post_id == media_post_id,
                VideoDelivery.status.in_(["sending", "sent"]), VideoDelivery.created_at >= since))).scalar_one()
        return n > 0

    async def create_delivery(self, media_post_id: int, bot_pk: int, user_id: int) -> int:
        async with AsyncSessionLocal() as db:
            row = VideoDelivery(media_post_id=media_post_id, bot_id=bot_pk, user_id=user_id, status="sending")
            db.add(row)
            await db.commit()
            return row.id

    async def _set_delivery(self, delivery_id: int, **values):
        async with AsyncSessionLocal() as db:
            await db.execute(update(VideoDelivery).where(VideoDelivery.id == delivery_id).values(**values))
            await db.commit()

    async def mark_delivery(self, delivery_id: int, status: str, error: Optional[str] = None):
        await self._set_delivery(delivery_id, status=status, error=error)

    async def mark_delivery_sent(self, delivery_id: int, video_msg_id: int, delete_at: datetime, sent_at: datetime):
        await self._set_delivery(delivery_id, status="sent", video_msg_id=video_msg_id,
                                 delete_at=delete_at, sent_at=sent_at)

    async def set_guide(self, delivery_id: int, guide_msg_id: int):
        await self._set_delivery(delivery_id, guide_msg_id=guide_msg_id)

    async def has_guide(self, user_id: int, media_post_id: int) -> bool:
        async with AsyncSessionLocal() as db:
            n = (await db.execute(select(func.count()).select_from(VideoDelivery).where(
                VideoDelivery.user_id == user_id, VideoDelivery.media_post_id == media_post_id,
                VideoDelivery.guide_msg_id.is_not(None)))).scalar_one()
        return n > 0

    async def due_deletions(self, now: datetime, limit: int) -> list[VideoDelivery]:
        async with AsyncSessionLocal() as db:
            return list((await db.execute(select(VideoDelivery).where(
                VideoDelivery.status == "sent", VideoDelivery.delete_at <= now)
                .order_by(VideoDelivery.delete_at).limit(limit))).scalars().all())

    async def mark_deleted(self, delivery_id: int, now: datetime):
        await self._set_delivery(delivery_id, status="deleted", deleted_at=now)

    async def reschedule_delete(self, delivery_id: int, attempts: int, next_at: datetime):
        await self._set_delivery(delivery_id, delete_attempts=attempts, delete_at=next_at)

    async def mark_delete_failed(self, delivery_id: int, error: str):
        await self._set_delivery(delivery_id, status="delete_failed", error=error[:255])
