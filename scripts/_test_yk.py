import asyncio
from decimal import Decimal

from app.config import get_settings
from app.services.yookassa import YooKassaService


async def main() -> None:
    yk = YooKassaService(get_settings())
    data = await yk.create_payment(
        Decimal("1.00"),
        "HWLS test",
        {"purpose": "test", "invoice_id": "test-1"},
        idempotence_key="test-hwls-yk-1",
    )
    print("ok", data.get("id"), yk.extract_confirmation_url(data)[:40] if yk.extract_confirmation_url(data) else "")


asyncio.run(main())
