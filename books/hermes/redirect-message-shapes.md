# hermes redirect：各场景下发给 LLM 的消息结构

源码基线：`/Users/rio/repos/opensource-refs/hermes-agent`（2026-09-08）。

redirect 的语义是「**同一个 turn 内原地转向**」：取消当前这次模型请求，把用户的纠正插成一条真实的
user message，循环重试。已完成的工作保留，turn 不重新开始。

它不是「重定向到另一个任务或会话」。

---

## 一、先判断：redirect 到底会不会发生

`agent/interrupt_control.py:228` 的 `redirect()` 有三道前置判断，按顺序：

| 顺序 | 条件 | 结果 |
|---|---|---|
| 1 | `not text or not text.strip()` | 返回 `False` |
| 2 | Codex app-server 有原生 `request_steer` | 走原生 `turn/steer`，**不走本文档的任何消息结构** |
| 3 | `_executing_tools` 为真 | **降级为 `steer()`**（见 Case 4） |
| 4 | `_model_request_active` 为 None 或未 set | 返回 `False` |
| 5 | `_interrupt_requested` 已为真且无 pending redirect | 返回 `False`（`/stop` 优先） |

只有穿过全部判断，才会写 `_pending_redirect` 并置 `_interrupt_requested = True`。

**返回 `False` 意味着这次纠正不会进入当前 turn**，调用方（如 `gateway/run_busy.py:475`）会退回
queue 模式，作为下一个新 turn 处理。

---

## 二、Case 矩阵

| Case | 触发时机 | 可见文本 | 尾巴角色 | 是否补 assistant |
|---|---|---|---|---|
| 1 | 模型生成中 | 有 | 非 assistant | 补，内容为可见文本 |
| 2 | 模型生成中 | 无（纯 thinking） | 非 assistant | 补 hidden 空壳 |
| 3 | 模型生成中 | 任意 | 已是 assistant | **不补**，checkpoint 折进 user |
| 4 | 工具执行中 | — | — | 不补，降级为 steer |
| 5 | 响应已返回但 redirect 到达（竞态） | — | — | 丢弃响应，走 Case 1/2/3 |
| 6 | 同一 turn 内多次 redirect | — | — | 文本累加后按 Case 1/2/3 处理 |

---

## 三、各 Case 的具体消息结构

补消息的统一入口是 `_apply_active_turn_redirect`（`agent/conversation_loop.py:273`），调用点在
`begin_iteration`（`agent/turn_iteration_prep.py:316`）——注意**不是中断那一刻**，中断时只置了
`restart_with_redirected_messages` 标记就 break 了。

### Case 1：模型生成中被打断，已有可见文本

假设模型已经说了「我准备用 SQLite 实现，先建表」，用户说「改用 Postgres」。

追加两条：

```python
{"role": "assistant", "content": "我准备用 SQLite 实现，先建表"}

{"role": "user", "content": "改用 Postgres",
 "api_content": "[Context from the interrupted assistant response]\n"
               "[This response was interrupted by a user correction.]\n\n"
               "Visible response before the interruption:\n\n"
               "我准备用 SQLite 实现，先建表\n\n改用 Postgres"}
```

wire 上（经 `substitute_api_content` 替换后）模型看到的是：

```
assistant: 我准备用 SQLite 实现，先建表

user:      [Context from the interrupted assistant response]
           [This response was interrupted by a user correction.]

           Visible response before the interruption:

           我准备用 SQLite 实现，先建表

           改用 Postgres
```

### Case 2：模型生成中被打断，一个可见字都没吐（纯 thinking）

```python
{"role": "assistant", "content": "",
 "display_kind": "hidden",
 "api_content": "[response interrupted]"}

{"role": "user", "content": "改用 Postgres",
 "api_content": "[Context from the interrupted assistant response]\n"
               "[This response was interrupted by a user correction.]\n\n改用 Postgres"}
```

wire 上：

```
assistant: [response interrupted]

user:      [Context from the interrupted assistant response]
           [This response was interrupted by a user correction.]

           改用 Postgres
```

第一条是**空壳**，`display_kind: "hidden"` 让它在 UI 上完全不可见。它存在的唯一目的是占住
assistant 的位置，保证 role 交替合法。

### Case 3：尾巴已经是 assistant

不补 placeholder（`conversation_loop.py:296` 的 `if not (messages and messages[-1].get("role") == "assistant")`）。
checkpoint 照常折进 user 的 `api_content`，直接 append 一条 user 消息：

```python
{"role": "user", "content": "改用 Postgres",
 "api_content": "[Context from the interrupted assistant response]\n"
               "[This response was interrupted by a user correction.]\n\n"
               "Visible response before the interruption:\n\n"
               "<visible>\n\n改用 Postgres"}
```

### Case 4：工具执行中被 redirect —— 降级为 steer

**这个 case 根本不产生 redirect 消息。** 源码注释（`interrupt_control.py:248`）：
*"Never kill a tool to deliver guidance"*。

走的是 `steer()`，文本挂到最后一条 tool result 的末尾（`agent_runtime_helpers.py:3149`）：

```python
# 直接改 tool 消息的 content，不新增消息
target["content"] = existing_content + format_steer_marker(steer_text)
```

mark 形如（`agent/prompt_builder.py:512`）：

```
[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position; not tool output and not a new delivery when replayed from conversation history]
改用 Postgres
[/OUT-OF-BAND USER MESSAGE]
```

这个 marker 是自描述的：它声明自己是用户原话、不是工具输出、重放历史时不是新投递。
注释里解释了原因——早期用裸的 "User guidance:" 时，模型把它当 prompt injection 拒绝执行。

同时 redirect 会给工具 worker 发 `_request_yield`，让前台 terminal 命令把进程交给后台
registry 立即返回，否则一个 `sleep 300` 能把纠正堵五分钟。

### Case 5：响应已返回，redirect 同时到达（竞态）

`turn_api_call.py:142`。判定条件：响应已拿到，但 `_pending_redirect` 非空。

**整个响应被丢弃**（不 append 到 messages），置 `restart_with_redirected_messages = True` 后
break，然后按 Case 1/2/3 重建。注释说得很清楚：*"discard the now-stale response and rebuild
from the correction rather than lose it"*。

### Case 6：同一 turn 内多次 redirect

累加发生在 `redirect()` 里（`interrupt_control.py:271`），不是 drain 时：

```python
self._pending_redirect = (
    f"{existing}\n\n[Additional user correction]\n{cleaned}" if existing else cleaned
)
```

所以两次 redirect 后：`_pending_redirect = "first\n\n[Additional user correction]\nsecond"`，
drain 时一次性取出，整体作为 `text` 拼进 `correction`。

---

## 四、为什么保留可见文本，却不保留 CoT

`_apply_active_turn_redirect` 拿的是 `agent._strip_think_blocks(...)`，剥掉 thinking 后的**可见文本**。
原始思维链从没进过 `_current_streamed_assistant_text`——`stream_delivery.py:305` 在累积前就过了
think scrubber。

保留可见文本的原因：

1. **role 交替是硬约束**。不补 assistant 占位，user 纠正就直接踩在 user/tool 上，严格 provider 会拒绝或幻觉。
2. **避免重复劳动**。模型知道上一版说到哪，不会把讲过的重讲一遍。

不留原始 CoT 的原因更重（`conversation_loop.py:276` 原话）：

> raw chain-of-thought never enters replayable content (inlined CoT reads as a prefill jailbreak and
> bricks the session with empty-response storms)

在 assistant 位置塞内容形同冒充模型输出，触发分类器后会持续返回空响应，session 永久报废。
`turn_api_call.py:177` 另一行注释更直接：*"Never materialize incomplete signed/encrypted reasoning items"*。

---

## 五、两个容易忽略的机制

**一、被取消的那次调用不计迭代预算。** `turn_iteration_prep.py:404`：

```python
if _retry.restart_with_redirected_messages:
    api_call_count -= 1
    agent.iteration_budget.refund()
```

理由是「没产出有效的 assistant item」。否则用户纠正三次就白吃掉三轮预算。

**二、一存两用。** `content` 进 transcript、给用户看；`api_content` 才是发给 provider 的。
替换发生在 `substitute_api_content`（`turn_context.py:96`），调用点 `chat_completion_helpers.py:1966`。

---

## 六、常量与源码索引

| 常量 | 值 | 位置 |
|---|---|---|
| `_INTERRUPT_SCAFFOLD_MARKER` | `[This response was interrupted by a user correction.]` | `conversation_loop.py:65` |
| `_INTERRUPTED_PLACEHOLDER` | `[response interrupted]` | `agent_runtime_helpers.py:2320` |
| `STEER_MARKER_OPEN` | `[OUT-OF-BAND USER MESSAGE — a direct message...]` | `prompt_builder.py:505` |
| `STEER_MARKER_CLOSE` | `[/OUT-OF-BAND USER MESSAGE]` | `prompt_builder.py:509` |

关键源码位置：

- `redirect()` —— `agent/interrupt_control.py:228`
- `_apply_active_turn_redirect()` —— `agent/conversation_loop.py:273`
- 消费点（drain + 补消息）—— `agent/turn_iteration_prep.py:316`
- 中断判定与预算退还 —— `agent/turn_api_call.py:175`、`agent/turn_iteration_prep.py:404`
- steer 落到 tool result —— `agent/agent_runtime_helpers.py:3149`
- `api_content` 替换 —— `agent/turn_context.py:96`
- 边界测试 —— `tests/run_agent/test_steer.py:66-99`

调用 redirect 的上层：`hermes_cli/cli_tui_mixin.py:1416`、`gateway/run_busy.py:517`、
`acp_adapter/server.py:701`、`tui_gateway/methods_session.py:2015`。
