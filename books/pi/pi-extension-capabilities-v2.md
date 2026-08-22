# Pi Agent 如何扩展 Agent 能力

## 包清单与能力

关于 Pi 的整个架构，相信你已经看过不少视频介绍了，这里简单复习一下：

- 最核心的底座是 `pi-ai`，对不同 Provider 的 API 调用做了统一封装，在 Pi 的语义模型下统一了消息格式与流式输出格式；
- 在 `pi-ai` 之上，`pi-agent-core` 是通用 agent 运行时引擎，提供 `Agent` 类、`agentLoop` / `runAgentLoop` 主循环、transport 抽象、状态管理、附件（attachment）支持，以及 harness 层（session、skills、system-prompt、tools 注册表、prompt templates 等）；
- `pi-tui` 是终端 UI 库，通过差分渲染提供流畅的终端交互体验；
- `pi-coding-agent` 构建在 `pi-agent-core` 和 `pi-tui` 之上，提供 CLI、read / bash / edit / write 工具、session 管理、扩展系统、命令系统、模型注册等。它负责把扩展挂到 core 的 `Agent` 上，是你编写 extension、启动 Agent 的主入口。

此外，Pi 还提供了与监控、评估（eval）相关的 `pi-evals` 和 `pi-telemetry`，以及远端服务组件 `pi-protocol`、`pi-client`、`pi-server` 等。

Pi 的扩展能力都由 `pi-coding-agent` 开放出来，`packages/coding-agent/src/core/extensions/types.ts` 中的 `Extension` 接口定义了扩展的数据结构（即运行时如何持有每个扩展）：

```typescript
export interface Extension {
  path: string;                        // 原始路径（CLI -e 临时扩展是 <temporary:...> 合成路径）
  resolvedPath: string;                // 文件系统解析后的绝对路径，jiti 靠它 import
  hidden?: boolean;                    // 可选：是否从"启动扩展列表"隐藏（展示开关，不影响逻辑）
  sourceInfo: SourceInfo;              // 来源信息（source: local/temporary… + baseDir），报错/诊断定位用
  handlers: Map<string, HandlerFn[]>;  // ← 事件订阅表：api.on 写、ExtensionRunner.emit 读
  tools: Map<string, RegisteredTool>;  // ← 注册的工具：api.registerTool 写、getAllRegisteredTools 聚合
  messageRenderers: Map<string, MessageRenderer>;  // 自定义消息条目渲染器
  markdownTransformer?: MarkdownTransformer;       // 可选：单个 markdown 变换函数
  entryRenderers?: Map<string, EntryRenderer>;     // 可选：自定义 entry 渲染器（配 appendEntry）
  commands: Map<string, RegisteredCommand>;        // 斜杠命令：api.registerCommand 写
  flags: Map<string, ExtensionFlag>;              // CLI 标志：api.registerFlag 写
  shortcuts: Map<KeyId, ExtensionShortcut>;        // 快捷键：api.registerShortcut 写
}
```

这些 `Map` 对象就是扩展能力的实际落点。`extension` 通过 `ExtensionAPI` 注册时，对应类型的数据会被写入各自的 `Map` 中：

如下插件监听了 "session_start" 事件，注册了一个greet工具
```typescript
//如下插件监听了 "session_start" 事件，注册了一个greet工具
export default function (pi: ExtensionAPI) {
  pi.on("session_start", async (event, ctx) => {
    ...
  });

  pi.registerTool({
    name: "greet",
    label: "Greet",
    description: "Greet someone by name",
    parameters: Type.Object({
      name: Type.String({ description: "Name to greet" }),
    }),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      ...
    },
  });
}
```

`ExtensionAPI`提供了`on`和`registerXXX`方法，将插件添加到对应的map中，Pi的插件机制是非常标准的观察者设计模式；
```typescript
function createExtensionAPI(
	extension: Extension,
	runtime: ExtensionRuntime,
	cwd: string,
	eventBus: EventBus,
): ExtensionAPI {
	const api = {
		// Registration methods - write to extension
		on(event: string, handler: HandlerFn): void {
			runtime.assertActive();
			const list = extension.handlers.get(event) ?? [];
			list.push(handler);
			extension.handlers.set(event, list);
		},

		registerTool(tool: ToolDefinition): void {
			runtime.assertActive();
			extension.tools.set(tool.name, {
				definition: tool,
				sourceInfo: extension.sourceInfo,
			});
			runtime.refreshTools();
		},
    ...
  } as ExtensionAPI;

	return api;
}
```

插件逻辑的执行基本上有两种方式：
- 通过事件激活
- 查表激活
下面咱们挨个看一下。

## 通过事件激活

从 agent 启动到结束（包括中间的 agent loop 和各类命令执行），Pi 在每一个关键执行节点都会通过 `await emit` 向事件总线发送事件。例如当有待注入的 pending 消息时，loop 会先 `emit` 出 `message_start` / `message_end` 来包裹该消息——这正是消息落库前后的钩子时机。

```typescript
async function runLoop(
	initialContext: AgentContext,
	newMessages: AgentMessage[],
	initialConfig: AgentLoopConfig,
	signal: AbortSignal | undefined,
	emit: AgentEventSink,
	streamFunction: StreamFn,
): Promise<void> {
	...
	// Outer loop: continues when queued follow-up messages arrive after agent would stop
	while (true) {
		let hasMoreToolCalls = true;
    ...
			// Process pending messages (inject before next assistant response)
			if (pendingMessages.length > 0) {
				for (const message of pendingMessages) {
					await emit({ type: "message_start", message });
					await emit({ type: "message_end", message });
					currentContext.messages.push(message);
					newMessages.push(message);
				}
				pendingMessages = [];
			}
      ...
    }
    ...
  }
}
```

因为是 `await`，loop 会等待所有监听了该事件的 extension 都完成事件消费，再进行下一步。

通过 `pi.on` 订阅事件注册的扩展，都是消息消费类的插件，具体的消费逻辑在 `ExtensionRunner` 中：

```typescript
async emit<TEvent extends RunnerEmitEvent>(event: TEvent): Promise<RunnerEmitResult<TEvent>> {
		...
		for (const ext of this.extensions) {
			const handlers = ext.handlers.get(event.type);
			if (!handlers || handlers.length === 0) continue;

			for (const handler of handlers) {
				try {
					const handlerResult = await handler(event, ctx);
					...
				} catch (err) {
					...
				}
			}
		}

		return result as RunnerEmitResult<TEvent>;
	}
```

下面这个 extension 的作用是：用户启动 Pi 时打印一行欢迎词：

```typescript
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
export default function (pi: ExtensionAPI) {
  pi.on("session_start", async (event, ctx) => {
    // 只在 agent 真正“启动”时打招呼；reload / resume / fork 不打扰
    //if (event.reason !== "startup" && event.reason !== "new") return;

    const message = "大哥，又来玩啦";
    console.log(message); // 字面打印到终端
    ctx.ui.notify(message, "info"); // TUI 里弹一条通知，保证看得见
  });
}
```

## 查表激活

另一类扩展（如 tools）提供额外能力，它在 `buildRuntime` 时主动加载进运行时，过程如下：

```shell
discoverAndLoadExtensions (loader.ts:697)
  → jiti import 模块 → 工厂执行
    → api.registerTool → extension.tools.set (loader.ts:269)   ← 第一跳：登记
AgentSession 构造 / reload
  → _buildRuntime (agent-session.ts:404 / 2747)
    → _refreshToolRegistry (agent-session.ts:2591)             ← 第二跳：进表
      → getAllRegisteredTools (runner.ts:451) 跨扩展聚合
      → wrapRegisteredTools (wrapper.ts:43)
      → _toolRegistry → setActiveToolsByName
      → Agent 的工具表（agent-session.ts 中的 _toolRegistry）   ← agent 真正持有的工具表
```

`getAllRegisteredTools` 的具体逻辑就是从注册的 extension 中，聚合所有 extension 注册的 tools：

```typescript
getAllRegisteredTools(): RegisteredTool[] {
		const toolsByName = new Map<string, RegisteredTool>();
		for (const ext of this.extensions) {
			for (const tool of ext.tools.values()) {
				if (!toolsByName.has(tool.definition.name)) {
					toolsByName.set(tool.definition.name, tool);
				}
			}
		}
		return Array.from(toolsByName.values());
	}
```

需要说明的是：声明在配置中的扩展会在 Pi 启动时自动加载；只有启动之后才新增或修改的扩展，才需要执行 `/reload` 重新加载。新增 / 修改的扩展之所以能免编译即生效，依赖的是 jiti 提供的按需加载能力。

## 常用的事件触发点

Pi 提供的事件触发点非常多，下面是 Pi 启动以及常见命令执行时的事件触点：

```shell
pi starts
  │
  ├─► project_trust (user/global and CLI extensions only, before project resources load)
  ├─► session_start { reason: "startup" }
  └─► resources_discover { reason: "startup" }

/new (new session) or /resume (switch session)
  ├─► session_before_switch (can cancel)
  ├─► session_shutdown
  ├─► session_start { reason: "new" | "resume", previousSessionFile? }
  └─► resources_discover { reason: "startup" }

/fork or /clone
  ├─► session_before_fork (can cancel)
  ├─► session_shutdown
  ├─► session_start { reason: "fork", previousSessionFile }
  └─► resources_discover { reason: "startup" }

/name or pi.setSessionName()
  └─► session_info_changed

/compact or auto-compaction
  ├─► session_before_compact (can cancel or customize)
  ├─► session_compact (success)
  └─► session_compact_failed (failure or abort)

/tree navigation
  ├─► session_before_tree (can cancel or customize)
  └─► session_tree

/model or Ctrl+P (model selection/cycling)
  ├─► thinking_level_select (if model change changes/clamps thinking level)
  └─► model_select

thinking level changes (settings, keybinding, pi.setThinkingLevel())
  └─► thinking_level_select

exit (Ctrl+C, Ctrl+D, SIGHUP, SIGTERM)
  └─► session_shutdown
```

agent-loop 的事件触点如下，如果你是基于 Pi 构建面向自己业务场景的 agent，用得比较多的触点，大概是跟工具相关的那几个：

```shell
user sends prompt ─────────────────────────────────────────┐
  │                                                        │
  ├─► (extension commands checked first, bypass if found)  │
  ├─► input (can intercept, transform, or handle)          │
  ├─► (skill/template expansion if not handled)            │
  ├─► before_agent_start (can inject message, modify system prompt)
  ├─► agent_start                                          │
  ├─► message_start / message_update / message_end         │
  │                                                        │
  │   ┌─── turn (repeats while LLM calls tools) ───┐       │
  │   │                                            │       │
  │   ├─► turn_start                               │       │
  │   ├─► context (can modify messages)            │       │
  │   ├─► before_provider_headers (can mutate headers)     |
  │   ├─► before_provider_request (can inspect or replace payload)
  │   ├─► after_provider_response (status + headers, before stream consume)
  │   │                                            │       │
  │   │   LLM responds, may call tools:            │       │
  │   │     ├─► tool_execution_start               │       │
  │     ├─► tool_call (can block)              │       │
  │   │     ├─► tool_execution_update              │       │
  │   │     ├─► tool_result (can modify)           │       │
  │   │     └─► tool_execution_end                 │       │
  │   │                                            │       │
  │   └─► turn_end                                 │       │
  │                                                        │
  ├─► agent_end                                            │
  └─► agent_settled (no retry/compaction/follow-up left)   │
                                                           │
user sends another prompt ◄────────────────────────────────┘
```

## Pi 扩展机制小结

Pi 的扩展点植入逻辑与时机，都由框架代码显式定义。（dsh 的机制留待后续章节补充。）
