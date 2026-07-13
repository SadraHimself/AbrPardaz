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
    ProviderAccount, ProviderType, Server, ServerPlan, ServerStatus, User,
)
from bot.keyboards.admin import back_to_admin_kb, cancel_admin_kb
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

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="تست اتصال", callback_data=f"admin:hz_test:{account.id}"),
         InlineKeyboardButton(text="ایمپورت محصولات", callback_data=f"admin:hz_import:{account.id}")],
        [InlineKeyboardButton(text="ویرایش نام", callback_data=f"admin:hz_edit:{account.id}:name"),
         InlineKeyboardButton(text="ویرایش توکن", callback_data=f"admin:hz_edit:{account.id}:token")],
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
        f"محصولات ایمپورت‌شده: {plans_count}\n"
        f"سرورهای فعال مشتری: {servers_count}\n"
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

@router.callback_query(F.data.startswith("admin:hz_import:"))
async def cb_hz_import(cb: CallbackQuery, session: AsyncSession):
    account = await _get_hz_account(cb, session, int(cb.data.split(":")[2]))
    if not account:
        return
    await cb.answer("در حال دریافت لوکیشن‌ها...")
    prov = HetznerProvider(api_token=account.api_key or "")
    try:
        locs = await asyncio.wait_for(prov.list_locations(), timeout=20)
    except Exception as e:
        await cb.message.answer(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"خطا در دریافت لوکیشن‌ها: <code>{str(e)[:200]}</code>",
            parse_mode="HTML",
        )
        return
    rows = [[InlineKeyboardButton(
        text=f"{l['city']} ({l['name']}) — {l['country']}",
        callback_data=f"admin:hzloc:{account.id}:{l['name']}",
    )] for l in locs]
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data=f"admin:hz:{account.id}")])
    await cb.message.edit_text(
        "<b>ایمپورت محصولات هتزنر</b>\n\nلوکیشن را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _render_hz_plans(msg, session: AsyncSession, account: ProviderAccount, loc: str):
    prov = HetznerProvider(api_token=account.api_key or "")
    plans = await asyncio.wait_for(prov.list_plans(location=loc), timeout=30)

    # کدام‌ها قبلاً ایمپورت شده‌اند؟
    existing = (await session.execute(
        select(ServerPlan.provider_plan_id).where(
            ServerPlan.provider_account_id == account.id,
            ServerPlan.location == loc,
        )
    )).scalars().all()
    imported = set(existing)

    rows = []
    for p in sorted(plans, key=lambda x: (x.price_monthly or 0)):
        mark = "✅" if p.provider_plan_id in imported else "⬜"
        rows.append([InlineKeyboardButton(
            text=(f"{mark} {p.provider_plan_id} · {p.cpu}c/{p.ram // 1024}G/{p.disk}G "
                  f"· €{p.price_monthly:g}/ماه"),
            callback_data=f"admin:hzpick:{account.id}:{loc}:{p.provider_plan_id}",
        )])
    rows.append([InlineKeyboardButton(text="بازگشت", callback_data=f"admin:hz_import:{account.id}")])
    await msg.edit_text(
        f"<b>پلن‌های هتزنر — {loc}</b>\n\n"
        "قیمت‌ها «قیمت خرید» (EUR با VAT) هستند — با تپ روی هر پلن، به محصولات اضافه می‌شود.\n"
        "محصول ایمپورت‌شده غیرفعال است تا قیمت فروش را تعیین کنید.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("admin:hzloc:"))
async def cb_hz_location(cb: CallbackQuery, session: AsyncSession):
    parts = cb.data.split(":")
    account = await _get_hz_account(cb, session, int(parts[2]))
    if not account:
        return
    await cb.answer("در حال دریافت پلن‌ها و قیمت‌ها...")
    try:
        await _render_hz_plans(cb.message, session, account, parts[3])
    except Exception as e:
        await cb.message.answer(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> '
            f"خطا در دریافت پلن‌ها: <code>{str(e)[:200]}</code>",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("admin:hzpick:"))
async def cb_hz_pick(cb: CallbackQuery, session: AsyncSession):
    parts = cb.data.split(":")
    account = await _get_hz_account(cb, session, int(parts[2]))
    if not account:
        return
    loc, ptype = parts[3], parts[4]

    dup = (await session.execute(
        select(ServerPlan).where(
            ServerPlan.provider_account_id == account.id,
            ServerPlan.provider_plan_id == ptype,
            ServerPlan.location == loc,
        )
    )).scalar_one_or_none()
    if dup:
        await cb.answer("این پلن قبلاً ایمپورت شده.", show_alert=True)
        return

    prov = HetznerProvider(api_token=account.api_key or "")
    try:
        plans = await asyncio.wait_for(prov.list_plans(location=loc), timeout=30)
    except Exception as e:
        await cb.answer(f"خطا: {str(e)[:150]}", show_alert=True)
        return
    info = next((p for p in plans if p.provider_plan_id == ptype), None)
    if not info:
        await cb.answer("پلن در این لوکیشن موجود نیست.", show_alert=True)
        return

    plan = ServerPlan(
        provider_type=ProviderType.HETZNER,
        provider_account_id=account.id,
        name=f"{ptype}-{loc}",
        display_name=f"{ptype.upper()} — {loc}",
        ram=info.ram, cpu=info.cpu, disk=info.disk, bandwidth=info.bandwidth,
        price_hourly=None, price_monthly=None,   # قیمت فروش را ادمین تعیین می‌کند
        location=loc,
        is_active=False,                          # تا قیمت‌گذاری، در فروش دیده نمی‌شود
        category=None,
        provider_plan_id=ptype,
        extra_data={
            "currency": "eur",
            "cost_hourly": info.price_hourly,     # قیمت خرید (EUR gross)
            "cost_monthly": info.price_monthly,
        },
    )
    session.add(plan)
    await session.flush()
    await cb.answer(f"✅ {ptype} اضافه شد — قیمت فروش را در «محصولات» تعیین کنید.", show_alert=True)
    await _render_hz_plans(cb.message, session, account, loc)
