"""HTTP-слой вебхука разовых постов: авторизация и разбор тела (ТЗ 6.6)."""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.routes import api_router
from app.config import get_settings
from tests.conftest import now_utc, slot_for

SILENCE = timedelta(minutes=20)
PATH = "/api/hwls/oneshot-published"


@pytest_asyncio.fixture
async def client():
    api = FastAPI()
    api.include_router(api_router)
    api.state.settings = get_settings()
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
def secret() -> str:
    return get_settings().hwls_relay_secret


async def test_requires_secret(client, world):
    response = await client.post(PATH, json={"telegram_chat_id": world.chat_tg_ids[0]})
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


async def test_rejects_wrong_secret(client, world):
    response = await client.post(
        PATH,
        json={"telegram_chat_id": world.chat_tg_ids[0]},
        headers={"X-HWLS-Key": "wrong-secret"},
    )
    assert response.status_code == 401


async def test_sets_silence(client, world, secret):
    t0 = now_utc()
    response = await client.post(
        PATH,
        json={
            "telegram_chat_id": world.chat_tg_ids[0],
            "network": "hammer",
            "message_id": 42,
            "published_at": t0.isoformat(),
        },
        headers={"X-HWLS-Key": secret},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["duplicate"] is False
    assert (await slot_for(world.chat_ids[0])).locked_until == t0 + SILENCE


async def test_repeated_delivery_is_idempotent(client, world, secret):
    payload = {
        "telegram_chat_id": world.chat_tg_ids[0],
        "network": "hammer",
        "message_id": 77,
        "published_at": now_utc().isoformat(),
    }
    first = await client.post(PATH, json=payload, headers={"X-HWLS-Key": secret})
    second = await client.post(PATH, json=payload, headers={"X-HWLS-Key": secret})

    assert first.json()["silence_until"] == second.json()["silence_until"]
    assert second.json()["duplicate"] is True


async def test_unknown_chat_returns_404(client, secret, world):
    response = await client.post(
        PATH,
        json={"telegram_chat_id": -100_777_777, "message_id": 1},
        headers={"X-HWLS-Key": secret},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "chat_not_found"


async def test_chat_is_required(client, secret, world):
    response = await client.post(PATH, json={"message_id": 1}, headers={"X-HWLS-Key": secret})
    assert response.status_code == 400
    assert response.json()["error"] == "chat_required"


async def test_accepts_iso_with_z_suffix(client, world, secret):
    response = await client.post(
        PATH,
        json={
            "telegram_chat_id": world.chat_tg_ids[0],
            "message_id": 5,
            "published_at": "2026-09-04T12:00:00Z",
        },
        headers={"X-HWLS-Key": secret},
    )
    assert response.status_code == 200
    assert response.json()["silence_until"].startswith("2026-09-04T12:20:00")
