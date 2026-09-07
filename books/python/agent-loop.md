# 如何创建一个事件驱动的agent-loop

事件定义应包含，事件的类型(来源)，事件的内容，事件的名称；
Agent的执行容器，应该有一个收件箱，用于接收事件，应该有一个分发函数，用于将事件分发到对应的执行逻辑:
1. 正常的agent执行；
2. 中断消息要处理中断；
3. 其他的一些业务逻辑；

message格式的定义。

agent 和模型交互的载体是 messages 数组，它属于请求体：你发一组 message 进去，模型回一个 response。
这两件事得分开看——response 里只有 `message` 那一段才是一条 message，`finish_reason`、`usage`、耗时是 response 级的字段，不进 messages。循环里 append 回去的是前者，不是把整个 response 塞进去。

一条 message 长什么样，由 role 决定。

**system / developer**

```json
{ "role": "system", "content": "你是一个…" }
```

`content` 一般是字符串，需要多模态或分段缓存时可以是 part 数组。

**user**

```json
{ "role": "user", "content": "北京今天天气怎么样" }
```

带图片、音频时 `content` 换成 part 数组：

```json
{
  "role": "user",
  "content": [
    { "type": "text", "text": "这张图里写了什么" },
    { "type": "image_url", "image_url": { "url": "data:image/png;base64,..." } }
  ]
}
```

**assistant**

模型产出的那条，也是唯一会在 `content` 之外多带东西的形态：

```json
{
  "role": "assistant",
  "content": "我查一下",
  "tool_calls": [
    {
      "id": "call_1",
      "type": "function",
      "function": { "name": "get_weather", "arguments": "{\"city\":\"北京\"}" }
    }
  ]
}
```

`content` 可以为 null——只要调工具、不说话的时候就是空的；它和 `tool_calls` 也可以并存，模型完全可以一边说话一边要调工具（Anthropic 里两者本来就是同一个 content 数组里的 text 块和 tool_use 块）。

两个细节：`function.arguments` 是 JSON **字符串**不是对象，漏一次序列化就是 400；`refusal` 出现时 `content` 是空的，看着像空回复，其实是明确的拒答信号。

开了 thinking 的模型还会在 assistant 消息上带思考内容（DeepSeek 是顶层 `reasoning_content`，Anthropic 是 content 里的 thinking 块），上面带加密签名，得原样带回去，改了、删了、重排了直接 400。

**tool**

工具结果，只需要说清两件事：回指哪个调用、结果是什么：

```json
{
  "role": "tool",
  "tool_call_id": "call_1",
  "name": "get_weather",
  "content": "晴，26℃，东南风 3 级"
}
```

**三条约束**

- 工具结果有两套表达：Chat Completions 是独立的 `role=tool` 消息，Anthropic 是 user 消息 content 里的 `tool_result` 块。选一套用，别在同一个结构里同时留两种。
- `tool_call_id` 必须回指 `tool_calls` 里的 id，一一配对。上下文不够时只能替换这条消息的内容，不能把它删掉——服务端会校验配对，删了请求直接被拒。
- 角色名各家不完全一致：Anthropic 只有 user / assistant，Google 把 assistant 叫 model。四家的字段对照见《从 Messages 到 Agent Loop》第一节。

---

轨迹(trajectory)是agent交互的过程资产，记录了运行期间完整的交互历史。基于轨迹可以定期提取用户长期记忆、构建评测集、做模型后训练、做递归自我改进（RSI）。

轨迹和 messages 不是一回事。messages 是发给模型看的视图，轨迹是实际发生过的事，前者只是后者的一部分。被截断之前的工具原始返回、每次调用的耗时和 token、重试了几次、被谁中断的、什么时候压缩的——这些都不进 messages，但都要进轨迹。只存 messages 的话，压过一次缩就再也回不去了。

所以轨迹的主体是一条 append-only 的扁平事件流 `events`，它是唯一真相源，turn / step / messages 都是从它派生的视图。嵌套结构写盘要回头改父节点，事件流只要追加，进程崩了也不丢已落盘的部分。

顶层：
```json
{
  "schema_version": "1.0",
  "session_id": "sess_01J8",
  "user_id": "u_1024",
  "agent_id": "weather_agent",
  "status": "completed",
  "created_at": "2026-09-06T19:53:40+08:00",
  "updated_at": "2026-09-06T19:54:12+08:00",
  "env": {
    "model": "deepseek-v4",
    "system_prompt_id": "sys_weather_v3",
    "system_prompt_sha": "9f2c1a",
    "tools": [
      { "name": "get_weather", "schema_sha": "b71de0" }
    ],
    "code_version": "a3f91c2"
  },
  "summary": {
    "turns": 1,
    "steps": 2,
    "tool_calls": 1,
    "errors": 0,
    "end_reason": "stop",
    "wall_time_ms": 32000,
    "usage": { "input_tokens": 2043, "output_tokens": 156 }
  },
  "events": []
}
```

`env` 里那几个 sha 决定这条轨迹能不能复现。换个模型版本、或者改一句 system prompt，同一条轨迹的结论就对不上了。

轮次（一次用户输入 = 一个 turn，一个 turn 可能跑多个 step，一个 step = 一次模型调用）：
```json
{
  "type": "turn_start",
  "seq": 1,
  "event_id": "evt_001",
  "ts": "2026-09-06T19:53:40+08:00",
  "session_id": "sess_01J8",
  "turn_id": "turn_1",
  "step_id": null,
  "parent_id": null,
  "actor": "user",
  "message": { "role": "user", "content": "北京今天天气怎么样" },
  "payload": {
    "input": { "text": "北京今天天气怎么样", "attachments": [] }
  }
}
```

事件通用字段：

- `seq`：会话内单调递增，落盘顺序就是它，重放按它排序。
- `type`：`turn_start` / `turn_end` / `step_start` / `step_end` / `model_request` / `model_response` / `tool_call` / `tool_result` / `error` / `interrupt` / `compaction` / `memory_extracted` / `subagent_start` / `subagent_end`。
- `parent_id`：子 agent 或嵌套步骤指回父事件，用来表达这一步是谁发起的。
- `actor`：`user` / `model` / `tool` / `system` / `subagent`。
- `message`：这一事件对应的 message，就是上面四种形态之一；没有就置 null（比如工具结果的原文放 payload，不进 message）。
- `payload`：按 type 定，各家差异最大的部分。

几类 payload 示例：

```json
{ "type": "model_response",
  "message": {
    "role": "assistant",
    "content": "我查一下",
    "tool_calls": [
      { "id": "call_1", "type": "function",
        "function": { "name": "get_weather", "arguments": "{\"city\":\"北京\"}" } }
    ]
  },
  "payload": {
    "finish_reason": "tool_calls",
    "usage": { "input_tokens": 812, "output_tokens": 47 },
    "latency_ms": 1830,
    "raw_ref": "s3://traces/sess_01J8/resp_2.json"
}}

{ "type": "tool_call", "payload": {
    "call_id": "call_1", "name": "get_weather",
    "arguments": { "city": "北京" },
    "timeout_ms": 10000
}}

{ "type": "tool_result", "payload": {
    "call_id": "call_1",
    "output_raw": "晴，26℃，东南风 3 级 …（后面还有 8000 字）",
    "output_sent": "晴，26℃，东南风 3 级 … [已截断，原文 1.2 万字]",
    "truncated": true, "is_error": false, "duration_ms": 940
}}

{ "type": "interrupt", "payload": {
    "source": "user", "reason": "cancel",
    "completed_steps": 2, "pending_tool_calls": ["call_3"]
}}

{ "type": "compaction", "payload": {
    "replaced_events": [3, 17],
    "summary_text": "用户问北京天气…",
    "model": "deepseek-v4"
}}

{ "type": "memory_extracted", "payload": {
    "candidates": [{ "kind": "preference", "text": "用户在北京" }],
    "source_event_range": [1, 42]
}}
```

`tool_result` 的 payload 里 `output_raw` 和 `output_sent` 是两份：进 message 的是截断后的 `output_sent`，原文留在轨迹里。压缩上下文是策略，丢原文是事故——没有 `output_raw`，既没法调整截断策略，也没法回头判断这一步到底错在哪。

`compaction` 记 `replaced_events` 是同一个道理：压缩只改视图，被换掉那几条 message 所在的 event 还在 events 里，随时能还原。message 本身不带 id，轨迹里一律用 seq 定位。


应该有一个控制agent工作的函数，用于启动真正的agent-loop(是一个coro)，并且能够处理他的异常；
以及真正的agent-loop的逻辑；

