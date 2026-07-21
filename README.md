# Agent Cookbook

LLM Agent 实战指南——从工作流到自主 Agent 的完整教程。

## 目录

### LangGraph 系列

| 章节 | 内容 |
|------|------|
| [01 为什么需要工作流](chapters/01-why-workflow.md) | 工作流 vs 单体 Agent，六种基础模式 |
| [02 LangGraph 基础](chapters/02-langgraph-basics.md) | State、Node、Edge、StateGraph |
| [03 进阶功能](chapters/03-langgraph-advanced.md) | Checkpoint、HITL、Store、Time Travel |
| [04 Stream](chapters/04-langgraph-stream.md) | Stream 模式与 Event Stream |
| [05 Pregel 引擎](chapters/05-langgraph-pregel.md) | Actor/Channel 模型，编译与运行时 |
| [06 部署](chapters/06-langgraph-deployment.md) | SDK 嵌入、Agent Server、云端部署 |
| [07 前端](chapters/07-langgraph-frontend.md) | React/Vue/Svelte 前端库对接 |
| [08 实战：旁白脚本工作流](chapters/08-langgraph-case-study.md) | 端到端案例 |

> 示例代码见 `examples/` 目录。更多内容（Agent 等）陆续补充。

## 快速开始

```bash
# 安装依赖
uv sync

# 设置环境变量
cp .env.example .env
# 编辑 .env，填入你的 LLM API 配置

# 启动 Jupyter
uv run jupyter lab
```
