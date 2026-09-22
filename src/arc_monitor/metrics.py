"""Event-time features with explicit units, uncertainty and risk-evidence coverage."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
import math

from .models import BURN, ZERO, Funding, Holders, Launch, Liquidity, Trade, iso, utc
from .storage import Store
from .wash import suspicious_trades, wash_stats

SCORE_VERSION = 'heuristic-v1-unvalidated'
ZERO_D = Decimal(0)


def decimal_text(value: Decimal | None) -> str | None:
    return format(value, 'f') if value is not None else None


def clusters(first: dict[str, datetime], funds: list[Funding]) -> tuple[dict[str, str], float]:
    """One-hop first-observed-funder proxy. Never claims distinct people.

    Funding after a wallet's first eligible buy is not used. Explicit hubs are not merged.
    Unobserved funding leaves a wallet separate and lowers reported funding coverage.
    """
    earliest = {}
    for f in sorted(funds, key=lambda f: (f.ts, f.id)):
        if f.wallet in first and f.ts <= first[f.wallet] and f.wallet not in earliest:
            earliest[f.wallet] = f
    result = {}
    for wallet in first:
        fund = earliest.get(wallet)
        result[wallet] = 'funder:' + fund.funder if fund and not fund.hub else 'wallet:' + wallet
    return result, len(earliest) / len(first) if first else 0.0


def holder_stats(state: Holders | None) -> dict:
    result = dict(holders_count=None, top1_eligible_share=None, top5_eligible_share=None,
                  top10_eligible_share=None, top100_eligible_share=None, top10_supply_share=None)
    if state is None or not state.complete:
        return result
    eligible = sorted((a.balance for a in state.accounts if a.role == 'wallet'
                       and a.wallet not in {ZERO, BURN} and a.balance > 0), reverse=True)
    denominator = sum(eligible, ZERO_D)
    result['holders_count'] = len(eligible)
    if denominator:
        for n in (1, 5, 10, 100):
            result[f'top{n}_eligible_share'] = float(sum(eligible[:n], ZERO_D) / denominator)
        if state.total_supply:
            result['top10_supply_share'] = float(sum(eligible[:10], ZERO_D) / state.total_supply)
    return result


def liquidity_stats(rows: list[Liquidity], pools: set[str], cutoff: datetime,
                    max_age_minutes: int = 15) -> dict:
    latest = {}
    peak = ZERO_D
    for row in sorted((r for r in rows if r.ts < cutoff), key=lambda r: (r.ts, r.id)):
        latest[row.pool] = row
        total = sum((v.amount_usdc for v in latest.values()
                     if row.ts-v.ts <= timedelta(minutes=max_age_minutes)), ZERO_D)
        peak = max(peak, total)
    known_pools = pools | set(latest)
    fresh = {p: r for p, r in latest.items() if cutoff-r.ts <= timedelta(minutes=max_age_minutes)}
    complete = bool(known_pools) and known_pools.issubset(fresh)
    current = sum((fresh[p].amount_usdc for p in known_pools), ZERO_D) if complete else None
    return {'liquidity_usdc': decimal_text(current), 'peak_observed_liquidity_usdc': decimal_text(peak) if latest else None,
            'liquidity_retention': float(current / peak) if current is not None and peak else None,
            'liquidity_pools_known': len(known_pools), 'liquidity_pools_fresh': len(fresh),
            'liquidity_valuation': sorted({r.valuation for r in fresh.values()})}


def baseline(trades: list[Trade], creator: str | None, funds: list[Funding],
             min_buyers: int = 10, minimum_quote: Decimal = Decimal('10')) -> dict:
    """First crossing of 10 funder-proxy buyers; no future round trips modify that crossing."""
    first = {}
    prefix = []
    for trade in sorted(trades, key=lambda t: (t.ts, t.id)):
        prefix.append(trade)
        if trade.side != 'buy' or trade.wallet == creator or trade.quote_amount < minimum_quote:
            continue
        first.setdefault(trade.wallet, trade.ts)
        if len(first) < min_buyers:
            continue
        flagged = suspicious_trades(prefix)
        eligible = {}
        for t in prefix:
            if t.side == 'buy' and t.wallet != creator and t.quote_amount >= minimum_quote and t.id not in flagged:
                eligible.setdefault(t.wallet, t.ts)
        proxy, _ = clusters(eligible, funds)
        if trade.id not in flagged and len(set(proxy.values())) >= min_buyers:
            return {'price_usdc': decimal_text(trade.price), 'timestamp': iso(trade.ts),
                    'rule': '10 funder-proxy buyers, each buy >=10 USDC'}
    return {'price_usdc': None, 'timestamp': None, 'rule': 'baseline not reached'}


def origins(store: Store, cutoff: datetime) -> dict[str, Launch]:
    result = {}
    for item in store.events('launch', before=cutoff):
        old = result.get(item.token)
        if old is None or (old.origin == 'first_trade' and item.origin == 'launch_event'):
            result[item.token] = item
        elif old.origin == item.origin and item.ts < old.ts:
            result[item.token] = item
    return result


def snapshot(store: Store, token: str, cutoff: datetime, *, include_creator: bool = True) -> dict:
    cutoff = utc(cutoff)
    token = token.lower()
    launch = origins(store, cutoff).get(token)
    if launch is None:
        raise KeyError(token)
    trades = store.events('trade', token=token, before=cutoff)
    window_start = cutoff - timedelta(minutes=30)
    window = [t for t in trades if t.ts >= window_start]
    wash = wash_stats(window)
    full_flagged = suspicious_trades(trades)
    external = [t for t in trades if t.wallet != launch.creator and t.id not in full_flagged]
    first = {}
    for t in external:
        if t.side == 'buy' and t.quote_amount >= Decimal('1'):
            first.setdefault(t.wallet, t.ts)
    funds = store.events('funding', before=cutoff)
    proxy, funding_coverage = clusters(first, funds)
    proxy_first = {}
    for wallet, group in proxy.items():
        proxy_first[group] = min(proxy_first.get(group, first[wallet]), first[wallet])
    new_proxy = sum(cutoff-timedelta(minutes=5) <= ts < cutoff for ts in proxy_first.values())
    new = sum(cutoff-timedelta(minutes=5) <= ts < cutoff for ts in first.values())
    previous = sum(cutoff-timedelta(minutes=10) <= ts < cutoff-timedelta(minutes=5) for ts in first.values())
    net = sum((t.quote_amount if t.side == 'buy' else -t.quote_amount for t in window), ZERO_D)
    pools = {t.pool for t in trades}
    if launch.pool:
        pools.add(launch.pool)
    liquidity = liquidity_stats(store.events('liquidity', token=token, before=cutoff), pools, cutoff)
    holders = store.events('holders', token=token, before=cutoff)
    h = holders[-1] if holders and cutoff-holders[-1].ts <= timedelta(hours=1) else None
    stats = holder_stats(h)
    risks = store.events('risk', token=token, before=cutoff)
    r = risks[-1] if risks and cutoff-risks[-1].ts <= timedelta(hours=1) else None
    contract_fields = ['buy_tax_bps', 'sell_tax_bps', 'dangerous_permissions', 'lp_locked']
    contract_known = r is not None and all(getattr(r, k) is not None for k in contract_fields) and bool(r.evidence)
    missing = []
    if not contract_known:
        missing.append('contract_risk')
    if stats['holders_count'] is None:
        missing.append('complete_holders')
    if liquidity['liquidity_usdc'] is None:
        missing.append('fresh_liquidity')
    if not window:
        missing.append('recent_trades')
    observed_risk = 100.0
    if first:
        observed_risk -= (1-len(proxy_first)/len(first))*35
    if wash['wash_score'] is not None:
        observed_risk -= wash['wash_score'] * .4
    if stats['top10_eligible_share'] is not None:
        observed_risk -= max(0, stats['top10_eligible_share'] - .2) * 40
    if r is not None:
        observed_risk -= min(30, ((r.sell_tax_bps or 0)+(r.buy_tax_bps or 0)) / 100)
        observed_risk -= 60 if r.dangerous_permissions else 0
        observed_risk -= 15 if r.lp_locked is False else 0
    if liquidity['liquidity_retention'] is not None:
        observed_risk -= max(0, .8-liquidity['liquidity_retention']) * 30
    observed_risk = round(max(0, min(100, observed_risk)))
    components = {
        'new_buyers': min(30, new_proxy * 1.5),
        'external_buyers': min(20, len(proxy_first) * .5),
        'adjusted_volume': min(20, math.log10(1 + float(wash['adjusted'])) * 4),
        'buy_flow': max(0, min(15, float(net / wash['raw']) * 15)) if wash['raw'] else 0,
        'liquidity_retention': min(15, max(0, liquidity['liquidity_retention'] or 0) * 15),
    }
    runner = round(sum(components.values()))
    price = trades[-1].price if trades else None
    price_age = (cutoff-trades[-1].ts).total_seconds() if trades else None
    baseline_info = baseline(trades, launch.creator, funds)
    multiple = float(price / Decimal(baseline_info['price_usdc'])) if price and baseline_info['price_usdc'] else None
    peak = max((t.price for t in trades), default=None)
    age_minutes = (cutoff-launch.ts).total_seconds() / 60
    risk_score = observed_risk if not missing else None
    stage = 'NEW' if age_minutes < 10 else 'TRIAGE'
    if len(proxy_first) >= 10:
        stage = 'WATCH'
    if runner >= 65 and risk_score is not None and risk_score >= 60 and Decimal(liquidity['liquidity_usdc']) >= 1000:
        stage = 'RUNNER' if multiple and multiple >= 5 else 'CANDIDATE'
    if age_minutes >= 60 and len(first) < 5:
        stage = 'LOW_ACTIVITY'  # a monitoring priority, never a survival outcome
    # Mature first-ten-minute buyers returning at least 30 min after their first buy.
    cohort = {w: ts for w, ts in first.items() if ts < launch.ts+timedelta(minutes=10)}
    retention = None
    if cutoff >= launch.ts+timedelta(minutes=40) and cohort:
        returned = {t.wallet for t in external if t.wallet in cohort
                    and t.ts >= cohort[t.wallet]+timedelta(minutes=30)}
        retention = len(returned) / len(cohort)
    quality = ['Scores are uncalibrated heuristics; adjusted volume is not proven organic volume.',
               'Funding clusters represent a proxy, not unique people.',
               'Event-time replay does not simulate API delivery delays.']
    if any(t.identity_quality == 'row_fingerprint' for t in window):
        quality.append('Trade identity uses row fingerprints; identical genuine legs can be conflated.')
    if launch.origin == 'first_trade':
        quality.append('Launch time is unknown; displayed age is since first observed USDC trade.')
    if not store.covered('trades', launch.ts, cutoff):
        quality.append('Provider coverage is incomplete; buyer and volume features may be undercounted.')
    if price_age is not None and price_age > 900:
        quality.append('The last observed trade price is stale (>15 min).')
    result = dict(token=token, symbol=launch.symbol or (trades[-1].symbol if trades else ''),
        launchpad=launch.launchpad, creator=launch.creator, origin=launch.origin,
        launch_at=iso(launch.ts), as_of=iso(cutoff), age_minutes=round(age_minutes, 1), stage=stage,
        price_usdc=decimal_text(price), price_age_seconds=price_age,
        fdv_usdc=decimal_text(price*h.total_supply) if price and h and h.total_supply else None,
        market_cap_usdc=decimal_text(price*h.circulating_supply) if price and h and h.circulating_supply else None,
        raw_volume_usdc=decimal_text(wash['raw']), adjusted_volume_usdc=decimal_text(wash['adjusted']),
        net_buy_flow_usdc=decimal_text(net), window_minutes=30,
        trade_rows=len(window), transactions=len({t.tx for t in window}),
        unique_buyers=len(first), unique_traders=len({t.wallet for t in window}),
        buyer_clusters_estimate=len(set(proxy.values())), funding_coverage=funding_coverage,
        new_buyers_5m=new, new_buyer_clusters_5m=new_proxy, new_buyers_previous_5m=previous,
        buyer_acceleration=new / previous if previous else None,
        emerging_from_zero=bool(new and not previous), buyer_retention_30m=retention,
        wash_score=wash['wash_score'], suspicious_volume_share=wash['suspicious_share'],
        top_trader_volume_share=wash['top_trader_share'],
        risk_score=risk_score, observed_risk_score=observed_risk, missing_evidence=missing,
        risk_evidence_coverage=(4-len(missing))/4, runner_score=runner,
        runner_components=components, score_version=SCORE_VERSION,
        baseline=baseline_info, price_multiple_from_baseline=multiple,
        drawdown_from_observed_peak=float(price/peak-1) if price and peak else None,
        quality_notes=quality, mode=store.mode, **liquidity, **stats)
    result['creator_history'] = creator_history(store, launch, cutoff) if include_creator else None
    return result


def creator_history(store: Store, current: Launch, cutoff: datetime) -> dict | None:
    if not current.creator:
        return None
    previous = [l for l in origins(store, cutoff).values() if l.creator == current.creator
                and l.token != current.token and l.origin == 'launch_event']
    from .research import outcome
    labels = [outcome(store,l,24,cutoff)['survived'] for l in previous]
    return {'observed_other_launches': len(previous),
            'other_launches_24h': sum(l.ts >= cutoff-timedelta(hours=24) for l in previous),
            'mature_covered_24h': sum(v is not None for v in labels),
            'survived_24h': sum(v is True for v in labels),
            'censored_24h': sum(v is None for v in labels)}


def ecosystem(store: Store, cutoff: datetime) -> dict:
    start = cutoff-timedelta(hours=24)
    trades = store.events('trade', before=cutoff, since=start)
    grouped = defaultdict(list)
    for t in trades:
        grouped[t.token].append(t)
    volume = sum((wash_stats(ts)['adjusted'] for ts in grouped.values()), ZERO_D)
    flows = store.events('flow', before=cutoff, since=start)
    flow_covered = store.covered('flows', start, cutoff)
    incoming = sum((f.amount_usdc for f in flows if f.direction == 'in'), ZERO_D)
    outgoing = sum((f.amount_usdc for f in flows if f.direction == 'out'), ZERO_D)
    return {'as_of':iso(cutoff),'mode':store.mode, 'window_hours':24,
            'observed_tokens':len(origins(store,cutoff)),
            'observed_launches_24h':sum(l.ts>=start and l.origin=='launch_event' for l in origins(store,cutoff).values()),
            'active_traders_24h':len({t.wallet for t in trades}),
            'raw_volume_usdc_24h':decimal_text(sum((t.quote_amount for t in trades),ZERO_D)),
            'adjusted_volume_usdc_24h':decimal_text(volume),
            'cctp_net_usdc_24h':decimal_text(incoming-outgoing) if flow_covered else None,
            'cctp_coverage':flow_covered,'trades_coverage':store.covered('trades',start,cutoff),
            'trade_scope':'Observed non-USDC tokens in verified USDC pairs; not all Arc DEX markets'}
