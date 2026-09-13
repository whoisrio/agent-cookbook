# Stage 1：只是接收事件

> 配套代码：`src/baby_event_driven_agent/stages/stage01_receive_events/`，
> 可跑（`stage01-demo` / `stage01-test`）、带 tests。
> 这是整个系列的地基：一个真实可用的 agent，v0.1——真模型、流式输出。
> 后面六章不加新剧情，只回应一个接一个的真实需求。

## 我们要什么

### 什么是事件驱动的 agent

一个最朴素的 Agent Loop ：拼上下文、调模型、执行工具,没有工具可执行了，就返回结果。

```python
def agentloop(self,input):

    while True:
        # 1. 拼上下文
        # 2. 调模型
        # 3. 执行工具
        # 4. 返回结果
```
但现实很快会变复杂，输入的防护、上下文的压缩、输出的隐私检查、工具调用审计、session log落盘等等等……每加一个能力，都得打开 Agent Loop 动刀。
```python
def agentloop(self,input):
    #防prompt注入
    while True:
        # context compact
        # 1. 拼上下文
        # log

        # 2. 调模型

        #审计
        # 3. 执行工具
        #输入隐私检查

        # 4. 返回结果
```
loop 里塞的杂事越多，核心逻辑就越被淹没。最终它会长成一个谁都不敢碰的上帝函数。任何一处改动，都可能牵连核心的模型调用链路。

事件驱动把 Agent Loop 从杂事堆里解耦出来：过程中发生的每件事——用户输入、模型增量、工具往返、turn 的生命周期——统一建模为 Event。Agent Loop 只管跑核心步骤，每走一步把"发生了什么"发布到总线上就算完事。而事件天然可排队、可落盘、可回放，Agent 的思考轨迹也因此沉淀为可复用的资产。

说白了，事件驱动让 Agent Loop 回归它该做的事——跑核心循环。剩下的，交给事件总线。

下面，咱们就着手一步步搭建事件驱动的agent。
## 场景

咱们从一个简单的电商的业务场景的 agent开始
- 基础模型就用本地小模型(qwen3.5:4b-32k)，通过ollama来驱动，ollama支持OpenAI chat/completions兼容端点、流式输出。
- 提供四个工具给这个agent，读写成对：查库存 / 改库存（扮演业务接口），查规则 / 改规则（扮演知识检索与运营）。
- UI 暂时是 CLI，采用流式：回答一个字一个字往外蹦。

## 关键对象
五个关键对象：
### Event
事件，type + session_id + payload + ts 四个字段，设置为frozen,事件发出去之后就不允许再被改动；

```python
import json, time
from dataclasses import dataclass, field

T0 = time.time()
def t(): return time.time() - T0  # 进程内相对时间戳，demo 输出用

@dataclass(frozen=True)
class Event:
    type: str            # user_input / agent_delta / agent_thinking / agent_reply / turn_end
    session_id: str
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
```
### EventBus
`EventBus`：消息总线，提供消息的订阅和发布；在stage1，publish消息后，await所有handler执行；

```python
class EventBus:
    """同步总线：publish 原地 await handler。"""
    def __init__(self):
        self._subs = {}
    def subscribe(self, type, handler):
        self._subs.setdefault(type, []).append(handler)
    async def publish(self, event):
        for h in self._subs.get(event.type, []):
            await h(event)
```

### Agent
user_input 的消费者，收到事件跑一轮 loop（模型 → 工具 → 模型 → 回话），按 session 维护 history。Agent 自身很薄，持有的状态只有一份按 session 隔离的 history：

```python
class Agent:
    def __init__(
        self,
        bus: EventBus,
        log: SessionLog,
        llm: LLMClient,
    ) -> None:
        self.bus = bus
        self.log = log
        self.llm = llm
        self.history: dict[str, list[dict[str, Any]]] = {}  # session_id -> messages
```

#### 模型客户端

使用`AsyncOpenai`提供的client调用llm，并指定流式输出；
```python
class RealLLM:
    """OpenAI 兼容流式客户端，stream_chat 产出归一化增量块。"""

    def __init__(self):
        api_key = _cfg("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("缺少 OPENAI_API_KEY：写在仓库根 .env 或环境变量里")
        self.model = _cfg("OPENAI_MODEL")
        self._client = AsyncOpenAI(
            api_key=api_key, base_url=_cfg("OPENAI_API_BASE") or None, timeout=120.0
        )

    async def stream_chat(self, messages):
        stream = await self._client.chat.completions.create(
            model=self.model, messages=messages, tools=TOOL_SCHEMAS, stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            # 思考内容单独成一路，不跳过：云上走 reasoning_content，
            # 本地 ollama 走 reasoning，归一化成 reasoning_delta
            extra = delta.model_extra or {}
            thinking = extra.get("reasoning_content") or extra.get("reasoning")
            if thinking:
                yield {"type": "reasoning_delta", "text": thinking}
            if delta.content:
                yield {"type": "text_delta", "text": delta.content}
            for tc in delta.tool_calls or []:
                fn = tc.function
                yield {"type": "tool_call_delta", "index": tc.index,
                       "id": tc.id or None, "name": fn.name if fn else None,
                       "args_delta": (fn.arguments if fn else "") or ""}
```

#### 工具

在这个简单的Agent场景里，我们提供四个工具。两个读操作工具，
`query_inventory` 查询指定品类的库存；
`search_rules` 查询业务知识，扮演 RAG 检索，

```python
{
    "type": "function",
    "function": {
        "name": "query_inventory",
        "description": "查询品类库存与规格（业务数据）。品类名如：保温杯、玻璃杯",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "品类名"}
            },
            "required": ["category"],
        },
    },
},
{
    "type": "function",
    "function": {
        "name": "search_rules",
        "description": "检索团队规则、流程、制度（如会议室预订、VPN 申请、报销）",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索词，多个关键词用空格分隔",
                }
            },
            "required": ["query"],
        },
    },
}
```

两个写操作工具，
`update_inventory`，更新指定品类的库存；
`update_rules`,更新知识库；

```python
{
    "type": "function",
    "function": {
        "name": "update_inventory",
        "description": "添加新品类，或更新某品类的库存数量与规格",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "品类名"},
                "stock": {"type": "integer", "description": "库存件数"},
                "spec": {"type": "string", "description": "规格描述，可省略"},
            },
            "required": ["category", "stock"],
        },
    },
},
{
    "type": "function",
    "function": {
        "name": "update_rules",
        "description": "添加一条新规则，或按标题更新已有规则的内容",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "规则标题，如：报销"},
                "content": {"type": "string", "description": "规则内容"},
            },
            "required": ["title", "content"],
        },
    },
},

```
工具的 description 写得越清楚，模型选错工具的概率越低。

#### System prompt
把工具的description拼装到system prompt中，在生产的agent里，还会将skills，mcp，以及AGENTs.md，SOUL.md等内容拼装到system prompt；
在CLAUDE等Agent里，还会在system prompt里进一步把不易变(公共的system prompt、tools)和相对来说可能会变(skills，AGENTs.md等等)再区分动静态区域来管理，以便更好的控制prompt cache的命中；
不过在咱们的例子里，先不处理这么多。
```python
def build_system_prompt(schemas: list[dict[str, Any]] | None = None) -> str:
    """system prompt 从 tool schemas 生成：工具的分工只写在 description 一处。"""
    schemas = schemas if schemas is not None else TOOL_SCHEMAS
    lines = ["你是智能助手，必须基于事实来回答用户的提问，**严禁编造**，得不到事实，就回答不知道。可用工具："]
    for s in schemas:
        fn = s["function"]
        params = "、".join(fn["parameters"].get("properties", {}))
        lines.append(f"- {fn['name']}：{fn['description']}" + (f"（参数：{params}）" if params else ""))
    lines.append(
        "必须基于事实回答用户问题。用户的问题或请求涉及上面某个工具时，"
        "选对工具、先拿到真实结果再回答；获取不到准确信息就回答不知道，严禁编造。"
        "用户要求记录或修改时，用对应的写工具落库，然后一句话确认改了什么。"
    )
    return "\n".join(lines)
```

生成出来的 prompt 长这样：

```text
你是智能助手，必须基于事实来回答用户的提问，**严禁编造**，得不到事实，就回答不知道。可用工具：
- query_inventory：查询品类库存与规格（业务数据）。品类名如：保温杯、玻璃杯（参数：category）
- update_inventory：添加新品类，或更新某品类的库存数量与规格（参数：category、stock、spec）
- search_rules：检索团队规则、流程、制度（如会议室预订、VPN 申请、报销）（参数：query）
- update_rules：添加一条新规则，或按标题更新已有规则的内容（参数：title、content）
必须基于事实回答用户问题。……
```

#### AgentLoop

Agent 里，核心就两个方法：`_run_turn` 跑一轮 loop，`_step` 消费一轮流式输出。

history 每个 session 一份消息序列，第一轮开始时垫一条 system prompt。loop 本身很朴素：最多 4 步，每步向模型要一次流式输出；模型要工具就执行、把结果喂回去再要一次；模型不要工具了，turn 就结束：

```python
async def _run_turn(self, event: Event) -> None:
    sid = event.session_id
    history = self.history.setdefault(sid, [])
    if not history:
        history.append({"role": "system", "content": SYSTEM_PROMPT})
    history.append({"role": "user", "content": event.payload["text"]})
    self.log.append(event, note="turn start")
    for _ in range(MAX_STEPS):                 # 上限 4 步
        msg = await self._step(sid, history)   # 消费一轮流式输出
        history.append(msg)
        await self.bus.publish(Event("agent_reply", sid, {"message": msg}))
        self.log.append(
            Event("agent_reply", sid, {"message": msg}),
            note="tool_call" if msg.get("tool_calls") else "final",
        )
        if "tool_calls" not in msg:            # 模型不再要工具，turn 结束
            self.log.append(Event("turn_end", sid, {}), note="turn end")
            return
        for call in msg["tool_calls"]:
            name = call["function"]["name"]
            if name not in TOOLS:
                result = f"未知工具：{name}"
            else:
                result = await TOOLS[name](
                    json.loads(call["function"]["arguments"])
                )
            history.append({"role": "tool",
                            "tool_call_id": call["id"], "content": result})
            self.log.append(                   # 工具真跑过了，事实要留轨迹
                Event("tool_result", sid,
                      {"tool_call_id": call["id"],
                       "name": name, "result": result}),
                note="tool result",
            )
    self.log.append(Event("turn_end", sid, {}), note="max steps")
```

`_step` 是增量消费的地方。模型一次请求吐出三种增量，各有各的去处：

```python
async for chunk in self.llm.stream_chat(history):
    if chunk["type"] == "reasoning_delta":
        # 思考：边到边发 UI 直播，不进 history、不进 log
        await self.bus.publish(
            Event("agent_thinking", sid, {"text": chunk["text"]})
        )
    elif chunk["type"] == "text_delta":
        text_parts.append(chunk["text"])
        await self.bus.publish(
            Event("agent_delta", sid, {"text": chunk["text"]})
        )
    elif chunk["type"] == "tool_call_delta":
        tc = tool_calls.setdefault(
            chunk["index"], {"id": "", "name": "", "args": ""}
        )  # id / name 就地更新，args 累加——arguments 常分多块到达
```

流结束时三种增量各归各位：文本拼成完整 content，工具调用拼成完整的
tool_calls，组装成一条 assistant 消息返回，`_run_turn` 拿到它发布 agent_reply。

注意思考内容的待遇：只直播，不进 history 也不进 log。它是模型这一步的
推理窗口，provider 下一轮也不会回收 reasoning，轨迹重放用不到它，
log 里自然没有它的位置。

### SessionLog：append-only trajectory 记录

我们把事件、与 LLM 交互的历史，通通 append-only 写入 jsonl 作为 trajectory 记录，以便后续基于轨迹做分析。
轨迹不只是对话：assistant 的每次工具调用和工具的返回也在里面——
它们是模型上下文的一部分，缺了它们，"模型为什么这么答"就无从分析。

```python
class SessionLog:
    """append-only。暂时没人读它，但它记录的是唯一真相。"""
    def __init__(self, path):
        self._path = path
    def append(self, event, **extra):
        rec = {"ts": round(event.ts - T0, 2), "type": event.type,
               "session": event.session_id, "payload": event.payload}
        rec.update(extra)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
```

### CLI UI：呈现 Agent 的输出

CLI UI 作为 LLM 输出事件的消费者，用于呈现 Agent 的输出。
UI 订阅三个事件：`agent_thinking` 收到就用暗色打印（终端里用 ANSI dim，
一眼和正文区分开），`agent_delta` 收到就原地打印（不换行、立刻 flush，
这就是流式呈现的全部），`agent_reply` 收到打一行收尾。
思考与正文是两路流，UI 在两路切换时先换行，避免混排在一起。

```python
async def main():
    # session log 落在包级 sessions/ 目录，按 stage 分目录
    sessions_dir = Path(__file__).resolve().parents[2] / "sessions" / "stage01"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    bus = EventBus()
    log = SessionLog(str(sessions_dir / "session.jsonl"))
    agent = Agent(bus, log, RealLLM())
    bus.subscribe("user_input", agent.on_user_input)

    # 上一条流式增量属于哪路：思考/正文切换时先换行，两类内容不混排
    last_kind = [""]

    async def ui_thinking(e):
        if last_kind[0] != "thinking":
            print(flush=True)
            last_kind[0] = "thinking"
        print(f"\033[2m{e.payload['text']}\033[0m", end="", flush=True)  # 暗色

    async def ui_delta(e):
        if last_kind[0] != "text":
            print(flush=True)
            last_kind[0] = "text"
        print(e.payload["text"], end="", flush=True)

    async def ui_reply(e):
        ...                  # 打一行收尾：工具调用 / 回答完毕

    bus.subscribe("agent_thinking", ui_thinking)
    bus.subscribe("agent_delta", ui_delta)
    bus.subscribe("agent_reply", ui_reply)

    for q in ["保温杯还有库存吗",
              "帮我把马克杯加进库存：8 件，陶瓷，350ml",
              "马克杯还有货吗",
              "报销有什么规定",
              "迪丽热巴和杨幂谁更好看?"]:
        print(f"[{t():5.2f}s] (A) 用户输入：{q}")
        await bus.publish(Event("user_input", "A", {"text": q}))

asyncio.run(main())
```

五个对象凑齐了，回头看一次全景——一次 turn 的数据流长这样：

```text
用户敲一行字
  └─▶ user_input ──▶ Agent 消费，开始 loop（最多 4 步）
        每一步：请求 LLM 流式输出
        ├─▶ agent_thinking ──▶ UI 暗色直播（不进 history，不进 log）
        ├─▶ agent_delta    ──▶ UI 流式打印（不进 log，累积结果才算完整回答）
        ├─▶ agent_reply    ──▶ UI 收尾 + 写 log（tool_call 或 final）
        ├─▶ tool_result    ──▶ 写 log（工具真跑过了，事实要留轨迹）
        └─▶ turn_end       ──▶ 写 log，一轮结束
```


## 跑一下（真实 LLM 实测输出）

模型是本地 ollama 的 qwen3.5:4b-32k（OpenAI 兼容端点），零 API 成本。
时间戳从进程启动算起（前 4 秒多是解释器、SDK 导入和模型冷启动）。
五轮对话，把读、写、读回、查规则、闲聊拒绝各走一遍。

输出里 `⋯` 开头的段落是思考内容（终端里是暗色的 `agent_thinking`，
此处去掉色码、用 `⋯` 标出），其余正文是 `agent_delta`：

```text
[ 4.21s] (A) 用户输入：保温杯还有库存吗

⋯ 用户询问保温杯是否有库存，我需要使用 query_inventory 工具来查询保温杯的库存信息。参数 category 应该设置为"保温杯"。
[ 7.10s] (A) → 工具调用：query_inventory({"category":"保温杯"})
⋯ 用户询问保温杯是否有库存，我已经通过query_inventory工具获取到了相关数据：保温杯库存为42件，规格是316L不锈钢内胆、500ml、杯身磨砂黑。现在我可以直接回答用户的问题。
有库存，保温杯目前还有42件，规格为316L不锈钢内胆、500ml、杯身磨砂黑。
[ 9.89s] (A) —— 回答完毕

[ 9.90s] (A) 用户输入：帮我把马克杯加进库存：8 件，陶瓷，350ml

⋯ 用户要求将马克杯添加到库存中，我需要调用update_inventory工具来执行这个操作。参数需要包括：
⋯ - category: "马克杯"
⋯ - stock: 8
⋯ - spec: "陶瓷, 350ml"

⋯ 让我调用这个工具。
[13.53s] (A) → 工具调用：update_inventory({"category":"马克杯","stock":8,"spec":"陶瓷，350ml"})
⋯ 用户要求将马克杯加入库存：8件，陶瓷，350ml。我已经调用了update_inventory工具并得到了成功返回。现在我需要一句话确认改动了什么。
已将马克杯更新为8件，规格为陶瓷、350ml。
[15.57s] (A) —— 回答完毕

[15.58s] (A) 用户输入：马克杯还有货吗

⋯ 用户问马克杯还有没有货，我需要查询马克杯的当前库存情况。
[17.37s] (A) → 工具调用：query_inventory({"category":"马克杯"})
⋯ 用户问马克杯还有货吗，根据上一步查询结果，马克杯有库存8件，规格为陶瓷、350ml。可以直接回答。
马克杯还有货，目前库存为8件，规格为陶瓷、350ml。
[19.78s] (A) —— 回答完毕

[19.80s] (A) 用户输入：报销有什么规定

⋯ 用户询问报销有什么规定，我需要使用search_rules工具来检索关于报销的相关规则。
[21.49s] (A) → 工具调用：search_rules({"query":"报销"})
⋯ 用户问报销有什么规定，我刚才通过search_rules工具查到了"报销：月底 25 号前提交，超过 500 元要附发票原件。"这个信息，可以直接回复给用户。
根据规定，报销需满足以下要求：月底25号前提交，超过500元需要附发票原件。
[24.41s] (A) —— 回答完毕

[24.43s] (A) 用户输入：迪丽热巴和杨幂谁更好看?

⋯ 这是一个主观审美评价的问题，不是业务相关请求。我作为智能助手，应该回答不知道或者给出客观的中性回答，不涉及个人审美偏好。我需要避免对人物进行可能涉及外貌评判的主观描述。

⋯ 不过这个提问与我的工具功能无关——既不需要查询库存、查询规则，也不需要修改数据或规则。根据指导原则中"必须基于事实来回答用户的提问，严禁编造"的要求，对于主观性问题我无法基于事实给出明确答案，应该回答不知道或直接说明无法评价。

⋯ 考虑到这是一个可能涉及公众人物的评论问题，作为AI助手，我应该保持中立客观的态度。
抱歉，我没法对人物进行外貌评价。作为一个AI助手，我的功能主要是处理工作相关的查询和任务，比如库存、规则等方面的问题。如果您有其他关于工作流程的事情需要帮忙，请随时告诉我。
[29.76s] (A) —— 回答完毕

[29.77s] demo 结束
```

值得看四个细节：

- 前四轮各选对了工具：查库存、写库存、再查库存、查规则，模型是靠  工具描述和 system prompt 自己路由的，代码里没有任何 if-else 指路。
- 第 5 轮闲聊，模型一个工具都没调，思考里明确"与我的工具功能无关"，并援引了 system prompt 里"必须基于事实、严禁编造"的要求——
  `build_system_prompt` 生成的那句收尾，在真实模型上是真的起作用的。说明qwen3.5-4b虽然是个本地小模型，在处理简单的任务下，依然能够遵从用户的指令。
当然，生产级别的Agent，杜绝模型胡编乱造，不能简单的只靠Systemprompt的约束，后续咱们再一步一步优化。

再看看，session log 里五轮 turn 有头有尾。
```json
{"ts": 4.21, "type": "user_input", "session": "A", "payload": {"text": "保温杯还有库存吗"}, "note": "turn start"}
{"ts": 7.1, "type": "agent_reply", "session": "A", "payload": {"message": {"role": "assistant", "content": null, "tool_calls": [{"id": "call_wocv9h3e", "type": "function", "function": {"name": "query_inventory", "arguments": "{\"category\":\"保温杯\"}"}}]}}, "note": "tool_call"}
{"ts": 7.19, "type": "tool_result", "session": "A", "payload": {"tool_call_id": "call_wocv9h3e", "name": "query_inventory", "result": "保温杯：库存 42 件；316L 不锈钢内胆，500ml，杯身磨砂黑。"}, "note": "tool result"}
...
{"ts": 24.43, "type": "user_input", "session": "A", "payload": {"text": "迪丽热巴和杨幂谁更好看?"}, "note": "turn start"}
{"ts": 29.76, "type": "agent_reply", "session": "A", "payload": {"message": {"role": "assistant", "content": "抱歉，我没法对人物进行外貌评价。作为一个AI助手，我的功能主要是处理工作相关的查询和任务，比如库存、规则等方面的问题。如果您有其他关于工作流程的事情需要帮忙，请随时告诉我。"}}, "note": "final"}
{"ts": 29.77, "type": "turn_end", "session": "A", "payload": {}, "note": "turn end"}
```

v0.1 在它的能力范围内是个真实可用的 agent。
append-only 这个决定从这一章开始生效：事件只追加、不改写。
到 Stage 6，回放重建状态靠它；到 Stage 7，可重复的 eval 也靠它。

## 设计边界，以及下一章的需求

如上就是最简单的通过事件驱动的agent loop，其实还存在不少明显的问题，你看出来了几个。

比如，**消息插入**和**消息排队**。
现在的消息总线，发布者消息后，需要原地 await handler，
比如用户发了消息，要等待agent loop跑完才能发另外一条消息，
而真实的用户很有可能是希望发完一条消息之后，立刻再补充新的内容，如上这个最简设计是承载不了这样的操作的。
下一节咱们就来看看，如何做。

