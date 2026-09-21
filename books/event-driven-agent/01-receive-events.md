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

history 每个 session 一份消息序列，第一轮开始时垫一条 system prompt。loop 本身是标准的loop：拼装message调用模型，模型要工具就执行，把结果喂回去继续调用模型，模型不要工具了，turn 就结束：

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

### SessionLog：append-only trajectory 记录

我们把事件、与 LLM 交互的历史，通通 append-only 写入 jsonl 作为 trajectory 记录，以便后续基于轨迹做分析。
轨迹不只是对话：assistant 的每次工具调用和工具的返回也在里面——它们是模型上下文的一部分，缺了它们，"模型为什么这么答"就无从分析。

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

五个对象凑齐了，一次 turn 的数据流大致如下：

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

终端实录：asciinema 录的 `.cast` 渲染成 gif，同目录还有 `.mp4`，`index.json` 里记着每个 case
的说明。每个 case 都能单独复跑，case 名是 `两位编号-语义名`——**编号让文件名字典序 = 演示顺序**，
语义名说明这个 case 在看什么：

```
stage01-demo 01-read-stock     # → rec/stage01/docs/01-read-stock.gif
stage01-demo --list            # 五个 case 及说明
```

模型是本地 ollama 的 qwen3.5:4b-32k（OpenAI 兼容端点），零 API 成本。时间戳从进程启动算起
（开头半秒左右是解释器、SDK 导入和模型冷启动）。每个录像开头那行
`── <case 名>：<在看什么> ──` 就是这个 case 的说明。

五个 case：读 → 写 → 读回 → 查规则 → 越界，各录一段。

### 01-read-stock：纯读——模型自己挑读工具，不写任何文件

![01-read-stock：纯读库存](../../src/baby_event_driven_agent/rec/stage01/docs/01-read-stock.gif)

`query_inventory({"category":"保温杯"})` → 42 件。工具是模型自己选的：路由依据只有工具描述
和 system prompt，代码里没有一个 if-else 指路。

### 02-write-stock：带副作用的写——真往 inventory.txt 追加一行

![02-write-stock：写库存](../../src/baby_event_driven_agent/rec/stage01/docs/02-write-stock.gif)

`update_inventory({"category":"马克杯","stock":8,"spec":"陶瓷, 350ml"})` → 已更新。跑前跑后
diff `knowledge-base/inventory.txt`，能看见多出来的那一行。

### 03-read-back：读回刚写进去的那行

![03-read-back：读回库存](../../src/baby_event_driven_agent/rec/stage01/docs/03-read-back.gif)

`query_inventory({"category":"马克杯"})` → 8 件。这一问是 02 的对照：写是真的落了库，不是模型
嘴上说写成功（单独跑它前先跑 02-write-stock）。

### 04-search-rules：换个知识库查规则

![04-search-rules：查规则](../../src/baby_event_driven_agent/rec/stage01/docs/04-search-rules.gif)

`search_rules({"query":"报销"})` → 规则原文。同样要事实，换了个知识库（`rules.txt`），看模型
选不选得对工具。

### 05-off-topic：知识库答不了的问题

![05-off-topic：越界问题](../../src/baby_event_driven_agent/rec/stage01/docs/05-off-topic.gif)

一个工具都没调：思考里把四个工具逐个排除，援引 system prompt 里"获取不到准确信息就回答不知
道，严禁编造"那条，最后明说这事没法基于事实下结论。

这五段合起来说明一件事：**路由权在模型，边界由 prompt 兜底**——前四个 case 各选对了工具，
代码里没有一个 if-else 指路；05 一问它一个工具都没调，援引的正是 `build_system_prompt` 生成
的那句收尾。说明 qwen3.5-4b 虽然是个本地小模型，在处理简单任务时依然能够遵从用户指令。
当然，生产级 agent 要杜绝模型胡编乱造，不能只靠 system prompt 约束，后续咱们再一步一步优化。

五个 case 还连成一条线（同一进程、同一个 session，"读→写→读回"的依赖才成立），整段也有
录像：`rec/stage01/docs/all.gif`。

再看看，session log 里每一轮 turn 都有头有尾（下面是 01-read-stock 那一轮的原文）：

```json
{"ts": 0.37, "type": "user_input", "session": "A", "payload": {"text": "保温杯还有库存吗"}, "note": "turn start"}
{"ts": 5.03, "type": "agent_reply", "session": "A", "payload": {"message": {"role": "assistant", "content": null, "tool_calls": [{"id": "call_upz114wp", "type": "function", "function": {"name": "query_inventory", "arguments": "{\"category\":\"保温杯\"}"}}]}}, "note": "tool_call"}
{"ts": 5.03, "type": "tool_result", "session": "A", "payload": {"tool_call_id": "call_upz114wp", "name": "query_inventory", "result": "保温杯：库存 42 件；316L 不锈钢内胆，500ml，杯身磨砂黑。"}, "note": "tool result"}
{"ts": 7.97, "type": "agent_reply", "session": "A", "payload": {"message": {"role": "assistant", "content": "保温杯目前有库存，共42件，规格为316L不锈钢内胆，500ml容量，杯身磨砂黑色。"}}, "note": "final"}
{"ts": 7.97, "type": "turn_end", "session": "A", "payload": {}, "note": "turn end"}
```

v0.1 在它的能力范围内是个真实可用的 agent。
append-only 这个决定从这一章开始生效：事件只追加、不改写。
到 Stage 5，回放重建状态靠它；到 Stage 6，可重复的 eval 也靠它。

## 设计边界，以及下一章的需求

如上就是最简单的通过事件驱动的agent loop，其实还存在不少明显的问题，你看出来了几个。

比如，**消息插入**和**消息排队**。
现在的消息总线，发布者消息后，需要原地 await handler，
比如用户发了消息，要等待agent loop跑完才能发另外一条消息，
而真实的用户很有可能是希望发完一条消息之后，立刻再补充新的内容，如上这个最简设计是承载不了这样的操作的。
下一节咱们就来看看，如何做。

