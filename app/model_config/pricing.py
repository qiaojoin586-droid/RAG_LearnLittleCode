"""Append-only flat token rates and trace-time price attribution."""
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import re

import sqlalchemy as sa

from app.model_config.contracts import content_hash
from app.model_config.endpoints import canonical_url
from app.model_config.errors import fail
from app.model_config.security import Principal, SCOPE, scoped
from app.model_config.service import json_view, uid, utcnow
from app.model_config.tables import ai_profile_revisions, model_price_versions


MONEY_SCALE = Decimal('0.0000000001')
MONEY_LIMIT = Decimal('10000000000')


def parse_rate(value):
    if not re.fullmatch(r'(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,10})?', value):
        fail(40002, 'PRICE_RATE_INVALID', 422)
    return Decimal(value).quantize(MONEY_SCALE)


async def register_price(control, db, user_id, *, origin, model, currency, unit_tokens,
                         input_price, output_price, source, request_key):
    normalized, path = canonical_url(origin)
    if path != '/' or len(normalized) > 320:
        fail(40002, 'PRICE_ORIGIN_REQUIRED', 422)
    if not 1 <= len(model) <= 128 or model.strip() != model or not model.isprintable():
        fail(40002, 'PRICE_MODEL_INVALID', 422)
    if not re.fullmatch(r'[A-Z]{3}', currency) or not 1 <= unit_tokens <= 1_000_000_000:
        fail(40002, 'PRICE_UNIT_INVALID', 422)
    if not re.fullmatch(r'[a-zA-Z0-9._:-]{1,55}', source):
        fail(40002, 'PRICE_SOURCE_INVALID', 422)
    input_rate, output_rate = parse_rate(input_price), parse_rate(output_price)
    body = {'origin': normalized, 'model': model, 'currency': currency, 'unit_tokens': unit_tokens,
            'input_unit_price': format(input_rate, 'f'), 'output_unit_price': format(output_rate, 'f'),
            'provenance': f'operator:{source}'}
    actor = Principal(user_id, None, utcnow() + timedelta(seconds=30))
    table = model_price_versions

    async def operation(head):
        query = sa.select(table.c.origin, table.c.model, table.c.price_version).where(
            scoped(table), table.c.origin == normalized, table.c.model == model)
        previous = (await db.execute(query.order_by(table.c.price_version.desc()).limit(1))).first()
        if previous and (previous.origin != normalized or previous.model != model):
            fail(40923, 'PRICE_IDENTITY_COLLISION')
        version = previous.price_version + 1 if previous else 1
        now = await control._now(db)
        identifier = uid()
        row = {'id': identifier, **body, 'price_version': version, 'effective_from': now}
        await db.execute(table.insert().values(**{**row, 'input_unit_price': input_rate,
                    'output_unit_price': output_rate}, **SCOPE, created_at=now))
        return row, identifier, json_view(row)

    target = content_hash([normalized, model])
    return await control._mutate(db, actor, 'price.register', 'config.publish', target,
                                 body, request_key, operation, http_status=201)


def trace_row(event):
    row = dict(event)
    observed = row.pop('_price_at', None)
    if observed:
        row['created_at'] = datetime.fromisoformat(observed)
    return row


async def price_trace_batch(db, events):
    rows = [trace_row(event) for event in events]
    pending = defaultdict(list)
    for event, row in zip(events, rows):
        if row.get('price_version_id') or not row.get('provider_origin') or not event.get('_price_at'):
            continue
        if not (row.get('release_id') or row.get('is_test')):
            continue
        # Use the exact pinned profile name, including characters beyond the
        # trace display column's limit and after a backup model dispatch.
        if row.get('profile_revision_id'):
            pending[row['profile_revision_id']].append(row)

    if not pending:
        return rows
    profiles = await db.execute(sa.select(ai_profile_revisions.c.id, ai_profile_revisions.c.model,
        ai_profile_revisions.c.base_url).where(scoped(ai_profile_revisions), ai_profile_revisions.c.id.in_(pending)))
    identities = defaultdict(list)
    for profile in profiles:
        origin, _ = canonical_url(profile.base_url)
        identities[(origin, profile.model)].extend(row for row in pending[profile.id] if row['provider_origin'] == origin)

    table = model_price_versions
    for (origin, model), matching_rows in identities.items():
        if not matching_rows:
            continue
        query = sa.select(table).where(scoped(table), table.c.origin == origin, table.c.model == model,
            table.c.effective_from <= max(row['created_at'] for row in matching_rows))
        prices = (await db.execute(query.order_by(table.c.effective_from.desc(), table.c.price_version.desc()))).mappings().all()
        for row in matching_rows:
            price = next((price for price in prices if price['model'] == model and price['origin'] == origin
                          and price['effective_from'] <= row['created_at']), None)
            if price is None:
                continue
            row.update(price_version_id=price['id'], cost_currency=price['currency'])
            prompt, completion = row.get('prompt_tokens'), row.get('completion_tokens')
            if (row.get('usage_status') not in ('known', 'complete') or
                    any(type(value) is not int or value < 0 for value in (prompt, completion))):
                continue
            amount = (prompt * price['input_unit_price'] + completion * price['output_unit_price']) / price['unit_tokens']
            if amount >= MONEY_LIMIT:
                continue
            amount = amount.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)
            if amount < MONEY_LIMIT:
                row.update(cost_amount=amount, pricing_status='priced')
    return rows
