from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.core.texts import RULES_TEXT
from app.keyboards.helpers import MENU_BACK_TEXTS, MENU_HOME_TEXTS
from app.keyboards.main import inline_home_row, profile_keyboard
from app.services import banners
from app.services.welcome import send_welcome, show_main_menu

start_router = Router()


@start_router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await send_welcome(message)


async def show_rules(message: Message, *, edit: bool = False) -> None:
    """Фрейм «Правила».

    Текст длиннее подписи к фото (1024), поэтому уходит вторым сообщением —
    и по этой же причине экран нельзя перерисовать на месте: edit здесь
    принимается для единообразия вызовов, но всегда отправляет новый экран.
    """
    await banners.show_screen(message, banners.RULES, RULES_TEXT, inline_home_row(), edit=edit)


@start_router.message(Command("rules"))
@start_router.message(F.text.in_({"📄 Правила", "📌 Правила"}))
async def cmd_rules(message: Message) -> None:
    await show_rules(message)


async def show_profile(message: Message, *, tg_user=None, edit: bool = False) -> None:
    """Фрейм «Профиль»."""
    from app.db.session import SessionLocal
    from app.services.users import get_or_create_user
    from app.services.wallet import build_profile_text

    user_obj = tg_user or message.from_user
    async with SessionLocal() as session:
        user = await get_or_create_user(session, user_obj)
    await banners.show_screen(
        message,
        banners.PROFILE,
        build_profile_text(user_obj, user),
        profile_keyboard(),
        edit=edit,
    )


@start_router.message(Command("profile"))
@start_router.message(F.text == "👤 Профиль")
async def cmd_profile(message: Message) -> None:
    await show_profile(message)


@start_router.message(F.text.in_(MENU_HOME_TEXTS))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_main_menu(message)


@start_router.message(F.text.in_(MENU_BACK_TEXTS - MENU_HOME_TEXTS))
async def cmd_back(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_main_menu(message)
