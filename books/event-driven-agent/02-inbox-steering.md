# Stage 2：收件箱 + 总线分方向

> 配套代码：`src/baby_event_driven_agent/stages/stage02_inbox_steering/`，
> 可跑（`stage02-demo` / `stage02-test`）、带 tests。
> 保留 stage01 的全部能力，本章改两处：**总线分方向**（inbound 同步入队 /
> outbound 异步扇出）和**收件箱 + 常驻 worker**。

## 需求来了

Stage 1 有个预设的边界：一次处理一个请求。真实用户不会遵守这个预设，回答还在跑，他就想把下一句发出去。
可能是插话（把当前的输入，直接插入到当前的对话轮次中），即 steering；
也可能是追问（一轮交互完成了，接着处理），即 followup。

市面上绝大多数 agent 都支持这两种能力，比如 workbuddy、claude code。

>差几张followup和steering的截图

所以 Stage 2，咱们就给这个事件驱动的 agent，添加上 followup 和 steering 的能力。

## 先看 Stage 1 卡在哪

Stage 1 在它自己的能力范围内是好用的——单用户、一问一答。但它的结构里埋着三处上限，需求一升级就撞上。

**一是投递与执行绑死。** `publish` 一路 `await` 到 handler 跑完整个 turn：

```python
async def publish(self, event):
    for handler in self._subs.get(event.type, []):
        await handler(event)          # handler 就地跑完整个 turn 才返回
```

于是"回答还在跑"的时候，第二条消息没有安身之处——调用方卡在 `await` 里，消息无处排队。

**二是 publish 语义混杂。** 同一个方法，一会儿当"投递命令"（`user_input`），一会儿当"扇出事件"（`agent_delta`）。一条通道背两种语义，量小的时候无害，一旦要排队、要分优先级，就必须拆开。

**三是单 task 串行，没有收件箱、也没有控制通道。** 

要支持用户输入"打断"和"排队"这两个能力，就得把前两处一起拆掉：**总线分方向**，再加一个**收件箱**。

## 设计一：总线分方向

总线从这一章起分成两条路，各走各的：
- **inbound（命令）**：`publish(event, to)` —— **同步**，把命令交给目标 agent 登记的投递函数就返回。不 await、不扇出。
- **outbound（事件）**：`emit(event)` —— **异步**，把 agent 发出的事件扇出给订阅者。

```python
Handler = Callable[[Event], Awaitable[None]]   # outbound 订阅者：异步
Sink = Callable[[Event], None]                 # inbound 投递函数：同步

class EventBus:
    def __init__(self):
        self._sinks: dict[str, Sink] = {}
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    def register(self, agent_id, sink):        # agent 登记入站投递函数
        self._sinks[agent_id] = sink
    def subscribe(self, type, handler):        # 订阅 outbound 事件
        self._subs[type].append(handler)

    def publish(self, event, to):              # inbound：同步投递，立即返回
        sink = self._sinks.get(to)
        if sink is None:
            raise KeyError(f"没有这个 agent：{to!r}")
        sink(event)

    async def emit(self, event):               # outbound：异步扇出
        for handler in self._subs.get(event.type, []):
            await handler(event)
```

> 本章的 `emit` 还是 `await handler` 的**简单扇出**。异步分发、QoS、背压、拦截治理，留到 Stage 4 讲。

## 设计二：收件箱 + 常驻 worker

handler 的位置，agent 现在登记的是**同步的投递函数** `enqueue`：只往收件箱塞，立刻返回。

```python
class Agent:
    def __init__(self, bus, log, llm, agent_id="agent"):
        ...
        self.inboxes: dict[str, asyncio.Queue] = {}   # session_id -> 收件箱
        self._workers: dict[str, asyncio.Task] = {}
        bus.register(agent_id, self.enqueue)          # inbound 命令路由到这里

    def enqueue(self, event):                         # 同步：只投递
        sid = event.session_id
        self.inboxes.setdefault(sid, asyncio.Queue()).put_nowait(event)
        if sid not in self._workers or self._workers[sid].done():
            self._workers[sid] = asyncio.create_task(self._worker(sid))
```

消费侧是一个常驻 worker：取一条消息跑一个 turn，跑完接着取。

```python
    async def _worker(self, sid):
        inbox = self.inboxes[sid]
        while True:
            event = await inbox.get()
            await self._run_turn(event)
```

两个设计决定值得停下来看：

**worker 永不自行退出。** 它只响应显式的 `stop()`（cancel 所有 worker 并等待）。
这样设计，是需要worker能够及时响应消息。
```python
   while True:
       event = await inbox.get()
```
退出清理是 `stop()` 的显式职责，不是每个 turn 的尾部负担。

消息投递采用 put_nowait()投递，函数是同步的，`put_nowait` 和 `done()` 检查之间**没有任何 await 点**。

## 关键论点：分类时机在消费端
同一条用户消息，可能是 followup，也可能是 steering，取决于它**被消费的
那一刻** worker 在干什么：

```python
    async def _worker(self, sid):
        inbox = self.inboxes[sid]
        while True:
            event = await inbox.get()      # 空闲时取到 → 新 turn 的输入（followup）
            await self._run_turn(event)

    async def _drain_steering(self, sid, history):
        """step 边界 drain：此刻 inbox 里的消息全部当 steering，拼进当前上下文。

        消费发生时发 steering_consumed 事件——UI 靠它看见
        "插话在这一刻生效了"。
        """
        inbox = self.inboxes[sid]
        texts = []
        while not inbox.empty():
            ev = inbox.get_nowait()
            texts.append(ev.payload["text"])
            history.append({"role": "user", "content": ev.payload["text"]})
            self.log.append(ev, note="steering")
        if texts:
            await self.bus.emit(Event("steering_consumed", sid, {"texts": texts}))
```

`_run_turn` 的每个 step 开始前 drain 一次，drain 到的消息直接拼进当前
上下文，模型在下一步就看得见它；turn 结束后 worker 回到 `inbox.get()`，
排着的消息自然成为下一个 turn 的输入。消息自己不背语义，分类权在消费
那一刻——这比"提交时打标"干净得多：UI 只需要往一个口子里投消息，
不需要替 agent 预判时间窗口。

turn_end 在这一章也升级成了真事件：stage 1时，用户消息的事件处理是在事件发出后直接await完成的，stage 2 用户输入的事件是投递到agent的收件箱，所以发出turn_end事件，让外部能感知到turn的结束。

## 跑一下（真实 LLM 实测输出）

三个动作：问保温杯（完整一个 turn）；紧接着问报销（worker 已空闲 →
followup，立刻开新 turn，且它的答案不在第一轮检索结果里，模型必然发起
search）；趁报销这轮**工具调用正在飞**时插话 VPN。

插话时机不是 sleep 碰运气，是事件驱动的：agent 在第一个工具调用增量到达时
发 `tool_call_started` 事件，demo 等到它才插话——此刻 step 确定在飞，
后面还有增量、工具执行、下一个 step 边界，drain 必然有机会捞到。
但"发得早"不保证"被 steering 消化"：若插话落在最后一个 drain 点之后
（临界降级），worker 会在 turn 结束后把它当 followup 取走。两种结局
demo 都会在屏幕上如实打出来（★ steering 生效 / 降级行），肉眼可辨。

本次实测（`OPENAI_MODEL=qwen3.7-flash stage02-demo`）：

> 记录待更新：下面这段输出录于工具集还是 read/write/search 的早期版本（所以看到
> `search`），当前代码暴露的是 `query_inventory` / `search_rules` 等。事件序列与
> steering 行为不受影响，但工具名对不上——待用当前代码复跑后替换。

```text
[ 0.32s] (A) 用户输入：保温杯还有库存吗

[ 5.29s] (A) → 工具调用：search({"query": "保温杯 库存"})
是的，保温杯还有库存。目前库存有 42 件。
[ 8.00s] (A) —— 回答完毕

[ 8.00s] (A) 用户接着问：报销有什么规定

[10.68s] (A) 用户插话（此刻工具调用正在飞）：顺便说说VPN怎么申请

[10.92s] (A) → 工具调用：search({"query": "报销 规定"})

[10.92s] (A) ★ steering 生效：「顺便说说VPN怎么申请」拼进当前 turn 的上下文，不开新 turn

[18.38s] (A) → 工具调用：search({"query": "VPN 申请"})
关于报销和 VPN 申请的规定如下：……
[20.60s] (A) —— 回答完毕
```

★ 那一行就是 steering 的可视化瞬间：10.68s 发出的插话，在 10.92s 的
step 边界被 drain 进当前上下文——模型随后**自己发起了一次
`search("VPN 申请")`**，最后在同一轮里把两个问题一起答了。
分类、排队、拼上下文，全程没有一行代码写死"这是插话"。

真实运行里两种结局都出现过。另一次实测中，同样的插话晚了约 10 毫秒——
worker 在 tool_call 回复后原子地跑完了"执行工具 + step 边界 drain"，
插话落在了最后一个 drain 点之后：屏幕上没有 ★，turn 收尾也没带上它，
它作为 followup 开了新 turn。这正是插话语义的边界：**steering 的意义
只存在于"当前 turn 还活着且尚未越过最后一个 drain 点"的时候**，
错过窗口就自然降级成普通用户消息——没有任何特殊代码处理"降级"，
worker 的下一次 `inbox.get()` 天然接住。

session log 把这个故事记得更清楚（节选，本次 ★ 命中的那轮）：

```json
{"ts": 8.0, "type": "user_input", "session": "A", "payload": {"text": "报销有什么规定"}, "note": "turn start"}
{"ts": 10.92, "type": "agent_reply", "session": "A", "payload": {"message": {"role": "assistant", "content": null, "tool_calls": [{"function": {"name": "search", "arguments": "{\"query\": \"报销 规定\"}"}}]}}, "note": "tool_call"}
{"ts": 10.68, "type": "user_input", "session": "A", "payload": {"text": "顺便说说VPN怎么申请"}, "note": "steering"}
{"ts": 18.38, "type": "agent_reply", "session": "A", "payload": {"message": {"role": "assistant", "content": null, "tool_calls": [{"function": {"name": "search", "arguments": "{\"query\": \"VPN 申请\"}"}}]}}, "note": "tool_call"}
```

第三行值得盯 10 秒：它的 `ts` 是 10.68——消息**到达**的时刻——却排在
10.92 的记录后面，因为 log 按消费顺序追加，它是在 step 边界被 drain 的
那一刻写进去的。到达时间和消费时间是两个时刻，这一行就是"分类权在
消费端"的字面证据。

这轮的完整序列也值得看一眼——assistant → tool_result → （steering
拼进来）→ assistant → tool_result → assistant，四步一个 turn：
报销的检索结果刚回来，插话已经在上下文里，模型决定再检索一次 VPN，
最后一条 assistant 把两件事一起答完。

## 设计边界，以及下一章的需求

demo 的兜底逻辑盖住了一个本章很隐蔽的 bug, `stop()` 会丢消息，而且丢得
悄无声息。worker 被 cancel 的那一刻，两种东西一起没了：

- 还排在收件箱里、没被取走的：消费者没了，它们再也不会被处理。它们连
  session log 里都没痕迹——log 只在消费时追加，只有 UI 还记得自己发过。
- 已经取出来、`_run_turn` 还没跑完的那条：出队即离开收件箱，掐掉之后不会
  回到队里。log 里留下一条 `turn start`，`turn end` 永远不来——用户以为
  在跑，其实什么都没了。

两种都没有任何提示：调用方既收不到 `turn_end`，也不知道还剩几条没消化。

目前实现的 steering 还有一层天然等待：它只在 step 边界生效，**正在飞的
那一步等不了**。实测插话 10.68s 发出、10.92s 被消化，只等了 0.24 秒——
运气好，正好赶上边界。但那不是设计保证的：如果当时在飞的是一次 8 秒的
模型请求，用户就干等 8 秒，当前设计里没有任何东西能把它掐掉。

而用户不等的时候会做什么？按停止。所以下一个需求：**打断正在飞的那一步**
——而且打断有两种意图：只是想停（turn 结束），或者"我改主意了，接着干"
（turn 不结束，原地转向）。这就需要一条和收件箱完全不同的车道：控制信号。
Stage 3（打断与转向）见。
