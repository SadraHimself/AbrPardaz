# TeleCloud Bot — Project Status

## Architecture Overview

```
telecloud-bot/
├── bot/
│   ├── config.py              # Pydantic settings from .env
│   ├── main.py                # Bot entry point (aiogram Dispatcher)
│   ├── database/
│   │   ├── models.py          # SQLAlchemy ORM models
│   │   ├── session.py         # Async engine + session factory
│   │   └── base.py            # Base declarative class
│   ├── handlers/
│   │   ├── __init__.py        # Router registration
│   │   ├── start.py           # /start, terms, channel lock, welcome
│   │   ├── auth.py            # Phone verification (Shahkar KYC)
│   │   ├── billing.py         # Wallet charge, transaction history
│   │   ├── servers.py         # Buy/manage servers (user-facing)
│   │   ├── admin.py           # Providers, plans, discounts, sub-products
│   │   ├── admin_users.py     # User management panel
│   │   ├── admin_stats.py     # Stats, bot settings, finance, channels
│   │   └── admin_broadcast.py # Mass messaging
│   ├── keyboards/
│   │   ├── main.py            # Main menu keyboards
│   │   ├── server.py          # Server action keyboards
│   │   └── admin.py           # All admin keyboards
│   ├── providers/
│   │   ├── base.py            # Abstract BaseProvider interface
│   │   └── virtualizor.py     # Virtualizor Admin API client
│   ├── services/
│   │   ├── billing.py         # Credit/debit wallet
│   │   ├── server.py          # Server lifecycle (create/delete/action)
│   │   └── notification.py    # User notification messages
│   ├── tasks/
│   │   ├── celery_app.py      # Celery app + beat schedule
│   │   ├── billing.py         # Hourly billing, monthly expiry, low-balance alerts
│   │   ├── server.py          # Traffic sync task
│   │   └── stats.py           # Daily stat aggregation + 30-day cleanup
│   └── middlewares/
│       └── ...                # Auth, session, throttling middlewares
└── alembic/
    └── versions/
        ├── 0001_initial.py    # Initial schema
        └── 0002_features.py   # SubProduct, BotSettings, DailyStat + new columns
```

---

## Database Models

| Model | Description |
|-------|-------------|
| `User` | Telegram users — balance, KYC status, terms_accepted_at |
| `ProviderAccount` | Virtualizor panel credentials (api_key, api_secret, strict_kyc) |
| `ServerPlan` | Purchasable plans (RAM/CPU/Disk/BW, pricing, category) |
| `SubProduct` | Add-ons per plan (extra traffic GB, extra IPs) |
| `Server` | User VPS instances — linked to plan + provider |
| `Transaction` | Wallet credit/debit history |
| `DiscountCode` | Percentage discount codes (optional per-user, expiry, max uses) |
| `BotSettings` | Key-value store for bot configuration (welcome text, support, channels, etc.) |
| `DailyStat` | Daily aggregated stats (new users, revenue, active users, wallet total) |

---

## Feature Implementation Status

### ✅ Completed

| Feature | Handler/File |
|---------|-------------|
| Phone verification (Shahkar) | `handlers/auth.py` |
| Terms & conditions flow | `handlers/start.py` |
| Force-join channel check | `handlers/start.py` |
| Dynamic welcome + sticker | `handlers/start.py` — BotSettings: `welcome_text`, `welcome_sticker_id` |
| Maintenance mode | `handlers/start.py` — BotSettings: `maintenance_mode` |
| Buy server — category → plan → billing | `handlers/servers.py` |
| **Hostname selection** during purchase | `handlers/servers.py` — BuyServerStates.entering_hostname |
| **OS selection from Virtualizor** | `handlers/servers.py` — BuyServerStates.selecting_os |
| Discount code at checkout | `handlers/servers.py` |
| Server panel — start/stop/restart/delete | `handlers/servers.py` |
| Server panel — traffic monitor | `handlers/servers.py` |
| **VNC activation** | `handlers/servers.py` + `providers/virtualizor.py` |
| **Sub-product purchase** (traffic, extra IP) | `handlers/servers.py` |
| Admin — Virtualizor providers CRUD | `handlers/admin.py` |
| Admin — Provider enable/disable toggle | `handlers/admin.py` |
| Admin — Strict KYC toggle per provider | `handlers/admin.py` |
| Admin — Plans CRUD with categories | `handlers/admin.py` |
| Admin — **Auto-fetch specs** when Plan ID given | `handlers/admin.py` — `PlanFSM.confirm_autofetch` |
| Admin — **Sub-products** CRUD per plan | `handlers/admin.py` |
| Admin — Discount codes CRUD | `handlers/admin.py` |
| Admin — User list (paginated + search) | `handlers/admin_users.py` |
| Admin — User detail (ban, credit/debit, KYC, message) | `handlers/admin_users.py` |
| Admin — User payment history | `handlers/admin_users.py` |
| Admin — Per-user discount code | `handlers/admin_users.py` |
| Admin — Stats (today/month/custom range) | `handlers/admin_stats.py` |
| Admin — Bot settings editor (8 fields) | `handlers/admin_stats.py` |
| Admin — Force-join channel add/remove | `handlers/admin_stats.py` |
| Admin — Bulk credit all active users | `handlers/admin_stats.py` |
| Admin — Group price adjustment (% change) | `handlers/admin_stats.py` |
| Admin — Broadcast with filter | `handlers/admin_broadcast.py` |
| Admin — Real forward support | `handlers/admin_broadcast.py` |
| Admin — Rate limiting 25 msg/sec | `handlers/admin_broadcast.py` |
| Hourly billing task | `tasks/billing.py` |
| Monthly expiry check task | `tasks/billing.py` |
| Traffic sync task (every 15 min) | `tasks/server.py` |
| **Daily stats aggregation** | `tasks/stats.py` |
| **30-day stats cleanup** | `tasks/stats.py` |

---

## BotSettings Keys (editable from admin panel)

| Key | Default | Description |
|-----|---------|-------------|
| `welcome_text` | built-in template | متن خوش‌آمدگویی — متغیرها: `{name}`, `{balance}`, `{status}` |
| `welcome_sticker_id` | — | file_id استیکر خوش‌آمدگویی |
| `support_text` | built-in | متن صفحه پشتیبانی |
| `support_id` | — | username یا ID تلگرام پشتیبانی |
| `website_url` | — | آدرس وبسایت |
| `terms_text` | built-in | متن قوانین و مقررات |
| `force_channels` | `[]` (JSON) | لیست channel ID/username برای عضویت اجباری |
| `maintenance_mode` | `0` | اگر `1` باشد ربات برای غیرادمین‌ها بسته است |
| `maintenance_text` | built-in | پیام نمایش‌داده‌شده در حالت تعمیر |

---

## Celery Beat Schedule

| Task | Schedule | Description |
|------|----------|-------------|
| `run_hourly_billing` | هر ساعت `:00` | بیلینگ سرورهای ساعتی |
| `run_monthly_expiry_check` | هر شب `00:05` | بررسی انقضای سرورهای ماهیانه |
| `sync_all_traffic` | هر ۱۵ دقیقه | آپدیت ترافیک مصرفی از Virtualizor |
| `send_low_balance_alerts` | هر روز `10:00` | هشدار موجودی کم |
| `aggregate_daily_stats` | هر شب `00:30` | جمع‌آوری آمار روزانه |
| `cleanup_old_stats` | هر یکشنبه `03:00` | حذف آمار قدیمی‌تر از ۳۰ روز |

---

## Virtualizor API Authentication

```python
# Correct auth format (from SDK source /usr/local/virtualizor/sdk/admin.php):
random_key = 8 random lowercase alphanum chars
adminapipass = random_key + md5(api_pass + random_key)

# Request params:
adminapikey = <api_key>
adminapipass = <as above>
act = <action name>
api = "json"
```

---

## Environment Variables

See `.env.example` for all required variables.

Critical ones:
- `BOT_TOKEN` — Telegram bot token
- `ADMIN_IDS` — JSON array of admin telegram IDs e.g. `[123456789]`
- `DATABASE_URL` — PostgreSQL async URL
- `REDIS_URL` — for FSM state storage

---

## Deployment

```bash
# 1. DB migrations
alembic upgrade head

# 2. Bot
python -m bot.main

# 3. Celery worker
celery -A bot.tasks.celery_app worker -l info

# 4. Celery beat (periodic tasks)
celery -A bot.tasks.celery_app beat -l info
```
