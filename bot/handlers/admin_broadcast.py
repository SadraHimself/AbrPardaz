"""Admin broadcast — queued mass messaging with real forward support."""
from __future__ import annotations

import asyncio
from typing import Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import Server, User, UserStatus
from bot.keyboards.admin import back_to_admin_kb, broadcast_confirm_kb, broadcast_menu_kb

router = Router(name="admin_broadcast")


class AdminFilter(Filter):
    # user برای آپدیت‌های گروهی (سرویس‌پیام‌ها/تاپیک‌ها) ست نمی‌شود — پیش‌فرض None
    # تا فیلتر به‌جای TypeError، فقط False برگرداند
    async def __call__(self, event: Message | CallbackQuery, user: User | None = None) -> bool:
        if user is None:
            return False
        return user.is_admin or (user.telegram_id in settings.admin_ids)


router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())


class BroadcastFSM(StatesGroup):
    waiting_message = State()
    waiting_forward_message = State()
    confirming = State()
    confirming_forward = State()


_FILTER_LABELS = {
    "all": "همه کاربران",
    "buyers": "کاربرانی که خرید داشتن",
    "non_buyers": "کاربرانی که خرید نداشتن",
}


# ── Entry ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:broadcast")
async def cb_broadcast_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(
        "<b>پیام همگانی</b>\n\nمخاطبان را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=broadcast_menu_kb(),
    )
    await cb.answer()


# ── Filter selection message input ─────────────────────────────────────────

@router.callback_query(F.data.startswith("admin:bc_filter:"))
async def cb_bc_filter(cb: CallbackQuery, state: FSMContext):
    filter_type = cb.data.split(":")[2]
    await state.update_data(bc_filter=filter_type)
    await state.set_state(BroadcastFSM.waiting_message)
    await cb.message.edit_text(
        f"<b>پیام به: {_FILTER_LABELS[filter_type]}</b>\n\n"
        "پیام خود را ارسال کنید.\n\n"
        "<b>نکته:</b> برای forward واقعی (حفظ استیکر پریمیوم و Forwarded from)، "
        "پیام را از کانال موردنظر forward کنید.",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb("admin:broadcast"),
    )
    await cb.answer()


@router.message(BroadcastFSM.waiting_message)
async def msg_bc_message(message: Message, state: FSMContext, session: AsyncSession):
    filter_type = (await state.get_data()).get("bc_filter", "all")
    count = await _count_recipients(session, filter_type)

    is_forward = bool(message.forward_from or message.forward_from_chat)

    await state.update_data(
        bc_from_chat_id=message.chat.id,
        bc_message_id=message.message_id,
        bc_is_forward=is_forward,
    )
    await state.set_state(BroadcastFSM.confirming)

    fwd_note = "\nپیام به صورت <b>forward واقعی</b> ارسال می‌شود." if is_forward else ""
    await message.answer(
        f"<b>تأیید ارسال</b>\n\n"
        f"مخاطب: {_FILTER_LABELS[filter_type]}\n"
        f"تعداد: ~{count} نفر{fwd_note}\n\n"
        "آیا ارسال شود؟",
        parse_mode="HTML",
        reply_markup=broadcast_confirm_kb(filter_type),
    )


# ── Send ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("admin:bc_send:"), BroadcastFSM.confirming)
async def cb_bc_send(cb: CallbackQuery, state: FSMContext, session: AsyncSession):
    data = await state.get_data()
    await state.clear()

    filter_type = data["bc_filter"]
    from_chat_id = data["bc_from_chat_id"]
    message_id = data["bc_message_id"]
    is_forward = data.get("bc_is_forward", False)

    recipients = await _get_recipients(session, filter_type)
    total = len(recipients)

    progress_msg = await cb.message.edit_text(
        f"در حال ارسال به {total} کاربر...",
    )
    await cb.answer()

    sent, failed = 0, 0
    for tg_id in recipients:
        try:
            if is_forward:
                await cb.bot.forward_message(
                    chat_id=tg_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                )
            else:
                await cb.bot.copy_message(
                    chat_id=tg_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                )
            sent += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                if is_forward:
                    await cb.bot.forward_message(tg_id, from_chat_id, message_id)
                else:
                    await cb.bot.copy_message(tg_id, from_chat_id, message_id)
                sent += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            failed += 1
        except Exception:
            failed += 1

        # Rate limiting: 25 messages/second max
        if sent % 25 == 0:
            await asyncio.sleep(1)

    await progress_msg.edit_text(
        f"<b>ارسال تمام شد!</b>\n\n"
        f"ارسال موفق: {sent}\n"
        f"ناموفق: {failed}",
        parse_mode="HTML",
        reply_markup=back_to_admin_kb("admin:broadcast"),
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _count_recipients(session: AsyncSession, filter_type: str) -> int:
    query = select(User).where(User.status == UserStatus.ACTIVE)
    if filter_type == "buyers":
        buyer_ids = select(Server.user_id).distinct()
        query = query.where(User.id.in_(buyer_ids))
    elif filter_type == "non_buyers":
        buyer_ids = select(Server.user_id).distinct()
        query = query.where(User.id.notin_(buyer_ids))
    result = await session.execute(select(User.telegram_id).where(
        User.status == UserStatus.ACTIVE
    ).filter(*(_build_filter_conditions(filter_type))))
    return len(list(result.scalars().all()))


async def _get_recipients(session: AsyncSession, filter_type: str) -> list[int]:
    conditions = _build_filter_conditions(filter_type)
    result = await session.execute(
        select(User.telegram_id).where(User.status == UserStatus.ACTIVE, *conditions)
    )
    return list(result.scalars().all())


def _build_filter_conditions(filter_type: str) -> list:
    if filter_type == "buyers":
        from sqlalchemy import exists
        return [exists(select(Server.id).where(Server.user_id == User.id))]
    elif filter_type == "non_buyers":
        from sqlalchemy import exists
        return [~exists(select(Server.id).where(Server.user_id == User.id))]
    return []
