/**
 * 笔记分类树静态配置（v2 笔记页三栏，融合 test.md，决策 D3）
 *
 * 树为前端静态结构，不动后端：
 * - 与后端 category 对齐的节点（工作/学习/技术/生活/其他）走 list 接口精确过滤
 * - 静态占位节点（脚本/项目文档/闪念/阅读/三体/Flutter…）本期点击显示空列表，
 *   数量徽标固定 0 —— 后端 category 为自由字符串，将来笔记写入这些分类自然有数据
 * - "所有笔记" 不传 category；"未分类" 本期不进树（后端 list 不支持 null 过滤，决策 M3）
 */

export interface CategoryTreeNode {
  /** 节点唯一标识 */
  key: string;
  /** 显示名 */
  label: string;
  /** 映射后端 category 字段；undefined = 无过滤（all）或静态占位节点 */
  category?: string;
  children?: CategoryTreeNode[];
}

/** 默认展开的节点（顶层 + 有子分类的顶层） */
export const DEFAULT_EXPANDED_KEYS = ['all', 'work', 'tech', 'life'];

export const CATEGORY_TREE: CategoryTreeNode[] = [
  { key: 'all', label: '所有笔记' },
  {
    key: 'work',
    label: '工作',
    category: '工作',
    children: [
      { key: 'work-script', label: '脚本' }, // 静态占位
      {
        key: 'work-docs',
        label: '项目文档',
        children: [{ key: 'work-docs-xxx', label: 'XXX服务_V1.0.06' }], // 静态占位
      },
    ],
  },
  { key: 'study', label: '学习', category: '学习' },
  {
    key: 'tech',
    label: '技术',
    category: '技术',
    children: [
      { key: 'tech-flutter', label: 'Flutter' }, // 静态占位
      { key: 'tech-java', label: 'Java' },
      { key: 'tech-nodejs', label: 'Nodejs' },
      { key: 'tech-vue', label: 'Vue' },
    ],
  },
  {
    key: 'life',
    label: '生活',
    category: '生活',
    children: [
      { key: 'life-handicraft', label: '手工' }, // 静态占位
      { key: 'life-travel', label: '旅游' },
      { key: 'life-parenting', label: '育儿' },
      { key: 'life-recipe', label: '菜谱' },
    ],
  },
  { key: 'flash', label: '闪念' }, // 静态占位
  { key: 'reading', label: '阅读' }, // 静态占位
  { key: 'santi', label: '三体' }, // 静态占位
  { key: 'other', label: '其他', category: '其他' }, // 融合补充：后端兜底语义（not_in 标准分类）
];
