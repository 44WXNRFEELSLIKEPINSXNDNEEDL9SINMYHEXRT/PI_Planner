"""Проверки CLI-обвязки планировщика."""

from contextlib import nullcontext

from tools import run_planner
from tests.test_planner import engineer, inputs, task


def test_if_empty_does_not_replace_an_existing_plan(monkeypatch, capsys) -> None:
    """Контейнерный bootstrap не создаёт новый прогон при каждом рестарте."""
    monkeypatch.setattr(run_planner.db, "dsn", lambda: "dbname=test")
    monkeypatch.setattr(run_planner.db, "atomic_transaction", nullcontext)
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
    assert "as_of_sprint = 0" in sql[0] and "status IN ('ok', 'infeasible')" in sql[0]


def test_validation_failure_exits_nonzero_and_reports_run(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_planner.db, "dsn", lambda: "dbname=test")
    monkeypatch.setattr(run_planner.db, "atomic_transaction", nullcontext)
    monkeypatch.setattr(run_planner.planner, "load_inputs",
                        lambda: inputs([task("A")], [engineer("ENG-1")]))
    violation = {"check_code": "UNDER_ALLOCATED", "entity": "A / QA", "detail": "0 ЧЧ"}

    def reject(_plan, **_kwargs):
        raise run_planner.planner.PlanValidationError(42, 1, [violation])

    monkeypatch.setattr(run_planner.planner, "write_plan", reject)

    assert run_planner.main([]) == 1
    stderr = capsys.readouterr().err
    assert "прогон 42 не опубликован" in stderr
    assert "UNDER_ALLOCATED" in stderr


def test_planner_options_can_be_selected_from_environment(monkeypatch, capsys) -> None:
    monkeypatch.setenv("PLANNER_ALGORITHM", "lns-cpsat@1")
    monkeypatch.setenv("PLANNER_SEARCH_TIME_SECONDS", "2.5")
    monkeypatch.setenv("PLANNER_RANDOM_SEED", "17")
    monkeypatch.setattr(run_planner.db, "dsn", lambda: "dbname=test")
    monkeypatch.setattr(run_planner.db, "atomic_transaction", nullcontext)
    monkeypatch.setattr(
        run_planner.planner,
        "load_inputs",
        lambda: inputs([task("A")], [engineer("ENG-1")]),
    )
    original_build_plan = run_planner.planner.build_plan
    captured: dict[str, object] = {}

    def capture_options(source, **options):
        captured.update(options)
        return original_build_plan(source, **options)

    monkeypatch.setattr(run_planner.planner, "build_plan", capture_options)

    assert run_planner.main(["--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "алгоритм: lns-cpsat@1" in output
    assert captured["algorithm"] == "lns-cpsat@1"
    assert captured["max_time_seconds"] == 2.5
    assert captured["random_seed"] == 17
    assert captured["simulate_next_pi"] is False
