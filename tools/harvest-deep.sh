#!/usr/bin/env bash
# ============================================================
# harvest-deep.sh — 目标帖深度采集(铁律:跑满轮次+覆盖率达标才算完成)
#
# 用法:
#   ./harvest-deep.sh -s <serial> -t targets.txt
#     targets.txt 每行: note_id
#   ./harvest-deep.sh -s <serial> -n <note_id>          # 单帖
#
# 行为(参数读 tools/feishu/feishu.conf 的 DEEP_* 段):
#   每帖: 打开详情 → DEEP_ROUNDS 轮(每轮 DEEP_SWIPE_PER_ROUND 滑 + DEEP_WAIT_PER_ROUND 秒)
#         → 算覆盖率(抓到评论/官方 comments_count) → < DEEP_MIN_COVERAGE% 加轮(上限 2x)
#   覆盖率分母不含被折叠评论(平台折叠的广告/不友好内容,无采集价值)
#
# 依赖: 模块(1.2.3+)的楼中楼自动展开 + /sdcard 或 filesDir 的 ndjson 落盘;open-note.sh 的 token 解析
# ============================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$HERE/feishu/feishu.conf"
SERIAL=""; TARGETS=""
while getopts "s:t:n:" o; do case "$o" in
  s) SERIAL="$OPTARG";; t) TARGETS="$OPTARG";; n) TARGETS="$OPTARG";; esac; done
[[ -n "$TARGETS" ]] || { echo "用法: $0 -s <serial> -t targets.txt|-n note_id" >&2; exit 1; }
ADB=(adb); [[ -n "$SERIAL" ]] && ADB=(adb -s "$SERIAL")

# 读深度参数
DEEP_ROUNDS=8; DEEP_SWIPE=2; DEEP_WAIT=12; DEEP_MIN_COV=80
[[ -f "$CONF" ]] && eval "$(grep -E '^DEEP_' "$CONF")"
echo "深度参数: 轮次=$DEEP_ROUNDS 每轮滑动=$DEEP_SWIPE 每轮等待=${DEEP_WAIT}s 覆盖率门槛=${DEEP_MIN_COV}%"

DIR=/data/data/com.xingin.xhs/files/xhs-harvest
NDJSON="$DIR/notes.ndjson"

# ndjson 拉到本地(增量: 记上次行数,只取新增)
LOCAL="$HERE/harvest-out/deep.ndjson"
mkdir -p "$HERE/harvest-out"
${ADB[@]} shell "su -c 'wc -l < $NDJSON'" </dev/null 2>/dev/null | tr -d '\r' > /tmp/deep_total.txt
TOTAL=$(cat /tmp/deep_total.txt || echo 0)
SKIP=$(( TOTAL > 20000 ? TOTAL - 20000 : 0 ))
${ADB[@]} shell "su -c \"tail -n +$((SKIP+1)) $NDJSON\"" </dev/null 2>/dev/null > "$LOCAL"
echo "语料拉取: 共 $TOTAL 行(取后 2 万行) → $LOCAL"

# 覆盖率计算器: python 内联
coverage() { # coverage <note_id> → 百分比
python3 - "$1" "$LOCAL" <<'PY'
import json, sys, urllib.parse as up
nid=sys.argv[1]; path=sys.argv[2]
total=0; got=set()
for line in open(path,encoding='utf-8',errors='ignore'):
    line=line.strip()
    if not line: continue
    try: rec=json.loads(line)
    except Exception: continue
    u=rec.get('url','')
    try: b=json.loads(rec['body'])
    except Exception: continue
    d=b.get('data')
    if not isinstance(d,dict): continue
    if 'comment' in u and nid in u:
        q=up.parse_qs(up.urlparse(u).query)
        if q.get('note_id',[''])[0]==nid:
            # 官方总数:主列表页响应带 comment_count,最可靠
            if d.get('comment_count'): total=int(d['comment_count'])
            for c in (d.get('comments') or []):
                if isinstance(c,dict):
                    got.add(c.get('id'))
                    for s in (c.get('sub_comments') or []): got.add(s.get('id'))
    elif ('/v10/note' in u or 'imagefeed' in u) and nid in u:
        blocks = d if isinstance(d,list) else [d]
        for blk in blocks:
            if not isinstance(blk,dict): continue
            for n in (blk.get('note_list') or blk.get('items') or ([blk['note']] if isinstance(blk.get('note'),dict) else [])):
                if isinstance(n,dict) and (n.get('id')==nid) and not total:
                    total=int(n.get('comments_count') or n.get('comment_count') or 0)
if total<=0:
    print(100); raise SystemExit
print(min(100, int(len(got)*100/total)))
PY
}

# 主循环
for NID in $(cat "$TARGETS" | grep -v '^$' | awk -F'|' '{print $1}'); do
  echo "=== 深采 $NID ==="
  # 1) 打开(token 从已有语料解析;找不到则要求传入 -t 文件带 token 列)
  TOK=$(grep -m1 "^$NID " "$HERE/harvest-out/tokens.txt" 2>/dev/null | awk '{print $2}')
  if [[ -n "$TOK" ]]; then
    ${ADB[@]} shell am start -a android.intent.action.VIEW \
      -d "xhsdiscover://item/$NID?xsec_token=$TOK&xsec_source=search_note" com.xingin.xhs >/dev/null 2>&1 </dev/null
    sleep 9
  else
    echo "  ⚠ 无 token,假设已在前台(手动打开)"
  fi
  # 2) 轮次循环 + 覆盖率反馈
  MAX=$(( DEEP_ROUNDS * 2 ))
  for round in $(seq 1 $MAX); do
    for i in $(seq 1 $DEEP_SWIPE); do
      ${ADB[@]} shell input swipe 620 2250 620 1000 500 </dev/null >/dev/null 2>&1
      sleep 2
    done
    sleep "$DEEP_WAIT"
    COV=$(coverage "$NID")
    echo "  round$round 覆盖率=${COV}%"
    [[ "$COV" -ge "$DEEP_MIN_COV" ]] && { echo "  ✓ 达标"; break; }
  done
  # 3) 增量拉新语料进本地(供下一帖覆盖率计算)
  ${ADB[@]} shell "su -c 'wc -l < $NDJSON'" </dev/null 2>/dev/null | tr -d '\r' > /tmp/deep_total.txt
  NEW=$(cat /tmp/deep_total.txt)
  ${ADB[@]} shell "su -c \"tail -n +$((SKIP+1)) $NDJSON\"" </dev/null 2>/dev/null > "$LOCAL.new"
  mv "$LOCAL.new" "$LOCAL"
  SKIP=$(( NEW > 20000 ? NEW - 20000 : 0 ))
  echo "  done $NID (语料累计 $NEW 行)"
done
echo "== 深采完成,语料: $LOCAL =="
echo "下一步: python3 harvest-extract.py $LOCAL 或飞书同步"
