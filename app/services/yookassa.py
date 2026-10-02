import asyncio
import json
import logging
import uuid
import threading
from decimal import Decimal
from typing import Any

from yookassa import Configuration, Payment

from app.config import Settings

logger = logging.getLogger("yookassa")
_CONFIGURATION_LOCK = threading.RLock()


def _payment_to_dict(payment: Any) -> dict[str, Any]:
    if isinstance(payment, dict):
        return payment
    if hasattr(payment, "json"):
        raw = payment.json()
        if isinstance(raw, str):
            return json.loads(raw)
        if isinstance(raw, dict):
            return raw
    if hasattr(payment, "dict"):
        return payment.dict()
    return dict(payment)


class YooKassaService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _create_payment_sync(self, payload: dict[str, Any], idempotence_key: str) -> dict[str, Any]:
        with _CONFIGURATION_LOCK:
            Configuration.account_id = self.settings.yookassa_shop_id
            Configuration.secret_key = self.settings.yookassa_secret_key
            return _payment_to_dict(Payment.create(payload, idempotence_key))

    def _get_payment_sync(self, payment_id: str) -> dict[str, Any]:
        with _CONFIGURATION_LOCK:
            Configuration.account_id = self.settings.yookassa_shop_id
            Configuration.secret_key = self.settings.yookassa_secret_key
            return _payment_to_dict(Payment.find_one(payment_id))

    def _verify_account_sync(self) -> dict[str, Any]:
        from yookassa import Settings

        with _CONFIGURATION_LOCK:
            Configuration.account_id = self.settings.yookassa_shop_id
            Configuration.secret_key = self.settings.yookassa_secret_key
            return _payment_to_dict(Settings.get_account_settings())

    async def verify_account(self) -> dict[str, Any]:
        """Спросить у ЮKassa, чей это магазин (`account_id`) и тестовый ли он
        (`test`). Бросает исключение SDK, если shopId/ключ не подходят."""
        return await asyncio.to_thread(self._verify_account_sync)

    async def create_payment(
        self,
        amount: Decimal,
        description: str,
        metadata: dict[str, str],
        idempotence_key: str | None = None,
        return_url: str | None = None,
        save_payment_method: bool = False,
        payment_method_id: str | None = None,
        transfers: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "capture": True,
            "description": description,
            "metadata": {k: str(v) for k, v in metadata.items() if v is not None},
        }
        if transfers:
            payload["transfers"] = transfers
        if payment_method_id:
            payload["payment_method_id"] = payment_method_id
        else:
            payload["confirmation"] = {
                "type": "redirect",
                "return_url": return_url or self.settings.yookassa_return_url,
            }
        if save_payment_method:
            payload["save_payment_method"] = True
        key = idempotence_key or str(uuid.uuid4())
        return await asyncio.to_thread(self._create_payment_sync, payload, key)

    async def get_payment(self, provider_payment_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_payment_sync, provider_payment_id)

    @staticmethod
    def extract_confirmation_url(payment_data: dict[str, Any]) -> str | None:
        confirmation = payment_data.get("confirmation") or {}
        if isinstance(confirmation, dict):
            return confirmation.get("confirmation_url")
        return None

    @staticmethod
    def payment_status(payment_data: dict[str, Any]) -> str:
        return str(payment_data.get("status", ""))
