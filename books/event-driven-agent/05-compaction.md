# Stage 5：上下文压缩——有损变换的纪律

> 配套代码（设计稿，代码尚未落地）：计划新增
> `src/baby_event_driven_agent/stages/stage05_compaction/`

## 为什么要做压缩

即便现在的模型已经支持百万级上下文（GPT-astra 已经支持 1.1M），长程任务下照样不够用：agent 连着干几个小时、工具结果一轮轮堆上去，百万 token 也有见底的时候。

经过上一章的处理，咱们的 agent 已经有了完整的 trajectory，这一章我们基于 trajectory 轨迹来处理上下文压缩；这一章，我们就来严肃处理上下文的压缩。

## 核心设计摘要

压缩的范围，先划清楚总边界：动的只是视图，事实层的轨迹 append-only、一个字节不删，被压掉的原文永远躺在轨迹里。
进摘要的只有压缩点前的全部消息，历史的 user message、assistant 消息（含 tool_calls）、tool 结果整段序列化。
不进行压缩的内容：system prompt 不随历史折叠(AGENTS.md,SOULD.md，MCP，TOOLS，SKILLS等等在system prompt区域的都不会压缩)；当前对话最近 N 个 step 原样保留；

核心规则如下: 

**规则一：触发——按 token 检测，按 step 下刀。**
每次 LLM 调用前计量投影出来的 messages，越过压缩水位线就触发。但压缩刀口不是切在 token 超过水位线的那条消息，而是落在倒数第 N 个 step 的起点，保留最近 N 个 step 原文不动，保证 tool 配对完整，刀口之前的整体送去摘要。

>一次用户消息到LLM给出最终答复叫做一个turn，turn里每一次LLM要求调用工具+工具结果返回，是一次step

**规则二：折叠——旧摘要被新摘要吞掉，原文永不重读。**
多次压缩时，新摘要的输入 = 上一刀摘要 + 两刀之间的增量 step。摘要模型永远只看到"上一刀摘要 + 新增内容"，从第一次压缩之后就不再重读任何原始全文。递归折叠，原文不回放。

记住这两条，后面的内容就是它们的展开。

## 压缩机制：两种天然的想法

最容易想到的压缩方式有两种。

一种是按 token 消耗：算上下文占了多少 token，超过窗口的某个比例就压，和窗口限制直接对齐。但是，agent 与 llm 交互，是要严格遵循消息格式的，只判断 token 消耗，有可能压缩时会意外截断消息，导致消息角色不匹配。

另一种则是按步数：数 step 数，超过 N 个 step 就压，token 都不用算。这种方式，虽然确保了消息格式的正确性（step 是消息序列天然合法的最小单位），但是每一次与 LLM 交互的 tool_result 长度都可能是不一样的，有可能某次查询工具的结果返回了超多数据，而后 n 步的工具调用返回的结果却很少，这种机制，不容易找到正确的压缩时机。

两种方案各有问题，实际方案是把它们组合起来。

### 实际方案：按 token 检测，按 step 下刀（两步走）

主流 agent 的做法，都是综合了上述两种方式。

- **第一步——检测（token 计量）**：每次 LLM 调用前计量投影出来的 messages，越过水位线就触发。计量以 API 返回的真实 usage（`prompt_tokens`）为准，落代码校准；调用之间的新增量用字符估算，不引 tokenizer 依赖。
- **第二步——下刀（找 step 边界）**：触发后不是切在 token 位置，而是找到倒数第 N 个 step 的第一条消息，刀口落在这里。这样保证 tool 配对完整，不用再修序列。保留最近 N 个 step（比如 3 步）原文。


### 水位和窗口
"什么时候触发"有两种设法。

- **按预留token数**：pi / Codex CLI 的写法。pi：触发条件 `contextTokens > W − reserveTokens`（pi 默认预留 16k，即 headroom）；保留窗直接按 token 预算（pi `keepRecentTokens` 默认 20k）。直观可控，但换模型要重调数值。

  ```text
  输入 token 用量轴：

   0 ├────────────────────────────────────┼───────────────┤ W
                  usedTokens                reserveTokens
                                            

  复检：tokens(摘要) + tokens(保留窗) + headroom ≤ W − reserveTokens
  headroom：下一次调用的新增输入（用户消息或 tool 结果）+ 回复的 max_tokens + 一次工具往返
  ```

  - **按比例**：以 W 为分母设一条线，换模型、换窗口配置不用动。Gemini CLI 的 `chatCompression.contextPercentageThreshold`（0~1，超过窗口该比例就压）是同款思路；Claude Code 触发线也是比例（有效窗口的 ~85%~92%），但预留是绝对数（20k），算混搭：

    ```text
    输入 token 用量轴：

     0 ├────────────────────────────────────────────┼─────┤ W
                                                H·W
                                            用量到这就触发

    复检：tokens(摘要) + tokens(保留窗) + headroom ≤ H·W
    headroom：下一次调用的新增输入（用户消息或 tool 结果）+ 回复的 max_tokens + 一次工具往返
    ```


触发压缩时，保留最近N个step的消息不压缩，如下（N=3，正要发 turn3 的第 s10 步，s10 还没开始）：
```text
 turn1                    turn2                          turn3（在飞）
      s1      ...   s6          s7       s8       s9            s10（还没发）
 u1   a1 tr   ...   a6    u2    a7 tr    a8 tr    a9    │  u3   a10 tr…
└───────── 送去做摘要 ─────────┘└───────── 原样保留 ──────────┘
                                ↑ 刀口 keep_from = s7 的第一条消息
                                  （step 边界，不必是 turn 起点）

摘要调用输入：摘要 prompt + 上一刀的摘要（若有）+ 刀口之前的全部消息（u1、s1..s6、u2，含图中省略的 step）
压缩后视图：  [system][<摘要>][s7][s8][s9][u3]  ← 下一次调用从这里接着发
```


### 触发位置：

上下文是否超出阈值，检测在 step 边界：上一个 step 完整结算之后、下一次调用之前。此刻下一次调用要发的全部内容都已 append 进轨迹，所有messages的token消耗都可以从轨迹中拿到。

### 多次压缩：滚动折叠

第二次压缩时，视图已经是 [摘要 A] + [后续steps] ，新摘要的输入 = 上一刀的摘要 + 两刀之间的增量 step，不重读所有轨迹：

```text
原始轨迹：s1 s2 s3 s4 s5 s6 s7 s8 s9 s10
第一次压缩 compA（keep_from=s7）：s1..s6 由摘要 A 代替
  视图 A：[<摘要 A>, s7, s8, s9, s10]
  轨迹：  s1..s10, compA(keep_from=s7)

继续跑到 s14，再次触发，新刀口 keep_from=s12：
  摘要 B 的输入 = 摘要 A 文本 + s7..s11
                 （当前视图里、新刀口之前的全部内容）
  视图 B：[<摘要 B>, s12, s13, s14]
  轨迹：  s1..s10, compA, s11..s14, compB(keep_from=s12)
          投影只认最后一刀 compB：它之前的全部 entry（含 compA
          节点）跳过，摘要 B 插视图最前
```

## 摘要调用：选型与组装

### 压缩模型选型

压缩 LLM 是"只读压缩"角色，和对话 LLM 的能力需求完全不同：

| 维度 | 对话模型 | 压缩/摘要模型 |
|---|---|---|
| 需要工具调用 | 是 | 否（裸 chat） |
| 需要推理/规划 | 强 | 否（只压缩不推理） |
| 上下文窗口 | 越大越好 | 必须 ≥ 被压段原文 + prompt + 输出余量 |
| 输出精度 | 高 | 中（摘要编错有代价但可回查） |
| 调用频率 | 每个 step | 仅触发时 |

因此**压缩模型可以和对话模型不同**，选型优先级：**窗口容量 > 摘要忠实度 > 成本 > 延迟**。压缩模型不需要对话模型那样强的推理能力。

具体策略：

- **默认（小/中型会话）**：复用对话模型。简单、摘要风格一致、不引入额外依赖。
- **长程/生产会话**：用小一号但窗口足够的模型（如对话用大模型，压缩用同系列小模型）。成本能差一个数量级。
- **极端场景**：被压段本身很大，主模型窗口装不下 → 降级到窗口最大的可用模型，或走分块 map-reduce，不过压缩质量就最差。

配置上在 `CompactionPolicy` 补两个字段：

```python
summarizer_model: str | None = None  # None = 复用对话模型
fallback_model: str | None = None   # 主选装不下时降级
```

### 压缩调用的 messages 组装策略

压缩 LLM 的输入不是直接把对话视图切片扔过去，把待压缩的messages作为压缩模型的user message的content输入

本章做的就是这个：被压段序列化成**一段文本**，放进单条 user 消息的 content。
>如果不拼接成单条user message的content，直接用 压缩system prompt + 原始对话按 step 逐条发送给LLM压缩，LLM很有可能会把压缩的意图理解成对话的意图。

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

system 放压缩专用指令，如下是压缩时user message的结构，待压缩的对话放在 <conversation> 中间，如果已经压过一刀，将上一刀的摘要放到<previous-summary>中间。

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


## 其他影响上下文的一些策略

除了通过LLM对messages做整体摘要外，还有一些控制message长度的处理方式，避免单条message占用过多的context；比如，

### 输入摘要
针对用户的超长输入先做摘要，或者将超长输入保存为文档，再让LLM阅读，这样在user message就不需要直接写入超长输入的所有内容；

### 超大的tool_result 的处理机制
超大tool_result的处理，
一般情况下，agent发给LLM的messages，占用空间最多恐怕是tool_result，针对检索类型的工具，一般都需要设计一个执行工具结果返回的上限(2000~5000字符)，而后将完整内容记录磁盘索引，比如fetch到的超多内容，只返回tool_result窗口允许的长度，其他写入/tmp，然后在tool_result同时返回文件位置，有需要时LLM自行发起read，而纯粹的read工具，一般则不单独设置上限，以防止读到的信息不完整。

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

### 其他直接控制点（输入 / 输出 / tool_result）

- **粘贴落盘**（输入侧）：用户贴进对话的大段文本/代码 → 写成文件，消息里只留引用——blob 不只是 tool_result 的归宿，输入侧同样适用；
- **图片降采样**（输入侧）：粘贴的图片进消息前缩放/压缩，多模态内容占 token 大头且无法文本摘要；
- **thinking 块清除**（输出侧）：reasoning 块是输出侧的 token 大头，Claude Code 在距上次调用超过缓存 TTL 后清除历史 thinking，同样的"等缓存死透再动手"逻辑；
- **小模型转写**（tool_result 侧）：WebFetch 类工具不让原文进消息——抓取后先按 prompt 用小模型定向提取，进对话的已经是答案。比 cap 更进一步：原文根本不进来。


## 代码改动（设计稿，待实现）

新包 `stage05_compaction/` 从 `stage04_trajectory/` 拷贝，`transport/`、`session/` 不动，增量如下。

`session/compaction.py` 已有手动档（Summarizer 两档 / maybe_compact，见 04），本章在同一个文件上扩展。刀口粒度从 turn 换成 step，04 的 `cut_before_turn(entries, keep_turns)` 随之改名为 `cut_before_step(entries, keep_steps)`：

```python
@dataclass(frozen=True)
class CompactionPolicy:
    version: str = "2026-09-25.v1"
    mode: str = "ratio"           # "ratio" 比例式（本书默认）| "reserve" 预留式（pi 同款）
    window_tokens: int = 0        # 模型窗口 W
    # ratio 模式读取：
    watermark: float = 0.7        # 触发线 = 复检线 = H·W
    # reserve 模式读取：
    reserve_tokens: int | None = None  # 触发 = W − reserve；压后预算同这条线，超了就再压
    keep_tokens: int | None = None     # 保留窗 token 预算
    # 两种模式共用：
    headroom_tokens: int = 0      # 下一次调用的新增输入 + 回复 + 一次工具往返
    keep_steps: int = 3           # 保留窗 step 数上界（护栏，与 keep_tokens 取更紧）
    summarizer_model: str | None = None   # None = 复用对话模型
    fallback_model: str | None = None     # 主选装不下时降级

def estimate_tokens(messages: list[dict]) -> int: ...       # 真实 usage 锚定 + 增量字符估算
def cut_before_step(entries, keep_steps: int, keep_tokens: int | None = None) -> str: ...
    # 刀口 = 从尾部往前收 step，step 数或 token 预算任一用尽即停（双约束取更紧）
    # 在飞的 step 还没落盘，天然不在被压段

class Summarizer(Protocol):
    async def summarize(self, segment: list[dict], previous: str | None) -> str: ...
    # ScriptedSummarizer（离线确定性）/ LiveSummarizer（裸 chat、不带工具、max_tokens 封顶）

def serialize_segment(segment, previous_summary, policy) -> dict:
    # 序列化管线：剥离 system / 剥离工具定义 / 逐条按视图渲染成文本
    #           （tool_calls 带参数、tool 结果分页/cap 后原样）→ <conversation> 包裹
    #           → <previous-summary> 前置 → 压缩指令收尾；返回单条 user 消息
    #           （LiveSummarizer 的渲染逻辑上提为这条公共管线）

async def maybe_compact(traj, policy, summarizer, *, reason="watermark") -> Entry | None:
    # 投影 → 计量 → 未越水位返回 None（手动调用跳过水位）→ cut_before_step
    # → 序列化摘要输入 → 摘要 → append compaction
    # 空摘要视为失败：append 空摘要等于把被压段从视图里抹掉
    # 失败 fail-open：记 context_compact_failed，不 append、不抛出
```

`agent.py` 的检测挂在 `_run_steps` 循环顶部（step 边界）：`查 interrupt → 计量（锚点 + 轨迹增量）→ 越线则 maybe_compact → drain steering → build_context → 发请求`——计量依据全在轨迹层，不需要投影，压完直接 build 一次即得新视图，不做"build → 压 → 重新 build"的无用功；压缩分支从认第一条改成认最后一切；`MAX_STEPS` 提为可配置。

工具层：`tools.py` 加分页参数和 4 个新工具（须满足"结论前置"约束）；执行层加 cap 包装和 `BlobStore`（`blobs/<sha256>`，写一次、不可变）；payload 加 blob 元数据注脚；投影和 sanitize 不解引用。`llm.py` 开 `include_usage` 解析尾部 usage。事件 `context_compacted` / `context_compact_failed` 经 `bus.record` 落盘。

## demo（脚本设计，实测输出待回填）

沿用 main.py 的分段惯例，五段全部打真实模型（读仓库根 .env 的 OpenAI 兼容端点，本地 ollama 也行），不用剧本替身——压缩的行为断言（摘要保真、副作用不重做）只有真模型才算数；剧本替身只保留给离线单元测试。demo policy 阈值调小（水位一两千 token、cap 几百字符、保留 3 个 step），不用造几十万 token 的会话。

先行落地三段（`stage05_compaction/main.py`，入口 `stage05-demo`，真模型）：压缩前后对照、摘要失败 fail-open、副作用不重做——对应下面第 4 条的 fail-open 半边与第 5 条；其余段落依赖 05 增量（水位计量、分页与批次句柄、cap+blob、二次折叠），落地后回填。

1. **触发策略对照**：同样 N 个 step，一个会话含大结果、一个闲聊——纯按步数前者超限、后者误压，组合策略下两者都正确。
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
| 工单域 | `list_tasks(status?)`、`get_task(task_id, offset, limit)` | 工单条目分页，多步任务的驱动器 |
| 批次句柄 | `batch_update_inventory(items[])`（每批条数有上限，一次审批，返回 `result_id`）、`get_batch_result(result_id, offset, limit)` | 写操作不可重放，尾部按句柄查，不能让模型再调一次拿结果 |

即 4 个现有工具（查询类加分页）加 4 个新工具。


离线测试（条数待回填），分组：

- 计量与触发：usage 锚定加增量估算、水位触发、手动/自动同通道；
- 刀口：落在倒数第 N 个 step 的第一条消息、在飞的 step 总不在被压段、tool 配对完整、复检判据（摘要+保留窗+headroom ≤ 触发线）不达标时收缩保留窗；
- **messages 组装**：摘要输入是单条 user 消息（序列化文本）、不含对话 system prompt、不含工具定义、tool 调用与结果按视图渲染、压缩指令收尾、blob 不解引用；
- tool 信息：摘要输入含被压 step 的 tool 页、压后被压 step 不进视图、保留窗配对完整、**tool_result 分层视图矩阵逐项验证**；
- **工具规范**：分页工具结论前置（cap 后头部含整体状态/失败原因/后续指引）、违反约束的工具被拒绝接入；
- 工具层：分页返回 total/has_more、cap 后头部加标记进轨迹且 blob hash 可核对、投影（含 resume）不解引用、batch 超条数被契约拒绝、get_batch_result 按句柄分页；
- 折叠与不变式：认最后一切、旧摘要被吞且原文不重读、两次投影逐字节一致、rewind 过两刀（回归 5a）、policy_version/hash 落 entry；
- 失败处理：摘要失败 fail-open 加 failed 事件、压缩被 interrupt 不留半截 entry；
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
