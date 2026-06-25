"""Main menu keyboards."""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from bot.config import settings


def main_menu_kb(is_admin: bool = False) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="🖥 سرور‌های من")
    builder.button(text="🛒 خرید سرور")
    builder.button(text="💰 کیف پول")
    builder.button(text="👤 مشخصات کاربری")
    builder.button(text="🆘 پشتیبانی")
    if settings.WEBAPP_URL:
        builder.button(text="🌐 پنل مدیریت", web_app=WebAppInfo(url=settings.WEBAPP_URL))
    if is_admin:
        builder.button(text="⚙️ پنل ادمین")
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)


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
    builder.button(text="📜 تاریخچه تراکنش‌ها", callback_data="tx_history")
    builder.button(text="🔙 بازگشت", callback_data="main_menu")
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
