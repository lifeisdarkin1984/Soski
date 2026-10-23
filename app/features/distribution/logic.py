"""منطق خالصِ توزیع (بدون تلگرام و دیتابیس)؛ کاملاً قابل تست.

قالب کپشن (نحو):
  {caption}  = کپشن تمیزشده‌ی پست اصلی
  {promo}    = متن تبلیغاتی تنظیم‌شده در ربات مدیریت
  فقط همین دو متغیر مجازند؛ هر { یا } دیگر خطاست.
"""
import re
import secrets
from collections import namedtuple
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

CAPTION_LIMIT = 1024
PAYLOAD_PREFIX = "v_"
_TOKEN_CORE = r"[A-Za-z0-9_-]{16}"
PAYLOAD_RE = re.compile(rf"^{PAYLOAD_PREFIX}({_TOKEN_CORE})$")
TOKEN_RE = re.compile(rf"^{_TOKEN_CORE}$")

ALLOWED_PLACEHOLDERS = ("caption", "promo")
DEFAULT_TPL_CAPTION = "{caption}\n\n{promo}"
DEFAULT_TPL_BUTTON = "📥 دریافت ویدیو"
DEFAULT_DELETE_MINUTES = 60
MIN_DELETE_MINUTES = 1
MAX_DELETE_MINUTES = 47 * 60      # تلگرام حذف پیام‌های قدیمی‌تر از ۴۸ ساعت را قبول نمی‌کند
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


def validate_template(tpl: str) -> tuple[bool, str]:
    if not (tpl or "").strip():
        return False, "قالب خالی است."
    tokens = re.findall(r"\{([^{}]*)\}", tpl)
    bad = [t for t in tokens if t not in ALLOWED_PLACEHOLDERS]
    if bad:
        return False, "متغیر نامعتبر: " + ", ".join("{" + b + "}" for b in bad) + "\nفقط {caption} و {promo} مجازند."
    leftover = re.sub(r"\{[^{}]*\}", "", tpl)
    if "{" in leftover or "}" in leftover:
        return False, "آکولاد ناقص است؛ فقط {caption} و {promo} مجازند."
    return True, ""


def validate_button_text(text: str) -> tuple[bool, str]:
    t = (text or "").strip()
    if not t:
        return False, "متن دکمه خالی است."
    if len(t) > MAX_BUTTON_TEXT:
        return False, f"متن دکمه حداکثر {MAX_BUTTON_TEXT} نویسه باشد."
    return True, ""


def _render(template: str, caption: str, promo: str) -> str:
    values = {"caption": caption, "promo": promo}
    return re.sub(r"\{(caption|promo)\}", lambda m: values[m.group(1)], template)   # یک‌مرحله‌ای؛ جایگزینی تو در تو ندارد


def compose_caption(template: str, caption: Optional[str], promo: Optional[str],
                    limit: int = CAPTION_LIMIT) -> tuple[str, bool]:
    """(کپشن نهایی، آیا کپشن اصلی کوتاه شد). اگر قالب به‌تنهایی از حد بیشتر باشد TemplateTooLong."""
    caption, promo = caption or "", promo or ""
    full = _render(template, caption, promo)
    if u16len(full) <= limit:
        return full, False
    n = template.count("{caption}")
    fixed = _render(template, "", promo)
    if n == 0 or u16len(fixed) >= limit:
        raise TemplateTooLong("قالب و متن تبلیغاتی به‌تنهایی از حد مجاز کپشن (۱۰۲۴) بلندترند.")
    budget = (limit - u16len(fixed)) // n
    if budget < 10:
        raise TemplateTooLong("برای کپشن اصلی جای کافی باقی نمی‌ماند؛ قالب یا متن تبلیغاتی را کوتاه کنید.")
    cut = trim_u16(caption, budget - 1).rstrip() + "…"
    return _render(template, cut, promo), True


# ---------- زمان حذف ----------
def parse_delete_minutes(text: Any) -> int:
    t = str(text).strip()
    if not t.isdigit():
        raise ValueError("فقط عدد (دقیقه) بفرستید.")
    v = int(t)
    if not (MIN_DELETE_MINUTES <= v <= MAX_DELETE_MINUTES):
        raise ValueError(f"عدد باید بین {MIN_DELETE_MINUTES} تا {MAX_DELETE_MINUTES} دقیقه (کمتر از ۴۸ ساعت) باشد.")
    return v


def format_duration(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    if h == 0:
        return f"{m} دقیقه"
    if m == 0:
        return f"{h} ساعت"
    return f"{h} ساعت و {m} دقیقه"


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
