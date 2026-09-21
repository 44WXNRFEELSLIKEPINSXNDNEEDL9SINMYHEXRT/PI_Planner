"""Общие фикстуры тестов.

`fake_db` подменяет обращения к PostgreSQL, которые делает `app/views.py`:
тесты витрин идут без базы, а текст SQL проверяется по записи вызовов, а не по
факту выполнения. На живой базе витрины проверяются отдельно — приёмка в
docs/RUNBOOK.md, раздел «Приёмка сервера».
"""
from __future__ import annotations

from typing import Any

import pytest

from app import views


class FakeViewsDB:
    """Записывает SQL и параметры вместо похода в PostgreSQL."""

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        total: int | None = None,
        run_id: int | None = 2,
        columns: list[str] | None = None,
    ) -> None:
        self.rows = list(rows or [])
        self.total = total
        self.run_id = run_id
        self.columns = columns or ["alert_id", "run_id", "sprint_no"]
        self.calls: list[tuple[str, str, list[Any]]] = []
        self.error: Exception | None = None

    # ------------------------------------------------------------- подмена db
    def query_dicts(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.calls.append(("query_dicts", self._norm(sql), list(params or [])))
        self._boom()
        if "information_schema" in sql:
            return [{"column_name": name} for name in self.columns]
        return [dict(row) for row in self.rows]

    def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        self.calls.append(("query_one", self._norm(sql), list(params or [])))
        self._boom()
        return {"run_id": self.run_id}

    def scalar(self, sql: str, params: Any = None) -> Any:
        self.calls.append(("scalar", self._norm(sql), list(params or [])))
        self._boom()
        return self.total if self.total is not None else len(self.rows)

    # ------------------------------------------------------------- для тестов
    def _boom(self) -> None:
        if self.error is not None:
            raise self.error

    @staticmethod
    def _norm(sql: str) -> str:
        """SQL одной строкой: в тестах его читают глазами и сравнивают по подстроке."""
        return " ".join(sql.split())

    @property
    def sql(self) -> list[str]:
        return [call[1] for call in self.calls]

    @property
    def kinds(self) -> list[str]:
        return [call[0] for call in self.calls]

    def last(self, kind: str) -> tuple[str, list[Any]]:
        """Последний вызов указанного вида: (SQL, параметры)."""
        for call_kind, sql, params in reversed(self.calls):
            if call_kind == kind:
                return sql, params
        raise AssertionError(f"нет вызовов {kind}: {self.calls}")

    def select(self) -> tuple[str, list[Any]]:
        """Последний запрос к самой витрине.

        Отдельно от `last`: после пустой страницы `fetch` добирает колонки из
        `information_schema`, и «последний запрос» — уже не он.
        """
        for call_kind, sql, params in reversed(self.calls):
            if call_kind == "query_dicts" and "information_schema" not in sql:
                return sql, params
        raise AssertionError(f"нет выборки из витрины: {self.calls}")


@pytest.fixture()
def fake_db(monkeypatch) -> FakeViewsDB:
    """Фальшивая база для `app.views` — тесты идут без PostgreSQL."""
    fake = FakeViewsDB()
    for name in ("query_dicts", "query_one", "scalar"):
        monkeypatch.setattr(views.db, name, getattr(fake, name))
    return fake
