"""
数据模型包初始化

统一导出所有 ORM 模型，方便其他模块引用。
"""

from app.models.base import Base
from app.models.user import User
from app.models.note import Note
from app.models.category import NoteCategory
from app.models.chat import ChatSession, ChatMessage
from app.models.review import ReviewRecord
from app.models.note_template import NoteTemplate
from app.models.ppt_template import PptTemplate
from app.models.knowledge import KnowledgeDocument
from app.models.knowledge_ingest import knowledge_ingest_jobs
from app.models.note_index import note_index_jobs
from app.models.index_build import index_builds
from app.models.model_trace import ModelTrace, ModelPricing
from app.models.tool_audit import ToolCallAudit
from app.models import tool_operation as _tool_operation
from app.model_config import tables as _model_config_tables  # Register M1 Core tables.
from app.user_memory import tables as _user_memory_tables
from app.models import workflow_run as _workflow_run
from app.models import workflow_event as _workflow_event
from app.models import operation_approval as _operation_approval
from app.models import email_dispatch as _email_dispatch
from app.models import workflow_model_budget as _workflow_model_budget

__all__ = [
    "Base",
    "User",
    "Note",
    "NoteCategory",
    "ChatSession",
    "ChatMessage",
    "ReviewRecord",
    "NoteTemplate",
    "KnowledgeDocument",
    "ModelTrace",
    "ModelPricing",
    "ToolCallAudit",
]
