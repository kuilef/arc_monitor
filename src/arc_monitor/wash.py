"""Explainable suspicion heuristics, never proof of manipulation or trader identity."""
from collections import defaultdict, deque
from decimal import Decimal

from .models import Trade


def suspicious_trades(trades: list[Trade], seconds: int = 60) -> set[str]:
    """Discount both legs of short, near-flat round trips for the same wallet/token.

    Same-transaction opposite legs are flagged even when sizes differ. Normal arbitrage
    can trigger this rule. A small net market flow alone does NOT trigger it.
    """
    recent = defaultdict(deque)
    transaction_sides = defaultdict(list)
    flagged = set()
    for trade in sorted(trades, key=lambda t: (t.ts, t.id)):
        key = (trade.token, trade.wallet)
        queue = recent[key]
        while queue and (trade.ts - queue[0].ts).total_seconds() > seconds:
            queue.popleft()
        for prior in queue:
            if prior.side != trade.side:
                ratio = min(prior.quantity, trade.quantity) / max(prior.quantity, trade.quantity)
                if ratio >= Decimal('.9'):
                    flagged.update((prior.id, trade.id))
        queue.append(trade)
        transaction_sides[(trade.token, trade.wallet, trade.tx)].append(trade)
    for group in transaction_sides.values():
        if len({t.side for t in group}) > 1:
            flagged.update(t.id for t in group)
    return flagged


def wash_stats(trades: list[Trade]) -> dict:
    gross = sum((t.quote_amount for t in trades), Decimal(0))
    flagged = suspicious_trades(trades)
    removed = sum((t.quote_amount for t in trades if t.id in flagged), Decimal(0))
    totals = defaultdict(Decimal)
    for t in trades:
        totals[t.wallet] += t.quote_amount
    top_share = max(totals.values(), default=Decimal(0)) / gross if gross else Decimal(0)
    suspicious_share = removed / gross if gross else Decimal(0)
    score = min(100, round(float(suspicious_share) * 80 + max(0, float(top_share) - .3) / .7 * 20))
    return {'raw': gross, 'adjusted': gross - removed, 'flagged': flagged,
            'suspicious_share': float(suspicious_share), 'top_trader_share': float(top_share),
            'wash_score': score if trades else None}
