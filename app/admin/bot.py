import os
from pyrogram import Client
from app.core.config import settings

for _d in ("sessions/admin", "sessions/users", "sessions/temp", "logs"):
    os.makedirs(_d, exist_ok=True)

admin_bot = Client(
    name="admin_bot", api_id=settings.api_id, api_hash=settings.api_hash,
    bot_token=settings.admin_bot_token, workdir="sessions/admin"
)
