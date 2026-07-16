"""User-facing Hetzner snapshot flows: create / delete / restore.

- ساخت: از سرور هتزنر اسنپ‌شات می‌گیرد → حجم واقعی (image_size) خوانده → ردیف
  Snapshot ذخیره + هزینه‌ی ساعتی محاسبه و اولین ساعت فوری کسر می‌شود.
- حذف: از هتزنر (DELETE /images) + غیرفعال‌سازی ردیف.
- ریستور: rebuild سرور با image اسنپ‌شات — با چک سازگاری معماری و disk_size
  پیش از فراخوانی (چون rebuild دیسک را پاک می‌کند). اسنپ‌شاتِ هر سرویس هتزنر
  روی هر سرویس هتزنرِ دیگرِ همان کاربر قابل استفاده است (ماهانه→ساعتی هم مجاز).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import (
    ProviderAccount, ProviderType, Server, ServerStatus, Snapshot, User,
)
from bot.providers.hetzner import HetznerProvider
from bot.services.billing import BillingService
from bot.services.log_service import LogService
from bot.services.snapshot import hourly_toman, sell_hourly_eur
from bot.utils.loading import ERR

logger = logging.getLogger(__name__)
router = Router(name="snapshots")

_SNAP = '‏<tg-emoji emoji-id="5346269127059196142">📸</tg-emoji>'
_BACK = "5258236805890710909"


def _hz_provider(account: ProviderAccount) -> HetznerProvider:
    return HetznerProvider(api_token=account.api_key or "")


# ── منوی اسنپ‌شات (از دکمه‌ی جزئیات سرور) ─────────────────────────────────────

@router.callback_query(F.data.startswith("srv_snap:"))
async def cb_snap_menu(cb: CallbackQuery, user: User, session: AsyncSession):
    server_id = int(cb.data.split(":")[1])
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    if server.provider_type != ProviderType.HETZNER:
        await cb.answer("اسنپ‌شات فقط برای سرورهای هتزنر است.", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text(
        f"{_SNAP} <b>اسنپ‌شات — {server.name}</b>\n\n"
        "اسنپ‌شات یک نسخه‌ی کامل از دیسک سرور است که می‌توانید بعداً روی هر "
        "سرویس هتزنر خود بازگردانید. هزینه‌ی نگهداری ساعتی از کیف پول کسر می‌شود.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="ساخت اسنپ‌شات از این سرویس",
                                  callback_data=f"snap_new:{server_id}")],
            [InlineKeyboardButton(text="استفاده از اسنپ‌شات روی این سرویس",
                                  callback_data=f"snap_use:{server_id}")],
            [InlineKeyboardButton(text="اسنپ‌شات‌های من",
                                  callback_data=f"snap_list:{server_id}")],
            [InlineKeyboardButton(text="بازگشت", callback_data=f"server:{server_id}",
                                  **{"icon_custom_emoji_id": _BACK})],
        ]),
    )


# ── ساخت اسنپ‌شات ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("snap_new:"))
async def cb_snap_new_confirm(cb: CallbackQuery, user: User, session: AsyncSession):
    server_id = int(cb.data.split(":")[1])
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id or server.provider_type != ProviderType.HETZNER:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text(
        f"{_SNAP} <b>ساخت اسنپ‌شات</b>\n\n"
        f"از سرویس <b>{server.name}</b> یک اسنپ‌شات ساخته می‌شود.\n"
        "هزینه‌ی نگهداری آن به‌صورت ساعتی از کیف پول شما کسر خواهد شد "
        "(هزینه بستگی به حجم واقعی دیسک دارد و پس از ساخت مشخص می‌شود).\n\n"
        "ادامه می‌دهید؟",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="بله، بساز", callback_data=f"snap_new_do:{server_id}",
                                 **{"style": "success", "icon_custom_emoji_id": "5206607081334906820"}),
            InlineKeyboardButton(text="انصراف", callback_data=f"srv_snap:{server_id}",
                                 **{"style": "danger", "icon_custom_emoji_id": "5240241223632954241"}),
        ]]),
    )


@router.callback_query(F.data.startswith("snap_new_do:"))
async def cb_snap_new_do(cb: CallbackQuery, user: User, session: AsyncSession):
    server_id = int(cb.data.split(":")[1])
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id or server.provider_type != ProviderType.HETZNER:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    account = await session.get(ProviderAccount, server.provider_account_id) if server.provider_account_id else None
    if not account:
        await cb.answer("اطلاعات سرویس‌دهنده یافت نشد.", show_alert=True)
        return

    await cb.answer("در حال ساخت اسنپ‌شات...")
    try:
        await cb.message.delete()
    except Exception:
        pass
    wait = await cb.message.answer(
        '‏<tg-emoji emoji-id="5386367538735104399">⌛️</tg-emoji> '
        "در حال ساخت اسنپ‌شات... (ممکن است چند دقیقه طول بکشد)",
        parse_mode="HTML")

    prov = _hz_provider(account)
    try:
        info = await prov.create_snapshot(
            server.provider_server_id,
            description=f"{server.name} — {user.telegram_id}",
            labels={"tg_user_id": str(user.telegram_id), "src_server": str(server.id)},
        )
        ppg = await prov.snapshot_price_per_gb_month()
    except Exception as e:
        await wait.edit_text(
            f"{ERR} ساخت اسنپ‌شات ناموفق بود:\n<code>{str(e)[:300]}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="بازگشت", callback_data=f"server:{server_id}",
                                     **{"icon_custom_emoji_id": _BACK})]]),
        )
        return

    size_gb = float(info.get("image_size") or 0)
    cost_monthly_eur = size_gb * ppg

    snap = Snapshot(
        user_id=user.id,
        provider_account_id=account.id,
        hetzner_image_id=str(info["id"]),
        description=server.name,
        source_server_name=server.name,
        size_gb=size_gb,
        disk_size=int(info.get("disk_size") or 0),
        architecture=info.get("architecture", "x86"),
        is_active=True,
        last_billed_at=datetime.now(timezone.utc),
        extra_data={"currency": "eur", "cost_monthly_eur": cost_monthly_eur,
                    "price_per_gb_month_eur": ppg},
    )
    session.add(snap)
    await session.flush()

    # کسر اولین ساعت بلافاصله
    per_hour = await hourly_toman(session, snap, account)
    charged_note = ""
    if per_hour > 0:
        billing = BillingService(session)
        ok = await billing.debit(
            user.id, per_hour,
            description=f"اسنپ‌شات — {server.name}")
        charged_note = (f"\nاولین ساعت کسر شد: {per_hour:,.0f} تومان" if ok
                        else "\n⚠️ موجودی برای کسر ساعت اول کافی نبود؛ در دور بعدی تلاش می‌شود.")

    await LogService(cb.bot, session).log_snapshot_created(
        user, server.name, size_gb, per_hour)

    await wait.edit_text(
        f'<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> '
        f"<b>اسنپ‌شات ساخته شد</b>\n\n"
        f"منبع: {server.name}\n"
        f"حجم: {size_gb:g} GB\n"
        f"هزینه ساعتی: {per_hour:,.0f} تومان{charged_note}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="اسنپ‌شات‌های من", callback_data=f"snap_list:{server_id}")],
            [InlineKeyboardButton(text="بازگشت", callback_data=f"server:{server_id}",
                                  **{"icon_custom_emoji_id": _BACK})]]),
    )


# ── لیست اسنپ‌شات‌های کاربر + حذف ────────────────────────────────────────────

async def _user_snapshots(session: AsyncSession, user_id: int) -> list[Snapshot]:
    rows = (await session.execute(
        select(Snapshot).where(Snapshot.user_id == user_id, Snapshot.is_active == True)
        .order_by(Snapshot.created_at.desc())
    )).scalars().all()
    return list(rows)


@router.callback_query(F.data.startswith("snap_list:"))
async def cb_snap_list(cb: CallbackQuery, user: User, session: AsyncSession):
    server_id = int(cb.data.split(":")[1])
    snaps = await _user_snapshots(session, user.id)
    await cb.answer()
    if not snaps:
        await cb.message.edit_text(
            f"{_SNAP} <b>اسنپ‌شات‌های من</b>\n\nهنوز اسنپ‌شاتی ندارید.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="بازگشت", callback_data=f"srv_snap:{server_id}",
                                     **{"icon_custom_emoji_id": _BACK})]]),
        )
        return
    rows = []
    for s in snaps:
        acc = await session.get(ProviderAccount, s.provider_account_id)
        per_hour_eur = sell_hourly_eur(s, acc) if acc else 0
        rows.append([InlineKeyboardButton(
            text=f"{s.source_server_name or '—'} · {s.size_gb:g}GB · {s.architecture}",
            callback_data=f"snap_show:{s.id}:{server_id}")])
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data=f"srv_snap:{server_id}",
                                      **{"icon_custom_emoji_id": _BACK})])
    await cb.message.edit_text(
        f"{_SNAP} <b>اسنپ‌شات‌های من</b> ({len(snaps)})\n\nیک اسنپ‌شات را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("snap_show:"))
async def cb_snap_show(cb: CallbackQuery, user: User, session: AsyncSession):
    _, snap_id, server_id = cb.data.split(":")
    snap = await session.get(Snapshot, int(snap_id))
    if not snap or snap.user_id != user.id or not snap.is_active:
        await cb.answer("اسنپ‌شات یافت نشد.", show_alert=True)
        return
    acc = await session.get(ProviderAccount, snap.provider_account_id)
    per_hour = await hourly_toman(session, snap, acc) if acc else 0
    await cb.answer()
    await cb.message.edit_text(
        f"{_SNAP} <b>اسنپ‌شات {snap.source_server_name or ''}</b>\n\n"
        f"حجم: {snap.size_gb:g} GB\n"
        f"معماری: {snap.architecture}\n"
        f"حداقل دیسک برای بازگردانی: {snap.disk_size} GB\n"
        f"هزینه ساعتی: {per_hour:,.0f} تومان\n"
        f"ساخته شده: {snap.created_at.strftime('%Y/%m/%d')}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="حذف اسنپ‌شات", callback_data=f"snap_del:{snap.id}:{server_id}",
                                  **{"style": "danger"})],
            [InlineKeyboardButton(text="بازگشت", callback_data=f"snap_list:{server_id}",
                                  **{"icon_custom_emoji_id": _BACK})],
        ]),
    )


@router.callback_query(F.data.startswith("snap_del:"))
async def cb_snap_del(cb: CallbackQuery, user: User, session: AsyncSession):
    _, snap_id, server_id = cb.data.split(":")
    snap = await session.get(Snapshot, int(snap_id))
    if not snap or snap.user_id != user.id or not snap.is_active:
        await cb.answer("اسنپ‌شات یافت نشد.", show_alert=True)
        return
    account = await session.get(ProviderAccount, snap.provider_account_id)
    await cb.answer("در حال حذف...")
    if account:
        try:
            await _hz_provider(account).delete_image(snap.hetzner_image_id)
        except Exception as e:
            # اگر از سمت هتزنر قبلاً حذف شده باشد، بی‌ضرر است؛ فقط لاگ
            logger.warning("snapshot delete provider error %s: %s", snap.id, e)
    snap.is_active = False
    await session.flush()
    await LogService(cb.bot, session).log_snapshot_deleted(user, snap.source_server_name or "—")
    await cb.message.edit_text(
        f'<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> اسنپ‌شات حذف شد.',
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="بازگشت", callback_data=f"snap_list:{server_id}",
                                 **{"icon_custom_emoji_id": _BACK})]]),
    )


# ── استفاده از اسنپ‌شات روی یک سرویس (ریستور) ────────────────────────────────

@router.callback_query(F.data.startswith("snap_use:"))
async def cb_snap_use(cb: CallbackQuery, user: User, session: AsyncSession):
    server_id = int(cb.data.split(":")[1])
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id or server.provider_type != ProviderType.HETZNER:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    account = await session.get(ProviderAccount, server.provider_account_id) if server.provider_account_id else None
    if not account:
        await cb.answer("اطلاعات سرویس‌دهنده یافت نشد.", show_alert=True)
        return

    await cb.answer("در حال بررسی سازگاری...")
    try:
        target = await _hz_provider(account).server_type_info(server.provider_server_id)
    except Exception as e:
        await cb.message.answer(f"{ERR} خطا در خواندن مشخصات سرور: <code>{str(e)[:200]}</code>",
                                parse_mode="HTML")
        return

    snaps = await _user_snapshots(session, user.id)
    # فقط اسنپ‌شات‌های سازگار: معماری یکسان + disk_size ≤ دیسک سرور مقصد
    compatible = [s for s in snaps
                  if s.architecture == target["architecture"] and s.disk_size <= target["disk"]]

    if not compatible:
        await cb.message.edit_text(
            f"{_SNAP} <b>استفاده از اسنپ‌شات</b>\n\n"
            "اسنپ‌شات سازگاری برای این سرور ندارید.\n"
            f"<i>معماری سرور: {target['architecture']} · دیسک: {target['disk']} GB — "
            "اسنپ‌شات باید هم‌معماری و حجم دیسکش کوچک‌تر یا مساوی باشد.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="بازگشت", callback_data=f"srv_snap:{server_id}",
                                     **{"icon_custom_emoji_id": _BACK})]]),
        )
        return

    rows = [[InlineKeyboardButton(
        text=f"{s.source_server_name or '—'} · {s.size_gb:g}GB",
        callback_data=f"snap_restore:{s.id}:{server_id}")] for s in compatible]
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data=f"srv_snap:{server_id}",
                                      **{"icon_custom_emoji_id": _BACK})])
    await cb.message.edit_text(
        f"{_SNAP} <b>استفاده از اسنپ‌شات روی {server.name}</b>\n\n"
        '‏<tg-emoji emoji-id="6008233706039284019">⚠️</tg-emoji> با بازگردانی، تمام اطلاعات '
        "فعلیِ این سرور پاک و با محتوای اسنپ‌شات جایگزین می‌شود. یک اسنپ‌شات انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("snap_restore:"))
async def cb_snap_restore_confirm(cb: CallbackQuery, user: User, session: AsyncSession):
    _, snap_id, server_id = cb.data.split(":")
    snap = await session.get(Snapshot, int(snap_id))
    server = await session.get(Server, int(server_id))
    if not snap or snap.user_id != user.id or not snap.is_active or not server or server.user_id != user.id:
        await cb.answer("یافت نشد.", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text(
        f"{_SNAP} <b>تأیید بازگردانی</b>\n\n"
        f"اسنپ‌شات: {snap.source_server_name or '—'} ({snap.size_gb:g}GB)\n"
        f"روی سرور: {server.name}\n\n"
        '‏<tg-emoji emoji-id="6008233706039284019">⚠️</tg-emoji> '
        "<b>تمام اطلاعات فعلی این سرور پاک می‌شود و قابل بازگشت نیست.</b>\n"
        "ادامه می‌دهید؟",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="بله، بازگردان", callback_data=f"snap_restore_do:{snap.id}:{server_id}",
                                 **{"style": "danger", "icon_custom_emoji_id": "5206607081334906820"}),
            InlineKeyboardButton(text="انصراف", callback_data=f"snap_use:{server_id}",
                                 **{"style": "success", "icon_custom_emoji_id": "5240241223632954241"}),
        ]]),
    )


@router.callback_query(F.data.startswith("snap_restore_do:"))
async def cb_snap_restore_do(cb: CallbackQuery, user: User, session: AsyncSession):
    _, snap_id, server_id = cb.data.split(":")
    snap = await session.get(Snapshot, int(snap_id))
    server = await session.get(Server, int(server_id))
    if not snap or snap.user_id != user.id or not snap.is_active or not server or server.user_id != user.id:
        await cb.answer("یافت نشد.", show_alert=True)
        return
    account = await session.get(ProviderAccount, server.provider_account_id) if server.provider_account_id else None
    if not account:
        await cb.answer("اطلاعات سرویس‌دهنده یافت نشد.", show_alert=True)
        return

    await cb.answer("در حال بازگردانی...")
    try:
        await cb.message.delete()
    except Exception:
        pass
    wait = await cb.message.answer(
        '‏<tg-emoji emoji-id="5386367538735104399">⌛️</tg-emoji> '
        "در حال بازگردانی اسنپ‌شات... (چند دقیقه)",
        parse_mode="HTML")

    prov = _hz_provider(account)
    try:
        # چک نهاییِ سازگاری قبل از عملیاتِ مخرب
        target = await prov.server_type_info(server.provider_server_id)
        if snap.architecture != target["architecture"] or snap.disk_size > target["disk"]:
            raise RuntimeError("این اسنپ‌شات با سرور مقصد سازگار نیست")
        new_pass = await prov.rebuild_from_image(server.provider_server_id, snap.hetzner_image_id)
    except Exception as e:
        await wait.edit_text(
            f"{ERR} بازگردانی ناموفق بود:\n<code>{str(e)[:300]}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="بازگشت", callback_data=f"server:{server_id}",
                                     **{"icon_custom_emoji_id": _BACK})]]),
        )
        return

    server.status = ServerStatus.REBUILDING
    if new_pass:
        extra = dict(server.extra_data or {})
        extra["root_password"] = new_pass
        server.extra_data = extra
    await session.flush()

    await LogService(cb.bot, session).log_snapshot_restored(
        user, snap.source_server_name or "—", server)

    pass_line = (f"\nرمز root جدید: <code>{new_pass}</code>" if new_pass
                 else "\nرمز ورود همان رمزِ داخل اسنپ‌شات است.")
    await wait.edit_text(
        f'<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> '
        f"<b>بازگردانی شروع شد</b>\n\n"
        f"سرور {server.name} با اسنپ‌شات «{snap.source_server_name or '—'}» بازگردانی می‌شود."
        f"{pass_line}\n\n"
        "چند دقیقه تا آماده‌شدن صبر کنید.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="بازگشت", callback_data=f"server:{server_id}",
                                 **{"icon_custom_emoji_id": _BACK})]]),
    )
