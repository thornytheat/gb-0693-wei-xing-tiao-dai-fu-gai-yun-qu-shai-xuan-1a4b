from flask import Blueprint, jsonify, render_template, request

from app.config import MODULES, SHORT, TITLE
from app.errors import DomainError
from app.reader import decode
from app.revisions.builder import build_revision
from app.store import get_run, get_version, list_versions, register_recheck

bp = Blueprint('recheck', __name__)


def _recheck_payload():
    if request.files:
        if 'file' not in request.files:
            raise DomainError('REQUEST', '上传必须使用file字段', 400)
        return decode(request.files['file'].read())
    body = request.get_json(silent=True) or {}
    text = body.get('recheck_csv') or body.get('csv')
    if not isinstance(text, str):
        raise DomainError('REQUEST', '须提供 recheck_csv 文本或 file 文件', 400)
    return text


@bp.route('/api/runs/<ident>/recheck', methods=['POST'])
def recheck(ident):
    text = _recheck_payload()
    return jsonify(register_recheck(ident, text, lambda parent: build_revision(parent, text)))


@bp.route('/api/runs/<ident>/versions')
def versions(ident):
    return jsonify(list_versions(ident))


@bp.route('/api/runs/<ident>/versions/<int:revision>')
def version_detail(ident, revision):
    return jsonify(get_version(ident, revision))


@bp.route('/recheck')
def recheck_page():
    ident = request.args.get('run_id')
    run = get_run(ident) if ident else None
    ledger = list_versions(run['manifest']['run_id']) if run else None
    return render_template('revisions/recheck.html', title=TITLE, modules=MODULES, short=SHORT,
                           run=run, ledger=ledger)
