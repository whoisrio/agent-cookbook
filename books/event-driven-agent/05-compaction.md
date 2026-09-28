# Stage 5：上下文压缩——有损变换的纪律

> 配套代码（设计稿，代码尚未落地）：计划新增
> `src/baby_event_driven_agent/stages/stage05_compaction/`

## 为什么要做压缩

即便现在的模型已经支持百万级上下文（GPT-astra 已经支持 1.1M），长程任务下照样不够用：agent 连着干几个小时、工具结果一轮轮堆上去，百万 token 也有见底的时候。

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

H(压缩阈值)（如 0.7）不能设太满：压缩自己也是一次模型调用，要读被压段原文、要留写摘要的输出余量（见"压缩救不了自己"）。N 由 T（如 0.4）反推。N 轮的 token 量有上界，因为单轮工具结果有分页和 cap 兜着（见后）。

触发那一刻哪些消息送去摘要（N=3，对话走到 t7，t7 还在进行中）：

```text
 t1       t2        t3       t4         │ t5       t6        t7（进行中）
 u1 a1    u2 a2 t2  u3 a3    u4 a4 t4   │ u5 a5    u6 a6 t6   u7 a7…
└───────────── 送去做摘要 ────────────┘   └──────── 原样保留 ────────┘
                                        ↑ 刀口 keep_from = u5
                                          （t5 的第一条消息，turn 边界）

摘要调用输入：摘要 prompt + 上一刀的摘要（若有）+ t1..t4 的全部消息
压缩后视图：  [system][<摘要>][t5][t6][t7…]
```

- 刀口之前不删除，只是这一次的视图里由摘要替代，原文还在轨迹里；
- t7 走到一半也在保留窗里：进行中的 turn 不能切，切了 tool 配对就不完整；
- 已经压过一次时，更早的轮次不在视图里，送去摘要的是上一刀的摘要加两刀之间的轮次（见"滚动折叠"）。

### 触发位置：第四种边界动作

上下文是否超出阈值，检测在 `build_context` 之后、正式调用LLM之前，如果超出，立刻执行压缩：

```text
followup,steering  = 等 step 边界，把消息拼进当前上下文，不打断在跑的（Stage 2）
interrupt = 掐掉在跑的 step，turn 结束（Stage 3）
compact   = step 边界处换一副更短的视图，再发下一次调用
```

压缩调用本身也是一次在跑的 LLM 调用：被 interrupt 掐断就不 append（Stage 3 纪律，append 只在摘要成功结算后），压缩期间到的 steering 等下一个 drain 点。


### 多次压缩：滚动折叠

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


## 摘要调用：选型与组装

### 压缩模型选型

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
- **极端场景**：被压段本身很大，主模型窗口装不下 → 降级到窗口最大的可用模型，或走分块 map-reduce，不过压缩质量就就最差。

配置上在 `CompactionPolicy` 补两个字段：

```python
summarizer_model: str | None = None  # None = 复用对话模型
fallback_model: str | None = None   # 主选装不下时降级
```

### 压缩调用的 messages 组装策略

压缩 LLM 的输入不是直接把对话视图切片扔过去，而是经过一层转换。
核心原则：**压缩 LLM 的输入严格等于对话 LLM 当时的视野（信息等价，形态可以不同），不展开 blob、不补后续页、不做任何增强。压缩 LLM 的信息量 ≤ 对话 LLM 当时的信息量，不多不少。**

**形态：序列化成一段文本，不按对话轮次丢。**

本章做的就是这个：被压段序列化成**一段文本**，放进单条 user 消息的 content。
>如果不拼接成单条user message的content，直接用 压缩system prompt + 原始对话轮次发送给LLM压缩，LLM很有可能会把压缩的意图理解成对话的意图。

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

**1.压缩 System prompt**

摘要 LLM 用专门的压缩指令示例：

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

不同场景的agent，压缩时的prompt略有不同，根据实际场景要求压缩时的 MUST 和 NEVER，比如coding agent，压缩时，会要求保留架构决策之类的。
>这些要求，也可以写在AGENTS.md里，压缩时，同样会加载。实际压缩的效果，那就是压缩模型本身的能力决定的了。

**2. 指令位置：system 定角色，user 文本末尾定任务**

system 放压缩专用指令，如下是压缩时user message的结构，待压缩的对话放在 <conversation> 中间，如果有多轮压缩，将上一轮的压缩信息放到<previous-summary>中间。

```python
COMPRESSION_USER = """
<conversation>
{conversation}
</conversation>

<previous-summary>
{previous_summary}
</previous-summary>

输出摘要：把 <conversation> 里的新内容并入 <previous-summary>，只输出合并后的完整摘要。
```


### 压缩时的异常兜底

压缩调用自己也要装进**摘要模型**的窗口：摘要输入 = 摘要 prompt + 被压段 S + 摘要输出余量，装不装得下由摘要模型的窗口决定。一般摘要模型就是正式模型，窗口同为 W——所以水位判断天然已把压缩调用算在内：70% 触发时 S ≤ 0.7W，摘要调用装得下。异常只有两个例外：

- **触发太晚**：S ≈ 整段历史 ≈ W，摘要调用装不下 → 滚动折叠，早触发（摘要输入 = 旧摘要 + 新增段，天然 < W）；
- **摘要模型更小**（`summarizer_model` 配了小窗口）：水位和兜底判断都按摘要模型的窗口算，不能照搬 W。

死局只剩一个：等 S ≈ W 才压——不压正式调用 400，压摘要调用也 400，原样重试就是死循环。单条消息本身超窗压缩救不了（摘要它得先发原文，删了 tool_calls 悬空），只能工具层消灭：分页加 cap。

兜底阶梯，每级请求严格变小：

1. 水位主动压缩；
2. 400 后激进压缩：保留轮次调小，先保住当前 turn；
3. 被压段仍超窗：分块 map-reduce，必要时换 `fallback_model`；
4. 单条超大消息：投影换占位/指针（不改事实层），或分页/句柄重取；
5. 都不行：告知用户，或带交接摘要开新会话。


## 其他影响上下文的一些策略

### 超大的tool_result 的处理机制

一般情况下，agent发给LLM的messages，占用空间最多恐怕是tool_result，针对检索类型的工具，一般都需要设计一个写入tool_result的上限(2000~5000字符)，而后将完整内容记录磁盘索引，比如fetch到的超多内容，只返回tool_result窗口允许的长度，其他写入/tmp，然后在tool_result同时返回文件位置，有需要时LLM自行发起read，而纯粹的read工具，一般则不单独设置上限，以防止读到的信息不完整。

针对read工具，也有一些agent设计了分页读取的机制，默认先读取tool_result窗口允许的内容，由LLM决定是否读取下一页的内容。

在一轮对话里，LLM的最终答复里，实际都涵盖了tool_result的内容，比如你要求agent帮你分析某个超大的上帝类，模型读取类的代码之后，可能还会调用glob，grep之类的去检索引用该类的代码，然后给你最终答复。

```text
user : 帮我分析这个上帝类
assistant(tool_call): read
tool : '...超级上帝类.. ' <- 2000字符
assistant(tool_call): glob | grep
tool : '...检索结果'
assistant: 如下是xxx类的分析 ...
```
这种tool_result，如果一直在messages里，也是比较消耗上下文，对上下文压缩时的帮助其实也不大，因为这类tools返回的信息，在LLM给出答复的时候，已经总了整理归纳。

针对这类场景，我们看看claude是如何处理的，
#### 冷热分层：Claude Code 的 tool_result 清除（机制介绍，本书不实现）

长度限制和分页都发生在结果**进入消息之前**。Claude Code 在此之上还有一组会话中途的清除机制（microcompact），按 prompt cache 的冷热分两条路：

**冷路（缓存已死）：Time-Based MicroCompact。** 距上次 API 调用超过 60 分钟（≥ prompt cache 最长 TTL 1h），缓存前缀反正注定全量重写，此时直接把旧的 tool_result（保留最近 5 个）替换成 `[Old tool result content cleared]`——无 LLM 参与、轨迹不变、只瘦视图。触发条件本身就是保护条件：等缓存死透再改历史，不烧任何活缓存。

**热路（缓存还活着）：本地消息不动，让服务端删。** 两条实现：
`cache_edits`（Cached MicroCompact）：随请求附一块声明式删除，服务端在已缓存的前缀里摘掉指定 tool_result，本地消息一字节不动；
通过服务端的配合以达到精简上下文，同时保证prompt cache的命中。
>可以做tool_result结果替换的tool，一般也会做白名单控制。

如上两种机制，对于已经挂掉的缓存，通用的agent都可以支持，而对于prompt cache仍然处于生效阶段的这类动态替换，则需要服务端的配合；
现在各家provider也提供了cache_control的功能来主动管理缓存，不过主动设置的缓存的价格是缓存未命中价格的1.2倍，且有数量限制，所以如上的热路策略，并不是通用方案。

### turn 级摘要：单轮过大的中间层

一个 step 里用户输入过长，或者工具调用爆炸，一轮就吃掉大半窗口，那么也值得在直接对单次调用直接做压缩

### 其他直接控制点（输入 / 输出 / tool_result）

- **粘贴落盘**（输入侧）：用户贴进对话的大段文本/代码 → 写成文件，消息里只留引用——blob 不只是 tool_result 的归宿，输入侧同样适用；
- **图片降采样**（输入侧）：粘贴的图片进消息前缩放/压缩，多模态内容占 token 大头且无法文本摘要；
- **thinking 块清除**（输出侧）：reasoning 块是输出侧的 token 大头，Claude Code 在距上次调用超过缓存 TTL 后清除历史 thinking，同样的"等缓存死透再动手"逻辑；
- **小模型转写**（tool_result 侧）：WebFetch 类工具不让原文进消息——抓取后先按 prompt 用小模型定向提取，进对话的已经是答案。比 cap 更进一步：原文根本不进来。


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


## 验证（计划，代码落地后回填）


### 工具集扩充

现有四件套撑不出长会话，也没有大结果。5b 在业务世界内加工具，不引入文件系统语义（read_file 是 coding agent 的形态，不贴合运营 agent）：

| 形态 | 工具 | 剩余部分怎么取 |
| --- | --- | --- |
| 列表分页 | `query_inventory(category?, max_stock?, offset, limit)`、`search_rules(query, offset, limit)` | 同参数翻页，返回 `total/has_more` |
| 单条详情 | `get_inventory_detail(category)` | 列表只给摘要行，完整规格按 key 取 |
| 工单域 | `list_tasks(status?)`、`get_task(task_id, offset, limit)` | 工单条目分页，多轮任务的驱动器 |
| 批次句柄 | `batch_update_inventory(items[])`（每批条数有上限，一次审批，返回 `result_id`）、`get_batch_result(result_id, offset, limit)` | 写操作不可重放，尾部按句柄查，不能让模型再调一次拿结果 |

即 4 个现有工具（查询类加分页）加 4 个新工具。


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
