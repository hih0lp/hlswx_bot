"""Раздел «Города и чаты» по кадрам макета (ТЗ этапа 2, 6.5).

Кадры 368:406 → 368:429 → 368:506; добавление города 368:637 → 368:644 →
368:664, чата — 368:550 → 368:592 → 368:575.

Главное здесь — второй шаг добавления чата. До этой правки чат заводился
только по @username, а `telegram_chat_id` оставался пустым, и публикация в
него падала с `chat_not_configured` (`app/services/publish.py`).
"""

from __future__ import annotations

import pytest

from app.core import admin_texts as T
from app.db.session import SessionLocal
from app.handlers.admin.places import _city_key_for, _resolve_target, _slug
from app.models.entities import City


class _FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeBot:
    """Telegram отвечает на get_chat — или падает, как на неизвестной группе."""

    def __init__(self, chat_id: int | None = None, error: str | None = None) -> None:
        self._chat_id = chat_id
        self._error = error

    async def get_chat(self, target: str):
        if self._error:
            raise RuntimeError(self._error)
        return _FakeChat(self._chat_id)


def test_slug_transliterates_the_label():
    assert _slug("Казань") == "kazan"
    assert _slug("Нижний Новгород") == "nizhniy_novgorod"
    assert _slug("Ростов-на-Дону") == "rostov_na_donu"


def test_slug_never_comes_out_empty():
    assert _slug("!!!") == "city"


async def test_city_key_is_generated_and_unique():
    async with SessionLocal() as session:
        first = await _city_key_for(session, "Казань")
        session.add(City(key=first, label="Казань"))
        await session.commit()

        second = await _city_key_for(session, "Казань")

    assert first == "kazan"
    assert second == "kazan_2"


async def test_numeric_id_is_taken_as_is():
    username, chat_id, error = await _resolve_target("-1003917161713")
    assert username is None
    assert chat_id == -1003917161713
    assert error == ""


async def test_username_is_resolved_into_a_chat_id(monkeypatch):
    """Ради этого второй шаг и появился: без chat_id публикация не пройдёт."""
    monkeypatch.setattr("app.bot.runtime.get_bot", lambda: _FakeBot(chat_id=-100_777))

    username, chat_id, error = await _resolve_target("@VacancieKzn")

    assert username == "VacancieKzn"
    assert chat_id == -100_777
    assert error == ""


async def test_unresolvable_username_is_kept_with_an_explanation(monkeypatch):
    """Бот ещё не в группе — чат заводим, но честно предупреждаем."""
    monkeypatch.setattr(
        "app.bot.runtime.get_bot", lambda: _FakeBot(error="chat not found"),
    )

    username, chat_id, error = await _resolve_target("@VacancieKzn")

    assert username == "VacancieKzn"
    assert chat_id is None
    assert "chat not found" in error


@pytest.mark.parametrize("raw", ["", "не username", "@ab", "12", "@имя_группы"])
async def test_garbage_is_rejected(raw: str):
    _username, _chat_id, error = await _resolve_target(raw)
    assert error == "bad"


def test_key_is_not_asked_from_the_admin():
    """Кадр 368:637 спрашивает только название — служебный ключ ушёл из текстов."""
    assert "ключ" not in T.CITY_ADD_ASK.lower()
    assert "Введите название города" in T.CITY_ADD_ASK
    assert "Ключ" not in T.CITY_CARD


def test_chat_is_added_in_two_steps():
    assert "Введите название чата" in T.CHAT_ADD_ASK
    assert "Введите username или ID чата" in T.CHAT_ADD_TARGET_ASK
    assert "доступен для публикаций" in T.CHAT_ADDED
