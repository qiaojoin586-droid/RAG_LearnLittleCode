"""Frozen local M3-14 evaluation and adoption criteria."""
from collections import Counter
from hashlib import sha256
from math import ceil, log2
import json
from pathlib import Path
from statistics import mean


FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
ARMS = 'ABCD'


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def chunk_id(row):
    return f"{row['id']}_0" if row['source'] == 'knowledge' else f"note_{row['id']}"


def load_frozen():
    def read(name):
        return json.loads((FIXTURES / name).read_text(encoding='utf-8'))
    protocol = read('rag_m3_14_protocol.json')
    data = read('rag_m3_14_ablation.json')
    corpus = {chunk_id(row): row for row in data['corpus']}
    questions = data['questions']
    counts = Counter(q['category'] for q in questions)
    if (digest(data) != protocol['fixture_sha256'] or len(corpus) != len(data['corpus'])
            or len(questions) < protocol['min_questions'] or set(counts) != set(protocol['categories'])
            or min(counts.values()) < protocol['min_per_category']
            or any(len({q[k] for q in questions}) != len(questions) for k in ('id', 'query'))):
        raise ValueError('Invalid frozen dataset')
    for q in questions:
        if bool(q['gold']) == (q['category'] in ('access', 'unanswerable')):
            raise ValueError(q['id'])
        for key, fact in q['gold'].items():
            if corpus[key]['state'] != 'active' or not fact or fact not in corpus[key]['text']:
                raise ValueError(q['id'])
    return protocol, corpus, questions


def score(q, row):
    gold = q['gold']
    ranked = row['final']
    first = next((i for i, key in enumerate(ranked, 1) if key in gold), None)
    gains = [key in gold and key not in ranked[:i] for i, key in enumerate(ranked[:5])]
    def dcg(flags):
        return sum(hit / log2(i + 2) for i, hit in enumerate(flags))
    found = set(row['top20']) & gold.keys()
    return {
        'recall20': len(found) / len(gold) if gold else None,
        'ndcg5': dcg(gains) / dcg([1] * min(5, len(gold))) if gold else None,
        'mrr5': (1 / first if first else 0) if gold else None,
        'relevant_evidence': len(set(ranked) & gold.keys()),
        'duplicates': len(ranked) - len(set(ranked)),
    }


def ratio(a, b):
    return a / b if b else None


def summarize(rows):
    answerable = [r for r in rows if r['recall20'] is not None]
    negative = [r for r in rows if r['recall20'] is None]
    latencies = sorted(r['latency_ms'] for r in rows)
    counts = lambda key: sum(r[key] for r in rows)
    evidence = sum(len(r['final']) for r in rows)
    candidate_count = sum(len(r['top20']) for r in rows)
    shared = sum(len(families) > 1 for r in rows for families in r['top20_families'].values())
    successes = sum(r['status'] != 'error' for r in rows)
    successful_questions = sum(r['status'] != 'error' and not r['answer_error'] and r['supported_claims'] == r['claims'] and
        (r['answered_facts'] == r['gold_facts'] if r['gold_facts'] else r['abstained']) for r in rows)
    modes = Counter(r['expansion'].get('reason', r['expansion']['status']) for r in rows)
    return {
        'questions': len(rows), 'successful_retrievals': successes, 'successful_questions': successful_questions,
        **{key: mean(r[key] for r in answerable) if answerable else None for key in ('recall20','ndcg5','mrr5')},
        'evidence_precision': ratio(counts('relevant_evidence'), evidence),
        'duplicate_rate': ratio(counts('duplicates'), evidence),
        'cross_family_overlap_at20': ratio(shared, candidate_count),
        'locator_accuracy': ratio(counts('verified_locators'), counts('locator_total')),
        'citation_support_rate': ratio(counts('supported_claims'), counts('claims')),
        'answer_fact_recall': ratio(counts('answered_facts'), counts('gold_facts')),
        'answer_abstention_accuracy': ratio(sum(r['abstained'] for r in negative), len(negative)),
        'answer_output_errors': counts('answer_error'),
        'forbidden_dispatches': counts('forbidden_dispatches'), 'forbidden_citations': counts('forbidden_citations'),
        'p50_ms': latencies[ceil(.5 * len(rows))-1], 'p95_ms': latencies[ceil(.95 * len(rows))-1],
        'external_cny_per_success': ratio(counts('external_cny'), successful_questions),
        'reranked_questions': sum(r['rerank'] == 'success' for r in rows),
        'expansion_attempts': counts('expansion_attempts'),
        'expanded_questions': sum(r['expansion']['status'] == 'searched' for r in rows),
        'constraint_drift_rate': ratio(counts('drift_rejections'), counts('parsed_generations')),
        'expansion_outcomes': dict(modes), 'retrieval_statuses': dict(Counter(r['status'] for r in rows)),
        'exclusive_top20_gold_hits': dict(sum((Counter(r['exclusive']) for r in rows), Counter())),
    }


def decide(protocol, summaries):
    verdicts = {}
    for i, arm in enumerate(ARMS):
        s = summaries[arm]
        checks = {
            'complete': s['questions'] >= protocol['min_questions'] and s['successful_retrievals'] == s['questions'],
            'locators': s['locator_accuracy'] == protocol['min_locator_accuracy'],
            'disclosures': s['forbidden_dispatches'] + s['forbidden_citations'] <= protocol['max_forbidden_disclosures'],
            'latency': s['p95_ms'] <= protocol['max_p95_ms'],
            'cost': s['external_cny_per_success'] is not None and s['external_cny_per_success'] <= protocol['max_external_cny_per_success'],
        }
        deltas = {}
        if i:
            prior = summaries[ARMS[i-1]]
            deltas = {key: s[key] - prior[key] for key in ('recall20','ndcg5')}
            checks['recall'] = deltas['recall20'] + 1e-12 >= protocol['min_recall_gain']
            checks['ranking'] = deltas['ndcg5'] + 1e-12 >= protocol['min_ndcg_gain'][arm]
        if arm in 'CD':
            checks['previous_layer_adoptable'] = verdicts[ARMS[i-1]]['eligible']
            checks['enhancement_executed'] = s['reranked_questions' if arm == 'C' else 'expanded_questions'] > 0
        verdicts[arm] = {'eligible': all(checks.values()), 'checks': checks, 'gain_vs_previous': deltas}
    return verdicts


def grade_rows(protocol, questions, rows):
    by_id = {q['id']:q for q in questions}
    pairs = {(r['arm'], r['id']) for r in rows}
    if len(pairs) != len(rows) or pairs != {(a, key) for a in ARMS for key in by_id}:
        raise ValueError('Required question/arm pairs are missing or duplicated')
    for row in rows:
        q = by_id[row['id']]
        if row['category'] != q['category'] or len(row['top20']) > 20 or len(row['final']) > protocol['final_k']:
            raise ValueError(row['id'])
        row.update(score(q, row))
    groups = {a:[r for r in rows if r['arm']==a] for a in ARMS}
    summaries = {a:summarize(group) for a, group in groups.items()}
    gates = decide(protocol, summaries)
    return {'arms':summaries, 'gates':gates, 'assessment_complete':True,
            'approved_quality_and_cost_gates_passed': gates['B']['eligible'] and
                all(v['checks']['locators'] and v['checks']['disclosures'] for v in gates.values()),
            'by_category':{a:{c:summarize([r for r in group if r['category']==c])
                              for c in protocol['categories']} for a,group in groups.items()}}
