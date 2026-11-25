import { useEffect, useState } from 'react';
import { configError, modelConfigApi, type EmbeddingStatus } from '../api/modelConfig';

const reasonName: Record<string, string> = {
  INDEX_BASELINE_UNVERIFIED: '集合基线尚未核验', INDEX_SPACE_UNVERIFIED: '缺少建库模型证据',
  QUERY_SPACE_UNVERIFIED: '缺少查询模型证据', SEMANTIC_IDENTITY_UNVERIFIED: '向量空间证据不完整',
  SEMANTIC_FINGERPRINT_INVALID: '向量空间证据校验失败', EMBEDDING_DIMENSION_MISMATCH: '向量维度不一致',
  EMBEDDING_SPACE_MISMATCH: '建库与查询模型属于不同向量空间',
  QUERY_IDENTITY_PROFILE_MISMATCH: '查询连接与模型修订证据不一致', COMPATIBLE: '空间证据一致',
};

export default function ModelConfigEmbedding({ draftId }: { draftId: string }) {
  const [result, setResult] = useState<EmbeddingStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    modelConfigApi.embeddingStatus(draftId, controller.signal).then(value => {
      if (!controller.signal.aborted) { setResult(value); setLoading(false); }
    }).catch(failure => {
      if (!controller.signal.aborted) { setResult(null); setError(configError(failure).message); setLoading(false); }
    });
    return () => controller.abort();
  }, [draftId, reload]);
  return <section className="mc-policy" aria-labelledby="mc-embedding-heading" aria-busy={loading}>
    <div className="mc-actions"><h2 id="mc-embedding-heading">现有向量索引</h2><button disabled={loading}
      onClick={() => { setError(''); setLoading(true); setReload(value => value + 1); }}>刷新索引状态</button></div>
    {loading ? <p role="status">正在读取集合基线…</p> : error ? <p role="alert" className="mc-error">{error}</p> : result && <>
      <p className="mc-muted">模型名称和维度相同，仍需确认建库时使用的模型修订与处理规则。待核验的集合不能用于已发布的向量检索。</p>
      {!result.items.length ? <p role="status">尚未登记现有集合，请由配置管理员完成本地基线检查与登记。</p> :
        <div className="mc-baseline-grid">{result.items.map(item => <article key={item.id} className="mc-test-result">
          <h3>{item.index_key === 'rag' ? '知识库' : item.index_key === 'notes' ? '笔记' : item.index_key} · v{item.index_version}</h3>
          <p>集合：{item.collection_name}</p><p>观测维度：{item.dimension}；目录记录数：{item.metadata_record_count ?? '未知'}</p>
          <p>{item.semantic_provenance_verified ? '已由管理员核验构建记录' : '待核验：缺少建库模型来源证据'}</p>
          <p className="mc-muted">{item.runtime_usable ? '当前发布使用此索引' : '尚未启用索引查询绑定'}</p>
        </article>)}</div>}
      {result.draft?.embedding_diff.map(item => <p key={item.index_key}>{item.index_key}：{reasonName[item.reason] ?? '此绑定仍需核验'}</p>)}
      {!result.runtime_binding_supported && <p className="mc-info">当前发布仍需停用向量角色。索引登记仅记录观测结果，查询绑定接入完成后才可启用。</p>}
      {result.runtime_binding_supported && <p className="mc-info">首次启用需要管理员核验原始构建记录并绑定草稿，再测试和发布。同空间换密钥或超时会更新查询连接；更换模型空间需等待索引迁移。</p>}
      {!result.migration_supported && <p className="mc-muted">新向量空间的索引重建与切换尚未开放。</p>}
    </>}
  </section>;
}
