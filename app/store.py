import hashlib
import json
import os
import sqlite3
from pathlib import Path

from app.errors import DomainError


def connection():
    path = Path(os.environ.get('APP_DB', 'data/runs.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    _ensure_revision_tables(db)
    return db


def _ensure_revision_tables(db):
    # 修正版本：从父批次派生的不可变快照，按 revision 单调递增
    db.execute(
        'CREATE TABLE IF NOT EXISTS run_versions('
        'run_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,'
        ' decision TEXT NOT NULL DEFAULT "待复核", created_at TEXT NOT NULL,'
        'PRIMARY KEY(run_id, revision))'
    )
    # 云区复查记录：按批次+版本号收口；同版异值按 request_key 隔离
    db.execute(
        'CREATE TABLE IF NOT EXISTS recheck_requests('
        'request_key TEXT PRIMARY KEY, run_id TEXT NOT NULL, base_revision INTEGER NOT NULL,'
        ' target_revision INTEGER NOT NULL, status TEXT NOT NULL,'
        ' recheck_text TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL,'
        ' updated_at TEXT NOT NULL)'
    )
    db.execute('CREATE INDEX IF NOT EXISTS idx_recheck_run ON recheck_requests(run_id, target_revision)')
    db.commit()


def save(result):
    with connection() as db:
        ident = result['manifest']['run_id']
        row = db.execute('SELECT payload,revision,decision FROM runs WHERE id=?', (ident,)).fetchone()
        if row:
            # 历史库（版本表引入前的批次）补建 r1 版本链，保证后续派生有父版本
            exists = db.execute('SELECT 1 FROM run_versions WHERE run_id=? AND revision=?',
                                (ident, row['revision'])).fetchone()
            if not exists:
                db.execute(
                    'INSERT OR IGNORE INTO run_versions(run_id,revision,payload,decision,created_at) VALUES (?,?,?,?,?)',
                    (ident, int(row['revision']), row['payload'], row['decision'], result['manifest']['created_at']),
                )
            return {**json.loads(row['payload']), 'revision': row['revision'], 'decision': row['decision'], 'reused': True}
        db.execute('INSERT INTO runs(id,payload) VALUES (?,?)', (ident, json.dumps(result, ensure_ascii=False)))
        db.execute(
            'INSERT INTO run_versions(run_id,revision,payload,decision,created_at) VALUES (?,?,?,?,?)',
            (ident, 1, json.dumps(result, ensure_ascii=False), '待复核', result['manifest']['created_at']),
        )
    return {**result, 'revision': 1, 'decision': '待复核', 'reused': False}


def get_run(ident):
    with connection() as db:
        row = db.execute('SELECT payload,revision,decision FROM runs WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise DomainError('NOT_FOUND', '批次不存在', 404)
        return {**json.loads(row['payload']), 'revision': row['revision'], 'decision': row['decision']}


def latest():
    with connection() as db:
        row = db.execute('SELECT payload,revision,decision FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
        return {**json.loads(row['payload']), 'revision': row['revision'], 'decision': row['decision']} if row else None


def history():
    with connection() as db:
        return [dict(r) for r in db.execute('SELECT id,revision,decision FROM runs ORDER BY rowid DESC')]


def review(ident, revision, decision):
    if decision not in ('通过', '退回'):
        raise DomainError('DECISION', '复核动作无效')
    with connection() as db:
        row = db.execute('SELECT payload FROM runs WHERE id=? AND revision=?', (ident, revision)).fetchone()
        if not row:
            raise DomainError('VERSION', '版本已变化，请重新载入', 409)
        current = revision + 1
        # 复核推进版本号：在版本链落一个同内容的复核快照，保证链头连续可追溯
        db.execute(
            'INSERT OR IGNORE INTO run_versions(run_id,revision,payload,decision,created_at) VALUES (?,?,?,?,?)',
            (ident, current, row['payload'], decision, _now()),
        )
        changed = db.execute('UPDATE runs SET revision=?,decision=? WHERE id=? AND revision=?',
                             (current, decision, ident, revision)).rowcount
        if not changed:
            raise DomainError('VERSION', '版本已变化，请重新载入', 409)
    return {'revision': current, 'decision': decision}


# ---------------------------------------------------------------- 云区复查修正版本

def request_key_for(ident, recheck_text):
    digest = hashlib.sha256(recheck_text.encode('utf-8')).hexdigest()
    return hashlib.sha256((ident + ':' + digest).encode('utf-8')).hexdigest()[:20]


def latest_revision_no(db, ident):
    row = db.execute('SELECT MAX(revision) AS max_rev FROM run_versions WHERE run_id=?', (ident,)).fetchone()
    value = row['max_rev']
    return int(value) if value is not None else 0


def register_recheck(ident, recheck_text, build_snapshot):
    """收复查记录并派生修正版本。

    build_snapshot(parent) -> child payload（先在事务外算好，落库失败不影响旧版）。
    幂等：同一 (批次, 复查内容) 重试返回既有请求，不新增版本；
    单调：晚到的旧基版拒绝；中断后未完成请求可续跑。
    """
    parent = get_run(ident)
    base_revision = int(parent['revision'])
    request_key = request_key_for(ident, recheck_text)

    with connection() as db:
        existing = db.execute('SELECT * FROM recheck_requests WHERE request_key=?', (request_key,)).fetchone()
        latest_no = latest_revision_no(db, ident)
        now = _now()
        if existing:
            record = dict(existing)
            if record['status'] == 'succeeded':
                return _recheck_response(db, record, reused=True)
            # running（中断续跑）或 failed（重试）：沿用原目标版本号，不新增版本；
            # 但若版本链已被其他复查推进到该目标版本之后，则晚到旧修订仍须拒绝
            if record['target_revision'] <= latest_no and latest_no >= record['target_revision']:
                occupied = db.execute(
                    'SELECT 1 FROM run_versions WHERE run_id=? AND revision=?',
                    (ident, record['target_revision']),
                ).fetchone()
                if occupied:
                    raise DomainError('STALE_REVISION',
                                      '复查目标版本 %d 已被占用，晚到旧修订不能倒退' % record['target_revision'], 409)
            target_revision = record['target_revision']
            db.execute('UPDATE recheck_requests SET status=?, updated_at=? WHERE request_key=?',
                       ('running', now, request_key))
        else:
            # 晚到旧修订：请求所基版本落后于版本链头部即拒绝，不允许倒退
            if base_revision < latest_no:
                raise DomainError('STALE_REVISION',
                                  '复查基于旧修订 %d，当前版本 %d，不能倒退' % (base_revision, latest_no), 409)
            target_revision = latest_no + 1
            db.execute(
                'INSERT INTO recheck_requests(request_key,run_id,base_revision,target_revision,status,recheck_text,detail,created_at,updated_at)'
                ' VALUES (?,?,?,?,?,?,?,?,?)',
                (request_key, ident, base_revision, target_revision, 'running', recheck_text,
                 json.dumps({}, ensure_ascii=False), now, now),
            )

    # 事务外重算：任何异常都只把请求标记 failed，旧版继续可用
    try:
        child = build_snapshot(parent)
    except DomainError as exc:
        _mark_recheck(request_key, 'failed', {'code': exc.code, 'message': exc.message})
        raise
    except Exception as exc:  # 中断/未知错误：保留旧版，允许续跑重试
        _mark_recheck(request_key, 'failed', {'code': 'RECHECK_FAILED', 'message': str(exc)})
        raise DomainError('RECHECK_FAILED', '修正版本派生失败，旧版保留：%s' % exc, 500)

    child['manifest']['recheck']['request_key'] = request_key
    child['manifest']['recheck']['base_revision'] = base_revision
    now = _now()
    with connection() as db:
        try:
            db.execute(
                'INSERT INTO run_versions(run_id,revision,payload,decision,created_at) VALUES (?,?,?,?,?)',
                (ident, target_revision, json.dumps(child, ensure_ascii=False), '待复核', now),
            )
        except sqlite3.IntegrityError:
            # 并发下目标版本已被其他复查占用：放弃本次切换，旧版保留
            _mark_recheck(request_key, 'failed', {'code': 'VERSION', 'message': '目标版本已被其他复查占用'})
            raise DomainError('VERSION', '修正版本冲突，旧版保留', 409)
        detail = json.dumps(child['manifest']['recheck'], ensure_ascii=False)
        db.execute(
            'UPDATE recheck_requests SET status=?, detail=?, updated_at=? WHERE request_key=?',
            ('succeeded', detail, now, request_key),
        )
        # 切换当前指针与快照；只有全部成功才执行，失败时 runs 保持旧版
        db.execute('UPDATE runs SET revision=?, decision=?, payload=? WHERE id=?',
                   (target_revision, '待复核', json.dumps(child, ensure_ascii=False), ident))
        record = dict(db.execute('SELECT * FROM recheck_requests WHERE request_key=?', (request_key,)).fetchone())
        response = _recheck_response(db, record, reused=False)
    return response


def _mark_recheck(request_key, status, detail):
    with connection() as db:
        db.execute(
            'UPDATE recheck_requests SET status=?, detail=?, updated_at=? WHERE request_key=?',
            (status, json.dumps(detail, ensure_ascii=False), _now(), request_key),
        )


def _recheck_response(db, record, reused):
    ident = record['run_id']
    row = db.execute('SELECT payload,revision,decision FROM runs WHERE id=?', (ident,)).fetchone()
    payload = json.loads(row['payload'])
    detail = json.loads(record['detail']) if record['detail'] else {}
    if detail:
        payload.setdefault('manifest', {})['recheck'] = detail
    return {
        **payload,
        'revision': row['revision'],
        'decision': row['decision'],
        'recheck': {**detail, 'request_key': record['request_key'],
                    'status': record['status'], 'reused': reused},
    }


def list_versions(ident):
    with connection() as db:
        if not db.execute('SELECT 1 FROM runs WHERE id=?', (ident,)).fetchone():
            raise DomainError('NOT_FOUND', '批次不存在', 404)
        rows = db.execute(
            'SELECT run_id,revision,decision,created_at FROM run_versions WHERE run_id=? ORDER BY revision',
            (ident,),
        ).fetchall()
        requests_rows = db.execute(
            'SELECT request_key,base_revision,target_revision,status,created_at,updated_at,detail'
            ' FROM recheck_requests WHERE run_id=? ORDER BY target_revision, created_at', (ident,),
        ).fetchall()
    versions = [dict(r) for r in rows]
    requests = []
    for r in requests_rows:
        item = dict(r)
        item['detail'] = json.loads(item['detail']) if item['detail'] else {}
        requests.append(item)
    return {'run_id': ident, 'versions': versions, 'rechecks': requests}


def get_version(ident, revision):
    with connection() as db:
        row = db.execute(
            'SELECT payload,revision,decision FROM run_versions WHERE run_id=? AND revision=?',
            (ident, revision),
        ).fetchone()
        if row is None:
            raise DomainError('NOT_FOUND', '修正版本不存在', 404)
        return {**json.loads(row['payload']), 'revision': row['revision'], 'decision': row['decision']}


def _now():
    import time
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
