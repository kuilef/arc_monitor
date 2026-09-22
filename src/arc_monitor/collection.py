"""Resumable bounded-range ingestion. Full windows are committed with their checkpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import Launch, iso, utc
from .providers.bitquery import normalize_launch, normalize_trade
from .storage import Store


class CoverageError(RuntimeError):
    pass


def collect(store: Store, provider, kind: str, start: datetime, end: datetime,
            *, limit: int = 1000, max_requests: int = 500, refresh: bool = False,
            window_seconds: int = 300, progress=None) -> dict:
    start,end=utc(start),utc(end)
    if kind not in ('trades','launches') or start>=end or limit<1 or max_requests<1 or window_seconds<1:
        raise ValueError('invalid collection parameters')
    if end > datetime.now(timezone.utc):
        raise ValueError('cannot collect or assert coverage for a future interval')
    if start.microsecond or end.microsecond:
        raise ValueError('collection windows must align to whole seconds')
    result={'requests':0,'inserted':0,'windows':0,'skipped_windows':0,'excluded_rows':0}
    normalizer=normalize_trade if kind=='trades' else normalize_launch
    known={}
    for l in store.events('launch'):
        known[l.token]=min(known.get(l.token,l.ts),l.ts)
    chunk_start=start
    while chunk_start<end:
        chunk_end=min(end,chunk_start+timedelta(seconds=window_seconds))
        stack=[(chunk_start,chunk_end)]
        while stack:
            left,right=stack.pop()
            if not refresh and store.covered(kind,left,right):
                result['skipped_windows']+=1
                continue
            if result['requests']>=max_requests:
                raise CoverageError('request budget exhausted; rerun the same range to resume committed windows')
            rows=provider.fetch(kind,left,right,limit)
            result['requests']+=1
            store.save_raw('bitquery',{'kind':kind,'start':iso(left),'end':iso(right),'rows':rows})
            if len(rows)>=limit:
                seconds=int((right-left).total_seconds())
                if seconds<=1:
                    raise CoverageError(f'saturated one-second interval {iso(left)}; use a larger limit or narrower source partitions')
                mid=left+timedelta(seconds=seconds//2)
                stack.extend([(mid,right),(left,mid)])
                continue
            normalized=[]
            discovered={}
            for row in rows:
                item=normalizer(row)
                if item is None:
                    result['excluded_rows']+=1
                    continue
                normalized.append(item)
                if kind=='trades' and (item.token not in known or item.ts<known[item.token]):
                    old=discovered.get(item.token)
                    if old is None or item.ts<old.ts:
                        discovered[item.token]=item
            for t in discovered.values():
                normalized.append(Launch(id='first-trade:'+t.id,token=t.token,ts=t.ts,tx=t.tx,
                    source=t.source,symbol=t.symbol,pool=t.pool,origin='first_trade',launchpad='unattributed'))
            result['inserted']+=store.record_window(kind,left,right,normalized,'bitquery')
            for t in discovered.values():
                known[t.token]=t.ts
            result['windows']+=1
            if progress:
                progress(kind,left,right,len(rows))
        chunk_start=chunk_end
    result['api_requests']=getattr(provider,'requests',result['requests'])
    return result
