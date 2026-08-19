/**
 * 关于页面
 * 
 * 展示项目介绍、技术栈信息、版本信息等。
 */

import { BookOpen, Brain, Database, Sparkles } from 'lucide-react';

export default function AboutUs() {
  return (
    <div className="max-w-2xl mx-auto p-6">
      <h1 className="text-2xl font-heading font-bold text-[var(--color-text)] mb-6">
        关于
      </h1>

      {/* 项目简介 */}
      <section className="p-6 bg-[var(--color-card)] rounded-[var(--radius-lg)] border border-[var(--color-border)] mb-6">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-10 h-10 rounded-[var(--radius-md)] bg-[var(--color-accent)] flex items-center justify-center">
            <BookOpen size={20} className="text-white" />
          </div>
          <div>
            <h2 className="text-lg font-bold text-[var(--color-text)]">RAG LearnLittle</h2>
            <p className="text-xs text-[var(--color-text-tertiary)]">智能笔记助手 v1.0.0</p>
          </div>
        </div>
        <p className="text-sm text-[var(--color-text-secondary)] leading-relaxed">
          RAG LearnLittle 是一款基于 RAG（检索增强生成）技术的智能笔记应用。
          它结合了笔记管理、AI 对话、知识库和艾宾浩斯遗忘曲线回顾等功能，
          帮助用户高效记录、组织和回顾知识，让学习更加科学、智能。
        </p>
      </section>

      {/* 核心功能 */}
      <section className="p-6 bg-[var(--color-card)] rounded-[var(--radius-lg)] border border-[var(--color-border)] mb-6">
        <h2 className="text-base font-medium text-[var(--color-text)] mb-4">核心功能</h2>
        <div className="grid grid-cols-2 gap-4">
          <FeatureItem
            icon={<BookOpen size={18} className="text-blue-500" />}
            title="智能笔记"
            desc="富文本编辑、标签分类、语义搜索"
          />
          <FeatureItem
            icon={<Sparkles size={18} className="text-purple-500" />}
            title="AI 对话"
            desc="基于笔记和知识库的智能问答"
          />
          <FeatureItem
            icon={<Database size={18} className="text-green-500" />}
            title="知识库"
            desc="文档上传、自动切片、向量化存储"
          />
          <FeatureItem
            icon={<Brain size={18} className="text-orange-500" />}
            title="每日回顾"
            desc="艾宾浩斯遗忘曲线科学复习"
          />
        </div>
      </section>

      {/* 技术栈 */}
      <section className="p-6 bg-[var(--color-card)] rounded-[var(--radius-lg)] border border-[var(--color-border)]">
        <h2 className="text-base font-medium text-[var(--color-text)] mb-4">技术栈</h2>
        <div className="space-y-3">
          <TechGroup label="后端" items={['FastAPI', 'SQLAlchemy', 'ChromaDB', 'Redis', 'LangChain']} />
          <TechGroup label="前端" items={['React 19', 'TypeScript', 'Vite 6', 'Tailwind CSS', 'Zustand']} />
          <TechGroup label="AI" items={['OpenAI API', 'Sentence Transformers', 'Tiptap Editor']} />
        </div>
      </section>
    </div>
  );
}

/** 功能特性卡片 */
function FeatureItem({ icon, title, desc }: { icon: React.ReactNode; title: string; desc: string }) {
  return (
    <div className="flex items-start gap-3 p-3 rounded-[var(--radius-md)] bg-[var(--color-bg)]">
      <div className="mt-0.5 shrink-0">{icon}</div>
      <div>
        <h3 className="text-sm font-medium text-[var(--color-text)]">{title}</h3>
        <p className="text-xs text-[var(--color-text-tertiary)] mt-0.5">{desc}</p>
      </div>
    </div>
  );
}

/** 技术栈分组展示 */
function TechGroup({ label, items }: { label: string; items: string[] }) {
  return (
    <div className="flex items-center gap-3">
      <span className="text-xs text-[var(--color-text-tertiary)] w-8 shrink-0">{label}</span>
      <div className="flex flex-wrap gap-1.5">
        {items.map((item) => (
          <span
            key={item}
            className="px-2 py-0.5 text-xs rounded-full bg-[var(--color-accent-bg)] text-[var(--color-accent)] border border-[var(--color-accent)]/20"
          >
            {item}
          </span>
        ))}
      </div>
    </div>
  );
}
