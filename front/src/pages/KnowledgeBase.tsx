/**
 * 知识库管理页面
 * 
 * 功能：
 * 1. 拖拽 / 点击上传文档（SSE 实时进度条）
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
import type { KnowledgeDocument, KnowledgeSSEMessage } from '../types/api';

/** 上传文件进度状态 */
interface UploadProgress {
  filename: string;
  progress: number;
  stage: string;
  status: 'uploading' | 'completed' | 'error';
  message?: string;
}

export default function KnowledgeBase() {
  const { t } = useTranslation();

  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploads, setUploads] = useState<UploadProgress[]>([]);
  const [deleteId, setDeleteId] = useState<number | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  /** 加载文档列表 */
  useEffect(() => {
    (async () => {
      try {
        const res = await knowledgeApi.listDocuments();
        setDocuments(res.data.data.documents);
      } catch { /* ignore */ }
      finally { setLoading(false); }
    })();
  }, []);

  /**
   * 处理文件上传（SSE 流式进度）
   * 使用原生 fetch 读取 SSE 事件流，实时更新进度条
   */
  const handleFileUpload = useCallback(async (file: File) => {
    const newUpload: UploadProgress = {
      filename: file.name,
      progress: 0,
      stage: '准备上传',
      status: 'uploading',
    };
    setUploads((prev) => [...prev, newUpload]);

    try {
      const response = await knowledgeApi.upload(file);
      const reader = response.body?.getReader();
      if (!reader) throw new Error('无法读取响应流');

      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        // 按行解析 SSE 事件
        const lines = buffer.split('\n');
        buffer = lines.pop() || ''; // 最后一行可能不完整，保留

        for (const line of lines) {
          if (!line.startsWith('data:')) continue;
          const jsonStr = line.slice(5).trim();
          if (!jsonStr) continue;

          try {
            const event: KnowledgeSSEMessage = JSON.parse(jsonStr);

            if (event.event_type === 'processing') {
              setUploads((prev) =>
                prev.map((u) =>
                  u.filename === file.name
                    ? { ...u, progress: event.progress || 0, stage: event.stage || '' }
                    : u
                )
              );
            } else if (event.event_type === 'completed') {
              setUploads((prev) =>
                prev.map((u) =>
                  u.filename === file.name
                    ? { ...u, progress: 100, status: 'completed', stage: '完成' }
                    : u
                )
              );
              // 刷新文档列表
              const res = await knowledgeApi.listDocuments();
              setDocuments(res.data.data.documents);
            } else if (event.event_type === 'error') {
              setUploads((prev) =>
                prev.map((u) =>
                  u.filename === file.name
                    ? { ...u, status: 'error', message: event.message || '上传失败' }
                    : u
                )
              );
            } else if (event.event_type === 'finish') {
              // 所有文件处理完毕
              break;
            }
          } catch {
            // 忽略 JSON 解析错误
          }
        }
      }
    } catch (err) {
      setUploads((prev) =>
        prev.map((u) =>
          u.filename === file.name
            ? { ...u, status: 'error', message: err instanceof Error ? err.message : '上传失败' }
            : u
        )
      );
    }
  }, []);

  /** 文件选择回调 */
  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;
    Array.from(files).forEach(handleFileUpload);
    // 清空 input 以允许重复选择同一文件
    e.target.value = '';
  };

  /** 拖拽放置处理 */
  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const files = e.dataTransfer.files;
    if (files.length > 0) {
      Array.from(files).forEach(handleFileUpload);
    }
  };

  /** 删除文档 */
  const handleDelete = async () => {
    if (deleteId === null) return;
    try {
      await knowledgeApi.deleteDocument(deleteId);
      setDocuments(documents.filter((d) => d.id !== deleteId));
      toast.success('已删除');
    } catch {
      toast.error(t('common.error'));
    }
    setDeleteId(null);
  };

  /** 清除已完成的上传记录 */
  const clearCompleted = () => {
    setUploads((prev) => prev.filter((u) => u.status === 'uploading'));
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
          {uploads.map((upload, idx) => (
            <div
              key={idx}
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
                  <span className="text-xs text-[var(--color-text-tertiary)] ml-2">
                    {upload.status === 'error' ? upload.message : `${upload.progress}% · ${upload.stage}`}
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
