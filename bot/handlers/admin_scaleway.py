"""Admin panel — Scaleway account + product import (تک-اکانتی).

فلو: محصولات ← سرویس‌دهنده‌ها ← اسکیل‌وی (Scaleway)
- افزودن اکانت: نام + Secret Key (کنسول ← IAM ← API keys) + Project ID (اختیاری،
  خالی = پروژه‌ی پیش‌فرضِ توکن) → تست زنده → ذخیره → حذف پیام توکن از چت
- جزئیات: تست / ایمپورت / ویرایش نام-توکن-پروژه / لیمیت VM / سود ساعتی و ماهانه /
  نرخ دیسک / نرخ IP / دیسک پیش‌فرض / گروه مقصد / حذف
- ایمپورت: zone → لیست تایپ‌ها با «قیمت خرید کامل» (€ ساعتی و ماهانه) →
  تپ = ServerPlan (غیرفعال تا تعیین سود)

⚠️ قیمت خرید = تایپ + دیسک + IPv4 (دیسک و IP جدا شارژ می‌شوند و API قیمت
نمی‌دهد؛ نرخشان از همین پنل تنظیم می‌شود — `scaleway_settings.py`).
"""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import F, Router
from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import (
    ProductGroup, ProviderAccount, ProviderType, Server, ServerPlan,
    ServerStatus, User,
)
from bot.keyboards.admin import back_to_admin_kb, cancel_admin_kb, group_pick_kb
from bot.providers.scaleway import (
    API_BASE, MIN_DISK_GB, ScalewayProvider, family_of, short_name, zone_label,
)

logger = logging.getLogger(__name__)
router = Router(name="admin_scaleway")


class AdminFilter(Filter):
    async def __call__(self, event: Message | CallbackQuery, user: User | None = None) -> bool:
        if user is None:
            return False
        return user.is_admin or (user.telegram_id in settings.admin_ids)


router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())


class ScalewayFSM(StatesGroup):
    add_name = State()
    add_token = State()
    add_project = State()
    edit_value = State()    # name | token | project
    edit_limit = State()
    edit_margin = State()
    edit_rate = State()     # vol | ip | disk


def _prov(account: ProviderAccount) -> ScalewayProvider:
    return ScalewayProvider(
        api_token=account.api_key or "",
        project_id=(account.extra_config or {}).get("project_id") or "",
    )


async def _sw_account(session: AsyncSession) -> ProviderAccount | None:
    from bot.services.scaleway_settings import get_account
    return await get_account(session)


async def _safe_edit(msg, text: str, reply_markup=None):
    from aiogram.exceptions import TelegramBadRequest
    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ── صفحه اصلی (تک-اکانتی: لیست = جزئیات) ─────────────────────────────────────

async def _render_sw_home(msg, session: AsyncSession):
    from bot.services.scaleway_settings import (
        get_default_disk_gb, get_group_name, get_ip_month, get_margins,
        get_volume_rate,
    )
    account = await _sw_account(session)

    if not account:
        await _safe_edit(
            msg,
            "<b>اسکیل‌وی (Scaleway)</b>\n\n"
            "هنوز اکانتی ثبت نشده. کلید API از کنسول ساخته می‌شود:\n"
            "<i>console.scaleway.com ← IAM ← API keys ← Generate</i>\n"
            "<i>(Secret Key فقط یک بار نمایش داده می‌شود)</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="افزودن اکانت", callback_data="admin:sw_add")],
                [InlineKeyboardButton(text="بازگشت", callback_data="admin:provtypes")],
            ]),
        )
        return

    mh, mm = await get_margins(session)
    group = await get_group_name(session)
    vol_rate = await get_volume_rate(session)
    ip_month = await get_ip_month(session)
    disk_gb = await get_default_disk_gb(session)
    cfg = account.extra_config or {}
    vm_limit = int(cfg.get("vm_limit") or 0)
    project = str(cfg.get("project_id") or "")
    token_masked = f"{(account.api_key or '')[:6]}…{(account.api_key or '')[-4:]}"

    plans_count = (await session.execute(
        select(func.count(ServerPlan.id)).where(
            ServerPlan.provider_type == ProviderType.SCALEWAY)
    )).scalar() or 0
    servers_count = (await session.execute(
        select(func.count(Server.id)).where(
            Server.provider_account_id == account.id,
            Server.status != ServerStatus.DELETED,
        )
    )).scalar() or 0

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="تست اتصال", callback_data="admin:sw_test"),
         InlineKeyboardButton(text="ایمپورت محصولات", callback_data="admin:sw_import")],
        [InlineKeyboardButton(text="ویرایش نام", callback_data="admin:sw_edit:name"),
         InlineKeyboardButton(text="ویرایش توکن", callback_data="admin:sw_edit:token")],
        [InlineKeyboardButton(text=f"Project ID: {project or '—'}",
                              callback_data="admin:sw_edit:project")],
        [InlineKeyboardButton(text=f"سود ساعتی: {mh if mh is not None else '—'}٪",
                              callback_data="admin:swm:h"),
         InlineKeyboardButton(text=f"سود ماهانه: {mm if mm is not None else '—'}٪",
                              callback_data="admin:swm:m")],
        [InlineKeyboardButton(text=f"نرخ دیسک: €{vol_rate:g}/GB/ماه",
                              callback_data="admin:swrate:vol"),
         InlineKeyboardButton(text=f"نرخ IP: €{ip_month:g}/ماه",
                              callback_data="admin:swrate:ip")],
        [InlineKeyboardButton(text=f"دیسک پیش‌فرض: {disk_gb} گیگ",
                              callback_data="admin:swrate:disk")],
        [InlineKeyboardButton(text=f"لیمیت VM: {vm_limit or 'تعیین نشده'}",
                              callback_data="admin:sw_limit")],
        [InlineKeyboardButton(text=f"گروه مقصد: {group}", callback_data="admin:swgrp")],
        [InlineKeyboardButton(
            text=("غیرفعال کردن" if account.is_active else "فعال کردن"),
            callback_data="admin:sw_toggle")],
        [InlineKeyboardButton(text="حذف اکانت", callback_data="admin:sw_del")],
        [InlineKeyboardButton(text="بازگشت", callback_data="admin:provtypes")],
    ])
    await _safe_edit(
        msg,
        f"<b>اسکیل‌وی (Scaleway)</b>\n\n"
        f"اکانت: {account.name} {'✅' if account.is_active else '❌'}\n"
        f"Secret Key: <code>{token_masked}</code>\n"
        f"سرورهای فعال مشتری: {servers_count}"
        f"{f' / {vm_limit}' if vm_limit else ''}\n"
        f"محصولات ایمپورت‌شده: {plans_count}\n\n"
        "قیمت‌ها به یورو است — نرخ یورو خودکار از نوسان (مشترک با هتزنر).\n"
        "قیمت خرید = تایپ + دیسک + IPv4 (دیسک و IP جدا شارژ می‌شوند و در API "
        "قیمت ندارند؛ نرخشان را همین‌جا تنظیم کنید).\n"
        "خرید ماهانه = ساعتی × ۷۴۴ (ماه ۳۱ روزه — محافظه‌کارانه).\n\n"
        "⚠️ کوتای Scaleway per-Organization و پایین است؛ لیمیت VM را تنظیم کنید.\n"
        "⚠️ توقف هزینه فقط با حذف کامل است — خاموشی هزینه‌ی دیسک و IP را قطع نمی‌کند.\n"
        "محصول ایمپورت‌شده تا تعیین سود غیرفعال است.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "admin:scaleway")
async def cb_scaleway(cb: CallbackQuery, session: AsyncSession):
    await cb.answer()
    await _render_sw_home(cb.message, session)


# ── افزودن اکانت ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:sw_add")
async def cb_sw_add(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    if await _sw_account(session):
        await cb.answer("Scaleway فعلاً تک-اکانتی است — اکانت موجود را ویرایش کنید.",
                        show_alert=True)
        return
    await state.set_state(ScalewayFSM.add_name)
    await cb.message.edit_text(
        "<b>افزودن اکانت Scaleway</b>\n\n"
        "نام دلخواه اکانت را وارد کنید:\n<i>مثال: اسکیل‌وی اصلی</i>",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(ScalewayFSM.add_name)
async def sw_add_name(message: Message, state: FSMContext):
    await state.update_data(sw_name=(message.text or "").strip())
    await state.set_state(ScalewayFSM.add_token)
    await message.answer(
        "<b>Secret Key</b> را وارد کنید:\n"
        "<i>console.scaleway.com ← IAM ← API keys ← Generate API key</i>\n"
        "<i>(کلید باید دسترسی InstancesFullAccess داشته باشد)</i>",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )


@router.message(ScalewayFSM.add_token)
async def sw_add_token(message: Message, state: FSMContext):
    token = (message.text or "").strip()
    # توکن نباید در چت بماند
    try:
        await message.delete()
    except Exception:
        pass
    await state.update_data(sw_token=token)
    await state.set_state(ScalewayFSM.add_project)
    await message.answer(
        "<b>Project ID</b> را وارد کنید:\n"
        "<i>console.scaleway.com ← Project settings ← Project ID</i>\n\n"
        "اگر می‌خواهید پروژه‌ی پیش‌فرضِ خودِ توکن استفاده شود، «رد کردن» را بزنید.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="رد کردن", callback_data="admin:sw_add_noproj")],
            [InlineKeyboardButton(text="انصراف", callback_data="admin:scaleway")],
        ]),
    )


async def _sw_finish_add(msg, state: FSMContext, session: AsyncSession,
                         project_id: str) -> None:
    data = await state.get_data()
    await state.clear()
    token = data.get("sw_token") or ""

    wait = await msg.answer("در حال تست اتصال به Scaleway...")
    prov = ScalewayProvider(api_token=token, project_id=project_id)
    try:
        info = await asyncio.wait_for(prov.verify(), timeout=60)
    except Exception as e:
        from html import escape as _esc
        await wait.edit_text(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> اتصال ناموفق:\n'
            f"<code>{_esc(str(e)[:300])}</code>\n\nدوباره از «افزودن اکانت» تلاش کنید.",
            parse_mode="HTML",
            reply_markup=back_to_admin_kb("admin:scaleway"),
        )
        return

    account = ProviderAccount(
        provider_type=ProviderType.SCALEWAY,
        name=data.get("sw_name") or "Scaleway",
        api_key=token,
        api_secret=None,
        api_endpoint=API_BASE,
        extra_config={"project_id": project_id} if project_id else {},
        is_active=True,
        strict_kyc=False,
    )
    session.add(account)
    await session.flush()

    await wait.edit_text(
        f"✅ <b>اکانت Scaleway اضافه شد!</b>\n\n"
        f"نام: {account.name}\n"
        f"پروژه: {info.get('project')}\n"
        f"تایپ‌های قابل فروش در fr-par-1: {info.get('types')}\n"
        f"لوکیشن‌ها: {info.get('zones')}\n\n"
        "حالا «سود ساعتی/ماهانه» را تنظیم و محصولات را ایمپورت کنید.",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb("admin:scaleway"),
    )


@router.message(ScalewayFSM.add_project)
async def sw_add_project(message: Message, state: FSMContext, session: AsyncSession):
    await _sw_finish_add(message, state, session, (message.text or "").strip())


@router.callback_query(ScalewayFSM.add_project, F.data == "admin:sw_add_noproj")
async def cb_sw_add_noproj(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    await cb.answer()
    await _sw_finish_add(cb.message, state, session, "")


# ── تست / ویرایش / لیمیت / سود / نرخ‌ها / toggle ──────────────────────────────

@router.callback_query(F.data == "admin:sw_test")
async def cb_sw_test(cb: CallbackQuery, session: AsyncSession):
    account = await _sw_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    await cb.answer("در حال تست...")
    try:
        info = await asyncio.wait_for(_prov(account).verify(), timeout=60)
        await cb.message.answer(
            f"✅ اتصال برقرار است — {info.get('types')} تایپ در fr-par-1 | "
            f"پروژه: {info.get('project')}",
        )
    except Exception as e:
        from html import escape as _esc
        await cb.message.answer(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"اتصال ناموفق: <code>{_esc(str(e)[:300])}</code>",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("admin:sw_edit:"))
async def cb_sw_edit(cb: CallbackQuery, state: FSMContext):
    field = cb.data.split(":")[2]
    await state.update_data(sw_field=field)
    await state.set_state(ScalewayFSM.edit_value)
    label = {"name": "نام جدید", "token": "Secret Key جدید",
             "project": "Project ID جدید (برای پاک‌کردن، یک خط تیره «-» بفرستید)"}[field]
    await cb.message.edit_text(
        f"<b>ویرایش اکانت Scaleway</b>\n\n{label} را وارد کنید:",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(ScalewayFSM.edit_value)
async def sw_edit_value(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    account = await _sw_account(session)
    if not account:
        await message.answer("اکانت یافت نشد.")
        return
    value = (message.text or "").strip()
    field = data.get("sw_field")
    if field == "name":
        account.name = value
    elif field == "project":
        new_proj = "" if value == "-" else value
        try:
            await asyncio.wait_for(
                ScalewayProvider(account.api_key or "", new_proj).verify(), timeout=60)
        except Exception as e:
            from html import escape as _esc
            await message.answer(
                f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
                f"Project ID نامعتبر: <code>{_esc(str(e)[:200])}</code>",
                parse_mode="HTML", reply_markup=back_to_admin_kb("admin:scaleway"),
            )
            return
        cfg = dict(account.extra_config or {})
        cfg["project_id"] = new_proj
        account.extra_config = cfg
    else:
        try:
            await message.delete()
        except Exception:
            pass
        _proj = (account.extra_config or {}).get("project_id") or ""
        try:
            await asyncio.wait_for(ScalewayProvider(value, _proj).verify(), timeout=60)
        except Exception as e:
            from html import escape as _esc
            await message.answer(
                f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
                f"توکن نامعتبر: <code>{_esc(str(e)[:200])}</code>",
                parse_mode="HTML", reply_markup=back_to_admin_kb("admin:scaleway"),
            )
            return
        account.api_key = value
    await session.flush()
    await message.answer("ذخیره شد.", reply_markup=back_to_admin_kb("admin:scaleway"))


@router.callback_query(F.data == "admin:sw_limit")
async def cb_sw_limit(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ScalewayFSM.edit_limit)
    await cb.message.edit_text(
        "<b>لیمیت تعداد VM اکانت Scaleway</b>\n\n"
        "کوتای Scaleway per-Organization است و API آن را نمی‌دهد؛ این عدد کنترل "
        "داخلی ربات است.\nبا رسیدن سرورهای فعال ربات به این عدد، خرید جدید مسدود "
        "می‌شود.\n\nعدد لیمیت (0 = بدون کنترل):",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(ScalewayFSM.edit_limit, F.text.regexp(r"^\d+$"))
async def sw_limit_value(message: Message, state: FSMContext, session: AsyncSession):
    await state.clear()
    account = await _sw_account(session)
    if not account:
        await message.answer("اکانت یافت نشد.")
        return
    cfg = dict(account.extra_config or {})
    cfg["vm_limit"] = int(message.text)
    account.extra_config = cfg
    await session.flush()
    await message.answer(
        f"لیمیت VM روی {int(message.text) or 'بدون کنترل'} ثبت شد.",
        reply_markup=back_to_admin_kb("admin:scaleway"),
    )


@router.callback_query(F.data.startswith("admin:swm:"))
async def cb_sw_margin(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":")[2]
    await state.update_data(sw_margin_kind=kind)
    await state.set_state(ScalewayFSM.edit_margin)
    label = "ساعتی" if kind == "h" else "ماهانه"
    await cb.message.edit_text(
        f"<b>درصد سود {label} (کل Scaleway)</b>\n\n"
        "قیمت فروش = قیمت خرید (یورو) × (۱ + سود٪)\n"
        "این سود روی <b>همه‌ی محصولات Scaleway</b> اعمال می‌شود و در سینک "
        "دوره‌ای هم دنبال قیمت provider می‌ماند. با ثبت سود، محصولات "
        "ایمپورت‌شده فعال می‌شوند.\n\n"
        f"درصد سود {label} را وارد کنید (مثال: 35):",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(ScalewayFSM.edit_margin, F.text.regexp(r"^\d+(\.\d+)?$"))
async def sw_margin_value(message: Message, state: FSMContext, session: AsyncSession):
    from bot.services.scaleway_settings import apply_margins_to_catalog, set_margin
    data = await state.get_data()
    await state.clear()
    await set_margin(session, hourly=(data.get("sw_margin_kind") == "h"),
                     value=float(message.text))
    await session.flush()
    updated = await apply_margins_to_catalog(session)
    await message.answer(
        f"سود ثبت شد ({message.text}٪) — قیمت فروش {updated} محصول Scaleway "
        "به‌روز و فعال شد.",
        reply_markup=back_to_admin_kb("admin:scaleway"),
    )


_RATE_LABELS = {
    "vol": ("نرخ دیسک بلاک (€ به‌ازای هر گیگ در ماه)",
            "قیمت رسمی Block Storage 5K حدود <b>0.095</b> است (نسخه‌ی 15K حدود "
            "0.129). این نرخ در قیمت خرید همه‌ی پلن‌ها ضربِ حجم دیسک می‌شود."),
    "ip": ("نرخ IPv4 قابل انعطاف (€ در ماه)",
           "قیمت رسمی <b>3.65</b> است (0.005 یورو در ساعت × ۷۳۰). هر سرور یک "
           "IPv4 رزروشده می‌گیرد، پس این مبلغ همیشه در قیمت خرید هست."),
    "disk": (f"حجم دیسک پیش‌فرض محصولات جدید (گیگ، حداقل {MIN_DISK_GB})",
             "دیسک در قیمت تایپ نیست و جدا خریداری می‌شود. تغییر این عدد فقط "
             "روی محصولاتی که <b>بعداً</b> ایمپورت می‌شوند اثر دارد."),
}


@router.callback_query(F.data.startswith("admin:swrate:"))
async def cb_sw_rate(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":")[2]
    if kind not in _RATE_LABELS:
        await cb.answer("نامعتبر.", show_alert=True)
        return
    title, hint = _RATE_LABELS[kind]
    await state.update_data(sw_rate_kind=kind)
    await state.set_state(ScalewayFSM.edit_rate)
    await cb.message.edit_text(
        f"<b>{title}</b>\n\n{hint}\n\nمقدار جدید را وارد کنید:",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(ScalewayFSM.edit_rate, F.text.regexp(r"^\d+(\.\d+)?$"))
async def sw_rate_value(message: Message, state: FSMContext, session: AsyncSession):
    from bot.services.scaleway_settings import (
        apply_margins_to_catalog, recompute_catalog_costs, set_default_disk_gb,
        set_ip_month, set_volume_rate,
    )
    data = await state.get_data()
    await state.clear()
    kind = data.get("sw_rate_kind")
    value = float(message.text)
    if kind == "vol":
        await set_volume_rate(session, value)
    elif kind == "ip":
        await set_ip_month(session, value)
    else:
        await set_default_disk_gb(session, int(value))
    await session.flush()
    # نرخ دیسک/IP روی قیمت خریدِ همه‌ی پلن‌های موجود اثر دارد؛ «دیسک پیش‌فرض»
    # فقط روی ایمپورت‌های بعدی (حجم دیسکِ پلن‌های موجود دست نمی‌خورد).
    note = ""
    if kind in ("vol", "ip"):
        n = await recompute_catalog_costs(session)
        await apply_margins_to_catalog(session)
        note = f" — قیمت خرید/فروش {n} محصول بازمحاسبه شد"
    await message.answer(f"ثبت شد ({message.text}){note}.",
                         reply_markup=back_to_admin_kb("admin:scaleway"))


@router.callback_query(F.data == "admin:sw_toggle")
async def cb_sw_toggle(cb: CallbackQuery, session: AsyncSession):
    account = await _sw_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    account.is_active = not account.is_active
    await session.flush()
    await cb.answer(f"{'فعال' if account.is_active else 'غیرفعال'} شد.")
    await _render_sw_home(cb.message, session)


# ── گروه مقصد ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:swgrp")
async def cb_sw_group_pick(cb: CallbackQuery, session: AsyncSession):
    groups = (await session.execute(
        select(ProductGroup).order_by(ProductGroup.name)
    )).scalars().all()
    await cb.answer()
    await cb.message.edit_text(
        "<b>گروه مقصد محصولات Scaleway</b>\n\n"
        "همه‌ی محصولات Scaleway در این گروه قرار می‌گیرند (کاتالوگِ موجود هم "
        "منتقل می‌شود):\n<i>(گروه جدید را از «گروه محصولات» بسازید)</i>",
        parse_mode="HTML",
        reply_markup=group_pick_kb(groups, "admin:swgrpset",
                                   allow_new=False, cancel_cb="admin:scaleway"),
    )


@router.callback_query(F.data.startswith("admin:swgrpset:"))
async def cb_sw_group_set(cb: CallbackQuery, session: AsyncSession):
    from bot.services.scaleway_settings import set_group_name
    group = await session.get(ProductGroup, int(cb.data.split(":")[2]))
    if not group:
        await cb.answer("گروه یافت نشد.", show_alert=True)
        return
    await set_group_name(session, group.name)
    sw_plans = (await session.execute(
        select(ServerPlan).where(ServerPlan.provider_type == ProviderType.SCALEWAY)
    )).scalars().all()
    for p in sw_plans:
        p.category = group.name
    await session.flush()
    await cb.answer(f"گروه مقصد: {group.name}")
    await _render_sw_home(cb.message, session)


# ── حذف اکانت (قواعد ۵.۸) ────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:sw_del")
async def cb_sw_del(cb: CallbackQuery, session: AsyncSession):
    account = await _sw_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text(
        f"حذف اکانت <b>{account.name}</b>؟\n"
        "<i>چون تک-اکانتی است، همه‌ی محصولات Scaleway هم حذف می‌شوند.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="بله، حذف شود", callback_data="admin:sw_del_do"),
            InlineKeyboardButton(text="انصراف", callback_data="admin:scaleway"),
        ]]),
    )


@router.callback_query(F.data == "admin:sw_del_do")
async def cb_sw_del_do(cb: CallbackQuery, session: AsyncSession):
    account = await _sw_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    servers_count = (await session.execute(
        select(func.count(Server.id)).where(
            Server.provider_account_id == account.id,
            Server.status != ServerStatus.DELETED,
        )
    )).scalar() or 0
    if servers_count:
        await cb.answer(
            f"این اکانت {servers_count} سرور فعال مشتری دارد — اول آن‌ها را حذف کنید.",
            show_alert=True,
        )
        return
    await cb.answer("در حال حذف...")
    try:
        from sqlalchemy import text as _text, update as _update
        await session.execute(_text("SET LOCAL statement_timeout = '8s'"))
        plans = (await session.execute(
            select(ServerPlan).where(ServerPlan.provider_type == ProviderType.SCALEWAY)
        )).scalars().all()
        for p in plans:
            await session.delete(p)
        await session.execute(
            _update(Server).where(Server.provider_account_id == account.id)
            .values(provider_account_id=None)
        )
        # اول وابسته‌ها + flush جدا، بعد اکانت (relationship تعریف نشده)
        await session.flush()
        await session.delete(account)
        await session.flush()
    except Exception as e:
        logger.exception("scaleway account delete failed")
        await session.rollback()
        from html import escape as _esc
        await cb.message.answer(
            "❌ حذف اکانت ناموفق بود:\n<code>" + _esc(str(e)[:300]) + "</code>"
        )
        return
    await _render_sw_home(cb.message, session)


# ── ایمپورت محصولات ──────────────────────────────────────────────────────────

# کش کوتاه‌مدت تایپ‌ها/لوکیشن‌ها/موجودی — هر کلیک API نخورد
_plans_cache: dict = {}
_locs_cache: dict = {}
_fields_cache: dict = {}
_avail_cache: dict = {}


async def _zone_plans(account: ProviderAccount, zone: str):
    key = (account.id, zone)
    cached = _plans_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < 300:
        return cached[1]
    plans = await asyncio.wait_for(_prov(account).list_plans(location=zone), timeout=45)
    _plans_cache[key] = (now, plans)
    return plans


async def _zone_fields(account: ProviderAccount, zone: str) -> dict:
    key = (account.id, zone)
    cached = _fields_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < 300:
        return cached[1]
    m = await asyncio.wait_for(_prov(account).raw_type_fields(zone), timeout=45)
    _fields_cache[key] = (now, m)
    return m


async def _zone_avail(account: ProviderAccount, zone: str) -> dict:
    key = (account.id, zone)
    cached = _avail_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < 300:
        return cached[1]
    try:
        m = await asyncio.wait_for(_prov(account).availability(zone), timeout=45)
    except Exception as e:
        logger.info("scaleway availability(%s) failed: %s", zone, e)
        m = {}
    _avail_cache[key] = (now, m)
    return m


async def _locations(account: ProviderAccount) -> list[dict]:
    cached = _locs_cache.get(account.id)
    now = time.monotonic()
    if cached and now - cached[0] < 600:
        return cached[1]
    # ده zone × صفحه‌بندیِ products/servers — کندترین فراخوانیِ پنل؛ ۱۰ دقیقه کش
    locs = await asyncio.wait_for(_prov(account).list_locations(), timeout=240)
    _locs_cache[account.id] = (now, locs)
    return locs


async def _imported_map(session: AsyncSession, zone: str) -> dict:
    rows = (await session.execute(
        select(ServerPlan).where(
            ServerPlan.provider_type == ProviderType.SCALEWAY,
            ServerPlan.location == zone,
        )
    )).scalars().all()
    return {p.provider_plan_id: p for p in rows}


@router.callback_query(F.data == "admin:sw_import")
async def cb_sw_import(cb: CallbackQuery, session: AsyncSession):
    from bot.services.scaleway_settings import get_default_disk_gb, get_group_name
    account = await _sw_account(session)
    if not account:
        await cb.answer("اول اکانت را اضافه کنید.", show_alert=True)
        return
    await cb.answer("در حال دریافت لوکیشن‌ها...")
    try:
        locs = await _locations(account)
    except Exception as e:
        from html import escape as _esc
        await cb.message.answer(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"خطا در دریافت لوکیشن‌ها: <code>{_esc(str(e)[:200])}</code>",
            parse_mode="HTML",
        )
        return
    group_name = await get_group_name(session)
    disk_gb = await get_default_disk_gb(session)
    rows = [[InlineKeyboardButton(
        text=f"{l['display_name']} · {l.get('count', 0)} تایپ",
        callback_data=f"admin:swzone:{l['slug']}",
    )] for l in locs if l.get("count")]
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data="admin:scaleway")])
    await _safe_edit(
        cb.message,
        "<b>ایمپورت محصولات Scaleway</b>\n\n"
        f"محصولات به گروه «{group_name}» می‌روند و با دیسک <b>{disk_gb} گیگ</b> "
        "ساخته می‌شوند.\nقیمت‌ها € و شاملِ تایپ + دیسک + IPv4 اند.\n"
        "لوکیشن را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _plan_status_mark(plan: ServerPlan | None) -> str:
    if plan is None:
        return "⬜"                              # ایمپورت‌نشده
    if (plan.extra_data or {}).get("unavailable"):
        return "⛔"                              # ناموجود/حذف‌شده از کاتالوگ
    return "✅" if plan.is_active else "☑️"       # فعال / ایمپورت‌شده‌ی بی‌قیمت


_AVAIL_MARK = {"available": "", "scarce": " ⚠️", "shortage": " ⛔"}


async def _render_sw_families(msg, session: AsyncSession, account: ProviderAccount,
                              zone: str):
    """مرحله‌ی «خانواده» بین لوکیشن و تایپ‌ها.

    ⚠️ هر zone حدود صد تایپ دارد؛ یک کیبورد تخت از سقف ردیف‌های تلگرام رد
    می‌شود و پیام اصلاً ارسال نمی‌شود (الگوی جیکور: region → family → flavor)."""
    plans = await _zone_plans(account, zone)
    imported = await _imported_map(session, zone)

    fams: dict[str, list] = {}
    for p in plans:
        fams.setdefault(family_of(p.provider_plan_id), []).append(p)
    # خانواده‌هایی که فقط پلنِ ایمپورت‌شده‌ی قدیمی دارند هم دکمه بگیرند تا
    # ادمین بتواند حذفشان کند
    for pid in imported:
        fams.setdefault(family_of(pid), [])

    rows = []
    for fam in sorted(fams):
        total = len(fams[fam])
        n_imp = sum(1 for pid in imported if family_of(pid) == fam)
        rows.append([InlineKeyboardButton(
            text=f"{fam} ({n_imp}/{total})",
            callback_data=f"admin:swfam:{zone}:{fam}",
        )])
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data="admin:sw_import")])
    await _safe_edit(
        msg,
        f"<b>تایپ‌های Scaleway — {zone_label(zone)}</b>\n\n"
        "خانواده را انتخاب کنید (ایمپورت‌شده/کل):\n"
        "<i>BASIC/PLAY/DEV = vCPU اشتراکی · POP2/STANDARD/COMPUTE/MEMORY = "
        "vCPU اختصاصی · پسوند A یا 2-A = معماری ARM</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _render_sw_plans(msg, session: AsyncSession, account: ProviderAccount,
                           zone: str, fam: str):
    from bot.services.scaleway_settings import (
        full_costs, get_default_disk_gb, get_ip_month, get_volume_rate,
    )
    plans = [p for p in await _zone_plans(account, zone)
             if family_of(p.provider_plan_id) == fam]
    fields = await _zone_fields(account, zone)
    avail = await _zone_avail(account, zone)
    imported = await _imported_map(session, zone)
    disk_gb = await get_default_disk_gb(session)
    vol_rate = await get_volume_rate(session)
    ip_month = await get_ip_month(session)

    rows = []
    for p in plans:
        db = imported.get(p.provider_plan_id)
        mark = _plan_status_mark(db)
        # قیمت روی دکمه = خریدِ کاملِ همان چیزی که فروخته می‌شود (دیسکِ پلن اگر
        # ایمپورت شده، وگرنه دیسک پیش‌فرض)
        _disk = int(db.disk) if db and db.disk else max(
            disk_gb, int((fields.get(p.provider_plan_id) or {}).get("local_min_gb") or 0))
        _, cm = full_costs(p.price_hourly or 0, _disk, vol_rate, ip_month)
        ram_g = p.ram // 1024 if p.ram >= 1024 else p.ram
        code = short_name(p.provider_plan_id, p.cpu, p.ram)
        av = _AVAIL_MARK.get(avail.get(p.provider_plan_id, "available"), "")
        rows.append([
            InlineKeyboardButton(
                text=f"{mark} {code}{av} · {p.cpu}c/{ram_g}G/{_disk}G · €{cm:g}/ماه",
                callback_data=f"admin:swpick:{zone}:{p.provider_plan_id}",
            ),
            InlineKeyboardButton(
                text="ℹ️",
                callback_data=f"admin:swinfo:{zone}:{p.provider_plan_id}",
            ),
        ])
    # ایمپورت‌شده‌هایی که دیگر در کاتالوگ provider نیستند — قابل حذف بمانند
    shown = {p.provider_plan_id for p in plans}
    for pid in sorted(imported):
        if pid in shown or family_of(pid) != fam:
            continue
        rows.append([InlineKeyboardButton(
            text=f"⛔ {imported[pid].display_name or pid} · حذف‌شده از کاتالوگ — حذف",
            callback_data=f"admin:swpick:{zone}:{pid}",
        )])
    rows.append([
        InlineKeyboardButton(text="ایمپورت همه",
                             callback_data=f"admin:swallon:{zone}:{fam}"),
        InlineKeyboardButton(text="حذف همه",
                             callback_data=f"admin:swalloff:{zone}:{fam}"),
    ])
    rows.append([InlineKeyboardButton(text="بازگشت",
                                      callback_data=f"admin:swzone:{zone}")])
    _arch = (fields.get(plans[0].provider_plan_id, {}).get("arch")
             if plans else "x86_64")
    await _safe_edit(
        msg,
        f"<b>{fam} — {zone_label(zone)}</b>\n\n"
        "<b>راهنمای وضعیت:</b>\n"
        "✅ فعال (در فروش)\n"
        "☑️ ایمپورت‌شده بی‌قیمت\n"
        "⛔ ناموجود / حذف‌شده از کاتالوگ\n"
        "⬜ ایمپورت‌نشده\n"
        "⚠️ کنار کد = موجودی کم (scarce) · ⛔ کنار کد = تمام‌شده (shortage)\n\n"
        "عدد = قیمت خرید ماهانه (€، شامل دیسک و IPv4) · تپ = افزودن/حذف · ℹ️ = جزئیات\n"
        f"<i>معماری این خانواده: {_arch} — لیست سیستم‌عامل خودکار هم‌معماری "
        "فیلتر می‌شود.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("admin:swzone:"))
async def cb_sw_zone(cb: CallbackQuery, session: AsyncSession):
    account = await _sw_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    zone = cb.data.split(":")[2]
    await cb.answer("در حال دریافت تایپ‌ها...")
    try:
        await _render_sw_families(cb.message, session, account, zone)
    except Exception as e:
        from html import escape as _esc
        await cb.message.answer(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"خطا در دریافت تایپ‌ها: <code>{_esc(str(e)[:200])}</code>",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("admin:swfam:"))
async def cb_sw_family(cb: CallbackQuery, session: AsyncSession):
    account = await _sw_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    _, _, zone, fam = cb.data.split(":", 3)
    await cb.answer()
    try:
        await _render_sw_plans(cb.message, session, account, zone, fam)
    except Exception as e:
        from html import escape as _esc
        await cb.message.answer(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"خطا در دریافت تایپ‌ها: <code>{_esc(str(e)[:200])}</code>",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("admin:swinfo:"))
async def cb_sw_info(cb: CallbackQuery, session: AsyncSession):
    from bot.services.scaleway_settings import (
        full_costs, get_default_disk_gb, get_ip_month, get_volume_rate,
    )
    account = await _sw_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    parts = cb.data.split(":")
    zone, pid = parts[2], parts[3]
    plans = await _zone_plans(account, zone)
    info = next((p for p in plans if p.provider_plan_id == pid), None)
    if not info:
        await cb.answer("تایپ یافت نشد.", show_alert=True)
        return
    raw = (await _zone_fields(account, zone)).get(pid) or {}
    avail = (await _zone_avail(account, zone)).get(pid, "available")
    imported = (await _imported_map(session, zone)).get(pid)
    disk_gb = int(imported.disk) if imported and imported.disk \
        else max(await get_default_disk_gb(session),
                 int(raw.get("local_min_gb") or 0))
    ch, cm = full_costs(info.price_hourly or 0, disk_gb,
                        await get_volume_rate(session), await get_ip_month(session))
    ram_g = info.ram // 1024 if info.ram >= 1024 else info.ram
    _mb = raw.get("bandwidth_mbit") or 0
    _av_fa = {"available": "موجود", "scarce": "موجودی کم",
              "shortage": "تمام‌شده"}.get(avail, avail)
    await cb.answer(
        (f"{pid}\n"
         f"{info.cpu} هسته | {ram_g} گیگ رم | {disk_gb} گیگ بلاک | {raw.get('arch', '?')}\n"
         + (f"کانال: {_mb} Mbit — ترافیک نامحدود\n" if _mb else "")
         + f"موجودی: {_av_fa}\n"
         + "\nقیمت خرید (یورو، با دیسک و IP):\n"
         f"ساعتی: {round(ch, 5):g}\n"
         f"ماهانه: {round(cm, 2):g}")[:195],
        show_alert=True,
    )


async def _import_one(session: AsyncSession, account: ProviderAccount,
                      zone: str, info, group_name: str, fields: dict) -> ServerPlan:
    from bot.services.scaleway_settings import (
        full_costs, get_default_disk_gb, get_ip_month, get_volume_rate,
    )
    raw = fields.get(info.provider_plan_id) or {}
    disk_gb = await get_default_disk_gb(session)
    # بعضی تایپ‌های قدیمی حداقلِ حجمِ اجباری برای دیسکِ اولیه دارند
    # (`volumes_constraint.min_size`) — کمتر از آن، ساخت با invalid_arguments رد
    # می‌شود. پس کفِ حجم را همان‌جا بالا می‌بریم تا قیمت هم درست دربیاید.
    disk_gb = max(disk_gb, int(raw.get("local_min_gb") or 0))
    vol_rate = await get_volume_rate(session)
    ip_month = await get_ip_month(session)
    ch, cm = full_costs(info.price_hourly or 0, disk_gb, vol_rate, ip_month)
    plan = ServerPlan(
        provider_type=ProviderType.SCALEWAY,
        provider_account_id=account.id,
        name=f"scw-{info.provider_plan_id}-{zone}",
        display_name=short_name(info.provider_plan_id, info.cpu, info.ram),
        ram=info.ram, cpu=info.cpu, disk=disk_gb,
        bandwidth=0,                              # ترافیک نامحدود (کانال Mbit جدا)
        price_hourly=None, price_monthly=None,    # فروش با سود سراسری
        location=zone,
        datacenter=zone,
        is_active=False,
        category=group_name,
        provider_plan_id=info.provider_plan_id,
        extra_data={
            "currency": "eur",
            # خام: فقط قیمتِ خودِ instance (مبنای بازمحاسبه با تغییر نرخ دیسک/IP)
            "type_cost_hourly": info.price_hourly,
            "cost_hourly": ch,
            "cost_monthly": cm,
            "disk_cost_monthly": round(float(disk_gb) * vol_rate, 4),
            "ip_cost_monthly": round(float(ip_month), 4),
            "region_name": zone_label(zone),
            "bandwidth_mbit": raw.get("bandwidth_mbit"),
            "arch": raw.get("arch") or "x86_64",
            "commercial_type": info.provider_plan_id,
        },
    )
    session.add(plan)
    return plan


async def _remove_plan(session: AsyncSession, plan: ServerPlan) -> tuple[bool, str]:
    servers = (await session.execute(
        select(Server).where(Server.status != ServerStatus.DELETED)
    )).scalars().all()
    in_use = any((s.extra_data or {}).get("plan_id") == plan.id for s in servers)
    if in_use:
        plan.is_active = False
        return False, "غیرفعال شد (سرور فعال دارد)"
    await session.delete(plan)
    return True, "حذف شد"


@router.callback_query(F.data.startswith("admin:swallon:"))
async def cb_sw_all_on(cb: CallbackQuery, session: AsyncSession):
    from bot.services.scaleway_settings import apply_margins_to_catalog, get_group_name
    account = await _sw_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    _, _, zone, fam = cb.data.split(":", 3)
    plans = [p for p in await _zone_plans(account, zone)
             if family_of(p.provider_plan_id) == fam]
    fields = await _zone_fields(account, zone)
    imported = await _imported_map(session, zone)
    group_name = await get_group_name(session)
    added = 0
    for info in plans:
        if info.provider_plan_id in imported:
            continue
        await _import_one(session, account, zone, info, group_name, fields)
        added += 1
    await session.flush()
    if added:
        await apply_margins_to_catalog(session)
    await cb.answer(f"{added} تایپ اضافه شد." if added else "همه از قبل ایمپورت شده‌اند.")
    await _render_sw_plans(cb.message, session, account, zone, fam)


@router.callback_query(F.data.startswith("admin:swalloff:"))
async def cb_sw_all_off(cb: CallbackQuery, session: AsyncSession):
    account = await _sw_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    _, _, zone, fam = cb.data.split(":", 3)
    imported = await _imported_map(session, zone)
    removed = kept = 0
    for pid, plan in imported.items():
        if family_of(pid) != fam:
            continue
        deleted, _ = await _remove_plan(session, plan)
        if deleted:
            removed += 1
        else:
            kept += 1
    await session.flush()
    note = f"{removed} حذف شد" + (f"، {kept} فقط غیرفعال شد (سرور فعال دارد)" if kept else "")
    await cb.answer(note if (removed or kept) else "چیزی برای حذف نیست.",
                    show_alert=bool(kept))
    await _render_sw_plans(cb.message, session, account, zone, fam)


@router.callback_query(F.data.startswith("admin:swpick:"))
async def cb_sw_pick(cb: CallbackQuery, session: AsyncSession):
    from bot.services.scaleway_settings import (
        apply_margins_to_catalog, get_group_name, get_margins,
    )
    account = await _sw_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    parts = cb.data.split(":")
    zone, pid = parts[2], parts[3]

    existing = (await session.execute(
        select(ServerPlan).where(
            ServerPlan.provider_type == ProviderType.SCALEWAY,
            ServerPlan.provider_plan_id == pid,
            ServerPlan.location == zone,
        )
    )).scalar_one_or_none()

    if existing:
        if not existing.is_active:
            # ایمپورت‌شده ولی غیرفعال → کلیک دوباره = فعال‌سازی (الگوی تایم‌وب/روت)
            mh, mm = await get_margins(session)
            if mh is not None or mm is not None:
                await apply_margins_to_catalog(session)
            if not existing.is_active and (existing.price_hourly or existing.price_monthly):
                existing.is_active = True
            await session.flush()
            if existing.is_active:
                await cb.answer(f"✅ {pid}: دوباره فعال شد.")
            else:
                await cb.answer(f"{pid}: اول سود Scaleway را تنظیم کنید تا قیمت بگیرد.",
                                show_alert=True)
        else:
            deleted, note = await _remove_plan(session, existing)
            await session.flush()
            await cb.answer(f"{pid}: {note}", show_alert=not deleted)
    else:
        plans = await _zone_plans(account, zone)
        info = next((p for p in plans if p.provider_plan_id == pid), None)
        if not info:
            await cb.answer("تایپ در این لوکیشن موجود نیست.", show_alert=True)
            return
        group_name = await get_group_name(session)
        fields = await _zone_fields(account, zone)
        await _import_one(session, account, zone, info, group_name, fields)
        await session.flush()
        mh, mm = await get_margins(session)
        if mh is not None or mm is not None:
            await apply_margins_to_catalog(session)
            await cb.answer(f"✅ تایپ {pid} اضافه و قیمت‌گذاری/فعال شد.")
        else:
            await cb.answer(f"✅ تایپ {pid} اضافه شد — سود Scaleway را تنظیم کنید.")
    await _render_sw_plans(cb.message, session, account, zone, family_of(pid))
