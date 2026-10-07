"""Snapshot facts retain full selections and explicit provenance."""
from decimal import Decimal
from types import SimpleNamespace as Row
from uuid import uuid4

from app.assistant import evidence, facts, retrieval
from app import auth
from app.assistant.conversations import ChatError
import pytest


def test_team_and_metric_use_complete_snapshot(monkeypatch):
    tasks = tuple(Row(task_id=f"T{i}", team_id="A", summary=f"Task {i}", prodf_id="P",
                      demand_hh=Decimal("2"), sp_to_plan=Decimal("1"), status="Open")
                  for i in range(70))
    plan = Row(pi_id="PI", as_of_sprint=0, status="built",
               params={"algorithm": "a", "formula_version": "f"}, actuals_upload_id=None,
               schedule=tuple(Row(task_id=f"T{i}", decision="in_quarter") for i in range(70)),
               states=(), assignments=(), alerts=(), team_capacity=(), role_demands=(),
               kpis=(Row(kpi_code="KPI", sprint_no=1, kind="forecast", value=Decimal("3")),),
               baseline=())
    inputs = Row(tasks=tasks, team_sp_per_sprint={"A": Decimal("5")}, sprint_count=6,
                 last_reported_sprint=0, fund_hours_per_fte=Decimal("480"))
    monkeypatch.setattr(facts.snapshots, "load", lambda _sid: (inputs, plan, {}, {}))
    sid = uuid4()
    team = facts.get_team(sid, "A")
    assert team["complete_selection"] and len(team["tasks"]) == 70
    assert len(team["schedule"]) == 70
    metric = facts.get_metric(sid, "kpi")
    assert metric["rows"][0]["value"] == "3"
    records, context = evidence.prepare(sid, "Почему KPI 3?", None,
                                        retrieval.SearchResult(None, (), "kb_not_indexed"), "")
    assert records[0]["payload"]["rows"][0]["value"] == "3"
    assert f"[evidence:{records[0]['evidence_id']}]" in context


def test_compare_runs_marks_observation_not_cause(monkeypatch):
    before_id, after_id = uuid4(), uuid4()

    def load(sid):
        end = 1 if sid == before_id else 2
        inputs = Row(source_sha256="source")
        plan = Row(pi_id="PI", as_of_sprint=0, params={"algorithm": "a", "formula_version": "f"},
                   actuals_upload_id=None, schedule=(Row(task_id="T", end_sprint=end),), kpis=())
        return inputs, plan, {}, {}

    monkeypatch.setattr(facts.snapshots, "load", load)
    result = facts.compare_runs(before_id, after_id)
    assert result["shared_tasks"] == 1 and len(result["changed_tasks"]) == 1
    assert "причины" in result["interpretation"]


def test_evidence_read_is_scoped_to_owner(monkeypatch):
    seen = {}

    def query(_sql, params):
        seen["params"] = params
        return None

    monkeypatch.setattr(evidence.db, "query_one", query)
    eid = uuid4()
    with pytest.raises(ChatError, match="evidence_not_found"):
        evidence.get(auth.Principal("user", "viewer", "db", 17), str(eid))
    assert seen["params"] == (eid, "db:17")


def test_unverified_citation_is_rejected():
    with pytest.raises(ValueError, match="unverified_evidence_reference"):
        evidence.check_citations({"summary": "[evidence:made-up]", "explanation": ""}, [])


def test_numeric_claim_is_rendered_from_saved_evidence():
    eid = uuid4()
    answer = {"summary": "Команда испытывает дефицит.", "explanation": "Есть незавершённые задачи.",
              "fact_refs": [{"evidence_id": str(eid), "field": "task_count"}]}
    records = [{"evidence_id": eid, "source_type": "snapshot", "payload": {"task_count": 17}}]
    evidence.render_fact_refs(answer, records)
    assert f"task_count = 17 [evidence:{eid}]" in answer["explanation"]
    answer["summary"] = "Команда имеет 99 задач."
    with pytest.raises(ValueError, match="untyped_numeric_claim"):
        evidence.render_fact_refs(answer, records)


def test_fact_cache_returns_an_independent_value(monkeypatch):
    calls = []

    def load(_sid):
        calls.append(1)
        return (Row(tasks=(), team_sp_per_sprint={}),
                Row(pi_id="PI", as_of_sprint=0, status="ok",
                    params={"algorithm": "a", "formula_version": "f"},
                    actuals_upload_id=None, schedule=(), assignments=(), alerts=(), kpis=()),
                {}, {})

    monkeypatch.setattr(facts.snapshots, "load", load)
    sid = uuid4()
    first = facts.get_overview(sid)
    first["task_count"] = 999
    second = facts.get_overview(sid)
    assert second["task_count"] == 0 and calls == [1]


def test_markdown_list_numbers_are_not_numeric_claims():
    answer = {"summary": "План строится по шагам.",
              "explanation": "1. Загрузка датасета.\n2) Проверка качества.\n  3. Расчёт плана.", "fact_refs": []}
    evidence.render_fact_refs(answer, [])
    answer["explanation"] += "\n4. В плане 37 задач."
    with pytest.raises(ValueError, match="untyped_numeric_claim"):
        evidence.render_fact_refs(answer, [])


def test_loan_totals_include_zero_sprints_and_only_foreign_orbits():
    from decimal import Decimal
    from types import SimpleNamespace
    rows = [SimpleNamespace(sprint_no=1, hours=Decimal('7.50'), home_team_id='A', serving_team_id='B'),
            SimpleNamespace(sprint_no=1, hours=Decimal('20'), home_team_id='B', serving_team_id='B')]
    totals = facts._loan_totals(rows, 2)
    assert totals['loan_hours'] == Decimal('7.50')
    assert totals['loan_hours_by_sprint'] == [{'sprint_no': 1, 'hours': Decimal('7.50')},
                                             {'sprint_no': 2, 'hours': Decimal(0)}]


def test_direct_loan_answer_uses_stored_amounts():
    eid = uuid4()
    records = [{'evidence_id': eid, 'source_type': 'snapshot',
                'payload': {'loan_hours': '267.00', 'loan_hours_by_sprint': [{'sprint_no': 1, 'hours': '100.00'}]}}]
    result = evidence.loan_answer(records, 1, False)
    assert result['status'] == 'answered' and '267.00' in result['summary']
    assert 'Спринт 1: 100.00' in result['explanation']
    assert str(eid) in result['evidence_ids']


def test_task_fallback_preserves_the_actual_question():
    records = [{'evidence_id': uuid4(), 'source_type': 'snapshot', 'payload': {
        'operation': 'get_task_trace', 'schedule': [{'task_id': 'ANL-3042', 'decision': 'deferred_next_pi',
                                                   'reason_text': 'Не хватает часов аналитика.'}]}}]
    result = evidence.task_answer(records, 1, False, ('task', 'ANL-3042'))
    assert 'ANL-3042' in result['summary'] and 'Не хватает часов аналитика' in result['explanation']
    assert 'Найдено проблем' not in result['summary']


def test_action_plan_counts_missing_roles_without_promising_a_gain(monkeypatch):
    from tests.test_planner import task, engineer, inputs
    from app import planner
    source = inputs([task('A', roles={1: 20}), task('B', roles={2: 20})], [engineer('E')], sprint_count=1)
    plan = planner.build_plan(source, simulate_next_pi=False)
    monkeypatch.setattr(facts.snapshots, 'load', lambda sid: (source, plan, {}, {}))
    sid = uuid4()
    payload = facts.get_improvement_plan(sid)
    assert payload['selected_tasks'] == 1 and payload['deferred_tasks'] == 1
    assert payload['missing_roles'][0]['task_ids'] == ['B']
    assert payload['effect_calculated'] is False
    records = [{'source_type': 'snapshot', 'evidence_id': uuid4(), 'payload': payload}]
    answer = evidence.action_answer(records, 1, False)
    assert 'Начните с роли' in answer['summary'] and 'сценарий' in answer['explanation']
    assert 'не равно гарантированному приросту' in answer['explanation']
