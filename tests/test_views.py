"""Тесты белого списка витрин и конверта ответа (`app/views.py`).

Живая база не нужна: обращения к PostgreSQL подменены (`tests/conftest.py`).
Здесь проверяется то, что проверяемо без данных: состав белого списка, разбор
параметров, текст SQL (имя витрины и имена колонок сортировки приходят только из
белого списка), форма конверта и поведение на пустой витрине. Проверка тех же
витрин на настоящих данных — приёмка в docs/RUNBOOK.md, раздел «Приёмка сервера».
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app import views
from tests.conftest import FakeViewsDB

# Внутренняя кухня ETL (docs/SCHEMA.md §3) и промежуточные вьюхи: наружу не отдаём.
FORBIDDEN = (
    "load_batches",
    "dq_issues",
    "role_aliases",
    "task_sequence",
    "task_role_estimates",
    "task_role_spent",
    "pi_periods",
    "team_history",
    "engineer_skills",
    "skills",
    "v_role_supply_hh",
    "v_backlog_demand",
)


def test_whitelist_is_consistent() -> None:
    """Каждая витрина описана полностью: имя, экран, порядок внутри белого списка."""
    names = [source.name for source in views.SOURCES]

    assert len(names) == len(set(names))  # имя — ключ URL, дубликат был бы двусмысленным
    assert len(views.SOURCES) == len(views.BY_NAME)
    for source in views.SOURCES:
        assert source.screen, f"{source.name}: не указан экран"
        assert source.order, f"{source.name}: пустой порядок по умолчанию"
        for column in source.order.split(","):
            assert column.lstrip("-+") in source.orderable, f"{source.name}: {column} вне списка"
        if source.run_column is not None:
            # Ось контракта одна — run_id (docs/SCHEMA.md §2): любое другое имя
            # означало бы, что витрина фильтруется не по прогону.
            assert source.run_column == "run_id", f"{source.name}: неожиданная ось прогона"
    # Список прогонов для выбора run_id сам по прогону не фильтруется.
    assert views.BY_NAME["plan_runs"].run_column is None


def test_whitelist_keeps_etl_internals_out() -> None:
    """Наружу идёт справочный слой и контракт прогона, а не кухня ETL."""
    names = set(views.BY_NAME)

    assert not names & set(FORBIDDEN)
    assert "v_task_board" in names and "plan_task_schedule" in names
    assert "plan_runs" in names  # список прогонов: фронту нужно чем-то выбирать run_id


def test_registration_rejects_a_column_outside_the_whitelist() -> None:
    """Опечатка в порядке падает при импорте, а не 500-й в бою."""
    with pytest.raises(ValueError, match="вне белого списка"):
        views._source("v_x", screen="Тест", order="nope", orderable=("a", "b"))


def test_unknown_view_is_not_put_into_sql(fake_db: FakeViewsDB) -> None:
    """Имя витрины — не текст SQL: попытка подстановки не доходит до базы."""
    with pytest.raises(views.UnknownView) as excinfo:
        views.fetch("v_task_board;DROP TABLE tasks")

    payload = excinfo.value.payload()
    assert payload["error"] == "not_found"
    assert "v_task_board;DROP TABLE tasks" in payload["message"]
    assert payload["known"] == [source.name for source in views.SOURCES]
    assert fake_db.calls == []  # ни одного запроса: даже SELECT не собирался


def test_order_accepts_only_whitelisted_columns(fake_db: FakeViewsDB) -> None:
    """`order` — единственное место, где имя колонки попадает в текст SQL."""
    with pytest.raises(views.BadRequest) as excinfo:
        views.fetch("v_role_deficit", order="1;--")

    payload = excinfo.value.payload()
    assert payload["error"] == "bad_request"
    assert payload["param"] == "order"
    assert payload["known"] == list(views.BY_NAME["v_role_deficit"].orderable)
    assert fake_db.calls == []

    envelope = views.fetch("v_role_deficit", order="team_id,-gap_hh")
    sql, params = fake_db.select()
    assert "ORDER BY team_id ASC NULLS LAST, gap_hh DESC NULLS LAST" in sql
    assert envelope["order"] == ["team_id", "-gap_hh"]
    # Витрина не привязана к прогону: в параметрах только limit и offset.
    assert params == [views.LIMIT_DEFAULT, 0]


def test_order_and_limit_are_parameters_not_text(fake_db: FakeViewsDB) -> None:
    """`run_id`, `limit`, `offset` — параметры `%s::int`, а не склейка строк."""
    views.fetch("alerts", limit=10, offset=5)

    sql, params = fake_db.select()
    assert "LIMIT %s::int OFFSET %s::int" in sql
    assert params == [2, 10, 5]


def test_nulls_are_last_in_both_directions(fake_db: FakeViewsDB) -> None:
    """Переносы без спринта не должны всплывать наверх: иначе первым видно «не запланировано»."""
    views.fetch("plan_task_schedule", order="-start_sprint")

    sql, _ = fake_db.select()
    assert "start_sprint DESC NULLS LAST" in sql

def test_default_run_is_the_last_ok_run(fake_db: FakeViewsDB) -> None:
    """`run_id` по умолчанию — последний удачный прогон, как в KPI и приёмке."""
    envelope = views.fetch("alerts")

    sql, params = fake_db.select()
    assert "WHERE run_id = %s::int" in sql
    assert params[0] == 2
    assert envelope["run_id"] == 2
    assert envelope["run_default"] is True
    assert "status = 'ok'" in fake_db.sql[0]


def test_explicit_run_id_is_not_overridden(fake_db: FakeViewsDB) -> None:
    """Явный `run_id` из UI важнее «текущего»: пересчёт в середине квартала (ADR-014)."""
    envelope = views.fetch("alerts", run_id=7)

    sql, params = fake_db.select()
    assert params[0] == 7
    assert envelope["run_id"] == 7
    assert envelope["run_default"] is False
    assert "query_one" not in fake_db.kinds  # прогон по умолчанию не искали


def test_run_id_on_a_reference_view_is_a_400(fake_db: FakeViewsDB) -> None:
    """`v_task_board` не привязана к прогону: молча игнорировать параметр нельзя."""
    with pytest.raises(views.BadRequest) as excinfo:
        views.fetch("v_task_board", run_id=1)

    payload = excinfo.value.payload()
    assert payload["param"] == "run_id"
    assert "plan_task_schedule" in payload["known"]
    assert fake_db.calls == []


def test_reference_views_are_not_filtered_by_run(fake_db: FakeViewsDB) -> None:
    """Справочный слой один на все прогоны: фильтра по run_id в SQL нет."""
    envelope = views.fetch("v_orbit_map")

    sql, _ = fake_db.select()
    assert "WHERE" not in sql
    assert envelope["run_id"] is None
    assert envelope["run_column"] is None
    assert envelope["run_default"] is False


def test_no_ok_run_means_no_filter(fake_db: FakeViewsDB) -> None:
    """Удачных прогонов ещё нет: отдаём витрину как есть, а не выдумываем run_id."""
    fake_db.run_id = None

    envelope = views.fetch("alerts")

    assert envelope["run_id"] is None
    assert "WHERE" not in fake_db.select()[0]


def test_limit_and_offset_are_checked() -> None:
    """Потолок `limit` — защита от «вытащить всю базу одним запросом»."""
    assert views.parse_limit(None) == views.LIMIT_DEFAULT
    assert views.parse_limit("10") == 10
    assert views.parse_offset(None) == 0

    for raw, param in (("0", "limit"), ("5001", "limit"), ("-1", "offset"), ("abc", "limit")):
        with pytest.raises(views.BadRequest) as excinfo:
            if param == "limit":
                views.parse_limit(raw)
            else:
                views.parse_offset(raw)
        assert excinfo.value.payload()["param"] == param

    with pytest.raises(views.BadRequest):
        views.parse_run_id("2;DROP")
def test_default_order_keeps_its_direction(fake_db: FakeViewsDB) -> None:
    """«-» в порядке по умолчанию — это DESC.

    Потеря знака при регистрации переворачивала бы список прогонов (фронт выбирал бы
    самый старый) и срез дефицита по компании — и это не падало бы ни на одном тесте.
    """
    assert views.BY_NAME["plan_runs"].order == "-run_id"
    assert views.BY_NAME["v_role_coverage_org"].order == "-gap_hh,role_name"
    assert views.BY_NAME["v_task_board"].order == "priority_rung,task_id"

    envelope = views.fetch("plan_runs")
    assert envelope["order"] == ["-run_id"]
    assert "ORDER BY run_id DESC NULLS LAST" in fake_db.select()[0]


def test_short_page_does_not_recount(fake_db: FakeViewsDB) -> None:
    """Страница не полная и это её начало — COUNT(*) не нужен: больше строк нет."""
    fake_db.rows = [{"alert_id": 1}, {"alert_id": 2}]

    envelope = views.fetch("alerts", limit=10)

    assert envelope["count"] == 2
    assert envelope["returned"] == 2
    assert envelope["truncated"] is False
    assert envelope["has_more"] is False
    assert "scalar" not in fake_db.kinds


def test_full_page_counts_the_whole_view(fake_db: FakeViewsDB) -> None:
    """Полная страница — обрезание: без отдельного COUNT(*) фронт не покажет «45 из 300»."""
    fake_db.rows = [{"alert_id": 1}, {"alert_id": 2}]
    fake_db.total = 23

    envelope = views.fetch("alerts", limit=2)

    assert envelope["count"] == 23
    assert envelope["returned"] == 2
    assert envelope["truncated"] is True
    assert envelope["has_more"] is True
    count_sql, count_params = fake_db.last("scalar")
    assert count_sql.startswith("SELECT COUNT(*) FROM alerts")
    assert count_params == [2]  # фильтр тот же, что у выборки строк


def test_offset_recounts_even_on_a_short_page(fake_db: FakeViewsDB) -> None:
    """`offset > 0` — это хвост списка: `count` из длины страницы был бы враньём."""
    fake_db.rows = [{"alert_id": 3}]
    fake_db.total = 23

    envelope = views.fetch("alerts", limit=10, offset=22)

    assert envelope["count"] == 23
    assert envelope["returned"] == 1
    assert envelope["has_more"] is False


def test_empty_view_is_a_normal_answer(fake_db: FakeViewsDB) -> None:
    """«В этом прогоне переносов нет» — это 200 с `count: 0`, а не 404."""
    fake_db.rows = []

    envelope = views.fetch("alerts", run_id=999)

    assert envelope["count"] == 0
    assert envelope["returned"] == 0
    assert envelope["items"] == []
    assert envelope["has_more"] is False
    # Форму ответа фронт должен знать и без строк: колонки берём из схемы.
    assert envelope["columns"] == ["alert_id", "run_id", "sprint_no"]
    assert "information_schema" in fake_db.sql[-1]


def test_envelope_answers_the_questions_of_every_screen(fake_db: FakeViewsDB) -> None:
    """Конверт: что показано, на каком прогоне, на какой момент и какой формы строки."""
    fake_db.rows = [{"alert_id": 1, "run_id": 2, "level": "red"}]

    envelope = views.fetch("alerts")

    assert set(envelope) == {
        "view", "kind", "screen", "note", "run_id", "run_column", "run_default", "as_of",
        "order", "limit", "offset", "count", "returned", "truncated", "has_more", "columns",
        "items",
    }
    assert envelope["view"] == "alerts"
    assert envelope["kind"] == "table"
    assert envelope["screen"] == "Алерты"
    assert envelope["columns"] == ["alert_id", "run_id", "level"]
    assert envelope["items"] == [{"alert_id": 1, "run_id": 2, "level": "red"}]
    # `as_of` — ISO-8601 с местным смещением: шапка UI обязана его показывать,
    # иначе цифры «плывут» между экранами незаметно.
    assert datetime.fromisoformat(envelope["as_of"]).tzinfo is not None


def test_catalog_documents_every_view() -> None:
    """`GET /api/views` — контракт витрин, а не переписка: фронт читает его сам."""
    payload = views.catalog()

    assert payload["count"] == len(views.SOURCES)
    assert payload["limit_default"] == views.LIMIT_DEFAULT
    assert payload["limit_max"] == views.LIMIT_MAX
    assert [item["name"] for item in payload["items"]] == [s.name for s in views.SOURCES]
    for item in payload["items"]:
        assert item["screen"] and item["note"]
        assert item["order"] and item["orderable"]
        assert {col.lstrip("-+") for col in item["order"]} <= set(item["orderable"])

