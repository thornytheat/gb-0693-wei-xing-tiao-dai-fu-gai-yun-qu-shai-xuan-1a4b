"""云区复查修正版本：遮罩解析、派生差额、版本收口与 HTTP 契约。"""
import io
from decimal import Decimal

import pytest

from app.errors import DomainError
from app.pipeline import calculate
from app.revisions.builder import build_revision
from app.revisions.mask import parse_recheck
from app.store import (
    get_run, get_version, list_versions, register_recheck, save,
)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / 'recheck.sqlite3'
    monkeypatch.setenv('APP_DB', str(path))
    return path


@pytest.fixture
def batch(db_path):
    return save(calculate())


@pytest.fixture
def ident(batch):
    return batch['manifest']['run_id']


def derive(ident_value, text):
    return register_recheck(ident_value, text, lambda parent: build_revision(parent, text))


# ---------------------------------------------------------- 遮罩解析

def test_parse_basic_and_dateline():
    masks, rejected = parse_recheck('scene,cloud_w,cloud_e\nSC0000,165,166\nDATELINE,179,-178\n')
    assert rejected == []
    assert masks['SC0000'] == [(Decimal('165'), Decimal('166'))]
    assert len(masks['DATELINE']) == 2  # 跨 180 拆分


def test_parse_same_version_conflict_isolated():
    text = 'scene,cloud_w,cloud_e\nSC0000,165,166\nSC0000,165,167\n'
    masks, rejected = parse_recheck(text)
    assert 'SC0000' not in masks
    assert len(rejected) == 2 and all(r['原因码'] == 'RECHECK_CONFLICT' for r in rejected)


def test_parse_same_value_duplicate_is_consistent():
    masks, rejected = parse_recheck('scene,cloud_w,cloud_e\nSC0000,165,166\nSC0000,165,166\n')
    assert rejected == [] and masks['SC0000'] == [(Decimal('165'), Decimal('166'))]


def test_parse_missing_endpoint_and_bad_header():
    masks, rejected = parse_recheck('scene,cloud_w,cloud_e\nSC0000,165,\n')
    assert rejected[0]['原因码'] == 'CLOUD'
    with pytest.raises(DomainError) as exc:
        parse_recheck('a,b\n1,2\n')
    assert exc.value.code == 'HEADER'
    with pytest.raises(DomainError):
        parse_recheck('')


def test_parse_empty_cloud_means_cleared_mask():
    masks, rejected = parse_recheck('scene,cloud_w,cloud_e\nSC0000,,\n')
    assert rejected == [] and masks['SC0000'] == []


# ---------------------------------------------------------- 派生与差额

def test_derivation_preserves_parent_and_recomputes(ident):
    text = 'scene,cloud_w,cloud_e\nDATELINE,179,-178\n'
    child = derive(ident, text)
    assert child['revision'] == 2
    parent = get_version(ident, 1)
    assert 'recheck' not in parent['manifest']
    # 已签核批次的条带、隔离、采购证据原样保留（r1 快照不可变）
    assert parent['quarantine'] == get_version(ident, 1)['quarantine']
    assert parent['stages']['6']['rows'] == get_version(ident, 1)['stages']['6']['rows']
    diff = child['manifest']['recheck']['diff']
    assert diff['覆盖宽度_旧_度'] == '20'
    assert diff['覆盖宽度_新_度'] == '19'
    assert diff['覆盖差额_度'] == '-1'
    assert diff['失去覆盖段'] and not diff['新增覆盖段']


def test_unknown_scene_is_rejected_not_applied(ident):
    text = 'scene,cloud_w,cloud_e\nGHOST,1,2\n'
    child = derive(ident, text)
    rejected = child['manifest']['recheck']['rejected']
    assert any(r['原因码'] == 'RECHECK_UNKNOWN' and r['scene'] == 'GHOST' for r in rejected)
    assert child['manifest']['recheck']['applied_scenes'] == []
    # 没有可采信复查时，覆盖与父版一致
    assert child['manifest']['recheck']['diff']['覆盖差额_度'] == '0'


def test_scene_level_width_delta_and_union_redundancy(ident):
    # SC0005 跨度 170-172 落在目标区内，完全云掩后场景有效宽度 -2
    text = 'scene,cloud_w,cloud_e\nSC0005,170,172\nSC0020,170,172\nSC0035,170,172\n'
    child = derive(ident, text)
    row = next(r for r in child['manifest']['recheck']['mask_rows'] if r['场景'] == 'SC0005')
    assert row['原有效宽度_度'] == '2' and row['新有效宽度_度'] == '0'
    assert row['有效宽度差额_度'] == '-2'
    diff = child['manifest']['recheck']['diff']
    # 冗余条带补齐并集：总覆盖不减少，但采购候选随遮罩变化
    assert diff['覆盖宽度_新_度'] == '20'
    assert diff['退出采购场景']  # 贪心选择受影响
    from decimal import Decimal
    assert Decimal(diff['采购额差额_CNY']) <= 0


def test_real_union_gap_when_all_redundancy_masked(ident):
    # 云掩所有触及 170-172 的条带，制造真实并集缺口
    scenes = ['SC0004', 'SC0005', 'SC0006', 'SC0019', 'SC0020', 'SC0021', 'SC0034', 'SC0035', 'SC0036']
    lines = ['scene,cloud_w,cloud_e'] + ['%s,170,172' % s for s in scenes]
    child = derive(ident, '\n'.join(lines) + '\n')
    diff = child['manifest']['recheck']['diff']
    assert Decimal(diff['覆盖差额_度']) == -2
    assert diff['失去覆盖段'] == [{'起始经度': '170', '结束经度': '172'}]
    assert Decimal(diff['缺口宽度_新_度']) == 2
    # 守恒：新覆盖 + 新缺口 = 20
    assert Decimal(diff['覆盖宽度_新_度']) + Decimal(diff['缺口宽度_新_度']) == 20


def test_parent_quarantine_evidence_unchanged_in_child(ident, batch):
    text = 'scene,cloud_w,cloud_e\nSC0001,166,167\n'
    child = derive(ident, text)
    assert child['quarantine'] == batch['quarantine']


def test_chained_revision_links_parent(ident):
    first = derive(ident, 'scene,cloud_w,cloud_e\nSC0000,165,166\n')
    second = derive(ident, 'scene,cloud_w,cloud_e\nSC0001,166,167\n')
    assert first['revision'] == 2 and second['revision'] == 3
    assert second['manifest']['recheck']['parent_revision'] == 2
    assert [v['revision'] for v in list_versions(ident)['versions']] == [1, 2, 3]


# ---------------------------------------------------------- 版本收口

def test_retry_is_idempotent_and_adds_no_version(ident):
    text = 'scene,cloud_w,cloud_e\nSC0000,165,166\n'
    first = derive(ident, text)
    again = derive(ident, text)
    assert again['revision'] == first['revision']
    assert again['recheck']['reused'] is True
    assert len(list_versions(ident)['versions']) == 2


def test_stale_late_revision_rejected(ident, monkeypatch):
    derive(ident, 'scene,cloud_w,cloud_e\nSC0000,165,166\n')  # 推进到 r2
    import app.store as store
    original = store.get_run
    monkeypatch.setattr(store, 'get_run', lambda value: {**original(value), 'revision': 1})
    with pytest.raises(DomainError) as exc:
        derive(ident, 'scene,cloud_w,cloud_e\nSC0001,166,167\n')
    assert exc.value.code == 'STALE_REVISION' and exc.value.status == 409
    monkeypatch.setattr(store, 'get_run', original)
    assert get_run(ident)['revision'] == 2


def test_failed_request_rejected_once_chain_moved_past_it(ident):
    # 一个失败请求登记目标 r2；随后另一条复查成功占据 r2，旧请求重试不得倒退
    from app.store import connection, request_key_for
    stale_text = 'scene,cloud_w,cloud_e\nSC0002,167,168\n'
    key = request_key_for(ident, stale_text)
    with connection() as db:
        db.execute(
            'INSERT INTO recheck_requests(request_key,run_id,base_revision,target_revision,status,'
            'recheck_text,detail,created_at,updated_at) VALUES (?,?,1,2,?,?,?,?,?)',
            (key, ident, 'failed', stale_text, '{}', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
        )
    derive(ident, 'scene,cloud_w,cloud_e\nSC0003,168,169\n')  # 成功推进到 r2
    with pytest.raises(DomainError) as exc:
        derive(ident, stale_text)
    assert exc.value.code == 'STALE_REVISION' and exc.value.status == 409
    assert get_run(ident)['revision'] == 2


def test_failure_keeps_old_version_and_retry_uses_same_slot(ident):
    bad = 'wrong,headers\n1,2\n'
    for _ in range(2):
        with pytest.raises(DomainError):
            derive(ident, bad)
    assert get_run(ident)['revision'] == 1
    assert [v['revision'] for v in list_versions(ident)['versions']] == [1]
    request = next(q for q in list_versions(ident)['rechecks'] if q['status'] == 'failed')
    # 失败请求占用的目标版本仍是 2，续跑成功后不产生跳号
    ok = derive(ident, 'scene,cloud_w,cloud_e\nSC0000,165,165.5\n')
    assert ok['revision'] == 2
    assert request['target_revision'] == 2


def test_interrupted_running_request_can_resume(ident):
    # 模拟中断：请求已登记为 running 但未完成，续跑沿用同一目标版本号
    text = 'scene,cloud_w,cloud_e\nSC0000,165,166\n'
    from app.store import connection, request_key_for
    key = request_key_for(ident, text)
    with connection() as db:
        db.execute(
            'INSERT INTO recheck_requests(request_key,run_id,base_revision,target_revision,status,'
            'recheck_text,detail,created_at,updated_at) VALUES (?,?,1,2,?,?,?,?,?)',
            (key, ident, 'running', text, '{}', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
        )
    resumed = derive(ident, text)
    assert resumed['revision'] == 2
    assert resumed['recheck']['status'] == 'succeeded'
    assert [v['revision'] for v in list_versions(ident)['versions']] == [1, 2]
    # 再次提交仍是幂等返回
    assert derive(ident, text)['recheck']['reused'] is True


# ---------------------------------------------------------- HTTP 契约

def test_recheck_endpoint_json_and_versions(client):
    run = client.post('/api/run', json={}).get_json()
    rid = run['manifest']['run_id']
    resp = client.post('/api/runs/%s/recheck' % rid,
                       json={'recheck_csv': 'scene,cloud_w,cloud_e\nSC0000,165,166\n'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['revision'] == 2 and body['manifest']['recheck']
    ledger = client.get('/api/runs/%s/versions' % rid).get_json()
    assert [v['revision'] for v in ledger['versions']] == [1, 2]
    detail = client.get('/api/runs/%s/versions/1' % rid)
    assert detail.status_code == 200 and 'recheck' not in detail.get_json()['manifest']
    assert client.get('/api/runs/%s/versions/99' % rid).status_code == 404


def test_recheck_endpoint_upload(client):
    run = client.post('/api/run', json={}).get_json()
    rid = run['manifest']['run_id']
    data = {'file': (io.BytesIO('scene,cloud_w,cloud_e\nSC0000,165,166\n'.encode()), 'r.csv')}
    resp = client.post('/api/runs/%s/recheck' % rid, data=data, content_type='multipart/form-data')
    assert resp.status_code == 200 and resp.get_json()['revision'] == 2


def test_recheck_unknown_batch_404(client):
    resp = client.post('/api/runs/nope/recheck', json={'recheck_csv': 'scene,cloud_w,cloud_e\nA,1,2\n'})
    assert resp.status_code == 404


def test_recheck_bad_payload_keeps_old_version(client):
    run = client.post('/api/run', json={}).get_json()
    rid = run['manifest']['run_id']
    resp = client.post('/api/runs/%s/recheck' % rid, json={'recheck_csv': 'a,b\n1,2\n'})
    assert resp.status_code == 422
    current = client.get('/api/runs/%s' % rid).get_json()
    assert current['revision'] == 1


def test_recheck_non_object_json_rejected(client):
    run = client.post('/api/run', json={}).get_json()
    rid = run['manifest']['run_id']
    resp = client.post('/api/runs/%s/recheck' % rid, json=['not', 'object'])
    assert resp.status_code == 400


def test_recheck_page_renders(client):
    run = client.post('/api/run', json={}).get_json()
    rid = run['manifest']['run_id']
    client.post('/api/runs/%s/recheck' % rid,
                json={'recheck_csv': 'scene,cloud_w,cloud_e\nSC0000,165,166\n'})
    page = client.get('/recheck?run_id=%s' % rid).data.decode()
    assert '云区复查' in page and 'versions --ledger' in page and 'mask --old-vs-new' in page
    assert 'SC0000' in page


def test_evidence_csv_contains_diff_and_version_sections(client):
    run = client.post('/api/run', json={}).get_json()
    rid = run['manifest']['run_id']
    client.post('/api/runs/%s/recheck' % rid,
                json={'recheck_csv': 'scene,cloud_w,cloud_e\nDATELINE,179,-178\n'})
    text = client.get('/api/evidence.csv?run_id=%s&revision=2' % rid).data.decode('utf-8-sig')
    assert '云区复查遮罩对照' in text
    assert '新旧覆盖与采购差额' in text
    assert '覆盖差额_度' in text and '父批次版本' in text
    # 父版本证据不含复查区块
    v1 = client.get('/api/evidence.csv?run_id=%s&revision=1' % rid).data.decode('utf-8-sig')
    assert '云区复查遮罩对照' not in v1


def test_module_pages_still_render_after_revision(client):
    run = client.post('/api/run', json={}).get_json()
    rid = run['manifest']['run_id']
    client.post('/api/runs/%s/recheck' % rid,
                json={'recheck_csv': 'scene,cloud_w,cloud_e\nSC0000,165,166\n'})
    for n in range(1, 8):
        assert client.get('/work/%d?run_id=%s' % (n, rid)).status_code == 200


def test_signed_off_review_then_revision_chain(client):
    run = client.post('/api/run', json={}).get_json()
    rid = run['manifest']['run_id']
    client.post('/api/review', json={'id': rid, 'revision': 1, 'decision': '通过'})
    signed = client.get('/api/runs/%s' % rid).get_json()
    assert signed['decision'] == '通过' and signed['revision'] == 2
    # 复核快照进入版本链：r1 原状态冻结，r2 承载签核决定
    v1 = client.get('/api/runs/%s/versions/1' % rid).get_json()
    v2 = client.get('/api/runs/%s/versions/2' % rid).get_json()
    assert v1['decision'] == '待复核' and v2['decision'] == '通过'
    # 已签核后派生修正版本：新批次待复核，签核证据留在历史版本
    resp = client.post('/api/runs/%s/recheck' % rid,
                       json={'recheck_csv': 'scene,cloud_w,cloud_e\nSC0000,165,166\n'})
    assert resp.status_code == 200 and resp.get_json()['decision'] == '待复核'
    ledger = client.get('/api/runs/%s/versions' % rid).get_json()
    assert [v['revision'] for v in ledger['versions']] == [1, 2, 3]
    assert client.get('/api/runs/%s/versions/2' % rid).get_json()['decision'] == '通过'


def test_recheck_after_review_keeps_chain_continuous(client):
    run = client.post('/api/run', json={}).get_json()
    rid = run['manifest']['run_id']
    client.post('/api/review', json={'id': rid, 'revision': 1, 'decision': '退回'})
    resp = client.post('/api/runs/%s/recheck' % rid,
                       json={'recheck_csv': 'scene,cloud_w,cloud_e\nSC0001,166,167\n'})
    assert resp.status_code == 200 and resp.get_json()['revision'] == 3
    ledger = client.get('/api/runs/%s/versions' % rid).get_json()
    assert [v['revision'] for v in ledger['versions']] == [1, 2, 3]
    assert [v['decision'] for v in ledger['versions']] == ['待复核', '退回', '待复核']
