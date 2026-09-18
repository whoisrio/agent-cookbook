# Stage 3：打断与转向（interrupt + redirect）

> 配套代码：`src/baby_event_driven_agent/stages/stage03_interrupt/`，
> `stage03-demo` 跑演示（真模型），`stage03-test` 跑测试（同样打真模型，
> 不设替身——本章要验的就是真 provider 收不收这套消息形状）。
> 本 stage 保留 stage02 的全部能力（收件箱、steering、followup），在此之上加一种新东西：
> 打断在飞的一步，并决定掐完之后是结束（interrupt）还是原地转向（redirect）。

## 需求

回答还在跑，但用户已经不想要这个回答了：问错了、跑偏了、等不及了。
用户按停止，正在飞的那一步要能停下来。

现有设计接不住这个需求。
stage02 里正在飞的 step 是一个普通的 await：模型请求一旦发出，代码就在流消费循环里等增量，
没有任何入口能让它提前结束。
收件箱也帮不上忙——往收件箱里投一条“停止”没有用，它只会被当成一条普通消息排队，
而排队就意味着等，等就违背了停止的本意。

所以中断和消息是两种东西，车道要分开：
消息进收件箱，排队，在 step 边界被消费，语义由消费时机决定；
中断是控制信号，不排队，直接作用在正在飞的那一步上。

## 中断发生时agentloop的状态，影响如何处理中断
要支持中断和转向，首先要搞清楚收到中断信号的那一刻的，agent正在处理的任务要如何处理。
一个必须遵从的原则，就是与LLM交互的messages，在LLM调用tools的loop过程中，assistant的消息和tool的消息必须成对的。
处理中断消息，可能有如下时点，
- 发送消息给LLM，LLM还未返回
- 发送消息给LLM，LLM输出thinking token
- 发送消息给LLM，LLM要求使用tools，tools信息输出中，还未执行工具
- tools执行中，可能存在分批次执行，部分tool完成执行，tool执行到一半，tool在等待被调度
- tools全都完成执行，回复给LLM
- LLM输出最终结果中 

### 每种情况下 history 长什么样

下面用用 /Chat/Completions 能直接发出去的消息列表来说明在如上各个状态下，收到stop或者redirect信号后 messages如何处理；
一个默认的规则是，在收到stop或者redirect后，补充message占位信息，需要主动添加被打断的描述，如下: 
- **stop 的中断标记**：一条**独立**的 user 消息，内容`[本轮已被用户中断，不要回答上面那条问题]`。作用是别让那条没回答的 user 在下一轮
  被重新消费。
- **redirect 的上下文标注**：**折进纠正 user 的 content**，形如  `[上一轮回答被用户打断，以下是用户的纠正]\n\n<纠正文>`。作用是让模型知道前面那条 assistant 是残缺的、这段是纠正。
下面逐个场景看一下，

#### 1.已发 LLM、未回复
场景1,已发 LLM、未回复，
收到stop：assistant 侧整个丢；但**不能把那条 user 就这么晾在尾部**——下一轮模型看到一个没回答的问题，会自己去答（demo 里"报销"被翻出来重答就是这个）。末尾补一条**中断标记**：
```json
[
  {"role": "user", "content": "报销有什么规定"},
  {"role": "user", "content": "[本轮已被用户中断，不要回答上面那条问题]"}
]
```

redirect：补一条纠正 user，并把上下文标注折进去：
```json
[
  {"role": "user", "content": "报销有什么规定"},
  {"role": "user",
   "content": "[上一轮回答被用户打断，以下是用户的纠正]\n\n先别查了，改成订会议室"}
]
```

>连续两条 user 是合法的；要对齐 role 交替，就在中间插 `{"role": "assistant","content": ""}`。
>不同的agent处理的方式略有不同，比如claude在这个场景下的处理方式如上，pi是补上了 assistant的消息。

#### 2.只在吐 thinking
场景2，LLM已经在响应请求，吐出thinking的内容，
stop 同上，thinking信息不记录。
redirect 要先补一个空壳 assistant 占位，redirect消息里补上纠正信息：
```json
[
  {"role": "user", "content": "报销有什么规定"},
  {"role": "assistant", "content": "[response interrupted]"},
  {"role": "user",
   "content": "[上一轮回答被用户打断，以下是用户的纠正]\n\n先别查了，改成订会议室"}
]
```

#### 3.模型要调工具，参数还没吐完，还未真正执行工具（流里已经出了 `tool_call_delta`）
场景3，模型要调工具，参数还没吐完，还未真正执行工具（流里已经出了 `tool_call_delta`）
收到stop信号，仍然将不完整的LLM输出丢弃，而后补上打断占位消息。半截的 `tool_call` 不是合法消息（`arguments` 断在半路），也没执行过：

```json
[
  {"role": "user", "content": "报销有什么规定"},
  {"role": "user", "content": "[本轮已被用户中断，不要回答上面那条问题]"}
]
```

redirect：同样整步丢，只补一条带标注的纠正 user：

```json
[
  {"role": "user", "content": "报销有什么规定"},
  {"role": "user",
   "content": "[上一轮回答被用户打断，以下是用户的纠正]\n\n先别查了，改成订会议室"}
]
```

如上3种场景的处理方式是一致，在没有收到完整的LLM返回，收到中断信号就当没收到处理。

#### 4. tool 执行中
对于已经在执行的tool，目前咱们的设计规则是让已经在执行的tool执行完，没有执行的，就不再执行。
>当然，生成场景，有一些会改动状态的工具，实际上是应该支持被中断的，这个复杂的场景，作为后续优化的一个关键需求点先记录下来。

假设模型一次发起三个检索：`call_1` 已返回，`call_2` 正在跑（等它跑完），`call_3`还没开始。
stop：真实结果照留，没开始的补占位，然后直接收尾（不再开下一批）；末尾补一条
**assistant 封口占位**——尾部停在 `tool` 上，缺的是"这个 turn 由 assistant 收尾"
（前面几格尾部是 `user`，补的才是 user 中断标记）。
```json
[
  {"role": "user", "content": "报销、VPN、会议室分别怎么弄"},
  {
    "role": "assistant",
    "content": null,
    "tool_calls": [
      {"id": "call_1", "type": "function",
       "function": {"name": "search_rules", "arguments": "{\"query\": \"报销\"}"}},
      {"id": "call_2", "type": "function",
       "function": {"name": "search_rules", "arguments": "{\"query\": \"VPN\"}"}},
      {"id": "call_3", "type": "function",
       "function": {"name": "search_rules", "arguments": "{\"query\": \"会议室\"}"}}
    ]
  },
  {"role": "tool", "tool_call_id": "call_1",
   "content": "报销：每月 25 号前提交，超 500 元需发票原件。"},
  {"role": "tool", "tool_call_id": "call_2",
   "content": "VPN：内网 Portal → 自助服务 → 远程接入，需部门经理审批。"},
  {"role": "tool", "tool_call_id": "call_3",
   "content": "[被用户中断，未执行]"},
  {"role": "assistant", "content": "[本轮已被用户中断，不再基于上面的工具结果作答]"}
]
```
redirect：和 stop **只差最后一条**——正在跑的 `call_2` 一样等它跑完，没开始的`call_3` 一样补占位（不新起调用），只是末尾不放中断标记，换成纠正 user，turn 不结束。注意这里**不加"被用户打断"那段标注**：assistant 自己的输出
没有被切断（`tool_calls` 是完整的、工具也正常跑完了），语义上这就是steering。

```json
[
  {"role": "user", "content": "报销、VPN、会议室分别怎么弄"},
  {
    "role": "assistant",
    "content": null,
    "tool_calls": [
      {"id": "call_1", "type": "function",
       "function": {"name": "search_rules", "arguments": "{\"query\": \"报销\"}"}},
      {"id": "call_2", "type": "function",
       "function": {"name": "search_rules", "arguments": "{\"query\": \"VPN\"}"}},
      {"id": "call_3", "type": "function",
       "function": {"name": "search_rules", "arguments": "{\"query\": \"会议室\"}"}}
    ]
  },
  {"role": "tool", "tool_call_id": "call_1",
   "content": "报销：每月 25 号前提交，超 500 元需发票原件。"},
  {"role": "tool", "tool_call_id": "call_2",
   "content": "VPN：内网 Portal → 自助服务 → 远程接入，需部门经理审批。"},
  {"role": "tool", "tool_call_id": "call_3",
   "content": "[被用户中断，未执行]"},
  {"role": "user", "content": "先别查了，改成订会议室"}
]
```

#### 5. tool 刚好跑完（取消没赶上）
如果取消信号到的那一刻，这一批工具已经全部拿到真实结果。LLM还没有给出完整的最终的回答。
**stop 在这里要生效**：直接补一条"封口"的 assistant 占位把 turn 收掉。

```json
[
  {"role": "user", "content": "报销和 VPN 分别怎么弄"},
  {
    "role": "assistant",
    "content": null,
    "tool_calls": [
      {"id": "call_1", "type": "function",
       "function": {"name": "search_rules", "arguments": "{\"query\": \"报销\"}"}},
      {"id": "call_2", "type": "function",
       "function": {"name": "search_rules", "arguments": "{\"query\": \"VPN\"}"}}
    ]
  },
  {"role": "tool", "tool_call_id": "call_1",
   "content": "报销：每月 25 号前提交，超 500 元需发票原件。"},
  {"role": "tool", "tool_call_id": "call_2",
   "content": "VPN：内网 Portal → 自助服务 → 远程接入，需部门经理审批。"},
  {"role": "assistant", "content": "[本轮已被用户中断，不再基于上面的工具结果作答]"}
]
```

redirect：这一格已经是 step 边界，**没有残破消息可修**（工具结果是真的、
assistant 的 `tool_calls` 也是完整的），所以这里天然就是steering的逻辑了。

```json
{"role": "user", "content": "先别查了，改成订会议室"}
```

#### 6. 模型在回答，只说了一半（流里只有 `text_delta`）

工具跑完、模型开始写**最终回答**（没有工具的 turn 就是直接作答）——这一步流里
只有 `text_delta`、没有 `tool_call_delta`。半句文本已经上屏、还没进 history；
结算就是把它存成一条 text-only 的 assistant。

stop（留）：
```json
[
  {"role": "user", "content": "报销有什么规定"},
  {"role": "assistant", "content": "报销需要在每月 25 号前提交，另外"},
  {"role": "user", "content": "[本轮已被用户中断，不要回答上面那条问题]"}
]
```
（`content` 是打断那一刻已经上屏的半句，没有 `tool_calls`。）

stop（丢）就是 ② 的 stop。redirect：可见文本留成 assistant，纠正 user 折标注
（Hermes 还会把"打断前已输出的内容"再抄一份进 user，这里省略）：
```json
[
  {"role": "user", "content": "报销有什么规定"},
  {"role": "assistant", "content": "报销需要在每月 25 号前提交，另外"},
  {"role": "user",
   "content": "[上一轮回答被用户打断，以下是用户的纠正]\n\n先别查了，改成订会议室"}
]
```


### 工具接口：把"能不能中断"预留出来
为了后续让工具也支持打断，顺便给工具添加一个能否处理打断的能力描述方法，
通过on_interrupt来描述工具收到打断信号的行为，
-cancel,可以打断，工具自行处理回滚
-yield，不打断，可以转后台执行，执行结果可忽略
-block，不能打断，必须等待工具执行完成，目前全都按照这个方式来处理。

（这是**预留的接口**，本章代码里还没落地：`llm.TOOLS` 现在就是三个普通 async
函数，没有 Protocol、没有 `on_interrupt`。实现上等于它们全是 block——
`_phase == "tools"` 那一段不掐正在跑的工具，只给没开始的调用补"未执行"占位。
等真要支持 cancel / yield，把这个 Protocol 加上、让 `_run_step` 在掐之前问一句
即可，turn 侧的收尾逻辑不用改。）

```python
from typing import Literal, Protocol

class Tool(Protocol):
    name: str
    def execute(self, args: dict) -> str: ...

    def on_interrupt(self) -> Literal["cancel", "yield", "block"]:
        """中断到达时工具自报家门；默认 block：不可中断，跑完为止。"""
        return "block"
```

### 结束原因要落到 session log

session log 也得把 turn 是怎么结束的说明白。三种结局各留一条轨迹：

```text
掐在飞：  user_interrupt → step_cancelled   → turn_end(reason=interrupted)
边界命中：user_interrupt → turn_interrupted  → turn_end(reason=interrupted)
落空：    user_interrupt →                     turn_end(reason=turn_end)
```

`step_cancelled` 只能是"掐掉了一个 step"的标记，不能兼任"命中"标记——边界命中
没有 step 可取消。`turn_end` 的 payload 也带上 `reason`，UI 不用再靠单独订阅
`step_cancelled` 反推；同一个 `reason` 同时也是这条记录的 `note`（见 `_end_turn`），
回放时按 `note` 过滤就能挑出所有被掐过的 turn。

`reason` 一共三个值：`interrupted`（被掐：掐在飞或命中边界）、`turn end`（正常答完，
信号落空也算它）、`max steps`（转满上限）。有一条不是例外但要说清：redirect 掐在飞
之后 turn **不结束**——它最终记的 reason 是这个 turn 真正结束时的原因（`turn end`
或 `max steps`），纠正消息在同一个 turn 里被重发，中间没有 turn 边界。


## 代码改动
搞清楚如上设计，来看看具体的代码改动。这一章是叠在 stage02 上的增量：
总线（两条方向的 publish / emit）、收件箱、常驻 worker、steering 一行没动，
改动集中在 agent 的 turn 循环与它的入站投递函数，一共三处。

第一处，给每个 session 记录当前在飞的 step。
step 不再直接 await，而是包成一个 task 挂在 `_inflight` 上：

```python
partial: dict[str, Any] = {}
step_task = asyncio.create_task(self._run_step(sid, history, partial))
self._inflight[sid] = step_task
try:
    msg, tool_results = await step_task
except asyncio.CancelledError:
    ...
finally:
    if self._inflight.get(sid) is step_task:
        self._inflight[sid] = None
```

`_run_step` 是一个完整单元：消费流式输出，若模型发起工具调用就连工具一起执行。
工具结果不在 task 里直接写 history，而是返回给 turn 协程统一追加——
这保证 history 里出现的永远是完整的 assistant 消息加紧随的 tool 结果，
被取消的 step 不会留下半截消息。这正是结算一节里最省事的那条政策：
整步全丢；②③④ 的保留形状留给 redirect 一节。

唯一渗透出来的东西是那个 `partial` 字典：task 被取消时局部变量会随 task 一起
消失，而"已经观测到什么"必须由外面收尾的地方看得到，所以把文本碎片、累积中的
tool_calls、"吐过 thinking 吗"都写在调用方持有的 dict 里。stop 不需要它，
redirect 全靠它。

第二处，中断信号的入口。
它不再单独订阅总线上的某个事件，而是和 `user_input` 共用 stage02 那个 inbound
投递入口（`bus.register(agent_id, self.enqueue)` 登记的 `enqueue`），由它按类型分派：

```python
def enqueue(self, event: Event) -> None:
    """总线 inbound 的投递函数（同步）：分派完立刻返回。"""
    if event.type == "user_interrupt":
        self.on_interrupt(event)
        return
    # user_input：投进收件箱，必要时起 worker（stage02 原样，没改）
```

同一个收件地址、同一个入口：user_input 去排队，user_interrupt 不去。
因此 `on_interrupt` 也是**同步**的——inbound 不 await，它自己体内也没有 await，
整段在 asyncio 单线程里是原子的。它做三件事：

```python
def on_interrupt(self, event: Event) -> None:
    intent = str(event.payload.get("intent", "stop"))
    task = self._inflight.get(sid)
    active = bool(self._turn_active.get(sid)) or task is not None
    if active:
        self._pending_interrupt[sid] = {
            "intent": intent, "text": event.payload.get("text"),
        }
        if task is not None and not task.done() and self._phase.get(sid) == "stream":
            task.cancel()
    self.log.append(event, note="interrupt received")
```

- **记下待消费的中断**（intent + 纠正文本），由 step 边界或取消分支各取所需；
- **只掐流式输出那一段**（`_phase == "stream"`）——工具执行那一段（`"tools"`）
  默认不可中断，让它跑完，命中留给 step 边界；
- **空闲时什么都不置**：没有 turn 就不留任何标志，否则会把下一个 turn 掐死。

cancel 作用在具体的 task 对象上，不在飞的 session 查到的是 `None`，信号自然落空——
不存在“信号残留下来杀错下一个 turn”的问题。中断请求本身无论命中与否都进 log
（上面最后一行），它是发生过的事实；是否命中看后面有没有 `step_cancelled`。

有一种边角要说明：信号到达的那一刻，step 恰好刚完成（`task.done()`），
取消落空，掐不到那一步。但这不等于 turn 也放过——看这一步是不是最后一个：
- 它已经是最终回答（没有 tool_calls）：turn 正常收尾，完成赢；
- 它只是工具调用、后面还有一步：turn 还没结束，stop 在边界生效，补一条 assistant
  占位封口（见"各落点"的 ⑦）。
取消请求和完成在竞速，step 层面完成的赢者已定；turn 层面的收尾是另一回事。

第三处，被取消之后 turn 怎么收尾。
`await step_task` 处会抛出 CancelledError，先分清是谁被取消了：

```python
except asyncio.CancelledError:
    if not step_task.cancelled():
        raise  # 是 turn 协程自己被 stop() 取消，继续往外抛
    self._inflight[sid] = None
    pending = self._pending_interrupt.pop(sid, None) or {"intent": "stop", "text": None}
    event = Event("step_cancelled", sid, {"intent": pending["intent"]})
    self.log.append(event, note="interrupt")
    await self.bus.emit(event)
    if await self._close_after_cancel(sid, history, pending, partial):
        continue          # redirect：同一个 turn 里带着纠正重发
    return
```

同一个 Event 对象先进 log 再 `emit` 出去（stage02 起出站事件走 emit，
不再 publish）——log 和总线看到的是同一份数据，ts 也一样。

`_close_after_cancel` 决定退出还是继续：stop 就地封口并 `_end_turn(sid, "interrupted")`
返回 False——本 turn 结束；redirect 把 `partial` 里观测到的碎片按事实补成合法
消息序列、再补一条带 `REDIRECT_NOTE` 标注的纠正 user，返回 True——`continue` 之后
是同一个 turn 的下一次循环，纠正消息在下一个 step 边界前就躺在上下文里等模型读，
没有新 turn，worker 也不会再去收件箱取新消息。

注意取消的粒度是单步，不是执行体：worker 没死，回到收件箱接着取消息；
history 没坏，已完成的步骤全部保留。
`stop()` 是另一回事——进程收尾时连在飞的 step 和 worker 一起取消，两者不要混。

顺带交代一个设计选择。
给中断单独开一条高优先级队列、和收件箱竞速，是一个自然想到的方案，
这个项目的早期实现真这么做过：竞速取两个 asyncio.Queue 时，
输家手里已经取出的消息会静默丢失。
本 stage 的做法绕开了整类问题：中断根本不给队列，它不是消息，
没有入队出队，就没有可丢的东西。

## 跑一下（真实 LLM 实测输出）

四个动作（见 `stage03_interrupt/main.py`）：
1.问保温杯，走完一个完整 turn；
2.紧接着问报销，等 `tool_call_started` 真的到了（step 正在飞）再按停止；
3.问会议室，同样等到工具意图出现后发 redirect 中断（"先别查会议室了，改成查报销规定"）；
4.最后问 VPN，验的是中断之后 agent 还活着、收件箱也没坏。

```text
[ 4.19s] (A) 用户输入：保温杯还有库存吗

[ 7.55s] (A) → 工具调用：search({"query": "保温杯 库存"})
保温杯还有库存，目前剩余 42 件。这款保温杯是 316L 不锈钢内胆，容量为 500ml，杯身颜色为磨砂黑。
[ 8.23s] (A) —— 回答完毕

[ 8.24s] (A) 用户接着问：报销有什么规定

[ 9.00s] (A) 用户按下停止（工具调用正在飞）

[ 9.02s] (A) 已停止：正在飞的那一步被取消，turn 结束

[ 9.02s] (A) 用户再问：顺便说说VPN怎么申请

[11.67s] (A) → 工具调用：search({"query": "报销 规定"}), search({"query": "VPN 申请"})
关于您的两个问题，规定如下：

**1. 报销规定：**
*   **提交时间：** 需在每个月 **25号前** 提交。
*   **发票要求：** 金额超过 **500元** 的报销需要附上 **发票原件**。

**2. VPN 申请流程：**
*   **入口：** 请前往内网 Portal（门户），依次点击 **自助服务** -> **远程接入**。
*   **审批：** 提交后需要等待 **部门经理审批**。
[13.58s] (A) —— 回答完毕
```

9.00s 的停止信号命中，9.02s 那一步作废、turn 收尾，
9.02s 的下一条消息立刻开新 turn——中断到恢复，间隔不到 20ms。

第三轮有一个值得注意的真实行为：模型并发发起了两次检索，一次查 VPN，另一次查的是报销——那个被中断的问题。
原因是中断不作废历史：“报销有什么规定”这条 user 消息在 turn 开始时就写进了 history，
被取消的只是那次的 assistant 回复，provider 对消息序列的要求是完整的 assistant 消息，user 消息悬着完全合法。
模型看到 history 里有一个没回答的问题，自己把它补上了。
如果产品上不想要这个行为（用户按停止就是不想听到报销的事），
中断时把当前 turn 的 user 消息从 history 里撤掉即可，两条路都通，
本 stage 选择保留，因为它让“history 是 log 的投影”这条线更清楚。

session log 里中断的痕迹（节选）：

```json
{"ts": 8.24, "type": "user_input", "session": "A", "payload": {"text": "报销有什么规定"}, "note": "turn start"}
{"ts": 9.0, "type": "user_interrupt", "session": "A", "payload": {"intent": "stop"}, "note": "interrupt received"}
{"ts": 9.01, "type": "step_cancelled", "session": "A", "payload": {"intent": "stop"}, "note": "interrupt"}
{"ts": 9.02, "type": "turn_end", "session": "A", "payload": {"reason": "interrupted"}, "note": "interrupted"}
```

中断请求本身（`user_interrupt`）无论命中与否都进 log——它是发生过的事实；
是否命中由 `step_cancelled` 有没有出现来判断。

## 从“停”到“转向”：本章后半的 redirect

如上就是本章带中断能力的消息在agentloop中插入的机制实现，在agentloop中实现中断信号的关键原则就是保证 assistant 和 tool消息的配对，主动补充打断说明，让llm在读取历史上下文时能够感知到被打断过。

到目前为止，这个agent-loop已经初步支持了消息了steering、followup、interrupt(stop+redirect)
loop 往总线 emit 的东西越来越多，上行的量一上来，总线和 UI 就顶不住了，下一章看看如何在这个部分做优化。


