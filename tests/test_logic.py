"""تست‌های منطق خالص (بدون نیاز به تلگرام و دیتابیس).  اجرا:  python -m unittest discover -s tests -v"""
import unittest
from types import SimpleNamespace as NS

from app.core.link_parser import link_parser
from app.features.detector.bot_detector import bot_detector
from app.features.detector.analyzer import analyzer
from app.features.processor.caption_cleaner import caption_cleaner
from app.features.bot_engine.helpers import (
    choose_button, pick_bot_message, join_target, build_start_text, get_media, reply_keyboard_texts, parse_destination_input)


def ent(kind, offset, length, url=None):
    return NS(type=NS(name=kind), offset=offset, length=length, url=url)   # شبیه enum پایروگرام


def post(text=None, caption=None, entities=None, caption_entities=None, buttons=None):
    markup = NS(inline_keyboard=[[NS(text="t", url=u) for u in buttons]]) if buttons else None
    return NS(text=text, caption=caption, entities=entities, caption_entities=caption_entities, reply_markup=markup)


class LinkParserTests(unittest.TestCase):
    def test_bot_with_start_payload(self):
        r = link_parser.extract_bot("https://t.me/File_Getter_bot?start=abc123")
        self.assertEqual(r, {"username": "File_Getter_bot", "start_param": "abc123"})

    def test_bot_without_underscore(self):
        self.assertEqual(link_parser.extract_bot_username("t.me/SomeFileBot"), "SomeFileBot")

    def test_tg_scheme(self):
        r = link_parser.extract_bot("tg://resolve?domain=my_bot&start=xyz")
        self.assertEqual(r["start_param"], "xyz")

    def test_channel_and_private(self):
        self.assertEqual(link_parser.parse_telegram_link("https://t.me/mychannel")["type"], "channel")
        p = link_parser.parse_telegram_link("https://t.me/+AbCdEf123")
        self.assertEqual((p["type"], p["invite_hash"]), ("private_channel", "AbCdEf123"))
        p = link_parser.parse_telegram_link("https://t.me/joinchat/AbCdEf123")
        self.assertEqual(p["invite_hash"], "AbCdEf123")

    def test_trailing_slash_and_params(self):
        self.assertEqual(link_parser.parse_telegram_link("https://t.me/mychannel/?x=1")["type"], "channel")

    def test_ignores_non_telegram_and_reserved(self):
        self.assertIsNone(link_parser.parse_telegram_link("https://example.com/x_bot"))
        self.assertIsNone(link_parser.parse_telegram_link("https://t.me/addstickers/pack"))
        self.assertIsNone(link_parser.parse_telegram_link("https://t.me/c/123/456"))
        self.assertIsNone(link_parser.parse_telegram_link(""))


class BotDetectorTests(unittest.TestCase):
    def test_button_url_wins_with_payload(self):
        m = post(text="hi", buttons=["https://t.me/x_bot?start=p1"])
        self.assertEqual(bot_detector.extract_bot(m), {"username": "x_bot", "start_param": "p1"})

    def test_caption_of_media_post(self):
        text = "ببین https://t.me/vid_bot?start=77 عالیه"
        start = text.index("https")
        m = post(caption=text, caption_entities=[ent("URL", start, len("https://t.me/vid_bot?start=77"))])
        self.assertEqual(bot_detector.extract_bot(m)["start_param"], "77")

    def test_text_link_entity(self):
        m = post(text="دانلود", entities=[ent("TEXT_LINK", 0, 5, "https://t.me/dl_bot?start=k")])
        self.assertEqual(bot_detector.extract_bot_username(m), "dl_bot")

    def test_emoji_before_link_utf16_offsets(self):
        text = "😀😀 https://t.me/emo_bot?start=z"
        # هر ایموجی در UTF-16 دو واحد است: 2*2 + 1 فاصله = 5
        m = post(text=text, entities=[ent("URL", 5, len("https://t.me/emo_bot?start=z"))])
        self.assertEqual(bot_detector.extract_bot_username(m), "emo_bot")

    def test_plain_mention_and_none(self):
        self.assertEqual(bot_detector.extract_bot_username(post(text="get it @MyFileBot now")), "MyFileBot")
        self.assertIsNone(bot_detector.extract_bot_username(post(text="no bot here @channel")))
        self.assertFalse(analyzer.analyze_message(post(text="hello"))["is_media_request"])
        self.assertTrue(analyzer.analyze_message(post(text="x @a_bot"))["is_media_request"])


class CaptionCleanerTests(unittest.TestCase):
    def test_removes_links_mentions_keeps_text(self):
        out = caption_cleaner.clean("فیلم جدید\nhttps://t.me/x_bot?start=1\n@promo_channel\nدانلود کنید")
        self.assertNotIn("t.me", out)
        self.assertNotIn("@", out)
        self.assertIn("فیلم جدید", out)
        self.assertIn("دانلود کنید", out)

    def test_entity_removal_with_emoji(self):
        text = "😀 کلیک کنید"
        out = caption_cleaner.clean(text, [ent("TEXT_LINK", 3, 8, "https://t.me/a_bot")])
        self.assertTrue(out.startswith("😀"))
        self.assertNotIn("کلیک", out)

    def test_empty_and_long(self):
        self.assertEqual(caption_cleaner.clean(""), "")
        self.assertEqual(caption_cleaner.clean(None), "")
        self.assertLessEqual(len(caption_cleaner.clean("الف " * 600)), 1024)


class FlowHelperTests(unittest.TestCase):
    def btn(self, text, cb="x"):
        return NS(text=text, callback_data=cb)

    def test_choose_button(self):
        self.assertEqual(choose_button([self.btn("لغو"), self.btn("📥 دریافت فایل")]).text, "📥 دریافت فایل")
        self.assertEqual(choose_button([self.btn("Verify")]).text, "Verify")
        self.assertIsNone(choose_button([self.btn("لغو"), self.btn("پشتیبانی")]))
        self.assertIsNone(choose_button([self.btn("A"), self.btn("B")]))      # مطمئن نیست → کلیک نمی‌کند
        self.assertEqual(choose_button(["📥 Download"]), "📥 Download")        # کیبورد معمولی

    def test_pick_bot_message(self):
        old = NS(id=5, outgoing=False, reply_markup=None, video=NS(), document=None, audio=None,
                 animation=None, voice=None, photo=None)
        mine = NS(id=11, outgoing=True, reply_markup=None)
        text = NS(id=12, outgoing=False, reply_markup=None, video=None, document=None, audio=None,
                  animation=None, voice=None, photo=None)
        vid = NS(id=13, outgoing=False, reply_markup=None, video=NS(), document=None, audio=None,
                 animation=None, voice=None, photo=None)
        msg, newest = pick_bot_message([vid, text, mine, old], baseline_id=11)
        self.assertIs(msg, vid)
        self.assertEqual(newest, 13)
        msg, _ = pick_bot_message([old], baseline_id=11)       # پیام قدیمی نادیده گرفته می‌شود
        self.assertIsNone(msg)
        withbtn = NS(id=14, outgoing=False, reply_markup=NS(), video=None, document=None, audio=None,
                     animation=None, voice=None, photo=None)
        msg, _ = pick_bot_message([withbtn, text], baseline_id=11)
        self.assertIs(msg, withbtn)

    def test_join_target_and_start_text(self):
        self.assertEqual(join_target({"type": "private_channel", "invite_hash": "H"}), "https://t.me/+H")
        self.assertEqual(join_target({"type": "channel", "identifier": "chan"}), "chan")
        self.assertIsNone(join_target({"type": "bot", "identifier": "b_bot"}))
        self.assertEqual(build_start_text("p"), "/start p")
        self.assertEqual(build_start_text(None), "/start")

    def test_media_and_reply_keyboard(self):
        self.assertEqual(get_media(NS(video=None, document=NS(a=1), audio=None, animation=None,
                                       voice=None, photo=None))[0], "document")
        self.assertIsNone(get_media(NS(video=None, document=None, audio=None, animation=None,
                                        voice=None, photo=None)))
        self.assertEqual(reply_keyboard_texts(NS(keyboard=[["a", NS(text="b")]])), ["a", "b"])


class DestinationInputTests(unittest.TestCase):
    def test_numeric_ids(self):
        self.assertEqual(parse_destination_input("3966174532"), -1003966174532)
        self.assertEqual(parse_destination_input("-1003966174532"), -1003966174532)
        self.assertEqual(parse_destination_input(" -100123456 "), -100123456)

    def test_username_and_links(self):
        self.assertEqual(parse_destination_input("@my_channel"), "my_channel")
        self.assertEqual(parse_destination_input("https://t.me/my_channel"), "my_channel")
        self.assertEqual(parse_destination_input("https://t.me/+A9pylkLo_LgwNDM0"), "https://t.me/+A9pylkLo_LgwNDM0")

    def test_invalid(self):
        self.assertIsNone(parse_destination_input(""))
        self.assertIsNone(parse_destination_input(None))
        self.assertIsNone(parse_destination_input("سلام"))
        self.assertIsNone(parse_destination_input("https://example.com/x"))


if __name__ == "__main__":
    unittest.main()
