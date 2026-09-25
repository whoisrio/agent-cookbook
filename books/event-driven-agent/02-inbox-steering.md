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

> 这两种能力在真实运行里长什么样，见本章末尾「跑一下」一节的三段录像
> （`01-idle-turn` / `02-followup` / `03-steering`）。

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

终端实录（`.cast` → gif，同目录有 `.mp4` 和 `index.json`）。三个动作是一条线上的叙事，
所以 case 是**累积**的，名字是 `两位编号-语义名`（编号让文件名字典序 = 演示顺序，
语义名说明跑到哪一步）：

```
stage02-demo 01-idle-turn     # 动作 1
stage02-demo 02-followup      # 动作 1-2
stage02-demo 03-steering      # 动作 1-3（= 全部，默认）
```

插话时机不是 sleep 碰运气，是事件驱动的：agent 在第一个工具调用增量到达时发
`tool_call_started`，demo 等到它才插话——此刻 step 确定在飞，后面还有增量、工具执行、
下一个 step 边界，drain 必然有机会捞到。但"发得早"不保证"被 steering 消化"：若插话落在
最后一个 drain 点之后（临界降级），worker 会在 turn 结束后把它当 followup 取走。两种结局
demo 都会在屏幕上如实打出来（★ steering 生效 / 降级行），肉眼可辨。

### 01-idle-turn：空闲时投递，走完完整一轮

![01-idle-turn：完整一轮](../../src/baby_event_driven_agent/rec/stage02/docs/01-idle-turn.gif)

worker 空闲 → 投递即开新 turn：`query_inventory` + 流式回答，一轮有头有尾。

### 02-followup：紧接着再问，排队成 followup

![02-followup：排到当前 turn 之后](../../src/baby_event_driven_agent/rec/stage02/docs/02-followup.gif)

第二问（报销）紧接着第一问发出。它的答案不在第一轮检索结果里，模型必然发起
`search_rules`——这一条是给动作 3 铺的"工具一定会在飞"。

### 03-steering：工具在飞时插话

![03-steering：插话被 drain 进当前 turn](../../src/baby_event_driven_agent/rec/stage02/docs/03-steering.gif)

屏幕上的 ★ 就是 steering 的可视化瞬间：

```text
[ 9.10s] 用户 │ 用户插话（此刻工具调用正在飞）：顺便说说VPN怎么申请
[ 9.15s] LLM(要求执行工具) │ → search_rules({"query":"报销"})
[ 9.15s] 系统 │ ★ steering 生效：「顺便说说VPN怎么申请」拼进当前 turn 的上下文，不开新 turn
[11.32s] LLM(要求执行工具) │ → search_rules({"query":"VPN 申请"})
```

插话在 step 边界被 drain 进当前上下文，模型随后**自己发起了一次
`search_rules({"query":"VPN 申请"})`**，最后在同一轮里把两个问题一起答了。分类、排队、
拼上下文，全程没有一行代码写死"这是插话"。

session log 把这件事记得更清楚（这一轮的原文，节选）：

```json
{"ts": 6.88, "type": "user_input", "session": "A", "payload": {"text": "报销有什么规定"}, "note": "turn start"}
{"ts": 9.15, "type": "agent_reply", "session": "A", "payload": {"message": {"role": "assistant", "content": null, "tool_calls": [{"id": "call_qcgztvh1", "type": "function", "function": {"name": "search_rules", "arguments": "{\"query\":\"报销\"}"}}]}}, "note": "tool_call"}
{"ts": 9.15, "type": "tool_result", "session": "A", "payload": {"tool_call_id": "call_qcgztvh1", "name": "search_rules", "result": "报销：月底 25 号前提交，超过 500 元要附发票原件。"}, "note": "tool result"}
{"ts": 9.1, "type": "user_input", "session": "A", "payload": {"text": "顺便说说VPN怎么申请"}, "note": "steering"}
{"ts": 11.32, "type": "agent_reply", "session": "A", "payload": {"message": {"role": "assistant", "content": null, "tool_calls": [{"id": "call_vwl61ffb", "type": "function", "function": {"name": "search_rules", "arguments": "{\"query\":\"VPN 申请\"}"}}]}}, "note": "tool_call"}
{"ts": 18.08, "type": "agent_reply", "session": "A", "payload": {"message": {"role": "assistant", "content": "目前报销和VPN申请的规定如下：……"}}, "note": "final"}
```

倒数第三行值得盯 10 秒：它的 `ts` 是 9.1——消息**到达**的时刻——却排在 9.15 的记录后面，
因为 log 按消费顺序追加，它是在 step 边界被 drain 的那一刻写进去的。到达时间和消费时间是
两个时刻，这一行就是"分类权在消费端"的字面证据。

真实运行里两种结局都出现过。另一次实测中，同样的插话晚了约 10 毫秒——worker 在 tool_call
回复后原子地跑完了"执行工具 + step 边界 drain"，插话落在了最后一个 drain 点之后：屏幕上
没有 ★，turn 收尾也没带上它，它作为 followup 开了新 turn。这正是插话语义的边界：
**steering 的意义只存在于"当前 turn 还活着且尚未越过最后一个 drain 点"的时候**，错过窗口
就自然降级成普通用户消息——没有任何特殊代码处理"降级"，worker 的下一次 `inbox.get()`
天然接住。

这轮的完整序列也值得看一眼——assistant → tool_result →（steering 拼进来）→ assistant →
tool_result → assistant，四步一个 turn：报销的检索结果刚回来，插话已经在上下文里，模型决定
再检索一次 VPN，最后一条 assistant 把两件事一起答完。

## 设计边界，以及下一章的需求

目前实现的 steering 还有一层天然等待：它只在 step 边界生效，loop正在飞的时候，插入消息也得老实等着，有可能要等上十多秒。如果用户连这个等待时间也不想有呢 ? 

所以下一个需求就来了：打断正在飞的agent loop。
而且打断有两种意图：只是想停，让 turn 结束；或者"我改主意了，接着干"，turn 不结束，原地转向。这就需要一条和收件箱完全不同的车道：控制信号。Stage 3，打断与转向，咱们下次再讨论。

