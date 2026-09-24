"""Круговая пагинация (ТЗ этапа 2, п. 6.8).

Проверяем именно цикличность: заказчик просил «круговой зацикленный вариант
с пагинацией», то есть с последней страницы «вперёд» ведёт на первую.
Подписи кнопок — из кадров макета (`349:1025`, `338:725`, `340:892`).
"""

from __future__ import annotations

from app.keyboards.pagination import NOOP_CB, page_count, page_slice, pager_row

ITEMS = list(range(11))  # 11 элементов по 5 на странице — три страницы


def test_page_count():
    assert page_count(0, 5) == 1
    assert page_count(5, 5) == 1
    assert page_count(6, 5) == 2
    assert page_count(11, 5) == 3


def test_page_slice_normal():
    assert page_slice(ITEMS, 0, 5) == ([0, 1, 2, 3, 4], 0, 3)
    assert page_slice(ITEMS, 1, 5) == ([5, 6, 7, 8, 9], 1, 3)
    assert page_slice(ITEMS, 2, 5) == ([10], 2, 3)


def test_page_slice_wraps_both_ways():
    # За последней страницей идёт первая, перед первой — последняя.
    assert page_slice(ITEMS, 3, 5) == page_slice(ITEMS, 0, 5)
    assert page_slice(ITEMS, -1, 5) == page_slice(ITEMS, 2, 5)


def test_page_slice_short_and_empty_lists():
    assert page_slice([1, 2], 0, 5) == ([1, 2], 0, 1)
    assert page_slice([1, 2], 7, 5) == ([1, 2], 0, 1)
    assert page_slice([], 0, 5) == ([], 0, 1)


def test_pager_row_hidden_on_single_page():
    assert pager_row("p:", 0, 1) == []


def test_pager_row_targets_wrap_around():
    prev_btn, counter, next_btn = pager_row("p:", 0, 3)
    assert prev_btn.callback_data == "p:2"   # назад с первой — на последнюю
    assert next_btn.callback_data == "p:1"
    assert counter.text == "1 / 3"
    assert counter.callback_data == NOOP_CB

    prev_btn, _, next_btn = pager_row("p:", 2, 3)
    assert prev_btn.callback_data == "p:1"
    assert next_btn.callback_data == "p:0"   # вперёд с последней — на первую
