# Stage 4：消息机制 —— 事件离开 agent 之后要走多远

> 配套代码：`src/baby_event_driven_agent/stages/stage04_message_bus/`，
> `stage04-demo` 跑演示（第 1、2、6 段不打模型，可以当基准反复跑；第 3~5 段打真模型），
> `stage04-test` 跑测试（24 条离线 + 2 条真模型）。
> 本章强化 outbound（事件离开 agent 之后怎么到达它的消费者），下行只动一处：
> 收件箱按意图分成两条队列，`publish` 链路与 stage02/03 一字未改——上下行
> 不对称仍是本章的前提，不是要解决的问题。
到目前为止，我们已经给用户的输入消息添加了steering、followup以及interrupt的机制，但是一条消息是插话还是
排队，取决于消息**被消费的那一刻** agent 在干什么，是自动agent自行识别确认的，并且消息的先后顺序以及优先级并不能控制，只是按照消息来到的顺序处理，如果用户希望主动控制消息处理的时机呢 ? 

另外，在agent的事件消费端，agent 把 emit 出的事件交给订阅者的代码还是这两行：

```python
for handler in subscribers[type]:
    await handler(event)
```

这行代码无差别逐个 await 订阅 emit 事件的 handler 执行。
显然，emit 的一方未必希望等所有消费者都处理完，比如纯粹的广播通知，因此消息的等待机制要可定义；
消费者也各有快慢，现在的 flash 模型每秒能吐几百 token，stream 模式下 UI 上的呈现不能还是来一个 token 就刷一次；
工具调用该不该执行，也不能"发出去就完事"。这一章，我们就从上行消息（agent emit）讲起，把它要解决的这几件事逐一落地；最后回头补一刀下行：多条消息排队时谁先被处理——如同上章的打断信号要插队，下行也得有优先级。

## 输入消息的优先级：插队有先后
先把消息处理的控制权还给用户；
那么在agent正在执行过程中的消息，默认都放在等待队列中，也就是followup；
如果用户希望插话，那么就通过插入的方式让agent在下一个step与LLM交互之前，把用户指定的消息消费掉；
如果用户希望立刻打断当前对话，那么通过主动interrupt当前的step，让agent立刻处理新的消息；
如果用户输入了多组消息，那么还需要能够控制消息处理的优先级；

所以，我们把agent的消息inbox，分为了steering inbox和followup inbox，用户的消息默认进入followup inbox；
用户希望下一个step插话时，消息就进入steering inbox；
用户希望新的消息立刻被消费，通过打断的操作，agent中断当前任务的执行，会立刻消费followup inbox里最新的一条消息；

前几章，我们用asyncio.queue来承载用户输入消息，但是asyncio.queue方便处理FIFO，但是对插队处理反而不方便，所以我们把
两个inbox改成用list来承载；

```python
  self._followups: dict[str, list[tuple[int, int, Event]]] = {} # {'session_id':[(priority,arrival,event)]}
  self._steerings: dict[str, list[tuple[int, int, Event]]] = {}
```
用户的输入消息默认投递到followup inbox，针对steering的操作，除了在每一次与llm交互的step中从steering box取最新的消息；在每一次turn开始时，同样也会优先处理steering inbox未被处理掉的消息，避免steering触发时loop即将走到边界导致steering消息未被正确提取。


另外，agent的输入消息和agent过程中emit的事件，从原始的`Eevet`中派生出各自的子类型，以便针对不同的消息处理和消费逻辑做对应的处理
下行消息声明的是"怎么排队"，两个方向的东西都收在各自的子类里。
```pyhon

```

上行事件约束的是"谁消费我"（stream 事件的子类声明只允许邮箱型消费者），
```python

```




## emit事件投递与处理分离
慢消费者拖慢的原因是emit里 `await handler`，agent loop发出事件后等待所有handler处理完，才能进行下一步处理；
所以，你一定想到了，在agent的emit消息的消费端，同样把消息投递和处理分开；

```python
async def emit(self, event: Event) -> None:
    for sub in self._subs:
        if not sub.matches(event.type):
            continue
        if isinstance(sub.handler, Mailbox):
            sub.handler.offer(event)    # 邮箱型：热路径只做缓冲追加
        else:
            await sub.handler(event)    # await 型：契约是微秒级只做接收
```

`emit` 只有分派：没有治理、没有裁决、没有返回值。治理不在这里——"工具执不执行"
是 agent 工具执行路径上的准入问题，跟"事件怎么到达消费者"根本不是一件事，
完整机制在"消息确认机制"一节展开。

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
[实测] 同步扇出：200 个事件，loop 等订阅者等了 1.13s
[实测] 异步分发：emit 只花 0.002s，发完时已顺带送掉 0 条（队列压着 199 条，drain 再等 1.14s）
       说明 │ 两套发法订阅者都收到了 200 条：异步分发没有少送，只是把"等"这件事从 loop 挪到了 lane worker。
```

`drain()` 是给 demo / 测试收尾用的：等两条道把队列里的东西发完。生产里不需要——
消费者自己会追上来，慢就慢着。

> **一个容易被忽略的事实**：`emit` 不主动让出事件循环（没有 `await` 到 I/O）。
> lane worker 能跑起来，靠的是 agent loop 自己的 await 点——等 token 的那一下。
> 洪峰压测里我用 `asyncio.sleep(0)` 模拟这个让出点，否则生产者一次跑完，worker
> 一次都没被调度。

### stream消息消费缓冲

上行挤着两种量级完全不同的东西：

- **生命周期事件**：`turn_end` / `agent_reply` / `tool_result` / `step_cancelled` …
  低频、**不可丢**、要按序。它们是事实，丢一条轨迹就断了。
- **token 流**：`agent_delta` / `agent_thinking`。高频、**可丢**、可合并。
  它们是呈现，丢一帧只是屏幕上少一个字。

挤在同一条队列里会发生什么：用户按停止之后，`turn_end` 排在几千个 delta 后面，
UI 要等洪峰刷完才显示"已停止"——用户以为没停住。这不是"停止信号丢了"（它走
inbound，根本不排队），是**停止的结果被洪峰堵住了**。

缓冲层按事件量级分流：生命周期事件一条队列，token 流一条队列，各自的满了怎么办分开定：

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
[统计] stream 道：投递 65 / 丢弃 1935（0.01s）
[统计] state 道：投递 202 / 丢弃 0；turn_end 收到 1 条、agent_reply 收到 1 条
```

丢了一千多条 token 增量，但两条"不可丢"的一条没丢。这就是"可丢的丢、不可丢的背压"要买的东西。
"可丢"不是随便丢——它的代价和兜底都要说清楚：

- **代价**：屏幕上会少几个字的中间过程。
- **兜底**：完整答案走 state 道（`agent_reply`），一定到。UI 拿它把显示补齐，
  用户看到的最终文本是完整的。

反过来，state 道满了是 `await put`：生命周期事件宁可让 loop 慢下来也不丢。
这是设计选择，不是默认值——如果消费者真的挂了，宁可背压到源头，也不制造
一条永远对不上的轨迹。

## 消息确认机制，hitl

发出去的消息要不要管？前几章的回答是"不管"——emit 没有返回值，订阅者想看就看。
一旦有安全、合规的需求，"不管"就不成立了：工具已经被执行了，再拦就晚了。

治理要回答的其实是两类问题，本章一次把两类都接上：

- **现在能不能判**？能判的（黑名单、规则匹配）→ 规则当场给答案（本节前半）。
- **现在判不了呢**？判不了的（要不要批准）→ 去问人，等人给答案（本节后半）。

两类问题在两个地方落位，都不在总线上：

- **治理**（当场判）：agent 工具执行路径上的准入关卡。规则在注册时声明清楚
  自己的行为，`Governor` 把它们组织成一条串行链：

```python
@dataclass(frozen=True)
class Rule:
    name: str                    # 进裁决署名，事后答得出"谁拒的"
    handler: Callable[[str, str], Awaitable[Decision | None]]  # (工具名, 参数)
    order: int = 100             # 串行顺序，小的先跑
    on_failure: str = FAIL_OPEN  # 规则自己出问题时：放行还是拒绝
```

- **审批**（要等人）：声明在工具自己身上——`Tool.requires_approval`，
  和 `HANDLER_SHAPE` 声明在事件类上是同一个道理：约束归声明方。

agent 执行一次工具调用前的两道关卡，顺序就是"先能判的、再要等的"：

```python
verdict = await self.governor.check(name, args_text)   # 1) 治理：规则链当场判
if not verdict.allowed:
    reason = "; ".join(d.reason for d in verdict.decisions if d.action == DENY)
    return f"{BLOCKED_PREFIX}：{reason or '被规则拒绝'}", True, False
args_text = verdict.arguments                          # 规则改写过的参数

tool = self.tools.get(name)
if tool is not None and tool.requires_approval:        # 2) 审批：工具声明要问人
    answer = await self._request_approval(sid, name, call["id"], args_text,
                                          tool.approval_reason)
    if answer is None:                                 # 等待期间被中断：没批也没拒
        return NO_EXEC, False, True
    if answer.action == DENY:
        return f"{APPROVAL_REJECTED}：{answer.reason}", True, False
    if answer.patch:                                   # 人的改写最后覆盖
        args_text = str(answer.patch.get("arguments", args_text))

result = await tool.fn(json.loads(args_text))
```

审批的回路不在总线上：agent 发一条 `approval_required` 出去（谁扮演"人"，谁就
订阅它），然后自己 await 一个 future；答复由 `user_approval` 旁路直接 resolve
——等在那一头的不是队列。等不到（超时）按拒绝处理（fail-closed），无论哪种
结局都补一条 `approval_decided` 回执：一问必有一答。

三条规则值得单独说：

1. **被否决的工具不执行**，裁决由 `Verdict` 带回 agent，agent 据此补一条
   自描述占位进上下文——占位文本自描述"为什么没执行"，事实已经留下来了；
   下一章它们随 history 落进轨迹（唯一的事实层）。
2. **规则自己出问题时怎么办，注册时说清楚**：安全相关的用 `FAIL_CLOSED`
   （规则挂了就拒绝），其余用 `FAIL_OPEN`。运行时不猜。
3. **规则链有预算**（默认 50ms，串行共用），超时按 `on_failure` 处理。
   治理不能反过来成为 loop 的瓶颈。

实测（demo 第 4 段，把 `update_rules` 拉黑后让 agent 去加一条规则）：

```text
[工具] ← update_rules 被治理拦下：[被规则拦截，未执行]：工具 update_rules 在黑名单里
[实测] rules.txt 未被改动（治理生效，工具没执行）
       说明 │ 被否决的结果作为一条 tool 消息进了上下文（1 条，占位以"[被规则拦截，未执行]"开头），
             模型知道这不是工具的真实输出；裁决的事实就在占位文本里，下一章它们随 history 落进轨迹。
```

这一轮模型被拦了一次，转头去查了规则库、看到现有规则后放弃修改，改口告诉
用户去找管理员。占位必须是**自描述**的：模型拿到它要知道这不是工具的真实
输出（否则会编造结果）。这和 stage03 里"中断占位要自描述"是同一条原则。

> 治理只发生在工具执行前的那一个点上（agent 手里的 governor），也是刻意只开
> 一个；总线从此和治理无关，只管事件到达消费者。每多一个准入点就多一处
> "agent 和规则之间的隐式契约"，多了就管不住。真需要更多（消息出 agent
> 之前、工具结果回填之前），照这个形状加，但每次都要问一句：能不能改成观测？


## 跑一下（真实 LLM 实测输出）

终端实录（`.cast` → gif，同目录有 `.mp4` 和 `index.json`）。上行五段是一条线上的叙事，
case 是**累积**的，名字是 `两位编号-语义名`（编号让文件名字典序 = 演示顺序）；第 6 段
是下行的独立演示，不打模型，单独跑：

```
stage04-demo --list                       # 六段及说明
stage04-demo 02-burst-load                # 只跑到第 2 段（累积）
stage04-demo 06-inbox-priority            # 只跑下行的优先级（独立，不打模型）
```

### 01-sync-vs-async：同步扇出 vs 异步分发（不打模型）

![01-sync-vs-async](../../src/baby_event_driven_agent/rec/stage04/docs/01-sync-vs-async.gif)

同一个慢订阅者（每事件睡 5ms），两边各发 200 个事件。同步扇出的写法：loop 等订阅者等了
1.1s 左右，`emit` 的耗时就是订阅者的耗时。本章的写法：`emit` 只花 0.002s 就返回（队列压着
199 条，drain 时再等），而且**消费和发送是并发的**——生产者每让出一次，worker 就消化一批，
不必等全部发完。两套发法订阅者都收到了 200 条：异步分发没有少送，只是把"等"从 loop 挪到了
lane worker。

### 02-burst-load：洪峰压测（不打模型）

![02-burst-load](../../src/baby_event_driven_agent/rec/stage04/docs/02-burst-load.gif)

直接往总线灌 2000 个 token 增量（stream 道，队列 64，满了丢最新），中间夹一条 `turn_end`
（state 道，不可丢）。看两条道各自的账，以及洪峰过程中每 200 条采样一次的 `[道]` 行：
stream 道顶到 64 满、丢弃数往上爬，state 道**始终为 0**——这就是"可丢的丢、
不可丢的背压"要买的东西。
后半段还有一组对照：换成一个快的消费者，增量一条没丢、上屏帧数被合并缓冲砍掉一个数量级；
把让出点去掉，生产者一个调度片就灌完，队列 64 条之外全会丢——**"不丢"的前提是生产者让出，
不是消费者快**。

### 03-slow-subscriber：真跑一轮（慢订阅者不拖 loop，UI 合并缓冲）

![03-slow-subscriber](../../src/baby_event_driven_agent/rec/stage04/docs/03-slow-subscriber.gif)

同一个慢订阅者挂在这一轮上，loop 不等它。UI 侧把 token 攒成帧再刷：满 96 字或到 50ms 帧界，
先到先刷（只等满会一顿一顿）。屏幕上的 `[实测]` 两行就是证据：这一轮从投递到 `turn_end`
的总耗时（4.83s，其中同步扇出光等待就要 0.57s），以及"上行的 107 个增量被刷成了 59 帧"。

### 04-permission-veto：当场否决

![04-permission-veto](../../src/baby_event_driven_agent/rec/stage04/docs/04-permission-veto.gif)

挂上 `permission_guard` 把 `update_rules`（改规则库）拉黑，让 agent 去加一条规则：预期
工具调用在执行前被治理否决、工具一条没执行、`rules.txt` 字节未变。被否决的结果作为一条 tool
消息进了上下文（占位以 `[被规则拦截，未执行]` 开头），模型知道这不是工具的真实输出——
这一轮它被拦了一次，转而去查规则库，最后放弃修改，改口告诉用户去找管理员。

### 05-approval-flow：人工确认（等答复的那一半）

![05-approval-flow](../../src/baby_event_driven_agent/rec/stage04/docs/05-approval-flow.gif)

规则只判"这个要不要问人"（当场，微秒级），答案由人来给（之后，可能要几十秒）——这一段的
"人"就是 demo 里那个订阅者。屏幕上一串都在：`approval_required`（带 request_id，agent 正
挂在这次等待上）→ `→ 批准` → `确认结果：allow by user` → 工具真执行（为了让 demo 能反复
跑，写工具落在 `sessions/stage04/` 的副本上）。请求和结果都是事件，什么时候问的、谁批的、
批完工具返回了什么，屏幕上这一串就是全部经过。

### 06-inbox-priority：下行的意图与优先级（不打模型，独立跑）

![06-inbox-priority](../../src/baby_event_driven_agent/rec/stage04/docs/06-inbox-priority.gif)

turn 进行中依次投三类消息：followup（默认，排队）、两条 steering（priority=100、
10，故意乱序）、一条 redirect，中途把 followup 升级为插话（promote）。屏幕上的
次序就是答案：**转向纠正 → 加急插话(10) → 排队的话(100) → 常规插话(100)**——
redirect 旁路第一时刻落地，steering 在 step 边界按优先级拼进本轮，promote 的
那条按自己的 priority 精确入列。意图归用户，priority 管队列内先后，旁路高于
一切队列。

## 验证

- 环境：Python 3.13，openai SDK；模型走仓库根 `.env` 的 OpenAI 兼容端点
  （本地 ollama 的 `qwen3.5:4b-32k`）。
- pytest：`stage04-test` **28 passed**（2026-09-24 真跑）——26 条离线、
  2 条打真模型。
  离线（不打模型，全是传输层和治理的事，跟模型无关）：
  - 慢订阅者（10ms/事件）不拖慢 emit：60 个事件的 emit 耗时远小于同步扇出，
    且 60 条一条没丢；
  - 洪峰：stream 道丢自己的（`dropped > 0`）、state 道一条不丢、`turn_end` 收到；
  - 合并缓冲的对照：800 个增量一条不丢、上屏 ≪800 帧；无让出点的洪峰：快消费者
    也救不了，队列 64 条之外全丢（736）——"不丢"的前提是生产者让出，不是消费者
    快。这两条对照在 demo 2b 段里也有一份直刷对照输出；洪峰过程中每 200 条还会
    采样一次两条道的队列深度与丢弃数（`[道]` 行）：stream 道顶到 64 满、丢弃数
    往上爬，state 道始终为 0——见上面 `02-burst-load` 那段录像；
  - 治理（当场判）：否决 → 工具一次没执行（用探针替掉 `update_inventory`，不碰
    仓库里的知识库文件）、上下文里是自描述占位；
    改写 → 工具按改过的参数执行；
  - 人工确认（等人答）：批准 → 工具真执行，`approval_required` 与
    `approval_decided` 靠 `request_id` 配对、署名是 `user`；
    拒绝 → 不执行 + `[人工确认未通过，未授权执行]` 占位、署名 `user`；
    批准并改写参数 → 按人给的参数执行（裁决是 `modify`）；
    超时 → **按拒绝处理**（`fail-closed`）、署名 `approval_timeout`、占位是
    `[人工确认超时，未授权执行]`；
    等待期间被中断 → 这次调用记 `[等待人工确认期间被中断，未授权执行]`、turn 以
    `interrupted` 收尾，**回执照样有**（`action=abandoned`，`by=user_interrupt`）；
    迟到 / 号不对的答复 → 不认领、不伪造裁决，那条等待继续走到超时；
  - 合并缓冲：不满就等帧界、满了立刻刷、stop 时把尾巴刷掉；
  - 下行的意图与优先级：默认 followup——turn 在飞时发的四条普通消息只排队
    不插话，turn 结束后按优先级作为新轮处理（同级 FIFO）；steering 在 step
    边界按优先级 drain 进当前轮（不新开轮）；turn 空闲时取到 steering 自然
    降级成主输入；followup 与 steering 同飞时互不混（steering 进本轮、
    followup 留到下轮）；打断（stop）优先于队列——turn 按 interrupted 收尾，
    worker 立刻按优先级接上 followup；转向（redirect）的纠正先于插话落地，
    同轮继续；promote 把还在排队的消息精确升级为插话（别的消息不动、顺序
    不乱），已被取走的消息 promote 返回 False；promote 错过 turn 的最后
    边界时不产生顺序反转——取件按 (priority, 到达序) 全局合并取最小。
  真模型：
  - 一轮真对话：`turn_end` 一条没丢；事件数够多（≥30）时断言"慢观测者在
    turn_end 那一刻还没追上"——分发的确没在 loop 里等订阅者。事件太少时滞后
    可能不存在，那就只验一轮有头有尾，不硬凑断言；
  - 回归：换了传输层之后，stage03 的"掐掉在飞的一步、turn 以 interrupted 收尾"
    照旧，且合成消息也走事件出去（订阅者看得到，否则重建不出这段 history）。
  demo 第 5 段是唯一一条"人在回路"的端到端证据（真模型触发、真事件、真执行）；
  写工具落在 `sessions/stage04/inventory-demo.txt` 这份副本上，好让 demo 能反复跑。
- **治理用例为什么用脚本化 LLM**：要确定性地触发一次工具调用（"模型会不会调
  那个工具"不该是这条用例的变量）。其余用例不设替身——本章要验的传输层行为
  跟模型无关，真模型只在"跑通一整轮"那两条上出现。
- 测试不用 pytest 的 tmp_path fixture（沙箱 shim 会拦 `pytest-of-unknown` 的
  mkdir），用 `tempfile.mkdtemp` 自建自清理。

## 降级项：真上生产还要补什么

本章解决的是**单进程内**的传输层。跨进程 / 跨机器这几件事没做，也不该在这一章做：

- **事实层（落盘与回放）**：本章的事件发完即丢，对话的事实靠 agent 写进
  上下文的自描述占位承载（工具为什么没执行、审批谁批的，占位文本里都有）。
  下一章这些 history 落成 append-only 的轨迹——那是唯一的事实层，治理裁决的
  元数据也会进占位所在 entry 的 payload。跨会话的全局审计视图（"所有会话里
  谁什么时候批了什么"）不存在，需要时扫轨迹文件或建索引。
- **多进程 / 多实例部署**：把 Lane 换成消息中间件（Redis Stream / NATS / Kafka）
  只是最浅一层。跟着动的：**投递语义**——进程内"一次必达、deny 不投递"变
  at-least-once，消费者要幂等；**落盘的位置**——持久化挪给 broker，进程内不再
  有事件账；**单写者前提**——"同一 session 只有一个 worker"是有序性的地基，
  多实例要靠会话亲和把一个会话的写钉在一个分区上。真正能平移的是"按可丢性
  分流"这个决策（对应持久化等级不同的两个 topic），不是现在的 emit 链路。
- **跨进程的人工确认**：等待靠进程内的 future，所以答复必须回到**同一个进程**
  （见"改动三"末尾那条边界）。跨进程要么靠轨迹里的 approval 请求记录续读 + 带 `request_id` 的
  答复通道，要么由一个中央审批服务持有"谁在等"这份状态。
- **答复里没有"人"的身份**：`approval_decided` 的署名只有 `by: "user"`，没有
  "哪个用户、用哪个凭证批的"。真上生产这里要接身份（谁批的比批了什么更重要）。
- **观测者没有超时**：`_lane_loop` 串行 await 每个 handler，"慢"有界、"死"无界
  ——一个挂死的观测者会挂住整条 lane worker，而 lane 全局共享，所有 session
  的这条道连坐（拦截者有 `wait_for` + 预算，观测者没有）。要设防得先定义
  "观测者超时算不算已消费"，是新语义，留给需求真出现的那天。
- **inbox 无界**：下行收件箱是优先队列，没有 maxsize——worker 卡住时用户消息
  无限堆积；priority 也没有饥饿老化（低优先级被持续插队时永远轮不到）。
  下行低频，暂可接受；上界、满了的语义与老化机制和"走出单机"一起做。
- **事件 schema 演进**：type 和 payload 的形状会变，回放旧轨迹需要版本号或
  兼容规则——本章没有。

## 下一章

Stage 4 保证了事件**能到达、能治理、能被消费者接住**。Stage 5a（会话与真相）
接着解决另一半：多轮对话的状态怎么管——以及崩溃之后怎么恢复。

- `history` 是内存里的工作态，进程一重启就没了——Stage 5a 把它换成 append-only
  的轨迹树（参考 pi 的 session 设计）：节点带 `parentId`，认父不认子，rewind
  是移指针，喂给模型的上下文是从树投影出来的视图；
- **轨迹是唯一的事实层**。本章发完即丢的事件不另立账本：占位里的裁决事实、
  审批的请求与结局都以 entry 进轨迹（治理裁决的元数据进占位所在 entry 的
  payload，审批请求/回执是独立的 entry）；恢复时扫描轨迹闭合悬空的审批；
- 落盘的工程（append-only、长度前缀 + CRC 判残尾）在 5a 讲清。
  5b 再谈可裁剪、可压缩（压缩是树上的视图标记）。
