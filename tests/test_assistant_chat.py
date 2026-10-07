"""Core chat boundaries without a live provider or database."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import auth
from app.assistant import conversations, memory, worker


def test_knowledge_chat_facts_need_no_dataset():
    assert "без прогона" in memory.facts(None, "Как работает система?")


def test_answer_shape_and_clarification():
    raw = {
        "status": "needs_clarification", "summary": "Уточните команду",
        "explanation": "Есть несколько вариантов", "clarification": "Какую команду вы имеете в виду?",
    }
    answer = memory.parse_answer(json.dumps(raw), 3, False, uuid4(), ("team", "ALPHA"))
    assert answer["context_revision"] == 3
    assert answer["evidence_ids"] == []
    assert answer["entity_refs"] == [{"type": "team", "id": "ALPHA", "context_revision": 3}]
    assert answer["clarification"]
    with pytest.raises(ValueError, match="missing_clarification"):
        memory.parse_answer(json.dumps({**raw, "clarification": ""}), 3, False, None)


def test_history_keeps_last_question_and_current_revision(monkeypatch):
    latest = uuid4()
    older = uuid4()
    seen = {}

    def fake_query(sql, params):
        seen["params"] = params
        return [{"message_id": latest, "role": "user", "content": "эта команда?"},
                {"message_id": older, "role": "assistant", "content": "Команда Альфа"}]

    monkeypatch.setattr(memory.db, "query_dicts", fake_query)
    cid = uuid4()
    assert memory.history(cid, 2, latest) == [
        {"role": "assistant", "content": "Команда Альфа"},
        {"role": "user", "content": "эта команда?"},
    ]
    assert seen["params"] == (cid, 2)


def test_followup_resolves_saved_team_only_in_same_revision(monkeypatch):
    monkeypatch.setattr(memory.snapshots, "load", lambda _sid: (
        SimpleNamespace(team_sp_per_sprint={"ALPHA": 10}, tasks=[]), None, None, None))
    seen = {}

    def fake_query(_sql, params):
        seen["revision"] = params[1]
        return {"payload": {"entity_refs": [{"type": "team", "id": "ALPHA"}]}}

    monkeypatch.setattr(memory.db, "query_one", fake_query)
    cid, sid = uuid4(), uuid4()
    assert memory.focus(cid, 4, sid, "Что с этой командой?") == ("team", "ALPHA")
    assert seen["revision"] == 4
    assert memory.focus(cid, 5, sid, "Сравни ALPHA") == ("team", "ALPHA")


def test_worker_rechecks_user_role(monkeypatch):
    monkeypatch.setattr(worker.db, "query_one", lambda *_: {
        "user_id": 7, "name": "Пользователь", "role": "viewer"})
    principal = worker._current_principal({
        "owner_key": "db:7", "input_payload": {"principal": {
            "source": "db", "user_id": 7, "role": "admin", "name": "старое имя"}},
    })
    assert principal == auth.Principal("Пользователь", "viewer", "db", 7)
    monkeypatch.setattr(worker.db, "query_one", lambda *_: None)
    with pytest.raises(conversations.ChatError, match="user_no_longer_active"):
        worker._current_principal({"owner_key": "db:7", "input_payload": {
            "principal": {"source": "db", "user_id": 7}}})


@pytest.mark.parametrize("raw", [None, {}, {"title": ""}, {"title": "   "}, {"title": "x" * 121}, {"title": 5},
                                 {"title": "ok", "extra": 1}])
def test_rename_rejects_bad_titles_before_touching_the_database(raw):
    with pytest.raises(conversations.ChatError) as exc:
        conversations.rename(auth.ANONYMOUS, str(uuid4()), raw)
    assert exc.value.code == "invalid_title"


def test_chat_owner_can_rename_and_delete_with_viewer_role():
    assert auth.required_role("DELETE", "/api/assistant/conversations/abc") == "viewer"
    assert auth.required_role("PUT", "/api/assistant/conversations/abc/title") == "viewer"


@pytest.mark.parametrize('question', ['почему ANL 3042 не попала в план', 'ANL3042', 'ANL–3042', 'anl-3042'])
def test_task_focus_accepts_alternate_separators(monkeypatch, question):
    source = SimpleNamespace(tasks=[SimpleNamespace(task_id='ANL-3042')], team_sp_per_sprint={})
    monkeypatch.setattr(memory.snapshots, 'load', lambda sid: (source, None, None, None))
    assert memory.focus(uuid4(), 1, uuid4(), question) == ('task', 'ANL-3042')


def test_general_question_does_not_inherit_previous_task(monkeypatch):
    source = SimpleNamespace(tasks=[SimpleNamespace(task_id='ANL-3042')], team_sp_per_sprint={})
    monkeypatch.setattr(memory.snapshots, 'load', lambda sid: (source, None, None, None))
    def unexpected_query(*args):
        raise AssertionError('A new general question should not inherit an old entity')
    monkeypatch.setattr(memory.db, 'query_one', unexpected_query)
    assert memory.focus(uuid4(), 1, uuid4(), 'напиши количество заемных часов') is None


def test_knowledge_chat_has_resource_rules_without_index():
    context = memory.facts(None, 'Откуда берутся роли, ёмкость команд и Bus Factor?')
    assert 'Excel' in context and 'focus_factor' in context and 'подтверждённым носителям' in context


@pytest.mark.parametrize('kind,question,payload,focus', [
    ('loan_hours', 'напиши количество заемных часов', {'loan_hours': '243.00'}, None),
    ('task_explanation', 'почему ANL 3042 не попала в план', {
        'operation': 'get_task_trace', 'schedule': [{'task_id': 'ANL-3042', 'decision': 'deferred_next_pi',
                                                  'reason_text': 'Нет роли руководителя проекта.'}]}, ('task', 'ANL-3042')),
])
def test_worker_answers_verified_direct_questions_without_model(monkeypatch, kind, question, payload, focus):
    from app.assistant import orchestrator, retrieval
    sid = uuid4()
    record = {'source_type': 'snapshot', 'evidence_id': uuid4(), 'payload': payload}
    row = {'content': question, 'context_revision': 1}
    monkeypatch.setattr(worker, '_prepare', lambda job: (
        row, {}, [], '', sid, False, focus, retrieval.SearchResult(None, (), 'not_required'),
        [record], orchestrator.Intent(kind)))
    monkeypatch.setattr(worker.db, 'query_one', lambda *args: {'running': True})
    results = []
    monkeypatch.setattr(worker, '_finish', lambda job, row, answer, usage, records: results.append((answer, usage)))
    def unexpected_generation(*args, **kwargs):
        raise AssertionError('Verified direct answers must not call the model')
    monkeypatch.setattr(worker.providers, 'generate', unexpected_generation)
    worker.process({'kind': 'message', 'job_id': uuid4(), 'attempt_count': 1})
    assert len(results) == 1 and results[0][0]['status'] == 'answered'
    assert results[0][1]['deterministic'] is True


def test_planning_actions_do_not_depend_on_provider_quota(monkeypatch):
    from app.assistant import orchestrator, retrieval
    row = {'content': 'обьясни с чего приступить увеличение количества задач', 'context_revision': 1}
    records = [{'source_type': 'snapshot', 'evidence_id': uuid4(), 'payload': {
        'operation': 'get_improvement_plan', 'task_count': 37, 'selected_tasks': 8,
        'deferred_tasks': 29, 'missing_roles': [], 'reason_counts': {}, 'skill_gap_tasks': []}}]
    monkeypatch.setattr(worker, '_prepare', lambda job: (
        row, {}, [], '', uuid4(), False, None, retrieval.SearchResult(None, (), 'not_required'),
        records, orchestrator.Intent('planning_actions')))
    monkeypatch.setattr(worker.db, 'query_one', lambda *args: {'running': True})
    answers = []
    monkeypatch.setattr(worker, '_finish', lambda job, row, answer, usage, records: answers.append(answer))
    def limited(*args, **kwargs):
        raise worker.providers.ProviderError('rate_limited', retryable=True)
    monkeypatch.setattr(worker.providers, 'generate', limited)
    worker.process({'kind': 'message', 'job_id': uuid4(), 'attempt_count': 1})
    assert len(answers) == 1 and answers[0]['status'] == 'answered'
