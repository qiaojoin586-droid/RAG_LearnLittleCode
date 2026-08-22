/**
 * 笔记内容预览区（v2 笔记页三栏第三栏，本期仅空状态，决策 D4）
 *
 * test.md 规格：未选中笔记时展示灰色线稿风格「文档+铅笔」图标 +
 * 提示文字「选择一个笔记开始查看」，内容垂直水平居中。
 *
 * 本期点击笔记卡片跳转 /notes/:id 编辑器；
 * 预留容器：后续版本在此内嵌只读渲染（react-markdown + .md-prose 现成样式）。
 */

import { useTranslation } from 'react-i18next';
import { PenLine } from 'lucide-react';

export default function NotePreviewPanel() {
  const { t } = useTranslation();

  return (
    /* 空状态在父容器内水平+垂直居中：
       本组件是父容器（flex）的直接子项，flex-1 占满宽度、默认 stretch 占满高度，
       不依赖 h-full 百分比高度（在 flex 嵌套链中可能解析失败导致垂直未居中） */
    <div className="flex-1 flex flex-col items-center justify-center text-center px-6">
      {/* 灰色线稿图标：文档 + 铅笔 */}
      <div className="relative mb-4">
        <PenLine size={48} className="text-[var(--color-text-tertiary)] opacity-50" />
      </div>
      <p className="text-sm text-[var(--color-text-tertiary)]">{t('note.selectToView')}</p>
    </div>
  );
}
