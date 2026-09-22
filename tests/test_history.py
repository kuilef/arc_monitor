import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from arc_monitor.budget import BudgetExceeded
from arc_monitor.providers.bitquery import ProviderError
from arc_monitor.storage import Store


T = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


class Provider:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def fetch(self, kind, start, end, limit):
        self.calls.append(("fetch", kind, start, end, limit))
        value = self.responses.get(kind, [])
        if isinstance(value, Exception):
            raise value
        return value

    def execute(self, query, root, cube):
        self.calls.append(("execute", query, root, cube))
        value = self.responses.get("flows", [])
        if isinstance(value, Exception):
            raise value
        return value


def raw(store):
    with store.engine.connect() as conn:
        return [json.loads(value) for value in conn.execute(select(store.raw.c.payload)).scalars()]


def test_limit_one_probes_report_rows_or_ambiguous_empty_without_writing_coverage():
    from arc_monitor.history import probe_history

    provider = Provider({"launches": [{"event": 1}], "trades": [], "flows": [{"message": 1}]})
    with Store(":memory:") as store:
        result = probe_history(store, provider, [T], window_seconds=120)
        streams = result["probes"][0]["streams"]
        assert streams["launches"]["status"] == "available_if_rows"
        assert streams["trades"]["status"] == "empty_not_proof_absent"
        assert streams["flows"]["status"] == "available_if_rows"
        assert result["requests"] == 3 and result["limit"] == 1
        assert all(item["row_count"] in {0, 1} and item["raw_id"] for item in streams.values())
        assert not any(store.covered(name, T, T + timedelta(seconds=120))
                       for name in ("launches", "trades", "flows"))
        assert all(store.count(kind) == 0 for kind in ("launch", "trade", "flow"))
        assert len(raw(store)) == 3

    assert provider.calls[0] == ("fetch", "launches", T, T + timedelta(seconds=120), 1)
    assert provider.calls[1] == ("fetch", "trades", T, T + timedelta(seconds=120), 1)
    method, query, root, cube = provider.calls[2]
    assert (method, root, cube) == ("execute", "EVM", "Events")
    assert "limit: {count: 1}" in query


def test_provider_errors_are_visible_per_stream_and_never_masked_as_empty():
    from arc_monitor.history import probe_history

    provider = Provider({"launches": ProviderError("launch probe denied"), "trades": [],
                         "flows": ProviderError("flow schema unavailable", retryable=True)})
    with Store(":memory:") as store:
        result = probe_history(store, provider, [T])
        streams = result["probes"][0]["streams"]
        assert streams["launches"] == {"status": "provider_error", "error": "launch probe denied",
                                         "retryable": False, "raw_id": streams["launches"]["raw_id"]}
        assert streams["trades"]["status"] == "empty_not_proof_absent"
        assert streams["flows"]["status"] == "provider_error"
        assert streams["flows"]["retryable"] is True
        assert not store.covered("trades", T, T + timedelta(seconds=120))
        assert any(item.get("status") == "provider_error" for item in raw(store))


def test_budget_exhaustion_propagates_immediately():
    from arc_monitor.history import probe_history

    provider = Provider({"launches": BudgetExceeded("point reserve reached")})
    with Store(":memory:") as store:
        with pytest.raises(BudgetExceeded, match="point reserve"):
            probe_history(store, provider, [T])
    assert len(provider.calls) == 1


@pytest.mark.parametrize("dates,seconds,include", [
    ([T] * 9, 120, True),
    ([T.replace(tzinfo=None)], 120, True),
    ([datetime.now(timezone.utc) - timedelta(seconds=30)], 120, True),
    ([T.replace(microsecond=1)], 120, True),
    ([T], 0, True),
    ([T], 120, "yes"),
])
def test_all_validation_happens_before_http(dates, seconds, include):
    from arc_monitor.history import probe_history

    provider = Provider()
    with Store(":memory:") as store:
        with pytest.raises(ValueError):
            probe_history(store, provider, dates, window_seconds=seconds, include_flows=include)
    assert provider.calls == []


def test_flows_can_be_omitted_without_changing_read_only_semantics():
    from arc_monitor.history import probe_history

    provider = Provider()
    with Store(":memory:") as store:
        result = probe_history(store, provider, [T], include_flows=False)
        assert set(result["probes"][0]["streams"]) == {"launches", "trades"}
        assert result["requests"] == 2
    assert [call[0] for call in provider.calls] == ["fetch", "fetch"]
