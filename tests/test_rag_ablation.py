"""Regressions for M3-14 grading and policy decisions."""
from copy import deepcopy
from pathlib import Path
import subprocess
import sys

from eval.ablation_metrics import ARMS, decide, load_frozen, score, summarize


def test_frozen_workload_has_per_question_gold_and_forbidden_states():
    protocol, corpus, questions = load_frozen()
    assert len(questions) == 108
    assert all(sum(q['category'] == c for q in questions) == 18 for c in protocol['categories'])
    assert {r['state'] for r in corpus.values()} == {'active','foreign','revoked','deleted'}
    assert all(len(q['gold']) == 2 for q in questions if q['category']=='multi')


def test_score_observes_pre_rerank_recall_and_only_final_evidence_for_ranking():
    q = {'gold':{'a':'fact A', 'b':'fact B'}}
    row = {'top20':['a','b'], 'final':['different','a']}
    values = score(q, row)
    assert values['recall20'] == 1 and values['mrr5'] == .5 and 0 < values['ndcg5'] < .5
    assert score({'gold':{}}, row)['ndcg5'] is None
    row['final'] = ['a','a']
    assert score(q, row)['ndcg5'] == score(q, {**row, 'final':['a']})['ndcg5']


def test_no_evidence_and_errors_cannot_improve_the_gate():
    protocol, _, _ = load_frozen()
    base = {'recall20':.9,'ndcg5':.6,'locator_accuracy':1,'questions':108,'successful_retrievals':108,
            'p95_ms':300,'external_cny_per_success':0,'forbidden_citations':0,'forbidden_dispatches':0,
            'expanded_questions':10,'reranked_questions':100}
    groups = {arm:deepcopy(base) for arm in ARMS}
    groups['C']['ndcg5'] = .61
    groups['D']['ndcg5'] = .62
    assert all(g['eligible'] for g in decide(protocol, groups).values())
    groups['B']['locator_accuracy'] = None
    assert not any(decide(protocol, groups)[a]['eligible'] for a in 'BCD')
    groups['B']['locator_accuracy'] = 1
    for metric, value in [('expanded_questions',0), ('external_cny_per_success',None), ('p95_ms',1001),
                          ('forbidden_dispatches',1), ('forbidden_citations',1), ('successful_retrievals',107)]:
        altered = deepcopy(groups)
        altered['D'][metric] = value
        assert not decide(protocol, altered)['D']['eligible']
    groups['D']['ndcg5'] = .619
    assert not decide(protocol, groups)['D']['eligible']


def test_negative_cases_have_abstention_metrics_and_no_manufactured_perfect_scores():
    row = dict(final=[], top20=[], top20_families={}, latency_ms=10, status='error', expansion={'status':'off'},
               rerank='unavailable', exclusive={}, abstained=False)
    row.update(score({'gold':{}}, row))
    for key in ('claims','supported_claims','answered_facts','gold_facts','answer_error','forbidden_dispatches',
                'forbidden_citations','verified_locators','locator_total','external_cny','drift_rejections',
                'parsed_generations','expansion_attempts'):
        row[key] = 0
    result = summarize([row])
    assert result['recall20'] is result['ndcg5'] is result['locator_accuracy'] is None
    assert result['external_cny_per_success'] is None and result['answer_abstention_accuracy'] == 0


def test_local_startup_honors_reranking_decision_and_explicit_opt_in():
    # Importing main registers global routes and loads .env. Exercise the actual
    # startup entry point in a fresh interpreter, isolated from other API tests.
    program = """
import asyncio
from unittest.mock import Mock, patch
from types import SimpleNamespace
from main import BackgroundInitManager

async def check():
    manager = BackgroundInitManager()
    load = Mock(return_value=object())
    with patch.dict('sys.modules', {'sentence_transformers': SimpleNamespace(CrossEncoder=load)}):
        with patch('app.utils.config.get_chroma_config', return_value={'reranker': {'enabled': False}}):
            await manager._init_reranker()
            load.assert_not_called()
            assert manager.reorder_service is None
        with patch('app.utils.config.get_chroma_config', return_value={'reranker': {'enabled': True, 'model_name': 'local'}}):
            await manager._init_reranker()
            load.assert_called_once_with('local')
            assert manager.reorder_service is load.return_value

asyncio.run(check())
"""
    subprocess.run([sys.executable, '-c', program], cwd=Path(__file__).resolve().parents[1], check=True, timeout=60)
