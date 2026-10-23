"""تست‌های بخش توزیع با ربات و دیتابیسِ ساختگی (بدون تلگرام و MySQL واقعی).  اجرا:  python -m unittest discover -s tests -v"""
import asyncio
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

from app.features.distribution import logic
from app.features.distribution.botapi import BotApi, BotApiError, BotApiNetworkError
from app.features.distribution.health import check_bot
from app.features.distribution.poster import PostPublisher, enqueue_post
from app.features.distribution.receiver import (
    DeleteScheduler, ReceiverService, T_INVALID, T_CHECK_FAILED, T_SEND_UNKNOWN, T_NEED_JOIN, T_WELCOME)

T0 = datetime(2026, 10, 23, 12, 0, 0)


# ====================== ابزارهای ساختگی ======================
class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class FakeApi:
    def __init__(self):
        self.calls = []                  # (method, args, kwargs)
        self.members = {}                # (chat_id, user_id) -> dict یا Exception
        self.copy_effects = []           # صف رفتار copy: Exception یا None
        self.copy_delay = 0
        self.delete_effects = []
        self._mid = 1000

    def _next(self):
        self._mid += 1
        return self._mid

    def _log(self, name, *a, **k):
        self.calls.append((name, a, k))

    def of(self, name):
        return [c for c in self.calls if c[0] == name]

    async def get_chat_member(self, chat_id, user_id):
        self._log("get_chat_member", chat_id, user_id)
        v = self.members.get((chat_id, user_id), {"status": "left"})
        if isinstance(v, Exception):
            raise v
        return v

    async def copy_message(self, chat_id, from_chat_id, message_id, caption=None, reply_markup=None):
        self._log("copy_message", chat_id, from_chat_id, message_id, caption=caption, reply_markup=reply_markup)
        if self.copy_delay:
            await asyncio.sleep(self.copy_delay)
        if self.copy_effects:
            eff = self.copy_effects.pop(0)
            if eff:
                raise eff
        return {"message_id": self._next()}

    async def send_message(self, chat_id, text, reply_markup=None, reply_to_message_id=None):
        self._log("send_message", chat_id, text, reply_markup=reply_markup, reply_to=reply_to_message_id)
        return {"message_id": self._next()}

    async def delete_message(self, chat_id, message_id):
        self._log("delete_message", chat_id, message_id)
        if self.delete_effects:
            eff = self.delete_effects.pop(0)
            if eff:
                raise eff
        return True

    async def answer_callback_query(self, cb_id, text=None, alert=False):
        self._log("answer", cb_id, text, alert)
        return True


class FakeStore:
    """همان رابط DistStore؛ منطق چرخشی و وضعیت‌ها همان‌هاست (از pick_next_bot واقعی استفاده می‌کند)."""
    def __init__(self, clock):
        self.clock = clock
        self.settings = {}
        self.bots = {}
        self.posts = {}
        self.forced = []
        self.deliveries = {}
        self._pid = 0
        self._did = 0

    # تنظیمات
    async def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    async def set_setting(self, key, value):
        self.settings[key] = value

    # ربات‌ها
    def add_bot(self, pk, username, active=True):
        self.bots[pk] = NS(id=pk, bot_id=pk * 111, username=username, is_active=active, last_assigned_seq=0,
                           assigned_count=0, request_count=0, error_count=0)
        return self.bots[pk]

    async def bump_bot(self, pk, requests=0, errors=0):
        self.bots[pk].request_count += requests
        self.bots[pk].error_count += errors

    async def list_forced(self, only_active=False):
        return [c for c in self.forced if c.is_active or not only_active]

    # پست‌ها
    async def enqueue_post(self, delivery_id, storage_chat_id, storage_msg_id, caption, token):
        if any(p.delivery_id == delivery_id for p in self.posts.values()):
            return False
        self._pid += 1
        self.posts[self._pid] = NS(id=self._pid, delivery_id=delivery_id, token=token, storage_chat_id=storage_chat_id,
                                   storage_msg_id=storage_msg_id, caption=caption, final_caption=None,
                                   caption_trimmed=False, status="pending", assigned_bot_id=None, attempts=0,
                                   next_attempt_at=None, error_message=None, final_chat_id=None, final_msg_id=None)
        return True

    def add_published_post(self, token, bot_pk, storage_chat=-1001, storage_msg=77, final_caption="کپشن نهایی"):
        self._pid += 1
        self.posts[self._pid] = NS(id=self._pid, delivery_id=self._pid, token=token, storage_chat_id=storage_chat,
                                   storage_msg_id=storage_msg, caption="c", final_caption=final_caption,
                                   caption_trimmed=False, status="published", assigned_bot_id=bot_pk, attempts=1,
                                   next_attempt_at=None, error_message=None, final_chat_id=-1002, final_msg_id=5)
        return self.posts[self._pid]

    async def claim_next_post(self, now):
        ready = sorted((p for p in self.posts.values() if p.status == "pending"
                        and (p.next_attempt_at is None or p.next_attempt_at <= now)), key=lambda p: p.id)
        if not ready:
            return None
        bot = logic.pick_next_bot(self.bots.values())
        if bot is None:
            return None
        bot.last_assigned_seq = max(b.last_assigned_seq for b in self.bots.values()) + 1
        bot.assigned_count += 1
        p = ready[0]
        p.status, p.assigned_bot_id = "publishing", bot.id
        p.attempts += 1
        return p, bot

    async def mark_post_published(self, post_id, chat_id, msg_id, final_caption, trimmed, now):
        p = self.posts[post_id]
        p.status, p.final_chat_id, p.final_msg_id, p.final_caption, p.caption_trimmed = \
            "published", chat_id, msg_id, final_caption, trimmed

    async def mark_post_retry(self, post_id, error, retry_at):
        p = self.posts[post_id]
        p.status, p.error_message, p.next_attempt_at = "pending", error, retry_at

    async def mark_post_failed(self, post_id, error):
        self.posts[post_id].status, self.posts[post_id].error_message = "failed", error

    async def mark_post_unknown(self, post_id, error):
        self.posts[post_id].status, self.posts[post_id].error_message = "unknown", error

    async def get_post_by_token(self, token):
        return next((p for p in self.posts.values() if p.token == token), None)

    # تحویل
    async def recent_delivery_exists(self, user_id, media_post_id, seconds):
        since = self.clock() - timedelta(seconds=seconds)
        return any(d.user_id == user_id and d.media_post_id == media_post_id and d.status in ("sending", "sent")
                   and d.created_at >= since for d in self.deliveries.values())

    async def create_delivery(self, media_post_id, bot_pk, user_id):
        self._did += 1
        self.deliveries[self._did] = NS(id=self._did, media_post_id=media_post_id, bot_id=bot_pk, user_id=user_id,
                                        video_msg_id=None, guide_msg_id=None, status="sending", delete_at=None,
                                        delete_attempts=0, error=None, created_at=self.clock())
        return self._did

    async def mark_delivery(self, did, status, error=None):
        self.deliveries[did].status, self.deliveries[did].error = status, error

    async def mark_delivery_sent(self, did, video_msg_id, delete_at, sent_at):
        d = self.deliveries[did]
        d.status, d.video_msg_id, d.delete_at = "sent", video_msg_id, delete_at

    async def set_guide(self, did, guide_id):
        self.deliveries[did].guide_msg_id = guide_id

    async def has_guide(self, user_id, media_post_id):
        return any(d.user_id == user_id and d.media_post_id == media_post_id and d.guide_msg_id
                   for d in self.deliveries.values())

    async def due_deletions(self, now, limit):
        return [d for d in self.deliveries.values() if d.status == "sent" and d.delete_at <= now][:limit]

    async def mark_deleted(self, did, now):
        self.deliveries[did].status = "deleted"

    async def reschedule_delete(self, did, attempts, next_at):
        self.deliveries[did].delete_attempts, self.deliveries[did].delete_at = attempts, next_at

    async def mark_delete_failed(self, did, error):
        self.deliveries[did].status, self.deliveries[did].error = "delete_failed", error


def start_update(user, token, chat_type="private"):
    return {"message": {"message_id": 1, "from": {"id": user}, "chat": {"id": user, "type": chat_type},
                        "text": f"/start v_{token}"}}


def cb_update(user, data, cb_id="cb1"):
    return {"callback_query": {"id": cb_id, "from": {"id": user}, "data": data,
                               "message": {"message_id": 9, "chat": {"id": user, "type": "private"}}}}


CH_A = NS(id=1, chat_id=-1001, title="کانال A", link="https://t.me/chanA", is_active=True)
CH_B = NS(id=2, chat_id=-1002, title="کانال B", link="https://t.me/+secretB", is_active=True)


# ====================== منطق خالص ======================
class LogicTests(unittest.TestCase):
    def test_token_and_payload(self):
        t = logic.make_token()
        self.assertEqual(len(t), 16)
        self.assertEqual(logic.parse_payload(logic.build_payload(t)), t)
        self.assertLessEqual(len(logic.build_payload(t)), 64)
        self.assertEqual(logic.build_deep_link("MyBot", t), f"https://t.me/MyBot?start=v_{t}")
        self.assertNotEqual(logic.make_token(), logic.make_token())

    def test_invalid_payloads(self):
        t = logic.make_token()
        for bad in ["", None, "v_short", f"x_{t}", f"v_{t}x", f"v_{t[:-1]}!", "v_" + "a'; DROP" + "b" * 8]:
            self.assertIsNone(logic.parse_payload(bad), bad)
        with self.assertRaises(ValueError):
            logic.build_payload("bad token")

    def test_parse_start(self):
        self.assertEqual(logic.parse_start_command("/start abc"), "abc")
        self.assertEqual(logic.parse_start_command("/start@MyBot abc"), "abc")
        self.assertEqual(logic.parse_start_command("/start"), "")
        self.assertIsNone(logic.parse_start_command("hello"))
        self.assertIsNone(logic.parse_start_command(None))

    def test_compose_plain(self):
        text, trimmed = logic.compose_caption("{caption}\n\n{promo}", "سلام", "@ch")
        self.assertEqual(text, "سلام\n\n@ch")
        self.assertFalse(trimmed)

    def test_compose_no_nested_substitution(self):
        text, _ = logic.compose_caption("{caption}|{promo}", "x{promo}y", "P")
        self.assertEqual(text, "x{promo}y|P")

    def test_compose_trims_long_caption_and_flags(self):
        cap = "الف" * 1000
        text, trimmed = logic.compose_caption("{caption}\n\n{promo}", cap, "@my_channel")
        self.assertTrue(trimmed)
        self.assertLessEqual(logic.u16len(text), 1024)
        self.assertTrue(text.endswith("@my_channel"))
        self.assertIn("…", text)

    def test_compose_emoji_counts_utf16(self):
        cap = "😀" * 600     # هر ایموجی ۲ واحد UTF-16
        text, trimmed = logic.compose_caption("{caption}", cap, "")
        self.assertTrue(trimmed)
        self.assertLessEqual(logic.u16len(text), 1024)

    def test_compose_template_too_long(self):
        with self.assertRaises(logic.TemplateTooLong):
            logic.compose_caption("{caption}\n{promo}", "x", "p" * 1100)
        with self.assertRaises(logic.TemplateTooLong):
            logic.compose_caption("{promo}", "x", "p" * 1100)

    def test_validate_template(self):
        self.assertTrue(logic.validate_template("{caption}\n{promo}")[0])
        self.assertTrue(logic.validate_template("فقط متن")[0])
        for bad in ["", "  ", "{title}", "{caption", "caption}", "{{caption}}", "{}"]:
            self.assertFalse(logic.validate_template(bad)[0], bad)

    def test_button_text(self):
        self.assertTrue(logic.validate_button_text("📥 دریافت")[0])
        self.assertFalse(logic.validate_button_text(" ")[0])
        self.assertFalse(logic.validate_button_text("x" * 41)[0])

    def test_delete_minutes(self):
        self.assertEqual(logic.parse_delete_minutes("60"), 60)
        for bad in ["0", "2821", "-5", "abc", "1.5", ""]:
            with self.assertRaises(ValueError):
                logic.parse_delete_minutes(bad)
        self.assertEqual(logic.format_duration(30), "30 دقیقه")
        self.assertEqual(logic.format_duration(120), "2 ساعت")
        self.assertEqual(logic.format_duration(90), "1 ساعت و 30 دقیقه")

    def test_member_status(self):
        f = logic.interpret_member_status
        self.assertIs(f({"status": "member"}), True)
        self.assertIs(f({"status": "creator"}), True)
        self.assertIs(f({"status": "left"}), False)
        self.assertIs(f({"status": "kicked"}), False)
        self.assertIs(f({"status": "restricted", "is_member": True}), True)
        self.assertIs(f({"status": "restricted", "is_member": False}), False)
        self.assertIsNone(f({"status": "weird"}))
        self.assertIsNone(f(None))

    def test_membership_verdict(self):
        e = logic.evaluate_membership
        self.assertEqual(e([]).status, "ok")
        self.assertEqual(e([("a", True), ("b", True)]).status, "ok")
        self.assertEqual(e([("a", True), ("b", None)]).status, "unknown")
        r = e([("a", False), ("b", None)])
        self.assertEqual((r.status, r.missing, r.unknown), ("missing", ["a"], ["b"]))

    def test_round_robin_even_and_ordered(self):
        bots = [NS(id=i, is_active=True, last_assigned_seq=0) for i in (1, 2, 3)]
        order = []
        for _ in range(9):
            b = logic.pick_next_bot(bots)
            b.last_assigned_seq = max(x.last_assigned_seq for x in bots) + 1
            order.append(b.id)
        self.assertEqual(order, [1, 2, 3] * 3)

    def test_round_robin_skips_inactive_and_none(self):
        bots = [NS(id=1, is_active=False, last_assigned_seq=0), NS(id=2, is_active=True, last_assigned_seq=5)]
        self.assertEqual(logic.pick_next_bot(bots).id, 2)
        self.assertIsNone(logic.pick_next_bot([NS(id=1, is_active=False, last_assigned_seq=0)]))

    def test_bot_permissions(self):
        self.assertTrue(logic.bot_can_post({"status": "administrator", "can_post_messages": True}))
        self.assertFalse(logic.bot_can_post({"status": "administrator", "can_post_messages": False}))
        self.assertTrue(logic.bot_can_post({"status": "creator"}))
        self.assertFalse(logic.bot_can_post({"status": "member"}))


# ====================== کلاینت Bot API ======================
class BotApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_flood_wait_retry(self):
        seq = [(429, {"ok": False, "error_code": 429, "description": "Too Many Requests",
                      "parameters": {"retry_after": 3}}), (200, {"ok": True, "result": {"message_id": 5}})]
        slept = []

        async def transport(url, params, timeout):
            return seq.pop(0)

        async def sleep(s):
            slept.append(s)

        api = BotApi("TOKEN123", transport=transport, sleep=sleep)
        self.assertEqual((await api.copy_message(1, 2, 3))["message_id"], 5)
        self.assertEqual(slept, [4])

    async def test_error_mapping_hides_token(self):
        async def transport(url, params, timeout):
            return 400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}

        api = BotApi("SECRET_TOKEN", transport=transport)
        with self.assertRaises(BotApiError) as cm:
            await api.get_chat(1)
        self.assertEqual(cm.exception.code, 400)
        self.assertNotIn("SECRET_TOKEN", str(cm.exception))

    async def test_network_error_not_retried_for_sends(self):
        n = {"c": 0}

        async def transport(url, params, timeout):
            n["c"] += 1
            raise OSError("https://api.telegram.org/botSECRET_TOKEN/x failed")

        api = BotApi("SECRET_TOKEN", transport=transport, sleep=lambda s: asyncio.sleep(0))
        with self.assertRaises(BotApiNetworkError) as cm:
            await api.copy_message(1, 2, 3)
        self.assertEqual(n["c"], 1)                       # ارسال خودکار تکرار نمی‌شود
        self.assertNotIn("SECRET_TOKEN", str(cm.exception))
        self.assertIsNone(cm.exception.__cause__)

    async def test_network_error_retried_for_reads(self):
        n = {"c": 0}

        async def transport(url, params, timeout):
            n["c"] += 1
            raise OSError("x")

        api = BotApi("T", transport=transport, sleep=lambda s: asyncio.sleep(0))
        with self.assertRaises(BotApiNetworkError):
            await api.get_chat_member(1, 2)
        self.assertEqual(n["c"], 3)

    async def test_none_params_dropped(self):
        seen = {}

        async def transport(url, params, timeout):
            seen.update(params)
            return 200, {"ok": True, "result": True}

        await BotApi("T", transport=transport).delete_message(5, 6)
        await BotApi("T", transport=transport).copy_message(1, 2, 3)
        self.assertNotIn("caption", seen)
        self.assertNotIn("reply_markup", seen)


# ====================== ربات دریافت ======================
class ReceiverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = FakeStore(self.clock)
        self.store.add_bot(1, "RecvBot")
        self.token = logic.make_token()
        self.post = self.store.add_published_post(self.token, 1)
        self.store.forced = [CH_A, CH_B]
        self.api = FakeApi()
        self.svc = ReceiverService(self.api, 1, self.store, now_fn=self.clock)
        self.user = 555

    def all_member(self):
        for ch in (CH_A, CH_B):
            self.api.members[(ch.chat_id, self.user)] = {"status": "member"}

    def texts(self):
        return [c[1][1] for c in self.api.of("send_message")]

    async def test_welcome_and_invalid(self):
        await self.svc.handle_update({"message": {"from": {"id": 5}, "chat": {"id": 5, "type": "private"}, "text": "/start"}})
        await self.svc.handle_update({"message": {"from": {"id": 5}, "chat": {"id": 5, "type": "private"}, "text": "/start v_bad"}})
        self.assertEqual(self.texts(), [T_WELCOME, T_INVALID])
        self.assertEqual(self.api.of("copy_message"), [])

    async def test_group_chat_ignored(self):
        await self.svc.handle_update(start_update(self.user, self.token, chat_type="supergroup"))
        self.assertEqual(self.api.calls, [])

    async def test_unpublished_or_other_bot_rejected(self):
        self.post.status = "pending"
        await self.svc.handle_update(start_update(self.user, self.token))
        self.post.status, self.post.assigned_bot_id = "published", 2
        await self.svc.handle_update(start_update(self.user, self.token))
        self.assertEqual(self.texts(), [T_INVALID, T_INVALID])
        self.assertEqual(self.api.of("copy_message"), [])

    async def test_not_member_shows_only_missing_and_does_not_send(self):
        self.api.members[(CH_A.chat_id, self.user)] = {"status": "member"}
        self.api.members[(CH_B.chat_id, self.user)] = {"status": "left"}
        await self.svc.handle_update(start_update(self.user, self.token))
        self.assertEqual(self.api.of("copy_message"), [])
        msg = self.api.of("send_message")[0]
        self.assertIn(T_NEED_JOIN, msg[1][1])
        self.assertIn("کانال B", msg[1][1])
        self.assertNotIn("کانال A", msg[1][1])
        rows = msg[2]["reply_markup"]["inline_keyboard"]
        self.assertEqual(rows[0][0]["url"], CH_B.link)
        self.assertEqual(rows[-1][0]["callback_data"], f"chk:{self.token}")

    async def test_unknown_membership_neither_sends_nor_claims(self):
        self.api.members[(CH_A.chat_id, self.user)] = BotApiError(400, "Bad Request: member list is inaccessible")
        self.api.members[(CH_B.chat_id, self.user)] = {"status": "member"}
        await self.svc.handle_update(start_update(self.user, self.token))
        self.assertEqual(self.api.of("copy_message"), [])
        self.assertEqual(self.texts(), [T_CHECK_FAILED])
        self.assertGreaterEqual(self.store.bots[1].error_count, 1)

    async def test_member_gets_video_guide_and_delete_time(self):
        self.all_member()
        await self.svc.handle_update(start_update(self.user, self.token))
        copies = self.api.of("copy_message")
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0][1], (self.user, -1001, 77))
        self.assertEqual(copies[0][2]["caption"], "کپشن نهایی")
        d = self.store.deliveries[1]
        self.assertEqual(d.status, "sent")
        self.assertEqual(d.delete_at, T0 + timedelta(minutes=60))       # پیش‌فرض ۶۰ دقیقه
        guide = self.api.of("send_message")[0]
        self.assertIn("حذف می‌شود", guide[1][1])
        self.assertEqual(guide[2]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], f"re:{self.token}")
        self.assertIsNotNone(d.guide_msg_id)

    async def test_custom_delete_time(self):
        self.all_member()
        self.store.settings["delete_after_min"] = "5"
        await self.svc.handle_update(start_update(self.user, self.token))
        self.assertEqual(self.store.deliveries[1].delete_at, T0 + timedelta(minutes=5))

    async def test_redownload_rechecks_membership_and_has_independent_timer(self):
        self.all_member()
        await self.svc.handle_update(start_update(self.user, self.token))
        self.clock.advance(minutes=61)
        calls_before = len(self.api.of("get_chat_member"))
        await self.svc.handle_update(cb_update(self.user, f"re:{self.token}"))
        self.assertGreater(len(self.api.of("get_chat_member")), calls_before)    # عضویت دوباره بررسی شد
        self.assertEqual(len(self.api.of("copy_message")), 2)
        self.assertEqual(len([c for c in self.api.of("send_message")]), 1)      # راهنمای تکراری نمی‌آید
        self.assertEqual(self.store.deliveries[2].delete_at, self.clock() + timedelta(minutes=60))
        self.assertNotEqual(self.store.deliveries[1].delete_at, self.store.deliveries[2].delete_at)

    async def test_redownload_blocked_if_user_left(self):
        self.all_member()
        await self.svc.handle_update(start_update(self.user, self.token))
        self.clock.advance(minutes=61)
        self.api.members[(CH_A.chat_id, self.user)] = {"status": "left"}
        await self.svc.handle_update(cb_update(self.user, f"re:{self.token}"))
        self.assertEqual(len(self.api.of("copy_message")), 1)

    async def test_check_button_after_joining(self):
        self.api.members[(CH_A.chat_id, self.user)] = {"status": "left"}
        self.api.members[(CH_B.chat_id, self.user)] = {"status": "member"}
        await self.svc.handle_update(start_update(self.user, self.token))
        self.assertEqual(self.api.of("copy_message"), [])
        self.api.members[(CH_A.chat_id, self.user)] = {"status": "member"}
        await self.svc.handle_update(cb_update(self.user, f"chk:{self.token}"))
        self.assertEqual(len(self.api.of("copy_message")), 1)

    async def test_concurrent_double_click_sends_once(self):
        self.all_member()
        self.api.copy_delay = 0.05
        await asyncio.gather(self.svc.handle_update(cb_update(self.user, f"re:{self.token}", "a")),
                             self.svc.handle_update(cb_update(self.user, f"re:{self.token}", "b")))
        self.assertEqual(len(self.api.of("copy_message")), 1)

    async def test_fast_second_click_after_finish_is_debounced(self):
        self.all_member()
        await self.svc.handle_update(cb_update(self.user, f"re:{self.token}"))
        await self.svc.handle_update(cb_update(self.user, f"re:{self.token}"))     # همان لحظه (ساعت ثابت)
        self.assertEqual(len(self.api.of("copy_message")), 1)

    async def test_bad_callback_data_ignored(self):
        await self.svc.handle_update(cb_update(self.user, "re:../../etc"))
        await self.svc.handle_update(cb_update(self.user, "zzz:" + self.token))
        self.assertEqual(self.api.of("copy_message"), [])
        self.assertEqual(self.api.of("get_chat_member"), [])

    async def test_ambiguous_send_marks_unknown_and_never_auto_resends(self):
        self.all_member()
        self.api.copy_effects = [BotApiNetworkError("TimeoutError")]
        await self.svc.handle_update(start_update(self.user, self.token))
        self.assertEqual(len(self.api.of("copy_message")), 1)
        self.assertEqual(self.store.deliveries[1].status, "unknown")
        self.assertIsNone(self.store.deliveries[1].delete_at)
        self.assertIn(T_SEND_UNKNOWN, self.texts())

    async def test_send_failure_recorded(self):
        self.all_member()
        self.api.copy_effects = [BotApiError(400, "Bad Request: message to copy not found")]
        await self.svc.handle_update(start_update(self.user, self.token))
        self.assertEqual(self.store.deliveries[1].status, "send_failed")
        self.assertEqual(self.store.bots[1].error_count, 1)

    async def test_blocked_user_not_counted_as_error(self):
        self.all_member()
        self.api.copy_effects = [BotApiError(403, "Forbidden: bot was blocked by the user")]
        await self.svc.handle_update(start_update(self.user, self.token))
        self.assertEqual(self.store.deliveries[1].status, "send_failed")
        self.assertEqual(self.store.bots[1].error_count, 0)

    async def test_no_forced_channels_means_direct_send(self):
        self.store.forced = []
        await self.svc.handle_update(start_update(self.user, self.token))
        self.assertEqual(len(self.api.of("copy_message")), 1)


# ====================== حذف زمان‌دار ======================
class DeleteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = FakeStore(self.clock)
        self.api = FakeApi()

        async def api_for(pk):
            return self.api

        self.sched = DeleteScheduler(self.store, api_for, now_fn=self.clock)

    async def add(self, minutes):
        did = await self.store.create_delivery(1, 1, 555)
        await self.store.mark_delivery_sent(did, 900 + did, self.clock() + timedelta(minutes=minutes), self.clock())
        return did

    async def test_only_due_messages_deleted_and_only_that_message(self):
        a, b = await self.add(10), await self.add(120)
        self.clock.advance(minutes=11)
        await self.sched.run_once()
        self.assertEqual(self.api.of("delete_message"), [("delete_message", (555, 900 + a), {})])
        self.assertEqual(self.store.deliveries[a].status, "deleted")
        self.assertEqual(self.store.deliveries[b].status, "sent")

    async def test_survives_restart_new_scheduler_same_db(self):
        a = await self.add(10)
        self.clock.advance(minutes=30)        # «سرویس خاموش بود»
        async def api_for(pk):
            return self.api
        fresh = DeleteScheduler(self.store, api_for, now_fn=self.clock)
        await fresh.run_once()
        self.assertEqual(self.store.deliveries[a].status, "deleted")

    async def test_retry_with_backoff_then_give_up(self):
        a = await self.add(1)
        self.clock.advance(minutes=2)
        self.api.delete_effects = [BotApiNetworkError("x")] * 10
        for i in range(DeleteScheduler.MAX_ATTEMPTS):
            await self.sched.run_once()
            if self.store.deliveries[a].status == "delete_failed":
                break
            self.assertGreater(self.store.deliveries[a].delete_at, self.clock())   # عقب افتاد
            self.clock.advance(minutes=20)
        self.assertEqual(self.store.deliveries[a].status, "delete_failed")
        self.assertEqual(len(self.api.of("delete_message")), DeleteScheduler.MAX_ATTEMPTS)

    async def test_already_deleted_counts_as_deleted(self):
        a = await self.add(1)
        self.clock.advance(minutes=2)
        self.api.delete_effects = [BotApiError(400, "Bad Request: message to delete not found")]
        await self.sched.run_once()
        self.assertEqual(self.store.deliveries[a].status, "deleted")

    async def test_cannot_delete_old_message_marked_failed(self):
        a = await self.add(1)
        self.clock.advance(minutes=2)
        self.api.delete_effects = [BotApiError(400, "Bad Request: message can't be deleted for everyone")]
        await self.sched.run_once()
        self.assertEqual(self.store.deliveries[a].status, "delete_failed")


# ====================== انتشار در کانال نهایی ======================
class PosterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = FakeStore(self.clock)
        self.store.settings["final_chat_id"] = "-100999"
        self.apis = {}

        async def api_for(pk):
            return self.apis.setdefault(pk, FakeApi())

        self.pub = PostPublisher(self.store, api_for, now_fn=self.clock)

    async def enq(self, n, caption="cap"):
        for i in range(1, n + 1):
            self.assertTrue(await enqueue_post(self.store, 100 + i, -1001, 50 + i, caption))

    def post_of(self, i):
        return self.store.posts[i]

    async def test_round_robin_across_two_bots_and_deep_links(self):
        self.store.add_bot(1, "BotOne")
        self.store.add_bot(2, "BotTwo")
        await self.enq(4)
        while await self.pub.run_once():
            pass
        self.assertEqual([self.post_of(i).assigned_bot_id for i in (1, 2, 3, 4)], [1, 2, 1, 2])
        self.assertTrue(all(self.post_of(i).status == "published" for i in (1, 2, 3, 4)))
        call = self.apis[1].of("copy_message")[0]
        self.assertEqual(call[1], (-100999, -1001, 51))
        url = call[2]["reply_markup"]["inline_keyboard"][0][0]["url"]
        self.assertEqual(url, f"https://t.me/BotOne?start=v_{self.post_of(1).token}")
        url2 = self.apis[2].of("copy_message")[0][2]["reply_markup"]["inline_keyboard"][0][0]["url"]
        self.assertTrue(url2.startswith("https://t.me/BotTwo?start=v_"))
        self.assertNotEqual(self.post_of(1).token, self.post_of(2).token)

    async def test_inactive_bot_skipped_and_counters(self):
        self.store.add_bot(1, "BotOne")
        self.store.add_bot(2, "BotTwo", active=False)
        await self.enq(3)
        while await self.pub.run_once():
            pass
        self.assertEqual({self.post_of(i).assigned_bot_id for i in (1, 2, 3)}, {1})
        self.assertEqual(self.store.bots[1].assigned_count, 3)
        self.assertEqual(self.store.bots[2].assigned_count, 0)

    async def test_no_active_bot_keeps_queue(self):
        self.store.add_bot(1, "BotOne", active=False)
        await self.enq(2)
        self.assertFalse(await self.pub.run_once())
        self.assertEqual([self.post_of(i).status for i in (1, 2)], ["pending", "pending"])
        self.assertEqual(self.apis, {})
        self.store.bots[1].is_active = True                       # با فعال‌شدن ربات، صف خالی می‌شود
        while await self.pub.run_once():
            pass
        self.assertEqual([self.post_of(i).status for i in (1, 2)], ["published", "published"])

    async def test_no_final_channel_keeps_queue(self):
        self.store.add_bot(1, "BotOne")
        del self.store.settings["final_chat_id"]
        await self.enq(1)
        self.assertFalse(await self.pub.run_once())
        self.assertEqual(self.post_of(1).status, "pending")

    async def test_enqueue_is_idempotent(self):
        self.assertTrue(await enqueue_post(self.store, 7, -1, 1, "a"))
        self.assertFalse(await enqueue_post(self.store, 7, -1, 1, "a"))
        self.assertEqual(len(self.store.posts), 1)

    async def test_ambiguous_result_is_not_resent(self):
        self.store.add_bot(1, "BotOne")
        await self.enq(1)
        api = self.apis.setdefault(1, FakeApi())
        api.copy_effects = [BotApiNetworkError("TimeoutError")]
        await self.pub.run_once()
        self.assertEqual(self.post_of(1).status, "unknown")
        self.clock.advance(hours=1)
        self.assertFalse(await self.pub.run_once())
        self.assertEqual(len(api.of("copy_message")), 1)

    async def test_failure_retries_with_next_bot_then_fails(self):
        self.store.add_bot(1, "BotOne")
        self.store.add_bot(2, "BotTwo")
        await self.enq(1)
        for pk in (1, 2):
            self.apis.setdefault(pk, FakeApi()).copy_effects = [BotApiError(400, "Bad Request: not enough rights")] * 5
        await self.pub.run_once()
        p = self.post_of(1)
        self.assertEqual((p.status, p.attempts), ("pending", 1))
        self.assertEqual(p.next_attempt_at, T0 + timedelta(seconds=60))
        self.assertFalse(await self.pub.run_once())               # هنوز زمانش نرسیده
        for expected_attempts in (2, 3):
            self.clock.advance(seconds=61)
            await self.pub.run_once()
        self.assertEqual((p.status, p.attempts), ("failed", 3))
        self.assertIn("not enough rights", p.error_message)
        self.assertEqual(self.apis[1].of("copy_message")[0][1][0], -100999)
        self.assertEqual(len(self.apis[2].of("copy_message")), 1)  # تلاش دوم با ربات بعدی

    async def test_long_caption_trimmed_and_full_text_sent_separately(self):
        self.store.add_bot(1, "BotOne")
        self.store.settings["tpl_promo"] = "@my_channel"
        await self.enq(1, caption="ب" * 1020)
        await self.pub.run_once()
        p = self.post_of(1)
        self.assertEqual(p.status, "published")
        self.assertTrue(p.caption_trimmed)
        self.assertLessEqual(logic.u16len(p.final_caption), 1024)
        sent = self.apis[1].of("send_message")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1][1], "ب" * 1020)                 # متن کامل بی‌صدا حذف نشد
        self.assertEqual(sent[0][2]["reply_to"], p.final_msg_id)

    async def test_template_too_long_fails_without_sending(self):
        self.store.add_bot(1, "BotOne")
        self.store.settings["tpl_promo"] = "p" * 1100
        await self.enq(1)
        await self.pub.run_once()
        self.assertEqual(self.post_of(1).status, "failed")
        self.assertEqual(self.apis.get(1, FakeApi()).of("copy_message"), [])

    async def test_custom_template_and_button(self):
        self.store.add_bot(1, "BotOne")
        self.store.settings.update({"tpl_caption": "🎬 {caption}\n{promo}", "tpl_promo": "@ads", "tpl_button": "دانلود"})
        await self.enq(1, caption="فیلم")
        await self.pub.run_once()
        call = self.apis[1].of("copy_message")[0]
        self.assertEqual(call[2]["caption"], "🎬 فیلم\n@ads")
        self.assertEqual(call[2]["reply_markup"]["inline_keyboard"][0][0]["text"], "دانلود")


# ====================== سلامت ربات ======================
class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_checks(self):
        class Api:
            async def get_me(self):
                return {"username": "RecvBot"}

            async def get_chat_member(self, chat_id, user_id):
                if chat_id == -1:      # مخزن: ادمین
                    return {"status": "administrator"}
                if chat_id == -2:      # کانال نهایی: ادمین بدون اجازه‌ی ارسال
                    return {"status": "administrator", "can_post_messages": False}
                if chat_id == -3:      # عضویت اجباری: خطا
                    raise BotApiError(400, "Bad Request: chat not found")
                return {"status": "administrator"}

        forced = [NS(chat_id=-3, title="X"), NS(chat_id=-4, title="Y")]
        checks = await check_bot(Api(), 11, -1, -2, forced)
        ok = {c.label: c.ok for c in checks}
        self.assertTrue(ok["دسترسی به مخزن"])
        self.assertFalse(ok["ارسال در کانال نهایی"])
        self.assertFalse(ok["بررسی عضویت «X»"])
        self.assertTrue(ok["بررسی عضویت «Y»"])
        self.assertIn("chat not found", [c for c in checks if "«X»" in c.label][0].detail)

    async def test_bad_token_stops_early(self):
        class Api:
            async def get_me(self):
                raise BotApiError(401, "Unauthorized")

        checks = await check_bot(Api(), 1, -1, None, [])
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0].ok)


if __name__ == "__main__":
    unittest.main()
