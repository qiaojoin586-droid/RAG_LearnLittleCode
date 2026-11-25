/**
 * 知识库管理页面
 * 
 * 功能：
 * 1. 拖拽 / 点击上传文档并查看持久任务进度
 * 2. 文档列表展示 + 删除
 * 3. 文档详情抽屉（查看切片信息）
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';
import { Upload, FileText, Trash2, CheckCircle, AlertCircle } from 'lucide-react';
import { knowledgeApi } from '../api/knowledge';
import EmptyState from '../components/common/EmptyState';
import ConfirmDialog from '../components/common/ConfirmDialog';
import type { KnowledgeDocument } from '../types/api';
import { useKnowledgeUploads } from '../hooks/useKnowledgeUploads';

const uploadStages: Record<string, string> = {
  preparing: '准备上传', queued: '等待入库', parsing: '解析文档',
  splitting: '文本切片', vectorizing: '写入索引', complete: '完成',
};

export default function KnowledgeBase() {
  const { t } = useTranslation();

  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [deleteId, setDeleteId] = useState<number | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const documentRequest = useRef(0);

  const refreshDocuments = useCallback(async (signal: AbortSignal) => {
    const request = ++documentRequest.current;
    try {
      const response = await knowledgeApi.listDocuments(signal);
      if (!signal.aborted && request === documentRequest.current) {
        setDocuments(response.data.data.documents);
      }
    } catch { /* Upload progress is read from the durable job. */ }
    finally {
      if (!signal.aborted && request === documentRequest.current) setLoading(false);
    }
  }, []);
  const { uploads, uploadFile, clearCompleted } = useKnowledgeUploads(refreshDocuments);

  useEffect(() => {
    const load = new AbortController();
    void refreshDocuments(load.signal);
    return () => load.abort();
  }, [refreshDocuments]);

  /** 文件选择回调 */
  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;
    Array.from(files).forEach(uploadFile);
    // 清空 input 以允许重复选择同一文件
    e.target.value = '';
  };

  /** 拖拽放置处理 */
  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const files = e.dataTransfer.files;
    if (files.length > 0) {
      Array.from(files).forEach(uploadFile);
    }
  };

  /** 删除文档 */
  const handleDelete = async () => {
    if (deleteId === null) return;
    try {
      await knowledgeApi.deleteDocument(deleteId);
      documentRequest.current++;
      setDocuments((previous) => previous.filter((d) => d.id !== deleteId));
      toast.success('已删除');
    } catch {
      toast.error(t('common.error'));
    }
    setDeleteId(null);
  };

  /** 格式化文件大小 */
  const formatSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  return (
    <div className="p-6">
      <h1 className="text-2xl font-heading font-bold text-[var(--color-text)] mb-6">
        {t('nav.knowledge')}
      </h1>

      {/* 上传区域（支持拖拽） */}
      <div
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        onClick={() => fileInputRef.current?.click()}
        className={`
          relative border-2 border-dashed rounded-[var(--radius-lg)] p-8 text-center cursor-pointer
          transition-colors mb-6
          ${dragOver
            ? 'border-[var(--color-accent)] bg-[var(--color-accent-bg)]'
            : 'border-[var(--color-border)] hover:border-[var(--color-accent)] hover:bg-[var(--color-accent-bg)]'
          }
        `}
      >
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept=".pdf,.txt,.md,.markdown"
          onChange={handleFileChange}
          className="hidden"
        />
        <Upload size={36} className="mx-auto mb-3 text-[var(--color-text-tertiary)]" />
        <p className="text-sm text-[var(--color-text-secondary)] mb-1">
          {t('knowledge.dragHint')}
        </p>
        <p className="text-xs text-[var(--color-text-tertiary)]">
          {t('knowledge.supportFormats')}
        </p>
      </div>

      {/* 上传进度列表 */}
      {uploads.length > 0 && (
        <div className="mb-6 space-y-2">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-medium text-[var(--color-text)]">上传进度</span>
            <button
              onClick={clearCompleted}
              className="text-xs text-[var(--color-text-tertiary)] hover:text-[var(--color-text)]"
            >
              清除已完成
            </button>
          </div>
          {uploads.map((upload) => (
            <div
              key={upload.id}
              className="flex items-center gap-3 p-3 bg-[var(--color-card)] rounded-[var(--radius-md)] border border-[var(--color-border)]"
            >
              {/* 状态图标 */}
              {upload.status === 'completed' ? (
                <CheckCircle size={18} className="text-green-500 shrink-0" />
              ) : upload.status === 'error' ? (
                <AlertCircle size={18} className="text-[var(--color-danger)] shrink-0" />
              ) : (
                <FileText size={18} className="text-[var(--color-accent)] shrink-0 animate-pulse" />
              )}

              {/* 文件名 + 进度条 */}
              <div className="flex-1 min-w-0">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-sm text-[var(--color-text)] truncate">
                    {upload.filename}
                  </span>
                  <span role="status" className="text-xs text-[var(--color-text-tertiary)] ml-2">
                    {upload.status === 'error' ? upload.message : upload.stage === 'reconnecting'
                      ? t('knowledge.reconnecting')
                      : `${upload.progress}% · ${uploadStages[upload.stage] ?? upload.stage}`}
                  </span>
                </div>
                <div className="w-full h-1.5 bg-[var(--color-border)] rounded-full overflow-hidden">
                  <div
                    className={`h-full rounded-full transition-all duration-300 ${
                      upload.status === 'error'
                        ? 'bg-[var(--color-danger)]'
                        : upload.status === 'completed'
                          ? 'bg-green-500'
                          : 'bg-[var(--color-accent)]'
                    }`}
                    style={{ width: `${upload.progress}%` }}
                  />
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 文档列表 */}
      <h2 className="text-lg font-medium text-[var(--color-text)] mb-3">
        {t('knowledge.documents')}
      </h2>

      {loading ? (
        <p className="text-center text-[var(--color-text-tertiary)]">{t('common.loading')}</p>
      ) : documents.length === 0 ? (
        <EmptyState icon={<FileText size={48} />} title={t('knowledge.empty')} />
      ) : (
        <div className="space-y-2">
          {documents.map((doc) => (
            <div
              key={doc.id}
              className="flex items-center justify-between p-4 bg-[var(--color-card)] rounded-[var(--radius-md)] border border-[var(--color-border)] hover:shadow-card"
            >
              <div className="flex items-center gap-3 min-w-0">
                <FileText size={20} className="text-[var(--color-accent)] shrink-0" />
                <div className="min-w-0">
                  <h3 className="text-sm font-medium text-[var(--color-text)] truncate">
                    {doc.filename}
                  </h3>
                  <p className="text-xs text-[var(--color-text-tertiary)] mt-0.5">
                    {formatSize(doc.file_size)} · {doc.chunk_count} 个切片 · {doc.file_type}
                  </p>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <span className="text-xs text-[var(--color-text-tertiary)]">
                  {new Date(doc.created_at).toLocaleDateString()}
                </span>
                <button
                  onClick={() => setDeleteId(doc.id)}
                  className="p-1.5 text-[var(--color-text-tertiary)] hover:text-[var(--color-danger)]"
                >
                  <Trash2 size={15} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 删除确认弹窗 */}
      <ConfirmDialog
        open={!!deleteId}
        title="确认删除"
        description="删除后文档及其所有切片数据将永久丢失，不可恢复。"
        confirmLabel="删除"
        variant="danger"
        onConfirm={handleDelete}
        onCancel={() => setDeleteId(null)}
      />
    </div>
  );
}
