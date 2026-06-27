#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  AbrPardaz — Installer / Updater
#  Usage: curl -fsSL https://raw.githubusercontent.com/SadraHimself/AbrPardaz/main/install.sh | sudo bash
# ─────────────────────────────────────────────────────────────

REPO_URL="https://github.com/SadraHimself/AbrPardaz.git"
INSTALL_DIR="/opt/abrpardaz"
SERVICE_USER="abrpardaz"
DB_NAME="abrpardaz"
DB_USER="abrpardaz"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "\n${CYAN}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[ OK ]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()     { echo -e "\n${RED}[ ERR]${NC} $*"; exit 1; }
ask()     { read -r -p "$1" "$2" </dev/tty; }

# ── Root check ────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    die "Please run as root: sudo bash install.sh"
fi

clear
echo ""
echo -e "${CYAN}  ╔══════════════════════════════════════╗${NC}"
echo -e "${CYAN}  ║     AbrPardaz Bot  —  Installer      ║${NC}"
echo -e "${CYAN}  ╚══════════════════════════════════════╝${NC}"
echo ""

# ── Detect mode: fresh install or update ─────────────────────
if [[ -d "$INSTALL_DIR/.git" ]] && [[ -f "$INSTALL_DIR/.env" ]]; then
    MODE="update"
else
    MODE="install"
fi

# ═══════════════════════════════════════════════════════════════
# UPDATE MODE — pull new code, migrate, restart
# ═══════════════════════════════════════════════════════════════
if [[ "$MODE" == "update" ]]; then
    echo -e "  ${YELLOW}Existing installation detected — running update...${NC}"
    echo ""

    VENV="$INSTALL_DIR/.venv"
    ENV_FILE="$INSTALL_DIR/.env"

    # Stop services
    info "Stopping services..."
    systemctl stop abrpardaz-bot abrpardaz-worker abrpardaz-beat 2>/dev/null || true
    success "Services stopped."

    # Pull latest code
    info "Pulling latest code from GitHub..."
    git config --global --add safe.directory "$INSTALL_DIR" 2>/dev/null || true
    if git -C "$INSTALL_DIR" fetch --quiet && \
       git -C "$INSTALL_DIR" reset --hard origin/main --quiet; then
        success "Code updated."
    else
        warn "git pull failed. Starting with existing code..."
    fi
    chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"

    # Install any new Python packages
    info "Updating Python packages..."
    "$VENV/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt" \
        || warn "pip install had errors, continuing..."
    success "Packages up to date."

    # Run migrations (safe — only applies new ones, skips existing)
    info "Running database migrations..."
    cd "$INSTALL_DIR"
    if sudo -u "$SERVICE_USER" "$VENV/bin/alembic" upgrade head; then
        success "Migrations applied."
    else
        warn "Migration failed. Run manually:"
        warn "  cd $INSTALL_DIR && sudo -u $SERVICE_USER .venv/bin/alembic upgrade head"
    fi

    # Restart services
    info "Restarting services..."
    RESTART_TIME=$(date "+%Y-%m-%d %H:%M:%S")
    systemctl start abrpardaz-bot abrpardaz-worker abrpardaz-beat
    success "Services restarted."

    # Check bot — only look at logs produced AFTER this restart
    info "Checking bot connection..."
    sleep 10
    LOG=$(journalctl -u abrpardaz-bot --since "$RESTART_TIME" --no-pager 2>/dev/null)
    if echo "$LOG" | grep -q "Bot started\|Started polling"; then
        echo -e "\n  ${GREEN}✅ Bot updated and running!${NC}"
    elif echo "$LOG" | grep -q "Unauthorized\|401"; then
        echo -e "\n  ${RED}✗ Invalid bot token.${NC}"
        echo "  Edit: nano $ENV_FILE"
        echo "  Restart: systemctl restart abrpardaz-bot"
    else
        echo -e "\n  ${YELLOW}⚠ Last logs:${NC}"
        echo "$LOG" | tail -6 | sed 's/^/    /'
    fi

    echo ""
    echo -e "${GREEN}  ╔══════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}  ║   ✅  AbrPardaz updated successfully!    ║${NC}"
    echo -e "${GREEN}  ╚══════════════════════════════════════════╝${NC}"
    echo ""
    echo "  Useful commands:"
    echo "    Live log    -> journalctl -u abrpardaz-bot -f"
    echo "    Bot status  -> systemctl status abrpardaz-bot"
    echo "    Edit config -> nano $ENV_FILE"
    echo ""
    exit 0
fi

# ═══════════════════════════════════════════════════════════════
# INSTALL MODE — fresh installation
# ═══════════════════════════════════════════════════════════════
echo -e "  ${YELLOW}Please enter the following information:${NC}"
echo ""

BOT_TOKEN=""
while [[ -z "$BOT_TOKEN" ]]; do
    ask "  Bot Token (from @BotFather): " BOT_TOKEN
done

ADMIN_ID=""
while [[ -z "$ADMIN_ID" ]]; do
    ask "  Admin Telegram ID (number): " ADMIN_ID
done

echo ""
success "Config received. Starting installation..."
sleep 1

# ── System packages ───────────────────────────────────────────
info "Installing system packages..."
if command -v apt-get &>/dev/null; then
    apt-get update -y || warn "apt-get update failed, continuing..."
    apt-get install -y git python3 python3-pip python3-venv curl \
        postgresql postgresql-contrib redis-server build-essential libpq-dev \
        || die "Failed to install system packages."

    # pydantic-core requires Python <= 3.13. Install 3.12 if 3.11/3.12 not present.
    if ! command -v python3.12 &>/dev/null && ! command -v python3.11 &>/dev/null; then
        info "Python 3.12/3.11 not found — installing from deadsnakes PPA..."
        apt-get install -y software-properties-common 2>/dev/null || true
        add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
        apt-get update -y 2>/dev/null || true
        apt-get install -y python3.12 python3.12-venv python3.12-dev \
            || warn "Could not install Python 3.12. Install may fail on Python 3.14+."
    fi
elif command -v yum &>/dev/null; then
    yum install -y git python3 python3-pip curl postgresql postgresql-server redis \
        || die "Failed to install system packages."
else
    die "Unsupported OS. Please use Ubuntu/Debian or CentOS/RHEL."
fi
success "System packages installed."

# ── PostgreSQL ────────────────────────────────────────────────
info "Setting up PostgreSQL..."
systemctl start postgresql  || warn "PostgreSQL start failed."
systemctl enable postgresql || warn "PostgreSQL enable failed."

DB_PASS="$(openssl rand -hex 16)"
cd /tmp
sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';" 2>/dev/null || \
    sudo -u postgres psql -c "ALTER USER $DB_USER WITH PASSWORD '$DB_PASS';" 2>/dev/null || true
sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;" 2>/dev/null || true
success "Database '$DB_NAME' is ready."

# ── Redis ─────────────────────────────────────────────────────
info "Setting up Redis..."
systemctl start redis-server  || systemctl start redis  || warn "Redis start failed."
systemctl enable redis-server || systemctl enable redis || warn "Redis enable failed."
success "Redis is ready."

# ── Clone repo ────────────────────────────────────────────────
info "Cloning repository..."
git clone --depth=1 "$REPO_URL" "$INSTALL_DIR" \
    || die "git clone failed. Check server internet access."
success "Repository cloned."

# ── System user ───────────────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"
success "System user '$SERVICE_USER' ready."

# ── Python venv ───────────────────────────────────────────────
info "Creating Python virtual environment..."
VENV="$INSTALL_DIR/.venv"
PYTHON_BIN=$(command -v python3.12 2>/dev/null || \
             command -v python3.11 2>/dev/null || \
             command -v python3.10 2>/dev/null || \
             command -v python3    2>/dev/null) \
             || die "Python 3 not found."

$PYTHON_BIN -m venv "$VENV" || die "Failed to create venv."
PIP="$VENV/bin/pip"
PYTHON_EXEC="$VENV/bin/python"

"$PIP" install --upgrade pip -q || warn "pip upgrade failed."

info "Installing Python packages (this may take a few minutes)..."
"$PIP" install -r "$INSTALL_DIR/requirements.txt" || die "Failed to install requirements."
success "Python packages installed."

# ── .env ──────────────────────────────────────────────────────
info "Creating .env file..."
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

NP_API_KEY=
NP_IPN_SECRET=
NP_OUTCOME_CURRENCY=trx
NP_PRICE_CURRENCY=usd
NP_WEBHOOK_PORT=8081
ENV
chown "$SERVICE_USER":"$SERVICE_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"
success ".env created."

# ── DB migrations ─────────────────────────────────────────────
info "Running database migrations..."
cd "$INSTALL_DIR"
if sudo -u "$SERVICE_USER" "$VENV/bin/alembic" upgrade head; then
    success "Migrations applied."
else
    warn "Migration failed. Run manually later:"
    warn "  cd $INSTALL_DIR && sudo -u $SERVICE_USER .venv/bin/alembic upgrade head"
fi

# ── Systemd services ──────────────────────────────────────────
info "Installing systemd services..."

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
START_TIME=$(date "+%Y-%m-%d %H:%M:%S")
systemctl start  abrpardaz-bot abrpardaz-worker abrpardaz-beat
success "Services installed and started."

# ── Check bot connection ──────────────────────────────────────
info "Checking bot connection to Telegram..."
sleep 10
LOG=$(journalctl -u abrpardaz-bot --since "$START_TIME" --no-pager 2>/dev/null)

if echo "$LOG" | grep -q "Bot started\|Started polling"; then
    echo -e "\n  ${GREEN}✅ Bot connected successfully!${NC}"
elif echo "$LOG" | grep -q "Unauthorized\|401"; then
    echo -e "\n  ${RED}✗ Invalid bot token.${NC}"
    echo "  Edit: nano $ENV_FILE"
    echo "  Restart: systemctl restart abrpardaz-bot"
elif echo "$LOG" | grep -q "Conflict\|409"; then
    echo -e "\n  ${YELLOW}⚠ Bot is already running elsewhere (conflict).${NC}"
    echo "  Stop the other instance first."
else
    echo -e "\n  ${YELLOW}⚠ Status unknown — last logs:${NC}"
    echo "$LOG" | tail -8 | sed 's/^/    /'
fi

# ── Done ──────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}  ╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}  ║   ✅  AbrPardaz installed successfully!   ║${NC}"
echo -e "${GREEN}  ╚══════════════════════════════════════════╝${NC}"
echo ""
echo "  Install path : $INSTALL_DIR"
echo "  Config file  : $ENV_FILE"
echo ""
echo "  Useful commands:"
echo "    Live bot log  -> journalctl -u abrpardaz-bot -f"
echo "    Bot status    -> systemctl status abrpardaz-bot"
echo "    Restart all   -> systemctl restart abrpardaz-bot abrpardaz-worker abrpardaz-beat"
echo "    Edit config   -> nano $ENV_FILE"
echo "    Update bot    -> curl -fsSL https://raw.githubusercontent.com/SadraHimself/AbrPardaz/main/install.sh | sudo bash"
echo ""
