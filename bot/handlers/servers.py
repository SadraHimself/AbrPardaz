"""Server management handlers — Virtualizor only, category-based buying with discount codes."""
from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import (
    BillingType, DiscountCode, ProductGroup, ProviderAccount, ProviderType,
    Server, ServerPlan, ServerStatus, SubProduct, SubProductType, SuspendReason, User,
)
from bot.keyboards.main import back_kb, cancel_kb
from bot.keyboards.server import (
    add_traffic_kb, server_actions_kb, server_delete_confirm_kb,
    server_list_kb, subproducts_buy_kb,
)
from bot.providers.virtualizor import VirtualizorProvider
from bot.services.billing import BillingService
from bot.services.log_service import LogService
from bot.services.notification import NotificationService
from bot.services.server import ServerService
from bot.utils.loading import answer_loading, edit_loading

router = Router(name="servers")

# تبدیل ارقام لاتین به فارسی برای نمایش مشخصات پلن
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


class BuyServerStates(StatesGroup):
    selecting_category = State()
    selecting_plan = State()
    selecting_billing = State()
    entering_hostname = State()
    selecting_os = State()
    entering_discount = State()
    entering_email = State()
    confirming = State()


class EditServerStates(StatesGroup):
    waiting_ram = State()
    waiting_cpu = State()
    waiting_disk = State()


# ── List servers ──────────────────────────────────────────────────────────────

async def _show_server_list(target_msg, user: User, session: AsyncSession):
    svc = ServerService(session)
    servers = await svc.get_user_servers(user.id)
    if not servers:
        await target_msg.edit_text(
            "شما هیچ سروری ندارید.\nبرای خرید از منوی زیر استفاده کنید:",
            reply_markup=server_list_kb([]),
        )
    else:
        await target_msg.edit_text(
            f'<tg-emoji emoji-id="5345837435601305335">◼</tg-emoji> <b>سرور‌های شما</b> ({len(servers)} سرور):',
            parse_mode="HTML",
            reply_markup=server_list_kb(servers),
        )


@router.callback_query(F.data == "my_servers")
async def cb_my_servers(cb: CallbackQuery, user: User, session: AsyncSession):
    await edit_loading(cb.message)
    await cb.answer()
    await _show_server_list(cb.message, user, session)


@router.message(F.text == "سرور‌های من")
async def msg_my_servers(message: Message, user: User, session: AsyncSession):
    loading = await answer_loading(message)
    await _show_server_list(loading, user, session)


@router.callback_query(F.data.startswith("server:"))
async def cb_server_detail(cb: CallbackQuery, user: User, session: AsyncSession):
    await _render_server_detail(cb, user, session, int(cb.data.split(":")[1]))


async def _render_server_detail(cb: CallbackQuery, user: User, session: AsyncSession, server_id: int):
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return

    # Sync live status from Virtualizor on every open so user always sees current state
    if server.status != ServerStatus.DELETED and server.provider_account_id and server.provider_server_id:
        try:
            svc = ServerService(session)
            await svc.sync_server_status(server)
        except Exception:
            pass

    extra_data = server.extra_data or {}
    is_running = str(extra_data.get("machine_status", "1")) == "1"

    if server.status == ServerStatus.ACTIVE and not is_running:
        status_label = "🔴 خاموش"
    else:
        status_label = {
            ServerStatus.ACTIVE: "🟢 فعال",
            ServerStatus.SUSPENDED: "🔴 ساسپند",
            ServerStatus.BUILDING: "🔨 در حال ساخت",
            ServerStatus.REBUILDING: "🔄 در حال ریبیلد",
            ServerStatus.REBOOTING: "🔄 در حال ریبوت",
            ServerStatus.DELETED: "⚫ حذف شده",
            ServerStatus.PENDING: "⏳ در انتظار",
        }.get(server.status, server.status.value)

    traffic_text = ""
    if server.traffic_limit_gb:
        pct = int(server.traffic_used_gb / server.traffic_limit_gb * 100)
        traffic_text = f"\n• ترافیک: {server.traffic_used_gb:.1f}/{server.traffic_limit_gb:.0f} GB ({pct}%)"

    billing_label = "ساعتی" if server.billing_type == BillingType.HOURLY else "ماهیانه"
    price = server.price_hourly if server.billing_type == BillingType.HOURLY else server.price_monthly
    price_unit = "تومان/ساعت" if server.billing_type == BillingType.HOURLY else "تومان/ماه"

    await cb.message.edit_text(
        f'<tg-emoji emoji-id="5348332751470739727">📃</tg-emoji> <b>نام سرور:</b> {server.name}\n\n'
        f"آیپی: <code>{server.ip_address or 'در حال تخصیص'}</code>\n"
        f"موقعیت: {server.location or 'نامشخص'}\n"
        f"وضعیت: {status_label}\n\n"
        f"• رم: {server.ram} MB | پردازنده: {server.cpu} | دیسک: {server.disk} GB"
        f"{traffic_text}\n"
        f"• {billing_label} — {price:,.0f} {price_unit}\n"
        f"• ساخته شده: {server.created_at.strftime('%Y/%m/%d')}",
        parse_mode="HTML",
        reply_markup=server_actions_kb(server),
    )
    try:
        await cb.answer()
    except Exception:
        pass  # callback may already be answered by the caller (e.g. refresh)


@router.callback_query(F.data.startswith("srv_refresh:"))
async def cb_server_refresh(cb: CallbackQuery, user: User, session: AsyncSession):
    server_id = int(cb.data.split(":")[1])
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    try:
        svc = ServerService(session)
        await svc.sync_server_status(server)
        await cb.answer("✅ بروز شد.")
    except Exception as e:
        await cb.answer(f"خطا: {e}", show_alert=True)
        return
    await _render_server_detail(cb, user, session, server_id)


# ── Server actions ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("srv_action:"))
async def cb_server_action(cb: CallbackQuery, user: User, session: AsyncSession):
    _, server_id_str, action = cb.data.split(":")
    server_id = int(server_id_str)
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return

    if action in ("delete_confirm", "delete") and server.billing_type != BillingType.HOURLY:
        await cb.answer("سرورهای ماهانه قابل حذف توسط کاربر نیستند. با پشتیبانی تماس بگیرید.", show_alert=True)
        return

    if action == "delete_confirm":
        await cb.message.edit_text(
            f"آیا سرور <b>{server.name}</b> حذف شود؟\nاین عمل قابل بازگشت نیست!",
            parse_mode="HTML",
            reply_markup=server_delete_confirm_kb(server_id),
        )
        await cb.answer()
        return

    if action == "restart_confirm":
        await cb.message.edit_text(
            f"آیا از ریبوت سرور <b>{server.name}</b> مطمئن هستید؟\nسرور به مدت چند ثانیه از دسترس خارج می‌شود.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="بله، ریبوت شود", callback_data=f"srv_action:{server_id}:restart", **{"style": "success", "icon_custom_emoji_id": "5206607081334906820"}),
                InlineKeyboardButton(text="انصراف", callback_data=f"server:{server_id}", **{"style": "danger", "icon_custom_emoji_id": "5240241223632954241"}),
            ]]),
        )
        await cb.answer()
        return

    if action == "rebuild_menu":
        account = await session.get(ProviderAccount, server.provider_account_id) if server.provider_account_id else None
        if not account:
            await cb.message.answer("❌ اطلاعات پروایدر یافت نشد.")
            await cb.answer()
            return
        try:
            import asyncio as _ai
            prov = VirtualizorProvider(account.api_endpoint, account.api_key, account.api_secret)
            os_list = await _ai.wait_for(prov.list_os_templates(), timeout=12)
        except Exception as e:
            await cb.message.answer(f"❌ خطا در دریافت لیست OS: {e}")
            await cb.answer()
            return
        builder = InlineKeyboardBuilder()
        for os_item in os_list[:20]:
            builder.button(text=os_item["name"], callback_data=f"srv_rebuild:{server_id}:{os_item['id']}")
        builder.button(text="انصراف", callback_data=f"server:{server_id}", **{"icon_custom_emoji_id": "5240241223632954241"})
        builder.adjust(2)
        await cb.message.edit_text(
            f"🔁 <b>ریبیلد — {server.name}</b>\n\nسیستم‌عامل جدید را انتخاب کنید:",
            parse_mode="HTML",
            reply_markup=builder.as_markup(),
        )
        await cb.answer()
        return

    labels = {"start": "روشن", "stop": "خاموش", "restart": "ریبوت",
              "delete": "حذف", "change_ip": "تغییر IP",
              "suspend": "ساسپند", "unsuspend": "رفع ساسپند"}
    await cb.answer(f"⏳ {labels.get(action, action)}...")

    # restart & delete arrive from a confirmation dialog — remove it so its
    # "✅ بله" button can't be tapped again to re-run the action.
    if action in ("restart", "delete"):
        try:
            await cb.message.delete()
        except Exception:
            pass

    try:
        svc = ServerService(session)
        kwargs = {}
        if action == "suspend":
            kwargs["reason"] = SuspendReason.ADMIN
        ok = await svc.perform_action(server, action, **kwargs)
        label = labels.get(action, action)
        if ok:
            if action == "start":
                msg = '<tg-emoji emoji-id="5895403643863043222">🫥</tg-emoji> سرور با موفقیت روشن شد.'
                _extra = dict(server.extra_data or {})
                _extra["machine_status"] = "1"
                server.extra_data = _extra
                await session.flush()
            elif action == "stop":
                msg = '<tg-emoji emoji-id="5927031220390072917">🔴</tg-emoji> سرور با موفقیت خاموش شد.'
                _extra = dict(server.extra_data or {})
                _extra["machine_status"] = "0"
                server.extra_data = _extra
                await session.flush()
            elif action == "change_ip":
                msg = f"✅ IP جدید: <code>{server.ip_address}</code>"
            else:
                msg = f"✅ {label} با موفقیت انجام شد."
            await cb.message.answer(msg, parse_mode="HTML")
            await LogService(cb.bot, session).log_server_action(user, server, action)
        else:
            await cb.message.answer("❌ عملیات ناموفق بود.")
    except NotImplementedError as e:
        await cb.message.answer(f"⚠️ {e}")
    except Exception as e:
        await cb.message.answer(f"❌ خطا: {e}")


# ── Rebuild OS ───────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("srv_rebuild:"))
async def cb_server_rebuild_confirm(cb: CallbackQuery, user: User, session: AsyncSession):
    parts = cb.data.split(":")
    server_id, os_id = int(parts[1]), parts[2]
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    await cb.message.edit_text(
        f"🔁 <b>تأیید ریبیلد</b>\n\n"
        f"سرور: <b>{server.name}</b>\n\n"
        "⚠️ <b>ریبیلد تمام اطلاعات دیسک را پاک می‌کند!</b>\n"
        "این عمل قابل بازگشت نیست. آیا مطمئن هستید؟",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="بله، ریبیلد شود", callback_data=f"srv_rebuild_do:{server_id}:{os_id}", **{"icon_custom_emoji_id": "5206607081334906820"}),
            InlineKeyboardButton(text="انصراف", callback_data=f"server:{server_id}", **{"icon_custom_emoji_id": "5240241223632954241"}),
        ]]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("srv_rebuild_do:"))
async def cb_server_rebuild_do(cb: CallbackQuery, user: User, session: AsyncSession):
    parts = cb.data.split(":")
    server_id, os_id = int(parts[1]), parts[2]
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    await cb.answer("⏳ ریبیلد شروع می‌شود...")
    # Remove the confirmation message so its "بله، ریبیلد شود" button can't be
    # tapped again — otherwise every tap rebuilds the server from scratch.
    try:
        await cb.message.delete()
    except Exception:
        pass
    try:
        import secrets as _sec
        import string as _str
        _alpha = _str.ascii_letters + _str.digits + "!@#$%^&*"
        new_root_pass = "".join(_sec.choice(_alpha) for _ in range(16))
        svc = ServerService(session)
        ok = await svc.perform_action(server, "rebuild", os_id=os_id, new_password=new_root_pass)
        if ok:
            server.status = ServerStatus.REBUILDING
            _extra = dict(server.extra_data or {})
            _extra["root_password"] = new_root_pass
            server.extra_data = _extra
            await session.flush()
            await cb.message.answer(
                f"✅ <b>ریبیلد شروع شد.</b>\n\n"
                f"🔑 رمز root جدید: <code>{new_root_pass}</code>\n\n"
                "⚠️ این رمز را در جای امنی ذخیره کنید.\n"
                "🔔 چند دقیقه منتظر نصب OS بمانید.",
                parse_mode="HTML",
                reply_markup=back_kb(f"server:{server_id}"),
            )
            await LogService(cb.bot, session).log_server_action(user, server, "rebuild")
        else:
            await cb.message.answer(
                "❌ ریبیلد ناموفق بود.",
                reply_markup=back_kb(f"server:{server_id}"),
            )
    except NotImplementedError as e:
        await cb.message.answer(f"⚠️ {e}")
    except Exception as e:
        await cb.message.answer(f"❌ خطا: {e}")


# ── Traffic ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("srv_traffic:"))
async def cb_server_traffic(cb: CallbackQuery, user: User, session: AsyncSession):
    server_id = int(cb.data.split(":")[1])
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    limit_text = f"{server.traffic_limit_gb:.0f} GB" if server.traffic_limit_gb else "نامحدود"
    pct = int(server.traffic_used_gb / server.traffic_limit_gb * 100) if server.traffic_limit_gb else 0
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    await cb.message.edit_text(
        f"📊 <b>ترافیک — {server.name}</b>\n\n"
        f"مصرف: {server.traffic_used_gb:.2f} GB\n"
        f"حد مجاز: {limit_text}\n"
        f"[{bar}] {pct}%",
        parse_mode="HTML",
        reply_markup=add_traffic_kb(server_id),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("add_traffic:"))
async def cb_do_add_traffic(cb: CallbackQuery, user: User, session: AsyncSession):
    _, server_id_str, gb_str = cb.data.split(":")
    server_id, gb = int(server_id_str), int(gb_str)
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    total = gb * 500
    billing = BillingService(session)
    ok = await billing.debit(user.id, total, server_id=server_id,
                              description=f"خرید {gb}GB ترافیک — {server.name}")
    if not ok:
        await cb.answer("موجودی کافی نیست.", show_alert=True)
        return
    try:
        svc = ServerService(session)
        await svc.perform_action(server, "add_traffic", gb=gb)
        if server.traffic_limit_gb:
            server.traffic_limit_gb += gb
        await cb.answer(f"✅ {gb} GB اضافه شد.")
    except Exception as e:
        await billing.credit(user.id, total, description=f"برگشت وجه ترافیک {gb}GB")
        await cb.answer(f"❌ خطا: {e}", show_alert=True)


# ══════════════════════════════════════════════════════════════════════════════
#  BUY SERVER — category → plan → billing → discount → confirm
# ══════════════════════════════════════════════════════════════════════════════

async def _show_buy_categories(target_msg, user: User, state: FSMContext, session: AsyncSession):
    # فقط دسته‌هایی که حداقل یک محصول فعال (غیرمخفی) دارند
    result = await session.execute(
        select(ServerPlan.category).where(ServerPlan.is_active == True).distinct()
    )
    categories = sorted({row[0] for row in result.all() if row[0]})

    # متادیتای گروه: گروه‌های مخفی حذف، اموجی پریمیوم اضافه.
    # برای هر دسته‌ی بدون ردیف گروه، یک گروه ساخته می‌شود تا دکمه‌ها ID-محور باشند
    # (نام خام در callback_data محدودیت ۶۴ بایتی تلگرام را می‌شکست).
    res = await session.execute(select(ProductGroup))
    groups = {g.name: g for g in res.scalars().all()}
    for cat in categories:
        if cat not in groups:
            g = ProductGroup(name=cat)
            session.add(g)
            groups[cat] = g
    await session.flush()

    entries = []
    for cat in categories:
        g = groups[cat]
        if g.is_hidden:
            continue
        entries.append((g.id, cat, g.emoji_id))

    if not entries:
        await target_msg.edit_text("در حال حاضر هیچ محصولی موجود نیست.", reply_markup=back_kb())
        return
    await state.set_state(BuyServerStates.selecting_category)
    builder = InlineKeyboardBuilder()
    for gid, cat, emoji_id in entries:
        kwargs = {"icon_custom_emoji_id": emoji_id} if emoji_id else {}
        builder.button(text=cat, callback_data=f"buygrp:{gid}", **kwargs)
    builder.button(text="بازگشت به منو", callback_data="cancel", **{"icon_custom_emoji_id": "5258236805890710909"})
    builder.adjust(1)
    await target_msg.edit_text(
        '<tg-emoji emoji-id="5926980668624998964">🟡</tg-emoji> دسته‌بندی مورد نظر را انتخاب کنید:',
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "buy_server")
async def cb_buy_server(cb: CallbackQuery, user: User, state: FSMContext, session: AsyncSession):
    await edit_loading(cb.message)
    await cb.answer()
    await _show_buy_categories(cb.message, user, state, session)


@router.message(F.text == "تهیه سرور")
async def msg_buy_server(message: Message, user: User, state: FSMContext, session: AsyncSession):
    loading = await answer_loading(message)
    await _show_buy_categories(loading, user, state, session)


@router.callback_query(BuyServerStates.selecting_category, F.data.startswith("buygrp:"))
async def cb_select_group(cb: CallbackQuery, user: User, state: FSMContext, session: AsyncSession):
    """ID-based group selection — resolves the group name then shows its plans."""
    group = await session.get(ProductGroup, int(cb.data.split(":")[1]))
    if not group:
        await cb.answer("گروه یافت نشد.", show_alert=True)
        return
    await _select_category(cb, user, state, session, group.name)


@router.callback_query(BuyServerStates.selecting_category, F.data.startswith("buycat:"))
async def cb_select_category(cb: CallbackQuery, user: User, state: FSMContext, session: AsyncSession):
    await _select_category(cb, user, state, session, cb.data[len("buycat:"):])


async def _select_category(cb: CallbackQuery, user: User, state: FSMContext,
                           session: AsyncSession, category: str):
    # گروه مخفی‌شده قابل خرید نیست (حتی از روی کیبورد قدیمی)
    _grp = (await session.execute(
        select(ProductGroup).where(ProductGroup.name == category)
    )).scalar_one_or_none()
    if _grp and _grp.is_hidden:
        await cb.answer("این گروه در حال حاضر در دسترس نیست.", show_alert=True)
        return

    # KYC check for iranian servers
    if "ایران" in category and not user.is_kyc_verified:
        await cb.message.edit_text(
            "🪪 <b>احراز هویت لازم است</b>\n\n"
            "برای خرید سرور ایران، احراز هویت با شاهکار الزامی است.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🪪 احراز هویت", callback_data="start_verify")],
                [InlineKeyboardButton(text="بازگشت به منو", callback_data="cancel", **{"icon_custom_emoji_id": "5258236805890710909"})],
            ]),
        )
        await state.clear()
        await cb.answer()
        return

    await state.update_data(category=category)
    result = await session.execute(
        select(ServerPlan).where(
            ServerPlan.category == category,
            ServerPlan.is_active == True,
        ).order_by(ServerPlan.name)
    )
    plans = list(result.scalars().all())

    if not plans:
        await cb.answer("در این دسته‌بندی محصولی موجود نیست.", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    for plan in plans:
        ram_gb = plan.ram // 1024 if plan.ram >= 1024 else plan.ram
        if plan.bandwidth and plan.bandwidth >= 1000:
            traffic = f"{plan.bandwidth / 1000:g}ترابایت"
        else:
            traffic = f"{plan.bandwidth}گیگ"
        if plan.price_monthly:
            price = f"{plan.price_monthly:,.0f} تومان"
        else:
            price = f"{plan.price_hourly or 0:,.0f} تومان/ساعت"
        label = f"{plan.cpu}هسته | {ram_gb}رم | {traffic} | {price}".translate(_FA_DIGITS)
        builder.button(text=label, callback_data=f"buyplan:{plan.id}", **{"icon_custom_emoji_id": "5260726538302660868"})
    builder.button(text="بازگشت", callback_data="buy_server", **{"icon_custom_emoji_id": "5258236805890710909"})
    builder.adjust(1)

    await state.set_state(BuyServerStates.selecting_plan)
    await cb.message.edit_text(
        '<tg-emoji emoji-id="5926980668624998964">🟡</tg-emoji> یک محصول انتخاب کنید:',
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await cb.answer()


@router.callback_query(BuyServerStates.selecting_plan, F.data.startswith("buyplan:"))
async def cb_select_plan(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    plan_id = int(cb.data.split(":")[1])
    plan = await session.get(ServerPlan, plan_id)
    if not plan:
        await cb.answer("محصول یافت نشد.", show_alert=True)
        return

    await state.update_data(
        plan_id=plan_id,
        provider_account_id=plan.provider_account_id,
    )

    # Determine available billing types
    has_hourly = bool(plan.price_hourly)
    has_monthly = bool(plan.price_monthly)

    if has_hourly and has_monthly:
        await state.set_state(BuyServerStates.selecting_billing)
        await cb.message.edit_text(
            f"نوع بیلینگ را انتخاب کنید:\nپلن: {plan.display_name or plan.name}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text=f"ساعتی — {plan.price_hourly:,.0f} تومان", callback_data="buybilling:hourly", **{"icon_custom_emoji_id": "5798535677318533269"}),
                    InlineKeyboardButton(text=f"ماهانه — {plan.price_monthly:,.0f} تومان", callback_data="buybilling:monthly", **{"icon_custom_emoji_id": "5778496382117613636"}),
                ],
                [InlineKeyboardButton(text="انصراف", callback_data="cancel", **{"icon_custom_emoji_id": "5240241223632954241", "style": "danger"})],
            ]),
        )
    elif has_hourly:
        await state.update_data(billing="hourly")
        await _ask_hostname(cb, state)
    else:
        await state.update_data(billing="monthly")
        await _ask_hostname(cb, state)

    await cb.answer()


@router.callback_query(BuyServerStates.selecting_billing, F.data.startswith("buybilling:"))
async def cb_select_billing(cb: CallbackQuery, state: FSMContext):
    billing = cb.data.split(":")[1]
    await state.update_data(billing=billing)
    await _ask_hostname(cb, state)
    await cb.answer()


# ── Hostname + OS selection ───────────────────────────────────────────────────

async def _ask_hostname(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BuyServerStates.entering_hostname)
    await cb.message.edit_text(
        "یک اسم برای سرور خود انتخاب کنید.\n\n"
        '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> فقط حروف کوچک، اعداد و خط تیره مجاز است.\n'
        "یا دکمه زیر را بزنید تا سیستم خودکار انتخاب کند:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="خودکار", callback_data="buyhost:auto", **{"icon_custom_emoji_id": "6039614175917903752"})],
            [InlineKeyboardButton(text="بازگشت به منو", callback_data="cancel", **{"icon_custom_emoji_id": "5258236805890710909"})],
        ]),
    )


@router.callback_query(BuyServerStates.entering_hostname, F.data == "buyhost:auto")
async def cb_hostname_auto(cb: CallbackQuery, user: User, state: FSMContext, session: AsyncSession):
    await state.update_data(hostname=None)
    await _ask_os(cb, state, session, user)
    await cb.answer()


@router.message(BuyServerStates.entering_hostname)
async def msg_hostname(message: Message, user: User, state: FSMContext, session: AsyncSession):
    import re
    raw = message.text.strip().lower()
    if not re.match(r'^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$', raw):
        await message.answer(
            "❌ اسم سرور نامعتبر است.\n"
            "فقط حروف کوچک (a-z)، اعداد و خط تیره (-) مجاز است.\n"
            "باید با حرف یا عدد شروع و تموم بشه:"
        )
        return
    await state.update_data(hostname=raw)
    await _ask_os_message(message, state, session, user)


async def _ask_os(cb: CallbackQuery, state: FSMContext, session: AsyncSession, user: User):
    data = await state.get_data()
    account = await session.get(ProviderAccount, data.get("provider_account_id")) if data.get("provider_account_id") else None

    os_list = []
    if account:
        try:
            import asyncio
            prov = VirtualizorProvider(account.api_endpoint, account.api_key, account.api_secret)
            os_list = await asyncio.wait_for(prov.list_os_templates(), timeout=10)
        except Exception:
            pass

    if not os_list:
        plan_id = data.get("plan_id")
        plan_osid = None
        if plan_id:
            from sqlalchemy import select as _select
            from bot.database.models import ServerPlan as _ServerPlan
            _plan = await session.get(_ServerPlan, plan_id)
            if _plan and (_plan.extra_data or {}).get("osid"):
                plan_osid = str(_plan.extra_data["osid"])
        await state.update_data(os_id=plan_osid or "", os_name="پیش‌فرض")
        if data.get("billing") == "hourly":
            await state.update_data(discount_id=None, discount_percent=0)
            await _ask_email_or_confirm(cb.message, state, session, user)
        else:
            await _ask_discount(cb, state)
        return

    os_name_map = {str(o["id"]): o["name"] for o in os_list[:20]}
    await state.update_data(os_options=os_name_map)
    await state.set_state(BuyServerStates.selecting_os)

    builder = InlineKeyboardBuilder()
    for os_item in os_list[:20]:
        builder.button(text=os_item["name"], callback_data=f"buyos:{os_item['id']}")
    builder.button(text="بازگشت به منو", callback_data="cancel", **{"icon_custom_emoji_id": "5258236805890710909"})
    builder.adjust(2)

    await cb.message.edit_text(
        '<tg-emoji emoji-id="4916105371858240403">🖱</tg-emoji> یک OS برای سرور انتخاب کنید',
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


async def _ask_os_message(message: Message, state: FSMContext, session: AsyncSession, user: User):
    data = await state.get_data()
    account = await session.get(ProviderAccount, data.get("provider_account_id")) if data.get("provider_account_id") else None

    os_list = []
    if account:
        try:
            import asyncio
            prov = VirtualizorProvider(account.api_endpoint, account.api_key, account.api_secret)
            os_list = await asyncio.wait_for(prov.list_os_templates(), timeout=10)
        except Exception:
            pass

    if not os_list:
        plan_id = data.get("plan_id")
        plan_osid = None
        if plan_id:
            from sqlalchemy import select as _select
            from bot.database.models import ServerPlan as _ServerPlan
            _plan = await session.get(_ServerPlan, plan_id)
            if _plan and (_plan.extra_data or {}).get("osid"):
                plan_osid = str(_plan.extra_data["osid"])
        await state.update_data(os_id=plan_osid or "", os_name="پیش‌فرض")
        if data.get("billing") == "hourly":
            await state.update_data(discount_id=None, discount_percent=0)
            await _ask_email_or_confirm(message, state, session, user, from_message=True)
        else:
            await state.set_state(BuyServerStates.entering_discount)
            await message.answer(
                "🏷 <b>کد تخفیف</b>\n\n"
                "اگر کد تخفیف دارید وارد کنید یا دکمه زیر:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⏭ بدون کد تخفیف", callback_data="buydisc:skip")],
                    [InlineKeyboardButton(text="انصراف", callback_data="cancel", **{"style": "danger", "icon_custom_emoji_id": "5240241223632954241"})],
                ]),
            )
        return

    os_name_map = {str(o["id"]): o["name"] for o in os_list[:20]}
    await state.update_data(os_options=os_name_map)
    await state.set_state(BuyServerStates.selecting_os)
    builder = InlineKeyboardBuilder()
    for os_item in os_list[:20]:
        builder.button(text=os_item["name"], callback_data=f"buyos:{os_item['id']}")
    builder.button(text="بازگشت به منو", callback_data="cancel", **{"icon_custom_emoji_id": "5258236805890710909"})
    builder.adjust(2)

    await message.answer(
        '<tg-emoji emoji-id="4916105371858240403">🖱</tg-emoji> یک OS برای سرور انتخاب کنید',
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(BuyServerStates.selecting_os, F.data.startswith("buyos:"))
async def cb_select_os(cb: CallbackQuery, user: User, state: FSMContext, session: AsyncSession):
    os_id = cb.data[len("buyos:"):]
    data = await state.get_data()
    os_options = data.get("os_options", {})
    os_name = os_options.get(str(os_id), os_id) if isinstance(os_options, dict) else os_id
    await state.update_data(os_id=os_id, os_name=os_name)
    if data.get("billing") == "hourly":
        await state.update_data(discount_id=None, discount_percent=0)
        await _ask_email_or_confirm(cb.message, state, session, user)
    else:
        await _ask_discount(cb, state)
    await cb.answer()


async def _ask_discount(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BuyServerStates.entering_discount)
    await cb.message.edit_text(
        "🏷 <b>کد تخفیف</b>\n\n"
        "اگر کد تخفیف دارید وارد کنید.\n"
        "در غیر این صورت /skip بزنید یا دکمه زیر را بزنید:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ بدون کد تخفیف", callback_data="buydisc:skip")],
            [InlineKeyboardButton(text="انصراف", callback_data="cancel", **{"style": "danger", "icon_custom_emoji_id": "5240241223632954241"})],
        ]),
    )


async def _ask_email_or_confirm(msg, state: FSMContext, session, user: User, from_message=False):
    """If user has no email, collect it; otherwise go straight to confirmation."""
    if user.email:
        await _show_confirm(msg, state, session, from_message=from_message)
    else:
        await state.set_state(BuyServerStates.entering_email)
        text = (
            '‏<tg-emoji emoji-id="5348348681504441752">📧</tg-emoji> <b>ایمیل</b>\n\n'
            "لطفا برای ساخت سرور، ایمیل خود را وارد کنید"
        )
        if from_message:
            await msg.answer(text, parse_mode="HTML")
        else:
            await msg.edit_text(text, parse_mode="HTML")


@router.message(BuyServerStates.entering_email)
async def msg_enter_email(message: Message, user: User, state: FSMContext, session: AsyncSession):
    import re
    email = message.text.strip().lower()
    if not re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email):
        await message.answer("❌ ایمیل نامعتبر است. لطفاً یک ایمیل معتبر وارد کنید:")
        return
    user.email = email
    await session.flush()
    await _show_confirm(message, state, session, from_message=True)


@router.callback_query(BuyServerStates.entering_discount, F.data == "buydisc:skip")
async def cb_discount_skip(cb: CallbackQuery, user: User, state: FSMContext, session: AsyncSession):
    await state.update_data(discount_id=None, discount_percent=0)
    await _ask_email_or_confirm(cb.message, state, session, user)
    await cb.answer()


@router.message(BuyServerStates.entering_discount)
async def msg_discount_code(message: Message, user: User, state: FSMContext, session: AsyncSession):
    raw = message.text.strip().upper()

    if raw in ("/SKIP", "SKIP"):
        await state.update_data(discount_id=None, discount_percent=0)
        await _ask_email_or_confirm(message, state, session, user, from_message=True)
        return

    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(DiscountCode).where(
            DiscountCode.code == raw,
            DiscountCode.is_active == True,
        )
    )
    code = result.scalar_one_or_none()

    if not code:
        await message.answer("❌ کد تخفیف نامعتبر است.")
        return
    if code.expires_at and code.expires_at < now:
        await message.answer("❌ کد تخفیف منقضی شده.")
        return
    if code.max_uses and code.use_count >= code.max_uses:
        await message.answer("❌ ظرفیت این کد تخفیف پر شده.")
        return

    await state.update_data(discount_id=code.id, discount_percent=code.discount_percent)
    await message.answer(f"✅ کد تخفیف <b>{code.code}</b> — {code.discount_percent:.0f}% اعمال شد!", parse_mode="HTML")
    await _ask_email_or_confirm(message, state, session, user, from_message=True)


async def _show_confirm(msg, state: FSMContext, session, from_message=False, user_balance_tg_id=None):
    data = await state.get_data()
    plan = await session.get(ServerPlan, data["plan_id"])
    if not plan:
        await msg.answer("محصول یافت نشد.")
        await state.clear()
        return

    billing = data["billing"]
    base_price = plan.price_hourly if billing == "hourly" else plan.price_monthly
    discount_pct = data.get("discount_percent", 0)
    final_price = base_price * (1 - discount_pct / 100) if discount_pct else base_price
    price_unit = "تومان/ساعت" if billing == "hourly" else "تومان/ماه"

    await state.set_state(BuyServerStates.confirming)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=" ", callback_data="confirm_purchase", **{"icon_custom_emoji_id": "5206607081334906820", "style": "success"}),
            InlineKeyboardButton(text=" ", callback_data="cancel", **{"icon_custom_emoji_id": "5210952531676504517", "style": "danger"}),
        ]
    ])

    hostname_line = f"• نام سرور: {data['hostname']}\n" if data.get("hostname") else ""
    os_line = f"• سیستم‌عامل: {data.get('os_name', '')}\n" if data.get("os_name") else ""
    discount_line = f"• تخفیف: {discount_pct:.0f}% (قیمت اصلی: {base_price:,.0f} T)\n" if discount_pct else ""

    text = (
        f'<tg-emoji emoji-id="4987757216040747796">💎</tg-emoji> <b>تأیید سفارش</b>\n\n'
        f"• پلن: {plan.display_name or plan.name}\n"
        f"• ارائه دهنده: {plan.category or ''}\n"
        f"• رم: {plan.ram} MB | پردازنده: {plan.cpu} | دیسک: {plan.disk} GB\n"
        f"• ترافیک: {plan.bandwidth} GB\n"
        f"• موقعیت: {plan.location or 'نامشخص'}\n"
        f"{hostname_line}"
        f"{os_line}\n"
        f"{discount_line}"
        f"• قیمت نهایی: <b>{final_price:,.0f} {price_unit}</b>\n\n"
        "آیا تأیید می‌کنید؟"
    )

    if from_message:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(BuyServerStates.confirming, F.data == "confirm_purchase")
async def cb_confirm_purchase(cb: CallbackQuery, user: User, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    plan = await session.get(ServerPlan, data["plan_id"])
    if not plan:
        await cb.answer("محصول یافت نشد.", show_alert=True)
        return

    billing_str = data["billing"]
    billing_type = BillingType.HOURLY if billing_str == "hourly" else BillingType.MONTHLY
    base_price = plan.price_hourly if billing_type == BillingType.HOURLY else plan.price_monthly
    discount_pct = data.get("discount_percent", 0)
    final_price = base_price * (1 - discount_pct / 100) if discount_pct else base_price

    if user.balance < (final_price or 0):
        await cb.answer("موجودی کافی نیست. کیف پول را شارژ کنید.", show_alert=True)
        return

    if billing_type == BillingType.HOURLY:
        max_hourly = (user.extra_data or {}).get("max_hourly_servers", 5)
        hourly_count = (await session.execute(
            select(func.count(Server.id)).where(
                Server.user_id == user.id,
                Server.billing_type == BillingType.HOURLY,
                Server.status != ServerStatus.DELETED,
            )
        )).scalar() or 0
        if hourly_count >= max_hourly:
            await cb.answer(
                f"⛔ شما به حداکثر {max_hourly} سرور ساعتی همزمان رسیده‌اید.",
                show_alert=True,
            )
            return

    await cb.message.edit_text("⏳ در حال ساخت سرور...")
    await cb.answer()
    await state.clear()

    # Mark discount as used
    if data.get("discount_id"):
        code = await session.get(DiscountCode, data["discount_id"])
        if code:
            code.use_count += 1
            await session.flush()

    hostname = data.get("hostname") or f"srv-{user.telegram_id}"
    os_id = data.get("os_id") or ""
    import secrets as _secrets
    import string as _string
    _alpha = _string.ascii_letters + _string.digits + "!@#$%^&*"
    root_password = "".join(_secrets.choice(_alpha) for _ in range(16))

    try:
        svc = ServerService(session)
        server = await svc.create_server(
            user=user, plan=plan, os_id=os_id,
            billing_type=billing_type,
            hostname=hostname,
            extra={"root_password": root_password},
        )
        billing = BillingService(session)
        await billing.debit(user.id, final_price or 0, server_id=server.id,
                            description=f"خرید سرور {server.name}")
        # Persist root password so it can be displayed again from panel
        _extra = dict(server.extra_data or {})
        _extra["root_password"] = root_password
        server.extra_data = _extra
        await session.flush()

        plan_name = plan.display_name or plan.name
        delivery = (
            f'<tg-emoji emoji-id="5397916757333654639">➕</tg-emoji> <b>سرور {server.name} آماده است!</b>\n\n'
            f"• پلن: {plan_name}\n"
            f"• آیپی: <code>{server.ip_address or 'در حال تخصیص...'}</code>\n"
            f"• پسورد: <code>{root_password}</code>\n"
            f"\n• این اطلاعات را در جای امنی ذخیره کنید.\n"
            "• سیستم‌عامل در حال نصب است — چند دقیقه منتظر بمانید."
        )
        await cb.message.edit_text(delivery, parse_mode="HTML")
        await LogService(cb.bot, session).log_purchase(
            user, server, plan_name, billing_str, final_price or 0
        )
    except Exception as e:
        await cb.message.edit_text(f"❌ خطا در ساخت سرور: {e}\nبا پشتیبانی تماس بگیرید.")


# ── Change IP ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("srv_changeip:"))
async def cb_change_ip_confirm(cb: CallbackQuery, user: User, session: AsyncSession):
    server_id = int(cb.data.split(":")[1])
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    if server.status != ServerStatus.ACTIVE:
        await cb.answer("سرور باید فعال باشد.", show_alert=True)
        return

    account = await session.get(ProviderAccount, server.provider_account_id) if server.provider_account_id else None
    fee = float((account.extra_config or {}).get("change_ip_fee", 0) or 0) if account else 0

    if fee > 0 and user.balance < fee:
        await cb.answer(f"موجودی کافی نیست. هزینه تغییر IP: {fee:,.0f} تومان — موجودی: {user.balance:,.0f} تومان", show_alert=True)
        return

    fee_text = f"{fee:,.0f} تومان" if fee > 0 else "رایگان"
    await cb.message.edit_text(
        f'<tg-emoji emoji-id="5895403643863043222">🫥</tg-emoji> <b>تأیید تغییر آیپی</b>\n\n'
        f"آیپی فعلی = <code>{server.ip_address or 'نامشخص'}</code>\n"
        f"هزینه: <b>{fee_text}</b>\n\n"
        "سرور پس از تغییر IP ریبوت خواهد شد.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="تأیید تغییر IP", callback_data=f"srv_changeip_do:{server_id}", **{"style": "success", "icon_custom_emoji_id": "5206607081334906820"}),
            InlineKeyboardButton(text="انصراف", callback_data=f"server:{server_id}", **{"style": "danger", "icon_custom_emoji_id": "5240241223632954241"}),
        ]]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("srv_changeip_do:"))
async def cb_change_ip_do(cb: CallbackQuery, user: User, session: AsyncSession):
    server_id = int(cb.data.split(":")[1])
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    if server.status != ServerStatus.ACTIVE:
        await cb.answer("سرور باید فعال باشد.", show_alert=True)
        return

    account = await session.get(ProviderAccount, server.provider_account_id) if server.provider_account_id else None
    if not account:
        await cb.answer("اطلاعات پروایدر یافت نشد.", show_alert=True)
        return

    fee = float((account.extra_config or {}).get("change_ip_fee", 0) or 0)

    billing = BillingService(session)
    if fee > 0:
        ok = await billing.debit(user.id, fee, server_id=server_id, description=f"تغییر IP — {server.name}")
        if not ok:
            await cb.answer("موجودی کافی نیست.", show_alert=True)
            return

    await cb.answer("⏳ در حال تغییر IP...")
    # Remove the confirmation dialog so its "✅ تأیید" button can't be re-tapped.
    try:
        await cb.message.delete()
    except Exception:
        pass
    wait = await cb.message.answer("⏳ در حال تغییر IP...")
    try:
        prov = VirtualizorProvider(account.api_endpoint, account.api_key, account.api_secret)
        old_ip = server.ip_address
        new_ip = await prov.change_ip(server.provider_server_id)
        server.ip_address = new_ip
        await session.flush()

        # Restart so the new IP takes effect inside the OS
        try:
            await prov.restart_server(server.provider_server_id)
        except Exception:
            pass

        fee_text = f"\n💸 هزینه کسر شد: {fee:,.0f} تومان" if fee > 0 else ""
        await wait.edit_text(
            f'<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> <b>آیپی تغییر کرد</b>\n\n'
            f"• آیپی قبلی: <code>{old_ip or 'نامشخص'}</code>\n"
            f"• آیپی جدید: <code>{new_ip}</code>{fee_text}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="بازگشت", callback_data=f"server:{server_id}", **{"icon_custom_emoji_id": "5258236805890710909"}),
            ]]),
        )
        await LogService(cb.bot, session).log_ip_change(user, server, old_ip, new_ip)
    except Exception as e:
        if fee > 0:
            await billing.credit(user.id, fee, description=f"برگشت وجه تغییر IP — {server.name}")
        await wait.edit_text(
            f"❌ <b>تغییر IP ناموفق بود:</b> {e}\n\nوجه برگشت داده شد.",
            parse_mode="HTML",
            reply_markup=back_kb(f"server:{server_id}"),
        )


# ── Mute hourly billing notification ─────────────────────────────────────────

@router.callback_query(F.data.startswith("srv_mute_hourly:"))
async def cb_mute_hourly(cb: CallbackQuery, user: User, session: AsyncSession):
    server_id = int(cb.data.split(":")[1])
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    extra = dict(server.extra_data or {})
    extra["hourly_notify"] = False
    server.extra_data = extra
    await session.commit()
    await cb.answer("✅ اطلاع‌رسانی ساعتی این سرور خاموش شد.", show_alert=True)


# ── Sub-products ───────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("srv_subproducts:"))
async def cb_server_subproducts(cb: CallbackQuery, user: User, session: AsyncSession):
    server_id = int(cb.data.split(":")[1])
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return

    if not server.plan_id:
        await cb.answer("این سرور پلن مشخصی ندارد.", show_alert=True)
        return

    result = await session.execute(
        select(SubProduct).where(
            SubProduct.plan_id == server.plan_id,
            SubProduct.is_active == True,
        ).order_by(SubProduct.name)
    )
    subs = list(result.scalars().all())

    if not subs:
        await cb.answer("خدمات اضافه‌ای برای این سرور موجود نیست.", show_alert=True)
        return

    await cb.message.edit_text(
        f"📦 <b>خدمات اضافه — {server.name}</b>\n\n"
        f"💰 موجودی شما: {user.balance:,.0f} تومان\n\n"
        "یک گزینه را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=subproducts_buy_kb(server_id, subs),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("buy_subprod:"))
async def cb_buy_subprod(cb: CallbackQuery, user: User, session: AsyncSession):
    parts = cb.data.split(":")
    server_id, sp_id = int(parts[1]), int(parts[2])
    server = await session.get(Server, server_id)
    sp = await session.get(SubProduct, sp_id)

    if not server or server.user_id != user.id or not sp:
        await cb.answer("یافت نشد.", show_alert=True)
        return

    if user.balance < sp.price:
        await cb.answer(f"موجودی کافی نیست. نیاز: {sp.price:,.0f}T — موجودی: {user.balance:,.0f}T", show_alert=True)
        return

    billing = BillingService(session)
    ok = await billing.debit(user.id, sp.price, server_id=server_id, description=f"خرید {sp.name} — {server.name}")
    if not ok:
        await cb.answer("موجودی کافی نیست.", show_alert=True)
        return

    try:
        svc = ServerService(session)
        if sp.type == SubProductType.TRAFFIC:
            await svc.perform_action(server, "add_traffic", gb=int(sp.value))
            if server.traffic_limit_gb:
                server.traffic_limit_gb += sp.value
        await session.flush()
        await cb.answer(f"✅ {sp.name} فعال شد!", show_alert=True)
        await _render_server_detail(cb, user, session, server_id)
    except Exception as e:
        # refund on failure
        await billing.credit(user.id, sp.price, description=f"برگشت وجه {sp.name}")
        await cb.answer(f"❌ خطا: {e}", show_alert=True)


# ── Change root password ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("srv_chpass:"))
async def cb_change_password_confirm(cb: CallbackQuery, user: User, session: AsyncSession):
    server_id = int(cb.data.split(":")[1])
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    if server.status != ServerStatus.ACTIVE:
        await cb.answer("سرور باید فعال باشد.", show_alert=True)
        return
    await cb.message.edit_text(
        '<tg-emoji emoji-id="5256248974767046755">🔒</tg-emoji> رمز جدید به صورت خودکار ایجاد می‌شود.\n'
        "سرور پس از تغییر رمز ریبوت خواهد شد.\n\n"
        "آیا مطمئن هستید؟",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="بله، رمز تغییر شود", callback_data=f"srv_chpass_do:{server_id}", **{"style": "success", "icon_custom_emoji_id": "5206607081334906820"}),
            InlineKeyboardButton(text="انصراف", callback_data=f"server:{server_id}", **{"style": "danger", "icon_custom_emoji_id": "5240241223632954241"}),
        ]]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("srv_chpass_do:"))
async def cb_change_password_do(cb: CallbackQuery, user: User, session: AsyncSession):
    server_id = int(cb.data.split(":")[1])
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    if server.status != ServerStatus.ACTIVE:
        await cb.answer("سرور باید فعال باشد.", show_alert=True)
        return

    import secrets as _secrets
    import string as _string
    _alphabet = _string.ascii_letters + _string.digits + "!@#$%^&*"
    new_password = "".join(_secrets.choice(_alphabet) for _ in range(16))

    await cb.answer("⏳ در حال تغییر رمز...")
    # Remove the confirmation dialog so its "✅ بله" button can't be re-tapped.
    try:
        await cb.message.delete()
    except Exception:
        pass
    wait = await cb.message.answer('<tg-emoji emoji-id="5427181942934088912">💬</tg-emoji> در حال تغییر رمز root...', parse_mode="HTML")
    try:
        svc = ServerService(session)
        ok = await svc.perform_action(server, "change_password", password=new_password)
        if ok:
            account = await session.get(ProviderAccount, server.provider_account_id) if server.provider_account_id else None
            if account:
                try:
                    prov = VirtualizorProvider(account.api_endpoint, account.api_key, account.api_secret)
                    await prov.restart_server(server.provider_server_id)
                except Exception:
                    pass
            await wait.edit_text(
                f"<b>رمز root تغییر کرد</b>\n\n"
                f'<tg-emoji emoji-id="5256248974767046755">🔒</tg-emoji> رمز جدید: <code>{new_password}</code>\n\n'
                "سرور در حال ریبوت است.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="بازگشت", callback_data=f"server:{server_id}", **{"icon_custom_emoji_id": "5258236805890710909"}),
                ]]),
            )
            await LogService(cb.bot, session).log_server_action(user, server, "change_password")
        else:
            await wait.edit_text(
                "❌ تغییر رمز ناموفق بود.",
                reply_markup=back_kb(f"server:{server_id}"),
            )
    except Exception as e:
        await wait.edit_text(
            f"❌ <b>خطا:</b> {e}",
            parse_mode="HTML",
            reply_markup=back_kb(f"server:{server_id}"),
        )


# ── Edit server hardware ──────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("srv_edit:"))
async def cb_srv_edit(cb: CallbackQuery, user: User, session: AsyncSession, state: FSMContext):
    server_id = int(cb.data.split(":")[1])
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    await state.update_data(server_id=server_id)
    await state.set_state(EditServerStates.waiting_ram)
    await cb.message.edit_text(
        f"⚙️ <b>ویرایش {server.name}</b>\n\n"
        f"RAM فعلی: {server.ram} MB\n"
        "مقدار جدید RAM (MB) یا 0 برای بدون تغییر:",
        parse_mode="HTML",
        reply_markup=cancel_kb(),
    )
    await cb.answer()


@router.message(EditServerStates.waiting_ram, F.text.regexp(r"^\d+$"))
async def edit_ram(message: Message, state: FSMContext):
    await state.update_data(ram=int(message.text) or None)
    await state.set_state(EditServerStates.waiting_cpu)
    await message.answer("CPU جدید (0 = بدون تغییر):")


@router.message(EditServerStates.waiting_cpu, F.text.regexp(r"^\d+$"))
async def edit_cpu(message: Message, state: FSMContext):
    await state.update_data(cpu=int(message.text) or None)
    await state.set_state(EditServerStates.waiting_disk)
    await message.answer("Disk (GB) جدید (0 = بدون تغییر):")


@router.message(EditServerStates.waiting_disk, F.text.regexp(r"^\d+$"))
async def edit_disk(message: Message, user: User, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()
    server = await session.get(Server, data["server_id"])
    if not server or server.user_id != user.id:
        await message.answer("سرور یافت نشد.")
        return
    kwargs = {k: v for k, v in {
        "ram": data.get("ram"),
        "cpu": data.get("cpu"),
        "disk": int(message.text) or None,
    }.items() if v}
    if not kwargs:
        await message.answer("هیچ تغییری اعمال نشد.")
        return
    try:
        svc = ServerService(session)
        ok = await svc.perform_action(server, "edit", **kwargs)
        await message.answer("✅ سخت‌افزار ویرایش شد." if ok else "❌ ویرایش ناموفق بود.")
    except NotImplementedError:
        await message.answer("⚠️ این پروایدر ویرایش آنلاین را پشتیبانی نمی‌کند.")
    except Exception as e:
        await message.answer(f"❌ خطا: {e}")
