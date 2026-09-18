# Stage 4：消息机制 —— 事件离开 agent 之后要走多远

> 配套代码：`src/baby_event_driven_agent/stages/stage04_message_bus/`，
> `stage04-demo` 跑演示（前两段不打模型，可以当基准反复跑；后三段打真模型），
> `stage04-test` 跑测试（11 条离线 + 2 条真模型）。
> 本章**只强化 outbound**（事件离开 agent 之后怎么到达它的消费者）。
> inbound 的 `publish` 与 stage02/03 一字未改——上下行不对称是本章的前提，不是
> 要解决的问题。

到此为止，事件从 agent 到订阅者只有一行代码：

```python
for handler in subscribers[type]:
    await handler(event)
```

一行能撑住前三章，是因为前三章的订阅者只有一个（demo 的打印函数），而且
不慢。一旦订阅者是真 UI、真 embedder、真审计后端，这一行就开始出事。本章
把这行拆成六个机制，逐个解决一类问题。

## 需求：上行和下行根本不是一回事

前三章一直在往一个方向加东西：命令怎么进 agent（收件箱、steering、打断）。
那是**下行**。这一章看另一个方向：**上行**——事件离开 agent 之后怎么到达它的消费者。

两边的需求几乎相反，把它们塞进同一条通道，两边都会别扭：

| | 下行（命令 → agent） | 上行（事件 → 消费者） |
|---|---|---|
| 频率 | 低（一次 turn 几条） | 高（一次 turn 几百条 token 增量） |
| 丢了会怎样 | 用户的话丢了，不能接受 | token 丢一帧是屏幕少一个字；生命周期事件不能丢 |
| 顺序 | 要排队，语义由消费时机决定 | 生命周期事件要按序；token 流要合并 |
| 谁等谁 | 命令等 agent（收件箱排队） | **不能**让 agent 等消费者 |
| 数量关系 | 一个 agent | 多个消费者，广播 |

所以本章的全部机制都只加在上行。下行的 `publish` 一行没动：

```python
def publish(self, event: Event, to: str) -> None:
    sink = self._sinks.get(to)
    sink(event)          # 同步投递，立即返回
```

上行要解决的六件事，按“先能跑，再能定位，再能治理，最后能留下来”排：

1. **慢消费者拖死 loop** → 上行必须异步分发
2. **token 流洪峰淹没信号** → QoS 分道，命令通道和流式通道分离
3. **UI 刷不动** → 背压合并缓冲
4. **事件多了没法定位** → 事件信封（seq / correlation_id）
5. **发出去的消息要不要管** → 拦截 / 否决 / 改写治理；当场能判的归规则，
   要等人答复的归人（人工确认）
6. **崩溃之后怎么回放** → 落盘与消费位点

## 改动一：上行必须异步分发

慢消费者拖死 loop 的机制很简单：emit 里 `await handler`，那订阅者花的时间
就是 agent loop 花的时间。一个 5ms 的订阅者，一轮 200 个事件就是把 loop 拖慢
1 秒——而且拖慢的是**模型请求之间**的那段路，用户直接感觉到卡。

本章的做法：`emit` 只负责治理、落盘、入队，**不等订阅者**。每条道有自己的
worker 去送：

```python
async def emit(self, event: Event) -> EmitResult:
    decisions, current = await self._intercept(event)   # 1) 治理
    current = self._persist(current, decisions)         # 2) 落盘（seq 在这里分配）
    if any(d.action == DENY for d in decisions):
        return EmitResult(False, current, decisions)    # 3) 被否决就不投递
    await self._deliver(current)                        # 4) 入队，不等订阅者
    return EmitResult(True, current, decisions)
```

```python
class Lane:
    def __init__(self, name, maxsize, drop_when_full):
        self.queue = asyncio.Queue(maxsize=maxsize)
        ...

async def _lane_loop(self, lane: Lane) -> None:
    while True:
        event = await lane.queue.get()
        for sub in self._subs:                 # 慢订阅者只拖慢自己这条道
            if sub.mode != OBSERVE or not sub.matches(event.type):
                continue
            await self._notify(sub, event)
        lane.queue.task_done()
```

实测（demo 第 1 段，同一个 5ms/事件的订阅者，同样 200 个事件）：

```text
[实测] 同步扇出：200 个事件，loop 等订阅者等了 1.14s
[实测] 异步分发：同样 200 个事件，emit 只花 0.009s（订阅者的 1.14s 由 lane worker 背，drain 时才等）
       说明 │ 两套发法订阅者都收到了 200 条：异步分发没有少送，只是把"等"这件事从 loop 挪到了 lane worker。
```

`drain()` 是给 demo / 测试收尾用的：等两条道把队列里的东西发完。生产里不需要——
消费者自己会追上来，慢就慢着。

> **一个容易被忽略的事实**：`emit` 不主动让出事件循环（它只有入队和写盘，
> 没有 `await` 到 I/O）。lane worker 能跑起来，靠的是 agent loop 自己的 await
> 点——等 token 的那一下。洪峰压测里我用 `asyncio.sleep(0)` 模拟这个让出点，
> 否则生产者一次跑完，worker 一次都没被调度。

## 改动二：QoS 分道：洪峰淹没不了信号

上行挤着两种量级完全不同的东西：

- **生命周期事件**：`turn_end` / `agent_reply` / `tool_result` / `step_cancelled` …
  低频、**不可丢**、要按序。它们是事实，丢一条轨迹就断了。
- **token 流**：`agent_delta` / `agent_thinking`。高频、**可丢**、可合并。
  它们是呈现，丢一帧只是屏幕上少一个字。

挤在同一条队列里会发生什么：用户按停止之后，`turn_end` 排在几千个 delta 后面，
UI 要等洪峰刷完才显示"已停止"——用户以为没停住。这不是"停止信号丢了"（它走
inbound，根本不排队），是**停止的结果被洪峰堵住了**。

分两条道，各有自己的队列和 worker，各自的满了怎么办也分开定：

```python
STATE = "state"    # 生命周期 / 命令回执：满了 await put（背压回生产者），不丢
STREAM = "stream"  # token 流：满了丢最新，并计数

STREAM_TYPES = frozenset({"agent_delta", "agent_thinking"})

def lane_of(type: str) -> str:
    """默认走 state：新类型宁可先被当回事，也别悄悄被丢。"""
    return STREAM if type in STREAM_TYPES else STATE
```

```python
async def _deliver(self, event: Event) -> None:
    lane = self._lanes[lane_of(event.type)]
    self._ensure_workers()
    if lane.drop_when_full:
        try:
            lane.queue.put_nowait(event)
        except asyncio.QueueFull:
            lane.dropped += 1     # 可丢：丢的是 token 增量，不是事实
        return
    await lane.queue.put(event)   # 不可丢：宁可让 loop 慢下来
```

实测（demo 第 2 段，2000 个 token 增量里夹一条 `turn_end` 和一条完整答案
`agent_reply`，stream 队列设成 64）：

```text
[统计] stream 道：投递 101 / 丢弃 1899（0.18s）
[统计] state 道：投递 202 / 丢弃 0；turn_end 收到 1 条、agent_reply 收到 1 条
```

丢了一千多条 token 增量，但两条"不可丢"的一条没丢。这就是分道要买的东西。
"可丢"不是随便丢——它的代价和兜底都要说清楚：

- **代价**：屏幕上会少几个字的中间过程。
- **兜底**：完整答案走 state 道（`agent_reply`），一定到。UI 拿它把显示补齐，
  用户看到的最终文本是完整的。

反过来，state 道满了是 `await put`：生命周期事件宁可让 loop 慢下来也不丢。
这是设计选择，不是默认值——如果消费者真的挂了，宁可背压到源头，也不制造
一条永远对不上的轨迹。

## 改动三：背压合并缓冲：UI 刷不动

token 是一个一个来的，屏幕没必要一个一个刷。合并缓冲攒够了再刷，但**两个
触发条件缺一不可**：

- **满了就刷**：洪峰时不攒着，攒着就是延迟；
- **到帧界就刷**：只等满的话，尾巴上的字要等下一批才出来，UI 一顿一顿。

先到先刷：

```python
class CoalescingBuffer:
    def add(self, text: str) -> None:
        self._buf.append(text)
        self._size += len(text)
        if self._size >= self._max_chars:
            self.flush()

    async def _ticker(self) -> None:      # 帧界
        while True:
            await asyncio.sleep(self._frame)
            self.flush()
```

实测（demo 第 2 段末尾，换个不慢的消费者，800 个增量）：

```text
[统计] 同样的洪峰换个快消费者：800 个增量一条没丢，合并缓冲把它们刷成了 9 帧
```

800 次 IO 变成 9 次。缓冲放在**消费者侧**（demo 的 UI 订阅者里），不是总线里——
总线不知道谁要合并，也不该替每个消费者决定帧长。

## 改动四：事件信封：seq 和 correlation_id

事件一多，"第几条""属于哪一轮"就成了刚需。信封在 stage01 的
`(type, session_id, payload, ts)` 上加两个字段：

```python
@dataclass(frozen=True)
class Event:
    type: str
    session_id: str
    payload: dict = field(default_factory=dict)
    seq: int = 0              # 总线在 emit 时分配，落盘即编号
    correlation_id: str = ""  # 一次 turn 的簇 id
    ts: float = field(default_factory=time.time)
```

- **seq**：全局单调，**落盘即编号**（`EventLog.append` 分配），进程重启接着走。
  Stage 5 拿它当轨迹坐标（压缩区间、回放位点），Stage 6 拿它切 eval 切片。
- **correlation_id**：一次 turn 的簇 id，同一次用户输入引发的所有事件共享它。
  回放时能把散落的 token 流和生命周期事件聚成一簇。
  `tool_result` 与 `tool_call` 的配对**不占**这个字段——那是更细的一层关联，
  靠 payload 里的 `tool_call_id`。

```python
# agent 侧：一次 turn 一个簇 id
self._corr[sid] = f"turn-{uuid.uuid4().hex[:8]}"
```

实测（demo 第 3 段，真跑一轮）：这一轮上行 86 个事件，seq 单调、corr 同一簇；
真模型用例 `test_turn_has_envelope_and_dispatch_is_async` 就断言这两件事。

## 改动五：治理：拦截 / 否决 / 改写 / 人工确认

发出去的消息要不要管？前四章的回答是"不管"——emit 没有返回值，订阅者想看就看。
一旦有安全、合规、审计的需求，"不管"就不成立了：工具已经被执行了，再拦就晚了。

治理要回答的其实是两类问题，本章一次把两类都接上：

- **现在能不能判**？能判的（黑名单、脱敏规则、审计）→ 规则当场给答案（本节前半）。
- **现在判不了呢**？判不了的（要不要批准）→ 去问人，等人给答案（本节后半）。

拦截型订阅者在注册时就声明清楚自己的行为，运行时照单执行：

```python
@dataclass(frozen=True)
class Subscription:
    name: str                    # 进轨迹，用于归因
    event_types: tuple[str, ...]
    handler: Callable[[Event], Awaitable[Decision | None]]
    mode: str = OBSERVE          # OBSERVE：异步 fire-and-forget | INTERCEPT：串行、有预算
    order: int = 100             # 拦截型串行顺序，小的先跑
    on_failure: str = FAIL_OPEN  # 拦截者自己出问题时：放行还是拒绝
```

`emit` 返回一个 `EmitResult`，**agent 只看这个结果决定下一步**：

```python
gate = await self._emit("before_tool_call", sid,
                        {"name": name, "call_id": call["id"], "arguments": args_text})
if gate.needs_approval:                      # 有人说了"这得问人"（ASK）
    verdict = await self._request_approval(sid, name, call["id"], args_text, reason)
    if verdict is None:                      # 等待期间被中断：没批也没拒
        return NO_EXEC, False, True
    if verdict.action == DENY:
        return f"{APPROVAL_REJECTED}：{verdict.reason}", True, False
elif not gate.allowed:                       # 有人否了（DENY）
    reason = "; ".join(d.reason for d in gate.decisions if d.action == "deny")
    return f"{BLOCKED_PREFIX}：{reason or '被规则拒绝'}", True, False

args_text = str(gate.event.payload.get("arguments", args_text))  # 改写后的参数
result = await TOOLS[name](json.loads(args_text))
```

三条规则值得单独说：

1. **被否决的事件照样落盘**（顺序是：治理 → 落盘 → 判决 → 投递）。
   "有人试图做、被拒了"也是事实，事后要答得出"这个工具为什么没执行"。
   裁决本身跟着事件进 log。
2. **拦截者自己出问题时怎么办，注册时说清楚**：安全相关的用 `FAIL_CLOSED`
   （拦截者挂了就拒绝），观测类的用 `FAIL_OPEN`。运行时不猜。
3. **拦截者有预算**（默认 50ms，串行共用），超时按 `on_failure` 处理。
   治理不能反过来成为 loop 的瓶颈。

实测（demo 第 4 段，把 `update_rules` 拉黑后让 agent 去加一条规则）：

```text
[工具] ← update_rules 被治理拦下：[被规则拦截，未执行]：工具 update_rules 在黑名单里
[实测] rules.txt 未被改动（治理生效，工具没执行）
       说明 │ 被否决的结果作为一条 tool 消息进了上下文（1 条，占位以"[被规则拦截，未执行]"开头），
             模型知道这不是工具的真实输出；裁决本身也在 log 里。
```

占位必须是**自描述**的：模型拿到它要知道这不是工具的真实输出（否则会编造结果）。
这和 stage03 里"中断占位要自描述"是同一条原则。

> 治理点只有一个（`before_tool_call`），也是刻意只开一个。每多一个拦截点就多
> 一处"agent 和订阅者之间的隐式契约"，多了就管不住。真需要更多（消息出 agent
> 之前、工具结果回填之前），照这个形状加，但每次都要问一句：能不能改成观测？

### 等答复的那一半：人工确认

上面三类都是**当场算得出来**的：查集合、正则替换、写日志，微秒级。审批不是——答案是
**人**以后给的。两半的等待尺度差了四个数量级，所以要分开：

- **要不要问人**：当场判，还是拦截器，返回 `Decision.ask(...)`（微秒级）。
- **批还是拒**：等人给。agent 发一条 `approval_required` 出去，自己 `await` 一个 future；
  答复由 inbound 的 `user_approval` 直接 resolve 它。

为什么不让拦截器自己去 await 那个 future：它在 `emit` 的调用栈里跑，预算以毫秒计，
超时按 `on_failure` 走（默认放行）——把秒级的人命答复塞进去，等于让整个 loop 被一次
点击的等待掐着，而且超时那条路会直接放行，恰好是审批最不能接受的默认值。

```python
# 1) 规则当场判"要不要问人"
async def policy(event: Event) -> Decision | None:
    if event.payload.get("name") in gated:
        return Decision.ask(by, f"工具 {name} 需要人工确认")

# 2) agent 发请求，然后等；先登记 future 再发请求——答复可能比 await 先到
self._approvals[req_id] = fut
await self._emit("approval_required", sid,
                 {"request_id": req_id, "name": name, "arguments": arguments})
verdict = await asyncio.wait_for(fut, self.approval_timeout)   # 超时按拒绝

# 3) 答复走旁路，直接交给正在等它的那次等待（不进收件箱）
def on_approval(self, event: Event) -> None:
    fut = self._approvals.get(str(event.payload.get("request_id", "")))
    fut.set_result(self._verdict_from(event.payload))   # 批=allow 拒=deny 批并改写=modify
```

六条规矩：

1. **答复为什么不进收件箱**：等在那一头的不是队列。agent 此刻正卡在 step 里 await 那个
   future，而收件箱只在 **step 边界** 被 drain——答复排进队列就永远递不到。这和 Stage 3
   的中断是同一条理由。所以 `enqueue` 里现在有两条旁路（`user_interrupt` /
   `user_approval`），其余照旧排队。
2. **超时是拒绝，不是放行**（fail-closed）：等的这段时间里，唯一合理的默认值是"没批就不干"。
   占位也要分清"人拒了"和"没人答"——模型看到的文本、log 里的署名都得对得上。
3. **请求和答复都是事件**（`approval_required` / `approval_decided`），配对靠 payload 里的
   `request_id`（和 `tool_call_id` 一个路子，不占信封字段）。谁、什么时候、因为什么批的，
   都在 log 里——**审计链就是这么攒出来的，不用另写一套日志**。
4. **等的时候要能撤**：等待期间按停止，`on_interrupt` 直接结束这次等待（既不批也不拒），
   让 step 跑完、在 step 边界按 `interrupted` 结算——和 Stage 3"工具执行中不掐、跑完在
   边界收尾"是同一个形状。不这么做，用户按停止后得干等到确认超时。
5. **一问必有一答（回执）**：`approval_required` 发出去之后，不管结局是批准、拒绝、超时、
   还是被中断放弃，都必须紧跟一条 `approval_decided`——被中断放弃也算一种结局
   （`action=abandoned`）。只有请求没有回执，回放的人就不知道这次确认是结束了还是
   还挂着。唯一例外是进程被硬杀：和落盘的"残尾"一个道理，没写完的不算已发生。
6. **没人等的答复也要留痕**。人可能点晚了、点重了、号给错了——这些答复没有谁在等它，
   但**不能消失**：用户以为批了、系统按拒绝走了，两边就永远对不上账。落痕走总线的
   同步入口：

```python
def record(self, event: Event) -> Event:
    """同步落一条**已经在发生的事实**（不治理，但会异步补给观察者）。"""
    recorded = self._persist(event, ())          # seq 按因果顺序当场拿到
    task = asyncio.get_running_loop().create_task(self._deliver(recorded))
    ...
```

为什么要单独一个同步入口：inbound 的旁路是同步的（`publish` → `sink`，不能 await），
`emit` 是异步的，在那个上下文里 await 不了。代价说清楚——这条路径**不过治理**
（既成事实拦不了），**落盘同步、投递异步**（seq 当场拿到，观察者稍后才看到）。
所以它只给异常路径用：正常事件仍然只能走 `emit`。

注意"留痕"和"伪造裁决"是两件事：记录的是一条 `approval_reply`（带 `stale: true`），
不是给那次确认补一个批准——那次确认的回执早就写完了。

实测（demo 第 5 段：真模型 + 一个假装是人的订阅者）：

```text
[用户] 把保温杯库存改成 45 件
[人工] ？ update_inventory 要执行：{"category":"保温杯","stock":45,"spec":"316L不锈钢内胆，500ml，杯身磨砂黑"}
       说明 │ approval_required（seq=3354，request_id=ap-35e97dd1，超时 30s）：agent 正挂在这
             次等待上，等的人不在队列那头
[人工] → 批准
[系统] 确认结果：allow by user（seq=3355）
[工具] ← update_inventory 结果：已更新：保温杯：库存 45 件；316L不锈钢内胆，500ml，杯身磨砂黑。
[人工] （手抖又点了一下同一条确认）
[系统] 又一条答复到了（request_id=ap-35e97dd1）：没人在等它了 → 不认领，只留痕（seq=3398）
[实测] 工具真执行了，副本上现在是：保温杯：库存 45 件；316L不锈钢内胆，500ml，杯身磨砂黑。
```

（`approval_required` 的 seq=3354 和 `approval_decided` 的 seq=3355 挨着：请求和回执在
轨迹上是相邻的两条，中间没有别的事件插进来——因为等的那段时间里 loop 什么都没干。
重点的那个答复拿到了 seq=3398，比它"到达的真实时刻"晚了：它是同步落盘、异步投递的，
号是当场拿的，观察者稍后才看到。）

> **单进程的边界**：等待靠的是进程内的一个 future，所以答复必须送到**同一个进程**。
> 多实例部署时这条不成立——那时候"谁在等"要靠位点续读（读 log 里那条
> `approval_required`）+ 带 `request_id` 的答复通道，或者由一个中央审批服务持有这份状态。
> 这部分和"走出单机"一起留在降级项里。

## 改动六：落盘与消费位点

前三章的 session log 是"一行一条 json"。它有两个问题：崩在写一半时最后一行是
残的，而"这条记录完不完整"在 jsonl 里**没法判定**——读的一方只能 try/except，
炸了之后要么整段读不出来，要么人肉修；另一个问题是没法按段保留、按位点续读。

本章换成段 + 稀疏索引 + 长度前缀 + CRC：

```python
line = b"%08x %08x " % (len(data), zlib.crc32(data)) + data + b"\n"
```

```python
data = parts[2][:-1] if parts[2].endswith(b"\n") else parts[2]
if len(data) != length or zlib.crc32(data) != crc:
    out.append((None, False))    # 长度/CRC 对不上 = 崩在写一半
    return out
```

"这条记录完不完整"变成一个可判定的问题：撞上残尾就停在那里，前面已落盘的
一条不少。

- **段与稀疏索引**：写满一段（默认 64KB）滚动新段，`index.json` 记每段的
  first/last seq（段内顺序扫描，和 Kafka 一个路子）。索引丢了能从段文件重建——
  段文件自己是自描述的。
- **保留**：按段数滚动删除最老的段（默认留 8 段）。
- **脱敏**：默认 `redact_secrets` 只改**落盘**那份，内存里的事件不动。
  改内存会让治理看到的和落下的对不上账——脱敏是最后一道，不是第一道。
- **位点**：`offsets.json` 记每个消费者"已消费到的最后一条 seq"，重启后从
  下一条续读（`read_since`）。这就是断点续放。

实测（demo 第 5 段）：

```text
[统计] 落盘：8 段 / 230243 字节 / seq 1..3434（最老可用 1976）
[统计] 位点：ui 已消费到 seq 5，从下一条续读 → 1459 条（1976..3434）
       说明 │ 位点 5 已经落在保留窗口之外（最老可用 1976）：保留策略删掉的段读不回来，
             续读只能从现存最老的一条开始——这是保留与位点唯一的冲突点。
[统计] 残尾：完好时这一段读到 2 条；砍掉最后 11 字节后读到 1 条（停在坏记录之前，没炸）
```

两处顺带的设计选择：

- **落盘只有一处**：总线 `emit` 里。agent 不再自己写文件——事实层不能有两个
  写入者，否则 seq 和顺序都对不上。连**合成消息**（中断标记、assistant 占位、
  纠正 user）都通过 emit 出去（payload 里带 `synthetic: true` 和 `note`），
  否则"history 是 log 的投影"这条会在中断这条路上断掉。
- **落空的中断不留痕**：什么都没发生，就没有事实可记。生效的中断会变成
  `step_cancelled` / `turn_interrupted`（都带 intent），不需要再记一笔"收到过"。

## 代码改动小结

| 文件 | 行数 | 干什么 |
|---|---|---|
| `events.py` | 140 | 事件信封（seq / correlation_id）、QoS 两条道、`Subscription` / `Decision`（含 `ask`）/ `EmitResult` |
| `bus.py` | 279 | inbound `publish`（未改）+ outbound `emit` 四步 + Lane worker + 治理预算 + `record`（同步落痕）+ `drain` / `stats` |
| `persistence.py` | 260 | `EventLog`：段 + 稀疏索引 + 长度前缀 + CRC + 保留 + 脱敏 + 位点 |
| `outbound.py` | 80 | `CoalescingBuffer`：满了或到帧界，二者其一 |
| `subscribers.py` | 106 | 示例订阅者：`permission_guard` / `approval_policy`（两个拦截）/ `slow_observer` / `counter`（观测） |
| `agent.py` | 643 | stage03 全部能力 + `_emit` 单一出口 + `before_tool_call` 治理点 + 人工确认（等待 / 超时 / 回执 / 答复旁路） |
| `llm.py` | 264 | 与 stage03 相同（未改） |
| `main.py` | 514 | 六段 demo，前两段不打模型 |
| `tests/` | 785 | 17 条离线 + 2 条真模型 |

agent 侧的改动只有三处，都很小——这正是把机制放在总线上的意义：

1. 所有 outbound 走 `self._emit(...)`（带 turn 级 correlation_id）。
2. 工具执行前多一个 `before_tool_call` 拦截点，被否决就不执行。
3. 拦截者说"要问人"时，agent 发 `approval_required` 并等答复；答复从
   `user_approval` 旁路进来，直接交给那次等待；每种结局都补一条 `approval_decided`
   回执，没人等的答复走 `bus.record` 单独留痕。

中断与转向的逻辑一字未动：那是 Stage 3 的事，跟"事件怎么到达消费者"无关。

## 跑一下（真实 LLM 实测输出）

```text
── 第 1 段：同步扇出 vs 异步分发（同一个慢订阅者） ──
       说明 │ 慢订阅者每个事件睡 5ms，同样发 200 个事件。左边是 stage02/03 的写法
             （emit 里 await 每一个订阅者），右边是本章的写法（emit 只入队，lane worker 去送）。
[实测] 同步扇出：200 个事件，loop 等订阅者等了 1.14s
[实测] 异步分发：同样 200 个事件，emit 只花 0.009s（订阅者的 1.14s 由 lane worker 背，drain 时才等）
       说明 │ 两套发法订阅者都收到了 200 条：异步分发没有少送，只是把"等"这件事从 loop 挪到了 lane worker。

── 第 2 段：洪峰压测：token 流里夹一条 turn_end ──
       说明 │ 直接往总线灌 2000 个 token 增量（stream 道，队列 64，满了丢最新），中间夹一条
             turn_end（state 道，不可丢）。看两条道各自的账。
[统计] stream 道：投递 101 / 丢弃 1899（0.18s）
[统计] state 道：投递 202 / 丢弃 0；turn_end 收到 1 条、agent_reply 收到 1 条
       说明 │ 两条道各有自己的 worker：token 流堵了只丢自己的，turn_end 排在洪峰之后也照样先到 ——
             这就是 QoS 分道要买的东西。丢的是可丢的：屏幕上会少几个字，但完整答案走 state 道
             （agent_reply）一条没丢，UI 拿它兜底就能补齐。
[统计] 同样的洪峰换个快消费者：800 个增量一条没丢，合并缓冲把它们刷成了 9 帧

── 第 3 段：真跑一轮：慢订阅者不拖 loop，UI 用合并缓冲刷屏 ──
[用户] 保温杯还有库存吗
[思考] 用户想知道保温杯还有没有库存，我需要调用 query_inventory 工具来查询"保温杯"这个品类的库存信息。
[工具] ← query_inventory 结果：保温杯：库存 42 件；316L 不锈钢内胆，500ml，杯身磨砂黑。
[系统] turn 结束（reason=turn end）
[实测] 这一轮上行 86 个事件（其中 30 个是 token 增量）；从投递到 turn_end = 8.40s
       说明 │ 同步扇出的写法要在每个事件上等 5ms，光等待就 ≈ 0.43s（这一轮的实测总耗时是 8.40s，
             里面主要是模型请求）
[实测] 合并缓冲：79 个增量 → 50 帧（每帧 ≤96 字或 50ms 一次）

── 第 4 段：当场否决：规则说了算 ──
[用户] 加一条规则：会议室要提前一天预订
[工具] ← update_rules 被治理拦下：[被规则拦截，未执行]：工具 update_rules 在黑名单里
[系统] turn 结束（reason=turn end）
[实测] rules.txt 未被改动（治理生效，工具没执行）
       说明 │ 被否决的结果作为一条 tool 消息进了上下文（1 条，占位以"[被规则拦截，未执行]"开头），
             模型知道这不是工具的真实输出；裁决本身也在 log 里。

── 第 5 段：人工确认：人说了算（等答复的那一半） ──
[用户] 把保温杯库存改成 45 件
[人工] ？ update_inventory 要执行：{"category":"保温杯","stock":45,"spec":"316L不锈钢内胆，500ml，杯身磨砂黑"}
       说明 │ approval_required（seq=3354，request_id=ap-35e97dd1，超时 30s）：agent 正挂在这
             次等待上，等的人不在队列那头
[人工] → 批准
[系统] 确认结果：allow by user（seq=3355）
[工具] ← update_inventory 结果：已更新：保温杯：库存 45 件；316L不锈钢内胆，500ml，杯身磨砂黑。
[系统] turn 结束（reason=turn end）
[人工] （手抖又点了一下同一条确认）
[系统] 又一条答复到了（request_id=ap-35e97dd1）：没人在等它了 → 不认领，只留痕（seq=3398）
[实测] 工具真执行了，副本上现在是：保温杯：库存 45 件；316L不锈钢内胆，500ml，杯身磨砂黑。
       说明 │ 确认的请求和结果都是事件，seq 排得出来：什么时候问的、谁批的、批完工具返回了什么。

── 第 6 段：落盘与位点 ──
[统计] 落盘：8 段 / 257156 字节 / seq 1..3398（最老可用 1758）
[统计] 位点：ui 已消费到 seq 5，从下一条续读 → 1641 条（1758..3398）
[统计] 残尾：完好时这一段读到 153 条；砍掉最后 11 字节后读到 152 条（停在坏记录之前，没炸）

── history 尾部（这一轮的真实消息形状）──
  {"role": "assistant", "content": null, "tool_calls": [{"id": "call_xov5nao9", "type": "function", "function": {"name": "update_inventory", "arguments": "{\"category\":\"保温杯\",\"stock\":45,\"spec\":\"316L不锈钢内胆，500ml，杯身磨砂黑\"}"}}]}
  {"role": "tool", "tool_call_id": "call_xov5nao9", "content": "已更新：保温杯：库存 45 件；316L不锈钢内胆，500ml，杯身磨砂黑。"}
  {"role": "assistant", "content": "已将保温杯库存改为45件，规格为316L不锈钢内胆、500ml容量、磨砂黑杯身。"}

[系统] demo 结束
  session log: .../src/baby_event_driven_agent/sessions/stage04
```

`_step` 里可见文本是边到边发 `agent_delta`、边累积的，所以 `[回答]` 是逐帧
出来的；模型先调 `query_inventory`，真结果回填后才给最终答复。

## 验证

- 环境：Python 3.13.12，openai SDK；模型走仓库根 `.env` 的 OpenAI 兼容端点
  （2026-09-18 实测是本地 ollama 的 `qwen3.5:4b-32k`）。
- pytest：`stage04-test` **19 passed**（2026-09-18 真跑，11.01s）——17 条离线、
  2 条打真模型。
  离线（不打模型，全是传输层和治理的事，跟模型无关）：
  - 慢订阅者（10ms/事件）不拖慢 emit：60 个事件的 emit 耗时远小于同步扇出，
    且 60 条一条没丢；
  - 洪峰：stream 道丢自己的（`dropped > 0`）、state 道一条不丢、`turn_end` 收到；
  - 信封：seq 单调（1,2,3）、重新打开目录（模拟重启）接着编号、位点续读正确；
  - correlation_id：一轮对话的所有事件同簇；
  - 治理（当场判）：否决 → 工具一次没执行（用探针替掉 `update_inventory`，不碰
    仓库里的知识库文件）、上下文里是自描述占位、log 里有 deny 裁决；
    改写 → 工具按改过的参数执行；
  - 人工确认（等人答）：批准 → 工具真执行，`approval_required` 与
    `approval_decided` 靠 `request_id` 配对、署名是 `user`；
    拒绝 → 不执行 + `[人工确认未通过，未授权执行]` 占位、署名 `user`；
    批准并改写参数 → 按人给的参数执行（裁决是 `modify`）；
    超时 → **按拒绝处理**（`fail-closed`）、署名 `approval_timeout`、占位是
    `[人工确认超时，未授权执行]`；
    等待期间被中断 → 这次调用记 `[等待人工确认期间被中断，未授权执行]`、turn 以
    `interrupted` 收尾，**回执照样有**（`action=abandoned`，`by=user_interrupt`）；
    迟到 / 号不对的答复 → 不认领、不伪造裁决，但落一条 `approval_reply`
    （`stale: true`）**留痕**，那条等待继续走到超时；
  - 落盘：砍掉最后 11 字节（崩在写一半）→ 读到坏记录为止，前 3 条完好；
    位点续读；脱敏只改落盘那份（内存里的事件不变）；保留按段滚动，老段读不回来；
  - 合并缓冲：不满就等帧界、满了立刻刷、stop 时把尾巴刷掉。
  真模型：
  - 一轮真对话：事件带 seq / correlation_id 且 seq 单调；`turn_end` 一条没丢；
    事件数够多（≥30）时断言"慢观测者在 turn_end 那一刻还没追上"——分发的确
    没在 loop 里等订阅者。事件太少时滞后可能不存在，那就只验信封，不硬凑断言；
  - 回归：换了传输层之后，stage03 的"掐掉在飞的一步、turn 以 interrupted 收尾"
    照旧，且合成消息也进了事实层（否则回放重建不出这段 history）。
  demo 第 5 段是唯一一条"人在回路"的端到端证据（真模型触发、真事件、真执行）；
  写工具落在 `sessions/stage04/inventory-demo.txt` 这份副本上，好让 demo 能反复跑。
- **治理用例为什么用脚本化 LLM**：要确定性地触发一次工具调用（"模型会不会调
  那个工具"不该是这条用例的变量）。其余用例不设替身——本章要验的传输层行为
  跟模型无关，真模型只在"跑通一整轮"那两条上出现。
- 测试不用 pytest 的 tmp_path fixture（沙箱 shim 会拦 `pytest-of-unknown` 的
  mkdir），用 `tempfile.mkdtemp` 自建自清理。

## 降级项：真上生产还要补什么

本章解决的是**单进程内**的传输层。跨进程 / 跨机器这几件事没做，也不该在这一章做：

- **多进程 / 多实例部署**：现在的 Lane 是进程内的 `asyncio.Queue`，另一个进程
  看不到。要做跨进程，得把队列换成真的消息中间件（Redis Stream / NATS / Kafka），
  信封和分道的形状不用改，改的是 Lane 的落地方式。
- **跨进程的人工确认**：等待靠进程内的 future，所以答复必须回到**同一个进程**
  （见"改动五"末尾那条边界）。跨进程要么靠位点续读 + 带 `request_id` 的答复通道，
  要么由一个中央审批服务持有"谁在等"这份状态。
- **答复里没有"人"的身份**：`approval_decided` 的署名只有 `by: "user"`，没有
  "哪个用户、用哪个凭证批的"。真上生产这里要接身份（谁批的比批了什么更重要）。
- **进程被硬杀时，可能只有请求没有回执**：`approval_required` 落了盘、
  `approval_decided` 没来得及落，中间进程没了。这和落盘的"残尾"是同一类问题
  （没写完的不算已发生），但回放的人需要知道"看到孤立的请求 = 那次没走完"。
- **`record` 这条同步路径绕过了治理**：它只给异常路径用（目前只有"没人等的答复"）。
  用错地方就等于开了个后门——正常事件必须走 `emit`。
- **消费者组与位点并发**：现在位点是一个消费者一个数字，没有 group、没有
  rebalance、没有"多个实例同时消费一个会话"的约定。
- **持久化强度**：落盘只 `flush` 到 OS，不 `fsync`。崩进程不丢已落盘的部分
  （这是本章要保证的），崩机器可能丢最后几行。`fsync` 在 token 流的热路径上
  太贵，真要强保证就把落盘挪到单独的 IO 线程 / 进程。
- **脱敏字段清单**：默认清单是一条正则（`token|secret|password|api[_-]?key|…`），
  真上生产要按业务定死，并且要有测试兜着。
- **事件 schema 演进**：type 和 payload 的形状会变，回放旧轨迹需要版本号或
  兼容规则——本章没有。
- **审计与回溯**：裁决链已经在了（谁批的、什么时候、因为什么，都在 `approval_decided`
  里），缺的是**规则自己怎么变的**——黑名单/审批名单是代码里的常量，没有版本、
  没有"谁在什么时候把它改成这样"的记录。

## 下一章

Stage 4 保证了事件**能到达、能定位、能治理、能留下来**。Stage 5（会话与轨迹）
接着解决另一半：多轮对话的状态怎么管。

- `history` 是内存里的工作态，进程一重启就没了——Stage 5 把它换成会话存储，
  `seq` 正好就是轨迹坐标（压缩区间、回放位点都靠它）；
- `correlation_id` 已经把一次 turn 的事件聚成一簇，Stage 5 要的是把这些簇
  串成一条**可回放、可裁剪、可压缩**的轨迹；
- Stage 6 的 eval 则直接拿 `seq` 切切片：从哪一条到哪一条，重放一遍看结果。
