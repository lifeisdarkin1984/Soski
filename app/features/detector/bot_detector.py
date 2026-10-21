import re
from typing import Optional, Dict
from app.core.link_parser import link_parser

MENTION_RE = re.compile(r"(?<![\w/])@([A-Za-z][A-Za-z0-9_]{1,}[Bb][Oo][Tt])\b")
URL_RE = re.compile(r"(?:https?://)?(?:t|telegram)\.me/[^\s)>\]]+", re.I)


def _etype(entity) -> str:
    t = getattr(entity, "type", "")
    return str(getattr(t, "name", t)).lower()


def _utf16_slice(text: str, offset: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    return raw[offset * 2:(offset + length) * 2].decode("utf-16-le", errors="ignore")


class BotDetector:
    @staticmethod
    def extract_bot(message) -> Optional[Dict[str, Optional[str]]]:
        """برمی‌گرداند {'username':..., 'start_param':...} یا None"""
        # ۱) دکمه‌های لینک‌دار زیر پست
        markup = getattr(message, "reply_markup", None)
        rows = getattr(markup, "inline_keyboard", None) or []
        for row in rows:
            for button in row:
                url = getattr(button, "url", None)
                if url:
                    found = link_parser.extract_bot(url)
                    if found:
                        return found

        text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        entities = (getattr(message, "entities", None)
                    or getattr(message, "caption_entities", None) or [])

        # ۲) لینک‌های داخل متن یا کپشن (پیام‌های رسانه‌دار هم پوشش داده می‌شوند)
        for entity in entities:
            kind = _etype(entity)
            if kind == "text_link":
                url = getattr(entity, "url", None)
            elif kind == "url":
                url = _utf16_slice(text, entity.offset, entity.length)
            else:
                continue
            found = link_parser.extract_bot(url) if url else None
            if found:
                return found

        # ۳) لینک خام که در entities نیامده
        for match in URL_RE.finditer(text):
            found = link_parser.extract_bot(match.group(0))
            if found:
                return found

        # ۴) آیدی @xxxbot
        match = MENTION_RE.search(text)
        if match:
            return {"username": match.group(1), "start_param": None}
        return None

    @staticmethod
    def extract_bot_username(message) -> Optional[str]:
        found = BotDetector.extract_bot(message)
        return found["username"] if found else None


bot_detector = BotDetector()
