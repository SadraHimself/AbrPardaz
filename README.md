# AbrPardaz 🖥

**ربات ابر پرداز** — ربات تلگرام برای فروش و مدیریت سرور مجازی (VPS) روی پنل Virtualizor

[![GitHub](https://img.shields.io/badge/GitHub-AbrPardaz-blue?logo=github)](https://github.com/SadraHimself/AbrPardaz)
[![Python](https://img.shields.io/badge/Python-3.11+-green?logo=python)](https://python.org)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-blue)](https://aiogram.dev)

---

## نصب سریع (یک خط)

```bash
curl -fsSL https://raw.githubusercontent.com/SadraHimself/AbrPardaz/main/install.sh | sudo bash
```

> اسکریپت به صورت خودکار ربات را در `/opt/abrpardaz` نصب می‌کند، محیط Python می‌سازد، سرویس‌های systemd را ثبت می‌کند و ربات را راه‌اندازی می‌کند.
>
> **پیش‌نیاز:** Ubuntu/Debian یا CentOS/RHEL — اجرا به عنوان root یا با sudo

---

## ویژگی‌ها

- **خرید سرور** با انتخاب OS از Virtualizor، hostname دلخواه، نوع بیلینگ (ساعتی / ماهانه)
- **مدیریت سرور**: روشن/خاموش/ریبوت/حذف، تغییر IP، اتصال VNC
- **ترافیک**: نمایش مصرف + خرید ترافیک اضافه و IP اضافه (sub-products)
- **کیف پول**: شارژ آنلاین، تاریخچه تراکنش‌ها
- **کد تخفیف**: درصدی، با انقضا و محدودیت استفاده، قابل اختصاص به کاربر خاص
- **احراز هویت**: تأیید شماره موبایل ایرانی از طریق Shahkar
- **قوانین و مقررات**: flow قبول قوانین + عضویت اجباری در کانال
- **پنل ادمین کامل**: مدیریت کاربران، محصولات، آمار، تنظیمات، broadcast

---

## پیش‌نیازها

| نرم‌افزار | نسخه |
|-----------|-------|
| Python | 3.11+ |
| PostgreSQL | 14+ |
| Redis | 6+ |
| Virtualizor | پنل KVM با API فعال |

---

## نصب دستی

### ۱. دریافت کد

```bash
git clone https://github.com/SadraHimself/AbrPardaz.git /opt/abrpardaz
cd /opt/abrpardaz
```

### ۲. محیط Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### ۳. فایل تنظیمات

```bash
cp .env.example .env
nano .env
```

متغیرهای اجباری:

```env
BOT_TOKEN=your_bot_token
ADMIN_IDS=[123456789]
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/abrpardaz
REDIS_URL=redis://localhost:6379/0
```

### ۴. مایگریشن دیتابیس

```bash
alembic upgrade head
```

### ۵. اجرا

```bash
# ربات
python -m bot.main

# Celery worker (بیلینگ + sync ترافیک)
celery -A bot.tasks.celery_app worker -l info

# Celery beat (وظایف زمان‌بندی‌شده)
celery -A bot.tasks.celery_app beat -l info
```

---

## تنظیمات Virtualizor

در پنل Virtualizor:

1. **Configuration → API** را باز کنید
2. **Enable API** را فعال کنید
3. IP سرور ربات را در لیست مجاز اضافه کنید
4. **API Key** و **API Pass** را کپی کنید

سپس از پنل ادمین ربات → **🖥 سرورهای ویرچولایزور** → اضافه کردن سرور

---

## ساختار پروژه

```
/opt/abrpardaz/
├── bot/
│   ├── handlers/        # هندلرهای تلگرام
│   ├── keyboards/       # کیبوردهای inline/reply
│   ├── providers/       # کلاینت Virtualizor API
│   ├── services/        # منطق کسب‌وکار (بیلینگ، سرور)
│   ├── tasks/           # وظایف Celery (بیلینگ، آمار، ترافیک)
│   ├── database/        # مدل‌های SQLAlchemy
│   └── middlewares/     # احراز هویت، session، throttling
├── alembic/             # مایگریشن‌های دیتابیس
├── install.sh           # اسکریپت نصب خودکار
├── .env.example         # نمونه فایل تنظیمات
└── PROJECT_STATUS.md    # وضعیت کامل پروژه
```

برای جزئیات کامل تمام فیچرها و مدل‌ها → [PROJECT_STATUS.md](PROJECT_STATUS.md)

---

## پنل ادمین

دسترسی: `/start` ← دکمه **⚙️ پنل ادمین** (فقط برای `ADMIN_IDS`)

| بخش | کاربرد |
|-----|---------|
| 👥 کاربران | لیست، جستجو، ban/unban، شارژ/برداشت کیف پول، KYC دستی |
| 📦 محصولات | CRUD پلن‌ها + auto-fetch مشخصات از Virtualizor |
| 📎 ریز-محصولات | ترافیک اضافه و IP اضافه به ازای هر پلن |
| 🖥 سرورهای ویرچولایزور | افزودن پنل، تست اتصال، KYC سختگیرانه |
| 📊 آمار | امروز / ماهانه / بازه تاریخ دلخواه |
| 📢 پیام همگانی | broadcast با فیلتر + forward واقعی + rate limit |
| ⚙️ تنظیمات | متن خوش‌آمد، استیکر، پشتیبانی، قوانین، حالت تعمیر |
| 🔒 قفل کانال | مدیریت عضویت اجباری |
| 💰 مالی | شارژ گروهی کاربران + تنظیم قیمت گروهی |
| 🏷 کدهای تخفیف | ایجاد، ویرایش، حذف، اختصاص به کاربر |

---

## لایسنس

MIT — [SadraHimself](https://github.com/SadraHimself)
