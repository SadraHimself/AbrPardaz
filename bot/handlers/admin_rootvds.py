"""Admin panel — RootVDS account + product import (تک-اکانتی).

فلو: محصولات ← سرویس‌دهنده‌ها ← روت (RootVDS)
- افزودن اکانت: نام + Auth-Token (پنل rootvds ← my/settings/create-api-token) →
  تست زنده (موجودی + تعرفه‌ها) → ذخیره → حذف پیام توکن از چت
- جزئیات: تست / ایمپورت / ویرایش نام-توکن / لیمیت VM دستی / سود ساعتی و ماهانه /
  گروه مقصد / حذف
- ایمپورت: لوکیشن → لیست تعرفه‌ها با «قیمت خرید» (₽ ماهانه) → تپ = ServerPlan
  (غیرفعال تا تعیین سود). خرید ساعتی = ماهانه ÷ ۷۲۰. نرخ روبل مشترک با تایم‌وب.

⚠️ auto-hide موجودی نداریم (درس تایم‌وب) — خطای ظرفیت فقط شکستِ همان خرید است.
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
from bot.providers.rootvds import API_BASE, RootVDSProvider

logger = logging.getLogger(__name__)
router = Router(name="admin_rootvds")


class AdminFilter(Filter):
    async def __call__(self, event: Message | CallbackQuery, user: User | None = None) -> bool:
        if user is None:
            return False
        return user.is_admin or (user.telegram_id in settings.admin_ids)


router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())


class RootVDSFSM(StatesGroup):
    add_name = State()
    add_token = State()
    edit_value = State()    # name | token
    edit_limit = State()
    edit_margin = State()


def _prov(account: ProviderAccount) -> RootVDSProvider:
    return RootVDSProvider(api_token=account.api_key or "")


async def _rv_account(session: AsyncSession) -> ProviderAccount | None:
    from bot.services.rootvds_settings import get_account
    return await get_account(session)


async def _safe_edit(msg, text: str, reply_markup=None):
    from aiogram.exceptions import TelegramBadRequest
    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ── صفحه اصلی (تک-اکانتی: لیست = جزئیات) ─────────────────────────────────────

async def _render_rv_home(msg, session: AsyncSession):
    from bot.services.rootvds_settings import get_group_name, get_margins
    account = await _rv_account(session)

    if not account:
        await _safe_edit(
            msg,
            "<b>روت (RootVDS)</b>\n\n"
            "هنوز اکانتی ثبت نشده. توکن از پنل rootvds ساخته می‌شود:\n"
            "<i>rootvds.ru ← my/settings/create-api-token</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="افزودن اکانت", callback_data="admin:rv_add")],
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
            ServerPlan.provider_type == ProviderType.ROOTVDS)
    )).scalar() or 0
    servers_count = (await session.execute(
        select(func.count(Server.id)).where(
            Server.provider_account_id == account.id,
            Server.status != ServerStatus.DELETED,
        )
    )).scalar() or 0

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="تست اتصال", callback_data="admin:rv_test"),
         InlineKeyboardButton(text="ایمپورت محصولات", callback_data="admin:rv_import")],
        [InlineKeyboardButton(text="ویرایش نام", callback_data="admin:rv_edit:name"),
         InlineKeyboardButton(text="ویرایش توکن", callback_data="admin:rv_edit:token")],
        [InlineKeyboardButton(text=f"سود ساعتی: {mh if mh is not None else '—'}٪",
                              callback_data="admin:rvm:h"),
         InlineKeyboardButton(text=f"سود ماهانه: {mm if mm is not None else '—'}٪",
                              callback_data="admin:rvm:m")],
        [InlineKeyboardButton(text=f"لیمیت VM: {vm_limit or 'تعیین نشده'}",
                              callback_data="admin:rv_limit")],
        [InlineKeyboardButton(text=f"گروه مقصد: {group}", callback_data="admin:rvgrp")],
        [InlineKeyboardButton(
            text=("غیرفعال کردن" if account.is_active else "فعال کردن"),
            callback_data="admin:rv_toggle")],
        [InlineKeyboardButton(text="حذف اکانت", callback_data="admin:rv_del")],
        [InlineKeyboardButton(text="بازگشت", callback_data="admin:provtypes")],
    ])
    await _safe_edit(
        msg,
        f"<b>روت (RootVDS)</b>\n\n"
        f"اکانت: {account.name} {'✅' if account.is_active else '❌'}\n"
        f"Token: <code>{token_masked}</code>\n"
        f"سرورهای فعال مشتری: {servers_count}"
        f"{f' / {vm_limit}' if vm_limit else ''}\n"
        f"محصولات ایمپورت‌شده: {plans_count}\n\n"
        "قیمت‌ها به روبل است — نرخ روبل خودکار از نوسان (مشترک با تایم‌وب).\n"
        "فروش ساعتی و ماهانه — خرید ساعتی = ماهانه ÷ ۷۲۰.\n"
        "محصول ایمپورت‌شده تا تعیین سود غیرفعال است.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "admin:rootvds")
async def cb_rootvds(cb: CallbackQuery, session: AsyncSession):
    await cb.answer()
    await _render_rv_home(cb.message, session)


# ── افزودن اکانت ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:rv_add")
async def cb_rv_add(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    if await _rv_account(session):
        await cb.answer("RootVDS فعلاً تک-اکانتی است — اکانت موجود را ویرایش کنید.",
                        show_alert=True)
        return
    await state.set_state(RootVDSFSM.add_name)
    await cb.message.edit_text(
        "<b>افزودن اکانت RootVDS</b>\n\n"
        "نام دلخواه اکانت را وارد کنید:\n<i>مثال: روت اصلی</i>",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(RootVDSFSM.add_name)
async def rv_add_name(message: Message, state: FSMContext):
    await state.update_data(rv_name=(message.text or "").strip())
    await state.set_state(RootVDSFSM.add_token)
    await message.answer(
        "توکن API را وارد کنید:\n"
        "<i>پنل rootvds ← my/settings/create-api-token</i>",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )


@router.message(RootVDSFSM.add_token)
async def rv_add_token(message: Message, state: FSMContext, session: AsyncSession):
    token = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()
    # توکن نباید در چت بماند
    try:
        await message.delete()
    except Exception:
        pass

    wait = await message.answer("در حال تست اتصال به RootVDS...")
    prov = RootVDSProvider(api_token=token)
    try:
        info = await asyncio.wait_for(prov.verify(), timeout=40)
    except Exception as e:
        from html import escape as _esc
        await wait.edit_text(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> اتصال ناموفق:\n'
            f"<code>{_esc(str(e)[:300])}</code>\n\nدوباره از «افزودن اکانت» تلاش کنید.",
            parse_mode="HTML",
            reply_markup=back_to_admin_kb("admin:rootvds"),
        )
        return

    account = ProviderAccount(
        provider_type=ProviderType.ROOTVDS,
        name=data.get("rv_name") or "RootVDS",
        api_key=token,
        api_secret=None,
        api_endpoint=API_BASE,
        is_active=True,
        strict_kyc=False,
    )
    session.add(account)
    await session.flush()

    await wait.edit_text(
        f"✅ <b>اکانت RootVDS اضافه شد!</b>\n\n"
        f"نام: {account.name}\n"
        f"موجودی اکانت: {info.get('balance'):,.0f} {info.get('currency')}\n"
        f"تعرفه‌ها: {info.get('presets')} در {info.get('locations')} لوکیشن\n\n"
        "حالا «سود ساعتی/ماهانه» را تنظیم و محصولات را ایمپورت کنید.",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb("admin:rootvds"),
    )


# ── تست / ویرایش / لیمیت / سود / toggle ──────────────────────────────────────

@router.callback_query(F.data == "admin:rv_test")
async def cb_rv_test(cb: CallbackQuery, session: AsyncSession):
    account = await _rv_account(session)
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


@router.callback_query(F.data.startswith("admin:rv_edit:"))
async def cb_rv_edit(cb: CallbackQuery, state: FSMContext):
    field = cb.data.split(":")[2]
    await state.update_data(rv_field=field)
    await state.set_state(RootVDSFSM.edit_value)
    label = "نام جدید" if field == "name" else "توکن API جدید"
    await cb.message.edit_text(
        f"<b>ویرایش اکانت RootVDS</b>\n\n{label} را وارد کنید:",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(RootVDSFSM.edit_value)
async def rv_edit_value(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    account = await _rv_account(session)
    if not account:
        await message.answer("اکانت یافت نشد.")
        return
    value = (message.text or "").strip()
    if data.get("rv_field") == "name":
        account.name = value
    else:
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await asyncio.wait_for(RootVDSProvider(value).verify(), timeout=40)
        except Exception as e:
            from html import escape as _esc
            await message.answer(
                f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
                f"توکن نامعتبر: <code>{_esc(str(e)[:200])}</code>",
                parse_mode="HTML", reply_markup=back_to_admin_kb("admin:rootvds"),
            )
            return
        account.api_key = value
    await session.flush()
    await message.answer("ذخیره شد.", reply_markup=back_to_admin_kb("admin:rootvds"))


@router.callback_query(F.data == "admin:rv_limit")
async def cb_rv_limit(cb: CallbackQuery, state: FSMContext):
    await state.set_state(RootVDSFSM.edit_limit)
    await cb.message.edit_text(
        "<b>لیمیت تعداد VM اکانت RootVDS</b>\n\n"
        "API سقف اکانت را نمی‌دهد؛ این عدد کنترل داخلی ربات است.\n"
        "با رسیدن سرورهای فعال ربات به این عدد، خرید جدید مسدود می‌شود.\n\n"
        "عدد لیمیت (0 = بدون کنترل):",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(RootVDSFSM.edit_limit, F.text.regexp(r"^\d+$"))
async def rv_limit_value(message: Message, state: FSMContext, session: AsyncSession):
    await state.clear()
    account = await _rv_account(session)
    if not account:
        await message.answer("اکانت یافت نشد.")
        return
    cfg = dict(account.extra_config or {})
    cfg["vm_limit"] = int(message.text)
    account.extra_config = cfg
    await session.flush()
    await message.answer(
        f"لیمیت VM روی {int(message.text) or 'بدون کنترل'} ثبت شد.",
        reply_markup=back_to_admin_kb("admin:rootvds"),
    )


@router.callback_query(F.data.startswith("admin:rvm:"))
async def cb_rv_margin(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":")[2]
    await state.update_data(rv_margin_kind=kind)
    await state.set_state(RootVDSFSM.edit_margin)
    label = "ساعتی" if kind == "h" else "ماهانه"
    await cb.message.edit_text(
        f"<b>درصد سود {label} (کل RootVDS)</b>\n\n"
        "قیمت فروش = قیمت خرید (روبل) × (۱ + سود٪)\n"
        "این سود روی <b>همه‌ی محصولات RootVDS</b> اعمال می‌شود و در سینک "
        "دوره‌ای هم دنبال قیمت provider می‌ماند. با ثبت سود، محصولات "
        "ایمپورت‌شده فعال می‌شوند.\n\n"
        f"درصد سود {label} را وارد کنید (مثال: 35):",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(RootVDSFSM.edit_margin, F.text.regexp(r"^\d+(\.\d+)?$"))
async def rv_margin_value(message: Message, state: FSMContext, session: AsyncSession):
    from bot.services.rootvds_settings import apply_margins_to_catalog, set_margin
    data = await state.get_data()
    await state.clear()
    await set_margin(session, hourly=(data.get("rv_margin_kind") == "h"),
                     value=float(message.text))
    await session.flush()
    updated = await apply_margins_to_catalog(session)
    await message.answer(
        f"سود ثبت شد ({message.text}٪) — قیمت فروش {updated} محصول RootVDS به‌روز و فعال شد.",
        reply_markup=back_to_admin_kb("admin:rootvds"),
    )


@router.callback_query(F.data == "admin:rv_toggle")
async def cb_rv_toggle(cb: CallbackQuery, session: AsyncSession):
    account = await _rv_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    account.is_active = not account.is_active
    await session.flush()
    await cb.answer(f"{'فعال' if account.is_active else 'غیرفعال'} شد.")
    await _render_rv_home(cb.message, session)


# ── گروه مقصد ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:rvgrp")
async def cb_rv_group_pick(cb: CallbackQuery, session: AsyncSession):
    groups = (await session.execute(
        select(ProductGroup).order_by(ProductGroup.name)
    )).scalars().all()
    await cb.answer()
    await cb.message.edit_text(
        "<b>گروه مقصد محصولات RootVDS</b>\n\n"
        "همه‌ی محصولات RootVDS در این گروه قرار می‌گیرند (کاتالوگِ موجود هم منتقل می‌شود):\n"
        "<i>(گروه جدید را از «گروه محصولات» بسازید)</i>",
        parse_mode="HTML",
        reply_markup=group_pick_kb(groups, "admin:rvgrpset",
                                   allow_new=False, cancel_cb="admin:rootvds"),
    )


@router.callback_query(F.data.startswith("admin:rvgrpset:"))
async def cb_rv_group_set(cb: CallbackQuery, session: AsyncSession):
    from bot.services.rootvds_settings import set_group_name
    group = await session.get(ProductGroup, int(cb.data.split(":")[2]))
    if not group:
        await cb.answer("گروه یافت نشد.", show_alert=True)
        return
    await set_group_name(session, group.name)
    rv_plans = (await session.execute(
        select(ServerPlan).where(ServerPlan.provider_type == ProviderType.ROOTVDS)
    )).scalars().all()
    for p in rv_plans:
        p.category = group.name
    await session.flush()
    await cb.answer(f"گروه مقصد: {group.name}")
    await _render_rv_home(cb.message, session)


# ── حذف اکانت (قواعد ۵.۸) ────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:rv_del")
async def cb_rv_del(cb: CallbackQuery, session: AsyncSession):
    account = await _rv_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text(
        f"حذف اکانت <b>{account.name}</b>؟\n"
        "<i>چون تک-اکانتی است، همه‌ی محصولات RootVDS هم حذف می‌شوند.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="بله، حذف شود", callback_data="admin:rv_del_do"),
            InlineKeyboardButton(text="انصراف", callback_data="admin:rootvds"),
        ]]),
    )


@router.callback_query(F.data == "admin:rv_del_do")
async def cb_rv_del_do(cb: CallbackQuery, session: AsyncSession):
    account = await _rv_account(session)
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
            select(ServerPlan).where(ServerPlan.provider_type == ProviderType.ROOTVDS)
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
        logger.exception("rootvds account delete failed")
        await session.rollback()
        from html import escape as _esc
        await cb.message.answer(
            "❌ حذف اکانت ناموفق بود:\n<code>" + _esc(str(e)[:300]) + "</code>"
        )
        return
    await _render_rv_home(cb.message, session)


# ── ایمپورت محصولات ──────────────────────────────────────────────────────────

# کش کوتاه‌مدت تعرفه‌ها/لوکیشن‌ها — هر کلیک API نخورد
_plans_cache: dict = {}
_locs_cache: dict = {}


async def _location_plans(account: ProviderAccount, loc: str):
    key = (account.id, loc)
    cached = _plans_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < 300:
        return cached[1]
    plans = await asyncio.wait_for(
        _prov(account).list_plans(location=loc), timeout=30)
    plans.sort(key=lambda p: (p.ram, p.disk, p.price_monthly or 0))
    _plans_cache[key] = (now, plans)
    return plans


async def _locations(account: ProviderAccount) -> list[dict]:
    cached = _locs_cache.get(account.id)
    now = time.monotonic()
    if cached and now - cached[0] < 300:
        return cached[1]
    locs = await asyncio.wait_for(_prov(account).list_locations(), timeout=30)
    _locs_cache[account.id] = (now, locs)
    return locs


async def _imported_map(session: AsyncSession, loc: str) -> dict:
    rows = (await session.execute(
        select(ServerPlan).where(
            ServerPlan.provider_type == ProviderType.ROOTVDS,
            ServerPlan.location == loc,
        )
    )).scalars().all()
    return {p.provider_plan_id: p for p in rows}


def _city_code(display_name: str, slug: str) -> str:
    """کد کوتاه شهر برای اسم پلن: سه حرف اول نام (RootVDS نام‌گذاری ساده دارد)."""
    base = "".join(ch for ch in (display_name or slug) if ch.isalnum())
    return (base[:3] or slug.replace("-", "")[:3]).upper()


def _build_labels(plans: list, city_code: str) -> dict:
    """کد کوتاه هر پلنِ لوکیشن: {CITY}-1..N به‌ترتیب رم→دیسک (الگوی تایم‌وب،
    ساده‌شده — RootVDS نوع سرور جدا ندارد)."""
    ordered = sorted(plans, key=lambda p: (p.ram, p.disk, p.price_monthly or 0))
    return {p.provider_plan_id: f"{city_code}-{i + 1}"
            for i, p in enumerate(ordered)}


@router.callback_query(F.data == "admin:rv_import")
async def cb_rv_import(cb: CallbackQuery, session: AsyncSession):
    from bot.services.rootvds_settings import get_group_name
    account = await _rv_account(session)
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
    rows = [[InlineKeyboardButton(
        text=f"{l['display_name']} · {l.get('count', 0)} تعرفه",
        callback_data=f"admin:rvloc:{l['slug']}",
    )] for l in locs if l.get("count")]
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data="admin:rootvds")])
    await _safe_edit(
        cb.message,
        "<b>ایمپورت محصولات RootVDS</b>\n\n"
        f"محصولات به گروه «{group_name}» می‌روند. قیمت‌ها ₽ ماهانه‌اند "
        "(ساعتی = ÷۷۲۰).\nلوکیشن را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


def _plan_status_mark(plan: ServerPlan | None) -> str:
    if plan is None:
        return "⬜"                              # ایمپورت‌نشده
    if (plan.extra_data or {}).get("unavailable"):
        return "⛔"                              # از کاتالوگ provider حذف شده
    return "✅" if plan.is_active else "☑️"       # فعال / ایمپورت‌شده‌ی بی‌قیمت


async def _render_rv_plans(msg, session: AsyncSession, account: ProviderAccount, loc: str):
    plans = await _location_plans(account, loc)
    imported = await _imported_map(session, loc)
    locs = await _locations(account)
    loc_name = next((l["display_name"] for l in locs if l["slug"] == loc), loc)
    labels = _build_labels(plans, _city_code(loc_name, loc))
    rows = []
    for p in plans:
        db = imported.get(p.provider_plan_id)
        mark = _plan_status_mark(db)
        ram_g = p.ram // 1024 if p.ram >= 1024 else p.ram
        code = labels.get(p.provider_plan_id, "?")
        rows.append([
            InlineKeyboardButton(
                text=f"{mark} {code} · {p.cpu}c/{ram_g}G/{p.disk}G · ₽{p.price_monthly:g}",
                callback_data=f"admin:rvpick:{loc}:{p.provider_plan_id}",
            ),
            InlineKeyboardButton(
                text="ℹ️",
                callback_data=f"admin:rvinfo:{loc}:{p.provider_plan_id}",
            ),
        ])
    # ایمپورت‌شده‌هایی که دیگر در کاتالوگ provider نیستند
    shown = {p.provider_plan_id for p in plans}
    for pid in sorted(imported):
        if pid not in shown:
            rows.append([InlineKeyboardButton(
                text=f"⛔ {imported[pid].display_name or pid} · حذف‌شده از RootVDS — حذف",
                callback_data=f"admin:rvpick:{loc}:{pid}",
            )])
    rows.append([
        InlineKeyboardButton(text="ایمپورت همه", callback_data=f"admin:rvallon:{loc}"),
        InlineKeyboardButton(text="حذف همه", callback_data=f"admin:rvalloff:{loc}"),
    ])
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data="admin:rv_import")])
    await _safe_edit(
        msg,
        f"<b>تعرفه‌های RootVDS — {loc_name}</b>\n\n"
        "<b>راهنمای وضعیت:</b>\n"
        "✅ فعال (در فروش)\n"
        "☑️ ایمپورت‌شده بی‌قیمت\n"
        "⛔ حذف‌شده از کاتالوگ\n"
        "⬜ ایمپورت‌نشده\n\n"
        "عدد = قیمت خرید ماهانه (₽) · تپ = افزودن/حذف · ℹ️ = جزئیات",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("admin:rvloc:"))
async def cb_rv_location(cb: CallbackQuery, session: AsyncSession):
    account = await _rv_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    loc = cb.data.split(":")[2]
    await cb.answer("در حال دریافت تعرفه‌ها...")
    try:
        await _render_rv_plans(cb.message, session, account, loc)
    except Exception as e:
        from html import escape as _esc
        await cb.message.answer(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"خطا در دریافت تعرفه‌ها: <code>{_esc(str(e)[:200])}</code>",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("admin:rvinfo:"))
async def cb_rv_info(cb: CallbackQuery, session: AsyncSession):
    account = await _rv_account(session)
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
    ram_g = info.ram // 1024 if info.ram >= 1024 else info.ram
    # قالبِ خوانا: هر مقدار در خط خودش با لیبل فارسی
    await cb.answer(
        f"{pid}" + (f" — {info.name}" if info.name else "") + "\n"
        f"{info.cpu} هسته | {ram_g} گیگ رم | {info.disk} گیگ دیسک\n\n"
        "قیمت خرید (روبل):\n"
        f"ساعتی: {round(info.price_hourly or 0, 4):g}\n"
        f"ماهانه: {round(info.price_monthly or 0, 2):g}",
        show_alert=True,
    )


async def _import_one(session: AsyncSession, account: ProviderAccount,
                      loc: str, info, group_name: str,
                      display_name: str) -> ServerPlan:
    plan = ServerPlan(
        provider_type=ProviderType.ROOTVDS,
        provider_account_id=account.id,
        name=f"rv{info.provider_plan_id}-{loc}",
        display_name=display_name,
        ram=info.ram, cpu=info.cpu, disk=info.disk,
        bandwidth=0,                              # ترافیک گزارش نمی‌شود
        price_hourly=None, price_monthly=None,    # فروش با سود سراسری
        location=loc,
        is_active=False,
        category=group_name,
        provider_plan_id=info.provider_plan_id,
        extra_data={
            "currency": "rub",
            "cost_hourly": info.price_hourly,     # خرید ₽/ساعت (ماهانه ÷۷۲۰)
            "cost_monthly": info.price_monthly,   # خرید ₽/ماه
            "region_name": None,                  # پایین‌تر ست می‌شود
            "preset_name": info.name,
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


@router.callback_query(F.data.startswith("admin:rvallon:"))
async def cb_rv_all_on(cb: CallbackQuery, session: AsyncSession):
    from bot.services.rootvds_settings import apply_margins_to_catalog, get_group_name
    account = await _rv_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    loc = cb.data.split(":")[2]
    plans = await _location_plans(account, loc)
    imported = await _imported_map(session, loc)
    group_name = await get_group_name(session)
    locs = await _locations(account)
    loc_name = next((l["display_name"] for l in locs if l["slug"] == loc), loc)
    labels = _build_labels(plans, _city_code(loc_name, loc))
    added = 0
    for info in plans:
        if info.provider_plan_id in imported:
            continue
        plan = await _import_one(session, account, loc, info, group_name,
                                 labels.get(info.provider_plan_id, info.name))
        extra = dict(plan.extra_data or {})
        extra["region_name"] = loc_name
        plan.extra_data = extra
        added += 1
    await session.flush()
    if added:
        await apply_margins_to_catalog(session)
    await cb.answer(f"{added} تعرفه اضافه شد." if added else "همه از قبل ایمپورت شده‌اند.")
    await _render_rv_plans(cb.message, session, account, loc)


@router.callback_query(F.data.startswith("admin:rvalloff:"))
async def cb_rv_all_off(cb: CallbackQuery, session: AsyncSession):
    account = await _rv_account(session)
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
    await _render_rv_plans(cb.message, session, account, loc)


@router.callback_query(F.data.startswith("admin:rvpick:"))
async def cb_rv_pick(cb: CallbackQuery, session: AsyncSession):
    from bot.services.rootvds_settings import (
        apply_margins_to_catalog, get_group_name, get_margins,
    )
    account = await _rv_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    parts = cb.data.split(":")
    loc, pid = parts[2], parts[3]

    existing = (await session.execute(
        select(ServerPlan).where(
            ServerPlan.provider_type == ProviderType.ROOTVDS,
            ServerPlan.provider_plan_id == pid,
            ServerPlan.location == loc,
        )
    )).scalar_one_or_none()

    if existing:
        if not existing.is_active:
            # ایمپورت‌شده ولی غیرفعال → کلیک دوباره = فعال‌سازی (الگوی تایم‌وب)
            mh, mm = await get_margins(session)
            if mh is not None or mm is not None:
                await apply_margins_to_catalog(session)
            if not existing.is_active and (existing.price_hourly or existing.price_monthly):
                existing.is_active = True
            await session.flush()
            if existing.is_active:
                await cb.answer(f"✅ {pid}: دوباره فعال شد.")
            else:
                await cb.answer(f"{pid}: اول سود RootVDS را تنظیم کنید تا قیمت بگیرد.",
                                show_alert=True)
        else:
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
        locs = await _locations(account)
        loc_name = next((l["display_name"] for l in locs if l["slug"] == loc), loc)
        labels = _build_labels(plans, _city_code(loc_name, loc))
        plan = await _import_one(session, account, loc, info, group_name,
                                 labels.get(pid, info.name))
        extra = dict(plan.extra_data or {})
        extra["region_name"] = loc_name
        plan.extra_data = extra
        await session.flush()
        mh, mm = await get_margins(session)
        if mh is not None or mm is not None:
            await apply_margins_to_catalog(session)
            await cb.answer(f"✅ تعرفه {pid} اضافه و قیمت‌گذاری/فعال شد.")
        else:
            await cb.answer(f"✅ تعرفه {pid} اضافه شد — سود RootVDS را تنظیم کنید.")
    await _render_rv_plans(cb.message, session, account, loc)
