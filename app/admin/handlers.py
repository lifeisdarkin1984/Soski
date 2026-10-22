import os
from collections import deque
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
from sqlalchemy import select, func
from app.admin.bot import admin_bot
from app.core.config import settings
from app.core.logger import logger
from app.core.database import AsyncSessionLocal
from app.core.exceptions import AccountAuthError
from app.features.accounts.manager import account_manager, worker_manager
from app.models import Account, AccountStatus, MonitoredChannel, ProcessedMessage, Delivery
from app.repositories.message_repository import MessageRepository
from app.repositories.channel_repository import ChannelRepository
from app.features.detector.analyzer import analyzer
from app.repositories.settings_repository import SettingsRepository, get_destination_info
from app.features.bot_engine.helpers import parse_destination_input, unwrap_chat

LOG_FILE = os.path.join("logs", "collector.log")
login_states: dict[int, dict] = {}   # admin_id -> {'step': 'phone'|'code'|'password', 'phone': str}


# ---------- دسترسی: فقط مدیرها (در سمت سرور بررسی می‌شود) ----------
async def _admin_check(_, __, update) -> bool:
    user = getattr(update, "from_user", None)
    return bool(user and user.id in settings.admin_id_list)

admin_only = filters.create(_admin_check)


def kb(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])


MAIN_MENU = kb([
    [("👥 اکانت‌ها", "accounts"), ("📡 کانال‌ها", "channels")],
    [("🗂 فایل‌های ارسال‌شده", "history"), ("📊 وضعیت سیستم", "status")],
    [("🧾 خطاها و لاگ", "logs"), ("🔁 تلاش مجدد ناموفق‌ها", "retry_failed")],
    [("📤 مقصد ارسال", "dest"), ("♻️ ری‌استارت ورکرها", "restart_all")],
])
BACK = kb([[("🔙 منوی اصلی", "main")]])


async def _edit(cq: CallbackQuery, text: str, markup=None):
    try:
        await cq.message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.DISABLED)
    except Exception as e:
        if "MESSAGE_NOT_MODIFIED" not in str(e):
            logger.warning(f"edit failed: {type(e).__name__}")


# ---------- پیام‌ها ----------
@admin_bot.on_message(filters.private & filters.command(["start", "menu"]) & admin_only)
async def cmd_start(client: Client, message: Message):
    login_states.pop(message.from_user.id, None)
    await message.reply_text("پنل مدیریت جمع‌کننده‌ی رسانه", reply_markup=MAIN_MENU, parse_mode=ParseMode.DISABLED)


@admin_bot.on_message(filters.private & filters.command("cancel") & admin_only)
async def cmd_cancel(client: Client, message: Message):
    state = login_states.pop(message.from_user.id, None)
    if state:
        await account_manager.cancel_login(state.get("phone"))
    await message.reply_text("لغو شد.", reply_markup=MAIN_MENU)


@admin_bot.on_message(filters.private & ~admin_only, group=5)
async def deny(client: Client, message: Message):
    await message.reply_text("شما دسترسی ندارید.")


@admin_bot.on_message(filters.private & ~filters.command(["start", "menu", "cancel"]) & admin_only, group=1)
async def login_steps(client: Client, message: Message):
    admin_id = message.from_user.id
    state = login_states.get(admin_id)
    if not state:
        return
    text = (message.text or "").strip()
    if state["step"] == "dest":
        await _set_destination(message, admin_id)
        return
    if not text:
        await message.reply_text("لطفاً متن بفرستید.")
        return
    try:
        if state["step"] == "phone":
            phone = await account_manager.begin_login(text)
            login_states[admin_id] = {"step": "code", "phone": phone}
            await message.reply_text(
                "کد تلگرام ارسال شد. کد را با فاصله بین رقم‌ها بفرستید (مثلاً 1 2 3 4 5) تا تلگرام آن را "
                "به‌عنوان «اشتراک کد» مسدود نکند.\nبرای لغو: /cancel")
        elif state["step"] == "code":
            result = await account_manager.submit_code(state["phone"], text)
            if result == "password_required":
                login_states[admin_id] = {"step": "password", "phone": state["phone"]}
                await message.reply_text("رمز دومرحله‌ای (Two-step) اکانت را بفرستید.\nبرای لغو: /cancel")
            else:
                await _login_done(message, state["phone"], admin_id)
        elif state["step"] == "password":
            await account_manager.submit_password(state["phone"], text)
            try:
                await message.delete()   # رمز از چت پاک شود
            except Exception:
                pass
            await _login_done(message, state["phone"], admin_id)
    except AccountAuthError as e:
        if str(e) in ("کد اشتباه است.", "رمز دومرحله‌ای اشتباه است."):
            await message.reply_text(f"{e} دوباره بفرستید یا /cancel")
        else:
            login_states.pop(admin_id, None)
            await message.reply_text(f"❌ {e}", reply_markup=MAIN_MENU)
    except Exception as e:
        logger.error(f"login step failed: {type(e).__name__}: {e}", exc_info=True)
        login_states.pop(admin_id, None)
        await message.reply_text("❌ خطای غیرمنتظره؛ لاگ را ببینید.", reply_markup=MAIN_MENU)


async def _set_destination(message: Message, admin_id: int):
    chat = getattr(message, "forward_from_chat", None)
    origin = getattr(message, "forward_origin", None)
    if chat is None and origin is not None:
        chat = getattr(origin, "chat", None)
    target = chat.id if chat is not None else parse_destination_input(message.text)
    if target is None:
        await message.reply_text(
            "ورودی قابل فهم نیست. یک پیام از مقصد فوروارد کنید، یا آیدی (مثل -1001234567890)، "
            "@یوزرنیم یا لینک را بفرستید.\nلغو: /cancel")
        return

    workers = [w for w in worker_manager.workers.values() if w.is_running]
    if not workers:
        if isinstance(target, int):
            async with AsyncSessionLocal() as db:
                await SettingsRepository(db).set_destination(target, getattr(chat, "title", "") or "")
            login_states.pop(admin_id, None)
            await message.reply_text(
                "ذخیره شد. ولی هنوز اکانتی روشن نیست و نتوانستم دسترسی ارسال را بررسی کنم.", reply_markup=MAIN_MENU)
        else:
            await message.reply_text("برای لینک یا یوزرنیم، اول باید یک اکانت اضافه و روشن باشد؛ یا آیدی عددی بفرستید.")
        return

    client = workers[0].client
    # کش شناخته‌شده‌های اکانت را تازه می‌کنیم تا کانالی که تازه عضو/ادمین شده شناخته شود
    try:
        async for _ in client.get_dialogs():
            pass
    except Exception as e:
        logger.warning(f"dialog refresh failed: {type(e).__name__}")
    try:
        resolved = await client.get_chat(target)
        if not getattr(resolved, "id", None):
            # لینک دعوت و اکانت هنوز عضو نیست: با همان لینک عضو می‌شود
            joined = await client.join_chat(target)
            resolved = unwrap_chat(joined) or await client.get_chat(target)
            if not getattr(resolved, "id", None):
                raise ValueError("join request pending")
        test = await client.send_message(resolved.id, "✅ مقصد تنظیم شد (پیام آزمایشی)")
        try:
            await test.delete()
        except Exception:
            pass
    except Exception as e:
        name = type(e).__name__
        logger.warning(f"destination check failed: {name}: {e}")
        if name in ("ChatWriteForbidden", "ChatAdminRequired", "UserBannedInChannel", "ChatSendPlainForbidden"):
            hint = "اکانت عضو است ولی اجازه‌ی ارسال ندارد. در کانال، ادمینش کنید و اجازه‌ی «ارسال پست» بدهید."
        elif name in ("ChannelInvalid", "ChannelPrivate", "PeerIdInvalid", "InviteHashExpired", "InviteHashInvalid"):
            hint = "اکانت این مقصد را نمی‌شناسد؛ احتمالاً عضو نیست یا لینک نامعتبر است. اکانت را عضو کنید و دوباره بفرستید."
        else:
            hint = "اکانت را عضو کنید (در کانال، ادمین با اجازه‌ی ارسال پست) و دوباره بفرستید."
        await message.reply_text(f"❌ نتوانستم در مقصد پیام بفرستم ({name}).\n{hint}\nلغو: /cancel")
        return

    title = getattr(resolved, "title", None) or getattr(resolved, "first_name", None) or ""
    async with AsyncSessionLocal() as db:
        await SettingsRepository(db).set_destination(resolved.id, title)
    login_states.pop(admin_id, None)
    resumed = 0
    for worker in list(worker_manager.workers.values()):
        resumed += await worker.resume_stuck_flows()
    await message.reply_text(
        f"✅ مقصد تنظیم شد: {title or resolved.id}" + (f"\n{resumed} درخواست منتظر شروع شد." if resumed else ""),
        reply_markup=MAIN_MENU)


async def _login_done(message: Message, phone: str, admin_id: int):
    login_states.pop(admin_id, None)
    async with AsyncSessionLocal() as db:
        account = (await db.execute(select(Account).where(Account.phone == phone))).scalar_one()
    await worker_manager.stop_worker(account.id)   # اگر قبلاً در حال اجرا بود با سشن جدید دوباره شروع شود
    started = await account_manager.start_account(account.id)
    await message.reply_text(
        f"✅ اکانت {phone} اضافه شد" + (" و شروع به کار کرد." if started else "."), reply_markup=MAIN_MENU)


# ---------- دکمه‌ها ----------
@admin_bot.on_callback_query(~admin_only)
async def cb_deny(client: Client, cq: CallbackQuery):
    await cq.answer("دسترسی ندارید", show_alert=True)


@admin_bot.on_callback_query(admin_only)
async def on_callback(client: Client, cq: CallbackQuery):
    data = cq.data or ""
    try:
        await _route(client, cq, data)
    except Exception as e:
        logger.error(f"callback {data} failed: {type(e).__name__}: {e}", exc_info=True)
        await cq.answer("خطا؛ لاگ را ببینید", show_alert=True)
        return
    try:
        await cq.answer()
    except Exception:
        pass


def _worker_label(account_id: int) -> str:
    st = worker_manager.get_worker_status(account_id)
    return {"running": "🟢 در حال کار", "starting": "🟡 در حال شروع", "error": "🔴 خطا", "stopped": "⚪ متوقف"}.get(
        st.value if st else "stopped", "⚪ متوقف")


async def _route(client: Client, cq: CallbackQuery, data: str):
    admin_id = cq.from_user.id

    if data == "main":
        login_states.pop(admin_id, None)
        await _edit(cq, "پنل مدیریت جمع‌کننده‌ی رسانه", MAIN_MENU)

    elif data == "accounts":
        async with AsyncSessionLocal() as db:
            accounts = (await db.execute(select(Account).order_by(Account.id))).scalars().all()
        rows = [[(f"{a.phone}  {_worker_label(a.id)}", f"acc_{a.id}")] for a in accounts]
        rows.append([("➕ افزودن اکانت", "add_account")])
        rows.append([("🔙 منوی اصلی", "main")])
        await _edit(cq, f"اکانت‌ها ({len(accounts)})", kb(rows))

    elif data.startswith("acc_"):
        await _account_detail(cq, int(data.split("_")[1]))

    elif data.startswith("accstart_"):
        account_id = int(data.split("_")[1])
        ok = await account_manager.start_account(account_id)
        await cq.answer("شروع شد" if ok else "قبلاً در حال کار است یا سشن معتبر نیست", show_alert=True)
        await _account_detail(cq, account_id)

    elif data.startswith("accstop_"):
        account_id = int(data.split("_")[1])
        await account_manager.stop_account(account_id)
        await _account_detail(cq, account_id)

    elif data.startswith("accdel_"):
        account_id = int(data.split("_")[1])
        await _edit(cq, "مطمئنید می‌خواهید این اکانت حذف شود؟ (سشن ذخیره‌شده پاک می‌شود)",
                    kb([[("✅ بله، حذف شود", f"accdelok_{account_id}")], [("🔙 انصراف", f"acc_{account_id}")]]))

    elif data.startswith("accdelok_"):
        await account_manager.remove_account(int(data.split("_")[1]))
        await _edit(cq, "اکانت حذف شد.", kb([[("🔙 اکانت‌ها", "accounts")]]))

    elif data == "add_account":
        login_states[admin_id] = {"step": "phone"}
        await _edit(cq, "شماره‌ی اکانت را با کد کشور بفرستید (مثلاً +989123456789).\nبرای لغو: /cancel",
                    kb([[("🔙 انصراف", "main")]]))

    elif data == "channels" or data.startswith("channels_"):
        page = int(data.split("_")[1]) if "_" in data else 0
        await _channels_page(cq, page)

    elif data == "detect_test":
        await _detect_test(cq)

    elif data == "sync_channels":
        for worker in list(worker_manager.workers.values()):
            if worker.is_running:
                await worker.sync_now()
        await _channels_page(cq, 0)

    elif data == "history":
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(Delivery).order_by(Delivery.created_at.desc()).limit(10))).scalars().all()
        if not rows:
            text = "هنوز فایلی ارسال نشده."
        else:
            text = "۱۰ فایل آخر:\n\n" + "\n".join(
                f"• {d.created_at:%m-%d %H:%M}  @{d.target_bot_username}  [{d.status}]" for d in rows)
        await _edit(cq, text, BACK)

    elif data == "status":
        await _edit(cq, await _status_text(), kb([[("🔄 تازه‌سازی", "status")], [("🔙 منوی اصلی", "main")]]))

    elif data == "logs":
        await _edit(cq, _tail_logs(), kb([[("🔄 تازه‌سازی", "logs")], [("🔙 منوی اصلی", "main")]]))

    elif data == "retry_failed":
        async with AsyncSessionLocal() as db:
            count = await MessageRepository(db).requeue_failed()
        resumed = 0
        for worker in list(worker_manager.workers.values()):
            resumed += await worker.resume_stuck_flows()
        await _edit(cq, f"{count} مورد ناموفق به صف برگشت و {resumed} مورد اجرا شد.", BACK)

    elif data == "dest":
        dest_id, title = await get_destination_info()
        text = (f"مقصد فعلی: {title or '-'}\nآیدی: {dest_id}" if dest_id is not None
                else "مقصد هنوز تنظیم نشده؛ تا تنظیم نشود هیچ فایلی ارسال نمی‌شود.")
        await _edit(cq, text, kb([[("✏️ تنظیم / تغییر مقصد", "dest_set")], [("🔙 منوی اصلی", "main")]]))

    elif data == "dest_set":
        login_states[admin_id] = {"step": "dest"}
        await _edit(cq, "مقصد را به یکی از این روش‌ها بفرستید:\n"
                        "۱) یک پیام از آن کانال/گروه برای من فوروارد کنید\n"
                        "۲) آیدی عددی (مثل -1001234567890)\n"
                        "۳) @یوزرنیم یا لینک کانال\n\n"
                        "نکته: اکانتی که به ربات اضافه کرده‌اید باید در آن مقصد اجازه‌ی ارسال داشته باشد.",
                    kb([[("🔙 انصراف", "main")]]))

    elif data == "restart_all":
        await _edit(cq, "در حال ری‌استارت ورکرها...")
        started = await account_manager.restart_all()
        await _edit(cq, f"{started} ورکر دوباره شروع شد.", BACK)


PAGE_SIZE = 15


def _all_links(message) -> list:
    urls = []
    markup = getattr(message, "reply_markup", None)
    for row in getattr(markup, "inline_keyboard", None) or []:
        for b in row:
            if getattr(b, "url", None):
                urls.append(b.url)
    for e in (getattr(message, "entities", None) or getattr(message, "caption_entities", None) or []):
        kind = str(getattr(getattr(e, "type", ""), "name", getattr(e, "type", ""))).lower()
        if kind == "text_link" and getattr(e, "url", None):
            urls.append(e.url)
    return urls


async def _detect_test(cq: CallbackQuery):
    """بدون ارسال چیزی: ۴۰ پست آخر هر کانال را با همان تشخیص‌دهنده‌ی اصلی می‌سنجد."""
    workers = [w for w in worker_manager.workers.values() if w.is_running]
    if not workers:
        await _edit(cq, "هیچ اکانتی روشن نیست.", BACK)
        return
    await _edit(cq, "در حال بررسی ۴۰ پست آخر هر کانال... چند ثانیه صبر کنید.")
    total = with_bot = checked = 0
    samples, others = [], []
    for w in workers:
        async with AsyncSessionLocal() as db:
            channels = await ChannelRepository(db).get_monitored_channels(w.account_id)
        for ch in channels[:10]:
            checked += 1
            name = ch.channel_title or (f"@{ch.channel_username}" if ch.channel_username else ch.channel_id)
            try:
                async for m in w.client.get_chat_history(ch.channel_id, limit=40):
                    total += 1
                    found = analyzer.analyze_message(m)
                    if found["is_media_request"]:
                        with_bot += 1
                        if len(samples) < 6:
                            samples.append(f"• {name}: @{found['bot_username']}")
                    else:
                        for u in _all_links(m):
                            if len(others) < 6 and u[:70] not in others:
                                others.append(u[:70])
            except Exception as e:
                logger.warning(f"detect test failed for {ch.channel_id}: {type(e).__name__}: {e}")
    text = (f"🔍 نتیجه‌ی تست\nکانال‌های بررسی‌شده: {checked}\nپست‌های بررسی‌شده: {total}\n"
            f"پست‌های دارای لینک ربات: {with_bot}\n")
    if samples:
        text += "\nنمونه‌ها:\n" + "\n".join(samples) + "\n"
    if others:
        text += "\nلینک‌هایی که ربات نبودند (نمونه):\n" + "\n".join(others)
    await _edit(cq, text[:3800], kb([[("🔙 کانال‌ها", "channels")], [("🔙 منوی اصلی", "main")]]))


async def _channels_page(cq: CallbackQuery, page: int):
    page = max(page, 0)
    async with AsyncSessionLocal() as db:
        total = (await db.execute(select(func.count()).select_from(MonitoredChannel))).scalar_one()
        rows = (await db.execute(select(MonitoredChannel).order_by(MonitoredChannel.id)
                                 .offset(page * PAGE_SIZE).limit(PAGE_SIZE))).scalars().all()
    lines = [f"کانال‌های تحت نظر: {total}  (صفحه {page + 1})", ""]
    for c in rows:
        name = c.channel_title or (f"@{c.channel_username}" if c.channel_username else "بدون نام")
        lines.append(f"• {name}  [{c.channel_id}]")
    if not rows:
        lines.append("چیزی در این صفحه نیست.")
    nav = []
    if page > 0:
        nav.append(("◀ قبلی", f"channels_{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(("بعدی ▶", f"channels_{page + 1}"))
    rows_kb = ([nav] if nav else []) + [[("🔄 همگام‌سازی الان", "sync_channels")],
               [("🔍 تست تشخیص لینک ربات", "detect_test")], [("🔙 منوی اصلی", "main")]]
    await _edit(cq, "\n".join(lines), kb(rows_kb))


async def _account_detail(cq: CallbackQuery, account_id: int):
    async with AsyncSessionLocal() as db:
        account = await db.get(Account, account_id)
        if not account:
            await _edit(cq, "اکانت پیدا نشد.", kb([[("🔙 اکانت‌ها", "accounts")]]))
            return
        channels = (await db.execute(select(func.count()).select_from(MonitoredChannel).where(
            MonitoredChannel.account_id == account_id))).scalar_one()
    text = (f"اکانت: {account.phone}\nوضعیت در دیتابیس: {account.status.value}\n"
            f"ورکر: {_worker_label(account_id)}\nکانال‌های تحت نظر: {channels}")
    await _edit(cq, text, kb([
        [("▶️ شروع", f"accstart_{account_id}"), ("⏹ توقف", f"accstop_{account_id}")],
        [("🗑 حذف", f"accdel_{account_id}")],
        [("🔙 اکانت‌ها", "accounts")],
    ]))


async def _group_counts(db, column) -> dict:
    rows = (await db.execute(select(column, func.count()).group_by(column))).all()
    return {getattr(k, "value", k): v for k, v in rows}


async def _status_text() -> str:
    async with AsyncSessionLocal() as db:
        msgs = await _group_counts(db, ProcessedMessage.status)
        deliveries = await _group_counts(db, Delivery.status)
        channels = (await db.execute(select(func.count()).select_from(MonitoredChannel))).scalar_one()
        accounts = await _group_counts(db, Account.status)
    health = await worker_manager.health_check()
    dest_id, dest_title = await get_destination_info()
    return (
        "📊 وضعیت سیستم\n\n"
        f"اکانت‌ها: {sum(accounts.values())} (فعال: {accounts.get('active', 0)})\n"
        f"ورکرهای سالم: {sum(1 for v in health.values() if v)} از {len(worker_manager.workers)}\n"
        f"کانال‌های تحت نظر: {channels}\n"
        f"مقصد ارسال: {(dest_title or dest_id) if dest_id is not None else 'تنظیم نشده'}\n\n"
        f"درخواست‌ها: در صف {msgs.get('pending', 0)} | در حال کار {msgs.get('processing', 0)} | "
        f"موفق {msgs.get('completed', 0)} | ناموفق {msgs.get('failed', 0)}\n"
        f"فایل‌ها: ارسال‌شده {deliveries.get('sent', 0)} | در حال ارسال {deliveries.get('sending', 0)} | "
        f"نامشخص {deliveries.get('unknown', 0)}"
    )


def _tail_logs(limit: int = 12) -> str:
    try:
        with open(LOG_FILE, encoding="utf-8", errors="ignore") as f:
            lines = deque(f, maxlen=500)
    except FileNotFoundError:
        return "فایل لاگ پیدا نشد."
    important = [l.rstrip() for l in lines if " | WARNING" in l or " | ERROR" in l]
    text = "\n".join(important[-limit:])
    return ("آخرین هشدارها و خطاها:\n\n" + text)[-3800:] if text else "خطا یا هشداری ثبت نشده 👌"
