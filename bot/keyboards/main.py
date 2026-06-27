"""Main menu keyboards."""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import settings


# Premium emoji IDs for keyboard button icons (Bot API 9.4+, requires bot owner Premium)
_ICON = {
    "server":  "5262701463049609410",
    "buy":     "5346024644635804737",
    "profile": "5974048815789903111",
    "support": "5348323259593014362",
    "admin":   "5895483165182529286",
}


def main_menu_kb(is_admin: bool = False) -> ReplyKeyboardMarkup:
    row1 = [KeyboardButton(text="تهیه سرور", icon_custom_emoji_id=_ICON["buy"], **{"style": "primary"})]
    row2 = [
        KeyboardButton(text="سرور‌های من", icon_custom_emoji_id=_ICON["server"]),
        KeyboardButton(text="مشخصات کاربری", icon_custom_emoji_id=_ICON["profile"]),
    ]
    row3 = [KeyboardButton(text="پشتیبانی", icon_custom_emoji_id=_ICON["support"], **{"style": "success"})]
    if settings.WEBAPP_URL:
        row3.append(KeyboardButton(text="🌐 پنل مدیریت", web_app=WebAppInfo(url=settings.WEBAPP_URL)))
    rows = [row1, row2, row3]
    if is_admin:
        rows.append([KeyboardButton(text="پنل ادمین", icon_custom_emoji_id=_ICON["admin"])])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def request_phone_kb() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="📱 ارسال شماره تلفن", request_contact=True)
    return builder.as_markup(resize_keyboard=True, one_time_keyboard=True)


def remove_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="cancel")]
    ])


def back_kb(callback: str = "main_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data=callback)]
    ])


def wallet_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 شارژ کیف پول", callback_data="charge_wallet")
    builder.button(text="💎 شارژ با کریپتو", callback_data="crypto_pay")
    builder.button(text="🔙 بازگشت", callback_data="user_profile")
    builder.adjust(1)
    return builder.as_markup()


def charge_amount_kb() -> InlineKeyboardMarkup:
    amounts = [10_000, 50_000, 100_000, 200_000, 500_000]
    builder = InlineKeyboardBuilder()
    for a in amounts:
        builder.button(text=f"{a:,} تومان", callback_data=f"charge:{a}")
    builder.button(text="❌ انصراف", callback_data="cancel")
    builder.adjust(2)
    return builder.as_markup()
