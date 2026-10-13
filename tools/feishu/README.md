# 飞书线索库 · 每日日报 Worker

把 `tools/harvest-out/leads.csv` 同步进飞书多维表格「小红书线索库」,并按天出统计、推群。

## 一句话流程

```
手机(模块采集 ndjson) → harvest.sh intent → leads.csv
    → feishu-sync.py   → 飞书「线索表」(note_id 查重, 新增/更新)
    → feishu-digest.py → 「每日统计表」+ webhook 推群卡片
    → 仪表盘「线索统计看板」自动聚合展示(饼图/排行/趋势/指标卡)
```

## 在线资源(已建好)

| 资源 | 值 |
|---|---|
| 多维表格 | 小红书线索库 `SsaubL7jdaeSu9sdUrJcY3KQnoc` |
| 链接 | https://v7g82ie6bk.feishu.cn/base/SsaubL7jdaeSu9sdUrJcY3KQnoc |
| 线索表 | `tblHigw1zFZGGVbt`(note_id 主键) |
| 账号表 | `tblGBqaVkIz2KKML`(user_id 主键) |
| 每日统计表 | `tblsSqzuqqtdxzJ5` |
| 推送记录表 | `tbl4OcpVSra1goQC` |
| 仪表盘 | 线索统计看板 `blkwQAbPpQNfQouu`(9 组件) |

## 配置(feishu.conf)

- 表 ID / webhook 已写死在 `feishu.conf`,环境变量 `FEISHU_*` 可覆盖
- `TRACK_RULES`:赛道→关键词映射,**换赛道只改这里**(通用性入口)
- `FEISHU_WEBHOOK_URL`:群机器人 webhook(群设置→群机器人→自定义机器人),不填则只写表不推群
- webhook 要真实推群时还需在群里添加机器人并打开"自定义机器人"能力

## 每日操作

```bash
cd tools/feishu
python3 feishu-sync.py                  # leads.csv → 线索表(增量)
python3 feishu-digest.py                # 今天统计 → 每日统计表 (+webhook)
python3 feishu-digest.py --date 2026-10-06   # 补某天
```

cron 示例(每天 21:30):

```
30 21 * * * cd /Users/a1234/xhs-savefix/tools/feishu && python3 feishu-sync.py && python3 feishu-digest.py
```

## 踩坑记录

- lark-cli 字段类型名:多选是 `select` + `"multiple":true`(没有 `multi_select` 这个 type,
  会被静默降级成单选);数字 style 只有 `plain/currency/progress/rating`(没有 percentage)
- `+record-list` 输出是**矩阵布局**:`data.fields`=列名数组,`data.data`=行数组,列序对齐
- 身份:脚本统一 `--as user`(bot 缺 base scope);bot 无 token 过期但需在开发者后台申请 scope,
  要无人值守部署时给 app 申请 `base:app:...` 系列权限再切回 bot
- `+record-upsert` 单选字段传多值会报 "accepts only one option";select 一律传 `["值"]` 数组
- dashboard `statistics` 组件没有 COUNT rollup,计数用 `{"count_all":true}` + filter
- 同名 Base 可重复创建(不冲突),dashboard 同名才冲突;本目录曾产生过一个重复空库,
  有 search:docs:read scope 后可用 `lark-cli drive +search --doc-types bitable` 找到并清理

## 每日监测(增量,全本地)—— harvest-monitor.py

每天自动:搜索监测关键词 → 只看新帖(老帖评论涨≥15 才重看) → 本地 qwen 相关度筛选 Top5
→ 深采(可选) → 新帖入线索表 → 交互卡片推 1616 群。零 ZCode token。

```bash
cd tools
python3 harvest-monitor.py --no-capture     # 轻量:只筛新帖+推卡(约8分钟)
python3 harvest-monitor.py                  # 完整:含深采(每帖再加约2-4分钟)
```

cron(每天 21:30):
```
30 21 * * * cd /Users/a1234/xhs-savefix/tools && python3 -u harvest-monitor.py >> harvest-out/monitor.log 2>&1
```

状态: `harvest-out/monitor_state.json`(记录每帖上次评论数,增量判断依据)。
配置: `feishu.conf` 的 MONITOR_* 段(关键词/TopN/评论增量门槛/深采轮次)。
