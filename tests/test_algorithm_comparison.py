"""Сравнительный запуск использует один и тот же набор входов для обоих планировщиков."""
from app import ingest, planner
from tests.test_planner import T1, engineer, inputs, task


def test_double_run_publishes_both_algorithms_and_reports_task_differences(monkeypatch) -> None:
    source = inputs(
        [
            task("A-80", sp=1, prodf="P-80", roles={1: 80}),
            task("B-40", sp=1, prodf="P-40-A", roles={1: 40}),
            task("C-40", sp=1, prodf="P-40-B", roles={1: 40}),
        ],
        [engineer("ENG-1")],
        sprint_count=1,
        team_sp={T1: 10},
    )
    monkeypatch.setattr(planner, "load_inputs", lambda: source)
    captured: list[tuple[object, dict]] = []

    def save(plan, **options):
        captured.append((plan, options))
        return 100 + len(captured)

    monkeypatch.setattr(planner, "write_plan", save)

    result = ingest.compare_algorithms(search_time_seconds=5)

    assert [run["run_id"] for run in result["runs"]] == [101, 102]
    assert [run["algorithm"] for run in result["runs"]] == [
        planner.ALGORITHM, planner.ALGORITHM_LOCAL_SEARCH,
    ]
    assert all(options["options"]["simulate_next_pi"] is (index == 0)
               for index, (_plan, options) in enumerate(captured))
    assert all(options["options"]["as_of_sprint"] == 0 for _plan, options in captured)
    assert result["different_tasks"] > 0
    assert any(row["assignments_changed"] or row["story_points_changed"]
               or row["greedy"] != row["lns"] for row in result["differences"])
