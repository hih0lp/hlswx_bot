"""HTTP-клиент: HLSWX → Hammer/W Pay для публикации в группы."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import get_settings

logger = logging.getLogger("hammer_relay")


@dataclass
class RelayResult:
    ok: bool
    message_id: int | None = None
    error: str = ""
    network: str = ""


async def relay_publish(
    *,
    text: str,
    contact: str,
    telegram_chat_id: int,
    telegram_username: str,
    network: str,
    city_key: str = "",
    pin: bool = False,
    photo_url: str | None = None,
    premium: bool = False,
    apply_chat_lock: bool = False,
) -> RelayResult:
    settings = get_settings()
    if not settings.hammer_relay_enabled:
        return RelayResult(ok=False, error="relay_disabled", network=network)
    secret = (settings.hwls_relay_secret or "").strip()
    if not secret:
        return RelayResult(ok=False, error="relay_secret_not_configured", network=network)

    base = settings.hammer_relay_url.rstrip("/")
    url = f"{base}/api/hwls/publish"
    payload = {
        "text": text,
        "contact": contact,
        "telegram_chat_id": telegram_chat_id,
        "telegram_username": telegram_username,
        "network": network,
        "city_key": city_key,
        "pin": pin,
        "photo_url": photo_url or None,
        "premium": premium,
        "apply_chat_lock": apply_chat_lock,
    }
    headers = {"X-HWLS-Key": secret, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # Соединение не поднялось — релей запроса не видел, пост точно не вышел.
        logger.warning("Hammer relay unreachable chat_id=%s network=%s: %s", telegram_chat_id, network, exc)
        return RelayResult(ok=False, error=f"relay_unreachable: {exc}", network=network)
    except httpx.RequestError as exc:
        # Запрос ушёл, ответ оборвался. Релей мог успеть опубликовать, поэтому
        # ни повторять, ни досылать своим ботом нельзя — будет дубль в группе.
        logger.warning(
            "Hammer relay response lost chat_id=%s network=%s: %s",
            telegram_chat_id,
            network,
            type(exc).__name__,
        )
        return RelayResult(
            ok=False,
            error=f"relay_timeout: {type(exc).__name__} {exc}".strip(),
            network=network,
        )

    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code >= 400 or not data.get("ok"):
        err = data.get("error") or resp.text[:200] or f"http_{resp.status_code}"
        logger.warning(
            "Hammer relay rejected chat_id=%s network=%s: %s",
            telegram_chat_id,
            network,
            err,
        )
        return RelayResult(ok=False, error=str(err), network=network)

    return RelayResult(
        ok=True,
        message_id=data.get("message_id"),
        network=data.get("network", network),
    )


async def _relay_call(path: str, payload: dict) -> tuple[bool, str]:
    """Общий вызов вспомогательных ручек релея (правка/закрытие поста)."""
    settings = get_settings()
    if not settings.hammer_relay_edit_enabled:
        # Ручки на стороне Hammer/W ещё не реализованы — см. docs/integration_hammer.md
        return False, "relay_edit_not_available"
    secret = (settings.hwls_relay_secret or "").strip()
    if not secret:
        return False, "relay_secret_not_configured"

    url = f"{settings.hammer_relay_url.rstrip('/')}{path}"
    headers = {"X-HWLS-Key": secret, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.RequestError as exc:
        return False, f"relay_unreachable: {exc}"

    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code >= 400 or not data.get("ok"):
        return False, str(data.get("error") or resp.text[:200] or f"http_{resp.status_code}")
    return True, ""


async def relay_edit(
    *,
    telegram_chat_id: int,
    message_id: int,
    text: str,
    contact: str,
    network: str,
) -> tuple[bool, str]:
    """Правка уже опубликованного поста на стороне Hammer/W (ТЗ 6.8)."""
    return await _relay_call(
        "/api/hwls/edit",
        {
            "telegram_chat_id": telegram_chat_id,
            "message_id": message_id,
            "text": text,
            "contact": contact,
            "network": network,
        },
    )


async def relay_close(
    *,
    telegram_chat_id: int,
    message_id: int,
    text: str,
    contact: str,
    network: str,
) -> tuple[bool, str]:
    """Зачеркнуть опубликованный пост на стороне Hammer/W (ТЗ 6.8)."""
    return await _relay_call(
        "/api/hwls/close",
        {
            "telegram_chat_id": telegram_chat_id,
            "message_id": message_id,
            "text": text,
            "contact": contact,
            "network": network,
        },
    )
