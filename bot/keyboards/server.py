"""Server management keyboards."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.database.models import BillingType, Server, ServerStatus


def _btn(text: str, cbd: str, style: str | None = None) -> InlineKeyboardButton:
    """Create an InlineKeyboardButton with optional Telegram style (success/danger/primary)."""
    if style:
        return InlineKeyboardButton(text=text, callback_data=cbd, **{"style": style})
    return InlineKeyboardButton(text=text, callback_data=cbd)


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
        return "🟢" if str((server.extra_data or {}).get("machine_status", "1")) == "1" else "🔴"
    return "🔴"


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
    sid = server.id
    is_hourly = server.billing_type == BillingType.HOURLY
    rows: list[list[InlineKeyboardButton]] = []

    if server.status == ServerStatus.ACTIVE:
        # Always show both power buttons — start (green) and stop (red)
        rows.append([
            _btn("▶️ روشن", f"srv_action:{sid}:start", "success"),
            _btn("⏹ خاموش", f"srv_action:{sid}:stop", "danger"),
        ])
        rows.append([
            _btn("🔄 ریبوت", f"srv_action:{sid}:restart_confirm", "primary"),
            _btn("🔁 ریبیلد", f"srv_action:{sid}:rebuild_menu", "primary"),
        ])
        rows.append([
            _btn("🌐 تغییر IP", f"srv_changeip:{sid}"),
            _btn("🔑 تغییر رمز", f"srv_chpass:{sid}"),
        ])
        if is_hourly:
            rows.append([_btn("🗑 حذف سرور", f"srv_action:{sid}:delete_confirm", "danger")])

    elif server.status == ServerStatus.SUSPENDED:
        rows.append([
            _btn("▶️ فعال‌سازی", f"srv_action:{sid}:unsuspend", "success"),
        ])
        if is_hourly:
            rows[-1].append(_btn("🗑 حذف سرور", f"srv_action:{sid}:delete_confirm", "danger"))

    elif server.status != ServerStatus.DELETED:
        rows.append([
            _btn("▶️ روشن کردن", f"srv_action:{sid}:start", "success"),
            _btn("🔁 ریبیلد", f"srv_action:{sid}:rebuild_menu", "primary"),
        ])
        rows.append([_btn("🔄 بررسی وضعیت", f"srv_refresh:{sid}")])
        if is_hourly:
            rows[-1].append(_btn("🗑 حذف سرور", f"srv_action:{sid}:delete_confirm", "danger"))

    rows.append([_btn("🔙 بازگشت به لیست", "my_servers")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def server_delete_confirm_kb(server_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            _btn("✅ بله، حذف شود", f"srv_action:{server_id}:delete", "danger"),
            _btn("❌ خیر", f"server:{server_id}", "success"),
        ]
    ])


def subproducts_buy_kb(server_id: int, sub_products) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for sp in sub_products:
        unit = "GB" if sp.type.value == "traffic" else "عدد"
        builder.button(
            text=f"📦 {sp.name} — {sp.value:.0f} {unit} — {sp.price:,.0f}T",
            callback_data=f"buy_subprod:{server_id}:{sp.id}",
        )
    builder.button(text="🔙 بازگشت", callback_data=f"server:{server_id}")
    builder.adjust(1)
    return builder.as_markup()


def add_traffic_kb(server_id: int) -> InlineKeyboardMarkup:
    options = [50, 100, 200, 500, 1000]
    builder = InlineKeyboardBuilder()
    for gb in options:
        builder.button(text=f"{gb} GB", callback_data=f"add_traffic:{server_id}:{gb}")
    builder.button(text="🔙 بازگشت", callback_data=f"server:{server_id}")
    builder.adjust(3)
    return builder.as_markup()
