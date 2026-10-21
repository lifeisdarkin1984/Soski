# telegram-media-collector
Telegram Media Collector System

---

## راه‌اندازی سریع (سرور)

1. `cp .env.example .env` و همه‌ی مقدارها را پر کنید (`ADMIN_IDS` الزامی است).
2. `pip install -r requirements.txt`
3. `alembic upgrade head`
4. افزودن اکانت (پیشنهادی، از ترمینال): `python -m scripts.add_account`  — یا بعد از اجرا از داخل ربات مدیریت.
5. `python -m app.main`

تست منطق: `python -m unittest discover -s tests -v`
