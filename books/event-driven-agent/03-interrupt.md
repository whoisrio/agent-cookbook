# Stage 3：打断与转向（interrupt + redirect）

> 配套代码：`src/baby_event_driven_agent/stages/stage03_interrupt/`，
> `stage03-demo` 跑演示（真模型），`stage03-test` 跑离线测试。
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

## 机制：把“正在飞的一步”变成可以被取消的 task

改动集中在 agent 的 turn 循环里，一共三处。

第一处，给每个 session 记录当前在飞的 step。
step 不再直接 await，而是包成一个 task 挂在 `_inflight` 上：

```python
step_task = asyncio.create_task(self._run_step(sid, history))
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
被取消的 step 不会留下半截消息。

第二处，中断信号的 handler。
它注册在总线的 `user_interrupt` 事件上，做的事只有一件：

```python
async def on_interrupt(self, event: Event) -> None:
    sid = event.session_id
    task = self._inflight.get(sid)
    if task is not None and not task.done():
        task.cancel()
    self.log.append(event, note="interrupt received")
```

查一下这个 session 有没有在飞的 step，有就 cancel。
检查和 cancel 之间没有 await，在 asyncio 单线程里是原子的。
cancel 作用在具体的 task 对象上，不在飞的 session 查到的是 None，信号自然落空——
不存在“信号残留下来杀错下一个 turn”的问题。

有一种边角要说明：信号到达的那一刻，step 恰好刚完成（`task.done()`），
取消就落空，turn 正常收尾。
取消请求和完成在竞速，完成的赢者已定。
这不是缺陷，是取消类操作的固有语义，UI 上如实打印即可。

第三处，被取消之后 turn 怎么收尾。
`await step_task` 处会抛出 CancelledError，先分清是谁被取消了：

```python
except asyncio.CancelledError:
    if not step_task.cancelled():
        raise  # 是 turn 协程自己被 stop() 取消，继续往外抛
    self._inflight[sid] = None
    self.log.append(Event("step_cancelled", sid, {}), note="interrupt")
    await self.bus.publish(Event("step_cancelled", sid, {}))
    await self._end_turn(sid, "interrupted")
    return
```

是中断就记 log、发事件、turn 以 `interrupted` 收尾。
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

三个动作：问保温杯（完整一个 turn）；紧接着问报销，
等它真的发起工具调用、step 正在飞时按停止；中断之后再问 VPN。

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

第三轮有一个值得注意的真实行为：模型并发发起了两次检索，
一次查 VPN，另一次查的是报销——那个被中断的问题。
原因是中断不作废历史：
“报销有什么规定”这条 user 消息在 turn 开始时就写进了 history，
被取消的只是那次的 assistant 回复，
provider 对消息序列的要求是完整的 assistant 消息，user 消息悬着完全合法。
模型看到 history 里有一个没回答的问题，自己把它补上了。
如果产品上不想要这个行为（用户按停止就是不想听到报销的事），
中断时把当前 turn 的 user 消息从 history 里撤掉即可，两条路都通，
本 stage 选择保留，因为它让“history 是 log 的投影”这条线更清楚。

session log 里中断的痕迹（节选）：

```json
{"ts": 8.24, "type": "user_input", "session": "A", "payload": {"text": "报销有什么规定"}, "note": "turn start"}
{"ts": 9.0, "type": "user_interrupt", "session": "A", "payload": {}, "note": "interrupt received"}
{"ts": 9.01, "type": "step_cancelled", "session": "A", "payload": {}, "note": "interrupt"}
{"ts": 9.02, "type": "turn_end", "session": "A", "payload": {}, "note": "interrupted"}
```

中断请求本身（`user_interrupt`）无论命中与否都进 log——它是发生过的事实；
是否命中由 `step_cancelled` 有没有出现来判断。

## 从“停”到“转向”：本章后半的 redirect

中断解决的是“怎么停”，但停完之后用户面对的是一个问题：
被掐掉的那个 turn 结束了，如果用户的意思不是“别答了”而是“我改主意了，换个问法”，
他就得把新问题当成一条全新消息重发，agent 从头理解一遍。
用户的真实意图往往是：接着当前的进度干，只是方向变了。

把这两种意图区分开，需要一种新机制：
同样是掐掉正在飞的一步，turn 不结束，被掐断的输出还要按 provider 的格式要求补齐消息序列，
然后立刻重发。
这一机制就是本章后半要展开的 redirect。

再往后（Stage 4）：loop 往总线 emit 的东西越来越多，上行的量一上来，
总线和 UI 就顶不住了——那是下一章的事。

## 验证

- 环境：Python 3.13.12，openai SDK；模型走仓库根 `.env` 的 OpenAI 兼容端点
  （本次实测用 `OPENAI_MODEL=qwen3.7-flash` 覆盖；demo 用真模型，会产生少量 token 费用）。
- 实跑：`stage03-demo` 输出即正文时间线与 session log（2026-09-09 真实 LLM 运行，非手写）。
- pytest：`stage03-test` 4 passed，全部离线（FakeLLM 与 RealLLM 同协议）——
  中断取消在飞 step 且 turn 收尾（history 尾部无半截 assistant，后续问答正常）、
  空闲时中断落空且不影响下一个 turn、排队消息在中断后不丢（被下一 turn 的
  drain 当 steering 消化，与 stage02 语义一致）、step 重构后 steering 回归。
  FakeLLM 的 first_call_gate 把第一步挂在 `Event.wait()` 上，
  取消信号的注入点是确定的，测试不靠 sleep 碰运气。
- 测试不用 pytest 的 tmp_path fixture（WorkBuddy 沙箱 shim 会拦
  pytest-of-unknown 的 mkdir），用 tempfile.mkdtemp 自建自清理。
