#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
feishu-sync.py — 把 tools/harvest-out/leads.csv 增量同步进飞书多维表格「线索表」

- 按 note_id 查重(拉已存在 note_id 集合,本地比对)
- 新线索 → 新增记录;已存在 → 更新联系方式/评分/最后更新时间(缺的字段不覆盖)
- 通用性:赛道分类由 feishu.conf 的 TRACK_RULES 关键词映射,换赛道只改配置

用法:
  python3 feishu-sync.py [--csv ../harvest-out/leads.csv] [--dry-run]
前置: lark-cli 已登录(bot 或 user);feishu.conf 里表 ID 正确
"""
import csv, json, os, subprocess, sys, time, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = {}
exec(open(os.path.join(HERE, 'feishu.conf'), encoding='utf-8').read(), CONF)

BT = os.environ.get('FEISHU_BITABLE_APP_TOKEN', CONF.get('FEISHU_BITABLE_APP_TOKEN', ''))
TBL = os.environ.get('FEISHU_LEADS_TABLE_ID', CONF.get('FEISHU_LEADS_TABLE_ID', ''))
TRACK_RULES = json.loads(CONF.get('TRACK_RULES', '{}'))
HIGH_VALUE = [x.strip() for x in CONF.get('HIGH_VALUE_POSTER', '').split(',') if x.strip()]
CONTACTS = [x.strip() for x in CONF.get('CONTACT_SIGNALS', '').split(',') if x.strip()]


def lark(*args):
    """调 lark-cli,返回解析后的 json;失败抛异常"""
    r = subprocess.run(['lark-cli'] + list(args) + ['--as', 'user'],
                       capture_output=True, text=True, timeout=120)
    try:
        d = json.loads(r.stdout)
    except Exception:
        raise RuntimeError(f'lark-cli 输出解析失败: {r.stdout[:200]} / {r.stderr[:200]}')
    if not d.get('ok'):
        raise RuntimeError(f'lark-cli 失败: {json.dumps(d.get("error"), ensure_ascii=False)[:200]}')
    return d.get('data') or {}


def table_records(table_id, fields):
    """拉全表已有记录(json 矩阵布局),返回 [{record_id, fields:{名:值}}]"""
    r = subprocess.run(['lark-cli', 'base', '+record-list',
                        '--base-token', BT, '--table-id', table_id,
                        '--format', 'json', '--as', 'user'],
                       capture_output=True, text=True, timeout=120)
    d = json.loads(r.stdout)
    if not d.get('ok'):
        raise RuntimeError(json.dumps(d.get('error'), ensure_ascii=False)[:200])
    data = d.get('data') or {}
    names = data.get('fields') or []
    rows = []
    for row in data.get('data') or []:
        rec = {'record_id': row[data.get('id_col', 0)] if row else ''}
        rec['fields'] = {names[i]: v for i, v in enumerate(row) if v not in (None, '', []) and i < len(names)}
        rows.append(rec)
    return rows


def guess_track(text):
    t = (text or '').lower()
    best, hits = '其他', 0
    for track, kws in TRACK_RULES.items():
        n = sum(1 for k in kws if k.lower() in t)
        if n > hits:
            best, hits = track, n
    return best if hits else ''


def score(row):
    s = 50
    if row.get('发帖人类型') in HIGH_VALUE:
        s += 30
    for c in CONTACTS:
        if row.get(c):
            s += 8
            break
    if row.get('岗位/需求'):
        s += 5
    return min(s, 100)


def to_record(row, now_ms):
    """leads.csv 行 → 线索表 CellValue"""
    text_all = ' '.join(str(v) for v in row.values())
    track = guess_track(text_all)
    rec = {
        'note_id': row.get('note_id') or row.get('id') or '',
        '标题': row.get('标题') or row.get('title') or '',
        '原文摘要': (row.get('摘要') or row.get('desc') or '')[:500],
        '小红书作者': row.get('作者') or row.get('nickname') or '',
        '小红书号': row.get('小红书号') or row.get('red_id') or '',
        '公司': row.get('公司') or '',
        '岗位/需求': row.get('岗位') or '',
        '发帖人类型': row.get('发帖人类型') or '未知',
        '意图类型': row.get('意图') or ('招聘' if '招' in text_all else '其他'),
        '触达路径': row.get('触达路径') or '',
        '邮箱': row.get('邮箱') or '',
        '微信': row.get('微信') or '',
        '首次发现时间': now_ms,
        '最后更新时间': now_ms,
    }
    # url 类字段:裸链接
    for src, dst in (('主页链接', '作者主页'), ('笔记链接', '笔记链接'), ('二维码', '二维码/招聘链接')):
        v = row.get(src) or ''
        if v.startswith('http'):
            rec[dst] = v
    if track:
        rec['赛道'] = [track]          # select: 数组
    rec['线索评分'] = score(rec)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', default=os.path.join(HERE, '..', 'harvest-out', 'leads.csv'))
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        print(f'leads.csv 不存在: {args.csv}'); return 1
    with open(args.csv, encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    print(f'leads.csv 共 {len(rows)} 行')

    existing = {}
    for r in table_records(TBL, ['note_id']):
        v = (r.get('fields') or {}).get('note_id')
        if isinstance(v, list):
            v = ''.join(x.get('text', '') for x in v if isinstance(x, dict))
        if v:
            existing[v] = r.get('record_id')
    print(f'线上已有 {len(existing)} 条')

    now_ms = int(time.time() * 1000)
    created = updated = skipped = 0
    for row in rows:
        nid = row.get('note_id') or row.get('id') or ''
        if not nid:
            skipped += 1; continue
        rec = to_record(row, now_ms)
        if args.dry_run:
            print('[dry] new' if nid not in existing else '[dry] upd', nid, rec.get('标题', '')[:20])
            continue
        try:
            if nid in existing:
                # 更新:只带变化的字段 + 最后更新时间
                upd = {k: v for k, v in rec.items() if k in
                       ('邮箱', '微信', '线索评分', '最后更新时间', '二维码/招聘链接', '触达路径') and v}
                lark('base', '+record-upsert', '--base-token', BT, '--table-id', TBL,
                     '--record-id', existing[nid], '--json', json.dumps(upd, ensure_ascii=False))
                updated += 1
            else:
                lark('base', '+record-upsert', '--base-token', BT, '--table-id', TBL,
                     '--json', json.dumps(rec, ensure_ascii=False))
                created += 1
            time.sleep(0.6)   # 限流友好
        except Exception as e:
            print(f'  ! {nid}: {e}')
    print(f'完成: 新增 {created} / 更新 {updated} / 跳过 {skipped}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
