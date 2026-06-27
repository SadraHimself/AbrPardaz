"""Wallet and payment handlers."""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import Server, Transaction, TransactionType, User
from bot.keyboards.main import back_kb, charge_amount_kb, wallet_kb
from bot.utils.loading import answer_loading, edit_loading

router = Router(name="billing")

_CLEANUP_HOURS = 72
_PAGE_SIZE = 8
_TEHRAN = timezone(timedelta(hours=3, minutes=30))


# ── XML invoice builder ──────────────────────────────────────────────────────

def _build_xml(user: User, txs: list) -> bytes:
    root = ET.Element("invoice")
    root.set("generated", datetime.utcnow().isoformat())
    u_el = ET.SubElement(root, "user")
    ET.SubElement(u_el, "name").text = f"{user.first_name or ''} {user.last_name or ''}".strip() or "—"
    ET.SubElement(u_el, "telegram_id").text = str(user.telegram_id)
    ET.SubElement(u_el, "phone").text = user.phone_number or "—"
    txs_el = ET.SubElement(root, "transactions")
    txs_el.set("count", str(len(txs)))
    total_debit = total_credit = 0.0
    for tx in txs:
        t = ET.SubElement(txs_el, "transaction")
        t.set("id", str(tx.id))
        ET.SubElement(t, "type").text = tx.type.value
        ET.SubElement(t, "amount").text = str(tx.amount)
        ET.SubElement(t, "description").text = tx.description or ""
        ET.SubElement(t, "timestamp").text = tx.created_at.isoformat() if tx.created_at else ""
        if tx.type.value == "debit":
            total_debit += tx.amount
        else:
            total_credit += tx.amount
    summary = ET.SubElement(root, "summary")
    ET.SubElement(summary, "total_debit").text = str(total_debit)
    ET.SubElement(summary, "total_credit").text = str(total_credit)
    ET.SubElement(summary, "current_balance").text = str(user.balance)
    buf = io.BytesIO()
    ET.ElementTree(root).write(buf, encoding="utf-8", xml_declaration=True)
    return buf.getvalue()


# ── Wallet ───────────────────────────────────────────────────────────────────

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


@router.message(F.text == "کیف پول")
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


# ── Transaction history ──────────────────────────────────────────────────────

def _tz(d: datetime | None) -> datetime:
    """Ensure datetime is timezone-aware (UTC)."""
    if d is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


async def _get_tx_items(user_id: int, session: AsyncSession) -> list[dict]:
    """
    Build a sorted list of display items:
    - Grouped hourly billing per server (description starts with "ساعتی — ")
    - All other transactions individually
    """
    result = await session.execute(
        select(Transaction)
        .where(Transaction.user_id == user_id)
        .order_by(Transaction.created_at.desc())
        .limit(500)
    )
    all_txs = list(result.scalars().all())

    hourly: dict[int, dict] = {}
    others: list[dict] = []

    for tx in all_txs:
        is_hourly = (
            tx.type == TransactionType.DEBIT
            and tx.server_id is not None
            and (tx.description or "").startswith("ساعتی — ")
        )
        if is_hourly:
            g = hourly.setdefault(tx.server_id, {
                "kind": "srv",
                "server_id": tx.server_id,
                "total": 0.0,
                "count": 0,
                "last_date": tx.created_at,
                "rate": tx.amount,
            })
            g["total"] += tx.amount
            g["count"] += 1
            if _tz(tx.created_at) > _tz(g["last_date"]):
                g["last_date"] = tx.created_at
        else:
            others.append({
                "kind": "tx",
                "tx_id": tx.id,
                "amount": tx.amount,
                "type": tx.type,
                "description": tx.description or "",
                "server_id": tx.server_id,
                "created_at": tx.created_at,
            })

    items: list[dict] = list(hourly.values()) + others
    items.sort(key=lambda x: _tz(x.get("last_date") or x.get("created_at")), reverse=True)
    return items


def _item_btn(item: dict, page: int) -> InlineKeyboardButton:
    if item["kind"] == "srv":
        return InlineKeyboardButton(
            text=f"برداشت — {item['total']:,.0f} تومان",
            callback_data=f"tx_srv:{item['server_id']}:{page}",
            **{"style": "danger"},
        )
    is_debit = item["type"] == TransactionType.DEBIT
    return InlineKeyboardButton(
        text=f"{'برداشت' if is_debit else 'واریز'} — {item['amount']:,.0f} تومان",
        callback_data=f"tx_item:{item['tx_id']}:{page}",
        **{"style": "danger" if is_debit else "success"},
    )


async def _render_tx_page(target_msg, user: User, session: AsyncSession, page: int = 0):
    items = await _get_tx_items(user.id, session)

    if not items:
        await target_msg.edit_text("📜 هیچ تراکنشی وجود ندارد.", reply_markup=back_kb("wallet"))
        return

    total_pages = max(1, (len(items) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    page_items = items[page * _PAGE_SIZE: (page + 1) * _PAGE_SIZE]

    buttons = [[_item_btn(item, page)] for item in page_items]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="← قبلی", callback_data=f"tx_page:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="بعدی →", callback_data=f"tx_page:{page + 1}"))
    if nav:
        buttons.append(nav)

    buttons.append([
        InlineKeyboardButton(text="📄 فاکتور XML", callback_data="invoice_xml", **{"style": "primary"}),
    ])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="wallet")])

    await target_msg.edit_text(
        f"📜 <b>تاریخچه تراکنش‌ها</b>  {page + 1}/{total_pages}\n\n"
        f"<i>⚠️ تراکنش‌ها هر {_CLEANUP_HOURS} ساعت پاک می‌شوند.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data == "tx_history")
async def cb_tx_history(cb: CallbackQuery, user: User, session: AsyncSession):
    await edit_loading(cb.message)
    await cb.answer()
    await _render_tx_page(cb.message, user, session, page=0)


@router.callback_query(F.data.startswith("tx_page:"))
async def cb_tx_page(cb: CallbackQuery, user: User, session: AsyncSession):
    page = int(cb.data.split(":")[1])
    await edit_loading(cb.message)
    await cb.answer()
    await _render_tx_page(cb.message, user, session, page)


@router.callback_query(F.data.startswith("tx_srv:"))
async def cb_tx_srv_detail(cb: CallbackQuery, user: User, session: AsyncSession):
    parts = cb.data.split(":")
    server_id = int(parts[1])
    back_page = int(parts[2]) if len(parts) > 2 else 0
    await cb.answer()

    result = await session.execute(
        select(Transaction).where(
            Transaction.user_id == user.id,
            Transaction.server_id == server_id,
            Transaction.type == TransactionType.DEBIT,
        ).order_by(Transaction.created_at.asc())
    )
    hourly_txs = [
        t for t in result.scalars().all()
        if (t.description or "").startswith("ساعتی — ")
    ]

    server = await session.get(Server, server_id)
    srv_name = server.name if server else f"سرور #{server_id}"
    srv_ip = (server.ip_address or "—") if server else "—"

    if not hourly_txs:
        back_row = [[InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"tx_page:{back_page}")]]
        await cb.message.edit_text(
            "تراکنشی یافت نشد.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=back_row),
        )
        return

    rate = hourly_txs[0].amount
    count = len(hourly_txs)
    total = sum(t.amount for t in hourly_txs)

    back_row = [[InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"tx_page:{back_page}")]]
    await cb.message.edit_text(
        f"💸 <b>جزئیات برداشت</b>\n\n"
        f"🖥 سرور: <b>{srv_name}</b>\n"
        f"🌐 آی‌پی: <code>{srv_ip}</code>\n"
        f"💳 نوع: ساعتی\n"
        f"⏱ مدت: {count} ساعت × {rate:,.0f} تومان\n"
        f"💰 مجموع: <b>{total:,.0f} تومان</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=back_row),
    )


@router.callback_query(F.data.startswith("tx_item:"))
async def cb_tx_item_detail(cb: CallbackQuery, user: User, session: AsyncSession):
    parts = cb.data.split(":")
    tx_id = int(parts[1])
    back_page = int(parts[2]) if len(parts) > 2 else 0
    await cb.answer()

    tx = await session.get(Transaction, tx_id)
    if not tx or tx.user_id != user.id:
        await cb.answer("تراکنش یافت نشد.", show_alert=True)
        return

    server = await session.get(Server, tx.server_id) if tx.server_id else None

    if tx.type == TransactionType.DEBIT:
        icon, label = "💸", "برداشت وجه"
    elif tx.type == TransactionType.REFUND:
        icon, label = "💚", "برگشت وجه"
    else:
        icon, label = "💰", "واریز"

    date_str = ""
    if tx.created_at:
        d = _tz(tx.created_at).astimezone(_TEHRAN)
        date_str = d.strftime("%Y/%m/%d — %H:%M")

    lines = [f"{icon} <b>{label}</b>", "", f"💰 مبلغ: <b>{tx.amount:,.0f} تومان</b>"]
    if tx.description:
        lines.append(f"📝 شرح: {tx.description}")
    if server:
        lines.append(f"🖥 سرور: {server.name}")
        if server.ip_address:
            lines.append(f"🌐 آی‌پی: <code>{server.ip_address}</code>")
    if date_str:
        lines.append(f"📅 {date_str}")

    back_row = [[InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"tx_page:{back_page}")]]
    await cb.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=back_row),
    )


@router.callback_query(F.data == "invoice_xml")
async def cb_invoice_xml(cb: CallbackQuery, user: User, session: AsyncSession):
    await cb.answer("⏳ در حال تهیه فاکتور...")
    result = await session.execute(
        select(Transaction)
        .where(Transaction.user_id == user.id)
        .order_by(Transaction.created_at.desc())
        .limit(200)
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
    result = await session.execute(
        select(Server).where(
            Server.user_id == user.id,
            Server.status == Server.status.ACTIVE,
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
