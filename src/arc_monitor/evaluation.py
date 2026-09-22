"""Purged hold-out evaluation for fixed launch-monitoring rules.

This module evaluates the existing survival outcome.  Its execution section is a
conditional replay scenario, not evidence that a historical order could be filled.
"""
from __future__ import annotations

import csv
import hashlib
import json
from copy import deepcopy
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable

from sqlalchemy import select

from .metrics import liquidity_stats, origins
from .models import iso, parse_time, utc
from .research import research_rows
from .storage import Store, canonical, micros
from .wash import suspicious_trades

PROTOCOL = "arc-evaluation-v1"
PARTICIPATION_CAP = Decimal("0.01")
MIN_NOTIFICATION_PRECISION = 0.60
MIN_NOTIFICATION_COVERAGE = 0.80
RISK_MAX_AGE = timedelta(minutes=15)


def dataset_fingerprint(store: Store, observed_until: datetime) -> str:
    """Bind evaluation to ordered stored events and coverage known by the cutoff."""
    observed_until = utc(observed_until)
    event_payload = {}
    for kind in sorted(store.tables):
        event_payload[kind] = [
            item.model_dump(mode="json")
            for item in store.events(kind, before=observed_until)
        ]
    cutoff = micros(observed_until)
    with store.engine.connect() as connection:
        coverage = [
            {
                "stream": row.stream,
                "start_micros": row.start,
                "end_micros": min(row.end, cutoff),
                "provider": row.provider,
            }
            for row in connection.execute(
                select(store.windows.c.stream, store.windows.c.start, store.windows.c.end,
                       store.windows.c.provider)
                .where(store.windows.c.start < cutoff)
                .order_by(store.windows.c.stream, store.windows.c.start,
                          store.windows.c.end, store.windows.c.provider)
            )
        ]
        immutable_settings = ("mode", "chain_id", "schema_version", "data_scope_version")
        settings = dict(connection.execute(
            select(store.settings.c.key, store.settings.c.value)
            .where(store.settings.c.key.in_(immutable_settings))
            .order_by(store.settings.c.key)
        ).all())
    payload = {
        "observed_until": iso(observed_until),
        "settings": settings,
        "events": event_payload,
        "coverage": coverage,
    }
    return hashlib.sha256(canonical(payload).encode()).hexdigest()


def _report_sha256(report: dict) -> str:
    payload = deepcopy(report)
    fingerprints = payload.get("fingerprints", {})
    fingerprints.pop("report_sha256", None)
    fingerprints.pop("gate_sha256", None)
    return hashlib.sha256(canonical(payload).encode()).hexdigest()


def _gate_sha256(fingerprints: dict) -> str:
    payload = {
        "protocol": fingerprints["protocol"],
        "config_sha256": fingerprints["config_sha256"],
        "data_sha256": fingerprints["data_sha256"],
        "report_sha256": fingerprints["report_sha256"],
    }
    return hashlib.sha256(canonical(payload).encode()).hexdigest()


def verify_report_fingerprints(report: dict) -> bool:
    """Detect report/config edits; callers must also compare the live data hash."""
    try:
        fingerprints = report["fingerprints"]
        config_hash = hashlib.sha256(canonical(report["configuration"]).encode()).hexdigest()
        return (
            report["protocol"] == fingerprints["protocol"] == PROTOCOL
            and config_hash == fingerprints["config_sha256"]
            and _report_sha256(report) == fingerprints["report_sha256"]
            and _gate_sha256(fingerprints) == fingerprints["gate_sha256"]
        )
    except (KeyError, TypeError, ValueError):
        return False


def _number(value: object) -> Decimal:
    return Decimal(str(value or 0))


Rule = tuple[str, str, Callable[[dict], bool]]
RULES: tuple[Rule, ...] = (
    ("runner_65", "fixed", lambda row: _number(row.get("runner_score")) >= 65),
    ("buyers_10", "comparator", lambda row: _number(row.get("buyer_clusters_estimate")) >= 10),
    ("volume_1000", "comparator", lambda row: _number(row.get("adjusted_volume_usdc")) >= 1000),
    ("runner_65_and_buyers_10", "ablation", lambda row: (
        _number(row.get("runner_score")) >= 65
        and _number(row.get("buyer_clusters_estimate")) >= 10
    )),
    ("runner_65_and_volume_1000", "ablation", lambda row: (
        _number(row.get("runner_score")) >= 65
        and _number(row.get("adjusted_volume_usdc")) >= 1000
    )),
    ("buyers_10_and_volume_1000", "ablation", lambda row: (
        _number(row.get("buyer_clusters_estimate")) >= 10
        and _number(row.get("adjusted_volume_usdc")) >= 1000
    )),
)
RULE_PARAMETERS = {
    "runner_65": [{"field": "runner_score", "operator": ">=", "value": 65}],
    "buyers_10": [{"field": "buyer_clusters_estimate", "operator": ">=", "value": 10}],
    "volume_1000": [{"field": "adjusted_volume_usdc", "operator": ">=", "value": 1000}],
    "runner_65_and_buyers_10": [
        {"field": "runner_score", "operator": ">=", "value": 65},
        {"field": "buyer_clusters_estimate", "operator": ">=", "value": 10},
    ],
    "runner_65_and_volume_1000": [
        {"field": "runner_score", "operator": ">=", "value": 65},
        {"field": "adjusted_volume_usdc", "operator": ">=", "value": 1000},
    ],
    "buyers_10_and_volume_1000": [
        {"field": "buyer_clusters_estimate", "operator": ">=", "value": 10},
        {"field": "adjusted_volume_usdc", "operator": ">=", "value": 1000},
    ],
}


def matches_rule(rule_name: str, features: dict) -> bool:
    """Apply the exact stable rule named by a saved evaluation report."""
    for name, _family, predicate in RULES:
        if name == rule_name:
            return bool(predicate(features))
    raise ValueError(f"unknown evaluation rule: {rule_name}")


def _metrics(rows: list[dict], predicate: Callable[[dict], bool]) -> dict:
    tp = fp = tn = fn = 0
    for row in rows:
        predicted = bool(predicate(row))
        actual = bool(row["survived"])
        if predicted and actual:
            tp += 1
        elif predicted:
            fp += 1
        elif actual:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    false_positive_rate = fp / (fp + tn) if fp + tn else None
    return {
        "rows": len(rows),
        "alerts": tp + fp,
        "confusion_matrix": {
            "true_positive": tp,
            "false_positive": fp,
            "true_negative": tn,
            "false_negative": fn,
        },
        "precision": precision,
        "recall": recall,
        "false_positive_rate": false_positive_rate,
        "false_alerts": fp,
    }


def _select_rule(train: list[dict]) -> tuple[Rule, list[dict]]:
    evaluated = []
    for priority, (name, family, predicate) in enumerate(RULES):
        metrics = _metrics(train, predicate)
        evaluated.append({"name": name, "family": family, **metrics})
    # Rules that never alert cannot win by an undefined precision. Stable priority
    # makes the documented runner rule win exact ties.
    best_index = max(
        range(len(RULES)),
        key=lambda index: (
            evaluated[index]["precision"] if evaluated[index]["precision"] is not None else -1,
            evaluated[index]["recall"] if evaluated[index]["recall"] is not None else -1,
            -evaluated[index]["false_alerts"],
            -index,
        ),
    )
    return RULES[best_index], evaluated


def _split(rows: list[dict], split_at: datetime | None, min_train: int,
           min_test: int) -> tuple[datetime | None, list[dict], list[dict], int, int]:
    if not rows:
        return split_at, [], [], 0, 0
    rows = sorted(rows, key=lambda row: (row["feature_cutoff"], row["token"]))
    if split_at is None:
        candidates = sorted({parse_time(row["feature_cutoff"]) for row in rows})
        viable = []
        for candidate in candidates:
            train = [row for row in rows if parse_time(row["label_end"]) <= candidate]
            test = [row for row in rows if parse_time(row["feature_cutoff"]) >= candidate]
            if len(train) >= min_train and len(test) >= min_test:
                viable.append((abs(len(train) / len(rows) - 0.70), -len(train), candidate))
        split_at = min(viable)[2] if viable else candidates[max(0, int(len(candidates) * 0.70) - 1)]
    else:
        split_at = utc(split_at)

    train = [row for row in rows if parse_time(row["label_end"]) <= split_at]
    raw_test = [row for row in rows if parse_time(row["feature_cutoff"]) >= split_at]
    train_tokens = {row["token"] for row in train}
    test = [row for row in raw_test if row["token"] not in train_tokens]
    overlap_removed = len(raw_test) - len(test)
    selected_ids = {(row["token"], row["feature_cutoff"]) for row in train + raw_test}
    purged = len(rows) - len(selected_ids)
    return split_at, train, test, purged, overlap_removed


def _latest_liquidity(store: Store, token: str, pools: set[str], at: datetime) -> Decimal | None:
    # liquidity_stats uses a strict pre-cutoff boundary, so one microsecond makes
    # a snapshot at the trade timestamp available without looking into the future.
    result = liquidity_stats(
        store.events("liquidity", token=token, before=at + timedelta(microseconds=1)),
        pools,
        at + timedelta(microseconds=1),
    )
    return Decimal(result["liquidity_usdc"]) if result["liquidity_usdc"] is not None else None


def _fresh_risk(store: Store, token: str, at: datetime):
    rows = store.events("risk", token=token, before=at + timedelta(microseconds=1))
    if not rows:
        return None
    latest = rows[-1]
    if at - latest.ts > RISK_MAX_AGE or not latest.evidence.strip():
        return None
    return latest


def _execution_for_row(store: Store, row: dict, *, fee_bps: int, slippage_bps: int,
                       position_usdc: Decimal, latency_seconds: int) -> dict:
    launch = origins(store, parse_time(row["label_end"])).get(row["token"])
    if launch is None:
        return {"status": "no_launch", "conditional_pnl_usdc": None,
                "verified_executable_pnl_usdc": None, "unknowns": ["launch_missing"]}
    cutoff = parse_time(row["feature_cutoff"])
    end = parse_time(row["label_end"])
    all_trades = store.events("trade", token=row["token"], before=end)
    candidate_start = cutoff + timedelta(seconds=latency_seconds)
    entry = None
    for item in all_trades:
        if item.ts < candidate_start or item.wallet == launch.creator or item.quote_amount < 10:
            continue
        prefix = [prior for prior in all_trades if prior.ts <= item.ts]
        if item.id not in suspicious_trades(prefix):
            entry = item
            break
    if entry is None:
        return {"status": "no_entry_and_terminal_exit", "conditional_pnl_usdc": None,
                "verified_executable_pnl_usdc": None, "unknowns": ["fill_path_unobserved"]}
    terminal_start = end - timedelta(minutes=15)
    terminal_flagged = suspicious_trades(all_trades)
    terminal = [
        item for item in all_trades
        if item.ts >= max(entry.ts, terminal_start) and item.wallet != launch.creator
        and item.quote_amount >= 10 and item.id not in terminal_flagged
    ]
    if not terminal or terminal[-1].ts <= entry.ts:
        return {"status": "terminal_price_unavailable", "conditional_pnl_usdc": None,
                "verified_executable_pnl_usdc": None,
                "unknowns": ["fresh_terminal_price_unknown"]}
    exit_trade = terminal[-1]
    pools = {item.pool for item in all_trades}
    if launch.pool:
        pools.add(launch.pool)
    entry_liquidity = _latest_liquidity(store, row["token"], pools, entry.ts)
    exit_liquidity = _latest_liquidity(store, row["token"], pools, exit_trade.ts)
    if entry_liquidity is None or exit_liquidity is None:
        return {"status": "fresh_liquidity_unavailable", "conditional_pnl_usdc": None,
                "verified_executable_pnl_usdc": None, "unknowns": ["fresh_liquidity_unknown"]}
    capacity = min(entry_liquidity, exit_liquidity) * PARTICIPATION_CAP
    if position_usdc > capacity:
        return {"status": "participation_cap_exceeded", "conditional_pnl_usdc": None,
                "verified_executable_pnl_usdc": None, "unknowns": ["insufficient_historical_liquidity"],
                "max_position_usdc": float(capacity)}

    entry_risk = _fresh_risk(store, row["token"], entry.ts)
    exit_risk = _fresh_risk(store, row["token"], exit_trade.ts)
    unknowns = []
    if entry_risk is None or exit_risk is None \
            or entry_risk.buy_tax_bps is None or exit_risk.sell_tax_bps is None:
        unknowns.append("tax_unknown")
    if entry_risk is None or exit_risk is None \
            or entry_risk.dangerous_permissions is None or exit_risk.dangerous_permissions is None:
        unknowns.append("sellability_unknown")
    else:
        unknowns.append("sellability_unverified")
        if entry_risk.dangerous_permissions or exit_risk.dangerous_permissions:
            unknowns.append("dangerous_permissions_observed")
    if entry_risk is None or exit_risk is None \
            or entry_risk.lp_locked is None or exit_risk.lp_locked is None:
        unknowns.append("lp_status_unknown")
    elif not entry_risk.lp_locked or not exit_risk.lp_locked:
        unknowns.append("lp_unlocked_observed")
    buy_tax = Decimal(
        entry_risk.buy_tax_bps if entry_risk and entry_risk.buy_tax_bps is not None else 0
    ) / 10_000
    sell_tax = Decimal(
        exit_risk.sell_tax_bps if exit_risk and exit_risk.sell_tax_bps is not None else 0
    ) / 10_000
    friction = Decimal(fee_bps + slippage_bps) / 10_000
    entry_price = entry.price * (1 + friction + buy_tax)
    exit_price = exit_trade.price * (1 - friction - sell_tax)
    proceeds = position_usdc / entry_price * exit_price
    pnl = proceeds - position_usdc
    return {
        "status": "conditional_only",
        "entry_at": iso(entry.ts),
        "exit_at": iso(exit_trade.ts),
        "entry_price_usdc": float(entry.price),
        "exit_price_usdc": float(exit_trade.price),
        "exit_price_interpretation": "terminal observed trade price, not a known fill",
        "entry_liquidity_usdc": float(entry_liquidity),
        "exit_liquidity_usdc": float(exit_liquidity),
        "position_usdc": float(position_usdc),
        "conditional_pnl_usdc": round(float(pnl), 8),
        "verified_executable_pnl_usdc": None,
        "unknowns": unknowns,
    }


def _execution(store: Store, rows: list[dict], predicate: Callable[[dict], bool], *,
               fee_bps: int, slippage_bps: int, position_usdc: Decimal,
               latency_seconds: int) -> tuple[dict, dict[str, dict]]:
    positions = {}
    for row in rows:
        if predicate(row):
            positions[row["token"]] = _execution_for_row(
                store, row, fee_bps=fee_bps, slippage_bps=slippage_bps,
                position_usdc=position_usdc, latency_seconds=latency_seconds,
            )
    modeled = [item for item in positions.values() if item["conditional_pnl_usdc"] is not None]
    unknowns = sorted({reason for item in positions.values() for reason in item["unknowns"]})
    aggregate = {
        "alerts": len(positions),
        "modeled_positions": len(modeled),
        "conditional_pnl_usdc": round(sum(item["conditional_pnl_usdc"] for item in modeled), 8)
        if modeled else None,
        "verified_executable_pnl_usdc": None,
    }
    return {
        "assumptions": {
            "entry": "first eligible trade at or after alert latency",
            "exit": "last eligible trade before horizon end",
            "terminal_price_window_minutes": 15,
            "risk_evidence_max_age_minutes": 15,
            "fee_bps_per_side": fee_bps,
            "slippage_bps_per_side": slippage_bps,
            "position_usdc": float(position_usdc),
            "latency_seconds": latency_seconds,
            "liquidity_participation_cap": float(PARTICIPATION_CAP),
        },
        "aggregate": aggregate,
        "unknowns": unknowns,
        "positions": positions,
        "interpretation": (
            "Conditional historical scenario only. Trade prints and liquidity snapshots do not prove fills; "
            "unknown tax or sellability prevents a verified executable PnL claim."
        ),
    }, positions


def _safe_cell(value: object) -> object:
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _write_exports(folder: str, report: dict, export_rows: list[dict]) -> None:
    path = Path(folder)
    path.mkdir(parents=True, exist_ok=True)
    json_path = path / "evaluation.json"
    json_tmp = path / ".evaluation.json.tmp"
    json_tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    json_tmp.replace(json_path)

    fields = ["token", "symbol", "feature_cutoff", "label_end", "split", "survived",
              "predicted", "execution_status", "conditional_pnl_usdc"]
    csv_path = path / "evaluation.csv"
    csv_tmp = path / ".evaluation.csv.tmp"
    with csv_tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in export_rows:
            writer.writerow({key: _safe_cell(row.get(key)) for key in fields})
    csv_tmp.replace(csv_path)


def _validate(horizon_hours: int, feature_minutes: int, min_train: int, min_test: int,
              fee_bps: int, slippage_bps: int, position_usdc: float,
              latency_seconds: int) -> None:
    if horizon_hours not in (1, 6, 24, 72):
        raise ValueError("horizon_hours must be one of 1, 6, 24, 72")
    if feature_minutes <= 0 or feature_minutes >= horizon_hours * 60:
        raise ValueError("feature_minutes must be positive and before the label horizon")
    if min_train <= 0 or min_test <= 0:
        raise ValueError("min_train and min_test must be positive")
    if not 0 <= fee_bps < 10_000 or not 0 <= slippage_bps < 10_000:
        raise ValueError("fee_bps and slippage_bps must be in [0, 10000)")
    if position_usdc <= 0:
        raise ValueError("position_usdc must be positive")
    if latency_seconds < 0:
        raise ValueError("latency_seconds must be nonnegative")


def evaluate_research(
    store: Store,
    folder: str,
    observed_until: datetime,
    *,
    split_at: datetime | None = None,
    horizon_hours: int = 1,
    feature_minutes: int = 15,
    min_train: int = 10,
    min_test: int = 10,
    fee_bps: int = 30,
    slippage_bps: int = 100,
    position_usdc: float = 100,
    latency_seconds: int = 120,
) -> dict:
    """Evaluate fixed rules on an independent, purged chronological hold-out."""
    _validate(horizon_hours, feature_minutes, min_train, min_test, fee_bps,
              slippage_bps, position_usdc, latency_seconds)
    observed_until = utc(observed_until)
    rows = research_rows(store, observed_until, horizons=(horizon_hours,),
                         feature_minutes=(feature_minutes,))
    censor_reasons = Counter()
    eligible = []
    for row in rows:
        if not row["feature_coverage"]:
            censor_reasons["incomplete_feature_coverage"] += 1
        elif row["survived"] is None:
            censor_reasons[row.get("reason") or "unknown_label"] += 1
        else:
            eligible.append(row)

    split_at, train, test, purged, overlap_removed = _split(
        eligible, split_at, min_train, min_test
    )
    config = {
        "horizon_hours": horizon_hours,
        "feature_minutes": feature_minutes,
        "min_train": min_train,
        "min_test": min_test,
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "position_usdc": position_usdc,
        "latency_seconds": latency_seconds,
        "split_at": iso(split_at) if split_at else None,
    }
    config_hash = hashlib.sha256(canonical(config).encode()).hexdigest()
    data_hash = dataset_fingerprint(store, observed_until)
    fingerprints = {
        "protocol": PROTOCOL,
        "config_sha256": config_hash,
        "data_sha256": data_hash,
    }
    report = {
        "protocol": PROTOCOL,
        "mode": store.mode,
        "as_of": iso(observed_until),
        "status": "insufficient_data",
        "configuration": config,
        "fingerprints": fingerprints,
        "sample_counts": {
            "launch_rows": len(rows),
            "eligible": len(eligible),
            "censored_or_incomplete": len(rows) - len(eligible),
            "train": len(train),
            "test": len(test),
        },
        "censor_reasons": dict(sorted(censor_reasons.items())),
        "split": {
            "split_at": iso(split_at) if split_at else None,
            "train_label_end_at_or_before_split": True,
            "test_feature_cutoff_at_or_after_split": True,
            "purged_rows": purged,
            "token_overlap": len({row["token"] for row in train} & {row["token"] for row in test}),
            "overlap_rows_removed": overlap_removed,
        },
        "notification_eligible": False,
        "notification_reasons": ["insufficient_independent_train_test"],
        "limitations": [
            "The target is the existing activity-based survival label, not future maximum price.",
            "Rows without complete feature and label coverage are excluded and counted by reason.",
            "Rule choice uses training data only; the test partition remains held out.",
            "Execution outputs are conditional scenarios and are not verified executable returns.",
        ],
    }
    export_rows = []
    if len(train) >= min_train and len(test) >= min_test:
        selected, rule_results = _select_rule(train)
        name, family, predicate = selected
        train_metrics = _metrics(train, predicate)
        test_metrics = _metrics(test, predicate)
        execution, positions = _execution(
            store, test, predicate, fee_bps=fee_bps, slippage_bps=slippage_bps,
            position_usdc=Decimal(str(position_usdc)), latency_seconds=latency_seconds,
        )
        coverage = len(eligible) / len(rows) if rows else 0.0
        reasons = []
        if len(train) < 10 or len(test) < 10:
            reasons.append("insufficient_independent_train_test")
        if test_metrics["precision"] is None or test_metrics["precision"] < MIN_NOTIFICATION_PRECISION:
            reasons.append("insufficient_precision")
        if coverage < MIN_NOTIFICATION_COVERAGE:
            reasons.append("insufficient_coverage")
        if test_metrics["alerts"] == 0:
            reasons.append("insufficient_test_alerts")
        if execution["aggregate"]["verified_executable_pnl_usdc"] is None \
                or execution["aggregate"]["modeled_positions"] < execution["aggregate"]["alerts"] \
                or execution["unknowns"]:
            reasons.append("uncertain_execution")
        report.update({
            "status": "evaluated",
            "selection": {
                "rule": name,
                "family": family,
                "parameters": RULE_PARAMETERS[name],
                "selected_on": "train_only",
                "objective": "precision, then recall, then fewer false alerts; stable rule priority breaks ties",
                "candidate_train_results": rule_results,
            },
            "train": train_metrics,
            "test": test_metrics,
            "eligible_coverage": coverage,
            "execution": execution,
            "notification_eligible": not reasons,
            "notification_reasons": reasons,
        })
        for split_name, partition in (("train", train), ("test", test)):
            for row in partition:
                item = {
                    **row,
                    "split": split_name,
                    "predicted": predicate(row),
                    "execution_status": None,
                    "conditional_pnl_usdc": None,
                }
                position = positions.get(row["token"])
                if position:
                    item["execution_status"] = position["status"]
                    item["conditional_pnl_usdc"] = position["conditional_pnl_usdc"]
                export_rows.append(item)
    else:
        export_rows = [{**row, "split": "excluded", "predicted": None,
                        "execution_status": None, "conditional_pnl_usdc": None} for row in rows]

    report["fingerprints"]["report_sha256"] = _report_sha256(report)
    report["fingerprints"]["gate_sha256"] = _gate_sha256(report["fingerprints"])
    _write_exports(folder, report, export_rows)
    return report
