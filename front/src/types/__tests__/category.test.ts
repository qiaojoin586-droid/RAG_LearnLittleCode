/**
 * 分类类型工具函数测试（无外部依赖，纯函数）
 *
 * 覆盖：flattenCategories / findCategory / isDescendant / UNCATEGORIZED_SENTINEL
 */

import { describe, it, expect } from 'vitest';
import {
  UNCATEGORIZED_SENTINEL,
  flattenCategories,
  findCategory,
  isDescendant,
  type Category,
} from '../category';

/** 测试树：工作(脚本, 项目文档(XXX服务)) / 学习 */
function makeTree(): Category[] {
  const leaf = (id: string, name: string): Category => ({
    id, name, parent_id: null, sort_order: 0,
    created_at: '', updated_at: '', children: [], note_count: 0,
  });
  return [
    {
      ...leaf('work', '工作'),
      parent_id: null,
      children: [
        leaf('script', '脚本'),
        { ...leaf('docs', '项目文档'), children: [leaf('xxx', 'XXX服务')] },
      ],
    },
    leaf('study', '学习'),
  ];
}

describe('UNCATEGORIZED_SENTINEL', () => {
  it('与后端约定保持一致', () => {
    expect(UNCATEGORIZED_SENTINEL).toBe('__uncategorized__');
  });
});

describe('flattenCategories', () => {
  it('按先父后子顺序输出并标记深度', () => {
    const flat = flattenCategories(makeTree());
    expect(flat.map((f) => f.category.id)).toEqual(['work', 'script', 'docs', 'xxx', 'study']);
    expect(flat.map((f) => f.depth)).toEqual([0, 1, 1, 2, 0]);
  });

  it('空树返回空数组', () => {
    expect(flattenCategories([])).toEqual([]);
  });
});

describe('findCategory', () => {
  it('能递归找到嵌套节点', () => {
    const found = findCategory(makeTree(), 'xxx');
    expect(found?.name).toBe('XXX服务');
  });

  it('未找到返回 null', () => {
    expect(findCategory(makeTree(), 'not-exist')).toBeNull();
  });
});

describe('isDescendant', () => {
  it('能识别子孙关系', () => {
    const tree = makeTree();
    expect(isDescendant(tree, 'work', 'script')).toBe(true);
    expect(isDescendant(tree, 'work', 'xxx')).toBe(true);
  });

  it('非子孙关系返回 false', () => {
    const tree = makeTree();
    expect(isDescendant(tree, 'work', 'study')).toBe(false);
    expect(isDescendant(tree, 'script', 'xxx')).toBe(false);
    expect(isDescendant(tree, 'work', 'work')).toBe(false);
  });
});
