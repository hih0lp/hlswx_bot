#!/usr/bin/env python3
"""Сетка кнопок экрана против кадра макета.

Текст экранов сверяет `figma_screens.py`, и он же стоит тестом. Кнопки до
20.09.2026 не сверял никто — а обе последние жалобы заказчика были именно про
них: пять кнопок минут в один ряд вместо 2+2+1 и баннер на каждом экране.
Подписи проверялись (`test_button_labels.py`, `test_admin_button_widths.py`),
но только на ширину: сколько кнопок стоит в ряду, не смотрел ни один тест.

Статически это не снять — клавиатуры собираются внутри обработчиков из данных.
Поэтому экран поднимается **по-настоящему**: обработчик вызывается с
поддельным Telegram, и разбирается та разметка, которую он реально отдал.

Привязка «обработчик → кадр» берётся оттуда же, откуда её берёт тест текста, —
из кода, а не из отдельной таблицы, которая протухнет:

1. номер кадра в докстроке обработчика («кадр макета 341:1248»);
2. если его нет — через константу текста: обработчик форматирует `T.NAME`,
   а над `NAME` в `admin_texts.py` стоит комментарий с номером кадра.

Сверяется **форма**: сколько кнопок в каждом ряду. Подписи не сверяем — на
экранах списков они собраны из данных, и образцы в макете всё равно свои.
Ряды-записи (в макете это подписи с «@username» или «#124», многострочные) и
пагинация из сравнения выброшены: их число зависит от содержимого базы.

    DATABASE_URL=... PYTHONPATH=. ./scripts/figma_buttons.py
    DATABASE_URL=... PYTHONPATH=. ./scripts/figma_buttons.py -v   # и совпавшие
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import glob
import json
import os
import re
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from figma_read import frames_of, render  # noqa: E402
from figma_screens import mapping  # noqa: E402

DUMP = "docs/design/figma.json"
ADMIN_ID = 777
FRAME_ID = re.compile(r"\b(\d+:\d+)\b")
CONST_REF = re.compile(r"\bT\.([A-Z][A-Z0-9_]*)")
# Образец данных в подписи кнопки макета: номер записи, username, дата, счётчик
# страниц. Такие ряды бот строит из базы, и их число сравнивать бессмысленно.
SAMPLE = re.compile(r"@\w|#\d|\d{2}\.\d{2}\.\d{4}|\d+\s*/\s*\d+|‹|›")


def anchors() -> list[tuple[str, str, str, list[str]]]:
    """(модуль, обработчик, выражение callback_data, кадры).

    `callback_data` возвращаем **выражением**, а не значением: в декораторах
    стоят константы модуля (`F.data == INTERVAL_CB`), а не литералы, и
    раскрыть их можно только после импорта модуля.
    """
    const_frames: dict[str, list[str]] = {}
    for ids, name in mapping("app/core/admin_texts.py"):
        const_frames.setdefault(name, []).extend(ids)

    out = []
    for path in sorted(glob.glob("app/handlers/admin/*.py")):
        module = os.path.basename(path)[:-3]
        if module == "__init__":
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            decorators = [ast.unparse(d) for d in node.decorator_list]
            if not any("callback_query" in d for d in decorators):
                continue
            # Берём только кнопки без аргументов: `F.data == <что-то>`.
            # У `startswith` в callback_data едет id записи, и без базы с
            # нужной строкой экран не поднять.
            exact = re.findall(r"F\.data == ([^)\s]+)", " ".join(decorators))
            if not exact:
                continue
            body = ast.unparse(node)
            ids = FRAME_ID.findall(ast.get_docstring(node) or "")
            if not ids:
                used = [m for m in CONST_REF.findall(body) if m in const_frames]
                ids = sorted({f for name in used for f in const_frames[name]})
            if ids:
                out.append((module, node.name, exact[0], ids))
    return out


def resolve_data(module, expression: str) -> str | None:
    """Значение `callback_data` из выражения декоратора."""
    if expression.startswith(('"', "'")):
        return expression.strip("\"'")
    return getattr(module, expression, None)


def design_rows(frame) -> list[int]:
    """Сколько кнопок в каждом ряду кадра, без рядов-записей и пагинации."""
    rows = []
    for kind, payload in render(frame):
        if kind != "row":
            continue
        if any("\n" in label or SAMPLE.search(label) for label in payload):
            continue
        rows.append(len(payload))
    return rows


def bot_rows(markup) -> list[int]:
    if markup is None:
        return []
    rows = []
    for row in markup.inline_keyboard:
        labels = [b.text for b in row]
        if any("\n" in label or SAMPLE.search(label) for label in labels):
            continue
        rows.append(len(labels))
    return rows


class FakeBot:
    """Телеграм, от которого нужна только отданная разметка."""

    id = 1

    def __init__(self) -> None:
        self.markup = None
        self.text = ""

    async def __call__(self, method, *a, **kw):
        return True

    def _keep(self, text, markup):
        if markup is not None or text:
            self.markup, self.text = markup, text or ""

    async def send_message(self, chat_id, text=None, **kw):
        self._keep(text, kw.get("reply_markup"))
        return _message()

    async def send_photo(self, chat_id, photo=None, caption=None, **kw):
        self._keep(caption, kw.get("reply_markup"))
        return _message()

    async def edit_message_text(self, text=None, **kw):
        self._keep(text, kw.get("reply_markup"))
        return _message()

    async def edit_message_media(self, media=None, **kw):
        self._keep(getattr(media, "caption", ""), kw.get("reply_markup"))
        return _message()

    async def edit_message_reply_markup(self, **kw):
        return True


def _message():
    from aiogram.types import Chat, Message, User

    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=ADMIN_ID, type="private"),
        from_user=User(id=1, is_bot=True, first_name="bot"),
        text="экран",
    )


async def open_screen(data: str, handler):
    """Поднять экран по callback_data и вернуть отданную разметку."""
    import inspect

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import CallbackQuery, User

    bot = FakeBot()
    callback = CallbackQuery(
        id="1",
        from_user=User(id=ADMIN_ID, is_bot=False, first_name="admin"),
        chat_instance="t",
        data=data,
        message=_message().as_(bot),
    ).as_(bot)

    args = [callback]
    if "state" in inspect.signature(handler).parameters:
        args.append(
            FSMContext(
                storage=MemoryStorage(),
                key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID),
            ),
        )
    await handler(*args)
    return bot.markup


async def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true", help="показывать совпавшие")
    args = p.parse_args()

    if not os.path.exists(DUMP):
        sys.exit(f"нет {DUMP} — сначала выгрузите дамп")
    with open(DUMP) as handle:
        frames = {f["id"]: f for f in frames_of(json.load(handle))}

    import app.services.access as access

    access.is_admin = lambda user_id: True
    access.is_staff = lambda user_id: True
    import app.handlers.admin.common as common

    common._is_admin = lambda user_id: True

    import importlib

    modules = {}
    for path in sorted(glob.glob("app/handlers/admin/*.py")):
        name = os.path.basename(path)[:-3]
        if name == "__init__":
            continue
        module = importlib.import_module(f"app.handlers.admin.{name}")
        module._is_admin = lambda user_id: True
        modules[name] = module

    checked = bad = skipped = 0
    for module_name, func_name, expression, ids in anchors():
        module = modules.get(module_name)
        handler = getattr(module, func_name, None)
        data = resolve_data(module, expression)
        if handler is None or data is None:
            skipped += 1
            print(f"=== {func_name} — не раскрылось callback_data «{expression}»")
            continue
        try:
            markup = await open_screen(data, handler)
        except Exception as exc:                      # noqa: BLE001
            skipped += 1
            print(f"=== {func_name} ({data}) — экран не поднялся: {type(exc).__name__}: {exc}")
            continue
        if markup is None:
            skipped += 1
            print(f"=== {func_name} ({data}) — экран не отдал разметку")
            continue
        checked += 1
        got = bot_rows(markup)
        for frame_id in ids:
            frame = frames.get(frame_id)
            if frame is None:
                continue
            want = design_rows(frame)
            same = want == got
            if same and not args.verbose:
                continue
            bad += 0 if same else 1
            mark = "совпадает" if same else "РАСХОЖДЕНИЕ"
            print(f"=== {func_name}  кадр {frame_id} ({frame['name']}) — {mark}")
            print(f"    макет: {want}")
            print(f"    бот  : {got}")
            if not same:
                print(f"    кнопки бота: {[[b.text for b in r] for r in markup.inline_keyboard]}")
    print(f"\nсверено экранов: {checked}, с расхождением сетки: {bad}", file=sys.stderr)
    if skipped:
        print(f"не поднялось экранов: {skipped}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
