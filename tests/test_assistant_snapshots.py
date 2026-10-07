"""Historical replay uses saved inputs, including non-string map keys and modes."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from app import planner
from app import sensitivity
from app.assistant import snapshots
from tests.test_planner import engineer, inputs, task


def _record(source, plan, options):
    input_text = snapshots._dump({"inputs": source, "baseline_starts": {}, "options": options})
    return {
        "serialization_version": snapshots.SERIALIZATION_VERSION,
        "input_payload": json.loads(input_text),
        "plan_payload": json.loads(snapshots._dump(plan)),
        "input_sha256": hashlib.sha256(input_text.encode()).hexdigest(),
    }


def test_snapshot_replay_roundtrip_and_incompatible_algorithm(monkeypatch) -> None:
    source = inputs([task("A", roles={1: 80})], [engineer("E-1")])
    options = {
        "as_of_sprint": 0,
        "dependency_mode": planner.DEFAULT_DEPENDENCY_MODE,
        "initiative_mode": planner.INITIATIVE_MODE_GREEDY,
        "priority_strategy": planner.DEFAULT_PRIORITY_STRATEGY,
        "simulate_next_pi": True,
    }
    plan = planner.build_plan(source, baseline_starts={}, **options)
    record = _record(source, plan, options)
    monkeypatch.setattr(snapshots.db, "query_one", lambda *_args: record)
    restored_input, restored_plan, baseline, restored_options = snapshots.replay(uuid4())
    assert restored_input == source
    assert restored_plan.schedule == plan.schedule
    assert restored_options == options and baseline == {}
    assert restored_input.coverage == source.coverage
    assert restored_input.sprint_orbit_rates == source.sprint_orbit_rates

    old_plan = replace(plan, params={**plan.params, "algorithm": "unavailable@old"})
    record["plan_payload"] = json.loads(snapshots._dump(old_plan))
    with pytest.raises(snapshots.SnapshotUnavailable, match="cannot be replayed"):
        snapshots.replay(uuid4())


def test_snapshot_detects_changed_input_payload(monkeypatch) -> None:
    source = inputs([task("A")], [engineer("E-1")])
    plan = planner.build_plan(source)
    record = _record(source, plan, {
        "as_of_sprint": 0,
        "dependency_mode": planner.DEFAULT_DEPENDENCY_MODE,
        "initiative_mode": planner.INITIATIVE_MODE_GREEDY,
        "priority_strategy": planner.DEFAULT_PRIORITY_STRATEGY,
        "simulate_next_pi": True,
    })
    saved_input = next(value for key, value in record["input_payload"]["items"] if key == "inputs")
    saved_input["fields"]["tasks"]["items"][0]["fields"]["task_id"] = "other"
    monkeypatch.setattr(snapshots.db, "query_one", lambda *_args: record)
    with pytest.raises(snapshots.SnapshotUnavailable, match="checksum"):
        snapshots.load(uuid4())


def test_snapshot_save_accepts_search_options_beyond_replay_modes() -> None:
    source = inputs([task("A")], [engineer("E-1")])
    options = {
        "as_of_sprint": 0,
        "dependency_mode": planner.DEFAULT_DEPENDENCY_MODE,
        "initiative_mode": planner.INITIATIVE_MODE_GREEDY,
        "priority_strategy": planner.DEFAULT_PRIORITY_STRATEGY,
        "simulate_next_pi": False,
        "algorithm": planner.ALGORITHM_LOCAL_SEARCH,
        "max_time_seconds": 2.5,
        "random_seed": 17,
    }
    plan = planner.build_plan(source, **options)

    class Cursor:
        def __init__(self):
            self.calls = []

        def execute(self, query, params=None):
            self.calls.append((query, params))
            return self

        def fetchone(self):
            return {"generation_id": uuid4()}

    cursor = Cursor()
    snapshots.save(cursor, 12, source, plan, baseline_starts={}, options=options)

    input_text = cursor.calls[-1][1][4]
    saved = snapshots._decode(json.loads(input_text))
    assert saved["options"] == {
        key: options[key]
        for key in (
            "as_of_sprint", "dependency_mode", "initiative_mode", "priority_strategy", "simulate_next_pi"
        )
    }


def test_sensitivity_uses_the_saved_priority_and_input(monkeypatch) -> None:
    source = inputs([task("A")], [engineer("E-1")])
    options = {
        "as_of_sprint": 0,
        "dependency_mode": planner.DEFAULT_DEPENDENCY_MODE,
        "initiative_mode": planner.INITIATIVE_MODE_GREEDY,
        "priority_strategy": planner.PRIORITY_TASK,
        "simulate_next_pi": True,
    }
    original = planner.build_plan(source, baseline_starts={}, **options)
    monkeypatch.setattr(sensitivity.snapshots, "for_current_run", lambda _run_id: uuid4())
    monkeypatch.setattr(sensitivity.snapshots, "replay", lambda _id: (source, original, {}, options))
    monkeypatch.setattr(sensitivity.db, "query_one", lambda *_args: {"run_id": 7})
    monkeypatch.setattr(sensitivity.db, "scalar", lambda *_args: 7)
    monkeypatch.setattr(sensitivity.planner, "load_inputs", lambda: (_ for _ in ()).throw(
        AssertionError("scenario must use stored input")
    ))

    result = sensitivity.evaluate(7)
    assert result["run_id"] == 7
    assert len(result["scenarios"]) == 3
