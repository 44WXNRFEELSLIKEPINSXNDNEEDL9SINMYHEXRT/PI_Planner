"""Measure planner quality and elapsed time on one locally captured Inputs snapshot.

The snapshot is a trusted local pickle containing inputs, baseline_starts and
last_run, captured together in a read-only repeatable-read transaction. No plans
are published. --code-root can point to an older checkout for paired measurements.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pickle
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--code-root', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--label', default='current')
    parser.add_argument('--budgets', type=float, nargs='+', default=[3, 10, 30])
    parser.add_argument('--seeds', type=int, nargs='+', default=[0, 1, 2])
    args = parser.parse_args()
    sys.path.insert(0, str(args.code_root.resolve()))
    os.environ['PLANNER_SEARCH_WORKERS'] = '1'
    from app import planner
    from app.planner.capacity import effective_capacity
    from app.planner.graph import _refresh_live_graph
    from app.planner.local_search.objective import evaluate_objective
    from app.planner.local_search.validation import validate_plan
    from dataclasses import replace
    import ortools

    code_files = ('app/planner/core.py', 'app/planner/local_search/search.py',
                  'app/planner/local_search/solver.py', 'app/planner/local_search/objective.py')
    code_digest = hashlib.sha256()
    for name in code_files:
        code_digest.update(name.encode())
        code_digest.update((args.code_root / name).read_bytes())

    raw = args.snapshot.read_bytes()
    payload = pickle.loads(raw)
    source = payload['inputs']
    last = payload.get('last_run') or {}
    params = last.get('params') or {}
    as_of = int(last.get('as_of_sprint', 0))
    options = dict(
        as_of_sprint=as_of,
        baseline_starts=payload.get('baseline_starts', {}) if as_of > 0 else {},
        dependency_mode=params.get('dependency_mode', planner.DEFAULT_DEPENDENCY_MODE),
        initiative_mode=params.get('initiative_mode', planner.INITIATIVE_MODE_GREEDY),
        priority_strategy=params.get('priority_strategy', planner.DEFAULT_PRIORITY_STRATEGY),
        simulate_next_pi=False,
    )
    effective, _ = _refresh_live_graph(source, as_of, options['dependency_mode'])
    capacity, _ = effective_capacity(effective, as_of)
    effective = replace(effective, team_sp_per_sprint=capacity)
    report = {
        'label': args.label,
        'code_sha256': code_digest.hexdigest(),
        'measured_at_utc': datetime.now(timezone.utc).isoformat(),
        'snapshot_sha256': hashlib.sha256(raw).hexdigest(),
        'source_sha256': source.source_sha256,
        'pi_id': source.pi_id,
        'tasks': len(source.tasks),
        'engineers': len(source.engineers),
        'sprints': source.sprint_count,
        'as_of_sprint': as_of,
        'baseline_tasks': len(source.baseline_schedule),
        'rules': {key: options[key] for key in ('dependency_mode', 'initiative_mode', 'priority_strategy')},
        'python': platform.python_version(),
        'ortools': ortools.__version__,
        'workers': 1,
        'cpu_count': os.cpu_count(),
        'cpu_affinity_count': len(os.sched_getaffinity(0)),
        'budgets': args.budgets,
        'seeds': args.seeds,
        'runs': [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    greedy_vector = None
    runs = [(planner.ALGORITHM, 0, 0)] + [
        (planner.ALGORITHM_LOCAL_SEARCH, budget, seed)
        for budget in args.budgets for seed in args.seeds
    ]
    for algorithm, budget, seed in runs:
        started = time.perf_counter()
        try:
            plan = planner.build_plan(source, **options, algorithm=algorithm,
                                      max_time_seconds=budget or 10, random_seed=seed)
            elapsed = time.perf_counter() - started
            objective = evaluate_objective(plan, effective, priority_strategy=options['priority_strategy'])
            if greedy_vector is None:
                greedy_vector = objective.vector
            optimization = plan.params.get('optimization') or {}
            row = {
                'algorithm': algorithm,
                'actual_algorithm': plan.params['algorithm'],
                'budget_seconds': budget,
                'seed': seed,
                'elapsed_seconds': round(elapsed, 6),
                'objective': objective.as_dict(),
                'vs_greedy': 'better' if objective.vector > greedy_vector else (
                    'equal' if objective.vector == greedy_vector else 'worse'),
                'complete_initiatives': sum(objective.complete_initiatives),
                'selected_tasks': len(plan.in_quarter),
                'selected_task_ids': sorted(row.task_id for row in plan.in_quarter),
                'completion_sprint_sum': -objective.negative_completion_sprint_sum,
                'loan_hours': str(objective.loan_hours),
                'solver_status': optimization.get('solver_status'),
                'bounds_scope': optimization.get('objective_bounds_scope'),
                'neighborhoods_attempted': optimization.get('neighborhoods_attempted', 0),
                'neighborhoods_improved': optimization.get('neighborhoods_improved', 0),
                'fallback_reason': plan.params.get('fallback_reason'),
                'validation_errors': list(validate_plan(plan, effective, dependency_mode=options['dependency_mode'])),
            }
        except Exception as exc:
            row = dict(algorithm=algorithm, budget_seconds=budget, seed=seed,
                       elapsed_seconds=round(time.perf_counter() - started, 6), error=str(exc))
        report['runs'].append(row)
        save()
        print(json.dumps({'label': args.label, **{key: row.get(key) for key in (
            'algorithm', 'budget_seconds', 'seed', 'elapsed_seconds', 'complete_initiatives',
            'selected_tasks', 'completion_sprint_sum', 'loan_hours', 'vs_greedy', 'solver_status', 'error'
        )}}, ensure_ascii=False), flush=True)
    return int(any('error' in row for row in report['runs']))


if __name__ == '__main__':
    raise SystemExit(main())
