"""DB-телеметрия измеряет клиентские операции без SQL в labels."""
from __future__ import annotations

from app import db


class FakeCursor:
    rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _sql, _params=()):
        return None

    def fetchall(self):
        return [{"value": 1}]


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return FakeCursor()


def test_query_records_connection_operation_rows_and_latency(monkeypatch) -> None:
    db.reset_metrics()
    monkeypatch.setattr(db.psycopg, "connect", lambda *_args, **_kwargs: FakeConnection())

    assert db.query_dicts("SELECT secret", operation="test_query") == [{"value": 1}]
    snapshot = db.metrics_snapshot()

    assert snapshot["connections"] == 0
    assert snapshot["operations"] == 0
    assert snapshot["connection_total"] == {"success": 1}
    assert snapshot["operation_total"] == {("test_query", "success"): 1}
    assert snapshot["operation_count"] == {"test_query": 1}
    assert snapshot["rows_total"] == {"test_query": 1}
    assert "secret" not in repr(snapshot)


def test_connection_failure_is_counted(monkeypatch) -> None:
    db.reset_metrics()

    def fail(*_args, **_kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(db.psycopg, "connect", fail)

    try:
        db.query_dicts("SELECT 1", operation="test_query")
    except RuntimeError:
        pass
    else:
        raise AssertionError("connection failure must propagate")

    snapshot = db.metrics_snapshot()
    assert snapshot["connection_total"] == {"error": 1}
    assert snapshot["operation_total"] == {("test_query", "error"): 1}
