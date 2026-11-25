"""Run the frozen M3-14 comparison against isolated, locally served models."""
import argparse
import asyncio
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timezone
from hashlib import file_digest
import json
import os
from pathlib import Path
import random
import shutil
import sys
from threading import Condition
from tempfile import NamedTemporaryFile, TemporaryDirectory
from time import perf_counter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ablation_metrics import ARMS, digest, grade_rows, load_frozen
from ablation_environment import OWNER, local_mysql, local_ollama, seeded_indexes
from app.rag.citations import CitationTurn, confirm_citations
from app.rag.query_expansion import InvalidExpansion, validated_queries
from app.rag.rag_service import RagService, RetrievalUnavailable
from app.rag.ranking import FUSION_POLICY
from app.rag.source_spans import hash_text


audit = ContextVar('ablation_audit')
EXPAND_PROMPT = ('只输出 JSON 对象 {"queries":["..."]}。把多方面问题拆为一到两个检索短句，'
                 '保留原问题的主体和修饰条件，按原词拆分，不添加主题、时间、编号或条件。')
ANSWER_PROMPT = ('只据附带的证据回答问题。返回 JSON 对象 {"claims":[{"quote":"原文中连续的一句或短语",'
                 '"citation":"E1"}]}。每个答案要点引用对应证据。找不到直接答案时返回 {"claims":[]}。')


class MeasuredService(RagService):
    def _source_limit(self, top_k):
        # Keep the candidate depth constant when keywords are disabled for arm A.
        return FUSION_POLICY.per_source

    async def _recall(self, *args, **kwargs):
        rows, failed, methods = await super()._recall(*args, **kwargs)
        audit.get()['candidates'] = [(r['chunk_id'], sorted({p['method'] for p in r['provenance']})) for r in rows[:20]]
        return rows, failed, methods


def sha_file(path):
    with path.open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()


class LocalCrossEncoder:
    def __init__(self, weights, tokenizer, expected_sha, restricted):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        if sha_file(weights) != expected_sha:
            raise ValueError('CrossEncoder file does not match the frozen INT8 model')
        self.tokenizer = Tokenizer.from_file(str(tokenizer))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(pad_id=1, pad_token='<pad>')
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(weights), sess_options=options, providers=['CPUExecutionProvider'])
        self.restricted = restricted
        self.workers = Condition()
        self.running = 0

    def predict(self, pairs):
        with self.workers:
            self.running += 1
        try:
            return self._scores(pairs)
        finally:
            with self.workers:
                self.running -= 1
                self.workers.notify_all()

    def drain(self):
        with self.workers:
            self.workers.wait_for(lambda: self.running == 0)

    def _scores(self, pairs):
        import numpy as np
        hits = sum(text in self.restricted for _, text in pairs)
        if hits:
            audit.get()['forbidden_dispatches'] += hits
            raise ValueError('Restricted rerank input')
        encodings = self.tokenizer.encode_batch(pairs)
        fields = {'input_ids':'ids', 'attention_mask':'attention_mask', 'token_type_ids':'type_ids'}
        inputs = {i.name: np.asarray([getattr(e, fields[i.name]) for e in encodings], dtype=np.int64)
                  for i in self.session.get_inputs()}
        logits = self.session.run(None, inputs)[0]
        return logits.reshape(len(pairs)).tolist()


async def experiment(args, directory):
    import chromadb
    from chromadb.config import Settings
    protocol, corpus, questions = load_frozen()
    restricted = {r['text'] for r in corpus.values() if r['state'] != 'active'}
    ranker = LocalCrossEncoder(args.encoder, args.tokenizer, protocol['encoder_sha256'], restricted)
    embed_name, chat_name = protocol['embedding'], protocol['generation']
    async with local_ollama(args.ollama, args.models, directory) as (http, installed):
        model_versions = {name:installed[name] for name in (embed_name, chat_name)}
        async def post(path, body):
            response = await http.post(path, json=body)
            response.raise_for_status()
            return response.json()

        async def embed(texts):
            return (await post('/api/embed', {'model':embed_name,'input':texts,'keep_alive':'30m'}))['embeddings']

        async def generate(prompt, user, tokens):
            response = await post('/api/chat', {'model':chat_name, 'messages':[
                {'role':'system','content':prompt},{'role':'user','content':user}], 'stream':False,
                'think':False, 'format':'json', 'options':{'seed':42, 'temperature':0, 'num_predict':tokens}})
            return response['message']['content']

        async def expand(query, *, max_output_tokens):
            trace = audit.get()
            trace['expansion_attempts'] += 1
            raw = await generate(EXPAND_PROMPT, query, max_output_tokens)
            try:
                validated_queries(query, raw)
            except InvalidExpansion as error:
                if str(error) == 'constraint_changed':
                    trace['drift_rejections'] += 1
            try:
                parsed = json.loads(raw)
                trace['parsed_generations'] += int(isinstance(parsed, dict) and isinstance(parsed.get('queries'), list))
            except (ValueError, TypeError):
                pass
            return raw

        with local_mysql(args.mysqld, directory) as url, chromadb.EphemeralClient(
                settings=Settings(anonymized_telemetry=False)) as chroma:
            async with seeded_indexes(url, chroma, corpus, embed) as (factory, vectors, db_version):
                services = {arm:MeasuredService(vectors, db_session_factory=factory, enable_keyword=arm!='A',
                    rerank_model=ranker if arm in 'CD' else None, expansion_generator=expand if arm=='D' else None)
                    for arm in ARMS}
                await generate(EXPAND_PROMPT, '如何设置打印机和扫描仪？', 32)
                await asyncio.to_thread(ranker.predict, [('打印', '纸张尺寸')])
                async def retrieve(arm, q):
                    trace = {'candidates':[], 'expansion_attempts':0, 'drift_rejections':0,
                             'parsed_generations':0, 'forbidden_dispatches':0}
                    token = audit.set(trace)
                    start = perf_counter()
                    try:
                        result = await services[arm].query(q['query'], OWNER, top_k=protocol['final_k'])
                    except RetrievalUnavailable as error:
                        result = {'status':'error', 'failed_sources':error.failed_sources, 'sources':[], 'context':''}
                    finally:
                        elapsed = (perf_counter() - start) * 1000
                        audit.reset(token)
                    sources = result['sources']
                    blocks = json.loads(result['context'] or '[]')
                    refs = {s['citation_id']:s for s in sources if s.get('citation_id')}
                    turn = CitationTurn(result['context'], sources, delivered=True)
                    expected = turn.cited(' '.join(f"[{ref}]" for ref in refs))
                    async with factory() as db:
                        verified = await confirm_citations(db, OWNER, expected)
                    locators = 0
                    unsafe = 0
                    for block in blocks:
                        source = refs.get(block.get('id'), {})
                        known = corpus.get(source.get('chunk_id'), {})
                        loc = source.get('source_locator', {})
                        unsafe += int(known.get('state') != 'active' or block['text'] in restricted)
                        locators += int(block.get('id') in verified and known.get('text') == block['text']
                                        and loc.get('text_sha256') == hash_text(block['text']))
                    top = trace.pop('candidates')
                    exclusive = Counter(families[0] for key, families in top if key in q['gold'] and len(families)==1)
                    row = {'id':q['id'], 'category':q['category'], 'arm':arm, 'status':result['status'],
                        'failed_sources':result.get('failed_sources', []), 'top20':[key for key,_ in top],
                        'top20_families':dict(top),
                        'final':[s['chunk_id'] for s in sources], 'exclusive':dict(exclusive),
                        'latency_ms':round(elapsed, 3), 'rerank':result.get('rerank_status','unavailable'),
                        'expansion':result.get('query_expansion',{'status':'off'}), 'verified_locators':locators,
                        'locator_total':max(len(blocks), len(sources)), 'external_cny':0, **trace}
                    row['forbidden_dispatches'] += unsafe
                    return row, ([] if unsafe else blocks), {ref:s['chunk_id'] for ref,s in refs.items()}

                order = questions.copy()
                random.Random(protocol['order_seed']).shuffle(order)
                width = protocol['concurrency']
                samples = []
                warm = {'id':'warmup','category':'warmup','query':'试运行服务的配置方法和注意事项有哪些？','gold':{}}
                for arm in ARMS:
                    await asyncio.gather(*(retrieve(arm, warm) for _ in range(width)))
                    await asyncio.to_thread(ranker.drain)
                for offset in range(0, len(order), width):
                    batch = order[offset:offset+width]
                    shift = (offset // width) % len(ARMS)
                    for arm in ARMS[shift:] + ARMS[:shift]:
                        group = await asyncio.gather(*(retrieve(arm, q) for q in batch))
                        samples.extend(zip(batch, group, strict=True))
                        if arm in 'CD':
                            await asyncio.to_thread(ranker.drain)
                    if offset % 24 == 0:
                        print(f'Retrieval {offset + len(batch)}/{len(order)}', flush=True)

                # The answer probe is outside the retrieval timer. Exactly equal question and
                # numbered evidence inputs reuse the same temperature-zero answer across arms.
                answers = {}
                for q, (row, blocks, refs) in samples:
                    prompt = json.dumps({'question':q['query'],'evidence':blocks}, ensure_ascii=False)
                    answers.setdefault(prompt, None)
                prompts = list(answers)
                for offset in range(0, len(prompts), width):
                    batch = prompts[offset:offset+width]
                    values = await asyncio.gather(*(generate(ANSWER_PROMPT, p, 256) for p in batch))
                    answers.update(zip(batch, values, strict=True))
                    if offset % 48 == 0:
                        print(f'Answer probe {offset+len(batch)}/{len(prompts)}', flush=True)
                rows = []
                for q, (row, blocks, refs) in samples:
                    prompt = json.dumps({'question':q['query'],'evidence':blocks}, ensure_ascii=False)
                    try:
                        claims = json.loads(answers[prompt])['claims']
                        if not isinstance(claims, list) or not all(isinstance(c,dict) and isinstance(c.get('quote'),str)
                                and isinstance(c.get('citation'),str) for c in claims):
                            raise ValueError('invalid claims')
                        error = 0
                    except (ValueError, KeyError, TypeError):
                        claims, error = [], 1
                    supported, answered, forbidden = 0, set(), 0
                    evidence_text = {block['id']:block['text'] for block in blocks}
                    for claim in claims:
                        ref, quote = claim['citation'], claim['quote'].strip()
                        key = refs.get(ref)
                        forbidden += int(key is not None and corpus[key]['state'] != 'active')
                        if key in q['gold'] and quote in evidence_text.get(ref,'') and q['gold'][key] in quote:
                            supported += 1
                            answered.add(key)
                    row.update(claims=len(claims), supported_claims=supported, answered_facts=len(answered),
                               gold_facts=len(q['gold']), abstained=not claims and not error and row['status']!='error',
                               answer_error=error, forbidden_citations=forbidden,
                               answer_claims=claims, citation_sources=refs, supported_fact_sources=sorted(answered))
                    rows.append(row)
    return {'milestone':'M3-14', 'completed_at_utc':datetime.now(timezone.utc).isoformat(),
        'samples':len(questions), 'per_category':dict(Counter(q['category'] for q in questions)),
        'protocol':protocol, 'protocol_sha256':digest(protocol), 'model_digests':model_versions,
        'tokenizer_sha256':sha_file(args.tokenizer), 'answer_prompt_sha256':hash_text(ANSWER_PROMPT),
        'expand_prompt_sha256':hash_text(EXPAND_PROMPT), 'answer_probe_unique_inputs':len(answers),
        'index':db_version, **grade_rows(protocol, questions, rows), 'rows':rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mysqld', type=Path, required=True)
    parser.add_argument('--ollama', default=shutil.which('ollama'))
    parser.add_argument('--models', type=Path, default=Path.home()/'.ollama/models')
    parser.add_argument('--encoder', type=Path, required=True)
    parser.add_argument('--tokenizer', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT/'.cache/upgrade-m3-ablation/retrieval/M3-14.json')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=args.output.parent) as temp:
        report = asyncio.run(experiment(args, Path(temp)))
    with NamedTemporaryFile(dir=args.output.parent, suffix='.tmp', delete=False) as staging:
        tmp = Path(staging.name)
    try:
        tmp.write_text(json.dumps(report,ensure_ascii=False, indent=2)+'\n',encoding='utf-8')
        os.replace(tmp, args.output)
    finally:
        tmp.unlink(missing_ok=True)
    print(json.dumps({a:g['eligible'] for a,g in report['gates'].items()}))


if __name__ == '__main__':
    main()
