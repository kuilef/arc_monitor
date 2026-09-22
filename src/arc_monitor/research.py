"""Censored labels and descriptive exports, not a fitted trading strategy backtest."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .models import Launch, iso
from .storage import Store
from .wash import suspicious_trades


def outcome(store: Store, launch: Launch, hours: int, observed_until: datetime) -> dict:
    if hours not in (1, 6, 24, 72):
        raise ValueError('supported horizons: 1, 6, 24, 72 hours')
    end = launch.ts+timedelta(hours=hours)
    result = {'horizon_hours': hours, 'survived': None, 'reason': None}
    if launch.origin != 'launch_event':
        result['reason'] = 'unknown_launch_time'
        return result
    if end > observed_until:
        result['reason'] = 'immature'
        return result
    if not store.covered('trades',launch.ts,end):
        result['reason'] = 'incomplete_provider_coverage'
        return result
    start = end-timedelta(minutes=15 if hours == 1 else 60)
    trades = store.events('trade',token=launch.token,before=end)
    flagged = suspicious_trades(trades)
    eligible = [t for t in trades if t.ts>=start and t.wallet!=launch.creator
                and t.id not in flagged and t.quote_amount>=1]
    first = {}
    for t in eligible:
        first.setdefault(t.wallet,t.ts)
    from .metrics import clusters, liquidity_stats
    proxy, _ = clusters(first,store.events('funding',before=end))
    survived = len(set(proxy.values())) >= 2
    if hours == 6:
        pools = {t.pool for t in trades}
        if launch.pool:
            pools.add(launch.pool)
        liquidity = liquidity_stats(store.events('liquidity',token=launch.token,before=end),pools,end)
        if liquidity['liquidity_usdc'] is None:
            result['reason'] = 'liquidity_unavailable'
            return result
        survived = survived and Decimal(liquidity['liquidity_usdc']) >= 1000
    result['survived'] = survived
    result['reason'] = 'observed' if survived else 'below_defined_activity_threshold'
    return result


def future_label(store: Store, launch: Launch, cutoff: datetime, hours: int,
                 observed_until: datetime) -> dict:
    """Only prices AFTER the feature cutoff contribute to the label.

    Reference is the last >=10 USDC, non-creator, non-flagged trade before cutoff.
    These are observed price multiples, not feasible fill prices or strategy P&L.
    """
    end = launch.ts+timedelta(hours=hours)
    result = dict(future_price_5x=None, future_price_10x=None, future_price_50x=None,
                  future_max_multiple=None, label_reason=None,
                  label_reference='last eligible trade before feature cutoff')
    if cutoff >= end or end > observed_until or not store.covered('trades',launch.ts,end):
        result['label_reason'] = 'immature_or_incomplete'
        return result
    prior = store.events('trade',token=launch.token,before=cutoff)
    from .metrics import baseline
    if baseline(prior,launch.creator,store.events('funding',before=cutoff))['price_usdc'] is None:
        result['label_reason'] = 'ten_buyer_baseline_not_reached_at_cutoff'
        return result
    bad = suspicious_trades(prior)
    prices = [t for t in prior if t.id not in bad and t.wallet!=launch.creator and t.quote_amount>=10]
    if not prices:
        result['label_reason'] = 'no_eligible_reference_price'
        return result
    reference = prices[-1].price
    future = store.events('trade',token=launch.token,since=cutoff,before=end)
    bad_future = suspicious_trades(prior+future)
    eligible = [t.price for t in future if t.id not in bad_future and t.wallet!=launch.creator and t.quote_amount>=10]
    if not eligible:
        result['label_reason'] = 'no_future_eligible_trade'
        return result
    multiple = max(eligible)/reference
    result.update(future_max_multiple=float(multiple), future_price_5x=multiple>=5,
                  future_price_10x=multiple>=10, future_price_50x=multiple>=50,
                  label_reason='observed_price_only_no_execution_model')
    return result


def purged_time_split(rows: list[dict], split_at: datetime) -> tuple[list[dict], list[dict]]:
    """Training outcomes must end before split; hold-out features must start after it."""
    from .models import parse_time
    train = [r for r in rows if parse_time(r['label_end']) <= split_at]
    test = [r for r in rows if parse_time(r['feature_cutoff']) >= split_at]
    return train, test


def research_rows(store: Store, observed_until: datetime, horizons=(1,6,24,72),
                  feature_minutes=(5,15,30,60)) -> list[dict]:
    from .metrics import origins, snapshot
    result = []
    for launch in origins(store,observed_until).values():
        if launch.origin != 'launch_event':
            continue
        for minutes in feature_minutes:
            cutoff = launch.ts+timedelta(minutes=minutes)
            if cutoff>observed_until:
                continue
            features = snapshot(store,launch.token,cutoff)
            for horizon in horizons:
                if minutes >= horizon*60:
                    continue
                row = {k:features[k] for k in ('token','symbol','launchpad','unique_buyers',
                    'new_buyers_5m','buyer_clusters_estimate','funding_coverage','runner_score',
                    'risk_score','wash_score','raw_volume_usdc','adjusted_volume_usdc','liquidity_usdc')}
                row.update(feature_cutoff=iso(cutoff), feature_minutes=minutes,
                           feature_coverage=store.covered('trades',launch.ts,cutoff),
                           label_end=iso(launch.ts+timedelta(hours=horizon)),
                           **outcome(store,launch,horizon,observed_until),
                           **future_label(store,launch,cutoff,horizon,observed_until))
                result.append(row)
    return result


def export_research(store: Store, folder: str, observed_until: datetime) -> dict:
    path = Path(folder)
    path.mkdir(parents=True,exist_ok=True)
    rows = research_rows(store,observed_until)
    with (path/'features_and_outcomes.csv').open('w',encoding='utf-8-sig',newline='') as f:
        if rows:
            writer = csv.DictWriter(f,fieldnames=list(rows[0]))
            writer.writeheader()
            for row in rows:
                # CSV spreadsheet formula injection protection for untrusted ticker metadata.
                writer.writerow({k: "'"+v if isinstance(v,str) and v[:1] in ('=','+','-','@','\t','\r') else v
                                 for k,v in row.items()})
    summaries = []
    for minutes in (5,15,30,60):
        for horizon in (1,6,24,72):
            cohort = [r for r in rows if r['feature_minutes']==minutes and r['horizon_hours']==horizon]
            if not cohort:
                continue
            covered = [r for r in cohort if r['feature_coverage'] and r['survived'] is not None]
            summaries.append({'feature_minutes':minutes,'horizon_hours':horizon,'rows':len(cohort),
                'eligible':len(covered),'censored_or_incomplete':len(cohort)-len(covered),
                'survival_rate':sum(r['survived'] for r in covered)/len(covered) if covered else None})
    report = {'mode':store.mode,'as_of':iso(observed_until),'rows':len(rows),'cohorts':summaries,
        'interpretation':'Descriptive event-time research, not fitted prediction or executable P&L.',
        'limitations':['Synthetic results do not establish market alpha.' if store.mode=='demo' else 'Vendor scope is not all-chain completeness.',
                        'No slippage, tax, MEV, transaction costs or delivery-latency model.',
                        'One token appears at multiple cutoffs; rows are not independent samples.',
                        'Use purged_time_split to separate all training labels from hold-out feature times.']}
    (path/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    return report
