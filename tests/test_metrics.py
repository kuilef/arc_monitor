from datetime import timedelta
from decimal import Decimal

import pytest

from test_storage import A, T, W, trade
from arc_monitor.models import Funding, Holders, Launch, Liquidity, Risk
from arc_monitor.storage import Store


def launch(**kw):
    values = dict(id='launch', token=A, ts=T, source='test', tx='0x'+'a'*64, creator=W, symbol='TST', pool='pool1')
    values.update(kw)
    return Launch(**values)


def wallet(i):
    return '0x'+format(i, '040x')


def buyer(i, minute, **kw):
    return trade(id=f'b{i}-{minute}', ts=T+timedelta(minutes=minute), wallet=wallet(i), **kw)


def test_metric_engine_exists():
    import importlib.util
    assert importlib.util.find_spec('arc_monitor.metrics') is not None, 'metric engine not implemented'


def snapshot(s, minutes=60):
    from arc_monitor.metrics import snapshot
    return snapshot(s, A, T+timedelta(minutes=minutes))


def test_unknown_evidence_is_not_safe():
    with Store(':memory:', mode='demo') as s:
        s.ingest([launch(), buyer(1, 3)])
        m = snapshot(s)
        assert m['risk_score'] is None
        assert m['liquidity_usdc'] is None
        assert m['market_cap_usdc'] is None
        assert 'contract_risk' in m['missing_evidence']


def test_only_first_buy_counts_in_new_buyer_velocity():
    with Store(':memory:', mode='demo') as s:
        s.ingest([launch(), buyer(1, 5), buyer(1, 28), buyer(2, 27), buyer(3, 21)])
        m = snapshot(s, 30)
        assert m['new_buyers_5m'] == 1
        assert m['new_buyers_previous_5m'] == 1
        assert m['unique_buyers'] == 3


def test_creator_does_not_count_as_external_buyer():
    with Store(':memory:', mode='demo') as s:
        s.ingest([launch(), trade(ts=T+timedelta(minutes=5))])
        assert snapshot(s)['unique_buyers'] == 0


def test_future_does_not_change_past_features():
    with Store(':memory:', mode='demo') as s:
        s.ingest([launch(), buyer(1, 3)])
        before = snapshot(s, 30)
        s.ingest([buyer(2, 40), Liquidity(id='future',token=A,ts=T+timedelta(minutes=40),
                                        pool='pool1',amount_usdc='900000',source='test')])
        assert snapshot(s, 30) == before


def test_repeated_round_trip_is_discounted():
    with Store(':memory:', mode='demo') as s:
        s.ingest([launch(), buyer(1, 20, quote_amount='1000'),
                  buyer(1, 20.2, side='sell', quote_amount='1000')])
        m = snapshot(s, 30)
        assert Decimal(m['raw_volume_usdc']) == 2000
        assert Decimal(m['adjusted_volume_usdc']) == 0
        assert m['wash_score'] >= 70


def test_balanced_independent_market_is_not_automatically_wash():
    with Store(':memory:', mode='demo') as s:
        s.ingest([launch(), buyer(1, 20, quote_amount='1000'),
                  buyer(2, 20.2, side='sell', quote_amount='1000')])
        m = snapshot(s, 30)
        assert Decimal(m['net_buy_flow_usdc']) == 0
        assert Decimal(m['adjusted_volume_usdc']) == 2000


def test_common_funder_is_proxy_and_hubs_not_merged():
    from arc_monitor.metrics import clusters
    funds = [Funding(id=str(i), ts=T, source='test', wallet=wallet(i), funder=wallet(999), amount_usdc=5)
             for i in (1,2)]
    first = {wallet(1): T+timedelta(minutes=1), wallet(2): T+timedelta(minutes=1)}
    c, coverage = clusters(first, funds)
    assert len(set(c.values())) == 1
    assert coverage == 1
    c, _ = clusters(first, [f.model_copy(update={'hub': True}) for f in funds])
    assert len(set(c.values())) == 2


def test_future_funding_not_used_for_clustering():
    from arc_monitor.metrics import clusters
    first = {wallet(1): T, wallet(2): T}
    funds = [Funding(id=str(i),ts=T+timedelta(minutes=2),source='test',wallet=wallet(i),
                     funder=wallet(999),amount_usdc=5) for i in (1,2)]
    c, coverage = clusters(first, funds)
    assert len(set(c.values())) == 2
    assert coverage == 0


def test_holder_denominator_excludes_infrastructure_but_not_creator():
    from arc_monitor.metrics import holder_stats
    h = Holders(id='h',token=A,ts=T,source='test',complete=True,total_supply=1000,
        accounts=[{'wallet':wallet(1),'balance':800,'role':'pool'},
                  {'wallet':W,'balance':100}, {'wallet':wallet(2),'balance':100}])
    out = holder_stats(h)
    assert out['holders_count'] == 2
    assert out['top1_eligible_share'] == .5
    assert out['top10_supply_share'] == .2


def test_partial_holders_do_not_invent_concentration():
    from arc_monitor.metrics import holder_stats
    h = Holders(id='h',token=A,ts=T,source='test',complete=False,
        accounts=[{'wallet':wallet(1),'balance':800}])
    assert holder_stats(h)['top10_eligible_share'] is None


def test_stale_liquidity_is_unknown_not_zero():
    with Store(':memory:', mode='demo') as s:
        s.ingest([launch(), Liquidity(id='l', token=A,ts=T,pool='pool1',amount_usdc=10000,source='test')])
        assert snapshot(s,60)['liquidity_usdc'] is None


def test_current_liquidity_retention_and_fdv_not_market_cap():
    with Store(':memory:', mode='demo') as s:
        s.ingest([launch(), buyer(1,58,quantity=10,quote_amount=100),
            Liquidity(id='p',token=A,ts=T+timedelta(minutes=50),pool='pool1',amount_usdc=10000,source='test'),
            Liquidity(id='l',token=A,ts=T+timedelta(minutes=59),pool='pool1',amount_usdc=2500,source='test'),
            Holders(id='h',token=A,ts=T+timedelta(minutes=59),source='test',total_supply=1000,complete=False,accounts=[])])
        m = snapshot(s,60)
        assert m['liquidity_retention'] == .25
        assert Decimal(m['fdv_usdc']) == 10000
        assert m['market_cap_usdc'] is None


def test_complete_evidence_can_produce_risk_score():
    with Store(':memory:', mode='demo') as s:
        s.ingest([launch(), buyer(1,58),
            Liquidity(id='l',token=A,ts=T+timedelta(minutes=59),pool='pool1',amount_usdc=10000,source='test'),
            Holders(id='h',token=A,ts=T+timedelta(minutes=59),source='test',complete=True,
                    accounts=[{'wallet':wallet(i),'balance':1} for i in range(1,101)]),
            Risk(id='r',token=A,ts=T+timedelta(minutes=59),source='test',buy_tax_bps=0,
                 sell_tax_bps=0,dangerous_permissions=False,lp_locked=True,evidence='test fixture')])
        assert snapshot(s,60)['risk_score'] is not None


def test_survival_censored_without_coverage():
    from arc_monitor.research import outcome
    with Store(':memory:', mode='demo') as s:
        s.ingest([launch()])
        assert outcome(s,launch(),1,T+timedelta(hours=2))['survived'] is None


def test_survival_zero_trades_with_full_coverage_is_false():
    from arc_monitor.research import outcome
    with Store(':memory:', mode='demo') as s:
        s.ingest([launch()])
        s.record_window('trades',T,T+timedelta(hours=2),[],'test')
        assert outcome(s,launch(),1,T+timedelta(hours=2))['survived'] is False


def test_survival_observed_is_true_and_immature_is_null():
    from arc_monitor.research import outcome
    with Store(':memory:', mode='demo') as s:
        s.ingest([launch(), buyer(1,50), buyer(2,55)])
        s.record_window('trades',T,T+timedelta(hours=2),[],'test')
        assert outcome(s,launch(),1,T+timedelta(hours=2))['survived'] is True
        assert outcome(s,launch(),6,T+timedelta(hours=2))['survived'] is None


def test_tenth_buyer_baseline_not_first_dust_price():
    from arc_monitor.metrics import baseline
    rows = [buyer(i, i, quantity=10, quote_amount=100) for i in range(1,11)]
    rows.insert(0, buyer(99,.1,quantity=1000,quote_amount='.000001'))
    result = baseline(rows, creator=None, funds=[])
    assert result['price_usdc'] == '10'
    assert result['timestamp'] == '2026-09-16T00:10:00Z'


def test_price_label_is_not_return_from_before_feature_cutoff():
    from arc_monitor.research import future_label
    with Store(':memory:', mode='demo') as s:
        s.ingest([launch(), *[buyer(i,i,quantity=10,quote_amount=100) for i in range(1,11)],
                  buyer(100,20,quantity=1,quote_amount=1000), buyer(101,40,quantity=10,quote_amount=100)])
        s.record_window('trades',T,T+timedelta(hours=2),[],'test')
        label = future_label(s,launch(),T+timedelta(minutes=30),1,T+timedelta(hours=2))
        assert label['future_price_5x'] is False


def test_funder_cluster_does_not_get_independent_buyer_score():
    with Store(':memory:',mode='demo') as s:
        rows=[buyer(i,26+i/100,quote_amount=100) for i in range(1,21)]
        s.ingest([launch(),*rows])
        unclustered=snapshot(s,30)
        funds=[Funding(id=f'fund-{i}',ts=T,source='test',wallet=wallet(i),funder=wallet(999),amount_usdc=100)
               for i in range(1,21)]
        s.ingest(funds)
        clustered=snapshot(s,30)
        assert clustered['unique_buyers']==20 and clustered['buyer_clusters_estimate']==1
        assert clustered['runner_components']['new_buyers']<unclustered['runner_components']['new_buyers']
        assert clustered['runner_components']['external_buyers']<unclustered['runner_components']['external_buyers']
        assert clustered['stage']=='TRIAGE'
        assert clustered['observed_risk_score']<unclustered['observed_risk_score']
