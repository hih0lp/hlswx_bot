"""Оборванный ответ при публикации: пост вышел, а мы об этом не узнали.

Разбор bug1/bug2 от 20.09.2026. Релей отвечал «HTTP Client says - Request
timeout error», объявления при этом лежали в группах, а бот трижды повторял
отправку и в конце писал администратору «публикация отменена».
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.services.hammer_relay import RelayResult
from app.services.publish import _bot_missing_in_chat, _relay_infra_failure, process_publish_queue
from app.services.publish_errors import (
    STATUS_UNCONFIRMED,
    human_reason,
    is_permanent,
    is_uncertain,
)
from tests.conftest import all_jobs, jobs_for_chat, now_utc

# Так выглядит TelegramNetworkError у aiogram — и у нас, и на стороне релея.
AIOGRAM_TIMEOUT = "HTTP Client says - Request timeout error"


@pytest.mark.parametrize(
    "error",
    [
        AIOGRAM_TIMEOUT,
        "relay_timeout: ReadTimeout",
        "Server disconnected without sending a response",
        "http_504",
    ],
)
def test_lost_response_is_uncertain_not_permanent(error):
    """Ответ не дошёл — это не провал: повторять нельзя, отменять нечего."""
    assert is_uncertain(error)
    assert not is_permanent(error)
    assert not _relay_infra_failure(error), "досылка своим ботом положила бы дубль"


@pytest.mark.parametrize(
    "error",
    [
        "Connection reset by peer",
        "relay_unreachable: ConnectError",
    ],
)
def test_broken_connection_stays_retryable(error):
    """Соединение не поднялось — запроса никто не видел, обычный повтор."""
    assert not is_uncertain(error)


def test_chat_not_found_blames_the_bot_not_the_group():
    """ID групп живые (сверка с Telegram 20.09.2026) — виноват не «удалённый» чат."""
    assert is_permanent("Bad Request: chat not found")
    assert _bot_missing_in_chat("Bad Request: chat not found")
    assert "не добавлен в группу" in human_reason("Bad Request: chat not found")
    assert "удалена" not in human_reason("Bad Request: chat not found").split("(")[0]


def test_uncertain_reason_tells_admin_where_to_look():
    assert "скорее всего" in human_reason(AIOGRAM_TIMEOUT)


async def test_lost_response_is_not_retried(bot, world, enqueue, monkeypatch):
    """Три повтора клали в группу три одинаковых поста. Теперь отправка одна."""
    notices: list[tuple[int, int, str]] = []

    async def fake_notify(publication_id, chat_id, error):
        notices.append((publication_id, chat_id, error))

    monkeypatch.setattr(
        "app.services.notifications.notify_admins_publish_failed", fake_notify, raising=True,
    )
    bot.fail_chats[world.chat_tg_ids[0]] = AIOGRAM_TIMEOUT

    await enqueue("sub1")
    t0 = now_utc()
    await process_publish_queue(bot, now=t0)

    chat_jobs = await jobs_for_chat(world.chat_ids[0])
    assert chat_jobs[0].status == STATUS_UNCONFIRMED
    assert chat_jobs[0].attempts == 1, "повтор задвоил бы объявление в группе"
    assert notices, "администратор должен узнать, что исход неизвестен"

    # Следующие проходы воркера тоже не должны ничего досылать.
    await process_publish_queue(bot, now=t0 + timedelta(minutes=5))
    sent_to_chat = [m for m in bot.sent if m["chat_id"] == world.chat_tg_ids[0]]
    assert sent_to_chat == [], "в группу не ушло ни одной удачной отправки"

    chat_jobs = await jobs_for_chat(world.chat_ids[0])
    assert chat_jobs[0].attempts == 1


async def test_lost_response_does_not_fail_the_publication(bot, world, enqueue, monkeypatch):
    """Пользователю пост показывается опубликованным, а не проваленным."""

    async def fake_notify(publication_id, chat_id, error):
        return None

    monkeypatch.setattr(
        "app.services.notifications.notify_admins_publish_failed", fake_notify, raising=True,
    )
    bot.fail_chats[world.chat_tg_ids[0]] = AIOGRAM_TIMEOUT

    await enqueue("sub1")
    await process_publish_queue(bot, now=now_utc())

    notes = bot.user_notifications(world.user_tg_ids["sub1"])
    assert notes, "уведомление должно уйти"
    assert "не удалось" not in notes[-1].lower()
    assert "опубликован" in notes[-1].lower()

    statuses = {j.status for j in await all_jobs()}
    assert statuses == {"published", STATUS_UNCONFIRMED}


async def test_lost_response_holds_the_chat_slot(bot, world, enqueue, monkeypatch):
    """Группа занята: пост, вероятно, вышел, и следующий должен отстоять паузу."""

    async def fake_notify(publication_id, chat_id, error):
        return None

    monkeypatch.setattr(
        "app.services.notifications.notify_admins_publish_failed", fake_notify, raising=True,
    )
    bot.fail_chats[world.chat_tg_ids[0]] = AIOGRAM_TIMEOUT

    t0 = now_utc()
    await enqueue("sub1")
    await process_publish_queue(bot, now=t0)

    bot.fail_chats.clear()
    await enqueue("sub2")
    await process_publish_queue(bot, now=t0 + timedelta(minutes=1))

    chat_jobs = await jobs_for_chat(world.chat_ids[0])
    waiting = [j for j in chat_jobs if j.status == "queued"]
    assert waiting, "второй пост не должен уйти в группу сразу за неподтверждённым"


async def test_photo_post_falls_back_to_relay_when_our_bot_is_absent(
    bot, world, enqueue, monkeypatch,
):
    """Посты с фото уходят нашим ботом, а он в группах не состоит.

    Правка от 17.09.2026 отправила объявления с пользовательской картинкой в
    обход релея. Своего бота в боевых группах нет, так что такие посты не
    доходили никуда: Telegram отвечал «chat not found». Текст важнее картинки —
    досылаем релеем, фото при этом теряется (чужой бот наш file_id не откроет).
    """
    calls: list[dict] = []

    async def fake_relay(**kwargs):
        calls.append(kwargs)
        return RelayResult(ok=True, message_id=777, network="hammer")

    monkeypatch.setattr("app.services.hammer_relay.relay_publish", fake_relay, raising=True)
    for tg_id in world.chat_tg_ids:
        bot.fail_chats[tg_id] = "Bad Request: chat not found"

    await enqueue("sub1", photo_url="AgACAgIAAxkBAAI-file-id")
    await process_publish_queue(bot, now=now_utc())

    assert len(calls) == len(world.chat_tg_ids), "все группы должны уйти релеем"
    assert all(call["photo_url"] is None for call in calls)
    assert {j.status for j in await all_jobs()} == {"published"}


async def test_relay_read_timeout_differs_from_unreachable(monkeypatch):
    """Клиент релея различает «не достучались» и «ответ потеряли»."""
    from app.services import hammer_relay

    monkeypatch.setattr(
        hammer_relay,
        "get_settings",
        lambda: SimpleNamespace(
            hammer_relay_enabled=True,
            hwls_relay_secret="test-secret",
            hammer_relay_url="http://relay",
        ),
        raising=True,
    )

    class _Client:
        def __init__(self, exc):
            self._exc = exc

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise self._exc

    async def call(exc):
        monkeypatch.setattr(
            hammer_relay.httpx, "AsyncClient", lambda **kw: _Client(exc), raising=True,
        )
        return await hammer_relay.relay_publish(
            text="текст",
            contact="@user",
            telegram_chat_id=-100_000_001,
            telegram_username="group_a",
            network="hammer",
        )

    request = httpx.Request("POST", "http://relay/api/hwls/publish")

    lost = await call(httpx.ReadTimeout("timed out", request=request))
    assert is_uncertain(lost.error), lost.error

    refused = await call(httpx.ConnectError("refused", request=request))
    assert not is_uncertain(refused.error), refused.error
    assert _relay_infra_failure(refused.error), "к релею не достучались — досылаем сами"
