from pyrogram import Client
from app.core.config import settings

admin_bot = Client(
    name="admin_bot", api_id=settings.api_id, api_hash=settings.api_hash,
    bot_token=settings.admin_bot_token, workdir="sessions/admin"
)
