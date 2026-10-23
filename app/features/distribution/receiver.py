"""ربات دریافت: لینک اختصاصی ← بررسی عضویت اجباری ← ارسال خصوصی ← حذف زمان‌دار ← دریافت مجدد.
فقط منطق؛ دیتابیس از طریق `store` و تلگرام از طریق `api` تزریق می‌شود (برای تست)."""
import asyncio
import logging
from datetime import timedelta
from typing import Awaitable, Callable, Optional

from app.features.distribution.botapi import BotApi, BotApiError, BotApiNetworkError
from app.features.distribution.logic import (
    DEFAULT_DELETE_MINUTES, TOKEN_RE, evaluate_membership, format_duration, interpret_member_status,
    parse_payload, parse_start_command, utcnow,
)

log = logging.getLogger("Collector.distribution")

T_WELCOME = "سلام 👋\nبرای دریافت ویدیو، دکمه‌ی «دریافت» زیر پست‌های کانال را بزنید."
T_INVALID = "این لینک معتبر نیست یا منقضی شده است."
T_NEED_JOIN = "برای دریافت ویدیو ابتدا در کانال(های) زیر عضو شوید، سپس دکمه‌ی «عضو شدم» را بزنید:"
T_CHECK_FAILED = "الان امکان بررسی عضویت شما نیست. چند لحظه بعد دوباره دکمه‌ی زیر را بزنید."
T_SEND_FAILED = "ارسال ویدیو با خطا روبه‌رو شد. کمی بعد دوباره امتحان کنید."
T_SEND_UNKNOWN = "اگر ویدیو به دستتان نرسید، دوباره دکمه‌ی دریافت را بزنید."
T_BUSY = "در حال ارسال… لطفاً چند لحظه صبر کنید."
BTN_CHECK = "✅ عضو شدم، بررسی کن"
BTN_AGAIN = "🔄 دریافت مجدد"


def guide_text(minutes: int) -> str:
    return (f"⏳ این ویدیو بعد از {format_duration(minutes)} از این چت حذف می‌شود.\n"
            "بعد از حذف، با دکمه‌ی زیر دوباره دریافتش کنید.")


class ReceiverService:
    def __init__(self, api: BotApi, bot_pk: int, store, now_fn: Callable = utcnow, concurrency: int = 20):
        self.api = api
        self.bot_pk = bot_pk
        self.store = store
        self.now = now_fn
        self._sem = asyncio.Semaphore(concurrency)
        self._locks: dict[tuple, asyncio.Lock] = {}

    # ---------- ورودی ----------
    async def handle_update(self, update: dict):
        async with self._sem:
            if "message" in update:
                await self._on_message(update["message"])
            elif "callback_query" in update:
                await self._on_callback(update["callback_query"])

    async def _on_message(self, msg: dict):
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        if chat.get("type") != "private" or not sender.get("id") or sender.get("is_bot"):
            return
        payload = parse_start_command(msg.get("text"))
        if payload is None or payload == "":
            await self._say(chat["id"], T_WELCOME)
            return
        token = parse_payload(payload)
        if not token:
            await self._say(chat["id"], T_INVALID)
            return
        await self._request(sender["id"], chat["id"], token, None)

    async def _on_callback(self, cb: dict):
        cb_id = cb.get("id")
        kind, _, token = (cb.get("data") or "").partition(":")
        chat = ((cb.get("message") or {}).get("chat")) or {}
        user_id = (cb.get("from") or {}).get("id")
        if kind not in ("chk", "re") or not TOKEN_RE.match(token) or chat.get("type") != "private" or not user_id:
            await self._answer(cb_id)
            return
        await self._request(user_id, chat["id"], token, cb_id)

    # ---------- درخواست ----------
    async def _request(self, user_id: int, chat_id: int, token: str, cb_id: Optional[str]):
        key = (user_id, token)
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked():                      # کلیک تکراریِ هم‌زمان
            await self._answer(cb_id, T_BUSY)
            return
        try:
            async with lock:
                await self._request_locked(user_id, chat_id, token, cb_id)
        finally:
            self._locks.pop(key, None)

    async def _request_locked(self, user_id: int, chat_id: int, token: str, cb_id: Optional[str]):
        post = await self.store.get_post_by_token(token)
        # فقط ربات اختصاص‌یافته به همین پست، و فقط پستِ واقعاً منتشرشده
        if post is None or post.status != "published" or post.assigned_bot_id != self.bot_pk:
            await self._answer(cb_id, T_INVALID, alert=True)
            await self._say(chat_id, T_INVALID)
            return
        await self.store.bump_bot(self.bot_pk, requests=1)

        forced = await self.store.list_forced(only_active=True)
        results = await asyncio.gather(*[self._is_member(ch.chat_id, user_id) for ch in forced])
        verdict = evaluate_membership(list(zip(forced, results)))

        if verdict.status == "missing":
            rows = []
            for ch in verdict.missing:
                if getattr(ch, "link", None):
                    rows.append([{"text": f"📢 {ch.title or 'عضویت در کانال'}", "url": ch.link}])
            rows.append([{"text": BTN_CHECK, "callback_data": f"chk:{token}"}])
            names = "\n".join(f"• {ch.title or ch.chat_id}" for ch in verdict.missing)
            await self._answer(cb_id, "هنوز عضو همه‌ی کانال‌ها نیستید", alert=bool(cb_id))
            await self._say(chat_id, f"{T_NEED_JOIN}\n{names}", {"inline_keyboard": rows})
            return
        if verdict.status == "unknown":
            await self._answer(cb_id, "بررسی عضویت ممکن نشد", alert=bool(cb_id))
            await self._say(chat_id, T_CHECK_FAILED,
                            {"inline_keyboard": [[{"text": BTN_CHECK, "callback_data": f"chk:{token}"}]]})
            return
        await self._deliver(user_id, chat_id, post, cb_id)

    async def _is_member(self, chat_id: int, user_id: int) -> Optional[bool]:
        try:
            member = await self.api.get_chat_member(chat_id, user_id)
            return interpret_member_status(member)
        except (BotApiError, BotApiNetworkError) as e:
            detail = f"{e.code} {e.description}" if isinstance(e, BotApiError) else type(e).__name__
            log.warning(f"Membership check failed for channel {chat_id}: {detail}")
            await self.store.bump_bot(self.bot_pk, errors=1)
            return None

    # ---------- ارسال ----------
    async def _delete_minutes(self) -> int:
        raw = await self.store.get_setting("delete_after_min")
        try:
            v = int(raw)
            return v if 1 <= v <= 47 * 60 else DEFAULT_DELETE_MINUTES
        except (TypeError, ValueError):
            return DEFAULT_DELETE_MINUTES

    async def _deliver(self, user_id: int, chat_id: int, post, cb_id: Optional[str]):
        if await self.store.recent_delivery_exists(user_id, post.id, 3):
            await self._answer(cb_id, T_BUSY)
            return
        delivery_id = await self.store.create_delivery(post.id, self.bot_pk, user_id)
        try:
            res = await self.api.copy_message(chat_id, post.storage_chat_id, post.storage_msg_id,
                                              caption=post.final_caption)
        except BotApiNetworkError:
            # شاید ارسال شده باشد؛ خودکار دوباره نمی‌فرستیم
            await self.store.mark_delivery(delivery_id, "unknown", "network error; result unknown")
            await self.store.bump_bot(self.bot_pk, errors=1)
            log.error(f"Send to user {user_id} has unknown result (network)")
            await self._answer(cb_id)
            await self._say(chat_id, T_SEND_UNKNOWN,
                            {"inline_keyboard": [[{"text": BTN_AGAIN, "callback_data": f"re:{post.token}"}]]})
            return
        except BotApiError as e:
            await self.store.mark_delivery(delivery_id, "send_failed", f"{e.code}: {e.description}"[:255])
            if e.code != 403:    # 403 = کاربر ربات را بلاک کرده؛ خطای سیستم نیست
                await self.store.bump_bot(self.bot_pk, errors=1)
                log.error(f"copyMessage failed for post {post.id}: {e.code} {e.description}")
                await self._say(chat_id, T_SEND_FAILED)
            await self._answer(cb_id)
            return

        minutes = await self._delete_minutes()
        now = self.now()
        await self.store.mark_delivery_sent(delivery_id, int(res["message_id"]), now + timedelta(minutes=minutes), now)
        if not await self.store.has_guide(user_id, post.id):
            guide = await self._say(chat_id, guide_text(minutes),
                                    {"inline_keyboard": [[{"text": BTN_AGAIN, "callback_data": f"re:{post.token}"}]]})
            if guide:
                await self.store.set_guide(delivery_id, int(guide["message_id"]))
        await self._answer(cb_id)

    # ---------- ابزار ----------
    async def _say(self, chat_id: int, text: str, markup: Optional[dict] = None) -> Optional[dict]:
        try:
            return await self.api.send_message(chat_id, text, reply_markup=markup)
        except (BotApiError, BotApiNetworkError) as e:
            log.warning(f"sendMessage failed: {type(e).__name__}")
            return None

    async def _answer(self, cb_id: Optional[str], text: Optional[str] = None, alert: bool = False):
        if not cb_id:
            return
        try:
            await self.api.answer_callback_query(cb_id, text, alert)
        except (BotApiError, BotApiNetworkError):
            pass


class DeleteScheduler:
    """حذف زمان‌دار پیام‌های ویدیو؛ زمان هر پیام در دیتابیس است و بعد از ری‌استارت ادامه پیدا می‌کند."""
    MAX_ATTEMPTS = 6

    def __init__(self, store, api_for: Callable[[int], Awaitable[BotApi]], now_fn: Callable = utcnow):
        self.store = store
        self.api_for = api_for
        self.now = now_fn

    async def run_once(self) -> int:
        due = await self.store.due_deletions(self.now(), 50)
        for d in due:
            try:
                await self._delete_one(d)
            except Exception as e:
                log.error(f"delete job {d.id} crashed: {type(e).__name__}: {e}")
        return len(due)

    async def _retry_or_fail(self, d, reason: str):
        attempts = d.delete_attempts + 1
        if attempts >= self.MAX_ATTEMPTS:
            await self.store.mark_delete_failed(d.id, reason[:255])
            log.error(f"Delete of delivery {d.id} gave up: {reason}")
            return
        delay = min(60 * (2 ** (attempts - 1)), 900)
        await self.store.reschedule_delete(d.id, attempts, self.now() + timedelta(seconds=delay))

    async def _delete_one(self, d):
        try:
            api = await self.api_for(d.bot_id)
            await api.delete_message(d.user_id, d.video_msg_id)
        except BotApiError as e:
            desc = e.description.lower()
            if "message to delete not found" in desc:
                await self.store.mark_deleted(d.id, self.now())            # قبلاً پاک شده
            elif "can't be deleted" in desc or e.code in (401, 403):
                await self.store.mark_delete_failed(d.id, f"{e.code}: {e.description}"[:255])
                log.warning(f"Delivery {d.id} cannot be deleted: {e.code} {e.description}")
            else:
                await self._retry_or_fail(d, f"{e.code}: {e.description}")
        except (BotApiNetworkError, Exception) as e:
            await self._retry_or_fail(d, f"{type(e).__name__}")
        else:
            await self.store.mark_deleted(d.id, self.now())
