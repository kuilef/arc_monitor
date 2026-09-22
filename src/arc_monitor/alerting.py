"""Local alert candidates only. Nothing is transmitted and no orders are placed."""
from datetime import datetime

from .metrics import origins, snapshot, SCORE_VERSION
from .models import iso
from .storage import Store


def evaluate_alerts(store: Store, at: datetime, *, min_runner=65, min_risk=60,
                    cooldown_minutes=60) -> list[dict]:
    if not 0<=min_runner<=100 or not 0<=min_risk<=100 or cooldown_minutes<1:
        raise ValueError('invalid alert thresholds or cooldown')
    alerts=[]
    # Fixed time buckets, rather than sliding throttles: transparent and reproducible.
    bucket=int(at.timestamp())//(cooldown_minutes*60)
    for token in origins(store,at):
        s=snapshot(store,token,at)
        if s['risk_score'] is None or s['risk_score']<min_risk or s['runner_score']<min_runner:
            continue
        item={'token':token,'symbol':s['symbol'],'timestamp':iso(at),'mode':store.mode,
              'runner_score':s['runner_score'],'risk_score':s['risk_score'],
              'liquidity_usdc':s['liquidity_usdc'],'adjusted_volume_usdc':s['adjusted_volume_usdc'],
              'score_version':SCORE_VERSION,'message':'Research candidate, not a buy instruction.'}
        key=f'{store.mode}:{token}:{SCORE_VERSION}:{bucket}'
        if store.alert_once(key,token,item):
            alerts.append(item)
    return alerts
