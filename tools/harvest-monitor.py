#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
harvest-monitor.py — AI录音设备小红书增量监测日报(全本地,零 ZCode token)

每天:
  1. 手机拟人搜索监测关键词(deeplink),模块被动采搜索结果
  2. 增量判断:
     - 新帖(不在 state) → 本地 qwen 打相关分,取 Top N
     - 老帖:官方评论数比上次记录涨 ≥ MONITOR_NEW_COMMENT_MIN → 重看
  3. 深采目标帖(复用人像节奏;模块自动展开楼中楼)
  4. 本地 qwen 对新帖热评做洞察归纳(local-llm/prompts 模板)
  5. 新帖写飞书线索表;交互卡片推 1616 群

用法:
  python3 harvest-monitor.py                     # 全流程
  python3 harvest-monitor.py --no-capture        # 跳过深采(快速/调试)
  python3 harvest-monitor.py --dry-run           # 不写飞书不推群
  python3 harvest-monitor.py --keywords "AI录音,plaud"

状态: harvest-out/monitor_state.json(笔记 id → 上次评论数/日期)
依赖: 模块采集(在跑) / litellm 网关(local-llm/start-gateway.sh) / lark-cli(user+bot)
"""
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import argparse
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = {}
exec(open(os.path.join(HERE, 'feishu', 'feishu.conf'), encoding='utf-8').read(), CONF)

BT = CONF.get('FEISHU_BITABLE_APP_TOKEN', '')
LEADS = CONF.get('FEISHU_LEADS_TABLE_ID', '')
CHAT_ID = CONF.get('FEISHU_CHAT_ID', '')
LLM_BASE = os.environ.get('XHS_LLM_BASE', 'http://127.0.0.1:4000')
LLM_KEY = os.environ.get('XHS_LLM_KEY', 'sk-local-claude-code-gateway')
LLM_MODEL = os.environ.get('XHS_LLM_MODEL', 'glm-5.1')

KW = [k.strip() for k in (CONF.get('MONITOR_KEYWORDS') or 'AI录音,录音笔,plaud,录音转写').split(',') if k.strip()]
TOP_N = int(CONF.get('MONITOR_TOP_N') or 5)
NEW_CMT_MIN = int(CONF.get('MONITOR_NEW_COMMENT_MIN') or 15)
CAPTURE_ROUNDS = int(CONF.get('MONITOR_CAPTURE_ROUNDS') or 4)
NDJSON_PHONE = '/data/data/com.xingin.xhs/files/xhs-harvest/notes.ndjson'
STATE_PATH = os.path.join(HERE, 'harvest-out', 'monitor_state.json')


def sh(cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def adb(*args, timeout=30):
    return sh(['adb'] + list(args), timeout)


def sleep_human(lo, hi):
    time.sleep(round(random.uniform(lo, hi), 1))


# ---------- 1. 手机搜索 ----------

def drive_search(serial, keyword):
    url = 'xhsdiscover://search/result?keyword=' + urllib.parse.quote(keyword)
    adb('-s', serial, 'shell', 'am', 'start', '-a', 'android.intent.action.VIEW',
        '-d', url, 'com.xingin.xhs')
    time.sleep(8)
    for _ in range(3):
        adb('-s', serial, 'shell', 'input', 'swipe', '620', '2250', '620', '1100',
            str(random.randint(450, 750)))
        sleep_human(3.5, 7)


# ---------- 2. 语料拉取与解析 ----------

def pull_tail(serial, lines=30000):
    out = os.path.join(HERE, 'harvest-out', 'monitor-today.ndjson')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    total = adb('-s', serial, 'shell', 'su', '-c',
                f'wc -l < {NDJSON_PHONE}').stdout.strip() or '0'
    total = int(total) if total.isdigit() else 0
    skip = max(0, total - lines)
    r = adb('-s', serial, 'shell', 'su', '-c',
            f'tail -n +{skip + 1} {NDJSON_PHONE}', timeout=120)
    open(out, 'w', encoding='utf-8').write(r.stdout)
    return out


def parse_search_notes(path, since_ms=0):
    notes = {}
    for line in open(path, encoding='utf-8', errors='ignore'):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if '/search/notes' not in rec.get('url', ''):
            continue
        try:
            if since_ms and int(rec.get('ts', 0)) < since_ms:
                continue   # 只统计本次运行新落盘的
        except (TypeError, ValueError):
            continue
        try:
            b = json.loads(rec['body'])
        except Exception:
            continue
        d = b.get('data')
        items = d.get('items') if isinstance(d, dict) else (d if isinstance(d, list) else [])
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            n = it.get('note') or {}
            nid = n.get('id')
            if not nid:
                continue
            u = n.get('user') or {}
            notes[nid] = {
                'title': (n.get('title') or n.get('displayTitle') or '')[:60],
                'nick': (u.get('nickname') or '')[:20],
                'likes': n.get('liked_count') or 0,
                'ccount': n.get('comments_count') or 0,
                'token': n.get('xsec_token') or '',
            }
    return notes


def parse_comments(path, nids, since_ms=0):
    """抽目标帖评论(主列表+子页),返回 {nid: [comment...]}"""
    want = set(nids)
    got = {n: {} for n in want}
    for line in open(path, encoding='utf-8', errors='ignore'):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        u = rec.get('url', '')
        if 'comment' not in u:
            continue
        try:
            if since_ms and int(rec.get('ts', 0)) < since_ms:
                continue
        except (TypeError, ValueError):
            continue
        try:
            b = json.loads(rec['body'])
        except Exception:
            continue
        d = b.get('data')
        if not isinstance(d, dict):
            continue
        q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
        nid = q.get('note_id', ['?'])[0]
        if nid not in want:
            continue
        for c in (d.get('comments') or []):
            if not isinstance(c, dict):
                continue
            cu = c.get('user') or {}
            got[nid][c['id']] = {'user': cu.get('nickname', ''), 'ip': c.get('ip_location', ''),
                                 'likes': c.get('like_count', 0), 'content': c.get('content', '')}
            for s in (c.get('sub_comments') or []):
                su = s.get('user') or {}
                got[nid][s['id']] = {'user': su.get('nickname', ''), 'ip': s.get('ip_location', ''),
                                     'likes': s.get('like_count', 0), 'content': s.get('content', '')}
    return {n: sorted(v.values(), key=lambda c: -c['likes']) for n, v in got.items()}


# ---------- 3. 本地 LLM(经 litellm 网关,anthropic 协议) ----------

def llm(prompt, system='你是产品调研分析师,服务对象是AI录音硬件出海团队(对标Plaud)。', max_tokens=1500, force_json=False):
    """直连 ollama(qwen3.8):思考模型必须 think:false,否则单次推理 5-10 分钟且会把网关队列堵死"""
    body = {'model': 'qwen3.8:27b-mlx', 'prompt': system + '\n\n' + prompt,
            'stream': False, 'think': False,
            'options': {'temperature': 0.2, 'num_predict': max_tokens}}
    if force_json:
        body['format'] = 'json'
    t0 = time.time()
    try:
        req = urllib.request.Request('http://127.0.0.1:11434/api/generate',
                                     data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
        d = json.loads(urllib.request.urlopen(req, timeout=600).read())
    except urllib.error.HTTPError:
        # 老版本 ollama 不认 think 参数 → 去掉重试
        body.pop('think', None)
        req = urllib.request.Request('http://127.0.0.1:11434/api/generate',
                                     data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
        d = json.loads(urllib.request.urlopen(req, timeout=600).read())
    print(f'    [ollama {round(time.time()-t0,1)}s]')
    return d.get('response', '')


def llm_json(prompt, **kw):
    kw.setdefault('force_json', True)
    raw = llm(prompt, **kw)
    raw = re.sub(r'^```(json)?|```$', '', raw.strip(), flags=re.M).strip()
    i, j = raw.find('['), raw.rfind(']')
    if i >= 0 and j > i:
        raw = raw[i:j + 1]
    try:
        arr = json.loads(raw)
    except json.JSONDecodeError:
        try:
            arr = json.loads(raw[i:raw.rfind('}') + 1] + ']')
        except Exception:
            return []
    if isinstance(arr, dict):
        # 单对象(非索引map)→ 包一层;索引map → 取 values
        arr = [arr] if ('score' in arr or 'i' in arr) else list(arr.values())
    return [x for x in arr if isinstance(x, dict)]


CLASSIFY_PROMPT = """/no_think
以下是小红书搜索结果里"AI录音硬件/录音笔/会议转写"相关候选笔记。
逐条评估与"AI录音硬件产品调研"(对标Plaud,关注:用户需求/竞品口碑/痛点/市场动向)的相关度。

输出JSON数组,每条:
{"i":序号,"score":相关度0-100,"type":"需求讨论|竞品口碑|痛点吐槽|市场动向|招聘合作|无关","summary":"一句话摘要(≤40字,含关键数字/品牌)"}

要求:必须为每一条候选输出一项,不能遗漏。
打分参考:直接讨论AI录音硬件产品/竞品(Plaud/录音豆/讯飞等)=80+;
泛AI硬件讨论=50-79;招聘/合作=40-60;其他=低于40。
只输出JSON。

候选笔记:
{listing}"""


def classify_notes(cands):
    listing = '\n'.join(
        f"{i}. [{c['nick']}|👍{c['likes']}|评{c['ccount']}] {c['title'] or '(无标题)'}"
        for i, c in enumerate(cands[:10]))
    try:
        arr = llm_json(CLASSIFY_PROMPT.replace('{listing}', listing))
    except Exception as e:
        print('    分类失败:', e)
        return []
    out = []
    for o in arr:
        try:
            i = int(o.get('i', -1))
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(cands):
            c = dict(cands[i])
            c['score'] = int(o.get('score', 0) or 0)
            c['type'] = o.get('type', '未知')
            c['summary'] = o.get('summary', '')
            out.append(c)
    return out


def load_prompt_md(name):
    try:
        txt = open(os.path.join(HERE, 'local-llm', 'prompts', name), encoding='utf-8').read()
        return txt.split('## 模板', 1)[1].split('```', 2)[1].strip('\n')
    except Exception:
        return ''


def summarize_hot_comments(comments, cap=12):
    if not comments:
        return []
    tpl = load_prompt_md('batch_classify.md') or (
        '提炼有信息量的评论,每行一条:[类别|主题] 要点 —— 用户(👍赞)。类别:痛点/需求/竞品口碑/其他。只输出清单。\n')
    listing = '\n'.join(
        f"{j}. ({c['user']}|{c['ip']}|👍{c['likes']}): {c['content'][:150]}"
        for j, c in enumerate(comments[:cap]))
    try:
        raw = llm('/no_think\n' + tpl + '\n评论列表:\n' + listing, max_tokens=800)
    except Exception as e:
        print('    热评归纳失败:', e)
        return []
    return [l for l in raw.splitlines() if l.strip().startswith('[')]


# ---------- 4. 状态 ----------

def load_state():
    if os.path.exists(STATE_PATH):
        try:
            return json.load(open(STATE_PATH, encoding='utf-8'))
        except Exception:
            pass
    return {'notes': {}, 'last_run': ''}


def save_state(st):
    st['last_run'] = date.today().isoformat()
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    json.dump(st, open(STATE_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


def feishu_known_note_ids():
    r = sh(['lark-cli', 'base', '+record-list', '--base-token', BT, '--table-id', LEADS,
            '--format', 'json', '--as', 'user'], timeout=120)
    ids = set()
    try:
        d = json.loads(r.stdout)
        data = d.get('data') or {}
        names = data.get('fields') or []
        if 'note_id' in names:
            i = names.index('note_id')
            for row in data.get('data') or []:
                if row and row[i]:
                    ids.add(str(row[i]))
    except Exception:
        pass
    return ids


# ---------- 5. 深采(精简版,复用 harvest-deep 语义) ----------

def deep_capture(serial, nid, token, rounds=None):
    rounds = rounds or CAPTURE_ROUNDS
    if not token:
        print(f'    无 token,跳过 {nid[:12]}')
        return False
    adb('-s', serial, 'shell', 'am', 'start', '-a', 'android.intent.action.VIEW',
        '-d', f'xhsdiscover://item/{nid}?xsec_token={token}&xsec_source=search_note',
        'com.xingin.xhs')
    time.sleep(9)
    for _ in range(rounds):
        for _ in range(2):
            adb('-s', serial, 'shell', 'input', 'swipe', '620', '2250', '620', '1000',
                str(random.randint(450, 700)))
            sleep_human(2, 3.5)
        sleep_human(12, 18)
    return True


# ---------- 6. 飞书 ----------

def upsert_lead(note, intent, score):
    now = int(time.time() * 1000)
    rec = {
        'note_id': note['nid'], '标题': note['title'], '原文摘要': note['summary'],
        '小红书作者': note['nick'], '赛道': ['AI录音硬件'],
        '意图类型': [intent if intent in ('招聘', '营销', '产品讨论', '调研', '其他') else '产品讨论'],
        '线索评分': score, '首次发现时间': now, '最后更新时间': now, '跟进状态': ['待跟进'],
    }
    r = sh(['lark-cli', 'base', '+record-upsert', '--base-token', BT, '--table-id', LEADS,
            '--json', json.dumps(rec, ensure_ascii=False), '--as', 'user'], timeout=60)
    try:
        return json.loads(r.stdout).get('ok')
    except Exception:
        return False


def push_card(title_lines, hot_lines, n_new, n_re, cmt_total, dry=False):
    if not CHAT_ID:
        print('未配置 FEISHU_CHAT_ID,跳过推送')
        return
    base_url = f'https://v7g82ie6bk.feishu.cn/base/{BT}'
    if not title_lines:
        title_lines = ['今日监测关键词无新增相关帖子(老帖评论增量也均低于门槛)']
    new_md = '\n'.join(title_lines[:8]) or '—'
    hot_md = '\n'.join(hot_lines[:10]) or ('—(未深采)' if not hot_lines else '—')
    card = {
        'config': {'wide_screen_mode': True},
        'header': {'template': 'green',
                   'title': {'tag': 'plain_text',
                             'content': f'🎧 AI录音设备监测日报 · {date.today().isoformat()}'}},
        'elements': [
            {'tag': 'div', 'text': {'tag': 'lark_md',
             'content': f'**新帖 {n_new} 篇**(已入线索表) · 重看 {n_re} 篇(评论有增量) · 新增评论语料 {cmt_total} 条'}},
            {'tag': 'hr'},
            {'tag': 'div', 'text': {'tag': 'lark_md',
             'content': f'**🆕 今日新帖**\n{new_md}'}},
            {'tag': 'hr'},
            {'tag': 'div', 'text': {'tag': 'lark_md',
             'content': f'**🔥 热评信号(本地 qwen 提炼)**\n{hot_md}'}},
            {'tag': 'hr'},
            {'tag': 'action', 'actions': [
                {'tag': 'button', 'text': {'tag': 'plain_text', 'content': '📊 线索库'},
                 'type': 'primary', 'url': f'{base_url}?table={LEADS}'},
                {'tag': 'button', 'text': {'tag': 'plain_text', 'content': '💡 评论洞察表'},
                 'url': f'{base_url}?table=tbliB7pTGagBYPoM'},
            ]},
            {'tag': 'note', 'elements': [{'tag': 'plain_text',
             'content': 'harvest-monitor.py 自动生成 · 本地 qwen3.8:27b 提炼'}]},
        ],
    }
    if dry:
        print('[dry] 卡片预览: 新帖', n_new, '重看', n_re)
        return
    r = sh(['lark-cli', 'im', '+messages-send', '--chat-id', CHAT_ID, '--as', 'bot',
            '--msg-type', 'interactive', '--content', json.dumps(card, ensure_ascii=False)],
           timeout=60)
    try:
        d = json.loads(r.stdout)
        print('卡片推送:', '成功' if d.get('ok') else r.stdout[:150],
              f"msg_id={(d.get('data') or {}).get('message_id','')}")
    except Exception as e:
        print('卡片推送异常:', e)


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--no-capture', action='store_true')
    ap.add_argument('--keywords', default='')
    args = ap.parse_args()
    kws = [k.strip() for k in args.keywords.split(',') if k.strip()] or KW

    # 设备
    devs = [l.split()[0] for l in adb('devices').stdout.splitlines()[1:] if 'device' in l]
    if not devs:
        print('没有 adb 设备'); return 1
    serial = devs[0]
    print(f'设备 {serial} | 关键词 {kws}')

    t_start_ms = int(time.time() * 1000)
    print('== 1/6 手机搜索 ==')
    for k in kws:
        print(f'  搜索: {k}')
        drive_search(serial, k)
        sleep_human(2, 4)

    print('== 2/6 拉取语料 ==')
    corpus = pull_tail(serial)

    print('== 3/6 增量判断 ==')
    notes = parse_search_notes(corpus, t_start_ms)
    print(f'  搜索到笔记 {len(notes)} 条')
    st = load_state()
    known_feishu = feishu_known_note_ids()
    fresh, recheck = [], []
    for nid, n in notes.items():
        prev = st['notes'].get(nid)
        if prev is None:
            if nid not in known_feishu:
                fresh.append({**n, 'nid': nid})
        else:
            delta = (n['ccount'] or 0) - (prev.get('ccount') or 0)
            if delta >= NEW_CMT_MIN and prev.get('last_capture', '') != date.today().isoformat():
                recheck.append({**n, 'nid': nid, 'delta': delta})
    # 关键词预筛:likes 排序会把无关热帖排前,先按标题含监测词过滤再给 LLM
    kwpat = '|'.join(re.escape(k) for k in kws)
    matched = [n for n in fresh if re.search(kwpat, n['title'] or '', re.I)]
    if matched:
        fresh = matched
    fresh.sort(key=lambda x: -(x['likes'] or 0))
    recheck.sort(key=lambda x: -x.get('delta', 0))
    print(f'  全新 {len(fresh)} 篇 · 老帖评论增量≥{NEW_CMT_MIN} 的 {len(recheck)} 篇')

    print('== 4/6 本地 qwen 分类筛选 ==')
    scored = classify_notes(fresh) if fresh else []
    picked = [c for c in scored if c.get('score', 0) >= 40][:TOP_N]
    for c in picked:
        print(f"  [{c['score']}|{c['type']}] {c['title'][:30]} — {c['summary'][:40]}")

    targets = [c['nid'] for c in picked] + [c['nid'] for c in recheck[:2]]
    print('== 5/6 深采 ==')
    if args.no_capture:
        print('  (--no-capture 跳过)')
    else:
        for c in picked + recheck[:2]:
            print(f"  深采 {c['nid'][:14]} [{c['title'][:24]}]")
            deep_capture(serial, c['nid'], c.get('token', ''))
            sleep_human(3, 6)
        corpus = pull_tail(serial)   # 深采后再拉一次

    print('== 6/6 热评提炼 + 推送 ==')
    cmts = parse_comments(corpus, targets, t_start_ms) if targets else {}
    cmt_total = sum(len(v) for v in cmts.values())
    top_comments = []
    for nid, cs in cmts.items():
        top_comments.extend(cs[:6])
    top_comments.sort(key=lambda c: -c['likes'])
    hot_lines = summarize_hot_comments(top_comments) if top_comments else []

    title_lines = [f"• [{c.get('score','?')}|{c.get('type','')}] {c['title'][:30]} — {c.get('summary','')[:36]}({c['nick']},👍{c['likes']})"
                   for c in picked]
    for c in recheck[:2]:
        title_lines.append(f"• [重看|评论+{c['delta']}] {c['title'][:30]}({c['nick']})")

    # state 更新(所有今天搜到的都记 ccount)
    for nid, n in notes.items():
        st['notes'].setdefault(nid, {})
        st['notes'][nid]['ccount'] = n['ccount']
        st['notes'][nid]['title'] = n['title']
    for c in picked + recheck[:2]:
        st['notes'][c['nid']]['last_capture'] = date.today().isoformat()
    save_state(st)

    # 飞书写入
    n_saved = 0
    if not args.dry_run:
        for c in picked:
            intent = '招聘' if '招聘' in c.get('type', '') else ('营销' if '营销' in c.get('type', '') else '产品讨论')
            if upsert_lead(c, intent, min(95, 50 + (c.get('score', 0) or 0) // 2)):
                n_saved += 1
            time.sleep(0.5)
        print(f'  线索入库 {n_saved}/{len(picked)}')
        push_card(title_lines, hot_lines, len(picked), len(recheck[:2]), cmt_total)
    else:
        push_card(title_lines, hot_lines, len(picked), len(recheck[:2]), cmt_total, dry=True)
        print('(dry-run)')

    print('== 完成 ==')
    return 0


if __name__ == '__main__':
    sys.exit(main())
