import copy
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from arc_monitor.models import USDC, units
from arc_monitor.storage import Store
from test_storage import A, T, W


def raw_trade():
    return {'Block':{'Time':'2026-09-16T00:00:00Z'},'TransactionHeader':{'Hash':'0x'+'1'*64},
        'Trader':{'Address':W},'Side':'Buy','Amounts':{'Base':'100','Quote':'12.5'},
        'AmountsInUsd':{'Quote':'12.5'},'PriceInUsd':'12345',
        'Pair':{'Market':{'Network':'Arc','Protocol':'uniswap_v4'},
            'Pool':{'Id':'0x'+'a'*64,'Address':'0x'+'c'*40},
            'Token':{'Id':'bid:arc:'+A,'Address':A,'Symbol':'TST'},
            'QuoteToken':{'Id':'bid:arc','Address':'','Symbol':'USDC'}}}


def test_provider_exists():
    import importlib.util
    assert importlib.util.find_spec('arc_monitor.providers.bitquery') is not None, 'Bitquery adapter not implemented'


def test_native_usdc_and_executed_price_not_reference_price():
    from arc_monitor.providers.bitquery import normalize_trade
    t = normalize_trade(raw_trade())
    assert t.quote_amount == Decimal('12.5')
    assert t.quantity == 100
    assert t.price == Decimal('.125')
    assert t.pool == '0x'+'a'*64
    assert t.identity_quality == 'row_fingerprint'


def test_reverse_pair_orientation():
    from arc_monitor.providers.bitquery import normalize_trade
    row = raw_trade()
    row['Pair']['Token'],row['Pair']['QuoteToken'] = row['Pair']['QuoteToken'],row['Pair']['Token']
    row['Amounts'] = {'Base':'12.5','Quote':'100'}
    t = normalize_trade(row)
    assert t.token == A and t.side == 'sell' and t.quote_amount == Decimal('12.5')


def test_fake_usdc_symbol_does_not_pass_quote_filter():
    from arc_monitor.providers.bitquery import normalize_trade
    row = raw_trade()
    row['Pair']['QuoteToken'] = {'Id':'bid:arc:'+'0x'+'d'*40,'Address':'0x'+'d'*40,'Symbol':'USDC'}
    assert normalize_trade(row) is None


def test_duplicate_identity_is_stable_but_pool_is_part_of_identity():
    from arc_monitor.providers.bitquery import normalize_trade
    row = raw_trade()
    first = normalize_trade(row)
    row['PriceInUsd'] = '77777'
    assert normalize_trade(row).id == first.id
    row['Pair']['Pool']['Id'] = '0x'+'b'*64
    assert normalize_trade(row).id != first.id


def test_v4_without_pool_id_fails_closed():
    from arc_monitor.providers.bitquery import normalize_trade
    row = raw_trade()
    row['Pair']['Pool']['Id'] = ''
    with pytest.raises(ValueError,match='pool'):
        normalize_trade(row)


def raw_launch(factory=None, topic=None, token_position=1):
    from arc_monitor.providers.registry import REGISTRY
    factory = factory or next(iter(REGISTRY))
    entry = REGISTRY[factory]
    topics = ['0x'+(topic or entry['signature']), '0x'+'0'*24+A[2:], '0x'+'0'*24+W[2:]]
    if token_position==2:
        topics[1],topics[2] = '0x'+'f'*64,topics[1]
    return {'Block':{'Number':'123','Time':'2026-09-16T00:00:00Z'},
        'Transaction':{'Hash':'0x'+'2'*64,'From':W},
        'LogHeader':{'Address':factory,'Index':7,'Removed':False,'Data':'0x'},
        'Log':{'Signature':{'Name':'','SignatureHash':topic or entry['signature']}},
        'Topics':[{'Hash':t} for t in topics], 'Arguments':[]}


def test_argus_undecoded_topics_are_supported():
    from arc_monitor.providers.bitquery import normalize_launch
    l = normalize_launch(raw_launch())
    assert l.token == A and l.creator == W and l.launchpad=='Argus'


def test_unknown_contract_cannot_impersonate_known_factory():
    from arc_monitor.providers.bitquery import normalize_launch
    row=raw_launch()
    row['LogHeader']['Address']='0x'+'d'*40
    assert normalize_launch(row).launchpad=='unknown-signature-match'


def test_pegd_token_is_topic_two():
    from arc_monitor.providers.bitquery import normalize_launch
    assert normalize_launch(raw_launch('0xd0aa679ec263e8f9bc929426eb9eab2e061d2c5f',token_position=2)).token == A


def test_minara_lp_recipient_is_not_creator():
    from arc_monitor.providers.bitquery import normalize_launch
    l = normalize_launch(raw_launch('0x4d3a3f4e1a918845c2038bc064c4d250822b203e',token_position=2))
    assert l.creator is None and l.pool == '0x'+'f'*64


def test_removed_launch_rejected():
    from arc_monitor.providers.bitquery import normalize_launch
    row=raw_launch()
    row['LogHeader']['Removed']=True
    assert normalize_launch(row) is None


def test_decoded_creator_must_match_indexed_argus_creator():
    from arc_monitor.providers.bitquery import normalize_launch
    row=raw_launch()
    row['Arguments']=[{'Name':'creator','Value':{'address':'0x'+'e'*40}}]
    with pytest.raises(ValueError,match='creator'):
        normalize_launch(row)


def test_graphql_errors_and_missing_rows_not_treated_as_empty():
    from arc_monitor.providers.bitquery import BitqueryClient, ProviderError
    for body in [{'errors':[{'message':'not entitled'}]}, {'data':None}, {'data':{'Trading':{}}}]:
        client = httpx.Client(transport=httpx.MockTransport(lambda req,b=body:httpx.Response(200,json=b)))
        with BitqueryClient('not-a-secret',client=client) as bq:
            with pytest.raises(ProviderError):
                bq.fetch('trades',T,T+timedelta(seconds=1),100)


def test_429_retries_then_succeeds():
    from arc_monitor.providers.bitquery import BitqueryClient
    calls=[]
    def handle(req):
        calls.append(1)
        return httpx.Response(429,headers={'Retry-After':'0'}) if len(calls)==1 else httpx.Response(200,json={'data':{'Trading':{'Trades':[]}}})
    with BitqueryClient('test',client=httpx.Client(transport=httpx.MockTransport(handle)),sleep=lambda _:None) as bq:
        assert bq.fetch('trades',T,T+timedelta(seconds=1),100)==[]
        assert len(calls)==2


def test_http_401_not_retried_and_key_not_exposed():
    from arc_monitor.providers.bitquery import BitqueryClient, ProviderError
    calls=[]
    def handle(req):
        calls.append(1)
        return httpx.Response(401,text='do-not-leak-secret')
    with BitqueryClient('do-not-leak-secret',client=httpx.Client(transport=httpx.MockTransport(handle))) as bq:
        with pytest.raises(ProviderError) as exc:
            bq.fetch('trades',T,T+timedelta(seconds=1),100)
        assert 'do-not-leak-secret' not in str(exc.value)
        assert len(calls)==1


def test_adaptive_backfill_splits_saturated_windows():
    from arc_monitor.collection import collect
    class Fake:
        def fetch(self,kind,start,end,limit):
            return [raw_trade()]*limit if (end-start).total_seconds()>1 else []
    with Store(':memory:',mode='demo') as s:
        result=collect(s,Fake(),'trades',T,T+timedelta(seconds=4),limit=2,max_requests=20)
        assert result['requests']==7
        assert s.covered('trades',T,T+timedelta(seconds=4))


def test_saturated_single_second_fails_without_false_coverage():
    from arc_monitor.collection import collect, CoverageError
    class Fake:
        def fetch(self,*args): return [raw_trade()]*2
    with Store(':memory:',mode='demo') as s:
        with pytest.raises(CoverageError,match='saturated'):
            collect(s,Fake(),'trades',T,T+timedelta(seconds=1),limit=2)
        assert s.checkpoint('trades') is None


def test_unknown_data_shape_keeps_window_uncommitted():
    from arc_monitor.collection import collect
    class Fake:
        def fetch(self,*args): return [{'unrecognised':'shape'}]
    with Store(':memory:',mode='demo') as s:
        with pytest.raises((ValueError,KeyError)):
            collect(s,Fake(),'trades',T,T+timedelta(seconds=1),limit=2)
        assert s.checkpoint('trades') is None


def test_decimals_do_not_double_count_native_and_erc20():
    assert units(10**18,18)==units(10**6,6)==1
    assert units('123456789012345678901234567890123456',18)==Decimal('123456789012345678.901234567890123456')


@pytest.mark.parametrize('raw,decimals',[('1.2',6),('-1',6),(True,6),('5',37),('5',-1)])
def test_invalid_raw_units(raw,decimals):
    with pytest.raises(ValueError): units(raw,decimals)


def test_rpc_chain_mismatch_fails_before_collecting():
    from arc_monitor.providers.rpc import RpcClient
    with RpcClient('https://rpc.test',client=httpx.Client(transport=httpx.MockTransport(
        lambda req:httpx.Response(200,json={'jsonrpc':'2.0','id':1,'result':'0x1'})))) as rpc:
        with pytest.raises(ValueError,match='chain'):
            rpc.verify()


def test_rpc_cannot_sign_or_send_transactions():
    from arc_monitor.providers.rpc import RpcClient
    with RpcClient('https://rpc.test') as rpc:
        with pytest.raises(ValueError,match='read-only'):
            rpc.call('eth_sendRawTransaction',['0x00'])


def test_query_half_open_windows_and_raw_topic_filter():
    from arc_monitor.providers.bitquery import query_for
    for kind in ['trades','launches']:
        q=query_for(kind,T,T+timedelta(seconds=1),123)
        assert q.count('2026-09-16T00:00:00Z')==2
        assert '2026-09-16T00:00:01Z' not in q
        assert q.count('{') == q.count('}') and '$COMMON' not in q
        assert 'count: 123' in q
    assert 'Topics' in query_for('launches',T,T+timedelta(seconds=1),123)
    assert 'dataset' not in query_for('trades',T,T+timedelta(seconds=1),123)


def test_gecko_does_not_backdate_usd_valuation_or_accept_another_token():
    from arc_monitor.providers.gecko import GeckoClient
    pool='0x'+'d'*64
    body={'data':{'attributes':{'address':pool,'reserve_in_usd':'12000.50'},
                 'relationships':{'base_token':{'data':{'id':'arc_'+A}},
                                  'quote_token':{'data':{'id':'arc_'+USDC}}}}}
    with GeckoClient(client=httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=body)))) as g:
        state=g.pool_liquidity(A,pool,observed_at=T)
        assert state.amount_usdc==Decimal('12000.50')
        assert state.valuation=='usd_assuming_usdc_peg' and state.ts==T
        with pytest.raises(ValueError,match='token'):
            g.pool_liquidity(W,pool,observed_at=T)


def test_rpc_response_id_must_match_request():
    from arc_monitor.providers.rpc import RpcClient, ProviderError
    with RpcClient('https://rpc.test',client=httpx.Client(transport=httpx.MockTransport(
        lambda req:httpx.Response(200,json={'jsonrpc':'2.0','id':999,'result':'0x13b2'})))) as rpc:
        with pytest.raises(ProviderError,match='identity'):
            rpc.verify()


def test_backfill_rejects_future_coverage():
    from arc_monitor.collection import collect
    from datetime import datetime, timezone
    now=datetime.now(timezone.utc).replace(microsecond=0)
    class NeverCalled:
        def fetch(self,*_):
            raise AssertionError('A future range must not be queried')
    with Store(':memory:') as s:
        with pytest.raises(ValueError,match='future'):
            collect(s,NeverCalled(),'trades',now,now+timedelta(hours=1))


def test_successful_trade_backfill_discovers_token_and_resumes_idempotently():
    from arc_monitor.collection import collect
    class Fake:
        def fetch(self,*_): return [raw_trade()]
    with Store(':memory:',mode='demo') as s:
        first=collect(s,Fake(),'trades',T,T+timedelta(seconds=1),limit=10)
        assert first['inserted']==2 and s.count('trade')==1 and s.count('launch')==1
        assert s.events('launch')[0].origin=='first_trade'
        second=collect(s,Fake(),'trades',T,T+timedelta(seconds=1),limit=10)
        assert second['requests']==0 and second['skipped_windows']==1
        assert collect(s,Fake(),'trades',T,T+timedelta(seconds=1),limit=10,refresh=True)['inserted']==0


def test_successful_launch_backfill_and_budget_limit():
    from arc_monitor.collection import collect, CoverageError
    class Launches:
        def fetch(self,*_): return [raw_launch()]
    class Empty:
        def fetch(self,*_): return []
    with Store(':memory:',mode='demo') as s:
        assert collect(s,Launches(),'launches',T,T+timedelta(seconds=1),limit=10)['inserted']==1
        assert s.events('launch')[0].creator==W
        with pytest.raises(CoverageError,match='budget'):
            collect(s,Empty(),'trades',T,T+timedelta(seconds=3),max_requests=1,window_seconds=1)
        assert s.covered('trades',T,T+timedelta(seconds=1))
        assert not s.covered('trades',T,T+timedelta(seconds=3))


def mock_rpc(responder):
    import json
    def handle(request):
        body=json.loads(request.content)
        return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],
                                       'result':responder(body['method'],body['params'])})
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_rpc_uints_are_normalized_and_not_a_safety_audit():
    from arc_monitor.providers.rpc import RpcClient
    def respond(method,params):
        if method=='eth_chainId': return hex(5042)
        if method=='eth_getCode': return '0x6000'
        return '0x'+format(6 if params[0]['data']=='0x313ce567' else 10**12,'064x')
    with RpcClient('https://rpc.test',client=mock_rpc(respond)) as rpc:
        info=rpc.inspect_token(A)
        assert info['erc20_verified'] is True and Decimal(info['total_supply'])==1000000
        assert 'not a contract safety audit' in info['warning']


def test_rpc_invalid_erc20_return_is_not_verified():
    from arc_monitor.providers.rpc import RpcClient
    def respond(method,params):
        return hex(5042) if method=='eth_chainId' else '0x6000' if method=='eth_getCode' else '0x'
    with RpcClient('https://rpc.test',client=mock_rpc(respond)) as rpc:
        assert rpc.inspect_token(A)['erc20_verified'] is False


def test_rpc_native_and_erc20_are_pinned_and_not_added():
    from arc_monitor.providers.rpc import RpcClient
    seen=[]
    def respond(method,params):
        seen.append((method,params))
        return {'eth_chainId':hex(5042),'eth_blockNumber':'0x10','eth_getBalance':hex(10**18),
                'eth_call':hex(10**6)}[method]
    with RpcClient('https://rpc.test',client=mock_rpc(respond)) as rpc:
        state=rpc.usdc_balance(W)
        assert Decimal(state['balance_usdc'])==1
        assert state['representations_match_within_1_micro_usdc'] is True
        assert all(p[-1]=='0x10' for m,p in seen if m in ['eth_getBalance','eth_call'])


def rpc_launch_responder(mismatch=False):
    raw=raw_launch()
    block_hash='0x'+'a'*64
    log={'removed':False,'blockNumber':'0x1','blockHash':block_hash,
         'transactionHash':raw['Transaction']['Hash'],'address':raw['LogHeader']['Address'],
         'logIndex':'0x2','topics':[t['Hash'] for t in raw['Topics']],'data':'0x'}
    def respond(method,params):
        if method=='eth_chainId': return hex(5042)
        if method=='eth_getLogs': return [log]
        return {'hash':('0x'+'f'*64) if mismatch else block_hash,'timestamp':hex(int(T.timestamp()))}
    return respond


def test_rpc_launch_log_retains_separate_source_index():
    from arc_monitor.providers.rpc import RpcClient
    with RpcClient('https://rpc.test',client=mock_rpc(rpc_launch_responder())) as rpc:
        rows=list(rpc.launch_logs(1,1))
        assert len(rows)==1 and rows[0].token==A and rows[0].creator==W
        assert rows[0].id.startswith('rpc-launch:') and rows[0].id.endswith(':2')


def test_rpc_block_hash_mismatch_is_not_accepted():
    from arc_monitor.providers.rpc import RpcClient, ProviderError
    with RpcClient('https://rpc.test',client=mock_rpc(rpc_launch_responder(True))) as rpc:
        with pytest.raises(ProviderError,match='hash mismatch'):
            list(rpc.launch_logs(1,1))
