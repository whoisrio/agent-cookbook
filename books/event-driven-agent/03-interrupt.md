# Stage 3：打断与转向（interrupt + redirect）

> 配套代码：`src/baby_event_driven_agent/stages/stage03_interrupt/`，
> `stage03-demo` 跑演示（真模型），`stage03-test` 跑测试（同样打真模型，
> 不设替身——本章要验的就是真 provider 收不收这套消息形状）。
> 本 stage 保留 stage02 的全部能力（收件箱、steering、followup），在此之上加一种新东西：
> 打断在跑的一步，并决定掐完之后是结束（interrupt）还是原地转向（redirect）。

## 需求

Agent仍在执行中，但用户改变了想法，希望Agent立刻停止当前的任务，转而执行新的任务，咱们前两章设计的baby agent还不能够支持；
stage02 里，用户消息进收件箱，排队，在 step 边界被消费，语义由消费时机决定；
而用户改变想法，则是要打断当前任务，打断是控制信号，不能排队，不能进入收件箱，必须立刻处理。


## 打断发生时agentloop的状态，影响如何处理打断
打断发送的时机，可能有如下几种，
- 发送消息给LLM，LLM还未返回就被打断；
- 发送消息给LLM，LLM输出thinking token时被打断；
- 发送消息给LLM，LLM要求使用tools，tools信息输出中，相当于LLM正在输出时被打断；
- tools执行中被打断，由于模型一次可能调用多个tool，那么可能有部分tool完成了执行，部分tool执行到一半，部分tool扔在等待被调度；
- tools全都完成执行时被打断
- LLM输出最终结果时被打断

无论agent在哪种执行状态下被打断，一个必须遵从的原则，就是与LLM交互的messages，assistant的消息和tool的消息必须成对的，
agent执行时被打断，意味着没法完整的收到LLM的回复或者工具执行的结果，就需要agent在处理打断信号时补齐应该有的消息，以保持messages的配对格式，因此，整体策略是，
- 只要进入了agent的循环，没有收到LLM的完整消息前，就补充上assistant消息，标识该轮对话已经被打断；
- LLM处于工具调用执行过程中，没有完成的工具调用，主动补齐 tool_result ，补充的内容则是标识该工具被打断；

咱们将如上场景分类看一下，

#### LLM 输出没走完：整步丢 + assistant 打断占位
一，LLM 输出没走完，
"用户消息已发，LLM未回 / LLM只在吐 thinking / LLM要调工具参数还未完成输出 / LLM最终回答只说了一半"四种时机，都属于这种情况，
这几种情况，我们选择直接丢掉LLM的不完整的输出内容，
>一些agent会记录不完整的thinking token，咱们选择忽略，不完整的就不记录

那么messages里就是如下，
```json
[
  {"role": "user", "content": "报销有什么规定"},
  {"role": "assistant", "content": "[response interrupted]"}
]
```

如果用户有新增的消息，那么就在中断消息后输入，
```json
[
  {"role": "user", "content": "报销有什么规定"},
  {"role": "assistant", "content": "[response interrupted]"},
  {"role": "user", "content": "先别查了，改成订会议室"}
]
```

#### tool 执行中（边界命中）：在跑的跑完，没开始的补占位
二，tool正在执行中，
对于已经在执行的tool，目前咱们的设计规则是让已经在执行的tool执行完，没有执行的，就不再执行。
>当然，生产场景，有一些会改动状态的工具，实际上是应该支持被打断的，这个复杂的场景，作为后续优化的一个关键需求点先记录下来。

假设模型一次发起三个工具调用：`call_1` 已返回，`call_2` 正在跑（等它跑完），`call_3`还没开始。
那么针对 call_1 的 tool_result就是完整的，
继续等待call_2跑完，call_2的tool_result也是完整的，
那么call_3不再执行，call_3的tool_result则是主动补充的消息，记录的信息是被中断，
在这次工具调用后，再补充上 被中断的 assistant消息；

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
  {"role": "tool", "tool_call_id": "call_3","content": "[被用户打断，未执行]"},
  {"role": "assistant", "content": "[本轮已被用户打断，不再基于上面的工具结果作答]"}
]
```
如果此时inbox队列中有新增的用户消息，那么就不添加assistant占位执行，直接发送新的用户消息；

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
   "content": "[被用户打断，未执行]"},
  {"role": "user", "content": "先别查了，改成订会议室"}
]
```

#### tool 刚好跑完（边界命中）：收口 ≠ 残缺
三，如果用户打断的那一刻，所有工具都已经完成执行并全部拿到真实结果，而LLM还没有给出下一轮的输出。
对于中断信号，就直接补上assistant占位消息即可。

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
  {"role": "assistant", "content": "[本轮已被用户打断，不再基于上面的工具结果作答]"}
]
```
如果此时inbox中有待处理的用户消息，则什么都不需要补充，这时就是一个标准的消息steering，
```json
{"role": "user", "content": "先别查了，改成订会议室"}
```

#### 6. 模型在回答，只说了一半（流里只有 `text_delta`）

工具跑完、模型开始写**最终回答**（没有工具的 turn 就是直接作答），此时的选择依然是直接丢掉LLM的不完整的输出内容，
补充上占位符即可。


### 工具接口：把"能不能打断"预留出来
如上场景中，对于收到打断信号时正在执行的tools，我们目前的选择是让tools执行完，
为了后续让工具也支持打断，我们先给工具预留一个能否处理打断的能力描述方法，
通过on_interrupt来描述工具收到打断信号的行为，
-cancel,可以打断，工具自行处理回滚
-yield，不打断，可以转后台执行，执行结果可忽略
-block，不能打断，必须等待工具执行完成，目前全都按照这个方式来处理。


```python
from typing import Literal, Protocol

class Tool(Protocol):
    name: str
    def execute(self, args: dict) -> str: ...

    def on_interrupt(self) -> Literal["cancel", "yield", "block"]:
        """打断到达时工具自报家门；默认 block：不可打断，跑完为止。"""
        return "block"
```

## 代码改动
搞清楚如上设计，来看看具体的代码改动。这一章是叠在 stage02 上的增量修改：

第一，打断信号的入口。
agent消费总线事件时，针对`user_interrupt`直接同步处理，

```python
def enqueue(self, event: Event) -> None:
    """总线 inbound 的投递函数（同步）：分派完立刻返回。"""
    if event.type == "user_interrupt":
        self.on_interrupt(event)
        return
    # user_input：投进收件箱，必要时起 worker（stage02 原样，没改）
```
在on_interrupt中，把打断记进 `_pending_interrupt`,目前针对tool执行阶段，正在执行的tools，我们允许他执行完，因此需要tools执行完成时要从_pending_interrupt里检查是否呀中断；
如果处于LLM流式输出段（`_phase == "stream"`），那么就直接中断当前task。

```python
def on_interrupt(self, event: Event) -> None:
    task = self._inflight.get(sid)
    active = bool(self._turn_active.get(sid)) or task is not None
    if active:
        self._pending_interrupt[sid] = {
            "text": event.payload.get("text"),
        }
        if task is not None and not task.done() and self._phase.get(sid) == "stream":
            task.cancel()
```

第二，把与的一次完整的LLM交互从Stage02的worker(coro)中独立成一个task对象，
worker(coro)负责消息消费，不cancel；
_flight中的task(coro)作为与单独与LLM交互的协程task，单独做cancel；

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


## 跑一下（真实 LLM 实测输出）

我们通过如下case实际演示一下，在不同的阶段收到中断信号时的实际效果，
01 演示还没有收到LLM完整回复，直接中断；
02 在01的基础上演示有追加的用户消息的情况；
03 演示LLM输出到一半被中断的情况；
04 则是演示工具执行中被中断的情况；
05 是在04的基础上追加新的用户消息处理；
06 则是LLM完成工具调用，在输出最终答复时被中断的演示；

### 01 已发 LLM、未回复
如下case中，用户刚要求agent介绍自己，agent就收到中断信号，因此，此时agent没有任何输出
![01-sent-stop](../../src/baby_event_driven_agent/rec/stage03/docs/01-sent-stop.gif)

对应的session log如下，
```json
{"ts": 0.23, "type": "user_input", "payload": {"text": "你好，用一句话介绍你自己"}, "note": "turn start"}
{"ts": 0.39, "type": "agent_reply", "payload": {"message": {"role": "assistant", "content": "[response interrupted]"}, "synthetic": true}, "note": "marker"}
```

### 02 打断时附了一条新消息
第二个case，则是在第一个case的基础上，补充了一条用户新增的消息，可以看到LLM按照用户最新的消息处理用户的任务
![02-sent-redirect](../../src/baby_event_driven_agent/rec/stage03/docs/02-sent-redirect.gif)

对应的session log如下，
```json
{"ts": 0.23, "type": "user_input", "payload": {"text": "你好，用一句话介绍你自己"}, "note": "turn start"}
{"ts": 0.39, "type": "agent_reply", "payload": {"message": {"role": "assistant", "content": "[response interrupted]"}, "synthetic": true}, "note": "interrupted"}
{"ts": 0.39, "type": "user_input", "payload": {"text": "别自我介绍了，改成说说报销规定", "synthetic": true}, "note": "redirect"}
{"ts": 4.81, "type": "agent_reply", "payload": {"message": {"role": "assistant", …, "tool_calls": [search_rules…]}}, "note": "tool_call"}
{"ts": 4.81, "type": "tool_result", "payload": {"…", "result": "报销：报销流程：发起申请 -> 财务审核 -> 打款。详见财务制度。", "skipped": false}, "note": "tool result"}
{"ts": 7.43, "type": "agent_reply", "payload": {"message": {"role": "assistant", "content": "报销流程是：发起申请 -> 财务审核 -> 打款，详细规定请查看财务制度。"}}, "note": "final"}
```

### 03 回答只说了一半
第三个case，模型正在输出最终回答，只说了一半就收到中断信号，半句话整步丢弃，账里补上打断占位
![03-half-answer-stop](../../src/baby_event_driven_agent/rec/stage03/docs/03-half-answer-stop.gif)

对应的session log如下，
```json
{"ts": 0.33, "type": "user_input", "payload": {"text": "用三句话说说，事件驱动架构相比轮询好在哪儿"}, "note": "turn start"}
{"ts": 6.33, "type": "agent_reply", "payload": {"message": {"role": "assistant", "content": "[response interrupted]"}, "synthetic": true}, "note": "marker"}
```

### 04 工具执行中被中断
打断信号落在工具执行阶段：模型发起的两个检索，第一个跑完、结果照进账，第二个补"未执行"
占位；尾部停在 tool 上，补 assistant 封口
![04-tool-running-stop](../../src/baby_event_driven_agent/rec/stage03/docs/04-tool-running-stop.gif)

对应的session log如下，
```json
{"ts": 0.33, "type": "user_input", "payload": {"text": "报销和 VPN 分别有什么规定，都要查"}, "note": "turn start"}
{"ts": 5.11, "type": "agent_reply", "payload": {"message": {"role": "assistant", …, "tool_calls": [search_rules × 2…]}}, "note": "tool_call"}
{"ts": 5.11, "type": "tool_result", "payload": {"…", "result": "报销：报销流程：发起申请 -> 财务审核 -> 打款。详见财务制度。", "skipped": false}, "note": "tool result"}
{"ts": 5.11, "type": "tool_result", "payload": {"…", "result": "[被用户中断，未执行]", "skipped": true}, "note": "tool skipped"}
{"ts": 5.11, "type": "agent_reply", "payload": {"message": {"role": "assistant", "content": "[本轮已被用户中断，不再基于上面的工具结果作答]"}, "synthetic": true}, "note": "marker"}
```

### 05 工具执行中被中断，并附了一条新消息
在04的基础上追加了一条新的用户消息：第一个检索的结果照进账，第二个补"未执行"占位，
新消息在 step 边界被处理，agent 接着检索订会议室并在同一个 turn 里回答
![05-tool-running-redirect](../../src/baby_event_driven_agent/rec/stage03/docs/05-tool-running-redirect.gif)

对应的session log如下，
```json
{"ts": 0.22, "type": "user_input", "payload": {"text": "报销和 VPN 分别有什么规定，都要查"}, "note": "turn start"}
{"ts": 4.36, "type": "agent_reply", "payload": {"message": {"role": "assistant", …, "tool_calls": [search_rules × 2…]}}, "note": "tool_call"}
{"ts": 4.36, "type": "tool_result", "payload": {"…", "result": "报销：报销流程：发起申请 -> 财务审核 -> 打款。详见财务制度。", "skipped": false}, "note": "tool result"}
{"ts": 4.36, "type": "tool_result", "payload": {"…", "result": "[被用户中断，未执行]", "skipped": true}, "note": "tool skipped"}
{"ts": 4.36, "type": "user_input", "payload": {"text": "先别查了，改成订会议室", "synthetic": true}, "note": "redirect"}
{"ts": 5.79, "type": "agent_reply", "payload": {"message": {"role": "assistant", …, "tool_calls": [search_rules("会议室")…]}}, "note": "tool_call"}
{"ts": 5.79, "type": "tool_result", "payload": {"…", "result": "会议室预订：需提前一天通过OA系统申请，填写事由和时间，经直属上级审批通过后即可生效。…", "skipped": false}, "note": "tool result"}
{"ts": 12.81, "type": "agent_reply", "payload": {"message": {"role": "assistant", "content": "根据查询结果：…"}}, "note": "final"}
```

### 06 工具刚好跑完时被中断
打断到达时工具已经全部跑完：结果照进账，尾部停在 tool 上，封口照样补——封口针对的是
"turn 静止后结果没人接"，与工具有没有残缺无关
![06-tools-done-stop](../../src/baby_event_driven_agent/rec/stage03/docs/06-tools-done-stop.gif)

对应的session log如下，
```json
{"ts": 0.35, "type": "user_input", "payload": {"text": "报销有什么规定"}, "note": "turn start"}
{"ts": 3.67, "type": "agent_reply", "payload": {"message": {"role": "assistant", …, "tool_calls": [search_rules…]}}, "note": "tool_call"}
{"ts": 3.67, "type": "tool_result", "payload": {"…", "result": "报销：报销流程：发起申请 -> 财务审核 -> 打款。详见财务制度。", "skipped": false}, "note": "tool result"}
{"ts": 3.67, "type": "agent_reply", "payload": {"message": {"role": "assistant", "content": "[本轮已被用户打断，不再基于上面的工具结果作答]"}, "synthetic": true}, "note": "marker"}
```



## 小结
总结一下，对于agent loop运行时收到中断信号，咱们的处理目标就是让messages保持完整，
1. 只要进入了agent的循环，没有收到LLM的完整消息前，就补充上assistant消息，标识该轮对话已经被打断；
2. LLM处于工具调用执行过程中，没有完成的工具调用，主动补齐 tool_result ，补充的内容则是标识该工具被打断；
3. 对于刚好完成了所有tool_result处理，且用户有新增的追加消息时，就当做steering处理，此时没有任何中断信号；

到目前为止，这个agent-loop已经初步支持了消息了steering、followup、interrupt等机制，
agent在处理任务时，新增消息的处理方式各有不同，你用过哪些agent，他们的默认处理方式是什么样的呢？


