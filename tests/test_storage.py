from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

UTC = timezone.utc
T = datetime(2026, 9, 16, tzinfo=UTC)
A = '0x' + 'a' * 40
W = '0x' + 'b' * 40


def get_types():
    from arc_monitor.models import Launch, Trade
    from arc_monitor.storage import Store
    return Launch, Trade, Store


def trade(**updates):
    _, Trade, _ = get_types()
    data = dict(id='t1', token=A, ts=T, tx='0x'+'1'*64, wallet=W, side='buy',
                quantity='100', quote_amount='1.000000000000000001', pool='pool1', source='test')
    data.update(updates)
    return Trade(**data)


def test_schema_exists():
    import importlib.util
    assert importlib.util.find_spec('arc_monitor.models') is not None, 'event schema is not implemented'


def test_decimal_roundtrip_and_dedup():
    _, _, Store = get_types()
    with Store(':memory:', mode='demo') as s:
        assert s.ingest([trade()]) == 1
        assert s.ingest([trade()]) == 0
        assert s.events('trade')[0].quote_amount == Decimal('1.000000000000000001')


def test_conflicting_duplicate_rolls_back_entire_batch():
    _, _, Store = get_types()
    with Store(':memory:', mode='demo') as s:
        s.ingest([trade()])
        with pytest.raises(ValueError, match='conflict'):
            s.ingest([trade(id='t2'), trade(quote_amount='900')])
        assert len(s.events('trade')) == 1


def test_mode_isolation(tmp_path):
    _, _, Store = get_types()
    path = str(tmp_path/'db.sqlite')
    Store(path, mode='demo').close()
    with pytest.raises(ValueError, match='mode'):
        Store(path, mode='live')


def test_half_open_time_filter():
    _, _, Store = get_types()
    with Store(':memory:', mode='demo') as s:
        s.ingest([trade()])
        assert s.events('trade', before=T) == []
        assert len(s.events('trade', before=T+timedelta(microseconds=1))) == 1


def test_coverage_merges_but_never_hides_gap():
    _, _, Store = get_types()
    with Store(':memory:', mode='demo') as s:
        s.record_window('trades', T, T+timedelta(minutes=10), [], 'test')
        s.record_window('trades', T+timedelta(minutes=11), T+timedelta(minutes=20), [], 'test')
        assert not s.covered('trades', T, T+timedelta(minutes=20))
        s.record_window('trades', T+timedelta(minutes=10), T+timedelta(minutes=11), [], 'test')
        assert s.covered('trades', T, T+timedelta(minutes=20))


def test_window_and_cursor_are_atomic():
    _, _, Store = get_types()
    with Store(':memory:', mode='demo') as s:
        s.ingest([trade()])
        with pytest.raises(ValueError):
            s.record_window('trades', T, T+timedelta(hours=1), [trade(quote_amount='7')], 'test')
        assert s.checkpoint('trades') is None
        assert not s.covered('trades', T, T+timedelta(hours=1))


@pytest.mark.parametrize('field,value', [('quantity','NaN'), ('quote_amount','-1'),
                                        ('quote_amount','Infinity'), ('wallet','garbage'),
                                        ('ts', datetime(2026,9,16))])
def test_invalid_financial_inputs_rejected(field, value):
    with pytest.raises(ValueError):
        trade(**{field: value})


def test_addresses_are_canonicalized():
    assert trade(wallet='0x'+'B'*40).wallet == W


@pytest.mark.parametrize('amount',['1e10000','1e-10000'])
def test_extreme_amount_cannot_overflow_analytics(amount):
    with pytest.raises(ValueError):
        trade(quote_amount=amount)
