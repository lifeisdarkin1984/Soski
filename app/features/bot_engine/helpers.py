"""منطق خالص (بدون وابستگی به تلگرام/دیتابیس) برای موتور تعامل با ربات؛ قابل تست مستقل."""
import re
from typing import Optional, Sequence, Tuple, Any, List, Union
from app.core.link_parser import link_parser

POSITIVE = [
    'دریافت', 'دانلود', 'فایل', 'ویدیو', 'فیلم', 'بررسی', 'تایید', 'تأیید', 'ادامه', 'عضو', 'شدم',
    'ارسال', 'get', 'download', 'file', 'video', 'check', 'verify', 'continue', 'joined', 'join',
    'confirm', 'send', 'start', 'ok', 'done',
]
NEGATIVE = [
    'لغو', 'انصراف', 'بازگشت', 'برگشت', 'پشتیبانی', 'راهنما', 'تبلیغ', 'خرید', 'کانال ما',
    'cancel', 'back', 'support', 'help', 'buy', 'premium', 'vip', 'ads',
]
MEDIA_ATTRS = ('video', 'document', 'audio', 'animation', 'voice', 'photo')


def _text(btn: Any) -> str:
    if isinstance(btn, str):
        return btn.lower()
    return (getattr(btn, 'text', '') or '').lower()


def choose_button(buttons: Sequence[Any]) -> Optional[Any]:
    """دکمه‌ی مناسب را انتخاب می‌کند؛ اگر مطمئن نباشد None (به‌جای کلیک تصادفی)."""
    candidates = [b for b in buttons if _text(b) and not any(n in _text(b) for n in NEGATIVE)]
    for btn in candidates:
        if any(p in _text(btn) for p in POSITIVE):
            return btn
    if len(candidates) == 1:
        return candidates[0]
    return None


def get_media(message: Any) -> Optional[Tuple[str, Any]]:
    for attr in MEDIA_ATTRS:
        obj = getattr(message, attr, None)
        if obj:
            return attr, obj
    return None


def pick_bot_message(history: Sequence[Any], baseline_id: int) -> Tuple[Optional[Any], int]:
    """
    history: پیام‌های چت با ربات، جدیدترین اول.
    فقط پیام‌های ربات که بعد از baseline_id آمده‌اند. اولویت: رسانه، سپس پیام دارای دکمه، سپس جدیدترین.
    برمی‌گرداند (پیام انتخابی، بزرگ‌ترین id جدید)
    """
    new = [m for m in history if m.id > baseline_id and not getattr(m, 'outgoing', False)]
    if not new:
        return None, baseline_id
    newest = max(m.id for m in new)
    for m in new:
        if get_media(m):
            return m, newest
    for m in new:
        if getattr(m, 'reply_markup', None):
            return m, newest
    return new[0], newest


def join_target(parsed: dict) -> Optional[str]:
    """چیزی که باید به join_chat داده شود. لینک دعوت خصوصی باید به‌صورت لینک کامل باشد."""
    if parsed['type'] == 'private_channel':
        return f"https://t.me/+{parsed['invite_hash']}"
    if parsed['type'] == 'channel':
        return parsed['identifier']
    return None


def build_start_text(start_param: Optional[str]) -> str:
    return f"/start {start_param}" if start_param else "/start"


def reply_keyboard_texts(markup: Any) -> List[str]:
    rows = getattr(markup, 'keyboard', None) or []
    texts = []
    for row in rows:
        for btn in row:
            texts.append(btn if isinstance(btn, str) else getattr(btn, 'text', ''))
    return [t for t in texts if t]


def parse_destination_input(text: Optional[str]) -> Union[int, str, None]:
    """ورودی مدیر (آیدی عددی، @یوزرنیم، لینک) را به چیزی تبدیل می‌کند که get_chat بفهمد."""
    t = (text or "").strip()
    if not t:
        return None
    if re.fullmatch(r"-?\d{5,}", t):
        n = int(t)
        return int("-100" + str(n)) if n > 0 else n   # آیدی خام کانال → قالب تلگرام
    if t.startswith("@") and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", t[1:]):
        return t[1:]
    parsed = link_parser.parse_telegram_link(t)
    if parsed:
        if parsed["type"] == "private_channel":
            return f"https://t.me/+{parsed['invite_hash']}"
        if parsed["type"] == "channel":
            return parsed["username"]
        return None
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", t):
        return t
    return None
