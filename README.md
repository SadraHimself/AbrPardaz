# TeleCloud Bot 🖥

ربات تلگرام برای فروش و مدیریت سرور مجازی (VPS) روی پنل Virtualizor

---

## ویژگی‌ها

- **خرید سرور** با انتخاب OS، hostname، نوع بیلینگ (ساعتی / ماهانه)
- **مدیریت سرور**: روشن/خاموش/ریبوت/حذف، تغییر IP، VNC
- **ترافیک**: نمایش مصرف + خرید ترافیک اضافه
- **کیف پول**: شارژ آنلاین، تاریخچه تراکنش‌ها
- **کد تخفیف**: درصدی، با انقضا و محدودیت استفاده
- **احراز هویت**: تأیید شماره موبایل ایرانی از طریق Shahkar
- **قوانین و مقررات**: flow تأیید قوانین + عضویت اجباری در کانال
- **پنل ادمین کامل**: مدیریت کاربران، محصولات، آمار، تنظیمات، broadcast

---

## پیش‌نیازها

- Python 3.11+
- PostgreSQL 14+
- Redis 6+
- پنل Virtualizor (با API فعال)

---

## نصب و راه‌اندازی

### ۱. کلون و نصب وابستگی‌ها

```bash
git clone <repo-url>
cd telecloud-bot
python -m venv .venv
source .venv/bin/activate   # Linux/Mac
# یا: .venv\Scripts\activate  # Windows

pip install -r requirements.txt
```

### ۲. فایل `.env`

```bash
cp .env.example .env
# ویرایش .env با مقادیر واقعی
nano .env
```

متغیرهای اجباری:
```
BOT_TOKEN=your_bot_token
ADMIN_IDS=[123456789]
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/telecloud
REDIS_URL=redis://localhost:6379/0
```

### ۳. مایگریشن دیتابیس

```bash
alembic upgrade head
```

### ۴. اجرا

**ربات:**
```bash
python -m bot.main
```

**Celery worker** (بیلینگ و sync ترافیک):
```bash
celery -A bot.tasks.celery_app worker -l info
```

**Celery beat** (وظایف زمان‌بندی‌شده):
```bash
celery -A bot.tasks.celery_app beat -l info
```

---

## تنظیمات Virtualizor

در پنل Virtualizor:
1. **Configuration → API** را باز کنید
2. **Enable API** را فعال کنید
3. IP سرور ربات را whitelist کنید
4. **API Key** و **API Pass** را کپی کنید

سپس از پنل ادمین ربات → **سرورهای ویرچولایزور** → اضافه کردن سرور

---

## ساختار پروژه

```
bot/
├── handlers/        # Telegram message/callback handlers
├── keyboards/       # Inline/reply keyboards
├── providers/       # Virtualizor API client
├── services/        # Business logic (billing, server lifecycle)
├── tasks/           # Celery background tasks
├── database/        # SQLAlchemy models + session
└── middlewares/     # Auth, session injection, throttling
```

برای جزئیات کامل → [`PROJECT_STATUS.md`](PROJECT_STATUS.md)

---

## پنل ادمین

دستورات دسترسی:
- `/start` → دکمه **پنل ادمین** (برای کاربران در `ADMIN_IDS`)

بخش‌های پنل:
| بخش | کاربرد |
|-----|---------|
| 👥 کاربران | مدیریت کاربران، ban/credit/پیام |
| 📦 محصولات | CRUD پلن‌ها + ریز-محصولات |
| 🖥 سرورهای ویرچولایزور | افزودن/ویرایش/حذف پنل‌ها |
| 📊 آمار | امروز / ماهانه / بازه دلخواه |
| 📢 پیام همگانی | broadcast با فیلتر + forward واقعی |
| ⚙️ تنظیمات | متن خوش‌آمد، استیکر، پشتیبانی، قوانین |
| 🔒 قفل کانال | مدیریت عضویت اجباری |
| 💰 مالی | شارژ گروهی + تنظیم قیمت |
| 🏷 کدهای تخفیف | ایجاد/ویرایش/حذف |

---

## لایسنس

MIT
