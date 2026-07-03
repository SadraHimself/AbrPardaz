"""Admin: restore database from a backup ZIP uploaded directly to the bot."""
from __future__ import annotations

import asyncio
import io
import os
import subprocess
import zipfile
from urllib.parse import urlparse

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.database.models import ServerStatus, User

router = Router(name="admin_restore")


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
    """Run psql restore. Returns (success, error_message)."""
    from bot.config import settings as cfg
    url = (
        cfg.DATABASE_URL
        .replace("postgresql+asyncpg://", "postgresql://")
        .replace("postgresql+psycopg2://", "postgresql://")
    )
    parsed = urlparse(url)
    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = parsed.password

    result = subprocess.run(
        [
            "psql",
            "-h", parsed.hostname or "localhost",
            "-p", str(parsed.port or 5432),
            "-U", parsed.username or "postgres",
            "-d", parsed.path.lstrip("/"),
            "--no-password",
        ],
        input=sql_bytes,
        capture_output=True,
        env=env,
        timeout=300,
    )
    if result.returncode != 0:
        return False, result.stderr.decode("utf-8", errors="replace")
    return True, ""


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
    if not user.is_admin:
        return
    doc = message.document
    if not doc or not doc.file_name or not doc.file_name.endswith(".zip"):
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
async def cb_restore_confirm(cb: CallbackQuery, user: User, bot: Bot, state: FSMContext):
    if not user.is_admin:
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
            f"خطا در دریافت فایل:\n<code>{e}</code>",
            parse_mode="HTML",
        )
        return

    # 2. Extract SQL from ZIP
    try:
        with zipfile.ZipFile(buf) as zf:
            sql_names = [n for n in zf.namelist() if n.endswith(".sql")]
            if not sql_names:
                await cb.message.edit_text("فایل SQL داخل ZIP پیدا نشد.")
                return
            sql_bytes = zf.read(sql_names[0])
            sql_kb = len(sql_bytes) // 1024
    except Exception as e:
        await cb.message.edit_text(
            f"خطا در باز کردن ZIP:\n<code>{e}</code>",
            parse_mode="HTML",
        )
        return

    await cb.message.edit_text(
        f"در حال بازیابی دیتابیس...\n"
        f"<i>({sql_kb} KB — ممکن است چند دقیقه طول بکشد)</i>",
        parse_mode="HTML",
    )

    # 3. Run psql restore in a thread (blocking subprocess)
    ok, err = await asyncio.to_thread(_run_psql, sql_bytes)

    if not ok:
        await cb.message.edit_text(
            f"<b>خطا در بازیابی</b>\n\n<code>{err[:800]}</code>",
            parse_mode="HTML",
        )
        return

    # 4. Dispose SQLAlchemy pool so next queries get fresh connections
    try:
        from bot.database.session import engine
        await engine.dispose()
    except Exception:
        pass

    await cb.message.edit_text(
        "دیتابیس بازیابی شد.\nدر حال همگام‌سازی سرورها با Virtualizor..."
    )

    # 5. Re-sync all servers with live Virtualizor status
    sync = await _resync_servers()

    await cb.message.edit_text(
        f"<b>بازیابی کامل شد</b>\n\n"
        f"فایل: <code>{file_name}</code>\n"
        f"سرورهای همگام‌شده: <b>{sync['synced']}</b>\n"
        f"خطا در همگام‌سازی: <b>{sync['errors']}</b>\n\n"
        f"ربات آماده به کار است.",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "restore_cancel")
async def cb_restore_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("عملیات بازیابی لغو شد.")
    await cb.answer()
