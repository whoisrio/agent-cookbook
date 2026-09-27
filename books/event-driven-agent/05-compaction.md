# Stage 5：上下文压缩——有损变换的纪律（原 5b）

> 配套代码（设计稿，代码尚未落地）：计划新增
> `src/baby_event_driven_agent/stages/stage05_compaction/`

## 为什么要做压缩

即便现在的模型已经支持百万级上下文（GPT-1.1 的 extra 模式支持 1.1M），长程任务下照样不够用：agent 连着干几个小时、跨几天 resume，工具结果一轮轮堆上去，百万 token 也有见底的时候。

经过上一章的处理，咱们的 agent 已经有了完整的 trajectory，这一章我们基于 trajectory 轨迹来处理上下文压缩；这一章，我们就来严肃处理上下文的压缩。

## 核心设计摘要

本文档的核心规则只有两条，其余所有内容都是这两条规则的配套细节：

**规则一：触发——按 token 检测，按轮次下刀。**

每次 LLM 调用前计量投影出来的 messages，越过水位线（默认 70% 窗口）就触发。但刀口不是切在 token 位置，而是落在倒数第 N 个 turn 的第一条 user 消息上，保证 tool 配对完整。保留最近 N 轮原文不动，刀口之前的整体送去摘要。

**规则二：折叠——旧摘要被新摘要吞掉，原文永不重读。**

多次压缩时，新摘要的输入 = 上一刀摘要 + 两刀之间的增量轮次。摘要模型永远只看到"上一刀摘要 + 新增内容"，从第一次压缩之后就不再重读任何原始全文。递归折叠，原文不回放。

记住这两条，后面的内容就是它们的展开。

## 压缩机制：两种天然的想法

最容易想到的压缩方式有两种。

一种是按 token 消耗：算上下文占了多少 token，超过窗口的某个比例就压，和窗口限制直接对齐。但是，agent 与 llm 交互，是要严格遵循消息格式的，只判了 token 数量，有可能压缩时会意外截断消息，导致消息角色不匹配。

另一种则是按轮次：数 turn 数，超过 N 轮就压，token 都不用算。这种方式，虽然确保了消息格式的正确性，但是每一次与 LLM 交互的 tool_result 长度都可能是不一样的，有可能某次查询工具的结果返回了超多数据，而后 n 轮的工具调用返回的结果却很少，这种机制，不容易找到正确的压缩时机。

两种方案各有问题，实际方案是把它们组合起来。

### 实际方案：按 token 检测，按轮次压缩（两步走）

主流 agent 的做法，都是综合了上述两种方式。关键要理解这是**两个独立的步骤**：

- **第一步——检测（token 计量）**：每次 LLM 调用前计量投影出来的 messages，越过水位线就触发。计量以 API 返回的真实 usage（`prompt_tokens`）为准，落代码校准；调用之间的新增量用字符估算，不引 tokenizer 依赖。
- **第二步——下刀（找 turn 边界）**：触发后不是切在 token 位置，而是找到倒数第 N 个 turn 的第一条 user 消息，刀口落在这里。这样保证 tool 配对完整，不用再修序列。保留最近 N 轮（比如 3 轮）原文。

需要强调：**刀口的位置由 turn 边界决定，不是由 token 数量决定。** 所以实际压掉的量通常比"70% 以上的部分"要多——因为一整个 turn（含所有 tool 往返）会被整体送走。这不是浪费，是代价：换来了消息格式天然合法。

另外，压缩时，会保留最近 N 轮的对话不进行压缩，以免冲掉了用户最近的要求。咱们的 agent 的 system prompt，是实时添加到 messages 里，因此 system prompt 的内容是不进入压缩窗口的。

### 水位和窗口

W 是模型窗口，输入输出共享，设两条比例线：

```text
token 用量轴：

 0 ├────────────────────────────────┼──────────────┼─────┤ W
                                  T·W             H·W
                                  压完落到这以内    用量到这就触发

压后预算：tokens(摘要) + tokens(最近 N 轮) + headroom ≤ T·W
headroom：下一轮用户输入 + 回复的 max_tokens + 一次工具往返
```

H（如 0.7）不能设太满：压缩自己也是一次模型调用，要读被压段原文、要留写摘要的输出余量（见"压缩救不了自己"）。N 由 T（如 0.4）反推。N 轮的 token 量有上界，因为单轮工具结果有分页和 cap 兜着（见后）。

触发那一刻哪些消息送去摘要（N=3，对话走到 t7，t7 还在进行中）：

```text
 t1       t2        t3       t4         │ t5       t6        t7（进行中）
 u1 a1    u2 a2 t2  u3 a3    u4 a4 t4   │ u5 a5    u6 a6 t6   u7 a7…
└───────────── 送去做摘要 ────────────┘ └──────── 原样保留 ────────┘
                                        ↑ 刀口 keep_from = u5
                                          （t5 的第一条消息，turn 边界）

摘要调用输入：摘要 prompt + 上一刀的摘要（若有）+ t1..t4 的全部消息
压缩后视图：  [system][<摘要>][t5][t6][t7…]
```

- 刀口之前不删除，只是这一次的视图里由摘要替代，原文还在轨迹里；
- t7 走到一半也在保留窗里：进行中的 turn 不能切，切了 tool 配对就不完整；
- 已经压过一次时，更早的轮次不在视图里，送去摘要的是上一刀的摘要加两刀之间的轮次（见"滚动折叠"）。

### 触发位置：第四种边界动作

检测在 `build_context` 之后、正式调用之前，只在 step 结算后做：

```text
steering  = 等 step 边界，把消息拼进当前上下文，不打断在飞的（Stage 2）
interrupt = 掐掉在飞的 step，turn 结束（Stage 3）
redirect  = 掐掉在飞的 step + 补消息，turn 不结束（Stage 3）
compact   = step 边界处换一副更短的视图，再发下一次调用
            （手动档已在 04 落地；本章补自动水位触发，同一函数、reason 不同）
```

流中间不换 messages，在飞请求的上下文会漂移。压缩调用本身也是一次在飞的 LLM 调用：被 interrupt 掐断就不 append（Stage 3 纪律，append 只在摘要成功结算后），压缩期间到的 steering 等下一个 drain 点。

`/compact` 手动压缩和自动水位触发走同一个函数，只在 reason 上区分（manual / watermark / fallback）。

## 压缩模型选型

压缩 LLM 是"只读压缩"角色，和对话 LLM 的能力需求完全不同：

| 维度 | 对话模型 | 压缩/摘要模型 |
|---|---|---|
| 需要工具调用 | 是 | 否（裸 chat） |
| 需要推理/规划 | 强 | 否（只压缩不推理） |
| 上下文窗口 | 越大越好 | 必须 ≥ 被压段原文 + prompt + 输出余量 |
| 输出精度 | 高 | 中（摘要编错有代价但可回查） |
| 调用频率 | 每轮 | 仅触发时 |

因此**压缩模型可以和对话模型不同**，选型优先级：**窗口容量 > 摘要忠实度 > 成本 > 延迟**。压缩模型不需要对话模型那样强的推理能力。

具体策略：

- **默认（小/中型会话）**：复用对话模型。简单、摘要风格一致、不引入额外依赖。
- **长程/生产会话**：用小一号但窗口足够的模型（如对话用大模型，压缩用同系列小模型）。成本能差一个数量级。
- **极端场景**：被压段本身很大，主模型窗口装不下 → 降级到窗口最大的可用模型，或走分块 map-reduce（兜底阶梯第 3 级）。

**W 是运行时真正生效的窗口（实现期真坑）。** 模型宣传值不等于服务端生效值：本地 ollama 默认 num_ctx=4096，模型声明 262k 也白搭——摘要调用的分段原文加思考链加输出挤在 4096 里，摘要动辄被拦腰截断（空摘要或半截摘要）。
对策：本地派生模型显式设 num_ctx（本书 demo 用 `qwen3.5:4b-32k`，一条命令重现：`ollama create qwen3.5:4b-32k -f Modelfile`，Modelfile 两行——`FROM <原模型>` 加 `PARAMETER num_ctx 32768`），或按选型表核对运行时窗口再填 `window_tokens`。

配置上在 `CompactionPolicy` 补两个字段：

```python
summarizer_model: str | None = None  # None = 复用对话模型
fallback_model: str | None = None   # 主选装不下时降级
```

## 压缩调用的 messages 组装策略

压缩 LLM 的输入不是直接把对话视图切片扔过去，而是经过一层转换。
核心原则：**压缩 LLM 的输入严格等于对话 LLM 当时的视野（信息等价，形态可以不同），不展开 blob、不补后续页、不做任何增强。压缩 LLM 的信息量 ≤ 对话 LLM 当时的信息量，不多不少。**

**形态：序列化成一段文本，不按对话轮次丢。**

最直接的做法是把被压段的消息原样保留（role、tool_calls 与 tool_call_id 配对）作为 messages 丢进摘要调用。
本章不做：被压段序列化成**一段文本**，放进单条 user 消息的 content。两个真实理由：

- **防止模型把输入当成要继续的对话。**
  被压段通常结束在 assistant 或 tool 消息上，按消息序列丢进去，模型最后看到的模式是"一段进行中的对话"。
  它很容易顺着接话、继续调工具，或在摘要前先回两句——摘要调用要的是"读完材料写文档"，不是"接手聊天"。
  序列化之后整体变成一份带角色前缀的资料文档，不再是可供接续的对话。
- **摘掉摘要调用的格式负担。**
  provider 对消息序列有严格约束：tool_result 必须与 tool_calls 配对，孤儿结果不合法。
  被压段来自轨迹，可能带着崩溃/打断留下的孤儿工具结果——对话调用有 04 的 sanitize 补 UNKNOWN 占位兜着，摘要调用是另一个消费者，按消息组装就得自己再养一套 sanitize。
  序列化把角色变成文本前缀，provider 不校验格式，孤儿结果照样读得懂。

代价是结构保真度：tool_call 的 id 配对、消息边界变成文本，靠模型自己读。
摘要不需要补发工具调用、也不引用任何 tool_call_id，这点代价可以接受——"调了哪些工具、什么参数、结论是什么"在文本里完好保留（正是摘要要保住的四样东西的前两样）。
pi 是同一个选择：压缩与分支摘要都经 `serializeConversation()` 序列化成文本再发，注释写明的目的就是防止模型把输入当成要继续的对话。

转换管线：

```text
对话 messages（视图里的样子）
        │
        ▼
┌────────────────────────────────────────────┐
│  1. 剥离对话 system prompt                 │ ← 对话的行为指令对摘要没用
│  2. 剥离 tool definitions                  │ ← 裸 chat，不需要工具定义
│  3. 每条消息按视图渲染成文本               │ ← 角色变前缀；tool_calls 渲染成工具名+关键参数，
│                                            │    tool 结果分页/cap 后什么样就渲染什么样，
│                                            │    assistant 与 tool 结果的先后顺序原样保留
│  4. <conversation> 标签包裹                │ ← 声明"这是资料，不是对话"
│  5. 上一刀摘要前置（若有）                 │ ← <previous-summary> 标签，滚动折叠时旧摘要折在这里
│  6. 压缩指令收尾                           │ ← 模型最后看到的是"输出摘要"，不是没聊完的对话
│  7. 可选：文本裁剪                         │ ← 仅当压缩模型窗口小于对话窗口时
└────────────────────────────────────────────┘
        │
        ▼
   摘要调用输入：[system][user（序列化文本 + 指令）]，messages 数组只有这两条
```

**① System prompt：不发对话的，改用压缩专用指令**

对话的 system prompt 是行为指令（"你是 xx agent，规则是 xx"），对摘要模型没用且浪费 token。摘要 LLM 用专门的压缩指令：

```python
COMPRESSION_SYSTEM = """你是一个上下文压缩器。任务是将提供的对话历史压缩为结构化摘要。
不要继续这段对话，不要回答对话里的任何问题，只输出摘要。
严格遵循以下规则：
1. 只压缩，不推理，不补充未发生过的事实
2. 按以下分段输出：已完成 / 关键事实与数据 / 副作用 / 待办
3. 保留所有工具调用的关键参数和结果结论
4. 明确标注副作用（写/改操作），防止重复执行
5. 如果信息不足无法压缩，输出 [INSUFFICIENT] 并说明原因"""
```

**② 指令位置：system 定角色，user 文本末尾定任务**

system 放压缩专用指令（①），任务指令再放在序列化文本的**末尾**——资料在前、指令收尾，模型最后读到的是"输出摘要"（pi 的 SUMMARIZATION_PROMPT 开头即 "The messages above are a conversation to summarize"，就是这个用意）。
滚动折叠时，上一刀的摘要以 `<previous-summary>` 标签放在指令之前，指令要求"把新内容并入既有摘要"（对应 pi 的 UPDATE_SUMMARIZATION_PROMPT）。

**③ tool 消息的处理**

被压段里的 `tool_use` + `tool_result` 消息，不管结果是完整的第一页还是被 cap 截断的头部+标记，**视图里是什么样，就原样渲染进序列化文本**。
不展开 blob、不补后续页、不做任何"增强"。
也不再叠第二层截断：单条消息的体积已被分页和 cap 封住，序列化时再截一刀反而破坏"不多不少"（pi 序列化时把 tool 结果截到 2000 字符控制摘要请求体积，本章这一层已经由 cap 承担）。
这一点至关重要，详见下节。

## 压缩时的 tool 信息

### tool_result 分层视图

谁在什么时机看到 tool_result 的什么版本，这是整个压缩设计中最容易混淆的地方，先看清楚这个矩阵：

| 消费者 | 阶段 | 是否看到 | 看到的是什么 |
|---|---|---|---|
| 对话 LLM | 保留窗内，分页工具 | 是 | 当前页（按 limit） |
| 对话 LLM | 保留窗内，超 cap 工具 | 是 | 头部 N 字符 + 标记 |
| 对话 LLM | 被压段 | 否 | 由摘要文本替代，原文不发 |
| 压缩 LLM | 被压段 | 是 | 视图内容原样（渲染进序列化文本，分页/cap 后版本，非 blob 全文） |
| 任何 LLM | blob 存档 | 否 | 永不自动解引用（模型没有这工具） |

**核心结论：压缩 LLM 看到的 = 对话 LLM 当时看到的。** 如果对话 LLM 当时只看到第一页或头部，压缩 LLM 也只看到这些。这是严格约束，不是建议。

这个约束的原因在"工具开发规范"里展开：如果压缩 LLM 偷偷读了 blob 全文，就会出现"模型当时没看到的东西，摘要里说它看到了"，摘要变成了篡改历史。

### 同一份 tool_result 的两个收件人

- **摘要调用**：被压轮次的 tool 消息按视图里的样子渲染进序列化文本——分页/cap 后是什么样就渲染什么样，不发 blob 全文，因为摘要调用是裸 chat、不带工具，不能翻页。
- **压后的对话调用**：被压轮次的 tool 消息不再发，由摘要替代；保留窗里的工具对话原样成对保留。

### 摘要 prompt 要保住四样东西

1. 调过哪些工具、关键参数（查了什么、改了哪个对象）；
2. 结果结论：成功/失败和关键数据；
3. 副作用：write/update 做过就是做过，丢了模型压完会重做（5a 的 `branch_with_summary` 防的就是这个）；
4. 未决事项：任务进行到哪、下一步原本要干什么。

摘要调用走裸 chat，不带工具，`max_tokens` 封顶，输出按"已完成 / 关键事实与数据 / 副作用 / 待办"分段，只压缩、不推理、不补没发生过的事。摘要编错一句结论会污染之后所有轮次；原文可以靠 rewind 和 blob 回查。

## 大工具结果与工具开发规范

### 工具开发规范：结论前置（硬约束）

这是整个压缩系统正确性的基础约束，必须显式写进工具开发规范，不是顺带提醒。

> **约束 1（结论前置）**：任何可能返回大结果的工具，必须保证默认返回（第一页/cap 内可见部分）包含：
> - 操作整体状态（成功/失败/部分失败）
> - 失败条目的原因摘要
> - 后续动作指引（是否需要重试、怎么翻页）
> 
> 违反此约束的工具不得接入 agent。

**为什么这是硬约束：**

cap 截断后，消息里只剩头部。如果工具不保证"头部就有结论"，压缩 LLM 和对话 LLM 都会丢失关键信息，导致模型做出错误决策——最典型的是重复执行写操作（模型以为没做成功，又调了一次）。

cap 是兜底，不是主设计。你不能指望 cap 永远不触发，也不能指望触发后"刚好截断在安全位置"。工具开发者和 agent 框架开发者可能是不同的人，如果不在规范里写死，工具作者根本不知道自己的工具会被 cap + 压缩。

**好的工具设计长这样：**

```json
{
  "summary": "500 条中 498 条成功，2 条失败",
  "overall_status": "partial_failure",
  "failed_items": [
    {"sku": "CUP-003", "reason": "库存不足"},
    {"sku": "CUP-007", "reason": "SKU 不存在"}
  ],
  "details_truncated": true,
  "next_page_token": "eyJvZmZzZXQiOjIwfQ=="
}
```

这样即使 cap 只保留了头部，模型也能看到：整体结果是什么、有没有失败、为什么失败、后续该干什么。

**反例（坏设计）：**

- 先返回无关描述，关键字段在末尾
- 逐条平铺全部结果，结论在最后一条
- 只有数据，没有总数和状态

### 两种长结果

契约内分页：工具自带 `offset/limit/page_token`，返回一页就是一次完整回答，没有截断、只有下一页，响应带 `total/has_more`。列表查询、工单详情都是这类。

契约外 cap：工具语义上返回完整结果（比如 batch 的逐条结果），harness 套一道字符上限兜底，超了就头部进消息、全文存档、标记自描述。

两者的边界一句话：**截断必须配取回通道**。给承诺完整、又没有分页参数的工具（裸 read_file、无参全量查询）硬套 cap，它每次都返回不完整还没法继续，等于工具在撒谎。

### 工具集扩充

现有四件套撑不出长会话，也没有大结果。5b 在业务世界内加工具，不引入文件系统语义（read_file 是 coding agent 的形态，不贴合运营 agent）：

| 形态 | 工具 | 剩余部分怎么取 |
| --- | --- | --- |
| 列表分页 | `query_inventory(category?, max_stock?, offset, limit)`、`search_rules(query, offset, limit)` | 同参数翻页，返回 `total/has_more` |
| 单条详情 | `get_inventory_detail(category)` | 列表只给摘要行，完整规格按 key 取 |
| 工单域 | `list_tasks(status?)`、`get_task(task_id, offset, limit)` | 工单条目分页，多轮任务的驱动器 |
| 批次句柄 | `batch_update_inventory(items[])`（每批条数有上限，一次审批，返回 `result_id`）、`get_batch_result(result_id, offset, limit)` | 写操作不可重放，尾部按句柄查，不能让模型再调一次拿结果 |

即 4 个现有工具（查询类加分页）加 4 个新工具。

### cap + blob 存档

cap 包装在工具执行层（`_execute_call` 拿到结果字符串之后、落轨迹之前），对所有工具生效：

```text
工具返回原始结果（如 200k 字符）
 ├─ 在 cap 内：消息内容 = 结果原文，照旧
 └─ 超 cap：
     全文 ──► blobs/<sha256>（只写一次、不可变；内容寻址 = 去重 + 校验）
     消息内容 = 头部 N 字符 + 自描述标记（共多少、存档 id、省略多少）
     轨迹 payload 另记元数据 {archive_id, bytes, sha256, omitted_chars}
     （和 synthetic/note 一样是审计注脚，发模型前 strip）
```

分页标记是工具自己写的（has_more、下一页参数），存档标记是 cap 包装加的，出处不混。

头部进消息、进轨迹，全文只进 blob，投影不解引用。resume 还原的是模型当时收到的头部加标记，不灌 blob 全文：灌了，长会话一恢复就重新撑爆窗口；模型当时没看到全文，灌回去是伪造历史；投影依赖外部文件状态，可复现不变式就破了。取回尾部是模型主动发起的业务动作（翻页、详情、句柄查询），blob 是审计存档，回答"工具当时到底返回了什么"，保留期和脱敏也单独设。

这样单条消息被契约页大小封住，比窗口还大的"毒丸"进不了轨迹；cap 只防没兜住的第三方工具和异常记录，demo 里把 cap 调到比一页还小来触发它。

数据用 5b 包自带的 `data/`（脚本确定性造几百到上千行库存和若干工单），不动共享的 knowledge-base——1 到 5 章的测试断言了"保温杯 42 件"这些具体值。

## 多次压缩：滚动折叠

第二次压缩时，视图已经是 [摘要 A] + [保留段]，新摘要吞掉旧摘要，不重读全文：

```text
原始轨迹：e1 e2 e3 e4 e5 e6 e7 e8 e9 e10
第一次压缩 compA（keep_from=e7）：e1..e6 由摘要 A 代替
  视图 A：[<摘要 A>, e7, e8, e9, e10]
  轨迹：  e1..e10, compA(keep_from=e7)

继续聊到 e14，再次触发，新刀口 keep_from=e12：
  摘要 B 的输入 = 摘要 A 文本 + e7..e11
                 （当前视图里、新刀口之前的全部内容）
  视图 B：[<摘要 B>, e12, e13, e14]
  轨迹：  e1..e10, compA, e11..e14, compB(keep_from=e12)
          投影只认最后一刀 compB：它之前的全部 entry（含 compA
          节点）跳过，摘要 B 插视图最前
```

**精确表述**：多次压缩时，新摘要的输入 = 上一刀的摘要 + 两刀之间的增量轮次。**不是"之前压缩过的所有内容"累积堆叠**，而是只带上一刀摘要（旧摘要已经被折叠进去了，它代表了一切更早的历史）。

摘要模型永远不重读 e1..e6 的原文，它们由摘要 A 代表，这也是死循环成因一的解法。`build_context` 相应从 5a 的"认第一条"改成"认最后一切"；5a 当初认第一条，是折叠语义没定义时不丢信息的保守做法。

rewind 不用特殊处理：压缩是当前路径上的视图，rewind 到它之前它就不在路径上（5a 已测）；折叠只发生在同一路径的 compaction 之间，分叉出去的分支各算各的。

折叠有损，递归摘要误差叠加，越老的细节越模糊。所以摘要只管连续性（进行到哪、别重复什么），不管事实查询；关键数据查轨迹和 blob，长期事实留给记忆投影（Stage 6 后记）。

## 压缩救不了自己：死循环与兜底阶梯

快到边界时有个死局：不压，下一步超长；压，压缩调用自己也超长。

```text
正式调用预算：system + 历史 H + 新输入 + 输出余量          ≤ W
压缩调用预算：摘要 prompt + 被压段原文 S + 摘要的输出余量   ≤ W
                            ↑ 压完之前 S 一个 token 都没少，
                              压缩没法作用于它读不到的东西

H 涨到 ≈ W：
 ├─ 不压 → 正式调用 H + 新输入 > W → 400
 ├─ 压   → 摘要调用 P + S + 输出余量 > W（首次压缩 S ≈ H ≈ W）→ 400
 └─ 错误处理里"压完重试"→ 压失败 → 重试 → 再 400 ……（死循环）
```

两个成因。

**一，首次压缩触发太晚。** 水位设到 95% 或等 400 才压，第一次的被压段约等于整段历史，摘要调用装不下，近边界处连写摘要的输出余量都没有。两个变体：压完保留轮次太多、摘要太长，下一次调用仍超限，再压已经没有够小的可压段（空转 churn）；字符估算偏小把会话骗进被动模式，400 成了唯一的发现途径。"滚动折叠"给解法：每次摘要的输入是旧摘要加新增段，水位早触发时它天然小于一次正式调用。

**二，单条消息本身超过窗口，压缩救不了：**

```text
某步工具返回比窗口还大的结果：
 ├─ 正式调用：这条 tool 消息装不下 → 400
 ├─ 压缩调用：想摘要它，就得先把它原文发给摘要模型 → 还是装不下 → 400
 └─ 删也不行：assistant 的 tool_calls 悬在前头，provider 要求配对
    tool 结果；sanitize 补 UNKNOWN 占位后，模型发现结果"缺失"，
    只会把工具重调一遍
```

它发生在 turn 中间，刀口却只能在 turn 边界。这个成因只能在工具层消灭，也就是前面的分页加 cap。

兜底阶梯如下，每一级请求体严格变小；踩 400 后原样重试就是死循环本身，禁止：

1. 水位主动压缩；
2. 400 后激进压缩，保留轮次从 N 调小，先保住当前 turn；
3. 被压段本身仍超窗：分块 map-reduce 摘要（逐块摘要再归并），此时可能需要切换到窗口更大的 `fallback_model`；
4. 单条超大消息：投影层换成指针/占位（同 UNKNOWN 占位纪律，不改事实层），或走分页/句柄重取；
5. 都不行：如实告知用户，或带一份交接摘要开新会话。

压缩失败 fail-open：本次不压、留痕、下一边界重试，不静默截断。对应 pi 的 `session_before_compact`（可取消/自定义）、`session_compact`、`session_compact_failed` 三个钩子。

## 留痕与可复现不变式

### 两层账各记什么

轨迹的坐标是 entry id，EventLog 的坐标是 seq，不混用。

`compaction` entry 在 5a 的 `summary` + `keep_from_id` 上补齐字段：

| 字段 | 内容 |
|---|---|
| `summary` | 摘要文本（折叠时已吞掉旧摘要） |
| `keep_from_id` | 刀口：从该 entry 起原样保留 |
| `from_id` / `to_id` | 被压区间（当前视图意义上的区间；折叠时含旧摘要） |
| `policy_version` | 水位、保留轮次、摘要 prompt 这套策略的版本号 |
| `tokens_before` / `tokens_after` | 压前压后的计量值与摘要 token 数 |
| `summarizer` | 摘要用的模型（可与对话模型不同） |
| `summarizer_model` | 摘要模型标识（显式记录选型） |
| `hash` | 被压区间规范化内容的 sha256，钉住"这刀针对的原文" |
| `reason` | watermark / manual / fallback |

EventLog 走 Stage 4 的 `bus.record`：`context_compacted`（原因、水位、耗时、成败、sid / correlation id），失败记 `context_compact_failed`。轨迹答"模型看到了什么、刀口在哪"，EventLog 答"什么时候、因为什么、花了多久压了一次"。

### 不变式：给定 (trajectory, policy_version) → 唯一 messages

摘要是模型生成的，同一段历史压两次文本可能不同，可复现性靠一条：**摘要文本生成即落盘，它是事实、不是策略产物；重放读盘上的 summary，绝不重跑摘要模型。** `policy_version` 钉的是策略，摘要结果钉在事实层，同一份轨迹在任何机器上重放，投影逐字节相同。`hash` 用来核对这刀声称压掉的区间和盘上原文对不对得上。

Stage 6 直接用这个不变式：golden trajectory 存事实层加 policy_version，不存压缩后的 messages（否则评测被某一次策略绑死）；硬断言走事实层，judge 的输入走视图层并注明模型当时看的是压缩上下文；换过压缩策略的两次评测不能直接比较。

### 压缩的代价

- 多一次摘要调用：钱、延迟、一个新的失败面；
- 摘要编错会污染之后所有轮次（只压缩不推理，原文可回查）；
- 递归折叠的误差累积；
- 压缩让 prompt cache 的前缀整体失效。缓存省成本不省窗口，压缩省窗口但会作废缓存，水位别设太低。

## 代码改动（设计稿，待实现）

新包 `stage05_compaction/` 从 `stage04_trajectory/` 拷贝，`transport/`、`session/` 不动，增量如下。

`session/compaction.py` 已有手动档（cut_before_turn / Summarizer 两档 / maybe_compact，见 04），本章在同一个文件上扩展：

```python
@dataclass(frozen=True)
class CompactionPolicy:
    version: str = "2026-09-25.v1"
    window_tokens: int = 0        # 模型窗口 W
    watermark: float = 0.7        # 用量到 H·W 触发（按 token 检测）
    target: float = 0.4           # 压完摘要 + N 轮落到 T·W 以内
    headroom_tokens: int = 0      # 下轮输入 + 回复 + 一次工具往返
    keep_turns: int = 3           # 保留最近 N 轮（按轮次下刀）
    summarizer_model: str | None = None   # None = 复用对话模型
    fallback_model: str | None = None     # 主选装不下时降级

def estimate_tokens(messages: list[dict]) -> int: ...       # 真实 usage 锚定 + 增量字符估算
def cut_before_turn(entries, keep_turns: int) -> str: ...  # 刀口 = 倒数第 N 个 turn 的第一条 user
                                                           # 进行中的 turn 计入保留窗

class Summarizer(Protocol):
    async def summarize(self, segment: list[dict], previous: str | None) -> str: ...
    # ScriptedSummarizer（离线确定性）/ LiveSummarizer（裸 chat、不带工具、max_tokens 封顶）

def serialize_segment(segment, previous_summary, policy) -> dict:
    # 序列化管线：剥离 system / 剥离工具定义 / 逐条按视图渲染成文本
    #           （tool_calls 带参数、tool 结果分页/cap 后原样）→ <conversation> 包裹
    #           → <previous-summary> 前置 → 压缩指令收尾；返回单条 user 消息
    #           （LiveSummarizer 的渲染逻辑上提为这条公共管线）

async def maybe_compact(traj, policy, summarizer, *, reason="watermark") -> Entry | None:
    # 投影 → 计量 → 未越水位返回 None（手动/fallback 跳过水位）→ cut_before_turn
    # → 序列化摘要输入 → 摘要 → append compaction
    # 空摘要视为失败：append 空摘要等于把被压段从视图里抹掉
    # 失败 fail-open：记 context_compact_failed，不 append、不抛出
```

`agent.py` 在 `_step` 发请求前加一段：`build_context → 计量 → maybe_compact → 重新 build_context → 发请求`；压缩分支从认第一条改成认最后一切；`MAX_STEPS` 提为可配置。

工具层：`tools.py` 加分页参数和 4 个新工具（须满足"结论前置"约束）；执行层加 cap 包装和 `BlobStore`（`blobs/<sha256>`，写一次、不可变）；payload 加 blob 元数据注脚；投影和 sanitize 不解引用。`llm.py` 开 `include_usage` 解析尾部 usage。事件 `context_compacted` / `context_compact_failed` 经 `bus.record` 落盘。

## demo（脚本设计，实测输出待回填）

沿用 main.py 的分段惯例，五段全部打真实模型（读仓库根 .env 的 OpenAI 兼容端点，本地 ollama 也行），不用剧本替身——压缩的行为断言（摘要保真、副作用不重做）只有真模型才算数；剧本替身只保留给离线单元测试。demo policy 阈值调小（水位一两千 token、cap 几百字符、保留 3 轮），不用造几十万 token 的会话。

先行落地三段（`stage05_compaction/main.py`，入口 `stage05-demo`，真模型）：压缩前后对照、摘要失败 fail-open、副作用不重做——对应下面第 4 条的 fail-open 半边与第 5 条；其余段落依赖 05 增量（水位计量、分页与批次句柄、cap+blob、二次折叠），落地后回填。

1. **触发策略对照**：同样 N 轮，一个会话含大结果、一个闲聊——纯按轮次前者超限、后者误压，组合策略下两者都正确。
2. **补货任务单**：`list_tasks → get_task 分页 → query_inventory(max_stock) 翻页 → search_rules → 水位到自动压缩 → batch_update_inventory（一次审批）→ 压完继续未处理条目 → 汇报`。断言：已更新条目不被第二次写、截断清单经分页取回、压后投影合法且 token 下降。
3. **cap + blob**：cap 调到比一页小，看头部进消息、全文落 blob、resume 后仍不解引用。
4. **二次折叠 + 失败 fail-open**：两刀后认最后一切、旧摘要被吞；摘要器外面包一层一次性失败注入（空摘要同样算失败），看留痕、不 append、下一边界重试成功。
5. **压缩前后对照**：同一任务端到端，打印压前压后投影、compaction entry、事件和 usage 校准值。

## 与 pi 的对照

|       | pi（coding-agent）                                  | 本章（stage05）                                                   |
| ----- | ------------------------------------------------- | ------------------------------------------------------------- |
| 触发    | `/compact` 手动 + auto-compaction（上下文百分比水位）         | 手动/自动/fallback 同一 `maybe_compact`；按 token 检测、按轮次保留 N 轮        |
| 钩子    | before_compact（可取消/自定义）/ compact / compact_failed | `context_compacted` / `context_compact_failed`（before 钩子留扩展点） |
| 事实记录  | CompactionEntry + firstKeptEntryId                | 同语义（`keep_from_id`）+ policy_version / token 前后 / hash / 摘要模型  |
| 多次压缩  | 新摘要吞旧摘要                                           | 同；投影认最后一切（5a 认第一条的折叠补全）                                       |
| 摘要生成  | 模型生成（branchWithSummary 同款）；输入经 serializeConversation 序列化成文本 | 同；摘要输入文本序列化，tool 结果按视图全量渲染（pi 截 2000 字符，本章由 cap 封顶）；ScriptedSummarizer 保离线确定性，Live 裸 chat 封顶 |
| 大工具结果 | Read 契约分页（offset/limit）；bash 输出 cap + 全文存临时文件     | 业务工具契约分页（翻页/详情/句柄）+ 统一 cap + blob 内容寻址                        |
| 模型选型  | 复用对话模型                                            | 支持独立 `summarizer_model` + `fallback_model`                    |
| 可复现   | 依赖会话文件                                            | 显式不变式 `(trajectory, policy_version) → 唯一 messages`，摘要落盘即事实    |

## 验证（计划，代码落地后回填）

离线测试（条数待回填），分组：

- 计量与触发：usage 锚定加增量估算、水位触发、手动/自动同通道；
- 刀口：落在倒数第 N 个 turn 的第一条 user、进行中 turn 总在保留窗、tool 配对完整、压后满足 target + headroom；
- **messages 组装**：摘要输入是单条 user 消息（序列化文本）、不含对话 system prompt、不含工具定义、tool 调用与结果按视图渲染、压缩指令收尾、blob 不解引用；
- tool 信息：摘要输入含被压轮次的 tool 页、压后被压轮次不进视图、保留窗配对完整、**tool_result 分层视图矩阵逐项验证**；
- **工具规范**：分页工具结论前置（cap 后头部含整体状态/失败原因/后续指引）、违反约束的工具被拒绝接入；
- 工具层：分页返回 total/has_more、cap 后头部加标记进轨迹且 blob hash 可核对、投影（含 resume）不解引用、batch 超条数被契约拒绝、get_batch_result 按句柄分页；
- 折叠与不变式：认最后一切、旧摘要被吞且原文不重读、两次投影逐字节一致、rewind 过两刀（回归 5a）、policy_version/hash 落 entry；
- 失败与兜底：摘要失败 fail-open 加 failed 事件、压缩被 interrupt 不留半截 entry、阶梯每级请求体严格变小；
- 回归：stage04_trajectory 全部既有用例在新包通过。

真模型（先行落地 5 条，`stage05_compaction/tests/test_stage05_compaction.py`；目录按章归位，
实现与 04 共用一份，用例的 import 指向 stage04_trajectory，增量落地后原地翻转到本包）：

- 手动压缩端到端（真模型摘要）：entry 落盘、事件报账、新视图 = [system, <摘要>, 保留窗] 且序列合法；
- 口述事实凭摘要可续：原文不在投影、摘要保住关键信息、回答带得出来；摘要失败（空摘要）fail-open 留痕、下一边界重试成功；
- 副作用不重做：写操作被压掉之后全程恰好一次真实写（设值语义落库正确）；
- resume：压缩视图从盘上还原与关会话前一致（append-only）；
- rewind 回归：branch 回刀口，原文逐字回来、文件一个字节不动。

05 独有增量（水位自动触发、cap+blob、滚动折叠）落地后把对应用例补进同一文件。
demo 五段实测输出回填各小节。

## 下一章预告

能跑了、能重放了、压缩也可复现了，但"改了 prompt、换了模型、重构了 loop，行为有没有变坏"仍然没有答案。Stage 6（rubric 和 eval）接手：重放录制好的会话（事实层 + policy_version），硬断言加 rubric 打分。append-only 这条从 Stage 1 埋下来的线，在那里收口。
