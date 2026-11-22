"""Local operator commands for inspecting and rebuilding note index jobs."""

import argparse
import asyncio
import json

from sqlalchemy import select

from app.models.note_index import note_index_jobs as jobs
from app.rag.note_index import rebuild_note_index, retry_failed_note_index


async def run(args, factory):
    async with factory() as db, db.begin():
        if args.command == 'rebuild':
            queued = await rebuild_note_index(db, args.user_id, args.note_id)
            output, exit_code = {'queued': queued}, 0 if queued else 1
        elif args.command == 'retry':
            retried = await retry_failed_note_index(db, args.user_id, args.note_id, args.revision)
            output, exit_code = {'requeued': retried}, 0 if retried else 1
        else:
            rows = await db.execute(select(jobs.c.note_id, jobs.c.revision, jobs.c.operation, jobs.c.attempts,
                                           jobs.c.error_code).where(
                jobs.c.user_id == args.user_id, jobs.c.status == 'failed',
            ).order_by(jobs.c.next_attempt_at, jobs.c.note_id).limit(args.limit))
            output, exit_code = [dict(row) for row in rows.mappings()], 0
    print(json.dumps(output))
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    failed = commands.add_parser('failed')
    failed.add_argument('user_id')
    failed.add_argument('--limit', type=int, choices=range(1, 101), default=25, metavar='1-100')
    retry = commands.add_parser('retry')
    retry.add_argument('user_id')
    retry.add_argument('note_id')
    retry.add_argument('revision')
    rebuild = commands.add_parser('rebuild')
    rebuild.add_argument('user_id')
    rebuild.add_argument('note_id')
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv()
    from app.db.database import async_session_factory, close_db

    async def execute():
        try:
            return await run(args, async_session_factory)
        finally:
            await close_db()

    return asyncio.run(execute())


if __name__ == '__main__':
    raise SystemExit(main())
