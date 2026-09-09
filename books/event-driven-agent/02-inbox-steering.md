# Stage 2：收件箱——followup 和 steering

> 配套代码：`src/baby_event_driven_agent/stages/stage02_inbox_steering/`，
> 可跑（`stage02-demo` / `stage02-test`）、带 tests。
> 模型和工具与 Stage 1 完全相同，这章只动一个地方：消息进来之后怎么排队。

## 需求来了

Stage 1 的 v0.1 有个说好的边界：一次处理一个请求。publish 原地 await handler，
整个 turn 占着总线回调。真实用户不管这些——回答还在跑，他就想把下一句发出去。
可能是追问（答完了接着处理），可能是插话（正在回答的东西里顺便带上它）。

所以 Stage 2 的需求是两条：

- 消息要**先有地方排队**，发布的人不被 turn 拖住。
- 排队的消息要能**插进正在跑的回答**（steering），或者**排在回答之后**
  （followup）。

## 设计：收件箱 + 常驻 worker

改动只发生在 agent 的入口。总线 handler 不再跑 turn，只做一件事——
投进收件箱，立刻返回：

```python
class Agent:
    def __init__(self, bus, log, llm):
        self.bus, self.log, self.llm = bus, log, llm
        self.history = {}                                   # session_id -> messages
        self.inboxes: dict[str, asyncio.Queue] = {}         # session_id -> 收件箱
        self._workers: dict[str, asyncio.Task] = {}

    async def on_user_input(self, event):
        """总线的 user_input handler：投进收件箱立刻返回。"""
        sid = event.session_id
        self.inboxes.setdefault(sid, asyncio.Queue()).put_nowait(event)
        if sid not in self._workers or self._workers[sid].done():
            self._workers[sid] = asyncio.create_task(self._worker(sid))
```

消费侧是一个常驻 worker：取一条消息跑一个 turn，跑完接着取。

```python
    async def _worker(self, sid):
        """常驻消费循环：取一条消息跑一个 turn，跑完接着取。"""
        inbox = self.inboxes[sid]
        while True:
            event = await inbox.get()
            await self._run_turn(event)
```

两个设计决定值得停下来看：

**worker 永不自行退出。** 它只响应显式的 `stop()`（cancel 所有 worker 并等待）。
反过来设计——每个 turn 起一个 task、跑完自杀——就要面对一个尴尬的竞态：
turn 的最终检查已经过了，task 正在收尾，消息此刻进来，task 死了，
消息永远没人取。让 worker 不退出，这个竞态就根本不存在；
退出清理是 `stop()` 的显式职责，不是每个 turn 的尾部负担。

**spawn 检查为什么是原子的？** `put_nowait` 和 `done()` 检查之间没有 await，
asyncio 单线程事件循环里这段代码不会被打断。这是 asyncio 的一个基本事实：
原子性不靠锁，靠"中间没有让出控制权的点"。

## 关键论点：分类时机在消费端

同一条用户消息，可能是 followup，也可能是 steering——取决于它**被消费的
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
        "插话在这一刻生效了"，而不是靠翻 session log。
        """
        inbox = self.inboxes[sid]
        texts = []
        while not inbox.empty():
            ev = inbox.get_nowait()
            texts.append(ev.payload["text"])
            history.append({"role": "user", "content": ev.payload["text"]})
            self.log.append(ev, note="steering")
        if texts:
            await self.bus.publish(Event("steering_consumed", sid, {"texts": texts}))
```

`_run_turn` 的每个 step 开始前 drain 一次，drain 到的消息直接拼进当前
上下文，模型在下一步就看得见它；turn 结束后 worker 回到 `inbox.get()`，
排着的消息自然成为下一个 turn 的输入。消息自己不背语义，分类权在消费
那一刻——这比"提交时打标"干净得多：UI 只需要往一个口子里投消息，
不需要替 agent 预判时间窗口。

turn_end 在这一章也升级成了真事件：Stage 1 它只写 log（没人需要等它），
现在 UI 要等一轮结束再发下一条，所以 `_run_turn` 收尾时把它 publish 上总线。

## 跑一下（真实 LLM 实测输出）

三个动作按真实时间顺序发生：问保温杯（完整一个 turn）；紧接着问玻璃杯
（worker 已空闲 → followup，立刻开新 turn）；1 秒后趁玻璃杯的 turn
还在跑插话（→ 下一个 step 边界被 drain 进当前 turn）。

```text
[ 3.00s] (A) 用户输入：保温杯还有库存吗

[ 8.01s] (A) → 工具调用：search({"query": "保温杯 库存"})
保温杯还有库存，现有 42 件。具体规格为：316L 不锈钢内胆，500ml，杯身磨砂黑。
[ 8.95s] (A) —— 回答完毕

[ 8.97s] (A) 用户接着问：帮我查一下玻璃杯的库存

[ 9.97s] (A) 用户插话（此刻上一条还在跑）：顺便说说会议室怎么订

[18.50s] (A) → 工具调用：search({"query": "玻璃杯 库存"})

[19.85s] (A) → 工具调用：search({"query": "会议室 预订"})
玻璃杯的库存有 17 件，规格是高硼硅玻璃，400ml，且可以进微波炉。

关于会议室预订，您可以联系行政小王进行安排。注意在预订前，请先查看日历确认
会议室没有被锁定。
[21.17s] (A) —— 回答完毕
```

注意 9.97s 那句插话去哪了：它没有开新 turn，而是等玻璃杯那步跑完，
在 step 边界被 drain 进当前上下文——模型随后**自己发起了一次
`search("会议室 预订")`**，最后在同一轮里把两个问题一起答了。
分类、排队、拼上下文，全程没有一行代码写死"这是插话"。

session log 把这个故事记得更清楚（节选）：

```json
{"ts": 8.97, "type": "user_input", "session": "A", "payload": {"text": "帮我查一下玻璃杯的库存"}, "note": "turn start"}
{"ts": 18.5, "type": "agent_reply", "session": "A", "payload": {"message": {"role": "assistant", "content": null, "tool_calls": [{"function": {"name": "search", "arguments": "{\"query\": \"玻璃杯 库存\"}"}}]}}, "note": "tool_call"}
{"ts": 9.97, "type": "user_input", "session": "A", "payload": {"text": "顺便说说会议室怎么订"}, "note": "steering"}
{"ts": 19.85, "type": "agent_reply", "session": "A", "payload": {"message": {"role": "assistant", "content": null, "tool_calls": [{"function": {"name": "search", "arguments": "{\"query\": \"会议室 预订\"}"}}]}}, "note": "tool_call"}
```

第三行值得盯 10 秒：它的 `ts` 是 9.97——消息**到达**的时刻——却排在
18.5 的记录后面，因为 log 按消费顺序追加，它是在 step 边界被 drain 的
那一刻写进去的。到达时间和消费时间是两个时刻，这一行就是"分类权在
消费端"的字面证据。

## 设计边界，以及下一章的需求

steering 有个天然的等待：它只在 step 边界生效，**正在飞的那一步等不了**。
上面的实测里这个等待真实发生了——插话 9.97s 发出，那一步 18.5s 才落地，
用户等了 8.5 秒才看到自己的话被消化。模型请求一旦发出，当前设计里
没有任何东西能把它掐掉。

而用户不等的时候会做什么？按停止。所以下一个需求：**中断**——
正在飞的模型请求要能被掐掉，而且不能把 agent 掐死。
这就需要一条和收件箱完全不同的车道：控制信号。Stage 3 见。

## 验证

- 环境：Python 3.13.12，openai SDK 2.46；模型走仓库根 `.env` 的 OpenAI
  兼容端点（本次实测用 `OPENAI_MODEL=qwen3.7-flash` 覆盖，原配模型额度
  已耗尽；demo 用真模型，会产生少量 token 费用）。
- 实跑：`stage02-demo` 输出即正文时间线与 session log（2026-09-09 真实
  LLM 运行，非手写）。
- pytest：`stage02-test` 4 passed，全部离线（FakeLLM 与 RealLLM 同协议）——
  handler 只投递立刻返回、followup 开新 turn、steering 在 step 边界生效
  （用 first_call_gate 把"消息在 step 在飞时到达"做成确定性时序）、
  多 session 收件箱与 history 隔离。FakeLLM 的 `first_call_tool` 把 turn
  撑成两步——一步的 turn 没有 step 边界，插话只能降级成 followup，
  这本身就是语义的一部分（消费那一刻分类）。
- 测试不用 pytest 的 tmp_path fixture（WorkBuddy 沙箱 shim 会拦
  pytest-of-unknown 的 mkdir），用 tempfile.mkdtemp 自建自清理。
