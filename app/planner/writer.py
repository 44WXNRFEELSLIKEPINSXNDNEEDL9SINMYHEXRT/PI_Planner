"""Запись прогона и всего контракта одной транзакцией."""
from __future__ import annotations

import json
import time
from typing import Any

from app import db
from app.planner.constants import ALGORITHM
from app.planner.model import Inputs, Plan


# ---------------------------------------------------------------------------
#  ЗАПИСЬ: весь контракт одной транзакцией
# ---------------------------------------------------------------------------
class PlanValidationError(RuntimeError):
    """План сохранён для диагностики, но не опубликован из-за инвариантов."""

    def __init__(self, run_id: int, errors: int, violations: list[dict[str, Any]]) -> None:
        self.run_id = run_id
        self.errors = errors
        self.violations = violations
        super().__init__(f"прогон {run_id} не опубликован: {errors} ошибок приёмки")


def write_plan(plan: Plan, *, inputs: Inputs | None = None,
               baseline_starts: dict[str, int] | None = None,
               options: dict[str, Any] | None = None) -> int:
    """Пишет и проверяет контракт в одной транзакции; публикует только без ошибок.

    `is_loan` не пишем никогда — это генерируемая колонка (см. docs/RUNBOOK.md, раздел 7).
    """
    started = time.perf_counter()
    plan.params.setdefault("observability", {})
    with db.transaction(operation="planner_write") as cur:
        canonical_baseline_id: int | None = None
        if plan.as_of_sprint == 0:
            cur.execute(
                "SELECT MIN(run_id) AS run_id FROM plan_runs "
                "WHERE as_of_sprint = 0 AND status IN ('ok', 'infeasible')"
            )
            baseline_row = cur.fetchone()
            if baseline_row and baseline_row.get("run_id") is not None:
                canonical_baseline_id = int(baseline_row["run_id"])
        cur.execute(
            """
            INSERT INTO plan_runs (pi_id, as_of_sprint, algorithm, params, status, note,
                                   actuals_upload_id)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
            RETURNING run_id
            """,
            (
                plan.pi_id,
                plan.as_of_sprint,
                plan.params.get("algorithm", ALGORITHM),
                json.dumps(plan.params, ensure_ascii=False),
                "failed",  # до проверки результат не виден как успешный
                plan.note,
                plan.actuals_upload_id,
            ),
        )
        row = cur.fetchone()
        if row is None:  # INSERT ... RETURNING без строки — такого быть не может
            raise RuntimeError("plan_runs не вернул run_id")
        run_id = int(row["run_id"])
        if plan.as_of_sprint == 0:
            canonical_baseline_id = canonical_baseline_id or run_id
            plan.params["baseline_run_id"] = canonical_baseline_id
            cur.execute("UPDATE plan_runs SET params = %s::jsonb WHERE run_id = %s",
                        (json.dumps(plan.params, ensure_ascii=False), run_id))

        if plan.as_of_sprint == 0 and canonical_baseline_id != run_id:
            # Alternative baseline plans may select different tasks, but the
            # committed snapshot is immutable and belongs to the first
            # published baseline. Reusing it keeps BASELINE_MUTATED invariant.
            cur.execute(
                """
                INSERT INTO plan_baseline (run_id, task_id, planned_sp, committed)
                SELECT %s, task_id, planned_sp, committed
                FROM plan_baseline WHERE run_id = %s
                """,
                (run_id, canonical_baseline_id),
            )
        elif plan.baseline:
            cur.executemany(
                """
                INSERT INTO plan_baseline (run_id, task_id, planned_sp, committed)
                VALUES (%s, %s, %s, %s)
                """,
                [(run_id, row.task_id, row.planned_sp, row.committed) for row in plan.baseline],
            )

        cur.executemany(
            """
            INSERT INTO plan_task_schedule
                (run_id, task_id, start_sprint, end_sprint, forecast_end_date,
                 decision, decision_reason, reason_code, reason_text, reason_details)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            [
                (
                    run_id,
                    row.task_id,
                    row.start_sprint,
                    row.end_sprint,
                    row.forecast_end_date,
                    row.decision,
                    row.decision_reason,
                    row.reason_code,
                    row.reason_text,
                    json.dumps(row.reason_details, ensure_ascii=False),
                )
                for row in plan.schedule
            ],
        )

        if plan.sp_shares:
            cur.executemany(
                "INSERT INTO plan_task_sp (run_id, task_id, sprint_no, sp) VALUES (%s, %s, %s, %s)",
                [(run_id, task_id, sprint_no, sp) for task_id, sprint_no, sp in plan.sp_shares],
            )

        if plan.role_demands:
            cur.executemany(
                """INSERT INTO plan_role_demand_snapshot
                   (run_id, task_id, role_id, needed_hours) VALUES (%s, %s, %s, %s)""",
                [(run_id, task_id, role_id, hours)
                 for task_id, role_id, hours in plan.role_demands],
            )

        if plan.team_capacity:
            cur.executemany(
                """INSERT INTO plan_team_capacity
                   (run_id, team_id, history_points, observed_points, avg_velocity, focus_factor,
                    available_sp_per_sprint, observed_through_sprint)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                [(run_id, row.team_id, row.history_points, row.observed_points, row.avg_velocity,
                  row.focus_factor, row.available_sp_per_sprint, row.observed_through_sprint)
                 for row in plan.team_capacity],
            )

        if plan.capacity_snapshot:
            cur.executemany(
                """INSERT INTO plan_capacity_snapshot
                   (run_id, engineer_id, team_id, sprint_no, available_hours)
                   VALUES (%s, %s, %s, %s, %s)""",
                [(run_id, engineer_id, team_id, sprint_no, hours)
                 for engineer_id, team_id, sprint_no, hours in plan.capacity_snapshot],
            )

        if plan.graph_bounds:
            cur.executemany(
                """INSERT INTO plan_dependency_bounds
                   (run_id, task_id, earliest_start_sprint) VALUES (%s, %s, %s)""",
                [(run_id, task_id, earliest) for task_id, earliest in plan.graph_bounds],
            )

        if plan.assignments:
            cur.executemany(
                """
                INSERT INTO plan_assignments
                    (run_id, task_id, sprint_no, engineer_id, role_id, hours,
                     home_team_id, serving_team_id, work_hours)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        run_id,
                        row.task_id,
                        row.sprint_no,
                        row.engineer_id,
                        row.role_id,
                        row.hours,
                        row.home_team_id,
                        row.serving_team_id,
                        row.work_hours if row.work_hours is not None else row.hours,
                    )
                    for row in plan.assignments
                ],
            )

        cur.executemany(
            """
            INSERT INTO task_state
                (run_id, task_id, as_of_sprint, status, remaining_hh, remaining_sp,
                 forecast_end_sprint)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    run_id,
                    row.task_id,
                    row.as_of_sprint,
                    row.status,
                    row.remaining_hh,
                    row.remaining_sp,
                    row.forecast_end_sprint,
                )
                for row in plan.states
            ],
        )

        if plan.alerts:
            cur.executemany(
                """
                INSERT INTO alerts
                    (run_id, sprint_no, level, alert_type, entity_type, entity_id,
                     message, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                [
                    (
                        run_id,
                        row.sprint_no,
                        row.level,
                        row.alert_type,
                        row.entity_type,
                        row.entity_id,
                        row.message,
                        json.dumps(row.payload, ensure_ascii=False),
                    )
                    for row in plan.alerts
                ],
            )

        cur.executemany(
            """
            INSERT INTO kpi_snapshots
                (run_id, sprint_no, kpi_code, value, target_min, target_max, details, kind, calculation_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            [
                (
                    run_id,
                    row.sprint_no,
                    row.kpi_code,
                    row.value,
                    row.target_min,
                    row.target_max,
                    json.dumps(row.details, ensure_ascii=False),
                    row.kind,
                    row.calculation_status,
                )
                for row in plan.kpis
            ],
        )
        write_seconds = round(time.perf_counter() - started, 6)
        plan.params.setdefault("observability", {})["write_plan_seconds"] = write_seconds
        cur.execute(
            """
            UPDATE plan_runs
            SET params = jsonb_set(
                params,
                '{observability,write_plan_seconds}',
                to_jsonb(%s::numeric),
                true
            )
            WHERE run_id = %s
            """,
            (write_seconds, run_id),
        )
        # Вьюха видит все записанные строки в этой транзакции. Читатели не
        # увидят status='ok' до того, как независимая проверка закончится.
        cur.execute(
            """SELECT check_code, entity, detail
               FROM v_plan_violations
               WHERE run_id = %s AND severity = 'error'
               ORDER BY check_code, entity, detail""",
            (run_id,),
        )
        violations = list(cur.fetchall())
        if violations:
            sample = violations[:20]
            cur.execute(
                """UPDATE plan_runs
                   SET params = jsonb_set(params, '{validation}', %s::jsonb, true),
                       note = %s
                   WHERE run_id = %s""",
                (
                    json.dumps({"errors": len(violations), "sample": sample}, ensure_ascii=False),
                    f"Не опубликован: {len(violations)} ошибок приёмки",
                    run_id,
                ),
            )
        else:
            cur.execute(
                """UPDATE plan_runs
                   SET status = %s,
                       params = jsonb_set(params, '{validation}', '{"errors": 0}'::jsonb, true)
                   WHERE run_id = %s""",
                (plan.status, run_id),
            )
            if inputs is not None:
                from app.assistant import snapshots

                if options is None:
                    raise ValueError("snapshot options are required for a published plan")
                snapshots.save(cur, run_id, inputs, plan,
                               baseline_starts=baseline_starts or {}, options=options)
    if violations:
        raise PlanValidationError(run_id, len(violations), sample)
    return run_id
