"""Run `python -m arc_monitor --help`. Credentials come from environment only."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import time

from .models import EVENT, iso, parse_time
from .storage import Store


def emit(value):
    print(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),flush=True)


def parser() -> argparse.ArgumentParser:
    p=argparse.ArgumentParser(prog='arc-monitor',description='Read-only Arc token research and local monitoring')
    p.add_argument('--db',default=os.getenv('ARC_DB','data/arc.db'),help='SQLite path or SQLAlchemy database URL')
    p.add_argument('--mode',choices=['live','demo'],default='live',help='Dataset identity; never mix demo/live')
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
    poll.add_argument('--lookback-minutes',type=int,default=10)
    poll.add_argument('--lag-seconds',type=int,default=120)
    poll.add_argument('--max-requests',type=int,default=500,help='Hard budget for the entire invocation, not each cycle')
    poll.add_argument('--cycles',type=int,default=0,help='0 means keep polling until Ctrl+C or budget exhaustion')
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
    from .providers.bitquery import BitqueryClient, query_for
    from .collection import collect
    if args.command=='doctor':
        emit({'python':sys.version.split()[0],'mode':args.mode,
              'bitquery_token_set':bool(os.getenv('BITQUERY_TOKEN')),
              'rpc_url_set':bool(os.getenv('ARC_RPC_URL')),
              'network_tested':False,'safety':'No private keys are used.'})
        return 0
    if args.command=='query':
        print(query_for(args.stream,args.start,args.end,args.limit))
        return 0
    live_commands={'backfill','poll','rpc-launches','liquidity'}
    if args.mode=='demo' and args.command in live_commands:
        raise ValueError('live collection must use --mode live and a separate database')
    with Store(args.db,mode=args.mode) as store:
        at=getattr(args,'as_of',None) or (DEMO_ASOF if args.mode=='demo' else datetime.now(timezone.utc).replace(microsecond=0))
        if args.command=='init':
            emit({'initialized':True,'mode':store.mode})
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
        elif args.command=='backfill':
            with BitqueryClient(os.getenv('BITQUERY_TOKEN',''),max_requests=args.max_requests) as provider:
                for kind in (['launches','trades'] if args.stream=='both' else [args.stream]):
                    result=collect(store,provider,kind,args.start,args.end,limit=args.limit,
                        max_requests=args.max_requests,refresh=args.refresh,window_seconds=args.window_seconds)
                    emit({'stream':kind,**result})
        elif args.command=='poll':
            if min(args.interval,args.lookback_minutes,args.max_requests)<1 or args.lag_seconds<0 or args.cycles<0:
                raise ValueError('invalid polling interval, lookback, lag, cycle count or budget')
            with BitqueryClient(os.getenv('BITQUERY_TOKEN',''),max_requests=args.max_requests) as provider:
                cycles=0
                while True:
                    end=datetime.now(timezone.utc).replace(microsecond=0)-timedelta(seconds=args.lag_seconds)
                    for kind in ['launches','trades']:
                        checkpoint=store.checkpoint(kind)
                        # Resume gaps after a shutdown; replay a short overlap to catch late arrivals.
                        start=min(checkpoint,end) if checkpoint else end
                        start-=timedelta(minutes=args.lookback_minutes)
                        result=collect(store,provider,kind,start,end,max_requests=args.max_requests,refresh=True)
                        emit({'cycle':cycles+1,'stream':kind,**result})
                    cycles+=1
                    if args.cycles and cycles>=args.cycles:
                        break
                    time.sleep(args.interval)
        elif args.command=='research':
            from .research import export_research
            emit(export_research(store,args.out,at))
        elif args.command=='alerts':
            from .alerting import evaluate_alerts
            emit(evaluate_alerts(store,at,min_runner=args.min_runner,min_risk=args.min_risk,
                                 cooldown_minutes=args.cooldown_minutes))
        elif args.command in {'rpc-check','rpc-launches'}:
            from .providers.rpc import RpcClient
            if not args.url:
                raise ValueError('Set ARC_RPC_URL or pass --url for a trusted Arc mainnet RPC endpoint')
            with RpcClient(args.url) as rpc:
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
            with GeckoClient() as g:
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
    args=parser().parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print('Stopped. Committed windows remain available for resume.',file=sys.stderr)
        return 130
    except (ValueError,RuntimeError,OSError,KeyError) as exc:
        # Error messages constructed by providers never contain API response bodies or tokens.
        print(f'Error: {exc}',file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
