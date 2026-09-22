"""Run `python -m arc_monitor --help`. Local dotenv, environment, then CLI flags."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from .models import EVENT, iso, parse_time
from .config import env_number
from .storage import Store


def emit(value):
    print(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),flush=True)


def parser() -> argparse.ArgumentParser:
    p=argparse.ArgumentParser(prog='arc-monitor',description='Read-only Arc token research and local monitoring')
    p.add_argument('--env-file',default='.env',help='Literal dotenv file; process environment takes precedence')
    p.add_argument('--no-env-file',action='store_true',help='Use process environment only')
    p.add_argument('--db',default=os.getenv('ARC_DB','data/arc.db'),help='SQLite path or SQLAlchemy database URL')
    p.add_argument('--mode',choices=['live','demo'],default=os.getenv('ARC_MODE','live'),help='Dataset identity; never mix demo/live')
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('init',help='Initialize dataset without fetching')
    d=sub.add_parser('demo',help='Load deterministic synthetic examples')
    d.add_argument('--serve',action='store_true')
    d.add_argument('--port',type=int,default=8000)
    s=sub.add_parser('serve',help='Read-only dashboard on 127.0.0.1')
    s.add_argument('--port',type=int,default=8000)
    s.add_argument('--max-tokens',type=int,default=500)
    i=sub.add_parser('import-jsonl',help='Atomic validated normalized-event import; does not assert coverage')
    i.add_argument('path')
    e=sub.add_parser('export-jsonl',help='Export normalized events')
    e.add_argument('path')
    b=sub.add_parser('backfill',help='Resumable Bitquery HTTP backfill; requires BITQUERY_TOKEN')
    b.add_argument('--start',required=True,type=parse_time)
    b.add_argument('--end',required=True,type=parse_time,help='Exclusive UTC boundary')
    b.add_argument('--stream',choices=['both','launches','trades'],default='both')
    b.add_argument('--limit',type=int,default=1000)
    b.add_argument('--max-requests',type=int,default=500)
    b.add_argument('--refresh',action='store_true',help='Requery already committed ranges for late provider data')
    b.add_argument('--window-seconds',type=int,default=300)
    poll=sub.add_parser('poll',help='Blocking live polling loop; Ctrl+C to stop')
    poll.add_argument('--interval',type=int,default=60)
    poll.add_argument('--lookback-minutes',type=int,default=2)
    poll.add_argument('--lag-seconds',type=int,default=120)
    poll.add_argument('--max-requests',type=int,default=500,help='Hard budget for the entire invocation, not each cycle')
    poll.add_argument('--cycles',type=int,default=0,help='0 means keep polling until Ctrl+C or budget exhaustion')
    poll.add_argument('--overlap-seconds',type=int,default=30)
    poll.add_argument('--limit',type=int,default=1000)
    poll.add_argument('--window-seconds',type=int,default=120)
    poll.add_argument('--max-failures',type=int,default=5)
    poll.add_argument('--enrich-every',type=int,default=10,help='Enrich every N cycles; 0 disables, first cycle included')
    poll.add_argument('--max-tokens',type=int,default=2)
    poll.add_argument('--max-wallets',type=int,default=5)
    poll.add_argument('--cctp',action='store_true',help='Reconcile CCTP in freshly collected windows')
    poll.add_argument('--telegram',action='store_true',help='Attempt gated Telegram delivery after each cycle')
    quota=sub.add_parser('quota',help='Refresh observed Bitquery quota, without printing secrets')
    history=sub.add_parser('probe-history',help='Sample availability per stream; does not assert coverage')
    history.add_argument('--dates',type=parse_time,nargs='+',required=True)
    history.add_argument('--window-seconds',type=int,default=120)
    en=sub.add_parser('enrich',help='Collect current evidence for a bounded observed token set')
    en.add_argument('--max-tokens',type=int,default=3)
    en.add_argument('--max-wallets',type=int,default=10)
    en.add_argument('--holder-limit',type=int,default=1000)
    en.add_argument('--funding-hours',type=int,default=24)
    cc=sub.add_parser('cctp',help='Reconcile CCTP messages and canonical USDC movements')
    cc.add_argument('--start',type=parse_time,required=True)
    cc.add_argument('--end',type=parse_time,required=True)
    cc.add_argument('--limit',type=int,default=100)
    cc.add_argument('--max-transactions',type=int,default=20)
    rec=sub.add_parser('reconcile',help='Keep separate RPC evidence for stored provider trades')
    rec.add_argument('--trade-id')
    rec.add_argument('--limit',type=int,default=3)
    for command in [quota,en,cc,history]:
        command.add_argument('--max-requests',type=int,default=100)
    for command in [poll,en,cc,rec]:
        command.add_argument('--rpc-url',default=os.getenv('ARC_RPC_URL') or 'https://rpc.mainnet.arc.io')
        command.add_argument('--rpc-max-requests',type=int,default=150)
    for command in [b,poll,quota,en,cc,history]:
        command.add_argument('--rpm',type=float,default=env_number('ARC_REQUESTS_PER_MINUTE',6))
        command.add_argument('--points-budget',type=float,default=env_number('ARC_POINTS_PER_RUN',100))
        command.add_argument('--daily-points',type=float,default=env_number('ARC_POINTS_PER_DAY',200))
        command.add_argument('--points-per-request',type=float,default=env_number('ARC_POINTS_PER_REQUEST',10))
        command.add_argument('--reserve-points',type=float,default=env_number('ARC_POINTS_RESERVE',20))
    status=sub.add_parser('status',help='Coverage intervals, holes, freshness, collector and cached quota')
    status.add_argument('--start',type=parse_time)
    status.add_argument('--lag-seconds',type=int,default=120)
    status.add_argument('--stale-seconds',type=int,default=300)
    cost=sub.add_parser('cost',help='Point-cost scenario from cached account usage; not a supplier bill')
    cost.add_argument('--requests-per-hour',type=float,default=60)
    ev=sub.add_parser('evaluate',help='Purged temporal hold-out, feature comparison and execution scenarios')
    ev.add_argument('--out',default='reports/evaluation')
    ev.add_argument('--as-of',type=parse_time)
    ev.add_argument('--split-at',type=parse_time)
    ev.add_argument('--horizon-hours',type=int,default=1)
    ev.add_argument('--feature-minutes',type=int,default=15)
    ev.add_argument('--min-train',type=int,default=10)
    ev.add_argument('--min-test',type=int,default=10)
    ev.add_argument('--fee-bps',type=int,default=30)
    ev.add_argument('--slippage-bps',type=int,default=100)
    ev.add_argument('--position-usdc',type=float,default=100)
    ev.add_argument('--latency-seconds',type=int,default=120)
    nt=sub.add_parser('notify',help='Dry run by default; Telegram delivery requires eligible bound research')
    nt.add_argument('--send',action='store_true')
    nt.add_argument('--max-messages',type=int,default=10)
    r=sub.add_parser('research',help='Event-time features, censored outcomes, CSV and JSON cohort report')
    r.add_argument('--out',default='reports/research')
    r.add_argument('--as-of',type=parse_time)
    a=sub.add_parser('alerts',help='Evaluate local alert candidates; no messages are sent')
    a.add_argument('--min-runner',type=int,default=65)
    a.add_argument('--min-risk',type=int,default=60)
    a.add_argument('--cooldown-minutes',type=int,default=60)
    a.add_argument('--as-of',type=parse_time)
    q=sub.add_parser('query',help='Print provider GraphQL for validation in Bitquery IDE, no API request')
    q.add_argument('--stream',choices=['trades','launches'],required=True)
    q.add_argument('--start',type=parse_time,required=True)
    q.add_argument('--end',type=parse_time,required=True)
    q.add_argument('--limit',type=int,default=1000)
    for name in ['rpc-check','rpc-launches']:
        rp=sub.add_parser(name,help='Read-only RPC chain verification' if name=='rpc-check' else 'Indexed launch topics by block')
        rp.add_argument('--url',default=os.getenv('ARC_RPC_URL'))
        if name=='rpc-check':
            rp.add_argument('--token')
            rp.add_argument('--wallet')
            rp.add_argument('--max-requests',type=int,default=100)
        else:
            rp.add_argument('--start-block',type=int,required=True)
            rp.add_argument('--end-block',type=int,required=True)
            rp.add_argument('--max-requests',type=int,default=100)
    g=sub.add_parser('liquidity',help='Opt-in current GeckoTerminal pool snapshot; USD estimate assuming USDC peg')
    g.add_argument('--token',required=True)
    g.add_argument('--pool',required=True)
    sub.add_parser('doctor',help='Show configuration presence, not secrets; no network requests')
    return p


def run(args) -> int:
    from .demo import DEMO_ASOF, seed_demo
    from .providers.bitquery import query_for
    if args.command=='doctor':
        emit({'python':sys.version.split()[0],'mode':args.mode,
              'bitquery_token_set':bool(os.getenv('BITQUERY_TOKEN')),
              'rpc_url_set':bool(os.getenv('ARC_RPC_URL')),
              'network_tested':False,'safety':'No private keys are used.'})
        return 0
    if args.command=='query':
        print(query_for(args.stream,args.start,args.end,args.limit))
        return 0
    if args.command in {'backfill','poll'}:
        from .monitoring import run_collection
        return run_collection(args,os.getenv('BITQUERY_TOKEN',''),emit)
    if args.command in {'quota','enrich','cctp','reconcile','probe-history'}:
        from .commands import run_evidence
        return run_evidence(args,emit)
    live_commands={'backfill','poll','rpc-launches','liquidity','enrich','cctp','reconcile'}
    if args.mode=='demo' and args.command in live_commands:
        raise ValueError('live collection must use --mode live and a separate database')
    with Store(args.db,mode=args.mode) as store:
        at=getattr(args,'as_of',None) or (DEMO_ASOF if args.mode=='demo' else datetime.now(timezone.utc).replace(microsecond=0))
        if args.command=='init':
            emit({'initialized':True,'mode':store.mode})
        elif args.command=='status':
            from .operations import collection_status
            emit(collection_status(store,at,start=args.start,lag_seconds=args.lag_seconds,
                                   stale_seconds=args.stale_seconds))
        elif args.command=='cost':
            from .operations import cost_projection
            emit(cost_projection(store,args.requests_per_hour))
        elif args.command=='demo':
            emit(seed_demo(store))
        elif args.command=='import-jsonl':
            path=Path(args.path)
            if path.stat().st_size>100_000_000:
                raise ValueError('JSONL import is limited to 100 MB per atomic batch')
            events=[]
            with path.open(encoding='utf-8-sig') as f:
                for line_number,line in enumerate(f,1):
                    if line.strip():
                        try:
                            event=EVENT.validate_json(line)
                        except ValueError:
                            raise ValueError(f'Invalid normalized event at line {line_number}; nothing imported') from None
                        if (event.source=='demo') != (args.mode=='demo'):
                            raise ValueError('demo-source events and live data must be imported into separate dataset modes')
                        events.append(event)
            emit({'inserted':store.ingest(events),'coverage':'not asserted by a plain JSONL import'})
        elif args.command=='export-jsonl':
            path=Path(args.path)
            path.parent.mkdir(parents=True,exist_ok=True)
            events=[e for k in store.tables for e in store.events(k)]
            events.sort(key=lambda e:(e.ts,e.id))
            with path.open('w',encoding='utf-8') as f:
                for e in events:
                    f.write(e.model_dump_json()+'\n')
            emit({'exported':len(events),'path':str(path)})
        elif args.command=='research':
            from .research import export_research
            emit(export_research(store,args.out,at))
        elif args.command=='evaluate':
            from .evaluation import evaluate_research
            report=evaluate_research(store,args.out,at,split_at=args.split_at,
                horizon_hours=args.horizon_hours,feature_minutes=args.feature_minutes,
                min_train=args.min_train,min_test=args.min_test,fee_bps=args.fee_bps,
                slippage_bps=args.slippage_bps,position_usdc=args.position_usdc,
                latency_seconds=args.latency_seconds)
            import hashlib
            path=Path(args.out)/'evaluation.json'
            store.set_state('evaluation',{'as_of':iso(at),'path':str(path.resolve()),
                'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'notification_eligible':report.get('notification_eligible',False)})
            emit(report)
        elif args.command=='notify':
            from .notifications import notify
            from .operations import WriterLock
            with WriterLock(args.db):
                emit(notify(store,at,send=args.send,max_messages=args.max_messages))
        elif args.command=='alerts':
            from .alerting import evaluate_alerts
            emit(evaluate_alerts(store,at,min_runner=args.min_runner,min_risk=args.min_risk,
                                 cooldown_minutes=args.cooldown_minutes))
        elif args.command in {'rpc-check','rpc-launches'}:
            from .providers.rpc import RpcClient
            from .budget import RequestBudget
            if not args.url:
                raise ValueError('Set ARC_RPC_URL or pass --url for a trusted Arc mainnet RPC endpoint')
            with RpcClient(args.url,budget=RequestBudget(max_requests=args.max_requests,
                    rpm=60,points_budget=args.max_requests,points_per_request=1,reserve_points=0)) as rpc:
                rpc.verify()
                if args.command=='rpc-check':
                    info={'chain_id':5042,'verified':True}
                    if args.token:
                        info['token']=rpc.inspect_token(args.token)
                    if args.wallet:
                        info['usdc']=rpc.usdc_balance(args.wallet)
                    emit(info)
                else:
                    inserted=0
                    for launch in rpc.launch_logs(args.start_block,args.end_block,max_requests=args.max_requests):
                        inserted+=store.ingest([launch])
                    emit({'inserted':inserted,'scope':'Registry launch logs only; no trade coverage asserted'})
        elif args.command=='liquidity':
            from .providers.gecko import GeckoClient
            from .budget import RequestBudget
            with GeckoClient(budget=RequestBudget(max_requests=1,rpm=20)) as g:
                value=g.pool_liquidity(args.token,args.pool)
                store.ingest([value])
                emit(value.model_dump(mode='json'))
        if args.command=='serve' or (args.command=='demo' and args.serve):
            import uvicorn
            from .app import create_app
            if not 1<=args.port<=65535:
                raise ValueError('port must be in 1..65535')
            uvicorn.run(create_app(store,max_tokens=getattr(args,'max_tokens',500)),
                        host='127.0.0.1',port=args.port)
    return 0


def main(argv=None) -> int:
    loaded=[]
    try:
        from .config import load_env
        pre=argparse.ArgumentParser(add_help=False)
        pre.add_argument('--env-file',default='.env')
        pre.add_argument('--no-env-file',action='store_true')
        settings,_=pre.parse_known_args(argv)
        if not settings.no_env_file:
            loaded=load_env(settings.env_file,required=settings.env_file!='.env')
        args=parser().parse_args(argv)
        return run(args)
    except KeyboardInterrupt:
        print('Stopped. Committed windows remain available for resume.',file=sys.stderr)
        return 130
    except (ValueError,RuntimeError,OSError,KeyError) as exc:
        # Error messages constructed by providers never contain API response bodies or tokens.
        print(f'Error: {exc}',file=sys.stderr)
        return 2
    finally:
        # A library caller can invoke main repeatedly with different files. Only
        # this invocation's inserted keys are removed; inherited environment wins.
        for key in loaded:
            os.environ.pop(key,None)


if __name__=='__main__':
    raise SystemExit(main())
