"""Server management keyboards."""
from __future__ import annotations

from bot.database.models import Server, ServerStatus
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def server_list_kb(servers: list[Server]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for s in servers:
        icon = "🟢" if s.status == ServerStatus.ACTIVE else "🔴" if s.status == ServerStatus.SUSPENDED else "⚪"
        builder.button(text=f"{icon} {s.name} ({s.ip_address or 'بدون IP'})",
                       callback_data=f"server:{s.id}")
    builder.button(text="🛒 خرید سرور جدید", callback_data="buy_server")
    builder.button(text="🔙 بازگشت", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()


def server_actions_kb(server: Server) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    sid = server.id

    if server.status == ServerStatus.ACTIVE:
        builder.button(text="🔄 ریبوت", callback_data=f"srv_action:{sid}:restart")
        builder.button(text="⏹ خاموش", callback_data=f"srv_action:{sid}:stop")
        builder.button(text="🔁 ریبیلد", callback_data=f"srv_action:{sid}:rebuild_menu")
        builder.button(text="🌐 تغییر IP", callback_data=f"srv_action:{sid}:change_ip")
        builder.button(text="🖥 VNC", callback_data=f"srv_vnc:{sid}")
        builder.button(text="📊 ترافیک", callback_data=f"srv_traffic:{sid}")
        builder.button(text="➕ ترافیک اضافه", callback_data=f"srv_add_traffic:{sid}")
        builder.button(text="📦 خدمات اضافه", callback_data=f"srv_subproducts:{sid}")
        builder.button(text="⚙️ ویرایش سخت‌افزار", callback_data=f"srv_edit:{sid}")
        builder.button(text="🗑 حذف سرور", callback_data=f"srv_action:{sid}:delete_confirm")

    elif server.status == ServerStatus.SUSPENDED:
        builder.button(text="▶️ فعال‌سازی", callback_data=f"srv_action:{sid}:unsuspend")
        builder.button(text="🗑 حذف سرور", callback_data=f"srv_action:{sid}:delete_confirm")

    elif server.status == ServerStatus.DELETED:
        pass  # no actions

    else:
        builder.button(text="🔄 بررسی وضعیت", callback_data=f"srv_refresh:{sid}")

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
