/**
 * 笔记文档类型选项（文档类型功能，2026-08-21）
 *
 * 扩展位：后续新增文档类型只需在此数组追加元素，
 * 并在编辑器/徽章处增加对应分支，无需改表结构（后端 format 为自由字符串）。
 */

import type { NoteFormat } from '../utils/noteFormat';

export const NOTE_FORMATS: { value: NoteFormat; labelKey: string }[] = [
  { value: 'md', labelKey: 'note.format.md' },
  { value: 'txt', labelKey: 'note.format.txt' },
];
