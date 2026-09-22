import csv
import json
from datetime import timedelta

import pytest

from arc_monitor.models import Launch, Liquidity, Trade
from arc_monitor.storage import Store
from test_storage import T


def address(number: int) -> str:
    return "0x" + format(number, "040x")


def launch(number: int, *, symbol: str = "TST") -> Launch:
    at = T + timedelta(hours=number * 3)
    return Launch(
        id=f"launch-{number}",
        token=address(10_000 + number),
        ts=at,
        source="test",
        tx="0x" + format(number + 1, "064x"),
        creator=address(20_000 + number),
        symbol=symbol,
        pool=f"pool-{number}",
    )


def trade(item: Launch, suffix: str, minute: float, wallet_number: int, *,
          quote: str = "100", quantity: str = "10", side: str = "buy") -> Trade:
    return Trade(
        id=f"{item.id}-{suffix}",
        token=item.token,
        ts=item.ts + timedelta(minutes=minute),
        source="test",
        tx="0x" + format(abs(hash((item.id, suffix))) % (16**64), "064x"),
        wallet=address(wallet_number),
        side=side,
        quantity=quantity,
        quote_amount=quote,
        pool=item.pool,
        symbol=item.symbol,
    )


def populated_store(count: int = 16, *, stale_terminal_number: int | None = None) -> tuple[Store, object]:
    store = Store(":memory:", mode="demo")
    launches = []
    events = []
    for number in range(count):
        item = launch(number, symbol="=UNTRUSTED" if number == count - 1 else "TST")
        launches.append(item)
        events.append(item)
        high_signal = number % 2 == 0
        feature_buyers = 20 if high_signal else 2
        events.extend(
            trade(item, f"feature-{buyer}", 10 + buyer / 10, number * 100 + buyer + 1)
            for buyer in range(feature_buyers)
        )
        events.extend([
            Liquidity(id=f"{item.id}-liq-entry", token=item.token,
                      ts=item.ts + timedelta(minutes=19), source="test",
                      pool=item.pool, amount_usdc="10000"),
            Liquidity(id=f"{item.id}-liq-exit", token=item.token,
                      ts=item.ts + timedelta(minutes=58), source="test",
                      pool=item.pool, amount_usdc="10000"),
            trade(item, "entry", 20, number * 100 + 80, quote="120", quantity="10"),
        ])
        if high_signal and number != stale_terminal_number:
            events.extend([
                # Later round-trip evidence must not retroactively change the entry
                # that was eligible at minute 20.
                trade(item, "entry-roundtrip", 20.5, number * 100 + 80,
                      quote="120", quantity="10", side="sell"),
                trade(item, "late-a", 50, number * 100 + 90, quote="180", quantity="10"),
                trade(item, "late-b", 59, number * 100 + 91, quote="240", quantity="10"),
            ])
        else:
            events.append(trade(item, "terminal", 40, number * 100 + 80,
                                quote="80", quantity="10"))
    store.ingest(events)
    observed_until = launches[-1].ts + timedelta(hours=2)
    store.record_window("trades", launches[0].ts, observed_until, [], "test")
    return store, observed_until


def test_sparse_or_uncovered_data_is_reported_as_insufficient(tmp_path):
    from arc_monitor.evaluation import evaluate_research

    with Store(":memory:", mode="live") as store:
        store.ingest([launch(0)])
        report = evaluate_research(
            store, str(tmp_path), T + timedelta(hours=2), min_train=1, min_test=1
        )

    assert report["status"] == "insufficient_data"
    assert report["notification_eligible"] is False
    assert report["sample_counts"]["eligible"] == 0
    assert report["censor_reasons"] == {"incomplete_feature_coverage": 1}
    assert json.loads((tmp_path / "evaluation.json").read_text(encoding="utf-8"))["status"] == "insufficient_data"
    assert (tmp_path / "evaluation.csv").is_file()


def test_evaluation_uses_purged_disjoint_holdout_and_train_only_selection(tmp_path):
    from arc_monitor.evaluation import evaluate_research

    store, observed_until = populated_store()
    with store:
        report = evaluate_research(
            store,
            str(tmp_path),
            observed_until,
            min_train=5,
            min_test=4,
            latency_seconds=120,
        )

    assert report["status"] == "evaluated"
    assert report["split"]["token_overlap"] == 0
    assert report["split"]["purged_rows"] >= 0
    assert report["selection"]["selected_on"] == "train_only"
    assert report["selection"]["rule"] == "runner_65"
    assert report["selection"]["parameters"] == [
        {"field": "runner_score", "operator": ">=", "value": 65}
    ]
    assert report["test"]["confusion_matrix"]["false_positive"] == 0
    assert report["test"]["precision"] == 1.0
    assert report["test"]["false_positive_rate"] == 0.0
    assert report["test"]["false_alerts"] == 0
    assert report["notification_eligible"] is False
    assert "insufficient_independent_train_test" in report["notification_reasons"]
    assert "uncertain_execution" in report["notification_reasons"]
    assert report["fingerprints"]["protocol"] == "arc-evaluation-v1"
    assert len(report["fingerprints"]["gate_sha256"]) == 64


def test_public_rule_matcher_uses_reported_feature_fields():
    from arc_monitor.evaluation import matches_rule

    features = {
        "runner_score": 65,
        "buyer_clusters_estimate": 9,
        "adjusted_volume_usdc": "1000",
    }
    assert matches_rule("runner_65", features) is True
    assert matches_rule("buyers_10", features) is False
    assert matches_rule("volume_1000", features) is True
    with pytest.raises(ValueError, match="unknown evaluation rule"):
        matches_rule("edited-rule", features)


def test_execution_uses_delayed_first_trade_terminal_exit_and_never_claims_verified_pnl(tmp_path):
    from arc_monitor.evaluation import evaluate_research

    store, observed_until = populated_store()
    with store:
        report = evaluate_research(
            store, str(tmp_path), observed_until, min_train=5, min_test=4,
            fee_bps=30, slippage_bps=100, position_usdc=100, latency_seconds=120,
        )

    scenario = report["execution"]["aggregate"]
    assert scenario["modeled_positions"] > 0
    assert scenario["verified_executable_pnl_usdc"] is None
    assert scenario["conditional_pnl_usdc"] is not None
    assert "tax_unknown" in report["execution"]["unknowns"]
    assert "sellability_unknown" in report["execution"]["unknowns"]
    assert "lp_status_unknown" in report["execution"]["unknowns"]
    assert report["execution"]["assumptions"]["entry"] == "first eligible trade at or after alert latency"
    assert report["execution"]["assumptions"]["exit"] == "last eligible trade before horizon end"
    assert report["execution"]["assumptions"]["terminal_price_window_minutes"] == 15
    assert report["execution"]["assumptions"]["liquidity_participation_cap"] == 0.01
    for token, position in report["execution"]["positions"].items():
        if position["conditional_pnl_usdc"] is not None:
            number = int(token, 16) - 10_000
            assert position["entry_at"] == (T + timedelta(hours=number * 3, minutes=20)).isoformat().replace(
                "+00:00", "Z"
            )


def test_execution_rejects_terminal_price_observed_too_early(tmp_path):
    from arc_monitor.evaluation import evaluate_research

    store, observed_until = populated_store(stale_terminal_number=14)
    with store:
        report = evaluate_research(
            store, str(tmp_path), observed_until, min_train=5, min_test=4
        )

    position = report["execution"]["positions"][address(10_014)]
    assert position["status"] == "terminal_price_unavailable"
    assert position["conditional_pnl_usdc"] is None
    assert position["unknowns"] == ["fresh_terminal_price_unknown"]


def test_exports_json_and_formula_safe_csv(tmp_path):
    from arc_monitor.evaluation import evaluate_research, verify_report_fingerprints

    store, observed_until = populated_store()
    with store:
        report = evaluate_research(store, str(tmp_path), observed_until, min_train=5, min_test=4)

    exported = json.loads((tmp_path / "evaluation.json").read_text(encoding="utf-8"))
    assert exported["fingerprints"] == report["fingerprints"]
    assert verify_report_fingerprints(exported) is True
    exported["test"]["precision"] = 0.0
    assert verify_report_fingerprints(exported) is False
    with (tmp_path / "evaluation.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows
    assert all(not row["symbol"].startswith("=") for row in rows)


def test_dataset_fingerprint_binds_events_and_coverage():
    from arc_monitor.evaluation import dataset_fingerprint

    item = launch(0)
    with Store(":memory:", mode="live") as store:
        before = dataset_fingerprint(store, T + timedelta(hours=2))
        with store.engine.begin() as connection:
            connection.execute(store.settings.insert().values(key="ops:evaluation", value="run-1"))
        after_operational_setting = dataset_fingerprint(store, T + timedelta(hours=2))
        store.ingest([item])
        with_event = dataset_fingerprint(store, T + timedelta(hours=2))
        store.record_window("trades", T, T + timedelta(hours=1), [], "test")
        with_coverage = dataset_fingerprint(store, T + timedelta(hours=2))

    assert after_operational_setting == before
    assert len({before, with_event, with_coverage}) == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {"horizon_hours": 2},
        {"feature_minutes": 60},
        {"fee_bps": -1},
        {"slippage_bps": 10_000},
        {"position_usdc": 0},
        {"latency_seconds": -1},
    ],
)
def test_invalid_evaluation_configuration_is_rejected(tmp_path, kwargs):
    from arc_monitor.evaluation import evaluate_research

    with Store(":memory:") as store, pytest.raises(ValueError):
        evaluate_research(store, str(tmp_path), T, **kwargs)
