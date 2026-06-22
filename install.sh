#!/usr/bin/env bash
# ================================================================
#  Abr Pardaz Bot — Auto Installer
#  Usage: sudo bash install.sh
# ================================================================
set -euo pipefail

R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'
C='\033[0;36m'; W='\033[1m'; N='\033[0m'

ok()   { echo -e "  ${G}✓${N} $1"; }
info() { echo -e "\n${C}▶${N} $1"; }
warn() { echo -e "  ${Y}⚠${N} $1"; }
die()  { echo -e "\n${R}✗ Error:${N} $1" >&2; exit 1; }

PROJ="/opt/abr-pardaz"
VENV="$PROJ/.venv"
DB_NAME="telecloud"
DB_USER="telecloud"
DB_PASS=""
BOT_TOKEN=""
ADMIN_ID=""
PYTHON=""

# ── 1. Config ───────────────────────────────────────────────────
get_config() {
    clear
    echo -e "${C}╔══════════════════════════════════════╗${N}"
    echo -e "${C}║   ☁️  Abr Pardaz Bot — Installer    ║${N}"
    echo -e "${C}╚══════════════════════════════════════╝${N}\n"

    read -rp "  Bot Token   [Enter = keep default]: " _t
    BOT_TOKEN="${_t:-8716134766:AAEzHo4MQohBrgWQMWKpmq985-Wv06d4AJU}"

    read -rp "  Admin ID    [Enter = keep default]: " _a
    ADMIN_ID="${_a:-6972925122}"

    DB_PASS="$(openssl rand -hex 12)"

    echo -e "\n  ${G}Config confirmed. Starting install...${N}\n"
    sleep 1
}

# ── 2. System packages ──────────────────────────────────────────
install_system() {
    info "Installing system packages"
    [ "$EUID" -ne 0 ] && die "Must run as root: sudo bash install.sh"

    [ -f /etc/os-release ] && { . /etc/os-release; ok "OS: $PRETTY_NAME"; }

    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -yq \
        python3 python3-pip python3-venv python3-dev \
        postgresql postgresql-contrib \
        redis-server \
        build-essential libpq-dev openssl curl wget git 2>&1 | tail -3

    PYTHON=$(command -v python3.12 2>/dev/null \
        || command -v python3.11 2>/dev/null \
        || command -v python3.10 2>/dev/null \
        || command -v python3 2>/dev/null \
        || die "Python 3 not found")
    ok "Python: $($PYTHON --version)"
}

# ── 3. Database ─────────────────────────────────────────────────
setup_db() {
    info "Setting up PostgreSQL"
    systemctl start postgresql && systemctl enable postgresql --quiet
    ok "PostgreSQL running"

    # Create user if not exists, ALWAYS sync password (prevents mismatch on re-runs)
    cd /tmp && sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';" 2>/dev/null || true
    cd /tmp && sudo -u postgres psql -c "ALTER USER $DB_USER WITH PASSWORD '$DB_PASS';" >/dev/null 2>&1
    ok "DB user '$DB_USER' ready (password synced)"

    cd /tmp && sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" 2>/dev/null | grep -q 1 \
        || sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;" >/dev/null 2>&1
    ok "Database '$DB_NAME' ready"

    info "Setting up Redis"
    systemctl start redis-server && systemctl enable redis-server --quiet
    redis-cli ping 2>/dev/null | grep -q PONG && ok "Redis ready" || warn "Redis did not respond"
}

# ── 4. Python venv ──────────────────────────────────────────────
setup_venv() {
    info "Creating Python virtual environment"
    $PYTHON -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    ok "venv created: $VENV"

    info "Installing Python packages (may take 3-5 minutes)"
    "$VENV/bin/pip" install --quiet -r "$PROJ/requirements.txt"
    ok "All packages installed"
}

# ── 5. .env file ────────────────────────────────────────────────
setup_env() {
    info "Creating .env"
    cat > "$PROJ/.env" << ENV
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
    chmod 600 "$PROJ/.env"
    ok ".env created (chmod 600)"
}

# ── 6. DB Migration ─────────────────────────────────────────────
run_migration() {
    info "Running database migrations"
    cd "$PROJ"
    set -a; source .env; set +a

    if [ -z "$(ls -A alembic/versions/ 2>/dev/null | grep -v .gitkeep)" ]; then
        "$VENV/bin/alembic" revision --autogenerate -m "initial" 2>&1 | tail -3
        ok "Initial migration created"
    else
        ok "Migration already exists"
    fi
    "$VENV/bin/alembic" upgrade head 2>&1 | tail -3
    ok "Database tables ready"
}

# ── 7. systemd services ─────────────────────────────────────────
setup_services() {
    info "Creating systemd services"

    cat > /etc/systemd/system/abr-pardaz-bot.service << SVC
[Unit]
Description=Abr Pardaz Telegram Bot
After=network.target postgresql.service redis.service

[Service]
Type=simple
User=root
WorkingDirectory=${PROJ}
ExecStart=${VENV}/bin/python -m bot.main
Restart=always
RestartSec=5
EnvironmentFile=${PROJ}/.env
StandardOutput=journal
StandardError=journal
SyslogIdentifier=abr-bot

[Install]
WantedBy=multi-user.target
SVC

    cat > /etc/systemd/system/abr-pardaz-celery.service << SVC
[Unit]
Description=Abr Pardaz Celery Worker
After=network.target redis.service postgresql.service

[Service]
Type=simple
User=root
WorkingDirectory=${PROJ}
ExecStart=${VENV}/bin/celery -A bot.tasks.celery_app worker --loglevel=info --concurrency=2
Restart=always
RestartSec=5
EnvironmentFile=${PROJ}/.env
StandardOutput=journal
StandardError=journal
SyslogIdentifier=abr-celery

[Install]
WantedBy=multi-user.target
SVC

    cat > /etc/systemd/system/abr-pardaz-beat.service << SVC
[Unit]
Description=Abr Pardaz Celery Beat
After=network.target redis.service

[Service]
Type=simple
User=root
WorkingDirectory=${PROJ}
ExecStart=${VENV}/bin/celery -A bot.tasks.celery_app beat --loglevel=info
Restart=always
RestartSec=5
EnvironmentFile=${PROJ}/.env
StandardOutput=journal
StandardError=journal
SyslogIdentifier=abr-beat

[Install]
WantedBy=multi-user.target
SVC

    systemctl daemon-reload
    systemctl enable abr-pardaz-bot abr-pardaz-celery abr-pardaz-beat --quiet
    systemctl start abr-pardaz-bot
    sleep 4
    systemctl start abr-pardaz-celery abr-pardaz-beat 2>/dev/null || true
    ok "Services started"
}

# ── 8. Connection check ─────────────────────────────────────────
check_bot() {
    info "Checking bot connection to Telegram"
    sleep 5

    LOG=$(journalctl -u abr-pardaz-bot -n 50 --no-pager 2>/dev/null)

    if echo "$LOG" | grep -q "Bot started"; then
        BOT_USER=$(echo "$LOG" | grep "Bot started:" | tail -1 | sed 's/.*@//')
        echo -e "\n  ${G}${W}✅ Bot connected! @${BOT_USER}${N}"
    elif echo "$LOG" | grep -q "Unauthorized"; then
        echo -e "\n  ${R}✗ Invalid bot token!${N}"
        echo -e "  Edit: ${Y}nano $PROJ/.env${N}"
        echo -e "  Then: ${Y}systemctl restart abr-pardaz-bot${N}"
    elif echo "$LOG" | grep -q "Conflict"; then
        echo -e "\n  ${Y}⚠ Bot is already running elsewhere (conflict)!${N}"
        echo -e "  Stop the other instance, then: ${Y}systemctl restart abr-pardaz-bot${N}"
    elif echo "$LOG" | grep -q "Cannot connect\|Network\|ConnectionError"; then
        echo -e "\n  ${R}✗ Server cannot reach Telegram (blocked/firewall)${N}"
        echo -e "  Configure a proxy or use a non-restricted server"
    else
        echo -e "\n  ${Y}⚠ Status unknown — last logs:${N}"
        echo "$LOG" | tail -15 | sed 's/^/    /'
    fi
}

# ── Summary ─────────────────────────────────────────────────────
summary() {
    echo ""
    echo -e "${C}══════════════════════════════════════════════${N}"
    echo -e "${W}  Service status:${N}"
    for svc in abr-pardaz-bot abr-pardaz-celery abr-pardaz-beat; do
        systemctl is-active --quiet "$svc" 2>/dev/null \
            && echo -e "  ${G}●${N} $svc — running" \
            || echo -e "  ${R}●${N} $svc — stopped"
    done
    echo ""
    echo -e "${W}  File locations:${N}"
    echo -e "  ${Y}$PROJ/.env${N}          ← API keys & config"
    echo -e "  ${Y}$PROJ/bot/${N}           ← bot source code"
    echo -e "  ${Y}$PROJ/alembic/${N}       ← database migrations"
    echo -e "  ${Y}$PROJ/.venv/${N}         ← Python virtual environment"
    echo ""
    echo -e "${W}  Useful commands:${N}"
    echo -e "  ${C}journalctl -u abr-pardaz-bot -f${N}       # live bot log"
    echo -e "  ${C}systemctl restart abr-pardaz-bot${N}       # restart bot"
    echo -e "  ${C}systemctl status abr-pardaz-bot${N}        # status"
    echo -e "  ${C}journalctl -u abr-pardaz-celery -f${N}     # billing log"
    echo -e "  ${C}nano $PROJ/.env${N}                        # edit config"
    echo ""
    echo -e "${W}  After editing .env, always restart:${N}"
    echo -e "  ${C}systemctl restart abr-pardaz-bot abr-pardaz-celery abr-pardaz-beat${N}"
    echo -e "${C}══════════════════════════════════════════════${N}\n"
}

# ── Main ────────────────────────────────────────────────────────
main() {
    get_config
    install_system
    setup_db
    setup_venv
    setup_env
    run_migration
    setup_services
    check_bot
    summary
}

main "$@"
