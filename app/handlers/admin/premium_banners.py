"""Premium clone banner customization (franchise stage 3)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.db.session import SessionLocal
from app.keyboards.style import STYLE_MAIN, STYLE_PLAIN
from app.models.entities import AppSetting
from app.services import access, admin_ui
from app.services.tenant import current_partner_id, current_partner_tier
from app.states.admin import AdminFlow

router = Router()
SETTING_PREFIX = "premium_banner:"
BANNERS = (
    ("main_menu", "Главное меню"),
    ("profile", "Профиль"),
    ("rules", "Правила"),
    ("ad", "Оформление подписки"),
    ("publication", "Публикация"),
    ("subscription", "Подписка"),
    ("topup", "Пополнение"),
    ("integration", "Интеграция"),
    ("attention", "Уведомление"),
)


def _can_manage(user_id: int | None) -> bool:
    return bool(
        user_id
        and current_partner_id()
        and current_partner_tier() == "premium"
        and access.is_admin(user_id)
    )


async def _screen(target: Message | CallbackQuery, user_id: int) -> None:
    if not _can_manage(user_id):
        if isinstance(target, CallbackQuery):
            await target.answer("Баннеры доступны администратору клона на тарифе Премиум", show_alert=True)
        else:
            await target.answer("Баннеры доступны администратору клона на тарифе Премиум")
        return
    async with SessionLocal() as session:
        rows = (await session.scalars(
            select(AppSetting).where(
                AppSetting.partner_id == current_partner_id(),
                AppSetting.key.like(f"{SETTING_PREFIX}%"),
            ),
        )).all()
    configured = {row.key.removeprefix(SETTING_PREFIX) for row in rows}
    buttons = []
    for key, label in BANNERS:
        state = "✅ свой" if key in configured else "по умолчанию"
        row = [InlineKeyboardButton(
            text=f"{label} · {state}", callback_data=f"adm:banners:set:{key}", style=STYLE_PLAIN,
        )]
        if key in configured:
            row.append(InlineKeyboardButton(
                text="↩️ Базовый", callback_data=f"adm:banners:reset:{key}", style=STYLE_PLAIN,
            ))
        buttons.append(row)
    buttons.append([admin_ui.panel_button()])
    await admin_ui.show(
        target,
        "<b>🖼 Баннеры</b>\nВыберите экран, чтобы заменить изображение.\nФото загружается только в этот бот-клон.",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data == "adm:banners")
async def premium_banners_home(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await _screen(callback, callback.from_user.id)


@router.callback_query(F.data.startswith("adm:banners:set:"))
async def premium_banner_choose(callback: CallbackQuery, state: FSMContext) -> None:
    if not _can_manage(callback.from_user.id):
        await callback.answer("Доступно только администратору клона Премиум", show_alert=True)
        return
    key = callback.data.rsplit(":", 1)[-1]
    if key not in {item[0] for item in BANNERS}:
        await callback.answer("Неизвестный экран", show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await state.update_data(premium_banner_key=key)
    await state.set_state(AdminFlow.premium_banner_photo)
    label = dict(BANNERS)[key]
    await admin_ui.prompt(
        callback,
        f"Пришлите одним сообщением новое фото для экрана «{label}».\n"
        "Отправка другого сообщения не сохранит баннер. Чтобы вернуться, нажмите «Назад».",
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button("adm:banners")]]),
    )


@router.message(AdminFlow.premium_banner_photo)
async def premium_banner_save(message: Message, state: FSMContext) -> None:
    if not message.from_user or not _can_manage(message.from_user.id):
        await state.clear()
        return
    if not message.photo:
        await admin_ui.prompt(
            message,
            "Нужно отправить изображение как фото Telegram.",
            InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button("adm:banners")]]),
        )
        return
    data = await state.get_data()
    key = data.get("premium_banner_key")
    if key not in {item[0] for item in BANNERS}:
        await state.clear()
        await admin_ui.show(message, "Не удалось определить экран. Откройте «Баннеры» ещё раз.")
        return
    setting_key = SETTING_PREFIX + key
    file_id = message.photo[-1].file_id
    async with SessionLocal() as session:
        setting = await session.scalar(select(AppSetting).where(
            AppSetting.partner_id == current_partner_id(),
            AppSetting.key == setting_key,
        ))
        if setting is None:
            setting = AppSetting(partner_id=current_partner_id(), key=setting_key, value=file_id)
            session.add(setting)
        else:
            setting.value = file_id
        setting.updated_by_telegram_id = message.from_user.id
        await session.commit()
    await state.clear()
    await admin_ui.show(
        message,
        f"Баннер для «{dict(BANNERS)[key]}» сохранён только в этом клоне.",
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button("adm:banners")]]),
    )


@router.callback_query(F.data.startswith("adm:banners:reset:"))
async def premium_banner_reset(callback: CallbackQuery) -> None:
    if not _can_manage(callback.from_user.id):
        await callback.answer("Доступно только администратору клона Премиум", show_alert=True)
        return
    key = callback.data.rsplit(":", 1)[-1]
    if key not in {item[0] for item in BANNERS}:
        await callback.answer("Неизвестный экран", show_alert=True)
        return
    async with SessionLocal() as session:
        setting = await session.scalar(select(AppSetting).where(
            AppSetting.partner_id == current_partner_id(),
            AppSetting.key == SETTING_PREFIX + key,
        ))
        if setting:
            await session.delete(setting)
            await session.commit()
    await callback.answer("Возвращён базовый баннер")
    await _screen(callback, callback.from_user.id)
