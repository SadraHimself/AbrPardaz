"""Wallet and payment handlers."""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import Transaction, TransactionType, User
from bot.keyboards.main import back_kb, charge_amount_kb, wallet_kb
from bot.utils.loading import answer_loading, edit_loading

router = Router(name="billing")

_TX_LIMIT = 20
_CLEANUP_HOURS = 72


def _tx_icon(tx: Transaction) -> str:
    if tx.type == TransactionType.CREDIT or tx.type.value == "credit":
        return "🟢"
    if tx.type == TransactionType.REFUND or tx.type.value == "refund":
        return "🔵"
    return "🔴"


def _tx_sign(tx: Transaction) -> str:
    if tx.type == TransactionType.CREDIT or tx.type.value in ("credit", "refund"):
        return "+"
    return "−"


def _format_tx_list(txs: list) -> str:
    lines = []
    for tx in txs:
        icon = _tx_icon(tx)
        sign = _tx_sign(tx)
        date = tx.created_at.strftime("%Y/%m/%d — %H:%M") if tx.created_at else "—"
        desc = tx.description or "—"
        lines.append(
            f"{icon} {sign}{tx.amount:,.0f} تومان\n"
            f"📅 {date}\n"
            f"📄 {desc}\n"
            f"─────────────────────"
        )
    return "\n".join(lines)


def _build_xml(user: User, txs: list) -> bytes:
    root = ET.Element("invoice")
    root.set("generated", datetime.utcnow().isoformat())

    u_el = ET.SubElement(root, "user")
    ET.SubElement(u_el, "name").text = f"{user.first_name or ''} {user.last_name or ''}".strip() or "—"
    ET.SubElement(u_el, "telegram_id").text = str(user.telegram_id)
    ET.SubElement(u_el, "phone").text = user.phone_number or "—"

    txs_el = ET.SubElement(root, "transactions")
    txs_el.set("count", str(len(txs)))

    total_debit = 0.0
    total_credit = 0.0
    for tx in txs:
        t = ET.SubElement(txs_el, "transaction")
        t.set("id", str(tx.id))
        ET.SubElement(t, "type").text = tx.type.value
        ET.SubElement(t, "amount").text = str(tx.amount)
        ET.SubElement(t, "description").text = tx.description or ""
        ET.SubElement(t, "timestamp").text = tx.created_at.isoformat() if tx.created_at else ""
        if tx.type.value in ("debit",):
            total_debit += tx.amount
        else:
            total_credit += tx.amount

    summary = ET.SubElement(root, "summary")
    ET.SubElement(summary, "total_debit").text = str(total_debit)
    ET.SubElement(summary, "total_credit").text = str(total_credit)
    ET.SubElement(summary, "current_balance").text = str(user.balance)

    buf = io.BytesIO()
    tree = ET.ElementTree(root)
    tree.write(buf, encoding="utf-8", xml_declaration=True)
    return buf.getvalue()


async def _render_wallet(target_msg, user: User):
    await target_msg.edit_text(
        f"💰 <b>کیف پول</b>\n\n"
        f"موجودی فعلی: <b>{user.balance:,.0f} تومان</b>\n\n"
        f"برای شارژ کیف پول دکمه زیر را بزنید:",
        parse_mode="HTML",
        reply_markup=wallet_kb(),
    )


@router.callback_query(F.data == "wallet")
async def cb_wallet(cb: CallbackQuery, user: User):
    await edit_loading(cb.message)
    await cb.answer()
    await _render_wallet(cb.message, user)


@router.message(F.text == "💰 کیف پول")
async def msg_wallet(message: Message, user: User):
    loading = await answer_loading(message)
    await _render_wallet(loading, user)


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
    await edit_loading(cb.message)
    await cb.answer()

    result = await session.execute(
        select(Transaction)
        .where(Transaction.user_id == user.id)
        .order_by(Transaction.created_at.desc())
        .limit(_TX_LIMIT)
    )
    txs = list(result.scalars().all())

    if not txs:
        await cb.message.edit_text(
            "📜 هیچ تراکنشی وجود ندارد.",
            reply_markup=back_kb("wallet"),
        )
        return

    body = _format_tx_list(txs)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 دریافت فاکتور XML", callback_data="invoice_xml")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="wallet")],
    ])
    await cb.message.edit_text(
        f"📜 <b>آخرین {len(txs)} تراکنش</b>\n\n"
        f"{body}\n\n"
        f"<i>⚠️ تراکنش‌ها هر {_CLEANUP_HOURS} ساعت به‌طور خودکار پاک می‌شوند.</i>",
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.callback_query(F.data == "invoice_xml")
async def cb_invoice_xml(cb: CallbackQuery, user: User, session: AsyncSession):
    await cb.answer("⏳ در حال تهیه فاکتور...")
    result = await session.execute(
        select(Transaction)
        .where(Transaction.user_id == user.id)
        .order_by(Transaction.created_at.desc())
        .limit(_TX_LIMIT)
    )
    txs = list(result.scalars().all())
    xml_bytes = _build_xml(user, txs)
    filename = f"invoice_{user.telegram_id}_{datetime.utcnow().strftime('%Y%m%d')}.xml"
    await cb.message.answer_document(
        BufferedInputFile(xml_bytes, filename=filename),
        caption=(
            f"📄 <b>فاکتور تراکنش‌ها</b>\n"
            f"👤 {user.first_name or ''}\n"
            f"📅 {datetime.utcnow().strftime('%Y/%m/%d')}\n"
            f"🔢 تعداد: {len(txs)} تراکنش"
        ),
        parse_mode="HTML",
    )


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
