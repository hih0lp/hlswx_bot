"""«Проверить чаты» — служебный экран раздела «Города и чаты» (ТЗ 6.5).

Раздел жил с этапа 1 и после переезда управления в панель остался без кнопки:
попасть на него было нельзя, хотя экран добавления чата прямо обещает
«проверьте права бота — кнопка "Проверить чаты"». Кнопка возвращена в «Города
и группы», а экран приведён к общей механике панели.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app.core import admin_texts as T
from app.db.session import SessionLocal
from app.handlers.admin.common import _is_admin
from app.keyboards.style import STYLE_MAIN
from app.models.entities import Chat
from app.services import admin_ui

router = Router()

CHATS_CB = "adm:chats"
PLACES_CB = "adm:places"

# Экран уходит подписью к баннеру (1024 символа), поэтому длинные списки
# обрезаем: администратору важны проблемные чаты, а не полный перечень.
OK_LIMIT = 20
BAD_LIMIT = 10

# Группы этих сетей обслуживает релей: публикует бот сети (@HammerPay… / @WPay…),
# а наш бот в группе может и не состоять. «chat not found» для нашего бота там
# не поломка, поэтому членство нашего бота в таких группах не проверяем.
RELAY_NETWORKS = {"hammer": "Hammer", "w": "W"}


@router.callback_query(F.data == CHATS_CB)
async def admin_check_chats(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer("Проверяю…")
    from app.bot.runtime import get_bot

    from app.config import get_settings

    relay_on = get_settings().hammer_relay_enabled
    bot = get_bot()
    async with SessionLocal() as session:
        chats = (
            await session.scalars(
                select(Chat).where(Chat.active.is_(True)).order_by(Chat.sort_order),
            )
        ).all()
        me = await bot.get_me()
        ok_lines: list[str] = []
        relay_lines: list[str] = []
        bad_lines: list[str] = []
        renamed_lines: list[str] = []
        fixed = 0
        for chat in chats:
            label = f"@{chat.telegram_username}"
            chat_id = chat.telegram_chat_id
            if not chat_id:
                try:
                    tg = await bot.get_chat(f"@{chat.telegram_username}")
                except Exception as exc:
                    bad_lines.append(f"❌ {label} — нет id: {str(exc)[:50]}")
                    continue
                chat.telegram_chat_id = tg.id
                fixed += 1
                chat_id = tg.id
            else:
                # Группа может сменить username, id при этом остаётся прежним.
                # Так «Work52Hammer» стал «worknnhammer», а в базе и в запросах
                # к релею ещё месяц ездило мёртвое имя.
                try:
                    tg = await bot.get_chat(chat_id)
                except Exception:
                    tg = None
                if tg and tg.username and tg.username != chat.telegram_username:
                    renamed_lines.append(f"✏️ @{chat.telegram_username} → @{tg.username}")
                    chat.telegram_username = tg.username
                    label = f"@{tg.username}"
                    fixed += 1
            network = (chat.network or "hammer").lower()
            if relay_on and network in RELAY_NETWORKS:
                # Публикует релей. Смотрим только, что сама группа на месте.
                if tg is None:
                    try:
                        tg = await bot.get_chat(f"@{chat.telegram_username}")
                    except Exception as exc:
                        bad_lines.append(f"⚠️ {label} — группа не найдена: {str(exc)[:40]}")
                        continue
                relay_lines.append(f"🔁 {label} · {RELAY_NETWORKS[network]}")
                continue
            try:
                member = await bot.get_chat_member(chat_id, me.id)
            except Exception as exc:
                bad_lines.append(f"❌ {label} — {str(exc)[:60]}")
                continue
            if member.status in {"administrator", "creator", "member"}:
                pin = " 📌" if member.status == "administrator" else ""
                ok_lines.append(f"✅ {label}{pin}")
            else:
                bad_lines.append(f"⚠️ {label} — {member.status}")
        if fixed:
            await session.commit()

    text = T.CHATS_CHECK.format(ok=len(ok_lines), relay=len(relay_lines), bad=len(bad_lines))
    if relay_lines:
        text += T.CHATS_CHECK_RELAY_NOTE
    if fixed:
        text += T.CHATS_CHECK_FIXED.format(count=fixed)
    if renamed_lines:
        text += "\n\n<b>Сменили username:</b>\n" + "\n".join(renamed_lines[:BAD_LIMIT])
    if bad_lines:
        text += "\n\n<b>Проблемы:</b>\n" + "\n".join(bad_lines[:BAD_LIMIT])
    if ok_lines:
        text += "\n\n" + "\n".join(ok_lines[:OK_LIMIT])

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=T.BTN_CHATS_RECHECK, callback_data=CHATS_CB, style=STYLE_MAIN)],
            [admin_ui.back_button(PLACES_CB)],
        ],
    )
    await admin_ui.show(callback, text, markup)
