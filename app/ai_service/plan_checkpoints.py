"""Private checkpoints for plans whose inputs can safely be reconstructed."""

from dataclasses import dataclass, field
import json

import sqlalchemy as sa

from app.ai_service.context_composer import assistant_data
from app.ai_service.detached_chat import background_actions, read_only_chat
from app.ai_service.workflows import ChatWorkflow, database_now, lease_lost
from app.model_config.contracts import content_hash
from app.models.workflow_run import workflow_runs


MAX_PLAN_BYTES = 16 * 1024
MAX_RESULT_CHARS = 500
CHECKPOINT_VERSION = 1


@dataclass
class PlanCheckpoint:
    workflow: ChatWorkflow
    input_hash: str
    data: dict = field(default_factory=dict)

    @classmethod
    async def for_context(cls, ctx):
        workflow = getattr(ctx, 'workflow', None)
        if workflow is None or getattr(workflow, 'attempt', None) is None or not read_only_chat.get():
            return None
        if ctx.attachment_content or ctx.attachment_names:
            return None
        data = json.loads(assistant_data.get() or '{}')
        with_actions = background_actions.get()
        if not isinstance(data, dict) or (not with_actions and any(data.values())):
            return None
        history = []
        for message in ctx.compressed_messages:
            extra = message.additional_kwargs
            if (not isinstance(message.content, str) or getattr(message, 'tool_calls', None)
                    or (extra and (not with_actions or set(extra) - {'raglearn_history'}))):
                return None
            history.append((message.type, message.content))
        inputs = {'version': CHECKPOINT_VERSION, 'question': ctx.user_message, 'system': ctx.system_prompt,
                  'history': history, 'model_role': getattr(ctx.agent_model, 'role_key', None)}
        if with_actions:
            inputs['context'] = data
        key = content_hash(inputs)
        table = workflow_runs
        async with workflow.session_factory() as db:
            row = (await db.execute(sa.select(table.c.plan_checkpoint).where(
                *workflow._active(sa.func.current_timestamp()),
            ))).one_or_none()
        if row is None:
            raise lease_lost()
        stored = row.plan_checkpoint or {}
        return cls(workflow, key, stored if isinstance(stored, dict) and stored.get('input_hash') == key else {})

    async def store_plan(self, plan):
        data = {'input_hash': self.input_hash, 'plan': plan, 'results': {}}
        if len(json.dumps(data, ensure_ascii=False).encode('utf-8')) > MAX_PLAN_BYTES:
            self.data = {}
            return
        async with self.workflow.session_factory() as db:
            async with db.begin():
                if not await self.workflow._checkpoint(db, await database_now(db), plan_checkpoint=data):
                    raise lease_lost()
        self.data = data

    def read_result(self, step, dependencies):
        result = self.data.get('results', {}).get(str(step))
        if isinstance(result, dict) and result.get('dependency_hash') == content_hash(sorted(dependencies.items())):
            return result['text']
        return None

    async def store_result(self, step, result, dependencies):
        if not self.data:
            return
        # The plan event consumer writes results serially, including for parallel steps.
        saved = {'text': result[:MAX_RESULT_CHARS], 'dependency_hash': content_hash(sorted(dependencies.items()))}
        data = {**self.data, 'results': {**self.data['results'], str(step): saved}}
        if len(json.dumps(data, ensure_ascii=False).encode('utf-8')) > MAX_PLAN_BYTES:
            return
        async with self.workflow.session_factory() as db:
            async with db.begin():
                if not await self.workflow._checkpoint(db, await database_now(db), plan_checkpoint=data):
                    raise lease_lost()
        self.data = data
