"""Определение сети чата по username (Hammer / W / package)."""

from __future__ import annotations

import pytest

from app.core.seed import CHAT_SEED
from app.services.chat_network import infer_chat_network


@pytest.mark.parametrize(
    ("username", "expected"),
    [
        ("Work77Hammer", "hammer"),
        ("HalturamskHammer", "hammer"),
        ("GruzchikiMskHammer", "hammer"),
        ("HalturaSpbHammer", "hammer"),
        ("Work39Hammer", "hammer"),
        ("GruzekbHammer", "hammer"),
        ("Gruzyarhammer", "hammer"),
        ("GruzChelHammer", "hammer"),
        # Финальная «w» при корне «rabot» / «vacanc» — сеть W.
        ("Podrabotkaspbw", "w"),
        ("Podrabotkanskw", "w"),
        ("Podrabotkannw", "w"),
        ("Podrabotkamskw", "w"),
        ("Podrabotkasmrw", "w"),
        ("Vacanciesmskw", "w"),
        ("Rabota_Spb_W", "w"),
        # Сеть не читается по имени — заказчик отнёс группу к W (15.09.2026).
        ("VacancieKzn", "w"),
    ],
)
def test_network_inferred_from_username(username: str, expected: str):
    assert infer_chat_network(username) == expected


def test_every_seeded_chat_resolves_to_a_known_network():
    """Ни одна группа из перечня не должна уходить в партнёрский `package`."""
    unknown = [row[2] for row in CHAT_SEED if infer_chat_network(row[2]) == "package"]
    assert unknown == []
