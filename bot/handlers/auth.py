"""Identity verification (احراز هویت) via Zohal → Shahkar."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import User
from bot.keyboards.main import back_kb
from bot.services.shahkar import (
    ShahkarService, normalize_card, normalize_ir_mobile, valid_birth_date, valid_national_code,
)
from bot.utils.loading import ERR, WARN

router = Router(name="auth")

_DIGIT_TRANS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _verify_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="بازگشت", callback_data="cancel",
                             **{"icon_custom_emoji_id": "5258236805890710909"}),
    ]])


class VerifyStates(StatesGroup):
    full_name = State()
    national_code = State()
    birth_date = State()
    card_number = State()
    phone = State()


# ── Identity verification (Zohal → Shahkar) ───────────────────────────────────

@router.callback_query(F.data == "start_verify")
async def cb_start_verify(cb: CallbackQuery, user: User, state: FSMContext):
    if user.is_kyc_verified:
        await cb.message.edit_text(
            '<tg-emoji emoji-id="6026147640968744910">✅</tg-emoji> '
            "حساب کاربری شما از قبل احراز هویت شده است.",
            parse_mode="HTML",
            reply_markup=back_kb("user_profile"),
        )
        await cb.answer()
        return

    await state.set_state(VerifyStates.full_name)
    await cb.message.edit_text(
        "برای پرداخت ریالی احراز هویت الزامی است\n\n"
        '‏<tg-emoji emoji-id="5983580310292402968">✍️</tg-emoji> '
        "لطفاً <b>نام و نام خانوادگی</b> خود را وارد کنید:",
        parse_mode="HTML",
        reply_markup=_verify_back_kb(),
    )
    await cb.answer()


@router.message(VerifyStates.full_name, F.text)
async def verify_full_name(message: Message, state: FSMContext):
    full = (message.text or "").strip()
    parts = full.split()
    if len(parts) < 2 or len(full) > 100:
        await message.answer(f"{ERR} لطفاً نام و نام خانوادگی را کامل وارد کنید.", parse_mode="HTML")
        return
    await state.update_data(first_name=parts[0], last_name=" ".join(parts[1:]))
    await state.set_state(VerifyStates.national_code)
    await message.answer(
        '‏<tg-emoji emoji-id="5346136537123801643">🪪</tg-emoji> '
        "لطفاً <b>کد ملی</b> ۱۰ رقمی خود را وارد کنید:",
        parse_mode="HTML", reply_markup=_verify_back_kb(),
    )


@router.message(VerifyStates.national_code, F.text)
async def verify_national_code(message: Message, state: FSMContext):
    code = (message.text or "").strip().translate(_DIGIT_TRANS)
    if not valid_national_code(code):
        await message.answer(f"{ERR} کد ملی معتبر نیست. یک کد ملی صحیح ۱۰ رقمی وارد کنید:", parse_mode="HTML")
        return
    await state.update_data(national_code=code)
    await state.set_state(VerifyStates.birth_date)
    await message.answer(
        '‏<tg-emoji emoji-id="5346136537123801643">📅</tg-emoji> لطفا <b>تاریخ تولد</b> خود را وارد کنید\n'
        "مثال: 1377/07/19",
        parse_mode="HTML", reply_markup=_verify_back_kb(),
    )


@router.message(VerifyStates.birth_date, F.text)
async def verify_birth_date(message: Message, state: FSMContext):
    bd = (message.text or "").strip().translate(_DIGIT_TRANS).replace("-", "/").replace(".", "/")
    if not valid_birth_date(bd):
        await message.answer(f"{ERR} تاریخ تولد معتبر نیست. به‌صورت شمسی وارد کنید — مثال: 1377/07/19", parse_mode="HTML")
        return
    await state.update_data(birth_date=bd)
    await state.set_state(VerifyStates.card_number)
    await message.answer(
        '‏<tg-emoji emoji-id="5346227465876423936">💳</tg-emoji> '
        "لطفا شماره کارتی که قصد انجام پرداختی با آن دارید را وارد کنید\n\n"
        '‏<tg-emoji emoji-id="5258503720928288433">ℹ️</tg-emoji> '
        "نکته: این عمل به‌منظور تأییدِ تطابق کارت و صاحب کارت می‌باشد",
        parse_mode="HTML", reply_markup=_verify_back_kb(),
    )


@router.message(VerifyStates.card_number, F.text)
async def verify_card_number(message: Message, state: FSMContext):
    card = normalize_card((message.text or "").strip().translate(_DIGIT_TRANS))
    if not card:
        await message.answer(f"{ERR} شماره کارت معتبر نیست. یک شماره کارت ۱۶ رقمی صحیح وارد کنید:", parse_mode="HTML")
        return
    await state.update_data(card_number=card)
    await state.set_state(VerifyStates.phone)
    await message.answer(
        'لطفاً <tg-emoji emoji-id="5172893417717367746">📱</tg-emoji> '
        "<b>شماره تلفن</b> خود را وارد کنید:\n"
        '<tg-emoji emoji-id="6030801830739448093">⚠️</tg-emoji> '
        "شماره تلفن وارد شده باید به نام خود شما باشد",
        parse_mode="HTML", reply_markup=_verify_back_kb(),
    )


@router.message(VerifyStates.phone, F.text)
async def verify_phone(message: Message, user: User, state: FSMContext, session: AsyncSession):
    raw = (message.text or "").strip().translate(_DIGIT_TRANS)
    phone = normalize_ir_mobile(raw)
    if not phone:
        await message.answer(f"{ERR} شماره معتبر نیست. لطفاً یک شماره موبایل ایرانی معتبر وارد کنید.", parse_mode="HTML")
        return

    data = await state.get_data()
    national_code = data.get("national_code", "")
    first_name = data.get("first_name", "")
    last_name = data.get("last_name", "")
    birth_date = data.get("birth_date", "")
    card_number = data.get("card_number", "")
    await state.clear()

    wait = await message.answer("⏳ در حال بررسی اطلاعات در سامانه شاهکار و بانک...")

    svc = ShahkarService()
    try:
        shahkar_ok = await svc.verify(phone, national_code)
        # only spend the card-check request if the mobile↔national code matched
        card_ok = await svc.verify_card(national_code, card_number, birth_date) if shahkar_ok else False
    except RuntimeError as e:
        if "not configured" in str(e):
            await wait.edit_text(
                f"{WARN} سرویس احراز هویت هنوز پیکربندی نشده است. با پشتیبانی تماس بگیرید.",
                parse_mode="HTML",
            )
            return
        shahkar_ok = card_ok = False
    except Exception:
        await wait.edit_text(
            f"{WARN} خطا در ارتباط با سامانه احراز هویت. لطفاً کمی بعد دوباره تلاش کنید.",
            parse_mode="HTML",
        )
        return

    if not shahkar_ok:
        await wait.edit_text(
            f"{ERR} شماره موبایل با کد ملی مطابقت ندارد.\n"
            "مطمئن شوید شماره به نام همین کد ملی ثبت شده باشد و دوباره اقدام کنید.",
            parse_mode="HTML",
        )
        return
    if not card_ok:
        await wait.edit_text(
            f"{ERR} شماره کارت با کد ملی/تاریخ تولد مطابقت ندارد.\n"
            "کارت بانکی باید به نام خودتان باشد. دوباره از «احراز هویت» اقدام کنید.",
            parse_mode="HTML",
        )
        return

    # Store the verified identity. Keep the FULL card (needed for the Zarinpal
    # card_pan lock) but never expose it — the profile shows only the last 4 digits.
    user.national_id = national_code
    user.phone_number = phone
    user.is_kyc_verified = True
    user.is_phone_verified = True
    extra = dict(user.extra_data or {})
    extra["verified_name"] = f"{first_name} {last_name}".strip()
    extra["card_pan"] = card_number
    extra["birth_date"] = birth_date
    user.extra_data = extra
    await session.flush()

    masked = "**** **** **** " + card_number[-4:]
    await wait.edit_text(
        '<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji> '
        "<b>احراز هویت با موفقیت انجام شد!</b>\n\n"
        f"نام: {first_name} {last_name}\n"
        f"کد ملی: <code>{national_code}</code>\n"
        f"شماره: <code>{phone}</code>\n"
        f"کارت: <code>{masked}</code>",
        parse_mode="HTML",
    )
