"""Проверки CLI-обвязки планировщика."""

from tools import run_planner


def test_if_empty_does_not_replace_an_existing_plan(monkeypatch, capsys) -> None:
    """Контейнерный bootstrap не создаёт новый прогон при каждом рестарте."""
    monkeypatch.setattr(run_planner.db, "dsn", lambda: "dbname=test")
    sql: list[str] = []

    def scalar(query: str) -> int:
        sql.append(query)
        return 1

    monkeypatch.setattr(run_planner.db, "scalar", scalar)

    def must_not_load_inputs():
        raise AssertionError("существующий план не должен пересчитываться")

    monkeypatch.setattr(run_planner.planner, "load_inputs", must_not_load_inputs)

    assert run_planner.main(["--if-empty"]) == 0
    assert "пропуск" in capsys.readouterr().out
    assert "as_of_sprint = 0" in sql[0] and "status = 'ok'" in sql[0]
