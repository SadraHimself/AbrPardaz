"""تعرفه‌های ترافیک اضافه (فعلاً فقط ویرچولایزور).

هر اکانت ویرچولایزور تعرفه‌های خودش را دارد؛ در `ProviderAccount.extra_config`
زیر کلید `traffic_tariffs` به‌صورت لیستی از دیکشنری ذخیره می‌شود (بدون جدول
جدید — الگوی همان `change_ip_fee`/`extra_ip_fee`):

    {"id": 1, "name": "۱۰۰ گیگابایت", "gb": 100, "price": 5,
     "currency": "eur", "is_active": true}

قیمت به ارز خودش ذخیره می‌شود و **فقط لحظه‌ی خرید** با نرخ روز به تومان تبدیل
می‌شود — دقیقاً مثل قیمت‌گذاری پلن‌ها (bot/services/currency.py).
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import ProviderAccount

_KEY = "traffic_tariffs"


def get_tariffs(account: ProviderAccount, active_only: bool = False) -> list[dict]:
    raw = (account.extra_config or {}).get(_KEY) or []
    items = [t for t in raw if isinstance(t, dict)]
    if active_only:
        items = [t for t in items if t.get("is_active", True)]
    return sorted(items, key=lambda t: float(t.get("gb") or 0))


def get_tariff(account: ProviderAccount, tariff_id: int) -> dict | None:
    for t in get_tariffs(account):
        if int(t.get("id") or 0) == int(tariff_id):
            return t
    return None


def _save(account: ProviderAccount, items: list[dict]) -> None:
    # JSON column: دیکشنری باید دوباره ست شود تا SQLAlchemy تغییر را ببیند
    cfg = dict(account.extra_config or {})
    cfg[_KEY] = items
    account.extra_config = cfg


async def add_tariff(session: AsyncSession, account: ProviderAccount, *, name: str,
                     gb: float, price: float, currency: str) -> dict:
    items = get_tariffs(account)
    new_id = max((int(t.get("id") or 0) for t in items), default=0) + 1
    item = {"id": new_id, "name": name, "gb": float(gb), "price": float(price),
            "currency": (currency or "irt").lower(), "is_active": True}
    items.append(item)
    _save(account, items)
    await session.flush()
    return item


async def toggle_tariff(session: AsyncSession, account: ProviderAccount,
                        tariff_id: int) -> bool | None:
    items = get_tariffs(account)
    state = None
    for t in items:
        if int(t.get("id") or 0) == int(tariff_id):
            t["is_active"] = not t.get("is_active", True)
            state = t["is_active"]
    if state is None:
        return None
    _save(account, items)
    await session.flush()
    return state


async def delete_tariff(session: AsyncSession, account: ProviderAccount,
                        tariff_id: int) -> bool:
    items = get_tariffs(account)
    remaining = [t for t in items if int(t.get("id") or 0) != int(tariff_id)]
    if len(remaining) == len(items):
        return False
    _save(account, remaining)
    await session.flush()
    return True


async def price_toman(session: AsyncSession, tariff: dict) -> float:
    """قیمت تعرفه به تومان با نرخ روز. صفر = نرخ ارز در دسترس نیست."""
    from bot.services.currency import to_toman
    price = float(tariff.get("price") or 0)
    cur = (tariff.get("currency") or "irt").lower()
    if price <= 0:
        return 0.0
    if cur == "irt":
        return price
    return await to_toman(session, price, cur)
