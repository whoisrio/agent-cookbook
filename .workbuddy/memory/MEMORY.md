# 项目记忆：Agent Cookbook

## 项目定位
LLM Agent 实战教程仓库（cookbook 风格），定位"从工作流到自主 Agent 的完整教程"。

## 结构现状（2026-07-21 核查）
- `chapters/langgraph/`：LangGraph 系列教程，正文 01~09 + Middleware 专章（现 `05-langchain-middleware.md`，见下撞号）。09=Agent Loop，已拆为 `09-agent-loop.md`（核心：最简 loop + middleware 机制 + 默认内置 + 自定义 steering/followUp）与 `09-agent-loop-appendix.md`（Pi Agent 对比、停止机制七~7.4、未讲内容、节点附录）。第 08 章「旁白脚本工作流实战案例」仍待补。
  - **Middleware 专章编号现状（2026-08-05）**：昨日建的 `10-langchain-middleware.md` 已被用户改名成 `05-langchain-middleware.md`（完整专章：六类 hook、jump_to、内置库、子图嵌入、节点图、速查表）。这导致与 `05-langgraph-pregel.md` **撞号**（见已知问题）。`src/baby_agent/agent.py` 是 steering/followUp 可跑示例。
  - **09 主文重新内嵌 middleware 机制（2026-08-05）**：用户重写 09 时把一段 middleware 机制（create_agent 签名/源码摘录/6 hook/节点图/串并联）又写回 09 主文，与 05 专章存在内容重叠。当前设计是「09 在 Agent Loop 语境下引入、05 专章深讲」——重叠属有意但需留意。09 主文原空的「默认内置 middleware」章节已于今日补全（分类清单 + 指针指 05 第六节），未把 05 内容整段抄回。
- `chapters/deepagent/`：建了目录但为空，系列未动工。
- 配套 demo 代码项目：`/Users/rio/repos/myprjs/demos/py-langgraph-demo/`（**独立 git 仓库**，非本仓库子目录）。含 `langgraph.json`（可部署到 LangGraph Platform）、`app/basicGraph/`（promptChain* 覆盖 02~04 章）、`app/pregel/`（05 章）、`app/llm/`（llm.py、streamHelper.py）。覆盖 02~06 章。
- `src/agent_cookbook/`：`__init__.py` + `llm.py` + `stream_helper.py`，是 demo 中 `app/llm/` 下文件的镜像副本。**现为 examples/ 下 notebook 的公共依赖单一真相源**——notebook 通过 `sys.path` 加仓库根后 `from src.agent_cookbook import ...` 引入。`llm.py` 中 `.env` 已改为按仓库根相对路径读取（不再依赖 notebook 运行目录 cwd）。
- `examples/`：由 demos/py-langgraph-demo 转换填充——`01~06` 章对应 6 个 `.ipynb`（公共依赖统一引 `src.agent_cookbook`，**不再有** `_common` 拷贝）；含 `langgraph.json` 副本（供 06 演示）。`gen.py` 生成脚本已于 2026-07-22 删除（后续示例不再从 demo 自动生成，直接写在 notebook 里）。**示例归属（2026-07-22 更正）**：`01-workflow-patterns.ipynb` 承载 6 种 workflow pattern 的完整可跑示例（promptChain / generator-evaluator / orchestrator-worker / router / 嵌套子图 / agent），节点共用、每段代码前配 markdown 说明 + mermaid；`02-langgraph-basics.ipynb` 仅 LangGraph 基础（simpleGraph / promptChain / promptChainWithReview 三节 + 小结），不放 workflow 模式示例。两者 kernelspec 均为 `agent-cookbook`。07 章无代码。
- 教程（README + 7 章正文）**未引用** `demos/py-langgraph-demo`，读者无法得知配套代码位置。

## 已知问题（脚手架）
- ~~README 章节链接路径失效~~：已于 2026-07-21 修复，章节链接改为 `chapters/langgraph/*.md` 匹配实际目录。第 08 章文件尚未创建，README 中已标注"待补"。
- 配图 assets 引用问题：02 章 `chapters/langgraph/02-langgraph-basics.md` 引用 `![prompChain.png](../assets/promptchain.png)`，两处错误——①文件名大小写 `prompChain.png` ≠ 实际 `promptchain.png`；②路径 `../assets/` 从 chapters/langgraph/ 只回退到 chapters/，应为 `../../assets/`。另 03、06 章有多张图走 mintcdn.com 外链，发布前建议本地化到 assets/。
- 教程对接配套 demo 进展：README 已补「配套 Notebook 章节对照表」+ 快速开始（uv sync / .env / VSCode 内核自动识别说明）。notebook 公共依赖改为引 `src/agent_cookbook`（单一真相源），删除了重复的 `examples/_common/llm_utils.py`。**遗留**：7 章正文仍未提及 `demos/py-langgraph-demo`，读者需自行对照。**决策（2026-07-21）**：用户确认保留 `src/agent_cookbook`、不改为引 demo——cookbook 仓库自包含优先于消除与 demo 的重复。`src/agent_cookbook` 与 demo 的 `app/llm` 是两份同源副本（stream_helper 完全一致，llm.py 仅差 `.env` 路径修复），改一处需同步另一处，勿再提"合并到 demo"。
- ~~`.gitignore` 忽略 `*.ipynb`~~：已于 2026-07-21 加例外 `!examples/**/*.ipynb`，配套 notebook 可正常纳入版本控制。
- **编号撞车 + README 死链（2026-08-05 待用户拍板）**：`05-langchain-middleware.md`（middleware 专章，由昨日 10 改名）与 `05-langgraph-pregel.md`（Pregel 引擎）**两个 05 并存**；README 第 20 行仍链接已删除的 `10-langchain-middleware.md`、第 35 行写「10 Middleware」、且完全没列 `09-agent-loop-appendix.md`。需决定 middleware 专章的编号（建议恢复 10 或挪到 09 之后），并同步修 README 三处。
- **05 内置 middleware 清单不全（2026-08-05 核实）**：已对 `langchain.agents.middleware.__all__` 逐一核实，05 第六节漏列两个真实内置 `FilesystemFileSearchMiddleware`、`ProviderToolSearchMiddleware`；`dynamic_prompt` 是函数非类（描述无误）。05 里 `HumanInTheLoopMiddleware(interrupt_on=...)` 等示例参数未实际跑过，发布前建议实跑验证。

## 约定（2026-07-22，用户明确要求）
- **示例落点在 notebook，不在 .md**：系列教程的可跑示例代码统一写在 `examples/*.ipynb`；`chapters/` 下的 .md 教程正文只放示意性片段与概念讲解，不堆完整代码。补示例优先改 notebook。（之前曾误把 02 章后三种 workflow pattern 的完整代码写进 .md，已被用户纠正并撤回。）
