"""Проверка категорий в «Управлении» (ТЗ этапа 2, 6.11).

Ключевое требование ТЗ: раздел показывает количество объявлений без
проставленной категории, а администратор присваивает её вручную. До этапа 2
счётчик считал только объявления, отнесённые моделью к «Прочему», а список
проверки отдавал последние пять записей без фильтра по вердикту.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.seed import _seed_tariffs
from app.db.session import SessionLocal
from app.handlers.admin.manage import _save_verdict, pending_count
from app.models.entities import ClassificationLog, TrainingSample
from app.services import admin_stats, tariffs


@pytest.fixture(autouse=True)
async def _seeded():
    await _seed_tariffs()
    await tariffs.refresh()
    yield
    await tariffs.refresh()


async def _log(category: str = "SHABASHKA", *, verdict: str | None = None) -> int:
    async with SessionLocal() as session:
        row = ClassificationLog(
            text="Нужен грузчик на склад, оплата ежедневно, график 5/2",
            predicted_category=category,
            confidence=0.42,
            admin_verdict=verdict,
        )
        session.add(row)
        await session.commit()
        return row.id


async def test_pending_counts_every_unreviewed_ad():
    """Считаем все объявления без вердикта, а не только «Прочее»."""
    await _log("SHABASHKA")
    await _log("VACANCY")
    await _log("OTHER")
    assert await pending_count() == 3


async def test_pending_ignores_reviewed_ads():
    await _log("OTHER")
    await _log("OTHER", verdict="approved")
    await _log("OTHER", verdict="fixed")
    assert await pending_count() == 1


async def test_dashboard_review_counter_matches_section():
    """Сводка панели и экран раздела считают одно и то же."""
    await _log("VACANCY")
    await _log("RENT")
    stats = await admin_stats.gather_dashboard_stats()
    assert stats["review_logs"] == await pending_count() == 2


async def test_confirm_writes_predicted_category():
    log_id = await _log("VACANCY")
    assert await _save_verdict(log_id, "VACANCY", verdict="approved", source="admin_ok")

    async with SessionLocal() as session:
        log = await session.scalar(select(ClassificationLog).where(ClassificationLog.id == log_id))
        assert log.admin_verdict == "approved"
        assert log.final_category == "VACANCY"

    assert await pending_count() == 0


async def test_manual_category_overrides_the_model():
    log_id = await _log("OTHER")
    assert await _save_verdict(log_id, "GOLD", verdict="fixed", source="admin_fix")

    async with SessionLocal() as session:
        log = await session.scalar(select(ClassificationLog).where(ClassificationLog.id == log_id))
        assert log.predicted_category == "OTHER", "предсказание модели остаётся как было"
        assert log.final_category == "GOLD"
        assert log.admin_verdict == "fixed"


async def test_verdict_feeds_the_training_set():
    """Кадр 349:404 обещает «Пример добавлен для обучения модели»."""
    log_id = await _log("RENT")
    await _save_verdict(log_id, "RENT", verdict="approved", source="admin_ok")

    async with SessionLocal() as session:
        samples = (await session.scalars(select(TrainingSample))).all()
    assert [(s.target_category, s.source) for s in samples] == [("RENT", "admin_ok")]


async def test_missing_log_is_reported():
    assert not await _save_verdict(9999, "RENT", verdict="approved", source="admin_ok")
