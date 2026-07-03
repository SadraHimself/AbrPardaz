"""Identity verification (احراز هویت) via Zohal → Shahkar."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import User
from bot.keyboards.main import cancel_kb
from bot.services.shahkar import ShahkarService, normalize_ir_mobile, valid_national_code

router = Router(name="auth")

_DIGIT_TRANS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


class VerifyStates(StatesGroup):
    first_name = State()
    last_name = State()
    national_code = State()
    phone = State()


# ── Identity verification (Zohal → Shahkar) ───────────────────────────────────

@router.callback_query(F.data == "start_verify")
async def cb_start_verify(cb: CallbackQuery, user: User, state: FSMContext):
    if user.is_kyc_verified:
        await cb.answer("شما قبلاً احراز هویت شده‌اید.", show_alert=True)
        return

    await state.set_state(VerifyStates.first_name)
    await cb.message.edit_text(
        "🪪 <b>احراز هویت</b>\n\n"
        "لطفاً <b>نام</b> خود را وارد کنید:",
        parse_mode="HTML",
        reply_markup=cancel_kb(),
    )
    await cb.answer()


@router.message(VerifyStates.first_name, F.text)
async def verify_first_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not (2 <= len(name) <= 50):
        await message.answer("❌ نام معتبر نیست. دوباره وارد کنید:")
        return
    await state.update_data(first_name=name)
    await state.set_state(VerifyStates.last_name)
    await message.answer(
        "لطفاً <b>نام خانوادگی</b> خود را وارد کنید:",
        parse_mode="HTML", reply_markup=cancel_kb(),
    )


@router.message(VerifyStates.last_name, F.text)
async def verify_last_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not (2 <= len(name) <= 50):
        await message.answer("❌ نام خانوادگی معتبر نیست. دوباره وارد کنید:")
        return
    await state.update_data(last_name=name)
    await state.set_state(VerifyStates.national_code)
    await message.answer(
        "لطفاً <b>کد ملی</b> ۱۰ رقمی خود را وارد کنید:",
        parse_mode="HTML", reply_markup=cancel_kb(),
    )


@router.message(VerifyStates.national_code, F.text)
async def verify_national_code(message: Message, state: FSMContext):
    code = (message.text or "").strip().translate(_DIGIT_TRANS)
    if not valid_national_code(code):
        await message.answer("❌ کد ملی معتبر نیست. یک کد ملی صحیح ۱۰ رقمی وارد کنید:")
        return
    await state.update_data(national_code=code)
    await state.set_state(VerifyStates.phone)
    await message.answer(
        "لطفاً <b>شماره تلفن</b> خود را وارد کنید:\n"
        "<i>فقط شماره موبایل ایرانی — مثال: 09121234567</i>",
        parse_mode="HTML", reply_markup=cancel_kb(),
    )


@router.message(VerifyStates.phone, F.text)
async def verify_phone(message: Message, user: User, state: FSMContext, session: AsyncSession):
    raw = (message.text or "").strip().translate(_DIGIT_TRANS)
    phone = normalize_ir_mobile(raw)
    if not phone:
        await message.answer(
            "❌ شماره معتبر نیست. یک شماره موبایل ایرانی وارد کنید:\n"
            "<i>مثال: 09121234567</i>",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    national_code = data.get("national_code", "")
    await state.clear()

    wait = await message.answer("⏳ در حال بررسی اطلاعات در سامانه شاهکار...")

    try:
        matched = await ShahkarService().verify(phone, national_code)
    except RuntimeError as e:
        if "not configured" in str(e):
            await wait.edit_text(
                "⚠️ سرویس احراز هویت هنوز پیکربندی نشده است. با پشتیبانی تماس بگیرید."
            )
            return
        matched = False
    except Exception:
        await wait.edit_text(
            "⚠️ خطا در ارتباط با سامانه احراز هویت. لطفاً کمی بعد دوباره تلاش کنید."
        )
        return

    if not matched:
        await wait.edit_text(
            "❌ اطلاعات وارد شده مطابقت ندارد.\n"
            "مطمئن شوید شماره موبایل به نام همین کد ملی ثبت شده باشد، "
            "سپس دوباره از دکمه «احراز هویت» اقدام کنید."
        )
        return

    user.first_name = data.get("first_name") or user.first_name
    user.last_name = data.get("last_name") or user.last_name
    user.national_id = national_code
    user.phone_number = phone
    user.is_kyc_verified = True
    user.is_phone_verified = True
    await session.flush()

    await wait.edit_text(
        "✅ <b>احراز هویت با موفقیت انجام شد!</b>\n\n"
        f"نام: {user.first_name} {user.last_name}\n"
        f"کد ملی: <code>{national_code}</code>\n"
        f"شماره: <code>{phone}</code>",
        parse_mode="HTML",
    )
