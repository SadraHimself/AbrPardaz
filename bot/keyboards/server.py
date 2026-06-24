"""Server management keyboards."""
from __future__ import annotations

from bot.database.models import BillingType, Server, ServerStatus
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def status_dot(server: Server) -> str:
    """Keyboard dot kept in exact sync with the real machine state:
    online → 🟢 , offline / suspended → 🔴 , pending (locked/building) → ⚪."""
    if server.status == ServerStatus.SUSPENDED:
        return "🔴"
    if server.status in (
        ServerStatus.PENDING, ServerStatus.BUILDING,
        ServerStatus.REBUILDING, ServerStatus.REBOOTING,
    ):
        return "⚪"
    if server.status == ServerStatus.ACTIVE:
        # ACTIVE covers both running and powered-off; machine_status decides.
        return "🟢" if str((server.extra_data or {}).get("machine_status", "1")) == "1" else "🔴"
    return "🔴"  # deleted / unknown


def server_list_kb(servers: list[Server]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for s in servers:
        icon = status_dot(s)
        builder.button(text=f"{icon} {s.name} ({s.ip_address or 'بدون IP'})",
                       callback_data=f"server:{s.id}")
    builder.button(text="🛒 خرید سرور جدید", callback_data="buy_server")
    builder.button(text="🔙 بازگشت", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()


def server_actions_kb(server: Server) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    sid = server.id

    is_hourly = server.billing_type == BillingType.HOURLY

    if server.status == ServerStatus.ACTIVE:
        extra = server.extra_data or {}
        is_running = str(extra.get("machine_status", "1")) == "1"
        if is_running:
            builder.button(text="🔄 ریبوت", callback_data=f"srv_action:{sid}:restart_confirm")
            builder.button(text="⏹ خاموش", callback_data=f"srv_action:{sid}:stop")
        else:
            builder.button(text="▶️ روشن کردن", callback_data=f"srv_action:{sid}:start")
        builder.button(text="🔁 ریبیلد", callback_data=f"srv_action:{sid}:rebuild_menu")
        builder.button(text="🌐 تغییر IP", callback_data=f"srv_changeip:{sid}")
        builder.button(text="🔑 تغییر رمز", callback_data=f"srv_chpass:{sid}")
        if is_hourly:
            builder.button(text="🗑 حذف سرور", callback_data=f"srv_action:{sid}:delete_confirm")

    elif server.status == ServerStatus.SUSPENDED:
        builder.button(text="▶️ فعال‌سازی", callback_data=f"srv_action:{sid}:unsuspend")
        if is_hourly:
            builder.button(text="🗑 حذف سرور", callback_data=f"srv_action:{sid}:delete_confirm")

    elif server.status == ServerStatus.DELETED:
        pass  # no actions

    else:
        builder.button(text="▶️ روشن کردن", callback_data=f"srv_action:{sid}:start")
        builder.button(text="🔁 ریبیلد", callback_data=f"srv_action:{sid}:rebuild_menu")
        builder.button(text="🔄 بررسی وضعیت", callback_data=f"srv_refresh:{sid}")
        if is_hourly:
            builder.button(text="🗑 حذف سرور", callback_data=f"srv_action:{sid}:delete_confirm")

    builder.button(text="🔙 بازگشت به لیست", callback_data="my_servers")
    builder.adjust(2)
    return builder.as_markup()


def subproducts_buy_kb(server_id: int, sub_products) -> InlineKeyboardMarkup:
    from aiogram.utils.keyboard import InlineKeyboardBuilder as _B
    builder = _B()
    for sp in sub_products:
        unit = "GB" if sp.type.value == "traffic" else "عدد"
        builder.button(
            text=f"📦 {sp.name} — {sp.value:.0f} {unit} — {sp.price:,.0f}T",
            callback_data=f"buy_subprod:{server_id}:{sp.id}",
        )
    builder.button(text="🔙 بازگشت", callback_data=f"server:{server_id}")
    builder.adjust(1)
    return builder.as_markup()


def server_delete_confirm_kb(server_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ بله، حذف شود", callback_data=f"srv_action:{server_id}:delete"),
            InlineKeyboardButton(text="❌ خیر", callback_data=f"server:{server_id}"),
        ]
    ])



def add_traffic_kb(server_id: int) -> InlineKeyboardMarkup:
    options = [50, 100, 200, 500, 1000]
    builder = InlineKeyboardBuilder()
    for gb in options:
        builder.button(text=f"{gb} GB", callback_data=f"add_traffic:{server_id}:{gb}")
    builder.button(text="🔙 بازگشت", callback_data=f"server:{server_id}")
    builder.adjust(3)
    return builder.as_markup()
