## 七、Agent 怎么停下来：LangGraph 的停止机制

讲完了怎么转和怎么重启，还有一件事没讲：**怎么停。**

没它你没法做工程级的 Agent——用户按了取消你得停、工具执行抛了致命异常你得停、上下文窗口快爆了你也得停。LangGraph 的停止机制分三层，每一层适用不同场景。

### 7.1 默认停止：没 tool_calls 就停

这是 `create_agent` 的内建逻辑，等价于最简 Loop 里的 `if not response.tool_calls: return`。模型输出里没有 tool_calls → 进入 `after_agent` → END。这是"正常停止"——该做的事做完了，该查的 followUp 也查了。

### 7.2 jump_to = "end"：middleware 主动叫停

你在 middleware 里返回 `{"jump_to": "end"}`，Agent 在下一步直接跳结束。这在两个场景里特别有用：

**场景 1：上下文快爆了。** `before_model` 里检查 messages 的 token 数，超了就优雅退出，不让 LLM 报 `prompt too long`。

```python
@before_model(can_jump_to=["end"])
def context_guard(state: CustomerServiceState, runtime: Runtime) -> dict | None:
    """上下文过长时主动叫停。"""
    total_chars = sum(len(str(m.content)) for m in state["messages"])
    if total_chars > 100_000:
        return {
            "messages": [AIMessage(content="对话太长，请开始新会话。")],
            "jump_to": "end",
        }
    return None
```

**场景 2：工具执行致命错误。** `after_model` 里检测到模型请求了危险操作，直接挡掉并终止。

```python
@after_model(can_jump_to=["end"])
def safety_gate(state: CustomerServiceState, runtime: Runtime) -> dict | None:
    """如果模型要执行危险操作，直接终止。"""
    last_msg = state["messages"][-1]
    dangerous_calls = [
        tc for tc in getattr(last_msg, "tool_calls", [])
        if tc.get("name") in ("delete_order", "refund_all")
    ]
    if dangerous_calls:
        return {
            "messages": [AIMessage(content="该操作需要人工审核，已终止。")],
            "jump_to": "end",
        }
    return None
```

注意 `can_jump_to=["end"]` 声明——middleware 默认不允许跳转，你得显式声明才能用 `jump_to`。

### 7.3 interrupt()：暂停等人工

第三章 HITL 里详细讲过的机制。在关键节点暂停，等人工确认后再继续。跟 `jump_to = "end"` 的区别：interrupt 是"等一下"，jump_to 是"不干了"。

### 7.4 对照 Pi Agent：stopReason 的粒度

Pi Agent 把停止原因分成 5 种，每种走不同的后续路径：

| stopReason | 含义 | 后续行为 |
|------------|------|---------|
| `toolUse` | 有工具调用 | 继续内层循环 |
| `stop` | 自然终止 | 正常停，检查 followUp |
| `length` | token 截断 | 同上，但说明任务可能没做完 |
| `error` | 调用异常 | **硬停止**，不检查 followUp |
| `aborted` | 用户中止 | **硬停止**，不检查 followUp |

LangGraph 没有这种"按停止原因分流"的内建机制。但你可以用 middleware 实现等价物：

- **正常停**：默认行为，模型不输出 tool_calls → `after_agent` 检查 followUp
- **硬停止**：在 `after_model` 或 `before_model` 里判断异常条件，设 `jump_to = "end"`。由于 `jump_to` 是 `EphemeralValue`（用完即清），下一圈不会残留。
- **区分 stop 和 length**：你可以在 `after_model` 里检查 `response_metadata["finish_reason"]`，如果是 `"length"` 就标记一个 `stop_reason` 到 state 里。

两者的设计哲学差异在于：Pi Agent 把 stopReason 作为 Loop 的一等概念，因为 Loop 是它自己写的。LangGraph 把停止交给 middleware 和调用方，因为图的停止逻辑是引擎管的事。两种方式都能做，但 Pi 的 stopReason 让你在 Loop 层做细粒度决策时更顺手。


## 十、没有讲的东西

这一章聚焦在编排层的 **Execution Orchestration**（执行编排）上。四大职责中另外三个——Context Management、Memory Management、Observability——各自都能写一章，不在本章范围。具体来说：

- **Context Management**（上下文组装）——每次调 LLM 前怎么拼 Prompt（static/dynamic 分区、KV Cache 命中），什么时候触发压缩。这是下一章的重点，也是将本章的 Agent Loop 从"能跑"推进到"生产级"的关键一步。
- **Memory Management**（记忆管理）——短期 history 和长期经验的存储、检索、摘要。LangGraph 的 Store 和 checkpointer 已经提供了基础设施，但"什么时候记、记什么、怎么取"的策略需要单独讲。
- **Observability**（可观测性）——编排层每一步的完整 Trace（Prompt、推理结果、工具调用、返回值）。这部分依赖 LangSmith，在部署和监控相关的章节展开。
- **工具系统的五步管道**（参数校验、权限拦截、执行、结果后处理）——这属于工具设计层，对应 `wrap_tool_call` middleware，会在上下文工程章提到但重点是"工具结果怎么裁剪"，不是完整的工具管道设计。
- **AgentMessage 到 LLM Message 的转换层**——`create_agent` 直接操作 `BaseMessage`，没有 Pi Agent 那种内部 7 种消息类型的设计，不需要这层转换。

## 十一、下一站

Agent 能自己决定下一步了。但它的决定依赖"看到了什么"——上下文怎么构建、对话历史怎么管理。下一章来看 LangGraph 的 Store 怎么帮你做长期记忆和上下文管理。

---

> **配套 Notebook**：`examples/09-agent-loop.ipynb`（待创建）。包含本章全部可运行代码，含 steering 和 followUp 两个场景的完整演示。

> **Pi Agent 参考**：本章的双层 Loop 设计参考了 [Pi Agent Book 第 3 章](https://dg-ai-notes.pages.dev/modules/ch03-agent-loop)。Pi 用 `getSteeringMessages` / `getFollowUpMessages` 钩子（每次循环检查队列），LangChain 用 `before_model` / `after_agent` middleware（在生命周期 hook 点注入）。`jump_to` 机制等价于 Pi 的外层循环 `continue`。


---

## 附录1，Agent Loop Graph中各节点的补充说明

### 各节点内部逻辑

#### before_agent / before_model

每次执行时：
1. middleware 在 hook 里可修改 state（如注入消息、设置 `jump_to`）
2. 返回 state 更新，或 `None` 不更新
3. 可通过 `jump_to` 跳转到 model / tools / end，跳过后续节点

#### model 节点

```
① 收到 ModelRequest（messages、tools、response_format、system_message）
② wrap_model_call 拦截链执行（outer → inner → 实际调用）
③ _get_bound_model：根据 response_format 策略绑定 tools 到 model
④ model.invoke(messages) 调用 LLM
⑤ _handle_model_output：处理响应
   - ProviderStrategy：parse content 为 structured_response
   - ToolStrategy：检测 structured_output tool call，parse args
   - 普通：直接返回 AIMessage
⑥ 返回 ModelResponse（messages + 可选 structured_response）
```

#### after_model / after_agent

每次执行时：
1. middleware 在 hook 里可修改 state
2. 可通过 `jump_to` 跳转到 model / tools / end
3. after_model 在**每次循环**执行；after_agent 在**退出循环后**执行一次

#### tools 节点

```
① 收到 tool_calls（通过 Send 并行分发）
② wrap_tool_call 拦截链执行（outer → inner → 实际执行）
③ 执行 tool，返回 ToolMessage
④ 不含 built_in_tools（dict 格式，provider 侧执行）
⑤ 不含 structured_output_tools（虚拟 tool，不需执行）
```

#### model_to_tools 路由逻辑

| 条件 | 目的地 |
|---|---|
| middleware 设置了 `jump_to` | 按 jump_to 路由（model / tools / end） |
| 无 AIMessage | exit_node（END 或 after_agent） |
| AIMessage.tool_calls 为空 | exit_node ← **经典退出条件** |
| 有 pending tool_calls | `[Send("tools", [tc]) for tc in pending]` ← 并行分发 |
| 有 structured_response | exit_node |
| 有 tool_calls 但无 pending | loop_entry_node（artificial ToolMessage 注入场景） |

#### tools_to_model 路由逻辑

| 条件 | 目的地 |
|---|---|
| 无 AIMessage | loop_entry_node |
| 所有 tool 都是 `return_direct=True` | exit_node |
| structured_output tool 已执行 | exit_node |
| 默认 | loop_entry_node（回到循环继续） |

---

### 节点数量说明

上图每个 hook 只画了一个节点，实际数量取决于 middleware 的实现情况：

| 条件 | 产生的节点 |
|---|---|
| 有 N 个 middleware 实现了 `before_agent` | N 个 `before_agent` 节点，按注册顺序串联 |
| 有 N 个 middleware 实现了 `before_model` | N 个 `before_model` 节点，按注册顺序串联 |
| 有 N 个 middleware 实现了 `after_model` | N 个 `after_model` 节点，按**注册反序**串联 |
| 有 N 个 middleware 实现了 `after_agent` | N 个 `after_agent` 节点，按**注册反序**串联 |
| 有 tools 或有 `wrap_tool_call` middleware | 1 个 `tools` 节点 |
| 以上都没有 | 无额外节点 |

例如 `create_agent(..., middleware=[A, B, C])`，三个都实现了全部 hook：

```
节点总数 = 3(before_agent) + 3(before_model) + 1(model)
         + 3(after_model) + 1(tools) + 3(after_agent)
         = 14 个节点

连接顺序:
before_agent:  A → B → C → loop_entry    （正序）
before_model:  A → B → C → model         （正序）
after_model:   C → B → A → 条件边        （反序）
after_agent:   C → B → A → END           （反序）
```