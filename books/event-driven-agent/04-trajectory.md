# Stage 5a：会话与真相 —— log、投影与异常恢复

> 配套代码：`src/baby_event_driven_agent/stages/stage04_trajectory/`


前面几章，一直在处理的是用户的新增消息的输入。
这一节，我们来给agent增加上从已有session中恢复(reload)，以及在对话中跳转到指定消息的能力(rewind)；
要支持如上的能力，必须依赖咱们一开始就在记录的会话轨迹(trajectory)，轨迹作为agent运行阶段的真实记录，不仅是agent会话管理的数据来源，还是进行用户记忆整理，agent能力评测等等功能的基础；
不过咱们初始设计的轨迹还比较简单，这次咱们一起来改造他。

## Trajectory 和 messages 的关系
在进入改造之前，我们先搞清楚轨迹trajectory和与LLM交互的messages的关系；
trajectory是agent操作过程中的事实记录，每一步用户和模型的动作都被原封不动的记录下来，也就是咱们一直在说的append-only；
这些操作不光是和LLM相关的交互，也包括agent本身的一些设置，比如切换模型，切换thinking level等等；
```bash
/model
/thinking
```
这些操作记录在trajectory中，并不需要将这些操作作为独立的messages发送给LLM，
所以，messages是当前session trajectory的子集。
trajectory是agent 运行期间的唯一事实标准，完整的记录了在agent运行期间影响与LLM交互的所有操作，而messages是可以从trajectory还原出来的直接与LLM交互的消息，当下主流的agent都设计了从trajectory到messages的投影转换。

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

- **id**：UUID（`uuid4().hex[:8]`），会话内唯一，rewind / fork 的寻址坐标。
- **parent_id**：父 entry 的 id，根节点为 `None`。
- **type**：即如上类型分组，投影函数按它分派。
- **ts**：trajectory记录的时间戳。
- **payload**：内容按 `type` 来区分, 比如`message`，存的是喂给模型的和模型吐出的原始数据。

entry的type可选的类型大致如下，要根据type来决定是否在投影生成 message context使用该entry，

| 组    | 装什么                                                              | 谁消费它              |
| ---- | ---------------------------------------------------------------- | ----------------- |
| 进上下文 | message / branch_summary / compaction                            | 模型（经投影）           |
| 改状态  | model_change / thinking_level                                    | 投影函数（不产生消息，覆盖式提取） |
| 纯元数据 | session_started / session_resumed / session_end / label / custom | 回放的人和 UI          |


### messages--trajectory的投影

trajectory 定下来之后，来看看如何通过轨迹来生成messages。

1. **路径遍历**：从 leafId 沿 parentId 走回根，再 reverse 成根→叶顺序。
2. **按 type 分派**：message 转成消息，model_change 更新当前模型，
   thinking_level 更新模型思考等级，元数据跳过，
   如果message经过压缩，那么就从压缩点记录的位置开始取entry。
3. **sanitize**：保证发给模型的消息序列合法，处理agent异常退出时可能写入的不完整trajectory。

> 从轨迹加载(reload)回上下文，有一个小设计：system prompt 总是取最新的，而不是
> 直接采用轨迹 header 里记的那份。原始记录的system prompt，用在回放诊断的场景。


### 有压缩的轨迹怎么组织、怎么读
如果messages经过压缩，具体是如何添加压缩entry以及如何从trajectory中恢复messages；

压缩不删任何东西，触发上下文压缩时，轨迹树上会添加一个 `compaction` entry，payload 装两样东西——`summary`（被压掉掉的messages的摘要）和 `keep_from_id`（边界指针：从哪条entry起原样保留，即keep_from_id这条轨迹之前的messages，都被compaction的summary替代）。

在如下轨迹样例中，压缩发生时对话已经走到 e5，compA **追加在那一刻路径的末尾**(压缩e1+e2，保留最近滑窗 e3+e4)

```text
压缩发生时：  e1 e2 | e3 e4 e5 [compA(keep_from=e3)]
              摘要   原样保留    ↑ 追加在末尾（刀口在 e2|e3 之间）

之后继续对话：e1 e2 | e3 e4 e5 compA e6 e7 ...
                                    ↑ 新对话接在 compA 之后
```

压缩完成后，后续messages从trajectory恢复时，
1. **`keep_from_id` 之前的messages不再加载到对话中;
2. **摘要插在system prompt之后**，最终顺序是 `[system, summary, e3, e4, e5, e6 ...]`

>有一些agent会把summary 放到system prompt，咱们先不这样做
### rewind 与 fork——切换不修改历史，只创造新的"当前"
下面再看看rewind操作是如何影响trajectory的，
**rewind**（`branch(to_id)`）的实现是一行赋值：`self.leaf_id = to_id`。
rewind后，从rewind点拉出session分支，如下所示，从entry4 rewind到entry3后，新的entry5的parent是3，rewind操作后，后续的轨迹内容，就继续在entry5后生长。

```text
1 → 2 → 3 → 4          branch(3) 前的原路径
        └→ 5           branch(3) 后追加：3 有两个孩子（4 和 5）
```

再看看，如果轨迹上存在压缩节点，需要如何处理。

拿压缩后的轨迹看两个落点，如下轨迹中，压缩发生时对话走到 e5，压缩entry 压掉 e1、e2，之后又新增了 e6、e7、e8 3条messages，

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
这个summary是rewind操作的一种可选项，比如pi agent通过`tree`切换对话分支时，就提供了这样的能力。

下面看看fork，
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

### 异常恢复——三级收口
再来看看从trajectory恢复session时的异常保护；
假如agent在运行时出现了异常导致进程挂掉，trajectory可能就会不完整，

**1. 单条轨迹的格式不完整**——死在一条记录中间

```text
崩溃时：    e1 e2 e3 [半条记录，CRC 对不上]        ← 残尾，不是事实
resume 后： e1 e2 e3 session_resumed{torn_tail: true}
            ↑ 残尾字节裁掉（没写完的不算事实），痕迹记在 resumed 里
```

**2. messages不满足匹配条件**——死在 assistant 落盘后、工具结果落盘前：

```text
崩溃时：    ... user → assistant(tool_calls c1)          ← 结果永远没来
resume 后： ... user → assistant(tool_calls c1)
                     → session_resumed → user("继续")     ← 文件里永远悬挂
投影时才补：assistant → tool[UNKNOWN: 会话在工具执行前中断，结果缺失]
            ↑ 修复只发生在投影，模型知道缺了什么、能自己重调
```

也可能是需要权限审批的工具调用时，没有及时拿到审批信息，agent就意外终止，导致轨迹记录的messages格式不匹配，

```text
崩溃时（EventLog）：   ... approval_required{request_id: ap-x}    ← 没有配对的 decided
resume 后（EventLog）：... approval_required
                       → approval_decided{action: abandoned}      ← bus.record 补，幂等
```

处理的方式，都是在从trajectory resume消息时，主动的将不完整的轨迹补充完整，并且添加上主动补充的说明。
## 代码改动：轨迹层三个类，agent 侧只动两处

轨迹层就三个类，各管一层：`Entry` 是树上的节点，`Trajectory` 是轨迹文件的内存镜像，`TrajectoryLog` 管单会话文件，

### Entry：树上的节点

```python
@dataclass(frozen=True)
class Entry:
    id: str
    parent_id: str | None
    type: str
    ts: str
    payload: dict[str, Any]
```

它是 frozen 的，不允许修改。`type` 就是前面那张三分组表里的 10 种；`to_dict() / from_dict()` 负责与落盘行的互转。

### TrajectoryLog：单会话轨迹文件

轨迹文件按照 `<sid>.jsonl`命名，第一行是 session header（type=session，带 sid / cwd / system_prompt 原文，注意 header 不是树节点），其后 entry 逐行追加。
提供三个关键方法

- `append(record)`：一行落盘（单线程下原子）；
- `read()`：读全文件，返回 `(header, entries, torn)`，撞上残尾停在最后一条完好处；
- `truncate_torn()`：把残尾字节裁掉——没写完的不算事实。

这里的容错策略是对 pi 的有意偏离：pi 用"临时文件 + 原子重命名"从源头杜绝残尾，读到坏行直接抛错（fail-fast），悬挂调用整条 drop；本项目反过来——容忍残尾、裁掉留痕、投影补占位，理由见 demo 6：修复要可审计，模型要知道缺了什么。


### Trajectory：文件的内存镜像（一棵 entry 树）

状态只有三样：`header`、全树索引 `_by_id`、一个指针 `leaf_id`。所有操作围绕这三个状态，核心是 append 和 path：

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

def path(self) -> list[Entry]:
    """当前路径：leaf 沿 parentId 走回根，再 reverse 成根→叶顺序。
    只有这条线上的 entry 会进投影——其他分支的数据不是"被过滤"，
    是遍历根本不经过它们。
    """
```

- **append**：认父不认子（新节点只带 parent_id），先落盘后动内存；`_index` 顺带做合法性检查（id 冲突、父节点不存在直接抛错）。
- **path()**：投影要的当前路径就从这来。
- **branch(to_id)**：rewind 的全部实现就一行 `self.leaf_id = to_id`，没有任何节点被删除。
- **branch_with_summary(keep_from, summary)**：移指针 + 追加一条 `branch_summary` 遗言节点。
- **fork(new_log, sid=…)**：把当前路径原样克隆进新文件（id / parentId 不改），补一条 `session_resumed` 留痕。

### agent 侧：只动两处

agent侧实现从轨迹到messages的投影，

**1. 消息存储的映射：按照sid(sessionid)管理轨迹，

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

没 attach 过的 sid 来了直接报错（`_traj`）。`_step` 的上下文从内存 list 换成投影：每次 LLM 调用前 `build_context(traj)` 现算，不缓存，rewind / 压缩之后，下一次调用自动就是新视图。

**2. system prompt 的替换：prompt 是参数，变更要留痕。**
reload session时，system prompt 不直接从轨迹中恢复，但它**存在轨迹文件里**：初始值记在 header（第一行，不是树节点），变更以 `prompt_change` entry 追加进轨迹树。这样"这个会话当时用的是哪个 prompt"随时查得到，否则重放核对不了。做法与 model_change 同一个模式：

- 初始值记在 header（`TrajectoryLog` 的第一行）；
- 变更以 `prompt_change` entry 追加——`attach` 发现本次运行的 prompt 与轨迹记录的不一致（比如 resume 时换了模板），就补一条留痕，新 prompt 从此生效；
- 投影时**覆盖式提取**：沿当前路径走，取最后一次 `prompt_change`；没有变更回落 header。

```python
elif e.type == PROMPT_CHANGE:
    p_ = str(e.payload.get("system_prompt", ""))
    if p_:
        prompt = p_  # 覆盖式提取：路径上最后一次生效
...
if prompt is None:
    prompt = str(traj.header.get("system_prompt", ""))  # 没变更过：回落 header
```

其余一切照旧：合成消息（中断标记、assistant 占位、纠正 user）同样进轨迹。


## demo

先认识一下跑 demo 的 agent。之前它只有四件套工具——查库存（query_inventory）、查规则（search_rules）、改库存（update_inventory）、改规则（update_rules）。现在给它加了一个任务域：list_tasks 列出今天的任务单，get_task 取单上的完整条目，扩成六件套（4 读 + 2 写）；写操作的审批也从"每个写都问人"升级为按量判——小补货直接放行，超过 50 件才要店长审批。于是它第一次能"领单 → 逐项处理 → 汇报"地跑长任务，来支持演示咱们的轨迹记录的演示。

下面从六个 demo看一下各种操作之后的轨迹长什么样，按"写 → 恢复 → 压缩 → 回退 → 分叉 → 崩溃恢复"的顺序把轨迹层的每个能力过一遍。
六个 demo 全部打真实模型（读仓库根 `.env` 的 OpenAI 兼容端点，本地 ollama 也行；没配 key 的段整段跳过）。
entry id 每次运行随机生成，模型输出每次也会不同，下面的引用是一次真实运行。

### demo 1 · 写轨迹：写好的 entry 长什么样（01-trajectory-shape）

先说这个 case 里 agent 在干什么：**用户问保温杯库存 → LLM 发起工具调用 query_inventory → agent 执行工具 → LLM 拿到结果回复**。四步对话各记一条 message entry，加上开头记录的 session_started（会话开始）和 model_change（模型选择），正好 6 条 entry。

```text
[用户] 保温杯还有库存吗
[entry] cce21772 ← ∅  session_started  {"by": "store"}
[entry] c3220530 ← cce21772  model_change
       {"model_id": "modelscope.cn/unsloth/Qwen3.5-4B-GGUF:Q4_K_M", "by": "store"}
[entry] 80d4ac1f ← c3220530  message
       {"message": {"role": "user", "content": "保温杯还有库存吗"}, "synthetic": false, "note": ""}
[entry] f950df0c ← 80d4ac1f  message
       {"message": {"role": "assistant", "content": null, "tool_calls": [{"id": "call_z7xvdqpf",
        "type": "function", "function": {"name": "query_inventory", "arguments": "{\"category\":\"保温杯\"}"}}]},
        "synthetic": false, "note": ""}
[entry] c9617b5d ← f950df0c  message
       {"message": {"role": "tool", "tool_call_id": "call_z7xvdqpf",
        "content": "保温杯：库存 3 件；316L 不锈钢内胆，500ml，杯身磨砂黑。"}, "synthetic": false, "note": ""}
[entry] 76953da3 ← c9617b5d  message
       {"message": {"role": "assistant", "content": "保温杯还有库存，目前有 3 件：材质 316L 不锈钢内胆、
        容量 500ml、外观杯身磨砂黑"}, "synthetic": false, "note": ""}
[统计] 文件 6 条 entry + 1 条 header（不是节点，type=session）；
       message 里 1 user / 2 assistant / 1 tool；残尾=False
[系统] 文件头原文：{"type": "session", "version": 1, "id": "992b5c5e…",
       "cwd": "…/sessions/stage04/trajectory-shape", "created": "2026-09-27T07:31:50.685+00:00",
       "note": "", "system_prompt": "你是一个通过工具干活的通用 agent。"}
```

### demo 2 · 从正常轨迹恢复（02-resume-normal）

先说这个 case 里 agent 在干什么：**和 demo 1 一样跑一轮"问库存 → 工具 → 回复"，写下轨迹后关掉会话；进程重启后，新 agent 拿着 sid resume 这张轨迹；用户接着问"刚才查的是哪个品类？"，agent 凭恢复的历史答出保温杯**。轨迹的变化：6 条 entry 上先落一笔 session_end，resume 时再补一笔 session_resumed 留痕，8 条。

```text
[用户] 保温杯还有库存吗
[entry] 1937b045 ← ∅  session_started  {"by": "store"}
[entry] 4f12847a ← 1937b045  model_change
       {"model_id": "modelscope.cn/unsloth/Qwen3.5-4B-GGUF:Q4_K_M", "by": "store"}
[entry] 4b3809d0 ← 4f12847a  message
       {"message": {"role": "user", "content": "保温杯还有库存吗"}, …}
[entry] 2e7dee48 ← 4b3809d0  message
       {"message": {"role": "assistant", "content": null, "tool_calls": [
        {"id": "call_i1vxc0tz", "function": {"name": "query_inventory",
         "arguments": "{\"category\":\"保温杯\"}"}}]}, …}
[entry] 399284bc ← 2e7dee48  message
       {"message": {"role": "tool", "tool_call_id": "call_i1vxc0tz",
        "content": "保温杯：库存 3 件；316L 不锈钢内胆，500ml，杯身磨砂黑。"}, …}
[entry] c96b8357 ← 399284bc  message
       {"message": {"role": "assistant", "content": "有库存，目前有 3 件。…"}, …}
[实测] 6 条 entry + 1 条 header——和 demo 1 的那张轨迹对得上
[实测] 关闭时：投影 5 条消息；进程到此结束，内存里什么都可以扔了
[实测] resume 重建树：8 条 entry（原 6 条 + resumed 留痕）；投影与关闭前逐字节相同：True
[entry] c650414a ← c96b8357  session_end      {"reason": "第一段对话结束"}
[entry] d38077d4 ← c650414a  session_resumed  {"torn_tail": false, "note": "进程重启后恢复"}
[用户] 刚才查的是哪个品类？
[实测] 继续对话：上下文 7 条（system + 恢复的历史 + 新一轮），
       回答：刚才查询的品类是保温杯。
       说明 │ 读文件重建树 + attach 登记。上下文不是从内存拿的——
             每次 LLM 调用前从轨迹投影现算，内存丢了，对话丢不了。
```

### demo 3 · 触发压缩之后的操作（03-compact）

先说这个 case 里 agent 在干什么：**用户连问三轮（保温杯 → 玻璃杯 → 汇总），agent 每轮都发起工具调用、一轮轮把上下文堆长；用户喊压一下，agent 在下一个 step 边界把前两轮压成一份摘要**——追加一个 compaction entry（摘要 + 刀口 keep_from_id），原文一个字节不删；之后的"继续"，模型看到的就是 [system, <摘要>, 保留窗] 的短视图。压缩了哪些，三样东西摆在一起看：compaction entry 原文、被压进摘要的消息清单、压缩后的投影。

```text
[用户] 保温杯还有库存吗 / 玻璃杯呢 / 帮我汇总一下
[用户] 上下文有点长了，压一下（compact_request，下一个边界生效）
[用户] 继续
[实测] 边界压缩：投影 11 → 13 条消息；compaction entry 6edb5431
       （keep_from=2b46d2c5，reason=manual）——被压的原文一个字节没动：全树 24 条 entry 都在
[entry] {"type": "compaction", "id": "6edb5431", "parentId": "2b46d2c5", …,
       "payload": {"summary": "【已完成】已查询并汇总保温杯与玻璃杯的当前库存信息。
                    【关键事实与规则】保温杯：库存 3 件（316L 不锈钢内胆，500ml，磨砂黑）；
                    玻璃杯：库存 17 件（高硼硅玻璃，400ml，可进微波炉）。…",
                   "keep_from_id": "2b46d2c5", "reason": "manual"}}
[系统] 被压进摘要的消息（keep_from 之前，原文仍在轨迹里）：
  user      保温杯还有库存吗
  assistant → toolCall(query_inventory)
  tool      保温杯：库存 3 件；316L 不锈钢内胆，500ml，杯身磨砂黑。
  assistant 保温杯目前有 3 件的库存。规格：316L 不锈钢内胆，500ml，杯身磨砂黑
  user      玻璃杯呢
  assistant → toolCall(query_inventory)
  tool      玻璃杯：库存 17 件；高硼硅玻璃，400ml，可进微波炉。
  assistant 玻璃杯目前有 17 件的库存。规格：高硼硅玻璃，400ml，可进微波炉
  user      帮我汇总一下
  assistant 库存汇总报告：保温杯 3 件、玻璃杯 17 件…
[系统] 压缩后的投影（模型实际看到的）：
  system    你是一个通过工具干活的通用 agent。
  user      <summary>【已完成】已执行两类产品（保温杯、玻璃杯）的库存查询，并汇总了结果反馈给用户。\n\n【关键事实与规则】保温杯：3 件（316L 内胆/500ml/磨砂黑）；玻璃杯：17 件（高硼硅/400ml/可微波）。\n\n【副作用】无。\n\n【待办】无明确后续任务，等待用户进一步指令或查询需求。</summary>
  user      继续
  …         （保留窗：最后一轮的原文，tool 配对完整）
       说明 │ 压缩只追加视图标记：compaction 的 payload = summary + keep_from_id
             （从哪条起原样保留）。投影遇到它：刀口之前跳过、摘要插在 system 之后、
             只认当前路径上第一条（折叠语义归 05）。
       说明 │ 这轮保留窗（最后一轮）比被压段还长，条数没降反升——压缩的收益
             取决于被压段和保留窗的实际长度，机制本身不变。
```

### demo 4 · 压缩之后再 rewind，然后继续对话（04-rewind-after-compact）

先说这个 case 里 agent 在干什么：
用户领了 T-101 任务单，agent 跑三轮（定方案 → 逐项核对）后边界压缩；
用户希望"换个思路重来"，agent 把指针 rewind 回第一条 user message；
再 branch 回压缩节点，又能回到压缩刚做完那一刻。同一个文件，两个落点两种视图。

```text
[用户] 开始处理 T-101，先定个方案 / 继续 / 继续
[实测] 压缩完成：compaction entry de949a3f（keep_from=71900475）；投影 14 条 = [system, <摘要>, 保留窗…]
[实测] 落点一 branch(e9c8ac63)：文件字节未变：True；投影 2 条 = [system, 那条 user]
       ——compaction 不在路径上，旧消息逐字回来
  system    你是一个通过工具干活的通用 agent。
  user      开始处理 T-101，先定个方案
[用户] 换个思路重来：先查规则再动手
[实测] 回退后继续对话 = 分支：e9c8ac63 现在有两个孩子 ['f6fd78af', 'd97fce44']
[实测] 落点二 branch(de949a3f)：投影 14 条 = 回到压缩刚做完那一刻：[system, <摘要>, 保留窗…]
  system    你是一个通过工具干活的通用 agent。
  user      <summary>【已完成】已处理任务 T-101…因人工确认超时，马克杯暂未补…</summary>
  user      继续
  …         （保留窗原文）
       说明 │ 两个落点，文件都一个字节没动；差别只在 leaf 指针走到哪、
             投影因此算出什么。被抛弃的分支原样躺在文件里，随时能 branch 回去。
```

### demo 5 · fork（05-fork）

先说这个 case 里 agent 在干什么：**agent 跑完一轮"问库存 → 工具 → 回复"后，把当前路径克隆进一份新会话文件；之后旧会话续一句、新会话也续一句，两条轨迹各自生长，互不可见**——fork 出来的新文件 id 与 parentId 原样保留，是完整合法的轨迹，旧文件原封不动。

```text
[系统] store 里的会话：['2675d6a5…', '62ca1dba…']
[实测] 原会话 2675d6a5…：7 条 entry，最后一条 user = 旧会话的下一句
[实测] 分叉 62ca1dba…：9 条 entry，最后一条 user = 新会话的下一句
[实测] 分叉的生命周期事实：{"type": "session_resumed", "forked_from": "2675d6a5…"}
       说明 │ session 切换不修改历史，只创造新的"当前"：模型切换是树上的
             新节点，会话切换是新文件——都是追加，都不是改写。
```

### demo 6 · 从有问题的轨迹 resume（06-resume-broken）

先说这个 case 里 agent 在干什么：**demo 主动构造两份不完整的轨迹，各自先摆"坏轨迹"原文、再摆"修好之后"的样子**——
1. 轨迹1最后一条entry因为agent运行时异常导致没有被完整记录，resume这个session时，删除掉不完整的信息，补上 session_resumed 留痕，并直接补一条 assistant 占位 entry 进轨迹（synthetic 标记区分主动补充）；
2. 轨迹里躺着一条"assistant 要了工具结果但结果永远没来"的悬挂调用（崩在工具执行前），投影补一条自描述占位，模型知道缺了什么，原文件字节不变。两个现场全部以"轨迹是唯一真相"为基准。

```text
# 轨迹1
[entry] fee3b7b9 ← ∅         session_started  {"by": "store"}
[entry] 2627956d ← fee3b7b9  model_change     {"model_id": "qwen3.5:4b-32k", …}
[entry] 856fbbf6 ← 2627956d  message          user 保温杯还有库存吗
[entry] 89ecc191 ← 856fbbf6  message          assistant → toolCall(query_inventory)
[entry] 09409dbf ← 89ecc191  message          tool 保温杯：库存 3 件；316L 不锈钢内胆，500ml，杯身磨砂黑。
[entry] b3648adf ← 09409dbf  message           {"type": "message", "id": "b3648adf",
       "parentId": "09409dbf", …, "content": "保温杯现在还有库存，现有库存为3件，
       具体规格是316L不锈钢内胆，容量500ml，杯�                       ← 正文说到一半戛然而止
[实测] 完好 6 条 → 只读到 5 条（停在坏记录之前，torn=True）。
       残尾声明的父节点就是上面最后一条 entry——它是没出生的第 6 条，没写完的不算已发生
[实测] 修好之后：resume 先把残尾字节物理裁掉（文件 1873 → 2155 字节），补 session_resumed 留痕
       + 主动补一条 assistant 占位 entry 进轨迹，轨迹尾部三条：
[entry] 09409dbf ← 89ecc191  message          tool（原样还在）
[entry] 0c81bb4b ← 09409dbf  session_resumed  {"torn_tail": true, "note": "crash 恢复演练"}
[entry] 19364757 ← 0c81bb4b  message          assistant [UNKNOWN: 会话崩溃在回复写到一半，该回复已丢弃]
       （"synthetic": true, "note": "主动补充的占位：崩溃时写到一半的回复已被裁掉"）
[实测] 修好之后的投影（模型实际看到的）——占位已在轨迹里，投影照常透传：
  system    你是一个通过工具干活的通用 agent。
  user      保温杯还有库存吗
  assistant （content=null，等工具结果）
  tool      保温杯：库存 3 件；316L 不锈钢内胆，500ml，杯身磨砂黑。
  assistant [UNKNOWN: 会话崩溃在回复写到一半，该回复已丢弃]
       说明 │ 占位是主动修复，直接进事实层：content 自述"回复已丢弃"，
             synthetic 标记区分主动补充——模型知道上一条回复没说完，
             而不是以为对话天然停在 tool 结果。

# 轨迹2
[实测] 现场二坏轨迹：末尾是 assistant 的 toolCall，底下没有 tool 回执：
[entry] f8ae067f ← 6dfd8cf6  message          user 把库存改成 45 件
[entry] 5b1b5674 ← f8ae067f  message          assistant → toolCall(update_inventory)
[实测] 修好之后的投影（模型实际看到的）：
  system    你是一个通过工具干活的通用 agent。
  user      把库存改成 45 件
  assistant （content=null，等工具结果）
  tool      [UNKNOWN: 会话在工具执行前中断，结果缺失]
[实测] 占位只补在投影里，原文件字节未变：True
       说明 │ 两处的修复分级：现场一是字节级修复（残尾没写完、从来不是事实，
             物理裁掉重写）；现场二是语义级修复（悬挂调用是已发生的事实，
             文件一个字节不动，只改投影）。占位都自描述、修复都留痕——
             轨迹是唯一真相，坏的地方用视图补，修的过程可审计。
       说明 │ 与 pi 的分歧：pi 原子写入让残尾不可能出现，读到坏数据就抛错、
             悬挂调用整条 drop；本项目选择容错修复路线——能修就修、
             修必留痕、模型必须知道缺了什么。是设计选择，不是疏漏。
```


## 总结

如上就是关于trajectory的内容，为了便于演示压缩后的trajectory，本章agent也增加了基础的上下文压缩能力，下一章再详细讨论。