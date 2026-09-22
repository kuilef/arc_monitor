"""Small transactional event store; SQLite by default, PostgreSQL via SQLAlchemy URL."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (BigInteger, Column, Index, Integer, MetaData, String, Table,
                        Text, create_engine, event as sa_event, select)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.pool import StaticPool

from .models import CHAIN_ID, EVENT, Event, iso, utc

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def micros(value: datetime) -> int:
    delta = utc(value) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


class Store:
    """One dataset per DB. Do not run multiple collector writers against one cursor."""
    def __init__(self, database: str = 'arc.db', *, mode: str = 'live'):
        if mode not in {'live', 'demo'}:
            raise ValueError('mode must be live or demo')
        if database == ':memory:':
            url = 'sqlite+pysqlite://'
            kwargs = {'poolclass': StaticPool, 'connect_args': {'check_same_thread': False}}
        elif '://' in database:
            url, kwargs = database, {}
        else:
            path = Path(database).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            url, kwargs = f'sqlite+pysqlite:///{path.as_posix()}', {}
        self.engine = create_engine(url, **kwargs)
        if self.engine.dialect.name == 'sqlite':
            @sa_event.listens_for(self.engine, 'connect')
            def configure(dbapi, _):
                cursor = dbapi.cursor()
                cursor.execute('PRAGMA journal_mode=WAL')
                cursor.execute('PRAGMA busy_timeout=30000')
                cursor.close()
        if self.engine.dialect.name not in {'sqlite', 'postgresql'}:
            self.close()
            raise ValueError('only SQLite and PostgreSQL are supported')
        self.meta = MetaData()
        self.tables = {}
        for kind, name in [('launch', 'launches'), ('trade', 'trades'), ('liquidity', 'liquidity_snapshots'),
                           ('holders', 'holders_snapshots'), ('funding', 'wallet_funding'),
                           ('flow', 'cctp_flows'), ('risk', 'risk_snapshots')]:
            table = Table(name, self.meta,
                          Column('id', String(256), primary_key=True),
                          Column('token', String(42), nullable=False),
                          Column('ts', BigInteger, nullable=False),
                          Column('source', String(80), nullable=False),
                          Column('payload', Text, nullable=False))
            Index(f'ix_{name}_token_time', table.c.token, table.c.ts)
            self.tables[kind] = table
        self.settings = Table('settings', self.meta, Column('key', String(80), primary_key=True),
                              Column('value', Text, nullable=False))
        self.windows = Table('coverage', self.meta, Column('id', String(64), primary_key=True),
                             Column('stream', String(80), nullable=False),
                             Column('start', BigInteger, nullable=False),
                             Column('end', BigInteger, nullable=False),
                             Column('provider', String(80), nullable=False))
        self.cursors = Table('checkpoints', self.meta, Column('stream', String(80), primary_key=True),
                             Column('value', Text, nullable=False))
        self.raw = Table('raw_batches', self.meta, Column('id', String(64), primary_key=True),
                         Column('source', String(80)), Column('payload', Text, nullable=False))
        self.alerts = Table('alerts', self.meta, Column('id', String(64), primary_key=True),
                            Column('token', String(42)), Column('payload', Text, nullable=False))
        self.meta.create_all(self.engine)
        self.mode = mode
        try:
            with self.engine.begin() as conn:
                for key, value in [('mode', mode), ('chain_id', str(CHAIN_ID)), ('schema_version', '1')]:
                    conn.execute(self._insert(self.settings).values(key=key, value=value)
                                 .on_conflict_do_nothing(index_elements=['key']))
                    actual = conn.execute(select(self.settings.c.value).where(self.settings.c.key == key)).scalar_one()
                    if actual != value:
                        raise ValueError(f'database {key} mismatch: {actual!r} != {value!r}')
        except Exception:
            self.close()
            raise

    def _insert(self, table):
        return sqlite_insert(table) if self.engine.dialect.name == 'sqlite' else pg_insert(table)

    def _ingest(self, conn, events: list[Event]) -> int:
        count = 0
        for item in events:
            item = EVENT.validate_python(item)
            table = self.tables[item.kind]
            payload = canonical(item.model_dump(mode='json'))
            old = conn.execute(select(table.c.payload).where(table.c.id == item.id)).scalar_one_or_none()
            if old is not None:
                if old != payload:
                    raise ValueError(f'event identity conflict for {item.kind}/{item.id}')
                continue
            # Reconciled CCTP message identity is independent of source/native representation.
            if item.kind == 'flow':
                for previous in conn.execute(select(table.c.payload)).scalars():
                    p = json.loads(previous)
                    if (p['direction'], p['message_key']) == (item.direction, item.message_key):
                        raise ValueError('duplicate CCTP message_key; normalize its ID before ingestion')
            conn.execute(table.insert().values(id=item.id, token=item.token, ts=micros(item.ts),
                                               source=item.source, payload=payload))
            count += 1
        return count

    def ingest(self, events: list[Event]) -> int:
        with self.engine.begin() as conn:
            return self._ingest(conn, events)

    def events(self, kind: str, *, token: str | None = None, before: datetime | None = None,
               since: datetime | None = None) -> list:
        table = self.tables[kind]
        query = select(table.c.payload).order_by(table.c.ts, table.c.id)
        if token is not None:
            query = query.where(table.c.token == token.lower())
        if before is not None:
            query = query.where(table.c.ts < micros(before))
        if since is not None:
            query = query.where(table.c.ts >= micros(since))
        with self.engine.connect() as conn:
            return [EVENT.validate_json(v) for v in conn.execute(query).scalars()]

    def record_window(self, stream: str, start: datetime, end: datetime,
                      events: list[Event], provider: str) -> int:
        if not utc(start) < utc(end):
            raise ValueError('coverage window must have start < end')
        if any(not start <= e.ts < end for e in events):
            raise ValueError('event outside committed coverage interval')
        key = hashlib.sha256(f'{stream}:{iso(start)}:{iso(end)}:{provider}'.encode()).hexdigest()
        with self.engine.begin() as conn:
            count = self._ingest(conn, events)
            conn.execute(self._insert(self.windows).values(id=key, stream=stream, start=micros(start),
                         end=micros(end), provider=provider).on_conflict_do_nothing(index_elements=['id']))
            previous = conn.execute(select(self.cursors.c.value).where(self.cursors.c.stream == stream)).scalar_one_or_none()
            from .models import parse_time
            if previous is None or end > parse_time(previous):
                stmt = self._insert(self.cursors).values(stream=stream, value=iso(end))
                conn.execute(stmt.on_conflict_do_update(index_elements=['stream'], set_={'value': iso(end)}))
            return count

    def covered(self, stream: str, start: datetime, end: datetime) -> bool:
        if start >= end:
            return False
        cursor, finish = micros(start), micros(end)
        with self.engine.connect() as conn:
            rows = conn.execute(select(self.windows.c.start, self.windows.c.end)
                    .where(self.windows.c.stream == stream, self.windows.c.end > cursor,
                           self.windows.c.start < finish).order_by(self.windows.c.start)).all()
        for a, b in rows:
            if a > cursor:
                return False
            cursor = max(cursor, b)
            if cursor >= finish:
                return True
        return False

    def checkpoint(self, stream: str) -> datetime | None:
        from .models import parse_time
        with self.engine.connect() as conn:
            value = conn.execute(select(self.cursors.c.value).where(self.cursors.c.stream == stream)).scalar_one_or_none()
        return parse_time(value) if value else None

    def save_raw(self, source: str, body: dict) -> str:
        payload = canonical(body)
        key = hashlib.sha256((source + payload).encode()).hexdigest()
        with self.engine.begin() as conn:
            conn.execute(self._insert(self.raw).values(id=key, source=source, payload=payload)
                         .on_conflict_do_nothing(index_elements=['id']))
        return key

    def alert_once(self, key: str, token: str, payload: dict) -> bool:
        ident = hashlib.sha256(key.encode()).hexdigest()
        with self.engine.begin() as conn:
            result = conn.execute(self._insert(self.alerts).values(id=ident, token=token,
                     payload=canonical(payload)).on_conflict_do_nothing(index_elements=['id']))
            return result.rowcount == 1

    def alert_list(self) -> list[dict]:
        with self.engine.connect() as conn:
            return [json.loads(s) for s in conn.execute(select(self.alerts.c.payload)).scalars()]

    def count(self, kind: str) -> int:
        from sqlalchemy import func
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(self.tables[kind])).scalar_one()

    def close(self):
        self.engine.dispose()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
