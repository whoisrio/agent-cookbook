# Stage 5：上下文压缩——有损变换的纪律

> 配套代码已落地：实现与 04 共用一份（`stage04_trajectory/` 的
> `session/compaction.py`、`agent.py`、`llm.py`），用例归位
> `src/baby_event_driven_agent/stages/stage05_compaction/tests/`。

## 为什么要做压缩

即便现在的模型已经支持百万级上下文（GPT-astra 已经支持 1.1M），长程任务下照样不够用：agent 连着干几个小时、工具结果一轮轮堆上去，百万 token 也有见底的时候。

经过上一章的处理，咱们的 agent 已经有了完整的 trajectory，这一章我们基于 trajectory 轨迹来处理上下文压缩；这一章，我们就来严肃处理上下文的压缩。

## 核心设计摘要

压缩的范围，先划清楚总边界：动的只是视图，事实层的轨迹 append-only、一个字节不删，被压掉的原文永远躺在轨迹里。
进摘要的只有压缩点前的全部消息，历史的 user message、assistant 消息（含 tool_calls）、tool 结果整段序列化。
不进行压缩的内容：system prompt 不随历史折叠(AGENTS.md,SOULD.md，MCP，TOOLS，SKILLS等等在system prompt区域的都不会压缩)；当前对话最近 N 个 step 原样保留；

核心规则如下: 

**规则一：触发——按 token 检测，按 step 切。**
每次 LLM 调用前计量投影出来的 messages，越过压缩水位线就触发。但压缩切点不是切在 token 超过水位线的那条消息，而是落在倒数第 N 个 step 的起点，保留最近 N 个 step 原文不动，保证 tool 配对完整，切点之前的整体送去摘要。

>一次用户消息到LLM给出最终答复叫做一个turn，turn里每一次LLM要求调用工具+工具结果返回，是一次step

**规则二：折叠——旧摘要被新摘要吞掉，原文永不重读。**
多次压缩时，新摘要的输入 = 上一次压缩的摘要 + 两次压缩之间的增量 step。摘要模型永远只看到"上一次压缩的摘要 + 新增内容"，从第一次压缩之后就不再重读任何原始全文。递归折叠，原文不回放。

记住这两条，后面的内容就是它们的展开。

## 压缩机制：两种天然的想法

最容易想到的压缩方式有两种。

一种是按 token 消耗：算上下文占了多少 token，超过窗口的某个比例就压，和窗口限制直接对齐。但是，agent 与 llm 交互，是要严格遵循消息格式的，只判断 token 消耗，有可能压缩时会意外截断消息，导致消息角色不匹配。

另一种则是按步数：数 step 数，超过 N 个 step 就压，token 都不用算。这种方式，虽然确保了消息格式的正确性（step 是消息序列天然合法的最小单位），但是每一次与 LLM 交互的 tool_result 长度都可能是不一样的，有可能某次查询工具的结果返回了超多数据，而后 n 步的工具调用返回的结果却很少，这种机制，不容易找到正确的压缩时机。

两种方案各有问题，实际方案是把它们组合起来。

### 实际方案：按 token 检测，按 step 切（两步走）

主流 agent 的做法，都是综合了上述两种方式。

- **第一步——检测（token 计量）**：每次 LLM 调用前计量投影出来的 messages，越过水位线就触发。计量以 API 返回的真实 usage（`prompt_tokens`）为准，落代码校准；调用之间的新增量用字符估算，不引 tokenizer 依赖。
- **第二步——定切点（找 step 边界）**：触发后不是切在 token 位置，而是找到倒数第 N 个 step 的第一条消息，切点落在这里。这样保证 tool 配对完整，不用再修序列。保留最近 N 个 step（比如 3 步）原文。


### 水位和窗口
"什么时候触发"有两种设法。

- **按预留token数**，设置reserveTokens作为上下文窗口空闲阈值，当 usedTokens > contextwindow - reserveTokens时，即触发压缩
>pi / Codex CLI 采取的就是这种策略。pi：触发条件 `contextTokens > W − reserveTokens`（pi 的reserveToknes默认设置为 16k）

  ```text
  输入 token 用量轴：

   0 ├────────────────────────────────────┼───────────────┤ W
                  usedTokens                reserveTokens
  ```

  - **按比例**：以 W 为分母设一条线，换模型、换窗口配置不用动。Gemini CLI 的 `chatCompression.contextPercentageThreshold`（0~1，超过窗口该比例就压）是同款思路；Claude Code 触发线也是比例（有效窗口的 ~85%~92%），但预留是绝对数（20k），算混搭：

    ```text
    输入 token 用量轴：

     0 ├────────────────────────────────────────────┼─────┤ W
                                                H·W
                                            用量到这就触发

    复检：tokens(摘要) + tokens(保留窗) ≤ H·W
    不达标就收缩保留窗再摘要
    ```


触发压缩时，保留最近N个step的消息不压缩，如下（N=3，正要发 turn3 的第 s10 步，s10 还没开始）：
```text
 turn1                    turn2                          turn3（在飞）
      s1      ...   s6          s7       s8       s9            s10（还没发）
 u1   a1 tr   ...   a6    u2    a7 tr    a8 tr    a9    │  u3   a10 tr…
└───────── 送去做摘要 ─────────┘└───────── 原样保留 ──────────┘
                                ↑ 切点 keep_from = s7 的第一条消息
                                  （step 边界，不必是 turn 起点）

摘要调用输入：摘要 prompt + 上一次压缩的摘要（若有）+ 切点之前的全部消息（u1、s1..s6、u2，含图中省略的 step）
压缩后视图：  [system][<摘要>][s7][s8][s9][u3]  ← 下一次调用从这里接着发
```
>pi 在触发压缩时则是保留keepRecentTokens数量的tokens不压缩，根据keepRecentTokens来检查可以留下最近多少个step不进压缩窗口
>咱们的baby agent直接取最近n轮


### 触发位置：

上下文是否超出阈值，检测在 step 边界：上一个 step 完整结算之后、下一次调用之前。此刻下一次调用要发的全部内容都已 append 进轨迹，所有messages的token消耗都可以从轨迹中拿到。

### 多次压缩：滚动折叠

第二次压缩时，视图已经是 [摘要 A] + [后续steps] ，新摘要的输入 = 上一次压缩的摘要 + 两次压缩之间的增量 step，不重读所有轨迹：

```text
原始轨迹：s1 s2 s3 s4 s5 s6 s7 s8 s9 s10
第一次压缩 compA（keep_from=s7）：s1..s6 由摘要 A 代替
  视图 A：[<摘要 A>, s7, s8, s9, s10]
  轨迹：  s1..s10, compA(keep_from=s7)

继续跑到 s14，再次触发，新切点 keep_from=s12：
  摘要 B 的输入 = 摘要 A 文本 + s7..s11
                 （当前视图里、新切点之前的全部内容）
  视图 B：[<摘要 B>, s12, s13, s14]
  轨迹：  s1..s10, compA, s11..s14, compB(keep_from=s12)
          投影只认最后一次压缩 compB：它之前的全部 entry（含 compA
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

配置上可以在 `CompactionPolicy` 补两个字段。这是选型讨论，**本书未实现**——当前
`LiveSummarizer` 固定复用对话模型，降级策略留作展望：

```python
summarizer_model: str | None = None  # None = 复用对话模型
fallback_model: str | None = None   # 主选装不下时降级
```

### 压缩调用的 messages 组装策略

压缩 LLM 的输入不是直接把对话视图切片扔过去，把待压缩的messages作为压缩模型的user message的content输入
>如果不拼接成单条user message的content，直接用 压缩system prompt + 原始对话按 step 逐条发送给LLM压缩，LLM很有可能会把压缩的意图理解成对话的意图。

**1.压缩 System prompt**

摘要 LLM 用专门的压缩指令示例，需要给出明确的压缩规则：

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

system 放压缩专用指令，如下是压缩时user message的结构，待压缩的对话放在 <conversation> 中间，如果已经压缩过，将上一次压缩的摘要放到<previous-summary>中间。

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


## 代码改动

实现与 04 共用一份：`transport/`、`session/` 不拆包，本章增量直接落在 `stage04_trajectory/` 的 `session/compaction.py`、`agent.py`、`llm.py` 上；用例归位 `stage05_compaction/tests/`，import 暂指向 `stage04_trajectory`，后续如有 05 专属的结构性增量再原地翻转到本包。

`session/compaction.py` 已有手动档（Summarizer 两档 / maybe_compact，见 04），本章在同一个文件上扩展。切点粒度从 turn 换成 step：新增 `cut_before_step(entries, keep_steps, keep_tokens)`，04 的 `cut_before_turn(entries, keep_turns)` 保留——路径上没有任何 step（纯聊天）时由 `cut_before_step` 退回 turn 刀口：

```python
@dataclass(frozen=True)
class CompactionPolicy:
    version: str = "2026-09-25.v1"
    mode: str = "ratio"           # "ratio" 比例式（本书默认）| "reserve" 预留式（pi 同款）
    window_tokens: int = 0        # 模型窗口 W；0 = 计量不可用，水位不触发（手动压缩不受影响）
    # ratio 模式读取：
    watermark: float = 0.7        # 触发线 = 复检线 = H·W
    # reserve 模式读取：
    reserve_tokens: int | None = None  # 触发 = W − reserve；压后预算同这条线，超了就再压
    # 两种模式共用：
    keep_tokens: int | None = None     # 保留窗 token 预算
    keep_steps: int = 3                # 保留窗 step 数上界（护栏，与 keep_tokens 取更紧）

def estimate_tokens(messages: list[dict]) -> int: ...       # 字符估算（CJK 按字计、其余 4 字符/token）
class TokenMeter: ...
    # 真实 usage 锚定（anchor 落在发起调用时的轨迹 leaf）+ 之后的轨迹增量估算；
    # 锚点被 rewind 掉（不在当前路径上）就回退全量估算

def cut_before_step(entries, keep_steps: int, keep_tokens: int | None = None) -> str: ...
    # 切点 = 从尾部往前收 step，step 数或 token 预算任一用尽即停（双约束取更紧）
    # 在飞的 step 还没落盘，天然不在被压段

class Summarizer(Protocol):
    async def summarize(self, segment: list[dict], previous: str | None) -> str: ...
    # ScriptedSummarizer（离线确定性）/ LiveSummarizer（裸 chat、不带工具、max_tokens 封顶）

def serialize_segment(segment, previous_summary, policy) -> dict:
    # 序列化管线：逐条按视图渲染成文本（tool_calls 带参数、结果原样、system 不进）
    #           → <conversation> 包裹 → <previous-summary> 前置（若有）→ 压缩指令收尾
    #           返回单条 user 消息；LiveSummarizer 与离线测试共用这条管线

async def maybe_compact(traj, policy, summarizer, *, reason="watermark") -> Entry | None:
    # 投影 → 计量 → 未越水位返回 None（手动调用跳过水位）→ cut_before_step
    # → 序列化摘要输入 → 摘要 → 复检（摘要+保留窗 ≤ 触发线，否则收缩保留窗重压，
    #   收到 1 个 step 仍超线返回 None，不硬压）→ append compaction
    # 空摘要视为失败：append 空摘要等于把被压段从视图里抹掉
    # 失败 fail-open：记 context_compact_failed，不 append、不挡 turn
```

`agent.py` 的检测挂在 `_run_steps` 循环顶部（step 边界）：`查 interrupt → 计量（锚点 + 轨迹增量）→ 越线则 maybe_compact → drain steering → build_context → 发请求`——计量依据全在轨迹层，不需要投影，压完直接 build 一次即得新视图，不做"build → 压 → 重新 build"的无用功；同一 leaf 上只尝试一次（复检不过的会话每个边界都越线，没有新 entry 就不重复白打摘要调用）；压缩分支从认第一条改成认最后一次压缩；`MAX_STEPS` 提为可配置。

工具层不动：3c 的六件套原样沿用。分页契约、批次句柄、cap + blob（含 `BlobStore` 与工具的"结论前置"约束）**本书未实现**，留作展望。`llm.py` 开 `include_usage` 解析尾部 usage，作为计量的真实锚点。事件 `context_compacted` / `context_compact_failed` 经总线落盘。

## demo（`stage05_compaction/main.py`，入口 `stage05-demo`）

沿用 main.py 的分段惯例，五段全部打真实模型（读仓库根 .env 的 OpenAI 兼容端点，本地 ollama 也行），不用剧本替身——压缩的行为断言（摘要保真、副作用不重做）只有真模型才算数；剧本替身只保留给离线单元测试。
demo policy 阈值调小（水位一两千 token、保留 1~2 个 step），不用造几十万 token 的会话。
下面按"压缩前后对照 → 失败兜底 → 副作用不重做 → 触发策略 → 二次折叠"的顺序，把压缩的每条纪律过一遍。
entry id 每次运行随机生成，模型输出每次也会不同，下面的引用是一次真实运行；真模型有波动，个别段要靠重跑取到干净的现场。

### demo 1 · 压缩前后对照（01-compact-before-after）

先说这个 case 里 agent 在干什么：**用户连问三轮库存（保温杯 → 玻璃杯 → 马克杯），agent 每轮发起工具调用、把上下文堆到 13 条消息；用户喊压一下，下一个 step 边界把切点之前的历史压成一份摘要**——追加一条 compaction entry（摘要全文 + 切点 keep_from_id），原文一个字节不删。
压缩了哪些，四样东西摆在一起看：compaction entry、context_compacted 事件报账、压前压后投影对比、轨迹全树。

```text
[用户] 保温杯还有库存吗 / 玻璃杯呢 / 马克杯还有吗
[用户] 汇总一下三个品类的情况（发出 compact_request，下一轮 step 边界生效）
[实测] 压缩前投影：13 条消息（system×1、user×3、assistant×6、tool×3）
[entry] 6c22a2c5  reason=manual  keep_from=5fd7c60f
       摘要全文：
       已完成
       关键事实与数据
       保温杯（316L 不锈钢内胆，500ml，磨砂黑）：库存 3 件
       玻璃杯（高硼硅玻璃，400ml，可微波炉用）：库存 17 件
       待办
       查询马克杯库存
       副作用
       无
[实测] context_compacted：消息 14 → 6 条
[实测] 压缩后投影：10 条消息（system×1、user×2、assistant×4、tool×3）
[实测] 新视图头部：system | <summary>已完成 关键事实与数据 保温杯（316L 不锈钢内胆，500ml，磨砂黑）…
       说明 │ 全树 19 条 entry 原样都在——压缩只换视图，不动事实层。
             摘要插在 system 之后、保留窗之前；刀口之前的消息由摘要替代。
```

### demo 2 · 摘要失败 fail-open（02-compact-fail-open）

先说这个 case 里 agent 在干什么：**摘要器第一次被注入空返回（推理模型思考 token 吃掉输出时的真实姿态），第二次放行真模型**——空摘要比长摘要危害大：append 空摘要等于把被压段从视图里抹掉，所以按失败处理：context_compact_failed 留痕、不 append、不挡 turn；重发请求，下一边界重试成功。

```text
[用户] 保温杯还有库存吗 / 好的
[实测] 压缩前投影：7 条消息（system×1、user×2、assistant×3、tool×1）
[用户] 继续（第一次压缩：摘要器被注入空返回）
[实测] 失败留痕：context_compact_failed × 1
       错误：摘要器返回空摘要（推理模型思考 token 吃掉输出时会发生）
[实测] 失败后：compaction entry 无（不 append）；投影 9 条消息（system×1、user×3、assistant×4、tool×1）
[用户] 继续（重发：下一边界重试）
[实测] 重试成功：entry 0f668654；context_compacted × 1
       说明 │ 失败与成功都留痕在 EventLog；turn 都没有被挡住——
             压缩救不了自己，但也不拖垮会话。
```

### demo 3 · 副作用不重做（03-side-effect-not-repeated）

先说这个 case 里 agent 在干什么：**用户先把保温杯库存改成 10 件（免审批量），再闲聊两轮把这个写操作推进被压段；压缩之后让模型汇报"今天改了哪些库存、还需要再改吗"**——模型凭摘要里的"已完成"段知道"做过就是做过"，全程恰好一次真实 update_inventory，落库值正确（设值语义）。

```text
[用户] 把保温杯的库存改成 10 件 / 玻璃杯还有多少 / 好的，先这样
[用户] 汇报一下今天改了哪些库存。保温杯还需要再改吗？（发出 compact_request，写操作随之进入被压段）
[实测] 写操作计数：真实 update_inventory × 1（被压掉的那次；没有第二次）
[实测] 落库值：保温杯：库存 10 件。
[entry] 摘要"已完成"段：**已完成任务**：更新保温杯库存至 10 件。
[实测] 模型汇报：根据任务 T-101 补货核查，今天调整了以下库存：
       保温杯 10 件（目标 50 件）、玻璃杯 17 件（目标 20 件）……
       关于保温杯：目前库存 10 件、目标 50 件，**需要继续补货**——需要我立即更新库存吗？
       说明 │ 模型停在请示，没有擅自再写一次；写操作原文在轨迹里随时可查，
             摘要只负责"别重复做"，不负责事实查询。
```

### demo 4 · 触发策略对照（04-watermark-trigger）

先说这个 case 里 agent 在干什么：**两个会话跑同一套组合策略（ratio 水位 + step 刀口，window=2000、水位 0.7，触发线 1400 token）**——会话 A 连查七轮品类，每轮工具往返把 token 一路顶上去，第 6 轮越线自动压缩；会话 B 纯闲聊聊了三轮，估算始终不到触发线的六成，全程不压。
触发看 token（真实 usage 锚点 + 轨迹增量估算），下刀看 step（保留窗 tool 配对完整）——纯按步数会把闲聊也误压，纯按 token 会切在消息中间。

```text
会话 A：工具往返的查询会话（触发线 1400 token）
[用户] 保温杯和玻璃杯的库存都查一下
[实测] 计量：第 1 轮后估算 698 token（触发线 1400）
[用户] 马克杯和雨伞呢
[实测] 计量：第 2 轮后估算 875 token（触发线 1400）
[用户] 帆布包和围巾呢
[实测] 计量：第 3 轮后估算 1053 token（触发线 1400）
[用户] 保温壶和不锈钢碗呢
[实测] 计量：第 4 轮后估算 1269 token（触发线 1400）
[用户] 再把陶瓷餐具查一下
[实测] 计量：第 5 轮后估算 1419 token（触发线 1400）——已越线
[用户] 电水壶和保鲜盒呢
[实测] 自动压缩：水位越线，reason=watermark，消息 26 → 11 条
       （触发判定发生在 turn 内的 step 边界，用的是发起调用时的真实 usage 锚点；
        第 6 轮打印的估算 1395 是压缩完成后按新视图计的账——旧视图的账已经翻篇）

会话 B：闲聊会话（同样的轮数）
[用户] 你好呀 / 今天店里忙吗 / 好的谢谢你
[实测] 计量：估算 548 / 534 / 760 token（触发线 1400）
[实测] 闲聊会话全程未压缩（正确，最终估算 760 < 触发线 1400）：
       token 计量分辨了大小会话，不会像纯按步数那样误压闲聊
       说明 │ 触发与下刀是两步：token 越水位才触发；触发后刀口落在倒数第 N 个
             step 起点，保留窗 tool 配对完整。两步组合替代了纯步数（误压闲聊）
             与纯 token（截断消息）两种单一策略。
```

### demo 5 · 二次折叠（05-fold-twice）

先说这个 case 里 agent 在干什么：**同一个会话里连压两刀**——第一刀把保温杯、玻璃杯两轮压成摘要 A；再聊马克杯、雨伞两轮后压第二刀，摘要 B 的输入 = 摘要 A + 增量 step（previous 传递），投影认最后一切，视图里只剩摘要 B，原始全文一次都不重读。
折叠有没有真发生，看摘要 B 的内容就知道：保温杯 3 件、玻璃杯 17 件这两条事实只存在于摘要 A 里（原文已不在第二刀的被压段），它们出现在摘要 B 中，说明旧摘要被并入了新摘要。

```text
[用户] 保温杯还有库存吗 / 玻璃杯呢
[用户] （第一刀）上下文有点长了，压一下
[实测] 第一刀：compaction 4d9da9c4（keep_from=163f2c6c，reason=manual-1）
       摘要 A：已完成 / 关键事实与数据：保温杯库存 3 件（规格：316L 不锈钢内胆，
       500ml，杯身磨砂黑）；待查询玻璃杯库存。
[用户] 马克杯还有吗 / 雨伞呢
[用户] （第二刀）再压一次
[实测] 第二刀：compaction 44708f6f（keep_from=9e3daca1，reason=manual-2）
       摘要 B：已完成 / 关键事实与数据：
       * 保温杯：3 件（规格：316L 不锈钢内胆，500ml，磨砂黑）      ← 来自摘要 A
       * 玻璃杯：17 件（规格：高硼硅玻璃，400ml，可微波）          ← 来自摘要 A
       * 马克杯：8 件（来源：query_inventory({"category":"马克杯"})，结果：陶瓷材质，容量 350ml）
       副作用：无写/改操作
       待办：查询雨伞库存
[实测] 折叠：摘要 B 的输入带着上一刀摘要（previous=摘要A）：True——旧摘要被吞，原始全文一次都不重读
[实测] 投影：视图里只剩摘要 B：True（认最后一切）
       说明 │ 全树 29 条 entry、两刀 compaction 都在轨迹里：branch 回第一刀还能回到
             摘要 A 的视图；投影只认当前路径上最后一刀。
```


## 验证（离线已跑，真模型已跑）

### 离线测试（`stage04_trajectory/tests/test_stage05_mechanism.py`，18 条，全绿）

- 计量与触发：`estimate_tokens` 字符估算（不引 tokenizer）、`TokenMeter` 锚点 + 轨迹增量（锚点被 rewind 掉回退全量估算）、ratio / reserve 两种触发线、window=0 不触发；
- 切点：落在倒数第 N 个 step 的第一条消息、tool 配对完整、step 数或 keep_tokens 预算任一用尽即停、纯聊天退回 turn 刀口；
- 折叠与不变式：认最后一次压缩、旧摘要被吞（previous 传递 + 原文不重读）、rewind 过切点语义不变；
- 复检：摘要+保留窗 ≤ 触发线不达标时收缩保留窗重压、收到 1 个 step 仍超线返回 None（不硬压）、手动通道跳过复检；
- **messages 组装**：摘要输入是单条 user 消息（序列化文本）、不含对话 system prompt、不含工具定义、压缩 system prompt 与文档逐字一致；
- agent 集成：水位自动压缩挂 step 边界、计量不越线不压、usage 锚定在最终答复步、max_steps 可配置。

分页 / 批次句柄 / cap+blob 相关的离线测试随功能 descope，一并留作展望（见"代码改动"）。

### 真模型（`stage05_compaction/tests/test_stage05_compaction.py`，5 条）

- 手动压缩端到端（真模型摘要）：entry 落盘、事件报账、新视图 = [system, <摘要>, 保留窗] 且序列合法；
- 口述事实凭摘要可续：原文不在投影、摘要保住关键信息、回答带得出来；摘要失败（空摘要）fail-open 留痕、下一边界重试成功；
- 副作用不重做：写操作被压掉之后全程恰好一次真实写（设值语义落库正确）；
- resume：压缩视图从盘上还原与关会话前一致（append-only）；
- rewind 回归：branch 回切点，原文逐字回来、文件一个字节不动。

demo 五段的实测输出已回填各小节（见"demo"）。

## 下一章预告

能跑了、能重放了、压缩也可复现了，但"改了 prompt、换了模型、重构了 loop，行为有没有变坏"仍然没有答案。Stage 6（rubric 和 eval）接手：重放录制好的会话（事实层 + policy_version），硬断言加 rubric 打分。append-only 这条从 Stage 1 埋下来的线，在那里收口。
