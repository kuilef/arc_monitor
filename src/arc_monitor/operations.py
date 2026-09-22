"""Local process lease and explicit interval coverage/freshness status."""

from __future__ import annotations

from datetime import timedelta
import os
from pathlib import Path

from sqlalchemy import select

from .models import iso
from .storage import EPOCH, micros


def cost_projection(store, requests_per_hour=60):
    import math

    if not math.isfinite(requests_per_hour) or requests_per_hour <= 0:
        raise ValueError("Expected request frequency must be finite and positive")
    quota = store.get_state("quota")
    if not quota or quota.get("validation_error"):
        return {"status": "usage_unavailable", "points_per_hour": None, "points_per_day": None}
    period = quota["billing_period"]
    usage = period["usage"]
    count = usage.get("requests_usage")
    points = usage.get("points_usage")
    average = points / count if count and points is not None else None
    hourly = average * requests_per_hour if average is not None else None
    return {
        "status": "account_average_scenario",
        "usage_checked_at": quota["checked_at"],
        "observed_account_points_per_request": average,
        "assumed_requests_per_hour": requests_per_hour,
        "points_per_hour": hourly,
        "points_per_day": hourly * 24 if hourly is not None else None,
        "remaining_points": period["limits"]["points_limit"] - points if points is not None else None,
        "money_per_day": None,
        "limitations": [
            "Account-wide counters can lag and include other clients.",
            "Assumed frequency excludes extra backfill/retries/enrichment unless supplied.",
            "Future query costs vary; this is a scenario, not a bill or guaranteed ceiling.",
        ],
    }


class WriterLock:
    def __init__(self, database):
        self.database = database
        self.file = None

    def __enter__(self):
        if self.database == ":memory:":
            return self
        if "://" in self.database:
            raise ValueError("Managed collector currently requires a local SQLite file")
        path = Path(self.database).expanduser().resolve().with_suffix(".collector.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise RuntimeError("Another collector holds this database lock") from None
        return self

    def __exit__(self, *_):
        if self.file:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()


def coverage_intervals(store, stream, start, end):
    left, right = micros(start), micros(end)
    with store.engine.connect() as conn:
        rows = conn.execute(
            select(store.windows.c.start, store.windows.c.end)
            .where(
                store.windows.c.stream == stream, store.windows.c.end > left, store.windows.c.start < right
            )
            .order_by(store.windows.c.start)
        ).all()
    merged = []
    for a, b in rows:
        a, b = max(left, a), min(right, b)
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    gaps = []
    cursor = left
    for a, b in merged:
        if a > cursor:
            gaps.append([cursor, a])
        cursor = max(cursor, b)
    if cursor < right:
        gaps.append([cursor, right])

    def display(interval):
        return {
            "start": iso(EPOCH + timedelta(microseconds=interval[0])),
            "end": iso(EPOCH + timedelta(microseconds=interval[1])),
        }

    return {
        "intervals": [display(i) for i in merged],
        "gaps": [display(i) for i in gaps],
        "gap_seconds": sum(b - a for a, b in gaps) / 1e6,
        "complete": not gaps,
    }


def collection_status(store, now, *, start=None, lag_seconds=120, stale_seconds=300):
    end = now - timedelta(seconds=lag_seconds)
    start = start or end - timedelta(hours=24)
    if start >= end:
        raise ValueError("Status start must precede the lag-adjusted end")
    streams = {}
    for stream, kind in [("launches", "launch"), ("trades", "trade"), ("flows", "flow")]:
        cp = store.checkpoint(stream)
        table = store.tables[kind]
        with store.engine.connect() as conn:
            latest = conn.execute(
                select(table.c.ts).where(table.c.ts <= micros(now)).order_by(table.c.ts.desc()).limit(1)
            ).scalar_one_or_none()
        age = (end - cp).total_seconds() if cp else None
        streams[stream] = {
            **coverage_intervals(store, stream, start, end),
            "checkpoint": iso(cp) if cp else None,
            "lag_seconds": max(0, age) if age is not None else None,
            "fresh": age is not None and cp <= now and age <= stale_seconds,
            "latest_event_at": iso(EPOCH + timedelta(microseconds=latest)) if latest is not None else None,
        }
    collector = store.get_state("collector")
    if collector and collector.get("heartbeat_at"):
        from .models import parse_time

        heartbeat_age = (now - parse_time(collector["heartbeat_at"])).total_seconds()
        expected_wake = (
            parse_time(collector["next_wake_at"])
            if collector.get("status") == "waiting" and collector.get("next_wake_at")
            else None
        )
        overdue = (now - expected_wake).total_seconds() if expected_wake else heartbeat_age
        collector = {
            **collector,
            "heartbeat_age_seconds": heartbeat_age,
            "unresponsive": collector.get("status") in {"running", "waiting", "starting", "retrying"}
            and overdue > stale_seconds,
        }
    return {
        "as_of": iso(now),
        "start": iso(start),
        "target_end": iso(end),
        "streams": streams,
        "collector": collector,
        "quota": store.get_state("quota"),
        "daily_budget": store.get_state("daily_budget"),
        "enrichment": store.get_state("enrichment"),
        "coverage_scope": "Successful bounded queries for configured streams, not all-chain completeness.",
    }
