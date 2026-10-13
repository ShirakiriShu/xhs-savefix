#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
harvest-insight.py — 用本地 Ollama(qwen) 对采集语料做深度评论提炼

流程:
  1. 从 ndjson 语料抽目标帖的评论(默认: 相关度筛选的全部已采帖)
  2. 分批(每批 30 条)喂给 qwen:逐条打标(痛点/需求/竞品口碑/其他)+ 提炼要点
  3. 汇总轮:把各批结果归纳成洞察主题(去重合并),每主题给 产品建议+优先级
  4. 写入飞书「评论洞察表」(upsert by 洞察ID)

用法:
  python3 harvest-insight.py                    # 全部已采相关帖
  python3 harvest-insight.py --note 696b46e4    # 指定帖
  python3 harvest-insight.py --dry-run          # 只提炼不写飞书
  python3 harvest-insight.py --model qwen3.8:27b-mlx --batch 30

前置: ollama serve 且已 pull 模型;语料 ndjson 存在(harvest-deep.sh 产出)
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import argparse
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = {}
exec(open(os.path.join(HERE, 'feishu', 'feishu.conf'), encoding='utf-8').read(), CONF)
BT = CONF.get('FEISHU_BITABLE_APP_TOKEN', '')
INSIGHT_TBL = 'tbliB7pTGagBYPoM'   # 评论洞察表
MODEL = 'qwen3.8:27b-mlx'
OLLAMA = 'http://localhost:11434'


def ollama(prompt, timeout=900, force_json=True):
    body = {"model": MODEL, "prompt": prompt, "stream": False,
            "options": {"temperature": 0.2, "num_predict": 4000}}
    if force_json:
        body["format"] = "json"
    req = urllib.request.Request(OLLAMA + '/api/generate',
                                 data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    t0 = time.time()
    resp = urllib.request.urlopen(req, timeout=timeout)
    d = json.loads(resp.read())
    print(f'    [ollama {round(time.time()-t0,1)}s, {d.get("eval_count",0)} tokens]')
    return d.get('response', '')


def load_comments(paths, note_filter=None):
    """从 ndjson 抽评论,按帖分组"""
    groups = defaultdict(dict)   # nid -> {cid: {user,content,likes,ip}}
    titles = {}
    for path in paths:
        for line in open(path, encoding='utf-8', errors='ignore'):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            u = rec.get('url', '')
            try:
                b = json.loads(rec['body'])
            except Exception:
                continue
            d = b.get('data')
            if not isinstance(d, dict):
                continue
            q = up = None
            import urllib.parse as up
            q = up.parse_qs(up.urlparse(u).query)
            nid = q.get('note_id', ['?'])[0]
            if note_filter and not any(nid.startswith(p) for p in note_filter):
                if not note_filter:
                    # 无过滤时按相关词粗筛
                    pass
            if '/note/comment/sub_comments' in u or 'comment/list?' in u:
                if note_filter and not any(nid.startswith(p) for p in note_filter):
                    continue
                for c in (d.get('comments') or []):
                    if not isinstance(c, dict):
                        continue
                    cu = c.get('user') or {}
                    groups[nid][c['id']] = {
                        'user': cu.get('nickname', ''), 'content': c.get('content', ''),
                        'likes': c.get('like_count', 0), 'ip': c.get('ip_location', '')}
                    for s in (c.get('sub_comments') or []):
                        su = s.get('user') or {}
                        groups[nid][s['id']] = {
                            'user': su.get('nickname', ''), 'content': s.get('content', ''),
                            'likes': s.get('like_count', 0), 'ip': s.get('ip_location', '')}
            elif ('/v10/note' in u or 'imagefeed' in u) and 'comment' not in u:
                blocks = d if isinstance(d, list) else [d]
                for blk in blocks:
                    if not isinstance(blk, dict):
                        continue
                    for n in (blk.get('note_list') or blk.get('items') or
                              ([blk['note']] if isinstance(blk.get('note'), dict) else [])):
                        if isinstance(n, dict):
                            nid2 = n.get('id') or n.get('note_id')
                            if nid2:
                                titles[nid2] = (n.get('title') or n.get('displayTitle') or '')[:50]
    return groups, titles


# prompt 从 local-llm/prompts/ 读(模型可自迭代;文件缺失时用内置兜底)
def _load_prompt(name, fallback):
    path = os.path.join(HERE, 'local-llm', 'prompts', name)
    try:
        txt = open(path, encoding='utf-8').read()
        block = txt.split('## 模板', 1)[1].split('```', 2)[1].strip('\n')
        return block
    except Exception:
        return fallback

BATCH_PROMPT = _load_prompt('batch_classify.md',
    """你是产品调研分析师。以下是"AI录音硬件"(对标Plaud)相关的小红书评论。
提炼有信息量的内容,每行一条,格式:[类别|主题] 要点(保留原话) —— 用户(👍赞)
类别:痛点/需求/竞品口碑/其他。只输出清单,跳过无信息量评论。最多600 tokens。

""")


def batch_classify(comments):
    """分批自由文本归纳,返回文本块列表"""
    blocks = []
    items = list(comments)
    B = 12
    for i in range(0, len(items), B):
        chunk = items[i:i + B]
        listing = '\n'.join(
            f"{j}. ({c['user']}|👍{c['likes']}): {c['content'][:150]}"
            for j, c in enumerate(chunk))
        prompt = BATCH_PROMPT + listing
        print(f'  批次 {i//B+1}/{(len(items)+B-1)//B}: {len(chunk)} 条')
        try:
            raw = ollama(prompt, force_json=False)
            blocks.append(raw)
            print(f'    归纳 {len(raw.splitlines())} 行')
        except Exception as e:
            print(f'    批次失败: {e}')
    return blocks


MERGE_PROMPT = _load_prompt('merge_insights.md',
    """你是产品调研负责人。归纳以下评论清单为去重洞察主题,输出严格JSON数组:
[{"topic":"","type":"痛点|需求|竞品口碑","count":0,"heat":0,"typical":"原文≤80字","who":"","advice":"≤60字","priority":"P0|P1|P2"}]
typical 必须原文摘录。清单:
""")


def merge_insights(blocks):
    prompt = MERGE_PROMPT + '\n\n'.join(blocks)
    raw = ollama(prompt, timeout=900, force_json=True)
    try:
        arr = json.loads(raw)
        return arr if isinstance(arr, list) else []
    except Exception as e:
        print(f'汇总解析失败: {e}; 原始前200字: {raw[:200]}')
        return []


def push_feishu(insights, dry=False):
    ok = 0
    for i, ins in enumerate(insights):
        rec = {
            '洞察ID': f"AI-{i+1:02d}",
            '类型': [ins.get('type', '需求')],
            '主题': ins.get('topic', ''),
            '评论条数': ins.get('count', 0),
            '热度': ins.get('heat', 0),
            '典型评论': ins.get('typical', ''),
            '典型评论人': ins.get('who', ''),
            '产品建议': ins.get('advice', ''),
            '优先级': [ins.get('priority', 'P2')],
        }
        if dry:
            print('  [dry]', rec['洞察ID'], rec['类型'], rec['主题'], f"(证据: {rec['典型评论'][:30]}...)")
            ok += 1
            continue
        r = subprocess.run(['lark-cli', 'base', '+record-upsert', '--base-token', BT,
                            '--table-id', INSIGHT_TBL, '--json', json.dumps(rec, ensure_ascii=False),
                            '--as', 'user'], capture_output=True, text=True)
        try:
            if json.loads(r.stdout).get('ok'):
                ok += 1
            else:
                print('  写入失败:', r.stdout[:120])
        except Exception as e:
            print('  写入异常:', e)
        time.sleep(0.5)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--note', action='append', default=[], help='目标帖 id 前缀,可多次')
    ap.add_argument('--ndjson', action='append', default=[])
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    paths = args.ndjson or [os.path.join(HERE, 'harvest-out', 'deep.ndjson')]
    if not any(os.path.exists(p) for p in paths):
        paths = ['/tmp/batch_exact.ndjson', '/tmp/final5.ndjson']
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        print('没有可用语料文件'); return 1
    filt = set(args.note) if args.note else None
    groups, titles = load_comments(paths, filt)
    print(f'帖子 {len(groups)} 个,语料文件 {len(paths)} 个')

    # 只保留评论≥5的帖
    groups = {k: v for k, v in groups.items() if len(v) >= 5}
    all_comments = []
    for nid, cs in sorted(groups.items(), key=lambda x: -len(x[1])):
        print(f"  帖 {nid[:16]} ({titles.get(nid,'?')[:20]}) 评论 {len(cs)}")
        all_comments.extend(cs.values())
    print(f'评论总数: {len(all_comments)}')
    if not all_comments:
        print('没有评论可提炼'); return 1

    print('== 1/2 分批归纳 ==')
    blocks = batch_classify(all_comments)
    print(f'归纳完成: {len(blocks)} 个文本块')

    print('== 2/2 归纳洞察主题(结构化) ==')
    insights = merge_insights(blocks)
    print(f'洞察主题: {len(insights)}')
    for i, ins in enumerate(insights):
        print(f"  [{ins.get('priority')}] {ins.get('type')} | {ins.get('topic')} "
              f"(评{ins.get('count')},👍{ins.get('heat')}) — {ins.get('advice','')[:40]}")

    if args.dry_run:
        print('(dry-run,不写飞书)')
        json.dump(insights, open('/tmp/qwen_insights.json', 'w'), ensure_ascii=False, indent=1)
        return 0
    ok = push_feishu(insights)
    print(f'写入飞书评论洞察表: {ok}/{len(insights)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
