"""Admin panel — providers, plans, discounts, sub-products management."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import (
    DiscountCode, ProductGroup, ProviderAccount, ProviderType,
    Server, ServerPlan, ServerStatus, SubProduct, SubProductType,
    Transaction, TransactionType, User, plan_sort_key,
)
from bot.keyboards.admin import (
    admin_menu_kb, back_to_admin_kb, billing_type_admin_kb,
    cancel_admin_kb, confirm_kb, discount_detail_kb,
    discounts_list_kb, group_detail_kb, group_pick_kb, groups_list_kb,
    plan_detail_kb, plans_groups_kb, plans_in_group_kb, plans_menu_kb,
    provider_detail_kb, provider_types_kb, providers_list_kb,
    providers_select_kb, skip_or_cancel_kb,
    subprod_detail_kb, subprod_type_kb, subproducts_kb,
)
from bot.providers import get_provider
from bot.providers.virtualizor import VirtualizorProvider
import html as _html

from bot.utils.loading import ERR, answer_loading, edit_loading
from bot.services.billing import BillingService
from bot.services.currency import CURRENCY_LABELS, fmt_price, obj_currency

# کیبورد انتخاب واحد قیمت محصول
def _currency_pick_kb():
    from aiogram.types import InlineKeyboardButton as _Btn, InlineKeyboardMarkup as _Mk
    return _Mk(inline_keyboard=[
        [_Btn(text="ریالی (تومان)", callback_data="admin:plancur:irt")],
        [_Btn(text="دلاری ($)", callback_data="admin:plancur:usd")],
        [_Btn(text="یورویی (€)", callback_data="admin:plancur:eur")],
        [_Btn(text="انصراف", callback_data="admin_panel")],
    ])

router = Router(name="admin")


class AdminFilter(Filter):
    # user برای آپدیت‌های گروهی (سرویس‌پیام‌ها/تاپیک‌ها) ست نمی‌شود — پیش‌فرض None
    # تا فیلتر به‌جای TypeError، فقط False برگرداند
    async def __call__(self, event: Message | CallbackQuery, user: User | None = None) -> bool:
        if user is None:
            return False
        return user.is_admin or (user.telegram_id in settings.admin_ids)


router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())


# ── FSM States ────────────────────────────────────────────────────────────────

class ProviderFSM(StatesGroup):
    add_name = State()
    add_url = State()
    add_key = State()
    add_pass = State()
    edit_value = State()


class PlanFSM(StatesGroup):
    add_category = State()        # group selection (callback-based)
    add_group_name = State()      # inline creation of a new group during plan add
    add_group_emoji = State()
    add_name = State()
    add_display_name = State()
    add_provider = State()
    add_plan_id = State()
    confirm_autofetch = State()
    add_ram = State()
    add_cpu = State()
    add_disk = State()
    add_bandwidth = State()
    add_billing = State()
    add_currency = State()   # واحد قیمت: ریالی / دلاری / یورویی
    add_price_hourly = State()
    add_price_monthly = State()
    add_location = State()
    add_emoji = State()             # اموجی پریمیوم محصول (مثل گروه — قابل رد کردن)
    edit_value = State()
    edit_emoji = State()            # ویرایش اموجی محصول
    edit_price_currency = State()   # انتخاب واحد ارز هنگام ویرایش قیمت
    edit_plan_id = State()          # ورود Plan ID جدید → fetch از ویرچولایزور
    edit_plan_id_confirm = State()  # تأیید مشخصات پلن خوانده‌شده


class GroupFSM(StatesGroup):
    add_name = State()
    add_emoji = State()
    edit_name = State()
    edit_emoji = State()


class DiscountFSM(StatesGroup):
    add_code = State()
    add_percent = State()
    add_expires = State()
    add_max_uses = State()
    edit_value = State()


class SubProductFSM(StatesGroup):
    select_type = State()
    add_name = State()
    add_price = State()
    add_value = State()
    edit_value = State()


# ── Main panel ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin_panel")
async def cb_admin_panel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_loading(cb.message)
    await cb.answer()
    await cb.message.edit_text(
        "<b>پنل ادمین</b>\n\nیک بخش را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=admin_menu_kb(),
    )


@router.message(F.text == "پنل ادمین")
async def msg_admin_panel(message: Message, state: FSMContext):
    await state.clear()
    loading = await answer_loading(message)
    await loading.edit_text(
        "<b>پنل ادمین</b>\n\nیک بخش را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=admin_menu_kb(),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  VIRTUALIZOR PROVIDER MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "admin:providers")
async def cb_admin_providers(cb: CallbackQuery, session: AsyncSession):
    result = await session.execute(
        select(ProviderAccount)
        .where(ProviderAccount.provider_type == ProviderType.VIRTUALIZOR)
        .order_by(ProviderAccount.name)
    )
    providers = list(result.scalars().all())
    count = len(providers)
    text = (
        f"<b>سرورهای ویرچولایزور</b>\n\nتعداد: {count} سرور\nیک سرور انتخاب کنید:"
        if count else
        "<b>سرورهای ویرچولایزور</b>\n\nهیچ سروری اضافه نشده."
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=providers_list_kb(providers))
    await cb.answer()


@router.callback_query(F.data == "admin:prov_add")
async def cb_prov_add_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ProviderFSM.add_name)
    await cb.message.edit_text(
        "<b>اضافه کردن سرور ویرچولایزور</b>\n\n"
        "مرحله ۱/۴ — نام سرور:\n<i>مثال: ایران DC1</i>",
        parse_mode="HTML",
        reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(ProviderFSM.add_name)
async def prov_add_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(ProviderFSM.add_url)
    await message.answer(
        "مرحله ۲/۴ — آدرس پنل:\n<i>مثال: https://nl.example.com:4085</i>",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )


@router.message(ProviderFSM.add_url)
async def prov_add_url(message: Message, state: FSMContext):
    await state.update_data(url=message.text.strip().rstrip("/"))
    await state.set_state(ProviderFSM.add_key)
    await message.answer("مرحله ۳/۴ — API Key:", reply_markup=cancel_admin_kb())


@router.message(ProviderFSM.add_key)
async def prov_add_key(message: Message, state: FSMContext):
    await state.update_data(api_key=message.text.strip())
    await state.set_state(ProviderFSM.add_pass)
    await message.answer("مرحله ۴/۴ — API Pass:", reply_markup=cancel_admin_kb())


@router.message(ProviderFSM.add_pass)
async def prov_add_pass(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()

    account = ProviderAccount(
        provider_type=ProviderType.VIRTUALIZOR,
        name=data["name"],
        api_key=data["api_key"],
        api_secret=message.text.strip(),
        api_endpoint=data["url"],
        is_active=True,
    )
    session.add(account)
    await session.flush()

    test_msg = await message.answer("در حال تست اتصال...")
    try:
        prov = VirtualizorProvider(data["url"], data["api_key"], account.api_secret)
        plans = await asyncio.wait_for(prov.list_plans(), timeout=10)
        status = f"✅ اتصال موفق — {len(plans)} پلن یافت شد"
    except asyncio.TimeoutError:
        status = "❌ تایم‌اوت"
    except Exception as e:
        status = f"❌ {str(e)[:80]}"

    await test_msg.delete()
    await message.answer(
        f"<b>سرور اضافه شد!</b>\n\n{account.name}\n{account.api_endpoint}\n{status}",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb("admin:providers"),
    )


async def _render_provider_detail(message, account, provider_id: int):
    """Shared helper — renders provider detail message without touching cb.answer()."""
    extra = account.extra_config or {}
    change_ip_fee = float(extra.get("change_ip_fee", 0) or 0)
    extra_ip_fee = float(extra.get("extra_ip_fee", 0) or 0)
    fee_text = f"{change_ip_fee:,.0f} تومان" if change_ip_fee else "رایگان"
    extra_fee_text = f"{extra_ip_fee:,.0f} تومان" if extra_ip_fee else "رایگان"
    await message.edit_text(
        f"<b>{account.name}</b>\n\n"
        f"URL: <code>{account.api_endpoint}</code>\n"
        f"API Key: <code>{account.api_key}</code>\n"
        f"API Pass: <code>{'*' * 8}</code>\n"
        f"وضعیت: {'✅ فعال' if account.is_active else '❌ غیرفعال'}\n"
        f"Strict KYC: {'روشن' if account.strict_kyc else 'خاموش'}\n"
        f"هزینه تغییر IP: {fee_text}\n"
        f"هزینه IP اضافه: {extra_fee_text}\n"
        f"ID: {account.id}",
        parse_mode="HTML",
        reply_markup=provider_detail_kb(provider_id, account.is_active, account.strict_kyc,
                                        change_ip_fee, extra_ip_fee),
    )


@router.callback_query(F.data.startswith("admin:prov:"))
async def cb_prov_detail(cb: CallbackQuery, session: AsyncSession):
    provider_id = int(cb.data.split(":")[2])
    account = await session.get(ProviderAccount, provider_id)
    if not account:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    await _render_provider_detail(cb.message, account, provider_id)
    await cb.answer()


@router.callback_query(F.data.startswith("admin:prov_toggle:"))
async def cb_prov_toggle(cb: CallbackQuery, session: AsyncSession):
    provider_id = int(cb.data.split(":")[2])
    account = await session.get(ProviderAccount, provider_id)
    if not account:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    account.is_active = not account.is_active
    await session.flush()
    await cb.answer(f"{'فعال' if account.is_active else 'غیرفعال'} شد.")
    await _render_provider_detail(cb.message, account, provider_id)


@router.callback_query(F.data.startswith("admin:prov_kyc:"))
async def cb_prov_kyc_toggle(cb: CallbackQuery, session: AsyncSession):
    provider_id = int(cb.data.split(":")[2])
    account = await session.get(ProviderAccount, provider_id)
    if not account:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    account.strict_kyc = not account.strict_kyc
    await session.flush()
    await cb.answer(f"Strict KYC {'روشن' if account.strict_kyc else 'خاموش'} شد.")
    await _render_provider_detail(cb.message, account, provider_id)


@router.callback_query(F.data.startswith("admin:prov_edit:"))
async def cb_prov_edit_start(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    provider_id, field = int(parts[2]), parts[3]
    labels = {
        "name": "نام سرور", "url": "آدرس پنل",
        "api_key": "API Key", "api_pass": "API Pass",
        "change_ip_fee": "هزینه تغییر IP (تومان، 0 = رایگان)",
        "extra_ip_fee": "هزینه IP اضافه (تومان، 0 = رایگان)",
    }
    await state.update_data(edit_provider_id=provider_id, edit_field=field)
    await state.set_state(ProviderFSM.edit_value)
    await cb.message.edit_text(
        f"<b>ویرایش {labels.get(field, field)}</b>\n\nمقدار جدید:",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(ProviderFSM.edit_value)
async def prov_edit_value(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    account = await session.get(ProviderAccount, data["edit_provider_id"])
    if not account:
        await message.answer("سرور یافت نشد.")
        return
    field, value = data["edit_field"], message.text.strip()
    if field == "name":
        account.name = value
    elif field == "url":
        account.api_endpoint = value.rstrip("/")
    elif field == "api_key":
        account.api_key = value
    elif field == "api_pass":
        account.api_secret = value
    elif field in ("change_ip_fee", "extra_ip_fee"):
        extra = dict(account.extra_config or {})
        try:
            extra[field] = float(value)
        except ValueError:
            await message.answer("مقدار باید عدد باشد.")
            return
        account.extra_config = extra
    await session.flush()
    extra = account.extra_config or {}
    change_ip_fee = float(extra.get("change_ip_fee", 0) or 0)
    extra_ip_fee = float(extra.get("extra_ip_fee", 0) or 0)
    await message.answer("تغییر ذخیره شد.", reply_markup=provider_detail_kb(
        data["edit_provider_id"], account.is_active, account.strict_kyc, change_ip_fee, extra_ip_fee))


@router.callback_query(F.data.startswith("admin:prov_test:"))
async def cb_prov_test(cb: CallbackQuery, session: AsyncSession):
    provider_id = int(cb.data.split(":")[2])
    account = await session.get(ProviderAccount, provider_id)
    if not account:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    await cb.answer()
    test_msg = await cb.message.answer("در حال تست اتصال...")
    try:
        prov = VirtualizorProvider(account.api_endpoint, account.api_key, account.api_secret)
        plans = await asyncio.wait_for(prov.list_plans(), timeout=15)
        # Fetch nodes and IPs for diagnostics
        extra_info = ""
        try:
            nodes = await asyncio.wait_for(prov.list_nodes(), timeout=10)
            if nodes:
                node_lines = []
                for n in nodes[:5]:
                    status_icon = "✅" if n["online"] else "❌"
                    line = f"  {status_icon} <b>{n['name'] or 'بدون نام'}</b> | IP: <code>{n['ip']}</code>"
                    if n.get("os"):
                        line += f"\n     OS: <code>{n['os']}</code>"
                    if n.get("cpu"):
                        line += f"\n     CPU: <code>{str(n['cpu'])[:50]}</code>"
                    if n.get("cpu_load"):
                        line += f"  Load: <code>{n['cpu_load']}</code>"
                    if n.get("ram_total_mb"):
                        used = n.get("ram_used_mb", 0)
                        total = n["ram_total_mb"]
                        line += f"\n     RAM: <code>{used:.0f}/{total:.0f} MB</code>"
                    node_lines.append(line)
                extra_info += "\n\n<b>سرورهای Virtualizor:</b>\n" + "\n".join(node_lines)
            else:
                extra_info += "\n\n<b>سرورها:</b> هیچ سروری یافت نشد"
        except Exception as e:
            extra_info += f"\n\n<b>سرورها — خطا:</b> <code>{e}</code>"
        try:
            storages = await asyncio.wait_for(prov.list_storages(), timeout=8)
            if storages:
                st_lines = [f"  {s['name']} ({s['free_gb']:.0f}GB آزاد) {'✅' if s['is_primary'] else ''}" for s in storages[:3]]
                extra_info += "\n\n<b>استوریج‌ها:</b>\n" + "\n".join(st_lines)
            else:
                extra_info += "\n\n<b>استوریج‌ها:</b> یافت نشد"
        except Exception as e:
            extra_info += f"\n\n<b>استوریج‌ها — خطا:</b> <code>{e}</code>"
        await test_msg.edit_text(
            f"✅ <b>اتصال موفق!</b>\n{account.name}\n{len(plans)} پلن{extra_info}",
            parse_mode="HTML",
        )
    except asyncio.TimeoutError:
        await test_msg.edit_text(f"{ERR} <b>تایم‌اوت</b>\n{account.api_endpoint}", parse_mode="HTML")
    except Exception as e:
        await test_msg.edit_text(f"{ERR} <b>خطا:</b> {_html.escape(str(e))}", parse_mode="HTML")


@router.callback_query(F.data.startswith("admin:prov_monitor:"))
async def cb_prov_monitor(cb: CallbackQuery, session: AsyncSession):
    provider_id = int(cb.data.split(":")[2])
    account = await session.get(ProviderAccount, provider_id)
    if not account:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    await cb.answer()
    mon_msg = await cb.message.answer("در حال خواندن اطلاعات سرور...")
    prov = VirtualizorProvider(account.api_endpoint, account.api_key, account.api_secret)
    try:
        nodes = await asyncio.wait_for(prov.list_nodes(), timeout=15)
        if not nodes:
            await mon_msg.edit_text("هیچ سروری یافت نشد.\n\nاحتمالاً API key دسترسی <code>act=servers</code> ندارد.\nVirtualizor Configuration Admin API ویرایش کلید فعال کردن همه دسترسی‌ها", parse_mode="HTML")
            return
        lines = []
        for n in nodes:
            status = "✅ آنلاین" if n["online"] else "❌ آفلاین"
            block = (
                f"━━━━━━━━━━━━━━━━\n"
                f"<b>{n['name'] or 'بدون نام'}</b>  {status}\n"
                f"IP: <code>{n['ip']}</code>\n"
            )
            if n.get("os"):
                block += f"OS: <code>{n['os']}</code>\n"
            if n.get("cpu"):
                block += f"CPU: <code>{str(n['cpu'])[:60]}</code>\n"
            if n.get("cpu_load"):
                block += f"Load: <code>{n['cpu_load']}</code>\n"
            if n.get("ram_total_mb"):
                used = n.get("ram_used_mb", 0)
                total = n["ram_total_mb"]
                pct = (used / total * 100) if total else 0
                block += f"RAM: <code>{used:.0f} / {total:.0f} MB  ({pct:.0f}%)</code>\n"
            if n.get("hdd"):
                block += f"HDD: <code>{str(n['hdd'])[:60]}</code>\n"
            if n.get("virt_type"):
                block += f"Virt: <code>{n['virt_type']}</code>\n"
            block += f"serid: <code>{n['serid']}</code>"
            lines.append(block)
        text = f"<b>مانیتور — {account.name}</b>\n\n" + "\n\n".join(lines)
        await mon_msg.edit_text(text, parse_mode="HTML")
    except asyncio.TimeoutError:
        await mon_msg.edit_text("تایم‌اوت هنگام خواندن اطلاعات سرور.")
    except Exception as e:
        await mon_msg.edit_text(
            f"<b>خطا:</b> <code>{e}</code>\n\n"
            "اگر خطا «access privileges» است:\n"
            "Virtualizor Configuration Admin API ویرایش کلید فعال کردن همه دسترسی‌ها",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("admin:prov_del:"))
async def cb_prov_del_confirm(cb: CallbackQuery, session: AsyncSession):
    provider_id = int(cb.data.split(":")[2])
    account = await session.get(ProviderAccount, provider_id)
    if not account:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    await cb.message.edit_text(
        f"حذف سرور <b>{account.name}</b>؟",
        parse_mode="HTML",
        reply_markup=confirm_kb(f"admin:prov_del_do:{provider_id}", "admin:providers"),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("admin:prov_del_do:"))
async def cb_prov_del_do(cb: CallbackQuery, session: AsyncSession):
    from sqlalchemy import delete as sql_delete
    provider_id = int(cb.data.split(":")[2])
    account = await session.get(ProviderAccount, provider_id)
    removed_vms = 0
    if account:
        # Clean up customers' active VMs still on this provider (force — the node
        # may be down / being decommissioned). Keep the Server rows for history but
        # mark them DELETED and unlink the FK so the account row can be removed.
        result = await session.execute(
            select(Server).where(
                Server.provider_account_id == provider_id,
                Server.status != ServerStatus.DELETED,
            )
        )
        for srv in result.scalars().all():
            if srv.provider_server_id:
                try:
                    await get_provider(account).delete_server(srv.provider_server_id)
                except Exception:
                    pass  # force clean regardless of node state
            srv.status = ServerStatus.DELETED
            srv.provider_account_id = None
            removed_vms += 1

        # Delete associated plans, then the account
        await session.execute(
            sql_delete(ServerPlan).where(ServerPlan.provider_account_id == provider_id)
        )
        await session.delete(account)
        await session.flush()

    msg = "سرور و محصولات مربوطه حذف شدند."
    if removed_vms:
        msg += f"\n{removed_vms} سرویس فعال مشتریان هم حذف شد."
    await cb.message.edit_text(msg, reply_markup=back_to_admin_kb("admin:providers"))
    await cb.answer()


# ══════════════════════════════════════════════════════════════════════════════
#  PLAN / PRODUCT MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

async def _adopt_groups(session: AsyncSession) -> list[ProductGroup]:
    """Ensure every existing plan category has a ProductGroup row (adopts legacy
    string categories), then return all groups sorted by name."""
    res = await session.execute(select(ServerPlan.category).distinct())
    cats = {row[0] for row in res.all() if row[0]}
    res = await session.execute(select(ProductGroup))
    groups = {g.name: g for g in res.scalars().all()}
    created = False
    for c in sorted(cats - set(groups)):
        g = ProductGroup(name=c)
        session.add(g)
        groups[c] = g
        created = True
    if created:
        await session.flush()
    return sorted(groups.values(), key=lambda g: g.name)


@router.callback_query(F.data == "admin:plans")
async def cb_admin_plans(cb: CallbackQuery, session: AsyncSession):
    await cb.answer()
    total = (await session.execute(select(func.count(ServerPlan.id)))).scalar() or 0
    await cb.message.edit_text(
        f"<b>محصولات</b>\n\n{total} محصول — یک بخش را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=plans_menu_kb(),
    )


@router.callback_query(F.data == "admin:provtypes")
async def cb_admin_provtypes(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(
        "<b>سرویس‌دهنده‌ها</b>\n\nسرویس‌دهنده مورد نظر را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=provider_types_kb(),
    )


@router.callback_query(F.data == "admin:plans_list")
async def cb_admin_plans_list(cb: CallbackQuery, session: AsyncSession):
    await cb.answer()
    groups = await _adopt_groups(session)
    entries = [(str(g.id), f"{'❌ ' if g.is_hidden else ''}{g.name}") for g in groups]
    orphan_count = (await session.execute(
        select(func.count(ServerPlan.id)).where(ServerPlan.category.is_(None))
    )).scalar() or 0
    if orphan_count:
        entries.append(("none", f"بدون گروه ({orphan_count})"))
    text = (
        "<b>محصولات</b>\n\nگروه مورد نظر را انتخاب کنید:"
        if entries else
        "<b>محصولات</b>\n\nهیچ محصولی اضافه نشده."
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=plans_groups_kb(entries))


@router.callback_query(F.data.startswith("admin:plans_grp:"))
async def cb_admin_plans_grp(cb: CallbackQuery, session: AsyncSession):
    key = cb.data.split(":")[2]
    if key == "none":
        cond, title = ServerPlan.category.is_(None), "بدون گروه"
    else:
        group = await session.get(ProductGroup, int(key))
        if not group:
            await cb.answer("گروه یافت نشد.", show_alert=True)
            return
        cond, title = ServerPlan.category == group.name, group.name
    await cb.answer()
    result = await session.execute(select(ServerPlan).where(cond))
    plans = sorted(result.scalars().all(), key=plan_sort_key)
    await cb.message.edit_text(
        f"<b>{title}</b>\n\n{len(plans)} محصول:",
        parse_mode="HTML",
        reply_markup=plans_in_group_kb(plans, key),
    )


# ─── Manual product ordering (ترتیب نمایش) ────────────────────────────────────

async def _sorted_group_plans(session: AsyncSession, key: str):
    """محصولات یک گروه به ترتیب نمایش. None یعنی گروه نامعتبر."""
    if key == "none":
        cond = ServerPlan.category.is_(None)
    else:
        group = await session.get(ProductGroup, int(key))
        if not group:
            return None
        cond = ServerPlan.category == group.name
    result = await session.execute(select(ServerPlan).where(cond))
    return sorted(result.scalars().all(), key=plan_sort_key)


async def _render_sort_view(msg, session: AsyncSession, key: str):
    from aiogram.types import InlineKeyboardButton as _Btn, InlineKeyboardMarkup as _Mk
    plans = await _sorted_group_plans(session, key)
    if plans is None:
        await msg.edit_text("گروه یافت نشد.")
        return
    rows = []
    for p in plans:
        rows.append([
            _Btn(text="🔼", callback_data=f"admin:psort:{key}:{p.id}:up"),
            _Btn(text="🔽", callback_data=f"admin:psort:{key}:{p.id}:down"),
            _Btn(text=p.display_name or p.name, callback_data=f"admin:plan:{p.id}"),
        ])
    rows.append([_Btn(text="بازگشت", callback_data=f"admin:plans_grp:{key}")])
    await msg.edit_text(
        "<b>ترتیب نمایش محصولات</b>\n\n"
        "با 🔼 و 🔽 جای هر محصول را عوض کنید — همین ترتیب در فلوی خرید هم اعمال می‌شود.",
        parse_mode="HTML",
        reply_markup=_Mk(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("admin:plans_sort:"))
async def cb_plans_sort(cb: CallbackQuery, session: AsyncSession):
    await cb.answer()
    await _render_sort_view(cb.message, session, cb.data.split(":")[2])


@router.callback_query(F.data.startswith("admin:psort:"))
async def cb_plan_sort_move(cb: CallbackQuery, session: AsyncSession):
    _, _, key, pid, direction = cb.data.split(":")
    plans = await _sorted_group_plans(session, key)
    if not plans:
        await cb.answer("گروه یافت نشد.", show_alert=True)
        return
    idx = next((i for i, p in enumerate(plans) if p.id == int(pid)), None)
    if idx is None:
        await cb.answer("محصول یافت نشد.", show_alert=True)
        return
    j = idx - 1 if direction == "up" else idx + 1
    if j < 0 or j >= len(plans):
        await cb.answer("ابتدای/انتهای لیست است.")
        return
    plans[idx], plans[j] = plans[j], plans[idx]
    # نرمال‌سازی: ترتیب فعلیِ لیست عیناً ذخیره می‌شود
    for i, p in enumerate(plans):
        extra = dict(p.extra_data or {})
        extra["sort"] = i
        p.extra_data = extra
    await session.flush()
    await cb.answer()
    await _render_sort_view(cb.message, session, key)


# ─── Product groups (گروه محصولات) ────────────────────────────────────────────

async def _render_group_detail(msg, session: AsyncSession, group: ProductGroup, edit: bool = True):
    count = (await session.execute(
        select(func.count(ServerPlan.id)).where(ServerPlan.category == group.name)
    )).scalar() or 0
    emoji_text = f"<code>{group.emoji_id}</code>" if group.emoji_id else "ندارد"
    text = (
        f"<b>{group.name}</b>\n\n"
        f"اموجی: {emoji_text}\n"
        f"وضعیت: {'❌ مخفی' if group.is_hidden else '✅ نمایان'}\n"
        f"تعداد محصولات: {count}"
    )
    kb = group_detail_kb(group.id, group.is_hidden)
    if edit:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "admin:groups")
async def cb_admin_groups(cb: CallbackQuery, session: AsyncSession):
    await cb.answer()
    groups = await _adopt_groups(session)
    text = (
        f"<b>گروه محصولات</b>\n\n{len(groups)} گروه:"
        if groups else
        "<b>گروه محصولات</b>\n\nهنوز گروهی ساخته نشده."
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=groups_list_kb(groups))


@router.callback_query(F.data.startswith("admin:group:"))
async def cb_group_detail(cb: CallbackQuery, session: AsyncSession):
    group = await session.get(ProductGroup, int(cb.data.split(":")[2]))
    if not group:
        await cb.answer("گروه یافت نشد.", show_alert=True)
        return
    await cb.answer()
    await _render_group_detail(cb.message, session, group)


@router.callback_query(F.data == "admin:group_add")
async def cb_group_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(GroupFSM.add_name)
    await cb.message.edit_text(
        "<b>گروه جدید</b>\n\nنام گروه محصول را وارد کنید:",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(GroupFSM.add_name)
async def group_add_name(message: Message, state: FSMContext, session: AsyncSession):
    name = (message.text or "").strip()
    if not name or len(name) > 60:
        await message.answer("نام معتبر نیست (حداکثر ۶۰ کاراکتر). دوباره وارد کنید:")
        return
    existing = (await session.execute(
        select(ProductGroup).where(ProductGroup.name == name)
    )).scalar_one_or_none()
    if existing:
        await message.answer("گروهی با این نام از قبل وجود دارد.", reply_markup=back_to_admin_kb("admin:groups"))
        await state.clear()
        return
    await state.update_data(group_name=name)
    await state.set_state(GroupFSM.add_emoji)
    await message.answer(
        "هش اموجی مخصوص گروه محصول را وارد کنید:\n"
        "<i>مثال: 5258503720928288433</i>",
        parse_mode="HTML", reply_markup=skip_or_cancel_kb(),
    )


async def _finish_group_add(msg, state: FSMContext, session: AsyncSession, emoji_id: str | None):
    data = await state.get_data()
    await state.clear()
    group = ProductGroup(name=data["group_name"], emoji_id=emoji_id)
    session.add(group)
    await session.flush()
    await _render_group_detail(msg, session, group, edit=False)


@router.message(GroupFSM.add_emoji)
async def group_add_emoji(message: Message, state: FSMContext, session: AsyncSession):
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("هش اموجی باید فقط عدد باشد. دوباره وارد کنید یا «رد کردن» را بزنید:")
        return
    await _finish_group_add(message, state, session, raw)


@router.callback_query(GroupFSM.add_emoji, F.data == "admin:skip")
async def group_add_emoji_skip(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    await cb.answer()
    await _finish_group_add(cb.message, state, session, None)


@router.callback_query(F.data.startswith("admin:group_edit:"))
async def cb_group_edit(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    gid, field = int(parts[2]), parts[3]
    await state.update_data(group_id=gid)
    if field == "name":
        await state.set_state(GroupFSM.edit_name)
        await cb.message.edit_text(
            "<b>ویرایش نام گروه</b>\n\nنام جدید را وارد کنید:\n"
            "<i>دسته‌بندی همه محصولات این گروه هم به‌روز می‌شود.</i>",
            parse_mode="HTML", reply_markup=cancel_admin_kb(),
        )
    else:
        await state.set_state(GroupFSM.edit_emoji)
        await cb.message.edit_text(
            "<b>ویرایش اموجی گروه</b>\n\n"
            "هش اموجی جدید را وارد کنید:\n"
            "<i>مثال: 5258503720928288433</i>\n\n"
            "برای حذف اموجی، «رد کردن» را بزنید.",
            parse_mode="HTML", reply_markup=skip_or_cancel_kb(),
        )
    await cb.answer()


@router.message(GroupFSM.edit_name)
async def group_edit_name(message: Message, state: FSMContext, session: AsyncSession):
    new_name = (message.text or "").strip()
    if not new_name or len(new_name) > 60:
        await message.answer("نام معتبر نیست. دوباره وارد کنید:")
        return
    data = await state.get_data()
    await state.clear()
    group = await session.get(ProductGroup, data["group_id"])
    if not group:
        await message.answer("گروه یافت نشد.")
        return
    dup = (await session.execute(
        select(ProductGroup).where(ProductGroup.name == new_name, ProductGroup.id != group.id)
    )).scalar_one_or_none()
    if dup:
        await message.answer("گروهی با این نام از قبل وجود دارد.", reply_markup=back_to_admin_kb("admin:groups"))
        return
    old_name = group.name
    group.name = new_name
    await session.execute(
        update(ServerPlan).where(ServerPlan.category == old_name).values(category=new_name)
    )
    await session.flush()
    await _render_group_detail(message, session, group, edit=False)


@router.message(GroupFSM.edit_emoji)
async def group_edit_emoji(message: Message, state: FSMContext, session: AsyncSession):
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("هش اموجی باید فقط عدد باشد. دوباره وارد کنید یا «رد کردن» را بزنید:")
        return
    data = await state.get_data()
    await state.clear()
    group = await session.get(ProductGroup, data["group_id"])
    if not group:
        await message.answer("گروه یافت نشد.")
        return
    group.emoji_id = raw
    await session.flush()
    await _render_group_detail(message, session, group, edit=False)


@router.callback_query(GroupFSM.edit_emoji, F.data == "admin:skip")
async def group_edit_emoji_remove(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    await cb.answer("اموجی حذف شد.")
    group = await session.get(ProductGroup, data["group_id"])
    if not group:
        return
    group.emoji_id = None
    await session.flush()
    await _render_group_detail(cb.message, session, group)


@router.callback_query(F.data.startswith("admin:group_hide:"))
async def cb_group_hide(cb: CallbackQuery, session: AsyncSession):
    group = await session.get(ProductGroup, int(cb.data.split(":")[2]))
    if not group:
        await cb.answer("گروه یافت نشد.", show_alert=True)
        return
    group.is_hidden = not group.is_hidden
    await session.flush()
    await cb.answer("گروه مخفی شد." if group.is_hidden else "گروه نمایان شد.")
    await _render_group_detail(cb.message, session, group)


@router.callback_query(F.data.startswith("admin:group_del_do:"))
async def cb_group_del_do(cb: CallbackQuery, session: AsyncSession):
    await cb.answer()
    group = await session.get(ProductGroup, int(cb.data.split(":")[2]))
    if group:
        await session.delete(group)
        await session.flush()
    await cb.message.edit_text("گروه حذف شد.", reply_markup=back_to_admin_kb("admin:groups"))


@router.callback_query(F.data.startswith("admin:group_del:"))
async def cb_group_del(cb: CallbackQuery, session: AsyncSession):
    group = await session.get(ProductGroup, int(cb.data.split(":")[2]))
    if not group:
        await cb.answer("گروه یافت نشد.", show_alert=True)
        return
    count = (await session.execute(
        select(func.count(ServerPlan.id)).where(ServerPlan.category == group.name)
    )).scalar() or 0
    if count:
        await cb.answer(f"این گروه {count} محصول دارد. ابتدا محصولات را منتقل یا حذف کنید.", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text(
        f"حذف گروه <b>{group.name}</b>؟",
        parse_mode="HTML",
        reply_markup=confirm_kb(f"admin:group_del_do:{group.id}", "admin:groups"),
    )


@router.callback_query(F.data.startswith("admin:plan:"))
async def cb_plan_detail(cb: CallbackQuery, session: AsyncSession):
    await _render_plan_detail(cb, session, int(cb.data.split(":")[2]))


async def _render_plan_detail(cb: CallbackQuery, session: AsyncSession, plan_id: int):
    plan = await session.get(ServerPlan, plan_id)
    if not plan:
        await cb.answer("محصول یافت نشد.", show_alert=True)
        return

    _cur = obj_currency(plan)
    billing_lines = []
    if plan.price_hourly:
        billing_lines.append(f"{fmt_price(plan.price_hourly, _cur)}/ساعت")
    if plan.price_monthly:
        billing_lines.append(f"{fmt_price(plan.price_monthly, _cur)}/ماه")
    if _cur != "irt":
        billing_lines.append(f"واحد قیمت: {CURRENCY_LABELS[_cur]} (تبدیل با نرخ روز)")
    # قیمت خرید (برای محصولات ایمپورت‌شده از API مثل هتزنر) — راهنمای مارجین‌گذاری
    _px = plan.extra_data or {}
    if _px.get("cost_hourly") or _px.get("cost_monthly"):
        billing_lines.append(
            f"قیمت خرید: €{_px.get('cost_hourly', 0):g}/ساعت · €{_px.get('cost_monthly', 0):g}/ماه"
        )

    prov_name = "—"
    if plan.provider_account_id:
        acc = await session.get(ProviderAccount, plan.provider_account_id)
        prov_name = acc.name if acc else "—"

    await cb.message.edit_text(
        f"<b>{plan.display_name or plan.name}</b>\n\n"
        f"{plan.category or '—'}\n"
        f"{prov_name}\n"
        f"Plan ID: <code>{plan.provider_plan_id or '—'}</code>\n\n"
        f"{plan.ram} MB | {plan.cpu} CPU | {plan.disk} GB | {plan.bandwidth} GB BW\n"
        f"{plan.location or '—'}\n\n"
        + "\n".join(billing_lines) + "\n\n"
        + ("✅ فعال" if plan.is_active else "❌ غیرفعال"),
        parse_mode="HTML",
        reply_markup=plan_detail_kb(plan_id, plan.is_active, plan.provider_type),
    )
    try:
        await cb.answer()
    except Exception:
        pass  # already answered by the caller (toggle/setgrp)


# ─── Add plan FSM ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:plan_add")
async def cb_plan_add_start(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    groups = await _adopt_groups(session)
    await state.set_state(PlanFSM.add_category)
    await cb.message.edit_text(
        "<b>افزودن محصول</b>\n\nمرحله ۱ — گروه محصول را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=group_pick_kb(groups, "admin:plan_grpsel"),
    )
    await cb.answer()


async def _plan_ask_name(msg):
    await msg.answer(
        "مرحله ۲ — نام داخلی (slug):\n<i>مثال: NL-Basic</i>",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )


@router.callback_query(PlanFSM.add_category, F.data.startswith("admin:plan_grpsel:"))
async def plan_pick_group(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    key = cb.data.split(":")[2]
    if key == "new":
        await state.set_state(PlanFSM.add_group_name)
        await cb.message.edit_text(
            "نام گروه جدید را وارد کنید:",
            reply_markup=cancel_admin_kb(),
        )
        await cb.answer()
        return
    group = await session.get(ProductGroup, int(key))
    if not group:
        await cb.answer("گروه یافت نشد.", show_alert=True)
        return
    await cb.answer()
    await state.update_data(category=group.name)
    await state.set_state(PlanFSM.add_name)
    await cb.message.edit_text(
        "مرحله ۲ — نام داخلی (slug):\n<i>مثال: NL-Basic</i>",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )


@router.message(PlanFSM.add_group_name)
async def plan_new_group_name(message: Message, state: FSMContext, session: AsyncSession):
    name = (message.text or "").strip()
    if not name or len(name) > 60:
        await message.answer("نام معتبر نیست (حداکثر ۶۰ کاراکتر). دوباره وارد کنید:")
        return
    existing = (await session.execute(
        select(ProductGroup).where(ProductGroup.name == name)
    )).scalar_one_or_none()
    if existing:
        # گروه از قبل هست — همان را انتخاب کن و ادامه بده
        await state.update_data(category=existing.name)
        await state.set_state(PlanFSM.add_name)
        await message.answer("گروه از قبل موجود بود و انتخاب شد.")
        await _plan_ask_name(message)
        return
    await state.update_data(new_group_name=name)
    await state.set_state(PlanFSM.add_group_emoji)
    await message.answer(
        "هش اموجی مخصوص گروه محصول را وارد کنید:\n"
        "<i>مثال: 5258503720928288433</i>",
        parse_mode="HTML", reply_markup=skip_or_cancel_kb(),
    )


async def _plan_create_group(state: FSMContext, session: AsyncSession, emoji_id: str | None) -> None:
    data = await state.get_data()
    group = ProductGroup(name=data["new_group_name"], emoji_id=emoji_id)
    session.add(group)
    await session.flush()
    await state.update_data(category=group.name)
    await state.set_state(PlanFSM.add_name)


@router.message(PlanFSM.add_group_emoji)
async def plan_new_group_emoji(message: Message, state: FSMContext, session: AsyncSession):
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("هش اموجی باید فقط عدد باشد. دوباره وارد کنید یا «رد کردن» را بزنید:")
        return
    await _plan_create_group(state, session, raw)
    await message.answer("گروه ساخته شد.")
    await _plan_ask_name(message)


@router.callback_query(PlanFSM.add_group_emoji, F.data == "admin:skip")
async def plan_new_group_emoji_skip(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    await cb.answer()
    await _plan_create_group(state, session, None)
    await cb.message.answer("گروه (بدون اموجی) ساخته شد.")
    await _plan_ask_name(cb.message)


@router.message(PlanFSM.add_name)
async def plan_add_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(PlanFSM.add_display_name)
    await message.answer(
        "مرحله ۳ — نام نمایشی:\n<i>مثال: پلن پایه هلند</i>",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )


@router.message(PlanFSM.add_display_name)
async def plan_add_display_name(message: Message, state: FSMContext):
    await state.update_data(display_name=message.text.strip())
    await state.set_state(PlanFSM.add_emoji)
    await message.answer(
        "هش اموجی مخصوص محصول را وارد کنید:\n"
        "<i>مثال: 5258503720928288433</i>\n\n"
        "این اموجی کنار دکمه محصول در فلوی خرید نمایش داده می‌شود.",
        parse_mode="HTML", reply_markup=skip_or_cancel_kb(),
    )


async def _plan_ask_provider(msg, state: FSMContext, session: AsyncSession):
    await state.set_state(PlanFSM.add_provider)
    result = await session.execute(
        select(ProviderAccount).where(
            ProviderAccount.provider_type == ProviderType.VIRTUALIZOR,
            ProviderAccount.is_active == True,
        )
    )
    providers = list(result.scalars().all())
    if not providers:
        await msg.answer("ابتدا یک سرور ویرچولایزور اضافه کنید.", reply_markup=back_to_admin_kb("admin:providers"))
        await state.clear()
        return
    await msg.answer("مرحله ۴ — سرور ویرچولایزور:", reply_markup=providers_select_kb(providers))


@router.message(PlanFSM.add_emoji)
async def plan_add_emoji(message: Message, state: FSMContext, session: AsyncSession):
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("هش اموجی باید فقط عدد باشد. دوباره وارد کنید یا «رد کردن» را بزنید:")
        return
    await state.update_data(plan_emoji=raw)
    await _plan_ask_provider(message, state, session)


@router.callback_query(PlanFSM.add_emoji, F.data == "admin:skip")
async def plan_add_emoji_skip(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    await cb.answer()
    await state.update_data(plan_emoji=None)
    await _plan_ask_provider(cb.message, state, session)


@router.callback_query(PlanFSM.add_provider, F.data.startswith("admin:plan_prov:"))
async def plan_add_provider(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    provider_id = int(cb.data.split(":")[2])
    await state.update_data(provider_account_id=provider_id)

    account = await session.get(ProviderAccount, provider_id)
    if account:
        fetch_msg = await cb.message.edit_text("در حال خواندن پلن‌های Virtualizor...")
        try:
            prov = VirtualizorProvider(account.api_endpoint, account.api_key, account.api_secret)
            virt_plans = await asyncio.wait_for(prov.list_plans(), timeout=15)
            if virt_plans:
                await state.set_state(PlanFSM.add_plan_id)
                from aiogram.utils.keyboard import InlineKeyboardBuilder as _IKB
                builder = _IKB()
                for p in virt_plans[:25]:
                    ram_label = f"{p.ram // 1024}GB" if p.ram >= 1024 else f"{p.ram}MB"
                    label = f"{p.name} | {ram_label}/{p.cpu}C/{p.disk}G"
                    builder.button(text=label, callback_data=f"admin:vplan:{p.provider_plan_id}")
                builder.button(text="انصراف", callback_data="admin_panel")
                builder.adjust(1)
                await fetch_msg.edit_text(
                    "مرحله ۵ — انتخاب پلن از Virtualizor:\n"
                    "<i>یکی از پلن‌های زیر را انتخاب کنید:</i>",
                    parse_mode="HTML",
                    reply_markup=builder.as_markup(),
                )
                await cb.answer()
                return
            else:
                await fetch_msg.edit_text("هیچ پلنی در Virtualizor یافت نشد. ابتدا پلن بسازید.")
                await cb.answer()
                return
        except Exception as e:
            await fetch_msg.edit_text(
                f"خطا در اتصال به Virtualizor: {e}\n\nPlan ID را دستی وارد کنید:",
                parse_mode="HTML",
                reply_markup=cancel_admin_kb(),
            )
            await state.set_state(PlanFSM.add_plan_id)
            await cb.answer()
            return

    await state.set_state(PlanFSM.add_plan_id)
    await cb.message.edit_text(
        "مرحله ۵ — Plan ID در ویرچولایزور:\n<i>شناسه پلن را وارد کنید.</i>",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.callback_query(PlanFSM.add_plan_id, F.data.startswith("admin:vplan:"))
async def plan_select_virt_plan(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    plan_id_str = cb.data.split(":", 2)[2]
    data = await state.get_data()
    account = await session.get(ProviderAccount, data["provider_account_id"])
    if not account:
        await cb.answer("پروایدر یافت نشد.", show_alert=True)
        return

    try:
        prov = VirtualizorProvider(account.api_endpoint, account.api_key, account.api_secret)
        virt_plans = await asyncio.wait_for(prov.list_plans(), timeout=15)
        matched = next((p for p in virt_plans if str(p.provider_plan_id) == plan_id_str), None)
    except Exception as e:
        await cb.answer(f"خطا: {e}", show_alert=True)
        return

    if not matched:
        await cb.answer("پلن یافت نشد.", show_alert=True)
        return

    await state.update_data(
        provider_plan_id=plan_id_str,
        ram=matched.ram, cpu=matched.cpu, disk=matched.disk,
        bandwidth=matched.bandwidth, autofetch=True,
    )
    await state.set_state(PlanFSM.confirm_autofetch)
    ram_label = f"{matched.ram // 1024}GB" if matched.ram >= 1024 else f"{matched.ram}MB"
    await cb.message.edit_text(
        f"<b>پلن انتخاب شد:</b> {matched.name}\n\n"
        f"RAM: {ram_label} ({matched.ram} MB)\n"
        f"CPU: {matched.cpu} هسته\n"
        f"Disk: {matched.disk} GB\n"
        f"Bandwidth: {matched.bandwidth} GB\n\n"
        "آیا این مشخصات درست است؟",
        parse_mode="HTML",
        reply_markup=confirm_kb("admin:plan_autofetch_ok", "admin:plan_autofetch_no"),
    )
    await cb.answer()


@router.message(PlanFSM.add_plan_id)
async def plan_add_plan_id(message: Message, state: FSMContext, session: AsyncSession):
    plan_id_str = message.text.strip()
    await state.update_data(provider_plan_id=plan_id_str)

    data = await state.get_data()
    account = await session.get(ProviderAccount, data["provider_account_id"])
    if account:
        fetch_msg = await message.answer("در حال خواندن مشخصات از Virtualizor...")
        try:
            prov = VirtualizorProvider(account.api_endpoint, account.api_key, account.api_secret)
            virt_plans = await asyncio.wait_for(prov.list_plans(), timeout=15)
            matched = next((p for p in virt_plans if str(p.provider_plan_id) == plan_id_str), None)
            if matched:
                await state.update_data(
                    ram=matched.ram, cpu=matched.cpu, disk=matched.disk,
                    bandwidth=matched.bandwidth, autofetch=True,
                )
                await fetch_msg.edit_text(
                    f"<b>مشخصات خودکار خوانده شد:</b>\n\n"
                    f"RAM: {matched.ram} MB\nCPU: {matched.cpu} هسته\n"
                    f"Disk: {matched.disk} GB\nBandwidth: {matched.bandwidth} GB\n\n"
                    "آیا این مشخصات درست است؟",
                    parse_mode="HTML",
                    reply_markup=confirm_kb("admin:plan_autofetch_ok", "admin:plan_autofetch_no"),
                )
                await state.set_state(PlanFSM.confirm_autofetch)
                return
            else:
                available = ", ".join(str(p.provider_plan_id) for p in virt_plans[:15])
                await fetch_msg.edit_text(
                    f"Plan ID <code>{plan_id_str}</code> یافت نشد.\n"
                    f"IDهای موجود: <code>{available or 'هیچ'}</code>",
                    parse_mode="HTML",
                )
        except Exception as e:
            await fetch_msg.edit_text(f"خطا: {e}")

    await state.set_state(PlanFSM.add_ram)
    await message.answer("مرحله ۶ — RAM (مگابایت):", reply_markup=cancel_admin_kb())


@router.callback_query(PlanFSM.confirm_autofetch, F.data == "admin:plan_autofetch_ok")
async def plan_autofetch_ok(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PlanFSM.add_billing)
    await cb.message.edit_text("مرحله ۷ — نوع بیلینگ:", reply_markup=billing_type_admin_kb())
    await cb.answer()


@router.callback_query(PlanFSM.confirm_autofetch, F.data == "admin:plan_autofetch_no")
async def plan_autofetch_no(cb: CallbackQuery, state: FSMContext):
    await state.update_data(autofetch=False, ram=None, cpu=None, disk=None, bandwidth=None)
    await state.set_state(PlanFSM.add_ram)
    await cb.message.edit_text("مرحله ۶ — RAM (مگابایت):", reply_markup=cancel_admin_kb())
    await cb.answer()


@router.message(PlanFSM.add_ram, F.text.regexp(r"^\d+$"))
async def plan_add_ram(message: Message, state: FSMContext):
    await state.update_data(ram=int(message.text))
    await state.set_state(PlanFSM.add_cpu)
    await message.answer("مرحله ۷ — CPU (هسته):", reply_markup=cancel_admin_kb())


@router.message(PlanFSM.add_cpu, F.text.regexp(r"^\d+$"))
async def plan_add_cpu(message: Message, state: FSMContext):
    await state.update_data(cpu=int(message.text))
    await state.set_state(PlanFSM.add_disk)
    await message.answer("مرحله ۸ — Disk (GB):", reply_markup=cancel_admin_kb())


@router.message(PlanFSM.add_disk, F.text.regexp(r"^\d+$"))
async def plan_add_disk(message: Message, state: FSMContext):
    await state.update_data(disk=int(message.text))
    await state.set_state(PlanFSM.add_bandwidth)
    await message.answer("مرحله ۹ — Bandwidth ماهانه (GB):", reply_markup=cancel_admin_kb())


@router.message(PlanFSM.add_bandwidth, F.text.regexp(r"^\d+$"))
async def plan_add_bandwidth(message: Message, state: FSMContext):
    await state.update_data(bandwidth=int(message.text))
    await state.set_state(PlanFSM.add_billing)
    await message.answer("مرحله ۱۰ — نوع بیلینگ:", reply_markup=billing_type_admin_kb())


@router.callback_query(PlanFSM.add_billing, F.data.startswith("admin:billing:"))
async def plan_add_billing(cb: CallbackQuery, state: FSMContext):
    billing = cb.data.split(":")[2]
    await state.update_data(billing=billing)
    # قبل از قیمت، واحد قیمت پرسیده می‌شود (ریالی/دلاری/یورویی)
    await state.set_state(PlanFSM.add_currency)
    await cb.message.edit_text(
        "واحد قیمت این محصول چه باشد؟\n"
        "<i>قیمت‌های ارزی هنگام کسر، با نرخ روز به ریال تبدیل می‌شوند.</i>",
        parse_mode="HTML",
        reply_markup=_currency_pick_kb(),
    )
    await cb.answer()


@router.callback_query(PlanFSM.add_currency, F.data.startswith("admin:plancur:"))
async def plan_add_currency(cb: CallbackQuery, state: FSMContext):
    cur = cb.data.split(":")[2]
    if cur not in CURRENCY_LABELS:
        await cb.answer("واحد نامعتبر.", show_alert=True)
        return
    await state.update_data(plan_currency=cur)
    unit = CURRENCY_LABELS[cur]
    data = await state.get_data()
    if data.get("billing") in ("hourly", "both"):
        await state.set_state(PlanFSM.add_price_hourly)
        await cb.message.edit_text(f"قیمت ساعتی ({unit}):", reply_markup=cancel_admin_kb())
    else:
        await state.update_data(price_hourly=None)
        await state.set_state(PlanFSM.add_price_monthly)
        await cb.message.edit_text(f"قیمت ماهانه ({unit}):", reply_markup=cancel_admin_kb())
    await cb.answer()


@router.message(PlanFSM.add_price_hourly, F.text.regexp(r"^\d+(\.\d+)?$"))
async def plan_add_price_hourly(message: Message, state: FSMContext):
    await state.update_data(price_hourly=float(message.text))
    data = await state.get_data()
    unit = CURRENCY_LABELS.get(data.get("plan_currency", "irt"), "تومان")
    if data.get("billing") == "both":
        await state.set_state(PlanFSM.add_price_monthly)
        await message.answer(f"قیمت ماهانه ({unit}):", reply_markup=cancel_admin_kb())
    else:
        await state.update_data(price_monthly=None)
        await state.set_state(PlanFSM.add_location)
        await message.answer("موقعیت (یا /skip):\n<i>مثال: netherlands</i>", parse_mode="HTML", reply_markup=skip_or_cancel_kb())


@router.message(PlanFSM.add_price_monthly, F.text.regexp(r"^\d+(\.\d+)?$"))
async def plan_add_price_monthly(message: Message, state: FSMContext):
    await state.update_data(price_monthly=float(message.text))
    await state.set_state(PlanFSM.add_location)
    await message.answer("موقعیت (یا /skip):", reply_markup=skip_or_cancel_kb())


@router.message(PlanFSM.add_location)
async def plan_add_location(message: Message, state: FSMContext, session: AsyncSession):
    raw = message.text.strip()
    location = None if raw.lower() in ("/skip", "skip") else raw
    await _save_new_plan(message, state, session, location)


@router.callback_query(PlanFSM.add_location, F.data == "admin:skip")
async def plan_add_location_skip(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    await _save_new_plan(cb.message, state, session, None)
    await cb.answer()


async def _save_new_plan(msg, state: FSMContext, session: AsyncSession, location: Optional[str]):
    data = await state.get_data()
    await state.clear()
    plan = ServerPlan(
        provider_type=ProviderType.VIRTUALIZOR,
        provider_account_id=data.get("provider_account_id"),
        provider_plan_id=data.get("provider_plan_id"),
        name=data["name"],
        display_name=data.get("display_name"),
        category=data.get("category"),
        ram=data.get("ram") or 1024,
        cpu=data.get("cpu") or 1,
        disk=data.get("disk") or 20,
        bandwidth=data.get("bandwidth") or 1000,
        price_hourly=data.get("price_hourly"),
        price_monthly=data.get("price_monthly"),
        location=location,
        is_active=True,
        extra_data={
            "currency": data.get("plan_currency", "irt"),
            **({"emoji_id": data["plan_emoji"]} if data.get("plan_emoji") else {}),
        },
    )
    session.add(plan)
    await session.flush()

    _cur = data.get("plan_currency", "irt")
    billing_lines = []
    if plan.price_hourly:
        billing_lines.append(f"{fmt_price(plan.price_hourly, _cur)}/ساعت")
    if plan.price_monthly:
        billing_lines.append(f"{fmt_price(plan.price_monthly, _cur)}/ماه")

    autofetch_note = "\nمشخصات از Virtualizor خوانده شد" if data.get("autofetch") else ""
    await msg.answer(
        f"<b>محصول اضافه شد!</b>{autofetch_note}\n\n"
        f"{plan.display_name or plan.name}\n"
        f"{plan.category}\n"
        f"{plan.ram}MB | {plan.cpu}CPU | {plan.disk}GB\n"
        + "\n".join(billing_lines),
        parse_mode="HTML",
        reply_markup=back_to_admin_kb("admin:plans"),
    )


# ─── Edit plan ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin:plan_edit:"))
async def cb_plan_edit_start(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    parts = cb.data.split(":")
    plan_id, field = int(parts[2]), parts[3]

    # گروه محصول از لیست انتخاب می‌شود (نه تایپ متن)
    if field == "category":
        groups = await _adopt_groups(session)
        await cb.message.edit_text(
            "<b>تغییر گروه محصول</b>\n\nگروه جدید را انتخاب کنید:",
            parse_mode="HTML",
            reply_markup=group_pick_kb(groups, f"admin:plan_setgrp:{plan_id}",
                                       allow_new=False, cancel_cb=f"admin:plan:{plan_id}"),
        )
        await cb.answer()
        return

    # اموجی محصول: مثل گروه — هش جدید یا «رد کردن» برای حذف
    if field == "emoji":
        await state.update_data(edit_plan_id=plan_id)
        await state.set_state(PlanFSM.edit_emoji)
        await cb.message.edit_text(
            "<b>ویرایش اموجی محصول</b>\n\n"
            "هش اموجی جدید را وارد کنید:\n"
            "<i>مثال: 5258503720928288433</i>\n\n"
            "برای حذف اموجی، «رد کردن» را بزنید.",
            parse_mode="HTML", reply_markup=skip_or_cancel_kb(),
        )
        await cb.answer()
        return

    # تغییر Plan ID: مشخصات از ویرچولایزور خوانده و بعد از تأیید اعمال می‌شود
    if field == "provider_plan_id":
        _plan_chk = await session.get(ServerPlan, plan_id)
        if _plan_chk and _plan_chk.provider_type != ProviderType.VIRTUALIZOR:
            await cb.answer("این گزینه فقط برای محصولات ویرچولایزور است.", show_alert=True)
            return
        await state.update_data(edit_plan_id=plan_id)
        await state.set_state(PlanFSM.edit_plan_id)
        await cb.message.edit_text(
            "<b>تغییر Plan ID ویرچولایزور</b>\n\n"
            "Plan ID جدید را وارد کنید:\n"
            "<i>مشخصات (RAM/CPU/Disk/BW) خودکار از پلن خوانده می‌شود.</i>",
            parse_mode="HTML", reply_markup=cancel_admin_kb(),
        )
        await cb.answer()
        return

    # ویرایش قیمت: اول واحد ارز پرسیده می‌شود (قابل تغییر از یورو به تومان/دلار و برعکس)
    if field in ("price_hourly", "price_monthly"):
        _plan = await session.get(ServerPlan, plan_id)
        _cur_now = CURRENCY_LABELS.get(obj_currency(_plan) if _plan else "irt", "تومان")
        await state.update_data(edit_plan_id=plan_id, edit_field=field)
        await state.set_state(PlanFSM.edit_price_currency)
        await cb.message.edit_text(
            f"<b>ویرایش {'قیمت ساعتی' if field == 'price_hourly' else 'قیمت ماهانه'}</b>\n\n"
            f"واحد فعلی: <b>{_cur_now}</b>\n"
            "واحد قیمت جدید را انتخاب کنید:",
            parse_mode="HTML",
            reply_markup=_currency_pick_kb(),
        )
        await cb.answer()
        return

    labels = {
        "location": "موقعیت",
        "display_name": "نام نمایشی",
    }
    await state.update_data(edit_plan_id=plan_id, edit_field=field)
    await state.set_state(PlanFSM.edit_value)
    await cb.message.edit_text(
        f"<b>ویرایش {labels.get(field, field)}</b>\n\nمقدار جدید:",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(PlanFSM.edit_emoji)
async def plan_edit_emoji(message: Message, state: FSMContext, session: AsyncSession):
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("هش اموجی باید فقط عدد باشد. دوباره وارد کنید یا «رد کردن» را بزنید:")
        return
    data = await state.get_data()
    await state.clear()
    plan = await session.get(ServerPlan, data["edit_plan_id"])
    if not plan:
        await message.answer("محصول یافت نشد.")
        return
    extra = dict(plan.extra_data or {})
    extra["emoji_id"] = raw
    plan.extra_data = extra
    await session.flush()
    await message.answer("اموجی محصول ذخیره شد.",
                         reply_markup=plan_detail_kb(plan.id, plan.is_active, plan.provider_type))


@router.callback_query(PlanFSM.edit_emoji, F.data == "admin:skip")
async def plan_edit_emoji_remove(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    await cb.answer("اموجی حذف شد.")
    plan = await session.get(ServerPlan, data["edit_plan_id"])
    if not plan:
        return
    extra = dict(plan.extra_data or {})
    extra.pop("emoji_id", None)
    plan.extra_data = extra
    await session.flush()
    await _render_plan_detail(cb, session, plan.id)


@router.callback_query(PlanFSM.edit_price_currency, F.data.startswith("admin:plancur:"))
async def plan_edit_price_currency(cb: CallbackQuery, state: FSMContext):
    cur = cb.data.split(":")[2]
    if cur not in CURRENCY_LABELS:
        await cb.answer("واحد نامعتبر.", show_alert=True)
        return
    await state.update_data(edit_currency=cur)
    await state.set_state(PlanFSM.edit_value)
    data = await state.get_data()
    label = "قیمت ساعتی" if data.get("edit_field") == "price_hourly" else "قیمت ماهانه"
    await cb.message.edit_text(
        f"<b>{label} ({CURRENCY_LABELS[cur]})</b>\n\nمقدار جدید:",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(PlanFSM.edit_plan_id)
async def plan_edit_plan_id_input(message: Message, state: FSMContext, session: AsyncSession):
    new_id = (message.text or "").strip()
    data = await state.get_data()
    plan = await session.get(ServerPlan, data["edit_plan_id"])
    if not plan:
        await state.clear()
        await message.answer("محصول یافت نشد.")
        return
    account = await session.get(ProviderAccount, plan.provider_account_id) if plan.provider_account_id else None
    if not account:
        await state.clear()
        await message.answer("سرور ویرچولایزور این محصول یافت نشد.",
                             reply_markup=back_to_admin_kb("admin:plans_list"))
        return

    wait = await message.answer("در حال خواندن پلن از Virtualizor...")
    try:
        prov = VirtualizorProvider(account.api_endpoint, account.api_key, account.api_secret)
        virt_plans = await asyncio.wait_for(prov.list_plans(), timeout=15)
        matched = next((p for p in virt_plans if str(p.provider_plan_id) == new_id), None)
    except Exception as e:
        await wait.edit_text(f"{ERR} خطا در اتصال به Virtualizor: {_html.escape(str(e))}\n\nدوباره Plan ID را وارد کنید:",
                             parse_mode="HTML", reply_markup=cancel_admin_kb())
        return

    if not matched:
        available = ", ".join(str(p.provider_plan_id) for p in virt_plans[:15])
        await wait.edit_text(
            f"{ERR} Plan ID <code>{new_id}</code> یافت نشد.\n"
            f"IDهای موجود: <code>{available or 'هیچ'}</code>\n\nدوباره وارد کنید:",
            parse_mode="HTML", reply_markup=cancel_admin_kb(),
        )
        return

    await state.update_data(
        pending_plan_id=new_id,
        pending_ram=matched.ram, pending_cpu=matched.cpu,
        pending_disk=matched.disk, pending_bandwidth=matched.bandwidth,
    )
    await state.set_state(PlanFSM.edit_plan_id_confirm)
    ram_label = f"{matched.ram // 1024}GB" if matched.ram >= 1024 else f"{matched.ram}MB"
    await wait.edit_text(
        f"<b>پلنی که ID آن را وارد کردید:</b> {matched.name}\n\n"
        f"RAM: {ram_label} ({matched.ram} MB)\n"
        f"CPU: {matched.cpu} هسته\n"
        f"Disk: {matched.disk} GB\n"
        f"Bandwidth: {matched.bandwidth} GB\n\n"
        "تأیید می‌کنید؟ (مشخصات محصول با این مقادیر جایگزین می‌شود)",
        parse_mode="HTML",
        reply_markup=confirm_kb("admin:planid_apply", f"admin:plan:{plan.id}"),
    )


@router.callback_query(PlanFSM.edit_plan_id_confirm, F.data == "admin:planid_apply")
async def plan_edit_plan_id_apply(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    plan = await session.get(ServerPlan, data["edit_plan_id"])
    if not plan:
        await cb.answer("محصول یافت نشد.", show_alert=True)
        return
    plan.provider_plan_id = data["pending_plan_id"]
    plan.ram = data["pending_ram"]
    plan.cpu = data["pending_cpu"]
    plan.disk = data["pending_disk"]
    plan.bandwidth = data["pending_bandwidth"]
    await session.flush()
    await cb.answer("Plan ID و مشخصات به‌روز شد.")
    await _render_plan_detail(cb, session, plan.id)


@router.callback_query(F.data.startswith("admin:plan_setgrp:"))
async def cb_plan_setgrp(cb: CallbackQuery, session: AsyncSession):
    parts = cb.data.split(":")
    plan_id, gid = int(parts[2]), parts[3]
    plan = await session.get(ServerPlan, plan_id)
    group = await session.get(ProductGroup, int(gid)) if gid.isdigit() else None
    if not plan or not group:
        await cb.answer("یافت نشد.", show_alert=True)
        return
    plan.category = group.name
    await session.flush()
    await cb.answer("گروه محصول تغییر کرد.")
    await _render_plan_detail(cb, session, plan_id)


async def _propagate_plan_price(session: AsyncSession, plan: ServerPlan) -> int:
    """قیمت/ارز جدید پلن روی سرورهای موجودِ مشتری‌ها هم اعمال می‌شود.

    سرورهای جدید plan_id دارند؛ سرورهای قدیمیِ بدون لینک، با تطبیق
    پروایدر + مشخصات (رم/سی‌پی‌یو/دیسک) یک‌بار adopt می‌شوند و از آن به بعد
    بیلینگ همیشه قیمت روز پلن را می‌خواند."""
    result = await session.execute(
        select(Server).where(Server.status != ServerStatus.DELETED)
    )
    cur = (plan.extra_data or {}).get("currency", "irt")
    count = 0
    for s in result.scalars().all():
        extra = dict(s.extra_data or {})
        linked = extra.get("plan_id") == plan.id
        if not linked and extra.get("plan_id") is None:
            linked = (
                s.provider_account_id == plan.provider_account_id
                and s.ram == plan.ram and s.cpu == plan.cpu and s.disk == plan.disk
            )
            if linked:
                extra["plan_id"] = plan.id
        if not linked:
            continue
        s.price_hourly = plan.price_hourly
        s.price_monthly = plan.price_monthly
        extra["currency"] = cur
        s.extra_data = extra
        count += 1
    await session.flush()
    return count


@router.message(PlanFSM.edit_value)
async def plan_edit_value(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    plan = await session.get(ServerPlan, data["edit_plan_id"])
    if not plan:
        await message.answer("محصول یافت نشد.")
        return
    field, raw = data["edit_field"], message.text.strip()
    try:
        warn = ""
        if field in ("price_hourly", "price_monthly"):
            val = float(raw)
            setattr(plan, field, val if val > 0 else None)
            # اگر واحد ارز جدیدی انتخاب شده، روی پلن اعمال می‌شود
            # (واحد در سطح پلن است و هر دو قیمت را شامل می‌شود)
            new_cur = data.get("edit_currency")
            if new_cur and new_cur in CURRENCY_LABELS:
                old_cur = obj_currency(plan)
                extra = dict(plan.extra_data or {})
                extra["currency"] = new_cur
                plan.extra_data = extra
                other_field = "price_monthly" if field == "price_hourly" else "price_hourly"
                other_label = "ماهانه" if field == "price_hourly" else "ساعتی"
                if new_cur != old_cur and getattr(plan, other_field):
                    warn = (
                        f"\n\n⚠️ واحد قیمت در سطح محصول است — قیمت {other_label} هم از این پس "
                        f"به {CURRENCY_LABELS[new_cur]} تفسیر می‌شود؛ در صورت نیاز آن را هم ویرایش کنید."
                    )
            # اعمال قیمت جدید روی سرورهای فعالِ همین پلن (مشتری‌های قبلی)
            synced = await _propagate_plan_price(session, plan)
            if synced:
                warn += f"\n\nقیمت جدید روی {synced} سرور فعال مشتری‌ها هم اعمال شد."
        elif field in ("ram", "cpu", "disk", "bandwidth"):
            setattr(plan, field, int(raw))
        else:
            setattr(plan, field, raw if raw not in ("-", "—", "none", "0") else None)
        await session.flush()
        await message.answer(f"ذخیره شد.{warn}",
                             reply_markup=plan_detail_kb(data["edit_plan_id"], plan.is_active, plan.provider_type))
    except ValueError:
        await message.answer("مقدار نامعتبر.")


@router.callback_query(F.data.startswith("admin:plan_toggle:"))
async def cb_plan_toggle(cb: CallbackQuery, session: AsyncSession):
    plan_id = int(cb.data.split(":")[2])
    plan = await session.get(ServerPlan, plan_id)
    if not plan:
        await cb.answer("محصول یافت نشد.", show_alert=True)
        return
    plan.is_active = not plan.is_active
    await session.flush()
    await cb.answer(f"{'فعال' if plan.is_active else 'غیرفعال'} شد.")
    await _render_plan_detail(cb, session, plan_id)


@router.callback_query(F.data.startswith("admin:plan_del:"))
async def cb_plan_del_confirm(cb: CallbackQuery, session: AsyncSession):
    plan_id = int(cb.data.split(":")[2])
    plan = await session.get(ServerPlan, plan_id)
    if not plan:
        await cb.answer("محصول یافت نشد.", show_alert=True)
        return
    await cb.message.edit_text(
        f"حذف محصول <b>{plan.display_name or plan.name}</b>؟",
        parse_mode="HTML",
        reply_markup=confirm_kb(f"admin:plan_del_do:{plan_id}", "admin:plans"),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("admin:plan_del_do:"))
async def cb_plan_del_do(cb: CallbackQuery, session: AsyncSession):
    plan_id = int(cb.data.split(":")[2])
    plan = await session.get(ServerPlan, plan_id)
    if plan:
        await session.delete(plan)
        await session.flush()
    await cb.message.edit_text("محصول حذف شد.", reply_markup=back_to_admin_kb("admin:plans"))
    await cb.answer()


# ══════════════════════════════════════════════════════════════════════════════
#  SUB-PRODUCTS
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("admin:subproducts:"))
async def cb_subproducts(cb: CallbackQuery, session: AsyncSession):
    plan_id = int(cb.data.split(":")[2])
    plan = await session.get(ServerPlan, plan_id)
    if not plan:
        await cb.answer("محصول یافت نشد.", show_alert=True)
        return
    result = await session.execute(
        select(SubProduct).where(SubProduct.plan_id == plan_id).order_by(SubProduct.name)
    )
    subs = list(result.scalars().all())
    await cb.message.edit_text(
        f"<b>ریز-محصولات — {plan.display_name or plan.name}</b>\n\n"
        f"{len(subs)} ریز-محصول:",
        parse_mode="HTML",
        reply_markup=subproducts_kb(plan_id, subs),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("admin:subprod:"))
async def cb_subprod_detail(cb: CallbackQuery, session: AsyncSession):
    await _render_subprod_detail(cb, session, int(cb.data.split(":")[2]))


async def _render_subprod_detail(cb: CallbackQuery, session: AsyncSession, sp_id: int):
    sp = await session.get(SubProduct, sp_id)
    if not sp:
        await cb.answer("ریز-محصول یافت نشد.", show_alert=True)
        return
    type_label = "ترافیک" if sp.type == SubProductType.TRAFFIC else "IP اضافه"
    unit = "GB" if sp.type == SubProductType.TRAFFIC else "عدد"
    await cb.message.edit_text(
        f"<b>{sp.name}</b>\n\n"
        f"نوع: {type_label}\n"
        f"مقدار: {sp.value} {unit}\n"
        f"قیمت: {sp.price:,.0f} تومان\n"
        f"وضعیت: {'✅ فعال' if sp.is_active else '❌ غیرفعال'}",
        parse_mode="HTML",
        reply_markup=subprod_detail_kb(sp_id, sp.plan_id, sp.is_active),
    )
    try:
        await cb.answer()
    except Exception:
        pass


@router.callback_query(F.data.startswith("admin:subprod_add:"))
async def cb_subprod_add_start(cb: CallbackQuery, state: FSMContext):
    plan_id = int(cb.data.split(":")[2])
    await state.update_data(subprod_plan_id=plan_id)
    await state.set_state(SubProductFSM.select_type)
    await cb.message.edit_text(
        "<b>ریز-محصول جدید</b>\n\nنوع را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=subprod_type_kb(plan_id),
    )
    await cb.answer()


@router.callback_query(SubProductFSM.select_type, F.data.startswith("admin:subprod_type:"))
async def cb_subprod_type(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    plan_id, sp_type = int(parts[2]), parts[3]
    await state.update_data(subprod_type=sp_type)
    await state.set_state(SubProductFSM.add_name)
    await cb.message.edit_text("نام ریز-محصول:\n<i>مثال: ۱۰ گیگ ترافیک اضافه</i>", parse_mode="HTML", reply_markup=cancel_admin_kb())
    await cb.answer()


@router.message(SubProductFSM.add_name)
async def subprod_add_name(message: Message, state: FSMContext):
    await state.update_data(subprod_name=message.text.strip())
    await state.set_state(SubProductFSM.add_price)
    await message.answer("قیمت (تومان):", reply_markup=cancel_admin_kb())


@router.message(SubProductFSM.add_price, F.text.regexp(r"^\d+(\.\d+)?$"))
async def subprod_add_price(message: Message, state: FSMContext):
    await state.update_data(subprod_price=float(message.text))
    data = await state.get_data()
    sp_type = data["subprod_type"]
    unit = "GB" if sp_type == "traffic" else "تعداد IP"
    await state.set_state(SubProductFSM.add_value)
    await message.answer(f"مقدار ({unit}):", reply_markup=cancel_admin_kb())


@router.message(SubProductFSM.add_value, F.text.regexp(r"^\d+(\.\d+)?$"))
async def subprod_add_value(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()

    sp_type = SubProductType.TRAFFIC if data["subprod_type"] == "traffic" else SubProductType.EXTRA_IP
    sp = SubProduct(
        plan_id=data["subprod_plan_id"],
        name=data["subprod_name"],
        type=sp_type,
        price=data["subprod_price"],
        value=float(message.text),
        is_active=True,
    )
    session.add(sp)
    await session.flush()
    await message.answer(
        f"ریز-محصول <b>{sp.name}</b> اضافه شد!\nقیمت: {sp.price:,.0f} تومان",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb(f"admin:subproducts:{data['subprod_plan_id']}"),
    )


@router.callback_query(F.data.startswith("admin:subprod_edit:"))
async def cb_subprod_edit(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    sp_id, field = int(parts[2]), parts[3]
    labels = {"name": "نام", "price": "قیمت (تومان)", "value": "مقدار"}
    await state.update_data(edit_sp_id=sp_id, edit_sp_field=field)
    await state.set_state(SubProductFSM.edit_value)
    await cb.message.edit_text(
        f"<b>ویرایش {labels.get(field, field)}</b>\n\nمقدار جدید:",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(SubProductFSM.edit_value)
async def subprod_edit_value(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    sp = await session.get(SubProduct, data["edit_sp_id"])
    if not sp:
        await message.answer("یافت نشد.")
        return
    field, raw = data["edit_sp_field"], message.text.strip()
    try:
        if field == "name":
            sp.name = raw
        elif field in ("price", "value"):
            setattr(sp, field, float(raw))
        await session.flush()
        await message.answer("ذخیره شد.", reply_markup=subprod_detail_kb(sp.id, sp.plan_id, sp.is_active))
    except ValueError:
        await message.answer("مقدار نامعتبر.")


@router.callback_query(F.data.startswith("admin:subprod_toggle:"))
async def cb_subprod_toggle(cb: CallbackQuery, session: AsyncSession):
    sp_id = int(cb.data.split(":")[2])
    sp = await session.get(SubProduct, sp_id)
    if not sp:
        await cb.answer("یافت نشد.", show_alert=True)
        return
    sp.is_active = not sp.is_active
    await session.flush()
    await cb.answer(f"{'فعال' if sp.is_active else 'غیرفعال'} شد.")
    await _render_subprod_detail(cb, session, sp_id)


@router.callback_query(F.data.startswith("admin:subprod_del:"))
async def cb_subprod_del(cb: CallbackQuery, session: AsyncSession):
    sp_id = int(cb.data.split(":")[2])
    sp = await session.get(SubProduct, sp_id)
    if not sp:
        await cb.answer("یافت نشد.", show_alert=True)
        return
    plan_id = sp.plan_id
    await session.delete(sp)
    await session.flush()
    await cb.message.edit_text("حذف شد.", reply_markup=back_to_admin_kb(f"admin:subproducts:{plan_id}"))
    await cb.answer()


# ══════════════════════════════════════════════════════════════════════════════
#  DISCOUNT CODES
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "admin:discounts")
async def cb_admin_discounts(cb: CallbackQuery, session: AsyncSession):
    result = await session.execute(select(DiscountCode).order_by(DiscountCode.created_at.desc()))
    codes = list(result.scalars().all())
    await cb.message.edit_text(
        f"<b>کدهای تخفیف</b>\n\nتعداد: {len(codes)}",
        parse_mode="HTML",
        reply_markup=discounts_list_kb(codes),
    )
    await cb.answer()


@router.callback_query(F.data == "admin:disc_add")
async def cb_disc_add_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(DiscountFSM.add_code)
    await cb.message.edit_text(
        "<b>کد تخفیف جدید</b>\n\nمرحله ۱ — کد:\n<i>مثال: SUMMER20</i>",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(DiscountFSM.add_code)
async def disc_add_code(message: Message, state: FSMContext, session: AsyncSession):
    code = message.text.strip().upper()
    exists = await session.execute(select(DiscountCode).where(DiscountCode.code == code))
    if exists.scalar_one_or_none():
        await message.answer("این کد قبلاً وجود دارد:")
        return
    await state.update_data(code=code)
    await state.set_state(DiscountFSM.add_percent)
    await message.answer("مرحله ۲ — درصد تخفیف (۱-۱۰۰):", reply_markup=cancel_admin_kb())


@router.message(DiscountFSM.add_percent, F.text.regexp(r"^\d+(\.\d+)?$"))
async def disc_add_percent(message: Message, state: FSMContext):
    pct = float(message.text)
    if not 1 <= pct <= 100:
        await message.answer("باید بین ۱ تا ۱۰۰:")
        return
    await state.update_data(discount_percent=pct)
    await state.set_state(DiscountFSM.add_expires)
    await message.answer("مرحله ۳ — انقضا (YYYY-MM-DD) یا /skip:", reply_markup=skip_or_cancel_kb())


@router.message(DiscountFSM.add_expires)
async def disc_add_expires(message: Message, state: FSMContext):
    raw = message.text.strip()
    if raw.lower() in ("/skip", "skip"):
        await state.update_data(expires_at=None)
    else:
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            await state.update_data(expires_at=dt.isoformat())
        except ValueError:
            await message.answer("فرمت نادرست. مثال: 2025-12-31")
            return
    await state.set_state(DiscountFSM.add_max_uses)
    await message.answer("مرحله ۴ — حداکثر استفاده یا /skip:", reply_markup=skip_or_cancel_kb())


@router.callback_query(DiscountFSM.add_expires, F.data == "admin:skip")
async def disc_add_expires_skip(cb: CallbackQuery, state: FSMContext):
    await state.update_data(expires_at=None)
    await state.set_state(DiscountFSM.add_max_uses)
    await cb.message.edit_text("مرحله ۴ — حداکثر استفاده یا /skip:", reply_markup=skip_or_cancel_kb())
    await cb.answer()


@router.message(DiscountFSM.add_max_uses)
async def disc_add_max_uses(message: Message, state: FSMContext, session: AsyncSession):
    raw = message.text.strip()
    max_uses = None
    if raw.lower() not in ("/skip", "skip"):
        try:
            max_uses = int(raw)
        except ValueError:
            await message.answer("عدد صحیح وارد کنید:")
            return
    await _save_discount(message, state, session, max_uses)


@router.callback_query(DiscountFSM.add_max_uses, F.data == "admin:skip")
async def disc_add_max_uses_skip(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    await _save_discount(cb.message, state, session, None)
    await cb.answer()


async def _save_discount(msg, state: FSMContext, session: AsyncSession, max_uses: Optional[int]):
    data = await state.get_data()
    await state.clear()
    expires_at = datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None
    code = DiscountCode(
        code=data["code"],
        discount_percent=data["discount_percent"],
        expires_at=expires_at,
        max_uses=max_uses,
        is_active=True,
    )
    session.add(code)
    await session.flush()
    exp_text = expires_at.strftime("%Y-%m-%d") if expires_at else "بدون انقضا"
    await msg.answer(
        f"<b>کد تخفیف ساخته شد!</b>\n\n"
        f"کد: <code>{code.code}</code>\n"
        f"{code.discount_percent:.0f}%\n"
        f"{exp_text}\n"
        f"{max_uses or 'نامحدود'}",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb("admin:discounts"),
    )


@router.callback_query(F.data.startswith("admin:disc:"))
async def cb_disc_detail(cb: CallbackQuery, session: AsyncSession):
    await _render_disc_detail(cb, session, int(cb.data.split(":")[2]))


async def _render_disc_detail(cb: CallbackQuery, session: AsyncSession, disc_id: int):
    code = await session.get(DiscountCode, disc_id)
    if not code:
        await cb.answer("کد یافت نشد.", show_alert=True)
        return
    exp = code.expires_at.strftime("%Y-%m-%d") if code.expires_at else "بدون انقضا"
    max_u = str(code.max_uses) if code.max_uses else "نامحدود"
    user_note = f"\nاختصاصی کاربر ID: {code.user_id}" if code.user_id else ""
    await cb.message.edit_text(
        f"<b>{code.code}</b>{user_note}\n\n"
        f"{code.discount_percent:.0f}%\n"
        f"{exp}\n"
        f"{code.use_count}/{max_u}\n"
        f"وضعیت: {'✅ فعال' if code.is_active else '❌ غیرفعال'}",
        parse_mode="HTML",
        reply_markup=discount_detail_kb(disc_id, code.is_active),
    )
    try:
        await cb.answer()
    except Exception:
        pass


@router.callback_query(F.data.startswith("admin:disc_toggle:"))
async def cb_disc_toggle(cb: CallbackQuery, session: AsyncSession):
    disc_id = int(cb.data.split(":")[2])
    code = await session.get(DiscountCode, disc_id)
    if not code:
        await cb.answer("کد یافت نشد.", show_alert=True)
        return
    code.is_active = not code.is_active
    await session.flush()
    await cb.answer(f"{'فعال' if code.is_active else 'غیرفعال'} شد.")
    await _render_disc_detail(cb, session, disc_id)


@router.callback_query(F.data.startswith("admin:disc_del:"))
async def cb_disc_del_confirm(cb: CallbackQuery, session: AsyncSession):
    disc_id = int(cb.data.split(":")[2])
    code = await session.get(DiscountCode, disc_id)
    if not code:
        await cb.answer("کد یافت نشد.", show_alert=True)
        return
    await cb.message.edit_text(
        f"حذف کد <b>{code.code}</b>؟",
        parse_mode="HTML",
        reply_markup=confirm_kb(f"admin:disc_del_do:{disc_id}", "admin:discounts"),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("admin:disc_del_do:"))
async def cb_disc_del_do(cb: CallbackQuery, session: AsyncSession):
    disc_id = int(cb.data.split(":")[2])
    code = await session.get(DiscountCode, disc_id)
    if code:
        await session.delete(code)
        await session.flush()
    await cb.message.edit_text("کد حذف شد.", reply_markup=back_to_admin_kb("admin:discounts"))
    await cb.answer()


@router.callback_query(F.data.startswith("admin:disc_edit:"))
async def cb_disc_edit_start(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    disc_id, field = int(parts[2]), parts[3]
    labels = {
        "percent": "درصد (۱-۱۰۰)",
        "expires_at": "انقضا (YYYY-MM-DD یا 0)",
        "max_uses": "حداکثر استفاده (0=نامحدود)",
    }
    await state.update_data(edit_disc_id=disc_id, edit_field=field)
    await state.set_state(DiscountFSM.edit_value)
    await cb.message.edit_text(
        f"<b>{labels.get(field, field)}</b>\n\nمقدار جدید:",
        parse_mode="HTML", reply_markup=cancel_admin_kb(),
    )
    await cb.answer()


@router.message(DiscountFSM.edit_value)
async def disc_edit_value(message: Message, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    code = await session.get(DiscountCode, data["edit_disc_id"])
    if not code:
        await message.answer("کد یافت نشد.")
        return
    field, raw = data["edit_field"], message.text.strip()
    try:
        if field == "percent":
            code.discount_percent = float(raw)
        elif field == "expires_at":
            if raw in ("0", "none", "-"):
                code.expires_at = None
            else:
                code.expires_at = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        elif field == "max_uses":
            val = int(raw)
            code.max_uses = None if val == 0 else val
        await session.flush()
        await message.answer("ذخیره شد.", reply_markup=discount_detail_kb(data["edit_disc_id"], code.is_active))
    except ValueError as e:
        await message.answer(f"مقدار نامعتبر: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  QUICK COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

@router.message(F.text.startswith("/credit "))
async def cmd_credit_user(message: Message, session: AsyncSession):
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("فرمت: /credit <telegram_id> <amount>")
        return
    try:
        tg_id, amount = int(parts[1]), float(parts[2])
    except ValueError:
        await message.answer("مقادیر نامعتبر.")
        return
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if not user:
        await message.answer("کاربر یافت نشد.")
        return
    await BillingService(session).credit(user.id, amount, description="شارژ توسط ادمین")
    await message.answer(f"{amount:,.0f} تومان به {tg_id} اضافه شد.")
    try:
        await message.bot.send_message(tg_id, f"{amount:,.0f} تومان به کیف‌پول شما اضافه شد.")
    except Exception:
        pass
