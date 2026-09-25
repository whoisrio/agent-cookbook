# Stage 4：消息机制 —— 事件离开 agent 之后要走多远

> 配套代码：`src/baby_event_driven_agent/stages/stage04_message_bus/`，
> `stage04-demo` 跑演示（默认打真实模型：本地 ollama，读仓库根 `.env`；
> `--scripted` 切到离线回放，不打模型），
> `stage04-test` 跑测试（24 条离线 + 2 条真模型，共 26 条）。
> 本章强化 outbound（事件离开 agent 之后怎么到达它的消费者），下行只动一处：
> 收件箱按意图分成两条队列，`publish` 链路与 stage02/03 一字未改——上下行
> 不对称仍是本章的前提，不是要解决的问题。

到目前为止，我们已经给用户的输入消息添加了steering、followup以及interrupt的机制；
当下不同的agent，对用户在agent loop中新增输入的消息的默认处理各有不同，
比如codex/workbuddy 等agent，默认按照followup处理，用户可以主动触发插队，让消息变成steering；
pi agent的默认行为就是steering；
而hermes agent，默认行为就interrupt and redirect；
这一章，我们把咱们baby agent的消息机制按照codex/workbuddy的方式来处理；

另外，在agent的事件消费端，LLM和thinking的stream输出每次有delta内容到来都会发送事件，现在的 flash 模型每秒能吐几百 token，stream 模式下 UI 上的呈现不能还是来一个 token 就刷一次；这一类事件的消费，需要设置接收缓冲区，到达一定数量之后，统一刷新一次；

第三，我们给工具添加上审批消息的机制，

## agent运行时，让用户控制新增输入消息的行为
先把消息处理的控制权还给用户；
那么在agent正在执行过程中的消息，默认都放在等待队列中，也就是followup；
如果用户希望插话，那么就通过插入的方式让agent在下一个step与LLM交互之前，把用户指定的消息消费掉；
如果用户希望立刻打断当前对话，那么通过主动interrupt当前的step，让agent立刻处理新的消息；

所以，我们把agent的消息inbox，分为了steering inbox和followup inbox，用户的消息默认进入followup inbox；
```python
class Agent:
    def __init__(
        self,
        ...
    ) -> None:
        ...
        self._followups: dict[str, list[tuple[int, int, Event]]] = {}
        self._steerings: dict[str, list[tuple[int, int, Event]]] = {}
```
用户希望下一个step插话时，通知执行插入操作，消息就进入steering inbox；
用户希望新的消息立刻被消费，通过打断的操作，agent中断当前任务的执行，会立刻消费followup inbox里最新的一条消息；

前几章，我们用asyncio.queue来承载用户输入消息，asyncio.queue方便处理FIFO，但是对插队处理反而不方便，所以我们把两个inbox改成用list来承载；
用户的输入消息默认投递到followup inbox，针对steering消息，除了在每一次与llm交互的step中从steering box读取外，
在每一次turn开始时，同样也需要优先处理steering inbox未被处理掉的消息，避免steering触发时loop即将走到边界导致steering消息未被正确提取。

## 上行的消费约束：不同的事件，由不同形状的 handler 处理

不同类型的事件，消费方式可能是不同的。比如 UI 消费 LLM stream 输出的增量事件：
不能来一个 delta 就刷一遍屏，那样很容易把 UI 刷爆——增量要先攒进缓冲区，攒够了
按帧刷。也就是说，这类事件需要的是"投递即返回、订阅者自己排干"的邮箱型消费者
（offer），而不是逐条 `await` 的 handler。什么类型的事件允许什么样的消费者，
声明在事件类自己身上——**我能被谁消费，问我**：
所以，我们扩展了`StreamEvent`类型作为`Event`的子类，并且定义了它的消费者类型是`MailBox`

```python
@runtime_checkable
class Mailbox(Protocol):
    """邮箱型消费者的结构：offer 把事件放进消费者自己的缓冲，永不阻塞。

    结构约束而非基类——任何提供 offer 的对象都是邮箱型；总线分派时只认这个结构。
    """

    def offer(self, event: Event) -> None: ...


class StreamEvent(Event):
    """stream 事件（高频、逐 token）：只允许邮箱型消费者订阅。"""

    HANDLER_SHAPE = Mailbox  # 基类是 object（无约束），子类改写
```

总线只执行一条对所有事件类型都相同的通用规则：订阅进门时，对声明的每个事件
类型问一遍消费约束

```python
def require_consumable(event_type: str, handler: object) -> None:
    """事件类型自己的消费约束：我能被谁消费，问我（的类），不查别处。"""
    shape = event_class(event_type).HANDLER_SHAPE
    if shape is not object and not isinstance(handler, shape):
        raise ValueError(f"事件 {event_type} 只允许 {shape.__name__} 型消费者")
```

分派时按 handler 的形状走两条路：

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

### 消费者侧：合并缓冲

stream 事件的消费者，通过`CoalescingBuffer`接收agent吐出的stream delta数据，达到缓冲区阈值或者delta结束，再flush到UI上。

```python
class CoalescingBuffer:
    """把一串增量合并成一帧：add 只做缓冲追加，满格 / 帧界触发刷屏。"""

    def add(self, text: str) -> None:
        self._buf.append(text)
        self._size += len(text)
        self.merged += 1
        if self._size >= self._max_chars:
            self.flush()          # 满了立刻刷

    # start() 另起一个 ticker task，每 frame 秒（50ms）flush 一次——帧界也刷

class StreamConsumer(CoalescingBuffer):
    """stream 事件消费者基类：offer 只做缓冲追加（微秒级），
    满格 / 帧界触发 on_flush——子类只实现慢侧钩子（刷新 UI）。"""

    def offer(self, event: Event) -> None:
        self.add(str(event.payload.get("text", "")))
```

## 工具审批消息确认

工具是否要审批，首先要支持在工具中定义，于是我们先把之前纯json schema定义的工具，改写成如下形式，
通过 `requires_approval`来定义工具是否需要审批，
```python
@dataclass(frozen=True)
class Tool:
    """一个工具的完整定义：给模型看的 schema、真实现、要不要人工确认。"""
    schema: dict[str, Any]           # function schema：name / description / parameters
    fn: ToolFn
    requires_approval: bool = False  # 执行前要不要问人
    approval_reason: str = ""        # 问人时给用户看的理由
```
我们的 `update_inventory`工具的定义就改造成如下形式，

```python
"update_inventory": Tool(
        schema=_fn_schema(
            "update_inventory",
            "添加新品类，或更新某品类的库存数量与规格",
            {
                "category": {"type": "string", "description": "品类名"},
                "stock": {"type": "integer", "description": "库存件数"},
                "spec": {"type": "string", "description": "规格描述，可省略"},
            },
            ["category", "stock"],
        ),
        fn=update_inventory,
        requires_approval=True,  # 真写业务数据：执行前问人
        approval_reason="该工具会修改库存数据，需要人工确认",
    )
```
LLM 发起工具调用时，检查到工具定义要求审批，就发出 `approval_required`事件（带 request_id、工具名、参数、理由，
   谁扮演"人"谁就订阅它），然后挂在 future 上等答复，此时loop被挂起
```python
tool = self.tools.get(name)
if tool is not None and tool.requires_approval:    # 工具自己声明要问人
    answer = await self._request_approval(sid, name, call["id"], args_text,
                                          tool.approval_reason)
    if answer is None:                             # 等待期间被中断：没批也没拒
        return NO_EXEC, False, True
    if answer.action == DENY:
        return f"{APPROVAL_REJECTED}：{answer.reason}", True, False
    if answer.patch:                               # 人的改写最后生效
        args_text = str(answer.patch.get("arguments", args_text))

result = await tool.fn(json.loads(args_text))
```

直到`user_approval`送到agent是，在 `enqueue` 里直接处理，不会把消息放到followup或者steering队列：

```python
def enqueue(self, event: Event) -> None:
    if event.type == "user_interrupt":
        self.on_interrupt(event)   # 旁路：打断
        return
    if event.type == "user_approval":
        self.on_approval(event)    # 旁路：审批答复，立即处理
        return
    ...                            # 其余消息按意图进 followup / steering inbox
```

## 跑一下（实测输出）

02~05 的回答由真实模型生成（本地 ollama，`Qwen3.5-4B-GGUF`，配置读仓库根
`.env`）；01 只测总线，不打模型。case 可单独跑：

```
stage04-demo                              # 跑全部（默认打真模型）
stage04-demo 02-promote-to-steering       # 只跑一个 case
stage04-demo --scripted                   # 离线回放：不打模型，确定性重跑
stage04-demo --list                       # 列出所有 case
```

> 02~05 都以"模型真的发起一次写工具调用"为前提，而小模型不保证每次都调工具
> （4B 模型有时把工具调用写成文本、或直接反问）。demo 不做兜底：`open_tool_window()`
> 只等一次，拿不到"工具在飞"的窗口就打印说明并结束这一段。要确定性，加 `--scripted`。



### 01-followup-default：turn 在飞时的新消息默认 followup

agent 正在执行写工具时用户再发一条消息：只排队，当前 turn 完整跑完之后，
才作为新 turn 的主输入。实测次序：

```text
[用户] 把保温杯库存改成 3 件
[用户] → 开始处理：把保温杯库存改成 3 件
[思考] 用户要求将保温杯库存改成 3 件，这需要使用 update_inventory 工具…（reasoning 按帧合并输出）
[LLM(要求执行工具)] → update_inventory（{"category":"保温杯","stock":3}）
[用户] 顺便查一下规则（turn 在飞 → followup，排队）
[执行工具] ← update_inventory 结果：已更新：保温杯库存 3 件（demo 副本）
[思考] 用户让我把保温杯库存改成 3 件…（略）
[回答] 已更新保温杯库存为 3 件。
[系统] turn 结束（reason=turn end）
[用户] → 开始处理：顺便查一下规则          ← turn 1 结束后才轮到它
[LLM(要求执行工具)] → search_rules（{"query":"规则 制度"}）
[执行工具] ← search_rules 结果：（无命中）
[回答] 本次搜索未命中任何团队规则或制度…
[系统] turn 结束（reason=turn end）
```

`[思考]` / `[回答]` 两路走的是和 01 同一个 `StreamConsumer`——delta 逐条 offer 进
缓冲，帧界才刷到屏幕上，刷屏节奏不跟着 token 走。

合并缓冲有一个副作用值得单独说：**屏幕次序会落后于事件次序**。最后一帧没攒够
96 字、也没到帧界时，`turn_end` 已经发出去了——屏幕上就会看到"回答被 turn 结束
切成两半"，或尾巴滞后到后面的说明行里。demo 因此在两个时点主动 flush：

- assistant 消息成形（`agent_reply`，要打印它发起的工具调用之前）；
- turn 结束（`turn_end`，打印 turn 边界之前）。

这是呈现层的收口，不是机制：事件流本身的次序从头到尾没有变。

### 02-promote-to-steering：把排队的消息升级成插话

消息先按 followup 排队；用户点名"这条别等了"——`promote` 把它精确移动到
steering 队列，下一个 step 边界拼进当前轮，turn 不断。实测：

```text
[用户] 把保温杯库存改成 3 件
[用户] → 开始处理：把保温杯库存改成 3 件
[思考] …（略）
[LLM(要求执行工具)] → update_inventory（{"category":"保温杯","stock":3}）
[用户] 顺便查一下规则（先按 followup 排队）
[用户] → 等不了了，插话！（promote）
[执行工具] ← update_inventory 结果：已更新：保温杯库存 3 件（demo 副本）
[系统] 插话拼进本轮：顺便查一下规则        ← step 边界 drain
[思考] 用户想要查询规则…（略）
[LLM(要求执行工具)] → search_rules（{"query":"规则"}）
[执行工具] ← search_rules 结果：（无命中）
[回答] 没有找到相关规则内容。
[系统] turn 结束（reason=turn end）
```


### 03-interrupt-redirect：打断让纠正先落地

排队中的消息不动，用户通过打断（`user_interrupt` 旁路）直接发来纠正文本：
本轮在 step 边界转向，纠正第一时刻落进上下文；排队的消息等下一轮。实测：

```text
[用户] 把保温杯库存改成 3 件
[LLM(要求执行工具)] → update_inventory（{"category":"保温杯","stock":3}）
[用户] 顺便查一下规则（followup，排队）
[用户] → 等不了：打断！先别改库存了，去订会议室（redirect）
[执行工具] ← update_inventory 结果：已更新：保温杯库存 3 件（demo 副本）
[用户] → 开始处理：先别改库存了，去订会议室   ← 纠正先于一切队列落地
[思考] 用户说要"订会议室"，先检索会议室预订规则…（略）
[LLM(要求执行工具)] → search_rules（{"query":"会议室预订"}）
[执行工具] ← search_rules 结果：会议室预订：找行政小王，订前先看日历有没有被锁。
[回答] 会议室预订规则：找行政小王，订前先看日历有没有被锁…
[系统] turn 结束（reason=turn end）
[用户] → 开始处理：顺便查一下规则            ← 排队的消息不受影响
[系统] turn 结束（reason=turn end）
```

这一轮模型先查了一次库存再写（`query_inventory` → `update_inventory`），
打断落在第二个工具之后——纠正文本照样作为 user 消息挤进本轮，模型随即转向
去查会议室流程。

### 04-ui-coalescing-buffer：UI 缓冲消费

往总线灌 200 个 `agent_delta`，UI 是 `StreamConsumer`：offer 只做缓冲追加，
满 96 字立刻刷，到 50ms 帧界也刷。实测：

```text
[UI] 刷一帧（96 字）
[UI] 刷一帧（96 字）
[实测] 200 个 delta 全部送达（merged=200），emit 总耗时 0.000s——emit 返回时事件只是进了缓冲
[UI] 刷一帧（8 字）
[实测] 200 次渲染合并成 3 帧——刷屏节奏归消费者，不归总线
```

### 05-approval-flow：工具审批（要等人的那一半）

工具自己声明 `requires_approval`：agent 发 `approval_required`（带
request_id），答复由人给——用例里用订阅者模拟人点了一下批准；答复走旁路
直接 resolve，工具拿到授权才执行（写工具落在 sessions 副本上）。实测：

```text
[用户] 把保温杯库存改成 45 件
[LLM(要求执行工具)] → update_inventory（{"category":"保温杯","stock":45}）
[系统] ？ update_inventory 要执行：{"category":"保温杯","stock":45}（request_id=ap-6a84fd4c，超时 10s）
[用户] → 批准
[系统] 确认结果：allow by user
[执行工具] ← update_inventory 结果：已更新：保温杯：库存 45 件。
[思考] …（略）
[回答] 已将保温杯库存更新为 45 件。
[系统] turn 结束（reason=turn end）
[实测] 工具真执行了，副本上现在是：保温杯：库存 45 件。
```

等不到答复（超时）时按拒绝处理（fail-closed），不会是"没人管就放行"。


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
