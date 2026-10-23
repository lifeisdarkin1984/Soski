"""منطق خالصِ توزیع (بدون تلگرام و دیتابیس)؛ کاملاً قابل تست.

کپشن نهایی = همان کپشنِ ذخیره‌شده در مخزن + (دو خط فاصله) + «متن انتهای کپشن» که از پنل تنظیم می‌شود.
"""
import json
import re
import secrets
import time
from collections import namedtuple
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

CAPTION_LIMIT = 1024
PAYLOAD_PREFIX = "v_"
_TOKEN_CORE = r"[A-Za-z0-9_-]{16}"
PAYLOAD_RE = re.compile(rf"^{PAYLOAD_PREFIX}({_TOKEN_CORE})$")
TOKEN_RE = re.compile(rf"^{_TOKEN_CORE}$")

DEFAULT_TPL_BUTTON = "📥 دریافت ویدیو"
DEFAULT_POST_TEXT = "🎬 ویدیوی جدید"       # اگر نه کپشن باشد نه متن انتهایی (پیام متنیِ خالی ممکن نیست)
POST_TEXT_LIMIT = 4096                     # پیام متنی کانال نهایی
VIDEO_CAPTION_LIMIT = 1024                 # کپشن ویدیو داخل ربات
MAX_SUFFIX_LEN = 900
DEFAULT_DELETE_SECONDS = 3600
MIN_DELETE_SECONDS = 5
MAX_DELETE_SECONDS = 47 * 3600             # تلگرام حذف پیام‌های قدیمی‌تر از ۴۸ ساعت را قبول نمی‌کند
MAX_BUTTON_TEXT = 40


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------- لینک اختصاصی ----------
def make_token() -> str:
    """۱۶ نویسه‌ی تصادفی و غیرقابل حدس (۹۶ بیت)."""
    return secrets.token_urlsafe(12)


def build_payload(token: str) -> str:
    if not TOKEN_RE.match(token or ""):
        raise ValueError("invalid token")
    return PAYLOAD_PREFIX + token


def parse_payload(payload: Optional[str]) -> Optional[str]:
    """payload ورودی /start را اعتبارسنجی می‌کند؛ توکن یا None."""
    m = PAYLOAD_RE.match((payload or "").strip())
    return m.group(1) if m else None


def build_deep_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start={build_payload(token)}"


def parse_start_command(text: Optional[str]) -> Optional[str]:
    """None اگر پیام دستور /start نیست؛ وگرنه payload (ممکن است خالی باشد)."""
    if not text:
        return None
    parts = text.strip().split(maxsplit=1)
    if not parts or parts[0].split("@")[0].lower() != "/start":
        return None
    return parts[1].strip() if len(parts) > 1 else ""


# ---------- کپشن ----------
class TemplateTooLong(ValueError):
    pass


def u16len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def trim_u16(s: str, n: int) -> str:
    if n <= 0:
        return ""
    return s.encode("utf-16-le")[: n * 2].decode("utf-16-le", errors="ignore")


def validate_suffix(text: str) -> tuple[bool, str]:
    if u16len(text or "") > MAX_SUFFIX_LEN:
        return False, f"متن انتهایی حداکثر {MAX_SUFFIX_LEN} نویسه باشد."
    return True, ""


def validate_button_text(text: str) -> tuple[bool, str]:
    t = (text or "").strip()
    if not t:
        return False, "متن دکمه خالی است."
    if len(t) > MAX_BUTTON_TEXT:
        return False, f"متن دکمه حداکثر {MAX_BUTTON_TEXT} نویسه باشد."
    return True, ""


def compose_final(caption: Optional[str], suffix: Optional[str], limit: int) -> tuple[str, bool]:
    """(متن نهایی، آیا کپشن اصلی برای جاشدن کوتاه شد). متن انتهایی هیچ‌وقت بریده نمی‌شود."""
    caption, suffix = (caption or "").strip(), (suffix or "").strip()
    text = f"{caption}\n\n{suffix}" if caption and suffix else (caption or suffix)
    if u16len(text) <= limit:
        return text, False
    budget = limit - (u16len(suffix) + 2 if suffix else 0)
    if not caption or budget < 10:
        raise TemplateTooLong("متن انتهایی به‌تنهایی از حد مجاز بلندتر است؛ کوتاهش کنید.")
    cut = trim_u16(caption, budget - 1).rstrip() + "…"
    return (f"{cut}\n\n{suffix}" if suffix else cut), True


def parse_msg_ids(storage_msg_ids: Optional[str], first_id: int) -> list[int]:
    """شناسه‌ی پیام‌های ویدیو در مخزن (به ترتیب)."""
    try:
        ids = [int(i) for i in json.loads(storage_msg_ids or "[]")]
    except (ValueError, TypeError):
        ids = []
    return ids or [int(first_id)]


class FlagCache:
    """کلید خاموش/روشن با کش کوتاه (برای اینکه با هر پست به دیتابیس نرویم)."""
    def __init__(self, getter, ttl: float = 5.0, now=time.monotonic):
        self._getter, self._ttl, self._now = getter, ttl, now
        self._cache: dict[str, tuple[float, bool]] = {}

    async def get(self, key: str, default: bool = True) -> bool:
        hit = self._cache.get(key)
        if hit and self._now() - hit[0] < self._ttl:
            return hit[1]
        raw = await self._getter(key)
        value = default if raw is None else str(raw).strip().lower() not in ("0", "false", "off", "no")
        self._cache[key] = (self._now(), value)
        return value

    def invalidate(self, key: Optional[str] = None):
        self._cache.pop(key, None) if key else self._cache.clear()


# ---------- زمان حذف ----------
def parse_delete_seconds(text: Any) -> int:
    t = str(text).strip()
    if not t.isdigit():
        raise ValueError("فقط عدد (ثانیه) بفرستید.")
    v = int(t)
    if not (MIN_DELETE_SECONDS <= v <= MAX_DELETE_SECONDS):
        raise ValueError(f"عدد باید بین {MIN_DELETE_SECONDS} تا {MAX_DELETE_SECONDS} ثانیه (کمتر از ۴۸ ساعت) باشد.")
    return v


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} ساعت")
    if m:
        parts.append(f"{m} دقیقه")
    if sec or not parts:
        parts.append(f"{sec} ثانیه")
    return " و ".join(parts)


# ---------- عضویت ----------
MembershipResult = namedtuple("MembershipResult", "status missing unknown")   # status: ok | missing | unknown


def interpret_member_status(member: Optional[dict]) -> Optional[bool]:
    """True/False فقط وقتی تلگرام صریحاً گفته؛ در غیر این صورت None (نامشخص)."""
    if not isinstance(member, dict):
        return None
    st = member.get("status")
    if st in ("member", "administrator", "creator"):
        return True
    if st == "restricted":
        return bool(member.get("is_member", False))
    if st in ("left", "kicked"):
        return False
    return None


def evaluate_membership(results: Sequence[tuple[Any, Optional[bool]]]) -> MembershipResult:
    missing = [ch for ch, ok in results if ok is False]
    unknown = [ch for ch, ok in results if ok is None]
    if missing:
        return MembershipResult("missing", missing, unknown)
    if unknown:
        return MembershipResult("unknown", [], unknown)
    return MembershipResult("ok", [], [])


# ---------- تخصیص چرخشی ----------
def pick_next_bot(bots: Iterable[Any]) -> Optional[Any]:
    """از بین ربات‌های فعال، همانی که «قدیمی‌ترین» نوبتش را داشته (کمترین last_assigned_seq؛ تساوی با id)."""
    active = [b for b in bots if getattr(b, "is_active", False)]
    if not active:
        return None
    return min(active, key=lambda b: (b.last_assigned_seq, b.id))


# ---------- سلامت ربات ----------
def bot_is_admin(member: Optional[dict]) -> bool:
    return isinstance(member, dict) and member.get("status") in ("administrator", "creator")


def bot_can_post(member: Optional[dict]) -> bool:
    if not bot_is_admin(member):
        return False
    if member.get("status") == "creator":
        return True
    return member.get("can_post_messages", True) is not False
