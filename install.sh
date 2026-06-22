#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  AbrPardaz — Automated Installer
#  Usage: curl -fsSL https://raw.githubusercontent.com/SadraHimself/AbrPardaz/main/install.sh | sudo bash
# ─────────────────────────────────────────────────────────────

REPO_URL="https://github.com/SadraHimself/AbrPardaz.git"
INSTALL_DIR="/opt/abrpardaz"
SERVICE_USER="abrpardaz"
DB_NAME="abrpardaz"
DB_USER="abrpardaz"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "\n${CYAN}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()     { echo -e "\n${RED}[ERR]${NC}  $*"; exit 1; }
ask()     { read -r -p "$1" "$2" </dev/tty; }

# ── Root check ────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    die "با sudo اجرا کنید: sudo bash install.sh"
fi

clear
echo ""
echo -e "${CYAN}╔══════════════════════════════════════╗${NC}"
echo -e "${CYAN}║       ربات ابر پرداز — نصب‌کننده     ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════╝${NC}"
echo ""

# ── Get config from user ──────────────────────────────────────
echo -e "${YELLOW}اطلاعات زیر را وارد کنید:${NC}"
echo ""

BOT_TOKEN=""
while [[ -z "$BOT_TOKEN" ]]; do
    ask "  🤖 توکن ربات (از @BotFather): " BOT_TOKEN
done

ADMIN_ID=""
while [[ -z "$ADMIN_ID" ]]; do
    ask "  👤 Admin Telegram ID (عدد): " ADMIN_ID
done

echo ""
success "اطلاعات دریافت شد. نصب شروع می‌شود..."
sleep 1

# ── System packages ───────────────────────────────────────────
info "نصب پیش‌نیازهای سیستم..."
if command -v apt-get &>/dev/null; then
    apt-get update -y || warn "apt-get update با مشکل مواجه شد، ادامه می‌دهیم..."
    apt-get install -y git python3 python3-pip python3-venv curl \
        postgresql postgresql-contrib redis-server build-essential libpq-dev \
        || die "نصب پیش‌نیازها ناموفق بود."
elif command -v yum &>/dev/null; then
    yum install -y git python3 python3-pip curl postgresql postgresql-server redis \
        || die "نصب پیش‌نیازها ناموفق بود."
else
    die "سیستم‌عامل پشتیبانی نمی‌شود. Ubuntu/Debian یا CentOS/RHEL لازم است."
fi
success "پیش‌نیازها نصب شدند."

# ── PostgreSQL ────────────────────────────────────────────────
info "راه‌اندازی PostgreSQL..."
systemctl start postgresql  || warn "PostgreSQL start ناموفق"
systemctl enable postgresql || warn "PostgreSQL enable ناموفق"

DB_PASS="$(openssl rand -hex 16)"
cd /tmp
sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';" 2>/dev/null || \
    sudo -u postgres psql -c "ALTER USER $DB_USER WITH PASSWORD '$DB_PASS';" 2>/dev/null || true
sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;" 2>/dev/null || true
success "دیتابیس '$DB_NAME' آماده است."

# ── Redis ─────────────────────────────────────────────────────
info "راه‌اندازی Redis..."
systemctl start redis-server  || systemctl start redis || warn "Redis start ناموفق"
systemctl enable redis-server || systemctl enable redis || warn "Redis enable ناموفق"
success "Redis آماده است."

# ── Clone repo ────────────────────────────────────────────────
info "دریافت کد از GitHub..."
if [[ -d "$INSTALL_DIR/.git" ]]; then
    git -C "$INSTALL_DIR" fetch --quiet && \
    git -C "$INSTALL_DIR" reset --hard origin/main --quiet && \
    success "کد بروز شد." || warn "بروزرسانی ناموفق، از نسخه موجود استفاده می‌شود."
else
    git clone --depth=1 "$REPO_URL" "$INSTALL_DIR" || die "git clone ناموفق. اینترنت سرور را بررسی کنید."
    success "کد دریافت شد."
fi

# ── System user ───────────────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"

# ── Python venv ───────────────────────────────────────────────
info "ساخت محیط Python..."
VENV="$INSTALL_DIR/.venv"
PYTHON_BIN=$(command -v python3.12 2>/dev/null || \
             command -v python3.11 2>/dev/null || \
             command -v python3.10 2>/dev/null || \
             command -v python3    2>/dev/null || \
             die "Python 3 یافت نشد.")

$PYTHON_BIN -m venv "$VENV" || die "ساخت venv ناموفق بود."
PIP="$VENV/bin/pip"
PYTHON_EXEC="$VENV/bin/python"

"$PIP" install --upgrade pip -q || warn "upgrade pip ناموفق"
info "نصب کتابخانه‌های Python (چند دقیقه طول می‌کشد)..."
"$PIP" install -r "$INSTALL_DIR/requirements.txt" || die "نصب requirements ناموفق بود."
success "کتابخانه‌ها نصب شدند."

# ── .env ──────────────────────────────────────────────────────
info "ساخت فایل .env..."
ENV_FILE="$INSTALL_DIR/.env"
cat > "$ENV_FILE" << ENV
BOT_TOKEN=${BOT_TOKEN}
ADMIN_IDS=[${ADMIN_ID}]
WEBAPP_URL=

DATABASE_URL=postgresql+asyncpg://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}

REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/1
CELERY_RESULT_BACKEND=redis://localhost:6379/2

MIN_BALANCE_THRESHOLD=0
TRAFFIC_GRACE_SECONDS=300

SHAHKAR_BASE_URL=
SHAHKAR_SERVICE_ID=
SHAHKAR_PASSWORD=

VIRTUALIZOR_PANEL_URL=
VIRTUALIZOR_API_KEY=
VIRTUALIZOR_API_PASS=
ENV
chown "$SERVICE_USER":"$SERVICE_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"
success ".env ساخته شد."

# ── DB Migration ──────────────────────────────────────────────
info "اجرای مایگریشن دیتابیس..."
cd "$INSTALL_DIR"
if sudo -u "$SERVICE_USER" "$VENV/bin/alembic" upgrade head; then
    success "مایگریشن‌ها اعمال شدند."
else
    warn "مایگریشن ناموفق بود."
    warn "بعداً دستی اجرا کنید: cd $INSTALL_DIR && sudo -u $SERVICE_USER .venv/bin/alembic upgrade head"
fi

# ── Systemd services ──────────────────────────────────────────
info "نصب سرویس‌های systemd..."

cat > /etc/systemd/system/abrpardaz-bot.service << EOF
[Unit]
Description=AbrPardaz Telegram Bot
After=network.target postgresql.service redis.service

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$PYTHON_EXEC -m bot.main
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/abrpardaz-worker.service << EOF
[Unit]
Description=AbrPardaz Celery Worker
After=network.target redis.service

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV/bin/celery -A bot.tasks.celery_app worker -l info --concurrency=4
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/abrpardaz-beat.service << EOF
[Unit]
Description=AbrPardaz Celery Beat Scheduler
After=network.target redis.service

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV/bin/celery -A bot.tasks.celery_app beat -l info --schedule /tmp/abrpardaz-beat.db
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable abrpardaz-bot abrpardaz-worker abrpardaz-beat
systemctl start  abrpardaz-bot abrpardaz-worker abrpardaz-beat
success "سرویس‌ها راه‌اندازی شدند."

# ── Check bot ─────────────────────────────────────────────────
info "بررسی اتصال ربات به تلگرام..."
sleep 6
LOG=$(journalctl -u abrpardaz-bot -n 30 --no-pager 2>/dev/null)

if echo "$LOG" | grep -q "Bot started\|Started polling"; then
    echo -e "\n  ${GREEN}✅ ربات با موفقیت متصل شد!${NC}"
elif echo "$LOG" | grep -q "Unauthorized\|401"; then
    echo -e "\n  ${RED}✗ توکن ربات نامعتبر است.${NC}"
    echo "  ویرایش: nano $ENV_FILE"
    echo "  ری‌استارت: systemctl restart abrpardaz-bot"
elif echo "$LOG" | grep -q "Conflict\|409"; then
    echo -e "\n  ${YELLOW}⚠ ربات در جای دیگری در حال اجراست (conflict).${NC}"
else
    echo -e "\n  ${YELLOW}⚠ وضعیت نامشخص — لاگ:${NC}"
    echo "$LOG" | tail -8 | sed 's/^/    /'
fi

# ── Summary ───────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   ✅  نصب AbrPardaz با موفقیت انجام شد        ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo "  مسیر نصب     : $INSTALL_DIR"
echo "  فایل config  : $ENV_FILE"
echo ""
echo "  دستورات مفید:"
echo "    لاگ ربات      → journalctl -u abrpardaz-bot -f"
echo "    وضعیت ربات    → systemctl status abrpardaz-bot"
echo "    ری‌استارت     → systemctl restart abrpardaz-bot abrpardaz-worker abrpardaz-beat"
echo "    ویرایش config → nano $ENV_FILE"
echo ""
