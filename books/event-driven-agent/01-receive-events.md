# Stage 1：只是接收事件

> 配套代码：`src/baby_event_driven_agent/stages/stage01_receive_events/`，
> 可跑（`stage01-demo` / `stage01-test`）、带 tests。
> 这是整个系列的地基：一个真实可用的 agent，v0.1——真模型、流式输出。
> 后面六章不加新剧情，只回应一个接一个的真实需求。

## 我们要什么

一个事件驱动的 agent，v0.1 长这样：

- 一个 `Event`：类型、session_id、payload。
- 一个 `EventBus`：谁关心什么事件就注册 handler，事件来了原地调用。
- 一个 agent：收到 `user_input`，跑一轮 loop（模型 → 工具 → 模型 → 回话），
  模型输出是流式的：文本增量边到边发给 UI，工具调用增量边到边累积。
- 一个 session log：所有事件 append-only 写进 jsonl。这章没人读它，但它记录的
  是唯一真相，后面每章都会回来找它。
- 一个 CLI UI：stdin 收输入，stdout 流式打回复。

设计决定只有一条值得注意：总线的 publish 原地 await handler，
所以 agent 的整个 turn 是在总线回调里跑完的。
在"一次处理一个请求"的前提下，这个决定完全成立——先看它正常干活的样子。

## 代码

### events：事件、总线、session log

```python
import asyncio, json, time
from dataclasses import dataclass, field

T0 = time.time()
def t(): return time.time() - T0

@dataclass(frozen=True)
class Event:
    type: str            # user_input / agent_reply / turn_end
    session_id: str
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

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

### LLM 客户端与工具

模型是真的：OpenAI 兼容端点，配置读仓库根 `.env`（环境变量优先于文件，
临时换模型不用改文件）。客户端只讲一种"增量协议"——文本增量、工具调用
增量，流结束即本轮请求结束：

```python
class RealLLM:
    """OpenAI 兼容流式客户端，stream_chat 产出归一化增量块。"""

    def __init__(self):
        api_key = _cfg("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("缺少 OPENAI_API_KEY：写在仓库根 .env 或环境变量里")
        self.model = _cfg("OPENAI_MODEL")
        self._client = AsyncOpenAI(
            api_key=api_key, base_url=_cfg("OPENAI_API_BASE") or None, timeout=60.0
        )

    async def stream_chat(self, messages):
        stream = await self._client.chat.completions.create(
            model=self.model, messages=messages, tools=TOOL_SCHEMAS, stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            # qwen3 系列思考内容走 reasoning_content，不是面向用户的输出，跳过
            if (delta.model_extra or {}).get("reasoning_content"):
                continue
            if delta.content:
                yield {"type": "text_delta", "text": delta.content}
            for tc in delta.tool_calls or []:
                fn = tc.function
                yield {"type": "tool_call_delta", "index": tc.index,
                       "id": tc.id or None, "name": fn.name if fn else None,
                       "args_delta": (fn.arguments if fn else "") or ""}
```

工具是真工具：`search` 在本地知识库文件里逐行检索（先按空格分词，
整句分不出词就退化成 2 字滑窗），数据源是同目录的 `knowledge.txt`：

```text
保温杯：库存 42 件；316L 不锈钢内胆，500ml，杯身磨砂黑。
玻璃杯：库存 17 件；高硼硅玻璃，400ml，可进微波炉。
会议室预订：找行政小王，订前先看日历有没有被锁。
...
```

tests 里用 `FakeLLM`：和 `RealLLM` 一模一样的 stream_chat 协议，
arguments 故意拆成两块发，逼消费端的增量累积逻辑真实工作。
测试要确定性、不花钱、不依赖网络，所以替身只活在 tests 里。

### agent：流式消费，handler 里跑整个 turn

```python
SYSTEM_PROMPT = (
    "你是一个带工具的通用 agent。search 工具检索的是本地知识库（团队笔记），"
    "用户问到笔记里可能有的信息时，先检索再根据结果回答。"
)

class Agent:
    def __init__(self, bus, log, llm):
        self.bus, self.log, self.llm = bus, log, llm
        self.history = {}  # session_id -> messages

    async def on_user_input(self, event):
        await self._run_turn(event)  # 整个 turn 在总线回调里跑完

    async def _step(self, sid, history):
        """消费一轮流式输出：文本边到边发 agent_delta，边累积，流结束拼完整消息。"""
        text_parts, tool_calls = [], {}  # index -> 累积中的调用
        async for chunk in self.llm.stream_chat(history):
            if chunk["type"] == "text_delta":
                text_parts.append(chunk["text"])
                await self.bus.publish(Event("agent_delta", sid, {"text": chunk["text"]}))
            elif chunk["type"] == "tool_call_delta":
                tc = tool_calls.setdefault(chunk["index"], {"id": "", "name": "", "args": ""})
                if chunk.get("id"):   tc["id"] = chunk["id"]
                if chunk.get("name"): tc["name"] = chunk["name"]
                tc["args"] += chunk.get("args_delta", "")
        if tool_calls:  # 流结束，增量拼成合法的 assistant 消息
            return {"role": "assistant", "content": "".join(text_parts) or None,
                    "tool_calls": [{"id": tc["id"], "type": "function",
                                    "function": {"name": tc["name"], "arguments": tc["args"]}}
                                   for _, tc in sorted(tool_calls.items())]}
        return {"role": "assistant", "content": "".join(text_parts)}

    async def _run_turn(self, event):
        sid = event.session_id
        history = self.history.setdefault(sid, [])
        if not history:
            history.append({"role": "system", "content": SYSTEM_PROMPT})
        history.append({"role": "user", "content": event.payload["text"]})
        self.log.append(event, note="turn start")
        for _ in range(4):
            msg = await self._step(sid, history)
            history.append(msg)
            await self.bus.publish(Event("agent_reply", sid, {"message": msg}))
            # 完整回答进 log（agent_delta 不记：它是传输层的瞬时增量，
            # 累积结果就是这条 reply）
            self.log.append(Event("agent_reply", sid, {"message": msg}),
                            note="tool_call" if msg.get("tool_calls") else "final")
            if "tool_calls" not in msg:
                self.log.append(Event("turn_end", sid, {}), note="turn end")
                return
            for call in msg["tool_calls"]:
                result = await TOOLS[call["function"]["name"]](
                    json.loads(call["function"]["arguments"]))
                history.append({"role": "tool", "tool_call_id": call["id"],
                                "content": result})
        self.log.append(Event("turn_end", sid, {}), note="max steps")
```

两个实战细节值得停一下：

- **system prompt 不能省**。第一版没有它，模型把"保温杯还有库存吗"当闲聊，
  回了一句"我无法访问实时库存"——它根本不知道 search 能查到什么。
- **工具调用增量必须累积**。实测流式下 arguments 是分块到的：
  首块带 `id` 和 `name`，后面几块各自带一段 JSON 字符串，
  拼齐、流结束，才是一条合法的 assistant 消息。

### CLI：一问答完，再问下一句

UI 订阅两个事件：`agent_delta` 收到就原地打印（不换行、立刻 flush，
这就是流式呈现的全部），`agent_reply` 收到打一行收尾。

```python
async def main():
    bus, log = EventBus(), SessionLog("session.jsonl")
    agent = Agent(bus, log, RealLLM())
    bus.subscribe("user_input", agent.on_user_input)

    async def ui_delta(e):
        print(e.payload["text"], end="", flush=True)

    async def ui_reply(e):
        msg = e.payload["message"]
        if msg.get("tool_calls"):
            calls = ", ".join(f"{c['function']['name']}({c['function']['arguments']})"
                              for c in msg["tool_calls"])
            print(f"\n[{t():5.2f}s] (A) → 工具调用：{calls}")
        else:
            print(f"\n[{t():5.2f}s] (A) —— 回答完毕")

    bus.subscribe("agent_delta", ui_delta)
    bus.subscribe("agent_reply", ui_reply)

    print(f"[{t():5.2f}s] (A) 用户输入：保温杯还有库存吗")
    await bus.publish(Event("user_input", "A", {"text": "保温杯还有库存吗"}))
    print(f"\n[{t():5.2f}s] (A) 收到完整回答，用户接着问：玻璃杯呢")
    await bus.publish(Event("user_input", "A", {"text": "玻璃杯呢"}))
    print(f"\n[{t():5.2f}s] demo 结束")

asyncio.run(main())
```

## 跑一下（真实 LLM 实测输出）

模型 qwen3.7-flash（OpenAI 兼容端点），时间戳从进程启动算起
（前 2 秒多是 Python 解释器和 SDK 的导入时间）：

```text
[ 2.73s] (A) 用户输入：保温杯还有库存吗

[ 5.99s] (A) → 工具调用：search({"query": "保温杯 库存"})
保温杯还有库存，目前剩余 42 件。

具体信息如下：
- **内胆**：316L 不锈钢
- **容量**：500ml
- **外观**：杯身磨砂黑
[ 6.74s] (A) —— 回答完毕

[ 6.76s] (A) 收到完整回答，用户接着问：玻璃杯呢
玻璃杯还有库存，剩余 **17** 件。

具体参数如下：
- **材质**：高硼硅玻璃
- **容量**：400ml
- **特点**：可进微波炉
[ 8.61s] (A) —— 回答完毕

[ 8.62s] demo 结束
```

值得看三个细节：

- 5.99s 到 6.74s 之间那段"保温杯还有库存……"不是一次性打印的，
  是 `agent_delta` 一块一块到达、原地 flush 出来的流式呈现。
- 数字都是真的：42 件、316L、500ml 全部来自 `knowledge.txt`，
  是 search 工具检索后模型组织出来的回答。
- 第二问"玻璃杯呢"模型**没有再调 search**——第一轮的 tool 结果里
  已经带着玻璃杯那行，它直接从上下文组织了回答。这是真模型自己的
  选择，不是代码里写死的。

session log 里两轮 turn 有头有尾，完整回答都进了 log（append-only）：

```json
{"ts": 2.73, "type": "user_input", "session": "A", "payload": {"text": "保温杯还有库存吗"}, "note": "turn start"}
{"ts": 5.99, "type": "agent_reply", "session": "A", "payload": {"message": {"role": "assistant", "content": null, "tool_calls": [{"id": "call_9924f5f5c59d4555b2b537c0", "type": "function", "function": {"name": "search", "arguments": "{\"query\": \"保温杯 库存\"}"}}]}}, "note": "tool_call"}
{"ts": 6.74, "type": "agent_reply", "session": "A", "payload": {"message": {"role": "assistant", "content": "保温杯还有库存，目前剩余 42 件。……"}}, "note": "final"}
{"ts": 6.75, "type": "turn_end", "session": "A", "payload": {}, "note": "turn end"}
{"ts": 6.76, "type": "user_input", "session": "A", "payload": {"text": "玻璃杯呢"}, "note": "turn start"}
{"ts": 8.61, "type": "agent_reply", "session": "A", "payload": {"message": {"role": "assistant", "content": "玻璃杯还有库存，剩余 **17** 件。……"}}, "note": "final"}
{"ts": 8.62, "type": "turn_end", "session": "A", "payload": {}, "note": "turn end"}
```

v0.1 在它的能力范围内是个真实可用的 agent。
append-only 这个决定从这一章开始生效：事件只追加、不改写。
到 Stage 6，回放重建状态靠它；到 Stage 7，可重复的 eval 也靠它。

## 设计边界，以及下一章的需求

有一件事 v0.1 没有设计：**排队**。

publish 原地 await handler，意味着 agent 的整个 turn 占着总线回调不放。
这个前提下，"用户在回答还在跑的时候又发一条消息"没有安身之处——
设计里没有任何东西接住它、让它等。

而真实用户一定会这么干：回答要跑两秒，他 0.2 秒后就想到问题问错了，
想纠正；或者问完一个问题紧接着想问下一个，不想盯着屏幕等。

所以下一个需求很明确：消息要先有地方排队，agent 决定什么时候消费、
怎么解释消费到的消息。这就是 Stage 2 的收件箱，附带两个新语义——
steering（插进正在跑的回答）和 followup（排在回答之后）。

顺带看清一个事实，下一章会反复用到：asyncio 给了你并发，没给你串行化。
串行化要自己买，收件箱就是那个价钱。

## 验证

- 环境：Python 3.13.12，假 LLM / 假工具，无需 API key；代码在仓库
  `src/baby_event_driven_agent/stages/stage01_receive_events/`。
- 实跑：`stage01-demo`（pyproject `[project.scripts]` 注册的命令）输出即正文
  时间线（2026-09-08 实际运行，非手写）。
- pytest：`stage01-test` 3 passed——顺序问答与 log 完整性、多 session history
  隔离、log append-only。
- 测试不用 pytest 的 tmp_path fixture（WorkBuddy 沙箱 shim 会拦
  pytest-of-unknown 的 mkdir），用 tempfile.mkdtemp 自建自清理。
