# Agent Cookbook

LLM Agent 实战指南——从工作流、Agent Loop 到事件驱动 Agent 的完整教程。
正文、配套 Notebook 与可运行代码分开放置：`books/` 写原理，`examples/` 放 Notebook 与演练脚本，`src/` 是可复用的 Python / TS 包。

## 仓库结构

```text
agent-cookbook/
├── books/                     # 正文（Markdown）
│   ├── langchain-langgraph/   # LangChain / LangGraph 系列
│   ├── event-driven-agent/    # 事件驱动 Agent 系列
│   ├── python/                # Python 异步与 Agent Loop
│   ├── dsh/                   # DeepSeek Harness（Agent Loop + Cordis）
│   ├── pi/                    # Pi Agent 架构与扩展
│   ├── hermes/                # hermes redirect 消息结构
│   ├── applications/          # 应用实践（RAG 等）
│   ├── obsolete/              # 历史草稿，仅存档
│   ├── 从 Completions 到 Responses.md
│   └── 从 Messages 到 Agent Loop.md
├── examples/                  # 配套 Notebook 与演练代码
│   ├── langgraph/             # LangGraph 系列 Notebook + langgraph.json
│   ├── python/                # 异步编程 Notebook
│   ├── dsh/cordis/            # Cordis 概念示例（TypeScript）
│   └── pi/welcome-msg/        # Pi 扩展示例
├── src/                       # Python 包
│   ├── agent_cookbook/        # 公共依赖：config / openai_client / structured / model / stream_json
│   ├── baby_agent/            # 带 steering / followup 的 CLI Agent
│   ├── baby_agent_ts/         # TS 版：createReactAgent + Ink TUI
│   └── baby_event_driven_agent/  # 事件驱动 Agent（含分阶段代码与测试）
├── assets/                    # 正文配图
├── .vscode/                   # 解释器 / Notebook / 调试配置
├── .env.example
└── pyproject.toml             # Python 依赖、脚本入口、pytest 配置
```

## 正文（books/）

### LangChain / LangGraph 系列

| 章节 | 内容 |
|------|------|
| [01 为什么需要工作流](books/langchain-langgraph/01-why-workflow.md) | 工作流 vs 单体 Agent，六种基础模式 |
| [02 LangGraph 基础](books/langchain-langgraph/02-langgraph-basics.md) | State、Node、Edge、StateGraph 与高级控制 |
| [03 进阶功能](books/langchain-langgraph/03-langgraph-advanced.md) | Checkpoint、HITL、Store、Time Travel |
| [04 Stream](books/langchain-langgraph/04-langgraph-stream.md) | Stream 模式与 Event Stream |
| [05 Pregel 引擎](books/langchain-langgraph/05-langgraph-pregel.md) | Actor/Channel 模型，编译与运行时 |
| [06 create_agent](books/langchain-langgraph/06-langgraph-create-agent.md) | LangChain 如何基于 LangGraph 构建 Agent Loop |
| [07 Tools](books/langchain-langgraph/07-tools.md) | 工具定义与调用 |
| [08 Context](books/langchain-langgraph/08-context.md) | 上下文工程（待补，文件暂为空） |
| [Deep Agents 源码解读](books/langchain-langgraph/deepagent/01-deepagents-architecture.md) | 在 `create_agent` 之上构建生产级 Agent |

### 事件驱动 Agent 系列

| 章节 | 内容 |
|------|------|
| [00 大纲](books/event-driven-agent/00-outline.md) | 七步进化总纲与写作约定 |
| [01 接收事件](books/event-driven-agent/01-receive-events.md) | 事件、总线、订阅分发、append-only session log |
| [02 收件箱与 Steering](books/event-driven-agent/02-inbox-steering.md) | 收件箱 + 常驻 worker，steering / followup |
| [03 用户中断](books/event-driven-agent/03-interrupt.md) | 中断正在飞的那一步，loop 仍存活 |

### Python 基础

| 章节 | 内容 |
|------|------|
| [Python 异步编程](books/python/01-python-async.md) | 把 I/O 等待期间的 CPU 用起来 |
| [事件驱动的 agent-loop](books/python/agent-loop.md) | 收件箱、分发、中断与 message 格式 |

### DSH（DeepSeek Harness）

| 章节 | 内容 |
|------|------|
| [Agent Loop 怎么转](books/dsh/dsh-agent-loop.md) | 三层 while 嵌套与事件接缝 |
| [Tool 与 LLM 的可靠性](books/dsh/dsh-tool-llm-reliability.md) | 安全护栏 |
| [Cordis 核心原理](books/dsh/cordis/dsh-cordis-core-mech.md) | 插件 / 服务依赖与生命周期 |
| [Cordis 总结](books/dsh/cordis/dsh-cordis-summary.md) | 一句话原理与要点回顾 |
| [Cordis 故事版](books/dsh/cordis/dsh-cordis-story-only.md) | 用故事讲清机制 |
| [Cordis 完整故事](books/dsh/cordis/dsh-extension-cordis-full-story.md) | 核心工作原理长文 |
| [Cordis 代码样例](books/dsh/cordis/dsh-cordis-code-example.md) | 孔太斯大楼咖啡店 |

### Pi / Hermes

| 章节 | 内容 |
|------|------|
| [Pi 基础介绍](books/pi/pi-basics.md) | monorepo 包清单与依赖关系 |
| [Pi 扩展能力](books/pi/pi-extension-capabilities-v2.md) | 如何扩展 Agent 能力 |
| [hermes redirect](books/hermes/redirect-message-shapes.md) | 各场景下发给 LLM 的消息结构 |

### 应用实践与单篇

| 章节 | 内容 |
|------|------|
| [RAG 基础](books/applications/rag/rag-intros.md) | 向量库选型等入门笔记 |
| [BGE 向量检索索引与重排配置建议](books/applications/rag/BGE向量检索索引与重排配置建议.md) | 检索索引与重排配置 |
| [从 Completions 到 Responses](books/从%20Completions%20到%20Responses.md) | LLM API 演化与多 Provider 适配 |
| [从 Messages 到 Agent Loop](books/从%20Messages%20到%20Agent%20Loop.md) | 三家 API 字段差异与 Agent Loop |

> `books/obsolete/` 为历史草稿（middleware、部署、前端等），仅存档，不再维护。

## 配套代码（examples/ 与 src/）

### Notebook（`examples/`）

| Notebook | 说明 |
|----------|------|
| `examples/langgraph/01-workflow-patterns.ipynb` | orchestrator-worker 结构演示（无需 API Key） |
| `examples/langgraph/02-langgraph-basics.ipynb` | StateGraph / 节点 / 边 / 条件边 / 画图（需 Key） |
| `examples/langgraph/03-advanced-checkpoint-hitl-store.ipynb` | Checkpoint / HITL / Store / Time Travel（需 Key） |
| `examples/langgraph/04-streaming.ipynb` | 流式 / 自定义事件 / Stream 转换器（需 Key） |
| `examples/langgraph/05-pregel-engine.ipynb` | Actor / Channel / Topic（无需 Key） |
| `examples/langgraph/06-deployment.ipynb` | `langgraph.json` 与 `langgraph dev` 说明 |
| `examples/langgraph/07-langchain-create-agent.ipynb` | `create_agent` 用法（需 Key） |
| `examples/langgraph/08-response-api.ipynb` | Responses API 演示 |
| `examples/python/async-basics.ipynb` | Python 异步基础 |
| `examples/python/async-demo.ipynb` | 异步并发展示 |

Notebook 的公共依赖（`model` / `openai_client` / `config` / `stream_json`）来自仓库内的
`src/agent_cookbook` 包，运行时会自动加入路径并导入，无需手动安装。
`examples/langgraph/langgraph.json` 供 `langgraph dev` 使用。

### Python 包（`src/`）

| 包 | 说明 |
|------|------|
| `src/agent_cookbook/` | 公共依赖：`config` / `openai_client` / `structured`（原生 OpenAI 结构化输出） / `model`（langchain，仅 07 用） / `stream_json` |
| `src/baby_agent/` | 带 steering / followup 的 CLI Agent（Textual TUI），含 `tests/` |
| `src/baby_agent_ts/` | TypeScript 版：`createReactAgent` + Ink TUI（`npm start`） |
| `src/baby_event_driven_agent/` | 事件驱动 Agent：`bus` / `agent` / `extensions` / `trajectory`，含 `demo.py` |
| `src/baby_event_driven_agent/stages/` | 分阶段可运行代码，与正文一一对应 |
| `src/baby_event_driven_agent/tests/` | 总线与 Agent Loop 的测试 |

事件驱动系列每个 stage 自成包，`pyproject.toml` 里已注册 demo / test 脚本：

```bash
uv run stage01-demo   # stage01 真模型演示    uv run stage01-test   # stage01 离线测试
uv run stage02-demo   # stage02 收件箱/steering
uv run stage03-demo   # stage03 中断
```

### TypeScript 示例

```bash
# Cordis 概念示例（examples/dsh/cordis）
cd examples/dsh/cordis
npm install
npm run 01            # 运行单个；npm run all 运行全部

# TS 版 Baby Agent（src/baby_agent_ts）
cd src/baby_agent_ts
npm install
npm start
```

## 快速开始

```bash
# 1. 安装依赖（会创建 .venv，含 jupyter / ipykernel / langgraph 等）
uv sync

# 2. 准备环境变量：在仓库根目录生成 .env，填入你的 LLM 配置
cp .env.example .env
#   编辑 .env，至少填 OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_MODEL

# 3. 打开 Notebook
#   - VSCode：本仓库已带 .vscode/settings.json，解释器自动锁定 .venv，
#     打开 examples/ 下的 .ipynb 时内核会自动选择 "Python (agent-cookbook)"；
#   - 命令行：uv run jupyter lab
```

运行 CLI Agent：

```bash
uv run python -m baby_agent                      # 默认模型
uv run python -m baby_agent --model gpt-4o-mini  # 指定模型
```

> 注意：调用真实模型的 Notebook（02 / 03 / 04 / 07）和 `stage0x-demo` 必须先完成第 2 步的 `.env`；
> 01 / 05 / 06 以及各 `stage0x-test` 不需要 Key（测试使用 Fake / Scripted LLM）。
