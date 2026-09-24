"""Сессия Telegram с быстрым обрывом зависшего подключения.

С этого сервера примерно каждое третье новое TCP-соединение к api.telegram.org
не устанавливается совсем: SYN пропадает, и запрос висит до общего таймаута
(120 с), после чего публикация уходит в ретрай. Повтор на новом соединении
(другой порт) с высокой вероятностью проходит сразу.

Поэтому подключение обрывается через CONNECT_TIMEOUT секунд и повторяется до
CONNECT_RETRIES раз. Повторяем только стадию подключения: запрос к Telegram в
этот момент ещё не отправлен, значит, дубля сообщения быть не может. Ошибки
после установленного соединения (чтение ответа и т. п.) не повторяем.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiohttp import ClientConnectorError, ClientError, ClientTimeout, ConnectionTimeoutError

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 6.0
CONNECT_RETRIES = 4


class ResilientSession(AiohttpSession):
    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        session = await self.create_session()
        url = self.api.api_url(token=bot.token, method=method.__api_method__)
        total = self.timeout if timeout is None else timeout
        limits = ClientTimeout(total=total, sock_connect=CONNECT_TIMEOUT)

        raw_result: Any = None
        status = 0
        for attempt in range(1, CONNECT_RETRIES + 1):
            # FormData одноразовая (файлы читаются потоком) — на каждую попытку своя.
            form = self.build_form_data(bot=bot, method=method)
            try:
                async with session.post(url, data=form, timeout=limits) as resp:
                    raw_result = await resp.text()
                    status = resp.status
                break
            except (ConnectionTimeoutError, ClientConnectorError) as exc:
                if attempt < CONNECT_RETRIES:
                    logger.info(
                        "Telegram: соединение не установилось (%s), попытка %s/%s",
                        type(exc).__name__, attempt, CONNECT_RETRIES,
                    )
                    continue
                # Текст намеренно НЕ «Request timeout error»: тот означает «запрос мог
                # уйти, ответа нет» и не ретраится. Здесь соединение не
                # установилось совсем — запрос не отправлен, повторять безопасно.
                raise TelegramNetworkError(
                    method=method,
                    message="Connect timeout: соединение с Telegram не установилось, запрос не отправлен",
                ) from exc
            except asyncio.TimeoutError as exc:
                raise TelegramNetworkError(method=method, message="Request timeout error") from exc
            except ClientError as exc:
                raise TelegramNetworkError(
                    method=method, message=f"{type(exc).__name__}: {exc}",
                ) from exc

        response = self.check_response(
            bot=bot, method=method, status_code=status, content=raw_result,
        )
        return cast(TelegramType, response.result)
