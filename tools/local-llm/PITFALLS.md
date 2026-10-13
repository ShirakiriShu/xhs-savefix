# 本地模型踩坑记录(append-only,新坑追加不要覆盖)

## ollama / qwen3.8:27b-mlx

### 1. 速度是硬约束
- 实测 **1.9 tok/s**(M 系列 MLX,18GB 模型)。一批 12 条评论 ≈1500 输出 tokens ≈ **12-13 分钟**
- 353 条全量 ≈ 7-8 小时 → 只适合过夜/周度跑;日常用规则引擎,紧急验证用 6-12 条小批量
- `ollama pull` 小模型很慢(网络),想快只能减 tokens/减批量,不能换模型(本机只有 27B)

### 2. `format:"json"` 不保证数组
- 即使指定 JSON 模式,qwen 会把"数组"返回成**以序号为 key 的字典** `{"0":{...},"1":{...}}`
- 甚至返回元素为 int/str 混合的退化输出(实为长文本被中途截断)
- **解法**:解析层做三重容错——①`isinstance(dict)` 取 values;②截取首 `[` 到尾 `]`;③截断 JSON 补 `]` 闭合。
  但最稳的架构是**阶段1 用自由文本清单**(模型擅长归纳),只在阶段2(汇总)要 JSON

### 3. num_predict 截断 = 隐性解析失败
- 300 tokens 时 JSON 输出到一半被掐,报"Unterminated string"
- 批量任务 `num_predict ≥ 2000`(清单类 600 够);失败时先怀疑截断,再看格式

### 4. 逐条结构化(每条评论一个 JSON 对象)在这个模型上不稳
- 30 条/批时全部失败;12 条/批格式才开始稳定
- 大批量 + 复杂 schema → 格式漂移概率随批大小指数上升。**宁可多批小,不要少批大**

### 5. 超时设置
- 单批 ollama 调用 timeout ≥ 900s(27B 速度下 1500 tokens 就要 13 分钟)
- 宿主脚本用 `nohup + -u`(unbuffered)后台跑,print 缓冲会让日志空置半小时像死机

## 飞书 lark-cli(供本地模型调工具时参考)

### 6. 消息撤回权限
- bot 发的消息**只能 bot 身份撤**(`--as bot` DELETE /im/v1/messages/<id>);user 撤 bot 消息报 not valid
- message_id 要存完整(截断 24 位的 ID 撤回会报 invalid);digest 已把 msg_id 写进「推送记录表」

### 7. litellm gateway 协议适配(harvest-companies 等切本地时)
- ollama 原生只有 OpenAI `/v1/chat/completions`,不支持 anthropic `/v1/messages`
- 要么 litellm 做协议转换代理(`litellm --model ollama/qwen3.8:27b-mlx`),要么改脚本协议
- 改协议时注意 anthropic 的 system/messages/max_tokens 字段名与 OpenAI 不同(system 是顶层字段 vs messages[0].role=system)

### 8. Bitable 写入
- select 字段永远传数组 `["值"]`,单选多值会报 "accepts only one option"
- 字段类型名:多选是 `select + multiple:true`(**没有** `multi_select` 类型,会被静默降级成单选)
- number 的 style 只有 plain/currency/progress/rating(没有 percentage)
- record-list 的 `--format json` 返回**矩阵**:data.fields=列名数组, data.data=行数组

## 流程纪律

### 9. 深度覆盖铁律(2026-10-07 教训)
- "每帖滑 2 屏等 18s"式敷衍采集 → 4/8 篇覆盖率 31-68%,数据没价值
- 必须跑满 DEEP_ROUNDS=8 轮 + 覆盖率≥80% 才算完成(harvest-deep.sh 已固化)

### 10. 先链路后动手
- 遇到"请求不通/行为异常",禁止盲试参数;先反编译源码把完整链路查清
  (调用方→实参→client 构建→鉴权在哪层),失败路径也是结论,写入文档

### 11. litellm 网关切本地实测(2026-10-08)
- 关键配置:litellm config 里 `model_name: glm-5.1`(脚本默认模型名)→ `litellm_params.model: ollama/qwen3.8:27b-mlx`,
  配合 master_key=脚本默认 KEY → **三脚本真正零改动**(协议/解析/模型名全兼容)
- `drop_params: true` 必开:anthropic-version 头会被当未知参数报错
- **思考模型坑**:qwen3.8 经 anthropic 协议返回 content 数组含 `thinking` 块+`text` 块,
  脚本 `b.get('type')=='text'` 过滤恰好兼容(写代码时的无心之功);
  若 max_tokens 太小(<200)会全被思考吃掉,text 为空
- **客户端超时**:脚本原 `urlopen timeout=120s`,27B 大批次(长 prompt+思考)会超;
  已统一改 600s。litellm 侧也要 `request_timeout: 600`
- 实测延迟:公司抽取 59s/次(输入短);批量按 3-5 分钟/批预估

### 12. qwen3.8 + ollama 增量监测实战(2026-10-08)
- **`think:false` 是开关**(api/generate 顶层参数):86s/次 vs 思考模式 560s+;
  prompt 里写 `/no_think` 文本标签经 litellm 网关**不可靠**,要直连 ollama 传参数
- **ollama 串行队列陷阱**:客户端超时断开后,服务端仍在继续生成,后续请求排队阻塞
  (症状:下一次调用也莫名超时)→ `ollama stop <model>` 清队列;注意 stop 后冷加载≈189s
- **format:json + think:false 时输出形态**:可能只返回**单对象**(不是数组)且只评估第 1 条
  → 解析要兼容单对象/索引map/数组三种;prompt 必须写"必须为每一条输出"
- **LLM 前先关键词预筛**:按点赞排序的候选会把无关热帖(美食/探店)排最前,
  浪费 LLM 预算;先按监测词过滤标题再送分类
- 自动推送要加空结果保护,否则会往群里发空区块卡片(已修)
