#!/usr/bin/env bash
# نصب جداگانه‌ی Media Collector روی سرور — به تبچیز دست نمی‌زند.
# فقط این‌ها را می‌سازد: پوشه /opt/media-collector ، دیتابیس collector_db ، کاربر collector_user ، سرویس media-collector
set -euo pipefail

APP_DIR=/opt/media-collector
DB_NAME=collector_db
DB_USER=collector_user

[ "$(id -u)" = "0" ] || { echo "با کاربر root اجرا کنید."; exit 1; }
cd "$APP_DIR"
[ -f app/main.py ] || { echo "فایل‌های پروژه در $APP_DIR پیدا نشد."; exit 1; }

echo "== نصب پیش‌نیازها =="
apt-get update -y
apt-get install -y python3-venv python3-pip python3-dev build-essential unzip

echo "== ساخت محیط پایتون جدا =="
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

if [ -f .env ]; then
  echo "== فایل .env از قبل هست؛ دست نمی‌زنم =="
else
  echo "== دیتابیس جدا =="
  if ! mysql -e "select 1" >/dev/null 2>&1; then
    read -r -s -p "رمز root مای‌اسکیوال: " MYSQL_PWD; echo
    export MYSQL_PWD
  fi
  DB_PASS="$(./venv/bin/python -c 'import secrets;print(secrets.token_hex(16))')"
  mysql -e "CREATE DATABASE IF NOT EXISTS ${DB_NAME} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
            CREATE USER IF NOT EXISTS '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASS}';
            ALTER USER '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASS}';
            GRANT ALL PRIVILEGES ON ${DB_NAME}.* TO '${DB_USER}'@'localhost';
            FLUSH PRIVILEGES;"
  unset MYSQL_PWD || true

  echo "== اطلاعات تلگرام (از my.telegram.org و @BotFather) =="
  read -r -p "API_ID: " API_ID
  read -r -p "API_HASH: " API_HASH
  read -r -p "توکن ربات مدیریت (یک ربات جدید، نه ربات تبچیز): " BOT_TOKEN
  read -r -p "آیدی عددی شما (ADMIN_IDS): " ADMIN_IDS
  KEY="$(./venv/bin/python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"

  cat > .env <<ENV
API_ID=${API_ID}
API_HASH=${API_HASH}
ADMIN_BOT_TOKEN=${BOT_TOKEN}
ADMIN_IDS=${ADMIN_IDS}
DATABASE_URL=mysql+asyncmy://${DB_USER}:${DB_PASS}@localhost:3306/${DB_NAME}
SESSION_ENCRYPTION_KEY=${KEY}
INITIAL_SCAN_COUNT=100
CATCH_UP_LIMIT=200
RESYNC_INTERVAL_MINUTES=15
LOG_LEVEL=INFO
ENV
  chmod 600 .env
fi

echo "== ساخت جدول‌ها =="
./venv/bin/alembic upgrade head

echo "== ساخت سرویس =="
cat > /etc/systemd/system/media-collector.service <<UNIT
[Unit]
Description=Telegram Media Collector
After=network-online.target mysql.service
Wants=network-online.target

[Service]
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python -m app.main
Restart=on-failure
RestartSec=10
Nice=10

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable media-collector

echo
echo "نصب تمام شد. حالا اکانت اضافه کنید:   cd ${APP_DIR} && ./venv/bin/python -m scripts.add_account"
echo "بعد سرویس را روشن کنید:              systemctl start media-collector"
echo "لاگ زنده:                            journalctl -u media-collector -f"
