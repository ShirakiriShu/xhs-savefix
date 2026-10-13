# 本地执行层 · 分工与依赖(2026-10-08 定)

## 分工原则

> **ZCode(带 token 的强模型)= 开发/完善工具**;**本地模型(qwen 27B / 规则引擎)= 日常执行**。
> 日常任务链不需要任何 ZCode token。

## 任务链依赖面盘点

| 环节 | 脚本 | LLM 依赖 | 状态 |
|---|---|---|---|
| 手机采集 | 模块(LSPosed) | 无 | ✅ 纯本地 |
| 深采编排 | harvest-deep.sh | 无 | ✅ 纯本地 |
| 结构化抽取 | harvest-extract.py | 无(规则) | ✅ 纯本地 |
| 线索同步飞书 | feishu/feishu-sync.py | 无(规则分类) | ✅ 纯本地 |
| 日报+卡片推送 | feishu/feishu-digest.py | 无 | ✅ 纯本地 |
| 采集端监控 | mcp/server.py 的 collect_status | 无 | ✅ 纯本地 |
| **深度评论洞察** | harvest-insight.py | **本地 ollama qwen3.8:27b** | ✅ 本地模型 |
| 公司实体抽取 | harvest-companies.py | **本地 litellm 网关 → ollama qwen** | ✅ 已切本地(2026-10-08) |
| 招聘意图判定 | harvest-recruit.py | 同上 | ✅ 同上 |
| 邮箱补全 | harvest-emailfind.py | 同上 | ✅ 同上 |

### LLM 网关(已落地,2026-10-08)

`./local-llm/start-gateway.sh` 一键启动 litellm(127.0.0.1:4000):
- anthropic `/v1/messages` 协议 → ollama qwen3.8:27b-mlx(litellm 做转换)
- 模型别名 **glm-5.1**=本地 qwen(三个脚本默认 MODEL 名,零改动)
- master_key=sk-local-claude-code-gateway(与脚本默认 KEY 一致)
- 已验证:公司抽取端到端正确(宇树/星动纪元,59s/次);思考模型输出 content 里 thinking 块与 text 块分离,
  脚本按 type=='text' 过滤天然兼容
- 注意:27B 速度 1.9 tok/s,单次 LLM 调用 1-5 分钟,批量任务请耐心/过夜跑

## 日常操作(零 ZCode token)

```bash
cd tools
# 每日循环(cron 21:30 示例在 feishu/README.md)
./harvest-deep.sh -s <serial> -t targets.txt     # 深采(覆盖率闭环)
python3 feishu/feishu-sync.py                    # 线索入库
python3 feishu/feishu-digest.py                  # 日报+1616群卡片
# 周度深度(过夜)
nohup python3 -u harvest-insight.py > insight.log 2>&1 &
```

## 何时才需要 ZCode
- 改/修脚本与模块(hook 适配新版小红书、协议变化)
- 新功能开发(新视图、新工具、MCP 扩展)
- 需要强推理的复杂逆向(本地 27B 推理慢且弱于旗舰模型)
