import asyncio
from typing import Optional, Tuple, Set
from pyrogram import Client
from pyrogram.types import Message
from pyrogram.errors import FloodWait, UserIsBlocked, UserAlreadyParticipant, InviteHashExpired, InviteHashInvalid
from sqlalchemy import select
from app.core.logger import logger
from app.core.database import AsyncSessionLocal
from app.core.link_parser import link_parser
from app.repositories.flow_repository import FlowRepository
from app.models.message import ProcessedMessage, FlowStatus
from app.features.bot_engine.helpers import (
    choose_button, get_media, pick_bot_message, join_target, build_start_text, reply_keyboard_texts,
)

MAX_INTERACTION_STEPS = 8
MAX_JOIN_RETRIES_PER_CHANNEL = 2
MAX_JOIN_FAILURES = 3
RESPONSE_TIMEOUT = 15.0     # حداکثر انتظار برای پاسخ ربات در هر مرحله
POLL_INTERVAL = 1.5
MAX_FLOOD_SLEEP = 300


class FlowAbort(Exception):
    """شکست قطعی؛ پیام به‌عنوان failed علامت می‌خورد."""


async def _flood_safe(factory, attempts: int = 3):
    """اجرای یک درخواست تلگرام و رعایت FloodWait."""
    for i in range(attempts):
        try:
            return await factory()
        except FloodWait as e:
            wait = min(int(e.value) + 1, MAX_FLOOD_SLEEP)
            logger.warning(f"FloodWait: sleeping {wait}s")
            await asyncio.sleep(wait)
            if i == attempts - 1:
                raise
    return None


class BotFlowEngine:
    @staticmethod
    async def process_request(client: Client, processed_msg_id: int, bot_username: str, account_id: int,
                              start_param: Optional[str] = None) -> Tuple[Optional[Message], int]:
        async with AsyncSessionLocal() as db:
            flow_repo = FlowRepository(db)
            p_msg = (await db.execute(
                select(ProcessedMessage).where(ProcessedMessage.id == processed_msg_id))).scalar_one_or_none()
            if not p_msg:
                return None, 0
            start_param = start_param or p_msg.start_param
            start_text = build_start_text(start_param)
            await flow_repo.mark_message_processing(processed_msg_id)

            joined_count = 0
            joined_in_session: Set[str] = set()
            join_failures = 0
            try:
                # هر بار از اول شروع می‌کنیم (حتی بعد از ری‌استارت) تا پیام‌های قدیمی ربات با پاسخ جدید قاطی نشوند
                sent = await _flood_safe(lambda: client.send_message(bot_username, start_text))
                baseline = sent.id
                await flow_repo.update_flow_status(processed_msg_id, FlowStatus.interacting)

                for step in range(MAX_INTERACTION_STEPS):
                    bot_msg, baseline = await BotFlowEngine._wait_for_bot_message(client, bot_username, baseline)
                    if bot_msg is None:
                        raise FlowAbort("Bot did not respond in time")

                    if get_media(bot_msg):
                        return bot_msg, joined_count

                    markup = bot_msg.reply_markup
                    inline_rows = getattr(markup, 'inline_keyboard', None) or []
                    url_buttons = [b for row in inline_rows for b in row if getattr(b, 'url', None)]
                    cb_buttons = [b for row in inline_rows for b in row if getattr(b, 'callback_data', None)]

                    # --- عضویت اجباری ---
                    joined_now = 0
                    for btn in url_buttons:
                        parsed = link_parser.parse_telegram_link(btn.url)
                        target = join_target(parsed) if parsed else None
                        if not target or target in joined_in_session:
                            continue
                        ok, newly = await BotFlowEngine._join(client, flow_repo, processed_msg_id, target, parsed)
                        if ok:
                            joined_in_session.add(target)
                            joined_now += 1
                            joined_count += 1 if newly else 0
                        else:
                            join_failures += 1
                            if join_failures >= MAX_JOIN_FAILURES:
                                raise FlowAbort("Too many join failures")

                    if joined_now:
                        await flow_repo.update_flow_status(processed_msg_id, FlowStatus.joined_forced)
                        await asyncio.sleep(2)
                        verify = choose_button(cb_buttons)
                        if verify is not None:
                            await BotFlowEngine._click(client, bot_msg, verify)
                        else:
                            sent = await _flood_safe(lambda: client.send_message(bot_username, start_text))
                            baseline = max(baseline, sent.id)
                        continue

                    # --- دکمه‌ی شیشه‌ای (callback) ---
                    target_btn = choose_button(cb_buttons)
                    if target_btn is not None:
                        await BotFlowEngine._click(client, bot_msg, target_btn)
                        continue

                    # --- کیبورد معمولی زیر چت ---
                    texts = reply_keyboard_texts(markup)
                    chosen = choose_button(texts) if texts else None
                    if chosen is not None:
                        sent = await _flood_safe(lambda: client.send_message(bot_username, chosen))
                        baseline = max(baseline, sent.id)
                        continue
                    # چیزی برای انجام نیست؛ شاید ربات پیام بعدی را بفرستد
                raise FlowAbort("Max interaction steps reached")
            except FlowAbort as e:
                await flow_repo.mark_message_failed(processed_msg_id, str(e))
                return None, 0
            except UserIsBlocked as e:
                await flow_repo.mark_message_failed(processed_msg_id, f"Blocked by bot: {e}")
                return None, 0
            except Exception as e:
                logger.error(f"Flow error for @{bot_username}: {e}", exc_info=True)
                await flow_repo.mark_message_failed(processed_msg_id, f"{type(e).__name__}: {e}")
                return None, 0

    @staticmethod
    async def _wait_for_bot_message(client: Client, bot_username: str, baseline: int):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RESPONSE_TIMEOUT
        while True:
            try:
                history = [m async for m in client.get_chat_history(bot_username, limit=8)]
            except FloodWait as e:
                await asyncio.sleep(min(int(e.value) + 1, MAX_FLOOD_SLEEP))
                history = []
            msg, newest = pick_bot_message(history, baseline)
            if msg is not None:
                # اگر ربات هنوز در حال فرستادن پیام‌های پشت‌سرهم است، کمی صبر کن تا پیام کامل‌تر را بگیریم
                await asyncio.sleep(1.0)
                try:
                    history = [m async for m in client.get_chat_history(bot_username, limit=8)]
                    msg, newest = pick_bot_message(history, baseline)
                except Exception:
                    pass
                return msg, newest
            if loop.time() >= deadline:
                return None, baseline
            await asyncio.sleep(POLL_INTERVAL)

    @staticmethod
    async def _click(client: Client, bot_msg: Message, button) -> None:
        try:
            await _flood_safe(lambda: client.request_callback_answer(
                chat_id=bot_msg.chat.id, message_id=bot_msg.id, callback_data=button.callback_data))
        except FloodWait:
            raise
        except Exception as e:
            # ربات ممکن است به کلیک جواب ندهد (timeout)؛ ادامه می‌دهیم و پیام بعدی را منتظر می‌مانیم
            logger.info(f"Callback click gave no answer: {type(e).__name__}")

    @staticmethod
    async def _join(client: Client, flow_repo: FlowRepository, processed_msg_id: int, target: str, parsed: dict):
        """(موفق؟، آیا همین الان عضو شد؟)"""
        for attempt in range(MAX_JOIN_RETRIES_PER_CHANNEL):
            try:
                chat = await client.join_chat(target)
                await flow_repo.add_joined_channel(
                    processed_msg_id, getattr(chat, 'id', 0) or 0,
                    getattr(chat, 'username', None) or parsed.get('username') or parsed.get('invite_hash'))
                return True, True
            except UserAlreadyParticipant:
                return True, False
            except (InviteHashExpired, InviteHashInvalid):
                logger.warning(f"Invite link invalid/expired: {target}")
                return False, False
            except FloodWait as e:
                await asyncio.sleep(min(int(e.value) + 1, MAX_FLOOD_SLEEP))
            except Exception as e:
                logger.warning(f"Join failed ({type(e).__name__}) for {target}")
                if type(e).__name__ == "InviteRequestSent":
                    return False, False   # نیاز به تأیید مدیر کانال؛ خودکار حل نمی‌شود
                await asyncio.sleep(1)
        return False, False


bot_flow_engine = BotFlowEngine()
