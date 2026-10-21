from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.setting import CollectorSetting

KEY_DEST_ID = "destination_chat_id"
KEY_DEST_TITLE = "destination_title"


class SettingsRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, key: str) -> Optional[str]:
        row = await self.session.get(CollectorSetting, key)
        return row.setting_value if row else None

    async def set(self, key: str, value: str):
        row = await self.session.get(CollectorSetting, key)
        if row:
            row.setting_value = value
        else:
            self.session.add(CollectorSetting(setting_key=key, setting_value=value))
        await self.session.commit()

    async def set_destination(self, chat_id: int, title: str):
        await self.set(KEY_DEST_ID, str(chat_id))
        await self.set(KEY_DEST_TITLE, title or "")


async def get_destination_info() -> tuple[Optional[int], str]:
    """(آیدی مقصد، عنوان). اول از دیتابیس (تنظیم‌شده در ربات)، بعد مقدار قدیمی .env."""
    async with AsyncSessionLocal() as db:
        repo = SettingsRepository(db)
        raw = await repo.get(KEY_DEST_ID)
        title = await repo.get(KEY_DEST_TITLE) or ""
    if raw and raw.lstrip("-").isdigit():
        return int(raw), title
    if settings.destination_chat_id:
        return settings.destination_chat_id, ""
    return None, ""


async def get_destination_id() -> Optional[int]:
    return (await get_destination_info())[0]
