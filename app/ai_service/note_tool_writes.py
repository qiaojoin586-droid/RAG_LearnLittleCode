"""Shared note and review mutations for transactional tools and user confirmation."""

from pydantic import ValidationError
from sqlalchemy import select

from app.core.failed_response import BusinessError, ErrorCode
from app.core.logger_handler import logger
from app.model_config.contracts import content_hash
from app.models.category import NoteCategory
from app.models.note import Note
from app.models.review import ReviewRecord
from app.schemas.note import NoteCreate, NoteUpdate


TRANSACTIONAL_TOOLS = frozenset({'create_note_tool', 'update_note_tool', 'mark_reviewed_tool'})


async def resolve_category(db, user_id, name, *, lock=False):
    if not name:
        return {'id': None, 'name': None}
    query = select(NoteCategory.id, NoteCategory.name).where(
        NoteCategory.user_id == user_id, NoteCategory.name == name, NoteCategory.deleted_at.is_(None),
    )
    if lock:
        query = query.with_for_update()
    categories = (await db.execute(query.limit(2))).all()
    if len(categories) > 1:
        raise BusinessError(ErrorCode.INVALID_PARAMETER, message='同名分类有多个候选，请明确分类', http_status=409)
    category = categories[0] if categories else None
    return {'id': category.id, 'name': category.name} if category else {'id': None, 'name': name}


async def read_target(db, user_id, name, arguments):
    """Resolve the exact owned target, with locks held until the decision commits."""
    target = {}
    if name == 'mark_reviewed_tool':
        query = select(ReviewRecord, Note.title).join(Note, Note.id == ReviewRecord.note_id).where(
            ReviewRecord.id == arguments['review_id'], ReviewRecord.user_id == user_id,
            Note.user_id == user_id, Note.deleted_at.is_(None),
        ).with_for_update()
        row = (await db.execute(query)).one_or_none()
        if row is None:
            raise BusinessError(ErrorCode.NOTE_NOT_FOUND, message='回顾记录不存在', http_status=404)
        review, title = row
        return {'id': review.id, 'title': title, 'note_id': review.note_id, 'version': content_hash({
            'count': review.review_count, 'interval': review.interval_days, 'quality': review.quality,
            'next': review.next_review_at.isoformat(), 'last': review.reviewed_at.isoformat() if review.reviewed_at else None,
        })}
    if name == 'update_note_tool':
        query = select(Note).where(Note.id == arguments['note_id'], Note.user_id == user_id,
                                   Note.deleted_at.is_(None)).with_for_update()
        note = (await db.execute(query)).scalar_one_or_none()
        if note is None:
            raise BusinessError(ErrorCode.NOTE_NOT_FOUND, http_status=404)
        target = {'id': note.id, 'title': note.title, 'version': content_hash({
            'title': note.title, 'content': note.content, 'tags': note.tags,
            'category': note.category, 'category_id': note.category_id,
            'updated_at': note.updated_at.isoformat(),
        })}
    if arguments.get('category') is not None or name == 'create_note_tool':
        target['category'] = await resolve_category(db, user_id, arguments.get('category'), lock=True)
    try:
        if name == 'create_note_tool':
            NoteCreate(**arguments)
        else:
            NoteUpdate(**{key: value for key, value in arguments.items() if key != 'note_id'})
    except ValidationError:
        raise BusinessError(ErrorCode.INVALID_PARAMETER, message='笔记修改参数无效', http_status=422) from None
    return target


async def apply_write(db, user_id, name, arguments, after_commit, note_service, review_service, *, target=None):
    if name == 'mark_reviewed_tool':
        review = await review_service.mark_reviewed(db, arguments['review_id'], user_id)
        next_date = review.next_review_at.date().isoformat()
        return f'回顾完成！下次回顾时间: {next_date}', {'review_id': review.id, 'next_date': next_date}

    values = {key: value for key, value in arguments.items() if key not in ('note_id', 'category')}
    if arguments.get('category') is not None or name == 'create_note_tool':
        category = target['category'] if target is not None else await resolve_category(db, user_id, arguments.get('category'))
        values.update(category=category['name'], category_id=category['id'])
    if name == 'create_note_tool':
        note = await note_service.create_note(db, user_id, NoteCreate(**values), after_commit=after_commit)
        return f'笔记创建成功！ID: {note.id}, 标题: {note.title}', {'note_id': note.id}
    if name == 'update_note_tool':
        note = await note_service.update_note(db, arguments['note_id'], user_id, NoteUpdate(**values))
        return f'笔记更新成功！ID: {note.id}, 标题: {note.title}', {'note_id': note.id}
    raise ValueError('Unknown transactional tool')


async def replay_write(db, user_id, name, reference, note_service):
    if name == 'mark_reviewed_tool':
        review = await db.scalar(select(ReviewRecord.id).where(
            ReviewRecord.id == reference['review_id'], ReviewRecord.user_id == user_id,
        ))
        if review is None:
            raise BusinessError(ErrorCode.NOTE_NOT_FOUND, message='回顾记录不存在', http_status=404)
        return f"回顾完成！下次回顾时间: {reference['next_date']}"
    note = await note_service.get_note(db, reference['note_id'], user_id)
    action = '创建' if name == 'create_note_tool' else '更新'
    return f'笔记{action}成功！ID: {note.id}, 标题: {note.title}'


async def run_after_commit(name, effects):
    for effect in effects:
        try:
            await effect()
        except Exception as error:
            logger.warning(f'已提交的工具操作后处理失败: tool={name}, error_type={type(error).__name__}')
