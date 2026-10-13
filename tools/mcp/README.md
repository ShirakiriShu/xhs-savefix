# xhs-leads MCP server

把小红书线索管线(sync/digest/查询/推送/采集状态)包成 MCP tools,运营同学在 AI 助手里自然语言使用。

## 工具

| 工具 | 用途 | 例句 |
|---|---|---|
| `query_leads` | 查线索表 | "今天新增哪些 AI 录音硬件线索?""带邮箱的招聘线索有哪些?" |
| `sync_leads` | leads.csv → 飞书(增量) | "同步一下今天采集的数据" |
| `daily_digest` | 日报统计+推群 | "出今天的日报推到群里" |
| `push_feishu` | 手动推群 | "把这段话推到运营群" |
| `collect_status` | 采集端状态 | "采集还正常吗?" |

## 接入 ZCode / Claude Code

```json
{
  "mcpServers": {
    "xhs-leads": {
      "command": "/Users/a1234/xhs-savefix/tools/mcp/venv/bin/python",
      "args": ["/Users/a1234/xhs-savefix/tools/mcp/server.py"]
    }
  }
}
```

前置: `lark-cli auth status` 已登录;`../feishu/feishu.conf` 表 ID 正确;webhook 推群需配 `FEISHU_WEBHOOK_URL`。

## 手动冒烟

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | ./venv/bin/python server.py
```
