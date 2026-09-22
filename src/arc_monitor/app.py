"""Local read-only dashboard. No CDN, wallet interaction, auth token exposure or writes."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .demo import DEMO_ASOF
from .metrics import ecosystem, origins, snapshot, SCORE_VERSION
from .models import address, iso
from .storage import Store

WEB=Path(__file__).with_name('web')


def create_app(store: Store, *, max_tokens: int=500) -> FastAPI:
    if max_tokens<1:
        raise ValueError('max_tokens must be positive')
    app=FastAPI(title='Arc Monitor',version='0.1.0',docs_url=None,redoc_url=None)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['127.0.0.1','localhost','[::1]','testserver'])
    lock=threading.Lock()
    cached={'expires':0.0,'items':[],'observed':0}

    def cutoff():
        return DEMO_ASOF if store.mode=='demo' else datetime.now(timezone.utc).replace(microsecond=0)

    def board():
        with lock:
            if time.monotonic()>=cached['expires']:
                at=cutoff()
                launches=origins(store,at)
                selected=sorted(launches.values(),key=lambda l:l.ts,reverse=True)[:max_tokens]
                cached.update(items=[snapshot(store,l.token,at,include_creator=False) for l in selected],
                              observed=len(launches),expires=time.monotonic()+10)
            return cached['items'],cached['observed']

    @app.middleware('http')
    async def security_headers(request,call_next):
        response=await call_next(request)
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Cache-Control']='no-store'
        return response

    @app.get('/api/status')
    def status():
        from .operations import collection_status
        cursors={k:store.checkpoint(k) for k in ['launches','trades','flows']}
        return {'mode':store.mode,'synthetic':store.mode=='demo','chain_id':5042,
                'as_of':iso(cutoff()),'score_version':SCORE_VERSION,'dashboard_limit':max_tokens,
                'checkpoints':{k:iso(v) if v else None for k,v in cursors.items()},
                'coverage_note':'A checkpoint is not proof of gap-free or all-chain coverage.',
                'rows':{k:store.count(k) for k in ['launch','trade','liquidity','holders','funding','risk']},
                'collection':collection_status(store,cutoff())}

    @app.get('/api/tokens')
    def tokens(q: str=Query('',max_length=150), stage: str=Query('',max_length=30),
               launchpad: str=Query('',max_length=100), min_runner: int=Query(0,ge=0,le=100),
               sort: Literal['runner_score','age_minutes','adjusted_volume_usdc','unique_buyers']='runner_score',
               limit: int=Query(100,ge=1,le=1000), offset: int=Query(0,ge=0)):
        items,observed=board()
        rows=[r for r in items if (not q or q.lower() in (r['token']+' '+r['symbol']).lower())
              and (not stage or r['stage']==stage) and (not launchpad or r['launchpad']==launchpad)
              and r['runner_score']>=min_runner]
        rows.sort(key=lambda r:float(r[sort] or 0),reverse=sort!='age_minutes')
        return {'items':rows[offset:offset+limit],'total':len(rows),'observed_tokens':observed,
                'scanned_tokens':len(items),'truncated_universe':observed>max_tokens}

    @app.get('/api/tokens/{token}')
    def token_detail(token: str):
        try:
            token=address(token)
        except ValueError:
            raise HTTPException(422,'Invalid contract address') from None
        at=cutoff()
        try:
            metrics=snapshot(store,token,at)
        except KeyError:
            raise HTTPException(404,'Token not observed in this dataset') from None
        trades=store.events('trade',token=token,before=at)
        # Display-only downsampling; features and research always use the full event set.
        if len(trades)>400:
            indexes={round(i*(len(trades)-1)/399) for i in range(400)}
            shown=[trades[i] for i in sorted(indexes)]
        else:
            shown=trades
        series=[{'time':iso(t.ts),'price_usdc':str(t.price),'quote_amount':str(t.quote_amount),
                 'side':t.side,'wallet':t.wallet} for t in shown]
        return {'metrics':metrics,'series':series,'series_total':len(trades),
                'chart_note':'Selected executed trade prices, not OHLC or executable quotes.'}

    @app.get('/api/ecosystem')
    def eco():
        result=ecosystem(store,cutoff())
        result['scope']=result['trade_scope']
        return result

    @app.get('/api/alerts')
    def alerts():
        return {'items':store.alert_list()[-1000:],'note':'Local research candidates only.'}

    @app.get('/')
    def home():
        return FileResponse(WEB/'index.html')

    @app.get('/assets/{filename}')
    def asset(filename: str):
        if filename not in {'app.js','style.css'}:
            raise HTTPException(404)
        return FileResponse(WEB/filename)

    return app


def app_from_env() -> FastAPI:
    """ASGI factory for container/process managers; credentials are not exposed by status."""
    import os
    from .config import load_env
    load_env(os.getenv('ARC_ENV_FILE','.env'))
    from contextlib import asynccontextmanager

    store=Store(os.getenv('ARC_DB','data/arc.db'),mode=os.getenv('ARC_MODE','live'))
    app=create_app(store,max_tokens=int(os.getenv('ARC_MAX_TOKENS','500')))

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield
        finally:
            store.close()

    app.router.lifespan_context=lifespan
    return app
