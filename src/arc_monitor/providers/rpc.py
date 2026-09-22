"""Read-only JSON-RPC, explicit chain validation, no wallet keys or signing methods."""
from __future__ import annotations

from datetime import datetime, timezone
import re

import httpx

from ..models import CHAIN_ID, USDC, address, iso, units
from .bitquery import ProviderError, normalize_launch
from .registry import REGISTRY

ALLOWED = {'eth_chainId','eth_blockNumber','eth_getBlockByNumber','eth_getCode','eth_getLogs',
           'eth_call','eth_getBalance','eth_getTransactionReceipt','eth_getStorageAt','web3_sha3'}


class RpcClient:
    def __init__(self,url: str,*,client: httpx.Client | None = None,budget=None):
        self.url=url
        self.client=client or httpx.Client(timeout=30)
        self._id=0
        self._verified=False
        self.budget=budget

    def call(self,method: str,params: list | None = None):
        if method not in ALLOWED:
            raise ValueError('RPC client is read-only; method is not permitted')
        if method!='eth_chainId' and not self._verified:
            self.verify()
        if self.budget:
            self.budget.consume()
        self._id+=1
        try:
            response=self.client.post(self.url,json={'jsonrpc':'2.0','id':self._id,'method':method,'params':params or []})
            response.raise_for_status()
            data=response.json()
        except (httpx.HTTPError,ValueError):
            raise ProviderError('RPC transport, HTTP or JSON error; check configured endpoint') from None
        if not isinstance(data,dict) or data.get('error') or 'result' not in data:
            raise ProviderError('RPC returned an error or no result')
        if data.get('jsonrpc') != '2.0' or type(data.get('id')) is not int or data['id'] != self._id:
            raise ProviderError('RPC response identity does not match request')
        return data['result']

    def verify(self) -> dict:
        chain=int(self.call('eth_chainId'),16)
        if chain!=CHAIN_ID:
            raise ValueError(f'RPC chain mismatch: expected {CHAIN_ID}, received {chain}')
        self._verified=True
        return {'chain_id':chain,'verified':True}

    def inspect_token(self,token: str) -> dict:
        token=address(token)
        code=self.call('eth_getCode',[token,'latest'])
        if not isinstance(code,str) or not code.startswith('0x') or code in ('0x','0x0'):
            return {'token':token,'has_code':False,'erc20_verified':False}
        def uint_call(selector):
            raw=self.call('eth_call',[{'to':token,'data':selector},'latest'])
            if not isinstance(raw,str) or not re.fullmatch('0x[0-9a-fA-F]{64}',raw):
                raise ProviderError('ERC20 method did not return a uint256')
            return int(raw,16)
        try:
            decimals=uint_call('0x313ce567')
            supply=units(uint_call('0x18160ddd'),decimals)
        except (ValueError,ProviderError):
            return {'token':token,'has_code':True,'erc20_verified':False,
                    'warning':'decimals/totalSupply calls failed; code presence is not a safety audit'}
        return {'token':token,'has_code':True,'erc20_verified':True,'decimals':decimals,
                'total_supply':str(supply),'warning':'ERC20 interface calls are not a contract safety audit'}

    def usdc_balance(self,wallet: str) -> dict:
        wallet=address(wallet)
        # Pin both representations to one block to avoid a moving-balance comparison.
        block=self.call('eth_blockNumber')
        native=units(int(self.call('eth_getBalance',[wallet,block]),16),18)
        encoded='0x70a08231'+'0'*24+wallet[2:]
        erc20=units(int(self.call('eth_call',[{'to':USDC,'data':encoded},block]),16),6)
        return {'wallet':wallet,'block':block,'native_usdc':str(native),'erc20_usdc':str(erc20),
                'balance_usdc':str(native),'representations_match_within_1_micro_usdc':abs(native-erc20)<=units(1,6),
                'note':'Same asset in two representations. They are not added.'}

    def launch_logs(self,start_block: int,end_block: int,*,max_requests: int = 100):
        if start_block<0 or end_block<start_block or max_requests<1:
            raise ValueError('invalid block range or request budget')
        self.verify()
        block_cache={}
        used=0
        left=start_block
        while left<=end_block:
            right=min(left+499,end_block)
            if used>=max_requests:
                raise ProviderError('RPC log request budget exhausted')
            used+=1
            logs=self.call('eth_getLogs',[{'fromBlock':hex(left),'toBlock':hex(right),
                'address':list(REGISTRY),'topics':[['0x'+e['signature'] for e in REGISTRY.values()]]}])
            if not isinstance(logs,list):
                raise ProviderError('RPC logs are not a list')
            for log in logs:
                if log.get('removed'):
                    continue
                number=int(log['blockNumber'],16)
                if not left<=number<=right:
                    raise ProviderError('RPC returned a log outside requested block range')
                if number not in block_cache:
                    block_cache[number]=self.call('eth_getBlockByNumber',[hex(number),False])
                block=block_cache[number]
                if block.get('hash','').lower()!=log['blockHash'].lower():
                    raise ProviderError('RPC block/log hash mismatch; refusing inconsistent history')
                row={'Block':{'Number':number,'Time':iso(datetime.fromtimestamp(int(block['timestamp'],16),timezone.utc))},
                     'Transaction':{'Hash':log['transactionHash']},
                     'LogHeader':{'Address':log['address'],'Index':int(log['logIndex'],16),'Data':log.get('data','0x')},
                     'Topics':[{'Hash':t} for t in log['topics']]}
                parsed=normalize_launch(row,source='rpc')
                if parsed:
                    yield parsed
            left=right+1

    def __enter__(self):
        return self

    def __exit__(self,*_):
        self.client.close()
