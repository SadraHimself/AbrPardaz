"""Admin: restore database from a backup ZIP uploaded directly to the bot."""
from __future__ import annotations

import asyncio
import io
import os
import re
import subprocess
import zipfile
from urllib.parse import unquote, urlparse

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import ServerStatus, User

router = Router(name="admin_restore")

# محدودیت دانلود Bot API تلگرام برای فایل‌ها
_TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024


def _is_admin(user: User) -> bool:
    """مثل بقیه بخش‌های ادمین: فلگ DB یا ADMIN_IDS از env —
    تا بعد از ریستورِ بکاپی که فلگ ادمین ندارد هم دسترسی قطع نشود."""
    from bot.config import settings
    return user.is_admin or (user.telegram_id in settings.admin_ids)


class RestoreFSM(StatesGroup):
    confirm = State()


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="بله، بازیابی شود",
            callback_data="restore_do",
            **{"style": "danger"},
        ),
        InlineKeyboardButton(
            text="لغو",
            callback_data="restore_cancel",
        ),
    ]])


def _run_psql(sql_bytes: bytes) -> tuple[bool, str]:
    """Run psql restore atomically. Returns (success, error_message).

    ایمنی:
    - --single-transaction: یا کل بکاپ اعمال می‌شود یا هیچ (شکست وسط کار،
      دیتای فعلی را نابود نمی‌کند)
    - ON_ERROR_STOP=1: بدون آن psql با وجود خطا exit=0 می‌داد و ریستورِ
      خراب «موفق» گزارش می‌شد
    - قبل از شروع، سایر اتصال‌ها (سلری/بات) قطع می‌شوند تا DROP TABLE پشت
      قفل نماند؛ سلری در اجرای بعدی خودش دوباره وصل می‌شود
    """
    from bot.config import settings as cfg
    url = (
        cfg.DATABASE_URL
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgresql+psycopg2://", "postgresql://")
    )
    parsed = urlparse(url)
    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)

    base_cmd = [
        "psql",
        "-h", parsed.hostname or "localhost",
        "-p", str(parsed.port or 5432),
        "-U", parsed.username or "postgres",
        "-d", parsed.path.lstrip("/"),
        "--no-password",
    ]

    # ۱) قطع اتصال‌های دیگر به همین دیتابیس (به‌جز اتصال خودِ این psql)
    try:
        subprocess.run(
            base_cmd + ["-c",
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND pid <> pg_backend_pid();"],
            capture_output=True, env=env, timeout=30,
        )
    except Exception:
        pass  # اگر نشد، lock_timeout پایین جلوی هنگ را می‌گیرد

    # ۲) ریستور اتمیک — lock_timeout داخل تراکنش تا در بدترین حالت هم هنگ نکند.
    # اگر دامپ با PostgreSQL جدیدتر گرفته شده باشد، هدرش SETهایی دارد که نسخه
    # قدیمی‌تر نمی‌شناسد (مثل transaction_timeout در PG17) — این SETها صرفاً
    # تنظیم session هستند؛ خط خاطی حذف و خودکار دوباره تلاش می‌شود.
    sql = b"SET lock_timeout = '60s';\n" + sql_bytes
    for _attempt in range(6):
        try:
            result = subprocess.run(
                base_cmd + ["-v", "ON_ERROR_STOP=1", "--single-transaction"],
                input=sql,
                capture_output=True,
                env=env,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return False, ("زمان بازیابی از ۵ دقیقه گذشت و متوقف شد. "
                           "تراکنش برگشت خورد — دیتای فعلی دست‌نخورده است.")
        if result.returncode == 0:
            return True, ""
        err = result.stderr.decode("utf-8", errors="replace")
        m = re.search(r'unrecognized configuration parameter "([^"]+)"', err)
        if not m:
            return False, err
        param = m.group(1).encode()
        sql = re.sub(rb"(?m)^SET\s+" + re.escape(param) + rb"\s*=[^;]*;\s*\n?", b"", sql)
    return False, err


async def _resync_servers() -> dict[str, int]:
    """After restore: re-fetch live status of every server from Virtualizor."""
    from bot.database.models import ProviderAccount, Server
    from bot.database.session import AsyncSessionFactory
    from bot.providers.virtualizor import VirtualizorProvider
    from sqlalchemy import select

    synced = errors = 0
    try:
        async with AsyncSessionFactory() as session:
            result = await session.execute(
                select(Server).where(
                    Server.status != ServerStatus.DELETED,
                    Server.provider_server_id.isnot(None),
                    Server.provider_account_id.isnot(None),
                )
            )
            servers = list(result.scalars().all())

            # Cache provider clients to avoid re-creating per server
            clients: dict[int, VirtualizorProvider] = {}

            for server in servers:
                try:
                    acc_id = server.provider_account_id
                    if acc_id not in clients:
                        account = await session.get(ProviderAccount, acc_id)
                        if not account or not account.api_endpoint:
                            continue
                        clients[acc_id] = VirtualizorProvider(
                            account.api_endpoint,
                            account.api_key,
                            account.api_secret,
                        )
                    prov = clients[acc_id]
                    info = await prov.get_server(server.provider_server_id)

                    # Map status string back to ServerStatus enum
                    status_map = {
                        "active": ServerStatus.ACTIVE,
                        "off": ServerStatus.ACTIVE,
                        "suspended": ServerStatus.SUSPENDED,
                        "building": ServerStatus.BUILDING,
                    }
                    if info.status in status_map and server.status not in (
                        ServerStatus.SUSPENDED, ServerStatus.DELETED
                    ):
                        server.status = status_map[info.status]

                    # Update machine_status in extra_data
                    extra = dict(server.extra_data or {})
                    extra["machine_status"] = "1" if info.status == "active" else "0"
                    server.extra_data = extra

                    if info.ip_address:
                        server.ip_address = info.ip_address

                    synced += 1
                except Exception:
                    errors += 1

            await session.commit()
    except Exception:
        pass

    return {"synced": synced, "errors": errors}


@router.message(F.document)
async def handle_zip_upload(message: Message, user: User, state: FSMContext):
    if not _is_admin(user):
        return
    # فقط در چت خصوصی — فوروارد بکاپ داخل گروه لاگ نباید prompt ریستور باز کند
    if message.chat.type != "private":
        return
    doc = message.document
    if not doc or not doc.file_name or not doc.file_name.endswith(".zip"):
        return

    if (doc.file_size or 0) > _TG_DOWNLOAD_LIMIT:
        await message.answer(
            '‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            "حجم فایل بیش از ۲۰ مگابایت است — Bot API تلگرام اجازه دانلود نمی‌دهد.\n"
            "بکاپ را مستقیم روی سرور بازیابی کنید:\n"
            "<code>unzip backup.zip && psql -U abrpardaz -d abrpardaz "
            "-v ON_ERROR_STOP=1 --single-transaction -f backup_*.sql</code>",
            parse_mode="HTML",
        )
        return

    size_kb = (doc.file_size or 0) // 1024
    await state.set_state(RestoreFSM.confirm)
    await state.update_data(file_id=doc.file_id, file_name=doc.file_name)

    await message.answer(
        f"<b>بازیابی دیتابیس</b>\n\n"
        f"فایل: <code>{doc.file_name}</code>\n"
        f"حجم: <b>{size_kb} KB</b>\n\n"
        f"<b>هشدار:</b> تمام داده‌های فعلی با داده‌های بکاپ جایگزین می‌شوند.\n"
        f"پس از بازیابی، سرورها با Virtualizor همگام می‌شوند.\n\n"
        f"ادامه می‌دهید؟",
        parse_mode="HTML",
        reply_markup=_confirm_kb(),
    )


@router.callback_query(RestoreFSM.confirm, F.data == "restore_do")
async def cb_restore_confirm(cb: CallbackQuery, user: User, bot: Bot,
                             state: FSMContext, session: AsyncSession):
    if not _is_admin(user):
        await cb.answer("دسترسی ندارید", show_alert=True)
        return

    data = await state.get_data()
    file_id = data.get("file_id")
    file_name = data.get("file_name", "backup.zip")
    await state.clear()
    await cb.answer()

    await cb.message.edit_text("در حال دریافت فایل از تلگرام...")

    # 1. Download ZIP from Telegram
    try:
        tg_file = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download_file(tg_file.file_path, buf)
        buf.seek(0)
    except Exception as e:
        await cb.message.edit_text(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"خطا در دریافت فایل:\n<code>{e}</code>",
            parse_mode="HTML",
        )
        return

    # 2. Extract SQL from ZIP (بزرگ‌ترین فایل sql = دامپ اصلی)
    try:
        with zipfile.ZipFile(buf) as zf:
            sql_names = [n for n in zf.namelist() if n.endswith(".sql")]
            if not sql_names:
                await cb.message.edit_text("فایل SQL داخل ZIP پیدا نشد.")
                return
            sql_names.sort(key=lambda n: zf.getinfo(n).file_size, reverse=True)
            sql_bytes = zf.read(sql_names[0])
            sql_kb = len(sql_bytes) // 1024
    except Exception as e:
        await cb.message.edit_text(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"خطا در باز کردن ZIP:\n<code>{e}</code>",
            parse_mode="HTML",
        )
        return

    await cb.message.edit_text(
        f"در حال بازیابی دیتابیس...\n"
        f"<i>({sql_kb} KB — ممکن است چند دقیقه طول بکشد)</i>",
        parse_mode="HTML",
    )

    # 3. سشنِ همین آپدیت را می‌بندیم و pool را خالی می‌کنیم تا اتصال زنده‌ای از
    #    خود بات پشت DROP TABLE قفل نماند. (کامیت پایانیِ middleware بعداً روی
    #    اتصال تازه انجام می‌شود و بی‌ضرر است.)
    try:
        await session.commit()
        await session.close()
    except Exception:
        pass
    try:
        from bot.database.session import engine
        await engine.dispose()
    except Exception:
        pass

    # 4. Run psql restore in a thread (blocking subprocess)
    try:
        ok, err = await asyncio.to_thread(_run_psql, sql_bytes)
    except Exception as e:
        ok, err = False, str(e)

    if not ok:
        await cb.message.edit_text(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"<b>خطا در بازیابی</b>\n\n<code>{err[:800]}</code>\n\n"
            "<i>بازیابی اتمیک است — دیتای فعلی دست‌نخورده باقی مانده.</i>",
            parse_mode="HTML",
        )
        return

    # 5. Pool تازه + ساخت جدول‌هایی که در بکاپ قدیمی نبوده‌اند (مثلاً جدول‌های جدید)
    try:
        from bot.database.models import Base
        from bot.database.session import engine
        await engine.dispose()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception:
        pass

    await cb.message.edit_text(
        "دیتابیس بازیابی شد.\nدر حال همگام‌سازی سرورها با Virtualizor..."
    )

    # 6. Re-sync all servers with live Virtualizor status
    sync = await _resync_servers()

    await cb.message.edit_text(
        f"<b>بازیابی کامل شد</b>\n\n"
        f"فایل: <code>{file_name}</code>\n"
        f"سرورهای همگام‌شده: <b>{sync['synced']}</b>\n"
        f"خطا در همگام‌سازی: <b>{sync['errors']}</b>\n\n"
        "برای اطمینان از پاک‌شدن کش‌ها، سرویس‌ها را ریستارت کنید:\n"
        "<code>systemctl restart abrpardaz-bot abrpardaz-worker abrpardaz-beat</code>",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "restore_cancel")
async def cb_restore_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("عملیات بازیابی لغو شد.")
    await cb.answer()
