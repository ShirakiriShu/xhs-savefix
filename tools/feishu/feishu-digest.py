#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
feishu-digest.py — 每日摘要:统计当日新增 → 写「每日统计表」→ 推飞书群 webhook

- 数据源:线索表(按 首次发现时间 过滤当日)
- 指标:新增/高价值/带联系方式/招聘/营销、Top 关键词、Top 公司、联系方式覆盖率
- webhook 未配置时只写统计表不推群

用法:
  python3 feishu-digest.py [--date 2026-10-07] [--dry-run]
"""
import json, os, subprocess, sys, time, argparse, datetime, collections

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = {}
exec(open(os.path.join(HERE, 'feishu.conf'), encoding='utf-8').read(), CONF)

BT = os.environ.get('FEISHU_BITABLE_APP_TOKEN', CONF.get('FEISHU_BITABLE_APP_TOKEN', ''))
TBL = os.environ.get('FEISHU_LEADS_TABLE_ID', CONF.get('FEISHU_LEADS_TABLE_ID', ''))
DAILY = os.environ.get('FEISHU_DAILY_TABLE_ID', CONF.get('FEISHU_DAILY_TABLE_ID', ''))
PUSHLOG = os.environ.get('FEISHU_PUSHLOG_TABLE_ID', CONF.get('FEISHU_PUSHLOG_TABLE_ID', ''))
WEBHOOK = os.environ.get('FEISHU_WEBHOOK_URL', CONF.get('FEISHU_WEBHOOK_URL', ''))
CHAT_ID = os.environ.get('FEISHU_CHAT_ID', CONF.get('FEISHU_CHAT_ID', ''))
HIGH_VALUE = [x.strip() for x in CONF.get('HIGH_VALUE_POSTER', '').split(',') if x.strip()]


def lark_json(*args):
    r = subprocess.run(['lark-cli'] + list(args) + ['--as', 'user'],
                       capture_output=True, text=True, timeout=120)
    d = json.loads(r.stdout)
    if not d.get('ok'):
        raise RuntimeError(json.dumps(d.get('error'), ensure_ascii=False)[:200])
    return d.get('data') or {}


def cell_text(v):
    """bitable cell → 纯文本(文本字段可能带格式片段)"""
    if isinstance(v, list):
        return ''.join(x.get('text', '') for x in v if isinstance(x, dict)) \
            if v and isinstance(v[0], dict) else ','.join(str(x) for x in v)
    return str(v or '')


def fetch_all_leads():
    r = subprocess.run(['lark-cli', 'base', '+record-list',
                        '--base-token', BT, '--table-id', TBL,
                        '--format', 'json', '--as', 'user'],
                       capture_output=True, text=True, timeout=120)
    d = json.loads(r.stdout)
    if not d.get('ok'):
        raise RuntimeError(json.dumps(d.get('error'), ensure_ascii=False)[:200])
    data = d.get('data') or {}
    names = data.get('fields') or []
    out = []
    for row in data.get('data') or []:
        out.append({'fields': {names[i]: v for i, v in enumerate(row) if v not in (None, '', []) and i < len(names)}})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=datetime.date.today().isoformat())
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    day = datetime.datetime.strptime(args.date, '%Y-%m-%d')
    day_start = int(day.timestamp() * 1000)
    day_end = int((day + datetime.timedelta(days=1)).timestamp() * 1000)

    rows = fetch_all_leads()
    today = []
    for r in rows:
        f = r.get('fields') or {}
        ts = f.get('首次发现时间')
        if isinstance(ts, list):
            ts = ts[0] if ts else None
        # cell 形态:毫秒时间戳 或 ISO 字符串(如 2026-10-06T23:29:37.606+08:00)
        ts_ms = None
        try:
            ts_ms = int(ts)
        except (TypeError, ValueError):
            if isinstance(ts, str) and 'T' in ts:
                try:
                    ts_ms = int(datetime.datetime.fromisoformat(ts).timestamp() * 1000)
                except ValueError:
                    pass
        if ts_ms is None:
            continue
        if day_start <= ts_ms < day_end:
            today.append(f)

    n = len(today)
    high = sum(1 for f in today if cell_text(f.get('发帖人类型')) in HIGH_VALUE)
    contact = sum(1 for f in today
                  if cell_text(f.get('邮箱')) or cell_text(f.get('微信')) or cell_text(f.get('二维码/招聘链接')))
    recruit = sum(1 for f in today if cell_text(f.get('意图类型')) == '招聘')
    marketing = sum(1 for f in today if cell_text(f.get('意图类型')) == '营销')
    cover = round(contact * 100.0 / n, 1) if n else 0.0

    kw_hist = collections.Counter()
    company_hist = collections.Counter()
    for f in today:
        kws = f.get('关键词') or []
        if isinstance(kws, list):
            for k in kws:
                kw_hist[str(k)] += 1
        comp = cell_text(f.get('公司')).strip()
        if comp:
            company_hist[comp] += 1
    top_kw = '、'.join(k for k, _ in kw_hist.most_common(3)) or '-'
    top_co = '、'.join(k for k, _ in company_hist.most_common(3)) or '-'

    stat = {
        '日期': day_start,
        '新增线索数': n, '高价值线索数': high,
        '带联系方式线索数': contact, '联系方式覆盖率': cover,
        '招聘线索数': recruit, '营销线索数': marketing,
        'Top关键词': top_kw, 'Top公司': top_co,
        '推送状态': ['未推送'],
    }
    print(json.dumps(stat, ensure_ascii=False, indent=1))

    if not args.dry_run:
        lark_json('base', '+record-upsert', '--base-token', BT, '--table-id', DAILY,
                  '--json', json.dumps(stat, ensure_ascii=False))
        print('已写每日统计表')

        if CHAT_ID or WEBHOOK:
            lines = [f'【小红书线索日报 {args.date}】',
                     f'今日新增 {n} 条(高价值 {high},带联系方式 {contact},覆盖率 {cover}%)',
                     f'招聘 {recruit} / 营销 {marketing}',
                     f'Top关键词: {top_kw}',
                     f'Top公司: {top_co}', '',
                     '📊 详情看多维表格「小红书线索库」→ 每日统计视图']
            if high or contact:
                lines.append('')
                lines.append('⭐ 高价值线索:')
                for f in today[:5]:
                    if cell_text(f.get('发帖人类型')) in HIGH_VALUE or cell_text(f.get('邮箱')):
                        lines.append(f"• {cell_text(f.get('公司')) or cell_text(f.get('标题'))[:24]} — "
                                     f"{cell_text(f.get('触达路径'))[:40]}")
            ok = False
            text = '\n'.join(lines)
            msg_id = ''
            if CHAT_ID:
                # 优先交互卡片(统计+按钮直达多维表格);失败回退 markdown
                base_url = f"https://v7g82ie6bk.feishu.cn/base/{BT}" if BT.startswith('Ss') or True else ''
                card = {
                  "config": {"wide_screen_mode": True},
                  "header": {"template": "red", "title": {"tag": "plain_text",
                              "content": f"📊 小红书线索日报 · {args.date}"}},
                  "elements": [
                    {"tag": "div", "text": {"tag": "lark_md",
                      "content": f"**今日新增线索 {n} 条**(高价值 {high}) · 带联系方式 {contact}(覆盖 {cover}%)"}},
                    {"tag": "hr"},
                    {"tag": "div", "fields": [
                      {"is_short": True, "text": {"tag": "lark_md",
                        "content": f"**📈 结构**\n招聘 {recruit} · 营销 {marketing}"}},
                      {"is_short": True, "text": {"tag": "lark_md",
                        "content": f"**🔥 Top关键词**\n{top_kw}\n**🏢 Top公司**\n{top_co}"}},
                    ]},
                    {"tag": "hr"},
                    {"tag": "div", "text": {"tag": "lark_md",
                      "content": text.split('\n', 6)[-1] if '\n⭐' in text else '详见评论洞察表'}},
                    {"tag": "hr"},
                    {"tag": "action", "actions": [
                      {"tag": "button", "text": {"tag": "plain_text", "content": "📊 线索库(卡片/看板)"},
                       "type": "primary", "url": f"{base_url}?table={TBL}"},
                      {"tag": "button", "text": {"tag": "plain_text", "content": "💡 评论洞察表"},
                       "url": f"{base_url}?table=tbliB7pTGagBYPoM"},
                      {"tag": "button", "text": {"tag": "plain_text", "content": "📈 每日统计"},
                       "url": f"{base_url}?table={DAILY}"},
                    ]},
                    {"tag": "note", "elements": [{"tag": "plain_text",
                      "content": "由采集 Worker 自动生成"}]},
                  ],
                }
                card_json = json.dumps(card, ensure_ascii=False)
                r = subprocess.run(['lark-cli', 'im', '+messages-send', '--chat-id', CHAT_ID,
                                    '--as', 'bot', '--msg-type', 'interactive', '--content', card_json],
                                   capture_output=True, text=True, timeout=30)
                try:
                    d = json.loads(r.stdout)
                    ok = d.get('ok') is True
                    msg_id = (d.get('data') or {}).get('message_id', '')
                    print('卡片推送:', '成功' if ok else r.stdout[:150], f'(msg_id={msg_id})')
                except Exception as e:
                    print('卡片推送异常:', e)
                if not ok:
                    r = subprocess.run(['lark-cli', 'im', '+messages-send', '--chat-id', CHAT_ID,
                                        '--as', 'bot', '--markdown', text],
                                       capture_output=True, text=True, timeout=30)
                    try:
                        d = json.loads(r.stdout)
                        ok = d.get('ok') is True
                        msg_id = (d.get('data') or {}).get('message_id', '')
                        print('markdown 回退推送:', '成功' if ok else '失败')
                    except Exception as e:
                        print('markdown 回退异常:', e)
            if not ok and WEBHOOK:
                body = {"msg_type": "text", "content": {"text": text}}
                import urllib.request
                req = urllib.request.Request(WEBHOOK, data=json.dumps(body).encode(),
                                             headers={'Content-Type': 'application/json'})
                resp = urllib.request.urlopen(req, timeout=15)
                ok = json.loads(resp.read()).get('code') == 0
                print('webhook 推送:', '成功' if ok else '失败')
            if not ok:
                print('推送失败:chat_id 与 webhook 均未配置或不可用')
            lark_json('base', '+record-upsert', '--base-token', BT, '--table-id', DAILY,
                      '--json', json.dumps({'推送状态': ['成功' if ok else '失败']}, ensure_ascii=False))
            lark_json('base', '+record-upsert', '--base-token', BT, '--table-id', PUSHLOG,
                      '--json', json.dumps({'日期': day_start, '批次': args.date + '-daily',
                                            '线索数量': n, '消息ID': msg_id, '是否成功': ok},
                                           ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
