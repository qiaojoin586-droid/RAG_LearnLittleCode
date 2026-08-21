# -*- coding: utf-8 -*-
"""
向量库维度迁移脚本：1024 维（text-embedding-v3 时代）→ 2560 维（qwen3.7-text-embedding）

背景（2026-08-22 BUG）：
- ChromaDB 两个集合（rag_collection / notes_collection）建库维度为 1024
- .env 的 DASHSCOPE_EMBED_MODEL 已切换为 qwen3.7-text-embedding（输出 2560 维；
  text-embedding-v3 免费额度已耗尽 403，无法回退）
- 导致 search_notes_tool / RAG 检索抛 InvalidArgumentError（1024 vs 2560）

迁移原理（无损）：
- ChromaDB 持久化了每条向量的原文（#document），可直接读出重新嵌入
- 步骤：读全量（ids/documents/metadatas）→ 删除集合 → 重建（首条 upsert 自动定维 2560）
  → 分批（20 条/批）重嵌入 → 回填原 id 与 metadata
- 维度由首次 upsert 自然确定为 2560，无需显式指定

用法：
  1. （推荐）暂停后端服务，避免 SQLite 文件锁冲突
  2. python scripts/manual/migrate_embeddings_2560.py
  3. 校验输出中的 count 与 dimension，然后重启后端
"""

import os
import sys
import time

import dotenv

# 允许从项目根运行：注入项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

dotenv.load_dotenv()

import chromadb
from chromadb.config import Settings
from langchain_community.embeddings import DashScopeEmbeddings

from app.utils.config import get_chroma_config

BATCH_SIZE = 20  # 与 vector_store.EMBEDDING_BATCH_SIZE 一致


def main() -> None:
    # Windows 控制台默认 GBK：强制 UTF-8 输出，避免 emoji/中文编码错误
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cfg = get_chroma_config()
    persist_dir = cfg.get("persist_directory", "data/chroma")
    collections = cfg.get("collections", {})
    names = [collections.get("rag", "rag_collection"), collections.get("notes", "notes_collection")]

    model_name = os.getenv("DASHSCOPE_EMBED_MODEL", "qwen3.7-text-embedding")
    api_key = os.getenv("DASHSCOPE_API_KEY", "")
    if not api_key:
        print("[ERROR] DASHSCOPE_API_KEY 未配置")
        sys.exit(1)
    print(f"Embedding 模型: {model_name} | 持久化目录: {persist_dir}")

    embed_model = DashScopeEmbeddings(model=model_name, dashscope_api_key=api_key)
    client = chromadb.PersistentClient(path=persist_dir, settings=Settings(anonymized_telemetry=False))

    for name in names:
        try:
            old = client.get_collection(name)
        except Exception:
            print(f"[SKIP] 集合 {name} 不存在，跳过")
            continue

        # 1) 读出全量原文与元数据
        data = old.get(include=["documents", "metadatas"])
        ids = data.get("ids") or []
        docs = data.get("documents") or []
        metas = data.get("metadatas") or []
        total = len(ids)
        if total == 0:
            print(f"[SKIP] 集合 {name} 为空，跳过")
            continue
        print(f"[OK] {name}: 共 {total} 条，开始迁移…")

        # 2) 删除旧集合并重建（维度由首次 upsert 定为 2560）
        client.delete_collection(name)
        new = client.create_collection(name, metadata={"hnsw:space": "cosine"})

        # 3) 分批重嵌入并回填（保留原 id 与 metadata）
        for start in range(0, total, BATCH_SIZE):
            end = min(start + BATCH_SIZE, total)
            batch_docs = [d or "" for d in docs[start:end]]
            embeddings = embed_model.embed_documents(batch_docs)
            new.add(
                ids=ids[start:end],
                embeddings=embeddings,
                documents=batch_docs,
                metadatas=metas[start:end],
            )
            print(f"  {name}: {end}/{total}")
            time.sleep(0.2)  # 轻量限速，避免 API 限流

        print(f"[DONE] {name} 迁移完成: {new.count()} 条（原 {total} 条）")

    print("迁移结束。请重启后端验证语义搜索。")


if __name__ == "__main__":
    main()
