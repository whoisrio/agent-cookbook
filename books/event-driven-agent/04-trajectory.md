# Stage 5a：会话与真相 —— log、投影与异常恢复

> 配套代码：`src/baby_event_driven_agent/stages/stage04_trajectory/`
>（与 3c 同包：工具层与长程任务场景见 3c，本章落轨迹层与手动压缩），
> `stage04-demo` 跑演示（十三段：1-5 / 7-11 / 13 离线可反复跑；6 / 12 打真模型），
> `stage04-test` 跑测试（**57 条：55 离线 + 2 真模型**，2026-09-26 实测）。
> 本章机制形状参考 pi coding agent 的 session 设计（树、认父不认子、rewind
> 移指针、投影式上下文），落盘工程沿用 3b 的长度前缀 + CRC，文末有逐项对照。
> 压缩的语义（entry 类型 + 投影规则）与手动触发（compact_request + Summarizer）
> 都在本章落地；自动触发策略（水位、滚动折叠）归 05。
> 包分三层：`transport/`（3b 传输层）、`session/`（trajectory + store +
> compaction，事实层）、`agent.py`（agent + 投影 + 压缩接线）——和"账分两层记"
> 是同一个分法。

前面几章，一直在处理的是用户的新增消息的输入。
这一节，我们来给agent增加上从已有session中恢复，继续之前对话(reload)，以及在对话中跳转到指定消息的能力(rewind)；
要支持如上的能力，必须依赖咱们一开始就在记录的会话轨迹(trajectory)，轨迹作为agent运行阶段的真实记录，还是进行用户记忆整理，agent能力评测等等功能的基础；
咱们初始设计的轨迹太简单，这次咱们一起来改造他。

## Trajectory 和 messages 的关系
在进入改造之前，我们先搞清楚trajectory和messages的关系；
trajectory是agent操作过程中的事实记录，每一步用户和模型的动作都被原封不动的记录下来，也就是咱们一直在说的append-only；
这些操作不光是和LLM相关的交互，也包括agent本身的一些设置，比如切换模型，切换thinking level等等；
messages是当前会话中发给LLM的消息列表，让LLM理解当前对话的上下文，但是由于LLM上下文窗口是有限的，所以，messages实际上是当前session trajectory的子集，LLM的上下文压缩，或者用户对话进行rewind操作等等，都会导致messages和和trajectory的不一致。
trajectory是agent 运行期间的唯一事实标准，messages是可以从trajectory还原出来的，所以当下主流的agent都设计了从trajectory到messages的投影转换。

### trajectory的重设计
初始的设计里，我们的trajectory(session log)设计成了list，并且没有记录每行记录之间的关系，这样的设计仅仅只能当做日志记录，没法支撑上述咱们提到的操作。因此，我们首先要给trajectory加上其引用的parent标识，那么每一行trajectory直接的关系如下。(参考pi agent的设计，只在新增轨迹条目的记录其父节点，不做子节点记录，避免改动已有轨迹)

  ```text
  1 → 2 → 3 → 4          原路径，当前节点是 4
          └→ 5 → 6       rewind 到 3 再继续追加：3 现在有两个孩子
  ```

具体的trajectory条目(entry)定义如下，
```python
class Entry:
    id: str
    parent_id: str | None 
    type: str
    ts: str
    payload: dict[str, Any]
```
各字段说明：

- **id**：8 位短 UUID（`uuid4().hex[:8]`），会话内唯一，rewind / fork 的寻址坐标。
- **parent_id**：父 entry 的 id，根节点为 `None`。
- **type**：即如上类型分组，投影函数按它分派。
- **ts**：trajectory记录的时间戳。
- **payload**：内容按 `type` 来区分, 比如`message`，存的是喂给模型的和模型吐出的原始数据。

entry的type可选的类型大致如下，要根据type来决定是否在投影生成 message context使用该entry，

| 组 | 装什么 | 谁消费它 |
|---|---|---|
| 进上下文 | message / branch_summary / compaction | 模型（经投影） |
| 改状态 | model_change / prompt_change | 投影函数（不产生消息，覆盖式提取） |
| 纯元数据 | session_started / session_resumed / session_end / label / custom | 回放的人和 UI |


### messages--trajectory的投影

trajectory 定下来之后，来看看如何通过轨迹来生成messages。
**messages =f(trajectory)**。f函数落在 agent 侧，
agent 每次 LLM 调用前根据内存中的trajectory现算一遍，不缓存：
resume 时从文件加载，append 时落盘和更新索引同时做，trajectory终是文件的镜像。

f 内部三步：
1. **路径遍历**：从 leafId 沿 parentId 走回根，再 reverse 成根→叶顺序。被
   rewind 抛弃的分支根本不在这条路上，自然进不了上下文。
2. **按 type 分派**：message 转成消息，model_change 更新当前模型，
   prompt_change 更新当前 system prompt（都是覆盖式提取），元数据跳过，
   如果message经过压缩，那么就从压缩点记录的位置开始取entry。
3. **sanitize**：保证发给模型的消息序列合法，处理agent异常退出时可能写入的不完整trajectory。

> 从轨迹加载回上下文，有一个小设计：system prompt 总是取最新的，而不是
> 直接采用轨迹 header 里记的那份。落地是：初始 prompt 记在 header；之后
> 每次运行发现 agent 的 prompt 和轨迹记录不一致（比如 resume 时换了模板），
> attach 就追加一条 `prompt_change` entry（改状态，不进消息序列）；投影
> 覆盖式提取、路径上最后一次生效，没有变更就回落 header。


### 有压缩的轨迹怎么组织、怎么读
我们看看如果messages经过压缩，应该如何添加压缩entry以及如何从trajectory中恢复messages；

现在看添加压缩节点，
压缩不删任何东西，append-only 之下，它只能是一个**追加的视图标记**：往树上多加一个 `compaction` entry，payload 装两样东西——`summary`（被压掉那段的摘要）和 `keep_from_id`（边界指针：从那条entry起原样保留，之前的由摘要代替）。

在如下轨迹样例中，压缩发生时对话已经走到 e5，compA **追加在那一刻路径的末尾**(压缩e1+e2，保留最近滑窗 e3+e4)

```text
压缩发生时：  e1 e2 | e3 e4 e5 [compA(keep_from=e3)]
              摘要   原样保留    ↑ 追加在末尾（刀口在 e2|e3 之间）

之后继续对话：e1 e2 | e3 e4 e5 compA e6 e7 ...
                                    ↑ 新对话接在 compA 之后
```
两个时刻的信息都在 entry 里：`keep_from_id=e3` 画出"e1 e2 由摘要代替、
e3 起原样保留"的刀口；节点本身挂在压缩那一刻的 leaf 上。

再说说读取。投影走到 compaction 节点时，按如下规则处理：
1. **`keep_from_id` 之前跳过、从它起原样保留**。跳过不是删除，e1、e2
   还在文件里，只是这一刀的视图里不出现。
2. **摘要插在视图最前**，不是它树上所在的位置。节点追加在压缩那一刻的
   路径末尾，但它代表的是最前面那段被压掉的历史——树上位置和视图位置
   相反。最终顺序是 `[system, summary, e3, e4, e5, e6 ...]`：摘要开头，
   其后全是原文。

### rewind 与 fork——切换不修改历史，只创造新的"当前"

**rewind**（`branch(to_id)`）的实现是一行赋值：`self.leaf_id = to_id`。
rewind后，从rewind点拉出session分支，如下所示，从4rewind到3后，新的5的parent是3.

```text
1 → 2 → 3 → 4          branch(3) 前的原路径
        └→ 5           branch(3) 后追加：3 有两个孩子（4 和 5）
```

**rewind 落点与压缩视图**
rewind 就是移指针，对 compaction 节点没有任何特殊处理，差别全在投影怎么读它。
拿压缩后的轨迹看两个落点（压缩发生时对话走到 e5，compA 压掉 e1、e2，之后
又聊了 e6、e7、e8）：

```text
e1 e2 | e3 e4 e5 compA(keep_from=e3) e6 e7 e8        leaf = e8
```

**落点一：rewind 到 压缩发生前的 e5 或更早的entry，就当压缩没发生过：

```text
e1 → e2 → e3 → e4 → e5 ─┬→ compA(keep_from=e3) → e6 → e7 → e8
                        └→ 9     branch(e5) 后继续对话：9 与 compA 同父

新的路径（leaf = 9）：e1 → e2 → e3 → e4 → e5 → 9
                     —— compA 连同 e6 e7 e8 都不在路径上
投影：[system, e1, e2, e3, e4, e5, 9]    旧消息逐字回来——压缩是视图，不是
                                        对数据的手术
```

**落点二：rewind 到 压缩点compA**（`branch(compA)`）。路径到 e1 e2 e3 e4 e5 compA
为止：

```text
e1 → e2 → e3 → e4 → e5 → compA ─┬→ e6 → e7 → e8
                                └→ 9     branch(compA) 后继续对话：9 与 e6 同父

新的路径（leaf = 9）：e1 → e2 → e3 → e4 → e5 → compA → 9
投影：[system, <摘要>, e3, e4, e5, 9]    e1、e2 跳过、摘要插最前——回到压缩
                                        刚做完那一刻的样子
```

两个落点文件都一个字节没动，e6、e7、e8 原样躺着；差别只在路径走到哪、投影因此算出什么。

**branch_with_summary——rewind 时给被抛弃段留摘要**
还有一种带分支整理能力的summary，比如用户在session a中要求agent执行任务，进行了多轮对话，但是效果不尽如任意，希望切回某个初始点，尝试别的方式，
rewind回到过去时，希望一并把当前session已经做的尝试进行summary以避免agent重复相同的路径，那么rewind+summary之后的路径就会变成如下，

```text
1 → 2 → 3 ─┬→ 4 → 5      被抛弃段，原样躺在文件里
           └→ S           新增的摘要节点，挂在 3 下（与 4 同父）
               └→ 6      之后的新对话从 S 继续

当前路径（leaf = 6）：1 → 2 → 3 → S → 6
投影：[system, 1, 2, 3, <summary>4、5 里试过 X，结论是 Y</summary>, 6…]
```
这个summary是rewind操作的一种可选项，比如pi agent通过`tree`切换对话分支时，就提供了这样的能力

**fork**（`SessionStore.fork`）把当前路径克隆进一份新会话文件（id 与
parentId 原样保留，补一条 `session_resumed` 说明 forked_from）。新文件是
完整合法的轨迹，可以独立继续生长；旧文件原封不动。

"从哪里开始 fork"没有专门参数——fork 克隆的就是**当前路径**，想从更早的
地方开，先 rewind 再 fork，两个原语组合即可。克隆出来的新文件
长这样：

```text
原文件（A，原封不动）：  e1 e2 e3 e4 e5      ← leaf 不动，继续用就是原会话

新文件（B）：
header {type: session, id: B, note: "forked from A"}
e1 e2 e3 e4 e5                          ← 原样克隆：id / parentId 不改
session_resumed {forked_from: A}        ← 生命周期标记，也是新的 leaf
```

id 原样保留（不重新编号）——per-session 文件隔离，两份文件里的同名 id互不冲突。和原始路径的关系记在 `session_resumed` 的 `forked_from` 里（审计留痕，不是引用）；之后两条轨迹各自生长，一边 rewind /压缩 / 追加都影响不到另一边。

### 手动压缩：把口子插上电（compact_request + Summarizer）

压缩的语义钉死了，谁来按按钮？本章给手动档：一条 `compact_request` 控制事件
（旁路，与 user_interrupt / user_approval 同一待遇，不进收件箱），agent 在
**step 边界**处理它——与 steering（等边界拼消息）、interrupt（揸在飞）、
redirect（揸 + 补消息）并列的第四种边界动作：换一副更短的视图，再发下一次调用。
流中间不换上下文，在飞请求的视图不漂移：

```python
compact_reason = self._pending_compact.pop(sid, None)
if compact_reason is not None:
    # 不消耗 step 预算；摘要失败 fail-open，不挡 turn
    await self._compact(sid, traj, reason=compact_reason)
```

`_compact` 做三件事：算刀口（保留最近 N 轮，刀口 = 倒数第 N 轮的第一条真实
user entry，天然落在 turn 边界，tool 配对完整）→ 摘要（Summarizer 协议两档：
ScriptedSummarizer 离线确定性 / LiveSummarizer 裸 chat 不带工具、max_tokens
封顶）→ 追加 `compaction` entry（summary + keep_from_id + reason）。
投影一字未改：下一次 build_context 自动就是新视图。

两个纪律值得点名：

- **摘要的输入是刀口前的投影视图**，不是原始 entry——branch_summary 的
  <summary> 也在里面，"摘要吞摘要"在手动档就第一次发生。滚动折叠（多刀
  互吞、投影认最后一刀）归 05，在那之前 maybe_compact 拒绝第二刀。
- **摘要编错一句会污染之后所有轮次**。所以摘要调用是裸 chat、按四段输出
  （已完成 / 关键事实与规则 / 副作用 / 待办）、只压缩不推理；原文可靠 rewind
  和轨迹回查。摘要的代价也真实可感：刻意遗漏一条"已处理"，模型对不上账
  就会重查一次——一次冗余查询换整段窗口（demo 第 13 段实测）。

### 异常恢复——三级收口
下面，再来看看从trajectory恢复session时的异常保护；
假如agent在运行时出现了异常导致进程挂掉，trajectory可能就会不完整，按残迹分三种。resume（store 的第二个入口：从盘上读回轨迹重建树，然后追加一条 `session_resumed`，标记"第二次运行从这里开始"）逐个处理：

**1. 格式不完整**——死在一条记录中间，长度/CRC 对不上：

```text
崩溃时：    e1 e2 e3 [半条记录，CRC 对不上]        ← 残尾，不是事实
resume 后： e1 e2 e3 session_resumed{torn_tail: true}
            ↑ 残尾字节裁掉（没写完的不算事实），痕迹记在 resumed 里
```

**2. 工具调用悬挂**——死在 assistant 落盘后、工具结果落盘前：

```text
崩溃时：    ... user → assistant(tool_calls c1)          ← 结果永远没来
resume 后： ... user → assistant(tool_calls c1)
                     → session_resumed → user("继续")     ← 文件里永远悬挂
投影时才补：assistant → tool[UNKNOWN: 会话在工具执行前中断，结果缺失]
            ↑ 修复只发生在投影，模型知道缺了什么、能自己重调
```

**3. 审批悬挂**——死在 `approval_required` 落盘后、decided 前。审批住在
EventLog 那本账上（3b 的审批流），Trajectory 不涉及：

```text
崩溃时（EventLog）：   ... approval_required{request_id: ap-x}    ← 没有配对的 decided
resume 后（EventLog）：... approval_required
                       → approval_decided{action: abandoned}      ← bus.record 补，幂等
```

## 代码改动：session 层新增，agent 侧只动三处

`session/`（trajectory + store）是本章新增的，`agent.py` 是唯一被改的老文件。
先看新增层的两个类，再看 agent 动的那三刀。

### Trajectory：一棵常驻内存的 entry 树

事实层本体就两个类。`TrajectoryLog` 管单会话文件：第一行是 session header
（type=session，带 sid / cwd / system_prompt 原文——header 不是树节点），
其后 entry 逐行追加，框架和 3b 的 EventLog 相同：长度前缀 + CRC，崩在
写一半时读到坏记录为止，残尾字节裁掉再续写：

```python
line = b"%08x %08x " % (len(data), zlib.crc32(data)) + data + b"\n"   # 长度 + CRC + json
```

`Trajectory` 是文件的内存镜像，状态有三个：`header`、全树索引 `_by_id`、
一个指针 `leaf_id`。所有操作围绕这三个状态，核心是 append：

```python
def append(self, etype: str, payload: dict[str, Any]) -> Entry:
    """追加 O(1) 三步：建节点（认父）→ 落盘 → 索引 + 移 leaf。不修改任何旧节点。"""
    if self.torn:                          # 加载时撞过残尾：第一次追加前先裁
        self.log.truncate_torn()
        self.torn = False
    entry = Entry(
        id=_short_id(),                    # 8 位短 id（pi 同款）
        parent_id=self.leaf_id,            # 认父：父 = 此刻的 leaf
        type=etype, ts=_now(), payload=payload,
    )
    self.log.append(entry.to_dict())       # 先落盘，后动内存
    self._index(entry)
    return entry

def _index(self, entry: Entry) -> None:
    if entry.id in self._by_id:
        raise ValueError(f"entry id 冲突：{entry.id}")
    if entry.parent_id is not None and entry.parent_id not in self._by_id:
        raise ValueError(f"entry {entry.id} 的父节点不存在：{entry.parent_id}")
    self._by_id[entry.id] = entry
    self.leaf_id = entry.id                # 追加即移动 leaf：树末端永远指向最新事实
```

`_index` 同时是数据合法性的检查点：id 冲突、父节点不存在都直接抛错，
"认父不认子"和 id 唯一由它保证。投影要的当前路径来自 `path()`：

```python
def path(self) -> list[Entry]:
    """当前路径：leaf 沿 parentId 走回根，再 reverse 成根→叶顺序。

    只有这条线上的 entry 会进投影——其他分支的数据不是"被过滤"，
    是遍历根本不经过它们。
    """
    out: list[Entry] = []
    cur = self.leaf
    while cur is not None:
        out.append(cur)
        cur = self._by_id.get(cur.parent_id) if cur.parent_id else None
    out.reverse()
    return out
```

branch / branch_with_summary / fork 都是"移指针 + append"的组合（见前面
几节）；观测另有 `entries()`（全树、按文件顺序）和 `branch_points()`（同父
多子的分叉点，grep parentId 的程序版）。

### agent 侧：只动三处

这正是把机制放在轨迹层上的意义：

1. `self.history: dict[sid, list]` → `self.trajectories: dict[sid, Trajectory]`：
   所有 `history.append(...)` 换成 `traj.append(MESSAGE, message_payload(...))`
   ——消息进 append-only 的 entry 树，而不是内存 list。sid 的唯一入口是
   `attach`：登记 store.start/resume 的产物，顺手处理 prompt 变更留痕——

```python
def attach(self, traj: Trajectory) -> str:
    self.trajectories[traj.sid] = traj
    recorded = str(traj.header.get("system_prompt", ""))
    for e in traj.path():
        if e.type == PROMPT_CHANGE and e.payload.get("system_prompt"):
            recorded = str(e.payload["system_prompt"])
    if recorded != self.system_prompt:     # resume 换了模板：变更落盘留痕
        traj.append(PROMPT_CHANGE, {"system_prompt": self.system_prompt, "by": "agent_attach"})
    return traj.sid
```

   没 attach 过的 sid 来了直接报错——宁可炸也不静默开一段新历史（3b
   结尾那个困境的结构性解法）：

```python
def _traj(self, sid: str) -> Trajectory:
    traj = self.trajectories.get(sid)
    if traj is None:
        raise KeyError(f"未知 session：{sid!r}——sid 由 SessionStore 分配（start/resume），"
                       "再用 agent.attach(traj) 登记")
    return traj
```

2. `_step` 的上下文从内存 list 换成投影——每次 LLM 调用前从轨迹现算，
   不缓存；rewind / 压缩之后，下一次调用自动就是新视图：

```python
def build_context(self, traj: Trajectory) -> Projection:
    return build_context(traj)   # 路径遍历 → 按类型分派 → sanitize；纯函数，逐字节可复现
```

3. 合成消息（中断标记、assistant 占位、纠正 user）照旧进事实层
   （`synthetic: true` + note），只是落点从 history 变成轨迹——否则
   "history 是 log 的投影"在合成消息这条路上断掉。

中断 / steering / redirect 的逻辑一字未动：被掐的 step 不留半截消息这条
Stage 3 纪律，在树上同样成立——append 只发生在 step 成功结算之后。
bus / events / persistence / outbound / subscribers 与 3b 一字未改；
工具层与数据源见 3c（tools.py + data/）。

4. 手动压缩接线：`compact_request` 控制事件走旁路（与 interrupt 同待遇，
   同步置标记），`_run_steps` 在 step 边界检查并调 `_compact`——算刀口 →
   摘要 → 追加 compaction entry → 发 `context_compacted`（EventLog 答
   "什么时候、压了多少"，轨迹 entry 答"刀口在哪、摘要是什么"）。
   摘要失败 fail-open：留痕（`context_compact_failed`）、不 append、
   不挡 turn，下一个边界可重试。投影零改动。

## demo
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
  user      <summary>试过查保温杯库存（3 件），结论：库存偏紧，建议按目标补货。</summary>
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



### 实测（demo 第 4 段）

```text
[实测] 残尾：完好 6 条 → 砍 11 字节后读到 5 条（停在坏记录之前，torn=True）
       → resume 裁掉残尾续写，resumed 事件留痕 torn_tail=True
[实测] 悬挂调用·补占位：投影补了 1 条占位
       → [UNKNOWN: 会话在工具执行前中断，结果缺失]；原文件字节未变：True
[实测] 悬挂审批：孤立请求 ap-deadbeef → 闭合 ['ap-deadbeef']；再扫一遍：[]
       （幂等，闭合过的不再碰）
       说明 │ 共同纪律：没写完的不算已发生（字节级）；修复只作用于喂给模型
             的投影（语义级）；补的裁决走 record 留痕，不伪造"当时批过"（审批）。
```



## 与 pi 的对照

| | pi（coding-agent） | 本章（stage04_trajectory） |
|---|---|---|
| 事实层 | entry 树，裸 jsonl 行 | entry 树，长度前缀 + CRC（残尾可判定） |
| entry 类型 | 9 种，按对 LLM 调用的影响分三组 | 10 种：9 种同构（lifecycle 换成 session_* 三个）+ `prompt_change`（pi 的 prompt 在 harness 不落盘） |
| 追加 | appendEntry：认父 + 移 leafId | 同 |
| rewind | `branch()`：leafId = to_id；`branchWithSummary`（切分支时总结被弃分支，摘要由模型生成） | 同；摘要是收的文本，模型生成与否归宿主 |
| 上下文 | buildSessionContext：路径遍历 + 分派 | build_context：同构 + sanitize 补占位收口 |
| 压缩 | CompactionEntry + firstKeptEntryId；/compact 手动 + auto 水位 | 同语义，本书叫 `keep_from_id`（"从它开始保留"）；手动触发本章落地（compact_request + Summarizer 两档），自动策略归 05 |
| 恢复 | transformMessages 收口（drop） | sanitize 补占位 + CRC 残尾 + 悬挂审批闭合 |
| session 切换 | `_rewriteFile` 克隆当前路径 | `fork()` 克隆路径 + 新 sid |

## 跑一下

```text
── 第 6 段：真跑一轮，两层事实各自记账 ──
       说明 │ 真模型 + 真工具。EventLog 记传输层的事件账（token 流、治理、
             生命周期），轨迹记会话的结构账（消息树、投影）。两层坐标不同。
[用户] 保温杯还有库存吗
[工具] ← query_inventory 结果：保温杯：库存 3 件；316L 不锈钢内胆，500ml，杯身磨砂黑。
[系统] 轨迹（尾部 6 条）：
  8c71520b ← a6b61df9  prompt_change    {"system_prompt": "…可用工具：query_inventory、…"}
  fe11fdbd ← 8c71520b  message          user: 保温杯还有库存吗
  f89418e1 ← fe11fdbd  message          assistant → toolCall(query_inventory)
  697e79f0 ← f89418e1  message          tool: 保温杯：库存 3 件；316L 不锈钢内胆…
  e3e9bf05 ← 697e79f0  message          assistant: 有的，保温杯目前还有库存，共3件…
  12d7ef0e ← e3e9bf05  session_end      {"reason": "demo 结束"}
[统计] EventLog：seq 1..141（传输层事件账）；轨迹：8 条 entry（会话结构账）；
       投影出 5 条消息，model=live
```

同一轮对话：EventLog 141 条（token 增量 + 生命周期 + 治理），轨迹 8 条
entry。**账分两层记，各答各的问题**：传输层答"事件怎么流的、谁批的"，
会话层答"模型看到了什么、从哪能回退"。

```text
── 第 13 段：长任务的轨迹解法（离线，一镜到底） ──
       说明 │ 同一张任务单（第 11 段的命运），换轨迹跑法。
[实测] branch_with_summary：移指针 + 追加遗言节点 720a9e41（挂在 cdc6d013 下）；
       抛弃 8 条 entry，原样躺在文件里——副作用（保温杯 50）在遗言里带账
[实测] 边界压缩：投影 32 → 13 条消息；compaction entry 677cd0a2
       （keep_from=ca80f7a2，reason=manual）
[系统] 摘要全文：【已完成】T-101 补货核查处理完：保温杯此前已直接补到 50
       （副作用，已确认无需重做）；玻璃杯补到 20；马克杯需补 52 件超上限、
       报备被店长拒绝、未补；保温壶补到 20。【关键规则】…【副作用】…【待办】…
[工具] ← query_inventory 结果：雨伞：库存 13 件。   ← 摘要漏了它，模型对不上账重查
[系统] 进程在工具执行前被杀：assistant 要了围巾的数据，结果永远没来
[实测] resume 后投影 15 条 = 压缩视图（[system, <摘要>, 保留窗…]），不是全量原文；
       悬挂调用补了 1 条占位：[UNKNOWN: 会话在工具执行前中断，结果缺失]
[实测] append-only：resume 前字节是 resume 后的前缀：True
[统计] 全树 58 条 entry（被抛弃分支 8 条 + compaction 原文都在）；
       当前路径 50 条；EventLog seq 1..105
```

一个摘要吞另一份摘要（幕 3 遗言进了压缩摘要）、一次冗余查询（刻意遗漏的
代价）、一次不丢历史的崩溃——三个场景，同一个纪律：修复只作用于视图，
事实层只追加。

## 总结

会话立住了、也能从崩溃里重建了，纠偏、压缩、恢复三个场景都跑通了。
但手动压缩有个天生的局限：它靠人眼判断"上下文太长了"——等你看出来，
窗口可能已经爆了；而且第二次压缩怎么办（新摘要怎么吞旧摘要、投影认哪
一刀）语义还没定义。05（压缩与上下文）接手：压缩是事件
（`context_compacted {…}` 进 EventLog）、自动触发按水位检测、滚动折叠
补全多次压缩的语义、不变式是"给定 (log, 参数版本) → 唯一 messages"。
本章留下的口子刚好够它用：`compaction` entry、`keep_from_id` 投影语义、
`maybe_compact` 的 reason 参数——自动档只是换一个触发者，机制一字不改。
