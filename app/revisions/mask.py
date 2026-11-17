"""云区复查遮罩：解析复查记录 CSV，按场景汇总最新遮罩区间。

复查记录只携带场景编号与修订后的云区端点（cloud_w/cloud_e），
不接外部卫星平台；坐标契约与主管线一致（支持跨180拆分、全角数字）。
"""
import csv
import io

from app.domain import merge, split
from app.errors import DomainError
from app.numeric import number

RECHECK_HEADERS = ['scene', 'cloud_w', 'cloud_e']


def parse_recheck(text):
    """解析复查 CSV，返回 (masks, rejected)。

    masks: {canonical_scene: [(start, end), ...]} 已合并的云区区间
    rejected: [{'行号', 'scene', '原因码', '原因'}] 同版异值/格式问题隔离记录
    解析失败（表头/结构）抛 DomainError，与主管线错误风格一致。
    """
    if not isinstance(text, str):
        raise DomainError('TYPE', '复查记录必须是文本')
    text = text.lstrip('﻿')
    if not text.strip():
        raise DomainError('EMPTY', '复查记录为空')
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    try:
        head = next(reader, None)
    except csv.Error:
        raise DomainError('STRUCTURE', '复查表头损坏')
    if head is None:
        raise DomainError('BLANK', '复查文件仅有空行')
    head = [x.strip() for x in head]
    if set(head) != set(RECHECK_HEADERS) or len(head) != len(RECHECK_HEADERS):
        raise DomainError('HEADER', '复查字段须为 scene,cloud_w,cloud_e')

    raw = []
    try:
        for cells in reader:
            if not cells or all(not x.strip() for x in cells):
                continue
            if [x.strip() for x in cells] == head:
                continue
            if len(cells) != len(head):
                raise DomainError('COLUMNS', '复查行列数不符（行 %s）' % reader.line_num)
            row = {**dict(zip(head, [x.strip() for x in cells])), '行号': reader.line_num}
            raw.append(row)
    except csv.Error:
        raise DomainError('STRUCTURE', '复查记录引号未闭合或截断')
    if not raw:
        raise DomainError('HEADER_ONLY', '复查文件没有业务记录')

    masks = {}
    rejected = []
    # 同版异值隔离：同一场景同一版本号下出现不同云区，先收集再裁决
    grouped = {}
    for row in raw:
        scene = row['scene']
        if not scene:
            rejected.append({'行号': row['行号'], 'scene': '',
                             '原因码': 'MISSING', '原因': '复查场景编号为空'})
            continue
        try:
            if bool(row['cloud_w']) != bool(row['cloud_e']):
                raise DomainError('CLOUD', '云区端点缺失')
            if not row['cloud_w']:
                spans = []
            else:
                west = number(row['cloud_w'])
                east = number(row['cloud_e'])
                spans = split(west, east)
        except DomainError as exc:
            rejected.append({'行号': row['行号'], 'scene': scene,
                             '原因码': exc.code, '原因': exc.message})
            continue
        grouped.setdefault(scene, []).append((row['行号'], tuple(spans)))

    for scene, entries in grouped.items():
        fingerprints = {spans for _, spans in entries}
        if len(fingerprints) > 1:
            for line_no, _ in entries:
                rejected.append({'行号': line_no, 'scene': scene,
                                 '原因码': 'RECHECK_CONFLICT', '原因': '同版复查云区取值冲突'})
            continue
        spans = next(iter(fingerprints))
        if spans:
            masks[scene] = merge(list(spans))
        else:
            masks[scene] = []
    return masks, rejected
