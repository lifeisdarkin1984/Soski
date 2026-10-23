"""منوهای «توزیع و انتشار» در ربات مدیریت (همه با پیشوند v2: ؛ فقط برای ADMIN_IDS).
ربات‌های دریافت، کانال نهایی، عضویت اجباری، قالب کپشن، زمان حذف، آمار، خطاها و سلامت."""
import asyncio
import re

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery, Message

from app.admin.bot import admin_bot
from app.admin.handlers import admin_only, kb, _edit, login_states
from app.core.config import settings
from app.core.crypto import crypto
from app.core.logger import logger
from app.features.accounts.manager import worker_manager
from app.features.bot_engine.helpers import parse_destination_input, unwrap_chat
from app.features.distribution import logic
from app.features.distribution.botapi import BotApi, BotApiError, BotApiNetworkError
from app.features.distribution.health import check_bot
from app.features.distribution.runtime import distribution
from app.repositories.settings_repository import get_destination_id, get_destination_info

store = distribution.store
v2_states: dict[int, dict] = {}
TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")
HOME = "v2:home"


def _back(target: str = HOME):
    return kb([[("🔙 بازگشت", target)]])


async def _reply(message: Message, text: str, markup=None):
    await message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.DISABLED)


def _fmt_checks(checks) -> str:
    return "\n".join(f"{'✅' if c.ok else '❌'} {c.label}: {c.detail}" for c in checks)


async def _final_chat_id():
    raw = await store.get_setting("final_chat_id")
    return int(raw) if raw and raw.lstrip("-").isdigit() else None


async def _run_checks(bot) -> list:
    api = await distribution.api_for(bot.id)
    forced = await store.list_forced(only_active=True)
    checks = await check_bot(api, bot.bot_id, await get_destination_id(), await _final_chat_id(), forced)
    ok = all(c.ok for c in checks)
    bad = next((f"{c.label}: {c.detail}" for c in checks if not c.ok), None)
    await store.save_health(bot.id, ok, bad)
    return checks


async def _any_api():
    bots = await store.list_bots()
    if not bots:
        return None
    active = [b for b in bots if b.is_active]
    return await distribution.api_for((active or bots)[0].id)


# ---------- ورودی‌های متنی ----------
@admin_bot.on_message(filters.private & filters.command(["start", "menu", "cancel"]) & admin_only, group=-1)
async def v2_cancel(client: Client, message: Message):
    v2_states.pop(message.from_user.id, None)      # بدون stop_propagation؛ هندلر اصلی هم اجرا می‌شود


async def _resolve_chat(message: Message):
    """(chat_id, title, username, error). از فوروارد، آیدی عددی یا @یوزرنیم.
    آیدی عددی بدون getChat پذیرفته می‌شود؛ خطای واقعی بعداً از پیام آزمایشی دیده می‌شود."""
    chat = getattr(message, "forward_from_chat", None)
    origin = getattr(message, "forward_origin", None)
    if chat is None and origin is not None:
        chat = getattr(origin, "chat", None)
    if chat is not None:
        return chat.id, getattr(chat, "title", None), getattr(chat, "username", None), None
    target = parse_destination_input(message.text)
    if target is None:
        return None, None, None, "ورودی قابل فهم نیست."
    if isinstance(target, str) and target.startswith("http"):
        return await _resolve_invite(target)
    if isinstance(target, int):
        return target, None, None, None
    api = await _any_api()
    if api is None:
        return None, None, None, "اول یک ربات دریافت اضافه کنید."
    try:
        info = await api.get_chat(f"@{target}")
    except BotApiError as e:
        return None, None, None, f"تلگرام: {e.code} {e.description}"
    except BotApiNetworkError:
        return None, None, None, "اتصال به تلگرام برقرار نشد."
    return info["id"], info.get("title"), info.get("username"), None


async def _resolve_invite(link: str):
    """ربات‌ها با لینک دعوت وارد چت نمی‌شوند؛ اکانت جمع‌کننده با لینک عضو می‌شود و آیدی چت را پیدا می‌کند.
    (ربات دریافت را بعدش باید خودتان در آن چت اضافه کنید.)"""
    workers = [w for w in worker_manager.workers.values() if getattr(w, "is_running", False)]
    if not workers:
        return None, None, None, "برای لینک دعوت باید حداقل یک اکانت جمع‌کننده روشن باشد؛ یا آیدی عددی/فوروارد بفرستید."
    client = workers[0].client
    try:
        chat = await client.get_chat(link)
        if not getattr(chat, "id", None):          # اکانت هنوز عضو نیست
            joined = await client.join_chat(link)
            chat = unwrap_chat(joined) or await client.get_chat(link)
            if not getattr(chat, "id", None):
                return None, None, None, "درخواست عضویت ثبت شد ولی هنوز تأیید نشده؛ بعد از تأیید دوباره بفرستید."
    except Exception as e:
        logger.warning(f"invite resolve failed: {type(e).__name__}: {e}")
        return None, None, None, f"اکانت نتوانست با این لینک عضو شود ({type(e).__name__}). لینک منقضی یا نامعتبر است؟"
    return chat.id, getattr(chat, "title", None), getattr(chat, "username", None), None


async def _title_of(api: BotApi, chat_id: int, title):
    if title:
        return title
    try:
        return (await api.get_chat(chat_id)).get("title") or str(chat_id)
    except (BotApiError, BotApiNetworkError):
        return str(chat_id)


@admin_bot.on_message(filters.private & admin_only & ~filters.command(["start", "menu", "cancel"]), group=2)
async def v2_text(client: Client, message: Message):
    admin_id = message.from_user.id
    state = v2_states.get(admin_id)
    if not state:
        return
    if login_states.get(admin_id):          # ورود اکانت در جریان است؛ این حالت منقضی
        v2_states.pop(admin_id, None)
        return
    step = state["step"]
    text = (message.text or "").strip()
    try:
        if step == "bot_token":
            await _add_bot(message, text)
        elif step == "final_chat":
            await _set_final(message, admin_id)
        elif step == "forced_chat":
            await _forced_chat(message, admin_id)
        elif step == "forced_link":
            await _forced_link(message, admin_id, text)
        elif step == "tpl_suffix":
            suffix = "" if text == "-" else (message.text or "").strip()
            ok, err = logic.validate_suffix(suffix)
            if not ok:
                await _reply(message, f"❌ {err}\nدوباره بفرستید یا /cancel")
                return
            await store.set_setting("caption_suffix", suffix)
            v2_states.pop(admin_id, None)
            await _reply(message, "✅ متن انتهای کپشن ذخیره شد (برای پست‌های بعدی).", _back("v2:tpl"))
        elif step == "tpl_button":
            ok, err = logic.validate_button_text(text)
            if not ok:
                await _reply(message, f"❌ {err}\nدوباره بفرستید یا /cancel")
                return
            await store.set_setting("tpl_button", text)
            v2_states.pop(admin_id, None)
            await _reply(message, "✅ متن دکمه ذخیره شد (فقط برای پست‌های بعدی).", _back("v2:tpl"))
        elif step == "delsec":
            try:
                seconds = logic.parse_delete_seconds(text)
            except ValueError as e:
                await _reply(message, f"❌ {e}\nدوباره بفرستید یا /cancel")
                return
            await store.set_setting("delete_after_sec", str(seconds))
            v2_states.pop(admin_id, None)
            await _reply(message, f"✅ زمان حذف: {logic.format_duration(seconds)} (برای ویدیوهای بعدی).", _back())
    except Exception as e:
        logger.error(f"v2 text step {step} failed: {type(e).__name__}: {e}", exc_info=True)
        v2_states.pop(admin_id, None)
        await _reply(message, "❌ خطای غیرمنتظره؛ لاگ را ببینید.", _back())


# ---------- افزودن ربات ----------
async def _add_bot(message: Message, token: str):
    admin_id = message.from_user.id
    try:
        await message.delete()          # توکن از چت پاک شود
    except Exception:
        pass
    if not TOKEN_RE.match(token):
        await _reply(message, "قالب توکن درست نیست. توکن را از @BotFather کپی کنید و بفرستید، یا /cancel")
        return
    if token == settings.admin_bot_token:
        await _reply(message, "این توکن خودِ ربات مدیریت است. یک ربات جدید بسازید.")
        return
    try:
        me = await BotApi(token).get_me()
    except BotApiError:
        await _reply(message, "❌ تلگرام این توکن را قبول نکرد.\nدوباره بفرستید یا /cancel")
        return
    except BotApiNetworkError:
        await _reply(message, "❌ اتصال به تلگرام برقرار نشد؛ کمی بعد دوباره بفرستید.")
        return
    if await store.get_bot_by_tg_id(me["id"]):
        v2_states.pop(admin_id, None)
        await _reply(message, "این ربات قبلاً اضافه شده است.", _back("v2:bots"))
        return
    bot = await store.add_bot(me["id"], me["username"], crypto.encrypt(token))
    v2_states.pop(admin_id, None)
    await _reply(
        message,
        f"✅ ربات @{me['username']} اضافه شد (فعلاً غیرفعال).\n\n"
        "مرحله‌ی بعد: این ربات را «ادمین» کنید در کانال مخزن، کانال نهایی (با اجازه‌ی ارسال پست) و کانال‌های "
        "عضویت اجباری. بعد در همین منو دکمه‌ی «فعال‌سازی» را بزنید تا دسترسی‌ها واقعاً آزمایش شوند.",
        kb([[("🔧 مدیریت این ربات", f"v2:bot:{bot.id}")], [("🔙 ربات‌ها", "v2:bots")]]))


# ---------- کانال نهایی ----------
async def _send_test(api: BotApi, chat_id: int) -> str:
    """پیام آزمایشی می‌فرستد و پاک می‌کند؛ رشته‌ی خطا یا ''."""
    try:
        sent = await api.send_message(chat_id, "✅ پیام آزمایشی (به‌زودی پاک می‌شود)")
    except BotApiError as e:
        return f"{e.code} {e.description}"
    except BotApiNetworkError:
        return "عدم اتصال به تلگرام"
    try:
        await api.delete_message(chat_id, sent["message_id"])
    except (BotApiError, BotApiNetworkError):
        pass
    return ""


async def _set_final(message: Message, admin_id: int):
    api = await _any_api()
    if api is None:
        await _reply(message, "اول یک ربات دریافت اضافه کنید؛ تست ارسال با آن انجام می‌شود.", _back())
        v2_states.pop(admin_id, None)
        return
    chat_id, title, _, error = await _resolve_chat(message)
    if chat_id is None:
        await _reply(message, f"❌ {error}\nلغو: /cancel")
        return
    err = await _send_test(api, chat_id)
    if err:
        hint = ("ربات این کانال را نمی‌شناسد؛ ربات را به کانال اضافه کنید." if "chat not found" in err.lower()
                else "ربات را ادمین کانال کنید (اجازه‌ی «ارسال پست») و دوباره بفرستید.")
        await _reply(message, f"❌ ربات نتوانست در این کانال پیام بفرستد.\nخطای تلگرام: {err}\n{hint}\nلغو: /cancel")
        return
    title = await _title_of(api, chat_id, title)
    await store.set_setting("final_chat_id", str(chat_id))
    await store.set_setting("final_chat_title", title or "")
    v2_states.pop(admin_id, None)
    await _reply(message, f"✅ کانال نهایی تنظیم شد: {title}\n(پیام آزمایشی ارسال و پاک شد)", _back())


# ---------- عضویت اجباری ----------
async def _forced_chat(message: Message, admin_id: int):
    chat_id, title, username, error = await _resolve_chat(message)
    if chat_id is None:
        await _reply(message, f"❌ {error}\n(برای کانال خصوصی حتماً فوروارد یا آیدی عددی)\nلغو: /cancel")
        return
    api = await _any_api()
    if api is not None:
        title = await _title_of(api, chat_id, title)
    if username:
        await _finish_forced(message, admin_id, chat_id, title, f"https://t.me/{username}")
        return
    typed = (message.text or "").strip()
    if re.match(r"^https://t\.me/(\+|joinchat/)", typed):      # لینک دعوتی که فرستاده شد، خودش لینک دکمه است
        await _finish_forced(message, admin_id, chat_id, title, typed)
        return
    v2_states[admin_id] = {"step": "forced_link", "chat_id": chat_id, "title": title or str(chat_id)}
    await _reply(message, "این کانال یوزرنیم عمومی ندارد. لینک دعوتش (https://t.me/+...) را بفرستید تا روی دکمه‌ی "
                          "عضویت قرار بگیرد.\nلغو: /cancel")


async def _forced_link(message: Message, admin_id: int, text: str):
    if not re.match(r"^https://t\.me/[A-Za-z0-9_+\-/]+$", text):
        await _reply(message, "لینک باید شبیه https://t.me/+abc... باشد. دوباره بفرستید یا /cancel")
        return
    st = v2_states[admin_id]
    await _finish_forced(message, admin_id, st["chat_id"], st["title"], text)


async def _finish_forced(message: Message, admin_id: int, chat_id: int, title, link: str):
    row = await store.add_forced(chat_id, title or str(chat_id), link)
    v2_states.pop(admin_id, None)
    await _reply(message, f"✅ «{title or chat_id}» به عضویت اجباری اضافه شد.\n"
                          "حالا هر ربات دریافت باید در این کانال ادمین باشد؛ از دکمه‌ی آزمایش استفاده کنید.",
                 kb([[("🔍 آزمایش دسترسی", f"v2:fctest:{row.id}")], [("🔙 عضویت اجباری", "v2:forced")]]))


# ---------- دکمه‌ها ----------
@admin_bot.on_callback_query(admin_only & filters.regex(r"^v2:"), group=-1)
async def v2_callback(client: Client, cq: CallbackQuery):
    try:
        await _route(cq, cq.data or "")
    except Exception as e:
        logger.error(f"v2 callback {cq.data} failed: {type(e).__name__}: {e}", exc_info=True)
        try:
            await cq.answer("خطا؛ لاگ را ببینید", show_alert=True)
        except Exception:
            pass
    else:
        try:
            await cq.answer()
        except Exception:
            pass
    cq.stop_propagation()     # هندلر عمومیِ پنل برای این دکمه‌ها اجرا نشود


def _arg(parts: list[str], i: int = 2) -> int:
    return int(parts[i])


async def _route(cq: CallbackQuery, data: str):
    admin_id = cq.from_user.id
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "home"
    if action in ("home", "togc", "togd", "bots", "forced", "tpl", "stats", "issues", "health", "final", "del"):
        login_states.pop(admin_id, None)
        v2_states.pop(admin_id, None)

    if action in ("togc", "togd"):
        key = "collect_enabled" if action == "togc" else "distribute_enabled"
        now_on = await distribution.flags.get(key)
        await store.set_setting(key, "0" if now_on else "1")
        distribution.flags.invalidate(key)
        action = "home"

    if action == "home":
        collect = await distribution.flags.get("collect_enabled")
        distribute = await distribution.flags.get("distribute_enabled")
        await _edit(cq, "🚀 توزیع و انتشار\n(مخزن همان «مقصد ارسال» است)\n\n"
                        f"جمع‌آوری ویدیو و کپشن: {'🟢 روشن' if collect else '🔴 خاموش'}\n"
                        f"توزیع و انتشار: {'🟢 روشن' if distribute else '🔴 خاموش'}\n\n"
                        "خاموش‌بودن جمع‌آوری: پست‌های آن مدت رد می‌شوند و بعد از روشن‌شدن از همان‌جا ادامه می‌دهد.\n"
                        "خاموش‌بودن توزیع: انتشار پست جدید متوقف می‌شود (صف می‌ماند)؛ دکمه‌های قبلی کار می‌کنند.", kb([
            [(f"{'🟢' if collect else '🔴'} جمع‌آوری: {'روشن' if collect else 'خاموش'}", "v2:togc")],
            [(f"{'🟢' if distribute else '🔴'} توزیع و انتشار: {'روشن' if distribute else 'خاموش'}", "v2:togd")],
            [("🤖 ربات‌های دریافت", "v2:bots"), ("📢 کانال نهایی", "v2:final")],
            [("🔒 عضویت اجباری", "v2:forced"), ("✍️ کپشن و دکمه", "v2:tpl")],
            [("⏱ زمان حذف ویدیو", "v2:del"), ("🗄 مخزن (مقصد)", "dest")],
            [("📈 آمار", "v2:stats"), ("⚠️ ناموفق‌ها", "v2:issues")],
            [("🩺 سلامت و آزمایش اتصال", "v2:health")],
            [("🔙 منوی اصلی", "main")]]))

    # ----- ربات‌ها -----
    elif action == "bots":
        bots = await store.list_bots()
        rows = [[(f"{'🟢' if b.is_active else '⚪'} @{b.username}", f"v2:bot:{b.id}")] for b in bots]
        rows += [[("➕ افزودن ربات", "v2:botadd")], [("🔙 بازگشت", HOME)]]
        await _edit(cq, f"ربات‌های دریافت ({len(bots)})\nتخصیص ویدیوها بین ربات‌های فعال چرخشی است.", kb(rows))

    elif action == "botadd":
        v2_states[admin_id] = {"step": "bot_token"}
        await _edit(cq, "توکن ربات جدید را از @BotFather بفرستید (پیام شما بعد از خواندن پاک می‌شود).\n"
                        "هر ربات فقط باید در یک برنامه استفاده شود.\nلغو: /cancel", _back("v2:bots"))

    elif action == "bot":
        await _bot_detail(cq, _arg(parts))

    elif action == "botact":
        await _toggle_bot(cq, _arg(parts))

    elif action == "bothc":
        bot = await store.get_bot(_arg(parts))
        checks = await _run_checks(bot)
        await _edit(cq, f"🩺 @{bot.username}\n\n{_fmt_checks(checks)}", _back(f"v2:bot:{bot.id}"))

    elif action == "botdel":
        pk = _arg(parts)
        await _edit(cq, "این ربات حذف شود؟ (فقط اگر هیچ پست یا تحویلی به آن وابسته نباشد)",
                    kb([[("✅ بله", f"v2:botdelok:{pk}")], [("🔙 انصراف", f"v2:bot:{pk}")]]))

    elif action == "botdelok":
        pk = _arg(parts)
        bot = await store.get_bot(pk)
        if bot and bot.is_active:
            await store.set_bot_active(pk, False)
            await distribution.sync_bots()
        if await store.delete_bot(pk):
            distribution.forget_api(pk)
            await _edit(cq, "ربات حذف شد.", _back("v2:bots"))
        else:
            await _edit(cq, "این ربات پست یا تحویل ثبت‌شده دارد و حذف نمی‌شود؛ فقط غیرفعال شد.\n"
                            "(لینک‌های قبلیِ آن در کانال نهایی دیگر پاسخ نمی‌دهند.)", _back("v2:bots"))

    # ----- کانال نهایی -----
    elif action == "final":
        fid, title = await _final_chat_id(), await store.get_setting("final_chat_title", "")
        text = f"کانال نهایی: {title or '-'}\nآیدی: {fid}" if fid else "کانال نهایی هنوز تنظیم نشده؛ تا آن موقع پست‌ها در صف می‌مانند."
        await _edit(cq, text, kb([[("✏️ تنظیم / تغییر", "v2:finalset")], [("🧪 آزمایش ارسال", "v2:finaltest")],
                                  [("🔙 بازگشت", HOME)]]))

    elif action == "finalset":
        v2_states[admin_id] = {"step": "final_chat"}
        await _edit(cq, "کانال یا گروه نهایی را بفرستید: فوروارد یک پیام، آیدی عددی، @یوزرنیم یا لینک دعوت خصوصی (t.me/+...).\n"
                        "با لینک دعوت، اکانت جمع‌کننده عضو می‌شود و آیدی را پیدا می‌کند.\n"
                        "ربات‌های دریافت را خودتان در آن اضافه کنید (کانال: ادمین با اجازه‌ی ارسال پست؛ گروه: عضو).\nلغو: /cancel", _back("v2:final"))

    elif action == "finaltest":
        fid = await _final_chat_id()
        bots = [b for b in await store.list_bots() if b.is_active]
        if not fid or not bots:
            await _edit(cq, "برای آزمایش، کانال نهایی و حداقل یک ربات فعال لازم است.", _back("v2:final"))
            return
        lines = []
        for b in bots:
            err = await _send_test(await distribution.api_for(b.id), fid)
            lines.append(f"{'❌' if err else '✅'} @{b.username}" + (f": {err}" if err else ""))
        await _edit(cq, "نتیجه‌ی آزمایش ارسال (پیام آزمایشی پاک می‌شود):\n\n" + "\n".join(lines), _back("v2:final"))

    # ----- عضویت اجباری -----
    elif action == "forced":
        chans = await store.list_forced()
        rows = [[(f"{'🟢' if c.is_active else '⚪'} {c.title or c.chat_id}", f"v2:fc:{c.id}")] for c in chans]
        rows += [[("➕ افزودن کانال", "v2:fcadd")], [("🔙 بازگشت", HOME)]]
        await _edit(cq, f"کانال‌های عضویت اجباری ({len(chans)})\nکاربر باید عضو همه‌ی کانال‌های فعال باشد.", kb(rows))

    elif action == "fcadd":
        v2_states[admin_id] = {"step": "forced_chat"}
        await _edit(cq, "کانال را بفرستید: یک پیام از آن فوروارد کنید، یا آیدی عددی/@یوزرنیم.\nلغو: /cancel",
                    _back("v2:forced"))

    elif action in ("fc", "fcact", "fcdel", "fcdelok", "fctest"):
        pk = _arg(parts)
        chans = {c.id: c for c in await store.list_forced()}
        ch = chans.get(pk)
        if ch is None:
            await _edit(cq, "این کانال پیدا نشد.", _back("v2:forced"))
        elif action == "fcact":
            await store.set_forced_active(pk, not ch.is_active)
            await _edit(cq, "وضعیت تغییر کرد (از همین لحظه روی درخواست‌های جدید اعمال می‌شود).", _back(f"v2:fc:{pk}"))
        elif action == "fcdel":
            await _edit(cq, f"«{ch.title}» حذف شود؟", kb([[("✅ بله", f"v2:fcdelok:{pk}")], [("🔙 انصراف", f"v2:fc:{pk}")]]))
        elif action == "fcdelok":
            await store.delete_forced(pk)
            await _edit(cq, "حذف شد.", _back("v2:forced"))
        elif action == "fctest":
            bots = await store.list_bots()
            if not bots:
                await _edit(cq, "هنوز رباتی اضافه نشده.", _back(f"v2:fc:{pk}"))
                return
            lines = []
            for b in bots:
                checks = await check_bot(await distribution.api_for(b.id), b.bot_id, None, None, [ch])
                checks = [c for c in checks if c.label != "مخزن"]        # فقط خودِ کانال اجباری مهم است
                bad = [c for c in checks if not c.ok]
                lines.append(f"{'❌' if bad else '✅'} @{b.username}" + (f": {bad[0].detail}" if bad else ""))
            await _edit(cq, f"آزمایش بررسی عضویت «{ch.title}»:\n\n" + "\n".join(lines), _back(f"v2:fc:{pk}"))
        else:
            await _edit(cq, f"{'🟢 فعال' if ch.is_active else '⚪ غیرفعال'}\n{ch.title}\nآیدی: {ch.chat_id}\nلینک: {ch.link or '-'}",
                        kb([[("🔁 فعال/غیرفعال", f"v2:fcact:{pk}"), ("🔍 آزمایش", f"v2:fctest:{pk}")],
                            [("🗑 حذف", f"v2:fcdel:{pk}")], [("🔙 بازگشت", "v2:forced")]]))

    # ----- کپشن و دکمه -----
    elif action == "tpl":
        suffix = await store.get_setting("caption_suffix", "") or "(خالی)"
        btn = await store.get_setting("tpl_button", logic.DEFAULT_TPL_BUTTON)
        await _edit(cq, "کپشن نهایی = همان کپشنی که در مخزن ذخیره می‌شود + متن انتهایی زیر.\n\n"
                        f"متن انتهای کپشن:\n{suffix}\n\nمتن دکمه: {btn}\n\n"
                        "کانال نهایی فقط کپشن و دکمه می‌گیرد؛ ویدیوها فقط داخل ربات دریافت‌اند.\n"
                        "تغییر فقط روی پست‌های بعدی اثر دارد.",
                    kb([[("➕ متن انتهای کپشن", "v2:tpls")], [("✏️ متن دکمه", "v2:tplb")],
                        [("👁 پیش‌نمایش", "v2:tplv")], [("🔙 بازگشت", HOME)]]))

    elif action in ("tpls", "tplb"):
        step, prompt = {
            "tpls": ("tpl_suffix", "متنی که همیشه آخر کپشن اضافه شود را بفرستید (آیدی کانال، توضیحات، لینک‌ها). "
                                   "برای خالی‌کردن: -"),
            "tplb": ("tpl_button", "متن دکمه‌ی شیشه‌ای زیر پست را بفرستید (حداکثر ۴۰ نویسه)."),
        }[action]
        v2_states[admin_id] = {"step": step}
        await _edit(cq, prompt + "\nلغو: /cancel", _back("v2:tpl"))

    elif action == "tplv":
        suffix = await store.get_setting("caption_suffix", "") or ""
        try:
            text, trimmed = logic.compose_final("نمونه‌ی کپشن ذخیره‌شده در مخزن", suffix, logic.POST_TEXT_LIMIT)
            await _edit(cq, "پیش‌نمایش پست کانال نهایی:\n\n" + (text or logic.DEFAULT_POST_TEXT),
                        _back("v2:tpl"))
        except logic.TemplateTooLong as e:
            await _edit(cq, f"❌ {e}", _back("v2:tpl"))

    # ----- زمان حذف (ثانیه) -----
    elif action == "del":
        raw = await store.get_setting("delete_after_sec")
        cur = int(raw) if raw and raw.isdigit() else logic.DEFAULT_DELETE_SECONDS
        await _edit(cq, f"ویدیو {logic.format_duration(cur)} بعد از ارسال از چت کاربر حذف می‌شود.\n"
                        "(از ۵ ثانیه تا کمتر از ۴۸ ساعت؛ فقط برای ویدیوهای بعدی اثر دارد.)",
                    kb([[("30 ثانیه", "v2:delset:30"), ("60 ثانیه", "v2:delset:60")],
                        [("5 دقیقه", "v2:delset:300"), ("1 ساعت", "v2:delset:3600")],
                        [("✏️ عدد دلخواه (ثانیه)", "v2:delcustom")], [("🔙 بازگشت", HOME)]]))

    elif action == "delset":
        seconds = logic.parse_delete_seconds(parts[2])
        await store.set_setting("delete_after_sec", str(seconds))
        await _edit(cq, f"✅ زمان حذف: {logic.format_duration(seconds)}", _back("v2:del"))

    elif action == "delcustom":
        v2_states[admin_id] = {"step": "delsec"}
        await _edit(cq, "زمان حذف را به ثانیه بفرستید (۵ تا ۱۶۹۲۰۰).\nلغو: /cancel", _back("v2:del"))

    # ----- آمار / خطاها / سلامت -----
    elif action == "stats":
        await _edit(cq, await _stats_text(), kb([[("🔄 تازه‌سازی", "v2:stats")], [("🔙 بازگشت", HOME)]]))

    elif action == "issues":
        await _issues(cq)

    elif action == "requeue":
        which = parts[2]
        n = await store.requeue_posts(["failed"] if which == "failed" else ["unknown"])
        await _edit(cq, f"{n} پست به صف انتشار برگشت.", _back("v2:issues"))

    elif action == "health":
        bots = await store.list_bots()
        if not bots:
            await _edit(cq, "هنوز رباتی اضافه نشده.", _back())
            return
        await _edit(cq, "در حال آزمایش اتصال‌ها…")
        results = await asyncio.gather(*[_run_checks(b) for b in bots])
        text = "\n\n".join(f"@{b.username} {'🟢' if b.is_active else '⚪'}\n{_fmt_checks(c)}" for b, c in zip(bots, results))
        await _edit(cq, "🩺 نتیجه‌ی آزمایش واقعی:\n\n" + text, _back())


async def _bot_detail(cq: CallbackQuery, pk: int):
    bot = await store.get_bot(pk)
    if bot is None:
        await _edit(cq, "ربات پیدا نشد.", _back("v2:bots"))
        return
    health = "آزمایش نشده" if bot.last_health_ok is None else (
        f"{'✅ سالم' if bot.last_health_ok else '❌ ' + (bot.last_error or 'مشکل')} ({bot.last_health_at:%m-%d %H:%M} UTC)")
    await _edit(cq, f"@{bot.username}  {'🟢 فعال' if bot.is_active else '⚪ غیرفعال'}\n"
                    f"سلامت: {health}\nتخصیص‌یافته: {bot.assigned_count} | درخواست‌ها: {bot.request_count} | خطاها: {bot.error_count}",
                kb([[("🔁 فعال‌سازی/غیرفعال‌سازی", f"v2:botact:{pk}")], [("🩺 آزمایش دسترسی‌ها", f"v2:bothc:{pk}")],
                    [("🗑 حذف", f"v2:botdel:{pk}")], [("🔙 ربات‌ها", "v2:bots")]]))


async def _toggle_bot(cq: CallbackQuery, pk: int):
    bot = await store.get_bot(pk)
    if bot is None:
        return
    if bot.is_active:
        n = await store.count_published_for_bot(pk)
        await store.set_bot_active(pk, False)
        await distribution.sync_bots()
        warn = (f"\n\n⚠️ {n} پست منتشرشده به این ربات لینک دارند؛ تا فعال‌سازی دوباره، دکمه‌ی دانلودشان کار نمی‌کند."
                if n else "")
        await _edit(cq, f"@{bot.username} غیرفعال شد (پست جدیدی به آن اختصاص نمی‌یابد)." + warn, _back(f"v2:bot:{pk}"))
        return
    checks = await _run_checks(bot)
    if not all(c.ok for c in checks):
        await _edit(cq, f"❌ فعال نشد؛ این موارد باید درست شود:\n\n{_fmt_checks(checks)}", _back(f"v2:bot:{pk}"))
        return
    await store.set_bot_active(pk, True)
    await distribution.sync_bots()
    await _edit(cq, f"✅ @{bot.username} فعال شد.\n\n{_fmt_checks(checks)}", _back(f"v2:bot:{pk}"))


async def _stats_text() -> str:
    posts, deliveries, bots = await store.post_counts(), await store.delivery_counts(), await store.list_bots()
    pn = {"pending": "در صف", "publishing": "در حال انتشار", "published": "منتشرشده", "failed": "ناموفق", "unknown": "نامشخص"}
    dn = {"sent": "ارسال‌شده (هنوز در چت)", "deleted": "حذف‌شده", "send_failed": "ارسال ناموفق", "unknown": "ارسال نامشخص",
          "delete_failed": "حذف ناموفق", "sending": "در حال ارسال"}
    lines = ["📈 پست‌های کانال نهایی:"] + [f"• {pn[k]}: {posts.get(k, 0)}" for k in pn]
    lines += ["", "📥 تحویل به کاربران:"] + [f"• {dn[k]}: {deliveries.get(k, 0)}" for k in dn]
    lines += ["", "🤖 ربات‌ها:"] + [f"• @{b.username}: اختصاص {b.assigned_count} | درخواست {b.request_count} | خطا {b.error_count}"
                                   for b in bots]
    return "\n".join(lines)


async def _issues(cq: CallbackQuery):
    bad = await store.list_posts(["failed", "unknown"], 8)
    dl = await store.delivery_counts()
    lines = []
    for p in bad:
        label = "ناموفق" if p.status == "failed" else "نامشخص"
        lines.append(f"• پست {p.id} [{label}]: {(p.error_message or '')[:90]}")
    if not lines:
        lines = ["پست ناموفق یا نامشخصی نیست."]
    extra = [f"حذف ناموفق: {dl.get('delete_failed', 0)} | ارسال ناموفق: {dl.get('send_failed', 0)} | "
             f"ارسال نامشخص: {dl.get('unknown', 0)}"]
    note = ("\n\n«نامشخص» یعنی نتیجه‌ی ارسال معلوم نیست. اول کانال نهایی را چک کنید؛ فقط اگر پست نیامده بود، "
            "دکمه‌ی ارسال مجدد را بزنید (وگرنه ممکن است دو بار منتشر شود).")
    await _edit(cq, "⚠️ ناموفق‌ها\n\n" + "\n".join(lines) + "\n\n" + "\n".join(extra) + note,
                kb([[("🔁 تلاش مجدد ناموفق‌ها", "v2:requeue:failed")],
                    [("🔁 ارسال مجدد نامشخص‌ها (بعد از چک دستی)", "v2:requeue:unknown")], [("🔙 بازگشت", HOME)]]))
