import re
from typing import Optional, Dict, Any
from urllib.parse import urlparse, parse_qs

RESERVED_PATHS = {
    "share", "joinchat", "addstickers", "addemoji", "addtheme", "proxy", "socks",
    "setlanguage", "login", "confirmphone", "contact", "bg", "invoice", "iv",
    "c", "s", "m", "boost", "giftcode", "addlist", "web", "k", "a",
}
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
HOSTS = {"t.me", "telegram.me", "telegram.dog", "www.t.me"}


def _norm(url: str) -> Optional[Any]:
    if not url:
        return None
    url = url.strip().strip("<>()[]{}.,;،")
    if url.lower().startswith("tg:"):
        return urlparse(url)
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in HOSTS:
        return None
    return parsed


class TelegramLinkParser:
    def parse_telegram_link(self, url: str) -> Optional[Dict[str, Any]]:
        parsed = _norm(url)
        if parsed is None:
            return None
        query = parse_qs(parsed.query)

        # tg://resolve?domain=x&start=y   /   tg://join?invite=hash
        if parsed.scheme.lower() == "tg":
            if parsed.netloc == "resolve" or parsed.path.strip("/") == "resolve":
                name = (query.get("domain") or [""])[0]
                if USERNAME_RE.match(name):
                    start = (query.get("start") or [None])[0]
                    return self._username_result(name, start)
            if parsed.netloc == "join" or parsed.path.strip("/") == "join":
                invite = (query.get("invite") or [""])[0]
                if invite:
                    return {"type": "private_channel", "identifier": invite, "invite_hash": invite}
            return None

        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            return None
        first = parts[0]

        if first.startswith("+") and len(first) > 1:
            return {"type": "private_channel", "identifier": first[1:], "invite_hash": first[1:]}
        if first.lower() == "joinchat" and len(parts) > 1:
            return {"type": "private_channel", "identifier": parts[1], "invite_hash": parts[1]}
        if first.lower() in RESERVED_PATHS:
            return None
        if not USERNAME_RE.match(first):
            return None

        start = (query.get("start") or [None])[0]
        if len(parts) >= 2 and parts[1].isdigit():
            return {"type": "post", "identifier": first, "username": first, "message_id": int(parts[1])}
        return self._username_result(first, start)

    @staticmethod
    def _username_result(name: str, start: Optional[str]) -> Dict[str, Any]:
        if name.lower().endswith("bot"):
            return {"type": "bot", "identifier": name, "username": name, "start_param": start}
        return {"type": "channel", "identifier": name, "username": name}

    def is_bot_link(self, url: str) -> bool:
        parsed = self.parse_telegram_link(url)
        return bool(parsed and parsed["type"] == "bot")

    def extract_bot_username(self, url: str) -> Optional[str]:
        parsed = self.parse_telegram_link(url)
        if parsed and parsed["type"] == "bot":
            return parsed["username"]
        return None

    def extract_start_param(self, url: str) -> Optional[str]:
        parsed = self.parse_telegram_link(url)
        if parsed and parsed["type"] == "bot":
            return parsed.get("start_param")
        return None

    def extract_bot(self, url: str) -> Optional[Dict[str, Optional[str]]]:
        parsed = self.parse_telegram_link(url)
        if parsed and parsed["type"] == "bot":
            return {"username": parsed["username"], "start_param": parsed.get("start_param")}
        return None


link_parser = TelegramLinkParser()
