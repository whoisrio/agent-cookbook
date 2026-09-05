# 从 Completions 到 Responses：LLM API 演化与多 Provider 适配实录

2026 年 7 月，随着DeepSeek-V4-flash正式版的发布，DeepSeek 同时官宣了为了满足大家对 Codex 的需求，开始支持Responses API 。虽然只是阉割版本的Response API，但是聊胜于无。

Responses API 到底比 Chat Completions 好在哪里，值得各家去兼容？今天咱们一块来了解一下。

---

## 一、API 演化：三代接口，从裸续写到 Agent 内循环

LLM API 的发展史，本质上是「模型能干什么」推动「API 该长什么样」的过程。三代 API，每一代都对应模型能力的一次质变。

### 1. `/v1/completions`（2020，GPT-3）：没有「对话」这回事

这是最原始的形态。一个 prompt 进去，一个 text 出来，仅此而已。如下示例，
请求，
```shell
curl https://api.openai.com/v1/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-xxxx" \
  -d '{
    "model": "davinci",
    "prompt": "写一个冰淇淋店的标语：",
    "max_tokens": 32,
    "temperature": 0.7,
    "top_p": 1,
    "n": 1,
    "stream": false
  }'
```
响应
```shell
{
  "id": "cmpl-7C9Wxi9Du4j1lQjdjhxBlO22M61LD",
  "object": "text_completion",
  "created": 1683130927,
  "model": "davinci",
  "choices": [
    {
      "text": "\n\n让甜牙齿在我们的奶油小屋撒野",
      "index": 0,
      "finish_reason": "length",
      "logprobs": null
    }
  ],
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 16,
    "total_tokens": 26
  }
}
```
没有 system prompt 这个概念——你想约束模型行为，就把指令写在 prompt 最前面。没有多轮对话——每一轮你自己拼上下文，拼到 token 上限为止。
没有 message 角色 system/user/assistant 的分工，也没有 function call。像构造对话，就得自行在prompt里拼装，角色边界完全靠约定，容易漂移。
```json
你是一个手机客服。
用户：我的手机开不了机。
客服：请长按电源键10秒。
用户：还是没反应。
客服：
```
openai不久后，将永久停止对completions api的支持；国产的开源模型其实也没支持过这一版本的api。

### 2. `/v1/chat/completions`（2023.3，GPT-3.5-Turbo）：对话成为一等公民
随着chapgpt在2022年底的横空出世，2023年openai也推出了 /v1/chat/completions api，这版api中，对话成为一等公民，
把「对话」建模为原生数据结构，而不是靠码农构造角色，`system` / `user` / `assistant` / `tool` 四种角色分工明确；。
模型的后训练阶段，也会对chat中的角色做针对性的强化，让模型能够更好的区分出系统指令和用户指令。
同时，添加了function calling的支持，极大的拓展了模型的能力边界，可以说是agent走向能用的起点。

咱们看一个用户询问天气的交互示例，
**请求结构如下：**
```json
{
  "model": "gpt-5.5",
  "messages": [
    {"role": "system", "content": "你是一个天气助手"},
    {"role": "user", "content": "北京今天天气怎么样？"}
  ],
  "tools": [{
    "type": "function",
    "function": {
      "name": "get_weather",
      "description": "查询指定城市的天气",
      "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"]
      }
    }
  }],
  "tool_choice": "auto"
}
```


**模型收到用户消息后，返回工具调用，此时的`finish_reason`是`tool_call`，表示模型认为需要调用工具获得更多信息：**

```json
{
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_abc123",
        "type": "function",
        "function": {
          "name": "get_weather",
          "arguments": "{\"city\": \"北京\"}"
        }
      }]
    },
    "finish_reason": "tool_calls"
  }],
  "usage": {"prompt_tokens": 150, "completion_tokens": 30, "total_tokens": 180}
}
```

**本地执行工具获得结果后返回给模型，此时需要回复完整的message列表**

```json
[
  {"role": "system", "content": "你是一个天气助手"},
  {"role": "user", "content": "北京今天天气怎么样？"},
  {
    "role": "assistant",
    "content": null,
    "tool_calls": [{
      "id": "call_abc123",
      "type": "function",
      "function": {
        "name": "get_weather",
        "arguments": "{\"city\": \"北京\"}"
      }
    }]
  },
  {
    "role": "tool",
    "tool_call_id": "call_abc123",
    "content": "{\"temperature\": \"32°C\", \"condition\": \"晴\", \"humidity\": \"45%\"}"
  }
]
```

**模型给出最终答复，`finish_reason`是`stop`表示模型认为此次任务可以结束了**

```json
{
  "id": "chatcmpl-9Xk3mNpQr7vLw2Yz8AbCdE",
  "object": "chat.completion",
  "created": 1723356789,
  "model": "gpt-4o-2024-08-06",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "北京今天天气晴朗，气温32°C，湿度45%，体感偏热，建议注意防晒和补水。"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 200,
    "completion_tokens": 28,
    "total_tokens": 228
  }
}
```
如上就是一次完整的交互了，需要注意的是，assistant 和 tool 的消息必须是成对出现的，这样的组合在模型后训练阶段做过针对性的训练以让模型更好的理解工具调用结果，
如果assistant要求调用工具后接了非工具调用结果的消息，模型很可能当成工具结果来处理，导致模型犯错。

这个 API 统治了 2023至今 的 LLM 爆发期，至今仍然是各家 Provider 适配的主流 API。但是它仍然也有一些局限性，

#### 你要手动拼装整个 agent loop
**一：你要手动拼装整个 agent loop。** 
如上样例所示，模型返回 tool call 后，你需要：解析 arguments（它是 JSON 字符串）→ 执行函数 → 手动拼回 messages 数组（把 assistant 消息和 tool 消息按顺序塞回去）→ 再发起一次请求。这是「用户问天气 → 调工具 → 回复」这个最简单的 agent loop 的完整手动流程：

```python
# 第一轮
resp = client.chat.completions.create(model="gpt-5.5", messages=messages, tools=tools)
msg = resp.choices[0].message

# 手动解析 JSON string
fn_args = json.loads(msg.tool_calls[0].function.arguments)

# 执行工具，拿到结果
tool_result = call_weather_api(fn_args["city"])

# 手动拼回 messages
messages.append(msg.model_dump())
messages.append({"role": "tool", "tool_call_id": msg.tool_calls[0].id, "content": tool_result})

# 第二轮
resp2 = client.chat.completions.create(model="gpt-5.5", messages=messages)
print(resp2.choices[0].message.content)
```

每一步都是你自己写的胶水代码。

#### 返回结构嵌套太深
**二：返回结构嵌套太深。** `choices[0].message.tool_calls[0].function.arguments`——四层路径。而且 tool_calls 和 content 互斥（有 tool call 时 content 是 null），但都在同一个 message 对象里。类型系统帮不了你：你拿到一个 `message`，只能自己判断它是文本回复还是工具调用。

#### streaming 靠猜
**三：streaming 靠猜。** SSE 流里所有 chunk 都是同样的 `data` 事件，delta 字段可能是文本、可能是 tool call 片段、可能是 finish_reason。你必须自己写状态机来区分：

```
data: {"choices":[{"delta":{"content":"今天"},"index":0}]}
data: {"choices":[{"delta":{"content":"天气"},"index":0}]}
data: {"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"{\"ci"}}]}},"index":0}]}
```
#### reasoning 状态每轮丢失
**四：reasoning 状态每轮丢失。** Chat Completions 的每一次 API 调用都是一次「失忆」——模型在上轮花了 2000 个 thinking token 想清楚的问题，下轮从头开始。

#### system prompt 混在 messages 里
**五：system prompt 混在 messages 里。** Chat Completions 把系统指令塞在 messages 数组第一条，和对话历史混在一起——system 虽然是一等公民，但只是「messages 里的一种 role」。另一方面，参数也越堆越乱：`response_format` 管 JSON、`tool_choice` 管工具选择、`parallel_tool_calls` 管是否并行、`stream_options` 管附加信息，全是平铺在顶层请求体里的独立开关，每加一个能力就加一个顶级参数。

### 3. `/v1/responses`（2025.3）：统一 agent loop，缓解五大痛点
随着模型能力越来越强，openai在2025年3月推出了response api，支持有状态，Responses API 的定位是「一个请求承载一个完整的 agent loop」——用户发话 → 模型推理 → 需要工具就调 → 拿到结果继续 → 返回最终回复。
另外，在工具调用方面，在服务端，也直接集成了一些列内置工具如web_search、code_interpreter等等，让使用这类的基础工具能力变得更加容易。

咱们看看response api的请求和返回，
**请求（带上工具定义，让模型决定是否调用）：**
```json
POST /v1/responses
{
  "model": "gpt-5.5",
  "input": "北京今天天气怎么样？",
  "instructions": "你是一个天气助手，用户问天气时先调工具查数据再回复。",
  "tools": [{
    "type": "function",
    "name": "get_weather",
    "description": "查询指定城市的实时天气",
    "parameters": {
      "type": "object",
      "properties": {
        "city": {"type": "string", "description": "城市名称"}
      },
      "required": ["city"]
    }
  }],
  "tool_choice": "auto",
  "temperature": 0.7
}
```

**返回（模型决定调用 get_weather）：**
```json
{
  "id": "resp_abc123def456",
  "object": "response",
  "created_at": 1722758400,
  "status": "completed",
  "model": "gpt-5.5-2026-06-15",
  "output": [
    {
      "id": "fc_abc123",
      "type": "function_call",
      "call_id": "call_xyz789",
      "name": "get_weather",
      "arguments": "{\"city\": \"北京\"}",
      "status": "completed"
    }
  ],
  "usage": {
    "input_tokens": 120,
    "output_tokens": 25,
    "total_tokens": 145
  }
}
```

把工具结果传回去：

```json
POST /v1/responses
{
  "model": "gpt-5.5",
  "input": [{
    "type": "function_call_output",
    "call_id": "call_xyz789",
    "output": "{\"temperature\": 25, \"condition\": \"晴\", \"humidity\": 45}"
  }],
  "previous_response_id": "resp_abc123def456"
}
```
模型的最终回复
```json
{
  "id": "resp_def456ghi789",
  "object": "response",
  "status": "completed",
  "model": "gpt-5.5-2026-06-15",
  "output": [
    {
      "id": "msg_abc123",
      "type": "message",
      "role": "assistant",
      "status": "completed",
      "content": [{
        "type": "output_text",
        "text": "北京今天晴，气温 25°C，湿度 45%，适合户外活动。",
        "annotations": []
      }]
    }
  ],
  "usage": {
    "input_tokens": 60,
    "output_tokens": 30,
    "total_tokens": 90
  }
}
```
看上去是不是简洁了许多，对比 Chat Completions,response api做了五个关键改进：
---
#### 改进一：不需要手动拼装消息。
**改进一：不需要手动拼装消息。**，支持有状态；

Responses API 的 `input` 替代了 `messages`。
简单场景直接扔字符串，复杂场景扔 item 数组。Agent Loop里不再需要手动拼 messages了。内置工具（web_search 等）由服务端一次性跑完；自定义 function call 做agent环境中执行、再用 `previous_response_id` 把结果传回去，但不用像 Chat Completions 那样手动重建整段对话历史。
System prompt可以放到instruction字段，不过要注意的是这个字段是无状态的，不能通过previous_response_id继承；如果不喜欢instruction，在input列表里，使用developer角色来承载system prompt也是可以的；

```python
import json

response = client.responses.create(
    model="gpt-5.5",
    input="北京天气怎么样？",
    instructions="你是一个天气助手",
    tools=[{"type": "function", "name": "get_weather", ...}]
)

# output 直接拿到 tool call
tool_call = response.output[0]          # type: function_call
args = json.loads(tool_call.arguments)  # arguments 仍是 JSON string
city = args["city"]

# 传入结果，引用上轮
response2 = client.responses.create(
    model="gpt-5.5",
    input=[{
        "type": "function_call_output",
        "call_id": tool_call.call_id,
        "output": json.dumps({"temp": 25, "condition": "晴"})
    }],
    previous_response_id=response.id
)

print(response2.output_text)            # 不再写 choices[0].message.content
```
这个方式，把消息message list的工作交给了服务端，也把控制context的主动权丢掉了，在context 压缩或者做session切换的时候，不一定好用，大部分宣称支持response api的provider，其实也没有支持这个能力，继续在response api上使用自行拼装消息列表的形式。
---

#### 改进二：服务端内置工具。
**改进二：服务端内置工具。**

Chat Completions 的工具全是「声明式」的——你声明函数签名，模型决定调用，但实际执行是你自己做的（调外部 API、查数据库、搜网页）。
Responses API 把 web_search、file_search、code_interpreter 等变成了服务端原生工具：

```python
# 1) 最小用法：声明工具类型，不需要传入工具定义，OpenAI 服务端自动执行搜索，这个`web_search` deepseek的response api也支持；
response = client.responses.create(
    model="gpt-5.5",
    input="2026年8月11日上海重大新闻？",
    tools=[{"type": "web_search"}],
)
print(response.output_text)            # 已带联网检索结果

# 2) 带选项的用法
response = client.responses.create(
    model="gpt-5.5",
    input="2026年8月11日上海重大新闻？",
    tools=[{
        "type": "web_search",
        "search_context_size": "medium",          # low / medium / high
        "user_location": {                          # 按地理位置优化结果
            "type": "approximate",
            "country": "CN",
            "city": "Shanghai",
            "region": "Shanghai",
        },
    }],
)

# 3) 输出里多了一个 web_search_call 项，最终消息带引用标注
for item in response.output:
    if item.type == "web_search_call":
        print("搜索动作：", item.action)          # 如 {"type": "search", "query": "..."}
    if item.type == "message":
        text = item.content[0].text
        cites = item.content[0].annotations        # 类型为 url_citation，含 url/title
        print(text, cites)
```
模型返回的message样例如下，在output里，通过`web_search_call`告诉你服务端执行的工具
```json
{
  "id": "resp_abc123",
  "object": "response",
  "created_at": 1723356789,
  "model": "gpt-4o-2024-08-06",
  "output": [
    {
      "id": "ws_68b9d1220b288199bf942a3e48055f3602e3b78a8dbf73ac",
      "type": "web_search_call",
      "status": "completed",
      "action": {
        "type": "search",
        "query": "2026年8月11日上海重大新闻"
      }
    },
    {
      "id": "msg_68b9d123f4788199a544b6b97e65673e02e3b78a8dbf73ac",
      "type": "message",
      "status": "completed",
      "role": "assistant",
      "content": [
        {
          "type": "output_text",
          "text": "今天上海的重大新闻包括：上海自贸区发布新一轮制度创新方案，将进一步扩大金融开放。",
          "annotations": [
            {
              "type": "url_citation",
              "start_index": 48,
              "end_index": 72,
              "url": "https://www.shanghai.gov.cn/nw12345/20260811/...",
              "title": "上海自贸区新一轮制度创新方案发布"
            }
          ]
        }
      ]
    }
  ],
  "usage": {
    "input_tokens": 25,
    "output_tokens": 180,
    "total_tokens": 205
  }
}
```

不需要对接 tavily 等外部 api 服务，不需要处理搜索结果解析——搜索的执行和限流都沉到了服务端，你只管发请求、收结果。对需要联网搜索、代码执行、文件检索的场景，这是开发体验上的质变。
>如果你之前是使用claude，codex等agent对接的国内的provider，要使用搜索的能力，你还得自行的配置搜索服务商；

---

#### 改进三: 工具的延迟加载和
**改进三: 工具的延迟加载和**

response api支持通过在 `tools` 定义中指定`defer_loading`来延迟加载工具的完整信息，默认只加载tool的名称，需要的时候再加载完整的工具信息，以减少工具对context的占用；
```json
{
  "model": "gpt-5.5",
  "instructions": "你是一个企业智能助手，根据用户需求调用合适的工具。",
  "input": "帮我查一下张三这个月的未回款金额",
  "tools": [
    {
      "type": "namespace",
      "name": "crm",
      "description": "CRM客户关系管理工具，包含客户查询、订单管理、回款查询等功能",
      "tools": [
        {
          "type": "function",
          "name": "get_customer_payment",
          "description": "查询客户月度未回款金额",
          "defer_loading": true,
          "parameters": {
            "type": "object",
            "properties": {
              "customer_name": {"type": "string"}
            },
            "required": ["customer_name"]
          }
        },
        {
          "type": "function",
          "name": "list_open_orders",
          "description": "查询客户未完成的订单列表",
          "defer_loading": true,
          "parameters": {
            "type": "object",
            "properties": {
              "customer_name": {"type": "string"}
            },
            "required": ["customer_name"]
          }
        }
      ]
    },
    {
      "type": "namespace",
      "name": "mes",
      "description": "MES生产执行系统工具，包含工单查询、产线状态等功能",
      "tools": [
        {
          "type": "function",
          "name": "query_work_order",
          "description": "查询车间工单完成率",
          "defer_loading": true,
          "parameters": {
            "type": "object",
            "properties": {
              "workshop_name": {"type": "string"}
            },
            "required": ["workshop_name"]
          }
        }
      ]
    },
    {
      "type": "tool_search"
    }
  ],
  "tool_search": {
    "enabled": true,
    "max_results": 2,
    "namespaces": ["crm"]
  }
}
```
另外，可以通过tool_search机制，在模型认为没有合适的工具来进行下一步的时候，要求client端来检索是否有合适的工具可用，检索到的工具通过`tool_search_output`返回给模型，这是一种更彻底的延迟加载，

交互示例如下，如下是一个查询订单物流的示例，初始请求中，仅提供client端的tool_search，
```json
{
  "model": "gpt-5.5",
  "input": "帮我查一下 order_42 的物流状态",
  "tools": [
    {
      "type": "tool_search",
      "execution": "client",
      "description": "Find project tools needed to continue the task.",
      "parameters": {
        "type": "object",
        "properties": {
          "goal": {"type": "string"}
        },
        "required": ["goal"],
        "additionalProperties": false
      }
    }
  ],
  "parallel_tool_calls": false
}
```
模型要求提供可用的工具来查询物流状态
```json
{
  "id": "resp_first",
  "object": "response",
  "output": [
    {
      "type": "tool_search_call",
      "execution": "client",
      "call_id": "call_abc123",
      "status": "completed",
      "arguments": {
        "goal": "Find the shipping ETA tool for order_42."
      }
    }
  ]
}
```
client返回工具检索结果，客户端(也就是agent)需要自行实现工具检索和匹配的逻辑，通过在input中返回`tool_search_output`
```python
second_response = client.responses.create(
    model="gpt-5.5",
    input=[
        # 把第一轮的 output 原样带上
        *first_response.output,
        # 加上 tool_search_output
        {
            "type": "tool_search_output",
            "execution": "client",
            "call_id": "call_abc123",       # 必须与 tool_search_call 的 call_id 一致
            "status": "completed",
            "tools": [                      # 你搜到的工具，带完整 schema
                {
                    "type": "function",
                    "name": "get_shipping_eta",
                    "description": "Look up shipping ETA details for an order.",
                    "defer_loading": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "order_id": {"type": "string"}
                        },
                        "required": ["order_id"],
                        "additionalProperties": False
                    }
                }
            ]
        }
    ]
)
```
通过这类增量检索的消息，并不会放到消息的头部，但是模型在后训练阶段对 `tool_search_output`有过针对性的训练，后续如果需要再使用工具的时候，也能够识别到`tool_search_output`的工具
如果用`v1/chat/completions`来实现增量工具检索，需要自行实现tools的双层路由，更新工具时，也可能导致kv缓存失效等问题。

---

#### 改进四：返回结果对象化
**改进四：返回结果对象化**
Chat Completions 的返回是 `choices[0].message`——一个「万能容器」，不管是文本、工具调用、还是拒绝回复，全塞在同一个 message 对象里。你要自己判断 `content` 是不是 null、`tool_calls` 存不存在、`refusal` 有没有值。

Responses API 的返回是 `output`——一个带类型的 item 数组：

```json
{
  "output": [
    {"type": "reasoning", "summary": [...]},
    {"type": "function_call", "call_id": "...", "name": "get_weather", "arguments": "{\"city\": \"北京\"}"},
    {"type": "message", "content": [{"type": "output_text", "text": "北京今天..."}]}
  ]
}
```
每个 item 有明确的 `type` 字段，IDE 的类型检查、代码补全都跟得上。你不用猜返回里有什么——遍历 `output` 数组，按 type 分发即可。SDK 还提供了便捷属性（如 `response.output_text`），终于不用写 `choices[0].message.content` 了。

在stream输出方面，Chat Completions 的流式事件只有一个 `data` 类型，chunk 里的 delta 要靠猜。Responses API 的事件是命名的、带语义的：
```shell
event: response.output_text.delta              → 文本增量
event: response.function_call_arguments.delta  → 工具调用参数增量
event: response.reasoning_summary_part.added   → 推理摘要
```
不需要从 `choices[0].delta.tool_calls[0].function.arguments` 这种路径里挑片段，事件名本身就告诉了你这个 chunk 是什么。
（不过代价同样真实：Responses 的流式事件有 20+ 种类型——`response.created` / `output_item.added` / `output_text.delta` / `function_call_arguments.delta` / `file_search_call.*` / `code_interpreter_call.*` / `response.completed` …… 客户端得写事件分发（switch / 映射表）而不是单纯读 delta；流也更「吵」，简单问答场景下大量元数据事件属于冗余。作为较新的 API，第三方范例和库的成熟度也不及 Chat Completions。所以语义化是双刃剑：信息更丰富，但解析更重。）


---

#### 改进五：reasoning 状态跨轮次持久化。
**改进五：reasoning 状态跨轮次持久化。**
Chat Completions 每轮调用都会丢失模型的内部推理过程（thinking tokens）。Responses API 通过 `previous_response_id` 串联多轮时，推理状态得以保留：
```python
resp1 = client.responses.create(
    model="gpt-5",
    input="这段代码的 bug 在哪里？[长代码...]",
    reasoning={"effort": "high"}
)

# 第二轮：模型还记得上轮的分析过程
resp2 = client.responses.create(
    model="gpt-5",
    input="好，帮我修复",
    previous_response_id=resp1.id
)
```

据 OpenAI 官方基准，TAU-Bench 上提升约 5%，缓存利用率比 Chat Completions 高 40–80%（这是 OpenAI 自家数据，需自行验证是否贴合你的任务）。对需要深度推理的任务（代码审查、数学证明、复杂决策链）收益明显。
不过，deepseek并没有支持这个能力，保留reasoning状态，也会占用额为对资源，所以这个能力实际的效果很难评。


---

### 代价与局限：Responses API 也不是银弹
**代价与局限：Responses API 也不是银弹**
Responses API用「把状态 / 工具 / 推理交给 OpenAI 服务端」换来了开发体验的提升，代价是绑定openai、数据出域、可控性下降，openai想把模型以外的更多的能力留在服务端，也容易形成用户对openai基础服务的依赖；
OpenAI 明确Chat Completions 不会废弃，对于各个开源Provider来说，未来很长一段时间应该也还是会把/v1/chat/completions作为主要API提供服务。