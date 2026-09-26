# Stage 5：上下文压缩——有损变换的纪律（原 5b）

> 配套代码（设计稿，代码尚未落地）：计划新增
> `src/baby_event_driven_agent/stages/stage05_compaction/`（从 stage04_trajectory
> 拷贝后增量改），`stage05-demo` 跑演示、`stage05-test` 跑测试；测试条数与
> 实测输出落地后回填，本稿不预填数字。
>
> 04 已落地压缩的语义与**手动档**（`compaction` entry：`summary` +
> `keep_from_id`；摘要插视图最前、刀口之前跳过、rewind 过压缩点旧消息回来；
> compact_request 控制事件 + Summarizer 两档 + maybe_compact，机制件在
> `session/compaction.py`）。本章补三件事：什么时候压（自动水位）、
> 多次压怎么折叠（认最后一刀）、压完怎么还能重放得回来（不变式）。
> 触发形状参考 pi 的
> auto-compaction（`/compact`、`session_before_compact` / `session_compact`
> / `session_compact_failed`）。

## 为什么要做压缩

即便现在的模型已经支持百万级上下文（GPT-1.1 的 extra 模式支持 1.1M），长程任务下照样不够用：agent 连着干几个小时、跨几天 resume，工具结果一轮轮堆上去，百万 token 也有见底的时候。
经过上一章的处理，咱们的agent已经有了完整的trajectory，这一章我们基于trajectory轨迹来处理上下文压缩；
这一章，我们就来严肃处理上下文的压缩。

## 压缩机制：两种天然的想法
如何压缩上下文，最容易想到的方式有两种。

一种是按 token 消耗：算上下文占了多少 token，超过窗口的某个比例就压，和窗口限制直接对齐。
但是，agent与llm交互，是要严格遵循消息格式的，只判了token数量，有可能压缩时会意外截断消息，导致消息角色不匹配；

另一种则是按轮次：数 turn 数，超过 N 轮就压，token 都不用算。这种方式，虽然确保了消息格式的正确性，但是每一次与LLM交互的tool_result长度都可能是一样的，有可能某次查询工具的结果返回了超多数据，而后n轮的工具调用返回的结果却很少，这种机制，不容易找到正确的压缩时机。

### 实际方案：按 token 检测，按轮次压缩

主流agent的做法，都是综合了如上两种方式：
- 检测用 token：每次 LLM 调用前计量投影出来的 messages，越过水位线就
  触发。计量以 API 返回的真实 usage（`prompt_tokens`）为准，落代码
  校准；调用之间的新增量用字符估算，不引 tokenizer 依赖。
- 下刀按轮次：保留最近 N 个 turn（比如 3 轮）原文，刀口之前的整体送去
  摘要。刀口天然落在一条 user 消息前面，tool 配对完整，不用再修序列。

另外，压缩时，会爆炸最近N轮的对话是不进行压缩的，以免冲掉了用户最近的要求。
咱们的agent的system prompt，是实时添加到messages里，因此system prompt的内容是不进入压缩窗口的。
下面咱们具体看一下，

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

H（如 0.7）不能设太满：压缩自己也是一次模型调用，要读被压段原文、要
留写摘要的输出余量（见"压缩救不了自己"）。N 由 T（如 0.4）反推。
N 轮的 token 量有上界，因为单轮工具结果有分页和 cap 兜着（见后）。

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
- t7 走到一半也在保留窗里：进行中的 turn 不能切，切了 tool 配对就不
  完整；
- 已经压过一次时，更早的轮次不在视图里，送去摘要的是上一刀的摘要加
  两刀之间的轮次（见"滚动折叠"）。

### 触发位置：第四种边界动作

检测在 `build_context` 之后、正式调用之前，只在 step 结算后做：

```text
steering  = 等 step 边界，把消息拼进当前上下文，不打断在飞的（Stage 2）
interrupt = 掐掉在飞的 step，turn 结束（Stage 3）
redirect  = 掐掉在飞的 step + 补消息，turn 不结束（Stage 3）
compact   = step 边界处换一副更短的视图，再发下一次调用
            （手动档已在 04 落地；本章补自动水位触发，同一函数、reason 不同）
```

流中间不换 messages，在飞请求的上下文会漂移。压缩调用本身也是一次
在飞的 LLM 调用：被 interrupt 掐断就不 append（Stage 3 纪律，append
只在摘要成功结算后），压缩期间到的 steering 等下一个 drain 点。

`/compact` 手动压缩和自动水位触发走同一个函数，只在 reason 上区分
（manual / watermark / fallback）。

## 压缩时的 tool 信息

同一份 tool_result 有两个收件人。

- 摘要调用：被压轮次的 tool 消息按视图里的样子原样发——分页/cap 后
  是什么样就发什么样，不发 blob 全文，因为摘要调用是裸 chat、不带
  工具，不能翻页。这反过来要求工具契约保证结论在第一页，否则摘要和
  模型都拿不到结论。
- 压后的对话调用：被压轮次的 tool 消息不再发，由摘要替代；保留窗里
  的工具对话原样成对保留。

摘要 prompt 要保住四样东西：

1. 调过哪些工具、关键参数（查了什么、改了哪个对象）；
2. 结果结论：成功/失败和关键数据；
3. 副作用：write/update 做过就是做过，丢了模型压完会重做（5a 的
   `branch_with_summary` 防的就是这个）；
4. 未决事项：任务进行到哪、下一步原本要干什么。

摘要调用走裸 chat，不带工具，`max_tokens` 封顶，输出按"已完成 /
关键事实与数据 / 副作用 / 待办"分段，只压缩、不推理、不补没发生过
的事。摘要编错一句结论会污染之后所有轮次；原文可以靠 rewind 和
blob 回查。

## 大工具结果：分页是契约，cap 是兜底

### 两种长结果

契约内分页：工具自带 `offset/limit/page_token`，返回一页就是一次
完整回答，没有截断、只有下一页，响应带 `total/has_more`。列表查询、
工单详情都是这类。

契约外 cap：工具语义上返回完整结果（比如 batch 的逐条结果），
harness 套一道字符上限兜底，超了就头部进消息、全文存档、标记
自描述。

两者的边界一句话：**截断必须配取回通道**。给承诺完整、又没有分页
参数的工具（裸 read_file、无参全量查询）硬套 cap，它每次都返回
不完整还没法继续，等于工具在撒谎。

### 工具集扩充

现有四件套撑不出长会话，也没有大结果。5b 在业务世界内加工具，不
引入文件系统语义（read_file 是 coding agent 的形态，不贴合运营
agent）：

| 形态 | 工具 | 剩余部分怎么取 |
|---|---|---|
| 列表分页 | `query_inventory(category?, max_stock?, offset, limit)`、`search_rules(query, offset, limit)` | 同参数翻页，返回 `total/has_more` |
| 单条详情 | `get_inventory_detail(category)` | 列表只给摘要行，完整规格按 key 取 |
| 工单域 | `list_tasks(status?)`、`get_task(task_id, offset, limit)` | 工单条目分页，多轮任务的驱动器 |
| 批次句柄 | `batch_update_inventory(items[])`（每批条数有上限，一次审批，返回 `result_id`）、`get_batch_result(result_id, offset, limit)` | 写操作不可重放，尾部按句柄查，不能让模型再调一次拿结果 |

即 4 个现有工具（查询类加分页）加 4 个新工具。

### cap + blob 存档

cap 包装在工具执行层（`_execute_call` 拿到结果字符串之后、落轨迹
之前），对所有工具生效：

```text
工具返回原始结果（如 200k 字符）
 ├─ 在 cap 内：消息内容 = 结果原文，照旧
 └─ 超 cap：
     全文 ──► blobs/<sha256>（只写一次、不可变；内容寻址 = 去重 + 校验）
     消息内容 = 头部 N 字符 + 自描述标记（共多少、存档 id、省略多少）
     轨迹 payload 另记元数据 {archive_id, bytes, sha256, omitted_chars}
     （和 synthetic/note 一样是审计注脚，发模型前 strip）
```

分页标记是工具自己写的（has_more、下一页参数），存档标记是 cap
包装加的，出处不混。

头部进消息、进轨迹，全文只进 blob，投影不解引用。resume 还原的是
模型当时收到的头部加标记，不灌 blob 全文：灌了，长会话一恢复就
重新撑爆窗口；模型当时没看到全文，灌回去是伪造历史；投影依赖
外部文件状态，可复现不变式就破了。取回尾部是模型主动发起的业务
动作（翻页、详情、句柄查询），blob 是审计存档，回答"工具当时
到底返回了什么"，保留期和脱敏也单独设。

这样单条消息被契约页大小封住，比窗口还大的"毒丸"进不了轨迹；
cap 只防没兜住的第三方工具和异常记录，demo 里把 cap 调到比一页
还小来触发它。

数据用 5b 包自带的 `data/`（脚本确定性造几百到上千行库存和若干
工单），不动共享的 knowledge-base——1 到 5 章的测试断言了"保温杯
42 件"这些具体值。

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

**一，首次压缩触发太晚。** 水位设到 95% 或等 400 才压，第一次的
被压段约等于整段历史，摘要调用装不下，近边界处连写摘要的输出余量
都没有。两个变体：压完保留轮次太多、摘要太长，下一次调用仍超限，
再压已经没有够小的可压段（空转 churn）；字符估算偏小把会话骗进
被动模式，400 成了唯一的发现途径。"滚动折叠"给解法：每次摘要的
输入是旧摘要加新增段，水位早触发时它天然小于一次正式调用。

**二，单条消息本身超过窗口，压缩救不了：**

```text
某步工具返回比窗口还大的结果：
 ├─ 正式调用：这条 tool 消息装不下 → 400
 ├─ 压缩调用：想摘要它，就得先把它原文发给摘要模型 → 还是装不下 → 400
 └─ 删也不行：assistant 的 tool_calls 悬在前头，provider 要求配对
    tool 结果；sanitize 补 UNKNOWN 占位后，模型发现结果"缺失"，
    只会把工具重调一遍
```

它发生在 turn 中间，刀口却只能在 turn 边界。这个成因只能在工具层
消灭，也就是前面的分页加 cap。

兜底阶梯如下，每一级请求体严格变小；踩 400 后原样重试就是死循环
本身，禁止：

1. 水位主动压缩；
2. 400 后激进压缩，保留轮次从 N 调小，先保住当前 turn；
3. 被压段本身仍超窗：分块 map-reduce 摘要（逐块摘要再归并）；
4. 单条超大消息：投影层换成指针/占位（同 UNKNOWN 占位纪律，不改
   事实层），或走分页/句柄重取；
5. 都不行：如实告知用户，或带一份交接摘要开新会话。

压缩失败 fail-open：本次不压、留痕、下一边界重试，不静默截断。
对应 pi 的 `session_before_compact`（可取消/自定义）、
`session_compact`、`session_compact_failed` 三个钩子。

## 多次压缩：滚动折叠

5a 的投影只认路径上第一条 compaction，折叠语义当时在代码注释里
记给了本章。

第二次压缩时，视图已经是 [摘要 A] + [保留段]，新摘要吞掉旧摘要，
不重读全文：

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

摘要模型永远不重读 e1..e6 的原文，它们由摘要 A 代表，这也是死循环
成因一的解法。`build_context` 相应从 5a 的"认第一条"改成"认最后
一刀"；5a 当初认第一条，是折叠语义没定义时不丢信息的保守做法。

rewind 不用特殊处理：压缩是当前路径上的视图，rewind 到它之前它就
不在路径上（5a 已测）；折叠只发生在同一路径的 compaction 之间，
分叉出去的分支各算各的。

折叠有损，递归摘要误差叠加，越老的细节越模糊。所以摘要只管连续性
（进行到哪、别重复什么），不管事实查询；关键数据查轨迹和 blob，
长期事实留给记忆投影（Stage 6 后记）。

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
| `hash` | 被压区间规范化内容的 sha256，钉住"这刀针对的原文" |
| `reason` | watermark / manual / fallback |

EventLog 走 Stage 4 的 `bus.record`：`context_compacted`（原因、
水位、耗时、成败、sid / correlation id），失败记
`context_compact_failed`。轨迹答"模型看到了什么、刀口在哪"，
EventLog 答"什么时候、因为什么、花了多久压了一次"。

### 不变式：给定 (trajectory, policy_version) → 唯一 messages

摘要是模型生成的，同一段历史压两次文本可能不同，可复现性靠一条：
**摘要文本生成即落盘，它是事实、不是策略产物；重放读盘上的
summary，绝不重跑摘要模型。** `policy_version` 钉的是策略，摘要
结果钉在事实层，同一份轨迹在任何机器上重放，投影逐字节相同。
`hash` 用来核对这刀声称压掉的区间和盘上原文对不对得上。

Stage 6 直接用这个不变式：golden trajectory 存事实层加
policy_version，不存压缩后的 messages（否则评测被某一次策略
绑死）；硬断言走事实层，judge 的输入走视图层并注明模型当时看的
是压缩上下文；换过压缩策略的两次评测不能直接比较。

### 压缩的代价

- 多一次摘要调用：钱、延迟、一个新的失败面；
- 摘要编错会污染之后所有轮次（只压缩不推理，原文可回查）；
- 递归折叠的误差累积；
- 压缩让 prompt cache 的前缀整体失效。缓存省成本不省窗口，压缩
  省窗口但会作废缓存，水位别设太低。

## 代码改动（设计稿，待实现）

新包 `stage05_compaction/` 从 `stage04_trajectory/` 拷贝，
`transport/`、`session/` 不动，增量如下。

`session/compaction.py` 已有手动档（cut_before_turn / Summarizer 两档 /
maybe_compact，见 04），本章在同一个文件上扩展：

```python
@dataclass(frozen=True)
class CompactionPolicy:
    version: str = "2026-09-25.v1"
    window_tokens: int = 0        # 模型窗口 W
    watermark: float = 0.7        # 用量到 H·W 触发（按 token 检测）
    target: float = 0.4           # 压完摘要 + N 轮落到 T·W 以内
    headroom_tokens: int = 0      # 下轮输入 + 回复 + 一次工具往返
    keep_turns: int = 3           # 保留最近 N 轮（按轮次下刀）

def estimate_tokens(messages: list[dict]) -> int: ...       # 真实 usage 锚定 + 增量字符估算
def cut_before_turn(entries, keep_turns: int) -> str: ...  # 刀口 = 倒数第 N 个 turn 的第一条 user
                                                           # 进行中的 turn 计入保留窗

class Summarizer(Protocol):
    async def summarize(self, segment: list[dict], previous: str | None) -> str: ...
# ScriptedSummarizer（离线确定性）/ LiveSummarizer（裸 chat、不带工具、max_tokens 封顶）

async def maybe_compact(traj, policy, summarizer, *, reason="watermark") -> Entry | None:
    # 投影 → 计量 → 未越水位返回 None → cut_before_turn → 摘要 → append compaction
    # 失败 fail-open：记 context_compact_failed，不 append、不抛出
```

`agent.py` 在 `_step` 发请求前加一段：`build_context → 计量 →
maybe_compact → 重新 build_context → 发请求`；压缩分支从认第一条
改成认最后一刀；`MAX_STEPS` 提为可配置。

工具层：`tools.py` 加分页参数和 4 个新工具；执行层加 cap 包装和
`BlobStore`（`blobs/<sha256>`，写一次、不可变）；payload 加 blob
元数据注脚；投影和 sanitize 不解引用。`llm.py` 开 `include_usage`
解析尾部 usage。事件 `context_compacted` /
`context_compact_failed` 经 `bus.record` 落盘。

## demo（脚本设计，实测输出待回填）

沿用 main.py 的分段惯例，前四段离线（ScriptedLLM +
ScriptedSummarizer），第五段真模型。demo policy 阈值调小（水位
一两千 token、cap 几百字符、保留 3 轮），不用造几十万 token 的
会话。

1. **触发策略对照**：同样 N 轮，一个会话含大结果、一个闲聊——纯
   按轮次前者超限、后者误压，组合策略下两者都正确。
2. **补货任务单（离线）**：`list_tasks → get_task 分页 →
   query_inventory(max_stock) 翻页 → search_rules → 水位到自动
   压缩 → batch_update_inventory（一次审批）→ 压完继续未处理
   条目 → 汇报`。断言：已更新条目不被第二次写、截断清单经分页
   取回、压后投影合法且 token 下降。
3. **cap + blob**：cap 调到比一页小，看头部进消息、全文落 blob、
   resume 后仍不解引用。
4. **二次折叠 + 失败 fail-open**：两刀后认最后一刀、旧摘要被吞；
   注入一次摘要失败，看留痕、不 append、下一边界重试成功。
5. **真模型**：同一任务端到端，打印压前压后投影、compaction
   entry、事件和 usage 校准值。

## 与 pi 的对照

| | pi（coding-agent） | 本章（stage05） |
|---|---|---|
| 触发 | `/compact` 手动 + auto-compaction（上下文百分比水位） | 手动/自动/fallback 同一 `maybe_compact`；按 token 检测、按轮次保留 N 轮 |
| 钩子 | before_compact（可取消/自定义）/ compact / compact_failed | `context_compacted` / `context_compact_failed`（before 钩子留扩展点） |
| 事实记录 | CompactionEntry + firstKeptEntryId | 同语义（`keep_from_id`）+ policy_version / token 前后 / hash / 摘要模型 |
| 多次压缩 | 新摘要吞旧摘要 | 同；投影认最后一刀（5a 认第一条的折叠补全） |
| 摘要生成 | 模型生成（branchWithSummary 同款） | 同；ScriptedSummarizer 保离线确定性，Live 裸 chat 封顶 |
| 大工具结果 | Read 契约分页（offset/limit）；bash 输出 cap + 全文存临时文件 | 业务工具契约分页（翻页/详情/句柄）+ 统一 cap + blob 内容寻址 |
| 可复现 | 依赖会话文件 | 显式不变式 `(trajectory, policy_version) → 唯一 messages`，摘要落盘即事实 |

## 验证（计划，代码落地后回填）

离线测试（条数待回填），分组：

- 计量与触发：usage 锚定加增量估算、水位触发、手动/自动同通道；
- 刀口：落在倒数第 N 个 turn 的第一条 user、进行中 turn 总在保留
  窗、tool 配对完整、压后满足 target + headroom；
- tool 信息：摘要输入含被压轮次的 tool 页、压后被压轮次不进视图、
  保留窗配对完整；
- 工具层：分页返回 total/has_more、cap 后头部加标记进轨迹且 blob
  hash 可核对、投影（含 resume）不解引用、batch 超条数被契约拒绝、
  get_batch_result 按句柄分页；
- 折叠与不变式：认最后一刀、旧摘要被吞且原文不重读、两次投影
  逐字节一致、rewind 过两刀（回归 5a）、policy_version/hash 落
  entry；
- 失败与兜底：摘要失败 fail-open 加 failed 事件、压缩被 interrupt
  不留半截 entry、阶梯每级请求体严格变小；
- 回归：stage04_trajectory 全部既有用例在新包通过。

真模型（计划 2 条）：补货任务端到端触发真实压缩且压后任务完成
（无重复写）；从盘上 resume，投影与压后一致。demo 五段实测输出
回填各小节。

## 下一章预告

能跑了、能重放了、压缩也可复现了，但"改了 prompt、换了模型、
重构了 loop，行为有没有变坏"仍然没有答案。Stage 6（rubric 和
eval）接手：重放录制好的会话（事实层 + policy_version），硬断言
加 rubric 打分。append-only 这条从 Stage 1 埋下来的线，在那里
收口。
