"""Opt-in current-pool cross-check. A USD reserve estimate is not on-chain USDC cash.

Reference: https://apiguide.geckoterminal.com/ ; public API: 30 calls/minute.
No automatic market-wide scans or historical backdating.
"""
from datetime import datetime, timezone
import re

import httpx

from ..models import Liquidity, address, iso
from .bitquery import ProviderError


class GeckoClient:
    def __init__(self, *, client=None):
        self.client=client or httpx.Client(timeout=30)

    def pool_liquidity(self, token: str, pool: str, *, observed_at=None) -> Liquidity:
        token=address(token)
        if not re.fullmatch(r'0x(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})',pool):
            raise ValueError('pool must be an explicit address or bytes32 pool ID')
        pool=pool.lower()
        try:
            response=self.client.get('https://api.geckoterminal.com/api/v2/networks/arc/pools/'+pool)
            if response.status_code!=200:
                raise ProviderError(f'GeckoTerminal HTTP {response.status_code}; no snapshot recorded')
            data=response.json()['data']
            attr=data['attributes']
            tokens={data['relationships'][side]['data']['id'].lower() for side in ['base_token','quote_token']}
            if 'arc_'+token not in tokens:
                raise ValueError('Gecko pool does not contain the requested token')
            if attr['address'].lower()!=pool:
                raise ValueError('Gecko pool identity differs; do not match by shared PoolManager')
            at=observed_at or datetime.now(timezone.utc)
            return Liquidity(id=f'gecko:{pool}:{iso(at)}',token=token,ts=at,source='geckoterminal',
                pool=pool,amount_usdc=attr['reserve_in_usd'],valuation='usd_assuming_usdc_peg')
        except (KeyError,TypeError,httpx.TransportError):
            raise ProviderError('GeckoTerminal transport or schema failure; no snapshot recorded') from None

    def __enter__(self):
        return self

    def __exit__(self,*_):
        self.client.close()
