"""Deterministic synthetic fixtures. These are NOT real Arc tokens or market results."""
from __future__ import annotations

import hashlib
from datetime import timedelta
from decimal import Decimal

from .models import Flow, Funding, Holder, Holders, Launch, Liquidity, Risk, Trade, parse_time, iso
from .storage import Store

DEMO_ASOF = parse_time('2026-09-22T12:00:00Z')
DEMO_START = parse_time('2026-09-16T00:00:00Z')


def addr(number: int) -> str:
    return '0x' + format(number, '040x')


def tx(key: str) -> str:
    return '0x' + hashlib.sha256(key.encode()).hexdigest()


def demo_events() -> list:
    events = []
    # symbol, age in minutes, scenario, platform; metadata is intentionally fictional.
    scenarios = [('GROW',80,'organic','Argus'), ('LOOP',90,'wash','Tolly'),
                 ('QUIET',5760,'dead','Argus'), ('DRAIN',1560,'drain','Argus'),
                 ('FUNDED',45,'cluster','RadarDEX Classic'), ('EARLY',3,'young','Warp'),
                 ('UNKNOWN',120,'generic','unknown')]
    for n, (symbol, age, scenario, platform) in enumerate(scenarios, 1):
        token, pool = addr(100+n), '0x' + format(100+n,'064x')
        creator = addr(9001 if n in [1,3,4] else 9000+n)
        launched = DEMO_ASOF-timedelta(minutes=age)
        events.append(Launch(id=f'demo-launch-{n}', token=token, ts=launched, source='demo',
            tx=tx(f'launch{n}'), creator=creator if scenario!='generic' else None,
            launchpad=platform, symbol=symbol, name=f'Synthetic {scenario}', pool=pool,
            origin='first_trade' if scenario=='generic' else 'launch_event'))
        if scenario=='dead':
            offsets=[1,2,4]
        else:
            offsets=sorted(set(range(1,min(age,61))) | set(range(max(1,age-29),age)))
            for horizon in [360,1440,4320]:
                if horizon<age:
                    offsets+=list(range(horizon-55,horizon,10))
            offsets=sorted(set(offsets))
        buyer_first={}
        for k, minute in enumerate(offsets):
            count = (1 + (4 if minute>=age-5 else 1)) if scenario=='organic' else 1
            for j in range(count):
                wallet=addr(n*10000+k*10+j+1)
                timestamp=launched+timedelta(minutes=minute,seconds=j*5)
                if timestamp>=DEMO_ASOF:
                    continue
                price=Decimal('0.01')*(1+Decimal(minute)/Decimal(15))
                if scenario=='drain' and minute>80:
                    price=Decimal('0.003')
                quote=Decimal(100+k*3+j*7)
                if scenario=='wash':
                    wallet=addr(n*10000+1)
                    price,quote=Decimal('.1'),Decimal(25000)
                key=f'demo-trade-{n}-{minute}-{j}'
                events.append(Trade(id=key,token=token,ts=timestamp,source='demo',tx=tx(key),
                    wallet=wallet,side='buy',quantity=quote/price,quote_amount=quote,pool=pool,symbol=symbol))
                buyer_first.setdefault(wallet,timestamp)
                if scenario=='wash':
                    events.append(Trade(id=key+'-sell',token=token,ts=timestamp+timedelta(seconds=5),
                        source='demo',tx=tx(key+'-sell'),wallet=wallet,side='sell',quantity=quote/price,
                        quote_amount=quote,pool=pool,symbol=symbol))
        for k,(wallet,first) in enumerate(buyer_first.items()):
            events.append(Funding(id=f'demo-funding-{n}-{k}',ts=first-timedelta(seconds=30),
                source='demo',wallet=wallet,funder=addr(80001 if scenario=='cluster' else 1000000+n*10000+k),
                amount_usdc='1000'))
        # Snapshots at actual fixture times, not retroactive observations of the current state.
        sample_times=sorted(set([launched+timedelta(minutes=1)] +
              [launched+timedelta(minutes=m) for m in offsets if m%5==0] +
              ([] if scenario=='dead' else [DEMO_ASOF-timedelta(seconds=30)])))
        for k,ts_ in enumerate(sample_times):
            minute=(ts_-launched).total_seconds()/60
            liquid=Decimal(10000)+Decimal(str(minute))*100
            if scenario=='wash': liquid=Decimal(3000)
            if scenario=='drain' and minute>80: liquid=Decimal(300)
            events.append(Liquidity(id=f'demo-liq-{n}-{k}',token=token,ts=ts_,source='demo',
                                    pool=pool,amount_usdc=liquid))
        # Only independently supplied/verified fields can enable the full risk score in live mode.
        state_time=DEMO_ASOF-timedelta(seconds=20)
        accounts=[Holder(wallet=addr(400000+n*1000+i),balance='8000') for i in range(100)]
        accounts+=[Holder(wallet=creator,balance='10000'),Holder(wallet=addr(700000+n),balance='190000',role='pool')]
        events.append(Holders(id=f'demo-holders-{n}',token=token,ts=state_time,source='demo',
            accounts=accounts,complete=scenario not in ['generic','young'],total_supply='1000000',
            circulating_supply='810000' if scenario not in ['generic','young'] else None))
        if scenario not in ['generic','young']:
            events.append(Risk(id=f'demo-risk-{n}',token=token,ts=state_time,source='demo',
                buy_tax_bps=0,sell_tax_bps=2000 if scenario=='drain' else 0,
                dangerous_permissions=scenario=='drain',lp_locked=scenario!='drain',
                evidence='Synthetic fixture, not an audited contract.'))
    for direction,amount in [('in','250000'),('out','85000')]:
        events.append(Flow(id='demo-flow-'+direction,ts=DEMO_ASOF-timedelta(hours=2),source='demo',
                           direction=direction,amount_usdc=amount,message_key='demo-message-'+direction))
    return sorted(events,key=lambda e:(e.ts,e.id))


def seed_demo(store: Store) -> dict:
    if store.mode!='demo':
        raise ValueError('demo data requires a separate database opened with --mode demo')
    events=demo_events()
    inserted=store.ingest(events)
    for stream in ['trades','launches','flows']:
        store.record_window(stream,DEMO_START,DEMO_ASOF,[],provider='synthetic-fixtures')
    return {'mode':'demo','synthetic':True,'as_of':iso(DEMO_ASOF),'inserted':inserted,
            'tokens':store.count('launch'),'trades':store.count('trade')}
