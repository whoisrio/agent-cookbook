# Stage 5a：会话与真相 —— log、投影与异常恢复

> 配套代码：`src/baby_event_driven_agent/stages/stage05_session/`，
> `stage05-demo` 跑演示（前五段离线不打模型，可当基准反复跑；第六段打真模型），
> `stage05-test` 跑测试（**35 条：33 离线 + 2 真模型**，2026-09-21 实测）。
> 本章机制形状参考 pi coding agent 的 session 设计（树、认父不认子、rewind
> 移指针、投影式上下文），落盘工程沿用 Stage 4 的长度前缀 + CRC，文末有逐项对照。
> 压缩只留口子（entry 类型 + 投影语义），触发逻辑归 5b。

Stage 4 结束时的困境：事件留下来了、能重放了，但"会话"本身还不存在。
`history` 还是 agent 内存里的一个 list——进程一重启，同一个 session_id 静默
变成一段新历史，而磁盘上躺着上一段。用户第二天回来说"接着上次聊"，agent
一问三不知。

本章把"会话"立起来，回答四个问题：

1. 会话的**事实层**长什么样？（轨迹：append-only 的 entry 树）
2. 喂给模型的上下文怎么来的？（投影：`messages = f(轨迹, policy)`）
3. 崩溃之后怎么恢复？（残尾判定 + 投影层修复 + 悬挂审批闭合）
4. 会话怎么回退、怎么切换？（rewind 是移指针，fork 是克隆路径）

## 需求：history 降格为投影

前三章一直说"history 是 log 的投影"。但看 stage04 的代码：history 就是
agent 内存里的 list，直接往里 append；EventLog 记的是**事件**（user_input /
agent_reply / tool_result…），history 记的是**消息**——两者靠约定对齐，没有
任何机制保证"从 log 能重建出 history"。约定不是结构。

本章把它变成结构。轨迹层（`trajectory.py`）的三个决定：

1. **事实层是 per-session 的 entry 树**，不是内存 list。每个 session 一个
   文件，第一行 header（不是树节点），其后 entry 逐行追加。
2. **落盘用 Stage 4 的框架**：长度前缀 + CRC。pi 的轨迹是裸 jsonl，崩在写
   一半时"这条完不完整"不可判定；这个工程在 Stage 4 已经造好并测过，直接
   复用——pi 的树结构 + Stage 4 的落盘工程。
3. **投影是从树算出来的视图**：`build_context(policy)` 每次现算，可以有损
   （压缩、修复），但修复只作用于喂给模型的副本，**绝不写回文件**。

两个事实层各司其职，不冲突：

| | EventLog（总线侧，本章一字未改） | Trajectory（agent 侧，本章新增） |
|---|---|---|
| 记什么 | 传输层的事件账：token 流、治理、生命周期 | 会话的结构账：消息树、状态、生命周期 |
| 坐标 | seq（全局单调） | entry id（8 位短 UUID） |
| 粒度 | 一轮 86 条事件 | 一轮 4 条 entry |
| 保留 | 按段滚动（审计有窗口） | 永久（删了就不是 append-only） |

## 机制一：轨迹的形状——9 种 entry，认父不认子

entry 按"对 LLM 调用的影响"分三组（分类轴就是消费方式——投影函数要按它
分派）：

- **进上下文**：`message`（user / assistant / tool，一条 assistant 连
  tool_calls 带可见文本是一个节点）、`branch_summary`（被抛弃分支的摘要，
  遗言不是对话）、`compaction`（口子：类型 + `first_kept_id` 已定义，投影
  语义已实现，触发归 5b）。
- **改状态**：`model_change`（不产生消息，投影时覆盖式提取）。
- **纯元数据**：`session_started` / `session_resumed` / `session_end` /
  `label` / `custom`——不进上下文，给回放的人和 UI 看。

树上只有三块骨头：

1. **认父不认子**：节点只带 `parentId`，父节点不知道孩子。追加永远是
   新增，从不修改——这是 append-only 能成立的结构前提。
2. **append O(1) 三步**：建节点（认父）→ 落盘 → byId 索引 + 移 `leafId`。
3. **树是读出来的**：想找分叉？按 parentId 反查（`branch_points()`）。

消息的细粒度配对（tool 结果拴回哪次调用）不占 parentId——那是消息自己的
事，靠 `tool_call_id`。和 Stage 4 的信封分层是同一个思路。

### 轨迹长什么样（demo 第 1 段实测）

```text
[entry] 2eecbff3 ← ∅        session_started  {"by": "store"}
[entry] e18bfc71 ← 2eecbff3 model_change    → fake-model
[entry] eb2e8363 ← e18bfc71 message        user: 保温杯还有库存吗
[entry] e2b97f9e ← eb2e8363 message        assistant → toolCall(query_inventory)
[entry] 1820dfae ← e2b97f9e message        tool: 保温杯：库存 42 件；316L 不锈钢内胆…
[entry] fd02f445 ← 1820dfae message        assistant: 保温杯库存 42 件，316L 不锈钢内胆。
[统计] 文件 6 条 entry + 1 条 header（不是节点，type=session）；message 里
       1 user / 2 assistant / 1 tool；残尾=False
[实测] 分叉点：无（大多数会话的轨迹就是一条链，树是为少数时刻准备的）
```

## 机制二：投影——messages = f(轨迹, policy)

`build_context(policy)` 三步，和 pi 的 `buildSessionContext` 同构：

1. **路径遍历**：从 `leafId` 沿 `parentId` 走回根，reverse 成根→叶顺序。
   只有这条线上的 entry 会进投影——被抛弃的分支不是"被过滤"，是遍历
   根本不经过它们。
2. **按类型分派**：message → messages；model_change → 覆盖变量（路径上
   最后一次生效，一次都没有则回落 `policy.default_model`）；元数据 → 跳过
   （stats 里数得出）；branch_summary → `<summary>` 的 user 消息；
   compaction → 摘要插在最前，切割点之前的跳过。
3. **sanitize 收口**：保证序列约束永远满足——system 永远第一条（system 是
   参数不是事实，由 policy 前置）、合成注脚 strip、孤儿 tool 结果丢弃、
   悬挂的工具调用按两档修复（见机制四）。

这个函数是**纯函数**：同一份文件 + 同一个 policy，两次投影逐字节相同。
这不是洁癖，是 Stage 6 eval 的地基——golden trajectory 存事实层 +
policy_version，不存压缩结果，重放时现算。

## 机制三：rewind 与 fork——切换不修改历史，只创造新的"当前"

**rewind**（`branch(to_id)`）的实现核心就一行：`self.leaf_id = to_id`。
没有任何 entry 被删除——它们还在 byId 里、还在文件里，只是不在当前路径上。
回退后继续追加，**分支**就出现了：两个节点共享同一个 parent。

**fork**（`SessionStore.fork`）把当前路径克隆进一份新会话文件（id 与
parentId 原样保留，补一条 `session_resumed` 说明 forked_from）。新文件是
完整合法的轨迹，可以独立继续生长；旧文件原封不动。

可选的**带摘要 rewind**（`branch_with_summary`）：摘要节点挂在回退点上
（与被抛弃分支同父），新分支的投影里多一条 `<summary>` user 消息——
"之前试过 X，结论是 Y"。知道历史，不被细节淹没；它是视图，不是对话。

### 实测（demo 第 3 段）

```text
[实测] branch(07df5b83)：文件里还是 6 条 entry（6 → 6，一条没删），字节未变：True
[实测] 回退后投影只剩 2 条消息（system + 那条 user）
[实测] 回退后追加 = 分支：07df5b83 现在有两个孩子 ['e2e7b5b7', '3a546b41']
       （grep parentId 的程序版）
[实测] branch_with_summary：摘要节点 4ab442a6 挂在 07df5b83 下（抛弃了 1 个 entry）
[系统] 现在的投影（新分支的 agent 看到的）：
  system    你是一个通过工具干活的通用 agent。
  user      保温杯还有库存吗
  user      <summary>试过查保温杯库存（42 件），结论：库存充足，无需补货。</summary>
       说明 │ 被抛弃的分支原样躺在文件里——想回头随时能回（branch 回去即可）。
```

### session 切换（demo 第 5 段实测）

```text
[系统] store 里的会话：['5a7b8f59…', 'a6c8b997…']
[实测] 原会话 5a7b8f59…：7 条 entry，最后一条 user = 旧会话的下一句
[实测] 分叉 a6c8b997…：8 条 entry，最后一条 user = 新会话的下一句
[实测] 分叉的生命周期事实：{"type": "session_resumed", "forked_from": "5a7b8f59…"}
       说明 │ session 切换不修改历史，只创造新的"当前"：模型切换是树上的
             新节点，会话切换是新文件——都是追加，都不是改写。
```

## 机制四：异常恢复——三级收口

恢复的前提有两个：log 本身**可判定**（写一半能认出来，Stage 4 的 CRC）+
事实**自描述**（读得懂"那次没走完"，本章的定义）。三级收口：

**1. 字节级残尾**：长度/CRC 对不上就是残尾，读到它为止，前面一条不少。
resume 时撞上残尾：`TrajectoryLog` 记下最后一条完好记录的字节边界，第一次
追加前把残尾裁掉——残尾不构成事实（没写完的不算已发生），裁它不是改历史；
不裁的话它赖在文件中间，后续追加的记录永远读不到。裁完在 `session_resumed`
事件里留 `torn_tail: true` 的痕。

**2. 语义残尾·悬挂的工具调用**：崩在工具执行前，轨迹尾部可能是
"assistant 带着 tool_calls，结果永远没来"。第二次复用 Stage 3 的消息形状
表，在投影层两档修复：`placeholder`——补一条自描述占位
（`[UNKNOWN: 会话在工具执行前中断，结果缺失]`）；`drop`——连那条 assistant
一起撤。两档都**只改投影**，原文件字节不变（测试钉死）。
顺带处理孤儿 tool 结果（配不上任何调用的）：直接跳过——发出去 provider
直接拒。这也解释了 compaction 的 `first_kept_id` 为什么必须选在序列合法的
边界（一轮的开头）：选在 turn 中间，保留段以孤儿 tool 开头，会被 sanitize 丢掉。

**3. 悬挂审批**：进程被硬杀留下孤立的 `approval_required`（在 EventLog 里，
没有配对的 `approval_decided`）。resume 时扫一遍，每个悬空的请求补一条
`approval_decided {action: abandoned, by: session_resume}`，走 bus.record
留痕——"一问必有一答"在崩溃路径上也成立，且幂等（闭合过的不碰）。

### 实测（demo 第 4 段）

```text
[实测] 残尾：完好 6 条 → 砍 11 字节后读到 5 条（停在坏记录之前，torn=True）
       → resume 裁掉残尾续写，resumed 事件留痕 torn_tail=True
[实测] 悬挂调用·placeholder 档：投影补了 1 条占位
       → [UNKNOWN: 会话在工具执行前中断，结果缺失]；原文件字节未变：True
[实测] 悬挂调用·drop 档：投影里还有要工具的 assistant 吗：False
       （连那条 assistant 一起撤了，repaired=1）
[实测] 悬挂审批：孤立请求 ap-deadbeef → 闭合 ['ap-deadbeef']；再扫一遍：[]
       （幂等，闭合过的不再碰）
       说明 │ 共同纪律：没写完的不算已发生（字节级）；修复只作用于喂给模型
             的投影（语义级）；补的裁决走 record 留痕，不伪造"当时批过"（审批）。
```

## 机制五：会话身份与生命周期——sid 由 store 分配

`SessionStore` 三个入口：`start` / `resume` / `close`，判据是"store 里有没有
这个 sid"。三条规矩：

- **sid 由 store 分配**（uuid4 hex，文件名即 sid），不是调用方随口给。agent
  只认 store 里 start/resume 过的会话（`attach` 之后收工）；没 attach 过的
  sid 来了直接报错——宁可炸也不静默开一段新历史（Stage 4 结尾那个困境的
  结构性解法）。
- **生命周期事件进轨迹**：`session_started` / `session_resumed`（带
  torn_tail / forked_from）/ `session_end` 都落盘。否则一个文件里两段进程
  的历史首尾相接，回放时看不出中间断过——和 Stage 2"排队的消息连 log 里
  都没痕迹"是同一类坑。
- **同 session 单写者**：一个 sid 同时只有一个 Trajectory 在写（一个 agent）。

## agent 侧的改动只有三处

这正是把机制放在轨迹层上的意义（对照 04 章"agent 侧改动只有三处"）：

1. `self.history: dict[sid, list]` → `self.trajectories: dict[sid, Trajectory]`。
   所有 `history.append(...)` 换成 `traj.append(MESSAGE, message_payload(...))`。
2. `_step` 的上下文从内存 list 换成 `traj.build_context(policy).messages`——
   history 降格为投影，每次 LLM 调用前从轨迹现算。rewind / 压缩之后，下一次
   调用自动就是新视图。
3. 合成消息（中断标记、assistant 占位、纠正 user）照旧进事实层
   （`synthetic: true` + note），只是落点从 history 变成轨迹——否则
   "history 是 log 的投影"在合成消息这条路上断掉。

中断 / steering / redirect 的逻辑一字未动：被掐的 step 不留半截消息这条
Stage 3 纪律，在树上同样成立——append 只发生在 step 成功结算之后。
bus / events / persistence / llm / outbound / subscribers 与 stage04 一字
未改。

## 与 pi 的对照

| | pi（coding-agent） | 本章（stage05_session） |
|---|---|---|
| 事实层 | entry 树，裸 jsonl 行 | entry 树，长度前缀 + CRC（残尾可判定） |
| entry 类型 | 9 种，按对 LLM 调用的影响分三组 | 9 种同构（lifecycle 换成 session_* 三个） |
| 追加 | appendEntry：认父 + 移 leafId | 同 |
| rewind | `branch()`：leafId = to_id | 同；另有 `branch_with_summary` |
| 上下文 | buildSessionContext：路径遍历 + 分派 | build_context：同构 + sanitize 两档修复 |
| 压缩 | CompactionEntry + firstKeptEntryId | 口子已留（同语义），触发归 5b |
| 恢复 | transformMessages 收口 | sanitize 两档 + CRC 残尾 + 悬挂审批闭合 |
| session 切换 | `_rewriteFile` 克隆当前路径 | `fork()` 克隆路径 + 新 sid |

## 跑一下

```text
── 第 6 段：真跑一轮，两层事实各自记账 ──
       说明 │ 真模型 + 真工具。EventLog 记传输层的事件账（token 流、治理、
             生命周期），轨迹记会话的结构账（消息树、投影）。两层坐标不同。
[用户] 保温杯还有库存吗
[工具] ← query_inventory 结果：保温杯：库存 42 件；316L 不锈钢内胆，500ml，杯身磨砂黑。
[系统] 轨迹（尾部 6 条）：
  7a1c5273 ← 5bd45442  model_change     → live
  1a8e07a4 ← 7a1c5273  message          user: 保温杯还有库存吗
  37a12755 ← 1a8e07a4  message          assistant → toolCall(query_inventory)
  9047591c ← 37a12755  message          tool: 保温杯：库存 42 件；316L 不锈钢内胆…
  86864f7c ← 9047591c  message          assistant: 有库存，目前还有42件…
  1b7de00a ← 86864f7c  session_end      {"reason": "demo 结束"}
[统计] EventLog：seq 1..86（传输层事件账）；轨迹：7 条 entry（会话结构账）；
       投影出 5 条消息，model=live
```

同一轮对话：EventLog 86 条（30 个 token 增量 + 生命周期 + 治理），轨迹 7 条
entry。**账分两层记，各答各的问题**：传输层答"事件怎么流的、谁批的"，
会话层答"模型看到了什么、从哪能回退"。

## 验证

- 环境：Python 3.13（仓库 .venv），模型走仓库根 .env 的 OpenAI 兼容端点
  （2026-09-21 实测）。
- pytest：`stage05-test` **35 passed**（2026-09-21 真跑）——33 离线 + 2 真模型。
  离线三组（ScriptedLLM 把工具调用变成确定性事件）：
  - 轨迹层（`test_stage05_trajectory.py`，15 条）：append O(1) 与 id 唯一、认父不认子、路径遍历根→叶、
    投影分派与确定性（两次投影逐字节相同）、model_change 覆盖与回落、
    压缩口子（摘要插最前、切割点之前跳过、rewind 到压缩之前旧消息原样
    回来）、rewind 移指针且字节不变、回退后追加 = 分支、branch_summary
    是视图不是对话、CRC 残尾停在最后一条完好 entry、fork 双文件独立、
    悬挂调用两档修复且原文件字节不变；
  - 会话层（`test_stage05_session.py`，10 条）：sid 由 store 分配、start 落 started + 初始
    model_change、resume 重建树且生命周期事实齐全、残尾判定并裁剪留痕、
    close 落 session_end、fork 在 store 注册新会话、悬挂审批闭合（幂等）、
    同文件两实例投影一致；
  - agent 集成（`test_stage05_agent.py`，8 条）：一轮 turn 在轨迹里是合法序列、投影每次 step 现算、
    steering 进轨迹、中断收尾与合成消息留痕（ScriptedLLM 用 hang_after 把
    "中断落在 stream 阶段"从竞速变成确定）、redirect 补齐消息形状且被掐的
    工具一次没执行、治理否决的占位进轨迹而裁决进 EventLog、resume 换新
    agent 实例接着聊（不重复事实）、未知 sid 宁炸不静默开新历史。
  真模型（2 条）：一轮真对话后轨迹落盘且投影合法（从盘上重建的实例投影
  一致）；回归——中断语义没被改坏，合成消息进的是轨迹。
- demo：`stage05-demo` 六段，前五段离线实测通过（输出见各机制小节），
  第六段真模型实测通过（输出见上）。
- 实现过程中修掉的两个真问题（都进了测试）：
  - 残尾裁剪：resume 撞残尾后直接追加会让新记录不可读——第一次追加前
    先把残尾字节裁掉（它们不构成事实）；
  - `last_message()` 语义：resume/fork 之后 leaf 可能是元数据节点，
    "最后一条消息"要跳过尾部元数据往前找。

## 下一章预告

会话立住了、也能从崩溃里重建了，但会话一长上下文装不下；一旦压缩，
"重放出来的上下文跟当时不一样"——可复现性没了。Stage 5b（压缩与上下文）
接手：压缩是事件（`context_compacted {from, to, summary, policy_version,
hash}` 进 log）、只在 step 边界触发、不变式是"给定 (log, policy_version) →
唯一 messages"。本章已把口子留好：`compaction` entry 的类型、`first_kept_id`
的投影语义都在，缺的只是"什么时候压、压成什么"的策略——那是 5b 的全部内容。
