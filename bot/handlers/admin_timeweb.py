"""Admin panel — Timeweb Cloud account + product import (تک-اکانتی).

فلو: محصولات ← سرویس‌دهنده‌ها ← تایم‌وب
- افزودن اکانت: نام + JWT Token (پنل تایم‌وب ← «API и Terraform») → تست زنده
  (وضعیت اکانت + موجودی + تعرفه‌ها) → ذخیره → حذف پیام توکن از چت
- جزئیات: تست / ایمپورت / ویرایش نام-توکن / لیمیت VM دستی / سود ساعتی و ماهانه /
  نرخ روبل (خودکار از Navasan؛ اینجا قابل‌تنظیم دستی) / گروه مقصد / حذف
- ایمپورت: لوکیشن → لیست تعرفه‌ها با «قیمت خرید» (₽ ماهانه) → تپ = ServerPlan
  (غیرفعال تا تعیین سود). قیمت خرید ساعتی = ماهانه ÷ ۷۲۰.

⚠️ عملیاتی: «تأیید حذف سرویس‌ها» (تلگرام/SMS) در پنل تایم‌وب باید خاموش باشد
وگرنه حذف سرور از API کار نمی‌کند.
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
from bot.providers.timeweb import API_BASE, LOC_LABELS, TimewebProvider

logger = logging.getLogger(__name__)
router = Router(name="admin_timeweb")


class AdminFilter(Filter):
    async def __call__(self, event: Message | CallbackQuery, user: User | None = None) -> bool:
        if user is None:
            return False
        return user.is_admin or (user.telegram_id in settings.admin_ids)


router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())


class TimewebFSM(StatesGroup):
    add_name = State()
    add_token = State()
    edit_value = State()    # name | token
    edit_limit = State()
    edit_margin = State()


def _prov(account: ProviderAccount) -> TimewebProvider:
    return TimewebProvider(api_token=account.api_key or "")


async def _tw_account(session: AsyncSession) -> ProviderAccount | None:
    from bot.services.timeweb_settings import get_account
    return await get_account(session)


# ── صفحه اصلی (تک-اکانتی: لیست = جزئیات) ─────────────────────────────────────

async def _render_tw_home(msg, session: AsyncSession):
    from bot.services.timeweb_settings import get_group_name, get_margins
    account = await _tw_account(session)

    if not account:
        await msg.edit_text(
            "<b>تایم‌وب (Timeweb Cloud)</b>\n\n"
            "هنوز اکانتی ثبت نشده. توکن JWT از پنل تایم‌وب ساخته می‌شود:\n"
            "<i>timeweb.cloud ← بخش «API и Terraform» ← ساخت توکن</i>\n\n"
            "⚠️ در پنل تایم‌وب «تأیید حذف سرویس‌ها» را خاموش کنید — وگرنه حذف "
            "سرور از API کار نمی‌کند.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="افزودن اکانت", callback_data="admin:tw_add")],
                [InlineKeyboardButton(text="بازگشت", callback_data="admin:provtypes")],
            ]),
        )
        return

    mh, mm = await get_margins(session)
    group = await get_group_name(session)
    cfg = account.extra_config or {}
    vm_limit = int(cfg.get("vm_limit") or 0)
    token_masked = f"{(account.api_key or '')[:6]}…{(account.api_key or '')[-4:]}"

    plans_count = (await session.execute(
        select(func.count(ServerPlan.id)).where(
            ServerPlan.provider_type == ProviderType.TIMEWEB)
    )).scalar() or 0
    servers_count = (await session.execute(
        select(func.count(Server.id)).where(
            Server.provider_account_id == account.id,
            Server.status != ServerStatus.DELETED,
        )
    )).scalar() or 0

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="تست اتصال", callback_data="admin:tw_test"),
         InlineKeyboardButton(text="ایمپورت محصولات", callback_data="admin:tw_import")],
        [InlineKeyboardButton(text="ویرایش نام", callback_data="admin:tw_edit:name"),
         InlineKeyboardButton(text="ویرایش توکن", callback_data="admin:tw_edit:token")],
        [InlineKeyboardButton(text=f"سود ساعتی: {mh if mh is not None else '—'}٪",
                              callback_data="admin:twm:h"),
         InlineKeyboardButton(text=f"سود ماهانه: {mm if mm is not None else '—'}٪",
                              callback_data="admin:twm:m")],
        [InlineKeyboardButton(text=f"لیمیت VM: {vm_limit or 'تعیین نشده'}",
                              callback_data="admin:tw_limit")],
        [InlineKeyboardButton(text=f"گروه مقصد: {group}", callback_data="admin:twgrp")],
        [InlineKeyboardButton(
            text=("غیرفعال کردن" if account.is_active else "فعال کردن"),
            callback_data="admin:tw_toggle")],
        [InlineKeyboardButton(text="حذف اکانت", callback_data="admin:tw_del")],
        [InlineKeyboardButton(text="بازگشت", callback_data="admin:provtypes")],
    ])
    await msg.edit_text(
        f"<b>تایم‌وب (Timeweb Cloud)</b>\n\n"
        f"اکانت: {account.name} {'✅' if account.is_active else '❌'}\n"
        f"Token: <code>{token_masked}</code>\n"
        f"سرورهای فعال مشتری: {servers_count}"
        f"{f' / {vm_limit}' if vm_limit else ''}\n"
        f"محصولات ایمپورت‌شده: {plans_count}\n\n"
        "قیمت‌ها به روبل است — نرخ روبل هر ۸ ساعت خودکار از نوسان آپدیت می‌شود "
        "(نمایش: بخش مالی ← نرخ ارز). فروش ساعتی و ماهانه — ساعتی = ماهانه ÷ ۷۲۰.\n"
        "محصول ایمپورت‌شده تا تعیین سود غیرفعال است.\n"
        "⚠️ «تأیید حذف سرویس‌ها» در پنل تایم‌وب باید خاموش باشد.",
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.callback_query(F.data == "admin:timeweb")
async def cb_timeweb(cb: CallbackQuery, session: AsyncSession):
    await cb.answer()
    await _render_tw_home(cb.message, session)


# ── افزودن اکانت ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:tw_add")
async def cb_tw_add(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    if await _tw_account(session):
        await cb.answer("تایم‌وب فعلاً تک-اکانتی است — اکانت موجود را ویرایش کنید.",
                        show_alert=True)
        return
    await state.set_state(TimewebFSM.add_name)
    await cb.message.edit_text(
        "<b>افزودن اکانت تایم‌وب</b>\n\n"
        "نام دلخواه اکانت را وارد کنید:\n<i>مثال: تایم‌وب اصلی</i>",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(TimewebFSM.add_name)
async def tw_add_name(message: Message, state: FSMContext):
    await state.update_data(tw_name=(message.text or "").strip())
    await state.set_state(TimewebFSM.add_token)
    await message.answer(
        "توکن JWT را وارد کنید:\n"
        "<i>پنل تایم‌وب ← «API и Terraform» ← ساخت توکن</i>",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )


@router.message(TimewebFSM.add_token)
async def tw_add_token(message: Message, state: FSMContext, session: AsyncSession):
    token = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()
    # توکن نباید در چت بماند
    try:
        await message.delete()
    except Exception:
        pass

    wait = await message.answer("در حال تست اتصال به Timeweb...")
    prov = TimewebProvider(api_token=token)
    try:
        info = await asyncio.wait_for(prov.verify(), timeout=40)
    except Exception as e:
        from html import escape as _esc
        await wait.edit_text(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> اتصال ناموفق:\n'
            f"<code>{_esc(str(e)[:300])}</code>\n\nدوباره از «افزودن اکانت» تلاش کنید.",
            parse_mode="HTML",
            reply_markup=back_to_admin_kb("admin:timeweb"),
        )
        return

    account = ProviderAccount(
        provider_type=ProviderType.TIMEWEB,
        name=data.get("tw_name") or "Timeweb",
        api_key=token,
        api_secret=None,
        api_endpoint=API_BASE,
        is_active=True,
        strict_kyc=False,
    )
    session.add(account)
    await session.flush()

    await wait.edit_text(
        f"✅ <b>اکانت تایم‌وب اضافه شد!</b>\n\n"
        f"نام: {account.name}\n"
        f"موجودی اکانت: {info.get('balance'):,.0f} {info.get('currency')}\n"
        f"تعرفه‌ها: {info.get('presets')} در {info.get('locations')} لوکیشن\n\n"
        "حالا «سود ساعتی/ماهانه» را تنظیم و محصولات را ایمپورت کنید.\n"
        "⚠️ یادآوری: «تأیید حذف سرویس‌ها» در پنل تایم‌وب خاموش باشد.",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb("admin:timeweb"),
    )


# ── تست / ویرایش / لیمیت / نرخ روبل / toggle ─────────────────────────────────

@router.callback_query(F.data == "admin:tw_test")
async def cb_tw_test(cb: CallbackQuery, session: AsyncSession):
    account = await _tw_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    await cb.answer("در حال تست...")
    try:
        info = await asyncio.wait_for(_prov(account).verify(), timeout=40)
        await cb.message.answer(
            f"✅ اتصال برقرار است — موجودی: {info.get('balance'):,.0f} "
            f"{info.get('currency')} | {info.get('presets')} تعرفه",
        )
    except Exception as e:
        from html import escape as _esc
        await cb.message.answer(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"اتصال ناموفق: <code>{_esc(str(e)[:300])}</code>",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("admin:tw_edit:"))
async def cb_tw_edit(cb: CallbackQuery, state: FSMContext):
    field = cb.data.split(":")[2]
    await state.update_data(tw_field=field)
    await state.set_state(TimewebFSM.edit_value)
    label = "نام جدید" if field == "name" else "توکن JWT جدید"
    await cb.message.edit_text(
        f"<b>ویرایش اکانت تایم‌وب</b>\n\n{label} را وارد کنید:",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(TimewebFSM.edit_value)
async def tw_edit_value(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    account = await _tw_account(session)
    if not account:
        await message.answer("اکانت یافت نشد.")
        return
    value = (message.text or "").strip()
    if data.get("tw_field") == "name":
        account.name = value
    else:
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await asyncio.wait_for(TimewebProvider(value).verify(), timeout=40)
        except Exception as e:
            from html import escape as _esc
            await message.answer(
                f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
                f"توکن نامعتبر: <code>{_esc(str(e)[:200])}</code>",
                parse_mode="HTML", reply_markup=back_to_admin_kb("admin:timeweb"),
            )
            return
        account.api_key = value
    await session.flush()
    await message.answer("ذخیره شد.", reply_markup=back_to_admin_kb("admin:timeweb"))


@router.callback_query(F.data == "admin:tw_limit")
async def cb_tw_limit(cb: CallbackQuery, state: FSMContext):
    await state.set_state(TimewebFSM.edit_limit)
    await cb.message.edit_text(
        "<b>لیمیت تعداد VM اکانت تایم‌وب</b>\n\n"
        "API تایم‌وب سقف اکانت را نمی‌دهد؛ این عدد کنترل داخلی ربات است.\n"
        "با رسیدن سرورهای فعال ربات به این عدد، خرید جدید مسدود می‌شود.\n\n"
        "عدد لیمیت (0 = بدون کنترل):",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(TimewebFSM.edit_limit, F.text.regexp(r"^\d+$"))
async def tw_limit_value(message: Message, state: FSMContext, session: AsyncSession):
    await state.clear()
    account = await _tw_account(session)
    if not account:
        await message.answer("اکانت یافت نشد.")
        return
    cfg = dict(account.extra_config or {})
    cfg["vm_limit"] = int(message.text)
    account.extra_config = cfg
    await session.flush()
    await message.answer(
        f"لیمیت VM روی {int(message.text) or 'بدون کنترل'} ثبت شد.",
        reply_markup=back_to_admin_kb("admin:timeweb"),
    )


@router.callback_query(F.data.startswith("admin:twm:"))
async def cb_tw_margin(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":")[2]
    await state.update_data(tw_margin_kind=kind)
    await state.set_state(TimewebFSM.edit_margin)
    label = "ساعتی" if kind == "h" else "ماهانه"
    await cb.message.edit_text(
        f"<b>درصد سود {label} (کل تایم‌وب)</b>\n\n"
        "قیمت فروش = قیمت خرید (روبل) × (۱ + سود٪)\n"
        "این سود روی <b>همه‌ی محصولات تایم‌وب</b> اعمال می‌شود و در سینک "
        "دوره‌ای هم دنبال قیمت تایم‌وب می‌ماند. با ثبت سود، محصولات "
        "ایمپورت‌شده فعال می‌شوند.\n\n"
        f"درصد سود {label} را وارد کنید (مثال: 35):",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(TimewebFSM.edit_margin, F.text.regexp(r"^\d+(\.\d+)?$"))
async def tw_margin_value(message: Message, state: FSMContext, session: AsyncSession):
    from bot.services.timeweb_settings import apply_margins_to_catalog, set_margin
    data = await state.get_data()
    await state.clear()
    await set_margin(session, hourly=(data.get("tw_margin_kind") == "h"),
                     value=float(message.text))
    await session.flush()
    updated = await apply_margins_to_catalog(session)
    await message.answer(
        f"سود ثبت شد ({message.text}٪) — قیمت فروش {updated} محصول تایم‌وب به‌روز و فعال شد.",
        reply_markup=back_to_admin_kb("admin:timeweb"),
    )


@router.callback_query(F.data == "admin:tw_toggle")
async def cb_tw_toggle(cb: CallbackQuery, session: AsyncSession):
    account = await _tw_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    account.is_active = not account.is_active
    await session.flush()
    await cb.answer(f"{'فعال' if account.is_active else 'غیرفعال'} شد.")
    await _render_tw_home(cb.message, session)


# ── گروه مقصد ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:twgrp")
async def cb_tw_group_pick(cb: CallbackQuery, session: AsyncSession):
    groups = (await session.execute(
        select(ProductGroup).order_by(ProductGroup.name)
    )).scalars().all()
    await cb.answer()
    await cb.message.edit_text(
        "<b>گروه مقصد محصولات تایم‌وب</b>\n\n"
        "همه‌ی محصولات تایم‌وب در این گروه قرار می‌گیرند (کاتالوگِ موجود هم منتقل می‌شود):\n"
        "<i>(گروه جدید را از «گروه محصولات» بسازید)</i>",
        parse_mode="HTML",
        reply_markup=group_pick_kb(groups, "admin:twgrpset",
                                   allow_new=False, cancel_cb="admin:timeweb"),
    )


@router.callback_query(F.data.startswith("admin:twgrpset:"))
async def cb_tw_group_set(cb: CallbackQuery, session: AsyncSession):
    from bot.services.timeweb_settings import set_group_name
    group = await session.get(ProductGroup, int(cb.data.split(":")[2]))
    if not group:
        await cb.answer("گروه یافت نشد.", show_alert=True)
        return
    await set_group_name(session, group.name)
    tw_plans = (await session.execute(
        select(ServerPlan).where(ServerPlan.provider_type == ProviderType.TIMEWEB)
    )).scalars().all()
    for p in tw_plans:
        p.category = group.name
    await session.flush()
    await cb.answer(f"گروه مقصد: {group.name}")
    await _render_tw_home(cb.message, session)


# ── حذف اکانت (قواعد ۵.۸) ────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:tw_del")
async def cb_tw_del(cb: CallbackQuery, session: AsyncSession):
    account = await _tw_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text(
        f"حذف اکانت <b>{account.name}</b>؟\n"
        "<i>چون تک-اکانتی است، همه‌ی محصولات تایم‌وب هم حذف می‌شوند.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="بله، حذف شود", callback_data="admin:tw_del_do"),
            InlineKeyboardButton(text="انصراف", callback_data="admin:timeweb"),
        ]]),
    )


@router.callback_query(F.data == "admin:tw_del_do")
async def cb_tw_del_do(cb: CallbackQuery, session: AsyncSession):
    account = await _tw_account(session)
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
        from sqlalchemy import update as _update, text as _text
        await session.execute(_text("SET LOCAL statement_timeout = '8s'"))
        plans = (await session.execute(
            select(ServerPlan).where(ServerPlan.provider_type == ProviderType.TIMEWEB)
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
        logger.exception("timeweb account delete failed")
        await session.rollback()
        from html import escape as _esc
        await cb.message.answer(
            "❌ حذف اکانت ناموفق بود:\n<code>" + _esc(str(e)[:300]) + "</code>"
        )
        return
    await _render_tw_home(cb.message, session)


# ── ایمپورت محصولات ──────────────────────────────────────────────────────────

# کش کوتاه‌مدت تعرفه‌ها (rate limit ۲۰/s سخاوتمند است ولی هر کلیک API نخورد)
_plans_cache: dict = {}


async def _location_plans(account: ProviderAccount, loc: str):
    key = (account.id, loc)
    cached = _plans_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < 300:
        return cached[1]
    plans = await asyncio.wait_for(
        _prov(account).list_plans(location=loc), timeout=30)
    plans.sort(key=lambda p: (p.price_monthly or 0))
    _plans_cache[key] = (now, plans)
    return plans


async def _imported_map(session: AsyncSession, loc: str) -> dict:
    rows = (await session.execute(
        select(ServerPlan).where(
            ServerPlan.provider_type == ProviderType.TIMEWEB,
            ServerPlan.location == loc,
        )
    )).scalars().all()
    return {p.provider_plan_id: p for p in rows}


@router.callback_query(F.data == "admin:tw_import")
async def cb_tw_import(cb: CallbackQuery, session: AsyncSession):
    from bot.services.timeweb_settings import get_group_name
    account = await _tw_account(session)
    if not account:
        await cb.answer("اول اکانت را اضافه کنید.", show_alert=True)
        return
    await cb.answer("در حال دریافت لوکیشن‌ها...")
    try:
        locs = await asyncio.wait_for(_prov(account).list_locations(), timeout=30)
    except Exception as e:
        from html import escape as _esc
        await cb.message.answer(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"خطا در دریافت لوکیشن‌ها: <code>{_esc(str(e)[:200])}</code>",
            parse_mode="HTML",
        )
        return
    group_name = await get_group_name(session)
    rows = [[InlineKeyboardButton(
        text=f"{l['display_name']} ({l['slug']})",
        callback_data=f"admin:twloc:{l['slug']}",
    )] for l in locs]
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data="admin:timeweb")])
    await cb.message.edit_text(
        "<b>ایمپورت محصولات تایم‌وب</b>\n\n"
        f"محصولات به گروه «{group_name}» می‌روند. قیمت‌ها ₽ ماهانه‌اند "
        "(ساعتی = ÷۷۲۰).\nلوکیشن را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _render_tw_plans(msg, session: AsyncSession, account: ProviderAccount, loc: str):
    plans = await _location_plans(account, loc)
    imported = await _imported_map(session, loc)
    rows = []
    for p in plans:
        mark = "✅" if p.provider_plan_id in imported else "⬜"
        ram_g = p.ram // 1024 if p.ram >= 1024 else p.ram
        rows.append([
            InlineKeyboardButton(
                text=f"{mark} {p.cpu}c/{ram_g}G/{p.disk}G · ₽{p.price_monthly:g}",
                callback_data=f"admin:twpick:{loc}:{p.provider_plan_id}",
            ),
            InlineKeyboardButton(
                text="ℹ️",
                callback_data=f"admin:twinfo:{loc}:{p.provider_plan_id}",
            ),
        ])
    # ایمپورت‌شده‌هایی که دیگر عرضه نمی‌شوند — قابل حذف بمانند
    shown = {p.provider_plan_id for p in plans}
    for pid in sorted(imported):
        if pid in shown:
            continue
        rows.append([InlineKeyboardButton(
            text=f"⛔ {pid} · ناموجود — حذف",
            callback_data=f"admin:twpick:{loc}:{pid}",
        )])
    rows.append([
        InlineKeyboardButton(text="ایمپورت همه", callback_data=f"admin:twallon:{loc}"),
        InlineKeyboardButton(text="حذف همه", callback_data=f"admin:twalloff:{loc}"),
    ])
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data="admin:tw_import")])
    await msg.edit_text(
        f"<b>تعرفه‌های تایم‌وب — {LOC_LABELS.get(loc, loc)}</b>\n\n"
        "عدد = قیمت خرید ماهانه (₽) · تپ = افزودن/حذف · ℹ️ = جزئیات\n"
        "<i>محصول تازه‌ایمپورت‌شده تا تعیین سود غیرفعال می‌ماند.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("admin:twloc:"))
async def cb_tw_location(cb: CallbackQuery, session: AsyncSession):
    account = await _tw_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    loc = cb.data.split(":")[2]
    await cb.answer("در حال دریافت تعرفه‌ها...")
    try:
        await _render_tw_plans(cb.message, session, account, loc)
    except Exception as e:
        from html import escape as _esc
        await cb.message.answer(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"خطا در دریافت تعرفه‌ها: <code>{_esc(str(e)[:200])}</code>",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("admin:twinfo:"))
async def cb_tw_info(cb: CallbackQuery, session: AsyncSession):
    account = await _tw_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    parts = cb.data.split(":")
    loc, pid = parts[2], parts[3]
    plans = await _location_plans(account, loc)
    info = next((p for p in plans if p.provider_plan_id == pid), None)
    if not info:
        await cb.answer("تعرفه یافت نشد.", show_alert=True)
        return
    try:
        raw = await asyncio.wait_for(_prov(account).preset_details(pid), timeout=20) or {}
    except Exception:
        raw = {}
    from bot.services.timeweb_settings import IP_RUB_MONTH, full_costs
    ch, cm = full_costs(info.price_monthly or 0)
    ram_g = info.ram // 1024 if info.ram >= 1024 else info.ram
    await cb.answer(
        f"preset {pid} — {info.cpu} vCPU ({raw.get('cpu_frequency', '?')}GHz) / "
        f"{ram_g}GB RAM / {info.disk}GB {str(raw.get('disk_type') or '').upper()}\n"
        f"کانال: {raw.get('bandwidth', '?')} Mbit (ترافیک نامحدود)\n"
        f"تعرفه: ₽{info.price_monthly:g} + IP: ₽{IP_RUB_MONTH:g}\n"
        f"خرید کامل: ₽{cm:g}/ماه · ₽{ch:g}/ساعت\n"
        f"{raw.get('description_short') or ''}",
        show_alert=True,
    )


async def _import_one(session: AsyncSession, account: ProviderAccount,
                      loc: str, info, group_name: str) -> ServerPlan:
    from bot.services.timeweb_settings import full_costs
    disk_type = ""
    try:
        raw = await _prov(account).preset_details(info.provider_plan_id) or {}
        disk_type = (raw.get("disk_type") or "").upper()
        bandwidth_mbit = raw.get("bandwidth")
        cpu_freq = raw.get("cpu_frequency")
    except Exception:
        bandwidth_mbit = cpu_freq = None
    # قیمت خرید کامل = تعرفه + IPv4 عمومی (سرویس جدا در فاکتور تایم‌وب)
    ch, cm = full_costs(info.price_monthly or 0)
    plan = ServerPlan(
        provider_type=ProviderType.TIMEWEB,
        provider_account_id=account.id,
        name=f"tw{info.provider_plan_id}-{loc}",
        display_name=f"{info.disk}G {disk_type}".strip(),
        ram=info.ram, cpu=info.cpu, disk=info.disk,
        bandwidth=0,                              # ترافیک نامحدود (کانال Mbit جدا)
        price_hourly=None, price_monthly=None,    # فروش با سود سراسری
        location=loc,
        is_active=False,
        category=group_name,
        provider_plan_id=info.provider_plan_id,
        extra_data={
            "currency": "rub",
            "preset_rub": info.price_monthly,     # تعرفه‌ی خام (برای سینک/مقایسه)
            "cost_hourly": ch,                    # خرید کامل ₽/ساعت (تعرفه+IP ÷۷۲۰)
            "cost_monthly": cm,                   # خرید کامل ₽/ماه (تعرفه+IP)
            "region_name": LOC_LABELS.get(loc, loc),
            "disk_type": disk_type,
            "bandwidth_mbit": bandwidth_mbit,
            "cpu_frequency": cpu_freq,
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


@router.callback_query(F.data.startswith("admin:twallon:"))
async def cb_tw_all_on(cb: CallbackQuery, session: AsyncSession):
    from bot.services.timeweb_settings import apply_margins_to_catalog, get_group_name
    account = await _tw_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    loc = cb.data.split(":")[2]
    plans = await _location_plans(account, loc)
    imported = await _imported_map(session, loc)
    group_name = await get_group_name(session)
    added = 0
    for info in plans:
        if info.provider_plan_id in imported:
            continue
        await _import_one(session, account, loc, info, group_name)
        added += 1
    await session.flush()
    if added:
        await apply_margins_to_catalog(session)
    await cb.answer(f"{added} تعرفه اضافه شد." if added else "همه از قبل ایمپورت شده‌اند.")
    await _render_tw_plans(cb.message, session, account, loc)


@router.callback_query(F.data.startswith("admin:twalloff:"))
async def cb_tw_all_off(cb: CallbackQuery, session: AsyncSession):
    account = await _tw_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    loc = cb.data.split(":")[2]
    imported = await _imported_map(session, loc)
    removed = kept = 0
    for pid, plan in imported.items():
        deleted, _ = await _remove_plan(session, plan)
        if deleted:
            removed += 1
        else:
            kept += 1
    await session.flush()
    note = f"{removed} حذف شد" + (f"، {kept} فقط غیرفعال شد (سرور فعال دارد)" if kept else "")
    await cb.answer(note if (removed or kept) else "چیزی برای حذف نیست.", show_alert=bool(kept))
    await _render_tw_plans(cb.message, session, account, loc)


@router.callback_query(F.data.startswith("admin:twpick:"))
async def cb_tw_pick(cb: CallbackQuery, session: AsyncSession):
    from bot.services.timeweb_settings import (
        apply_margins_to_catalog, get_group_name, get_margins,
    )
    account = await _tw_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    parts = cb.data.split(":")
    loc, pid = parts[2], parts[3]

    existing = (await session.execute(
        select(ServerPlan).where(
            ServerPlan.provider_type == ProviderType.TIMEWEB,
            ServerPlan.provider_plan_id == pid,
            ServerPlan.location == loc,
        )
    )).scalar_one_or_none()

    if existing:
        deleted, note = await _remove_plan(session, existing)
        await session.flush()
        await cb.answer(f"{pid}: {note}", show_alert=not deleted)
    else:
        plans = await _location_plans(account, loc)
        info = next((p for p in plans if p.provider_plan_id == pid), None)
        if not info:
            await cb.answer("تعرفه در این لوکیشن موجود نیست.", show_alert=True)
            return
        group_name = await get_group_name(session)
        await _import_one(session, account, loc, info, group_name)
        await session.flush()
        mh, mm = await get_margins(session)
        if mh is not None or mm is not None:
            await apply_margins_to_catalog(session)
            await cb.answer(f"✅ تعرفه {pid} اضافه و قیمت‌گذاری/فعال شد.")
        else:
            await cb.answer(f"✅ تعرفه {pid} اضافه شد — سود تایم‌وب را تنظیم کنید.")
    await _render_tw_plans(cb.message, session, account, loc)
