"""Разбор @username / Telegram ID для whitelist и других админ-операций."""

from __future__ import annotations

import re
from dataclasses import dataclass

from aiogram import Bot

_USERNAME_RE = re.compile(r"^@?(?P<u>[a-zA-Z][a-zA-Z0-9_]{4,31})$")


@dataclass
class ResolvedTelegramUser:
    telegram_id: int | None
    username: str | None
    pending_username: bool = False


def normalize_username(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    match = _USERNAME_RE.match(text)
    if not match:
        return None
    return match.group("u")


def parse_telegram_id(raw: str) -> int | None:
    text = (raw or "").strip()
    if text.isdigit():
        return int(text)
    if text.startswith("-") and text[1:].isdigit():
        return int(text)
    return None


async def resolve_telegram_user(bot: Bot, raw: str) -> ResolvedTelegramUser:
    text = (raw or "").strip()
    tg_id = parse_telegram_id(text)
    if tg_id is not None:
        return ResolvedTelegramUser(telegram_id=tg_id, username=None)

    username = normalize_username(text)
    if not username:
        return ResolvedTelegramUser(telegram_id=None, username=None)

    for handle in (f"@{username}", username):
        try:
            chat = await bot.get_chat(handle)
            return ResolvedTelegramUser(
                telegram_id=chat.id,
                username=(chat.username or username).lstrip("@"),
            )
        except Exception:
            continue

    return ResolvedTelegramUser(
        telegram_id=None,
        username=username,
        pending_username=True,
    )
