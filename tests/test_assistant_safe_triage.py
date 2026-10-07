from types import SimpleNamespace as Row
from uuid import uuid4
import pytest
from app import auth
from app.assistant import diagnostics, evidence, triage


def test_diagnostics_only_env_admin():
    payload = {'summary': 'safe', '_diagnostics': {'job_id': 'job', 'reasons': ['invalid_model_json']},
               'nested': [{'diagnostics': {'secret': 'hidden'}}]}
    for principal in (auth.Principal('user', 'viewer', 'db', 1), auth.Principal('admin', 'admin', 'db', 2), auth.ANONYMOUS):
        result = diagnostics.public_payload(payload, principal)
        assert '_diagnostics' not in result and 'diagnostics' not in result
        assert result['nested'] == [{}]
    admin_payload = diagnostics.public_payload(payload, auth.Principal('admin-token', 'admin', 'env'))
    assert admin_payload['diagnostics']['reasons'] == ['invalid_model_json']
    assert '_diagnostics' in payload
    assert diagnostics.reason('provider body or secret') == 'invalid_answer_or_context'


def test_entity_and_number_injection():
    eid = str(uuid4())
    records = [{'evidence_id': eid, 'source_type': 'snapshot', 'payload': {'task_id': 'TASK-12', 'count': 7}}]
    good = {'summary': 'Задача [entity:TASK-12]', 'explanation': '', 'fact_refs': [{'evidence_id': eid, 'field': 'count'}]}
    evidence.render_fact_refs(good, records)
    assert 'count = 7' in good['explanation']
    for text, reason in [('Проверенные значения: выдумано 999', 'untyped_numeric_claim'),
                         ('Задача [entity:TASK-999]', 'unverified_entity_reference')]:
        with pytest.raises(ValueError, match=reason):
            evidence.render_fact_refs({'summary': text, 'explanation': '', 'fact_refs': []}, records)
    with pytest.raises(ValueError, match='invalid_fact_reference'):
        evidence.render_fact_refs({'summary': 'Ответ', 'explanation': '',
                                   'fact_refs': [{'evidence_id': str(uuid4()), 'field': 'count'}]}, records)


def test_complete_ranked_report_and_followup(monkeypatch):
    tasks = [Row(task_id=f'T{i}', prodf_id='P', estimate_disputed=i == 0, remaining_unknown=False) for i in range(14)]
    alerts = [Row(alert_type='deadline_miss', entity_type='task', entity_id=t.task_id, level='red',
                  sprint_no=1, message='Проверьте срок', payload={}) for t in tasks]
    inputs = Row(tasks=tasks, all_deps=(('T0', 'T1', 0), ('T1', 'T2', 0)))
    monkeypatch.setattr(triage.snapshots, 'load', lambda sid: (inputs, Row(alerts=alerts), {}, {}))
    sid = uuid4()
    result, records = triage.answer(sid, 'все критические проблемы', None, 1, False)
    assert result['triage']['total'] == 15 and len(result['triage']['issues']) == 10
    assert len(records[0]['payload']['issues']) == 15
    assert result['triage']['issues'][0]['kind'] == 'data_quality'
    assert result['triage']['issues'][0]['affected_count'] == 3
    followup, _ = triage.answer(sid, 'подробнее о первой', result['triage'], 1, False)
    assert len(followup['triage']['issues']) == 1
    assert followup['triage']['issues'][0]['issue_id'] == result['triage']['issues'][0]['issue_id']
    next_page, _ = triage.answer(sid, 'следующие проблемы', result['triage'], 1, False)
    assert len(next_page['triage']['issues']) == 5
    assert triage.requested('можешь загрузить все критичные проблемы себе в контекст и сказать порядок', None)
    monkeypatch.setattr(triage.db, 'query_one', lambda *a: {'payload': result})
    assert triage.previous(uuid4(), 1, uuid4()) is None


def test_job_error_matches_ui_envelope_and_preserves_retry():
    principal = auth.Principal('user', 'viewer', 'db', 1)
    result = diagnostics.job_error({'code': 'rate_limited', 'message': 'rate_limited', 'retryable': True}, principal, 'job')
    assert result['request_id'] == 'job'
    assert result['error']['code'] == 'rate_limited'
    assert result['error']['retryable'] is True
    assert 'лимита запросов' in result['error']['message']
    assert diagnostics.job_error(None, principal, 'job') is None
