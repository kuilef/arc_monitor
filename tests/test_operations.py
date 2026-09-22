from datetime import datetime, timedelta, timezone
import pytest


def test_env_quotes_precedence_and_no_execution(tmp_path):
    from arc_monitor.config import load_env

    p = tmp_path / ".env"
    p.write_text(
        '\ufeffBITQUERY_TOKEN="file secret"\nARC_MODE=demo # comment\nARC_DB=database.db\n', encoding="utf-8"
    )
    env = {"BITQUERY_TOKEN": "process secret"}
    assert load_env(p, env) == ["ARC_MODE", "ARC_DB"]
    assert env == {"BITQUERY_TOKEN": "process secret", "ARC_MODE": "demo", "ARC_DB": "database.db"}
    p.write_text('BITQUERY_TOKEN="unterminated secret', encoding="utf-8")
    with pytest.raises(ValueError) as error:
        load_env(p, {})
    assert "secret" not in str(error.value)


def test_request_pacing_and_reservations_survive_stale_usage():
    from arc_monitor.budget import RequestBudget, BudgetExceeded

    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    b = RequestBudget(
        max_requests=10, rpm=6, points_budget=15, points_per_request=5, clock=lambda: clock[0], sleep=sleep
    )
    b.observe_usage(
        {
            "status": "active",
            "billing_period": {
                "limits": {"points_limit": 1000, "rate_limit": 10},
                "usage": {"points_usage": 40},
            },
        }
    )
    b.consume()
    b.consume()
    assert clock[0] >= 10
    b.observe_usage(
        {
            "status": "active",
            "billing_period": {
                "limits": {"points_limit": 1000, "rate_limit": 10},
                "usage": {"points_usage": 40},
            },
        }
    )
    b.consume()
    with pytest.raises(BudgetExceeded):
        b.consume()
    assert b.reserved_points == 15


def test_quota_block_and_missing_usage_fail_closed():
    from arc_monitor.budget import RequestBudget, BudgetExceeded

    b = RequestBudget(max_requests=10, rpm=6, points_budget=100)
    with pytest.raises(BudgetExceeded):
        b.observe_usage({"status": "expired"})
    with pytest.raises(BudgetExceeded):
        b.observe_usage({"status": "active", "billing_period": {}})


def test_coverage_holes_and_empty_window_freshness():
    from arc_monitor.operations import collection_status
    from arc_monitor.storage import Store

    t = datetime(2026, 9, 20, tzinfo=timezone.utc)
    with Store(":memory:") as s:
        s.record_window("trades", t, t + timedelta(minutes=2), [], "bitquery")
        s.record_window("trades", t + timedelta(minutes=3), t + timedelta(minutes=5), [], "bitquery")
        report = collection_status(s, t + timedelta(minutes=5), start=t, lag_seconds=0, stale_seconds=180)
        trade = report["streams"]["trades"]
        assert trade["gap_seconds"] == 60 and len(trade["gaps"]) == 1
        assert trade["fresh"] is True and trade["latest_event_at"] is None
        assert trade["complete"] is False
        assert report["streams"]["launches"]["fresh"] is False


def test_writer_lock_and_release(tmp_path):
    from arc_monitor.operations import WriterLock

    with WriterLock(str(tmp_path / "arc.db")):
        with pytest.raises(RuntimeError, match="collector"):
            with WriterLock(str(tmp_path / "arc.db")):
                pass
    with WriterLock(str(tmp_path / "arc.db")):
        pass


def test_bitquery_every_retry_metered_and_usage_secrets_not_returned():
    import httpx
    from arc_monitor.budget import RequestBudget
    from arc_monitor.providers.bitquery import BitqueryClient

    replies = [httpx.Response(429), httpx.Response(200, json={"data": {"EVM": {"Holders": []}}})]
    client = httpx.Client(transport=httpx.MockTransport(lambda r: replies.pop(0)))
    budget = RequestBudget(max_requests=3, rpm=600000, points_budget=50, sleep=lambda _: None)
    with BitqueryClient("secret", client=client, sleep=lambda _: None, budget=budget) as p:
        assert p.execute("query {}", "EVM", "Holders") == []
        assert p.requests == 2 and budget.requests == 2 and budget.reserved_points == 20


def test_bitquery_preserves_wire_decimal_text():
    import httpx
    from arc_monitor.providers.bitquery import BitqueryClient

    body = b'{"data":{"EVM":{"Holders":[{"amount":123456789.123456789}]}}}'
    with BitqueryClient(
        "secret",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))),
    ) as p:
        assert p.execute("query {}", "EVM", "Holders")[0]["amount"] == "123456789.123456789"


def test_daily_reservations_persist_across_restarts():
    from arc_monitor.monitoring import CollectionSession
    from arc_monitor.budget import BudgetExceeded
    from arc_monitor.storage import Store

    with Store(":memory:") as s:
        with CollectionSession(s, "secret", daily_points=10) as run:
            run.budget.consume()
        with pytest.raises(BudgetExceeded):
            with CollectionSession(s, "secret", daily_points=10) as run:
                run.budget.consume()
        assert s.get_state("daily_budget")["reserved_points"] == 10
        assert s.get_state("collector")["status"] == "budget_exhausted"
        assert s.get_state("collector")["budget"]["http_requests"] == 0
        assert s.get_state("collector")["budget"]["reserved_points"] == 0
        assert "secret" not in str(s.get_state("collector"))


def test_cli_env_file_doctor_does_not_print_secret(tmp_path, monkeypatch, capsys):
    from arc_monitor.cli import main

    monkeypatch.delenv("BITQUERY_TOKEN", raising=False)
    monkeypatch.delenv("ARC_MODE", raising=False)
    p = tmp_path / ".env"
    p.write_text("BITQUERY_TOKEN=very-secret\nARC_MODE=demo\n")
    assert main(["--env-file", str(p), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "very-secret" not in out and '"bitquery_token_set": true' in out
    assert '"mode": "demo"' in out


def test_account_reservations_survive_day_reset_and_stale_usage(monkeypatch):
    import httpx
    from arc_monitor import monitoring
    from arc_monitor.monitoring import CollectionSession
    from arc_monitor.storage import Store
    from arc_monitor.budget import BudgetExceeded

    at = datetime(2026, 9, 22, tzinfo=timezone.utc)
    monkeypatch.setattr(monitoring, "now", lambda: at)
    body = {
        "status": "active",
        "billing_period": {
            "started_at": "2026-09-01T00:00:00Z",
            "limits": {"points_limit": 35, "rate_limit": 10},
            "usage": {"points_usage": 0},
        },
    }

    def client():
        return httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))

    with Store(":memory:") as s:
        with CollectionSession(s, "secret", client=client(), sleep=lambda _: None) as run:
            run.refresh_usage()
            run.budget.consume()
        at += timedelta(days=1)
        with pytest.raises(BudgetExceeded):
            with CollectionSession(s, "secret", client=client(), sleep=lambda _: None) as run:
                run.refresh_usage()
                run.budget.consume()


def test_scheduled_wait_is_not_a_dead_heartbeat():
    from arc_monitor.storage import Store
    from arc_monitor.operations import collection_status
    from arc_monitor.models import iso

    at = datetime(2026, 9, 22, tzinfo=timezone.utc)
    with Store(":memory:") as s:
        s.set_state(
            "collector",
            {"status": "waiting", "heartbeat_at": iso(at), "next_wake_at": iso(at + timedelta(hours=1))},
        )
        assert not collection_status(s, at + timedelta(minutes=10))["collector"]["unresponsive"]
        assert collection_status(s, at + timedelta(minutes=70))["collector"]["unresponsive"]


def test_poll_prioritizes_fresh_streams_before_bounded_hole_repair(tmp_path, monkeypatch):
    from arc_monitor import monitoring
    from arc_monitor.cli import parser
    from arc_monitor.storage import Store
    from arc_monitor.models import iso

    at = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
    db = str(tmp_path / "arc.db")
    with Store(db) as store:
        store.set_state("monitor_start", iso(at - timedelta(hours=2)))
    monkeypatch.setattr(monitoring, "now", lambda: at)
    monkeypatch.setattr(monitoring.CollectionSession, "refresh_usage", lambda self: {})
    calls = []

    def collect(store, provider, kind, left, right, **kwargs):
        calls.append((kind, left, right))
        store.record_window(kind, left, right, [], "test")
        return {"requests": 1}

    monkeypatch.setattr(monitoring, "collect", collect)
    args = parser().parse_args(["--db", db, "poll", "--cycles", "1", "--enrich-every", "0"])
    assert monitoring.run_collection(args, "secret", lambda _: None) == 0
    assert [entry[0] for entry in calls[:2]] == ["launches", "trades"]
    assert all(entry[2] == at - timedelta(seconds=120) for entry in calls[:2])
    assert len(calls) == 4
    assert all((entry[2] - entry[1]).total_seconds() <= 120 for entry in calls[2:])


def test_expired_token_records_stop_before_any_http(monkeypatch):
    import httpx
    from arc_monitor.monitoring import CollectionSession
    from arc_monitor.storage import Store
    from arc_monitor.budget import BudgetExceeded

    monkeypatch.setenv("ARC_TOKEN_EXPIRES_AT", "2020-01-01T00:00:00Z")
    requests = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: requests.append(r)))
    with Store(":memory:") as store:
        with pytest.raises(BudgetExceeded):
            with CollectionSession(store, "secret", client=client) as session:
                session.refresh_usage()
        assert not requests
        assert store.get_state("collector")["status"] == "budget_exhausted"


def test_auxiliary_operation_keeps_collector_stop_visible():
    from arc_monitor.monitoring import CollectionSession
    from arc_monitor.storage import Store

    with Store(":memory:") as store:
        store.set_state("collector", {"status": "budget_exhausted", "run_id": "monitor"})
        with CollectionSession(store, "secret", state_key="operation:quota"):
            pass
        assert store.get_state("collector")["status"] == "budget_exhausted"
        assert store.get_state("operation:quota")["status"] == "completed"


def test_exhausted_fresh_quota_is_saved_before_collection_stops():
    import httpx
    from arc_monitor.monitoring import CollectionSession
    from arc_monitor.storage import Store
    from arc_monitor.budget import BudgetExceeded

    body = {
        "status": "active",
        "billing_period": {
            "started_at": "2026-09-01T00:00:00Z",
            "limits": {"points_limit": 100, "rate_limit": 10},
            "usage": {"points_usage": 95},
        },
    }
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    with Store(":memory:") as store:
        with pytest.raises(BudgetExceeded):
            with CollectionSession(store, "secret", client=client) as session:
                session.refresh_usage()
        assert store.get_state("quota")["billing_period"]["usage"]["points_usage"] == 95
        assert store.get_state("account_budget")["observed_used"] == 95
