"""آزمایش واقعیِ دسترسی ربات‌ها (فقط با Bot API؛ هیچ چیزی را تغییر نمی‌دهد)."""
import logging
from collections import namedtuple
from typing import Optional, Sequence

from app.features.distribution.botapi import BotApi, BotApiError, BotApiNetworkError
from app.features.distribution.logic import bot_can_post, bot_is_admin

log = logging.getLogger("Collector.distribution")
Check = namedtuple("Check", "label ok detail")


async def _member(api: BotApi, chat_id: int, bot_tg_id: int):
    try:
        return await api.get_chat_member(chat_id, bot_tg_id), None
    except BotApiError as e:
        return None, f"{e.code} {e.description}"
    except BotApiNetworkError:
        return None, "عدم اتصال به تلگرام"


async def check_bot(api: BotApi, bot_tg_id: int, storage_chat: Optional[int], final_chat: Optional[int],
                    forced: Sequence) -> list[Check]:
    checks: list[Check] = []
    try:
        me = await api.get_me()
        checks.append(Check("توکن ربات", True, f"@{me.get('username')}"))
    except BotApiError as e:
        return [Check("توکن ربات", False, f"{e.code} {e.description}")]
    except BotApiNetworkError:
        return [Check("اتصال به تلگرام", False, "قطع")]

    if storage_chat is None:
        checks.append(Check("مخزن", False, "مخزن (مقصد) هنوز تنظیم نشده"))
    else:
        m, err = await _member(api, storage_chat, bot_tg_id)
        ok = bool(m) and bot_is_admin(m)
        checks.append(Check("دسترسی به مخزن", ok, "ادمین است" if ok else (err or "ربات باید در کانال مخزن ادمین باشد")))

    if final_chat is not None:
        m, err = await _member(api, final_chat, bot_tg_id)
        chat_type = "channel"
        try:
            chat_type = (await api.get_chat(final_chat)).get("type", "channel")
        except (BotApiError, BotApiNetworkError):
            pass
        if chat_type == "channel":                # کانال: ادمین با اجازه‌ی ارسال پست
            ok = bool(m) and bot_can_post(m)
            hint = "ربات باید ادمین با اجازه‌ی «ارسال پست» باشد"
        else:                                     # گروه: عضو یا ادمین بودن کافی است
            ok = bool(m) and m.get("status") in ("member", "administrator", "creator")
            hint = "ربات باید عضو گروه باشد"
        checks.append(Check("ارسال در کانال نهایی", ok, "اجازه‌ی ارسال دارد" if ok else (err or hint)))

    for ch in forced:
        m, err = await _member(api, ch.chat_id, bot_tg_id)
        ok = bool(m) and bot_is_admin(m)
        checks.append(Check(f"بررسی عضویت «{ch.title or ch.chat_id}»", ok,
                            "ادمین است" if ok else (err or "ربات باید ادمین باشد تا عضویت کاربران را ببیند")))
    return checks
