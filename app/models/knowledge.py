"""
知识库文档数据模型

对应 MySQL 表: knowledge_documents
存储用户上传的知识库文档元数据。
向量数据存在 ChromaDB rag_collection 中，通过 document_id 关联。
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, String, DateTime, Integer, ForeignKey, Index, Table, Text, func, true
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class KnowledgeDocument(Base):
    """
    知识库文档模型
    
    存储文档元数据（文件名、路径、大小、类型、MD5 等）。
    向量数据存在 ChromaDB 的 rag_collection 中，通过 user_id metadata 隔离。
    
    关联关系：
    - 多对一 → User（所属用户）
    """
    __tablename__ = "knowledge_documents"
    __table_args__ = {'sqlite_autoincrement': True}
    
    # 主键：自增 ID
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    
    # 所属用户 ID（外键，级联删除）
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.uuid", ondelete="CASCADE"), nullable=False, index=True
    )
    
    # 原始文件名
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    
    # 服务器存储路径
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    
    # 文件大小（字节）
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    
    # 文件类型（MIME 类型，如 application/pdf）
    file_type: Mapped[str] = mapped_column(String(100), nullable=False)
    
    # 文件 MD5 哈希（用于去重）
    md5_hash: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    
    # 切片数量（文档被切分为多少个向量片段）
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ready_for_retrieval: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    source_manifest: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, deferred=True)
    
    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    
    # 更新时间
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )
    
    # ========== 关联关系 ==========
    user = relationship("User", back_populates="knowledge_documents")
    
    def __repr__(self) -> str:
        return f"<KnowledgeDocument(id={self.id}, filename={self.filename})>"


# Rebuildable MySQL keyword projection of the published source manifest.
knowledge_search_chunks = Table(
    'knowledge_search_chunks', Base.metadata,
    Column('document_id', Integer, ForeignKey('knowledge_documents.id', ondelete='CASCADE'), primary_key=True),
    Column('ordinal', Integer, primary_key=True),
    Column('resource_version_id', String(64), nullable=False),
    Column('content', Text().with_variant(LONGTEXT(), 'mysql'), nullable=False),
    Index('ft_knowledge_search_content', 'content', mysql_prefix='FULLTEXT',
          mysql_with_parser='ngram').ddl_if(dialect='mysql'),
    mysql_engine='InnoDB', mysql_charset='utf8mb4',
)
