"""Admin panel keyboards. (No emojis — kept clean/uncluttered by design.)"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def _st(is_active: bool) -> str:
    """Status indicator prefix for list rows (active / inactive)."""
    return "✅ " if is_active else "❌ "


def admin_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="سرورهای ویرچولایزور", callback_data="admin:providers")
    builder.button(text="محصولات", callback_data="admin:plans")
    builder.button(text="کدهای تخفیف", callback_data="admin:discounts")
    builder.button(text="کاربران", callback_data="admin:users")
    builder.button(text="آمار", callback_data="admin:stats")
    builder.button(text="پیام همگانی", callback_data="admin:broadcast")
    builder.button(text="تنظیمات ربات", callback_data="admin:settings")
    builder.button(text="مالی", callback_data="admin:finance")
    builder.button(text="تاپیک اطلاعات", callback_data="admin:log_group")
    builder.button(text="بازگشت", callback_data="main_menu")
    builder.adjust(2)
    return builder.as_markup()


# ── Provider keyboards ────────────────────────────────────────────────────────

def providers_list_kb(providers: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for p in providers:
        kyc = " (KYC)" if p.strict_kyc else ""
        builder.button(text=f"{_st(p.is_active)}{p.name}{kyc}", callback_data=f"admin:prov:{p.id}")
    builder.button(text="اضافه کردن سرور", callback_data="admin:prov_add")
    builder.button(text="بازگشت", callback_data="admin_panel")
    builder.adjust(1)
    return builder.as_markup()


def provider_detail_kb(provider_id: int, is_active: bool = True, strict_kyc: bool = False,
                       change_ip_fee: float = 0, extra_ip_fee: float = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="نام", callback_data=f"admin:prov_edit:{provider_id}:name")
    builder.button(text="URL پنل", callback_data=f"admin:prov_edit:{provider_id}:url")
    builder.button(text="API Key", callback_data=f"admin:prov_edit:{provider_id}:api_key")
    builder.button(text="API Pass", callback_data=f"admin:prov_edit:{provider_id}:api_pass")
    fee_label = f"هزینه تغییر IP: {change_ip_fee:,.0f}T" if change_ip_fee else "هزینه تغییر IP: رایگان"
    builder.button(text=fee_label, callback_data=f"admin:prov_edit:{provider_id}:change_ip_fee")
    extra_fee_label = f"هزینه IP اضافه: {extra_ip_fee:,.0f}T" if extra_ip_fee else "هزینه IP اضافه: رایگان"
    builder.button(text=extra_fee_label, callback_data=f"admin:prov_edit:{provider_id}:extra_ip_fee")
    builder.button(text="تست اتصال", callback_data=f"admin:prov_test:{provider_id}")
    builder.button(text="مانیتور سرور", callback_data=f"admin:prov_monitor:{provider_id}")
    kyc_text = "KYC: روشن" if strict_kyc else "KYC: خاموش"
    builder.button(text=kyc_text, callback_data=f"admin:prov_kyc:{provider_id}")
    toggle_text = "غیرفعال" if is_active else "فعال"
    builder.button(text=toggle_text, callback_data=f"admin:prov_toggle:{provider_id}")
    builder.button(text="حذف", callback_data=f"admin:prov_del:{provider_id}")
    builder.button(text="بازگشت", callback_data="admin:providers")
    builder.adjust(2)
    return builder.as_markup()


# ── Plan / group keyboards ────────────────────────────────────────────────────

def plans_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="گروه محصولات", callback_data="admin:groups")
    builder.button(text="محصولات", callback_data="admin:plans_list")
    builder.button(text="افزودن محصول", callback_data="admin:plan_add")
    builder.button(text="بازگشت", callback_data="admin_panel")
    builder.adjust(2, 1, 1)
    return builder.as_markup()


def groups_list_kb(groups: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for g in groups:
        tag = "❌ " if g.is_hidden else "✅ "
        builder.button(text=f"{tag}{g.name}", callback_data=f"admin:group:{g.id}")
    builder.button(text="گروه جدید", callback_data="admin:group_add")
    builder.button(text="بازگشت", callback_data="admin:plans")
    builder.adjust(1)
    return builder.as_markup()


def group_detail_kb(group_id: int, is_hidden: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="ویرایش نام", callback_data=f"admin:group_edit:{group_id}:name")
    builder.button(text="ویرایش اموجی", callback_data=f"admin:group_edit:{group_id}:emoji")
    builder.button(text=("نمایش گروه" if is_hidden else "مخفی کردن گروه"),
                   callback_data=f"admin:group_hide:{group_id}")
    builder.button(text="حذف گروه", callback_data=f"admin:group_del:{group_id}")
    builder.button(text="بازگشت", callback_data="admin:groups")
    builder.adjust(2, 1, 1, 1)
    return builder.as_markup()


def plans_groups_kb(entries: list) -> InlineKeyboardMarkup:
    """entries: (key, label) — key = group id or 'none' (بدون گروه)."""
    builder = InlineKeyboardBuilder()
    for key, label in entries:
        builder.button(text=label, callback_data=f"admin:plans_grp:{key}")
    builder.button(text="افزودن محصول", callback_data="admin:plan_add")
    builder.button(text="بازگشت", callback_data="admin:plans")
    builder.adjust(1)
    return builder.as_markup()


def plans_in_group_kb(plans: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for p in plans:
        builder.button(
            text=f"{_st(p.is_active)}{p.display_name or p.name}",
            callback_data=f"admin:plan:{p.id}",
        )
    builder.button(text="افزودن محصول", callback_data="admin:plan_add")
    builder.button(text="بازگشت", callback_data="admin:plans_list")
    builder.adjust(1)
    return builder.as_markup()


def group_pick_kb(groups: list, prefix: str, allow_new: bool = True,
                  cancel_cb: str = "admin_panel") -> InlineKeyboardMarkup:
    """Group selector — callback: {prefix}:{group_id} (+ {prefix}:new)."""
    builder = InlineKeyboardBuilder()
    for g in groups:
        builder.button(text=g.name, callback_data=f"{prefix}:{g.id}")
    if allow_new:
        builder.button(text="ساخت گروه جدید", callback_data=f"{prefix}:new")
    builder.button(text="انصراف", callback_data=cancel_cb)
    builder.adjust(1)
    return builder.as_markup()


def plan_detail_kb(plan_id: int, is_active: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="قیمت ساعتی", callback_data=f"admin:plan_edit:{plan_id}:price_hourly")
    builder.button(text="قیمت ماهانه", callback_data=f"admin:plan_edit:{plan_id}:price_monthly")
    builder.button(text="موقعیت", callback_data=f"admin:plan_edit:{plan_id}:location")
    builder.button(text="گروه محصول", callback_data=f"admin:plan_edit:{plan_id}:category")
    builder.button(text="نام نمایشی", callback_data=f"admin:plan_edit:{plan_id}:display_name")
    builder.button(text="اموجی محصول", callback_data=f"admin:plan_edit:{plan_id}:emoji")
    builder.button(text="Plan ID ویرچو", callback_data=f"admin:plan_edit:{plan_id}:provider_plan_id")
    toggle_text = "غیرفعال کردن" if is_active else "فعال کردن"
    builder.button(text=toggle_text, callback_data=f"admin:plan_toggle:{plan_id}")
    builder.button(text="حذف", callback_data=f"admin:plan_del:{plan_id}")
    builder.button(text="بازگشت", callback_data="admin:plans_list")
    builder.adjust(2)
    return builder.as_markup()


# ── Discount keyboards ────────────────────────────────────────────────────────

def discounts_list_kb(codes: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for c in codes:
        user_tag = "(اختصاصی) " if c.user_id else ""
        builder.button(
            text=f"{_st(c.is_active)}{user_tag}{c.code} — {c.discount_percent:.0f}%",
            callback_data=f"admin:disc:{c.id}",
        )
    builder.button(text="کد جدید", callback_data="admin:disc_add")
    builder.button(text="بازگشت", callback_data="admin_panel")
    builder.adjust(1)
    return builder.as_markup()


def discount_detail_kb(disc_id: int, is_active: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="درصد تخفیف", callback_data=f"admin:disc_edit:{disc_id}:percent")
    builder.button(text="تاریخ انقضا", callback_data=f"admin:disc_edit:{disc_id}:expires_at")
    builder.button(text="حداکثر استفاده", callback_data=f"admin:disc_edit:{disc_id}:max_uses")
    toggle_text = "غیرفعال" if is_active else "فعال"
    builder.button(text=toggle_text, callback_data=f"admin:disc_toggle:{disc_id}")
    builder.button(text="حذف", callback_data=f"admin:disc_del:{disc_id}")
    builder.button(text="بازگشت", callback_data="admin:discounts")
    builder.adjust(2)
    return builder.as_markup()


# ── User management keyboards ─────────────────────────────────────────────────

def user_detail_kb(user_id: int, is_banned: bool, is_kyc: bool, hourly_limit: int = 5) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    ban_text = "آنبن کاربر" if is_banned else "بن کاربر"
    builder.button(text=ban_text, callback_data=f"admin:user_ban:{user_id}")
    builder.button(text="افزایش موجودی", callback_data=f"admin:user_credit:{user_id}")
    builder.button(text="کاهش موجودی", callback_data=f"admin:user_debit:{user_id}")
    builder.button(text="کد تخفیف اختصاصی", callback_data=f"admin:user_disc:{user_id}")
    builder.button(text="تاریخچه پرداخت", callback_data=f"admin:user_payments:{user_id}")
    builder.button(text="سرویس‌های فعال", callback_data=f"admin:user_servers:{user_id}")
    builder.button(text=f"لیمیت ساعتی: {hourly_limit}", callback_data=f"admin:user_limit:{user_id}")
    if is_kyc:
        builder.button(text="حذف احراز هویت", callback_data=f"admin:user_unverify:{user_id}")
    else:
        builder.button(text="احراز هویت دستی", callback_data=f"admin:user_verify:{user_id}")
    builder.button(text="کد ملی", callback_data=f"admin:user_edit_nid:{user_id}")
    builder.button(text="شماره موبایل", callback_data=f"admin:user_edit_phone:{user_id}")
    builder.button(text="ارسال پیام", callback_data=f"admin:user_msg:{user_id}")
    builder.button(text="بازگشت", callback_data="admin:users")
    builder.adjust(2)
    return builder.as_markup()


def users_list_kb(users: list, page: int = 0, total: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for u in users:
        status = "🚫" if u.status.value == "banned" else ("✅" if u.is_phone_verified else "❌")
        builder.button(
            text=f"{status} {u.first_name or 'N/A'} | {u.balance:,.0f}T",
            callback_data=f"admin:user:{u.id}",
        )
    # Pagination
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="قبلی", callback_data=f"admin:users_page:{page-1}"))
    if (page + 1) * 20 < total:
        nav.append(InlineKeyboardButton(text="بعدی", callback_data=f"admin:users_page:{page+1}"))
    if nav:
        builder.row(*nav)
    builder.button(text="جستجوی کاربر", callback_data="admin:user_search")
    builder.button(text="بازگشت", callback_data="admin_panel")
    builder.adjust(1)
    return builder.as_markup()


# ── Stats keyboards ───────────────────────────────────────────────────────────

def stats_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="آمار امروز", callback_data="admin:stats_today")
    builder.button(text="آمار این ماه", callback_data="admin:stats_month")
    builder.button(text="بازه دلخواه", callback_data="admin:stats_range")
    builder.button(text="بازگشت", callback_data="admin_panel")
    builder.adjust(2)
    return builder.as_markup()


# ── Settings keyboards ────────────────────────────────────────────────────────

def settings_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="آیدی پشتیبان", callback_data="admin:setting:support_id")
    builder.button(text="شرایط پذیرش", callback_data="admin:setting:terms_text")
    builder.button(text="کانال‌های اجباری", callback_data="admin:channels")
    builder.button(text="بازگشت", callback_data="admin_panel")
    builder.adjust(2)
    return builder.as_markup()


def channels_kb(channels: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for ch in channels:
        builder.button(text=ch, callback_data=f"admin:ch_del:{ch.replace(':', '_')}")
    builder.button(text="افزودن کانال", callback_data="admin:ch_add")
    builder.button(text="بازگشت", callback_data="admin:settings")
    builder.adjust(1)
    return builder.as_markup()


# ── Finance keyboards ─────────────────────────────────────────────────────────

def finance_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="شارژ همه کاربران", callback_data="admin:finance_bulk_credit")
    builder.button(text="تغییر قیمت محصولات", callback_data="admin:finance_price_adj")
    builder.button(text="نرخ ارز", callback_data="admin:exrate")
    builder.button(text="NOWPayments", callback_data="admin:np")
    builder.button(text="بازگشت", callback_data="admin_panel")
    builder.adjust(1)
    return builder.as_markup()


def price_adj_categories_kb(categories: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="همه محصولات", callback_data="admin:price_cat:__all__")
    for cat in categories:
        builder.button(text=cat, callback_data=f"admin:price_cat:{cat}")
    builder.button(text="انصراف", callback_data="admin:finance")
    builder.adjust(1)
    return builder.as_markup()


# ── Broadcast keyboards ───────────────────────────────────────────────────────

def broadcast_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="ارسال به همه", callback_data="admin:bc_filter:all")
    builder.button(text="فقط خریداران", callback_data="admin:bc_filter:buyers")
    builder.button(text="فقط غیرخریداران", callback_data="admin:bc_filter:non_buyers")
    builder.button(text="بازگشت", callback_data="admin_panel")
    builder.adjust(1)
    return builder.as_markup()


def broadcast_confirm_kb(filter_type: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="ارسال", callback_data=f"admin:bc_send:{filter_type}"),
            InlineKeyboardButton(text="انصراف", callback_data="admin:broadcast"),
        ]
    ])


# ── Sub-product keyboards ─────────────────────────────────────────────────────

def subproducts_kb(plan_id: int, sub_products: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for sp in sub_products:
        builder.button(
            text=f"{_st(sp.is_active)}{sp.name} — {sp.price:,.0f}T",
            callback_data=f"admin:subprod:{sp.id}",
        )
    builder.button(text="افزودن ریز-محصول", callback_data=f"admin:subprod_add:{plan_id}")
    builder.button(text="بازگشت", callback_data=f"admin:plan:{plan_id}")
    builder.adjust(1)
    return builder.as_markup()


def subprod_detail_kb(sp_id: int, plan_id: int, is_active: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="نام", callback_data=f"admin:subprod_edit:{sp_id}:name")
    builder.button(text="قیمت", callback_data=f"admin:subprod_edit:{sp_id}:price")
    builder.button(text="مقدار", callback_data=f"admin:subprod_edit:{sp_id}:value")
    toggle = "غیرفعال" if is_active else "فعال"
    builder.button(text=toggle, callback_data=f"admin:subprod_toggle:{sp_id}")
    builder.button(text="حذف", callback_data=f"admin:subprod_del:{sp_id}")
    builder.button(text="بازگشت", callback_data=f"admin:subproducts:{plan_id}")
    builder.adjust(2)
    return builder.as_markup()


def subprod_type_kb(plan_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="ترافیک", callback_data=f"admin:subprod_type:{plan_id}:traffic"),
            InlineKeyboardButton(text="IP اضافه", callback_data=f"admin:subprod_type:{plan_id}:extra_ip"),
        ],
        [InlineKeyboardButton(text="انصراف", callback_data=f"admin:subproducts:{plan_id}")],
    ])


# ── Shared keyboards ──────────────────────────────────────────────────────────

def providers_select_kb(providers: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for p in providers:
        builder.button(text=p.name, callback_data=f"admin:plan_prov:{p.id}")
    builder.button(text="انصراف", callback_data="admin_panel")
    builder.adjust(1)
    return builder.as_markup()


def billing_type_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="فقط ساعتی", callback_data="admin:billing:hourly"),
            InlineKeyboardButton(text="فقط ماهانه", callback_data="admin:billing:monthly"),
        ],
        [InlineKeyboardButton(text="هر دو", callback_data="admin:billing:both")],
        [InlineKeyboardButton(text="انصراف", callback_data="admin_panel")],
    ])


def cancel_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="انصراف", callback_data="admin_panel")]
    ])


def skip_or_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="رد کردن", callback_data="admin:skip"),
            InlineKeyboardButton(text="انصراف", callback_data="admin_panel"),
        ]
    ])


def confirm_kb(confirm_data: str, cancel_data: str = "admin_panel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="تأیید", callback_data=confirm_data),
            InlineKeyboardButton(text="انصراف", callback_data=cancel_data),
        ]
    ])


def np_gateway_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="تست اتصال", callback_data="admin:np_test")
    builder.button(text="تنظیم نرخ $/T", callback_data="admin:np_rate")
    builder.button(text="تنظیم Webhook URL", callback_data="admin:np_wh")
    builder.button(text="بازگشت", callback_data="admin:finance")
    builder.adjust(2)
    return builder.as_markup()


def back_to_admin_kb(target: str = "admin_panel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="بازگشت", callback_data=target)]
    ])
