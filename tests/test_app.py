import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arc_monitor.storage import Store


def test_runnable_app_modules_exist():
    for name in ['demo','app','cli']:
        assert importlib.util.find_spec('arc_monitor.'+name) is not None, name+' not implemented'


def test_demo_is_deterministic_idempotent_and_separate():
    from arc_monitor.demo import seed_demo, DEMO_ASOF
    with Store(':memory:',mode='demo') as s:
        first=seed_demo(s)
        assert first['inserted'] > 100
        assert s.count('launch') == 7
        assert seed_demo(s)['inserted'] == 0
        assert s.covered('trades', DEMO_ASOF.replace(day=16,hour=0), DEMO_ASOF)
    with Store(':memory:') as s:
        with pytest.raises(ValueError,match='demo'):
            seed_demo(s)
        assert s.count('launch') == 0


def test_api_filter_detail_and_missing_risk():
    from arc_monitor.demo import seed_demo
    from arc_monitor.app import create_app
    with Store(':memory:',mode='demo') as s:
        seed_demo(s)
        with TestClient(create_app(s)) as c:
            status=c.get('/api/status').json()
            assert status['mode']=='demo' and status['synthetic'] is True
            data=c.get('/api/tokens').json()
            assert data['total']==7
            token=data['items'][0]['token']
            assert c.get('/api/tokens',params={'q':token}).json()['total']==1
            detail=c.get('/api/tokens/'+token).json()
            assert detail['metrics']['token']==token
            assert detail['series'] and 'price_usdc' in detail['series'][0]
            assert c.get('/api/tokens',params={'sort':'secret'}).status_code==422
            assert c.get('/api/tokens',params={'limit':100001}).status_code==422
            assert c.get('/api/tokens/not-an-address').status_code==422
            assert c.get('/api/tokens/'+'0x'+'f'*40).status_code==404
            assert c.post('/api/tokens').status_code==405
            assert 'default-src' in c.get('/').headers['content-security-policy']
            assert c.get('/').status_code==200
            assert c.get('/assets/app.js').status_code==200
            assert c.get('/assets/../../pyproject.toml').status_code==404
            assert 'scope' in c.get('/api/ecosystem').json()


def test_cli_demo_research_and_missing_key(tmp_path,capsys,monkeypatch):
    from arc_monitor.cli import main
    db=str(tmp_path/'demo.db')
    assert main(['--db',db,'--mode','demo','demo'])==0
    assert main(['--db',db,'--mode','demo','research','--out',str(tmp_path/'report')])==0
    report=json.loads((tmp_path/'report'/'summary.json').read_text())
    assert report['mode']=='demo' and report['rows']>0
    assert (tmp_path/'report'/'features_and_outcomes.csv').is_file()
    monkeypatch.delenv('BITQUERY_TOKEN',raising=False)
    assert main(['--db',str(tmp_path/'live.db'),'backfill','--start','2026-09-16T00:00:00Z',
                 '--end','2026-09-16T00:01:00Z'])==2
    assert 'BITQUERY_TOKEN' in capsys.readouterr().err


def test_cli_import_invalid_file_is_atomic(tmp_path):
    from arc_monitor.cli import main
    from test_storage import trade
    f=tmp_path/'data.jsonl'
    f.write_text(trade().model_dump_json()+'\n'+json.dumps({'bad':'row'}))
    db=str(tmp_path/'arc.db')
    assert main(['--db',db,'import-jsonl',str(f)])==2
    with Store(db) as s:
        assert s.count('trade')==0


def test_alerts_are_local_and_idempotent():
    from arc_monitor.demo import seed_demo, DEMO_ASOF
    from arc_monitor.alerting import evaluate_alerts
    with Store(':memory:',mode='demo') as s:
        seed_demo(s)
        first=evaluate_alerts(s,DEMO_ASOF,min_runner=0,min_risk=0)
        assert first and all(a['mode']=='demo' for a in first)
        assert evaluate_alerts(s,DEMO_ASOF,min_runner=0,min_risk=0)==[]
        assert s.alert_list()==first


def test_research_split_has_no_crossing_label_intervals():
    from arc_monitor.research import purged_time_split
    from test_storage import T
    from datetime import timedelta
    from arc_monitor.models import iso
    rows=[{'feature_cutoff':iso(T+timedelta(hours=a)), 'label_end':iso(T+timedelta(hours=b))}
          for a,b in [(0,2),(1,5),(4,8)]]
    train,test=purged_time_split(rows,T+timedelta(hours=4))
    assert train==[rows[0]] and test==[rows[2]]


def test_asgi_factory_uses_explicit_dataset(tmp_path,monkeypatch):
    from arc_monitor.app import app_from_env
    monkeypatch.setenv('ARC_DB',str(tmp_path/'app.db'))
    monkeypatch.setenv('ARC_MODE','live')
    with TestClient(app_from_env()) as c:
        assert c.get('/api/status').json()['mode']=='live'
        assert c.get('/api/tokens').json()['total']==0
