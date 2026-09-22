"""Bounded, read-only probes for evidence of provider history availability."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .cctp import cctp_query
from .models import iso, utc
from .providers.bitquery import ProviderError


AVAILABLE = "available_if_rows"
EMPTY = "empty_not_proof_absent"
ERROR = "provider_error"


def _validate(dates: list[datetime], window_seconds: int, include_flows: bool) -> list[tuple[datetime, datetime]]:
    if not isinstance(dates, list) or len(dates) > 8:
        raise ValueError("dates must be a list containing at most 8 starts")
    if (not isinstance(window_seconds, int) or isinstance(window_seconds, bool)
            or window_seconds < 1):
        raise ValueError("window_seconds must be a positive integer")
    if not isinstance(include_flows, bool):
        raise ValueError("include_flows must be boolean")
    now = datetime.now(timezone.utc)
    windows = []
    for value in dates:
        if not isinstance(value, datetime):
            raise ValueError("history probe starts must be datetimes")
        try:
            start = utc(value)
        except (AttributeError, TypeError, ValueError):
            raise ValueError("history probe starts must be timezone-aware") from None
        if start.microsecond:
            raise ValueError("history probe starts must align to whole seconds")
        end = start + timedelta(seconds=window_seconds)
        if end > now:
            raise ValueError("history probe windows must end in the past")
        windows.append((start, end))
    return windows


def probe_history(store, provider, dates: list[datetime], *, window_seconds: int = 120,
                  include_flows: bool = True) -> dict:
    """Probe short historical windows without ingesting rows or asserting coverage.

    Every successful query uses ``limit=1``. A row proves that the provider returned
    something at that date under this exact query; an empty result proves neither
    event absence nor provider retention depth. ``BudgetExceeded`` is deliberately
    not caught and therefore aborts immediately.
    """
    windows = _validate(dates, window_seconds, include_flows)
    report = {"window_seconds": window_seconds, "limit": 1, "include_flows": include_flows,
              "requests": 0, "probes": []}

    def result_for(stream: str, start: datetime, end: datetime, operation) -> dict:
        report["requests"] += 1
        try:
            rows = operation()
        except ProviderError as exc:
            payload = {"stream": stream, "start": iso(start), "end": iso(end), "limit": 1,
                       "status": ERROR, "error": str(exc),
                       "retryable": bool(getattr(exc, "retryable", False))}
            raw_id = store.save_raw("history_probe", payload)
            return {"status": ERROR, "error": str(exc),
                    "retryable": payload["retryable"], "raw_id": raw_id}
        if not isinstance(rows, list):
            payload = {"stream": stream, "start": iso(start), "end": iso(end), "limit": 1,
                       "status": ERROR, "error": "provider returned non-list rows",
                       "retryable": False}
            raw_id = store.save_raw("history_probe", payload)
            return {"status": ERROR, "error": payload["error"],
                    "retryable": False, "raw_id": raw_id}
        status = AVAILABLE if rows else EMPTY
        payload = {"stream": stream, "start": iso(start), "end": iso(end), "limit": 1,
                   "status": status, "rows": rows}
        raw_id = store.save_raw("history_probe", payload)
        return {"status": status, "row_count": len(rows), "raw_id": raw_id}

    for start, end in windows:
        streams = {}
        for stream in ("launches", "trades"):
            streams[stream] = result_for(
                stream, start, end,
                lambda stream=stream, start=start, end=end: provider.fetch(stream, start, end, 1),
            )
        if include_flows:
            streams["flows"] = result_for(
                "flows", start, end,
                lambda start=start, end=end: provider.execute(
                    cctp_query(start, end, 1), "EVM", "Events"
                ),
            )
        report["probes"].append({"start": iso(start), "end": iso(end), "streams": streams})
    return report
