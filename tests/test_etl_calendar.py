"""Календарь PI: ETL обязан закрыть квартал ровно — без дыр и нахлёстов.

Проверяем guard из `etl.load.build_sprints`. Ошибка в `PI_END`/`SPRINT_COUNT`
не должна доезжать до витрин: фонд часов считается из длин спринтов, поэтому
«недостающий» день тихо превратился бы в недостающие ЧЧ и в другую приёмку
(ADR-007, ADR-017).

Тест читает `etl/config.py` как есть — то есть проверяет и сам конфиг, а не
только функцию.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from etl import load


def test_build_sprints_covers_the_quarter_exactly() -> None:
    """Q3-2026: 01.07..30.09 = 92 дня = 6 × 14 + 8, последний спринт короткий."""
    pi, sprints = load.build_sprints()

    assert pi == ("PI-2026-Q3", date(2026, 7, 1), date(2026, 9, 30), 7, 14, 80)
    assert [(no, start, end) for _pi, no, start, end in sprints] == [
        (1, date(2026, 7, 1), date(2026, 7, 14)),
        (2, date(2026, 7, 15), date(2026, 7, 28)),
        (3, date(2026, 7, 29), date(2026, 8, 11)),
        (4, date(2026, 8, 12), date(2026, 8, 25)),
        (5, date(2026, 8, 26), date(2026, 9, 8)),
        (6, date(2026, 9, 9), date(2026, 9, 22)),
        (7, date(2026, 9, 23), date(2026, 9, 30)),
    ]
    assert sum((end - start).days + 1 for _pi, _no, start, end in sprints) == 92
    # Ни дыр, ни нахлёстов: следующий спринт начинается на день позже конца
    # предыдущего. Именно из этой непрерывности и получается фонд 6.5714.
    assert all(
        sprints[i + 1][2] == sprints[i][3] + timedelta(days=1) for i in range(len(sprints) - 1)
    )


def test_last_sprint_is_the_short_one() -> None:
    """Коротким может быть только последний спринт — остальные по 14 дней."""
    _pi, sprints = load.build_sprints()

    lengths = [(end - start).days + 1 for _pi, _no, start, end in sprints]
    assert lengths[:-1] == [14] * (len(lengths) - 1)
    assert lengths[-1] == 8


def test_sprint_that_does_not_fit_the_quarter_raises(monkeypatch) -> None:
    """8-й спринт начинается уже после конца квартала — падаем, а не режем молча."""
    monkeypatch.setattr(load.C, "SPRINT_COUNT", 8)

    with pytest.raises(ValueError, match="не влезает в границы"):
        load.build_sprints()


def test_quarter_not_fully_covered_raises(monkeypatch) -> None:
    """6 спринтов × 14 = 84 дня из 92: 8 дней квартала остались бы без фонда."""
    monkeypatch.setattr(load.C, "SPRINT_COUNT", 6)

    with pytest.raises(ValueError, match="дыра или нахлёст"):
        load.build_sprints()
