"""کلاینت ساده‌ی Bot API (فقط کتابخانه‌ی استاندارد؛ بدون وابستگی جدید).
چرا Bot API و نه Kurigram؟ ربات‌ها با Bot API هر کانالی را که عضوش هستند با آیدی عددی می‌شناسند؛
در MTProto ربات برای کانال‌های ندیده‌شده به access_hash نیاز دارد و خطای «Peer id invalid» می‌دهد.
توکن هرگز در لاگ یا پیام خطا نمی‌آید."""
import asyncio
import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Awaitable, Callable, Optional

_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="botapi")


class BotApiError(Exception):
    """تلگرام صریحاً خطا برگرداند (درخواست انجام نشده)."""
    def __init__(self, code: int, description: str, retry_after: Optional[int] = None):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description or ""
        self.retry_after = retry_after


class BotApiNetworkError(Exception):
    """قطع اتصال/تایم‌اوت: نتیجه‌ی درخواست «مبهم» است (شاید انجام شده باشد)."""


def _post_sync(url: str, body: bytes, timeout: float):
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


async def _default_transport(url: str, params: dict, timeout: float):
    body = json.dumps(params, ensure_ascii=False).encode("utf-8")
    loop = asyncio.get_running_loop()
    status, raw = await loop.run_in_executor(_EXECUTOR, _post_sync, url, body, timeout)
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        data = {"ok": False, "error_code": status, "description": "bad response"}
    return status, data


Transport = Callable[[str, dict, float], Awaitable[tuple[int, dict]]]


class BotApi:
    def __init__(self, token: str, transport: Optional[Transport] = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, max_flood_wait: int = 30):
        self._base = f"https://api.telegram.org/bot{token}/"
        self._transport = transport or _default_transport
        self._sleep = sleep
        self._max_flood_wait = max_flood_wait

    async def call(self, method: str, params: Optional[dict] = None, *, http_timeout: float = 30,
                   idempotent: bool = False) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        attempts = 0
        while True:
            attempts += 1
            try:
                status, data = await self._transport(self._base + method, params, http_timeout)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if idempotent and attempts < 3:
                    await self._sleep(attempts)
                    continue
                raise BotApiNetworkError(type(e).__name__) from None   # بدون زنجیره (ممکن است URL/توکن داشته باشد)
            if isinstance(data, dict) and data.get("ok"):
                return data.get("result")
            data = data if isinstance(data, dict) else {}
            code = int(data.get("error_code") or status or 0)
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if code == 429 and retry_after is not None and attempts < 4 and int(retry_after) <= self._max_flood_wait:
                await self._sleep(int(retry_after) + 1)
                continue
            raise BotApiError(code, str(data.get("description") or ""), retry_after)

    # ----- میان‌برها -----
    async def get_me(self) -> dict:
        return await self.call("getMe", idempotent=True)

    async def get_chat(self, chat_id) -> dict:
        return await self.call("getChat", {"chat_id": chat_id}, idempotent=True)

    async def get_chat_member(self, chat_id, user_id: int) -> dict:
        return await self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id}, idempotent=True)

    async def copy_message(self, chat_id, from_chat_id, message_id: int, caption: Optional[str] = None,
                           reply_markup: Optional[dict] = None) -> dict:
        return await self.call("copyMessage", {"chat_id": chat_id, "from_chat_id": from_chat_id,
                                               "message_id": message_id, "caption": caption,
                                               "reply_markup": reply_markup})

    async def send_message(self, chat_id, text: str, reply_markup: Optional[dict] = None,
                           reply_to_message_id: Optional[int] = None) -> dict:
        return await self.call("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": reply_markup,
                                               "reply_to_message_id": reply_to_message_id,
                                               "disable_web_page_preview": True})

    async def delete_message(self, chat_id, message_id: int) -> bool:
        return bool(await self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}))

    async def answer_callback_query(self, callback_id: str, text: Optional[str] = None, alert: bool = False):
        return await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text,
                                                       "show_alert": alert or None})

    async def get_updates(self, offset: Optional[int], timeout: int = 25) -> list:
        return await self.call("getUpdates", {"offset": offset, "timeout": timeout,
                                              "allowed_updates": ["message", "callback_query"]},
                               http_timeout=timeout + 15, idempotent=True)

    async def delete_webhook(self) -> bool:
        return bool(await self.call("deleteWebhook", {"drop_pending_updates": False}, idempotent=True))
