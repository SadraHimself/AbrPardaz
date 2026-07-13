"""Admin panel — Hetzner Cloud accounts + product import.

فلو: محصولات ← سرویس‌دهنده‌ها ← هتزنر
- افزودن اکانت: نام دلخواه (مثل «اکانت آلمان») + API Token → تست زنده → ذخیره
- جزئیات اکانت: تست / ایمپورت محصولات / ویرایش نام و توکن / فعال‌غیرفعال / حذف
- ایمپورت: لوکیشن → لیست پلن‌ها با «قیمت خرید» (EUR gross) → با یک تپ ServerPlan
  ساخته می‌شود (ارز eur، قیمت فروش خالی و غیرفعال تا ادمین قیمت بگذارد).
"""
from __future__ import annotations

import asyncio
import logging

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
from bot.providers.hetzner import API_BASE, HetznerProvider

logger = logging.getLogger(__name__)
router = Router(name="admin_hetzner")


class AdminFilter(Filter):
    async def __call__(self, event: Message | CallbackQuery, user: User) -> bool:
        return user.is_admin or (user.telegram_id in settings.admin_ids)


router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())


class HetznerFSM(StatesGroup):
    add_name = State()
    add_token = State()
    edit_value = State()
    edit_limit = State()   # لیمیت تعداد VM اکانت (دستی — API سقف را نمی‌دهد)


def _st(ok: bool) -> str:
    return "✅ " if ok else "❌ "


# ── لیست اکانت‌ها ─────────────────────────────────────────────────────────────

async def _hz_accounts(session: AsyncSession) -> list[ProviderAccount]:
    result = await session.execute(
        select(ProviderAccount).where(ProviderAccount.provider_type == ProviderType.HETZNER)
        .order_by(ProviderAccount.id)
    )
    return list(result.scalars().all())


async def _render_hz_list(msg, session: AsyncSession):
    accounts = await _hz_accounts(session)
    rows = [[InlineKeyboardButton(text=f"{_st(a.is_active)}{a.name}",
                                  callback_data=f"admin:hz:{a.id}")] for a in accounts]
    rows.append([InlineKeyboardButton(text="افزودن اکانت", callback_data="admin:hz_add")])
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data="admin:provtypes")])
    await msg.edit_text(
        f"<b>هتزنر (Hetzner Cloud)</b>\n\n{len(accounts)} اکانت:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "admin:hetzner")
async def cb_hetzner(cb: CallbackQuery, session: AsyncSession):
    await cb.answer()
    await _render_hz_list(cb.message, session)


# ── افزودن اکانت ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:hz_add")
async def cb_hz_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(HetznerFSM.add_name)
    await cb.message.edit_text(
        "<b>افزودن اکانت هتزنر</b>\n\n"
        "نام دلخواه اکانت را وارد کنید:\n<i>مثال: اکانت آلمان</i>",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(HetznerFSM.add_name)
async def hz_add_name(message: Message, state: FSMContext):
    await state.update_data(hz_name=(message.text or "").strip())
    await state.set_state(HetznerFSM.add_token)
    await message.answer(
        "API Token را وارد کنید:\n"
        "<i>Hetzner Console ← پروژه ← Security ← API Tokens ← New Token (Read & Write)</i>",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )


@router.message(HetznerFSM.add_token)
async def hz_add_token(message: Message, state: FSMContext, session: AsyncSession):
    token = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()

    # توکن نباید در چت بماند
    try:
        await message.delete()
    except Exception:
        pass

    wait = await message.answer("در حال تست اتصال به Hetzner...")
    prov = HetznerProvider(api_token=token)
    try:
        locs = await asyncio.wait_for(prov.list_locations(), timeout=20)
        types = await asyncio.wait_for(prov._paginate("/server_types", "server_types"), timeout=20)
    except Exception as e:
        await wait.edit_text(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> اتصال ناموفق:\n'
            f"<code>{str(e)[:300]}</code>\n\nدوباره از «افزودن اکانت» تلاش کنید.",
            parse_mode="HTML",
            reply_markup=back_to_admin_kb("admin:hetzner"),
        )
        return

    account = ProviderAccount(
        provider_type=ProviderType.HETZNER,
        name=data.get("hz_name") or "Hetzner",
        api_key=token,
        api_secret=None,
        api_endpoint=API_BASE,
        is_active=True,
        strict_kyc=False,
    )
    session.add(account)
    await session.flush()

    await wait.edit_text(
        f"✅ <b>اکانت هتزنر اضافه شد!</b>\n\n"
        f"نام: {account.name}\n"
        f"لوکیشن‌ها: {len(locs)} | پلن‌های موجود: {len(types)}\n\n"
        "حالا از «ایمپورت محصولات» پلن‌ها را اضافه کنید.",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb(f"admin:hz:{account.id}"),
    )


# ── جزئیات اکانت ─────────────────────────────────────────────────────────────

async def _render_hz_detail(msg, session: AsyncSession, account: ProviderAccount):
    plans_count = (await session.execute(
        select(func.count(ServerPlan.id)).where(ServerPlan.provider_account_id == account.id)
    )).scalar() or 0
    servers_count = (await session.execute(
        select(func.count(Server.id)).where(
            Server.provider_account_id == account.id,
            Server.status != ServerStatus.DELETED,
        )
    )).scalar() or 0
    token_masked = f"{(account.api_key or '')[:6]}…{(account.api_key or '')[-4:]}"

    # مصرف زنده اکانت از API (کل VMهای اکانت، نه فقط ساخته‌های ربات)
    vm_limit = int((account.extra_config or {}).get("vm_limit") or 0)
    try:
        live_count = await asyncio.wait_for(
            HetznerProvider(account.api_key or "").count_servers(), timeout=12
        )
        cap_line = (f"ظرفیت اکانت: {live_count} / {vm_limit if vm_limit else '—'}"
                    + ("" if vm_limit else " (لیمیت را ثبت کنید)"))
    except Exception:
        cap_line = (f"ظرفیت اکانت: نامشخص / {vm_limit or '—'} "
                    "(خطا در خواندن از API)")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="تست اتصال", callback_data=f"admin:hz_test:{account.id}"),
         InlineKeyboardButton(text="ایمپورت محصولات", callback_data=f"admin:hz_import:{account.id}")],
        [InlineKeyboardButton(text="ویرایش نام", callback_data=f"admin:hz_edit:{account.id}:name"),
         InlineKeyboardButton(text="ویرایش توکن", callback_data=f"admin:hz_edit:{account.id}:token")],
        [InlineKeyboardButton(text=f"لیمیت VM: {vm_limit or 'تعیین نشده'}",
                              callback_data=f"admin:hz_limit:{account.id}")],
        [InlineKeyboardButton(
            text=("غیرفعال کردن" if account.is_active else "فعال کردن"),
            callback_data=f"admin:hz_toggle:{account.id}")],
        [InlineKeyboardButton(text="حذف اکانت", callback_data=f"admin:hz_del:{account.id}")],
        [InlineKeyboardButton(text="بازگشت", callback_data="admin:hetzner")],
    ])
    await msg.edit_text(
        f"<b>{account.name}</b>\n\n"
        f"Token: <code>{token_masked}</code>\n"
        f"وضعیت: {'✅ فعال' if account.is_active else '❌ غیرفعال'}\n"
        f"{cap_line}\n"
        f"محصولات ایمپورت‌شده: {plans_count}\n"
        f"سرورهای فعال مشتری (ربات): {servers_count}\n"
        f"ID: {account.id}",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def _get_hz_account(cb: CallbackQuery, session: AsyncSession, acc_id: int):
    account = await session.get(ProviderAccount, acc_id)
    if not account or account.provider_type != ProviderType.HETZNER:
        await cb.answer("اکانت یافت نشد.", show_alert=True)
        return None
    return account


@router.callback_query(F.data.startswith("admin:hz:"))
async def cb_hz_detail(cb: CallbackQuery, session: AsyncSession):
    account = await _get_hz_account(cb, session, int(cb.data.split(":")[2]))
    if not account:
        return
    await cb.answer()
    await _render_hz_detail(cb.message, session, account)


@router.callback_query(F.data.startswith("admin:hz_test:"))
async def cb_hz_test(cb: CallbackQuery, session: AsyncSession):
    account = await _get_hz_account(cb, session, int(cb.data.split(":")[2]))
    if not account:
        return
    await cb.answer("در حال تست...")
    prov = HetznerProvider(api_token=account.api_key or "")
    try:
        locs = await asyncio.wait_for(prov.list_locations(), timeout=20)
        await cb.message.answer(
            f"✅ اتصال برقرار است — {len(locs)} لوکیشن در دسترس.",
        )
    except Exception as e:
        await cb.message.answer(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"اتصال ناموفق: <code>{str(e)[:300]}</code>",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("admin:hz_edit:"))
async def cb_hz_edit(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    acc_id, field = int(parts[2]), parts[3]
    await state.update_data(hz_id=acc_id, hz_field=field)
    await state.set_state(HetznerFSM.edit_value)
    label = "نام جدید" if field == "name" else "API Token جدید"
    await cb.message.edit_text(
        f"<b>ویرایش اکانت</b>\n\n{label} را وارد کنید:",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(HetznerFSM.edit_value)
async def hz_edit_value(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    account = await session.get(ProviderAccount, data.get("hz_id"))
    if not account:
        await message.answer("اکانت یافت نشد.")
        return
    value = (message.text or "").strip()
    if data.get("hz_field") == "name":
        account.name = value
    else:
        # تست توکن جدید قبل از ذخیره
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await asyncio.wait_for(HetznerProvider(value).ping(), timeout=20)
        except Exception as e:
            await message.answer(
                f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
                f"توکن نامعتبر: <code>{str(e)[:200]}</code>",
                parse_mode="HTML",
                reply_markup=back_to_admin_kb(f"admin:hz:{account.id}"),
            )
            return
        account.api_key = value
    await session.flush()
    await message.answer("ذخیره شد.", reply_markup=back_to_admin_kb(f"admin:hz:{account.id}"))


@router.callback_query(F.data.startswith("admin:hz_limit:"))
async def cb_hz_limit(cb: CallbackQuery, state: FSMContext):
    acc_id = int(cb.data.split(":")[2])
    await state.update_data(hz_id=acc_id)
    await state.set_state(HetznerFSM.edit_limit)
    await cb.message.edit_text(
        "<b>لیمیت تعداد VM اکانت</b>\n\n"
        "هتزنر سقف اکانت را از API نمی‌دهد؛ عددی که در پنل/تیکت تأیید شده را وارد کنید.\n"
        "با رسیدن مصرف زنده به این عدد، خرید جدید از این اکانت مسدود می‌شود.\n\n"
        "عدد لیمیت (0 = بدون کنترل):",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(HetznerFSM.edit_limit, F.text.regexp(r"^\d+$"))
async def hz_limit_value(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    account = await session.get(ProviderAccount, data.get("hz_id"))
    if not account:
        await message.answer("اکانت یافت نشد.")
        return
    cfg = dict(account.extra_config or {})
    cfg["vm_limit"] = int(message.text)
    account.extra_config = cfg
    await session.flush()
    await message.answer(
        f"لیمیت VM روی {int(message.text) or 'بدون کنترل'} ثبت شد.",
        reply_markup=back_to_admin_kb(f"admin:hz:{account.id}"),
    )


@router.callback_query(F.data.startswith("admin:hz_toggle:"))
async def cb_hz_toggle(cb: CallbackQuery, session: AsyncSession):
    account = await _get_hz_account(cb, session, int(cb.data.split(":")[2]))
    if not account:
        return
    account.is_active = not account.is_active
    await session.flush()
    await cb.answer(f"{'فعال' if account.is_active else 'غیرفعال'} شد.")
    await _render_hz_detail(cb.message, session, account)


@router.callback_query(F.data.startswith("admin:hz_del_do:"))
async def cb_hz_del_do(cb: CallbackQuery, session: AsyncSession):
    account = await _get_hz_account(cb, session, int(cb.data.split(":")[2]))
    if not account:
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
    # حذف محصولات ایمپورت‌شده‌ی همین اکانت + خود اکانت
    plans = (await session.execute(
        select(ServerPlan).where(ServerPlan.provider_account_id == account.id)
    )).scalars().all()
    for p in plans:
        await session.delete(p)
    await session.delete(account)
    await session.flush()
    await cb.answer("اکانت حذف شد.")
    await _render_hz_list(cb.message, session)


@router.callback_query(F.data.startswith("admin:hz_del:"))
async def cb_hz_del(cb: CallbackQuery, session: AsyncSession):
    account = await _get_hz_account(cb, session, int(cb.data.split(":")[2]))
    if not account:
        return
    await cb.answer()
    await cb.message.edit_text(
        f"حذف اکانت <b>{account.name}</b>؟\n"
        "محصولات ایمپورت‌شده‌ی این اکانت هم حذف می‌شوند.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="بله، حذف شود", callback_data=f"admin:hz_del_do:{account.id}"),
            InlineKeyboardButton(text="انصراف", callback_data=f"admin:hz:{account.id}"),
        ]]),
    )


# ── ایمپورت محصولات ──────────────────────────────────────────────────────────

_FAMILY_ORDER = ["cx", "cpx", "cax", "ccx"]
_FAMILY_LABEL = {
    "cx": "CX — اشتراکی (Intel/AMD)",
    "cpx": "CPX — اشتراکی (AMD)",
    "cax": "CAX — اشتراکی (ARM)",
    "ccx": "CCX — اختصاصی",
}

# کش کوتاه‌مدت پلن‌ها تا هر کلیک یک API call نخورد (rate limit هتزنر)
_plans_cache: dict = {}


def _family(ptype: str) -> str:
    letters = "".join(ch for ch in (ptype or "") if ch.isalpha())
    return letters.lower() or "other"


async def _location_plans(account: ProviderAccount, loc: str):
    import time
    key = (account.id, loc)
    cached = _plans_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < 300:
        return cached[1]
    prov = HetznerProvider(api_token=account.api_key or "")
    plans = await asyncio.wait_for(prov.list_plans(location=loc), timeout=30)
    _plans_cache[key] = (now, plans)
    return plans


async def _imported_map(session: AsyncSession, account: ProviderAccount, loc: str) -> dict:
    """provider_plan_id → ServerPlan برای پلن‌های ایمپورت‌شده‌ی این اکانت/لوکیشن."""
    rows = (await session.execute(
        select(ServerPlan).where(
            ServerPlan.provider_account_id == account.id,
            ServerPlan.location == loc,
        )
    )).scalars().all()
    return {p.provider_plan_id: p for p in rows}


async def _import_group_name(session: AsyncSession, account: ProviderAccount) -> str:
    """گروه مقصد ایمپورت — پیش‌فرض گروه «Hetzner» (در صورت نبود، ساخته می‌شود)."""
    name = (account.extra_config or {}).get("import_group")
    if name:
        grp = (await session.execute(
            select(ProductGroup).where(ProductGroup.name == name)
        )).scalar_one_or_none()
        if grp:
            return name
    grp = (await session.execute(
        select(ProductGroup).where(ProductGroup.name == "Hetzner")
    )).scalar_one_or_none()
    if not grp:
        grp = ProductGroup(name="Hetzner", is_hidden=False)
        session.add(grp)
        await session.flush()
    cfg = dict(account.extra_config or {})
    cfg["import_group"] = grp.name
    account.extra_config = cfg
    await session.flush()
    return grp.name


async def _render_import_home(msg, session: AsyncSession, account: ProviderAccount):
    group_name = await _import_group_name(session, account)
    prov = HetznerProvider(api_token=account.api_key or "")
    locs = await asyncio.wait_for(prov.list_locations(), timeout=20)
    rows = [[InlineKeyboardButton(text=f"گروه مقصد: {group_name}",
                                  callback_data=f"admin:hzgrp:{account.id}")]]
    rows += [[InlineKeyboardButton(
        text=f"{l['city']} ({l['name']}) — {l['country']}",
        callback_data=f"admin:hzloc:{account.id}:{l['name']}",
    )] for l in locs]
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data=f"admin:hz:{account.id}")])
    await msg.edit_text(
        "<b>ایمپورت محصولات هتزنر</b>\n\n"
        f"محصولات ایمپورت‌شده به گروه «{group_name}» می‌روند.\n"
        "لوکیشن را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("admin:hz_import:"))
async def cb_hz_import(cb: CallbackQuery, session: AsyncSession):
    account = await _get_hz_account(cb, session, int(cb.data.split(":")[2]))
    if not account:
        return
    await cb.answer("در حال دریافت لوکیشن‌ها...")
    try:
        await _render_import_home(cb.message, session, account)
    except Exception as e:
        await cb.message.answer(
            f'\u200F<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"خطا در دریافت لوکیشن‌ها: <code>{str(e)[:200]}</code>",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("admin:hzgrpset:"))
async def cb_hz_group_set(cb: CallbackQuery, session: AsyncSession):
    parts = cb.data.split(":")
    account = await _get_hz_account(cb, session, int(parts[2]))
    if not account:
        return
    group = await session.get(ProductGroup, int(parts[3]))
    if not group:
        await cb.answer("گروه یافت نشد.", show_alert=True)
        return
    cfg = dict(account.extra_config or {})
    cfg["import_group"] = group.name
    account.extra_config = cfg
    await session.flush()
    await cb.answer(f"گروه مقصد: {group.name}")
    await _render_import_home(cb.message, session, account)


@router.callback_query(F.data.startswith("admin:hzgrp:"))
async def cb_hz_group_pick(cb: CallbackQuery, session: AsyncSession):
    account = await _get_hz_account(cb, session, int(cb.data.split(":")[2]))
    if not account:
        return
    groups = (await session.execute(
        select(ProductGroup).order_by(ProductGroup.name)
    )).scalars().all()
    await cb.answer()
    await cb.message.edit_text(
        "<b>گروه مقصد ایمپورت</b>\n\n"
        "محصولات ایمپورت‌شده در این گروه قرار می‌گیرند:\n"
        "<i>(گروه جدید را از «گروه محصولات» بسازید)</i>",
        parse_mode="HTML",
        reply_markup=group_pick_kb(groups, f"admin:hzgrpset:{account.id}",
                                   allow_new=False,
                                   cancel_cb=f"admin:hz_import:{account.id}"),
    )


@router.callback_query(F.data.startswith("admin:hzloc:"))
async def cb_hz_location(cb: CallbackQuery, session: AsyncSession):
    parts = cb.data.split(":")
    account = await _get_hz_account(cb, session, int(parts[2]))
    if not account:
        return
    loc = parts[3]
    await cb.answer("در حال دریافت پلن‌ها و قیمت‌ها...")
    try:
        plans = await _location_plans(account, loc)
    except Exception as e:
        await cb.message.answer(
            f'\u200F<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"خطا در دریافت پلن‌ها: <code>{str(e)[:200]}</code>",
            parse_mode="HTML",
        )
        return
    imported = await _imported_map(session, account, loc)

    fams: dict = {}
    for p in plans:
        fams.setdefault(_family(p.provider_plan_id), []).append(p)
    ordered = [f for f in _FAMILY_ORDER if f in fams] + \
              sorted(f for f in fams if f not in _FAMILY_ORDER)

    rows = []
    for fam in ordered:
        total = len(fams[fam])
        n_imp = sum(1 for p in fams[fam] if p.provider_plan_id in imported)
        label = _FAMILY_LABEL.get(fam, fam.upper())
        rows.append([InlineKeyboardButton(
            text=f"{label} ({n_imp}/{total})",
            callback_data=f"admin:hzfam:{account.id}:{loc}:{fam}",
        )])
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data=f"admin:hz_import:{account.id}")])
    await cb.message.edit_text(
        f"<b>پلن‌های هتزنر — {loc}</b>\n\nدسته را انتخاب کنید (ایمپورت‌شده/کل):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _render_family(msg, session: AsyncSession, account: ProviderAccount,
                         loc: str, fam: str):
    plans = [p for p in await _location_plans(account, loc)
             if _family(p.provider_plan_id) == fam]
    imported = await _imported_map(session, account, loc)
    rows = []
    for p in sorted(plans, key=lambda x: (x.price_monthly or 0)):
        mark = "✅" if p.provider_plan_id in imported else "⬜"
        rows.append([
            InlineKeyboardButton(
                text=f"{mark} {p.provider_plan_id} · {p.cpu}c/{p.ram // 1024}G/{p.disk}G · €{p.price_monthly:g}/ماه",
                callback_data=f"admin:hzpick:{account.id}:{loc}:{p.provider_plan_id}",
            ),
            InlineKeyboardButton(
                text="جزئیات",
                callback_data=f"admin:hzinfo:{account.id}:{loc}:{p.provider_plan_id}",
            ),
        ])
    rows.append([
        InlineKeyboardButton(text="ایمپورت همه", callback_data=f"admin:hzfamon:{account.id}:{loc}:{fam}"),
        InlineKeyboardButton(text="حذف همه", callback_data=f"admin:hzfamoff:{account.id}:{loc}:{fam}"),
    ])
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data=f"admin:hzloc:{account.id}:{loc}")])
    await msg.edit_text(
        f"<b>{_FAMILY_LABEL.get(fam, fam.upper())} — {loc}</b>\n\n"
        "تپ روی پلن = افزودن/حذف از محصولات · «جزئیات» = قیمت خرید ساعتی و ماهانه\n"
        "<i>محصول تازه‌ایمپورت‌شده غیرفعال است تا قیمت فروش بگذارید.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("admin:hzfamon:"))
async def cb_hz_family_all_on(cb: CallbackQuery, session: AsyncSession):
    parts = cb.data.split(":")
    account = await _get_hz_account(cb, session, int(parts[2]))
    if not account:
        return
    loc, fam = parts[3], parts[4]
    plans = [p for p in await _location_plans(account, loc)
             if _family(p.provider_plan_id) == fam]
    imported = await _imported_map(session, account, loc)
    group_name = await _import_group_name(session, account)
    added = 0
    for info in plans:
        if info.provider_plan_id in imported:
            continue
        await _import_one(session, account, loc, info, group_name)
        added += 1
    await session.flush()
    await cb.answer(f"{added} پلن اضافه شد." if added else "همه از قبل ایمپورت شده‌اند.")
    await _render_family(cb.message, session, account, loc, fam)


@router.callback_query(F.data.startswith("admin:hzfamoff:"))
async def cb_hz_family_all_off(cb: CallbackQuery, session: AsyncSession):
    parts = cb.data.split(":")
    account = await _get_hz_account(cb, session, int(parts[2]))
    if not account:
        return
    loc, fam = parts[3], parts[4]
    imported = await _imported_map(session, account, loc)
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
    await _render_family(cb.message, session, account, loc, fam)


@router.callback_query(F.data.startswith("admin:hzfam:"))
async def cb_hz_family(cb: CallbackQuery, session: AsyncSession):
    parts = cb.data.split(":")
    account = await _get_hz_account(cb, session, int(parts[2]))
    if not account:
        return
    await cb.answer()
    await _render_family(cb.message, session, account, parts[3], parts[4])


@router.callback_query(F.data.startswith("admin:hzinfo:"))
async def cb_hz_info(cb: CallbackQuery, session: AsyncSession):
    parts = cb.data.split(":")
    account = await _get_hz_account(cb, session, int(parts[2]))
    if not account:
        return
    loc, pid = parts[3], parts[4]
    plans = await _location_plans(account, loc)
    info = next((p for p in plans if p.provider_plan_id == pid), None)
    if not info:
        await cb.answer("پلن یافت نشد.", show_alert=True)
        return
    await cb.answer(
        f"{pid.upper()} — {info.cpu} vCPU / {info.ram // 1024} GB RAM / {info.disk} GB Disk\n"
        f"Hourly cost: €{info.price_hourly:g}\n"
        f"Monthly cost: €{info.price_monthly:g}\n"
        f"Traffic: {info.bandwidth:,} GB\n"
        "(EUR incl. VAT)",
        show_alert=True,
    )


async def _import_one(session: AsyncSession, account: ProviderAccount,
                      loc: str, info, group_name: str) -> ServerPlan:
    plan = ServerPlan(
        provider_type=ProviderType.HETZNER,
        provider_account_id=account.id,
        name=f"{info.provider_plan_id}-{loc}",
        display_name=f"{info.provider_plan_id.upper()} — {loc}",
        ram=info.ram, cpu=info.cpu, disk=info.disk, bandwidth=info.bandwidth,
        price_hourly=None, price_monthly=None,   # قیمت فروش را ادمین تعیین می‌کند
        location=loc,
        is_active=False,                          # تا قیمت‌گذاری، در فروش دیده نمی‌شود
        category=group_name,
        provider_plan_id=info.provider_plan_id,
        extra_data={
            "currency": "eur",
            "cost_hourly": info.price_hourly,     # قیمت خرید (EUR gross)
            "cost_monthly": info.price_monthly,
        },
    )
    session.add(plan)
    return plan


async def _remove_plan(session: AsyncSession, plan: ServerPlan) -> tuple[bool, str]:
    """حذف پلن ایمپورت‌شده؛ اگر سرور فعال مشتری دارد فقط غیرفعال می‌شود."""
    servers = (await session.execute(
        select(Server).where(
            Server.provider_account_id == plan.provider_account_id,
            Server.status != ServerStatus.DELETED,
        )
    )).scalars().all()
    in_use = any((s.extra_data or {}).get("plan_id") == plan.id for s in servers)
    if in_use:
        plan.is_active = False
        return False, "غیرفعال شد (سرور فعال دارد)"
    await session.delete(plan)
    return True, "حذف شد"


@router.callback_query(F.data.startswith("admin:hzpick:"))
async def cb_hz_pick(cb: CallbackQuery, session: AsyncSession):
    parts = cb.data.split(":")
    account = await _get_hz_account(cb, session, int(parts[2]))
    if not account:
        return
    loc, pid = parts[3], parts[4]

    existing = (await session.execute(
        select(ServerPlan).where(
            ServerPlan.provider_account_id == account.id,
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
            await cb.answer("پلن در این لوکیشن موجود نیست.", show_alert=True)
            return
        group_name = await _import_group_name(session, account)
        await _import_one(session, account, loc, info, group_name)
        await session.flush()
        await cb.answer(f"✅ {pid} به گروه «{group_name}» اضافه شد — قیمت فروش را تعیین کنید.")
    await _render_family(cb.message, session, account, loc, _family(pid))
