# DSH · Tool 与 LLM 的可靠性 & 安全护栏

一句话：**两条链路，两套护栏——模型调用靠"失败重试 + 可插拔决策"，工具调用靠"四阶段执行链 + 单调守卫 + 结构化错误"；贯穿两者的是一条铁律：取消从不抛弃已开始的工具体，每条调用在 session 里都有头有尾。**

分核心内置和可选插件两层。核心内置在 dsh-llm / dsh-tools / dsh-agent-loop 里，装上就有；可选插件（guard 包等）要挂载才有。

## 模型调用这条链

**失败重试有默认值，但不是无限重试。** `llm/llm/src/retry-policy.ts` 里的默认策略（normal 模式）：

- **默认重试 2 次**（第一次请求之外最多再重试 2 次）。
- 只重试**可恢复的错误码**，默认 5 个：`EMPTY_RESPONSE`、`RATE_LIMIT`、`SERVER`、`TIMEOUT`、`TRANSPORT`。业务类错误（比如参数不合法）不在列表里，不重试。
- **指数退避 + 抖动**：初始 500ms、上限 10s、抖动 0.1。
- 也可以配 `mode: 'always'`——无限重试直到成功、取消或销毁。

**重试的决策点是 `agent/request-error` 接缝。** 模型调用失败时，插件在这里决定 retry 还是抛错；llm-retry 包负责执行等待（指数退避期间可取消），每次重试都会落 `llm/retry`、`llm/retry-started` 日志事件。上一篇文章讲过的 compaction 溢出恢复就是这个接缝的典型用法：遇到 `CONTEXT_WINDOW_EXCEEDED`，先压缩再返回 retry。

## 工具执行这条链

**四阶段执行链，每段都能拦。** 一次工具调用走 `tools/pre-execute` → `tools/execute` → `tools/result` → `tools/post-execute`：

- **pre-execute**：插件可以 allow / deny（带原因）/ ask。ask 走审批服务，**没配审批服务就降级 deny**——默认是保守的。
- **ToolGuard 单调守卫**：pre-execute 之后、工具体之前还有一道闸，**只能否决、不能放行**——所以监听器顺序再乱，也没法把别人拒掉的调用放回来。这是防"谁都能放行"的关键设计。
- **execute**：around 包装链 + 工具体本体。超时插件挂在这。
- **post-execute**：可以 accept（改写内容、附上下文）或 block（把纠正反馈变成 error 结果回给模型）。spill 和结果修剪器挂在这。

**任何环节抛错都不炸循环。** 工具抛异常统一转成结构化的 `ToolExecutionResult`（`isError` + `error.message` + `error.info{name, code}`），以"错误消息"形式回给模型。参数解析也有兜底：非法 JSON 保留为文本、空输入映射成 `{}`，解析失败不挂。

## 调度与并发

- 工具分两种执行模式：**parallel**（滚动池，默认最多 10 个并行）和 **exclusive**（屏障，等当前池排空再跑）——"改完文件再读"这种必须串行的场景靠 exclusive 保证。
- **派发可以重叠，但结果和上下文严格按模型顺序提交**：只提交连续的前缀槽位，绝不乱序。
- 工具可以声明 `concludesTurn`——某个工具（比如审批类）返回后直接结束本轮，不再调模型。

## 取消与重放一致性（最下功夫的地方）

**取消从不抛弃已开始的工具体。** 三条保证：

1. 已经启动的工具 promise 必须跑到 quiescence，结果才标为 ABORTED——不会半路丢。
2. 没来得及启动的调用，记一条**合成的错误结果**（`TOOL_ABORTED_BEFORE_DISPATCH`），保证 session 里每条 `tool/call` 都有对应的 `tool/result`——**重放永远有效**。
3. 调度器内部失败时：停止新派发、排空已开始调用、抛出第一个失败，但**不伪造恢复结果**。

另外执行时会把调用方 signal 与包装器 signal **融合**（fuse），谁先中止都算数。

## 防循环与超时（可选插件）

**repeat-tool-reminder：重复调用检测，只提醒不拦截。** 默认连续重复阈值 `[3, 5, 8]`，触发分级提醒（第一次温和、后续点名工具+次数+参数预览）。关键设计：

- **有参数相同判断**：chain key 是 `[工具名, 参数canonical串]`，参数先做**深度 key 排序**再序列化——`{"a":1,"b":2}` 和 `{"b":2,"a":1}` 算同一个参数。
- **只提醒不否决**（"observe-and-enrich, never veto"）：提醒以 plugin 来源的用户消息塞回 inbox，让模型自己改主意。
- 用户中途插话会**重置计数**——跨用户消息的重复不算循环。
- 被 deny 的调用也计数——模型反复撞被拒的调用正是要打断的循环。

**timeout-policy：合作式超时。** 工具声明 `timeoutMs` 并承诺响应 `exec.signal`；包装器用 deadline 替换 signal，超时后把结果替换成结构化的 `TOOL_TIMEOUT` 错误（错误码可路由）。细节：用 code 作用域区分"自己的超时"和"外层包装器的超时"，不会误判；完成后恢复上游 signal，让 post-execute 看到原始状态。

## 上下文安全（可选插件）

- **spill**：工具输出超过阈值时，全文落盘到会话目录，上下文只放路径——防止单条结果撑爆窗口。
- **compaction-tool-result-pruner**：修剪工具结果，防上下文爆炸。

## 消息身份：工具结果怎么进对话

dsh 内部用 user 消息容器装工具结果（`source.kind='tool'`、内容块 `type: 'tool-result'`），但**发给模型时序列化成标准的 `role: 'tool'` 消息**（带 `tool_call_id`）——模型看到的是一条正统的工具结果消息。重复调用提醒才是 plugin 来源的用户消息。两条路径在 session 里身份分明，重放时各归各。

## 设计取向

一句话收尾：**错误全部结构化、全部落盘、全部按模型顺序；循环本身绝不被工具炸穿。** 能拦的在事件接缝上拦（pre-execute 否决、request-error 决定重试），拦不住的都变成给模型看的错误消息——重试有上限、重复有提醒、超时有兜底、取消有头有尾。
