"""Bitquery documented Arc query shapes, without archive/combined assumptions.

Live auth/schema execution requires a user API token. Unit tests use synthetic responses.
Source: https://docs.bitquery.io/docs/blockchain/arc-mainnet/
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta
from decimal import Decimal

import httpx

from ..models import CHAIN_ID, USDC, Launch, Trade, address, iso, parse_time
from .registry import REGISTRY

ENDPOINT = 'https://streaming.bitquery.io/graphql'
PROTOCOLS = ['uniswap_v2', 'uniswap_v3', 'uniswap_v4']


class ProviderError(RuntimeError):
    pass


def decimal_string(value) -> str:
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError('trade amounts must be finite and positive')
    out = format(number, 'f')
    return out.rstrip('0').rstrip('.') if '.' in out else out


def token_address(token: dict) -> str:
    ident = token.get('Id', '')
    raw = token.get('Address', '')
    if ident == 'bid:arc':
        if raw not in ('', '0x', None):
            raise ValueError('native USDC id has a conflicting address')
        return USDC
    if ident and not ident.startswith('bid:arc:'):
        raise ValueError('token id is not on Arc')
    result = address(raw or ident.removeprefix('bid:arc:'))
    if ident and address(ident.removeprefix('bid:arc:')) != result:
        raise ValueError('token id/address conflict')
    return result


def normalize_trade(row: dict) -> Trade | None:
    pair = row['Pair']
    if pair['Market']['Network'] != 'Arc':
        raise ValueError('wrong network in trade response')
    protocol = pair['Market']['Protocol']
    if protocol not in PROTOCOLS:
        return None
    base, quote = token_address(pair['Token']), token_address(pair['QuoteToken'])
    if (base == USDC) == (quote == USDC):
        return None  # scope: exactly one verified USDC side
    side = row['Side'].lower()
    if side not in ('buy', 'sell'):
        raise ValueError('unknown trade Side')
    if quote == USDC:
        token, quantity, amount = base, row['Amounts']['Base'], row['Amounts']['Quote']
        symbol = pair['Token'].get('Symbol') or ''
    else:
        token, quantity, amount = quote, row['Amounts']['Quote'], row['Amounts']['Base']
        symbol = pair['QuoteToken'].get('Symbol') or ''
        side = 'sell' if side == 'buy' else 'buy'
    pool = pair['Pool'].get('Id') if protocol == 'uniswap_v4' else pair['Pool'].get('Address')
    if not pool:
        raise ValueError('missing pool identity (v4 requires Pool.Id)')
    if protocol != 'uniswap_v4':
        pool = address(pool)
    else:
        pool = pool.lower()
    quantity, amount = decimal_string(quantity), decimal_string(amount)
    wallet = address(row['Trader']['Address'])
    tx = row['TransactionHeader']['Hash'].lower()
    key = [CHAIN_ID, tx, pool, token, wallet, side, quantity, amount]
    ident = 'bq-trade:' + hashlib.sha256(json.dumps(key,separators=(',',':')).encode()).hexdigest()
    return Trade(id=ident, token=token, ts=parse_time(row['Block']['Time']), source='bitquery',
                 tx=tx,wallet=wallet,side=side,quantity=quantity,quote_amount=amount,pool=pool,
                 symbol=symbol,identity_quality='row_fingerprint')


def topic_hex(value: str) -> str:
    value = value.removeprefix('0x').lower()
    if not re.fullmatch('[0-9a-f]{64}',value):
        raise ValueError('expected a bytes32 topic')
    return value


def topic_address(value: str) -> str:
    raw = topic_hex(value)
    if raw[:24] != '0'*24:
        raise ValueError('indexed address has nonzero padding')
    return address('0x'+raw[-40:])


def normalize_launch(row: dict, *, source: str = 'bitquery') -> Launch | None:
    header = row['LogHeader']
    if header.get('Removed',False) or row.get('TransactionStatus',{}).get('Success') is False:
        return None
    emitter = address(header['Address'])
    topics = [topic_hex(t['Hash']) for t in row['Topics']]
    if not topics:
        return None
    signature = topics[0]
    known = REGISTRY.get(emitter)
    if known and known['signature'] != signature:
        return None
    template = known or next((e for e in REGISTRY.values() if e['signature']==signature),None)
    if template is None:
        return None
    args = {}
    for arg in row.get('Arguments') or []:
        values = [v for v in (arg.get('Value') or {}).values() if v is not None]
        if values:
            args[arg['Name']] = values[0]
    token = topic_address(topics[template['token_topic']])
    if args.get('token') and address(args['token']) != token:
        raise ValueError('decoded token does not match indexed token')
    creator = None
    # Do not infer the creator from the transaction sender or Minara's LP recipient.
    if 'creator_topic' in template:
        creator = topic_address(topics[template['creator_topic']])
    if args.get('creator'):
        decoded_creator = address(args['creator'])
        if creator and decoded_creator != creator:
            raise ValueError('decoded creator does not match indexed creator')
        creator = decoded_creator
    pool = args.get('poolId')
    if 'pool_topic' in template:
        pool = '0x'+topics[template['pool_topic']]
    if pool:
        pool = '0x'+topic_hex(pool)
    tx = row['Transaction']['Hash'].lower()
    # Provider-specific index: Bitquery Index must NOT be equated with RPC logIndex.
    index = int(header['Index'])
    return Launch(id=f'{source}-launch:{CHAIN_ID}:{tx}:{index}',token=token,
        ts=parse_time(row['Block']['Time']),source=source,tx=tx,block=int(row['Block']['Number']),
        creator=creator,launchpad=known['name'] if known else 'unknown-signature-match',
        name=str(args.get('name') or ''),symbol=str(args.get('symbol') or ''),pool=pool)


def _gql(value) -> str:
    """Serialize only internally constructed GraphQL input objects, not arbitrary code."""
    if isinstance(value, dict):
        return '{' + ','.join(key + ':' + _gql(v) for key, v in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(_gql(v) for v in value) + ']'
    return json.dumps(value)


def query_for(kind: str, start: datetime, end: datetime, limit: int) -> str:
    if kind not in ('trades', 'launches') or not 1 <= limit <= 10000 or start >= end:
        raise ValueError('invalid query kind, range or limit')
    if start.microsecond or end.microsecond:
        raise ValueError('provider windows must be aligned to whole UTC seconds')
    where = {'Block': {'Time': {'since': iso(start), 'till': iso(end - timedelta(seconds=1))}}}
    common = 'limit: {count: ' + str(limit) + '}, orderBy: {ascending: Block_Time}'
    if kind == 'trades':
        ids = ['bid:arc', 'bid:arc:' + USDC]
        where.update({
            'Pair': {'Market': {'Network': {'is': 'Arc'}, 'Protocol': {'in': PROTOCOLS}}},
            'any': [{'Pair': {side: {'Id': {'in': ids}}}} for side in ['Token', 'QuoteToken']]
        })
        template = """query { Trading { Trades($COMMON, where: $WHERE) {
            Block {Time} TransactionHeader {Hash} Trader {Address} Side
            Amounts {Base Quote} Pair {Market {Network Protocol} Pool {Id Address}
            Token {Id Address Symbol} QuoteToken {Id Address Symbol}}
        } } }"""
    else:
        # Raw topics retain early launches whose decoded event name is absent.
        where.update({
            'TransactionStatus': {'Success': True},
            'Call': {'Success': True, 'Reverted': False},
            'LogHeader': {'Removed': False},
            'any': [{'LogHeader': {'Address': {'is': a}},
                     'Topics': {'includes': [{'Hash': {'is': '0x' + e['signature']}}]}}
                    for a, e in REGISTRY.items()]
        })
        template = """query { EVM(network: arc) { Events($COMMON, where: $WHERE) {
            Block {Number Time} Transaction {Hash From} LogHeader {Address Index Data}
            Topics {Hash} Arguments {Name Value {
                ... on EVM_ABI_Address_Value_Arg {address}
                ... on EVM_ABI_String_Value_Arg {string}
                ... on EVM_ABI_Bytes_Value_Arg {hex}
            }}
        } } }"""
    return template.replace('$COMMON', common).replace('$WHERE', _gql(where))


class BitqueryClient:
    def __init__(self, token: str, *, client: httpx.Client | None = None,
                 sleep=time.sleep, max_requests: int = 500):
        if not token or not token.strip():
            raise ValueError('Set BITQUERY_TOKEN locally; no API token was provided')
        self._token = token.strip()
        self.client = client or httpx.Client(timeout=45)
        self.sleep = sleep
        self.requests = 0
        self.max_requests = max_requests

    def fetch(self, kind: str, start: datetime, end: datetime, limit: int) -> list[dict]:
        query = query_for(kind,start,end,limit)
        body = None
        for attempt in range(3):
            if self.requests >= self.max_requests:
                raise ProviderError('API request budget exhausted; committed windows can be resumed')
            self.requests += 1
            try:
                response = self.client.post(ENDPOINT,headers={'Authorization':'Bearer '+self._token},
                                            json={'query':query})
            except httpx.TransportError:
                if attempt==2:
                    raise ProviderError('Bitquery transport failed after 3 attempts') from None
                self.sleep(2**attempt)
                continue
            if response.status_code==429 or response.status_code>=500:
                if attempt==2:
                    raise ProviderError(f'Bitquery temporary HTTP {response.status_code}; retry budget exhausted')
                try:
                    delay = float(response.headers.get('Retry-After',2**attempt))
                except ValueError:
                    delay = 2**attempt
                self.sleep(max(0,min(60,delay)))
                continue
            if response.status_code!=200:
                raise ProviderError(f'Bitquery HTTP {response.status_code}; verify token and plan permissions')
            try:
                body=response.json()
            except ValueError:
                raise ProviderError('Bitquery returned invalid JSON') from None
            if not isinstance(body,dict) or body.get('errors') or not isinstance(body.get('data'),dict):
                # Do not echo provider text, which may contain credentials or request fragments.
                raise ProviderError('Bitquery GraphQL error or missing data; check query in provider IDE')
            break
        try:
            rows = body['data']['Trading' if kind=='trades' else 'EVM']['Trades' if kind=='trades' else 'Events']
        except (KeyError,TypeError):
            raise ProviderError('Bitquery response shape changed; refusing to mark empty coverage') from None
        if not isinstance(rows,list):
            raise ProviderError('Bitquery rows are not a list')
        return rows

    def __enter__(self):
        return self

    def __exit__(self,*_):
        self.client.close()
