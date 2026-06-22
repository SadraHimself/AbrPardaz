# Abr Pardaz — راهنمای راه‌اندازی

## پیش‌نیازها
- Python 3.12+
- PostgreSQL 15+
- Redis 7+
- Docker & Docker Compose (اختیاری)

---

## راه‌اندازی سریع با Docker

```bash
cp .env.example .env
# ویرایش فایل .env با توکن ربات و اطلاعات پنل‌ها

docker-compose up -d
```

---

## راه‌اندازی دستی

### 1. نصب dependencies
```bash
python -m venv .venv
.venv\Scripts\activate    # Windows
pip install -r requirements.txt
```

### 2. پیکربندی .env
```bash
cp .env.example .env
# BOT_TOKEN, DATABASE_URL, REDIS_URL و بقیه را پر کنید
```

### 3. ساخت دیتابیس
```bash
# migration اول
alembic revision --autogenerate -m "initial"
alembic upgrade head
```

### 4. اجرای ربات
```bash
python -m bot.main
```

### 5. اجرای Mini App Backend
```bash
uvicorn bot.webapp.app:app --host 0.0.0.0 --port 8000 --reload
```

### 6. اجرای Celery (بیلینگ خودکار)
```bash
# Worker
celery -A bot.tasks.celery_app worker --loglevel=info

# Beat scheduler (در ترمینال جداگانه)
celery -A bot.tasks.celery_app beat --loglevel=info
```

---

## اضافه کردن پروایدر و پلن (از داخل ربات)

**اضافه کردن پروایدر (ادمین):**
```
/addprovider virtualizor "سرور ایران DC1" API_KEY API_PASS https://panel.yourdomain.com:4083
```

**اضافه کردن پلن:**
```
/addplan virtualizor "vps-1" 1024 1 20 1000 500 15000 iran-dc1
# /addplan <provider> <name> <ram_mb> <cpu> <disk_gb> <bw_gb> <price_hourly> <price_monthly> <location>
```

**شارژ کاربر:**
```
/credit 123456789 50000
```

---

## ساختار پروژه

```
telecloud-bot/
├── bot/
│   ├── main.py              # Entry point
│   ├── config.py            # تنظیمات
│   ├── database/            # مدل‌های SQLAlchemy
│   ├── handlers/            # هندلرهای ربات
│   │   ├── start.py         # منوی اصلی
│   │   ├── auth.py          # احراز هویت + شاهکار
│   │   ├── servers.py       # مدیریت سرور
│   │   ├── billing.py       # کیف پول
│   │   └── admin.py         # پنل ادمین
│   ├── providers/           # پروایدرها
│   │   ├── base.py          # interface انتزاعی
│   │   ├── virtualizor.py   # Virtualizor KVM
│   │   ├── hetzner.py       # Hetzner Cloud
│   │   ├── digitalocean.py  # DigitalOcean
│   │   ├── vultr.py         # Vultr
│   │   └── linode.py        # Linode
│   ├── services/            # منطق کسب‌وکار
│   │   ├── billing.py       # بیلینگ
│   │   ├── server.py        # مدیریت سرور
│   │   ├── shahkar.py       # احراز هویت شاهکار
│   │   └── notification.py  # اطلاع‌رسانی
│   ├── tasks/               # Celery tasks
│   │   ├── celery_app.py    # تنظیمات Celery
│   │   ├── billing.py       # بیلینگ خودکار
│   │   └── server.py        # task های سرور
│   ├── keyboards/           # کیبوردها
│   ├── middlewares/         # middleware ها
│   └── webapp/              # FastAPI Mini App
├── webapp/
│   └── index.html           # Mini App UI (placeholder)
├── alembic/                 # migrations
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

---

## چه چیزی در فاز بعدی اضافه می‌شود؟
- [ ] درگاه پرداخت (ZarinPal / IDPay)
- [ ] UI کامل Mini App
- [ ] انتخاب OS هنگام خرید سرور
- [ ] پنل مدیریت پلن‌ها از ربات
- [ ] گزارش‌گیری مالی ادمین
- [ ] پشتیبانی IPv6 کامل
- [ ] سیستم تیکت پشتیبانی
