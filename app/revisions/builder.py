"""修正版本派生管线。

从父批次的不可变快照出发：M1/M2 的接收与坐标裁决保持不变，
仅以云区复查记录替换 M3 的场景遮罩后重算 M3-M7，
同时记录新旧覆盖/缺口/采购差额，供页面与证据导出核对。
"""
from collections import Counter
from copy import deepcopy
from decimal import Decimal

from app.domain import length, merge, subtract, TARGET
from app.modules.m3_cloud.rules import apply as m3_apply
from app.modules.m4_union.rules import apply as m4_apply
from app.modules.m5_gaps.rules import apply as m5_apply
from app.modules.m6_purchase.rules import apply as m6_apply
from app.modules.m7_delivery.rules import apply as m7_apply
from app.numeric import serial
from app.revisions.mask import parse_recheck


def _hydrate_scene(row):
    """把持久化场景（serial 后的字符串）还原为规则层需要的结构。"""
    return {
        **row,
        'spans': [(Decimal(a), Decimal(z)) for a, z in row['spans']],
        'cloud': [(Decimal(a), Decimal(z)) for a, z in row['cloud']],
        'price_n': Decimal(row['price_n']),
        'version_n': int(row['version_n']),
    }


def _metric_map(stage):
    return {name: value for name, value, _unit in stage['metrics']}


def build_revision(parent, recheck_text):
    """基于父批次快照与复查记录文本，构造修正版本（未持久化）。

    返回 dict：revision 快照、recheck 统计与 diff。
    父批次内容不会被修改。
    """
    masks, rejected = parse_recheck(recheck_text)

    child = deepcopy(parent)
    # M2 已裁决的有效场景（持久化快照）是派生起点；复查只改遮罩，不重新裁决坐标/版本
    scenes = [_hydrate_scene(row) for row in parent['scenes']]
    child['scenes'] = scenes
    # 父批次隔离区是已签核证据，原样保留；规则重算不重复累计历史隔离
    original_quarantine = list(parent.get('quarantine', []))
    child['quarantine'] = []
    child['usable'] = []

    known = {scene['canonical'] for scene in scenes}
    unknown = sorted(set(masks) - known)
    applied = sorted(set(masks) & known)
    for scene in unknown:
        rejected.append({'行号': None, 'scene': scene,
                         '原因码': 'RECHECK_UNKNOWN', '原因': '复查场景不在父批次有效场景中'})

    # M3：用复查遮罩覆盖场景自身云区，缺省复查的场景沿用原遮罩
    original_cloud = {scene['canonical']: list(scene['cloud']) for scene in scenes}
    for scene in scenes:
        if scene['canonical'] in masks:
            scene['cloud'] = masks[scene['canonical']]

    m3 = m3_apply(child)
    m3['stage'] = 3
    m3['name'] = '云区剔除'
    m3['issues'] = dict(Counter(row['原因码'] for row in original_quarantine))
    m3['new_quarantine'] = 0

    # 每个被复查场景的新旧有效宽度对照
    mask_rows = []
    usable_by_name = {row['canonical']: row for row in child['usable']}
    old_usable_by_name = {row['场景']: row for row in parent['stages']['3']['rows']}
    for name in applied:
        new_width = str(length(usable_by_name[name]['clear'])) if name in usable_by_name else '0'
        old_width = old_usable_by_name[name]['有效宽度_度'] if name in old_usable_by_name else '0'
        mask_rows.append({
            '场景': name,
            '原云区段数': len(original_cloud[name]),
            '复查云区段数': len(masks[name]),
            '原有效宽度_度': old_width,
            '新有效宽度_度': new_width,
            '有效宽度差额_度': str(Decimal(new_width) - Decimal(old_width)),
        })
    child['stages']['3'] = serial(m3)
    # 恢复已签核隔离证据；复查自身的隔离挂在 manifest.recheck.rejected
    child['quarantine'] = original_quarantine

    for stage_no, rule in ((4, m4_apply), (5, m5_apply), (6, m6_apply), (7, m7_apply)):
        result = rule(child)
        result['stage'] = stage_no
        result['name'] = {4: '覆盖并集', 5: '缺口定位', 6: '采购候选', 7: '交付核验'}[stage_no]
        result['issues'] = dict(Counter(row['原因码'] for row in original_quarantine))
        result['new_quarantine'] = 0
        child['stages'][str(stage_no)] = serial(result)

    diff = _diff(parent, child)
    child['manifest']['recheck'] = {
        'parent_run_id': parent['manifest']['run_id'],
        'parent_revision': parent['revision'],
        'applied_scenes': applied,
        'unknown_scenes': unknown,
        'rejected': rejected,
        'mask_rows': mask_rows,
        'diff': diff,
    }
    return serial(child)


def _diff(parent, child):
    """新旧覆盖、缺口、采购的确定性差额。"""
    old_cov = [(Decimal(a), Decimal(z)) for a, z in _coverage_intervals(parent)]
    new_cov = [(Decimal(a), Decimal(z)) for a, z in _coverage_intervals(child)]
    old_gap = length(subtract(TARGET, old_cov))
    new_gap = length(subtract(TARGET, new_cov))
    old_cost = Decimal(_metric_map(parent['stages']['6'])['采购额'])
    new_cost = Decimal(_metric_map(child['stages']['6'])['采购额'])
    old_pick = [row['场景'] for row in parent['stages']['6']['rows']]
    new_pick = [row['场景'] for row in child['stages']['6']['rows']]
    added = merge(subtract(new_cov, old_cov))
    removed = merge(subtract(old_cov, new_cov))
    return {
        '覆盖宽度_旧_度': str(length(old_cov)),
        '覆盖宽度_新_度': str(length(new_cov)),
        '覆盖差额_度': str(length(new_cov) - length(old_cov)),
        '新增覆盖段': [{'起始经度': str(a), '结束经度': str(z)} for a, z in added],
        '失去覆盖段': [{'起始经度': str(a), '结束经度': str(z)} for a, z in removed],
        '缺口宽度_旧_度': str(old_gap),
        '缺口宽度_新_度': str(new_gap),
        '采购额_旧_CNY': str(old_cost),
        '采购额_新_CNY': str(new_cost),
        '采购额差额_CNY': str(new_cost - old_cost),
        '采购场景_旧': old_pick,
        '采购场景_新': new_pick,
        '新增采购场景': sorted(set(new_pick) - set(old_pick)),
        '退出采购场景': sorted(set(old_pick) - set(new_pick)),
    }


def _coverage_intervals(snapshot):
    return [(row['起始经度'], row['结束经度']) for row in snapshot['stages']['4']['rows']]
