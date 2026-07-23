"""Admin panel — Gcore Edge Cloud account + product import (تک-اکانتی).

فلو: محصولات ← سرویس‌دهنده‌ها ← جیکور
- افزودن اکانت: نام + API Token (پورتال جیکور، اسکیم APIKey) + Project ID →
  تست زنده (clients/me + regions + probe پروژه) → ذخیره → حذف پیام توکن از چت
- جزئیات: تست / ایمپورت / ویرایش نام-توکن-project / لیمیت VM دستی /
  سود ساعتی-ماهانه / نرخ دیسک ($/GB/ماه) / دیسک پیش‌فرض / گروه مقصد / حذف
- ایمپورت: region → خانواده flavor → پلن‌ها با «قیمت خرید» (flavor + دیسک) →
  تپ = ساخت ServerPlan (غیرفعال تا سود ست شود؛ با سود ست‌شده فوراً قیمت می‌خورد)

نکته قیمت: flavor جیکور دیسک ندارد — دیسک volume جداست و قیمتش در API نیست؛
ادمین نرخ هر GB/ماه را دستی وارد می‌کند (gcore_settings).
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
    ProductGroup, ProviderAccount, ProviderType, Server, ServerPlan, ServerStatus, User,
)
from bot.keyboards.admin import back_to_admin_kb, cancel_admin_kb, group_pick_kb
from bot.providers.gcore import API_BASE, GcoreProvider

logger = logging.getLogger(__name__)
router = Router(name="admin_gcore")


class AdminFilter(Filter):
    async def __call__(self, event: Message | CallbackQuery, user: User | None = None) -> bool:
        if user is None:
            return False
        return user.is_admin or (user.telegram_id in settings.admin_ids)


router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())


class GcoreFSM(StatesGroup):
    add_name = State()
    add_token = State()
    add_project = State()
    edit_value = State()    # name | token | project
    edit_limit = State()
    edit_margin = State()
    edit_volrate = State()
    edit_diskgb = State()


_CUR_SYM = {"usd": "$", "eur": "€"}


def _sym(cur: str | None) -> str:
    return _CUR_SYM.get((cur or "usd").lower(), (cur or "").upper() + " ")


def _prov(account: ProviderAccount) -> GcoreProvider:
    return GcoreProvider(
        api_token=account.api_key or "",
        project_id=(account.extra_config or {}).get("project_id") or 0,
    )


async def _gc_account(session: AsyncSession) -> ProviderAccount | None:
    from bot.services.gcore_settings import get_account
    return await get_account(session)


# ── صفحه اصلی جیکور (تک-اکانتی: لیست = جزئیات) ──────────────────────────────

async def _render_gc_home(msg, session: AsyncSession):
    from bot.services.gcore_settings import (
        get_default_disk_gb, get_group_name, get_margins, get_volume_rate,
    )
    account = await _gc_account(session)

    if not account:
        await msg.edit_text(
            "<b>جیکور (Gcore Cloud)</b>\n\n"
            "هنوز اکانتی ثبت نشده. توکن از پورتال جیکور ساخته می‌شود:\n"
            "<i>Profile ← API tokens ← Create token (نقش Administrators/Engineers)</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="افزودن اکانت", callback_data="admin:gc_add")],
                [InlineKeyboardButton(text="بازگشت", callback_data="admin:provtypes")],
            ]),
        )
        return

    mh, _ = await get_margins(session)   # فروش جیکور فقط ساعتی است
    group = await get_group_name(session)
    vol_rate = await get_volume_rate(session)
    disk_gb = await get_default_disk_gb(session)
    cfg = account.extra_config or {}
    vm_limit = int(cfg.get("vm_limit") or 0)
    token_masked = f"{(account.api_key or '')[:6]}…{(account.api_key or '')[-4:]}"

    plans_count = (await session.execute(
        select(func.count(ServerPlan.id)).where(
            ServerPlan.provider_type == ProviderType.GCORE)
    )).scalar() or 0
    servers_count = (await session.execute(
        select(func.count(Server.id)).where(
            Server.provider_account_id == account.id,
            Server.status != ServerStatus.DELETED,
        )
    )).scalar() or 0

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="تست اتصال", callback_data="admin:gc_test"),
         InlineKeyboardButton(text="ایمپورت محصولات", callback_data="admin:gc_import")],
        [InlineKeyboardButton(text="ویرایش نام", callback_data="admin:gc_edit:name"),
         InlineKeyboardButton(text="ویرایش توکن", callback_data="admin:gc_edit:token")],
        [InlineKeyboardButton(text=f"Project ID: {cfg.get('project_id') or '—'}",
                              callback_data="admin:gc_edit:project"),
         InlineKeyboardButton(text=f"لیمیت VM: {vm_limit or 'تعیین نشده'}",
                              callback_data="admin:gc_limit")],
        [InlineKeyboardButton(text=f"سود ساعتی: {mh if mh is not None else '—'}٪",
                              callback_data="admin:gcm:h")],
        [InlineKeyboardButton(text=f"نرخ دیسک: {vol_rate:g} /GB/ماه (دستی)" if vol_rate
                              else "نرخ دیسک: خودکار (از API)",
                              callback_data="admin:gc_volrate"),
         InlineKeyboardButton(text=f"دیسک پیش‌فرض: {disk_gb} GB",
                              callback_data="admin:gc_diskgb")],
        [InlineKeyboardButton(text=f"گروه مقصد: {group}", callback_data="admin:gcgrp")],
        [InlineKeyboardButton(
            text=("غیرفعال کردن" if account.is_active else "فعال کردن"),
            callback_data="admin:gc_toggle")],
        [InlineKeyboardButton(text="حذف اکانت", callback_data="admin:gc_del")],
        [InlineKeyboardButton(text="بازگشت", callback_data="admin:provtypes")],
    ])
    await msg.edit_text(
        f"<b>جیکور (Gcore Cloud)</b>\n\n"
        f"اکانت: {account.name} {'✅' if account.is_active else '❌'}\n"
        f"Token: <code>{token_masked}</code>\n"
        f"سرورهای فعال مشتری: {servers_count}"
        f"{f' / {vm_limit}' if vm_limit else ''}\n"
        f"محصولات ایمپورت‌شده: {plans_count}\n\n"
        "فروش جیکور <b>فقط ساعتی</b> است. قیمت خرید = flavor + دیسک×نرخ دیسک — "
        "قیمت فروش = خرید × (۱ + سود٪).\n"
        "محصول ایمپورت‌شده تا تعیین «سود ساعتی» غیرفعال است و در فروش دیده نمی‌شود.\n"
        "ریبیلد و تغییر رمز برای جیکور در دسترس نیست (محدودیت API).",
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.callback_query(F.data == "admin:gcore")
async def cb_gcore(cb: CallbackQuery, session: AsyncSession):
    await cb.answer()
    await _render_gc_home(cb.message, session)


# ── افزودن اکانت ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:gc_add")
async def cb_gc_add(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    if await _gc_account(session):
        await cb.answer("جیکور فعلاً تک-اکانتی است — اکانت موجود را ویرایش کنید.",
                        show_alert=True)
        return
    await state.set_state(GcoreFSM.add_name)
    await cb.message.edit_text(
        "<b>افزودن اکانت جیکور</b>\n\n"
        "نام دلخواه اکانت را وارد کنید:\n<i>مثال: جیکور اصلی</i>",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(GcoreFSM.add_name)
async def gc_add_name(message: Message, state: FSMContext):
    await state.update_data(gc_name=(message.text or "").strip())
    await state.set_state(GcoreFSM.add_token)
    await message.answer(
        "API Token را وارد کنید:\n"
        "<i>Gcore Portal ← Profile ← API tokens ← Create token</i>",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )


@router.message(GcoreFSM.add_token)
async def gc_add_token(message: Message, state: FSMContext):
    token = (message.text or "").strip()
    await state.update_data(gc_token=token)
    # توکن نباید در چت بماند
    try:
        await message.delete()
    except Exception:
        pass
    await state.set_state(GcoreFSM.add_project)
    await message.answer(
        "Project ID را وارد کنید (عدد):\n"
        "<i>Gcore Portal ← Cloud ← Projects — معمولاً پروژه‌ی default</i>",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )


@router.message(GcoreFSM.add_project, F.text.regexp(r"^\d+$"))
async def gc_add_project(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    token, project_id = data.get("gc_token") or "", int(message.text)

    wait = await message.answer("در حال تست اتصال به Gcore...")
    prov = GcoreProvider(api_token=token, project_id=project_id)
    try:
        info = await asyncio.wait_for(prov.verify(), timeout=40)
    except Exception as e:
        from html import escape as _esc
        await wait.edit_text(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> اتصال ناموفق:\n'
            f"<code>{_esc(str(e)[:300])}</code>\n\nدوباره از «افزودن اکانت» تلاش کنید.",
            parse_mode="HTML",
            reply_markup=back_to_admin_kb("admin:gcore"),
        )
        return

    account = ProviderAccount(
        provider_type=ProviderType.GCORE,
        name=data.get("gc_name") or "Gcore",
        api_key=token,
        api_secret=None,
        api_endpoint=API_BASE,
        extra_config={"project_id": project_id},
        is_active=True,
        strict_kyc=False,
    )
    session.add(account)
    await session.flush()

    await wait.edit_text(
        f"✅ <b>اکانت جیکور اضافه شد!</b>\n\n"
        f"نام: {account.name}\n"
        f"اکانت جیکور: {info.get('email') or '—'} (client {info.get('client_id')})\n"
        f"لوکیشن‌های دارای VM: {info.get('regions')}\n\n"
        "قبل از ایمپورت، «نرخ دیسک» را از صفحه قیمت جیکور تنظیم کنید.",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb("admin:gcore"),
    )


# ── تست / ویرایش / لیمیت / toggle ────────────────────────────────────────────

@router.callback_query(F.data == "admin:gc_test")
async def cb_gc_test(cb: CallbackQuery, session: AsyncSession):
    account = await _gc_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    await cb.answer("در حال تست...")
    try:
        info = await asyncio.wait_for(_prov(account).verify(), timeout=40)
        await cb.message.answer(
            f"✅ اتصال برقرار است — {info.get('regions')} لوکیشن دارای VM "
            f"(اکانت: {info.get('email') or info.get('client_id')})",
        )
    except Exception as e:
        from html import escape as _esc
        await cb.message.answer(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"اتصال ناموفق: <code>{_esc(str(e)[:300])}</code>",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("admin:gc_edit:"))
async def cb_gc_edit(cb: CallbackQuery, state: FSMContext):
    field = cb.data.split(":")[2]
    await state.update_data(gc_field=field)
    await state.set_state(GcoreFSM.edit_value)
    label = {"name": "نام جدید", "token": "API Token جدید",
             "project": "Project ID جدید (عدد)"}.get(field, field)
    await cb.message.edit_text(
        f"<b>ویرایش اکانت جیکور</b>\n\n{label} را وارد کنید:",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(GcoreFSM.edit_value)
async def gc_edit_value(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    account = await _gc_account(session)
    if not account:
        await message.answer("اکانت یافت نشد.")
        return
    value = (message.text or "").strip()
    field = data.get("gc_field")
    from html import escape as _esc

    if field == "name":
        account.name = value
    elif field == "project":
        if not value.isdigit():
            await message.answer("Project ID باید عدد باشد.",
                                 reply_markup=back_to_admin_kb("admin:gcore"))
            return
        prov = GcoreProvider(api_token=account.api_key or "", project_id=int(value))
        try:
            await asyncio.wait_for(prov.verify(), timeout=40)
        except Exception as e:
            await message.answer(
                f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
                f"Project ID نامعتبر: <code>{_esc(str(e)[:200])}</code>",
                parse_mode="HTML", reply_markup=back_to_admin_kb("admin:gcore"),
            )
            return
        cfg = dict(account.extra_config or {})
        cfg["project_id"] = int(value)
        account.extra_config = cfg
    else:  # token
        try:
            await message.delete()
        except Exception:
            pass
        prov = GcoreProvider(api_token=value,
                             project_id=(account.extra_config or {}).get("project_id") or 0)
        try:
            await asyncio.wait_for(prov.verify(), timeout=40)
        except Exception as e:
            await message.answer(
                f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
                f"توکن نامعتبر: <code>{_esc(str(e)[:200])}</code>",
                parse_mode="HTML", reply_markup=back_to_admin_kb("admin:gcore"),
            )
            return
        account.api_key = value
    await session.flush()
    await message.answer("ذخیره شد.", reply_markup=back_to_admin_kb("admin:gcore"))


@router.callback_query(F.data == "admin:gc_limit")
async def cb_gc_limit(cb: CallbackQuery, state: FSMContext):
    await state.set_state(GcoreFSM.edit_limit)
    await cb.message.edit_text(
        "<b>لیمیت تعداد VM اکانت جیکور</b>\n\n"
        "سقف واقعی را quota جیکور هم گارد می‌کند؛ این عدد کنترل داخلی ربات است.\n"
        "با رسیدن سرورهای فعال ربات به این عدد، خرید جدید مسدود می‌شود.\n\n"
        "عدد لیمیت (0 = بدون کنترل):",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(GcoreFSM.edit_limit, F.text.regexp(r"^\d+$"))
async def gc_limit_value(message: Message, state: FSMContext, session: AsyncSession):
    await state.clear()
    account = await _gc_account(session)
    if not account:
        await message.answer("اکانت یافت نشد.")
        return
    cfg = dict(account.extra_config or {})
    cfg["vm_limit"] = int(message.text)
    account.extra_config = cfg
    await session.flush()
    await message.answer(
        f"لیمیت VM روی {int(message.text) or 'بدون کنترل'} ثبت شد.",
        reply_markup=back_to_admin_kb("admin:gcore"),
    )


@router.callback_query(F.data.startswith("admin:gcm:"))
async def cb_gc_margin(cb: CallbackQuery, state: FSMContext):
    await state.update_data(gc_margin_kind="h")   # فروش جیکور فقط ساعتی است
    await state.set_state(GcoreFSM.edit_margin)
    await cb.message.edit_text(
        "<b>درصد سود ساعتی (کل جیکور)</b>\n\n"
        "فروش جیکور فقط ساعتی است.\n"
        "قیمت فروش = قیمت خرید کامل (flavor + دیسک) × (۱ + سود٪)\n"
        "این سود روی <b>همه‌ی محصولات جیکور</b> اعمال می‌شود و در سینک دوره‌ای هم "
        "دنبال قیمت جیکور می‌ماند. با ثبت سود، محصولات ایمپورت‌شده فعال می‌شوند.\n\n"
        "درصد سود ساعتی را وارد کنید (مثال: 35):",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(GcoreFSM.edit_margin, F.text.regexp(r"^\d+(\.\d+)?$"))
async def gc_margin_value(message: Message, state: FSMContext, session: AsyncSession):
    from bot.services.gcore_settings import apply_margins_to_catalog, set_margin
    data = await state.get_data()
    await state.clear()
    await set_margin(session, hourly=(data.get("gc_margin_kind", "h") == "h"),
                     value=float(message.text))
    await session.flush()
    updated = await apply_margins_to_catalog(session)
    note = ""
    if not updated:
        note = ("\n\n⚠️ هیچ محصولی قیمت نگرفت — احتمالاً قیمت‌های API صفر برگشته "
                "(اکانت trial؟). بعد از اصلاح، دوباره سود را ثبت کنید.")
    await message.answer(
        f"سود ثبت شد ({message.text}٪) — قیمت فروش {updated} محصول جیکور به‌روز و فعال شد."
        f"{note}",
        reply_markup=back_to_admin_kb("admin:gcore"),
    )


@router.callback_query(F.data == "admin:gc_volrate")
async def cb_gc_volrate(cb: CallbackQuery, state: FSMContext):
    await state.set_state(GcoreFSM.edit_volrate)
    await cb.message.edit_text(
        "<b>نرخ دیسک جیکور</b>\n\n"
        "پیش‌فرض «خودکار» است: قیمت دیسک زنده از API قیمت‌گذاری خود جیکور "
        "خوانده می‌شود و نیازی به تنظیم دستی نیست.\n"
        "فقط اگر می‌خواهید override دستی بگذارید، قیمت هر GB در ماه را به ارز "
        "اکانت وارد کنید.\n\n"
        "عدد را وارد کنید (0 = برگشت به حالت خودکار):",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(GcoreFSM.edit_volrate, F.text.regexp(r"^\d+(\.\d+)?$"))
async def gc_volrate_value(message: Message, state: FSMContext, session: AsyncSession):
    from bot.services.gcore_settings import (
        apply_margins_to_catalog, recompute_catalog_costs, set_volume_rate,
    )
    await state.clear()
    await set_volume_rate(session, float(message.text))
    await session.flush()
    _volprice_cache.clear()
    account = await _gc_account(session)
    n = await recompute_catalog_costs(
        session, provider=_prov(account) if account else None)
    await apply_margins_to_catalog(session)
    label = message.text if float(message.text) > 0 else "خودکار (از API)"
    await message.answer(
        f"نرخ دیسک: {label} — قیمت خرید/فروش {n} محصول بازمحاسبه شد.",
        reply_markup=back_to_admin_kb("admin:gcore"),
    )


@router.callback_query(F.data == "admin:gc_diskgb")
async def cb_gc_diskgb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(GcoreFSM.edit_diskgb)
    await cb.message.edit_text(
        "<b>دیسک پیش‌فرض پلن‌های جیکور</b>\n\n"
        "حجم volume بوت (GB) برای پلن‌هایی که از این به بعد ایمپورت می‌شوند.\n"
        "پلن‌های ایمپورت‌شده‌ی قبلی حجم خودشان را نگه می‌دارند.\n\n"
        "عدد GB را وارد کنید (مثال: 5):",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(GcoreFSM.edit_diskgb, F.text.regexp(r"^\d+$"))
async def gc_diskgb_value(message: Message, state: FSMContext, session: AsyncSession):
    from bot.services.gcore_settings import set_default_disk_gb
    await state.clear()
    gb = int(message.text)
    if gb < 5 or gb > 2000:
        await message.answer("عدد بین 5 تا 2000 وارد کنید.")
        return
    await set_default_disk_gb(session, gb)
    await session.flush()
    await message.answer(f"دیسک پیش‌فرض روی {gb} GB ثبت شد.",
                         reply_markup=back_to_admin_kb("admin:gcore"))


@router.callback_query(F.data == "admin:gc_toggle")
async def cb_gc_toggle(cb: CallbackQuery, session: AsyncSession):
    account = await _gc_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    account.is_active = not account.is_active
    await session.flush()
    await cb.answer(f"{'فعال' if account.is_active else 'غیرفعال'} شد.")
    await _render_gc_home(cb.message, session)


# ── گروه مقصد ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:gcgrp")
async def cb_gc_group_pick(cb: CallbackQuery, session: AsyncSession):
    groups = (await session.execute(
        select(ProductGroup).order_by(ProductGroup.name)
    )).scalars().all()
    await cb.answer()
    await cb.message.edit_text(
        "<b>گروه مقصد محصولات جیکور</b>\n\n"
        "همه‌ی محصولات جیکور در این گروه قرار می‌گیرند (کاتالوگِ موجود هم منتقل می‌شود):\n"
        "<i>(گروه جدید را از «گروه محصولات» بسازید)</i>",
        parse_mode="HTML",
        reply_markup=group_pick_kb(groups, "admin:gcgrpset",
                                   allow_new=False, cancel_cb="admin:gcore"),
    )


@router.callback_query(F.data.startswith("admin:gcgrpset:"))
async def cb_gc_group_set(cb: CallbackQuery, session: AsyncSession):
    from bot.services.gcore_settings import set_group_name
    group = await session.get(ProductGroup, int(cb.data.split(":")[2]))
    if not group:
        await cb.answer("گروه یافت نشد.", show_alert=True)
        return
    await set_group_name(session, group.name)
    gc_plans = (await session.execute(
        select(ServerPlan).where(ServerPlan.provider_type == ProviderType.GCORE)
    )).scalars().all()
    for p in gc_plans:
        p.category = group.name
    await session.flush()
    await cb.answer(f"گروه مقصد: {group.name}")
    await _render_gc_home(cb.message, session)


# ── حذف اکانت (قواعد ۵.۸) ────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:gc_del")
async def cb_gc_del(cb: CallbackQuery, session: AsyncSession):
    account = await _gc_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text(
        f"حذف اکانت <b>{account.name}</b>؟\n"
        "<i>چون تک-اکانتی است، همه‌ی محصولات جیکور هم حذف می‌شوند.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="بله، حذف شود", callback_data="admin:gc_del_do"),
            InlineKeyboardButton(text="انصراف", callback_data="admin:gcore"),
        ]]),
    )


@router.callback_query(F.data == "admin:gc_del_do")
async def cb_gc_del_do(cb: CallbackQuery, session: AsyncSession):
    account = await _gc_account(session)
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
        # پلن‌های جیکور حذف می‌شوند (اکانت دیگری برای انتقال نیست — تک-اکانتی)
        plans = (await session.execute(
            select(ServerPlan).where(ServerPlan.provider_type == ProviderType.GCORE)
        )).scalars().all()
        for p in plans:
            await session.delete(p)
        # سرورهای تاریخیِ DELETED هنوز FK دارند → NULL
        await session.execute(
            _update(Server).where(Server.provider_account_id == account.id)
            .values(provider_account_id=None)
        )
        # اول وابسته‌ها + flush جدا، بعد اکانت (relationship تعریف نشده — ترتیب دستی)
        await session.flush()
        await session.delete(account)
        await session.flush()
    except Exception as e:
        logger.exception("gcore account delete failed")
        await session.rollback()
        from html import escape as _esc
        await cb.message.answer(
            "❌ حذف اکانت ناموفق بود:\n<code>" + _esc(str(e)[:300]) + "</code>"
        )
        return
    await _render_gc_home(cb.message, session)


# ── ایمپورت محصولات ──────────────────────────────────────────────────────────

# کش کوتاه‌مدت تا هر کلیک یک API call نخورد (rate limit جیکور نامشخص → محافظه‌کار)
_regions_cache: dict = {}
_plans_cache: dict = {}
_volprice_cache: dict = {}


async def _disk_monthly(session: AsyncSession, account: ProviderAccount,
                        rid: int, gb: int) -> float:
    """هزینه ماهانه دیسک با کش ۵ دقیقه‌ای (نرخ دستی یا قیمت زنده API)."""
    from bot.services.gcore_settings import disk_monthly_cost
    key = (account.id, int(rid), int(gb))
    cached = _volprice_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < 300:
        return cached[1]
    val = await disk_monthly_cost(session, _prov(account), rid, gb)
    _volprice_cache[key] = (now, val)
    return val


async def _gc_regions(account: ProviderAccount) -> list[dict]:
    cached = _regions_cache.get(account.id)
    now = time.monotonic()
    if cached and now - cached[0] < 300:
        return cached[1]
    regions = await asyncio.wait_for(_prov(account).list_regions(), timeout=30)
    _regions_cache[account.id] = (now, regions)
    return regions


async def _gc_region(account: ProviderAccount, rid: int) -> dict | None:
    return next((r for r in await _gc_regions(account) if r["id"] == rid), None)


async def _region_plans(account: ProviderAccount, rid: int):
    key = (account.id, rid)
    cached = _plans_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < 300:
        return cached[1]
    plans = await asyncio.wait_for(
        _prov(account).list_plans(location=str(rid)), timeout=30)
    _plans_cache[key] = (now, plans)
    return plans


def _family(flavor_id: str) -> str:
    """خانواده flavor: دو بخش اول ID («g2-standard-2-4» → «g2-standard»)."""
    parts = (flavor_id or "").split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else (flavor_id or "other")


def _is_excluded(flavor_id: str) -> bool:
    """خانواده‌های عرضه‌نشدنی — Basic VM (shared) و memory-optimized.
    منبع واحد سیاست در gcore_settings است (سینک/اعمال سود هم از همان می‌خوانند)."""
    from bot.services.gcore_settings import is_excluded_flavor
    return is_excluded_flavor(flavor_id)


async def _imported_map(session: AsyncSession, slug: str) -> dict:
    """provider_plan_id → ServerPlan برای محصولات ایمپورت‌شده‌ی این region."""
    rows = (await session.execute(
        select(ServerPlan).where(
            ServerPlan.provider_type == ProviderType.GCORE,
            ServerPlan.location == slug,
        )
    )).scalars().all()
    return {p.provider_plan_id: p for p in rows}


@router.callback_query(F.data == "admin:gc_import")
async def cb_gc_import(cb: CallbackQuery, session: AsyncSession):
    from bot.services.gcore_settings import get_group_name
    account = await _gc_account(session)
    if not account:
        await cb.answer("اول اکانت را اضافه کنید.", show_alert=True)
        return
    await cb.answer("در حال دریافت لوکیشن‌ها...")
    try:
        regions = await _gc_regions(account)
    except Exception as e:
        from html import escape as _esc
        await cb.message.answer(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"خطا در دریافت لوکیشن‌ها: <code>{_esc(str(e)[:200])}</code>",
            parse_mode="HTML",
        )
        return
    group_name = await get_group_name(session)
    warn = ""   # هزینه دیسک خودکار از API قیمت جیکور خوانده می‌شود

    rows, pair = [], []
    for r in regions:
        pair.append(InlineKeyboardButton(
            text=f"{r['display_name']} ({r['country']})",
            callback_data=f"admin:gcloc:{r['id']}",
        ))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data="admin:gcore")])
    await cb.message.edit_text(
        "<b>ایمپورت محصولات جیکور</b>\n\n"
        f"محصولات به گروه «{group_name}» می‌روند.{warn}\n"
        "لوکیشن را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("admin:gcloc:"))
async def cb_gc_location(cb: CallbackQuery, session: AsyncSession):
    account = await _gc_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    rid = int(cb.data.split(":")[2])
    await cb.answer("در حال دریافت پلن‌ها و قیمت‌ها...")
    region = await _gc_region(account, rid)
    if not region:
        await cb.answer("لوکیشن یافت نشد.", show_alert=True)
        return
    try:
        plans = await _region_plans(account, rid)
    except Exception as e:
        from html import escape as _esc
        await cb.message.answer(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"خطا در دریافت پلن‌ها: <code>{_esc(str(e)[:200])}</code>",
            parse_mode="HTML",
        )
        return
    imported = await _imported_map(session, region["slug"])

    fams: dict = {}
    for p in plans:
        if _is_excluded(p.provider_plan_id):
            continue  # Basic VM و memory ارائه نمی‌شوند
        fams.setdefault(_family(p.provider_plan_id), []).append(p)
    # خانواده‌هایی که فقط پلن ایمپورت‌شده‌ی قدیمی دارند (استثناشده/حذف‌شده از عرضه)
    # هم باید دکمه بگیرند تا ادمین بتواند پلن‌هایشان را حذف کند
    for pid in imported:
        fams.setdefault(_family(pid), [])

    rows = []
    for fam in sorted(fams):
        total = len(fams[fam])
        n_imp = sum(1 for pid in imported if _family(pid) == fam)
        rows.append([InlineKeyboardButton(
            text=f"{fam} ({n_imp}/{total})",
            callback_data=f"admin:gcfam:{rid}:{fam}",
        )])
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data="admin:gc_import")])
    await cb.message.edit_text(
        f"<b>پلن‌های جیکور — {region['display_name']}</b>\n\n"
        "دسته را انتخاب کنید (ایمپورت‌شده/کل):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _render_gc_family(msg, session: AsyncSession, account: ProviderAccount,
                            rid: int, fam: str):
    from bot.services.gcore_settings import get_default_disk_gb
    region = await _gc_region(account, rid)
    if not region:
        return
    plans = [p for p in await _region_plans(account, rid)
             if _family(p.provider_plan_id) == fam
             and not _is_excluded(p.provider_plan_id)]
    imported = await _imported_map(session, region["slug"])
    disk_gb = await get_default_disk_gb(session)
    dm = await _disk_monthly(session, account, rid, disk_gb)

    rows = []
    for p in sorted(plans, key=lambda x: (x.price_monthly or 0)):
        mark = "✅" if p.provider_plan_id in imported else "⬜"
        ram_g = p.ram // 1024 if p.ram >= 1024 else p.ram
        rows.append([
            InlineKeyboardButton(
                text=f"{mark} {p.provider_plan_id} · {p.cpu}c/{ram_g}G · "
                     f"{_sym(p.currency)}{p.price_monthly:g}",
                callback_data=f"admin:gcpick:{rid}:{p.provider_plan_id}",
            ),
            InlineKeyboardButton(
                text="ℹ️",
                callback_data=f"admin:gcinfo:{rid}:{p.provider_plan_id}",
            ),
        ])
    # ایمپورت‌شده‌هایی که دیگر در این region نیستند — قابل حذف بمانند
    shown = {p.provider_plan_id for p in plans}
    for pid in sorted(imported):
        if _family(pid) != fam or pid in shown:
            continue
        rows.append([InlineKeyboardButton(
            text=f"⛔ {pid} · ناموجود — حذف",
            callback_data=f"admin:gcpick:{rid}:{pid}",
        )])
    rows.append([
        InlineKeyboardButton(text="ایمپورت همه", callback_data=f"admin:gcfamon:{rid}:{fam}"),
        InlineKeyboardButton(text="حذف همه", callback_data=f"admin:gcfamoff:{rid}:{fam}"),
    ])
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data=f"admin:gcloc:{rid}")])
    await msg.edit_text(
        f"<b>{fam} — {region['display_name']}</b>\n\n"
        "عدد = قیمت خرید ماهانه‌ی flavor (بدون دیسک) · تپ = افزودن/حذف · ℹ️ = جزئیات\n"
        f"دیسک پلن‌های جدید: {disk_gb} GB ≈ {dm:g} در ماه "
        "(قیمت زنده از API — به قیمت خرید اضافه می‌شود)\n"
        "<i>محصول تازه‌ایمپورت‌شده تا تعیین سود غیرفعال می‌ماند.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("admin:gcfamon:"))
async def cb_gc_family_all_on(cb: CallbackQuery, session: AsyncSession):
    from bot.services.gcore_settings import apply_margins_to_catalog, get_group_name
    account = await _gc_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    parts = cb.data.split(":")
    rid, fam = int(parts[2]), parts[3]
    region = await _gc_region(account, rid)
    if not region:
        await cb.answer("لوکیشن یافت نشد.", show_alert=True)
        return
    plans = [p for p in await _region_plans(account, rid)
             if _family(p.provider_plan_id) == fam
             and not _is_excluded(p.provider_plan_id)]
    imported = await _imported_map(session, region["slug"])
    group_name = await get_group_name(session)
    added = 0
    for info in plans:
        if info.provider_plan_id in imported:
            continue
        await _import_one(session, account, region, info, group_name)
        added += 1
    await session.flush()
    if added:
        await apply_margins_to_catalog(session)
    await cb.answer(f"{added} پلن اضافه شد." if added else "همه از قبل ایمپورت شده‌اند.")
    await _render_gc_family(cb.message, session, account, rid, fam)


@router.callback_query(F.data.startswith("admin:gcfamoff:"))
async def cb_gc_family_all_off(cb: CallbackQuery, session: AsyncSession):
    account = await _gc_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    parts = cb.data.split(":")
    rid, fam = int(parts[2]), parts[3]
    region = await _gc_region(account, rid)
    if not region:
        await cb.answer("لوکیشن یافت نشد.", show_alert=True)
        return
    imported = await _imported_map(session, region["slug"])
    removed = kept = 0
    for pid, plan in imported.items():
        if _family(pid) != fam:
            continue
        deleted, _ = await _remove_plan(session, plan)
        if deleted:
            removed += 1
        else:
            kept += 1
    await session.flush()
    note = f"{removed} حذف شد" + (f"، {kept} فقط غیرفعال شد (سرور فعال دارد)" if kept else "")
    await cb.answer(note if (removed or kept) else "چیزی برای حذف نیست.", show_alert=bool(kept))
    await _render_gc_family(cb.message, session, account, rid, fam)


@router.callback_query(F.data.startswith("admin:gcfam:"))
async def cb_gc_family(cb: CallbackQuery, session: AsyncSession):
    account = await _gc_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    parts = cb.data.split(":")
    await cb.answer()
    await _render_gc_family(cb.message, session, account, int(parts[2]), parts[3])


@router.callback_query(F.data.startswith("admin:gcinfo:"))
async def cb_gc_info(cb: CallbackQuery, session: AsyncSession):
    from bot.services.gcore_settings import full_costs, get_default_disk_gb
    account = await _gc_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    parts = cb.data.split(":")
    rid, pid = int(parts[2]), parts[3]
    plans = await _region_plans(account, rid)
    info = next((p for p in plans if p.provider_plan_id == pid), None)
    if not info:
        await cb.answer("پلن یافت نشد.", show_alert=True)
        return
    disk_gb = await get_default_disk_gb(session)
    dm = await _disk_monthly(session, account, rid, disk_gb)
    ch, cm = full_costs(info.price_hourly or 0, info.price_monthly or 0, dm)
    s = _sym(info.currency)
    await cb.answer(
        f"{pid} — {info.cpu} vCPU / {info.ram // 1024 if info.ram >= 1024 else info.ram} GB RAM\n"
        f"Flavor: {s}{info.price_hourly:g}/h · {s}{info.price_monthly:g}/mo\n"
        f"Disk {disk_gb}GB: {s}{dm:g}/mo\n"
        f"قیمت خرید کامل: {s}{ch:g}/h · {s}{cm:g}/mo\n"
        "(ترافیک: نامحدود/رایگان)",
        show_alert=True,
    )


async def _import_one(session: AsyncSession, account: ProviderAccount,
                      region: dict, info, group_name: str) -> ServerPlan:
    from bot.services.gcore_settings import full_costs, get_default_disk_gb
    disk_gb = await get_default_disk_gb(session)
    dm = await _disk_monthly(session, account, int(region["id"]), disk_gb)
    ch, cm = full_costs(info.price_hourly or 0, info.price_monthly or 0, dm)
    plan = ServerPlan(
        provider_type=ProviderType.GCORE,
        provider_account_id=account.id,
        name=f"{info.provider_plan_id}-{region['slug']}",   # نام داخلی — یکتا با لوکیشن
        display_name=info.provider_plan_id.upper(),
        ram=info.ram, cpu=info.cpu, disk=disk_gb,
        bandwidth=0,                              # ترافیک جیکور نامحدود (0 = نامحدود)
        price_hourly=None, price_monthly=None,    # فروش با سود سراسری محاسبه می‌شود
        location=region["slug"],                  # ASCII برای callback_data + نمایش
        is_active=False,                          # تا قیمت‌گذاری، در فروش دیده نمی‌شود
        category=group_name,
        provider_plan_id=info.provider_plan_id,
        extra_data={
            "currency": (info.currency or "usd"),
            "cost_hourly": ch,                    # خرید کامل (flavor + دیسک)
            "cost_monthly": cm,
            "flavor_cost_hourly": info.price_hourly,   # خرید خامِ flavor (برای سینک)
            "flavor_cost_monthly": info.price_monthly,
            "region_id": int(region["id"]),       # برای فراخوانی‌های API
            "region_name": region["display_name"],  # لیبل مرحله‌ی لوکیشن خرید
        },
    )
    session.add(plan)
    return plan


async def _remove_plan(session: AsyncSession, plan: ServerPlan) -> tuple[bool, str]:
    """حذف پلن؛ اگر سرور فعال مشتری دارد فقط غیرفعال می‌شود."""
    servers = (await session.execute(
        select(Server).where(Server.status != ServerStatus.DELETED)
    )).scalars().all()
    in_use = any((s.extra_data or {}).get("plan_id") == plan.id for s in servers)
    if in_use:
        plan.is_active = False
        return False, "غیرفعال شد (سرور فعال دارد)"
    await session.delete(plan)
    return True, "حذف شد"


@router.callback_query(F.data.startswith("admin:gcpick:"))
async def cb_gc_pick(cb: CallbackQuery, session: AsyncSession):
    from bot.services.gcore_settings import (
        apply_margins_to_catalog, get_group_name, get_margins,
    )
    account = await _gc_account(session)
    if not account:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return
    parts = cb.data.split(":")
    rid, pid = int(parts[2]), parts[3]
    region = await _gc_region(account, rid)
    if not region:
        await cb.answer("لوکیشن یافت نشد.", show_alert=True)
        return

    existing = (await session.execute(
        select(ServerPlan).where(
            ServerPlan.provider_type == ProviderType.GCORE,
            ServerPlan.provider_plan_id == pid,
            ServerPlan.location == region["slug"],
        )
    )).scalar_one_or_none()

    if existing:
        deleted, note = await _remove_plan(session, existing)
        await session.flush()
        await cb.answer(f"{pid}: {note}", show_alert=not deleted)
    else:
        if _is_excluded(pid):
            await cb.answer("این خانواده ارائه نمی‌شود (Basic VM / memory).", show_alert=True)
            return
        plans = await _region_plans(account, rid)
        info = next((p for p in plans if p.provider_plan_id == pid), None)
        if not info:
            await cb.answer("پلن در این لوکیشن موجود نیست.", show_alert=True)
            return
        group_name = await get_group_name(session)
        await _import_one(session, account, region, info, group_name)
        await session.flush()
        mh, mm = await get_margins(session)
        if mh is not None or mm is not None:
            await apply_margins_to_catalog(session)
            await cb.answer(f"✅ {pid} اضافه و با سود جیکور قیمت‌گذاری/فعال شد.")
        else:
            await cb.answer(f"✅ {pid} به گروه «{group_name}» اضافه شد — سود جیکور را تنظیم کنید.")
    await _render_gc_family(cb.message, session, account, rid, _family(pid))
