"""Audit embedded Chroma against MySQL, with optional owner-scoped orphan removal."""

import argparse
import asyncio
import json
from uuid import UUID

from dotenv import load_dotenv


async def _run(args):
    from app.db.database import async_session_factory, close_db
    from app.rag.ingest import queue_source_manifest
    from app.rag.keyword_index import backfill_keyword_index
    from app.rag.maintenance_lock import VectorStoreBusy
    from app.rag.reconcile import reconcile
    from app.utils.config import get_chroma_config

    try:
        if args.backfill_document is not None:
            job = await queue_source_manifest(async_session_factory, args.owner, args.backfill_document)
            print(json.dumps({'queued': job is not None, 'job_id': job}))
            return 0 if job else 1
        if args.backfill_keywords is not None:
            count = await backfill_keyword_index(async_session_factory, args.owner, args.backfill_keywords)
            print(json.dumps({'keyword_chunks': count}))
            return 0
        result = await reconcile(async_session_factory, get_chroma_config(), owner=args.owner, apply=args.apply)
    except VectorStoreBusy:
        print(json.dumps({'error': 'vector_store_busy'}))
        return 2
    except Exception as error:
        print(json.dumps({'error': 'reconciliation_failed', 'error_type': type(error).__name__}))
        return 2
    finally:
        await close_db()
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 2 if result['unresolved_versions'] or any(row['status'] != 'checked' for row in result['collections']) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner', type=UUID, help='Limit findings to one account UUID')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--apply', action='store_true', help='Remove confirmed orphan vectors for --owner')
    actions.add_argument('--backfill-document', type=int, help='Queue source span indexing for an owned legacy document')
    actions.add_argument('--backfill-keywords', type=int, help='Index saved source spans without embedding again')
    args = parser.parse_args()
    if args.apply and args.owner is None:
        parser.error('--apply requires --owner')
    for name, value in (('--backfill-document', args.backfill_document), ('--backfill-keywords', args.backfill_keywords)):
        if value is not None and (args.owner is None or value <= 0):
            parser.error(f'{name} requires --owner and a positive ID')
    args.owner = str(args.owner) if args.owner else None
    load_dotenv()
    return asyncio.run(_run(args))


if __name__ == '__main__':
    raise SystemExit(main())
