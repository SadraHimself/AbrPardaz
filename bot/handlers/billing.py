"""Wallet and payment handlers."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import Transaction, User
from bot.keyboards.main import back_kb, charge_amount_kb, wallet_kb

router = Router(name="billing")


@router.callback_query(F.data == "wallet")
async def cb_wallet(cb: CallbackQuery, user: User):
    await cb.message.edit_text(
        f"💰 <b>کیف پول</b>\n\n"
        f"موجودی فعلی: <b>{user.balance:,.0f} تومان</b>\n\n"
        f"برای شارژ کیف پول دکمه زیر را بزنید:",
        parse_mode="HTML",
        reply_markup=wallet_kb(),
    )
    await cb.answer()


@router.callback_query(F.data == "charge_wallet")
async def cb_charge_wallet(cb: CallbackQuery):
    await cb.message.edit_text(
        "💳 <b>شارژ کیف پول</b>\n\nمبلغ مورد نظر را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=charge_amount_kb(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("charge:"))
async def cb_do_charge(cb: CallbackQuery, user: User):
    amount = int(cb.data.split(":")[1])
    # TODO: اتصال به درگاه پرداخت (ZarinPal, IDPay, ...)
    # فعلاً placeholder
    await cb.message.edit_text(
        f"💳 <b>پرداخت آنلاین</b>\n\n"
        f"مبلغ: {amount:,} تومان\n\n"
        "⏳ درگاه پرداخت در حال راه‌اندازی است.\n"
        "این قابلیت در نسخه بعدی اضافه می‌شود.",
        parse_mode="HTML",
        reply_markup=back_kb("wallet"),
    )
    await cb.answer()


@router.callback_query(F.data == "tx_history")
async def cb_tx_history(cb: CallbackQuery, user: User, session: AsyncSession):
    result = await session.execute(
        select(Transaction)
        .where(Transaction.user_id == user.id)
        .order_by(Transaction.created_at.desc())
        .limit(10)
    )
    txs = list(result.scalars().all())

    if not txs:
        await cb.message.edit_text(
            "📜 هیچ تراکنشی وجود ندارد.",
            reply_markup=back_kb("wallet"),
        )
        await cb.answer()
        return

    lines = []
    for tx in txs:
        sign = "+" if tx.type.value == "credit" else "-"
        date = tx.created_at.strftime("%m/%d %H:%M")
        lines.append(f"{sign}{tx.amount:,.0f}  |  {tx.description or ''}  |  {date}")

    await cb.message.edit_text(
        f"📜 <b>۱۰ تراکنش آخر</b>\n\n<code>" + "\n".join(lines) + "</code>",
        parse_mode="HTML",
        reply_markup=back_kb("wallet"),
    )
    await cb.answer()


@router.callback_query(F.data == "traffic")
async def cb_traffic_overview(cb: CallbackQuery, user: User, session: AsyncSession):
    from bot.database.models import Server, ServerStatus
    result = await session.execute(
        select(Server).where(
            Server.user_id == user.id,
            Server.status == ServerStatus.ACTIVE,
        )
    )
    servers = list(result.scalars().all())

    if not servers:
        await cb.message.edit_text("هیچ سرور فعالی ندارید.", reply_markup=back_kb())
        await cb.answer()
        return

    lines = []
    for s in servers:
        lim = f"{s.traffic_limit_gb:.0f}GB" if s.traffic_limit_gb else "∞"
        pct = int(s.traffic_used_gb / s.traffic_limit_gb * 100) if s.traffic_limit_gb else 0
        lines.append(f"🖥 {s.name}: {s.traffic_used_gb:.1f}/{lim} ({pct}%)")

    await cb.message.edit_text(
        "📊 <b>وضعیت ترافیک سرور‌ها</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=back_kb(),
    )
    await cb.answer()
