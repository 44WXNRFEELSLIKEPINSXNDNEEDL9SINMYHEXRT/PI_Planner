"""Lossless, versioned planner snapshots for historical explanation and replay."""
from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app import db
from app.planner.constants import ALGORITHM, FORMULA_VERSION
from app.planner.model import (
    AlertRow, Assignment, BaselineRow, CapacityRow, EngineerInput, Inputs,
    KpiRow, Plan, ScheduleRow, StateRow, TaskInput,
)

SERIALIZATION_VERSION = 1
MODEL_TYPES = {cls.__name__: cls for cls in (
    TaskInput, EngineerInput, Inputs, ScheduleRow, Assignment, AlertRow,
    KpiRow, BaselineRow, StateRow, CapacityRow, Plan,
)}
TAG = "__rag_type__"


class SnapshotUnavailable(ValueError):
    pass


def _encode(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return {TAG: "decimal", "value": str(value)}
    if isinstance(value, datetime):
        return {TAG: "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {TAG: "date", "value": value.isoformat()}
    if is_dataclass(value) and not isinstance(value, type):
        name = type(value).__name__
        if name not in MODEL_TYPES or MODEL_TYPES[name] is not type(value):
            raise TypeError(f"snapshot class {name} is not supported")
        return {TAG: "dataclass", "name": name,
                "fields": {field.name: _encode(getattr(value, field.name)) for field in fields(value)}}
    if isinstance(value, dict):
        pairs = [[_encode(key), _encode(item)] for key, item in value.items()]
        pairs.sort(key=lambda pair: json.dumps(pair[0], sort_keys=True, ensure_ascii=False))
        return {TAG: "map", "items": pairs}
    if isinstance(value, tuple):
        return {TAG: "tuple", "items": [_encode(item) for item in value]}
    if isinstance(value, frozenset):
        items = [_encode(item) for item in value]
        items.sort(key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
        return {TAG: "frozenset", "items": items}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    raise TypeError(f"snapshot value {type(value).__name__} is not supported")


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value.get(TAG)
    if kind is None:
        return {key: _decode(item) for key, item in value.items()}
    if kind == "decimal":
        return Decimal(value["value"])
    if kind == "datetime":
        return datetime.fromisoformat(value["value"])
    if kind == "date":
        return date.fromisoformat(value["value"])
    if kind == "map":
        return {_decode(key): _decode(item) for key, item in value["items"]}
    if kind == "tuple":
        return tuple(_decode(item) for item in value["items"])
    if kind == "frozenset":
        return frozenset(_decode(item) for item in value["items"])
    if kind == "dataclass":
        cls = MODEL_TYPES.get(value["name"])
        if cls is None:
            raise SnapshotUnavailable(f"unknown snapshot class: {value['name']}")
        return cls(**{key: _decode(item) for key, item in value["fields"].items()})
    raise SnapshotUnavailable(f"unknown snapshot value: {kind}")


def _dump(value: Any) -> str:
    return json.dumps(_encode(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def save(cur: Any, run_id: int, inputs: Inputs, plan: Plan, *,
         baseline_starts: dict[str, int], options: dict[str, Any]) -> UUID:
    """Called from writer's transaction; a failed plan write leaves no snapshot."""
    expected_options = {
        "as_of_sprint": plan.as_of_sprint,
        "dependency_mode": plan.params.get("dependency_mode"),
        "initiative_mode": plan.params.get("initiative_mode"),
        "priority_strategy": plan.params.get("priority_strategy"),
        "simulate_next_pi": plan.params.get("simulate_next_pi"),
    }
    # Search-specific CLI options are recorded on the plan, but the current
    # snapshot replay contract only stores the five stable planner modes above.
    snapshot_options = {key: options.get(key) for key in expected_options}
    if inputs.pi_id != plan.pi_id or snapshot_options != expected_options:
        raise ValueError("snapshot input or options do not match the published plan")
    row = cur.execute(
        "SELECT generation_id FROM public.assistant_dataset_generations "
        "WHERE schema_name = current_schema() AND active"
    ).fetchone()
    if row is None:
        # Also covers a manually seeded installation that did not run the
        # generated migration-stamps bootstrap file.
        generation_id = uuid4()
        cur.execute(
            "INSERT INTO public.assistant_dataset_generations "
            "(generation_id, schema_name, pi_id, scenario_id, source_sha256) "
            "VALUES (%s, current_schema(), %s, "
            "COALESCE((SELECT scenario_id FROM public.pi_contexts "
            "WHERE schema_name = current_schema() LIMIT 1), 'main'), %s)",
            (generation_id, inputs.pi_id, inputs.source_sha256 or "unloaded"),
        )
    else:
        generation_id = row["generation_id"]
    input_text = _dump({
        "inputs": inputs, "baseline_starts": baseline_starts, "options": snapshot_options,
    })
    plan_text = _dump(plan)
    snapshot_id = uuid4()
    cur.execute(
        "INSERT INTO public.assistant_input_snapshots "
        "(snapshot_id, generation_id, run_id, serialization_version, "
        "input_payload, plan_payload, input_sha256) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)",
        (snapshot_id, generation_id, run_id, SERIALIZATION_VERSION,
         input_text, plan_text, hashlib.sha256(input_text.encode("utf-8")).hexdigest()),
    )
    return snapshot_id


def load(snapshot_id: UUID) -> tuple[Inputs, Plan, dict[str, int], dict[str, Any]]:
    row = db.query_one(
        "SELECT serialization_version, input_payload, plan_payload, input_sha256 "
        "FROM public.assistant_input_snapshots WHERE snapshot_id = %s", (snapshot_id,),
    )
    if row is None:
        raise SnapshotUnavailable(f"snapshot {snapshot_id} does not exist")
    if row["serialization_version"] != SERIALIZATION_VERSION:
        raise SnapshotUnavailable(f"unsupported snapshot version: {row['serialization_version']}")
    input_text = json.dumps(row["input_payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if hashlib.sha256(input_text.encode("utf-8")).hexdigest() != row["input_sha256"]:
        raise SnapshotUnavailable("snapshot checksum mismatch")
    payload = _decode(row["input_payload"])
    plan = _decode(row["plan_payload"])
    if not isinstance(payload["inputs"], Inputs) or not isinstance(plan, Plan):
        raise SnapshotUnavailable("snapshot has unexpected content")
    return payload["inputs"], plan, payload["baseline_starts"], payload["options"]


def for_run(generation_id: UUID, run_id: int) -> UUID:
    row = db.query_one(
        "SELECT snapshot_id FROM public.assistant_input_snapshots "
        "WHERE generation_id = %s AND run_id = %s", (generation_id, run_id),
    )
    if row is None:
        raise SnapshotUnavailable(f"run {run_id} has no complete input snapshot")
    return row["snapshot_id"]


def for_current_run(run_id: int) -> UUID:
    """Resolve run_id only within the current schema's active generation."""
    row = db.query_one(
        "SELECT s.snapshot_id FROM public.assistant_input_snapshots s "
        "JOIN public.assistant_dataset_generations g USING (generation_id) "
        "WHERE g.schema_name = current_schema() AND g.active AND s.run_id = %s",
        (run_id,),
    )
    if row is None:
        raise SnapshotUnavailable(f"current run {run_id} has no complete input snapshot")
    return row["snapshot_id"]


def replay(snapshot_id: UUID) -> tuple[Inputs, Plan, dict[str, int], dict[str, Any]]:
    """Refuse incompatible algorithms or a baseline that no longer reproduces."""
    from app.planner.core import build_plan

    inputs, original, baseline_starts, options = load(snapshot_id)
    params = original.params
    if params.get("algorithm") != ALGORITHM or params.get("formula_version") != FORMULA_VERSION:
        raise SnapshotUnavailable("the recorded algorithm or formula version cannot be replayed")
    required = {"as_of_sprint", "dependency_mode", "initiative_mode", "priority_strategy", "simulate_next_pi"}
    if set(options) != required:
        raise SnapshotUnavailable("the recorded planner options are incomplete")
    rebuilt = build_plan(inputs, baseline_starts=baseline_starts, **options)
    compared = (
        "pi_id", "as_of_sprint", "status", "schedule", "assignments", "alerts", "kpis",
        "baseline", "states", "sp_shares", "actuals_upload_id", "role_demands",
        "graph_bounds", "team_capacity", "capacity_snapshot",
    )
    if any(getattr(rebuilt, field) != getattr(original, field) for field in compared):
        raise SnapshotUnavailable("the recorded run does not reproduce on its stored input")
    return inputs, original, baseline_starts, options
