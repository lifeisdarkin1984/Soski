from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    api_id: int
    api_hash: str
    admin_bot_token: str
    database_url: str
    session_encryption_key: str
    # اختیاری: مقصد اصلی از داخل ربات مدیریت تنظیم می‌شود؛ این فقط مقدار پیش‌فرض قدیمی است
    destination_chat_id: int | None = None
    # شناسه‌ی عددی مدیرها، جدا شده با کاما (مثلاً 123456,789012)
    admin_ids: str = ""
    initial_scan_count: int = 100
    catch_up_limit: int = 200
    resync_interval_minutes: int = 15
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def admin_id_list(self) -> list[int]:
        result = []
        for part in self.admin_ids.replace(";", ",").split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                result.append(int(part))
        return result


settings = Settings()
