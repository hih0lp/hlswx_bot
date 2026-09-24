"""Навигация и механика экранов админ-панели (ТЗ этапа 2, 6.12 и 6.13).

ТЗ 6.12: «на многошаговых сценариях кнопка "Отмена" заменяется кнопкой
"Назад", которая ведёт на один шаг назад, а не в начало сценария».
ТЗ 6.13: экраны панели — те же баннерные экраны, что и в пользовательской
части, то есть перерисовываются на месте.

Тесты здесь структурные: они читают исходники разделов. Причина — экраны
собираются inline в обработчиках, вызвать их без живого Telegram нельзя, а
расползание навигации уже случалось: три ветки панели годами возвращали
администратора в корень вместо родителя. Проверка на уровне исходника ловит
это на следующем же прогоне.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core import admin_texts as T
from app.keyboards.admin import (
    admin_log_keyboard,
    admin_ml_category_keyboard,
    admin_wlreq_keyboard,
    admin_wlreq_view_keyboard,
)
from app.services import admin_ui

ADMIN_DIR = Path(__file__).resolve().parents[1] / "app" / "handlers" / "admin"
BROADCAST = Path(__file__).resolve().parents[1] / "app" / "handlers" / "admin_broadcast.py"
KEYBOARDS = Path(__file__).resolve().parents[1] / "app" / "keyboards" / "admin.py"

# Разделы, для которых «Назад» = возврат в панель: это корневые пункты меню
# главного экрана (ТЗ 6.1), у них другого родителя нет.
ROOT_SECTIONS = {"home.py"}


def _sources() -> list[Path]:
    return [path for path in ADMIN_DIR.glob("*.py") if path.name != "__init__.py"] + [BROADCAST]


def _last_callback(markup) -> str:
    return markup.inline_keyboard[-1][-1].callback_data


def test_only_admin_ui_hands_out_the_panel_button():
    """`adm:home` живёт в одном месте, иначе «Назад» снова расползётся.

    Исключение — сам главный экран: там это кнопка «Обновить» и собственный
    обработчик.
    """
    offenders = {
        path.name
        for path in _sources()
        if '"adm:home"' in path.read_text(encoding="utf-8") and path.name not in ROOT_SECTIONS
    }
    assert not offenders, (
        f"эти разделы зашивают возврат в корень вместо admin_ui.panel_button(): {sorted(offenders)}"
    )


def test_panel_button_points_at_the_panel():
    assert admin_ui.panel_button().callback_data == admin_ui.HOME_CB
    assert admin_ui.BTN_TO_PANEL.startswith("←")


def test_no_cancel_button_left_in_the_panel():
    """ТЗ 6.12: «Отмена» на многошаговых сценариях заменена на «Назад».

    Ищем именно подпись кнопки, а не слово в тексте: в комментариях оно
    встречается законно — там объясняется, почему кнопки больше нет.
    """
    pattern = re.compile(r"text=[\"'][^\"']*Отмена")
    offenders = {
        path.name for path in _sources() if pattern.search(path.read_text(encoding="utf-8"))
    }
    assert not offenders, f"кнопка «Отмена» осталась в: {sorted(offenders)}"


def test_panel_screens_go_through_admin_ui():
    """Экран панели рисуется на баннере, а не новым сообщением (ТЗ 6.13).

    Исключение — обработчики slash-команд: это ответ на введённую команду, а
    не экран раздела, и баннер там не нужен.
    """
    pattern = re.compile(r"^\s*await (?:callback\.)?message\.answer\(", re.M)
    offenders: dict[str, int] = {}
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        # Обработчики команд отрезаем: они начинаются с @router.message(Command(...
        without_commands = re.split(r"@router\.message\(Command\(", text)[0]
        found = len(pattern.findall(without_commands))
        if found:
            offenders[path.name] = found
    assert not offenders, f"экраны вне admin_ui.show: {offenders}"


def test_no_edit_text_on_banner_screens():
    """`edit_text` на баннере не работает: экран — фото с подписью."""
    offenders = {
        path.name
        for path in _sources()
        if "message.edit_text(" in path.read_text(encoding="utf-8")
    }
    assert not offenders, f"правка подписи через edit_text в: {sorted(offenders)}"


@pytest.mark.parametrize(
    ("markup", "parent"),
    [
        (admin_wlreq_keyboard(), "adm:wlbl"),
        (admin_wlreq_view_keyboard(1, can_partner=True), "adm:wlreq"),
        (admin_log_keyboard(1), "adm:ml"),
        (admin_log_keyboard(1, back_cb="adm:ml:card:1"), "adm:ml:card:1"),
        (admin_ml_category_keyboard(), "adm:ml"),
    ],
)
def test_keyboard_returns_to_its_parent(markup, parent):
    assert _last_callback(markup) == parent


def test_category_keyboard_offers_labels_not_codes():
    """До этапа 2 администратор видел в кнопках «🏷 SHABASHKA»."""
    labels = [button.text for row in admin_log_keyboard(1).inline_keyboard for button in row]
    assert "SHABASHKA" not in " ".join(labels)
    assert T.BTN_CATEGORY_REJECT in labels


def test_dead_keyboards_are_gone():
    """Клавиатуры этапа 1, на которые больше никто не ссылается."""
    text = KEYBOARDS.read_text(encoding="utf-8")
    for name in (
        "admin_main_keyboard",
        "admin_back_keyboard",
        "admin_wl_keyboard",
        "admin_wlbl_keyboard",
        "admin_queue_keyboard",
    ):
        assert f"def {name}(" not in text, f"{name} осталась"
