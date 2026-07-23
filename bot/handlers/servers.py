"""Server management handlers — Virtualizor only, category-based buying with discount codes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import (
    BillingType, DiscountCode, ProductGroup, ProviderAccount, ProviderType,
    Server, ServerPlan, ServerStatus, SubProduct, SubProductType, SuspendReason, User,
    plan_sort_key,
)
from bot.keyboards.main import back_kb, cancel_kb
from bot.keyboards.server import (
    add_traffic_kb, server_actions_kb, server_delete_confirm_kb,
    server_list_kb, subproducts_buy_kb,
)
from bot.providers import get_provider
from bot.providers.virtualizor import VirtualizorProvider
from bot.services.billing import BillingService
from bot.services.currency import obj_currency, server_live_price, to_toman
from bot.services.log_service import LogService
from bot.services.notification import NotificationService
from bot.services.server import ServerService
from bot.utils.loading import ERR, WARN, answer_loading, edit_loading

import html as _html


def _esc(v) -> str:
    """HTML-escape متن exception قبل از قرارگیری در پیام parse_mode=HTML."""
    return _html.escape(str(v))

router = Router(name="servers")

# تبدیل ارقام لاتین به فارسی برای نمایش مشخصات پلن
_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

# لوکیشن‌های هتزنر: (اموجی پریمیوم پرچم، لیبل فارسی) — چیدمان اختصاصی خودمان
_HZ_LOC_META = {
    "fsn1": ("5409360418520967565", "Falkenstein"),
    "nbg1": ("5409360418520967565", "Nuremberg"),
    "hel1": ("5382151560182642075", "Helsinki"),
    "ash":  ("5927292517610426176", "Ashburn"),
    "hil":  ("5927292517610426176", "Hillsboro"),
    "sin":  ("5292144120993686909", "Singapore"),
}


# نوع flavor جیکور از توکن دوم ID («g2-standard-…» → standard) — مرحله «نوع سرور»
_GC_TYPE_LABELS = {
    "standard": "Standard",
    "cpu": "CPU Optimized",
    "highfreq": "High Frequency",
    "net": "Network Optimized",
    "gpu": "GPU",
}
_GC_TYPE_ORDER = ["standard", "cpu", "highfreq", "net", "gpu"]


def _gc_flavor_type(pid: str) -> str:
    parts = (pid or "").split("-")
    return (parts[1] if len(parts) > 1 else (parts[0] or "other")).lower()


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

    # فیکس حباب وضعیت: لیست هم مثل صفحه‌ی جزئیات، وضعیت زنده را sync می‌کند —
    # ولی فقط برای سرورهای «گذرا» (تازه‌ساخته/ریبیلد/ریبوت) تا لیست سنگین نشود.
    import asyncio as _aio
    _transitional = (ServerStatus.PENDING, ServerStatus.BUILDING,
                     ServerStatus.REBUILDING, ServerStatus.REBOOTING)
    for s in servers:
        if s.status in _transitional and s.provider_server_id:
            try:
                await _aio.wait_for(svc.sync_server_status(s), timeout=8)
            except Exception:
                pass
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
    price_unit = "تومان/ساعت" if server.billing_type == BillingType.HOURLY else "تومان/ماه"
    # قیمت لحظه‌ای از خودِ پلن (تغییر قیمت پلن فوراً همین‌جا دیده می‌شود)؛
    # قیمت ارزی با نرخ روز (آپدیت هر ۸ ساعت) به ریال تبدیل می‌شود
    price, _cur = await server_live_price(session, server,
                                          hourly=server.billing_type == BillingType.HOURLY)
    if _cur != "irt" and price:
        _toman = await to_toman(session, price, _cur)
        if _toman > 0:
            price = _toman

    _extra_ips = (server.extra_data or {}).get("extra_ips") or []
    extra_ip_line = "".join(f"آیپی اضافه: <code>{ip}</code>\n" for ip in _extra_ips)

    await cb.message.edit_text(
        f'<tg-emoji emoji-id="5348332751470739727">📃</tg-emoji> <b>نام سرور:</b> {server.name}\n\n'
        f"آیپی: <code>{server.ip_address or 'در حال تخصیص'}</code>\n"
        f"{extra_ip_line}"
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
        # جیکور برای VM اندپوینت rebuild ندارد (GCORE.md بخش ز) — دکمه هم در
        # کیبورد گارد شده؛ این گارد برای کیبوردهای قدیمی باقی‌مانده در چت است
        if server.provider_type == ProviderType.GCORE:
            await cb.answer("نصب مجدد OS برای این سرویس‌دهنده در دسترس نیست.", show_alert=True)
            return
        account = await session.get(ProviderAccount, server.provider_account_id) if server.provider_account_id else None
        if not account:
            await cb.message.answer(f"{ERR} اطلاعات پروایدر یافت نشد.", parse_mode="HTML")
            await cb.answer()
            return
        try:
            import asyncio as _ai
            prov = get_provider(account)
            os_list = await _ai.wait_for(prov.list_os_templates(), timeout=15)
        except Exception as e:
            await cb.message.answer(f"{ERR} خطا در دریافت لیست OS: {_esc(e)}", parse_mode="HTML")
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

    # ── گارد فعال‌سازی (unsuspend) بر اساس دلیل تعلیق — ضد دور زدن ──
    if action == "unsuspend":
        reason = server.suspend_reason
        if reason == SuspendReason.TRAFFIC_EXCEEDED:
            await cb.answer(
                "ترافیک این سرویس تمام شده است — پس از خرید ترافیک، سرویس فعال می‌شود.",
                show_alert=True,
            )
            return
        if reason == SuspendReason.ADMIN:
            await cb.answer(
                "این سرویس توسط مدیریت تعلیق شده — با پشتیبانی در تماس باشید.",
                show_alert=True,
            )
            return
        if reason == SuspendReason.EXPIRED:
            # فعال‌سازی = تمدید ماهانه با قیمت روز؛ بدون پرداخت خبری از روشن‌شدن نیست
            billing = BillingService(session)
            if not await billing.charge_monthly(server):
                await cb.answer("موجودی برای تمدید ماهانه کافی نیست. کیف پول را شارژ کنید.", show_alert=True)
                return
            server.expires_at = datetime.now(timezone.utc) + timedelta(days=30)
            await session.flush()
        if reason == SuspendReason.LOW_BALANCE and server.billing_type == BillingType.HOURLY:
            # حداقل اعتبارِ یک ساعت لازم است تا بلافاصله دوباره ساسپند نشود
            _amt, _cur = await server_live_price(session, server, hourly=True)
            _need = _amt if _cur == "irt" else await to_toman(session, _amt, _cur)
            if _need and user.balance < _need:
                await cb.answer(
                    f"برای فعال‌سازی حداقل اعتبار یک ساعت ({_need:,.0f} تومان) لازم است.",
                    show_alert=True,
                )
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
            elif action == "delete":
                msg = (
                    f'‏<tg-emoji emoji-id="5258503720928288433">🔔</tg-emoji> '
                    f"سرویس {server.name} با هاست‌نیم <code>{server.hostname or server.name}</code> "
                    "با موفقیت حذف شد\n\n"
                    '‎<tg-emoji emoji-id="5258093637450866522">🤖</tg-emoji> @abrmakerbot'
                )
            else:
                msg = f"✅ {label} با موفقیت انجام شد."
            await cb.message.answer(msg, parse_mode="HTML")
            await LogService(cb.bot, session).log_server_action(user, server, action)
        else:
            await cb.message.answer(f"{ERR} عملیات ناموفق بود.", parse_mode="HTML")
    except NotImplementedError as e:
        await cb.message.answer(f"{WARN} {_esc(e)}", parse_mode="HTML")
    except Exception as e:
        await cb.message.answer(f"{ERR} خطا: {_esc(e)}", parse_mode="HTML")


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
        f"{WARN} <b>ریبیلد تمام اطلاعات دیسک را پاک می‌کند!</b>\n"
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
            # هتزنر رمز دلخواه نمی‌پذیرد — رمز واقعی از سرویس (رمز تولیدی سرویس‌دهنده)
            real_pass = svc.last_root_password or new_root_pass
            server.status = ServerStatus.REBUILDING
            _extra = dict(server.extra_data or {})
            _extra["root_password"] = real_pass
            server.extra_data = _extra
            await session.flush()
            await cb.message.answer(
                f"✅ <b>ریبیلد شروع شد.</b>\n\n"
                f"🔑 رمز root جدید: <code>{real_pass}</code>\n\n"
                f"{WARN} این رمز را در جای امنی ذخیره کنید.\n"
                "🔔 چند دقیقه منتظر نصب OS بمانید.",
                parse_mode="HTML",
                reply_markup=back_kb(f"server:{server_id}"),
            )
            await LogService(cb.bot, session).log_server_action(user, server, "rebuild")
        else:
            await cb.message.answer(
                f"{ERR} ریبیلد ناموفق بود.",
                parse_mode="HTML",
                reply_markup=back_kb(f"server:{server_id}"),
            )
    except NotImplementedError as e:
        await cb.message.answer(f"{WARN} {_esc(e)}", parse_mode="HTML")
    except Exception as e:
        await cb.message.answer(f"{ERR} خطا: {_esc(e)}", parse_mode="HTML")


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


# از صفحه‌های بعدی (انتخاب محصول/بیلینگ) هم دکمه «بازگشت» به همین‌جا برمی‌گردد
_BUY_NAV_STATES = StateFilter(
    BuyServerStates.selecting_category,
    BuyServerStates.selecting_plan,
    BuyServerStates.selecting_billing,
)


@router.callback_query(_BUY_NAV_STATES, F.data.startswith("buygrp:"))
async def cb_select_group(cb: CallbackQuery, user: User, state: FSMContext, session: AsyncSession):
    """ID-based group selection — resolves the group name then shows its plans."""
    group = await session.get(ProductGroup, int(cb.data.split(":")[1]))
    if not group:
        await cb.answer("گروه یافت نشد.", show_alert=True)
        return
    await _select_category(cb, user, state, session, group.name)


@router.callback_query(_BUY_NAV_STATES, F.data.startswith("buycat:"))
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
        )
    )
    # ترتیب دستی ادمین (بخش «ترتیب محصولات»)، بدون آن: به‌ترتیب نام
    plans = sorted(result.scalars().all(), key=plan_sort_key)

    if not plans:
        await cb.answer("در این دسته‌بندی محصولی موجود نیست.", show_alert=True)
        return

    # گروه‌های چند-لوکیشنه (هتزنر/جیکور): اول لوکیشن انتخاب می‌شود، بعد محصولات همان لوکیشن
    _MULTI_LOC = (ProviderType.HETZNER, ProviderType.GCORE)
    if any(p.provider_type in _MULTI_LOC for p in plans):
        gid = _grp.id if _grp else 0
        locs = sorted({p.location for p in plans if p.location})
        # لیبل لوکیشن: هتزنر از نگاشت ثابت؛ جیکور نام region را در extra_data پلن دارد
        _dyn_labels = {}
        for p in plans:
            if p.location and (p.extra_data or {}).get("region_name"):
                _dyn_labels[p.location] = p.extra_data["region_name"]
        rows = []
        pair = []
        for loc in locs:
            emoji_id, label = _HZ_LOC_META.get(loc, (None, _dyn_labels.get(loc, loc)))
            kw = {"icon_custom_emoji_id": emoji_id} if emoji_id else {}
            pair.append(InlineKeyboardButton(text=label, callback_data=f"buyloc:{gid}:{loc}", **kw))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        rows.append([InlineKeyboardButton(text="بازگشت", callback_data="buy_server",
                                          **{"icon_custom_emoji_id": "5258236805890710909"})])
        await cb.message.edit_text(
            '<tg-emoji emoji-id="5926980668624998964">🟡</tg-emoji> '
            "موقعیت جغرافیایی سرور خود را انتخاب کنید:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await cb.answer()
        return

    await _render_plan_list(cb, state, session, category, plans, back_cb="buy_server")


async def _render_plan_list(cb: CallbackQuery, state: FSMContext, session: AsyncSession,
                            category: str, plans: list, back_cb: str):
    builder = InlineKeyboardBuilder()
    for plan in plans:
        ram_gb = plan.ram // 1024 if plan.ram >= 1024 else plan.ram
        if not plan.bandwidth:
            traffic = "نامحدود"   # bandwidth=0 یعنی ترافیک نامحدود (جیکور)
        elif plan.bandwidth >= 1000:
            # نمای کاربر: رند به نزدیک‌ترین ترابایت (20.48 → 20)؛ مقدار دقیق در پنل ادمین
            traffic = f"{round(plan.bandwidth / 1000)}ترابایت"
        else:
            traffic = f"{plan.bandwidth}گیگ"
        specs = f"{plan.cpu}هسته | {ram_gb}رم | {traffic}".translate(_FA_DIGITS)
        label = f"{specs} | {plan.display_name or plan.name}"
        # اموجی پریمیوم اختصاصی محصول؛ وگرنه پرچمِ لوکیشن (هتزنر)
        _pe = (plan.extra_data or {}).get("emoji_id") \
            or _HZ_LOC_META.get(plan.location or "", (None,))[0]
        _kw = {"icon_custom_emoji_id": _pe} if _pe else {}
        builder.button(text=label, callback_data=f"buyplan:{plan.id}", **_kw)
    builder.button(text="بازگشت", callback_data=back_cb, **{"icon_custom_emoji_id": "5258236805890710909"})
    builder.adjust(1)

    await state.update_data(category=category)
    await state.set_state(BuyServerStates.selecting_plan)
    await cb.message.edit_text(
        '<tg-emoji emoji-id="5926980668624998964">🟡</tg-emoji> یک محصول انتخاب کنید:',
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await cb.answer()


@router.callback_query(_BUY_NAV_STATES, F.data.startswith("buyloc:"))
async def cb_buy_location(cb: CallbackQuery, user: User, state: FSMContext, session: AsyncSession):
    _, gid, loc = cb.data.split(":")
    group = await session.get(ProductGroup, int(gid))
    if not group or group.is_hidden:
        await cb.answer("این گروه در دسترس نیست.", show_alert=True)
        return
    result = await session.execute(
        select(ServerPlan).where(
            ServerPlan.category == group.name,
            ServerPlan.is_active == True,
            ServerPlan.location == loc,
        )
    )
    plans = sorted(result.scalars().all(), key=plan_sort_key)
    if not plans:
        await cb.answer("در این لوکیشن محصولی موجود نیست.", show_alert=True)
        return
    # جیکور: اگر بیش از یک «نوع» flavor (Standard/CPU Optimized/…) در این
    # لوکیشن ایمپورت شده باشد، اول نوع سرور انتخاب می‌شود
    if all(p.provider_type == ProviderType.GCORE for p in plans):
        types = {_gc_flavor_type(p.provider_plan_id) for p in plans}
        if len(types) > 1:
            ordered = [t for t in _GC_TYPE_ORDER if t in types] + \
                      sorted(t for t in types if t not in _GC_TYPE_ORDER)
            rows = [[InlineKeyboardButton(
                text=_GC_TYPE_LABELS.get(t, t.title()),
                callback_data=f"buyloctype:{gid}:{loc}:{t}")] for t in ordered]
            rows.append([InlineKeyboardButton(
                text="بازگشت", callback_data=f"buygrp:{gid}",
                **{"icon_custom_emoji_id": "5258236805890710909"})])
            await state.set_state(BuyServerStates.selecting_plan)
            await cb.message.edit_text(
                '<tg-emoji emoji-id="5926980668624998964">🟡</tg-emoji> '
                "نوع سرور را انتخاب کنید:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
            await cb.answer()
            return
    await _render_plan_list(cb, state, session, group.name, plans,
                            back_cb=f"buygrp:{group.id}")


@router.callback_query(_BUY_NAV_STATES, F.data.startswith("buyloctype:"))
async def cb_buy_location_type(cb: CallbackQuery, user: User, state: FSMContext, session: AsyncSession):
    _, gid, loc, ftype = cb.data.split(":")
    group = await session.get(ProductGroup, int(gid))
    if not group or group.is_hidden:
        await cb.answer("این گروه در دسترس نیست.", show_alert=True)
        return
    result = await session.execute(
        select(ServerPlan).where(
            ServerPlan.category == group.name,
            ServerPlan.is_active == True,
            ServerPlan.location == loc,
        )
    )
    plans = sorted(
        [p for p in result.scalars().all()
         if _gc_flavor_type(p.provider_plan_id) == ftype],
        key=plan_sort_key)
    if not plans:
        await cb.answer("در این دسته محصولی موجود نیست.", show_alert=True)
        return
    # بازگشت از لیست پلن‌ها → همین صفحه‌ی نوع سرور (buyloc دوباره نوع‌ها را می‌سازد)
    await _render_plan_list(cb, state, session, group.name, plans,
                            back_cb=f"buyloc:{gid}:{loc}")


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

    # کاربر همیشه قیمت «ریالی» می‌بیند — پلن ارزی با نرخ روز تبدیل می‌شود
    _cur = obj_currency(plan)
    hourly_t = plan.price_hourly or 0
    monthly_t = plan.price_monthly or 0
    if _cur != "irt":
        hourly_t = await to_toman(session, hourly_t, _cur) if hourly_t else 0
        monthly_t = await to_toman(session, monthly_t, _cur) if monthly_t else 0
        if (plan.price_hourly and hourly_t <= 0) or (plan.price_monthly and monthly_t <= 0):
            await cb.answer("نرخ ارز هنوز تنظیم نشده. کمی بعد دوباره تلاش کنید.", show_alert=True)
            return

    if has_hourly and has_monthly:
        # بازگشت واقعی به لیست محصولاتِ همین گروه (نه شروع از اول)
        _grp = None
        if plan.category:
            _grp = (await session.execute(
                select(ProductGroup).where(ProductGroup.name == plan.category)
            )).scalar_one_or_none()
        back_cb = f"buygrp:{_grp.id}" if _grp else "buy_server"

        await state.set_state(BuyServerStates.selecting_billing)
        await cb.message.edit_text(
            f"نوع بیلینگ را انتخاب کنید:\nپلن: {plan.display_name or plan.name}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"ساعتی — {hourly_t:,.0f} تومان", callback_data="buybilling:hourly", **{"icon_custom_emoji_id": "5798535677318533269"})],
                [InlineKeyboardButton(text=f"ماهانه — {monthly_t:,.0f} تومان", callback_data="buybilling:monthly", **{"icon_custom_emoji_id": "5778496382117613636"})],
                [InlineKeyboardButton(text="بازگشت", callback_data=back_cb, **{"icon_custom_emoji_id": "5258236805890710909", "style": "primary"})],
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
            f"{ERR} اسم سرور نامعتبر است.\n"
            "فقط حروف کوچک (a-z)، اعداد و خط تیره (-) مجاز است.\n"
            "باید با حرف یا عدد شروع و تموم بشه:",
            parse_mode="HTML",
        )
        return
    await state.update_data(hostname=raw)
    await _ask_os_message(message, state, session, user)


async def _fetch_os_list(session: AsyncSession, account: ProviderAccount, data: dict) -> list:
    """لیست OSهای provider — جیکور per-region است (region از extra_data پلن)."""
    import asyncio
    prov = get_provider(account)
    if account.provider_type == ProviderType.GCORE:
        _plan = await session.get(ServerPlan, data.get("plan_id"))
        _rid = (_plan.extra_data or {}).get("region_id") if _plan else None
        if not _rid:
            return []
        # ایمیج با min_disk بزرگ‌تر از دیسک پلن حذف نمی‌شود — موقع خرید دیسک
        # خودکار به min_disk همان ایمیج بامپ و قیمت به‌روز محاسبه می‌شود
        # (رفتار سایت جیکور: انتخاب ویندوز → دیسک ۴۰ گیگ + هزینه لایسنس)
        return await asyncio.wait_for(
            prov.list_os_templates(location=str(_rid)), timeout=20)
    os_list = await asyncio.wait_for(prov.list_os_templates(), timeout=15)
    # فیلتر معماری (هتزنر): پلن cax = ARM و بقیه x86 — ایمیج ناهم‌معماری خطای ساخت می‌دهد
    if os_list and account.provider_type == ProviderType.HETZNER:
        _plan_arch = await session.get(ServerPlan, data.get("plan_id"))
        _is_arm = bool(_plan_arch and (_plan_arch.provider_plan_id or "").lower().startswith("cax"))
        os_list = [o for o in os_list if (o.get("architecture") == "arm") == _is_arm]
    return os_list


async def _ask_os(cb: CallbackQuery, state: FSMContext, session: AsyncSession, user: User):
    data = await state.get_data()
    account = await session.get(ProviderAccount, data.get("provider_account_id")) if data.get("provider_account_id") else None

    os_list = []
    if account:
        try:
            os_list = await _fetch_os_list(session, account, data)
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
    # متادیتای هر OS برای قیمت‌گذاری per-OS جیکور: [min_disk, لایسنس ساعتی]
    os_meta = {str(o["id"]): [int(o.get("min_disk") or 0),
                              float(o.get("price_per_hour") or 0),
                              1 if ("windows" in str(o.get("_flavor") or "").lower()
                                    or "windows" in str(o.get("name") or "").lower()) else 0]
               for o in os_list[:20]}
    await state.update_data(os_options=os_name_map, os_meta=os_meta)
    await state.set_state(BuyServerStates.selecting_os)

    builder = InlineKeyboardBuilder()
    for os_item in os_list[:20]:
        builder.button(text=os_item["name"], callback_data=f"buyos:{os_item['id']}")
    builder.adjust(2)
    # بازگشت: دکمه‌ی مستقل تمام-عرض پایین لیست (نه داخل شبکه‌ی دوستونه)
    builder.row(InlineKeyboardButton(
        text="بازگشت به منو", callback_data="cancel",
        **{"icon_custom_emoji_id": "5258236805890710909"}))

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
            os_list = await _fetch_os_list(session, account, data)
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
    os_meta = {str(o["id"]): [int(o.get("min_disk") or 0),
                              float(o.get("price_per_hour") or 0),
                              1 if ("windows" in str(o.get("_flavor") or "").lower()
                                    or "windows" in str(o.get("name") or "").lower()) else 0]
               for o in os_list[:20]}
    await state.update_data(os_options=os_name_map, os_meta=os_meta)
    await state.set_state(BuyServerStates.selecting_os)
    builder = InlineKeyboardBuilder()
    for os_item in os_list[:20]:
        builder.button(text=os_item["name"], callback_data=f"buyos:{os_item['id']}")
    builder.adjust(2)
    # بازگشت: دکمه‌ی مستقل تمام-عرض پایین لیست (نه داخل شبکه‌ی دوستونه)
    builder.row(InlineKeyboardButton(
        text="بازگشت به منو", callback_data="cancel",
        **{"icon_custom_emoji_id": "5258236805890710909"}))

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
    _meta = (data.get("os_meta") or {}).get(str(os_id)) or [0, 0, 0]
    await state.update_data(os_id=os_id, os_name=os_name,
                            os_min_disk=int(_meta[0] or 0),
                            os_lic_h=float(_meta[1] or 0),
                            os_is_win=bool(_meta[2] if len(_meta) > 2 else 0))
    if data.get("billing") == "hourly":
        await state.update_data(discount_id=None, discount_percent=0)
        await _ask_email_or_confirm(cb.message, state, session, user)
    else:
        await _ask_discount(cb, state)
    await cb.answer()


async def _ask_discount(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BuyServerStates.entering_discount)
    await cb.message.edit_text(
        '‏<tg-emoji emoji-id="5229064374403998351">🏷</tg-emoji> <b>کد تخفیف</b>\n\n'
        "اگر کد تخفیف دارید وارد کنید.\n"
        "در غیر این صورت از دکمه زیر استفاده کنید:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="بدون کد تخفیف", callback_data="buydisc:skip", **{"icon_custom_emoji_id": "5346172687863529237"})],
            [InlineKeyboardButton(text="انصراف", callback_data="cancel", **{"style": "danger", "icon_custom_emoji_id": "5240241223632954241"})],
        ]),
    )


async def _ask_email_or_confirm(msg, state: FSMContext, session, user: User, from_message=False):
    """If user has no email, collect it; otherwise go straight to confirmation.

    ایمیل فقط برای ویرچولایزور لازم است (ساخت کاربر پنل). سرویس‌دهنده‌های دیگر
    (هتزنر) به ایمیل نیازی ندارند → مستقیم تأیید."""
    data = await state.get_data()
    _plan = await session.get(ServerPlan, data["plan_id"]) if data.get("plan_id") else None
    needs_email = (_plan is None) or (_plan.provider_type == ProviderType.VIRTUALIZOR)

    if user.email or not needs_email:
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
        await message.answer(f"{ERR} ایمیل نامعتبر است. لطفاً یک ایمیل معتبر وارد کنید:", parse_mode="HTML")
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
        await message.answer(f"{ERR} کد تخفیف نامعتبر است.", parse_mode="HTML")
        return
    if code.expires_at and code.expires_at < now:
        await message.answer(f"{ERR} کد تخفیف منقضی شده.", parse_mode="HTML")
        return
    if code.max_uses and code.use_count >= code.max_uses:
        await message.answer(f"{ERR} ظرفیت این کد تخفیف پر شده.", parse_mode="HTML")
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
    show_disk = plan.disk
    # جیکور: قیمت مؤثر بر اساس OS انتخابی (بامپ دیسک + لایسنس ویندوز — زنده از API)
    if plan.provider_type == ProviderType.GCORE and billing == "hourly":
        try:
            base_price, _gc_addon, show_disk, _ = await _gcore_os_pricing(session, plan, data)
        except Exception as e:
            await msg.answer(f"{ERR} {_esc(e)}", parse_mode="HTML")
            return
    # تبدیل قیمت ارزی به ریال با نرخ روز (کاربر فقط قیمت ریالی می‌بیند)
    _cur = obj_currency(plan)
    if _cur != "irt" and base_price:
        base_price = await to_toman(session, base_price, _cur)
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
        f"• رم: {plan.ram} MB | پردازنده: {plan.cpu} | دیسک: {show_disk} GB\n"
        f"• ترافیک: {f'{plan.bandwidth} GB' if plan.bandwidth else 'نامحدود'}\n"
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


# کش کوتاه‌مدت قیمت زنده volume برای قیمت‌گذاری per-OS جیکور (صفحه تأیید + خرید)
_gc_volprice_cache: dict = {}


async def _gcore_os_pricing(session: AsyncSession, plan: ServerPlan,
                            data: dict) -> tuple[float, float, int, str | None]:
    """قیمت ساعتیِ مؤثر یک خرید جیکور بر اساس OS انتخابی (به ارز پلن).

    - دیسک مؤثر = max(دیسک پلن، min_disk ایمیج) — انتخاب ویندوز دیسک را خودکار
      بامپ می‌کند (رفتار سایت جیکور).
    - ویندوز flavor دوقلوی ویندوزی می‌خواهد (g2- → g2w-؛ خطای E2E جیکور) و
      قیمت flavor از همان نسخه‌ی wدار زنده خوانده می‌شود (لایسنس داخل آن).
    - قیمت = (flavor + دیسک مؤثر + قیمت image) × (۱ + سود ساعتی) — همه زنده.
    خروجی: (قیمت فروش ساعتی، سورشارژ نسبت به پلن، دیسک مؤثر GB، flavor_override)"""
    plan_disk = int(plan.disk or 0)
    md = int(data.get("os_min_disk") or 0)
    lic_h = float(data.get("os_lic_h") or 0)
    is_win = bool(data.get("os_is_win"))
    disk_used = max(plan_disk, md)
    base = float(plan.price_hourly or 0)
    if disk_used == plan_disk and lic_h <= 0 and not is_win:
        return base, 0.0, disk_used, None   # حالت عادی — همان قیمت پلن

    from bot.services.gcore_settings import get_margins, get_volume_rate
    import asyncio as _aio
    import time as _time
    extra = plan.extra_data or {}
    rid = int(extra.get("region_id") or 0)
    account = await session.get(ProviderAccount, plan.provider_account_id) \
        if plan.provider_account_id else None
    if not account or not rid:
        raise RuntimeError("اطلاعات قیمت‌گذاری این پلن ناقص است")
    prov = get_provider(account)
    mh, _ = await get_margins(session)
    now = _time.monotonic()

    flavor_override = None
    if is_win:
        parts = (plan.provider_plan_id or "").split("-")
        if parts and parts[0]:
            parts[0] += "w"
        win_flavor = "-".join(parts)
        wkey = ("wf", account.id, rid, win_flavor)
        cached = _gc_volprice_cache.get(wkey)
        if cached and now - cached[0] < 300:
            fp = cached[1]
        else:
            fp = await _aio.wait_for(prov.get_flavor_price(rid, win_flavor), timeout=20)
            _gc_volprice_cache[wkey] = (now, fp)
        if not fp or fp.get("disabled") or float(fp.get("price_per_hour") or 0) <= 0:
            raise RuntimeError("نسخه ویندوزی این پلن در این لوکیشن موجود نیست")
        flavor_h = float(fp["price_per_hour"])
        flavor_override = win_flavor
    else:
        flavor_h = float(extra.get("flavor_cost_hourly") or 0)

    rate = await get_volume_rate(session)
    if rate > 0:
        vol_h = disk_used * rate / 720.0
    else:
        vkey = (account.id, rid, disk_used)
        cached = _gc_volprice_cache.get(vkey)
        if cached and now - cached[0] < 300:
            vol_h = cached[1]
        else:
            p = await _aio.wait_for(
                prov.preview_volume_price(rid, disk_used), timeout=15)
            vol_h = float(p.get("price_per_hour") or 0) or \
                float(p.get("price_per_month") or 0) / 720.0
            if vol_h <= 0:
                raise RuntimeError("قیمت دیسک از سرویس‌دهنده خوانده نشد — کمی بعد تلاش کنید")
            _gc_volprice_cache[vkey] = (now, vol_h)
    sale_h = round((flavor_h + vol_h + lic_h) * (1 + float(mh or 0) / 100), 4)
    addon = max(0.0, round(sale_h - base, 4))
    return sale_h, addon, disk_used, flavor_override


def _delivery_text(server: Server, plan_name: str, password: str) -> str:
    """پیام تحویل سرویس — یوزرنیم واقعی از provider (root/Admin/...)؛ پیش‌فرض root."""
    username = (server.extra_data or {}).get("username") or "root"
    return (
        f'<tg-emoji emoji-id="5397916757333654639">➕</tg-emoji> <b>سرور {server.name} آماده است!</b>\n\n'
        f"• پلن: {plan_name}\n"
        f"• آیپی: <code>{server.ip_address or 'در حال تخصیص...'}</code>\n"
        f"• یوزرنیم: <code>{username}</code>\n"
        f"• پسورد: <code>{password}</code>\n"
        f"\n• این اطلاعات را در جای امنی ذخیره کنید.\n"
        "• سیستم‌عامل در حال نصب است — چند دقیقه منتظر بمانید."
    )


async def _bg_build_and_deliver(bot, chat_id: int, user_db_id: int, plan_db_id: int,
                                billing_str: str, hostname: str, os_id: str,
                                root_password: str, final_price: float,
                                disk_gb: int = 0,
                                price_addon_hourly: float = 0.0,
                                flavor_override: str | None = None) -> None:
    """ساخت و تحویل در پس‌زمینه (جیکور — ساخت طولانی است و نباید هندلر را بلاک کند).

    سشن مستقل باز می‌شود چون سشن هندلر با پایان هندلر بسته می‌شود.
    ترتیب: اول کسر (رزرو وجه — کاربر وسط ساخت نتواند موجودی را جای دیگر خرج کند)
    → ساخت → تحویل؛ شکست ساخت = برگشت کامل وجه + پیام شفاف."""
    import logging as _logging
    from bot.database.session import AsyncSessionFactory
    _log = _logging.getLogger(__name__)
    billing_type = BillingType.HOURLY if billing_str == "hourly" else BillingType.MONTHLY
    async with AsyncSessionFactory() as session:
        try:
            user = await session.get(User, user_db_id)
            plan = await session.get(ServerPlan, plan_db_id)
            if not user or not plan:
                return
            billing = BillingService(session)
            ok = await billing.debit(user.id, final_price,
                                     description=f"خرید سرور {hostname}")
            if not ok:
                await session.commit()
                await bot.send_message(
                    chat_id, f"{ERR} موجودی کافی نیست — خرید انجام نشد.",
                    parse_mode="HTML")
                return
            await session.commit()   # کسر قطعی شود قبل از عملیات طولانی
            try:
                svc = ServerService(session)
                _create_extra: dict = {"root_password": root_password}
                if disk_gb and disk_gb != int(plan.disk or 0):
                    # بامپ دیسک per-OS (ویندوز): caller extra آخرین merge است و
                    # مقدار disk پلن را override می‌کند
                    _create_extra["disk"] = disk_gb
                if flavor_override:
                    # ویندوز: flavor دوقلوی ویندوزی (g2w-…) — الزام API جیکور
                    _create_extra["flavor_override"] = flavor_override
                server = await svc.create_server(
                    user=user, plan=plan, os_id=os_id, billing_type=billing_type,
                    hostname=hostname, extra=_create_extra,
                )
                real_password = (server.extra_data or {}).get("root_password") or root_password
                _extra = dict(server.extra_data or {})
                _extra["root_password"] = real_password
                if price_addon_hourly > 0:
                    # سورشارژ per-server (دیسک بزرگ‌تر/لایسنس) — بیلینگ ساعتی از
                    # server_live_price همین را روی قیمت پلن اضافه می‌کند
                    _extra["price_addon_hourly"] = price_addon_hourly
                    if server.price_hourly:
                        server.price_hourly = float(server.price_hourly) + price_addon_hourly
                if disk_gb and disk_gb != int(server.disk or 0):
                    server.disk = disk_gb
                server.extra_data = _extra
                await session.flush()
                plan_name = plan.display_name or plan.name
                await session.commit()
                await bot.send_message(
                    chat_id, _delivery_text(server, plan_name, real_password),
                    parse_mode="HTML")
                try:
                    await LogService(bot, session).log_purchase(
                        user, server, plan_name, billing_str, final_price)
                    await session.commit()
                except Exception:
                    pass
            except Exception as e:
                _log.exception("background build failed for %s", hostname)
                await billing.credit(user.id, final_price,
                                     description=f"برگشت وجه — شکست ساخت {hostname}")
                await session.commit()
                await bot.send_message(
                    chat_id,
                    f"{ERR} خطا در ساخت سرور: {_esc(e)}\n"
                    "مبلغ به‌طور کامل به کیف پول برگشت داده شد.",
                    parse_mode="HTML")
        except Exception:
            _log.exception("background build/deliver fatal error")


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
    # جیکور: قیمت مؤثر per-OS (بامپ دیسک برای ویندوز + لایسنس — زنده از API)
    gc_disk_used, gc_addon_h, gc_flavor_override = int(plan.disk or 0), 0.0, None
    if plan.provider_type == ProviderType.GCORE and billing_type == BillingType.HOURLY:
        try:
            base_price, gc_addon_h, gc_disk_used, gc_flavor_override = \
                await _gcore_os_pricing(session, plan, data)
        except Exception as e:
            await cb.answer(f"خطا در قیمت‌گذاری: {str(e)[:150]}", show_alert=True)
            return
    # کسر همیشه ریالی است — پلن ارزی با نرخ روز تبدیل می‌شود
    _cur = obj_currency(plan)
    if _cur != "irt" and base_price:
        base_price = await to_toman(session, base_price, _cur)
        if base_price <= 0:
            await cb.answer("نرخ ارز هنوز تنظیم نشده. کمی بعد دوباره تلاش کنید.", show_alert=True)
            return
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

    await edit_loading(cb.message)
    await cb.answer()
    await state.clear()

    # Mark discount as used
    if data.get("discount_id"):
        code = await session.get(DiscountCode, data["discount_id"])
        if code:
            code.use_count += 1
            await session.flush()

    import secrets as _secrets
    import string as _string
    # اسم خودکار: پسوند رندوم تا سرورهای متعددِ یک کاربر اسم یکسان نگیرند
    _suffix = "".join(_secrets.choice(_string.ascii_lowercase + _string.digits) for _ in range(6))
    hostname = data.get("hostname") or f"srv-{_suffix}"
    os_id = data.get("os_id") or ""
    _alpha = _string.ascii_letters + _string.digits + "!@#$%^&*"
    root_password = "".join(_secrets.choice(_alpha) for _ in range(16))

    # سرویس‌دهنده‌های با ساخت طولانی (جیکور): پیام «در حال ساخت» فوری، و ادامه‌ی
    # ساخت/کسر/تحویل در پس‌زمینه با سشن مستقل (سشن هندلر با پایان هندلر بسته می‌شود)
    if plan.provider_type == ProviderType.GCORE:
        await cb.message.edit_text(
            '‏<tg-emoji emoji-id="5258503720928288433">🔔</tg-emoji> '
            "سرویس شما در حال ساخت است و بین ۱۰ تا ۱۵ دقیقه دیگر برای شما ارسال میشود.\n"
            "مشخصات و رمز عبور بعد از اتمام پروسه ساخت ارسال میشود.",
            parse_mode="HTML",
        )
        import asyncio as _aio
        _aio.create_task(_bg_build_and_deliver(
            bot=cb.bot, chat_id=cb.message.chat.id, user_db_id=user.id,
            plan_db_id=plan.id, billing_str=billing_str, hostname=hostname,
            os_id=os_id, root_password=root_password,
            final_price=float(final_price or 0),
            disk_gb=gc_disk_used, price_addon_hourly=gc_addon_h,
            flavor_override=gc_flavor_override,
        ))
        return

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
        # رمز واقعی: بعضی سرویس‌دهنده‌ها (هتزنر) رمز خودشان را تولید می‌کنند و
        # در extra_data برمی‌گردانند؛ وگرنه همان رمز تولیدیِ ربات (ویرچولایزور)
        real_password = (server.extra_data or {}).get("root_password") or root_password
        _extra = dict(server.extra_data or {})
        _extra["root_password"] = real_password
        server.extra_data = _extra
        await session.flush()

        plan_name = plan.display_name or plan.name
        await cb.message.edit_text(
            _delivery_text(server, plan_name, real_password), parse_mode="HTML")
        await LogService(cb.bot, session).log_purchase(
            user, server, plan_name, billing_str, final_price or 0
        )
    except Exception as e:
        await cb.message.edit_text(f"{ERR} خطا در ساخت سرور: {_esc(e)}\nبا پشتیبانی تماس بگیرید.", parse_mode="HTML")


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
    if (server.billing_type != BillingType.MONTHLY
            and server.provider_type == ProviderType.VIRTUALIZOR):
        await cb.answer("تغییر IP فقط برای سرورهای ماهانه فعال است.", show_alert=True)
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
    wait = await cb.message.answer('\u200F<tg-emoji emoji-id="5386367538735104399">⌛️</tg-emoji> در حال تغییر IP...', parse_mode="HTML")
    try:
        old_ip = server.ip_address
        svc = ServerService(session)
        ok = await svc.perform_action(server, "change_ip")
        if not ok:
            raise RuntimeError("سرویس‌دهنده تغییر IP را انجام نداد")
        new_ip = server.ip_address

        # ویرچولایزور: ریبوت تا IP جدید داخل OS اعمال شود
        # (هتزنر خودش در فرایند تعویض خاموش/روشن می‌کند)
        if server.provider_type == ProviderType.VIRTUALIZOR:
            try:
                await get_provider(account).restart_server(server.provider_server_id)
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
        await LogService(cb.bot, session).log_ip_change(user, server, old_ip, new_ip, fee)
    except Exception as e:
        if fee > 0:
            await billing.credit(user.id, fee, description=f"برگشت وجه تغییر IP — {server.name}")
        await wait.edit_text(
            f"{ERR} <b>تغییر IP ناموفق بود:</b> {_esc(e)}\n\nوجه برگشت داده شد.",
            parse_mode="HTML",
            reply_markup=back_kb(f"server:{server_id}"),
        )


# ── Extra IP (monthly servers only) ──────────────────────────────────────────

@router.callback_query(F.data.startswith("srv_addip:"))
async def cb_add_ip_confirm(cb: CallbackQuery, user: User, session: AsyncSession):
    server_id = int(cb.data.split(":")[1])
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    if server.status != ServerStatus.ACTIVE:
        await cb.answer("سرور باید فعال باشد.", show_alert=True)
        return
    if server.billing_type != BillingType.MONTHLY:
        await cb.answer("IP اضافه فقط برای سرورهای ماهانه فعال است.", show_alert=True)
        return
    if server.provider_type != ProviderType.VIRTUALIZOR:
        await cb.answer("IP اضافه برای این سرویس‌دهنده در دسترس نیست.", show_alert=True)
        return

    account = await session.get(ProviderAccount, server.provider_account_id) if server.provider_account_id else None
    fee = float((account.extra_config or {}).get("extra_ip_fee", 0) or 0) if account else 0

    if fee > 0 and user.balance < fee:
        await cb.answer(f"موجودی کافی نیست. هزینه IP اضافه: {fee:,.0f} تومان — موجودی: {user.balance:,.0f} تومان", show_alert=True)
        return

    fee_text = f"{fee:,.0f} تومان" if fee > 0 else "رایگان"
    await cb.message.edit_text(
        f'‏<tg-emoji emoji-id="5346024644635804737">🌐</tg-emoji> <b>تأیید IP اضافه</b>\n\n'
        f"یک آیپی جدید علاوه بر آیپی فعلی به سرور اختصاص می‌یابد.\n"
        f"آیپی فعلی = <code>{server.ip_address or 'نامشخص'}</code>\n"
        f"هزینه: <b>{fee_text}</b>\n\n"
        "سرور پس از افزودن IP ریبوت خواهد شد.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="تأیید IP اضافه", callback_data=f"srv_addip_do:{server_id}", **{"style": "success", "icon_custom_emoji_id": "5206607081334906820"}),
            InlineKeyboardButton(text="انصراف", callback_data=f"server:{server_id}", **{"style": "danger", "icon_custom_emoji_id": "5240241223632954241"}),
        ]]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("srv_addip_do:"))
async def cb_add_ip_do(cb: CallbackQuery, user: User, session: AsyncSession):
    server_id = int(cb.data.split(":")[1])
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return
    if server.status != ServerStatus.ACTIVE or server.billing_type != BillingType.MONTHLY:
        await cb.answer("این عملیات مجاز نیست.", show_alert=True)
        return

    account = await session.get(ProviderAccount, server.provider_account_id) if server.provider_account_id else None
    if not account:
        await cb.answer("اطلاعات پروایدر یافت نشد.", show_alert=True)
        return

    fee = float((account.extra_config or {}).get("extra_ip_fee", 0) or 0)

    billing = BillingService(session)
    if fee > 0:
        ok = await billing.debit(user.id, fee, server_id=server_id, description=f"IP اضافه — {server.name}")
        if not ok:
            await cb.answer("موجودی کافی نیست.", show_alert=True)
            return

    await cb.answer("⏳ در حال افزودن IP...")
    try:
        await cb.message.delete()
    except Exception:
        pass
    wait = await cb.message.answer('‏<tg-emoji emoji-id="5386367538735104399">⌛️</tg-emoji> در حال افزودن IP...', parse_mode="HTML")
    try:
        prov = VirtualizorProvider(account.api_endpoint, account.api_key, account.api_secret)
        new_ip = await prov.add_extra_ip(server.provider_server_id)

        # آیپی اضافه در extra_data ذخیره می‌شود (آیپی اصلی دست‌نخورده می‌ماند)
        extra = dict(server.extra_data or {})
        extra_ips = list(extra.get("extra_ips") or [])
        extra_ips.append(new_ip)
        extra["extra_ips"] = extra_ips
        server.extra_data = extra
        await session.flush()

        try:
            await prov.restart_server(server.provider_server_id)
        except Exception:
            pass

        fee_text = f"\nهزینه کسر شد: {fee:,.0f} تومان" if fee > 0 else ""
        await wait.edit_text(
            f'<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> <b>آیپی اضافه اختصاص یافت</b>\n\n'
            f"• آیپی اصلی: <code>{server.ip_address or 'نامشخص'}</code>\n"
            f"• آیپی جدید: <code>{new_ip}</code>{fee_text}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="بازگشت", callback_data=f"server:{server_id}", **{"icon_custom_emoji_id": "5258236805890710909"}),
            ]]),
        )
        await LogService(cb.bot, session).log_extra_ip(user, server, new_ip, fee)
    except Exception as e:
        if fee > 0:
            await billing.credit(user.id, fee, description=f"برگشت وجه IP اضافه — {server.name}")
        await wait.edit_text(
            f'‏<tg-emoji emoji-id="4956612582816351459">❌</tg-emoji> <b>افزودن IP ناموفق بود:</b> {e}\n\n'
            "وجه برگشت داده شد.",
            parse_mode="HTML",
            reply_markup=back_kb(f"server:{server_id}"),
        )


# ── Usage stats (traffic bar) ────────────────────────────────────────────────

@router.callback_query(F.data.startswith("srv_usage:"))
async def cb_server_usage(cb: CallbackQuery, user: User, session: AsyncSession):
    server_id = int(cb.data.split(":")[1])
    server = await session.get(Server, server_id)
    if not server or server.user_id != user.id:
        await cb.answer("سرور یافت نشد.", show_alert=True)
        return

    await cb.answer("⏳ در حال خواندن مصرف...")

    # مصرف زنده از ویرچولایزور (fallback: مقدار ذخیره‌شده در DB)
    used = float(server.traffic_used_gb or 0)
    account = await session.get(ProviderAccount, server.provider_account_id) if server.provider_account_id else None
    if account and server.provider_server_id:
        try:
            prov = get_provider(account)
            used = await prov.get_traffic(server.provider_server_id)
            server.traffic_used_gb = used
            await session.flush()
        except Exception:
            pass

    limit = float(server.traffic_limit_gb or 0)
    pct = (used / limit * 100) if limit > 0 else 0
    pct = max(0.0, min(pct, 100.0))
    filled = int(round(pct / 10))
    bar = "█" * filled + "░" * (10 - filled)

    def _g(v: float) -> str:
        return f"{v:g}" if v < 1000 else f"{v:,.0f}"

    if limit > 0:
        traffic_line = f"‏ترافیک {bar} {_g(used)} / {_g(limit)} GB ({pct:.0f}%)"
    else:
        # لیمیت صفر/خالی = ترافیک نامحدود (جیکور) — مصرف تجمعی هم گزارش نمی‌شود
        traffic_line = "‏ترافیک: نامحدود ♾"
    text = (
        f'‏<tg-emoji emoji-id="5936143551854285132">📊</tg-emoji> <b>آمار مصرف — {server.name}</b>\n\n'
        f"{traffic_line}"
    )
    try:
        await cb.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="بروزرسانی", callback_data=f"srv_usage:{server_id}")],
                [InlineKeyboardButton(text="بازگشت", callback_data=f"server:{server_id}", **{"icon_custom_emoji_id": "5258236805890710909"})],
            ]),
        )
    except Exception:
        pass  # message not modified (مقدار تغییری نکرده)


# Snapshot handlers moved to bot/handlers/snapshots.py


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
            new_password = svc.last_root_password or new_password
            account = await session.get(ProviderAccount, server.provider_account_id) if server.provider_account_id else None
            if account:
                try:
                    prov = get_provider(account)
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
                f"{ERR} تغییر رمز ناموفق بود.",
                parse_mode="HTML",
                reply_markup=back_kb(f"server:{server_id}"),
            )
    except Exception as e:
        await wait.edit_text(
            f"{ERR} <b>خطا:</b> {_esc(e)}",
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
        await message.answer("✅ سخت‌افزار ویرایش شد." if ok else f"{ERR} ویرایش ناموفق بود.", parse_mode="HTML")
    except NotImplementedError:
        await message.answer(f"{WARN} این پروایدر ویرایش آنلاین را پشتیبانی نمی‌کند.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"{ERR} خطا: {_esc(e)}", parse_mode="HTML")
