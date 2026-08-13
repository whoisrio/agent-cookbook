# create_agent 关键逻辑分析

## 概述

`create_agent` 是 LangChain 的 agent 工厂函数，基于 LangGraph 的 `StateGraph` 构建一个 ReAct agent。
核心流程：model → tool_calls → model → tool_calls → … → 无 tool_calls 时退出。

---

## 1. 参数总览

| 参数 | 作用 | 备注 |
|---|---|---|
| `model` | LLM 模型 | 字符串或 `BaseChatModel` 实例 |
| `tools` | agent 可用的工具列表 | 支持 `BaseTool`、callable、dict（server-side tools） |
| `system_prompt` | 系统提示词 | 转为 `SystemMessage` 注入消息列表头部 |
| `middleware` | 中间件列表 | 可拦截 model/tool 调用、注入 hooks |
| `response_format` | 结构化输出约束 | 让 agent 最终输出符合指定 schema |
| `state_schema` | 自定义状态 schema | 扩展 `AgentState` 的字段 |
| `context_schema` | 运行时上下文 schema | 定义 `runtime.context` 的类型（如 user_id、api_key） |
| `checkpointer` | 状态持久化 | 对话记忆、中断恢复 |
| `store` | 跨线程存储 | 多对话/多用户间共享数据 |
| `cache` | 节点级缓存 | **实际未生效**（见分析） |
| `interrupt_before/after` | 中断点 | HITL（人机交互）场景 |
| `transformers` | 流式转换器 | 自定义 stream 处理逻辑 |

---

## 2. response_format：结构化输出

### 解决的问题

Agent 正常输出是自由文本。`response_format` 让最终输出强制符合定义的 schema，使结果可被程序消费。

### 三种策略

| 策略 | 原理 | 适用场景 |
|---|---|---|
| `ToolStrategy` | schema 包装成虚拟 tool，`tool_choice="any"` 强制调用 | 所有模型的兜底方案 |
| `ProviderStrategy` | 用模型厂商原生 structured output（如 OpenAI `response_format: json_schema`） | 厂商支持时更可靠 |
| `AutoStrategy` | 自动检测：支持原生则用 ProviderStrategy，否则退回 ToolStrategy | 传 Pydantic 类时的默认策略 |

### 关键流程

1. 传入 Pydantic 类 → 自动包装为 `AutoStrategy`
2. 检测模型能力 → 决定用 `ProviderStrategy` 还是 `ToolStrategy`
3. `ToolStrategy`：schema → 虚拟 tool → 绑定到 model → model 调用 → parse args → 写入 `state["structured_response"]`
4. `ProviderStrategy`：schema → `response_format` kwargs → model 直接输出 JSON → parse content

### 核心结论

`response_format` 不是 tool_use 的 response，是 **agent 的输出约束**。
解决的是 agent 输出的**可集成性**，不是推理能力。

---

## 3. tools：工具分类与处理

### 两类 tools

```python
built_in_tools = [t for t in tools if isinstance(t, dict)]     # server-side
regular_tools = [t for t in tools if not isinstance(t, dict)]   # client-side
```

| | `built_in_tools`（dict） | `regular_tools`（BaseTool/callable） |
|---|---|---|
| 进 `ToolNode`？ | ❌ | ✅ |
| 本地执行？ | ❌ provider 侧执行 | ✅ 客户端执行 |
| 工具验证？ | ❌ 跳过 | ✅ 验证 |
| 典型示例 | OpenAI `file_search`、`code_interpreter` | 用户自定义的任何 tool |

### 流程

```
tools (用户传入)
├── dict (如 {"type": "file_search"})
│   └── 透传给 model.bind_tools()，provider 侧执行，不进 ToolNode
│
└── BaseTool / callable
    └── 进 ToolNode → 转 BaseTool → model.bind_tools()
        → model 返回 tool_call → 路由到 ToolNode 本地执行
```

---

## 4. middleware：中间件机制

### 4.1 middleware 可定义的能力

| 能力 | 类型 | 说明 |
|---|---|---|
| `state_schema` | `type[StateT]` | 自定义状态字段 |
| `tools` | `Sequence[BaseTool]` | 注册额外工具（类属性声明） |
| `transformers` | `Sequence[TransformerFactory]` | 流式转换器 |

### 4.2 Hook 体系

```
before_agent (执行一次)
  └→ before_model (每次循环开始)
      └→ model 节点
          └→ after_model (每次循环结束)
              └→ [有 tool_calls?] → tools → 回到 before_model
              └→ [无 tool_calls?] → after_agent (执行一次) → END
```

每个 hook 都有 sync 和 async 两个版本。

### 4.3 wrap_model_call / wrap_tool_call 与内置库

`wrap_model_call` / `wrap_tool_call` 怎么包住调用链、`jump_to` 怎么改流程、多个 middleware 怎么串、以及 `langchain.agents.middleware` 自带的内置库（重试、降级、限流、PII、HITL、压缩等），完整讲法在 [第 10 章：Middleware](10-langchain-middleware.md)。本源码剖析只保留上面两点实现洞察：`is not` 对象身份比较判断 hook 是否重写，以及 `wrap_*` 要求 sync/async 双实现否则抛 `NotImplementedError`。

---

## 5. context_schema：运行时上下文

不是 messages，是调用者注入的**运行时元数据**：

```python
@dataclass
class MyContext:
    user_id: str
    api_key: str

graph = create_agent(..., context_schema=MyContext)
graph.invoke(inputs, context=MyContext(user_id="u123", api_key="sk-..."))
```

在 middleware / tool 里通过 `runtime.context` 访问。

| | `state` | `runtime.context` |
|---|---|---|
| 生命周期 | 贯穿整个 graph，跨节点持久化 | 单次调用，不持久化 |
| 可变性 | 可被节点修改 | frozen，只读 |
| 典型数据 | messages, structured_response | user_id, api_key, tenant_id |

---

## 6. cache：节点级缓存（实际未生效）

### 结论：`create_agent` 的 `cache` 参数是死代码

证据链：

1. `create_agent` 把 `cache` 传给 `graph.compile(cache=cache)`
2. 但所有节点（model、tools、middleware hooks）加进去时**都没配 `cache_policy`**
3. 执行时所有 task 的 `cache_key` 都是 `None`
4. `match_cached_writes()` 永远不命中缓存
5. `put_writes()` 检查 `cache_key is None` 直接 return，从不写入

### cache 的理论用途

`cache` + `CachePolicy` 是 LangGraph 的通用 graph 能力，适用于**确定性节点**（相同输入 → 相同输出）。
但 agent 场景下：
- LLM 节点：不确定性，缓存无意义
- Tool 节点：通常有副作用或依赖外部状态，缓存危险
- 重试：checkpoint 已覆盖

**对 `create_agent` 使用者来说，`cache` 参数可以忽略。**

---

## 7. 图结构详解

### 7.1 四个关键节点角色

| 角色 | 确定逻辑 | 说明 |
|---|---|---|
| `entry_node` | 有 before_agent → 第一个 before_agent；有 before_model → 第一个 before_model；否则 → model | 图的入口，执行一次 |
| `loop_entry_node` | 有 before_model → 第一个 before_model；否则 → model | agent 循环的起点，tools 回到这里 |
| `loop_exit_node` | 有 after_model → 第一个 after_model；否则 → model | 每次循环的终点，条件边从这里出发 |
| `exit_node` | 有 after_agent → 最后一个 after_agent；否则 → END | 图的出口，执行一次 |

### 7.2 完整总图（所有 middleware + tools + structured_output）

假设 middleware 列表 `[A, B]`，两个 middleware 都实现了全部 hook：

```
START
  │
  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    before_agent 区域（执行一次）                      │
│                                                                     │
│  A.before_agent ──→ B.before_agent                                  │
│                          │                                          │
│         可通过 jump_to 跳转到: model / tools / end                   │
└─────────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    agent loop（可执行多次）                           │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │              before_model 区域                                │  │
│  │                                                               │  │
│  │  A.before_model ──→ B.before_model                            │  │
│  │                          │                                    │  │
│  │         可通过 jump_to 跳转到: model / tools / end             │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                          │                                          │
│                          ▼                                          │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  model 节点                                                    │  │
│  │                                                                │  │
│  │  1. 收到 ModelRequest（含 messages、tools、response_format）    │  │
│  │  2. wrap_model_call 拦截链：A.wrap → B.wrap → 实际调用          │  │
│  │  3. 绑定 tools（含 structured_output_tools）到 model           │  │
│  │  4. model.invoke(messages)                                     │  │
│  │  5. _handle_model_output 处理响应                              │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                          │                                          │
│                          ▼                                          │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │              after_model 区域                                 │  │
│  │                                                               │  │
│  │  B.after_model ──→ A.after_model  （注意：反序）                │  │
│  │                          │                                    │  │
│  │         可通过 jump_to 跳转到: model / tools / end             │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                          │                                          │
│                          ▼                                          │
│              ┌───────────────────────┐                              │
│              │     条件边路由         │                              │
│              │  (model_to_tools)     │                              │
│              └───────────────────────┘                              │
│                    │         │         │                             │
│          ┌─────────┘         │         └──────────┐                 │
│          ▼                   ▼                    ▼                  │
│     [to tools]      [to loop_entry]        [to exit_node]           │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  tools 节点（并行执行）                                         │  │
│  │                                                                │  │
│  │  每个 tool_call 通过 Send 并行分发：                             │  │
│  │    wrap_tool_call 拦截链：A.wrap → B.wrap → 实际执行             │  │
│  │                                                                │  │
│  │  包含：                                                         │  │
│  │    - 用户 tools（BaseTool / callable）                          │  │
│  │    - middleware tools                                           │  │
│  │    - ❌ 不含 built_in_tools（dict，provider 侧执行）             │  │
│  │    - ❌ 不含 structured_output_tools（虚拟 tool，不需执行）       │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                          │                                          │
│                          ▼                                          │
│              ┌───────────────────────┐                              │
│              │     条件边路由         │                              │
│              │  (tools_to_model)     │                              │
│              └───────────────────────┘                              │
│                    │                    │                            │
│          ┌─────────┘                    └──────────┐                │
│          ▼                                        ▼                 │
│  [to loop_entry]                           [to exit_node]           │
│  (回到 before_model)                                               │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    after_agent 区域（执行一次）                       │
│                                                                     │
│  B.after_agent ──→ A.after_agent  （注意：反序）                     │
│                          │                                          │
│         可通过 jump_to 跳转到: model / tools / end                   │
└─────────────────────────────────────────────────────────────────────┘
                          │
                          ▼
                        END
```

### 7.3 条件边路由详细逻辑

**model_to_tools 路由**（从 loop_exit_node 出发）：

```
loop_exit_node 输出
    │
    ├─ 有 jump_to? ──→ 按 jump_to 路由 (model / tools / end)
    │
    ├─ 无 AIMessage? ──→ exit_node
    │
    ├─ tool_calls 为空? ──→ exit_node  ← 经典退出条件
    │
    ├─ 有 pending tool_calls?
    │   └─→ [Send("tools", [tc]) for tc in pending]  ← 并行分发
    │
    ├─ 有 structured_response? ──→ exit_node
    │
    └─ 有 tool_calls 但无 pending ──→ loop_entry_node
        (artificial tool messages 注入，如 HITL 场景)
```

**tools_to_model 路由**（从 tools 节点出发）：

```
tools 输出
    │
    ├─ 无 AIMessage? ──→ loop_entry_node
    │
    ├─ 所有 tool 都是 return_direct=True? ──→ exit_node
    │
    ├─ structured_output tool 已执行? ──→ exit_node
    │
    └─ 默认 ──→ loop_entry_node
```

**model_to_tools 目的地集合**：

```
base: ["tools", exit_node]
+ response_format 存在 或 loop_exit_node != "model": 加入 loop_entry_node
```

**tools_to_model 目的地集合**：

```
base: [loop_entry_node]
+ 有 return_direct=True 的 tool 或 structured_output_tools: 加入 exit_node
```

### 7.4 场景一：无 middleware、有 tools（最常见）

```
START
  ↓
model ◄──────────────────┐
  ↓                      │
  [条件边]               │
  ├→ tools ──────────────┘  (有 pending tool_calls)
  └→ END                    (无 tool_calls)
```

### 7.5 场景二：无 middleware、无 tools、有 structured_output

```
START
  ↓
model ◄──────┐
  ↓          │
  [条件边]   │
  ├→ model ──┘  (structured_response 未生成，重试)
  └→ END        (structured_response 已生成)
```

### 7.6 场景三：无 middleware、无 tools、无 structured_output

```
START
  ↓
model
  ↓
END
```

单次调用，无循环。

### 7.7 场景四：有 middleware（完整版）

假设有 2 个 middleware：
- `A`：实现了 before_agent、before_model、wrap_model_call
- `B`：实现了 after_model、after_agent、wrap_tool_call

```
START
  ↓
A.before_agent (执行一次)
  ↓
┌→ A.before_model
│    ↓
│  model ◄──────────────────┐
│    ↓                      │
│  B.after_model            │
│    ↓                      │
│  [条件边]                 │
│  ├→ tools ────────────────┘  (有 pending tool_calls)
│  ├→ A.before_model           (有 response_format 或 jump_to)
│  └→ B.after_agent            (退出循环)
│         ↓
│  A.after_agent (执行一次)
│         ↓
│       END
```

**关键细节**：这些执行时机与 `jump_to` 跳转规则，统一在 [第 10 章：Middleware](10-langchain-middleware.md) 讲。下面只强调编译期结论。

### 7.8 middleware chain 的连接顺序

多个 middleware 按注册顺序串联：

```python
create_agent(..., middleware=[A, B, C])
```

```
before_agent:  A.before_agent → B.before_agent → C.before_agent → loop_entry
before_model:  A.before_model → B.before_model → C.before_model → model
model
after_model:   C.after_model → B.after_model → A.after_model → 条件边
after_agent:   C.after_agent → B.after_agent → A.after_agent → END
```

注意：before 是正序（A→B→C），after 是**反序**（C→B→A），类似栈的嵌套。连接顺序的规则与示例见 [第 10 章：Middleware](10-langchain-middleware.md)。

---

## 8. 核心设计模式总结

| 模式 | 说明 |
|---|---|
| **AutoStrategy 自动降级** | 检测模型能力，自动选 ProviderStrategy 或 ToolStrategy |
| **虚拟 tool 实现结构化输出** | ToolStrategy 把 schema 包装成 tool，`tool_choice="any"` 强制调用 |
| **洋葱模型拦截** | wrap_model_call / wrap_tool_call 组合成链，可修改请求/响应 |
| **is not 检测重写** | 用对象身份比较判断 middleware 是否重写了 hook 方法 |
| **sync/async 严格一致** | wrap 类 hook 要求两条路径都实现，否则报错而非静默跳过 |
| **server-side vs client-side tools** | dict 格式透传给 provider，BaseTool 进 ToolNode 本地执行 |
