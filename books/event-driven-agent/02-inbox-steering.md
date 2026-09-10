# Stage 2：收件箱——followup 和 steering

> 配套代码：`src/baby_event_driven_agent/stages/stage02_inbox_steering/`，
> 可跑（`stage02-demo` / `stage02-test`）、带 tests。
> 模型和工具与 Stage 1 完全相同，这章只动一个地方：消息进来之后怎么排队。

## 需求来了

Stage 1 的 v0.1 有个预设的边界：一次处理一个请求。publish 原地 await handler，整个 turn 占着总线回调。
真实用户场景可不会遵从这样的预设，回答还在跑，他就想把下一句发出去。
可能是追问（一轮交互完成了，接着处理），即followup；
也可能是插话（把当前的输入，直接插入到当前的对话轮次中），即steering。
市面上绝大多数agent都支持这两种能力，比如workbuddy，

比如claude code

>差几张followup和steering的截图

所以 Stage 2 ，咱们就给这个事件驱动的agent，添加上followup和steering的能力

## 设计：收件箱 + 常驻 worker

回顾一下，在stage1中咱们的agent往总线注册消息订阅的时候，handler接收到消息时，直接启动了agent；
要支持followup和steering，在咱们的agent中，就需要一个收件箱来存储agent需要处理的消息，handler接收到消息的时候，不再是直接启动agent，而是先往agent的消息收件箱先写入消息，读取消息的逻辑，在agentloop中执行；
我们把agentloop设计成asyncio.Task，以便根据当前session的agentloop的状态来判断是否需要重新创建task；

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

demo 的兜底逻辑里藏着一个本章最重要的教训：插话降级成 followup 后，
**demo 绝不能在第一个 turn_end 就 stop()**——那会把还在收件箱里的
插话连 worker 一起掐死。消息没丢，是等它的人先走了。

steering 还有个天然的等待：它只在 step 边界生效，**正在飞的那一步
等不了**。本次实测插话 10.68s 发出、10.92s 被消化，只等了 0.24 秒——
运气好，正好赶上边界。但那不是设计保证的：如果当时在飞的是一次
8 秒的模型请求，用户就干等 8 秒，当前设计里没有任何东西能把它掐掉。

而用户不等的时候会做什么？按停止。所以下一个需求：**中断**——
正在飞的模型请求要能被掐掉，而且不能把 agent 掐死。
这就需要一条和收件箱完全不同的车道：控制信号。Stage 3 见。

## 验证

- 环境：Python 3.13.12，openai SDK 2.46；模型走仓库根 `.env` 的 OpenAI
  兼容端点（本次实测用 `OPENAI_MODEL=qwen3.7-flash` 覆盖，原配模型额度
  已耗尽；demo 用真模型，会产生少量 token 费用）。
- 实跑：`stage02-demo` 输出即正文时间线与 session log（2026-09-09 真实
  LLM 运行，非手写）。demo 的插话时机由 `tool_call_started` 事件驱动，
  非固定 sleep；两种结局（★ steering / 临界降级 followup）都在真实
  运行中出现过，屏幕可见。
- pytest：`stage02-test` 5 passed，全部离线（FakeLLM 与 RealLLM 同协议）——
  handler 只投递立刻返回、followup 开新 turn、steering 在 step 边界生效
  （first_call_gate 确定性时序）、**插话错过最后一个 drain 点降级为
  followup 且不丢**（临界降级测试）、多 session 收件箱与 history 隔离。
- 测试不用 pytest 的 tmp_path fixture（WorkBuddy 沙箱 shim 会拦
  pytest-of-unknown 的 mkdir），用 tempfile.mkdtemp 自建自清理。
