"""Celery application + beat schedule."""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from bot.config import settings

app = Celery("telecloud")

app.conf.update(
    broker_url=settings.CELERY_BROKER_URL,
    result_backend=settings.CELERY_RESULT_BACKEND,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Tehran",
    enable_utc=True,
    # Periodic tasks
    beat_schedule={
        # هر ساعت بیلینگ ساعتی اجرا می‌شود
        "hourly-billing": {
            "task": "bot.tasks.billing.run_hourly_billing",
            "schedule": crontab(minute=0),
        },
        # هر شب نیمه‌شب بیلینگ ماهیانه بررسی می‌شود
        "monthly-expiry-check": {
            "task": "bot.tasks.billing.run_monthly_expiry_check",
            "schedule": crontab(hour=0, minute=5),
        },
        # هر 15 دقیقه ترافیک سرورها آپدیت می‌شود
        "traffic-sync": {
            "task": "bot.tasks.server.sync_all_traffic",
            "schedule": crontab(minute="*/15"),
        },
        # هر ۵ دقیقه سرورهای در حال ساخت بررسی می‌شوند
        "sync-building": {
            "task": "bot.tasks.server.sync_building_servers",
            "schedule": crontab(minute="*/5"),
        },
        # هر روز موجودی‌های کم هشدار داده می‌شود
        "low-balance-alert": {
            "task": "bot.tasks.billing.send_low_balance_alerts",
            "schedule": crontab(hour=10, minute=0),
        },
        # هر روز ساعت ۰۰:۳۰ آمار روزانه جمع‌آوری می‌شود
        "daily-stats": {
            "task": "bot.tasks.stats.aggregate_daily_stats",
            "schedule": crontab(hour=0, minute=30),
        },
        # هر هفته آمارهای قدیمی (بیش از ۳۰ روز) پاک می‌شوند
        "cleanup-old-stats": {
            "task": "bot.tasks.stats.cleanup_old_stats",
            "schedule": crontab(hour=3, minute=0, day_of_week=0),
        },
        # هر ۴ ساعت بکاپ کامل دیتابیس گرفته و به تلگرام ارسال می‌شود
        "database-backup": {
            "task": "bot.tasks.backup.run_database_backup",
            "schedule": crontab(minute=0, hour="*/4"),
        },
    },
)

# Import tasks to register them
import bot.tasks.backup   # noqa: F401, E402
import bot.tasks.billing  # noqa: F401, E402
import bot.tasks.server   # noqa: F401, E402
import bot.tasks.stats    # noqa: F401, E402
