#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xhs-leads MCP server (mcp SDK 2.x, MCPServer 高层 API, stdio)

工具(5 个): sync_leads / daily_digest / query_leads / push_feishu / collect_status
接入配置见 README.md
"""
import json
import os
import subprocess
import sys
from datetime import datetime

from mcp.server.mcpserver import MCPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.join(HERE, '..', 'feishu')
sys.path.insert(0, TOOLS_DIR)

CONF = {}
exec(open(os.path.join(TOOLS_DIR, 'feishu.conf'), encoding='utf-8').read(), CONF)
BT = CONF.get('FEISHU_BITABLE_APP_TOKEN', '')
TBL = CONF.get('FEISHU_LEADS_TABLE_ID', '')
WEBHOOK = CONF.get('FEISHU_WEBHOOK_URL', '')

server = MCPServer('xhs-leads', instructions='小红书线索库助手:查询/同步/日报/推群/采集状态')


def _run(cmd, timeout=180):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _cell(v):
    if isinstance(v, list):
        if v and isinstance(v[0], dict):
            return ''.join(x.get('text', '') for x in v if isinstance(x, dict))
        return ','.join(str(x) for x in v)
    return str(v or '')


def fetch_leads():
    r = _run(['lark-cli', 'base', '+record-list', '--base-token', BT,
              '--table-id', TBL, '--format', 'json', '--as', 'user'])
    d = json.loads(r.stdout)
    if not d.get('ok'):
        raise RuntimeError(json.dumps(d.get('error'), ensure_ascii=False)[:200])
    data = d.get('data') or {}
    names = data.get('fields') or []
    out = []
    for row in data.get('data') or []:
        out.append({names[i]: v for i, v in enumerate(row) if v not in (None, '', []) and i < len(names)})
    return out


def _iso_ms(s):
    return int(datetime.fromisoformat(s).timestamp() * 1000)


@server.tool()
def sync_leads() -> str:
    """把手机采集的线索(feishu-sync)增量同步到飞书多维表格「小红书线索库」。用于"同步线索/导入今天采集的数据"。返回新增/更新条数。"""
    r = _run([sys.executable, os.path.join(TOOLS_DIR, 'feishu-sync.py')], timeout=600)
    text = (r.stdout or '')
    return text + (f'\n[exit {r.returncode}] ' + r.stderr[-300:] if r.returncode else '')


@server.tool()
def daily_digest(date: str = '') -> str:
    """生成某天的线索日报:统计新增/高价值/联系方式覆盖率/Top关键词,写入「每日统计表」;配置了 webhook 则推送飞书群。date 为 YYYY-MM-DD,缺省=今天。"""
    cmd = [sys.executable, os.path.join(TOOLS_DIR, 'feishu-digest.py')]
    if date:
        cmd += ['--date', date]
    r = _run(cmd, timeout=600)
    text = (r.stdout or '')
    return text + (f'\n[exit {r.returncode}] ' + r.stderr[-300:] if r.returncode else '')


@server.tool()
def query_leads(track: str = '', intent: str = '', has_contact: bool = False,
                min_score: float = 0.0, since: str = '', keyword: str = '',
                limit: int = 20) -> str:
    """查询飞书线索表。track=赛道(如 AI录音硬件/具身智能/灵巧手/人形机器人);intent=意图类型(招聘/营销/产品讨论/调研);has_contact=true 只看有邮箱/微信/招聘链接的;min_score=最低评分;since=yyyy-mm-dd 之后的;keyword=标题/摘要包含词。用于"今天新增哪些 AI 录音硬件线索""带邮箱的招聘线索有哪些"。"""
    rows = fetch_leads()
    kw = (keyword or '').lower()
    out = []
    for f in rows:
        if track and _cell(f.get('赛道')) != track:
            continue
        if intent and _cell(f.get('意图类型')) != intent:
            continue
        if since:
            v = f.get('首次发现时间')
            t = None
            try:
                t = int(v)
            except (TypeError, ValueError):
                if isinstance(v, str) and 'T' in v:
                    t = _iso_ms(v)
            if t is None or t < _iso_ms(since + 'T00:00:00+08:00'):
                continue
        if min_score and float(_cell(f.get('线索评分')) or 0) < float(min_score):
            continue
        if kw and kw not in (_cell(f.get('标题')) + _cell(f.get('原文摘要'))).lower():
            continue
        if has_contact and not (_cell(f.get('邮箱')) or _cell(f.get('微信')) or _cell(f.get('二维码/招聘链接'))):
            continue
        out.append(f)
    out.sort(key=lambda f: float(_cell(f.get('线索评分')) or 0), reverse=True)
    out = out[:max(1, min(int(limit), 100))]
    if not out:
        return f'没有符合条件的线索(共 {len(rows)} 条在库)。'
    lines = [f'命中 {len(out)} 条(按评分降序):']
    for f in out:
        lines.append(
            f"• [{_cell(f.get('赛道')) or '未分类'}] {_cell(f.get('标题'))[:36]} | "
            f"{_cell(f.get('公司')) or _cell(f.get('小红书作者'))} | "
            f"{_cell(f.get('意图类型'))} 评分{_cell(f.get('线索评分'))} | "
            f"触达:{(_cell(f.get('触达路径')) or _cell(f.get('邮箱')) or '-')[:40]} | "
            f"发现:{_cell(f.get('首次发现时间'))[:10]}")
    return '\n'.join(lines)


@server.tool()
def push_feishu(text: str) -> str:
    """手动推送一段文字到飞书群 webhook。用于"把这段话推到运营群"。"""
    if not WEBHOOK:
        return '未配置 FEISHU_WEBHOOK_URL(见 ../feishu/feishu.conf),无法推群。'
    import urllib.request
    body = {"msg_type": "text", "content": {"text": text}}
    req = urllib.request.Request(WEBHOOK, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    return '推送成功' if resp.get('code') == 0 else f'推送失败: {resp}'


@server.tool()
def collect_status(serial: str = '') -> str:
    """查看手机采集端状态:adb 设备、采集 ndjson 行数/大小、目录开关文件。用于"采集还正常吗/今天有没有新数据"。"""
    base = ['adb'] + (['-s', serial] if serial else [])
    devs = [d for d in _run(base + ['devices']).stdout.strip().splitlines()[1:] if d.strip()]
    if not devs:
        return '没有已连接的 adb 设备。'
    dev = devs[0].split()[0]
    ndjson = '/data/data/com.xingin.xhs/files/xhs-harvest/notes.ndjson'
    out = _run(['adb', '-s', dev, 'shell',
                f'su -c "wc -l {ndjson} 2>/dev/null; du -h {ndjson} 2>/dev/null; '
                f'ls /data/data/com.xingin.xhs/files/xhs-harvest/ 2>/dev/null"'],
               timeout=30).stdout
    return f'设备: {dev}\n' + (out.strip() or '(采集目录为空或不可读)')


async def main():
    await server.run_stdio_async()


if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
